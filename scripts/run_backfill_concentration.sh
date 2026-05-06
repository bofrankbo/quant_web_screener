#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."

if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
fi

TICKER_START=${BACKFILL_TICKER_START:-1101}
TICKER_END=${BACKFILL_TICKER_END:-9999}
CONCURRENCY=${BACKFILL_CONCURRENCY:-${BACKFILL_WORKERS:-6000}}

python -m scripts.backfill_concentration \
  --ticker-start "$TICKER_START" \
  --ticker-end "$TICKER_END" \
  --concurrency "$CONCURRENCY"
