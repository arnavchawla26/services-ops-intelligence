"""Parts forecast and repeat-dispatch analysis on a tiny hand-made enterprise."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sois.parts_forecast import backtest, complete_grid, ses_fit_alpha, ses_forecast, summarise, weekly_demand
from sois.parts_forecast import run as parts_run
from sois.repeat_dispatch import cross_case_repeats, parse_mixed_timestamp, rates
from sois.repeat_dispatch import run as repeat_run


@pytest.fixture(scope="module")
def enterprise(tmp_path_factory) -> tuple[Path, Path]:
    """14 weeks of dispatches: two regions, two parts, a deterministic weekly pattern
    plus one asset with a cross-case repeat and one case with three visits."""
    root = tmp_path_factory.mktemp("enterprise")
    truth = tmp_path_factory.mktemp("truth")
    rng = np.random.default_rng(3)
    weeks = pd.date_range("2026-03-30", periods=14, freq="7D")
    cases, dispatches, parts, truth_rows = [], [], [], []
    k = 0
    for wi, wk in enumerate(weeks):
        for region, asset in (("AMER-West", "AST-1"), ("EMEA", "AST-2")):
            n = 3 + (wi % 2) if region == "AMER-West" else 1 + int(rng.integers(0, 2))
            for j in range(n):
                k += 1
                cid, did = f"CS-{k:05d}", f"DSP-{k:05d}"
                opened = wk + pd.Timedelta(days=j, hours=3)
                fmt_iso = k % 3 != 0
                cases.append({"case_id": cid, "opened_at": opened.strftime("%Y-%m-%dT%H:%M:%S") if fmt_iso else opened.strftime("%m/%d/%Y %H:%M"),
                              "closed_at": (opened + pd.Timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S") if fmt_iso else (opened + pd.Timedelta(days=2)).strftime("%m/%d/%Y %H:%M"),
                              "account_id": "ACC-1", "asset_id": asset, "category": "drive_failure" if j % 2 == 0 else "how_to",
                              "severity": 2, "description": "x", "resolution_code": "hardware_replaced", "resolution_text": "y",
                              "failure_reported": 1, "source_system": "svc_cloud" if fmt_iso else "legacy_crm"})
                dispatches.append({"dispatch_id": did, "case_id": cid, "technician_id": "TEC-1" if region == "AMER-West" else "TEC-2",
                                   "scheduled_at": (opened + pd.Timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"), "visit_number": 1,
                                   "outcome": "resolved", "parts_used": "PRT-1" if j % 2 == 0 else "PRT-2"})
                parts.append({"dispatch_id": did, "part_id": "PRT-1" if j % 2 == 0 else "PRT-2", "quantity": 1})
                truth_rows.append({"dispatch_id": did, "case_id": cid, "necessary": j % 2 == 0, "reason": "hardware_fault", "is_repeat": False})
    # one case with three visits (visits 2 and 3 are same-case repeats; the third is avoidable)
    for v, nec in ((2, True), (3, False)):
        dispatches.append({"dispatch_id": f"DSP-R{v}", "case_id": "CS-00001", "technician_id": "TEC-1",
                           "scheduled_at": f"2026-04-0{v + 2}T10:00:00", "visit_number": v, "outcome": "resolved", "parts_used": ""})
        truth_rows.append({"dispatch_id": f"DSP-R{v}", "case_id": "CS-00001", "necessary": nec,
                           "reason": "follow_up_after_incomplete" if nec else "repeat_without_new_fault", "is_repeat": True})
    pd.DataFrame(cases).to_csv(root / "case.csv", index=False)
    pd.DataFrame(dispatches).to_csv(root / "dispatch.csv", index=False)
    pd.DataFrame(parts).to_csv(root / "dispatch_part.csv", index=False)
    pd.DataFrame(truth_rows).to_csv(truth / "dispatch_truth.csv", index=False)
    pd.DataFrame({"asset_id": ["AST-1", "AST-2"], "site_id": ["S1", "S2"], "product_id": ["P1", "P2"]}).to_csv(root / "installed_asset.csv", index=False)
    pd.DataFrame({"site_id": ["S1", "S2"], "region": ["AMER-West", "EMEA"]}).to_csv(root / "site.csv", index=False)
    pd.DataFrame({"product_id": ["P1", "P2"], "family": ["Rack Server", "Storage Array"]}).to_csv(root / "product.csv", index=False)
    pd.DataFrame({"technician_id": ["TEC-1", "TEC-2"], "name": ["A", "B"]}).to_csv(root / "technician.csv", index=False)
    pd.DataFrame({"part_id": ["PRT-1", "PRT-2"], "supplier_id": ["SUP-1", "SUP-2"], "lead_time_days": [5, 9],
                  "unit_cost_usd": [1, 2], "preferred": [True, True]}).to_csv(root / "part_supplier.csv", index=False)
    pd.DataFrame({"supplier_id": ["SUP-1", "SUP-2"], "name": ["Sled Co", "Fan Co"], "region": ["EMEA", "APJ"]}).to_csv(root / "supplier.csv", index=False)
    return root, truth


# ---------------- parts forecast
def test_weekly_demand_and_grid(enterprise):
    root, _ = enterprise
    dem = weekly_demand(root)
    assert set(dem["region"]) == {"AMER-West", "EMEA"}
    grid = complete_grid(dem, pd.Timestamp("2026-03-30"), pd.Timestamp("2026-06-29"))
    assert len(grid) == 14 * 2 * 2
    # AMER-West alternates 3,4,3,4 dispatches a week: PRT-1 on even j -> 2,2,2,2 ; PRT-2 -> 1,2,1,2
    w = grid[(grid["region"] == "AMER-West") & (grid["part_id"] == "PRT-2")].sort_values("week")["demand"].tolist()
    assert w[:4] == [1, 2, 1, 2]


def test_ses_alpha_is_fit_on_history_only():
    y = np.array([1.0, 2.0, 1.0, 2.0, 1.0, 2.0, 1.0, 2.0])
    a = ses_fit_alpha(y)
    assert 0.05 <= a <= 0.95
    assert ses_forecast(np.array([5.0, 5.0, 5.0]), a) == 5.0


def test_backtest_targets_are_after_origin(enterprise):
    root, _ = enterprise
    grid = complete_grid(weekly_demand(root), pd.Timestamp("2026-03-30"), pd.Timestamp("2026-06-29"))
    det = backtest(grid, n_origins=4)
    assert (det["target_week"] == det["origin"] + pd.Timedelta(days=7)).all()
    assert set(det["model"]) == {"naive", "ma4", "ses", "lgbm"}
    assert det["origin"].nunique() == 4
    # naive forecast equals the actual of the origin week
    nv = det[det["model"] == "naive"].merge(grid, left_on=["region", "part_id", "origin"], right_on=["region", "part_id", "week"])
    assert (nv["forecast"] == nv["demand"]).all()
    s = summarise(det)
    assert {"region_part", "by_part", "by_region", "fleet_total_by_part", "fleet_total"} <= set(s["level"])
    naive_mae = s[(s["level"] == "region_part") & (s["model"] == "naive")]["mae"].iloc[0]
    assert naive_mae >= 0


def test_parts_run_writes_outputs(enterprise, tmp_path):
    root, _ = enterprise
    info = parts_run(root, tmp_path, "2026-03-30", "2026-06-29", n_origins=4)
    assert info["weeks"] == 14 and info["series"] == 4
    for f in ("parts_forecast_backtest.csv", "parts_forecast_detail.csv", "parts_forecast.png", "parts_forecast_summary.json"):
        assert (tmp_path / f).exists()


# ---------------- repeat dispatch
def test_parse_mixed_timestamp():
    s = pd.Series(["2026-04-02T12:11:06", "04/02/2026 12:11", "garbage", None])
    out = parse_mixed_timestamp(s)
    assert out.iloc[0] == pd.Timestamp("2026-04-02 12:11:06")
    assert out.iloc[1] == pd.Timestamp("2026-04-02 12:11:00")
    assert out.iloc[2:].isna().all()


def test_repeat_definitions(enterprise, tmp_path):
    root, truth = enterprise
    info = repeat_run(root, truth, tmp_path)
    assert info["same_case_repeats"] == 2
    assert info["same_case_definition_matches_truth"] is True
    # AST-1 gets several cases per week that close within 2 days: every first visit after the first week is a cross-case repeat
    assert info["cross_case_repeats"] > 0
    assert info["repeats_necessary"] >= 1 and info["repeats_avoidable"] >= 1
    assert info["unparsed_case_timestamps"] == 0
    tab = pd.read_csv(tmp_path / "repeat_dispatch.csv")
    assert set(tab["dimension"]) == {"all", "product_family", "case_category", "region", "technician", "first_visit_part_supplier"}
    assert (tmp_path / "repeat_dispatch.png").exists()


def test_cross_case_window():
    x = pd.DataFrame({
        "dispatch_id": ["d1", "d2", "d3"], "asset_id": ["A", "A", "A"], "case_id": ["c1", "c2", "c3"],
        "visit_number": [1, 1, 1],
        "scheduled": pd.to_datetime(["2026-04-01", "2026-04-20", "2026-06-01"]),
        "closed": pd.to_datetime(["2026-04-05", "2026-04-25", "2026-06-05"]),
    })
    r = cross_case_repeats(x, window_days=30)
    assert r.tolist() == [False, True, False]
