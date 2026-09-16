# Troubleshooting

Every number here was measured on 2026-09-16, and each says what it was
measured on. A figure with no date and no method is a guess wearing a fact's
clothes, so if you find one in this file, it is a bug.

---

## "Everything returns HTTP 403 / `Access Denied` / exit 3"

Two causes, and the first one is free to rule out.

**1. You passed `--headless`.** This is the big one on this site.

Measured from one datacentre address, four URLs, each fetched both ways in
the same minute:

| | served | refused |
|---|---|---|
| headful | **4 of 4** | 0 |
| headless | 0 | **4 of 4** (Akamai 403) |

Headful is the default in all three engines, so this only bites if you asked
for headless or you are in a container. On a machine with no display, give
the browser a virtual one instead of going headless:

```bash
xvfb-run -a python3 playwright_scraper.py --url "..." --pages 3
```

The shipped `Dockerfile` does exactly this, which is why it installs Xvfb
rather than passing `--headless` the way every other image in this family
does.

**2. Your address is a datacentre one.** Akamai refuses those. The scraper
says so and tells you what to try:

```
Akamai refused the request ('Access Denied' on the page). Try --headful
first ... Then a residential exit: --proxy/--proxy-file, or --cdp-endpoint
for the Scraping Browser API. An Australian exit is NOT required — a US
exit was served this site normally.
```

That last clause is measured: both a US exit and an AU exit through the
Scraping Browser returned the full catalogue. Unlike some sites in this
family, the exit country does not have to match the site.

A captcha solve buys nothing here, and it is worth being precise about why:
Akamai's denial page is about 400 bytes of plain HTML — a title, an `<h1>`,
one sentence and a reference number. Counted on it: zero `data-sitekey`
attributes, zero iframes, zero references to reCAPTCHA, hCaptcha, Turnstile,
DataDome or PerimeterX. **There is no widget on that page for any solver to
answer.** That is a statement about the page, not about the product — if
Woolworths ever serves an interstitial that does carry a widget,
`captcha_solver.py` already implements reCAPTCHA v2/v3, enterprise reCAPTCHA
and Cloudflare Turnstile, and `page_flow.STATE_POLICY` is the one line to
change.

---

## "The rows are right but `price_source` is `api` on every one"

Look at `unauthorised_redirect` in the `*.meta.json` sidecar.

Woolworths' page script reads `navigator.webdriver`, which Playwright and
Selenium both set. When it is true, the site's own app navigates the browser
to `https://www.woolworths.com.au/unauthorisederror` about a second after
load, and the product grid never renders.

What makes this worth a section is everything that stays NORMAL when it
happens:

- the document still answers **HTTP 200**;
- the API still answers **200 with real products**, because the session
  cookies are valid;
- so the run returns **the right rows** and reports success.

The only things you lose are the DOM price cross-check (`price_source` stays
`api` instead of becoming `api+dom`) and the grid itself. All three engines
pass `--disable-blink-features=AutomationControlled` to prevent it — measured
three runs each way: without the flag 3/3 redirected and 0 tiles rendered,
with it 3/3 stayed put and 36–39 tiles rendered.

If you see this anyway, either the flag is not reaching the browser or the
site has started looking at something else.

---

## "Most rows have no `original_price` or `discount_pct`"

Expected, and the alternative would be worse.

`WasPrice` is populated on **100%** of rows and **equals `Price` on 537 of
660** measured rows — 81%. Those products are simply not on special.
Reporting a was-price equal to the price would give you a 0% discount on four
products in five, and `discount_pct` would be meaningless.

So `original_price` is null unless `WasPrice > Price`, which was true of 119
of those 660 rows, and `discount_pct` is computed from the two rather than
read off a badge. `smoke_test.py` asserts no row ever carries an
`original_price` at or below its `price`.

---

## "Some of my rows are advertisements"

They are, and the site put them there. `is_sponsored` marks them — filter on
it.

Two things about them are worth knowing, because both change how you read a
multi-page run:

**They repeat.** Pages 1 and 3 of one search returned an identical set of 8
promoted products. Of 8 stockcodes appearing twice across three pages, all 8
were sponsored and 0 were organic. The scraper dedupes on `sku`, so you get
each product once — but if you are counting rows per page, that is why the
numbers do not add up.

**A listing never runs out of them.** Past its last real page the API keeps
answering HTTP 200 with `Success: true` and nothing but ads:

| page of a 16-page category | rows | organic |
|---|---|---|
| 16 | 43 | 35 |
| 17 | 1 | 0 |
| 18 | 8 | 0 |
| 99 | 8 | 0 |

This is why the walk stops on "no new ORGANIC product" rather than on "the
page was empty" — the latter would never fire, and a `--pages 50` request
would pad your output with the same eight ads forty times.

---

## "It says the listing has ended before `--pages`"

That is the stop condition above doing its job, and the run is **complete**,
not partial — `status: complete`, exit 0.

A run that asked for 8 pages of a 2-page listing read the whole listing. One
measured example: `?searchTerm=saffron` returned 36 + 33 = 69 rows over two
pages, and the site's own `SearchResultsCount` for that term is 69.

---

## "`SearchResultsCount` does not match how many rows I got"

Two separate reasons, and neither is a missing read.

