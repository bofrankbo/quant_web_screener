"""
Polars-based backtest engine driven by the sidebar filter controls.

Universe: stock-only by default, with optional watchlist/ticker pool and previous-day
market-cap rank filter. Non-stock instruments such as ETFs/ETNs are excluded.
Entry: technical conditions from the left sidebar, then D-1 universe membership
Exit: mirrored exit controls with separate MA, BB, RSI, volume, concentration settings
Model: signal-only (no position sizing) — each trade records price delta minus commission.
       Win rate and avg return are computed from completed trades.
"""
import io
import os
import time
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

import boto3
import duckdb
import polars as pl
from botocore.config import Config
from dotenv import load_dotenv
from app.config import DB_PATH, PARQUET_CACHE_PATH, TICKER_INFO_PATH
from app.r2 import download_parquet

load_dotenv()

CACHE_DIR = PARQUET_CACHE_PATH

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

_DEFAULT_COMMISSION = 0.001425  # Taiwan brokerage: 0.1425% each side
_STOCK_TYPES = {"twse", "tpex"}
_NON_STOCK_CATEGORIES = {
    # FinMind industry_category values that identify non-stock instruments.
    # These must be excluded from the universe; otherwise ETFs inflate signal counts.
    "受益證券", "上櫃指數股票型基金(ETF)", "上櫃ETF",
    "所有證券", "ETN", "存託憑證", "ETF", "大盤", "index", "Food",
}


# ── Data loading ──────────────────────────────────────────────────────────────

def get_conn() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH))


def _compute_screen_indicators(df: pl.DataFrame, ma_window: int, bb_window: int, rsi_period: int) -> pl.DataFrame:
    """Compute MA, Bollinger, vol_ratio, RSI per ticker using Polars window ops.

    Used by get_kline() for the chart endpoint — NOT by run_backtest()
    (which calls _compute_indicators() instead to also build exit-side signals).
    RSI uses a simple rolling mean of gains/losses rather than Wilder's EMA;
    the difference is negligible over 14 bars and avoids the cold-start issue.
    """
    df = df.sort(["ticker", "date"])

    df = df.with_columns([
        pl.col("close").rolling_mean(window_size=ma_window).over("ticker").alias("ma"),
        pl.col("volume").rolling_mean(window_size=ma_window).over("ticker").alias("avg_vol"),
    ])

    df = df.with_columns([
        pl.col("close").rolling_mean(window_size=bb_window).over("ticker").alias("bb_mid"),
        pl.col("close").rolling_std(window_size=bb_window).over("ticker").alias("bb_std"),
    ])
    df = df.with_columns([
        (pl.col("bb_mid") + 2.0 * pl.col("bb_std")).alias("bb_upper"),
        (pl.col("bb_mid") - 2.0 * pl.col("bb_std")).alias("bb_lower"),
        # Replace zero avg_vol with null so vol_ratio comes out null rather than inf.
        (pl.col("volume") / pl.col("avg_vol").replace(0, None)).alias("vol_ratio"),
    ])
    df = df.drop(["bb_mid", "bb_std"])

    df = df.with_columns(pl.col("close").diff().over("ticker").alias("_delta"))
    df = df.with_columns([
        pl.when(pl.col("_delta") > 0).then(pl.col("_delta")).otherwise(pl.lit(0.0)).alias("_gain"),
        pl.when(pl.col("_delta") < 0).then(-pl.col("_delta")).otherwise(pl.lit(0.0)).alias("_loss"),
    ])
    df = df.with_columns([
        pl.col("_gain").rolling_mean(window_size=rsi_period).over("ticker").alias("_avg_gain"),
        pl.col("_loss").rolling_mean(window_size=rsi_period).over("ticker").alias("_avg_loss"),
    ])
    df = df.with_columns(
        # +1e-10 guards against division by zero when avg_loss is 0 (all-up streak).
        (100.0 - 100.0 / (1.0 + pl.col("_avg_gain") / (pl.col("_avg_loss") + 1e-10))).alias("rsi")
    )

    return df.drop(["_delta", "_gain", "_loss", "_avg_gain", "_avg_loss"])

def _read_parquet_r2(key: str) -> pl.DataFrame | None:
    """Read a parquet from local cache, falling back to R2 on cache miss."""
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


