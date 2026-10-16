"""Pipeline orchestration: probe -> collect -> clean -> snapshot -> generate -> verify.

The CLI and the GitHub Actions workflows both call into here, so there is exactly one
definition of "a harvest run". Keeping that in Python rather than in shell/YAML means the
same logic can be run locally for debugging, which matters a lot when a GitHub-hosted run
fails on a site you cannot easily reproduce.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from harvest.adapters import base
from harvest.adapters.base import CollectContext
from harvest.clean.dedupe import clean_site_data, learn_bc_hosts
from harvest.config import Settings, get_settings
from harvest.fetch.http import CircuitOpen, HttpClient, RawRecorder
from harvest.generate import snapshot as snapshot_mod
from harvest.generate import tvbox as tvbox_mod
from harvest.generate.report import RunReport, SiteReport, write_report
from harvest.generate.verify import VerifyResult, verify_entry
from harvest.models import ProbeResult, Registry, RegistryEntry, SiteConfig, SiteData, utcnow_iso
from harvest.probe.detector import ProbeContext, detect


@dataclass
class RunOptions:
    mode: str = "full"  # probe | collect | full
    sites: list[str] = field(default_factory=list)
    tiers: list[int] = field(default_factory=list)
    since_hours: int | None = None
    max_titles: int | None = None
    max_requests: int | None = None
    gateway_base: str = ""
    flavors: tuple[str, ...] = tvbox_mod.FLAVORS
    run_id: str = ""
    reprobe: bool = False
    allow_browser: bool = True
    dry_run: bool = False
    verify: bool = True
    time_budget_seconds: int = 2700
    # Fixture recording is opt-in. Doing it automatically made the regression suite
    # non-deterministic (each run overwrote the curated baseline with whatever the live site
    # happened to return) and risked committing full copies of third-party pages.
    record_fixtures: bool = False
    repo_root: Path | None = None


# --------------------------------------------------------------------------------------
# Recipe / registry IO
# --------------------------------------------------------------------------------------


def recipe_path(key: str, settings: Settings) -> Path:
    return settings.sites_dir / f"{key}.yaml"


def load_recipe(key: str, settings: Settings | None = None) -> SiteConfig | None:
    settings = settings or get_settings()
    path = recipe_path(key, settings)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("key", key)
    return SiteConfig.model_validate(data)


def save_recipe(site: SiteConfig, settings: Settings | None = None) -> Path:
    """Persist a recipe, preserving everything the human wrote.

    The recipe file is this project's central human-maintained asset: it is where a site's
    reconciliation (why it is Tier 4, which API path, which failover hosts) is recorded for the
    next person. A naive ``dump(model_dump())`` round-trip destroys that -- it strips every
    comment and reflows the document, which is exactly what happened the first time the probe
    wrote back a detection result.

    So the write-back is deliberately narrow:

    * Only the keys the *machine* owns are merged (see ``MACHINE_OWNED_KEYS``). Everything else
      -- comments, ordering, hand-tuned headers, notes -- is left byte-for-byte intact.
    * A round-trip parser that understands comments is used, so even the machine-owned blocks
      keep any annotations a human added to them.

    A recipe that does not exist yet is written in full, since there is nothing to preserve.
    """
    from ruamel.yaml import YAML

    settings = settings or get_settings()
    settings.sites_dir.mkdir(parents=True, exist_ok=True)
    path = recipe_path(site.key, settings)

    rt = YAML()
    rt.preserve_quotes = True
    rt.width = 100
    rt.indent(mapping=2, sequence=4, offset=2)

    payload = site.model_dump(exclude_none=True)

    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as handle:
                doc = rt.load(handle) or {}
        except Exception:
            # Unparseable recipe: do not silently discard it, but do not crash the run either.
            # Writing a side-by-side copy keeps the human's version recoverable.
            path.with_suffix(".yaml.broken").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            doc = {}

        for key in MACHINE_OWNED_KEYS:
            if key not in payload:
                continue
            value = payload[key]
            if isinstance(value, dict):
                _deep_merge(doc, key, value)
            else:
                doc[key] = value
    else:
        doc = payload
        doc["_generated_by"] = "harvest"

    with path.open("w", encoding="utf-8", newline="\n") as handle:
        rt.dump(doc, handle)
    return path


# Keys the pipeline is allowed to rewrite. Everything else in a recipe belongs to the human and
# must survive untouched -- this is what keeps the file readable as documentation of *why* a
# site is configured the way it is.
MACHINE_OWNED_KEYS = ("adapter", "detect", "maccms", "browser", "selectors", "probe")


def _deep_merge(doc: dict, key: str, values: dict) -> None:
    """Merge ``values`` into ``doc[key]`` in place, keeping comments on untouched leaves."""
    node = doc.get(key)
    if node is None:
        doc[key] = dict(values)
        return
    if not isinstance(node, dict):
        doc[key] = dict(values)
        return
    for sub_key, sub_value in values.items():
        if isinstance(sub_value, dict) and isinstance(node.get(sub_key), dict):
            _deep_merge(node, sub_key, sub_value)
        else:
            node[sub_key] = sub_value


def load_registry(settings: Settings | None = None) -> Registry:
    settings = settings or get_settings()
    if not settings.registry_path.exists():
        return Registry()
    data = yaml.safe_load(settings.registry_path.read_text(encoding="utf-8")) or {}
    return Registry.model_validate(data)


def save_registry(registry: Registry, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    settings.sites_dir.mkdir(parents=True, exist_ok=True)
    payload = {"sites": [s.model_dump(exclude_none=True) for s in registry.sites]}
    header = "# Site ledger. Appended by the Issue workflow; `last_run` is refreshed each run.\n"
    settings.registry_path.write_text(
        header + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return settings.registry_path


def resolve_sites(options: RunOptions, settings: Settings) -> list[SiteConfig]:
    """Turn CLI/workflow selectors into concrete recipes."""
    registry = load_registry(settings)
    wanted = {s.strip() for s in options.sites if s.strip()}

    keys: list[str] = []
    for entry in registry.sites:
        if wanted and entry.key not in wanted:
            continue
        if wanted and not entry.enabled:
            # An explicitly named site is honoured even when disabled -- that is how a
            # quarantined or needs-mapping site gets retried by hand.
            keys.append(entry.key)
            continue
        if not wanted and not entry.enabled:
            continue
        keys.append(entry.key)

    for key in sorted(wanted - set(keys)):
        keys.append(key)

    configs: list[SiteConfig] = []
    for key in keys:
        site = load_recipe(key, settings)
        if site is None:
            registry_entry = registry.find(key)
            if registry_entry is None:
                continue
            site = SiteConfig(
                key=registry_entry.key,
                name=registry_entry.name,
                base_url=registry_entry.base_url,
                adapter=registry_entry.adapter,
            )
        if options.tiers and site.tier() not in options.tiers:
            continue
        configs.append(site)
    return configs


# --------------------------------------------------------------------------------------
# Individual stages
# --------------------------------------------------------------------------------------


async def run_probe(
    site: SiteConfig,
    http: HttpClient,
    *,
    settings: Settings,
    recorder: RawRecorder | None = None,
    allow_browser: bool = True,
    force_tier: int | None = None,
) -> ProbeResult:
    ctx = ProbeContext(
        settings=settings, http=http, recorder=recorder, allow_browser=allow_browser, force_tier=force_tier
    )
    result = await detect(site, ctx)
    return result


def apply_probe_patch(site: SiteConfig, result: ProbeResult) -> SiteConfig:
    """Fold a probe result back into the recipe.

    ``maccms`` and ``browser`` are typed models, so a patch has to be merged *through* the model
    rather than assigned over it. Assigning a raw dict would leave the field holding a plain
    dict, which then serialises wrong and silently loses the other fields' defaults.
    """
    patch = dict(result.patch or {})
    if result.adapter and result.adapter != "auto":
        site.adapter = result.adapter  # type: ignore[assignment]

    def merge_model(field_name: str, extra: dict) -> None:
        current = getattr(site, field_name, None)
        if hasattr(current, "model_dump"):
            merged = {**current.model_dump(exclude_none=True), **extra}
            setattr(site, field_name, type(current).model_validate(merged))
        elif isinstance(current, dict):
            current.update(extra)
        else:
            setattr(site, field_name, extra)

    for key in ("maccms", "browser"):
        if isinstance(patch.get(key), dict):
            merge_model(key, patch[key])

    if isinstance(patch.get("selectors"), dict):
        selectors = dict(site.selectors or {})
        for rule_key, rule_value in patch["selectors"].items():
            if isinstance(rule_value, dict) and isinstance(selectors.get(rule_key), dict):
                selectors[rule_key] = {**selectors[rule_key], **rule_value}
            else:
                selectors[rule_key] = rule_value
        site.selectors = selectors

    probe_meta = dict(site.probe or {})
    if isinstance(patch.get("api"), str):
        probe_meta["api"] = patch["api"]
    if patch.get("field_candidates"):
        probe_meta["field_candidates"] = patch["field_candidates"]
    if probe_meta:
        site.probe = probe_meta

    site.detect = site.detect.model_validate(
        {
            **site.detect.model_dump(),
            "version": 1,
            "checked_at": utcnow_iso(),
            "result": str(result.adapter),
            "tier": result.tier,
            "status": result.status,
            "stale": False,
            "evidence": result.evidence,
        }
    )
    return site


async def run_collect(
    site: SiteConfig,
    http: HttpClient,
    *,
    settings: Settings,
    options: RunOptions,
    recorder: RawRecorder | None = None,
) -> tuple[SiteData, Any]:
    base.load_builtin_adapters()
    adapter = base.instantiate(site)
    ctx = CollectContext(
        settings=settings,
        http=http,
        recorder=recorder,
        mode=options.mode if options.mode != "probe" else "full",
        since_hours=options.since_hours,
        max_titles=options.max_titles,
        max_requests=options.max_requests,
        deadline=time.monotonic() + max(60, options.time_budget_seconds),
    )
    data = await adapter.collect(site, ctx)
    return data, ctx


# --------------------------------------------------------------------------------------
# Full run
# --------------------------------------------------------------------------------------


async def run_pipeline(options: RunOptions, settings: Settings | None = None) -> RunReport:
    settings = settings or get_settings()
    settings.ensure_dirs()
    report = RunReport(mode=options.mode, run_id=options.run_id)
    report.started_at = utcnow_iso()

    sites = resolve_sites(options, settings)
    if not sites:
        report.notes.append("no sites matched the selector")
        report.finished_at = utcnow_iso()
        return report

    registry = load_registry(settings)
    pairs: list[tuple[SiteConfig, SiteData | None]] = []
    bc_host_counts: dict[str, int] = {}

    recorder = RawRecorder()
    async with HttpClient(settings, recorder=recorder) as http:
        for site in sites:
            site_report = report.add_site(
                SiteReport(key=site.key, name=site.name, adapter=str(site.adapter), tier=site.tier())
            )
            started = time.monotonic()
            data: SiteData | None = None

            try:
                # --- probe ---------------------------------------------------------
                # A recipe that names its adapter is an explicit human decision and is trusted;
                # re-probing it would spend requests (and, for Tier 4, a browser launch) to
                # rediscover something already written down. `--reprobe` forces it anyway.
                needs_probe = options.reprobe or site.adapter == "auto"
                if not needs_probe and site.detect.tier is None:
                    # Adapter pinned but never probed: adopt it, and say so rather than
                    # pretending detection ran.
                    site_report.tier = site.tier()
                    site_report.adapter = str(site.adapter)
                    site_report.notes.append(f"adapter pinned by recipe: {site.adapter} (T{site.tier()})")

                if needs_probe:
                    probe = await run_probe(
                        site, http, settings=settings, recorder=recorder,
                        allow_browser=options.allow_browser,
                    )
                    site = apply_probe_patch(site, probe)
                    site_report.tier = probe.tier
                    site_report.adapter = str(probe.adapter)
                    site_report.notes.append(f"probe: {probe.status} tier={probe.tier}")
                    if not probe.automatable:
                        site_report.status = "failed"
                        site_report.errors.append(
                            {"where": "probe", "error": f"not automatable (tier={probe.tier})"}
                        )
                        save_recipe(site, settings)
                        continue
                    if options.mode == "probe":
                        site_report.status = "ok"
                        save_recipe(site, settings)
                        if options.record_fixtures and recorder.entries:
                            _record_fixtures(recorder, settings, site.key)
                            recorder.entries.clear()
                        continue

                # --- budget guard ---------------------------------------------------
                # `--no-browser` must actually mean "no browser", including in the collect
                # stage. Silently launching one anyway would make the flag a lie.
                if not options.allow_browser and site.tier() >= 4:
                    site_report.status = "skipped"
                    site_report.notes.append(
                        "Tier 4 requires a browser; skipped because --no-browser was given. "
                        "Remove the flag, or rely on the last-good snapshot already in data/."
                    )
                    continue

                # --- collect -------------------------------------------------------
                data, ctx = await run_collect(
                    site, http, settings=settings, options=options, recorder=recorder
                )
                site_report.tier = data.tier
                site_report.adapter = data.adapter
                site_report.errors.extend(ctx.errors)

                if not data.vods:
                    site_report.status = "failed"
                    site_report.errors.append({"where": "collect", "error": "no titles harvested"})
                    pairs.append((site, None))
                    save_recipe(site, settings)
                    continue

                # --- clean ---------------------------------------------------------
                page_html = ctx.page_html_sample or ""
                cleaned = clean_site_data(
                    data,
                    page_html=page_html,
                    bc_filter=site.compliance.bc_filter,
                    adult_filter=site.compliance.policy != "allow",
                    quarantine_bc_site=site.compliance.bc_filter and site.compliance.policy != "allow",
                )
                data = cleaned.site_data
                site_report.vods = data.vod_count
                site_report.lines = data.line_count
                site_report.episodes = data.episode_count
                site_report.filtered = dict(cleaned.dropped.counts)
                site_report.bc_hits = dict(cleaned.dropped.bc_hosts)
                site_report.unknown_categories = dict(cleaned.unknown_categories)
                for host, count in cleaned.dropped.bc_hosts.items():
                    bc_host_counts[host] = bc_host_counts.get(host, 0) + count

                # Surface conditions that are *answers from the source*, not our failures.
                # Burying them would make a deliberately-gated site look like a broken harvest.
                if data.stats.get("paywalled_episodes"):
                    site_report.notes.append(
                        f"paywalled episodes: {data.stats['paywalled_episodes']} of "
                        f"{data.stats.get('episodes_walked', '?')} walked "
                        f"(source requires login/membership; not bypassed by design)"
                    )
                if data.stats.get("titles_truncated"):
                    site_report.notes.append(
                        f"episode budget reached; {data.stats['titles_truncated']} title(s) "
                        f"partially walked"
                    )
                if data.stats.get("stopped_early"):
                    site_report.notes.append(
                        f"stopped early on {len(data.stats['stopped_early'])} title(s) after a refusal"
                    )
                if data.stats.get("empty_titles"):
                    site_report.notes.append(
                        f"{data.stats['empty_titles']} title(s) yielded no playable address"
                    )
                # Tier-3 strategy is a cost disclosure, not a detail: the two paths differ by one
                # request per episode, so which one a site needs belongs in the report.
                if data.stats.get("episode_strategy") == "play_pages":
                    site_report.notes.append(
                        f"episodes read from intermediate play pages: "
                        f"{data.stats.get('play_pages_fetched', 0)} extra request(s) "
                        f"(one per episode)"
                    )
                if data.stats.get("play_page_truncated"):
                    site_report.notes.append(
                        f"play-page walk capped on {len(data.stats['play_page_truncated'])} "
                        f"title(s); raise selectors.detail.max_play_pages for the full episode list"
                    )
                if data.stats.get("titles_without_episodes"):
                    site_report.notes.append(
                        f"{data.stats['titles_without_episodes']} detail page(s) yielded no episode "
                        f"(unrecognised template or genuinely empty)"
                    )

                # Tier-4 per-title cost. Recorded structurally, not just as a note, because the
                # JSON report is what a batch-size decision reads.
                per_title = list(data.stats.get("per_title_ms") or [])
                if per_title:
                    site_report.per_title = per_title
                    avg_ms = int(sum(int(r.get("ms") or 0) for r in per_title) / len(per_title))
                    slowest = max(per_title, key=lambda r: int(r.get("ms") or 0))
                    site_report.notes.append(
                        f"per-title cost: avg {avg_ms}ms, slowest {int(slowest.get('ms') or 0)}ms "
                        f"(`{slowest.get('title')}`)"
                    )

                if not data.vods:
                    site_report.status = "quarantined" if cleaned.bc_site_scan.get("quarantined") else "failed"
                    site_report.errors.append(
                        {"where": "clean", "error": "all titles removed by filters"}
                    )
                    pairs.append((site, None))
                else:
                    if cleaned.bc_site_scan.get("quarantined"):
                        site_report.status = "quarantined"
                        site_report.notes.append("site-level gambling signals exceeded threshold")
                    else:
                        site_report.status = "ok" if not ctx.errors else "degraded"
                    pairs.append((site, data))
                    if not options.dry_run:
                        snapshot_mod.write_snapshot(site, data, settings=settings)
                        _write_normalized(site, data, settings)

                save_recipe(site, settings)

            except CircuitOpen as exc:
                site_report.status = "degraded"
                site_report.errors.append({"where": "circuit", "error": str(exc)})
                report.notes.append(
                    f"{site.key}: circuit breaker open; previous artefacts left untouched"
                )
            except Exception as exc:  # keep one bad site from killing the run
                site_report.status = "failed"
                site_report.errors.append({"where": "pipeline", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                site_report.elapsed_ms = int((time.monotonic() - started) * 1000)
                entry = registry.find(site.key)
                if entry is not None:
                    entry.adapter = site.adapter
                    entry.tier = site.tier()
                    entry.last_run = {
                        "at": utcnow_iso(),
                        "status": site_report.status,
                        "vods": site_report.vods,
                        "lines": site_report.lines,
                        "filtered": site_report.filtered_total,
                    }

        # --- learn BC hosts, then generate -------------------------------------
        if bc_host_counts and not options.dry_run:
            from collections import Counter

            report.learned_bc_hosts = learn_bc_hosts(Counter(bc_host_counts), threshold=5)

        if not options.dry_run:
            generated = tvbox_mod.generate_all(
                pairs,
                settings=settings,
                gateway_base=options.gateway_base,
                flavors=tuple(options.flavors),
                run_id=options.run_id,
            )
            for item in generated:
                report.generated.append(
                    {
                        "flavor": item.flavor,
                        "path": str(item.path.relative_to(settings.root))
                        if settings.root in item.path.parents
                        else str(item.path),
                        "sites": item.site_count,
                        "skipped": item.skipped,
                    }
                )

            # --- verify --------------------------------------------------------
            if options.verify:
                index_path = settings.tvbox_dir / "index.json"
                if index_path.exists():
                    import json

                    config = json.loads(index_path.read_text(encoding="utf-8"))
                    data_by_key = {s.key: d for s, d in pairs if d is not None}
                    site_by_key = {s.key: s for s, _ in pairs}
                    for entry in config.get("sites") or []:
                        key = str(entry.get("key") or "")
                        result: VerifyResult = await verify_entry(
                            entry,
                            site=site_by_key.get(key),
                            site_data=data_by_key.get(key),
                            http=http,
                            settings=settings,
                        )
                        report.verification.append(result.as_dict())
                        if not result.passed:
                            report.notes.append(
                                f"{key}: verification {result.status} -- "
                                f"{'; '.join(result.messages[:2]) or 'no detail'}"
                            )

        save_registry(registry, settings)
        if options.record_fixtures and recorder.entries and len(sites) == 1:
            _record_fixtures(recorder, settings, sites[0].key)

    report.finished_at = utcnow_iso()
    if not options.dry_run:
        write_report(report, settings)
    return report


def _write_normalized(site: SiteConfig, data: SiteData, settings: Settings) -> Path:
    path = settings.normalized_dir / f"{site.key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data.model_dump(exclude_none=True), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _record_fixtures(recorder: RawRecorder, settings: Settings, key: str) -> list[str]:
    """Persist recorded responses as regression fixtures -- opt-in only.

    Recording is deliberately not automatic. Two reasons, both learned the hard way:

    * **Determinism.** The suite asserts on this data. If every run overwrote the baseline with
      whatever the live site returned that day, a test failure would tell you nothing.
    * **Footprint.** A real detail page is ~100 KB and a homepage ~700 KB of third-party
      content. Committing full copies would bloat the repository and republish someone else's
      page. Fixtures are meant to be *curated down* to the signals under test.

    So this writes to ``tests/fixtures/<key>/`` for a human to trim and review, rather than
    replacing the curated set silently.
    """
    settings.fixtures_dir.mkdir(parents=True, exist_ok=True)
    return recorder.save_fixtures(settings.fixtures_dir, key)


def registry_entry_from(site: SiteConfig, origin: dict[str, Any] | None = None) -> RegistryEntry:
    return RegistryEntry(
        key=site.key,
        name=site.name,
        base_url=site.base_url,
        adapter=site.adapter,
        tier=site.tier(),
        enabled=site.enabled,
        origin=origin or {},
    )
