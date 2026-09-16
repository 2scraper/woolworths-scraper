#!/usr/bin/env python3
"""woolworths-scraper — pyppeteer edition

A parity engine. It must agree with playwright_scraper.py and
selenium_scraper.py on exit codes, run status, and whether a run crashes or
spends money — the shared modules (`product_parser`, `page_flow`,
`output_writer`, `proxy_pool`, `captcha_solver`) are what keep it honest, and
this file holds only "how to ask pyppeteer".

Read playwright_scraper.py's docstring for what is different about this SITE.
Three things are different about this ENGINE:

* **pyppeteer is async, and page_flow is not.** `_AsyncBridge` runs the
  coroutines on a private event loop and hands back plain values, so the
  shared policy module stays synchronous and single. The bridge also gives
  every call an explicit timeout, which pyppeteer's own API does not.

* **It CAN authenticate a proxy, and Selenium cannot.** Credentials go
  through `page.authenticate()` rather than onto the command line, where
  `--proxy-server=user:pass@host` would be readable by anything that can run
  `ps` (§8).

* **pyppeteer is effectively unmaintained** and its own README points at
  Playwright. It is here for parity; prefer playwright_scraper.py.

USAGE

    pip install -r requirements.txt -r requirements-puppeteer.txt

    python3 puppeteer_scraper.py \\
        --url "https://www.woolworths.com.au/shop/search/products?searchTerm=milk" \\
        --pages 3 --format json
"""

import argparse
import asyncio
import concurrent.futures
import json
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from pyppeteer import launch
from pyppeteer.launcher import connect
from pyppeteer.errors import PyppeteerError, TimeoutError as PPTimeout

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
logger = logging.getLogger("puppeteer_scraper")

FIELD_FLOOR = 90
DOM_CONFIRM_FLOOR = 90

# The one launch flag this site requires. Chrome sets `navigator.webdriver`
# true under automation and Woolworths' page script reads it: when true the
# SPA navigates ITSELF to /unauthorisederror and the grid never renders —
# while the document still answers 200 and the API still answers 200 with
# real products, so a run without it returns the right rows, reports success,
# and silently loses the DOM cross-check (§16). Measured three runs each way
# on 2026-09-16: without it 3/3 redirected, with it 3/3 stayed.
LAUNCH_ARGS = ("--disable-blink-features=AutomationControlled",)

# Chromium's own names for "the proxy is the problem, not the site". Kept
# identical across the three engines, because the two cases want OPPOSITE
# responses: a timeout deserves another try at the same exit, a dead proxy a
# different one (§8).
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


# Every await in this file goes through the bridge below with a timeout, so a
# hung remote call ends the operation instead of the run. pyppeteer provides
# no connect timeout of its own and its page methods' `timeout` option does
# not cover a browser that has stopped answering at all.
DEFAULT_OP_TIMEOUT = 120
# 150, not 30, and kept identical to the Playwright engine's
# CDP_CONNECT_TIMEOUT_MS — see its comment. Short version: the upgrade was
# measured hanging 121s before the SERVER hung up, so 30s gives up while the
# server is still working. It is NOT a cure for `profile_locked`, which was
# observed with no timed-out connect in its history at all.
CONNECT_TIMEOUT = 150


