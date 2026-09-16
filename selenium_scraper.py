#!/usr/bin/env python3
"""woolworths-scraper — Selenium edition

A parity engine. It must agree with playwright_scraper.py and
puppeteer_scraper.py on exit codes, run status, and whether a run crashes or
spends money — the shared modules (`product_parser`, `page_flow`,
`output_writer`, `proxy_pool`, `captcha_solver`) are what keep it honest, and
this file holds only "how to ask Selenium".

Read playwright_scraper.py's docstring for what is different about this SITE.
Four things are different about this ENGINE:

* **It drives the installed Chrome, and there is nothing to choose.**
  chromedriver has no bundled browser, so there is no browser-channel flag
  here.

* **It has to walk the shadow DOM by hand.** The product grid renders into
  open shadow roots on `<wc-product-tile>` elements. Playwright's CSS engine
  pierces those; Selenium's does not, so `_read_tiles` does the walk in page
  script. Same operation, three spellings — which is why `page_flow` names
  operations rather than passing JavaScript across the boundary (§1).

* **It counts refused API responses out of Chrome's performance log**,
  because Selenium has no response listener. If it always answered 0 it
  would report `complete` where its twins report `partial` on the identical
  run (§6). The log is enabled at driver creation and cannot be turned on
  later.

* **It cannot authenticate a proxy, and it cannot use an authenticated CDP
  endpoint.** `--proxy-server` takes an address with nowhere to put a
  password, and chromedriver's `debuggerAddress` takes a bare `host:port`.
  Both are warned about rather than silently half-done; use the Playwright
  or pyppeteer engine for either.

USAGE

    pip install -r requirements.txt -r requirements-selenium.txt

    python3 selenium_scraper.py \\
        --url "https://www.woolworths.com.au/shop/search/products?searchTerm=milk" \\
        --pages 3 --format json
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
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS)
from product_parser import (API_CATEGORIES_PATH, API_CATEGORY_PATH,
                            API_SEARCH_PATH, BASE_URL, CANONICAL_HOST,
                            PAGE_CAP, PAGE_SIZE, SELECTORS, api_request_for,
                            asset_reference_count, category_id_for_slug,
                            category_slug_from_url, detect_block_marker,
                            detect_page_state, is_unauthorised_redirect,
                            is_woolworths_host, iter_category_nodes,
                            mode_for_url, organic_count, overlay_dom_prices,
                            products_from_payload, search_term_from_url,
                            source_of, total_count, unsupported_reason)
from output_writer import dedupe_by_key, finish_run
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, split_credentials,
                        mask, ROTATE_MODES, ProxyError)
import env_config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("selenium_scraper")

# Explicit, because a driver that stops answering otherwise hangs the run:
# "every remote call is bounded" applies to this engine too.
PAGE_LOAD_TIMEOUT = 60
SCRIPT_TIMEOUT = 30

# Kept identical to the Playwright engine's, and the smoke suite asserts it:
# a floor that differed between engines would mean one of them warning about
# a page its twin called healthy.
FIELD_FLOOR = 90
DOM_CONFIRM_FLOOR = 90

# The one launch flag this site requires. Chrome sets `navigator.webdriver`
# true under automation, and Woolworths' page script reads it: when it is
# true the SPA navigates ITSELF to /unauthorisederror and the grid never
# renders — while the document still answers 200 and the API still answers
# 200 with real products. So without this a run returns the right rows,
# reports success, and silently loses the DOM cross-check (§16).
#
# Measured on the Playwright engine, three runs each way on 2026-09-16:
# without it 3/3 redirected and 0 tiles; with it 3/3 stayed and 36-39 tiles.
# Named here so the smoke suite can assert all three engines pass their own
# spelling of it.
LAUNCH_ARGS = ("--disable-blink-features=AutomationControlled",)


_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced.

    Global, not first-match: an error can repeat an endpoint several times,
    and a masker that handles one occurrence prints the password for the
    rest while looking like it works.
    """
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version."""
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version} Safari/537.36")


def _cdp_host_port(endpoint: str) -> str:
    """`host:port` out of a CDP endpoint, refusing one with credentials.

    Selenium cannot use an authenticated remote CDP endpoint at all, and this
    is the one place to say so. Playwright's `connect_over_cdp` and
    Puppeteer's `browserWSEndpoint` take a full `ws://user:pass@host:port`
    and authenticate on the WebSocket upgrade; chromedriver's
    `debuggerAddress` takes a bare `host:port` with nowhere to put a
    password. Silently stripping the credentials would produce a connection
    refusal a long way from its cause.
    """
    parts = urlsplit(endpoint)
    if parts.username or parts.password:
        logger.error(
            "This --cdp-endpoint carries credentials, and Selenium cannot "
            "send them: chromedriver's debuggerAddress is a bare host:port. "
            "The 2Captcha Scraping Browser API endpoint is authenticated, so "
            "it cannot be used from this engine — run playwright_scraper.py "
            "or puppeteer_scraper.py for it. Endpoint: %s",
            _mask_credentials(endpoint))
        sys.exit(2)
    host = parts.hostname or endpoint
    port = f":{parts.port}" if parts.port else ""
    return f"{host}{port}"


@dataclass
class PageOutcome:
    """What one page produced. Mirrors the Playwright engine's field for field."""
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    # What the site says the whole listing holds. NOT stable across pages on
    # this site (69 on page 1 of one search, 0 on page 50), so it is recorded
    # beside what the run read and never used as a loop bound.
    stated_total: Optional[int] = None
    # None, always: Woolworths numbers no rank on a listing, so there is no
    # arithmetic gap to compute, and an unknown gap must not read the same as
    # a gap of zero (§8).
    gap: Optional[int] = None
    dom_confirm: Optional[dict] = None
    api_errors: int = 0
    unauthorised: bool = False

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


