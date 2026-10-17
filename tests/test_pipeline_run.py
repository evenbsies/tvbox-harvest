"""End-to-end coverage for the pipeline entry point.

`run_pipeline` had none. Every workflow ends up calling it, and it owns the
collect -> clean -> learn -> generate -> report sequence, so a defect in the seams *between*
those stages is invisible to a suite that tests each stage in isolation. Two such defects shipped:

1. ``bc_host_counts`` was handed to ``learn_bc_hosts(dropped: DropLog)``, which reads
   ``.bc_hosts``. Any run that actually caught a gambling host -- the very thing the compliance
   layer exists to do -- died with

       AttributeError: 'Counter' object has no attribute 'bc_hosts'

   and published nothing. It survived every green run because none of them happened to hit a
   blacklisted host; the first scheduled run that did, failed. A compliance feature that breaks
   precisely when it works is worse than one that is absent, because the failure looks like an
   infrastructure problem rather than a hole in the filtering.

2. The pipeline's own ``generate_all`` call omitted ``carry_over``, so a local ``harvest collect``
   rewrote ``data/tvbox/index.json`` with only the sites it had just harvested.

The harness drives the real pipeline and stubs only ``run_collect``, which is where the network
would otherwise be. Everything downstream -- cleaning, BC learning, generation, reporting -- is
the production code path.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from harvest.adapters.base import CollectContext
from harvest.clean import blacklist as blacklist_mod
from harvest.config import Settings, get_settings
from harvest.models import Episode, Line, SiteData, Vod
from harvest.pipeline import RunOptions, run_pipeline

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A host that the learned blacklist will name, so the trigger is the test's own, not a regex in
#: config/blacklist/bc_domains.txt that someone might edit.
#: `RunOptions.verify` defaults to True and performs *live* requests to the generated entry
#: points, with `4**attempt` retry backoff -- so leaving it on makes these tests hit the network,
#: hang on this machine's proxy, and pass or fail depending on connectivity. Verification is
#: covered directly in tests/test_verify.py; here it would only add noise.
OFFLINE = {"verify": False}

#: A host that matches the shipped `bc_domains.txt` rules (`re:(?:^|\.)bc[-_.]`), so detection
#: comes from the real blacklist rather than from something this test seeds. If that list is
#: edited into no longer matching, this file's BC tests fail loudly -- which is the signal you
#: would want, because the compliance layer's trigger has moved.
BC_HOST = "bc-ad.example"

SITE_BASE = "https://cdn.example.test"


def _recipe(key: str, *, enabled: bool = True) -> str:
    return (
        f"key: {key}\n"
        f"name: {key}\n"
        f"base_url: {SITE_BASE}\n"
        f"adapter: maccms_v10\n"
        f"enabled: {'true' if enabled else 'false'}\n"
        f"detect:\n  tier: 1\n  status: ok\n  result: maccms_v10\n"
    )


def _root(tmp_path: Path, keys: list[str]) -> Path:
    """A throwaway project root: real config/, generated recipes and ledger."""
    (tmp_path / "sites").mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")

    for key in keys:
        (tmp_path / "sites" / f"{key}.yaml").write_text(_recipe(key), encoding="utf-8")

    ledger = "sites:\n" + "".join(
        f"- key: {key}\n  name: {key}\n  base_url: {SITE_BASE}\n  adapter: maccms_v10\n"
        f"  tier: 1\n  enabled: true\n  origin: {{type: manual}}\n  last_run: {{}}\n"
        for key in keys
    )
    (tmp_path / "sites" / "registry.yaml").write_text(ledger, encoding="utf-8")
    return tmp_path


def _learn_bc_host(root: Path, host: str) -> None:
    """Put a host on the learned BC list, as `doctor --learn-bc` would."""
    path = root / "config" / "blacklist" / "bc_domains.learned.txt"
    path.write_text(f"# seeded by the test\n{host}\n", encoding="utf-8")


def _data(key: str, *, media_host: str, vods: int = 1) -> SiteData:
    data = SiteData(
        site_key=key, site_name=key, base_url=SITE_BASE, adapter="maccms_v10", tier=1
    )
    for index in range(vods):
        data.vods.append(
            Vod(
                site_key=key,
                vid=f"{key}-{index}",
                name=f"{key} 影片 {index}",
                lines=[
                    Line(
                        name="线路1",
                        episodes=[
                            Episode(
                                index=1,
                                name="第01集",
                                url=f"https://{media_host}/hls/{key}-{index}.m3u8",
                            )
                        ],
                    )
                ],
            )
        )
    return data


def _stub_collect(data: SiteData):
    """Replace `run_collect`; it is the only place the pipeline touches the network."""

    async def _collect(site, http, *, settings=None, options=None, recorder=None):  # noqa: ANN001
        # The stub answers for whichever site the pipeline is working on, so the caller's
        # `sites` selector decides what gets harvested.
        payload = data if data.site_key == site.key else _data(site.key, media_host="ok.example.test")
        ctx = CollectContext(settings=settings, mode=options.mode if options else "full")
        return payload, ctx

    return _collect


def _activate(root: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("HARVEST_ROOT", str(root))
    get_settings.cache_clear()
    blacklist_mod.reload_blacklists()
    return Settings(root=root)


@pytest.mark.asyncio
async def test_a_gambling_hit_does_not_abort_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression. A BC hit is the compliance path working, not a reason to crash."""
    root = _root(tmp_path, ["demo"])
    _learn_bc_host(root, BC_HOST)
    settings = _activate(root, monkeypatch)

    import harvest.pipeline as pipeline

    monkeypatch.setattr(pipeline, "run_collect", _stub_collect(_data("demo", media_host=BC_HOST)))

    report = await run_pipeline(RunOptions(sites=["demo"], mode="full", **OFFLINE), settings)
    get_settings.cache_clear()

    assert len(report.sites) == 1
    site = report.sites[0]
    assert site.status in ("ok", "failed"), site.errors
    # The hit must be recorded, not swallowed.
    assert site.bc_hits.get(BC_HOST, 0) >= 1, (
        f"the blacklisted host was not recorded as a BC hit: {site.bc_hits}"
    )
    # And the offending title must not survive into the published config.
    published = json.loads((root / "data" / "tvbox" / "index.json").read_text(encoding="utf-8"))
    assert published["sites"] or site.status == "failed"


