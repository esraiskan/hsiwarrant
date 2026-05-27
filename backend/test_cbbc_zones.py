"""Unit tests for ``cbbc_zones`` (read-only zone aggregation)."""
from __future__ import annotations

import sys
import types
import unittest
from datetime import date
from pathlib import Path


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

from cbbc_storage import CbbcRecord, CbbcSnapshot  # noqa: E402
from cbbc_zones import compute_zones  # noqa: E402


def _rec(code: str, direction: str, call_level: float, *, outstanding: float = 1_000_000.0) -> CbbcRecord:
    return CbbcRecord(
        issuer="HSBC", code=code, call_level=call_level,
        outstanding_shares=outstanding, er_ratio=10000.0,
        direction=direction,  # type: ignore[arg-type]
        listing_date=date(2025, 1, 1), maturity_date=date(2030, 12, 31),
        underlying="HSI", snapshot_date=date(2026, 5, 26),
    )


def _snap(*records):
    return CbbcSnapshot(snapshot_date=date(2026, 5, 26), records=tuple(records))


class ComputeZonesTest(unittest.TestCase):
    def test_empty_snapshot_returns_empty_payload(self) -> None:
        z = compute_zones(_snap(), 25700.0)
        self.assertEqual(z.spot, 25700.0)
        self.assertEqual(z.targets_above, ())
        self.assertEqual(z.supports_below, ())
        self.assertEqual(z.live_record_count, 0)

    def test_none_snapshot_safe(self) -> None:
        z = compute_zones(None, 25700.0)
        self.assertEqual(z.live_record_count, 0)
        self.assertEqual(z.targets_above, ())

    def test_zero_street_filtered_out(self) -> None:
        zombie = _rec("HK.99001", "bear", 25750.0, outstanding=0.0)
        live = _rec("HK.99002", "bear", 25800.0, outstanding=10_000_000.0)
        z = compute_zones(_snap(zombie, live), 25700.0)
        self.assertEqual(z.live_record_count, 1)
        self.assertEqual(len(z.targets_above), 1)
        self.assertEqual(z.targets_above[0].bucket_low, 25800.0)

    def test_kill_filter_today_low(self) -> None:
        # today_low = 25431 → bull 25450 / 25460 must be filtered
        dead_1 = _rec("HK.99001", "bull", 25450.0, outstanding=10_000_000.0)
        dead_2 = _rec("HK.99002", "bull", 25460.0, outstanding=5_000_000.0)
        alive = _rec("HK.99003", "bull", 25400.0, outstanding=1_000_000.0)
        z = compute_zones(
            _snap(dead_1, dead_2, alive),
            25700.0,
            today_low=25431.0,
            min_notional_hkd=0.0,  # disable threshold for test
        )
        self.assertEqual(z.killed_record_count, 2)
        self.assertEqual(z.live_record_count, 1)
        # Only the 25400 bull (rounds to 25400) appears.
        codes_in_zone = [s.bucket_low for s in z.supports_below]
        self.assertEqual(codes_in_zone, [25400.0])

    def test_kill_filter_today_high(self) -> None:
        dead = _rec("HK.99001", "bear", 25750.0, outstanding=10_000_000.0)
        alive = _rec("HK.99002", "bear", 25800.0, outstanding=10_000_000.0)
        z = compute_zones(
            _snap(dead, alive),
            25700.0,
            today_high=25770.0,
            min_notional_hkd=0.0,
        )
        self.assertEqual(z.killed_record_count, 1)
        self.assertEqual([t.bucket_low for t in z.targets_above], [25800.0])

    def test_buckets_sorted_by_distance(self) -> None:
        b_far = _rec("HK.99001", "bear", 26000.0, outstanding=10_000_000.0)
        b_mid = _rec("HK.99002", "bear", 25800.0, outstanding=10_000_000.0)
        b_near = _rec("HK.99003", "bear", 25750.0, outstanding=10_000_000.0)
        z = compute_zones(_snap(b_far, b_mid, b_near), 25700.0, min_notional_hkd=0.0)
        # Closest first.
        self.assertEqual([t.bucket_low for t in z.targets_above],
                         [25750.0, 25800.0, 26000.0])

    def test_aggregates_within_bucket(self) -> None:
        # Two records 25803 + 25805 round to 25800 bucket.
        a = _rec("HK.99001", "bear", 25803.0, outstanding=10_000_000.0)
        b = _rec("HK.99002", "bear", 25805.0, outstanding=5_000_000.0)
        z = compute_zones(_snap(a, b), 25700.0, min_notional_hkd=0.0)
        self.assertEqual(len(z.targets_above), 1)
        zone = z.targets_above[0]
        self.assertEqual(zone.contract_count, 2)
        self.assertEqual(zone.outstanding_shares, 15_000_000.0)
        self.assertEqual(zone.notional_hkd, 15_000_000.0 * 10_000.0)

    def test_min_notional_threshold_filters_small_buckets(self) -> None:
        small = _rec("HK.99001", "bear", 25800.0, outstanding=100.0)  # tiny
        big = _rec("HK.99002", "bear", 25850.0, outstanding=10_000_000.0)
        z = compute_zones(_snap(small, big), 25700.0)  # default 1e11 threshold
        self.assertEqual(len(z.targets_above), 1)
        self.assertEqual(z.targets_above[0].bucket_low, 25850.0)


