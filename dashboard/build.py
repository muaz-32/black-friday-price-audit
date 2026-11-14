"""Build the static dashboard into _site/.

    python -m dashboard.build

Reads only small committed files (summary/, trends/, stores/stores.csv) and
writes _site/index.html plus JSON under _site/data/. No snapshot files needed.

Charts over time use a fixed panel of stores, so that adding stores to the list
does not look like a change in prices: the panel is every store fetched
successfully in the first snapshot that covered (nearly) the full store list.
"""

from __future__ import annotations

import argparse
import csv
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
            variants = int(r["variants"])
            share = int(r["marked_down"]) / variants if variants else 0.0
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
                "c": m.get("currency", ""),
                "v": int(last["variants"]),
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
        variants = sum(int(r["variants"]) for r in rows)
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
                "share": round(sum(int(r["marked_down"]) for r in rows) / variants, 4) if variants else None,
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--summary", type=Path, default=Path("summary"))
    ap.add_argument("--trends", type=Path, default=Path("trends"))
    ap.add_argument("--stores", type=Path, default=Path("stores/stores.csv"))
    ap.add_argument("--out", type=Path, default=Path("_site"))
    ap.add_argument("--repo", default="muaz-32/black-friday-price-audit")
    args = ap.parse_args()

    prices, details = build_prices(args.summary, args.stores)
    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": args.repo,
        "prices": prices,
        "trends": build_trends(args.trends),
    }
    store_dir = args.out / "data" / "store"
    if store_dir.exists():
        shutil.rmtree(store_dir)
    store_dir.mkdir(parents=True)
    (args.out / "data" / "overview.json").write_text(json.dumps(data, separators=(",", ":")))
    for store, series in details.items():
        (store_dir / f"{store}.json").write_text(json.dumps(series, separators=(",", ":")))
    shutil.copy(Path(__file__).parent / "index.html", args.out / "index.html")
    (args.out / ".nojekyll").write_text("")
    size = (args.out / "data" / "overview.json").stat().st_size
    print(f"built {args.out}: {len(prices['runs'])} runs in the series, {len(details)} stores, overview.json {size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
