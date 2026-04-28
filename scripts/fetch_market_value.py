"""
Fetch TaiwanStockMarketValue → R2 market_value/{date}.parquet (one file per trading day).

Usage:
  python -m scripts.fetch_market_value                        # yesterday
  python -m scripts.fetch_market_value --date 2026-04-27     # specific date
  python -m scripts.fetch_market_value --start 2026-01-01    # date range (end=yesterday)
  python -m scripts.fetch_market_value --start 2026-01-01 --end 2026-03-31
"""
import argparse
import io
import os
from datetime import date, datetime, timedelta

import boto3
import polars as pl
from botocore.config import Config
from dotenv import load_dotenv
from FinMind.data import DataLoader

load_dotenv()

R2_BUCKET            = os.environ["R2_BUCKET"]
R2_ENDPOINT          = os.environ["R2_ENDPOINT"]
R2_ACCESS_KEY_ID     = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
FINMIND_TOKEN        = os.environ.get("FINMIND_TOKEN") or os.environ["FINMIND_API_KEY"]

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4"),
    region_name="auto",
)


def _r2_key(d: str) -> str:
    return f"market_value/{d}.parquet"


def _exists_in_r2(d: str) -> bool:
    try:
        s3.head_object(Bucket=R2_BUCKET, Key=_r2_key(d))
        return True
    except Exception:
        return False


def _fetch_date(api: DataLoader, d: str) -> pl.DataFrame:
    df = api.taiwan_stock_market_value(start_date=d, end_date=d)
    if df is None or df.empty:
        return pl.DataFrame()
    pdf = pl.from_pandas(df)
    # Only real stocks: 4-digit numeric IDs
    pdf = pdf.filter(pl.col("stock_id").str.matches(r"^\d{4}$"))
    return pdf.select(["date", "stock_id", "market_value"])


def _upload(d: str, df: pl.DataFrame) -> None:
    buf = io.BytesIO()
    df.write_parquet(buf)
    buf.seek(0)
    s3.put_object(Bucket=R2_BUCKET, Key=_r2_key(d), Body=buf.getvalue())
    print(f"[ok] {d} → {len(df)} stocks → {_r2_key(d)}")


def _date_range(start: str, end: str) -> list[str]:
    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    out = []
    d = s
    while d <= e:
        out.append(str(d))
        d += timedelta(days=1)
    return out


def run(dates: list[str]) -> None:
    api = DataLoader()
    api.login_by_token(api_token=FINMIND_TOKEN)

    for d in dates:
        if _exists_in_r2(d):
            print(f"[skip] {d} already in R2")
            continue
        print(f"[fetch] {d} ...")
        df = _fetch_date(api, d)
        if df.is_empty():
            print(f"[skip] {d} no data returned")
            continue
        _upload(d, df)


def main():
    yesterday = str(date.today() - timedelta(days=1))
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Single date YYYY-MM-DD")
    parser.add_argument("--start", help="Start of range YYYY-MM-DD")
    parser.add_argument("--end", default=yesterday, help="End of range (default: yesterday)")
    args = parser.parse_args()

    if args.date:
        dates = [args.date]
    elif args.start:
        dates = _date_range(args.start, args.end)
    else:
        dates = [yesterday]

    run(dates)


if __name__ == "__main__":
    main()