class _Session:
    """One Chrome driver, relaunchable onto a different exit.

    Same contract as the Playwright engine's _BrowserSession, including the
    rule that a rotation means a genuinely FRESH browser: cookies a bot
    manager issued against one exit, replayed from another, are a stronger
    signal than either address alone.
    """

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.remote = bool(args.cdp_endpoint)
        self.driver = None

    def open(self):
        options = Options()
        if self.remote:
            options.debugger_address = _cdp_host_port(self.args.cdp_endpoint)
            logger.info("Attaching to an existing browser at %s.",
                        options.debugger_address)
            # No UA, no proxy, no fingerprint on this path: the remote browser
            # brings its own, and stacking a second creates a contradiction
            # rather than better cover.
            self.driver = webdriver.Chrome(options=options)
            self._apply_timeouts()
            return self

        if self.args.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        # 1440x900 matches what the captures were taken at, so the tile
        # counts in the README mean the same thing here.
        options.add_argument("--window-size=1440,900")
        # See LAUNCH_ARGS: this is the flag that keeps Woolworths' SPA from
        # bouncing the browser to /unauthorisederror, and it is passed by all
        # three engines.
        for flag in LAUNCH_ARGS:
            options.add_argument(flag)

        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            options.add_argument(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))
            if credentials:
                logger.warning(
                    "This proxy has credentials and SELENIUM CANNOT SEND "
                    "THEM: --proxy-server accepts an address only, and there "
                    "is no Selenium equivalent of pyppeteer's "
                    "page.authenticate. They have been stripped, so requests "
                    "will go out unauthenticated and the exit will most "
                    "likely refuse them. Use playwright_scraper.py or "
                    "puppeteer_scraper.py for an authenticated proxy.")

        # Chrome's performance log, which is how this engine counts refused
        # API responses — see `_api_error_count`. It has to be
        # asked for at driver creation; there is no way to turn it on later.
        # Without it this engine would report `complete` where its twins
        # report `partial` on the identical run (§6).
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        self.driver = webdriver.Chrome(options=options)
        self._apply_timeouts()
        _watch_api(self)

        version = self.driver.capabilities.get("browserVersion", "")
        if version:
            try:
                self.driver.execute_cdp_cmd(
                    "Network.setUserAgentOverride",
                    {"userAgent": _chrome_ua(version)})
            except WebDriverException as e:
                logger.debug("Could not override the user agent: %s", e)

        if self.args.fingerprint:
            self._apply_fingerprint()
        return self

    def _apply_timeouts(self):
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.driver.set_script_timeout(SCRIPT_TIMEOUT)

    def _apply_fingerprint(self):
        # THROUGH THE SHARED HELPER, never by reaching into the response
        # shape here. This line dug the UA out of the response itself until a
        # live call to the API showed what it actually returns: the UA is at
        # `userAgent.userAgent` in the chromium format and at `data.ua` in
        # the raw one, and the key this engine asked for exists in NEITHER.
        # So `--fingerprint`
        # silently set no user agent at all and the browser kept its own —
        # which defeats the flag rather than breaking it, because the run
        # then presents a Windows fingerprint's screen, locale and timezone
        # over a local Chromium's UA. That is the identity MISMATCH the flag
        # exists to avoid (§16, where the same defect was live in four
        # sibling repos at once).
        from fingerprint_client import (get_fingerprint, fingerprint_user_agent,
                                        playwright_init_script)
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = fingerprint_user_agent(fp)
        script = playwright_init_script(fp)
        try:
            if ua:
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride",
                                            {"userAgent": ua})
            # The same patch script the Playwright engine installs on its
            # context. Shared deliberately: two engines applying different
            # halves of one fingerprint would be a contradiction of exactly
            # the kind a fingerprint is meant to avoid.
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument", {"source": script})
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except WebDriverException as e:
            logger.warning("Could not apply the fingerprint over CDP (%s) — "
                           "continuing without it.", e)

    def relaunch(self):
        if self.remote:
            return
        self.close()
        self.open()

    def close(self):
        try:
            if self.driver is not None:
                # quit(), not close(): close() ends one window and leaves the
                # driver process running, which on a per-page rotation would
                # leak a chromedriver per page.
                self.driver.quit()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during driver teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to Selenium
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here. Note the JS dialect: Selenium's
# execute_script runs a function BODY and needs an explicit `return`, unlike
# the `() => expr` both other engines take — which is exactly why page_flow
# names operations instead of passing JavaScript across the boundary (§1).
def _count(session, selector: str) -> int:
    try:
        return len(session.driver.find_elements(By.CSS_SELECTOR, selector))
    except WebDriverException as e:
        logger.debug("count(%s) failed: %s", selector, e)
        return 0


