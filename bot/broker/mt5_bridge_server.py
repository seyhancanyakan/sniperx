#!/usr/bin/env python3
"""PinShot Bot — MT5 Bridge Server (runs on Windows PC alongside MT5)

This server exposes HFM MT5 operations via HTTP so the Hetzner bot can
communicate with MT5 remotely.

Usage:
    python mt5_bridge_server.py --port 5555 --api-key YOUR_SECRET_KEY

Security:
    - API key authentication via X-API-Key header
    - IP whitelist (set ALLOWED_IPS env var, comma-separated)
"""

import argparse
import os
import logging
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("mt5_bridge")

app = FastAPI(title="PinShot MT5 Bridge")

# Config
API_KEY = os.environ.get("MT5_BRIDGE_KEY", "")
ALLOWED_IPS = os.environ.get("ALLOWED_IPS", "").split(",") if os.environ.get("ALLOWED_IPS") else []
MAGIC = 20260316

# MT5
try:
    import MetaTrader5 as mt5
    MT5_OK = True
except ImportError:
    mt5 = None
    MT5_OK = False
    logger.error("MetaTrader5 package not found. pip install MetaTrader5")


def check_auth(request: Request):
    """Verify API key and IP whitelist."""
    if API_KEY:
        key = request.headers.get("X-API-Key", "")
        if key != API_KEY:
            raise HTTPException(status_code=403, detail="Invalid API key")
    if ALLOWED_IPS and ALLOWED_IPS[0]:
        client_ip = getattr(request.client, 'host', '') if request.client else ''
        if client_ip and client_ip not in ALLOWED_IPS:
            raise HTTPException(status_code=403, detail=f"IP {client_ip} not allowed")


@app.get("/")
async def root():
    return {"status": "ok", "service": "SniperX MT5 Bridge"}


TF_MAP = {}
if MT5_OK:
    TF_MAP = {
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1,
    }


@app.on_event("startup")
async def startup():
    if not MT5_OK:
        logger.error("Cannot start without MetaTrader5 package")
        return
    login = int(os.environ.get("MT5_LOGIN", "0"))
    password = os.environ.get("MT5_PASSWORD", "")
    server = os.environ.get("MT5_SERVER", "HFMarkets-Demo")
    path = os.environ.get("MT5_PATH", "")

    kwargs = {"login": login, "password": password, "server": server}
    if path:
        kwargs["path"] = path

    if not mt5.initialize(**kwargs):
        logger.error(f"MT5 init failed: {mt5.last_error()}")
    else:
        info = mt5.account_info()
        logger.info(f"MT5 connected: {info.login} | {info.balance} {info.currency}")


@app.on_event("shutdown")
async def shutdown():
    if MT5_OK:
        mt5.shutdown()


# ─── Endpoints ───


def ensure_mt5():
    """Auto-reconnect to MT5 if connection lost."""
    if not MT5_OK:
        return False
    info = mt5.account_info()
    if info is not None:
        return True
    # Try to reconnect
    login = int(os.environ.get("MT5_LOGIN", "0"))
    password = os.environ.get("MT5_PASSWORD", "")
    server = os.environ.get("MT5_SERVER", "HFMarkets-Demo")
    path = os.environ.get("MT5_PATH", "")
    kwargs = {"login": login, "password": password, "server": server}
    if path:
        kwargs["path"] = path
    if mt5.initialize(**kwargs):
        info = mt5.account_info()
        logger.info(f"MT5 reconnected: {info.login if info else '?'}")
        return True
    logger.warning(f"MT5 reconnect failed: {mt5.last_error()}")
    return False


@app.get("/reconnect")
async def reconnect(_=Depends(check_auth)):
    """Force MT5 reconnection."""
    if ensure_mt5():
        info = mt5.account_info()
        return {"status": "connected", "login": info.login if info else 0}
    return JSONResponse({"status": "failed", "error": str(mt5.last_error())}, 500)


