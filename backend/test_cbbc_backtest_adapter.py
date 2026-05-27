"""Unit tests for ``cbbc_backtest_adapter`` (cbbc-magnet-signal task 10.3)."""
from __future__ import annotations

import sys
import types
import unittest
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


# pandas/numpy ABI mismatch can break unrelated imports on this dev box; these
# stubs simply guarantee that ``import cbbc_backtest_adapter`` succeeds even
# when other modules pull pandas at import time.
def _ensure_module(name: str) -> types.ModuleType:
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
    mod = _ensure_module(name)
    for k, v in attrs.items():
        setattr(mod, k, v)


_stub_if_unavailable("pandas", {"DataFrame": type("DataFrame", (), {})})
_stub_if_unavailable("numpy", {"nan": float("nan")})

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cbbc_backtest_adapter import (  # noqa: E402
    CbbcBacktestAdapter,
    DayPreparation,
    IntradayNewListingEvent,
    build_intraday_event,
)
from cbbc_calculator import MagnetResult  # noqa: E402
from cbbc_storage import CbbcRecord, CbbcSnapshot, SnapshotError  # noqa: E402


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


@dataclass
class _RuntimeConfigStub:
    cbbc_magnet_layer_enabled: bool = True
    cbbc_dense_band_threshold_pts: float = 150.0
    cbbc_dense_band_pull_share: float = 0.40


@dataclass
class _StorageStub:
    """Records returned by ``read_snapshot`` keyed by (year, month, day)."""

    snapshots: dict[date, CbbcSnapshot] = field(default_factory=dict)
    raise_on: dict[date, str] = field(default_factory=dict)  # date → error code
    latest_before_value: CbbcSnapshot | None = None
    raise_on_latest_before: bool = False

    def read_snapshot(self, d: date) -> CbbcSnapshot:
        if d in self.raise_on:
            raise SnapshotError(self.raise_on[d], f"injected:{self.raise_on[d]}")
        if d in self.snapshots:
            return self.snapshots[d]
        raise SnapshotError("snapshot_missing", f"no snapshot for {d}")

    def latest_before(self, d: date) -> CbbcSnapshot | None:
        if self.raise_on_latest_before:
            raise RuntimeError("latest_before exploded")
        return self.latest_before_value


def _make_record(
    code: str,
    direction: str,
    call_level: float,
    *,
    listing_d: date | None = None,
    maturity_d: date | None = None,
    snap_d: date | None = None,
) -> CbbcRecord:
    listing_d = listing_d or date(2024, 1, 1)
    maturity_d = maturity_d or date(2030, 12, 31)
    snap_d = snap_d or date(2025, 1, 1)
    return CbbcRecord(
        issuer="HSBC",
        code=code,
        call_level=call_level,
        outstanding_shares=1_000_000.0,
        er_ratio=10000.0,
        direction=direction,  # type: ignore[arg-type]
        listing_date=listing_d,
        maturity_date=maturity_d,
        underlying="HSI",
        snapshot_date=snap_d,
    )


