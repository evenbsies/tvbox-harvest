"""Command line interface.

The same commands serve three callers: a human debugging locally, the GitHub workflows, and
``python -m harvest.cli`` inside Actions steps. Two conventions keep that workable:

* Anything a workflow needs to consume goes to stdout in a machine-readable form (notably
  ``plan``, which emits a GitHub Actions matrix).
* Anything a human needs goes through ``rich`` and stays out of stdout when ``--quiet``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from harvest.config import get_settings
from harvest.fetch.http import HttpClient
from harvest.generate import tvbox as tvbox_mod
from harvest.generate.report import build_run_comment, combine, combined_paths, load_runs
from harvest.generate.verify import verify_config_file
from harvest.models import SiteConfig
from harvest.pipeline import (
    RunOptions,
    load_recipe,
    load_registry,
    recipe_path,
    registry_entry_from,
    resolve_sites,
    run_pipeline,
    save_recipe,
    save_registry,
)

app = typer.Typer(add_completion=False, help="Harvest VOD sources and generate TVBox configs.")
console = Console()


# --------------------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------------------


@app.command()
def probe(
    url: str | None = typer.Argument(None, help="Site URL (creates/updates a recipe)"),
    site: str | None = typer.Option(None, "--site", help="Existing site key to re-probe"),
    key: str | None = typer.Option(None, "--key", help="Recipe key to use for --url"),
    tier: int | None = typer.Option(None, "--tier", help="Force a tier (skips earlier probes)"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Skip Tier-4 browser probing"),
    save: bool = typer.Option(True, "--save/--no-save", help="Write the recipe file"),
    out: Path | None = typer.Option(None, "--out", help="Write the probe JSON result here"),
) -> None:
    """Detect which adapter a site needs, and persist the evidence."""
    from harvest.probe.detector import ProbeContext, detect, normalize_base_url, site_from_url

    settings = get_settings()

    async def _run() -> dict[str, Any]:
        if site:
            config = load_recipe(site, settings)
            if config is None:
                console.print(f"[red]no recipe for key={site!r}[/red]")
                raise typer.Exit(code=1)
        elif url:
            config = load_recipe(key, settings) if key else None
            if config is None or config.base_url.rstrip("/") != normalize_base_url(url):
                config = site_from_url(url, key=key)
        else:
            console.print("[red]provide a URL or --site[/red]")
            raise typer.Exit(code=2)

        async with HttpClient(settings) as http:
            ctx = ProbeContext(
                settings=settings, http=http, allow_browser=not no_browser, force_tier=tier
            )
            result = await detect(config, ctx)

        if save and result.automatable:
            from harvest.pipeline import apply_probe_patch

            config = apply_probe_patch(config, result)
            path = save_recipe(config, settings)
            console.print(f"[green]recipe written:[/green] {path}")
            registry = load_registry(settings)
            registry.add_or_update(registry_entry_from(config))
            save_registry(registry, settings)
        return result.model_dump()

    payload = asyncio.run(_run())
    _print_probe(payload)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _print_probe(payload: dict[str, Any]) -> None:
    table = Table(title="probe result", show_lines=False)
    table.add_column("field")
    table.add_column("value")
    table.add_row("tier", str(payload.get("tier")))
    table.add_row("adapter", str(payload.get("adapter")))
    table.add_row("status", str(payload.get("status")))
    if payload.get("detail_url_sample"):
        table.add_row("detail sample", str(payload["detail_url_sample"]))
    console.print(table)

    evidence = payload.get("evidence") or {}
    for stage in evidence.get("stages") or []:
        keys = {k: v for k, v in stage.items() if k != "stage"}
        compact = json.dumps(keys, ensure_ascii=False)
        console.print(f"  [cyan]{stage.get('stage')}[/cyan] {compact[:400]}")
    for note in evidence.get("notes") or []:
        console.print(f"  [dim]note: {note}[/dim]")


# --------------------------------------------------------------------------------------
# collect / generate / verify
# --------------------------------------------------------------------------------------


@app.command()
def collect(
    site: str = typer.Option("", "--site", help="Comma-separated site keys (empty = all)"),
    mode: str = typer.Option("full", "--mode", help="probe | full | incremental"),
    since_hours: int | None = typer.Option(None, "--since-hours"),
    max_titles: int | None = typer.Option(None, "--max-titles"),
    max_requests: int | None = typer.Option(None, "--max-requests"),
    gateway: str = typer.Option("", "--gateway", help="Gateway base URL for bridged sites"),
    flavors: str = typer.Option(",".join(tvbox_mod.FLAVORS), "--flavors"),
    reprobe: bool = typer.Option(False, "--reprobe"),
    no_browser: bool = typer.Option(False, "--no-browser"),
    verify: bool = typer.Option(True, "--verify/--no-verify"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Do not write snapshots or configs"),
    budget_minutes: int = typer.Option(45, "--budget-minutes"),
    record_fixtures: bool = typer.Option(
        False,
        "--record-fixtures",
        help="Write fetched responses to tests/fixtures/<key>/ for review (opt-in; never automatic)",
    ),
) -> None:
    """Run the full harvest pipeline for the selected sites."""
    options = RunOptions(
        mode=mode,
        sites=[s.strip() for s in site.split(",") if s.strip()],
        since_hours=since_hours,
        max_titles=max_titles,
        max_requests=max_requests,
        gateway_base=gateway,
        flavors=tuple(f.strip() for f in flavors.split(",") if f.strip()),
        reprobe=reprobe,
        allow_browser=not no_browser,
        dry_run=dry_run,
        verify=verify,
        time_budget_seconds=budget_minutes * 60,
        record_fixtures=record_fixtures,
    )
    report = asyncio.run(run_pipeline(options))
    _print_run_report(report)


def _print_run_report(report) -> None:  # noqa: ANN001 - RunReport, avoids a circular import
    table = Table(title="harvest run", show_lines=False)
    for col in ("key", "tier", "adapter", "status", "vods", "lines", "eps", "filtered", "ms"):
        table.add_column(col)
    for site in report.sites:
        table.add_row(
            site.key,
            f"T{site.tier}",
            site.adapter,
            site.status,
            str(site.vods),
            str(site.lines),
            str(site.episodes),
            str(site.filtered_total),
            str(site.elapsed_ms),
        )
    console.print(table)

    for item in report.generated:
        console.print(
            f"[green]generated[/green] {item['flavor']}: {item['path']} ({item['sites']} sites)"
        )
        if item.get("skipped"):
            for skipped in item["skipped"]:
                console.print(f"  [yellow]skipped[/yellow] {skipped['key']}: {skipped['reason']}")
    for item in report.verification:
        color = {"ok": "green", "snapshot": "cyan", "failed": "red"}.get(item.get("status", ""), "white")
        console.print(f"  [{color}]verify {item.get('key')}: {item.get('status')}[/{color}]")
    for note in report.notes:
        console.print(f"[yellow]note[/yellow] {note}")


@app.command()
def generate(
    all_sites: bool = typer.Option(True, "--all/--no-all"),
    flavors: str = typer.Option(",".join(tvbox_mod.FLAVORS), "--flavors"),
    gateway: str = typer.Option("", "--gateway"),
    run_id: str = typer.Option("", "--run-id"),
    carry_over: bool = typer.Option(
        True,
        "--carry-over/--no-carry-over",
        help="Keep published entries for enabled sites this run has no data for",
    ),
) -> None:
    """Rebuild TVBox configs from existing normalized data, without re-harvesting.

    Data on disk is only ever the subset that the most recent collect produced. Everything else
    that should be published is carried forward from the previous config, because the alternative
    is worse: a run covering one site would otherwise publish a one-site subscription, and the
    Issue intake path always runs one site at a time.
    """
    settings = get_settings()
    pairs: list[tuple[SiteConfig, Any]] = []
    for path in sorted(settings.normalized_dir.glob("*.json")):
        recipe = load_recipe(path.stem, settings)
        if recipe is None:
            continue
        from harvest.models import SiteData

        data = SiteData.model_validate(json.loads(path.read_text(encoding="utf-8")))
        pairs.append((recipe, data))

    # The sites that ought to be in the published config: every enabled registry site. This is
    # the same rule the collect pipeline uses to pick sites, deliberately -- two different
    # notions of "should be published" is how a config ends up disagreeing with the ledger.
    keep: set[str] = set()
    if carry_over:
        keep = {site.key for site in resolve_sites(RunOptions(sites=[], mode="full"), settings)}

    if not pairs:
        # Nothing fresh on disk. That is the normal outcome of a run in which every site failed,
        # and the right response is to republish the enabled set from the previous config rather
        # than to abort. Aborting was worse in two ways: the message ("run `harvest collect`
        # first") reads like a code defect when the real story is that the sites did not answer,
        # and the run then never reached its summary or its jsDelivr purge.
        #
        # Still an error when there is nothing to carry forward -- a first run with no data is a
        # genuine mistake, and saying so is more useful than writing an empty subscription.
        previous_exists = any(
            (settings.tvbox_dir / name).exists() for name in tvbox_mod.produced_index_files()
        )
        if not (keep and previous_exists):
            console.print("[red]no normalized data found; run `harvest collect` first[/red]")
            raise typer.Exit(code=1)
        console.print(
            "[yellow]no fresh data; republishing the enabled sites from their last published "
            "entries[/yellow]"
        )

    generated = tvbox_mod.generate_all(
        pairs,
        settings=settings,
        gateway_base=gateway,
        flavors=tuple(f.strip() for f in flavors.split(",") if f.strip()),
        run_id=run_id,
        carry_over=keep,
    )
    for item in generated:
        console.print(f"[green]{item.flavor}[/green] -> {item.path} ({item.site_count} sites)")
        if item.skipped:
            for skip in item.skipped:
                console.print(f"  skipped {skip['key']}: {skip['reason']}")
        if item.carried_over:
            # Say it out loud: a carried-forward entry is last-run data, not this run's.
            console.print(
                f"  [yellow]carried over {len(item.carried_over)} site(s) with no data this "
                f"run: {', '.join(item.carried_over)}[/yellow]"
            )


@app.command()
def verify(
    targets: str = typer.Option("data/tvbox/index.json", "--targets"),
) -> None:
    """Simulate TVBox's own requests against the generated config."""
    settings = get_settings()

    async def _run() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        async with HttpClient(settings) as http:
            for target in [t.strip() for t in targets.split(",") if t.strip()]:
                path = Path(target)
                if not path.is_absolute():
                    path = settings.root / path
                if not path.exists():
                    console.print(f"[red]missing target: {path}[/red]")
                    continue
                for result in await verify_config_file(path, http=http, settings=settings):
                    results.append(result.as_dict())
        return results

    results = asyncio.run(_run())
    table = Table(title="verification")
    for col in ("key", "status", "list", "detail", "search", "messages"):
        table.add_column(col)
    failed = 0
    for item in results:
        if item["status"] == "failed":
            failed += 1
        table.add_row(
            item["key"],
            item["status"],
            str(item.get("list")),
            str(item.get("detail")),
            str(item.get("search")),
            "; ".join(item.get("messages") or [])[:120],
        )
    console.print(table)
    if failed:
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------------------
# plan (GitHub Actions matrix)
# --------------------------------------------------------------------------------------


