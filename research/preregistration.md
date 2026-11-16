# Pre-registration: Do "was" prices tell the truth on Black Friday? Evidence from 1,700 online stores in 15+ countries, and the EU's 30-day prior-price rule

*Draft for registration on OSF (osf.io, "OSF Preregistration" template) before 2026-11-20, a week before Black Friday. Sections follow that template.*

- **Author(s):** [your name, affiliation]
- **Repository:** https://github.com/muaz-32/black-friday-price-audit
- **Analysis code frozen at:** commit `98a0516` (`collector/variants.py`, `collector/refprice.py`). Any later change to how a measure is computed is listed under "Deviations" with its date and reason.
- **Status at registration:** data collection began on 2026-11-14. Interim figures have been computed to test the code, but no hypothesis test below has been run, and none can be: the Black Friday window has not happened yet.

---

## 1. Background

Retailers often announce discounts against a reference ("was", "compare-at") price. If that price was never really charged, the discount is overstated. Jurisdictions regulate this differently:

| Region | Rule on reference prices (summary; not legal advice) |
|---|---|
| **EU (27 states)** | Directive 98/6/EC Art. 6a, inserted by the Omnibus Directive (EU) 2019/2161 and applied from 28 May 2022. Any announced reduction must state as its prior price the **lowest price applied in at least the 30 days before the reduction**. The Court of Justice (C-330/23, *Aldi Süd*, 2024) held that the announced reduction itself must be computed from that prior price. Member states may make exceptions for goods on sale for under 30 days, perishable goods, and progressive reductions. |
| **UK** | No fixed 30-day rule since leaving the EU. Misleading price claims are prohibited under general consumer law (now the DMCC Act 2024), and industry guidance applies (CTSI Pricing Practices Guide). |
| **US** | FTC Guides Against Deceptive Pricing (16 CFR 233.1): a former price must have been offered "openly and actively" for a "reasonably substantial period". Some state laws add detail. |
| **Australia** | Australian Consumer Law ss 18 and 29(1)(i). ACCC guidance expects a "was" price to be one at which the goods were genuinely offered for a reasonable period just before the reduction. |
| **Canada** | Competition Act s 74.01: an "ordinary price" claim must meet a volume test or a time test. |

The EU rule is the only one that sets a mechanical, observable test. This makes stores selling into the EU a natural comparison group for the same platforms and the same kinds of brands elsewhere.

## 2. Research questions and hypotheses

- **RQ1.** How often is the "was" price shown on Black Friday above the prices the store actually charged before the reduction?
- **RQ2.** Is this less frequent among stores subject to the EU 30-day rule?
- **RQ3.** How large is the gap between the advertised discount and the real one?
- **RQ4.** Are Black Friday reduced prices matched or beaten later in the season?

**Confirmatory hypotheses.** All tests are two-sided at α = 0.05, with Holm correction within H1.

- **H1a.** Among eligible reduced variants (Section 4) in the Black Friday window, the share whose was-price is above the 30-day low is **lower in EU stores than in non-EU stores** (US, UK, AU and CA pooled).
- **H1b.** The same share is **lower in EU stores than in UK stores.** The UK is the closest institutional comparison: it shared EU consumer law until 2020, but the 30-day rule was adopted after it left, and it has never applied it.
- **H2.** Across all regions, the advertised discount exceeds the real discount, measured against the time-weighted average price of the prior 30 days. The median per-variant gap is above 0.
- **H3.** Raise-then-cut patterns are **more common among reductions that begin in the Black Friday window** than among reductions that begin in the January-sales window (2027-01-02 to 2027-01-31), within the same stores.

**Descriptive (no test).**

- **D1.** The share of Black Friday reduced variants whose price on the follow-up snapshots (2026-12-15 and 2027-01-15) is at or below the Black Friday price.
- **D2.** All measures by region, platform (Shopify or WooCommerce) and store size.

**Exploratory.** These are labelled as such in any report:

