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

### Data flow

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
| Data source | FinMind API | Existing subscription — no migration cost |
| Backend | FastAPI | Async-native, better perf than Flask |
| Compute | Polars | Faster than Pandas for large-scale filtering |
| Visualization | Lightweight Charts | TradingView OSS, best-in-class candlestick perf, supports signal markers |
| Storage | DuckDB | Columnar, queries CSVs in-place, native Polars integration |
| Deployment | Docker | Dev/prod parity, easy cloud migration |

---

## Roadmap

- [ ] FinMind API client wrapper with LRU cache (avoid redundant pulls)
- [x] Watchlist return ranking: 5d/10d/20d leaderboard across all lists (user-named, e.g. 航運、電子)
- [ ] Background scheduler: auto-refresh after market close
- [ ] Cloud deployment (Docker)
