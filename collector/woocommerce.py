"""Read a WooCommerce shop's public Store API (`/wp-json/wc/store/v1/products`).

The Store API is what WooCommerce's own block-based shop pages call from the
shopper's browser; it needs no key and returns what a shopper sees: name,
price, regular price, whether it is on sale, and stock status.

Two listings are walked per shop, 100 items a page:
  products                   every top-level product; a simple product is one row
  products?type=variation    each size or colour of a variable product is one row,
                             with the parent as its product
A variable product's own entry only carries its cheapest price, so it gets a row
of its own only when none of its variations is listed.

Differences from the Shopify feed, all visible in the data:
  - prices arrive in minor units ("1999" with 2 decimals is 19.99)
  - compare_at_price is the regular price when the product is on sale
  - no creation or publication dates, so these shops' products appear in the
    catalogue file only in the daily full catalogue
  - no store country; it is guessed from the domain or the currency
"""

from __future__ import annotations

import html
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import http
from .shopify import Store, StoreResult

API = "/wp-json/wc/store/v1/products"
PAGE_SIZE = 100  # the API's maximum
LIMITER = http.OTHER_LIMITER

# Country from the domain, else from the currency. Only a guess: a .com shop in
# euros could be in any euro country, so EUR maps to no country at all.
TLD_COUNTRY = {
    "uk": "GB", "au": "AU", "ca": "CA", "nz": "NZ", "ie": "IE", "de": "DE", "fr": "FR", "it": "IT",
    "es": "ES", "nl": "NL", "be": "BE", "at": "AT", "ch": "CH", "se": "SE", "no": "NO", "dk": "DK",
    "fi": "FI", "pl": "PL", "pt": "PT", "cz": "CZ", "gr": "GR", "za": "ZA", "in": "IN", "br": "BR",
    "mx": "MX", "ar": "AR", "cl": "CL", "co": "CO", "jp": "JP", "sg": "SG", "my": "MY", "ph": "PH",
    "ae": "AE", "il": "IL", "tr": "TR", "ro": "RO", "hu": "HU", "hk": "HK", "kr": "KR", "bd": "BD",
}  # fmt: skip
CURRENCY_COUNTRY = {
    "USD": "US", "GBP": "GB", "AUD": "AU", "CAD": "CA", "NZD": "NZ", "SEK": "SE", "NOK": "NO",
    "DKK": "DK", "CHF": "CH", "PLN": "PL", "CZK": "CZ", "HUF": "HU", "RON": "RO", "ZAR": "ZA",
    "INR": "IN", "BRL": "BR", "MXN": "MX", "JPY": "JP", "SGD": "SG", "HKD": "HK", "BDT": "BD",
}  # fmt: skip


def guess_country(domain: str, currency: str) -> str:
    tld = domain.rsplit(".", 1)[-1]
    return TLD_COUNTRY.get(tld) or CURRENCY_COUNTRY.get(currency, "")


def _money(prices: dict, key: str) -> float | None:
    raw = (prices or {}).get(key)
    if raw in (None, ""):
        return None
    try:
        return int(raw) / 10 ** int(prices.get("currency_minor_unit") or 0)
    except (TypeError, ValueError):
        return None


def _text(value) -> str:
    return html.unescape(value or "").strip()


def _list(session, url: str):
    """One page of a listing: (items, total pages or None, response)."""
    r = http.get(session, url, limiter=LIMITER)
    if r.status_code != 200:
        return None, None, r
    try:
        items = r.json()
    except ValueError:
        return None, None, r
    if not isinstance(items, list):
        return None, None, r
    try:
        pages = int(r.headers.get("X-WP-TotalPages", ""))
    except ValueError:
        pages = None
    return items, pages, r


def probe(domain: str) -> tuple[Store | None, str]:
    """Check that a domain serves the Store API. Returns (store, reason-if-rejected)."""
    session = http.new_session()
    try:
        items, _, r = _list(session, f"https://{domain}{API}?per_page=1")
    except http.FetchError as e:
        return None, f"woocommerce: {e}"
    if items is None:
        return None, f"woocommerce: HTTP {r.status_code}" if r.status_code != 200 else "woocommerce: not a product list"
    if not items:
        return None, "woocommerce: no products"
    if "prices" not in items[0]:
        return None, "woocommerce: not a product list"

    final = urlsplit(r.url)
    prefix = final.path.rsplit(API, 1)[0]
    base_url = f"{final.scheme}://{final.netloc}{prefix}"
    if not http.fetch_robots(session, base_url, limiter=LIMITER).allowed(prefix + API):
        return None, "robots.txt disallows the Store API"
    currency = str(items[0]["prices"].get("currency_code") or "")
    host = final.netloc.lower()
    return (
        Store(
            domain=host,
            base_url=base_url,
            country=guess_country(host, currency),
            currency=currency,
            shop_id=f"woocommerce:{host}",
            name="",
            listed_products=str(r.headers.get("X-WP-Total", "")),
            platform="woocommerce",
        ),
        "",
    )