- Price paths of a fixed random basket of product pages at large retailers that publish no feed (`collector/basket.py`, `stores/basket.csv`, drawn on 2026-11-16 with seed 20261116). The basket includes Costco, Wayfair, Marks & Spencer, JB Hi-Fi and Coolblue among its 14 retailers. Few of these pages carry a "was" price, so the basket is used to compare Black Friday prices with each product's own prices before and after, not for the measures in Section 4.

- Timing of price cuts relative to search interest. The repository logs Wikipedia page views, search autocomplete and trending topics hourly.
- Day-by-day dynamics through the season.
- Store-level characteristics associated with the measures.

## 3. Data collection

- **Sources.** Each store's public product feed: Shopify's `/products.json` and the WooCommerce Store API (`/wp-json/wc/store/v1/products`). These are the listings the stores' own pages use, read without logging in. The store list is `stores/stores.csv` (its size at registration is given by the registered commit), and `stores/rejected.csv` lists every domain probed and rejected, with the reason.
- **Frequency and vantage.** Every 6 hours (00:17, 06:17, 12:17 and 18:17 UTC), from GitHub Actions runners in the US. Each store's home market is pinned where the platform supports it.
- **Politeness.** robots.txt is respected; requests run at 1 per second per platform family, with back-off on throttling; no personal data is collected.
- **Stores added after registration.** Stores may be added until **2026-11-20 00:00 UTC**, so that every analysed store has at least 7 days of history before Black Friday. Stores added later are excluded from confirmatory tests.
- **Collection ends** on 2027-01-31.
- **Data availability.** Raw snapshots are published as Parquet files in the repository's monthly releases (`data-YYYY-MM`). Derived variant histories are in the `state` release, and per-run measures are in `summary/refprice/`.

## 4. Variables

Variables are measured per variant: a product's size or colour, each with its own price. Definitions are in `collector/refprice.py`, and constants are fixed at registration:

| Constant | Value |
|---|---|
| Price tolerance (`CENT`) | 0.005 in the store's currency |
| Minimum observation (`MIN_DAYS`) | 7 days |
| Prior window (`WINDOW_DAYS`) | 30 days |
| Raise threshold (`RAISE`) | 5% |

- **Reduced.** A variant is reduced in a snapshot when its compare-at price exceeds its price by more than `CENT`. These are the prices Shopify and WooCommerce themes conventionally display struck through. This is validated in Section 6.
- **Reduction start.** The first snapshot of the current uninterrupted run of reduced observations. A run ends only when the variant is seen at full price, not when it drops out of a listing. If the variant was already reduced when first observed, the start is unknown and the variant is ineligible for start-based measures.
- **Prior window.** The 30 days before the reduction start. Prices hold from each observation until the next one.
- **Eligible (start-based measures).** The reduction start was observed, and the prior window contains at least `MIN_DAYS` of observation.
- **`above_30d_low`** (primary outcome for H1). The was-price exceeds the lowest observed price in the prior window by more than `CENT`. Because prices are observed every 6 hours and only since the store was added, this is a **lower bound** on breaches of the 30-day rule.
- **`never_charged`.** The was-price exceeds every price the variant was observed at, given at least `MIN_DAYS` of observation.
- **`raise_then_cut`** (outcome for H3). In the prior window, the highest price is at least 5% above the lowest, the low came before the high, and the reduced price is not below that low.
- **Advertised discount:** 1 − price / was-price.
- **Real discount:** 1 − price / time-weighted average price in the prior window. A secondary version uses the prior window's lowest price.
- **Gap** (outcome for H2): advertised minus real discount.

## 5. Sample, windows and exclusions

- **Black Friday window.** Snapshots taken from 2026-11-27 00:00 UTC to 2026-11-30 23:59 UTC (Black Friday to Cyber Monday). A variant enters once, at its **first** reduced snapshot in the window.
- **Store inclusion.** A store is included if all of the following hold:
  - It is in `stores/stores.csv` by 2026-11-20 00:00 UTC.
  - It was read successfully in at least 80% of snapshots from 2026-11-20 to 2026-11-30.
  - It was not cut off at the page limit in the snapshot used.
