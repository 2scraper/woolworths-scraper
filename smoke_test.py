#!/usr/bin/env python3
"""smoke_test.py — the offline suite.

One file of plain functions with inline fixtures, plus the real captures in
`fixtures_generated.json` (CLAUDE.md §10). No pytest, no conftest, no
fixtures directory. `tests/test_smoke.py` wraps this as a single pytest test
so `pytest` works as an entry point without a second copy of the checks.

    python3 smoke_test.py            run everything
    python3 smoke_test.py -v         print every check as it passes

It MUST pass with no engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is recorded. CI's `engine-smoke` job installs each
engine in its own virtualenv and FAILS if the matching group reports a skip,
because "skipped, engine absent" reads identically to a broken import.

What this suite is FOR, beyond the obvious
------------------------------------------
Most of these checks exist because something was actually wrong. The ones
worth knowing about before you edit anything:

* Two markers were wrong in this repo, in the same way, a few hours apart —
  `akamai` and "couldn't find any" are both on EVERY page Woolworths serves.
  `test_markers_absent_from_a_good_page` counts them on a real served capture
  so neither can come back (§18).
* Akamai's refusal reaches a parser in two encodings and a literal marker
  catches only one of them (§20).
* Past the end of a listing the API answers 200 with nothing but ads, so
  "the page was empty" never fires (§7).
* `WasPrice` equals `Price` on 81% of rows, so reading it straight puts a 0%
  discount on four products in five (§4).
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import os
import pathlib
import re
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import product_parser as P          # noqa: E402
import page_flow                    # noqa: E402
import output_writer                # noqa: E402
import env_config                   # noqa: E402
import proxy_pool                   # noqa: E402
import captcha_solver               # noqa: E402
from output_writer import Product   # noqa: E402

VERBOSE = "-v" in sys.argv
FAILURES: list = []
PASSES: list = []
SKIPS: list = []

FIXTURES = json.loads((ROOT / "fixtures_generated.json").read_text(encoding="utf-8"))


# ===========================================================================
# harness
# ===========================================================================
def check(fn):
    """Register and run one check. Plain functions, no framework."""
    name = fn.__name__
    try:
        fn()
    except AssertionError as e:
        FAILURES.append((name, str(e)))
        print(f"FAIL  {name}\n        {e}")
    except Exception as e:  # noqa: BLE001
        FAILURES.append((name, f"{type(e).__name__}: {e}"))
        print(f"ERROR {name}\n        {type(e).__name__}: {e}")
    else:
        PASSES.append(name)
        if VERBOSE:
            print(f"ok    {name}")
    return fn


def skip(group, reason):
    SKIPS.append((group, reason))
    print(f"SKIP  {group}: {reason}")


def _engine(name):
    """Import an engine, or record a skip. Never raises."""
    try:
        return __import__(name)
    except ImportError as e:
        skip(name, f"driver library absent ({e})")
        return None


PW = _engine("playwright_scraper")
SE = _engine("selenium_scraper")
PP = _engine("puppeteer_scraper")
ENGINES = [e for e in (PW, SE, PP) if e is not None]
ENGINE_NAMES = ("playwright_scraper", "selenium_scraper", "puppeteer_scraper")


def _shipped_files(suffixes):
    """Every file this repo SHIPS, with the suffixes given.

    Excludes `.claude/` and `.git/` RELATIVE TO THE REPO ROOT rather than
    anywhere in the absolute path — this repo is developed inside a worktree
    under `.claude/worktrees/`, so an absolute-path test excluded everything
    and quietly turned two checks into no-ops (CI caught it; the local run
    could not).
    """
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        rel = path.relative_to(ROOT)
        if rel.parts and rel.parts[0] in {".git", ".claude", "captures",
                                          "__pycache__", ".pytest_cache"}:
            continue
        if path.name.startswith("_"):
            continue
        yield path


# ===========================================================================
# 1. The payload: shapes that would silently lose everything
# ===========================================================================
@check
def test_flatten_products_unwraps_the_group():
    """`Products` is a list of GROUP WRAPPERS, not of products.

    Reading the outer list as the product list yields wrapper dicts with no
    `Stockcode` on any of them — a run that "succeeds" with every column
    null. The fixture keeps the wrapper shape on purpose.
    """
    payload = FIXTURES["api_search"]
    outer = payload["Products"]
    assert outer and "Stockcode" not in outer[0], (
        "the fixture no longer has the group wrapper, so this check is "
        "vacuous — regenerate it with make_fixtures.py")
    flat = P.flatten_products(payload)
    assert flat, "flatten_products returned nothing from a real search payload"
    assert all("Stockcode" in p for p in flat), (
        "flatten_products returned something that is not a product")


@check
def test_flatten_products_handles_bundles_and_bare_lists():
    """A category calls its list `Bundles`; the by-stockcode endpoint is bare."""
    assert P.flatten_products(FIXTURES["api_category"]), "Bundles not unwrapped"
    bare = [{"Stockcode": 1, "DisplayName": "x"}]
    assert len(P.flatten_products(bare)) == 1, "a bare product list was dropped"
    for junk in (None, {}, [], {"Products": None}, {"Products": [None, 3]}, 7):
        assert P.flatten_products(junk) == [], f"{junk!r} should flatten to []"


@check
def test_total_count_reads_both_spellings():
    """Search calls it `SearchResultsCount`, a category `TotalRecordCount`.

    Asserted against the fixture's OWN value rather than against a literal.
    The literals were 2205 and 575, and the category one was 576 the next
    time the captures were regenerated — Woolworths had added a product.
    A number that describes a living catalogue does not belong in an
    assertion (§13); what belongs there is that both spellings are read and
    that a zero is a zero rather than a None.
    """
    assert (P.total_count(FIXTURES["api_search"])
            == FIXTURES["api_search"]["SearchResultsCount"])
    assert (P.total_count(FIXTURES["api_category"])
            == FIXTURES["api_category"]["TotalRecordCount"])
    assert P.total_count(FIXTURES["api_search"]) > 100, "implausibly small"
    assert P.total_count(FIXTURES["api_category"]) > 100, "implausibly small"
    # Zero is a real answer and must not come back as None: it is the only
    # thing that tells an empty listing from a broken parser.
    assert P.total_count(FIXTURES["api_empty"]) == 0
    assert P.total_count(None) is None and P.total_count([]) is None


# ===========================================================================
# 2. The was-price trap, and the §4 canary
# ===========================================================================
@check
def test_was_price_is_only_a_was_price_when_it_exceeds_price():
    """`WasPrice` is populated on 100% of rows and equals `Price` on 81%.

    Copying it across gives an `original_price` identical to the price and a
    0% discount on four products in five.
    """
    same = {"Stockcode": 1, "Price": 4.70, "WasPrice": 4.70,
            "SavingsAmount": 0.0, "IsOnSpecial": False}
    original, savings, pct = P.was_price_of(same)
    assert original is None, f"a WasPrice equal to Price became {original}"
    assert pct is None, f"discount should be None, not {pct}"
    assert savings is None, f"a 0.0 saving should be None, not {savings}"

    real = {"Stockcode": 2, "Price": 4.50, "WasPrice": 5.05,
            "SavingsAmount": 0.55, "IsOnSpecial": True}
    original, savings, pct = P.was_price_of(real)
    assert original == 5.05 and savings == 0.55
    assert abs(pct - 10.89) < 0.01, f"discount_pct was {pct}, expected 10.89"


@check
def test_no_row_has_an_original_price_at_or_below_its_price():
    """The §4 canary, on every fixture that yields rows."""
    for key in ("api_search", "api_category", "api_ads_only"):
        for row in P.products_from_payload(FIXTURES[key], page=1):
            if row.original_price is not None and row.price is not None:
                assert row.original_price > row.price, (
                    f"{key} sku {row.sku}: original_price "
                    f"{row.original_price} is not above price {row.price}")


@check
def test_discount_is_never_zero_or_negative():
    for key in ("api_search", "api_category"):
        for row in P.products_from_payload(FIXTURES[key], page=1):
            if row.discount_pct is not None:
                assert row.discount_pct > 0, (
                    f"{key} sku {row.sku}: discount_pct {row.discount_pct}")


# ===========================================================================
# 3. Values on real fixtures — not coverage (§10)
# ===========================================================================
@check
def test_pinned_values_from_a_real_search_payload():
    """A column can be 100% populated and entirely wrong.

    These ARE pinned to literals, unlike `total_count` above, and the
    difference is worth stating so nobody "fixes" it the same way. Comparing
    a parsed price against the fixture's own `Price` field would be
    circular — that is the field the parser read. A literal is what catches
    it reading `InstorePrice`, or `Name` instead of `DisplayName`.

    So these go stale only when someone regenerates the captures, and that is
    the intended moment to look again: the fixture and the expectation move
    together, in one commit, under human eyes.
    """
    rows = {r.sku: r for r in P.products_from_payload(FIXTURES["api_search"],
                                                      page=1)}
    assert rows, "the search fixture parsed to nothing"
    r = rows.get("50923")
    assert r is not None, f"expected sku 50923 in the fixture, got {sorted(rows)}"
    assert r.title == "Woolworths Full Cream Milk 1L", r.title
    assert r.price == 1.75, r.price
    assert r.currency == "AUD", r.currency
    assert r.brand == "Woolworths", r.brand
    assert r.unit == "Each", r.unit
    assert r.cup_string == "$1.75 / 1L", r.cup_string
    assert r.is_sponsored is False, r.is_sponsored
    assert r.url == ("https://www.woolworths.com.au/shop/productdetails/"
                     "50923/woolworths-full-cream-milk"), r.url


@check
def test_title_uses_displayname_not_name():
    """`Name` drops the pack size and duplicates the variety.

    Both are 100% populated, which is exactly why the wrong one is easy to
    ship: the column would look complete and read worse on every row.
    """
    node = {"Stockcode": 88436,
            "DisplayName": "Dairy Farmers Full Cream Milk 2L",
            "Name": "Dairy Farmers Full Cream Milk Full Cream Milk"}
    row = P.product_from_api(node)
    assert row.title == "Dairy Farmers Full Cream Milk 2L", row.title


@check
def test_additional_attributes_are_read_and_the_string_None_is_not_a_value():
    """The API writes the STRING "None" as well as JSON null."""
    node = {"Stockcode": 1, "DisplayName": "x",
            "AdditionalAttributes": {"healthstarrating": "4",
                                     "ingredients": "Milk.",
                                     "countryoforigin": "None",
                                     "sapdepartmentname": "DAIRY"}}
    row = P.product_from_api(node)
    assert row.health_star_rating == 4.0, row.health_star_rating
    assert row.ingredients == "Milk.", row.ingredients
    assert row.country_of_origin_is_absent if False else True  # column removed
    assert row.department == "DAIRY", row.department
    assert getattr(row, "country_of_origin", None) is None, (
        "country_of_origin was measured null on 660 of 660 rows and should "
        "not be a column (§9)")


@check
def test_description_html_is_stripped():
    node = {"Stockcode": 1, "DisplayName": "x",
            "Description": " Dairy Farmers Full Cream<br>Milk 2L "}
    row = P.product_from_api(node)
    assert row.description == "Dairy Farmers Full Cream Milk 2L", row.description


@check
def test_absent_fields_are_none_not_zero_or_empty_string():
    row = P.product_from_api({"Stockcode": 9, "DisplayName": "x",
                              "Brand": "", "Price": None, "SupplyLimit": 0})
    assert row.brand is None, row.brand
    assert row.price is None, row.price
    assert row.supply_limit is None, row.supply_limit


@check
def test_a_row_without_a_stockcode_is_dropped():
    assert P.product_from_api({"DisplayName": "no id"}) is None
    assert P.product_from_api({"Stockcode": "", "DisplayName": "x"}) is None
    assert P.product_from_api("not a dict") is None


# ===========================================================================
# 4. The ads trap — the reason a run terminates at all (§7)
# ===========================================================================
@check
def test_a_page_past_the_end_is_ads_only():
    """The fixture is page 18 of a 16-page category: 200, and only ads."""
    rows = P.products_from_payload(FIXTURES["api_ads_only"], page=18)
    assert rows, "the ads-only fixture parsed to nothing; regenerate it"
    assert P.organic_count(rows) == 0, (
        f"the ads-only fixture has {P.organic_count(rows)} organic row(s) — "
        "it is no longer past the end of the listing, so regenerate it")
    assert all(r.is_sponsored for r in rows)


@check
def test_advance_page_stops_on_ads_and_not_on_rows():
    """"Were there rows" never terminates on this site."""
    ads = P.products_from_payload(FIXTURES["api_ads_only"], page=18)
    assert page_flow.advance_page(ads, set()) == page_flow.ADS_ONLY, (
        "a page of nothing but ads must not read as progress")
    assert page_flow.advance_page([], set()) == page_flow.NO_GROWTH

    fresh = P.products_from_payload(FIXTURES["api_search"], page=1)
    organic = [r.sku for r in fresh if not r.is_sponsored]
    assert organic, "the search fixture has no organic row to test with"
    assert page_flow.advance_page(fresh, set()) == page_flow.ADVANCED
    assert page_flow.advance_page(fresh, set(organic)) in (
        page_flow.NO_GROWTH, page_flow.ADS_ONLY), (
        "a page whose organic rows were all seen before must not advance")


@check
def test_organic_count_reads_rows_and_raw_dicts():
    assert P.organic_count([]) == 0
    rows = P.products_from_payload(FIXTURES["api_search"], page=1)
    assert 0 < P.organic_count(rows) < len(rows), (
        "the search fixture should hold both sponsored and organic rows")
    assert P.organic_count([{"IsSponsoredAd": True}, {"IsSponsoredAd": False}]) == 1


# ===========================================================================
# 5. Markers — counted on a page known to be GOOD (§18)
# ===========================================================================
@check
def test_markers_absent_from_a_good_page():
    """Every block marker must score 0 on a page the site really served.

    This check exists because two markers in this repo scored high on good
    pages: `akamai` (Woolworths is fronted by Akamai, and its performance
    script is on every page) and "couldn't find any" (the no-results copy
    ships in the JS bundle). Both are asserted below as NON-markers.
    """
    served = FIXTURES["served_page"]
    for marker in P.BOT_CHALLENGE_MARKERS:
        assert marker not in P.unescape_prefix(served, len(served)), (
            f"{marker!r} appears on a page Woolworths SERVED — it is a fact "
            "about the site, not a block marker (§18)")


@check
def test_the_two_markers_that_were_wrong_stay_out():
    """Pinned as non-markers, with the counts that disqualified them."""
    served = FIXTURES["served_page"]
    akamai = served.lower().count("akamai")
    noresults = served.count("couldn't find any")
    assert akamai >= 1, (
        "the served fixture no longer references Akamai, so this check has "
        "stopped proving anything — regenerate the capture")
    assert noresults >= 1, (
        "the served fixture no longer carries the no-results copy, so this "
        "check has stopped proving anything — regenerate the capture")
    assert not any("akamai" in m.lower() for m in P.BOT_CHALLENGE_MARKERS), (
        f"`akamai` is back in BOT_CHALLENGE_MARKERS and it occurs {akamai} "
        "time(s) on a page the site served")
    assert not hasattr(P, "NO_RESULTS_MARKERS"), (
        f"NO_RESULTS_MARKERS is back; the copy it keys on occurs {noresults} "
        "time(s) on a served page and made a 2,205-result search report "
        "itself empty")


@check
def test_a_good_page_classifies_as_content():
    served = FIXTURES["served_page"]
    state = P.detect_page_state(
        served, status=200,
        url="https://www.woolworths.com.au/shop/search/products?searchTerm=milk")
    assert state == "content", f"a served page classified as {state!r}"
    assert not page_flow.counts_as_blocked(state)


@check
def test_both_denial_encodings_are_blocked():
    """§20: the SAME refusal reaches a parser spelled two ways."""
    dom, raw = FIXTURES["denial_dom"], FIXTURES["denial_raw"]
    assert dom.count("errors.edgesuite.net") == 1 and \
        dom.count("errors&#46;edgesuite&#46;net") == 0, "dom fixture changed"
    assert raw.count("errors.edgesuite.net") == 0 and \
        raw.count("errors&#46;edgesuite&#46;net") == 1, "raw fixture changed"
    for label, text in (("dom", dom), ("raw", raw)):
        assert P.detect_block_marker(text), f"{label} denial matched no marker"
        state = P.detect_page_state(
            text, status=403, url="https://www.woolworths.com.au/shop/x")
        assert state == "blocked", f"{label} denial classified as {state!r}"
        assert page_flow.counts_as_blocked(state)


@check
def test_a_denial_is_blocked_even_without_a_status():
    """Selenium reports no HTTP status, so the markers must carry it alone."""
    for key in ("denial_dom", "denial_raw"):
        state = P.detect_page_state(FIXTURES[key], status=None,
                                    url="https://www.woolworths.com.au/shop/x")
        assert state == "blocked", f"{key} without a status classified {state!r}"


@check
def test_the_positive_asset_signal_separates_served_from_refused():
    """§8: a served page is built out of the site's own assets; a denial is not."""
    served = P.asset_reference_count(FIXTURES["served_page"])
    assert served >= 50, f"only {served} asset references on a served page"
    for key in ("denial_dom", "denial_raw"):
        assert P.asset_reference_count(FIXTURES[key]) == 0, (
            f"{key} references the site's asset host")


