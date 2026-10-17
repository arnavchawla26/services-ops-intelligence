"""30-day drive failure risk as a dispatch-budget decision.

Question: on prediction date t, which drives will fail in the next 30 days,
and if the fleet can roll B trucks a day, how many of those failures does each
ranking catch and how many trucks roll for nothing?

Label.  y = 1 when the drive has failure = 1 on any day in (t, t + horizon],
horizon 30 days, taken from drive_summary.failure_date (one failure row per
drive in the Backblaze data).  A drive that leaves the fleet without a failure
flag inside the window is a 0 (censored, counted as such in the metrics JSON).

Features at t use only rows with date <= t:
    smart_{5,187,188,197,198} raw and normalized at t (Backblaze's five),
    smart_{9,194,199,241,242} raw at t, capacity_bytes, model,
    7-day delta (value at t minus value at t-7) and 7-day max of the raw
    reallocated / pending / uncorrectable counts (5, 187, 197, 198),
    days_seen_7 (rows in the last 8 days; a new drive has fewer).
A missing t-7 row leaves the delta null; LightGBM takes nulls natively, the
logistic regression gets zeros plus missing indicators.

Split (no label leakage).  Train prediction dates run daily from
train_start to train_end = T; their labels reach T + horizon.  Test prediction
dates start strictly after T + horizon, so no failure that is a test label was
ever a training label.  Default on the real quarter: train 2026-04-08 to
2026-04-24 (labels through 2026-05-24), test 2026-05-25 to 2026-05-31 (labels
through 2026-06-30).  Within the training period 20% of drives (hash bucket)
are held out; the models are fit on the other 80%, and the held-out drives on
the last cal_days prediction dates form the calibration slice (later in time
and drive-disjoint from the fit rows, never downsampled).

Negatives in the fit rows are downsampled to neg_rate (default 0.10); all
positives are kept.  Calibration (Platt scaling on the logit) is fit on the
un-downsampled calibration slice, which also undoes the prior shift.  The test
window is never downsampled and nothing is tuned on it.

Models: (a) Backblaze heuristic, score = number of the five attributes with
raw > 0 (ties broken by a seeded random jitter so a budget can be applied),
(b) logistic regression, (c) LightGBM.  (b) and (c) are both Platt-calibrated
on the calibration slice so their Brier scores are comparable.

Outputs under --reports: failure_model_metrics.json, dispatch_budget.csv and
.png, dispatch_budget_curve.csv (budgets 1..1000, for the dashboard
slider), reliability.csv, fleet_risk_*.csv (scores at --score-date joined to
the generated enterprise, when --enterprise is given).  Models and the full
drive-day test scores (test_scores.parquet) under --models.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

BB_FIVE: tuple[int, ...] = (5, 187, 188, 197, 198)
EXTRA: tuple[int, ...] = (9, 194, 199, 241, 242)
DELTA_IDS: tuple[int, ...] = (5, 187, 197, 198)
BUDGETS: tuple[int, ...] = (10, 25, 50, 100, 250)
TOP_FRACTIONS: tuple[float, ...] = (0.001, 0.01)


def feature_columns() -> list[str]:
    cols: list[str] = []
    for n in BB_FIVE:
        cols += [f"smart_{n}_raw", f"smart_{n}_normalized"]
    for n in EXTRA:
        cols.append(f"smart_{n}_raw")
    cols.append("capacity_bytes")
    for n in DELTA_IDS:
        cols += [f"smart_{n}_delta7", f"smart_{n}_max7"]
    cols.append("days_seen_7")
    return cols


NUMERIC_FEATURES: list[str] = feature_columns()


# ---------------------------------------------------------------- features
def _glob(parquet_dir: Path) -> str:
    return str(parquet_dir / "telemetry_daily" / "*" / "*.parquet").replace("'", "''")


def build_features(con: duckdb.DuckDBPyConnection, parquet_dir: Path, t: date, lookback: int = 7) -> pd.DataFrame:
    """One row per drive observed on t. Reads only partitions t-lookback..t."""
    g = _glob(parquet_dir)
    lo, hi = (t - timedelta(days=lookback)).isoformat(), t.isoformat()
    cur_cols = ", ".join(
        [f"smart_{n}_raw, smart_{n}_normalized" for n in BB_FIVE] + [f"smart_{n}_raw" for n in EXTRA]
    )
    lag_cols = ", ".join(f"smart_{n}_raw AS smart_{n}_lag7" for n in DELTA_IDS)
    max_cols = ", ".join(f"max(smart_{n}_raw) AS smart_{n}_max7" for n in DELTA_IDS)
    delta_cols = ", ".join(f"c.smart_{n}_raw - l.smart_{n}_lag7 AS smart_{n}_delta7" for n in DELTA_IDS)
    sql = f"""
    WITH w AS (
      SELECT date, serial_number, model, capacity_bytes, datacenter, vault_id, {cur_cols}
      FROM read_parquet('{g}', hive_partitioning=true)
      WHERE day >= '{lo}' AND day <= '{hi}'
    ),
    c AS (SELECT * FROM w WHERE date = DATE '{hi}'),
    l AS (SELECT serial_number, {lag_cols} FROM w WHERE date = DATE '{lo}'),
    a AS (SELECT serial_number, {max_cols}, count(*) AS days_seen_7 FROM w GROUP BY serial_number)
    SELECT c.*, {delta_cols}, {", ".join(f"a.smart_{n}_max7" for n in DELTA_IDS)}, a.days_seen_7
    FROM c LEFT JOIN l USING (serial_number) LEFT JOIN a USING (serial_number)
    """
    # sorted so the seeded downsampling draws the same rows on every run
    # (DuckDB's parallel scan does not promise row order)
    df = con.execute(sql).df().sort_values("serial_number", kind="stable").reset_index(drop=True)
    df["t"] = pd.Timestamp(t)
    return df


def add_labels(df: pd.DataFrame, summary: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """y = failure_date in (t, t + horizon]. summary: serial_number, failure_date, last_seen."""
    s = summary[["serial_number", "failure_date", "last_seen"]]
    out = df.merge(s, on="serial_number", how="left")
    fd = pd.to_datetime(out["failure_date"])
    t = out["t"]
    out["y"] = ((fd > t) & (fd <= t + pd.Timedelta(days=horizon))).astype(np.int8)
    ls = pd.to_datetime(out["last_seen"])
    out["censored"] = ((out["y"] == 0) & (ls < t + pd.Timedelta(days=horizon))).astype(np.int8)
    return out.drop(columns=["failure_date", "last_seen"])


def heuristic_score(df: pd.DataFrame, rng: np.random.Generator | None = None) -> np.ndarray:
    """Backblaze's rule: any of SMART 5/187/188/197/198 raw > 0 flags the drive.
    Score = number of flagged attributes (0..5) plus a seeded jitter in (0, 0.5)
    so a daily budget can pick a definite top-B among tied drives."""
    flags = np.zeros(len(df), dtype=np.float64)
    for n in BB_FIVE:
        flags += (df[f"smart_{n}_raw"].fillna(0).to_numpy() > 0)
    if rng is None:
        rng = np.random.default_rng(0)
    return flags + rng.random(len(df)) * 0.5


def holdout_bucket(serials: pd.Series, n_buckets: int = 5) -> np.ndarray:
    """Deterministic drive hash in [0, n_buckets). Bucket 0 is the calibration holdout."""
    def h(s: str) -> int:
        return int(hashlib.blake2b(s.encode(), digest_size=4).hexdigest(), 16) % n_buckets
    return serials.map(h).to_numpy()


# ---------------------------------------------------------------- models
@dataclass
class ModelBundle:
    model_categories: list[str]
    lr_models: list[str]                     # one-hot vocabulary for the logistic regression
    lr_scaler_mean: np.ndarray = field(default_factory=lambda: np.zeros(0))
    lr_scaler_std: np.ndarray = field(default_factory=lambda: np.ones(0))
    lr_coef: np.ndarray = field(default_factory=lambda: np.zeros(0))
    lr_intercept: float = 0.0
    lgbm_model_str: str = ""
    platt: dict[str, tuple[float, float]] = field(default_factory=dict)   # name -> (a, b): p = sigmoid(a*logit + b)
    lgbm_params: dict = field(default_factory=dict)


def _lr_matrix(df: pd.DataFrame, vocab: list[str]) -> np.ndarray:
    """Numeric features: log1p on raw counters, zero-fill, missing indicators, model one-hot."""
    cols = []
    for c in NUMERIC_FEATURES:
        v = df[c].to_numpy(dtype=np.float64)
        miss = np.isnan(v)
        if c.endswith("_raw") or c.endswith("_max7") or c == "capacity_bytes":
            v = np.log1p(np.clip(v, 0, None))
        elif c.endswith("_delta7"):
            v = np.sign(v) * np.log1p(np.abs(v))
        v = np.where(miss, 0.0, v)
        cols.append(v)
        if c in ("smart_187_raw", "smart_188_raw", "smart_5_delta7", "smart_241_raw"):
            cols.append(miss.astype(np.float64))
    m = df["model"].to_numpy()
    for name in vocab:
        cols.append((m == name).astype(np.float64))
    return np.column_stack(cols)


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-7, 1 - 1e-7)
    return np.log(p / (1 - p))


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-z))


def fit_platt(raw_scores: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Logistic regression of y on logit(raw score): p = sigmoid(a*logit + b)."""
    from sklearn.linear_model import LogisticRegression
    z = _logit(raw_scores).reshape(-1, 1)
    lr = LogisticRegression(C=1e6, max_iter=1000)
    lr.fit(z, y)
    return float(lr.coef_[0][0]), float(lr.intercept_[0])


