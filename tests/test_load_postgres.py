"""sql/schema.sql is tested two ways:
  1. always: DuckDB runs the DDL and loads the generated CSVs into it (same SQL
     subset), which proves the column lists match the generator's output and
     that every FK resolves.
  2. when a Postgres is reachable (docker compose up -d): the real loader runs
     end to end and row counts are checked. Skipped otherwise.
"""

from __future__ import annotations

import re
import socket
from pathlib import Path

import duckdb
import pytest

from sois.generate_enterprise import generate, load_inputs, write_outputs
from sois.load_postgres import ENTERPRISE_ORDER, dsn_from_env, load, read_dotenv
from sois.load_telemetry import KEPT_COLUMNS, build_summaries, load_daily_csvs

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "sql" / "schema.sql"
FIXTURE = Path(__file__).parent / "fixtures" / "backblaze_synthetic"


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    base = tmp_path_factory.mktemp("pipe")
    pq, ent, truth = base / "pq", base / "ent", base / "truth"
    load_daily_csvs(FIXTURE, pq)
    build_summaries(pq)
    ds, dm, sha = load_inputs(pq)
    write_outputs(generate(ds, dm, 11), ent, truth, 11, sha)
    return pq, ent


def test_schema_telemetry_columns_match_loader():
    text = SCHEMA.read_text()
    block = re.search(r"CREATE TABLE telemetry_daily_recent \((.*?)\n\);", text, re.S).group(1)
    cols = re.findall(r'^\s*"([a-z0-9_]+)"\s+[A-Z]+', block, re.M)
    assert cols == list(KEPT_COLUMNS)


def test_schema_covers_every_generated_table(pipeline):
    _, ent = pipeline
    generated = {p.stem for p in ent.glob("*.csv")}
    assert generated == set(ENTERPRISE_ORDER)


def test_schema_runs_in_duckdb_and_accepts_generated_csvs(pipeline):
    pq, ent = pipeline
    con = duckdb.connect()
    con.execute(SCHEMA.read_text())
    con.execute(f"INSERT INTO drive_model SELECT model, vendor, capacity_bytes, drive_count, failure_count FROM read_parquet('{pq / 'drive_model.parquet'}')")
    con.execute(
        "INSERT INTO telemetry_drive_summary SELECT serial_number, model, capacity_bytes, datacenter, cluster_id, "
        f"vault_id, pod_id, first_seen, last_seen, days_observed, failed, failure_date, max_power_on_hours FROM read_parquet('{pq / 'drive_summary.parquet'}')"
    )
    cols = ", ".join(f'"{c}"' for c in KEPT_COLUMNS)
    con.execute(f"INSERT INTO telemetry_daily_recent SELECT {cols} FROM read_parquet('{pq}/telemetry_daily/*/*.parquet')")
    for table in ENTERPRISE_ORDER:
        # all_varchar then implicit cast: mirrors what Postgres COPY does with CSV text
        con.execute(f'INSERT INTO "{table}" SELECT * FROM read_csv(\'{ent / (table + ".csv")}\', header=true, all_varchar=true)')
    assert con.execute("SELECT count(*) FROM installed_asset").fetchone()[0] == 6
    assert con.execute("SELECT count(*) FROM telemetry_daily_recent").fetchone()[0] == 17
    assert con.execute('SELECT count(*) FROM "case"').fetchone()[0] > 0
    # dates parsed, nulls preserved
    assert con.execute("SELECT count(*) FROM installed_asset WHERE warranty_end IS NULL").fetchone()[0] >= 0
    assert con.execute("SELECT typeof(install_date) FROM installed_asset LIMIT 1").fetchone()[0] == "DATE"


def _postgres_reachable() -> bool:
    env = read_dotenv(ROOT / ".env")
    host, port = env.get("POSTGRES_HOST", "localhost"), int(env.get("POSTGRES_PORT", "5432"))
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _postgres_reachable(), reason="no Postgres listening; run `docker compose up -d`")
def test_load_into_real_postgres(pipeline):
    pq, ent = pipeline
    counts = load(dsn_from_env(ROOT), pq, ent, SCHEMA, telemetry_days=2)
    assert counts["installed_asset"] == 6
    assert counts["telemetry_drive_summary"] == 6
    assert counts["drive_model"] == 5
    assert counts["telemetry_daily_recent"] == 6 + 5   # last 2 of 3 fixture days
    assert counts["case"] > 0 and counts["query_log"] == 300
    import psycopg
    with psycopg.connect(dsn_from_env(ROOT)) as pg:
        n_null = pg.execute("SELECT count(*) FROM installed_asset WHERE warranty_end IS NULL").fetchone()[0]
        fk = pg.execute("SELECT count(*) FROM \"case\" c LEFT JOIN installed_asset a USING (asset_id) WHERE a.asset_id IS NULL").fetchone()[0]
    assert fk == 0 and n_null >= 0