def fetch_store(store: Store, *, max_pages: int = 100, page_delay: float = 0.5) -> StoreResult:
    """Walk both listings for one shop, one request at a time."""
    res = StoreResult(store=store, started_at=datetime.now(timezone.utc))
    t0 = time.monotonic()
    session = http.new_session()
    try:
        robots = http.fetch_robots(session, store.base_url, limiter=LIMITER)
        prefix = urlsplit(store.base_url).path
        if not robots.allowed(prefix + API):
            res.error = "robots.txt disallows the Store API"
            return res
        delay = min(max(page_delay, robots.crawl_delay), 10.0)
        res.currency = store.currency
        res.listed_products = int(store.listed_products) if store.listed_products.isdigit() else None

        def walk(extra: str) -> list[dict] | None:
            out: list[dict] = []
            page = 1
            while True:
                items, pages, r = _list(session, f"{store.base_url}{API}?per_page={PAGE_SIZE}&page={page}{extra}")
                res.http_status = r.status_code
                if items is None:
                    # Past the last page WordPress answers 400; that is the end, not an error.
                    if r.status_code == 400 and page > 1:
                        break
                    res.error = f"HTTP {r.status_code} on page {page}{' (variations)' if extra else ''}"
                    return None
                out += items
                res.pages += 1
                if not items or (pages is not None and page >= pages):
                    break
                if page >= max_pages:
                    res.truncated = True
                    break
                page += 1
                time.sleep(delay)
            return out

        products = walk("")
        if products is None:
            return res
        parents = {p["id"]: p for p in products if p.get("id") is not None}
        variable = {pid for pid, p in parents.items() if p.get("type") == "variable"}
        variations = walk("&type=variation") if variable else []
        if variations is None:
            return res

        fetched_at = datetime.now(timezone.utc)
        seen: set[int] = set()
        described: set[int] = set()

        def row(item: dict, parent: dict) -> None:
            vid = item.get("id")
            if vid is None or vid in seen:
                return
            seen.add(vid)
            described.add(parent.get("id"))
            prices = item.get("prices") or {}
            price, regular = _money(prices, "price"), _money(prices, "regular_price")
            variant = ", ".join(f"{_text(a.get('name'))}: {_text(a.get('value'))}" for a in item.get("variation_attributes") or [])
            if not variant and isinstance(item.get("variation"), str):
                variant = _text(item["variation"])
            res.rows.append(
                {
                    "fetched_at": fetched_at,
                    "store": store.domain,
                    "currency": prices.get("currency_code") or res.currency,
                    "product_id": parent.get("id"),
                    "variant_id": vid,
                    "handle": parent.get("slug"),
                    "title": _text(parent.get("name") or item.get("name")),
                    "variant_title": variant or None,
                    "vendor": ", ".join(_text(b.get("name")) for b in parent.get("brands") or []) or None,
                    "product_type": _text(((parent.get("categories") or [{}])[0] or {}).get("name")) or None,
                    "tags": "|".join(_text(t.get("name")) for t in parent.get("tags") or [] if t.get("name")),
                    "sku": item.get("sku") or None,
                    "price": price,
                    "compare_at_price": regular if price is not None and regular is not None and regular > price else None,
                    "available": item.get("is_in_stock"),
                    "product_created_at": None,
                    "product_published_at": None,
                }
            )

        for v in variations:
            parent = parents.get(v.get("parent"))
            if parent is not None:  # a variation of a product not listed is not on sale to shoppers
                row(v, parent)
        for pid, p in parents.items():
            if pid not in described:
                row(p, p)
        res.products = len(described)
        res.ok = True
    except http.FetchError as e:
        res.error, res.http_status = str(e), e.status
    except Exception as e:  # one broken shop must never sink the whole run
        res.error = f"{type(e).__name__}: {e}"[:300]
    finally:
        res.seconds = round(time.monotonic() - t0, 2)
        if not res.ok:
            res.rows = []  # a partial catalogue would look like products vanishing
    return res
