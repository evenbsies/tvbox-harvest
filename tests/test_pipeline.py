"""Pipeline-level tests: recipe persistence, registry handling, and CLI safety helpers.

The recipe write-back tests exist because of a real regression. ``save_recipe`` originally
round-tripped ``model_dump()`` through ``yaml.safe_dump``, which stripped every comment and
reflowed the document. Since the recipe file is where a site's reconciliation is recorded for
the next person -- why it is Tier 4, which API path, which failover hosts, what the signature
requirement is -- that behaviour silently destroyed the project's most valuable documentation
the first time a probe wrote back a result.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from harvest.config import Settings
from harvest.models import ProbeResult, SiteConfig
from harvest.pipeline import (
    MACHINE_OWNED_KEYS,
    apply_probe_patch,
    load_recipe,
    load_registry,
    registry_entry_from,
    save_recipe,
    save_registry,
)

COMMENTED_RECIPE = '''\
# Tier-4 recipe: a site whose play addresses only exist at runtime.
#
# RECONNAISSANCE NOTES -- these must survive a probe write-back:
#   * /api.php/provide/vod/ returns Go's default 404 body, so this is not PHP/MacCMS.
#   * the play address arrives via a *signed* internal call and cannot be constructed.

key: demo
name: 示例站
base_url: https://demo.test
adapter: playwright_sniff
enabled: false

request:
  rate_limit: 0.5          # deliberately gentle: browser-driven crawl

browser:
  # The second host really is spelled this way upstream.
  fallback_hosts:
    - oss.example.com
    - myoss.exmple.com

detect:
  version: 1
  result: playwright_sniff
  tier: 4
  status: ok
'''


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    s = Settings(root=tmp_path)
    (tmp_path / "sites").mkdir(parents=True, exist_ok=True)
    s.ensure_dirs()
    return s


class TestRecipeWriteBack:
    def _write(self, settings: Settings) -> Path:
        path = settings.sites_dir / "demo.yaml"
        path.write_text(COMMENTED_RECIPE, encoding="utf-8")
        return path

    def test_load_reads_the_recipe(self, settings):
        self._write(settings)
        site = load_recipe("demo", settings)
        assert site is not None
        assert site.adapter == "playwright_sniff"
        assert site.tier() == 4
        assert site.browser["fallback_hosts"] == ["oss.example.com", "myoss.exmple.com"]

    def test_comments_survive_a_writeback(self, settings):
        """The regression: comments must not be destroyed by a probe result being persisted."""
        path = self._write(settings)
        site = load_recipe("demo", settings)
        assert site is not None

        site = apply_probe_patch(
            site,
            ProbeResult(
                tier=4,
                adapter="playwright_sniff",
                status="ok",
                evidence={"stages": [{"stage": "preflight"}]},
                patch={"browser": {"fallback_hosts": ["new.example.com"]}},
            ),
        )
        save_recipe(site, settings)

        text = path.read_text(encoding="utf-8")
        assert "# RECONNAISSANCE NOTES" in text
        assert "not PHP/MacCMS" in text
        assert "deliberately gentle" in text
        assert "spelled this way upstream" in text

    def test_machine_keys_are_updated(self, settings):
        path = self._write(settings)
        site = load_recipe("demo", settings)
        assert site is not None

        site = apply_probe_patch(
            site,
            ProbeResult(
                tier=1,
                adapter="maccms_v10",
                status="ok",
                patch={"maccms": {"api_path": "/custom/vod/"}},
                evidence={"stages": [{"stage": "tier1_match", "path": "/custom/vod/"}]},
            ),
        )
        save_recipe(site, settings)

        reloaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert reloaded["adapter"] == "maccms_v10"
        assert reloaded["detect"]["tier"] == 1
        assert reloaded["detect"]["status"] == "ok"
        assert reloaded["maccms"]["api_path"] == "/custom/vod/"

    def test_human_keys_are_not_rewritten(self, settings):
        """A write-back must not inject every default back into a deliberately lean recipe."""
        path = self._write(settings)
        site = load_recipe("demo", settings)
        assert site is not None
        site = apply_probe_patch(site, ProbeResult(tier=4, adapter="playwright_sniff", status="ok"))
        save_recipe(site, settings)

        reloaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        # Fields the machine does not own should be present only because the human put them there.
        assert reloaded["request"]["rate_limit"] == 0.5
        assert "compliance" not in reloaded
        assert reloaded["enabled"] is False

    def test_machine_owned_key_list_is_what_we_think(self):
        assert set(MACHINE_OWNED_KEYS) == {"adapter", "detect", "maccms", "browser", "selectors", "probe"}

    def test_new_recipe_is_written_in_full(self, settings):
        site = SiteConfig(key="fresh", name="新站", base_url="https://fresh.test", adapter="maccms_v10")
        path = save_recipe(site, settings)
        assert path.exists()
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["key"] == "fresh"
        assert data["base_url"] == "https://fresh.test"

    def test_nested_comments_survive_merge(self, settings):
        path = self._write(settings)
        site = load_recipe("demo", settings)
        assert site is not None
        save_recipe(site, settings)
        assert "# The second host really is spelled this way upstream." in path.read_text(encoding="utf-8")


class TestRegistry:
    def test_add_then_update_is_idempotent(self, settings):
        registry = load_registry(settings)
        site = SiteConfig(key="a", name="A", base_url="https://a.test", adapter="maccms_v10")
        registry.add_or_update(registry_entry_from(site))
        registry.add_or_update(registry_entry_from(site))
        assert len(registry.sites) == 1
        save_registry(registry, settings)

        reloaded = load_registry(settings)
        assert len(reloaded.sites) == 1
        assert reloaded.find("a") is not None

    def test_update_preserves_origin_metadata(self, settings):
        registry = load_registry(settings)
        site = SiteConfig(key="b", name="B", base_url="https://b.test", adapter="maccms_v10")
        registry.add_or_update(registry_entry_from(site, origin={"type": "issue", "issue": 42}))
        registry.add_or_update(registry_entry_from(site))
        entry = registry.find("b")
        assert entry is not None
        assert entry.origin.get("issue") == 42, "re-registering must not drop provenance"