class _AsyncBridge:
    """Runs pyppeteer's coroutines on a private event loop, synchronously.

    Exists so this engine can reuse page_flow.py unchanged. That module holds
    the policy all three engines must share, and it is written against plain
    synchronous callables — the right shape for two of the three drivers.
    Bridging here keeps the policy in one place rather than growing an async
    copy of it that would drift.

    The second benefit is what the family's rules actually require: every
    call gets an explicit, enforced timeout. `.result(timeout)` returns
    control even when the browser never answers, which pyppeteer's own API
    does not offer.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        # pyppeteer leaves CDP calls in flight when a browser closes, and the
        # loop then logs each one at ERROR level, AFTER a successful run has
        # printed its results. Five of those under a "Saved 95 products" line
        # read as a failed run. Only that shape is swallowed; anything else
        # still gets the default handler, because silencing the loop
        # wholesale would hide real faults.
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        # BOTH, not one or the other. asyncio puts its own words in
        # `message` ("Future exception was never retrieved") and the
        # library's in `exception`, and an `or` between them looks at the
        # exception and never sees the message — which is why these kept
        # printing after they were "handled".
        message = " | ".join(
            str(context.get(k)) for k in ("exception", "message")
            if context.get(k))
        if any(m in message for m in (
                "Target closed", "Connection closed",
                "Task was destroyed but it is pending",
                "Future exception was never retrieved",
                "No session with given id",
                "Event loop is closed")):
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, coro, timeout: Optional[float] = DEFAULT_OP_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"pyppeteer call did not return within {timeout}s")

    def close(self):
        """Stop the loop, CANCELLING whatever it still has in flight.

        Stopping the loop outright leaves pyppeteer's background tasks
        pending — its websocket reader and keepalive — and asyncio then
        prints "Task was destroyed but it is pending!" plus a traceback for
        each. That happens AFTER the output is written, so the run is fine
        and the log looks like a crash.

        Cancelling first is the fix, and it has to happen ON the loop thread —
        `call_soon_threadsafe` is what gets it there.
        """
        def _cancel_and_stop():
            pending = [t for t in asyncio.all_tasks(self.loop)
                       if t is not asyncio.current_task(self.loop)]
            for task in pending:
                task.cancel()
            if pending:
                logger.debug("Cancelled %d pending pyppeteer task(s) on "
                             "teardown.", len(pending))
            self.loop.stop()

        self.loop.call_soon_threadsafe(_cancel_and_stop)
        self._thread.join(timeout=5)


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
    # this site, so it is recorded beside what the run read and never used as
    # a loop bound.
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


_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA from the browser's own reported version.

    `browser.version()` returns "HeadlessChrome/115.0.0.0"; the marketing
    part is what a real Chrome would send.
    """
    number = version.split("/")[-1] if "/" in version else version
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{number} Safari/537.36")


class _Session:
    """One pyppeteer browser + page, relaunchable onto a different exit."""

    def __init__(self, bridge: _AsyncBridge, args, pool):
        self.bridge, self.args, self.pool = bridge, args, pool
        self.remote = bool(args.cdp_endpoint)
        self.browser = self.page = None

    def open(self):
        if self.remote:
            logger.info("Connecting to an existing browser over CDP: %s",
                        _mask_credentials(self.args.cdp_endpoint))
            # pyppeteer's browserWSEndpoint takes the full ws://user:pass@host
            # form and authenticates on the WebSocket upgrade, so an
            # authenticated Scraping Browser endpoint works here — unlike
            # Selenium's debuggerAddress, which has nowhere to put a password.
            self.browser = self.bridge.run(
                connect(browserWSEndpoint=self.args.cdp_endpoint,
                        ignoreHTTPSErrors=True),
                timeout=getattr(self.args, "cdp_connect_timeout",
                                CONNECT_TIMEOUT))
            self.page = self.bridge.run(self.browser.newPage())
            _watch_api(self)
            return self

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage",
                       *LAUNCH_ARGS]
        launch_kwargs = {}
        if self.args.chromium_path:
            launch_kwargs["executablePath"] = self.args.chromium_path
            logger.info("Using the browser at %s instead of pyppeteer's own.",
                        self.args.chromium_path)
        credentials = None
        if self.pool:
            exit_url = self.pool.current
            # Credentials go through page.authenticate(), never onto the
            # command line: --proxy-server= becomes part of the browser's
            # argv, readable by anything that can run `ps`.
            scrubbed, credentials = split_credentials(exit_url)
            launch_args.append(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(exit_url))

        # handleSIGINT/TERM/HUP off, and not for tidiness: pyppeteer installs
        # signal handlers inside launch(), and `signal.signal` raises "signal
        # only works in main thread of the main interpreter" because the
        # event loop here lives on a worker thread. Teardown is handled by
        # _Session.close() in scrape()'s finally block instead.
        self.browser = self.bridge.run(
            launch(headless=self.args.headless, args=launch_args,
                   ignoreHTTPSErrors=True, handleSIGINT=False,
                   handleSIGTERM=False, handleSIGHUP=False, **launch_kwargs),
            timeout=CONNECT_TIMEOUT * 2)
        self.page = self.bridge.run(self.browser.newPage())
        version = self.bridge.run(self.browser.version())
        self.bridge.run(self.page.setUserAgent(_chrome_ua(version)))
        # 1440x900, matching the captures. The viewport decides how far the
        # captures were taken at, so the tile counts in the README mean
        # keeping the window the measured size keeps the README's numbers
        # meaningful.
        self.bridge.run(self.page.setViewport({"width": 1440, "height": 900}))
        if credentials:
            self.bridge.run(self.page.authenticate(
                {"username": credentials[0], "password": credentials[1]}))
        _watch_api(self)
        return self

    def relaunch(self):
        if self.remote:
            return
        try:
            self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error while closing browser: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.bridge.run(self.page.close(), timeout=30)
            else:
                self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to pyppeteer
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here. pyppeteer takes `() => expr` like
# Playwright and unlike Selenium, which is exactly why page_flow names
# operations rather than passing JavaScript across the boundary (§1).
def _count(session, selector: str) -> int:
    try:
        return len(session.bridge.run(session.page.querySelectorAll(selector)))
    except Exception as e:  # noqa: BLE001
        logger.debug("count(%s) failed: %s", selector, e)
        return 0


