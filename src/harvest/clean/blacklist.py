"""Blacklist loading and matching.

Two independent lists are supported, both file-driven so they can be tuned without
touching code:

* **Adult list** -- ``config/blacklist/terms.txt`` + ``regex.txt``
* **Gambling (BC) list** -- ``config/blacklist/bc_terms.txt`` + ``bc_domains.txt``
  (+ ``bc_domains.learned.txt`` and ``bc_terms.learned.txt`` written by the learner)

Matching rules deliberately differ by script. CJK terms are matched as plain substrings
(they are not whitespace-delimited). ASCII terms are matched on word boundaries, otherwise
``av`` would match ``avatar`` and the filter would eat legitimate titles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from harvest.config import get_settings

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


@dataclass
class TermMatcher:
    name: str
    terms: list[str] = field(default_factory=list)
    patterns: list[re.Pattern[str]] = field(default_factory=list)
    _ascii_re: re.Pattern[str] | None = None

    def compile(self) -> TermMatcher:
        ascii_terms = [t for t in self.terms if not _CJK.search(t)]
        if ascii_terms:
            alternation = "|".join(re.escape(t) for t in sorted(ascii_terms, key=len, reverse=True))
            self._ascii_re = re.compile(rf"(?i)(?<![A-Za-z0-9])(?:{alternation})(?![A-Za-z0-9])")
        return self

    def match(self, *texts: str | None) -> list[str]:
        """Return every term/pattern that hits any of the supplied texts."""
        blob = "\n".join(t for t in texts if t)
        if not blob:
            return []
        hits: list[str] = []
        for term in self.terms:
            if _CJK.search(term):
                if term in blob:
                    hits.append(term)
            elif self._ascii_re is not None and self._ascii_re.search(blob):
                hits.append(term)
        for pat in self.patterns:
            m = pat.search(blob)
            if m:
                hits.append(f"regex:{pat.pattern}")
        return hits

    def __bool__(self) -> bool:
        return bool(self.terms or self.patterns)


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _read_patterns(path: Path) -> list[re.Pattern[str]]:
    out: list[re.Pattern[str]] = []
    for line in _read_lines(path):
        try:
            out.append(re.compile(line))
        except re.error:
            continue
    return out


@lru_cache(maxsize=1)
def adult_matcher() -> TermMatcher:
    cfg = get_settings().config_dir / "blacklist"
    return TermMatcher(
        name="adult",
        terms=_read_lines(cfg / "terms.txt"),
        patterns=_read_patterns(cfg / "regex.txt"),
    ).compile()


@lru_cache(maxsize=1)
def bc_term_matcher() -> TermMatcher:
    cfg = get_settings().config_dir / "blacklist"
    return TermMatcher(
        name="bc_terms",
        terms=_read_lines(cfg / "bc_terms.txt") + _read_lines(cfg / "bc_terms.learned.txt"),
    ).compile()


@dataclass
class DomainRule:
    raw: str
    regex: re.Pattern[str] | None
    host: str | None

    def matches_host(self, host: str) -> bool:
        host = host.lower().strip(".")
        if self.host:
            return host == self.host or host.endswith("." + self.host)
        return False


@dataclass
class BcDomainList:
    rules: list[DomainRule] = field(default_factory=list)
    url_patterns: list[re.Pattern[str]] = field(default_factory=list)

    def match_host(self, host: str) -> str | None:
        for rule in self.rules:
            if rule.matches_host(host):
                return rule.raw
        for pat in self.url_patterns:
            if pat.search(host):
                return f"regex:{pat.pattern}"
        return None

    def match_url(self, url: str) -> str | None:
        for pat in self.url_patterns:
            if pat.search(url):
                return f"regex:{pat.pattern}"
        # Fall back to the hostname so a pattern anchored on a label boundary still applies
        # when the caller only has the full URL to hand.
        host = url.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]
        host = host.rsplit("@", 1)[-1].split(":", 1)[0]
        if host:
            for pat in self.url_patterns:
                if pat.search(host):
                    return f"regex:{pat.pattern}"
        return None


def _parse_bc_domains(path: Path) -> tuple[list[DomainRule], list[re.Pattern[str]]]:
    """Parse the BC domain list.

    Two entry forms, both explicit so a pattern can never be silently mistaken for a hostname:

    * ``re:<regex>`` -- compiled and tested against the full URL *and* the bare hostname.
    * anything else  -- an exact host, or any subdomain of it.

    The older ``/regex/`` spelling is still accepted for compatibility, but is rejected for
    patterns containing a literal ``/`` (which a URL regex almost always does), so it is not
    the recommended form.
    """
    rules: list[DomainRule] = []
    urlpats: list[re.Pattern[str]] = []
    for line in _read_lines(path):
        lowered = line.lower()
        if lowered.startswith("re:"):
            try:
                urlpats.append(re.compile(line[3:].strip(), re.IGNORECASE))
            except re.error:
                continue
            continue
        if line.startswith("/") and line.endswith("/") and len(line) > 2 and "/" not in line[1:-1]:
            try:
                urlpats.append(re.compile(line[1:-1], re.IGNORECASE))
            except re.error:
                continue
            continue
        # Strip any trailing inline comment before treating the line as a hostname.
        host = line.split()[0].lower().lstrip(".")
        if "/" in host:
            host = host.split("/", 1)[0]
        host = host.split("#", 1)[0]
        if host:
            rules.append(DomainRule(raw=line, regex=None, host=host))
    return rules, urlpats


@lru_cache(maxsize=1)
def bc_domains() -> BcDomainList:
    cfg = get_settings().config_dir / "blacklist"
    all_rules: list[DomainRule] = []
    all_pats: list[re.Pattern[str]] = []
    for name in ("bc_domains.txt", "bc_domains.learned.txt"):
        rules, pats = _parse_bc_domains(cfg / name)
        all_rules.extend(rules)
        all_pats.extend(pats)
    return BcDomainList(rules=all_rules, url_patterns=all_pats)


def reload_blacklists() -> None:
    """Drop caches so edited lists take effect (used by tests and `harvest doctor`)."""
    adult_matcher.cache_clear()
    bc_term_matcher.cache_clear()
    bc_domains.cache_clear()


def append_learned_bc_host(host: str, reason: str = "") -> bool:
    """Promote a host into the learned BC blacklist. Returns True if newly added."""
    host = host.lower().strip(".")
    if not host or host in {"", "localhost"}:
        return False
    cfg = get_settings().config_dir / "blacklist"
    cfg.mkdir(parents=True, exist_ok=True)
    path = cfg / "bc_domains.learned.txt"
    existing = set(_read_lines(path))
    if host in existing:
        return False
    header = ""
    if not path.exists():
        header = "# Auto-generated by `harvest doctor --learn-bc`. Safe to delete/regenerate.\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(header)
        fh.write(f"{host}" + (f"  # {reason}" if reason else "") + "\n")
    bc_domains.cache_clear()
    return True
