"""Check shown "was" prices against the prices stores actually charged.

    python -m collector.refprice --run 20261116T001710Z

Reads the variant history (collector.variants) as it stood after <run>, so any
past run can be measured again from the newest state file. For every variant a
store showed as reduced in that run (compare-at price above the price), and only
for stores fetched successfully in that run:

  never_charged    the "was" price is above every price the variant was seen at,
                   over at least MIN_DAYS of observation
  above_30d_low    the "was" price is above the lowest price the variant was seen
                   at in the 30 days before this reduction began (the EU's prior-
                   price rule, Directive 98/6/EC Art. 6a); counted only when the
                   start of the reduction was observed, after at least MIN_DAYS
                   of observation, and so a lower bound on the true count
  raise_then_cut   in those 30 days the price rose at least RAISE from a low, and
                   the reduced price is no lower than that low

  advertised       1 - price / was price, the discount the store claims
  real_30d_low     1 - price / lowest price in the 30 days before the reduction
  real_avg         1 - price / time-weighted average price in those 30 days

Writes summary/refprice/<date>.csv (one row per store and run) and
summary/refprice_runs.csv (one row per run and region). The measures are those
of the pre-registration in research/preregistration.md; change them only with a
dated note there.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

import duckdb

CENT = 0.005
MIN_DAYS = 7
WINDOW_DAYS = 30
RAISE = 0.05

EU = set("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split())
REGIONS = {"US": "US", "GB": "UK", "AU": "AU", "CA": "CA"}


def region(country: str) -> str:
    return "EU" if country in EU else REGIONS.get(country, "other" if country else "unknown")


STORE_COLS = [
    "run_id", "store", "country", "region", "on_sale",
    "span_eligible", "never_charged",
    "episode_eligible", "above_30d_low", "raise_then_cut",
    "median_advertised", "median_real_30d_low", "median_real_avg", "median_gap",
]  # fmt: skip
RUN_COLS = [
    "run_id", "region", "stores", "on_sale", "span_eligible", "never_charged", "episode_eligible",
    "above_30d_low", "raise_then_cut", "median_advertised", "median_real_avg",
]  # fmt: skip

VARIANTS = f"""
WITH h AS (
    SELECT *, strptime(run_id, '%Y%m%dT%H%M%SZ') AS s
    FROM read_parquet($history) WHERE run_id <= $run AND store IN (SELECT store FROM ok)
), seg AS (
    SELECT *,
        coalesce(lead(s) OVER v, $now + INTERVAL 1 SECOND) AS e,
        min(s) OVER v_all AS first_seen,
        max(price) OVER v_all AS max_ever,
        -- A reduction ends only when the variant is seen at its full price again;
        -- dropping out of a listing for a while neither ends nor starts one.
        sum(CASE WHEN price IS NOT NULL AND compare_at IS NULL THEN 1 ELSE 0 END)
            OVER (v ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS grp,
        row_number() OVER (PARTITION BY store, variant_id ORDER BY run_id DESC) AS back
    FROM h
    WINDOW v AS (PARTITION BY store, variant_id ORDER BY run_id),
           v_all AS (PARTITION BY store, variant_id)
), cur AS (  -- variants shown as reduced in this run
    SELECT store, variant_id, price, compare_at, grp, first_seen, max_ever
    FROM seg WHERE back = 1 AND price IS NOT NULL AND compare_at IS NOT NULL
), episode AS (  -- when the current reduction began; grp = 0 means before we first saw the variant
    SELECT c.store, c.variant_id, min(s.s) AS start
    FROM cur c JOIN seg s USING (store, variant_id)
    WHERE c.grp > 0 AND s.grp = c.grp AND s.price IS NOT NULL AND s.compare_at IS NOT NULL
    GROUP BY 1, 2
), prior AS (  -- prices in the 30 days before the reduction began
    SELECT ep.store, ep.variant_id, ep.start,
        min(s.price) AS low,
        arg_min(s.s, s.price) AS low_at,
        max(s.price) AS high,
        arg_max(s.s, s.price) AS high_at,
        sum(s.price * epoch(least(s.e, ep.start) - greatest(s.s, ep.start - INTERVAL {WINDOW_DAYS} DAY)))
          / sum(epoch(least(s.e, ep.start) - greatest(s.s, ep.start - INTERVAL {WINDOW_DAYS} DAY))) AS avg
    FROM episode ep JOIN seg s USING (store, variant_id)
    WHERE s.price IS NOT NULL AND s.s < ep.start AND s.e > ep.start - INTERVAL {WINDOW_DAYS} DAY
    GROUP BY 1, 2, 3
)
SELECT c.store, c.price, c.compare_at,
    epoch($now - c.first_seen) / 86400 >= {MIN_DAYS} AS span_ok,
    c.compare_at > c.max_ever + {CENT} AS never_charged,
    p.low IS NOT NULL
        AND epoch(p.start - greatest(c.first_seen, p.start - INTERVAL {WINDOW_DAYS} DAY)) / 86400 >= {MIN_DAYS}
        AS episode_ok,
    c.compare_at > p.low + {CENT} AS above_low,
    p.high >= p.low * (1 + {RAISE}) AND p.low_at < p.high_at AND c.price >= p.low - {CENT} AS raise_then_cut,
    1 - c.price / c.compare_at AS advertised,
    1 - c.price / p.low AS real_low,
    1 - c.price / p.avg AS real_avg
FROM cur c LEFT JOIN prior p USING (store, variant_id)
"""

PER_STORE = """
SELECT store,
    count(*) AS on_sale,
    count(*) FILTER (span_ok) AS span_eligible,
    count(*) FILTER (span_ok AND never_charged) AS never_charged,
    count(*) FILTER (episode_ok) AS episode_eligible,
    count(*) FILTER (episode_ok AND above_low) AS above_30d_low,
    count(*) FILTER (episode_ok AND raise_then_cut) AS raise_then_cut,
    round(median(advertised), 4) AS median_advertised,
    round(median(real_low) FILTER (episode_ok), 4) AS median_real_30d_low,
    round(median(real_avg) FILTER (episode_ok), 4) AS median_real_avg,
    round(median(advertised - real_avg) FILTER (episode_ok), 4) AS median_gap
FROM v GROUP BY store ORDER BY store
"""


def newest(state: Path) -> Path | None:
    files = sorted(state.glob("variant-history-*.parquet"))
    return files[-1] if files else None


def rewrite(path: Path, cols: list[str], rows: list[dict], run: str) -> None:
    """Replace this run's rows in a CSV, keeping every other run's."""
    keep = []
    if path.exists():
        with path.open(newline="") as f:
            keep = [r for r in csv.DictReader(f) if r["run_id"] != run]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(keep + rows, key=lambda r: (r["run_id"], r.get("store") or r.get("region"))))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--state", type=Path, default=Path("state"))
    ap.add_argument("--history", type=Path, help="variant history file (default: newest in --state)")
    ap.add_argument("--stores", type=Path, default=Path("stores/stores.csv"))
    ap.add_argument("--out", type=Path, default=Path("summary"))
    args = ap.parse_args()

    history = args.history or newest(args.state)
    runs = args.data / f"runs-{args.run}.parquet"
    if not history or not runs.exists():
        print(f"missing variant history or {runs}", file=sys.stderr)
        return 2
    db = duckdb.connect()
    latest = db.execute("SELECT max(run_id) FROM read_parquet(?)", [str(history)]).fetchall()[0][0]
    if latest < args.run:
        print(f"{history} stops at {latest}, before {args.run}", file=sys.stderr)
        return 2
    now = datetime.strptime(args.run, "%Y%m%dT%H%M%SZ")
    # Stores cut off at the page limit are left out: some of their variants were
    # not read in this run, so their last known state may be stale.
    db.execute(f"CREATE TEMP TABLE ok AS SELECT store FROM read_parquet('{runs}') WHERE ok AND NOT coalesce(truncated, false)")
    db.execute(
        f"CREATE TEMP TABLE v AS {VARIANTS}", {"history": str(history), "run": args.run, "now": now}
    )
    countries = {}
    if args.stores.exists():
        with args.stores.open(newline="") as f:
            countries = {r["domain"]: r.get("country", "") for r in csv.DictReader(f)}

    cur = db.execute(PER_STORE)
    names = [d[0] for d in cur.description]
    stores = []
    for row in cur.fetchall():
        r = dict(zip(names, row), run_id=args.run)
        r["country"] = countries.get(r["store"], "")
        r["region"] = region(r["country"])
        stores.append(r)

    # Pooled per region: medians over variants, counts summed.
    db.execute("CREATE TEMP TABLE reg (store VARCHAR, region VARCHAR)")
    db.executemany("INSERT INTO reg VALUES (?, ?)", [(s["store"], s["region"]) for s in stores])
    cur = db.execute(
        """
        SELECT region, count(DISTINCT store) AS stores, count(*) AS on_sale,
            count(*) FILTER (span_ok) AS span_eligible,
            count(*) FILTER (span_ok AND never_charged) AS never_charged,
            count(*) FILTER (episode_ok) AS episode_eligible,
            count(*) FILTER (episode_ok AND above_low) AS above_30d_low,
            count(*) FILTER (episode_ok AND raise_then_cut) AS raise_then_cut,
            round(median(advertised), 4) AS median_advertised,
            round(median(real_avg) FILTER (episode_ok), 4) AS median_real_avg
        FROM v JOIN reg USING (store) GROUP BY ROLLUP (region) ORDER BY region NULLS LAST
        """
    )
    names = [d[0] for d in cur.description]
    regions = [dict(zip(names, row), run_id=args.run) for row in cur.fetchall()]
    for r in regions:
        r["region"] = r["region"] or "all"

    rewrite(args.out / "refprice" / f"{args.run[:4]}-{args.run[4:6]}-{args.run[6:8]}.csv", STORE_COLS, stores, args.run)
    rewrite(args.out / "refprice_runs.csv", RUN_COLS, regions, args.run)
    for r in regions:
        print({k: r[k] for k in RUN_COLS if k != "run_id"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
