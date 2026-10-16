"""Gambling (BC / 博彩) detection and filtering.

This is a first-class requirement, not an afterthought, so it gets its own module with
two independent detection lines:

**Line 1 -- site level: "is this site carrying gambling advertising?"**
Scans fetched HTML for gambling signals: outbound links / iframes pointing at blacklisted
hosts, injected redirect scripts, floating ad layers, and gambling keywords in visible
text. A site that trips ``BC_SITE_THRESHOLD`` distinct signal *kinds* is quarantined and
kept out of the main subscription until a human explicitly opts it back in via
``compliance.bc_filter: false``.

**Line 2 -- entry level: "is this source supplied by a gambling operator?"**
Inspects the harvested item itself: the media host against the domain blacklist, gambling
promotion query parameters, injected ``#EXT-X-`` ad tags inside manifests, gambling
vocabulary in the title/remarks/category, and host reputation learned across runs.

Both lines are conservative about *evidence*: they record why something was dropped so the
decision can be audited in ``data/reports/<date>.json``.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from harvest.clean.blacklist import (
    append_learned_bc_host,
    bc_domains,
    bc_term_matcher,
)
from harvest.models import Vod

# A site needs at least this many distinct signal kinds to be quarantined. Two independent
# kinds (e.g. a bad outbound link + an ad script) is the point where coincidence stops
# being plausible.
BC_SITE_THRESHOLD = 2

_IFRAME_SRC = re.compile(r"<iframe[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
_ANCHOR_HREF = re.compile(r"<a[^>]+href=[\"']([^\"']+)[\"']", re.IGNORECASE)
_SCRIPT_SRC = re.compile(r"<script[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
_REDIRECT_PATTERNS = [
    re.compile(r"location\.(?:href|replace)\s*[=(]\s*[\"']([^\"']+)[\"']", re.IGNORECASE),
    re.compile(r"window\.open\(\s*[\"']([^\"']+)[\"']", re.IGNORECASE),
]
_AD_TAG = re.compile(r"#EXT-X-(?:AD|ASSET|CUE|DISCONTINUITY-SEQUENCE)[^\n]*", re.IGNORECASE)
_PROMO_PARAMS = {
    "pid", "agent", "agency", "channel", "promo", "invite", "inviter", "ref",
    "aff", "affiliate", "tg", "bot", "from_code", "share_code",
}


# --------------------------------------------------------------------------------------
# Line 1: site level
# --------------------------------------------------------------------------------------


@dataclass
class SiteBcScan:
    signals: dict[str, list[str]] = field(default_factory=dict)
    keyword_hits: list[str] = field(default_factory=list)

    @property
    def kinds(self) -> int:
        return len(self.signals)

    @property
    def quarantined(self) -> bool:
        return self.kinds >= BC_SITE_THRESHOLD or len(self.keyword_hits) >= 6

    def as_evidence(self) -> dict[str, object]:
        return {
            "kinds": self.kinds,
            "quarantined": self.quarantined,
            "signals": {k: v[:5] for k, v in self.signals.items()},
            "keyword_hits": self.keyword_hits[:20],
        }


def scan_site_html(html: str, base_url: str = "") -> SiteBcScan:
    """Scan a page for gambling-advertising signals."""
    scan = SiteBcScan()
    if not html:
        return scan

    hostlist = bc_domains()

    bad_links: list[str] = []
    for pattern in (_ANCHOR_HREF, _IFRAME_SRC, _SCRIPT_SRC):
        for raw in pattern.findall(html):
            host = _host_of(raw, base_url)
            if not host:
                continue
            if hostlist.match_host(host) or hostlist.match_url(raw):
                bad_links.append(raw[:200])
    if bad_links:
        scan.signals["blacklisted_outbound"] = sorted(set(bad_links))

    redirects: list[str] = []
    for pattern in _REDIRECT_PATTERNS:
        for raw in pattern.findall(html):
            host = _host_of(raw, base_url)
            if host and (hostlist.match_host(host) or hostlist.match_url(raw)):
                redirects.append(raw[:200])
    if redirects:
        scan.signals["injected_redirect"] = sorted(set(redirects))

    # Gambling vocabulary appearing in the page itself (banner copy, float layers, etc.)
    hits = bc_term_matcher().match(html)
    if hits:
        # Require a meaningful density so a film titled "赌神" does not condemn the site.
        counts = Counter(hits)
        total = sum(counts.values())
        if total >= 3:
            scan.signals["keyword_density"] = [f"{k}x{v}" for k, v in counts.most_common(10)]
        scan.keyword_hits = hits

    return scan


def _host_of(raw_url: str, base_url: str) -> str:
    raw_url = raw_url.strip()
    if not raw_url or raw_url.startswith(("javascript:", "#", "mailto:", "data:")):
        return ""
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    try:
        parsed = urlparse(raw_url)
    except ValueError:
        return ""
    host = parsed.hostname or ""
    if not host and base_url:
        from urllib.parse import urljoin

        try:
            host = urlparse(urljoin(base_url, raw_url)).hostname or ""
        except ValueError:
            host = ""
    return host.lower()


# --------------------------------------------------------------------------------------
# Line 2: entry level
# --------------------------------------------------------------------------------------


@dataclass
class EntryVerdict:
    dropped: bool = False
    reasons: list[str] = field(default_factory=list)
    bad_hosts: list[str] = field(default_factory=list)

    def add(self, reason: str) -> None:
        self.dropped = True
        self.reasons.append(reason)


def check_entry(vod: Vod, policy_bc_filter: bool = True) -> EntryVerdict:
    """Inspect one harvested item for gambling provenance."""
    verdict = EntryVerdict()
    if not policy_bc_filter:
        return verdict

    terms = bc_term_matcher()
    hostlist = bc_domains()

    # (a) naming: category / title / remarks. Gambling "content" is usually labelled as
    # such, so the category is the cheapest and most reliable signal.
    cat_hits = terms.match(*vod.cat_raw, vod.cat_norm)
    if cat_hits:
        verdict.add(f"bc_category:{','.join(cat_hits[:4])}")

    name_hits = terms.match(vod.name, vod.remarks)
    if len(name_hits) >= 2:
        verdict.add(f"bc_name:{','.join(name_hits[:4])}")

    # (b) media provenance: blacklisted host or promotion parameters
    for line in vod.lines:
        for ep in line.episodes:
            for url in ep.all_urls():
                host = _host_of(url, vod.source_url or "")
                if not host:
                    continue
                hit = hostlist.match_host(host) or hostlist.match_url(url)
                if hit:
                    verdict.bad_hosts.append(host)
                    verdict.add(f"bc_host:{hit}")
                promo = _promo_params(url)
                if promo:
                    verdict.add(f"bc_promo_params:{','.join(sorted(promo))}")

    verdict.bad_hosts = sorted(set(verdict.bad_hosts))
    return verdict


def _promo_params(url: str) -> set[str]:
    try:
        query = parse_qs(urlparse(url).query)
    except ValueError:
        return set()
    return {k.lower() for k in query if k.lower() in _PROMO_PARAMS}


def check_manifest(text: str) -> EntryVerdict:
    """Detect ad/marketing tag injection inside an HLS manifest."""
    verdict = EntryVerdict()
    if not text:
        return verdict

    text = text[:200_000]
    hostlist = bc_domains()

    for tag in _AD_TAG.findall(text):
        for raw in re.findall(r"https?://[^\s,\"']+", tag):
            host = _host_of(raw, "")
            if host and (hostlist.match_host(host) or hostlist.match_url(raw)):
                verdict.add(f"bc_manifest_ad_tag:{host}")

    for raw in re.findall(r"https?://[^\s,\"']+", text):
        if hostlist.match_url(raw):
            verdict.add(f"bc_manifest_url:{raw[:120]}")

    return verdict


def learn_from_hosts(hosts: Counter[str], threshold: int = 5) -> list[str]:
    """Promote hosts that repeatedly tripped BC signals into the learned blacklist.

    Returns the hosts newly added. Called at the end of a run so the blacklist improves
    without a human editing files.
    """
    added: list[str] = []
    for host, count in hosts.items():
        if count >= threshold and append_learned_bc_host(host, f"auto: {count} hits"):
            added.append(host)
    return added


def decode_base64_host_list(value: str) -> list[str]:
    """Decode a base64-encoded JSON array of hostnames, e.g. 4kvm.tv's ``window._pdf``.

    Returns ``[]`` on anything unexpected -- this parses untrusted page content.
    """
    if not value:
        return []
    value = value.strip().strip("\"'")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4)).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return []
    import json

    try:
        data = json.loads(decoded)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9.-]{1,253}", item):
            out.append(item)
    return out
