# quant_web_screener

Taiwan equity screener — reads local CSV data via DuckDB, computes indicators with Polars, serves via FastAPI + plain HTML frontend.

---

## Screener Filters (`/screen`)

| Condition | Description | Default |
|---|---|---|
| Close > MA(N) | Price above moving average | on |
| Bollinger Upper Breakout | Close > MA + 2σ | off |
| Volume Ratio ≥ X | Today's vol / MA vol | 1.5x |
| RSI range | RSI within [min, max] | 0–100 |
| Concentration 20d ≥ N | 籌碼集中度 20-day | off |

Output columns: `ticker, date, close, open, high, low, volume, ma, bb_upper, bb_lower, vol_ratio, rsi, concentration_20d`

---

## Watchlist Manager (`/watchlist-manager`)

- Create/rename/delete named watchlists (e.g. 晶圓, 連接器, 散熱)
- Per-watchlist summary table: 代號, 名稱, 收盤, 日%, 5d%, 10d%, 20d%, 備註 (custom column)
- **Overview panel** (default): table of all watchlists with average 5d/10d/20d% (active tickers only), clickable rows to open watchlist
- **全部 view**: all tickers across all watchlists, grouped by watchlist with section headers; inactive tickers are hidden
- **Per-ticker active toggle** (●/○): toggles whether a ticker counts toward averages and appears in 全部 view
- Custom column label (editable per watchlist)
- Add/remove tickers; drag-to-reorder preserved via JSON order

Watchlist data stored in `data/watchlists.json`:
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
`active` only stores `false` entries — absence means active (compact).

---

## Architecture

```
quant_web_screener/
├── app/
│   ├── config.py          ← path config (points to Trading/history_data/tw)
│   ├── screener.py        ← DuckDB reads CSV → Polars computes indicators → filter
│   ├── api.py             ← FastAPI: /screen, /kline, /pattern_match, /watchlists/*
│   └── pattern_matcher.py ← DTW pattern matching
├── frontend/
│   ├── index.html         ← screener UI
│   ├── watchlist.html     ← watchlist manager
│   └── kline_draw.html    ← draw pattern → match against market
├── data/
│   ├── screener.duckdb    ← DuckDB connection file (no data stored)
│   └── watchlists.json    ← watchlist persistence
├── scripts/
│   └── run_dev.sh
└── requirements.txt
```

### Data flow (current — local dev)

```
CSV files (Trading/history_data/tw/)
    │
    ▼ DuckDB read_csv_auto (glob, no data copy)
    │  → last N rows per ticker
    │
    ▼ Polars: MA, Bollinger, Volume Ratio, RSI
    │
    ▼ Filter + sort (vol_ratio desc)
    │
    ▼ FastAPI → browser
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

Static pages: `/` → screener, `/draw` → kline draw, `/watchlist-manager` → watchlist manager

---

## Start

```bash
cd /Users/yanyifu/Documents/_Coding/quant_web_screener
bash scripts/run_dev.sh
# FastAPI: http://localhost:8000
# API docs: http://localhost:8000/docs
```

---

## Tool Stack

| Layer | Tool | Reason |
|---|---|---|
| Data source | FinMind API (999 plan) | Existing subscription — covers OHLCV, 三大法人, 籌碼集中度 |
| Backend | FastAPI | Async-native, better perf than Flask |
| Compute | Polars | Faster than Pandas for large-scale filtering |
| Visualization | Lightweight Charts | TradingView OSS, best-in-class candlestick perf, supports signal markers |
| Storage (local) | DuckDB | Columnar, queries CSVs in-place, native Polars integration |
| Storage (cloud target) | Supabase PostgreSQL | See cloud migration plan below |
| Deployment | Docker | Dev/prod parity, easy cloud migration |

---

## Cloud Migration Plan

> **Goal**: migrate from local-CSV dev setup to a hosted public website.
> All new feature decisions should be made with this target architecture in mind.

### Target architecture

```
FinMind API (999 plan)
    ↓  daily ingest job at 15:30 TST (n8n or GitHub Actions)
Supabase PostgreSQL
  ├── ohlcv            ← daily OHLCV, all tickers
  ├── trader_info      ← 三大法人
  ├── concentration    ← 籌碼集中度
  ├── ticker_info      ← stock names / sector
  ├── screener_cache   ← pre-computed indicators (nightly batch)
  └── watchlists       ← migrate from watchlists.json
    ↓
FastAPI on Railway / Fly.io
    ↓
HTML/JS frontend (served from same server or Cloudflare Pages)
```

### Key schema decisions

```sql
-- Primary time-series table
CREATE TABLE ohlcv (
    ticker  TEXT        NOT NULL,
    date    DATE        NOT NULL,
    open    NUMERIC(10,2),
    high    NUMERIC(10,2),
    low     NUMERIC(10,2),
    close   NUMERIC(10,2),
    volume  BIGINT,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX ON ohlcv (ticker, date DESC);

-- Pre-computed screener results (nightly job writes here)
-- /screen reads this table instead of computing on the fly
CREATE TABLE screener_cache (
    date        DATE        NOT NULL,
    ticker      TEXT        NOT NULL,
    close       NUMERIC(10,2),
    ma_20       NUMERIC(10,2),
    bb_upper    NUMERIC(10,2),
    bb_lower    NUMERIC(10,2),
    vol_ratio   NUMERIC(6,2),
    rsi_14      NUMERIC(5,1),
    conc_20d    NUMERIC(5,2),
    PRIMARY KEY (date, ticker)
);
```

### Why pre-compute (screener_cache)

Current `/screen` scans 456 CSVs and computes indicators on every request → slow.
Correct pattern: nightly batch job computes all indicators for all tickers → writes to `screener_cache`.
API queries: `SELECT * FROM screener_cache WHERE date = today AND vol_ratio >= ? AND ...` → <50ms.
K-line endpoint still queries `ohlcv` directly (single ticker, fast).

### Migration steps (in order)

1. Build Supabase schema + one-time import of existing CSVs via DuckDB COPY
2. Rewrite `screener.py` to query `screener_cache` (PostgreSQL) instead of CSV glob
3. Build FinMind ingest job (n8n): daily OHLCV + 三大法人 upsert → Supabase
4. Build nightly indicator batch job → writes `screener_cache`
5. Dockerize FastAPI, deploy to Railway; set `DATABASE_URL` env var
6. Migrate `watchlists.json` → Supabase `watchlists` table
7. Frontend: point API base URL at production domain

### What NOT to do (past decisions)

- Do NOT scan CSV glob on every API request — pre-compute instead
- Do NOT use DuckDB as the production database — it's a local-dev tool here
- Do NOT store watchlists as JSON in production — move to DB for multi-user safety
- Do NOT deploy without Docker — local path assumptions will break

---

## Roadmap

- [ ] FinMind ingest job (n8n: daily OHLCV → Supabase)
- [ ] Nightly screener_cache batch job
- [ ] Rewrite screener.py to query PostgreSQL
- [ ] Docker + Railway deployment
- [ ] Migrate watchlists.json → Supabase table
- [x] Watchlist return ranking: 5d/10d/20d leaderboard across all lists
- [ ] 三大法人 filter in screener
- [ ] Background scheduler: auto-refresh after market close
