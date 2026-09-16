"""
product_parser.py
-----------------
Woolworths extraction. THIS FILE IS THE SITE — everything else in the repo is
family core with a handful of named constants (CLAUDE.md §1).

What Woolworths actually publishes, measured 2026-09-16
--------------------------------------------------------
Nothing, in the HTML. That is not a figure of speech and it is the single
fact that shapes this file.

A served category page is 872 KB. Its `<body>` serialises to **3,735
characters of visible text**, all of it navigation chrome: the word "Banana"
appears **zero** times on `/shop/browse/fruit-veg`, and the seven
`$nn.nn` strings in the document are the cart total and the delivery
thresholds. There are **zero** `/shop/productdetails/` anchors, and the
`application/ld+json` count is zero on search and specials and exactly one
on a category page, where it is a `BreadcrumbList`.

The catalogue is not missing — it is behind two doors at once:

1.  **It arrives as JSON, over the site's own API**, after the shell paints.
2.  **It renders into SHADOW DOM.** The grid is 67 `<wc-product-tile>`
    custom elements whose content lives in OPEN shadow roots, which
    `page.content()` does not serialise. Reading those roots needs a live
    browser; no amount of care with BeautifulSoup reaches them.

So this parser's primary input is **the API's JSON**, not markup, and that is
deliberate rather than a shortcut:

    POST /apis/ui/Search/products     search      -> {"Products": [...]}
    POST /apis/ui/browse/category     a category  -> {"Bundles":  [...]}
    GET  /apis/ui/products/{codes}    by stockcode -> [ ... ]

All three answer with the SAME 115-field product object, so one builder reads
all three and `data_source` records which one did.

There is deliberately NO HTML fallback path
-------------------------------------------
§4 asks for two paths, structured first and a CSS/URL fallback second. The
second path is absent here, and the measurement above is the reason: a
fallback parsing the served HTML would find zero products on every page
Woolworths serves, on every mode, forever. That is dead code that looks
load-bearing, which §17 says is worse than no code.

What replaces it is a SECOND VIEW rather than a second parser: the engines
read the shadow-DOM tiles through a driver primitive and hand the result to
`overlay_dom_prices()`, which confirms the API's price against the rendered
one and records `price_source` as `api+dom`. That is the reconciliation §4
wants, and it is the only DOM read in this repo.

Four traps this file exists to avoid
------------------------------------
1.  **`Products` is a list of GROUPS, not of products.** Each entry is a
    bundle wrapper `{"Products": [...], "Name", "DisplayName"}` holding one
    product on every row measured. A parser reading `payload["Products"]` as
    the product list gets 36 wrapper dicts with no `Stockcode` on any of
    them. `flatten_products()` is the whole of the fix and the reason it has
    a test.

2.  **`WasPrice` is populated on 100% of rows and equals `Price` on 81% of
    them.** See `Product.original_price` in output_writer.py for the numbers.
    Reading it straight gives a 0% discount on four products in five.

3.  **Promoted ads repeat across pages, and a listing NEVER runs out of
    them.** Past its last real page the API keeps answering HTTP 200 with
    `Success: true` and nothing but sponsored rows — page 17 of a 16-page
    category returned 1 row, page 18 returned 8, page 99 returned 8, all
    sponsored, all the same ones page 1 had. A run that stops when a page
    returns no rows never stops. `organic_count()` is what the engines
    actually test.

4.  **`akamai` appears on every page Woolworths SERVES and on neither page it
    refuses.** Counted: 1 occurrence on each of six served captures, 0 on
    both denial pages. As a block marker it is exactly inverted — §18's rule
    ("count it on a page you know is good") catching a marker that three of
    this family's repos would have accepted on sight, since Woolworths is
    indeed fronted by Akamai.
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

from output_writer import CURRENCY_BY_HOST, SOURCE_DEFAULT, Product

logger = logging.getLogger("product_parser")


# ===========================================================================
# Hosts
# ===========================================================================
# Woolworths Online is ONE site on ONE host. There is no country table here
# because there is no second country: `woolworths.co.nz` is a different
# company's site on a different platform (Woolworths NZ, formerly Countdown)
# and `woolworths.co.za` is a different company again (Woolworths Holdings,
# South Africa) — neither runs this API, neither is in scope, and claiming
# them would be the "is not a MediaMarkt site" error §5 warns about in
# reverse.
#
# Taken from the site's own markup rather than from a guess, which is what
# §5 asks for. Woolworths publishes NO `<link rel="alternate" hreflang=…>`
# set at all — 0 occurrences of `hreflang` on a served page — and exactly one
# canonical, `https://www.woolworths.com.au/...`. One host, named by the site.
#
# The apex answers too: `https://woolworths.com.au/...` returned HTTP 200 and
# redirected to `www.`, so both spellings are accepted and both normalise to
# the canonical one before a URL is rebuilt (§5's "whether hosts answer on
# www." check, run rather than assumed).
CANONICAL_HOST = "www.woolworths.com.au"
HOSTS = ("www.woolworths.com.au", "woolworths.com.au")

BASE_URL = f"https://{CANONICAL_HOST}"


# ===========================================================================
# The site's own API
# ===========================================================================
API_SEARCH_PATH = "/apis/ui/Search/products"
API_CATEGORY_PATH = "/apis/ui/browse/category"
API_PRODUCTS_PATH = "/apis/ui/products/"
# The category tree, which is how an opaque category id is resolved from a
# URL slug — see `category_id_for_slug`.
API_CATEGORIES_PATH = "/apis/ui/PiesCategoriesWithSpecials"

# What the site's own UI asks for, and therefore what looks ordinary. The
# API accepts larger values; asking for more than the site does is the kind
# of difference a bot manager scores on, and 36 is plenty.
PAGE_SIZE = 36

# A listing's pages are addressable: page N is a request parameter, not a
# cursor, and page 1 does not have to be read before page 5 can be asked for
# (§7). Both modes qualify, which is why `--concurrency` is allowed on both.
PAGE_URL_REASON = (
    "both modes paginate by an integer page number in the request body, so "
    "page N is addressable without reading page N-1"
)
CONCURRENCY_REASON = None  # None == no reason to refuse it

# A cap so a malformed `--pages` cannot walk forever. A 575-product category
# is 16 pages; the largest department is comfortably under this.
PAGE_CAP = 200


# ===========================================================================
# Page state: what did the site just hand us?
# ===========================================================================
# Akamai's refusal, in the two spellings the SAME page reaches a parser in
# (§20, measured again here on a fourth site):
#
#     raw bytes, as `requests` or curl see it
#         You don&#39;t...  no — the LETTERS are plain, the PUNCTUATION is
#         escaped: `errors&#46;edgesuite&#46;net`, `Reference&#32;&#35;18...`
#     browser DOM, as page.content() serialises it
#         `errors.edgesuite.net`, `Reference #18...`
#
# Counted on this site: `errors.edgesuite.net` 1 in the DOM form and 0 in the
# raw form; `Reference #` 1 and 0. A literal marker for either matches the
# three browser engines and silently misses the HTTP client.
#
# Every marker below was chosen to survive BOTH spellings — the escaping
# touches punctuation only, so `edgesuite`, `Access Denied` and
# `You don't have permission` are each intact in both — and the text is
# unescaped over a bounded prefix before matching anyway, so a marker added
# later does not reacquire the hole.
#
# Each was also counted on six pages known to be good (§18): 0 occurrences of
# every one of them. `akamai` was a CANDIDATE and is deliberately absent — it
# scored 1 on all six good pages and 0 on both denials.
BOT_CHALLENGE_MARKERS = (
    "Access Denied",
    "You don't have permission",
    "edgesuite",
)

# How much of the document to unescape before looking. An Akamai denial is
# ~400 bytes; a served page is ~850 KB. Unescaping the whole of the latter on
# every fetch buys nothing and risks a product name deep in a payload reading
# as a marker (§20).
UNESCAPE_PREFIX_BYTES = 4096

# The POSITIVE signal, and the one that answers correctly where a marker list
# cannot: was this page built out of Woolworths' own assets? (§8, and §18's
# Chromium-error-page case, which a title check gets wrong because the error
# page is titled with the site's own hostname.)
#
# Measured: 71 to 90 occurrences on each of six served pages — a search, a
# category, specials, the home page, and two fetched through the Scraping
# Browser — and 0 on both denial pages.
ASSET_HOST_MARKER = "cdn1.woolworths.media"
# One, not two. §17's classification-order trap was a threshold of 2 rejecting
# a minimal real page that carried 1; the measured floor here is 71 and the
# measured ceiling on a denial is 0, so a threshold of 1 separates them with
# the widest margin available and cannot be tripped by a lean page.
ASSET_HOST_MIN = 1

# THERE IS NO NO-RESULTS MARKER, and the absence is measured rather than an
# oversight.
#
# The obvious candidate is Woolworths' own "we couldn't find any" copy, which
# a no-results search does carry. It was taken as a marker and it was WRONG,
# because §18's rule — count it on a page you know is good — had not been
# applied to it. Counted afterwards on six captures: a search with 2,205
# results 4, a category 4, specials 4, the home page 4, a no-results search
# 4, and a page fetched over CDP 4. The string lives in the JS bundle as a
# template, not in rendered copy, so it is on every page the site serves.
#
# Shipped, it made a search for "milk" report itself empty and exit 4. That
# is the same shape of mistake as `akamai` above, twice in one file, which is
# why the rule is worth stating twice: a marker that matches every page is
# worse than no marker.
#
# Emptiness is therefore decided where it can be: the API states its own
# result count, and `total_count()` reads it. An HTML classification cannot
# tell an empty listing from a full one on this site, and does not try.

# How many rendered tiles mean "the grid has painted". Must be > 1: waiting
# for one resolves on the first tile long before the grid is done (§5).
MIN_CARD_MATCHES = 3

# Names the engines use for the shadow-DOM read. These are NOT passed to
# BeautifulSoup — nothing in this repo parses product markup as a string,
# because there is none. They are the selectors each engine hands to its own
# driver, inside the browser, where open shadow roots are reachable.
SELECTORS = {
    # The custom element that IS a tile. 67 on a category page: 42 in the
    # grid and 25 in a horizontal "you might also like" carousel, which is
    # §4's neighbour-tile trap — `dom_tiles_to_index` keys on the stockcode
    # in each tile's own link, so a carousel tile can only ever overwrite
    # itself.
    "tile": "wc-product-tile",
    # Inside a tile's shadow root. A tile links to its product TWICE (image
    # and title), which is exactly the case §4 says to count DISTINCT ids
    # for rather than links.
    "item_link": 'a[href*="/shop/productdetails/"]',
    "price": '[class*="product-tile-price"]',
    "cup_price": '[class*="price-per-cup"]',
}


def unescape_prefix(html: str, limit: int = UNESCAPE_PREFIX_BYTES) -> str:
    """HTML-unescape the first `limit` characters.

    Both spellings of a denial page then compare equal against a plain
    marker, and a 850 KB payload is not rewritten on every fetch.
    """
    if not html:
        return ""
    return _html.unescape(html[:limit])


def detect_block_marker(html: str) -> Optional[str]:
    """Which refusal marker this page carries, or None.

    Matched against the unescaped prefix, so it answers the same for a page
    that arrived as raw bytes and for the same page serialised out of a
    browser.
    """
    head = unescape_prefix(html)
    for marker in BOT_CHALLENGE_MARKERS:
        if marker in head:
            return marker
    return None


def asset_reference_count(html: str) -> int:
    """How many times the page references Woolworths' own asset host."""
    return (html or "").count(ASSET_HOST_MARKER)


