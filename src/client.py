"""HTTP client for Cardmarket.

Uses curl_cffi with browser TLS/JA3 impersonation, which is the most reliable
lightweight way to get past Cloudflare's TLS fingerprint check. Combined with
Apify RESIDENTIAL proxies it clears the standard managed challenge in most
cases. Interactive JS challenges cannot be solved here — those runs will need a
browser-based fallback (see README).
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from curl_cffi import requests as cffi_requests

# Rotate across a few recent Chrome fingerprints on retry.
IMPERSONATE_PROFILES = [
    "chrome124",
    "chrome123",
    "chrome120",
    "chrome116",
]

# A Cloudflare interstitial / block tends to be small and contains these markers.
CLOUDFLARE_MARKERS = (
    "Just a moment...",
    "cf-browser-verification",
    "cf_chl_opt",
    "Attention Required! | Cloudflare",
    "Checking if the site connection is secure",
)


@dataclass
class FetchResult:
    status_code: int
    text: str
    url: str
    ok: bool
    blocked: bool


def _looks_blocked(status_code: int, text: str) -> bool:
    if status_code in (403, 429, 503):
        return True
    head = text[:4000]
    return any(marker in head for marker in CLOUDFLARE_MARKERS)


class CardmarketClient:
    def __init__(
        self,
        *,
        proxy_url_factory: Optional[Callable[[], Awaitable[Optional[str]]]],
        max_retries: int = 4,
        base_delay_ms: int = 1200,
        timeout: int = 45,
        logger=None,
    ) -> None:
        self._proxy_url_factory = proxy_url_factory
        self._max_retries = max_retries
        self._base_delay_ms = base_delay_ms
        self._timeout = timeout
        self._log = logger

    def _log_info(self, msg: str) -> None:
        if self._log:
            self._log.info(msg)

    def _log_warn(self, msg: str) -> None:
        if self._log:
            self._log.warning(msg)

    async def _new_proxy(self) -> Optional[str]:
        if self._proxy_url_factory is None:
            return None
        try:
            return await self._proxy_url_factory()
        except Exception as exc:  # noqa: BLE001
            self._log_warn(f"Could not obtain proxy URL: {exc}")
            return None

    async def _sleep_jittered(self, attempt: int) -> None:
        # Exponential-ish backoff with jitter, floored at the polite base delay.
        base = self._base_delay_ms * (attempt + 1)
        jitter = random.randint(0, self._base_delay_ms)
        await asyncio.sleep((base + jitter) / 1000.0)

    def _headers(self, referer: Optional[str]) -> dict:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,it;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin" if referer else "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    async def fetch(self, url: str, *, referer: Optional[str] = None) -> FetchResult:
        """Fetch a URL, rotating proxy IP + impersonation profile on block/error."""
        last_result: Optional[FetchResult] = None

        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                await self._sleep_jittered(attempt)

            proxy = await self._new_proxy()
            impersonate = IMPERSONATE_PROFILES[attempt % len(IMPERSONATE_PROFILES)]
            proxies = {"http": proxy, "https": proxy} if proxy else None

            try:
                # curl_cffi is sync; run it off the event loop.
                resp = await asyncio.to_thread(
                    cffi_requests.get,
                    url,
                    headers=self._headers(referer),
                    impersonate=impersonate,
                    proxies=proxies,
                    timeout=self._timeout,
                    allow_redirects=True,
                )
            except Exception as exc:  # noqa: BLE001
                self._log_warn(
                    f"[{attempt + 1}/{self._max_retries + 1}] request error on {url}: {exc}"
                )
                last_result = FetchResult(0, "", url, ok=False, blocked=True)
                continue

            text = resp.text or ""
            blocked = _looks_blocked(resp.status_code, text)
            final_url = str(resp.url) if getattr(resp, "url", None) else url

            if resp.status_code == 200 and not blocked:
                return FetchResult(resp.status_code, text, final_url, ok=True, blocked=False)

            self._log_warn(
                f"[{attempt + 1}/{self._max_retries + 1}] "
                f"status={resp.status_code} blocked={blocked} url={url} "
                f"(profile={impersonate})"
            )
            last_result = FetchResult(resp.status_code, text, final_url, ok=False, blocked=blocked)

        return last_result or FetchResult(0, "", url, ok=False, blocked=True)
