"""
K-line quantitative screener: DuckDB reads CSVs, Polars computes indicators.
Filters: MA, Bollinger Band, Volume Ratio, RSI, Concentration.
"""
import duckdb
import polars as pl
from datetime import date
from app.config import MARKET_VALUE_PATH, DB_PATH, TICKER_INFO_PATH, PARQUET_CACHE_PATH
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.r2 import download_parquet


def get_conn() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH))


def _compute_indicators(df: pl.DataFrame, ma_window: int, bb_window: int, rsi_period: int) -> pl.DataFrame:
    """Compute MA, Bollinger, vol_ratio, RSI per ticker using Polars window ops."""
    df = df.sort(["ticker", "date"])

    # MA
    df = df.with_columns([
        pl.col("close").rolling_mean(window_size=ma_window).over("ticker").alias("ma"),
        pl.col("volume").rolling_mean(window_size=ma_window).over("ticker").alias("avg_vol"),
    ])

    # Bollinger (uses separate bb_window)
    df = df.with_columns([
        pl.col("close").rolling_mean(window_size=bb_window).over("ticker").alias("bb_mid"),
        pl.col("close").rolling_std(window_size=bb_window).over("ticker").alias("bb_std"),
    ])
    df = df.with_columns([
        (pl.col("bb_mid") + 2.0 * pl.col("bb_std")).alias("bb_upper"),
        (pl.col("bb_mid") - 2.0 * pl.col("bb_std")).alias("bb_lower"),
        (pl.col("volume") / pl.col("avg_vol").replace(0, None)).alias("vol_ratio"),
    ])
    df = df.drop(["bb_mid", "bb_std"])

    # RSI via Wilder's smoothing approximation (simple moving avg variant)
    df = df.with_columns(
        pl.col("close").diff().over("ticker").alias("_delta")
    )
    df = df.with_columns([
        pl.when(pl.col("_delta") > 0).then(pl.col("_delta")).otherwise(pl.lit(0.0)).alias("_gain"),
        pl.when(pl.col("_delta") < 0).then(-pl.col("_delta")).otherwise(pl.lit(0.0)).alias("_loss"),
    ])
    df = df.with_columns([
        pl.col("_gain").rolling_mean(window_size=rsi_period).over("ticker").alias("_avg_gain"),
        pl.col("_loss").rolling_mean(window_size=rsi_period).over("ticker").alias("_avg_loss"),
    ])
    df = df.with_columns(
        (100.0 - 100.0 / (1.0 + pl.col("_avg_gain") / (pl.col("_avg_loss") + 1e-10))).alias("rsi")
    )

    return df.drop(["_delta", "_gain", "_loss", "_avg_gain", "_avg_loss"])


