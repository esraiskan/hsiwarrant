import asyncio
from datetime import datetime, timedelta
import unittest

import pandas as pd

from models import PositionType
from strategy import (
    CUM_TREND_ENTRY_MODE,
    CUM_TREND_SAME_SIDE_STOP_COOLDOWN_SECONDS,
    HSIStrategyEngine,
)
from unittest.mock import AsyncMock, MagicMock


def _df(slopes=None, closes=None, rsi=66.0):
    if slopes is None:
        slopes = [0.2, 0.3]
    if closes is None:
        closes = [100, 108, 116, 124, 132, 140, 141]
    rows = []
    for i, close in enumerate(closes):
        rows.append({
            "open": close - 1,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "volume": 1000,
            "RSI": rsi,
            "VWAP": close - 0.5,
            "VWAP_SLOPE": 0.1,
            "VOL_MA": 900,
        })
    # Last row represents the live/incomplete candle; slope checks use the two completed rows before it.
    rows[-3]["VWAP_SLOPE"] = slopes[0]
    rows[-2]["VWAP_SLOPE"] = slopes[1]
    return pd.DataFrame(
        rows,
        index=pd.date_range("2026-05-12 09:40:00", periods=len(rows), freq="min"),
    )


class CumTrendEntryControlsTest(unittest.TestCase):
    def setUp(self):
        self.engine = HSIStrategyEngine()
        self.engine._save_runtime_state = lambda: None

    def test_completed_cum5_uses_last_completed_kline(self):
        df = _df(closes=[100, 110, 120, 130, 140, 150, 999])

        self.assertEqual(self.engine._completed_cum5(df), 50.0)

    def test_bull_requires_two_completed_positive_vwap_slopes(self):
        valid_df = _df(slopes=[0.1, 0.2])
        invalid_df = _df(slopes=[0.1, -0.2])

        self.assertTrue(self.engine._completed_vwap_slopes_confirm(valid_df, PositionType.BULL))
        self.assertFalse(self.engine._completed_vwap_slopes_confirm(invalid_df, PositionType.BULL))

    def test_bear_requires_two_completed_negative_vwap_slopes(self):
        valid_df = _df(slopes=[-0.1, -0.2])
        invalid_df = _df(slopes=[-0.1, 0.2])

        self.assertTrue(self.engine._completed_vwap_slopes_confirm(valid_df, PositionType.BEAR))
        self.assertFalse(self.engine._completed_vwap_slopes_confirm(invalid_df, PositionType.BEAR))

    def test_same_completed_kline_same_side_is_deduped(self):
        key_time = "2026-05-12 09:45:00"
        self.assertFalse(self.engine._is_duplicate_cum_trend_signal(key_time, PositionType.BULL))

        self.engine._mark_cum_trend_signal_submitted(key_time, PositionType.BULL)

        self.assertTrue(self.engine._is_duplicate_cum_trend_signal(key_time, PositionType.BULL))
        self.assertFalse(self.engine._is_duplicate_cum_trend_signal(key_time, PositionType.BEAR))
        self.assertFalse(self.engine._is_duplicate_cum_trend_signal("2026-05-12 09:46:00", PositionType.BULL))

    def test_bull_rsi_must_pull_back_to_68_or_below(self):
        df = _df(slopes=[0.1, 0.2])

        self.assertIn(
            "等待回踩",
            ";".join(self.engine._cum_trend_entry_block_reasons(
                PositionType.BULL, "2026-05-12 09:45:00", 70.0, df
            )),
        )
        self.assertIn(
            "等待回踩",
            ";".join(self.engine._cum_trend_entry_block_reasons(
                PositionType.BULL, "2026-05-12 09:45:00", 69.0, df
            )),
        )
        self.assertEqual(
            self.engine._cum_trend_entry_block_reasons(
                PositionType.BULL, "2026-05-12 09:45:00", 68.0, df
            ),
            [],
        )

    def test_bear_rsi_must_rebound_to_32_or_above(self):
        df = _df(slopes=[-0.1, -0.2])

        self.assertIn(
            "等待反弹",
            ";".join(self.engine._cum_trend_entry_block_reasons(
                PositionType.BEAR, "2026-05-12 09:45:00", 30.0, df
            )),
        )
        self.assertIn(
            "等待反弹",
            ";".join(self.engine._cum_trend_entry_block_reasons(
                PositionType.BEAR, "2026-05-12 09:45:00", 31.0, df
            )),
        )
        self.assertEqual(
            self.engine._cum_trend_entry_block_reasons(
                PositionType.BEAR, "2026-05-12 09:45:00", 32.0, df
            ),
            [],
        )

    def test_cum_trend_stop_cooldown_only_blocks_same_side(self):
        self.engine.last_cum_trend_stop_position = PositionType.BULL
        self.engine.last_cum_trend_stop_time = datetime.now() - timedelta(seconds=30)

        self.assertGreater(self.engine._cum_trend_stop_cooldown_remaining(PositionType.BULL), 0)
        self.assertEqual(self.engine._cum_trend_stop_cooldown_remaining(PositionType.BEAR), 0)

    def test_cum_trend_stop_cooldown_expires_after_180_seconds(self):
        self.engine.last_cum_trend_stop_position = PositionType.BULL
        self.engine.last_cum_trend_stop_time = datetime.now() - timedelta(
            seconds=CUM_TREND_SAME_SIDE_STOP_COOLDOWN_SECONDS + 1
        )

        self.assertEqual(self.engine._cum_trend_stop_cooldown_remaining(PositionType.BULL), 0)

    def test_only_cum_trend_loss_marks_stop_cooldown(self):
        self.engine._mark_cum_trend_stop_if_needed(CUM_TREND_ENTRY_MODE, PositionType.BULL, -400.0)

        self.assertEqual(self.engine.last_cum_trend_stop_position, PositionType.BULL)
        self.assertIsNotNone(self.engine.last_cum_trend_stop_time)

        self.engine.last_cum_trend_stop_position = PositionType.NONE
        self.engine.last_cum_trend_stop_time = None
        self.engine._mark_cum_trend_stop_if_needed("放量动能", PositionType.BULL, -400.0)

        self.assertEqual(self.engine.last_cum_trend_stop_position, PositionType.NONE)
        self.assertIsNone(self.engine.last_cum_trend_stop_time)

    def test_degraded_cumtrend_refreshes_to_filled_order_before_cancel(self):
        self.engine.entry_mode = CUM_TREND_ENTRY_MODE
        self.engine.pending_buy_order_id = "ORDER-1"
        self.engine.pending_entry_side = PositionType.BULL
        self.engine.position = PositionType.NONE
        self.engine.momentum_entry_trigger_price = 26570.8
        self.engine._monitor_entry_order = AsyncMock(return_value=None)
        self.engine.trader.get_order = MagicMock(return_value={"order_status": "FILLED_ALL"})
        self.engine.trader.cancel_order = MagicMock()

        cancelled = asyncio.run(self.engine._cancel_degraded_cum_trend_entry(
            "2026-05-12 09:56:04",
            hsi_price=26566.54,
            rsi=66.51,
            curr_slope=-0.01,
            cum5=23.4,
        ))

        self.assertTrue(cancelled)
        self.engine._monitor_entry_order.assert_awaited_once()
        self.engine.trader.cancel_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
