from pathlib import Path

import duckdb
import pytest

from sois.load_telemetry import build_summaries, load_daily_csvs, vendor_from_model

FIXTURE = Path(__file__).parent / "fixtures" / "backblaze_synthetic"


@pytest.fixture(scope="module")
def loaded(tmp_path_factory) -> tuple[Path, dict]:
    out = tmp_path_factory.mktemp("parquet")
    load_daily_csvs(FIXTURE, out)
    summary = build_summaries(out)
    return out, summary


def test_counts(loaded):
    _, s = loaded
    assert s["days"] == 3
    assert s["rows"] == 6 + 6 + 5
    assert s["drives"] == 6
    assert s["failures"] == 1
    assert s["failed_drives"] == 1
    assert s["models"] == 5


def test_partition_layout(loaded):
    out, _ = loaded
    parts = sorted(p.name for p in (out / "telemetry_daily").iterdir())
    assert parts == ["day=2026-04-01", "day=2026-04-02", "day=2026-04-03"]
    for p in parts:
        assert (out / "telemetry_daily" / p / "data.parquet").exists()


def test_schema_is_stable_across_drifting_files(loaded):
    out, _ = loaded
    con = duckdb.connect()
    cols_per_file = []
    for p in sorted((out / "telemetry_daily").glob("*/data.parquet")):
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{p}')").fetchall()]
        cols_per_file.append(cols)
    assert cols_per_file[0] == cols_per_file[1] == cols_per_file[2]
    assert "smart_199_raw" in cols_per_file[0]
    assert "smart_240_raw" in cols_per_file[0]
    # day 2 had no smart_199 column: it must be NULL, not an error
    nulls = con.execute(
        f"SELECT count(*) FILTER (WHERE smart_199_raw IS NULL), count(*) "
        f"FROM read_parquet('{out / 'telemetry_daily' / 'day=2026-04-02' / 'data.parquet'}')"
    ).fetchone()
    assert nulls == (6, 6)


def test_drive_summary(loaded):
    out, _ = loaded
    con = duckdb.connect()
    row = con.execute(
        f"SELECT model, first_seen, last_seen, days_observed, failed, failure_date "
        f"FROM read_parquet('{out / 'drive_summary.parquet'}') WHERE serial_number = 'SYNTH0003'"
    ).fetchone()
    assert row[0] == "HGST SYNTHMODEL"
    assert str(row[1]) == "2026-04-01" and str(row[2]) == "2026-04-02"
    assert row[3] == 2 and row[4] == 1 and str(row[5]) == "2026-04-02"
    healthy = con.execute(
        f"SELECT count(*) FROM read_parquet('{out / 'drive_summary.parquet'}') WHERE failed = 0 AND days_observed = 3"
    ).fetchone()[0]
    assert healthy == 5


def test_drive_model_vendors(loaded):
    out, _ = loaded
    con = duckdb.connect()
    rows = dict(
        con.execute(f"SELECT model, vendor FROM read_parquet('{out / 'drive_model.parquet'}')").fetchall()
    )
    assert rows == {
        "ST00000SYNTH": "Seagate",
        "HGST SYNTHMODEL": "HGST",
        "TOSHIBA SYNTHMODEL": "Toshiba",
        "WDC SYNTHMODEL": "Western Digital",
        "UNKNOWNVENDOR SYNTH": "Unknown",
    }
    counts = dict(
        con.execute(f"SELECT model, drive_count FROM read_parquet('{out / 'drive_model.parquet'}')").fetchall()
    )
    assert counts["ST00000SYNTH"] == 2


@pytest.mark.parametrize(
    "model,vendor",
    [
        ("ST12000NM0008", "Seagate"),
        ("HGST HUH721212ALE600", "HGST"),
        ("TOSHIBA MG07ACA14TA", "Toshiba"),
        ("WDC WUH721816ALE6L4", "Western Digital"),
        ("Hitachi HDS5C4040ALE630", "Hitachi"),
        ("", "Unknown"),
        (None, "Unknown"),
    ],
)
def test_vendor_rules(model, vendor):
    assert vendor_from_model(model) == vendor
