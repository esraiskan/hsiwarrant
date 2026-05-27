"""Unit tests for ``cbbc_calculator`` (cbbc-magnet-signal task 3.3).

Covers:
- bias 截断、跳过非法记录
- stale 标志、decay 越界拒绝
- Δ>5 重算、零总 pull 边界
"""
from __future__ import annotations

import math
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
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

from cbbc_calculator import (  # noqa: E402
    HistBucket,
    MagnetEngine,
    MagnetEngineError,
    MagnetResult,
    compute_magnet,
)
from cbbc_storage import CbbcRecord, CbbcSnapshot  # noqa: E402

_HK = ZoneInfo("Asia/Hong_Kong")


def _hk(*args) -> datetime:
    return datetime(*args, tzinfo=_HK)


def _stable_clock(t: datetime):
    """Return a clock callable that always reports ``t``."""
    return lambda: t


_BASE_DATE = date(2025, 1, 2)


def _record(
    code: str,
    direction: str,
    call_level: float,
    *,
    outstanding: float = 1_000_000.0,
    er_ratio: float = 10000.0,
    listing_d: date = date(2024, 6, 1),
    maturity_d: date = date(2026, 12, 31),
) -> CbbcRecord:
    return CbbcRecord(
        issuer="HSBC",
        code=code,
        call_level=call_level,
        outstanding_shares=outstanding,
        er_ratio=er_ratio,
        direction=direction,  # type: ignore[arg-type]
        listing_date=listing_d,
        maturity_date=maturity_d,
        underlying="HSI",
        snapshot_date=_BASE_DATE,
    )


def _snapshot(*records: CbbcRecord, snapshot_date: date = _BASE_DATE) -> CbbcSnapshot:
    return CbbcSnapshot(snapshot_date=snapshot_date, records=tuple(records))


# --------------------------------------------------------------------------- #
# compute_magnet (task 3.1)
# --------------------------------------------------------------------------- #


