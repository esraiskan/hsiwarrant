import unittest

from trend_filter import get_cum_trend_filter_reasons


class CumTrendFilterTest(unittest.TestCase):
    def assert_allowed(self, direction, ratio):
        self.assertEqual(get_cum_trend_filter_reasons(direction, ratio), [])

    def assert_blocked(self, direction, ratio):
        self.assertNotEqual(get_cum_trend_filter_reasons(direction, ratio), [])

    def test_bull_cumtrend_requires_bullish_breadth(self):
        self.assert_allowed("bull", 1.6)
        self.assert_blocked("bull", 1.0)
        self.assert_blocked("bull", 0.4)

    def test_bear_cumtrend_requires_bearish_breadth(self):
        self.assert_allowed("bear", 0.8)
        self.assert_blocked("bear", 1.2)
        self.assert_blocked("bear", 4.06)

    def test_missing_breadth_blocks_cumtrend(self):
        self.assert_blocked("bull", None)
        self.assert_blocked("bear", None)


if __name__ == "__main__":
    unittest.main()