def _load_price_data(start_date: str, tickers: list[str] | None = None) -> pl.DataFrame:
    """Scan all per-ticker parquets via DuckDB glob, extracting ticker name from filename.

    Passing tickers=None loads the full universe (all files in cache/tickers/).
    Passing an empty list returns an empty DataFrame without hitting disk.
    """
    import duckdb
    parquet_glob = str(CACHE_DIR / "tickers" / "*.parquet")
    ticker_filter = ""
    if tickers is not None:
        if not tickers:
            return pl.DataFrame()
        escaped = ", ".join("'" + t.replace("'", "''") + "'" for t in tickers)
        ticker_filter = f"WHERE ticker IN ({escaped})"
    query = f"""
    WITH priced AS (
        SELECT
            regexp_extract(filename, '([^/\\\\]+)\\.parquet$', 1) AS ticker,
            date, open, high, low, close, volume
        FROM read_parquet('{parquet_glob}', filename=true)
        WHERE date >= DATE '{start_date}'
    )
    SELECT *
    FROM priced
    {ticker_filter}
    ORDER BY ticker, date
    """
    conn = duckdb.connect()
    df = conn.execute(query).pl()
    conn.close()
    return df


def _load_concentration(start_date: str) -> pl.DataFrame:
    """Scan concentration parquets — one file per ticker, same glob pattern as prices."""
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


def _load_market_value(
    start_date: str,
    market_cap_rank: int,
    allowed_tickers: list[str] | None = None,
) -> pl.DataFrame | None:
    """Build a (date, ticker) membership table for the top-N market-cap universe.

    Reads per-day snapshot files (market_value/YYYY-MM-DD.parquet) so the rank
    reflects the actual market cap on each historical date rather than today's value.
    Returns columns [date, ticker, in_mc=True]; unmatched rows will be null after
    a left-join, which the caller fills with False.
    """
    mv_dir = CACHE_DIR / "market_value"
    allowed_set = set(allowed_tickers) if allowed_tickers is not None else None
    daily_files = sorted(
        p for p in mv_dir.glob("????-??-??.parquet")
        if p.stem >= start_date
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
            if allowed_set is not None:
                mv_df = mv_df.filter(pl.col("stock_id").is_in(list(allowed_set)))
            mv_df = mv_df.with_columns(pl.col("date").cast(pl.Date))
            mv_df = mv_df.filter(pl.col("date") >= pl.lit(start_date).cast(pl.Date))
            # Rank descending within each day so rank=1 is the largest cap.
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

    return None


def _load_ticker_names() -> dict[str, str]:
    df = _read_parquet_r2("meta/ticker_info.parquet")
    if df is None:
        return {}
    return dict(zip(df["stock_id"].to_list(), df["stock_name"].to_list()))


def _load_ticker_info() -> pl.DataFrame | None:
    """Load ticker metadata, preferring the R2 parquet over the legacy local CSV."""
    df = _read_parquet_r2("meta/ticker_info.parquet")
    if df is not None and not df.is_empty():
        return df

    if TICKER_INFO_PATH.exists():
        conn = get_conn()
        try:
            return conn.execute(
                f"""
                SELECT stock_id, stock_name, industry_category, type
                FROM read_csv_auto('{TICKER_INFO_PATH}')
                """
            ).pl()
        finally:
            conn.close()
    return None


@lru_cache(maxsize=1)
def _stock_tickers() -> tuple[str, ...]:
    """Return all stock-only ticker IDs, cached for the process lifetime.

    The cache is intentionally process-scoped (lru_cache on module-level function)
    because ticker_info is updated at most once per day and re-loading 3k rows
    on every screener call would be wasteful.
    """
    df = _load_ticker_info()
    if df is None or df.is_empty():
        return ()
    allowed = (
        df.filter(pl.col("type").is_in(list(_STOCK_TYPES)))
          .filter(~pl.col("industry_category").is_in(list(_NON_STOCK_CATEGORIES)))
          .select(pl.col("stock_id"))
          .unique()
    )
    return tuple(allowed["stock_id"].to_list())


def _filter_stock_only_tickers(tickers: list[str] | None) -> list[str] | None:
    """Intersect a caller-supplied ticker list with the stock-only universe.

    If tickers is None, returns the full stock universe.
    If the metadata is unavailable (empty _stock_tickers), passes tickers through
    unchanged rather than silently returning an empty list.
    """
    if tickers is None:
        return list(_stock_tickers())
    stock_set = set(_stock_tickers())
    if not stock_set:
        return tickers
    return [t for t in tickers if t in stock_set]


def get_ticker_summary(tickers: list[str]) -> pl.DataFrame:
    """Return last close, day%, 5d%, 10d%, 20d% for each ticker.

    Fetches only the last 22 rows per ticker to minimise memory; 22 bars is
    enough for all percentage lookbacks (1, 5, 10, 20 days).
    """
    _empty = pl.DataFrame(schema={
        "ticker": pl.Utf8, "close": pl.Float64,
        "day_pct": pl.Float64, "pct_5d": pl.Float64,
        "pct_10d": pl.Float64, "pct_20d": pl.Float64,
    })
    if not tickers:
        return _empty

    def _fetch(ticker: str):
        df = download_parquet(f"tickers/{ticker}.parquet")
        if df is None or df.is_empty():
            return None
        return ticker, df.sort("date").tail(22)["close"].to_list()

    rows = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch, t): t for t in tickers}
        for fut in as_completed(futures):
            result = fut.result()
            if result is None:
                continue
            ticker, closes = result
            n = len(closes)
            last = closes[-1]

            def _pct(back: int, closes=closes, n=n, last=last):
                if n >= back + 1:
                    old = closes[-(back + 1)]
                    return round((last / old - 1) * 100, 2) if old else None
                return None

            rows.append({
                "ticker": ticker,
                "close": round(last, 2),
                "day_pct": _pct(1),
                "pct_5d": _pct(5),
                "pct_10d": _pct(10),
                "pct_20d": _pct(20),
            })

    return pl.DataFrame(rows) if rows else _empty


