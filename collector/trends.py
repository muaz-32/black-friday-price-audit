"""Record what people are searching for, from three openly public sources.

    python -m collector.trends

Appends to CSV files under trends/ (committed to the repository, one file per day):
  trending/<date>.csv   Google's "trending now" feed per country: the searches
                        rising fastest right now, with Google's rough volume
  suggest/<date>.csv    Google autocomplete for seed phrases such as
                        "black friday ": what people most often type next
  pageviews.csv         daily Wikipedia page views for a panel of articles

Google's 0-100 "interest over time" numbers are not collected: the only
sanctioned way to get them is the official Trends API, which is invite-only.

What to fetch is in trends/config.json. The trending feed is read on every run;
autocomplete at most every 6 hours and page views at most once a day, judged
from what is already on disk, so a late or skipped run does not leave a gap.
"""

from __future__ import annotations

import argparse
import csv
import json
import string
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

from . import http

HT = {"ht": "https://trends.google.com/trending/rss"}
TRENDING_COLS = ["fetched_at", "geo", "rank", "query", "approx_traffic", "published_at", "news_source", "news_title"]
SUGGEST_COLS = ["fetched_at", "geo", "seed", "rank", "suggestion"]
PAGEVIEW_COLS = ["date", "project", "article", "views"]
SUGGEST_EVERY = timedelta(hours=5, minutes=30)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerows(rows)


def last_fetch(directory: Path) -> datetime | None:
    """The newest fetched_at in a directory of daily CSVs."""
    files = sorted(directory.glob("*.csv"))
    if not files:
        return None
    with files[-1].open(newline="", encoding="utf-8") as f:
        stamps = [row["fetched_at"] for row in csv.DictReader(f)]
    return datetime.strptime(max(stamps), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if stamps else None


# --- sources ----------------------------------------------------------------


def fetch_trending(session, geo: str, now: datetime) -> list[dict]:
    r = http.get(session, f"https://trends.google.com/trending/rss?geo={geo}")
    if r.status_code != 200:
        raise http.FetchError(f"HTTP {r.status_code}", r.status_code)
    rows = []
    for rank, item in enumerate(ET.fromstring(r.content).findall("./channel/item"), 1):
        traffic = (item.findtext("ht:approx_traffic", "", HT) or "").rstrip("+").replace(",", "")
        published = item.findtext("pubDate")
        news = item.find("ht:news_item", HT)
        rows.append(
            {
                "fetched_at": iso(now),
                "geo": geo,
                "rank": rank,
                "query": item.findtext("title"),
                "approx_traffic": int(traffic) if traffic.isdigit() else "",
                "published_at": iso(parsedate_to_datetime(published)) if published else "",
                "news_source": news.findtext("ht:news_item_source", "", HT) if news is not None else "",
                "news_title": news.findtext("ht:news_item_title", "", HT) if news is not None else "",
            }
        )
    return rows


def fetch_suggestions(session, geo: str, seed: str, now: datetime) -> list[dict]:
    url = f"https://suggestqueries.google.com/complete/search?client=firefox&hl=en&gl={geo.lower()}&q={quote(seed)}"
    r = http.get(session, url)
    if r.status_code != 200:
        raise http.FetchError(f"HTTP {r.status_code}", r.status_code)
    suggestions = json.loads(r.text)[1]
    return [
        {"fetched_at": iso(now), "geo": geo, "seed": seed, "rank": rank, "suggestion": s}
        for rank, s in enumerate(suggestions, 1)
    ]


def fetch_pageviews(session, article: str, start: date, end: date) -> list[dict]:
    url = (
        "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/"
        f"{quote(article, safe='')}/daily/{start:%Y%m%d}/{end:%Y%m%d}"
    )
    r = http.get(session, url)
    if r.status_code != 200:
        raise http.FetchError(f"HTTP {r.status_code}", r.status_code)
    return [
        {
            "date": f"{i['timestamp'][:4]}-{i['timestamp'][4:6]}-{i['timestamp'][6:8]}",
            "project": i["project"],
            "article": i["article"],
            "views": i["views"],
        }
        for i in r.json()["items"]
    ]


# --- run --------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", type=Path, default=Path("trends"))
    ap.add_argument("--force", action="store_true", help="fetch autocomplete and page views even if recently done")
    ap.add_argument(
        "--min-gap-minutes",
        type=float,
        default=0,
        help="do nothing if the trending feed was read more recently than this (two triggers, one slot)",
    )
    args = ap.parse_args()

    cfg = json.loads((args.dir / "config.json").read_text())
    now = datetime.now(timezone.utc)
    newest = last_fetch(args.dir / "trending")
    if args.min_gap_minutes and newest and now - newest < timedelta(minutes=args.min_gap_minutes):
        print(json.dumps({"skipped": f"trending feed already read at {iso(newest)}"}))
        return 0
    today = now.date()
    session = http.new_session()
    failures: list[str] = []
    done: dict[str, int] = {}

    rows = []
    for geo in cfg["trending_geos"]:
        try:
            rows += fetch_trending(session, geo, now)
        except (http.FetchError, ET.ParseError, ValueError) as e:
            failures.append(f"trending {geo}: {e}")
    if rows:
        append_csv(args.dir / "trending" / f"{today}.csv", TRENDING_COLS, rows)
    done["trending"] = len(rows)

    previous = last_fetch(args.dir / "suggest")
    if args.force or previous is None or now - previous >= SUGGEST_EVERY:
        seeds = list(cfg["suggest_seeds"])
        seeds += [base + letter for base in cfg.get("suggest_alphabet", []) for letter in string.ascii_lowercase]
        rows = []
        for geo in cfg["suggest_geos"]:
            for seed in seeds:
                try:
                    rows += fetch_suggestions(session, geo, seed, now)
                except (http.FetchError, ValueError, IndexError) as e:
                    failures.append(f"suggest {geo} {seed!r}: {e}")
        if rows:
            append_csv(args.dir / "suggest" / f"{today}.csv", SUGGEST_COLS, rows)
        done["suggest"] = len(rows)

    # Wikimedia publishes a day's counts some hours after it ends, so "yesterday"
    # may not be there yet; the file is rebuilt whole and catches up next time.
    views_path = args.dir / "pageviews.csv"
    latest = ""
    if views_path.exists():
        with views_path.open(newline="", encoding="utf-8") as f:
            latest = max((row["date"] for row in csv.DictReader(f)), default="")
    yesterday = today - timedelta(days=1)
    if args.force or latest < str(yesterday):
        start = date.fromisoformat(cfg["pageviews_start"])
        rows = []
        for article in cfg["pageviews_articles"]:
            try:
                rows += fetch_pageviews(session, article, start, yesterday)
            except (http.FetchError, ValueError, KeyError) as e:
                failures.append(f"pageviews {article}: {e}")
        if rows:
            rows.sort(key=lambda r: (r["article"], r["date"]))
            tmp = views_path.with_suffix(".csv.tmp")
            with tmp.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=PAGEVIEW_COLS)
                w.writeheader()
                w.writerows(rows)
            tmp.replace(views_path)
        done["pageviews"] = len(rows)

    print(json.dumps({"fetched_at": iso(now), "rows": done, "failures": len(failures)}))
    for line in failures[:20]:
        print("  failed:", line, file=sys.stderr)
    # The trending feed is the one source that cannot be backfilled.
    return 1 if done["trending"] == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
