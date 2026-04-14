"""
K-line quantitative screener: DuckDB reads CSVs, Polars computes indicators.
Filters: MA, Bollinger Band, Volume Ratio, RSI, Concentration.
"""
import duckdb
import polars as pl
from app.config import PRICE_ADJ_PATH, CONCENTRATION_PATH, DB_PATH


def get_conn() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH))


def _compute_indicators(df: pl.DataFrame, ma_window: int, rsi_period: int) -> pl.DataFrame:
    """Compute MA, Bollinger, vol_ratio, RSI per ticker using Polars window ops."""
    df = df.sort(["ticker", "date"])

    # MA + Bollinger
    df = df.with_columns([
        pl.col("close").rolling_mean(window_size=ma_window).over("ticker").alias("ma"),
        pl.col("close").rolling_std(window_size=ma_window).over("ticker").alias("ma_std"),
        pl.col("volume").rolling_mean(window_size=ma_window).over("ticker").alias("avg_vol"),
    ])
    df = df.with_columns([
        (pl.col("ma") + 2.0 * pl.col("ma_std")).alias("bb_upper"),
        (pl.col("ma") - 2.0 * pl.col("ma_std")).alias("bb_lower"),
        (pl.col("volume") / pl.col("avg_vol").replace(0, None)).alias("vol_ratio"),
    ])

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
    ma_window: int = 20,
    volume_ratio: float = 1.5,
    price_above_ma: bool = True,
    bb_breakout: bool = False,
    rsi_period: int = 14,
    rsi_min: float = 0.0,
    rsi_max: float = 100.0,
    use_concentration: bool = False,
    conc_min: float = 0.0,
    top_n: int = 50,
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
    lookback = max(ma_window, rsi_period) + 10
    csv_glob = str(PRICE_ADJ_PATH / "*.csv")

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
    """

    with get_conn() as conn:
        df = conn.execute(fetch_query).pl()

    if df.is_empty():
        return pl.DataFrame()

    # Polars: compute indicators
    df = _compute_indicators(df, ma_window, rsi_period)

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

    # Optional: join concentration_20d
    if use_concentration:
        conc_glob = str(CONCENTRATION_PATH / "*.csv")
        conc_query = f"""
        WITH ranked AS (
            SELECT
                regexp_extract(filename, '([^/]+)\\.csv$', 1) AS ticker,
                CAST(date AS DATE) AS date,
                CAST(concentration_20d AS DOUBLE) AS concentration_20d,
                ROW_NUMBER() OVER (PARTITION BY filename ORDER BY date DESC) AS rn
            FROM read_csv_auto('{conc_glob}', filename=true)
        )
        SELECT ticker, concentration_20d
        FROM ranked
        WHERE rn = 1
        """
        with get_conn() as conn:
            conc_df = conn.execute(conc_query).pl()

        latest = latest.join(conc_df, on="ticker", how="left")
        latest = latest.filter(
            pl.col("concentration_20d").is_not_null()
            & pl.col("concentration_20d").ge(conc_min)
        )
    else:
        latest = latest.with_columns(pl.lit(None).cast(pl.Float64).alias("concentration_20d"))

    # Select and round output columns
    out_cols = ["ticker", "date", "close", "open", "high", "low", "volume",
                "ma", "bb_upper", "bb_lower", "vol_ratio", "rsi", "concentration_20d"]
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