def apply_platt(raw_scores: np.ndarray, ab: tuple[float, float]) -> np.ndarray:
    a, b = ab
    return _sigmoid(a * _logit(raw_scores) + b)


def fit_models(fit_df: pd.DataFrame, model_categories: list[str], seed: int, lgbm_params: dict | None = None) -> ModelBundle:
    import lightgbm as lgb
    from sklearn.linear_model import LogisticRegression

    y = fit_df["y"].to_numpy()
    counts = fit_df["model"].value_counts()
    lr_vocab = [m for m in counts.index if counts[m] >= 200]
    bundle = ModelBundle(model_categories=model_categories, lr_models=lr_vocab)

    X = _lr_matrix(fit_df, lr_vocab)
    mean, std = X.mean(axis=0), X.std(axis=0)
    std[std == 0] = 1.0
    lr = LogisticRegression(C=0.1, max_iter=2000)
    lr.fit((X - mean) / std, y)
    bundle.lr_scaler_mean, bundle.lr_scaler_std = mean, std
    bundle.lr_coef, bundle.lr_intercept = lr.coef_[0].copy(), float(lr.intercept_[0])

    params = {
        "objective": "binary", "learning_rate": 0.05, "num_leaves": 31, "min_data_in_leaf": 200,
        "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 10.0,
        "verbose": -1, "seed": seed, "num_threads": 8, "deterministic": True, "force_row_wise": True,
    }
    if lgbm_params:
        params.update(lgbm_params)
    n_rounds = int(params.pop("num_boost_round", 400))
    Xg = fit_df[NUMERIC_FEATURES + ["model"]].copy()
    Xg["model"] = pd.Categorical(Xg["model"], categories=model_categories)
    ds = lgb.Dataset(Xg, label=y, categorical_feature=["model"], free_raw_data=True)
    booster = lgb.train(params, ds, num_boost_round=n_rounds)
    bundle.lgbm_model_str = booster.model_to_string()
    bundle.lgbm_params = {**params, "num_boost_round": n_rounds}
    return bundle


