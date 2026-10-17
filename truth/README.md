# truth/

Ground truth for the generated enterprise. The CSVs here are rebuilt by
`make generate` from the seed in `generator_manifest.json` and are gitignored
because they are large and fully reproducible; the manifest (committed)
carries the SHA-256 of every generated file so a rebuild can be verified.

| File | One row per | Answers |
|---|---|---|
| `account_resolution.csv` | account row | which canonical company is behind every spelling and id |
| `asset_serial_map.csv` | installed asset | the telemetry serial behind every ERP serial, and what mess was applied |
| `case_truth.csv` | case | clean ISO timestamps, canonical account, whether the case is tied to a telemetry failure, whether a hardware fault truly existed, whether it was deflectable and by which runbook |
| `dispatch_truth.csv` | dispatch | whether the truck roll was necessary, why, and whether it was a repeat |
| `generator_manifest.json` | run | seed, input hash, row counts and hashes |

Rules that produced the mess: `docs/MESS_RULES.md`. How each table is built:
`docs/GENERATOR.md`.