def _sleep(ms: int) -> None:
    time.sleep(ms / 1000.0)


# Chromium's own names for "the proxy is the problem, not the site". Kept
# identical to the other two engines', and the smoke suite asserts that,
# because the two want OPPOSITE responses: a timeout deserves another try at
# the same exit, a dead proxy a different one. Catching only the timeout is
# how a dead exit escaped as a traceback in a sibling repo (§8).
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH",
    "ERR_SOCKS_CONNECTION_FAILED",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one."""
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _content(session) -> Optional[str]:
    try:
        return session.driver.page_source
    except WebDriverException as e:
        logger.debug("page_source unavailable (page navigating?): %s", e)
        return None


# The site's own API, asked from inside the page. Note the DIALECT: Selenium's
# `execute_async_script` runs a function BODY, takes its arguments from
# `arguments[...]`, and signals completion by calling the callback Selenium
# appends as the LAST argument. Playwright and pyppeteer take an async arrow
# and await it. Three spellings of one operation, which is exactly why
# page_flow names the operation and each engine writes its own (§1).
_FETCH_API_JS = """
const path = arguments[0], body = arguments[1];
const done = arguments[arguments.length - 1];
const opt = (body === null)
    ? {headers: {'Accept': 'application/json'}}
    : {method: 'POST',
       headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
       body: JSON.stringify(body)};
fetch(path, opt)
    .then(function (r) { return r.text().then(function (t) {
        done({status: r.status, text: t}); }); })
    .catch(function (e) { done({status: 0, text: '' + e}); });
