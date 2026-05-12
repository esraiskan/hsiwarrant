import asyncio
import unittest
from unittest.mock import MagicMock

import strategy
from models import PositionType
from strategy import HSIStrategyEngine


class OnlyExtremeEntriesTest(unittest.TestCase):
    def setUp(self):
        self.engine = HSIStrategyEngine()
        self.records = []
        self.engine._reset_order_state()
        self.engine.position = PositionType.NONE

        async def capture_record(record):
            self.records.append(record)

        self.engine._emit_trade_record = capture_record
        self.engine._save_runtime_state = lambda: None

    def test_blocks_non_extreme_entry_without_placing_order(self):
        self.engine.only_extreme_entries = True
        self.engine.bull_warrant_code = "HK.12345"
        self.engine.data_source.get_security_snapshot = MagicMock()
        self.engine.trader.place_order = MagicMock()

        asyncio.run(self.engine._submit_entry_order(
            PositionType.BULL,
            hsi_price=26000.0,
            rsi=55.0,
            current_time="2026-05-11 10:00:00",
            mode="放量动能",
        ))

        self.engine.data_source.get_security_snapshot.assert_not_called()
        self.engine.trader.place_order.assert_not_called()
        self.assertEqual(len(self.records), 1)
        self.assertIn("已开启只买极度超买/超卖", self.records[0].message)

    def test_allows_extreme_entry_to_place_order(self):
        self.engine.only_extreme_entries = True
        self.engine.bull_warrant_code = "HK.12345"
        self.engine.data_source.get_security_snapshot = MagicMock(return_value={
            "bid_price": 0.052,
            "ask_price": 0.053,
            "price_spread": 0.001,
        })
        self.engine.trader.place_order = MagicMock(return_value={
            "success": True,
            "order_id": "ORDER-1",
        })

        asyncio.run(self.engine._submit_entry_order(
            PositionType.BULL,
            hsi_price=26000.0,
            rsi=15.0,
            current_time="2026-05-11 10:00:00",
            mode="极度超卖",
        ))

        self.engine.data_source.get_security_snapshot.assert_called_once_with(
            "HK.12345",
            include_order_book=True,
        )
        self.engine.trader.place_order.assert_called_once_with("HK.12345", 0.052, self.engine.share_count, "BUY")
        self.assertEqual(self.engine.pending_buy_order_id, "ORDER-1")

    def test_runtime_config_payload_includes_only_extreme_entries(self):
        self.engine.only_extreme_entries = True

        payload = self.engine._runtime_config_payload()

        self.assertIs(payload["only_extreme_entries"], True)

    def test_runtime_config_loads_only_extreme_entries_default_false(self):
        original_load_runtime_config = strategy.load_runtime_config
        try:
            strategy.load_runtime_config = lambda: {"share_count": 100000}
            engine = HSIStrategyEngine()
        finally:
            strategy.load_runtime_config = original_load_runtime_config

        self.assertIs(engine.only_extreme_entries, False)

    def test_runtime_config_loads_only_extreme_entries_true(self):
        original_load_runtime_config = strategy.load_runtime_config
        try:
            strategy.load_runtime_config = lambda: {
                "share_count": 100000,
                "only_extreme_entries": True,
            }
            engine = HSIStrategyEngine()
        finally:
            strategy.load_runtime_config = original_load_runtime_config

        self.assertIs(engine.only_extreme_entries, True)


if __name__ == "__main__":
    unittest.main()
