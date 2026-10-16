"""Core data contracts for tvbox-harvest.

Two families of models live here:

1. **Recipe / configuration models** -- ``SiteConfig`` and friends. These are loaded
   from ``sites/<key>.yaml`` and describe *how* to harvest one site.
2. **Normalized data models** -- ``SiteData`` and friends. These are the site-agnostic
   intermediate representation every adapter must produce, and the only thing the
   cleaning stage and the TVBox generator are allowed to consume.

Keeping the two families separate is what makes "write code per site *type*, config per
site *instance*" possible: adapters translate into ``SiteData``, and nothing downstream
ever sees an upstream field name such as ``vod_play_url``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AdapterKind = Literal["maccms_v10", "json_custom", "html_ssr", "playwright_sniff", "auto"]
CompliancePolicy = Literal["allow", "block_adult", "quarantine"]


def utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------------------
# Recipe / configuration models
# --------------------------------------------------------------------------------------


class RequestConfig(BaseModel):
    """Per-site HTTP behaviour. Every outbound request goes through these settings."""

    model_config = ConfigDict(extra="allow")

    headers: dict[str, str] = Field(
        default_factory=lambda: {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )
    rate_limit: float = 1.0  # requests per second, per site
    timeout: float = 15.0
    retries: int = 3
    proxy: str | None = None
    respect_robots: bool = True
    delay_jitter: float = 0.3  # extra random seconds added on top of rate_limit


class ComplianceConfig(BaseModel):
    """Content policy. Defaults are deliberately conservative."""

    policy: CompliancePolicy = "block_adult"
    bc_filter: bool = True  # filter gambling (BC) advertising / operator-supplied sources
    notes: str | None = None


class AuthConfig(BaseModel):
    type: Literal["none", "token", "md5_sign"] = "none"
    # Name of the env var / secret holding the token; never store the token itself.
    token_secret: str | None = None
    token_query_name: str = "token"


class MaccmsConfig(BaseModel):
    api_path: str = "/api.php/provide/vod/"
    categories_all: bool = True
    page_limit: int = 20
    max_pages: int = 40
    incremental_hours: int | None = 24


class DetectInfo(BaseModel):
    """Evidence-fixed result of the probe stage. Written back into the recipe file."""

    version: int = 1
    checked_at: str | None = None
    result: str | None = None
    tier: int | None = None
    status: str | None = None  # ok | needs_mapping | unknown
    stale: bool = False
    evidence: dict[str, Any] = Field(default_factory=dict)


class TvboxOverride(BaseModel):
    """Hand-written overrides merged on top of the adapter-provided TVBox site entry."""

    model_config = ConfigDict(extra="allow")

    type: int | None = None
    api: str | None = None
    ext: str | None = None
    searchable: int | None = None
    quickSearch: int | None = None
    filterable: int | None = None
    playerType: int | None = None
    changeable: int | None = None
    hide: int | None = None


class SiteConfig(BaseModel):
    """A single site recipe -- the atomic unit of declarative configuration."""

    model_config = ConfigDict(extra="allow")

    key: str
    name: str
    base_url: str
    adapter: AdapterKind = "auto"
    enabled: bool = True

    compliance: ComplianceConfig = Field(default_factory=ComplianceConfig)
    request: RequestConfig = Field(default_factory=RequestConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    maccms: MaccmsConfig = Field(default_factory=MaccmsConfig)
    tvbox: TvboxOverride = Field(default_factory=TvboxOverride)
    detect: DetectInfo = Field(default_factory=DetectInfo)

    schedule: str | None = None
    # Free-form blocks consumed by the individual adapters.
    fields: dict[str, Any] = Field(default_factory=dict)
    selectors: dict[str, Any] = Field(default_factory=dict)
    browser: dict[str, Any] = Field(default_factory=dict)
    listing: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    probe: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def origin(self) -> str:
        return self.base_url

    def tier(self) -> int:
        return {"maccms_v10": 1, "json_custom": 2, "html_ssr": 3, "playwright_sniff": 4}.get(
            self.adapter, 0
        )


class RegistryEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    key: str
    name: str
    base_url: str
    adapter: AdapterKind = "auto"
    tier: int | None = None
    enabled: bool = True
    origin: dict[str, Any] = Field(default_factory=dict)
    last_run: dict[str, Any] = Field(default_factory=dict)


class Registry(BaseModel):
    sites: list[RegistryEntry] = Field(default_factory=list)

    def find(self, key: str) -> RegistryEntry | None:
        return next((s for s in self.sites if s.key == key), None)

    def add_or_update(self, entry: RegistryEntry) -> RegistryEntry:
        existing = self.find(entry.key)
        if existing is None:
            self.sites.append(entry)
            return entry
        merged = existing.model_dump()
        merged.update({k: v for k, v in entry.model_dump().items() if v not in (None, {}, [])})
        updated = RegistryEntry.model_validate(merged)
        self.sites[self.sites.index(existing)] = updated
        return updated


# --------------------------------------------------------------------------------------
# Probe result
# --------------------------------------------------------------------------------------


class ProbeResult(BaseModel):
    tier: int  # 0 == not automatable
    adapter: AdapterKind | str = "auto"
    status: Literal["ok", "needs_mapping", "unknown"] = "ok"
    evidence: dict[str, Any] = Field(default_factory=dict)
    # Patch to apply onto the recipe (e.g. {"maccms": {"api_path": "..."}})
    patch: dict[str, Any] = Field(default_factory=dict)
    detail_url_sample: str | None = None
    categories: list[str] = Field(default_factory=list)
    elapsed_ms: int | None = None

    @property
    def automatable(self) -> bool:
        return self.tier > 0 and self.status != "unknown"


# --------------------------------------------------------------------------------------
# Normalized data models (site-agnostic intermediate representation)
# --------------------------------------------------------------------------------------


class AltURL(BaseModel):
    """An alternate address for the *same* episode (e.g. a mirror host)."""

    url: str
    host: str = ""
    note: str | None = None


class Episode(BaseModel):
    index: int  # 1-based
    name: str
    url: str
    alts: list[AltURL] = Field(default_factory=list)

    def all_urls(self) -> list[str]:
        return [self.url] + [a.url for a in self.alts]


class Line(BaseModel):
    """A play line / source, mirroring MacCMS ``vod_play_from``."""

    name: str = "线路1"
    episodes: list[Episode] = Field(default_factory=list)

    @property
    def usable(self) -> bool:
        return len(self.episodes) > 0


class Vod(BaseModel):
    site_key: str
    vid: str  # upstream raw id, e.g. "ch4l5b3mn"
    sid: str = ""  # external, URL-safe stable id
    name: str
    year: int | None = None
    cat_raw: list[str] = Field(default_factory=list)
    cat_norm: str = "其他"
    remarks: str | None = None
    pic: str | None = None
    actor: str | None = None
    director: str | None = None
    area: str | None = None
    lang: str | None = None
    content: str | None = None
    lines: list[Line] = Field(default_factory=list)
    fingerprint: str = ""
    source_url: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def episode_count(self) -> int:
        return sum(len(line.episodes) for line in self.lines)

    @property
    def playable(self) -> bool:
        return any(line.usable for line in self.lines)

    def ensure_sid(self) -> str:
        if not self.sid:
            self.sid = compute_sid(self.site_key, self.vid)
        return self.sid


class Category(BaseModel):
    id: str
    name: str
    norm: str = "其他"


class SiteData(BaseModel):
    site_key: str
    site_name: str
    base_url: str
    adapter: str
    tier: int = 0
    collected_at: str = Field(default_factory=utcnow_iso)
    categories: list[Category] = Field(default_factory=list)
    vods: list[Vod] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def vod_count(self) -> int:
        return len(self.vods)

    @property
    def line_count(self) -> int:
        return sum(len(v.lines) for v in self.vods)

    @property
    def episode_count(self) -> int:
        return sum(v.episode_count for v in self.vods)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

_SID_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")


def is_sid_safe(vid: str) -> bool:
    return bool(vid) and len(vid) <= 40 and all(c in _SID_SAFE for c in vid)


def compute_sid(site_key: str, vid: str) -> str:
    """External id used in generated MacCMS payloads; must survive URL round-trips."""
    if is_sid_safe(vid):
        return vid
    return "V" + hashlib.sha1(f"{site_key}|{vid}".encode()).hexdigest()[:12]


def parse_play_url(play_url: str) -> list[Line]:
    """Parse a MacCMS ``vod_play_url`` string into normalized lines.

    Order matters and is non-negotiable: split lines on ``$$$``, then episodes on ``#``,
    and only then split each episode's name/url on the *first* ``$``.
    """
    lines: list[Line] = []
    if not play_url:
        return lines

    for raw_line in str(play_url).split("$$$"):
        episodes: list[Episode] = []
        for raw_ep in raw_line.split("#"):
            raw_ep = raw_ep.strip()
            if not raw_ep:
                continue
            name, _, url = raw_ep.partition("$")
            if not url:
                # No name part at all -- the whole token is the url.
                name, url = "", name
            url = url.strip()
            if not url:
                continue
            if not name.strip() or name.strip().startswith("http"):
                name = f"第{len(episodes) + 1:02d}集"
            episodes.append(Episode(index=len(episodes) + 1, name=name.strip(), url=url))
        lines.append(Line(name=f"线路{len(lines) + 1}", episodes=episodes))
    return lines


def build_play_url(lines: list[Line], expand_alts_as_lines: bool = True) -> tuple[str, str]:
    """Render normalized lines back into ``(vod_play_from, vod_play_url)``.

    Round-trip safe: episode names have ``$``/``#`` stripped and URLs get ``#`` escaped,
    because MacCMS uses those characters as delimiters.
    """

    def esc_name(s: str) -> str:
        return s.replace("$", "").replace("#", "").strip() or "播放"

    def esc_url(s: str) -> str:
        return s.replace("#", "%23").strip()

    froms: list[str] = []
    groups: list[str] = []

    for line in lines:
        if not line.episodes:
            continue
        froms.append(line.name or f"线路{len(froms) + 1}")
        groups.append("#".join(f"{esc_name(e.name)}${esc_url(e.url)}" for e in line.episodes))

        if expand_alts_as_lines:
            # Alternate hosts become extra switchable lines: this is how a T4 site's
            # `_pdf` fallback host list is surfaced to TVBox's "switch line" feature.
            alt_eps: dict[str, list[str]] = {}
            for ep in line.episodes:
                for alt in ep.alts:
                    alt_eps.setdefault(alt.note or "alts", []).append(
                        f"{esc_name(ep.name)}${esc_url(alt.url)}"
                    )
            for alt_name, eps in alt_eps.items():
                froms.append(f"{line.name}-{alt_name}")
                groups.append("#".join(eps))

    return "$$$".join(froms), "$$$".join(groups)
