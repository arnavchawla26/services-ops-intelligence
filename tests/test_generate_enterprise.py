from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sois.generate_enterprise import generate, load_inputs, write_outputs
from sois.load_telemetry import build_summaries, load_daily_csvs

FIXTURE = Path(__file__).parent / "fixtures" / "backblaze_synthetic"
SEED = 123


@pytest.fixture(scope="module")
def gen(synth_inputs):
    ds, dm = synth_inputs
    return generate(ds, dm, SEED)


@pytest.fixture(scope="module")
def written(gen, tmp_path_factory):
    out = tmp_path_factory.mktemp("ent")
    truth = tmp_path_factory.mktemp("truth")
    manifest = write_outputs(gen, out, truth, SEED, "test")
    return out, truth, manifest


# ---------------- determinism ----------------
def test_same_seed_same_bytes(synth_inputs, tmp_path):
    ds, dm = synth_inputs
    m1 = write_outputs(generate(ds, dm, SEED), tmp_path / "a", tmp_path / "ta", SEED, "x")
    m2 = write_outputs(generate(ds, dm, SEED), tmp_path / "b", tmp_path / "tb", SEED, "x")
    assert {k: v["sha256_csv"] for k, v in m1["tables"].items()} == {k: v["sha256_csv"] for k, v in m2["tables"].items()}
    assert {k: v["sha256_csv"] for k, v in m1["truth"].items()} == {k: v["sha256_csv"] for k, v in m2["truth"].items()}


def test_different_seed_different_bytes(synth_inputs, tmp_path):
    ds, dm = synth_inputs
    m1 = write_outputs(generate(ds, dm, SEED), tmp_path / "a", tmp_path / "ta", SEED, "x")
    m2 = write_outputs(generate(ds, dm, SEED + 1), tmp_path / "b", tmp_path / "tb", SEED + 1, "x")
    assert m1["tables"]["case"]["sha256_csv"] != m2["tables"]["case"]["sha256_csv"]


# ---------------- referential integrity ----------------
def _subset(child: pd.Series, parent: pd.Series) -> None:
    missing = set(child.dropna()) - set(parent)
    assert not missing, f"{child.name}: {sorted(missing)[:5]}"


def test_foreign_keys_resolve(gen):
    d, t = gen.data, gen.truth
    _subset(d["site"]["account_id"], d["account"]["account_id"])
    _subset(d["contact"]["account_id"], d["account"]["account_id"])
    _subset(d["installed_asset"]["site_id"], d["site"]["site_id"])
    _subset(d["installed_asset"]["product_id"], d["product"]["product_id"])
    _subset(d["installed_asset"]["order_id"], d["order"]["order_id"])
    _subset(d["order"]["account_id"], d["account"]["account_id"])
    _subset(d["order_line"]["order_id"], d["order"]["order_id"])
    _subset(d["order_line"]["product_id"], d["product"]["product_id"])
    _subset(d["part_supplier"]["part_id"], d["part"]["part_id"])
    _subset(d["part_supplier"]["supplier_id"], d["supplier"]["supplier_id"])
    _subset(d["inventory"]["part_id"], d["part"]["part_id"])
    _subset(d["case"]["asset_id"], d["installed_asset"]["asset_id"])
    _subset(d["case"]["account_id"], d["account"]["account_id"])
    _subset(d["dispatch"]["case_id"], d["case"]["case_id"])
    _subset(d["dispatch"]["technician_id"], d["technician"]["technician_id"])
    _subset(d["dispatch_part"]["dispatch_id"], d["dispatch"]["dispatch_id"])
    _subset(d["dispatch_part"]["part_id"], d["part"]["part_id"])
    _subset(d["lineage"]["job_id"], d["job"]["job_id"])
    # truth covers every row it is about
    assert set(t["case_truth"]["case_id"]) == set(d["case"]["case_id"])
    assert set(t["dispatch_truth"]["dispatch_id"]) == set(d["dispatch"]["dispatch_id"])
    assert set(t["asset_serial_map"]["asset_id"]) == set(d["installed_asset"]["asset_id"])
    assert len(t["account_resolution"]) == len(d["account"])


def test_ids_unique_where_they_should_be(gen):
    d = gen.data
    for name, col in [("site", "site_id"), ("contact", "contact_id"), ("installed_asset", "asset_id"),
                      ("order", "order_id"), ("order_line", "order_line_id"), ("case", "case_id"),
                      ("dispatch", "dispatch_id"), ("technician", "technician_id"), ("part", "part_id"),
                      ("supplier", "supplier_id"), ("runbook", "runbook_id"), ("account", "account_row_id")]:
        assert d[name][col].is_unique, name


