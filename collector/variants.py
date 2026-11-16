"""Keep a compact price history per variant: one row each time its price changes.

    python -m collector.variants --run 20261116T001710Z

The variant-level twin of collector.products, and the input of the
reference-price measures in collector.refprice. Folds one snapshot into
  state/variant-history-<run>.parquet
      store, product_id, variant_id, run_id, price, compare_at

A variant gets a row when it is first seen, when its price or its shown
compare-at price changes by more than half a cent, and when it disappears
from a store that was fetched successfully (price empty in that row).
compare_at is kept only while it is above the price, i.e. while the store
shows the variant as reduced; a compare-at price at or below the price is not
shown to shoppers and is stored as empty.

Values hold from the run of their row until the next row of the same variant,
so a store that could not be fetched in some run simply has no new rows.
Like the product history, the state can be rebuilt by replaying the snapshots
oldest first, from one vantage point.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

CENT = 0.005
COLS = "store VARCHAR, product_id BIGINT, variant_id BIGINT, run_id VARCHAR, price DOUBLE, compare_at DOUBLE"


def newest(state: Path) -> Path | None:
    files = sorted(state.glob("variant-history-*.parquet"))
    return files[-1] if files else None


def differs(a: str, b: str) -> str:
    return f"(({a} IS NULL) <> ({b} IS NULL) OR abs({a} - {b}) > {CENT})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--state", type=Path, default=Path("state"))
    ap.add_argument("--vantage", default="github-actions", help="refuse snapshots taken from anywhere else")
    args = ap.parse_args()

    prices = args.data / f"prices-{args.run}.parquet"
    runs = args.data / f"runs-{args.run}.parquet"
    if not prices.exists() or not runs.exists():
        print(f"missing {prices} or {runs}", file=sys.stderr)
        return 2
    args.state.mkdir(parents=True, exist_ok=True)
    db = duckdb.connect()
    seen = {v for (v,) in db.execute("SELECT DISTINCT vantage FROM read_parquet(?)", [str(runs)]).fetchall()}
    if seen != {args.vantage}:
        print(f"run {args.run} was taken from {sorted(seen)}, not {args.vantage}; not applied", file=sys.stderr)
        return 2

    old = newest(args.state)
    db.execute(f"CREATE TABLE history ({COLS})")
    if old:
        db.execute(f"INSERT INTO history SELECT * FROM read_parquet('{old}')")
        latest = db.execute("SELECT max(run_id) FROM history").fetchall()[0][0]
        if latest and latest >= args.run:
            print(f"state already includes {latest}; {args.run} not applied")
            return 0

    db.execute(
        f"""
        CREATE TEMP TABLE cur AS
        SELECT store, any_value(product_id) AS product_id, variant_id, any_value(price) AS price,
            any_value(CASE WHEN compare_at_price > price + {CENT} THEN compare_at_price END) AS compare_at
        FROM read_parquet('{prices}')
        WHERE variant_id IS NOT NULL AND price IS NOT NULL
        GROUP BY store, variant_id
        """
    )
    db.execute(
        """
        CREATE TEMP TABLE last AS
        SELECT * FROM history
        QUALIFY row_number() OVER (PARTITION BY store, variant_id ORDER BY run_id DESC) = 1
        """
    )
    # A store cut off at the page limit was not read in full: its missing items
    # are not gone, so it gets no "removed" rows.
    db.execute(f"CREATE TEMP TABLE ok AS SELECT store FROM read_parquet('{runs}') WHERE ok AND NOT coalesce(truncated, false)")
    changed = db.execute(
        f"""
        INSERT INTO history
        SELECT c.store, c.product_id, c.variant_id, ?, c.price, c.compare_at
        FROM cur c LEFT JOIN last l USING (store, variant_id)
        WHERE l.variant_id IS NULL
           OR {differs("c.price", "l.price")}
           OR {differs("c.compare_at", "l.compare_at")}
        """,
        [args.run],
    ).fetchall()[0][0]
    removed = db.execute(
        """
        INSERT INTO history
        SELECT l.store, l.product_id, l.variant_id, ?, NULL, NULL
        FROM last l SEMI JOIN ok USING (store) ANTI JOIN cur c USING (store, variant_id)
        WHERE l.price IS NOT NULL
        """,
        [args.run],
    ).fetchall()[0][0]

    out = args.state / f"variant-history-{args.run}.parquet"
    tmp = out.with_suffix(".parquet.tmp")
    db.execute(
        f"COPY (SELECT * FROM history ORDER BY store, variant_id, run_id) TO '{tmp}' "
        "(FORMAT parquet, COMPRESSION zstd, COMPRESSION_LEVEL 9)"
    )
    tmp.rename(out)
    rows = db.execute("SELECT count(*) FROM history").fetchall()[0][0]
    current = db.execute("SELECT count(*) FROM cur").fetchall()[0][0]
    print(
        {
            "run_id": args.run,
            "variants": current,
            "new_or_changed": changed,
            "removed": removed,
            "history_rows": rows,
            "history_bytes": out.stat().st_size,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
