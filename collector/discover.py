"""Probe candidate domains and keep the ones that serve a public product feed.

    python -m collector.discover

Reads stores/candidates.txt, writes stores/stores.csv (the list the snapshot
job uses) and stores/rejected.csv (why each of the others was dropped).

Each domain is tried as a Shopify store first, then as a WooCommerce shop.
"""

from __future__ import annotations

import argparse
import csv
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, fields
from pathlib import Path

from . import shopify, woocommerce
from .shopify import Store

PROBES = {"shopify": shopify.probe, "woocommerce": woocommerce.probe}
# Reasons that say nothing about the platform: the domain is gone, or it asked
# us to slow down or stay away. Trying another platform would not help.
FINAL = re.compile(r"Error|HTTP 429|HTTP 430|robots\.txt")

DOMAIN_RE = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$")


def read_candidates(path: Path) -> list[str]:
    seen, out = set(), []
    for line in path.read_text().splitlines():
        d = line.split("#", 1)[0].strip().lower()
        if d and DOMAIN_RE.match(d) and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def load_stores(path: Path) -> list[Store]:
    names = {f.name for f in fields(Store)}
    with path.open(newline="") as f:
        stores = [Store(**{k: v or "" for k, v in row.items() if k in names}) for row in csv.DictReader(f)]
    for s in stores:
        s.platform = s.platform or "shopify"  # lists written before platforms existed
    return stores


def probe(domain: str, platforms: list[str]) -> tuple[Store | None, str]:
    reasons = []
    for name in platforms:
        store, reason = PROBES[name](domain)
        if store is not None:
            return store, ""
        reasons.append(reason)
        if FINAL.search(reason):
            break
    return None, "; ".join(reasons)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--candidates", type=Path, default=Path("stores/candidates.txt"))
    ap.add_argument("--out", type=Path, default=Path("stores/stores.csv"))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--retry", metavar="REGEX", help="also re-probe rejected domains whose reason matches REGEX")
    ap.add_argument(
        "--platforms",
        default="shopify,woocommerce",
        help="platforms to try, in order (comma-separated: shopify, woocommerce)",
    )
    ap.add_argument("--fresh", action="store_true", help="ignore existing results and probe everything")
    args = ap.parse_args()
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]
    unknown = set(platforms) - set(PROBES)
    if unknown:
        ap.error(f"unknown platform(s): {', '.join(sorted(unknown))}")
    retry = re.compile(args.retry) if args.retry else None
    rejected_path = args.out.with_name("rejected.csv")

    stores: dict[str, Store] = {}
    rejected: dict[str, str] = {}
    if not args.fresh:
        if args.out.exists():
            stores = {s.shop_id or s.domain: s for s in load_stores(args.out)}
        if rejected_path.exists():
            with rejected_path.open(newline="") as f:
                rejected = {r["domain"]: r["reason"] for r in csv.DictReader(f)}

    known = {s.domain for s in stores.values()}
    todo = [
        d
        for d in read_candidates(args.candidates)
        if d not in known and (d not in rejected or (retry and retry.search(rejected[d])))
    ]
    print(f"probing {len(todo)} candidates ({len(stores)} stores already known)...", flush=True)
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda d: probe(d, platforms), todo))

    added = 0
    for domain, (store, reason) in zip(todo, results):
        rejected.pop(domain, None)
        if store is None:
            rejected[domain] = reason
            continue
        key = store.shop_id or store.domain  # two domains can front one shop
        if key in stores:
            if stores[key].domain != domain:
                rejected[domain] = f"same shop as {stores[key].domain}"
        else:
            stores[key] = store
            added += 1

    rows = sorted(stores.values(), key=lambda s: s.domain)
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[x.name for x in fields(Store)])
        w.writeheader()
        w.writerows(asdict(s) for s in rows)
    with rejected_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["domain", "reason"])
        w.writerows(sorted(rejected.items()))

    print(f"added {added}; {len(rows)} stores -> {args.out}")
    print(f"{len(rejected)} rejected -> {rejected_path}")


if __name__ == "__main__":
    main()