def get_ticker_names(tickers: list[str]) -> pl.DataFrame:
    """Return ticker → stock_name."""
    _empty = pl.DataFrame(schema={"ticker": pl.Utf8, "name": pl.Utf8})
    if not tickers:
        return _empty

    ticker_set = set(tickers)
    cache_path = CACHE_DIR / "meta" / "ticker_info.parquet"
    if cache_path.exists():
        df = pl.read_parquet(cache_path)
        return (
            df.filter(pl.col("stock_id").is_in(ticker_set))
            .select(pl.col("stock_id").alias("ticker"), pl.col("stock_name").alias("name"))
            .unique("ticker")
        )

    if TICKER_INFO_PATH.exists():
        escaped = ", ".join(f"'{t}'" for t in tickers)
        query = f"""
        SELECT stock_id AS ticker, stock_name AS name
        FROM read_csv_auto('{TICKER_INFO_PATH}')
        WHERE stock_id IN ({escaped})
        QUALIFY ROW_NUMBER() OVER (PARTITION BY stock_id ORDER BY date DESC) = 1
        """
        with get_conn() as conn:
            return conn.execute(query).pl()

    return _empty


_overview_cache: dict = {"data": None, "ts": 0.0}
_OVERVIEW_TTL = 600  # 10 minutes


def invalidate_market_overview_cache() -> None:
    _overview_cache["data"] = None
    _overview_cache["ts"] = 0.0