def predict_raw(bundle: ModelBundle, df: pd.DataFrame) -> dict[str, np.ndarray]:
    import lightgbm as lgb
    X = _lr_matrix(df, bundle.lr_models)
    z = ((X - bundle.lr_scaler_mean) / bundle.lr_scaler_std) @ bundle.lr_coef + bundle.lr_intercept
    out = {"logistic": _sigmoid(z)}
    booster = lgb.Booster(model_str=bundle.lgbm_model_str)
    Xg = df[NUMERIC_FEATURES + ["model"]].copy()
    Xg["model"] = pd.Categorical(Xg["model"], categories=bundle.model_categories)
    out["lightgbm"] = booster.predict(Xg)
    return out


def lgbm_importance(bundle: ModelBundle, top: int = 15) -> list[dict]:
    import lightgbm as lgb
    booster = lgb.Booster(model_str=bundle.lgbm_model_str)
    gain = booster.feature_importance(importance_type="gain")
    names = booster.feature_name()
    order = np.argsort(gain)[::-1][:top]
    total = float(gain.sum()) or 1.0
    return [{"feature": names[i], "gain_share": round(float(gain[i]) / total, 4)} for i in order]


# ---------------------------------------------------------------- metrics
def pr_auc(y: np.ndarray, s: np.ndarray) -> float:
    from sklearn.metrics import average_precision_score
    return float(average_precision_score(y, s))


