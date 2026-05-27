"""Unit tests for the HKSI-style AI advisor.

Focus areas:
- ``build_context`` is now compact (no full 80-bar dump)
- ``_attempt_autofix`` recovers structured arrays from prose
- ``_validate_semantics`` only nukes when autofix can't save the result
- ``_extract_levels_from_text`` accepts HSI-shaped numbers and rejects junk
"""
from __future__ import annotations

import unittest

from cbbc_hksi_style_ai_advisor import (
    _attempt_autofix,
    _extract_levels_from_text,
    _validate_semantics,
    build_context,
    normalize_review,
)


_ZONES = {
    "spot": 25500.0,
    "today_low": 25431.17,
    "today_high": 25588.0,
    "bucket_pts": 25,
    "live_record_count": 142,
    "killed_record_count": 8,
    "targets_above": [
        {"bucket_low": 25600.0, "nearest_call_level": 25608.0,
         "direction": "bear", "distance_pts": 100.0,
         "notional_hkd": 12_500_000.0, "contract_count": 12,
         "outstanding_shares": 5_000_000.0, "safety_margin_pts": 20.0},
        {"bucket_low": 25700.0, "nearest_call_level": 25705.0,
         "direction": "bear", "distance_pts": 200.0,
         "notional_hkd": 9_000_000.0, "contract_count": 8,
         "outstanding_shares": 3_500_000.0, "safety_margin_pts": 117.0},
    ],
    "supports_below": [
        {"bucket_low": 25400.0, "nearest_call_level": 25395.0,
         "direction": "bull", "distance_pts": -100.0,
         "notional_hkd": 8_700_000.0, "contract_count": 9,
         "outstanding_shares": 3_500_000.0, "safety_margin_pts": 36.17},
    ],
}

_STATE = {
    "position": "none", "current_price": 25500.0, "entry_price": 0,
    "trade_count": 5, "win_count": 3, "loss_count": 2, "is_running": True,
    "cbbc_magnet_layer_enabled": True, "cbbc_magnet_degraded": False,
    "cbbc_magnet_bias": -0.18,
    "cbbc_nearest_bull_distance_pts": 70.0, "cbbc_nearest_bear_distance_pts": 100.0,
}

_REGIME = {
    "regime": "trending_up", "label": "上行趨勢", "bias": "bullish",
    "confidence": 70, "advice": "順勢做多",
    "current_price": 25500.0, "day_open": 25400.0,
}


def _make_klines(n: int) -> list[dict]:
    rows = []
    base = 25400.0
    for i in range(n):
        c = base + i * 1.5
        rows.append({
            "time": f"2026-05-27 {10 + i // 60:02d}:{i % 60:02d}",
            "open": c - 1, "high": c + 2, "low": c - 2,
            "close": c, "volume": 1000.0 + i,
            "rsi": 50.0 + (i % 20) - 10, "vwap": c - 0.5,
        })
    return rows


class BuildContextCompactnessTests(unittest.TestCase):
    """Confirm new build_context is much smaller than the old 80-row dump."""

    def test_omits_full_recent_klines(self) -> None:
        ctx = build_context(zones=_ZONES, state=_STATE, regime=_REGIME, klines=_make_klines(80))
        self.assertNotIn("recent_klines", ctx)
        self.assertIn("last_5_klines", ctx)
        self.assertIn("klines_summary", ctx)
        self.assertEqual(len(ctx["last_5_klines"]), 5)

    def test_klines_summary_has_aggregates(self) -> None:
        ctx = build_context(zones=_ZONES, state=_STATE, regime=_REGIME, klines=_make_klines(30))
        s = ctx["klines_summary"]
        self.assertEqual(s["count"], 30)
        self.assertGreater(s["high"], s["low"])
        self.assertIn(s["trend"], {"flat", "rising", "falling"})
        self.assertIsNotNone(s["rsi_now"])

    def test_zones_top_3_only(self) -> None:
        big_zones = {
            **_ZONES,
            "targets_above": _ZONES["targets_above"] * 5,
            "supports_below": _ZONES["supports_below"] * 5,
        }
        ctx = build_context(zones=big_zones, state=_STATE, regime=_REGIME, klines=[])
        self.assertLessEqual(len(ctx["cbbc_zones"]["targets_above"]), 3)
        self.assertLessEqual(len(ctx["cbbc_zones"]["supports_below"]), 3)


class ExtractLevelsTests(unittest.TestCase):
    def test_extracts_4_5_digit_hsi_levels(self) -> None:
        text = ["價位 25800 - 25850", "止賺 25,900"]
        out = _extract_levels_from_text(text)
        self.assertEqual(out, [25800.0, 25850.0, 25900.0])

    def test_rejects_non_hsi_numbers(self) -> None:
        text = ["amount 5000", "RSI 65", "confidence 0.7"]
        out = _extract_levels_from_text(text)
        self.assertEqual(out, [])

    def test_handles_empty(self) -> None:
        self.assertEqual(_extract_levels_from_text([]), [])
        self.assertEqual(_extract_levels_from_text(["", None]), [])  # type: ignore[arg-type]


