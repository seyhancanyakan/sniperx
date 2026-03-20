#!/usr/bin/env python3
"""PinShot AI Trading Bot — Main Entry Point

Orchestrates zone detection, trade management, AI validation, and web dashboard.
Supports Paper, HFM MT5 (direct), and MT5 Bridge (remote) brokers.
"""

import asyncio
import argparse
import logging
import time
import yaml
import os
from pathlib import Path
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("pinshot")


class PinShotBot:
    # ─── CLUSTER RISK GROUPS ───
    RISK_CLUSTERS = {
        "usd_major": ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD"],
        "jpy_cross": ["EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY"],
        "eur_cross": ["EURGBP", "EURCAD", "EURAUD"],
        "gbp_cross": ["GBPAUD", "GBPCAD"],
        "aud_cross": ["AUDCAD", "AUDNZD", "AUDCHF"],
        "metals": ["XAUUSD", "XAGUSD"],
        "indices": ["US100", "US30", "US500", "DE40"],
    }

    def __init__(self, config_path: str = None, broker_override: str = None):
        load_dotenv()
        self.config = self._load_config(config_path)
        if broker_override:
            self.config["broker"]["provider"] = broker_override
        self.broker = None
        self.trade_manager = None
        self.ai_engine = None
        self.telegram = None
        self.zones: dict = {}
        self.positions: list = []
        self.signals_log: list = []
        self.stats = {"total_r": 0.0, "wins": 0, "losses": 0, "trades": 0}
        self.running = False
        self._active_trades = set()
        self._traded_zones_file = str(Path(__file__).parent / "config" / "traded_zones.json")
        self._traded_zones = self._load_traded_zones()

    def _load_traded_zones(self) -> dict:
        """Load traded zones with timestamps. Format: {zone_key: timestamp}"""
        try:
            with open(self._traded_zones_file) as f:
                import json
                data = json.load(f)
                # Migrate from old list format
                if isinstance(data, list):
                    return {k: time.time() for k in data}
                return data
        except Exception:
            return {}

    def _save_traded_zone(self, zone_key: str):
        """Save zone as traded with current timestamp."""
        self._traded_zones[zone_key] = time.time()
        self._cleanup_stale_traded_zones()
        try:
            import json
            with open(self._traded_zones_file, "w") as f:
                json.dump(self._traded_zones, f)
        except Exception:
            pass

    # Timeframe-based TTL for traded zones (in days)
    _TF_TTL_DAYS = {
        "M5": 2,
        "M15": 4,
        "M30": 7,
        "H1": 21,
        "H4": 45,
    }
    _DEFAULT_TTL_DAYS = 3

    def _cleanup_stale_traded_zones(self):
        """Remove traded zones older than TTL based on timeframe.
        Zone key format: SYMBOL_TIMEFRAME_HIGH_LOW (e.g. EURUSD_M15_1.12345_1.12300)
        """
        now = time.time()
        # Load TTL config from settings if available
        sm = self.config.get("state_management", {})
        ttl_config = sm.get("traded_zone_ttl_days", self._TF_TTL_DAYS)
        default_ttl_days = self._DEFAULT_TTL_DAYS

        expired = []
        for k, ts in self._traded_zones.items():
            # Parse timeframe from zone key (SYMBOL_TIMEFRAME_HIGH_LOW)
            parts = k.split("_")
            tf = parts[1] if len(parts) >= 4 else ""
            ttl_days = ttl_config.get(tf, default_ttl_days)
            ttl_seconds = ttl_days * 86400
            if now - ts > ttl_seconds:
                expired.append(k)
        for k in expired:
            del self._traded_zones[k]

    def _load_config(self, path: str = None) -> dict:
        config_path = path or str(Path(__file__).parent / "config" / "settings.yaml")
        with open(config_path) as f:
            return yaml.safe_load(f)

    async def initialize(self):
        """Initialize broker, trade manager, AI engine, and notifications."""
        provider = self.config["broker"]["provider"]

        if provider == "paper":
            from .broker.paper import PaperBroker
            self.broker = PaperBroker()
            logger.info("Using Paper broker (simulated)")
        elif provider == "mt5":
            from .broker.mt5_hfm import MT5HFMBroker
            cfg = self.config["broker"]
            self.broker = MT5HFMBroker(
                login=cfg.get("mt5_login", 0),
                password=cfg.get("mt5_password", ""),
                server=cfg.get("mt5_server", "HFMarkets-Demo"),
                path=cfg.get("mt5_path", ""),
            )
            await self.broker.connect()
            logger.info("Connected to HFM MT5")
        elif provider == "mt5_bridge":
            from .broker.mt5_bridge_client import MT5BridgeClient
            cfg = self.config["broker"]
            self.broker = MT5BridgeClient(
                bridge_url=cfg.get("bridge_url", ""),
                api_key=cfg.get("bridge_api_key", ""),
            )
            logger.info(f"Using MT5 Bridge: {cfg.get('bridge_url', '')}")
        elif provider == "live_data":
            from .broker.live_data import LiveDataBroker
            self.broker = LiveDataBroker()
            logger.info("Using Live Data broker (Yahoo Finance + Paper trading)")
        else:
            raise ValueError(f"Unknown broker provider: {provider}")

        # Trade manager — start with empty positions (MT5 is source of truth)
        from .core.trade_manager import TradeManager
        self.trade_manager = TradeManager(self.broker, self.config.get("execution", {}))
        self.trade_manager.positions = []  # Clean start
        self._active_trades = set()  # Track symbol/tf combos

        # AI engine
        from .core.ai_engine import AIEngine
        self.ai_engine = AIEngine(self.config.get("ai", {}))

        # Telegram
        tg = self.config.get("telegram", {})
        if tg.get("enabled"):
            from .notifications.telegram import TelegramNotifier
            self.telegram = TelegramNotifier(
                tg.get("bot_token", ""),
                tg.get("chat_id", ""),
            )

    async def scan_loop(self):
        """Main scanning loop — scans ALL symbols x ALL timeframes."""
        await asyncio.sleep(2)

        # Load existing MT5 orders/positions — sync to trade_manager + dashboard
        try:
            from .core.trade_manager import Position
            existing_orders = await self.broker.get_pending_orders()
            existing_pos = await self.broker.get_open_positions()
            for o in existing_orders:
                sym = o.get("symbol", "")
                ticket = str(o.get("ticket", ""))
                self._active_trades.add(f"{sym}:existing:{ticket}")
                # Add to trade_manager so dashboard can show them
                if not any(p.ticket == ticket for p in self.trade_manager.positions):
                    direction = "buy" if o.get("type", 0) in (0, 2, 4) else "sell"
                    entry = o.get("price", o.get("open_price", 0))
                    sl = o.get("sl", 0)
                    tp = o.get("tp", 0)
                    lot = o.get("volume", o.get("lot", 0.01))
                    pip = self._get_pip(sym)
                    risk = abs(entry - sl) / pip if sl > 0 and pip > 0 else 0
                    pos = Position(
                        ticket=ticket, symbol=sym, direction=direction,
                        entry_price=entry, stop_loss=sl,
                        take_profit_1=tp, take_profit_2=tp,
                        lot_size=lot, risk_pips=risk,
                        status="pending",
                    )
                    self.trade_manager.positions.append(pos)
                logger.info(f"Existing order: {sym} ticket={ticket}")
            for p in existing_pos:
                sym = p.get("symbol", "")
                ticket = str(p.get("ticket", ""))
                self._active_trades.add(f"{sym}:existing:{ticket}")
                if not any(pos.ticket == ticket for pos in self.trade_manager.positions):
                    direction = "buy" if p.get("type", 0) == 0 else "sell"
                    entry = p.get("price", p.get("open_price", 0))
                    sl = p.get("sl", 0)
                    tp = p.get("tp", 0)
                    lot = p.get("volume", p.get("lot", 0.01))
                    pip = self._get_pip(sym)
                    risk = abs(entry - sl) / pip if sl > 0 and pip > 0 else 0
                    profit = p.get("profit", 0)
                    risk_money = risk * pip * lot * 100000 if pip > 0 else 1
                    r_result = round(profit / risk_money, 2) if risk_money > 0 else 0
                    pos = Position(
                        ticket=ticket, symbol=sym, direction=direction,
                        entry_price=entry, stop_loss=sl,
                        take_profit_1=tp, take_profit_2=tp,
                        lot_size=lot, risk_pips=risk,
                        status="open", r_result=r_result,
                    )
                    self.trade_manager.positions.append(pos)
                logger.info(f"Existing position: {sym} ticket={ticket}")
            self.positions = self.trade_manager.positions
        except Exception as e:
            logger.error(f"Failed to load existing trades: {e}")

        while self.running:
            symbols = self.config.get("symbols", [])
            timeframes = self.config.get("scan_timeframes", ["M1", "M5", "M15", "M30", "H1"])

            for sym in symbols:
                sym_name = sym["name"] if isinstance(sym, dict) else sym
                for tf in timeframes:
                    try:
                        await self._scan_symbol(sym_name, tf)
                    except Exception as e:
                        logger.error(f"Scan error {sym_name}/{tf}: {e}")
                    # Yield control so web server can respond
                    await asyncio.sleep(0.1)
            await asyncio.sleep(self.config.get("scan_interval", 5))

    def _get_pip(self, symbol: str) -> float:
        """Symbol-aware pip calculation."""
        sym = symbol.upper().replace("_", "")
        # JPY pairs
        for jpy in ("USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY"):
            if jpy in sym:
                return 0.01
        # Gold
        if "XAUUSD" in sym:
            return 0.01
        # Silver
        if "XAGUSD" in sym:
            return 0.001
        # Indices
        for idx in ("US100", "US30", "US500", "DE40", "JP225", "UK100"):
            if idx in sym:
                return 0.1
        return 0.0001

    def _get_cluster_for_symbol(self, symbol: str) -> str:
        """Return cluster name for symbol, or empty string."""
        sym = symbol.upper().replace("_", "")
        for cluster_name, members in self.RISK_CLUSTERS.items():
            if sym in members:
                return cluster_name
        return ""

    def _cluster_open_count(self, cluster_name: str) -> int:
        """Count open positions in the same cluster."""
        if not cluster_name or not self.trade_manager:
            return 0
        members = self.RISK_CLUSTERS.get(cluster_name, [])
        count = 0
        for pos in self.trade_manager.positions:
            if pos.status in ("open", "partial", "pending"):
                if pos.symbol.upper().replace("_", "") in members:
                    count += 1
        return count

    def _cluster_open_risk(self, cluster_name: str) -> float:
        """Sum risk_percent exposure in the same cluster (approximate by position count * risk_percent)."""
        if not cluster_name or not self.trade_manager:
            return 0.0
        ex = self.config.get("execution", {})
        risk_per_pos = ex.get("risk_percent", 2.0)
        return self._cluster_open_count(cluster_name) * risk_per_pos

    def _increment_manual_zone_counter(self, zone_id: str, field: str):
        """Increment a counter on a manual zone in the JSON file."""
        try:
            import json as _json
            mz_file = str(Path(__file__).parent / "config" / "manual_zones.json")
            with open(mz_file) as f:
                zones = _json.load(f)
            # zone_id is "manual_XXXX", extract the id part
            mid = zone_id.replace("manual_", "")
            for z in zones:
                if z["id"] == mid:
                    z[field] = z.get(field, 0) + 1
                    break
            with open(mz_file, "w") as f:
                _json.dump(zones, f, indent=2)
        except Exception:
            pass

    async def _scan_symbol(self, symbol: str, timeframe: str):
        from .core.detector import (detect_zones, calculate_atr, is_first_touch,
                                     is_flip_first_touch, update_zone_with_flip,
                                     update_zone_state, ZoneConfig)
        from .core.filters import combined_filter

        zd = self.config.get("zone_detection", {})
        ex = self.config.get("execution", {})

        candles = await self.broker.get_candles(symbol, timeframe, 500)
        if len(candles) < 50:
            return

        # ─── TF-aware config ───
        # Convert max_zone_age_minutes to bars for this timeframe
        tf_seconds = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
                      "H1": 3600, "H4": 14400, "D1": 86400}.get(timeframe, 900)
        max_age_min = zd.get("max_zone_age_minutes", 2880)  # default 48h
        max_stale = max(20, int(max_age_min * 60 / tf_seconds))
        lookback_bars = max(50, int(zd.get("lookback_minutes", 4320) * 60 / tf_seconds))  # default 72h
        lookback_bars = min(lookback_bars, 450)  # cap to prevent scanning too far

        cfg = ZoneConfig(
            spike_atr_mult=zd.get("spike_atr_mult", 1.0),
            base_min_candles=zd.get("base_min_candles", 2),
            max_cluster_candles=zd.get("base_max_candles", 6),
            left_side_max_swings=zd.get("left_side_max_swings", 5),
            lookback=lookback_bars,
            min_displacement=zd.get("min_displacement", 1.0),
            max_stale_bars=max_stale,
            use_trend_filter=zd.get("use_trend_filter", True),
            conflict_overlap_pct=zd.get("conflict_overlap_pct", 0.3),
            conflict_distance_atr=zd.get("conflict_distance_atr", 0.5),
            opposite_cooldown_bars=zd.get("opposite_cooldown_bars", 10),
            allow_opposite_only_after_bos=zd.get("allow_opposite_only_after_bos", True),
        )

        atr = calculate_atr(candles, 14)
        zone_key = f"{symbol}_{timeframe}"

        # ─── ZONE STORE: structural detect + incremental state ───
        # Step 1: Detect structural zone candidates (no state update inside)
        structural_zones = detect_zones(candles, zd.get("atr_period", 14), cfg)

        # Step 2: Merge with persistent zone store
        existing = self.zones.get(zone_key, [])
        existing_ids = {z.id for z in existing}

        for nz in structural_zones:
            if nz.id not in existing_ids:
                # New zone — do one-time historical state initialization
                nz._in_touch = False
                nz._last_processed_time = 0
                impulse_time = candles[min(nz.impulse_idx, len(candles)-1)].time if nz.impulse_idx < len(candles) else 0
                for c in candles:
                    if c.time <= impulse_time:
                        continue
                    # Find candle index for state update
                    cidx = next((j for j in range(len(candles)) if candles[j].time == c.time), 0)
                    update_zone_state(nz, c, cidx, cfg)
                    nz._last_processed_time = c.time
                nz._state_initialized = True
                existing.append(nz)
                logger.info(f"NEW_ZONE: {nz.zone_type.upper()} {symbol}/{timeframe} "
                            f"[{nz.status}] H:{nz.high:.5f} L:{nz.low:.5f} pins={nz.pin_count} "
                            f"disp={nz.displacement}x conf={nz.confidence:.0f} "
                            f"touch={nz.touch_count}")

        # Step 3: Incremental update for existing zones — only NEW closed candles
        for z in existing:
            if not getattr(z, '_state_initialized', False):
                continue
            last_time = getattr(z, '_last_processed_time', 0)
            for c in candles:
                if c.time <= last_time:
                    continue
                cidx = next((j for j in range(len(candles)) if candles[j].time == c.time), 0)
                z._in_touch = getattr(z, '_in_touch', False)
                update_zone_state(z, c, cidx, cfg)
                z._last_processed_time = c.time

        # Step 4: Prune very old zones (keep max 20 per symbol/tf)
        existing = [z for z in existing if not (z.is_stale and z.touch_count == 0 and z.bars_since_creation > cfg.max_stale_bars * 2)]
        existing = existing[-20:]

        zones = existing
        self.zones[zone_key] = zones

        # ─── REJECT REASON STATS ───
        reject_counts = {}
        for z in structural_zones:
            for r in z.reject_reasons:
                key = r.split(":")[0] if ":" in r else r
                reject_counts[key] = reject_counts.get(key, 0) + 1
        if reject_counts:
            logger.debug(f"REJECT_STATS {symbol}/{timeframe}: {reject_counts}")

        valid_zones = []
        new_flips = []
        for zone in zones:
            # Only trade fresh zones — no burned/departed (already touched = lower quality)
            if zone.is_stale or zone.is_invalid:
                continue

            # Check for flip (role reversal)
            flipped = update_zone_with_flip(zone, candles, len(candles) - 1, atr)
            if flipped:
                new_flips.append(flipped)
                logger.info(f"FLIP: {zone.zone_type}→{flipped.zone_type} {symbol}/{timeframe} "
                            f"break_str={flipped.breakout_strength_atr:.1f}")

            if not zone.is_invalid:
                valid_zones.append(zone)

        # Add flipped zones to valid list
        valid_zones.extend(new_flips)

        # ─── MANUAL ZONES: inject into scan ───
        try:
            import json as _json
            mz_file = str(Path(__file__).parent / "config" / "manual_zones.json")
            with open(mz_file) as _f:
                manual_zones = _json.load(_f)
            for mz in manual_zones:
                if mz.get("symbol") != symbol:
                    continue
                # Manuel zone herhangi bir scan TF'de trade edilebilir (M1'de cizilse bile M5'te calisir)
                if not mz.get("trade_active", True):
                    continue  # Visual only, skip trade logic
                from .core.detector import Zone
                fake_zone = Zone(
                    zone_type=mz["zone_type"],
                    high=mz["high"],
                    low=mz["low"],
                    start_idx=max(0, len(candles) - 50),
                    end_idx=max(0, len(candles) - 45),
                    start_time=mz.get("start_time", 0),
                    end_time=mz.get("end_time", 0),
                    confidence=mz.get("confidence", 90),
                    status="active",
                    is_fresh=True,
                    quality_score=90,
                    core_high=mz["high"],
                    core_low=mz["low"],
                    midpoint=(mz["high"] + mz["low"]) / 2,
                    id=f"manual_{mz['id']}",
                    displacement=3.0,
                    is_departed=True,
                    trade_eligible=True,
                )
                # Manual zone expiry logic
                expiry_mode = mz.get("expiry_mode", "until_first_trade")
                if expiry_mode == "until_first_trade" and mz.get("trade_count", 0) > 0:
                    continue  # Already traded once
                if expiry_mode == "until_first_fill" and mz.get("fill_count", 0) > 0:
                    continue  # Already filled once
                if expiry_mode == "until_expiry_bars":
                    tf_expiry = {"M5": 40, "M15": 30, "M30": 20, "H1": 15, "H4": 10}
                    max_bars = tf_expiry.get(timeframe, 30)
                    created = mz.get("created_at", 0)
                    tf_secs = {"M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400}
                    elapsed_bars = (time.time() - created) / tf_secs.get(timeframe, 900) if created else 0
                    if elapsed_bars > max_bars:
                        continue  # Expired
                fake_zone.is_invalid = False
                fake_zone.is_stale = False
                fake_zone.first_touch_taken = False
                fake_zone.touch_count = 0
                fake_zone._is_manual = True
                valid_zones.append(fake_zone)
                logger.info(f"MANUAL ZONE: {mz['zone_type']} {symbol}/{timeframe} "
                            f"H:{mz['high']:.5f} L:{mz['low']:.5f}")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug(f"Manual zone load error: {e}")

        # ─── Compute regime & MTF bias for ALL zones (dashboard display) ───
        regime = None
        regime_cfg = self.config.get("regime", {})
        if regime_cfg.get("enabled", False):
            try:
                from .core.regime import classify_regime
                regime = classify_regime(candles, regime_cfg)
            except Exception as e:
                logger.debug(f"Regime calc error {symbol}/{timeframe}: {e}")

        mtf_bias = None
        mtf_cfg = self.config.get("multi_timeframe", {})
        if mtf_cfg.get("enabled", False):
            try:
                from .core.mtf_bias import get_higher_tf_bias, BIAS_MAP
                higher_tf = mtf_cfg.get("bias_map", BIAS_MAP).get(timeframe)
                if higher_tf:
                    mtf_bias = await get_higher_tf_bias(self.broker, symbol, higher_tf, mtf_cfg)
            except Exception as e:
                logger.debug(f"MTF bias error {symbol}/{timeframe}: {e}")

        # ─── LEARNED PATTERN BONUS (approved patterns only) ───
        try:
            from .core.zone_learner import ZoneLearner
            learner = ZoneLearner(self)
            for zone in valid_zones:
                is_manual = getattr(zone, '_is_manual', False)
                if is_manual:
                    continue  # Manual zones already have user-set confidence
                bonus = learner.get_confidence_bonus(
                    zone_type=zone.zone_type,
                    timeframe=timeframe,
                    symbol=symbol,
                    is_flipped=zone.is_flipped,
                )
                if bonus != 0:
                    old_conf = zone.confidence
                    zone.confidence = min(max(zone.confidence + bonus, 0), 100)
                    zone._learned_bonus = bonus
                    logger.info(f"PATTERN_BONUS_APPLIED: {symbol}/{timeframe} "
                                f"{zone.zone_type} conf {old_conf:.0f} -> {zone.confidence:.0f} "
                                f"bonus={bonus:+.1f}")
                else:
                    zone._learned_bonus = 0
        except Exception as e:
            logger.debug(f"Learned pattern bonus error: {e}")

        # Set regime/bias metadata on ALL zones for dashboard
        for zone in valid_zones:
            zone._regime = regime.regime if regime else ""
            zone._higher_tf_bias = mtf_bias.bias if mtf_bias else ""
            zone._ai_validated = None
            zone._ai_confidence = 0.0
            zone._filter_reason = ""

        for zone in valid_zones:
            logger.info(f"ZONE: {zone.zone_type.upper()} {symbol}/{timeframe} "
                        f"[{zone.status}] H:{zone.high:.5f} L:{zone.low:.5f} "
                        f"disp={zone.displacement:.1f}x {'FLIP' if zone.is_flipped else ''}"
                        f"{' REG='+regime.regime if regime else ''}"
                        f"{' HTF='+mtf_bias.bias if mtf_bias else ''}")

            # Duplicate prevention (with TTL)
            zone_key = f"{symbol}_{timeframe}_{zone.high:.5f}_{zone.low:.5f}"
            if zone.is_flipped:
                zone_key += f"_flip_{zone.id}"
            if zone_key in self._traded_zones:
                continue

            # ─── TRADABLE CHECK: price must be IN the zone (±3 pip tolerance) ───
            pip = self._get_pip(symbol)
            proximity_buf = 3 * pip  # Only 3 pip — price must actually reach zone
            is_manual = getattr(zone, '_is_manual', False)
            last_price = candles[-1].close if candles else 0
            tradable = False

            if last_price <= 0:
                continue

            # Price must be inside zone or within 3 pips
            if zone.zone_type == "demand":
                if (zone.low - proximity_buf) <= last_price <= (zone.high + proximity_buf):
                    tradable = True
            elif zone.zone_type == "supply":
                if (zone.low - proximity_buf) <= last_price <= (zone.high + proximity_buf):
                    tradable = True

            if not tradable:
                continue

            # ─── CONFIRMATION CANDLE: require rejection signal before entry ───
            if not is_manual and len(candles) >= 3:
                last = candles[-1]
                prev = candles[-2]
                body = abs(last.close - last.open)
                upper_wick = last.high - max(last.close, last.open)
                lower_wick = min(last.close, last.open) - last.low
                total_range = last.high - last.low if last.high != last.low else pip

                confirmed = False
                if zone.zone_type == "demand":
                    # Need bullish signal: lower wick rejection, bullish close, or engulfing
                    if lower_wick > body * 0.5 and last.close > last.open:
                        confirmed = True  # Pin bar / hammer
                    elif last.close > last.open and last.close > prev.close:
                        confirmed = True  # Bullish close above prev
                    elif last.close > prev.high:
                        confirmed = True  # Bullish engulfing
                elif zone.zone_type == "supply":
                    # Need bearish signal: upper wick rejection, bearish close, or engulfing
                    if upper_wick > body * 0.5 and last.close < last.open:
                        confirmed = True  # Shooting star
                    elif last.close < last.open and last.close < prev.close:
                        confirmed = True  # Bearish close below prev
                    elif last.close < prev.low:
                        confirmed = True  # Bearish engulfing

                if not confirmed:
                    logger.debug(f"NO CONFIRM: {symbol}/{timeframe} {zone.zone_type} — waiting for rejection candle")
                    continue

            logger.info(f"TRADABLE+CONFIRMED: {zone.zone_type.upper()} {symbol}/{timeframe} "
                        f"price={last_price:.5f} zone=[{zone.low:.5f}-{zone.high:.5f}] "
                        f"status={zone.status} manual={is_manual}")

            # ─── QUALITY FILTER: combined_filter must pass (skip for manual zones) ───
            if not is_manual:
                min_conf = ex.get("min_confidence", 55)
                if zone.confidence < min_conf:
                    logger.info(f"SKIP {symbol}/{timeframe}: confidence {zone.confidence:.0f} < {min_conf}")
                    continue

                filter_result = combined_filter(zone, candles, len(candles) - 1,
                                                self.config.get("filters", {}))
                if not filter_result.valid:
                    logger.info(f"SKIP {symbol}/{timeframe}: filter failed: {filter_result.reason}")
                    continue

            # ─── REGIME FILTER (uses pre-computed regime from above, skip for manual) ───
            if not is_manual and regime and regime_cfg.get("enabled", False):
                is_counter_trend = (zone.zone_type == "demand" and regime.regime == "trend_down") or \
                                   (zone.zone_type == "supply" and regime.regime == "trend_up")
                if is_counter_trend and not regime_cfg.get("allow_countertrend_first_touch", False):
                    if not zone.is_flipped:
                        zone._filter_reason = f"counter-trend blocked: regime={regime.regime}"
                        logger.info(f"SKIP {symbol}/{timeframe}: {zone._filter_reason}")
                        continue
                if zone.is_flipped and regime.regime == "choppy":
                    zone._filter_reason = "flip blocked in choppy regime"
                    logger.info(f"SKIP {symbol}/{timeframe}: {zone._filter_reason}")
                    continue

            # ─── MULTI-TIMEFRAME BIAS (soft filter — reduce lot instead of blocking) ───
            mtf_aligned = True
            if not is_manual and mtf_bias and mtf_cfg.get("enabled", False):
                zone_dir = "bullish" if zone.zone_type == "demand" else "bearish"
                if mtf_bias.bias != zone_dir and mtf_bias.bias != "neutral":
                    mtf_aligned = False  # Will reduce lot size later, not block
                    zone._filter_reason = f"MTF bias={mtf_bias.bias} vs zone={zone_dir} (lot reduced)"
                    logger.info(f"MTF MISMATCH {symbol}/{timeframe}: {zone._filter_reason}")

            # ─── AI META-FILTER ───
            ai_result = None
            ai_cfg = self.config.get("ai", {})
            if ai_cfg.get("enabled", False) and ai_cfg.get("role") == "meta_filter":
                should_call_ai = True
                if ai_cfg.get("only_borderline", True):
                    bmin = ai_cfg.get("borderline_min_confidence", 55)
                    bmax = ai_cfg.get("borderline_max_confidence", 75)
                    should_call_ai = bmin <= zone.confidence <= bmax
                if should_call_ai:
                    try:
                        ai_result = await self.ai_engine.validate_zone(
                            zone, candles, atr,
                            regime=regime,
                            mtf_bias=mtf_bias,
                        )
                        zone._ai_validated = ai_result.get("valid", True)
                        zone._ai_confidence = float(ai_result.get("confidence", 0))
                        if not ai_result.get("valid", True):
                            if not ai_cfg.get("fail_open", True):
                                zone._filter_reason = f"AI rejected confidence={ai_result.get('confidence')}"
                                logger.info(f"SKIP {symbol}/{timeframe}: {zone._filter_reason}")
                                continue
                            else:
                                logger.info(f"AI rejected {symbol}/{timeframe} but fail_open=true, proceeding")
                    except Exception as e:
                        logger.warning(f"AI meta-filter error {symbol}/{timeframe}: {e}")
                        if not ai_cfg.get("fail_open", True):
                            zone._filter_reason = "AI error and fail_open=false"
                            logger.info(f"SKIP {symbol}/{timeframe}: {zone._filter_reason}")
                            continue

            # ─── ENTRY CALCULATION ───
            pip = self._get_pip(symbol)
            buf = ex.get("sl_buffer_pips", 3) * pip + ex.get("stop_buffer_atr", 0.0) * atr

            # Zone boundaries — WICK-based (iğneler dahil)
            # SL ve entry wick kenarından hesaplanır
            core_high = zone.high   # Wick dahil (zone üst kenarı)
            core_low = zone.low     # Wick dahil (zone alt kenarı)

            # Zone width filter: skip if zone too wide (> 1.5 ATR)
            zone_height = core_high - core_low
            if atr > 0 and zone_height > 1.5 * atr:
                logger.debug(f"SKIP: {symbol}/{timeframe} zone too wide {zone_height:.5f} > 1.5*ATR {1.5*atr:.5f}")
                continue

            # Entry placement
            entry_mode = ex.get("entry_placement", "mid")
            direction = "buy" if zone.zone_type == "demand" else "sell"

            if entry_mode == "mid":
                entry = (core_high + core_low) / 2
            elif entry_mode == "penetration":
                if direction == "buy":
                    entry = core_high - (core_high - core_low) * 0.25
                else:
                    entry = core_low + (core_high - core_low) * 0.25
            else:  # proximal
                entry = core_high if direction == "buy" else core_low

            # Stop: core distal edge
            if direction == "buy":
                sl = core_low - buf
            else:
                sl = core_high + buf

            risk = abs(entry - sl)
            if risk <= 0 or risk < pip:
                continue

            # ─── ATR-BASED MINIMUM SL: prevent noise stop-outs ───
            min_sl_distance = atr * 0.3  # At least 30% of ATR
            if risk < min_sl_distance:
                # Widen SL to minimum ATR distance
                if direction == "buy":
                    sl = entry - min_sl_distance
                else:
                    sl = entry + min_sl_distance
                risk = min_sl_distance
                logger.debug(f"SL widened to ATR minimum: {symbol}/{timeframe} risk={risk:.5f}")

            # TP calculation — respect partial TP config
            if ex.get("use_partial_tp", False):
                tp1_ratio = ex.get("tp1_rr", 2.0)
                tp2_ratio = ex.get("tp2_rr", 3.0)
                tp1 = entry + risk * tp1_ratio if direction == "buy" else entry - risk * tp1_ratio
                tp2 = entry + risk * tp2_ratio if direction == "buy" else entry - risk * tp2_ratio
            else:
                tp_ratio = ex.get("tp_ratio", 2.5)
                tp1 = entry + risk * tp_ratio if direction == "buy" else entry - risk * tp_ratio
                tp2 = tp1  # Same when no partial TP

            # ─── SPREAD GUARD ───
            max_spread = ex.get("max_spread_pips", 5.0)
            prices = await self.broker.get_price(symbol)
            spread_pips = prices.get("spread", 0) / pip
            if spread_pips > max_spread:
                logger.info(f"SKIP {symbol}: spread {spread_pips:.1f} > max {max_spread}")
                continue

            # ─── CLUSTER RISK LIMIT ───
            if ex.get("enable_cluster_risk", False):
                cluster_name = self._get_cluster_for_symbol(symbol)
                if cluster_name:
                    cluster_max = ex.get("cluster_max_risk_percent", 4.0)
                    cluster_risk = self._cluster_open_risk(cluster_name)
                    if cluster_risk >= cluster_max:
                        logger.info(f"SKIP: cluster {cluster_name} risk limit reached "
                                    f"({cluster_risk:.1f}% >= {cluster_max:.1f}%)")
                        continue

            # ─── ORDER PLACEMENT (zone NOT marked as used yet) ───
            max_pos = ex.get("max_positions", 3)
            zone_trade_key = f"{symbol}:{timeframe}:{zone.id}"
            if zone_trade_key in self._active_trades:
                continue  # Already traded this zone
            if self.trade_manager.get_open_count() < max_pos:
                from .core.trade_manager import Signal
                trade_signal = Signal(
                    symbol=symbol, direction=direction,
                    entry_price=round(entry, 5), stop_loss=round(sl, 5),
                    take_profit_1=round(tp1, 5), take_profit_2=round(tp2, 5),
                    lot_size=0.01, confidence=zone.confidence,
                )
                account = await self.broker.get_account()
                sl_pips = risk / pip
                raw_lot = self.trade_manager.calculate_lot_size(
                    symbol, sl_pips, ex.get("risk_percent", 3.0), account.balance
                )
                trade_signal.lot_size = min(max(raw_lot, 0.01), 1.0)

                # ─── CONFIDENCE-BASED RISK SCALING ───
                if zone.confidence < 70:
                    confidence_mult = 0.7
                elif zone.confidence > 85:
                    confidence_mult = 1.1
                else:
                    confidence_mult = 1.0
                trade_signal.lot_size = round(trade_signal.lot_size * confidence_mult, 2)

                # Regime risk multiplier
                if regime_cfg.get("enabled") and regime:
                    trade_signal.lot_size = round(
                        trade_signal.lot_size * regime.risk_multiplier, 2
                    )

                # AI risk adjustment
                if ai_result and ai_result.get("risk_adjustment"):
                    trade_signal.lot_size = round(
                        trade_signal.lot_size * ai_result["risk_adjustment"], 2
                    )

                # Manual zone risk reduction (0.75x — cautious sizing)
                if is_manual:
                    trade_signal.lot_size = round(trade_signal.lot_size * 0.75, 2)

                # MTF mismatch — trade with half lot (soft filter)
                if not mtf_aligned:
                    trade_signal.lot_size = round(trade_signal.lot_size * 0.5, 2)

                # Ensure bounds
                trade_signal.lot_size = min(max(trade_signal.lot_size, 0.01), 1.0)

                pos = await self.trade_manager.open_position(trade_signal)
                if not pos.ticket:
                    continue  # MT5 rejected — don't track
                pos.zone_id = zone.id
                pos.zone_key = zone_key  # Store for fill-based tracking
                pos.order_timeframe = timeframe  # Track TF for expiry/time stop
                self._active_trades.add(zone_trade_key)
                # NOTE: zone.first_touch_taken and _save_traded_zone are NOT set here.
                # They will be set when the order is FILLED (in the fill sync section below).
                # Track manual zone trade count
                if is_manual:
                    self._increment_manual_zone_counter(zone.id, "trade_count")

                self.signals_log.append({
                    "time": time.time(), "symbol": symbol,
                    "direction": direction,
                    "message": f"{direction.upper()} {symbol}/{timeframe} @ {entry:.5f} SL:{sl:.5f} TP:{tp1:.5f}"
                              f"{' [MANUAL]' if is_manual else ''}",
                    "type": "entry",
                })
                logger.info(f"TRADE [{ex.get('model','core_mid_2p5')}]: "
                            f"{direction.upper()} {symbol}/{timeframe} "
                            f"E:{entry:.5f} SL:{sl:.5f} TP:{tp1:.5f} "
                            f"risk={risk:.5f} R:R=1:{ex.get('tp_ratio', 2.5)}")
                if self.telegram:
                    await self.telegram.send_entry(trade_signal, zone)

        # Cancel pending orders for stale/invalid zones, but KEEP zones for dashboard display
        for zone in valid_zones:
            if zone.is_stale or zone.is_invalid:
                for pos in self.trade_manager.positions:
                    if pos.zone_id == zone.id and pos.status == "pending" and pos.ticket:
                        try:
                            await self.broker.cancel_order(pos.ticket)
                            pos.status = "cancelled"
                            logger.info(f"STALE CANCEL: zone={zone.id} ticket={pos.ticket}")
                        except Exception:
                            pass
                zone_trade_key = f"{symbol}:{timeframe}:{zone.id}"
                self._active_trades.discard(zone_trade_key)

        # zones already stored above in zone store section

        # ─── PENDING ORDER EXPIRY ───
        sm = self.config.get("state_management", {})
        expiry_cfg = sm.get("pending_order_expiry_bars", {})
        if expiry_cfg:
            for pos in self.trade_manager.positions:
                if pos.status != "pending" or not pos.ticket:
                    continue
                pos.order_bar_count += 1
                tf = pos.order_timeframe or timeframe
                max_bars = expiry_cfg.get(tf, 6)
                if pos.order_bar_count >= max_bars:
                    try:
                        await self.broker.cancel_order(pos.ticket)
                        pos.status = "cancelled"
                        zone_trade_key = f"{pos.symbol}:{tf}:{pos.zone_id}"
                        self._active_trades.discard(zone_trade_key)
                        logger.info(f"PENDING EXPIRED: {pos.ticket} after {pos.order_bar_count} bars "
                                    f"(tf={tf}, limit={max_bars})")
                    except Exception as e:
                        logger.error(f"Failed to cancel expired order {pos.ticket}: {e}")

        # Sync pending → open: check if MT5 filled any orders
        # CRITICAL: Only mark zone as "used" when order is actually FILLED
        try:
            mt5_pos = await self.broker.get_open_positions()
            mt5_tickets = {p.get("ticket","") for p in mt5_pos}
            for pos in self.trade_manager.positions:
                if pos.status == "pending" and pos.ticket in mt5_tickets:
                    pos.status = "open"
                    logger.info(f"ORDER FILLED: {pos.symbol} {pos.direction} ticket={pos.ticket}")
                    # NOW mark zone as used (on fill, not on order placement)
                    if hasattr(pos, 'zone_key') and pos.zone_key:
                        self._save_traded_zone(pos.zone_key)
                        logger.info(f"Zone marked as traded (filled): {pos.zone_key}")
        except Exception:
            pass

        # Cleanup: if pending order was cancelled/expired, free the zone for re-trade
        try:
            mt5_orders = await self.broker.get_pending_orders()
            mt5_order_tickets = {o.get("ticket","") for o in mt5_orders}
            for pos in self.trade_manager.positions:
                if pos.status == "pending" and pos.ticket not in mt5_order_tickets and pos.ticket not in mt5_tickets:
                    # Order disappeared without filling — zone should be freed
                    pos.status = "cancelled"
                    zone_trade_key = f"{pos.symbol}:{timeframe}:{pos.zone_id}"
                    self._active_trades.discard(zone_trade_key)
                    logger.info(f"UNFILLED ORDER EXPIRED: {pos.ticket} — zone freed for re-trade")
        except Exception:
            pass

        # Update current price and manage positions
        prices = await self.broker.get_price(symbol)
        current_price = prices.get("bid", 0)
        if current_price > 0:
            pip = self._get_pip(symbol)
            if hasattr(self.broker, 'paper'):
                self.broker.paper.update_price(symbol, current_price, current_price + 2 * pip)
            elif hasattr(self.broker, 'update_price'):
                self.broker.update_price(symbol, current_price, current_price + 2 * pip)

        zone_heights = {}
        if valid_zones:
            zone_heights[symbol] = abs(valid_zones[0].high - valid_zones[0].low)
        await self.trade_manager.manage_all_positions(
            {symbol: current_price}, zone_heights,
        )

        # Sync state
        self.positions = self.trade_manager.positions
        self._update_stats()

    def _update_stats(self):
        closed = [p for p in self.positions if p.status == "closed"]
        open_pos = [p for p in self.positions if p.status in ("open", "partial")]
        self.stats["trades"] = len(closed)
        self.stats["wins"] = len([p for p in closed if p.r_result > 0])
        self.stats["losses"] = len([p for p in closed if p.r_result <= 0])
        self.stats["total_r"] = round(sum(p.r_result for p in closed), 2)
        self.stats["open_r"] = round(sum(p.r_result for p in open_pos), 2)
        self.stats["open_count"] = len(open_pos)
        # Profit factor
        gross_profit = sum(p.r_result for p in closed if p.r_result > 0)
        gross_loss = abs(sum(p.r_result for p in closed if p.r_result <= 0))
        self.stats["profit_factor"] = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0.0

        # ─── LOCAL TRADE HISTORY: save closed trades ───
        for p in closed:
            hist_key = f"hist_{p.ticket}"
            if hist_key in self._active_trades:
                continue
            self._active_trades.add(hist_key)
            try:
                import json as _json
                hist_file = str(Path(__file__).parent / "config" / "trade_history.json")
                try:
                    with open(hist_file) as f:
                        history = _json.load(f)
                except Exception:
                    history = []
                # Don't duplicate
                if not any(h.get("ticket") == p.ticket for h in history):
                    history.append({
                        "ticket": p.ticket,
                        "position_id": p.ticket,
                        "symbol": p.symbol,
                        "direction": p.direction,
                        "entry_price": p.entry_price,
                        "exit_price": 0,
                        "volume": p.lot_size,
                        "profit": round(p.r_result, 2),
                        "net_profit": round(p.r_result, 2),
                        "swap": 0,
                        "commission": 0,
                        "r_result": round(p.r_result, 2),
                        "open_time": getattr(p, 'open_time', 0),
                        "close_time": time.time(),
                        "is_closed": True,
                        "zone_id": getattr(p, 'zone_id', ''),
                    })
                    with open(hist_file, "w") as f:
                        _json.dump(history, f, indent=2)
            except Exception:
                pass

        # ─── PHASE B: Outcome learning for manual zone trades ───
        for p in closed:
            zone_id = getattr(p, 'zone_id', '')
            if not zone_id or not zone_id.startswith('manual_'):
                continue
            outcome_key = f"outcome_{p.ticket}"
            if outcome_key in self._active_trades:
                continue  # Already processed
            self._active_trades.add(outcome_key)
            outcome = "tp" if p.r_result > 0 else "sl"
            mid = zone_id.replace("manual_", "")
            # Find manual zone data
            try:
                import json as _json
                mz_file = str(Path(__file__).parent / "config" / "manual_zones.json")
                with open(mz_file) as f:
                    mzones = _json.load(f)
                mz = next((z for z in mzones if z["id"] == mid), None)
                if mz:
                    import asyncio
                    from .core.zone_learner import ZoneLearner
                    learner = ZoneLearner(self)
                    asyncio.create_task(learner.learn_from_outcome(mz, outcome, p.r_result))
                    logger.info(f"PHASE B triggered: zone={mid} outcome={outcome} R={p.r_result:.2f}")
            except Exception as e:
                logger.debug(f"Phase B trigger error: {e}")

    async def run(self):
        """Start the bot: scanning loop + web dashboard."""
        self.running = True
        await self.initialize()

        logger.info("=" * 50)
        logger.info("  SNIPERX AI TRADING BOT")
        logger.info("=" * 50)
        logger.info(f"  Broker:  {self.config['broker']['provider']}")
        syms = [s['name'] if isinstance(s, dict) else s for s in self.config['symbols']]
        logger.info(f"  Symbols: {syms}")
        logger.info(f"  AI:      {self.config.get('ai', {}).get('provider', 'disabled')}")
        logger.info(f"  Web:     http://0.0.0.0:{self.config.get('web', {}).get('port', 8080)}")
        logger.info("=" * 50)

        import uvicorn
        from .web.server import app, set_bot_engine
        set_bot_engine(self)

        web = self.config.get("web", {})
        config = uvicorn.Config(
            app,
            host=web.get("host", "0.0.0.0"),
            port=web.get("port", 8080),
            log_level="info",
        )
        server = uvicorn.Server(config)

        await asyncio.gather(
            self.scan_loop(),
            server.serve(),
        )


def main():
    parser = argparse.ArgumentParser(description="PinShot AI Trading Bot")
    parser.add_argument("--config", type=str, help="Config file path")
    parser.add_argument("--broker", type=str,
                        choices=["paper", "mt5", "mt5_bridge"],
                        help="Broker override")
    parser.add_argument("--backtest", type=str, help="CSV file for backtesting")
    args = parser.parse_args()

    if args.backtest:
        from .backtest.engine import BacktestEngine
        from .backtest.report import ReportGenerator
        config_path = args.config or str(Path(__file__).parent / "config" / "settings.yaml")
        with open(config_path) as f:
            config = yaml.safe_load(f)
        engine = BacktestEngine(config)
        candles = engine.load_data(args.backtest)
        result = engine.run(candles)
        report = ReportGenerator(result)
        report.print_summary()
    else:
        bot = PinShotBot(config_path=args.config, broker_override=args.broker)
        asyncio.run(bot.run())


if __name__ == "__main__":
    main()
