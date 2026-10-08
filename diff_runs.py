#!/usr/bin/env python3
"""
diff_runs.py
-------------
Compares two output files from this project (JSON, as written by
output_writer.save) and reports what changed between them, keyed on `sku` —
the Woolworths stockcode, which is what the README tells people to diff on
for price monitoring.

    python3 diff_runs.py --old milk.2026-09-01.json \\
                          --new milk.2026-09-07.json

Typical use is a scheduled re-run of one of the engines, kept under a dated
filename, diffed against the previous one:

    python3 playwright_scraper.py --url "$URL" --out "milk_$(date +%F)"
    python3 diff_runs.py --old "$(ls -t milk_*.json | sed -n 2p)" \\
                          --new "milk_$(date +%F).json" --out diff.json

Four buckets, each keyed on sku:

  added          — sku present in --new, absent from --old
  removed        — sku present in --old, absent from --new (delisted, or just
                   off this particular search/category run)
  changed        — sku present in both, with a different price,
                   original_price, discount_pct, currency, unit price
                   (cup_price), special / half-price flag or stock flag
  source_changed — sku present in both with a different price, but also a
                   different `price_source`: one run read the price from the
                   API with the rendered tile agreeing (`api+dom`) and the
                   other from the API alone (`api`) or from the tile alone
                   (`dom`). Reported separately because it says something
                   about our own two snapshots, not about the shop — and
                   --fail-on-change deliberately ignores it.

A product this project's parser could not recover a sku for (None) cannot be
matched across runs at all, so it is counted and reported separately rather
than silently folded into "added"/"removed", which would be wrong on its face.

This file was medium-scraper's until 2026-09-23: it tracked claps, responses
and reading time, none of which a Woolworths row has, so a real price move
diffed as "0 changed". The check that pins the fix moves one price on a real
sample row and requires it to be reported.
"""

import argparse
import json
import pathlib
import re
import sys
from typing import Dict, List, Optional, Tuple

from output_writer import UNIQUE_BY_SKU_MODES

# What a price monitor on a supermarket watches. Each of these is a column
# the row carries (see output_writer.Product):
#
#   price, original_price, discount_pct, currency   the shelf price and the
#                                                   was-price it is cut from
#   cup_price       the unit price ("$2.35 / 1L"), which is how a 2L bottle
#                   is compared against a 3L one, and which can move when a
#                   pack size changes while the shelf price does not
#   is_on_special, is_half_price   a promotion starting or ending
#   is_in_stock     a product going out of stock
#
# NOT tracked: `is_sponsored`, which says whether this fetch served the row
# as a promoted ad rather than anything about the product, and the
# descriptive columns (ingredients, allergens, health stars), which change
# rarely and would make a price diff noisy.
TRACKED_FIELDS = ("price", "original_price", "discount_pct", "currency",
                  "cup_price", "is_on_special", "is_half_price",
                  "is_in_stock")

# The subset of TRACKED_FIELDS whose comparability depends on price_source
# matching between the two runs — see diff_products.
PRICE_FIELDS = ("price", "original_price", "discount_pct", "cup_price")