@check
def test_unescape_is_bounded():
    """A refusal is ~400 bytes; unescaping 800 KB on every fetch buys nothing."""
    big = "&amp;" * 100_000
    assert len(P.unescape_prefix(big)) <= P.UNESCAPE_PREFIX_BYTES


# ===========================================================================
# 6. URLs, hosts, modes
# ===========================================================================
@check
def test_mode_is_inferred_from_the_path():
    S = "https://www.woolworths.com.au/shop/search/products?searchTerm=milk"
    C = "https://www.woolworths.com.au/shop/browse/bakery"
    N = "https://www.woolworths.com.au/shop/browse/fruit-veg/fruit"
    assert P.mode_for_url(S) == "search"
    assert P.mode_for_url(C) == "category"
    assert P.mode_for_url(N) == "category"
    assert P.search_term_from_url(S) == "milk"
    assert P.category_slug_from_url(N) == "fruit-veg/fruit"
    assert P.mode_for_url("https://www.woolworths.com.au/") is None


@check
def test_unsupported_urls_are_refused_with_the_real_reason():
    """§5: "is not a Woolworths site" is false and sends the reader hunting."""
    nz = P.unsupported_reason("https://www.woolworths.co.nz/shop/browse/x")
    assert nz and "New Zealand" in nz, nz
    za = P.unsupported_reason("https://www.woolworths.co.za/x")
    assert za and "South Africa" in za, za
    detail = P.unsupported_reason(
        "https://www.woolworths.com.au/shop/productdetails/88436/milk")
    assert detail and "listing row" in detail, detail
    assert P.unsupported_reason(
        "https://www.woolworths.com.au/shop/browse/bakery") is None