@app.command()
def plan(
    sites: str = typer.Option("", "--sites"),
    tiers: str = typer.Option("", "--tiers"),
    output: Path | None = typer.Option(None, "--output"),
) -> None:
    """Emit a GitHub Actions build matrix, one entry per site.

    The matrix is what lets a single bad site fail without taking the run down, and lets
    Tier-1/2 sites be batched into one job while Tier-4 sites get their own.
    """
    settings = get_settings()
    options = RunOptions(
        sites=[s.strip() for s in sites.split(",") if s.strip()],
        tiers=[int(t) for t in tiers.split(",") if t.strip().isdigit()],
    )
    configs = resolve_sites(options, settings)

    include: list[dict[str, Any]] = []
    for config in configs:
        include.append(
            {
                "site": config.key,
                "tier": config.tier(),
                "adapter": config.adapter,
                "needs_browser": config.tier() >= 4,
            }
        )

    matrix = {"include": include}
    payload = f"matrix={json.dumps(matrix, separators=(',', ':'))}"
    if output:
        output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


# --------------------------------------------------------------------------------------
# Issue parsing
# --------------------------------------------------------------------------------------

FIELD_MAP = {
    "站点地址": "site_url",
    "site url": "site_url",
    "站点名称": "site_name",
    "site name": "site_name",
    "适配器类型": "adapter",
    "adapter": "adapter",
    "关注分类": "categories",
    "categories": "categories",
    "备注": "notes",
    "notes": "notes",
}

