"""Champion-Challenger Engine — Shadow mode evaluation.

Champion: current live model (rule_based_score + existing filters)
Challenger: new AI model (learned_score + regime-adaptive filters)

Challenger runs in shadow mode — produces signals but doesn't trade.
When challenger consistently beats champion, promotion is proposed.

Modes:
  observer: only logs data, no scoring
  shadow: scores zones but doesn't influence trades
  paper: simulates trades with challenger model
  live: challenger becomes new champion (after promotion)
"""

import json
import time
import logging
from pathlib import Path
from dataclasses import dataclass, asdict

logger = logging.getLogger("pinshot.ai.challenger")

CHALLENGER_FILE = Path(__file__).parent.parent / "config" / "ai_data" / "challenger_results.jsonl"
PROMOTION_FILE = Path(__file__).parent.parent / "config" / "ai_data" / "promotion_log.json"


@dataclass
class ShadowSignal:
    """A signal produced by challenger model but not traded."""
    timestamp: float
    symbol: str
    timeframe: str
    zone_id: str
    zone_type: str

    # Champion decision
    champion_score: float
    champion_action: str    # "trade" / "skip"

    # Challenger decision
    challenger_score: float
    challenger_action: str  # "trade" / "skip"

    # Actual outcome (filled later)
    actual_outcome: str = ""  # win/loss/no_touch
    actual_r: float = 0


class ChallengerEngine:
    def __init__(self, mode: str = "shadow"):
        """
        mode: observer | shadow | paper | live
        """
        self.mode = mode
        self.signals: list = []

    def evaluate_zone(self, zone_feature: dict,
                      champion_score: float,
                      champion_would_trade: bool) -> dict:
        """Run challenger evaluation on a zone.

        Returns challenger's recommendation without affecting live trading.
        """
        from .zone_scorer import learned_score, rule_based_score

        # Challenger score
        ch_score = learned_score(zone_feature)

        # Challenger trade decision (threshold: 55)
        ch_would_trade = ch_score >= 55

        signal = ShadowSignal(
            timestamp=time.time(),
            symbol=zone_feature.get("symbol", ""),
            timeframe=zone_feature.get("timeframe", ""),
            zone_id=zone_feature.get("zone_id", ""),
            zone_type=zone_feature.get("zone_type", ""),
            champion_score=champion_score,
            champion_action="trade" if champion_would_trade else "skip",
            challenger_score=ch_score,
            challenger_action="trade" if ch_would_trade else "skip",
        )

        self.signals.append(signal)

        # Log shadow signal
        try:
            Path(CHALLENGER_FILE).parent.mkdir(parents=True, exist_ok=True)
            with open(CHALLENGER_FILE, "a") as f:
                f.write(json.dumps(asdict(signal)) + "\n")
        except Exception as e:
            logger.error(f"Shadow signal log error: {e}")

        return {
            "challenger_score": ch_score,
            "challenger_action": "trade" if ch_would_trade else "skip",
            "champion_score": champion_score,
            "agrees": champion_would_trade == ch_would_trade,
        }

    def update_outcome(self, zone_id: str, outcome: str, r_result: float):
        """Update a shadow signal with actual outcome."""
        for sig in self.signals:
            if sig.zone_id == zone_id and not sig.actual_outcome:
                sig.actual_outcome = outcome
                sig.actual_r = r_result
                break

    def get_comparison(self) -> dict:
        """Compare champion vs challenger performance."""
        completed = [s for s in self.signals if s.actual_outcome]

        if len(completed) < 10:
            return {
                "status": "insufficient_data",
                "completed_signals": len(completed),
                "min_required": 10,
            }

        # Champion performance
        ch_trades = [s for s in completed if s.champion_action == "trade"]
        ch_wins = [s for s in ch_trades if s.actual_r > 0]
        ch_total_r = sum(s.actual_r for s in ch_trades)

        # Challenger performance
        cl_trades = [s for s in completed if s.challenger_action == "trade"]
        cl_wins = [s for s in cl_trades if s.actual_r > 0]
        cl_total_r = sum(s.actual_r for s in cl_trades)

        # Challenger correctly skipped losers?
        ch_skipped_good = [s for s in completed
                           if s.challenger_action == "skip"
                           and s.champion_action == "trade"
                           and s.actual_r < 0]

        # Challenger missed winners?
        cl_missed_wins = [s for s in completed
                          if s.challenger_action == "skip"
                          and s.champion_action == "trade"
                          and s.actual_r > 0]

        return {
            "status": "ready",
            "completed_signals": len(completed),
            "champion": {
                "trades": len(ch_trades),
                "wins": len(ch_wins),
                "win_rate": round(len(ch_wins) / len(ch_trades) * 100, 1) if ch_trades else 0,
                "total_r": round(ch_total_r, 2),
            },
            "challenger": {
                "trades": len(cl_trades),
                "wins": len(cl_wins),
                "win_rate": round(len(cl_wins) / len(cl_trades) * 100, 1) if cl_trades else 0,
                "total_r": round(cl_total_r, 2),
            },
            "challenger_advantages": {
                "correctly_skipped_losers": len(ch_skipped_good),
                "missed_winners": len(cl_missed_wins),
            },
            "promotion_eligible": (
                len(cl_trades) >= 10 and
                cl_total_r > ch_total_r and
                len(cl_wins) / max(len(cl_trades), 1) > 0.45
            ),
        }

    def check_promotion(self) -> dict:
        """Check if challenger should be promoted to champion."""
        comparison = self.get_comparison()

        if comparison.get("status") != "ready":
            return {"promote": False, "reason": "insufficient_data"}

        if not comparison.get("promotion_eligible"):
            ch_r = comparison["champion"]["total_r"]
            cl_r = comparison["challenger"]["total_r"]
            return {
                "promote": False,
                "reason": f"challenger R={cl_r} not better than champion R={ch_r}",
                "comparison": comparison,
            }

        # Promotion criteria met
        result = {
            "promote": True,
            "reason": "challenger outperforms champion",
            "comparison": comparison,
            "timestamp": time.time(),
        }

        # Log promotion proposal
        try:
            Path(PROMOTION_FILE).parent.mkdir(parents=True, exist_ok=True)
            existing = []
            try:
                with open(PROMOTION_FILE) as f:
                    existing = json.load(f)
            except Exception:
                pass
            existing.append(result)
            with open(PROMOTION_FILE, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            logger.error(f"Promotion log error: {e}")

        return result