"""


def _fetch_api(session, path: str, body=None):
    """Ask Woolworths' own API from inside the page. (status, payload, text)

    From INSIDE the page, not with `requests`: the browser has just been
    issued an Akamai session, and a same-origin `fetch` carries its cookies
    and its TLS fingerprint for free.

    Returns the raw text alongside the parsed payload so a response that is
    not JSON can be classified rather than raising a decode error a long way
    from the cause.
    """
    import json as _json
    try:
        res = session.driver.execute_async_script(_FETCH_API_JS, path, body)
    except WebDriverException as e:
        logger.debug("api fetch failed: %s", e)
        return None, None, ""
    if not isinstance(res, dict):
        return None, None, ""
    status, text = res.get("status"), res.get("text") or ""
    payload = None
    if text:
        try:
            payload = _json.loads(text)
        except ValueError:
            payload = None
    return status, payload, text


# Reading the grid means reaching INTO open shadow roots, and Selenium's CSS
# engine does not pierce them the way Playwright's does — `find_elements`
# would return the custom elements and nothing inside them. So the walk is
# done in page script, as a function BODY with an explicit `return`.
_READ_TILES_JS = """
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
"""


def _read_tiles(session) -> List[dict]:
    """The rendered tiles, read through their open shadow roots.

    Each tile yields `{href, price_text, cup_text}` — the raw strings, with
    every judgement about what they MEAN left to
    `product_parser.overlay_dom_prices`, so all three engines hand the parser
    the same thing.

    A tile with no product link is skipped rather than guessed at: a quarter
    of the tiles on a category page belong to a "you might also like"
    carousel, and the stockcode in each tile's OWN link is what keeps one
    from being attributed to a grid row (§4).
    """
    try:
        return session.driver.execute_script(_READ_TILES_JS) or []
    except WebDriverException as e:
        # A tile read confirms a price the API already gave us. It must never
        # take a run down.
        logger.debug("tile read failed: %s", e)
        return []


# Every page of a listing arrives over the site's own API, and counting the
# refused ones is what separates a listing that ran out (COMPLETE) from one
# whose next page was refused (PARTIAL). The other two engines get this from
# a response listener, which Selenium has no equivalent of — so it is read
# out of Chrome's performance log instead.
#
# This is NOT a cosmetic difference. If this engine always answered 0, it
# would report `complete` where its twins report `partial` on the identical
# run, and that is precisely the drift the shared modules exist to prevent
# (§6). The log has to be ENABLED at driver creation; see `_Session.open`.
_API_PATH_MARKER = "/apis/ui/"


def _watch_api(session) -> None:
    """Reset the running count. The log itself is enabled on the driver."""
    session._api_errors = 0
    _api_error_count(session)   # drain anything already buffered


def _api_error_count(session) -> int:
    """How many `/apis/ui/` responses have come back non-200 this session.

    Chrome's performance log is drained on read — each `get_log` call returns
    only entries since the last one — so this accumulates rather than
    recounting, and callers take a difference across the window they care
    about.
    """
    import json as _json
    total = getattr(session, "_api_errors", 0)
    try:
        entries = session.driver.get_log("performance")
    except Exception:  # noqa: BLE001 — an absent log must never break a run
        return total
    for entry in entries:
        try:
            message = _json.loads(entry.get("message", "{}"))["message"]
            if message.get("method") != "Network.responseReceived":
                continue
            response = message["params"]["response"]
            if _API_PATH_MARKER in response.get("url", "") \
                    and int(response.get("status", 0)) != 200:
                total += 1
        except Exception:  # noqa: BLE001
            continue
    session._api_errors = total
    return total


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args, html: str = "") -> int:
    return page_flow.min_matches(args.mode)


def _current_url(session) -> str:
    try:
        return session.driver.current_url
    except WebDriverException:
        return ""


def _classify(session, html: str, status=None) -> str:
    # `status` is POSITIONAL and second. Two engines in a sibling repo passed
    # it as a keyword and both crashed on their first fetch (§17); this
    # repo's smoke suite binds every shared-module call in every engine
    # against the callee's real signature for that reason.
    return page_flow.classify(html, status, _current_url(session))


def _rows_from_payload(payload, args, page_num: int, data_source: str) -> List:
    """Rows for this page, always a list.

    `page_num` is threaded through rather than defaulted, because `position`
    restarts at 1 on every page (§18).
    """
    return products_from_payload(payload, page=page_num,
                                 data_source=data_source, host=CANONICAL_HOST)


def handle_captcha_if_present(session, args, allow_solve: bool = True) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Same contract and same reconciliation as the Playwright engine, including
    what it CANNOT reach: Woolworths' refusal is Akamai's EDGE DENIAL,
    which publishes no sitekey, so there is nothing for a solver to answer.
    `page_flow.STATE_POLICY` marks that state retry-but-do-not-solve and
    nothing is ever charged for it.
    """
    driver = session.driver
    html = _content(session)
    if html is None:
        return False

    already_rendered = _count(session, _ready_selector(args))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, _current_url(session))
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: driver.execute_script(f"return ({js})();"),
        page_url=_current_url(session))
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

    driver.execute_script(f"return ({INJECT_TOKEN_JS})(arguments[0]);", token)
    logger.info("Token injected. Reloading page to continue.")
    _sleep(1500)
    try:
        driver.refresh()
    except WebDriverException as e:
        logger.warning("Reload after the solve failed: %s", e)
    return True


