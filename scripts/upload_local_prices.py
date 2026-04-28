"""
Upload local Trading CSV price history → R2 (upsert, skip if R2 already newer).

Usage:
  python -m scripts.upload_local_prices [--start-from 1101] [--limit 100]
"""
import argparse
import io
import os
import sys
from pathlib import Path

import boto3
import polars as pl
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv()

LOCAL_PRICE_DIR = Path(
    os.getenv("TRADING_DATA_PATH",
              "/Users/yanyifu/Documents/_Coding/Trading/history_data/tw")
) / "stock_price_adj"

R2_BUCKET            = os.environ["R2_BUCKET"]
R2_ENDPOINT          = os.environ["R2_ENDPOINT"]
R2_ACCESS_KEY_ID     = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4"),
    region_name="auto",
)


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


def _parse_csv(path: Path) -> pl.DataFrame:
    df = pl.read_csv(path, try_parse_dates=True)
    df = df.rename({
        "stock_id": "ticker",
        "max": "high",
        "min": "low",
        "Trading_Volume": "volume",
    }).select(["date", "open", "high", "low", "close", "volume"])
    df = df.with_columns(pl.col("date").cast(pl.Date))
    for col in ["open", "high", "low", "close", "volume"]:
        df = df.with_columns(pl.col(col).cast(pl.Float64))
    return df.sort("date")


def upload_ticker(ticker: str) -> tuple[str, str]:
    csv_path = LOCAL_PRICE_DIR / f"{ticker}.csv"
    if not csv_path.exists():
        return "missing", "no local CSV"

    key = f"tickers/{ticker}.parquet"
    try:
        local_df = _parse_csv(csv_path)
        existing = _download_parquet(key)

        if existing is not None:
            combined = pl.concat([existing, local_df]).unique(subset=["date"]).sort("date")
            added = max(0, len(combined) - len(existing))
        else:
            combined = local_df
            added = len(combined)

        _upload_parquet(key, combined)
        date_range = f"{combined['date'].min()} → {combined['date'].max()}"
        return "ok", f"+{added} rows  [{date_range}]  total={len(combined)}"
    except Exception as e:
        return "error", str(e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-from", help="Start from this ticker (inclusive)")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    csv_files = sorted(LOCAL_PRICE_DIR.glob("*.csv"))
    tickers = [f.stem for f in csv_files]

    if args.start_from:
        tickers = [t for t in tickers if t >= args.start_from]
    if args.limit:
        tickers = tickers[:args.limit]

    print(f"Uploading {len(tickers)} tickers from {LOCAL_PRICE_DIR} → R2 tickers/")
    counts = {"ok": 0, "missing": 0, "error": 0}
    errors = []

    for i, ticker in enumerate(tickers, 1):
        status, reason = upload_ticker(ticker)
        counts[status] += 1
        sym = {"ok": "✓", "missing": "–", "error": "✗"}[status]
        print(f"[{i:4d}/{len(tickers)}] {ticker} {sym}  {reason}")
        if status == "error":
            errors.append((ticker, reason))

    print(f"\nDone — ok: {counts['ok']}, missing: {counts['missing']}, errors: {counts['error']}")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for ticker, reason in errors:
            print(f"  {ticker}: {reason}")


if __name__ == "__main__":
    main()
