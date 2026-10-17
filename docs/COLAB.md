# Colab: feature pipeline on NVIDIA cuDF

[Open in Colab](https://colab.research.google.com/github/arnavchawla26/services-ops-intelligence/blob/main/notebooks/feature_pipeline_cudf.ipynb)

`notebooks/feature_pipeline_cudf.ipynb` builds the failure-model features
(the same ones `sois.failure_model.build_features` computes with DuckDB) on
N days of the real Backblaze 2026 Q2 CSVs twice: once with plain pandas, once
with `cudf.pandas` (NVIDIA RAPIDS, preinstalled on Colab GPU runtimes), each in
its own process so the accelerator is loaded before pandas is imported. It
prints a timings table and the environment.

## Steps

1. Open the link above. Runtime -> Change runtime type -> T4 GPU.
2. Optionally set `N_DAYS` in the first code cell (8 minimum, 30 default,
   91 for the whole quarter).
3. Runtime -> Run all. The notebook downloads the 2.1 GB public zip from
   Backblaze; no secrets, no account.
4. Paste the last cell's output below under "Measured timings" and commit.

## Measured timings

Run by Arnav on Google Colab on 2026-10-17, N_DAYS = 30, Tesla T4, pandas
2.2.3, cudf 26.06.00. Pasted from the notebook's last cell:

| step                                  |   pandas_s |   cudf_pandas_s |   speedup |
|:--------------------------------------|-----------:|----------------:|----------:|
| read N CSVs (needed columns)          |      48.5  |           51.33 |      0.94 |
| features: lag, 7-day max, delta, join |       4.06 |            0.79 |      5.14 |
| script total (incl. import)           |      53.03 |           52.22 |      1.02 |
| process wall clock                    |      57.22 |           55.52 |      1.03 |

days: 30, rows read: 10,430,946, drives on last day: 350,542, heuristic-flagged:
29,360 (identical counts in both runs).

Reading: the feature step (groupby, joins, lags) ran 5.1x faster on cudf.pandas;
the CSV read did not, so end to end the run is a wash (1.03x). The honest claim
is "5x on the feature step, 1x end to end on this script because CSV parsing
dominates"; a Parquet input or a larger N_DAYS would shift the balance toward
the GPU, and that was not measured.

## What the timings mean and do not mean

- The work is one `read_csv` over N files, one `groupby` with max/count, two
  index joins and a few column operations: the same shape as the production
  feature step, at N days instead of the 8-day window per prediction date.
- `cudf.pandas` falls back to CPU pandas for operations it does not cover;
  the speedup is for this script, on this data, on a T4, nothing more.
- In the repository the same features are built by DuckDB over Parquet
  (`make models`, about 30 seconds for the whole quarter's training and test
  windows on the laptop). The Colab notebook is the RAPIDS comparison the
  Dell "NVIDIA Data Processing Engine" framing asks for, not the production
  path.
