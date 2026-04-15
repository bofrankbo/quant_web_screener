import json
import polars as pl
from pathlib import Path
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from app.screener import screen_stocks, get_kline, get_ticker_summary, get_ticker_names
from app.pattern_matcher import match_pattern

WATCHLISTS_PATH = Path(__file__).parent.parent / "data" / "watchlists.json"


def _load_watchlists() -> dict:
    if not WATCHLISTS_PATH.exists():
        return {}
    data = json.loads(WATCHLISTS_PATH.read_text(encoding="utf-8"))
    # Migrate old list-only format → new dict format
    migrated = False
    for k, v in data.items():
        if isinstance(v, list):
            data[k] = {"tickers": v, "custom_label": "備註", "custom": {}}
            migrated = True
    if migrated:
        _save_watchlists(data)
    return data


def _save_watchlists(data: dict) -> None:
    WATCHLISTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCHLISTS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _tickers(entry: dict | list) -> list[str]:
    """Extract ticker list from a watchlist entry (handles both old and new format)."""
    if isinstance(entry, list):
        return entry
    return entry.get("tickers", [])

app = FastAPI(title="Quant Web Screener", version="0.3.0")


class Candle(BaseModel):
    open: float
    close: float
    high: float
    low: float
    volume: float = 0.0


class PatternMatchRequest(BaseModel):
    candles: list[Candle]
    window_min: int = 5
    window_max: int = 30
    use_volume: bool = False
    top_n: int = 30


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/screen")
def screen(
    ma_window: int = Query(default=20, ge=5, le=120),
    volume_ratio: float = Query(default=1.5, ge=1.0, le=10.0),
    price_above_ma: bool = Query(default=True),
    bb_breakout: bool = Query(default=False),
    rsi_period: int = Query(default=14, ge=5, le=50),
    rsi_min: float = Query(default=0.0, ge=0.0, le=100.0),
    rsi_max: float = Query(default=100.0, ge=0.0, le=100.0),
    use_concentration: bool = Query(default=False),
    conc_min: float = Query(default=0.0),
    top_n: int = Query(default=50, ge=1, le=200),
    watchlist: str | None = Query(default=None),
):
    tickers = None
    if watchlist:
        wl = _load_watchlists()
        entry = wl.get(watchlist)
        tickers = _tickers(entry) if entry is not None else []
    df = screen_stocks(
        ma_window=ma_window,
        volume_ratio=volume_ratio,
        price_above_ma=price_above_ma,
        bb_breakout=bb_breakout,
        rsi_period=rsi_period,
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        use_concentration=use_concentration,
        conc_min=conc_min,
        top_n=top_n,
        tickers=tickers,
    )
    return JSONResponse(content=df.to_dicts())


@app.get("/kline/{ticker}")
def kline(
    ticker: str,
    lookback: int = Query(default=120, ge=20, le=500),
    ma_window: int = Query(default=20, ge=5, le=120),
):
    df = get_kline(ticker=ticker, lookback=lookback, ma_window=ma_window)
    if df.is_empty():
        return JSONResponse(status_code=404, content={"detail": f"{ticker} not found"})
    return JSONResponse(content=df.with_columns(pl.col("date").cast(pl.Utf8)).to_dicts())


@app.post("/pattern_match")
def pattern_match(req: PatternMatchRequest):
    df = match_pattern(
        drawn_candles=[c.model_dump() for c in req.candles],
        window_min=req.window_min,
        window_max=req.window_max,
        use_volume=req.use_volume,
        top_n=req.top_n,
    )
    return JSONResponse(content=df.to_dicts())


# ── Watchlist CRUD ────────────────────────────────────────────────────────────

class CustomLabelRequest(BaseModel):
    label: str

class CustomValueRequest(BaseModel):
    value: str

class ActiveRequest(BaseModel):
    active: bool


@app.get("/watchlists")
def list_watchlists():
    wl = _load_watchlists()
    return JSONResponse(content={k: len(_tickers(v)) for k, v in wl.items()})


@app.get("/watchlists/{name}")
def get_watchlist(name: str):
    wl = _load_watchlists()
    if name not in wl:
        return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
    return JSONResponse(content=_tickers(wl[name]))


@app.post("/watchlists/{name}")
def create_watchlist(name: str):
    wl = _load_watchlists()
    if name not in wl:
        wl[name] = {"tickers": [], "custom_label": "備註", "custom": {}}
        _save_watchlists(wl)
    return JSONResponse(content={"ok": True})


@app.delete("/watchlists/{name}")
def delete_watchlist(name: str):
    wl = _load_watchlists()
    wl.pop(name, None)
    _save_watchlists(wl)
    return JSONResponse(content={"ok": True})


