import json
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
import polars as pl
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from app.auth import (
    auth_enabled,
    build_google_login_url,
    exchange_code_for_token,
    fetch_google_userinfo,
    get_session_user,
    login_user_from_google,
    verify_oauth_state,
)
from app.config import SESSION_SECRET_KEY, GOOGLE_REDIRECT_URI, WATCHLISTS_PATH
from app.db import init_db
from app.db import (
    add_ticker_to_watchlist,
    create_watchlist_for_user,
    delete_watchlist_for_user,
    get_watchlist_counts_for_user,
    get_watchlist_by_name,
    get_watchlist_items_for_user,
    get_watchlists_for_user,
    clear_watchlists_for_user,
    remove_ticker_from_watchlist,
    set_watchlist_custom_label,
    set_watchlist_item_active,
    set_watchlist_note,
    # portfolio
    get_portfolios_for_user,
    get_portfolio_by_name,
    create_portfolio_for_user,
    delete_portfolio_for_user,
    get_portfolio_positions,
    upsert_portfolio_position,
    delete_portfolio_position,
    add_portfolio_trade,
    get_portfolio_trades,
    upsert_monthly_pnl,
    get_monthly_pnl,
)
from app.pattern_matcher import match_pattern
from app.scheduler import scheduler, startup_sync
from app.backtest import run_backtest, get_kline, get_ticker_summary, get_ticker_names, get_market_overview
from app.stock_universe import load_stock_info


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


def _current_user(request: Request) -> dict | None:
    return get_session_user(request)


def _legacy_watchlist_summary(name: str) -> dict | None:
    wl = _load_watchlists()
    if name not in wl:
        return None
    entry = wl[name] if isinstance(wl[name], dict) else {
        "tickers": wl[name],
        "custom_label": "備註",
        "custom": {},
        "active": {},
    }
    return {
        "tickers": entry.get("tickers", []),
        "custom_label": entry.get("custom_label", "備註"),
        "custom_data": entry.get("custom", {}),
        "active_data": entry.get("active", {}),
    }


def _build_watchlist_summary(tickers: list[str], custom_label: str, custom_data: dict, active_data: dict) -> dict:
    if not tickers:
        return {"custom_label": custom_label, "rows": []}

    summary_df = get_ticker_summary(tickers)
    names_df = get_ticker_names(tickers)

    if not summary_df.is_empty() and not names_df.is_empty():
        summary_df = summary_df.join(names_df, on="ticker", how="left")
    elif not summary_df.is_empty():
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

    order = {t: i for i, t in enumerate(tickers)}
    rows.sort(key=lambda r: order.get(r["ticker"], 9999))
    return {"custom_label": custom_label, "rows": rows}


def _import_legacy_watchlists_to_user(user_id: int) -> None:
    if get_watchlists_for_user(user_id):
        return
    legacy = _load_watchlists()
    if not legacy:
        return
    for name, entry in legacy.items():
        tickers = _tickers(entry)
        custom_label = "備註"
        custom_data = {}
        active_data = {}
        if isinstance(entry, dict):
            custom_label = entry.get("custom_label", "備註")
            custom_data = entry.get("custom", {})
            active_data = entry.get("active", {})
        create_watchlist_for_user(user_id, name)
        set_watchlist_custom_label(user_id, name, custom_label)
        for ticker in tickers:
            add_ticker_to_watchlist(user_id, name, ticker)
            if ticker in custom_data:
                set_watchlist_note(user_id, name, ticker, custom_data[ticker])
            if ticker in active_data:
                set_watchlist_item_active(user_id, name, ticker, bool(active_data[ticker]))

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    threading.Thread(target=startup_sync, daemon=True).start()
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Quant Web Backtest", version="0.3.0", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    same_site="lax",
    https_only=False,
)


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


@app.get("/api/stock-universe")
def stock_universe():
    df = load_stock_info()
    if df.is_empty():
        return JSONResponse(content=[])
    rows = (
        df.select([
            pl.col("stock_id").alias("ticker"),
            pl.col("stock_name").alias("name"),
        ])
        .sort(pl.col("ticker").cast(pl.Int32))
        .to_dicts()
    )
    return JSONResponse(content=rows)


