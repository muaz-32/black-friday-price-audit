# Pre-registration: Do "was" prices tell the truth on Black Friday? Evidence from 46,000 online stores in 49 countries, and the EU's 30-day prior-price rule

*For registration on OSF on 2026-11-21, before Black Friday, with the **OSF Preregistration** template (version 4). Each heading below is one box of that form, in the form's order; paste each section into the box of the same name. Where a box has fixed choices, the choice to tick is given in bold.*

---

## Registration details (metadata page)

**Title:** Do "was" prices tell the truth on Black Friday? Evidence from 46,000 online stores in 49 countries, and the EU's 30-day prior-price rule

**Description:** We test whether the reference ("was") prices online stores show on Black Friday 2026 were actually charged before the sale, and whether this is more common among stores subject to the EU's 30-day prior-price rule (Directive 98/6/EC Art. 6a) than among comparable stores in the UK, US, Australia and Canada. Prices of about 46,000 Shopify and WooCommerce stores are collected every 6 hours from their public product feeds from 2026-11-14 to 2027-01-31. Code and data: https://github.com/muaz-32/black-friday-price-audit

**Contributors:** [your name, affiliation]

**Repository:** https://github.com/muaz-32/black-friday-price-audit. Measurement code (`collector/variants.py`, `collector/refprice.py`) frozen at commit `98a0516` and unchanged at registration. The analysis script, `research/analysis.py`, runs every model below from those measures; its defaults are the windows, thresholds and seeds given here. It was tested only on snapshots from before the Black Friday window.

---

## Overview

### Research questions or hypotheses

Research questions:

- **RQ1.** How often is the "was" price shown on Black Friday above the prices the store actually charged before the reduction?
- **RQ2.** Is this less frequent among stores subject to the EU 30-day rule?
- **RQ3.** How large is the gap between the advertised discount and the real one?
- **RQ4.** Are Black Friday reduced prices matched or beaten later in the season?

Confirmatory hypotheses (the variables are defined under "Measured variables" and "Indices"):

- **H1a.** Among eligible reduced variants in the Black Friday window, the share whose was-price is above the 30-day low (`above_30d_low`) is **lower in EU stores than in non-EU stores** (US, UK, AU and CA pooled).
- **H1b.** The same share is **lower in EU stores than in UK stores.** The UK is the closest institutional comparison: it shared EU consumer law until 2020, but the 30-day rule was adopted after it left, and it has never applied it.
- **H2.** Across all regions, the advertised discount exceeds the real discount, measured against the time-weighted average price of the prior 30 days. The median per-store gap is above 0.
- **H3.** Raise-then-cut patterns are **more common among reductions that begin in the Black Friday window** than among reductions that begin in the January-sales window (2027-01-02 to 2027-01-31), within the same stores.

Descriptive (no test):

- **D1.** The share of Black Friday reduced variants whose price on the follow-up snapshots (2026-12-15 and 2027-01-15) is at or below the Black Friday price.
- **D2.** All measures by region, platform (Shopify or WooCommerce) and store size.

### Foreknowledge of data or evidence

**Choice:** **Authors have observed the data, but have not performed the proposed analyses.**

### Explanation of foreknowledge and managing unintended influences

The outcome data do not exist yet: every confirmatory test uses reductions observed in the Black Friday window (2026-11-27 to 2026-11-30) or the January window (2027-01-02 to 2027-01-31), both after registration. What exists is the baseline: snapshots since 2026-11-14, which give the prices before each reduction. The authors have looked at these snapshots to build and test the collection and measurement code. The collection workflow also publishes per-run descriptive summaries automatically, including shares of reduced variants by region (`summary/refprice_runs.csv`); these cover snapshots before the Black Friday window only.

Safeguards:

- The hypotheses and the measurement code were fixed on 2026-11-16 (commit `98a0516`), when the store list held about 1,700 stores; it grew to 46,306 by 2026-11-18.
- Measures are computed by code, not by hand, with constants fixed at registration.
- Every later change to the code or this plan is listed under "Deviations" with its date and reason.
- Everything is public, so the timeline can be checked against the commit history.

