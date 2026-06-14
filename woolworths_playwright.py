#!/usr/bin/env python3
"""
Woolworths.com.au Scraper — Playwright (primary)
Scrapes products by search keyword or category browse.

Verified against live API responses — correct field mapping confirmed.

Usage:
  pip install playwright requests 2captcha-python
  playwright install chromium

  python woolworths_playwright.py --search "milk" --max-pages 5
  python woolworths_playwright.py --search "bread" --proxy http://user:pass@host:port
  python woolworths_playwright.py --category "fruit-veg" --max-pages 3 --debug
  python woolworths_playwright.py --search "cheese" --specials --sort PriceAsc --csv out.csv
"""

import argparse
import csv
import json
import os
import re
import signal
import sys
import time
import random
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "https://www.woolworths.com.au"
SEARCH_API = f"{BASE_URL}/apis/ui/Search/products"
BROWSE_API = f"{BASE_URL}/apis/ui/browse/category"

DEFAULT_PAGE_SIZE = 36
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Globals for signal handler
# ---------------------------------------------------------------------------
all_products: list[dict] = []
shutdown_requested = False


def signal_handler(sig, frame):
    global shutdown_requested
    print(f"\n[!] Signal {sig} — saving {len(all_products)} products and exiting…")
    shutdown_requested = True


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Woolworths.com.au scraper (Playwright)")
    p.add_argument("--search", help="Search keyword (e.g. 'milk')")
    p.add_argument("--category", help="Category slug (e.g. 'fruit-veg')")
    p.add_argument("--max-pages", type=int, default=5)
    p.add_argument("--output", default="woolworths_products.json")
    p.add_argument("--csv", help="CSV output filename")
    p.add_argument("--specials", action="store_true", help="Specials/deals only")
    p.add_argument(
        "--sort",
        choices=["TraderRelevance", "PriceAsc", "PriceDesc", "Name"],
        default="TraderRelevance",
    )
    p.add_argument("--proxy", help="Proxy URL http://user:pass@host:port")
    p.add_argument("--2captcha-key", dest="captcha_key")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--skip-browser", dest="skip_browser", action="store_true",
                   help="Skip browser warm-up, use requests directly (faster when browser is blocked)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Debug helpers
# ---------------------------------------------------------------------------
def debug_save(name: str, data, debug: bool):
    if not debug:
        return
    Path("debug").mkdir(exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    if isinstance(data, (dict, list)):
        path = f"debug/{ts}_{name}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    else:
        path = f"debug/{ts}_{name}.html"
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(data))
    print(f"  [debug] saved {path}")


# ---------------------------------------------------------------------------
# Product normalisation — verified against live API
# ---------------------------------------------------------------------------
def _sap_title(s: str) -> str:
    """'PROPRIETARY BAKERY' → 'Proprietary Bakery'"""
    return s.title() if s else ""


def _parse_json_list(raw: str) -> list:
    try:
        return json.loads(raw or "[]") or []
    except (json.JSONDecodeError, TypeError):
        return []