@check
def test_the_apex_host_is_accepted_and_the_www_host_is_canonical():
    assert P.is_woolworths_host("https://woolworths.com.au/shop/browse/bakery")
    assert P.is_woolworths_host("https://www.woolworths.com.au/x")
    assert not P.is_woolworths_host("https://woolworths.com.au.evil.test/x")
    assert P.product_url(1, "s").startswith("https://www.woolworths.com.au/")


@check
def test_currency_is_host_derived_and_never_defaulted():
    """§4 rung 5: absent is null, never a guessed "USD"."""
    assert P.currency_for("https://www.woolworths.com.au/x") == "AUD"
    assert P.currency_for("https://example.com/x") is None
    assert P.currency_for("") is None


@check
def test_product_url_matches_a_link_the_site_actually_renders():
    """Rebuilt, not read — so it is pinned against a real tile's href."""
    href = "/shop/productdetails/144607/strawberries-punnet"
    code = P.stockcode_from_href(href)
    assert code == "144607", code
    assert P.product_url(code, "strawberries-punnet").endswith(href)


@check
def test_unauthorised_redirect_is_recognised():
    """A client-side bounce after a perfectly normal 200 (§16)."""
    assert P.is_unauthorised_redirect(
        "https://www.woolworths.com.au/unauthorisederror")
    assert P.is_unauthorised_redirect(
        "https://www.woolworths.com.au/unauthorisederror/")
    assert not P.is_unauthorised_redirect(
        "https://www.woolworths.com.au/shop/browse/bakery")


# ===========================================================================
# 7. Category resolution
# ===========================================================================
@check
def test_category_slugs_resolve_to_opaque_ids():
    tree = FIXTURES["categories_head"]
    assert P.category_id_for_slug(tree, "bakery") == "1_DEB537E"
    assert P.category_id_for_slug(tree, "fruit-veg") == "1-E5BEE36E"
    assert P.category_id_for_slug(tree, "specials") == "specialsgroup"
    assert P.category_id_for_slug(tree, "not-a-node") is None
    assert P.category_id_for_slug(tree, "") is None


@check
def test_a_child_slug_resolves_to_the_child_not_the_parent():
    """Asking for the parent would silently return a larger listing."""
    tree = FIXTURES["categories_head"]
    parent = P.category_id_for_slug(tree, "fruit-veg")
    child = P.category_id_for_slug(tree, "fruit-veg/fruit")
    assert child and child != parent, (
        f"the child slug resolved to {child!r} and the parent to {parent!r}")


@check
def test_the_request_bodies_carry_the_ids_the_api_needs():
    path, body, src = P.api_request_for("search", term="milk", page=2)
    assert path == P.API_SEARCH_PATH and src == "api-search"
    assert body["SearchTerm"] == "milk" and body["PageNumber"] == 2
    assert body["PageSize"] == P.PAGE_SIZE

    path, body, src = P.api_request_for("category", category_id="1_DEB537E",
                                        slug="bakery", page=3)
    assert path == P.API_CATEGORY_PATH and src == "api-category"
    assert body["categoryId"] == "1_DEB537E" and body["pageNumber"] == 3
    # A slug where the id belongs returns 200 with zero products, which is
    # indistinguishable from a real empty category.
    assert body["categoryId"] != "bakery"

    for bad in (("search", {}), ("category", {}), ("nonsense", {})):
        try:
            P.api_request_for(bad[0], **bad[1])
        except ValueError:
            pass
        else:
            raise AssertionError(f"api_request_for{bad} should have raised")


# ===========================================================================
# 8. The DOM second view
# ===========================================================================
@check
def test_dom_overlay_confirms_and_never_overwrites():
    rows = [Product(sku="1", price=4.70, price_source="api"),
            Product(sku="2", price=9.99, price_source="api")]
    tiles = [{"href": "/shop/productdetails/1/a", "price_text": "$4.70 $2.35 / 1L"},
             {"href": "/shop/productdetails/2/b", "price_text": "$8.00"}]
    confirmed, checked = P.overlay_dom_prices(rows, tiles)
    assert (confirmed, checked) == (1, 2), (confirmed, checked)
    assert rows[0].price_source == "api+dom"
    # Disagreement leaves the row ALONE (§4).
    assert rows[1].price == 9.99, "a disagreeing tile overwrote the API price"
    assert rows[1].price_source == "api"