---

## Research Design

### Study type

**Choices:** **Non-randomized study** and **Adjustment or control-based design.**

### Intention for causal interpretation

**Choice:** **Indirect inference on causal relationship(s).**

The design is cross-sectional across jurisdictions, so results are described as associations consistent (or not) with an effect of the EU rule, not as proof of a causal effect.

### Blinding of experimental treatments

**Choice:** **No blinding is involved.**

### Additional blinding during research or analysis

All outcome measures are computed automatically by code frozen before registration, the same way for every region. In the validation step (see "Other planned analysis"), the coders can see each store's page and so its country; region cannot be hidden from them.

### Study design

This is an observational panel of public online prices.

- **Unit of observation:** a product variant (one size or colour, each with its own price) at one snapshot. Snapshots are taken every 6 hours from 2026-11-14 to 2027-01-31.
- **Comparison groups:** stores subject to the EU 30-day prior-price rule (EU stores priced in an EU currency) against stores in the US, UK, Australia and Canada, where no such mechanical rule applies.
- **Outcomes:** measured for each reduction a variant shows in the Black Friday window (H1, H2) or in the Black Friday and January windows (H3).
- **Platforms:** stores run on Shopify or WooCommerce. EU stores are mostly WooCommerce and US stores mostly Shopify, so platform is a covariate and every H1 comparison is repeated within each platform.

Rules on reference prices by region (summary; not legal advice):

| Region | Rule on reference prices |
|---|---|
| **EU (27 states)** | Directive 98/6/EC Art. 6a, inserted by the Omnibus Directive (EU) 2019/2161 and applied from 28 May 2022. Any announced reduction must state as its prior price the **lowest price applied in at least the 30 days before the reduction**. The Court of Justice (C-330/23, *Aldi Süd*, 2024) held that the announced reduction itself must be computed from that prior price. Member states may make exceptions for goods on sale for under 30 days, perishable goods, and progressive reductions. |
| **UK** | No fixed 30-day rule since leaving the EU. Misleading price claims are prohibited under general consumer law (now the DMCC Act 2024), and industry guidance applies (CTSI Pricing Practices Guide). |
| **US** | FTC Guides Against Deceptive Pricing (16 CFR 233.1): a former price must have been offered "openly and actively" for a "reasonably substantial period". Some state laws add detail. |
| **Australia** | Australian Consumer Law ss 18 and 29(1)(i). ACCC guidance expects a "was" price to be one at which the goods were genuinely offered for a reasonable period just before the reduction. |
| **Canada** | Competition Act s 74.01: an "ordinary price" claim must meet a volume test or a time test. |

### Randomization

There is no randomization; regions are not assigned by the researchers. Two random draws are used, each with a fixed seed:

- the basket of large-retailer product pages (seed 20261116), used only in exploratory analysis;
- the validation sample (seed 20261127).

---

## Sampling

### Data collection procedures

- **Sources.** Each store's public product feed: Shopify's `/products.json` and the WooCommerce Store API (`/wp-json/wc/store/v1/products`). These are the listings the stores' own pages use, read without logging in.
- **Sampling frame.** Stores were found through platform showcases, curated lists and a search of Common Crawl's public URL index (`collector/cc_candidates.py`). To reduce the difference in platform mix between regions, the same Common Crawl search was run for the UK, AU, CA and NZ country domains. The store list is `stores/stores.csv`: 46,306 stores (6,514 Shopify, 39,792 WooCommerce). The set of stores was fixed on 2026-11-18 (commit `d85e886`). On 2026-11-20 a `host` column was added, marking the 3,072 shops hosted by SiteGround (commit `1d07632`); no store was added or removed. `stores/rejected.csv` lists every domain probed and rejected, with the reason.
- **Frequency and vantage.** Every 6 hours (00:17, 06:17, 12:17 and 18:17 UTC), from GitHub Actions runners in the US. Shopify stores are read in their home market (via the `localization` setting), so the prices are those a local shopper sees.
- **Politeness.**
  - robots.txt is respected, including crawl delays up to 10 seconds.
  - Each store is read one request at a time, and requests back off when throttled.
  - The stores are split across 16 runners. On each runner, Shopify requests are capped at 1 per second, requests to SiteGround-hosted shops at 0.5 per second, and requests to other stores at 10 per second spread across different stores. SiteGround applies its limit across its whole network, so it gets a budget of its own. A SiteGround bot check is treated as throttling: the collector slows down and never tries to solve it.
  - No personal data is collected.