ADAPTER_MAP = {
    "auto": "auto",
    "苹果cms标准接口": "maccms_v10",
    "苹果cms": "maccms_v10",
    "自定义json接口": "json_custom",
    "json": "json_custom",
    "html静态站": "html_ssr",
    "html": "html_ssr",
    "需要浏览器嗅探": "playwright_sniff",
    "浏览器嗅探": "playwright_sniff",
}

BLOCKED_HOST_PATTERNS = [
    re.compile(r"^localhost$", re.I),
    re.compile(r"^127\."),
    re.compile(r"^10\."),
    re.compile(r"^192\.168\."),
    re.compile(r"^172\.(1[6-9]|2\d|3[01])\."),
    re.compile(r"^169\.254\."),
    re.compile(r"^0\."),
    re.compile(r"^\[?::1\]?$"),
    re.compile(r"\.local$", re.I),
]


def parse_issue_body(body: str) -> dict[str, str]:
    """Parse a GitHub Issue Form body into a flat dict.

    Issue Forms render as ``### <label>`` headings followed by the value, so we split on the
    headings rather than trying to parse Markdown. Unknown headings are ignored on purpose:
    the form will evolve and an unrecognised field should not break ingestion.
    """
    fields: dict[str, str] = {}
    parts = re.split(r"^###\s+(.+?)\s*$", body, flags=re.MULTILINE)
    # parts[0] is the preamble; then alternating (heading, value)
    for index in range(1, len(parts) - 1, 2):
        heading = parts[index].strip().lower()
        value = parts[index + 1].strip()
        key = FIELD_MAP.get(heading) or FIELD_MAP.get(parts[index].strip())
        if not key:
            continue
        if value in ("_No response_", "No response", "-"):
            value = ""
        fields[key] = value
    return fields


