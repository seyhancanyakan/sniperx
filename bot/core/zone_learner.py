"""Zone Learner — AI-powered pattern learning from manual zones.

Two-phase learning:
  Phase A (Annotation): When user draws a zone, AI analyzes WHY the human spotted it.
  Phase B (Outcome): After trade closes, AI analyzes whether the zone worked and why.

Learned patterns are stored as 'proposed' and only applied after approval or sufficient evidence.
"""

import json
import logging
import os
import time
from pathlib import Path

import httpx

logger = logging.getLogger("pinshot.learner")

LEARNED_PATTERNS_FILE = str(Path(__file__).parent.parent / "config" / "learned_patterns.json")
MANUAL_ZONES_FILE = str(Path(__file__).parent.parent / "config" / "manual_zones.json")

# ── CANONICAL JSON SCHEMA (all prompts use this) ──
# description_tr is DISPLAY-ONLY — no logic depends on it
# All scoring, matching, detection logic uses English fields only

# ── Phase A: Zone annotation prompt (at draw time) ──
ZONE_ANNOTATION_PROMPT = """You are analyzing a trading zone that a human trader manually identified on a chart, but the automated bot missed.

ZONE INFO:
- Symbol: {symbol}
- Timeframe: {timeframe}
- Type: {zone_type} ({zone_desc})
- Price range: {high:.5f} — {low:.5f}
- Zone width: {width:.5f}
- User note: {note}

SURROUNDING 50 CANDLES (before and around the zone):
{candles_str}

QUESTION: Why did a human trader identify this as a {zone_type} zone? What pattern or price action feature makes this area significant?

Respond ONLY with valid JSON. All fields English EXCEPT description_tr (Turkish display text):
{{"pattern": "short_pattern_name", "description": "1-2 sentence explanation", "description_tr": "Türkçe 1-2 cümle açıklama - bu zone neden önemli", "confidence_adjustments": [{{"feature": "feature_name", "bonus": 5}}, {{"feature": "feature_name2", "bonus": 3}}], "trigger_features": ["feature1", "feature2"], "zone_scope": "base|flip|both", "applicable_timeframes": ["M5","M15","M30","H1","H4"], "evidence_strength": "low|medium|high", "risk_notes": "any caveats or risks"}}"""

# ── Phase B: Outcome prompt (after trade closes) ──
OUTCOME_PROMPT = """A human trader manually drew a {zone_type} zone. Here is what happened:

ZONE INFO:
- Symbol: {symbol}, Timeframe: {timeframe}
- Type: {zone_type}, Price: {high:.5f} — {low:.5f}
- Trade result: {outcome} ({r_result}R)
- User note: {note}

SURROUNDING CANDLES AT ZONE TIME:
{candles_str}

QUESTION: Based on the outcome ({outcome}, {r_result}R), what can the bot learn?
- If the zone worked (TP hit): What feature should the bot look for?
- If the zone failed (SL hit): What was misleading?

Respond ONLY with valid JSON. All fields English EXCEPT description_tr (Turkish display text):
{{"lesson": "brief lesson", "description_tr": "Türkçe kısa ders - ne oldu, neden, bot ne öğrenmeli", "pattern": "short_pattern_name", "outcome_type": "success|failure", "suggested_bonus": -10 to +15, "avoid_features": ["feature1"], "seek_features": ["feature1"], "zone_scope": "base|flip|both", "evidence_strength": "low|medium|high", "risk_notes": "any caveats"}}"""

# ── Chart Annotation prompt (teacher mode) ──
ANNOTATION_PROMPT = """A human trader is teaching the bot by annotating a chart.

CONTEXT:
- Symbol: {symbol}
- Timeframe: {timeframe}
- Annotated at: price={price:.5f}, time={time}
- Trader's note: "{text}"

SURROUNDING 50 CANDLES around the annotated point:
{candles_str}

Analyze this teaching moment. What pattern, what features, what should the bot learn?

Respond ONLY with valid JSON. All fields English EXCEPT description_tr (Turkish display text):
{{"lesson": "what the trader is teaching", "description_tr": "Türkçe açıklama - trader ne öğretiyor", "pattern": "short_pattern_name", "features_to_detect": ["feature1", "feature2"], "confidence_adjustments": [{{"feature": "feature_name", "bonus": 5}}], "zone_detection_hint": "how to improve detection", "zone_scope": "base|flip|both", "applicable_timeframes": ["M5","M15","M30","H1","H4"], "evidence_strength": "low|medium|high", "risk_notes": "any caveats"}}"""


