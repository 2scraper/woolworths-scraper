#!/usr/bin/env python3
"""
Woolworths.com.au Scraper — Selenium
Alternative implementation using Selenium WebDriver.

Usage:
  pip install selenium webdriver-manager requests 2captcha-python
  python woolworths_selenium.py --search "milk" --max-pages 5
  python woolworths_selenium.py --search "bread" --proxy http://user:pass@host:port
  python woolworths_selenium.py --category "dairy-eggs-fridge" --debug
  python woolworths_selenium.py --search "milk" --skip-browser --proxy http://user:pass@host:port
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
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager

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
all_products: list[dict] = []
shutdown_requested = False


def signal_handler(sig, frame):
    global shutdown_requested
    print(f"\n[!] Signal {sig} — saving {len(all_products)} products…")
    shutdown_requested = True


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Woolworths.com.au scraper (Selenium)")
    p.add_argument("--search", help="Search keyword")
    p.add_argument("--category", help="Category slug (e.g. 'fruit-veg')")
    p.add_argument("--max-pages", type=int, default=5)
    p.add_argument("--output", default="woolworths_products.json")
    p.add_argument("--csv", help="CSV output filename")
    p.add_argument("--specials", action="store_true")
    p.add_argument("--sort", choices=["TraderRelevance", "PriceAsc", "PriceDesc", "Name"],
                   default="TraderRelevance")
    p.add_argument("--proxy", help="Proxy URL http://user:pass@host:port")
    p.add_argument("--2captcha-key", dest="captcha_key")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--skip-browser", dest="skip_browser", action="store_true",
                   help="Skip browser warm-up, use requests directly")
    return p.parse_args()


def debug_save(name, data, debug):
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
    print(f"  [debug] {path}")


# ---------------------------------------------------------------------------
# Product normalisation — verified against live API
# ---------------------------------------------------------------------------
def _sap_title(s: str) -> str:
    return s.title() if s else ""


def _parse_json_list(raw) -> list:
    try:
        return json.loads(raw or "[]") or []
    except (json.JSONDecodeError, TypeError):
        return []


def normalise_product(raw: dict) -> dict:
    aa = raw.get("AdditionalAttributes") or {}
    rating = raw.get("Rating") or {}

    category = _sap_title(aa.get("sapdepartmentname", ""))
    pies_cats = _parse_json_list(aa.get("piescategorynamesjson"))
    subcategory = pies_cats[-1] if pies_cats else ""
    all_depts = _parse_json_list(aa.get("piesdepartmentnamesjson"))
    departments = ", ".join(all_depts)

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
        "price": raw.get("Price"),
        "was_price": raw.get("WasPrice"),
        "savings_amount": raw.get("SavingsAmount"),
        "special_price": raw.get("Price") if raw.get("IsOnSpecial") else None,
        "cup_string": raw.get("CupString", ""),
        "cup_measure": raw.get("CupMeasure", ""),
        "is_special": bool(raw.get("IsOnSpecial")),
        "is_half_price": bool(raw.get("IsHalfPrice")),
        "promotion": (raw.get("HeaderTag") or {}).get("Content", ""),
        "package_size": raw.get("PackageSize", ""),
        "unit": raw.get("Unit", ""),
        "category": category,
        "subcategory": subcategory,
        "departments": departments,
        "is_available": raw.get("IsAvailable", True),
        "is_in_stock": raw.get("IsInStock", True),
        "is_purchasable": raw.get("IsPurchasable", True),
        "rating": rating.get("Average") or 0,
        "rating_count": rating.get("RatingCount") or 0,
        "review_count": rating.get("ReviewCount") or 0,
        "health_star_rating": aa.get("healthstarrating", ""),
        "lifestyle_claims": aa.get("lifestyleanddietarystatement", ""),
        "allergy_statement": aa.get("allergystatement", ""),
        "ingredients": aa.get("ingredients", ""),
        "storage_instructions": aa.get("storageinstructions", ""),
        "country_of_origin": aa.get("countryoforigin", "") or "",
        "scraped_at": datetime.utcnow().isoformat() + "Z",
    }


def _slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def extract_products(data: dict) -> list[dict]:
    results = []
    for bundle in data.get("Products") or []:
        for prod in bundle.get("Products") or []:
            results.append(normalise_product(prod))
    if not results:
        for bundle in data.get("Bundles") or []:
            for prod in bundle.get("Products") or []:
                results.append(normalise_product(prod))
    return results


# ---------------------------------------------------------------------------
# Session — Selenium browser
# ---------------------------------------------------------------------------
def build_driver(args):
    opts = Options()
    if not args.headed:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(f"user-agent={DEFAULT_UA}")
    opts.add_argument("--lang=en-AU")
    opts.add_argument("--window-size=1366,768")
    opts.add_argument("--ignore-certificate-errors")
    if args.proxy:
        opts.add_argument(f"--proxy-server={args.proxy}")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


def harvest_session_browser(args, debug):
    print("[*] Launching Selenium browser…")
    driver = build_driver(args)
    try:
        print("[*] Visiting homepage (timeout=45s)…")
        driver.set_page_load_timeout(45)
        driver.get(BASE_URL)
        time.sleep(random.uniform(2.5, 4.0))
        title = driver.title
        print(f"  Title: {title!r}")

        # Detect Akamai block
        if "access denied" in title.lower() or "just a moment" in title.lower():
            if debug:
                Path("debug").mkdir(exist_ok=True)
                driver.save_screenshot("debug/error_blocked.png")
                print("  [debug] debug/error_blocked.png")
            driver.quit()
            raise RuntimeError(
                f"Browser blocked by Akamai (title={title!r}). Falling back to requests."
            )

        if debug:
            Path("debug").mkdir(exist_ok=True)
            driver.save_screenshot("debug/01_homepage.png")

        try:
            driver.set_page_load_timeout(30)
            driver.get(f"{BASE_URL}/shop/browse/dairy-eggs-fridge")
            time.sleep(random.uniform(1.5, 2.5))
            if debug:
                driver.save_screenshot("debug/02_browse.png")
        except Exception as e:
            print(f"  [!] Browse page failed (non-fatal): {e}")

        cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
        print(f"[*] Harvested {len(cookies)} cookies")
        if debug:
            debug_save("cookies_browser", cookies, debug)
        return cookies

    except Exception:
        try:
            driver.quit()
        except Exception:
            pass
        raise
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def harvest_session_requests(proxy_url, debug):
    print("[*] Harvesting session via requests (no browser)…")
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        r = requests.get(BASE_URL, headers=headers, proxies=proxies, timeout=20)
        cookies = {c.name: c.value for c in r.cookies}
        print(f"[*] Got {len(cookies)} cookies via requests (status {r.status_code})")
        if debug:
            debug_save("cookies_requests", cookies, debug)
        return cookies
    except Exception as e:
        print(f"  [!] requests fallback failed: {e}")
        return {}


def get_session_cookies(args, debug):
    if args.skip_browser:
        print("[*] --skip-browser: using requests-based session harvest")
        return harvest_session_requests(args.proxy, debug)
    try:
        return harvest_session_browser(args, debug)
    except Exception as e:
        err = str(e)
        if any(x in err for x in ("timeout", "Timeout", "timed out", "ERR_TIMED_OUT")):
            print("[!] Browser timeout — falling back to requests")
        elif "blocked by Akamai" in err or "Access Denied" in err:
            print("[!] Browser blocked — falling back to requests")
        else:
            print(f"[!] Browser error: {e}")
        print("[*] Falling back to requests-based session harvest…")
        return harvest_session_requests(args.proxy, debug)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def api_post(url, payload, cookies, proxy, debug):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-AU,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "User-Agent": DEFAULT_UA,
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    for attempt in range(1, 4):
        try:
            resp = requests.post(url, json=payload, headers=headers,
                                 proxies=proxies, timeout=45)
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
            print(f"  [!] POST failed: {e}")
            return None
    return None


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------
def scrape_search(search_term, args, cookies):
    products = []
    page_num = 1
    total_count = None
    print(f"[*] Searching: '{search_term}'")

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
            break

        if total_count is None:
            total_count = data.get("SearchResultsCount") or data.get("TotalRecordCount") or "?"

        page_prods = extract_products(data)
        if not page_prods:
            print(f"  [*] No products on page {page_num}")
            break

        products.extend(page_prods)
        print(f"  Page {page_num}: +{len(page_prods)} (total {len(products)}/{total_count})")
        page_num += 1
        time.sleep(random.uniform(0.8, 1.5))

    return products


def scrape_category(category_slug, args, cookies):
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
            print(f"  [*] No products on page {page_num}")
            break

        total_count = data.get("SearchResultsCount") or data.get("TotalRecordCount") or "?"
        products.extend(page_prods)
        print(f"  Page {page_num}: +{len(page_prods)} (total {len(products)}/{total_count})")
        page_num += 1
        time.sleep(random.uniform(0.8, 1.5))

    return products


# ---------------------------------------------------------------------------
def save_json(products, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2, ensure_ascii=False)
    print(f"[✓] JSON → {path}")


def save_csv(products, path):
    if not products:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(products[0].keys()))
        writer.writeheader()
        writer.writerows(products)
    print(f"[✓] CSV → {path}")


# ---------------------------------------------------------------------------
def main():
    global all_products
    args = parse_args()

    if not args.search and not args.category:
        print("[!] Provide --search KEYWORD or --category SLUG")
        sys.exit(1)

    if args.debug:
        Path("debug").mkdir(exist_ok=True)

    proxy = args.proxy or os.environ.get("PROXY_URL")
    if proxy:
        args.proxy = proxy
    else:
        print("[!] No proxy — https://2prx.com for AU residential IPs")

    captcha_key = args.captcha_key or os.environ.get("TWOCAPTCHA_API_KEY")
    if captcha_key:
        print(f"[*] 2captcha key: {captcha_key[:8]}…")

    cookies = get_session_cookies(args, args.debug)
    if not cookies:
        print("[!] No cookies — check proxy")
        sys.exit(1)

    if args.search:
        products = scrape_search(args.search, args, cookies)
    else:
        products = scrape_category(args.category, args, cookies)

    all_products = products
    if not products:
        print("[!] No products scraped")
        sys.exit(1)

    save_json(products, args.output)
    if args.csv:
        save_csv(products, args.csv)
    print(f"[✓] Done — {len(products)} products")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        if all_products:
            save_json(all_products, "woolworths_partial.json")
        sys.exit(0)
