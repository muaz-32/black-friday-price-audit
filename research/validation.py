"""Measurement validation of the pre-registration ("Other planned analysis").

    python -m research.validation draw --history state/variant-history-<run>.parquet \\
        --info state/product-info-<run>.parquet --runs data --run <run_id>
    python -m research.validation shoot            # screenshots of the drawn pages
    python -m research.validation summarize        # after the coding is done

draw       A stratified random sample of variants flagged `above_30d_low`
           (episode_ok AND above_low, the H1 outcome) in one Black Friday
           snapshot: 50 each from the EU, UK, US and AU/CA, or all of a
           stratum's flagged variants if it has fewer. Seed 20261127. The
           flags come from collector.refprice.VARIANTS, the frozen code, through
           the same query as research/analysis.py. Writes sample.csv and
           draw.json to research/validation/.
shoot      One full-page screenshot per drawn page, taken while the Black
           Friday prices are live, with the project's own user agent, robots.txt
           respected, Shopify shown in the store's home market as in the
           snapshots, and no bot check ever solved. Writes the images and
           shots.csv (what was captured, when, or why not).
summarize  Reads coding.csv (exported from the coding page on the dashboard
           site): the share of confirmed was-price displays, RRP labels and
           30-day-low statements, by stratum, with Wilson 95% intervals; and
           rrp-stores.txt for `research.analysis --rrp-stores`. With
           --second, a second coder's independent coding of the same pages:
           agreement per question (share agreeing, Cohen's kappa with a 95%
           interval from a bootstrap over stores) and disagreements.csv. The
           results stay those of --coding, the primary coder.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import numpy as np
import pandas as pd

from collector import workdb
from research.analysis import FMT, PREREGISTERED, measures_query, region, stores_at_cutoff, ts

OUT = Path("research/validation")
SEED = 20261127
PER_STRATUM = 50
STRATA = ["EU", "UK", "US", "AU/CA"]
STRATUM = {"EU": "EU", "UK": "UK", "US": "US", "AU": "AU/CA", "CA": "AU/CA"}
ANSWERS = ("yes", "no", "unclear")
QUESTIONS = {
    "was_display": "Is the compare-at price displayed as a struck-through or 'was' price?",
    "rrp_label": "Is it labelled as a recommended retail price (RRP, UVP or similar)?",
    "low_30d": "Does the page show a separate 'lowest price in 30 days' statement?",
}
SAMPLE_COLS = [
    "n", "stratum", "region", "store", "platform", "country", "currency", "product_id", "variant_id",
    "title", "price", "compare_at", "url", "screenshot",
]  # fmt: skip


def product_url(base_url: str, platform: str, product_id: int, variant_id: int, handle: str | None) -> str:
    """Shopify pages live at /products/<handle>; WordPress finds any product by its id."""
    if platform == "woocommerce":
        return f"{base_url}/?post_type=product&p={product_id}"
    return f"{base_url}/products/{quote(handle or '')}?variant={variant_id}" if handle else ""


def draw_sample(flagged: pd.DataFrame, seed: int = SEED, per: int = PER_STRATUM) -> pd.DataFrame:
    """Up to `per` variants from each stratum, drawn with one generator in a fixed order."""
    rng = np.random.default_rng(seed)
    picked = []
    for stratum in STRATA:
        pool = flagged[flagged.stratum == stratum].sort_values(["store", "variant_id"]).reset_index(drop=True)
        k = min(per, len(pool))
        idx = np.sort(rng.choice(len(pool), size=k, replace=False)) if k else np.array([], dtype=int)
        picked.append(pool.iloc[idx])
    out = pd.concat(picked, ignore_index=True)
    out.insert(0, "n", range(1, len(out) + 1))
    return out


# --- draw -------------------------------------------------------------------


def cmd_draw(args) -> int:
    run = args.run
    if (args.out / "sample.csv").exists() and not args.force:
        print(f"{args.out / 'sample.csv'} exists: the sample is drawn once", file=sys.stderr)
        return 2
    cfg = PREREGISTERED
    lo, hi = ts(cfg.bf_start), ts(cfg.bf_end)
    if not (lo <= datetime.strptime(run, FMT) <= hi) and not args.test:
        print(f"{run} is outside the Black Friday window ({cfg.bf_start} to {cfg.bf_end})", file=sys.stderr)
        return 2
    stores = stores_at_cutoff(cfg.cutoff, args.stores_at_cutoff)
    stores["region"] = [region(c, cur) for c, cur in zip(stores.country, stores.currency)]
    current = pd.read_csv(args.stores, dtype=str, keep_default_na=False)[["domain", "base_url"]]
    stores = stores.merge(current.rename(columns={"domain": "store"}), on="store", how="left")

    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, workdb.connect(Path(tmp)) as db:
        reach = db.execute(f"SELECT max(run_id) FROM read_parquet('{args.history}')").fetchall()[0][0]
        if reach < run:
            print(f"{args.history} stops at {reach}, before {run}", file=sys.stderr)
            return 2
        db.register("listed", stores[["store"]])
        db.execute(
            f"""CREATE TEMP TABLE ok AS SELECT store FROM read_parquet('{args.runs}/runs-{run}.parquet')
            WHERE ok AND NOT coalesce(truncated, false) AND store IN (SELECT store FROM listed)"""
        )
        params = {"history": str(args.history), "run": run, "now": datetime.strptime(run, FMT)}
        db.execute(f"CREATE TEMP TABLE m AS {measures_query(args.min_days)}", params)
        flagged = db.execute("SELECT store, variant_id, price, compare_at FROM m WHERE episode_ok AND above_low").df()
        reduced = db.execute("SELECT count(*) FROM m").fetchall()[0][0]
        flagged = flagged.merge(stores, on="store")
        flagged["stratum"] = flagged.region.map(STRATUM)
        flagged = flagged.dropna(subset=["stratum"])
        sample = draw_sample(flagged)
        db.register("picked", sample[["store", "variant_id"]])
        ids = db.execute(
            f"""SELECT h.store, h.variant_id, any_value(h.product_id) AS product_id
            FROM read_parquet('{args.history}') h SEMI JOIN picked p USING (store, variant_id) GROUP BY ALL"""
        ).df()
        sample = sample.merge(ids, on=["store", "variant_id"], how="left")
        db.register("picked_products", sample[["store", "product_id"]])
        info = db.execute(
            f"""SELECT i.store, i.product_id, any_value(i.handle) AS handle, any_value(i.title) AS title
            FROM read_parquet('{args.info}') i SEMI JOIN picked_products p USING (store, product_id) GROUP BY ALL"""
        ).df()
    sample = sample.merge(info, on=["store", "product_id"], how="left")
    sample["url"] = [
        product_url(b, p, int(pid), int(vid), h if isinstance(h, str) else None)
        for b, p, pid, vid, h in zip(sample.base_url, sample.platform, sample.product_id, sample.variant_id, sample.handle)
    ]
    sample["screenshot"] = [f"{n:03d}.jpg" for n in sample.n]
    sample[SAMPLE_COLS].to_csv(args.out / "sample.csv", index=False)
    meta = {
        "run_id": run,
        "seed": SEED,
        "per_stratum": PER_STRATUM,
        "reduced_variants_in_run": int(reduced),
        "flagged_by_stratum": {s: int((flagged.stratum == s).sum()) for s in STRATA},
        "drawn_by_stratum": {s: int((sample.stratum == s).sum()) for s in STRATA},
        "history": Path(args.history).name,
        "code": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
        "drawn_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "test": bool(args.test),
    }
    (args.out / "draw.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta))
    return 0


# --- shoot ------------------------------------------------------------------


def cmd_shoot(args) -> int:
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import sync_playwright

    from collector import http

    sample = pd.read_csv(args.out / "sample.csv", dtype=str, keep_default_na=False)
    if args.limit:
        sample = sample.head(args.limit)
    shots = args.shots
    shots.mkdir(parents=True, exist_ok=True)
    session = http.new_session()
    robots: dict[str, http.Robots] = {}
    log = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for row in sample.itertuples():
            rec = {"n": row.n, "store": row.store, "url": row.url, "screenshot": "", "final_url": "", "status": "", "taken_at": "", "note": ""}
            log.append(rec)
            if not row.url:
                rec["note"] = "no product url (handle unknown)"
                continue
            parts = urlsplit(row.url)
            base = f"{parts.scheme}://{parts.netloc}"
            try:
                if base not in robots:
                    robots[base] = http.fetch_robots(session, base)
                if not robots[base].allowed(parts.path + (f"?{parts.query}" if parts.query else "")):
                    rec["note"] = "robots.txt disallows this page: code it by hand from the live page"
                    continue
            except http.FetchError as e:
                rec["note"] = f"robots.txt: {e}"
                continue
            context = browser.new_context(
                user_agent=http.USER_AGENT, viewport={"width": 1280, "height": 1600}, locale="en-US"
            )
            if row.platform == "shopify" and row.country:
                context.add_cookies([{"name": "localization", "value": row.country, "domain": parts.netloc, "path": "/"}])
            page = context.new_page()
            try:
                resp = page.goto(row.url, wait_until="load", timeout=45_000)
                page.wait_for_timeout(3_000)  # prices drawn by scripts after load
                rec["status"] = str(resp.status) if resp else ""
                rec["final_url"] = page.url
                rec["taken_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if resp and (resp.status in (202, 403, 429, 503) or "sgcaptcha" in page.url):
                    rec["note"] = "bot check or refusal: not captured, code it by hand from the live page"
                else:
                    page.screenshot(path=str(shots / row.screenshot), full_page=True, type="jpeg", quality=70)
                    rec["screenshot"] = row.screenshot
            except PWError as e:
                rec["note"] = f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"
            finally:
                context.close()
            print(f"[{row.n}] {rec['status'] or '-'} {row.store} {rec['note']}", flush=True)
            time.sleep(args.pause)
        browser.close()
    with open(args.out / "shots.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(log[0]))
        w.writeheader()
        w.writerows(log)
    taken = sum(bool(r["screenshot"]) for r in log)
    print(json.dumps({"pages": len(log), "screenshots": taken}))
    return 0


# --- summarize --------------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.959964) -> list[float | None]:
    if n == 0:
        return [None, None]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 4), round(c + h, 4)]


def read_coding(path: Path, sample: pd.DataFrame) -> pd.DataFrame:
    """One row per drawn variant with its answers ("" where not coded) and coder."""
    coding = pd.read_csv(path, dtype=str, keep_default_na=False)
    bad = [c for c in QUESTIONS if c not in coding.columns]
    if bad:
        sys.exit(f"{path} lacks the columns {bad}")
    if "coder" not in coding.columns:
        coding["coder"] = ""
    odd = sorted({v for q in QUESTIONS for v in coding[q]} - {*ANSWERS, ""})
    if odd:
        sys.exit(f"{path} has answers other than {ANSWERS}: {odd}")
    if coding.n.duplicated().any():
        sys.exit(f"{path} has variants coded twice: {sorted(coding.n[coding.n.duplicated()], key=int)}")
    df = sample[["n", "stratum", "store"]].merge(coding[["n", *QUESTIONS, "coder"]], on="n", how="left")
    return df.fillna("")


def kappa(a: pd.Series, b: pd.Series) -> float | None:
    """Cohen's kappa over yes/no/unclear; None when chance agreement is 1 (one answer throughout)."""
    if len(a) == 0:
        return None
    po = float((a.values == b.values).mean())
    pe = sum(float((a == c).mean()) * float((b == c).mean()) for c in ANSWERS)
    return None if pe >= 1 else (po - pe) / (1 - pe)


