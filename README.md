# Black Friday price audit

Are Black Friday discounts real? This project records the public prices of
online stores every few hours, starting before the sale and running until
January, so that every "was / now" claim can be checked against what the price
actually was.

Everything collected is public storefront data. The dataset and the code are
open so anyone can re-run the analysis.

## What is collected

Each run reads the public product feed (`/products.json`) of every store in
[stores/stores.csv](stores/stores.csv) and writes two Parquet files:

**`snap-<run_id>.parquet`** has one row per product variant:

| column | meaning |
|---|---|
| `run_id`, `fetched_at` | which run, and when this store's page was read (UTC) |
| `store`, `currency` | store domain and the currency its prices are in |
| `product_id`, `variant_id` | the store's own stable ids |
| `handle`, `title`, `variant_title`, `vendor`, `product_type`, `tags`, `sku` | catalogue details (`tags` joined with `\|`) |
| `price` | the price a shopper pays |
| `compare_at_price` | the strikethrough "was" price the store claims, if any |
| `available` | in stock or not |
| `product_created_at`, `product_published_at` | the store's own timestamps |

**`runs-<run_id>.parquet`** has one row per store: whether the fetch worked,
how many products it covered, how many the store says it has, and whether the
walk was cut off at the page cap. A variant missing from a snapshot only means
"removed" if that store's row says `ok` for that run.

Sales figures are not public anywhere. The proxies here are stock-outs
(`available` flipping to false) and how often a variant's price changes.

## Run it yourself

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python -m collector.snapshot
```

Add stores by appending domains to [stores/candidates.txt](stores/candidates.txt)
and running the probe, which keeps the ones that serve a feed:

```bash
.venv/bin/python -m collector.discover
```

## Get the data

Snapshots are published as release assets, one release per month:

```bash
gh release download data-2026-11 --repo muaz-32/black-friday-price-audit --dir data
```

## Query the data

```sql
-- DuckDB: variants whose claimed "was" price went up while the selling price did not drop
WITH s AS (SELECT * FROM read_parquet('data/snap-*.parquet'))
SELECT store, title, variant_title,
       min(compare_at_price) AS was_before, max(compare_at_price) AS was_after,
       min(price) AS lowest_price, max(price) AS highest_price
FROM s
GROUP BY ALL
HAVING max(compare_at_price) > min(compare_at_price)
ORDER BY was_after / was_before DESC;
```

## Scheduled collection

[.github/workflows/snapshot.yml](.github/workflows/snapshot.yml) runs the
snapshot every 6 hours on GitHub Actions, uploads both files to a monthly
release (`data-YYYY-MM`), and commits [status/latest.json](status/latest.json)
as a heartbeat.

## How it behaves towards stores

- Identifies itself with a descriptive user agent that links to this repository.
- Checks each store's `robots.txt` before every run and skips stores that disallow the feed.
- Sends at most 1 request per second in total across all stores
  (`BF_REQUESTS_PER_SECOND`) and honours `Retry-After`.
- Treats HTTP 429 from three different stores within a minute as the platform
  asking it to stop: it pauses every request, then resumes at a slower rate.
  Stores that were throttled are retried once at the end of the run.
- Reads only the public feed: no logins, no checkout, no customer data.

## Known limits

- **Prices are per market.** Stores show different prices by visitor country.
  Every request pins the store's home country, so numbers are comparable
  between runs, but they describe that one market only. The home country,
  currency and listed product count are read once, when a store is added.
- **Pinning the market is not perfect.** Two snapshots taken 4.5 hours apart
  on 2026-11-14, one from Bangladesh and one from GitHub's servers, agreed on
  99.4% of 1.04 million shared prices. At least one store (`www.pdpaola.com`)
  still priced by the visitor's IP address. Compare prices only between runs
  with the same `vantage` in the runs file; the scheduled runs are all
  `github-actions`, and the single `local` run is kept as an early reference.
- **Store selection is not random.** The list is hand-picked brand stores that
  run Shopify and leave the feed open. Findings describe these stores, not
  e-commerce as a whole, and not marketplaces such as Amazon.
- **`compare_at_price` is the store's claim.** It is the thing being audited,
  not a ground truth.
- **Large catalogues are capped** at 100 pages (25,000 products); capped stores
  are flagged `truncated` in the runs file.
- **The feed's `updated_at` is not recorded.** It returns the time of the
  request, not of the last change, so price changes are found by comparing
  snapshots.
- **The baseline is short.** Collection started on 2026-11-14, about a week
  before Black Friday deals begin. The comparison with prices in December and
  January is what carries most of the weight.
