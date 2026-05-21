import unittest

import pandas as pd

from models import BacktestRequest, BacktestStrategySelection
from backtest_service import (
    _DivergencePoint,
    _Runtime,
    _RsiDivergenceState,
    _completed_extreme_signal,
    _detect_entry,
    _detect_rsi_divergence,
    resolve_backtest_range,
)
from models import PositionType


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


def _large_body_extreme_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "open": 100.0,
                "high": 102.0,
                "low": 98.0,
                "close": 100.0,
                "volume": 1000.0,
                "RSI": 50.0,
                "VWAP": 100.0,
                "VWAP_SLOPE": -0.2,
                "VOL_MA": 1000.0,
            },
            {
                "open": 100.0,
                "high": 170.0,
                "low": 95.0,
                "close": 160.0,
                "volume": 2000.0,
                "RSI": 86.0,
                "VWAP": 101.0,
                "VWAP_SLOPE": -0.3,
                "VOL_MA": 1000.0,
            },
        ],
        index=pd.to_datetime(["2026-05-14 13:45:00", "2026-05-14 13:46:00"]),
    )


def _single_15m_df() -> pd.DataFrame:
    return pd.DataFrame(
        [{"open": 160.0, "high": 170.0, "low": 95.0, "close": 150.0}],
        index=pd.to_datetime(["2026-05-14 13:45:00"]),
    )


class BacktestServiceTest(unittest.TestCase):
    def test_month_mode_resolves_to_full_selected_month_span(self):
        payload = BacktestRequest(
            period_mode="months",
            months=["2026-04", "2026-03"],
            selection=BacktestStrategySelection(strategies=["normal"]),
        )

        start, end = resolve_backtest_range(payload)

        self.assertEqual(start.isoformat(), "2026-03-01")
        self.assertEqual(end.isoformat(), "2026-04-30")

    def test_month_mode_allows_at_most_two_months(self):
        payload = BacktestRequest(
            period_mode="months",
            months=["2026-01", "2026-02", "2026-03"],
            selection=BacktestStrategySelection(strategies=["normal"]),
        )

        with self.assertRaisesRegex(ValueError, "最多选择 2 个月份"):
            resolve_backtest_range(payload)

    def test_date_range_allows_62_day_span(self):
        payload = BacktestRequest(
            period_mode="date_range",
            date_start="2026-03-01",
            date_end="2026-05-02",
            selection=BacktestStrategySelection(strategies=["normal"]),
        )

        start, end = resolve_backtest_range(payload)

        self.assertEqual(start.isoformat(), "2026-03-01")
        self.assertEqual(end.isoformat(), "2026-05-02")

    def test_date_range_rejects_more_than_62_days(self):
        payload = BacktestRequest(
            period_mode="date_range",
            date_start="2026-03-01",
            date_end="2026-05-03",
            selection=BacktestStrategySelection(strategies=["normal"]),
        )

        with self.assertRaisesRegex(ValueError, "最多支持 62 日"):
            resolve_backtest_range(payload)

    def test_rsi_bullish_divergence_requires_three_lower_prices_and_higher_rsi(self):
        df = _divergence_df(
            closes=[100, 96, 94, 92, 90, 88, 86, 84, 82, 80, 84, 86, 83, 80, 78, 74, 70, 66, 72, 75, 72, 68, 64, 56, 62],
            rsis=[50, 45, 40, 35, 30, 24, 22, 18, 14, 9, 18, 25, 24, 23, 22, 21, 20, 20, 31, 35, 32, 29, 27, 24, 38],
        )
        state = _RsiDivergenceState(
            bull_lows=[
                _DivergencePoint(df.index[9], 78, 9),
                _DivergencePoint(df.index[16], 70, 18),
            ],
            bear_highs=[],
            used_bull_keys=set(),
            used_bear_keys=set(),
            used_bull_c_times=set(),
            used_bear_c_times=set(),
        )

        entry = _detect_rsi_divergence(df, 24, state)

        self.assertIsNotNone(entry)
        side, mode, branch, desc = entry
        self.assertEqual(side, PositionType.BULL)
        self.assertEqual(mode, "RSI背离")
        self.assertEqual(branch, "bullish_divergence")
        self.assertIn("连续底背离", desc)

    def test_very_extreme_pullback_allows_large_k_body(self):
        payload = BacktestRequest(
            period_mode="date_range",
            date_start="2026-05-14",
            date_end="2026-05-14",
            selection=BacktestStrategySelection(
                strategies=["extreme"],
                extreme_branches=["b2_very_extreme_pullback"],
            ),
        )

        entry = _detect_entry(
            _large_body_extreme_df(),
            _single_15m_df(),
            1,
            payload,
            _Runtime(request=payload, warnings=set()),
            pd.DataFrame(),
            "",
            _RsiDivergenceState.create(),
        )

        self.assertIsNotNone(entry)
        side, mode, branch, desc = entry
        self.assertEqual(side, PositionType.BEAR)
        self.assertEqual(mode, "极度超买")
        self.assertEqual(branch, "b2_very_extreme_pullback")
        self.assertIn("阳线涨60.0点", desc)

    def test_volume_extreme_allows_large_k_body(self):
        payload = BacktestRequest(
            period_mode="date_range",
            date_start="2026-05-14",
            date_end="2026-05-14",
            selection=BacktestStrategySelection(
                strategies=["extreme"],
                extreme_branches=["b1_volume_extreme"],
            ),
        )

        entry = _detect_entry(
            _large_body_extreme_df(),
            _single_15m_df(),
            1,
            payload,
            _Runtime(request=payload, warnings=set()),
            pd.DataFrame(),
            "",
            _RsiDivergenceState.create(),
        )

        self.assertIsNotNone(entry)
        side, mode, branch, desc = entry
        self.assertEqual(side, PositionType.BEAR)
        self.assertEqual(mode, "极度超买")
        self.assertEqual(branch, "b1_volume_extreme")
        self.assertIn("阳线涨60.0点", desc)

    def test_completed_extreme_allows_large_k_body(self):
        payload = BacktestRequest(
            period_mode="date_range",
            date_start="2026-05-14",
            date_end="2026-05-14",
            selection=BacktestStrategySelection(
                strategies=["extreme"],
                extreme_branches=["b3_completed_k"],
            ),
        )
        df = _large_body_extreme_df()
        current = df.iloc[1].copy()
        current["close"] = 158.0
        current["RSI"] = 82.0

        entry = _completed_extreme_signal(df.iloc[1], current, payload)

        self.assertIsNotNone(entry)
        side, mode, branch, desc = entry
        self.assertEqual(side, PositionType.BEAR)
        self.assertEqual(mode, "极度超买")
        self.assertEqual(branch, "b3_completed_k")
        self.assertIn("阳线涨60.0点", desc)


if __name__ == "__main__":
    unittest.main()
