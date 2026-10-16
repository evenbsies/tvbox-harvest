"""Signal library for tier detection.

Every function here is a pure observation: given already-fetched bytes, what does this
tell us about the site's shape? No network access, no side effects -- which is what makes
the tier decision regression-testable against recorded fixtures.

Signals are grouped by what they *prove*:

* MacCMS response envelope  -> the site speaks the standard interface (Tier 1)
* structured JSON w/ video-ish fields -> a custom interface exists (Tier 2)
* many same-pattern detail links + player container -> HTML scraping can work (Tier 3)
* player container but zero static media URLs -> runtime-only, needs a browser (Tier 4)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

# --- fingerprints ---------------------------------------------------------------------

GO_DEFAULT_404 = "404 page not found"
PHP_HINTS = ("x-powered-by", "phpsessid", "think_", "phpsid")
MACCMS_MARKERS = (
    "maccms", "苹果cms", "mac_cms", "provide/vod", "vod_play_url", "vod_play_from",
    "player_aaaa", "template/mxpro", "template/conch", "template/dsnake",
)

API_HINT_RE = re.compile(
    r"""["'\s(=]               # delimiter
        (/?[A-Za-z0-9_\-./]{0,60}      # optional prefix path
         (?:provide/vod|provide/art|api\.php(?:\?[A-Za-z0-9_\-=&%.]{0,40})?)
        )""",
    re.VERBOSE | re.IGNORECASE,
)
JSON_ENDPOINT_RE = re.compile(
    r"""["'](/[A-Za-z0-9_\-./]{2,80}/(?:api|data|ajax|json|list|vod|search)[A-Za-z0-9_\-./]{0,40})["']""",
    re.IGNORECASE,
)
M3U8_RE = re.compile(r"https?://[^\s\"'<>\\]{0,300}\.m3u8", re.IGNORECASE)
M3U8_ANY_RE = re.compile(r"\.m3u8", re.IGNORECASE)
HREF_RE = re.compile(r"""href=["']([^"'#]+)["']""", re.IGNORECASE)
PLAYER_CONTAINER_MARKERS = (
    "data-episode", "episodeManager", "episode_manager", "player_aaaa", "artplayer",
    "dplayer", "hls.js", "hls.min.js", "video-player", "playlist-btn", "mac_player",
    "player-wrapper", "data-line=", "plexplayer",
)
SPA_MARKERS = ("<div id=\"app\">", "<div id=\"root\">", "window.__nuxt__", "window.__next_f",
               "__NEXT_DATA__", "id=\"__nuxt\"")

# Episode-structure regexes. These belong here rather than in the Tier-4 adapter because the
# *structure* is static even on sites whose addresses are not -- the Tier-3 adapter, the Tier-4
# adapter and the probe all need to read the same episode scaffolding.
EPISODE_ATTR_RE = re.compile(r"""data-episode\s*=\s*["']?(\d+)""", re.IGNORECASE)
LINE_ATTR_RE = re.compile(r"""data-line\s*=\s*["']?(\d+)""", re.IGNORECASE)
EPISODE_COUNT_RE = re.compile(r"""episodeCount\s*:\s*(\d+)""", re.IGNORECASE)
LINE_NAME_RE = re.compile(r"""lineName\s*:\s*["']([^"']{1,60})["']""", re.IGNORECASE)

# Field dictionary used to score unknown JSON payloads. Weight reflects how specific the
# field name is to a video catalogue (higher == more diagnostic).
VIDEO_FIELD_WEIGHTS = {
    "vod_id": 3, "vod_name": 3, "vod_play_url": 4, "vod_play_from": 4,
    "vod_time": 2, "vod_pic": 2, "vod_remarks": 2, "vod_content": 2, "vod_year": 1,
    "type_id": 1, "type_name": 2, "type_pid": 1,
    "episodes": 3, "playlist": 2, "play_list": 2, "play_url": 3, "playurl": 3,
    "sources": 2, "source": 1, "m3u8": 3, "url": 1, "name": 1, "title": 1,
    "items": 1, "list": 1, "data": 1, "total": 1, "page": 1,
}
VIDEO_STRONG_FIELDS = {"vod_play_url", "play_url", "playurl", "m3u8", "vod_play_from", "episodes"}


@dataclass
class LinkCluster:
    pattern: str
    count: int
    samples: list[str] = field(default_factory=list)

    @property
    def dominant(self) -> bool:
        return self.count >= 20


@dataclass
class DetectedPage:
    url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""

    @property
    def server(self) -> str:
        return self.headers.get("server", "")

    @property
    def is_go_404(self) -> bool:
        return self.status == 404 and GO_DEFAULT_404 in self.body[:200]


def looks_like_html(body: str) -> bool:
    head = body.lstrip()[:400].lower()
    return head.startswith("<!doctype html") or head.startswith("<html") or "<head" in head


def count_m3u8(body: str) -> int:
    return len(M3U8_RE.findall(body))


def extract_m3u8_urls(body: str) -> list[str]:
    return list(dict.fromkeys(M3U8_RE.findall(body)))


def extract_api_hints(body: str) -> list[str]:
    """Pull candidate MacCMS-style interface paths out of page markup."""
    hints: list[str] = []
    for match in API_HINT_RE.finditer(body):
        raw = match.group(1)
        if not raw.startswith("/"):
            raw = "/" + raw
        if raw not in hints:
            hints.append(raw)
    return hints


def extract_json_candidates(body: str) -> list[str]:
    seen: list[str] = []
    for match in JSON_ENDPOINT_RE.finditer(body):
        raw = match.group(1)
        if raw not in seen:
            seen.append(raw)
    # Also catch fully-qualified endpoints.
    for match in re.finditer(r"""["'](https?://[A-Za-z0-9_\-.:/]{5,120}/api/[A-Za-z0-9_\-./]{0,50})["']""", body):
        raw = match.group(1)
        if raw not in seen:
            seen.append(raw)
    return seen


def cluster_detail_links(body: str, base_url: str) -> list[LinkCluster]:
    """Group same-shaped detail-page links, e.g. ``/play/{id}`` or ``/voddetail/{id}.html``.

    Returns clusters sorted by frequency. A dominant cluster (>=20 hits) is strong evidence
    the site exposes a browsable catalogue we can walk.
    """
    buckets: dict[str, LinkCluster] = {}
    for href in HREF_RE.findall(body):
        href = href.strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "data:")):
            continue
        path = urlparse(urljoin(base_url, href)).path
        if not path or path == "/":
            continue
        pattern = _generalize_path(path)
        if pattern is None:
            continue
        bucket = buckets.setdefault(pattern, LinkCluster(pattern=pattern, count=0))
        bucket.count += 1
        if len(bucket.samples) < 5:
            bucket.samples.append(urljoin(base_url, href))
    return sorted(buckets.values(), key=lambda c: c.count, reverse=True)


