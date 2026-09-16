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
from typing import Callable, Dict, List, Optional, Sequence

from product_parser import (CONCURRENCY_REASON, MIN_CARD_MATCHES, PAGE_CAP,
                            PAGE_URL_REASON, SELECTORS, asset_reference_count,
                            detect_block_marker, detect_page_state,
                            organic_count)

logger = logging.getLogger("page_flow")


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
BLOCK_RETRIES_WITHOUT_POOL = 3
BLOCK_RETRIES_WITH_POOL = 4
# A rotation is a fresh browser (§8): cookies Akamai issued against exit A
# and replayed from exit B are a stronger signal than either address alone,
# and Akamai's session cookies are exactly what this site issues.
RETRY_NEEDS_FRESH_CONTEXT = True


def block_retries(has_pool: bool) -> int:
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
            "Then a residential exit: --proxy/--proxy-file, or "
            "--cdp-endpoint for the Scraping Browser API. An Australian exit "
            "is NOT required — a US exit was served this site normally.")
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
    return page_num >= PAGE_CAP


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

