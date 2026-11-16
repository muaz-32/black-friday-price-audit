"""Track a fixed basket of products at large retailers that publish no product feed.

    python -m collector.basket check                      # which retailers can be read at all
    python -m collector.basket pick --per-retailer 150    # draw the basket (once; kept fixed)
    python -m collector.basket snapshot --run-id 20261116T121700Z

Retailers such as Target or John Lewis run their own platforms, with no public
product listing like Shopify's. What they do publish, for search engines, is a
sitemap of their product pages and, on each page, the product's price in
schema.org markup (JSON-LD). This collector uses only that:

  pick      reads robots.txt and the sitemaps it lists, and draws a random sample
            of product pages per retailer (fixed seed). A drawn page is kept only
            if it carries a schema.org Product with a price. The basket is written
            to stores/basket.csv and then kept fixed, so every snapshot reads the
            same pages: a random sample, not a hand-picked one.
  snapshot  reads every basket page once, one page at a time per retailer with a
            pause of at least BASKET_DELAY seconds (or the site's Crawl-delay),
            retailers side by side, and writes data/basket-<run>.parquet.

robots.txt is honoured for every page. A retailer that answers with blocks (HTTP
403 or a challenge page) for its first pages is skipped for that run and noted;
nothing here tries to get round a block.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit

import pyarrow as pa
import pyarrow.parquet as pq
import requests

from . import http

RETAILERS = Path("stores/basket_retailers.csv")
BASKET = Path("stores/basket.csv")
DELAY = float(os.environ.get("BASKET_DELAY", "5"))
BLOCK_AFTER = 5  # this many blocked pages in a row and the retailer is left alone for the run
HTML_HEADERS = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8", "Accept-Language": "en"}

SCHEMA = pa.schema(
    [
        ("fetched_at", pa.timestamp("us", tz="UTC")),
        ("run_id", pa.string()),
        ("retailer", pa.string()),
        ("country", pa.string()),
        ("url", pa.string()),
        ("http_status", pa.int32()),
        ("name", pa.string()),
        ("brand", pa.string()),
        ("sku", pa.string()),
        ("gtin", pa.string()),
        ("price", pa.float64()),
        ("currency", pa.string()),
        ("list_price", pa.float64()),
        ("availability", pa.string()),
        ("error", pa.string()),
    ]
)

LD_RE = re.compile(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I)
CHALLENGE_RE = re.compile(
    r"captcha|cf-chl|challenge-platform|px-captcha|_Incapsula_|access denied|are you a robot|bot detection"
    r"|pardon our interruption|just a moment\.\.\.|robot or human",
    re.I,
)


# --- page parsing -------------------------------------------------------------


def _walk(node):
    if isinstance(node, list):
        for x in node:
            yield from _walk(x)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "hasVariant", "mainEntity", "itemListElement", "item"):
            if key in node:
                yield from _walk(node[key])


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    return {str(x).split("/")[-1] for x in (t if isinstance(t, list) else [t]) if x}


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"[^\d.,]", "", str(value))
    if not s:
        return None
    if "," in s and "." in s:  # 1.299,00 or 1,299.00: the last separator is the decimal one
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if len(s.rsplit(",", 1)[1]) != 3 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _offer_price(offer: dict) -> tuple[float | None, str | None, float | None]:
    """(price, currency, list price) of one Offer or AggregateOffer."""
    price = _num(offer.get("price"))
    if price is None:
        price = _num(offer.get("lowPrice"))
    currency = offer.get("priceCurrency")
    list_price = None
    specs = offer.get("priceSpecification") or []
    for spec in specs if isinstance(specs, list) else [specs]:
        if not isinstance(spec, dict):
            continue
        kind = str(spec.get("priceType") or "")
        value = _num(spec.get("price"))
        if kind.endswith(("StrikethroughPrice", "ListPrice", "MSRP", "SRP")):
            list_price = value
        elif price is None and value is not None:
            price = value
        currency = currency or spec.get("priceCurrency")
    return price, currency, list_price


def parse(html_text: str) -> dict | None:
    """The first schema.org Product on a page that has a price, as a row fragment."""
    for block in LD_RE.findall(html_text):
        try:
            data = json.loads(unescape(block.strip()) if block.lstrip().startswith("&") else block.strip())
        except ValueError:
            continue
        for node in _walk(data):
            if not _types(node) & {"Product", "ProductGroup", "IndividualProduct"}:
                continue
            offers = node.get("offers")
            for offer in _walk(offers) if offers else []:
                price, currency, list_price = _offer_price(offer)
                if price is None:
                    continue
                brand = node.get("brand")
                if isinstance(brand, dict):
                    brand = brand.get("name")
                elif isinstance(brand, list):
                    brand = ", ".join(str(b.get("name", b)) if isinstance(b, dict) else str(b) for b in brand)
                gtin = next((node.get(k) for k in ("gtin13", "gtin12", "gtin14", "gtin8", "gtin") if node.get(k)), None)
                return {
                    "name": str(node.get("name") or "")[:300] or None,
                    "brand": str(brand)[:120] if brand else None,
                    "sku": str(node.get("sku") or offer.get("sku") or "") or None,
                    "gtin": str(gtin) if gtin else None,
                    "price": price,
                    "currency": str(currency) if currency else None,
                    "list_price": list_price if list_price and list_price > price else None,
                    "availability": str(offer.get("availability") or "").split("/")[-1] or None,
                }
    return None


# --- retailers, robots and sitemaps ---------------------------------------------


def load_retailers(path: Path = RETAILERS) -> list[dict]:
    with path.open(newline="") as f:
        return [r for r in csv.DictReader(f) if r["domain"].strip() and not r["domain"].startswith("#")]


class Site:
    """One retailer: its robots.txt, its own pace, one page at a time."""

    def __init__(self, domain: str):
        self.domain = domain
        self.base = f"https://{domain}"
        self.session = http.new_session()
        self.session.headers.update(HTML_HEADERS)
        self.robots_text = ""
        try:
            r = self.session.get(self.base + "/robots.txt", timeout=http.TIMEOUT)
            self.robots_status = r.status_code
            if r.status_code == 200:
                self.robots_text = r.text
        except requests.RequestException as e:
            self.robots_status = None
            self.robots_error = type(e).__name__
        self.robots = http.Robots(self.robots_text)
        self.delay = min(max(DELAY, self.robots.crawl_delay), 30.0)
        self.last = 0.0

    def sitemaps(self) -> list[str]:
        return re.findall(r"(?im)^\s*sitemap:\s*(\S+)", self.robots_text)

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        return self.robots.allowed(parts.path + (f"?{parts.query}" if parts.query else ""))

    def get(self, url: str) -> requests.Response:
        wait = self.last + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return self.session.get(url, timeout=http.TIMEOUT)
        finally:
            self.last = time.monotonic()


def blocked(r: requests.Response) -> bool:
    if r.status_code in (401, 403, 429, 430, 503):
        return True
    return r.status_code == 200 and len(r.text) < 20000 and bool(CHALLENGE_RE.search(r.text[:20000]))


def sitemap_rank(url: str) -> int:
    """Product sitemaps first, then unknown ones, then sitemaps of other kinds of page."""
    name = re.sub(r"sitemaps?", "", urlsplit(url).path.lower())  # "sitemap" itself contains "item"
    if re.search(r"product|pdp|/p/|[-_/]p[-_.]|items?\b|artikel|produit", name):
        return 0
    if re.search(r"categor|taxonomy|brand|store|keyword|facet|content|lister|browse|blog|page|image|video|search", name):
        return 2
    return 1


def sitemap_urls(site: Site, limit_files: int = 40, limit_urls: int = 300_000) -> list[str]:
    """Page URLs from the sitemaps in robots.txt, preferring product sitemaps."""
    queue = list(site.sitemaps()) or [site.base + "/sitemap.xml"]
    seen_files, pages = set(), []
    while queue and len(seen_files) < limit_files and len(pages) < limit_urls:
        # Product sitemaps first: they are what the basket is drawn from.
        queue.sort(key=sitemap_rank)
        url = queue.pop(0)
        if url in seen_files or not site.allowed(url):
            continue
        seen_files.add(url)
        try:
            r = site.get(url)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        body = r.content
        if body[:2] == b"\x1f\x8b":
            try:
                body = gzip.decompress(body)
            except OSError:
                continue
        text = body.decode("utf-8", "replace")
        locs = [unescape(x.strip()) for x in re.findall(r"<loc>\s*(.*?)\s*</loc>", text, re.S)]
        if "<sitemapindex" in text[:2000]:
            # Depth first: open this index's own sitemaps before the next index.
            queue = [u for u in locs if u not in seen_files] + queue
        else:
            pages += locs
    return pages


def looks_like_product(url: str) -> bool:
    path = urlsplit(url).path.lower()
    if re.search(r"/(category|categories|c|brand|brands|search|blog|help|stores?|pages?|collections?)/", path):
        return False
    return bool(
        re.search(r"/(p|s|product|products|prod|dp|ip|pd|item|artikel|produit|producto)/|\.p\b|/p-|\.product\.|\d{5,}", path)
    )


# --- commands ---------------------------------------------------------------------


def check(retailers: list[dict], tries: int) -> None:
    """For each retailer: robots, sitemaps, and whether a few product pages parse."""

    def one(r: dict) -> str:
        site = Site(r["domain"])
        if site.robots_status != 200:
            return f"{r['domain']:28} robots.txt {site.robots_status or getattr(site, 'robots_error', '?')}"
        if not site.allowed("/"):
            return f"{r['domain']:28} robots.txt disallows everything for us"
        urls = [u for u in sitemap_urls(site, limit_files=6, limit_urls=20000) if looks_like_product(u)]
        if not urls:
            return f"{r['domain']:28} no product pages found in {len(site.sitemaps())} sitemap(s)"
        random.Random(1).shuffle(urls)
        results = []
        for u in [u for u in urls if site.allowed(u)][:tries]:
            try:
                resp = site.get(u)
            except requests.RequestException as e:
                results.append(type(e).__name__)
                continue
            if blocked(resp):
                results.append(f"blocked {resp.status_code}")
                continue
            p = parse(resp.text) if resp.status_code == 200 else None
            results.append(f"{p['price']} {p['currency']}" + (f" was {p['list_price']}" if p and p["list_price"] else "") if p else f"HTTP {resp.status_code} no price")
        return f"{r['domain']:28} {len(urls)} product URLs; " + " | ".join(results)

    with ThreadPoolExecutor(8) as pool:
        for line in pool.map(one, retailers):
            print(line, flush=True)


def pick(retailers: list[dict], per_retailer: int, seed: int, out: Path) -> None:
    existing: dict[str, list[dict]] = {}
    if out.exists():
        with out.open(newline="") as f:
            for row in csv.DictReader(f):
                existing.setdefault(row["retailer"], []).append(row)

    def one(r: dict) -> list[dict]:
        domain = r["domain"]
        if existing.get(domain):
            return existing[domain]  # the basket stays fixed once drawn
        site = Site(domain)
        if site.robots_status not in (200, 404):
            print(f"{domain}: robots.txt {site.robots_status}; skipped", flush=True)
            return []
        urls = sorted({u for u in sitemap_urls(site) if looks_like_product(u) and site.allowed(u)})
        rng = random.Random(f"{seed}:{domain}")
        rng.shuffle(urls)
        kept, misses, blocks = [], 0, 0
        for u in urls:
            if len(kept) >= per_retailer or misses > per_retailer * 2 or blocks >= BLOCK_AFTER:
                break
            if not kept and misses >= 20:  # pages without a price in their markup: nothing to track
                break
            try:
                resp = site.get(u)
            except requests.RequestException:
                misses += 1
                continue
            if blocked(resp):
                blocks += 1
                continue
            blocks = 0
            p = parse(resp.text) if resp.status_code == 200 else None
            if p is None:
                misses += 1
                continue
            kept.append({"retailer": domain, "country": r["country"], "url": u, "picked_at": f"{datetime.now(timezone.utc):%Y-%m-%d}", "name": p["name"] or ""})
        print(f"{domain}: {len(urls)} product URLs in sitemaps, kept {len(kept)} ({misses} without a price, {blocks} blocked in a row at the end)", flush=True)
        return kept

    with ThreadPoolExecutor(8) as pool:
        rows = [row for rows in pool.map(one, retailers) for row in rows]
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["retailer", "country", "url", "picked_at", "name"])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda x: (x["retailer"], x["url"])))
    print(f"{len(rows)} basket pages -> {out}")


def snapshot(basket: Path, run_id: str, out_dir: Path, budget_minutes: float) -> int:
    with basket.open(newline="") as f:
        pages = list(csv.DictReader(f))
    by_retailer: dict[str, list[dict]] = {}
    for p in pages:
        by_retailer.setdefault(p["retailer"], []).append(p)
    deadline = time.monotonic() + budget_minutes * 60

    def one(domain: str) -> list[dict]:
        site = Site(domain)
        rows, streak = [], 0
        for p in by_retailer[domain]:
            row = {"run_id": run_id, "retailer": domain, "country": p["country"], "url": p["url"]}
            if time.monotonic() > deadline:
                rows.append(row | {"error": "skipped: run time budget used up"})
                continue
            if streak >= BLOCK_AFTER:
                rows.append(row | {"error": "skipped: retailer blocked this run"})
                continue
            if not site.allowed(p["url"]):
                rows.append(row | {"error": "robots.txt disallows"})
                continue
            row["fetched_at"] = datetime.now(timezone.utc)
            try:
                resp = site.get(p["url"])
            except requests.RequestException as e:
                rows.append(row | {"error": type(e).__name__})
                continue
            row["http_status"] = resp.status_code
            if blocked(resp):
                streak += 1
                rows.append(row | {"error": "blocked"})
                continue
            streak = 0
            parsed = parse(resp.text) if resp.status_code == 200 else None
            rows.append(row | (parsed or {"error": "no price on page" if resp.status_code == 200 else f"HTTP {resp.status_code}"}))
        ok = sum(1 for r in rows if r.get("price") is not None)
        print(f"{domain}: {ok}/{len(rows)} prices", flush=True)
        return rows

    with ThreadPoolExecutor(max(1, len(by_retailer))) as pool:
        rows = [r for rs in pool.map(one, sorted(by_retailer)) for r in rs]
    out_dir.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist([{c: r.get(c) for c in SCHEMA.names} for r in rows], schema=SCHEMA)
    path = out_dir / f"basket-{run_id}.parquet"
    pq.write_table(table, path, compression="zstd", compression_level=9)
    ok = sum(1 for r in rows if r.get("price") is not None)
    print(json.dumps({"run_id": run_id, "pages": len(rows), "prices": ok, "retailers": len(by_retailer), "bytes": path.stat().st_size}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--retailers", type=Path, default=RETAILERS)
    c.add_argument("--only", help="comma-separated domains")
    c.add_argument("--tries", type=int, default=3)
    p = sub.add_parser("pick")
    p.add_argument("--retailers", type=Path, default=RETAILERS)
    p.add_argument("--per-retailer", type=int, default=150)
    p.add_argument("--seed", type=int, default=20261116)
    p.add_argument("--out", type=Path, default=BASKET)
    s = sub.add_parser("snapshot")
    s.add_argument("--basket", type=Path, default=BASKET)
    s.add_argument("--run-id", required=True)
    s.add_argument("--out", type=Path, default=Path("data"))
    s.add_argument("--budget-minutes", type=float, default=150)
    args = ap.parse_args()

    if args.cmd == "snapshot":
        return snapshot(args.basket, args.run_id, args.out, args.budget_minutes)
    retailers = load_retailers(args.retailers)
    if getattr(args, "only", None):
        wanted = set(args.only.split(","))
        retailers = [r for r in retailers if r["domain"] in wanted]
    if args.cmd == "check":
        check(retailers, args.tries)
    else:
        pick(retailers, args.per_retailer, args.seed, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
