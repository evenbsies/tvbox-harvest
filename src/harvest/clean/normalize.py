"""URL / title normalization.

The single most important distinction in this module is **resource-page URL vs media URL**.
They need opposite treatment:

* A resource page URL is for *us* -- de-tracking it improves de-duplication.
* A media URL is for *the player* -- and it is frequently signed. Reordering its query
  parameters or dropping "tracking-looking" ones silently breaks playback. So the media
  normalizer only ever touches the case, the default port and the fragment.
"""

from __future__ import annotations

import html
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from harvest.models import Vod

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "spm", "scm", "share_source", "share_medium", "share_token", "share_code",
    "from", "fromsource", "invite", "inviter", "ref", "referer", "referrer",
    "weibo_id", "tt_from", "wx_from", "_t", "timestamp", "ts", "callback", "jsonp",
}

DEFAULT_PORTS = {"http": "80", "https": "443", "ftp": "21"}

# Quality / marketing noise stripped before fingerprinting a title.
QUALITY_TAGS = [
    r"HD", r"BD", r"TS", r"TC", r"CAM", r"DVDRip", r"WEB-?DL", r"HDRip", r"BluRay",
    r"4K", r"8K", r"2160P", r"1080P", r"720P", r"480P",
    r"蓝光", r"超清", r"高清", r"枪版", r"抢先版", r"清晰版", r"无删减", r"未删减",
    r"完整版", r"加长版", r"导演剪辑版", r"中字", r"中文字幕", r"国语", r"粤语", r"双语",
    r"原声", r"英语版", r"日语版", r"韩语版", r"更新至\s*\d+\s*[集话]",
    r"\d+\s*集全", r"全\s*\d+\s*集", r"第[一二三四五六七八九十\d]+季",
    r"HDTV", r"DVD", r"MP4", r"正片", r"预告片", r"花絮",
]
_QUALITY_RE = re.compile("|".join(QUALITY_TAGS), re.IGNORECASE)
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_YEAR_PAREN_RE = re.compile(r"[（(\[【]\s*((?:19|20)\d{2})\s*[)）\]】]")
_TAG_BRACKET_RE = re.compile(r"[（(\[【]([^）)\]】]{0,12})[)）\]】]")


def unescape(text: str | None) -> str:
    return html.unescape(text or "")


# --------------------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------------------


def _base_parse(url: str):
    url = unescape(url).strip()
    try:
        return urlparse(url)
    except ValueError:
        return None


def canon_resource_url(url: str) -> str:
    """Canonical form of a page URL, for de-duplication. Safe to reorder/filter params."""
    parsed = _base_parse(url)
    if not parsed or not parsed.scheme:
        return url.strip()

    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not host:
        return url.strip()
    port = parsed.port
    netloc = host if port in (None, int(DEFAULT_PORTS.get(scheme, "0"))) else f"{host}:{port}"

    pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    query = urlencode(sorted(pairs), doseq=True)

    path = re.sub(r"/{2,}", "/", parsed.path) or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, parsed.params, query, ""))


def canon_media_url(url: str) -> str:
    """Canonical form of a media URL. Deliberately minimal -- never trust us to rewrite a
    signed URL correctly, so only case/port/fragment are touched."""
    parsed = _base_parse(url)
    if not parsed or not parsed.scheme:
        return url.strip()

    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not host:
        return url.strip()
    port = parsed.port
    netloc = host if port in (None, int(DEFAULT_PORTS.get(scheme, "0"))) else f"{host}:{port}"
    # Fragment dropped: HLS uses it for player-only hints, not for retrieval.
    return urlunparse((scheme, netloc, parsed.path, parsed.params, parsed.query, ""))


def host_of(url: str) -> str:
    parsed = _base_parse(url)
    return (parsed.hostname or "").lower() if parsed else ""


def swap_host(url: str, new_host: str) -> str:
    """Replace the host of a URL, preserving scheme, port-less netloc, path and query."""
    parsed = _base_parse(url)
    if not parsed:
        return url
    return urlunparse(
        (parsed.scheme, new_host, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )


_BAD_URL_TOKENS = ("null", "undefined", "example.com", "0.0.0.0", "localhost", "127.0.0.1", "javascript:")


def is_plausible_media_url(url: str) -> bool:
    """Layer 0/1 of the address filter: syntactic validity + obvious taint."""
    if not url or len(url) > 2000:
        return False
    low = url.strip().lower()
    if not low.startswith(("http://", "https://", "rtsp://", "rtmp://")):
        return False
    if any(token in low for token in _BAD_URL_TOKENS):
        return False
    return bool(host_of(url))


# --------------------------------------------------------------------------------------
# Titles
# --------------------------------------------------------------------------------------


def normalize_title(title: str | None) -> str:
    """Reduce a title to a comparable key: full-width folded, tags stripped, lowercased."""
    if not title:
        return ""
    text = unicodedata.normalize("NFKC", unescape(title))
    text = text.replace("\u3000", " ")
    text = _QUALITY_RE.sub(" ", text)
    # Bracketed segments that contain nothing but tags/short noise
    text = _TAG_BRACKET_RE.sub(
        lambda m: " " if len(m.group(1).strip()) <= 6 and not _YEAR_RE.search(m.group(1)) else m.group(0),
        text,
    )
    text = re.sub(r"[\s·・\-_—|/\\]+", " ", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip().lower()


def extract_year(*candidates: str | None, fallback_text: str | None = None) -> int | None:
    """Year extraction, in priority order: explicit field, then bracketed, then bare."""
    for cand in candidates:
        if not cand:
            continue
        m = _YEAR_RE.search(str(cand))
        if m:
            year = int(m.group(0))
            if 1900 <= year <= 2100:
                return year
    if fallback_text:
        m = _YEAR_PAREN_RE.search(unescape(fallback_text))
        if m:
            return int(m.group(1))
        m = _YEAR_RE.search(fallback_text)
        if m:
            year = int(m.group(0))
            if 1900 <= year <= 2100:
                return year
    return None


def fingerprint(title: str | None, year: int | None) -> str:
    import hashlib

    key = f"{normalize_title(title)}|{year or ''}"
    return hashlib.sha1(key.encode()).hexdigest()


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t\u00a0]+", " ", html.unescape(text)).strip()


def apply_title_normalization(vod: Vod) -> Vod:
    """Fill in ``fingerprint``/``year`` for a harvested item."""
    if vod.year is None:
        vod.year = extract_year(fallback_text=vod.name)
    vod.name = unescape(vod.name).strip()
    vod.fingerprint = fingerprint(vod.name, vod.year)
    vod.content = strip_html(vod.content) or None
    vod.pic = unescape(vod.pic).strip() if vod.pic else None
    return vod
