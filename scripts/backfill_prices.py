"""
Backfill adjusted OHLCV (TaiwanStockPriceAdj) per ticker → R2.

Partial overwrite: only rows in [start_date, end_date] are replaced.
Rows outside that range in the existing R2 file are preserved.

Usage:
  python -m scripts.backfill_prices
  python -m scripts.backfill_prices --start 2026-01-01 --end 2026-05-12
  python -m scripts.backfill_prices --ticker-start 2330 --ticker-end 2330
  python -m scripts.backfill_prices --reset
  python -m scripts.backfill_prices --concurrency 20
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

from app.stock_universe import get_stock_tickers

load_dotenv()

R2_BUCKET            = os.environ["R2_BUCKET"]
R2_ENDPOINT          = os.environ["R2_ENDPOINT"]
R2_ACCESS_KEY_ID     = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
FINMIND_TOKEN        = os.environ.get("FINMIND_TOKEN") or os.environ["FINMIND_API_KEY"]

FINMIND_API          = "https://api.finmindtrade.com/api/v4/data"
DEFAULT_START        = "2026-01-01"
RATE_LIMIT_SLEEP     = 3600
CONNECT_RETRY_SLEEP  = 30

PROGRESS_FILE = Path(__file__).parent.parent / "data" / "backfill_prices_done.json"

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4"),
    region_name="auto",
)


def _load_progress(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _save_progress(path: Path, progress: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(progress, indent=2, ensure_ascii=False))


def _mark(path: Path, progress: dict, ticker: str, **meta) -> None:
    progress[ticker] = meta
    _save_progress(path, progress)


class AsyncRateLimiter:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._pause_until = 0.0

    async def wait(self) -> None:
        while True:
            async with self._lock:
                pause_until = self._pause_until
            if time.time() >= pause_until:
                return
            await asyncio.sleep(min(60, pause_until - time.time()))

    async def trip(self, seconds: int) -> None:
        async with self._lock:
            self._pause_until = max(self._pause_until, time.time() + seconds)


def _r2_download(ticker: str) -> pl.DataFrame | None:
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=f"tickers/{ticker}.parquet")
        return pl.read_parquet(io.BytesIO(obj["Body"].read()))
    except Exception as e:
        if "NoSuchKey" in str(e) or "404" in str(e):
            return None
        raise


def _r2_upload(ticker: str, df: pl.DataFrame) -> None:
    buf = io.BytesIO()
    df.write_parquet(buf)
    buf.seek(0)
    s3.put_object(Bucket=R2_BUCKET, Key=f"tickers/{ticker}.parquet", Body=buf.getvalue())


def _parse_price_df(records: list[dict]) -> pl.DataFrame:
    df = (
        pl.DataFrame(records)
        .rename({"max": "high", "min": "low", "Trading_Volume": "volume"})
        .select(["date", "open", "high", "low", "close", "volume"])
        .with_columns(pl.col("date").cast(pl.Date))
    )
    for col in ["open", "high", "low", "close", "volume"]:
        df = df.with_columns(pl.col(col).cast(pl.Float64))
    return df


def _merge_price(existing: pl.DataFrame | None, new_rows: pl.DataFrame) -> pl.DataFrame:
    if existing is None or existing.is_empty():
        return new_rows.sort("date")
    combined = (
        pl.concat([existing, new_rows])
        .unique(subset=["date"], keep="last")
        .sort("date")
    )
    return combined


async def _fetch_ticker_async(
    session: aiohttp.ClientSession,
    ticker: str,
    start_date: str,
    end_date: str,
    rate_limiter: AsyncRateLimiter,
    sem: asyncio.Semaphore,
) -> list[dict]:
    while True:
        await rate_limiter.wait()
        async with sem:
            try:
                async with session.get(
                    FINMIND_API,
                    params={
                        "dataset": "TaiwanStockPriceAdj",
                        "data_id": ticker,
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
                if e.status in (502, 503, 504):
                    print(f"\n  [RETRY {e.status}] {ticker}: sleeping {CONNECT_RETRY_SLEEP}s")
                    await asyncio.sleep(CONNECT_RETRY_SLEEP)
                    continue
                raise
            except (aiohttp.ClientConnectorError, aiohttp.ClientOSError,
                    aiohttp.ServerDisconnectedError, asyncio.TimeoutError) as e:
                print(f"\n  [CONNECT RETRY] {ticker}: {e}")
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


async def backfill_all_async(
    tickers: list[str],
    start_date: str,
    end_date: str,
    progress_file: Path,
    progress: dict,
    concurrency: int,
) -> None:
    sem = asyncio.Semaphore(concurrency)
    rate_limiter = AsyncRateLimiter()
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=120)

    async def process(i: int, ticker: str) -> None:
        prefix = f"[{i:4d}/{len(tickers)}] {ticker}"
        state = progress.get(ticker, {})
        if state.get("status") == "done" and state.get("end_date", "") >= end_date:
            print(f"{prefix} done (through {state.get('end_date', '-')}, {state.get('rows', 0)} rows)")
            return

        print(f"{prefix} [FinMind] fetching {start_date} -> {end_date} ...")
        _mark(progress_file, progress, ticker, status="running", start_date=start_date,
              end_date=end_date, updated_at=str(date.today()))
        try:
            records = await _fetch_ticker_async(
                session, ticker, start_date, end_date, rate_limiter, sem
            )
            if not records:
                print(f"{prefix} [FinMind] no data returned")
                _mark(progress_file, progress, ticker, status="empty",
                      start_date=start_date, end_date=end_date, updated_at=str(date.today()))
                return

            new_rows = _parse_price_df(records)
            print(f"{prefix} [FinMind] got {len(new_rows)} rows  [{new_rows['date'].min()} -> {new_rows['date'].max()}]")

            print(f"{prefix} [R2] downloading existing parquet ...")
            existing = _r2_download(ticker)
            existing_rows = len(existing) if existing is not None else 0
            print(f"{prefix} [R2] existing: {existing_rows} rows")

            merged = _merge_price(existing, new_rows)
            print(f"{prefix} [update] merged: {existing_rows} existing + {len(new_rows)} new -> {len(merged)} total  (overwrite range: {new_rows['date'].min()} -> {new_rows['date'].max()})")

            print(f"{prefix} [R2] uploading {len(merged)} rows ...")
            _r2_upload(ticker, merged)
            print(f"{prefix} [R2] upload done")

            _mark(progress_file, progress, ticker, status="done",
                  start_date=start_date, end_date=end_date,
                  rows=len(merged), updated_at=str(date.today()))
        except Exception as e:
            print(f"{prefix} [ERROR] {e}")
            _mark(progress_file, progress, ticker, status="error",
                  start_date=start_date, end_date=end_date,
                  error=str(e), updated_at=str(date.today()))

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        tasks = [asyncio.create_task(process(i, t)) for i, t in enumerate(tickers, 1)]
        await asyncio.gather(*tasks)


def main() -> None:
    yesterday = str(date.today() - timedelta(days=1))
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=DEFAULT_START, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=yesterday, help="End date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--ticker-start", default="1101", help="First ticker (inclusive)")
    parser.add_argument("--ticker-end", default="9999", help="Last ticker (inclusive)")
    parser.add_argument("--concurrency", type=int, default=20, help="Parallel FinMind requests")
    parser.add_argument("--reset", action="store_true", help="Clear progress file")
    args = parser.parse_args()

    if args.reset and PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        print(f"Progress file cleared: {PROGRESS_FILE}")

    progress = _load_progress(PROGRESS_FILE)
    tickers = list(get_stock_tickers(int(args.ticker_start), int(args.ticker_end)))

    print(f"=== Price backfill (partial overwrite): {args.start} -> {args.end} ===")
    print(f"    tickers: {len(tickers)}  concurrency: {args.concurrency}")
    print(f"    progress: {PROGRESS_FILE}\n")

    asyncio.run(backfill_all_async(
        tickers, args.start, args.end, PROGRESS_FILE, progress, args.concurrency
    ))

    done  = sum(1 for t in tickers if progress.get(t, {}).get("status") == "done")
    empty = sum(1 for t in tickers if progress.get(t, {}).get("status") == "empty")
    err   = sum(1 for t in tickers if progress.get(t, {}).get("status") == "error")
    print(f"\n=== Done: {done} ok / {empty} empty / {err} errors out of {len(tickers)} ===")


if __name__ == "__main__":
    main()