- **Data availability.** Raw snapshots are published as Parquet files in the repository's monthly releases (`data-YYYY-MM`). Derived variant histories are in the `state` release, and per-run measures are in `summary/refprice/`.

### Sample size

The units are stores (clusters) and variants within them. Stores listed at registration:

| Region | Shopify | WooCommerce | Total |
|---|---:|---:|---:|
| EU (EU currency) | 1,843 | 15,624 | 17,467 |
| UK | 1,023 | 8,165 | 9,188 |
| US | 1,106 | 232 | 1,338 |
| AU | 1,447 | 7,356 | 8,803 |
| CA | 616 | 2,834 | 3,450 |
| Excluded from H1: NZ, other countries, EU stores in other currencies, no country | 479 | 5,581 | 6,060 |

The number of variants is not known in advance: a full snapshot covers about 66 million variants, but only variants that are reduced in the Black Friday window and meet the eligibility rule enter the tests. Far fewer stores will contribute eligible reduced variants than are listed.

### Sample size rationale

The sample is fixed by collection, not chosen by a power analysis: every store found before the cut-off is followed. Minimum sizes for testing:

- H1a is tested only if at least **50 EU stores** each contribute at least one eligible reduced variant.
- H1b is tested only if, in addition, at least **50 UK stores** do.
- A within-platform comparison is run only if each of its two groups has at least 50 such stores.

Otherwise the comparison is reported descriptively, and that is stated.

### Starting and stopping rules

- **Start.** Collection began on 2026-11-14.
- **Store cut-off.** Stores may be added until **2026-11-20 00:00 UTC**, so that every analysed store has at least 7 days of history before Black Friday. No store has been added or removed since 2026-11-18. Any store added after the cut-off is excluded from confirmatory tests.
- **Stop.** Collection ends on 2027-01-31, the end of the January window.

---

## Variables

### Manipulated variables

None. This is an observational study.

### Measured variables

Per variant and snapshot:

- price;
- compare-at ("was") price;
- product and variant identifiers;
- Shopify's product creation date (WooCommerce publishes none).

Per store:

- platform (Shopify or WooCommerce);
- country and currency as reported by the platform;
- number of variants;
- whether the store was read successfully in each snapshot, and whether it was cut off at the page limit.

The variable each hypothesis uses:

| Hypothesis | Outcome | Main predictor |
|---|---|---|
| H1 | `above_30d_low` | EU indicator |
| H2 | the gap between advertised and real discount | none |
| H3 | `raise_then_cut` | Black Friday window indicator |

Covariates: log number of store variants, log median store price in euros, and platform.

### Indices

Definitions are in `collector/refprice.py`, and constants are fixed at registration:

| Constant | Value |
|---|---|
| Price tolerance (`CENT`) | 0.005 in the store's currency |
| Minimum observation (`MIN_DAYS`) | 7 days |
| Prior window (`WINDOW_DAYS`) | 30 days |
| Raise threshold (`RAISE`) | 5% |