def _sleep(ms: int) -> None:
    time.sleep(ms / 1000.0)


# Every page of a listing arrives over the site's own API, and that API can
# be refused while the document keeps answering 200. Counting the refusals is
# what separates a listing that ran out (COMPLETE) from one whose next page
# was refused (PARTIAL) — without it the two are the same observation and a
# throttled run reports "complete" holding page 1 (§7). All three engines
# count this the same way, so all three reach the same verdict.
_API_PATH_MARKER = "/apis/ui/"


def _watch_api(session) -> None:
    """Count API responses that were not 200.

    The document and the API are refused SEPARATELY on this site: the page
    can answer 200 while `/apis/ui/...` behind it is throttled. A run that
    saw that and reported "the listing ended" would claim a complete run
    while holding page 1 (§7).
    """
    session._api_errors = 0

    def on_response(resp):
        try:
            if _API_PATH_MARKER in resp.url and resp.status != 200:
                session._api_errors += 1
        except Exception:  # noqa: BLE001 — a listener must never raise
            pass

    try:
        session.page.on("response", on_response)
    except Exception as e:  # noqa: BLE001
        logger.debug("Could not attach the response listener: %s", e)


def _api_error_count(session) -> int:
    return getattr(session, "_api_errors", 0)


def _content(session) -> Optional[str]:
    try:
        return session.bridge.run(session.page.content())
    except Exception as e:  # noqa: BLE001
        logger.debug("content() unavailable (page navigating?): %s", e)
        return None


def _current_url(session) -> str:
    try:
        return session.bridge.run(session.page.evaluate("() => location.href"))
    except Exception:  # noqa: BLE001
        return ""


# The site's own API, asked from inside the page. pyppeteer takes an async
# ARROW and awaits it, where Selenium takes a function body plus a callback —
# three spellings of one operation, which is why page_flow names the
# operation and each engine writes its own (§1).
_FETCH_API_JS = """async ([p, b]) => {
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


def _fetch_api(session, path: str, body=None, timeout: float = 45.0):
    """Ask Woolworths' own API from inside the page. (status, payload, text)

    From INSIDE the page, not with `requests`: the browser has just been
    issued an Akamai session, and a same-origin `fetch` carries its cookies
    and its TLS fingerprint for free.
    """
    try:
        res = session.bridge.run(
            session.page.evaluate(_FETCH_API_JS, [path, body]), timeout=timeout)
    except (PyppeteerError, PPTimeout, TimeoutError) as e:
        logger.debug("api fetch failed: %s", e)
        return None, None, ""
    if not isinstance(res, dict):
        return None, None, ""
    status, text = res.get("status"), res.get("text") or ""
    payload = None
    if text:
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
    return status, payload, text


# The grid renders into OPEN shadow roots on `<wc-product-tile>` elements,
# which no serialised HTML carries — so the walk happens in page script.
_READ_TILES_JS = """() => {
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


def _read_tiles(session) -> List[dict]:
    """The rendered tiles, read through their open shadow roots.

    Raw strings only; every judgement about what they MEAN belongs to
    `product_parser.overlay_dom_prices`, so all three engines hand the parser
    the same thing. A tile with no product link is skipped rather than
    guessed at (§4).
    """
    try:
        return session.bridge.run(session.page.evaluate(_READ_TILES_JS),
                                  timeout=30) or []
    except (PyppeteerError, PPTimeout, TimeoutError) as e:
        # A tile read confirms a price the API already gave us. It must never
        # take a run down.
        logger.debug("tile read failed: %s", e)
        return []


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args, html: str = "") -> int:
    return page_flow.min_matches(args.mode)


