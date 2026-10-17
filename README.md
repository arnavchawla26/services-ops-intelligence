# services-ops-intelligence

Project 1 of 3: predictive support and parts forecasting for an installed
base of hardware, built the way a Services Operations team would use it.

## Why this exists

On October 6, 2026 Dell announced the next release of the Dell AI Data
Platform, and Arthur Lewis (President, Infrastructure Solutions Group) put the
problem in one line: "In many cases, the model is not the next constraint, the
data is." The platform's answer is a data layer that engines and agents share,
an enterprise knowledge graph built from metadata, lineage and query history,
and knowledge agents that get trusted, permissioned context instead of raw
tables. This repository is the data-science end of a three-part portfolio
built on that framing: one small enterprise whose systems disagree the way
real ones do, real hardware telemetry underneath it, and models whose output
is an operations decision rather than an accuracy score.

The three repositories:

1. `services-ops-intelligence` (this one): the shared data foundation and the
   Services Operations analyses.
2. `enterprise-knowledge-graph` (planned): ontology, entity resolution,
   lineage graph and text-to-Cypher over the same enterprise.
3. `knowledge-agents-pilot` (planned): governed agents on NVIDIA Auto
   Ontology, Elasticsearch and MCP, with evaluations and program artifacts.

## What is real and what is generated

