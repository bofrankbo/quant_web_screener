import os
import threading
import logging
import requests

logger = logging.getLogger(__name__)

_quotes: dict[str, dict] = {}
_lock = threading.Lock()


def fetch_and_store_quotes() -> None:
    token = os.environ.get("FINMIND_TOKEN") or os.environ.get("FINMIND_API_KEY", "")
    try:
        resp = requests.get(
            "https://api.finmindtrade.com/api/v4/taiwan_stock_tick_snapshot",
            headers={"Authorization": f"Bearer {token}"},
            params={"data_id": ""},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        snapshot = {}
        for row in data:
            sid = row.get("stock_id")
            if sid:
                snapshot[sid] = {
                    "open":        row.get("open"),
                    "high":        row.get("max"),
                    "low":         row.get("min"),
                    "close":       row.get("close"),
                    "volume":      row.get("total_volume"),
                    "change_rate": row.get("change_rate"),
                }
        with _lock:
            _quotes.clear()
            _quotes.update(snapshot)
        logger.debug("quotes updated: %d tickers", len(_quotes))
    except Exception as e:
        logger.warning("fetch_quotes failed: %s", e)


def get_quotes() -> dict:
    with _lock:
        return dict(_quotes)
