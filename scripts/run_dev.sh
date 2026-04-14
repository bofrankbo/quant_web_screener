#!/bin/bash
# Run FastAPI + Streamlit in parallel for local dev
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

# Use venv if present
PYTHON=python3
UVICORN=uvicorn
STREAMLIT=streamlit
if [ -d ".venv" ]; then
    PYTHON=".venv/bin/python"
    UVICORN=".venv/bin/uvicorn"
    STREAMLIT=".venv/bin/streamlit"
fi

echo "Starting FastAPI..."
$UVICORN app.api:app --reload --host 0.0.0.0 --port 8000 &
API_PID=$!

echo "Starting Streamlit..."
$STREAMLIT run frontend/streamlit_app.py --server.port 8501 &
ST_PID=$!

trap "kill $API_PID $ST_PID" EXIT
wait
