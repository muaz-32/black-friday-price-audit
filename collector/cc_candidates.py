"""Find candidate shops in Common Crawl's public URL index, by country domain.

    python -m collector.cc_candidates --tlds de,fr,nl --out stores/candidates-cc.txt
    # or split over several machines, then merge:
    python -m collector.cc_candidates --shard 0/8 --budget-minutes 200 --raw part-0.csv
    python -m collector.cc_candidates --merge part-*.csv --out stores/candidates-cc.txt

Common Crawl publishes the index of every crawl as Parquet files sorted by
reversed host name, readable over plain HTTPS with no account. For each
country domain this reads only the files whose range covers it, and keeps the
registered domains whose crawled URLs look like a shop:

  Shopify      at least 5 URLs of the form /collections/<c>/products/<p>
  WooCommerce  at least 5 /product/<p> URLs and 2 /product-category/<c> URLs

Both patterns also occur on other platforms; discovery probes every candidate
and keeps only the shops that really serve the Shopify or WooCommerce feed.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

import duckdb

BASE = "https://data.commoncrawl.org/"
EU = "at,be,bg,hr,cy,cz,dk,ee,fi,fr,de,gr,hu,ie,it,lv,lt,lu,mt,nl,pl,pt,ro,sk,si,es,se,eu"


def parts(crawl: str) -> list[str]:
    with urllib.request.urlopen(f"{BASE}crawl-data/{crawl}/cc-index-table.paths.gz", timeout=60) as r:
        lines = gzip.open(io.BytesIO(r.read())).read().decode().split()
    return [BASE + p for p in lines if "/subset=warc/" in p]


def shop_like(rows) -> list[tuple]:
    """Sum the per-file counts (a domain can straddle two files) and keep shop-like domains."""
    totals: dict[tuple, list[int]] = {}
    for d, tld, *counts in rows:
        acc = totals.setdefault((d, tld), [0, 0, 0])
        for k, c in enumerate(counts):
            acc[k] += int(c)
    return sorted((d, tld, *c) for (d, tld), c in totals.items() if d and (c[0] >= 5 or (c[1] >= 5 and c[2] >= 2)))


def write_new(found: list[tuple], known_paths: list[Path], out: Path) -> None:
    known: set[str] = set()
    for path in known_paths:
        if path.exists():
            for line in path.read_text().splitlines()[1:]:
                known.add(line.split(",", 1)[0].strip().lower().removeprefix("www."))
    new = [d for d, *_ in found if d.removeprefix("www.") not in known]
    by_tld: dict[str, int] = {}
    for _, tld, *_ in found:
        by_tld[tld] = by_tld.get(tld, 0) + 1
    print(f"{len(found)} shop-like domains, {len(new)} not yet probed; by domain: {by_tld}", file=sys.stderr)
    out.write_text("".join(f"{d}\n" for d in new))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--crawl", default="CC-MAIN-2026-43")
    ap.add_argument("--tlds", default=EU, help="comma-separated country domains")
    ap.add_argument("--out", type=Path, default=Path("stores/candidates-cc.txt"))
    ap.add_argument("--cache", type=Path, default=Path("data/cc-cache"), help="per-file results, so a rerun resumes")
    ap.add_argument("--known", type=Path, nargs="*", default=[Path("stores/stores.csv"), Path("stores/rejected.csv")])
    ap.add_argument("--shard", default="0/1", help="I/N: read only every Nth index file, starting at I")
    ap.add_argument("--budget-minutes", type=float, default=0, help="stop reading new files after this long")
    ap.add_argument("--raw", type=Path, help="write per-domain counts here instead of the candidate list")
    ap.add_argument("--merge", type=Path, nargs="*", help="combine --raw files from several shards")
    args = ap.parse_args()

    if args.merge:
        rows = []
        for path in args.merge:
            with path.open(newline="") as f:
                rows += [tuple(r) for r in list(csv.reader(f))[1:]]
        write_new(shop_like(rows), args.known, args.out)
        return 0

    tlds = sorted({t.strip().lower().lstrip(".") for t in args.tlds.split(",") if t.strip()})
    index, count = (int(x) for x in args.shard.split("/"))
    t0 = time.monotonic()
    con = duckdb.connect()
    con.sql("INSTALL httpfs; LOAD httpfs; SET threads = 2; SET http_retries = 8; SET http_retry_wait_ms = 2000")
    cache = args.cache
    cache.mkdir(parents=True, exist_ok=True)
    skipped: list[str] = []

    def query(sql: str, key: str):
        """Run one query, cached on disk so an interrupted search resumes where it stopped.
        Common Crawl answers 503 when busy: wait and retry, and give up on this file only."""
        hit = cache / f"{key}.json"
        if hit.exists():
            return [tuple(r) for r in json.loads(hit.read_text())]
        for attempt in range(4):
            try:
                rows = con.sql(sql).fetchall()
                hit.write_text(json.dumps(rows))
                return rows
            except duckdb.Error as e:
                wait = 60 * (attempt + 1)
                print(f"  {key[:40]}: {str(e)[:100]}; retrying in {wait}s", file=sys.stderr, flush=True)
                time.sleep(wait)
        skipped.append(key)
        return None

    # Each file covers one sorted range of reversed host names ("de,example)/..."),
    # so only the files whose range covers a wanted domain are read.
    wanted = []
    for url in parts(args.crawl)[index::count]:
        rows = query(
            f"SELECT min(stats_min_value), max(stats_max_value) FROM parquet_metadata('{url}') "
            "WHERE path_in_schema = 'url_surtkey'",
            f"range-{url.rsplit('/', 1)[-1][:10]}",
        )
        if rows and any(rows[0][0].split(",")[0] <= t <= rows[0][1].split(",")[0] for t in tlds):
            wanted.append(url)
    print(f"shard {args.shard}: {len(wanted)} index files cover {','.join(tlds)}", file=sys.stderr, flush=True)

    tld_list = ",".join(repr(t) for t in tlds)
    found = []
    for i, url in enumerate(wanted, 1):
        if args.budget_minutes and time.monotonic() - t0 > args.budget_minutes * 60:
            skipped += [u.rsplit("/", 1)[-1][:10] for u in wanted[i - 1 :]]
            print(f"time budget used up; {len(wanted) - i + 1} files left", file=sys.stderr, flush=True)
            break
        found += query(
            f"""SELECT url_host_registered_domain AS domain, url_host_tld AS tld,
                       count(*) FILTER (WHERE url_path LIKE '/collections/%/products/%') AS shopify,
                       count(*) FILTER (WHERE url_path LIKE '/product/%') AS woo_product,
                       count(*) FILTER (WHERE url_path LIKE '/product-category/%') AS woo_category
                FROM read_parquet('{url}')
                WHERE url_host_tld IN ({tld_list})
                GROUP BY 1, 2
                HAVING shopify > 0 OR woo_product > 0""",
            f"shops-{url.rsplit('/', 1)[-1][:10]}-{'-'.join(tlds)}"[:200],
        ) or []
        print(f"  {i}/{len(wanted)} files read", file=sys.stderr, flush=True)

    if args.raw:
        with args.raw.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["domain", "tld", "shopify", "woo_product", "woo_category"])
            w.writerows(found)
        print(f"{len(found)} domains with shop-like URLs -> {args.raw}", file=sys.stderr)
    else:
        write_new(shop_like(found), args.known, args.out)
    if skipped:
        print(f"{len(skipped)} index files not read; run again to retry them: {skipped}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
