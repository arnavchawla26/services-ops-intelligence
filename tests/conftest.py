"""Shared helpers: a synthetic drive_summary big enough to exercise every mess rule."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from sois.load_telemetry import vendor_from_model


def synthetic_drive_summary(n: int = 3000, failure_rate: float = 0.02, seed: int = 1) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Shape matches data/parquet/drive_summary.parquet. All values synthetic."""
    rng = np.random.default_rng(seed)
    models = ["ST00000SYNTH", "HGST SYNTHMODEL", "TOSHIBA SYNTHMODEL", "WDC SYNTHMODEL"]
    start = np.datetime64("2026-04-01")
    end = np.datetime64("2026-06-30")
    failed = rng.random(n) < failure_rate
    fail_offsets = rng.integers(0, 91, size=n).astype("timedelta64[D]")
    fail_date = np.where(failed, start + fail_offsets, np.datetime64("NaT", "D"))
    ds = pd.DataFrame({
        "serial_number": [f"SYNTH{i:07d}" for i in range(n)],
        "model": rng.choice(models, size=n),
        "capacity_bytes": rng.choice([4e12, 12e12, 14e12, 16e12, 18e12], size=n).astype("int64"),
        "datacenter": rng.choice(["synA", "synB", "synC"], size=n),
        "cluster_id": "1",
        "vault_id": rng.integers(1000, 1100, size=n).astype(str),
        "pod_id": "1",
        "first_seen": start,
        "last_seen": np.where(failed, fail_date, end),
        "days_observed": 91,
        "failed": failed.astype(int),
        "failure_date": fail_date,
        "max_power_on_hours": 1000,
    })
    dm = (ds.groupby("model")
            .agg(capacity_bytes=("capacity_bytes", "max"), drive_count=("serial_number", "count"),
                 failure_count=("failed", "sum"))
            .reset_index())
    dm["vendor"] = dm["model"].map(vendor_from_model)
    return ds, dm


@pytest.fixture(scope="session")
def synth_inputs():
    return synthetic_drive_summary()
