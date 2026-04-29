"""
Fetch broker concentration data → Cloudflare R2

Daily mode:
  - fetch all listed + OTC common stocks
  - fetch only the target date
  - compute the day's top-15 net buyers / net sellers
  - append one daily row per ticker to concentration/{ticker}.parquet

Concentration formula (top-15 brokers):
  buy_volume  = sum of top-15 net-buyers' net shares
  sell_volume = sum of top-15 net-sellers' net shares
  total_volume = sum of all brokers' buy shares
  concentration_Nd = rolling_N(buy_volume + sell_volume) / rolling_N(total_volume)

R2 layout:
  concentration/{ticker}.parquet

Usage:
  python -m scripts.fetch_concentration --backfill          # upload all local CSVs → R2
  python -m scripts.fetch_concentration [--date YYYY-MM-DD] # daily FinMind update (default: today)
  python -m scripts.fetch_concentration --tickers 2330 2454 # limit to specific tickers
"""
import asyncio
import argparse
import io
import os
from datetime import date
from pathlib import Path

import boto3
import aiohttp
import polars as pl
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv()

LOCAL_CONC_DIR = Path(
    os.getenv("TRADING_DATA_PATH",
              "/Users/yanyifu/Documents/_Coding/Trading/history_data/tw")
) / "concentration" / "self_calculate"

R2_ACCOUNT_ID        = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID     = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET            = os.environ["R2_BUCKET"]
R2_ENDPOINT          = os.environ["R2_ENDPOINT"]
FINMIND_TOKEN        = os.environ.get("FINMIND_TOKEN") or os.environ["FINMIND_API_KEY"]

FINMIND_REPORT_URL = "https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report"
FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"

NOT_STOCK_CATEGORIES = {
    "受益證券", "上櫃指數股票型基金(ETF)", "上櫃ETF",
    "所有證券", "ETN", "存託憑證", "ETF", "大盤", "index", "Food",
}

CONC_COLS = ["date", "total_volume", "buy_volume", "sell_volume", "amount",
             "concentration_5d", "concentration_20d"]

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4"),
    region_name="auto",
)


# --- R2 helpers ---
def _download_parquet(key: str) -> pl.DataFrame | None:
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=key)
        return pl.read_parquet(io.BytesIO(obj["Body"].read()))
    except Exception as e:
        if "NoSuchKey" in str(e) or "404" in str(e):
            return None
        raise


def _upload_parquet(key: str, df: pl.DataFrame) -> None:
    buf = io.BytesIO()
    df.write_parquet(buf)
    buf.seek(0)
    s3.put_object(Bucket=R2_BUCKET, Key=key, Body=buf.getvalue())


async def _finmind_get_async(session: aiohttp.ClientSession, dataset: str,
                             start_date: str, end_date: str,
                             stock_id: str | None = None) -> tuple[list[dict], str]:
    params = {
        "dataset": dataset,
        "start_date": start_date,
        "end_date": end_date,
        "token": FINMIND_TOKEN,
    }
    if stock_id:
        params["data_id"] = stock_id
    async with session.get(FINMIND_DATA_URL, params=params, timeout=aiohttp.ClientTimeout(total=60)) as resp:
        resp.raise_for_status()
        data = await resp.json()
    msg = data.get("msg", "")
    if data.get("status") != 200:
        raise RuntimeError(f"FinMind status={data.get('status')} msg={msg}")
    return data["data"], msg


async def _get_all_tickers(session: aiohttp.ClientSession) -> list[str]:
    today = str(date.today())
    records, _ = await _finmind_get_async(session, "TaiwanStockInfo", today, today)
    if not records:
        raise RuntimeError("TaiwanStockInfo returned empty data")
    df = pl.DataFrame(records)
    df = df.filter(pl.col("type").is_in(["twse", "tpex"]))
    df = df.filter(~pl.col("industry_category").is_in(list(NOT_STOCK_CATEGORIES)))
    df = df.with_columns(pl.col("stock_id").cast(pl.Utf8))
    df = df.filter(pl.col("stock_id").str.len_chars() <= 4)
    return sorted(df["stock_id"].unique().to_list())


# --- Concentration calculation ---
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


def _compute_one_day(records: list[dict]) -> dict | None:
    """Compute one day's concentration from raw broker records."""
    if not records:
        return None

    df = pl.DataFrame(records).select(["date", "securities_trader_id", "buy", "sell"])
    df = df.with_columns((pl.col("buy") - pl.col("sell")).alias("net"))

    by_broker = (
        df.group_by(["date", "securities_trader_id"])
        .agg([pl.col("buy").sum(), pl.col("net").sum()])
    )

    target_date = by_broker["date"][0]

    buy_vol = (
        by_broker.filter(pl.col("net") > 0)
        .sort("net", descending=True)
        .head(15)
        ["net"].sum()
    )
    sell_vol = (
        by_broker.filter(pl.col("net") < 0)
        .sort("net")
        .head(15)
        ["net"].sum()
    )
    total_vol = by_broker["buy"].sum()

    return {
        "date": target_date,
        "total_volume": int(total_vol),
        "buy_volume": int(buy_vol),
        "sell_volume": int(sell_vol),
        "amount": int(buy_vol + sell_vol),
    }