class ComputeMagnetTest(unittest.TestCase):
    def test_empty_snapshot_returns_neutral_result(self) -> None:
        result = compute_magnet(
            _snapshot(), 20000.0, 300.0,
            generated_at_hk=datetime(2025, 1, 2, 14, 30),
        )
        self.assertEqual(result.magnet_bias, 0.0)
        self.assertEqual(result.magnet_pull_bull, 0.0)
        self.assertEqual(result.magnet_pull_bear, 0.0)
        self.assertEqual(result.record_count, 0)
        self.assertEqual(result.skipped_count, 0)
        self.assertIsNone(result.nearest_bull_distance_pts)
        self.assertIsNone(result.nearest_bear_distance_pts)

    def test_invalid_records_are_skipped(self) -> None:
        # Negative outstanding + NaN call_level should both be skipped.
        bad = _record("HK.50000", "bull", -1.0)
        nan_record = _record("HK.50001", "bull", float("nan"))
        good = _record("HK.50002", "bear", 20100.0)
        result = compute_magnet(
            _snapshot(bad, nan_record, good), 20000.0, 300.0,
            generated_at_hk=datetime(2025, 1, 2, 14, 30),
        )
        self.assertEqual(result.record_count, 1)
        self.assertEqual(result.skipped_count, 2)

    def test_bias_clamped_to_unit_range(self) -> None:
        bear = _record("HK.50100", "bear", 20100.0,
                        outstanding=1e15, er_ratio=1e6)  # huge bear pull
        result = compute_magnet(
            _snapshot(bear), 20000.0, 300.0,
            generated_at_hk=datetime(2025, 1, 2, 14, 30),
        )
        self.assertLessEqual(result.magnet_bias, 1.0)
        self.assertGreaterEqual(result.magnet_bias, -1.0)
        self.assertEqual(result.magnet_bias, 1.0)

    def test_bias_zero_when_pulls_balanced(self) -> None:
        bull = _record("HK.50200", "bull", 19900.0)
        bear = _record("HK.50201", "bear", 20100.0)
        result = compute_magnet(
            _snapshot(bull, bear), 20000.0, 300.0,
            generated_at_hk=datetime(2025, 1, 2, 14, 30),
        )
        self.assertAlmostEqual(result.magnet_bias, 0.0, places=6)

    def test_zero_total_pull_keeps_bias_zero(self) -> None:
        # Records sit beyond decay distance → weight 0 → pulls all 0.
        bull = _record("HK.50300", "bull", 22000.0)
        bear = _record("HK.50301", "bear", 18000.0)
        result = compute_magnet(
            _snapshot(bull, bear), 20000.0, 100.0,
            generated_at_hk=datetime(2025, 1, 2, 14, 30),
        )
        self.assertEqual(result.magnet_pull_bull, 0.0)
        self.assertEqual(result.magnet_pull_bear, 0.0)
        self.assertEqual(result.magnet_bias, 0.0)
        # Nearest distances should still report (these participate even when weight=0).
        self.assertEqual(result.nearest_bull_distance_pts, 2000.0)
        self.assertEqual(result.nearest_bear_distance_pts, 2000.0)

    def test_zero_street_records_excluded_from_nearest(self) -> None:
        """街货量 = 0 的合约 (HKEX 上常见已赎回 / 占位记录) 不应污染
        ``nearest_*_distance_pts`` 计算 — 它们对市场没有真实拉力。"""
        # Bull 1: outstanding=0, very close to spot → must be SKIPPED.
        zombie_bull = _record("HK.99001", "bull", 19999.0, outstanding=0.0)
        # Bear 1: outstanding=0, very close to spot → must be SKIPPED.
        zombie_bear = _record("HK.99002", "bear", 20001.0, outstanding=0.0)
        # Bull 2: real outstanding, further away → should win nearest_bull.
        real_bull = _record("HK.99003", "bull", 19900.0, outstanding=1_000_000.0)
        # Bear 2: real outstanding, further away → should win nearest_bear.
        real_bear = _record("HK.99004", "bear", 20100.0, outstanding=1_000_000.0)
        result = compute_magnet(
            _snapshot(zombie_bull, zombie_bear, real_bull, real_bear),
            20000.0, 300.0,
            generated_at_hk=datetime(2025, 1, 2, 14, 30),
        )
        # Real records (100pt away) win over zombies (1pt away).
        self.assertEqual(result.nearest_bull_distance_pts, 100.0)
        self.assertEqual(result.nearest_bear_distance_pts, 100.0)
        # All four records pass field validation.
        self.assertEqual(result.record_count, 4)
        self.assertEqual(result.skipped_count, 0)
        # Pull only counts the real outstanding contracts.
        self.assertGreater(result.magnet_pull_bull, 0.0)
        self.assertGreater(result.magnet_pull_bear, 0.0)

    def test_today_low_filters_already_killed_bull_contracts(self) -> None:
        """B 兜底:今天 HSI 触及过的牛证收回价已被强制赎回,不应参与计算。"""
        # Spot 25735, today_low touched 25431 (real scenario from 2026-05-26).
        # These bulls have call_level >= 25431 → dead.
        dead_bull_1 = _record("HK.99100", "bull", 25450.0, outstanding=10_000_000.0)
        dead_bull_2 = _record("HK.99101", "bull", 25460.0, outstanding=5_000_000.0)
        # This bull has call_level < today_low → still alive.
        alive_bull = _record("HK.99102", "bull", 25400.0, outstanding=1_000_000.0)
        # Bears unaffected by today_low.
        bear = _record("HK.99103", "bear", 25800.0, outstanding=1_000_000.0)

        result = compute_magnet(
            _snapshot(dead_bull_1, dead_bull_2, alive_bull, bear),
            25735.0, 300.0,
            generated_at_hk=datetime(2026, 5, 26, 14, 30),
            today_low=25431.0,
        )
        # nearest_bull should jump from 285pt (dead 25450) to 335pt (alive 25400).
        self.assertEqual(result.nearest_bull_distance_pts, 335.0)
        # Dead bulls don't contribute to pull.
        # Only alive_bull (1M shares) and bear (1M shares) participate.
        # Both at distance 335pt (alive_bull) and 65pt (bear).
        self.assertGreater(result.magnet_pull_bear, result.magnet_pull_bull)

    def test_today_high_filters_already_killed_bear_contracts(self) -> None:
        """同理:今天 HSI 触及过的熊证收回价已被赎回。"""
        # Spot 25500, today_high pierced 25800. Bears at 25750 / 25800 are dead.
        dead_bear = _record("HK.99200", "bear", 25800.0, outstanding=10_000_000.0)
        alive_bear = _record("HK.99201", "bear", 25900.0, outstanding=1_000_000.0)
        bull = _record("HK.99202", "bull", 25200.0, outstanding=1_000_000.0)

        result = compute_magnet(
            _snapshot(dead_bear, alive_bear, bull),
            25500.0, 500.0,
            generated_at_hk=datetime(2026, 5, 26, 14, 30),
            today_high=25801.0,
        )
        # nearest_bear should be 400pt (alive 25900 - spot 25500), not 300pt.
        self.assertEqual(result.nearest_bear_distance_pts, 400.0)

    def test_today_extremes_none_means_no_filter(self) -> None:
        """``today_low / today_high`` 不传时应当与之前行为完全一致。"""
        bull = _record("HK.99300", "bull", 25450.0, outstanding=1_000_000.0)
        result = compute_magnet(
            _snapshot(bull),
            25735.0, 300.0,
            generated_at_hk=datetime(2026, 5, 26, 14, 30),
        )
        # Without today_low filter, the dead-looking bull is still considered.
        self.assertEqual(result.nearest_bull_distance_pts, 285.0)

    def test_invalid_decay_points_raises(self) -> None:
        with self.assertRaises(ValueError):
            compute_magnet(
                _snapshot(), 20000.0, 0.0,
                generated_at_hk=datetime(2025, 1, 2, 14, 30),
            )
        with self.assertRaises(ValueError):
            compute_magnet(
                _snapshot(), 20000.0, float("nan"),
                generated_at_hk=datetime(2025, 1, 2, 14, 30),
            )

    def test_non_finite_hsi_returns_degraded_result(self) -> None:
        bear = _record("HK.50400", "bear", 20100.0)
        result = compute_magnet(
            _snapshot(bear), float("nan"), 300.0,
            generated_at_hk=datetime(2025, 1, 2, 14, 30),
        )
        self.assertEqual(result.record_count, 0)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.magnet_bias, 0.0)