class TradeSetupDerivationTest(unittest.TestCase):
    """覆盖 ``_derive_bull_setup`` / ``_derive_bear_setup`` 的关键路径。"""

    def test_bull_setup_basic_shape(self) -> None:
        # 现价 25700; 上方 25800 / 25850 两档熊证墙;今天 low 25600。
        targets = (
            _rec("HK.B1", "bear", 25800.0, outstanding=10_000_000.0),
            _rec("HK.B2", "bear", 25850.0, outstanding=10_000_000.0),
        )
        z = compute_zones(_snap(*targets), 25700.0, today_low=25600.0, min_notional_hkd=0.0)
        self.assertIsNotNone(z.bull_setup)
        s = z.bull_setup
        assert s is not None
        self.assertEqual(s.direction, "bull")
        # 入场区 = spot ± 10
        self.assertEqual(s.entry_low, 25690.0)
        self.assertEqual(s.entry_high, 25710.0)
        # 第一止盈 = 最近熊证墙
        self.assertEqual(s.take_profit_1, 25800.0)
        self.assertEqual(s.take_profit_2, 25850.0)
        # 止损 = max(today_low - 5, spot - 50) = max(25595, 25650) = 25650
        self.assertEqual(s.stop_loss, 25650.0)
        # R:R = (25800 - 25700) / (25700 - 25650) = 100 / 50 = 2.0
        self.assertEqual(s.risk_reward, 2.0)

    def test_bear_setup_basic_shape(self) -> None:
        # 现价 25700; 下方 25600 / 25550 两档牛证墙;今天 high 25800。
        supports = (
            _rec("HK.S1", "bull", 25600.0, outstanding=10_000_000.0),
            _rec("HK.S2", "bull", 25550.0, outstanding=10_000_000.0),
        )
        z = compute_zones(_snap(*supports), 25700.0, today_high=25800.0, min_notional_hkd=0.0)
        self.assertIsNotNone(z.bear_setup)
        s = z.bear_setup
        assert s is not None
        self.assertEqual(s.direction, "bear")
        self.assertEqual(s.take_profit_1, 25600.0)
        self.assertEqual(s.take_profit_2, 25550.0)
        # 止损 = min(today_high + 5, spot + 50) = min(25805, 25750) = 25750
        self.assertEqual(s.stop_loss, 25750.0)
        # R:R = (25700 - 25600) / (25750 - 25700) = 100 / 50 = 2.0
        self.assertEqual(s.risk_reward, 2.0)

    def test_low_rr_setup_marked_skip(self) -> None:
        # spot 25700, 熊证墙紧贴 (25720, bucket 25725); 止损至少 50pt
        # → reward = 25 / risk = 50 → R:R = 0.5 < 1.0
        target = _rec("HK.B1", "bear", 25720.0, outstanding=10_000_000.0)
        z = compute_zones(_snap(target), 25700.0, today_low=25600.0, min_notional_hkd=0.0)
        s = z.bull_setup
        assert s is not None
        self.assertLess(s.risk_reward, 1.0)
        self.assertIn("不建议入场", s.rationale)

    def test_no_targets_no_bull_setup(self) -> None:
        # 只有牛证 (上方支撑) - 无法构造 bull setup
        bull_only = _rec("HK.S1", "bull", 25600.0, outstanding=10_000_000.0)
        z = compute_zones(_snap(bull_only), 25700.0, min_notional_hkd=0.0)
        self.assertIsNone(z.bull_setup)
        # 但 bear setup 应存在
        self.assertIsNotNone(z.bear_setup)

    def test_no_supports_no_bear_setup(self) -> None:
        bear_only = _rec("HK.B1", "bear", 25800.0, outstanding=10_000_000.0)
        z = compute_zones(_snap(bear_only), 25700.0, min_notional_hkd=0.0)
        self.assertIsNotNone(z.bull_setup)
        self.assertIsNone(z.bear_setup)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