async def _fetch_trader_report_async(session: aiohttp.ClientSession, ticker: str,
                                     target_date: str) -> list[dict]:
    params = {
        "data_id": ticker,
        "date": target_date,
        "token": FINMIND_TOKEN,
    }
    async with session.get(FINMIND_REPORT_URL, params=params, timeout=aiohttp.ClientTimeout(total=60)) as resp:
        resp.raise_for_status()
        data = await resp.json()
    if data.get("status") != 200:
        raise RuntimeError(f"FinMind error [{ticker}]: {data.get('msg')}")
    return data["data"]


# --- Backfill: local CSV → R2 ---
def backfill_from_local(tickers: list[str] | None = None) -> None:
    csv_files = sorted(LOCAL_CONC_DIR.glob("*.csv"))
    if tickers:
        ticker_set = set(tickers)
        csv_files = [f for f in csv_files if f.stem in ticker_set]

    print(f"Backfill: {len(csv_files)} local CSVs → R2 concentration/")

    for i, csv_path in enumerate(csv_files, 1):
        ticker = csv_path.stem
        key = f"concentration/{ticker}.parquet"

        try:
            df = pl.read_csv(csv_path, try_parse_dates=True)
            # Ensure all expected columns exist
            for col in ["concentration_5d", "concentration_20d"]:
                if col not in df.columns:
                    df = df.with_columns(pl.lit(None).cast(pl.Float64).alias(col))
            df = df.select([c for c in CONC_COLS if c in df.columns]).sort("date")

            # Recalculate rolling windows over full history
            df = _add_rolling(df.drop(["concentration_5d", "concentration_20d"]))

            _upload_parquet(key, df)
            print(f"[{i:4d}/{len(csv_files)}] {ticker} ✓  {len(df)} rows → {df['date'].max()}")
        except Exception as e:
            print(f"[{i:4d}/{len(csv_files)}] {ticker} ✗  {e}")


async def _process_ticker(session: aiohttp.ClientSession, ticker: str, target_date: str,
                          sem: asyncio.Semaphore) -> tuple[str, str]:
    key = f"concentration/{ticker}.parquet"
    async with sem:
        try:
            existing_task = asyncio.to_thread(_download_parquet, key)
            report_task = _fetch_trader_report_async(session, ticker, target_date)
            existing, records = await asyncio.gather(existing_task, report_task)

            if existing is not None and target_date in existing["date"].cast(pl.Utf8).to_list():
                return "skipped", ticker

            row = _compute_one_day(records)
            if row is None:
                return "skipped", ticker

            new_row = pl.DataFrame([row]).with_columns(pl.col("date").cast(pl.Date))

            if existing is not None:
                combined = pl.concat([
                    existing.drop(["concentration_5d", "concentration_20d"]),
                    new_row.drop(["concentration_5d", "concentration_20d"], strict=False),
                ])
                combined = combined.unique(subset=["date"]).sort("date")
            else:
                combined = new_row

            combined = _add_rolling(combined)
            await asyncio.to_thread(_upload_parquet, key, combined)
            return "updated", ticker
        except Exception as e:
            return f"error: {e}", ticker


async def daily_update_async(target_date: str, tickers: list[str] | None, concurrency: int) -> None:
    timeout = aiohttp.ClientTimeout(total=120)
    connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300)
    sem = asyncio.Semaphore(concurrency)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        if tickers is None:
            tickers = await _get_all_tickers(session)

        print(f"[daily] {target_date} — {len(tickers)} tickers (async x{concurrency})")

        tasks = [asyncio.create_task(_process_ticker(session, ticker, target_date, sem)) for ticker in tickers]

        updated = skipped = errors = 0
        for i, task in enumerate(asyncio.as_completed(tasks), 1):
            status, ticker = await task
            if status == "updated":
                updated += 1
            elif status == "skipped":
                skipped += 1
            else:
                errors += 1
                print(f"  [{i:4d}] {ticker} ✗  {status}")

            if i % 100 == 0:
                print(f"  [{i}/{len(tickers)}] updated={updated} skipped={skipped} errors={errors}")

        print(f"Done — updated: {updated}, skipped: {skipped}, errors: {errors}")


# --- Entry point ---
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true",
                        help="Upload all local CSVs from concentration/self_calculate/ to R2")
    parser.add_argument("--date", default=str(date.today()), help="YYYY-MM-DD (default: today)")
    parser.add_argument("--tickers", nargs="+", help="Limit to specific tickers")
    parser.add_argument("--concurrency", type=int, default=12,
                        help="Concurrent FinMind requests in daily mode")
    args = parser.parse_args()

    if args.backfill:
        backfill_from_local(args.tickers)
    else:
        asyncio.run(daily_update_async(args.date, args.tickers, args.concurrency))


if __name__ == "__main__":
    main()
