# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims at [Semantic Versioning](https://semver.org/) as closely
as a CLI toolkit can. A **patch** release means fixes; it does not promise
that every flag and every default is frozen, so a behaviour-changing default
can land in one — and when it does, the entry leads with it in a blockquote
rather than leaving anyone to discover it from their own output.

## [Unreleased]

## [0.1.1] — 2026-09-16

> **`--mode category` was broken in v0.1.0 on the Playwright engine**, which
> is the engine the README recommends. It died on its first API call with
> `AttributeError: 'Page' object has no attribute 'page'`. Search mode was
> unaffected. Upgrade if you used v0.1.0 for anything under
> `/shop/browse/`.

### Fixed

- `_resolve_target` passed a page where `_fetch_api` expects a session, on
  the Playwright engine only. The retry work in v0.1.0 changed that
  signature and landed in two engines out of three; the post-change
  verification used a search URL, which never reaches the line, so the bug
  shipped.

### Added

- A check comparing the ARGUMENT SPELLING of same-named helpers across the
  three engines, so a signature change that lands in some files and not
  others fails offline. Nothing existing could catch this one: arity was
  identical, so the call bound fine, and the shared-module binding check
  does not cover a module's own helpers.
- It immediately found a second divergence — `_read_tiles` took a page on
  Playwright and a session on its twins. All three now take the session.

Verified after the fix: every engine against BOTH modes — playwright 76/81,
selenium 80/81, puppeteer 76/81 rows, all `complete`, all exit 0.

## [0.1.0] — 2026-09-16

Rebuilt on the 2scraper family architecture. The previous contents of this
repository were three standalone single-file scripts sharing no row schema,
no exit codes and no tests with the rest of the family; nothing of that
generation remains.

> **If you used the previous scripts, read this.** They advertised `rating`,
> `rating_count`, `review_count` and `country_of_origin` columns. All four
> are gone, because all four were null on every row: Woolworths ships a
> `Rating` object on every product and it is empty on every product (0 on all
> 660 listing rows measured, and on 10 rows from the by-stockcode endpoint —
> 670 rows, not one non-zero), and `AdditionalAttributes.countryoforigin` is
> present on all 660 rows and null on all 660. `health_star_rating`,
> `ingredients`, `allergy_statement`, `dietary_claims` and
> `storage_instructions` ARE real and are new here — they live in
> `AdditionalAttributes` rather than at the top level, which is why the
> previous generation promised them and could not fill them.

### Added

- Two modes, `search` and `category`, inferred from the URL. Both answer from
  the site's own API with the same 115-field product object.
- Three engines — Playwright (primary), Selenium and pyppeteer — sharing
  `product_parser.py`, `page_flow.py`, `output_writer.py`, `proxy_pool.py`
  and `captcha_solver.py`, so exit codes and run status cannot drift between
  them. Verified: on one 2-page search, Selenium and pyppeteer each returned
  79 rows with identical columns and identical values on all 79.
- 41-column row schema with the family's five-column prefix, JSON and CSV,
  and a `<out>.meta.json` sidecar per run carrying `status`, `stop_reason`,
  the failed page numbers, `dom_confirm`, `sponsored_rows` and
  `unauthorised_redirect`.
- `is_sponsored` / `ad_status` / `offer_id`, because Woolworths injects
  promoted products into both listings and serves the same ones on every
  page.
- Unit pricing (`cup_price`, `cup_measure`, `cup_string`), which is the point
  of scraping a supermarket.
- A DOM second view: the engines read the rendered tiles through their open
  shadow roots and CONFIRM the API's price, recording `price_source` as
  `api+dom`. A disagreement leaves the row alone and warns with the sku.
- `--concurrency` that actually works on the Playwright engine, because every
  page is an integer in a request body rather than a cursor.
- An offline suite of 67 checks that passes with no engine library installed,
  fixtures cut from real captures by `make_fixtures.py`, and a canary that
  skips with a notice rather than failing where it cannot pass.