def recall_at_fraction(y: np.ndarray, s: np.ndarray, frac: float) -> float:
    k = max(1, int(round(len(y) * frac)))
    idx = np.argsort(-s, kind="stable")[:k]
    return float(y[idx].sum() / max(1, y.sum()))


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def reliability_table(y: np.ndarray, p: np.ndarray, n_bins: int = 10) -> pd.DataFrame:
    """Quantile bins of predicted probability: mean predicted vs observed rate."""
    order = np.argsort(p, kind="stable")
    bins = np.array_split(order, n_bins)
    rows = []
    for i, b in enumerate(bins):
        rows.append({
            "bin": i + 1, "n": int(len(b)), "p_min": float(p[b].min()), "p_max": float(p[b].max()),
            "mean_predicted": float(p[b].mean()), "observed_rate": float(y[b].mean()), "positives": int(y[b].sum()),
        })
    return pd.DataFrame(rows)


def dispatch_budget_table(test: pd.DataFrame, score_cols: dict[str, str], budgets: tuple[int, ...] | range = BUDGETS) -> pd.DataFrame:
    """Per prediction date, dispatch to the top-B drives by score; a truck is
    'caught' when that drive fails within the horizon, else it rolled for nothing.
    Days are scored independently (a drive flagged on two days counts twice).
    Scores are sorted once per (model, day); caught at every B is a cumulative sum."""
    days = sorted(test["t"].unique())
    y_all = test["y"].to_numpy()
    t_all = test["t"].to_numpy()
    budgets = list(budgets)
    rows = []
    for name, col in score_cols.items():
        s_all = test[col].to_numpy()
        caught = np.zeros(len(budgets), dtype=np.int64)
        trucks = np.zeros(len(budgets), dtype=np.int64)
        positives = 0
        for d in days:
            m = t_all == d
            y, s = y_all[m], s_all[m]
            cum = np.cumsum(y[np.argsort(-s, kind="stable")])
            positives += int(y.sum())
            for i, B in enumerate(budgets):
                k = min(B, len(y))
                caught[i] += int(cum[k - 1])
                trucks[i] += k
        for i, B in enumerate(budgets):
            c, w = int(caught[i]), int(trucks[i] - caught[i])
            rows.append({
                "model": name, "daily_budget": B, "days": len(days),
                "trucks_total": c + w, "caught_total": c, "wasted_total": w, "positives_total": positives,
                "caught_per_day": round(c / len(days), 2), "wasted_per_day": round(w / len(days), 2),
                "positives_per_day": round(positives / len(days), 2),
                "recall": round(c / max(1, positives), 4), "precision": round(c / max(1, c + w), 4),
            })
    return pd.DataFrame(rows)


