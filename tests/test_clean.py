"""Cleaning-stage tests: filtering, de-duplication, and the gambling (BC) pipeline.

The BC tests carry the most weight. A false positive silently deletes legitimate content from
someone's subscription, and a false negative ships gambling advertising into their player --
so both directions are asserted explicitly.
"""

from __future__ import annotations

import pytest

from harvest.clean.classify import load_category_map
from harvest.clean.dedupe import clean_site_data
from harvest.clean.gambling import (
    check_entry,
    check_manifest,
    decode_base64_host_list,
    scan_site_html,
)
from harvest.models import AltURL, Episode, Line, SiteData, Vod


def make_vod(
    *,
    vid: str = "1",
    name: str = "正常影片",
    cat: str = "电影",
    urls: list[str] | None = None,
    remarks: str | None = None,
    content: str | None = None,
) -> Vod:
    urls = urls or ["https://cdn.example.net/a/1.m3u8"]
    return Vod(
        site_key="demo",
        vid=vid,
        sid=vid,
        name=name,
        cat_raw=[cat],
        remarks=remarks,
        content=content,
        lines=[
            Line(
                name="线路1",
                episodes=[Episode(index=i + 1, name=f"第{i + 1:02d}集", url=u) for i, u in enumerate(urls)],
            )
        ],
    )


def make_site_data(vods: list[Vod]) -> SiteData:
    return SiteData(site_key="demo", site_name="Demo", base_url="https://demo.test", adapter="maccms_v10", tier=1, vods=vods)


class TestCategoryMapping:
    def test_known_category_maps(self):
        cmap = load_category_map()
        assert cmap.norm(["动作片"])[0] == "电影"
        assert cmap.norm(["国产剧"])[0] == "连续剧"
        assert cmap.norm(["番剧"])[0] == "动漫"

    def test_unknown_category_falls_back_but_is_preserved(self):
        cmap = load_category_map()
        assert cmap.norm(["某个没见过的分类"])[0] == cmap.unknown_policy

    def test_drop_categories_detected(self):
        cmap = load_category_map()
        assert cmap.is_dropped(["成人"]) is not None
        assert cmap.is_dropped(["电影"]) is None


class TestCategoryAuditAgainstRealCatalogue:
    """Audit the mapping table against the 44 category names actually returned by a live
    MacCMS site.

    Real category vocabularies are where mapping tables rot: the first live run reported
    `电影片` / `综艺片` / `动漫片` as unmapped, because the table only listed `电影` / `综艺` /
    `动漫`. Rather than bolting on three spellings, the suffix retry handles the whole family,
    and this test pins the outcome so the next variant shows up as a failure instead of a
    silent fall-through to 其他.
    """

    # Verbatim from a live `?ac=list` class table (44 entries).
    LIVE_CATEGORIES = [
        "电影片", "连续剧", "综艺片", "动漫片", "动作片", "喜剧片", "爱情片", "科幻片",
        "恐怖片", "剧情片", "战争片", "国产剧", "香港剧", "韩国剧", "欧美剧", "记录片",
        "台湾剧", "日本剧", "海外剧", "泰国剧", "大陆综艺", "港台综艺", "日韩综艺",
        "欧美综艺", "国产动漫", "日韩动漫", "欧美动漫", "港台动漫", "海外动漫", "伦理片",
        "电影解说", "体育", "足球", "篮球", "网球", "斯诺克", "演员", "新闻资讯",
        "电影资讯", "娱乐新闻", "预告片", "短剧", "动画片", "AI漫剧",
    ]

    def test_catalogue_size_is_what_we_sampled(self):
        assert len(self.LIVE_CATEGORIES) == 44

    def test_no_category_is_unrecognised(self):
        cmap = load_category_map()
        unresolved = [
            name
            for name in self.LIVE_CATEGORIES
            if not cmap.resolve_one(name).resolved
        ]
        assert unresolved == [], f"unmapped categories need table entries: {unresolved}"

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            # Suffix retry: the variant family resolved by one rule instead of three entries.
            ("电影片", "电影"),
            ("综艺片", "综艺"),
            ("动漫片", "动漫"),
            # Exact table.
            ("动作片", "电影"),
            ("记录片", "纪录片"),
            ("动画片", "动漫"),
            ("短剧", "短剧"),
            ("国产动漫", "动漫"),
            ("大陆综艺", "综艺"),
            # Fallback: every "*剧" is a series.
            ("韩国剧", "连续剧"),
            ("日本剧", "连续剧"),
            # Fallback: specific content-type checks must beat the broad suffix rules.
            ("AI漫剧", "动漫"),
            ("斯诺克", "体育"),
            ("网球", "体育"),
            # Deliberately parked in 其他 -- resolved, but belongs nowhere in particular.
            ("演员", "其他"),
            ("预告片", "其他"),
            ("娱乐新闻", "其他"),
        ],
    )
    def test_expected_targets(self, name, expected):
        assert load_category_map().resolve_one(name).target == expected

    def test_drop_category_wins_over_other_names(self):
        """A drop must take precedence even when a second name maps cleanly -- keeping one
        adult title is worse than dropping one film."""
        cmap = load_category_map()
        assert cmap.resolve(["电影", "伦理片"]).dropped_reason is not None

    def test_resolved_flag_is_false_only_for_true_misses(self):
        cmap = load_category_map()
        assert cmap.resolve_one("演员").resolved is True, "explicit 其他 entry is a decision"
        assert cmap.resolve_one("某种从没见过的分类").resolved is False

    def test_matched_by_is_reported_for_debugging(self):
        cmap = load_category_map()
        assert cmap.resolve_one("电影片").matched_by == "strip:片"
        assert cmap.resolve_one("动作片").matched_by == "exact"
        assert cmap.resolve_one("韩国剧").matched_by == "rule:endswith:剧"


