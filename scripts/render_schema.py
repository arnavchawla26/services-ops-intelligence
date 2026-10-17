"""Render sql/schema.sql. Run with `uv run python scripts/render_schema.py`.

The telemetry_daily_recent column list is generated from the loader's
KEPT_COLUMNS so the two never drift (tests/test_load_postgres.py checks this).
Everything else is static DDL written to the subset of SQL that both Postgres
16 and DuckDB accept, so the file is testable without Docker.
"""

from pathlib import Path

from sois.load_telemetry import KEPT_COLUMNS

PG_TYPE = {"DATE": "DATE", "VARCHAR": "VARCHAR", "BIGINT": "BIGINT", "INTEGER": "INTEGER", "BOOLEAN": "BOOLEAN"}

HEADER = '''-- Services Ops Intelligence: Postgres schema. Rendered by scripts/render_schema.py.
-- Written to the subset of SQL that both Postgres 16 and DuckDB accept, so the
-- DDL is testable without Docker (tests/test_load_postgres.py). No SERIAL, no
-- JSONB, no partial indexes.
--
-- Intentional looseness, see docs/MESS_RULES.md:
--   * account.account_id is NOT unique (rules A2/A3), so there is no FK from
--     case.account_id, site.account_id, order.account_id to account.
--   * case.opened_at / closed_at are VARCHAR because the source mixes two
--     formats (rule D1); the clean timestamps live in truth/.
--   * installed_asset.serial_number does not equal the telemetry serial for
--     about 18% of rows (rules S1/S2); join through truth/asset_serial_map.csv
--     or strip the prefix.
-- "order" and "case" are reserved words and must be quoted; kept on purpose
-- because the 13b schema names them that way and legacy schemas do too.
'''

DROPS = ["query_log", "lineage", "job", "data_product", "dispatch_part", "dispatch", '"case"', "runbook",
         "technician", "inventory", "part_supplier", "supplier", "part", "order_line", "installed_asset",
         '"order"', "product", "contact", "site", "account", "telemetry_daily_recent",
         "telemetry_drive_summary", "drive_model"]

