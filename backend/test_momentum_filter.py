import unittest

from momentum_filter import get_momentum_filter_reasons


class MomentumFilterTest(unittest.TestCase):
    def assert_allowed(self, direction, rsi, ratio):
        self.assertEqual(get_momentum_filter_reasons(direction, rsi, ratio), [])

    def assert_blocked(self, direction, rsi, ratio):
        self.assertNotEqual(get_momentum_filter_reasons(direction, rsi, ratio), [])

    def test_bear_momentum_blocks_low_rsi_or_bullish_breadth(self):
        self.assert_blocked("bear", 21, 4.17)
        self.assert_allowed("bear", 35, 1.2)
        self.assert_blocked("bear", 35, 3.0)

    def test_bull_momentum_blocks_high_rsi_or_bearish_breadth(self):
        self.assert_blocked("bull", 76, 4.0)
        self.assert_blocked("bull", 60, 0.4)
        self.assert_allowed("bull", 60, 1.0)

    def test_missing_breadth_only_uses_rsi_filter(self):
        self.assert_allowed("bear", 35, None)
        self.assert_allowed("bull", 60, None)
        self.assert_blocked("bear", 21, None)
        self.assert_blocked("bull", 76, None)


if __name__ == "__main__":
    unittest.main()
