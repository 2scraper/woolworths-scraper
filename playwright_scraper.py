#!/usr/bin/env python3
"""woolworths-scraper — Playwright edition (primary engine)

Scrapes Woolworths Online products out of one of two views:

    --mode search    (default)  /shop/search/products?searchTerm={term}
    --mode category             /shop/browse/{slug}[/{child}[/{grandchild}]]

Both yield the same row, because both are ways of selecting the same leaf;
see `output_writer.Product`. The mode is inferred from the URL, so passing it
is only ever a way to be explicit or to be told you are wrong.

There is deliberately no `--country` flag. Woolworths Online is one site in
one country: `woolworths.co.nz` is Woolworths New Zealand (a different
company on a different platform) and `woolworths.co.za` is Woolworths
Holdings in South Africa (an unrelated retailer sharing the name). A flag
could only ever disagree with the URL it was given, and this repo refuses
both hosts by name and with the reason (§5).

WHAT IS DIFFERENT ABOUT THIS SITE
---------------------------------
* **Run it HEADFUL. It is the difference between data and a 403.** Measured
  2026-09-16 from one datacentre address, four URLs each way, interleaved:
  headful was served HTTP 200 and the full catalogue **4 of 4**; headless got
  Akamai's 403 "Access Denied" page **4 of 4**. So this engine defaults to
  headful, and `--headless` is available for a reader who has an address
  where it works. This is §19's "measure it once per site; it is four runs"
  run on this site rather than inherited.

* **There is no product data in the HTML. None.** A served category page is
  872 KB whose visible text is 3,735 characters of navigation chrome; the
  word "Banana" appears zero times on `/shop/browse/fruit-veg`. The catalogue
  is behind two doors at once — it arrives as JSON over the site's own API,
  and it renders into OPEN SHADOW ROOTS on `<wc-product-tile>` elements that
  `page.content()` does not serialise.

  So this engine NAVIGATES ONCE, to make Akamai issue a session, and then
  asks the site's own API for each page from inside that page. Pages after
  the first are not navigations, and `--delay` spaces the API calls rather
  than page loads.

* **A category id is opaque and has to be looked up.** `/shop/browse/bakery`
  is `1_DEB537E` and `/shop/browse/fruit-veg` is `1-E5BEE36E` — the two do
  not even share a separator. The engine resolves the slug against the
  site's own tree (`/apis/ui/PiesCategoriesWithSpecials`) before asking for
  page 1, and refuses the URL with the reason if the slug is not in it.

* **A listing never runs out of ADS, only of products.** Past its last real
  page the API keeps answering HTTP 200 with `Success: true` and nothing but
  promoted rows — page 17 of a 16-page category returned 1 row, page 18
  returned 8, page 99 returned 8, all sponsored, all repeats of ads page 1
  already carried. So the loop stops on new ORGANIC rows, never on "the page
  was empty", and `is_sponsored` is a column so a consumer can filter.

* **`akamai` is on every page the site SERVES and on neither page it
  refuses** — 1 occurrence on each of six good captures, 0 on both denials.
  It is not a block marker here despite Woolworths being fronted by Akamai,
  which is §18's rule catching a marker that looks obviously right.

USAGE

    pip install -r requirements.txt -r requirements-playwright.txt
    playwright install chromium

    python3 playwright_scraper.py \\
        --url "https://www.woolworths.com.au/shop/search/products?searchTerm=milk" \\
        --pages 3 --format json

    python3 playwright_scraper.py \\
        --url "https://www.woolworths.com.au/shop/browse/bakery" \\
        --pages 3 --out bakery
"""

import argparse
import json
import logging
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS)
from product_parser import (API_CATEGORIES_PATH, API_CATEGORY_PATH,
                            API_SEARCH_PATH, BASE_URL, CANONICAL_HOST,
                            PAGE_CAP, PAGE_SIZE, PAGE_URL_REASON,
                            SELECTORS, api_request_for,
                            asset_reference_count, category_id_for_slug,
                            category_slug_from_url, detect_block_marker,
                            detect_page_state, is_woolworths_host,
                            is_unauthorised_redirect,
                            iter_category_nodes, mode_for_url, organic_count,
                            overlay_dom_prices, products_from_payload,
                            search_term_from_url, source_of, total_count,
                            unsupported_reason)
from output_writer import dedupe_by_key, finish_run, EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
import env_config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


# No browser channel is forced here, and that is a measurement rather than an
# omission. A sibling site reads the CLIENT before the address and refuses a
# bundled Chromium outright; Woolworths does not. Playwright's own bundled
# Chromium was served HTTP 200 and the full catalogue on every HEADFUL fetch
# measured — 4 of 4 URLs on 2026-09-16, from a datacentre exit. What this
# site refuses is headless, not the build: the same bundled Chromium headless
# was refused 4 of 4 on the same address in the same minute. Named here
# rather than at the call site so the smoke suite can assert the three
# engines agree on it; `--browser-channel chrome` is still available.
DEFAULT_BROWSER_CHANNEL = None

# The ONE launch flag this site actually requires, and the measurement that
# put it here — because without it the run still "works", which is the worst
# shape of failure this family knows (§16).
#
# Woolworths' page script reads `navigator.webdriver`, and Playwright and
# Selenium both set it true by default. When it is true the SPA navigates
# ITSELF to `https://www.woolworths.com.au/unauthorisederror` a second or so
# after load: the grid never renders and the browser ends up on an error
# page.
#
# What makes it worth a paragraph is what does NOT change:
#
#     the document still answers HTTP 200      (the edge served it)
#     the API still answers 200 with products  (the session cookies are fine)
#
# So a run without this flag returns the RIGHT ROWS, reports success, and
# silently loses the DOM price cross-check while sitting on an error URL.
# Nothing raises and no count looks wrong. That is the defect class this
# family keeps rediscovering: a tool doing less than it says while reporting
# success.
#
# Measured 2026-09-16, three runs each way, interleaved, one address:
#
#     without the flag   navigator.webdriver true,  0 tiles, 3/3 redirected
#                        to /unauthorisederror
#     with the flag      navigator.webdriver false, 36-39 tiles, 3/3 stayed
#                        on the search page
#
# Named here rather than at the call site so the smoke suite can assert all
# three engines pass their own spelling of it (§6: the three must agree).
LAUNCH_ARGS = ("--disable-blink-features=AutomationControlled",)

# How long to wait for a remote browser to accept the CDP connection.
#
# 150s, not the 30s this family shipped. What is MEASURED is narrow and
# arithmetic: against a live Scraping Browser endpoint the WebSocket upgrade
# hung for **121 seconds** before the SERVER hung up, so a 30s client timeout
# gives up while the server is still working. Sitting above the server's own
# give-up point means the client is never the one that walks away first.
#
# What is NOT established, and was claimed here for one commit before the
# evidence contradicted it: that giving up early is what leaves a profile
# stuck at `profile_locked`. Two profiles were observed locked and not
# clearing (one for over forty minutes, one after four minutes of complete
# silence), and the second locked INSTANTLY on its first WebSocket attempt —
# with no timed-out connect anywhere in its history. So the lock has some
# other cause, and this timeout is not a fix for it. Raising it is still
# right; expecting it to unwedge anything is not.
CDP_CONNECT_TIMEOUT_MS = 150_000


def _chrome_ua(chromium_version: str) -> str:
    """Build a desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded version number: that drifts the moment a newer Chrome
    ships, and a UA claiming an older Chrome than what the JS engine, WebGL
    strings and TLS ClientHello all report is itself a mismatch a
    fingerprinter can key on.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards rather than folded into shared
    state as the loop goes: dedupe that mutates a running set inside the loop
    makes the OUTPUT depend on the order pages happened to arrive in. Pages
    are strictly sequential on this site, which is exactly why keeping the
    merge order-independent costs nothing and keeps the family's contract.
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
    # None, always, on this site: Woolworths numbers no rank on a listing, so
    # there is no arithmetic gap to compute, and an unknown gap must not read
    # the same as a gap of zero (§8).
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

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


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

# The lowest share of DOM-checkable rows that must agree with the API's price
# before the two views are treated as describing different pages. Only rows
# that HAVE a rendered tile count toward it, so a page whose grid never
# painted does not trip it — it reports no check at all instead.
DOM_CONFIRM_FLOOR = 90



# ---------------------------------------------------------------------------
# page_flow, bound to Playwright
# ---------------------------------------------------------------------------
# Every decision about WHAT to do with a page — how long to wait, when to
# scroll, when a page has turned over — lives in page_flow.py so all three
# engines make it identically. What lives here is only HOW to ask this
# particular driver.
#
# The primitives are NAMED OPERATIONS rather than JavaScript (§1). Selenium's
# execute_script takes a function BODY with an explicit `return` while
# Playwright and pyppeteer take `() => expr`, so a shared module handing JS
# across this boundary would quietly acquire one driver's dialect.
def _count(page, selector: str) -> int:
    """How many elements match, or 0 if the page moved under us.

    GUARDED, like its twins in the other two engines. A readiness poll runs
    while the page is still settling, and a navigation under it — a redirect,
    or Akamai deciding mid-flight — makes Playwright raise `Execution context
    was destroyed, most likely because of a navigation`. Unguarded that is
    exit 1, a CRASH, where the correct answer is "blocked".

    0 is the safe reading rather than a lie: every caller treats it as "no
    tiles seen this poll", which makes a readiness wait keep waiting — which
    is what actually happened. The alternative, letting it propagate, turns a
    routine mid-poll navigation into a traceback.
    """
    try:
        return len(page.query_selector_all(selector))
    except (PWError, PWTimeout) as e:
        logger.debug("count(%s) failed: %s", selector, e)
        return 0


def _fetch_api_raw(page, path: str, body=None, timeout_ms: int = 45_000):
    """Ask Woolworths' own API from inside the page. (status, payload, text)

    From INSIDE the page, not with `requests`, and that is the whole design:
    the browser has just been issued an Akamai session, and a `fetch` on the
    same origin carries its cookies and its TLS fingerprint for free. A
    second HTTP client would have neither and would be refused.

    Handed to `page.evaluate` as a real FUNCTION OBJECT, which goes through
    `Runtime.callFunctionOn`. Not as a string: a string is evaluated, and a
    site whose CSP lacks `unsafe-eval` refuses that outright — it took a
    sibling repo's run down with EvalError and exit 1 (§18). Woolworths'
    CSP was checked and does permit string eval, so this is not working
    around a measured failure here; it costs nothing and the family has
    already paid for the lesson once.

    Returns the raw text alongside the parsed payload so a response that is
    not JSON (an interstitial, an HTML error) can be classified rather than
    raising a decode error on a line a long way from the cause.
    """
    js = """async ([p, b]) => {
        const opt = (b === null)
            ? {headers: {'Accept': 'application/json'}}
            : {method: 'POST',
               headers: {'Content-Type': 'application/json',
                         'Accept': 'application/json'},
               body: JSON.stringify(b)};
        const r = await fetch(p, opt);
        const t = await r.text();
        return {status: r.status, text: t};
    }"""
    page.set_default_timeout(timeout_ms)
    res = page.evaluate(js, [path, body])
    status = res.get("status")
    text = res.get("text") or ""
    payload = None
    if text:
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
    return status, payload, text


def _read_tiles(session) -> List[dict]:
    """The rendered tiles, read THROUGH their open shadow roots.

    The grid is `<wc-product-tile>` custom elements whose content lives in
    shadow roots. `page.content()` does not serialise them, which is why this
    repo has no HTML fallback path at all and why this read has to happen in
    the browser.

    Each tile yields `{href, price_text, cup_text}` — deliberately the raw
    strings, with every judgement about what they MEAN left to
    `product_parser.overlay_dom_prices`. The engine's job is to reach the
    node; the parser's job is to read it.

    A tile with no product link is skipped rather than guessed at: 25 of the
    67 tiles on one category page belong to a "you might also like" carousel,
    and the stockcode in each tile's OWN link is what keeps a carousel tile
    from ever being attributed to a grid row (§4).
    """
    js = """() => {
        const out = [];
        for (const t of document.querySelectorAll('wc-product-tile')) {
            const root = t.shadowRoot;
            if (!root) continue;
            const a = root.querySelector('a[href*="/shop/productdetails/"]');
            if (!a) continue;
            const p = root.querySelector('[class*="product-tile-price"]');
            const c = root.querySelector('[class*="price-per-cup"]');
            out.push({href: a.getAttribute('href') || '',
                      price_text: p ? p.textContent.trim() : '',
                      cup_text: c ? c.textContent.trim() : ''});
        }
        return out;
    }"""
    try:
        return session.page.evaluate(js) or []
    except (PWTimeout, PWError) as e:
        # A tile read is a nice-to-have: it confirms a price the API already
        # gave us. It must never take a run down.
        logger.debug("tile read failed: %s", e)
        return []


# Every page of a listing arrives over the site's own API, and that API can
# be refused while the document keeps answering 200. Counting the refusals is
# what separates a listing that ran out (COMPLETE) from one whose next page
# was refused (PARTIAL) — without it the two are the same observation and a
# throttled run reports "complete" holding page 1 (§7).
#
# Any status other than 200, rather than one specific code, and the SAME test
# in all three engines. A test that differed between them would mean one
# engine reporting `complete` where its twins report `partial` on the
# identical run, which is precisely the drift the shared modules exist to
# prevent (§6).
_API_PATH_MARKER = "/apis/ui/"


def _watch_api(session) -> None:
    """Count API responses that were not 200.

    The document and the API are refused SEPARATELY on this site: the page
    itself can answer 200 while `/apis/ui/...` behind it is throttled. A run
    that saw that and reported "the listing ended" would claim a complete run
    while holding page 1 (§7), so the count is kept and consulted.
    """
    session._api_errors = 0

    def on_response(resp):
        try:
            if _API_PATH_MARKER in resp.url and resp.status != 200:
                session._api_errors += 1
        except Exception:  # noqa: BLE001 — a listener must never raise
            pass

    session.context.on("response", on_response)


def _api_error_count(session) -> int:
    return getattr(session, "_api_errors", 0)


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args, html: str = "") -> int:
    return page_flow.min_matches(args.mode)


def _classify(page, html: str, status=None) -> str:
    return page_flow.classify(html, status, page.url)


# Chromium's own names for "the proxy is the problem, not the site". Matched
# on the error text because Playwright surfaces them as a generic Error.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH",
    "ERR_PROXY_CERTIFICATE_INVALID",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one.

    Distinguishing this from an ordinary timeout matters because the two want
    opposite responses: a timeout deserves a retry from the same exit, while
    an unusable exit deserves a different one — retrying it unchanged just
    spends the budget on a proxy that is not going to answer.
    """
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch a browser on `pool`'s current exit; return (browser, context, page).

    Uses Playwright's own Chromium by default, and on this site that is a
    measurement rather than a shrug: it was served HTTP 200 and the full feed
    on every fetch that was not challenged, and the challenges it did meet
    were Cloudflare's managed one, which tracks the ADDRESS's recent request
    rate rather than the browser build. `--browser-channel chrome` is offered
    for a reader who wants it and is not needed.

    Factored out so a proxy rotation can tear the whole browser down and call
    it again. Swapping the proxy under a live session would be cheaper and
    wrong: cookies a bot manager issued against one exit, replayed from
    another, are a stronger signal than either address alone.
    """
    launch_kwargs = {"headless": args.headless, "args": list(LAUNCH_ARGS)}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    channel = args.browser_channel
    if channel:
        try:
            browser = pw.chromium.launch(channel=channel, **launch_kwargs)
        except (PWError, PWTimeout) as e:
            # A fallback, not a downgrade. Unlike a sibling site, this one
            # was measured serving the bundled Chromium the full feed, so
            # losing the requested channel costs nothing that is known.
            logger.info(
                "Could not launch the %r channel (%s) — using Playwright's "
                "own Chromium instead, which this site was measured serving "
                "normally. Install the channel with `playwright install %s` "
                "if you want it.", channel, str(e)[:160], channel)
            browser = pw.chromium.launch(**launch_kwargs)
    else:
        browser = pw.chromium.launch(**launch_kwargs)

    ctx_kwargs = {"user_agent": _chrome_ua(browser.version),
                  "locale": args.locale,
                  "viewport": {"width": 1440, "height": 900}}
    init_script = None
    if args.fingerprint:
        # Only meaningful on this branch. Over --cdp-endpoint the Scraping
        # Browser already has its own fingerprint, and layering a second one
        # on top produces a mismatch rather than better cover.
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)",
                    fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        context.add_init_script(init_script)
    return browser, context, context.new_page()


class _BrowserSession:
    """One browser + context + page, relaunchable onto a different exit.

    Exists because a rotation replaces all three handles at once, and passing
    three mutable locals through every helper is how one of them ends up
    stale.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        _watch_api(self)
        return self

    def relaunch(self):
        """Tear the browser down and come back on the pool's current exit.

        On a remote browser this is a no-op — its exit is not ours to change.
        """
        if self.remote:
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    try:
        browser = pw.chromium.connect_over_cdp(
            args.cdp_endpoint, timeout=args.cdp_connect_timeout * 1000)
    except (PWError, PWTimeout) as e:
        # Playwright puts the endpoint it tried into the exception text, and
        # that endpoint is a URL with a password in it — repeated five times,
        # in the message plus a four-line call log. Unmasked it lands in the
        # terminal, in CI output and in any log the run is piped to, which is
        # the one thing this project promises does not happen. The host and
        # port are KEPT: which endpoint failed is the useful half and is not
        # the secret.
        raise PWError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"A Scraping Browser profile allows ONE live connection at a "
            f"time, so `profile_locked` means something holds this `pid`.\n"
            f"Worth knowing before you go looking for it on your side: two "
            f"profiles were observed here entering that state and NOT leaving "
            f"it — one for over forty minutes, one still locked after four "
            f"minutes of no requests at all, having locked on its very first "
            f"connection attempt. Waiting did not clear either. If that is "
            f"what you are seeing, it is not another run of this tool holding "
            f"it, and nothing on this side will free it: use a different pid, "
            f"or reset the profile from the 2Captcha dashboard."
        ) from None
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    # The Scraping Browser API exposes a documented CDP domain
    # (`Captcha.setAutoSolve` / `Captcha.solve`) that clears supported
    # challenges inside the browser. Tried first when --cdp-endpoint is set;
    # this script's own detect+solve logic still runs as a fallback.
    #
    # Worth knowing what it can and cannot do here, stated precisely (§19).
    #
    # The refusal this site actually serves is Akamai's edge denial: HTTP 403
    # and roughly 400 bytes of plain HTML — a title, an `<h1>`, one sentence
    # and a reference number. Measured on it: 0 `data-sitekey` attributes, 0
    # iframes of any kind, 0 references to reCAPTCHA, hCaptcha, Turnstile,
    # DataDome or PerimeterX. There is no widget on that page, so there is
    # nothing for this auto-solver — or for any solver, at any price — to
    # answer. That is a property of THE PAGE, not a limit of the product.
    #
    # What the auto-solver WOULD cover, if Woolworths ever rendered one, is a
    # reCAPTCHA or Turnstile widget; `captcha_solver.py` alongside it handles
    # reCAPTCHA v2 and v3, enterprise reCAPTCHA
    # (`RecaptchaV2EnterpriseTaskProxyless`) and Cloudflare Turnstile
    # (`TurnstileTaskProxyless`).
    #
    # One measured caution about reading markup fetched through this
    # endpoint: the Scraping Browser's own auto-solve extension injects a
    # `cf-turnstile-response` hunter into every page it loads, so the bare
    # string `cf-turnstile` appears on a perfectly good Woolworths listing
    # fetched over --cdp-endpoint (counted: 1 occurrence on two served pages,
    # 0 on the Akamai denial). `captcha_solver.detect_turnstile` deliberately
    # keys on Cloudflare's own furniture and on a class-plus-sitekey pattern
    # the extension's script tag cannot match, so it is not fooled — and the
    # smoke suite pins that, because the failure mode is paying for a solve
    # of a captcha that was never there (§19).
    try:
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Captcha.setAutoSolve",
                         {"autoSolve": True, "options": [{"type": "*"}]})
        cdp_session.on("Captcha.detected", lambda *_: logger.info(
            "[Scraping Browser] CAPTCHA detected on page."))
        cdp_session.on("Captcha.waitForSolve", lambda *_: logger.info(
            "[Scraping Browser] CAPTCHA sent to 2captcha for solving."))
        cdp_session.on("Captcha.solveFinished", lambda *_: logger.info(
            "[Scraping Browser] CAPTCHA solved automatically."))
        cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning(
            "[Scraping Browser] CAPTCHA auto-solve failed."))
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
    except Exception as e:  # noqa: BLE001
        logger.info("Captcha.setAutoSolve not available on this "
                    "--cdp-endpoint (%s) — relying on this script's own "
                    "detect+solve logic instead.", e)
    return browser, context, page


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching GLOBALLY rather than once is the point: a Playwright connection
# error repeats the endpoint five times, so a masker that handled only the
# first occurrence would print the password four times and look like it was
# working.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _content_when_settled(page, attempts: int = 4, pause_ms: int = 700):
    """page.content() that tolerates a page mid-navigation.

    Playwright RAISES rather than returning empty while a navigation is in
    flight ("Unable to retrieve content because the page is navigating"), and
    this site's challenge handler resolves by navigating — so the one moment
    this is called is the one moment it can fail. Returns None if the page
    will not hold still, so a caller can skip a check instead of failing the
    run.
    """
    for attempt in range(1, attempts + 1):
        try:
            return page.content()
        except PWError as e:
            if "navigating" not in str(e).lower():
                raise
            if attempt == attempts:
                logger.warning("Page kept navigating through %d attempts — "
                               "continuing without a snapshot.", attempts)
                return None
            logger.info("Page is navigating — retrying content() in %dms "
                        "(%d/%d).", pause_ms, attempt, attempts)
            page.wait_for_timeout(pause_ms)
    return None


def handle_captcha_if_present(page, args, allow_solve: bool = True) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Runs after EVERY navigation, for ANY page. The static-HTML and runtime
    reCAPTCHA detectors are run and RECONCILED against each other rather than
    short-circuited, because they can disagree about the variant and the
    parameters for one are rejected for the other.

    NOTE what this cannot help with, because on this site it is the normal
    case. Woolworths' refusal is Akamai's EDGE DENIAL — HTTP 403 and about
    400 bytes of plain HTML: a title, an `<h1>`, one sentence and a reference
    number. Measured on it: 0 `data-sitekey` attributes, 0 iframes, 0
    references to reCAPTCHA, hCaptcha, Turnstile, DataDome or PerimeterX.
    There is no widget on that page, so there is nothing to hand a solver —
    which is why `page_flow.STATE_POLICY` marks `blocked` as
    retry-but-do-not-solve and nothing is ever charged for it.

    Say that precisely, because the sentence next door is the most expensive
    mistake this family has made (§19): this is a statement about WHAT THIS
    PAGE CARRIES, not about what a solver can do. "Unsolvable" is a property
    of a page with no widget on it. If Woolworths ever serves an
    interstitial that does carry one, `captcha_solver.py` already implements
    reCAPTCHA v2 and v3, enterprise reCAPTCHA
    (`RecaptchaV2EnterpriseTaskProxyless`) and Cloudflare Turnstile
    (`TurnstileTaskProxyless`), and the one line to change is that flag.

    The detector is still run on every navigation, and that asymmetry is
    deliberate (§8). What a visitor meets depends on the exit country and on
    what the address has been doing, and a narrow detector is how a rendered
    challenge gets reported as an empty category months later. The cost of
    running it on a good page is one DOM read; the cost of not running it is
    a silent zero.

    It is also DELIBERATELY not narrowed to what was seen. Counted across the
    captures taken for this repo, WOOLWORTHS' OWN pages carry no captcha of
    any kind: `sitekey`, `data-sitekey`, `<captcha`, `recaptcha`, `hcaptcha`
    and `turnstile` are each 0 on all five served pages (search, category,
    specials, home, a no-results search) and on both Akamai denials. So
    unlike a sibling site — which had a reCAPTCHA key in its page config and
    an empty `<captcha-widgets>` mount on every page while rendering neither
    — there is no site-specific captcha shape to key on here, and the
    family's general detector is what remains.

    ONE CAVEAT, and it is the §19 extension trap arriving for real: a page
    fetched through the Scraping Browser is NOT clean. The same served
    listing that counts 0 locally counts `turnstile` 3, `recaptcha` 2 and
    `<captcha` 1 when fetched over --cdp-endpoint, because 2Captcha's
    auto-solve extension injects its hunters into every page it loads. So a
    reader debugging from a --dump-html capture taken over --cdp-endpoint
    will see captcha markers on a page that was served perfectly well, and
    they are the extension's rather than the site's. That is a fact about
    the capture, not about Woolworths.
    """
    html = _content_when_settled(page)
    if html is None:
        return False

    # Detected is not the same as blocking. A challenge on a page whose
    # answers are already rendered guards nothing, and counting the anchors is
    # instant — which is why this check sits here rather than after the
    # readiness wait. The other way round would cost 25 wasted seconds on a
    # page the challenge genuinely gates, where solving FIRST is what makes
    # the content appear.
    already_rendered = _count(page, _ready_selector(args))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: page.evaluate(js), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False

    # DETECTION is broad and unconditional; SOLVING is policy. `allow_solve`
    # comes from `page_flow.should_solve(state)`, which is False for every
    # state on this site because Akamai's denial here carries no widget for
    # a solver to answer (section 19). Detecting and then saying so is the
    # point: a challenge nobody logged is a silent zero months later.
    if not allow_solve:
        logger.warning(
            "%s detected via %s on a page whose state does not permit a "
            "solve. NOT spending a solve. This is worth reporting: no "
            "captcha has ever been observed on this site, so a real one "
            "here means page_flow.STATE_POLICY needs its `solve` flag "
            "turned on for this state.", challenge.kind, challenge.source)
        return False

    if when_blocked and already_rendered > page_flow.MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d cards are already on the page "
                    "— not solving it. Pass --solve-captcha always to solve "
                    "it anyway.", challenge.kind, challenge.source,
                    already_rendered)
        return False

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting "
                   "to solve.", challenge.kind, challenge.source,
                   challenge.sitekey, challenge.action)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be "
                       "solved — continuing with whatever the page holds.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing with "
                     "whatever the page holds.", e)
        return False

    page.evaluate(INJECT_TOKEN_JS, token)
    logger.info("Token injected. Reloading page to continue.")
    page.wait_for_timeout(1500)
    page.reload(wait_until="domcontentloaded", timeout=60000)
    return True


def _rows_from_payload(payload, args, page_num: int, data_source: str) -> List:
    """Rows for this page, always a list.

    `page_num` is threaded through rather than defaulted, because `position`
    restarts at 1 on every page: without the page number beside it a row from
    page 2 claims the same position as one from page 1 and the two are
    indistinguishable in the output (§18).
    """
    return products_from_payload(payload, page=page_num,
                                 data_source=data_source, host=CANONICAL_HOST)


def _confirm_with_dom(session, rows, page_num: int) -> Optional[dict]:
    """Confirm the API's prices against the rendered tiles.

    A CONFIRMATION, never a correction: where the two agree the row's
    `price_source` becomes `api+dom`, and where they disagree the row is left
    exactly as the API gave it and a warning names the sku (§4).

    Only meaningful for the page the browser is actually LOOKING at, which is
    page 1 — pages 2..N are fetched over the API without navigating, so the
    tiles on screen still belong to page 1 and matching them against page 2's
    rows would confirm nothing and could mis-attribute a price. The stockcode
    key makes a wrong match impossible rather than unlikely, but asking the
    question at all on the wrong page is noise.
    """
    tiles = _read_tiles(session)
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
            "grid re-rendered under the read. The rows are the API's and are "
            "unchanged.", share, DOM_CONFIRM_FLOOR)
    return {"tiles": len(tiles), "checked": checked, "confirmed": confirmed}


# ===========================================================================
# Workers — archive mode only
# ===========================================================================
def _worker_pool(pool, worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Each worker gets its OWN pool object holding the same exits rotated to a
    different offset. Two things fall out of that, both wanted:

      * Workers start on distinct exits, which is the point of running
        several — N workers all leaving from one address is just a faster way
        to burn that address.
      * No shared mutable state between threads, so rotation needs no lock:
        the concurrency is safe by construction rather than by discipline
        (§7).
    """
    if not pool:
        return None
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


