"""Take one snapshot of every store in stores/stores.csv.

    python -m collector.snapshot

Writes two Parquet files per run into data/:
  snap-<run_id>.parquet   one row per product variant: price, compare-at price, stock
  runs-<run_id>.parquet   one row per store: did the fetch work, how much it covered
and latest.json, a small summary of the run.

The runs file matters as much as the prices: a variant missing from a snapshot
only means "removed from the store" if that store's fetch succeeded in that run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .discover import load_stores
from .http import LIMITER
from .shopify import StoreResult, fetch_store

TS = pa.timestamp("s", tz="UTC")
SNAP_SCHEMA = pa.schema(
    [
        ("run_id", pa.string()),
        ("fetched_at", TS),
        ("store", pa.string()),
        ("currency", pa.string()),
        ("product_id", pa.int64()),
        ("variant_id", pa.int64()),
        ("handle", pa.string()),
        ("title", pa.string()),
        ("variant_title", pa.string()),
        ("vendor", pa.string()),
        ("product_type", pa.string()),
        ("tags", pa.string()),
        ("sku", pa.string()),
        ("price", pa.float64()),
        ("compare_at_price", pa.float64()),
        ("available", pa.bool_()),
        ("product_created_at", TS),
        ("product_published_at", TS),
    ]
)
RUNS_SCHEMA = pa.schema(
    [
        ("run_id", pa.string()),
        ("vantage", pa.string()),
        ("store", pa.string()),
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stores", type=Path, default=Path("stores/stores.csv"))
    ap.add_argument("--out", type=Path, default=Path("data"))
    ap.add_argument("--status", type=Path, default=Path("data/latest.json"), help="where to write the run summary")
    ap.add_argument("--workers", type=int, default=8, help="stores fetched in parallel")
    ap.add_argument("--max-pages", type=int, default=100, help="cap per store (250 products a page)")
    ap.add_argument("--limit", type=int, default=0, help="only the first N stores (for testing)")
    ap.add_argument("--min-ok", type=float, default=0.5, help="fail the run below this share of stores")
    args = ap.parse_args()

    stores = load_stores(args.stores)
    if args.limit:
        stores = stores[: args.limit]
    if not stores:
        print(f"no stores in {args.stores}; run `python -m collector.discover` first", file=sys.stderr)
        return 2
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    vantage = os.environ.get("BF_VANTAGE", "local")
    args.out.mkdir(parents=True, exist_ok=True)
    snap_path = args.out / f"snap-{run_id}.parquet"
    runs_path = args.out / f"runs-{run_id}.parquet"
    tmp_path = snap_path.with_suffix(".parquet.tmp")

    t0 = time.monotonic()
    results: list[StoreResult] = []
    lock = threading.Lock()
    total_rows = 0
    writer = pq.ParquetWriter(tmp_path, SNAP_SCHEMA, compression="zstd", compression_level=9)

    def run_batch(batch: list, label: str) -> None:
        nonlocal total_rows
        with ThreadPoolExecutor(args.workers) as pool:
            futures = [pool.submit(fetch_store, s, max_pages=args.max_pages) for s in batch]
            for done, fut in enumerate(as_completed(futures), 1):
                res = fut.result()
                n = len(res.rows)
                if n:
                    res.rows.sort(key=lambda r: (r["product_id"] or 0, r["variant_id"]))
                    for row in res.rows:
                        row["run_id"] = run_id
                    with lock:
                        writer.write_table(pa.Table.from_pylist(res.rows, schema=SNAP_SCHEMA))  # one row group per store
                    total_rows += n
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
        writer.close()
    tmp_path.rename(snap_path)  # readers never see a half-written file

    results.sort(key=lambda r: r.store.domain)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "run_id": run_id,
                    "vantage": vantage,
                    "store": r.store.domain,
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
        runs_path,
        compression="zstd",
    )

    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    summary = {
        "run_id": run_id,
        "vantage": vantage,
        "stores_ok": len(ok),
        "stores_failed": len(failed),
        "stores_truncated": sum(r.truncated for r in ok),
        "products": sum(r.products for r in ok),
        "variants": total_rows,
        "seconds": round(time.monotonic() - t0),
        "throttle_pauses": LIMITER.cooldowns,
        "snapshot_bytes": snap_path.stat().st_size,
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
