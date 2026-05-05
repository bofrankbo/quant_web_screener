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
  python -m scripts.backfill_concentration --concurrency 8
"""
from __future__ import annotations

import argparse
import io
import json
import os
import time
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

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


def _load_progress(progress_file: Path) -> dict:
    if progress_file.exists():
        return json.loads(progress_file.read_text())
    return {}


def _save_progress(progress_file: Path, progress: dict) -> None:
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    progress_file.write_text(json.dumps(progress, indent=2, ensure_ascii=False))


def _mark_ticker(progress_file: Path, progress: dict, ticker: str, **meta) -> None:
    progress[ticker] = meta
    _save_progress(progress_file, progress)


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pause_until = 0.0

    def wait(self) -> None:
        while True:
            with self._lock:
                pause_until = self._pause_until
            now = time.time()
            if now >= pause_until:
                return
            time.sleep(min(60, pause_until - now))

    def trip(self, seconds: int) -> None:
        with self._lock:
            self._pause_until = max(self._pause_until, time.time() + seconds)


def _missing_days(start_date: str, end_date: str, existing_dates: set[date]) -> list[date]:
    return [day for day in _iter_days(start_date, end_date) if day not in existing_dates]


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


def _finmind_get(
    dataset: str,
    start_date: str,
    end_date: str,
    stock_id: str,
    rate_limiter: RateLimiter,
) -> list[dict]:
    while True:
        rate_limiter.wait()
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
            rate_limiter.trip(RATE_LIMIT_SLEEP)
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


def _fetch_missing_day(
    ticker: str,
    day: date,
    rate_limiter: RateLimiter,
) -> tuple[date, pl.DataFrame | None]:
    day_s = str(day)
    records = _finmind_get(
        "TaiwanStockTradingDailyReport",
        day_s,
        day_s,
        ticker,
        rate_limiter,
    )
    return day, _compute_daily_row(records)


def backfill_ticker(
    ticker: str,
    progress_file: Path,
    progress: dict,
    end_date: str,
    concurrency: int,
) -> None:
    start_date = _listing_start(ticker)

    existing = _r2_download(ticker)
    existing_dates = _load_existing_dates(existing)
    missing_days = _missing_days(start_date, end_date, existing_dates)
    combined = _strip_rolling(existing) if existing is not None else None
    updated_days = 0
    rate_limiter = RateLimiter()

    print(f"  start: {start_date}  existing: {len(existing_dates)} days  missing: {len(missing_days)}")

    if not missing_days:
        _mark_ticker(
            progress_file,
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

    rows: list[pl.DataFrame] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        future_map = {
            executor.submit(_fetch_missing_day, ticker, day, rate_limiter): day
            for day in missing_days
        }
        for future in as_completed(future_map):
            day = future_map[future]
            day_s = str(day)
            _, row = future.result()
            if row is None or row.is_empty():
                print(f"    {day_s} -> no trader data")
                continue
            rows.append(row)
            updated_days += 1
            print(f"    {day_s} -> fetched")

    if rows:
        new_rows = pl.concat(rows).sort("date")
        combined = _merge_daily(combined, new_rows)
        _r2_upload(ticker, combined)

    final_rows = len(combined) if combined is not None else (len(existing) if existing is not None else 0)
    _mark_ticker(
        progress_file,
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
    parser.add_argument("--concurrency", type=int, default=8, help="Concurrent FinMind requests per ticker")
    args = parser.parse_args()

    progress_file = PROGRESS_FILE

    if args.reset and progress_file.exists():
        progress_file.unlink()
        print(f"Progress file cleared: {progress_file}")

    progress = _load_progress(progress_file)
    tickers = [
        t for t in get_stock_tickers(args.ticker_start, args.ticker_end)
        if args.ticker_start <= int(t) <= args.ticker_end
    ]

    print(f"=== Concentration backfill: {args.ticker_start} -> {args.ticker_end} ===")
    print(f"    target range: {BACKFILL_START} -> {yesterday}")
    print(f"    tickers: {len(tickers)}")
    print(f"    progress: {progress_file}\n")

    for i, ticker in enumerate(tickers, 1):
        prefix = f"[{i:4d}/{len(tickers)}] {ticker}"
        state = progress.get(ticker, {})
        if state.get("status") == "done" and state.get("next_date", "") > yesterday:
            print(f"{prefix} done ({state.get('rows', 0)} rows, through {state.get('through', '-')})")
            continue

        print(prefix)
        try:
            backfill_ticker(ticker, progress_file, progress, yesterday, args.concurrency)
        except Exception as e:
            _mark_ticker(
                progress_file,
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
