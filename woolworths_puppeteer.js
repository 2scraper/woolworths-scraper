#!/usr/bin/env node
/**
 * Woolworths.com.au Scraper — Puppeteer (Node.js)
 *
 * Usage:
 *   npm install puppeteer axios csv-writer yargs
 *   node woolworths_puppeteer.js --search "milk" --max-pages 5
 *   node woolworths_puppeteer.js --category "fruit-veg" --proxy http://user:pass@host:port --debug
 */

"use strict";

const puppeteer = require("puppeteer");
const axios = require("axios");
const fs = require("fs");
const path = require("path");
const { createObjectCsvWriter } = require("csv-writer");
const yargs = require("yargs/yargs");
const { hideBin } = require("yargs/helpers");

// ---------------------------------------------------------------------------
const BASE_URL = "https://www.woolworths.com.au";
const SEARCH_API = `${BASE_URL}/apis/ui/Search/products`;
const BROWSE_API = `${BASE_URL}/apis/ui/browse/category`;
const DEFAULT_PAGE_SIZE = 36;
const DEFAULT_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";

// ---------------------------------------------------------------------------
let allProducts = [];
let shutdownRequested = false;

process.on("SIGINT", () => { console.log(`\n[!] SIGINT`); shutdownRequested = true; });
process.on("SIGTERM", () => { console.log(`\n[!] SIGTERM`); shutdownRequested = true; });

// ---------------------------------------------------------------------------
const argv = yargs(hideBin(process.argv))
  .option("search", { type: "string" })
  .option("category", { type: "string" })
  .option("max-pages", { type: "number", default: 5 })
  .option("output", { type: "string", default: "woolworths_products.json" })
  .option("csv", { type: "string" })
  .option("specials", { type: "boolean", default: false })
  .option("sort", { choices: ["TraderRelevance", "PriceAsc", "PriceDesc", "Name"], default: "TraderRelevance" })
  .option("proxy", { type: "string" })
  .option("2captcha-key", { type: "string" })
  .option("debug", { type: "boolean", default: false })
  .option("headed", { type: "boolean", default: false })
  .option("skip-browser", { type: "boolean", default: false, describe: "Skip browser warm-up, use requests directly" })
  .check((a) => {
    if (!a.search && !a.category) throw new Error("Provide --search or --category");
    return true;
  })
  .parse();

// ---------------------------------------------------------------------------
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
function rand(min, max) { return Math.random() * (max - min) + min; }

