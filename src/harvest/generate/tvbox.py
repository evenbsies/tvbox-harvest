"""TVBox configuration generation.

Contract with the client: TVBox requests ``api`` with ``ac=list`` / ``ac=detail&ids=`` /
``ac=class`` and expects MacCMS-shaped JSON. Three bridge strategies are possible, and this
module's job is to be honest about which one each site needs:

* **direct** -- the site itself speaks MacCMS, so point ``api`` at it. Zero dependencies.
* **gateway** -- a site that does not speak MacCMS is served through the optional edge
  gateway, which re-expresses the snapshot as MacCMS JSON. The entry points at the gateway.
* **experimental** -- Tier 3/4 sites with no bridge configured. They are emitted into a
  separate flavour file so the default subscription never ships a source that cannot work.

The generated top level deliberately leaves ``spider`` empty: this project does not require
a jar, and shipping one would make the config depend on a binary we do not control.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from harvest.config import Settings, get_settings
from harvest.models import SiteConfig, SiteData, utcnow_iso

FLAVORS = ("direct", "gateway", "experimental")

# `direct` is the default subscription; the others are opt-in fallbacks.
FLAVOR_FILES = {
    "direct": "index.json",
    "gateway": "index.gateway.json",
    "experimental": "index.experimental.json",
}


@dataclass
class GeneratedConfig:
    flavor: str
    path: Path
    site_count: int
    site_keys: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    @property
    def digest(self) -> str:
        return hashlib.sha1(self.path.read_bytes()).hexdigest()[:12] if self.path.exists() else ""


def load_tvbox_defaults(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    path = settings.config_dir / "tvbox_defaults.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def build_entry(
    site: SiteConfig,
    site_data: SiteData | None,
    defaults: dict[str, Any],
    *,
    gateway_base: str = "",
    use_gateway: bool = False,
    name_suffix: bool = False,
) -> dict[str, Any]:
    """Produce one ``sites[]`` entry.

    The adapter owns the type/api decision because it knows what shape the site actually
    produces; this function only merges defaults and applies the recipe's explicit overrides.
    """
    from harvest.adapters import base

    adapter_cls = base.get_adapter_class(site.adapter)
    if adapter_cls is None:
        base.load_builtin_adapters()
        adapter_cls = base.get_adapter_class(site.adapter)

    if adapter_cls is not None:
        entry = dict(adapter_cls().build_tvbox_site(site, site_data))
    else:
        entry = {"key": site.key, "name": site.name, "type": 1, "api": ""}

    site_defaults = dict(defaults.get("site_defaults") or {})
    if use_gateway and site.tier() >= 3:
        site_defaults.update(defaults.get("gateway_defaults") or {})
        if gateway_base:
            entry["api"] = f"{gateway_base.rstrip('/')}/vod/{site.key}"

    for key, value in site_defaults.items():
        entry.setdefault(key, value)

    # Recipe overrides win over everything.
    for key, value in site.tvbox.model_dump(exclude_none=True).items():
        entry[key] = value

    if name_suffix:
        template = str((defaults.get("name_suffix") or {}).get("template") or " [{tier}]")
        entry["name"] = f"{entry.get('name', site.key)}{template.format(tier=f'T{site.tier()}')}"

    if entry.get("ext") in (None, ""):
        entry.pop("ext", None)
    return {k: v for k, v in entry.items() if v is not None}


def build_config(
    *,
    entries: list[dict[str, Any]],
    defaults: dict[str, Any],
    run_id: str = "",
    gateway_base: str = "",
    flavor: str = "direct",
) -> dict[str, Any]:
    top = dict(defaults.get("top_level") or {})
    config: dict[str, Any] = {
        "sites": entries,
        "lives": top.pop("lives", []),
        "parses": top.pop("parses", []),
        "flags": top.pop("flags", []),
        "rules": top.pop("rules", []),
    }
    config.update(top)
    config["spider"] = top.get("spider", "")
    config["version"] = f"{flavor}-{run_id or utcnow_iso()}"
    config["generated_at"] = utcnow_iso()
    config["generated_by"] = "tvbox-harvest"
    if gateway_base:
        config["gateway"] = gateway_base
    return config


def write_config(config: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def generate_all(
    pairs: list[tuple[SiteConfig, SiteData | None]],
    *,
    settings: Settings | None = None,
    gateway_base: str = "",
    flavors: tuple[str, ...] = FLAVORS,
    run_id: str = "",
) -> list[GeneratedConfig]:
    """Render every requested flavour from one set of normalized site data."""
    settings = settings or get_settings()
    defaults = load_tvbox_defaults(settings)
    name_suffix = bool((defaults.get("name_suffix") or {}).get("enabled"))
    tvbox_dir = settings.tvbox_dir
    out: list[GeneratedConfig] = []

    # Per-site files first: they are what a user subscribes to for a single source, and they
    # are the only artefacts that must exist for `verify` to test a site in isolation.
    for site, data in pairs:
        entry = build_entry(
            site, data, defaults, gateway_base=gateway_base, use_gateway=bool(gateway_base)
        )
        single = build_config(
            entries=[entry], defaults=defaults, run_id=run_id, gateway_base=gateway_base,
            flavor="site",
        )
        write_config(single, tvbox_dir / "sites" / f"{site.key}.json")

    for flavor in flavors:
        entries: list[dict[str, Any]] = []
        keys: list[str] = []
        skipped: list[dict[str, str]] = []

        for site, data in pairs:
            tier = site.tier()
            if flavor == "direct":
                if tier >= 3:
                    skipped.append({"key": site.key, "reason": f"tier{tier}_needs_bridge"})
                    continue
                entry = build_entry(site, data, defaults, gateway_base="", use_gateway=False)
                if not entry.get("api"):
                    skipped.append({"key": site.key, "reason": "no_api"})
                    continue
            elif flavor == "gateway":
                if tier >= 3 and not gateway_base:
                    skipped.append({"key": site.key, "reason": "gateway_not_configured"})
                    continue
                entry = build_entry(
                    site, data, defaults, gateway_base=gateway_base, use_gateway=bool(gateway_base)
                )
                if not entry.get("api"):
                    skipped.append({"key": site.key, "reason": "no_api"})
                    continue
            else:  # experimental
                entry = build_entry(
                    site, data, defaults, gateway_base=gateway_base, use_gateway=bool(gateway_base)
                )
                if name_suffix:
                    entry["name"] = f"{entry.get('name', site.key)} *exp"

            entries.append(entry)
            keys.append(site.key)

        config = build_config(
            entries=entries, defaults=defaults, run_id=run_id, gateway_base=gateway_base,
            flavor=flavor,
        )
        if skipped:
            config["_skipped"] = skipped
        path = tvbox_dir / FLAVOR_FILES[flavor]
        write_config(config, path)
        out.append(GeneratedConfig(flavor=flavor, path=path, site_count=len(entries), site_keys=keys, skipped=skipped))

    # Convenience alias: `index.direct.json` is what the README tells users to fall back to
    # when they have not deployed a gateway, so keep it present even when it duplicates.
    direct_path = tvbox_dir / "index.json"
    if direct_path.exists():
        (tvbox_dir / "index.direct.json").write_bytes(direct_path.read_bytes())

    return out
