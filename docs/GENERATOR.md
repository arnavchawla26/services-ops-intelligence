# How the enterprise is generated

`uv run python -m sois.generate_enterprise --parquet data/parquet --out data/enterprise --truth truth --seed 20261017`

Input: `data/parquet/drive_summary.parquet` and `drive_model.parquet`, written
by `sois.load_telemetry` from the real Backblaze CSVs (one row per drive:
serial, model, capacity, datacenter, vault, first and last day seen, failure
flag, failure date). Nothing else is read. One `numpy.random.default_rng(seed)`
drives every draw, tables are built in the order below, and the same seed on
the same input gives byte-identical CSVs (`tests/test_generate_enterprise.py`
checks this). `truth/generator_manifest.json` records the seed, the SHA-256 of
the input summary, and the SHA-256 and row count of every output file.

Scale rules of thumb (real 2026 Q2 input, about 354K drives, 91 days): about
236 accounts, 700 sites, 354K assets, 7K to 9K cases, 3K to 4K dispatches.
Exact counts are in the manifest after a run; nothing below quotes a number
that was not computed.

## Tables, in build order

**account** (CRM). `n = clip(round(drives / 1500), 12, 300)` companies, names
drawn without replacement from 40 prefixes x 12 suffixes plus a legal form, an
industry, and a home region (weighted AMER-West 30%, AMER-East 28%, EMEA 20%,
APJ 15%, LATAM 7%). Then the spelling and id mess (MESS_RULES A1 to A3).
`account_row_id` is the only unique key.

**site** (CRM). 1 to 5 per company; the first site is in the home region, the
rest stay there with probability 0.6. City/state from a per-region list, a
random street number and street name.

**contact** (CRM). 1 to 3 per company; name from two 20-name lists, title from
a 6-title list, email at `<company>.example`, a `+1-555` phone.

**product** (ERP). Ten fixed generic SKUs in five families (Rack Server,
Storage Array, HCI Node, Backup Appliance, Edge Gateway), each with a capacity
class. None is a real product.

**installed_asset** (ERP). Exactly one asset per telemetry drive; the drive
serial is the asset serial (with mess S1, S2). Drives that share a Backblaze
`(datacenter, vault_id)` are placed at the same customer site (a vault is a
physical group, so failures cluster by site the way they cluster by vault).
Product is drawn from the SKUs whose capacity class matches the drive
(< 10 TB small, 10 to 16 TB mid, above large). `install_date` = first day seen
in telemetry minus 30 to 1500 days. `warranty_end` = install + 3y (65%) or 5y
(35%), null 10% (W1). `status` active except 2% decommissioned (E1).

**order, order_line** (ERP). One order per (account, install month) covering
the assets installed that month; `order_date` is 5 to 40 days before the first
install, in US format (D2). Lines aggregate assets by product with a unit
price of 78% to 98% of list.

**part, supplier, part_supplier, inventory** (supply chain). 30 fixed parts
(drive sleds by capacity, PSUs, fans, DIMMs, controllers, backplanes, NICs,
cables, boards, chassis) and 8 suppliers. Each part has 1 to 3 suppliers with
lead time 3 to 45 days, cost 60% to 90% of the part's unit cost, the first
marked preferred. Inventory is Poisson on-hand per (depot, part) with a mean
per category and a reorder point of half that mean; six depots (one per region
plus Central).

**technician** (support). `clip(round(drives / 4000), 8, 80)` technicians, the
first five one per region so every region has one, 1 to 3 skills from
{storage, server, network, firmware, hci}, hire dates 2018 to 2025.

**runbook** (support). 22 fixed runbooks with product family, category, a
`;`-separated symptoms field and numbered steps.

**case** (support). Two sources, then sorted by open time so case ids are
chronological:

1. Telemetry-driven. For every drive with `failure = 1` on some day: with
   probability 0.85 a `drive_failure` case opens `Exp(mean 1.2 days)` after the
   failure date (severity 1 or 2); independently, with probability 0.25 an
   early-warning case (`predictive_alert` 70%, `performance` 30%, severity 2 or
   3) opens 1 to 21 days before. Both are `linked_to_telemetry_failure = true`
   and `true_hardware_fault = true` in truth. About 15% of failed drives
   therefore have no case at all.
2. Background. `max(8, Poisson(0.0002 x drives x days))` cases spread uniformly
   over assets and the window, categories how_to 22%, config 18%, firmware 12%,
   connectivity 15%, performance 15%, false_alarm 8%, drive_failure 10%.
   Background `drive_failure` cases are true hardware faults 70% of the time
   (the support system sees a fault the telemetry flag never recorded);
   connectivity and performance 15%; the rest never.

For each case: the account id is chosen among the company's ids (A4); a
runbook is matched by (category, family) 80% of the time; deflectability truth
is 0.85 for how_to/config/firmware at severity 3 or 4 with a matched runbook,
0.6 for false_alarm, 0.03 otherwise. Deflectable cases copy one symptom phrase
from their runbook into the description, so text matching has signal that is
real but not trivial (the phrase is one of several in the runbook's symptoms
field, surrounded by templated text). Resolution code depends on the truth:
hardware faults close as `hardware_replaced` 90% (else `no_fault_found`); other
categories draw from their own small distributions. 5% of cases are still open
(`closed_at` null, `resolution_code = pending`). Duration is Gamma(2) with a
severity-dependent scale (10, 24, 60, 120 hours). 30% of cases come from
`legacy_crm` and get the US timestamp format (D1).

**dispatch, dispatch_part** (support). Dispatch probability by category:
drive_failure 0.92, predictive_alert 0.55, performance 0.35, connectivity 0.30,
false_alarm 0.25, firmware 0.15, config 0.12, how_to 0.05. The technician comes
from the site's region. First visit is scheduled Gamma(2) with mean 1.5 days
(severity 1 to 2) or 3 days after the case opens; outcome resolved 72%,
parts_not_available 10%, no_fault_found 10%, incomplete 8% (a case without a
real fault cannot be parts_not_available). Repeats per rule R1. Hardware-fault
visits that resolve or run incomplete consume the drive sled matching the
drive's capacity (plus a backplane 8% of the time); 25% of other resolved
visits consume one random non-drive part. Truth: a first visit is `necessary`
when the case has a true hardware fault and is not deflectable; a repeat is
necessary only after `parts_not_available` or `incomplete` on a real fault.

**data_product, job, lineage, query_log** (metadata for Project 2). Three data
products, four jobs, ten lineage edges (all fixed), and 300 query-log rows
drawn from ten SQL templates with random user, time, duration and row count.

## What this generator does not do

- It does not model customer size: every vault is equally likely to land at
  any site, so asset counts per account are roughly uniform.
- Case text is templated, not written; it has no typos, slang, or attachments.
- Parts demand is driven only by dispatches; there is no seasonality beyond
  what the real failure dates carry.
- Nothing is backdated: generated timestamps are in the telemetry window
  (2026 Q2) because that is when the drives were observed.
