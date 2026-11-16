"""Find candidate shops in Common Crawl's public URL index, by country domain.

    python -m collector.cc_candidates --tlds de,fr,nl --out stores/candidates-cc.txt

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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--crawl", default="CC-MAIN-2026-43")
    ap.add_argument("--tlds", default=EU, help="comma-separated country domains")
    ap.add_argument("--out", type=Path, default=Path("stores/candidates-cc.txt"))
    ap.add_argument("--cache", type=Path, default=Path("data/cc-cache"), help="per-file results, so a rerun resumes")
    ap.add_argument("--known", type=Path, nargs="*", default=[Path("stores/stores.csv"), Path("stores/rejected.csv")])
    args = ap.parse_args()
    tlds = sorted({t.strip().lower().lstrip(".") for t in args.tlds.split(",") if t.strip()})

    con = duckdb.connect()
    con.sql("INSTALL httpfs; LOAD httpfs; SET threads = 2; SET http_retries = 8; SET http_retry_wait_ms = 2000")

    cache = args.cache
    cache.mkdir(parents=True, exist_ok=True)

    def query(sql: str, key: str):
        """Run one query, cached on disk so an interrupted search resumes where it stopped.
        Common Crawl answers 503 when busy: wait and retry, and give up on this file only."""
        hit = cache / f"{key}.json"
        if hit.exists():
            return [tuple(r) for r in json.loads(hit.read_text())]
        for attempt in range(6):
            try:
                rows = con.sql(sql).fetchall()
                hit.write_text(json.dumps(rows))
                return rows
            except duckdb.Error as e:
                wait = 60 * (attempt + 1)
                print(f"  {key}: {str(e)[:120]}; retrying in {wait}s", file=sys.stderr, flush=True)
                time.sleep(wait)
        skipped.append(key)
        return None

    # Each file covers one sorted range of reversed host names ("de,example)/..."),
    # so only the files whose range covers a wanted domain are read.
    skipped: list[str] = []
    wanted = []
    for url in parts(args.crawl):
        rows = query(
            f"SELECT min(stats_min_value), max(stats_max_value) FROM parquet_metadata('{url}') "
            "WHERE path_in_schema = 'url_surtkey'",
            f"range-{url.rsplit('/', 1)[-1][:10]}",
        )
        if not rows:
            continue
        lo, hi = rows[0]
        if any(lo.split(",")[0] <= t <= hi.split(",")[0] for t in tlds):
            wanted.append(url)
    print(f"{len(wanted)} index files cover {','.join(tlds)}", file=sys.stderr)

    tld_list = ",".join(repr(t) for t in tlds)
    found = []
    for i, url in enumerate(wanted, 1):
        found += query(
            f"""SELECT url_host_registered_domain AS domain, url_host_tld AS tld,
                       count(*) FILTER (WHERE url_path LIKE '/collections/%/products/%') AS shopify,
                       count(*) FILTER (WHERE url_path LIKE '/product/%') AS woo_product,
                       count(*) FILTER (WHERE url_path LIKE '/product-category/%') AS woo_category
                FROM read_parquet('{url}')
                WHERE url_host_tld IN ({tld_list})
                GROUP BY 1, 2""",
            f"shops-{url.rsplit('/', 1)[-1][:10]}-{'-'.join(tlds)}"[:200],
        ) or []
        print(f"  {i}/{len(wanted)} files read", file=sys.stderr, flush=True)
    # A domain can straddle two files: add its counts up before applying the thresholds.
    totals: dict[tuple, list[int]] = {}
    for d, tld, *counts in found:
        acc = totals.setdefault((d, tld), [0, 0, 0])
        for k, c in enumerate(counts):
            acc[k] += c
    found = sorted(
        (d, tld, *c) for (d, tld), c in totals.items() if d and (c[0] >= 5 or (c[1] >= 5 and c[2] >= 2))
    )

    known: set[str] = set()
    for path in args.known:
        if path.exists():
            for line in path.read_text().splitlines()[1:]:
                d = line.split(",", 1)[0].strip().lower()
                known.add(d.removeprefix("www."))
    new = [d for d, *_ in found if d and d.removeprefix("www.") not in known]
    by_tld: dict[str, int] = {}
    for _, tld, *_ in found:
        by_tld[tld] = by_tld.get(tld, 0) + 1
    print(f"{len(found)} shop-like domains, {len(new)} not yet probed; by domain: {by_tld}", file=sys.stderr)
    args.out.write_text("".join(f"{d}\n" for d in new))
    if skipped:
        print(f"{len(skipped)} index files could not be read; run again to retry them: {skipped}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
