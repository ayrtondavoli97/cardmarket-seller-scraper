"""Parsers for Cardmarket search pages and product (article) tables.

Cardmarket's markup changes periodically. To stay resilient, every extraction
tries a list of candidate selectors and falls back to None rather than throwing.
When a page yields zero rows and debugSaveHtml is on, the raw HTML is dumped so
selectors can be patched from a real sample.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

# Condition ordering, best -> worst.
CONDITION_RANK = {
    "MT": 7,  # Mint
    "NM": 6,  # Near Mint
    "EX": 5,  # Excellent
    "GD": 4,  # Good
    "LP": 3,  # Light Played
    "PL": 2,  # Played
    "PO": 1,  # Poor
}

_PRICE_RE = re.compile(r"([\d.,]+)")


def make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _first_text(node, selectors: list[str]) -> Optional[str]:
    for sel in selectors:
        found = node.select_one(sel)
        if found:
            text = found.get_text(strip=True)
            if text:
                return text
    return None


def _first_attr(node, selectors: list[str], attrs: list[str]) -> Optional[str]:
    for sel in selectors:
        found = node.select_one(sel)
        if not found:
            continue
        for attr in attrs:
            val = found.get(attr)
            if val:
                return val.strip() if isinstance(val, str) else val
    return None


def parse_price(raw: Optional[str]) -> Optional[float]:
    """Parse a Cardmarket price string like '1.234,56 €' -> 1234.56."""
    if not raw:
        return None
    m = _PRICE_RE.search(raw)
    if not m:
        return None
    num = m.group(1)
    # European format: '.' thousands, ',' decimals.
    if "," in num:
        num = num.replace(".", "").replace(",", ".")
    else:
        # No decimal comma; a lone dot could be decimal already.
        num = num
    try:
        return float(num)
    except ValueError:
        return None


def is_product_page(soup: BeautifulSoup) -> bool:
    """A single-product page carries an article/offer table."""
    return bool(
        soup.select_one(".article-row")
        or soup.select_one("#table .article-row")
        or soup.select_one("[id^='articleRow']")
    )


def parse_search_results(html: str, base_url: str, limit: int) -> list[str]:
    """Return absolute product URLs from a search results page."""
    soup = make_soup(html)
    urls: list[str] = []
    seen: set[str] = set()

    # Product links point at /Products/Singles/... (singles) primarily.
    anchors = soup.select(
        "a[href*='/Products/Singles/'], "
        "table a[href*='/Products/'], "
        ".table-body a[href*='/Products/']"
    )
    for a in anchors:
        href = a.get("href")
        if not href:
            continue
        if "/Products/Search" in href:
            continue
        abs_url = urljoin(base_url, href)
        # Keep only product detail URLs, not category listings.
        if "/Products/" not in abs_url:
            continue
        if abs_url in seen:
            continue
        seen.add(abs_url)
        urls.append(abs_url)
        if len(urls) >= limit:
            break

    return urls


def _extract_condition(row) -> Optional[str]:
    # Condition badge, e.g. <span class="badge ... st_SpecialIcon">NM</span>
    txt = _first_text(
        row,
        [
            ".article-condition span",
            "a.article-condition span",
            ".product-attributes .badge",
            "span.badge",
        ],
    )
    if txt:
        code = txt.strip().upper()[:2]
        if code in CONDITION_RANK:
            return code
    return None


def _extract_card_language(row) -> Optional[str]:
    return _first_attr(
        row,
        [
            ".product-attributes span.icon[aria-label]",
            ".product-attributes span.icon[data-bs-original-title]",
            ".product-attributes span.icon[title]",
            "span.icon[data-original-title]",
        ],
        ["aria-label", "data-bs-original-title", "title", "data-original-title"],
    )


def _extract_seller_country(row) -> Optional[str]:
    return _first_attr(
        row,
        [
            ".seller-info span.icon[aria-label]",
            ".col-seller span.icon[data-bs-original-title]",
            ".seller-info span.icon[title]",
        ],
        ["aria-label", "data-bs-original-title", "title"],
    )


def _extract_flags(row) -> dict:
    """Detect foil / first edition / signed / altered from attribute icons."""
    attr_area = row.select_one(".product-attributes") or row
    titles = []
    for icon in attr_area.select("span.icon, span[data-bs-original-title], span[title]"):
        for a in ("aria-label", "data-bs-original-title", "title", "data-original-title"):
            v = icon.get(a)
            if v:
                titles.append(v.lower())
    joined = " ".join(titles)
    return {
        "isFoil": "foil" in joined,
        "isFirstEdition": "first edition" in joined or "1st" in joined,
        "isSigned": "signed" in joined,
        "isAltered": "altered" in joined,
    }


def parse_offers(html: str, product_url: str, limit: int) -> tuple[dict, list[dict]]:
    """Return (product_meta, offers) from a product page."""
    soup = make_soup(html)

    card_name = _first_text(soup, ["h1", ".page-title-container h1"])
    # Strip trailing count that Cardmarket sometimes appends to the h1.
    if card_name:
        card_name = re.sub(r"\s*\d+\s*$", "", card_name).strip()

    expansion = _first_attr(
        soup,
        ["div.expansion-symbol[aria-label]", "a[href*='/Expansions/'] span", ".expansion a"],
        ["aria-label", "title"],
    ) or _first_text(soup, ["a[href*='/Expansions/']", ".expansion-name"])

    product_meta = {
        "cardName": card_name,
        "expansion": expansion,
        "productUrl": product_url,
    }

    rows = soup.select(".article-row")
    if not rows:
        rows = soup.select("[id^='articleRow']")

    offers: list[dict] = []
    for row in rows:
        seller_name = _first_text(
            row,
            [".seller-name a", ".col-seller a", "span.seller-name a", ".seller-info a"],
        )
        seller_type = _first_attr(
            row,
            [".seller-name .icon[aria-label]", ".seller-info .icon[data-bs-original-title]"],
            ["aria-label", "data-bs-original-title", "title"],
        )
        seller_rating = _first_attr(
            row,
            [".seller-info .icon[aria-label]", "span.sell-count[aria-label]"],
            ["aria-label", "data-bs-original-title", "title"],
        )

        price_raw = _first_text(
            row,
            [".price-container .color-primary", ".col-offer .price-container", ".price-container"],
        )
        amount_raw = _first_text(
            row,
            [".amount-container .item-count", ".col-offer .amount-container", ".amount-container"],
        )

        flags = _extract_flags(row)
        offer = {
            "sellerName": seller_name,
            "sellerType": seller_type,
            "sellerRating": seller_rating,
            "sellerCountry": _extract_seller_country(row),
            "price": parse_price(price_raw),
            "priceRaw": price_raw,
            "currency": "EUR",
            "condition": _extract_condition(row),
            "language": _extract_card_language(row),
            "quantity": _to_int(amount_raw),
            **flags,
        }
        offers.append(offer)
        if len(offers) >= limit:
            break

    return product_meta, offers


def _to_int(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    m = re.search(r"\d+", raw)
    return int(m.group(0)) if m else None
