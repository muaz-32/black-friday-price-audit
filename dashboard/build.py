"""Build the static dashboard into _site/.

    python -m dashboard.build

Reads small committed files (summary/, trends/, stores/stores.csv) and, for the
product pages, the product history in state/ if it is there. Writes
_site/index.html plus JSON under _site/data/. No snapshot files are needed.

Charts over time use a fixed panel of stores, so that adding stores to the list
does not look like a change in prices: the panel is every store fetched
successfully in the first snapshot that covered (nearly) the full store list.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import re
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

VANTAGE = "github-actions"  # prices can differ by vantage point; never mix them
SHOPPING = re.compile(
    r"black friday|cyber monday|deal|sale|discount|coupon|walmart|amazon|target|best buy|costco|ebay|etsy|temu|shein"
    r"|aliexpress|ps5|playstation|xbox|nintendo|switch 2|iphone|ipad|airpods|macbook|galaxy|pixel|kindle|lego|dyson"
    r"|air fryer|oled|laptop|headphones|gift",
    re.I,
)


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def num(value, cast=float):
    return cast(value) if value not in ("", None) else None


def run_time(run_id: str) -> str:
    return datetime.strptime(run_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_prices(summary: Path, stores_csv: Path) -> tuple[dict, dict]:
    runs = [r for r in read_csv(summary / "runs.csv") if r["vantage"] == VANTAGE]
    runs.sort(key=lambda r: r["run_id"])
    wanted = {r["run_id"] for r in runs}
    by_store: dict[str, list[dict]] = defaultdict(list)
    for path in sorted((summary / "store_runs").glob("*.csv")):
        for row in read_csv(path):
            if row["run_id"] in wanted and row["ok"] == "1":
                by_store[row["store"]].append(row)
    for rows in by_store.values():
        rows.sort(key=lambda r: r["run_id"])
    meta = {r["domain"]: r for r in read_csv(stores_csv)}
    if not runs:
        return {"latest": None, "runs": [], "stores": [], "baseline_run": None}, {}

    most = max(int(r["stores_ok"]) for r in runs)
    baseline = next(r["run_id"] for r in runs if int(r["stores_ok"]) >= 0.9 * most)
    panel = {s for s, rows in by_store.items() if any(r["run_id"] == baseline for r in rows)}

    # Per-store history, with the price index chained from each run-to-run ratio.
    details: dict[str, list] = {}
    table = []
    for store, rows in by_store.items():
        index, cuts, rises, inflated, series = 100.0, 0, 0, 0, []
        for r in rows:
            index *= num(r["price_ratio"]) or 1.0
            cuts += num(r["price_down"], int) or 0
            rises += num(r["price_up"], int) or 0
            inflated += num(r["discount_inflated"], int) or 0
            variants = num(r["variants"], int) or 0
            share = (num(r["marked_down"], int) or 0) / variants if variants else 0.0
            r["_share"], r["_index"] = share, index
            series.append(
                [r["run_id"], variants, round(share, 4), num(r["median_discount"]), num(r["price_down"], int),
                 num(r["price_up"], int), num(r["discount_inflated"], int), round(index, 2)]
            )  # fmt: skip
        details[store] = series
        first, last = rows[0], rows[-1]
        m = meta.get(store, {})
        table.append(
            {
                "d": store,
                "m": m.get("country", ""),
                "pl": m.get("platform") or "shopify",
                "c": m.get("currency", ""),
                "v": num(last["variants"], int) or 0,
                "s": round(last["_share"], 4),
                "md": num(last["median_discount"]),
                "mp": num(last["median_price"]),
                "s0": round(first["_share"], 4),
                "idx": round(index, 2),
                "cuts": cuts,
                "rises": rises,
                "infl": inflated,
                "n": len(rows),
                "first": first["run_id"],
                "last": last["run_id"],
            }
        )
    table.sort(key=lambda r: -r["v"])

    # Panel series: one point per run from the baseline on.
    at = {(s, r["run_id"]): r for s, rows in by_store.items() if s in panel for r in rows}
    series, index = [], 100.0
    for run in runs:
        rid = run["run_id"]
        if rid < baseline:
            continue
        rows = [at[(s, rid)] for s in panel if (s, rid) in at]
        variants = sum((num(r["variants"], int) or 0) for r in rows)
        weight = sum(num(r["matched"], int) or 0 for r in rows if num(r["price_ratio"]))
        if weight:
            log = sum((num(r["matched"], int) or 0) * math.log(num(r["price_ratio"])) for r in rows if num(r["price_ratio"]))
            index *= math.exp(log / weight)
        shares = sorted(r["_share"] for r in rows)
        compared = rid != baseline and weight > 0
        series.append(
            {
                "run_id": rid,
                "t": run_time(rid),
                "stores": len(rows),
                "variants": variants,
                "share": round(sum((num(r["marked_down"], int) or 0) for r in rows) / variants, 4) if variants else None,
                "median_store_share": round(shares[len(shares) // 2], 4) if shares else None,
                "cuts": sum(num(r["price_down"], int) or 0 for r in rows) if compared else None,
                "rises": sum(num(r["price_up"], int) or 0 for r in rows) if compared else None,
                "inflated": sum(num(r["discount_inflated"], int) or 0 for r in rows) if compared else None,
                "index": round(index, 3),
            }
        )

    last = runs[-1]
    latest = {k: num(v, int) if k not in ("run_id", "prev_run_id", "vantage", "median_discount") else v for k, v in last.items()}
    latest["median_discount"] = num(last["median_discount"])
    latest["t"] = run_time(last["run_id"])
    return {
        "latest": latest,
        "baseline_run": baseline,
        "baseline_t": run_time(baseline),
        "panel_stores": len(panel),
        "runs": series,
        "stores": table,
    }, details


def build_trends(trends: Path) -> dict:
    views: dict[str, list] = defaultdict(list)
    for r in read_csv(trends / "pageviews.csv"):
        views[r["article"]].append([r["date"], int(r["views"])])

    suggest_files = sorted((trends / "suggest").glob("*.csv"))
    first_seen: dict[tuple, str] = {}
    latest_rows: list[dict] = []
    for path in suggest_files:
        rows = read_csv(path)
        for r in rows:
            first_seen.setdefault((r["geo"], r["seed"], r["suggestion"]), r["fetched_at"])
        if path == suggest_files[-1] and rows:
            newest = max(r["fetched_at"] for r in rows)
            latest_rows = [r for r in rows if r["fetched_at"] == newest]
    started = min(first_seen.values(), default="")
    suggest = defaultdict(lambda: defaultdict(list))
    for r in sorted(latest_rows, key=lambda r: int(r["rank"])):
        seen = first_seen[(r["geo"], r["seed"], r["suggestion"])]
        suggest[r["geo"]][r["seed"]].append([r["suggestion"], seen[:10], seen != started])

    trending_files = sorted((trends / "trending").glob("*.csv"))
    now: dict[str, list] = {}
    shopping: dict[tuple, dict] = {}
    newest = ""
    for path in trending_files:
        rows = read_csv(path)
        for r in rows:
            if SHOPPING.search(r["query"]):
                key = (r["geo"], r["query"].lower())
                hit = shopping.setdefault(key, {"geo": r["geo"], "query": r["query"], "first": r["fetched_at"], "traffic": 0})
                hit["last"] = r["fetched_at"]
                hit["traffic"] = max(hit["traffic"], num(r["approx_traffic"], int) or 0)
        if path == trending_files[-1] and rows:
            newest = max(r["fetched_at"] for r in rows)
            for r in sorted((r for r in rows if r["fetched_at"] == newest), key=lambda r: int(r["rank"])):
                now.setdefault(r["geo"], []).append([r["query"], num(r["approx_traffic"], int), r["news_title"]])
    return {
        "pageviews": views,
        "suggest": suggest,
        "suggest_at": latest_rows[0]["fetched_at"] if latest_rows else None,
        "trending": now,
        "trending_at": newest or None,
        "shopping": sorted(shopping.values(), key=lambda h: h["last"], reverse=True)[:200],
    }


MOVERS = """
WITH ends AS (
    SELECT store, product_id,
        arg_min(price_min, run_id) FILTER (price_min IS NOT NULL) AS p0,
        arg_min(greatest(coalesce(compare_at, 0), price_max), run_id) FILTER (price_min IS NOT NULL) AS ref0,
        arg_max(price_min, run_id) AS p1, arg_max(compare_at, run_id) AS c1,
        min(run_id) AS first_run, max(run_id) AS last_run
    FROM history GROUP BY store, product_id HAVING count(*) > 1
), scored AS (
    SELECT e.*, i.title, i.handle, p1 / p0 AS ratio, c1 / ref0 AS ref_ratio
    FROM ends e LEFT JOIN info i USING (store, product_id)
    WHERE p1 IS NOT NULL AND p0 >= 5
)
SELECT kind, store, product_id, title, handle, p0, p1, ref0, c1 FROM (
    SELECT 'cuts' AS kind, *, ratio AS score FROM scored WHERE ratio BETWEEN 0.05 AND 0.97
    UNION ALL SELECT 'rises', *, -ratio FROM scored WHERE ratio BETWEEN 1.03 AND 20
    UNION ALL SELECT 'inflated', *, -ref_ratio FROM scored WHERE c1 > p1 AND p1 >= p0 - 0.005 AND ref_ratio BETWEEN 1.03 AND 20
)
QUALIFY row_number() OVER (PARTITION BY kind, store ORDER BY score) <= 3
ORDER BY kind, score
"""


def build_products(state: Path, out: Path) -> dict | None:
    """Per-store product files and the lists of biggest moves, from the newest state files.

    Product row: [id, title, handle, [[run index, lowest price, highest price or
    null if the same, compare-at or null], ...]]; a removal is [run index, null].
    """
    history = sorted(state.glob("product-history-*.parquet"))
    info = sorted(state.glob("product-info-*.parquet"))
    if not history or not info:
        return None
    import duckdb  # only needed for this part, so the rest builds without it

    db = duckdb.connect()
    db.execute(f"CREATE VIEW history AS SELECT * FROM read_parquet('{history[-1]}')")
    db.execute(f"CREATE VIEW info AS SELECT * FROM read_parquet('{info[-1]}')")
    runs = [r for (r,) in db.execute("SELECT DISTINCT run_id FROM history ORDER BY 1").fetchall()]
    index = {r: i for i, r in enumerate(runs)}
    directory = out / "data" / "products"
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)

    cur = db.execute(
        """
        SELECT h.store, h.product_id, i.title, i.handle, h.run_id, h.price_min, h.price_max, h.compare_at
        FROM history h LEFT JOIN info i USING (store, product_id)
        ORDER BY h.store, h.product_id, h.run_id
        """
    )
    counts: dict[str, int] = {}
    store, products, product = None, [], None

    def flush() -> None:
        if store is not None:
            # Gzipped: the site has a 1 GB limit and these files shrink about 6x;
            # the page unpacks them with the browser's DecompressionStream.
            body = json.dumps({"runs": runs, "products": products}, separators=(",", ":")).encode()
            (directory / f"{store}.json.gz").write_bytes(gzip.compress(body, 9, mtime=0))
            counts[store] = len(products)

    while batch := cur.fetchmany(200_000):
        for s_, pid, title, handle, run_id, lo, hi, cmp_ in batch:
            if s_ != store:
                flush()
                store, products, product = s_, [], None
            if product is None or product[0] != pid:
                product = [pid, title or handle or str(pid), handle or "", []]
                products.append(product)
            if lo is None:
                product[3].append([index[run_id], None])
            else:
                product[3].append([index[run_id], lo, None if hi is None or abs(hi - lo) < 0.005 else hi, cmp_])
    flush()

    movers: dict[str, list] = {"cuts": [], "rises": [], "inflated": []}
    for kind, s_, pid, title, handle, p0, p1, ref0, c1 in db.execute(MOVERS).fetchall():
        if len(movers[kind]) < 150:
            movers[kind].append([s_, pid, title or handle or str(pid), p0, p1, ref0, c1])
    (out / "data" / "movers.json").write_text(json.dumps(movers, separators=(",", ":")))
    size = sum(f.stat().st_size for f in directory.iterdir())
    print(f"products: {sum(counts.values())} in {len(counts)} stores, {size // 1_000_000} MB, through {runs[-1]}")
    return {"runs": len(runs), "last_run": runs[-1], "stores": counts}



STORE_FIELDS = ["d", "m", "pl", "c", "v", "s", "md", "mp", "s0", "idx", "cuts", "rises", "infl", "n", "first", "last"]


def write_store_table(path: Path, table: list[dict], products: dict[str, int]) -> int:
    """{"fields": [...], "runs": [...], "rows": [[...], ...]}; first/last are indices into runs,
    and the last field is the number of products with a price history (0: none)."""
    runs = sorted({r for row in table for r in (row["first"], row["last"])})
    at = {r: i for i, r in enumerate(runs)}
    rows = [
        [at[row[k]] if k in ("first", "last") else row.get(k) for k in STORE_FIELDS] + [products.get(row["d"], 0)]
        for row in table
    ]
    path.write_text(json.dumps({"fields": STORE_FIELDS + ["np"], "runs": runs, "rows": rows}, separators=(",", ":")))
    return path.stat().st_size


def copy_validation(src: Path, dst: Path, repo: str) -> None:
    """The validation coding page, its sample and the screenshot log, if drawn."""
    if not (src / "index.html").exists():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "sample.csv", "shots.csv"):
        if (src / name).exists():
            shutil.copy(src / name, dst / name)
    (dst / "config.json").write_text(json.dumps({"repo": repo}))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--summary", type=Path, default=Path("summary"))
    ap.add_argument("--trends", type=Path, default=Path("trends"))
    ap.add_argument("--stores", type=Path, default=Path("stores/stores.csv"))
    ap.add_argument("--state", type=Path, default=Path("state"), help="product history files; skipped if absent")
    ap.add_argument("--out", type=Path, default=Path("_site"))
    ap.add_argument("--validation", type=Path, default=Path("research/validation"), help="coding page and sample")
    ap.add_argument("--repo", default="muaz-32/black-friday-price-audit")
    args = ap.parse_args()

    prices, details = build_prices(args.summary, args.stores)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "data").mkdir(exist_ok=True)
    products = build_products(args.state, args.out)
    # The store table goes in a file of its own, as columns rather than one
    # object per store: the page shows its charts from overview.json first and
    # fills the tables when this arrives (11 MB in one file made the first view
    # slow, more so on slow connections).
    table = prices.pop("stores")
    counts = (products or {}).pop("stores", {})
    stores_file = write_store_table(args.out / "data" / "stores.json", table, counts)
    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": args.repo,
        "prices": prices,
        "trends": build_trends(args.trends),
        "products": products,
    }
    store_dir = args.out / "data" / "store"
    if store_dir.exists():
        shutil.rmtree(store_dir)
    store_dir.mkdir(parents=True)
    (args.out / "data" / "overview.json").write_text(json.dumps(data, separators=(",", ":")))
    for store, series in details.items():
        (store_dir / f"{store}.json").write_text(json.dumps(series, separators=(",", ":")))
    shutil.copy(Path(__file__).parent / "index.html", args.out / "index.html")
    copy_validation(args.validation, args.out / "validation", args.repo)
    (args.out / ".nojekyll").write_text("")
    size = (args.out / "data" / "overview.json").stat().st_size
    print(
        f"built {args.out}: {len(prices['runs'])} runs in the series, {len(details)} stores, "
        f"overview.json {size // 1024} KB, stores.json {stores_file // 1024} KB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