@app.get("/candles/{symbol}/{timeframe}/{count}")
async def get_candles(symbol: str, timeframe: str, count: int, _=Depends(check_auth)):
    ensure_mt5()
    tf = TF_MAP.get(timeframe)
    if tf is None:
        return JSONResponse({"error": f"Unknown timeframe: {timeframe}"}, 400)
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None:
        return JSONResponse({"candles": [], "error": str(mt5.last_error())})
    candles = [
        {"time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]),
         "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["tick_volume"])}
        for r in rates
    ]
    return {"candles": candles}


@app.get("/price/{symbol}")
async def get_price(symbol: str, _=Depends(check_auth)):
    ensure_mt5()
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return {"bid": 0, "ask": 0, "spread": 0, "error": str(mt5.last_error())}
    return {"bid": tick.bid, "ask": tick.ask, "spread": round(tick.ask - tick.bid, 6)}


@app.get("/account")
async def get_account(_=Depends(check_auth)):
    ensure_mt5()
    info = mt5.account_info()
    if info is None:
        return {"error": str(mt5.last_error())}
    return {
        "balance": info.balance, "equity": info.equity,
        "margin": info.margin, "free_margin": info.margin_free,
        "currency": info.currency, "login": info.login,
    }


@app.get("/positions")
async def get_positions(_=Depends(check_auth)):
    positions = mt5.positions_get()
    if not positions:
        return {"positions": []}
    result = []
    for p in positions:
        if p.magic != MAGIC:
            continue
        result.append({
            "ticket": str(p.ticket), "symbol": p.symbol,
            "type": p.type,
            "direction": "buy" if p.type == 0 else "sell",
            "open_price": p.price_open, "price_current": p.price_current,
            "lot": p.volume,
            "sl": p.sl, "tp": p.tp, "pnl": p.profit,
            "swap": p.swap, "commission": getattr(p, 'commission', 0),
            "time": int(p.time),
        })
    return {"positions": result}


@app.get("/orders")
async def get_orders(_=Depends(check_auth)):
    orders = mt5.orders_get()
    if not orders:
        return {"orders": []}
    result = []
    for o in orders:
        if o.magic != MAGIC:
            continue
        result.append({
            "ticket": str(o.ticket), "symbol": o.symbol,
            "type": o.type, "price": o.price_open,
            "lot": o.volume_current, "sl": o.sl, "tp": o.tp,
        })
    return {"orders": result}


@app.post("/order/market")
async def place_market(body: dict, _=Depends(check_auth)):
    symbol = body["symbol"]
    direction = body["direction"]
    lot = body["lot"]
    sl = body.get("sl", 0.0)
    tp = body.get("tp", 0.0)

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return {"success": False, "message": "No tick data"}

    order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
    price = tick.ask if direction == "buy" else tick.bid

    # Auto-detect filling mode for symbol
    sym_info = mt5.symbol_info(symbol)
    filling = mt5.ORDER_FILLING_IOC
    if sym_info:
        if sym_info.filling_mode & 1:  # FOK
            filling = mt5.ORDER_FILLING_FOK
        elif sym_info.filling_mode & 2:  # IOC
            filling = mt5.ORDER_FILLING_IOC
        else:
            filling = mt5.ORDER_FILLING_RETURN

    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
        "volume": lot, "type": order_type, "price": price,
        "sl": sl, "tp": tp, "magic": MAGIC,
        "comment": "SniperX", "type_filling": filling,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        msg = str(mt5.last_error()) if result is None else f"{result.retcode}: {result.comment}"
        return {"success": False, "message": msg}
    return {"success": True, "ticket": str(result.order), "fill_price": result.price}


@app.post("/order/limit")
async def place_limit(body: dict, _=Depends(check_auth)):
    symbol = body["symbol"]
    direction = body["direction"]
    price = body["price"]
    lot = body["lot"]
    sl = body.get("sl", 0.0)
    tp = body.get("tp", 0.0)

    # Auto-detect filling mode
    sym_info = mt5.symbol_info(symbol)
    filling = mt5.ORDER_FILLING_IOC
    if sym_info:
        if sym_info.filling_mode & 1:
            filling = mt5.ORDER_FILLING_FOK
        elif sym_info.filling_mode & 2:
            filling = mt5.ORDER_FILLING_IOC
        else:
            filling = mt5.ORDER_FILLING_RETURN

    order_type = mt5.ORDER_TYPE_BUY_LIMIT if direction == "buy" else mt5.ORDER_TYPE_SELL_LIMIT
    request = {
        "action": mt5.TRADE_ACTION_PENDING, "symbol": symbol,
        "volume": lot, "type": order_type, "price": price,
        "sl": sl, "tp": tp, "magic": MAGIC,
        "comment": "SniperX", "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        msg = str(mt5.last_error()) if result is None else f"{result.retcode}: {result.comment}"
        return {"success": False, "message": msg}
    return {"success": True, "ticket": str(result.order)}


@app.put("/position/modify")
async def modify_position(body: dict, _=Depends(check_auth)):
    ticket = int(body["ticket"])
    sl = body.get("sl")
    tp = body.get("tp")

    pos = mt5.positions_get(ticket=ticket)
    if pos and len(pos) > 0:
        p = pos[0]
        request = {
            "action": mt5.TRADE_ACTION_SLTP, "position": ticket,
            "symbol": p.symbol,
            "sl": sl if sl is not None else p.sl,
            "tp": tp if tp is not None else p.tp,
            "magic": MAGIC,
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return {"success": True}
        return {"success": False, "message": str(result)}
    return {"success": False, "message": "Position not found"}


@app.post("/position/close")
async def close_position(body: dict, _=Depends(check_auth)):
    ticket = int(body["ticket"])
    lot = body.get("lot")

    pos = mt5.positions_get(ticket=ticket)
    if not pos or len(pos) == 0:
        return {"success": False, "message": "Position not found"}

    p = pos[0]
    close_type = mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY
    tick = mt5.symbol_info_tick(p.symbol)
    price = tick.bid if p.type == 0 else tick.ask
    volume = lot if lot else p.volume

    # Auto-detect filling mode
    sym_info = mt5.symbol_info(p.symbol)
    filling = mt5.ORDER_FILLING_IOC
    if sym_info:
        if sym_info.filling_mode & 1:
            filling = mt5.ORDER_FILLING_FOK
        elif sym_info.filling_mode & 2:
            filling = mt5.ORDER_FILLING_IOC
        else:
            filling = mt5.ORDER_FILLING_RETURN

    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol,
        "volume": volume, "type": close_type, "position": ticket,
        "price": price, "magic": MAGIC,
        "comment": "SniperX close", "type_filling": filling,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        return {"success": True}
    return {"success": False, "message": str(result)}


@app.get("/history")
async def get_history(days: int = 30, _=Depends(check_auth)):
    """Get closed deal history for the last N days."""
    from datetime import datetime, timedelta
    date_from = datetime.now() - timedelta(days=days)
    date_to = datetime.now() + timedelta(days=1)

    deals = mt5.history_deals_get(date_from, date_to)
    if not deals:
        return {"deals": [], "debug": "no deals from MT5"}

    # Group deals by position_id to reconstruct trades
    entries = {}  # position_id -> entry deal
    exits = {}    # position_id -> exit deal

    for d in deals:
        if d.magic != MAGIC:
            continue
        pid = str(d.position_id)
        deal_data = {
            "ticket": str(d.ticket),
            "direction": "buy" if d.type == 0 else "sell",
            "price": d.price,
            "volume": d.volume,
            "profit": d.profit,
            "swap": d.swap,
            "commission": getattr(d, 'commission', 0),
            "time": int(d.time),
            "symbol": d.symbol,
        }
        if d.entry == 0:  # DEAL_ENTRY_IN
            entries[pid] = deal_data
        elif d.entry == 1:  # DEAL_ENTRY_OUT
            exits[pid] = deal_data

    # Build trade summaries from matched entry+exit pairs
    result = []
    for pid, entry in entries.items():
        ext = exits.get(pid)
        total_profit = entry["profit"] + (ext["profit"] if ext else 0)
        total_swap = entry["swap"] + (ext["swap"] if ext else 0)
        total_comm = entry["commission"] + (ext["commission"] if ext else 0)
        result.append({
            "position_id": pid,
            "symbol": entry["symbol"],
            "direction": entry["direction"],
            "entry_price": entry["price"],
            "exit_price": ext["price"] if ext else 0,
            "volume": entry["volume"],
            "profit": round(total_profit, 2),
            "swap": round(total_swap, 2),
            "commission": round(total_comm, 2),
            "net_profit": round(total_profit + total_swap + total_comm, 2),
            "open_time": entry["time"],
            "close_time": ext["time"] if ext else 0,
            "is_closed": ext is not None,
        })

    result.sort(key=lambda x: x.get("close_time", 0), reverse=True)
    return {"deals": result, "total_raw_deals": len(deals)}


@app.delete("/order/{ticket}")
async def cancel_order(ticket: int, _=Depends(check_auth)):
    request = {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        return {"success": True}
    return {"success": False, "message": str(result)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PinShot MT5 Bridge Server")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--api-key", type=str, default="")
    args = parser.parse_args()
    if args.api_key:
        API_KEY = args.api_key
    uvicorn.run(app, host=args.host, port=args.port)
