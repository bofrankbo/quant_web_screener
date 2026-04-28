"""
Shared Cloudflare R2 client and helpers.
"""
import io
import os

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


def download_parquet(key: str) -> pl.DataFrame | None:
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=key)
        return pl.read_parquet(io.BytesIO(obj["Body"].read()))
    except Exception as e:
        if "NoSuchKey" in str(e) or "404" in str(e):
            return None
        raise


def download_parquets(keys: list[str]) -> dict[str, pl.DataFrame]:
    """Batch download; returns {key: df} for keys that exist."""
    result = {}
    for key in keys:
        df = download_parquet(key)
        if df is not None:
            result[key] = df
    return result
