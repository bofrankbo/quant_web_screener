"""
Backfill concentration data across a date range.

Rate limit: ~6000 requests/hour from FinMind.
With ~1950 tickers/date → 3 dates per batch, then sleep 1 hour.
Resumable: already-fetched (ticker, date) pairs are skipped automatically.

Usage:
  python -m scripts.backfill_concentration --start 2026-01-01
  python -m scripts.backfill_concentration --start 2026-01-01 --end 2026-03-31
  python -m scripts.backfill_concentration --start 2026-01-01 --batch-size 2  # conservative
"""
import argparse
import asyncio
import time
from datetime import date, datetime, timedelta

import aiohttp
from dotenv import load_dotenv

load_dotenv()

from scripts.fetch_concentration import _get_all_tickers, daily_update_async

REQUESTS_PER_HOUR = 6000
SLEEP_SECONDS = 3600


def _trading_days(start: str, end: str) -> list[str]:
    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    days = []
    d = s
    while d <= e:
        if d.weekday() < 5:
            days.append(str(d))
        d += timedelta(days=1)
    return days


async def _fetch_tickers(concurrency: int) -> list[str]:
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=120),
        connector=aiohttp.TCPConnector(limit=concurrency),
    ) as session:
        return await _get_all_tickers(session)


def main():
    yesterday = str(date.today() - timedelta(days=1))
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=yesterday, help="End date (default: yesterday)")
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument(
        "--batch-size", type=int, default=3,
        help="Dates per batch before sleeping 1h (default: 3, ~5850 req < 6000 limit)",
    )
    args = parser.parse_args()

    days = _trading_days(args.start, args.end)
    total_batches = (len(days) + args.batch_size - 1) // args.batch_size

    print(f"=== Concentration backfill: {args.start} → {args.end} ===")
    print(f"    {len(days)} trading days | batch_size={args.batch_size} | "
          f"{total_batches} batches | ~{total_batches - 1}h sleep total")

    tickers = asyncio.run(_fetch_tickers(args.concurrency))
    print(f"    {len(tickers)} tickers\n")

    for batch_num, batch_start in enumerate(range(0, len(days), args.batch_size), 1):
        batch = days[batch_start:batch_start + args.batch_size]
        print(f"--- Batch {batch_num}/{total_batches}: {batch[0]} → {batch[-1]} ---")
        t0 = time.time()

        for d in batch:
            asyncio.run(daily_update_async(d, tickers, args.concurrency))

        elapsed = time.time() - t0
        remaining_days = days[batch_start + args.batch_size:]

        if not remaining_days:
            break

        sleep_for = max(0, SLEEP_SECONDS - elapsed)
        next_date = remaining_days[0]
        wake_at = datetime.now() + timedelta(seconds=sleep_for)
        print(f"\nBatch done in {elapsed:.0f}s. {len(remaining_days)} days left.")
        print(f"Sleeping {sleep_for / 60:.1f} min → next batch at {wake_at.strftime('%H:%M')} (next: {next_date})\n")
        time.sleep(sleep_for)

    print("\n=== Backfill complete ===")


if __name__ == "__main__":
    main()
