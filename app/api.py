import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
import polars as pl
from fastapi import FastAPI, Query, Request, UploadFile, File
from fastapi.responses import JSONResponse, RedirectResponse, Response
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
)
from app.pattern_matcher import match_pattern
from app.scheduler import scheduler, startup_sync
from app.backtest import run_backtest, get_kline, get_ticker_summary, get_ticker_names, get_market_overview


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


def _export_watchlists_payload(user_id: int) -> dict:
    watchlists = get_watchlists_for_user(user_id)
    payload: dict[str, dict] = {}
    for wl in watchlists:
        items = get_watchlist_items_for_user(user_id, wl["name"])
        payload[wl["name"]] = {
            "tickers": [item["ticker"] for item in items],
            "custom_label": wl.get("custom_label", "備註"),
            "custom": {item["ticker"]: item.get("note", "") for item in items if item.get("note")},
            "active": {item["ticker"]: False for item in items if not bool(item.get("active", 1))},
        }
    return payload


def _import_watchlists_payload(user_id: int, payload: dict, replace: bool = True) -> int:
    if replace:
        clear_watchlists_for_user(user_id)
    count = 0
    for name, entry in payload.items():
        if not isinstance(entry, dict):
            entry = {"tickers": entry}
        tickers = entry.get("tickers", []) or []
        custom_label = entry.get("custom_label", "備註")
        custom_data = entry.get("custom", {}) or {}
        active_data = entry.get("active", {}) or {}
        create_watchlist_for_user(user_id, name)
        set_watchlist_custom_label(user_id, name, custom_label)
        for ticker in tickers:
            add_ticker_to_watchlist(user_id, name, ticker)
            if ticker in custom_data:
                set_watchlist_note(user_id, name, ticker, custom_data[ticker])
            if ticker in active_data:
                set_watchlist_item_active(user_id, name, ticker, bool(active_data[ticker]))
        count += 1
    return count


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


@app.get("/auth/google/login")
def auth_google_login(request: Request):
    if not auth_enabled():
        return JSONResponse(
            status_code=400,
            content={
                "detail": "Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET."
            },
    )
    login_url, state, redirect_uri = build_google_login_url(request)
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
    return RedirectResponse(url="/watchlist-manager")


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


@app.get("/watchlists/export")
def export_watchlists(request: Request):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "not authenticated"})
    payload = _export_watchlists_payload(user["id"])
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="watchlists-export.json"'},
    )


@app.post("/watchlists/import")
async def import_watchlists(
    request: Request,
    file: UploadFile = File(...),
    replace: bool = Query(default=True),
):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "not authenticated"})
    raw = await file.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON file"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"detail": "JSON root must be an object"})
    imported = _import_watchlists_payload(user["id"], payload, replace=replace)
    return JSONResponse(content={"ok": True, "imported": imported})


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
    if user:
        return JSONResponse(content=get_watchlist_counts_for_user(user["id"]))
    wl = _load_watchlists()
    return JSONResponse(content={k: len(_tickers(v)) for k, v in wl.items()})


@app.get("/watchlists/{name}")
def get_watchlist(request: Request, name: str):
    user = _current_user(request)
    if user:
        watchlist = get_watchlist_by_name(user["id"], name)
        if watchlist is None:
            return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
        items = get_watchlist_items_for_user(user["id"], name)
        return JSONResponse(content=[item["ticker"] for item in items])
    wl = _load_watchlists()
    if name not in wl:
        return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
    return JSONResponse(content=_tickers(wl[name]))


@app.post("/watchlists/{name}")
def create_watchlist(request: Request, name: str):
    user = _current_user(request)
    if user:
        create_watchlist_for_user(user["id"], name)
        return JSONResponse(content={"ok": True})
    wl = _load_watchlists()
    if name not in wl:
        wl[name] = {"tickers": [], "custom_label": "備註", "custom": {}}
        _save_watchlists(wl)
    return JSONResponse(content={"ok": True})


@app.delete("/watchlists/{name}")
def delete_watchlist(request: Request, name: str):
    user = _current_user(request)
    if user:
        delete_watchlist_for_user(user["id"], name)
        return JSONResponse(content={"ok": True})
    wl = _load_watchlists()
    wl.pop(name, None)
    _save_watchlists(wl)
    return JSONResponse(content={"ok": True})


@app.put("/watchlists/{name}/tickers/{ticker}")
def add_ticker(request: Request, name: str, ticker: str):
    user = _current_user(request)
    if user:
        try:
            add_ticker_to_watchlist(user["id"], name, ticker)
        except KeyError:
            return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
        return JSONResponse(content={"ok": True})
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
def remove_ticker(request: Request, name: str, ticker: str):
    user = _current_user(request)
    if user:
        remove_ticker_from_watchlist(user["id"], name, ticker)
        return JSONResponse(content={"ok": True})
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
def watchlist_summary(request: Request, name: str):
    user = _current_user(request)
    if user:
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

    legacy = _legacy_watchlist_summary(name)
    if legacy is None:
        return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
    return JSONResponse(
        content=_build_watchlist_summary(
            tickers=legacy["tickers"],
            custom_label=legacy["custom_label"],
            custom_data=legacy["custom_data"],
            active_data=legacy["active_data"],
        )
    )


@app.put("/watchlists/{name}/custom_label")
def update_custom_label(request: Request, name: str, req: CustomLabelRequest):
    user = _current_user(request)
    if user:
        if get_watchlist_by_name(user["id"], name) is None:
            return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
        set_watchlist_custom_label(user["id"], name, req.label)
        return JSONResponse(content={"ok": True})
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
def update_custom_value(request: Request, name: str, ticker: str, req: CustomValueRequest):
    user = _current_user(request)
    if user:
        if get_watchlist_by_name(user["id"], name) is None:
            return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
        set_watchlist_note(user["id"], name, ticker, req.value)
        return JSONResponse(content={"ok": True})
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
def set_ticker_active(request: Request, name: str, ticker: str, req: ActiveRequest):
    user = _current_user(request)
    if user:
        if get_watchlist_by_name(user["id"], name) is None:
            return JSONResponse(status_code=404, content={"detail": f"{name} not found"})
        set_watchlist_item_active(user["id"], name, ticker, req.active)
        return JSONResponse(content={"ok": True})
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


@app.get("/backtest")
def backtest_page():
    return RedirectResponse(url="/static/screener.html")


@app.get("/watchlist-manager")
def watchlist_manager():
    return RedirectResponse(url="/static/watchlist.html")


@app.get("/market-overview")
def market_overview_page():
    return RedirectResponse(url="/static/market_overview.html")


@app.get("/api/market-overview")
def api_market_overview():
    rows = get_market_overview()
    return JSONResponse(content=rows)