def validate_public_url(url: str) -> tuple[bool, str]:
    """Reject SSRF-shaped input. Users submit arbitrary strings; we must not fetch them."""
    from urllib.parse import urlparse

    url = (url or "").strip()
    if not url:
        return False, "empty url"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip()
    if not host or "." not in host:
        return False, f"invalid host: {host!r}"
    for pattern in BLOCKED_HOST_PATTERNS:
        if pattern.search(host):
            return False, f"refusing non-public host: {host}"
    return True, url


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def _recipe_key(name: str, host: str, base_url: str, taken: set[str]) -> str:
    """A recipe key: ASCII, stable, and never colliding with an existing site.

    The form asks for a site name in the operator's own language, so the name is usually not
    ASCII at all. Slugifying it therefore yields nothing, and the previous implementation fell
    back to the constant ``"site"`` -- which is how a submission of ``cj.lziapi.com`` named
    "采集接口验证站" was registered under the key ``site``. Every Chinese-named submission would
    have received that same key, so each new one would have overwritten the last, and the error
    would only have shown up as a site mysteriously disappearing.

    So: try the name, then the host, then a hash of the URL, and if the result is already taken
    by a *different* origin, disambiguate. Deterministic throughout, because the key names the
    recipe file and the snapshot directory.
    """
    import hashlib  # noqa: PLC0415 -- only needed here, keeps the module's import list honest

    for candidate in (_slug(name), _slug(host.replace("www.", "")), _slug(host)):
        if candidate:
            key = candidate[:40].strip("-")
            break
    else:
        # No usable ASCII anywhere: the host is non-ASCII too (an IDN). Hash the origin.
        key = "site-" + hashlib.sha1(base_url.encode("utf-8")).hexdigest()[:8]

    unique = key
    suffix = 2
    while unique in taken:
        unique = f"{key[:36]}-{suffix}"
        suffix += 1
    return unique