def normalise_product(raw: dict) -> dict:
    """
    Field mapping verified against live Woolworths Search API responses.

    Key points:
    - Price is a direct float (not a nested object)
    - WasPrice = original price, SavingsAmount = discount amount
    - Rating is a nested object {Average, ReviewCount, RatingCount, ...}
    - AdditionalAttributes contains rich product metadata
    - sapdepartmentname is the most reliable single top-level category
    - piescategorynamesjson[-1] is the most specific subcategory
    - piesdepartmentnamesjson may list multiple departments (bread -> Bakery + Lunch Box)
    """
    aa = raw.get("AdditionalAttributes") or {}
    rating = raw.get("Rating") or {}

    # Category: sapdepartmentname → "Proprietary Bakery", "Fresh Convenience", etc.
    category = _sap_title(aa.get("sapdepartmentname", ""))

    # Subcategory: last entry in piescategorynamesjson is most specific
    pies_cats = _parse_json_list(aa.get("piescategorynamesjson"))
    subcategory = pies_cats[-1] if pies_cats else ""

    # All Woolworths navigation departments (product may belong to multiple)
    all_depts = _parse_json_list(aa.get("piesdepartmentnamesjson"))
    departments = ", ".join(all_depts)

    # Description: AdditionalAttributes.description is the richest (HTML stripped)
    description = aa.get("description") or raw.get("Description") or ""
    description = re.sub(r"<[^>]+>", " ", description).strip()
    description = re.sub(r"\s+", " ", description)

    stockcode = raw.get("Stockcode", "")
    url_name = raw.get("UrlFriendlyName") or _slugify(
        raw.get("DisplayName") or raw.get("Name", "")
    )

    return {
        "stockcode": stockcode,
        "barcode": raw.get("Barcode", ""),
        "name": raw.get("DisplayName") or raw.get("Name", ""),
        "brand": raw.get("Brand", "") or aa.get("brand", ""),
        "description": description,
        "url": f"{BASE_URL}/shop/productdetails/{stockcode}/{url_name}",
        "image_url": (
            raw.get("LargeImageFile")
            or raw.get("MediumImageFile")
            or raw.get("SmallImageFile", "")
        ),
        # Pricing — Price is a direct float in this API, not an object
        "price": raw.get("Price"),
        "was_price": raw.get("WasPrice"),        # original price (= price when not on special)
        "savings_amount": raw.get("SavingsAmount"),
        "special_price": raw.get("Price") if raw.get("IsOnSpecial") else None,
        "cup_string": raw.get("CupString", ""),  # e.g. "$0.66 / 100G"
        "cup_measure": raw.get("CupMeasure", ""),
        "is_special": bool(raw.get("IsOnSpecial")),
        "is_half_price": bool(raw.get("IsHalfPrice")),
        "promotion": (raw.get("HeaderTag") or {}).get("Content", ""),  # e.g. "Save $0.50"
        # Product info
        "package_size": raw.get("PackageSize", ""),
        "unit": raw.get("Unit", ""),
        "category": category,           # top-level: "Proprietary Bakery", "Fresh Convenience"
        "subcategory": subcategory,     # most specific: "Packaged Bread & Bakery"
        "departments": departments,     # all nav depts: "Lunch Box, Bakery"
        # Availability
        "is_available": raw.get("IsAvailable", True),
        "is_in_stock": raw.get("IsInStock", True),
        "is_purchasable": raw.get("IsPurchasable", True),
        # Ratings — Rating is a nested object (not top-level fields)
        "rating": rating.get("Average") or 0,
        "rating_count": rating.get("RatingCount") or 0,
        "review_count": rating.get("ReviewCount") or 0,
        # AdditionalAttributes extras
        "health_star_rating": aa.get("healthstarrating", ""),
        "lifestyle_claims": aa.get("lifestyleanddietarystatement", ""),
        "allergy_statement": aa.get("allergystatement", ""),
        "ingredients": aa.get("ingredients", ""),
        "storage_instructions": aa.get("storageinstructions", ""),
        "country_of_origin": aa.get("countryoforigin", "") or "",
        # Meta
        "scraped_at": datetime.utcnow().isoformat() + "Z",
    }


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


# ---------------------------------------------------------------------------
# Extract products from API response — handles real Products[].Products[] shape
# ---------------------------------------------------------------------------
def extract_products(data: dict) -> list[dict]:
    results = []
    # Primary shape (confirmed): Products[i].Products[j]
    # Each outer bundle contains exactly 1 product (bundle size = 1 for search API)
    for bundle in data.get("Products") or []:
        for prod in bundle.get("Products") or []:
            results.append(normalise_product(prod))
    # Fallback shape (browse API may differ): Bundles[i].Products[j]
    if not results:
        for bundle in data.get("Bundles") or []:
            for prod in bundle.get("Products") or []:
                results.append(normalise_product(prod))
    return results


# ---------------------------------------------------------------------------
# Session acquisition — browser first, requests fallback
# ---------------------------------------------------------------------------
def build_proxy_config(proxy_url: str) -> dict | None:
    if not proxy_url:
        return None
    m = re.match(r"https?://(?:([^:@]+):([^@]+)@)?([^:]+):(\d+)", proxy_url)
    if not m:
        return {"server": proxy_url}
    user, pwd, host, port = m.groups()
    cfg = {"server": f"http://{host}:{port}"}
    if user:
        cfg["username"] = user
        cfg["password"] = pwd
    return cfg


