"""
Pattern matching via DTW (Dynamic Time Warping).
User draws K-line pattern → find stocks whose recent price (and optionally volume)
looks most similar, across a range of window sizes.
"""
import numpy as np
import polars as pl
import duckdb
from dtaidistance import dtw

from app.config import PRICE_ADJ_PATH


def _normalize(arr: np.ndarray) -> np.ndarray:
    mn, mx = arr.min(), arr.max()
    if mx - mn < 1e-9:
        return np.zeros_like(arr, dtype=np.float64)
    return (arr - mn) / (mx - mn)


def match_pattern(
    drawn_candles: list[dict],
    window_min: int,
    window_max: int,
    use_volume: bool = False,
    top_n: int = 30,
) -> pl.DataFrame:
    """
    For each ticker, take the last `w` bars for each w in [window_min, window_max],
    normalize, compute DTW against the drawn pattern, keep the best w.
    Returns top_n tickers sorted by dtw_score ascending.
    """
    # Drawn pattern as normalized close series
    drawn_close = np.array([c["close"] for c in drawn_candles], dtype=np.float64)
    drawn_close_n = _normalize(drawn_close)

    drawn_vol_n = None
    if use_volume:
        v = np.array([c.get("volume", 0.0) for c in drawn_candles], dtype=np.float64)
        drawn_vol_n = _normalize(v)

    # Load enough bars for all window sizes
    lookback = window_max + 5
    csv_glob = str(PRICE_ADJ_PATH / "*.csv")

    query = f"""
    WITH ranked AS (
        SELECT
            regexp_extract(filename, '([^/]+)\\.csv$', 1) AS ticker,
            CAST(date AS DATE)                              AS date,
            CAST(close AS DOUBLE)                          AS close,
            CAST("Trading_Volume" AS DOUBLE)               AS volume,
            ROW_NUMBER() OVER (PARTITION BY filename ORDER BY date DESC) AS rn
        FROM read_csv_auto('{csv_glob}', filename=true)
    )
    SELECT ticker, date, close, volume
    FROM ranked
    WHERE rn <= {lookback}
    ORDER BY ticker, date
    """

    conn = duckdb.connect()
    df = conn.execute(query).pl()
    conn.close()

    if df.is_empty():
        return pl.DataFrame(schema={"ticker": pl.Utf8, "dtw_score": pl.Float64, "best_window": pl.Int32})

    results = []

    for (ticker,), group in df.group_by(["ticker"], maintain_order=False):
        group = group.sort("date")
        closes = group["close"].to_numpy().astype(np.float64)
        volumes = group["volume"].to_numpy().astype(np.float64) if use_volume else None

        best_dist = float("inf")
        best_w = window_min

        for w in range(window_min, window_max + 1):
            if len(closes) < w:
                continue

            window_close = closes[-w:]
            window_close_n = _normalize(window_close)
            dist = dtw.distance_fast(drawn_close_n, window_close_n)

            if use_volume and volumes is not None and len(volumes) >= w:
                window_vol_n = _normalize(volumes[-w:])
                vol_dist = dtw.distance_fast(drawn_vol_n, window_vol_n)
                dist = dist * 0.7 + vol_dist * 0.3

            if dist < best_dist:
                best_dist = dist
                best_w = w

        if best_dist < float("inf"):
            results.append({
                "ticker": ticker,
                "dtw_score": round(float(best_dist), 4),
                "best_window": best_w,
            })

    results.sort(key=lambda x: x["dtw_score"])
    top = results[:top_n]

    if not top:
        return pl.DataFrame(schema={"ticker": pl.Utf8, "dtw_score": pl.Float64, "best_window": pl.Int32})

    return pl.DataFrame(top)