# --------------------------------------------------------------------------- #
# MagnetEngine (task 3.2)
# --------------------------------------------------------------------------- #


class MagnetEngineDecayValidationTest(unittest.TestCase):
    def test_invalid_decay_at_init_raises(self) -> None:
        with self.assertRaises(MagnetEngineError) as ctx:
            MagnetEngine(decay_points=-1.0)
        self.assertEqual(ctx.exception.code, "cbbc_magnet_decay_points_invalid")

    def test_update_decay_invalid_keeps_old_value(self) -> None:
        engine = MagnetEngine(
            decay_points=300.0, clock=_stable_clock(_hk(2025, 1, 2, 14, 30))
        )
        with self.assertRaises(MagnetEngineError):
            engine.update_decay_points(20000.0)  # > 10000 ⇒ invalid
        # Internal state must still hold the original value; we verify by
        # forcing a recompute through a snapshot push.
        bear = _record("HK.50500", "bear", 20100.0)
        engine.update_snapshot(_snapshot(bear))
        engine.update_hsi_spot(20000.0, _hk(2025, 1, 2, 14, 30))
        self.assertEqual(engine.latest().decay_points, 300.0)

    def test_update_decay_valid_recomputes_immediately(self) -> None:
        engine = MagnetEngine(
            decay_points=300.0, clock=_stable_clock(_hk(2025, 1, 2, 14, 30))
        )
        bear = _record("HK.50600", "bear", 20100.0)
        engine.update_snapshot(_snapshot(bear))
        engine.update_hsi_spot(20000.0, _hk(2025, 1, 2, 14, 30))
        engine.update_decay_points(50.0)
        # 50pt threshold + record at distance 100pt → weight=0 → no pull.
        result = engine.latest()
        self.assertEqual(result.decay_points, 50.0)
        self.assertEqual(result.magnet_pull_bear, 0.0)


