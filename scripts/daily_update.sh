#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."

if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
fi

DATE=${1:-$(date +%Y-%m-%d)}
python -m scripts.daily_update --date "$DATE"
