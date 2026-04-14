import streamlit as st
import httpx
import polars as pl

API_BASE = "http://localhost:8000"

st.set_page_config(page_title="K-Line Screener", layout="wide")
st.title("K-Line Quant Screener")

with st.sidebar:
    st.header("Price / MA")
    ma_window = st.slider("MA Window", 5, 120, 20)
    price_above_ma = st.checkbox("Close > MA", value=True)
    bb_breakout = st.checkbox("Bollinger Upper Breakout (close > BB+2σ)", value=False)

    st.divider()
    st.header("Volume")
    volume_ratio = st.slider("Min Volume Ratio (× MA vol)", 1.0, 5.0, 1.5, step=0.1)

    st.divider()
    st.header("RSI")
    rsi_period = st.slider("RSI Period", 5, 50, 14)
    rsi_range = st.slider("RSI Range", 0, 100, (0, 100))

    st.divider()
    st.header("Concentration")
    use_concentration = st.checkbox("Filter by Concentration 20d", value=False)
    conc_min = st.number_input(
        "Min concentration_20d", value=0.0, step=0.5,
        disabled=not use_concentration,
    )

    st.divider()
    top_n = st.slider("Top N results", 10, 200, 50)
    run = st.button("Screen", type="primary")

if run:
    with st.spinner("Scanning..."):
        resp = httpx.get(
            f"{API_BASE}/screen",
            params={
                "ma_window": ma_window,
                "volume_ratio": volume_ratio,
                "price_above_ma": price_above_ma,
                "bb_breakout": bb_breakout,
                "rsi_period": rsi_period,
                "rsi_min": rsi_range[0],
                "rsi_max": rsi_range[1],
                "use_concentration": use_concentration,
                "conc_min": conc_min,
                "top_n": top_n,
            },
            timeout=120,
        )
    if resp.status_code == 200:
        data = resp.json()
        if not data:
            st.warning("No stocks matched the criteria.")
        else:
            df = pl.DataFrame(data)
            st.success(f"{len(df)} stocks matched")

            # Column display config
            col_cfg = {
                "ticker": st.column_config.TextColumn("Ticker"),
                "date": st.column_config.DateColumn("Date"),
                "close": st.column_config.NumberColumn("Close", format="%.2f"),
                "ma": st.column_config.NumberColumn(f"MA{ma_window}", format="%.2f"),
                "bb_upper": st.column_config.NumberColumn("BB Upper", format="%.2f"),
                "bb_lower": st.column_config.NumberColumn("BB Lower", format="%.2f"),
                "vol_ratio": st.column_config.NumberColumn("Vol Ratio", format="%.2f"),
                "rsi": st.column_config.NumberColumn(f"RSI{rsi_period}", format="%.1f"),
                "concentration_20d": st.column_config.NumberColumn("Conc 20d", format="%.2f"),
            }
            st.dataframe(df, use_container_width=True, column_config=col_cfg)
    else:
        st.error(f"API error: {resp.status_code} — {resp.text}")