@app.put("/watchlists/{name}/tickers/{ticker}")
def add_ticker(name: str, ticker: str):
    wl = _load_watchlists()
    if name not in wl:
        return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
    t_list = _tickers(wl[name])
    if ticker not in t_list:
        t_list.append(ticker)
        if isinstance(wl[name], dict):
            wl[name]["tickers"] = t_list
        else:
            wl[name] = {"tickers": t_list, "custom_label": "備註", "custom": {}}
        _save_watchlists(wl)
    return JSONResponse(content={"ok": True})


@app.delete("/watchlists/{name}/tickers/{ticker}")
def remove_ticker(name: str, ticker: str):
    wl = _load_watchlists()
    if name in wl:
        t_list = _tickers(wl[name])
        if ticker in t_list:
            t_list.remove(ticker)
            if isinstance(wl[name], dict):
                wl[name]["tickers"] = t_list
            else:
                wl[name] = {"tickers": t_list, "custom_label": "備註", "custom": {}}
            _save_watchlists(wl)
    return JSONResponse(content={"ok": True})


@app.get("/watchlists/{name}/summary")
def watchlist_summary(name: str):
    wl = _load_watchlists()
    if name not in wl:
        return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
    entry = wl[name] if isinstance(wl[name], dict) else {"tickers": wl[name], "custom_label": "備註", "custom": {}}
    tickers = entry.get("tickers", [])
    custom_label = entry.get("custom_label", "備註")
    custom_data = entry.get("custom", {})
    active_data = entry.get("active", {})  # {ticker: False} — absence means active

    if not tickers:
        return JSONResponse(content={"custom_label": custom_label, "rows": []})

    summary_df = get_ticker_summary(tickers)
    names_df = get_ticker_names(tickers)

    if not summary_df.is_empty() and not names_df.is_empty():
        summary_df = summary_df.join(names_df, on="ticker", how="left")
    elif not summary_df.is_empty():
        import polars as pl
        summary_df = summary_df.with_columns(pl.lit(None).cast(pl.Utf8).alias("name"))

    rows = []
    for row in summary_df.to_dicts():
        t = row["ticker"]
        rows.append({
            "ticker": t,
            "stock_name": row.get("name") or "",
            "close": row.get("close"),
            "day_pct": row.get("day_pct"),
            "pct_5d": row.get("pct_5d"),
            "pct_10d": row.get("pct_10d"),
            "pct_20d": row.get("pct_20d"),
            "custom": custom_data.get(t, ""),
            "active": active_data.get(t, True),
        })

    # Preserve watchlist order
    order = {t: i for i, t in enumerate(tickers)}
    rows.sort(key=lambda r: order.get(r["ticker"], 9999))

    return JSONResponse(content={"custom_label": custom_label, "rows": rows})


@app.put("/watchlists/{name}/custom_label")
def update_custom_label(name: str, req: CustomLabelRequest):
    wl = _load_watchlists()
    if name not in wl:
        return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
    if isinstance(wl[name], dict):
        wl[name]["custom_label"] = req.label
    else:
        wl[name] = {"tickers": wl[name], "custom_label": req.label, "custom": {}}
    _save_watchlists(wl)
    return JSONResponse(content={"ok": True})


@app.put("/watchlists/{name}/custom/{ticker}")
def update_custom_value(name: str, ticker: str, req: CustomValueRequest):
    wl = _load_watchlists()
    if name not in wl:
        return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
    if isinstance(wl[name], dict):
        wl[name].setdefault("custom", {})[ticker] = req.value
    else:
        wl[name] = {"tickers": wl[name], "custom_label": "備註", "custom": {ticker: req.value}}
    _save_watchlists(wl)
    return JSONResponse(content={"ok": True})


@app.put("/watchlists/{name}/active/{ticker}")
def set_ticker_active(name: str, ticker: str, req: ActiveRequest):
    wl = _load_watchlists()
    if name not in wl:
        return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
    entry = wl[name]
    if isinstance(entry, dict):
        entry.setdefault("active", {})
        if req.active:
            entry["active"].pop(ticker, None)   # absence = active, save space
        else:
            entry["active"][ticker] = False
    else:
        active = {} if req.active else {ticker: False}
        wl[name] = {"tickers": entry, "custom_label": "備註", "custom": {}, "active": active}
    _save_watchlists(wl)
    return JSONResponse(content={"ok": True})


# ── Static files (must be after all route definitions) ────────────────────────
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def root():
    return RedirectResponse(url="/static/index.html")


@app.get("/draw")
def draw():
    return RedirectResponse(url="/static/kline_draw.html")


@app.get("/watchlist-manager")
def watchlist_manager():
    return RedirectResponse(url="/static/watchlist.html")
