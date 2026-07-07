# Cardmarket Seller Listings Scraper

Go from a **free-text search** all the way down to **every seller offer** on
[Cardmarket](https://www.cardmarket.com): price, condition, language, quantity,
seller, seller country and product flags (foil / first edition / signed /
altered).

```
search query  ->  product pages  ->  seller offer table  ->  dataset
```

## ⚠️ Cardmarket is behind an aggressive Cloudflare managed challenge

Plain HTTP clients (curl_cffi) and even self-hosted stealth browsers (Camoufox)
get looped on the "Just a moment..." challenge from datacenter/residential IPs.
The reliable path is a **managed unblocker** that solves Cloudflare server-side.

**Recommended setup (default):**
1. Get an API key from **ZenRows** (free tier: 1,000 credits) or **Scrapfly**.
2. Set `unblockerProvider` = `zenrows` (or `scrapfly`) and paste the key into
   `unblockerApiKey` (stored encrypted).
3. Run. The unblocker returns the final rendered HTML and the parsers do the rest.

Cost is typically ~$1-3 per 1,000 successful requests (residential + JS render),
so this is best for personal / low-volume use, not a high-volume commercial run.

## Fetch layers

| unblockerProvider | Image (Dockerfile) | Result |
|---|---|---|
| `zenrows` / `scrapfly` | `.actor/Dockerfile` (lean, default) | Works — Cloudflare solved server-side |
| `none` + `useBrowser` | `.actor/Dockerfile.browser` | Camoufox/Chromium fallback — usually blocked by Cardmarket |
| `none` + no browser | either | curl_cffi HTTP — will NOT pass the challenge |

The default lean image ships without browser deps. To use the browser fallback,
build with `.actor/Dockerfile.browser`.

## Input (main fields)

- `game` — YuGiOh, Pokemon, Magic, OnePiece, Lorcana, …
- `searchQueries` — card names / free-text searches
- `productUrls` — direct product URLs (skips search)
- `unblockerProvider` / `unblockerApiKey` / `unblockerCountry`
- `maxProductsPerQuery`, `maxOffersPerProduct`
- `minCondition` (MT>NM>EX>GD>LP>PL>PO), `cardLanguage`, `sellerCountry`
- `debugSaveHtml` — dump HTML to key-value store on 0-row pages

## Output (one item per offer)

```json
{
  "game": "YuGiOh",
  "cardName": "Blue-Eyes White Dragon",
  "expansion": "Legend of Blue Eyes White Dragon",
  "productUrl": "https://www.cardmarket.com/en/YuGiOh/Products/Singles/...",
  "sellerName": "SomeSeller",
  "sellerCountry": "Italy",
  "price": 1.5,
  "currency": "EUR",
  "condition": "NM",
  "language": "English",
  "quantity": 3,
  "isFoil": false
}
```

## Roadmap / tuning

- Selector patching: parsers are defensive and dump HTML on 0-row pages so
  selectors can be updated from a real sample.
- Deep offer pagination (AJAX "show more") is a v2 item.

## Legal

Extracts only publicly visible product/price info; no auth bypass, no private
endpoints, no personal data beyond public seller display names. You are
responsible for complying with Cardmarket's ToS and applicable law.
