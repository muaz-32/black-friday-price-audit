"""Confirmatory analysis of the pre-registration: H1a, H1b, H2, H3, D1 and D2.

    python -m research.analysis --history state/variant-history-<run>.parquet \\
        --runs data/analysis --out research/results

Implements Section "Analysis Plan" of research/preregistration.md. The
measures themselves are not re-implemented: every per-variant measure comes
from collector.refprice.VARIANTS, the code frozen at commit 98a0516, evaluated
once per snapshot. This script only selects the samples and runs the models.

Inputs
  --history   a variant-history file (the `state` release) that reaches past
              the last snapshot needed (the end of the January window, or of
              whatever windows are set)
  --runs      a folder with the runs-<run_id>.parquet files of every snapshot
              in the windows (the monthly `data-YYYY-MM` releases)
  stores      the store list as it stood at the cut-off, read from git history
              (or --stores-at-cutoff)
  ECB rates   research/ecb-rates-2026-11-20.csv, euros per unit of currency

Every window, threshold and date is a flag whose default is the pre-registered
value. If any is changed (for example to try the script on data from before
Black Friday), the report says in its first line that it is not the
pre-registered analysis and lists what was changed.
"""

from __future__ import annotations

import argparse
import atexit
import csv
import io
import json
import math
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
from scipy import optimize, special, stats
from statsmodels.stats.multitest import multipletests

from collector.refprice import CENT, EU, MIN_DAYS, VARIANTS

# --- pre-registered settings ---------------------------------------------------

EU_CURRENCIES = {"EUR", "BGN", "CZK", "DKK", "HUF", "PLN", "RON", "SEK"}
NON_EU = {"US": "US", "GB": "UK", "AU": "AU", "CA": "CA"}
FMT = "%Y%m%dT%H%M%SZ"


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)


@dataclass
class Settings:
    cutoff: str = "2026-11-20T00:00:00Z"  # stores listed by then
    inclusion_start: str = "2026-11-20T00:00:00Z"  # read in >= 80% of these snapshots
    inclusion_end: str = "2026-11-30T23:59:59Z"
    inclusion_share: float = 0.8
    bf_start: str = "2026-11-27T00:00:00Z"  # Black Friday window
    bf_end: str = "2026-11-30T23:59:59Z"
    jan_start: str = "2027-01-02T00:00:00Z"  # January-sales window (H3)
    jan_end: str = "2027-01-31T23:59:59Z"
    followups: list[str] = field(default_factory=lambda: ["2026-12-15", "2027-01-15"])  # D1
    min_stores: int = 50
    top_share: float = 0.05  # robustness (b)
    alpha: float = 0.05
    bootstrap: int = 2000
    seed: int = 20261127
    vantage: str = "github-actions"
    rates: str = "research/ecb-rates-2026-11-20.csv"
    # Only for trying the script before 7 days of history exist; any other value
    # than the frozen MIN_DAYS changes the measures and is flagged in the report.
    min_days: int = MIN_DAYS


PREREGISTERED = Settings()

# The frozen query, with two identifying columns added to its output. Nothing
# in how a measure is computed changes; the assertion keeps it that way.
_ORIG = "SELECT c.store, c.price, c.compare_at,"
assert VARIANTS.count(_ORIG) == 1, "collector.refprice.VARIANTS changed; see the pre-registration"
MEASURES = VARIANTS.replace(_ORIG, "SELECT c.store, c.variant_id, p.start AS episode_start, c.price, c.compare_at,")
_MIN = f"/ 86400 >= {MIN_DAYS}"
assert MEASURES.count(_MIN) == 2


def measures_query(min_days: int) -> str:
    return MEASURES if min_days == MIN_DAYS else MEASURES.replace(_MIN, f"/ 86400 >= {min_days}")


# --- data -------------------------------------------------------------------