@app.get("/login")
def login_page(request: Request):
    if _current_user(request):
        return RedirectResponse(url="/")
    return RedirectResponse(url="/static/login.html")


@app.get("/auth/google/login")
def auth_google_login(request: Request, next: str | None = None):
    if not auth_enabled():
        return JSONResponse(
            status_code=400,
            content={
                "detail": "Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET."
            },
        )
    if next:
        request.session["login_next"] = next
    login_url, _state, _redirect_uri = build_google_login_url(request)
    return RedirectResponse(url=login_url)


@app.get("/auth/google/callback", name="auth_google_callback")
def auth_google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    if error:
        return JSONResponse(status_code=400, content={"detail": error})
    if not auth_enabled():
        return JSONResponse(
            status_code=400,
            content={
                "detail": "Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET."
            },
        )
    if not code or not state:
        return JSONResponse(status_code=400, content={"detail": "Missing code or state"})
    if not verify_oauth_state(state):
        return JSONResponse(status_code=400, content={"detail": "Invalid OAuth state"})

    redirect_uri = GOOGLE_REDIRECT_URI or str(request.url_for("auth_google_callback"))
    token = exchange_code_for_token(code=code, redirect_uri=redirect_uri)
    userinfo = fetch_google_userinfo(token["access_token"])
    user_row = login_user_from_google(request, userinfo)
    _import_legacy_watchlists_to_user(user_row["id"])
    next_url = request.session.pop("login_next", "/watchlist-manager")
    return RedirectResponse(url=next_url)


@app.get("/auth/me")
def auth_me(request: Request):
    user = get_session_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "not authenticated"})
    return JSONResponse(content={"user": user})


@app.post("/auth/logout")
def auth_logout(request: Request):
    request.session.clear()
    return JSONResponse(content={"ok": True})


@app.get("/kline/{ticker}")
def kline(
    ticker: str,
    ma_window: int = Query(default=10, ge=5, le=120),
    bb_window: int = Query(default=22, ge=5, le=120),
):
    df = get_kline(ticker=ticker, ma_window=ma_window, bb_window=bb_window)
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



# ── Backtest ──────────────────────────────────────────────────────────────────

@app.post("/backtest/run")
def backtest_run(
    request: Request,
    lookback_days: int = Query(default=365, ge=30, le=730),
    ma_window: int = Query(default=10, ge=5, le=120),
    bb_window: int = Query(default=22, ge=5, le=120),
    volume_ratio: float = Query(default=1.5, ge=1.0, le=10.0),
    price_above_ma: bool = Query(default=True),
    bb_breakout: bool = Query(default=False),
    rsi_period: int = Query(default=14, ge=5, le=50),
    rsi_min: float = Query(default=0.0, ge=0.0, le=100.0),
    rsi_max: float = Query(default=100.0, ge=0.0, le=100.0),
    use_concentration: bool = Query(default=False),
    conc_5d_min: float = Query(default=0.0),
    conc_20d_min: float = Query(default=0.0),
    watchlist: str | None = Query(default=None),
    market_cap_rank: int | None = Query(default=None, ge=1, le=2000),
    exit_price_below_ma: bool = Query(default=True),
    exit_ma_window: int = Query(default=10, ge=5, le=120),
    exit_bb_breakdown: bool = Query(default=False),
    exit_bb_window: int = Query(default=22, ge=5, le=120),
    exit_use_rsi: bool = Query(default=False),
    exit_rsi_period: int = Query(default=14, ge=5, le=50),
    exit_rsi_min: float = Query(default=0.0, ge=0.0, le=100.0),
    exit_rsi_max: float = Query(default=75.0, ge=0.0, le=100.0),
    exit_use_volume: bool = Query(default=False),
    exit_volume_ratio: float = Query(default=1.5, ge=1.0, le=10.0),
    exit_use_concentration: bool = Query(default=False),
    exit_conc_5d_min: float = Query(default=0.0),
    exit_conc_min: float = Query(default=0.0),
    commission: float = Query(default=0.001425, ge=0.0, le=0.1),
    debug: bool = Query(default=False),
):
    tickers = None
    if watchlist:
        user = _current_user(request)
        if user:
            items = get_watchlist_items_for_user(user["id"], watchlist)
            tickers = [item["ticker"] for item in items]
        else:
            wl = _load_watchlists()
            entry = wl.get(watchlist)
            tickers = _tickers(entry) if entry is not None else []
    result = run_backtest(
        lookback_days=lookback_days,
        ma_window=ma_window,
        bb_window=bb_window,
        volume_ratio=volume_ratio,
        price_above_ma=price_above_ma,
        bb_breakout=bb_breakout,
        rsi_period=rsi_period,
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        use_concentration=use_concentration,
        conc_5d_min=conc_5d_min,
        conc_20d_min=conc_20d_min,
        tickers=tickers,
        market_cap_rank=market_cap_rank,
        exit_price_below_ma=exit_price_below_ma,
        exit_ma_window=exit_ma_window,
        exit_bb_breakdown=exit_bb_breakdown,
        exit_bb_window=exit_bb_window,
        exit_use_rsi=exit_use_rsi,
        exit_rsi_period=exit_rsi_period,
        exit_rsi_min=exit_rsi_min,
        exit_rsi_max=exit_rsi_max,
        exit_use_volume=exit_use_volume,
        exit_volume_ratio=exit_volume_ratio,
        exit_use_concentration=exit_use_concentration,
        exit_conc_5d_min=exit_conc_5d_min,
        exit_conc_min=exit_conc_min,
        commission=commission,
        debug=debug,
    )
    return JSONResponse(content=result)


