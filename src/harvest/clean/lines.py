"""Keep only the play lines the caller asked for.

Why this exists: a MacCMS-style detail page usually offers several *play sources* for the same
title -- 闪电, 非凡, 量子, sometimes a line labelled `4K` or `4K高清`. They are separate streams of
the same film, not separate films. Harvesting all of them multiplies the work for no benefit when
the intent is "only the 4K one", and it makes the subscription harder to use: a client shows four
entries for one movie and the user has to guess.

The rule is a name match, deliberately:

* Names come from the source (`vod_play_from` on Tier 1, tab labels elsewhere). They are what a
  user reads in the player, so "4K line" means exactly what it says on screen.
* A line whose name is unknown is **not** assumed to be 4K. Filtering for `4K` on a site that
  does not label its lines returns nothing, and that is the correct answer -- inventing a match
  from the media URL or the title's `remarks` would be a different claim wearing the same word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from harvest.models import SiteData


def _compile(patterns: list[str] | tuple[str, ...]) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for raw in patterns:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            compiled.append(re.compile(text, re.IGNORECASE))
        except re.error:
            # A literal like "4K(" must not take the whole run down; treat it as a substring.
            compiled.append(re.compile(re.escape(text), re.IGNORECASE))
    return compiled


@dataclass
class LineFilterResult:
    """What the filter did, in enough detail to explain the shortfall."""

    kept_titles: int = 0
    dropped_lines: int = 0
    dropped_titles: list[str] = field(default_factory=list)
    #: name -> number of titles offering it, after filtering. Empty names are recorded as "—".
    names: dict[str, int] = field(default_factory=dict)
    #: What the site offered *before* filtering. This is what makes "kept nothing" explainable:
    #: the report can say which lines exist instead of leaving the operator to guess.
    observed: dict[str, int] = field(default_factory=dict)

    @property
    def dropped_title_count(self) -> int:
        return len(self.dropped_titles)


def line_names(data: SiteData) -> dict[str, int]:
    """How often each line name appears across the catalogue (before any filtering)."""
    names: dict[str, int] = {}
    for vod in data.vods:
        for line in vod.lines:
            key = (line.name or "").strip() or "—"
            names[key] = names.get(key, 0) + 1
    return dict(sorted(names.items(), key=lambda kv: kv[1], reverse=True))


def filter_lines(
    data: SiteData,
    *,
    keep: list[str] | tuple[str, ...] = (),
    drop: list[str] | tuple[str, ...] = (),
) -> LineFilterResult:
    """Drop non-matching lines in place; a title with no line left is dropped from the catalogue.

    `keep` empty means "no positive filter" (only `drop` applies). Both are case-insensitive
    regular expressions, falling back to a literal match when the pattern does not compile.
    """
    keep_res = _compile(list(keep))
    drop_res = _compile(list(drop))
    result = LineFilterResult()
    if not keep_res and not drop_res:
        result.kept_titles = data.vod_count
        result.names = line_names(data)
        result.observed = dict(result.names)
        return result

    result.observed = line_names(data)

    survivors = []
    for vod in data.vods:
        kept_here = []
        for line in vod.lines:
            name = (line.name or "").strip()
            if keep_res and not any(rx.search(name) for rx in keep_res):
                result.dropped_lines += 1
                continue
            if drop_res and any(rx.search(name) for rx in drop_res):
                result.dropped_lines += 1
                continue
            kept_here.append(line)
            key = name or "—"
            result.names[key] = result.names.get(key, 0) + 1
        if not kept_here:
            result.dropped_titles.append(vod.name or vod.vid)
            continue
        vod.lines = kept_here
        survivors.append(vod)

    data.vods = survivors
    result.kept_titles = len(survivors)
    result.names = dict(sorted(result.names.items(), key=lambda kv: kv[1], reverse=True))
    return result
