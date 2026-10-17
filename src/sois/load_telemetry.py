"""Load Backblaze daily SMART CSVs into partitioned Parquet with DuckDB.

Input layout (Backblaze Hard Drive Data, 2026 Q2 schema, see docs in README):
    date, serial_number, model, capacity_bytes, failure, datacenter, cluster_id,
    vault_id, pod_id, pod_slot_num, is_legacy_format, then smart_N_normalized /
    smart_N_raw pairs. The set of SMART columns varies across days and quarters,
    so every file is inspected and missing kept-columns are filled with NULL.

Outputs under --out (default data/parquet):
    telemetry_daily/day=YYYY-MM-DD/data.parquet   one file per daily CSV
    drive_summary.parquet                          one row per serial
    drive_model.parquet                            one row per model
    load_summary.json                              counts printed at the end

Why per-file instead of one read_csv over a glob: a glob with union_by_name
sniffs every file and errors on type disagreements across days; one file at a
time lets us pin the types of the columns we keep and keeps memory flat.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import duckdb

# SMART attributes kept. Each gives a _normalized and a _raw column.
# 5, 187, 188, 197, 198: the five Backblaze found most predictive of failure.
# 9 power-on hours, 194 temperature, 199 UDMA CRC errors, 241/242 LBAs
# written/read, 1/7 read/seek error rate, 3 spin-up time, 4/12 start-stop and
# power cycles, 10 spin retry, 190 airflow temp, 192 power-off retracts,
# 193 load cycles, 196 reallocation events, 240 head flying hours.
SMART_IDS: tuple[int, ...] = (
    1, 3, 4, 5, 7, 9, 10, 12, 187, 188, 190, 192, 193, 194, 196, 197, 198, 199,
    240, 241, 242,
)

BASE_COLUMNS: dict[str, str] = {
    "date": "DATE",
    "serial_number": "VARCHAR",
    "model": "VARCHAR",
    "capacity_bytes": "BIGINT",
    "failure": "INTEGER",
    "datacenter": "VARCHAR",
    "cluster_id": "VARCHAR",
    "vault_id": "VARCHAR",
    "pod_id": "VARCHAR",
    "pod_slot_num": "INTEGER",
    "is_legacy_format": "BOOLEAN",
}


def smart_columns() -> dict[str, str]:
    cols: dict[str, str] = {}
    for n in SMART_IDS:
        cols[f"smart_{n}_normalized"] = "BIGINT"
        cols[f"smart_{n}_raw"] = "BIGINT"
    return cols


KEPT_COLUMNS: dict[str, str] = {**BASE_COLUMNS, **smart_columns()}


# Vendor from the model string. Backblaze model strings look like
# "ST12000NM0008" (Seagate), "HGST HUH721212ALE600", "TOSHIBA MG07ACA14TA",
# "WDC WUH721816ALE6L4", "Hitachi HDS5C4040ALE630", "Seagate BarraCuda...",
# "Micron_5200_MTFD..." (SSD), "DELLBOSS VD", "SAMSUNG ...". Anything else -> Unknown.
VENDOR_RULES: tuple[tuple[str, str], ...] = (
    (r"^ST\d", "Seagate"),
    (r"^SEAGATE", "Seagate"),
    (r"^HGST", "HGST"),
    (r"^HITACHI", "Hitachi"),
    (r"^TOSHIBA", "Toshiba"),
    (r"^WDC", "Western Digital"),
    (r"^WD\b|^WD ", "Western Digital"),
    (r"^MICRON", "Micron"),
    (r"^SAMSUNG", "Samsung"),
    (r"^CT\d", "Crucial"),
    (r"^DELLBOSS", "Dell"),
    (r"^SSDSC", "Intel"),
)


def vendor_from_model(model: str | None) -> str:
    if not model:
        return "Unknown"
    m = model.strip().upper()
    for pattern, vendor in VENDOR_RULES:
        if re.search(pattern, m):
            return vendor
    return "Unknown"


def _csv_columns(con: duckdb.DuckDBPyConnection, csv_path: Path) -> list[str]:
    rows = con.execute(
        "DESCRIBE SELECT * FROM read_csv(?, header=true, sample_size=1024)",
        [str(csv_path)],
    ).fetchall()
    return [r[0] for r in rows]


def _select_list(present: set[str]) -> str:
    parts = []
    for col, typ in KEPT_COLUMNS.items():
        if col in present:
            parts.append(f'TRY_CAST("{col}" AS {typ}) AS "{col}"')
        else:
            parts.append(f'NULL::{typ} AS "{col}"')
    return ",\n        ".join(parts)


def _day_from_name(csv_path: Path) -> str | None:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", csv_path.name)
    return m.group(1) if m else None


def load_daily_csvs(raw_dir: Path, out_dir: Path, threads: int | None = None) -> dict:
    """Convert every *.csv in raw_dir into telemetry_daily/day=.../data.parquet."""
    csvs = sorted(raw_dir.glob("*.csv"))
    if not csvs:
        raise FileNotFoundError(f"no CSV files in {raw_dir}")

    daily_dir = out_dir / "telemetry_daily"
    daily_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute("SET preserve_insertion_order = false")
    if threads:
        con.execute(f"SET threads = {int(threads)}")

    missing_report: dict[str, list[str]] = {}
    n_files = 0
    for csv_path in csvs:
        day = _day_from_name(csv_path)
        if day is None:
            print(f"skip (no date in name): {csv_path.name}", file=sys.stderr)
            continue
        present = set(_csv_columns(con, csv_path))
        missing = [c for c in KEPT_COLUMNS if c not in present]
        if missing:
            missing_report[csv_path.name] = missing
        # Only pass types for columns that exist in this file.
        types = {c: KEPT_COLUMNS[c] for c in KEPT_COLUMNS if c in present and c != "date"}
        part_dir = daily_dir / f"day={day}"
        part_dir.mkdir(parents=True, exist_ok=True)
        target = part_dir / "data.parquet"
        # read_csv named arguments cannot be bound parameters, so the struct of
        # types is inlined. Names and types come from KEPT_COLUMNS (our own
        # constants); the file path is quoted with single quotes doubled.
        types_literal = "{" + ", ".join(f"'{c}': '{t}'" for c, t in types.items()) + "}"
        src = str(csv_path).replace("'", "''")
        dst = str(target).replace("'", "''")
        con.execute(
            f"""
            COPY (
              SELECT
                {_select_list(present)}
              FROM read_csv('{src}', header=true, union_by_name=false, types={types_literal})
              WHERE serial_number IS NOT NULL
            ) TO '{dst}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
        n_files += 1
        print(f"wrote {target.relative_to(out_dir)}")

    con.close()
    return {"files": n_files, "missing_columns": missing_report}