def _confirm_with_dom(session, rows, page_num: int) -> Optional[dict]:
    """Confirm the API's prices against the rendered tiles.

    A CONFIRMATION, never a correction: where the two agree the row's
    `price_source` becomes `api+dom`, and where they disagree the row is left
    exactly as the API gave it and a warning names the sku (§4).

    Only for page 1, which is the page the browser is actually looking at —
    pages 2..N are fetched over the API without navigating.
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
            "on price, against a measured floor of %d%%. The rows are the "
            "API's and are unchanged.", share, DOM_CONFIRM_FLOOR)
    return {"tiles": len(tiles), "checked": checked, "confirmed": confirmed}


# Exceptions an API request can raise on this driver.
API_ERRORS = (WebDriverException,)


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

def _land(session, args, outcome):
    """Make sure the driver is ON the listing page. (html, status, state)

    Navigates only when it has to. Every page of a listing is fetched from
    inside ONE loaded page — the navigation exists to make Akamai issue a
    session, not to reach page N.

    Selenium reports no HTTP status, so `status` is always None here and
    `detect_page_state` falls through to the markers and the asset-host
    signal. That is the one place this engine has strictly less information
    than its twins, and it is why the positive asset-host check matters so
    much on this site: it answers correctly with no status at all (§8).
    """
    if getattr(session, "landed_url", None) == args.url:
        html = _content(session)
        if html:
            return html, None, _classify(session, html, None)
        session.landed_url = None

    for attempt in range(1, args.retries + 1):
        try:
            session.driver.get(args.url)
            break
        except WebDriverException as e:
            reason = _proxy_failure(e)
            if reason:
                logger.error("Exit failed: %s", reason)
                outcome.load_failed = True
                return None, None, "blocked"
            if attempt < args.retries:
                pause = args.retry_delay * (2 ** (attempt - 1))
                logger.warning("Could not load %s (attempt %d/%d: %s) — "
                               "retrying in %.1fs.", args.url, attempt,
                               args.retries, _mask_credentials(str(e))[:140],
                               pause)
                time.sleep(pause)
            else:
                outcome.load_failed = True
                return None, None, "blocked"

    html = _content(session) or ""
    state = _classify(session, html, None)
    if state != "blocked":
        session.landed_url = args.url
    return html, None, state


def _resolve_target(session, args, outcome):
    """What to ask the API for. (ok, term, category_id, slug)

    A category id is OPAQUE: `bakery` is `1_DEB537E`. Sending the slug
    instead returns 200 with zero products and `Success: true`, which reads
    exactly like a real empty category — so an unresolved slug is refused
    here rather than turned into a run that reports success on nothing.

    Cached on the session: the tree is ~2,700 nodes and does not change
    between pages of one run.
    """
    if args.mode == "search":
        term = search_term_from_url(args.url)
        if not term:
            logger.error("No searchTerm parameter in %s.", args.url)
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
            "code: the slug was looked up in Woolworths' own tree (%s, %d "
            "nodes) and is not in it. Check the URL in a browser.",
            slug, API_CATEGORIES_PATH,
            sum(1 for _ in iter_category_nodes(tree)))
        outcome.load_failed = True
        return False, None, None, None

    cached[slug] = node_id
    logger.info("Category %r resolves to node id %s.", slug, node_id)
    return True, None, node_id, slug


def _fetch_one_page(session, args, pool, page_num: int,
                    url=None) -> PageOutcome:
    """Fetch one page of the listing and parse it.

    `url` is accepted for the family's signature and is the LISTING's
    address, the same for every page: a page is a number in a request body,
    not an address.
    """
    outcome = PageOutcome(page_num=page_num, url=url or args.url)
    has_pool = bool(pool and len(pool) > 1)
    block_retries = page_flow.block_retries(has_pool)

    html = state = None
    for block_attempt in range(block_retries + 1):
        outcome.load_failed = False
        html, status, state = _land(session, args, outcome)
        # The POLICY decides whether another fetch could change this
        # answer, rather than each engine deciding for itself (section 1).
        if not page_flow.should_retry(state) and not outcome.load_failed:
            break
        if block_attempt < block_retries:
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

    # Detection runs on every page, whatever the state (section 8);
    # whether a SOLVE may be bought is page_flow's call.
    try:
        if args.solve_captcha != "never":
            if handle_captcha_if_present(session, args,
                                         allow_solve=page_flow.should_solve(state)):
                html = _content(session) or html
                state = _classify(session, html, None)
    except WebDriverException as e:
        logger.debug("captcha check skipped: %s", e)

    outcome.state = state

    if outcome.load_failed and state != "blocked":
        outcome.final_url = _current_url(session) or args.url
        return outcome

    if page_flow.counts_as_blocked(state):
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        try:
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.error(
            "The site did not serve this request — %d bytes, %d reference(s) "
            "to the site's own asset host, saved to %s. This is exit 3, "
            "distinct from a genuinely empty result (exit 4).",
            len(html or ""), asset_reference_count(html or ""), debug_html)
        logger.error("%s", page_flow.block_advice(
            html, headless=bool(getattr(args, "headless", False)),
            has_pool=has_pool))
        outcome.blocked_by = (detect_block_marker(html or "")
                              or ("no-response" if not html else "akamai"))
        outcome.final_url = _current_url(session) or args.url
        return outcome

    if page_num == 1:
        found = page_flow.wait_for_tiles(
            lambda sel: _count(session, sel), _sleep,
            page_flow.ready_selector(args.mode), _min_matches(args, html))
        logger.info("%d tile(s) had painted when the API was asked.", found)
        if is_unauthorised_redirect(_current_url(session)):
            outcome.unauthorised = True
            logger.warning(
                "The app navigated itself to %s — it has decided this "
                "browser is automated, and the product grid will not render. "
                "The rows below still come from the site's own API and are "
                "correct, but the DOM price cross-check is impossible. This "
                "engine passes %s to prevent it.",
                _current_url(session), LAUNCH_ARGS[0])

    ok, term, category_id, slug = _resolve_target(session, args, outcome)
    if not ok:
        outcome.final_url = _current_url(session) or args.url
        return outcome

    path, body, data_source = api_request_for(
        args.mode, term=term, category_id=category_id, slug=slug,
        page=page_num, page_size=args.page_size)

    errors_before = _api_error_count(session)
    status, payload, text = _fetch_api_with_retries(
        session, args, path, body, page_num)

    if status != 200 or payload is None:
        logger.error(
            "POST %s for page %d answered HTTP %s with %d byte(s) that %s "
            "JSON. This is not the end of the listing — the run is reported "
            "as PARTIAL (exit 6) rather than complete. Raise --delay (it is "
            "%.1fs now), or spread the load with --proxy-file.",
            path, page_num, status, len(text or ""),
            "are not" if payload is None else "is", args.delay)
        outcome.load_failed = True
        outcome.state = "api_error"
        outcome.final_url = _current_url(session)
        return outcome

    if args.dump_html:
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
                "against a measured floor of %d%%. Re-run with --dump-html "
                "and look at the .api.json.", share, page_num, FIELD_FLOOR)
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
    outcome.final_url = _current_url(session)
    return outcome


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    stop_reason = "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None

    if args.concurrency > 1:
        # Refused rather than silently ignored, and with the reason that is
        # actually true HERE: the pages are addressable, so concurrency is
        # meaningful on this site — it is this ENGINE that does not implement
        # it. Saying "not supported by this engine" keeps the distinction a
        # reader needs, because playwright_scraper.py does support it.
        logger.warning(
            "--concurrency %d is not implemented in this engine. The pages "
            "ARE independently addressable on this site, so concurrency is "
            "meaningful — use playwright_scraper.py for it. Continuing with "
            "one worker; the rows are identical either way.",
            args.concurrency)

    session = _Session(args, pool).open()
    try:
        first = _fetch_one_page(session, args, pool, 1, args.url)
        outcomes.append(first)

        if not first.ok:
            stop_reason = ("page_load_timeout" if first.load_failed
                           else f"blocked_{first.blocked_by}")
            blocked = first.blocked_by is not None
        else:
            seen_keys.update(p.sku for p in first.products if p.sku is not None)
            if page_flow.organic_count(first.products) == 0 and args.pages > 1:
                logger.info("Page 1 carried no organic rows — not asking for "
                            "more pages.")
                stop_reason = "pagination_exhausted"
            else:
                for page_num in range(2, args.pages + 1):
                    if page_flow.page_cap_reached(page_num):
                        stop_reason = "page_cap"
                        logger.warning("Stopping at the %d-page cap.", PAGE_CAP)
                        break
                    time.sleep(args.delay)
                    out = _fetch_one_page(session, args, pool, page_num, args.url)
                    outcomes.append(out)
                    if not out.ok:
                        stop_reason = ("page_load_timeout" if out.load_failed
                                       else f"blocked_{out.blocked_by}")
                        blocked = blocked or out.blocked_by is not None
                        break
                    # ORGANIC rows, not rows: past its last real page this
                    # API keeps answering 200 with nothing but the same
                    # promoted ads page 1 carried (§7).
                    turn = page_flow.advance_page(out.products, seen_keys)
                    seen_keys.update(p.sku for p in out.products
                                     if p.sku is not None)
                    if turn != page_flow.ADVANCED:
                        logger.info(
                            "Page %d added no organic product this run had "
                            "not already seen (%s) — the listing has ended. "
                            "Stopping with %d page(s) fetched rather than the "
                            "%d asked for.", page_num,
                            "every row on it was a promoted ad"
                            if turn == page_flow.ADS_ONLY else "no new rows",
                            page_num, args.pages)
                        stop_reason = "pagination_exhausted"
                        break
    finally:
        session.close()

    # Merge in PAGE ORDER, not arrival order (§8).
    all_rows = []
    seen = set()
    for outcome in sorted(outcomes, key=lambda o: o.page_num):
        all_rows.extend(dedupe_by_key(outcome.products, seen, "sku"))

    pages_completed = sum(1 for o in outcomes if o.ok)
    failed_pages = sorted(o.page_num for o in outcomes if not o.ok)
    sponsored = sum(1 for r in all_rows if r.is_sponsored)
    if all_rows:
        logger.info("%d row(s) after deduping on sku, of which %d (%.0f%%) "
                    "are promoted ads.", len(all_rows), sponsored,
                    100.0 * sponsored / len(all_rows))

    stated = next((o.stated_total for o in outcomes
                   if o.stated_total is not None), None)
    api_errors = sum(o.api_errors or 0 for o in outcomes)
    if api_errors:
        logger.warning("%d API response(s) during this run were not 200. A "
                       "page that answered 200 while its API call was refused "
                       "is a PARTIAL run, not a short listing.", api_errors)

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
                      extra={"mode": args.mode,
                             "stated_total": stated,
                             "sponsored_rows": sponsored,
                             "api_errors": api_errors,
                             "unauthorised_redirect": any(o.unauthorised
                                                          for o in outcomes),
                             "dom_confirm": next((o.dom_confirm for o in outcomes
                                                  if o.dom_confirm), None)})


def parse_args():
    p = argparse.ArgumentParser(
        description="Woolworths Online product scraper (Selenium edition)")
    p.add_argument("--url", default=None,
                   help="A Woolworths Online URL: a search "
                        "(/shop/search/products?searchTerm={term}) or a "
                        "category (/shop/browse/{slug}, child nodes "
                        "included). Required, unless WOOLWORTHS_URL is set in "
                        "the environment or in .env.")
    p.add_argument("--mode", choices=["search", "category"], default=None,
                   help="Which view the URL is. Inferred from the URL by "
                        "default; passing one that disagrees is an error "
                        "rather than an override.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Filled from the URL "
                        "by default — the search term or the category slug.")
    p.add_argument("--pages", type=int, default=1,
                   help=f"Number of pages to walk (default 1, cap {PAGE_CAP}). "
                        f"The walk stops early when a page adds no ORGANIC "
                        f"product it has not already seen — never when a page "
                        f"comes back empty, because past its last real page "
                        f"this listing keeps answering 200 with nothing but "
                        f"promoted ads.")
    p.add_argument("--delay", type=float, default=2.0,
                   help="Delay between pages, seconds (default 2.0).")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for family compatibility and NOT "
                        "IMPLEMENTED in this engine. The pages are "
                        "independently addressable on this site, so "
                        "concurrency is meaningful — use "
                        "playwright_scraper.py for it.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3).")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry (default 2.0); it "
                        "doubles on each subsequent attempt.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="json",
                   help="Output format (default json).")
    p.add_argument("--out", default="woolworths_products",
                   help="Output filename prefix (default "
                        "woolworths_products).")
    p.add_argument("--locale", default="en-AU",
                   help="Browser locale (default en-AU).")
    p.add_argument("--proxy", default=None,
                   help="Single proxy URL. NOTE: Selenium cannot send proxy "
                        "credentials — --proxy-server takes an address only, "
                        "with nowhere to put a password. A user:pass URL has "
                        "its credentials stripped and you are warned.")
    p.add_argument("--proxy-file", default=None, metavar="PATH",
                   help="File of proxy URLs, one per line, used as a pool.")
    p.add_argument("--proxy-rotate", choices=ROTATE_MODES, default="per-run",
                   help="When to move to the next exit (default per-run). A "
                        "rotation always relaunches the browser.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool before use.")
    p.add_argument("--proxy-block-retries", type=int, default=4, metavar="N",
                   help="How many exits to try when a page comes back "
                        "refused (default 4, and only with a pool).")
    p.add_argument("--twocaptcha-key", default=None,
                   help="2Captcha API key. Prefer TWOCAPTCHA_KEY in .env.")
    p.add_argument("--captcha-api", choices=["v1", "v2"], default="v2",
                   help="Which 2Captcha API to use (default v2).")
    p.add_argument("--solve-captcha", choices=["never", "when-blocked", "always"],
                   default="when-blocked",
                   help="When to spend money on a solve (default "
                        "when-blocked). Nothing has yet been measured to "
                        "solve on this site: Akamai's refusal here carries no "
                        "widget of any kind, so no solve is attempted. That "
                        "is a statement about the page, not the product.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a device fingerprint from 2Captcha and apply "
                        "it. Ignored with --cdp-endpoint.")
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag: Windows, Microsoft Windows or "
                        "Android. NOT a list — Chrome, Desktop and Mobile are "
                        "each rejected by the API with 400.")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Attach to an already-running browser, host:port. "
                        "NOTE: chromedriver's debuggerAddress takes a bare "
                        "host:port with nowhere for a password, so an "
                        "AUTHENTICATED endpoint (the Scraping Browser API) "
                        "cannot be used from this engine — use "
                        "playwright_scraper.py or puppeteer_scraper.py.")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write an empty file when a run finds nothing. Off by "
                        "default.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save what the parser is given, on success as well as "
                        "failure. Writes TWO files per page: the document, "
                        "and `.api.json` — the payload the rows actually come "
                        "from, which is the one to read.")
    # HEADFUL by default. Measured on this site 2026-09-16 from one
    # datacentre address, four URLs each way: headful served 4/4, headless
    # refused 4/4 with Akamai's 403.
    p.add_argument("--headful", dest="headless", action="store_false",
                   default=False,
                   help="Run with a real browser window. THE DEFAULT, and on "
                        "this site the difference between data and a 403: "
                        "4/4 served headful against 0/4 headless.")
    p.add_argument("--headless", dest="headless", action="store_true",
                   help="Run headless. Measured 0/4 served on this site from "
                        "a datacentre address. Kept for a machine with no "
                        "display, but expect exit 3.")
    args = p.parse_args()
    env_config.apply(args)
    if not args.url:
        p.error("no --url given, and WOOLWORTHS_URL is not set in the "
                "environment or in .env.")
    why = unsupported_reason(args.url)
    if why:
        p.error(why)

    kind = mode_for_url(args.url)
    if args.mode is None:
        args.mode = kind
        logger.info("Reading %s as a %s listing.", args.url, args.mode)
    elif args.mode != kind:
        p.error(f"--mode {args.mode} does not match {args.url!r}, which is a "
                f"{kind} page. The mode follows the URL on this site.")

    if args.category is None:
        args.category = (search_term_from_url(args.url) if kind == "search"
                         else category_slug_from_url(args.url))

    # Not a flag: the site's own UI asks for 36 and so does this. A request
    # that does not look like the UI's is the kind of difference a bot
    # manager scores on.
    args.page_size = PAGE_SIZE

    if args.pages > PAGE_CAP:
        logger.warning("--pages %d is above this scraper's %d-page cap; it "
                       "will stop there.", args.pages, PAGE_CAP)
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key.")
        sys.exit(2)
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