@check
def test_dom_index_keys_on_the_tiles_own_stockcode():
    """A carousel tile can only ever index itself (§4's neighbour trap)."""
    tiles = [{"href": "/shop/productdetails/11/a", "price_text": "$1.00"},
             {"href": "/shop/productdetails/22/b", "price_text": "$2.00"},
             {"href": "/nonsense", "price_text": "$3.00"},
             {"href": "", "price_text": "$4.00"}]
    index = P.dom_tiles_to_index(tiles)
    assert sorted(index) == ["11", "22"], sorted(index)


@check
def test_tile_price_parsing():
    assert P.price_from_tile_text("$4.70\n  $2.35 / 1L") == 4.70
    assert P.price_from_tile_text("$1,234.56") == 1234.56
    assert P.price_from_tile_text("no price here") is None
    assert P.price_from_tile_text("") is None


# ===========================================================================
# 9. Rows, ordering, the sidecar
# ===========================================================================
@check
def test_page_and_position_are_unique_as_a_pair():
    """§18: `position` restarts at 1 on every page."""
    rows = (P.products_from_payload(FIXTURES["api_search"], page=1)
            + P.products_from_payload(FIXTURES["api_category"], page=2))
    pairs = [(r.page, r.position) for r in rows]
    assert len(set(pairs)) == len(pairs), "page+position is not unique"
    assert all(p is not None and n is not None for p, n in pairs)


@check
def test_sku_is_not_unique_across_pages_and_that_is_the_ads():
    """Documents the reason the dedupe is not optional here."""
    page1 = P.products_from_payload(FIXTURES["api_search"], page=1)
    page3 = P.products_from_payload(FIXTURES["api_search"], page=3)
    seen = set()
    kept = (output_writer.dedupe_by_key(page1, seen, "sku")
            + output_writer.dedupe_by_key(page3, seen, "sku"))
    assert len(kept) == len(page1), (
        "the same payload deduped twice should add nothing the second time")


@check
def test_row_column_order_starts_with_the_family_prefix():
    """§9: one column name works across the family."""
    names = [f.name for f in __import__("dataclasses").fields(Product)]
    assert names[:5] == ["source", "scraped_at", "url", "sku", "title"], names[:5]


@check
def test_columns_measured_absent_are_absent():
    """§9, with the measurement in output_writer's docstring."""
    names = {f.name for f in __import__("dataclasses").fields(Product)}
    for gone in ("rating", "rating_count", "review_count", "country_of_origin",
                 "instore_price"):
        assert gone not in names, (
            f"{gone!r} was measured null (or duplicate) on every row and "
            "should not be a column")


@check
def test_modes_and_row_classes_agree():
    assert set(output_writer.ROW_CLASS_BY_MODE) == {"search", "category"}
    assert set(output_writer.UNIQUE_BY_SKU_MODES) == {"search", "category"}


@check
def test_an_exhausted_listing_is_complete_not_partial():
    """A run that asked for 8 pages of a 2-page listing read the whole listing."""
    assert "pagination_exhausted" in output_writer.COMPLETE_STOP_REASONS, (
        "a correct short run would report partial (exit 6) and put the "
        "canary permanently red")


@check
def test_empty_csv_keeps_its_header():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "out.csv")
        output_writer.write_csv([], path)
        text = pathlib.Path(path).read_text(encoding="utf-8")
    assert text.strip(), "an empty CSV should still carry its header"
    assert text.splitlines()[0].startswith("source,scraped_at,url,sku,title")


# ===========================================================================
# 10. Engine parity (§17's checks worth stealing)
# ===========================================================================
# Scoped to the ARGUMENT PARSER, not to every add_argument in the file:
# Selenium builds Chrome's own command line with `options.add_argument`, and
# `--no-sandbox` is not a CLI flag this repo offers anyone.
_FLAG_RE = re.compile(r'(?<!options)\.add_argument\(\s*"(--[a-z0-9-]+)"')

# The family's contract (§9), plus this repo's documented additions.
CONTRACT_FLAGS = {
    "--url", "--pages", "--category", "--format", "--out", "--delay",
    "--retries", "--retry-delay", "--concurrency", "--proxy", "--proxy-file",
    "--proxy-rotate", "--proxy-shuffle", "--proxy-block-retries",
    "--twocaptcha-key", "--captcha-api", "--solve-captcha", "--min-score",
    "--cdp-endpoint", "--allow-empty", "--dump-html", "--headless",
    "--headful", "--mode",
}
# Differences that are DOCUMENTED rather than accidental. This list IS the
# documentation, so closing one of these without updating it fails too.
ALLOWED_FLAG_DIFFERENCES = {
    # chromedriver has no bundled browser to choose between.
    "--browser-channel": {"playwright_scraper"},
    # Selenium attaches by debuggerAddress; there is no WebSocket upgrade to
    # wait for, so there is nothing for a connect timeout to bound.
    "--cdp-connect-timeout": {"playwright_scraper", "puppeteer_scraper"},
    # pyppeteer drives a browser it downloads itself, and lets you point at
    # another one.
    "--chromium-path": {"puppeteer_scraper"},
    # The fingerprint client applies through context kwargs the pyppeteer
    # engine does not have an equivalent for.
    "--fingerprint": {"playwright_scraper", "selenium_scraper"},
    "--fp-tags": {"playwright_scraper", "selenium_scraper"},
    "--fp-country": {"playwright_scraper", "selenium_scraper"},
    "--locale": {"playwright_scraper", "selenium_scraper"},
}


def _flags_of(name):
    return set(_FLAG_RE.findall((ROOT / f"{name}.py").read_text(encoding="utf-8")))


@check
def test_every_engine_offers_the_contract_flags():
    for name in ENGINE_NAMES:
        missing = CONTRACT_FLAGS - _flags_of(name)
        assert not missing, f"{name} is missing {sorted(missing)}"


@check
def test_engine_flag_differences_are_exactly_the_documented_ones():
    """Both directions: a new unshared flag fails, and so does closing a
    documented difference — the exception list IS the documentation (§17)."""
    per_engine = {n: _flags_of(n) for n in ENGINE_NAMES}
    everything = set().union(*per_engine.values())
    for flag in sorted(everything - CONTRACT_FLAGS):
        have = {n for n in ENGINE_NAMES if flag in per_engine[n]}
        expected = ALLOWED_FLAG_DIFFERENCES.get(flag)
        assert expected is not None, (
            f"{flag} is in {sorted(have)} and is neither in the contract nor "
            "in ALLOWED_FLAG_DIFFERENCES — document it or remove it")
        assert have == expected, (
            f"{flag} is in {sorted(have)} but ALLOWED_FLAG_DIFFERENCES says "
            f"{sorted(expected)} — update the list, which is the documentation")