def build_summaries(out_dir: Path) -> dict:
    """drive_summary.parquet, drive_model.parquet and load_summary.json from telemetry_daily."""
    glob = str(out_dir / "telemetry_daily" / "*" / "*.parquet")
    con = duckdb.connect()
    con.execute(
        f"CREATE VIEW t AS SELECT * EXCLUDE (day) FROM read_parquet('{glob}', hive_partitioning=true)"
    )

    # Per-drive summary. failure_date is the (last) day the failure flag was 1.
    con.execute(
        """
        CREATE TABLE drive_summary AS
        SELECT
          serial_number,
          arg_max(model, date)          AS model,
          max(capacity_bytes)           AS capacity_bytes,
          arg_max(datacenter, date)     AS datacenter,
          arg_max(cluster_id, date)     AS cluster_id,
          arg_max(vault_id, date)       AS vault_id,
          arg_max(pod_id, date)         AS pod_id,
          min(date)                     AS first_seen,
          max(date)                     AS last_seen,
          count(*)                      AS days_observed,
          max(failure)                  AS failed,
          max(CASE WHEN failure = 1 THEN date END) AS failure_date,
          max(smart_9_raw)              AS max_power_on_hours
        FROM t
        GROUP BY serial_number
        """
    )
    con.execute(
        f"COPY drive_summary TO '{out_dir / 'drive_summary.parquet'}' (FORMAT PARQUET)"
    )

    con.create_function("vendor_from_model", vendor_from_model, [str], str, null_handling="special")
    con.execute(
        """
        CREATE TABLE drive_model AS
        SELECT
          model,
          vendor_from_model(model)   AS vendor,
          max(capacity_bytes)        AS capacity_bytes,
          count(*)                   AS drive_count,
          sum(failed)                AS failure_count
        FROM drive_summary
        GROUP BY model
        ORDER BY drive_count DESC
        """
    )
    con.execute(f"COPY drive_model TO '{out_dir / 'drive_model.parquet'}' (FORMAT PARQUET)")

    rows, days, first, last = con.execute(
        "SELECT count(*), count(DISTINCT date), min(date), max(date) FROM t"
    ).fetchone()
    drives, failures, failed_drives = con.execute(
        "SELECT count(*), sum(failed), count(*) FILTER (WHERE failed = 1) FROM drive_summary"
    ).fetchone()
    n_models = con.execute("SELECT count(*) FROM drive_model").fetchone()[0]
    con.close()

    summary = {
        "rows": int(rows),
        "days": int(days),
        "first_date": str(first),
        "last_date": str(last),
        "drives": int(drives),
        "failures": int(failures or 0),
        "failed_drives": int(failed_drives),
        "models": int(n_models),
    }
    (out_dir / "load_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", type=Path, default=Path("data/raw/data_Q2_2026"), help="directory of daily CSVs")
    ap.add_argument("--out", type=Path, default=Path("data/parquet"), help="parquet output directory")
    ap.add_argument("--threads", type=int, default=None, help="DuckDB threads (default: all)")
    args = ap.parse_args(argv)

    info = load_daily_csvs(args.raw, args.out, args.threads)
    if info["missing_columns"]:
        print("columns filled with NULL because absent in some files:")
        for name, cols in info["missing_columns"].items():
            print(f"  {name}: {', '.join(cols)}")
    summary = build_summaries(args.out)
    print(json.dumps(summary, indent=2))
    print(
        f"rows={summary['rows']:,} drives={summary['drives']:,} "
        f"failures={summary['failures']:,} days={summary['days']} models={summary['models']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