def stores_at_cutoff(cutoff: str, path: Path | None) -> pd.DataFrame:
    """The store list as committed before the cut-off."""
    if path:
        text = path.read_text()
    else:
        rev = subprocess.run(
            ["git", "rev-list", "-1", f"--before={cutoff}", "HEAD", "--", "stores/stores.csv"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip
        if not rev:
            sys.exit(f"no commit of stores/stores.csv before {cutoff}")
        text = subprocess.run(["git", "show", f"{rev}:stores/stores.csv"], capture_output=True, text=True, check=True).stdout
    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    return df[["domain", "country", "currency", "platform"]].rename(columns={"domain": "store"})


def region(country: str, currency: str) -> str:
    if country in EU:
        return "EU" if currency in EU_CURRENCIES else "EU, other currency"
    return NON_EU.get(country, "other" if country else "unknown")


def load_rates(path: str) -> dict[str, float]:
    with open(path, newline="") as f:
        return {r["currency"]: float(r["per_eur"]) for r in csv.DictReader(f)}


def runs_between(runs: list[str], start: str, end: str) -> list[str]:
    a, b = ts(start), ts(end)
    return [r for r in runs if a <= datetime.strptime(r, FMT) <= b]


def first_run_on(runs: list[str], day: str) -> str | None:
    hits = [r for r in runs if r[:8] == day.replace("-", "")]
    return hits[0] if hits else None


# --- statistics -------------------------------------------------------------


def logit(df: pd.DataFrame, formula: str, term: str) -> dict:
    """Variant-level logistic regression with standard errors clustered by store."""
    out = {"variants": int(len(df)), "stores": int(df["store"].nunique())}
    if df.empty or df["y"].nunique() < 2:
        return out | {"error": "no variants" if df.empty else "the outcome does not vary"}
    try:
        groups = pd.factorize(df["store"])[0]
        res = smf.logit(formula, df).fit(disp=0, cov_type="cluster", cov_kwds={"groups": groups})
        lo, hi = res.conf_int().loc[term]
        me = res.get_margeff(at="overall").summary_frame()
        out |= {
            "coef": float(res.params[term]),
            "odds_ratio": math.exp(res.params[term]),
            "or_ci": [math.exp(lo), math.exp(hi)],
            "p": float(res.pvalues[term]),
            "ame": float(me.loc[term, "dy/dx"]),
            "ame_ci": [float(me.loc[term, "Conf. Int. Low"]), float(me.loc[term, "Cont. Int. Hi."])],
            "params": {k: float(v) for k, v in res.params.items()},
        }
    except Exception as e:  # separation, a singular design, too few stores
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def store_level(df: pd.DataFrame, outcome: str, flag: str) -> dict:
    """Robustness (a): one proportion per store, every store weighted equally."""
    p = df.groupby("store").agg(share=(outcome, "mean"), g=(flag, "first"))
    a, b = p.loc[p.g == 1, "share"], p.loc[p.g == 0, "share"]
    out = {"stores_1": int(len(a)), "stores_0": int(len(b)), "mean_1": float(a.mean()), "mean_0": float(b.mean())}
    if len(a) > 1 and len(b) > 1:
        out["welch_p"] = float(stats.ttest_ind(a, b, equal_var=False).pvalue)
        out["mann_whitney_p"] = float(stats.mannwhitneyu(a, b, alternative="two-sided").pvalue)
    return out


def boot_ci(values: np.ndarray, stat, n: int, seed: int) -> list[float]:
    """Percentile interval from resampling stores (rows of `values`) with replacement."""
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(values), size=(n, len(values)))
    draws = np.array([stat(values[i]) for i in idx])
    return [float(np.quantile(draws, 0.025)), float(np.quantile(draws, 0.975))]


def fe_logit_binary(tables: pd.DataFrame) -> dict:
    """Logistic regression of y on one binary x with store fixed effects.

    With a single binary regressor, the conditional (fixed-effects) logit
    depends on each store only through its 2x2 table, and its likelihood is
    the product of noncentral hypergeometric terms (Fisher). `tables` has one
    row per store: n1, k1 (reductions and successes with x = 1) and n0, k0.
    Stores without variation in x or y carry no information and drop out.
    """
    t = tables[(tables.n1 > 0) & (tables.n0 > 0)].copy()
    t["k"] = t.k1 + t.k0
    t = t[(t.k > 0) & (t.k < t.n1 + t.n0)]
    out = {"stores": int(len(t)), "reductions": int((t.n1 + t.n0).sum())}
    if len(t) == 0:
        out["error"] = "no store has reductions in both windows with variation in the outcome"
        return out
    # Support of k1 in each stratum, as one flat array with stratum ids.
    lo = np.maximum(0, t.k - t.n0).to_numpy()
    hi = np.minimum(t.k, t.n1).to_numpy()
    sizes = hi - lo + 1
    sid = np.repeat(np.arange(len(t)), sizes)
    u = np.concatenate([np.arange(a, b + 1) for a, b in zip(lo, hi)])
    n1, n0, k = (np.repeat(t[c].to_numpy(), sizes) for c in ("n1", "n0", "k"))
    logc = special.gammaln(n1 + 1) - special.gammaln(u + 1) - special.gammaln(n1 - u + 1)
    logc += special.gammaln(n0 + 1) - special.gammaln(k - u + 1) - special.gammaln(n0 - k + u + 1)
    k1 = t.k1.to_numpy()

    def nll(b: float) -> float:
        z = logc + b * u
        m = np.full(len(t), -np.inf)
        np.maximum.at(m, sid, z)
        denom = m + np.log(np.bincount(sid, weights=np.exp(z - m[sid])))
        return -float(np.sum(b * k1 - denom))  # the C(n1,k1)C(n0,k0) terms are constant

    res = optimize.minimize_scalar(nll, bounds=(-20, 20), method="bounded")
    b, h = float(res.x), 1e-4
    info = (nll(b + h) - 2 * nll(b) + nll(b - h)) / h**2
    se = 1 / math.sqrt(info) if info > 0 else float("nan")
    z = b / se
    out |= {
        "coef": b,
        "odds_ratio": math.exp(b),
        "or_ci": [math.exp(b - 1.959964 * se), math.exp(b + 1.959964 * se)],
        "p": float(2 * stats.norm.sf(abs(z))),
    }
    if abs(b) > 19:
        out["error"] = "estimate at the bound: the outcome is separated by window"
    return out


# --- analysis ---------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--history", type=Path, required=True)
    ap.add_argument("--runs", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("research/results"))
    ap.add_argument("--stores-at-cutoff", type=Path, help="store list at the cut-off (default: from git history)")
    ap.add_argument("--rrp-stores", type=Path, help="stores coded as showing RRP labels, one per line (robustness c)")
    d = PREREGISTERED
    for name, value in asdict(d).items():
        flag = "--" + name.replace("_", "-")
        if isinstance(value, list):
            ap.add_argument(flag, nargs="*", default=value)
        else:
            ap.add_argument(flag, type=type(value), default=value)
    args = ap.parse_args()
    cfg = Settings(**{k: getattr(args, k) for k in asdict(d)})
    changed = {k: v for k, v in asdict(cfg).items() if v != asdict(d)[k]}

    db = duckdb.connect()
    db.execute("SET preserve_insertion_order = false")

    # Snapshots ---------------------------------------------------------------
    db.execute(
        f"""CREATE TABLE runs AS SELECT run_id, store, ok, coalesce(truncated, false) AS truncated
        FROM read_parquet('{args.runs}/runs-*.parquet', union_by_name = true) WHERE vantage = ?""",
        [cfg.vantage],
    )
    all_runs = [r for (r,) in db.execute("SELECT DISTINCT run_id FROM runs ORDER BY 1").fetchall()]
    incl_runs = runs_between(all_runs, cfg.inclusion_start, cfg.inclusion_end)
    bf_runs = runs_between(all_runs, cfg.bf_start, cfg.bf_end)
    jan_runs = runs_between(all_runs, cfg.jan_start, cfg.jan_end)
    fu_runs = {day: first_run_on(all_runs, day) for day in cfg.followups}
    if not incl_runs or not bf_runs:
        sys.exit(f"no snapshots in the inclusion window ({len(incl_runs)}) or the Black Friday window ({len(bf_runs)})")
    needed = max(bf_runs + jan_runs + [r for r in fu_runs.values() if r])
    reach = db.execute(f"SELECT max(run_id) FROM read_parquet('{args.history}')").fetchone()[0]
    if reach < needed:
        sys.exit(f"{args.history} stops at {reach}, before {needed}")

    # Stores ------------------------------------------------------------------
    stores = stores_at_cutoff(cfg.cutoff, args.stores_at_cutoff)
    stores["region"] = [region(c, cur) for c, cur in zip(stores.country, stores.currency)]
    stores["woo"] = (stores.platform == "woocommerce").astype(int)
    share_ok = (
        db.execute(
            f"SELECT store, count(*) FILTER (ok) / {len(incl_runs)} AS share FROM runs "
            f"WHERE run_id IN ({','.join(repr(r) for r in incl_runs)}) GROUP BY store"
        )
        .df()
        .set_index("store")["share"]
    )
    stores["share_ok"] = stores.store.map(share_ok).fillna(0.0)
    stores["included"] = stores.share_ok >= cfg.inclusion_share
    included = stores[stores.included]
    db.register("included_df", included[["store"]])
    db.execute("CREATE TABLE included AS SELECT store FROM included_df")

    # The history of the included stores only, up to the last snapshot needed.
    tmp = Path(tempfile.mkdtemp(prefix="bf-analysis-"))
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    hist = tmp / "history.parquet"
    db.execute(
        f"""COPY (SELECT * FROM read_parquet('{args.history}')
            WHERE run_id <= '{needed}' AND store IN (SELECT store FROM included))
        TO '{hist}' (FORMAT parquet)"""
    )

    # Store covariates at the first Black Friday snapshot: number of variants
    # listed and their median price, in euros.
    r0 = bf_runs[0]
    rates = load_rates(cfg.rates)
    cov = db.execute(
        f"""SELECT store, count(*) AS variants, median(price) AS median_price FROM (
            SELECT store, price FROM read_parquet('{hist}') WHERE run_id <= '{r0}'
            QUALIFY row_number() OVER (PARTITION BY store, variant_id ORDER BY run_id DESC) = 1)
        WHERE price IS NOT NULL GROUP BY store"""
    ).df()
    cov = cov.merge(stores[["store", "currency"]], on="store")
    cov["per_eur"] = cov.currency.map(rates)
    cov["log_variants"] = np.log(cov.variants)
    eur = cov.median_price / cov.per_eur  # no rate, or a median price of 0: no covariate
    cov["log_median_eur"] = np.log(eur.where(eur > 0))

    # Measures, once per snapshot ---------------------------------------------
    # bf: each variant at its first reduced snapshot in the Black Friday window.
    # episodes: each reduction that began in either window, at its first snapshot.
    def measures(run: str) -> str:
        return f"SELECT '{run}' AS run_id, * FROM ({measures_query(cfg.min_days)})"

    params = lambda run: {"history": str(hist), "run": run, "now": datetime.strptime(run, FMT)}  # noqa: E731
    first = True
    for run in sorted(set(bf_runs) | set(jan_runs)):
        db.execute("CREATE OR REPLACE TEMP TABLE ok AS SELECT store FROM runs "
                   f"WHERE run_id = '{run}' AND ok AND NOT truncated AND store IN (SELECT store FROM included)")  # fmt: skip
        db.execute(f"CREATE OR REPLACE TEMP TABLE m AS {measures(run)}", params(run))
        window = "bf" if run in bf_runs else "jan"
        lo, hi = (cfg.bf_start, cfg.bf_end) if window == "bf" else (cfg.jan_start, cfg.jan_end)
        ep = f"""SELECT *, '{window}' AS win FROM m WHERE episode_ok
            AND episode_start BETWEEN '{ts(lo)}' AND '{ts(hi)}'"""
        if first:
            db.execute("CREATE TABLE bf AS SELECT * FROM m WHERE false")
            db.execute(f"CREATE TABLE episodes AS {ep} AND false")
            first = False
        if window == "bf":
            db.execute("INSERT INTO bf SELECT * FROM m WHERE NOT EXISTS "
                       "(SELECT 1 FROM bf b WHERE b.store = m.store AND b.variant_id = m.variant_id)")  # fmt: skip
        db.execute(f"""INSERT INTO episodes SELECT * FROM ({ep}) e WHERE NOT EXISTS (SELECT 1 FROM episodes x
            WHERE x.store = e.store AND x.variant_id = e.variant_id AND x.episode_start = e.episode_start)""")
        print(f"{run} ({window}): {db.execute('SELECT count(*) FROM m').fetchone()[0]:,} reduced variants", file=sys.stderr)

    bf = db.execute("SELECT * FROM bf").df()
    flags = ["span_ok", "never_charged", "episode_ok", "above_low", "raise_then_cut"]
    for c in flags:  # NULL (not computable) counts as false; each is used only with its eligibility flag
        bf[c] = bf[c].astype("boolean").fillna(False).astype(bool)
    bf = bf.merge(stores[["store", "region", "woo", "platform"]], on="store").merge(
        cov[["store", "log_variants", "log_median_eur"]], on="store", how="left"
    )
    results: dict = {
        "settings": asdict(cfg),
        "changed_from_preregistration": changed,
        "snapshots": {"inclusion": len(incl_runs), "black_friday": bf_runs, "january": len(jan_runs), "followups": fu_runs},
        "stores": {
            "listed_at_cutoff": int(len(stores)),
            "included": int(stores.included.sum()),
            "included_by_region": {k: int(v) for k, v in included.groupby("region").size().items()},
        },
    }

    # H1 ----------------------------------------------------------------------
    elig = bf[bf.episode_ok].copy()
    elig["y"] = elig.above_low.astype(int)
    elig["eu"] = (elig.region == "EU").astype(int)
    h1_regions = {"EU", "US", "UK", "AU", "CA"}
    base = elig[elig.region.isin(h1_regions)]
    missing_cov = base[base.log_median_eur.isna() | base.log_variants.isna()]
    base = base.drop(missing_cov.index)
    results["h1_dropped_no_covariate"] = {"variants": int(len(missing_cov)), "stores": int(missing_cov.store.nunique())}
    eu_stores = base.loc[base.eu == 1, "store"].nunique()
    uk_stores = base.loc[base.region == "UK", "store"].nunique()
    full = "y ~ eu + log_variants + log_median_eur + woo"
    one_platform = "y ~ eu + log_variants + log_median_eur"

    def h1(sample: pd.DataFrame, rrp: set[str] | None) -> dict:
        r = {"main": logit(sample, full, "eu"), "store_level": store_level(sample, "y", "eu")}
        counts = sample.groupby("store").size().sort_values(ascending=False)
        top = set(counts.index[: max(1, int(round(len(counts) * cfg.top_share)))])
        r["without_top_stores"] = logit(sample[~sample.store.isin(top)], full, "eu")
        r["without_rrp_stores"] = (
            logit(sample[~sample.store.isin(rrp)], full, "eu") if rrp is not None
            else {"error": "not run: the validation coding (RRP labels) is not available yet"}
        )  # fmt: skip
        return r

    rrp = set(args.rrp_stores.read_text().split()) if args.rrp_stores else None
    span = bf[bf.span_ok & bf.region.isin(h1_regions)].dropna(subset=["log_variants", "log_median_eur"]).copy()
    span["y"] = span.never_charged.astype(int)
    span["eu"] = (span.region == "EU").astype(int)
    designs = {
        "H1a": (lambda df: df, {"EU", "US", "UK", "AU", "CA"}),
        "H1b": (lambda df: df[df.region.isin({"EU", "UK"})], {"EU", "UK"}),
    }
    for name, (pick, _) in designs.items():
        enough = eu_stores >= cfg.min_stores and (name == "H1a" or uk_stores >= cfg.min_stores)
        sample = pick(base)
        r = {"eu_stores": int(eu_stores), "uk_stores": int(uk_stores), "tested": bool(enough)}
        r |= h1(sample, rrp)
        r["never_charged"] = logit(pick(span), full, "eu")
        within = {}
        for plat, woo in (("woocommerce", 1), ("shopify", 0)):
            s = sample[sample.woo == woo]
            if name == "H1a" and plat == "woocommerce":  # WooCommerce EU against WooCommerce UK/AU/CA
                s = s[s.region.isin({"EU", "UK", "AU", "CA"})]
            g1, g0 = s.loc[s.eu == 1, "store"].nunique(), s.loc[s.eu == 0, "store"].nunique()
            w = {"eu_stores": int(g1), "other_stores": int(g0)}
            w |= (logit(s, one_platform, "eu") if min(g1, g0) >= cfg.min_stores
                  else {"error": f"fewer than {cfg.min_stores} stores in a group"})  # fmt: skip
            within[plat] = w
        r["within_platform"] = within
        results[name] = r
    tested = [n for n in designs if results[n]["tested"] and "p" in results[n]["main"]]
    if tested:
        reject, p_holm, _, _ = multipletests([results[n]["main"]["p"] for n in tested], alpha=cfg.alpha, method="holm")
        for n, rej, ph in zip(tested, reject, p_holm):
            results[n]["main"] |= {"p_holm": float(ph), "reject_at_alpha": bool(rej)}

    # H2 ----------------------------------------------------------------------
    gap = elig.assign(gap=elig.advertised - elig.real_avg).dropna(subset=["gap"])
    med = gap.groupby("store")["gap"].median()
    h2 = {"stores": int(len(med)), "variants": int(len(gap)), "median_of_store_medians": float(med.median()) if len(med) else None}
    if len(med) > 1:
        try:
            w = stats.wilcoxon(med.to_numpy(), alternative="two-sided", zero_method="wilcox")
            h2 |= {"wilcoxon_p": float(w.pvalue), "reject_at_alpha": bool(w.pvalue < cfg.alpha and med.median() > 0)}
        except ValueError as e:  # every store median exactly 0
            h2["error"] = str(e)
        h2["ci"] = boot_ci(med.to_numpy(), np.median, cfg.bootstrap, cfg.seed)
    results["H2"] = h2

    # H3 ----------------------------------------------------------------------
    ep = db.execute("SELECT store, win, raise_then_cut FROM episodes").df()
    ep["y"] = ep.raise_then_cut.astype("boolean").fillna(False).astype(int)
    h3 = {
        "episodes": {w: int((ep.win == w).sum()) for w in ("bf", "jan")},
        "share": {w: float(ep.loc[ep.win == w, "y"].mean()) if (ep.win == w).any() else None for w in ("bf", "jan")},
    }
    if len(ep):
        g = ep.groupby(["store", "win"])["y"]
        n, k = g.size().unstack(fill_value=0), g.sum().unstack(fill_value=0)
        tables = pd.DataFrame({
            "n1": n.get("bf", 0), "k1": k.get("bf", 0), "n0": n.get("jan", 0), "k0": k.get("jan", 0),
        }, index=n.index).astype(int)  # fmt: skip
        h3 |= fe_logit_binary(tables)
    else:
        h3 |= {"stores": 0, "reductions": 0, "error": "no reductions began in either window"}
    if "p" in h3 and "error" not in h3:
        h3["reject_at_alpha"] = bool(h3["p"] < cfg.alpha)
    results["H3"] = h3

    # D1 ----------------------------------------------------------------------
    d1 = {}
    for day, run in fu_runs.items():
        if not run:
            d1[day] = {"error": "no snapshot on that day"}
            continue
        f = db.execute(
            f"""SELECT b.store, (l.price <= b.price + {CENT})::INT AS y FROM bf b JOIN (
                SELECT store, variant_id, price FROM read_parquet('{hist}') WHERE run_id <= '{run}'
                QUALIFY row_number() OVER (PARTITION BY store, variant_id ORDER BY run_id DESC) = 1
            ) l USING (store, variant_id)
            WHERE l.price IS NOT NULL AND b.store IN
                (SELECT store FROM runs WHERE run_id = '{run}' AND ok AND NOT truncated)"""
        ).df()
        per = f.groupby("store")["y"].agg(["sum", "size"]).to_numpy()
        d1[day] = {"snapshot": run, "variants": int(len(f)), "stores": int(len(per))}
        if len(per):
            ratio = lambda a: a[:, 0].sum() / a[:, 1].sum()  # noqa: E731
            d1[day] |= {"share": float(ratio(per)), "ci": boot_ci(per, ratio, cfg.bootstrap, cfg.seed)}
    results["D1"] = d1

    # D2 ----------------------------------------------------------------------
    d2 = (
        bf.assign(gap=bf.advertised - bf.real_avg, el=bf.episode_ok, sp=bf.span_ok)
        .groupby(["region", "platform"])
        .apply(
            lambda g: pd.Series(
                {
                    "stores": g.store.nunique(),
                    "reduced_variants": len(g),
                    "eligible": int(g.el.sum()),
                    "share_above_30d_low": g.loc[g.el, "above_low"].astype(float).mean(),
                    "share_raise_then_cut": g.loc[g.el, "raise_then_cut"].astype(float).mean(),
                    "share_never_charged": g.loc[g.sp, "never_charged"].astype(float).mean(),
                    "median_advertised": g.advertised.median(),
                    "median_real_avg": g.loc[g.el, "real_avg"].median(),
                    "median_gap": g.loc[g.el, "gap"].median(),
                }
            ),
            include_groups=False,
        )
        .reset_index()
    )
    counts = ["stores", "reduced_variants", "eligible"]
    d2[counts] = d2[counts].astype(int)
    results["D2"] = json.loads(d2.to_json(orient="records"))

    # Output ------------------------------------------------------------------
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_text(json.dumps(results, indent=2, default=str) + "\n")
    d2.to_csv(args.out / "d2.csv", index=False)
    (args.out / "report.md").write_text(report(results))
    print(f"wrote {args.out}/report.md, results.json and d2.csv", file=sys.stderr)
    return 0


# --- report -----------------------------------------------------------------


def fmt(x, digits=3) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "–"
    if isinstance(x, float):
        return f"{x:.{digits}g}" if abs(x) < 1e-3 and x != 0 else f"{x:.{digits}f}"
    return f"{x:,}" if isinstance(x, int) else str(x)


def model_line(m: dict, unit: str = "variants") -> str:
    if "error" in m:
        return f"not estimated ({m['error']})"
    s = (f"OR {fmt(m['odds_ratio'])} [{fmt(m['or_ci'][0])}, {fmt(m['or_ci'][1])}], p = {fmt(m['p'], 4)}"
         f"; {fmt(m['variants'])} {unit} in {fmt(m['stores'])} stores")  # fmt: skip
    if "ame" in m:
        s += f"; AME {fmt(m['ame'])} [{fmt(m['ame_ci'][0])}, {fmt(m['ame_ci'][1])}]"
    if "p_holm" in m:
        s += f"; Holm p = {fmt(m['p_holm'], 4)}, {'reject' if m['reject_at_alpha'] else 'do not reject'} H0"
    return s


def report(r: dict) -> str:
    out = []
    if r["changed_from_preregistration"]:
        out.append("> **NOT THE PRE-REGISTERED ANALYSIS.** Settings differ from the pre-registration: "
                   + ", ".join(f"`{k}` = {v}" for k, v in r["changed_from_preregistration"].items())
                   + ". Use these numbers to check the code, not as tests of the hypotheses.\n")  # fmt: skip
    s = r["stores"]
    out += [
        "# Analysis report\n",
        f"Stores listed at the cut-off: {fmt(s['listed_at_cutoff'])}; included: {fmt(s['included'])} "
        f"({', '.join(f'{k} {fmt(int(v))}' for k, v in sorted(s['included_by_region'].items()))}).",
        f"Snapshots: {r['snapshots']['inclusion']} in the inclusion window, {len(r['snapshots']['black_friday'])} "
        f"in the Black Friday window, {r['snapshots']['january']} in the January window.\n",
        f"H1 variants dropped for lack of a covariate (no exchange rate or no listing): "
        f"{fmt(r['h1_dropped_no_covariate']['variants'])} in {fmt(r['h1_dropped_no_covariate']['stores'])} stores.\n",
    ]
    for name in ("H1a", "H1b"):
        h = r[name]
        out.append(f"## {name}\n")
        out.append(f"EU stores with an eligible reduced variant: {h['eu_stores']}; UK: {h['uk_stores']}. "
                   + ("Tested." if h["tested"] else "**Below the pre-registered minimum: reported descriptively.**"))  # fmt: skip
        out.append(f"\n- Main model: {model_line(h['main'])}")
        sl = h["store_level"]
        out.append(f"- (a) Store-level shares: EU {fmt(sl.get('mean_1'))} ({sl['stores_1']} stores), "
                   f"others {fmt(sl.get('mean_0'))} ({sl['stores_0']}); Welch p = {fmt(sl.get('welch_p'), 4)}, "
                   f"Mann-Whitney p = {fmt(sl.get('mann_whitney_p'), 4)}")  # fmt: skip
        out.append(f"- (b) Without the top 5% of stores: {model_line(h['without_top_stores'])}")
        out.append(f"- (c) Without RRP-labelled stores: {model_line(h['without_rrp_stores'])}")
        out.append(f"- (d) `never_charged` as the outcome: {model_line(h['never_charged'])}")
        for plat, w in h["within_platform"].items():
            out.append(f"- (e) Within {plat} ({w['eu_stores']} EU stores, {w['other_stores']} others): {model_line(w)}")
        out.append("")
    h2 = r["H2"]
    out += ["## H2\n", f"Median of store-level median gaps: {fmt(h2['median_of_store_medians'])} "
            f"[{fmt((h2.get('ci') or [None])[0])}, {fmt((h2.get('ci') or [None, None])[1])}], "
            f"Wilcoxon p = {fmt(h2.get('wilcoxon_p'), 4)}; {fmt(h2['stores'])} stores, {fmt(h2['variants'])} variants.\n"]  # fmt: skip
    h3 = r["H3"]
    out += ["## H3\n", f"Reductions beginning in the Black Friday window: {fmt(h3['episodes']['bf'])} "
            f"(raise-then-cut share {fmt(h3['share']['bf'])}); January: {fmt(h3['episodes']['jan'])} "
            f"(share {fmt(h3['share']['jan'])}).", f"\nStore fixed-effects logit: {model_line({**h3, 'variants': h3.get('reductions')}, 'reductions')}\n"]  # fmt: skip
    out.append("## D1\n")
    for day, d in r["D1"].items():
        out.append(f"- {day}: " + (d["error"] if "error" in d else
                   f"{fmt(d.get('share'))} [{fmt((d.get('ci') or [None])[0])}, {fmt((d.get('ci') or [None, None])[1])}] "
                   f"of {fmt(d['variants'])} variants in {fmt(d['stores'])} stores at or below the Black Friday price"))  # fmt: skip
    out.append("\n## D2\n\nBy region and platform: see `d2.csv`.\n")
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