# ── Watchlist CRUD ────────────────────────────────────────────────────────────

class CustomLabelRequest(BaseModel):
    label: str

class CustomValueRequest(BaseModel):
    value: str

class ActiveRequest(BaseModel):
    active: bool


@app.get("/watchlists")
def list_watchlists(request: Request):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "login required"})
    return JSONResponse(content=get_watchlist_counts_for_user(user["id"]))


@app.get("/watchlists/{name}")
def get_watchlist(request: Request, name: str):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "login required"})
    watchlist = get_watchlist_by_name(user["id"], name)
    if watchlist is None:
        return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
    items = get_watchlist_items_for_user(user["id"], name)
    return JSONResponse(content=[item["ticker"] for item in items])


@app.post("/watchlists/{name}")
def create_watchlist(request: Request, name: str):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "login required"})
    create_watchlist_for_user(user["id"], name)
    return JSONResponse(content={"ok": True})


@app.delete("/watchlists/{name}")
def delete_watchlist(request: Request, name: str):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "login required"})
    delete_watchlist_for_user(user["id"], name)
    return JSONResponse(content={"ok": True})


@app.put("/watchlists/{name}/tickers/{ticker}")
def add_ticker(request: Request, name: str, ticker: str):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "login required"})
    try:
        add_ticker_to_watchlist(user["id"], name, ticker)
    except KeyError:
        return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
    return JSONResponse(content={"ok": True})


@app.delete("/watchlists/{name}/tickers/{ticker}")
def remove_ticker(request: Request, name: str, ticker: str):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "login required"})
    remove_ticker_from_watchlist(user["id"], name, ticker)
    return JSONResponse(content={"ok": True})


@app.get("/watchlists/{name}/summary")
def watchlist_summary(request: Request, name: str):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "login required"})
    watchlist = get_watchlist_by_name(user["id"], name)
    if watchlist is None:
        return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
    items = get_watchlist_items_for_user(user["id"], name)
    tickers = [item["ticker"] for item in items]
    custom_data = {item["ticker"]: item.get("note", "") for item in items}
    active_data = {item["ticker"]: bool(item.get("active", 1)) for item in items}
    return JSONResponse(
        content=_build_watchlist_summary(
            tickers=tickers,
            custom_label=watchlist.get("custom_label", "備註"),
            custom_data=custom_data,
            active_data=active_data,
        )
    )


@app.put("/watchlists/{name}/custom_label")
def update_custom_label(request: Request, name: str, req: CustomLabelRequest):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "login required"})
    if get_watchlist_by_name(user["id"], name) is None:
        return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
    set_watchlist_custom_label(user["id"], name, req.label)
    return JSONResponse(content={"ok": True})