# ---------------- mess rules ----------------
def test_account_has_three_spellings_and_duplicate_ids(gen):
    acc, truth = gen.data["account"], gen.truth["account_resolution"]
    assert not acc["account_id"].is_unique, "mess rule A3: some account_id values repeat"
    counts = truth.groupby("canonical_account_id").size()
    assert (counts == 3).any(), "mess rule A1: some companies appear with three spellings"
    assert (truth["variant_type"] == "duplicate_id").any(), "mess rule A2"
    # every spelling resolves to a canonical id that is itself a canonical row
    canon = set(truth.loc[truth["variant_type"] == "canonical", "account_id"])
    assert set(truth["canonical_account_id"]) <= canon


def test_two_date_formats(gen):
    case = gen.data["case"]
    iso = case["opened_at"].str.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
    us = case["opened_at"].str.match(r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}$")
    assert iso.any() and us.any() and (iso | us).all()
    assert (case.loc[us, "source_system"] == "legacy_crm").all()
    assert gen.data["order"]["order_date"].str.match(r"^\d{2}/\d{2}/\d{4}$").all()
    assert gen.data["installed_asset"]["install_date"].str.match(r"^\d{4}-\d{2}-\d{2}$").all()


def test_warranty_nulls_and_serial_prefixes(gen):
    ia, amap = gen.data["installed_asset"], gen.truth["asset_serial_map"]
    null_share = ia["warranty_end"].isna().mean()
    assert 0.05 < null_share < 0.15
    assert (amap["serial_mess"] == "vendor_prefix").any()
    prefixed = amap[amap["serial_mess"] == "vendor_prefix"]
    assert (prefixed["erp_serial"] != prefixed["telemetry_serial"]).all()
    assert all(e.endswith(t) for e, t in zip(prefixed["erp_serial"], prefixed["telemetry_serial"]))
    # join back to telemetry works through truth, not directly
    assert (ia["serial_number"] == amap["erp_serial"]).all()


def test_failure_defined_differently(gen, synth_inputs):
    ds, _ = synth_inputs
    case, truth = gen.data["case"], gen.truth["case_truth"]
    support_failures = set(case.loc[case["failure_reported"] == 1, "case_id"])
    telemetry_linked = set(truth.loc[truth["linked_to_telemetry_failure"], "case_id"])
    assert support_failures - telemetry_linked, "support failures with no telemetry failure must exist"
    assert telemetry_linked - support_failures, "telemetry failures without a part swap must exist"
    # and some telemetry failures never become a case at all
    failed_serials = set(ds.loc[ds["failed"] == 1, "serial_number"])
    assert failed_serials - set(truth["telemetry_serial"])


# ---------------- causal timing ----------------
def test_cases_cluster_around_telemetry_failures(gen):
    truth = gen.truth["case_truth"]
    linked = truth[truth["linked_to_telemetry_failure"]].copy()
    assert len(linked) > 20
    opened = pd.to_datetime(linked["opened_at_iso"])
    fd = pd.to_datetime(linked["telemetry_failure_date"])
    lag_days = (opened - fd).dt.total_seconds() / 86400
    assert (lag_days >= -22).all() and (lag_days <= 15).all()
    assert (lag_days < 0).any(), "some cases open before the failure flag"
    assert (lag_days >= 0).any(), "most cases open after the failure flag"
    assert (lag_days >= 0).mean() > 0.6


def test_dispatch_truth_logic(gen):
    d, t, ct = gen.data["dispatch"], gen.truth["dispatch_truth"], gen.truth["case_truth"]
    m = d.merge(t, on=["dispatch_id", "case_id"]).merge(ct[["case_id", "true_hardware_fault", "deflectable"]], on="case_id")
    assert (m.loc[m["visit_number"] > 1, "is_repeat"]).all()
    assert m["necessary"].any() and (~m["necessary"]).any()
    # necessary only ever when a real hardware fault exists; truth is about the
    # asset, not about what the technician wrote down (no_fault_found can be wrong)
    assert (m.loc[m["necessary"], "true_hardware_fault"]).all()
    assert (~m.loc[~m["true_hardware_fault"], "necessary"]).all()
    assert (~m.loc[m["deflectable"] & (m["visit_number"] == 1), "necessary"]).all()
    assert m["is_repeat"].any()


# ---------------- end to end through the real loader on the fixture ----------------
def test_end_to_end_on_fixture(tmp_path):
    pq = tmp_path / "pq"
    load_daily_csvs(FIXTURE, pq)
    build_summaries(pq)
    ds, dm, sha = load_inputs(pq)
    T = generate(ds, dm, 7)
    manifest = write_outputs(T, tmp_path / "ent", tmp_path / "truth", 7, sha)
    assert manifest["tables"]["installed_asset"]["rows"] == 6
    assert (tmp_path / "truth" / "generator_manifest.json").exists()
    assert set(T.truth["asset_serial_map"]["telemetry_serial"]) == set(ds["serial_number"])
