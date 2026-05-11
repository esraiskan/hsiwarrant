import unittest

from strategy import (
    EXTREME_VOLUME_SURGE_MULTIPLIER,
    MOMENTUM_VOLUME_SURGE_MULTIPLIER,
    VERY_EXTREME_AVG_VOLUME_MULTIPLIER,
    VERY_EXTREME_RSI_OVERBOUGHT,
    VERY_EXTREME_RSI_OVERSOLD,
    VERY_EXTREME_VOLUME_SURGE_MULTIPLIER,
)


class VolumeThresholdTest(unittest.TestCase):
    def test_extreme_and_momentum_use_separate_volume_thresholds(self):
        self.assertEqual(EXTREME_VOLUME_SURGE_MULTIPLIER, 1.4)
        self.assertEqual(VERY_EXTREME_VOLUME_SURGE_MULTIPLIER, 1.25)
        self.assertEqual(VERY_EXTREME_AVG_VOLUME_MULTIPLIER, 1.0)
        self.assertEqual(MOMENTUM_VOLUME_SURGE_MULTIPLIER, 1.5)
        self.assertEqual(VERY_EXTREME_RSI_OVERBOUGHT, 85)
        self.assertEqual(VERY_EXTREME_RSI_OVERSOLD, 16)


if __name__ == "__main__":
    unittest.main()
