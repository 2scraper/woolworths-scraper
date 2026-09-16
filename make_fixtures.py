#!/usr/bin/env python3
"""make_fixtures.py — cut `fixtures_generated.json` from real captures.

The offline suite's fixtures come from REAL responses rather than from hand-
written JSON (CLAUDE.md §10). This script is how they are produced, so the
next person can reproduce them instead of trusting a blob.

    python3 make_fixtures.py --captures ./captures --out fixtures_generated.json

What goes in, and why each one earns its place:

    api_search        one page of POST /apis/ui/Search/products, trimmed to a
                      handful of products but with the GROUP WRAPPER shape
                      intact — that wrapper is the trap `flatten_products`
                      exists for, and a fixture that flattened it would test
                      nothing.
    api_category      the same for POST /apis/ui/browse/category, whose list
                      is called `Bundles` rather than `Products`.
    api_empty         a search that matched nothing: `SearchResultsCount` 0.
                      The only thing that can tell an empty listing from a
                      broken parser on this site.
    api_ads_only      a page PAST the end of a category: HTTP 200,
                      `Success: true`, and nothing but sponsored rows. The
                      single most important behavioural fixture here.
    denial_dom        Akamai's refusal as a BROWSER serialises it.
    denial_raw        the same refusal as raw bytes, where the punctuation is
                      entity-escaped. Both, because a marker that survives
                      only one of them silently misses an entire engine
                      (§20).
    served_page       a page the site really served, WHOLE. Not truncated,
                      and that is deliberate: the two markers this repo got
                      wrong are `akamai` (first occurrence at byte 347,429)
                      and "couldn't find any" (at 492,089), so any head short
                      enough to be tidy would cut away the very strings the
                      suite exists to pin, and the counts would come back 0
                      for the wrong reason. A fixture that makes a check pass
                      by omission is worse than no fixture.

Nothing here carries session material or personal data: a Woolworths listing
response is a product catalogue, and the fields that could identify a
shopper (`IsInTrolley`, `QuantityInTrolley`, `HasBeenBoughtBefore`) are
anonymous zeros on an unauthenticated fetch. They are dropped anyway, along
with the ad tokens, which are per-request and meaningless later — see
`_scrub`. A guard in smoke_test.py checks the committed file for their
SHAPES rather than for the old literals, so the next capture is caught too.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys

# Per-request advertising tokens. Long, opaque, meaningless after the fact,
# and exactly the kind of thing that should not sit in a public repo — scrubbed
# by NAME here and by PATTERN in the suite.
_AD_TOKEN_FIELDS = ("AdID", "AttributionToken", "AttributionTokenRef")
# Anything that would describe a SHOPPER rather than the shop.
_SHOPPER_FIELDS = ("IsInTrolley", "QuantityInTrolley", "HasBeenBoughtBefore",
                   "IsPersonalisedByPurchaseHistory")

# Kept on every trimmed product. Everything the parser reads, plus the few
# fields a future reader would want to see beside them.
_KEEP = (
    "Stockcode", "Barcode", "DisplayName", "Name", "Brand", "Description",
    "Variety", "Price", "WasPrice", "SavingsAmount", "CupPrice", "CupMeasure",
    "CupString", "PackageSize", "Unit", "IsOnSpecial", "IsHalfPrice",
    "IsSponsoredAd", "AdStatus", "OfferId", "IsAvailable", "IsInStock",
    "IsPurchasable", "SupplyLimit", "LargeImageFile", "MediumImageFile",
    "UrlFriendlyName", "AdditionalAttributes",
)
_KEEP_ATTRS = (
    "sapdepartmentname", "sapcategoryname", "sapsubcategoryname",
    "healthstarrating", "lifestyleanddietarystatement", "allergystatement",
    "ingredients", "storageinstructions", "countryoforigin",
)


def _scrub(product: dict) -> dict:
    """One product, trimmed to what the parser reads and stripped of tokens."""
    out = {k: product[k] for k in _KEEP if k in product}
    for field in _AD_TOKEN_FIELDS + _SHOPPER_FIELDS:
        out.pop(field, None)
    attrs = product.get("AdditionalAttributes")
    if isinstance(attrs, dict):
        out["AdditionalAttributes"] = {k: attrs[k] for k in _KEEP_ATTRS
                                       if k in attrs}
    return out


def _sample(groups: list, keep: int) -> list:
    """`keep` groups SPREAD across the list, not the first `keep`.

    The first groups of a Woolworths listing are the promoted block — all
    four of the first four on one search were `IsSponsoredAd: true` — so
    taking a prefix produced a fixture with no organic row in it and nothing
    to assert a discount or a was-price against. Sampling evenly keeps both
    kinds.
    """
    if len(groups) <= keep:
        return list(groups)
    step = len(groups) / float(keep)
    return [groups[int(i * step)] for i in range(keep)]


def _trim_payload(payload: dict, keep: int = 6) -> dict:
    """Keep `keep` group wrappers, WITH their wrapper shape intact.

    The wrapper is the point: `Products`/`Bundles` is a list of
    `{"Products": [...]}` objects, and a fixture that flattened it would make
    `flatten_products`'s own test vacuous.
    """
    out = dict(payload)
    for key in ("Products", "Bundles"):
        if isinstance(payload.get(key), list):
            groups = []
            for group in _sample(payload[key], keep):
                g = dict(group)
                g["Products"] = [_scrub(p) for p in (group.get("Products") or [])]
                groups.append(g)
            out[key] = groups
    # Drop the response-level advertising tokens too.
    for field in _AD_TOKEN_FIELDS:
        out.pop(field, None)
    for noisy in ("Aggregations", "FacetFilters", "UpperDynamicContent",
                  "LowerDynamicContent", "RichRelevancePlacement",
                  "VisualShoppingAisleResponse", "SeoMetaTags", "Passes",
                  "PersonalizedViewTypes", "Corrections"):
        out.pop(noisy, None)
    return out


def verify_equivalence(captures: pathlib.Path, fixtures: dict) -> list:
    """Every product kept in a fixture must parse to the SAME row as it does
    in the untrimmed original.

    CLAUDE.md §15 step 3 asks for exactly this before a trimmed fixture is
    committed, and it is easy to skip because the trimmed file looks fine on
    its own. What it catches is a `_scrub` that drops a field the parser
    reads: the fixture would still parse, still produce rows, and quietly
    disagree with reality on one column — so the suite would be asserting
    against a fiction.

    Returns a list of human-readable differences; empty means equivalent.
    """
    import product_parser as P

    problems = []
    pairs = [("api_search", "api_search.json"),
             ("api_category", "api_category.json"),
             ("api_ads_only", "api_ads_only.json"),
             ("api_empty", "api_empty.json")]

    for key, raw_name in pairs:
        raw_path = captures / raw_name
        if not raw_path.is_file():
            problems.append(f"{key}: {raw_name} missing, cannot verify")
            continue
        raw = json.loads(raw_path.read_text(encoding="utf-8"))

        full = {r.sku: r for r in P.products_from_payload(raw, page=1)}
        trimmed = {r.sku: r for r in P.products_from_payload(fixtures[key], page=1)}

        if not trimmed and full:
            problems.append(f"{key}: the trimmed fixture parses to nothing "
                            f"while the original yields {len(full)}")
            continue

        for sku, row in trimmed.items():
            original = full.get(sku)
            if original is None:
                problems.append(f"{key}: sku {sku} is in the fixture but not "
                                f"in the original capture")
                continue
            for field in dataclasses.fields(row):
                # `position` legitimately differs: the fixture keeps a subset
                # of the groups, so a product's index within the page moves.
                # Everything else must be byte-identical.
                if field.name in ("scraped_at", "position"):
                    continue
                a = getattr(row, field.name)
                b = getattr(original, field.name)
                if a != b:
                    problems.append(
                        f"{key}: sku {sku}.{field.name} is {a!r} in the "
                        f"fixture and {b!r} in the original")
    return problems


def build(captures: pathlib.Path) -> dict:
    """Assemble the fixture bundle from a directory of real captures."""
    def read_json(name):
        path = captures / name
        if not path.is_file():
            print(f"  ! missing {name}", file=sys.stderr)
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def read_text(name, limit=None):
        path = captures / name
        if not path.is_file():
            print(f"  ! missing {name}", file=sys.stderr)
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[:limit] if limit else text

    fixtures = {
        "_README": (
            "Generated by make_fixtures.py from real captures taken "
            "2026-09-16. Products are TRIMMED (a subset of fields, a few "
            "rows) but never edited: every value is the site's own. The "
            "group-wrapper shape is preserved deliberately. Advertising "
            "tokens and trolley fields are removed; smoke_test.py checks "
            "for their SHAPES so a future capture is caught too."),
        "api_search": _trim_payload(read_json("api_search.json") or {}),
        "api_category": _trim_payload(read_json("api_category.json") or {}),
        "api_empty": _trim_payload(read_json("api_empty.json") or {}),
        "api_ads_only": _trim_payload(read_json("api_ads_only.json") or {}, keep=8),
        "categories_head": (read_json("categories.json") or {}),
        "denial_dom": read_text("denial_dom.html"),
        "denial_raw": read_text("denial_raw.html"),
        "served_page": read_text("served.html"),
    }
    # The category tree is 2,700 nodes; keep only the top level plus one
    # branch, which is all `category_id_for_slug` needs to be tested on.
    tree = fixtures["categories_head"]
    if isinstance(tree, dict) and isinstance(tree.get("Categories"), list):
        kept = []
        for node in tree["Categories"]:
            slim = {k: node.get(k) for k in ("NodeId", "Description",
                                             "UrlFriendlyName")}
            children = node.get("Children") or []
            slim["Children"] = [
                {k: c.get(k) for k in ("NodeId", "Description",
                                       "UrlFriendlyName")}
                for c in children[:4]]
            kept.append(slim)
        fixtures["categories_head"] = {"Categories": kept}
    return fixtures


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--captures", default="./captures", type=pathlib.Path,
                   help="Directory holding the raw captures (default ./captures)")
    p.add_argument("--out", default="fixtures_generated.json", type=pathlib.Path)
    args = p.parse_args()

    if not args.captures.is_dir():
        print(f"No such directory: {args.captures}", file=sys.stderr)
        return 2
    fixtures = build(args.captures)

    # Verified BEFORE the file is written, so a fixture that disagrees with
    # its own source never reaches the repo (§15 step 3).
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    problems = verify_equivalence(args.captures, fixtures)
    if problems:
        print("TRIMMED FIXTURES DO NOT MATCH THEIR ORIGINALS:", file=sys.stderr)
        for problem in problems:
            print("  -", problem, file=sys.stderr)
        print("Nothing written.", file=sys.stderr)
        return 1
    print("equivalence: every kept product parses identically to the "
          "untrimmed original")

    args.out.write_text(json.dumps(fixtures, indent=1, ensure_ascii=False),
                        encoding="utf-8")
    size = args.out.stat().st_size
    print(f"Wrote {args.out} ({size:,} bytes)")
    for key, value in fixtures.items():
        if key.startswith("_"):
            continue
        if isinstance(value, str):
            print(f"  {key:18s} {len(value):,} chars")
        else:
            print(f"  {key:18s} {len(json.dumps(value)):,} chars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
