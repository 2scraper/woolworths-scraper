# Woolworths Scraper

Open-source scraper for [woolworths.com.au](https://www.woolworths.com.au) — Australia's largest supermarket chain. Extract product data by keyword search or category browse: names, brands, prices, ratings, images, package sizes, on-sale status, and more. Outputs JSON and CSV.

**GitHub:** `github.com/2scraper/woolworths-scraper`

---

## Data collected

| Field | Description |
|---|---|
| `stockcode` | Woolworths internal product ID |
| `barcode` | EAN/UPC barcode |
| `name` | Full product name (`DisplayName`) |
| `brand` | Brand name |
| `description` | Full product description (HTML stripped) |
| `url` | Product page URL |
| `image_url` | High-resolution image URL |
| `price` | Current shelf price (AUD) |
| `was_price` | Original price before discount |
| `savings_amount` | Discount amount (e.g. `0.5`) |
| `special_price` | Equals `price` when `is_special=true`, else null |
| `cup_string` | Unit price string (e.g. `"$0.66 / 100G"`) |
| `cup_measure` | Unit measure (e.g. `"100G"`) |
| `is_special` | Currently on promotion |
| `is_half_price` | Half-price promotion |
| `promotion` | Promotion label (e.g. `"Save $0.50"`) |
| `package_size` | Package weight / volume / count |
| `unit` | Unit type (e.g. `"Each"`) |
| `category` | Top-level SAP department (e.g. `"Proprietary Bakery"`) |
| `subcategory` | Most specific Pies category (e.g. `"Packaged Bread & Bakery"`) |
| `departments` | All navigation departments, comma-separated (e.g. `"Lunch Box, Bakery"`) |
| `is_available` | Available for order |
| `is_in_stock` | In stock |
| `is_purchasable` | Can be added to cart |
| `rating` | Average customer rating |
| `rating_count` | Number of ratings |
| `review_count` | Number of written reviews |
| `health_star_rating` | Australian Health Star Rating (1–5) |
| `lifestyle_claims` | Dietary claims (e.g. `"High Fibre,Low Sugar,Vegan"`) |
| `allergy_statement` | Allergen information |
| `ingredients` | Ingredients list |
| `storage_instructions` | Storage instructions |
| `country_of_origin` | Country of origin |
| `scraped_at` | UTC timestamp |

---

## How it works

Woolworths uses **Akamai Bot Manager** (TLS fingerprinting, browser environment checks, session-bound JWTs). The scraper uses a two-phase approach:

**Phase 1 — Session harvest:** A real browser visits the homepage to pass Akamai's challenge and receive session cookies (`wow-auth-token`, `w-rctx`, `bm_sv`, `_abck`). If the browser is blocked by the proxy (Akamai returns `Access Denied` to browser TLS fingerprint but allows plain requests), the scraper automatically falls back to a requests-based cookie harvest — which is sufficient for unauthenticated product search.

**Phase 2 — API extraction:** Session cookies are used to call Woolworths' internal REST API directly (`POST /apis/ui/Search/products`). No DOM parsing — clean structured JSON responses with complete product data.

**Observed behaviour with residential proxies:** Some proxy endpoints pass Akamai for `requests` but not for browser-level TLS. Use `--skip-browser` to go directly to requests-based harvest in this case.

---

## Quick start

### Playwright (primary — Python)

```bash
pip install playwright requests 2captcha-python
playwright install chromium

# Search by keyword
python woolworths_playwright.py --search "milk" --max-pages 5

# Skip browser warm-up (use when browser gets Access Denied through proxy)
python woolworths_playwright.py --search "milk" --skip-browser --proxy http://user:pass@host:port

# Browse a category
python woolworths_playwright.py --category "dairy-eggs-fridge" --max-pages 3

# Specials only, sorted by price, with CSV output
python woolworths_playwright.py --search "cheese" --specials --sort PriceAsc --csv cheese.csv
```

### Selenium (Python alternative)

```bash
pip install selenium webdriver-manager requests 2captcha-python

python woolworths_selenium.py --search "yogurt" --max-pages 3
python woolworths_selenium.py --search "milk" --skip-browser --proxy http://user:pass@host:port
python woolworths_selenium.py --category "fruit-veg" --specials
```

### Puppeteer (Node.js alternative)

```bash
npm install puppeteer axios csv-writer yargs

node woolworths_puppeteer.js --search "chicken" --max-pages 5
node woolworths_puppeteer.js --search "milk" --skip-browser --proxy http://user:pass@host:port
node woolworths_puppeteer.js --category "meat-seafood-deli" --debug
```

---

## CLI reference

| Flag | Default | Description |
|---|---|---|
| `--search KEYWORD` | — | Product search term |
| `--category SLUG` | — | Category slug (see below) |
| `--max-pages N` | `5` | Maximum pages to scrape (36 products/page) |
| `--output FILE` | `woolworths_products.json` | JSON output path |
| `--csv FILE` | — | Also export as CSV |
| `--specials` | `false` | Specials/deals only |
| `--sort` | `TraderRelevance` | `TraderRelevance` / `PriceAsc` / `PriceDesc` / `Name` |
| `--proxy URL` | — | Proxy URL (`http://user:pass@host:port`) |
| `--skip-browser` | `false` | Skip browser warm-up, use requests directly |
| `--2captcha-key KEY` | — | 2captcha API key (or env `TWOCAPTCHA_API_KEY`) |
| `--headed` | `false` | Show browser window |
| `--debug` | `false` | Save screenshots + raw JSON to `debug/` |

### Common category slugs

| Slug | Category |
|---|---|
| `fruit-veg` | Fruit & Veg |
| `dairy-eggs-fridge` | Dairy, Eggs & Fridge |
| `meat-seafood-deli` | Meat, Seafood & Deli |
| `bakery` | Bakery |
| `pantry` | Pantry |
| `snacks-confectionery` | Snacks & Confectionery |
| `drinks` | Drinks |
| `frozen` | Frozen |
| `health-beauty` | Health & Beauty |
| `household` | Household |
| `baby-child` | Baby & Child |
| `pet` | Pet |

---

## Output format

### JSON

```json
[
  {
    "stockcode": 6071665,
    "barcode": "9310199012717",
    "name": "Tip Top The One Digestive Health Soft White Sliced Loaf 680g",
    "brand": "Tip Top",
    "price": 4.5,
    "was_price": 5.0,
    "savings_amount": 0.5,
    "special_price": 4.5,
    "cup_string": "$0.66 / 100G",
    "is_special": true,
    "promotion": "Save $0.50",
    "package_size": "680g",
    "category": "Proprietary Bakery",
    "subcategory": "Packaged Bread & Bakery",
    "departments": "Lunch Box, Bakery",
    "health_star_rating": "5",
    "lifestyle_claims": "High Fibre,Low Fat,Low Sugar,Source of Fibre",
    "scraped_at": "2026-06-14T23:20:00Z"
  }
]
```

---

## Proxy behaviour

Woolworths Akamai Bot Manager behaves differently for browser TLS vs plain HTTPS:

| Request type | Akamai response |
|---|---|
| Plain `requests` with residential proxy | ✓ Allowed — returns session cookies |
| Browser (Playwright/Selenium) with same proxy | May return `Access Denied` |
| Browser without proxy (datacenter IP) | `ERR_TIMED_OUT` — dropped at TCP level |

**Recommendation:** use `--skip-browser` with residential proxies from [2prx.com](https://2prx.com). The scraper works fully without browser warm-up — plain requests are sufficient to obtain Akamai session cookies for product search.

For workloads requiring full authenticated sessions (`wow-auth-token`), use the [2captcha Anti-Detect Browser](https://2captcha.com/anti-detect-browser).

---

## Legal notice

For research, price monitoring, and educational purposes. Review Woolworths' Terms of Service before use.

---

## License

MIT — see [LICENSE](LICENSE)