def screen_stocks(
    ma_window: int = 10,
    bb_window: int = 22,
    volume_ratio: float = 1.5,
    price_above_ma: bool = True,
    bb_breakout: bool = False,
    rsi_period: int = 14,
    rsi_min: float = 0.0,
    rsi_max: float = 100.0,
    use_concentration: bool = False,
    conc_min: float = 0.0,
    conc_5d_min: float = 0.0,
    market_cap_rank: int | None = None,
    top_n: int = 50,
    tickers: list[str] | None = None,
) -> pl.DataFrame:
    """
    Multi-condition K-line screener.

    Price conditions (all active when flag is True):
      price_above_ma  : close > MA(ma_window)
      bb_breakout     : close > Bollinger upper band
      rsi_min/max     : RSI(rsi_period) within [rsi_min, rsi_max]
      volume_ratio    : volume > volume_ratio * MA(volume)
      use_concentration: concentration_20d >= conc_min (join concentration CSVs)
    """
    lookback = max(ma_window, bb_window, rsi_period) + 10
    parquet_glob = str(PARQUET_CACHE_PATH / "tickers" / "*.parquet")

    # Market cap pre-filter: use T-1 market value file (one parquet per date)
    if market_cap_rank is not None:
        mv_dir = PARQUET_CACHE_PATH / "market_value"
        mv_files = sorted(mv_dir.glob("????-??-??.parquet"))
        if mv_files:
            t1_file = mv_files[-1]  # latest available date = T-1
            mv = pl.read_parquet(t1_file)
            mc_tickers = (
                mv.sort("market_value", descending=True)
                  .head(market_cap_rank)
                  ["stock_id"]
                  .to_list()
            )
            if tickers is not None:
                tickers = [t for t in tickers if t in set(mc_tickers)]
            else:
                tickers = mc_tickers

    ticker_filter = ""
    if tickers is not None:
        if not tickers:
            return pl.DataFrame()
        escaped = ", ".join(f"'{t}'" for t in tickers)
        ticker_filter = f"AND ticker IN ({escaped})"

    # DuckDB: read local parquet cache, extract last `lookback` rows per ticker
    fetch_query = f"""
    WITH ranked AS (
        SELECT
            regexp_extract(filename, '([^/\\\\]+)\\.parquet$', 1) AS ticker,
            date,
            open, high, low, close, volume,
            ROW_NUMBER() OVER (
                PARTITION BY filename ORDER BY date DESC
            ) AS rn
        FROM read_parquet('{parquet_glob}', filename=true)
    )
    SELECT ticker, date, open, high, low, close, volume
    FROM ranked
    WHERE rn <= {lookback}
    {ticker_filter}
    """

    with get_conn() as conn:
        df = conn.execute(fetch_query).pl()

    if df.is_empty():
        return pl.DataFrame()

    # Polars: compute indicators
    df = _compute_indicators(df, ma_window, bb_window, rsi_period)

    # Keep T-1 row per ticker: exclude today so signal is always based on previous close
    today = pl.lit(date.today())
    latest = (
        df.filter(pl.col("date").cast(pl.Date) < today)
        .sort(["ticker", "date"])
        .group_by("ticker")
        .agg(pl.all().last())
    )

    # Apply filters
    mask = pl.lit(True)

    if price_above_ma:
        mask = mask & pl.col("close").gt(pl.col("ma"))

    if bb_breakout:
        mask = mask & pl.col("close").gt(pl.col("bb_upper"))

    mask = mask & pl.col("vol_ratio").ge(volume_ratio)

    if rsi_min > 0.0:
        mask = mask & pl.col("rsi").ge(rsi_min)
    if rsi_max < 100.0:
        mask = mask & pl.col("rsi").le(rsi_max)

    # Drop null indicator rows (insufficient history)
    mask = mask & pl.col("ma").is_not_null() & pl.col("rsi").is_not_null()

    latest = latest.filter(mask)

    # Fetch concentration for filtered tickers only (parallel R2 downloads)
    filtered_tickers = latest["ticker"].to_list()

    def _fetch_conc(ticker: str):
        df = download_parquet(f"concentration/{ticker}.parquet")
        if df is None or df.is_empty():
            return None
        last = df.sort("date").tail(1)
        return {
            "ticker": ticker,
            "concentration_5d": last["concentration_5d"][0],
            "concentration_20d": last["concentration_20d"][0],
        }

    conc_rows = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        for row in pool.map(_fetch_conc, filtered_tickers):
            if row is not None:
                conc_rows.append(row)

    conc_df = pl.DataFrame(conc_rows) if conc_rows else pl.DataFrame(schema={
        "ticker": pl.Utf8, "concentration_5d": pl.Float64, "concentration_20d": pl.Float64,
    })
    latest = latest.join(conc_df, on="ticker", how="left")

    if use_concentration:
        latest = latest.filter(
            pl.col("concentration_20d").is_not_null()
            & pl.col("concentration_20d").ge(conc_min)
        )
        if conc_5d_min > 0.0:
            latest = latest.filter(
                pl.col("concentration_5d").is_not_null()
                & pl.col("concentration_5d").ge(conc_5d_min)
            )

    # Select and round output columns
    out_cols = ["ticker", "date", "close", "open", "high", "low", "volume",
                "ma", "bb_upper", "bb_lower", "vol_ratio", "rsi",
                "concentration_5d", "concentration_20d"]
    latest = (
        latest
        .select(out_cols)
        .with_columns([
            pl.col("close").round(2),
            pl.col("ma").round(2),
            pl.col("bb_upper").round(2),
            pl.col("bb_lower").round(2),
            pl.col("vol_ratio").round(2),
            pl.col("rsi").round(1),
        ])
        .sort("vol_ratio", descending=True)
        .head(top_n)
    )

    return latest


def get_ticker_summary(tickers: list[str]) -> pl.DataFrame:
    """Return last close, day%, 5d%, 10d%, 20d% for each ticker (reads from R2)."""
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
    """Return ticker → stock_name. Reads R2-synced parquet; falls back to local CSV."""
    _empty = pl.DataFrame(schema={"ticker": pl.Utf8, "name": pl.Utf8})
    if not tickers:
        return _empty

    ticker_set = set(tickers)

    # Primary: local parquet cache (synced from R2 meta/ticker_info.parquet)
    cache_path = PARQUET_CACHE_PATH / "meta" / "ticker_info.parquet"
    if cache_path.exists():
        df = pl.read_parquet(cache_path)
        return (
            df.filter(pl.col("stock_id").is_in(ticker_set))
            .select(pl.col("stock_id").alias("ticker"), pl.col("stock_name").alias("name"))
            .unique("ticker")
        )

    # Fallback: local CSV from Trading repo
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


def get_kline(ticker: str, ma_window: int = 10, bb_window: int = 22) -> pl.DataFrame:
    """Return full OHLCV + MA + Bollinger history for a single ticker (reads from R2)."""
    df = download_parquet(f"tickers/{ticker}.parquet")
    if df is None or df.is_empty():
        return pl.DataFrame()

    df = df.sort("date")
    df = df.with_columns(pl.lit(ticker).alias("ticker"))
    df = _compute_indicators(df, ma_window=ma_window, bb_window=bb_window, rsi_period=14)
    df = df.drop(["ticker"])

    return df.select(["date", "open", "high", "low", "close", "volume", "ma", "bb_upper", "bb_lower", "rsi"])
