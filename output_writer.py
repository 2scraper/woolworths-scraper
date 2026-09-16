"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Two modes, one row shape
------------------------
    --mode search     /shop/search/products?searchTerm={term}
    --mode category   /shop/browse/{slug}[/{child}[/{grandchild}]]

Both yield the same class, because they are two ways of SELECTING the same
leaf: a Woolworths product. Search ranks the catalogue by a term, a category
browses a node of it, and the site answers both from the same internal API
with the same 115-field product object. `/shop/browse/specials` is a
category like any other — its node id is the literal `specialsgroup` — so it
needs no mode of its own.

Where the columns below come from
---------------------------------
Woolworths server-renders NO product data. Measured on six captures taken
2026-09-16: zero `application/ld+json` product blocks on search, category and
specials pages alike (a category page carries exactly one JSON-LD block and
it is a `BreadcrumbList`), and zero `/shop/productdetails/` links anywhere in
the served markup. The catalogue arrives afterwards, as JSON, over the site's
own API:

    POST /apis/ui/Search/products     search
    POST /apis/ui/browse/category     a category node, by opaque id
    GET  /apis/ui/products/{codes}    the same object, fetched by stockcode

All three return the SAME product shape — 115 fields — so one parser reads
all three and `data_source` says which answered. `product_parser.py` has the
detail.

Every column here is measured, and the measurement is why it is here
--------------------------------------------------------------------
Taken on a 660-row corpus (589 distinct stockcodes) drawn 2026-09-16 from
five search terms and three category nodes. The numbers are a snapshot of one
run rather than a property of the site, which is why they name their date
(CLAUDE.md §13).

Five columns the previous generation of this repo advertised are NOT here,
and each absence is a measurement rather than an oversight (§9: a column null
on every row of every run should not exist, and removing it needs the number
written down so someone can put it back with a better one):

    rating          The API ships a `Rating` object on every product and it
    rating_count    is EMPTY on every product. RatingCount, ReviewCount,
    review_count    RatingSum and Average were 0 on all 660 listing rows and
                    on 10 rows fetched from the by-stockcode detail endpoint
                    — 670 rows, zero non-zero. The object's presence is what
                    makes this worth writing down: a parser that reads it
                    fills three columns with 0 and reports 100% coverage.

    country_of_origin
                    `AdditionalAttributes.countryoforigin` exists on all 660
                    rows and is null on all 660.

    instore_price   Not null — DUPLICATE. `InstorePrice` equalled `Price` on
                    656 of 656 rows carrying both, to the cent. The in-store
                    and online SPECIAL FLAGS did disagree, on 13 of 660 rows,
                    which is a real difference with no price behind it; if a
                    later capture shows the prices themselves diverging this
                    is the column to add back.

