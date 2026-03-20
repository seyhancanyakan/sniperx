"""SniperX — FastAPI Web Server"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from pydantic import BaseModel
from pathlib import Path
import asyncio
import json
import logging
import time
import uuid

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
        seen_levels = []  # list of (mid, height, zone_type) tuples
        for z in zlist:
            # Skip only truly dead zones — show stale/departed/active for visual reference
            if getattr(z, 'status', '') in ('invalid', 'consumed', 'invalidated_near_miss'):
                continue
            # Deduplicate overlapping zones — fuzzy match at same price area
            z_mid = (z.high + z.low) / 2
            z_height = z.high - z.low
            is_dup = False
            for seen_mid, seen_h, seen_type in seen_levels:
                if seen_type == z.zone_type:
                    # Same direction: skip if centers within 50% of zone height
                    if z_height > 0 and abs(z_mid - seen_mid) < z_height * 0.5:
                        is_dup = True
                        break
                    if seen_h > 0 and abs(z_mid - seen_mid) < seen_h * 0.5:
                        is_dup = True
                        break
            if is_dup:
                continue
            seen_levels.append((z_mid, z_height, z.zone_type))
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
                "learned_bonus": round(getattr(z, '_learned_bonus', 0), 1),
            })
    # Add manual zones
    for mz in _load_manual_zones():
        zones.append({
            "symbol": mz["symbol"],
            "timeframe": mz["timeframe"],
            "type": mz["zone_type"],
            "zone_type": mz["zone_type"],
            "status": mz.get("status", "active"),
            "high": mz["high"],
            "low": mz["low"],
            "start_idx": 0,
            "end_idx": 0,
            "pin_count": 0,
            "breakout_direction": "",
            "breakout_magnitude": 0,
            "spike_magnitude": 0,
            "gap_count": 0,
            "confidence": mz.get("confidence", 90),
            "triangular_score": 0,
            "start_time": int(mz.get("start_time", 0)),
            "end_time": int(mz.get("end_time", 0)),
            "white_space_score": 0,
            "core_high": mz["high"],
            "core_low": mz["low"],
            "midpoint": (mz["high"] + mz["low"]) / 2,
            "is_fresh": True,
            "quality_score": 90,
            "displacement": 0,
            "touch_count": 0,
            "is_invalid": False,
            "regime": "",
            "higher_tf_bias": "",
            "ai_validated": False,
            "ai_confidence": 0,
            "filter_reason": "",
            "is_flipped": False,
            "touch_penetration_pct": 0,
            "is_manual": True,
            "manual_id": mz["id"],
            "trade_active": mz.get("trade_active", True),
            "note": mz.get("note", ""),
            "priority": mz.get("priority", 50),
            "expiry_mode": mz.get("expiry_mode", "until_first_trade"),
            "source": "manual",
            "learning_status": mz.get("learning_status", "pending"),
        })
    return zones


_mt5_cache = {"positions": [], "orders": [], "ts": 0}


async def _refresh_mt5_cache():
    """Fetch live positions+orders from MT5 bridge, cache for 1s."""
    now = time.time()
    if now - _mt5_cache["ts"] < 1.0:
        return  # Use cache
    if not bot_engine or not bot_engine.broker:
        return
    try:
        _mt5_cache["positions"] = await bot_engine.broker.get_open_positions()
        _mt5_cache["orders"] = await bot_engine.broker.get_pending_orders()
        _mt5_cache["ts"] = now
    except Exception:
        pass


def _serialize_positions():
    if not bot_engine:
        return []
    results = []

    # Live open positions from MT5
    for p in _mt5_cache.get("positions", []):
        entry = p.get("open_price", p.get("price", 0))
        sl = p.get("sl", 0)
        tp = p.get("tp", 0)
        lot = p.get("lot", p.get("volume", 0.01))
        profit = p.get("pnl", p.get("profit", 0))
        direction = p.get("direction", "buy" if p.get("type", 0) == 0 else "sell")
        current = p.get("price_current", entry)
        results.append({
            "ticket": str(p.get("ticket", "")),
            "symbol": p.get("symbol", ""),
            "direction": direction,
            "entry_price": entry,
            "stop_loss": sl,
            "take_profit_1": tp,
            "take_profit_2": tp,
            "lot_size": lot,
            "tp1_hit": False,
            "be_set": False,
            "trailing_active": False,
            "status": "open",
            "r_result": 0,
            "profit": round(profit, 2),
            "current_price": current,
            "swap": round(p.get("swap", 0), 2),
            "commission": round(p.get("commission", 0), 2),
            "open_time": p.get("time", 0),
        })

    # Pending orders from MT5
    for o in _mt5_cache.get("orders", []):
        entry = o.get("price", o.get("open_price", 0))
        sl = o.get("sl", 0)
        tp = o.get("tp", 0)
        lot = o.get("volume", o.get("lot", 0.01))
        # type: 2=buy_limit, 3=sell_limit, 4=buy_stop, 5=sell_stop
        otype = o.get("type", 0)
        direction = "buy" if otype in (0, 2, 4) else "sell"
        order_type_label = {2: "BUY LIMIT", 3: "SELL LIMIT", 4: "BUY STOP", 5: "SELL STOP"}.get(otype, "PENDING")
        results.append({
            "ticket": str(o.get("ticket", "")),
            "symbol": o.get("symbol", ""),
            "direction": direction,
            "entry_price": entry,
            "stop_loss": sl,
            "take_profit_1": tp,
            "take_profit_2": tp,
            "lot_size": lot,
            "tp1_hit": False,
            "be_set": False,
            "trailing_active": False,
            "status": "pending",
            "r_result": 0,
            "profit": 0,
            "current_price": 0,
            "order_type": order_type_label,
            "open_time": o.get("time", 0),
        })

    return results


def _serialize_stats():
    # Combine bot internal stats with MT5 live data
    positions = _mt5_cache.get("positions", [])
    open_count = len(positions)
    open_pnl = sum(p.get("pnl", 0) for p in positions)

    # Get history stats from local cache
    history = []
    try:
        with open(TRADE_HISTORY_FILE) as f:
            history = json.load(f)
    except Exception:
        pass

    closed = [d for d in history if d.get("is_closed")]
    wins = [d for d in closed if d.get("net_profit", 0) > 0]
    losses = [d for d in closed if d.get("net_profit", 0) <= 0]
    total_profit = sum(d.get("net_profit", 0) for d in closed)
    gross_profit = sum(d.get("net_profit", 0) for d in wins)
    gross_loss = abs(sum(d.get("net_profit", 0) for d in losses))
    wr = round(len(wins) / len(closed) * 100, 1) if closed else 0
    pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0

    return {
        "total_r": round(total_profit, 2),
        "wins": len(wins),
        "losses": len(losses),
        "trades": len(closed),
        "win_rate": wr,
        "open_r": round(open_pnl, 2),
        "open_count": open_count,
        "profit_factor": pf,
    }


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
    await _refresh_mt5_cache()
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



@app.get("/api/zones")
async def get_zones():
    return JSONResponse({"zones": _serialize_zones()})


@app.get("/api/positions")
async def get_positions():
    await _refresh_mt5_cache()
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


# ─── MANUAL ZONES ───

MANUAL_ZONES_FILE = str(Path(__file__).parent.parent / "config" / "manual_zones.json")
ANNOTATIONS_FILE = str(Path(__file__).parent.parent / "config" / "annotations.json")
LEARNED_PATTERNS_FILE = str(Path(__file__).parent.parent / "config" / "learned_patterns.json")
CONFIG_PROPOSALS_FILE = str(Path(__file__).parent.parent / "config" / "config_change_proposals.json")


class ManualZoneRequest(BaseModel):
    symbol: str
    timeframe: str
    high: float
    low: float
    start_time: float
    end_time: float
    zone_type: str  # "demand" or "supply"
    trade_active: bool = True  # False = visual only, True = bot trades it
    note: str = ""  # Why the user drew this zone
    priority: int = 50  # 0-100, higher = more important
    expiry_mode: str = "until_first_trade"  # until_first_trade, until_first_fill, until_manual_delete, until_expiry_bars


class ManualZoneUpdate(BaseModel):
    high: float | None = None
    low: float | None = None
    trade_active: bool | None = None
    note: str | None = None
    priority: int | None = None
    expiry_mode: str | None = None


def _load_manual_zones() -> list:
    try:
        with open(MANUAL_ZONES_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_manual_zones(zones: list):
    with open(MANUAL_ZONES_FILE, "w") as f:
        json.dump(zones, f, indent=2)


@app.post("/api/zones/manual")
async def create_manual_zone(req: ManualZoneRequest):
    zones = _load_manual_zones()
    zone = {
        "id": str(uuid.uuid4())[:8],
        "symbol": req.symbol,
        "timeframe": req.timeframe,
        "high": req.high,
        "low": req.low,
        "start_time": req.start_time,
        "end_time": req.end_time,
        "zone_type": req.zone_type,
        "confidence": 80,
        "status": "active",
        "source": "manual",
        "is_manual": True,
        "trade_active": req.trade_active,
        "note": req.note,
        "priority": req.priority,
        "expiry_mode": req.expiry_mode,
        "created_at": time.time(),
        "trade_count": 0,
        "fill_count": 0,
        "outcome": None,  # Will be filled after trade closes
        "r_result": None,
        "learning_status": "pending",  # pending, reviewed, learned, rejected
    }
    # Overlap detection: suppress auto zones in same area
    overlap_info = None
    if bot_engine:
        zone_key = f"{req.symbol}_{req.timeframe}"
        auto_zones = bot_engine.zones.get(zone_key, [])
        for az in auto_zones:
            overlap_high = min(req.high, az.high)
            overlap_low = max(req.low, az.low)
            if overlap_low < overlap_high:  # There IS overlap
                zone_range = req.high - req.low
                overlap_pct = (overlap_high - overlap_low) / zone_range if zone_range > 0 else 0
                if overlap_pct > 0.5 and az.zone_type == req.zone_type:
                    overlap_info = {"auto_zone_id": az.id, "overlap_pct": round(overlap_pct, 2)}
                    # Mark auto zone as suppressed by manual
                    az._manual_override = True
                    logger.info(f"Manual zone overrides auto zone {az.id} "
                                f"(overlap={overlap_pct:.0%})")

    zone["overlap_with_auto"] = overlap_info
    zones.append(zone)
    _save_manual_zones(zones)
    logger.info(f"Manual zone created: {zone['zone_type']} {req.symbol}/{req.timeframe} "
                f"H:{req.high:.5f} L:{req.low:.5f} id={zone['id']}"
                f" trade_active={zone['trade_active']} expiry={zone['expiry_mode']}")

    # Trigger AI learning in background
    if bot_engine:
        asyncio.create_task(_learn_from_manual_zone(zone))

    return JSONResponse({"status": "ok", "zone": zone})


@app.get("/api/zones/manual")
async def list_manual_zones():
    return JSONResponse({"zones": _load_manual_zones()})


@app.patch("/api/zones/manual/{zone_id}")
async def update_manual_zone(zone_id: str, req: ManualZoneUpdate):
    zones = _load_manual_zones()
    zone = next((z for z in zones if z["id"] == zone_id), None)
    if not zone:
        return JSONResponse({"status": "error", "message": "Zone not found"}, status_code=404)
    update = req.model_dump(exclude_none=True)
    zone.update(update)
    _save_manual_zones(zones)
    logger.info(f"Manual zone updated: {zone_id} fields={list(update.keys())}")
    return JSONResponse({"status": "ok", "zone": zone})


@app.delete("/api/zones/manual/{zone_id}")
async def delete_manual_zone(zone_id: str):
    zones = _load_manual_zones()
    zones = [z for z in zones if z["id"] != zone_id]
    _save_manual_zones(zones)
    logger.info(f"Manual zone deleted: {zone_id}")
    return JSONResponse({"status": "ok"})


async def _learn_from_manual_zone(zone: dict):
    """Send manual zone to AI for pattern learning."""
    try:
        from ..core.zone_learner import ZoneLearner
        learner = ZoneLearner(bot_engine)
        await learner.learn_from_zone(zone)
    except Exception as e:
        logger.error(f"Zone learning error: {e}")


# ─── ANNOTATIONS (Chart Notes for AI Learning) ───

class AnnotationRequest(BaseModel):
    symbol: str
    timeframe: str
    time: float  # candle timestamp where annotation is placed
    price: float
    text: str  # user's note/teaching


def _load_annotations() -> list:
    try:
        with open(ANNOTATIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_annotations(annotations: list):
    with open(ANNOTATIONS_FILE, "w") as f:
        json.dump(annotations, f, indent=2)


@app.post("/api/annotations")
async def create_annotation(req: AnnotationRequest):
    annotations = _load_annotations()
    ann = {
        "id": str(uuid.uuid4())[:8],
        "symbol": req.symbol,
        "timeframe": req.timeframe,
        "time": req.time,
        "price": req.price,
        "text": req.text,
        "created_at": time.time(),
        "learning_status": "pending",
        "ai_response": None,
    }
    annotations.append(ann)
    _save_annotations(annotations)
    logger.info(f"Annotation created: {req.symbol}/{req.timeframe} @ {req.price:.5f} — {req.text[:50]}")

    # Trigger AI learning in background
    if bot_engine:
        asyncio.create_task(_learn_from_annotation(ann))

    return JSONResponse({"status": "ok", "annotation": ann})


@app.get("/api/annotations")
async def list_annotations(symbol: str = None, timeframe: str = None):
    annotations = _load_annotations()
    if symbol:
        annotations = [a for a in annotations if a["symbol"] == symbol]
    if timeframe:
        annotations = [a for a in annotations if a["timeframe"] == timeframe]
    return JSONResponse({"annotations": annotations})


@app.delete("/api/annotations/{ann_id}")
async def delete_annotation(ann_id: str):
    annotations = _load_annotations()
    annotations = [a for a in annotations if a["id"] != ann_id]
    _save_annotations(annotations)
    return JSONResponse({"status": "ok"})


async def _learn_from_annotation(ann: dict):
    """Send chart annotation to AI for learning."""
    try:
        from ..core.zone_learner import ZoneLearner
        learner = ZoneLearner(bot_engine)
        result = await learner.learn_from_annotation(ann)
        # Save AI response back to annotation
        if result:
            annotations = _load_annotations()
            for a in annotations:
                if a["id"] == ann["id"]:
                    a["learning_status"] = "reviewed"
                    a["ai_response"] = result
                    break
            _save_annotations(annotations)
    except Exception as e:
        logger.error(f"Annotation learning error: {e}")


# ─── TRADE HISTORY ───

TRADE_HISTORY_FILE = str(Path(__file__).parent.parent / "config" / "trade_history.json")


def _load_trade_history() -> list:
    try:
        with open(TRADE_HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_trade_history(history: list):
    with open(TRADE_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


@app.get("/api/history")
async def get_trade_history(days: int = 30):
    """Get trade history — tries MT5 bridge first, falls back to local cache."""
    # Try MT5 bridge
    if bot_engine and bot_engine.broker and hasattr(bot_engine.broker, 'get_history'):
        try:
            deals = await bot_engine.broker.get_history(days)
            if deals:
                # Cache locally
                _save_trade_history(deals)
                # Calculate summary stats
                closed = [d for d in deals if d.get("is_closed")]
                wins = [d for d in closed if d.get("net_profit", 0) > 0]
                losses = [d for d in closed if d.get("net_profit", 0) <= 0]
                total_profit = sum(d.get("net_profit", 0) for d in closed)
                gross_profit = sum(d.get("net_profit", 0) for d in wins)
                gross_loss = abs(sum(d.get("net_profit", 0) for d in losses))
                return JSONResponse({
                    "deals": deals,
                    "summary": {
                        "total_trades": len(closed),
                        "wins": len(wins),
                        "losses": len(losses),
                        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
                        "total_profit": round(total_profit, 2),
                        "gross_profit": round(gross_profit, 2),
                        "gross_loss": round(gross_loss, 2),
                        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0,
                        "avg_win": round(gross_profit / len(wins), 2) if wins else 0,
                        "avg_loss": round(gross_loss / len(losses), 2) if losses else 0,
                    }
                })
        except Exception as e:
            logger.debug(f"MT5 history fetch: {e}")

    # Fallback to local cache
    deals = _load_trade_history()
    return JSONResponse({"deals": deals, "summary": {}})


@app.post("/api/history/learn")
async def trigger_history_learning():
    """Trigger AI learning from all closed trades in history."""
    if not bot_engine or not bot_engine.broker:
        return JSONResponse({"status": "error", "message": "Bot not running"}, status_code=500)
    try:
        from ..core.zone_learner import ZoneLearner
        learner = ZoneLearner(bot_engine)
        # Get history
        deals = []
        if hasattr(bot_engine.broker, 'get_history'):
            deals = await bot_engine.broker.get_history(30)
        if not deals:
            deals = _load_trade_history()
        count = await learner.learn_from_all_history(deals)
        return JSONResponse({"status": "ok", "learned": count, "total_deals": len(deals)})
    except Exception as e:
        logger.error(f"History learning error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# ─── LEARNING API ───

def _load_learned_patterns() -> list:
    try:
        with open(LEARNED_PATTERNS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_learned_patterns(patterns: list):
    with open(LEARNED_PATTERNS_FILE, "w") as f:
        json.dump(patterns, f, indent=2)


def _load_config_proposals() -> list:
    try:
        with open(CONFIG_PROPOSALS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_config_proposals(proposals: list):
    with open(CONFIG_PROPOSALS_FILE, "w") as f:
        json.dump(proposals, f, indent=2)



@app.get("/api/learning/patterns")
async def get_learning_patterns():
    patterns = _load_learned_patterns()
    return JSONResponse({"patterns": patterns})


class PatternStatusUpdate(BaseModel):
    status: str  # proposed, approved, disabled, rejected


VALID_PATTERN_STATUSES = ("proposed", "approved", "disabled", "rejected")


@app.put("/api/learning/patterns/{pattern_id}")
async def update_pattern_status(pattern_id: str, req: PatternStatusUpdate):
    if req.status not in VALID_PATTERN_STATUSES:
        return JSONResponse({"status": "error", "message": f"Invalid status. Use: {', '.join(VALID_PATTERN_STATUSES)}"}, status_code=400)
    patterns = _load_learned_patterns()
    pat = next((p for p in patterns if p["id"] == pattern_id), None)
    if not pat:
        return JSONResponse({"status": "error", "message": "Pattern not found"}, status_code=404)
    old_status = pat.get("status", "proposed")
    pat["status"] = req.status
    pat["status_changed_at"] = time.time()
    _save_learned_patterns(patterns)
    logger.info(f"PATTERN_STATUS_CHANGED: {pattern_id} {old_status} -> {req.status} "
                f"pattern={pat.get('pattern', '?')}")
    return JSONResponse({"status": "ok", "pattern": pat})


@app.get("/api/learning/annotations")
async def get_learning_annotations():
    annotations = _load_annotations()
    # Return last 20, newest first
    annotations.sort(key=lambda a: a.get("created_at", 0), reverse=True)
    return JSONResponse({"annotations": annotations[:20]})


@app.get("/api/learning/outcomes")
async def get_learning_outcomes():
    """Return patterns that have outcome data (Phase B)."""
    patterns = _load_learned_patterns()
    outcomes = [p for p in patterns if p.get("phase") == "outcome" or p.get("win_count", 0) + p.get("loss_count", 0) > 0]
    return JSONResponse({"outcomes": outcomes})


@app.get("/api/learning/stats")
async def get_learning_stats():
    patterns = _load_learned_patterns()
    annotations = _load_annotations()
    total = len(patterns)
    approved = len([p for p in patterns if p.get("status") in ("approved", "approved_live", "active")])
    rejected = len([p for p in patterns if p.get("status") == "rejected"])
    proposed = len([p for p in patterns if p.get("status") == "proposed"])
    disabled = len([p for p in patterns if p.get("status") == "disabled"])
    with_outcomes = [p for p in patterns if p.get("win_count", 0) + p.get("loss_count", 0) > 0]
    total_wins = sum(p.get("win_count", 0) for p in with_outcomes)
    total_losses = sum(p.get("loss_count", 0) for p in with_outcomes)
    total_trades = total_wins + total_losses
    overall_wr = round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0
    avg_r = round(sum(p.get("avg_r", 0) for p in with_outcomes) / len(with_outcomes), 2) if with_outcomes else 0
    total_bonus_active = sum(p.get("confidence_bonus", 0) for p in patterns if p.get("status") in ("approved", "approved_live", "active"))
    return JSONResponse({
        "total_patterns": total,
        "approved": approved,
        "rejected": rejected,
        "proposed": proposed,
        "disabled": disabled,
        "total_annotations": len(annotations),
        "reviewed_annotations": len([a for a in annotations if a.get("learning_status") == "reviewed"]),
        "total_trades_with_learning": total_trades,
        "overall_win_rate": overall_wr,
        "avg_r": avg_r,
        "total_active_bonus": total_bonus_active,
    })


# ─── CONFIG CHANGE PROPOSALS (view-only, no live apply) ───

@app.get("/api/learning/proposals")
async def get_config_proposals():
    proposals = _load_config_proposals()
    return JSONResponse({"proposals": proposals})


class ProposalAction(BaseModel):
    action: str  # approve, reject


@app.put("/api/learning/proposals/{proposal_id}")
async def update_config_proposal(proposal_id: str, req: ProposalAction):
    """Update proposal status. Approve = acknowledge for future staged apply.
    No live settings.yaml mutation — that requires a separate staged pipeline."""
    if req.action not in ("approve", "reject"):
        return JSONResponse({"status": "error", "message": "Invalid action. Use approve or reject."}, status_code=400)

    proposals = _load_config_proposals()
    prop = next((p for p in proposals if p["id"] == proposal_id), None)
    if not prop:
        return JSONResponse({"status": "error", "message": "Proposal not found"}, status_code=404)

    old_status = prop.get("status", "proposed")

    if req.action == "reject":
        prop["status"] = "rejected"
        prop["resolved_at"] = time.time()
        _save_config_proposals(proposals)
        logger.info(f"CONFIG_CHANGE_REJECTED: {proposal_id} field={prop.get('field_name')}")
        return JSONResponse({"status": "ok", "proposal": prop})

    if req.action == "approve":
        # Only mark as approved — NO settings.yaml write, NO hot reload
        # Actual apply will happen via staged pipeline in a future phase
        prop["status"] = "approved"
        prop["resolved_at"] = time.time()
        _save_config_proposals(proposals)
        logger.info(f"CONFIG_CHANGE_APPROVED (staged, not applied): {proposal_id} "
                    f"field={prop.get('field_name')} "
                    f"{prop.get('old_value')} -> {prop.get('proposed_value')}")
        return JSONResponse({"status": "ok", "proposal": prop})


# Store last candle per symbol/tf for live WS updates (set by candle endpoint)
_last_candle_cache: dict = {}  # key: "SYMBOL_TF" -> {time, open, high, low, close, volume}


@app.get("/api/candles/{symbol}")
async def get_candles_v2(symbol: str, timeframe: str = "M1", count: int = 500):
    """Candle endpoint — also caches last candle for WS live updates."""
    if not bot_engine or not bot_engine.broker:
        return JSONResponse({"candles": []})
    try:
        candles = await bot_engine.broker.get_candles(symbol, timeframe, count)
        result = [
            {"time": int(c.time), "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
            for c in candles
        ]
        # Cache the last candle for WS live tick
        if result:
            _last_candle_cache[f"{symbol}_{timeframe}"] = result[-1]
        return JSONResponse({"candles": result})
    except Exception as e:
        logger.error(f"Candle fetch error: {e}")
        return JSONResponse({"candles": [], "error": str(e)})


async def _get_live_tick(symbol: str, timeframe: str) -> dict | None:
    """Get live candle update — lightweight, only fetches last 2 candles."""
    if not bot_engine or not bot_engine.broker:
        return None
    try:
        candles = await bot_engine.broker.get_candles(symbol, timeframe, 2)
        if candles:
            c = candles[-1]
            tick = {"time": int(c.time), "open": c.open, "high": c.high,
                    "low": c.low, "close": c.close, "volume": c.volume}
            _last_candle_cache[f"{symbol}_{timeframe}"] = tick
            return tick
    except Exception:
        pass
    return _last_candle_cache.get(f"{symbol}_{timeframe}")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    # Track what the client is viewing for targeted live candle
    client_symbol = None
    client_tf = None
    client_symbols = []  # multi-chart support
    try:
        while True:
            await _refresh_mt5_cache()
            account = await _get_account_info()

            # Get live candles for client's subscribed symbols
            live_candles = {}
            if client_symbols and client_tf:
                for sym in client_symbols[:6]:
                    tick = await _get_live_tick(sym, client_tf)
                    if tick:
                        live_candles[sym] = tick

            data = {
                "type": "update",
                "zones": _serialize_zones(),
                "positions": _serialize_positions(),
                "stats": _serialize_stats(),
                "account": account,
                "signals": bot_engine.signals_log[-10:] if bot_engine else [],
                "timestamp": time.time(),
            }
            if live_candles:
                data["live_candles"] = live_candles
            # Legacy single candle support
            if client_symbol and client_tf:
                lc = await _get_live_tick(client_symbol, client_tf)
                if lc:
                    data["live_candle"] = lc

            await websocket.send_json(data)
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                # Client can send {"symbol":"EURUSD","tf":"M1"} to subscribe
                try:
                    req = json.loads(msg)
                    if req.get("symbol"):
                        client_symbol = req["symbol"]
                    if req.get("tf"):
                        client_tf = req["tf"]
                    if req.get("symbols"):
                        client_symbols = req["symbols"][:6]
                except Exception:
                    pass
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
