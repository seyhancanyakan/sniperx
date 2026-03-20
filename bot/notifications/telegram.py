"""PinShot Bot — Telegram Notifications + Command Handler

Sends: zone detected, entry, TP, SL, hourly summary
Receives: /durum /pozisyon /zone /pnl /kasa commands
"""

import httpx
import asyncio
import logging
import time

logger = logging.getLogger("pinshot.telegram")


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.enabled = bool(bot_token and chat_id)
        self._last_update_id = 0
        self._bot_engine = None
        self._last_hourly = 0

    async def send(self, message: str):
        if not self.enabled:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(f"{self.base_url}/sendMessage", json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                })
        except Exception as e:
            logger.error(f"Telegram send error: {e}")

    async def send_zone_detected(self, zone, symbol: str, timeframe: str):
        arrow = "\u25b2" if zone.zone_type == "demand" else "\u25bc"
        gap_str = f"YES \u26a1" if zone.gap_count > 0 else "NO"
        msg = (
            f"\U0001f50d <b>YEN\u0130 ZONE TESP\u0130T</b>\n\n"
            f"\U0001f4cd {symbol} {timeframe} \u2014 {zone.zone_type.upper()} ZONE\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"Range: {zone.high:.5f} \u2014 {zone.low:.5f}\n"
            f"Pins: {zone.pin_count} \U0001f538 | Gap: {gap_str}\n"
            f"Breakout: {arrow} {zone.breakout_magnitude:.1f}x ATR\n"
            f"Triangular: {zone.triangular_score:.0f}% | Freshness: {zone.white_space_score:.0f}%\n"
            f"Confidence: {zone.confidence:.0f}%\n\n"
            f"\U0001f4cb Limit emir yerle\u015ftirildi: "
            f"{zone.zone_type.upper()} LIMIT @ {zone.high if zone.zone_type == 'demand' else zone.low:.5f}"
        )
        await self.send(msg)

    async def send_entry(self, signal, zone):
        pip_est = 0.01 if signal.entry_price > 10 else 0.0001
        sl_pips = abs(signal.entry_price - signal.stop_loss) / pip_est
        risk_amount = signal.lot_size * sl_pips * (6.50 if pip_est == 0.01 else 10.0)
        tp1_rr = abs(signal.take_profit_1 - signal.entry_price) / abs(signal.entry_price - signal.stop_loss) if abs(signal.entry_price - signal.stop_loss) > 0 else 0
        tp2_rr = abs(signal.take_profit_2 - signal.entry_price) / abs(signal.entry_price - signal.stop_loss) if abs(signal.entry_price - signal.stop_loss) > 0 else 0

        msg = (
            f"\U0001f3af <b>SNIPER ENTRY</b>\n\n"
            f"{signal.direction.upper()} {signal.symbol} @ {signal.entry_price:.5f}\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\U0001f6d1 SL: {signal.stop_loss:.5f} ({sl_pips:.1f} pip)\n"
            f"\u2705 TP1: {signal.take_profit_1:.5f} (1:{tp1_rr:.0f}) \u2014 yar\u0131 kapat\n"
            f"\u2705 TP2: {signal.take_profit_2:.5f} (1:{tp2_rr:.0f}) \u2014 kalan\u0131 kapat\n"
            f"\U0001f4e6 Lot: {signal.lot_size} | Risk: ${risk_amount:.0f}\n"
            f"\U0001f916 AI Confidence: {signal.confidence:.0f}%"
        )
        await self.send(msg)

    async def send_tp_hit(self, position, tp_level: int):
        if tp_level == 1:
            msg = (
                f"\U0001f4b0 <b>TP1 HIT \u2014 Yar\u0131 Kapat\u0131ld\u0131</b>\n\n"
                f"{position.symbol} {position.direction.upper()} +{position.r_result:.1f}R\n"
                f"SL \u2192 {position.entry_price:.5f} (break-even)\n"
                f"Kalan: TP2 hedefleniyor..."
            )
        else:
            msg = (
                f"\U0001f4b0 <b>TP2 HIT \u2014 Tam Kapat\u0131ld\u0131</b>\n\n"
                f"{position.symbol} {position.direction.upper()} +{position.r_result:.1f}R\n"
                f"Tebrikler! Trade tamamland\u0131."
            )
        await self.send(msg)

    async def send_sl_hit(self, position):
        msg = (
            f"\U0001f6d1 <b>SL HIT</b>\n\n"
            f"{position.symbol} {position.direction.upper()} {position.r_result:+.1f}R\n"
            f"Entry: {position.entry_price:.5f} | SL: {position.current_sl:.5f}"
        )
        await self.send(msg)

    # ─── HOURLY SUMMARY ───

    async def send_hourly_summary(self, bot_engine):
        """Send hourly performance summary."""
        now = time.time()
        if now - self._last_hourly < 3500:  # ~1 hour
            return
        self._last_hourly = now

        stats = bot_engine.stats
        positions = bot_engine.trade_manager.positions if bot_engine.trade_manager else []
        open_pos = [p for p in positions if p.status in ("open", "partial")]
        pending = [p for p in positions if p.status == "pending"]

        # Account
        acc_str = ""
        try:
            acc = await bot_engine.broker.get_account()
            acc_str = f"\U0001f4b0 Kasa: {acc.balance:.2f} {acc.currency}\n\U0001f4c8 Equity: {acc.equity:.2f}\n"
        except:
            pass

        # Open positions
        pos_str = ""
        if open_pos:
            pos_str = "\n<b>Acik Pozisyonlar:</b>\n"
            for p in open_pos:
                emoji = "\U0001f7e2" if p.r_result >= 0 else "\U0001f534"
                pos_str += f"  {emoji} {p.symbol} {p.direction.upper()} {p.r_result:+.1f}R\n"
        if pending:
            pos_str += f"\n\u23f3 Bekleyen: {len(pending)} emir\n"

        # Active zones
        zone_count = 0
        for zlist in bot_engine.zones.values():
            zone_count += len([z for z in zlist if z.status in ('active', 'departed')])

        msg = (
            f"\U0001f4ca <b>SAATLIK OZET</b>\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"{acc_str}"
            f"\U0001f3af Total R: {stats.get('total_r', 0):+.1f}\n"
            f"\U0001f4c8 Win Rate: {stats.get('win_rate', 0):.0f}%\n"
            f"\U0001f4cb Trades: {stats.get('trades', 0)} "
            f"(W:{stats.get('wins', 0)} L:{stats.get('losses', 0)})\n"
            f"\U0001f50d Aktif Zone: {zone_count}\n"
            f"{pos_str}"
        )
        await self.send(msg)

    # ─── COMMAND HANDLER ───

    def set_bot_engine(self, engine):
        self._bot_engine = engine

    async def poll_commands(self):
        """Check for incoming Telegram commands."""
        if not self.enabled or not self._bot_engine:
            return
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"{self.base_url}/getUpdates",
                    params={"offset": self._last_update_id + 1, "timeout": 0}
                )
                data = resp.json()
                for update in data.get("result", []):
                    self._last_update_id = update["update_id"]
                    msg = update.get("message", {})
                    text = msg.get("text", "").strip().lower()
                    chat_id = str(msg.get("chat", {}).get("id", ""))

                    if chat_id != self.chat_id:
                        continue

                    if text in ("/durum", "/status"):
                        await self._cmd_status()
                    elif text in ("/pozisyon", "/pos", "/positions"):
                        await self._cmd_positions()
                    elif text in ("/zone", "/zones"):
                        await self._cmd_zones()
                    elif text in ("/pnl", "/kar"):
                        await self._cmd_pnl()
                    elif text in ("/kasa", "/account", "/hesap"):
                        await self._cmd_account()
                    elif text in ("/help", "/yardim"):
                        await self._cmd_help()

        except Exception as e:
            pass  # silent fail for polling

    async def _cmd_help(self):
        await self.send(
            "\U0001f916 <b>SniperX Bot Komutlari</b>\n\n"
            "/durum - Genel durum\n"
            "/pozisyon - Acik pozisyonlar\n"
            "/zone - Aktif zone'lar\n"
            "/pnl - Kar/zarar detayi\n"
            "/kasa - Hesap bakiyesi\n"
            "/yardim - Bu mesaj"
        )

    async def _cmd_status(self):
        e = self._bot_engine
        stats = e.stats
        positions = e.trade_manager.positions if e.trade_manager else []
        open_p = [p for p in positions if p.status in ("open", "partial")]
        pending_p = [p for p in positions if p.status == "pending"]
        zone_count = sum(len([z for z in zl if z.status in ('active', 'departed')]) for zl in e.zones.values())

        acc_str = ""
        try:
            acc = await e.broker.get_account()
            acc_str = f"\U0001f4b0 {acc.balance:.2f} {acc.currency} | Equity: {acc.equity:.2f}\n"
        except:
            pass

        await self.send(
            f"\U0001f4ca <b>SNIPERX DURUM</b>\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"{acc_str}"
            f"\U0001f3af R: {stats.get('total_r', 0):+.1f} | WR: {stats.get('win_rate', 0):.0f}%\n"
            f"\U0001f4cb Trades: {stats.get('trades', 0)}\n"
            f"\U0001f7e2 Acik: {len(open_p)} | \u23f3 Bekleyen: {len(pending_p)}\n"
            f"\U0001f50d Zone: {zone_count} aktif"
        )

    async def _cmd_positions(self):
        e = self._bot_engine
        positions = e.trade_manager.positions if e.trade_manager else []
        open_p = [p for p in positions if p.status in ("open", "partial")]

        if not open_p:
            await self.send("\U0001f4bc Acik pozisyon yok")
            return

        msg = "\U0001f4bc <b>ACIK POZISYONLAR</b>\n\n"
        for p in open_p:
            emoji = "\U0001f7e2" if p.r_result >= 0 else "\U0001f534"
            msg += (
                f"{emoji} <b>{p.symbol}</b> {p.direction.upper()}\n"
                f"   Entry: {p.entry_price:.5f} | Lot: {p.lot_size}\n"
                f"   SL: {p.stop_loss:.5f} | TP: {p.take_profit_1:.5f}\n"
                f"   R: {p.r_result:+.1f} | {'TP1 HIT' if p.tp1_hit else 'Bekliyor'}\n\n"
            )
        await self.send(msg)

    async def _cmd_zones(self):
        e = self._bot_engine
        active = []
        for zl in e.zones.values():
            for z in zl:
                if z.status in ('active', 'departed'):
                    active.append(z)

        if not active:
            await self.send("\U0001f50d Aktif zone yok")
            return

        msg = f"\U0001f50d <b>AKTIF ZONE'LAR ({len(active)})</b>\n\n"
        for z in active[:10]:
            arrow = "\u25b2" if z.zone_type == "demand" else "\u25bc"
            msg += (
                f"{arrow} <b>{z.zone_type.upper()}</b>\n"
                f"   {z.high:.5f} - {z.low:.5f}\n"
                f"   Pins: {z.pin_count} | Conf: {z.confidence:.0f}%\n\n"
            )
        await self.send(msg)

    async def _cmd_pnl(self):
        e = self._bot_engine
        stats = e.stats
        positions = e.trade_manager.positions if e.trade_manager else []
        closed = [p for p in positions if p.status == "closed"]

        msg = (
            f"\U0001f4b5 <b>KAR/ZARAR</b>\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"Total R: {stats.get('total_r', 0):+.1f}\n"
            f"Win Rate: {stats.get('win_rate', 0):.0f}%\n"
            f"Trades: {stats.get('trades', 0)} (W:{stats.get('wins', 0)} L:{stats.get('losses', 0)})\n"
            f"PF: {stats.get('profit_factor', 0)}\n"
        )

        if closed:
            last5 = closed[-5:]
            msg += "\n<b>Son 5 Trade:</b>\n"
            for p in reversed(last5):
                emoji = "\u2705" if p.r_result > 0 else "\u274c"
                msg += f"  {emoji} {p.symbol} {p.direction.upper()} {p.r_result:+.1f}R\n"

        await self.send(msg)

    async def _cmd_account(self):
        e = self._bot_engine
        try:
            acc = await e.broker.get_account()
            await self.send(
                f"\U0001f3e6 <b>HESAP</b>\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"Login: {acc.login}\n"
                f"Balance: {acc.balance:.2f} {acc.currency}\n"
                f"Equity: {acc.equity:.2f}\n"
                f"Margin: {acc.margin:.2f}\n"
                f"Free: {acc.margin_free:.2f}"
            )
        except Exception as ex:
            await self.send(f"\u274c Hesap bilgisi alinamadi: {ex}")
