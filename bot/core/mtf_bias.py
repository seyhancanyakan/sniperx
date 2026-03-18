"""PinShot Bot — Multi-Timeframe Bias

Determines directional bias from higher timeframe EMA alignment and slope.
"""

from dataclasses import dataclass
from typing import Optional
import logging

from .detector import Candle
from .regime import calculate_ema, _ema_slope

logger = logging.getLogger("pinshot.mtf")


@dataclass
class MTFBias:
    higher_tf: str
    bias: str            # bullish, bearish, neutral
    ema_aligned: bool
    slope_direction: str  # up, down, flat
    confidence: float     # 0-1


# Default bias map: trade TF -> higher TF for bias
BIAS_MAP = {
    "M5": "M30",
    "M15": "H1",
    "M30": "H4",
    "H1": "H4",
    "H4": None,  # standalone, no higher TF
}


async def get_higher_tf_bias(broker, symbol: str, higher_tf: str,
                             config: dict) -> Optional[MTFBias]:
    """Get directional bias from higher timeframe.

    Args:
        broker: Broker instance with get_candles() method
        symbol: Trading symbol
        higher_tf: Higher timeframe string (e.g. "H1", "H4")
        config: multi_timeframe config section

    Returns:
        MTFBias or None if data unavailable
    """
    try:
        candles = await broker.get_candles(symbol, higher_tf, 100)
        if not candles or len(candles) < 50:
            logger.debug(f"MTF: insufficient data for {symbol}/{higher_tf} ({len(candles) if candles else 0} bars)")
            return None
    except Exception as e:
        logger.warning(f"MTF: failed to get candles for {symbol}/{higher_tf}: {e}")
        return None

    ema_fast = calculate_ema(candles, 20)
    ema_slow = calculate_ema(candles, 50)
    slope = _ema_slope(candles, 20, 10)

    # Determine alignment
    ema_aligned = (ema_fast > ema_slow and slope > 0) or \
                  (ema_fast < ema_slow and slope < 0)

    # Determine slope direction
    if slope > 0.001:
        slope_dir = "up"
    elif slope < -0.001:
        slope_dir = "down"
    else:
        slope_dir = "flat"

    # Classify bias
    if ema_fast > ema_slow and slope > 0:
        bias = "bullish"
        confidence = min(1.0, 0.5 + abs(slope) * 10)
    elif ema_fast < ema_slow and slope < 0:
        bias = "bearish"
        confidence = min(1.0, 0.5 + abs(slope) * 10)
    else:
        bias = "neutral"
        confidence = 0.3

    return MTFBias(
        higher_tf=higher_tf,
        bias=bias,
        ema_aligned=ema_aligned,
        slope_direction=slope_dir,
        confidence=round(confidence, 2),
    )