And one column is here that a naive port would have got backwards — see
`original_price`.
"""
import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The host a row came from. Woolworths Online is one site on one host, so
# unlike the sibling repos this genuinely does not vary — but the column
# stays, in the family's position and under the family's name, so a consumer
# reading six of these repos reads the same first five columns (§9).
SOURCE_DEFAULT = "woolworths.com.au"

# Woolworths Online prices in Australian dollars, and the API NEVER SAYS SO:
# there is no `currency`, `Currency`, `priceCurrency` or `AUD` token anywhere
# in a product object (counted across 50 full objects, 0 occurrences of each).
#
# So this is not read from the payload and it is not guessed from a `$` in
# the DOM either — §4's ladder rates a bare symbol a guess, and it would be
# one, since `$` alone is equally USD, NZD or SGD. It is derived from the
# HOST: woolworths.com.au is the Australian supermarket and sells in AUD.
# That is a fact about which site answered, which is why it lives beside
# `HOSTS` in product_parser.py and is refused for any host not in that table.
CURRENCY_BY_HOST = {"woolworths.com.au": "AUD"}


@dataclass
class Product:
    # --- the family prefix, byte-identical and in order across the family ---
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Rebuilt, not read. There is no product link in the served markup to
    # copy — zero `/shop/productdetails/` anchors on any capture — so this is
    # assembled from `Stockcode` and `UrlFriendlyName`, which is the address
    # the site's own router builds. `product_parser.product_url()` owns the
    # shape and `smoke_test.py` pins it against a live capture.
    url: str = ""
    # `Stockcode`, as a string. Woolworths' own product number, stable across
    # a rename (the slug in the URL is not), and the join key for
    # `diff_runs.py`. 100% populated on all 660 rows.
    sku: Optional[str] = None
    # `DisplayName` — the name as the shelf shows it, including the pack size
    # ("Dairy Farmers Full Cream Milk 2L"). NOT `Name`, which drops the size
    # and duplicates the variety ("Dairy Farmers Full Cream Milk Full Cream
    # Milk"). Both are 100% populated, which is why the wrong one is easy to
    # ship: `title` would look complete and read worse on every row.
    title: Optional[str] = None

    # --- the product -----------------------------------------------------
    brand: Optional[str] = None          # 93.9%
    # `Description`, HTML stripped — it carries `<br>` on a real fraction of
    # rows.
    description: Optional[str] = None    # 100%
    # `Variety` — the flavour/variant word ("Full Cream Milk"). 79.1%.
    variety: Optional[str] = None
    barcode: Optional[str] = None        # 100%

    # --- price -----------------------------------------------------------
    # `Price`. 99.4% — the 0.6% that is null are products the API returns
    # with no price at all, and they are also `IsAvailable: false`.
    price: Optional[float] = None
    # Always "AUD" on this host, and never read from the payload. See
    # CURRENCY_BY_HOST above.
    currency: Optional[str] = None
    # `WasPrice`, AND ONLY WHEN IT IS ACTUALLY A WAS-PRICE.
    #
    # This is the trap this repo would most easily have shipped. `WasPrice`
    # is populated on 100% of rows and EQUALS `Price` on 537 of 660 of them —
    # 81%. Copying it straight across gives an `original_price` identical to
    # the price and a computed discount of 0% on four rows in five, on
    # products that are not discounted at all. Only 119 rows carry a
    # `WasPrice` greater than `Price`, and those are the real ones.
    #
    # So: null unless `WasPrice > Price`. `discount_pct` is then computed
    # from the two rather than read, and is null rather than 0 where there is
    # no discount — §4's rule, and `smoke_test.py` asserts no row has an
    # `original_price` at or below its `price`.
    original_price: Optional[float] = None
    # `SavingsAmount`. Equalled `WasPrice - Price` on every one of the 660
    # rows, so it is a cross-check rather than a second source; a row where
    # the two disagree is worth knowing about and the parser warns.
    savings_amount: Optional[float] = None
    # Computed from `original_price` and `price`, never read from a badge.
    # Null when there is no was-price, rather than 0.
    discount_pct: Optional[float] = None
    # WHICH view built the price on this row (§8: never present a guess as a
    # fact). `diff_runs.py` reports a price difference that comes with a
    # `price_source` difference as `source_changed` rather than as a change.
    #
    #   api          the site's own JSON, which is the normal case
    #   api+dom      the JSON, with the rendered tile agreeing
    #   dom          the rendered tile only — the fallback path
    price_source: Optional[str] = None

    # --- unit pricing, which is why anyone scrapes a supermarket ----------
    # `CupPrice` / `CupMeasure` / `CupString` — the shelf's unit price
    # ("$2.35 / 1L"). 99.4%, and the only way to compare a 2L bottle against
    # a 3L one.
    cup_price: Optional[float] = None
    cup_measure: Optional[str] = None
    cup_string: Optional[str] = None
    package_size: Optional[str] = None   # 100%
    # `Unit` — 'Each' on 656 of 660 rows and 'KG' on 4. A near-constant
    # column, kept because the 4 are the ones priced by weight.
    unit: Optional[str] = None

    # --- promotions -------------------------------------------------------
    is_on_special: Optional[bool] = None   # 17.6% true
    is_half_price: Optional[bool] = None   # 6.5% true
    # A PROMOTED AD, not an organic result — `IsSponsoredAd`, 17.0% of rows.
    #
    # This column is load-bearing rather than informational. Woolworths
    # injects promoted products into both listings, and the SAME ads come
    # back on every page: pages 1 and 3 of one search returned an identical
    # set of 8, and of 8 stockcodes appearing twice across three pages all 8
    # were sponsored and 0 were organic. Past the end of a category the API
    # keeps answering 200 with `Success: true` and NOTHING BUT ads — page 17
    # of a 16-page category returned 1 row, page 18 returned 8, page 99
    # returned 8, every one of them sponsored.
    #
    # So a consumer doing price monitoring filters on this, and the engines
    # count ORGANIC rows to decide the listing has ended. See
    # `page_flow.organic_count`.
    is_sponsored: Optional[bool] = None
    # `AdStatus` — 'Promoted' where the row is an ad, null otherwise.
    ad_status: Optional[str] = None
    # `OfferId`, where the promotion has one. 33.6%.
    offer_id: Optional[str] = None

    # --- availability ------------------------------------------------------
    is_available: Optional[bool] = None
    is_in_stock: Optional[bool] = None
    is_purchasable: Optional[bool] = None
    # `SupplyLimit` — the per-order cap. 100% populated.
    supply_limit: Optional[int] = None

    # --- where it sits in the catalogue -------------------------------------
    # From `AdditionalAttributes`, which carries SAP's merchandising
    # hierarchy. 99.5% on all three.
    department: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None

    # --- what is in it ------------------------------------------------------
    # All five live in `AdditionalAttributes` rather than at the top level,
    # which is why the previous generation of this repo promised them and
    # could not fill them.
    health_star_rating: Optional[float] = None   # 53.6%
    dietary_claims: Optional[str] = None         # 74.4%
    allergy_statement: Optional[str] = None      # 71.8%
    ingredients: Optional[str] = None            # 83.2%
    storage_instructions: Optional[str] = None   # 65.3%

    image_url: Optional[str] = None              # 100%

    # --- provenance ---------------------------------------------------------
    # WHICH of the site's answers built this row.
    #
    #   api-search     POST /apis/ui/Search/products
    #   api-category   POST /apis/ui/browse/category
    #   api-products   GET  /apis/ui/products/{codes}
    #   dom            the rendered tile, the fallback path
    data_source: Optional[str] = None
    # The fetch this row came from, and its position within it. Unique as a
    # PAIR across a run — `position` restarts at 1 on every page, so the
    # column is worthless without `page` beside it, and `smoke_test.py`
    # asserts the pair is unique (§18).
    #
    # Note that `sku` is NOT unique across a multi-page run before deduping,
    # and that is the ads above rather than a bug.
    page: Optional[int] = None
    position: Optional[int] = None


# Both modes yield the same class: a Woolworths row is a product whichever
# way it was selected, and the API answers both with the same object.
ROW_CLASS_BY_MODE = {"search": Product, "category": Product}

# Modes whose rows are one-per-sku AFTER the dedupe in `save()`, and
# therefore safe to hand to diff_runs.py. Both qualify — see `is_sponsored`
# for why the dedupe is not optional here.
UNIQUE_BY_SKU_MODES = ("search", "category")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output. This site needs that more
    than its siblings do: a scroll batch re-parses the WHOLE feed, cards
    already read included, so every batch after the first arrives mostly
    duplicate by design. A batch that drops all of its rows is the signal
    that the feed is exhausted, which is §7's data-based terminating
    condition and the only one available here.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    All three of this repo's modes are one row per `sku`, so `key` is never
    overridden here — the parameter exists because the rest of the family
    shares this function and one of them needs it.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On this site this code specifically does NOT cover the three ways to get a
# real page with no products on it: a `/p/<slug>` discovery hub, which
# answers 200 with banners and carousels and no stories; a tag whose feed
# matches nothing ("Oops, produk nggak ditemukan"); and one page past the
# end of a category listing. All three are EXIT_NO_PRODUCTS — the request
# was served exactly as asked and simply has no products on it. Reporting
# any of them as blocked would send a user hunting for a proxy problem that
# does not exist.
#
# What EXIT_BLOCKED means here is unusually literal: this site refuses a
# address it has scored NOTHING at all. No status code, no interstitial, no
# vendor marker — the HTTP/2 stream is reset and the run sees a connection
# error rather than a page.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because all three browser engines return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "search", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are both recorded, and on this site BOTH of them
    genuinely vary. `mode`, because a tag row and an archive row populate
    different columns: reading time and word count come from a payload the
    tag feed does not carry at all (21 fields per post against the archive's
    84), so diffing one against the other would report both as having
    appeared from nowhere. `source`, because a row must say which host
    domain, and two runs that landed on different hosts describe the same
    catalogue through different addresses. diff_runs.py refuses a pair whose
    modes differ.

    `extra` carries facts about the run that are not about any single row.
    This repo puts the scroll trace there, plus `rows_new_per_batch`, and —
    in archive mode — the DAYS a run covered and any day that redirected.
    A redirect is the thing worth recording: a tag day with no stories does
    not 404, it redirects up to the month view, which is a different
    renderer holding a different set of stories (FINDINGS.md §4).

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the correct output.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected outcome.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# Woolworths publishes no `link[rel=next]`, no pagination control and no
# numbered anchors anywhere — it publishes no product markup at all, so there
# is nothing to put one in. What it has instead is better: a page is an
# INTEGER in a request body, so page N is addressable without reading page
# N-1, and `--concurrency` is meaningful here where it is not in every
# sibling repo (§7).
#
# That makes the terminating condition data rather than markup by
# construction, which is what §7 asks for. The difficulty is the shape that
# data takes — see below.
#
# "pagination_exhausted" is the data-side termination condition here, and on
# this site recognising it is the whole difficulty: the listing has a last
# page but the API does not admit to it. Past the end it keeps answering HTTP
# 200 with `Success: true` and NOTHING BUT PROMOTED ADS — page 17 of a
# 16-page category returned 1 row, page 18 returned 8 and page 99 returned 8,
# every one of them sponsored and every one a repeat of an ad page 1 already
# carried. So the condition is "no ORGANIC product this run had not already
# seen", never "the page was empty", and `page_flow.advance_page` is where it
# is decided.
#
# It is a COMPLETE reason, and that distinction is the point of this tuple: a
# run that asked for 8 pages of a 2-page listing and stopped at 2 read the
# whole listing. Reporting it `partial` (exit 6) would make every correct
# run of a small category look like a failure, and would put the canary
# permanently red — which teaches everyone to ignore the canary (§11).
#
# What must NOT reach here is a listing that stopped because the API was
# REFUSED. The document answers 200 while `/apis/ui/...` behind it is
# throttled, so the two are the same observation from the outside; the
# engines keep them apart by recording the refusal count and reporting
# `api_error` instead, because a throttled run reported as an exhausted
# listing is how a run holding page 1 says "complete" (§7).
#
# "no_new_products" is kept for the family's shape, so a consumer that
# branches on a sibling repo's stop reason keeps working.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted",
                         "no_new_products")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "search", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    complete = stop_reason in COMPLETE_STOP_REASONS
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are genuinely different things and a pipeline branches on
        # them (§8: blocked is not empty is not partial):
        #
        #   blocked          something stood between the run and the content
        #   did not complete we never reached the site — a dead proxy, a
        #                    load timeout, a refused batch
        #   completed        we asked, and the site's reply was nothing
        #
        # The middle one used to fall through to EXIT_NO_PRODUCTS, and that
        # was measured rather than reasoned about in a sibling repo: an
        # unreachable proxy produced exit 4 — "ran fine, found nothing" — on
        # a feed with hundreds of rows, while the sidecar beside it said
        # `status: failed`, `pages_completed: 0`. A consumer branching on the
        # exit code, which is what this family says exit codes are for, would
        # have recorded an empty catalogue.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Failed run: 0 of {pages_requested} page(s) were "
                  f"fetched ({stop_reason}). This is NOT an empty result — "
                  f"nothing was read from the site at all.")
            return EXIT_PARTIAL
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