def get_market_overview() -> list[dict]:
    """Return latest price + concentration for all tickers in local cache.

    Reads all parquet files in bulk via DuckDB glob — no per-ticker network calls.
    buy_volume / sell_volume are top-15 broker net shares (in 張, sell is negative).
    Result is cached for 10 minutes.
    """
    now = time.monotonic()
    if _overview_cache["data"] is not None and now - _overview_cache["ts"] < _OVERVIEW_TTL:
        return _overview_cache["data"]

    tickers_glob = str(CACHE_DIR / "tickers" / "*.parquet")
    conc_glob = str(CACHE_DIR / "concentration" / "*.parquet")
    meta_path = CACHE_DIR / "meta" / "ticker_info.parquet"

    conn = duckdb.connect()
    try:
        price_df = conn.execute(f"""
            SELECT
                replace(split_part(filename, '/', -1), '.parquet', '') AS ticker,
                date, close, volume,
                ROW_NUMBER() OVER (PARTITION BY filename ORDER BY date DESC) AS rn
            FROM read_parquet('{tickers_glob}', filename=true)
            QUALIFY rn <= 2
        """).pl()

        conc_df = conn.execute(f"""
            SELECT
                replace(split_part(filename, '/', -1), '.parquet', '') AS ticker,
                date, buy_volume, sell_volume, amount,
                concentration_5d, concentration_20d
            FROM read_parquet('{conc_glob}', filename=true)
            QUALIFY ROW_NUMBER() OVER (PARTITION BY filename ORDER BY date DESC) = 1
        """).pl()
    finally:
        conn.close()

    latest = price_df.filter(pl.col("rn") == 1).drop("rn")
    prev = (
        price_df.filter(pl.col("rn") == 2)
        .select(["ticker", pl.col("close").alias("prev_close")])
    )
    price = (
        latest
        .join(prev, on="ticker", how="left")
        .with_columns([
            ((pl.col("close") - pl.col("prev_close")).round(2)).alias("day_chg"),
            (
                (pl.col("close") - pl.col("prev_close")) / pl.col("prev_close") * 100
            ).round(2).alias("day_pct"),
        ])
        .drop("prev_close")
    )

    conc_df = conc_df.drop("date")
    result = price.join(conc_df, on="ticker", how="left")

    if meta_path.exists():
        names = (
            pl.read_parquet(meta_path)
            .select([pl.col("stock_id").alias("ticker"), pl.col("stock_name").alias("name")])
            .unique("ticker")
        )
        result = result.join(names, on="ticker", how="left")
    else:
        result = result.with_columns(pl.lit("").alias("name"))

    result = result.with_columns([
        pl.col("date").cast(pl.Utf8),
        pl.col("name").fill_null(""),
    ])

    cols = ["ticker", "name", "date", "close", "day_chg", "day_pct",
            "volume", "buy_volume", "sell_volume", "amount",
            "concentration_5d", "concentration_20d"]
    result = result.select([c for c in cols if c in result.columns])
    rows = result.sort("ticker").to_dicts()

    _overview_cache["data"] = rows
    _overview_cache["ts"] = now
    return rows


def get_kline(ticker: str, ma_window: int = 10, bb_window: int = 22) -> pl.DataFrame:
    """Return full OHLCV + MA + Bollinger history for a single ticker."""
    df = download_parquet(f"tickers/{ticker}.parquet")
    if df is None or df.is_empty():
        return pl.DataFrame()

    df = df.sort("date")
    df = df.with_columns(pl.lit(ticker).alias("ticker"))
    df = _compute_screen_indicators(df, ma_window=ma_window, bb_window=bb_window, rsi_period=14)
    df = df.drop(["ticker"])

    return df.select(["date", "open", "high", "low", "close", "volume", "ma", "bb_upper", "bb_lower", "rsi"])


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
    """Compute entry-side and exit-side indicators in a single Polars pass.

    Entry indicators: sma, bb_upper, bb_lower, vol_ratio, rsi
    Exit indicators:  exit_sma, exit_bb_lower, exit_vol_ratio, exit_rsi
    Both sets use .over("ticker") so the entire universe is processed at once
    without a Python loop over individual tickers.
    """
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
        (pl.col("bb_mid") - 2.0 * pl.col("bb_std")).alias("bb_lower"),
        (pl.col("volume") / pl.col("vol_ma").replace(0, None)).alias("vol_ratio"),
        (pl.col("volume") / pl.col("exit_vol_ma").replace(0, None)).alias("exit_vol_ratio"),
        (pl.col("exit_bb_mid") - 2.0 * pl.col("exit_bb_std")).alias("exit_bb_lower"),
    ]).drop(["bb_mid", "bb_std", "vol_ma", "exit_vol_ma"])

    # RSI: separate gain/loss series, then rolling mean of each.
    # Using SMA (not EMA) for simplicity — avoids seed-value sensitivity on short histories.
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
) -> pl.Expr:
    """Build a Polars boolean expression for entry signal detection.

    All active conditions are combined with AND — a ticker must satisfy every
    enabled filter on a given day to generate an entry signal.
    Starts from a base null-guard (sma and bb_upper must be non-null) so tickers
    with insufficient history are silently excluded rather than raising errors.
    """
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
    """Build a Polars boolean expression for exit signal detection.

    All active exit conditions are combined with AND — every enabled condition
    must trigger simultaneously to close the position.
    Returns False (never exit) when no conditions are configured.
    """
    conditions: list[pl.Expr] = []

    if exit_price_below_ma:
        conditions.append(pl.col("close") < pl.col("exit_sma"))
    if exit_bb_breakdown:
        conditions.append(pl.col("close") < pl.col("exit_bb_lower"))
    if exit_use_rsi and (exit_rsi_min > 0.0 or exit_rsi_max < 100.0):
        # RSI out of range in either direction (inner OR is intentional)
        conditions.append(
            pl.col("exit_rsi").is_not_null()
            & (
                (pl.col("exit_rsi") <= exit_rsi_min)
                | (pl.col("exit_rsi") >= exit_rsi_max)
            )
        )
    if exit_use_volume and exit_volume_ratio > 1.0:
        conditions.append(
            pl.col("exit_vol_ratio").is_not_null()
            & (pl.col("exit_vol_ratio") <= exit_volume_ratio)
        )
    if exit_use_concentration:
        conc_conditions: list[pl.Expr] = []
        if exit_conc_5d_min > 0.0:
            conc_conditions.append(
                pl.col("concentration_5d").is_not_null()
                & (pl.col("concentration_5d") <= exit_conc_5d_min)
            )
        if exit_conc_min > 0.0:
            conc_conditions.append(
                pl.col("concentration_20d").is_not_null()
                & (pl.col("concentration_20d") <= exit_conc_min)
            )
        if conc_conditions:
            conc_mask = conc_conditions[0]
            for c in conc_conditions[1:]:
                conc_mask = conc_mask & c
            conditions.append(conc_mask)

    if not conditions:
        return pl.lit(False)

    mask = conditions[0]
    for c in conditions[1:]:
        mask = mask & c
    return mask


