# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims at [Semantic Versioning](https://semver.org/) as closely
as a CLI toolkit can. A **patch** release means fixes; it does not promise
that every flag and every default is frozen, so a behaviour-changing default
can land in one — and when it does, the entry leads with it in a blockquote
rather than leaving anyone to discover it from their own output.

## [0.4.0] — 2026-10-08

### Added

- **`--mode stores`: find the Woolworths near a postcode.** A third mode and
  a second row class, `Store`, with the family prefix byte-identical to
  `Product`'s. Four lookups, all ungated — no URL, no key, no account:

  ```bash
  --mode stores --postcode 3000            # 10 stores, nearest first
  --mode stores --suburb "Bondi"           # 10 stores, no distance
  --mode stores --near -37.8136,144.9631   # 30 stores, all with a distance
  --mode stores --store-id 3304            # one store by its StoreNo
  ```

  Each row carries the store number, name, address, suburb, state, postcode,
  coordinates, distance, phone, trading hours and facilities. One lookup per
  run: the site's four parameter sets are not combinable, so two is an error
  rather than a silent choice between them.

- **`sample_stores.json` / `sample_stores.csv`**, six rows from a real run,
  and `ci_checks.py --sample-check` now holds BOTH row classes to their
  dataclass. A repo with two schemas has two to keep honest, and checking
  only the first is how the other goes stale with nothing failing.

### Changed

