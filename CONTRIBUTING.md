# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

Woolworths changing something is the normal way this stops working, and it
has its own issue template. The detail that saves the most time is WHICH part
broke, because they fail very differently.

**The API is the whole read path.** There is no HTML fallback in this repo,
and that is measured rather than lazy: a served category page is 872 KB whose
visible text is 3,735 characters of navigation chrome, the word "Banana"
appears zero times on `/shop/browse/fruit-veg`, and there are zero
`/shop/productdetails/` anchors anywhere in the markup. A fallback parsing
the served HTML would find zero products on every page, forever — dead code
that looks load-bearing.

So every row comes from one of:

```
POST /apis/ui/Search/products          search
POST /apis/ui/browse/category          a category node, by opaque id
GET  /apis/ui/PiesCategoriesWithSpecials   slug -> opaque node id
```

Three ways these break, in rough order of how quietly they do it:

1. **The group wrapper flattens.** `Products` (search) and `Bundles`
   (category) are lists of `{"Products": [...]}` objects rather than lists of
   products. If that changes, every row loses its `Stockcode` and is dropped,
   and you get exit 4 on a listing that plainly has products. LOUD, and the
   log says so by name.
2. **A field is renamed.** `DisplayName`, `Price`, `WasPrice`, `CupString`,
   `IsSponsoredAd`, or one of the `AdditionalAttributes` keys. This is the
   quiet one: the row count stays healthy and one column empties out. The
   coverage line every run prints is what shows it.
3. **The request body gains a required field.** Page 1 comes back HTTP 200
   with zero products and `Success: true` — which is exactly what a real
   empty category looks like. The scraper tells the two apart by the site's
   own stated count, so this reports as a parser failure rather than as an
   empty listing.

**The shadow-DOM tiles are a second view, not a second path.** The grid
renders into OPEN shadow roots on `<wc-product-tile>` elements, which
`page.content()` does not serialise — Playwright's CSS engine pierces them,
Selenium and pyppeteer walk them in page script. They are read only to
CONFIRM the API's price, so if they break you lose `price_source: api+dom`
and nothing else. The rows stay correct.

**Akamai's refusal page** is how a block is recognised: `Access Denied`,
`You don't have permission`, `edgesuite`. Every one of those survives BOTH
encodings the same page arrives in — a browser serialises
`errors.edgesuite.net` while raw bytes carry `errors&#46;edgesuite&#46;net`,
so a marker keyed on the punctuation catches the browser engines and silently
misses an HTTP client.

**Before you add a marker, count it on a page you know is good.** This repo
got that wrong twice in one day. `akamai` occurs once on every page
Woolworths serves (its own performance script) and zero times on the denial
page — exactly inverted. `couldn't find any` occurs four times on every page,
good or empty, because it ships in the JS bundle as a template; taken as a
no-results marker it made a 2,205-result search report itself empty.
`smoke_test.py` now pins both as NON-markers against a real served capture.

`--dump-html PATH` writes the document AND `PATH.api.json`, the payload the
rows actually come from. On this site the second one is the useful file.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once —
   including its WARNING branch, which is what runs when a bare GitHub
   runner's datacentre address is refused and no `WOOLWORTHS_PROXY` secret is set.
   This canary needs no secret to do real work: eight of fourteen fetches
   were served in full with no key and no proxy, from a DATACENTRE address
   at that. What it has NOT been measured doing is getting past
   Cloudflare from a shared datacentre address, and since the challenge here
   tracks the address's recent request rate, a runner is the worst case for
   it. That is exactly why a block there is a warning rather than a failure —
   until you set `WOOLWORTHS_PROXY`, after which it is a failure, because then it
   means something.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions with inline HTML/JSON fixtures — no pytest, no
conftest, no fixtures directory. Copy the nearest existing check and edit it.

Ten properties in this repo exist because they were once absent, or because
they cost a sibling repo real time. Tests pin all ten, so a PR that breaks one
will fail rather than silently regress:

- **`sku` is `Stockcode`, as a string.** Woolworths' own product number,
  stable across a rename — the slug in the URL is not — and the join key
  `diff_runs.py` uses. Note it is NOT unique across a multi-page run before
  deduping, because the same promoted ads are served on every page;
  `page` + `position` is the pair that is unique, and the suite asserts it.

- **`original_price` is null unless `WasPrice` EXCEEDS `Price`.** `WasPrice`
  is populated on 100% of rows and equals `Price` on 537 of 660 measured
  rows. Copying it across puts a 0% discount on four products in five. The
  suite asserts no row carries an `original_price` at or below its `price`,
  and `discount_pct` is computed from the two rather than read off a badge.

