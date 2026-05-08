import unittest

from trend_filter import (
    get_cum_trend_boundary_filter_reasons,
    get_cum_trend_filter_reasons,
)


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

    def test_strong_cumtrend_skips_boundary_filter(self):
        self.assertEqual(
            get_cum_trend_boundary_filter_reasons(
                "bear",
                -45.0,
                55.0,
                26320.0,
                26315.0,
                26310.0,
                26380.0,
                20.0,
                79.0,
            ),
            [],
        )

    def test_boundary_bear_blocks_near_recent_low(self):
        reasons = get_cum_trend_boundary_filter_reasons(
            "bear",
            -31.2,
            30.6,
            26310.0,
            26298.9,
            26293.6,
            26354.0,
            20.0,
            79.0,
        )
        self.assertIn("near recent low gap=5.3", reasons)

    def test_boundary_bear_allows_confirmed_extension(self):
        self.assertEqual(
            get_cum_trend_boundary_filter_reasons(
                "bear",
                -35.0,
                32.0,
                26325.0,
                26315.0,
                26300.0,
                26370.0,
                20.0,
                79.0,
            ),
            [],
        )

    def test_boundary_bull_blocks_near_recent_high(self):
        reasons = get_cum_trend_boundary_filter_reasons(
            "bull",
            32.0,
            68.0,
            26320.0,
            26328.0,
            26280.0,
            26332.0,
            20.0,
            79.0,
        )
        self.assertIn("near recent high gap=4.0", reasons)


if __name__ == "__main__":
    unittest.main()