**It is not stable across pages.** Measured: a search for `saffron` reported
69 on page 1 and **0** on page 50. Treat it as a hint for logging, never as a
loop bound — the scraper does the same, which is why it is recorded in the
sidecar as `stated_total` beside what the run actually read.

**You asked for fewer pages than the listing has.** 3 pages of a 2,205-result
search is ~110 rows, and that is the whole difference.

---

## "Zero rows — exit 4"

Read the last line of the log; it distinguishes two very different things.

```
The site reports 0 results for this listing, and 0 rows were parsed.
That is a correct, empty answer rather than a failed read — exit 4.
```

That is a genuinely empty listing. Nothing is wrong.

```
The site states 575 result(s) for this listing and the parser produced
NONE. That is a parser failure, not an empty category.
```

That is a bug, and the run saves the payload to `*_debug.html.api.json`. The
likeliest cause is that the **group wrapper** changed: `Products` (search)
and `Bundles` (category) are lists of `{"Products": [...]}` objects rather
than lists of products, and if that shape flattens, every row loses its
`Stockcode` and is dropped.

---

## "A category URL is refused"

```
'sourdough' is not a category node on this site. It is not a typo in the
code: the slug was looked up in Woolworths' own tree (2696 nodes) and is
not in it.
```

Browse endpoints need an **opaque node id**, not the slug: `bakery` is
`1_DEB537E` and `fruit-veg` is `1-E5BEE36E` — the two do not even share a
separator. The scraper resolves the slug against
`/apis/ui/PiesCategoriesWithSpecials` before asking for page 1.

If it refuses, open the URL in a browser. Sending the slug where the id
belongs returns HTTP 200 with zero products and `Success: true`, which is
indistinguishable from a real empty category — so the scraper refuses rather
than reporting success on nothing.

`/shop/browse/specials` works and is a category like any other; its node id
is the literal `specialsgroup`.

---

## "A single product page is refused"

On purpose:

```
a single product page is not a mode in this repo: the same object is
already on every listing row, and /apis/ui/products/{stockcode} returns it
if one row is all you need
```

Listing rows and the by-stockcode endpoint return the **same 115-field
object**, so a product mode would add a mode without adding a column.

---

## "`woolworths.co.nz` / `woolworths.co.za` are refused"

They are different companies, and the scraper says so rather than claiming
they are not Woolworths sites:

- **woolworths.co.nz** — Woolworths New Zealand, formerly Countdown. A
  different platform that does not serve the `/apis/ui` API this repo reads.
- **woolworths.co.za** — Woolworths Holdings, South Africa. An unrelated
  retailer that shares the name.

This repo is `woolworths.com.au` only. The apex (`woolworths.com.au` with no
`www.`) is accepted and redirects to `www.`.

---

## "`--concurrency 4` — does it actually do anything here?"

On the **Playwright** engine, yes. Every page of both modes is addressable —
a page is an integer in a request body, not a cursor — so page 5 can be
fetched without reading page 4. Each worker owns its own browser and its own
exit.

Two refusals you may hit:

- **With `--cdp-endpoint`**, it is refused: the Scraping Browser API allows
  one live connection per profile, and workers collide with
  `profile_locked`. Use several `pid`s, one run each.
- **On the Selenium and pyppeteer engines**, it is not implemented. They say
  so and continue with one worker; the rows are identical either way.

Also note every worker is a **real browser window**, because this site
refuses headless — four workers means four windows and the memory for them.

---

## "`--cdp-endpoint` says `profile_locked`"

One live connection per profile. The most common cause is the obvious one: a
previous run has not let go yet. Measured here — connecting a second session
to the same `pid` while the first was open failed immediately with
`500 profile_locked`, and the same endpoint connected fine about a minute
later.

Use a different `pid` for concurrent runs, and reuse `pid`s across sequential
runs rather than minting a new one each time (they are capped per account).

A Scraping Browser profile's credentials also expire in about a day, which is
why no working endpoint is pasted anywhere in this repo — it would be stale
before you read it and would fail with an auth error a long way from its
cause.

---

## "The tests pass but a live run is broken"

That is the gap the offline suite cannot close on its own, and this repo has
been bitten by it: the `/unauthorisederror` bounce above produced correct
rows and a green suite while silently losing half the verification.

Re-capture and regenerate the fixtures so the suite can see what you saw:

```bash
python3 playwright_scraper.py --url "..." --dump-html out.html
# out.html          the document — no product data on this site
# out.html.api.json the payload the rows actually come from  <-- read this one

# then, with captures named as make_fixtures.py expects:
python3 make_fixtures.py --captures ./captures --out fixtures_generated.json
python3 smoke_test.py
```

---

## "Which host should I point it at?"

`https://www.woolworths.com.au`. One site, one catalogue, one currency
(AUD — the API never states it, so it is derived from the host and refused
for any host not in the table).

```bash
# a search
--url "https://www.woolworths.com.au/shop/search/products?searchTerm=milk"

# a category, and its child nodes
--url "https://www.woolworths.com.au/shop/browse/bakery"
--url "https://www.woolworths.com.au/shop/browse/fruit-veg/fruit"

# specials — a category like any other
--url "https://www.woolworths.com.au/shop/browse/specials"
```
