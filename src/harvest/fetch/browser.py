"""Headless-browser network sniffing.

Why this exists: on Tier-4 sites the media address simply is not present in any static
response. It is produced at runtime by the page's own player code, sometimes behind a
signed/secret-key request. The only approach that generalizes across such sites without
writing a bespoke reverse-engineering effort per site is to run a real browser, drive the
page the way a user would, and *observe* the requests it makes.

Design notes:

* One browser process is reused across all titles in a run; a fresh context per title keeps
  cookies/localStorage from leaking between titles.
* We record every response, not just matches, so a failed sniff still produces a readable
  trace of what the player actually asked for.
* ``read_globals`` pulls page-scope variables (e.g. 4kvm's base64 ``_pdf`` host list) which
  some players use for failover.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

MEDIA_CONTENT_TYPES = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "video/mp4",
    "video/mp2t",
)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _host_key(host: str) -> str:
    """Normalise a hostname for same-site comparison.

    ``str.lstrip("www.")`` looks like it strips a prefix but actually strips any leading run of
    the *characters* ``w`` and ``.``. ``"ww2.example.com"`` therefore became ``"2.example.com"``
    and ``"wow.example.com"`` became ``"ow.example.com"``. This feeds the check that decides
    whether an episode link is ours to follow, so a mangled key is not cosmetic: it silently
    changes which links get visited, and it can turn an off-site host into a "same-site" one.
    """
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _same_site(base_url: str, candidate: str) -> bool:
    """True when both URLs share a registrable-ish host.

    Only same-site links are followed. Following an off-site href would turn a harvest run into
    an open redirector for whatever the page chooses to link to.
    """
    from urllib.parse import urlparse

    try:
        a = urlparse(base_url).hostname or ""
        b = urlparse(candidate).hostname or ""
    except ValueError:
        return False
    if not a or not b:
        return False
    a = _host_key(a)
    b = _host_key(b)
    return a == b or a.endswith("." + b) or b.endswith("." + a)


@dataclass
class SniffedResponse:
    url: str
    status: int = 0
    content_type: str = ""
    resource_type: str = ""
    method: str = "GET"

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "content_type": self.content_type,
            "resource_type": self.resource_type,
            "method": self.method,
        }


@dataclass
class SniffResult:
    page_url: str
    media_urls: list[str] = field(default_factory=list)
    matched: list[SniffedResponse] = field(default_factory=list)
    responses: list[SniffedResponse] = field(default_factory=list)
    globals: dict[str, Any] = field(default_factory=dict)
    html_len: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.media_urls)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "page_url": self.page_url,
            "media_urls": self.media_urls[:6],
            "matched": [m.as_dict() for m in self.matched[:10]],
            "response_count": len(self.responses),
            "globals": {k: (v[:120] if isinstance(v, str) else v) for k, v in self.globals.items()},
            "error": self.error,
        }


class BrowserSniffer:
    """Context-managed Playwright session."""
    def __init__(
        self,
        *,
        headless: bool = True,
        user_agent: str = DEFAULT_UA,
        locale: str = "zh-CN",
        # NOTE: "media" is deliberately NOT blocked. hls.js usually fetches the manifest via
        # XHR, but some players let the <video> element request it directly -- blocking media
        # would hide exactly the response we came for.
        block_resource_types: tuple[str, ...] = ("image", "font", "stylesheet"),
    ) -> None:
        self.headless = headless
        self.user_agent = user_agent
        self.locale = locale
        self.block_resource_types = block_resource_types
        self._pw = None
        self._browser = None

    async def __aenter__(self) -> BrowserSniffer:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            if self._pw is not None:
                await self._pw.stop()
            self._browser = None
            self._pw = None

    # -- main entry point --------------------------------------------------------------

    async def sniff(
        self,
        url: str,
        *,
        sniff_patterns: list[str] | None = None,
        click_selector: str | None = None,
        click_episode: int | None = 1,
        read_globals: list[str] | None = None,
        wait_until: str = "domcontentloaded",
        timeout: float = 30.0,
        settle_ms: int = 2500,
        extra_wait_ms: int = 8000,
        capture_body_patterns: list[str] | None = None,
    ) -> SniffResult:
        assert self._browser is not None, "BrowserSniffer must be used as an async context manager"

        patterns = [re.compile(p) for p in (sniff_patterns or [r"\.m3u8", r"/m3u8/"])]
        body_patterns = [re.compile(p) for p in (capture_body_patterns or [])]
        result = SniffResult(page_url=url)
        collected: list[SniffedResponse] = []
        bodies: list[tuple[str, str]] = []

        context = await self._browser.new_context(
            user_agent=self.user_agent,
            locale=self.locale,
            viewport={"width": 1366, "height": 900},
            ignore_https_errors=True,
        )
        # Hide the most obvious automation tells; some players branch on them.
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()

        await self._install_routes(page, set(self.block_resource_types), body_patterns, bodies)

        def _on_response(resp) -> None:
            try:
                ctype = (resp.headers or {}).get("content-type", "")
                entry = SniffedResponse(
                    url=resp.url,
                    status=resp.status,
                    content_type=ctype,
                    resource_type=resp.request.resource_type,
                    method=resp.request.method,
                )
            except Exception:
                return
            collected.append(entry)
            haystack = f"{entry.url}\n{entry.content_type}"
            if any(p.search(haystack) for p in patterns):
                result.matched.append(entry)

        page.on("response", _on_response)

        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout * 1000)
            await page.wait_for_timeout(settle_ms)

            # Prefer the play-API body: on many sites the player never requests the stream in
            # headless mode, so the body is the only place the address appears.
            result.media_urls = await self._media_from_bodies(bodies)
            for found in self._extract_media(collected, patterns):
                if found not in result.media_urls:
                    result.media_urls.append(found)

            if click_selector:
                selector = (
                    click_selector.format(ep=click_episode) if "{ep}" in click_selector else click_selector
                )
                click_errors: list[dict[str, Any]] = []
                method = await self._activate_episode(
                    page,
                    selector,
                    base_url=url,
                    wait_until=wait_until,
                    timeout=timeout,
                    follow_links=True,
                    error_sink=click_errors,
                    episode=int(click_episode or 1),
                )
                result.globals["_activate_method"] = method
                if click_errors:
                    result.globals["_click_error"] = click_errors[0].get("error")

            # Give the player time to negotiate and request the manifest.
            deadline = asyncio.get_event_loop().time() + (extra_wait_ms / 1000.0)
            while asyncio.get_event_loop().time() < deadline:
                if self._extract_media(result.matched, patterns):
                    break
                await page.wait_for_timeout(400)

            if read_globals:
                for name in read_globals:
                    try:
                        value = await page.evaluate(f"() => window.{name} || null")
                    except Exception:
                        value = None
                    if value is not None:
                        result.globals[name] = value

            # Last resort: the URL may be sitting in a JSON blob or attribute even if it was
            # never requested as a discrete response (e.g. blocked by our own route filter).
            try:
                html = await page.content()
                result.html_len = len(html)
                for found in re.findall(r"https?://[^\s\"'<>\\]{0,300}\.m3u8", html):
                    if found not in result.media_urls:
                        result.media_urls.append(found)
            except Exception:
                pass

            try:
                for value in result.globals.values():
                    if isinstance(value, str):
                        from harvest.clean.gambling import decode_base64_host_list

                        for host in decode_base64_host_list(value):
                            result.globals.setdefault("_decoded_hosts", [])
                            hosts = result.globals["_decoded_hosts"]
                            if host not in hosts:
                                hosts.append(host)
            except Exception:
                pass

        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            result.responses = collected
            result.media_urls = self._extract_media(result.matched, patterns, result.media_urls)
            try:
                await context.close()
            except Exception:
                pass

        return result

    @staticmethod
    def _extract_media(
        matched: list[SniffedResponse], patterns: list[re.Pattern[str]], seed: list[str] | None = None
    ) -> list[str]:
        urls: list[str] = []
        for item in seed or []:
            if item not in urls:
                urls.append(item)
        for item in matched:
            if item.status >= 400:
                continue
            is_media_ct = any(ct in item.content_type.lower() for ct in MEDIA_CONTENT_TYPES)
            is_media_url = any(p.search(item.url) for p in patterns)
            if (is_media_ct or is_media_url) and item.url not in urls:
                urls.append(item.url)
        return urls

    # -- interaction helpers -----------------------------------------------------------

    @staticmethod
    async def _element_href(page, selector: str) -> str | None:
        """Read an element's href, if it is a link at all.

        Many players render episode selectors as real anchors (one page per episode), which is
        the most reliable thing to act on: navigating gives a clean page load that autoplays,
        with no dependence on player internals.
        """
        try:
            return await page.eval_on_selector(
                selector, "el => (el && el.tagName === 'A' && el.href) ? el.href : null"
            )
        except Exception:
            return None

    @staticmethod
    async def _js_click(page, selector: str) -> None:
        """Click via JavaScript, bypassing hit-testing.

        A preroll ad overlay swallows real pointer events for its duration (observed: 12s on
        4kvm.tv), which makes ``page.click`` time out even though the element is present and
        correct. Dispatching the click on the element itself sidesteps the overlay entirely.
        """
        await page.eval_on_selector(selector, "el => el.click()")
        await page.eval_on_selector(
            selector,
            "el => el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}))",
        )

    async def _activate_episode(
        self,
        page,
        selector: str,
        *,
        base_url: str,
        wait_until: str,
        timeout: float,
        follow_links: bool,
        error_sink: list[dict[str, Any]],
        episode: int,
    ) -> str:
        """Move the page to the requested episode. Returns how it was done, for the trace."""
        href = await self._element_href(page, selector) if follow_links else None
        if href and _same_site(base_url, href):
            if href.rstrip("/") != page.url.rstrip("/"):
                try:
                    await page.goto(href, wait_until=wait_until, timeout=timeout * 1000)
                    return "navigate"
                except Exception as exc:
                    error_sink.append({"episode": episode, "error": f"goto failed: {exc}"[:160]})
            return "noop"

        try:
            await page.click(selector, timeout=min(6000, timeout * 1000))
            return "click"
        except Exception:
            pass
        try:
            await self._js_click(page, selector)
            return "js_click"
        except Exception as exc:
            error_sink.append({"episode": episode, "error": f"click failed: {exc}"[:160]})
            return "failed"

    @staticmethod
    async def _media_from_bodies(targets: list[tuple[str, str]]) -> list[str]:
        """Extract media URLs from captured API response bodies.

        This is the *primary* signal on these sites, not a fallback. Measured on 4kvm.tv: the
        player asks ``/video/play?...`` for the address about a second after the page loads, but
        in headless mode it never goes on to request the ``.m3u8`` at all (no user gesture, so
        hls.js never starts playback). Reading the API response is therefore the only way to
        learn the address -- waiting for the stream request would return nothing.

        Both spellings show up in the wild: absolute URLs, and host-plus-path fragments that the
        page joins against a failover host from a global (e.g. 4kvm's base64 ``_pdf``).
        """
        found: list[str] = []
        for _url, text in targets:
            if not text:
                continue
            for match in re.findall(r"https?://[^\s\"'<>\\]{0,400}\.m3u8[^\s\"'<>\\]{0,200}", text):
                if match not in found:
                    found.append(match)
            for match in re.findall(r"[\"'](/m3u8/[^\"'<>\s]{1,300})[\"']", text):
                if match not in found:
                    found.append(match)
        return found

    @staticmethod
    async def _install_routes(
        page,
        blocked_resource_types: set[str],
        body_patterns: list[re.Pattern[str]],
        bodies: list[tuple[str, str]],
    ) -> None:
        """Route every request, capturing response bodies for the patterns we care about.

        Bodies are read **at interception time** rather than later from a stored response object.
        That distinction is the whole point: the browser evicts response bodies once the page
        navigates, so a deferred ``await response.text()`` silently fails for every episode after
        the first. Interception also makes the capture deterministic -- we own the response, so
        there is no race with eviction.

        The response is replayed to the page unchanged via ``fulfill(response=...)``, so the
        player behaves exactly as it would without us.
        """

        async def _handler(route) -> None:
            request = route.request
            try:
                if body_patterns and any(p.search(request.url) for p in body_patterns):
                    response = await route.fetch()
                    try:
                        bodies.append((request.url, await response.text()))
                    except Exception as exc:
                        bodies.append((request.url, ""))
                        _ = exc
                    await route.fulfill(response=response)
                    return
                if request.resource_type in blocked_resource_types:
                    await route.abort()
                    return
                await route.continue_()
            except Exception:
                # A route handler must never raise into the page: better a missed capture than a
                # broken page load that then yields nothing at all.
                try:
                    await route.continue_()
                except Exception:
                    pass

        await page.route("**/*", _handler)

    @staticmethod
    async def _collect_episode_media(
        page,
        collected: list[SniffedResponse],
        bodies: list[tuple[str, str]],
        collected_marker: int,
        body_marker: int,
        patterns: list[re.Pattern[str]],
        deadline_s: float,
    ) -> list[str]:
        """Wait for one episode's address, preferring the API body over the stream request.

        Two independent signals, in priority order:

        1. The play-API response body -- authoritative and available as soon as the player asks
           for the address. On sites that never start playback in headless mode (a very common
           case, since there is no user gesture) this is the *only* usable signal.
        2. The media request itself (``.m3u8`` / ``application/vnd.apple.mpegurl``) -- kept for
           players that embed the address in the page or fetch it without an API call.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + deadline_s
        from_bodies: list[str] = []
        from_streams: list[str] = []

        while loop.time() < deadline:
            from_bodies = await BrowserSniffer._media_from_bodies(bodies[body_marker:])
            from_streams = BrowserSniffer._extract_media(collected[collected_marker:], patterns)
            if from_bodies or from_streams:
                break
            await page.wait_for_timeout(250)

        merged = list(from_bodies)
        for url in from_streams:
            if url not in merged:
                merged.append(url)
        return merged

    ## -- multi-episode capture ---------------------------------------------------------

    async def sniff_episodes(
        self,
        url: str,
        *,
        click_selector: str,
        episodes: list[int],
        sniff_patterns: list[str] | None = None,
        read_globals: list[str] | None = None,
        wait_until: str = "domcontentloaded",
        timeout: float = 40.0,
        settle_ms: int = 2500,
        per_episode_wait_ms: int = 10000,
        follow_links: bool = True,
        capture_body_patterns: list[str] | None = None,
        stop_body_patterns: list[str] | None = None,
    ) -> dict[str, Any]:
        """Drive one detail page through several episodes inside a single browser context.

        Three things this gets right that a naive implementation does not:

        1. **The first episode is usually already playing.** Most players autoplay on load, so
           its media is captured during the initial settle window -- before any click. Looking
           only at post-click traffic would report episode 1 as missing.
        2. **Clicks get blocked by preroll ad overlays.** So when the selector is a link, we
           navigate to it instead of clicking; when it is not, we dispatch the click in JS.
        3. **Order is the fallback.** If per-episode attribution fails, the distinct media URLs
           in arrival order line up with the episode sequence, which is a better outcome than
           reporting nothing.
        """
        assert self._browser is not None, "BrowserSniffer must be used as an async context manager"

        patterns = [re.compile(p) for p in (sniff_patterns or [r"\.m3u8", r"/m3u8/"])]
        out: dict[str, Any] = {
            "page_url": url,
            "episodes": {},
            "episode_bodies": {},
            "globals": {},
            "responses": [],
            "errors": [],
            "methods": {},
            "stopped_early": None,
        }

        context = await self._browser.new_context(
            user_agent=self.user_agent,
            locale=self.locale,
            viewport={"width": 1366, "height": 900},
            ignore_https_errors=True,
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()

        if self.block_resource_types:
            blocked = set(self.block_resource_types)

            async def _route(route) -> None:
                try:
                    if route.request.resource_type in blocked:
                        await route.abort()
                    else:
                        await route.continue_()
                except Exception:
                    pass

            await page.route("**/*", _route)

        collected: list[SniffedResponse] = []
        # (url, body) for every response matching capture_body_patterns. Bodies are read at
        # interception time; see _install_routes for why that matters.
        bodies: list[tuple[str, str]] = []
        body_patterns = [re.compile(p) for p in (capture_body_patterns or [])]

        await self._install_routes(page, set(self.block_resource_types), body_patterns, bodies)

        def _on_response(resp) -> None:
            try:
                collected.append(
                    SniffedResponse(
                        url=resp.url,
                        status=resp.status,
                        content_type=(resp.headers or {}).get("content-type", ""),
                        resource_type=resp.request.resource_type,
                        method=resp.request.method,
                    )
                )
            except Exception:
                return

        page.on("response", _on_response)

        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout * 1000)
            await page.wait_for_timeout(settle_ms)

            # Episode 1 is normally already loaded by now, its play call included.
            autoplay_media = await self._media_from_bodies(bodies)

            if read_globals:
                for name in read_globals:
                    try:
                        value = await page.evaluate(f"() => window.{name} || null")
                    except Exception:
                        value = None
                    if value is not None:
                        out["globals"][name] = value

            for index, ep in enumerate(episodes):
                selector = click_selector.format(ep=ep) if "{ep}" in click_selector else click_selector
                marker = len(collected)
                # Episode 1's play request happens during the initial load, so its body window
                # starts at zero rather than at the current position.
                body_marker = 0 if index == 0 else len(bodies)

                method = await self._activate_episode(
                    page,
                    selector,
                    base_url=url,
                    wait_until=wait_until,
                    timeout=timeout,
                    follow_links=follow_links,
                    error_sink=out["errors"],
                    episode=ep,
                )
                out["methods"][str(ep)] = method

                if method == "failed" and index > 0:
                    continue

                found = await self._collect_episode_media(
                    page,
                    collected,
                    bodies,
                    marker,
                    body_marker,
                    patterns,
                    per_episode_wait_ms / 1000.0,
                )
                if not found and index == 0 and autoplay_media:
                    found = list(autoplay_media)
                if found:
                    out["episodes"][ep] = found

                # Keep the raw play-API bodies per episode: they carry structured quality and
                # entitlement flags, which is far more informative than a bare URL list.
                episode_texts = [text for _url, text in bodies[body_marker:]]
                if episode_texts:
                    out["episode_bodies"][str(ep)] = episode_texts

                # Some sites give one episode away and gate the rest behind a membership. When
                # the play API says so, walking the remaining episodes is pure waste: every one
                # will be refused. Stop and let the caller record why.
                if stop_body_patterns:
                    haystack = "\n".join(episode_texts)
                    for pattern in stop_body_patterns:
                        if pattern and pattern in haystack:
                            out["stopped_early"] = {
                                "episode": ep,
                                "reason": pattern,
                                "remaining": [e for e in episodes if e > ep],
                            }
                            break
                    if out["stopped_early"]:
                        break

            # Positional fallback: distinct media URLs in arrival order correspond to the
            # episode sequence. Better to emit a probably-correct mapping than nothing at all.
            # Bodies first, since that is where the addresses actually live on these sites.
            ordered = await self._media_from_bodies(bodies)
            for url in self._extract_media(collected, patterns):
                if url not in ordered:
                    ordered.append(url)
            missing = [ep for ep in episodes if not out["episodes"].get(ep)]
            if missing and ordered:
                taken = {u for urls in out["episodes"].values() for u in urls}
                spare = [u for u in ordered if u not in taken]
                for ep, url in zip(missing, spare, strict=False):
                    out["episodes"][ep] = [url]
                if spare:
                    out["positional_fallback_used"] = True

            try:
                html = await page.content()
                literals = re.findall(r"https?://[^\s\"'<>\\]{0,300}\.m3u8", html)
                if literals:
                    out["html_media_literals"] = list(dict.fromkeys(literals))
            except Exception:
                pass

        except Exception as exc:
            out["errors"].append({"episode": None, "error": f"{type(exc).__name__}: {exc}"[:300]})
        finally:
            out["responses"] = [r.as_dict() for r in collected[:400]]
            try:
                hosts: list[str] = []
                for value in out["globals"].values():
                    if isinstance(value, str):
                        from harvest.clean.gambling import decode_base64_host_list

                        for host in decode_base64_host_list(value):
                            if host not in hosts:
                                hosts.append(host)
                if hosts:
                    out["globals"]["_decoded_hosts"] = hosts
            except Exception:
                pass
            try:
                await context.close()
            except Exception:
                pass

        return out
