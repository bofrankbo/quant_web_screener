"""
Backfill concentration one ticker at a time, one day at a time.

Rules:
  - Start from max(2021-07-01, listing_date from meta/ticker_info.csv)
  - For each date, if concentration already exists, skip that day
  - If not, fetch TaiwanStockTradingDailyReport for that ticker/date
  - Compute top-15 net buy / net sell and concentration
  - Append and upload the ticker parquet back to R2

Usage:
  python -m scripts.backfill_concentration
  python -m scripts.backfill_concentration --ticker-start 1101 --ticker-end 9999
  python -m scripts.backfill_concentration --reset
"""
from __future__ import annotations

import argparse
import io
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import boto3
import polars as pl
import requests
from botocore.config import Config
from dotenv import load_dotenv

from app.stock_universe import get_listing_date, get_stock_tickers

load_dotenv()

R2_BUCKET = os.environ["R2_BUCKET"]
R2_ENDPOINT = os.environ["R2_ENDPOINT"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN") or os.environ["FINMIND_API_KEY"]

FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"
BACKFILL_START = "2021-07-01"
RATE_LIMIT_SLEEP = 3600

PROGRESS_FILE = Path(__file__).parent.parent / "data" / "backfill_conc_done.json"

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4"),
    region_name="auto",
)


def _load_progress() -> dict:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text())
    return {}


def _save_progress(progress: dict) -> None:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2, ensure_ascii=False))


def _mark_ticker(progress: dict, ticker: str, **meta) -> None:
    progress[ticker] = meta
    _save_progress(progress)


def _r2_download(ticker: str) -> pl.DataFrame | None:
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=f"concentration/{ticker}.parquet")
        return pl.read_parquet(io.BytesIO(obj["Body"].read()))
    except Exception as e:
        if "NoSuchKey" in str(e) or "404" in str(e):
            return None
        raise


def _r2_upload(ticker: str, df: pl.DataFrame) -> None:
    buf = io.BytesIO()
    df.write_parquet(buf)
    buf.seek(0)
    s3.put_object(Bucket=R2_BUCKET, Key=f"concentration/{ticker}.parquet", Body=buf.getvalue())