def _classify(session, html: str, status=None) -> str:
    # `status` POSITIONAL and second, matching the other two engines and the
    # callee's real signature (§17).
    return page_flow.classify(html, status, _current_url(session))


def _rows_from_payload(payload, args, page_num: int, data_source: str) -> List:
    """Rows for this page, always a list. `page_num` is threaded through
    because `position` restarts at 1 on every page (§18)."""
    return products_from_payload(payload, page=page_num,
                                 data_source=data_source, host=CANONICAL_HOST)


def handle_captcha_if_present(session, args, allow_solve: bool = True) -> bool:
    """Detect and solve a challenge. True if something was solved."""
    html = _content(session)
    if html is None:
        return False

    already_rendered = _count(session, _ready_selector(args))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    url = _current_url(session)
    html_challenge = detect_recaptcha_v3(html, url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: session.bridge.run(session.page.evaluate(js)), page_url=url)
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

    session.bridge.run(session.page.evaluate(INJECT_TOKEN_JS, token))
    logger.info("Token injected. Reloading page to continue.")
    _sleep(1500)
    try:
        session.bridge.run(session.page.reload(
            {"waitUntil": "domcontentloaded", "timeout": 60000}))
    except Exception as e:  # noqa: BLE001
        logger.warning("Reload after the solve failed: %s", e)
    return True