def _build_snapshot(
    snap_d: date,
    records: list[CbbcRecord] | None = None,
) -> CbbcSnapshot:
    return CbbcSnapshot(
        snapshot_date=snap_d,
        records=tuple(records or []),
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


class PrepareForDayTest(unittest.TestCase):
    """task 10.3 — D-1 fallback / snapshot 缺失整日跳过."""

    def setUp(self) -> None:
        self.config = _RuntimeConfigStub()

    def test_d_minus_1_snapshot_used_when_present(self) -> None:
        d_minus_1 = date(2025, 1, 1)
        day = date(2025, 1, 2)
        snap = _build_snapshot(
            d_minus_1,
            [_make_record("HK.51000", "bull", 19000.0)],
        )
        storage = _StorageStub(snapshots={d_minus_1: snap})

        adapter = CbbcBacktestAdapter(storage=storage, config=self.config)
        prep = adapter.prepare_for_day(day)

        self.assertFalse(prep.missing)
        self.assertEqual(prep.base_snapshot, snap)
        self.assertFalse(prep.fallback_used)
        self.assertEqual(adapter.summary().cbbc_snapshot_missing_days, 0)

    def test_falls_back_via_latest_before_when_d_minus_1_missing(self) -> None:
        day = date(2025, 1, 2)
        d_minus_1 = day - timedelta(days=1)
        # D-1 missing, but a 2024-12-31 snapshot is available via latest_before.
        old_snap = _build_snapshot(date(2024, 12, 31))
        storage = _StorageStub(
            raise_on={d_minus_1: "snapshot_missing"},
            latest_before_value=old_snap,
        )

        adapter = CbbcBacktestAdapter(storage=storage, config=self.config)
        prep = adapter.prepare_for_day(day)

        self.assertFalse(prep.missing)
        self.assertTrue(prep.fallback_used)
        self.assertEqual(prep.base_snapshot, old_snap)
        self.assertEqual(adapter.summary().cbbc_snapshot_missing_days, 0)

    def test_missing_day_when_no_snapshot_and_no_fallback(self) -> None:
        day = date(2025, 1, 2)
        d_minus_1 = day - timedelta(days=1)
        storage = _StorageStub(
            raise_on={d_minus_1: "snapshot_missing"},
            latest_before_value=None,
        )

        adapter = CbbcBacktestAdapter(storage=storage, config=self.config)
        prep = adapter.prepare_for_day(day)

        self.assertTrue(prep.missing)
        self.assertEqual(adapter.summary().cbbc_snapshot_missing_days, 1)

    def test_non_trading_day_d_minus_1_falls_back(self) -> None:
        # When D-1 is a holiday, read_snapshot raises non_trading_day; the
        # adapter must still try latest_before.
        day = date(2025, 1, 6)  # assume this is a trading day
        d_minus_1 = day - timedelta(days=1)  # Sunday
        old_snap = _build_snapshot(date(2025, 1, 3))
        storage = _StorageStub(
            raise_on={d_minus_1: "non_trading_day"},
            latest_before_value=old_snap,
        )

        adapter = CbbcBacktestAdapter(storage=storage, config=self.config)
        prep = adapter.prepare_for_day(day)

        self.assertFalse(prep.missing)
        self.assertTrue(prep.fallback_used)
        self.assertEqual(adapter.summary().cbbc_snapshot_missing_days, 0)

    def test_storage_disk_failure_falls_back_then_returns_missing(self) -> None:
        day = date(2025, 1, 2)
        d_minus_1 = day - timedelta(days=1)
        # ``read_snapshot`` raises a non-SnapshotError; the adapter logs and
        # then asks ``latest_before`` (which also throws). No usable snapshot
        # → missing day.
        storage = _StorageStub(
            raise_on_latest_before=True,
            latest_before_value=None,
        )

        # Patch the read_snapshot to raise OSError to exercise the disk-error path.
        def boom(d: date) -> CbbcSnapshot:  # noqa: ARG001
            raise OSError("disk dead")

        storage.read_snapshot = boom  # type: ignore[assignment]

        adapter = CbbcBacktestAdapter(storage=storage, config=self.config)
        prep = adapter.prepare_for_day(day)

        self.assertTrue(prep.missing)
        self.assertEqual(adapter.summary().cbbc_snapshot_missing_days, 1)


class IntradayInjectionTest(unittest.TestCase):
    """task 10.3 — 新发记录注入幂等 + 顺序."""

    def setUp(self) -> None:
        self.day = date(2025, 1, 2)
        d_minus_1 = self.day - timedelta(days=1)
        self.base = _build_snapshot(d_minus_1, [_make_record("HK.50001", "bull", 19000.0)])
        self.storage = _StorageStub(snapshots={d_minus_1: self.base})

    def test_intraday_listing_injected_when_ts_meets_listing_time(self) -> None:
        listing_time = datetime(2025, 1, 2, 10, 30)
        new_event = build_intraday_event(
            listing_time,
            _make_record(
                "HK.50002", "bear", 21000.0, listing_d=self.day, snap_d=self.day
            ),
        )

        def loader(day: date):  # noqa: ARG001
            return [new_event]

        adapter = CbbcBacktestAdapter(
            storage=self.storage, config=_RuntimeConfigStub(), intraday_loader=loader
        )
        prep = adapter.prepare_for_day(self.day)
        self.assertFalse(prep.missing)
        self.assertEqual(len(prep.intraday_new_listings), 1)

        # Before listing_time → still 1 record only.
        adapter.at_replay_ts(datetime(2025, 1, 2, 10, 0), 20000.0)
        self.assertEqual(len(adapter._current_records), 1)

        # After listing_time → injected.
        adapter.at_replay_ts(datetime(2025, 1, 2, 10, 31), 20000.0)
        self.assertEqual(len(adapter._current_records), 2)
        self.assertIn("HK.50002", adapter._injected_codes)

        # Re-run later → still 2 records (idempotent).
        adapter.at_replay_ts(datetime(2025, 1, 2, 14, 0), 20000.0)
        self.assertEqual(len(adapter._current_records), 2)

    def test_duplicate_listing_codes_only_injected_once(self) -> None:
        """If the loader hands the same code twice (e.g. duplicated upstream),
        only the first one wins."""
        ts = datetime(2025, 1, 2, 10, 30)
        new_record = _make_record(
            "HK.50002", "bear", 21000.0, listing_d=self.day, snap_d=self.day
        )

        def loader(day: date):  # noqa: ARG001
            return [
                build_intraday_event(ts, new_record),
                build_intraday_event(ts + timedelta(seconds=5), new_record),
            ]

        adapter = CbbcBacktestAdapter(
            storage=self.storage, config=_RuntimeConfigStub(), intraday_loader=loader
        )
        adapter.prepare_for_day(self.day)
        adapter.at_replay_ts(datetime(2025, 1, 2, 11, 0), 20000.0)
        # Base + 1 injected = 2 records (duplicate skipped).
        self.assertEqual(len(adapter._current_records), 2)

    def test_no_op_when_day_missing(self) -> None:
        adapter = CbbcBacktestAdapter(
            storage=_StorageStub(latest_before_value=None),
            config=_RuntimeConfigStub(),
        )
        prep = adapter.prepare_for_day(self.day)
        self.assertTrue(prep.missing)

        # at_replay_ts must be a no-op when there is no base snapshot.
        adapter.at_replay_ts(datetime(2025, 1, 2, 11, 0), 20000.0)
        # _current_records stays empty; no exception raised.
        self.assertEqual(adapter._current_records, [])


class ConsultExtremeTest(unittest.TestCase):
    """task 10.3 — control_total / vetoed counters / layer-disabled / fail-safe."""

    def setUp(self) -> None:
        self.day = date(2025, 1, 2)
        d_minus_1 = self.day - timedelta(days=1)
        # Snapshot with one bear record at HSI+50 → BULL extreme reversal sees
        # bear distance 50pt.
        self.base = _build_snapshot(
            d_minus_1,
            [
                _make_record(
                    "HK.50100",
                    "bear",
                    20050.0,
                    listing_d=date(2024, 12, 1),
                    maturity_d=date(2026, 1, 1),
                    snap_d=d_minus_1,
                ),
            ],
        )
        self.storage = _StorageStub(snapshots={d_minus_1: self.base})

    def _make_adapter(self, **config_overrides) -> CbbcBacktestAdapter:
        config = _RuntimeConfigStub(**config_overrides)
        adapter = CbbcBacktestAdapter(storage=self.storage, config=config)
        prep = adapter.prepare_for_day(self.day)
        assert not prep.missing
        # Push a stable HSI spot so the engine has a fresh result.
        adapter.at_replay_ts(datetime(2025, 1, 2, 10, 0), 20000.0)
        return adapter

    def test_control_total_increments_for_relevant_branches(self) -> None:
        adapter = self._make_adapter(cbbc_magnet_layer_enabled=False)  # layer off
        adapter.consult_extreme(
            "bull",
            20000.0,
            datetime(2025, 1, 2, 10, 5),
            branch="b1_volume_extreme",
            mode="极度超卖",
        )
        adapter.consult_extreme(
            "bear",
            20000.0,
            datetime(2025, 1, 2, 10, 6),
            branch="b3_completed_k",
            mode="极度超买",
        )
        s = adapter.summary()
        # control_total counts both, even though layer is off.
        self.assertEqual(s.control_total, 2)
        # No vetoes when layer is disabled.
        self.assertEqual(s.total_vetoed, 0)

    def test_layer_disabled_returns_layer_disabled_decision(self) -> None:
        adapter = self._make_adapter(cbbc_magnet_layer_enabled=False)
        decision = adapter.consult_extreme(
            "bull",
            20000.0,
            datetime(2025, 1, 2, 10, 5),
            branch="b1_volume_extreme",
            mode="极度超卖",
        )
        assert decision is not None
        self.assertFalse(decision.vetoed_by_cbbc_magnet)
        self.assertEqual(decision.reason_code, "cbbc_magnet_layer_disabled")
        self.assertEqual(adapter.summary().cbbc_snapshot_missing_days, 0)

    def test_dense_band_above_veto_increments_above_counter(self) -> None:
        # Bear record at HSI+50 → BULL reversal hits cbbc_dense_band_above.
        adapter = self._make_adapter(cbbc_magnet_layer_enabled=True)
        decision = adapter.consult_extreme(
            "bull",
            20000.0,
            datetime(2025, 1, 2, 10, 5),
            branch="b1_volume_extreme",
            mode="极度超卖",
        )
        assert decision is not None
        self.assertTrue(decision.vetoed_by_cbbc_magnet)
        self.assertEqual(decision.reason_code, "cbbc_dense_band_above")
        s = adapter.summary()
        self.assertEqual(s.total_vetoed, 1)
        self.assertEqual(s.vetoed_dense_band_above, 1)
        self.assertEqual(s.vetoed_dense_band_below, 0)
        self.assertEqual(s.control_total, 1)

    def test_missing_day_consult_returns_none_and_increments_control_total(self) -> None:
        # A day with no D-1 base snapshot and no fallback.
        bad_storage = _StorageStub(latest_before_value=None)
        adapter = CbbcBacktestAdapter(storage=bad_storage, config=_RuntimeConfigStub())
        prep = adapter.prepare_for_day(self.day)
        self.assertTrue(prep.missing)
        decision = adapter.consult_extreme(
            "bull",
            20000.0,
            datetime(2025, 1, 2, 10, 5),
            branch="b1_volume_extreme",
            mode="极度超卖",
        )
        # No layer ⇒ no decision returned, but control_total still bumps so
        # the operator can compare apples-to-apples.
        self.assertIsNone(decision)
        s = adapter.summary()
        self.assertEqual(s.control_total, 1)
        self.assertEqual(s.cbbc_snapshot_missing_days, 1)
        self.assertEqual(s.total_vetoed, 0)

    def test_non_extreme_branch_does_not_bump_control_total(self) -> None:
        adapter = self._make_adapter(cbbc_magnet_layer_enabled=True)
        decision = adapter.consult_extreme(
            "bull",
            20000.0,
            datetime(2025, 1, 2, 10, 5),
            branch="momentum",
            mode=None,
        )
        # control_total is meant for extreme reversals only.
        self.assertEqual(adapter.summary().control_total, 0)
        # Direction can't be inferred from "momentum" + no mode tokens; we
        # accept either None or a no-veto decision (current implementation
        # returns None because direction inference falls through "bull"
        # heuristics). Just ensure no veto counter increments.
        self.assertEqual(adapter.summary().total_vetoed, 0)


class SummaryTest(unittest.TestCase):
    """task 10.3 — summary 计数正确."""

    def test_summary_reflects_running_counters(self) -> None:
        day = date(2025, 1, 2)
        d_minus_1 = day - timedelta(days=1)
        base = _build_snapshot(d_minus_1, [_make_record("HK.99100", "bull", 19500.0)])
        storage = _StorageStub(snapshots={d_minus_1: base})

        adapter = CbbcBacktestAdapter(storage=storage, config=_RuntimeConfigStub())
        adapter.prepare_for_day(day)
        adapter.at_replay_ts(datetime(2025, 1, 2, 10, 0), 20000.0)

        # BEAR reversal sees bull distance ~500pt → cbbc_dense_band_clear.
        adapter.consult_extreme(
            "bear",
            20000.0,
            datetime(2025, 1, 2, 10, 5),
            branch="b1_volume_extreme",
            mode="极度超买",
        )

        s = adapter.summary()
        self.assertEqual(s.control_total, 1)
        self.assertEqual(s.total_vetoed, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
