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
from collections.abc import Collection
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

# A byte-identical copy of the direct entry point under a self-describing name, for users who
# prefer an explicit "not the gateway build" filename. It is a real artefact written on every
# run -- it was once mistaken for a stale leftover and dropped from the jsDelivr purge list,
# which would have left that copy un-refreshed for up to 12 hours.
DIRECT_ALIAS_FILE = "index.direct.json"


def produced_index_files() -> tuple[str, ...]:
    """Every file `generate_all` writes into `data/tvbox/`, as bare filenames.

    Single source of truth for "what is a generated entry point". Anything that needs to
    enumerate them -- the jsDelivr purge list, its regression test -- reads this instead of
    restating the names, which is how the two drifted apart before.
    """
    return (*FLAVOR_FILES.values(), DIRECT_ALIAS_FILE)


def default_purge_targets() -> list[str]:
    """Repo-relative paths of the generated entry points, for cache invalidation.

    Used when a run's git diff is empty and there is nothing to infer targets from. Derived
    rather than hardcoded: a hardcoded copy had already drifted from reality twice, and both
    times the consequence was a stale subscription served from jsDelivr's 12-hour branch cache.
    """
    return [f"data/tvbox/{name}" for name in produced_index_files()]


@dataclass
class GeneratedConfig:
    flavor: str
    path: Path
    site_count: int
    site_keys: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    # Sites that this run had no data for and whose last published entry was carried forward.
    carried_over: list[str] = field(default_factory=list)

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
    parses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    top = dict(defaults.get("top_level") or {})
    # Pop unconditionally: `config.update(top)` below would otherwise put the defaults' empty
    # `parses: []` back over whatever was passed in, and the discovered routes would vanish
    # silently while the run reported them as kept.
    default_parses = top.pop("parses", [])
    config: dict[str, Any] = {
        "sites": entries,
        "lives": top.pop("lives", []),
        # `parses` come from config/parses.yaml (解析线路), filtered by name there. Anything
        # declared in the defaults file is used only when nothing was passed in.
        "parses": list(parses if parses is not None else default_parses),
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


def load_published_entries(path: Path) -> dict[str, dict[str, Any]]:
    """The site entries of an already-written config, keyed by site key.

    Used to carry forward sites a given run has no data for. Returns an empty mapping when the
    file is absent or unreadable -- a missing previous config is the normal state on the first
    run, not an error.
    """
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    entries: dict[str, dict[str, Any]] = {}
    if isinstance(config, dict):
        for entry in config.get("sites") or []:
            if isinstance(entry, dict) and entry.get("key"):
                entries[str(entry["key"])] = entry
    return entries


def _gateway_for(site: SiteConfig, gateway_base: str) -> str:
    """The gateway this site should be published against.

    The `--gateway` flag / `HARVEST_GATEWAY` variable wins, but a recipe may name its own
    gateway in `probe.gateway` -- and then it is self-contained. That matters more than it
    looks: the gate below used to consider only `gateway_base`, so a nightly run with no
    variable set dropped a fully configured Tier-4 site as `gateway_not_configured`, and
    the published subscription silently lost a source that had been working. A repository
    variable is invisible from the file that owns the site; a field in the recipe is not.
    """
    return gateway_base or str((site.probe or {}).get("gateway") or "")


def _rebuild_carried(
    key: str,
    stored: dict[str, Any],
    defaults: dict[str, Any],
    *,
    settings: Settings,
    gateway_base: str,
    flavor: str,
    name_suffix: bool,
) -> dict[str, Any] | None:
    """Project a carried-over site from its recipe instead of copying the old entry.

    Returns ``None`` when the site cannot be published in this flavour at all, which is the
    honest answer for a bridged site in the direct config.
    """
    from harvest.pipeline import load_recipe  # local: pipeline imports this module

    try:
        site = load_recipe(key, settings)
    except Exception:
        site = None
    if site is None:
        return dict(stored)  # no recipe to consult: the stored entry is all that is known

    gateway = "" if flavor == "direct" else _gateway_for(site, gateway_base)
    entry = build_entry(site, None, defaults, gateway_base=gateway, use_gateway=bool(gateway))
    if name_suffix:
        template = str((defaults.get("name_suffix") or {}).get("template") or " [{tier}]")
        entry["name"] = f"{entry.get('name', key)}{template.format(tier=f'T{site.tier()}')}"
    if not entry.get("api"):
        # A recipe that produces no address is one this flavour cannot publish. Falling back to
        # the stored entry would put a bridged address into the direct config, which is exactly
        # what the direct flavour promises not to do.
        return None
    return entry


def generate_all(
    pairs: list[tuple[SiteConfig, SiteData | None]],
    *,
    settings: Settings | None = None,
    gateway_base: str = "",
    flavors: tuple[str, ...] = FLAVORS,
    run_id: str = "",
    carry_over: Collection[str] = (),
    parses: list[dict[str, Any]] | None = None,
) -> list[GeneratedConfig]:
    """Render every requested flavour from one set of normalized site data.

    ``carry_over`` names sites that *should* be published but have no data in this run. Their
    entries are copied forward from the flavour's existing file. Without it, a run covering a
    subset of sites produces a config listing only that subset -- so accepting one new site
    through the Issue form would silently replace the whole published subscription with a
    single entry. The daily full run masks this; any single-site run exposes it.
    """
    settings = settings or get_settings()
    defaults = load_tvbox_defaults(settings)
    name_suffix = bool((defaults.get("name_suffix") or {}).get("enabled"))
    tvbox_dir = settings.tvbox_dir
    out: list[GeneratedConfig] = []

    def gateway_for(site: SiteConfig) -> str:
        return _gateway_for(site, gateway_base)

    # Per-site files first: they are what a user subscribes to for a single source, and they
    # are the only artefacts that must exist for `verify` to test a site in isolation.
    for site, data in pairs:
        site_gateway = gateway_for(site)
        entry = build_entry(
            site, data, defaults, gateway_base=site_gateway, use_gateway=bool(site_gateway)
        )
        single = build_config(
            entries=[entry], defaults=defaults, run_id=run_id, gateway_base=site_gateway,
            flavor="site",
            # Deliberately empty: a single-site subscription must not drag in parse routes that
            # belong to a different site. Those live in the full configs below.
            parses=[],
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
                # Adaptors build `api` from `probe.gateway`, so a recipe that names its gateway
                # would otherwise land in the *direct* config with a bridged address -- a
                # "direct" subscription that secretly needs a gateway. Strip the field for this
                # flavour rather than teaching every adaptor about flavours: the requirement is
                # about this config's promise, not about the adaptor.
                direct_site = site.model_copy(
                    update={
                        "probe": {
                            k: v for k, v in (site.probe or {}).items() if k != "gateway"
                        }
                    }
                )
                entry = build_entry(direct_site, data, defaults, gateway_base="", use_gateway=False)
                if not entry.get("api"):
                    skipped.append({"key": site.key, "reason": "no_api"})
                    continue
            elif flavor == "gateway":
                site_gateway = gateway_for(site)
                if tier >= 3 and not site_gateway:
                    skipped.append({"key": site.key, "reason": "gateway_not_configured"})
                    continue
                entry = build_entry(
                    site, data, defaults, gateway_base=site_gateway,
                    use_gateway=bool(site_gateway),
                )
                if not entry.get("api"):
                    skipped.append({"key": site.key, "reason": "no_api"})
                    continue
            else:  # experimental
                site_gateway = gateway_for(site)
                entry = build_entry(
                    site, data, defaults, gateway_base=site_gateway,
                    use_gateway=bool(site_gateway),
                )
                if name_suffix:
                    entry["name"] = f"{entry.get('name', site.key)} *exp"

            entries.append(entry)
            keys.append(site.key)

        path = tvbox_dir / FLAVOR_FILES[flavor]

        # Carry forward enabled sites this run has no data for. Read the previous file *before*
        # overwriting it, and only for keys the caller says should be published -- a site that
        # has since been disabled must disappear from the config, not live on in a stale entry.
        #
        # The carried entry is rebuilt from its recipe, not copied verbatim. A copy freezes every
        # field the recipe owns: rename a site and the old name survives until the next time that
        # site happens to be harvested; move the gateway and the entry keeps pointing at the old
        # host. Rebuilding costs nothing (no request is made -- `data` is None) and keeps the
        # recipe as the single source of truth, with the previous file contributing only the fact
        # that the site is published at all.
        carried: list[str] = []
        if carry_over:
            existing = load_published_entries(path)
            for key in carry_over:
                if key in keys or key not in existing:
                    continue
                rebuilt = _rebuild_carried(
                    key, existing[key], defaults, settings=settings, gateway_base=gateway_base,
                    flavor=flavor, name_suffix=name_suffix,
                )
                if rebuilt is None:
                    continue
                entries.append(rebuilt)
                keys.append(key)
                carried.append(key)

        # Deterministic order regardless of which subset was harvested: without this, a partial
        # run appends its carried entries after the fresh ones and every run reshuffles the file.
        entries.sort(key=lambda item: str(item.get("key") or ""))
        keys = [str(item.get("key") or "") for item in entries]

        # The config-level `gateway` is informational, but leaving it empty while entries point
        # at a gateway makes a generated file look broken to whoever reads it next. The direct
        # flavour is the exception: no entry in it can use a gateway, so advertising one there
        # would describe a route the file does not take.
        config_gateway = "" if flavor == "direct" else (
            gateway_base or next(
                (str((s.probe or {}).get("gateway") or "") for s, _ in pairs
                 if (s.probe or {}).get("gateway")),
                "",
            )
        )
        config = build_config(
            entries=entries, defaults=defaults, run_id=run_id, gateway_base=config_gateway,
            flavor=flavor,
            parses=parses,
        )
        if skipped:
            config["_skipped"] = skipped
        if carried:
            config["_carried_over"] = sorted(carried)
        write_config(config, path)
        out.append(
            GeneratedConfig(
                flavor=flavor,
                path=path,
                site_count=len(entries),
                site_keys=keys,
                skipped=skipped,
                carried_over=sorted(carried),
            )
        )

    # Convenience alias: `index.direct.json` is what the README tells users to fall back to
    # when they have not deployed a gateway, so keep it present even when it duplicates.
    direct_path = tvbox_dir / "index.json"
    if direct_path.exists():
        (tvbox_dir / DIRECT_ALIAS_FILE).write_bytes(direct_path.read_bytes())

    return out
