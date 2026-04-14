from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from app.screener import screen_stocks
from app.pattern_matcher import match_pattern

app = FastAPI(title="Quant Web Screener", version="0.3.0")


class Candle(BaseModel):
    open: float
    close: float
    high: float
    low: float
    volume: float = 0.0


class PatternMatchRequest(BaseModel):
    candles: list[Candle]
    window_min: int = 5
    window_max: int = 30
    use_volume: bool = False
    top_n: int = 30


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/screen")
def screen(
    ma_window: int = Query(default=20, ge=5, le=120),
    volume_ratio: float = Query(default=1.5, ge=1.0, le=10.0),
    price_above_ma: bool = Query(default=True),
    bb_breakout: bool = Query(default=False),
    rsi_period: int = Query(default=14, ge=5, le=50),
    rsi_min: float = Query(default=0.0, ge=0.0, le=100.0),
    rsi_max: float = Query(default=100.0, ge=0.0, le=100.0),
    use_concentration: bool = Query(default=False),
    conc_min: float = Query(default=0.0),
    top_n: int = Query(default=50, ge=1, le=200),
):
    df = screen_stocks(
        ma_window=ma_window,
        volume_ratio=volume_ratio,
        price_above_ma=price_above_ma,
        bb_breakout=bb_breakout,
        rsi_period=rsi_period,
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        use_concentration=use_concentration,
        conc_min=conc_min,
        top_n=top_n,
    )
    return JSONResponse(content=df.to_dicts())


@app.post("/pattern_match")
def pattern_match(req: PatternMatchRequest):
    df = match_pattern(
        drawn_candles=[c.model_dump() for c in req.candles],
        window_min=req.window_min,
        window_max=req.window_max,
        use_volume=req.use_volume,
        top_n=req.top_n,
    )
    return JSONResponse(content=df.to_dicts())