def _load(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _by_sku(products: List[dict]) -> Tuple[Dict[str, dict], int]:
    indexed = {}
    unmatchable = 0
    for p in products:
        sku = p.get("sku")
        if sku is None:
            unmatchable += 1
            continue
        # A run's own output can already hold a duplicate sku (two rows in the
        # same category, or a rerun of dedupe_by_sku's job on older output
        # written before it existed) — keep the first and count the rest as
        # unmatchable rather than letting one clobber the other silently.
        if sku in indexed:
            unmatchable += 1
            continue
        indexed[sku] = p
    return indexed, unmatchable


def _within_tolerance(before: dict, after: dict, changes: dict,
                      tolerance_pct: float) -> bool:
    """True if every differing price field moved by less than `tolerance_pct`.

    Inherited from this family, where a sibling site converts prices for a
    cross-border visitor and the exchange rate ticks between two runs.
    Woolworths quotes AUD to everyone and converts nothing, so every cent of
    a difference is a real price move. The flag stays available and DEFAULTS
    TO ZERO; set it non-zero only with a reason you can state.

    A move is judged on the LARGEST relative change among the price fields,
    so a genuine cut is not hidden by a tolerance applied field-by-field.
    """
    if tolerance_pct <= 0:
        return False
    for field in PRICE_FIELDS:
        if field not in changes:
            continue
        was, now = before.get(field), after.get(field)
        if not isinstance(was, (int, float)) or not isinstance(now, (int, float)):
            return False  # a None appearing or disappearing is a real change
        if was == 0:
            return False
        if abs(now - was) / abs(was) * 100.0 > tolerance_pct:
            return False
    return True


def diff_products(old: List[dict], new: List[dict],
                  price_tolerance_pct: float = 0.0) -> dict:
    """The four buckets, plus the tolerance bucket."""
    old_by_sku, old_unmatchable = _by_sku(old)
    new_by_sku, new_unmatchable = _by_sku(new)

    added = [new_by_sku[sku] for sku in new_by_sku.keys() - old_by_sku.keys()]
    removed = [old_by_sku[sku] for sku in old_by_sku.keys() - new_by_sku.keys()]

    changed, source_changed, within_tolerance, lifecycle = [], [], [], []
    for sku in old_by_sku.keys() & new_by_sku.keys():
        before, after = old_by_sku[sku], new_by_sku[sku]
        field_changes = {
            field: {"old": before.get(field), "new": after.get(field)}
            for field in TRACKED_FIELDS
            if before.get(field) != after.get(field)
        }
        if not field_changes:
            continue

        # A row whose price_source differs between runs is not comparable on
        # price: one run had the API price confirmed against a rendered tile
        # and the other did not, or fell back to the tile alone. The figures
        # should agree, and when they do not, the difference is in how OUR
        # two snapshots rendered, not in what the shop charges. Non-price
        # fields still compare fine.
        sources = (before.get("price_source"), after.get("price_source"))
        if sources[0] != sources[1] and any(f in field_changes for f in PRICE_FIELDS):
            price_part = {f: v for f, v in field_changes.items() if f in PRICE_FIELDS}
            other_part = {f: v for f, v in field_changes.items() if f not in PRICE_FIELDS}
            source_changed.append({
                "sku": sku, "title": after.get("title"),
                "price_source": {"old": sources[0], "new": sources[1]},
                "changes": price_part,
            })
            field_changes = other_part
            if not field_changes:
                continue

        # A move inside the tolerance — see _within_tolerance. Only when the
        # ONLY differences are price fields: a stock or promotion change
        # alongside is a real change whatever the size of the move.
        if (all(f in PRICE_FIELDS for f in field_changes)
                and _within_tolerance(before, after, field_changes,
                                      price_tolerance_pct)):
            within_tolerance.append({"sku": sku, "title": after.get("title"),
                                     "changes": field_changes})
            continue

        changed.append({"sku": sku, "title": after.get("title"),
                        "changes": field_changes})

    # `lifecycle` is emitted always empty, so a consumer written against the
    # family's diff shape does not have to branch.
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "source_changed": source_changed,
        "within_tolerance": within_tolerance,
        "lifecycle": lifecycle,
        "unmatchable_old": old_unmatchable,
        "unmatchable_new": new_unmatchable,
    }


def _print_summary(result: dict) -> None:
    print(f"[+] {len(result['added'])} added, {len(result['removed'])} removed, "
          f"{len(result['changed'])} changed, "
          f"{len(result['source_changed'])} not comparable on price, "
          f"{len(result.get('within_tolerance', []))} within the price "
          f"tolerance.")
    for p in result["added"]:
        print(f"  + {p.get('sku')}  {p.get('title')}  {p.get('price')} {p.get('currency')}")
    for p in result["removed"]:
        print(f"  - {p.get('sku')}  {p.get('title')}  {p.get('price')} {p.get('currency')}")
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {deltas}")
    for c in result.get("within_tolerance", []):
        moves = ", ".join(
            f"{f}: {v['old']} -> {v['new']}" for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {moves}  [within "
              f"--price-tolerance-pct]")
    for c in result["source_changed"]:
        src = c["price_source"]
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ? {c['sku']}  {c['title']}  {deltas}  "
              f"[price_source {src['old']!r} -> {src['new']!r}: the two runs "
              f"read the price differently, so this is not a site-side "
              f"price change]")
    unmatchable = result["unmatchable_old"] + result["unmatchable_new"]
    if unmatchable:
        print(f"[!] {unmatchable} row(s) across both files had no sku or a "
              f"duplicate sku, and could not be matched across runs.")


