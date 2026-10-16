"""A tiny extraction DSL shared by the ``html_ssr`` adapter and the probe.

Why a DSL instead of hard-coded parsers: every HTML site needs slightly different
selectors, but they need the *same handful of operations*. Declaring those operations in
the site recipe keeps site knowledge out of code, and means adding a Tier-3 site is a
YAML edit rather than a Python edit.

Supported rule types:

``css`` / ``css_list``
    CSS selector with optional attribute/text extraction. Values may be:
    ``"::text"``, ``"::html"``, ``"::attr(href)"`` or a bare attribute name.
``regex``
    Regular expression with capture group 1 (or a named ``group``).
``json_regex``
    Find a JSON blob inside the HTML via regex, then read a dotted path from it.
``json_ld``
    Read a ``<script type="application/ld+json">`` block and read a dotted path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from harvest.probe.signals import safe_json_loads

_JS_URL_RE = re.compile(r"""["'](https?://[^"'\\\s]{0,400}?(?:\.m3u8|/index\.m3u8)[^"'\\\s]{0,200})["']""")
_BARE_M3U8_RE = re.compile(r"""(?:url|src|file)\s*[:=]\s*["']([^"']{0,400}\.m3u8[^"']{0,200})["']""")


def parse_html(html: str):
    """Parse HTML with selectolax, preferring the lexbor backend.

    selectolax 1.0 deprecated (and hard-fails on) the old `modest` parser, and lexbor is also
    the more standards-correct of the two -- which matters here because the sites we parse are
    frequently malformed in creative ways. The import is deferred so the module can still be
    imported for pure-regex work when the parser is unavailable.
    """
    try:
        from selectolax.lexbor import LexborHTMLParser

        return LexborHTMLParser(html)
    except Exception:
        from selectolax.parser import HTMLParser

        return HTMLParser(html)


@dataclass
class ExtractResult:
    values: list[str] = field(default_factory=list)
    pairs: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.values or self.pairs)


def parse_selector_value(spec: str) -> tuple[str, str]:
    """Split ``".poster img::attr(src)"`` into ``(".poster img", "attr:src")``."""
    if "::attr(" in spec:
        selector, _, rest = spec.partition("::attr(")
        return selector.strip(), "attr:" + rest.rstrip(")").strip()
    if spec.endswith("::text"):
        return spec[: -len("::text")].strip(), "text"
    if spec.endswith("::html"):
        return spec[: -len("::html")].strip(), "html"
    return spec.strip(), "text"


def node_text(node: Any) -> str:
    """Read a node's text, tolerating both selectolax APIs.

    selectolax 1.0 renamed ``text_content()`` to ``text()`` and made ``html`` a plain string
    rather than a method. Since a parser upgrade must not silently turn every extracted value
    into an empty string, both spellings are supported explicitly.
    """
    getter = getattr(node, "text", None)
    if callable(getter):
        try:
            return (getter(deep=True) or "").strip()
        except TypeError:
            try:
                return (getter() or "").strip()
            except TypeError:
                pass
    legacy = getattr(node, "text_content", None)
    if callable(legacy):
        try:
            return (legacy() or "").strip()
        except Exception:
            return ""
    return ""


def node_html(node: Any) -> str:
    value = getattr(node, "html", None)
    if callable(value):
        try:
            return (value() or "").strip()
        except Exception:
            return ""
    return value.strip() if isinstance(value, str) else ""


def node_attributes(node: Any) -> dict[str, str]:
    attrs = getattr(node, "attributes", None)
    if isinstance(attrs, dict):
        return {str(k): str(v) for k, v in attrs.items()}
    return {}


def _node_value(node: Any, mode: str) -> str:
    if mode == "text":
        return node_text(node)
    if mode == "html":
        return node_html(node)
    if mode.startswith("attr:"):
        return (node_attributes(node).get(mode[5:]) or "").strip()
    return node_text(node)


def select_all(html: str, selector: str, mode: str = "text") -> list[str]:
    tree = parse_html(html)
    out: list[str] = []
    try:
        nodes = tree.css(selector)
    except Exception:
        return out
    for node in nodes:
        value = _node_value(node, mode)
        if value:
            out.append(value)
    return out


def select_one(html: str, selector: str, mode: str = "text") -> str:
    values = select_all(html, selector, mode)
    return values[0] if values else ""


