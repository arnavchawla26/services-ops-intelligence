"""Seeded generator for the enterprise that sits on top of the drive telemetry.

Reads the per-drive summary produced by load_telemetry (serial, model,
datacenter, vault, first/last seen, failure flag and date) and writes CSV plus
Parquet for CRM, ERP, support and supply-chain tables, with intentional,
documented mess (docs/MESS_RULES.md) and ground truth under truth/
(docs/GENERATOR.md explains every table).

Everything is driven by one numpy Generator seeded from --seed, and tables are
built in a fixed order, so the same seed and the same input give byte-identical
CSVs. Nothing in here calls a model or the network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from sois.load_telemetry import vendor_from_model

DAY = np.timedelta64(1, "D")
SEC = np.timedelta64(1, "s")

REGIONS = ["AMER-West", "AMER-East", "EMEA", "APJ", "LATAM"]
REGION_WEIGHTS = [0.30, 0.28, 0.20, 0.15, 0.07]
DEPOTS = {r: f"DEPOT-{r}" for r in REGIONS} | {"Central": "DEPOT-Central"}

CITIES = {
    "AMER-West": [("San Jose", "CA"), ("Seattle", "WA"), ("Denver", "CO"), ("Phoenix", "AZ")],
    "AMER-East": [("Austin", "TX"), ("Boston", "MA"), ("Atlanta", "GA"), ("Raleigh", "NC")],
    "EMEA": [("Amsterdam", "NL"), ("Frankfurt", "DE"), ("Dublin", "IE"), ("Madrid", "ES")],
    "APJ": [("Singapore", "SG"), ("Sydney", "AU"), ("Tokyo", "JP"), ("Bangalore", "IN")],
    "LATAM": [("Sao Paulo", "BR"), ("Mexico City", "MX"), ("Santiago", "CL"), ("Bogota", "CO")],
}
STREETS = ["Market St", "Industrial Pkwy", "Innovation Dr", "Harbor Blvd", "Commerce Way",
           "Technology Ln", "Enterprise Ave", "Logistics Rd", "Research Blvd", "Gateway Ct"]

NAME_PREFIX = ["Northwind", "Ridgeline", "Harborview", "Bluestone", "Cedarbrook", "Ironvale",
               "Summitpoint", "Lakeshore", "Redwood", "Silverline", "Oakridge", "Brightwater",
               "Granite", "Meridian", "Pinecrest", "Stonefield", "Westgate", "Clearview",
               "Highland", "Copperfield", "Evergreen", "Fairmont", "Goldcrest", "Kingsbridge",
               "Lighthouse", "Maplewood", "Northgate", "Riverbend", "Sandstone", "Thornhill",
               "Valleyforge", "Whitehall", "Ashford", "Beacon", "Crestline", "Dunmore",
               "Eastfield", "Foxglove", "Greystone", "Hollowbrook"]
NAME_SUFFIX = ["Logistics", "Health", "Capital", "Energy", "Analytics", "Foods", "Mutual",
               "Systems", "Media", "Pharma", "Retail", "Aerospace"]
LEGAL = ["Inc", "LLC", "Corp", "Ltd", "GmbH", "plc", "Holdings"]
INDUSTRIES = ["Healthcare", "Financial Services", "Manufacturing", "Retail", "Public Sector",
              "Media", "Energy", "Technology", "Education", "Transportation"]

FIRST_NAMES = ["Alex", "Priya", "Jordan", "Wei", "Maria", "Samir", "Elena", "Tomas", "Aisha",
               "Noah", "Hana", "Luis", "Fatima", "Ravi", "Sofia", "Kenji", "Nadia", "Omar",
               "Grace", "Mateo"]
LAST_NAMES = ["Patel", "Nguyen", "Garcia", "Okafor", "Schmidt", "Rossi", "Kim", "Silva",
              "Andersson", "Haddad", "Mori", "Novak", "Dubois", "Mensah", "Ivanova", "Reyes",
              "Chen", "Olsen", "Kaur", "Brennan"]
TITLES = ["IT Director", "Storage Admin", "Infrastructure Manager", "Procurement Lead",
          "Data Center Operations", "CIO"]

# Products: generic server/storage SKUs. The product an asset maps to is picked
# from its drive capacity class (see GENERATOR.md); nothing here is a real SKU.
PRODUCTS = [
    # product_id, name, family, drive_bays, list_price_usd, capacity_class
    ("PRD-RS110", "RS-110 Rack Server", "Rack Server", 8, 9800, "small"),
    ("PRD-RS120", "RS-120 Rack Server", "Rack Server", 12, 12400, "small"),
    ("PRD-RS240", "RS-240 Rack Server", "Rack Server", 24, 18900, "mid"),
    ("PRD-SA300", "SA-300 Storage Array", "Storage Array", 24, 31000, "mid"),
    ("PRD-SA500", "SA-500 Storage Array", "Storage Array", 60, 74000, "large"),
    ("PRD-SA700", "SA-700 Storage Array", "Storage Array", 90, 118000, "large"),
    ("PRD-HC200", "HC-200 HCI Node", "HCI Node", 12, 27500, "mid"),
    ("PRD-HC400", "HC-400 HCI Node", "HCI Node", 24, 41000, "large"),
    ("PRD-BA150", "BA-150 Backup Appliance", "Backup Appliance", 36, 52000, "large"),
    ("PRD-EG050", "EG-050 Edge Gateway", "Edge Gateway", 4, 5600, "small"),
]
FAMILIES = sorted({p[2] for p in PRODUCTS})

PARTS = [
    # part_id, part_number, description, category, unit_cost_usd, capacity_tb (sleds only)
    ("PRT-0001", "HDD-SLED-04T", "4TB SATA HDD in sled", "drive", 95, 4),
    ("PRT-0002", "HDD-SLED-08T", "8TB SATA HDD in sled", "drive", 150, 8),
    ("PRT-0003", "HDD-SLED-12T", "12TB SATA HDD in sled", "drive", 210, 12),
    ("PRT-0004", "HDD-SLED-14T", "14TB SATA HDD in sled", "drive", 240, 14),
    ("PRT-0005", "HDD-SLED-16T", "16TB SATA HDD in sled", "drive", 270, 16),
    ("PRT-0006", "HDD-SLED-18T", "18TB SATA HDD in sled", "drive", 310, 18),
    ("PRT-0007", "HDD-SLED-20T", "20TB SATA HDD in sled", "drive", 360, 20),
    ("PRT-0008", "HDD-SLED-22T", "22TB SATA HDD in sled", "drive", 410, 22),
    ("PRT-0009", "HDD-SLED-24T", "24TB SATA HDD in sled", "drive", 460, 24),
    ("PRT-0010", "SSD-SLED-02T", "1.9TB SATA SSD in sled", "drive", 380, 2),
    ("PRT-0011", "PSU-1100W", "1100W hot-plug power supply", "power", 220, None),
    ("PRT-0012", "PSU-2000W", "2000W hot-plug power supply", "power", 340, None),
    ("PRT-0013", "FAN-60MM", "60mm hot-plug fan module", "cooling", 45, None),
    ("PRT-0014", "FAN-80MM", "80mm hot-plug fan module", "cooling", 55, None),
    ("PRT-0015", "DIMM-32G", "32GB DDR5 RDIMM", "memory", 160, None),
    ("PRT-0016", "DIMM-64G", "64GB DDR5 RDIMM", "memory", 300, None),
    ("PRT-0017", "RAID-CTRL", "SAS RAID controller", "controller", 520, None),
    ("PRT-0018", "HBA-12G", "12Gb SAS HBA", "controller", 310, None),
    ("PRT-0019", "BACKPLANE-24", "24-bay SAS backplane", "backplane", 410, None),
    ("PRT-0020", "BACKPLANE-60", "60-bay SAS backplane", "backplane", 890, None),
    ("PRT-0021", "NIC-25G", "25GbE dual-port NIC", "network", 290, None),
    ("PRT-0022", "NIC-100G", "100GbE dual-port NIC", "network", 780, None),
    ("PRT-0023", "CABLE-SAS", "SAS 12G cable 1m", "cable", 28, None),
    ("PRT-0024", "CABLE-DAC", "25G DAC cable 3m", "cable", 42, None),
    ("PRT-0025", "BBU-CTRL", "RAID controller battery", "controller", 95, None),
    ("PRT-0026", "SYSBOARD-RS", "Rack server system board", "board", 1450, None),
    ("PRT-0027", "SYSBOARD-SA", "Storage array controller board", "board", 2900, None),
    ("PRT-0028", "BEZEL-STD", "Front bezel", "chassis", 35, None),
    ("PRT-0029", "RAIL-KIT", "Sliding rail kit", "chassis", 70, None),
    ("PRT-0030", "TPM-MOD", "TPM 2.0 module", "board", 38, None),
]
PART_CATEGORY_STOCK = {"drive": 40, "power": 12, "cooling": 15, "memory": 20, "controller": 8,
                       "backplane": 4, "network": 10, "cable": 30, "board": 3, "chassis": 10}

SUPPLIERS = [
    ("SUP-01", "Pacific Storage Components", "APJ"),
    ("SUP-02", "Atlantic Power Supplies", "AMER-East"),
    ("SUP-03", "Rhine Electronics", "EMEA"),
    ("SUP-04", "Sierra Memory Modules", "AMER-West"),
    ("SUP-05", "Delta Interconnect", "AMER-East"),
    ("SUP-06", "Andes Logistics Parts", "LATAM"),
    ("SUP-07", "Kanto Precision", "APJ"),
    ("SUP-08", "Nordic Board Works", "EMEA"),
]

SKILLS = ["storage", "server", "network", "firmware", "hci"]

CASE_CATEGORIES = ["how_to", "config", "firmware", "connectivity", "performance",
                   "false_alarm", "drive_failure"]
BACKGROUND_WEIGHTS = [0.22, 0.18, 0.12, 0.15, 0.15, 0.08, 0.10]
DISPATCH_PROB = {"drive_failure": 0.92, "predictive_alert": 0.55, "performance": 0.35,
                 "connectivity": 0.30, "firmware": 0.15, "config": 0.12, "how_to": 0.05,
                 "false_alarm": 0.25}

SYMPTOMS = {
    "how_to": ["how do I expand a volume", "need steps to add a spare drive",
               "where is the alert log in the management console",
               "how to export the health report", "how to set up SNMP alerts"],
    "config": ["RAID group shows degraded after config change", "wrong IP on management port",
               "LUN not visible to host after mapping", "NTP drift on controller",
               "replication schedule not running"],
    "firmware": ["drive firmware mismatch warning", "controller firmware update failed at 60%",
                 "BIOS reports unsupported firmware on backplane", "firmware rollback requested"],
    "connectivity": ["host loses path intermittently", "management port unreachable",
                     "SAS link resets in log", "NIC link flapping on port 2"],
    "performance": ["latency spikes on volume during backup window",
                    "throughput dropped 40 percent since last week",
                    "slow rebuild on disk group", "high IO wait on drive bay"],
    "false_alarm": ["predictive failure alert cleared by itself",
                    "temperature alert during HVAC maintenance",
                    "amber light but no fault in logs"],
    "drive_failure": ["drive in bay reports failed", "SMART reallocated sector count climbing",
                      "drive offline, rebuild started", "uncorrectable errors on drive",
                      "drive not detected after reboot"],
    "predictive_alert": ["SMART pending sector count rising", "reported uncorrectable rising",
                         "drive temperature above threshold for 3 days",
                         "predictive failure alert on drive bay"],
}

RUNBOOKS = [
    # runbook_id, title, product_family, category, symptoms, steps
    ("RB-001", "Expand a volume", "Storage Array", "how_to",
     "how do I expand a volume; grow LUN; add capacity to volume",
     "1. Open Storage > Volumes. 2. Select volume, Expand. 3. Enter new size. 4. Rescan on host."),
    ("RB-002", "Add a hot spare", "Storage Array", "how_to",
     "need steps to add a spare drive; assign spare; global spare",
     "1. Insert drive. 2. Storage > Disks > Assign as spare. 3. Confirm spare policy."),
    ("RB-003", "Find the alert log", "Rack Server", "how_to",
     "where is the alert log in the management console; view alerts; event log",
     "1. Management console > Maintenance > System Event Log. 2. Filter by severity."),
    ("RB-004", "Export health report", "HCI Node", "how_to",
     "how to export the health report; health check export; support bundle",
     "1. Cluster > Health. 2. Export. 3. Attach to case."),
    ("RB-005", "Configure SNMP alerts", "Rack Server", "how_to",
     "how to set up SNMP alerts; snmp trap destination",
     "1. Management > Alerts > SNMP. 2. Add trap destination. 3. Test trap."),
    ("RB-006", "Degraded RAID after change", "Storage Array", "config",
     "RAID group shows degraded after config change; disk group degraded; rebuild",
     "1. Check disk group state. 2. Verify all members present. 3. Start rebuild if a spare exists."),
    ("RB-007", "Management port addressing", "Rack Server", "config",
     "wrong IP on management port; management IP; iDRAC-style port",
     "1. Console > Network. 2. Set static address. 3. Verify from jump host."),
    ("RB-008", "LUN mapping not visible", "Storage Array", "config",
     "LUN not visible to host after mapping; host cannot see volume; masking",
     "1. Verify host initiator. 2. Check masking view. 3. Rescan HBA on host."),
    ("RB-009", "Controller time drift", "Storage Array", "config",
     "NTP drift on controller; time out of sync; clock skew",
     "1. Settings > Time. 2. Set NTP servers. 3. Force sync."),
    ("RB-010", "Replication schedule", "Backup Appliance", "config",
     "replication schedule not running; replication paused; schedule skipped",
     "1. Replication > Policies. 2. Check schedule window. 3. Resume policy."),
    ("RB-011", "Drive firmware mismatch", "Storage Array", "firmware",
     "drive firmware mismatch warning; firmware versions differ; drive fw",
     "1. Download drive firmware bundle. 2. Apply rolling update. 3. Verify versions."),
    ("RB-012", "Controller firmware update failed", "Storage Array", "firmware",
     "controller firmware update failed at 60%; update stalled; firmware failed",
     "1. Collect logs. 2. Retry on peer controller first. 3. Escalate if second failure."),
    ("RB-013", "Backplane firmware unsupported", "Rack Server", "firmware",
     "BIOS reports unsupported firmware on backplane; backplane fw",
     "1. Check compatibility matrix. 2. Update backplane firmware. 3. Reboot."),
    ("RB-014", "Firmware rollback", "HCI Node", "firmware",
     "firmware rollback requested; revert firmware; downgrade",
     "1. Confirm supported rollback path. 2. Stage previous bundle. 3. Apply node by node."),
    ("RB-015", "Intermittent path loss", "Storage Array", "connectivity",
     "host loses path intermittently; multipath flapping; path down",
     "1. Check SFP and cable. 2. Review switch port errors. 3. Replace cable if CRC errors."),
    ("RB-016", "Management port unreachable", "Rack Server", "connectivity",
     "management port unreachable; cannot ping management",
     "1. Verify link light. 2. Check VLAN. 3. Reset management controller."),
    ("RB-017", "SAS link resets", "Storage Array", "connectivity",
     "SAS link resets in log; phy errors; link reset",
     "1. Check SAS cable seating. 2. Replace cable. 3. If persists, backplane."),
    ("RB-018", "Backup window latency", "Storage Array", "performance",
     "latency spikes on volume during backup window; backup slow",
     "1. Check QoS policy. 2. Move backup window. 3. Enable throttling."),
    ("RB-019", "Slow rebuild", "Storage Array", "performance",
     "slow rebuild on disk group; rebuild taking days",
     "1. Raise rebuild priority. 2. Check for failing peer drives. 3. Monitor."),
    ("RB-020", "Alert cleared on its own", "Rack Server", "false_alarm",
     "predictive failure alert cleared by itself; amber light but no fault in logs",
     "1. Check event log for transient. 2. Clear alert. 3. Monitor 7 days."),
    ("RB-021", "Temperature alert", "HCI Node", "false_alarm",
     "temperature alert during HVAC maintenance; inlet temperature",
     "1. Confirm facility event. 2. Clear alert when inlet temperature normal."),
    ("RB-022", "Replace a failed drive", "Storage Array", "drive_failure",
     "drive in bay reports failed; drive offline, rebuild started; drive not detected after reboot",
     "1. Confirm failed drive bay. 2. Dispatch replacement sled. 3. Verify rebuild complete."),
]

QUERY_TEMPLATES = [
    ("SELECT count(*) FROM telemetry_drive_summary WHERE failed = 1", ["telemetry_drive_summary"]),
    ("SELECT region, count(*) FROM \"case\" c JOIN installed_asset a USING (asset_id) JOIN site s USING (site_id) GROUP BY region",
     ["case", "installed_asset", "site"]),
    ("SELECT part_id, on_hand FROM inventory WHERE depot_id = 'DEPOT-AMER-East'", ["inventory"]),
    ("SELECT p.family, count(*) FROM dispatch d JOIN \"case\" c USING (case_id) JOIN installed_asset a USING (asset_id) JOIN product p USING (product_id) GROUP BY 1",
     ["dispatch", "case", "installed_asset", "product"]),
    ("SELECT s.name, count(*) FROM dispatch_part dp JOIN part_supplier ps USING (part_id) JOIN supplier s USING (supplier_id) GROUP BY 1 ORDER BY 2 DESC LIMIT 5",
     ["dispatch_part", "part_supplier", "supplier"]),
    ("SELECT model, vendor, failure_count::float / drive_count FROM drive_model ORDER BY 3 DESC",
     ["drive_model"]),
    ("SELECT account_id, count(*) FROM \"case\" GROUP BY 1 ORDER BY 2 DESC LIMIT 20", ["case"]),
    ("SELECT technician_id, count(*) FROM dispatch WHERE outcome <> 'resolved' GROUP BY 1",
     ["dispatch"]),
    ("SELECT date, count(*) FILTER (WHERE failure = 1) FROM telemetry_daily_recent GROUP BY 1",
     ["telemetry_daily_recent"]),
    ("SELECT category, avg(epoch(closed_at) - epoch(opened_at)) FROM \"case\" GROUP BY 1", ["case"]),
]
QUERY_USERS = ["svc_dashboard", "analyst_01", "analyst_02", "ops_lead", "etl_nightly", "agent_triage"]


@dataclass
class Tables:
    data: dict[str, pd.DataFrame] = field(default_factory=dict)
    truth: dict[str, pd.DataFrame] = field(default_factory=dict)


def _ts_to_iso(ts: np.ndarray) -> np.ndarray:
    return np.datetime_as_string(ts.astype("datetime64[s]"), unit="s")


def _ts_to_us(ts: np.ndarray) -> list[str]:
    """Legacy format MM/DD/YYYY HH:MM (mess rule D1)."""
    return [pd.Timestamp(t).strftime("%m/%d/%Y %H:%M") for t in ts]


def _capacity_class(capacity_bytes: np.ndarray) -> np.ndarray:
    tb = capacity_bytes / 1e12
    return np.where(tb < 10, "small", np.where(tb < 16, "mid", "large"))


def _sled_part_for_capacity(capacity_bytes: float) -> str:
    tb = capacity_bytes / 1e12
    sleds = [(p[0], p[5]) for p in PARTS if p[3] == "drive" and p[5] and p[5] >= 4]
    sleds.sort(key=lambda x: x[1])
    for pid, cap in sleds:
        if tb <= cap + 0.5:
            return pid
    return sleds[-1][0]


def _variant_spellings(name: str, legal: str, rng: np.random.Generator) -> list[str]:
    """Two alternative spellings for an account name (mess rule A1)."""
    base = name
    options = [
        f"{base}, {legal}.",
        base.upper(),
        f"The {base}",
        base.replace(" ", "  "),
        f"{base} {legal}".replace("Logistics", "Log.").replace("Analytics", "Anlytcs"),
        f"{base.split(' ')[0]} {base.split(' ')[1][:4]}. {legal}",
    ]
    picks = rng.choice(len(options), size=2, replace=False)
    return [options[i] for i in picks]


def generate(drive_summary: pd.DataFrame, drive_model: pd.DataFrame, seed: int) -> Tables:
    rng = np.random.default_rng(seed)
    T = Tables()

    ds = drive_summary.sort_values("serial_number", kind="stable").reset_index(drop=True)
    n = len(ds)
    if n == 0:
        raise ValueError("drive_summary is empty")
    for col in ("first_seen", "last_seen", "failure_date"):
        ds[col] = pd.to_datetime(ds[col])
    vendor_by_model = dict(zip(drive_model["model"], drive_model["vendor"]))
    ds["vendor"] = ds["model"].map(vendor_by_model).fillna("Unknown")
    ds["vault_id"] = ds["vault_id"].fillna("none").astype(str)
    ds["datacenter"] = ds["datacenter"].fillna("none").astype(str)

    window_start = ds["first_seen"].min().to_datetime64().astype("datetime64[D]")
    window_end = ds["last_seen"].max().to_datetime64().astype("datetime64[D]")
    window_days = int((window_end - window_start) / DAY) + 1

    # ---------------- CRM: account (with spelling and id mess) ----------------
    # At least 12 accounts even for tiny inputs, so the spelling/id mess always
    # exists; accounts without assets are prospects, which real CRMs also hold.
    n_accounts = int(np.clip(round(n / 1500), 12, 300))
    combos = [(p, s) for p in NAME_PREFIX for s in NAME_SUFFIX]
    picks = rng.choice(len(combos), size=n_accounts, replace=False)
    canonical = []
    for i, k in enumerate(picks):
        p, s = combos[k]
        legal = LEGAL[rng.integers(len(LEGAL))]
        canonical.append({
            "canonical_account_id": f"ACC-{i + 1:05d}",
            "canonical_name": f"{p} {s} {legal}",
            "base_name": f"{p} {s}",
            "legal": legal,
            "industry": INDUSTRIES[rng.integers(len(INDUSTRIES))],
            "region": rng.choice(REGIONS, p=REGION_WEIGHTS),
        })
    account_rows, truth_rows = [], []
    next_dup_id = n_accounts + 1
    # A1: a fixed 20% of companies (at least 2) appear under three spellings.
    n_messy = max(2, int(round(0.20 * n_accounts)))
    messy_idx = set(rng.choice(n_accounts, size=n_messy, replace=False).tolist())
    first_messy = True
    for ci, c in enumerate(canonical):
        cid = c["canonical_account_id"]
        account_rows.append({"account_id": cid, "account_name": c["canonical_name"],
                             "industry": c["industry"], "region": c["region"],
                             "source_system": "crm_cloud"})
        truth_rows.append({"account_id": cid, "account_name": c["canonical_name"],
                           "canonical_account_id": cid, "variant_type": "canonical"})
        if ci in messy_idx:
            variants = _variant_spellings(c["base_name"], c["legal"], rng)
            if first_messy:   # guarantee both duplicate kinds exist at any scale
                kinds = ["duplicate_id", "duplicate_row"]
                first_messy = False
            else:
                kinds = ["duplicate_id" if rng.random() < 0.6 else "duplicate_row" for _ in variants]
            for v, vtype in zip(variants, kinds):
                if vtype == "duplicate_id":  # A2: a second id for the same company
                    vid = f"ACC-{next_dup_id:05d}"
                    next_dup_id += 1
                else:  # A3: same id, different spelling (duplicate row)
                    vid = cid
                account_rows.append({"account_id": vid, "account_name": v, "industry": c["industry"],
                                     "region": c["region"], "source_system": "legacy_crm"})
                truth_rows.append({"account_id": vid, "account_name": v,
                                   "canonical_account_id": cid, "variant_type": vtype})
    account = pd.DataFrame(account_rows)
    account.insert(0, "account_row_id", np.arange(1, len(account) + 1))
    T.data["account"] = account
    T.truth["account_resolution"] = pd.DataFrame(truth_rows)
    ids_for_canonical: dict[str, list[str]] = {}
    for r in truth_rows:
        ids_for_canonical.setdefault(r["canonical_account_id"], [])
        if r["account_id"] not in ids_for_canonical[r["canonical_account_id"]]:
            ids_for_canonical[r["canonical_account_id"]].append(r["account_id"])

    # ---------------- CRM: site ----------------
    site_rows = []
    for c in canonical:
        n_sites = int(rng.integers(1, 6))
        for j in range(n_sites):
            region = c["region"] if (j == 0 or rng.random() < 0.6) else rng.choice(REGIONS, p=REGION_WEIGHTS)
            city, state = CITIES[region][rng.integers(4)]
            site_rows.append({
                "site_id": f"SITE-{len(site_rows) + 1:05d}",
                "account_id": c["canonical_account_id"],
                "site_name": f"{c['base_name']} {city}",
                "address": f"{rng.integers(100, 9999)} {STREETS[rng.integers(len(STREETS))]}",
                "city": city, "state": state, "region": region,
            })
    site = pd.DataFrame(site_rows)
    T.data["site"] = site
    site_region = dict(zip(site["site_id"], site["region"]))
    site_account = dict(zip(site["site_id"], site["account_id"]))

    # ---------------- CRM: contact ----------------
    contact_rows = []
    for c in canonical:
        domain = c["base_name"].lower().replace(" ", "") + ".example"
        for _ in range(int(rng.integers(1, 4))):
            fn = FIRST_NAMES[rng.integers(len(FIRST_NAMES))]
            ln = LAST_NAMES[rng.integers(len(LAST_NAMES))]
            contact_rows.append({
                "contact_id": f"CON-{len(contact_rows) + 1:05d}",
                "account_id": c["canonical_account_id"],
                "full_name": f"{fn} {ln}",
                "title": TITLES[rng.integers(len(TITLES))],
                "email": f"{fn.lower()}.{ln.lower()}@{domain}",
                "phone": f"+1-555-{rng.integers(100, 999)}-{rng.integers(1000, 9999)}",
            })
    T.data["contact"] = pd.DataFrame(contact_rows)

    # ---------------- ERP: product ----------------
    product = pd.DataFrame(PRODUCTS, columns=["product_id", "product_name", "family", "drive_bays",
                                              "list_price_usd", "capacity_class"])
    T.data["product"] = product
    products_by_class = {k: product[product["capacity_class"] == k]["product_id"].tolist()
                         for k in ("small", "mid", "large")}
    product_family = dict(zip(product["product_id"], product["family"]))

    # ---------------- ERP: installed_asset (serial joins telemetry) ----------------
    # Whole vaults go to one site: drives that share a vault share a customer site.
    vault_key = (ds["datacenter"] + "/" + ds["vault_id"]).to_numpy()
    uniq_vaults, vault_idx = np.unique(vault_key, return_inverse=True)
    vault_site = site["site_id"].to_numpy()[rng.integers(len(site), size=len(uniq_vaults))]
    asset_site = vault_site[vault_idx]

    cap_class = _capacity_class(ds["capacity_bytes"].fillna(0).to_numpy(dtype=float))
    asset_product = np.array([
        products_by_class[k][rng.integers(len(products_by_class[k]))] for k in cap_class
    ])
    first_seen_d = ds["first_seen"].to_numpy().astype("datetime64[D]")
    install_date = first_seen_d - rng.integers(30, 1500, size=n) * DAY
    warranty_years = rng.choice([3, 5], size=n, p=[0.65, 0.35])
    warranty_end = (install_date.astype("datetime64[D]") + (warranty_years * 365) * DAY)
    warranty_null = rng.random(n) < 0.10                      # mess W1
    warranty_end_str = np.where(warranty_null, None, _ts_to_iso(warranty_end)[:].astype(object))
    warranty_end_str = np.array([None if s is None else str(s)[:10] for s in warranty_end_str], dtype=object)

    serial_clean = ds["serial_number"].to_numpy().astype(str)
    prefix_map = {"Seagate": "SG-", "HGST": "HGST_", "Toshiba": "TOS-", "Western Digital": "WD-",
                  "Hitachi": "HIT-"}
    prefixed = rng.random(n) < 0.15                            # mess S1
    lowered = (~prefixed) & (rng.random(n) < 0.03)             # mess S2
    serial_erp = serial_clean.astype(object).copy()
    vendors = ds["vendor"].to_numpy()
    for i in np.flatnonzero(prefixed):
        serial_erp[i] = prefix_map.get(vendors[i], "OEM-") + serial_clean[i]
    for i in np.flatnonzero(lowered):
        serial_erp[i] = serial_clean[i].lower()
    status = np.where(rng.random(n) < 0.02, "decommissioned", "active")  # mess E1

    asset_id = np.array([f"AST-{i + 1:07d}" for i in range(n)])
    installed_asset = pd.DataFrame({
        "asset_id": asset_id,
        "serial_number": serial_erp,
        "site_id": asset_site,
        "product_id": asset_product,
        "install_date": _ts_to_iso(install_date).astype(str).tolist(),
        "warranty_end": warranty_end_str,
        "status": status,
    })
    installed_asset["install_date"] = installed_asset["install_date"].str[:10]
    T.truth["asset_serial_map"] = pd.DataFrame({
        "asset_id": asset_id, "telemetry_serial": serial_clean,
        "erp_serial": serial_erp, "serial_mess": np.where(prefixed, "vendor_prefix",
                                                          np.where(lowered, "lowercase", "none")),
    })

    # ---------------- ERP: order, order_line ----------------
    install_month = install_date.astype("datetime64[M]")
    order_key = pd.DataFrame({"account_id": [site_account[s] for s in asset_site],
                              "month": install_month.astype(str), "product_id": asset_product,
                              "install_date": install_date})
    order_rows, line_rows, asset_order = [], [], np.empty(n, dtype=object)
    grouped = order_key.groupby(["account_id", "month"], sort=True)
    for (acc, month), g in grouped:
        oid = f"ORD-{len(order_rows) + 1:06d}"
        order_date = g["install_date"].min() - int(rng.integers(5, 40)) * DAY
        order_rows.append({
            "order_id": oid, "account_id": acc,
            "order_date": pd.Timestamp(order_date).strftime("%m/%d/%Y"),   # mess D2 (US format)
            "status": "fulfilled", "currency": "USD",
        })
        asset_order[g.index.to_numpy()] = oid
        for pid, gg in g.groupby("product_id", sort=True):
            price = float(product.loc[product["product_id"] == pid, "list_price_usd"].iloc[0])
            line_rows.append({"order_line_id": f"OL-{len(line_rows) + 1:07d}", "order_id": oid,
                              "product_id": pid, "quantity": int(len(gg)),
                              "unit_price_usd": round(price * float(rng.uniform(0.78, 0.98)), 2)})
    installed_asset["order_id"] = asset_order
    T.data["installed_asset"] = installed_asset
    T.data["order"] = pd.DataFrame(order_rows)
    T.data["order_line"] = pd.DataFrame(line_rows)

    # ---------------- Supply chain: part, supplier, part_supplier, inventory ----------------
    part = pd.DataFrame([p[:5] for p in PARTS], columns=["part_id", "part_number", "description",
                                                         "category", "unit_cost_usd"])
    T.data["part"] = part
    supplier = pd.DataFrame(SUPPLIERS, columns=["supplier_id", "name", "region"])
    T.data["supplier"] = supplier
    ps_rows = []
    for pid, cost in zip(part["part_id"], part["unit_cost_usd"]):
        k = int(rng.integers(1, 4))
        sups = rng.choice(len(SUPPLIERS), size=k, replace=False)
        for j, si in enumerate(sorted(sups)):
            ps_rows.append({"part_id": pid, "supplier_id": SUPPLIERS[si][0],
                            "lead_time_days": int(rng.integers(3, 45)),
                            "unit_cost_usd": round(float(cost) * float(rng.uniform(0.6, 0.9)), 2),
                            "preferred": bool(j == 0)})
    T.data["part_supplier"] = pd.DataFrame(ps_rows)
    inv_rows = []
    for depot in DEPOTS.values():
        for pid, cat in zip(part["part_id"], part["category"]):
            inv_rows.append({"depot_id": depot, "part_id": pid,
                             "on_hand": int(rng.poisson(PART_CATEGORY_STOCK[cat])),
                             "reorder_point": int(PART_CATEGORY_STOCK[cat] // 2)})
    T.data["inventory"] = pd.DataFrame(inv_rows)

    # ---------------- Support: technician ----------------
    n_tech = int(np.clip(round(n / 4000), 8, 80))
    tech_rows = []
    for i in range(n_tech):
        region = REGIONS[i % len(REGIONS)] if i < len(REGIONS) else rng.choice(REGIONS, p=REGION_WEIGHTS)
        skills = sorted(rng.choice(SKILLS, size=int(rng.integers(1, 4)), replace=False).tolist())
        tech_rows.append({"technician_id": f"TEC-{i + 1:04d}",
                          "name": f"{FIRST_NAMES[rng.integers(len(FIRST_NAMES))]} {LAST_NAMES[rng.integers(len(LAST_NAMES))]}",
                          "region": region, "skills": ";".join(skills),
                          "hire_date": str((np.datetime64("2018-01-01") + int(rng.integers(0, 2800)) * DAY))})
    technician = pd.DataFrame(tech_rows)
    T.data["technician"] = technician
    techs_by_region = {r: technician[technician["region"] == r]["technician_id"].tolist() for r in REGIONS}

    # ---------------- Support: runbook ----------------
    runbook = pd.DataFrame(RUNBOOKS, columns=["runbook_id", "title", "product_family", "category",
                                              "symptoms", "steps"])
    T.data["runbook"] = runbook
    runbooks_by_cat_fam: dict[tuple[str, str], list[int]] = {}
    for i, r in runbook.iterrows():
        runbooks_by_cat_fam.setdefault((r["category"], r["product_family"]), []).append(i)
    runbooks_by_cat: dict[str, list[int]] = {}
    for i, r in runbook.iterrows():
        runbooks_by_cat.setdefault(r["category"], []).append(i)

    # ---------------- Support: case ----------------
    # 1) cases causally tied to telemetry failures
    failed_idx = np.flatnonzero(ds["failed"].to_numpy() == 1)
    fail_dates = ds["failure_date"].to_numpy().astype("datetime64[D]")
    case_specs: list[dict] = []
    for i in failed_idx:
        fd = fail_dates[i]
        if rng.random() < 0.85:   # post-failure case
            lag = float(rng.exponential(1.2))
            opened = fd.astype("datetime64[s]") + np.timedelta64(int(lag * 86400 + rng.integers(0, 86400)), "s")
            case_specs.append({"asset_idx": int(i), "opened": opened, "category": "drive_failure",
                               "severity": int(rng.choice([1, 2], p=[0.4, 0.6])),
                               "telemetry_failure": True, "hardware_fault": True})
        if rng.random() < 0.25:   # early-warning case before the failure flag
            lead = int(rng.integers(1, 22))
            opened = (fd - lead * DAY).astype("datetime64[s]") + np.timedelta64(int(rng.integers(0, 86400)), "s")
            cat = "predictive_alert" if rng.random() < 0.7 else "performance"
            case_specs.append({"asset_idx": int(i), "opened": opened, "category": cat,
                               "severity": int(rng.choice([2, 3], p=[0.5, 0.5])),
                               "telemetry_failure": True, "hardware_fault": True})
    # 2) background cases, uniform over assets and the window
    n_background = max(8, int(rng.poisson(0.0002 * n * window_days)))
    bg_assets = rng.integers(0, n, size=n_background)
    bg_cats = rng.choice(CASE_CATEGORIES, size=n_background, p=BACKGROUND_WEIGHTS)
    bg_offsets = rng.integers(0, window_days * 86400, size=n_background)
    for a, cat, off in zip(bg_assets, bg_cats, bg_offsets):
        opened = window_start.astype("datetime64[s]") + np.timedelta64(int(off), "s")
        hw = (cat == "drive_failure" and rng.random() < 0.7) or (cat in ("connectivity", "performance") and rng.random() < 0.15)
        sev = int(rng.choice([1, 2, 3, 4], p=[0.05, 0.2, 0.45, 0.3])) if cat != "drive_failure" else int(rng.choice([1, 2], p=[0.3, 0.7]))
        case_specs.append({"asset_idx": int(a), "opened": opened, "category": cat, "severity": sev,
                           "telemetry_failure": False, "hardware_fault": bool(hw)})
    case_specs.sort(key=lambda c: (c["opened"], c["asset_idx"], c["category"]))

    case_rows, case_truth_rows, dispatch_rows, dispatch_truth_rows, dispatch_part_rows = [], [], [], [], []
    sled_cache: dict[int, str] = {}
    for k, spec in enumerate(case_specs):
        case_id = f"CS-{k + 1:07d}"
        i = spec["asset_idx"]
        cat = spec["category"]
        sev = spec["severity"]
        opened = spec["opened"]
        site_id = asset_site[i]
        canonical_acc = site_account[site_id]
        ids = ids_for_canonical[canonical_acc]
        acc_id = ids[0] if (len(ids) == 1 or rng.random() < 0.7) else ids[int(rng.integers(1, len(ids)))]
        pid = asset_product[i]
        fam = product_family[pid]
        symptom = SYMPTOMS[cat][rng.integers(len(SYMPTOMS[cat]))]

        # runbook match and deflectability (truth)
        rb_candidates = runbooks_by_cat_fam.get((cat, fam)) or runbooks_by_cat.get(cat) or []
        rb_idx = int(rb_candidates[rng.integers(len(rb_candidates))]) if rb_candidates and rng.random() < 0.8 else None
        if cat in ("how_to", "config", "firmware") and sev >= 3 and rb_idx is not None:
            deflectable = rng.random() < 0.85
            reason = "runbook_covers_symptom"
        elif cat == "false_alarm":
            deflectable = rng.random() < 0.6
            reason = "transient_alert"
        else:
            deflectable = rng.random() < 0.03
            reason = "rare_self_service"
        if not deflectable:
            reason = "needs_human" if spec["hardware_fault"] else "no_matching_runbook" if rb_idx is None else "customer_could_not_self_serve"
        if deflectable and rb_idx is not None:
            # copy a symptom phrase from the runbook so text matching has real signal
            rb_sym = runbook.iloc[rb_idx]["symptoms"].split(";")
            symptom = rb_sym[rng.integers(len(rb_sym))].strip()

        source = "legacy_crm" if rng.random() < 0.30 else "svc_cloud"
        open_prob = 0.05
        is_open = rng.random() < open_prob
        dur_hours = float(rng.gamma(2.0, {1: 10, 2: 24, 3: 60, 4: 120}[sev]))
        closed = None if is_open else opened + np.timedelta64(int(dur_hours * 3600), "s")

        if spec["hardware_fault"]:
            resolution_code = "hardware_replaced" if rng.random() < 0.9 else "no_fault_found"
        elif cat == "false_alarm":
            resolution_code = rng.choice(["no_fault_found", "customer_educated"], p=[0.7, 0.3])
        elif cat in ("how_to",):
            resolution_code = rng.choice(["customer_educated", "duplicate"], p=[0.9, 0.1])
        elif cat == "config":
            resolution_code = rng.choice(["config_change", "customer_educated"], p=[0.8, 0.2])
        elif cat == "firmware":
            resolution_code = rng.choice(["firmware_update", "no_fault_found"], p=[0.85, 0.15])
        else:
            resolution_code = rng.choice(["config_change", "no_fault_found", "hardware_replaced"], p=[0.5, 0.35, 0.15])
        if is_open:
            resolution_code = "pending"
        resolution_text = {
            "hardware_replaced": f"Replaced failed component in {fam.lower()} after on-site diagnosis. Rebuild verified.",
            "no_fault_found": "No fault found on inspection; logs reviewed, alert cleared, customer advised to monitor.",
            "customer_educated": "Walked customer through the documented procedure; no change to hardware.",
            "config_change": "Corrected configuration with customer on a remote session.",
            "firmware_update": "Applied the current firmware bundle remotely and confirmed version.",
            "duplicate": "Duplicate of an existing case; merged.",
            "pending": "",
        }[resolution_code]
        description = (f"{fam} {pid}: {symptom}. Customer reports issue started "
                       f"{int(rng.integers(0, 72))} hours before opening the case. Severity {sev}.")

        opened_str = _ts_to_us([opened])[0] if source == "legacy_crm" else str(_ts_to_iso(np.array([opened]))[0])
        closed_str = None if closed is None else (_ts_to_us([closed])[0] if source == "legacy_crm" else str(_ts_to_iso(np.array([closed]))[0]))

        case_rows.append({
            "case_id": case_id, "opened_at": opened_str, "closed_at": closed_str,
            "account_id": acc_id, "asset_id": asset_id[i], "category": cat, "severity": sev,
            "description": description, "resolution_code": resolution_code,
            "resolution_text": resolution_text,
            # support definition of failure (mess rule F1): a case that ended in a part swap
            "failure_reported": int(resolution_code == "hardware_replaced"),
            "source_system": source,
        })
        case_truth_rows.append({
            "case_id": case_id,
            "opened_at_iso": str(_ts_to_iso(np.array([opened]))[0]),
            "closed_at_iso": None if closed is None else str(_ts_to_iso(np.array([closed]))[0]),
            "canonical_account_id": canonical_acc,
            "telemetry_serial": serial_clean[i],
            "linked_to_telemetry_failure": bool(spec["telemetry_failure"]),
            "telemetry_failure_date": None if not spec["telemetry_failure"] else str(fail_dates[i]),
            "true_hardware_fault": bool(spec["hardware_fault"]),
            "deflectable": bool(deflectable),
            "deflect_reason": reason,
            "matching_runbook_id": None if rb_idx is None else runbook.iloc[rb_idx]["runbook_id"],
        })

        # ---------------- Support: dispatch (with repeats) ----------------
        if rng.random() < DISPATCH_PROB[cat]:
            region = site_region[site_id]
            techs = techs_by_region[region] or technician["technician_id"].tolist()
            visit = 0
            prev_outcome = None
            t = opened
            while True:
                visit += 1
                mean_days = 1.5 if sev <= 2 else 3.0
                t = t + np.timedelta64(int(rng.gamma(2.0, mean_days / 2.0) * 86400), "s")
                did = f"DSP-{len(dispatch_rows) + 1:07d}"
                if visit == 1:
                    outcome = rng.choice(["resolved", "parts_not_available", "no_fault_found", "incomplete"],
                                         p=[0.72, 0.10, 0.10, 0.08])
                    if not spec["hardware_fault"] and outcome == "parts_not_available":
                        outcome = "no_fault_found"
                else:
                    outcome = rng.choice(["resolved", "incomplete"], p=[0.85, 0.15])
                parts: list[tuple[str, int]] = []
                if spec["hardware_fault"] and outcome in ("resolved", "incomplete"):
                    cap = float(ds["capacity_bytes"].iloc[i] or 0)
                    sled = sled_cache.setdefault(int(cap), _sled_part_for_capacity(cap))
                    parts.append((sled, 1))
                    if rng.random() < 0.08:
                        parts.append(("PRT-0019" if fam != "Storage Array" else "PRT-0020", 1))
                elif outcome == "resolved" and rng.random() < 0.25:
                    non_drive = part[part["category"] != "drive"]["part_id"].tolist()
                    parts.append((non_drive[rng.integers(len(non_drive))], 1))
                if visit == 1:
                    necessary = bool(spec["hardware_fault"]) and not deflectable
                    why = "hardware_fault" if necessary else ("deflectable_case" if deflectable else "no_hardware_fault")
                else:
                    necessary = bool(spec["hardware_fault"]) and prev_outcome in ("parts_not_available", "incomplete")
                    why = "follow_up_after_" + str(prev_outcome) if necessary else "repeat_without_new_fault"
                dispatch_rows.append({
                    "dispatch_id": did, "case_id": case_id, "technician_id": techs[rng.integers(len(techs))],
                    "scheduled_at": str(_ts_to_iso(np.array([t]))[0]), "visit_number": visit,
                    "outcome": str(outcome), "parts_used": ";".join(p for p, _ in parts),
                })
                dispatch_truth_rows.append({"dispatch_id": did, "case_id": case_id, "necessary": necessary,
                                            "reason": why, "is_repeat": visit > 1})
                for p, q in parts:
                    dispatch_part_rows.append({"dispatch_id": did, "part_id": p, "quantity": q})
                prev_outcome = outcome
                repeat = (outcome != "resolved" and rng.random() < 0.85) or (outcome == "resolved" and rng.random() < 0.06)
                if not repeat or visit >= 3:
                    break

    T.data["case"] = pd.DataFrame(case_rows)
    T.data["dispatch"] = pd.DataFrame(dispatch_rows)
    T.data["dispatch_part"] = pd.DataFrame(dispatch_part_rows, columns=["dispatch_id", "part_id", "quantity"])
    T.truth["case_truth"] = pd.DataFrame(case_truth_rows)
    T.truth["dispatch_truth"] = pd.DataFrame(dispatch_truth_rows, columns=["dispatch_id", "case_id", "necessary", "reason", "is_repeat"])

    # ---------------- Metadata for Project 2 ----------------
    T.data["data_product"] = pd.DataFrame([
        {"data_product_id": "DP-001", "name": "fleet_health", "owner": "services_ops_ds",
         "description": "Per-drive and per-site health from telemetry", "freshness_sla_hours": 24,
         "tables": "telemetry_drive_summary;drive_model;installed_asset;site"},
        {"data_product_id": "DP-002", "name": "parts_demand", "owner": "supply_chain_analytics",
         "description": "Parts used in dispatches by region and month", "freshness_sla_hours": 168,
         "tables": "dispatch;dispatch_part;part;inventory;site"},
        {"data_product_id": "DP-003", "name": "dispatch_quality", "owner": "services_ops_ds",
         "description": "Repeat dispatch rates by product, category, region", "freshness_sla_hours": 24,
         "tables": "dispatch;case;installed_asset;product;site"},
    ])
    jobs = [
        ("JOB-001", "load_telemetry", "daily 02:00", "sois.load_telemetry"),
        ("JOB-002", "generate_enterprise", "on demand", "sois.generate_enterprise"),
        ("JOB-003", "load_postgres", "daily 03:00", "sois.load_postgres"),
        ("JOB-004", "build_features", "daily 04:00", "planned"),
    ]
    T.data["job"] = pd.DataFrame(jobs, columns=["job_id", "name", "schedule", "entrypoint"])
    lineage = [
        ("JOB-001", "backblaze_daily_csv", "telemetry_daily"),
        ("JOB-001", "telemetry_daily", "telemetry_drive_summary"),
        ("JOB-001", "telemetry_drive_summary", "drive_model"),
        ("JOB-002", "telemetry_drive_summary", "installed_asset"),
        ("JOB-002", "installed_asset", "case"),
        ("JOB-002", "case", "dispatch"),
        ("JOB-002", "dispatch", "dispatch_part"),
        ("JOB-003", "telemetry_daily", "telemetry_daily_recent"),
        ("JOB-004", "telemetry_daily", "features_30d"),
        ("JOB-004", "case", "features_30d"),
    ]
    T.data["lineage"] = pd.DataFrame(lineage, columns=["job_id", "input_table", "output_table"])
    n_q = 300
    q_rows = []
    for k in range(n_q):
        sql, tables = QUERY_TEMPLATES[rng.integers(len(QUERY_TEMPLATES))]
        ts = window_start.astype("datetime64[s]") + np.timedelta64(int(rng.integers(0, window_days * 86400)), "s")
        q_rows.append({"query_id": f"Q-{k + 1:05d}", "ran_at": str(_ts_to_iso(np.array([ts]))[0]),
                       "user_name": QUERY_USERS[rng.integers(len(QUERY_USERS))], "sql_text": sql,
                       "tables": ";".join(tables), "duration_ms": int(rng.lognormal(5.5, 0.8)),
                       "rows_returned": int(rng.integers(1, 5000))})
    T.data["query_log"] = pd.DataFrame(q_rows).sort_values("ran_at", kind="stable").reset_index(drop=True)
    return T


# ---------------------------------------------------------------------------
def _write_df(df: pd.DataFrame, path_csv: Path) -> None:
    df.to_csv(path_csv, index=False, lineterminator="\n")
    df.to_parquet(path_csv.with_suffix(".parquet"), index=False)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_outputs(T: Tables, out_dir: Path, truth_dir: Path, seed: int, input_sha: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    truth_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"seed": seed, "input_drive_summary_sha256": input_sha, "tables": {}, "truth": {}}
    for name, df in T.data.items():
        p = out_dir / f"{name}.csv"
        _write_df(df, p)
        manifest["tables"][name] = {"rows": int(len(df)), "sha256_csv": sha256_file(p)}
    for name, df in T.truth.items():
        p = truth_dir / f"{name}.csv"
        _write_df(df, p)
        manifest["truth"][name] = {"rows": int(len(df)), "sha256_csv": sha256_file(p)}
    (truth_dir / "generator_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def load_inputs(parquet_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    ds_path = parquet_dir / "drive_summary.parquet"
    dm_path = parquet_dir / "drive_model.parquet"
    return pd.read_parquet(ds_path), pd.read_parquet(dm_path), sha256_file(ds_path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parquet", type=Path, default=Path("data/parquet"), help="dir with drive_summary.parquet and drive_model.parquet")
    ap.add_argument("--out", type=Path, default=Path("data/enterprise"))
    ap.add_argument("--truth", type=Path, default=Path("truth"))
    ap.add_argument("--seed", type=int, default=20261017)
    args = ap.parse_args(argv)

    ds, dm, sha = load_inputs(args.parquet)
    T = generate(ds, dm, args.seed)
    manifest = write_outputs(T, args.out, args.truth, args.seed, sha)
    for name, info in manifest["tables"].items():
        print(f"{name:18s} {info['rows']:>9,} rows")
    for name, info in manifest["truth"].items():
        print(f"truth/{name:12s} {info['rows']:>9,} rows")
    print(f"seed={args.seed} manifest={args.truth / 'generator_manifest.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
