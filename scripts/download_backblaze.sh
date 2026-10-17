#!/usr/bin/env bash
# Download Backblaze Hard Drive Data, 2026 Q2, into data/raw/ (an untrusted
# download directory; nothing in this repo executes from inside it).
#
# Source: https://www.backblaze.com/cloud-storage/resources/hard-drive-test-data
# File:   https://f001.backblazeb2.com/file/Backblaze-Hard-Drive-Data/data_Q2_2026.zip
# Size:   about 2.1 GB zipped, about 11.7 GB unzipped (Backblaze's own figures;
#         the script prints the real size it sees).
# Terms:  free to use, cite Backblaze, derivative work may be sold, the data
#         itself may not. See README "Data terms".
#
# Behaviour:
#   - never re-downloads an existing zip (pass --resume to continue a partial
#     download with curl -C -)
#   - verifies with `unzip -t` before extracting
#   - extracts into data/raw/data_Q2_2026/ (flattens the zip's top-level folder
#     if it has one, so the loader always finds data/raw/data_Q2_2026/*.csv)
#
# Usage: bash scripts/download_backblaze.sh [--resume] [--yes]
set -euo pipefail

URL="https://f001.backblazeb2.com/file/Backblaze-Hard-Drive-Data/data_Q2_2026.zip"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RAW_DIR="$REPO_ROOT/data/raw"
ZIP="$RAW_DIR/data_Q2_2026.zip"
OUT_DIR="$RAW_DIR/data_Q2_2026"

RESUME=0
YES=0
for arg in "$@"; do
  case "$arg" in
    --resume) RESUME=1 ;;
    --yes) YES=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

mkdir -p "$RAW_DIR"

if [[ -f "$ZIP" && $RESUME -eq 0 ]]; then
  echo "zip already present, not downloading: $ZIP"
else
  if [[ $YES -eq 0 ]]; then
    echo "About to download about 2.1 GB (11.7 GB after unzip) from:"
    echo "  $URL"
    echo "into $RAW_DIR"
    read -r -p "Continue? [y/N] " answer
    [[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "aborted"; exit 1; }
  fi
  if [[ $RESUME -eq 1 ]]; then
    curl -L --fail --retry 5 --retry-delay 10 -C - -o "$ZIP" "$URL"
  else
    curl -L --fail --retry 5 --retry-delay 10 -o "$ZIP" "$URL"
  fi
fi

echo "zip size on disk:"
ls -l "$ZIP"
du -h "$ZIP"

echo "verifying zip integrity (unzip -t) ..."
if ! unzip -tq "$ZIP"; then
  echo "integrity check FAILED. If a download is still running, wait for it;" >&2
  echo "otherwise rerun with --resume, or delete the zip and start over." >&2
  exit 1
fi

if [[ -d "$OUT_DIR" ]] && ls "$OUT_DIR"/*.csv >/dev/null 2>&1; then
  echo "already extracted: $OUT_DIR ($(ls "$OUT_DIR"/*.csv | wc -l | tr -d ' ') csv files)"
  exit 0
fi

echo "extracting into $OUT_DIR ..."
TMP_DIR="$RAW_DIR/.extract_tmp"
rm -rf "$TMP_DIR"
mkdir -p "$TMP_DIR"
unzip -q "$ZIP" -d "$TMP_DIR"

# Backblaze zips sometimes wrap the CSVs in a top-level folder; flatten it.
mkdir -p "$OUT_DIR"
find "$TMP_DIR" -type f -name '*.csv' -exec mv {} "$OUT_DIR"/ \;
rm -rf "$TMP_DIR"

echo "done: $(ls "$OUT_DIR"/*.csv | wc -l | tr -d ' ') csv files in $OUT_DIR"
du -sh "$OUT_DIR"
