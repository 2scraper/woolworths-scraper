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
from output_writer import dedupe_by_key, finish_run, EXIT_BAD_USAGE
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, split_credentials,
                        mask, ROTATE_MODES, ProxyError)
import env_config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("selenium_scraper")

# One definition each, in page_flow, shared by the three engines (§27.5),
# with the measurements behind them written down there.
PageOutcome = page_flow.PageOutcome
# Where a store lookup lands before asking the locator. Any page the site
# serves will do — the navigation exists to be issued an Akamai session,
# not to be read — and the home page is the cheapest one that is always
# there. Measured: the locator answers from it exactly as it does from a
# category page.
STORE_LANDING_URL = "https://www.woolworths.com.au/"

FIELD_FLOOR = page_flow.FIELD_FLOOR
DOM_CONFIRM_FLOOR = page_flow.DOM_CONFIRM_FLOOR
_mask_credentials = page_flow.mask_credentials

# Explicit, because a driver that stops answering otherwise hangs the run:
# "every remote call is bounded" applies to this engine too.
PAGE_LOAD_TIMEOUT = 60
SCRIPT_TIMEOUT = 30

# Kept identical to the Playwright engine's, and the smoke suite asserts it:
# a floor that differed between engines would mean one of them warning about
# a page its twin called healthy.

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
        # Which URL this session is currently landed on, or None. Declared
        # here rather than created by the first assignment from outside:
        # the shared loop reads it through its engine's ops object, and an
        # attribute that only exists once something has written it is one
        # rename away from an AttributeError nothing offline can see.
        self.landed_url = None

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


def handle_captcha_if_present(session, args, allow_solve: bool = True,
                              budget=None) -> bool:
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

    driver.execute_script(f"return ({INJECT_TOKEN_JS})(arguments[0]);", token)
    logger.info("Token injected. Reloading page to continue.")
    _sleep(1500)
    try:
        driver.refresh()
    except WebDriverException as e:
        logger.warning("Reload after the solve failed: %s", e)
    return True


# Exceptions an API request can raise on this driver.
API_ERRORS = (WebDriverException,)


# ===========================================================================
# The driver primitives the shared loop asks for
# ===========================================================================
# `page_flow.fetch_one_page` is ONE implementation for all three engines
# (§27.5); this class is the only part of it that is Selenium's. Every
# method is either a driver call or a two-line adapter, and no JavaScript
# crosses the boundary in either direction — the shared module names the
# OPERATION and this spells it in Selenium's dialect (§1).
#
# `smoke_test.py` derives the required method set from page_flow's own AST
# rather than from a hand-written list, so the day the loop reaches for a
# new operation, every engine missing it fails by name.
class SeleniumOps:
    """Selenium's half of the page loop."""

    # The exception types the shared loop catches around a driver call.
    driver_errors = (WebDriverException,)
    # Named in the one warning about the app bouncing us to
    # /unauthorisederror, so the reader is told which flag was supposed to
    # prevent it.
    launch_arg_hint = LAUNCH_ARGS[0]

    def __init__(self, session):
        self.session = session
        # Cache for the category tree, filled by page_flow.resolve_target.
        self.category_ids: dict = {}

    def is_live(self) -> bool:
        return getattr(self.session, "driver", None) is not None

    def is_landed(self, url: str) -> bool:
        return getattr(self.session, "landed_url", None) == url

    def set_landed(self, url: str) -> None:
        self.session.landed_url = url

    def clear_landing(self) -> None:
        self.session.landed_url = None

    def current_url(self):
        return _current_url(self.session)

    def goto(self, url: str, timeout_ms: int):
        # Selenium reports no HTTP status at all, so this returns None and
        # `detect_page_state` falls through to the markers and the
        # asset-host signal. That is the one place this engine has strictly
        # less information than its twins, and it is why the positive
        # asset-host check matters so much on this site: it answers
        # correctly with no status (§8). `timeout_ms` is set on the driver
        # at launch, which is where Selenium takes it.
        self.session.driver.get(url)
        return None

    def document_text(self):
        return _content(self.session)

    def classify(self, html, status=None) -> str:
        return _classify(self.session, html or "", status)

    def count(self, selector: str) -> int:
        return _count(self.session, selector)

    def wait_ms(self, ms: int) -> None:
        _sleep(ms)

    def screenshot(self, path: str) -> None:
        self.session.driver.save_screenshot(path)

    def read_tiles(self):
        return _read_tiles(self.session)

    def fetch_api(self, path: str, body=None):
        return _fetch_api(self.session, path, body)

    def api_error_count(self) -> int:
        return _api_error_count(self.session)

    def rows_from_payload(self, payload, args, page_num: int, data_source):
        return _rows_from_payload(payload, args, page_num, data_source)

    def proxy_failure(self, exc):
        return _proxy_failure(exc)

    def relaunch(self) -> None:
        self.session.relaunch()

    def handle_captcha(self, args, allow_solve: bool, budget) -> bool:
        return handle_captcha_if_present(self.session, args,
                                         allow_solve=allow_solve,
                                         budget=budget)


def _fetch_one_page(session, args, pool, page_num: int, url=None):
    """Thin wrapper over the shared loop, kept for the family's signature."""
    ops = getattr(session, "_ops", None)
    if ops is None:
        ops = session._ops = SeleniumOps(session)
    return page_flow.fetch_one_page(ops, args, pool, page_num, url)

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
        ops = session._ops = SeleniumOps(session)

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
        ops = session._ops = SeleniumOps(session)

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
    if args.mode == "stores":
        try:
            return _run_store_lookup(session, args, pool)
        finally:
            session.close()

    if args.store_id or args.postcode:
        refused = _honour_store_request(session, args, pool)
        if refused is not None:
            session.close()
            return refused

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
    p.add_argument("--mode", choices=["search", "category", "stores"],
                   default=None,
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
    args = p.parse_args(page_flow.join_near_argument(sys.argv[1:]))
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
