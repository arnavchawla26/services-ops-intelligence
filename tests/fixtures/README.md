# Test fixtures

`backblaze_synthetic/` holds three hand-made daily CSVs in the Backblaze Hard
Drive Data column layout (2026 schema: date, serial_number, model,
capacity_bytes, failure, datacenter, cluster_id, vault_id, pod_id,
pod_slot_num, is_legacy_format, then smart_N_normalized / smart_N_raw pairs).

Everything in them is synthetic. Serials start with `SYNTH`, models contain
`SYNTH`, and no value describes a real drive. Day 2 drops `smart_199_*` and
day 3 adds `smart_240_*` so the loader's schema-drift handling is exercised.
`SYNTH0003` has `failure = 1` on 2026-04-02 and is absent on 2026-04-03, which
is how a failed drive looks in the real files.
