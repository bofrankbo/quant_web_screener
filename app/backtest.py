"""
Polars-based backtest engine driven by the same screener params set in the sidebar.

Entry: built from price_above_ma, bb_breakout, volume_ratio, concentration, RSI, market cap
Exit:  mirrored exit controls with separate MA, BB, RSI, volume, concentration settings
Model: signal-only (no position sizing) — each trade records price delta minus commission.
       Win rate and avg return are computed from completed trades.
"""
import io
import os
from datetime import date, timedelta
from pathlib import Path

import boto3
import polars as pl
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv()

CACHE_DIR = Path(__file__).parent.parent / "data" / "cache"

R2_BUCKET            = os.environ["R2_BUCKET"]
R2_ENDPOINT          = os.environ["R2_ENDPOINT"]
R2_ACCESS_KEY_ID     = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4"),
    region_name="auto",
)

_DEFAULT_COMMISSION = 0.001425


# ── Data loading ──────────────────────────────────────────────────────────────

def _read_parquet_r2(key: str) -> pl.DataFrame | None:
    local = CACHE_DIR / key
    if local.exists():
        return pl.read_parquet(local)
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=key)
        return pl.read_parquet(io.BytesIO(obj["Body"].read()))
    except Exception as e:
        if "NoSuchKey" in str(e) or "404" in str(e):
            return None
        raise


def _load_price_data(start_date: str) -> pl.DataFrame:
    import duckdb
    parquet_glob = str(CACHE_DIR / "tickers" / "*.parquet")
    query = f"""
    SELECT
        regexp_extract(filename, '([^/\\\\]+)\\.parquet$', 1) AS ticker,
        date, open, high, low, close, volume
    FROM read_parquet('{parquet_glob}', filename=true)
    WHERE date >= DATE '{start_date}'
    ORDER BY ticker, date
    """
    conn = duckdb.connect()
    df = conn.execute(query).pl()
    conn.close()
    return df


def _load_concentration(start_date: str) -> pl.DataFrame:
    import duckdb
    parquet_glob = str(CACHE_DIR / "concentration" / "*.parquet")
    query = f"""
    SELECT
        regexp_extract(filename, '([^/\\\\]+)\\.parquet$', 1) AS ticker,
        date, concentration_5d, concentration_20d
    FROM read_parquet('{parquet_glob}', filename=true)
    WHERE date >= DATE '{start_date}'
    ORDER BY ticker, date
    """
    conn = duckdb.connect()
    df = conn.execute(query).pl()
    conn.close()
    return df


def _load_market_value(start_date: str, market_cap_rank: int) -> pl.DataFrame | None:
    """Load date-based market-cap membership from local cache.

    Prefer the per-day `market_value/YYYY-MM-DD.parquet` snapshots so the
    backtest can honor arbitrary `market_cap_rank` values. Fall back to the
    precomputed `top200.parquet` file if the daily snapshots are unavailable.
    """
    mv_dir = CACHE_DIR / "market_value"
    daily_files = sorted(
        p for p in mv_dir.glob("????-??-??.parquet")
        if p.name != "top200.parquet"
    )

    if daily_files:
        frames = []
        for path in daily_files:
            try:
                frames.append(
                    pl.read_parquet(path).select(["date", "stock_id", "market_value"])
                )
            except Exception:
                continue

        if frames:
            mv_df = pl.concat(frames, how="vertical_relaxed")
            mv_df = mv_df.with_columns(pl.col("date").cast(pl.Date))
            mv_df = mv_df.filter(pl.col("date") >= pl.lit(start_date).cast(pl.Date))
            mv_df = mv_df.with_columns(
                pl.col("market_value")
                .rank(method="ordinal", descending=True)
                .over("date")
                .alias("rank")
            )
            mv_df = mv_df.filter(pl.col("rank") <= market_cap_rank)
            return mv_df.select(
                pl.col("date"),
                pl.col("stock_id").alias("ticker"),
            ).with_columns(pl.lit(True).alias("in_mc"))

    top200 = _read_parquet_r2("market_value/top200.parquet")
    if top200 is None:
        return None
    return (
        top200
        .with_columns(pl.col("date").cast(pl.Date))
        .filter(pl.col("date") >= pl.lit(start_date).cast(pl.Date))
        .filter(pl.col("rank") <= market_cap_rank)
        .select(
            pl.col("date"),
            pl.col("stock_id").alias("ticker"),
        )
        .with_columns(pl.lit(True).alias("in_mc"))
    )


