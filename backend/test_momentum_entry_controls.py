import asyncio
from datetime import datetime, timedelta
import unittest
from unittest.mock import MagicMock

from models import PositionType
from strategy import (
    HSIStrategyEngine,
    MOMENTUM_BOOK_MIN_BUY_RATIO,
    MOMENTUM_ENTRY_FAIL_FAST_POINTS,
    MOMENTUM_ENTRY_FAIL_FAST_RSI,
    MOMENTUM_ENTRY_FAIL_FAST_SECONDS,
    MOMENTUM_ENTRY_MODE,
    MOMENTUM_LATE_VOLUME_SURGE_MULTIPLIER,
    MOMENTUM_VOLUME_SURGE_MULTIPLIER,
)


def snapshot(bid_volume: float, ask_volume: float) -> dict:
    return {
        "bid_price": 0.038,
        "ask_price": 0.039,
        "price_spread": 0.001,
        "bid_volume": bid_volume,
        "ask_volume": ask_volume,
    }


class MomentumEntryControlsTest(unittest.TestCase):
    def setUp(self):
        self.engine = HSIStrategyEngine()
        self.records = []

        async def capture_record(record):
            self.records.append(record)

        self.engine._emit_trade_record = capture_record
        self.engine._save_runtime_state = lambda: None
        self.engine.position = PositionType.NONE
        self.engine.enabled_strategies = ["normal", "extreme", "momentum", "cum_trend"]
        self.engine.bull_warrant_code = "HK.55087"
        self.engine.bear_warrant_code = "HK.67682"
        self.engine.trader.place_order = MagicMock(return_value={
            "success": True,
            "order_id": "ORDER-1",
        })

    def test_bull_momentum_skips_when_book_buy_ratio_is_weak(self):
        weak_buy_ratio = MOMENTUM_BOOK_MIN_BUY_RATIO - 0.05
        self.engine.data_source.get_security_snapshot = MagicMock(return_value=snapshot(
            bid_volume=weak_buy_ratio * 1000000,
            ask_volume=(1 - weak_buy_ratio) * 1000000,
        ))

        submitted = asyncio.run(self.engine._submit_entry_order(
            PositionType.BULL,
            hsi_price=26377.52,
            rsi=67.17,
            current_time="2026-05-12 15:35:51",
            mode=MOMENTUM_ENTRY_MODE,
            extra_message="阳线涨6.8点 | 1.5x量",
        ))

        self.assertFalse(submitted)
        self.engine.trader.place_order.assert_not_called()
        self.assertIn("跳过", self.records[0].message)
        self.assertIn("牛證買盤佔比", self.records[0].message)

    def test_bear_momentum_skips_when_book_buy_ratio_is_weak(self):
        weak_buy_ratio = MOMENTUM_BOOK_MIN_BUY_RATIO - 0.05
        self.engine.data_source.get_security_snapshot = MagicMock(return_value=snapshot(
            bid_volume=weak_buy_ratio * 1000000,
            ask_volume=(1 - weak_buy_ratio) * 1000000,
        ))

        submitted = asyncio.run(self.engine._submit_entry_order(
            PositionType.BEAR,
            hsi_price=26330.0,
            rsi=35.0,
            current_time="2026-05-12 15:10:00",
            mode=MOMENTUM_ENTRY_MODE,
            extra_message="阴线跌9.1点 | 1.5x量",
        ))

        self.assertFalse(submitted)
        self.engine.trader.place_order.assert_not_called()
        self.assertIn("熊證買盤佔比", self.records[0].message)

    def test_momentum_uses_sell1_when_buy_ratio_is_above_90_percent(self):
        self.engine.share_count = 200000
        self.engine.data_source.get_security_snapshot = MagicMock(return_value=snapshot(
            bid_volume=920000,
            ask_volume=80000,
        ))

        submitted = asyncio.run(self.engine._submit_entry_order(
            PositionType.BULL,
            hsi_price=26377.52,
            rsi=67.17,
            current_time="2026-05-15 10:01:00",
            mode=MOMENTUM_ENTRY_MODE,
            extra_message="阳线涨6.8点 | 1.5x量",
        ))

        self.assertTrue(submitted)
        self.engine.trader.place_order.assert_called_once_with("HK.55087", 0.039, 200000, "BUY")
        self.assertIn("挂 sell1 买入", self.records[0].message)
        self.assertIn("買盤佔比:92.0%", self.records[0].message)

    def test_momentum_uses_middle_price_when_spread_has_one_price_between_bid_and_ask(self):
        self.engine.share_count = 200000
        self.engine.data_source.get_security_snapshot = MagicMock(return_value={
            "bid_price": 0.038,
            "ask_price": 0.040,
            "price_spread": 0.001,
            "bid_volume": 600000,
            "ask_volume": 400000,
        })

        submitted = asyncio.run(self.engine._submit_entry_order(
            PositionType.BEAR,
            hsi_price=26330.0,
            rsi=35.0,
            current_time="2026-05-15 10:02:00",
            mode=MOMENTUM_ENTRY_MODE,
            extra_message="阴线跌9.1点 | 1.5x量",
        ))

        self.assertTrue(submitted)
        self.engine.trader.place_order.assert_called_once_with("HK.67682", 0.039, 200000, "BUY")
        self.assertIn("挂 中間 买入", self.records[0].message)
        self.assertIn("buy1和sell1相隔1格，掛中間", self.records[0].message)

    def test_momentum_late_session_requires_higher_volume_ratio(self):
        self.assertEqual(
            self.engine._required_momentum_volume_multiplier("2026-05-12 15:29:59"),
            MOMENTUM_VOLUME_SURGE_MULTIPLIER,
        )
        self.assertEqual(
            self.engine._required_momentum_volume_multiplier("2026-05-12 15:30:00"),
            MOMENTUM_LATE_VOLUME_SURGE_MULTIPLIER,
        )

    def test_momentum_fail_fast_exits_bull_when_price_and_rsi_break_down(self):
        self.engine.position = PositionType.BULL
        self.engine.entry_mode = MOMENTUM_ENTRY_MODE
        self.engine.current_warrant_code = "HK.55087"
        self.engine.exit_order_id = "EXIT-1"
        self.engine.entry_price = 26377.39
        self.engine.warrant_qty = 200000
        self.engine.entry_fill_time = datetime.now() - timedelta(
            seconds=MOMENTUM_ENTRY_FAIL_FAST_SECONDS + 1
        )
        self.engine.data_source.get_security_snapshot = MagicMock(return_value=snapshot(
            bid_volume=600000,
            ask_volume=400000,
        ))
        self.engine.trader.modify_order = MagicMock(return_value={
            "success": True,
            "message": "改单成功",
        })

        hsi_price = self.engine.entry_price - MOMENTUM_ENTRY_FAIL_FAST_POINTS
        asyncio.run(self.engine._handle_momentum_fail_fast_exit(
            "2026-05-12 15:37:10",
            hsi_price=hsi_price,
            rsi=MOMENTUM_ENTRY_FAIL_FAST_RSI,
            diff=hsi_price - self.engine.entry_price,
            actual_pnl=-160,
            reason_text=self.engine._momentum_fail_fast_reason(
                hsi_price,
                MOMENTUM_ENTRY_FAIL_FAST_RSI,
            ),
        ))

        self.engine.trader.modify_order.assert_called_once_with("EXIT-1", 0.038, 200000)
        self.assertTrue(self.engine.stop_loss_order_sent)
        self.assertTrue(self.engine.momentum_fail_fast_sent)
        self.assertIn("放量动能失败早退", self.records[0].message)


if __name__ == "__main__":
    unittest.main()
