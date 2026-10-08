"""page_flow.py — what to do with the page Woolworths just gave us.

Woolworths answers a request three ways, and two of them want a different
response, which is why this module exists rather than the same triage being
written three times inside three engines and drifting apart (§1):

    content    the shell is up and the grid's tiles have rendered
    shell      served, built out of Woolworths' own assets, nothing painted
               yet. Wants a WAIT, not a refetch — and on this site it is the
               NORMAL first state, see below
    blocked    Akamai's 403 denial, or an address that is not Woolworths'

There is no `empty` among them, and that is measured: the site's own "we
couldn't find any" copy is in the JS bundle on EVERY page it serves — 4
occurrences on a search with 2,205 results, 4 on a category, 4 on the home
page, 4 on a genuinely empty search. Taken as a marker it made a full
listing report itself empty. Whether a listing holds anything is answered by
the payload's own result count instead; see `product_parser.total_count`.

The policy lives in `STATE_POLICY` as DATA, so an engine cannot quietly
disagree with its twins about whether a page is worth retrying or worth
paying for.

`shell` is the NORMAL first state here
--------------------------------------
Woolworths server-renders no product data at all — see `product_parser`'s
docstring for the measurement. The first response is an 800 KB application
shell whose visible text is 3.7 KB of navigation, and the catalogue arrives
afterwards over the site's own API. So unlike a sibling repo whose site ships
its data in the document, a run here ALWAYS waits, and `shell` classifying as
"retry this" would refetch a page that was served perfectly well.

This is §18's finding — that which of the three missing-content cases applies
can differ between page kinds — arriving as a property of the whole site
rather than of one route.

The fetch is not a navigation
-----------------------------
The engines navigate ONCE, to make Akamai issue the session, and then ask the
site's own API for each page from inside that page. So `advance_page` below
is about a request the driver makes on the engine's behalf, not about
following a link, and the "did the listing end" question is answered from the
ROWS rather than from a next-link. There is no next-link to read: there is no
product markup in the document to put one in.

Everything here is pure or driven through small callables, so each engine
passes its own driver's primitives and keeps its browser plumbing to itself:

    count(selector) -> int              how many elements match
    sleep(ms) -> None                   wait
    fetch_json(kind, params) -> dict    ask the site's own API
    read_tiles() -> List[dict]          the rendered tiles, shadow roots and all

No JavaScript crosses that boundary in either direction (§1): Selenium's
`execute_script` takes a function BODY with an explicit `return` where
Playwright and pyppeteer take `() => expr`, so this module names the
OPERATION and each engine spells it in its own driver's dialect.

`read_tiles` is the one that could not be shared even if we wanted to
-------------------------------------------------------------------
The grid renders into OPEN SHADOW ROOTS on 67 `<wc-product-tile>` elements,
and the three drivers reach those three different ways: Playwright's own CSS
engine pierces them, Selenium needs `element.shadow_root` or a CDP call, and
pyppeteer needs an explicit `.shadowRoot` walk in page script. Naming the
operation is the only thing that keeps the three honest.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from product_parser import (API_CATEGORIES_PATH, CONCURRENCY_REASON,
                            MIN_CARD_MATCHES, PAGE_CAP, PAGE_URL_REASON,
                            SELECTORS, api_request_for, asset_reference_count,
                            category_id_for_slug, category_slug_from_url,
                            detect_block_marker, detect_page_state,
                            is_unauthorised_redirect, iter_category_nodes,
                            organic_count, overlay_dom_prices,
                            search_term_from_url, total_count)

logger = logging.getLogger("page_flow")

# `user:pass@` inside a URL anywhere in a string. An EXCEPTION MESSAGE is a
# log (§8), and a driver error carries the full proxy URL it failed to
# reach. GLOBAL rather than first-match: an error can repeat an endpoint
# several times, and a masker that handles one occurrence prints the
# password for the rest while looking like it works.
_CREDENTIALS_IN_URL_RE = re.compile(r"(\w+://)[^/\s:@]+:[^/\s:@]+@")


def mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced.

    One definition. It was three identical copies, one per engine, and only
    one of the three engines actually CALLED it on the load-failure path —
    so the same dead proxy printed its password from two engines and not
    from the third.
    """
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


# ===========================================================================
# Readiness
# ===========================================================================
READY_SELECTOR = SELECTORS["tile"]

# How long to wait for the grid before giving up and asking the API anyway.
# Asking anyway is deliberate: an empty category renders no tiles anyway, and
# treating "no tiles" as a failure would report a real, correct, empty answer
# as a fault.
CONTENT_TIMEOUT_MS = 25_000
POLL_MS = 500


def ready_selector(mode: str = "") -> str:
    return READY_SELECTOR


def min_matches(mode: str = "", expected: Optional[int] = None) -> int:
    """How many tiles mean "painted". Never 1 (§5)."""
    if expected is not None and expected > 0:
        return max(2, min(MIN_CARD_MATCHES, expected))
    return MIN_CARD_MATCHES