function slugify(text) {
  return (text || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function debugSave(name, data) {
  if (!argv.debug) return;
  fs.mkdirSync("debug", { recursive: true });
  const ts = new Date().toISOString().slice(11, 19).replace(/:/g, "");
  const fpath = `debug/${ts}_${name}.json`;
  fs.writeFileSync(fpath, JSON.stringify(data, null, 2));
  console.log(`  [debug] ${fpath}`);
}

// ---------------------------------------------------------------------------
// Normalise product — verified against live Woolworths Search API
// ---------------------------------------------------------------------------
function normaliseProduct(raw) {
  const aa = raw.AdditionalAttributes || {};
  const rating = raw.Rating || {};

  // sapdepartmentname is most reliable single top-level dept
  const sapDept = (aa.sapdepartmentname || "");
  const category = sapDept.split(" ").map(w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()).join(" ");

  // piescategorynamesjson[-1] is the most specific subcategory
  let subcategory = "";
  try { const c = JSON.parse(aa.piescategorynamesjson || "[]"); subcategory = c.length ? c[c.length-1] : ""; } catch {}

  // All navigation departments (product may belong to multiple)
  let departments = "";
  try { const d = JSON.parse(aa.piesdepartmentnamesjson || "[]"); departments = d.join(", "); } catch {}

  let description = aa.description || raw.Description || "";
  description = description.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();

  const stockcode = raw.Stockcode || "";
  const urlName = raw.UrlFriendlyName || slugify(raw.DisplayName || raw.Name || "");

  return {
    stockcode: String(stockcode),
    barcode: raw.Barcode || "",
    name: raw.DisplayName || raw.Name || "",
    brand: raw.Brand || aa.brand || "",
    description,
    url: `${BASE_URL}/shop/productdetails/${stockcode}/${urlName}`,
    image_url: raw.LargeImageFile || raw.MediumImageFile || raw.SmallImageFile || "",
    // Pricing — Price is a direct float in this API
    price: raw.Price ?? null,
    was_price: raw.WasPrice ?? null,
    savings_amount: raw.SavingsAmount ?? null,
    special_price: raw.IsOnSpecial ? raw.Price : null,
    cup_string: raw.CupString || "",
    cup_measure: raw.CupMeasure || "",
    is_special: !!raw.IsOnSpecial,
    is_half_price: !!raw.IsHalfPrice,
    promotion: (raw.HeaderTag || {}).Content || "",
    package_size: raw.PackageSize || "",
    unit: raw.Unit || "",
    category,
    subcategory,
    departments,
    is_available: raw.IsAvailable !== undefined ? raw.IsAvailable : true,
    is_in_stock: raw.IsInStock !== undefined ? raw.IsInStock : true,
    is_purchasable: raw.IsPurchasable !== undefined ? raw.IsPurchasable : true,
    rating: rating.Average || 0,
    rating_count: rating.RatingCount || 0,
    review_count: rating.ReviewCount || 0,
    health_star_rating: aa.healthstarrating || "",
    lifestyle_claims: aa.lifestyleanddietarystatement || "",
    allergy_statement: aa.allergystatement || "",
    ingredients: aa.ingredients || "",
    storage_instructions: aa.storageinstructions || "",
    country_of_origin: aa.countryoforigin || "",
    scraped_at: new Date().toISOString(),
  };
}

// ---------------------------------------------------------------------------
// Extract products — handles both API shapes
// ---------------------------------------------------------------------------
function extractProducts(data) {
  const results = [];
  // Primary: Products[].Products[]
  for (const bundle of data.Products || []) {
    for (const prod of bundle.Products || []) {
      results.push(normaliseProduct(prod));
    }
  }
  // Fallback: Bundles[].Products[]
  if (!results.length) {
    for (const bundle of data.Bundles || []) {
      for (const prod of bundle.Products || []) {
        results.push(normaliseProduct(prod));
      }
    }
  }
  return results;
}

// ---------------------------------------------------------------------------
// Parse proxy for axios
// ---------------------------------------------------------------------------
function parseProxyForAxios(proxyUrl) {
  if (!proxyUrl) return undefined;
  const m = proxyUrl.match(/https?:\/\/(?:([^:@]+):([^@]+)@)?([^:]+):(\d+)/);
  if (!m) return undefined;
  const [, user, pass, host, port] = m;
  return { protocol: "http", host, port: parseInt(port), ...(user ? { auth: { username: user, password: pass } } : {}) };
}

// ---------------------------------------------------------------------------
// Session harvester
// ---------------------------------------------------------------------------
async function harvestSessionBrowser() {
  console.log("[*] Launching Puppeteer browser…");
  const launchArgs = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--ignore-certificate-errors",
  ];
  if (argv.proxy) launchArgs.push(`--proxy-server=${argv.proxy}`);

  const browser = await puppeteer.launch({
    headless: argv.headed ? false : "new",
    args: launchArgs,
    defaultViewport: { width: 1366, height: 768 },
  });

  const page = await browser.newPage();
  await page.setUserAgent(DEFAULT_UA);
  await page.setExtraHTTPHeaders({ "Accept-Language": "en-AU,en;q=0.9" });
  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
  });

  try {
    console.log("[*] Visiting homepage (timeout=45s)…");
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 45000 });
    await sleep(rand(2500, 4000));
    const title = await page.title();
    console.log(`  Title: ${title}`);

    // Detect Akamai block
    if (title.toLowerCase().includes('access denied') || title.toLowerCase().includes('just a moment')) {
      if (argv.debug) { try { await page.screenshot({ path: 'debug/error_blocked.png' }); } catch {} }
      await browser.close();
      throw new Error(`Browser blocked by Akamai (title='${title}'). Falling back to requests.`);
    }

    if (argv.debug) {
      fs.mkdirSync("debug", { recursive: true });
      await page.screenshot({ path: "debug/01_homepage.png" });
    }

    try {
      await page.goto(`${BASE_URL}/shop/browse/dairy-eggs-fridge`, {
        waitUntil: "domcontentloaded", timeout: 30000
      });
      await sleep(rand(1500, 2500));
      if (argv.debug) await page.screenshot({ path: "debug/02_browse.png" });
    } catch (e) {
      console.log(`  [!] Browse page failed (non-fatal): ${e.message}`);
    }

    const rawCookies = await page.cookies();
    const cookies = {};
    for (const c of rawCookies) cookies[c.name] = c.value;
    console.log(`[*] Harvested ${Object.keys(cookies).length} cookies`);
    await browser.close();
    return cookies;

  } catch (e) {
    if (argv.debug) {
      try { await page.screenshot({ path: "debug/error_browser.png" }); } catch {}
    }
    await browser.close();
    throw e;
  }
}

