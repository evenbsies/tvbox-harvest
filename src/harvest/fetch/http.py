"""HTTP client used by every adapter.

Design constraints that shaped this module:

* **Single choke point.** Adapters never call ``httpx`` directly. That keeps rate limiting,
  retries, the circuit breaker and raw-response recording in one place, and makes the whole
  pipeline testable offline via fixtures.
* **Per-site identity.** A site that returns 403 to a bare ``python-httpx`` UA will happily
  serve a recipe-configured browser UA + ``Referer``. Recipes carry those headers.
* **Never punish a site for our own bug.** 4xx responses are *not* retried (the request was
  wrong); only transport errors, timeouts and 5xx/429 are.
"""

from __future__ import annotations

import asyncio
import json as stdlib_json
import random
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from harvest.config import Settings, get_settings
from harvest.fetch.ratelimit import get_limiter
from harvest.models import SiteConfig

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# Statuses that indicate the *site* is unhealthy, as opposed to the *request* being wrong.
# This distinction is load-bearing: during probing we deliberately hit paths that do not
# exist (that is how Tier 1 is ruled out), and counting those 404s toward the circuit breaker
# would trip it after a handful of perfectly normal negative results -- then silently skip the
# Tier-3/Tier-4 stages that would have succeeded.
BREAKER_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


