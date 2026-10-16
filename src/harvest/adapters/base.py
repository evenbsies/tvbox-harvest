"""Adapter interface.

The contract is deliberately small. An adapter must be able to:

1. ``collect`` -- walk a site and produce a normalized :class:`~harvest.models.SiteData`.
2. ``build_tvbox_site`` -- describe how that site should appear in a TVBox config.

Everything site-specific lives in the recipe, everything *type*-specific lives in the
subclass. There is no third place.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable

from harvest.config import Settings, get_settings
from harvest.fetch.http import HttpClient, RawRecorder
from harvest.models import SiteConfig, SiteData


@dataclass
class CollectContext:
    """Per-run budget and bookkeeping handed to every adapter."""

    settings: Settings = field(default_factory=get_settings)
    http: HttpClient | None = None
    recorder: RawRecorder | None = None
    mode: str = "full"  # full | incremental | probe
    since_hours: int | None = None
    max_titles: int | None = None
    max_requests: int | None = None
    deadline: float | None = None
    offline: bool = False
    page_html_sample: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.max_titles = self.max_titles or self.settings.max_titles_per_run
        self.max_requests = self.max_requests or self.settings.max_requests_per_site
        self.offline = self.offline or self.settings.offline

    @property
    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline

    def note_error(self, where: str, message: str) -> None:
        self.errors.append({"where": where, "error": str(message)[:300]})

    def require_http(self) -> HttpClient:
        if self.http is None:
            raise RuntimeError("CollectContext.http is not set")
        return self.http


@runtime_checkable
class Adapter(Protocol):
    kind: ClassVar[str]
    tier: ClassVar[int]

    async def collect(self, site: SiteConfig, ctx: CollectContext) -> SiteData: ...

    def build_tvbox_site(self, site: SiteConfig, data: SiteData | None) -> dict[str, Any]: ...


_REGISTRY: dict[str, type] = {}


def register(cls: type) -> type:
    kind = getattr(cls, "kind", None)
    if not kind:
        raise ValueError(f"{cls.__name__} must define a class-level `kind`")
    _REGISTRY[kind] = cls
    return cls


def get_adapter_class(kind: str) -> type | None:
    return _REGISTRY.get(kind)


def available_adapters() -> list[str]:
    return sorted(_REGISTRY)


def load_builtin_adapters() -> None:
    """Import the built-in adapters so their decorators run."""
    from harvest.adapters import html_ssr, json_custom, maccms_v10, playwright_sniff  # noqa: F401


def instantiate(site: SiteConfig, ctx: CollectContext | None = None):
    adapter_cls = get_adapter_class(site.adapter)
    if adapter_cls is None:
        load_builtin_adapters()
        adapter_cls = get_adapter_class(site.adapter)
    if adapter_cls is None:
        raise ValueError(f"no adapter registered for kind={site.adapter!r}")
    return adapter_cls()


def empty_site_data(site: SiteConfig, adapter: str, tier: int) -> SiteData:
    return SiteData(
        site_key=site.key,
        site_name=site.name,
        base_url=site.base_url,
        adapter=adapter,
        tier=tier,
    )
