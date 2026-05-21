import asyncio
import unittest
from unittest.mock import MagicMock

from models import PositionType
from strategy import HSIStrategyEngine


class ExtremeRsiStopVetoTest(unittest.TestCase):
    def setUp(self):
        self.engine = HSIStrategyEngine()
        self.records = []
        self.engine._reset_order_state()
        self.engine._save_runtime_state = lambda: None
        self.engine._save_runtime_config = lambda: None
        self.engine._emit_trade_record = self._capture_record
        self.engine.position = PositionType.BULL
        self.engine.current_warrant_code = "HK.55733"
        self.engine.exit_order_id = "TAKE-PROFIT-1"
        self.engine.warrant_qty = 200000
        self.engine.warrant_tick_size = 0.001
        self.engine.rsi_oversold = 22
        self.engine.rsi_overbought = 80
        self.engine.extreme_rsi_stop_veto_enabled = True
        self.engine.extreme_rsi_stop_hard_ticks = 2
        self.engine.extreme_rsi_stop_rearm_ticks = 1
        self.engine.data_source.get_security_snapshot = MagicMock(return_value={
            "bid_price": 0.025,
            "ask_price": 0.026,
            "price_spread": 0.001,
        })
        self.engine.trader.modify_order = MagicMock(return_value={
            "success": True,
            "message": "改单成功",
        })
        self.engine.trader.place_order = MagicMock()

    async def _capture_record(self, record):
        self.records.append(record)

    def test_extreme_rsi_cancels_current_stop_and_sets_two_tick_hard_stop(self):
        asyncio.run(self.engine._handle_stop_loss(
            "2026-05-13 10:30:00",
            hsi_price=26000.0,
            rsi=21.0,
            diff=-50.0,
            actual_pnl=-1000.0,
            active_stop_points=50.0,
        ))

        self.engine.trader.modify_order.assert_not_called()
        self.engine.trader.place_order.assert_not_called()
        self.assertTrue(self.engine.extreme_rsi_stop_veto_active)
        self.assertEqual(self.engine.extreme_rsi_stop_veto_price, 0.025)
        self.assertEqual(self.engine.extreme_rsi_stop_hard_price, 0.023)
        self.assertEqual(self.engine.extreme_rsi_stop_rearm_price, 0.026)
        self.assertIn("极端RSI取消本次普通止损", self.records[0].message)

    def test_hard_stop_after_veto_modifies_existing_exit_order_without_rsi_check(self):
        self.engine.extreme_rsi_stop_veto_active = True
        self.engine.extreme_rsi_stop_veto_price = 0.025
        self.engine.extreme_rsi_stop_hard_price = 0.023
        self.engine.extreme_rsi_stop_rearm_price = 0.026
        self.engine.data_source.get_security_snapshot.return_value = {
            "bid_price": 0.023,
            "ask_price": 0.024,
            "price_spread": 0.001,
        }

        asyncio.run(self.engine._handle_extreme_rsi_vetoed_stop(
            "2026-05-13 10:30:03",
            hsi_price=25990.0,
            rsi=10.0,
            diff=-60.0,
            actual_pnl=-1200.0,
        ))

        self.engine.trader.modify_order.assert_called_once_with("TAKE-PROFIT-1", 0.023, 200000)
        self.assertTrue(self.engine.stop_loss_order_sent)
        self.assertFalse(self.engine.extreme_rsi_stop_veto_active)
        self.assertIn("硬止损触发", self.records[0].message)
        self.assertIn("不再判断RSI", self.records[0].message)

    def test_rearm_clears_veto_without_sending_stop_order(self):
        self.engine.extreme_rsi_stop_veto_active = True
        self.engine.extreme_rsi_stop_veto_price = 0.025
        self.engine.extreme_rsi_stop_hard_price = 0.023
        self.engine.extreme_rsi_stop_rearm_price = 0.026
        self.engine.data_source.get_security_snapshot.return_value = {
            "bid_price": 0.026,
            "ask_price": 0.027,
            "price_spread": 0.001,
        }

        asyncio.run(self.engine._handle_extreme_rsi_vetoed_stop(
            "2026-05-13 10:30:06",
            hsi_price=26010.0,
            rsi=24.0,
            diff=-40.0,
            actual_pnl=-800.0,
        ))

        self.engine.trader.modify_order.assert_not_called()
        self.engine.trader.place_order.assert_not_called()
        self.assertFalse(self.engine.extreme_rsi_stop_veto_active)
        self.assertFalse(self.engine.stop_loss_order_sent)
        self.assertIn("已重新武装", self.records[0].message)

    def test_repeated_stop_chase_snapshot_failure_logs_once_but_keeps_retrying(self):
        self.engine.stop_loss_order_sent = True
        self.engine.data_source.get_security_snapshot = MagicMock(return_value=None)
        order = {
            "order_status": "SUBMITTED",
            "qty": 200000,
            "dealt_qty": 0,
        }

        asyncio.run(self.engine._chase_stop_loss_exit_order(
            "2026-05-14 11:23:07",
            hsi_price=26442.89,
            rsi=40.03,
            order=order,
        ))
        asyncio.run(self.engine._chase_stop_loss_exit_order(
            "2026-05-14 11:23:11",
            hsi_price=26442.57,
            rsi=39.77,
            order=order,
        ))

        self.assertEqual(self.engine.data_source.get_security_snapshot.call_count, 2)
        self.assertEqual(len(self.records), 1)
        self.assertIn("止损卖单追价失败", self.records[0].message)


if __name__ == "__main__":
    unittest.main()