- **`title` is `DisplayName`, never `Name`.** Both are 100% populated, which
  is exactly why the wrong one is easy to ship: `Name` drops the pack size
  and duplicates the variety ("Dairy Farmers Full Cream Milk Full Cream
  Milk"). The column would look complete and read worse on every row.

- **`Products` and `Bundles` are lists of GROUP WRAPPERS.** Reading the outer
  list as the product list yields wrapper dicts with no `Stockcode` on any of
  them — a run that "succeeds" with every column null. The fixtures keep the
  wrapper shape on purpose, so `flatten_products`'s test is not vacuous.

- **A listing ends when it stops producing new ORGANIC rows, never when a
  page is empty.** Past its last real page the API answers HTTP 200 with
  `Success: true` and nothing but promoted ads — page 17 of a 16-page
  category returned 1 row, page 18 returned 8, page 99 returned 8, all
  sponsored. A loop testing "were there rows" never terminates.

- **A marker that matches every good page is not a marker.** Counted on a
  real served capture: `akamai` once on every page Woolworths serves and zero
  times on its denial page — exactly inverted — and `couldn't find any` four
  times on every page, good or empty, because it ships in the JS bundle as a
  template. Both are pinned as NON-markers. Count any new marker on a page
  you know is good BEFORE adding it.

- **Block markers must survive BOTH encodings.** The same refusal reaches a
  browser as `errors.edgesuite.net` and an HTTP client as
  `errors&#46;edgesuite&#46;net`. Markers are matched against an unescaped,
  BOUNDED prefix, so a marker added later does not reacquire the hole.

- **Tile scoping is by stockcode, not by walking the DOM.** A quarter of the
  tiles on a category page belong to a "you might also like" carousel. Keying
  each tile on the stockcode in its OWN product link makes the neighbour-tile
  trap unreachable rather than merely unlikely — and the DOM read only ever
  CONFIRMS a price, never overwrites one.

- **Never write that a captcha cannot be solved.** No captcha has been
  observed on this site at all: `sitekey`, `recaptcha`, `hcaptcha` and
  `turnstile` are each 0 on every served page and on both denial pages. The
  honest sentence is "this page carries no widget", never "this cannot be
  solved" — the solver implements reCAPTCHA v2/v3, enterprise reCAPTCHA
  (`RecaptchaV2EnterpriseTaskProxyless`) and Turnstile
  (`TurnstileTaskProxyless`). The suite greps for that sentence shape and
  fails on it. Note also that a page fetched over `--cdp-endpoint` carries
  the Scraping Browser extension's own injected hunters, so captcha markers
  in such a dump are the extension's, not the site's.

- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out. And exit codes are a
  contract, not decoration: `0` ok, `1` crash, `2` bad usage, `3` blocked,
  `4` zero rows, `5` remote API error, `6` partial. A listing that genuinely
  ran out is `complete`; a listing whose next page the API REFUSED is
  `partial`, because a throttled run reporting "complete" is the failure §7
  exists to prevent.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it that
  way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has needed
  an explicit timeout its own API does not provide, and each has needed its own
  route out of the runtime — reporting a timeout is not the same as exiting on
  one. If you add a call to a remote browser or API, bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is the
  single most common bug class in this codebase's history. A selector that
  matches the *wrong* element is worse than one that matches nothing, because
  the second one tells you.

### If your change needs a live run

Most do not — the suite covers the parser, the writers, the captcha classifier
and the CLI contract against inline fixtures. If yours genuinely needs
woolworths.com.au, say in the PR what you ran, which URL and mode, from
which exit, and what you got — including the price and image coverage
percentages the run prints, and the scroll trace from the sidecar. Note that
a run from a datacentre address gets NO RESPONSE AT ALL, so "it returned
nothing" from a VPS is not a finding. Product counts differ by category, by
URL and by how far the scroll got, so a bare "worked for me" is not
reproducible.

**Run more than the primary engine.** "Mirror them exactly" is a design rule,
not a verification: the first live run of the pyppeteer engine crashed on its
FIRST fetch on a signature mismatch that four separate offline checks and 400
green assertions had not caught.

Do not add anything that submits the registration form. This project
deliberately never does, and a captcha token proved valid by creating a real
account is not a result worth having.

## Scope

This repo scrapes **public pages** on Woolworths Online: category listings, search
listings and product pages, exactly as an anonymous visitor is served them.
Out of scope: anything behind a login, anything that submits a form, and
anything that defeats a protection rather than passing it the way an ordinary
browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
