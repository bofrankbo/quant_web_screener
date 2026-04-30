# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Taiwan equity K-line screener — FastAPI backend + vanilla HTML/JS frontend deployed on Railway.
User is a quant trader — do NOT explain basic financial terms. Be concise, CLI-first.

## Dev Commands

```bash
cp .env.example .env        # fill in R2_*, FINMIND_API_KEY, GOOGLE_* vars
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
bash scripts/run_dev.sh     # FastAPI :8000 (auto-reload)
# API docs: http://localhost:8000/docs
```

### First-time data setup

```bash
python -m scripts.ingest --backfill          # FinMind prices → R2 (~30 min)
python -m scripts.concentration --backfill   # FinMind broker conc → R2
python -m scripts.sync_cache --all           # R2 → local data/cache/
```

### Daily update

```bash
bash scripts/daily_update.sh              # today
bash scripts/daily_update.sh 2026-04-25   # specific date
```

Runs `fetch_prices`, `fetch_concentration`, `fetch_market_value` in parallel, then `sync_cache`.
Also works on Railway Shell.

## Architecture

```
FinMind API (999 plan)
    ↓  daily 19:00 TST (APScheduler inside FastAPI)
Cloudflare R2  (per-ticker Parquet)
    tickers/{ticker}.parquet        ← adjusted OHLCV
    concentration/{ticker}.parquet  ← 籌碼集中度 (5d, 20d)
    meta/ticker_info.parquet        ← stock names
    market_value/{ticker}.parquet   ← market cap
    ↓  startup_sync() + daily sync (scripts/sync_cache.py)
Railway persistent volume  `APP_DATA_PATH`/cache/
    ↓  DuckDB reads local parquet (no per-query network overhead)
FastAPI (:8000)
    ↓
Browser (HTML/JS, Lightweight Charts)
```

App-data persistence on Railway: `APP_DATA_PATH` (volume `/data`) holds both `cache/` and `app.sqlite`.

## Module Map

| File | Role |
|---|---|
| `app/api.py` | All FastAPI routes + lifespan (init_db, startup_sync, scheduler) |
| `app/backtest.py` | Polars indicator engine + signal-only backtest loop |
| `app/screener.py` | Thin wrapper — `/screen` endpoint uses `backtest.py` helpers |
| `app/pattern_matcher.py` | DTW pattern match against local cache |
| `app/auth.py` | Google OAuth flow; HMAC-signed stateless OAuth state tokens |
| `app/db.py` | SQLite CRUD — users, user_watchlists, watchlist_items, activity_logs |
| `app/scheduler.py` | APScheduler: daily 19:00 TST job + `startup_sync()` |
| `app/r2.py` | boto3 R2 client + `download_parquet()` |
| `app/config.py` | All path config from env (DATA_DIR, PARQUET_CACHE_PATH, SQLITE_PATH, …) |
| `app/daily_update.py` | Parallelized fetch orchestrator called by scheduler and CLI |
| `scripts/daily_update.py` | CLI entrypoint → `app/daily_update.run_daily_update()` |
| `scripts/fetch_prices.py` | FinMind TaiwanStockPriceAdj → R2 tickers/ |
| `scripts/fetch_concentration.py` | FinMind broker data → R2 concentration/ |
| `scripts/fetch_market_value.py` | Market cap data → R2 market_value/ |
| `scripts/sync_cache.py` | R2 → local data/cache/ |

## Auth & Watchlist Dual-Mode

Every watchlist endpoint checks for a logged-in user first:
- **Logged in**: data read/written to SQLite (`app/db.py`).
- **Anonymous**: falls back to `data/watchlists.json` (legacy).

On first Google login, `_import_legacy_watchlists_to_user()` migrates JSON → SQLite (no-op if user already has SQLite data).

OAuth state is **stateless HMAC-signed** (not session-stored) — the token embeds `nonce.issued_at.sig`. Verified in `auth.py:verify_oauth_state()`.

## Screener & Backtest Parameters

Entry conditions (all optional, combined with AND):
- `price_above_ma` — close > MA(ma_window)
- `bb_breakout` — close > BB upper (bb_window)
- `volume_ratio` — today vol / MA vol ≥ threshold
- `rsi_min / rsi_max` — RSI(rsi_period) filter
- `use_concentration` + `conc_5d_min / conc_20d_min` — 籌碼集中度
- `market_cap_rank` — restrict universe to top-N by market cap
- `watchlist` — restrict universe to a named watchlist

Exit conditions mirror entry with separate `exit_*` params.

Indicators (MA, BB, vol_ratio, RSI, concentration) are computed in Polars with `rolling_*().over("ticker")` window ops in `backtest.py:_compute_screen_indicators()`.

## Key Design Decisions

- **DuckDB reads local Parquet cache** — not the Trading repo CSVs; cache is synced from R2.
- **Stateless OAuth state** — avoids session-storage race conditions on Railway.
- **SQLite WAL mode** — concurrent reads don't block writes.
- **No data migration** — original Trading repo at `/Users/yanyifu/Documents/_Coding/Trading/` is untouched; this repo uses R2/FinMind as its own source.
- **Streamlit removed** — frontend is now vanilla HTML/JS with Lightweight Charts (TradingView OSS).

## Railway Deployment

Required env vars (set in Railway → Variables):
```
R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_ENDPOINT
FINMIND_API_KEY
SESSION_SECRET_KEY
GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
GOOGLE_REDIRECT_URI=https://<app>.up.railway.app/auth/google/callback
APP_DATA_PATH=/data
```

Volume: mount at `/data`, 2 GB. Start command (railway.toml): `uvicorn app.api:app --host 0.0.0.0 --port $PORT`.

On startup, `startup_sync()` runs in a daemon thread (non-blocking) to populate the cache from R2.