# ── History trade analysis prompt (2-part to avoid hindsight bias) ──
# Part 1: Entry-time analysis — what was visible BEFORE outcome
# Part 2: Outcome analysis — what the result teaches
HISTORY_TRADE_PROMPT = """Analyze this completed trade in TWO parts:

TRADE INFO:
- Symbol: {symbol}
- Direction: {direction} ({dir_desc})
- Entry: {entry_price}, Exit: {exit_price}
- Result: {outcome} (profit: ${profit})
- Volume: {volume} lot

SURROUNDING 50 CANDLES around trade entry time:
{candles_str}

PART 1 — ENTRY-TIME ANALYSIS (what was visible at entry, before knowing outcome):
- What price action pattern existed at entry point?
- Was there displacement, base structure, imbalance?
- Was the zone type (base vs flip) appropriate?
- Were there warning signs visible at entry time?

PART 2 — OUTCOME ANALYSIS (what the result teaches):
- Why did this trade {outcome}?
- What should the bot seek or avoid in similar setups?

IMPORTANT: Distinguish between features visible AT ENTRY vs features only visible in hindsight.

Respond ONLY with valid JSON. All fields English EXCEPT description_tr (Turkish display text):
{{"lesson": "brief lesson (entry-time features only)", "description_tr": "Türkçe kısa açıklama - giriş anında ne vardı, sonuç ne öğretiyor", "pattern": "short_pattern_name", "outcome_type": "success|failure", "suggested_bonus": -10 to +15, "entry_features": ["features visible at entry time"], "hindsight_features": ["features only visible after outcome"], "avoid_features": ["warning signs visible at entry"], "seek_features": ["positive features visible at entry"], "zone_scope": "base|flip|both", "applicable_timeframes": ["M5","M15","M30","H1","H4"], "risk_notes": "any caveats"}}"""


def _sum_confidence_adjustments(result: dict) -> int:
    """Sum confidence bonus from confidence_adjustments list or single confidence_bonus."""
    # New schema: confidence_adjustments is a list
    ca_list = result.get("confidence_adjustments", [])
    if isinstance(ca_list, list) and ca_list:
        total = sum(int(ca.get("bonus", 0)) for ca in ca_list if isinstance(ca, dict))
        return min(max(total, 0), 15)
    # Fallback: old single field
    return min(max(int(result.get("confidence_bonus", 5)), 0), 15)