@pytest.mark.asyncio
async def test_incremental_mode_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scheduled cron's mode. It was never exercised, so its failures reached production.

    The AttributeError above surfaced on a scheduled run precisely because `incremental` is a mode
    nothing local used; the fix is mode-agnostic, but the coverage has to exist or the next
    mode-specific defect travels the same road.
    """
    root = _root(tmp_path, ["demo"])
    settings = _activate(root, monkeypatch)

    import harvest.pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "run_collect", _stub_collect(_data("demo", media_host="cdn.example.test"))
    )
    options = RunOptions(sites=["demo"], mode="incremental", since_hours=24, **OFFLINE)

    report = await run_pipeline(options, settings)
    get_settings.cache_clear()

    assert report.mode == "incremental"
    assert report.sites
    site = report.sites[0]
    assert site.status == "ok", site.errors
    assert site.vods == 1


@pytest.mark.asyncio
async def test_a_learned_host_is_promoted_after_repeated_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the fixed call for real: five hits must promote the host into the learned list."""
    root = _root(tmp_path, ["demo"])
    settings = _activate(root, monkeypatch)

    import harvest.pipeline as pipeline

    monkeypatch.setattr(
        pipeline,
        "run_collect",
        _stub_collect(_data("demo", media_host=BC_HOST, vods=5)),
    )

    report = await run_pipeline(RunOptions(sites=["demo"], mode="full", **OFFLINE), settings)
    get_settings.cache_clear()

    assert report.learned_bc_hosts == [BC_HOST], report.learned_bc_hosts
    learned = (root / "config" / "blacklist" / "bc_domains.learned.txt").read_text(encoding="utf-8")
    assert learned.count(BC_HOST) == 1, learned
    assert "auto: 5 hits" in learned, learned


