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

Pending. The development machine for this repository has no NVIDIA GPU, so the
notebook has not been run end to end yet; the pandas half of the script was
run locally on 8 days (2,766,327 rows, 346,361 drives on the last day) in 4.2
seconds on an Apple M-series laptop, which is only a check that the script is
correct, not a comparable number. When the Colab run is done, replace this
paragraph with the printed table (pandas seconds, cudf.pandas seconds,
speedup, per step) and the GPU name.

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