| Data | Source | Status |
|---|---|---|
| `telemetry_daily` (one row per drive per day: SMART attributes, failure flag, datacenter, vault, pod) | Backblaze Hard Drive Data, 2026 Q2, loaded as-is | Real |
| `drive_model`, `telemetry_drive_summary` | Derived from the telemetry by `sois.load_telemetry` | Real (derived) |
| `account`, `site`, `contact` (CRM) | `sois.generate_enterprise`, seeded | Generated |
| `product`, `order`, `order_line`, `installed_asset` (ERP) | same; `installed_asset.serial_number` is the real drive serial, so every asset is a real drive with real telemetry | Generated, joined to real |
| `part`, `supplier`, `part_supplier`, `inventory` (supply chain) | same | Generated |
| `case`, `dispatch`, `dispatch_part`, `technician`, `runbook` (support) | same; case timing is tied to real failure dates (cases cluster after a drive's failure flag, some open before it) | Generated, timed by real failures |
| `data_product`, `job`, `lineage`, `query_log` (metadata) | same | Generated |
| `truth/` | ground truth behind every piece of generated mess | Generated |

The generated data is deliberately dirty in documented ways (three spellings
of the same customer, duplicate ids, two date formats, two definitions of
"failure", null warranties, vendor-prefixed serials). Every rule is listed in
[docs/MESS_RULES.md](docs/MESS_RULES.md), every table's construction in
[docs/GENERATOR.md](docs/GENERATOR.md), and every rule has ground truth under
`truth/` so any answer can be checked. The generator is seeded; the same seed
gives byte-identical output, and `truth/generator_manifest.json` carries the
hashes.

Why generated data is acceptable here: a hiring manager cares that the problem
shape matches theirs, and known ground truth is what makes honest evaluation
possible. The telemetry, which is what the failure model learns from, is real.

## Planned analyses

None of these exist yet. Each will land in its own commit with its own numbers.

- 30-day failure model on the telemetry (time-based split, features only from
  days before the prediction date, calibrated probabilities), reported as a
  dispatch-budget decision: at a given number of truck rolls, how many failures
  are caught and how many trucks roll for nothing.
- Parts demand forecast by region and part, backtested month by month against
  a naive "same as last month" baseline.
- Repeat-dispatch analysis: which products, case categories and regions drive
  second visits, checked against `truth/dispatch_truth.csv`.
- Case deflection candidates: cases whose text matches a runbook closely
  enough that a human was not needed, scored against `truth/case_truth.csv`.
- A Streamlit dashboard for operations leads.
- A two-page findings memo per analysis.
- One Google Colab notebook running the feature pipeline with NVIDIA cuDF
  (`cudf.pandas`) and reporting measured timings against pandas.

## What exists today

- `scripts/download_backblaze.sh`: fetch, verify and extract the 2026 Q2 data.
- `src/sois/load_telemetry.py`: daily CSVs to partitioned Parquet with DuckDB,
  plus per-drive and per-model summaries. Handles SMART columns that come and
  go between files.
- `src/sois/generate_enterprise.py`: the seeded enterprise generator and its
  ground truth.
- `docker-compose.yml`, `sql/schema.sql`, `src/sois/load_postgres.py`:
  Postgres 16 with the generated tables, the drive summary, and the last 30
  days of daily telemetry (the full quarter stays in Parquet).
- `tests/`: 27 tests on a hand-made synthetic fixture in the Backblaze column
  layout (`tests/fixtures/`), covering loader counts and schema drift,
  generator determinism (same seed, same bytes), referential integrity, every
  mess rule, the causal timing of cases around failures, the DDL in DuckDB,
  and a live Postgres load when a database is listening.

## Setup

Requirements: Python 3.13, [uv](https://docs.astral.sh/uv/), Docker (for
Postgres), about 15 GB of disk for the raw data.

```
uv sync                                  # environment from uv.lock (pinned)
uv run pytest                            # fixture-only, no data needed
bash scripts/download_backblaze.sh       # asks before fetching 2.1 GB
make load                                # CSV -> data/parquet (DuckDB)
make generate                            # enterprise tables -> data/enterprise, truth/
cp .env.example .env && make db-up       # Postgres 16 in Docker
make postgres                            # schema + load
```

`make load` on the full quarter reads about 11.7 GB of CSV; expect minutes,
not seconds. `make postgres` copies roughly 30 days x all drives of daily rows
into Postgres (`--telemetry-days` to change).

## The telemetry

Backblaze publishes daily SMART snapshots for every drive in its data centers.
Per Backblaze's own page, 2026 Q2 is 2.1 GB zipped, 11.7 GB on disk, 354,415
drives, 1,498 failures and 31.5 million drive-days. Those are their numbers;
`make load` prints what it actually finds in `data/parquet/load_summary.json`,
and this README will be updated with the counted figures once the load has
run. Columns: `date, serial_number, model, capacity_bytes, failure,
datacenter, cluster_id, vault_id, pod_id, pod_slot_num, is_legacy_format`,
then `smart_N_normalized` / `smart_N_raw` pairs. The loader keeps attributes
1, 3, 4, 5, 7, 9, 10, 12, 187, 188, 190, 192, 193, 194, 196, 197, 198, 199,
240, 241, 242 (the five Backblaze found most predictive are 5, 187, 188, 197,
198). `failure = 1` marks the last day a drive reported before it was pulled.

Data terms: the Backblaze Hard Drive Data is free to download and use. Their
terms ask that you cite Backblaze as the source, allow you to sell derivative
work (this code and its results), and do not allow selling the data itself.
Source: https://www.backblaze.com/cloud-storage/resources/hard-drive-test-data

## Layout

```
scripts/      download_backblaze.sh, render_schema.py
src/sois/     load_telemetry.py, generate_enterprise.py, load_postgres.py
sql/          schema.sql (rendered; Postgres 16 and DuckDB compatible)
docs/         MESS_RULES.md, GENERATOR.md
truth/        ground truth (regenerated by `make generate`; manifest committed)
tests/        pytest suite and the synthetic fixture
data/         raw/ (downloads, untrusted), parquet/, enterprise/  (all gitignored)
```

## Honesty

Commit dates are real; nothing is backdated. The generated data is labelled
as generated everywhere it appears, its generator is in this repository, and
its ground truth is reproducible from the committed seed. No analysis result
is claimed in this README until the code that produced it is in the
repository with the numbers it printed.

## License

MIT, see [LICENSE](LICENSE). The Backblaze data is subject to Backblaze's
terms, not this license.
