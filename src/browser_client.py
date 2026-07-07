"""Playwright-based fetch layer for Cardmarket.

Cardmarket sits behind Cloudflare's managed JS challenge ("Just a moment...").
An HTTP client cannot execute the challenge JavaScript, so a real browser is
required. With a genuine Chromium + residential proxy the challenge normally
auto-resolves within a few seconds without any CAPTCHA.

This client mirrors CardmarketClient.fetch() -> FetchResult so the pipeline in
main.py is fetch-layer agnostic.
"""

from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from .client import FetchResult, CLOUDFLARE_MARKERS

CHALLENGE_TITLES = ("just a moment", "attention required", "un momento")

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--window-size=1366,768",
]

# Keep stealth minimal: in headful mode the browser is already coherent, and
# clumsy spoofing (fake plugin arrays etc.) is itself a bot signal.
STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


def _parse_proxy(proxy_url: Optional[str]) -> Optional[dict]:
    if not proxy_url:
        return None
    p = urlparse(proxy_url)
    cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        cfg["username"] = p.username
    if p.password:
        cfg["password"] = p.password
    return cfg


def _is_challenge_title(title: str) -> bool:
    t = (title or "").lower()
    return any(marker in t for marker in CHALLENGE_TITLES)


def _looks_blocked_html(html: str) -> bool:
    head = (html or "")[:4000]
    return any(marker in head for marker in CLOUDFLARE_MARKERS)


class BrowserCardmarketClient:
    def __init__(
        self,
        *,
        proxy_url_factory: Optional[Callable[[], Awaitable[Optional[str]]]],
        max_retries: int = 3,
        base_delay_ms: int = 1200,
        challenge_timeout_s: int = 40,
        nav_timeout_s: int = 60,
        logger=None,
    ) -> None:
        self._proxy_url_factory = proxy_url_factory
        self._max_retries = max_retries
        self._base_delay_ms = base_delay_ms
        self._challenge_timeout_s = challenge_timeout_s
        self._nav_timeout_s = nav_timeout_s
        self._log = logger
        self._playwright = None
        self._browser = None

    def _warn(self, msg: str) -> None:
        if self._log:
            self._log.warning(msg)

    def _info(self, msg: str) -> None:
        if self._log:
            self._log.info(msg)

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        # Headful under xvfb: the Apify Playwright image wraps the process in
        # xvfb-run, so a "real" windowed Chromium is available. Headless mode is
        # heavily fingerprinted by Cloudflare and gets hard-blocked.
        self._browser = await self._playwright.chromium.launch(
            headless=False, args=LAUNCH_ARGS
        )

    async def close(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._playwright:
                await self._playwright.stop()

    async def _new_proxy(self) -> Optional[str]:
        if self._proxy_url_factory is None:
            return None
        try:
            return await self._proxy_url_factory()
        except Exception as exc:  # noqa: BLE001
            self._warn(f"Could not obtain proxy URL: {exc}")
            return None

    async def _try_click_turnstile(self, page) -> None:
        """If the challenge shows an interactive Turnstile checkbox, click it."""
        try:
            iframe_el = await page.query_selector(
                "iframe[src*='challenges.cloudflare.com']"
            )
            if not iframe_el:
                return
            box = await iframe_el.bounding_box()
            if not box:
                return
            # The checkbox sits near the left edge of the widget.
            await page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
        except Exception:  # noqa: BLE001
            pass

    async def _wait_challenge(self, page) -> bool:
        """Wait for the Cloudflare challenge to self-resolve. True if cleared."""
        deadline = asyncio.get_event_loop().time() + self._challenge_timeout_s
        clicked = False
        while asyncio.get_event_loop().time() < deadline:
            try:
                title = await page.title()
            except Exception:  # noqa: BLE001
                title = ""
            if not _is_challenge_title(title):
                # Give the post-challenge redirect a moment to settle.
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:  # noqa: BLE001
                    pass
                return True
            # After a few seconds of no auto-resolve, try the Turnstile checkbox once.
            if not clicked and (deadline - asyncio.get_event_loop().time()) < (
                self._challenge_timeout_s - 6
            ):
                await self._try_click_turnstile(page)
                clicked = True
            await asyncio.sleep(1.5)
        return False

    async def fetch(self, url: str, *, referer: Optional[str] = None) -> FetchResult:
        last: Optional[FetchResult] = None

        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                base = self._base_delay_ms * (attempt + 1)
                await asyncio.sleep((base + random.randint(0, self._base_delay_ms)) / 1000.0)

            proxy_cfg = _parse_proxy(await self._new_proxy())
            context = None
            try:
                context = await self._browser.new_context(
                    proxy=proxy_cfg,
                    locale="en-US",
                    viewport={"width": 1366, "height": 768},
                    user_agent=None,  # keep Chromium's own coherent UA
                )
                await context.add_init_script(STEALTH_INIT_JS)
                page = await context.new_page()
                page.set_default_navigation_timeout(self._nav_timeout_s * 1000)

                if referer:
                    await page.set_extra_http_headers({"Referer": referer})

                resp = await page.goto(url, wait_until="domcontentloaded")
                status = resp.status if resp else 0

                title = await page.title()
                if _is_challenge_title(title):
                    self._info(
                        f"[{attempt + 1}/{self._max_retries + 1}] Cloudflare challenge "
                        f"detected on {url}, waiting up to {self._challenge_timeout_s}s..."
                    )
                    cleared = await self._wait_challenge(page)
                    if not cleared:
                        self._warn(
                            f"[{attempt + 1}/{self._max_retries + 1}] challenge did NOT "
                            f"clear on {url}"
                        )
                        last = FetchResult(status, await page.content(), page.url, ok=False, blocked=True)
                        continue

                html = await page.content()
                final_url = page.url

                if _looks_blocked_html(html):
                    self._warn(
                        f"[{attempt + 1}/{self._max_retries + 1}] page still looks "
                        f"blocked after challenge wait: {url}"
                    )
                    last = FetchResult(status, html, final_url, ok=False, blocked=True)
                    continue

                return FetchResult(200, html, final_url, ok=True, blocked=False)

            except Exception as exc:  # noqa: BLE001
                self._warn(
                    f"[{attempt + 1}/{self._max_retries + 1}] browser error on {url}: {exc}"
                )
                last = FetchResult(0, "", url, ok=False, blocked=True)
            finally:
                if context:
                    try:
                        await context.close()
                    except Exception:  # noqa: BLE001
                        pass

        return last or FetchResult(0, "", url, ok=False, blocked=True)
