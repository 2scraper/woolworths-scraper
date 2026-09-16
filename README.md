# woolworths-scraper

[![release](https://img.shields.io/github/v/release/2scraper/woolworths-scraper?sort=semver)](https://github.com/2scraper/woolworths-scraper/releases)
[![tests](https://github.com/2scraper/woolworths-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/woolworths-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/woolworths-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/woolworths-scraper/actions/workflows/canary.yml)
[![python](https://img.shields.io/badge/python-3.9%20%E2%80%93%203.13-blue)](pyproject.toml)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20pyppeteer-lightgrey)](#engines)
[![runs without an account](https://img.shields.io/badge/runs%20without-an%20account-brightgreen)](#what-you-need-before-this-works)

Scrapes product listings from **[woolworths.com.au](https://www.woolworths.com.au)**,
Australia's largest supermarket — search results and category browse, with
prices, unit prices, specials, health star ratings and allergen data. JSON or
CSV, one row per product.

```bash
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium

python3 playwright_scraper.py \
  --url "https://www.woolworths.com.au/shop/browse/bakery" \
  --pages 3 --format both --out bakery
```

```
[+] Saved 117 products -> bakery.json
[+] Saved 117 products -> bakery.csv
[+] Wrote run metadata -> bakery.meta.json (status=complete)
```

That count moves between runs — three pages of this category gave 117 on one
run and 111 on another the same afternoon — because the number of promoted
rows the site injects varies and they are deduplicated away.

---

## What you need before this works

**Nothing, if your address is residential.** No account, no API key, no
proxy. A run from an ordinary home connection gets the full catalogue.

**One thing, if it is not: run it with a visible browser.** That is the
default, so usually you do nothing — but it is worth knowing why, because it
is the single most useful measurement in this README.

Measured 2026-09-16 from one datacentre address, four URLs, each fetched both
ways within the same minute:

| | served (HTTP 200) | refused (Akamai 403) |
|---|---|---|
| **headful** | **4 of 4** | 0 |
| **headless** | 0 | **4 of 4** |

So `--headless` is offered and will almost certainly fail from a server. On a
machine with no display, give the browser a virtual one rather than going
headless — `xvfb-run -a python3 playwright_scraper.py …`, which is exactly
what the shipped `Dockerfile` does.

**A residential exit, if your address is refused anyway.** Akamai scores the
address. `--proxy`, `--proxy-file`, or `--cdp-endpoint` for the
[2Captcha Scraping Browser API](https://2captcha.com). An **Australian exit
is not required**: a US exit was served the full catalogue normally, which is
not true of every site in this family.

**A 2Captcha key buys you the proxy and the Scraping Browser here, not a
solve.** Being precise about that, because it decides whether to spend money:
no captcha has been observed on this site at all. Counted across every
capture taken for this repo — `sitekey`, `data-sitekey`, `recaptcha`,
`hcaptcha` and `turnstile` are each **0** on every served page and on both
denial pages. Akamai's refusal is ~400 bytes of plain HTML with no widget on
it, so there is nothing there for a solver at any price to answer. That is a
statement about the page, not about the product.

What is in place if that ever changes, stated exactly, because "already
implements" is easy to read as more than it is. `captcha_solver.py` builds
`RecaptchaV2Task`, `RecaptchaV2TaskProxyless`, `RecaptchaV3TaskProxyless`,
`RecaptchaV2EnterpriseTaskProxyless` and `TurnstileTaskProxyless`, and
carries the `turnstile.render` interception script a Cloudflare **Challenge**
page needs — that page publishes no sitekey, because Cloudflare passes
`sitekey`, `action`, `cData` and `chlPageData` to `turnstile.render()` once
and keeps nothing. **No engine installs that script yet**, so the Turnstile
path is a builder without a caller: reCAPTCHA is wired end to end, Turnstile
is not. Wiring it is ~15 lines per engine and wants a live challenge to
verify against, which this site has never served. `foodpanda-scraper` in this
family has it wired and measured.

---

## What it collects

One row per product, 41 columns. The coverage figures are from a 660-row
corpus (589 distinct products) drawn on 2026-09-16 from five search terms and
three category nodes — a snapshot of one day, not a property of the site.

| Column | | Coverage |
|---|---|---|
| `source` `scraped_at` `url` `sku` `title` | the family prefix, same across every repo in this family | 100% |
| `brand` | | 93.9% |
| `description` `variety` `barcode` | | 100% / 79.1% / 100% |
| `price` `currency` | AUD, derived from the host — the API never states a currency | 99.4% |
| `original_price` `savings_amount` `discount_pct` | **only when the product is really discounted** — see below | 18.0% |
| `price_source` | `api` or `api+dom` — whether the rendered tile confirmed the price | 100% |
| `cup_price` `cup_measure` `cup_string` | unit pricing (`$2.35 / 1L`) — the only way to compare a 2L bottle with a 3L one | 99.4% |
| `package_size` `unit` | | 100% |
| `is_on_special` `is_half_price` | | 17.6% / 6.5% true |
| `is_sponsored` `ad_status` `offer_id` | **promoted ads, and you will want to filter on this** | 17.0% true |
| `is_available` `is_in_stock` `is_purchasable` `supply_limit` | | 99.4% |
| `department` `category` `subcategory` | Woolworths' own merchandising hierarchy | 99.5% |
| `health_star_rating` | the Australian Health Star Rating, 0.5–5 | 53.6% |
| `dietary_claims` `allergy_statement` `ingredients` `storage_instructions` | | 74.4% / 71.8% / 83.2% / 65.3% |
| `image_url` | | 100% |
| `data_source` `page` `position` | provenance | 100% |

[`sample_output.json`](sample_output.json) and
[`sample_output.csv`](sample_output.csv) are twelve rows cut from a real run,
unedited.

### Traps that look like bugs

Four things surprise people, and all four are the site rather than the
scraper.

**Most rows have no discount, and that is correct.** `WasPrice` is populated
on 100% of rows and **equals `Price` on 537 of 660** of them — those products
are simply not on special. Reporting it as an original price would put a 0%
discount on four products in five, so `original_price` is null unless
`WasPrice` actually exceeds `Price` (119 of 660 rows), and `discount_pct` is
computed from the two rather than read off a badge.

**Some of your rows are advertisements.** Woolworths injects promoted
products into both listings and serves **the same ones on every page** — of 8
stockcodes appearing twice across three pages of one search, all 8 were
sponsored and 0 were organic. `is_sponsored` marks them. The scraper dedupes
on `sku` so you get each product once.

**A listing never runs out of ads, only of products.** Past its last real
page the API keeps answering HTTP 200 with `Success: true` and nothing but
promoted rows — on a 16-page category, page 17 returned 1 row, page 18
returned 8 and page 99 returned 8, every one sponsored. The scraper therefore
stops when a page adds no new ORGANIC product, never when a page comes back
empty. A run that asked for more pages than exist still reports `complete`.

**There are no ratings.** The API ships a `Rating` object on every product
and it is empty on every product: `RatingCount`, `ReviewCount`, `RatingSum`
and `Average` were 0 on all 660 listing rows and on 10 rows fetched from the
by-stockcode endpoint — 670 rows, not one non-zero. There is no rating
column, because a column that is null on every row of every run is worse than
a missing one. `country_of_origin` is absent for the same reason: present on
all 660 rows, null on all 660.

---

## Modes

The mode is inferred from the URL; passing one that disagrees is an error
rather than an override.

```bash
# search
--url "https://www.woolworths.com.au/shop/search/products?searchTerm=milk"

# a category, and its child nodes
--url "https://www.woolworths.com.au/shop/browse/bakery"
--url "https://www.woolworths.com.au/shop/browse/fruit-veg/fruit"

# specials is a category like any other
--url "https://www.woolworths.com.au/shop/browse/specials"
```

A single product page is refused with the reason: the same 115-field object
is already on every listing row, so a product mode would add a mode without
adding a column.

`woolworths.co.nz` (Woolworths New Zealand, formerly Countdown) and
`woolworths.co.za` (Woolworths Holdings, South Africa) are **different
companies on different platforms**, and both are refused by name.

---

## How it works, and why it is unusual

**Woolworths puts no product data in its HTML.** Not a little — none. A
served category page is 872 KB whose visible text is 3,735 characters of
navigation chrome; the word "Banana" appears zero times on
`/shop/browse/fruit-veg`, and there are zero `/shop/productdetails/` anchors
anywhere in the markup. The `application/ld+json` count is zero on search and
specials, and exactly one on a category page, where it is a `BreadcrumbList`.

The catalogue is behind two doors at once:

1. **It arrives as JSON**, over the site's own API, after the shell paints.
2. **It renders into shadow DOM** — 67 `<wc-product-tile>` custom elements
   whose content lives in open shadow roots that `page.content()` does not
   serialise.

So this scraper **navigates once**, which is what makes Akamai issue a
session, and then asks the site's own API for each page from inside that
loaded page. The browser's cookies and TLS fingerprint come along for free; a
second HTTP client would have neither.

```
POST /apis/ui/Search/products              search
POST /apis/ui/browse/category              a category node, by opaque id
GET  /apis/ui/PiesCategoriesWithSpecials   slug -> opaque node id
```

That last one matters: a category id is **opaque and not derivable from the
slug** — `bakery` is `1_DEB537E`, `fruit-veg` is `1-E5BEE36E`, and the two do
not even share a separator. The scraper resolves it against the site's own
tree before asking for page 1, and refuses the URL with the reason if the
slug is not in it. Sending the slug where the id belongs returns HTTP 200
with zero products, which is indistinguishable from a real empty category.

**The rendered tiles are read as a second view, not a second parser.** They
confirm the API's price; where the two agree the row's `price_source` becomes
`api+dom`, and where they disagree the row is left exactly as the API gave it
and a warning names the sku. Across the live runs taken for this README every
checkable row agreed — 36 of 36 on one search, 44 of 44 on one category — and
the engines warn below 90%.

There is deliberately **no HTML fallback parser**, and the measurement above
is why: it would find zero products on every page, forever.

---

## Engines

All three produce identical rows. Measured on the same 2-page search:
Selenium and pyppeteer each returned 79 rows, the same columns in the same
order, and every non-volatile value identical on all 79 shared products.

| | |
|---|---|
| **`playwright_scraper.py`** | The primary. The only one with working `--concurrency`. |
| `selenium_scraper.py` | Parity. Cannot authenticate a proxy (`--proxy-server` has nowhere to put a password) and cannot use an authenticated CDP endpoint (`debuggerAddress` takes a bare `host:port`). |
| `puppeteer_scraper.py` | Parity. Can authenticate both. pyppeteer is effectively unmaintained and its own README points at Playwright. |

Install exactly one: their pins are mutually unsatisfiable (playwright and
pyppeteer disagree on `pyee`, pyppeteer and selenium on `urllib3`). Use a
virtualenv per engine if you want more than one.

Because every page is an integer in a request body rather than a cursor,
`--concurrency` is genuinely meaningful here — page 5 can be fetched without
reading page 4. It is refused with `--cdp-endpoint`, where the Scraping
Browser allows one live connection per profile.

---

## Exit codes

| | |
|---|---|
| `0` | ok |
| `1` | crash |
| `2` | bad usage |
| `3` | blocked — Akamai refused. Distinct from "found nothing". |
| `4` | zero products |
| `5` | remote API error |
| `6` | partial — some page failed; `pages_failed` in the sidecar says which |

**A run that finds nothing writes nothing**, so last night's good output
survives a bad night. `--allow-empty` opts out. Every run writes
`<out>.meta.json` beside its data with `status`, `stop_reason`, which pages
failed by number, `dom_confirm`, `sponsored_rows` and
`unauthorised_redirect`.

`diff_runs.py` compares two runs by `sku` and refuses to compare runs that
are not both `complete`, because a partial run's unfetched pages would
otherwise read as delisted products.

---

## Full flag list

```
--url --mode --pages --category --format --out --delay --retries --retry-delay
--concurrency --locale --browser-channel --proxy --proxy-file --proxy-rotate
--proxy-shuffle --proxy-block-retries --twocaptcha-key --captcha-api
--solve-captcha --min-score --fingerprint --fp-tags --fp-country
--cdp-endpoint --cdp-connect-timeout --allow-empty --dump-html
--headful/--headless
```

`--browser-channel`, `--cdp-connect-timeout`, `--fingerprint`, `--fp-tags`,
`--fp-country` and `--locale` are not on every engine; `--chromium-path` is
pyppeteer only. The differences are asserted by the test suite in both
directions, so the exception list is the documentation.

`--dump-html PATH` writes two files per page: the document, and
**`PATH.api.json`** — the payload the rows actually come from. On this site
the second one is the useful file.

---

## Configuration

Credentials go in `.env` next to the scripts, never on a command line, where
`ps` can read them.

```bash
cp .env.example .env
python3 env_config.py     # prints what was picked up, without printing secrets
```

Precedence, highest first: **explicit flag → exported environment variable →
`.env` → default.** Variables: `TWOCAPTCHA_KEY`, `WOOLWORTHS_CDP_ENDPOINT`,
`WOOLWORTHS_PROXY`, `WOOLWORTHS_URL`. An unrecognised key in `.env` is
reported rather than ignored.

---

## Tests

```bash
python3 smoke_test.py       # the offline suite, no engine required
pytest                      # same checks, wrapped as one test
```

The suite passes with no engine library installed at all and records the
skips; CI installs each engine in its own virtualenv and fails if the
matching group skips. Fixtures are cut from real captures by
`make_fixtures.py` and scrubbed of advertising tokens.

The canary runs one real 3-page run a day, but **skips with a notice unless a
`WOOLWORTHS_PROXY` secret is set** — Akamai refuses GitHub's runners, and a
check that is always red teaches everyone to ignore checks.

---

## Legal and scope

Public product listings only, at a polite rate, from a site that serves them
to anonymous visitors. No login, no personal data, no attempt to reach
anything a shopper could not see. Woolworths' terms and `robots.txt` are your
responsibility to read; so is the rate you run at.

MIT licensed. Part of the [2scraper](https://github.com/2scraper) family,
which shares a row schema, exit codes and output contract across sites.
