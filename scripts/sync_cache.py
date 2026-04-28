"""
Sync R2 parquet files → local cache (data/cache/).
Run after daily ingest so screener queries hit local disk, not R2 network.

Usage:
  python -m scripts.sync_cache           # sync tickers/ only
  python -m scripts.sync_cache --all     # sync tickers/ + concentration/
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

R2_BUCKET   = os.environ["R2_BUCKET"]
R2_ENDPOINT = os.environ["R2_ENDPOINT"]

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    config=Config(signature_version="s3v4"),
    region_name="auto",
)

CACHE_DIR = Path(__file__).parent.parent / "data" / "cache"


def sync_prefix(prefix: str) -> None:
    local_dir = CACHE_DIR / prefix
    local_dir.mkdir(parents=True, exist_ok=True)

    paginator = s3.get_paginator("list_objects_v2")
    keys = [
        obj["Key"]
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=f"{prefix}/")
        for obj in page.get("Contents", [])
        if obj["Key"].endswith(".parquet")
    ]

    print(f"[sync] {prefix}/ — {len(keys)} files")
    ok = err = 0
    for key in keys:
        fname = Path(key).name
        dest = local_dir / fname
        try:
            obj = s3.get_object(Bucket=R2_BUCKET, Key=key)
            dest.write_bytes(obj["Body"].read())
            ok += 1
        except Exception as e:
            print(f"  ✗ {fname}: {e}", file=sys.stderr)
            err += 1

    print(f"[sync] {prefix}/ done — {ok} ok, {err} errors")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="Also sync concentration/")
    args = parser.parse_args()

    sync_prefix("tickers")
    if args.all:
        sync_prefix("concentration")
        sync_prefix("meta")


if __name__ == "__main__":
    main()
