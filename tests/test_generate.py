"""Generator, snapshot, TVBox-format and Issue-intake tests.

Includes the security-relevant tests for Issue body handling: a user-submitted URL must never
be able to point the harvester at a private address, and the body must never be treated as
anything but data.
"""

from __future__ import annotations

import json

import jsonschema
import pytest

from harvest.cli import parse_issue_body, validate_public_url
from harvest.generate import tvbox as tvbox_mod
from harvest.generate.snapshot import load_snapshot, vod_to_detail_record, write_snapshot
from harvest.models import AltURL, Episode, Line, SiteConfig, SiteData, Vod

SCHEMA = json.loads(
    (__import__("pathlib").Path(__file__).resolve().parents[1] / "config" / "schema" / "tvbox.schema.json").read_text(
        encoding="utf-8"
    )
)


def demo_site(adapter: str = "maccms_v10", key: str = "demo") -> SiteConfig:
    return SiteConfig(key=key, name="示例站", base_url="https://demo.test", adapter=adapter)


def demo_data(site: SiteConfig) -> SiteData:
    vod = Vod(
        site_key=site.key,
        vid="ch4l5b3mn",
        sid="ch4l5b3mn",
        name="示例影片",
        year=2024,
        cat_raw=["电影"],
        cat_norm="电影",
        lines=[
            Line(
                name="主线路",
                episodes=[
                    Episode(
                        index=1,
                        name="第01集",
                        url="https://oss.douyinbit.com/m3u8/a/1.m3u8",
                        alts=[AltURL(url="https://myoss.douinbit.top/m3u8/a/1.m3u8", host="myoss.douinbit.top", note="alts")],
                    )
                ],
            )
        ],
        fingerprint="fp1",
    )
    return SiteData(
        site_key=site.key,
        site_name=site.name,
        base_url=site.base_url,
        adapter=site.adapter,
        tier=site.tier(),
        categories=[],
        vods=[vod],
    )


class TestTvboxEntry:
    def test_tier1_entry_is_direct_and_has_no_spider(self):
        site = demo_site("maccms_v10")
        entry = tvbox_mod.build_entry(site, demo_data(site), {})
        assert entry["type"] == 1
        assert entry["api"] == "https://demo.test/api.php/provide/vod/"
        assert "ext" not in entry
        assert entry["key"] == "demo"

    def test_trailing_slash_of_api_path_is_preserved(self):
        site = demo_site("maccms_v10")
        site.maccms.api_path = "/custom/vod/"
        entry = tvbox_mod.build_entry(site, None, {})
        assert entry["api"].endswith("/custom/vod/")

    def test_recipe_override_wins(self):
        site = demo_site("maccms_v10")
        site.tvbox.playerType = 1
        site.tvbox.searchable = 0
        entry = tvbox_mod.build_entry(site, None, {})
        assert entry["playerType"] == 1
        assert entry["searchable"] == 0

    def test_tier4_without_gateway_gets_no_api(self):
        site = demo_site("playwright_sniff")
        entry = tvbox_mod.build_entry(site, demo_data(site), {}, gateway_base="", use_gateway=False)
        assert not entry.get("api")

    def test_tier4_with_gateway_points_at_gateway(self):
        site = demo_site("playwright_sniff")
        entry = tvbox_mod.build_entry(
            site, demo_data(site), {}, gateway_base="https://gw.test", use_gateway=True
        )
        assert entry["api"] == "https://gw.test/vod/demo"

    def test_entry_satisfies_tvbox_schema(self):
        site = demo_site("maccms_v10")
        config = tvbox_mod.build_config(
            entries=[tvbox_mod.build_entry(site, None, {})], defaults={}, run_id="t"
        )
        jsonschema.validate(config, SCHEMA)

    def test_config_never_ships_a_spider(self):
        config = tvbox_mod.build_config(entries=[], defaults={}, run_id="t")
        assert config["spider"] == ""


