from dotenv import load_dotenv
import os, requests, json

load_dotenv()
token = os.environ.get("FINMIND_API_KEY", "")

resp = requests.get("https://api.finmindtrade.com/api/v4/data", params={
    "dataset": "TaiwanStockConcentration",
    "data_id": "2330",
    "start_date": "2025-01-01",
    "end_date": "2025-01-10",
    "token": token,
})

print(f"HTTP {resp.status_code}")
print(resp.text[:2000])