@app.put("/watchlists/{name}/custom/{ticker}")
def update_custom_value(request: Request, name: str, ticker: str, req: CustomValueRequest):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "login required"})
    if get_watchlist_by_name(user["id"], name) is None:
        return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
    set_watchlist_note(user["id"], name, ticker, req.value)
    return JSONResponse(content={"ok": True})


@app.put("/watchlists/{name}/active/{ticker}")
def set_ticker_active(request: Request, name: str, ticker: str, req: ActiveRequest):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "login required"})
    if get_watchlist_by_name(user["id"], name) is None:
        return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
    set_watchlist_item_active(user["id"], name, ticker, req.active)
    return JSONResponse(content={"ok": True})


# ── Portfolio ─────────────────────────────────────────────────────────────────

class PositionIn(BaseModel):
    ticker: str
    name: str = ""
    shares: float = 0
    lots: float = 0
    avg_cost: float = 0
    currency: str = "TWD"
    market: str = "TW"
    price_override: float | None = None
    entry_date: str = ""
    entry_reason: str = ""
    margin_lots: float = 0
    futures_lots: float = 0
    note: str = ""


class TradeIn(BaseModel):
    ticker: str
    name: str = ""
    action: str  # 'buy' | 'sell'
    shares: float
    price: float
    currency: str = "TWD"
    commission: float = 0
    trade_date: str
    entry_reason: str = ""
    exit_reason: str = ""
    realized_pnl: float = 0
    note: str = ""


class MonthlyPnlIn(BaseModel):
    unrealized_tw: float = 0
    realized_tw: float = 0
    dividend_tw: float = 0
    unrealized_us_usd: float = 0
    realized_us_usd: float = 0
    usd_rate: float = 31.0
    unrealized_futures: float = 0
    realized_futures: float = 0
    algo_pnl: float = 0
    fee_rebate: float = 0
    total_pnl: float = 0
    nav: float = 0
    note: str = ""


def _get_last_close(ticker: str) -> float | None:
    """Return the most recent close price from local parquet cache."""
    from app.backtest import CACHE_DIR
    path = CACHE_DIR / "tickers" / f"{ticker}.parquet"
    if not path.exists():
        return None
    try:
        df = pl.read_parquet(path)
        if df.is_empty() or "close" not in df.columns:
            return None
        return float(df.sort("date").tail(1)["close"][0])
    except Exception:
        return None


@app.get("/api/portfolios")
def list_portfolios(request: Request):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    return JSONResponse(content=get_portfolios_for_user(user["id"]))


@app.post("/api/portfolios/{name}")
def create_portfolio(request: Request, name: str):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    portfolio = create_portfolio_for_user(user["id"], name)
    return JSONResponse(content=portfolio)


@app.delete("/api/portfolios/{name}")
def delete_portfolio(request: Request, name: str):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    delete_portfolio_for_user(user["id"], name)
    return JSONResponse(content={"ok": True})


@app.get("/api/portfolios/{name}/positions")
def get_positions(request: Request, name: str, usd_rate: float = Query(default=31.0)):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    portfolio = get_portfolio_by_name(user["id"], name)
    if portfolio is None:
        return JSONResponse(status_code=404, content={"detail": "Portfolio not found"})

    positions = get_portfolio_positions(portfolio["id"])
    total_value_twd = 0.0

    enriched = []
    for pos in positions:
        p = dict(pos)
        if p["market"] == "TW":
            live = _get_last_close(p["ticker"])
            p["current_price"] = live if live is not None else p["avg_cost"]
            p["market_value_twd"] = p["current_price"] * p["shares"]
        else:
            price_usd = p["price_override"] if p["price_override"] is not None else p["avg_cost"]
            p["current_price"] = price_usd
            p["market_value_twd"] = price_usd * p["shares"] * usd_rate
        cost_twd = p["avg_cost"] * p["shares"] * (usd_rate if p["currency"] == "USD" else 1)
        p["unrealized_pnl"] = p["market_value_twd"] - cost_twd
        p["unrealized_pct"] = (p["unrealized_pnl"] / cost_twd * 100) if cost_twd else 0
        total_value_twd += p["market_value_twd"]
        enriched.append(p)

    for p in enriched:
        p["position_weight"] = (p["market_value_twd"] / total_value_twd * 100) if total_value_twd else 0

    return JSONResponse(content={"positions": enriched, "total_value_twd": total_value_twd})


