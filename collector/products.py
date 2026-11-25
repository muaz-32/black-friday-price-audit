"""Keep a compact price history per product: one row each time its price changes.

    python -m collector.products --run 20261114T161240Z

A snapshot holds every price every time; almost all of them are unchanged. This
folds a snapshot into two small state files under state/:
  product-history-<run>.parquet   store, product, run in which a change was seen,
                                  lowest and highest variant price, highest
                                  compare-at price, largest claimed discount
  product-info-<run>.parquet      handle, title, vendor and type per product

The newest pair in state/ is the starting point; the new pair is written beside
it. A product gets a row when it is first seen, when its lowest price, highest
price or compare-at price changes, and when it disappears from a store that was
fetched successfully (prices empty in that row).

The state can always be rebuilt by replaying the snapshots in order, so losing
it loses nothing. Runs must be applied oldest first and from one vantage point.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import workdb

CENT = 0.005
HISTORY_COLS = """
    store VARCHAR, product_id BIGINT, run_id VARCHAR, price_min DOUBLE, price_max DOUBLE,
    compare_at DOUBLE, discount DOUBLE, variants INTEGER, available BOOLEAN
"""
INFO_COLS = "store VARCHAR, product_id BIGINT, handle VARCHAR, title VARCHAR, vendor VARCHAR, product_type VARCHAR"


def newest(state: Path, kind: str) -> Path | None:
    files = sorted(state.glob(f"product-{kind}-*.parquet"))
    return files[-1] if files else None


def differs(a: str, b: str) -> str:
    """SQL: two nullable prices differ by more than a cent."""
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
    catalog = args.data / f"catalog-{args.run}.parquet"
    if not prices.exists() or not runs.exists():
        print(f"missing {prices} or {runs}", file=sys.stderr)
        return 2
    args.state.mkdir(parents=True, exist_ok=True)
    with workdb.connect(args.state) as db:
        return apply(args, db, prices, runs, catalog)


def apply(args, db, prices: Path, runs: Path, catalog: Path) -> int:
    seen = {v for (v,) in db.execute("SELECT DISTINCT vantage FROM read_parquet(?)", [str(runs)]).fetchall()}
    if seen != {args.vantage}:
        print(f"run {args.run} was taken from {sorted(seen)}, not {args.vantage}; not applied", file=sys.stderr)
        return 2

    old_history, old_info = newest(args.state, "history"), newest(args.state, "info")
    db.execute(f"CREATE TABLE history ({HISTORY_COLS})")
    db.execute(f"CREATE TABLE info ({INFO_COLS})")
    if old_history:
        db.execute(f"INSERT INTO history SELECT * FROM read_parquet('{old_history}')")
        latest = db.execute("SELECT max(run_id) FROM history").fetchall()[0][0]
        if latest and latest >= args.run:
            print(f"state already includes {latest}; {args.run} not applied")
            return 0
    if old_info:
        db.execute(f"INSERT INTO info SELECT * FROM read_parquet('{old_info}')")

    db.execute(
        f"""
        CREATE TEMP TABLE cur AS
        SELECT store, product_id,
            min(price) AS price_min, max(price) AS price_max,
            max(compare_at_price) FILTER (compare_at_price > price) AS compare_at,
            round(max(1 - price / compare_at_price) FILTER (compare_at_price > price AND price > 0), 3) AS discount,
            count(*)::INTEGER AS variants, bool_or(available) AS available
        FROM read_parquet('{prices}') WHERE product_id IS NOT NULL AND price IS NOT NULL
        GROUP BY store, product_id
        """
    )
    db.execute(
        """
        CREATE TEMP TABLE last AS
        SELECT * FROM history
        QUALIFY row_number() OVER (PARTITION BY store, product_id ORDER BY run_id DESC) = 1
        """
    )
    # A store cut off at the page limit was not read in full: its missing items
    # are not gone, so it gets no "removed" rows.
    db.execute(f"CREATE TEMP TABLE ok AS SELECT store FROM read_parquet('{runs}') WHERE ok AND NOT coalesce(truncated, false)")
    changed = db.execute(
        f"""
        INSERT INTO history
        SELECT c.store, c.product_id, ?, c.price_min, c.price_max, c.compare_at, c.discount, c.variants, c.available
        FROM cur c LEFT JOIN last l USING (store, product_id)
        WHERE l.product_id IS NULL
           OR {differs("c.price_min", "l.price_min")}
           OR {differs("c.price_max", "l.price_max")}
           OR {differs("c.compare_at", "l.compare_at")}
        """,
        [args.run],
    ).fetchall()[0][0]
    removed = db.execute(
        """
        INSERT INTO history
        SELECT l.store, l.product_id, ?, NULL, NULL, NULL, NULL, 0, false
        FROM last l SEMI JOIN ok USING (store) ANTI JOIN cur c USING (store, product_id)
        WHERE l.price_min IS NOT NULL
        """,
        [args.run],
    ).fetchall()[0][0]

    described = 0
    if catalog.exists():
        db.execute(
            f"""
            CREATE TEMP TABLE fresh AS
            SELECT store, product_id, any_value(handle) AS handle, any_value(title) AS title,
                any_value(vendor) AS vendor, any_value(product_type) AS product_type
            FROM read_parquet('{catalog}') WHERE product_id IS NOT NULL GROUP BY store, product_id
            """
        )
        described = db.execute("SELECT count(*) FROM fresh").fetchall()[0][0]
        db.execute("DELETE FROM info USING fresh WHERE info.store = fresh.store AND info.product_id = fresh.product_id")
        db.execute("INSERT INTO info SELECT * FROM fresh")

    out = {kind: args.state / f"product-{kind}-{args.run}.parquet" for kind in ("history", "info")}
    for kind, order in (("history", "store, product_id, run_id"), ("info", "store, product_id")):
        tmp = out[kind].with_suffix(".parquet.tmp")
        db.execute(f"COPY (SELECT * FROM {kind} ORDER BY {order}) TO '{tmp}' (FORMAT parquet, COMPRESSION zstd, COMPRESSION_LEVEL 9)")
        tmp.rename(out[kind])
    totals = db.execute("SELECT (SELECT count(*) FROM history), (SELECT count(*) FROM cur), (SELECT count(*) FROM info)").fetchall()[0]
    print(
        {
            "run_id": args.run,
            "products": totals[1],
            "new_or_changed": changed,
            "removed": removed,
            "history_rows": totals[0],
            "described": described,
            "info_rows": totals[2],
            "history_bytes": out["history"].stat().st_size,
            "info_bytes": out["info"].stat().st_size,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
