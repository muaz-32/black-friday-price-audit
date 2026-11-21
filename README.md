# Black Friday price audit

**[📊 Live dashboard](https://muaz-32.github.io/black-friday-price-audit/)**: prices, discounts and product price histories, updated after every snapshot.

Are Black Friday discounts real? This project records the public prices of
online stores every few hours, starting before the sale and running until
January, so that every "was / now" claim can be checked against what the price
actually was.

Everything collected is public storefront data. The dataset and the code are
open so anyone can re-run the analysis.

## What is collected

Each run reads the public product listing of every store in
[stores/stores.csv](stores/stores.csv) and writes three Parquet files. Two
platforms are supported, both reading only what the shop's own pages show
shoppers:

- **Shopify**: the storefront feed `/products.json`.
- **WooCommerce**: the Store API `/wp-json/wc/store/v1/products`, which
  WooCommerce's own shop pages call from the browser. A variable product's
  sizes and colours are read from the same API (`type=variation`); prices
  arrive in minor units and are converted; `compare_at_price` is the regular
  price while a product is on sale. These shops publish no product dates and
  no home country (the `market` column is guessed from the domain or currency,
  and left empty for euro shops on generic domains).

The runs file has a `platform` column, so the two can always be told apart.

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
and running the probe. It tries each domain as a Shopify store, then as a
WooCommerce shop, and keeps the ones that serve a public listing; the reason
for every rejection goes to `stores/rejected.csv`:

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

## Dashboard

**https://muaz-32.github.io/black-friday-price-audit/** (rebuilt after every
snapshot and trends run by [pages.yml](.github/workflows/pages.yml))

A static page for exploring the data: how much of each store is marked down,
how prices move from snapshot to snapshot, a table of every store with its
history, and the search-interest signals.

```bash
python -m dashboard.build                       # writes _site/
python -m http.server 8000 --directory _site    # then open http://localhost:8000
```

It reads only small files committed here, not the snapshots themselves. After
each snapshot `collector.summarize` writes one row per store to
`summary/store_runs/<date>.csv` and one row per run to `summary/runs.csv`:
items listed, items marked down, median claimed discount, and, compared with
the previous snapshot, prices cut, prices raised, and "discounts" that grew
because the compare-at price went up while the selling price did not go down.
The price index chains the average price change of the same items from one
snapshot to the next (a geometric mean, first snapshot = 100).

### Product price histories

The dashboard's product view needs each product's history without loading
every snapshot. After each snapshot `collector.products` folds it into two
small files kept in the `state` release:

- `product-history-<run>.parquet`: one row each time a product's lowest price,
  highest price or compare-at price changes, and when it disappears from a
  store that was read successfully (`store`, `product_id`, `run_id`,
  `price_min`, `price_max`, `compare_at`, `discount`, `variants`, `available`).
- `product-info-<run>.parquet`: handle, title, vendor and type per product.

A product is one listing; its variants (sizes, colours) are summarised by the
cheapest and dearest. The variant-level detail stays in the snapshots, and the
state can be rebuilt from them by applying the runs in order.

### Reference prices: is the "was" price real?

`collector.variants` keeps the same kind of change log per variant
(`variant-history-<run>.parquet` in the `state` release: `store`,
`product_id`, `variant_id`, `run_id`, `price`, and `compare_at` while it is
above the price). From it `collector.refprice` checks, after every snapshot,
each variant a store shows as reduced:

| Measure | The shown "was" price... |
|---|---|
| `never_charged` | is above every price the variant was seen at (7+ days watched) |
| `above_30d_low` | is above the lowest price in the 30 days before the reduction began, the EU's prior-price rule (start of the reduction seen, 7+ days of prior history) |
| `raise_then_cut` | follows a price rise of 5%+ in those 30 days, and the "sale" price is no lower than the price before the rise |

plus the advertised discount against the real one (measured from the 30-day
time-weighted average price). Results go to `summary/refprice/<date>.csv`
(per store and run) and `summary/refprice_runs.csv` (per region and run).
Because prices are only seen every 6 hours and since a store was added, these
are lower bounds.

The study these feed is pre-registered in
[`research/preregistration.md`](research/preregistration.md): hypotheses,
measures and tests were fixed before Black Friday 2026.

The tests themselves are in [`research/analysis.py`](research/analysis.py),
which takes every per-variant measure from `collector.refprice` unchanged and
runs the pre-registered models (H1a, H1b, H2, H3 and the descriptive D1 and
D2). After the last follow-up snapshot:

```bash
pip install -r research/requirements.txt
gh release download state -p 'variant-history-*' --dir data/analysis
gh release download data-2026-11 -p 'runs-*' --dir data/analysis
gh release download data-2026-12 -p 'runs-*' --dir data/analysis
gh release download data-2027-01 -p 'runs-*' --dir data/analysis
python -m research.analysis --history data/analysis/variant-history-<newest>.parquet --runs data/analysis
```

It writes `research/results/report.md`, `results.json` and `d2.csv`. Every
window, threshold and seed is a flag that defaults to its pre-registered
value; a report run with any other value opens with a warning that it is not
the pre-registered analysis.

### Large retailers: the tracked basket

Large retailers on their own platforms (Target, John Lewis, MediaMarkt and so
on) publish no product feed. `collector.basket` follows them through a fixed
basket of product pages instead:

