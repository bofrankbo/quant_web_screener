#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
source .venv/bin/activate

DATE=${1:-$(date +%Y-%m-%d)}
echo "=== Daily update: $DATE ==="

echo "[1/3] Price (TaiwanStockPriceAdj → R2 tickers/)"
python -m scripts.ingest --date "$DATE"

echo "[2/3] Concentration (FinMind broker data → R2 concentration/)"
python -m scripts.concentration --date "$DATE"

echo "[3/3] Sync R2 → local cache (data/cache/tickers/)"
python -m scripts.sync_cache

echo "=== Done ==="