- **Reduced.** A variant is reduced in a snapshot when its compare-at price exceeds its price by more than `CENT`. These are the prices Shopify and WooCommerce themes conventionally display struck through; the validation step checks this.
- **Reduction start.** The first snapshot of the current uninterrupted run of reduced observations. A run ends only when the variant is seen at full price, not when it drops out of a listing. If the variant was already reduced when first observed, the start is unknown and the variant is ineligible for start-based measures.
- **Prior window.** The 30 days before the reduction start. Prices hold from each observation until the next one.
- **Eligible (start-based measures).** The reduction start was observed, and the prior window contains at least `MIN_DAYS` of observation.
- **`above_30d_low`** (outcome for H1). The was-price exceeds the lowest observed price in the prior window by more than `CENT`. Because prices are observed every 6 hours and only since the store was added, this is a **lower bound** on breaches of the 30-day rule.
- **`never_charged`.** The was-price exceeds every price the variant was observed at, given at least `MIN_DAYS` of observation.
- **`raise_then_cut`** (outcome for H3). In the prior window, the highest price is at least 5% above the lowest, the low came before the high, and the reduced price is not below that low.
- **Advertised discount:** 1 − price / was-price.
- **Real discount:** 1 − price / time-weighted average price in the prior window. A secondary version uses the prior window's lowest price.
- **Gap** (outcome for H2): advertised minus real discount.

---

## Analysis Plan

### Statistical models

- **H1.**
  - **Model:** logistic regression at the variant level, `above_30d_low ~ EU + log(store variants) + log(median price) + platform`, with standard errors clustered by store.
  - **Samples:** H1a uses EU and pooled non-EU stores; H1b uses EU and UK stores only.
  - **Report:** odds ratios, average marginal effects and 95% confidence intervals.
  - **Robustness:**
    - (a) store-level proportions, with every store weighted equally (Welch t-test and Mann-Whitney test);
    - (b) excluding the 5% of stores with the most eligible variants;
    - (c) excluding stores whose pages show RRP labels (see "Other planned analysis");
    - (d) `never_charged` as the outcome;
    - (e) within each platform: WooCommerce EU against WooCommerce UK/AU/CA, and Shopify EU against Shopify non-EU. A result that holds only in the pooled model and not within platforms is reported as such.
- **H2.** A one-sample test of the store-level median gap against 0 (Wilcoxon signed-rank test), with a store-clustered bootstrap confidence interval (2,000 resamples) for the median of the store medians.
- **H3.** Logistic regression of `raise_then_cut` on a Black Friday indicator with store fixed effects, using eligible reductions that begin in either window, each counted once. It is estimated as a conditional (fixed-effects) logit, so only stores with reductions beginning in both windows and with variation in the outcome contribute.
- **D1.** For every variant in the Black Friday sample, its price on the first snapshot of each follow-up day, in stores read successfully and not cut off at the page limit in that snapshot. Variants no longer listed are left out. Shares are reported with store-clustered bootstrap confidence intervals.
- **Bootstraps** use 2,000 resamples of stores and seed 20261127.

### Transformations

- **Indicators:**
  - EU = 1 for EU stores, 0 for the comparison stores;
  - platform = 1 for WooCommerce, 0 for Shopify;
  - Black Friday = 1 for reductions beginning in the Black Friday window, 0 for the January window.
- **Logs:** store size and median price enter as natural logs.
- **Currency:** a store's median price is converted to euros at the European Central Bank's reference rates of 2026-11-20, so that stores in different currencies are comparable. All other measures are ratios within one store's currency and need no conversion.

### Inference criteria

- All tests are two-sided at α = 0.05.
- Holm correction is applied across H1a and H1b.
- H2 and H3 are each a single test.
- Robustness checks and within-platform estimates are not separate confirmatory tests. They are reported with their estimates and confidence intervals, together with whether they agree in direction with the main result.

### Data inclusion and exclusion

- **Black Friday window.** Snapshots taken from 2026-11-27 00:00 UTC to 2026-11-30 23:59 UTC (Black Friday to Cyber Monday). A variant enters once, at its **first** reduced snapshot in the window.
- **Store inclusion.** A store is included if all of the following hold:
  - It is in `stores/stores.csv` by 2026-11-20 00:00 UTC.
  - It was read successfully in at least 80% of snapshots from 2026-11-20 to 2026-11-30.
  - It was not cut off at the page limit in the snapshot used.
