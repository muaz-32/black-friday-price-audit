"""Boil one snapshot down to a row per store, for the dashboard.

    python -m collector.summarize --run 20261114T090046Z [--prev 20261114T030012Z]

Reads data/prices-<run>.parquet and data/runs-<run>.parquet and appends to
  summary/store_runs/<date>.csv   one row per store for this run
  summary/runs.csv                one row for the whole run

With --prev, each variant is also compared with the previous snapshot from the
same vantage point: how many prices fell or rose, and how many "discounts" grew
without the price being cut. Only stores fetched successfully in both runs are
compared; for the others those columns are left empty.

These files are small and committed to the repository; the raw snapshots stay
the source of truth and anything here can be recomputed from them.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import duckdb

STORE_COLS = [
    "run_id", "store", "ok", "variants", "in_stock", "marked_down", "median_discount", "median_price",
    "matched", "price_down", "price_up", "discount_inflated", "price_ratio", "new_variants", "gone_variants",
]  # fmt: skip
RUN_COLS = [
    "run_id", "prev_run_id", "vantage", "stores_ok", "stores_failed", "variants", "marked_down",
    "median_discount", "matched", "price_down", "price_up", "discount_inflated",
]  # fmt: skip
CENT = 0.005  # prices are compared to the nearest cent

# A variant counts as marked down when the store shows a higher compare-at price.
# "discount_inflated": the claimed discount exists now, the selling price was not
# cut, and the reference price is higher than both the old reference and the old
# price, i.e. the saving comes from raising the "was" price, not lowering the "now" one.
PER_STORE = f"""
WITH cur AS (
    SELECT store, variant_id, price, compare_at_price AS cmp, available FROM read_parquet(?)
), j AS (
    SELECT cur.*, p.price AS p0, p.compare_at_price AS c0, p.variant_id IS NOT NULL AS matched
    FROM cur LEFT JOIN prev p USING (store, variant_id)
)
SELECT store,
    count(*) AS variants,
    count(*) FILTER (available) AS in_stock,
    count(*) FILTER (cmp > price AND price > 0) AS marked_down,
    round(median(1 - price / cmp) FILTER (cmp > price AND price > 0), 4) AS median_discount,
    round(median(price), 2) AS median_price,
    count(*) FILTER (matched) AS matched,
    count(*) FILTER (matched AND price < p0 - {CENT}) AS price_down,
    count(*) FILTER (matched AND price > p0 + {CENT}) AS price_up,
    count(*) FILTER (matched AND price >= p0 - {CENT} AND cmp > price AND cmp > greatest(c0, p0) + {CENT})
        AS discount_inflated,
    round(exp(avg(ln(least(greatest(price / p0, 0.1), 10))) FILTER (matched AND price > 0 AND p0 > 0)), 6)
        AS price_ratio,
    count(*) FILTER (NOT matched) AS new_variants
FROM j GROUP BY store
"""
GONE = """
SELECT p.store, count(*) FROM prev p
ANTI JOIN read_parquet(?) c USING (store, variant_id)
GROUP BY p.store
"""
EMPTY_PREV = "SELECT NULL::VARCHAR AS store, NULL::BIGINT AS variant_id, NULL::DOUBLE AS price, NULL::DOUBLE AS compare_at_price WHERE false"


def rewrite_csv(path: Path, cols: list[str], run_id: str, rows: list[dict]) -> None:
    """Replace this run's rows in the file (so a rerun does not duplicate them)."""
    kept: list[dict] = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            kept = [r for r in csv.DictReader(f) if r["run_id"] != run_id]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, restval="", extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(kept + rows, key=lambda r: (r["run_id"], r.get("store", ""))))
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True, help="run id to summarise")
    ap.add_argument("--prev", default="", help="run id of the previous snapshot from the same vantage")
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--out", type=Path, default=Path("summary"))
    args = ap.parse_args()

    prices = args.data / f"prices-{args.run}.parquet"
    runs = args.data / f"runs-{args.run}.parquet"
    if not prices.exists() or not runs.exists():
        print(f"missing {prices} or {runs}", file=sys.stderr)
        return 2
    db = duckdb.connect()
    run_rows = db.execute("SELECT store, ok, vantage FROM read_parquet(?)", [str(runs)]).fetchall()
    ok_now = {store for store, ok, _ in run_rows if ok}
    vantage = run_rows[0][2] if run_rows else ""

    prev_prices = args.data / f"prices-{args.prev}.parquet"
    prev_runs = args.data / f"runs-{args.prev}.parquet"
    comparable: set[str] = set()
    if args.prev and prev_prices.exists() and prev_runs.exists():
        ok_before = {s for (s,) in db.execute("SELECT store FROM read_parquet(?) WHERE ok", [str(prev_runs)]).fetchall()}
        comparable = ok_now & ok_before
        db.execute(f"CREATE TEMP VIEW prev AS SELECT store, variant_id, price, compare_at_price FROM read_parquet('{prev_prices}')")
    else:
        if args.prev:
            print(f"previous run {args.prev} not found in {args.data}; summarising without a comparison", file=sys.stderr)
            args.prev = ""
        db.execute(f"CREATE TEMP VIEW prev AS {EMPTY_PREV}")

    cur = db.execute(PER_STORE, [str(prices)])
    names = [d[0] for d in cur.description]
    stats = {row[0]: dict(zip(names, row)) for row in cur.fetchall()}
    gone = dict(db.execute(GONE, [str(prices)]).fetchall())

    change_cols = ("matched", "price_down", "price_up", "discount_inflated", "price_ratio", "new_variants")
    rows = []
    for store, ok, _ in sorted(run_rows):
        row = {"run_id": args.run, "store": store, "ok": int(bool(ok))}
        if ok and store in stats:
            row |= stats[store]
            if store in comparable:
                row["gone_variants"] = gone.get(store, 0)
            else:
                for c in change_cols:
                    row[c] = ""
        rows.append({k: ("" if v is None else v) for k, v in row.items()})

    date = f"{args.run[:4]}-{args.run[4:6]}-{args.run[6:8]}"
    rewrite_csv(args.out / "store_runs" / f"{date}.csv", STORE_COLS, args.run, rows)

    total = lambda col: sum(int(r[col]) for r in rows if r.get(col) not in ("", None))  # noqa: E731
    overall = db.execute(
        "SELECT round(median(1 - price / compare_at_price), 4) FROM read_parquet(?) WHERE compare_at_price > price AND price > 0",
        [str(prices)],
    ).fetchall()[0][0]
    summary = {
        "run_id": args.run,
        "prev_run_id": args.prev,
        "vantage": vantage,
        "stores_ok": len(ok_now),
        "stores_failed": len(run_rows) - len(ok_now),
        "variants": total("variants"),
        "marked_down": total("marked_down"),
        "median_discount": "" if overall is None else overall,
        "matched": total("matched") if comparable else "",
        "price_down": total("price_down") if comparable else "",
        "price_up": total("price_up") if comparable else "",
        "discount_inflated": total("discount_inflated") if comparable else "",
    }
    rewrite_csv(args.out / "runs.csv", RUN_COLS, args.run, [summary])
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
