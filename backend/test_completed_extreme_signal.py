import unittest

from models import PositionType
from strategy import HSIStrategyEngine


class CompletedExtremeSignalTest(unittest.TestCase):
    def setUp(self):
        self.engine = HSIStrategyEngine()
        self.engine.rsi_oversold = 26
        self.engine.rsi_overbought = 72

    def test_completed_extreme_overbought_allows_1442_shape(self):
        row = {
            "RSI": 85.11,
            "open": 26374.91,
            "high": 26392.00,
            "low": 26374.91,
            "close": 26390.80,
            "volume": 906_458_000.0,
            "VOL_MA": 640_191_500.0,
        }
        side, message = self.engine._completed_extreme_signal(
            row, current_price=26391.82, current_rsi=81.08
        )
        self.assertEqual(side, PositionType.BEAR)
        self.assertIn("上一根完成K触发", message)

    def test_completed_extreme_overbought_blocks_large_adverse_move(self):
        row = {
            "RSI": 85.11,
            "open": 26374.91,
            "high": 26392.00,
            "low": 26374.91,
            "close": 26390.80,
            "volume": 906_458_000.0,
            "VOL_MA": 640_191_500.0,
        }
        side, _ = self.engine._completed_extreme_signal(
            row, current_price=26400.00, current_rsi=82.0
        )
        self.assertEqual(side, PositionType.NONE)

    def test_completed_extreme_oversold_blocks_large_favorable_move(self):
        row = {
            "RSI": 13.5,
            "open": 26300.0,
            "high": 26300.0,
            "low": 26279.0,
            "close": 26280.0,
            "volume": 800_000_000.0,
            "VOL_MA": 500_000_000.0,
        }
        side, _ = self.engine._completed_extreme_signal(
            row, current_price=26295.0, current_rsi=20.0
        )
        self.assertEqual(side, PositionType.NONE)

    def test_completed_extreme_overbought_blocks_below_average_volume(self):
        row = {
            "RSI": 85.5,
            "open": 26460.89,
            "high": 26470.49,
            "low": 26460.89,
            "close": 26467.23,
            "volume": 792_000_000.0,
            "VOL_MA": 800_000_000.0,
        }
        side, _ = self.engine._completed_extreme_signal(
            row, current_price=26467.00, current_rsi=85.0
        )
        self.assertEqual(side, PositionType.NONE)

    def test_completed_extreme_overbought_allows_dynamic_pullback(self):
        row = {
            "RSI": 85.5,
            "open": 26460.89,
            "high": 26470.49,
            "low": 26460.89,
            "close": 26467.23,
            "volume": 840_000_000.0,
            "VOL_MA": 800_000_000.0,
        }
        side, message = self.engine._completed_extreme_signal(
            row, current_price=26467.49, current_rsi=85.0
        )
        self.assertEqual(side, PositionType.BEAR)
        self.assertIn("非常极端RSI均量回落", message)

    def test_completed_extreme_overbought_blocks_dynamic_without_pullback(self):
        row = {
            "RSI": 85.5,
            "open": 26460.89,
            "high": 26470.49,
            "low": 26460.89,
            "close": 26467.23,
            "volume": 840_000_000.0,
            "VOL_MA": 800_000_000.0,
        }
        side, _ = self.engine._completed_extreme_signal(
            row, current_price=26468.00, current_rsi=85.0
        )
        self.assertEqual(side, PositionType.NONE)

    def test_completed_extreme_overbought_blocks_dynamic_without_very_extreme_rsi(self):
        row = {
            "RSI": 80.0,
            "open": 26460.89,
            "high": 26470.49,
            "low": 26460.89,
            "close": 26467.23,
            "volume": 840_000_000.0,
            "VOL_MA": 800_000_000.0,
        }
        side, _ = self.engine._completed_extreme_signal(
            row, current_price=26467.49, current_rsi=80.0
        )
        self.assertEqual(side, PositionType.NONE)

    def test_completed_extreme_overbought_allows_normal_volume_without_pullback(self):
        row = {
            "RSI": 84.0,
            "open": 26460.89,
            "high": 26470.49,
            "low": 26460.89,
            "close": 26467.23,
            "volume": 1_128_000_000.0,
            "VOL_MA": 800_000_000.0,
        }
        side, message = self.engine._completed_extreme_signal(
            row, current_price=26468.00, current_rsi=83.0
        )
        self.assertEqual(side, PositionType.BEAR)
        self.assertIn("放量动能触发", message)


if __name__ == "__main__":
    unittest.main()
