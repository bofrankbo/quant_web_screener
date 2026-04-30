# quant_web_screener

```text
   ____            _       ____  _
  / __ \___  _____(_)___  / __ \(_)___  ____  ___  ___
 / / / / _ \/ ___/ / __ \/ / / / / __ \/ __ \/ _ \/ _ \
/ /_/ /  __/ /__/ / /_/ / /_/ / / / / / / / /  __/  __/
\____/\___/\___/_/\____/\____/_/_/ /_/_/ /_/\___/\___/

  >> Practical Wisdom in Quantitative Trading | Taiwan Stock Market
```

Taiwan stock screener and market dashboard built with FastAPI + vanilla HTML/JS.

## What it does

- Market overview with latest cache data, movers, and volume leaders
- Screener / backtest page for technical filters
- Watchlist manager with autocomplete and K-line popup
- Portfolio tracking
- Pattern drawing and DTW matching

## Main pages

- `/` - market dashboard
- `/market-overview` - market table and single-stock K-line
- `/backtest` - screener / backtest
- `/watchlist-manager` - watchlist manager
- `/portfolio` - holdings and PnL
- `/draw` - pattern draw tool

## Data layout

Runtime data lives under `APP_DATA_PATH`:

- `cache/tickers/` - per-ticker OHLCV parquet
- `cache/concentration/` - per-ticker concentration parquet
- `cache/market_value/` - daily market-cap parquet
- `cache/meta/` - ticker info parquet
- `watchlists.json` - legacy watchlist storage
- `stock_universe.csv` - generated stock universe
- `app.sqlite` - app data

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash scripts/run_dev.sh
```

Local app:

- FastAPI: `http://localhost:8000`
- Docs: `http://localhost:8000/docs`

## Required env vars

Set these in `.env`:

```bash
R2_ACCOUNT_ID
R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY
R2_BUCKET
R2_ENDPOINT
FINMIND_API_KEY
SESSION_SECRET_KEY
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
GOOGLE_REDIRECT_URI
APP_DATA_PATH
TRADING_DATA_PATH
```

## Common scripts

```bash
python -m scripts.fetch_prices --date 2026-04-30
python -m scripts.fetch_concentration --date 2026-04-30
python -m scripts.fetch_market_value --date 2026-04-30
python -m scripts.sync_cache --all
python -m scripts.daily_update --date 2026-04-30
python -m scripts.build_stock_universe
python -m scripts.backfill_concentration
```

## Notes

- `/backtest` is the screener entry page.
- The homepage is a market dashboard and only shows market/cache summaries.
- K-line styling is shared across watchlist, market overview, and screener.
- The app expects local parquet cache synced from R2 for fast queries.

## Repository layout

```text
app/        FastAPI backend and data logic
frontend/   Static HTML/JS pages
scripts/    Fetch, sync, and maintenance scripts
data/       Local runtime data and cache
```