- **Regions.** Regions are assigned from the store's country as reported by the platform:
  - **EU:** the 27 member states, and only stores priced in an EU currency, which excludes EU-registered stores priced in USD or GBP. This currency restriction is applied in the analysis; the automatic summaries in `summary/refprice/` group by country only.
  - **Non-EU:** US, UK, AU and CA.
  - **Excluded from H1:** other countries (including NZ) and stores with no country.
- **Variants:** only eligible variants (see "Indices") enter start-based measures.
- **Outliers:** no outliers are removed. Robustness check (b) limits the influence of the largest stores.

### Missing data

- A snapshot in which a store could not be read is a gap. Prices hold from the last observation until the next one, and a reduction run continues across the gap.
- Until 2026-11-20, SiteGround's bot check turned away 400 to 1,260 of its roughly 3,000 shops in each snapshot. These failed reads are gaps like any other. From 2026-11-20 these shops are read on their own slower budget.
- Stores read in fewer than 80% of the snapshots from 2026-11-20 to 2026-11-30 are excluded.
- A variant missing from a listing is treated as unobserved, not as a price change.
- Stores with no reported country are excluded from H1.

### Other planned analysis

**Measurement validation.**

- **Sample:** during the Black Friday window, a stratified random sample of 200 variants flagged `above_30d_low`: 50 each from the EU, UK, US and AU/CA, or all of a stratum's flagged variants if it has fewer than 50. Seed 20261127.
- **Coding:** the variants' product pages are opened and coded by hand for three things:
  1. Is the compare-at price displayed as a struck-through or "was" price?
  2. Is it labelled as a recommended retail price ("RRP", "UVP" or similar) rather than as the store's own former price?
  3. Does the page show a separate "lowest price in 30 days" statement?
- **Use:** screenshots are archived. The share of confirmed displays is reported, and the main estimates are repeated after excluding stores whose pages show RRP labels.

**Sensitivity analysis on the EU rule's exceptions.** Some EU member states exempt goods on sale for under 30 days and progressive reductions. H1 is repeated without products the store created less than 30 days before the reduction began, and without reductions that deepened during the window. Product creation dates come from Shopify; WooCommerce publishes none, so its stores are left out of this check.

**Exploratory analyses.** These are labelled as exploratory in any report:

- Price paths of a fixed random basket of product pages at large retailers that publish no feed (`collector/basket.py`, `stores/basket.csv`, drawn on 2026-11-16 with seed 20261116). The basket holds 2,271 pages from 17 retailers, including Marks & Spencer, JB Hi-Fi, Coolblue, Ulta and Hy-Vee: 150 pages each, except Costco (14) and Wayfair (7), where fewer pages could be read. Few of these pages carry a "was" price, so the basket is used to compare Black Friday prices with each product's own prices before and after, not for the measures above.
- Timing of price cuts relative to search interest. The repository logs Wikipedia page views, search autocomplete and trending topics hourly.
- Day-by-day dynamics through the season.
- Store-level characteristics associated with the measures.

---

## Other

### Context and additional information

Known limitations, stated in advance:

1. **Short pre-period.** Collection began on 2026-11-14, so a reduction starting on Black Friday has at most 13 days of observed prior prices, not 30. `above_30d_low` therefore undercounts breaches.
2. **Late-added stores.** Most stores entered the list between 2026-11-16 and 2026-11-18. At those stores, a reduction that began within 7 days of the store being added has too little prior observation and is ineligible. Early "Black Week" reductions are therefore under-represented among the eligible ones.
3. **Sampling interval.** Prices are observed every 6 hours. A price that lasted less than 6 hours may be missed.
4. **What the compare-at field means.** It is a field the store fills in, and a theme may display it differently or label it as an RRP. The validation step measures how often this happens.
5. **Store country.** The country and currency are those the store reports. A store's EU sales may run through a market not observed here.
6. **Sample selection.** Stores were found through platform showcases, curated lists and Common Crawl. They are not a random sample of retailers, and large retailers on their own platforms are absent.