def _load_ticker_names() -> dict[str, str]:
    df = _read_parquet_r2("meta/ticker_info.parquet")
    if df is None:
        return {}
    return dict(zip(df["stock_id"].to_list(), df["stock_name"].to_list()))


# ── Indicators ────────────────────────────────────────────────────────────────

def _compute_indicators(
    df: pl.DataFrame,
    ma_window: int,
    bb_window: int,
    rsi_period: int,
    exit_ma_window: int,
    exit_bb_window: int,
    exit_rsi_period: int,
) -> pl.DataFrame:
    """Compute entry and exit indicators per ticker."""
    df = df.sort(["ticker", "date"])

    df = df.with_columns([
        pl.col("close").rolling_mean(ma_window).over("ticker").alias("sma"),
        pl.col("volume").rolling_mean(ma_window).over("ticker").alias("vol_ma"),
        pl.col("close").rolling_mean(exit_ma_window).over("ticker").alias("exit_sma"),
        pl.col("volume").rolling_mean(exit_ma_window).over("ticker").alias("exit_vol_ma"),
    ])

    df = df.with_columns([
        pl.col("close").rolling_mean(bb_window).over("ticker").alias("bb_mid"),
        pl.col("close").rolling_std(bb_window).over("ticker").alias("bb_std"),
        pl.col("close").rolling_mean(exit_bb_window).over("ticker").alias("exit_bb_mid"),
        pl.col("close").rolling_std(exit_bb_window).over("ticker").alias("exit_bb_std"),
    ]).with_columns([
        (pl.col("bb_mid") + 2.0 * pl.col("bb_std")).alias("bb_upper"),
        (pl.col("volume") / pl.col("vol_ma").replace(0, None)).alias("vol_ratio"),
        (pl.col("volume") / pl.col("exit_vol_ma").replace(0, None)).alias("exit_vol_ratio"),
        (pl.col("exit_bb_mid") - 2.0 * pl.col("exit_bb_std")).alias("exit_bb_lower"),
    ]).drop(["bb_mid", "bb_std", "vol_ma", "exit_vol_ma"])

    df = df.with_columns(
        pl.col("close").diff().over("ticker").alias("_d")
    ).with_columns([
        pl.when(pl.col("_d") > 0).then(pl.col("_d")).otherwise(pl.lit(0.0)).alias("_g"),
        pl.when(pl.col("_d") < 0).then(-pl.col("_d")).otherwise(pl.lit(0.0)).alias("_l"),
    ]).with_columns([
        pl.col("_g").rolling_mean(rsi_period).over("ticker").alias("_ag"),
        pl.col("_l").rolling_mean(rsi_period).over("ticker").alias("_al"),
        pl.col("_g").rolling_mean(exit_rsi_period).over("ticker").alias("_exit_ag"),
        pl.col("_l").rolling_mean(exit_rsi_period).over("ticker").alias("_exit_al"),
    ]).with_columns(
        (100.0 - 100.0 / (1.0 + pl.col("_ag") / (pl.col("_al") + 1e-10))).alias("rsi")
    ).with_columns(
        (100.0 - 100.0 / (1.0 + pl.col("_exit_ag") / (pl.col("_exit_al") + 1e-10))).alias("exit_rsi")
    ).drop(["_d", "_g", "_l", "_ag", "_al", "_exit_ag", "_exit_al", "exit_bb_mid", "exit_bb_std"])

    return df


# ── Entry condition builder ───────────────────────────────────────────────────