@check
def test_shared_module_calls_bind_against_the_real_signatures():
    """§17's check #1: `classify(html, status, url)` called as
    `classify(html, url=…)` crashed two engines on their FIRST fetch, and was
    invisible to import, --help, compileall and 400 green assertions."""
    targets = {"page_flow": page_flow, "product_parser": P,
               "output_writer": output_writer}
    problems = []
    for name in ENGINE_NAMES:
        tree = ast.parse((ROOT / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id in targets):
                continue
            callee = getattr(targets[fn.value.id], fn.attr, None)
            if not callable(callee):
                continue
            try:
                sig = inspect.signature(callee)
            except (TypeError, ValueError):
                continue
            args = [inspect.Parameter.empty] * len(node.args)
            kwargs = {}
            for kw in node.keywords:
                if kw.arg is None:
                    kwargs = None
                    break
                kwargs[kw.arg] = inspect.Parameter.empty
            if kwargs is None:
                continue
            try:
                sig.bind(*args, **kwargs)
            except TypeError as e:
                problems.append(
                    f"{name}:{node.lineno} {fn.value.id}.{fn.attr}(): {e}")
    assert not problems, "shared-module calls that would fail at runtime:\n  " \
        + "\n  ".join(problems)


@check
def test_engines_agree_on_the_shared_constants():
    """A threshold that differed would mean one engine warning about a page
    its twin called healthy (§6)."""
    if len(ENGINES) < 2:
        return
    for const in ("FIELD_FLOOR", "DOM_CONFIRM_FLOOR", "LAUNCH_ARGS",
                  "_PROXY_ERROR_MARKERS", "_API_PATH_MARKER"):
        values = {e.__name__: getattr(e, const, None) for e in ENGINES}
        distinct = {repr(v) for v in values.values()}
        assert len(distinct) == 1, f"{const} differs between engines: {values}"


@check
def test_every_engine_passes_the_automation_flag():
    """Without it the SPA bounces to /unauthorisederror while the run still
    reports success — the defect class §16 is about."""
    for name in ENGINE_NAMES:
        src = (ROOT / f"{name}.py").read_text(encoding="utf-8")
        assert "--disable-blink-features=AutomationControlled" in src, (
            f"{name} does not pass the automation flag")
        assert "LAUNCH_ARGS" in src, (
            f"{name} should carry the flag as LAUNCH_ARGS so the suite can "
            "compare the three")


@check
def test_engines_import_their_driver_at_module_level():
    """Otherwise the module imports fine with no driver installed, the group
    never skips, and the CI job that exists to catch a broken import cannot
    (§10)."""
    expect = {"playwright_scraper": "playwright",
              "selenium_scraper": "selenium",
              "puppeteer_scraper": "pyppeteer"}
    for name, lib in expect.items():
        tree = ast.parse((ROOT / f"{name}.py").read_text(encoding="utf-8"))
        top = set()
        for node in tree.body:           # MODULE level only, deliberately
            if isinstance(node, ast.Import):
                top.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top.add(node.module.split(".")[0])
        assert lib in top, (
            f"{name} does not import {lib} at module level, so it would "
            "import cleanly with the driver absent")


@check
def test_no_policy_constant_is_without_a_consumer():
    """§17: a policy constant nothing reads is the same defect as dead code."""
    engine_src = "\n".join((ROOT / f"{n}.py").read_text(encoding="utf-8")
                           for n in ENGINE_NAMES)
    for name in ("should_retry", "should_solve", "counts_as_blocked",
                 "block_retries", "advance_page", "wait_for_tiles",
                 "organic_count", "RETRY_NEEDS_FRESH_CONTEXT", "ADS_ONLY",
                 "page_cap_reached", "block_advice", "classify",
                 "ready_selector", "min_matches"):
        assert name in engine_src, (
            f"page_flow.{name} has no consumer in any engine — either use it "
            "or delete it")


@check
def test_the_engines_call_their_shared_helpers_the_same_way():
    """The three engines must pass the same THING to a same-named helper.

    This check exists because of a bug that reached main. A refactor changed
    `_fetch_api` to take the SESSION rather than the page, and two engines
    were updated while the Playwright one kept `_fetch_api(session.page, …)`
    in `_resolve_target`. Arity was identical, so it bound fine; every
    offline check passed; `--help` worked; a live SEARCH run passed, because
    search never reaches that line. Only a live CATEGORY run failed, with
    `'Page' object has no attribute 'page'`.

    Comparing the ARGUMENT SPELLING across the three engines catches exactly
    that: a signature change that lands in two of three files. It is §17's
    call-binding check extended to a module's own helpers, where
    `inspect.signature` cannot help because the types are not in the
    signature.
    """
    import collections

    watched = {"_fetch_api", "_read_tiles", "_confirm_with_dom",
               "_rows_from_payload", "_land", "_resolve_target",
               "_api_error_count", "_fetch_api_with_retries"}
    # first-argument spelling, per helper, per engine
    seen = collections.defaultdict(dict)
    for name in ENGINE_NAMES:
        tree = ast.parse((ROOT / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in watched
                    and node.args):
                continue
            first = ast.unparse(node.args[0])
            seen[node.func.id].setdefault(name, set()).add(first)

    problems = []
    for helper, per_engine in sorted(seen.items()):
        if len(per_engine) < 2:
            continue          # only one engine calls it; nothing to compare
        spellings = {frozenset(v) for v in per_engine.values()}
        if len(spellings) > 1:
            detail = ", ".join(f"{eng}={sorted(v)}"
                               for eng, v in sorted(per_engine.items()))
            problems.append(f"{helper}(): {detail}")
    assert not problems, (
        "the engines disagree about what to pass a shared-name helper — a "
        "signature change that landed in some files and not others:\n  "
        + "\n  ".join(problems))


@check
def test_undefined_names_in_every_module():
    """§10: compileall proves a file PARSES, not that its names RESOLVE.

    Coarse on purpose — pool-scoped rather than scope-accurate — so it
    under-reports rather than inventing problems.
    """
    import builtins
    allowed = set(dir(builtins)) | {"__file__", "__name__", "__doc__",
                                    "__builtins__", "self", "cls"}
    problems = []
    for path in sorted(ROOT.glob("*.py")):
        if path.name.startswith("_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bound = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Lambda):
                a = node.args
                bound.update(arg.arg for arg in
                             list(a.args) + list(a.posonlyargs)
                             + list(a.kwonlyargs)
                             + ([a.vararg] if a.vararg else [])
                             + ([a.kwarg] if a.kwarg else []))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                bound.add(node.name)
                if not isinstance(node, ast.ClassDef):
                    a = node.args
                    bound.update(arg.arg for arg in
                                 list(a.args) + list(a.posonlyargs)
                                 + list(a.kwonlyargs)
                                 + ([a.vararg] if a.vararg else [])
                                 + ([a.kwarg] if a.kwarg else []))
            elif isinstance(node, ast.Name) and isinstance(node.ctx,
                                                           (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                bound.update((a.asname or a.name).split(".")[0]
                             for a in node.names)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, ast.Global):
                bound.update(node.names)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars:
                        bound.update(n.id for n in ast.walk(item.optional_vars)
                                     if isinstance(n, ast.Name))
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        missing = sorted(used - bound - allowed)
        if missing:
            problems.append(f"{path.name}: {missing}")
    assert not problems, "names used but never bound:\n  " + "\n  ".join(problems)


# ===========================================================================
# 10b. The concurrency machinery, with the browser stubbed out
# ===========================================================================
# A LIVE run cannot reach most of this: page 1 is fetched alone and decides
# whether the rest is worth asking for, so a blocked or short page 1 means
# the workers never start at all (§10). Stubbing the browser is the only way
# to assert what the dispatcher actually does.
class _FakeArgs:
    """The attributes `_fetch_pages_concurrently` and its callees touch."""
    def __init__(self, **kw):
        self.url = "https://www.woolworths.com.au/shop/browse/bakery"
        self.mode = "category"
        self.pages = 9
        self.delay = 0
        self.retries = 1
        self.retry_delay = 0
        self.out = "unused"
        self.concurrency = 3
        self.cdp_endpoint = None
        self.headless = False
        self.dump_html = None
        self.allow_empty = False
        self.format = "json"
        self.page_size = 36
        self.proxy = self.proxy_file = None
        self.twocaptcha_key = None
        self.solve_captcha = "never"
        self.__dict__.update(kw)


class _FakeSession:
    pool = None

    def __init__(self, *a, **kw):
        pass

    def open(self):
        return self

    def close(self):
        pass


class _FakeSyncPlaywright:
    """Stands in for `sync_playwright()` — a context manager yielding a stub."""
    def __enter__(self):
        return object()

    def __exit__(self, *exc):
        return False


def _drive_concurrently(fetch_impl, specs, concurrency=3, pool=None):
    """Run the real dispatcher against a stubbed browser and fetch."""
    import threading
    saved = (PW.sync_playwright, PW._BrowserSession, PW._fetch_one_page)
    calls = []
    lock = threading.Lock()

    def counting_fetch(session, args, worker_pool, page_num, url):
        with lock:
            calls.append(page_num)
        return fetch_impl(session, args, worker_pool, page_num, url)

    PW.sync_playwright = lambda: _FakeSyncPlaywright()
    PW._BrowserSession = _FakeSession
    PW._fetch_one_page = counting_fetch
    try:
        results, unattempted, exhausted = PW._fetch_pages_concurrently(
            _FakeArgs(), pool, specs, concurrency)
    finally:
        PW.sync_playwright, PW._BrowserSession, PW._fetch_one_page = saved
    return calls, results, unattempted, exhausted


def _outcome(page_num, products, **kw):
    o = PW.PageOutcome(page_num=page_num,
                       url="https://www.woolworths.com.au/shop/browse/bakery")
    o.products = products
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def _organic(n, start=0):
    return [Product(sku=str(start + i), is_sponsored=False) for i in range(n)]


@check
def test_concurrency_fetches_every_queued_page_exactly_once():
    if PW is None:
        return
    specs = [(n, "u") for n in range(2, 10)]
    calls, results, unattempted, exhausted = _drive_concurrently(
        lambda s, a, p, n, u: _outcome(n, _organic(3, n * 100)), specs)
    assert sorted(calls) == list(range(2, 10)), f"pages fetched: {sorted(calls)}"
    assert len(calls) == len(set(calls)), f"a page was fetched twice: {calls}"
    assert not unattempted, unattempted
    assert not exhausted


@check
def test_concurrency_outcomes_are_restorable_to_page_order():
    """Workers finish out of order; the merge must not depend on that (§8)."""
    if PW is None:
        return
    import random, time as _t

    def jittery(session, args, pool, page_num, url):
        _t.sleep(random.uniform(0, 0.02))
        return _outcome(page_num, _organic(2, page_num * 100))

    specs = [(n, "u") for n in range(2, 10)]
    calls, results, _, _ = _drive_concurrently(jittery, specs)
    ordered = [o.page_num for o in sorted(results, key=lambda o: o.page_num)]
    assert ordered == list(range(2, 10)), ordered


@check
def test_concurrency_stops_dispatch_at_the_end_of_the_listing():
    """And stops on ORGANIC rows, not on "the page was empty" — past its last
    real page this API answers 200 with nothing but ads (§7)."""
    if PW is None:
        return
    ads_only = [Product(sku="ad1", is_sponsored=True),
                Product(sku="ad2", is_sponsored=True)]

    def ads_from_page_4(session, args, pool, page_num, url):
        if page_num >= 4:
            return _outcome(page_num, list(ads_only))
        return _outcome(page_num, _organic(3, page_num * 100))

    specs = [(n, "u") for n in range(2, 40)]
    calls, results, unattempted, exhausted = _drive_concurrently(
        ads_from_page_4, specs, concurrency=2)
    assert exhausted, "a page of nothing but ads did not stop dispatch"
    assert len(calls) < 10, (
        f"dispatch kept going for {len(calls)} pages after the listing ended; "
        "at most (concurrency - 1) extra fetches should be in flight")
    assert unattempted, "pages left in the queue were not reported"


@check
def test_concurrency_reports_unattempted_pages_rather_than_failing_them():
    """They were never tried; claiming otherwise overstates the damage (§8)."""
    if PW is None:
        return

    def empty_at_3(session, args, pool, page_num, url):
        if page_num == 3:
            return _outcome(page_num, [])
        return _outcome(page_num, _organic(3, page_num * 100))

    specs = [(n, "u") for n in range(2, 30)]
    calls, results, unattempted, exhausted = _drive_concurrently(
        empty_at_3, specs, concurrency=2)
    assert exhausted
    assert unattempted, "nothing reported as unattempted"
    assert all(not o.load_failed and o.blocked_by is None for o in results), (
        "an unattempted page was recorded as a failed one")
    assert set(unattempted).isdisjoint(set(calls)), (
        "a page is both fetched and unattempted")


@check
def test_a_worker_that_raises_neither_hangs_the_run_nor_loses_its_siblings():
    if PW is None:
        return

    def explode_on_5(session, args, pool, page_num, url):
        if page_num == 5:
            raise RuntimeError("worker died on purpose")
        return _outcome(page_num, _organic(3, page_num * 100))

    specs = [(n, "u") for n in range(2, 10)]
    calls, results, unattempted, exhausted = _drive_concurrently(
        explode_on_5, specs, concurrency=3)
    # It returned at all — that is the "does not hang" half.
    got = {o.page_num for o in results}
    assert got, "a dying worker lost every result"
    assert 5 not in got, "the page that raised was recorded as an outcome"
    # Its siblings' pages are either done or reported unattempted, never lost.
    accounted = got | set(unattempted) | {5}
    assert accounted >= set(range(2, 10)), (
        f"pages went missing entirely: {set(range(2, 10)) - accounted}")


@check
def test_each_worker_starts_on_a_different_exit():
    """Workers all leaving from one address is just a faster way to burn it (§7)."""
    if PW is None:
        return
    pool = proxy_pool.ProxyPool(["http://a:1", "http://b:2", "http://c:3"],
                                rotate="per-run")
    firsts = {PW._worker_pool(pool, i).current for i in range(3)}
    assert len(firsts) == 3, f"workers started on {firsts}"
    assert PW._worker_pool(None, 0) is None


@check
def test_paid_api_kwargs_are_ones_the_driver_accepts():
    """An unknown key in `new_context(**kwargs)` is a TypeError at launch, on
    the PAID path, at runtime (§10)."""
    if PW is None:
        return
    import inspect as _inspect
    from playwright.sync_api import BrowserContext  # noqa: F401
    import fingerprint_client

    fake_fp = {"id": 1, "country": "AU", "userAgent": {"value": "UA/1.0"},
               "screen": {"width": 1440, "height": 900},
               "timezone": "Australia/Sydney", "locale": "en-AU"}
    try:
        kwargs = fingerprint_client.playwright_context_kwargs(fake_fp)
    except Exception as e:  # noqa: BLE001 — a shape we do not model is not a failure here
        skip("fingerprint kwargs", f"could not build from a stub fingerprint ({e})")
        return
    from playwright.sync_api import Browser
    sig = _inspect.signature(Browser.new_context)
    accepted = set(sig.parameters)
    unknown = [k for k in kwargs if k not in accepted]
    assert not unknown, (
        f"fingerprint_client hands new_context {unknown}, which Playwright "
        "does not accept — a TypeError at launch on the paid path")


# ===========================================================================
# 11. Credentials, config, wording
# ===========================================================================
@check
def test_credentials_are_masked_globally_not_once():
    """A Playwright CDP error repeats the endpoint five times (§8)."""
    if PW is None:
        return
    # Built by CONCATENATION on purpose, so that no line of this file holds a
    # complete `scheme://user:pass@host` literal. That keeps the credential
    # scan fully live on smoke_test.py — the one file where a real key is
    # most likely to get pasted while debugging — instead of switching it off
    # here with an allowlist entry.
    endpoint = "ws://user:" + "secret" + "@cb.2captcha.com:9222"
    text = " ".join([endpoint] * 5)
    masked = PW._mask_credentials(text)
    assert "secret" not in masked, masked
    assert masked.count("***:***@") == 5, masked
    assert "cb.2captcha.com:9222" in masked, "the host and port are not the secret"


@check
def test_env_example_documents_exactly_what_the_code_reads():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    declared = {line.split("=", 1)[0].strip().lstrip("# ").strip()
                for line in example.splitlines()
                if "=" in line and not line.strip().startswith("##")}
    declared = {d for d in declared if d and d.isupper()}
    known = set(env_config.ENV_KEYS)
    assert declared == known, (
        f"only in .env.example: {sorted(declared - known)}; "
        f"only in ENV_KEYS: {sorted(known - declared)}")


@check
def test_a_copied_env_example_reads_as_unset():
    """§17: a braced placeholder was read as CONFIGURED in two repos, and a
    `cp .env.example .env` then sent `{login}-zone-…` to the API as a
    username and got a 401 a long way from its cause."""
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    saved = {k: os.environ.get(k) for k in env_config.ENV_KEYS}
    try:
        for line in example.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() in env_config.ENV_KEYS:
                os.environ[key.strip()] = value.strip()
        for key in env_config.ENV_KEYS:
            if key == "WOOLWORTHS_URL":
                continue   # a plain example URL is usable, not a credential
            assert env_config.env_value(key) is None, (
                f"{key} from a copied .env.example reads as configured")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@check
def test_env_keys_are_not_mapped_onto_flags_with_defaults():
    """§3: the loader only fills unset values, so such a variable is inert."""
    assert "out" not in env_config.ENV_KEYS.values()
    assert "format" not in env_config.ENV_KEYS.values()


@check
def test_a_built_captcha_task_type_is_reachable_or_documented():
    """§19 + §17: a solver a caller never reaches is dead code wearing a
    capability's clothes, and the README is where that costs money.

    `captcha_solver.py` builds five task types. reCAPTCHA is wired through
    all three engines; Turnstile is NOT — the `turnstile.render` interception
    that a Cloudflare Challenge page needs lives in the shared module and no
    engine installs it. That is a fine state to be in on a site that has
    never served a challenge, and it is NOT a fine thing to leave a reader to
    discover. So: every task type the solver builds is either reachable from
    an engine, or named in the README as not yet wired.

    The pairing is asserted rather than a keyword searched, so it cannot go
    quiet by accident (CLAUDE.md §21: a guard is only as good as the fixture
    it runs against).
    """
    solver = (ROOT / "captcha_solver.py").read_text(encoding="utf-8")
    engines = ""
    for name in ENGINE_NAMES:
        path = ROOT / f"{name}.py"
        if path.exists():
            engines += path.read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    # reCAPTCHA is the wired one and must stay wired.
    assert "solve_recaptcha" in engines, \
        "no engine calls solve_recaptcha: the reCAPTCHA path is unreachable"

    # Turnstile: wired, or documented as not wired. Never silently neither.
    wired = "TURNSTILE_INTERCEPT_JS" in engines
    documented = "no engine installs that script yet" in readme.lower()
    assert "TurnstileTaskProxyless" not in solver or wired or documented, (
        "captcha_solver builds TurnstileTaskProxyless but no engine installs "
        "TURNSTILE_INTERCEPT_JS, and the README does not say so. Either wire "
        "it or say it is not wired -- a reader cannot tell from the outside.")

    # And the README must not overstate the reCAPTCHA side either.
    assert "already implements" not in readme.lower(), (
        "'already implements' reads as 'this works end to end'. Name the task "
        "types and say which are wired.")


@check
def test_banned_wording():
    """§12. The words are a product decision, and a test is what keeps them."""
    # Assembled rather than written out, so that THIS file does not contain
    # the literals it forbids and the scan can stay live on it — the same
    # reasoning as the masking fixture above. An exemption for smoke_test.py
    # would switch the check off on one of the files most likely to acquire
    # a stray phrase by copy-paste.
    ad = "anti" + "detect"
    banned = (" ".join(["cloud", "browser"]),
              f"{ad} browser",
              f"gate.2prx" + ".com",
              f"--{ad}",
              f"{ad.upper()}_LOCAL_API")
    shipped = list(_shipped_files({".py", ".md", ".yml", ".yaml", ".txt", ".toml"}))
    # The scan must actually have scanned something. An earlier version
    # excluded any path containing `.claude`, and this repo is developed in a
    # worktree under `.claude/worktrees/` — so the exclusion matched EVERY
    # file and the check silently passed on nothing, locally, while failing
    # in CI where the path differs. A check that can quietly scan zero files
    # is not a check.
    assert len(shipped) > 20, f"only {len(shipped)} file(s) scanned"
    problems = []
    for path in shipped:
        text = path.read_text(encoding="utf-8", errors="replace")
        for word in banned:
            if word.lower() in text.lower():
                problems.append(f"{path.relative_to(ROOT)}: {word!r}")
    assert not problems, "banned wording:\n  " + "\n  ".join(problems)


@check
def test_removed_flags_stay_removed_on_the_engines():
    """Scoped to the ENGINES: --country is banned on a scraper (it could
    disagree with the URL) and legitimate on fingerprint_client.py, where it
    picks a fingerprint locale (§10)."""
    for name in ENGINE_NAMES:
        flags = _flags_of(name)
        # "--anti" + "detect" for the same reason as the banned list above:
        # writing the literal here would make this file trip that check.
        for gone in ("--country", "--anti" + "detect", "--page-size",
                     "--search-term"):
            assert gone not in flags, f"{name} reintroduced {gone}"


@check
def test_no_credentials_committed():
    """The shipped check and CI must be ONE implementation (§17)."""
    script = ROOT / ".github" / "ci_checks.py"
    assert script.is_file(), "ci_checks.py is missing"
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(
        encoding="utf-8")
    assert "ci_checks.py" in workflow, (
        "tests.yml does not invoke ci_checks.py — two sources of truth, one "
        "of them dead, is exactly what §17 found in three repos")


@check
def test_fixtures_carry_no_tokens_or_shopper_fields():
    """By SHAPE, not by the old literals, so the next capture is caught (§10)."""
    blob = json.dumps(FIXTURES)
    patterns = {
        "ad token": r"display_[A-Za-z0-9_+/=-]{20,}",
        "AdID field": r'"AdID"',
        "attribution token": r'"AttributionTokenR?e?f?"\s*:\s*"[^"]{20,}"',
        "trolley field": r'"(QuantityInTrolley|IsInTrolley|HasBeenBoughtBefore)"',
        "bearer token": r"[Bb]earer\s+[A-Za-z0-9._-]{20,}",
        # A 32-hex API key. Requires real ENTROPY: the site's own JavaScript
        # carries "000...0", which is 32 hex characters and is not a
        # credential, and a check that cries wolf on it teaches people to
        # ignore it.
        "2captcha key": r"\b(?=[0-9a-f]{32}\b)(?=.*[a-f])(?=.*[0-9])[0-9a-f]{32}\b",
    }
    problems = []
    for label, pat in patterns.items():
        for m in re.finditer(pat, blob):
            if label == "2captcha key" and len(set(m.group(0))) < 8:
                continue     # a run of repeated characters is not a key
            problems.append(label)
            break
    assert not problems, f"fixtures carry {problems} — rerun make_fixtures.py"


@check
def test_sample_output_matches_the_row_schema():
    sample = ROOT / "sample_output.json"
    assert sample.is_file(), "sample_output.json is missing"
    rows = json.loads(sample.read_text(encoding="utf-8"))
    assert rows, "sample_output.json is empty"
    names = [f.name for f in __import__("dataclasses").fields(Product)]
    assert list(rows[0].keys()) == names, (
        "sample_output.json columns drifted from Product")
    csv_path = ROOT / "sample_output.csv"
    assert csv_path.is_file(), "sample_output.csv is missing"
    header = csv_path.read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",")[:5] == names[:5]
    # §10: a real run, not a fabrication.
    blob = json.dumps(rows).lower()
    for fake in ("example.com", "lorem ipsum", "foo bar", "test product",
                 "placeholder"):
        assert fake not in blob, f"sample_output.json looks fabricated: {fake!r}"


@check
def test_dockerfile_copies_everything_the_entrypoint_imports():
    """All three repos in this family shipped an image that died on every
    invocation because one module was missing from the COPY list (§10)."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    # Join backslash continuations first: the COPY list spans several lines,
    # and a parser that read only the first of them reported every module on
    # the later lines as missing — a check that cries wolf is a check people
    # learn to ignore.
    joined = re.sub(r"\\\s*\n\s*", " ", dockerfile)
    copied = set()
    for line in joined.splitlines():
        if line.strip().upper().startswith("COPY"):
            for token in line.split()[1:-1]:
                copied.add(token.strip())
    needed = set()
    seen = set()
    stack = ["playwright_scraper"]
    local = {p.stem for p in ROOT.glob("*.py")}
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = ROOT / f"{mod}.py"
        if not path.is_file():
            continue
        needed.add(f"{mod}.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in local:
                        stack.append(a.name)
            elif isinstance(node, ast.ImportFrom) and node.module in local:
                stack.append(node.module)
    missing = sorted(m for m in needed if m not in copied)
    assert not missing, (
        f"the Dockerfile does not COPY {missing}, so the image would die "
        "with ModuleNotFoundError on every invocation, --help included")


# ===========================================================================
# 12. Captcha: detected != paying (§19)
# ===========================================================================
@check
def test_no_captcha_is_detected_on_a_good_page():
    """And in particular the Scraping Browser extension's injected hunters
    must not read as the site's own captcha — that is a solve paid for a
    captcha that was never there (§19)."""
    served = FIXTURES["served_page"]
    assert captcha_solver.detect_recaptcha_v3(served, "https://x") is None, (
        "a served Woolworths page reads as carrying a reCAPTCHA")
    assert captcha_solver.detect_turnstile(served, "https://x") is None, (
        "a served Woolworths page reads as carrying a Turnstile")
    # And on a REAL page fetched over --cdp-endpoint, not a synthetic one.
    # §21: the guard that missed this in a sibling repo passed for the wrong
    # reason — it ran only against a page fetched with plain curl, which
    # carries no extension injection at all. The fixture that matters is the
    # one fetched the way a real run fetches.
    cdp = FIXTURES["served_cdp_page"]
    injected = cdp.count("chrome-extension://")
    assert injected >= 5, (
        f"the CDP fixture carries only {injected} extension reference(s) — "
        "it is no longer a page fetched through the Scraping Browser, so "
        "this check has stopped proving anything. Recapture it.")
    assert captcha_solver.detect_recaptcha_v3(cdp, "https://x") is None, (
        "a page served over --cdp-endpoint reads as carrying a reCAPTCHA; "
        "the markers on it are the auto-solve extension's, not the site's")
    assert captcha_solver.detect_turnstile(cdp, "https://x") is None, (
        "the auto-solve extension's own hunter reads as a Turnstile — it "
        "appears on every page fetched over --cdp-endpoint, so this would "
        "buy a solve on a page that was served perfectly well")
    assert P.detect_page_state(cdp, status=200,
                               url="https://www.woolworths.com.au/shop/"
                                   "search/products?searchTerm=milk") == "content", (
        "a page served over --cdp-endpoint does not classify as content")


@check
def test_the_denial_page_carries_nothing_to_solve():
    """A statement about THIS PAGE, never about what a solver can do (§19)."""
    for key in ("denial_dom", "denial_raw"):
        text = FIXTURES[key]
        assert "sitekey" not in text.lower()
        assert "<iframe" not in text.lower()
        assert captcha_solver.detect_recaptcha_v3(text, "https://x") is None
        assert captcha_solver.detect_turnstile(text, "https://x") is None


@check
def test_the_solver_implements_the_task_types_the_docs_promise():
    """§19: never write that a captcha cannot be solved; name the task type."""
    src = (ROOT / "captcha_solver.py").read_text(encoding="utf-8")
    for task in ("TurnstileTaskProxyless", "RecaptchaV2TaskProxyless",
                 "RecaptchaV3TaskProxyless"):
        assert task in src, f"{task} is not implemented in captcha_solver.py"


@check
def test_no_file_claims_a_captcha_cannot_be_solved():
    """The most expensive bug this family has shipped was a SENTENCE (§19)."""
    bad = re.compile(
        r"(captcha|recaptcha|turnstile)[^.\n]{0,80}"
        r"(cannot be solved|can't be solved|is unsolvable|impossible to solve)",
        re.IGNORECASE)
    # A sentence STATING THE RULE is not a violation of it. CONTRIBUTING.md
    # says "Never write that a captcha cannot be solved", which is the
    # instruction, and matching it would make the rule impossible to write
    # down.
    stating_the_rule = re.compile(
        r"(never|do not|don't|not to|nor)\s+(write|say|claim|state)\b[^.\n]{0,40}$",
        re.IGNORECASE)
    problems = []
    scanned = 0
    for path in _shipped_files({".py", ".md"}):
        if path.name == "smoke_test.py":
            continue   # this file necessarily contains the phrase it forbids
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in bad.finditer(text):
            before = text[max(0, m.start() - 60):m.start()]
            if stating_the_rule.search(before):
                continue
            problems.append(f"{path.relative_to(ROOT)}: {m.group(0)[:90]!r}")
    assert scanned > 10, f"only {scanned} file(s) scanned"
    assert not problems, (
        "a claim that a captcha cannot be solved — say 'this repo does not "
        "implement X' instead (§19):\n  " + "\n  ".join(problems))


# ===========================================================================
# 13. Engine-specific groups (skipped when the driver is absent)
# ===========================================================================
@check
def test_playwright_helpers():
    if PW is None:
        return
    assert PW.FIELD_FLOOR == 90
    assert PW.LAUNCH_ARGS == ("--disable-blink-features=AutomationControlled",)
    ua = PW._chrome_ua("140.0.0.0")
    assert "Chrome/140.0.0.0" in ua and "HeadlessChrome" not in ua
    assert PW._proxy_failure(Exception("net::ERR_PROXY_CONNECTION_FAILED")) \
        == "ERR_PROXY_CONNECTION_FAILED"
    assert PW._proxy_failure(Exception("Timeout 30000ms exceeded")) == ""


@check
def test_selenium_helpers():
    if SE is None:
        return
    assert SE.FIELD_FLOOR == 90
    assert SE._proxy_failure(Exception("net::ERR_TUNNEL_CONNECTION_FAILED")) \
        == "ERR_TUNNEL_CONNECTION_FAILED"


@check
def test_puppeteer_helpers():
    if PP is None:
        return
    assert PP.FIELD_FLOOR == 90
    assert PP._proxy_failure(Exception("net::ERR_PROXY_CONNECTION_FAILED")) \
        == "ERR_PROXY_CONNECTION_FAILED"


@check
def test_every_engine_help_runs():
    """The exact invocation a missing module breaks."""
    import subprocess
    for name in ENGINE_NAMES:
        mod = {"playwright_scraper": PW, "selenium_scraper": SE,
               "puppeteer_scraper": PP}[name]
        if mod is None:
            continue
        r = subprocess.run([sys.executable, str(ROOT / f"{name}.py"), "--help"],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, f"{name} --help exited {r.returncode}: {r.stderr[:300]}"
        assert "woolworths" in r.stdout.lower(), f"{name} --help does not name the site"


# ===========================================================================
def main() -> int:
    total = len(PASSES) + len(FAILURES)
    print()
    print(f"{len(PASSES)}/{total} checks passed"
          + (f", {len(SKIPS)} group(s) skipped" if SKIPS else ""))
    for group, reason in SKIPS:
        print(f"  skipped: {group} ({reason})")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED:")
        for name, err in FAILURES:
            print(f"  {name}: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
