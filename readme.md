# quant_web_screener

Taiwan equity K-line screener — FastAPI backend + vanilla HTML/JS frontend, deployed on Railway.

---

## Architecture

```
FinMind API (999 plan)
    ↓  daily 19:00 TST (APScheduler inside FastAPI)
    ↓  scripts/ingest.py + scripts/concentration.py
Cloudflare R2  (per-ticker Parquet)
    tickers/{ticker}.parquet        ← adjusted OHLCV
    concentration/{ticker}.parquet  ← 籌碼集中度 (5d, 20d)
    meta/ticker_info.parquet        ← stock names
    ↓  on startup + daily sync (scripts/sync_cache.py)
Railway persistent volume  `APP_DATA_PATH`/cache/
    ↓  DuckDB reads local parquet (fast, no network overhead per query)
FastAPI (:8000)
    ↓
Browser (HTML/JS, Lightweight Charts)
```

User-facing app data such as SQLite auth/profile/watchlist storage also lives under the Railway persistent volume at the path pointed to by `APP_DATA_PATH`.

**Cost: ~170 TWD/month** (Railway Hobby $5 + persistent volume $0.25/GB)

---

## Screener Filters (`/screen`)

| Filter | Description | Default |
|---|---|---|
| Close > MA(N) | Price above moving average | on, N=10 |
| Bollinger Upper Breakout | Close > MA + 2σ | off |
| Volume Ratio ≥ X | Today's vol / MA vol | 1.5x |
| RSI range | RSI(14) within [min, max] | 0–100 |
| 籌碼集中度 20d ≥ N | Broker concentration 20-day rolling | off |
| Market Cap Rank | Limit universe to top N by market cap | off (local-only) |

Output columns: `ticker, name, date, close, open, high, low, volume, ma, bb_upper, bb_lower, vol_ratio, rsi, concentration_5d, concentration_20d`

---

## Watchlist Manager (`/watchlist-manager`)

- Create/rename/delete named watchlists
- Per-watchlist summary: 代號, 名稱, 收盤, 日%, 5d%, 10d%, 20d%, custom column
- Overview panel: all watchlists with average returns, clickable to open
- 全部 view: all tickers across watchlists, grouped by list
- Per-ticker active toggle (●/○): controls whether ticker counts toward averages
- Logged-in users can export/import their watchlists as JSON from the watchlist manager page, which is useful for moving data between localhost and Railway.

Stored in `APP_DATA_PATH/watchlists.json` (on Railway persistent volume):
```json
{
  "晶圓": {
    "tickers": ["2330", "2303"],
    "custom_label": "備註",
    "custom": {"2330": "some note"},
    "active": {"2303": false}
  }
}
```

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/screen` | Screener with filters |
| GET | `/kline/{ticker}` | OHLCV + MA + Bollinger for single ticker |
| POST | `/pattern_match` | DTW pattern match against market |
| GET | `/watchlists` | List all watchlists `{name: count}` |
| GET | `/watchlists/{name}` | Ticker list |
| POST | `/watchlists/{name}` | Create watchlist |
| DELETE | `/watchlists/{name}` | Delete watchlist |
| PUT | `/watchlists/{name}/tickers/{ticker}` | Add ticker |
| DELETE | `/watchlists/{name}/tickers/{ticker}` | Remove ticker |
| GET | `/watchlists/{name}/summary` | Price summary (close, day%, 5d/10d/20d%, custom, active) |
| PUT | `/watchlists/{name}/custom_label` | Update custom column label |
| PUT | `/watchlists/{name}/custom/{ticker}` | Update custom value |
| PUT | `/watchlists/{name}/active/{ticker}` | Toggle ticker active state |

Static pages: `/` → screener, `/draw` → pattern draw, `/watchlist-manager` → watchlist manager

---

## Dev Setup

```bash
git clone <repo>
cd quant_web_screener
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in R2_* and FINMIND_API_KEY
bash scripts/run_dev.sh
# FastAPI: http://localhost:8000
# Docs:    http://localhost:8000/docs
```

### First-time data setup

```bash
# Backfill full price history → R2 (takes ~30 min)
python -m scripts.ingest --backfill

# Backfill concentration from local CSVs → R2
python -m scripts.concentration --backfill

# Sync R2 → local cache (needed for /screen and /draw)
python -m scripts.sync_cache --all
```

### Daily update (manual)

```bash
bash scripts/daily_update.sh          # today
bash scripts/daily_update.sh 2026-04-25  # specific date
```

---

## Railway Deployment

### One-time setup

1. Push repo to GitHub
2. [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
3. **Variables** → add all from `.env`:
   ```
   R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
   R2_BUCKET, R2_ENDPOINT, FINMIND_API_KEY
   SESSION_SECRET_KEY, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
   ```
4. **Service → Volumes** → New Volume, mount at `/data`, size 2GB
5. Redeploy — watch logs for `[startup] Syncing cache from R2`
6. Verify: `curl https://<app>.up.railway.app/health` → `{"status": "ok"}`

### How it runs on Railway

- **Startup**: `startup_sync()` downloads all R2 parquets → `APP_DATA_PATH/cache/` (background thread, non-blocking)
- **Daily 19:00 TST**: APScheduler triggers `ingest → concentration → sync_cache`
- **Persistent volume**: the directory pointed to by `APP_DATA_PATH` survives restarts — cache stays populated
- **SQLite app data**: `APP_DATA_PATH/app.sqlite` stores users, watchlists, preferences, and activity logs

---

## Project Structure

```
quant_web_screener/
├── app/
│   ├── api.py             ← FastAPI routes + lifespan (scheduler start)
│   ├── screener.py        ← DuckDB parquet scan → Polars indicators → filter
│   ├── pattern_matcher.py ← DTW pattern matching against local cache
│   ├── scheduler.py       ← APScheduler: daily 19:00 ingest + sync
│   ├── r2.py              ← boto3 R2 client + download_parquet()
│   └── config.py          ← path config (PARQUET_CACHE_PATH, etc.)
├── scripts/
│   ├── ingest.py          ← FinMind TaiwanStockPriceAdj → R2 tickers/
│   ├── concentration.py   ← FinMind broker data → R2 concentration/
│   ├── sync_cache.py      ← R2 → local data/cache/
│   └── daily_update.sh    ← ingest + concentration + sync (manual/cron)
├── frontend/
│   ├── index.html         ← screener UI
│   ├── watchlist.html     ← watchlist manager
│   └── kline_draw.html    ← draw pattern → match against market
├── data/
│   ├── watchlists.json    ← watchlist persistence (on persistent volume)
│   └── cache/             ← local parquet cache (synced from R2)
├── railway.toml           ← Railway build + start config
└── requirements.txt
```

---

## Tool Stack

| Layer | Tool |
|---|---|
| Data source | FinMind API (999 plan) |
| Data storage | Cloudflare R2 (Parquet, per-ticker) |
| Query engine | DuckDB (reads local Parquet cache) |
| Compute | Polars (indicators: MA, BB, RSI, concentration) |
| Backend | FastAPI |
| Scheduler | APScheduler (daily ingest at 19:00 TST) |
| Visualization | Lightweight Charts (TradingView OSS) |
| Deployment | Railway (persistent volume for Parquet cache) |
