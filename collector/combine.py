"""Join the files of a sharded snapshot into one set, as if one process took it.

    python -m collector.combine --run-id 20261115T061705Z --shards data/shards

Expects <shards>/<anything>/{prices,catalog,runs}-<run>.parquet and latest.json
from each shard, and writes data/{prices,catalog,runs}-<run>.parquet plus the
run summary. Row groups are copied as they are, so the joined files still hold
one row group per store.

A shard that left no files is reported in the summary; its stores are simply
absent from the runs file, so later steps treat them as not read this time.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyarrow.parquet as pq


def join(parts: list[Path], out: Path) -> int:
    rows = 0
    tmp = out.with_suffix(".parquet.tmp")
    writer = None
    try:
        for part in parts:
            f = pq.ParquetFile(part)
            if writer is None:
                writer = pq.ParquetWriter(tmp, f.schema_arrow, compression="zstd", compression_level=9)
            for i in range(f.num_row_groups):
                table = f.read_row_group(i)
                writer.write_table(table)
                rows += table.num_rows
    finally:
        if writer is not None:
            writer.close()
    if writer is None:
        return 0
    tmp.rename(out)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--shards", type=Path, default=Path("data/shards"))
    ap.add_argument("--expected", type=int, default=0, help="number of shards that should have run")
    ap.add_argument("--out", type=Path, default=Path("data"))
    ap.add_argument("--status", type=Path, default=Path("data/latest.json"))
    ap.add_argument("--min-ok", type=float, default=0.5)
    args = ap.parse_args()

    run = args.run_id
    dirs = sorted(d for d in args.shards.iterdir() if (d / f"runs-{run}.parquet").exists()) if args.shards.exists() else []
    if not dirs:
        print(f"no shard files for {run} under {args.shards}", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for kind in ("prices", "catalog", "runs"):
        parts = [d / f"{kind}-{run}.parquet" for d in dirs if (d / f"{kind}-{run}.parquet").exists()]
        counts[kind] = join(parts, args.out / f"{kind}-{run}.parquet")

    summaries = []
    for d in dirs:
        try:
            summaries.append(json.loads((d / "latest.json").read_text()))
        except (OSError, ValueError):
            pass
    runs = pq.read_table(args.out / f"runs-{run}.parquet").to_pylist()
    ok = [r for r in runs if r["ok"]]
    failed = [r for r in runs if not r["ok"]]
    first = summaries[0] if summaries else {}
    summary = {
        "run_id": run,
        "vantage": first.get("vantage", runs[0]["vantage"] if runs else ""),
        "catalog": first.get("catalog", runs[0]["catalog"] if runs else ""),
        "shards": len(dirs),
        "shards_missing": max(0, args.expected - len(dirs)),
        "stores_ok": len(ok),
        "stores_failed": len(failed),
        "stores_skipped": sum((r["error"] or "").startswith("skipped") for r in failed),
        "stores_truncated": sum(bool(r["truncated"]) for r in ok),
        "products": sum(r["products"] or 0 for r in ok),
        "variants": counts["prices"],
        "catalog_rows": counts["catalog"],
        "seconds": max((s.get("seconds", 0) for s in summaries), default=0),
        "throttle_pauses": sum(s.get("throttle_pauses", 0) for s in summaries),
        "prices_bytes": (args.out / f"prices-{run}.parquet").stat().st_size,
        "catalog_bytes": (args.out / f"catalog-{run}.parquet").stat().st_size,
        "failures": {r["store"]: r["error"] for r in sorted(failed, key=lambda r: r["store"])},
    }
    args.status.parent.mkdir(parents=True, exist_ok=True)
    args.status.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "failures"}))

    share = len(ok) / len(runs) if runs else 0.0
    if share < args.min_ok or summary["shards_missing"]:
        print(f"{share:.0%} of stores read; {summary['shards_missing']} shard(s) missing", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