class TestCleaning:
    def test_drops_unplayable_vod(self):
        vod = make_vod()
        vod.lines = []
        result = clean_site_data(make_site_data([vod]))
        assert result.site_data.vods == []
        assert result.dropped.counts["no_playable_line"] == 1

    def test_drops_placeholder_urls(self):
        vod = make_vod(urls=["https://example.com/a.m3u8", "https://cdn.example.net/ok.m3u8"])
        result = clean_site_data(make_site_data([vod]))
        assert result.dropped.counts["implausible_media_url"] == 1
        assert len(result.site_data.vods[0].lines[0].episodes) == 1

    def test_drops_adult_category(self):
        result = clean_site_data(make_site_data([make_vod(cat="成人")]))
        assert result.site_data.vods == []
        assert any(reason.startswith("drop_category") for reason in result.dropped.counts)

    def test_keeps_legitimate_title_containing_risky_words(self):
        """'赌神' is a film. A blocklist that removes it is worse than useless."""
        vod = make_vod(name="赌神", content="周润发主演的经典赌片")
        result = clean_site_data(make_site_data([vod]))
        assert len(result.site_data.vods) == 1

    def test_folds_duplicate_episode_urls_into_alts(self):
        vod = make_vod(urls=["https://cdn.example.net/a/1.m3u8", "https://cdn.example.net/a/1.m3u8"])
        result = clean_site_data(make_site_data([vod]))
        kept = result.site_data.vods[0]
        assert len(kept.lines[0].episodes) == 1
        assert kept.lines[0].episodes[0].alts
        assert result.dropped.counts["duplicate_episode_url"] == 1

    def test_deduplicates_by_fingerprint_and_keeps_richer_entry(self):
        thin = make_vod(vid="a", name="某片 1080P", urls=["https://cdn.example.net/a/1.m3u8"])
        rich = make_vod(
            vid="b",
            name="某片",
            urls=["https://cdn.example.net/b/1.m3u8", "https://cdn.example.net/b/2.m3u8"],
        )
        result = clean_site_data(make_site_data([thin, rich]))
        assert len(result.site_data.vods) == 1
        assert result.site_data.vods[0].vid == "b", "should keep the entry with more episodes"
        assert result.dropped.counts["duplicate_fingerprint"] == 1

    def test_deduplicates_same_vid(self):
        result = clean_site_data(make_site_data([make_vod(vid="x"), make_vod(vid="x")]))
        assert len(result.site_data.vods) == 1
        assert result.dropped.counts["duplicate_vid"] == 1

    def test_normalizes_title_and_category_on_survivors(self):
        vod = make_vod(name="某片 4K 蓝光中字", cat="动作片")
        result = clean_site_data(make_site_data([vod]))
        kept = result.site_data.vods[0]
        assert kept.name == "某片 4K 蓝光中字"      # display name is left alone
        assert kept.fingerprint                        # but a comparable key is computed
        assert kept.cat_norm == "电影"

    def test_reports_unknown_categories(self):
        result = clean_site_data(make_site_data([make_vod(cat="稀奇古怪分类")]))
        assert result.unknown_categories["稀奇古怪分类"] == 1

    def test_allow_policy_keeps_adult_category(self):
        result = clean_site_data(
            make_site_data([make_vod(cat="成人")]), adult_filter=False, bc_filter=False
        )
        assert len(result.site_data.vods) == 1