class TestGenerateAll:
    def test_direct_flavour_excludes_bridged_sites(self, tmp_path, monkeypatch):
        from harvest.config import Settings

        settings = Settings(root=tmp_path)
        settings.ensure_dirs()
        site1 = demo_site("maccms_v10", "t1")
        site2 = demo_site("playwright_sniff", "t4")
        generated = tvbox_mod.generate_all(
            [(site1, demo_data(site1)), (site2, demo_data(site2))], settings=settings, flavors=("direct",)
        )
        assert generated[0].site_keys == ["t1"]
        assert any(s["key"] == "t4" for s in generated[0].skipped)

    def test_gateway_flavour_skips_tier4_when_no_gateway(self, tmp_path):
        from harvest.config import Settings

        settings = Settings(root=tmp_path)
        settings.ensure_dirs()
        site = demo_site("playwright_sniff", "t4")
        generated = tvbox_mod.generate_all(
            [(site, demo_data(site))], settings=settings, flavors=("gateway",)
        )
        assert generated[0].site_count == 0
        assert generated[0].skipped[0]["reason"] == "gateway_not_configured"

    def test_recipe_declared_gateway_publishes_without_the_flag(self, tmp_path):
        """A site that names its gateway is self-contained.

        Regression: the gate only looked at `gateway_base`, so a nightly run with no
        `HARVEST_GATEWAY` variable dropped an otherwise complete Tier-4 entry as
        `gateway_not_configured`. The entry then vanished from a subscription that had been
        working, and nothing in the run reported it as an error.
        """
        from harvest.config import Settings

        settings = Settings(root=tmp_path)
        settings.ensure_dirs()
        site = demo_site("playwright_sniff", "t4")
        site.probe["gateway"] = "https://gw.example"
        generated = tvbox_mod.generate_all(
            [(site, demo_data(site))], settings=settings, flavors=("gateway",)
        )
        assert generated[0].site_keys == ["t4"]
        entry = json.loads((settings.tvbox_dir / "index.gateway.json").read_text("utf-8"))["sites"][0]
        assert entry["api"] == "https://gw.example/vod/t4"

    def test_direct_flavour_does_not_advertise_a_gateway(self, tmp_path):
        """The direct config must not name a route none of its entries take."""
        from harvest.config import Settings

        settings = Settings(root=tmp_path)
        settings.ensure_dirs()
        site = demo_site("maccms_v10", "t1")
        tvbox_mod.generate_all([(site, demo_data(site))], settings=settings, flavors=("direct",))
        config = json.loads((settings.tvbox_dir / "index.direct.json").read_text("utf-8"))
        assert "gateway" not in config

    def test_direct_flavour_never_publishes_a_bridged_api(self, tmp_path):
        """A Tier-2 site that names its gateway must not leak into the direct config.

        Regression: adaptors build `api` from `probe.gateway`, so once a recipe started declaring
        its gateway, a Tier-2 site passed the tier gate and was published in `index.json` with a
        `.../vod/<key>` address -- a "direct" subscription that silently needs the gateway.
        """
        from harvest.config import Settings

        settings = Settings(root=tmp_path)
        settings.ensure_dirs()
        site = demo_site("json_custom", "t2")
        site.probe["gateway"] = "https://gw.example"
        generated = tvbox_mod.generate_all(
            [(site, demo_data(site))], settings=settings, flavors=("direct", "gateway")
        )
        direct = json.loads((settings.tvbox_dir / "index.direct.json").read_text("utf-8"))
        assert direct["sites"] == []
        assert generated[0].skipped[0]["reason"] == "no_api"
        bridged = json.loads((settings.tvbox_dir / "index.gateway.json").read_text("utf-8"))
        assert bridged["sites"][0]["api"] == "https://gw.example/vod/t2"

    def test_experimental_flavour_includes_everything(self, tmp_path):
        from harvest.config import Settings

        settings = Settings(root=tmp_path)
        settings.ensure_dirs()
        sites = [demo_site("maccms_v10", "t1"), demo_site("playwright_sniff", "t4")]
        generated = tvbox_mod.generate_all(
            [(s, demo_data(s)) for s in sites], settings=settings, flavors=("experimental",)
        )
        assert set(generated[0].site_keys) == {"t1", "t4"}

    def test_per_site_file_written(self, tmp_path):
        from harvest.config import Settings

        settings = Settings(root=tmp_path)
        settings.ensure_dirs()
        site = demo_site("maccms_v10", "t1")
        tvbox_mod.generate_all([(site, demo_data(site))], settings=settings, flavors=("direct",))
        single = json.loads((settings.tvbox_dir / "sites" / "t1.json").read_text(encoding="utf-8"))
        assert single["sites"][0]["key"] == "t1"


class TestSnapshot:
    def test_alts_are_expanded_into_extra_lines(self, tmp_path):
        from harvest.config import Settings

        settings = Settings(root=tmp_path)
        settings.ensure_dirs()
        site = demo_site("playwright_sniff", "t4")
        write_snapshot(site, demo_data(site), settings=settings)

        manifest = load_snapshot("t4", settings)
        record = manifest["records"][0]
        assert record["vod_play_from"].count("$$$") == 1
        assert "myoss.douinbit.top" in record["vod_play_url"]

    def test_manifest_records_sid_map(self, tmp_path):
        from harvest.config import Settings

        settings = Settings(root=tmp_path)
        settings.ensure_dirs()
        site = demo_site("maccms_v10", "t1")
        write_snapshot(site, demo_data(site), settings=settings)
        manifest = load_snapshot("t1", settings)
        assert manifest["sid_map"]["ch4l5b3mn"] == "ch4l5b3mn"

    def test_detail_record_has_required_maccms_fields(self):
        site = demo_site("maccms_v10", "t1")
        record = vod_to_detail_record(demo_data(site).vods[0], {})
        for field in ("vod_id", "vod_name", "vod_play_from", "vod_play_url", "type_name"):
            assert field in record

    def test_stale_parts_are_removed_on_rewrite(self, tmp_path):
        from harvest.config import Settings

        settings = Settings(root=tmp_path)
        settings.ensure_dirs()
        site = demo_site("maccms_v10", "t1")
        write_snapshot(site, demo_data(site), settings=settings)
        stale = settings.snapshot_dir / "t1" / "list-part-099.json"
        stale.write_text("{}", encoding="utf-8")
        write_snapshot(site, demo_data(site), settings=settings)
        assert not stale.exists(), "a shrinking catalogue must not leave ghost shards behind"


