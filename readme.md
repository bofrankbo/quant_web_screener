# quant_web_screener — 狀態說明

## 這個專案在做什麼

從 `Trading/history_data/tw/stock_price_adj/*.csv` 直接篩選強勢股，**不搬資料、不建 DB**。
透過瀏覽器調參數，即時看結果。

---

## 現在可以用的篩選條件

| 條件 | 說明 | 預設 |
|---|---|---|
| Close > MA(N) | 收盤在均線上 | ✅ on |
| Bollinger Upper Breakout | 收盤突破布林上軌 (+2σ) | off |
| Volume Ratio ≥ X | 當日量 / MA量 | 1.5x |
| RSI range | RSI 在指定區間內 | 0–100 |
| Concentration 20d ≥ N | 集中度 20 日籌碼 | off |

輸出欄位：`ticker, date, close, open, high, low, volume, ma, bb_upper, bb_lower, vol_ratio, rsi, concentration_20d`

---

## 專案架構

```
quant_web_screener/
├── app/
│   ├── config.py          ← 資料路徑設定（指向 Trading/history_data/tw）
│   ├── screener.py        ← 核心邏輯：DuckDB 讀 CSV → Polars 算指標 → 篩選
│   └── api.py             ← FastAPI，/screen endpoint
├── frontend/
│   └── streamlit_app.py   ← 瀏覽器 UI，Sidebar 調參 → 打 API → 顯示表格
├── scripts/
│   └── run_dev.sh         ← 同時啟動兩個 server
├── .venv/                 ← 本次建立的 virtualenv（已裝好所有套件）
├── data/
│   └── screener.duckdb    ← DuckDB 連線用（in-memory query，不存資料）
└── requirements.txt
```

### 資料流

```
CSV files (Trading/)
    │
    ▼ DuckDB read_csv_auto（glob 直接讀，不搬資料）
    │  → 取每支股票最近 N 筆
    │
    ▼ Polars 計算指標
    │  → MA, Bollinger Band, Volume Ratio, RSI
    │
    ▼ 篩選 + 排序（vol_ratio desc）
    │
    ▼ FastAPI /screen → Streamlit 顯示
```

---

## 怎麼啟動

```bash
cd /Users/yanyifu/Documents/_Coding/quant_web_screener
bash scripts/run_dev.sh
```

- FastAPI：http://localhost:8000/docs  （可直接測 API）
- Streamlit UI：http://localhost:8501

---

## 可以繼續做的方向

### 短期（直接加在現有架構）

1. **日期篩選**：只看最近 N 個交易日有成交的股票（過濾停牌、下市）
2. **漲跌幅**：加 `(close - prev_close) / prev_close` 欄位
3. **三大法人**：`traderinfo/` 結構是 `ticker/date.csv`，需先用 script 壓成一支 flat CSV，再 join

### 中期

4. **背景排程**：每天收盤後自動跑一次，結果存成 `data/result_YYYYMMDD.parquet`，UI 可以查歷史
5. **K 線圖**：點擊 ticker 展開 candlestick chart（用 `plotly` 或 `altair`）
6. **自訂條件組合**：存成 preset，下次直接載入參數

### 如果要加三大法人（步驟）

```bash
# 先跑這個把 traderinfo 壓平（還沒寫，需要建這支 script）
python scripts/flatten_traderinfo.py
# 產出：data/traderinfo_latest.csv  columns: ticker, date, foreign_net, trust_net, dealer_net
# 然後在 screener.py 加 join 邏輯
```


  
# 網路架構惡補

  Web UI (Streamlit)
      ↓ speaks HTTP
  FastAPI client (httpx.get)                                                                                
      ↓ TCP connection
  [proxies if any]                                                                                          
      ↓           
  FastAPI server (listening on :8000)                                                                       
      ↓ parses HTTP request                                                                                 
  Python logic (screen_stocks)
                                                                                                            
  ---             
  Key distinction:
                                                                                                            
  - TCP = the pipe (transport layer, moves bytes reliably)
  - HTTP = the language spoken through that pipe (application layer)                                        
  - FastAPI/httpx = the code that reads/writes that language                                                
                                                                                                            
  So HTTP isn't a place in the chain — it's the format of every message sent through the chain.             
                                                                                                            
  ---                                                                                                       
  Simpler analogy:
                                                                                                            
  TCP = phone line
  HTTP = English (the language you speak on that line)                                                      
  httpx = your mouth                                                                                        
  FastAPI = the person on the other end who understands English.