# ── Trade tracker ─────────────────────────────────────────────────────────────

class _TradeTracker:
    def __init__(self, commission: float = _DEFAULT_COMMISSION):
        self.commission = commission
        self.open_trades: dict[str, dict] = {}  # ticker -> {entry_price, entry_date}
        self.completed: list[dict] = []  # flat event log: buy + sell rows

    def enter(self, ticker: str, price: float, entry_date: date) -> bool:
        if ticker in self.open_trades:
            return False
        self.open_trades[ticker] = {"entry_price": price, "entry_date": entry_date}
        self.completed.append({
            "date": str(entry_date),
            "ticker": ticker,
            "action": "buy",
            "price": round(float(price), 2),
        })
        return True

    def exit(self, ticker: str, price: float, exit_date: date) -> bool:
        if ticker not in self.open_trades:
            return False
        pos = self.open_trades.pop(ticker)
        net_return_pct = (price * (1 - self.commission)) / (pos["entry_price"] * (1 + self.commission)) - 1
        self.completed.append({
            "date": str(exit_date),
            "ticker": ticker,
            "action": "sell",
            "price": round(float(price), 2),
            "return_pct": round(net_return_pct * 100, 2),
        })
        return True

    def get_stats(self) -> dict:
        sells = [t for t in self.completed if t["action"] == "sell"]
        n = len(sells)
        if n == 0:
            return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "avg_return_pct": 0.0}
        wins = sum(1 for t in sells if t["return_pct"] > 0)
        avg = sum(t["return_pct"] for t in sells) / n
        return {
            "total_trades": n,
            "wins": wins,
            "losses": n - wins,
            "win_rate": round(wins / n * 100, 2),
            "avg_return_pct": round(avg, 2),
        }


# ── Main entry ────────────────────────────────────────────────────────────────