def normalize_value_spec(spec: str) -> str:
    """Normalize a recipe value spec into one of ``text`` / ``html`` / ``attr:<name>``."""
    spec = (spec or "").strip()
    if not spec or spec == "::text":
        return "text"
    if spec == "::html":
        return "html"
    if spec.startswith("::attr(") and spec.endswith(")"):
        return "attr:" + spec[len("::attr(") : -1].strip()
    if spec.startswith("::attr:"):
        return "attr:" + spec[len("::attr:") :].strip()
    return "attr:" + spec


def apply_rule(html: str, rule: dict[str, Any], context: dict[str, Any] | None = None) -> ExtractResult:
    """Execute one declarative extraction rule against a document."""
    context = context or {}
    kind = str(rule.get("type") or "css").lower()
    result = ExtractResult()

    if kind in ("css", "css_list"):
        selector = _subst(str(rule.get("selector") or ""), context)
        if not selector:
            return result

        try:
            nodes = parse_html(html).css(selector)
        except Exception as exc:
            result.notes.append(f"css failed for {selector!r}: {exc}")
            return result

        if rule.get("name") is not None or rule.get("url") is not None:
            name_mode = normalize_value_spec(str(rule.get("name") or "::text"))
            url_mode = normalize_value_spec(str(rule.get("url") or "::attr(href)"))
            for node in nodes:
                url = _node_value(node, url_mode)
                if not url:
                    continue
                result.pairs.append({"name": _node_value(node, name_mode), "url": url})
            return result

        mode = normalize_value_spec(str(rule.get("value") or rule.get("attr") or "::text"))
        values = [v for v in (_node_value(n, mode) for n in nodes) if v]
        result.values = values[:1] if kind == "css" else values
        return result

    if kind == "regex":
        pattern = str(rule.get("pattern") or "")
        if not pattern:
            return result
        try:
            compiled = re.compile(pattern, re.IGNORECASE | re.DOTALL)
        except re.error as exc:
            result.notes.append(f"bad regex: {exc}")
            return result
        group = rule.get("group", 1)
        for match in compiled.finditer(html):
            try:
                if isinstance(group, int):
                    value = match.group(group)
                else:
                    value = match.groupdict().get(str(group), "")
            except (IndexError, AttributeError, TypeError, KeyError):
                continue
            if value:
                result.values.append(value.strip())
        result.values = result.values[: int(rule.get("limit") or 2000)]
        return result

    if kind == "json_regex":
        blob = _find_json_blob(html, str(rule.get("pattern") or ""))
        if blob is None:
            return result
        path = str(rule.get("path") or rule.get("url_path") or rule.get("url_field") or "")
        if path:
            result.values.extend(_scalars(jp_get(blob, _as_path(path))))
        else:
            result.values.extend(_collect_m3u8_from_obj(blob))
        return result

    if kind == "json_ld":
        for raw in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',
            html,
            re.IGNORECASE,
        ):
            obj = safe_json_loads(raw.strip())
            if obj is not None:
                result.values.extend(_scalars(jp_get(obj, _as_path(str(rule.get("path") or "")))))
        return result

    result.notes.append(f"unknown rule type: {kind}")
    return result


def _subst(text: str, context: dict[str, Any]) -> str:
    for key, value in context.items():
        text = text.replace("{" + str(key) + "}", str(value))
    return text


def _find_json_blob(html: str, pattern: str) -> Any | None:
    if pattern:
        try:
            match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        except re.error:
            match = None
        if match:
            candidate = match.group(1) if match.groups() else match.group(0)
            obj = safe_json_loads(candidate.strip())
            if obj is not None:
                return obj
    return None