class TestIssueParsing:
    BODY = """### 站点地址

https://www.example.com/

### 站点名称

示例影视

### 适配器类型

自动探测

### 关注分类

电影,连续剧

### 备注

接口路径 /api.php/provide/vod/

### 合规确认

- [X] 我确认该站点可公开访问
"""

    def test_parses_fields_by_heading(self):
        fields = parse_issue_body(self.BODY)
        assert fields["site_url"] == "https://www.example.com/"
        assert fields["site_name"] == "示例影视"
        assert fields["adapter"] == "自动探测"
        assert fields["categories"] == "电影,连续剧"

    def test_missing_fields_do_not_crash(self):
        assert parse_issue_body("### 别的标题\n\n值\n") == {}

    def test_no_response_placeholder_becomes_empty(self):
        fields = parse_issue_body("### 站点名称\n\n_No response_\n")
        assert fields["site_name"] == ""


class TestUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8080/",
            "http://127.0.0.1/",
            "http://10.1.2.3/",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://0.0.0.0/",
            "http://printer.local/",
            "not a url",
            "",
        ],
    )
    def test_rejects_non_public_targets(self, url):
        ok, _ = validate_public_url(url)
        assert ok is False

    @pytest.mark.parametrize(
        "url",
        ["https://www.example.com/", "http://cdn.example.net:8080/x", "example.com"],
    )
    def test_accepts_public_targets(self, url):
        ok, resolved = validate_public_url(url)
        assert ok is True
        if not url.startswith("http"):
            assert resolved.startswith("https://") or resolved.startswith("http://")

    def test_scheme_is_added_when_missing(self):
        ok, resolved = validate_public_url("example.com")
        assert ok and resolved.startswith("https://example.com")


class TestGamblingHostsNeverReachOutput:
    """End-to-end-ish guard: a filtered host must not survive into the generated payload."""

    def test_blocked_host_absent_from_snapshot(self, tmp_path):
        from harvest.clean.dedupe import clean_site_data
        from harvest.config import Settings

        settings = Settings(root=tmp_path)
        settings.ensure_dirs()
        site = demo_site("maccms_v10", "t1")
        data = demo_data(site)
        data.vods.append(
            Vod(
                site_key="t1",
                vid="bad1",
                sid="bad1",
                name="某棋牌合集",
                cat_raw=["棋牌"],
                lines=[Line(name="L", episodes=[Episode(index=1, name="p", url="https://kaiyun-888.bc.example.com/a.m3u8")])],
            )
        )
        result = clean_site_data(data)
        assert all(v.vid != "bad1" for v in result.site_data.vods)

        write_snapshot(site, result.site_data, settings=settings)
        blob = json.dumps(load_snapshot("t1", settings), ensure_ascii=False)
        assert "kaiyun-888.bc.example.com" not in blob


def test_a_disabled_site_is_never_published_even_with_data(tmp_path):
    """`enabled: false` must remove a site even when its harvested data is still on disk.

    `generate --all` reads every normalized file, so a site switched off in its recipe kept
    appearing in the subscription until its data was deleted by hand. That is the opposite of
    what the flag is documented to do, and it is how a source we had just judged unsuitable
    (82% of its rows filtered as adult) would have stayed published.
    """
    from harvest.config import Settings

    settings = Settings(root=tmp_path)
    settings.ensure_dirs()
    live = demo_site("maccms_v10", "keep")
    off = demo_site("maccms_v10", "drop")
    off.enabled = False

    generated = tvbox_mod.generate_all(
        [(live, demo_data(live)), (off, demo_data(off))],
        settings=settings,
        flavors=("direct",),
    )

    config = json.loads((settings.tvbox_dir / "index.direct.json").read_text("utf-8"))
    assert [s["key"] for s in config["sites"]] == ["keep"]
    assert {"key": "drop", "reason": "disabled"} in config["_skipped"]
    assert generated[0].site_keys == ["keep"]


