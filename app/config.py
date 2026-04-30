import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

DATA_DIR = Path(os.getenv("APP_DATA_PATH", Path(__file__).parent.parent / "data"))
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY", "dev-only-session-secret-change-me")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "")

TRADING_DATA_PATH = Path(os.getenv("TRADING_DATA_PATH", "/Users/yanyifu/Documents/_Coding/Trading/history_data/tw"))
PRICE_ADJ_PATH = TRADING_DATA_PATH / "stock_price_adj"
CONCENTRATION_PATH = TRADING_DATA_PATH / "concentration"
MARKET_VALUE_PATH = TRADING_DATA_PATH / "market_value_twse_tpex"
TRADER_INFO_PATH = TRADING_DATA_PATH / "traderinfo"
TICKER_INFO_PATH = TRADING_DATA_PATH / "ticker_info.csv"

DUCKDB_PATH = DATA_DIR / "screener.duckdb"
DB_PATH = DUCKDB_PATH
SQLITE_PATH = DATA_DIR / "app.sqlite"
WATCHLISTS_PATH = DATA_DIR / "watchlists.json"
STOCK_UNIVERSE_PATH = DATA_DIR / "stock_universe.csv"

DATA_DIR.mkdir(parents=True, exist_ok=True)
DUCKDB_PATH.parent.mkdir(exist_ok=True)
WATCHLISTS_PATH.parent.mkdir(parents=True, exist_ok=True)

PARQUET_CACHE_PATH = DATA_DIR / "cache"
(PARQUET_CACHE_PATH / "tickers").mkdir(parents=True, exist_ok=True)
(PARQUET_CACHE_PATH / "meta").mkdir(parents=True, exist_ok=True)
(PARQUET_CACHE_PATH / "market_value").mkdir(parents=True, exist_ok=True)
