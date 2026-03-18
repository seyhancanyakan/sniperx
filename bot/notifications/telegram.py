"""PinShot Bot — Telegram Notifications"""

import httpx
import logging

logger = logging.getLogger("pinshot.telegram")


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.enabled = bool(bot_token and chat_id)

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
