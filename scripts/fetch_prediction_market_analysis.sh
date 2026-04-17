#!/usr/bin/env bash
# Download the jon-becker/prediction-market-analysis dataset (2026 clean
# Polymarket history) into data/polymarket/ so backtest.py and optimizer.py
# can read it.
set -euo pipefail

DATA_DIR=${1:-data/polymarket}
REPO_URL="https://github.com/jon-becker/prediction-market-analysis.git"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$DATA_DIR"
echo "Cloning $REPO_URL ..."
git clone --depth 1 "$REPO_URL" "$TMP_DIR/repo"

# Copy any parquet / csv files anywhere in the repo into $DATA_DIR preserving
# basename only. The upstream layout evolves; this is intentionally permissive.
shopt -s globstar nullglob
count=0
for f in "$TMP_DIR/repo"/**/*.parquet "$TMP_DIR/repo"/**/*.csv; do
    cp "$f" "$DATA_DIR/"
    count=$((count + 1))
done

echo "Copied $count data files into $DATA_DIR"
ls -1 "$DATA_DIR"

if [ "$count" -eq 0 ]; then
    echo "WARNING: no .parquet/.csv files found in upstream repo." >&2
    echo "Check https://github.com/jon-becker/prediction-market-analysis for" >&2
    echo "the current release layout and re-run, or drop your own dump into" >&2
    echo "$DATA_DIR/ manually." >&2
    exit 1
fi
