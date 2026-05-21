import asyncio
import unittest
from unittest.mock import MagicMock

from models import PositionType
from strategy import (
    MOMENTUM_ENTRY_MODE,
    MOMENTUM_PENDING_ADVERSE_MOVE_POINTS,
    MOMENTUM_PENDING_VWAP_SLOPE_BUFFER,
    HSIStrategyEngine,
)


class DegradedMomentumEntryTest(unittest.TestCase):
    def setUp(self):
        self.engine = HSIStrategyEngine()
        self.records = []

        async def capture_record(record):
            self.records.append(record)

        self.engine._emit_trade_record = capture_record
        self.engine._save_runtime_state = lambda: None
        self.engine.trader.get_order = MagicMock(return_value=None)
        self.engine.trader.cancel_order = MagicMock(return_value={
            "success": True,
            "message": "撤单成功",
        })

    def _set_pending_momentum(self, side, trigger_price=26410.0):
        self.engine.entry_mode = MOMENTUM_ENTRY_MODE
        self.engine.pending_buy_order_id = "ORDER-1"
        self.engine.pending_entry_side = side
        self.engine.position = PositionType.NONE
        self.engine.momentum_entry_trigger_price = trigger_price

    def test_bull_momentum_pending_cancels_on_adverse_pullback(self):
        self._set_pending_momentum(PositionType.BULL, trigger_price=26410.0)

        cancelled = asyncio.run(self.engine._cancel_degraded_momentum_entry(
            "2026-05-11 15:26:30",
            hsi_price=26410.0 - MOMENTUM_PENDING_ADVERSE_MOVE_POINTS,
            rsi=65.0,
            curr_slope=0.2,
        ))

        self.assertTrue(cancelled)
        self.engine.trader.cancel_order.assert_called_once_with("ORDER-1")
        self.assertEqual(self.engine.pending_buy_order_id, "")
        self.assertIn("放量动能买入挂单期间信号退化", self.records[0].message)
        self.assertIn("回落5.0点", self.records[0].message)

    def test_bear_momentum_pending_cancels_on_adverse_rebound(self):
        self._set_pending_momentum(PositionType.BEAR, trigger_price=26410.0)

        cancelled = asyncio.run(self.engine._cancel_degraded_momentum_entry(
            "2026-05-11 15:26:30",
            hsi_price=26410.0 + MOMENTUM_PENDING_ADVERSE_MOVE_POINTS,
            rsi=40.0,
            curr_slope=-0.2,
        ))

        self.assertTrue(cancelled)
        self.engine.trader.cancel_order.assert_called_once_with("ORDER-1")
        self.assertEqual(self.engine.pending_buy_order_id, "")
        self.assertIn("反弹5.0点", self.records[0].message)

    def test_bull_momentum_pending_keeps_order_when_vwap_slope_is_flat(self):
        self._set_pending_momentum(PositionType.BULL, trigger_price=26410.0)

        cancelled = asyncio.run(self.engine._cancel_degraded_momentum_entry(
            "2026-05-11 15:26:30",
            hsi_price=26409.0,
            rsi=65.0,
            curr_slope=0.0,
        ))

        self.assertFalse(cancelled)
        self.engine.trader.cancel_order.assert_not_called()
        self.assertEqual(self.engine.pending_buy_order_id, "ORDER-1")
        self.assertEqual(self.records, [])

    def test_bull_momentum_pending_cancels_when_vwap_slope_reverses_past_buffer(self):
        self._set_pending_momentum(PositionType.BULL, trigger_price=26410.0)

        cancelled = asyncio.run(self.engine._cancel_degraded_momentum_entry(
            "2026-05-11 15:26:30",
            hsi_price=26409.0,
            rsi=65.0,
            curr_slope=-(MOMENTUM_PENDING_VWAP_SLOPE_BUFFER + 0.01),
        ))

        self.assertTrue(cancelled)
        self.engine.trader.cancel_order.assert_called_once_with("ORDER-1")
        self.assertIn("VWAP斜率反向<=-0.05", self.records[0].message)

    def test_bear_momentum_pending_keeps_order_when_vwap_slope_is_flat(self):
        self._set_pending_momentum(PositionType.BEAR, trigger_price=26410.0)

        cancelled = asyncio.run(self.engine._cancel_degraded_momentum_entry(
            "2026-05-11 15:26:30",
            hsi_price=26409.0,
            rsi=40.0,
            curr_slope=0.0,
        ))

        self.assertFalse(cancelled)
        self.engine.trader.cancel_order.assert_not_called()
        self.assertEqual(self.engine.pending_buy_order_id, "ORDER-1")
        self.assertEqual(self.records, [])

    def test_bear_momentum_pending_cancels_when_vwap_slope_reverses_past_buffer(self):
        self._set_pending_momentum(PositionType.BEAR, trigger_price=26410.0)

        cancelled = asyncio.run(self.engine._cancel_degraded_momentum_entry(
            "2026-05-11 15:26:30",
            hsi_price=26409.0,
            rsi=40.0,
            curr_slope=MOMENTUM_PENDING_VWAP_SLOPE_BUFFER + 0.01,
        ))

        self.assertTrue(cancelled)
        self.engine.trader.cancel_order.assert_called_once_with("ORDER-1")
        self.assertIn("VWAP斜率反向>=0.05", self.records[0].message)

    def test_momentum_pending_keeps_order_when_signal_still_valid(self):
        self._set_pending_momentum(PositionType.BULL, trigger_price=26410.0)

        cancelled = asyncio.run(self.engine._cancel_degraded_momentum_entry(
            "2026-05-11 15:26:30",
            hsi_price=26408.0,
            rsi=65.0,
            curr_slope=0.2,
        ))

        self.assertFalse(cancelled)
        self.engine.trader.cancel_order.assert_not_called()
        self.assertEqual(self.engine.pending_buy_order_id, "ORDER-1")
        self.assertEqual(self.records, [])


if __name__ == "__main__":
    unittest.main()