BODY = '''
-- ---------------- telemetry (real, from Backblaze via Parquet) ----------------
CREATE TABLE drive_model (
    model           VARCHAR PRIMARY KEY,
    vendor          VARCHAR NOT NULL,
    capacity_bytes  BIGINT,
    drive_count     BIGINT NOT NULL,
    failure_count   BIGINT NOT NULL
);

CREATE TABLE telemetry_drive_summary (
    serial_number       VARCHAR PRIMARY KEY,
    model               VARCHAR,
    capacity_bytes      BIGINT,
    datacenter          VARCHAR,
    cluster_id          VARCHAR,
    vault_id            VARCHAR,
    pod_id              VARCHAR,
    first_seen          DATE NOT NULL,
    last_seen           DATE NOT NULL,
    days_observed       BIGINT NOT NULL,
    failed              INTEGER NOT NULL,
    failure_date        DATE,
    max_power_on_hours  BIGINT
);

-- Last N days of per-drive daily rows (N set by load_postgres --telemetry-days,
-- default 30). The full quarter stays in Parquet; this is the slice agents query.
CREATE TABLE telemetry_daily_recent (
{tel_cols},
    PRIMARY KEY (serial_number, "date")
);

-- ---------------- CRM (generated) ----------------
CREATE TABLE account (
    account_row_id  INTEGER PRIMARY KEY,
    account_id      VARCHAR NOT NULL,
    account_name    VARCHAR NOT NULL,
    industry        VARCHAR,
    region          VARCHAR,
    source_system   VARCHAR
);

CREATE TABLE site (
    site_id     VARCHAR PRIMARY KEY,
    account_id  VARCHAR NOT NULL,
    site_name   VARCHAR,
    address     VARCHAR,
    city        VARCHAR,
    state       VARCHAR,
    region      VARCHAR
);

CREATE TABLE contact (
    contact_id  VARCHAR PRIMARY KEY,
    account_id  VARCHAR NOT NULL,
    full_name   VARCHAR,
    title       VARCHAR,
    email       VARCHAR,
    phone       VARCHAR
);

-- ---------------- ERP (generated) ----------------
CREATE TABLE product (
    product_id      VARCHAR PRIMARY KEY,
    product_name    VARCHAR NOT NULL,
    family          VARCHAR NOT NULL,
    drive_bays      INTEGER,
    list_price_usd  DOUBLE PRECISION,
    capacity_class  VARCHAR
);

CREATE TABLE "order" (
    order_id    VARCHAR PRIMARY KEY,
    account_id  VARCHAR NOT NULL,
    order_date  VARCHAR NOT NULL,   -- MM/DD/YYYY, rule D2
    status      VARCHAR,
    currency    VARCHAR
);

CREATE TABLE installed_asset (
    asset_id        VARCHAR PRIMARY KEY,
    serial_number   VARCHAR NOT NULL,
    site_id         VARCHAR NOT NULL REFERENCES site(site_id),
    product_id      VARCHAR NOT NULL REFERENCES product(product_id),
    install_date    DATE NOT NULL,
    warranty_end    DATE,               -- null allowed, rule W1
    status          VARCHAR,
    order_id        VARCHAR REFERENCES "order"(order_id)
);

CREATE TABLE order_line (
    order_line_id   VARCHAR PRIMARY KEY,
    order_id        VARCHAR NOT NULL REFERENCES "order"(order_id),
    product_id      VARCHAR NOT NULL REFERENCES product(product_id),
    quantity        INTEGER NOT NULL,
    unit_price_usd  DOUBLE PRECISION
);

-- ---------------- supply chain (generated) ----------------
CREATE TABLE part (
    part_id         VARCHAR PRIMARY KEY,
    part_number     VARCHAR NOT NULL,
    description     VARCHAR,
    category        VARCHAR,
    unit_cost_usd   DOUBLE PRECISION
);

CREATE TABLE supplier (
    supplier_id VARCHAR PRIMARY KEY,
    name        VARCHAR NOT NULL,
    region      VARCHAR
);

CREATE TABLE part_supplier (
    part_id         VARCHAR NOT NULL REFERENCES part(part_id),
    supplier_id     VARCHAR NOT NULL REFERENCES supplier(supplier_id),
    lead_time_days  INTEGER,
    unit_cost_usd   DOUBLE PRECISION,
    preferred       BOOLEAN,
    PRIMARY KEY (part_id, supplier_id)
);

CREATE TABLE inventory (
    depot_id        VARCHAR NOT NULL,
    part_id         VARCHAR NOT NULL REFERENCES part(part_id),
    on_hand         INTEGER NOT NULL,
    reorder_point   INTEGER,
    PRIMARY KEY (depot_id, part_id)
);

-- ---------------- support (generated) ----------------
CREATE TABLE technician (
    technician_id   VARCHAR PRIMARY KEY,
    name            VARCHAR,
    region          VARCHAR,
    skills          VARCHAR,    -- ';'-joined, rule T1
    hire_date       DATE
);

CREATE TABLE runbook (
    runbook_id      VARCHAR PRIMARY KEY,
    title           VARCHAR NOT NULL,
    product_family  VARCHAR,
    category        VARCHAR,
    symptoms        VARCHAR,
    steps           VARCHAR
);

CREATE TABLE "case" (
    case_id          VARCHAR PRIMARY KEY,
    opened_at        VARCHAR NOT NULL,  -- two formats, rule D1
    closed_at        VARCHAR,
    account_id       VARCHAR NOT NULL,  -- no FK: account_id is not unique
    asset_id         VARCHAR NOT NULL REFERENCES installed_asset(asset_id),
    category         VARCHAR NOT NULL,
    severity         INTEGER NOT NULL,
    description      VARCHAR,
    resolution_code  VARCHAR,
    resolution_text  VARCHAR,
    failure_reported INTEGER NOT NULL,  -- support definition of failure, rule F1
    source_system    VARCHAR
);

CREATE TABLE dispatch (
    dispatch_id     VARCHAR PRIMARY KEY,
    case_id         VARCHAR NOT NULL REFERENCES "case"(case_id),
    technician_id   VARCHAR NOT NULL REFERENCES technician(technician_id),
    scheduled_at    TIMESTAMP NOT NULL,
    visit_number    INTEGER NOT NULL,
    outcome         VARCHAR NOT NULL,
    parts_used      VARCHAR             -- ';'-joined, rule P1; see dispatch_part
);

CREATE TABLE dispatch_part (
    dispatch_id VARCHAR NOT NULL REFERENCES dispatch(dispatch_id),
    part_id     VARCHAR NOT NULL REFERENCES part(part_id),
    quantity    INTEGER NOT NULL
);

-- ---------------- metadata for Project 2 (generated) ----------------
CREATE TABLE data_product (
    data_product_id     VARCHAR PRIMARY KEY,
    name                VARCHAR NOT NULL,
    owner               VARCHAR,
    description         VARCHAR,
    freshness_sla_hours INTEGER,
    tables              VARCHAR     -- ';'-joined table names
);

CREATE TABLE job (
    job_id      VARCHAR PRIMARY KEY,
    name        VARCHAR NOT NULL,
    schedule    VARCHAR,
    entrypoint  VARCHAR
);

CREATE TABLE lineage (
    job_id          VARCHAR NOT NULL REFERENCES job(job_id),
    input_table     VARCHAR NOT NULL,
    output_table    VARCHAR NOT NULL
);

CREATE TABLE query_log (
    query_id        VARCHAR PRIMARY KEY,
    ran_at          TIMESTAMP NOT NULL,
    user_name       VARCHAR,
    sql_text        VARCHAR,
    tables          VARCHAR,    -- ';'-joined
    duration_ms     INTEGER,
    rows_returned   INTEGER
);
'''


def render() -> str:
    tel_cols = ",\n".join(f'    "{c}" {PG_TYPE[t]}' for c, t in KEPT_COLUMNS.items())
    drops = "\n".join(f"DROP TABLE IF EXISTS {t} CASCADE;" for t in DROPS)
    return HEADER + "\n" + drops + "\n" + BODY.replace("{tel_cols}", tel_cols)


if __name__ == "__main__":
    out = Path(__file__).resolve().parents[1] / "sql" / "schema.sql"
    out.write_text(render())
    print(f"wrote {out}")
