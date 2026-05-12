"""
Backfill concentration one ticker at a time.

Rules:
  - Start from max(2021-07-01, listing_date from meta/ticker_info.csv)
  - Resume from the last saved `next_date` checkpoint when available
  - If the checkpoint is already past the latest target day, skip the ticker
  - Fetch TaiwanStockTradingDailyReport one day at a time with asyncio/aiohttp
  - If FinMind returns 429, pause the whole backfill for one hour and retry
  - Compute top-15 net buy / net sell and concentration
  - Append and upload the ticker parquet back to R2

Usage:
  python -m scripts.backfill_concentration
  python -m scripts.backfill_concentration --ticker-start 1101 --ticker-end 9999
  python -m scripts.backfill_concentration --reset
  python -m scripts.backfill_concentration --concurrency 6000
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import boto3
import aiohttp
import polars as pl
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
FINMIND_REPORT_URL = "https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report"
BACKFILL_START = "2026-01-01"
RATE_LIMIT_SLEEP = 3600
CONNECT_RETRY_SLEEP = 30
MAX_IN_FLIGHT_REQUESTS = 10

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


class AsyncRateLimiter:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._pause_until = 0.0

    async def wait(self) -> None:
        while True:
            async with self._lock:
                pause_until = self._pause_until
            now = time.time()
            if now >= pause_until:
                return
            await asyncio.sleep(min(60, pause_until - now))

    async def trip(self, seconds: int) -> None:
        async with self._lock:
            self._pause_until = max(self._pause_until, time.time() + seconds)


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


async def _finmind_get_async(
    session: aiohttp.ClientSession,
    dataset: str,
    start_date: str,
    end_date: str,
    stock_id: str,
    rate_limiter: AsyncRateLimiter,
    request_sem: asyncio.Semaphore,
) -> list[dict]:
    while True:
        await rate_limiter.wait()
        async with request_sem:
            try:
                async with session.get(
                    FINMIND_DATA_URL,
                    params={
                        "dataset": dataset,
                        "data_id": stock_id,
                        "start_date": start_date,
                        "end_date": end_date,
                        "token": FINMIND_TOKEN,
                    },
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            except aiohttp.ClientResponseError as e:
                if e.status in (402, 429):
                    wake_at = datetime.now() + timedelta(seconds=RATE_LIMIT_SLEEP)
                    print(f"\n  [RATE LIMIT {e.status}] sleeping 1h -> resuming at {wake_at.strftime('%H:%M:%S')}\n")
                    await rate_limiter.trip(RATE_LIMIT_SLEEP)
                    continue
                if e.status == 422:
                    # Historical dataset not yet indexed for recent dates; fall back to report endpoint
                    try:
                        async with session.get(
                            FINMIND_REPORT_URL,
                            params={"data_id": stock_id, "date": start_date, "token": FINMIND_TOKEN},
                            timeout=aiohttp.ClientTimeout(total=120),
                        ) as r2:
                            r2.raise_for_status()
                            d2 = await r2.json()
                    except aiohttp.ClientResponseError as e2:
                        if e2.status in (402, 429):
                            wake_at = datetime.now() + timedelta(seconds=RATE_LIMIT_SLEEP)
                            print(f"\n  [RATE LIMIT {e2.status}] sleeping 1h -> resuming at {wake_at.strftime('%H:%M:%S')}\n")
                            await rate_limiter.trip(RATE_LIMIT_SLEEP)
                            continue
                        raise
                    if d2.get("status") == 200:
                        return d2.get("data", [])
                    return []
                if e.status in (502, 503, 504):
                    print(f"\n  [RETRY {e.status}] {stock_id} {start_date}: sleeping {CONNECT_RETRY_SLEEP}s")
                    await asyncio.sleep(CONNECT_RETRY_SLEEP)
                    continue
                raise
            except (aiohttp.ClientConnectorError, aiohttp.ClientOSError, aiohttp.ServerDisconnectedError, asyncio.TimeoutError) as e:
                print(f"\n  [CONNECT RETRY] {stock_id} {start_date} -> {end_date}: {e}")
                await asyncio.sleep(CONNECT_RETRY_SLEEP)
                continue
        status = data.get("status")
        msg = data.get("msg", "")

        if status == 200:
            return data.get("data", [])

        if status in (402, 429) or "over" in msg.lower() or "limit" in msg.lower():
            wake_at = datetime.now() + timedelta(seconds=RATE_LIMIT_SLEEP)
            print(f"\n  [RATE LIMIT] {msg}")
            print(f"  Sleeping 1h -> resuming at {wake_at.strftime('%H:%M:%S')}\n")
            await rate_limiter.trip(RATE_LIMIT_SLEEP)
            continue

        raise RuntimeError(f"FinMind status={status} msg={msg}")


async def _fetch_daily_records_async(
    session: aiohttp.ClientSession,
    day: str,
    stock_id: str,
    rate_limiter: AsyncRateLimiter,
    request_sem: asyncio.Semaphore,
) -> tuple[str, list[dict]]:
    records = await _finmind_get_async(
        session,
        "TaiwanStockTradingDailyReport",
        day,
        day,
        stock_id,
        rate_limiter,
        request_sem,
    )
    return day, records


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


def _listing_start(ticker: str) -> str:
    listing_date = get_listing_date(ticker)
    if listing_date is None:
        return BACKFILL_START
    return max(BACKFILL_START, listing_date)


def _next_day(day_s: str) -> str:
    return str(datetime.strptime(day_s, "%Y-%m-%d").date() + timedelta(days=1))


def _iter_days(start: str, end: str):
    current = datetime.strptime(start, "%Y-%m-%d").date()
    stop = datetime.strptime(end, "%Y-%m-%d").date()
    while current <= stop:
        if current.weekday() < 5:
            yield str(current)
        current += timedelta(days=1)


async def backfill_ticker_async(
    ticker: str,
    progress_file: Path,
    progress: dict,
    end_date: str,
    concurrency: int,
) -> None:
    start_date = _listing_start(ticker)
    end_date_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
    batch_size = max(1, concurrency)
    request_limit = min(batch_size, MAX_IN_FLIGHT_REQUESTS)

    print(f"  [R2] downloading existing parquet ...")
    existing = _r2_download(ticker)
    existing_rows = len(existing) if existing is not None else 0
    print(f"  [R2] existing: {existing_rows} rows")
    combined = _strip_rolling(existing) if existing is not None else None
    state = progress.get(ticker, {})
    resume_from = state.get("next_date") if isinstance(state, dict) else None
    if not resume_from:
        resume_from = start_date

    resume_from_dt = datetime.strptime(resume_from, "%Y-%m-%d").date()
    if resume_from_dt > end_date_dt:
        final_rows = len(combined) if combined is not None else existing_rows
        _mark_ticker(
            progress_file,
            progress,
            ticker,
            status="done",
            start_date=start_date,
            next_date=_next_day(end_date),
            through=end_date,
            rows=final_rows,
            updated_at=str(date.today()),
        )
        print(f"  [update] start: {start_date}  resume: {resume_from}  already up to date")
        return

    print(f"  [update] start: {start_date}  resume: {resume_from}  existing: {existing_rows} rows")
    _mark_ticker(
        progress_file,
        progress,
        ticker,
        status="running",
        start_date=start_date,
        next_date=resume_from,
        through=end_date,
        rows=existing_rows,
        updated_at=str(date.today()),
    )

    timeout = aiohttp.ClientTimeout(total=120)
    connector = aiohttp.TCPConnector(limit=request_limit, limit_per_host=request_limit, ttl_dns_cache=300)
    request_sem = asyncio.Semaphore(request_limit)
    rate_limiter = AsyncRateLimiter()

    days = list(_iter_days(resume_from, end_date))
    print(f"  [FinMind] fetching {len(days)} days (async x{batch_size}, in-flight capped at {request_limit})")

    last_completed_day = None
    fetched_days = 0
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            tasks = [
                asyncio.create_task(
                    _fetch_daily_records_async(
                        session,
                        day_s,
                        ticker,
                        rate_limiter,
                        request_sem,
                    )
                )
                for day_s in days
            ]

            for completed in asyncio.as_completed(tasks):
                day_s, records = await completed
                new_rows = _compute_daily_row(records)
                if new_rows is None or new_rows.is_empty():
                    print(f"    [FinMind] {day_s} -> no trader data")
                else:
                    combined = _merge_daily(combined, new_rows)
                    fetched_days += 1
                    print(f"    [FinMind] {day_s} -> fetched")
                last_completed_day = day_s

    except Exception as e:
        if combined is not None:
            print(f"  [R2] uploading partial data ({len(combined)} rows) before stopping ...")
            _r2_upload(ticker, combined)
            print(f"  [R2] partial upload done")
        partial_next = _next_day(last_completed_day) if last_completed_day else resume_from
        _mark_ticker(
            progress_file,
            progress,
            ticker,
            status="error",
            start_date=start_date,
            next_date=partial_next,
            through=end_date,
            rows=len(combined) if combined is not None else 0,
            error=str(e),
            updated_at=str(date.today()),
        )
        print(f"  [ERROR] {e}")
        raise

    final_rows = len(combined) if combined is not None else existing_rows
    print(f"  [update] merged: {existing_rows} existing + {fetched_days} new days -> {final_rows} total")
    if combined is not None:
        print(f"  [R2] uploading {final_rows} rows ...")
        _r2_upload(ticker, combined)
        print(f"  [R2] upload done")
    _mark_ticker(
        progress_file,
        progress,
        ticker,
        status="done",
        start_date=start_date,
        next_date=_next_day(end_date),
        through=end_date,
        rows=final_rows,
        updated_at=str(date.today()),
    )


def backfill_ticker(
    ticker: str,
    progress_file: Path,
    progress: dict,
    end_date: str,
    concurrency: int,
) -> None:
    asyncio.run(backfill_ticker_async(ticker, progress_file, progress, end_date, concurrency))


def main() -> None:
    yesterday = str(date.today() - timedelta(days=1))
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker-start", type=int, default=1101)
    parser.add_argument("--ticker-end", type=int, default=9999)
    parser.add_argument("--reset", action="store_true", help="Clear progress and start over")
    parser.add_argument("--concurrency", type=int, default=6000, help="Concurrent FinMind requests per ticker")
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
        backfill_ticker(ticker, progress_file, progress, yesterday, args.concurrency)

    done = sum(1 for t in tickers if progress.get(t, {}).get("status") == "done")
    print(f"\n=== Done: {done}/{len(tickers)} tickers complete ===")


if __name__ == "__main__":
    main()