async function harvestSessionRequests() {
  console.log("[*] Falling back to requests-based session harvest…");
  try {
    const res = await axios.get(BASE_URL, {
      headers: {
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
      },
      proxy: parseProxyForAxios(argv.proxy),
      timeout: 20000,
      maxRedirects: 5,
    });
    const cookies = {};
    const setCookie = res.headers["set-cookie"] || [];
    for (const ck of setCookie) {
      const m = ck.match(/^([^=]+)=([^;]*)/);
      if (m) cookies[m[1]] = m[2];
    }
    console.log(`[*] Got ${Object.keys(cookies).length} cookies (status ${res.status})`);
    return cookies;
  } catch (e) {
    console.error(`  [!] requests fallback failed: ${e.message}`);
    return {};
  }
}

async function getSessionCookies() {
  if (argv['skip-browser']) {
    console.log('[*] --skip-browser: using requests-based session harvest');
    return await harvestSessionRequests();
  }
  try {
    return await harvestSessionBrowser();
  } catch (e) {
    const msg = e.message || "";
    if (msg.includes("ERR_TIMED_OUT") || msg.includes("Timeout") || msg.includes("timeout")) {
      console.log("[!] Browser timed out — falling back to requests");
    } else if (msg.includes("blocked by Akamai") || msg.includes("Access Denied")) {
      console.log("[!] Browser blocked — falling back to requests");
    } else {
      console.log(`[!] Browser error: ${e.message}`);
    }
    console.log("[*] Falling back to requests-based session harvest…");
    return await harvestSessionRequests();
  }
}

// ---------------------------------------------------------------------------
// API caller
// ---------------------------------------------------------------------------
async function apiPost(url, payload, cookies) {
  const cookieStr = Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join("; ");
  try {
    const res = await axios.post(url, payload, {
      headers: {
        Accept: "application/json, text/plain, */*",
        "Accept-Language": "en-AU,en;q=0.9",
        "Content-Type": "application/json",
        Origin: BASE_URL,
        Referer: `${BASE_URL}/`,
        "User-Agent": DEFAULT_UA,
        Cookie: cookieStr,
      },
      proxy: parseProxyForAxios(argv.proxy),
      timeout: 45000,
    });
    debugSave("api_response", res.data);
    return res.data;
  } catch (e) {
    if (e.code === "ECONNRESET" || (e.message && e.message.includes("timeout"))) {
      console.error(`  [!] API timeout/reset: ${e.message}`);
    } else {
      console.error(`  [!] POST failed: ${e.message}`);
    }
    return null;
  }
}