---

## Deviations (kept in the repository after registration; not part of the form)

Changes after registration are listed here with date and reason. The text above is the registered text (commit f8e8539, registered 2026-11-21). None of these changes touches how a measure is computed: `collector/refprice.py` is unchanged since commit 98a0516.

1. **SiteGround give-up rule (2026-11-22; commits 046e69a, df21168).** Some GitHub runner IPs were turned away by SiteGround's bot check from their first request, before any request of ours could have caused it. The collector's pauses then held each snapshot to its 4-hour limit, and later snapshots started late (20 to 22 Nov). Now, a runner turned away three times in a row stops asking SiteGround for the rest of that snapshot. This also covers SiteGround shops the store list does not mark as SiteGround. Their shops are recorded as not read, a gap for that snapshot, and are not retried from another runner. Each run summary counts them (`stores_turned_away`). The bot check is still never solved.
2. **Store order within a runner (2026-11-22; commit e9ca3f3).** Each runner now starts its biggest stores first, instead of in alphabetical order, so a large store started late no longer holds the snapshot an hour longer. Every store is still read once, the same way.
3. **Price histories repaired from the raw snapshots (2026-11-22 and 2026-11-25; commits 9a60b69, c175e18).** Twice the runner stopped responding during the history update, most likely because it ran out of memory: for the snapshots of 2026-11-22 02:59 and 2026-11-24 00:16 UTC. Each update now first adds any snapshot that is in the data releases but missing from the history. Both snapshots were put back this way by the next run, so the histories are the same as if no update had failed. The updates also spill to disk when memory runs short.
4. **One snapshot per 6-hour slot (2026-11-25; commit e707c39).** The registered schedule is every 6 hours, at 00:17, 06:17, 12:17 and 18:17 UTC. GitHub's backup trigger sometimes fired hours late, and the old rule (skip if the last snapshot is under 4 hours old) then dropped the next on-time snapshot. Two slots had no snapshot (24 Nov 12:12 and 25 Nov 00:12 UTC), and two had two (24 Nov 00:12 and 18:12). Since 25 Nov, a run is skipped only when its own slot (from 00:12, 06:12, 12:12 or 18:12) already has a snapshot. A missing slot is a gap like any other. The 80% inclusion rule counts the snapshots actually taken, so a missing slot does not count against any store.
5. **How the validation sample is drawn and captured (2026-11-26; `research/validation.py`).** The registration fixes the sample (200 variants flagged `above_30d_low`, 50 per stratum, seed 20261127) but not these details:
   - **Snapshot.** The sample is drawn from one snapshot: the first in the Black Friday window whose history update succeeded. It is not drawn from the whole window, so that the pages can be captured while the reductions are live.
   - **Flagged.** A variant is flagged when `episode_ok` and `above_low` both hold: the H1 outcome among the variants eligible for it.
   - **Strata.** The strata follow the regions used for H1, from the store list at the cut-off. AU and CA form one stratum.
   - **Draw.** Candidates are sorted by store and variant id. Each stratum is drawn in the order EU, UK, US, AU/CA with one generator seeded 20261127.
   - **Inclusion.** The 80% inclusion rule is not applied, because it can only be known after 30 Nov. Variants from stores excluded later stay in the validation sample.
   - **Screenshots.** One full-page screenshot per page is taken right after the draw, from a GitHub Actions runner in the US, with the project's user agent, respecting robots.txt. Shopify pages are shown in the store's home market, as in the snapshots.
   - **WooCommerce.** Pages are opened by product id, so they show the parent product; the variant has to be selected by hand when coding.
   - **Pages not captured.** A page that robots.txt disallows, or that answers with a bot check, is not captured, and is coded by hand from the live page.
   - **Publication.** The screenshots are published in the repository's `validation` release, and the coding is done on the dashboard site (`/validation/`).