def _generalize_path(path: str) -> str | None:
    """Turn ``/play/ch4l5b3mn`` into ``/play/{id}``; drop static assets and noise."""
    if re.search(r"\.(css|js|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|mp4|m3u8|json|xml|txt)$", path, re.I):
        return None
    if path.startswith(("/static/", "/assets/", "/_next/", "/img/", "/images/", "/css/", "/js/")):
        return None
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    generalized: list[str] = []
    for part in parts:
        if part.isdigit():
            generalized.append("{n}")
        elif re.fullmatch(r"[A-Za-z0-9_-]{4,24}", part) and re.search(r"\d", part):
            generalized.append("{id}")
        elif re.fullmatch(r"[A-Za-z0-9_-]{1,60}\.html?", part):
            generalized.append(re.sub(r"^[A-Za-z0-9_-]{1,40}(?=\.html?$)", "{slug}", part))
        else:
            generalized.append(part)
    return "/" + "/".join(generalized)


def has_player_container(body: str) -> list[str]:
    low = body.lower()
    return [m for m in PLAYER_CONTAINER_MARKERS if m.lower() in low]


def is_spa_skeleton(body: str) -> bool:
    low = body.lower()
    hits = [m for m in SPA_MARKERS if m.lower() in low]
    if not hits:
        return False
    # A SPA shell is tiny relative to the JS it loads.
    script_count = low.count("<script")
    return script_count >= 2 and len(body) < 30_000


