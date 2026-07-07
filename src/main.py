"""Cardmarket Seller Listings Scraper — main entrypoint.

Pipeline:
  search query  ->  product URLs  ->  seller offers  ->  dataset

Distinct from the existing Apify Cardmarket actors (which only scrape the two
trend pages, or require you to pre-supply product URLs): this one goes from a
free-text search all the way down to every seller offer, with condition,
language, quantity, seller and country, plus filtering.
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote

from apify import Actor

from .client import CardmarketClient
from .parsers import (
    CONDITION_RANK,
    is_product_page,
    make_soup,
    parse_offers,
    parse_search_results,
)

BASE = "https://www.cardmarket.com"


def build_search_url(site_lang: str, game: str, query: str) -> str:
    return f"{BASE}/{site_lang}/{game}/Products/Search?searchString={quote(query)}"


def passes_filters(offer: dict, *, min_condition: str, card_language: str, seller_country: str) -> bool:
    if min_condition and offer.get("condition"):
        min_rank = CONDITION_RANK.get(min_condition, 0)
        off_rank = CONDITION_RANK.get(offer["condition"], 0)
        if off_rank and off_rank < min_rank:
            return False
    if card_language:
        lang = (offer.get("language") or "").lower()
        if card_language.lower() not in lang:
            return False
    if seller_country:
        country = (offer.get("sellerCountry") or "").lower()
        if seller_country.lower() not in country:
            return False
    return True


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}

        game = actor_input.get("game", "YuGiOh")
        site_lang = actor_input.get("siteLanguage", "en")
        search_queries = actor_input.get("searchQueries") or []
        product_url_sources = actor_input.get("productUrls") or []
        max_products = int(actor_input.get("maxProductsPerQuery", 5))
        max_offers = int(actor_input.get("maxOffersPerProduct", 50))
        min_condition = actor_input.get("minCondition", "PO")
        card_language = actor_input.get("cardLanguage", "") or ""
        seller_country = actor_input.get("sellerCountry", "") or ""
        max_concurrency = int(actor_input.get("maxConcurrency", 3))
        request_delay_ms = int(actor_input.get("requestDelayMs", 1200))
        max_retries = int(actor_input.get("maxRetries", 4))
        debug_save_html = bool(actor_input.get("debugSaveHtml", False))

        # Normalize direct product URLs (accepts [{url:..}] or ["..."]).
        direct_urls: list[str] = []
        for item in product_url_sources:
            if isinstance(item, dict):
                url = item.get("url")
            else:
                url = item
            if url:
                direct_urls.append(url)

        if not search_queries and not direct_urls:
            Actor.log.warning("No searchQueries and no productUrls provided — nothing to do.")
            return

        # Proxy setup.
        proxy_cfg_input = actor_input.get("proxyConfiguration")
        proxy_configuration = await Actor.create_proxy_configuration(actor_proxy_input=proxy_cfg_input)

        async def proxy_url_factory():
            if proxy_configuration is None:
                return None
            return await proxy_configuration.new_url()

        client = CardmarketClient(
            proxy_url_factory=proxy_url_factory,
            max_retries=max_retries,
            base_delay_ms=request_delay_ms,
            logger=Actor.log,
        )

        semaphore = asyncio.Semaphore(max_concurrency)
        stats = {"products": 0, "offers": 0, "blocked": 0, "empty": 0}

        async def dump_html(tag: str, html: str) -> None:
            if not debug_save_html:
                return
            key = f"debug_{tag}_{stats['products']}_{stats['empty']}".replace("/", "_")[:60]
            try:
                await Actor.set_value(key, html, content_type="text/html")
                Actor.log.info(f"Saved debug HTML to key-value store: {key}")
            except Exception as exc:  # noqa: BLE001
                Actor.log.warning(f"Failed to save debug HTML: {exc}")

        async def scrape_product(url: str, referer: str | None = None) -> None:
            async with semaphore:
                result = await client.fetch(url, referer=referer)
                if not result.ok:
                    if result.blocked:
                        stats["blocked"] += 1
                        Actor.log.warning(f"Blocked/failed product page: {url}")
                        await dump_html("blocked_product", result.text)
                    return

                product_meta, offers = parse_offers(result.text, url, max_offers)
                if not offers:
                    stats["empty"] += 1
                    Actor.log.warning(f"0 offers parsed on {url} (selectors may need patching)")
                    await dump_html("product", result.text)
                    return

                stats["products"] += 1
                pushed = 0
                for offer in offers:
                    if not passes_filters(
                        offer,
                        min_condition=min_condition,
                        card_language=card_language,
                        seller_country=seller_country,
                    ):
                        continue
                    record = {**product_meta, **offer, "game": game}
                    await Actor.push_data(record)
                    pushed += 1
                stats["offers"] += pushed
                Actor.log.info(f"{product_meta.get('cardName') or url}: {pushed} offers pushed")

        # Resolve search queries into product URLs.
        product_targets: list[str] = list(direct_urls)

        for query in search_queries:
            search_url = build_search_url(site_lang, game, query)
            Actor.log.info(f"Searching: {query} -> {search_url}")
            result = await client.fetch(search_url)
            if not result.ok:
                stats["blocked"] += 1
                Actor.log.warning(f"Search blocked/failed for '{query}'")
                await dump_html("blocked_search", result.text)
                continue

            soup = make_soup(result.text)
            # Exact-match searches redirect straight to the product page.
            if is_product_page(soup):
                Actor.log.info(f"'{query}' resolved directly to a product page.")
                product_targets.append(result.url)
                continue

            found = parse_search_results(result.text, BASE, max_products)
            if not found:
                stats["empty"] += 1
                Actor.log.warning(f"No products found for '{query}'")
                await dump_html("search", result.text)
                continue
            Actor.log.info(f"'{query}': {len(found)} products found")
            product_targets.extend(found)

        # De-duplicate while preserving order.
        seen: set[str] = set()
        unique_targets = [u for u in product_targets if not (u in seen or seen.add(u))]
        Actor.log.info(f"Scraping offers from {len(unique_targets)} product page(s)")

        await asyncio.gather(*(scrape_product(u) for u in unique_targets))

        Actor.log.info(
            f"Done. products={stats['products']} offers={stats['offers']} "
            f"blocked={stats['blocked']} empty={stats['empty']}"
        )
        if stats["blocked"] and stats["offers"] == 0:
            Actor.log.warning(
                "All requests were blocked and nothing was scraped. Cardmarket's "
                "Cloudflare challenge likely needs RESIDENTIAL proxies (or a browser "
                "fallback). Check proxyConfiguration and re-run with debugSaveHtml=true."
            )