class MagnetEngineHsiStaleTest(unittest.TestCase):
    def test_stale_after_threshold_marks_existing_result(self) -> None:
        clock_t = [_hk(2025, 1, 2, 14, 30, 0)]
        engine = MagnetEngine(
            decay_points=300.0,
            hsi_stale_seconds=5.0,
            clock=lambda: clock_t[0],
        )
        bear = _record("HK.50700", "bear", 20100.0)
        engine.update_snapshot(_snapshot(bear))
        engine.update_hsi_spot(20000.0, _hk(2025, 1, 2, 14, 30, 0))
        first = engine.latest()
        self.assertFalse(first.hsi_spot_stale)

        # Advance clock past the stale threshold without pushing a new spot.
        clock_t[0] = _hk(2025, 1, 2, 14, 30, 10)
        result = engine.latest()
        self.assertTrue(result.hsi_spot_stale)

    def test_recompute_when_hsi_delta_exceeds_threshold(self) -> None:
        clock_t = [_hk(2025, 1, 2, 14, 30)]
        engine = MagnetEngine(
            decay_points=300.0,
            hsi_recompute_threshold_pts=5.0,
            clock=lambda: clock_t[0],
        )
        bear = _record("HK.50800", "bear", 20100.0)
        engine.update_snapshot(_snapshot(bear))
        engine.update_hsi_spot(20000.0, _hk(2025, 1, 2, 14, 30))
        first = engine.latest()
        # Δ=4 (≤ threshold) → no recompute, hsi_spot stays the same.
        clock_t[0] = _hk(2025, 1, 2, 14, 30, 1)
        engine.update_hsi_spot(20004.0, _hk(2025, 1, 2, 14, 30, 1))
        self.assertEqual(engine.latest().hsi_spot, first.hsi_spot)
        # Δ=10 (> threshold) → recompute.
        clock_t[0] = _hk(2025, 1, 2, 14, 30, 2)
        engine.update_hsi_spot(20010.0, _hk(2025, 1, 2, 14, 30, 2))
        self.assertEqual(engine.latest().hsi_spot, 20010.0)

    def test_stale_event_raises_engine_error(self) -> None:
        clock_t = [_hk(2025, 1, 2, 14, 30, 0)]
        engine = MagnetEngine(
            decay_points=300.0,
            hsi_stale_seconds=5.0,
            clock=lambda: clock_t[0],
        )
        bear = _record("HK.50900", "bear", 20100.0)
        engine.update_snapshot(_snapshot(bear))
        # First push establishes baseline.
        engine.update_hsi_spot(20000.0, _hk(2025, 1, 2, 14, 30, 0))
        # Second push 60s later → stale event.
        clock_t[0] = _hk(2025, 1, 2, 14, 31, 0)
        with self.assertRaises(MagnetEngineError) as ctx:
            engine.update_hsi_spot(20001.0, _hk(2025, 1, 2, 14, 31, 0))
        self.assertEqual(ctx.exception.code, "hsi_spot_stale")
        # The existing result is now marked stale.
        self.assertTrue(engine.latest().hsi_spot_stale)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