- **`--postcode` and `--store-id` on a LISTING run now refuse rather than
  mislead.** Measured 2026-10-08: `POST /apis/ui/Fulfilment` answers **401**
  to a guest, and it is the only fulfilment endpoint the front end has —
  checked by pulling all 88 `${baseApisUrl}` fragments out of the site's own
  bundles. The listing API ignores a store silently as well: seven spellings
  of a store parameter were added to the category body and every one came
  back HTTP 200 with the same 37 prices (§26's "the API answers wrong values
  with plausible data").

  So the run asks the site, reads the session's own `FulfilmentStoreId`
  before and after, and if nothing moved it exits **2 and writes nothing** —
  rather than scraping store 1101 and stamping the requested number on every
  row, which would be a file that is real in every cell and wrong in the one
  that matters (§8).

  **This repo does not implement an authenticated session** (§19's wording:
  a TODO, not a limitation). Whether an account could set a store is
  untested.

### Fixed

Three things only a live run found, each now pinned by a control:

- **Store coordinates are numbers, not strings.** The site sends them as
  strings and every Australian latitude is negative, so the CSV formula
  guard apostrophe-prefixed all of them — 10 of 10 rows on the first live
  run — and the column stopped being numeric for any mapping tool. The guard
  was right to escape a formula-shaped string; the fix is for the field not
  to be one.
- **`--near -37.8136,144.9631` works.** argparse reads a token beginning
  with `-` as another option and died with "expected one argument", which
  reads like the flag is broken. Every Australian latitude is negative, so
  this failed for **100% of real coordinates**. The two tokens are joined
  into the `--near=` form before argparse sees them — narrowly, only for
  `--near` and only when the next token parses as a coordinate pair.
- **The `shopper` fixture carries no session id.** A real `SessionId` came
  with the capture; it is replaced with an all-zero placeholder, it is the
  one value in these fixtures that is not verbatim, and the check guards the
  SHAPE so the next capture is caught too (§10).

### Note

Four fields the locator returns are deliberately **not** columns:
`AddressLine2`, `PartnerUrl`, `CategorisedFacilities` and `NearbyPartners`
were null on 50 of 50 records across three captures. §9 — and the
measurement is written down so someone can add one back with a better one.

## [0.3.1] — 2026-10-08

### Fixed

Two tails from the October audit that the first pass closed only half-way.
Found by re-reading the audit against the code rather than against the
commit message.

- **`--proxy-block-retries` now says whether it counts attempts or
  retries.** The audit asked for exactly that — "объяснить, число это
  попыток или дополнительных повторов" — and v0.2.0 fixed the flag
  reaching the policy while leaving the help reading "how many exits to
  try", which says *total*. It is a count of RETRIES: the loop is
  `range(N + 1)`, so 4 means up to 5 landings and 0 means try once. A
  reader who set 1 expecting one attempt got two. A check now pins the
  wording AND the arithmetic together, so the help cannot drift from the
  loop.
- **`diff_runs.py` refuses a file with no `.meta.json` beside it.** The
  audit asked for a "строгий режим при отсутствии metadata"; without a
  sidecar the completeness, mode, listing and store guards all `continue`d
  in silence, so two unrelated runs diffed as 100% churn and looked like
  news. `_run_status` had also described a missing sidecar as "the normal
  case for a single-page run", which is false: `finish_run` writes one
  whenever it writes rows. `--force` is the opt-out, the same one a partial
  run already had.

## [0.3.0] — 2026-10-08

### Changed

- **One fetch loop instead of three** (CLAUDE.md §27.5). The page loop —
  landing, block retries, proxy rotation, the solve budget, classification,
  the readiness wait, target resolution, the API call with its own retry
  budget, the DOM price confirmation, coverage checks and the
  empty/parser-failed decision — is now ONE implementation in `page_flow`,
  and each engine passes an object of 21 named driver operations.
  `PageOutcome`, `FIELD_FLOOR`, `DOM_CONFIRM_FLOOR` and the credential
  masker moved there with it.

  Measured before the move: `_fetch_one_page` was 254/213/215 lines in the
  three engines, 78% textually identical between the two closest, and a
  code-only diff — comments and log prose stripped — showed **no
  behavioural difference at all**. Every differing line was driver spelling
  or a shorter log message in the twins.

  The triplication had already cost this repo twice. v0.1.0 shipped
  `'Page' object has no attribute 'page'` in category mode on the Playwright
  engine alone, because one of three copies passed `session.page` where the
  others passed `session`; search mode never reaches that line, so it got
  through a search-only verification and reached main. The check written
  afterwards immediately found a second divergence of the same shape, in
  `_read_tiles`. §27.5 says to convert the repos where a divergence has
  actually bitten — this is one, twice.

  Net: the engines lost 1,271 lines of triplicated logic, `page_flow`
  gained 625 including its comments, and the whole repo is 391 lines
  shorter. What is still in all three is driver-specific (`_fetch_api`,
  `_read_tiles`, `_count`, `_proxy_failure`) or legitimately per-engine
  (`parse_args`' help prose, `scrape`'s launch and teardown).
  `handle_captcha_if_present` is the next candidate and is deliberately not
  in this change.

### Fixed

Three divergences the merge exposed, each of which had ONE engine doing it
right and the other two not:

- **A session refused between pages was read as content.** Two engines
  returned a hardcoded `"content"` when they were already landed on the
  URL; the third re-classified the document it found. The third is right —
  a session can be refused between page 1 and page 2, and the other two
  would have carried on asking the API from inside a denial page. The
  shared loop re-classifies.
- **A dead proxy printed its password, from two engines of three.** The
  masker was three identical copies and only Selenium actually called it on
  the load-failure path, so the same unusable exit leaked its credentials
  from Playwright and pyppeteer. One definition, called in the one place
  that logs a driver error (§8: an exception message is a log).
- **`landed_url` existed only once something had assigned it.** No session
  class declared it; it was created by the first write from outside. It now
  has a declaration and a comment in all three.

### Added

- **`test_every_engine_provides_every_operation_the_loop_asks_for`** —
  the required set is derived from `page_flow`'s own AST, never from a list
  someone keeps up to date, so the day the loop reaches for a new operation
  every engine missing it fails by name. Instance attributes assigned in
  `__init__` count, including the tuple form (§26 records the first version
  of this check elsewhere missing those and reporting false positives).

  It reads the engines **off disk, with no import** — which is the point,
  and was wrong first time round. Written against the imported modules it
  skipped for every engine whose driver is absent, so it covered one engine
  of three locally and **zero in the offline CI job**, where it then failed
  on its own "nothing was scanned" guard. §27.4 records exactly this trap
  in exactly this kind of check. The suite now also runs in a bare
  requirements-only venv locally, which is what CI's offline job is and
  what would have caught it before the push.
- **`test_the_shared_loop_runs_end_to_end_against_a_fake_driver`** — six
  cases through the whole decision tree offline, every answer from a real
  capture: a served page, the Akamai denial (blocked, retried, screenshot,
  never parsed), a genuinely empty listing, a payload stating 2,205 results
  that parses to none, the full block budget being spent, and a session
  refused between pages. Three copies of the loop could never have shared
  a test like this, which is half the argument for merging them.
- **`test_every_ops_method_reaches_a_session_attribute_that_exists`** —
  written because it happened on the first live run after the merge:
  `PuppeteerOps.goto` called `self.session.run(...)` where that engine's
  bridge is `self.session.bridge.run(...)`. The method existed, so the
  coverage check was happy; the attribute did not, so the engine died with
  `AttributeError` on its first navigation in both modes. Invisible to
  import, `--help`, `compileall` and the undefined-name walk; five lines
  and instant here.
- **`test_every_engine_drives_the_shared_loop`** — nobody kept a private
  copy, and the removed helpers stay removed.

## [0.2.1] — 2026-10-08

### Fixed

- **The output-mode rule now matches the sibling that fixed this first.**
  0.2.0 shipped `0o644 & ~umask`, which is indistinguishable from the right
  rule under the common umask 022 and **quietly narrows 0664 to 0644 under
  umask 002** — so a group-writable output directory, which is exactly the
  setup where several accounts share a scrape, stops being group-writable.
  It also overwrote the mode of an EXISTING target, undoing a tightening
  somebody may have done on purpose.

  `hackernews-scraper` had already solved both: an existing file keeps its
  own mode, and a new one gets `0o666 & ~umask` — exactly what
  `open(path, "w")` would have given it. Lifted verbatim. CLAUDE.md §16 says
  to lift the sibling's fix rather than invent a second one, and this is
  what inventing one costs: a variant that passes every check written for it
  and is wrong on the umask nobody tested with.

  Two new controls, each red on its own check: a flat 0644, and a save that
  reopens a file somebody chmodded to 0600.

## [0.2.0] - 2026-10-08

> **A write that died halfway destroyed the previous good output.** Every
> output file was opened with `open(path, "w")`, which truncates before the
> first byte is written — so a crash, a kill, a full disk or a Ctrl-C during
> a save left a SHORTER file where a complete one had been, and the run's
> own `.meta.json` could be truncated beside rows that were fine. Writes are
> atomic from this release. Nothing is needed from you; a scheduled job that
> has ever been interrupted mid-save is worth re-running.

> **CSV cells that begin `=`, `+`, `-` or `@` are now prefixed with an
> apostrophe** so a spreadsheet reads them as text rather than executing
> them. The JSON output is unchanged and still carries the site's exact
> bytes, so the two files can now differ; `csv_cells_escaped` in the sidecar
> says by how much. Measured on this site before shipping: **0 of 10,476
> string cells across 433 live rows** begin with one, so this changes
> nothing today and is a guard rather than a catch.

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

### Added

- **A per-page captcha solve budget that is actually consulted.**
  `page_flow.SOLVES_PER_PAGE` is 1, and `page_flow.SolveBudget` is what
  enforces it: the engines create one per PAGE, outside the block-retry
  loop, and it is charged immediately before the solver is called rather
  than after it returns, because a failed solve is still billed. It is
  deliberately not refilled by a proxy rotation — a fresh exit is a reason
  to re-fetch the page, not a fresh allowance to pay for it.

  Nothing is bought on this site today: `should_solve` is False for every
  state, because Akamai's denial here is ~400 bytes with no widget on it.
  This guards the day that changes, which is the only day it is expensive.
  CLAUDE.md §23 measured a sibling buying three Turnstile solves for one
  page against a `SOLVES_PER_PAGE = 1` that nothing read, and §27.4 then
  found the same bypass in 33 of the 39 family repos with a solve path.
- **`csv_cells_escaped` in the run sidecar**, so the divergence between the
  CSV and the JSON is declared rather than discovered as a stray
  apostrophe. It merges UNDER anything the engine put in `extra`: a
  collision there would be housekeeping silently dropping a measurement
  about the site.

### Fixed

- **Output files are written atomically** — to a temporary file in the
  target's own directory, `fsync`ed, then `os.replace`d over the real name.
  All three writers were truncating: `write_json`, `write_csv` and
  `write_run_meta`. The sidecar is the one that mattered most, because it is
  the file a consumer branches on, so a truncated one beside good rows reads
  as a broken run over data that is fine. Three planted-fault controls, one
  per writer: each turns the suite red by leaving a previous good file
  damaged.
- **An atomic write no longer makes the output private to the user that
  produced it.** `NamedTemporaryFile` creates at 0600 and `os.replace` keeps
  the temp file's mode, so adopting atomic writes silently narrows every
  output — and only once it became atomic, which is the worst time to find
  out. The mode is set to 0644 masked with the process umask, so a
  restrictive umask is still respected and nothing the user closed is
  reopened. Measured across the family on 2026-10-08 by CALLING each
  repo's sidecar writer rather than reading it: of 43 repos, **8 produce a
  0600 sidecar**, and they are exactly the ones that took the atomic writer
  without this — two others (craigslist, quora) had already taken both.
- **`.gitignore` covers every `.env` variant, not just `.env`.** A working
  copy renamed the way people actually rename one — `.env.bak`, `.env.local`,
  `.env.save` — was untracked but NOT ignored: one `git add -A` from a
  commit, and invisible to the person deciding whether running that is safe.
  `.env*` with `!.env.example`, so the documented example stays tracked.
  This landed in #5 on 2026-09-17 and was recorded in no release section
  until now; CLAUDE.md §22 has the two occasions the family paid for it.
- **The exit-code commentary in `output_writer.py` described another site.**
  It listed a `/p/<slug>` discovery hub, "no stories", an Indonesian
  no-results string and a refusal that resets the HTTP/2 stream — none of
  which is Woolworths. Replaced with what was measured here: a served answer
  with no products is a term the catalogue does not match or a page past the
  end of a listing (HTTP 200, `Success: true`, promoted ads only), and
  `EXIT_BLOCKED` is Akamai's edge denial at HTTP 403.

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


## [0.1.2] — 2026-09-17

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
