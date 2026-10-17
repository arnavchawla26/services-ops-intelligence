# Mess rules

The generated enterprise is deliberately dirty in the ways real CRM, ERP and
support systems are dirty. Every rule below is implemented in
`src/sois/generate_enterprise.py` (search for the rule code), is deterministic
for a given seed, and has ground truth under `truth/` so any answer can be
checked. Nothing is messy by accident; if you find mess not listed here, it is
a bug.

| Code | Rule | Where | Truth |
|---|---|---|---|
| A1 | 20% of companies (at least 2) appear under three spellings: canonical plus two of: `Name, Inc.`, `NAME`, `The Name`, doubled spaces, abbreviated suffix (`Log.`, `Anlytcs`), truncated word. | `account.account_name` | `truth/account_resolution.csv` maps every row to `canonical_account_id` with `variant_type` |
| A2 | Some variant spellings carry their own `account_id` (`duplicate_id`): one company, several ids. | `account.account_id` | same |
| A3 | Some variant spellings reuse the canonical `account_id` (`duplicate_row`): `account_id` is therefore not unique, which is why the table has a surrogate `account_row_id` and Postgres carries no primary key on `account_id`. | `account` | same |
| A4 | Cases reference the messy ids: 70% point at the canonical id, 30% at a duplicate id when one exists. | `case.account_id` | `truth/case_truth.csv.canonical_account_id` |
| D1 | `case.opened_at` and `case.closed_at` come in two formats by source system: `svc_cloud` writes ISO 8601 (`2026-04-02T12:11:06`), `legacy_crm` (30% of cases) writes US `MM/DD/YYYY HH:MM` without seconds. | `case` | `truth/case_truth.csv` has `opened_at_iso`, `closed_at_iso` |
| D2 | `order.order_date` is US format `MM/DD/YYYY`; `installed_asset.install_date` and `warranty_end` are ISO dates. Joining ERP dates to support dates needs parsing. | `order`, `installed_asset` | derivable |
| F1 | "Failure" has two definitions. Telemetry failure = Backblaze `failure = 1` on a drive-day (the drive was pulled). Support failure = `case.failure_reported = 1`, set when the case ended in `resolution_code = hardware_replaced`. They overlap but do not match: some part swaps happen with no telemetry failure (proactive or mis-diagnosed), some telemetry failures never raise a case (about 15% of failed drives), and a telemetry failure can close as `no_fault_found`. | `case.failure_reported` vs `telemetry_daily.failure` | `truth/case_truth.csv.linked_to_telemetry_failure`, `true_hardware_fault` |
| W1 | 10% of `installed_asset.warranty_end` are null. The rest are install date + 3 or 5 years. | `installed_asset` | none needed (null means unknown, by design) |
| S1 | 15% of ERP serials carry a vendor prefix the telemetry serial lacks: `SG-`, `HGST_`, `TOS-`, `WD-`, `HIT-`, `OEM-`. | `installed_asset.serial_number` | `truth/asset_serial_map.csv` (`telemetry_serial`, `erp_serial`, `serial_mess`) |
| S2 | 3% of ERP serials are lowercased. | same | same |
| E1 | 2% of assets are `status = decommissioned` in ERP while still reporting telemetry. ERP lags reality. | `installed_asset.status` | none |
| P1 | `dispatch.parts_used` is a `;`-joined string. The normalized `dispatch_part` table holds the same facts one row per part. | `dispatch`, `dispatch_part` | `dispatch_part` is the clean form |
| R1 | Repeat dispatches: an unresolved first visit (`parts_not_available`, `no_fault_found`, `incomplete`) is followed by another visit 85% of the time; 6% of resolved visits are followed by one anyway. Up to three visits per case. | `dispatch.visit_number` | `truth/dispatch_truth.csv.necessary`, `is_repeat`, `reason` |
| T1 | Technician `skills` is a `;`-joined list. | `technician.skills` | none |

What is clean on purpose: `site`, `product`, `part`, `supplier`, `runbook`,
`inventory`, and the metadata tables. The mess is concentrated where it
matters for entity resolution (Project 2) and for the definition questions a
manager asks (Project 1).