def plot_dispatch_budget(table: pd.DataFrame, path: Path, title: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7.5, 4.5), dpi=130)
    styles = {"heuristic": ("#8a8a8a", "--", "s"), "logistic": ("#3b6ea5", "-", "^"), "lightgbm": ("#b5452b", "-", "o")}
    for name, g in table.groupby("model", sort=False):
        color, ls, mk = styles.get(name, ("#222", "-", "o"))
        ax.plot(g["daily_budget"], g["caught_per_day"], ls, color=color, marker=mk, label=name)
    pos = table["positives_per_day"].iloc[0]
    ax.axhline(pos, color="#444", lw=0.8, ls=":")
    ax.text(table["daily_budget"].min(), pos, f"  failures in the next 30 days per day: {pos:.0f}", va="bottom", fontsize=8, color="#444")
    ax.set_xscale("log")
    ax.set_xticks(sorted(table["daily_budget"].unique()))
    ax.set_xticklabels([str(b) for b in sorted(table["daily_budget"].unique())])
    ax.set_xlabel("trucks per day (dispatch budget)")
    ax.set_ylabel("failures caught per day (mean over test days)")
    ax.set_title(title, fontsize=10)
    ax.grid(alpha=0.3)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


# ---------------------------------------------------------------- fleet scoring (enterprise join)
def score_fleet(con: duckdb.DuckDBPyConnection, parquet_dir: Path, bundle: ModelBundle, t: date,
                enterprise_dir: Path | None, truth_dir: Path | None, reports: Path, top_n: int = 100) -> dict:
    df = build_features(con, parquet_dir, t)
    raw = predict_raw(bundle, df)
    df["p_fail_30d"] = apply_platt(raw["lightgbm"], bundle.platt["lightgbm"])
    df["heuristic_flag"] = (heuristic_score(df) >= 1).astype(int)
    keep = ["serial_number", "model", "capacity_bytes", "datacenter", "vault_id", "p_fail_30d", "heuristic_flag",
            "smart_5_raw", "smart_187_raw", "smart_188_raw", "smart_197_raw", "smart_198_raw", "smart_9_raw"]
    fleet = df[keep].copy()
    info = {"score_date": t.isoformat(), "drives_scored": int(len(fleet)),
            "expected_failures_30d": round(float(fleet["p_fail_30d"].sum()), 1)}

    if enterprise_dir and truth_dir and (truth_dir / "asset_serial_map.csv").exists():
        amap = pd.read_csv(truth_dir / "asset_serial_map.csv", usecols=["asset_id", "telemetry_serial"])
        assets = pd.read_csv(enterprise_dir / "installed_asset.csv", usecols=["asset_id", "site_id", "product_id", "warranty_end"])
        sites = pd.read_csv(enterprise_dir / "site.csv", usecols=["site_id", "account_id", "site_name", "city", "region"])
        res = pd.read_csv(truth_dir / "account_resolution.csv", usecols=["account_id", "account_name", "canonical_account_id", "variant_type"])
        canon = res[res["variant_type"] == "canonical"][["canonical_account_id", "account_name"]].rename(
            columns={"canonical_account_id": "canonical_id", "account_name": "account_name"})
        site_acc = sites.merge(res[["account_id", "canonical_account_id"]].drop_duplicates("account_id"), on="account_id", how="left")
        site_acc = site_acc.merge(canon, left_on="canonical_account_id", right_on="canonical_id", how="left")
        joined = (fleet.merge(amap, left_on="serial_number", right_on="telemetry_serial", how="left")
                       .merge(assets, on="asset_id", how="left")
                       .merge(site_acc[["site_id", "site_name", "city", "region", "account_name"]], on="site_id", how="left"))
        info["drives_matched_to_assets"] = int(joined["asset_id"].notna().sum())
        top = joined.sort_values("p_fail_30d", ascending=False).head(top_n)
        top.to_csv(reports / "fleet_risk_top100.csv", index=False)
        site_tab = (joined.groupby(["site_id", "site_name", "city", "region", "account_name"], dropna=False)
                    .agg(drives=("serial_number", "count"), expected_failures_30d=("p_fail_30d", "sum"),
                         max_p=("p_fail_30d", "max"), heuristic_flagged=("heuristic_flag", "sum"))
                    .reset_index().sort_values("expected_failures_30d", ascending=False))
        site_tab["expected_failures_30d"] = site_tab["expected_failures_30d"].round(2)
        site_tab["max_p"] = site_tab["max_p"].round(4)
        site_tab.to_csv(reports / "fleet_risk_sites.csv", index=False)
    else:
        fleet.sort_values("p_fail_30d", ascending=False).head(top_n).to_csv(reports / "fleet_risk_top100.csv", index=False)

    vault_tab = (fleet.groupby(["datacenter", "vault_id"], dropna=False)
                 .agg(drives=("serial_number", "count"), expected_failures_30d=("p_fail_30d", "sum"),
                      max_p=("p_fail_30d", "max"), heuristic_flagged=("heuristic_flag", "sum"))
                 .reset_index().sort_values("expected_failures_30d", ascending=False))
    vault_tab["expected_failures_30d"] = vault_tab["expected_failures_30d"].round(2)
    vault_tab["max_p"] = vault_tab["max_p"].round(4)
    vault_tab.to_csv(reports / "fleet_risk_vaults.csv", index=False)
    return info