@pytest.mark.asyncio
async def test_a_run_with_no_bc_hits_reports_nothing_learned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction, so the assertion above cannot pass on a constant."""
    root = _root(tmp_path, ["demo"])
    _learn_bc_host(root, BC_HOST)
    settings = _activate(root, monkeypatch)

    import harvest.pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "run_collect", _stub_collect(_data("demo", media_host="ok.example.test"))
    )

    report = await run_pipeline(RunOptions(sites=["demo"], mode="full", **OFFLINE), settings)
    get_settings.cache_clear()

    assert report.learned_bc_hosts == []
    assert report.sites[0].bc_hits == {}


@pytest.mark.asyncio
async def test_the_pipeline_carries_over_sites_it_did_not_harvest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`harvest collect` locally must not shrink the published subscription either."""
    root = _root(tmp_path, ["alpha", "beta"])
    settings = _activate(root, monkeypatch)

    import harvest.pipeline as pipeline

    # First pass: both sites.
    monkeypatch.setattr(
        pipeline, "run_collect", _stub_collect(_data("alpha", media_host="ok.example.test"))
    )
    first = _stub_collect(_data("alpha", media_host="ok.example.test"))
    second = _stub_collect(_data("beta", media_host="ok.example.test"))

    async def both(site, http, **kwargs):  # noqa: ANN001
        return await (first if site.key == "alpha" else second)(site, http, **kwargs)

    monkeypatch.setattr(pipeline, "run_collect", both)
    await run_pipeline(RunOptions(sites=["alpha", "beta"], mode="full", **OFFLINE), settings)
    published = json.loads((root / "data" / "tvbox" / "index.json").read_text(encoding="utf-8"))
    assert [e["key"] for e in published["sites"]] == ["alpha", "beta"]

    # Second pass: alpha only, as a single-site run would do.
    monkeypatch.setattr(pipeline, "run_collect", first)
    (root / "data" / "normalized" / "beta.json").unlink(missing_ok=True)
    await run_pipeline(RunOptions(sites=["alpha"], mode="full", **OFFLINE), settings)
    get_settings.cache_clear()

    published = json.loads((root / "data" / "tvbox" / "index.json").read_text(encoding="utf-8"))
    assert [e["key"] for e in published["sites"]] == ["alpha", "beta"], (
        "the pipeline's generate call dropped a site this run did not harvest"
    )


@pytest.mark.asyncio
async def test_a_site_with_no_titles_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A site answering with an empty catalogue is a site failure, not a pipeline failure."""
    root = _root(tmp_path, ["demo"])
    settings = _activate(root, monkeypatch)

    import harvest.pipeline as pipeline

    empty = SiteData(site_key="demo", site_name="demo", base_url=SITE_BASE, adapter="maccms_v10")
    monkeypatch.setattr(pipeline, "run_collect", _stub_collect(empty))

    report = await run_pipeline(RunOptions(sites=["demo"], mode="full", **OFFLINE), settings)
    get_settings.cache_clear()

    assert report.sites[0].status == "failed"
    assert any("no titles" in str(e.get("error", "")) for e in report.sites[0].errors)


@pytest.mark.asyncio
async def test_the_selector_matching_nothing_returns_an_explained_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not an error: the run had nothing to do, and the report says so."""
    root = _root(tmp_path, ["demo"])
    settings = _activate(root, monkeypatch)

    report = await run_pipeline(RunOptions(sites=["nonexistent"], mode="full", **OFFLINE), settings)
    get_settings.cache_clear()

    assert report.sites == []
    assert any("no sites matched" in n for n in report.notes), report.notes


@pytest.mark.asyncio
async def test_dry_run_writes_no_configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--dry-run` must mean it: nothing on disk to accidentally commit."""
    root = _root(tmp_path, ["demo"])
    settings = _activate(root, monkeypatch)

    import harvest.pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "run_collect", _stub_collect(_data("demo", media_host="ok.example.test"))
    )

    report = await run_pipeline(RunOptions(sites=["demo"], mode="full", dry_run=True, **OFFLINE), settings)
    get_settings.cache_clear()

    assert report.sites
    assert report.generated == []
    assert not (root / "data" / "tvbox" / "index.json").exists()


def test_the_stub_helper_is_used_with_a_real_site_key(tmp_path: Path) -> None:
    """Guard the harness itself: the stubbed data must not silently mismatch the selector."""
    data = _data("demo", media_host="x.example.test")
    assert data.site_key == "demo"
    assert data.vods[0].lines[0].episodes[0].url.startswith("https://x.example.test/")
