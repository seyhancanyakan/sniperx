"""PinShot Bot — AI Engine for Zone Validation

Supports Anthropic Claude API and Z.ai (OpenAI-compatible) providers.
"""

import json
import logging
import os

import httpx

logger = logging.getLogger("pinshot.ai")

SYSTEM_PROMPT = """You are PinShot AI — a market maker footprint detector.
You analyze accumulation zones for trading opportunities.

RULES:
1. Min 2 pins required in zone (wick > 2x body)
2. Breakout must be 2+ same-color candles with body > 1.5x ATR
3. Gap/FVG presence = strong evidence (+15 confidence)
4. Left side must be clean (≤ 3 swings before zone)
5. Triangular return must score ≥ 60/100
6. White space/freshness must score ≥ 50/100
7. If price approached zone but bounced away without touching = invalid

Always respond with valid JSON only."""


class AIEngine:
    def __init__(self, settings: dict):
        self.enabled = settings.get("enabled", False)
        self.provider = settings.get("provider", "anthropic")
        self.model = settings.get("model", "claude-sonnet-4-20250514")
        self.min_confidence = settings.get("min_confidence", 65)
        self.role = settings.get("role", "meta_filter")
        self.timeout_sec = settings.get("timeout_sec", 12)

        # API keys from environment
        self.anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.zai_key = os.environ.get("ZAI_API_KEY", "")

        if self.provider == "zai":
            self.api_key = self.zai_key
        else:
            self.api_key = self.anthropic_key

    def _build_prompt(self, zone, candles: list, atr: float,
                      regime=None, mtf_bias=None) -> str:
        recent = candles[-20:] if len(candles) > 20 else candles
        ohlc_str = "\n".join([
            f"  [{c.time:.0f}] O:{c.open:.5f} H:{c.high:.5f} L:{c.low:.5f} C:{c.close:.5f}"
            for c in recent
        ])

        direction = "buy" if zone.zone_type == "demand" else "sell"

        # Entry/SL/TP estimates for context
        core_high = getattr(zone, 'core_high', zone.high)
        core_low = getattr(zone, 'core_low', zone.low)
        entry = (core_high + core_low) / 2
        risk = abs(core_high - core_low) / 2 + 0.15 * atr
        if direction == "buy":
            sl = core_low - 0.15 * atr
            tp = entry + risk * 2.5
        else:
            sl = core_high + 0.15 * atr
            tp = entry - risk * 2.5
        rr = 2.5

        # Regime info
        regime_str = "N/A"
        if regime:
            regime_str = (f"{regime.regime} (ema_fast={regime.ema_fast:.5f}, "
                         f"ema_slow={regime.ema_slow:.5f}, slope={regime.slope:.4f}, "
                         f"atr_ratio={regime.atr_ratio:.2f}, efficiency={regime.efficiency:.2f})")

        # MTF bias info
        bias_str = "N/A"
        if mtf_bias:
            bias_str = (f"{mtf_bias.bias} on {mtf_bias.higher_tf} "
                       f"(aligned={mtf_bias.ema_aligned}, slope={mtf_bias.slope_direction}, "
                       f"conf={mtf_bias.confidence:.2f})")

        return f"""ANALYZE this zone as meta-filter:
- Symbol/Direction: {direction.upper()}
- Zone type: {zone.zone_type}, is_flipped: {getattr(zone, 'is_flipped', False)}
- Zone: {zone.high:.5f}-{zone.low:.5f}, {zone.end_idx - zone.start_idx} bars, {zone.pin_count} pins
- Confidence: {zone.confidence:.0f}, Triangular: {zone.triangular_score:.0f}, WhiteSpace: {zone.white_space_score:.0f}
- Breakout: {zone.breakout_direction} {zone.breakout_magnitude:.1f}x ATR
- Displacement: {zone.displacement:.1f}x
- Breakout strength: {getattr(zone, 'breakout_strength_atr', 0):.1f} ATR
- Touch penetration: {getattr(zone, 'touch_penetration_pct', 0):.2f}
- Gaps/FVG: {zone.gap_count} detected
- Entry: {entry:.5f}, SL: {sl:.5f}, TP: {tp:.5f}, R:R=1:{rr}
- Current ATR: {atr:.5f}
- Regime: {regime_str}
- Higher TF bias: {bias_str}
- Recent 20 candles:
{ohlc_str}

Respond ONLY with JSON:
{{"valid": true/false, "confidence": 0-100, "direction": "{direction}", "reasoning": "brief explanation", "risk_adjustment": 1.0, "exit_style": "fixed"}}"""

    async def _call_anthropic(self, prompt: str) -> str:
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self.model,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": prompt}],
            "system": SYSTEM_PROMPT,
        }
        async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"]

    async def _call_zai(self, prompt: str) -> str:
        """Call Z.ai API (OpenAI-compatible endpoint)."""
        url = os.environ.get("ZAI_API_URL", "https://api.z.ai/v1/chat/completions")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": os.environ.get("ZAI_MODEL", "zai/glm-5"),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 256,
            "temperature": 0.3,
        }
        async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def validate_zone(self, zone, candles: list, atr: float,
                           regime=None, mtf_bias=None) -> dict:
        """Validate zone using AI provider.

        Args:
            zone: Zone object
            candles: List of candles
            atr: Current ATR value
            regime: Optional RegimeResult from regime filter
            mtf_bias: Optional MTFBias from multi-timeframe bias
        """
        default = {
            "valid": True,
            "confidence": zone.confidence,
            "reasoning": "AI disabled",
            "direction": zone.zone_type,
            "risk_adjustment": 1.0,
            "exit_style": "fixed",
        }

        if not self.enabled or not self.api_key:
            return default

        prompt = self._build_prompt(zone, candles, atr, regime=regime, mtf_bias=mtf_bias)

        try:
            if self.provider == "zai":
                response_text = await self._call_zai(prompt)
            else:
                response_text = await self._call_anthropic(prompt)

            # Parse JSON from response (handle markdown code blocks)
            text = response_text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1])
            if text.startswith("{"):
                pass
            else:
                # Try to find JSON in the text
                start = text.find("{")
                end = text.rfind("}") + 1
                if start >= 0 and end > start:
                    text = text[start:end]

            result = json.loads(text)
            logger.info(f"AI validation: valid={result.get('valid')}, "
                        f"confidence={result.get('confidence')}")
            return result

        except json.JSONDecodeError as e:
            logger.warning(f"AI response parse error: {e}")
            return default
        except Exception as e:
            logger.error(f"AI engine error: {e}")
            return default
