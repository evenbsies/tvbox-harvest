"""Category normalization onto a small, TVBox-friendly bucket set.

Configured entirely by ``config/category_map.yaml``. Resolution runs in three ordered steps:

1. **Exact lookup** against the table.
2. **Suffix retry** -- strip a trailing ``片`` and look up again. One rule resolves a whole
   family of variant spellings (电影片→电影, 综艺片→综艺, 动漫片→动漫) instead of requiring
   every variant to be enumerated by hand.
3. **Ordered fallback rules** -- ``contains`` / ``endswith`` / ``startswith`` matchers.

The order of step 3 is load-bearing. Specific content-type checks must precede the broad
suffix rules, otherwise ``AI漫剧`` is swallowed by ``剧`` → 连续剧 and ``记录片`` is swallowed
by ``片`` → 电影, which are both wrong.

A third distinction matters for reporting: a category that matches **nothing** is a genuine
gap worth fixing, whereas a category explicitly listed under the ``其他`` bucket is a
deliberate "belongs nowhere in particular" decision. Only the former is counted as
unrecognised, so the run report stays actionable instead of filling with known-irrelevant
entries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import yaml

from harvest.config import get_settings

DEFAULT_STRIP_SUFFIXES = ("片",)


class CategoryDecision(NamedTuple):
    """Result of mapping a category name.

    Tuple-shaped (``[0]`` is the target) so existing call sites keep working, while
    ``.resolved`` answers the question the report actually needs.
    """

    target: str
    dropped_reason: str | None = None
    resolved: bool = False
    matched_by: str = "unknown"

    def __bool__(self) -> bool:
        # A decision is falsy only when nothing matched at all -- handy for `if not decision`.
        return self.resolved


@dataclass
class FallbackRule:
    match: str  # contains | endswith | startswith | equals
    pattern: str
    target: str

    def applies(self, key: str) -> bool:
        if self.match == "contains":
            return self.pattern in key
        if self.match == "endswith":
            return key.endswith(self.pattern)
        if self.match == "startswith":
            return key.startswith(self.pattern)
        if self.match == "equals":
            return key == self.pattern
        return False


@dataclass
class CategoryMap:
    rules: dict[str, list[str]]
    drop_categories: set[str] = field(default_factory=set)
    fallback_rules: list[FallbackRule] = field(default_factory=list)
    strip_suffixes: tuple[str, ...] = DEFAULT_STRIP_SUFFIXES
    unknown_policy: str = "其他"
    _lookup: dict[str, str] | None = None

    def build(self) -> CategoryMap:
        lookup: dict[str, str] = {}
        for target, names in self.rules.items():
            for name in names:
                # First target wins, so declaration order in the YAML acts as priority.
                lookup.setdefault(normalize_key(name), target)
        self._lookup = lookup
        return self

    # -- resolution -------------------------------------------------------------------

    def resolve_one(self, raw: str) -> CategoryDecision:
        key = normalize_key(raw)
        if not key:
            return CategoryDecision(self.unknown_policy, None, False, "empty")

        if key in self.drop_categories:
            return CategoryDecision(self.unknown_policy, f"drop_category:{raw}", True, "drop")

        target = (self._lookup or {}).get(key)
        if target:
            return CategoryDecision(target, None, True, "exact")

        for suffix in self.strip_suffixes:
            if key.endswith(suffix) and len(key) > len(suffix):
                trimmed = key[: -len(suffix)]
                if trimmed in self.drop_categories:
                    return CategoryDecision(self.unknown_policy, f"drop_category:{raw}", True, "drop")
                target = (self._lookup or {}).get(trimmed)
                if target:
                    return CategoryDecision(target, None, True, f"strip:{suffix}")

        for rule in self.fallback_rules:
            if rule.applies(key):
                return CategoryDecision(rule.target, None, True, f"rule:{rule.match}:{rule.pattern}")

        return CategoryDecision(self.unknown_policy, None, False, "unknown")

    def resolve(self, raw_names: list[str]) -> CategoryDecision:
        """Resolve a list of raw names; the first decision that resolves wins.

        A drop always takes precedence -- if any name in the list is a drop category, the item
        is dropped even when another name maps cleanly. Being conservative here is correct: the
        cost of one wrongly-kept adult title is higher than one wrongly-dropped film.
        """
        if not raw_names:
            return CategoryDecision(self.unknown_policy, None, False, "empty")

        decisions = [self.resolve_one(str(raw)) for raw in raw_names if str(raw).strip()]
        if not decisions:
            return CategoryDecision(self.unknown_policy, None, False, "empty")

        for decision in decisions:
            if decision.dropped_reason:
                return decision
        for decision in decisions:
            if decision.resolved and decision.target != self.unknown_policy:
                return decision
        for decision in decisions:
            if decision.resolved:
                return decision
        return decisions[0]

    # -- back-compat shims ------------------------------------------------------------

    def norm(self, raw_names: list[str]) -> CategoryDecision:
        """Deprecated alias for :meth:`resolve`; kept because it reads better at call sites."""
        return self.resolve(raw_names)

    def is_dropped(self, raw_names: list[str]) -> str | None:
        return self.resolve(raw_names).dropped_reason


def normalize_key(value: str) -> str:
    """Case/whitespace-insensitive key for category comparison."""
    return "".join(str(value or "").split()).lower()


@lru_cache(maxsize=1)
def load_category_map(path: str | None = None) -> CategoryMap:
    cfg_path = Path(path) if path else get_settings().config_dir / "category_map.yaml"
    data: dict = {}
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    rules = {
        str(k): [str(x) for x in (v or [])]
        for k, v in (data.get("rules") or {}).items()
    }
    drops = {normalize_key(x) for x in (data.get("drop_categories") or [])}

    fallbacks: list[FallbackRule] = []
    for entry in data.get("fallback_rules") or []:
        if not isinstance(entry, dict):
            continue
        match = str(entry.get("match") or "contains").lower()
        pattern = str(entry.get("pattern") or "")
        target = str(entry.get("target") or "")
        if pattern and target:
            fallbacks.append(FallbackRule(match=match, pattern=normalize_key(pattern), target=target))

    suffixes = data.get("strip_suffixes")
    strip_suffixes = (
        tuple(normalize_key(s) for s in suffixes if str(s).strip())
        if isinstance(suffixes, list)
        else DEFAULT_STRIP_SUFFIXES
    )

    return CategoryMap(
        rules=rules,
        drop_categories=drops,
        fallback_rules=fallbacks,
        strip_suffixes=strip_suffixes or DEFAULT_STRIP_SUFFIXES,
        unknown_policy=str(data.get("unknown_policy") or "其他"),
    ).build()


def reload_category_map() -> None:
    load_category_map.cache_clear()
