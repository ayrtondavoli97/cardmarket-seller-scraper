"""Camoufox-based fetch layer for Cardmarket.

Camoufox is an anti-detection build of Firefox designed to pass Cloudflare's
managed challenge that vanilla Playwright Chromium cannot. It ships fingerprint
patches at the C++ level (not JS spoofing), rotates a coherent fingerprint, and
with humanize=True moves the cursor like a human — which is what clears the
non-interactive "Just a moment..." loop.

Same fetch() -> FetchResult interface as the other clients, so main.py is
agnostic to which engine is used.
"""

from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, Optional

from camoufox.async_api import AsyncCamoufox

from .client import FetchResult
from .browser_client import (
    _parse_proxy,
    _is_challenge_title,
    _looks_blocked_html,
)


class CamoufoxCardmarketClient:
    def __init__(
        self,
        *,
        proxy_url_factory: Optional[Callable[[], Awaitable[Optional[str]]]],
        max_retries: int = 3,
        base_delay_ms: int = 1200,
        challenge_timeout_s: int = 45,
        nav_timeout_s: int = 60,
        block_images: bool = True,
        logger=None,
    ) -> None:
        self._proxy_url_factory = proxy_url_factory
        self._max_retries = max_retries
        self._base_delay_ms = base_delay_ms
        self._challenge_timeout_s = challenge_timeout_s
        self._nav_timeout_s = nav_timeout_s
        self._block_images = block_images
        self._log = logger
        self._cm = None
        self._browser = None

    def _warn(self, msg: str) -> None:
        if self._log:
            self._log.warning(msg)

    def _info(self, msg: str) -> None:
        if self._log:
            self._log.info(msg)

    async def start(self) -> None:
        # Browser is launched lazily on first fetch (needs a proxy URL).
        return None

    async def _new_proxy(self) -> Optional[str]:
        if self._proxy_url_factory is None:
            return None
        try:
            return await self._proxy_url_factory()
        except Exception as exc:  # noqa: BLE001
            self._warn(f"Could not obtain proxy URL: {exc}")
            return None

    async def _launch(self) -> None:
        proxy_cfg = _parse_proxy(await self._new_proxy())
        self._cm = AsyncCamoufox(
            headless=False,          # attach to the image's xvfb display
            proxy=proxy_cfg,
            geoip=True,              # align locale/timezone/geo to the proxy exit IP
            humanize=True,           # human-like cursor movement
            block_images=self._block_images,
            i_know_what_im_doing=True,
        )
        self._browser = await self._cm.__aenter__()

    async def _relaunch(self) -> None:
        await self.close()
        await self._launch()

    async def _ensure_browser(self) -> None:
        if self._browser is None:
            await self._launch()

    async def close(self) -> None:
        try:
            if self._cm is not None:
                await self._cm.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._cm = None
            self._browser = None

    async def _wait_challenge(self, page) -> bool:
        deadline = asyncio.get_event_loop().time() + self._challenge_timeout_s
        while asyncio.get_event_loop().time() < deadline:
            try:
                title = await page.title()
            except Exception:  # noqa: BLE001
                title = ""
            if not _is_challenge_title(title):
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:  # noqa: BLE001
                    pass
                return True
            # Nudge: small mouse move keeps humanize/Turnstile happy.
            try:
                await page.mouse.move(
                    random.randint(200, 900), random.randint(200, 600)
                )
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(2.0)
        return False

    async def fetch(self, url: str, *, referer: Optional[str] = None) -> FetchResult:
        last: Optional[FetchResult] = None

        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                base = self._base_delay_ms * (attempt + 1)
                await asyncio.sleep((base + random.randint(0, self._base_delay_ms)) / 1000.0)

            try:
                await self._ensure_browser()
            except Exception as exc:  # noqa: BLE001
                self._warn(f"[{attempt + 1}/{self._max_retries + 1}] launch error: {exc}")
                last = FetchResult(0, "", url, ok=False, blocked=True)
                continue

            page = None
            try:
                page = await self._browser.new_page()
                page.set_default_navigation_timeout(self._nav_timeout_s * 1000)
                if referer:
                    await page.set_extra_http_headers({"Referer": referer})

                resp = await page.goto(url, wait_until="domcontentloaded")
                status = resp.status if resp else 0

                title = await page.title()
                if _is_challenge_title(title):
                    self._info(
                        f"[{attempt + 1}/{self._max_retries + 1}] CF challenge on {url}, "
                        f"waiting up to {self._challenge_timeout_s}s (camoufox)..."
                    )
                    if not await self._wait_challenge(page):
                        self._warn(
                            f"[{attempt + 1}/{self._max_retries + 1}] challenge did NOT clear"
                        )
                        last = FetchResult(status, await page.content(), page.url, ok=False, blocked=True)
                        await page.close()
                        await self._relaunch()
                        continue

                html = await page.content()
                final_url = page.url
                if _looks_blocked_html(html):
                    self._warn(
                        f"[{attempt + 1}/{self._max_retries + 1}] still blocked after wait"
                    )
                    last = FetchResult(status, html, final_url, ok=False, blocked=True)
                    await page.close()
                    await self._relaunch()
                    continue

                await page.close()
                return FetchResult(200, html, final_url, ok=True, blocked=False)

            except Exception as exc:  # noqa: BLE001
                self._warn(
                    f"[{attempt + 1}/{self._max_retries + 1}] browser error on {url}: {exc}"
                )
                last = FetchResult(0, "", url, ok=False, blocked=True)
                if page is not None:
                    try:
                        await page.close()
                    except Exception:  # noqa: BLE001
                        pass
                await self._relaunch()

        return last or FetchResult(0, "", url, ok=False, blocked=True)