def _run_status(path: str) -> Tuple[Optional[str], Optional[dict]]:
    """Read the `<out>.meta.json` sidecar beside a run's JSON output.

    Returns (status, meta), or (None, None) when there is no sidecar.

    That used to be described here as "the normal case for a single-page
    run", and it is not: `finish_run` writes a sidecar whenever it writes
    rows, so every run this tool is meant to read has one. A file without
    one is output from before the sidecar existed, a hand-edited file, or
    the leftovers of a run that failed — and none of those can be checked
    for completeness, mode, listing or store. `_check_comparable` therefore
    REFUSES it rather than skipping the checks in silence.
    """
    meta_path = re.sub(r"\.json$", "", path) + ".meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    return meta.get("status"), meta


def _check_comparable(args) -> bool:
    """Refuse an assortment diff between runs that are not both complete.

    This is the failure mode the sidecar exists for: a run cut short on page
    3 of 10 is missing every product on pages 4-10, and diffing it against
    yesterday's full run reports all of them as `removed` — reading as "these
    products were delisted" when in fact they were simply never fetched.
    Prices of the SKUs both runs DID see are still comparable, which is why
    this is a refusal with a --force escape hatch rather than a hard error.
    """
    problems = []
    modes = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        status, meta = _run_status(path)
        if status is None:
            # STRICT. Without a sidecar nothing below can be checked — not
            # completeness, not mode, not which listing or which store —
            # so skipping them quietly is how a diff of two unrelated runs
            # reports 100% churn and looks like news. `--force` is the
            # opt-out, same as for a partial run.
            problems.append(
                f"{label} ({path}) has no .meta.json beside it, so there is "
                f"no way to tell whether it was complete, what listing it "
                f"read or which store served it. Every check below is "
                f"blind on this file.")
            continue
        mode = (meta or {}).get("mode")
        if mode:
            modes[label] = mode
        if mode and mode not in UNIQUE_BY_SKU_MODES:
            # This tool's whole premise is one row per `sku`, diffed on
            # price. A mode that produces many rows per sku would give a diff
            # whose every line is an artefact of two rows sharing an id, so
            # it is refused outright rather than answered. Both of this
            # repo's current modes qualify; the check is here so that adding
            # one that does not is caught rather than discovered.
            problems.append(
                f"{label} ({path}) is a {mode!r} run, which is not one row "
                f"per sku. This tool diffs one row per sku, so there is "
                f"nothing here it can compare.")
        if status != "complete":
            problems.append(
                f"{label} ({path}) was a {status!r} run — stopped after "
                f"{meta.get('pages_completed')} of {meta.get('pages_requested')} "
                f"page(s), reason {meta.get('stop_reason')!r}")
    if len(set(modes.values())) > 1:
        problems.append(
            f"the two runs are different modes ({modes}). A listing row and a "
            f"detail row carry different fields, so `added`/`removed` would "
            f"describe the mode change rather than the catalogue.")

    # WHICH ADDRESS ANSWERED.
    #
    # `source` is the host that served a row, and on this site there is only
    # one: `www.woolworths.com.au`. So unlike the sibling repos this check is
    # nearly always quiet, and that is fine — it costs one comparison and it
    # catches the one case that matters, which is a pair of runs where the
    # column is not what the reader assumes.
    #
    # There is deliberately no language or country check either, and no
    # marketplace check: Woolworths Online is one catalogue in one currency.
    # `woolworths.co.nz` and `woolworths.co.za` are different companies and
    # `product_parser` refuses both by name, so a run cannot quietly hold
    # rows from one of them.
    # WHICH LISTING each run read, from its own sidecar.
    #
    # Two complete runs of DIFFERENT categories passed every guard here and
    # diffed cleanly: same mode, same host, so `added` and `removed` were
    # the whole of both files — a 100% churn report about two things that
    # were never the same question. The sidecar already recorded
    # `start_url`, so the information was on disk and simply unread.
    #
    # Compared by the sidecar's `listing` where present and by `start_url`
    # otherwise, so a pair written before that field existed still compares.
    listings = {}
    stores = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        meta_path = pathlib.Path(str(path).rsplit(".json", 1)[0] + ".meta.json")
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        listings[label] = meta.get("listing") or meta.get("start_url")
        if meta.get("store_ids"):
            stores[label] = tuple(meta["store_ids"])
    if len(set(listings.values())) > 1:
        problems.append(
            f"the two runs read different listings ({listings}). Every row "
            f"would be reported added or removed, which says nothing about "
            f"the catalogue and everything about the two URLs.")
    if len(set(stores.values())) > 1:
        problems.append(
            f"the two runs were served different fulfilment stores "
            f"({stores}). Woolworths prices per store, so a price "
            f"difference here is a difference of PLACE rather than of time.")

    sources = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        try:
            rows = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        hosts = {r.get("source") for r in rows if r.get("source")}
        if len(hosts) == 1:
            sources[label] = hosts.pop()
    if len(set(sources.values())) > 1:
        problems.append(
            f"the two runs landed on different hosts ({sources}). Woolworths "
            f"Online serves one catalogue from one host, so this is a sign "
            f"the two runs are not describing the same shop — `added` and "
            f"`removed` would report the address change rather than anything "
            f"about the products.")

    if not problems:
        return True

    # A generic headline, because the reasons below are not only about
    # completeness: a mode or host mismatch is refused too, and a message
    # naming the wrong reason sends the reader looking in the wrong place.
    print("[!] Refusing to diff these two runs:")
    for line in problems:
        print(f"      {line}")
    print("    Re-run the incomplete side, or pass --force to compare anyway "
          "(added/removed will include products that were simply never "
          "fetched).")
    return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Diff two woolworths-scraper JSON outputs by sku.")
    p.add_argument("--old", required=True, help="Earlier run's JSON output.")
    p.add_argument("--new", required=True, help="Later run's JSON output.")
    p.add_argument("--out", default=None,
                   help="Write the full diff as JSON to this path too.")
    p.add_argument("--price-tolerance-pct", type=float, default=0.0,
                   metavar="PCT",
                   help="Treat a price move smaller than PCT%% as noise "
                        "rather than a price change: reported separately and "
                        "ignored by --fail-on-change. Default 0 — Woolworths "
                        "quotes AUD to everyone, so every cent is a real move. "
                        "Set it non-zero only with a reason you can state.")
    p.add_argument("--fail-on-change", action="store_true",
                   help="Exit 1 if anything was added, removed or changed — "
                        "for a cron job that should only notify on a real diff.")
    p.add_argument("--force", action="store_true",
                   help="Diff even when a run's .meta.json says it was partial "
                        "or failed. Products never fetched by the short run will "
                        "appear as added/removed.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.force and not _check_comparable(args):
        return 2

    try:
        old = _load(args.old)
        new = _load(args.new)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Could not read one of the input files: {e}")
        return 2

    result = diff_products(old, new, price_tolerance_pct=args.price_tolerance_pct)
    _print_summary(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[+] Full diff written to {args.out}")

    # Neither `source_changed` nor `within_tolerance` is a reason to fail.
    # The first means our two runs read the price differently; the second
    # means a move inside a tolerance the caller chose. Neither says
    # anything about the shop, and alerting on either would train whoever
    # reads the alert to ignore it.
    if args.fail_on_change and (result["added"] or result["removed"] or result["changed"]):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