def _collect_m3u8_from_obj(obj: Any, acc: list[str] | None = None, depth: int = 0) -> list[str]:
    acc = acc if acc is not None else []
    if depth > 6:
        return acc
    if isinstance(obj, dict):
        for value in obj.values():
            _collect_m3u8_from_obj(value, acc, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _collect_m3u8_from_obj(item, acc, depth + 1)
    elif isinstance(obj, str) and ".m3u8" in obj:
        acc.append(obj)
    return acc


def _as_path(path: str) -> str:
    """Accept both ``$.a.b`` and ``a.b`` spellings in recipes."""
    path = (path or "").strip()
    return path if path.startswith("$") else "$." + path


def _scalars(values: list[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
        elif isinstance(value, (int, float)):
            out.append(str(value))
    return out


def _collect_path(obj: Any, path: str) -> list[str]:
    """Kept for recipes written against the earlier dotted-path form."""
    return _scalars(jp_get(obj, _as_path(path)))


def scan_m3u8_literals(html: str) -> list[str]:
    """Sweep raw HTML/JS for media URLs. Used by the probe as a cheap Tier-3 test."""
    found: list[str] = []
    for pattern in (_JS_URL_RE, _BARE_M3U8_RE):
        for match in pattern.findall(html):
            value = match if isinstance(match, str) else match[0]
            if value not in found:
                found.append(value)
    return found


@dataclass
class EpisodeCandidate:
    name: str
    url: str


def try_extract_episodes(html: str, selectors: dict[str, Any] | None = None) -> list[EpisodeCandidate]:
    """Best-effort episode extraction used by the probe to decide Tier 3 vs Tier 4."""
    rules: list[dict[str, Any]] = []
    if selectors:
        detail = selectors.get("detail") or {}
        rules.extend(detail.get("episodes") or [])
    rules.append({"type": "json_regex", "pattern": r"player_aaaa\s*=\s*(\{[\s\S]{0,4000}?\})\s*</script>"})
    rules.append({"type": "json_regex", "pattern": r"(__NUXT__|__INITIAL_STATE__)\s*=\s*(\{[\s\S]{0,4000}?\})\s*[;<]"})
    rules.append({"type": "regex", "pattern": r"""["']([^"']{0,400}\.m3u8[^"']{0,200})["']"""})

    out: list[EpisodeCandidate] = []
    seen: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        res = apply_rule(html, rule)
        for index, url in enumerate(res.values, start=1):
            if url in seen:
                continue
            seen.add(url)
            out.append(EpisodeCandidate(name=f"第{index:02d}集", url=url))
        for index, pair in enumerate(res.pairs, start=1):
            url = pair.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(EpisodeCandidate(name=pair.get("name") or f"第{index:02d}集", url=url))
    return out


def extract_hls_hosts(html: str) -> list[str]:
    """Hosts referenced by HLS URLs in the page -- used for failover-host inference."""
    from harvest.clean.normalize import host_of

    hosts: list[str] = []
    for url in scan_m3u8_literals(html):
        host = host_of(url)
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


# --------------------------------------------------------------------------------------
# Minimal JSONPath
# --------------------------------------------------------------------------------------

_PATH_TOKEN = re.compile(r"\.?([A-Za-z0-9_\-]+)|\[(\*|\d+)\]")


def jp_tokens(path: str) -> list[str | int]:
    """Tokenize a JSONPath-lite expression.

    Supported: ``$``, ``.key``, ``[0]``, ``[*]``, ``[]``. Deliberately not a full JSONPath
    implementation -- site recipes only ever need member access and list iteration, and a
    small grammar means a typo fails loudly instead of silently matching nothing.
    """
    path = (path or "").strip()
    if path.startswith("$"):
        path = path[1:]
    tokens: list[str | int] = []
    pos = 0
    while pos < len(path):
        match = _PATH_TOKEN.match(path, pos)
        if not match:
            if path[pos] == ".":
                pos += 1
                continue
            break
        name, index = match.group(1), match.group(2)
        if name is not None:
            tokens.append(name)
        elif index == "*":
            tokens.append("*")
        elif index is not None:
            tokens.append(int(index))
        pos = match.end()
    return tokens


def jp_get(obj: Any, path: str) -> list[Any]:
    """Resolve a JSONPath-lite expression to a list of matched values."""
    if not path:
        return []
    tokens = jp_tokens(path)
    if not tokens:
        return []
    current: list[Any] = [obj]
    for token in tokens:
        nxt: list[Any] = []
        for item in current:
            if token == "*":
                if isinstance(item, list):
                    nxt.extend(item)
            elif isinstance(token, int):
                if isinstance(item, list) and -len(item) <= token < len(item):
                    nxt.append(item[token])
            elif isinstance(item, dict) and token in item:
                nxt.append(item[token])
        current = nxt
        if not current:
            return []
    return current


def jp_first(obj: Any, path: str, default: Any = None) -> Any:
    values = jp_get(obj, path)
    return values[0] if values else default


def jp_str(obj: Any, path: str, default: str = "") -> str:
    value = jp_first(obj, path)
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        value = value[0] if value else default
    return str(value).strip() if not isinstance(value, str) else value.strip()