- **Regions.** Regions are assigned from the store's country as reported by the platform:
  - **EU:** the 27 member states, and only stores priced in an EU currency, which excludes EU-registered stores priced in USD.
  - **Non-EU:** US, UK, AU and CA.
  - **Excluded from H1:** other countries and stores with no country.
- **Platform balance.** Most EU stores were found through Common Crawl and are small WooCommerce shops, while most US stores are Shopify brands. To avoid comparing platforms instead of rules, the same Common Crawl search is run for the UK, AU, CA and NZ country domains. H1 is then estimated with platform as a covariate, and repeated **within each platform**: WooCommerce EU against WooCommerce UK/AU/CA, and Shopify EU against Shopify non-EU. A result that holds only in the pooled model and not within platforms is reported as such.
- **Sample size.** It is fixed by collection, not chosen. Stores are found through platform showcases, curated lists and a search of Common Crawl's public URL index (`collector/cc_candidates.py`); the counts per region and platform at registration are those of the registered commit. H1 is tested only if at least **50 EU stores** each contribute at least one eligible reduced variant. Otherwise H1 is reported descriptively, and that is stated.

## 6. Measurement validation

During the Black Friday window, a stratified random sample of 200 variants flagged `above_30d_low` will be drawn: 50 each from the EU, UK, US and AU/CA, using seed 20261127. The variants' product pages will be opened and coded by hand for three things:

1. Is the compare-at price displayed as a struck-through or "was" price?
2. Is it labelled as a recommended retail price ("RRP", "UVP" or similar) rather than as the store's own former price?
3. Does the page show a separate "lowest price in 30 days" statement?

Screenshots are archived. The share of confirmed displays is reported, and the main estimates are repeated after excluding stores whose pages show RRP labels.

## 7. Analysis plan

- **H1.**
  - **Model:** logistic regression at the variant level, `above_30d_low ~ EU + log(store variants) + log(median price) + platform`, with standard errors clustered by store.
  - **Samples:** H1a uses EU and pooled non-EU stores; H1b uses EU and UK stores only.
  - **Report:** odds ratios, average marginal effects and 95% confidence intervals.
  - **Robustness:**
    - (a) store-level proportions, with every store weighted equally (Welch t-test and Mann-Whitney test);
    - (b) excluding the 5% of stores with the most eligible variants;
    - (c) excluding RRP-labelled stores (Section 6);
    - (d) `never_charged` as the outcome;
    - (e) within-platform estimates (see Section 5).
- **H2.** A one-sample test of the store-level median gap against 0 (Wilcoxon signed-rank test), with a store-clustered bootstrap confidence interval (2,000 resamples).
- **H3.** Logistic regression of `raise_then_cut` on a Black Friday indicator with store fixed effects, using reductions that begin in either window.
- **D1.** Shares with store-clustered bootstrap confidence intervals.
- **Causality.** The design is cross-sectional across jurisdictions, so results are described as associations consistent (or not) with an effect of the EU rule, not as proof of a causal effect. Differences in store mix between regions are addressed only through the covariates and robustness checks listed above.

## 8. Known limitations, stated in advance

1. **Short pre-period.** Collection began on 2026-11-14, so a reduction starting on Black Friday has at most 13 days of observed prior prices, not 30. `above_30d_low` therefore undercounts breaches.
2. **Sampling interval.** Prices are observed every 6 hours. A price that lasted less than 6 hours may be missed.
3. **What the compare-at field means.** It is a field the store fills in, and a theme may display it differently or label it as an RRP. Section 6 measures how often this happens.
4. **Store country.** The country and currency are those the store reports. A store's EU sales may run through a market not observed here.
5. **Sample selection.** Stores were found through platform showcases, curated lists and Common Crawl. They are not a random sample of retailers, and large retailers on their own platforms are absent.
6. **Rule exceptions.** Some EU member states exempt goods on sale for under 30 days and progressive reductions. A sensitivity analysis repeats H1 without products the store created less than 30 days before the reduction began (Shopify's product creation date; WooCommerce publishes none, so its stores are left out of this check) and without reductions that deepened during the window.

## 9. Deviations

Changes after registration are listed here with date and reason. None yet.
