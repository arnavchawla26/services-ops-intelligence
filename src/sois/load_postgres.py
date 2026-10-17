"""Create the Postgres schema (sql/schema.sql) and load the enterprise tables
plus a telemetry slice.

What goes to Postgres and why:
    drive_model              all rows        small, agents join on it
    telemetry_drive_summary  all rows        one per drive (354K on real data)
    telemetry_daily_recent   last N days     N = --telemetry-days (default 30);
                                             the full quarter stays in Parquet
    every generated table    all rows        from data/enterprise/*.csv

Connection: POSTGRES_HOST/PORT/USER/PASSWORD/DB from the environment, else from
a .env file in the repo root (same keys as .env.example). Re-running drops and
recreates every table, so the load is idempotent.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from pathlib import Path

import duckdb
import psycopg

from sois.load_telemetry import KEPT_COLUMNS

# Parents before children, matching the FKs in sql/schema.sql.
ENTERPRISE_ORDER: tuple[str, ...] = (
    "account", "site", "contact", "product", "order", "installed_asset", "order_line",
    "part", "supplier", "part_supplier", "inventory", "technician", "runbook", "case",
    "dispatch", "dispatch_part", "data_product", "job", "lineage", "query_log",
)


def read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def dsn_from_env(repo_root: Path) -> str:
    env = {**read_dotenv(repo_root / ".env"), **{k: v for k, v in os.environ.items() if k.startswith("POSTGRES_")}}
    host = env.get("POSTGRES_HOST", "localhost")
    port = env.get("POSTGRES_PORT", "5432")
    user = env.get("POSTGRES_USER", "sois")
    pw = env.get("POSTGRES_PASSWORD", "sois_local_dev_only")
    db = env.get("POSTGRES_DB", "sois")
    return f"host={host} port={port} user={user} password={pw} dbname={db}"


def _q(name: str) -> str:
    return f'"{name}"'


def copy_csv_file(cur: psycopg.Cursor, table: str, csv_path: Path) -> int:
    with cur.copy(f"COPY {_q(table)} FROM STDIN WITH (FORMAT csv, HEADER true)") as cp, open(csv_path, "rb") as f:
        while chunk := f.read(1 << 20):
            cp.write(chunk)
    cur.execute(f"SELECT count(*) FROM {_q(table)}")
    return int(cur.fetchone()[0])


def copy_duckdb_query(cur: psycopg.Cursor, con: duckdb.DuckDBPyConnection, table: str, sql: str,
                      batch_rows: int = 200_000) -> int:
    """Stream a DuckDB query into a Postgres table through CSV COPY, in batches."""
    reader = con.execute(sql).fetch_record_batch(batch_rows)
    import pyarrow.csv as pacsv

    with cur.copy(f"COPY {_q(table)} FROM STDIN WITH (FORMAT csv, HEADER false)") as cp:
        for batch in reader:
            buf = io.BytesIO()
            pacsv.write_csv(batch, buf, write_options=pacsv.WriteOptions(include_header=False))
            cp.write(buf.getvalue())
    cur.execute(f"SELECT count(*) FROM {_q(table)}")
    return int(cur.fetchone()[0])


def load(dsn: str, parquet_dir: Path, enterprise_dir: Path, schema_path: Path,
         telemetry_days: int = 30) -> dict[str, int]:
    counts: dict[str, int] = {}
    con = duckdb.connect()
    with psycopg.connect(dsn) as pg:
        with pg.cursor() as cur:
            cur.execute(schema_path.read_text())
            pg.commit()

            # telemetry
            dm = parquet_dir / "drive_model.parquet"
            ds = parquet_dir / "drive_summary.parquet"
            counts["drive_model"] = copy_duckdb_query(
                cur, con, "drive_model",
                f"SELECT model, vendor, capacity_bytes, drive_count, failure_count FROM read_parquet('{dm}')")
            counts["telemetry_drive_summary"] = copy_duckdb_query(
                cur, con, "telemetry_drive_summary",
                f"SELECT serial_number, model, capacity_bytes, datacenter, cluster_id, vault_id, pod_id, "
                f"first_seen, last_seen, days_observed, failed, failure_date, max_power_on_hours "
                f"FROM read_parquet('{ds}')")
            glob = str(parquet_dir / "telemetry_daily" / "*" / "*.parquet")
            cols = ", ".join(_q(c) for c in KEPT_COLUMNS)
            counts["telemetry_daily_recent"] = copy_duckdb_query(
                cur, con, "telemetry_daily_recent",
                f"SELECT {cols} FROM read_parquet('{glob}', hive_partitioning=true) "
                f"WHERE date > (SELECT max(date) FROM read_parquet('{glob}')) - INTERVAL {int(telemetry_days)} DAY "
                f"ORDER BY date, serial_number")
            pg.commit()

            # enterprise
            for table in ENTERPRISE_ORDER:
                path = enterprise_dir / f"{table}.csv"
                if not path.exists():
                    raise FileNotFoundError(path)
                counts[table] = copy_csv_file(cur, table, path)
            pg.commit()
    con.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parquet", type=Path, default=Path("data/parquet"))
    ap.add_argument("--enterprise", type=Path, default=Path("data/enterprise"))
    ap.add_argument("--schema", type=Path, default=Path("sql/schema.sql"))
    ap.add_argument("--telemetry-days", type=int, default=30, help="days of daily telemetry to load (default 30)")
    ap.add_argument("--dsn", default=None, help="override the DSN built from env/.env")
    args = ap.parse_args(argv)

    dsn = args.dsn or dsn_from_env(Path.cwd())
    t0 = time.time()
    counts = load(dsn, args.parquet, args.enterprise, args.schema, args.telemetry_days)
    for k, v in counts.items():
        print(f"{k:24s} {v:>10,}")
    print(f"loaded {len(counts)} tables in {time.time() - t0:.1f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
