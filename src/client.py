"""HTTP client for Cardmarket.

Uses curl_cffi with browser TLS/JA3 impersonation. Crucially, it lets the
impersonation own the full browser header set (User-Agent, sec-ch-ua client
hints, header order) instead of hand-rolling headers — a mismatch there is a
classic cause of an instant Cloudflare 403 even when the TLS fingerprint is
right.

Each attempt uses a fresh Session that first warms up on the Cardmarket
homepage (to pick up the __cf_bm cookie), then requests the target with those
cookies, from a fresh proxy IP + impersonation profile.

Interactive JS challenges ("Just a moment...") cannot be solved by an HTTP
client. If those show up, a browser-based fallback is required (see README).
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

HOME_URL = "https://www.cardmarket.com/en"

# Markers that indicate a Cloudflare interstitial / challenge / block.
CLOUDFLARE_MARKERS = (
    "Just a moment...",
    "cf-browser-verification",
    "cf_chl_opt",
    "cf-challenge",
    "Attention Required! | Cloudflare",
    "Checking if the site connection is secure",
    "Enable JavaScript and cookies to continue",
    "Sorry, you have been blocked",
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
        warmup: bool = True,
        logger=None,
    ) -> None:
        self._proxy_url_factory = proxy_url_factory
        self._max_retries = max_retries
        self._base_delay_ms = base_delay_ms
        self._timeout = timeout
        self._warmup = warmup
        self._log = logger

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

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
        base = self._base_delay_ms * (attempt + 1)
        jitter = random.randint(0, self._base_delay_ms)
        await asyncio.sleep((base + jitter) / 1000.0)

    def _blocking_fetch(
        self, url: str, proxy: Optional[str], impersonate: str, referer: Optional[str]
    ):
        """Runs in a worker thread. Warms up, then fetches with a shared session."""
        session = cffi_requests.Session(impersonate=impersonate)
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}

        # Warm-up: load the homepage first so Cloudflare hands us __cf_bm and the
        # session looks like a real navigation. Failures here are non-fatal.
        if self._warmup:
            try:
                session.get(HOME_URL, timeout=self._timeout, allow_redirects=True)
            except Exception:  # noqa: BLE001
                pass

        # Only add Referer/Accept-Language on top of the impersonated defaults;
        # do NOT override UA / sec-ch-ua / Accept / header order.
        extra = {"Accept-Language": "en-US,en;q=0.9,it;q=0.8"}
        if referer:
            extra["Referer"] = referer
        elif self._warmup:
            extra["Referer"] = HOME_URL

        return session.get(url, headers=extra, timeout=self._timeout, allow_redirects=True)

    async def fetch(self, url: str, *, referer: Optional[str] = None) -> FetchResult:
        """Fetch a URL, rotating proxy IP + impersonation profile on block/error."""
        last_result: Optional[FetchResult] = None

        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                await self._sleep_jittered(attempt)

            proxy = await self._new_proxy()
            impersonate = IMPERSONATE_PROFILES[attempt % len(IMPERSONATE_PROFILES)]

            try:
                resp = await asyncio.to_thread(
                    self._blocking_fetch, url, proxy, impersonate, referer
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