@app.command("issue-parse")
def issue_parse(
    body_env: str = typer.Option("ISSUE_BODY", "--body-env"),
    number_env: str = typer.Option("ISSUE_NUMBER", "--number-env"),
    github_output: Path | None = typer.Option(None, "--github-output"),
    daily_quota: int = typer.Option(20, "--daily-quota"),
) -> None:
    """Parse a submitted Issue into a site recipe and register it.

    Security note: the issue body is untrusted input and is read from an *environment
    variable*, never interpolated into a shell command. Nothing here executes user input --
    it is only ever treated as data, and the only URL we will subsequently fetch must pass
    :func:`validate_public_url`.
    """
    settings = get_settings()
    body = os.environ.get(body_env, "")
    raw_number = os.environ.get(number_env, "0")
    try:
        issue_number = int(raw_number or 0)
    except ValueError:
        issue_number = 0

    fields = parse_issue_body(body)
    result: dict[str, Any] = {"ok": False, "key": "", "message": ""}

    ok, url_or_error = validate_public_url(fields.get("site_url", ""))
    if not ok:
        result["message"] = (
            f"表单填写不完整或地址无效：{url_or_error}\n\n"
            "请在「站点地址」里填写完整的公网 URL，例如 `https://www.example.com/`。"
        )
        _emit(result, github_output)
        return

    url = url_or_error
    adapter = ADAPTER_MAP.get(fields.get("adapter", "auto").strip().lower(), "auto")

    from urllib.parse import urlparse

    from harvest.probe.detector import normalize_base_url

    base = normalize_base_url(url)
    host = urlparse(base).hostname or ""

    registry = load_registry(settings)

    # Duplicate detection is by *origin*, not by the derived name. Keying it on the key meant the
    # same site resubmitted under a different display name was registered a second time -- and
    # then both entries were harvested and published, because nothing else compares base URLs.
    for entry in registry.sites:
        if normalize_base_url(entry.base_url) == base:
            last = (entry.last_run or {}).get("at", "未知")
            result.update(
                {
                    "ok": True,
                    "key": entry.key,
                    "duplicate": True,
                    "message": (
                        f"该站点已收录（key=`{entry.key}`），上次采集时间：{last}。无需重复提交。"
                    ),
                }
            )
            _emit(result, github_output)
            return

    key = _recipe_key(fields.get("site_name", ""), host, base, {e.key for e in registry.sites})

    today = __import__("datetime").date.today().isoformat()
    today_count = sum(
        1
        for entry in registry.sites
        if str((entry.origin or {}).get("at", "")).startswith(today)
    )
    if today_count >= daily_quota:
        result["message"] = f"今日新站提交已达上限（{daily_quota}），请明天再试。"
        _emit(result, github_output)
        return

    site = SiteConfig(
        key=key,
        name=fields.get("site_name") or host,
        base_url=base,
        adapter=adapter,  # type: ignore[arg-type]
        notes=fields.get("notes") or None,
    )
    if fields.get("categories"):
        site.probe = {"categories": [c.strip() for c in fields["categories"].split(",") if c.strip()]}

    path = save_recipe(site, settings)
    from harvest.models import utcnow_iso

    registry.add_or_update(
        registry_entry_from(
            site,
            origin={
                "type": "issue",
                "issue": issue_number,
                "at": utcnow_iso(),
            },
        )
    )
    save_registry(registry, settings)

    result.update(
        {
            "ok": True,
            "key": key,
            "recipe": str(path),
            "message": f"已受理，站点标识 `{key}`，采集任务已排队。",
        }
    )
    _emit(result, github_output)