class AutofixTests(unittest.TestCase):
    def test_recovers_take_profit_levels_from_text(self) -> None:
        plan = {
            "take_profit": ["第一目標 25800,第二目標 25850"],
            "stop_loss": ["跌破 25400 止蝕"],
            "entry_plan_1": {"action": "買升", "amount": 4000, "condition": "現價站穩 25500 入場"},
        }
        execution = {
            "enabled": True, "side": "bull", "entry_rules": [],
            "take_profit_levels": [], "stop_loss_levels": [], "give_up_levels": [],
            "max_position_hkd": 4000,
        }
        coefficients = {
            "direction": "NEUTRAL",
            "entry_min": 0, "entry_max": 0,
            "take_profit_min": 0, "take_profit_max": 0,
            "stop_loss_min": 0, "stop_loss_max": 0,
        }
        notes = _attempt_autofix(plan, execution, coefficients)
        self.assertIn("take_profit_levels", str(notes))
        self.assertIn("stop_loss_levels", str(notes))
        self.assertIn("entry_rules", str(notes))
        self.assertEqual(execution["take_profit_levels"], [25800.0, 25850.0])
        self.assertEqual(execution["stop_loss_levels"], [25400.0])
        self.assertEqual(len(execution["entry_rules"]), 1)
        self.assertEqual(execution["entry_rules"][0]["action"], "buy_bull")
        self.assertEqual(coefficients["direction"], "UP")
        self.assertEqual(coefficients["take_profit_min"], 25800.0)
        self.assertEqual(coefficients["stop_loss_min"], 25400.0)


class ValidateSemanticsAutofixIntegrationTests(unittest.TestCase):
    def _enabled_review_with_text_only(self) -> dict:
        """A review that, in the old version, would have been nuked but
        autofix should now recover."""
        raw = {
            "summary": "做多",
            "trade_plan": {
                "main_direction": "偏升 [up]",
                "status": "空倉等待",
                "entry_plan_1": {"condition": "現價站穩 25500 入場", "action": "買升", "amount": 4000},
                "entry_plan_2": {"condition": "", "action": "不做", "amount": 0},
                "stop_loss": ["跌破 25400 止蝕"],
                "take_profit": ["目標 25800 / 25850"],
                "give_up_conditions": ["VWAP 失守"],
                "product_warning": "避開太貼價收回價",
                "summary": "順勢做多",
            },
            "execution_plan": {
                "enabled": True, "side": "bull", "entry_rules": [],
                "take_profit_levels": [], "stop_loss_levels": [], "give_up_levels": [],
                "max_position_hkd": 4000, "time_in_force": "intraday", "notes": "",
            },
            "trade_coefficients": {
                "direction": "UP", "entry_min": 0, "entry_max": 0,
                "share_count": 200, "take_profit_min": 0, "take_profit_max": 0,
                "stop_loss_min": 0, "stop_loss_max": 0, "confidence": 0.6,
            },
            "risk_level": "medium",
            "key_supporting_points": ["bias 上方"],
            "conflicts": [],
            "watch_levels": [],
            "data_quality_notes": [],
            "limitations": [],
            "actionability": "observe",
            "suggested_user_action": "等候 25500 站穩",
            "confidence_comment": "中性",
        }
        # Use normalize_review to fully exercise the production path.
        return normalize_review(raw, context={})

    def test_autofix_keeps_status_alive(self) -> None:
        review = self._enabled_review_with_text_only()
        # Should NOT have been nuked to "不交易".
        self.assertEqual(review["trade_plan"]["status"], "空倉等待")
        self.assertTrue(review["execution_plan"]["enabled"])
        self.assertEqual(review["execution_plan"]["side"], "bull")
        self.assertGreaterEqual(len(review["execution_plan"]["entry_rules"]), 1)
        self.assertGreaterEqual(len(review["execution_plan"]["take_profit_levels"]), 1)
        # Auto-fix notes should be visible to the user.
        self.assertTrue(any(
            "auto-fix" in n for n in review.get("data_quality_notes", [])
        ))

    def test_genuine_contradiction_still_nukes(self) -> None:
        """If there is no recoverable info,validator still falls back to '不交易'."""
        raw = {
            "summary": "",
            "trade_plan": {
                "main_direction": "中性 [neutral]",
                "status": "空倉等待",
                "entry_plan_1": {"condition": "", "action": "不做", "amount": 0},
                "entry_plan_2": {"condition": "", "action": "不做", "amount": 0},
                "stop_loss": [], "take_profit": [], "give_up_conditions": [],
                "product_warning": "", "summary": "",
            },
            "execution_plan": {
                "enabled": True, "side": "bull", "entry_rules": [],
                "take_profit_levels": [], "stop_loss_levels": [], "give_up_levels": [],
                "max_position_hkd": 0, "time_in_force": "intraday", "notes": "",
            },
            "trade_coefficients": {
                "direction": "UP", "entry_min": 0, "entry_max": 0,
                "share_count": 0, "take_profit_min": 0, "take_profit_max": 0,
                "stop_loss_min": 0, "stop_loss_max": 0, "confidence": 0.0,
            },
            "risk_level": "medium",
            "key_supporting_points": [], "conflicts": [], "watch_levels": [],
            "data_quality_notes": [], "limitations": [],
            "actionability": "observe", "suggested_user_action": "", "confidence_comment": "",
        }
        review = normalize_review(raw, context={})
        self.assertEqual(review["trade_plan"]["status"], "不交易")
        self.assertFalse(review["execution_plan"]["enabled"])


if __name__ == "__main__":
    unittest.main()
