# Entry points. Everything runs through uv so the pinned environment is used.
# Order for a fresh machine: download -> load -> generate -> db-up -> postgres -> test -> analyses -> dashboard
# LightGBM on macOS needs Homebrew libomp: brew install libomp
.PHONY: sync test download load generate postgres db-up db-down clean-derived models parts repeat deflection analyses dashboard screenshot

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

models:          ## 30-day failure model, dispatch-budget table and chart, fleet risk files (needs data/parquet)
	$(UV) run python -m sois.failure_model --parquet data/parquet --enterprise data/enterprise --truth truth --reports reports --models models

parts:           ## weekly parts demand backtest (needs data/enterprise)
	$(UV) run python -m sois.parts_forecast --enterprise data/enterprise --reports reports

repeat:          ## repeat-dispatch analysis (needs data/enterprise and truth/)
	$(UV) run python -m sois.repeat_dispatch --enterprise data/enterprise --truth truth --reports reports

deflection:      ## case-deflection candidates: runbook text match scored against truth (needs data/enterprise and truth/)
	$(UV) run python -m sois.case_deflection --enterprise data/enterprise --truth truth --reports reports

analyses: models parts repeat deflection

dashboard:       ## Streamlit over reports/ only (no data needed)
	$(UV) run streamlit run app/dashboard.py

screenshot:      ## headless screenshot of the dashboard into reports/dashboard.png
	$(UV) run python scripts/screenshot_dashboard.py