1. `check` tests each retailer in `stores/basket_retailers.csv`. It reads
   robots.txt, opens the sitemaps listed there, and tries a few product pages.
2. `pick` draws up to 150 product pages per retailer **at random** from those
   sitemaps (fixed seed). It keeps the pages whose schema.org markup (the
   JSON-LD block retailers publish for search engines) carries a price. The
   result is `stores/basket.csv`, which then stays fixed, so every run reads
   the same products.
3. `snapshot` runs as its own job in every snapshot. It reads each retailer
   one page at a time, one page every 5 seconds (or the site's Crawl-delay if
   longer), and publishes `basket-<run>.parquet` beside the other files. The
   file holds the page URL, name, brand, SKU, GTIN, price, currency,
   availability and, where the markup has it, a strikethrough or list price.

Most large retailers block automated readers outright, often by refusing even
robots.txt. Those are left alone; nothing here tries to get round a block, and
a retailer that blocks five pages in a row is skipped for that run. From
GitHub's servers on 2026-11-16, 14 retailers could be read: Costco, Wayfair,
Ulta and GameStop (US); Marks & Spencer and Halfords (UK); JB Hi-Fi, Target
Australia, The Good Guys and Myer (AU); Coolblue (NL), Power (DK), Euronics
(IT) and Verkkokauppa (FI). Target US builds its prices in the browser, so its
pages carry none to read.

## Scheduled collection

Three GitHub Actions workflows do the work:

- [snapshot.yml](.github/workflows/snapshot.yml), every 6 hours: takes the
  price snapshot, uploads the files to a monthly release (`data-YYYY-MM`), and
  commits [status/latest.json](status/latest.json) as a heartbeat. The stores
  are split across 16 jobs that run side by side on separate GitHub runners
  (`--shard I/16`, balanced by expected requests), and `collector.combine` joins
  their files into the usual three, so readers see one snapshot. Within a job,
  Shopify stores and the rest are read by separate worker pools, since they wait
  on separate request budgets. A large-retailer basket job runs alongside.
- [trends.yml](.github/workflows/trends.yml), hourly: appends to the CSVs
  under `trends/`.
- [discover.yml](.github/workflows/discover.yml), by hand: probes
  `stores/candidates.txt` and commits the updated store list.

GitHub's own scheduler did not start a single run in this repository's first
hours, so the timed runs are started from outside: a free cron service calls
the `workflow_dispatch` endpoint of the snapshot workflow at minute 17 of
every sixth hour (UTC) and of the trends workflow at minute 37 of every hour.
The `schedule` triggers remain as a backup 15 minutes later; a run that finds
its slot already covered (a snapshot in the last 4 hours, a trending read in
the last 30 minutes) stops without fetching anything.

## How it behaves towards stores

- Identifies itself with a descriptive user agent that links to this repository.
- Checks each store's `robots.txt` before every run and skips stores that disallow the feed.
- Reads each store one page at a time, with a pause between pages, and honours
  `Retry-After`.
- Sends at most 1 request per second in total to Shopify stores from each
  runner (`BF_REQUESTS_PER_SECOND`); a scheduled run uses 16 runners, each
  with its own share of the stores, so every store is still read by one
  runner at that pace. WooCommerce shops are separate servers: each is read one
  page at a time, and together they share a budget of 10 requests per second
  per runner (`BF_OTHER_REQUESTS_PER_SECOND`).
- Shops hosted by SiteGround (marked `host=siteground` in `stores/stores.csv`,
  or recognised by SiteGround's response header) get a budget of their own of
  0.5 requests per second per runner (`BF_SITEGROUND_REQUESTS_PER_SECOND`):
  SiteGround judges the rate per visitor across its whole network and answers
  too many requests with an HTTP 202 bot check. That check is never solved or
  worked around; it is treated like HTTP 429.
- Treats HTTP 429, or a hosting network's bot check, from three different stores
  within a minute as the platform asking it to stop: it pauses every request on
  that budget, then resumes at a slower rate. Stores that were throttled are
  retried once at the end of the run.
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
- **Store selection is not random.** The list is hand-picked brand stores and
  retailers that run Shopify or WooCommerce and leave the listing open. Findings describe these stores, not
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

## Licence

- **Code** (`collector/`, `dashboard/`, the workflows and other scripts): MIT,
  see [LICENSE](LICENSE).
- **[research/preregistration.md](research/preregistration.md)**: CC BY 4.0,
  the same as its registration on OSF.
- **Data** (the snapshot files in the releases, and `stores/`, `summary/`,
  `status/` and `trends/`): prices, store lists and the measures derived from
  them are facts, published for research. This project claims no rights over
  them and adds no restrictions of its own; please cite this repository. It
  cannot grant rights it does not have, though:
  - Product titles, variant names, vendor names, tags and SKUs in the
    catalogue files, and store names, belong to the stores. They are kept so
    that prices can be matched to products, and brand names may be trademarks.
  - Whether reusing a large part of one store's listings needs that store's
    permission depends on where you are (in the EU, for example, the
    database right).
  - In `trends/`, Wikipedia page views come from the Wikimedia pageviews API,
    whose data is released under CC0. Google's autocomplete suggestions and
    trending searches, including the news headlines in the trending files,
    belong to Google and the publishers.