def _confirm_with_dom(session, rows, page_num: int) -> Optional[dict]:
    """Confirm the API's prices against the rendered tiles.

    A CONFIRMATION, never a correction: where the two agree the row's
    `price_source` becomes `api+dom`; where they disagree the row is left as
    the API gave it and a warning names the sku (§4). Page 1 only — that is
    the page the browser is looking at.
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


def _land(session, args, outcome):
    """Make sure the browser is ON the listing page. (html, status, state)

    Navigates only when it has to. Every page of a listing is fetched from
    inside ONE loaded page — the navigation exists to make Akamai issue a
    session, not to reach page N.
    """
    if getattr(session, "landed_url", None) == args.url:
        html = _content(session)
        if html:
            return html, None, _classify(session, html, None)
        session.landed_url = None

    status = None
    for attempt in range(1, args.retries + 1):
        try:
            resp = session.bridge.run(
                session.page.goto(args.url, {"waitUntil": "domcontentloaded",
                                             "timeout": 60000}),
                timeout=75)
            status = getattr(resp, "status", None)
            break
        except (PyppeteerError, PPTimeout, TimeoutError) as e:
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
    state = _classify(session, html, status)
    if state != "blocked":
        session.landed_url = args.url
    return html, status, state


def _resolve_target(session, args, outcome):
    """What to ask the API for. (ok, term, category_id, slug)

    A category id is OPAQUE: `bakery` is `1_DEB537E`. Sending the slug
    instead returns 200 with zero products and `Success: true`, which reads
    exactly like a real empty category — so an unresolved slug is refused
    here rather than turned into a run that reports success on nothing.
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
    except (PyppeteerError, PPTimeout, TimeoutError) as e:
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
            session.bridge.run(
                session.page.screenshot(
                    {"path": f"{args.out}_page{page_num}_debug.png"}),
                timeout=30)
        except Exception as e:  # noqa: BLE001
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
                "correct, but the DOM price cross-check is impossible.",
                _current_url(session))

    ok, term, category_id, slug = _resolve_target(session, args, outcome)
    if not ok:
        outcome.final_url = _current_url(session) or args.url
        return outcome

    path, body, data_source = api_request_for(
        args.mode, term=term, category_id=category_id, slug=slug,
        page=page_num, page_size=args.page_size)

    errors_before = _api_error_count(session)
    status, payload, text = _fetch_api(session, path, body)

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
        logger.warning(
            "--concurrency %d is not implemented in this engine. The pages "
            "ARE independently addressable on this site, so concurrency is "
            "meaningful — use playwright_scraper.py for it. Continuing with "
            "one worker; the rows are identical either way.",
            args.concurrency)

    bridge = _AsyncBridge()
    session = _Session(bridge, args, pool).open()
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
        # CANCELS what the loop still has in flight. Stopping it outright
        # leaves pyppeteer's websocket reader and keepalive pending, and
        # asyncio then prints "Task was destroyed but it is pending!" for
        # each — AFTER the output is written, so a fine run looks like a
        # crash.
        bridge.close()

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
        description="Woolworths Online product scraper (pyppeteer edition)")
    p.add_argument("--url", default=None,
                   help="A Woolworths Online URL: a search "
                        "(/shop/search/products?searchTerm={term}) or a "
                        "category (/shop/browse/{slug}, child nodes "
                        "included). Required, unless WOOLWORTHS_URL is set in "
                        "the environment or in .env.")
    p.add_argument("--mode", choices=["search", "category"], default=None,
                   help="Which view the URL is. Inferred from the URL by "
                        "default; passing one that disagrees is an error.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Filled from the URL "
                        "by default.")
    p.add_argument("--pages", type=int, default=1,
                   help=f"Number of pages to walk (default 1, cap {PAGE_CAP}). "
                        f"The walk stops when a page adds no ORGANIC product "
                        f"it has not already seen — never when a page comes "
                        f"back empty, because past its last real page this "
                        f"listing keeps answering 200 with nothing but ads.")
    p.add_argument("--delay", type=float, default=2.0,
                   help="Delay between pages, seconds (default 2.0).")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for family compatibility and NOT "
                        "IMPLEMENTED in this engine. Use "
                        "playwright_scraper.py, where it works.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3).")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry (default 2.0).")
    p.add_argument("--format", choices=["json", "csv", "both"], default="json",
                   help="Output format (default json).")
    p.add_argument("--out", default="woolworths_products",
                   help="Output filename prefix.")
    p.add_argument("--chromium-path", default=None, metavar="PATH",
                   help="Browser executable to drive instead of pyppeteer's "
                        "own download.")
    p.add_argument("--proxy", default=None,
                   help="Single proxy URL. Credentials go through "
                        "page.authenticate(), never onto the command line.")
    p.add_argument("--proxy-file", default=None, metavar="PATH",
                   help="File of proxy URLs, one per line, used as a pool.")
    p.add_argument("--proxy-rotate", choices=ROTATE_MODES, default="per-run",
                   help="When to move to the next exit (default per-run).")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool before use.")
    p.add_argument("--proxy-block-retries", type=int, default=4, metavar="N",
                   help="How many exits to try when a page comes back refused.")
    p.add_argument("--twocaptcha-key", default=None,
                   help="2Captcha API key. Prefer TWOCAPTCHA_KEY in .env.")
    p.add_argument("--captcha-api", choices=["v1", "v2"], default="v2",
                   help="Which 2Captcha API to use (default v2).")
    p.add_argument("--solve-captcha", choices=["never", "when-blocked", "always"],
                   default="when-blocked",
                   help="When to spend money on a solve (default "
                        "when-blocked). Nothing has yet been measured to "
                        "solve on this site: Akamai's refusal carries no "
                        "widget, so no solve is attempted. That is a "
                        "statement about the page, not the product.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP, e.g. "
                        "ws://user:pass@host:port. An AUTHENTICATED endpoint "
                        "works here, unlike the Selenium engine.")
    p.add_argument("--cdp-connect-timeout", type=float, default=CONNECT_TIMEOUT,
                   metavar="SECONDS",
                   help=f"How long to wait for --cdp-endpoint to accept the "
                        f"connection (default {int(CONNECT_TIMEOUT)}).")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write an empty file when a run finds nothing.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save what the parser is given, on success as well as "
                        "failure. Writes the document and `.api.json` — the "
                        "payload the rows actually come from.")
    # HEADFUL by default. Measured on this site 2026-09-16: headful 4/4
    # served, headless 0/4 (Akamai 403), from one datacentre address.
    p.add_argument("--headful", dest="headless", action="store_false",
                   default=False,
                   help="Run with a real browser window. THE DEFAULT, and on "
                        "this site the difference between data and a 403: "
                        "4/4 served headful against 0/4 headless.")
    p.add_argument("--headless", dest="headless", action="store_true",
                   help="Run headless. Measured 0/4 served on this site from "
                        "a datacentre address. Expect exit 3.")
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

    # Not a flag: the site's own UI asks for 36 and so does this.
    args.page_size = PAGE_SIZE

    if args.pages > PAGE_CAP:
        logger.warning("--pages %d is above this scraper's %d-page cap; it "
                       "will stop there.", args.pages, PAGE_CAP)
    return args


if __name__ == "__main__":
    args = parse_args()
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
