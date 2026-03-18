"""SniperX — FastAPI Web Server"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from pathlib import Path
import asyncio
import json
import logging
import time

logger = logging.getLogger("sniperx.web")

app = FastAPI(title="SniperX")


class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if "/static/" in str(request.url):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


app.add_middleware(NoCacheStaticMiddleware)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

bot_engine = None


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


def set_bot_engine(engine):
    global bot_engine
    bot_engine = engine


def _serialize_zones():
    if not bot_engine:
        return []
    zones = []
    for key, zlist in bot_engine.zones.items():
        # key is "SYMBOL_TIMEFRAME" e.g. "XAUUSD_H1"
        parts = key.rsplit("_", 1)
        sym = parts[0] if len(parts) == 2 else key
        tf = parts[1] if len(parts) == 2 else ""
        for z in zlist:
            zones.append({
                "symbol": sym,
                "timeframe": tf,
                "type": z.zone_type,
                "zone_type": z.zone_type,
                "status": z.status,
                "high": z.high,
                "low": z.low,
                "start_idx": z.start_idx,
                "end_idx": z.end_idx,
                "pin_count": z.pin_count,
                "breakout_direction": z.breakout_direction,
                "breakout_magnitude": round(z.breakout_magnitude, 2),
                "spike_magnitude": round(getattr(z, 'spike_magnitude', 0), 1),
                "gap_count": z.gap_count,
                "confidence": round(z.confidence, 1),
                "triangular_score": round(z.triangular_score, 1),
                "start_time": int(z.start_time) if z.start_time else 0,
                "end_time": int(z.end_time) if z.end_time else 0,
                "white_space_score": round(z.white_space_score, 1),
                "core_high": getattr(z, 'core_high', z.high),
                "core_low": getattr(z, 'core_low', z.low),
                "midpoint": getattr(z, 'midpoint', (z.high + z.low) / 2),
                "is_fresh": getattr(z, 'is_fresh', True),
                "quality_score": getattr(z, 'quality_score', 0),
                "displacement": getattr(z, 'displacement', 0),
                "touch_count": getattr(z, 'touch_count', 0),
                "is_invalid": getattr(z, 'is_invalid', False),
                # Phase 2: signal quality fields
                "regime": getattr(z, '_regime', ""),
                "higher_tf_bias": getattr(z, '_higher_tf_bias', ""),
                "ai_validated": getattr(z, '_ai_validated', False),
                "ai_confidence": getattr(z, '_ai_confidence', 0.0),
                "filter_reason": getattr(z, '_filter_reason', ""),
                "is_flipped": getattr(z, 'is_flipped', False),
                "touch_penetration_pct": round(getattr(z, 'touch_penetration_pct', 0.0), 3),
            })
    return zones


def _serialize_positions():
    if not bot_engine:
        return []
    positions = []
    for p in bot_engine.positions:
        positions.append({
            "ticket": p.ticket,
            "symbol": p.symbol,
            "direction": p.direction,
            "entry_price": p.entry_price,
            "stop_loss": p.stop_loss,
            "take_profit_1": p.take_profit_1,
            "take_profit_2": p.take_profit_2,
            "lot_size": p.lot_size,
            "tp1_hit": p.tp1_hit,
            "be_set": p.be_set,
            "trailing_active": p.trailing_active,
            "status": p.status,
            "r_result": round(p.r_result, 2),
        })
    return positions


def _serialize_stats():
    if not bot_engine:
        return {"total_r": 0, "wins": 0, "losses": 0, "trades": 0, "win_rate": 0}
    s = bot_engine.stats
    wr = (s["wins"] / s["trades"] * 100) if s["trades"] > 0 else 0
    return {**s, "win_rate": round(wr, 1)}


@app.get("/")
async def dashboard():
    return FileResponse(str(static_dir / "index.html"))


async def _get_account_info():
    if not bot_engine or not bot_engine.broker:
        return {"balance": 10000, "equity": 10000, "pnl": 0}
    try:
        acc = await bot_engine.broker.get_account()
        pnl = round(acc.equity - acc.balance, 2)
        return {"balance": round(acc.balance, 2), "equity": round(acc.equity, 2), "pnl": pnl}
    except Exception:
        return {"balance": 10000, "equity": 10000, "pnl": 0}


@app.get("/api/data")
async def get_all_data():
    account = await _get_account_info()
    return JSONResponse({
        "zones": _serialize_zones(),
        "positions": _serialize_positions(),
        "stats": _serialize_stats(),
        "account": account,
        "signals": bot_engine.signals_log[-50:] if bot_engine else [],
        "symbols": [s["name"] if isinstance(s, dict) else s for s in bot_engine.config["symbols"]] if bot_engine else [],
        "timestamp": time.time(),
    })


@app.get("/api/candles/{symbol}")
async def get_candles(symbol: str, timeframe: str = "M1", count: int = 500):
    if not bot_engine or not bot_engine.broker:
        return JSONResponse({"candles": []})
    try:
        candles = await bot_engine.broker.get_candles(symbol, timeframe, count)
        return JSONResponse({
            "candles": [
                {"time": int(c.time), "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
                for c in candles
            ]
        })
    except Exception as e:
        logger.error(f"Candle fetch error: {e}")
        return JSONResponse({"candles": [], "error": str(e)})


@app.get("/api/zones")
async def get_zones():
    return JSONResponse({"zones": _serialize_zones()})


@app.get("/api/positions")
async def get_positions():
    return JSONResponse({"positions": _serialize_positions()})


@app.get("/api/stats")
async def get_stats():
    return JSONResponse({"stats": _serialize_stats()})


@app.post("/api/config")
async def update_config(config: dict):
    if bot_engine:
        bot_engine.config.update(config)
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "error", "message": "Bot not running"}, status_code=500)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            account = await _get_account_info()
            data = {
                "type": "update",
                "zones": _serialize_zones(),
                "positions": _serialize_positions(),
                "stats": _serialize_stats(),
                "account": account,
                "signals": bot_engine.signals_log[-10:] if bot_engine else [],
                "timestamp": time.time(),
            }
            await websocket.send_json(data)
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
