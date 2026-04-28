"""
Fetch adjusted OHLCV from FinMind → per-ticker Parquet → Cloudflare R2
Run: python -m scripts.fetch_prices [--date YYYY-MM-DD]

R2 layout:
  tickers/{ticker}.parquet  ← adjusted OHLCV (date, open, high, low, close, volume)

Backfill (one-time):
  python -m scripts.fetch_prices --backfill [--start-from 1101] [--limit 100]
  python -m scripts.fetch_prices --backfill --from-start  # fetch full history from 1994
"""
import argparse
import io
import os
import time
from datetime import date

import boto3
import polars as pl
import requests
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv()

# --- R2 client ---
R2_ACCOUNT_ID        = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID     = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET            = os.environ["R2_BUCKET"]
R2_ENDPOINT          = os.environ["R2_ENDPOINT"]
FINMIND_TOKEN        = os.environ.get("FINMIND_TOKEN") or os.environ["FINMIND_API_KEY"]

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4"),
    region_name="auto",
)

FINMIND_API = "https://api.finmindtrade.com/api/v4/data"

NOT_STOCK_CATEGORIES = {
    "受益證券", "上櫃指數股票型基金(ETF)", "上櫃ETF",
    "所有證券", "ETN", "存託憑證", "ETF", "大盤", "index", "Food",
}