def _finmind_get(dataset: str, start_date: str, end_date: str, stock_id: str) -> list[dict]:
    while True:
        resp = requests.get(
            FINMIND_DATA_URL,
            params={
                "dataset": dataset,
                "data_id": stock_id,
                "start_date": start_date,
                "end_date": end_date,
                "token": FINMIND_TOKEN,
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        msg = data.get("msg", "")

        if status == 200:
            return data.get("data", [])

        if status in (402, 429) or "over" in msg.lower() or "limit" in msg.lower():
            wake_at = datetime.now() + timedelta(seconds=RATE_LIMIT_SLEEP)
            print(f"\n  [RATE LIMIT] {msg}")
            print(f"  Sleeping 1h -> resuming at {wake_at.strftime('%H:%M:%S')}\n")
            time.sleep(RATE_LIMIT_SLEEP)
            continue

        raise RuntimeError(f"FinMind status={status} msg={msg}")


def _compute_daily_row(records: list[dict]) -> pl.DataFrame | None:
    if not records:
        return None

    df = (
        pl.DataFrame(records)
        .select(["date", "securities_trader_id", "buy", "sell"])
        .with_columns([
            pl.col("date").cast(pl.Date),
            (pl.col("buy") - pl.col("sell")).alias("net"),
        ])
    )

    by_broker = (
        df.group_by(["date", "securities_trader_id"])
        .agg([pl.col("buy").sum(), pl.col("net").sum()])
    )

    buyers = (
        by_broker.filter(pl.col("net") > 0)
        .with_columns(pl.col("net").rank("dense", descending=True).over("date").alias("_r"))
        .filter(pl.col("_r") <= 15)
        .group_by("date")
        .agg(pl.col("net").sum().alias("buy_volume"))
    )

    sellers = (
        by_broker.filter(pl.col("net") < 0)
        .with_columns(pl.col("net").rank("dense", descending=False).over("date").alias("_r"))
        .filter(pl.col("_r") <= 15)
        .group_by("date")
        .agg(pl.col("net").sum().alias("sell_volume"))
    )

    total = by_broker.group_by("date").agg(pl.col("buy").sum().alias("total_volume"))

    return (
        total
        .join(buyers, on="date", how="left")
        .join(sellers, on="date", how="left")
        .with_columns([
            pl.col("buy_volume").fill_null(0).cast(pl.Int64),
            pl.col("sell_volume").fill_null(0).cast(pl.Int64),
            pl.col("total_volume").cast(pl.Int64),
        ])
        .with_columns((pl.col("buy_volume") + pl.col("sell_volume")).alias("amount"))
        .sort("date")
    )


def _add_rolling(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns([
        (
            (pl.col("buy_volume").rolling_sum(5) + pl.col("sell_volume").rolling_sum(5))
            / pl.col("total_volume").rolling_sum(5)
        ).alias("concentration_5d"),
        (
            (pl.col("buy_volume").rolling_sum(20) + pl.col("sell_volume").rolling_sum(20))
            / pl.col("total_volume").rolling_sum(20)
        ).alias("concentration_20d"),
    ])


def _strip_rolling(df: pl.DataFrame) -> pl.DataFrame:
    return df.drop(["concentration_5d", "concentration_20d"], strict=False)


def _merge_daily(existing: pl.DataFrame | None, new_row: pl.DataFrame) -> pl.DataFrame:
    if existing is None or existing.is_empty():
        return _add_rolling(new_row.sort("date"))
    combined = (
        pl.concat([_strip_rolling(existing), _strip_rolling(new_row)])
        .unique(subset=["date"])
        .sort("date")
    )
    return _add_rolling(combined)


def _iter_days(start: str, end: str):
    current = datetime.strptime(start, "%Y-%m-%d").date()
    stop = datetime.strptime(end, "%Y-%m-%d").date()
    while current <= stop:
        if current.weekday() < 5:
            yield current
        current += timedelta(days=1)


def _load_existing_dates(df: pl.DataFrame | None) -> set[date]:
    if df is None or df.is_empty() or "date" not in df.columns:
        return set()
    return set(df["date"].cast(pl.Date).to_list())


def _listing_start(ticker: str) -> str:
    listing_date = get_listing_date(ticker)
    if listing_date is None:
        return BACKFILL_START
    return max(BACKFILL_START, listing_date)


def backfill_ticker(ticker: str, progress: dict, end_date: str) -> None:
    start_date = _listing_start(ticker)
    state = progress.get(ticker, {})
    cursor = state.get("next_date", start_date)
    if cursor < start_date:
        cursor = start_date

    existing = _r2_download(ticker)
    existing_dates = _load_existing_dates(existing)
    combined = _strip_rolling(existing) if existing is not None else None
    updated_days = 0

    print(f"  start: {start_date}  cursor: {cursor}  existing: {len(existing_dates)} days")

    if cursor > end_date:
        _mark_ticker(
            progress,
            ticker,
            status="done",
            start_date=start_date,
            next_date=str((datetime.strptime(end_date, "%Y-%m-%d").date() + timedelta(days=1))),
            through=end_date,
            rows=len(existing) if existing is not None else 0,
            updated_at=str(date.today()),
        )
        return

    for day in _iter_days(cursor, end_date):
        day_s = str(day)
        next_day_s = str(day + timedelta(days=1))

        if day in existing_dates:
            _mark_ticker(
                progress,
                ticker,
                status=state.get("status", "processing"),
                start_date=start_date,
                next_date=next_day_s,
                through=day_s,
                rows=len(existing) if existing is not None else 0,
                updated_at=str(date.today()),
                note="already exists in concentration",
            )
            continue

        records = _finmind_get(
            "TaiwanStockTradingDailyReport",
            day_s,
            day_s,
            ticker,
        )
        row = _compute_daily_row(records)
        if row is None or row.is_empty():
            _mark_ticker(
                progress,
                ticker,
                status=state.get("status", "processing"),
                start_date=start_date,
                next_date=next_day_s,
                through=day_s,
                rows=len(combined) if combined is not None else (len(existing) if existing is not None else 0),
                updated_at=str(date.today()),
                note="no trader data",
            )
            continue

        combined = _merge_daily(combined if combined is not None else existing, row)
        _r2_upload(ticker, combined)
        existing = combined
        existing_dates.add(day)
        updated_days += 1
        _mark_ticker(
            progress,
            ticker,
            status="processing",
            start_date=start_date,
            next_date=next_day_s,
            through=day_s,
            rows=len(combined),
            updated_at=str(date.today()),
        )
        print(f"    {day_s} -> uploaded ({len(combined)} rows)")

    final_rows = len(combined) if combined is not None else (len(existing) if existing is not None else 0)
    _mark_ticker(
        progress,
        ticker,
        status="done",
        start_date=start_date,
        next_date=str((datetime.strptime(end_date, "%Y-%m-%d").date() + timedelta(days=1))),
        through=end_date,
        rows=final_rows,
        updated_at=str(date.today()),
    )
    print(f"    updated {updated_days} days, total {final_rows} days")


def main() -> None:
    yesterday = str(date.today() - timedelta(days=1))
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker-start", type=int, default=1101)
    parser.add_argument("--ticker-end", type=int, default=9999)
    parser.add_argument("--reset", action="store_true", help="Clear progress and start over")
    args = parser.parse_args()

    if args.reset and PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        print("Progress file cleared.")

    progress = _load_progress()
    tickers = [
        t for t in get_stock_tickers(args.ticker_start, args.ticker_end)
        if args.ticker_start <= int(t) <= args.ticker_end
    ]

    print(f"=== Concentration backfill: {args.ticker_start} -> {args.ticker_end} ===")
    print(f"    target range: {BACKFILL_START} -> {yesterday}")
    print(f"    tickers: {len(tickers)}")
    print(f"    progress: {PROGRESS_FILE}\n")

    for i, ticker in enumerate(tickers, 1):
        prefix = f"[{i:4d}/{len(tickers)}] {ticker}"
        state = progress.get(ticker, {})
        if state.get("status") == "done" and state.get("next_date", "") > yesterday:
            print(f"{prefix} done ({state.get('rows', 0)} rows, through {state.get('through', '-')})")
            continue

        print(prefix)
        try:
            backfill_ticker(ticker, progress, yesterday)
        except Exception as e:
            _mark_ticker(
                progress,
                ticker,
                status="error",
                error=str(e),
                updated_at=str(date.today()),
            )
            print(f"    ERROR: {e}")

    done = sum(1 for t in tickers if progress.get(t, {}).get("status") == "done")
    print(f"\n=== Done: {done}/{len(tickers)} tickers complete ===")


if __name__ == "__main__":
    main()
