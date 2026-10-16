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
from harvest.generate.verify import verify_config_file
from harvest.models import SiteConfig
from harvest.pipeline import (
    RunOptions,
    load_recipe,
    load_registry,
    recipe_path,
    registry_entry_from,
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
) -> None:
    """Rebuild TVBox configs from existing normalized data, without re-harvesting."""
    settings = get_settings()
    pairs: list[tuple[SiteConfig, Any]] = []
    for path in sorted(settings.normalized_dir.glob("*.json")):
        recipe = load_recipe(path.stem, settings)
        if recipe is None:
            continue
        from harvest.models import SiteData

        data = SiteData.model_validate(json.loads(path.read_text(encoding="utf-8")))
        pairs.append((recipe, data))

    if not pairs:
        console.print("[red]no normalized data found; run `harvest collect` first[/red]")
        raise typer.Exit(code=1)

    generated = tvbox_mod.generate_all(
        pairs,
        settings=settings,
        gateway_base=gateway,
        flavors=tuple(f.strip() for f in flavors.split(",") if f.strip()),
        run_id=run_id,
    )
    for item in generated:
        console.print(f"[green]{item.flavor}[/green] -> {item.path} ({item.site_count} sites)")


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
    configs = __import__("harvest.pipeline", fromlist=["resolve_sites"]).resolve_sites(options, settings)

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

    from harvest.probe.detector import normalize_base_url

    base = normalize_base_url(url)
    from urllib.parse import urlparse

    host = urlparse(base).hostname or ""
    key = re.sub(r"[^a-z0-9]+", "-", (fields.get("site_name") or host.replace("www.", "")).lower()).strip("-")
    key = key or "site"
    key = key[:40]

    registry = load_registry(settings)
    existing = registry.find(key)
    if existing is not None:
        last = (existing.last_run or {}).get("at", "未知")
        result.update(
            {
                "ok": True,
                "key": existing.key,
                "duplicate": True,
                "message": f"该站点已收录（key=`{existing.key}`），上次采集时间：{last}。无需重复提交。",
            }
        )
        _emit(result, github_output)
        return

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


def _emit(result: dict[str, Any], github_output: Path | None) -> None:
    text = json.dumps(result, ensure_ascii=False)
    if github_output:
        with github_output.open("a", encoding="utf-8") as handle:
            for key, value in result.items():
                if isinstance(value, (str, int, bool)):
                    handle.write(f"{key}={value}\n")
    print(text)


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
        targets = ["data/tvbox/index.json", "data/tvbox/index.gateway.json",
                   "data/tvbox/index.experimental.json", "data/tvbox/index.direct.json"]

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
def doctor(
    fix: bool = typer.Option(False, "--fix", help="Rewrite recipes whose detect block is stale"),
    learn_bc: bool = typer.Option(False, "--learn-bc", help="Promote recurring BC hosts into the blacklist"),
) -> None:
    """Health-check the repository: recipes, registry, schemas, blacklists, stale detection."""
    settings = get_settings()
    problems: list[str] = []

    registry = load_registry(settings)
    console.print(f"registry entries: [bold]{len(registry.sites)}[/bold]")

    for entry in registry.sites:
        site = load_recipe(entry.key, settings)
        if site is None:
            problems.append(f"{entry.key}: in registry but has no recipe file")
            continue
        if site.base_url.rstrip("/") != entry.base_url.rstrip("/"):
            problems.append(f"{entry.key}: base_url differs between recipe and registry")
        if site.detect.tier is None:
            problems.append(f"{entry.key}: never probed")
        elif site.detect.stale:
            problems.append(f"{entry.key}: detection marked stale")
        if site.adapter == "auto":
            problems.append(f"{entry.key}: adapter still 'auto'")
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
            problems.append(f"missing blacklist file: {path}")

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

    if problems:
        console.print("[red]problems found:[/red]")
        for problem in problems:
            console.print(f"  - {problem}")
        raise typer.Exit(code=1)
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
