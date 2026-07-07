# Cardmarket Seller Listings Scraper

Go from a **free-text search** all the way down to **every seller offer** on
[Cardmarket](https://www.cardmarket.com) — Europe's largest trading-card
marketplace — with price, condition, language, quantity, seller, seller country
and product flags (foil / first edition / signed / altered).

Unlike the trend-page scrapers already on the Store (which only read the two
"Weekly Top Cards" / "Best Bargains" pages) and unlike URL-only detail scrapers,
this actor does the full pipeline:

```
search query  ->  product pages  ->  seller offer table  ->  dataset
```

## What it does

- Runs each search query against Cardmarket search and opens the matching
  product pages (with a cap you control).
- Also accepts **direct product URLs** if you already have them — more reliable
  and skips the search step.
- Explodes each product into its seller offers and returns one row per offer.
- Filters by minimum condition, card language and seller country.

## Input

| Field | Description |
|---|---|
| `game` | Cardmarket section: YuGiOh, Pokemon, Magic, OnePiece, Lorcana, … |
| `searchQueries` | List of card names / free-text searches |
| `productUrls` | Direct product URLs (optional; skips search) |
| `siteLanguage` | URL locale (en/it/de/fr/es) |
| `maxProductsPerQuery` | Cap on products opened per query |
| `maxOffersPerProduct` | Cap on offers per product page |
| `minCondition` | Drop offers below this condition (MT>NM>EX>GD>LP>PL>PO) |
| `cardLanguage` | Keep only offers in this card language |
| `sellerCountry` | Keep only sellers from this country name/code |
| `proxyConfiguration` | **Use RESIDENTIAL** — Cardmarket sits behind Cloudflare |
| `maxConcurrency` | Parallel requests (keep low) |
| `requestDelayMs` | Polite jittered delay per request |
| `maxRetries` | Retries with fresh IP + fingerprint on block |
| `debugSaveHtml` | Dump raw HTML to key-value store on 0-row pages |

## Output

One dataset item per seller offer:

```json
{
  "game": "YuGiOh",
  "cardName": "Blue-Eyes White Dragon",
  "expansion": "Legend of Blue Eyes White Dragon",
  "productUrl": "https://www.cardmarket.com/en/YuGiOh/Products/Singles/...",
  "sellerName": "SomeSeller",
  "sellerType": "Professional",
  "sellerRating": "...",
  "sellerCountry": "Italy",
  "price": 1.5,
  "priceRaw": "1,50 €",
  "currency": "EUR",
  "condition": "NM",
  "language": "English",
  "quantity": 3,
  "isFoil": false,
  "isFirstEdition": false,
  "isSigned": false,
  "isAltered": false
}
```

## How it beats Cloudflare

The client uses [`curl_cffi`](https://github.com/lexiforest/curl_cffi) with
rotating Chrome TLS/JA3 impersonation, routed through Apify **RESIDENTIAL**
proxies, with a fresh IP + fingerprint on each retry. This clears Cardmarket's
standard managed challenge in most cases.

**Limitation:** interactive JavaScript challenges cannot be solved by an HTTP
client. If a run reports everything blocked, re-run with `debugSaveHtml=true`,
inspect the dump in the key-value store, and (roadmap) switch to the
browser-based fallback.

## Roadmap / known tuning points

- **Selector patching:** Cardmarket's markup shifts. Parsers are defensive and
  dump HTML on 0-row pages so selectors can be updated from a real sample.
- **Deep offer pagination:** v1 reads the offers rendered on the product page.
  AJAX "show more" pagination for very long offer lists is a v2 item.
- **Browser fallback** for interactive Cloudflare challenges.

## Legal

This actor extracts only publicly visible product and price information. It does
not bypass authentication, access private endpoints, or store personal data
beyond public seller display names. You are responsible for complying with
Cardmarket's Terms of Service and applicable law (GDPR, copyright) in your
jurisdiction.