class FetchError(RuntimeError):
    """Raised when a request could not be completed within the retry budget."""

    def __init__(self, message: str, status: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class CircuitOpen(RuntimeError):
    """Raised when a site has failed too many consecutive times in this run."""


@dataclass
class SiteStats:
    requests: int = 0
    bytes: int = 0
    errors: int = 0
    consecutive_failures: int = 0
    statuses: dict[int, int] = field(default_factory=dict)

    def record(self, status: int, size: int) -> None:
        self.requests += 1
        self.bytes += size
        self.statuses[status] = self.statuses.get(status, 0) + 1
        if status in BREAKER_STATUS:
            self.consecutive_failures += 1
        elif status < 400:
            self.consecutive_failures = 0
        # 4xx other than 429 is an informative answer, not a sign the site is down: leave the
        # counter untouched so a run of expected 404s neither trips nor resets the breaker.

    def record_transport_failure(self) -> None:
        self.consecutive_failures += 1


class HttpClient:
    """Thin async wrapper around ``httpx.AsyncClient`` with per-site policy."""

    def __init__(
        self,
        settings: Settings | None = None,
        recorder: RawRecorder | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.limiter = get_limiter()
        self.recorder = recorder
        self._clients: dict[str, httpx.AsyncClient] = {}
        self.stats: dict[str, SiteStats] = {}
        self._robots_cache: dict[str, bool] = {}

    # -- lifecycle ---------------------------------------------------------------------

    def _client_for(self, site: SiteConfig | None, key: str) -> httpx.AsyncClient:
        if key in self._clients:
            return self._clients[key]

        cfg = site.request if site else None
        headers = dict(cfg.headers) if cfg else {}
        if site and "{base_url}" in headers.get("Referer", ""):
            headers["Referer"] = headers["Referer"].replace("{base_url}", site.base_url)

        timeout = httpx.Timeout(
            (cfg.timeout if cfg else self.settings.default_timeout),
            connect=min(10.0, cfg.timeout if cfg else self.settings.default_timeout),
        )
        client = httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
            proxy=(cfg.proxy if cfg else None),
            http2=True,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self._clients[key] = client
        self.limiter.configure(
            key,
            min(cfg.rate_limit if cfg else self.settings.default_rate_limit, self.settings.max_rate_limit),
        )
        self.stats.setdefault(key, SiteStats())
        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- requests ----------------------------------------------------------------------

    async def get(
        self,
        url: str,
        site: SiteConfig | None = None,
        *,
        key: str | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        tolerate: tuple[int, ...] = (),
        record_name: str | None = None,
    ) -> httpx.Response:
        return await self.request(
            "GET",
            url,
            site=site,
            key=key,
            params=params,
            headers=headers,
            tolerate=tolerate,
            record_name=record_name,
        )

    async def request(
        self,
        method: str,
        url: str,
        site: SiteConfig | None = None,
        *,
        key: str | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        data: Any = None,
        tolerate: tuple[int, ...] = (),
        record_name: str | None = None,
    ) -> httpx.Response:
        site_key = key or (site.key if site else "default")
        stat = self.stats.setdefault(site_key, SiteStats())

        if stat.consecutive_failures >= self.settings.breaker_failures:
            raise CircuitOpen(
                f"circuit open for {site_key}: {stat.consecutive_failures} consecutive failures"
            )

        if self.settings.offline:
            raise FetchError(f"offline mode: refusing to fetch {url}", url=url)

        client = self._client_for(site, site_key)
        retries = site.request.retries if site else self.settings.default_retries
        last_exc: Exception | None = None

        for attempt in range(max(1, retries)):
            await self.limiter.acquire(site_key)
            try:
                resp = await client.request(
                    method, url, params=params, headers=headers, json=json_body, data=data
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                stat.record_transport_failure()
                await self._backoff(attempt)
                continue

            size = len(resp.content)
            stat.record(resp.status_code, size)

            if self.recorder is not None and record_name:
                self.recorder.record(site_key, record_name, url, resp)

            if resp.status_code in tolerate:
                return resp
            if resp.status_code in RETRYABLE_STATUS:
                last_exc = FetchError(
                    f"HTTP {resp.status_code} from {url}", status=resp.status_code, url=url
                )
                await self._backoff(attempt)
                continue
            return resp

        stat.errors += 1
        if isinstance(last_exc, Exception):
            raise last_exc
        raise FetchError(f"request failed after {retries} attempts: {url}", url=url)

    @staticmethod
    async def _backoff(attempt: int) -> None:
        # 1s, 4s, 16s with jitter -- deliberately slow so a struggling site is not piled on.
        await asyncio.sleep((4**attempt) * (0.8 + random.random() * 0.4))

    # -- robots ------------------------------------------------------------------------

    async def robots_allows(self, site: SiteConfig, path: str = "/") -> bool:
        """Best-effort robots check. A missing robots.txt means 'allowed'."""
        if not site.request.respect_robots:
            return True
        key = f"{site.key}:robots"
        if key in self._robots_cache:
            return self._robots_cache[key]

        allowed = True
        try:
            resp = await self.get(f"{site.base_url}/robots.txt", site=site, tolerate=(404,))
            if resp.status_code == 200 and resp.text.strip():
                allowed = _robots_text_allows(resp.text, path)
        except Exception:
            allowed = True
        self._robots_cache[key] = allowed
        return allowed


def _robots_text_allows(text: str, path: str) -> bool:
    """Minimal robots.txt evaluator: honours only the wildcard user-agent group."""
    in_wildcard = False
    disallows: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            in_wildcard = value == "*"
        elif field == "disallow" and in_wildcard and value:
            disallows.append(value)
    for pattern in disallows:
        if pattern == "/":
            return False
        if path.startswith(pattern.rstrip("*")):
            return False
    return True


# --------------------------------------------------------------------------------------
# Raw response recording (feeds data/raw/<key>/<date>.json.gz and test fixtures)
# --------------------------------------------------------------------------------------


class RawRecorder:
    """Keeps responses that the probe/adapter explicitly ask to be recorded.

    Two consumers: the raw archive for debugging/replay, and ``tests/fixtures`` which the
    probe writes so its tier decision can be regression-tested offline.
    """

    def __init__(self, max_bytes: int = 2_000_000) -> None:
        self.max_bytes = max_bytes
        self.entries: list[dict[str, Any]] = []

    def record(self, site_key: str, name: str, url: str, resp: httpx.Response) -> None:
        body = resp.content
        truncated = len(body) > self.max_bytes
        self.entries.append(
            {
                "site_key": site_key,
                "name": name,
                "url": url,
                "status": resp.status_code,
                "content_type": resp.headers.get("content-type", ""),
                "headers": {
                    k: v
                    for k, v in resp.headers.items()
                    if k.lower() in {"server", "content-type", "x-powered-by", "set-cookie", "location"}
                },
                "truncated": truncated,
                "body": body[: self.max_bytes].decode("utf-8", errors="replace"),
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {"entries": self.entries}

    def save_fixtures(self, root: Any, key: str) -> list[str]:
        from pathlib import Path

        out_dir = Path(root) / key
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for entry in self.entries:
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", entry["name"])[:80] or "entry"
            path = out_dir / f"{safe}.json"
            path.write_text(
                stdlib_json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            written.append(str(path))
        return written


# --------------------------------------------------------------------------------------
# Response inspection helpers (shared by probe + adapters)
# --------------------------------------------------------------------------------------


def looks_like_json(resp: httpx.Response) -> bool:
    """Decide whether a response carries a usable JSON payload.

    The body is authoritative; the declared content-type is not consulted at all. Many MacCMS
    deployments declare ``Content-Type: text/html;charset=utf-8`` while returning perfectly
    valid JSON -- it is one of the most common misconfigurations on these sites, and treating
    a wrong content-type as a veto silently breaks Tier-1 detection on a large share of real
    targets. Requiring the body to actually parse also keeps the answer honest: a body that
    parses is JSON even if mislabelled, and a body that does not is not JSON even if labelled
    correctly.
    """
    text = resp.text.strip()
    if not text:
        return False

    try:
        stdlib_json.loads(text)
        return True
    except Exception:
        pass

    # Truncated or streamed bodies will not parse whole; fall back to a structural sniff.
    head = text[0]
    if head == "{":
        return re.search(r'"(?:code|list|data|msg|total|pagecount|items|result)"\s*:', text[:4096]) is not None
    if head == "[":
        return True
    return False


def is_html_response(resp: httpx.Response) -> bool:
    """True when the response is plausibly an HTML document.

    Used to stop HTML-oriented detection stages from running against a JSON API response --
    a video-list JSON body contains plenty of strings that superficially resemble links, and
    misreading one as an HTML catalogue produces a Tier-3 verdict for an API-only site.

    Like :func:`looks_like_json`, the body is authoritative and the declared content-type is
    only a tie-breaker, for the same reason: these sites routinely mislabel JSON as text/html.
    """
    head = resp.text.lstrip()[:400].lower()
    if head.startswith(("{", "[")):
        return False
    if head.startswith(("<!doctype html", "<html")) or "<head" in head or "<body" in head:
        return True
    return "html" in resp.headers.get("content-type", "").lower()


def safe_json(resp: httpx.Response) -> Any | None:
    try:
        return resp.json()
    except Exception:
        pass
    try:
        return stdlib_json.loads(resp.text)
    except Exception:
        return None


def get_path(obj: Any, dotted: str) -> Any:
    """Tiny dotted-path getter used for evidence extraction, e.g. ``list.0.vod_id``."""
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if 0 <= idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur
