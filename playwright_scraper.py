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
from output_writer import dedupe_by_key, finish_run, EXIT_API_ERROR, EXIT_BAD_USAGE
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


# One definition each, in page_flow, shared by the three engines (§27.5),
# with the measurements behind them written down there. Bound here so every
# caller — the concurrent fetcher, `scrape`, the suite — keeps naming them
# the way the family does.
PageOutcome = page_flow.PageOutcome
# Where a store lookup lands before asking the locator. Any page the site
# serves will do — the navigation exists to be issued an Akamai session,
# not to be read — and the home page is the cheapest one that is always
# there. Measured: the locator answers from it exactly as it does from a
# category page.
STORE_LANDING_URL = "https://www.woolworths.com.au/"

FIELD_FLOOR = page_flow.FIELD_FLOOR
DOM_CONFIRM_FLOOR = page_flow.DOM_CONFIRM_FLOOR



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
        # Which URL this session is currently landed on, or None. Declared
        # here rather than created by the first assignment from outside:
        # the shared loop reads it through its engine's ops object, and an
        # attribute that only exists once something has written it is one
        # rename away from an AttributeError nothing offline can see.
        self.landed_url = None

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


def handle_captcha_if_present(page, args, allow_solve: bool = True,
                              budget=None) -> bool:
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
    # The budget is charged HERE, immediately before the money is spent,
    # and not after the solver returns: a failed solve is still billed
    # (§19). One call site in this engine, inside the block-retry loop —
    # so without this a page retried four times would buy four solves the
    # day `page_flow.should_solve` starts returning True.
    if budget is not None and not budget.charge():
        logger.warning(
            "%s detected via %s, but this page has already spent its "
            "%d-solve budget (page_flow.SOLVES_PER_PAGE). NOT buying "
            "another. Rotating to a new exit does not refill it: the page "
            "is what is being paid for.", challenge.kind, challenge.source,
            budget.limit)
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


# ===========================================================================
# The driver primitives the shared loop asks for
# ===========================================================================
# `page_flow.fetch_one_page` is ONE implementation for all three engines
# (§27.5); this class is the only part of it that is Playwright's. Every
# method is either a driver call or a two-line adapter, and no JavaScript
# crosses the boundary in either direction — the shared module names the
# OPERATION and this spells it in Playwright's dialect (§1).
#
# `smoke_test.py` derives the required method set from page_flow's own AST
# rather than from a hand-written list, so the day the loop reaches for a
# new operation, every engine missing it fails by name.
class PlaywrightOps:
    """Playwright's half of the page loop."""

    # The exception types the shared loop catches around a driver call.
    driver_errors = (PWTimeout, PWError)
    # Named in the one warning about the app bouncing us to
    # /unauthorisederror, so the reader is told which flag was supposed to
    # prevent it.
    launch_arg_hint = LAUNCH_ARGS[0]

    def __init__(self, session):
        self.session = session
        # Cache for the category tree, filled by page_flow.resolve_target.
        self.category_ids: dict = {}

    # -- where are we -------------------------------------------------
    def is_live(self) -> bool:
        return self.session.page is not None

    def is_landed(self, url: str) -> bool:
        return getattr(self.session, "landed_url", None) == url

    def set_landed(self, url: str) -> None:
        self.session.landed_url = url

    def clear_landing(self) -> None:
        self.session.landed_url = None

    def current_url(self):
        return self.session.page.url if self.session.page else None

    # -- moving and reading -------------------------------------------
    def goto(self, url: str, timeout_ms: int):
        resp = self.session.page.goto(url, wait_until="domcontentloaded",
                                      timeout=timeout_ms)
        return resp.status if resp else None

    def document_text(self):
        return _content_when_settled(self.session.page)

    def classify(self, html, status=None) -> str:
        return _classify(self.session.page, html or "", status)

    def count(self, selector: str) -> int:
        return _count(self.session.page, selector)

    def wait_ms(self, ms: int) -> None:
        self.session.page.wait_for_timeout(ms)

    def screenshot(self, path: str) -> None:
        self.session.page.screenshot(path=path)

    def read_tiles(self):
        return _read_tiles(self.session)

    # -- the site's own API -------------------------------------------
    def fetch_api(self, path: str, body=None):
        return _fetch_api(self.session, path, body)

    def api_error_count(self) -> int:
        return _api_error_count(self.session)

    def rows_from_payload(self, payload, args, page_num: int, data_source):
        return _rows_from_payload(payload, args, page_num, data_source)

    # -- recovery ------------------------------------------------------
    def proxy_failure(self, exc):
        return _proxy_failure(exc)

    def relaunch(self) -> None:
        self.session.relaunch()

    def handle_captcha(self, args, allow_solve: bool, budget) -> bool:
        return handle_captcha_if_present(self.session.page, args,
                                         allow_solve=allow_solve,
                                         budget=budget)


def _fetch_one_page(session, args, pool, page_num: int,
                    url: Optional[str] = None):
    """Thin wrapper over the shared loop, kept for the family's signature.

    The concurrent fetcher and the sequential loop both call this, and both
    hand it a session rather than an ops object.
    """
    ops = getattr(session, "_ops", None)
    if ops is None:
        ops = session._ops = PlaywrightOps(session)
    return page_flow.fetch_one_page(ops, args, pool, page_num, url)

def _stop_reason_for(outcome) -> str:
    """Why the walk stopped, named by what actually went wrong.

    `parser_found_nothing` is binance's name for it (section 26) and is
    deliberately NOT in `COMPLETE_STOP_REASONS`: the site said it held
    results and we produced none, so nothing about the catalogue has been
    established. Without this the branch fell through to
    `blocked_{blocked_by}` and reported `blocked_None`.
    """
    if getattr(outcome, "parse_failed", False):
        return "parser_found_nothing"
    if outcome.load_failed:
        return "page_load_timeout"
    return f"blocked_{outcome.blocked_by}"


def _honour_store_request(session, args, pool):
    """Ask for the requested store, or refuse the run. Returns an exit code
    to return immediately, or None to carry on.

    The refusal is the point. Accepting `--store-id` and then scraping
    whatever store the session happens to have, with the requested number
    stamped on every row, would produce a file that is real in every cell
    and wrong in the one that matters (§8: never present a guess as a
    fact). So the request is MADE, the session's own store is read before
    and after, and a run that did not get what it asked for stops.
    """
    ops = getattr(session, "_ops", None)
    if ops is None:
        ops = session._ops = PlaywrightOps(session)

    landing = page_flow.PageOutcome(page_num=1, url=args.url)
    html, _status, state = page_flow.land(ops, args, landing)
    if page_flow.counts_as_blocked(state):
        logger.error("The landing page was refused, so the store request "
                     "could not even be made. %s", page_flow.block_advice(
                         html, headless=bool(getattr(args, "headless", False)),
                         has_pool=bool(pool and len(pool) > 1)))
        return finish_run(
            [], args.out, args.format, args.allow_empty, blocked=True,
            stop_reason="blocked", pages_requested=args.pages,
            pages_completed=0, start_url=args.url,
            final_url=ops.current_url() or args.url, mode=args.mode)

    result = page_flow.request_store(ops, args)
    problem = page_flow.store_selection_problem(
        args, result["store_before"], result["store_after"])
    if problem:
        logger.error("%s", problem)
        logger.error("       The site answered the store request with "
                     "HTTP %s.", result.get("store_request_status"))
        return EXIT_BAD_USAGE
    logger.info("The site moved this session from store %s to %s.",
                result["store_before"], result["store_after"])
    args._store_result = result
    return None


def _run_store_lookup(session, args, pool) -> int:
    """One store lookup, start to finish. Returns the exit code.

    Kept apart from the listing path on purpose: there is no pagination,
    no DOM to read, no tiles to wait for and no price to confirm, so
    routing it through `fetch_one_page` would mean five branches that are
    False for every store run (§"a branch keyed on a mode that does not
    exist" — four dead branches per engine is a real cost this family has
    already paid).
    """
    ops = getattr(session, "_ops", None)
    if ops is None:
        ops = session._ops = PlaywrightOps(session)

    landing = page_flow.PageOutcome(page_num=1, url=args.url)
    html, _status, state = page_flow.land(ops, args, landing)
    if page_flow.counts_as_blocked(state):
        logger.error("The landing page was refused, so the locator cannot "
                     "be asked: a store lookup is a request made from "
                     "inside a loaded page. %s",
                     page_flow.block_advice(
                         html, headless=bool(getattr(args, "headless", False)),
                         has_pool=bool(pool and len(pool) > 1)))
        return finish_run(
            [], args.out, args.format, args.allow_empty, blocked=True,
            stop_reason="blocked", pages_requested=1, pages_completed=0,
            start_url=args.url, final_url=ops.current_url() or args.url,
            mode="stores", extra=_store_extra(args, None))

    result = page_flow.fetch_stores(ops, args)
    rows = result.products
    return finish_run(
        rows, args.out, args.format, args.allow_empty,
        blocked=False,
        stop_reason=("completed" if result.ok else
                     (result.state or "api_error")),
        pages_requested=1, pages_completed=1 if result.ok else 0,
        pages_failed=None if result.ok else [1],
        start_url=args.url, final_url=result.final_url or args.url,
        mode="stores", extra=_store_extra(args, rows))


def _store_extra(args, rows) -> dict:
    """What the sidecar records about a store lookup.

    The QUESTION as well as the answer, because two store files are only
    comparable if they asked the same thing — the same reason `listing`
    is in a listing run's sidecar.
    """
    extra = {
        "lookup": ("store-no" if args.store_id else
                   "postcode" if args.postcode else
                   "suburb" if args.suburb else "latlong"),
        "postcode": args.postcode,
        "suburb": args.suburb,
        "near": args.near,
        "store_no": args.store_id,
    }
    if rows:
        extra["store_ids"] = sorted({r.sku for r in rows if r.sku})
        extra["states"] = sorted({r.state for r in rows if r.state})
    return extra


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
            # A store lookup is one GET against the site's own locator, so
            # it takes none of what follows: no pagination, no tiles, no
            # DOM price confirmation.
            if args.mode == "stores":
                return _run_store_lookup(session, args, pool)

            if args.store_id or args.postcode:
                refused = _honour_store_request(session, args, pool)
                if refused is not None:
                    return refused

            # Page 1 is always fetched on its own, because its answer is what
            # decides whether the rest is worth asking for.
            first = _fetch_one_page(session, args, pool, 1, args.url)
            outcomes.append(first)

            if not first.ok:
                stop_reason = _stop_reason_for(first)
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
                    specs = [(n, args.url)
                             for n in page_flow.planned_pages(args.pages)]
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
                            stop_reason = _stop_reason_for(out)
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
        # WHICH listing, so `diff_runs.py` can refuse two runs of different
        # ones. `--category` was parsed, defaulted from the URL, and then
        # read by nothing at all — a flag in the family's own contract list
        # that did not reach a row, the sidecar or the log.
        "listing": args.category,
        # The store every price in this file belongs to. One value per run
        # in practice; recorded as the set actually seen, so a run that
        # somehow spanned two says so rather than implying one.
        "store_ids": sorted({r.store_id for r in all_rows if r.store_id}),
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
    p.add_argument("--mode", choices=["search", "category", "stores"],
                   default=None,
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
                   help="How many EXTRA attempts to make when a page comes "
                        "back refused, each on the next exit (default 4, "
                        "and only with a pool). It is a count of RETRIES, "
                        "not of attempts: 4 means up to 5 landings in "
                        "total, and 0 means try once and report it blocked.")
    # ---- choosing a store ------------------------------------------
    # What the site permits, measured 2026-10-08: the LOOKUP is open to
    # anyone and SETTING the store that prices a listing is not. See
    # `page_flow.store_selection_problem` and product_parser's store
    # section for the endpoint-by-endpoint table.
    p.add_argument("--postcode", default=None, metavar="NNNN",
                   help="Australian postcode, four digits. With --mode "
                        "stores it lists the stores near it. On a listing "
                        "run it asks for that store's prices — which an "
                        "anonymous session cannot have, so the run is "
                        "REFUSED with the reason rather than quietly "
                        "returning another store's prices.")
    p.add_argument("--suburb", default=None, metavar="NAME",
                   help="Suburb name, for --mode stores. Returns the "
                        "suburb's stores with no distance, because the "
                        "site has no point to measure from.")
    p.add_argument("--near", default=None, metavar="LAT,LONG",
                   help="Coordinates, for --mode stores — the lookup the "
                        "site's own store-locator page uses. Returns more "
                        "stores than a postcode does (30 against 10, "
                        "measured) and every one with a distance. A "
                        "southern latitude is negative, so either form "
                        "works: --near=-37.8136,144.9631 or "
                        "--near -37.8136,144.9631.")
    p.add_argument("--store-id", default=None, metavar="N",
                   help="A Woolworths store NUMBER (StoreNo, e.g. 3304). "
                        "With --mode stores it fetches that one store. "
                        "Note it is not the FulfilmentStoreId a listing "
                        "row carries: the two are different numbering "
                        "schemes and the row spells out which it is.")
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
    args = p.parse_args(page_flow.join_near_argument(sys.argv[1:]))
    # Fill --twocaptcha-key / --cdp-endpoint / --proxy / --url from the
    # environment or .env when the flag was not given. An explicit flag wins.
    env_config.apply(args)
    if not args.url and not (args.mode == "stores" or any(
            getattr(args, n, None)
            for n in ("postcode", "suburb", "near", "store_id"))):
        # A store lookup needs no listing URL — it supplies its own landing
        # page below — so this is checked after the store branch has had a
        # chance to claim the run, not before it.
        p.error("no --url given, and WOOLWORTHS_URL is not set in the "
                "environment or in .env.")
    # ---- store lookups do not read a listing ---------------------------
    # A store lookup is a GET against the site's own locator, so there is
    # no listing URL to validate and none to pass. A page is still needed
    # to fetch FROM — the API is asked from inside a loaded page, which is
    # where the Akamai session comes from — so a landing page is supplied
    # and the user does not have to think about it.
    _store_lookup = any(getattr(args, n, None)
                        for n in ("postcode", "suburb", "near", "store_id"))
    if args.mode == "stores" or (_store_lookup and args.url is None):
        args.mode = "stores"
        if args.near:
            parts = [x.strip() for x in str(args.near).split(",")]
            if len(parts) != 2 or not all(parts):
                p.error(f"--near takes LAT,LONG — two numbers separated by "
                        f"a comma, e.g. --near -37.8136,144.9631. Got "
                        f"{args.near!r}.")
            try:
                float(parts[0]); float(parts[1])
            except ValueError:
                p.error(f"--near takes two NUMBERS, got {args.near!r}.")
            args._near_lat, args._near_long = parts
        problem = page_flow.store_lookup_problem(args)
        if problem:
            p.error(problem)
        args.url = args.url or STORE_LANDING_URL
        args.category = args.category or (
            args.postcode or args.suburb or args.near or args.store_id)
        args.page_size = PAGE_SIZE
        if args.pages != 1:
            logger.info("--pages is ignored for a store lookup: the "
                        "locator answers in one call and has no pages.")
            args.pages = 1
        return args
    
    if args.url is None:
        p.error("--url is required for a listing run. For stores, pass "
                "--mode stores with --postcode, --suburb, --near or "
                "--store-id and no URL.")
    
    # ---- a store asked for on a LISTING run ----------------------------
    # Not silently ignored and not silently honoured. The attempt is made
    # at run time in `scrape`, against the live site, so what the reader
    # gets is the status the site returned today rather than a sentence
    # written here months ago (section 13).
    if args.suburb or args.near:
        p.error("--suburb and --near only apply to --mode stores. A "
                "listing is priced by a store, not by an area.")

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