@app.post("/api/portfolios/{name}/positions")
def add_position(request: Request, name: str, body: PositionIn):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    portfolio = get_portfolio_by_name(user["id"], name)
    if portfolio is None:
        return JSONResponse(status_code=404, content={"detail": "Portfolio not found"})
    upsert_portfolio_position(portfolio["id"], **body.model_dump())
    return JSONResponse(content={"ok": True})


@app.delete("/api/portfolios/{name}/positions/{ticker}")
def remove_position(request: Request, name: str, ticker: str):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    portfolio = get_portfolio_by_name(user["id"], name)
    if portfolio is None:
        return JSONResponse(status_code=404, content={"detail": "Portfolio not found"})
    delete_portfolio_position(portfolio["id"], ticker)
    return JSONResponse(content={"ok": True})


@app.get("/api/portfolios/{name}/trades")
def list_trades(request: Request, name: str):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    portfolio = get_portfolio_by_name(user["id"], name)
    if portfolio is None:
        return JSONResponse(status_code=404, content={"detail": "Portfolio not found"})
    return JSONResponse(content=get_portfolio_trades(user["id"], portfolio["id"]))


@app.post("/api/portfolios/{name}/trades")
def log_trade(request: Request, name: str, body: TradeIn):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    portfolio = get_portfolio_by_name(user["id"], name)
    if portfolio is None:
        return JSONResponse(status_code=404, content={"detail": "Portfolio not found"})
    trade_id = add_portfolio_trade(user["id"], portfolio["id"], **body.model_dump())
    return JSONResponse(content={"id": trade_id})


@app.get("/api/portfolio/monthly_pnl")
def list_monthly_pnl(request: Request):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    return JSONResponse(content=get_monthly_pnl(user["id"]))


@app.put("/api/portfolio/monthly_pnl/{year_month}")
def update_monthly_pnl(request: Request, year_month: str, body: MonthlyPnlIn):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    upsert_monthly_pnl(user["id"], year_month, **body.model_dump())
    return JSONResponse(content={"ok": True})


# ── Taiwan Weighted Total Return Index (annual) ───────────────────────────────
import re as _re
import requests as _requests

_TWSE_INDEX_CACHE: dict = {}  # {year: {"idx": float, "idx_rate": float}}
_TWSE_CACHE_TS: float = 0.0


def _fetch_twse_index_close(query_date: date) -> float | None:
    """Return 發行量加權股價報酬指數 close for the given date, or None if no data."""
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = _requests.get(url, params={"response": "json", "date": query_date.strftime("%Y%m%d"), "type": "ALL"}, headers=headers, timeout=10)
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    for table in data.get("tables", []):
        if not table or not table.get("data"):
            continue
        for row in table["data"]:
            if row and "發行量加權股價報酬指數" in str(row[0]):
                try:
                    return float(str(row[1]).replace(",", ""))
                except (ValueError, IndexError):
                    pass
    return None


def _last_trading_day_close(year: int, month: int = 12) -> float | None:
    """Walk backwards from month-end to find the last trading day and return index close."""
    if month == 12:
        d = date(year, 12, 31)
    else:
        # First day of next month minus 1
        d = date(year, month + 1, 1) - timedelta(days=1)
    for _ in range(10):
        val = _fetch_twse_index_close(d)
        if val is not None:
            return val
        d -= timedelta(days=1)
        time.sleep(0.1)
    return None


