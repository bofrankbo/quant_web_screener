from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import pandas as pd
import polars as pl
import requests
from dotenv import load_dotenv

from app.config import PARQUET_CACHE_PATH, STOCK_UNIVERSE_PATH, TICKER_INFO_PATH, TRADING_DATA_PATH

load_dotenv()

FINMIND_API = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN") or os.environ["FINMIND_API_KEY"]

STOCK_TYPES = {"twse", "tpex"}
NON_STOCK_CATEGORIES = {
    "受益證券", "上櫃指數股票型基金(ETF)", "上櫃ETF",
    "所有證券", "ETN", "存託憑證", "ETF", "大盤", "index", "Food",
}


def _finmind_get(dataset: str, start_date: str, end_date: str) -> tuple[list[dict], str]:
    resp = requests.get(
        FINMIND_API,
        params={
            "dataset": dataset,
            "start_date": start_date,
            "end_date": end_date,
            "token": FINMIND_TOKEN,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    msg = data.get("msg", "")
    if data.get("status") != 200:
        raise RuntimeError(f"FinMind status={data.get('status')} msg={msg}")
    return data["data"], msg


def _normalize_stock_rows(df: pl.DataFrame) -> pl.DataFrame:
    stock_int = pl.col("stock_id").cast(pl.Int32)
    df = (
        df.with_columns(pl.col("stock_id").cast(pl.Utf8))
          .filter(pl.col("type").is_in(list(STOCK_TYPES)))
          .filter(~pl.col("industry_category").is_in(list(NON_STOCK_CATEGORIES)))
          .filter(pl.col("stock_id").str.contains(r"^\d{4}$"))
          .filter(stock_int >= 1101)
    )
    if "date" in df.columns:
        df = df.with_columns(pl.col("date").cast(pl.Date, strict=False))
    return df


def _load_local_ticker_history() -> pl.DataFrame | None:
    if not TICKER_INFO_PATH.exists():
        return None
    df = pl.read_csv(
        TICKER_INFO_PATH,
        null_values=["None", "null", "NULL", ""],
        try_parse_dates=True,
    )
    if df.is_empty():
        return None
    return _normalize_stock_rows(df)


def load_stock_info() -> pl.DataFrame:
    """Load the latest stock universe snapshot."""
    local = _load_local_ticker_history()
    if local is not None:
        if "date" in local.columns:
            return (
                local.sort("date")
                .group_by("stock_id", maintain_order=True)
                .agg([
                    pl.col("stock_name").last(),
                    pl.col("industry_category").last(),
                    pl.col("type").last(),
                ])
                .sort(pl.col("stock_id").cast(pl.Int32))
            )
        return (
            local.select(["stock_id", "stock_name", "industry_category", "type"])
            .unique("stock_id", keep="last")
            .sort(pl.col("stock_id").cast(pl.Int32))
        )

    from datetime import date

    today = str(date.today())
    records, _ = _finmind_get("TaiwanStockInfo", today, today)
    if not records:
        raise RuntimeError("TaiwanStockInfo returned empty data")

    df = pl.DataFrame(records)
    df = _normalize_stock_rows(df)
    return df.select(["stock_id", "stock_name", "industry_category", "type"]).unique(
        "stock_id", keep="last"
    ).sort(pl.col("stock_id").cast(pl.Int32))


def build_stock_universe(min_ticker: int = 1101, max_ticker: int | None = None) -> pl.DataFrame:
    """Return a 4-digit stock universe filtered to common stocks only."""
    df = load_stock_info()
    stock_int = pl.col("stock_id").cast(pl.Int32)
    df = df.filter(stock_int >= min_ticker)
    if max_ticker is not None:
        df = df.filter(stock_int <= max_ticker)

    rows = []
    for row in df.select(["stock_id", "stock_name", "industry_category", "type"]).to_dicts():
        ticker = row["stock_id"]
        rows.append({
            **row,
            "listing_date": get_listing_date(ticker),
        })
    out = pl.DataFrame(rows) if rows else pl.DataFrame(schema={
        "stock_id": pl.Utf8,
        "stock_name": pl.Utf8,
        "industry_category": pl.Utf8,
        "type": pl.Utf8,
        "listing_date": pl.Utf8,
    })
    return out.sort(pl.col("stock_id").cast(pl.Int32))


@lru_cache(maxsize=4096)
def get_listing_date(ticker: str) -> str | None:
    """Return the earliest known listing date for a ticker, if available."""
    price_cache = PARQUET_CACHE_PATH / "tickers" / f"{ticker}.parquet"
    if price_cache.exists():
        df = pl.read_parquet(price_cache)
        if not df.is_empty() and "date" in df.columns:
            return str(df["date"].min())

    raw_price_files = sorted((TRADING_DATA_PATH / "cache").glob(f"price_{ticker}_*.pkl"))
    if raw_price_files:
        try:
            pdf = pd.read_pickle(raw_price_files[0])
            if hasattr(pdf, "columns") and "date" in pdf.columns and not pdf.empty:
                return str(pd.to_datetime(pdf["date"]).min().date())
        except Exception:
            pass

    hist = _load_local_ticker_history()
    if hist is None or "date" not in hist.columns:
        return None
    rows = hist.filter(pl.col("stock_id") == ticker)
    if rows.is_empty():
        return None
    return str(rows["date"].min())


@lru_cache(maxsize=8)
def get_stock_tickers(min_ticker: int = 1101, max_ticker: int | None = None) -> tuple[str, ...]:
    """Return the filtered ticker universe as a cached tuple."""
    df = load_stock_info()
    stock_int = pl.col("stock_id").cast(pl.Int32)
    df = df.filter(stock_int >= min_ticker)
    if max_ticker is not None:
        df = df.filter(stock_int <= max_ticker)
    if df.is_empty():
        return ()
    return tuple(df["stock_id"].to_list())


def save_stock_universe(path: str | Path = STOCK_UNIVERSE_PATH,
                        min_ticker: int = 1101,
                        max_ticker: int | None = None,
                        df: pl.DataFrame | None = None) -> Path:
    """Write the current stock universe to CSV for later reuse."""
    path = Path(path)
    if df is None:
        df = build_stock_universe(min_ticker=min_ticker, max_ticker=max_ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_csv(path)
    return path