class ZoneLearner:
    def __init__(self, bot_engine):
        self.bot = bot_engine
        self.zai_key = os.environ.get("ZAI_API_KEY", "")
        self.zai_url = os.environ.get("ZAI_API_URL", "https://api.z.ai/v1/chat/completions")
        self.zai_model = os.environ.get("ZAI_MODEL", "zai/glm-5")

    def _load_patterns(self) -> list:
        try:
            with open(LEARNED_PATTERNS_FILE) as f:
                return json.load(f)
        except Exception:
            return []

    def _save_patterns(self, patterns: list):
        with open(LEARNED_PATTERNS_FILE, "w") as f:
            json.dump(patterns, f, indent=2)

    async def _call_ai(self, prompt: str) -> str:
        """Call Z.ai API and return response text."""
        headers = {
            "Authorization": f"Bearer {self.zai_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.zai_model,
            "messages": [
                {"role": "system", "content": "You are a trading pattern analyst. Respond only with valid JSON."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 4000,
            "temperature": 0.3,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(self.zai_url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]
            # glm-5 is a reasoning model: real answer may be in content or reasoning_content
            text = (msg.get("content") or "").strip()
            if not text or "{" not in text:
                # Try extracting JSON from reasoning_content
                reasoning = (msg.get("reasoning_content") or "").strip()
                if "{" in reasoning:
                    text = reasoning
            return text

    def _parse_json(self, text: str) -> dict:
        """Extract JSON from AI response — tolerant of markdown fences and minor issues."""
        # Strip code fences
        if "```" in text:
            lines = text.split("\n")
            cleaned = []
            in_fence = False
            for line in lines:
                if line.strip().startswith("```"):
                    in_fence = not in_fence
                    continue
                cleaned.append(line)
            text = "\n".join(cleaned)
        # Find outermost JSON object
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]
        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Fix common issues: trailing commas, single quotes
        import re
        text = re.sub(r',\s*}', '}', text)
        text = re.sub(r',\s*]', ']', text)
        return json.loads(text)

    async def _get_candles_str(self, symbol: str, timeframe: str, zone_start: float) -> str:
        """Fetch and format candles around the zone."""
        candles = await self.bot.broker.get_candles(symbol, timeframe, 100)
        if len(candles) < 20:
            return ""
        nearby = [c for c in candles if abs(c.time - zone_start) < 50 * 86400] if zone_start else []
        if len(nearby) < 10:
            nearby = candles[-50:]
        return "\n".join([
            f"  [{c.time:.0f}] O:{c.open:.5f} H:{c.high:.5f} L:{c.low:.5f} C:{c.close:.5f} V:{c.volume:.0f}"
            for c in nearby[-50:]
        ])

    # ── PHASE A: Annotation Learning (at draw time) ──
    async def learn_from_zone(self, zone: dict):
        """Phase A: Analyze WHY a human drew this zone."""
        if not self.zai_key or not self.bot or not self.bot.broker:
            logger.info("Zone annotation learning skipped: no API key or broker")
            return

        symbol = zone["symbol"]
        timeframe = zone["timeframe"]

        try:
            candles_str = await self._get_candles_str(symbol, timeframe, zone.get("start_time", 0))
            if not candles_str:
                return

            zone_desc = "buy zone - price expected to bounce up" if zone["zone_type"] == "demand" else "sell zone - price expected to drop"
            prompt = ZONE_ANNOTATION_PROMPT.format(
                symbol=symbol, timeframe=timeframe,
                zone_type=zone["zone_type"], zone_desc=zone_desc,
                high=zone["high"], low=zone["low"],
                width=zone["high"] - zone["low"],
                note=zone.get("note", ""),
                candles_str=candles_str,
            )

            text = await self._call_ai(prompt)
            result = self._parse_json(text)

            pattern = {
                "id": f"pat_{zone.get('id', 'unknown')}",
                "pattern": result.get("pattern", "unknown"),
                "description": result.get("description", ""),
                "description_tr": result.get("description_tr", ""),
                "confidence_bonus": _sum_confidence_adjustments(result),
                "trigger_features": result.get("trigger_features", []),
                "applicable_timeframes": result.get("applicable_timeframes", [timeframe]),
                "zone_scope": result.get("zone_scope", "both"),
                "evidence_strength": "low",  # Computed, not AI-assigned
                "risk_notes": result.get("risk_notes", ""),
                "confidence_adjustments": result.get("confidence_adjustments", []),
                "source_symbol": symbol,
                "source_timeframe": timeframe,
                "source_zone_type": zone["zone_type"],
                "source_zone_id": zone.get("id", ""),
                "learned_at": time.time(),
                "phase": "annotation",
                "occurrence_count": 1,
                "sample_count": 1,
                "win_count": 0,
                "loss_count": 0,
                "avg_r": 0.0,
                "source_breakdown": {"history": 0, "annotation": 1, "outcome": 0},
                "contradictory_examples": 0,
                "last_applied_at": 0,
                "decay_score": 1.0,
                "live_stats": {"zones_with_bonus": 0, "trades_with_bonus": 0,
                               "bonus_win_count": 0, "bonus_loss_count": 0, "bonus_avg_r": 0.0},
                "status": "proposed",  # proposed → approved → active | rejected
            }

            patterns = self._load_patterns()
            existing = next((p for p in patterns if p["pattern"] == pattern["pattern"]), None)
            if existing:
                existing["occurrence_count"] = existing.get("occurrence_count", 1) + 1
                existing["sample_count"] = existing.get("sample_count", 1) + 1
                existing["learned_at"] = time.time()
                # Auto-promote to approved after 3 occurrences
                if existing.get("status") == "proposed" and existing["occurrence_count"] >= 10:
                    existing["status"] = "approved"
                    logger.info(f"PATTERN AUTO-APPROVED: {existing['pattern']} (count={existing['occurrence_count']})")
                logger.info(f"ANNOTATION (updated): {existing['pattern']} "
                            f"count={existing['occurrence_count']} status={existing['status']}")
            else:
                patterns.append(pattern)
                logger.info(f"ANNOTATION (new): {pattern['pattern']} — {pattern['description']} "
                            f"bonus=+{pattern['confidence_bonus']} status=proposed")

            self._save_patterns(patterns)

            # Update manual zone learning_status
            self._update_zone_learning_status(zone.get("id", ""), "reviewed")

        except json.JSONDecodeError as e:
            logger.warning(f"Annotation learning: AI parse error: {e}")
        except Exception as e:
            logger.error(f"Annotation learning error: {e}")

    # ── PHASE B: Outcome Learning (after trade closes) ──
    async def learn_from_outcome(self, zone: dict, outcome: str, r_result: float):
        """Phase B: Analyze whether the zone WORKED and why."""
        if not self.zai_key or not self.bot or not self.bot.broker:
            return

        symbol = zone["symbol"]
        timeframe = zone["timeframe"]

        try:
            candles_str = await self._get_candles_str(symbol, timeframe, zone.get("start_time", 0))
            if not candles_str:
                return

            prompt = OUTCOME_PROMPT.format(
                symbol=symbol, timeframe=timeframe,
                zone_type=zone["zone_type"],
                high=zone["high"], low=zone["low"],
                outcome=outcome, r_result=f"{r_result:.2f}",
                note=zone.get("note", ""),
                candles_str=candles_str,
            )

            text = await self._call_ai(prompt)
            result = self._parse_json(text)

            # Update the pattern learned from annotation phase
            patterns = self._load_patterns()
            zone_pat = next((p for p in patterns
                             if p.get("source_zone_id") == zone.get("id", "")), None)

            if zone_pat:
                # Update with outcome data
                zone_pat["phase"] = "outcome"
                if outcome == "tp":
                    zone_pat["win_count"] = zone_pat.get("win_count", 0) + 1
                else:
                    zone_pat["loss_count"] = zone_pat.get("loss_count", 0) + 1
                total = zone_pat.get("win_count", 0) + zone_pat.get("loss_count", 0)
                zone_pat["win_rate"] = round(zone_pat.get("win_count", 0) / total * 100, 1) if total > 0 else 0
                # Update avg_r
                old_avg = zone_pat.get("avg_r", 0)
                zone_pat["avg_r"] = round((old_avg * (total - 1) + r_result) / total, 2) if total > 0 else r_result
                zone_pat["outcome_lesson"] = result.get("lesson", "")
                zone_pat["avoid_features"] = result.get("avoid_features", [])
                zone_pat["seek_features"] = result.get("seek_features", [])

                # Adjust confidence bonus based on outcome
                suggested = int(result.get("suggested_bonus", 0))
                zone_pat["confidence_bonus"] = min(max(
                    zone_pat.get("confidence_bonus", 0) + suggested, -10), 20)

                # Reject pattern if win_rate < 30% after 5+ samples
                if total >= 5 and zone_pat.get("win_rate", 0) < 30:
                    zone_pat["status"] = "rejected"
                    logger.info(f"PATTERN REJECTED: {zone_pat['pattern']} "
                                f"win_rate={zone_pat['win_rate']}% after {total} trades")

                logger.info(f"OUTCOME LEARNED: {zone_pat['pattern']} "
                            f"result={outcome} {r_result:.2f}R "
                            f"win_rate={zone_pat.get('win_rate', 0)}%")
            else:
                # No existing annotation pattern, create new outcome-only entry
                new_pat = {
                    "id": f"out_{zone.get('id', 'unknown')}",
                    "pattern": result.get("pattern", "unknown_outcome"),
                    "description": result.get("lesson", ""),
                    "confidence_bonus": min(max(int(result.get("suggested_bonus", 0)), -10), 15),
                    "trigger_features": result.get("seek_features", []),
                    "avoid_features": result.get("avoid_features", []),
                    "applicable_timeframes": [timeframe],
                    "source_zone_id": zone.get("id", ""),
                    "source_zone_type": zone["zone_type"],
                    "learned_at": time.time(),
                    "phase": "outcome",
                    "occurrence_count": 1,
                    "sample_count": 1,
                    "win_count": 1 if outcome == "tp" else 0,
                    "loss_count": 0 if outcome == "tp" else 1,
                    "avg_r": r_result,
                    "status": "proposed",
                }
                patterns.append(new_pat)
                logger.info(f"OUTCOME (new): {new_pat['pattern']} — {new_pat['description']}")

            self._save_patterns(patterns)

            # Update manual zone with outcome
            self._update_zone_outcome(zone.get("id", ""), outcome, r_result)

        except Exception as e:
            logger.error(f"Outcome learning error: {e}")

    def _update_zone_learning_status(self, zone_id: str, status: str):
        """Update learning_status on a manual zone."""
        try:
            with open(MANUAL_ZONES_FILE) as f:
                zones = json.load(f)
            for z in zones:
                if z["id"] == zone_id:
                    z["learning_status"] = status
                    break
            with open(MANUAL_ZONES_FILE, "w") as f:
                json.dump(zones, f, indent=2)
        except Exception:
            pass

    def _update_zone_outcome(self, zone_id: str, outcome: str, r_result: float):
        """Store outcome on a manual zone."""
        try:
            with open(MANUAL_ZONES_FILE) as f:
                zones = json.load(f)
            for z in zones:
                if z["id"] == zone_id:
                    z["outcome"] = outcome
                    z["r_result"] = round(r_result, 2)
                    z["learning_status"] = "learned"
                    break
            with open(MANUAL_ZONES_FILE, "w") as f:
                json.dump(zones, f, indent=2)
        except Exception:
            pass

    # ── CHART ANNOTATION LEARNING (Teacher Mode) ──
    async def learn_from_annotation(self, ann: dict) -> dict | None:
        """Learn from a chart annotation — the trader is teaching the bot."""
        if not self.zai_key or not self.bot or not self.bot.broker:
            logger.info("Annotation learning skipped: no API key or broker")
            return None

        symbol = ann["symbol"]
        timeframe = ann["timeframe"]

        try:
            candles_str = await self._get_candles_str(symbol, timeframe, ann.get("time", 0))
            if not candles_str:
                return None

            prompt = ANNOTATION_PROMPT.format(
                symbol=symbol, timeframe=timeframe,
                price=ann["price"], time=ann["time"],
                text=ann["text"],
                candles_str=candles_str,
            )

            text = await self._call_ai(prompt)
            result = self._parse_json(text)

            # Store as learned pattern (proposed)
            pattern = {
                "id": f"ann_{ann.get('id', 'unknown')}",
                "pattern": result.get("pattern", "annotation"),
                "description": result.get("lesson", ""),
                "confidence_bonus": 0,  # annotations don't auto-add bonus
                "trigger_features": result.get("features_to_detect", []),
                "zone_detection_hint": result.get("zone_detection_hint", ""),
                "applicable_timeframes": result.get("applicable_timeframes", [timeframe]),
                "source_symbol": symbol,
                "source_timeframe": timeframe,
                "source_type": "annotation",
                "trader_note": ann["text"],
                "learned_at": time.time(),
                "phase": "annotation",
                "occurrence_count": 1,
                "sample_count": 1,
                "win_count": 0,
                "loss_count": 0,
                "avg_r": 0.0,
                "status": "proposed",
            }

            # Check confidence adjustment
            ca = result.get("confidence_adjustment", {})
            if ca and isinstance(ca, dict):
                pattern["confidence_bonus"] = min(max(int(ca.get("bonus", 0)), 0), 10)
                pattern["bonus_feature"] = ca.get("feature", "")

            patterns = self._load_patterns()
            existing = next((p for p in patterns if p["pattern"] == pattern["pattern"]), None)
            if existing:
                existing["occurrence_count"] = existing.get("occurrence_count", 1) + 1
                existing["sample_count"] = existing.get("sample_count", 1) + 1
                existing["learned_at"] = time.time()
                if existing.get("status") == "proposed" and existing["occurrence_count"] >= 10:
                    existing["status"] = "approved"
                    logger.info(f"ANNOTATION PATTERN AUTO-APPROVED: {existing['pattern']}")
                logger.info(f"ANNOTATION LEARNED (updated): {existing['pattern']} count={existing['occurrence_count']}")
            else:
                patterns.append(pattern)
                logger.info(f"ANNOTATION LEARNED (new): {pattern['pattern']} — {pattern['description'][:60]}")

            self._save_patterns(patterns)

            return {
                "lesson": result.get("lesson", ""),
                "pattern": result.get("pattern", ""),
                "features": result.get("features_to_detect", []),
                "hint": result.get("zone_detection_hint", ""),
            }

        except json.JSONDecodeError as e:
            logger.warning(f"Annotation learning parse error: {e}")
            return None
        except Exception as e:
            logger.error(f"Annotation learning error: {e}")
            return None

    # Source weight: how much to trust each learning source
    SOURCE_WEIGHTS = {
        "history": 0.5,       # Hindsight bias risk — lower trust
        "annotation": 0.7,    # Human intuition but no outcome yet
        "outcome": 1.0,       # Human zone + real trade result — highest trust
    }

    # Lifecycle: proposed → approved → disabled | rejected
    # Approved patterns affect scoring immediately (capped ±10)
    # User can DISABLE anytime if pattern underperforms
    LIVE_STATUSES = ("approved", "approved_live", "active")

    def _compute_evidence_strength(self, p: dict) -> str:
        """Compute evidence strength from data, not AI opinion."""
        sample_count = p.get("sample_count", p.get("occurrence_count", 1))
        win_rate = p.get("win_rate", 0)
        total_trades = p.get("win_count", 0) + p.get("loss_count", 0)

        score = 0
        if sample_count >= 5:
            score += 2
        elif sample_count >= 3:
            score += 1
        if total_trades >= 3 and win_rate >= 50:
            score += 2
        elif total_trades >= 1:
            score += 1
        # Bonus for diversity (multiple symbols/timeframes would add here)

        if score >= 4:
            return "high"
        elif score >= 2:
            return "medium"
        return "low"

    def get_confidence_bonus(self, zone_type: str, timeframe: str,
                             symbol: str = "", is_flipped: bool = False) -> float:
        """Get accumulated confidence bonus from APPROVED_LIVE patterns only.

        Shadow patterns are logged but return 0 bonus.
        Separate positive cap (+10) and negative cap (-8).
        Source weight applied per pattern origin.
        Evidence computed from data, not AI opinion.
        """
        patterns = self._load_patterns()
        pos_bonus = 0.0
        neg_bonus = 0.0
        applied = []
        skipped = []

        for p in patterns:
            pid = p.get("id", "?")
            status = p.get("status", "proposed")

            # Only approved/active patterns affect scoring
            if status not in self.LIVE_STATUSES:
                continue

            # Timeframe scope
            if timeframe not in p.get("applicable_timeframes", []):
                skipped.append(f"{pid}:tf_mismatch")
                continue

            # Zone type scope (demand/supply)
            source_zt = p.get("source_zone_type", "")
            if source_zt and source_zt != zone_type:
                if p.get("occurrence_count", 1) < 3:
                    skipped.append(f"{pid}:zone_type_mismatch")
                    continue

            # Zone scope (base/flip/both)
            zone_scope = p.get("zone_scope", "both")
            if zone_scope == "base" and is_flipped:
                skipped.append(f"{pid}:scope_base_only")
                continue
            if zone_scope == "flip" and not is_flipped:
                skipped.append(f"{pid}:scope_flip_only")
                continue

            # Compute evidence from data (override AI's subjective assessment)
            evidence = self._compute_evidence_strength(p)
            evidence_mult = {"high": 1.0, "medium": 0.6, "low": 0.3}.get(evidence, 0.3)

            # Source weight
            source = p.get("source_type", p.get("phase", "history"))
            source_mult = self.SOURCE_WEIGHTS.get(source, 0.5)

            # Occurrence ramp
            occ_weight = min(p.get("occurrence_count", 1), 5) / 5

            # Decay: patterns lose influence over time (half-life ~14 days)
            learned_at = p.get("learned_at", time.time())
            age_days = (time.time() - learned_at) / 86400
            decay = max(0.3, 1.0 - (age_days / 28))  # Floor at 0.3 after ~20 days
            p["decay_score"] = round(decay, 2)

            raw_bonus = p.get("confidence_bonus", 0)
            pat_bonus = raw_bonus * occ_weight * evidence_mult * source_mult * decay

            # Accumulate separately for pos/neg caps
            if pat_bonus >= 0:
                pos_bonus += pat_bonus
            else:
                neg_bonus += pat_bonus
            applied.append(f"{pid}:{pat_bonus:+.1f}(ev={evidence},src={source})")

            # Track application stats
            p["last_applied_at"] = time.time()
            ls = p.get("live_stats", {})
            ls["zones_with_bonus"] = ls.get("zones_with_bonus", 0) + 1
            p["live_stats"] = ls

        # Separate caps: +10 positive, -8 negative
        capped_pos = min(pos_bonus, 10)
        capped_neg = max(neg_bonus, -8)
        total = capped_pos + capped_neg

        if applied:
            logger.info(f"PATTERN_BONUS_APPLIED: {symbol}/{timeframe} {zone_type} "
                        f"{'flip' if is_flipped else 'base'} "
                        f"pos={capped_pos:+.1f} neg={capped_neg:+.1f} total={total:+.1f} "
                        f"[{','.join(applied)}]")
        if skipped:
            logger.debug(f"PATTERN_BONUS_SKIPPED: {symbol}/{timeframe} "
                         f"[{','.join(skipped[:5])}]")

        # Persist updated stats (decay_score, last_applied_at, live_stats)
        if applied:
            try:
                self._save_patterns(patterns)
            except Exception:
                pass

        return total

    # ─── CONFIG CHANGE PROPOSALS ───

    def generate_config_proposals(self) -> list:
        """Analyze approved patterns and generate config change proposals.

        Only generates proposals for config-level parameters (settings.yaml),
        NOT code-level changes. Each proposal is staged for user review.
        """
        import yaml

        patterns = self._load_patterns()
        approved = [p for p in patterns if p.get("status") in ("approved", "active")
                    and p.get("sample_count", 0) >= 3]
        if not approved:
            return []

        settings_file = str(Path(__file__).parent.parent / "config" / "settings.yaml")
        try:
            with open(settings_file) as f:
                settings = yaml.safe_load(f)
        except Exception:
            return []

        proposals_file = str(Path(__file__).parent.parent / "config" / "config_change_proposals.json")
        try:
            with open(proposals_file) as f:
                existing = json.load(f)
        except Exception:
            existing = []

        # Don't re-propose already pending/applied proposals
        existing_fields = {p["field_name"] for p in existing
                          if p.get("status") in ("proposed", "applied")}

        proposals = []
        zd = settings.get("zone_detection", {})

        # Analyze: if most approved patterns are from low-displacement zones
        low_disp_patterns = [p for p in approved if "displacement" in str(p.get("trigger_features", [])).lower()
                             or "low_displacement" in p.get("pattern", "")]
        if low_disp_patterns and len(low_disp_patterns) >= 2:
            field = "zone_detection.min_displacement"
            if field not in existing_fields:
                current = zd.get("min_displacement", 2.0)
                proposed = round(max(current - 0.2, 1.2), 1)
                if proposed != current:
                    proposals.append({
                        "id": f"cfg_{int(time.time())}_{field.replace('.','_')}",
                        "source_pattern_ids": [p["id"] for p in low_disp_patterns],
                        "field_name": field,
                        "old_value": current,
                        "proposed_value": proposed,
                        "reason": f"{len(low_disp_patterns)} approved patterns involve lower displacement zones",
                        "expected_effect": "More zones detected (slightly lower displacement threshold)",
                        "risk_note": "May increase false positives. Monitor win rate after change.",
                        "status": "proposed",
                        "created_at": time.time(),
                    })

        # Analyze: engulfing/pin bar patterns → might lower spike_atr_mult
        candle_patterns = [p for p in approved if any(f in str(p.get("trigger_features", [])).lower()
                           for f in ("engulfing", "pin_bar", "hammer", "doji"))]
        if candle_patterns and len(candle_patterns) >= 2:
            field = "zone_detection.spike_atr_mult"
            if field not in existing_fields:
                current = zd.get("spike_atr_mult", 2.0)
                proposed = round(max(current - 0.2, 1.2), 1)
                if proposed != current:
                    proposals.append({
                        "id": f"cfg_{int(time.time())}_{field.replace('.','_')}",
                        "source_pattern_ids": [p["id"] for p in candle_patterns],
                        "field_name": field,
                        "old_value": current,
                        "proposed_value": proposed,
                        "reason": f"{len(candle_patterns)} approved patterns have candle-level features the bot may miss with high impulse threshold",
                        "expected_effect": "Slightly more sensitive impulse detection",
                        "risk_note": "Only change if win rate on current settings is stable",
                        "status": "proposed",
                        "created_at": time.time(),
                    })

        # Save new proposals
        if proposals:
            existing.extend(proposals)
            with open(proposals_file, "w") as f:
                json.dump(existing, f, indent=2)
            for p in proposals:
                logger.info(f"CONFIG_CHANGE_PROPOSED: {p['field_name']} "
                            f"{p['old_value']} -> {p['proposed_value']} "
                            f"reason={p['reason'][:60]}")

        return proposals

    # ─── HISTORY TRADE LEARNING ───

    async def learn_from_history_trade(self, trade: dict) -> dict | None:
        """Analyze a closed trade from MT5 history and extract patterns."""
        if not self.zai_key or not self.bot or not self.bot.broker:
            logger.info("History learning skipped: no API key or broker")
            return None

        symbol = trade.get("symbol", "")
        if not symbol:
            return None

        # Skip already-learned trades
        patterns = self._load_patterns()
        trade_id = trade.get("position_id", "")
        if any(p.get("source_trade_id") == trade_id for p in patterns):
            return None

        try:
            # Guess timeframe from trade duration
            open_time = trade.get("open_time", 0)
            close_time = trade.get("close_time", 0)
            duration = close_time - open_time if close_time and open_time else 0
            if duration < 600:
                tf = "M5"
            elif duration < 1800:
                tf = "M15"
            elif duration < 7200:
                tf = "H1"
            else:
                tf = "H4"

            candles_str = await self._get_candles_str(symbol, tf, open_time)
            if not candles_str:
                # Try M15 as fallback
                candles_str = await self._get_candles_str(symbol, "M15", open_time)
            if not candles_str:
                return None

            direction = trade.get("direction", "buy")
            profit = trade.get("net_profit", trade.get("profit", 0))
            outcome = "win" if profit > 0 else "loss"

            prompt = HISTORY_TRADE_PROMPT.format(
                symbol=symbol,
                direction=direction,
                dir_desc="long/buy" if direction == "buy" else "short/sell",
                entry_price=trade.get("entry_price", 0),
                exit_price=trade.get("exit_price", 0),
                outcome=outcome,
                profit=f"{profit:.2f}",
                volume=trade.get("volume", 0),
                candles_str=candles_str,
            )

            text = await self._call_ai(prompt)
            result = self._parse_json(text)

            zone_type = "demand" if direction == "buy" else "supply"
            pattern = {
                "id": f"hist_{trade_id}",
                "pattern": result.get("pattern", "unknown"),
                "description": result.get("lesson", ""),
                "description_tr": result.get("description_tr", ""),
                "confidence_bonus": min(max(int(result.get("suggested_bonus", 0)), -10), 10),
                "trigger_features": result.get("seek_features", []),
                "entry_features": result.get("entry_features", []),
                "hindsight_features": result.get("hindsight_features", []),
                "avoid_features": result.get("avoid_features", []),
                "applicable_timeframes": result.get("applicable_timeframes", [tf]),
                "zone_scope": result.get("zone_scope", "both"),
                "evidence_strength": "low",  # Computed, not AI-assigned
                "risk_notes": result.get("risk_notes", ""),
                "source_symbol": symbol,
                "source_timeframe": tf,
                "source_zone_type": zone_type,
                "source_trade_id": trade_id,
                "source_type": "history",
                "learned_at": time.time(),
                "phase": "outcome",
                "occurrence_count": 1,
                "sample_count": 1,
                "win_count": 1 if profit > 0 else 0,
                "loss_count": 0 if profit > 0 else 1,
                "win_rate": 100.0 if profit > 0 else 0.0,
                "avg_r": round(profit, 2),
                "status": "proposed",
                # Extended tracking fields
                "source_breakdown": {"history": 1, "annotation": 0, "outcome": 0},
                "contradictory_examples": 0,
                "last_applied_at": 0,
                "decay_score": 1.0,  # 1.0 = fresh, decays over time
                "live_stats": {  # Track performance after going live
                    "zones_with_bonus": 0,
                    "trades_with_bonus": 0,
                    "bonus_win_count": 0,
                    "bonus_loss_count": 0,
                    "bonus_avg_r": 0.0,
                },
            }

            # Check if similar pattern already exists
            existing = next((p for p in patterns if p["pattern"] == pattern["pattern"]), None)
            if existing:
                existing["occurrence_count"] = existing.get("occurrence_count", 1) + 1
                existing["sample_count"] = existing.get("sample_count", 1) + 1
                if profit > 0:
                    existing["win_count"] = existing.get("win_count", 0) + 1
                else:
                    existing["loss_count"] = existing.get("loss_count", 0) + 1
                    # Track contradictory examples (loss when pattern predicted win, etc)
                    existing["contradictory_examples"] = existing.get("contradictory_examples", 0) + 1
                total = existing.get("win_count", 0) + existing.get("loss_count", 0)
                existing["win_rate"] = round(existing.get("win_count", 0) / total * 100, 1) if total > 0 else 0
                existing["learned_at"] = time.time()
                # Update source_breakdown
                sb = existing.get("source_breakdown", {"history": 0, "annotation": 0, "outcome": 0})
                sb["history"] = sb.get("history", 0) + 1
                existing["source_breakdown"] = sb
                # Recompute evidence_strength from data
                existing["evidence_strength"] = self._compute_evidence_strength(existing)
                logger.info(f"HISTORY LEARNED (updated): {existing['pattern']} "
                            f"count={existing['occurrence_count']} wr={existing['win_rate']}% "
                            f"ev={existing['evidence_strength']}")
            else:
                patterns.append(pattern)
                logger.info(f"HISTORY LEARNED (new): {pattern['pattern']} — "
                            f"{pattern['description'][:60]} outcome={outcome}")

            self._save_patterns(patterns)
            return result

        except json.JSONDecodeError as e:
            logger.warning(f"History learning parse error: {e}")
            return None
        except Exception as e:
            logger.error(f"History learning error for {symbol}: {e}", exc_info=True)
            return None

    async def learn_from_all_history(self, deals: list) -> int:
        """Process all history trades and learn from each. Returns count learned."""
        count = 0
        for trade in deals:
            if not trade.get("is_closed"):
                continue
            try:
                result = await self.learn_from_history_trade(trade)
                if result:
                    count += 1
            except Exception as e:
                logger.error(f"History learning error for {trade.get('symbol','?')}: {e}")
        return count
