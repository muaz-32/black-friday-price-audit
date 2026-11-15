"""Take one snapshot of every store in stores/stores.csv.

    python -m collector.snapshot

Writes three Parquet files per run into data/:
  prices-<run_id>.parquet    one row per product variant: price, compare-at price, stock
  catalog-<run_id>.parquet   what each variant is: title, handle, vendor, tags, sku
  runs-<run_id>.parquet      one row per store: did the fetch work, how much it covered
and latest.json, a small summary of the run.

Prices and catalogue are separate because the descriptive text is about 80% of
the bytes and barely changes between runs. The scheduled job writes the whole
catalogue once a day and, in its other runs, only products created or published
in the last 48 hours, so short-lived sale products are still described.

The runs file matters as much as the prices: a variant missing from a snapshot
only means "removed from the store" if that store's fetch succeeded in that run.

With --shard I/N this process reads only its share of the stores; the scheduled
job runs N shards side by side and `collector.combine` joins their files. Shares
are balanced by each store's expected number of requests and are the same in
every process, so the shards never overlap.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .discover import load_stores
from .http import LIMITER, OTHER_LIMITER
from . import shopify, woocommerce
from .shopify import Store, StoreResult

FETCHERS = {"shopify": shopify.fetch_store, "woocommerce": woocommerce.fetch_store}

TS = pa.timestamp("s", tz="UTC")
PRICES_SCHEMA = pa.schema(
    [
        ("run_id", pa.string()),
        ("fetched_at", TS),
        ("store", pa.string()),
        ("currency", pa.string()),
        ("product_id", pa.int64()),
        ("variant_id", pa.int64()),
        ("price", pa.float64()),
        ("compare_at_price", pa.float64()),
        ("available", pa.bool_()),
    ]
)
CATALOG_SCHEMA = pa.schema(
    [
        ("run_id", pa.string()),
        ("store", pa.string()),
        ("product_id", pa.int64()),
        ("variant_id", pa.int64()),
        ("handle", pa.string()),
        ("title", pa.string()),
        ("variant_title", pa.string()),
        ("vendor", pa.string()),
        ("product_type", pa.string()),
        ("tags", pa.string()),
        ("sku", pa.string()),
        ("product_created_at", TS),
        ("product_published_at", TS),
    ]
)
RUNS_SCHEMA = pa.schema(
    [
        ("run_id", pa.string()),
        ("vantage", pa.string()),
        ("catalog", pa.string()),
        ("store", pa.string()),
        ("platform", pa.string()),
        ("market", pa.string()),
        ("currency", pa.string()),
        ("ok", pa.bool_()),
        ("error", pa.string()),
        ("http_status", pa.int32()),
        ("pages", pa.int32()),
        ("products", pa.int32()),
        ("variants", pa.int32()),
        ("listed_products", pa.int32()),
        ("truncated", pa.bool_()),
        ("started_at", TS),
        ("seconds", pa.float64()),
    ]
)
RECENT = timedelta(hours=48)


def is_recent(row: dict, cutoff: datetime) -> bool:
    created, published = row["product_created_at"], row["product_published_at"]
    return bool((created and created >= cutoff) or (published and published >= cutoff))


def expected_requests(store: Store, max_pages: int) -> int:
    try:
        listed = int(store.listed_products)
    except ValueError:
        listed = 2500  # unknown: assume a mid-sized store
    if store.platform == "woocommerce":
        return 2 * min(max_pages, math.ceil(listed / woocommerce.PAGE_SIZE) + 1) + 1
    return min(max_pages, math.ceil(listed / shopify.PAGE_SIZE) + 1) + 1


def shard_of(stores: list[Store], index: int, count: int, max_pages: int) -> list[Store]:
    """Greedy balance: biggest stores first, each to the least loaded shard."""
    load = [0] * count
    mine = []
    for s in sorted(stores, key=lambda s: (-expected_requests(s, max_pages), s.domain)):
        target = load.index(min(load))
        load[target] += expected_requests(s, max_pages)
        if target == index:
            mine.append(s)
    return sorted(mine, key=lambda s: s.domain)


def parse_shard(value: str) -> tuple[int, int]:
    i, n = (int(x) for x in value.split("/"))
    if not 0 <= i < n:
        raise argparse.ArgumentTypeError("shard must be I/N with 0 <= I < N")
    return i, n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stores", type=Path, default=Path("stores/stores.csv"))
    ap.add_argument("--out", type=Path, default=Path("data"))
    ap.add_argument("--status", type=Path, default=Path("data/latest.json"), help="where to write the run summary")
    ap.add_argument(
        "--catalog",
        choices=("full", "recent", "auto"),
        default="full",
        help="full: describe every variant; recent: only products new in the last 48h; "
        "auto: full in the first run of the UTC day (before 06:00), recent otherwise",
    )
    ap.add_argument("--workers", type=int, default=8, help="stores fetched in parallel")
    ap.add_argument("--max-pages", type=int, default=100, help="cap per store (250 products a page)")
    ap.add_argument("--limit", type=int, default=0, help="only the first N stores (for testing)")
    ap.add_argument("--shard", type=parse_shard, default=(0, 1), metavar="I/N", help="read only share I of N (0-based)")
    ap.add_argument("--run-id", default="", help="use this run id (all shards of one run share it)")
    ap.add_argument(
        "--budget-minutes",
        type=float,
        default=0,
        help="stop starting new stores after this long, so the files are finished before a job timeout (0: no limit)",
    )
    ap.add_argument("--min-ok", type=float, default=0.5, help="fail the run below this share of stores")
    args = ap.parse_args()

    stores = load_stores(args.stores)
    if args.limit:
        stores = stores[: args.limit]
    if args.shard[1] > 1:
        stores = shard_of(stores, *args.shard, args.max_pages)
    if not stores:
        print(f"no stores in {args.stores}; run `python -m collector.discover` first", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    run_id = args.run_id or now.strftime("%Y%m%dT%H%M%SZ")
    vantage = os.environ.get("BF_VANTAGE", "local")
    catalog_mode = args.catalog
    if catalog_mode == "auto":
        catalog_mode = "full" if now.hour < 6 else "recent"
    cutoff = now - RECENT

    args.out.mkdir(parents=True, exist_ok=True)
    paths = {kind: args.out / f"{kind}-{run_id}.parquet" for kind in ("prices", "catalog", "runs")}
    tmp = {kind: paths[kind].with_suffix(".parquet.tmp") for kind in ("prices", "catalog")}
    writers = {
        "prices": pq.ParquetWriter(tmp["prices"], PRICES_SCHEMA, compression="zstd", compression_level=9),
        "catalog": pq.ParquetWriter(tmp["catalog"], CATALOG_SCHEMA, compression="zstd", compression_level=9),
    }

    t0 = time.monotonic()
    deadline = t0 + args.budget_minutes * 60 if args.budget_minutes else None
    results: list[StoreResult] = []
    counts = {"prices": 0, "catalog": 0}

    def fetch(store) -> StoreResult:
        # A run killed by the job timeout loses everything it fetched, so past
        # the budget the remaining stores are recorded as skipped instead.
        if deadline and time.monotonic() > deadline:
            return StoreResult(store=store, error="skipped: run time budget used up", started_at=datetime.now(timezone.utc))
        return FETCHERS[store.platform](store, max_pages=args.max_pages)

    def run_batch(batch: list, label: str) -> None:
        with ThreadPoolExecutor(args.workers) as pool:
            futures = [pool.submit(fetch, s) for s in batch]
            for done, fut in enumerate(as_completed(futures), 1):  # results are written on this thread only
                res = fut.result()
                n = len(res.rows)
                if n:
                    res.rows.sort(key=lambda r: (r["product_id"] or 0, r["variant_id"]))
                    for row in res.rows:
                        row["run_id"] = run_id
                    described = res.rows if catalog_mode == "full" else [r for r in res.rows if is_recent(r, cutoff)]
                    # One row group per store, so readers can skip to a store.
                    writers["prices"].write_table(pa.Table.from_pylist(res.rows, schema=PRICES_SCHEMA))
                    if described:
                        writers["catalog"].write_table(pa.Table.from_pylist(described, schema=CATALOG_SCHEMA))
                    counts["prices"] += n
                    counts["catalog"] += len(described)
                res.rows = []  # free memory; only the count is needed from here on
                res.variants = n  # type: ignore[attr-defined]
                results.append(res)
                mark = "ok " if res.ok else "ERR"
                note = "" if res.ok else f"  {res.error}"
                print(f"[{label} {done}/{len(batch)}] {mark} {res.store.domain}: {n} variants, {res.seconds}s{note}", flush=True)

    try:
        run_batch(stores, "pass 1")
        # Stores that were throttled get one more go once the rest are done;
        # by then the limiter has slowed down and any penalty has had time to lapse.
        throttled = [r for r in results if not r.ok and r.http_status in (429, 430)]
        if throttled:
            results = [r for r in results if r not in throttled]
            print(f"retrying {len(throttled)} throttled stores", flush=True)
            run_batch([r.store for r in throttled], "pass 2")
    finally:
        for w in writers.values():
            w.close()
    for kind in tmp:
        tmp[kind].rename(paths[kind])  # readers never see a half-written file

    results.sort(key=lambda r: r.store.domain)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "run_id": run_id,
                    "vantage": vantage,
                    "catalog": catalog_mode,
                    "store": r.store.domain,
                    "platform": r.store.platform,
                    "market": r.store.country,
                    "currency": r.currency,
                    "ok": r.ok,
                    "error": r.error,
                    "http_status": r.http_status,
                    "pages": r.pages,
                    "products": r.products,
                    "variants": r.variants,  # type: ignore[attr-defined]
                    "listed_products": r.listed_products,
                    "truncated": r.truncated,
                    "started_at": r.started_at,
                    "seconds": r.seconds,
                }
                for r in results
            ],
            schema=RUNS_SCHEMA,
        ),
        paths["runs"],
        compression="zstd",
    )

    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    summary = {
        "run_id": run_id,
        "vantage": vantage,
        "catalog": catalog_mode,
        "stores_ok": len(ok),
        "stores_failed": len(failed),
        "stores_skipped": sum(r.error.startswith("skipped") for r in failed),
        "stores_truncated": sum(r.truncated for r in ok),
        "products": sum(r.products for r in ok),
        "variants": counts["prices"],
        "catalog_rows": counts["catalog"],
        "seconds": round(time.monotonic() - t0),
        "throttle_pauses": LIMITER.cooldowns + OTHER_LIMITER.cooldowns,
        "shard": f"{args.shard[0]}/{args.shard[1]}",
        "prices_bytes": paths["prices"].stat().st_size,
        "catalog_bytes": paths["catalog"].stat().st_size,
        "failures": {r.store.domain: r.error for r in failed},
    }
    args.status.parent.mkdir(parents=True, exist_ok=True)
    args.status.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "failures"}))

    share = len(ok) / len(results) if results else 0.0
    if share < args.min_ok:
        print(f"only {share:.0%} of stores succeeded (minimum {args.min_ok:.0%})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