def is_woolworths_host(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in HOSTS


def detect_page_state(html: str, status: Optional[int] = None,
                      url: str = "") -> str:
    """What Woolworths just answered with. One of:

        blocked   Akamai refused, or this is not a Woolworths address
        shell     served, built out of Woolworths' assets, nothing painted
                  yet — this is the NORMAL first state, see below
        content   served, and tiles have rendered
        unknown   none of the above

    There is deliberately no `empty` here. Whether a listing holds anything
    is a property of the PAYLOAD, not of the document — see the note above
    NO-RESULTS for the measurement that settled it.

    `shell` rather than `unknown` is the normal first answer here, and the
    distinction matters because the two want opposite responses: a shell
    wants a WAIT (the catalogue is still in flight), an unknown wants a
    refetch. Woolworths ships an SPA whose first response carries no
    products by construction — §18's lesson that "which of the three missing-
    content cases applies can differ between page kinds" arriving as a
    property of the whole site rather than of one route.

    The order below is by how much each signal PROVES, not by cost (§17).
    """
    # 1. The status, which on this site is the primary signal — Akamai
    #    answers a refused request with 403 and a 400-byte page.
    if status is not None and status == 403:
        return "blocked"

    # 2. A refusal marker, in either encoding.
    if detect_block_marker(html):
        return "blocked"

    # 3. Somebody else's site. Checked before the positive signals so a
    #    redirect away from Woolworths cannot read as content.
    if url and not is_woolworths_host(url):
        return "blocked"

    # 4. Built out of Woolworths' own assets?
    if asset_reference_count(html) >= ASSET_HOST_MIN:
        return "content" if _looks_painted(html) else "shell"

    return "unknown"


def _looks_painted(html: str) -> bool:
    """Whether the grid's custom elements are in the document yet.

    Deliberately weak: it can only see that `<wc-product-tile>` elements
    EXIST, never what is in them, because their content is in shadow roots
    this function is not looking at a browser to read. The engines confirm
    with a live count through `page_flow`; this is for classifying a saved
    capture.
    """
    return (html or "").count("<wc-product-tile") >= MIN_CARD_MATCHES


# ===========================================================================
# URLs: what mode is this, and what does it select?
# ===========================================================================
_SEARCH_PATH_RE = re.compile(r"^/shop/search/products/?$", re.I)
_CATEGORY_PATH_RE = re.compile(r"^/shop/browse/([A-Za-z0-9\-/]+?)/?$", re.I)
# The address a product sits at. Nothing in the served markup uses it — it is
# here because the shadow-DOM tiles do, and because `product_url` builds it.
_PRODUCT_PATH_RE = re.compile(r"^/shop/productdetails/(\d+)(?:/([^/?#]+))?", re.I)

# Route prefixes under /shop/browse/ that are not category nodes.
_NOT_A_CATEGORY = frozenset({"", "shop", "browse"})


def mode_for_url(url: str) -> Optional[str]:
    """`search`, `category`, or None if this repo does not read that URL."""
    path = urlsplit(url).path or "/"
    if _SEARCH_PATH_RE.match(path):
        return "search"
    m = _CATEGORY_PATH_RE.match(path)
    if m and m.group(1).split("/")[0].lower() not in _NOT_A_CATEGORY:
        return "category"
    return None


def unsupported_reason(url: str) -> Optional[str]:
    """Why this repo refuses `url`, in words that name the actual cause.

    §5: "Refuse such a host WITH THE REASON — 'is not a Woolworths site' is
    false and sends the reader looking for a typo."
    """
    if not is_woolworths_host(url):
        host = (urlsplit(url).hostname or "").lower()
        if host in ("www.woolworths.co.nz", "woolworths.co.nz"):
            return ("woolworths.co.nz is Woolworths New Zealand (formerly "
                    "Countdown) — a different company on a different "
                    "platform, which does not serve the /apis/ui API this "
                    "repo reads")
        if host in ("www.woolworths.co.za", "woolworths.co.za"):
            return ("woolworths.co.za is Woolworths Holdings, South Africa — "
                    "an unrelated retailer that shares the name only")
        return f"{host or url!r} is not a woolworths.com.au address"
    if mode_for_url(url) is None:
        path = urlsplit(url).path
        if _PRODUCT_PATH_RE.match(path):
            return ("a single product page is not a mode in this repo: the "
                    "same object is already on every listing row, and "
                    "/apis/ui/products/{stockcode} returns it if one row is "
                    "all you need")
        return (f"{path!r} is neither /shop/search/products nor "
                f"/shop/browse/{{category}}")
    return None


def search_term_from_url(url: str) -> Optional[str]:
    """The `searchTerm` a search URL selects."""
    qs = parse_qs(urlsplit(url).query)
    for key in ("searchTerm", "searchterm"):
        if key in qs and qs[key]:
            return qs[key][0]
    return None


def category_slug_from_url(url: str) -> Optional[str]:
    """The `/shop/browse/{slug}` path a category URL selects.

    Returns the FULL slug path, child nodes included
    (`fruit-veg/fruit`), because the site's own tree keys on it.
    """
    m = _CATEGORY_PATH_RE.match(urlsplit(url).path or "/")
    return m.group(1).lower() if m else None


def product_url(stockcode: Any, slug: Optional[str] = None) -> str:
    """The address of a product, REBUILT.

    There is no anchor in the served markup to copy — measured zero on every
    capture — so this assembles what the site's own router builds, and what
    the shadow-DOM tiles are measured to contain:

        /shop/productdetails/{Stockcode}/{UrlFriendlyName}

    `smoke_test.py` pins the shape against a link taken out of a real tile.
    """
    code = str(stockcode).strip()
    if not code:
        return ""
    if slug:
        return f"{BASE_URL}/shop/productdetails/{code}/{slug}"
    return f"{BASE_URL}/shop/productdetails/{code}"


# Where the SPA sends a visitor it has decided is automated. Reached by a
# CLIENT-SIDE navigation after a perfectly normal HTTP 200, so no status code
# and no response marker can see it — only the address the browser ends up
# at can.
UNAUTHORISED_PATH = "/unauthorisederror"


def is_unauthorised_redirect(url: str) -> bool:
    """Whether the browser has been bounced to the site's "not authorised" page.

    Woolworths' page script reads `navigator.webdriver`; when it is true the
    app navigates itself here about a second after load. Measured three runs
    each way on 2026-09-16: 3/3 redirected with the default Playwright
    launch, 0/3 with `--disable-blink-features=AutomationControlled`.

    Worth a named check rather than an inline string because of what stays
    NORMAL when it happens — the document answered 200, and the site's API
    goes on answering 200 with real products, because the session cookies are
    valid. A run in this state returns correct rows and reports success while
    the grid is gone and the DOM cross-check is impossible. Nothing else in
    the pipeline can notice.
    """
    try:
        return (urlsplit(url or "").path or "").rstrip("/").lower() == UNAUTHORISED_PATH
    except ValueError:
        return False


def source_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host if host in HOSTS else SOURCE_DEFAULT


def currency_for(url_or_host: str) -> Optional[str]:
    """AUD, and only for a host this repo actually knows.

    Never a defaulted currency for an unknown host (§4, rung 5: absent is
    `null`, never a guessed "USD").
    """
    host = (urlsplit(url_or_host).hostname or url_or_host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return CURRENCY_BY_HOST.get(host)


# ===========================================================================
# Resolving a category slug to the opaque id the API wants
# ===========================================================================
def category_id_for_slug(tree: Any, slug: str) -> Optional[str]:
    """The `categoryId` the browse API needs, for a `/shop/browse/` slug.

    The id is OPAQUE and not derivable from the slug — `bakery` is
    `1_DEB537E`, `fruit-veg` is `1-E5BEE36E`, and the two do not even share a
    separator. So it has to be looked up in the site's own tree, which is
    what `/apis/ui/PiesCategoriesWithSpecials` returns.

    Walks children too, so `fruit-veg/fruit` resolves to the child node's id
    rather than its parent's — asking for the parent would silently return a
    different, larger listing while reporting success.
    """
    if not slug:
        return None
    wanted = [p for p in slug.strip("/").lower().split("/") if p]
    nodes = tree.get("Categories") if isinstance(tree, dict) else tree
    if not isinstance(nodes, list):
        return None

    node_id = None
    for part in wanted:
        match = None
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            if str(node.get("UrlFriendlyName", "")).lower() == part:
                match = node
                break
        if match is None:
            return None
        node_id = match.get("NodeId")
        nodes = match.get("Children") or []
    return str(node_id) if node_id else None


def iter_category_nodes(tree: Any, _depth: int = 0) -> Iterable[Tuple[str, str, str]]:
    """(slug, node_id, description) for every node in the tree, depth-first."""
    if _depth > 6:
        return
    nodes = tree.get("Categories") if isinstance(tree, dict) else tree
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        slug = str(node.get("UrlFriendlyName") or "")
        node_id = str(node.get("NodeId") or "")
        if slug and node_id:
            yield slug, node_id, str(node.get("Description") or "")
        for child_slug, child_id, desc in iter_category_nodes(
                node.get("Children") or [], _depth + 1):
            yield f"{slug}/{child_slug}", child_id, desc


# ===========================================================================
# The payload
# ===========================================================================
def flatten_products(payload: Any) -> List[dict]:
    """The product objects in an API response, in the order it listed them.

    THE TRAP THIS FUNCTION IS: the response's `Products` (search) and
    `Bundles` (category) are lists of GROUP WRAPPERS, not of products. Each
    wrapper is `{"Products": [...], "Name": ..., "DisplayName": ...}` and
    holds one product on every row measured. Reading the outer list as the
    product list yields 36 dicts with no `Stockcode` on any of them — a run
    that "succeeds" with every column null.

    Tolerates a bare list too, which is what `/apis/ui/products/{codes}`
    returns.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        # /apis/ui/products/{codes}: already a flat list of products.
        return [p for p in payload if isinstance(p, dict) and "Stockcode" in p]
    if not isinstance(payload, dict):
        return []

    groups = payload.get("Products")
    if groups is None:
        groups = payload.get("Bundles")
    if not isinstance(groups, list):
        return []

    out: List[dict] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        inner = group.get("Products")
        if isinstance(inner, list):
            out.extend(p for p in inner if isinstance(p, dict))
        elif "Stockcode" in group:
            # Defensive: a future response that flattens the wrapper away.
            out.append(group)
    return out


def total_count(payload: Any) -> Optional[int]:
    """How many products the API says the whole listing holds.

    NOT stable across pages, and this is measured rather than assumed: a
    search for `saffron` reported `SearchResultsCount` 69 on page 1 and 0 on
    page 50. So it is useful for logging and worthless as a loop bound —
    `organic_count()` is what decides when to stop.
    """
    if not isinstance(payload, dict):
        return None
    for key in ("SearchResultsCount", "TotalRecordCount"):
        v = payload.get(key)
        if isinstance(v, int):
            return v
    return None


def organic_count(rows: Sequence[Any]) -> int:
    """How many of these rows are real results rather than promoted ads.

    This is the number a listing actually runs out of. The ad slots never do:
    past its last real page the API answers 200 with `Success: true` and
    nothing but sponsored rows, the same ones page 1 carried.
    """
    n = 0
    for r in rows:
        sponsored = getattr(r, "is_sponsored", None)
        if sponsored is None and isinstance(r, dict):
            sponsored = r.get("IsSponsoredAd", r.get("is_sponsored"))
        if not sponsored:
            n += 1
    return n


# ===========================================================================
# Building a row
# ===========================================================================
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _text(value: Any) -> Optional[str]:
    """A trimmed string, or None — never an empty string.

    `Description` carries `<br>` on a real fraction of rows, so the tags come
    out here rather than in five call sites.
    """
    if value is None:
        return None
    s = str(value)
    s = _BR_RE.sub(" ", s)
    s = _TAG_RE.sub("", s)
    s = _html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _num(value: Any) -> Optional[float]:
    """A float, or None. Never 0.0 standing in for "absent"."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _flag(value: Any) -> Optional[bool]:
    """A bool, or None where the API omitted the field entirely.

    The distinction is real: `IsOnSpecial: false` is a fact about the
    product, a missing `IsOnSpecial` is a fact about the response.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None


def _attr(node: dict, key: str) -> Optional[str]:
    """A value out of `AdditionalAttributes`.

    The API writes the STRING "None" as well as JSON null for an absent
    attribute, and `_text` would happily return "None" as a value.
    """
    attrs = node.get("AdditionalAttributes")
    if not isinstance(attrs, dict):
        return None
    v = _text(attrs.get(key))
    if v is None or v.lower() == "none":
        return None
    return v


def was_price_of(node: dict) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """(original_price, savings_amount, discount_pct) — or three Nones.

    `WasPrice` is only a was-price when it is GREATER than `Price`. It is
    populated on 100% of rows and equal to `Price` on 81% of them, so the
    test is the whole point of the function.

    `discount_pct` is computed from the two rather than read from a badge,
    and is None rather than 0 where there is no discount (§4).
    """
    price = _num(node.get("Price"))
    was = _num(node.get("WasPrice"))
    savings = _num(node.get("SavingsAmount"))

    if price is None or was is None or was <= price:
        # Not a discount. `SavingsAmount` is 0.0 on these rows; passing it
        # through would put a 0 in a column that means "no saving" by being
        # null.
        return None, (savings if savings else None), None

    pct = round((was - price) / was * 100.0, 2)

    # A cross-check rather than a second source: `SavingsAmount` equalled
    # `WasPrice - Price` on all 660 rows measured, so a disagreement means
    # one of the three fields has changed meaning.
    if savings is not None and abs((was - price) - savings) > 0.011:
        logger.warning(
            "stockcode %s: SavingsAmount %.2f disagrees with WasPrice-Price "
            "%.2f — reporting the computed figure",
            node.get("Stockcode"), savings, was - price)

    return was, (savings if savings is not None else round(was - price, 2)), pct


def product_from_api(node: dict, *, page: Optional[int] = None,
                     position: Optional[int] = None,
                     data_source: str = "api-search",
                     host: str = CANONICAL_HOST) -> Optional[Product]:
    """One API product object -> one row, or None if it is not a product."""
    if not isinstance(node, dict):
        return None
    stockcode = node.get("Stockcode")
    if stockcode in (None, "", 0):
        return None

    original, savings, discount = was_price_of(node)
    slug = _text(node.get("UrlFriendlyName"))

    return Product(
        source=host if host in HOSTS else SOURCE_DEFAULT,
        url=product_url(stockcode, slug),
        sku=str(stockcode),
        # DisplayName, not Name — see the dataclass for why.
        title=_text(node.get("DisplayName")) or _text(node.get("Name")),

        brand=_text(node.get("Brand")),
        description=_text(node.get("Description")),
        variety=_text(node.get("Variety")),
        barcode=_text(node.get("Barcode")),

        price=_num(node.get("Price")),
        currency=currency_for(host),
        original_price=original,
        savings_amount=savings,
        discount_pct=discount,
        price_source="api",

        cup_price=_num(node.get("CupPrice")),
        cup_measure=_text(node.get("CupMeasure")),
        cup_string=_text(node.get("CupString")),
        package_size=_text(node.get("PackageSize")),
        unit=_text(node.get("Unit")),

        is_on_special=_flag(node.get("IsOnSpecial")),
        is_half_price=_flag(node.get("IsHalfPrice")),
        is_sponsored=_flag(node.get("IsSponsoredAd")),
        ad_status=_text(node.get("AdStatus")),
        offer_id=_text(node.get("OfferId")),

        is_available=_flag(node.get("IsAvailable")),
        is_in_stock=_flag(node.get("IsInStock")),
        is_purchasable=_flag(node.get("IsPurchasable")),
        supply_limit=int(_num(node.get("SupplyLimit")) or 0) or None,

        department=_attr(node, "sapdepartmentname"),
        category=_attr(node, "sapcategoryname"),
        subcategory=_attr(node, "sapsubcategoryname"),

        health_star_rating=_num(_attr(node, "healthstarrating")),
        dietary_claims=_attr(node, "lifestyleanddietarystatement"),
        allergy_statement=_attr(node, "allergystatement"),
        ingredients=_attr(node, "ingredients"),
        storage_instructions=_attr(node, "storageinstructions"),

        image_url=_text(node.get("LargeImageFile")) or _text(node.get("MediumImageFile")),

        data_source=data_source,
        page=page,
        position=position,
    )


def products_from_payload(payload: Any, *, page: Optional[int] = None,
                          data_source: str = "api-search",
                          host: str = CANONICAL_HOST) -> List[Product]:
    """Every row in one API response, in the order the site listed them.

    `position` is 1-based WITHIN THIS PAGE, which is why `page` has to travel
    with it — §18's arithmetic bug, where 60 of 119 rows claimed a position
    another row already held because the page number was never threaded in.
    """
    rows: List[Product] = []
    for i, node in enumerate(flatten_products(payload), start=1):
        row = product_from_api(node, page=page, position=i,
                               data_source=data_source, host=host)
        if row is not None:
            rows.append(row)
    return rows


# ===========================================================================
# The second view: reconciling against the rendered tile
# ===========================================================================
_STOCKCODE_IN_HREF_RE = re.compile(r"/shop/productdetails/(\d+)")


def stockcode_from_href(href: str) -> Optional[str]:
    m = _STOCKCODE_IN_HREF_RE.search(href or "")
    return m.group(1) if m else None


_PRICE_IN_TEXT_RE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)")


def price_from_tile_text(text: str) -> Optional[float]:
    """The first dollar amount in a tile's rendered text.

    Woolworths writes prices one way — `$3.80`, a dot decimal and a comma
    thousands separator — so the three grouping conventions §4 catalogues do
    not all arise here. The comma is still stripped, because `$1,234.56` is
    reachable on a bulk item and would otherwise parse as 1.
    """
    m = _PRICE_IN_TEXT_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def dom_tiles_to_index(tiles: Sequence[dict]) -> Dict[str, dict]:
    """{stockcode: tile} from what an engine read out of the shadow roots.

    Each tile is `{"href": ..., "price_text": ..., "cup_text": ...}` as the
    engine's driver produced it — this module never touches a browser.

    Keying on the stockcode in the tile's OWN link is what makes §4's
    neighbour-tile trap unreachable: a "you might also like" carousel tile
    carries its own product's link, so it can only ever index itself. 25 of
    the 67 tiles on one category page were carousel tiles, and a scope-by-
    position approach would have mixed them into the grid's rows.
    """
    index: Dict[str, dict] = {}
    for tile in tiles or []:
        if not isinstance(tile, dict):
            continue
        code = stockcode_from_href(tile.get("href") or "")
        if code and code not in index:
            index[code] = tile
    return index


def overlay_dom_prices(rows: Sequence[Product],
                       tiles: Sequence[dict]) -> Tuple[int, int]:
    """Confirm each row's price against the rendered tile. (confirmed, checked)

    This is a CONFIRMATION, not a correction. The API is the site's own
    structured answer and the tile is the same number after rendering; where
    they agree the row's `price_source` becomes `api+dom`, and where they
    disagree the row is LEFT ALONE and a warning names the sku (§4:
    "overwriting a correct row is worse than leaving one uncorrected").

    Returns (rows confirmed, rows that had a tile to check against) so the
    caller can log the coverage and warn when it falls.
    """
    index = dom_tiles_to_index(tiles)
    if not index:
        return 0, 0

    confirmed = checked = 0
    for row in rows:
        tile = index.get(str(row.sku or ""))
        if tile is None:
            continue
        shown = price_from_tile_text(tile.get("price_text") or "")
        if shown is None:
            continue
        checked += 1
        if row.price is not None and abs(shown - row.price) < 0.005:
            row.price_source = "api+dom"
            confirmed += 1
        else:
            logger.warning(
                "sku %s: API price %s but the rendered tile shows %s — "
                "leaving the row as the API gave it",
                row.sku, row.price, shown)
    return confirmed, checked


# ===========================================================================
# What to ask the API for
# ===========================================================================
# The request bodies are SITE KNOWLEDGE and therefore live here rather than
# in the engines — three copies of a payload shape is three chances for one
# engine to ask a slightly different question and get a slightly different
# catalogue back.
#
# They are plain dicts. No JavaScript crosses this boundary in either
# direction (§1); each engine serialises these itself, in its own driver's
# dialect.
#
# Both shapes are copied from the requests the SITE'S OWN UI sends, captured
# off the wire on 2026-09-16, rather than invented. That matters twice over:
# a field the UI always sends is a field the API may come to require, and a
# request that does not look like the UI's is the kind of difference a bot
# manager scores on. The one deliberate difference is `enableAdReRanking`,
# which is discussed below.


def search_request_body(term: str, page: int = 1,
                        page_size: int = PAGE_SIZE) -> dict:
    """The body for `POST /apis/ui/Search/products`."""
    location = f"/shop/search/products?searchTerm={term}"
    return {
        "SearchTerm": term,
        "PageNumber": int(page),
        "PageSize": int(page_size),
        "SortType": "TraderRelevance",
        "Filters": [],
        "IsSpecial": False,
        "Location": location,
        "formatObject": json.dumps({"name": term}),
        "isBundle": False,
        "isMobile": False,
        "isHideUnavailableProducts": False,
        "isRegisteredRewardCardPromotion": False,
        # The UI sends this false and so do we. Turning it on asks the site
        # to re-rank the promoted slots, which changes WHICH ads come back
        # without changing how many — and the ads are the rows this repo
        # already has to reason about carefully enough (see `organic_count`).
        "enableAdReRanking": False,
        "groupEdmVariants": True,
        "categoryVersion": "v2",
    }


def category_request_body(category_id: str, slug: str, page: int = 1,
                          page_size: int = PAGE_SIZE,
                          display_name: str = "") -> dict:
    """The body for `POST /apis/ui/browse/category`.

    `category_id` is the opaque node id, NOT the slug — see
    `category_id_for_slug`. Sending the slug returns HTTP 200 with
    `TotalRecordCount: 0` and no products, which is the shape of a real empty
    category: a run would report success on an empty answer. That is measured
    (`categoryId: "bakery"` -> 0 rows, `categoryId: "1_DEB537E"` -> 575) and
    it is why the engines refuse to ask until the slug has resolved.
    """
    url_path = f"/shop/browse/{slug}"
    return {
        "categoryId": category_id,
        "pageNumber": int(page),
        "pageSize": int(page_size),
        "sortType": "TraderRelevance",
        "url": url_path,
        "location": url_path,
        "formatObject": json.dumps({"name": display_name or slug}),
        "isSpecial": False,
        "isHideUnavailableProducts": False,
        "isBundle": False,
        "isMobile": False,
        "isRegisteredRewardCardPromotion": False,
        "isHideEverydayMarketProducts": False,
        "filters": [],
        "categoryVersion": "v2",
    }


def api_request_for(mode: str, *, term: Optional[str] = None,
                    category_id: Optional[str] = None,
                    slug: Optional[str] = None, page: int = 1,
                    page_size: int = PAGE_SIZE) -> Tuple[str, dict, str]:
    """(path, body, data_source) for one page of one mode.

    One place decides what a page request looks like, so the three engines
    cannot drift about it.
    """
    if mode == "search":
        if not term:
            raise ValueError("search mode needs a search term")
        return (API_SEARCH_PATH, search_request_body(term, page, page_size),
                "api-search")
    if mode == "category":
        if not category_id:
            raise ValueError("category mode needs a resolved category id")
        return (API_CATEGORY_PATH,
                category_request_body(category_id, slug or "", page, page_size),
                "api-category")
    raise ValueError(f"unknown mode {mode!r}")
