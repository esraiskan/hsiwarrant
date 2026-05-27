"""Tests for the CBBC offline research script (cbbc-magnet-signal task 11.5)."""
from __future__ import annotations

import io
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def _ensure(name: str) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


def _try_real_import(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _stub_if_unavailable(name: str, attrs: dict) -> None:
    if _try_real_import(name):
        return
    mod = _ensure(name)
    for k, v in attrs.items():
        setattr(mod, k, v)


_stub_if_unavailable("pandas", {"DataFrame": type("DataFrame", (), {})})
_stub_if_unavailable("numpy", {"nan": float("nan")})

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cbbc_research as research  # noqa: E402
from cbbc_research import (  # noqa: E402
    DayAggregate,
    NextDayHsiTrace,
    compute_correlation,
    compute_grouped_event_stats,
    evaluate_go_no_go,
    write_research_artifacts,
)


_HK = ZoneInfo("Asia/Hong_Kong")


def _agg(d, *, bias=0.1, near_money=False):
    return DayAggregate(
        trade_date=d,
        avg_magnet_bias=bias,
        avg_nearest_dense_band_distance_pts=80.0,
        sample_count=20,
        is_intraday_new_listing_near_money_day=near_money,
    )


def _trace_factory(deltas: dict[date, tuple[float, float, float]]):
    """Build a NextDayHsiLoader from a {trade_date: (close_d, high_d1, low_d1)} dict.

    ``close_d_plus_1`` is set to ``high_d1`` to model an "up" day; tests can
    override by supplying their own structure.
    """

    def loader(d: date):
        if d not in deltas:
            return None
        close_d, hi, lo = deltas[d]
        # Default close_d_plus_1 = hi to make an "up" day; tests that need a
        # down-day will pass hi < close_d.
        close_d1 = hi if hi != close_d else lo
        return NextDayHsiTrace(
            trade_date=d,
            close_d=close_d,
            close_d_plus_1=close_d1,
            max_high_d_plus_1=hi,
            min_low_d_plus_1=lo,
        )

    return loader


# --------------------------------------------------------------------------- #
# CLI parameter validation (R6.1 / R6.11)
# --------------------------------------------------------------------------- #


class CliValidationTest(unittest.TestCase):
    def test_invalid_date_format_exits_non_zero_no_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stderr = io.StringIO()
            with self.assertRaises(SystemExit) as ctx, redirect_stderr(stderr):
                research.main([
                    "--start-date", "not-a-date",
                    "--end-date", "2025-01-31",
                    "--decay-points", "300",
                    "--dense-band-threshold-pts", "150",
                ])
            self.assertNotEqual(ctx.exception.code, 0)
            # No files should have been created.
            self.assertEqual(list(tmp_path.iterdir()), [])
            self.assertIn("--start-date", stderr.getvalue())


    def test_decay_out_of_range_exits_non_zero(self) -> None:
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stderr(stderr):
            research.main([
                "--start-date", "2025-01-01",
                "--end-date", "2025-01-31",
                "--decay-points", "9999",
                "--dense-band-threshold-pts", "150",
            ])
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("--decay-points", stderr.getvalue())

    def test_start_after_end_returns_non_zero(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            rc = research.main([
                "--start-date", "2025-02-01",
                "--end-date", "2025-01-31",
                "--decay-points", "300",
                "--dense-band-threshold-pts", "150",
            ])
        self.assertNotEqual(rc, 0)
        self.assertIn("start_date must be <= end_date", stderr.getvalue())


# --------------------------------------------------------------------------- #
# Correlation / Grouped stats / Go-no-go (R6.5 / R6.6 / R6.7)
# --------------------------------------------------------------------------- #


class CorrelationTest(unittest.TestCase):
    def test_strong_positive_correlation(self) -> None:
        # Pair 5 days where positive bias precedes "up" next-day.
        days = [date(2025, 1, d) for d in range(2, 7)]
        biases = [0.5, 0.3, -0.3, -0.5, 0.4]
        # Build traces where direction matches sign(bias).
        deltas = {
            d: (20000.0, 20100.0 if biases[i] > 0 else 19900.0,
                19900.0 if biases[i] > 0 else 19800.0)
            for i, d in enumerate(days)
        }
        loader = _trace_factory(deltas)
        aggs = [_agg(d, bias=biases[i]) for i, d in enumerate(days)]
        result = compute_correlation(aggs, loader)
        self.assertEqual(result.n, 5)
        self.assertGreater(result.pearson_r, 0.9)

    def test_no_data_returns_zero_correlation(self) -> None:
        result = compute_correlation([], lambda d: None)
        self.assertEqual(result.n, 0)
        self.assertEqual(result.pearson_r, 0.0)


class GroupedEventStatsTest(unittest.TestCase):
    def test_buckets_split_correctly(self) -> None:
        days = [date(2025, 1, d) for d in range(2, 8)]
        # Three near-money days, three ordinary days.
        flags = [True, True, True, False, False, False]
        aggs = [_agg(days[i], near_money=flags[i]) for i in range(6)]
        deltas = {
            d: (20000.0, 20100.0 + i * 10, 19900.0 - i * 10)
            for i, d in enumerate(days)
        }
        loader = _trace_factory(deltas)
        out = compute_grouped_event_stats(aggs, loader)
        self.assertEqual(out[True].n, 3)
        self.assertEqual(out[False].n, 3)
        # p75 should be >= median for both.
        for stat in out.values():
            self.assertGreaterEqual(stat.p75_max_favorable_pts, stat.median_max_favorable_pts)
            self.assertGreaterEqual(stat.p75_max_adverse_pts, stat.median_max_adverse_pts)


class GoNoGoTest(unittest.TestCase):
    def test_below_threshold_returns_false(self) -> None:
        aggs = [_agg(date(2025, 1, 2) + timedelta(days=i)) for i in range(5)]
        self.assertFalse(evaluate_go_no_go(aggs))

    def test_at_threshold_returns_true(self) -> None:
        aggs = [_agg(date(2025, 1, 2) + timedelta(days=i)) for i in range(60)]
        self.assertTrue(evaluate_go_no_go(aggs))


class InsufficientTradingDaysExitTest(unittest.TestCase):
    def test_emit_exits_non_zero(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            rc = research._emit_insufficient_trading_days_and_exit(42)
        self.assertNotEqual(rc, 0)
        self.assertIn("insufficient_trading_days", stderr.getvalue())


# --------------------------------------------------------------------------- #
# Reproducibility header + CSV/MD output (R6.8 / R6.9 / R6.10)
# --------------------------------------------------------------------------- #


class WriteArtifactsTest(unittest.TestCase):
    def test_writes_csv_and_md_with_header(self) -> None:
        from cbbc_research import CorrelationResult, GroupedEventStat
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            aggs = [
                _agg(date(2025, 1, 2), bias=0.3, near_money=True),
                _agg(date(2025, 1, 3), bias=-0.1, near_money=False),
            ]
            corr = CorrelationResult(
                n=2, pearson_r=0.5, pearson_p=0.5, spearman_r=0.5, spearman_p=0.5
            )
            grouped = {
                True: GroupedEventStat(
                    n=1, median_max_favorable_pts=80.0, p75_max_favorable_pts=80.0,
                    median_max_adverse_pts=40.0, p75_max_adverse_pts=40.0,
                ),
                False: GroupedEventStat(
                    n=1, median_max_favorable_pts=20.0, p75_max_favorable_pts=20.0,
                    median_max_adverse_pts=10.0, p75_max_adverse_pts=10.0,
                ),
            }
            artifacts = write_research_artifacts(
                aggregates=aggs,
                correlation=corr,
                grouped=grouped,
                decay_points=300,
                dense_band_threshold_pts=150,
                dense_band_pull_share=0.40,
                start_date=date(2025, 1, 1),
                end_date=date(2025, 1, 31),
                snapshot_hashes=[("outstanding_20250102.parquet", "abc123")],
                started_at_hk=datetime(2025, 1, 31, 18, 0),
                finished_at_hk=datetime(2025, 1, 31, 18, 5),
                output_root=tmp_path,
            )
            self.assertTrue(artifacts.csv_path.exists())
            self.assertTrue(artifacts.md_path.exists())

            csv_content = artifacts.csv_path.read_text(encoding="utf-8")
            self.assertIn("# decay_points: 300", csv_content)
            self.assertIn("# dense_band_threshold_pts: 150", csv_content)
            self.assertIn("# cbbc_dense_band_pull_share: 0.4000", csv_content)
            self.assertIn("# start_date: 2025-01-01", csv_content)
            self.assertIn("sha256=abc123", csv_content)
            # CSV must NOT contain account / order / position / pnl fields.
            for forbidden in ("entry_price", "pnl_hkd", "position", "order_id"):
                self.assertNotIn(forbidden, csv_content)

            md_content = artifacts.md_path.read_text(encoding="utf-8")
            self.assertIn("Pearson r:", md_content)
            self.assertIn("near_money_day", md_content)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