def _fetch_pages_concurrently(args, pool, specs, concurrency: int):
    """Fetch `specs` [(page_num, url), ...] across `concurrency` workers.

    Each worker owns its own Playwright instance, browser and exit: with the
    sync API a browser belongs to the thread that made it, so sharing one
    across threads is not an option even if it were desirable.

    Only ever called for a day-archive walk. Those URLs are independent by
    construction — page 5 is "five days earlier", knowable without fetching
    page 4 — which is exactly what the other three modes do not have.
    """
    work = queue.Queue()
    for spec in specs:
        work.put(spec)

    results = []
    results_lock = threading.Lock()
    # Set when a day comes back with no rows at all. Without it, asking for
    # 40 days of a tag that only published on three of them would fetch 37
    # empty ones. Workers check it before taking more work, so at most
    # (concurrency - 1) extra fetches are in flight when it trips.
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            with sync_playwright() as pw:
                session = _BrowserSession(pw, args,
                                          _worker_pool(pool, index)).open()
                try:
                    first = True
                    while not exhausted.is_set():
                        try:
                            page_num, url = work.get_nowait()
                        except queue.Empty:
                            break
                        if not first:
                            time.sleep(args.delay)
                        first = False
                        outcome = _fetch_one_page(session, args, session.pool,
                                                  page_num, url)
                        with results_lock:
                            results.append(outcome)
                        # ORGANIC rows, not rows. Past its last real page
                        # this API keeps answering 200 with nothing but the
                        # same promoted ads page 1 carried — page 18 of a
                        # 16-page category returned 8 rows, every one an ad.
                        # A worker that stopped on "no rows" would never
                        # stop, and would pad the output with the same eight
                        # ads once per queued page (§7).
                        if outcome.ok and not page_flow.organic_count(
                                outcome.products):
                            logger.info("[%s] page %d carried no organic "
                                        "product — treating that as the end "
                                        "of the listing and stopping "
                                        "dispatch.", name, page_num)
                            exhausted.set()
                finally:
                    session.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as "
                             "unattempted rather than failed.", name)

    threads = [threading.Thread(target=worker, args=(i,),
                                name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Anything still queued was never attempted (a worker died, or dispatch
    # stopped at the end of the listing). NOT reported as failed pages: they
    # were not tried, and claiming otherwise would overstate the damage (§8).
    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait()[0])
        except queue.Empty:
            break
    return results, sorted(unattempted), exhausted.is_set()


# Exceptions an API request can raise on this driver.
API_ERRORS = (PWTimeout, PWError)


def _fetch_api(session, path, body=None):
    """Session-shaped wrapper, so the retry helper reads the same in all
    three engines."""
    return _fetch_api_raw(session.page, path, body)


def _fetch_api_with_retries(session, args, path, body, page_num):
    """`_fetch_api`, with the user's retry budget spent on it.

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
            status, payload, text = _fetch_api(session, path, body)
        except API_ERRORS as e:
            status, payload, text = None, None, ""
            logger.debug("API request raised: %s", e)
        if status == 200 and payload is not None:
            return status, payload, text
        if attempt < max(1, args.retries):
            pause = args.retry_delay * (2 ** (attempt - 1))
            logger.warning(
                "The API request for page %d did not return usable JSON "
                "(HTTP %s, %d bytes) — retrying in %.1fs (attempt %d/%d).",
                page_num, status, len(text or ""), pause, attempt, args.retries)
            time.sleep(pause)
    return status, payload, text

def _land(session, args, pool, outcome) -> tuple:
    """Make sure the browser is ON the listing page. (html, status, state)

    Navigates only when it has to. The engine fetches every page of a listing
    from inside ONE loaded page — the navigation exists to make Akamai issue
    a session, not to reach page N — so this is a no-op after the first call
    unless a rotation replaced the browser underneath us.
    """
    if getattr(session, "landed_url", None) == args.url and session.page:
        try:
            return session.page.content(), 200, "content"
        except (PWTimeout, PWError):
            session.landed_url = None

    status = None
    for attempt in range(1, args.retries + 1):
        try:
            resp = session.page.goto(args.url, wait_until="domcontentloaded",
                                     timeout=60000)
            status = resp.status if resp else None
            break
        except (PWTimeout, PWError) as e:
            # A dead or misconfigured proxy raises PWError
            # (net::ERR_PROXY_CONNECTION_FAILED), not PWTimeout — catching
            # only the latter lets it escape as a traceback, which is the
            # likeliest failure the first time anyone points --proxy-file at
            # a real list (§8). They want opposite responses: a timeout
            # deserves another try at the same exit, a dead proxy a
            # different one.
            reason = _proxy_failure(e)
            if reason:
                logger.error("Exit failed: %s", reason)
                outcome.load_failed = True
                return None, None, "blocked"
            if attempt < args.retries:
                pause = args.retry_delay * (2 ** (attempt - 1))
                logger.warning("Timeout loading %s (attempt %d/%d) — retrying "
                               "in %.1fs.", args.url, attempt, args.retries, pause)
                time.sleep(pause)
            else:
                outcome.load_failed = True
                return None, None, "blocked"

    html = _content_when_settled(session.page) or ""
    state = _classify(session.page, html, status)
    if state != "blocked":
        session.landed_url = args.url
    return html, status, state


def _resolve_target(session, args, outcome) -> tuple:
    """What to ask the API for. (ok, term, category_id, slug)

    A category id is OPAQUE and has to be looked up in the site's own tree:
    `bakery` is `1_DEB537E`, `fruit-veg` is `1-E5BEE36E`. Sending the slug
    instead returns HTTP 200 with zero products and `Success: true`, which is
    indistinguishable from a real empty category — so an unresolved slug is
    refused here rather than turned into a run that reports success on
    nothing.

    Cached on the session: the tree is 2,696 nodes and does not change
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
    cached = getattr(session, "category_ids", None)
    if cached is None:
        cached = session.category_ids = {}
    if slug in cached:
        return True, None, cached[slug], slug

    status, tree, _ = _fetch_api(session, API_CATEGORIES_PATH, None)
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


def _fetch_one_page(session, args, pool, page_num: int,
                    url: Optional[str] = None) -> PageOutcome:
    """Fetch one page of the listing and parse it.

    `url` is accepted for the family's signature and is the LISTING's
    address, the same for every page: on this site a page is a number in a
    request body, not an address. Passing it keeps the concurrent fetcher's
    call shape identical to its siblings'.

    Returns a PageOutcome and never raises for an EXPECTED failure — a
    timeout, a refusal, a dead exit are all recorded on the outcome instead.

    Always goes through `session.page`, never a captured local: a rotation
    replaces the browser, context and page together, and a stale handle is
    exactly the bug _BrowserSession exists to prevent.
    """
    outcome = PageOutcome(page_num=page_num, url=url or args.url)
    has_pool = bool(pool and len(pool) > 1)
    block_retries = page_flow.block_retries(has_pool)

    html = state = None
    for block_attempt in range(block_retries + 1):
        outcome.load_failed = False
        html, status, state = _land(session, args, pool, outcome)

        # The POLICY decides whether another fetch could change this
        # answer, rather than each engine deciding for itself (section 1).
        # False for content, shell and empty; True for blocked and unknown.
        if not page_flow.should_retry(state) and not outcome.load_failed:
            break

        if block_attempt < block_retries:
            # A rotation is a FRESH BROWSER (§8). Cookies Akamai issued
            # against exit A and replayed from exit B are a stronger signal
            # than either address alone, and Akamai session cookies are
            # exactly what this site issues.
            if pool and page_flow.RETRY_NEEDS_FRESH_CONTEXT:
                try:
                    pool.rotate()
                except ProxyError as e:
                    logger.warning("Could not rotate the exit: %s", e)
            session.landed_url = None
            logger.warning("Refused (attempt %d of %d) — relaunching%s.",
                           block_attempt + 1, block_retries + 1,
                           " on the next exit" if has_pool else "")
            try:
                session.relaunch()
            except Exception as e:  # noqa: BLE001
                logger.warning("Relaunch failed: %s", e)
            time.sleep(args.retry_delay)

    # Detection runs on every page, whatever the state — a challenge that
    # nobody recognises becomes an empty category months later (section 8).
    # Whether a SOLVE may be bought is page_flow's call.
    try:
        if args.solve_captcha != "never" and session.page is not None:
            if handle_captcha_if_present(session.page, args,
                                         allow_solve=page_flow.should_solve(state)):
                html = _content_when_settled(session.page) or html
                state = _classify(session.page, html, None)
    except (PWTimeout, PWError) as e:
        logger.debug("captcha check skipped: %s", e)

    outcome.state = state

    if outcome.load_failed and state != "blocked":
        outcome.final_url = session.page.url if session.page else args.url
        return outcome

    if page_flow.counts_as_blocked(state):
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        try:
            session.page.screenshot(path=f"{args.out}_page{page_num}_debug.png")
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        refs = asset_reference_count(html or "")
        marker = detect_block_marker(html or "")
        logger.error(
            "The site did not serve this request — %d bytes, %d reference(s) "
            "to the site's own asset host, saved to %s. This is exit 3, "
            "distinct from a genuinely empty result (exit 4).%s",
            len(html or ""), refs, debug_html,
            (f" Tried {block_retries + 1} exit(s)." if has_pool
             else f" Re-fetched {block_retries + 1} time(s)."))
        logger.error("%s", page_flow.block_advice(
            html, headless=bool(getattr(args, "headless", False)),
            has_pool=has_pool))
        outcome.blocked_by = marker or ("no-response" if not html else "akamai")
        outcome.final_url = session.page.url if session.page else args.url
        return outcome

    # Wait for the grid. BOUNDED, and not fatal if it never paints: an empty
    # category renders no tiles, and treating that as a failure would report
    # a correct answer as a fault.
    if page_num == 1:
        found = page_flow.wait_for_tiles(
            lambda sel: _count(session.page, sel),
            session.page.wait_for_timeout,
            page_flow.ready_selector(args.mode), _min_matches(args, html))
        logger.info("%d tile(s) had painted when the API was asked.", found)
        # Said LOUDLY, because nothing else in the run will notice: the
        # document answered 200, the API is about to answer 200, and the rows
        # will be correct. Only the address gives it away.
        if is_unauthorised_redirect(session.page.url):
            outcome.unauthorised = True
            logger.warning(
                "The app navigated itself to %s — it has decided this "
                "browser is automated, and the product grid will not render. "
                "The rows below still come from the site's own API and are "
                "correct, but the DOM price cross-check is impossible and "
                "price_source stays 'api' on every row. This engine passes "
                "%s to prevent it, so seeing this means either the flag did "
                "not take effect or the site now looks at something else.",
                session.page.url, LAUNCH_ARGS[0])

    ok, term, category_id, slug = _resolve_target(session, args, outcome)
    if not ok:
        outcome.final_url = session.page.url if session.page else args.url
        return outcome

    path, body, data_source = api_request_for(
        args.mode, term=term, category_id=category_id, slug=slug,
        page=page_num, page_size=args.page_size)

    errors_before = _api_error_count(session)
    status, payload, text = _fetch_api_with_retries(
        session, args, path, body, page_num)

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
            "are not" if payload is None else "is",
            args.delay)
        outcome.load_failed = True
        outcome.state = "api_error"
        outcome.final_url = session.page.url
        return outcome

    if args.dump_html:
        # Dumping on success, not only on failure: a run can return the right
        # NUMBER of rows with a field silently unpopulated, and then the only
        # way to tell a parsing bug from a too-early snapshot is the exact
        # bytes. On this site the bytes that matter are the API's, not the
        # document's, so BOTH are written.
        base = (args.dump_html if args.pages == 1
                else f"{args.dump_html}.page{page_num}")
        with open(base, "w", encoding="utf-8") as f:
            f.write(html or "")
        with open(f"{base}.api.json", "w", encoding="utf-8") as f:
            f.write(text or "")
        logger.info("Saved the document to %s (%d bytes) and the payload the "
                    "parser actually reads to %s.api.json (%d bytes).",
                    base, len(html or ""), base, len(text or ""))

    products = _rows_from_payload(payload, args, page_num, data_source)
    stated = total_count(payload)
    organic = organic_count(products)
    logger.info("Parsed %d row(s) from page %d — %d organic, %d promoted.%s",
                len(products), page_num, organic, len(products) - organic,
                f" The site states {stated} result(s) for this listing."
                if stated is not None else "")

    outcome.stated_total = stated
    outcome.api_errors = _api_error_count(session) - errors_before

    if products and page_num == 1:
        outcome.dom_confirm = _confirm_with_dom(session, products, page_num)

    if products:
        # Counted over the rows that CAN carry a price. Woolworths publishes
        # none for a product it is not selling: on one fruit-veg page 6 of 73
        # rows had no price and every one of the 6 was `IsAvailable: false`,
        # while 67 of the 67 available rows had one. Counting those 6 against
        # the floor printed a warning about a completely correct read, and a
        # warning that fires when nothing is wrong teaches people to ignore
        # warnings.
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
        # readers doing different things (section 20). The run still reports
        # exit 4 — the catalogue question was answered and the answer was
        # nothing — but nothing is dumped and nobody is sent to debug a read
        # that worked.
        logger.info(
            "The site reports 0 results for this listing, and 0 rows were "
            "parsed. That is a correct, empty answer rather than a failed "
            "read — exit 4.")
        outcome.state = "empty"
    else:
        # The site said it HAS results and the parser produced none. That is
        # ours, and it is the case section 20 asks to be named rather than
        # reported as "0 products", which sends the reader to check the URL
        # instead of the payload.
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        with open(f"{debug_html}.api.json", "w", encoding="utf-8") as f:
            f.write(text or "")
        outcome.state = "parse_failed"
        logger.error(
            "The site states %s result(s) for this listing and the parser "
            "produced NONE. That is a parser failure, not an empty "
            "category. Saved the document to %s and the payload to "
            "%s.api.json — the likeliest cause is that the group wrapper "
            "shape changed, so check whether `Products`/`Bundles` still "
            "holds one level of wrapper objects.",
            stated, debug_html, debug_html)

    outcome.products = products
    outcome.final_url = session.page.url
    return outcome


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    dedupe_key = "sku"
    stop_reason = "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None

    # Every page of every mode is addressable: a page is an integer in a
    # request body, so page N can be asked for without reading page N-1 (§7).
    by_url = page_flow.paginates_by_url(args.mode, args.url)

    concurrency = max(1, args.concurrency)
    if concurrency > 1:
        # Said out loud, because on several sites in this family the same
        # flag is accepted and then refused, and a reader deserves to know
        # which case they are in without reading the source.
        logger.info("--concurrency %d is available here: %s.",
                    concurrency, PAGE_URL_REASON)
        refusal = page_flow.concurrency_refusal(args.mode, args.url)
        if refusal:
            logger.warning("--concurrency %d is refused: %s.", concurrency, refusal)
            concurrency = 1
        elif args.cdp_endpoint:
            logger.warning("--concurrency is ignored with --cdp-endpoint: the "
                           "Scraping Browser API allows one live connection "
                           "per profile, and several workers would collide on "
                           "it (profile_locked). Use several pids instead, "
                           "one run each.")
            concurrency = 1
        elif not pool:
            logger.warning("--concurrency %d with no proxy pool: every worker "
                           "leaves from the SAME address, which is a faster "
                           "way to get that address scored than to gather "
                           "data. Akamai already refuses this site to a "
                           "datacentre address; an address that works is one "
                           "worth not burning. Pass --proxy-file to spread "
                           "the load.", concurrency)
        if pool and pool.rotates_per_page():
            logger.info("--proxy-rotate per-page is redundant under "
                        "--concurrency: each worker already holds its own "
                        "exit for its lifetime, which is the same spread "
                        "without a browser relaunch per page.")
        if concurrency > 8:
            logger.warning("--concurrency %d means %d browsers at once "
                           "(~150-300MB each), and every one of them is a "
                           "REAL WINDOW on this site, because headless is "
                           "refused. Make sure the machine has the memory "
                           "and a display for it.", concurrency, concurrency)

    with sync_playwright() as pw:
        session = _BrowserSession(pw, args, pool,
                                  remote=bool(args.cdp_endpoint)).open()
        try:
            # Page 1 is always fetched on its own, because its answer is what
            # decides whether the rest is worth asking for.
            first = _fetch_one_page(session, args, pool, 1, args.url)
            outcomes.append(first)

            if not first.ok:
                stop_reason = ("page_load_timeout" if first.load_failed
                               else f"blocked_{first.blocked_by}")
                blocked = first.blocked_by is not None
            else:
                seen_keys.update(p.sku for p in first.products
                                 if p.sku is not None)
                organic_first = page_flow.organic_count(first.products)
                if organic_first == 0 and args.pages > 1:
                    logger.info("Page 1 carried no organic rows — not asking "
                                "for more pages.")
                    stop_reason = "pagination_exhausted"
                elif args.pages > 1 and concurrency > 1:
                    session.close()
                    specs = [(n, args.url) for n in range(2, args.pages + 1)]
                    logger.info("Fetching %d more page(s) across %d workers%s.",
                                len(specs), concurrency,
                                f" over {len(pool)} exit(s)" if pool else "")
                    rest, unattempted, exhausted = _fetch_pages_concurrently(
                        args, pool, specs, concurrency)
                    outcomes.extend(rest)
                    failed = [o for o in rest if not o.ok]
                    if failed:
                        stop_reason = "page_failed"
                        blocked = blocked or any(o.blocked_by for o in failed)
                    elif exhausted:
                        stop_reason = "pagination_exhausted"
                    if unattempted:
                        logger.info("%d page(s) were never attempted: %s.",
                                    len(unattempted),
                                    ", ".join(str(n) for n in unattempted))
                elif args.pages > 1:
                    for page_num in range(2, args.pages + 1):
                        if page_flow.page_cap_reached(page_num):
                            stop_reason = "page_cap"
                            logger.warning("Stopping at the %d-page cap.",
                                           PAGE_CAP)
                            break
                        time.sleep(args.delay)
                        out = _fetch_one_page(session, args, pool, page_num,
                                              args.url)
                        outcomes.append(out)
                        if not out.ok:
                            stop_reason = ("page_load_timeout"
                                           if out.load_failed
                                           else f"blocked_{out.blocked_by}")
                            blocked = blocked or out.blocked_by is not None
                            break

                        # The listing has ended when it stops producing NEW
                        # ORGANIC rows — never when a page comes back empty.
                        # Past its last real page this API keeps answering
                        # 200 with nothing but the same promoted rows page 1
                        # carried, so a loop testing "were there rows" never
                        # terminates (§7, and see page_flow.advance_page).
                        turn = page_flow.advance_page(out.products, seen_keys)
                        seen_keys.update(p.sku for p in out.products
                                         if p.sku is not None)
                        if turn != page_flow.ADVANCED:
                            logger.info(
                                "Page %d added no organic product this run "
                                "had not already seen (%s) — the listing has "
                                "ended. Stopping with %d page(s) fetched "
                                "rather than the %d asked for.",
                                page_num,
                                "every row on it was a promoted ad"
                                if turn == page_flow.ADS_ONLY
                                else "no new rows",
                                page_num, args.pages)
                            stop_reason = "pagination_exhausted"
                            break
        finally:
            try:
                session.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("Ignoring teardown error: %s", e)

    # Merge in PAGE ORDER, not arrival order: dedupe that mutates a running
    # set inside the loop makes the output depend on which page finished
    # first (§8).
    all_rows = []
    seen = set()
    for outcome in sorted(outcomes, key=lambda o: o.page_num):
        all_rows.extend(dedupe_by_key(outcome.products, seen, dedupe_key))

    pages_completed = sum(1 for o in outcomes if o.ok)
    failed_pages = sorted(o.page_num for o in outcomes if not o.ok)

    sponsored = sum(1 for r in all_rows if r.is_sponsored)
    if all_rows:
        logger.info("%d row(s) after deduping on %s, of which %d (%.0f%%) are "
                    "promoted ads. The same ads are served on every page, so "
                    "the dedupe is doing real work here rather than guarding "
                    "against a hypothetical.",
                    len(all_rows), dedupe_key, sponsored,
                    100.0 * sponsored / len(all_rows))

    stated = next((o.stated_total for o in outcomes
                   if o.stated_total is not None), None)
    if all_rows and stated:
        logger.info("The site stated %d result(s) for this listing; this run "
                    "read %d row(s) across %d page(s). The difference is "
                    "pages not asked for, not rows missed — and the stated "
                    "figure is not stable across pages on this site.",
                    stated, len(all_rows), pages_completed)

    api_errors = sum(o.api_errors or 0 for o in outcomes)
    if api_errors:
        logger.warning("%d API response(s) during this run were not 200. A "
                       "page that answered 200 while its API call was refused "
                       "is a PARTIAL run, not a short listing.", api_errors)

    extra_meta = {
        "mode": args.mode,
        "stated_total": stated,
        "sponsored_rows": sponsored,
        "api_errors": api_errors,
        "unauthorised_redirect": any(o.unauthorised for o in outcomes),
        "dom_confirm": next((o.dom_confirm for o in outcomes
                             if o.dom_confirm), None),
    }

    final_url = next((o.final_url for o in sorted(outcomes,
                                                  key=lambda o: -o.page_num)
                      if o.final_url), args.url)
    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      pages_requested=args.pages,
                      pages_completed=pages_completed,
                      pages_failed=failed_pages,
                      blocked=blocked,
                      stop_reason=stop_reason,
                      start_url=args.url,
                      final_url=final_url,
                      mode=args.mode,
                      source=source_of(args.url),
                      extra=extra_meta)


