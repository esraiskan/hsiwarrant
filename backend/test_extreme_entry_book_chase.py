import asyncio
from datetime import datetime, timedelta
import unittest
from unittest.mock import MagicMock

from models import PositionType
from strategy import (
    EXTREME_ENTRY_FIRST_WAIT_SECONDS,
    HSIStrategyEngine,
    MOMENTUM_ENTRY_MODE,
)


class ExtremeEntryBookChaseTest(unittest.TestCase):
    def setUp(self):
        self.engine = HSIStrategyEngine()
        self.records = []

        async def capture_record(record):
            self.records.append(record)

        self.engine._emit_trade_record = capture_record
        self.engine._save_runtime_state = lambda: None
        self.engine.position = PositionType.NONE
        self.engine.current_warrant_code = "HK.12345"
        self.engine.pending_buy_order_id = "ORDER-1"
        self.engine.pending_entry_side = PositionType.BULL
        self.engine.entry_chase_count = 0
        self.engine.entry_order_time = datetime.now() - timedelta(seconds=EXTREME_ENTRY_FIRST_WAIT_SECONDS + 1)
        self.engine.momentum_entry_trigger_price = 26000.0
        self.engine.trader.get_order = MagicMock(return_value={
            "order_status": "SUBMITTED",
            "price": 0.052,
        })
        self.engine.trader.modify_order = MagicMock(return_value={
            "success": True,
            "message": "改单成功",
        })
        self.engine.trader.cancel_order = MagicMock(return_value={
            "success": True,
            "message": "撤单成功",
        })

    def test_extreme_entry_chases_one_tick_when_book_is_strong(self):
        self.engine.entry_mode = "极度超卖"
        self.engine.data_source.get_security_snapshot = MagicMock(return_value={
            "bid_price": 0.052,
            "ask_price": 0.053,
            "price_spread": 0.001,
            "bid_volume": 800000,
            "ask_volume": 200000,
        })

        asyncio.run(self.engine._monitor_entry_order(
            "2026-05-12 10:00:00",
            hsi_price=26005.0,
            rsi=15.0,
        ))

        self.engine.trader.modify_order.assert_called_once_with("ORDER-1", 0.053, self.engine.share_count)
        self.engine.trader.cancel_order.assert_not_called()
        self.assertEqual(self.engine.entry_chase_count, 1)
        self.assertIn("0.052->0.053", self.records[0].message)
        self.assertIn("買盤佔比:80.0%", self.records[0].message)
        self.assertIn("強買盤且賣一薄", self.records[0].message)

    def test_extreme_entry_keeps_latest_bid_when_book_is_not_strong_enough(self):
        self.engine.entry_mode = "极度超卖"
        self.engine.data_source.get_security_snapshot = MagicMock(return_value={
            "bid_price": 0.052,
            "ask_price": 0.053,
            "price_spread": 0.001,
            "bid_volume": 500000,
            "ask_volume": 500000,
        })

        asyncio.run(self.engine._monitor_entry_order(
            "2026-05-12 10:00:00",
            hsi_price=26005.0,
            rsi=15.0,
        ))

        self.engine.trader.modify_order.assert_not_called()
        self.engine.trader.cancel_order.assert_not_called()
        self.assertEqual(self.engine.pending_buy_order_id, "ORDER-1")
        self.assertEqual(self.engine.entry_chase_count, 1)
        self.assertIn("继续挂 buy1", self.records[0].message)
        self.assertIn("買盤佔比50.0%未達門檻", self.records[0].message)

    def test_non_extreme_entry_still_chases_latest_bid(self):
        self.engine.entry_mode = MOMENTUM_ENTRY_MODE
        self.engine.entry_order_time = datetime.now() - timedelta(seconds=self.engine.entry_order_wait_seconds + 1)
        self.engine.data_source.get_security_snapshot = MagicMock(return_value={
            "bid_price": 0.053,
            "ask_price": 0.054,
            "price_spread": 0.001,
            "bid_volume": 500000,
            "ask_volume": 500000,
        })

        asyncio.run(self.engine._monitor_entry_order(
            "2026-05-12 10:00:00",
            hsi_price=26005.0,
            rsi=60.0,
        ))

        self.engine.trader.modify_order.assert_called_once_with("ORDER-1", 0.053, self.engine.share_count)
        self.engine.trader.cancel_order.assert_not_called()
        self.assertIn("追價到最新 buy1", self.records[0].message)


if __name__ == "__main__":
    unittest.main()