def wait_for_tiles(count: Callable[[str], int], sleep: Callable[[int], None],
                   selector: Optional[str] = None,
                   minimum: Optional[int] = None,
                   timeout_ms: int = CONTENT_TIMEOUT_MS) -> int:
    """Poll until the grid has painted, or the budget runs out. Returns the count.

    POLLS rather than evaluating a wait expression. §18's Tokopedia finding
    was a site whose Content-Security-Policy has no `unsafe-eval`, which made
    Playwright's `wait_for_function` — a STRING handed to the browser to
    evaluate — kill the run with an EvalError on the site's most obvious URL.
    Woolworths' CSP was checked and does allow it (a string `wait_for_function`
    resolved fine on a live search page), so this is not working around a
    measured failure here. It is written this way anyway because the cost is
    nothing and the family has already paid for the lesson once.
    """
    sel = selector or READY_SELECTOR
    need = minimum if minimum is not None else MIN_CARD_MATCHES
    waited = 0
    seen = 0
    while waited < timeout_ms:
        try:
            seen = count(sel)
        except Exception as exc:                      # a driver hiccup, not a verdict
            logger.debug("tile count failed: %s", exc)
            seen = 0
        if seen >= need:
            return seen
        sleep(POLL_MS)
        waited += POLL_MS
    return seen


# ===========================================================================
# Classification
# ===========================================================================
def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "") -> str:
    """Which of the four states this response is in.

    Signature note: `status` is positional-or-keyword and SECOND, and every
    engine calls it the same way. §17's check-worth-stealing #1 exists
    because a sibling repo had `classify(html, status, url)` called as
    `classify(html, url=…)` in two of three engines, and both crashed on
    their first fetch.
    """
    return detect_page_state(html or "", status=status, url=url)


STATE_POLICY: Dict[str, Dict[str, bool]] = {
    # The grid painted. Nothing more to wait for.
    "content":   {"retry": False, "solve": False, "blocked": False},
    # Served, still painting. PARSED rather than retried: by the time an
    # engine asks, the readiness wait has already run, and the API is asked
    # regardless of whether tiles appeared — an empty category legitimately
    # paints none.
    "shell":     {"retry": False, "solve": False, "blocked": False},
    # Akamai's 403.
    #
    # Retried, because it is address-shaped rather than page-shaped and a
    # rotation is what clears it — see `block_advice` for what was measured.
    # NOT solved: Akamai renders no widget on this site's denial. That is a
    # statement about THIS PAGE and not about what any solver can do (§19) —
    # the denial page is 400 bytes of plain HTML with no captcha of any kind
    # in it, so there is nothing for a solver at any price to work on. If
    # Woolworths ever serves an interstitial that carries a widget,
    # `captcha_solver.py` already knows reCAPTCHA v2/v3, enterprise reCAPTCHA
    # and Turnstile, and this flag is the line to change.
    "blocked":   {"retry": True,  "solve": False, "blocked": True},
    # Not one of ours.
    "unknown":   {"retry": True,  "solve": False, "blocked": False},
}


def _policy(state: str) -> Dict[str, bool]:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])


def should_retry(state: str) -> bool:
    """Whether a refetch could plausibly change this answer.

    Consulted by every engine's block loop. False for `content` and `shell`,
    which were both served; True for `blocked` and for `unknown`.
    """
    return _policy(state)["retry"]


def should_solve(state: str) -> bool:
    """Whether to spend money on a solve for this state.

    False everywhere on this site, and that is about THIS PAGE rather than
    about any solver: Akamai's denial here is ~400 bytes of plain HTML with
    no widget of any kind on it, so there is nothing for a solver at any
    price to answer (section 19). Consulted anyway, by every engine, so that
    changing the policy here is all it takes if Woolworths ever serves an
    interstitial that does carry one.
    """
    return _policy(state)["solve"]


def counts_as_blocked(state: str) -> bool:
    return _policy(state)["blocked"]


# ===========================================================================
# Retry budget and advice
# ===========================================================================
# Consulted by all three engines — not a constant that reads like enforcement
# and enforces nothing (§17). `smoke_test.py` asserts each of these has a
# consumer outside this module.
# How many solves ONE PAGE may buy, across every attempt at it.
#
# Zero are bought on this site today: `should_solve` is False for every
# state, because Akamai's denial here carries no widget (see above). The cap
# exists anyway, and it is not decoration — §23 measured a sibling repo
# buying THREE Turnstile solves for one page against a `SOLVES_PER_PAGE = 1`
# that nothing consulted, and §27.4 then measured the same bypass in 33 of
# the 39 family repos that have a solve path at all.
#
# The shape of that bug is the reason this is an OBJECT rather than an
# integer. A bare constant beside a comment reads like enforcement and
# enforces nothing (§17), and a budget consulted at one of two call sites is
# the same defect wearing a number. There is one call site per engine here;
# it sits INSIDE the block-retry loop, so without a cap a page retried four
# times would buy four solves the moment the policy flag changed.
SOLVES_PER_PAGE = 1