# ---------------------------------------------------------------- pipeline
def daterange(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def run(parquet_dir: Path, reports: Path, models_dir: Path, train_start: date, train_end: date,
        test_start: date, test_end: date, horizon: int = 30, neg_rate: float = 0.10, cal_days: int = 5,
        seed: int = 20261017, enterprise_dir: Path | None = None, truth_dir: Path | None = None,
        score_date: date | None = None, lgbm_params: dict | None = None, budgets: tuple[int, ...] = BUDGETS,
        threads: int = 8, log=print) -> dict:
    if test_start <= train_end + timedelta(days=horizon):
        raise ValueError(f"test_start {test_start} must be after train_end + horizon = {train_end + timedelta(days=horizon)}")
    reports.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    rng = np.random.default_rng(seed)
    con = duckdb.connect()
    con.execute(f"SET threads = {int(threads)}")
    summary = con.execute(f"SELECT serial_number, failure_date, last_seen FROM read_parquet('{parquet_dir / 'drive_summary.parquet'}')").df()
    model_categories = sorted(con.execute(f"SELECT DISTINCT model FROM read_parquet('{parquet_dir / 'drive_model.parquet'}')").df()["model"].dropna().tolist())

    # ---- training rows: fit (80% of drives, negatives downsampled) and calibration (20% of drives, last cal_days dates)
    train_dates = daterange(train_start, train_end)
    cal_dates = set(train_dates[-cal_days:])
    fit_parts, cal_parts = [], []
    n_train_rows = n_train_pos = 0
    for t in train_dates:
        df = add_labels(build_features(con, parquet_dir, t), summary, horizon)
        n_train_rows += len(df)
        n_train_pos += int(df["y"].sum())
        bucket = holdout_bucket(df["serial_number"])
        fit = df[bucket != 0]
        keep = (fit["y"].to_numpy() == 1) | (rng.random(len(fit)) < neg_rate)
        fit_parts.append(fit[keep])
        if t in cal_dates:
            cal_parts.append(df[bucket == 0])
        log(f"train {t}: rows={len(df):,} positives={int(df['y'].sum())} kept={int(keep.sum()):,}")
    fit_df = pd.concat(fit_parts, ignore_index=True)
    cal_df = pd.concat(cal_parts, ignore_index=True)
    del fit_parts, cal_parts
    log(f"fit rows={len(fit_df):,} positives={int(fit_df['y'].sum()):,}; cal rows={len(cal_df):,} positives={int(cal_df['y'].sum()):,}")

    bundle = fit_models(fit_df, model_categories, seed, lgbm_params)
    cal_raw = predict_raw(bundle, cal_df)
    cal_y = cal_df["y"].to_numpy()
    for name, s in cal_raw.items():
        bundle.platt[name] = fit_platt(s, cal_y)
    cal_metrics = {name: {"pr_auc": round(pr_auc(cal_y, s), 4), "brier_calibrated": round(brier(cal_y, apply_platt(s, bundle.platt[name])), 6)}
                   for name, s in cal_raw.items()}
    with open(models_dir / "failure_model_bundle.pkl", "wb") as f:
        pickle.dump(bundle, f)
    (models_dir / "lightgbm.txt").write_text(bundle.lgbm_model_str)

    # ---- test window: every drive-day, no downsampling
    test_parts = []
    for t in daterange(test_start, test_end):
        df = add_labels(build_features(con, parquet_dir, t), summary, horizon)
        raw = predict_raw(bundle, df)
        df["heuristic"] = heuristic_score(df, rng)
        df["heuristic_flag"] = (df["heuristic"] >= 1).astype(np.int8)
        for name, s in raw.items():
            df[f"{name}_raw"] = s
            df[name] = apply_platt(s, bundle.platt[name])
        test_parts.append(df)
        log(f"test {t}: rows={len(df):,} positives={int(df['y'].sum())}")
    test = pd.concat(test_parts, ignore_index=True)
    del test_parts
    y = test["y"].to_numpy()
    score_cols = {"heuristic": "heuristic", "logistic": "logistic", "lightgbm": "lightgbm"}

    metrics: dict = {
        "config": {
            "horizon_days": horizon, "train_prediction_dates": [train_start.isoformat(), train_end.isoformat()],
            "train_labels_through": (train_end + timedelta(days=horizon)).isoformat(),
            "test_prediction_dates": [test_start.isoformat(), test_end.isoformat()],
            "test_labels_through": (test_end + timedelta(days=horizon)).isoformat(),
            "gap_days_between_last_train_label_and_first_test_prediction": (test_start - train_end - timedelta(days=horizon)).days,
            "negative_downsample_rate_fit": neg_rate, "holdout_drive_share_for_calibration": 0.2,
            "calibration_prediction_dates": sorted(d.isoformat() for d in cal_dates), "calibration": "platt",
            "seed": seed, "lightgbm_params": bundle.lgbm_params, "features": NUMERIC_FEATURES + ["model (categorical)"],
        },
        "counts": {
            "train_drive_days": n_train_rows, "train_positive_drive_days": n_train_pos,
            "fit_rows_after_downsampling": int(len(fit_df)), "fit_positives": int(fit_df["y"].sum()),
            "cal_rows": int(len(cal_df)), "cal_positives": int(cal_y.sum()),
            "test_drive_days": int(len(test)), "test_positive_drive_days": int(y.sum()),
            "test_distinct_failing_drives": int(test.loc[test["y"] == 1, "serial_number"].nunique()),
            "test_censored_drive_days": int(test["censored"].sum()),
            "test_days": int(test["t"].nunique()),
        },
        "calibration_slice": cal_metrics,
        "test": {},
    }
    for name, col in score_cols.items():
        s = test[col].to_numpy()
        m = {"pr_auc": round(pr_auc(y, s), 4)}
        for frac in TOP_FRACTIONS:
            m[f"recall_top_{frac:g}"] = round(recall_at_fraction(y, s, frac), 4)
        if name != "heuristic":
            m["brier_calibrated"] = round(brier(y, s), 6)
            m["brier_uncalibrated"] = round(brier(y, test[f"{name}_raw"].to_numpy()), 6)
        metrics["test"][name] = m
    metrics["test"]["baseline_rate"] = round(float(y.mean()), 6)
    metrics["test"]["brier_constant_predictor"] = round(float(y.mean() * (1 - y.mean())), 6)
    flagged = test["heuristic_flag"].to_numpy() == 1
    metrics["test"]["heuristic_flag_set"] = {
        "flagged_share_of_drive_days": round(float(flagged.mean()), 4),
        "flagged_per_day": round(float(flagged.sum()) / test["t"].nunique(), 1),
        "recall_if_every_flagged_drive_dispatched": round(float(y[flagged].sum() / max(1, y.sum())), 4),
        "precision_of_flag": round(float(y[flagged].mean()) if flagged.any() else 0.0, 4),
    }
    rel = reliability_table(y, test["lightgbm"].to_numpy())
    rel.to_csv(reports / "reliability.csv", index=False)
    metrics["reliability_lightgbm"] = rel.round(6).to_dict(orient="records")
    metrics["lightgbm_feature_importance_gain"] = lgbm_importance(bundle)

    table = dispatch_budget_table(test, score_cols, budgets)
    table.to_csv(reports / "dispatch_budget.csv", index=False)
    metrics["dispatch_budget"] = table.to_dict(orient="records")
    plot_dispatch_budget(table, reports / "dispatch_budget.png",
                         f"Daily dispatch budget vs failures caught, test {test_start} to {test_end} ({int(y.sum()):,} positive drive-days)")

    curve = dispatch_budget_table(test, score_cols, range(1, 1001))
    curve[["model", "daily_budget", "caught_per_day", "wasted_per_day", "positives_per_day", "recall", "precision"]].to_csv(
        reports / "dispatch_budget_curve.csv", index=False)
    # full drive-day scores are large (tens of MB on the real quarter); they stay out of git under models/
    test[["t", "serial_number", "model", "datacenter", "vault_id", "y", "heuristic", "heuristic_flag", "logistic", "lightgbm"]].to_parquet(
        models_dir / "test_scores.parquet", index=False)

    if score_date is not None:
        metrics["fleet_scoring"] = score_fleet(con, parquet_dir, bundle, score_date, enterprise_dir, truth_dir, reports)

    metrics["runtime_seconds"] = round(time.time() - t0, 1)
    (reports / "failure_model_metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    con.close()
    return metrics


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parquet", type=Path, default=Path("data/parquet"))
    ap.add_argument("--enterprise", type=Path, default=Path("data/enterprise"))
    ap.add_argument("--truth", type=Path, default=Path("truth"))
    ap.add_argument("--reports", type=Path, default=Path("reports"))
    ap.add_argument("--models", type=Path, default=Path("models"))
    ap.add_argument("--train-start", type=date.fromisoformat, default=date(2026, 4, 8))
    ap.add_argument("--train-end", type=date.fromisoformat, default=date(2026, 4, 24))
    ap.add_argument("--test-start", type=date.fromisoformat, default=date(2026, 5, 25))
    ap.add_argument("--test-end", type=date.fromisoformat, default=date(2026, 5, 31))
    ap.add_argument("--score-date", type=date.fromisoformat, default=date(2026, 6, 30), help="fleet scoring date for the dashboard; 'none' to skip")
    ap.add_argument("--no-score", action="store_true")
    ap.add_argument("--horizon", type=int, default=30)
    ap.add_argument("--neg-rate", type=float, default=0.10)
    ap.add_argument("--cal-days", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20261017)
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args(argv)
    m = run(args.parquet, args.reports, args.models, args.train_start, args.train_end, args.test_start, args.test_end,
            horizon=args.horizon, neg_rate=args.neg_rate, cal_days=args.cal_days, seed=args.seed,
            enterprise_dir=args.enterprise if args.enterprise.exists() else None,
            truth_dir=args.truth if args.truth.exists() else None,
            score_date=None if args.no_score else args.score_date, threads=args.threads)
    print(json.dumps({"counts": m["counts"], "test": m["test"]}, indent=2))
    for r in m["dispatch_budget"]:
        print(f"{r['model']:>10} budget={r['daily_budget']:>4} caught/day={r['caught_per_day']:>7} wasted/day={r['wasted_per_day']:>7} recall={r['recall']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