def _build_annual_index_data() -> list[dict]:
    """Fetch year-end closes from TWSE and compute annual returns."""
    today = date.today()
    current_year = today.year
    start_year = 2021  # need prev-year close to compute 2022 return

    closes: dict[int, float] = {}
    for y in range(start_year, current_year + 1):
        if y == current_year:
            # use latest available: previous month-end (or earlier if partial year)
            month = today.month - 1 if today.month > 1 else 12
            ref_year = y if today.month > 1 else y - 1
            val = _last_trading_day_close(ref_year, month)
        else:
            val = _last_trading_day_close(y)
        if val is not None:
            closes[y] = val
        time.sleep(0.2)

    result = []
    for y in range(2022, current_year + 1):
        if y not in closes or (y - 1) not in closes:
            continue
        tw_year = y - 1911
        idx_rate = (closes[y] - closes[y - 1]) / closes[y - 1]
        result.append({
            "year": f"{y}({tw_year})",
            "idx": round(closes[y], 0),
            "idx_rate": round(idx_rate, 4),
        })
    return result


@app.get("/api/index_annual")
def index_annual():
    global _TWSE_INDEX_CACHE, _TWSE_CACHE_TS
    # Cache for 6 hours
    if _TWSE_INDEX_CACHE and (time.time() - _TWSE_CACHE_TS) < 21600:
        return JSONResponse(content=_TWSE_INDEX_CACHE)
    data = _build_annual_index_data()
    if data:
        _TWSE_INDEX_CACHE = data
        _TWSE_CACHE_TS = time.time()
    return JSONResponse(content=data)


# ── Static files (must be after all route definitions) ────────────────────────
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def root():
    return RedirectResponse(url="/static/index.html")


@app.get("/draw")
def draw():
    return RedirectResponse(url="/static/kline_draw.html")


@app.get("/backtest")
def backtest_page():
    return RedirectResponse(url="/static/screener.html")


@app.get("/watchlist-manager")
def watchlist_manager(request: Request):
    if not _current_user(request):
        return RedirectResponse(url="/login?next=/watchlist-manager")
    return RedirectResponse(url="/static/watchlist.html")


@app.get("/market-overview")
def market_overview_page():
    return RedirectResponse(url="/static/market_overview.html")


@app.get("/portfolio")
def portfolio_page(request: Request):
    if not _current_user(request):
        return RedirectResponse(url="/login?next=/portfolio")
    return RedirectResponse(url="/static/portfolio.html")


@app.get("/api/market-overview")
def api_market_overview():
    rows = get_market_overview()
    return JSONResponse(content=rows)


def _to_float(value):
    try:
        if value is None:
            return None
        num = float(value)
        return num if num == num else None
    except Exception:
        return None


def _pick_home_row(row: dict) -> dict:
    return {
        "ticker": row.get("ticker"),
        "name": row.get("name") or "",
        "close": row.get("close"),
        "day_chg": row.get("day_chg"),
        "day_pct": row.get("day_pct"),
        "volume": row.get("volume"),
    }


@app.get("/api/home-summary")
def api_home_summary():
    rows = get_market_overview()
    if not rows:
        return JSONResponse(content={
            "latest_date": None,
            "total_rows": 0,
            "top_gainers": [],
            "top_losers": [],
            "top_volume": [],
        })

    dated_rows = [r for r in rows if r.get("date")]
    latest_date = max((str(r["date"]) for r in dated_rows), default=None)

    ranked = [
        {**row, "_day_pct": _to_float(row.get("day_pct")), "_volume": _to_float(row.get("volume"))}
        for row in rows
    ]

    gainers = [
        _pick_home_row(row)
        for row in sorted(
            [r for r in ranked if r["_day_pct"] is not None],
            key=lambda r: r["_day_pct"],
            reverse=True,
        )[:5]
    ]
    losers = [
        _pick_home_row(row)
        for row in sorted(
            [r for r in ranked if r["_day_pct"] is not None],
            key=lambda r: r["_day_pct"],
        )[:5]
    ]
    volumes = [
        _pick_home_row(row)
        for row in sorted(
            [r for r in ranked if r["_volume"] is not None and not str(r.get("ticker", "")).startswith("0")],
            key=lambda r: r["_volume"],
            reverse=True,
        )[:5]
    ]

    return JSONResponse(content={
        "latest_date": latest_date,
        "total_rows": len(rows),
        "top_gainers": gainers,
        "top_losers": losers,
        "top_volume": volumes,
    })