def parse_args():
    p = argparse.ArgumentParser(
        description="Woolworths Online product scraper (Playwright edition)")
    p.add_argument("--url", default=None,
                   help="A Woolworths Online URL: a search "
                        "(/shop/search/products?searchTerm={term}) or a "
                        "category (/shop/browse/{slug}, child nodes "
                        "included). /shop/browse/specials is a category like "
                        "any other. A single product page is refused with the "
                        "reason — the same object is already on every listing "
                        "row. Required, unless WOOLWORTHS_URL is set in the "
                        "environment or in .env.")
    p.add_argument("--mode", choices=["search", "category"], default=None,
                   help="Which view the URL is. Inferred from the URL by "
                        "default, and passing one that disagrees with the URL "
                        "is an error rather than an override: the mode is a "
                        "property of the path. Both yield the same row — the "
                        "site answers both from the same API with the same "
                        "115-field product object — and data_source says "
                        "which endpoint answered.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Filled from the URL "
                        "by default — the search term or the category slug — "
                        "so it is rarely empty.")
    p.add_argument("--pages", type=int, default=1,
                   help=f"Number of pages to walk (default 1, cap {PAGE_CAP}). "
                        f"A page is a real, independent request: page N is an "
                        f"integer in the request body, so pages can be "
                        f"fetched out of order and --concurrency is "
                        f"meaningful. The walk stops early when a page adds "
                        f"no ORGANIC product it has not already seen — never "
                        f"when a page comes back empty, because past its last "
                        f"real page this listing keeps answering 200 with "
                        f"nothing but promoted ads.")
    p.add_argument("--delay", type=float, default=2.0,
                   help="Delay between pages, seconds (default 2.0). These "
                        "are API calls inside an already-loaded page rather "
                        "than navigations, so they are cheap — but they are "
                        "still requests from one address, and the address is "
                        "what Akamai scores.")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Fetch pages 2..N across N browsers, each on its own "
                        "exit (default 1). Allowed here because every page is "
                        "addressable. Refused with --cdp-endpoint: the "
                        "Scraping Browser API allows one live connection per "
                        "profile and workers collide (profile_locked). Note "
                        "every worker is a REAL WINDOW, because this site "
                        "refuses headless.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "The pause between attempts doubles each time.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry (default 2.0); it "
                        "doubles on each subsequent attempt.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="json",
                   help="Output format (default json).")
    p.add_argument("--out", default="woolworths_products",
                   help="Output filename prefix (default "
                        "woolworths_products). A run that finds nothing writes "
                        "nothing, so last night's good output survives a bad "
                        "night; --allow-empty opts out.")
    p.add_argument("--locale", default="en-AU",
                   help="Browser locale (default en-AU). Woolworths Online is "
                        "an Australian site; a different locale does not "
                        "change the catalogue or the currency, which is AUD "
                        "on every row.")
    p.add_argument("--browser-channel", default=DEFAULT_BROWSER_CHANNEL,
                   help="Chrome channel to launch instead of Playwright's "
                        "bundled Chromium (e.g. chrome, msedge). Not needed: "
                        "the bundled Chromium was served this site normally, "
                        "headful, on every attempt measured.")
    p.add_argument("--proxy", default=None,
                   help="Single proxy URL, e.g. http://user:pass@host:port. "
                        "Credentials are passed through the driver's own "
                        "fields, never on a command line.")
    p.add_argument("--proxy-file", default=None, metavar="PATH",
                   help="File of proxy URLs, one per line, used as a pool.")
    p.add_argument("--proxy-rotate", choices=ROTATE_MODES, default="per-run",
                   help="When to move to the next exit (default per-run). A "
                        "rotation always relaunches the browser: cookies "
                        "Akamai issued against one exit and replayed from "
                        "another are a stronger signal than either address.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool before use, so parallel runs do not "
                        "all start on the same exit.")
    p.add_argument("--proxy-block-retries", type=int, default=4, metavar="N",
                   help="How many exits to try when a page comes back refused "
                        "(default 4, and only with a pool).")
    p.add_argument("--twocaptcha-key", default=None,
                   help="2Captcha API key. Prefer TWOCAPTCHA_KEY in .env — a "
                        "key in argv is readable by anything that can run ps.")
    p.add_argument("--captcha-api", choices=["v1", "v2"], default="v2",
                   help="Which 2Captcha API to use (default v2, the "
                        "createTask/getTaskResult one). v1 puts the key in "
                        "the query string, where any error message leaks it.")
    p.add_argument("--solve-captcha", choices=["never", "when-blocked", "always"],
                   default="when-blocked",
                   help="When to spend money on a solve (default "
                        "when-blocked). NOTE: nothing has yet been measured "
                        "to solve on this site — Akamai's refusal here is a "
                        "400-byte HTML page carrying no widget of any kind, "
                        "so there is nothing on it for a solver to work on, "
                        "and no solve is attempted. That is a statement about "
                        "THIS PAGE, not about the product: if Woolworths ever "
                        "serves an interstitial that carries a widget, the "
                        "solver already handles reCAPTCHA v2/v3, enterprise "
                        "reCAPTCHA and Cloudflare Turnstile.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request (0.3, 0.7 or "
                        "0.9 — the API only accepts these three). Ignored for "
                        "v2 widgets.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a device fingerprint from 2Captcha and apply "
                        "it. Ignored with --cdp-endpoint: the remote browser "
                        "brings its own, and stacking a second creates a "
                        "contradiction rather than better cover.")
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400. Use --fp-country to narrow further. "
                        "(default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP "
                        "instead of launching one locally, e.g. "
                        "ws://user:pass@host:port — the Scraping Browser API "
                        "endpoint, or any browser that exposes a CDP URL. "
                        "--proxy, --browser-channel and --headless/--headful "
                        "are ignored when this is set. An AUSTRALIAN exit is "
                        "not required: a US exit was served this site "
                        "normally.")
    p.add_argument("--cdp-connect-timeout", type=float,
                   default=CDP_CONNECT_TIMEOUT_MS / 1000, metavar="SECONDS",
                   help=f"How long to wait for --cdp-endpoint to accept the "
                        f"connection (default {CDP_CONNECT_TIMEOUT_MS // 1000}). "
                        f"Deliberately high: a Scraping Browser provisions a "
                        f"browser when the WebSocket upgrade arrives, and one "
                        f"was measured taking 121s before the SERVER gave up.")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write an empty file when a run finds nothing. Off by "
                        "default: a consumer cannot tell an empty category "
                        "from a failed run, and overwriting good data with [] "
                        "destroys the last known good output.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save what the parser is given, on success as well as "
                        "failure. Writes TWO files per page: the document, "
                        "and `.api.json` — the payload the rows actually come "
                        "from. The second is the one to read, because this "
                        "site puts no product data in the document at all.")
    # HEADFUL by default, and the reason is measured rather than habitual.
    #
    # Four URLs, each fetched both ways from one datacentre address on
    # 2026-09-16: headful was served HTTP 200 and the full catalogue 4 of 4;
    # headless got Akamai's 403 "Access Denied" page 4 of 4. Unlike a sibling
    # repo, the difference here is NOT the `HeadlessChrome` UA token — this
    # engine builds an ordinary Chrome UA from the browser's own version in
    # both modes, and headless was still refused every time.
    p.add_argument("--headful", dest="headless", action="store_false",
                   default=False,
                   help="Run with a real browser window. THE DEFAULT, and on "
                        "this site the difference between data and a 403: "
                        "measured 4/4 served headful against 0/4 headless "
                        "from one datacentre address.")
    p.add_argument("--headless", dest="headless", action="store_true",
                   help="Run headless. Measured 0/4 served on this site from "
                        "a datacentre address — Akamai refused every one. "
                        "Kept for a reader whose address it works from, and "
                        "for a machine with no display, but expect exit 3.")
    args = p.parse_args()
    # Fill --twocaptcha-key / --cdp-endpoint / --proxy / --url from the
    # environment or .env when the flag was not given. An explicit flag wins.
    env_config.apply(args)
    if not args.url:
        p.error("no --url given, and WOOLWORTHS_URL is not set in the "
                "environment or in .env.")
    why = unsupported_reason(args.url)
    if why:
        # Refused rather than attempted. This parser reads one site's private
        # API, so pointing it elsewhere would not fail loudly — it would
        # return zero rows and look like an empty category.
        p.error(why)

    kind = mode_for_url(args.url)
    if args.mode is None:
        # Inferred from the URL, which is the only thing that can be right:
        # the mode is a property of the path, not a preference.
        args.mode = kind
        logger.info("Reading %s as a %s listing.", args.url, args.mode)
    elif args.mode != kind:
        p.error(f"--mode {args.mode} does not match {args.url!r}, which is a "
                f"{kind} page. The mode follows the URL on this site; leave "
                f"it off and it is inferred.")

    if args.category is None:
        args.category = (search_term_from_url(args.url) if kind == "search"
                         else category_slug_from_url(args.url))

    # Not a flag, and deliberately so. The site's own UI asks for 36 and this
    # asks for 36: a request that does not look like the UI's is the kind of
    # difference a bot manager scores on, and there is nothing to gain — the
    # page count simply halves if you double it.
    args.page_size = PAGE_SIZE

    if args.pages > PAGE_CAP:
        logger.warning("--pages %d is above this scraper's %d-page cap; it "
                       "will stop there.", args.pages, PAGE_CAP)
    if args.concurrency > 1 and args.cdp_endpoint:
        logger.info("--concurrency will be ignored: see --cdp-endpoint.")
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint "
                     "API uses the same key, though it's a separate "
                     "subscription from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint, and "
                       "stacking a second one on top creates a mismatch "
                       "rather than better cover.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        # Bad usage, not a crash: a typo in a proxy list would otherwise
        # surface as a connection failure on page 1 with nothing naming it.
        logger.error("%s", e)
        sys.exit(2)
    except PWError as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). `profile_locked` means another run still holds this
        # `pid`, and a harness that sees exit 1 goes looking for a bug in the
        # scraper instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