class SolveBudget:
    """What one page is allowed to spend, counted where the money goes.

    Charged immediately before the solver is called rather than after it
    returns, because a FAILED solve is still billed — §19 records an
    `ERROR_CAPTCHA_UNSOLVABLE` that cost real money after 87 seconds. A
    budget that only counts successes is a budget that cannot be exceeded
    on paper while the bill says otherwise.

    Deliberately NOT reset on proxy rotation. A fresh exit is a reason to
    re-fetch the page, not a fresh allowance to pay for it: the page is the
    thing being bought, and rotating is how a run reaches the SAME page
    again.
    """

    def __init__(self, limit: int = SOLVES_PER_PAGE):
        self.limit = int(limit)
        self.spent = 0

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.limit

    def charge(self) -> bool:
        """Take one solve out of the budget. False if there is none left."""
        if self.exhausted:
            return False
        self.spent += 1
        return True


BLOCK_RETRIES_WITHOUT_POOL = 3
BLOCK_RETRIES_WITH_POOL = 4
# A rotation is a fresh browser (§8): cookies Akamai issued against exit A
# and replayed from exit B are a stronger signal than either address alone,
# and Akamai's session cookies are exactly what this site issues.
RETRY_NEEDS_FRESH_CONTEXT = True


def block_retries(has_pool: bool, requested: Optional[int] = None) -> int:
    """How many extra attempts a refused page gets.

    `requested` is `--proxy-block-retries`, and it was being ignored: the
    engines called this with `has_pool` alone, so the flag parsed, defaulted
    and changed nothing — section 17's "a policy constant nothing reads",
    wearing a CLI flag. Measured with `--proxy-block-retries 0` and a
    two-exit pool: the run still made every attempt the constant allows.

    The flag counts EXTRA attempts after the first, which is why 0 is a
    meaningful value and is honoured rather than treated as unset. It only
    applies with a pool — without one there is nothing to rotate to, and the
    constant is the re-fetch budget.
    """
    if has_pool and requested is not None:
        return max(0, int(requested))
    return BLOCK_RETRIES_WITH_POOL if has_pool else BLOCK_RETRIES_WITHOUT_POOL


def block_advice(html: Optional[str], headless: bool, has_pool: bool) -> str:
    """What to tell a user whose run came back blocked.

    The headless line is the first thing to say, because on this site it is
    measured and it is free to act on: from one datacentre address, four
    URLs fetched HEADFUL returned HTTP 200 with the full catalogue and the
    same four fetched HEADLESS returned Akamai's 403 denial. That is the
    §19 measurement, run on this site rather than inherited.
    """
    bits: List[str] = []
    marker = detect_block_marker(html or "")
    if marker:
        bits.append(f"Akamai refused the request ({marker!r} on the page).")
    else:
        bits.append("The request was refused.")

    if headless:
        bits.append(
            "Try --headful first: measured on this site from one datacentre "
            "address, 4 of 4 URLs were served headful and 0 of 4 headless. "
            "It costs nothing and is the most likely fix.")
    if not has_pool:
        bits.append(
            "Then an exit: --proxy/--proxy-file, or --cdp-endpoint for the "
            "Scraping Browser API. Note it may not be the address: two "
            "DATACENTRE addresses have been served headful, including a "
            "GitHub runner, so check the browser before buying an exit. An "
            "Australian one is NOT required either way.")
    else:
        bits.append("The pool rotated and was still refused; try another exit country.")
    return " ".join(bits)


# ===========================================================================
# Paging
# ===========================================================================
# Both modes address a page by an integer in the request body, so page N can
# be asked for without reading page N-1 — which is what makes `--concurrency`
# meaningful here (§7).
def paginates_by_url(mode: str = "", url: str = "") -> bool:
    return True


def concurrency_refusal(mode: str = "", url: str = "") -> Optional[str]:
    """Why concurrency must be refused for this target, or None."""
    return CONCURRENCY_REASON


def page_cap_reached(page_num: int) -> bool:
    """Whether `page_num` is past the cap. INCLUSIVE: page 200 of a 200-page
    cap is the last page fetched, not the first one skipped.

    It read `>=`, so `--pages 200` fetched 199 — the help says "cap 200" and
    the code meant 199.
    """
    return page_num > PAGE_CAP


def planned_pages(pages_requested: int) -> List[int]:
    """Pages 2..N that a run may fetch, bounded by the cap.

    ONE plan, built before the sequential/concurrent choice, because the two
    branches disagreed: the sequential loop stopped at the cap and the
    concurrent one queued `range(2, args.pages + 1)` with no bound at all,
    so `--pages 201 --concurrency 2` planned page 201 against a cap of 200.
    """
    last = min(int(pages_requested), PAGE_CAP)
    return list(range(2, last + 1))


# Outcomes of asking for one more page.
ADVANCED = "advanced"          # the page carried organic rows we had not seen
NO_GROWTH = "no_growth"        # nothing new — the listing is done
ADS_ONLY = "ads_only"          # rows came back, and every one was an ad


