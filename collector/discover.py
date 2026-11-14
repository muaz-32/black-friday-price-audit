"""Probe candidate domains and keep the ones that serve a public product feed.

    python -m collector.discover

Reads stores/candidates.txt, writes stores/stores.csv (the list the snapshot
job uses) and stores/rejected.csv (why each of the others was dropped).
"""

from __future__ import annotations

import argparse
import csv
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, fields
from pathlib import Path

from .shopify import Store, probe

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
    with path.open(newline="") as f:
        return [Store(**{k: v or "" for k, v in row.items()}) for row in csv.DictReader(f)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--candidates", type=Path, default=Path("stores/candidates.txt"))
    ap.add_argument("--out", type=Path, default=Path("stores/stores.csv"))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--retry", metavar="TEXT", help="also re-probe rejected domains whose reason contains TEXT")
    ap.add_argument("--fresh", action="store_true", help="ignore existing results and probe everything")
    args = ap.parse_args()
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
        if d not in known and (d not in rejected or (args.retry and args.retry in rejected[d]))
    ]
    print(f"probing {len(todo)} candidates ({len(stores)} stores already known)...", flush=True)
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(probe, todo))

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
