import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TRADING_DATA_PATH = Path(os.getenv("TRADING_DATA_PATH", "/Users/yanyifu/Documents/_Coding/Trading/history_data/tw"))
PRICE_ADJ_PATH = TRADING_DATA_PATH / "stock_price_adj" / "self_calculate"
CONCENTRATION_PATH = TRADING_DATA_PATH / "concentration"
TRADER_INFO_PATH = TRADING_DATA_PATH / "traderinfo"
TICKER_INFO_PATH = TRADING_DATA_PATH / "ticker_info.csv"

DB_PATH = Path(__file__).parent.parent / "data" / "screener.duckdb"
DB_PATH.parent.mkdir(exist_ok=True)