def advance_page(rows: Sequence[object], seen_skus: set) -> str:
    """Did this page extend the listing, or has it ended?

    NOT "were there rows". Past its last real page Woolworths' API keeps
    answering HTTP 200 with `Success: true` and NOTHING BUT PROMOTED ADS —
    measured on a 16-page category: page 17 returned 1 row, page 18 returned
    8, page 99 returned 8, every one of them sponsored and every one of them
    a repeat of an ad page 1 already carried. A loop that stops when a page
    returns no rows never stops, and a 50-page request would pad the output
    with the same eight ads forty times.

    So the test is organic rows that are NEW. `ADS_ONLY` is reported
    separately from `NO_GROWTH` because it is worth saying in a log which of
    the two happened: the first means the catalogue ended, the second could
    also mean a filter matched nothing.
    """
    organic_new = 0
    organic_total = 0
    for row in rows:
        if getattr(row, "is_sponsored", None):
            continue
        organic_total += 1
        sku = str(getattr(row, "sku", "") or "")
        if sku and sku not in seen_skus:
            organic_new += 1
    if organic_new:
        return ADVANCED
    if rows and organic_total == 0:
        return ADS_ONLY
    return NO_GROWTH



# ===========================================================================
# The page loop itself — ONE implementation, three drivers (§27.5)
# ===========================================================================
# Until 2026-10-08 the loop below lived three times over, once per engine,
# and the module docstring above already described the design this section
# completes: "Everything here is pure or driven through small callables, so
# each engine passes its own driver's primitives". The loop was the one part
# that had not been.
#
# WHY IT CHANGED. Measured on this repo before the move: `_fetch_one_page`
# was 254/213/215 lines in the three engines and 78% textually identical
# between the two closest, and a code-only diff (comments and log prose
# stripped) showed **no behavioural difference at all** — every line that
# differed was driver spelling or a shorter log message in the twins. So the
# triplication was buying nothing and had already cost twice:
#
#   * v0.1.0 shipped `'Page' object has no attribute 'page'` in category
#     mode on the Playwright engine alone, because one of three copies
#     passed `session.page` where the other two passed `session`. Search
#     mode never reaches that line, so a search-only verification missed it
#     and it reached main.
#   * the check written afterwards to catch that immediately found a SECOND
#     divergence of the same shape, in `_read_tiles`.
#
# CLAUDE.md §27.5 says to convert the repos where a divergence has actually
# bitten. This is one, twice.
#
# WHAT AN ENGINE STILL OWNS: launching and tearing down a browser, its own
# argument parser, and the one piece of JavaScript it has to spell in its
# own driver's dialect. Everything between landing on the page and returning
# a `PageOutcome` is here.
#
# NO JAVASCRIPT CROSSES THIS BOUNDARY, in either direction — the rule the
# docstring above states, now load-bearing rather than aspirational.
# Selenium's `execute_script` takes a function BODY with an explicit
# `return` where Playwright and pyppeteer take `() => expr`, so this module
# names the OPERATION (`read_tiles()`, `document_text()`) and each engine
# spells it however its driver demands.

# Coverage floors. Shared so the three engines cannot disagree about what
# "healthy" means — they were a duplicated pair of constants in each engine,
# which is the same defect as a duplicated loop wearing a smaller hat.
#
# The lowest share of rows that must carry both a title and a price before
# the read is suspect. Measured on a 660-row corpus drawn from five search
# terms and three category nodes on 2026-09-16: `DisplayName` was populated
# on 660 of 660 and `Price` on 656 of 660 (99.4%). So the floor sits high;
# 90% leaves room for the handful of unpriced, unavailable products without
# hiding a broken read.
#
# There is deliberately NO brand floor beside it. `Brand` was populated on
# 620 of 660 (93.9%), and the gap is unbranded fresh produce — a loose apple
# has no brand — rather than a parse that missed it. A threshold there would
# fire on a correct read of the fruit-and-veg department.
FIELD_FLOOR = 90