def run_backtest(
    lookback_days: int = 120,
    ma_window: int = 10,
    bb_window: int = 22,
    volume_ratio: float = 1.0,
    price_above_ma: bool = True,
    bb_breakout: bool = False,
    rsi_period: int = 14,
    rsi_min: float = 0.0,
    rsi_max: float = 100.0,
    use_concentration: bool = False,
    conc_5d_min: float = 0.0,
    conc_20d_min: float = 0.0,
    tickers: list[str] | None = None,
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
    # Load extra history before sim_start so all rolling windows are warm by day 1
    # of the simulation. Without warmup, early MA/BB/RSI values would be null and
    # the first ~bb_window trading days would produce zero signals.
    warmup = max(ma_window, bb_window, rsi_period, exit_ma_window, exit_bb_window, exit_rsi_period) + 10
    start_date = str(date.today() - timedelta(days=lookback_days + warmup))

    print("[backtest] Loading price data...")
    stock_tickers = _filter_stock_only_tickers(tickers)
    price_df = _load_price_data(start_date, tickers=stock_tickers)
    if price_df.is_empty():
        if tickers is not None:
            return {"error": "No tickers available for the selected universe."}
        return {"error": "No price data in local cache. Run sync_cache first."}

    print("[backtest] Computing indicators...")
    price_df = _compute_indicators(
        price_df, ma_window, bb_window, rsi_period, exit_ma_window, exit_bb_window, exit_rsi_period
    )

    # Trim to the actual simulation window after indicators are computed.
    sim_start = str(date.today() - timedelta(days=lookback_days))
    df = price_df.filter(pl.col("date") >= pl.lit(sim_start).cast(pl.Date))

    # ── Optional overlays ─────────────────────────────────────────────────────
    # this is optional data : concentration, etc ...
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

    if market_cap_rank is not None:
        print("[backtest] Loading market value data...")
        mv_df = _load_market_value(start_date, market_cap_rank, allowed_tickers=stock_tickers)
        if mv_df is not None:
            df = df.join(mv_df, on=["ticker", "date"], how="left")
            df = df.with_columns(pl.col("in_mc").fill_null(False))
        else:
            df = df.with_columns(pl.lit(False).alias("in_mc"))
    else:
        df = df.with_columns(pl.lit(True).alias("in_mc"))

    if market_cap_rank is not None:
        # Use yesterday's market-cap membership as the universe filter for today's entry.
        # This avoids look-ahead bias: we only know today's cap rank after the close.
        df = df.sort(["ticker", "date"])
        df = df.with_columns(
            pl.col("in_mc").shift(1).over("ticker").fill_null(False).alias("in_mc_prev")
        )
    else:
        df = df.with_columns(pl.lit(False).alias("in_mc_prev"))

    print("[backtest] Loading ticker names...")
    names = _load_ticker_names()

    df = df.sort(["date", "ticker"])
    trading_dates = df["date"].unique().sort().to_list()

    cond = _entry_mask(
        price_above_ma, bb_breakout, volume_ratio,
        use_concentration, conc_5d_min, conc_20d_min,
        rsi_min, rsi_max,
    )
    universe_mask = pl.lit(True)
    if market_cap_rank is not None:
        universe_mask = pl.col("in_mc_prev") == True
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

        # ── Next-day fill model ───────────────────────────────────────────────
        # Signals are generated on the close of day D.
        # Orders are queued with execute_date = D+1 and filled at D+1 open price.
        # This prevents look-ahead bias from using today's close as fill price.
        todays_orders = [o for o in pending_orders if o["execute_date"] == dt]
        if todays_orders:
            # Process sells first to free up capital before buys.
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

        # Check exit conditions for all open positions at today's close.
        for ticker in list(held):
            row = day.filter(pl.col("ticker") == ticker)
            if row.is_empty():
                continue
            if not row.filter(exit_cond).is_empty() and not any(
                o["type"] == "sell" and o["ticker"] == ticker for o in pending_orders
            ):
                pending_orders.append({"type": "sell", "ticker": ticker, "execute_date": next_dt})

        # Check entry conditions for all tickers in the active universe.
        for row in day.filter(universe_mask & cond).iter_rows(named=True):
            ticker = row["ticker"]
            if ticker in held:
                continue
            # Deduplicate: don't queue a second buy order for the same ticker on the same next_dt.
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

    # ── End-of-period snapshot ────────────────────────────────────────────────

    last_dt = trading_dates[-1]
    last_day = df.filter(pl.col("date") == last_dt)
    last_prices = dict(zip(last_day["ticker"].to_list(), last_day["close"].to_list()))

    # Positions still open at simulation end — valued at last close, not yet closed.
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

    # Tickers that fired an entry signal on the last trading day → actionable tomorrow.
    held = set(tracker.open_trades.keys())
    signals = []
    for r in last_day.filter(universe_mask & cond).iter_rows(named=True):
        signals.append({
            "ticker": r["ticker"],
            "name": names.get(r["ticker"], ""),
            "date": str(last_dt),
            "close": round(r["close"], 2) if r["close"] is not None else None,
            "ma": round(r["sma"], 2) if r["sma"] is not None else None,
            "bb_upper": round(r["bb_upper"], 2) if r["bb_upper"] is not None else None,
            "bb_lower": round(r["bb_lower"], 2) if r["bb_lower"] is not None else None,
            "vol_ratio": round(r["vol_ratio"], 2) if r["vol_ratio"] is not None else None,
            "rsi": round(r["rsi"], 1) if r["rsi"] is not None else None,
            "conc_5d": round(r["concentration_5d"], 4) if r["concentration_5d"] is not None else None,
            "conc_20d": round(r["concentration_20d"], 4) if r["concentration_20d"] is not None else None,
            # already_held lets the UI distinguish "new signal" from "signal on an open position".
            "already_held": r["ticker"] in held,
        })

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
