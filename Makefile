# Entry points. Everything runs through uv so the pinned environment is used.
# Order for a fresh machine: download -> load -> generate -> db-up -> postgres -> test
.PHONY: sync test download load generate postgres db-up db-down clean-derived

UV ?= uv

sync:            ## create .venv from uv.lock
	$(UV) sync

test:            ## run the test suite (fixture-only, no real data needed)
	$(UV) run pytest

download:        ## fetch Backblaze 2026 Q2 (2.1 GB zip, 11.7 GB unzipped). Asks before downloading.
	bash scripts/download_backblaze.sh

load:            ## CSV -> Parquet with DuckDB (needs data/raw/data_Q2_2026/)
	$(UV) run python -m sois.load_telemetry --raw data/raw/data_Q2_2026 --out data/parquet

generate:        ## seeded enterprise generator over the loaded telemetry serials
	$(UV) run python -m sois.generate_enterprise --parquet data/parquet --out data/enterprise --truth truth --seed 20261017

db-up:           ## start Postgres 16 (Docker)
	docker compose up -d

db-down:
	docker compose down

postgres:        ## create schema and load enterprise tables + telemetry summary into Postgres
	$(UV) run python -m sois.load_postgres --parquet data/parquet --enterprise data/enterprise

clean-derived:   ## remove derived data only; never touches data/raw
	rm -rf data/parquet data/enterprise