# --- FinMind helpers ---
def _finmind_get(dataset: str, start_date: str, end_date: str,
                 stock_id: str | None = None) -> tuple[list[dict], str]:
    params = {
        "dataset": dataset,
        "start_date": start_date,
        "end_date": end_date,
        "token": FINMIND_TOKEN,
    }
    if stock_id:
        params["data_id"] = stock_id
    resp = requests.get(FINMIND_API, params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    msg = data.get("msg", "")
    if data.get("status") != 200:
        raise RuntimeError(f"FinMind status={data.get('status')} msg={msg}")
    return data["data"], msg


def _get_all_tickers() -> list[str]:
    today = str(date.today())
    records, _ = _finmind_get("TaiwanStockInfo", today, today)
    if not records:
        raise RuntimeError("TaiwanStockInfo returned empty data")
    df = pl.DataFrame(records)
    df = df.filter(pl.col("type").is_in(["twse", "tpex"]))
    df = df.filter(~pl.col("industry_category").is_in(list(NOT_STOCK_CATEGORIES)))
    df = df.with_columns(
        pl.col("stock_id").cast(pl.Utf8)
    ).filter(pl.col("stock_id").str.len_chars() <= 4)
    return sorted(df["stock_id"].unique().to_list())


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


def _upsert_ticker(prefix: str, ticker: str, new_rows: pl.DataFrame) -> int:
    key = f"{prefix}/{ticker}.parquet"
    existing = _download_parquet(key)
    if existing is not None:
        combined = pl.concat([existing, new_rows]).unique(subset=["date"]).sort("date")
        added = max(0, len(combined) - len(existing))
    else:
        combined = new_rows.sort("date")
        added = len(combined)
    _upload_parquet(key, combined)
    return added


def _parse_price_df(records: list[dict]) -> pl.DataFrame:
    df = pl.DataFrame(records).rename({
        "stock_id": "ticker",
        "max": "high",
        "min": "low",
        "Trading_Volume": "volume",
    }).select(["ticker", "date", "open", "high", "low", "close", "volume"])
    df = df.with_columns(pl.col("date").cast(pl.Date))
    for col in ["open", "high", "low", "close", "volume"]:
        df = df.with_columns(pl.col(col).cast(pl.Float64))
    return df


# --- Ticker info (stock names) ---
def ingest_ticker_info() -> None:
    print("[meta] Fetching TaiwanStockInfo ...")
    today = str(date.today())
    records, _ = _finmind_get("TaiwanStockInfo", today, today)
    if not records:
        print("[meta] No data returned.")
        return
    df = pl.DataFrame(records).select(["stock_id", "stock_name", "industry_category", "type"])
    _upload_parquet("meta/ticker_info.parquet", df)
    print(f"[meta] Done. {len(df)} tickers uploaded to R2 meta/ticker_info.parquet")


# --- Daily ingest (all tickers, one date) ---
def ingest_price(target_date: str) -> None:
    print(f"[price] Fetching TaiwanStockPriceAdj for {target_date} ...")
    records, _ = _finmind_get("TaiwanStockPriceAdj", target_date, target_date)
    if not records:
        print("[price] No data returned.")
        return

    df = _parse_price_df(records)
    tickers = df["ticker"].unique().to_list()
    print(f"[price] {len(records)} rows, {len(tickers)} tickers — uploading to R2 ...")

    added_total = 0
    for ticker in tickers:
        rows = df.filter(pl.col("ticker") == ticker).drop("ticker")
        added_total += _upsert_ticker("tickers", ticker, rows)

    print(f"[price] Done. {added_total} new rows written.")



# --- Backfill (per-ticker, full history) ---
def backfill_ticker(ticker: str, end_date: str, delay: float, from_start: bool = False) -> tuple[str, str]:
    key = f"tickers/{ticker}.parquet"
    existing = _download_parquet(key)
    if from_start or existing is None:
        start_date = "1994-10-01"
    else:
        latest = existing["date"].max()
        earliest = existing["date"].min()
        if str(latest) >= end_date and str(earliest) <= "1994-10-02":
            return "current", f"up-to-date ({latest})"
        start_date = "1994-10-01"

    try:
        records, msg = _finmind_get("TaiwanStockPriceAdj", start_date, end_date, stock_id=ticker)
        if not records:
            detail = f" msg={msg}" if msg else ""
            return "empty", f"no data [{start_date} → {end_date}]{detail}"
        df = _parse_price_df(records).drop("ticker")
        added = _upsert_ticker("tickers", ticker, df)
        time.sleep(delay)
        return "updated", f"{added} rows [{df['date'].min()} → {df['date'].max()}]"
    except Exception as e:
        return "error", str(e)


def run_backfill(start_from: str | None, limit: int | None, end_date: str, delay: float, from_start: bool = False) -> None:
    print("Fetching ticker list ...")
    tickers = _get_all_tickers()
    if start_from:
        tickers = [t for t in tickers if t >= start_from]
    if limit:
        tickers = tickers[:limit]

    print(f"Backfill: {len(tickers)} tickers → R2 (TaiwanStockPriceAdj, up to {end_date}){' [from-start]' if from_start else ''}")
    counts = {"updated": 0, "current": 0, "empty": 0, "error": 0}
    empties = []
    errors = []
    for i, ticker in enumerate(tickers, 1):
        status, reason = backfill_ticker(ticker, end_date, delay, from_start=from_start)
        counts[status] += 1
        sym = {"updated": "✓", "current": "○", "empty": "–", "error": "✗"}[status]
        print(f"[{i:4d}/{len(tickers)}] {ticker} {sym}  {reason}")
        if status == "empty":
            empties.append((ticker, reason))
        if status == "error":
            errors.append((ticker, reason))

    print(f"\nDone — updated: {counts['updated']}, current: {counts['current']}, empty: {counts['empty']}, errors: {counts['error']}")
    if empties:
        print(f"\nEmpty ({len(empties)} tickers — no FinMind data):")
        for ticker, reason in empties:
            print(f"  {ticker}: {reason}")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for ticker, reason in errors:
            print(f"  {ticker}: {reason}")


# --- Entry point ---
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=str(date.today()), help="YYYY-MM-DD (default: today)")
    parser.add_argument("--skip-price", action="store_true")
    parser.add_argument("--backfill", action="store_true", help="Per-ticker full history backfill to R2")
    parser.add_argument("--start-from", help="Backfill: start from this ticker (inclusive)")
    parser.add_argument("--limit", type=int, help="Backfill: max tickers to process")
    parser.add_argument("--delay", type=float, default=0.3, help="Seconds between API calls (backfill)")
    parser.add_argument("--from-start", action="store_true", help="Backfill from 1994 even if R2 already has data")
    args = parser.parse_args()

    if args.backfill:
        run_backfill(args.start_from, args.limit, args.date, args.delay, from_start=args.from_start)
        return

    target = args.date
    print(f"=== Ingest run: {target} ===")
    if not args.skip_price:
        ingest_price(target)
    ingest_ticker_info()
    print("=== Done ===")


if __name__ == "__main__":
    main()
