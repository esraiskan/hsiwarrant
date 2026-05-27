"""Unit tests for ``cbbc_signal_adapter`` (cbbc-magnet-signal task 6.4 + 6.3).

Covers all seven ``ConsultReasonCode`` values + parameter fallback logging +
degradation lifecycle (R10.1–R10.6).
"""
from __future__ import annotations

import logging
import sys
import types
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


# Light stubs so importing the adapter doesn't pull pandas/numpy in.
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

from cbbc_calculator import HistBucket, MagnetResult  # noqa: E402
from cbbc_signal_adapter import (  # noqa: E402
    ConsultDecision,
    MagnetSignalAdapter,
    SystemClock,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@dataclass
class _Cfg:
    cbbc_magnet_layer_enabled: bool = True
    cbbc_dense_band_threshold_pts: float = 150.0
    cbbc_dense_band_pull_share: float = 0.40


class _StubEngine:
    def __init__(self, result: MagnetResult | None) -> None:
        self.result = result
        self.snapshots_received: list = []

    def latest(self) -> MagnetResult | None:
        return self.result

    def update_snapshot(self, snapshot) -> None:
        self.snapshots_received.append(snapshot)


def _make_result(
    *,
    bias: float = 0.0,
    pull_bull: float = 0.0,
    pull_bear: float = 0.0,
    nearest_bull: float | None = None,
    nearest_bear: float | None = None,
    hsi_spot_stale: bool = False,
) -> MagnetResult:
    return MagnetResult(
        generated_at_hk=datetime(2025, 1, 1, 14, 30),
        hsi_spot=20000.0,
        hsi_spot_stale=hsi_spot_stale,
        decay_points=300.0,
        magnet_bias=bias,
        magnet_pull_bull=pull_bull,
        magnet_pull_bear=pull_bear,
        histogram=(),
        record_count=2 if (pull_bull or pull_bear) else 0,
        skipped_count=0,
        nearest_bull_distance_pts=nearest_bull,
        nearest_bear_distance_pts=nearest_bear,
    )


class _LogCapture:
    """Capture cbbc_magnet log records during a test."""

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []
        self._handler = logging.Handler(level=logging.DEBUG)
        self._handler.emit = self.records.append  # type: ignore[assignment]
        self._previous_level: int | None = None

    def __enter__(self):
        logger = logging.getLogger("cbbc_magnet")
        self._previous_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(self._handler)
        return self

    def __exit__(self, exc_type, exc, tb):
        logger = logging.getLogger("cbbc_magnet")
        logger.removeHandler(self._handler)
        if self._previous_level is not None:
            logger.setLevel(self._previous_level)


# --------------------------------------------------------------------------- #
# Decision matrix (task 6.4)
# --------------------------------------------------------------------------- #


class ReasonCodeTest(unittest.TestCase):
    """All seven reason codes."""

    def test_layer_disabled(self) -> None:
        cfg = _Cfg(cbbc_magnet_layer_enabled=False)
        adapter = MagnetSignalAdapter(calculator=_StubEngine(None), config=cfg)
        decision = adapter.consult_for_extreme(
            "BULL", 20000.0, datetime(2025, 1, 1, 14, 30)
        )
        self.assertFalse(decision.vetoed_by_cbbc_magnet)
        self.assertEqual(decision.reason_code, "cbbc_magnet_layer_disabled")
        self.assertFalse(decision.magnet_available)

    def test_dense_band_clear(self) -> None:
        result = _make_result(
            bias=0.0, pull_bull=500.0, pull_bear=500.0,
            nearest_bull=400.0, nearest_bear=400.0,
        )
        adapter = MagnetSignalAdapter(calculator=_StubEngine(result), config=_Cfg())
        decision = adapter.consult_for_extreme(
            "BULL", 20000.0, datetime(2025, 1, 1, 14, 30)
        )
        self.assertFalse(decision.vetoed_by_cbbc_magnet)
        self.assertEqual(decision.reason_code, "cbbc_dense_band_clear")

    def test_dense_band_above(self) -> None:
        # BULL reversal with bear records dominating near the spot.
        result = _make_result(
            bias=0.8, pull_bull=0.0, pull_bear=1_000_000.0,
            nearest_bull=400.0, nearest_bear=50.0,
        )
        adapter = MagnetSignalAdapter(calculator=_StubEngine(result), config=_Cfg())
        decision = adapter.consult_for_extreme(
            "BULL", 20000.0, datetime(2025, 1, 1, 14, 30)
        )
        self.assertTrue(decision.vetoed_by_cbbc_magnet)
        self.assertEqual(decision.reason_code, "cbbc_dense_band_above")
        self.assertTrue(decision.magnet_available)
        # bias > 0 with BULL reversal → magnet aligned against reversal.
        self.assertTrue(decision.magnet_aligned_against_reversal)

    def test_dense_band_below(self) -> None:
        result = _make_result(
            bias=-0.8, pull_bull=1_000_000.0, pull_bear=0.0,
            nearest_bull=50.0, nearest_bear=400.0,
        )
        adapter = MagnetSignalAdapter(calculator=_StubEngine(result), config=_Cfg())
        decision = adapter.consult_for_extreme(
            "BEAR", 20000.0, datetime(2025, 1, 1, 14, 30)
        )
        self.assertTrue(decision.vetoed_by_cbbc_magnet)
        self.assertEqual(decision.reason_code, "cbbc_dense_band_below")

    def test_dense_band_pull_share_below(self) -> None:
        # bear distance is close (50pt) but bear share is below threshold.
        result = _make_result(
            bias=0.0, pull_bull=900_000.0, pull_bear=100_000.0,
            nearest_bull=400.0, nearest_bear=50.0,
        )
        adapter = MagnetSignalAdapter(calculator=_StubEngine(result), config=_Cfg())
        decision = adapter.consult_for_extreme(
            "BULL", 20000.0, datetime(2025, 1, 1, 14, 30)
        )
        self.assertFalse(decision.vetoed_by_cbbc_magnet)
        self.assertEqual(decision.reason_code, "cbbc_dense_band_pull_share_below")

    def test_unavailable_when_engine_returns_none(self) -> None:
        adapter = MagnetSignalAdapter(calculator=_StubEngine(None), config=_Cfg())
        decision = adapter.consult_for_extreme(
            "BULL", 20000.0, datetime(2025, 1, 1, 14, 30)
        )
        self.assertFalse(decision.vetoed_by_cbbc_magnet)
        self.assertEqual(decision.reason_code, "cbbc_magnet_consult_unavailable")
        self.assertFalse(decision.magnet_available)

    def test_unavailable_when_total_pull_zero(self) -> None:
        result = _make_result(
            bias=0.0, pull_bull=0.0, pull_bear=0.0,
            nearest_bull=10.0, nearest_bear=10.0,
        )
        adapter = MagnetSignalAdapter(calculator=_StubEngine(result), config=_Cfg())
        decision = adapter.consult_for_extreme(
            "BULL", 20000.0, datetime(2025, 1, 1, 14, 30)
        )
        self.assertFalse(decision.vetoed_by_cbbc_magnet)
        self.assertEqual(decision.reason_code, "cbbc_magnet_consult_unavailable")

    def test_unavailable_when_hsi_spot_stale(self) -> None:
        result = _make_result(
            bias=0.0, pull_bull=1.0, pull_bear=1.0,
            nearest_bull=10.0, nearest_bear=10.0,
            hsi_spot_stale=True,
        )
        adapter = MagnetSignalAdapter(calculator=_StubEngine(result), config=_Cfg())
        decision = adapter.consult_for_extreme(
            "BULL", 20000.0, datetime(2025, 1, 1, 14, 30)
        )
        self.assertEqual(decision.reason_code, "cbbc_magnet_consult_unavailable")

    def test_unavailable_when_hsi_spot_non_finite(self) -> None:
        result = _make_result(
            bias=0.0, pull_bull=1.0, pull_bear=1.0,
            nearest_bull=10.0, nearest_bear=10.0,
        )
        adapter = MagnetSignalAdapter(calculator=_StubEngine(result), config=_Cfg())
        decision = adapter.consult_for_extreme(
            "BULL", float("nan"), datetime(2025, 1, 1, 14, 30)
        )
        self.assertEqual(decision.reason_code, "cbbc_magnet_consult_unavailable")

    def test_unavailable_when_relevant_distance_missing(self) -> None:
        # BULL reversal needs nearest_bear; provide only nearest_bull.
        result = _make_result(
            bias=0.0, pull_bull=10.0, pull_bear=10.0,
            nearest_bull=10.0, nearest_bear=None,
        )
        adapter = MagnetSignalAdapter(calculator=_StubEngine(result), config=_Cfg())
        decision = adapter.consult_for_extreme(
            "BULL", 20000.0, datetime(2025, 1, 1, 14, 30)
        )
        self.assertEqual(decision.reason_code, "cbbc_magnet_consult_unavailable")


class AlignmentTest(unittest.TestCase):
    """``magnet_aligned_against_reversal`` semantics (R5.6)."""

    def test_bias_zero_not_aligned(self) -> None:
        result = _make_result(
            bias=0.0, pull_bull=1.0, pull_bear=1.0,
            nearest_bull=400.0, nearest_bear=400.0,
        )
        adapter = MagnetSignalAdapter(calculator=_StubEngine(result), config=_Cfg())
        decision = adapter.consult_for_extreme(
            "BULL", 20000.0, datetime(2025, 1, 1, 14, 30)
        )
        self.assertFalse(decision.magnet_aligned_against_reversal)

    def test_bear_reversal_negative_bias_aligned(self) -> None:
        result = _make_result(
            bias=-0.5, pull_bull=1.0, pull_bear=1.0,
            nearest_bull=400.0, nearest_bear=400.0,
        )
        adapter = MagnetSignalAdapter(calculator=_StubEngine(result), config=_Cfg())
        decision = adapter.consult_for_extreme(
            "BEAR", 20000.0, datetime(2025, 1, 1, 14, 30)
        )
        self.assertTrue(decision.magnet_aligned_against_reversal)


class ParameterFallbackTest(unittest.TestCase):
    """R5.7 / R5.3 — invalid threshold / pull_share ⇒ default fallback + WARN."""

    def test_invalid_threshold_falls_back_and_logs(self) -> None:
        cfg = _Cfg(cbbc_dense_band_threshold_pts=99999.0)  # out of range
        result = _make_result(
            bias=0.0, pull_bull=1.0, pull_bear=10.0,
            nearest_bull=400.0, nearest_bear=80.0,
        )
        adapter = MagnetSignalAdapter(calculator=_StubEngine(result), config=cfg)
        with _LogCapture() as cap:
            decision = adapter.consult_for_extreme(
                "BULL", 20000.0, datetime(2025, 1, 1, 14, 30)
            )
        # Default 150pt threshold means 80pt distance → in band; share is high → veto.
        self.assertTrue(decision.vetoed_by_cbbc_magnet)
        events = [
            r.__dict__.get("event")
            for r in cap.records
        ]
        self.assertIn("cbbc_dense_band_threshold_pts_invalid_fallback", events)

    def test_invalid_pull_share_falls_back_and_logs(self) -> None:
        cfg = _Cfg(cbbc_dense_band_pull_share=2.5)  # out of range
        result = _make_result(
            bias=0.0, pull_bull=1.0, pull_bear=10.0,
            nearest_bull=400.0, nearest_bear=80.0,
        )
        adapter = MagnetSignalAdapter(calculator=_StubEngine(result), config=cfg)
        with _LogCapture() as cap:
            adapter.consult_for_extreme(
                "BULL", 20000.0, datetime(2025, 1, 1, 14, 30)
            )
        events = [r.__dict__.get("event") for r in cap.records]
        self.assertIn("cbbc_dense_band_pull_share_invalid_fallback", events)


# --------------------------------------------------------------------------- #
# Degradation lifecycle (task 6.3 — R10.1–R10.6)
# --------------------------------------------------------------------------- #


class _FakeClock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def now_hk(self) -> datetime:
        return self.t


class DegradationLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _FakeClock(datetime(2025, 1, 5, 10, 0))
        self.engine = _StubEngine(None)
        self.last_refresh = datetime(2025, 1, 5, 9, 59)
        self.snapshot_obj = object()

        def _last_refresh():
            return self.last_refresh

        def _snapshot():
            return self.snapshot_obj

        self.adapter = MagnetSignalAdapter(
            calculator=self.engine,
            config=_Cfg(),
            clock=self.clock,
            last_refresh_provider=_last_refresh,
            snapshot_provider=_snapshot,
        )

    def test_no_provider_disables_lifecycle(self) -> None:
        # Adapter without last_refresh_provider stays out of degraded state
        # even if the snapshot is missing.
        adapter = MagnetSignalAdapter(
            calculator=self.engine, config=_Cfg(), clock=self.clock
        )
        adapter.evaluate_degradation()
        self.assertFalse(adapter.cbbc_magnet_degraded)

    def test_enters_degraded_when_refresh_age_exceeds_36h(self) -> None:
        self.last_refresh = datetime(2025, 1, 1, 0, 0)  # 4 days old
        with _LogCapture() as cap:
            self.adapter.evaluate_degradation()
        self.assertTrue(self.adapter.cbbc_magnet_degraded)
        events = [r.__dict__.get("event") for r in cap.records]
        self.assertEqual(events.count("degraded_no_data"), 1)

    def test_enters_degraded_when_last_refresh_none(self) -> None:
        self.last_refresh = None
        with _LogCapture() as cap:
            self.adapter.evaluate_degradation()
        self.assertTrue(self.adapter.cbbc_magnet_degraded)
        events = [r.__dict__.get("event") for r in cap.records]
        self.assertEqual(events.count("degraded_no_data"), 1)

    def test_idempotent_degraded_no_repeated_log(self) -> None:
        self.last_refresh = None
        self.adapter.evaluate_degradation()
        with _LogCapture() as cap:
            # second call must NOT emit another degraded_no_data
            self.adapter.evaluate_degradation()
        events = [r.__dict__.get("event") for r in cap.records]
        self.assertNotIn("degraded_no_data", events)

    def test_recovery_initialized_after_fresh_refresh(self) -> None:
        # Start degraded.
        self.last_refresh = None
        self.adapter.evaluate_degradation()
        self.assertTrue(self.adapter.cbbc_magnet_degraded)

        # Fresh refresh + good snapshot + good result.
        self.last_refresh = datetime(2025, 1, 5, 9, 59)
        self.engine.result = _make_result(pull_bear=1.0, nearest_bull=10.0, nearest_bear=10.0)
        with _LogCapture() as cap:
            self.adapter.evaluate_degradation()
        self.assertFalse(self.adapter.cbbc_magnet_degraded)
        events = [r.__dict__.get("event") for r in cap.records]
        # Exactly one INFO recovery_initialized; no extra degraded_no_data.
        self.assertEqual(events.count("recovery_initialized"), 1)

    def test_recovery_failed_invalid_result(self) -> None:
        self.last_refresh = None
        self.adapter.evaluate_degradation()
        # Refresh becomes fresh but engine still returns no result.
        self.last_refresh = datetime(2025, 1, 5, 9, 59)
        self.engine.result = None
        with _LogCapture() as cap:
            self.adapter.evaluate_degradation()
        self.assertTrue(self.adapter.cbbc_magnet_degraded)
        events = [r.__dict__.get("event") for r in cap.records]
        self.assertIn("recovery_failed", events)

    def test_recovery_failed_when_snapshot_provider_returns_none(self) -> None:
        self.last_refresh = None
        self.adapter.evaluate_degradation()
        self.last_refresh = datetime(2025, 1, 5, 9, 59)

        # Replace the snapshot provider with one that returns None.
        adapter = MagnetSignalAdapter(
            calculator=self.engine,
            config=_Cfg(),
            clock=self.clock,
            last_refresh_provider=lambda: self.last_refresh,
            snapshot_provider=lambda: None,
        )
        adapter._cbbc_magnet_degraded = True
        with _LogCapture() as cap:
            adapter.evaluate_degradation()
        self.assertTrue(adapter.cbbc_magnet_degraded)
        events = [r.__dict__.get("event") for r in cap.records]
        self.assertIn("recovery_failed", events)

    def test_mark_degraded_due_to_external_failure_emits_one_log(self) -> None:
        with _LogCapture() as cap:
            self.adapter.mark_degraded_due_to_external_failure(reason="boom")
            # Second call must be idempotent (no log).
            self.adapter.mark_degraded_due_to_external_failure(reason="boom2")
        events = [r.__dict__.get("event") for r in cap.records]
        self.assertEqual(events.count("degraded_no_data"), 1)
        self.assertTrue(self.adapter.cbbc_magnet_degraded)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
