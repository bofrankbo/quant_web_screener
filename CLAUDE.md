# CLAUDE.md

## Project Goal

Build a K-line quantitative stock screener web app for Taiwan equities.
User is a quant trader — do NOT explain basic financial terms. Be concise, CLI-first.

## What This Is

A new standalone repo at `/Users/yanyifu/Documents/_Coding/quant_web_screener/`.
The existing backtrader research repo at `/Users/yanyifu/Documents/_Coding/Trading/` is untouched — this project reads its data but does not modify it.

## Stack

| Layer | Tool |
|---|---|
| Data query | DuckDB (reads CSVs in-place, no migration) |
| Transforms | Polars |
| API | FastAPI (`app/api.py`) |
| Frontend | Streamlit (`frontend/streamlit_app.py`) |

## Data Source (read-only)

All data lives in the **Trading repo** — do not copy or migrate it.

```
/Users/yanyifu/Documents/_Coding/Trading/history_data/tw/
  stock_price_adj/   ← 1.1GB, adjusted daily OHLCV CSVs, one file per ticker
  concentration/     ← 51MB, 籌碼集中度
  traderinfo/        ← 49GB, 三大法人 (query selectively)
  PER_PBR/           ← valuation data
```

Path config is in `app/config.py` and `.env` (copy from `.env.example`).

## Architecture

```
quant_web_screener/
  app/
    config.py       ← env-based path config
    screener.py     ← DuckDB screening logic (main logic lives here)
    api.py          ← FastAPI, GET /screen with query params
  frontend/
    streamlit_app.py  ← sidebar params → POST to API → dataframe display
  data/
    screener.duckdb   ← created on first run (gitignored)
  scripts/
    run_dev.sh        ← starts FastAPI :8000 + Streamlit :8501 in parallel
```

## Current Screener Logic (`app/screener.py`)

Parameters exposed via API:
- `ma_window` (default 20) — MA period
- `volume_ratio` (default 1.5) — min ratio of today's vol vs MA vol
- `price_above_ma` (default true) — close > MA filter
- `top_n` (default 50)

DuckDB queries `stock_price_adj/*.csv` directly using window functions. Returns a Polars DataFrame.

## What Is NOT Done Yet (next tasks)

The screening conditions are minimal. User needs to decide and implement:

1. **布林通道突破** — Bollinger Band breakout entry signal
2. **籌碼集中度變化** — use `concentration/` CSVs, join with price data
3. **三大法人買超** — use `traderinfo/` (49GB — query by ticker, not full scan)
4. **複合條件篩選** — combine above signals with AND/OR logic in the API
5. **前端圖表** — add candlestick chart per ticker (consider Plotly in Streamlit)
6. **排程更新** — cron or n8n to refresh data daily

## Dev Commands

```bash
cp .env.example .env        # set TRADING_DATA_PATH if needed
pip install -r requirements.txt
./scripts/run_dev.sh        # FastAPI :8000 + Streamlit :8501

# or separately:
uvicorn app.api:app --reload --port 8000
streamlit run frontend/streamlit_app.py
```

## Key Decisions Already Made

- **DuckDB not SQLite** — columnar, handles GB-scale CSVs, native Polars integration
- **No data migration** — DuckDB queries Trading repo CSVs in-place
- **Two separate repos** — Trading repo stays as backtrader research; this repo is the web product
- **Streamlit not React** — user wants fast iteration, not a production frontend