def kappa_ci(both: pd.DataFrame, q: str, reps: int = 2000) -> list[float | None]:
    """95% percentile interval for kappa, resampling stores (the sample clusters by store)."""
    groups = [g for _, g in both.groupby("store")]
    if len(groups) < 2:
        return [None, None]
    rng = np.random.default_rng(SEED)
    ks = []
    for _ in range(reps):
        r = pd.concat([groups[i] for i in rng.integers(0, len(groups), len(groups))])
        k = kappa(r[q + "_1"], r[q + "_2"])
        if k is not None:
            ks.append(k)
    if len(ks) < reps / 2:  # mostly undefined: no interval worth reporting
        return [None, None]
    return [round(float(np.percentile(ks, 2.5)), 4), round(float(np.percentile(ks, 97.5)), 4)]


def agreement(first: pd.DataFrame, second: pd.DataFrame, args) -> tuple[dict, list[str]]:
    both = first.merge(second[["n", *QUESTIONS, "coder"]], on="n", suffixes=("_1", "_2"))
    c1 = sorted(set(first.coder) - {""})
    c2 = sorted(set(second.coder) - {""})
    out: dict = {"coders": [c1, c2], "questions": {}}
    lines = ["## Agreement between two coders", ""]
    lines.append(f"Primary coder: {', '.join(c1) or 'not named'}. Second coder: {', '.join(c2) or 'not named'}.")
    if set(c1) & set(c2):
        lines.append(f"**Warning:** the same coder appears in both files ({', '.join(sorted(set(c1) & set(c2)))}); the codings should be independent.")
    lines += [
        "",
        "Kappa is Cohen's over yes/no/unclear, with a 95% interval from 2,000 bootstrap resamples of stores.",
        "When almost every answer is the same, kappa can be low even with high agreement; read it next to the share agreeing.",
        "",
        "| Question | Coded by both | Agree | Share agreeing | Kappa | 95% interval |",
        "|---|---:|---:|---:|---:|---|",
    ]
    rows = []
    for q, text in QUESTIONS.items():
        b = both[(both[q + "_1"] != "") & (both[q + "_2"] != "")]
        agree = int((b[q + "_1"] == b[q + "_2"]).sum())
        k = kappa(b[q + "_1"], b[q + "_2"])
        ci = kappa_ci(b, q)
        out["questions"][q] = {
            "both": len(b), "agree": agree, "share_agree": round(agree / len(b), 4) if len(b) else None,
            "kappa": None if k is None else round(k, 4), "kappa_ci95": ci,
        }  # fmt: skip
        share = "–" if not len(b) else f"{agree / len(b):.1%}"
        kf = "–" if k is None else f"{k:.2f}"
        cif = "–" if ci[0] is None else f"{ci[0]:.2f} to {ci[1]:.2f}"
        lines.append(f"| {q} | {len(b)} | {agree} | {share} | {kf} | {cif} |")
        for _, r in b[b[q + "_1"] != b[q + "_2"]].iterrows():
            rows.append({"n": r.n, "stratum": r.stratum, "store": r.store, "question": q, "first": r[q + "_1"], "second": r[q + "_2"]})
    rows.sort(key=lambda r: (int(r["n"]), list(QUESTIONS).index(r["question"])))
    with open(args.out / "disagreements.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["n", "stratum", "store", "question", "first", "second"])
        w.writeheader()
        w.writerows(rows)
    out["disagreements"] = len(rows)
    lines += ["", f"{len(rows)} disagreements, listed in `disagreements.csv`. The results above are the primary coder's.", ""]
    return out, lines


def cmd_summarize(args) -> int:
    sample = pd.read_csv(args.out / "sample.csv", dtype=str, keep_default_na=False)
    df = read_coding(args.coding, sample)
    out: dict = {"coded": int((df[list(QUESTIONS)] != "").all(axis=1).sum()), "drawn": len(df), "questions": QUESTIONS}
    lines = ["# Measurement validation", "", f"{out['coded']} of {out['drawn']} drawn variants coded.", ""]
    for q, text in QUESTIONS.items():
        out[q] = {}
        lines += [f"## {text}", "", "| Stratum | Yes | No | Unclear | Share yes (of yes/no) | 95% interval |", "|---|---:|---:|---:|---:|---|"]
        for s in [*STRATA, "All"]:
            sub = df if s == "All" else df[df.stratum == s]
            yes, no, unc = (int((sub[q] == a).sum()) for a in ANSWERS)
            lo_hi = wilson(yes, yes + no)
            share = round(yes / (yes + no), 4) if yes + no else None
            out[q][s] = {"yes": yes, "no": no, "unclear": unc, "share_yes": share, "ci95": lo_hi}
            fmt = "–" if share is None else f"{share:.1%}"
            ci = "–" if lo_hi[0] is None else f"{lo_hi[0]:.1%} to {lo_hi[1]:.1%}"
            lines.append(f"| {s} | {yes} | {no} | {unc} | {fmt} | {ci} |")
        lines.append("")
    rrp = sorted(set(df.loc[df.rrp_label == "yes", "store"]))
    out["rrp_stores"] = rrp
    (args.out / "rrp-stores.txt").write_text("".join(f"{s}\n" for s in rrp))
    lines += [f"Stores whose pages show RRP labels: {len(rrp)} (`rrp-stores.txt`, for `research.analysis --rrp-stores`).", ""]
    if args.second:
        out["agreement"], more = agreement(df, read_coding(args.second, sample), args)
        lines += more
    (args.out / "summary.json").write_text(json.dumps(out, indent=2) + "\n")
    (args.out / "summary.md").write_text("\n".join(lines))
    print("\n".join(lines))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draw")
    d.add_argument("--history", type=Path, required=True)
    d.add_argument("--info", type=Path, required=True, help="product-info file (handles and titles)")
    d.add_argument("--runs", type=Path, required=True, help="folder with runs-<run>.parquet")
    d.add_argument("--run", required=True, help="the Black Friday snapshot to draw from")
    d.add_argument("--stores", type=Path, default=Path("stores/stores.csv"), help="current list, for base URLs")
    d.add_argument("--stores-at-cutoff", type=Path)
    d.add_argument("--min-days", type=int, default=PREREGISTERED.min_days)
    d.add_argument("--force", action="store_true", help="draw again over an existing sample")
    d.add_argument("--test", action="store_true", help="allow a snapshot outside the window (trial runs only)")
    s = sub.add_parser("shoot")
    s.add_argument("--shots", type=Path, default=OUT / "screenshots")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--pause", type=float, default=2.0, help="seconds between pages")
    m = sub.add_parser("summarize")
    m.add_argument("--coding", type=Path, default=OUT / "coding.csv", help="the primary coder's answers")
    m.add_argument("--second", type=Path, help="a second coder's independent answers, for agreement")
    args = ap.parse_args()
    return {"draw": cmd_draw, "shoot": cmd_shoot, "summarize": cmd_summarize}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
