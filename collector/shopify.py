"""Read a Shopify storefront's public product feed (`/products.json`).

The feed is the same data the storefront shows shoppers: title, price, the
"compare at" (strikethrough) price, and whether each variant is in stock.

Prices depend on the visitor's market: the same variant returned 16.00 with a
US market and 13.00 with a GB one. Every request therefore pins the store's
home country through the `localization` cookie, so a snapshot taken from any
machine in any country sees the same prices, in the store's own currency.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import http

PAGE_SIZE = 250  # the feed's maximum
FEED_PATH = f"/products.json?limit={PAGE_SIZE}&page=1"


@dataclass
class Store:
    domain: str
    base_url: str
    country: str = ""
    currency: str = ""
    shop_id: str = ""
    name: str = ""
    listed_products: str = ""  # the store's own published count, at discovery
    platform: str = "shopify"
    host: str = ""  # a hosting network that throttles by visitor IP across all its sites ("siteground")


@dataclass
class StoreResult:
    store: Store
    ok: bool = False
    error: str = ""
    http_status: int | None = None
    pages: int = 0
    products: int = 0
    truncated: bool = False
    currency: str = ""
    listed_products: int | None = None
    started_at: datetime | None = None
    seconds: float = 0.0
    rows: list[dict] = field(default_factory=list)


def _ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _money(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _market_cookie(country: str) -> dict | None:
    return {"localization": country} if country else None


def fetch_meta(session, base_url: str) -> dict:
    """`/meta.json`: shop id, home country, currency, published product count."""
    try:
        r = http.get(session, base_url + "/meta.json", tries=2)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, dict) else {}
    except (http.FetchError, ValueError):
        pass
    return {}


def probe(domain: str) -> tuple[Store | None, str]:
    """Check that a domain serves the feed. Returns (store, reason-if-rejected)."""
    session = http.new_session()
    try:
        r = http.get(session, f"https://{domain}/products.json?limit=1", tries=2)
    except http.FetchError as e:
        return None, str(e)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    try:
        products = r.json()["products"]
    except (ValueError, KeyError, TypeError):
        return None, "not a product feed"
    if not products:
        return None, "empty feed"

    final = urlsplit(r.url)
    prefix = final.path.rsplit("/products.json", 1)[0]
    base_url = f"{final.scheme}://{final.netloc}{prefix}"

    if not http.fetch_robots(session, base_url).allowed(prefix + FEED_PATH):
        return None, "robots.txt disallows the feed"

    meta = fetch_meta(session, base_url)
    return (
        Store(
            domain=final.netloc.lower(),
            base_url=base_url,
            country=str(meta.get("country") or ""),
            currency=str(meta.get("currency") or ""),
            shop_id=str(meta.get("myshopify_domain") or meta.get("id") or ""),
            name=str(meta.get("name") or ""),
            listed_products=str(meta.get("published_products_count") or ""),
        ),
        "",
    )


def fetch_store(store: Store, *, max_pages: int = 100, page_delay: float = 0.5) -> StoreResult:
    """Walk the whole feed for one store, one request at a time."""
    res = StoreResult(store=store, started_at=datetime.now(timezone.utc))
    t0 = time.monotonic()
    session = http.new_session()
    cookies = _market_cookie(store.country)
    seen: set[int] = set()
    try:
        robots = http.fetch_robots(session, store.base_url)
        prefix = urlsplit(store.base_url).path
        if not robots.allowed(prefix + FEED_PATH):
            res.error = "robots.txt disallows the feed"
            return res
        delay = min(max(page_delay, robots.crawl_delay), 10.0)

        # Market, currency and listed count come from discovery, not a fresh
        # /meta.json: that would add one request per store to every run.
        res.currency = store.currency
        res.listed_products = int(store.listed_products) if store.listed_products.isdigit() else None

        page = 1
        while True:
            url = f"{store.base_url}/products.json?limit={PAGE_SIZE}&page={page}"
            r = http.get(session, url, cookies=cookies)
            res.http_status = r.status_code
            if r.status_code != 200:
                res.error = f"HTTP {r.status_code} on page {page}"
                return res
            try:
                products = r.json().get("products") or []
            except ValueError:
                res.error = f"invalid JSON on page {page}"
                return res
            fetched_at = datetime.now(timezone.utc)
            for p in products:
                tags = p.get("tags") or []
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",")]
                tag_str = "|".join(t for t in tags if t)
                new_product = False
                for v in p.get("variants") or []:
                    vid = v.get("id")
                    if vid is None or vid in seen:  # pages can shift mid-walk
                        continue
                    seen.add(vid)
                    new_product = True
                    res.rows.append(
                        {
                            "fetched_at": fetched_at,
                            "store": store.domain,
                            "currency": res.currency,
                            "product_id": p.get("id"),
                            "variant_id": vid,
                            "handle": p.get("handle"),
                            "title": p.get("title"),
                            "variant_title": v.get("title"),
                            "vendor": p.get("vendor"),
                            "product_type": p.get("product_type"),
                            "tags": tag_str,
                            "sku": v.get("sku"),
                            "price": _money(v.get("price")),
                            "compare_at_price": _money(v.get("compare_at_price")),
                            "available": v.get("available"),
                            "product_created_at": _ts(p.get("created_at")),
                            "product_published_at": _ts(p.get("published_at")),
                        }
                    )
                res.products += new_product
            if not products:
                break
            res.pages = page
            # Only an empty page ends the feed: a store returned 247 products on
            # page 48 and full pages after it, so a short page is not the end.
            if page >= max_pages:
                res.truncated = True
                break
            page += 1
            time.sleep(delay)
        res.ok = True
    except http.FetchError as e:
        res.error, res.http_status = str(e), e.status
    except Exception as e:  # one broken store must never sink the whole run
        res.error = f"{type(e).__name__}: {e}"[:300]
    finally:
        res.seconds = round(time.monotonic() - t0, 2)
        if not res.ok:
            res.rows = []  # a partial catalogue would look like products vanishing
    return res
