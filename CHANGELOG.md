# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims at [Semantic Versioning](https://semver.org/) as closely
as a CLI toolkit can. A **patch** release means fixes; it does not promise
that every flag and every default is frozen, so a behaviour-changing default
can land in one — and when it does, the entry leads with it in a blockquote
rather than leaving anyone to discover it from their own output.

## [Unreleased]

> **A parser failure was reported as a COMPLETE run.** If a run ever logged
> "the parser produced NONE" it still exited 0 with `status: complete` and an
> empty `pages_failed`, so a scheduled job accepted a truncated file as a
> good one. Re-run anything that matters. Found by a third-party audit on
> 2026-10-08 and reproduced here on the audited commit.

> **Any run with a proxy pool CRASHED when it met a refusal.** The engines
> called `pool.rotate()`, which is the rotation mode STRING rather than a
> method, so a refused page raised `TypeError: 'str' object is not callable`
> — exit 1, in all three engines, on the one path a pool exists for. Not in
> the audit; found while reproducing its third finding, which could not be
> reached because of it.

> **`diff_runs.py` could not see a price change, and now can.** It was
> medium-scraper's file: it tracked `claps`, `responses`, `reading_time_min`,
> `word_count`, `content_chars`, `is_paywalled` and `publication`, and no
> Woolworths row has any of them. Every price move diffed as "0 changed", and
> `--fail-on-change` never fired. If you run it from cron, expect it to start
> reporting.

### Fixed

- **A page whose parser produced nothing is no longer a completed page.**
  `parse_failed` was being SET and then discarded: `PageOutcome.ok`
  consulted only `load_failed` and `blocked_by`, so the page counted as
  fetched-with-zero-rows and `advance_page` read that as the end of the
  listing. Reproduced with a renamed payload container: exit 0, `complete`,
  `pagination_exhausted`, `pages_failed: []`. Now exit 6, `partial`,
  `stop_reason: parser_found_nothing`, the page listed by number, and the
  rows already gathered kept. This is the defect CLAUDE.md §26 records
  binance-scraper hitting for real.
- **`pool.advance(reason)`, not `pool.rotate()`** — see the note above. The
  reason is required, and a first fix that called `pool.advance()` swapped
  one `TypeError` for another; the check that caught that reads the AST
  rather than the source text, because the first version of IT matched the
  comment explaining the bug.
- **`--proxy-block-retries` reaches the retry policy.** It parsed,
  defaulted and changed nothing — a CLI flag with no reader. `0` is now
  honoured as a meaningful value rather than treated as unset, and the flag
  applies only with a pool, because without one there is nothing to rotate
  to.
- **The page cap is inclusive, and both branches share one plan.**
  `page_cap_reached` used `>=`, so `--pages 200` fetched 199 while `--help`
  promised 200; and the concurrent branch built its own unbounded
  `range(2, args.pages + 1)`, so `--pages 201 --concurrency 2` planned page
  201 against a cap of 200. One `planned_pages()`, built before the
  sequential/concurrent choice.
- **`diff_runs.py` refuses two runs of different listings, or of different
  fulfilment stores.** Two complete runs of different categories passed
  every guard and diffed as 100% churn — same mode, same host. The sidecar
  already carried `start_url`; it was simply unread.
- **`diff_runs.py` tracks this site's columns**: `price`, `original_price`,
  `discount_pct`, `currency`, `cup_price`, `is_on_special`, `is_half_price`
  and `is_in_stock`. A price that moves together with `price_source` goes to
  `source_changed`, not `changed`. The summary prints price and currency
  instead of claps and author, and the help text no longer talks about claps.
  A new check moves one value at a time on a real `sample_output.json` row
  and requires exactly one reported change. It fails against the old file.
- **`.dockerignore` ignored another repo's output** (`vrbo_products.*`). It
  now ignores this repo's default `--out` prefix, `woolworths_products.*`.

- `SECURITY.md` said this project has no releases or version tags; it has
  both. "Supported versions" now names the latest release and `main`.
- `captcha_solver.py`'s docstring pointed at a "No DataDome solver" section
  that does not exist in this repo (it came with the copied core). Removed.

### Added

- **`store_id` on every row**, read from the payload's own
  `FulfilmentStoreId` (100% populated, and 1101 on all 660 rows of the
  measured corpus — one store, because a session that has not chosen one
  gets Woolworths' default). Woolworths prices per fulfilment store, so
  without this column two runs cannot tell a price CHANGE from a different
  store's price. This repo does not implement choosing a store; the column
  says which one you were served, which is the half a price diff needs
  before it can be trusted.
- **`listing` and `store_ids` in the sidecar.** `--category` was parsed,
  defaulted from the URL, and then read by nothing — not a column, not the
  sidecar, not the log.
### Changed

- **The canary runs live, daily, with no credential.** It was gated on a
  `WOOLWORTHS_PROXY` secret that has never been set, so every green run
  since this repo went public was the skip branch — a green badge over an
  untested claim. The gate came from an inference on the wrong axis
  ("Akamai refuses datacentre addresses"); what was actually measured is
  that the site refuses HEADLESS and serves HEADFUL.

  Measured 2026-10-08 on a bare runner with no secret of any kind: Azure
  `westus3`, headful under Xvfb, **109 rows, `status: complete`, exit 0,
  every assertion passed**. §§21/24: a canary that can pass without a
  credential must never be gated on one. A secret is still used where one
  is set, through the environment rather than a command line.

- **The README and the blocked-run advice no longer say the address is the
  problem.** Two datacentre addresses have now been served headful — a
  German VPS and a US GitHub runner — so a reader whose run is refused is
  told to check the browser before buying an exit.


## [0.1.2] — 2026-09-16

### Fixed

- **The README no longer says `captcha_solver.py` "already implements …
  Cloudflare Turnstile".** It builds `TurnstileTaskProxyless` and carries the
  `turnstile.render` interception script, but **no engine installs that
  script**, so the Turnstile path is a builder with no caller. reCAPTCHA is
  wired end to end; Turnstile is not. The old phrasing was true of the module
  and false of the tool, and the difference only shows up on the one day it
  matters. The README now names the five task types, says which are wired,
  and says wiring the rest is ~15 lines per engine that wants a live
  challenge to verify against — which this site has never served.

### Added

- **`test_a_built_captcha_task_type_is_reachable_or_documented`** — every
  task type the solver builds must be reachable from an engine, or named in
  the README as not yet wired. Asserted as a pairing rather than a keyword
  search so it cannot go quiet by accident, and it also fails on the phrase
  "already implements", which is what made the old sentence readable as more
  than it was. Verified by control: claiming the script is installed turns
  the suite red.

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