# The lowest share of DOM-checkable rows that must agree with the API's
# price before the two views are treated as describing different pages. Only
# rows that HAVE a rendered tile count toward it, so a page whose grid never
# painted does not trip it — it reports no check at all instead.
DOM_CONFIRM_FLOOR = 90


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards rather than folded into shared
    state as the loop goes: dedupe that mutates a running set inside the
    loop makes the OUTPUT depend on the order pages happened to arrive in.
    Pages are strictly sequential on this site, which is exactly why keeping
    the merge order-independent costs nothing and keeps the family's
    contract.
    """
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    # How many results the site says the whole listing holds
    # (`SearchResultsCount` / `TotalRecordCount`).
    #
    # NOT a per-page counter and never used as a gap. It is also NOT STABLE
    # across pages, which is measured rather than assumed: a search for
    # `saffron` reported 69 on page 1 and 0 on page 50. It goes in the
    # sidecar beside what the run actually read, which is what makes the
    # difference between the two visible rather than assumed.
    stated_total: Optional[int] = None
    # None, always, on this site: Woolworths numbers no rank on a listing,
    # so there is no arithmetic gap to compute, and an unknown gap must not
    # read the same as a gap of zero (§8).
    gap: Optional[int] = None
    # What the DOM cross-check found: how many rows had a rendered tile to
    # compare against and how many agreed. None where no tile was readable.
    dom_confirm: Optional[dict] = None
    # API responses during this page's fetch that were not 200. A page whose
    # document answered 200 while its API call was refused is a PARTIAL run,
    # not a short listing.
    api_errors: int = 0
    # Whether the app bounced this browser to /unauthorisederror. Recorded
    # rather than fatal: the rows are still the API's and still correct, but
    # the grid is gone, so this is why price_source is 'api' everywhere.
    unauthorised: bool = False
    # A page whose PARSER produced nothing from a payload the site said held
    # results. Separate from `load_failed` on purpose: the content arrived,
    # so this is ours rather than the network's, and the log says so — but
    # it must not count as a completed page.
    #
    # This was an audit's P1 and it is the family defect §26 records binance
    # hitting for real. The state was being SET and then discarded: `ok`
    # consulted only the two fields below, so a parser failure came back as
    # a page that succeeded with zero rows, which `advance_page` then read
    # as the end of the listing.
    parse_failed: bool = False

    @property
    def ok(self) -> bool:
        return (not self.load_failed and not self.parse_failed
                and self.blocked_by is None)


# ---------------------------------------------------------------------------
# The operations an engine must supply
# ---------------------------------------------------------------------------
# Not an ABC and not type-checked at runtime: `smoke_test.py` derives the
# required set from THIS MODULE'S OWN AST — every `ops.<name>` the loop
# reaches for — and asserts each engine provides all of them. A hand-written
# list would drift the day the loop uses a new one, which is §26's check #1
# and the reason it is worth copying with the pattern.
#
#   goto(url, timeout_ms) -> Optional[int]   navigate; the HTTP status, or
#                                            None where the driver has none
#                                            (Selenium). May raise.
#   document_text() -> Optional[str]         the settled document
#   classify(html, status) -> str            one of STATE_POLICY's keys
#   current_url() -> Optional[str]           where the driver actually is
#   screenshot(path) -> None                 best-effort; may raise
#   count(selector) -> int                   how many elements match
#   wait_ms(ms) -> None                      the driver's own sleep
#   read_tiles() -> List[dict]               rendered tiles, shadow roots
#                                            and all. Never raises.
#   rows_from_payload(payload, args, page_num, data_source) -> List
#   set_landed(url) -> None                  remember where we are
#   fetch_api(path, body) -> (status, payload, text)
#   api_error_count() -> int                 non-200 API responses so far
#   handle_captcha(args, allow_solve, budget) -> bool
#   relaunch() -> None                       a fresh browser on a fresh exit
#   clear_landing() -> None                  forget that we are landed
#   is_landed(url) -> bool                   already on this url?
#   is_live() -> bool                        is there a usable page at all?
#   proxy_failure(exc) -> Optional[str]      a dead exit, named, or None
#   driver_errors -> tuple                   the exception types to catch
#   launch_arg_hint -> str                   the flag named in one warning


def land(ops, args, outcome: PageOutcome):
    """Make sure the driver is ON the listing page. (html, status, state)

    Navigates only when it has to. Every page of a listing is fetched from
    inside ONE loaded page — the navigation exists to make Akamai issue a
    session, not to reach page N — so this is a no-op after the first call
    unless a rotation replaced the browser underneath us.

    Selenium reports no HTTP status, so `status` is None there and
    `detect_page_state` falls through to the markers and the asset-host
    signal. That is the one place that engine has strictly less information
    than its twins, and it is why the asset-host check exists (§8).
    """
    if ops.is_landed(args.url) and ops.is_live():
        # CLASSIFIED, not assumed to be content. Two of the three engines
        # used to return a hardcoded "content" here on the grounds that we
        # had already been served this page once; the third re-classified.
        # The third is right — a session can be refused between page 1 and
        # page 2, and the engines that assumed would have carried on asking
        # the API from inside a denial page.
        try:
            html = ops.document_text()
        except ops.driver_errors:
            html = None
        if html:
            return html, None, ops.classify(html, None)
        ops.clear_landing()

    status = None
    for attempt in range(1, args.retries + 1):
        try:
            status = ops.goto(args.url, 60000)
            break
        except ops.driver_errors as e:
            # A dead or misconfigured proxy raises the driver's generic
            # error (net::ERR_PROXY_CONNECTION_FAILED), not a timeout —
            # catching only the latter lets it escape as a traceback, which
            # is the likeliest failure the first time anyone points
            # --proxy-file at a real list (§8). They want opposite
            # responses: a timeout deserves another try at the same exit, a
            # dead proxy a different one.
            reason = ops.proxy_failure(e)
            if reason:
                logger.error("Exit failed: %s", reason)
                outcome.load_failed = True
                return None, None, "blocked"
            if attempt < args.retries:
                pause = args.retry_delay * (2 ** (attempt - 1))
                # MASKED: the driver puts the proxy URL it could not reach
                # into the exception text, credentials and all (§8).
                logger.warning("Could not load %s (attempt %d/%d: %s) — "
                               "retrying in %.1fs.", args.url, attempt,
                               args.retries,
                               mask_credentials(str(e))[:140], pause)
                time.sleep(pause)
            else:
                outcome.load_failed = True
                return None, None, "blocked"

    html = ops.document_text() or ""
    state = ops.classify(html, status)
    if state != "blocked":
        ops.set_landed(args.url)
    return html, status, state


def resolve_target(ops, args, outcome: PageOutcome):
    """What to ask the API for. (ok, term, category_id, slug)

    A category id is OPAQUE and has to be looked up in the site's own tree:
    `bakery` is `1_DEB537E`, `fruit-veg` is `1-E5BEE36E`. Sending the slug
    instead returns HTTP 200 with zero products and `Success: true`, which
    is indistinguishable from a real empty category — so an unresolved slug
    is refused here rather than turned into a run that reports success on
    nothing.

    Cached on the ops object: the tree is ~2,700 nodes and does not change
    between pages of one run.
    """
    if args.mode == "search":
        term = search_term_from_url(args.url)
        if not term:
            logger.error("No searchTerm parameter in %s. A search URL carries "
                         "the term; there is deliberately no flag for it, "
                         "because a flag could only ever disagree with the "
                         "URL it was given.", args.url)
            outcome.load_failed = True
            return False, None, None, None
        return True, term, None, None

    slug = category_slug_from_url(args.url)
    cached = getattr(ops, "category_ids", None)
    if cached is None:
        cached = ops.category_ids = {}
    if slug in cached:
        return True, None, cached[slug], slug

    status, tree, _ = ops.fetch_api(API_CATEGORIES_PATH, None)
    if status != 200 or not tree:
        logger.error("Could not read the category tree (%s %s). Without it a "
                     "slug cannot be turned into the opaque id the browse API "
                     "needs.", API_CATEGORIES_PATH, status)
        outcome.load_failed = True
        return False, None, None, None

    node_id = category_id_for_slug(tree, slug or "")
    if not node_id:
        logger.error(
            "%r is not a category node on this site. It is not a typo in the "
            "code: the slug was looked up in Woolworths' own tree "
            "(%s, %d nodes) and is not in it. Check the URL in a browser.",
            slug, API_CATEGORIES_PATH,
            sum(1 for _ in iter_category_nodes(tree)))
        outcome.load_failed = True
        return False, None, None, None

    cached[slug] = node_id
    logger.info("Category %r resolves to node id %s.", slug, node_id)
    return True, None, node_id, slug


def fetch_api_with_retries(ops, args, path, body, page_num: int):
    """`ops.fetch_api`, with the user's retry budget spent on it.

    Bounded and backed off, like the navigation retry beside it. The fault
    this absorbs is real and was measured: a live run hit
    `TypeError: Failed to fetch` — the request rejected inside Akamai's own
    hooked `window.fetch` — on page 1, and the same command a minute later
    returned 153 rows. Retrying the navigation but not the API request left
    the only call that actually fetches data unprotected.

    Returns (status, payload, text). A refusal that survives the budget is
    reported as PARTIAL by the caller, never as the end of the listing.
    """
    status = payload = None
    text = ""
    for attempt in range(1, max(1, args.retries) + 1):
        try:
            status, payload, text = ops.fetch_api(path, body)
        except ops.driver_errors as e:
            status, payload, text = None, None, ""
            logger.debug("API request raised: %s", e)
        if status == 200 and payload is not None:
            return status, payload, text
        if attempt < max(1, args.retries):
            pause = args.retry_delay * (2 ** (attempt - 1))
            logger.warning(
                "The API request for page %d did not return usable JSON "
                "(HTTP %s, %d bytes) — retrying in %.1fs (attempt %d/%d).",
                page_num, status, len(text or ""), pause, attempt,
                args.retries)
            time.sleep(pause)
    return status, payload, text


def confirm_with_dom(ops, rows, page_num: int) -> Optional[dict]:
    """Confirm the API's prices against the rendered tiles.

    A CONFIRMATION, never a correction: where the two agree the row's
    `price_source` becomes `api+dom`, and where they disagree the row is
    left exactly as the API gave it and a warning names the sku (§4).

    Only meaningful for the page the browser is actually LOOKING at, which
    is page 1 — pages 2..N are fetched over the API without navigating, so
    the tiles on screen still belong to page 1 and matching them against
    page 2's rows would confirm nothing and could mis-attribute a price. The
    stockcode key makes a wrong match impossible rather than unlikely, but
    asking the question at all on the wrong page is noise.
    """
    tiles = ops.read_tiles()
    if not tiles:
        logger.info("No rendered tiles were readable on page %d; rows keep "
                    "price_source='api'.", page_num)
        return None
    confirmed, checked = overlay_dom_prices(rows, tiles)
    share = (100.0 * confirmed / checked) if checked else 0.0
    logger.info("DOM price confirmation on page %d: %d of %d row(s) checked "
                "against %d rendered tile(s) agreed (%.0f%%).",
                page_num, confirmed, checked, len(tiles), share)
    if checked and share < DOM_CONFIRM_FLOOR:
        logger.warning(
            "Only %.0f%% of the rows checked against a rendered tile agreed "
            "on price, against a measured floor of %d%%. That is the two "
            "views of the same page disagreeing, which usually means the "
            "grid re-rendered under the read. The rows are the API's and "
            "are unchanged.", share, DOM_CONFIRM_FLOOR)
    return {"tiles": len(tiles), "checked": checked, "confirmed": confirmed}


def fetch_one_page(ops, args, pool, page_num: int,
                   url: Optional[str] = None) -> PageOutcome:
    """Fetch one page of the listing and parse it. ONE implementation.

    `url` is accepted for the family's signature and is the LISTING's
    address, the same for every page: on this site a page is a number in a
    request body, not an address. Taking it keeps the concurrent fetcher's
    call shape identical to its siblings'.

    Returns a PageOutcome and never raises for an EXPECTED failure — a
    timeout, a refusal, a dead exit are all recorded on the outcome instead.

    Every driver touch goes through `ops`, never a captured handle: a
    rotation replaces the browser, context and page together, and a stale
    handle is exactly the bug each engine's session object exists to
    prevent.
    """
    outcome = PageOutcome(page_num=page_num, url=url or args.url)
    has_pool = bool(pool and len(pool) > 1)
    block_retries_left = block_retries(
        has_pool, getattr(args, "proxy_block_retries", None))

    html = state = None
    # Per PAGE, not per attempt: the whole point is that retrying the same
    # page does not buy a second solve for it.
    solve_budget = SolveBudget()

    for block_attempt in range(block_retries_left + 1):
        outcome.load_failed = False
        html, status, state = land(ops, args, outcome)

        # The POLICY decides whether another fetch could change this answer,
        # rather than each engine deciding for itself (§1). False for
        # content, shell and empty; True for blocked and unknown.
        if not should_retry(state) and not outcome.load_failed:
            break

        if block_attempt < block_retries_left:
            # A rotation is a FRESH BROWSER (§8). Cookies Akamai issued
            # against exit A and replayed from exit B are a stronger signal
            # than either address alone, and Akamai session cookies are
            # exactly what this site issues.
            if pool and RETRY_NEEDS_FRESH_CONTEXT:
                try:
                    # `advance()`, not `rotate()`: ProxyPool stores the
                    # rotation MODE as `self.rotate`, so `pool.rotate()` is
                    # a string and calling it raised TypeError — a crash
                    # (exit 1) on every rotation, in all three engines, on
                    # the one path a pool exists for.
                    pool.advance("refused by the site")
                except Exception as e:  # noqa: BLE001 — ProxyError & friends
                    logger.warning("Could not rotate the exit: %s", e)
            ops.clear_landing()
            logger.warning("Refused (attempt %d of %d) — relaunching%s.",
                           block_attempt + 1, block_retries_left + 1,
                           " on the next exit" if has_pool else "")
            try:
                ops.relaunch()
            except Exception as e:  # noqa: BLE001
                logger.warning("Relaunch failed: %s", e)
            time.sleep(args.retry_delay)

    # Detection runs on every page, whatever the state — a challenge that
    # nobody recognises becomes an empty category months later (§8). Whether
    # a SOLVE may be bought is this module's call, and the budget is this
    # page's, so a retried page cannot buy a second one.
    try:
        if args.solve_captcha != "never" and ops.is_live():
            if ops.handle_captcha(args, allow_solve=should_solve(state),
                                  budget=solve_budget):
                html = ops.document_text() or html
                state = ops.classify(html, None)
    except ops.driver_errors as e:
        logger.debug("captcha check skipped: %s", e)

    outcome.state = state

    if outcome.load_failed and state != "blocked":
        outcome.final_url = ops.current_url() or args.url
        return outcome

    if counts_as_blocked(state):
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        try:
            ops.screenshot(f"{args.out}_page{page_num}_debug.png")
        except Exception as e:  # noqa: BLE001 — a screenshot is diagnostic
            logger.warning("Could not capture screenshot: %s", e)
        logger.error(
            "The site did not serve this request — %d bytes, %d reference(s) "
            "to the site's own asset host, saved to %s. This is exit 3, "
            "distinct from a genuinely empty result (exit 4).%s",
            len(html or ""), asset_reference_count(html or ""), debug_html,
            (f" Tried {block_retries_left + 1} exit(s)." if has_pool
             else f" Re-fetched {block_retries_left + 1} time(s)."))
        logger.error("%s", block_advice(
            html, headless=bool(getattr(args, "headless", False)),
            has_pool=has_pool))
        outcome.blocked_by = (detect_block_marker(html or "")
                              or ("no-response" if not html else "akamai"))
        outcome.final_url = ops.current_url() or args.url
        return outcome

    # Wait for the grid. BOUNDED, and not fatal if it never paints: an empty
    # category renders no tiles, and treating that as a failure would report
    # a correct answer as a fault.
    if page_num == 1:
        found = wait_for_tiles(ops.count, ops.wait_ms,
                               ready_selector(args.mode),
                               min_matches(args.mode))
        logger.info("%d tile(s) had painted when the API was asked.", found)
        # Said LOUDLY, because nothing else in the run will notice: the
        # document answered 200, the API is about to answer 200, and the
        # rows will be correct. Only the address gives it away.
        if is_unauthorised_redirect(ops.current_url()):
            outcome.unauthorised = True
            logger.warning(
                "The app navigated itself to %s — it has decided this "
                "browser is automated, and the product grid will not render. "
                "The rows below still come from the site's own API and are "
                "correct, but the DOM price cross-check is impossible and "
                "price_source stays 'api' on every row. This engine passes "
                "%s to prevent it, so seeing this means either the flag did "
                "not take effect or the site now looks at something else.",
                ops.current_url(), ops.launch_arg_hint)

    ok, term, category_id, slug = resolve_target(ops, args, outcome)
    if not ok:
        outcome.final_url = ops.current_url() or args.url
        return outcome

    path, body, data_source = api_request_for(
        args.mode, term=term, category_id=category_id, slug=slug,
        page=page_num, page_size=args.page_size)

    errors_before = ops.api_error_count()
    status, payload, text = fetch_api_with_retries(
        ops, args, path, body, page_num)

    if status != 200 or payload is None:
        # An API refusal is NOT the end of the listing, and must not be
        # reported as one: the document answers 200 while the API behind it
        # is throttled, so a run that collapsed the two would claim a
        # complete run holding page 1 (§7).
        logger.error(
            "POST %s for page %d answered HTTP %s with %d byte(s) that %s "
            "JSON. This is not the end of the listing — the run is reported "
            "as PARTIAL (exit 6) rather than complete. Raise --delay (it is "
            "%.1fs now), or spread the load with --proxy-file.",
            path, page_num, status, len(text or ""),
            "are not" if payload is None else "is", args.delay)
        outcome.load_failed = True
        outcome.state = "api_error"
        outcome.final_url = ops.current_url()
        return outcome

    if args.dump_html:
        # Dumping on success, not only on failure: a run can return the
        # right NUMBER of rows with a field silently unpopulated, and then
        # the only way to tell a parsing bug from a too-early snapshot is
        # the exact bytes. On this site the bytes that matter are the API's,
        # not the document's, so BOTH are written.
        base = (args.dump_html if args.pages == 1
                else f"{args.dump_html}.page{page_num}")
        with open(base, "w", encoding="utf-8") as f:
            f.write(html or "")
        with open(f"{base}.api.json", "w", encoding="utf-8") as f:
            f.write(text or "")
        logger.info("Saved the document to %s (%d bytes) and the payload the "
                    "parser actually reads to %s.api.json (%d bytes).",
                    base, len(html or ""), base, len(text or ""))

    products = ops.rows_from_payload(payload, args, page_num, data_source)
    stated = total_count(payload)
    organic = organic_count(products)
    logger.info("Parsed %d row(s) from page %d — %d organic, %d promoted.%s",
                len(products), page_num, organic, len(products) - organic,
                f" The site states {stated} result(s) for this listing."
                if stated is not None else "")

    outcome.stated_total = stated
    outcome.api_errors = ops.api_error_count() - errors_before

    if products and page_num == 1:
        outcome.dom_confirm = confirm_with_dom(ops, products, page_num)

    if products:
        # Counted over the rows that CAN carry a price. Woolworths publishes
        # none for a product it is not selling: on one fruit-veg page 6 of
        # 73 rows had no price and every one of the 6 was
        # `IsAvailable: false`, while 67 of the 67 available rows had one.
        # Counting those 6 against the floor printed a warning about a
        # completely correct read, and a warning that fires when nothing is
        # wrong teaches people to ignore warnings.
        sellable = [p for p in products if p.is_available is not False]
        with_title = sum(1 for p in products if p.title)
        with_price = sum(1 for p in sellable if p.price is not None)
        title_share = 100.0 * with_title / len(products)
        price_share = (100.0 * with_price / len(sellable)) if sellable else 100.0
        share = min(title_share, price_share)
        logger.info("Coverage on page %d: title %d/%d, price %d/%d of the "
                    "sellable rows (%.0f%% at worst); the measured floor is "
                    "%d%%.%s", page_num, with_title, len(products),
                    with_price, len(sellable), share, FIELD_FLOOR,
                    f" {len(products) - len(sellable)} row(s) are "
                    f"unavailable and carry no price by design."
                    if len(sellable) != len(products) else "")
        if share < FIELD_FLOOR:
            logger.warning(
                "Only %.0f%% of page %d carries both a title and a price, "
                "against a measured floor of %d%%. On a 660-row corpus the "
                "API carried a title on 100%% and a price on 99.4%%, so this "
                "is the read breaking rather than the catalogue being "
                "unusual. Re-run with --dump-html and look at the "
                ".api.json.", share, page_num, FIELD_FLOOR)
    elif stated == 0:
        # A genuinely empty listing, and the site says so itself. NOT a
        # parser problem, and saying so matters: the two want different
        # readers doing different things (§20). The run still reports exit 4
        # — the catalogue question was answered and the answer was nothing —
        # but nothing is dumped and nobody is sent to debug a read that
        # worked.
        logger.info(
            "The site reports 0 results for this listing, and 0 rows were "
            "parsed. That is a correct, empty answer rather than a failed "
            "read — exit 4.")
        outcome.state = "empty"
    else:
        # The site said it HAS results and the parser produced none. That is
        # ours, and it is the case §20 asks to be named rather than reported
        # as "0 products", which sends the reader to check the URL instead
        # of the payload.
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        with open(f"{debug_html}.api.json", "w", encoding="utf-8") as f:
            f.write(text or "")
        outcome.state = "parse_failed"
        outcome.parse_failed = True
        logger.error(
            "The site states %s result(s) for this listing and the parser "
            "produced NONE. That is a parser failure, not an empty "
            "category. Saved the document to %s and the payload to "
            "%s.api.json — the likeliest cause is that the group wrapper "
            "shape changed, so check whether `Products`/`Bundles` still "
            "holds one level of wrapper objects.",
            stated, debug_html, debug_html)

    outcome.products = products
    outcome.final_url = ops.current_url()
    return outcome