def _entry_mask(
    price_above_ma: bool,
    bb_breakout: bool,
    volume_ratio: float,
    use_concentration: bool,
    conc_5d_min: float,
    conc_20d_min: float,
    rsi_min: float,
    rsi_max: float,
    market_cap_rank: int | None,
) -> pl.Expr:
    mask = pl.col("sma").is_not_null() & pl.col("bb_upper").is_not_null()

    if price_above_ma:
        mask = mask & (pl.col("close") > pl.col("sma"))
    if bb_breakout:
        mask = mask & (pl.col("close") > pl.col("bb_upper"))
    if volume_ratio > 1.0:
        mask = mask & pl.col("vol_ratio").is_not_null() & (pl.col("vol_ratio") >= volume_ratio)
    if use_concentration:
        mask = (
            mask
            & pl.col("concentration_5d").is_not_null()
            & (pl.col("concentration_5d") > conc_5d_min)
            & pl.col("concentration_20d").is_not_null()
            & (pl.col("concentration_20d") > conc_20d_min)
        )
    if rsi_min > 0.0:
        mask = mask & pl.col("rsi").is_not_null() & (pl.col("rsi") >= rsi_min)
    if rsi_max < 100.0:
        mask = mask & pl.col("rsi").is_not_null() & (pl.col("rsi") <= rsi_max)
    if market_cap_rank is not None:
        mask = mask & (pl.col("in_mc") == True)

    return mask


def _exit_mask(
    exit_price_below_ma: bool,
    exit_bb_breakdown: bool,
    exit_use_rsi: bool,
    exit_rsi_min: float,
    exit_rsi_max: float,
    exit_use_volume: bool,
    exit_volume_ratio: float,
    exit_use_concentration: bool,
    exit_conc_5d_min: float,
    exit_conc_min: float,
) -> pl.Expr:
    mask = pl.lit(False)

    if exit_price_below_ma:
        mask = mask | (pl.col("close") < pl.col("exit_sma"))
    if exit_bb_breakdown:
        mask = mask | (pl.col("close") < pl.col("exit_bb_lower"))
    if exit_use_rsi and (exit_rsi_min > 0.0 or exit_rsi_max < 100.0):
        mask = mask | (
            pl.col("exit_rsi").is_not_null()
            & (
                (pl.col("exit_rsi") <= exit_rsi_min)
                | (pl.col("exit_rsi") >= exit_rsi_max)
            )
        )
    if exit_use_volume and exit_volume_ratio > 1.0:
        mask = mask | (
            pl.col("exit_vol_ratio").is_not_null()
            & (pl.col("exit_vol_ratio") <= exit_volume_ratio)
        )
    if exit_use_concentration:
        conc_mask = pl.lit(False)
        if exit_conc_5d_min > 0.0:
            conc_mask = conc_mask | (
                pl.col("concentration_5d").is_not_null()
                & (pl.col("concentration_5d") <= exit_conc_5d_min)
            )
        if exit_conc_min > 0.0:
            conc_mask = conc_mask | (
                pl.col("concentration_20d").is_not_null()
                & (pl.col("concentration_20d") <= exit_conc_min)
            )
        mask = mask | conc_mask

    return mask


# ── Trade tracker ─────────────────────────────────────────────────────────────

class _TradeTracker:
    def __init__(self, commission: float = _DEFAULT_COMMISSION):
        self.commission = commission
        self.open_trades: dict[str, dict] = {}  # ticker -> {entry_price, entry_date}
        self.completed: list[dict] = []

    def enter(self, ticker: str, price: float, entry_date: date) -> bool:
        if ticker in self.open_trades:
            return False
        self.open_trades[ticker] = {"entry_price": price, "entry_date": entry_date}
        return True

    def exit(self, ticker: str, price: float, exit_date: date) -> bool:
        if ticker not in self.open_trades:
            return False
        pos = self.open_trades.pop(ticker)
        net_return_pct = (price * (1 - self.commission)) / (pos["entry_price"] * (1 + self.commission)) - 1
        self.completed.append({
            "ticker": ticker,
            "entry_date": str(pos["entry_date"]),
            "exit_date": str(exit_date),
            "entry_price": round(float(pos["entry_price"]), 2),
            "exit_price": round(float(price), 2),
            "net_return_pct": round(net_return_pct * 100, 4),
        })
        return True

    def get_stats(self) -> dict:
        n = len(self.completed)
        if n == 0:
            return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "avg_return_pct": 0.0}
        wins = sum(1 for t in self.completed if t["net_return_pct"] > 0)
        avg = sum(t["net_return_pct"] for t in self.completed) / n
        return {
            "total_trades": n,
            "wins": wins,
            "losses": n - wins,
            "win_rate": round(wins / n * 100, 2),
            "avg_return_pct": round(avg, 4),
        }


