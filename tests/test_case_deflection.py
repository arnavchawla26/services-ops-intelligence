"""Case-deflection matcher on a tiny hand-made case/runbook set."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sois.case_deflection import BM25, choose_threshold, eligible_mask, load, prf, score_cases, tokenize
from sois.case_deflection import run as deflection_run

RUNBOOKS = [
    ("RB-A", "Expand a volume", "Storage Array", "how_to", "how do I expand a volume; grow LUN", "1. Open Volumes. 2. Expand."),
    ("RB-B", "Find the alert log", "Rack Server", "how_to", "where is the alert log; event log", "1. Maintenance > Event Log."),
    ("RB-C", "Replace a failed drive", "Storage Array", "drive_failure", "drive in bay reports failed; drive offline", "1. Dispatch sled."),
]


def make_case(k: int, asset: str, category: str, severity: int, symptom: str) -> dict:
    return {"case_id": f"CS-{k:05d}", "opened_at": "2026-04-01T00:00:00", "closed_at": "2026-04-02T00:00:00", "account_id": "ACC-1",
            "asset_id": asset, "category": category, "severity": severity,
            "description": f"{symptom}. Customer reports issue started 5 hours before opening the case. Severity {severity}.",
            "resolution_code": "customer_educated", "resolution_text": "x", "failure_reported": 0, "source_system": "svc_cloud"}


@pytest.fixture(scope="module")
def enterprise(tmp_path_factory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("enterprise")
    truth = tmp_path_factory.mktemp("truth")
    cases, truth_rows = [], []
    k = 0
    # Storage Array (AST-SA) and Rack Server (AST-RS); 12 runbook-phrase cases per family that are deflectable,
    # 12 generic-noise cases that are not, plus drive-failure cases that match RB-C strongly but are not deflectable.
    for asset, fam_phrases, rb in (("AST-SA", ["how do I expand a volume", "grow LUN"], "RB-A"),
                                   ("AST-RS", ["where is the alert log", "event log"], "RB-B")):
        for i in range(12):
            k += 1
            cases.append(make_case(k, asset, "how_to", 3, fam_phrases[i % 2]))
            truth_rows.append({"case_id": f"CS-{k:05d}", "deflectable": True, "deflect_reason": "runbook_covers_symptom", "matching_runbook_id": rb})
        for i in range(12):
            k += 1
            cases.append(make_case(k, asset, "performance", 3, f"throughput dropped {i} percent since last week"))
            truth_rows.append({"case_id": f"CS-{k:05d}", "deflectable": False, "deflect_reason": "rare_self_service", "matching_runbook_id": None})
    for i in range(6):
        k += 1
        cases.append(make_case(k, "AST-SA", "drive_failure", 1, "drive in bay reports failed"))
        truth_rows.append({"case_id": f"CS-{k:05d}", "deflectable": False, "deflect_reason": "needs_human", "matching_runbook_id": None})
    pd.DataFrame(cases).to_csv(root / "case.csv", index=False)
    pd.DataFrame(truth_rows).to_csv(truth / "case_truth.csv", index=False)
    pd.DataFrame(RUNBOOKS, columns=["runbook_id", "title", "product_family", "category", "symptoms", "steps"]).to_csv(root / "runbook.csv", index=False)
    pd.DataFrame({"asset_id": ["AST-SA", "AST-RS"], "site_id": ["S1", "S2"], "product_id": ["P-SA", "P-RS"]}).to_csv(root / "installed_asset.csv", index=False)
    pd.DataFrame({"product_id": ["P-SA", "P-RS"], "family": ["Storage Array", "Rack Server"]}).to_csv(root / "product.csv", index=False)
    return root, truth


def test_bm25_ranks_the_document_with_the_query_terms_first():
    docs = [tokenize(d) for d in ("expand a volume grow lun", "alert log event log maintenance", "drive failed offline rebuild")]
    bm = BM25(docs)
    s = bm.score(tokenize("how do I expand a volume"))
    assert s.argmax() == 0 and s[0] > s[1] and s[0] > s[2]
    assert bm.score(tokenize("nothing in vocabulary")).tolist() == [0.0, 0.0, 0.0]


def test_family_restriction_and_fallback():
    m = eligible_mask(pd.Series(["Storage Array", "Rack Server", "Edge Gateway"]), pd.Series(["Storage Array", "Rack Server", "Storage Array"]))
    assert m.tolist() == [[True, False, True], [False, True, False], [True, True, True]]


def test_best_runbook_respects_family(enterprise):
    root, truth = enterprise
    x, rb = load(root, truth)
    s = score_cases(x, rb)
    sa = s[(s["family"] == "Storage Array") & (s["category"] == "how_to")]
    rs = s[(s["family"] == "Rack Server") & (s["category"] == "how_to")]
    assert (sa["tfidf_runbook"] == "RB-A").all() and (sa["bm25_runbook"] == "RB-A").all()
    assert (rs["tfidf_runbook"] == "RB-B").all() and (rs["bm25_runbook"] == "RB-B").all()
    # a Rack Server case never matches a Storage Array runbook in family scope, even when the text would
    assert (rs["tfidf_runbook"] != "RB-A").all()
    # runbook-phrase cases outscore the generic-noise cases of the same family
    noise = s[(s["family"] == "Storage Array") & (s["category"] == "performance")]
    assert sa["tfidf_score"].min() > noise["tfidf_score"].max()
    assert sa["bm25_score"].min() > noise["bm25_score"].max()


def test_prf_and_threshold_choice():
    truth = np.array([True, True, False, False, True, False])
    flag = np.array([True, False, True, False, True, False])
    r = prf(flag, truth)
    assert (r["tp"], r["fp"], r["fn"]) == (2, 1, 1)
    assert r["precision"] == pytest.approx(2 / 3, abs=1e-4) and r["recall"] == pytest.approx(2 / 3, abs=1e-4)
    score = np.array([0.9, 0.8, 0.2, 0.1, 0.7, 0.3])
    assert choose_threshold(score, truth) == pytest.approx(0.7)
    # the severity gate is applied inside the F1 search
    assert choose_threshold(score, truth, extra=np.array([True, True, True, True, False, True])) == pytest.approx(0.8)


def test_run_writes_outputs_and_scores_against_truth(enterprise, tmp_path):
    root, truth = enterprise
    info = deflection_run(root, truth, tmp_path, seed=7)
    for f in ("case_deflection.csv", "case_deflection_pr_table.csv", "case_deflection_metrics.json", "case_deflection.png"):
        assert (tmp_path / f).exists()
    assert info["cases"] == 54 and info["runbooks"] == 3 and info["deflectable_in_truth"] == 24
    m = json.loads((tmp_path / "case_deflection_metrics.json").read_text())
    for v in ("tfidf", "bm25", "tfidf_all_runbooks", "bm25_all_runbooks", "tfidf+severity"):
        assert {"threshold_on_tune", "threshold_on_report"} <= set(m["variants"][v])
    t = m["variants"]["tfidf"]["threshold_on_tune"]
    assert t["evaluated_on"] == "report" and t["cases"] + m["variants"]["tfidf"]["threshold_on_report"]["cases"] == 54
    # the phrase cases are separable from the noise cases in family scope: recall 1, and the only false
    # positives possible are drive-failure cases, which the severity gate removes
    assert t["recall"] == 1.0 and t["precision"] >= 0.5
    assert m["variants"]["tfidf+severity"]["threshold_on_tune"]["precision"] == 1.0
    assert m["runbook_hit_rate_report_half"]["tfidf"] == 1.0
    assert "caveat" in m
    per_case = pd.read_csv(tmp_path / "case_deflection.csv")
    assert set(per_case["half"]) == {"tune", "report"}
    assert per_case["tfidf_candidate"].dtype == bool


def test_run_without_truth_writes_scores_only(enterprise, tmp_path):
    root, _ = enterprise
    info = deflection_run(root, None, tmp_path)
    assert "variants" not in info and (tmp_path / "case_deflection.csv").exists()
    assert not (tmp_path / "case_deflection.png").exists()