def harvest_session_playwright(args, debug: bool) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[!] playwright not installed: pip install playwright && playwright install chromium")
        sys.exit(1)

    print("[*] Launching browser to harvest session cookies…")
    proxy_config = build_proxy_config(args.proxy)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not args.headed,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
            proxy=proxy_config,
        )
        ctx = browser.new_context(
            user_agent=DEFAULT_UA,
            locale="en-AU",
            timezone_id="Australia/Sydney",
            viewport={"width": 1366, "height": 768},
            ignore_https_errors=True,
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = ctx.new_page()

        print("[*] Visiting homepage (timeout=45s)…")
        try:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=45_000)
            time.sleep(random.uniform(2.5, 4.0))
            title = page.title()
            print(f"  Title: {title!r}")
        except Exception as e:
            if debug:
                Path("debug").mkdir(exist_ok=True)
                page.screenshot(path="debug/error_homepage.png")
                print("  [debug] debug/error_homepage.png")
            browser.close()
            raise

        # Detect Akamai block — browser through this proxy gets Access Denied
        title_lower = title.lower()
        if "access denied" in title_lower or "just a moment" in title_lower:
            if debug:
                Path("debug").mkdir(exist_ok=True)
                page.screenshot(path="debug/error_blocked.png")
                print("  [debug] debug/error_blocked.png")
            browser.close()
            raise RuntimeError(
                f"Browser blocked by Akamai (title={title!r}). "
                "The proxy works for requests but not for browser TLS fingerprint. "
                "Falling back to requests-based harvest."
            )

        if debug:
            Path("debug").mkdir(exist_ok=True)
            page.screenshot(path="debug/01_homepage.png")

        try:
            page.goto(
                f"{BASE_URL}/shop/browse/dairy-eggs-fridge",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            time.sleep(random.uniform(1.5, 2.5))
            if debug:
                page.screenshot(path="debug/02_browse.png")
        except Exception as e:
            print(f"  [!] Browse page failed (non-fatal): {e}")

        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        print(f"[*] Harvested {len(cookies)} cookies")
        if debug:
            debug_save("cookies_browser", {k: v[:50] + "…" if len(v) > 50 else v for k, v in cookies.items()}, debug)
        browser.close()

    return cookies


def harvest_session_requests(proxy_url: str | None, debug: bool) -> dict:
    """
    Get Akamai session cookies via plain requests.
    Works when browser can't connect through the proxy.
    Won't yield wow-auth-token but Akamai bm_* cookies are sufficient
    for unauthenticated product search.
    """
    print("[*] Harvesting session via requests (no browser)…")
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        r = requests.get(BASE_URL, headers=headers, proxies=proxies, timeout=20, allow_redirects=True)
        cookies = {c.name: c.value for c in r.cookies}
        print(f"[*] Got {len(cookies)} cookies via requests (status {r.status_code})")
        if debug:
            debug_save("cookies_requests", cookies, debug)
        return cookies
    except Exception as e:
        print(f"  [!] requests session harvest failed: {e}")
        return {}


def get_session_cookies(args, debug: bool) -> dict:
    # --skip-browser: skip Playwright entirely, go straight to requests
    if getattr(args, "skip_browser", False):
        print("[*] --skip-browser: using requests-based session harvest")
        return harvest_session_requests(args.proxy, debug)

    try:
        cookies = harvest_session_playwright(args, debug)
        if cookies:
            return cookies
    except Exception as e:
        err = str(e)
        if any(x in err for x in ("ERR_TIMED_OUT", "Timeout", "timeout", "timed out")):
            print("[!] Browser timed out — falling back to requests")
        elif "blocked by Akamai" in err or "Access Denied" in err:
            print("[!] Browser blocked by proxy — falling back to requests")
        else:
            print(f"[!] Browser error: {e}")
        print("[*] Falling back to requests-based session harvest…")
    return harvest_session_requests(args.proxy, debug)


# ---------------------------------------------------------------------------
# API request helper
# ---------------------------------------------------------------------------
def api_post(url: str, payload: dict, cookies: dict, proxy: str | None, debug: bool) -> dict | None:
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-AU,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "User-Agent": DEFAULT_UA,
        "Cookie": cookie_str,
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    for attempt in range(1, 4):
        try:
            resp = requests.post(url, json=payload, headers=headers, proxies=proxies, timeout=45)
            resp.raise_for_status()
            data = resp.json()
            debug_save("api_response", data, debug)
            return data
        except requests.HTTPError as e:
            print(f"  [!] HTTP {e.response.status_code}: {e.response.text[:200]}")
            return None
        except requests.exceptions.Timeout:
            print(f"  [!] API timeout (attempt {attempt}/3)")
            if attempt < 3:
                time.sleep(attempt * 3)
            else:
                return None
        except Exception as e:
            print(f"  [!] API POST failed: {e}")
            return None
    return None


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------
def scrape_search(search_term: str, args, cookies: dict) -> list[dict]:
    products = []
    page_num = 1
    total_count = None
    print(f"[*] Searching for: '{search_term}'")

    while page_num <= args.max_pages and not shutdown_requested:
        payload = {
            "searchTerm": search_term,
            "pageNumber": page_num,
            "pageSize": DEFAULT_PAGE_SIZE,
            "sortType": args.sort,
            "location": f"/shop/search/products?searchTerm={search_term}",
            "formatObject": json.dumps({"name": search_term}),
            "isSpecial": args.specials,
            "isBundle": False,
            "isMobile": False,
            "filters": [],
            "groupEdmVariants": False,
        }
        data = api_post(SEARCH_API, payload, cookies, args.proxy, args.debug)
        if not data:
            print(f"  [!] Empty response on page {page_num}, stopping")
            break

        if total_count is None:
            total_count = (
                data.get("SearchResultsCount")
                or data.get("TotalRecordCount")
                or "?"
            )

        page_prods = extract_products(data)
        if not page_prods:
            print(f"  [*] No products on page {page_num}, done")
            break

        products.extend(page_prods)
        print(f"  Page {page_num}: +{len(page_prods)} products (total: {len(products)}/{total_count})")
        page_num += 1
        time.sleep(random.uniform(0.8, 1.5))

    return products


def scrape_category(category_slug: str, args, cookies: dict) -> list[dict]:
    products = []
    page_num = 1
    cat_path = f"/shop/browse/{category_slug.lstrip('/')}"
    print(f"[*] Browsing category: {category_slug}")

    while page_num <= args.max_pages and not shutdown_requested:
        payload = {
            "pageNumber": page_num,
            "pageSize": DEFAULT_PAGE_SIZE,
            "sortType": args.sort,
            "url": cat_path,
            "location": cat_path,
            "formatObject": "{}",
            "isSpecial": args.specials,
            "filters": [],
        }
        data = api_post(BROWSE_API, payload, cookies, args.proxy, args.debug)
        if not data:
            break

        page_prods = extract_products(data)
        if not page_prods:
            print(f"  [*] No products on page {page_num}, done")
            break

        total_count = (
            data.get("SearchResultsCount") or data.get("TotalRecordCount") or "?"
        )
        products.extend(page_prods)
        print(f"  Page {page_num}: +{len(page_prods)} products (total: {len(products)}/{total_count})")
        page_num += 1
        time.sleep(random.uniform(0.8, 1.5))

    return products


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def save_json(products: list[dict], path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2, ensure_ascii=False)
    print(f"[✓] Saved {len(products)} products → {path}")


def save_csv(products: list[dict], path: str):
    if not products:
        return
    keys = list(products[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(products)
    print(f"[✓] Saved {len(products)} products → {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global all_products

    args = parse_args()

    if not args.search and not args.category:
        print("[!] Provide --search KEYWORD or --category SLUG")
        sys.exit(1)

    if args.debug:
        Path("debug").mkdir(exist_ok=True)
        print("[*] Debug mode ON — saving to debug/")

    proxy = args.proxy or os.environ.get("PROXY_URL")
    if proxy:
        args.proxy = proxy
        print(f"[*] Proxy: {proxy}")
    else:
        print("[!] No proxy — Woolworths may rate-limit your IP")
        print("    AU residential proxies: https://2prx.com")

    captcha_key = args.captcha_key or os.environ.get("TWOCAPTCHA_API_KEY")
    if captcha_key:
        print(f"[*] 2captcha key: {captcha_key[:8]}…")
    else:
        print("[*] No 2captcha key — get one at https://2captcha.com")

    # Step 1: cookies
    cookies = get_session_cookies(args, args.debug)
    if not cookies:
        print("[!] No cookies obtained — check proxy credentials")
        sys.exit(1)

    auth_cookies = [k for k in cookies if k in ("wow-auth-token", "w-rctx")]
    if auth_cookies:
        print(f"[✓] Auth cookies present: {auth_cookies}")
    else:
        print("[!] Auth cookies missing (Akamai session cookies only — sufficient for search)")
        print("    For full session auth use 2captcha Anti-Detect Browser:")
        print("    https://2captcha.com/anti-detect-browser")

    # Step 2: scrape
    if args.search:
        products = scrape_search(args.search, args, cookies)
    else:
        products = scrape_category(args.category, args, cookies)

    all_products = products

    if not products:
        print("[!] No products scraped")
        if shutdown_requested:
            sys.exit(0)
        sys.exit(1)

    # Step 3: save
    save_json(products, args.output)
    if args.csv:
        save_csv(products, args.csv)

    print(f"\n[✓] Done — {len(products)} products scraped")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        if all_products:
            print(f"\n[!] Interrupted — saving {len(all_products)} products")
            save_json(all_products, "woolworths_partial.json")
        sys.exit(0)