def maccms_markers(body: str) -> list[str]:
    low = body.lower()
    return [m for m in MACCMS_MARKERS if m in low]


def is_maccms_class_list(payload: object) -> bool:
    """True when payload looks like MacCMS ``?ac=list`` / ``?ac=class`` output."""
    if not isinstance(payload, dict):
        return False
    if "list" not in payload or not isinstance(payload.get("list"), list):
        return False
    has_envelope = any(k in payload for k in ("code", "total", "pagecount", "page", "msg"))
    if not has_envelope:
        return False
    items = payload["list"]
    if items and isinstance(items[0], dict):
        first = items[0]
        if any(k in first for k in ("vod_id", "vod_name", "type_id", "type_name")):
            return True
    # An empty list is still valid when the envelope + class table are present.
    return isinstance(payload.get("class"), list) and len(payload.get("class") or []) > 0


def is_maccms_detail(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    items = payload.get("list")
    if not isinstance(items, list) or not items:
        return False
    first = items[0]
    if not isinstance(first, dict):
        return False
    return "vod_play_url" in first or "vod_play_from" in first


def flatten_keys(obj: object, prefix: str = "", depth: int = 0, max_depth: int = 4) -> list[str]:
    keys: list[str] = []
    if depth > max_depth:
        return keys
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.append(f"{prefix}{k}")
            keys.extend(flatten_keys(v, f"{prefix}{k}.", depth + 1, max_depth))
    elif isinstance(obj, list) and obj:
        keys.extend(flatten_keys(obj[0], f"{prefix}[]." , depth + 1, max_depth))
    return keys


@dataclass
class FieldScore:
    score: int
    strong_fields: list[str]
    sample_paths: dict[str, str]

    @property
    def promising(self) -> bool:
        return self.score >= 6 or bool(self.strong_fields)


def field_fingerprint(payload: object) -> FieldScore:
    """Score an arbitrary JSON payload against the video-field dictionary.

    Returns a score plus the concrete paths where each recognized field was found, which
    becomes the starting ``fields:`` mapping handed to a human (or straight to the
    ``json_custom`` adapter when the payload is already standard-shaped).
    """
    score = 0
    strong: list[str] = []
    paths: dict[str, str] = {}
    for key in flatten_keys(payload):
        leaf = key.rsplit(".", 1)[-1].lower()
        weight = VIDEO_FIELD_WEIGHTS.get(leaf)
        if not weight:
            continue
        if key in paths:
            continue
        paths[key] = key
        score += weight
        if leaf in VIDEO_STRONG_FIELDS:
            strong.append(key)
    return FieldScore(score=score, strong_fields=strong, sample_paths=paths)


def decode_base64_global(body: str, var_name: str) -> list[str]:
    """Read a ``window.<var> = "<base64>"`` global and decode it to a hostname list."""
    from harvest.clean.gambling import decode_base64_host_list

    pattern = re.compile(rf"""window\.{re.escape(var_name)}\s*=\s*["']([A-Za-z0-9+/=_\-]{{8,}})["']""")
    match = pattern.search(body)
    if not match:
        return []
    return decode_base64_host_list(match.group(1))


def safe_json_loads(text: str) -> object | None:
    try:
        return json.loads(text)
    except Exception:
        return None