# ── Main entry ────────────────────────────────────────────────────────────────

def run_backtest(
    lookback_days: int = 365,
    ma_window: int = 10,
    bb_window: int = 22,
    volume_ratio: float = 1.5,
    price_above_ma: bool = True,
    bb_breakout: bool = False,
    rsi_period: int = 14,
    rsi_min: float = 0.0,
    rsi_max: float = 100.0,
    use_concentration: bool = False,
    conc_5d_min: float = 0.0,
    conc_20d_min: float = 0.0,
    market_cap_rank: int | None = None,
    exit_price_below_ma: bool = True,
    exit_ma_window: int = 10,
    exit_bb_breakdown: bool = False,
    exit_bb_window: int = 22,
    exit_use_rsi: bool = False,
    exit_rsi_period: int = 14,
    exit_rsi_min: float = 0.0,
    exit_rsi_max: float = 75.0,
    exit_use_volume: bool = False,
    exit_volume_ratio: float = 1.5,
    exit_use_concentration: bool = False,
    exit_conc_5d_min: float = 0.0,
    exit_conc_min: float = 0.0,
    commission: float = _DEFAULT_COMMISSION,
    debug: bool = False,
) -> dict:
    warmup = max(ma_window, bb_window, rsi_period, exit_ma_window, exit_bb_window, exit_rsi_period) + 10
    start_date = str(date.today() - timedelta(days=lookback_days + warmup))

    print("[backtest] Loading price data...")
    price_df = _load_price_data(start_date)
    if price_df.is_empty():
        return {"error": "No price data in local cache. Run sync_cache first."}

    print("[backtest] Computing indicators...")
    price_df = _compute_indicators(
        price_df, ma_window, bb_window, rsi_period, exit_ma_window, exit_bb_window, exit_rsi_period
    )

    sim_start = str(date.today() - timedelta(days=lookback_days))
    df = price_df.filter(pl.col("date") >= pl.lit(sim_start).cast(pl.Date))

    # Concentration
    if use_concentration:
        print("[backtest] Loading concentration data...")
        conc_df = _load_concentration(start_date)
        if not conc_df.is_empty():
            df = df.join(
                conc_df.select(["ticker", "date", "concentration_5d", "concentration_20d"]),
                on=["ticker", "date"], how="left"
            )
        else:
            df = df.with_columns([
                pl.lit(None).cast(pl.Float64).alias("concentration_5d"),
                pl.lit(None).cast(pl.Float64).alias("concentration_20d"),
            ])
    else:
        df = df.with_columns([
            pl.lit(None).cast(pl.Float64).alias("concentration_5d"),
            pl.lit(None).cast(pl.Float64).alias("concentration_20d"),
        ])

    # Market cap
    if market_cap_rank is not None:
        print("[backtest] Loading market value data...")
        mv_df = _load_market_value(start_date, market_cap_rank)
        if mv_df is not None:
            df = df.join(mv_df, on=["ticker", "date"], how="left")
            df = df.with_columns(pl.col("in_mc").fill_null(False))
        else:
            df = df.with_columns(pl.lit(True).alias("in_mc"))
    else:
        df = df.with_columns(pl.lit(True).alias("in_mc"))

    print("[backtest] Loading ticker names...")
    names = _load_ticker_names()

    df = df.sort(["date", "ticker"])
    trading_dates = df["date"].unique().sort().to_list()

    cond = _entry_mask(
        price_above_ma, bb_breakout, volume_ratio,
        use_concentration, conc_5d_min, conc_20d_min,
        rsi_min, rsi_max, market_cap_rank,
    )
    exit_cond = _exit_mask(
        exit_price_below_ma,
        exit_bb_breakdown,
        exit_use_rsi,
        exit_rsi_min,
        exit_rsi_max,
        exit_use_volume,
        exit_volume_ratio,
        exit_use_concentration,
        exit_conc_5d_min,
        exit_conc_min,
    )

    tracker = _TradeTracker(commission=commission)
    pending_orders: list[dict] = []
    debug_days: list[dict] = []

    for idx, dt in enumerate(trading_dates):
        day = df.filter(pl.col("date") == dt)
        open_prices = dict(zip(day["ticker"].to_list(), day["open"].to_list()))

        # Execute orders generated on the previous trading day at today's open.
        todays_orders = [o for o in pending_orders if o["execute_date"] == dt]
        if todays_orders:
            for order in todays_orders:
                if order["type"] != "sell":
                    continue
                price = open_prices.get(order["ticker"])
                if price is not None:
                    tracker.exit(order["ticker"], price, dt)
            for order in todays_orders:
                if order["type"] != "buy":
                    continue
                price = open_prices.get(order["ticker"])
                if price is not None:
                    tracker.enter(order["ticker"], price, dt)
            pending_orders = [o for o in pending_orders if o["execute_date"] != dt]

        next_dt = trading_dates[idx + 1] if idx + 1 < len(trading_dates) else None
        if next_dt is None:
            continue

        held = set(tracker.open_trades.keys())

        # Exit signals generated on today's close → execute at next open.
        for ticker in list(held):
            row = day.filter(pl.col("ticker") == ticker)
            if row.is_empty():
                continue
            if not row.filter(exit_cond).is_empty() and not any(
                o["type"] == "sell" and o["ticker"] == ticker for o in pending_orders
            ):
                pending_orders.append({"type": "sell", "ticker": ticker, "execute_date": next_dt})

        # Entry signals → execute at next open.
        for row in day.filter(cond).iter_rows(named=True):
            ticker = row["ticker"]
            if ticker in held:
                continue
            if any(
                o["type"] == "buy" and o["ticker"] == ticker and o["execute_date"] == next_dt
                for o in pending_orders
            ):
                continue
            pending_orders.append({"type": "buy", "ticker": ticker, "execute_date": next_dt})

        if debug:
            debug_days.append({
                "date": str(dt),
                "open_trades": len(tracker.open_trades),
                "pending": len(pending_orders),
            })

    last_dt = trading_dates[-1]
    last_day = df.filter(pl.col("date") == last_dt)
    last_prices = dict(zip(last_day["ticker"].to_list(), last_day["close"].to_list()))

    # Open trades still running at end of period
    open_trades = sorted([
        {
            "ticker": t,
            "name": names.get(t, ""),
            "entry_date": str(pos["entry_date"]),
            "entry_price": round(float(pos["entry_price"]), 2),
            "current_price": round(float(last_prices.get(t, pos["entry_price"])), 2),
        }
        for t, pos in tracker.open_trades.items()
    ], key=lambda x: x["entry_date"], reverse=True)

    # Next-day entry signals
    held = set(tracker.open_trades.keys())
    signals = [
        {
            "ticker": r["ticker"],
            "name": names.get(r["ticker"], ""),
            "close": round(r["close"], 2),
            "bb_upper": round(r["bb_upper"], 2) if r["bb_upper"] is not None else None,
            "conc_5d": round(r["concentration_5d"], 4) if r["concentration_5d"] is not None else None,
            "conc_20d": round(r["concentration_20d"], 4) if r["concentration_20d"] is not None else None,
            "already_held": r["ticker"] in held,
        }
        for r in last_day.filter(cond).iter_rows(named=True)
    ]

    stats = tracker.get_stats()
    result = {
        "as_of": str(last_dt),
        "signals": signals,
        "open_trades": open_trades,
        "stats": stats,
        "trades": tracker.completed,
    }
    if debug:
        result["debug"] = {"days": debug_days, "final_pending": pending_orders}
    print(f"[backtest] Done. trades={stats['total_trades']}, win_rate={stats['win_rate']}%, open={len(open_trades)}")
    return result
