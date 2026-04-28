"""
Polars-based backtest engine driven by the same screener params set in the sidebar.

Entry: built from price_above_ma, bb_breakout, volume_ratio, concentration, RSI, market cap
Exit:  close < SMA(ma_window)
Size:  1/MAX_POS of portfolio per position  Commission: 0.3%  Max positions: 30
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

COMMISSION = 0.003
INIT_CASH  = 1_000_000.0
MAX_POS    = 30


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


def _load_market_value() -> pl.DataFrame | None:
    return _read_parquet_r2("market_value/top200.parquet")


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
) -> pl.DataFrame:
    """SMA, BB_upper, vol_ratio, RSI per ticker — mirrors screener.py logic."""
    df = df.sort(["ticker", "date"])

    df = df.with_columns([
        pl.col("close").rolling_mean(ma_window).over("ticker").alias("sma"),
        pl.col("volume").rolling_mean(ma_window).over("ticker").alias("vol_ma"),
    ])

    df = df.with_columns([
        pl.col("close").rolling_mean(bb_window).over("ticker").alias("bb_mid"),
        pl.col("close").rolling_std(bb_window).over("ticker").alias("bb_std"),
    ]).with_columns([
        (pl.col("bb_mid") + 2.0 * pl.col("bb_std")).alias("bb_upper"),
        (pl.col("volume") / pl.col("vol_ma").replace(0, None)).alias("vol_ratio"),
    ]).drop(["bb_mid", "bb_std", "vol_ma"])

    df = df.with_columns(
        pl.col("close").diff().over("ticker").alias("_d")
    ).with_columns([
        pl.when(pl.col("_d") > 0).then(pl.col("_d")).otherwise(pl.lit(0.0)).alias("_g"),
        pl.when(pl.col("_d") < 0).then(-pl.col("_d")).otherwise(pl.lit(0.0)).alias("_l"),
    ]).with_columns([
        pl.col("_g").rolling_mean(rsi_period).over("ticker").alias("_ag"),
        pl.col("_l").rolling_mean(rsi_period).over("ticker").alias("_al"),
    ]).with_columns(
        (100.0 - 100.0 / (1.0 + pl.col("_ag") / (pl.col("_al") + 1e-10))).alias("rsi")
    ).drop(["_d", "_g", "_l", "_ag", "_al"])

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


# ── Portfolio ─────────────────────────────────────────────────────────────────

class _Portfolio:
    def __init__(self):
        self.cash = INIT_CASH
        self.positions: dict[str, dict] = {}
        self.trades: list[dict] = []

    def portfolio_value(self, prices: dict[str, float]) -> float:
        return self.cash + sum(
            p["shares"] * prices.get(t, p["cost_price"])
            for t, p in self.positions.items()
        )

    def buy(self, ticker: str, price: float, pv: float, buy_date: date) -> bool:
        if len(self.positions) >= MAX_POS or ticker in self.positions:
            return False
        shares = int(pv / MAX_POS / price / 1000) * 1000
        if shares <= 0:
            return False
        cost = shares * price * (1 + COMMISSION)
        if cost > self.cash:
            return False
        self.cash -= cost
        self.positions[ticker] = {"shares": shares, "cost_price": price, "buy_date": buy_date}
        self.trades.append({"type": "buy", "ticker": ticker, "date": buy_date,
                            "price": price, "shares": shares})
        return True

    def sell(self, ticker: str, price: float, sell_date: date) -> None:
        if ticker not in self.positions:
            return
        pos = self.positions.pop(ticker)
        self.cash += pos["shares"] * price * (1 - COMMISSION)
        self.trades.append({"type": "sell", "ticker": ticker, "date": sell_date,
                            "price": price, "shares": pos["shares"],
                            "cost_price": pos["cost_price"]})

    def get_stats(self, prices: dict[str, float]) -> dict:
        fv = self.portfolio_value(prices)
        sells = [t for t in self.trades if t["type"] == "sell"]
        pnls = [(t["price"] - t["cost_price"]) * t["shares"] for t in sells]
        wins = sum(1 for p in pnls if p > 0)
        return {
            "total_trades": len(sells),
            "winning_trades": wins,
            "losing_trades": len(sells) - wins,
            "total_net_profit": round(sum(pnls), 2),
            "total_return_pct": round((fv - INIT_CASH) / INIT_CASH * 100, 2),
            "final_value": round(fv, 2),
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
) -> dict:
    warmup = max(ma_window, bb_window, rsi_period) + 10
    start_date = str(date.today() - timedelta(days=lookback_days + warmup))

    print("[backtest] Loading price data...")
    price_df = _load_price_data(start_date)
    if price_df.is_empty():
        return {"error": "No price data in local cache. Run sync_cache first."}

    print("[backtest] Computing indicators...")
    price_df = _compute_indicators(price_df, ma_window, bb_window, rsi_period)

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
        mv_df = _load_market_value()
        if mv_df is not None:
            mv_set = (mv_df.select(["date", "stock_id"])
                      .with_columns(pl.lit(True).alias("in_mc"))
                      .rename({"stock_id": "ticker"}))
            df = df.join(mv_set, on=["ticker", "date"], how="left")
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

    portfolio = _Portfolio()

    for dt in trading_dates:
        day = df.filter(pl.col("date") == dt)
        prices = dict(zip(day["ticker"].to_list(), day["close"].to_list()))
        pv = portfolio.portfolio_value(prices)

        # Exit: close < SMA
        for ticker in list(portfolio.positions.keys()):
            row = day.filter(pl.col("ticker") == ticker)
            if row.is_empty():
                continue
            sma = row["sma"][0]
            if sma is not None and row["close"][0] < sma:
                portfolio.sell(ticker, row["close"][0], dt)

        # Entry
        for row in day.filter(cond).iter_rows(named=True):
            portfolio.buy(row["ticker"], row["close"], pv, dt)

    last_dt = trading_dates[-1]
    last_day = df.filter(pl.col("date") == last_dt)
    last_prices = dict(zip(last_day["ticker"].to_list(), last_day["close"].to_list()))
    stats = portfolio.get_stats(last_prices)

    # Open positions
    positions = []
    for ticker, pos in portfolio.positions.items():
        cp = last_prices.get(ticker, pos["cost_price"])
        positions.append({
            "ticker": ticker,
            "name": names.get(ticker, ""),
            "buy_date": str(pos["buy_date"]),
            "cost_price": round(pos["cost_price"], 2),
            "current_price": round(cp, 2),
            "return_pct": round((cp - pos["cost_price"]) / pos["cost_price"] * 100, 2),
            "unrealized_pnl": round((cp - pos["cost_price"]) * pos["shares"], 2),
        })
    positions.sort(key=lambda x: x["buy_date"], reverse=True)

    # Next-day signals
    held = set(portfolio.positions.keys())
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

    print(f"[backtest] Done. signals={len(signals)}, positions={len(positions)}")
    return {
        "as_of": str(last_dt),
        "signals": signals,
        "positions": positions,
        "stats": stats,
    }