class TestGamblingSiteScan:
    def test_clean_page_is_not_quarantined(self):
        html = '<html><body><a href="/play/1">影片</a><a href="https://cdn.example.net/a.m3u8">播放</a></body></html>'
        scan = scan_site_html(html, "https://demo.test/")
        assert scan.quarantined is False

    def test_page_with_injected_gambling_redirect_is_quarantined(self):
        html = """
        <html><body>
        <a href="https://kaiyun-888.ad.example.com/?pid=1">广告</a>
        <script>window.location.href = "https://boyu-betting.ad.example.com/x";</script>
        <iframe src="https://leyu-ad.example.com/frame"></iframe>
        </body></html>
        """
        scan = scan_site_html(html, "https://demo.test/")
        assert scan.quarantined is True
        assert scan.kinds >= 2

    def test_keyword_density_alone_is_recorded(self):
        html = "<html><body>" + "博彩 真人娱乐 百家乐 投注 返水 上分 彩金 " * 3 + "</body></html>"
        scan = scan_site_html(html, "https://demo.test/")
        assert scan.keyword_hits
        assert "keyword_density" in scan.signals


class TestGamblingEntry:
    def test_legitimate_entry_passes(self):
        verdict = check_entry(make_vod(name="纪录片：海洋"))
        assert verdict.dropped is False

    def test_gambling_category_is_dropped(self):
        verdict = check_entry(make_vod(name="某合集", cat="棋牌"))
        assert verdict.dropped is True
        assert any(r.startswith("bc_category") for r in verdict.reasons)

    def test_gambling_naming_is_dropped(self):
        vod = make_vod(name="真人娱乐 百家乐 卡牌", cat="电影")
        verdict = check_entry(vod)
        assert verdict.dropped is True

    def test_blacklisted_media_host_is_dropped(self):
        vod = make_vod(urls=["https://kaiyun-888.bc.example.com/live/a.m3u8"])
        verdict = check_entry(vod)
        assert verdict.dropped is True
        assert verdict.bad_hosts

    def test_promotion_query_parameters_are_flagged(self):
        vod = make_vod(urls=["https://cdn.example.net/a/index.m3u8?pid=88771&channel=tg"])
        verdict = check_entry(vod)
        assert verdict.dropped is True
        assert any("bc_promo_params" in r for r in verdict.reasons)

    def test_signed_url_with_harmless_params_is_kept(self):
        """`token`/`expires` are normal CDN params -- flagging them would break real sources."""
        vod = make_vod(urls=["https://cdn.example.net/a/index.m3u8?token=abc&expires=123"])
        verdict = check_entry(vod)
        assert verdict.dropped is False

    def test_filter_can_be_disabled_per_site(self):
        verdict = check_entry(make_vod(name="真人娱乐 百家乐", cat="棋牌"), policy_bc_filter=False)
        assert verdict.dropped is False


class TestManifestScan:
    def test_ad_tag_with_blacklisted_host_is_flagged(self):
        manifest = """#EXTM3U
#EXT-X-AD:URL="https://kaiyun-888.bc.example.com/ad.mp4"
#EXTINF:10.0,
seg1.ts
"""
        verdict = check_manifest(manifest)
        assert verdict.dropped is True

    def test_clean_manifest_passes(self):
        manifest = "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10.0,\nseg1.ts\n"
        assert check_manifest(manifest).dropped is False


class TestBase64HostDecoding:
    def test_decodes_observed_payload(self):
        payload = "WyJvc3MuZG91eWluYml0LmNvbSIsIm15b3NzLmRvdXlpbmJpdC50b3AiXQ=="
        assert decode_base64_host_list(payload) == ["oss.douyinbit.com", "myoss.douyinbit.top"]

    def test_rejects_garbage(self):
        assert decode_base64_host_list("not base64 at all!!") == []
        assert decode_base64_host_list("") == []

    def test_rejects_non_host_entries(self):
        import base64

        payload = base64.b64encode(b'["ok.example.com","<script>"]').decode()
        assert decode_base64_host_list(payload) == ["ok.example.com"]


class TestAltsSurviveCleaning:
    def test_alts_are_preserved_on_kept_episodes(self):
        vod = make_vod()
        vod.lines[0].episodes[0].alts.append(
            AltURL(url="https://mirror.example.net/a/1.m3u8", host="mirror.example.net", note="alts")
        )
        result = clean_site_data(make_site_data([vod]))
        assert result.site_data.vods[0].lines[0].episodes[0].alts