// ---------------------------------------------------------------------------
// Scrapers
// ---------------------------------------------------------------------------
async function scrapeSearch(searchTerm, cookies) {
  const products = [];
  let pageNum = 1;
  let totalCount = null;
  console.log(`[*] Searching: '${searchTerm}'`);

  while (pageNum <= argv["max-pages"] && !shutdownRequested) {
    const payload = {
      searchTerm,
      pageNumber: pageNum,
      pageSize: DEFAULT_PAGE_SIZE,
      sortType: argv.sort,
      location: `/shop/search/products?searchTerm=${encodeURIComponent(searchTerm)}`,
      formatObject: JSON.stringify({ name: searchTerm }),
      isSpecial: argv.specials,
      isBundle: false,
      isMobile: false,
      filters: [],
      groupEdmVariants: false,
    };

    const data = await apiPost(SEARCH_API, payload, cookies);
    if (!data) break;

    if (totalCount === null) {
      totalCount = data.SearchResultsCount || data.TotalRecordCount || "?";
    }

    const pageProds = extractProducts(data);
    if (!pageProds.length) { console.log(`  [*] No products on page ${pageNum}`); break; }

    products.push(...pageProds);
    console.log(`  Page ${pageNum}: +${pageProds.length} (total ${products.length}/${totalCount})`);
    pageNum++;
    await sleep(rand(800, 1500));
  }
  return products;
}

async function scrapeCategory(categorySlug, cookies) {
  const products = [];
  let pageNum = 1;
  const catPath = `/shop/browse/${categorySlug.replace(/^\//, "")}`;
  console.log(`[*] Browsing category: ${categorySlug}`);

  while (pageNum <= argv["max-pages"] && !shutdownRequested) {
    const payload = {
      pageNumber: pageNum,
      pageSize: DEFAULT_PAGE_SIZE,
      sortType: argv.sort,
      url: catPath,
      location: catPath,
      formatObject: "{}",
      isSpecial: argv.specials,
      filters: [],
    };

    const data = await apiPost(BROWSE_API, payload, cookies);
    if (!data) break;

    const pageProds = extractProducts(data);
    if (!pageProds.length) { console.log(`  [*] No products on page ${pageNum}`); break; }

    const totalCount = data.SearchResultsCount || data.TotalRecordCount || "?";
    products.push(...pageProds);
    console.log(`  Page ${pageNum}: +${pageProds.length} (total ${products.length}/${totalCount})`);
    pageNum++;
    await sleep(rand(800, 1500));
  }
  return products;
}

// ---------------------------------------------------------------------------
// Output
// ---------------------------------------------------------------------------
function saveJson(products, filePath) {
  fs.writeFileSync(filePath, JSON.stringify(products, null, 2));
  console.log(`[✓] JSON → ${filePath}`);
}

async function saveCsv(products, filePath) {
  if (!products.length) return;
  const header = Object.keys(products[0]).map((k) => ({ id: k, title: k }));
  const writer = createObjectCsvWriter({ path: filePath, header });
  await writer.writeRecords(products);
  console.log(`[✓] CSV → ${filePath}`);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
(async () => {
  if (!argv.proxy && !process.env.PROXY_URL) {
    console.log("[!] No proxy — https://2prx.com for AU residential IPs");
  }
  if (argv.debug) fs.mkdirSync("debug", { recursive: true });

  const cookies = await getSessionCookies();
  if (!Object.keys(cookies).length) {
    console.error("[!] No cookies — check proxy"); process.exit(1);
  }

  const authCookies = Object.keys(cookies).filter(k => ["wow-auth-token","w-rctx"].includes(k));
  if (authCookies.length) {
    console.log(`[✓] Auth cookies: ${authCookies.join(", ")}`);
  } else {
    console.log("[!] Auth cookies missing — try 2captcha Anti-Detect Browser");
    console.log("    https://2captcha.com/anti-detect-browser");
  }

  let products;
  if (argv.search) {
    products = await scrapeSearch(argv.search, cookies);
  } else {
    products = await scrapeCategory(argv.category, cookies);
  }

  allProducts = products;
  if (!products.length) { console.log("[!] No products"); process.exit(1); }

  saveJson(products, argv.output);
  if (argv.csv) await saveCsv(products, argv.csv);
  console.log(`\n[✓] Done — ${products.length} products`);
})();
