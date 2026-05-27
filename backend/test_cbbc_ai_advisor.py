"""
``cbbc_ai_advisor.request_advice`` 的离线单元测试。

不真的打 LLM 端点 — 把 ``_post_chat_completion`` 用 ``unittest.mock`` 替成
返回固定文本,验证:
- 完整 happy path 解析成 ``ok=True``
- 解析失败时返回 ``ok=False`` 且保留 raw_model_text
- 无 api_key / base_url 时直接早返回 ``ok=False``
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch, AsyncMock

import cbbc_ai_advisor as ai


_SAMPLE_ZONES = {
    "spot": 25500.0,
    "today_low": 25431.17,
    "today_high": 25588.0,
    "bucket_pts": 25,
    "live_record_count": 142,
    "killed_record_count": 8,
    "targets_above": [
        {
            "bucket_low": 25600.0,
            "bucket_high": 25625.0,
            "direction": "bear",
            "distance_pts": 100.0,
            "notional_hkd": 12_500_000.0,
            "contract_count": 12,
            "outstanding_shares": 5_000_000.0,
        },
    ],
    "supports_below": [
        {
            "bucket_low": 25400.0,
            "bucket_high": 25425.0,
            "direction": "bull",
            "distance_pts": -100.0,
            "notional_hkd": 8_700_000.0,
            "contract_count": 9,
            "outstanding_shares": 3_500_000.0,
        },
    ],
}

_SAMPLE_STATE = {
    "cbbc_magnet_layer_enabled": True,
    "cbbc_magnet_degraded": False,
    "cbbc_magnet_bias": -0.18,
    "cbbc_nearest_bull_distance_pts": 70.0,
    "cbbc_nearest_bear_distance_pts": 100.0,
}

_SAMPLE_REGIME = {
    "regime": "trending_up",
    "label": "上行趋势",
    "bias": "bullish",
    "confidence": 70,
    "advice": "顺势做多",
    "current_price": 25500.0,
    "day_open": 25400.0,
}


def _run(coro):
    return asyncio.run(coro)


class ExtractJsonObjectTests(unittest.TestCase):
    def test_strips_code_fence(self) -> None:
        text = '```json\n{"direction":"bull","confidence":0.7}\n```'
        obj = ai._extract_json_object(text)
        self.assertEqual(obj, {"direction": "bull", "confidence": 0.7})

    def test_finds_json_after_preamble(self) -> None:
        text = '好的,这是建议:\n{"direction":"bear","confidence":0.5}'
        obj = ai._extract_json_object(text)
        self.assertEqual(obj, {"direction": "bear", "confidence": 0.5})

    def test_returns_none_on_unbalanced(self) -> None:
        self.assertIsNone(ai._extract_json_object("{not json"))

    def test_handles_nested(self) -> None:
        text = '{"a":1,"b":{"c":2,"d":3}}'
        obj = ai._extract_json_object(text)
        self.assertEqual(obj, {"a": 1, "b": {"c": 2, "d": 3}})


class ParseAdviceTests(unittest.TestCase):
    def test_happy_path_bull(self) -> None:
        raw = json.dumps({
            "direction": "bull",
            "confidence": 0.72,
            "entry_low": 25490,
            "entry_high": 25510,
            "take_profit_1": 25560,
            "take_profit_2": 25600,
            "stop_loss": 25450,
            "rationale": "下方街货支撑且突破开盘价",
        })
        adv = ai._parse_advice(raw, model="gpt-4o-mini", elapsed_seconds=0.42)
        self.assertTrue(adv.ok)
        self.assertEqual(adv.direction, "bull")
        self.assertAlmostEqual(adv.confidence, 0.72)
        self.assertEqual(adv.entry_low, 25490.0)
        self.assertEqual(adv.take_profit_2, 25600.0)
        self.assertEqual(adv.stop_loss, 25450.0)
        self.assertEqual(adv.rationale, "下方街货支撑且突破开盘价")
        self.assertEqual(adv.model, "gpt-4o-mini")

    def test_skip_direction_with_null_prices(self) -> None:
        raw = json.dumps({
            "direction": "skip",
            "confidence": 0.2,
            "entry_low": None,
            "entry_high": None,
            "take_profit_1": None,
            "take_profit_2": None,
            "stop_loss": None,
            "rationale": "无明显信号",
        })
        adv = ai._parse_advice(raw, model="m", elapsed_seconds=0.1)
        self.assertTrue(adv.ok)
        self.assertEqual(adv.direction, "skip")
        self.assertIsNone(adv.entry_low)

    def test_invalid_direction_returns_error(self) -> None:
        raw = json.dumps({"direction": "buy", "confidence": 0.5})
        adv = ai._parse_advice(raw, model="m", elapsed_seconds=0.1)
        self.assertFalse(adv.ok)
        self.assertIn("direction", adv.error or "")

    def test_garbage_returns_error_and_preserves_raw(self) -> None:
        raw = "completely not json"
        adv = ai._parse_advice(raw, model="m", elapsed_seconds=0.1)
        self.assertFalse(adv.ok)
        self.assertEqual(adv.raw_model_text, raw)

    def test_clamps_confidence_to_unit_range(self) -> None:
        for raw_conf, expected in [(1.5, 1.0), (-0.2, 0.0), (0.5, 0.5)]:
            with self.subTest(raw_conf=raw_conf):
                raw = json.dumps({"direction": "bull", "confidence": raw_conf})
                adv = ai._parse_advice(raw, model="m", elapsed_seconds=0.1)
                self.assertTrue(adv.ok)
                self.assertEqual(adv.confidence, expected)


class RequestAdviceTests(unittest.TestCase):
    def test_missing_api_key_short_circuits(self) -> None:
        adv = _run(ai.request_advice(
            zones=_SAMPLE_ZONES, state=_SAMPLE_STATE, regime=_SAMPLE_REGIME,
            api_key="", base_url="http://x", model="m",
        ))
        self.assertFalse(adv.ok)
        self.assertIn("api_key", adv.error or "")

    def test_missing_base_url_short_circuits(self) -> None:
        adv = _run(ai.request_advice(
            zones=_SAMPLE_ZONES, state=_SAMPLE_STATE, regime=_SAMPLE_REGIME,
            api_key="sk-x", base_url="", model="m",
        ))
        self.assertFalse(adv.ok)
        self.assertIn("base_url", adv.error or "")

    def test_happy_path_with_mocked_post(self) -> None:
        raw = json.dumps({
            "direction": "bear",
            "confidence": 0.8,
            "entry_low": 25510,
            "entry_high": 25525,
            "take_profit_1": 25460,
            "take_profit_2": 25420,
            "stop_loss": 25560,
            "rationale": "上方密集带阻力,RSI 超买",
        })
        with patch.object(
            ai, "_post_chat_completion",
            new=AsyncMock(return_value=(raw, 0.4, "claude-opus-4-7")),
        ):
            adv = _run(ai.request_advice(
                zones=_SAMPLE_ZONES, state=_SAMPLE_STATE, regime=_SAMPLE_REGIME,
                api_key="sk-test", base_url="http://127.0.0.1:8765", model="claude-opus-4-7",
            ))
        self.assertTrue(adv.ok)
        self.assertEqual(adv.direction, "bear")
        self.assertEqual(adv.entry_low, 25510.0)
        self.assertEqual(adv.stop_loss, 25560.0)
        self.assertEqual(adv.model, "claude-opus-4-7")

    def test_rate_limit_returns_friendly_chinese_error(self) -> None:
        async def _raise(**_kwargs):
            raise ai._RateLimitError(503, '{"error":{"message":"所有账号均达到速率限制"}}')
        with patch.object(ai, "_post_chat_completion", new=_raise):
            adv = _run(ai.request_advice(
                zones=_SAMPLE_ZONES, state=_SAMPLE_STATE, regime=_SAMPLE_REGIME,
                api_key="sk-test", base_url="http://127.0.0.1:8765",
                model="claude-opus-4-7",
            ))
        self.assertFalse(adv.ok)
        self.assertIn("速率限制", adv.error or "")
        self.assertIn("503", adv.error or "")

    def test_http_error_returns_ok_false(self) -> None:
        async def _raise(**_kwargs):
            raise RuntimeError("AI endpoint returned HTTP 502: bad gateway")
        with patch.object(ai, "_post_chat_completion", new=_raise):
            adv = _run(ai.request_advice(
                zones=_SAMPLE_ZONES, state=_SAMPLE_STATE, regime=_SAMPLE_REGIME,
                api_key="sk-test", base_url="http://127.0.0.1:8765", model="m",
            ))
        self.assertFalse(adv.ok)
        self.assertIn("HTTP 502", adv.error or "")


if __name__ == "__main__":
    unittest.main()
