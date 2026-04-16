"""
K-line quantitative screener: DuckDB reads CSVs, Polars computes indicators.
Filters: MA, Bollinger Band, Volume Ratio, RSI, Concentration.
"""
import duckdb
import polars as pl
from app.config import PRICE_ADJ_PATH, CONCENTRATION_PATH, MARKET_VALUE_PATH, DB_PATH, TICKER_INFO_PATH


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
    csv_glob = str(PRICE_ADJ_PATH / "*.csv")

    # Market cap pre-filter: narrow universe to top N by market value
    if market_cap_rank is not None:
        mc_files = sorted(MARKET_VALUE_PATH.glob("*.csv"))
        if mc_files:
            latest_mc = str(mc_files[-1])
            mc_query = f"""
            SELECT stock_id AS ticker
            FROM read_csv_auto('{latest_mc}')
            ORDER BY CAST(market_value AS DOUBLE) DESC
            LIMIT {market_cap_rank}
            """
            with get_conn() as conn:
                mc_df = conn.execute(mc_query).pl()
            mc_tickers = mc_df["ticker"].to_list()
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

    # DuckDB: read CSVs, extract last `lookback` rows per ticker
    # Column map: max→high, min→low, Trading_Volume→volume
    fetch_query = f"""
    WITH ranked AS (
        SELECT
            regexp_extract(filename, '([^/]+)\\.csv$', 1) AS ticker,
            CAST(date AS DATE) AS date,
            CAST(open AS DOUBLE)           AS open,
            CAST("max" AS DOUBLE)          AS high,
            CAST("min" AS DOUBLE)          AS low,
            CAST(close AS DOUBLE)          AS close,
            CAST("Trading_Volume" AS DOUBLE) AS volume,
            ROW_NUMBER() OVER (
                PARTITION BY filename ORDER BY date DESC
            ) AS rn
        FROM read_csv_auto('{csv_glob}', filename=true)
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

    # Keep only latest row per ticker
    latest = (
        df.sort(["ticker", "date"])
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

    # Always join concentration data; checkbox only controls filtering
    conc_glob = str(CONCENTRATION_PATH / "*.csv")
    conc_query = f"""
    WITH ranked AS (
        SELECT
            regexp_extract(filename, '([^/]+)\\.csv$', 1) AS ticker,
            CAST(concentration_5d AS DOUBLE) AS concentration_5d,
            CAST(concentration_20d AS DOUBLE) AS concentration_20d,
            ROW_NUMBER() OVER (PARTITION BY filename ORDER BY date DESC) AS rn
        FROM read_csv_auto('{conc_glob}', filename=true)
    )
    SELECT ticker, concentration_5d, concentration_20d
    FROM ranked
    WHERE rn = 1
    """
    with get_conn() as conn:
        conc_df = conn.execute(conc_query).pl()

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
    """
    Return last close, day%, 5d%, 10d%, 20d% for each ticker.
    Reads only the relevant CSV files (no full-market scan).
    """
    if not tickers:
        return pl.DataFrame(schema={
            "ticker": pl.Utf8, "close": pl.Float64,
            "day_pct": pl.Float64, "pct_5d": pl.Float64,
            "pct_10d": pl.Float64, "pct_20d": pl.Float64,
        })

    # Only read CSVs that exist
    paths = [str(PRICE_ADJ_PATH / f"{t}.csv") for t in tickers
             if (PRICE_ADJ_PATH / f"{t}.csv").exists()]
    if not paths:
        return pl.DataFrame(schema={
            "ticker": pl.Utf8, "close": pl.Float64,
            "day_pct": pl.Float64, "pct_5d": pl.Float64,
            "pct_10d": pl.Float64, "pct_20d": pl.Float64,
        })

    paths_lit = ", ".join(f"'{p}'" for p in paths)
    lookback = 22  # need 21 rows for 20d change

    fetch_query = f"""
    WITH ranked AS (
        SELECT
            regexp_extract(filename, '([^/]+)\\.csv$', 1) AS ticker,
            CAST(date AS DATE) AS date,
            CAST(close AS DOUBLE) AS close,
            ROW_NUMBER() OVER (PARTITION BY filename ORDER BY date DESC) AS rn
        FROM read_csv_auto([{paths_lit}], filename=true)
    )
    SELECT ticker, date, close
    FROM ranked
    WHERE rn <= {lookback}
    ORDER BY ticker, date
    """

    with get_conn() as conn:
        df = conn.execute(fetch_query).pl()

    if df.is_empty():
        return df

    rows = []
    for group in df.partition_by("ticker", maintain_order=False):
        ticker = group["ticker"][0]
        closes = group.sort("date")["close"].to_list()
        n = len(closes)
        last = closes[-1]

        def _pct(back: int):
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

    return pl.DataFrame(rows)


def get_ticker_names(tickers: list[str]) -> pl.DataFrame:
    """Return ticker → stock_name from ticker_info.csv (latest row per ticker)."""
    if not tickers:
        return pl.DataFrame(schema={"ticker": pl.Utf8, "name": pl.Utf8})

    escaped = ", ".join(f"'{t}'" for t in tickers)
    info_path = str(TICKER_INFO_PATH)

    query = f"""
    SELECT stock_id AS ticker, stock_name AS name
    FROM read_csv_auto('{info_path}')
    WHERE stock_id IN ({escaped})
    QUALIFY ROW_NUMBER() OVER (PARTITION BY stock_id ORDER BY date DESC) = 1
    """

    with get_conn() as conn:
        return conn.execute(query).pl()


def get_kline(ticker: str, lookback: int = 120, ma_window: int = 10, bb_window: int = 22) -> pl.DataFrame:
    """Return OHLCV + MA + Bollinger for a single ticker."""
    csv_path = str(PRICE_ADJ_PATH / f"{ticker}.csv")

    fetch_query = f"""
    WITH ranked AS (
        SELECT
            CAST(date AS DATE)               AS date,
            CAST(open AS DOUBLE)             AS open,
            CAST("max" AS DOUBLE)            AS high,
            CAST("min" AS DOUBLE)            AS low,
            CAST(close AS DOUBLE)            AS close,
            CAST("Trading_Volume" AS DOUBLE) AS volume,
            ROW_NUMBER() OVER (ORDER BY date DESC) AS rn
        FROM read_csv_auto('{csv_path}')
    )
    SELECT date, open, high, low, close, volume
    FROM ranked
    WHERE rn <= {lookback}
    ORDER BY date
    """

    with get_conn() as conn:
        df = conn.execute(fetch_query).pl()

    if df.is_empty():
        return df

    df = df.with_columns(pl.lit(ticker).alias("ticker"))
    df = _compute_indicators(df, ma_window=ma_window, bb_window=bb_window, rsi_period=14)
    df = df.drop(["ticker"])

    return df.select(["date", "open", "high", "low", "close", "volume", "ma", "bb_upper", "bb_lower", "rsi"])
