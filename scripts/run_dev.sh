#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

UVICORN=uvicorn
if [ -d ".venv" ]; then
    UVICORN=".venv/bin/uvicorn"
fi

echo "http://localhost:8000/       <- Screener"
echo "http://localhost:8000/draw   <- Pattern Draw"
echo "http://localhost:8000/docs   <- API Docs"

exec $UVICORN app.api:app --reload --host 0.0.0.0 --port 8000
