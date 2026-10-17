"""Failure model on a synthetic 60-day fleet. Checks the split has no label
leakage, features use only days <= t, the heuristic, the budget arithmetic,
and that the whole pipeline runs end to end on small data."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from sois.failure_model import (
    BB_FIVE, add_labels, build_features, dispatch_budget_table, heuristic_score, holdout_bucket,
    recall_at_fraction, reliability_table, run,
)
from sois.load_telemetry import KEPT_COLUMNS

START = date(2026, 4, 1)
N_DAYS = 60
N_DRIVES = 400


def synthetic_fleet(root: Path, seed: int = 7) -> pd.DataFrame:
    """Write telemetry_daily/day=.../data.parquet plus drive_summary and drive_model.
    Failing drives ramp smart_197/198 before their last day, so there is signal."""
    rng = np.random.default_rng(seed)
    models = ["ST00000SYNTH", "HGST SYNTHMODEL", "TOSHIBA SYNTHMODEL"]
    serial = [f"SYNTH{i:05d}" for i in range(N_DRIVES)]
    model = rng.choice(models, N_DRIVES)
    fail_day = np.full(N_DRIVES, -1)
    failing = rng.choice(N_DRIVES, 40, replace=False)
    fail_day[failing] = rng.integers(12, N_DAYS, 40)
    rows = []
    for d in range(N_DAYS):
        day = START + timedelta(days=d)
        for i in range(N_DRIVES):
            if fail_day[i] >= 0 and d > fail_day[i]:
                continue
            fails_today = int(fail_day[i] == d)
            ramp = fail_day[i] >= 0 and d >= fail_day[i] - 10
            r = {c: None for c in KEPT_COLUMNS}
            r.update({
                "date": day, "serial_number": serial[i], "model": model[i],
                "capacity_bytes": 12_000_000_000_000, "failure": fails_today, "datacenter": "synA",
                "cluster_id": "1", "vault_id": str(1000 + i % 10), "pod_id": "1", "pod_slot_num": i % 60,
                "is_legacy_format": False,
                "smart_5_raw": int(rng.poisson(8 if ramp else 0.02)), "smart_5_normalized": 100,
                "smart_187_raw": 0, "smart_187_normalized": 100, "smart_188_raw": 0, "smart_188_normalized": 100,
                "smart_197_raw": int(rng.poisson(20 if ramp else 0.01)), "smart_197_normalized": 100,
                "smart_198_raw": int(rng.poisson(20 if ramp else 0.01)), "smart_198_normalized": 100,
                "smart_9_raw": 10_000 + d * 24 + i, "smart_194_raw": 30, "smart_199_raw": 0,
                "smart_241_raw": 1_000_000 + d, "smart_242_raw": 2_000_000 + d,
            })
            rows.append(r)
    df = pd.DataFrame(rows)
    for c, typ in KEPT_COLUMNS.items():
        if typ == "BIGINT" or typ == "INTEGER":
            df[c] = df[c].astype("Int64")
    for d in range(N_DAYS):
        day = (START + timedelta(days=d)).isoformat()
        p = root / "telemetry_daily" / f"day={day}"
        p.mkdir(parents=True)
        df[df["date"] == START + timedelta(days=d)].to_parquet(p / "data.parquet", index=False)
    con = duckdb.connect()
    con.execute(f"""COPY (SELECT serial_number, arg_max(model, date) model, max(capacity_bytes) capacity_bytes,
        min(date) first_seen, max(date) last_seen, count(*) days_observed, max(failure) failed,
        max(CASE WHEN failure = 1 THEN date END) failure_date
        FROM read_parquet('{root}/telemetry_daily/*/*.parquet') GROUP BY serial_number)
        TO '{root}/drive_summary.parquet' (FORMAT PARQUET)""")
    con.execute(f"""COPY (SELECT model, count(*) drive_count FROM read_parquet('{root}/drive_summary.parquet') GROUP BY model)
        TO '{root}/drive_model.parquet' (FORMAT PARQUET)""")
    return pd.DataFrame({"serial_number": serial, "fail_day": fail_day})


@pytest.fixture(scope="module")
def fleet(tmp_path_factory):
    root = tmp_path_factory.mktemp("parquet")
    truth = synthetic_fleet(root)
    return root, truth


def test_features_only_from_past(fleet):
    root, _ = fleet
    con = duckdb.connect()
    t = START + timedelta(days=20)
    f = build_features(con, root, t)
    assert len(f) == con.execute(f"SELECT count(*) FROM read_parquet('{root}/telemetry_daily/day={t}/data.parquet')").fetchone()[0]
    # smart_9 at t equals the row on day t (not a later day); delta equals t minus t-7
    row = f[f["serial_number"] == "SYNTH00001"].iloc[0]
    assert row["smart_9_raw"] == 10_000 + 20 * 24 + 1
    direct = con.execute(
        f"SELECT a.smart_5_raw - b.smart_5_raw FROM read_parquet('{root}/telemetry_daily/day={t}/data.parquet') a "
        f"JOIN read_parquet('{root}/telemetry_daily/day={t - timedelta(days=7)}/data.parquet') b USING (serial_number) "
        f"WHERE serial_number = 'SYNTH00001'").fetchone()[0]
    assert row["smart_5_delta7"] == direct
    assert row["days_seen_7"] == 8


def test_labels_match_window(fleet):
    root, truth = fleet
    con = duckdb.connect()
    t = START + timedelta(days=15)
    summary = con.execute(f"SELECT serial_number, failure_date, last_seen FROM read_parquet('{root}/drive_summary.parquet')").df()
    f = add_labels(build_features(con, root, t), summary, horizon=30)
    expect = truth[(truth["fail_day"] > 15) & (truth["fail_day"] <= 45)]["serial_number"]
    got = f.loc[f["y"] == 1, "serial_number"]
    assert set(got) == set(expect)
    # a drive failing on t itself is present but is not a positive (its failure is not in (t, t+30])
    on_t = truth[truth["fail_day"] == 15]["serial_number"]
    assert f[f["serial_number"].isin(on_t)]["y"].eq(0).all()


def test_heuristic_and_budget_math():
    df = pd.DataFrame({f"smart_{n}_raw": [0, 0, 3, 0] for n in BB_FIVE})
    df.loc[1, "smart_197_raw"] = 1
    s = heuristic_score(df, np.random.default_rng(1))
    assert (np.floor(s) == np.array([0, 1, 5, 0])).all()
    test = pd.DataFrame({
        "t": [pd.Timestamp("2026-05-25")] * 4 + [pd.Timestamp("2026-05-26")] * 4,
        "y": [1, 0, 1, 0, 0, 1, 0, 0],
        "s": [0.9, 0.8, 0.1, 0.0, 0.5, 0.9, 0.1, 0.0],
    })
    tab = dispatch_budget_table(test, {"m": "s"}, budgets=(1, 2))
    b1 = tab[tab["daily_budget"] == 1].iloc[0]
    assert (b1["caught_total"], b1["wasted_total"], b1["positives_total"]) == (2, 0, 3)
    b2 = tab[tab["daily_budget"] == 2].iloc[0]
    assert (b2["caught_total"], b2["wasted_total"], b2["recall"]) == (2, 2, pytest.approx(2 / 3, abs=1e-3))
    assert recall_at_fraction(np.array([1, 0, 1, 0]), np.array([0.9, 0.8, 0.1, 0.0]), 0.5) == 0.5
    rel = reliability_table(np.array([0, 0, 1, 1]), np.array([0.1, 0.2, 0.8, 0.9]), n_bins=2)
    assert rel["observed_rate"].tolist() == [0.0, 1.0]
    b = holdout_bucket(pd.Series(["a", "b", "a"]))
    assert b[0] == b[2] and set(b) <= set(range(5))


def test_split_rejects_leakage(fleet, tmp_path):
    root, _ = fleet
    with pytest.raises(ValueError):
        run(root, tmp_path / "r", tmp_path / "m", START + timedelta(days=8), START + timedelta(days=12),
            START + timedelta(days=40), START + timedelta(days=42), horizon=30)


def test_end_to_end(fleet, tmp_path):
    root, _ = fleet
    reports, models = tmp_path / "reports", tmp_path / "models"
    # train t = day 8..14 (labels to day 44), test t = day 45..47 (labels to day 77 > fleet end: fine, censored)
    m = run(root, reports, models, START + timedelta(days=8), START + timedelta(days=14),
            START + timedelta(days=45), START + timedelta(days=47), horizon=30, neg_rate=0.5, cal_days=2,
            score_date=START + timedelta(days=50), lgbm_params={"num_boost_round": 30, "min_data_in_leaf": 5},
            budgets=(1, 5, 10), log=lambda *_: None)
    assert m["config"]["gap_days_between_last_train_label_and_first_test_prediction"] == 1
    assert m["counts"]["test_positive_drive_days"] > 0
    for name in ("heuristic", "logistic", "lightgbm"):
        assert 0 <= m["test"][name]["pr_auc"] <= 1
    assert m["test"]["lightgbm"]["pr_auc"] > m["test"]["baseline_rate"]
    for f in ("failure_model_metrics.json", "dispatch_budget.csv", "dispatch_budget_curve.csv", "dispatch_budget.png",
              "reliability.csv", "fleet_risk_top100.csv", "fleet_risk_vaults.csv"):
        assert (reports / f).exists(), f
    assert (models / "lightgbm.txt").exists()
    j = json.loads((reports / "failure_model_metrics.json").read_text())
    assert len(j["dispatch_budget"]) == 3 * 3
    scores = pd.read_parquet(models / "test_scores.parquet")
    assert scores["lightgbm"].between(0, 1).all()
