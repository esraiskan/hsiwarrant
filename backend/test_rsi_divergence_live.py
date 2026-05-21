import unittest

import pandas as pd

from models import PositionType
from strategy import HSIStrategyEngine, RsiDivergencePoint


def _divergence_df(closes: list[float], rsis: list[float]) -> pd.DataFrame:
    rows = []
    for close, rsi in zip(closes, rsis):
        rows.append({
            "open": close + 1,
            "high": close + 3,
            "low": close - 3,
            "close": close,
            "volume": 1000,
            "RSI": rsi,
            "VWAP": close + 5,
            "VWAP_SLOPE": -1.0,
            "VOL_MA": 900,
        })
    rows[0]["open"] = closes[0] + 220
    rows[0]["high"] = rows[0]["open"]
    return pd.DataFrame(
        rows,
        index=pd.date_range("2026-05-14 09:40:00", periods=len(rows), freq="min"),
    )


class LiveRsiDivergenceTest(unittest.TestCase):
    def test_detects_bullish_rsi_divergence_once_per_completed_pivot(self):
        engine = HSIStrategyEngine()
        df = _divergence_df(
            closes=[100, 96, 94, 92, 90, 88, 86, 84, 82, 80, 84, 86, 83, 80, 78, 74, 70, 66, 72, 75, 72, 68, 64, 56, 62],
            rsis=[50, 45, 40, 35, 30, 24, 22, 18, 14, 9, 18, 25, 24, 23, 22, 21, 20, 20, 31, 35, 32, 29, 27, 24, 38],
        )
        engine.rsi_divergence_bull_lows = [
            RsiDivergencePoint(df.index[9], 78, 9),
            RsiDivergencePoint(df.index[16], 70, 18),
        ]
        engine.rsi_divergence_day = "2026-05-14"

        signal = engine._detect_rsi_divergence_signal(df)

        self.assertIsNotNone(signal)
        side, branch, desc = signal
        self.assertEqual(side, PositionType.BULL)
        self.assertEqual(branch, "bullish_divergence")
        self.assertIn("连续底背离", desc)
        self.assertIsNone(engine._detect_rsi_divergence_signal(df))


if __name__ == "__main__":
    unittest.main()