### Site behaviour this release exists to handle

Each of these was measured on 2026-09-16 and each would otherwise be a silent
wrong answer:

- **Headful vs headless is the difference between data and a 403.** From one
  datacentre address, four URLs each way: headful served 4 of 4, headless
  refused 4 of 4. Headful is the default in all three engines, and the
  Dockerfile installs Xvfb rather than passing `--headless`.
- **`navigator.webdriver` gets the app to bounce the browser to
  `/unauthorisederror`** — while the document still answers 200 and the API
  still answers 200 with real products. A run without
  `--disable-blink-features=AutomationControlled` therefore returns the right
  rows, reports success, and silently loses the DOM cross-check. All three
  engines pass the flag; the sidecar records it if it happens anyway.
- **`WasPrice` equals `Price` on 537 of 660 rows.** `original_price` is null
  unless it genuinely exceeds `Price`, and `discount_pct` is computed rather
  than read, so the output does not claim a 0% discount on four products in
  five.
- **A listing never runs out of ads.** Past its last real page the API
  answers 200 with `Success: true` and nothing but promoted rows — page 17 of
  a 16-page category returned 1 row, page 18 returned 8, page 99 returned 8.
  The walk stops on new ORGANIC rows, never on "the page was empty".
- **A category id is opaque** (`bakery` is `1_DEB537E`) and is resolved
  against the site's own tree before page 1 is requested. Sending the slug
  returns 200 with zero products, which is indistinguishable from a real
  empty category.
- **Two markers that looked obviously right are not markers.** `akamai`
  occurs once on every page the site serves and zero times on its denial page
  — exactly inverted — and `couldn't find any` occurs four times on every
  page, good or empty, because it ships in the JS bundle; taken as a
  no-results marker it made a 2,205-result search report itself empty. Both
  are pinned as non-markers by the suite.
- **Akamai's refusal arrives in two encodings** — `errors.edgesuite.net` from
  a browser, `errors&#46;edgesuite&#46;net` from an HTTP client — so markers
  are matched against an unescaped, bounded prefix.

### Not included, with the measurement

- **No HTML fallback parser.** A served category page is 872 KB whose visible
  text is 3,735 characters of navigation chrome, with zero
  `/shop/productdetails/` anchors and no product JSON-LD. A fallback would
  find zero products on every page, forever.
- **No Scraper API engine.** The path works — HTTP 200, 619 KB, not blocked —
  and returns **zero products**, because it returns served HTML and this
  site's served HTML has no catalogue in it. Adding `waitFor` on the tile
  selector changed nothing. That is a property of the site, not a limit of
  the product, and shipping the client would have been dead code that looks
  load-bearing.
- **No product-detail mode.** `/apis/ui/products/{stockcode}` returns the
  same object that is already on every listing row, so the mode would add a
  mode without adding a column.
- **No rating, review or country-of-origin columns.** See the note at the top
  of this release.
- **No in-store price column.** `InstorePrice` equalled `Price` on 656 of 656
  rows carrying both. The in-store and online special FLAGS did disagree on
  13 of 660 rows; if the prices themselves ever diverge, that is the column
  to add back.

### Note on captcha

No captcha has been observed on this site: `sitekey`, `recaptcha`, `hcaptcha`
and `turnstile` are each 0 on every served page and on both denial pages, and
Akamai's refusal is ~400 bytes of plain HTML with no widget on it. No solve is
ever attempted and nothing is charged.

That is a statement about what these pages carry, not about what a solver can
do. `captcha_solver.py` implements reCAPTCHA v2/v3, enterprise reCAPTCHA
(`RecaptchaV2EnterpriseTaskProxyless`) and Cloudflare Turnstile
(`TurnstileTaskProxyless`); if Woolworths ever renders one,
`page_flow.STATE_POLICY` is the single line to change.

[Unreleased]: https://github.com/2scraper/woolworths-scraper/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/2scraper/woolworths-scraper/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/2scraper/woolworths-scraper/releases/tag/v0.1.0
