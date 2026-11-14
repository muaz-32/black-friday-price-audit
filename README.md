# Black Friday price audit

Are Black Friday discounts real? This project records the public prices of
online stores every few hours, starting before the sale and running until
January, so that every "was / now" claim can be checked against what the price
actually was.

Everything collected is public storefront data. The dataset and the code are
open so anyone can re-run the analysis.

## What is collected

Each run reads the public product feed (`/products.json`) of every store in
[stores/stores.csv](stores/stores.csv) and writes three Parquet files.

**`prices-<run_id>.parquet`** has one row per product variant, every run:

| column | meaning |
|---|---|
| `run_id`, `fetched_at` | which run, and when this store's page was read (UTC) |
| `store`, `currency` | store domain and the currency its prices are in |
| `product_id`, `variant_id` | the store's own stable ids |
| `price` | the price a shopper pays |
| `compare_at_price` | the strikethrough "was" price the store claims, if any |
| `available` | in stock or not |

**`catalog-<run_id>.parquet`** says what each variant is. Join it to prices on
`(store, variant_id)`:

| column | meaning |
|---|---|
| `handle`, `title`, `variant_title` | product page slug and names |
| `vendor`, `product_type`, `tags`, `sku` | catalogue details (`tags` joined with `\|`) |
| `product_created_at`, `product_published_at` | the store's own timestamps |

The descriptive text is about 80% of the bytes and rarely changes, so the
scheduled job writes the whole catalogue once a day (the run that starts just
after 00:00 UTC) and, in its other runs, only products created or published in
the last 48 hours. Use the latest catalogue row for a variant at or before the
price row you are describing.

**`runs-<run_id>.parquet`** has one row per store: whether the fetch worked,
how many products it covered, how many the store says it has, whether the walk
was cut off at the page cap, and which catalogue mode the run used. A variant
missing from a prices file only means "removed" if that store's row says `ok`
for that run.

Sales figures are not public anywhere. The proxies here are stock-outs
(`available` flipping to false) and how often a variant's price changes.

## Search interest

A second job records what people are searching for, hourly, into CSV files
under [trends/](trends/) (committed to this repository, not to a release):

| file | what it is |
|---|---|
| `trends/trending/<date>.csv` | Google's "trending now" feed for 14 countries: the ten searches rising fastest at that moment, with Google's rough volume (`approx_traffic`, a floor such as 1000) and the lead news story |
| `trends/suggest/<date>.csv` | Google autocomplete for seed phrases such as `black friday ` and `black friday a`…`z`, in four English-speaking markets, every 6 hours. `rank` is the position Google showed the suggestion in |
| `trends/pageviews.csv` | daily Wikipedia page views since 2026-10-01 for a panel of shopping-related articles, rebuilt once a day |

Countries, seed phrases and articles are listed in
[trends/config.json](trends/config.json).

These are indirect measures. Google's own 0–100 "interest over time" figures
for a chosen keyword are not collected, because the only sanctioned source is
the official Google Trends API, which is invite-only. The trending feed covers
all searches, not just shopping; most rows are sport and news.

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

The search-interest collector runs the same way:

```bash
.venv/bin/python -m collector.trends
```

## Get the data

Snapshots are published as release assets, one release per month:

```bash
gh release download data-2026-11 --repo muaz-32/black-friday-price-audit --dir data
```

## Query the data

```sql
-- DuckDB: variants whose claimed "was" price went up while the selling price did not drop
WITH moved AS (
  SELECT store, variant_id,
         min(compare_at_price) AS was_before, max(compare_at_price) AS was_after,
         min(price) AS lowest_price, max(price) AS highest_price
  FROM read_parquet('data/prices-*.parquet')
  GROUP BY ALL
  HAVING max(compare_at_price) > min(compare_at_price)
), names AS (
  SELECT store, variant_id, arg_max(title, run_id) AS title, arg_max(variant_title, run_id) AS variant_title
  FROM read_parquet('data/catalog-*.parquet')
  GROUP BY ALL
)
SELECT store, title, variant_title, was_before, was_after, lowest_price, highest_price
FROM moved JOIN names USING (store, variant_id)
ORDER BY was_after / was_before DESC;
```

## Scheduled collection

Three GitHub Actions workflows do the work:

- [snapshot.yml](.github/workflows/snapshot.yml), every 6 hours: takes the
  price snapshot, uploads the files to a monthly release (`data-YYYY-MM`), and
  commits [status/latest.json](status/latest.json) as a heartbeat.
- [trends.yml](.github/workflows/trends.yml), hourly: appends to the CSVs
  under `trends/`.
- [discover.yml](.github/workflows/discover.yml), by hand: probes
  `stores/candidates.txt` and commits the updated store list.

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
  `github-actions`, and the single `local` run (`20261114T042558Z`) is kept
  as an early reference.
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
