"""
Build a stock universe list from FinMind TaiwanStockInfo.

Default filters:
  - only `twse` and `tpex`
  - exclude ETF / ETN / bonds / depositary receipts / index items
  - keep 4-digit tickers starting from 1101

Usage:
  python -m scripts.build_stock_universe
  python -m scripts.build_stock_universe --min-ticker 1101 --max-ticker 2330
"""
from __future__ import annotations

import argparse

from app.config import STOCK_UNIVERSE_PATH
from app.stock_universe import build_stock_universe, save_stock_universe


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-ticker", type=int, default=1101)
    parser.add_argument("--max-ticker", type=int)
    parser.add_argument("--output", default=str(STOCK_UNIVERSE_PATH))
    args = parser.parse_args()

    df = build_stock_universe(min_ticker=args.min_ticker, max_ticker=args.max_ticker)
    path = save_stock_universe(args.output, min_ticker=args.min_ticker, max_ticker=args.max_ticker, df=df)

    print(f"saved {len(df)} tickers -> {path}")
    if not df.is_empty():
        print(df.head(10).to_string())


if __name__ == "__main__":
    main()
