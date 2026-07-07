"""Managed-unblocker fetch layer for Cardmarket.

Cardmarket runs an aggressive Cloudflare managed challenge that self-hosted
stealth browsers (even Camoufox) can't reliably clear from Apify residential
IPs. A managed unblocker API (ZenRows / Scrapfly) solves the TLS fingerprint,
JS challenge and Turnstile server-side and returns the final rendered HTML.

Same fetch() -> FetchResult interface as the other clients. You send the target
URL to the provider with your API key; you get HTML back. Typical cost is
~$1-3 per 1000 successful requests (residential + JS render), so keep this for
personal / low-volume use.
"""

from __future__ import annotations

import asyncio
import random
from typing import Optional

from curl_cffi import requests as cffi_requests

from .client import FetchResult

CHALLENGE_MARKERS = (
    "Just a moment...",
    "cf_chl_opt",
    "Enable JavaScript and cookies to continue",
    "Attention Required! | Cloudflare",
    "Sorry, you have been blocked",
)

ZENROWS_ENDPOINT = "https://api.zenrows.com/v1/"
SCRAPFLY_ENDPOINT = "https://api.scrapfly.io/scrape"


def _looks_blocked_html(html: str) -> bool:
    head = (html or "")[:4000]
    return any(m in head for m in CHALLENGE_MARKERS)


class UnblockerClient:
    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        country: str = "",
        max_retries: int = 2,
        base_delay_ms: int = 1000,
        timeout: int = 120,
        logger=None,
    ) -> None:
        self._provider = provider
        self._api_key = api_key
        self._country = country
        self._max_retries = max_retries
        self._base_delay_ms = base_delay_ms
        self._timeout = timeout
        self._log = logger

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def _warn(self, msg: str) -> None:
        if self._log:
            self._log.warning(msg)

    # --- provider request builders (run in worker thread) ------------------

    def _zenrows(self, url: str):
        params = {
            "url": url,
            "apikey": self._api_key,
            "js_render": "true",
            "premium_proxy": "true",
            "wait": "2500",
        }
        if self._country:
            params["proxy_country"] = self._country
        resp = cffi_requests.get(ZENROWS_ENDPOINT, params=params, timeout=self._timeout)
        # On success ZenRows returns the target HTML with 200; on failure a
        # non-200 with a JSON error body (quota, blocked, etc.).
        if resp.status_code == 200:
            return 200, resp.text, url
        return resp.status_code, resp.text, url

    def _scrapfly(self, url: str):
        params = {
            "key": self._api_key,
            "url": url,
            "asp": "true",           # anti scraping protection (CF/Turnstile)
            "render_js": "true",
            "proxy_pool": "public_residential_pool",
        }
        if self._country:
            params["country"] = self._country
        resp = cffi_requests.get(SCRAPFLY_ENDPOINT, params=params, timeout=self._timeout)
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return resp.status_code, resp.text or "", url
        result = (data or {}).get("result") or {}
        content = result.get("content", "") or ""
        origin_status = result.get("status_code", resp.status_code)
        final_url = result.get("url", url)
        if resp.status_code == 200 and content:
            return origin_status, content, final_url
        # Surface Scrapfly-level error message for the log.
        msg = (data or {}).get("message") or resp.text or ""
        return resp.status_code, msg[:500], final_url

    async def fetch(self, url: str, *, referer: Optional[str] = None) -> FetchResult:
        last: Optional[FetchResult] = None

        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                base = self._base_delay_ms * (attempt + 1)
                await asyncio.sleep((base + random.randint(0, self._base_delay_ms)) / 1000.0)

            try:
                if self._provider == "scrapfly":
                    status, text, final_url = await asyncio.to_thread(self._scrapfly, url)
                else:
                    status, text, final_url = await asyncio.to_thread(self._zenrows, url)
            except Exception as exc:  # noqa: BLE001
                self._warn(
                    f"[{attempt + 1}/{self._max_retries + 1}] {self._provider} request "
                    f"error on {url}: {exc}"
                )
                last = FetchResult(0, "", url, ok=False, blocked=True)
                continue

            blocked = _looks_blocked_html(text)
            if status == 200 and text and not blocked:
                return FetchResult(200, text, final_url, ok=True, blocked=False)

            self._warn(
                f"[{attempt + 1}/{self._max_retries + 1}] {self._provider} "
                f"status={status} blocked={blocked} url={url} :: {text[:180]}"
            )
            last = FetchResult(status, text, final_url, ok=False, blocked=blocked)

        return last or FetchResult(0, "", url, ok=False, blocked=True)