def _github_output_value(value: Any) -> str:
    """Render a value the way GitHub's `$GITHUB_OUTPUT` format expects it.

    Two conventions matter, and getting either wrong is silent:

    * Booleans must be lowercase. Python renders ``True``, GitHub expressions compare against
      ``'true'``, and ``'True' != 'true'`` -- so an output written as ``ok=True`` reads as false
      and every downstream ``if:`` is skipped without error. That is how the entire Issue intake
      came to acknowledge a submission and then quietly do nothing with it.
    * Single-line values use ``key=value``; values containing newlines need the delimited
      heredoc form. A multi-line value written as ``key=value`` keeps only the text before the
      first newline, so the half of a rejection message that explains *how to fix it* vanished.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


_GH_OUTPUT_DELIMITER = "ghadelimiter"


def _emit(result: dict[str, Any], github_output: Path | None) -> None:
    if github_output:
        with github_output.open("a", encoding="utf-8") as handle:
            for key, value in result.items():
                if not isinstance(value, (str, int, bool)):
                    continue
                text = _github_output_value(value)
                if "\n" in text or "\r" in text:
                    # The delimiter must not occur inside the value, or the block ends early.
                    delimiter = _GH_OUTPUT_DELIMITER
                    while delimiter in text:
                        delimiter += "_"
                    handle.write(f"{key}<<{delimiter}\n{text}\n{delimiter}\n")
                else:
                    handle.write(f"{key}={text}\n")
    print(json.dumps(result, ensure_ascii=False))


# --------------------------------------------------------------------------------------
# maintenance commands
# --------------------------------------------------------------------------------------


@app.command("purge-cdn")
def purge_cdn(
    paths: str = typer.Option("data/tvbox,data/snapshot", "--paths"),
    repo: str = typer.Option("", "--repo", help="owner/name; defaults to $GITHUB_REPOSITORY"),
    ref: str = typer.Option("main", "--ref"),
    changed_only: bool = typer.Option(True, "--changed-only/--all"),
) -> None:
    """Ask jsDelivr to drop its cached copies of the artefacts we just pushed.

    Without this, branch caching means users keep seeing yesterday's subscription for up to
    12 hours after a successful run -- the pipeline would look broken while working fine.
    """
    import subprocess

    repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        console.print("[yellow]no repo given and $GITHUB_REPOSITORY is unset; skipping[/yellow]")
        return

    targets: list[str] = []
    if changed_only:
        try:
            diff = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~1"], capture_output=True, text=True, check=False
            )
            files = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
        except Exception:
            files = []
        wanted = [p.strip().rstrip("/") + "/" for p in paths.split(",") if p.strip()]
        targets = [f for f in files if any(f.startswith(w) for w in wanted)]
    if not targets:
        # Derived from the same list `generate` writes from, so the two cannot disagree. An
        # earlier version hardcoded these names and drifted twice: it named `index.direct.json`
        # as if it were stale (it is a real artefact), then omitted it after that mistake was
        # half-corrected. Either way a subscriber keeps a cached copy for up to 12 hours.
        targets = tvbox_mod.default_purge_targets()

    async def _purge() -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        async with HttpClient(get_settings()) as http:
            for target in targets[:200]:
                url = f"https://purge.jsdelivr.net/gh/{repo}@{ref}/{target}"
                try:
                    resp = await http.get(url, key="jsdelivr")
                    out.append((target, resp.status_code))
                except Exception:
                    out.append((target, 0))
        return out

    for target, status in asyncio.run(_purge()):
        color = "green" if 200 <= status < 300 else "yellow"
        console.print(f"[{color}]purge {status}[/{color}] {target}")


@app.command()
def prune(keep: int = typer.Option(7, "--keep")) -> None:
    """Trim the raw archive so the repository does not grow without bound."""
    settings = get_settings()
    removed = 0
    for site_dir in sorted(settings.raw_dir.glob("*")):
        if not site_dir.is_dir():
            continue
        files = sorted(site_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            old.unlink()
            removed += 1
    console.print(f"pruned {removed} raw files (kept {keep} per site)")


@app.command()
def summary(
    reports: str = typer.Option("", "--reports", help="Report directory (default: data/reports)"),
    github_output: str = typer.Option("", "--github-output", help="Append summary= to this file"),
    markdown: str = typer.Option("", "--markdown", help="Write the combined report here"),
    comment: str = typer.Option("", "--comment", help="Write the Issue comment body here"),
    subscribe_url: str = typer.Option("", "--subscribe-url", help="Subscription URL for the comment"),
    learn_bc: bool = typer.Option(
        True,
        "--learn-bc/--no-learn-bc",
        help="Promote repeatedly-hit gambling hosts into the learned blacklist",
    ),
) -> None:
    """Combine one run's per-site reports into a single report and summary line.

    The collect job runs one matrix job per site, so a run's outcome is spread across N report
    files. Both the run summary and the Issue comment used to read `data/reports/*.json` inline
    in shell -- and both were consequently empty, because the report files were never uploaded as
    artifacts to begin with. This command makes the combination explicit, and testable.

    This is also where BC host learning belongs in CI. `run_pipeline` learns hosts too, but it
    runs inside each collect job, whose workspace is discarded -- and N parallel jobs writing
    `config/blacklist/bc_domains.learned.txt` would collide when their artifacts merge. Here every
    site's `bc_hits` has already been collected into one place, there is exactly one writer, and
    the commit step that follows stages `config`, so the learning actually survives.
    """
    settings = get_settings()
    reports_dir = Path(reports) if reports else settings.reports_dir
    runs = load_runs(reports_dir)

    if not runs:
        console.print(f"[yellow]no report files found in {reports_dir}[/yellow]")
        if github_output:
            with open(github_output, "a", encoding="utf-8") as handle:
                handle.write("summary=no report produced\nsites_ok=0\n")
        raise typer.Exit(code=0)

    combined = combine(runs)
    data = combined.as_dict()
    totals = data["summary"]

    if learn_bc:
        # Aggregate every site's BC hits and promote the repeat offenders. Summed across sites,
        # because the threshold is about a host being advertised *somewhere* repeatedly, and no
        # single site needs to trip it alone.
        from collections import Counter

        from harvest.clean.gambling import learn_from_hosts

        hits: Counter[str] = Counter()
        for site in combined.sites:
            for host, count in site.bc_hits.items():
                hits[host] += count
        added = learn_from_hosts(hits, threshold=5) if hits else []
        if added:
            console.print(f"[yellow]learned {len(added)} BC host(s): {', '.join(added)}[/yellow]")

    summary_path, md_path = combined_paths(settings, combined.started_at)
    reports_dir.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(combined.to_markdown(), encoding="utf-8")

    line = (
        f"sites {totals['sites_ok']}/{totals['sites_total']}, "
        f"vods {totals['vods']}, lines {totals['lines']}, filtered {totals['filtered']}"
    )
    console.print(f"[bold]{line}[/bold]")

    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"summary={line}\nsites_ok={totals['sites_ok']}\n")

    if markdown:
        Path(markdown).parent.mkdir(parents=True, exist_ok=True)
        Path(markdown).write_text(combined.to_markdown(), encoding="utf-8")

    if comment:
        Path(comment).parent.mkdir(parents=True, exist_ok=True)
        Path(comment).write_text(
            build_run_comment(combined, subscribe_url=subscribe_url), encoding="utf-8"
        )


@app.command()
def doctor(
    fix: bool = typer.Option(False, "--fix", help="Rewrite recipes whose detect block is stale"),
    learn_bc: bool = typer.Option(False, "--learn-bc", help="Promote recurring BC hosts into the blacklist"),
    strict: bool = typer.Option(False, "--strict", help="Treat warnings as failures too"),
) -> None:
    """Health-check the repository: recipes, registry, schemas, blacklists, stale detection.

    Output is split by severity, and the split is a deliberate claim about what a fresh clone
    looks like.

    ``errors`` mean something is actually wrong: a ledger entry with no recipe, a recipe and
    ledger that disagree about a URL, or a blacklist file that has gone missing (a filter that
    silently stops filtering is worse than no filter, so this one blocks).

    ``warnings`` mean "you have not done this yet", which is the normal state of a new clone.
    An unprobed recipe with ``adapter: auto`` is not broken -- ``collect`` probes it on the fly.
    Exiting non-zero for that would make every CI run on a fresh fork red, and a check that is
    always red is a check nobody reads.
    """
    settings = get_settings()
    errors: list[str] = []
    warnings: list[str] = []

    registry = load_registry(settings)
    console.print(f"registry entries: [bold]{len(registry.sites)}[/bold]")

    for entry in registry.sites:
        site = load_recipe(entry.key, settings)
        if site is None:
            errors.append(f"{entry.key}: in registry but has no recipe file")
            continue
        if site.base_url.rstrip("/") != entry.base_url.rstrip("/"):
            errors.append(
                f"{entry.key}: base_url differs between recipe ({site.base_url}) and "
                f"registry ({entry.base_url})"
            )
        if site.enabled != entry.enabled:
            # Not an error: harvesting requires both to agree, so the disable already won. But
            # one of the two files now describes a state that is not in effect, and the ledger is
            # the file people read to find out what is published.
            effective = site.enabled and entry.enabled
            warnings.append(
                f"{entry.key}: enabled disagrees between recipe ({site.enabled}) and registry "
                f"({entry.enabled}); harvesting is therefore {effective}. Update the ledger to "
                f"match the recipe."
            )
        if site.detect.tier is None:
            warnings.append(f"{entry.key}: never probed (collect will probe it on the fly)")
        elif site.detect.stale:
            warnings.append(f"{entry.key}: detection marked stale; re-probe or run --fix")
        if site.adapter == "auto":
            warnings.append(f"{entry.key}: adapter still 'auto' (resolved at collect time)")
        if site.tier() >= 3 and not (site.probe or {}).get("gateway"):
            console.print(
                f"[yellow]{entry.key}: Tier {site.tier()} needs a gateway/drpy bridge to be usable in TVBox[/yellow]"
            )
        if fix and site.detect.stale:
            site.detect.stale = False
            save_recipe(site, settings)

    for name in ("terms.txt", "regex.txt", "bc_terms.txt", "bc_domains.txt"):
        path = settings.config_dir / "blacklist" / name
        if not path.exists():
            errors.append(f"missing blacklist file: {path}")

    if learn_bc:
        from collections import Counter

        from harvest.clean.blacklist import reload_blacklists
        from harvest.clean.gambling import learn_from_hosts

        counts: Counter[str] = Counter()
        for report_path in sorted(settings.reports_dir.glob("*.json")):
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for record in payload if isinstance(payload, list) else [payload]:
                for site in (record or {}).get("sites", []):
                    for host, count in (site.get("bc_hits") or {}).items():
                        counts[host] += int(count)
        added = learn_from_hosts(counts, threshold=5)
        reload_blacklists()
        console.print(f"learned BC hosts: {added or 'none'}")

    for warning in warnings:
        console.print(f"[yellow]warning[/yellow] {warning}")

    if errors:
        console.print("[red]errors:[/red]")
        for error in errors:
            console.print(f"  - {error}")

    if errors or (strict and warnings):
        raise typer.Exit(code=1)
    if warnings:
        console.print(
            f"[green]no errors[/green] ({len(warnings)} warning(s); expected on a fresh clone)"
        )
    else:
        console.print("[green]no problems found[/green]")


@app.command()
def info() -> None:
    """Show resolved paths and versions -- handy when a workflow misbehaves."""
    settings = get_settings()
    console.print(f"root        : {settings.root}")
    console.print(f"sites dir   : {settings.sites_dir}")
    console.print(f"data dir    : {settings.data_dir}")
    console.print(f"config dir  : {settings.config_dir}")
    console.print(f"python      : {sys.version.split()[0]} at {sys.executable}")
    from harvest.adapters import base

    base.load_builtin_adapters()
    console.print(f"adapters    : {', '.join(base.available_adapters())}")
    registry = load_registry(settings)
    console.print(f"sites       : {len(registry.sites)} registered")


@app.command()
def env() -> None:
    """Alias kept for scripts: prints the resolved project root only."""
    print(get_settings().root)


def main() -> None:
    app()


if __name__ == "__main__":
    main()


# --------------------------------------------------------------------------------------
# small helpers used by tests
# --------------------------------------------------------------------------------------


def recipe_exists(key: str) -> bool:
    return recipe_path(key, get_settings()).exists()


def which_python() -> str:
    return sys.executable or shutil.which("python") or "python"


__all__ = ["app", "parse_issue_body", "validate_public_url", "main"]
