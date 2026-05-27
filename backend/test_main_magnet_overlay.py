"""Tests for the FastAPI /api/state + magnet_overlay WS hooks (task 9.3)."""
from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime
from pathlib import Path


# ABI-safe stubs (pandas/numpy/futu).
def _ensure(name: str) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


def _try_import(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


if not _try_import("pandas"):
    _ensure("pandas").Series = type("Series", (), {})
    _ensure("pandas").DataFrame = type("DataFrame", (), {})
if not _try_import("numpy"):
    _ensure("numpy").nan = float("nan")

if not _try_import("futu_data"):
    futu_mod = _ensure("futu_data")
    futu_mod.FutuDataSource = type(
        "FutuDataSource",
        (object,),
        {
            "__init__": lambda self, *a, **kw: None,
            "is_connected": False,
        },
    )
    futu_mod.calc_rsi = lambda *a, **kw: None
    futu_mod.calc_vwap = lambda *a, **kw: None


if not _try_import("futu_trader"):
    ft_mod = _ensure("futu_trader")
    ft_mod.FutuTrader = type(
        "FutuTrader",
        (object,),
        {
            "__init__": lambda self, *a, **kw: None,
            "is_connected": False,
            "real_unlocked_today": False,
            "trade_env_date": None,
            "get_trade_env": lambda self: "SIMULATE",
        },
    )

if not _try_import("market_regime"):
    _ensure("market_regime").classify_market_regime = lambda *a, **kw: None

if not _try_import("momentum_filter"):
    _ensure("momentum_filter").get_momentum_filter_reasons = lambda *a, **kw: []

if not _try_import("trend_filter"):
    tf = _ensure("trend_filter")
    tf.CUM_TREND_BOUNDARY_POINTS = 30.0
    tf.get_cum_trend_boundary_filter_reasons = lambda *a, **kw: []
    tf.get_cum_trend_filter_reasons = lambda *a, **kw: []

if not _try_import("trade_log_store"):
    tls = _ensure("trade_log_store")
    tls.append_trade_log = lambda r: None
    tls.load_trade_log = lambda: []
    tls.load_today_trade_log = lambda: []

if not _try_import("strategy_state_store"):
    sss = _ensure("strategy_state_store")
    sss.load_strategy_state = lambda: {}
    sss.save_strategy_state = lambda d: None

if not _try_import("backtest_service"):
    bs = _ensure("backtest_service")
    bs.run_backtest = lambda payload: None

sys.path.insert(0, str(Path(__file__).resolve().parent))


import main  # noqa: E402
from cbbc_calculator import HistBucket, MagnetResult  # noqa: E402
from cbbc_storage import CbbcRecord, CbbcSnapshot  # noqa: E402
from datetime import date  # noqa: E402


def _make_result(*, hsi_spot=20000.0, hsi_spot_stale=False, decay=300.0,
                 nearest_bull=80.0, nearest_bear=80.0):
    return MagnetResult(
        generated_at_hk=datetime(2025, 1, 6, 14, 30),
        hsi_spot=hsi_spot,
        hsi_spot_stale=hsi_spot_stale,
        decay_points=decay,
        magnet_bias=0.2,
        magnet_pull_bull=1_000_000.0,
        magnet_pull_bear=2_000_000.0,
        histogram=(
            HistBucket(bucket_low=75.0, bucket_high=80.0, pull_hkd=500.0),
            HistBucket(bucket_low=80.0, bucket_high=85.0, pull_hkd=1500.0),
        ),
        record_count=2,
        skipped_count=0,
        nearest_bull_distance_pts=nearest_bull,
        nearest_bear_distance_pts=nearest_bear,
    )


def _make_snapshot():
    rec_bull = CbbcRecord(
        issuer="HSBC", code="HK.50001", call_level=19920.0,
        outstanding_shares=1_000_000.0, er_ratio=10000.0, direction="bull",
        listing_date=date(2024, 1, 1), maturity_date=date(2030, 12, 31),
        underlying="HSI", snapshot_date=date(2025, 1, 6),
    )
    rec_bear = CbbcRecord(
        issuer="HSBC", code="HK.50002", call_level=20080.0,
        outstanding_shares=1_000_000.0, er_ratio=10000.0, direction="bear",
        listing_date=date(2024, 1, 1), maturity_date=date(2030, 12, 31),
        underlying="HSI", snapshot_date=date(2025, 1, 6),
    )
    rec_far = CbbcRecord(
        issuer="HSBC", code="HK.50003", call_level=22000.0,
        outstanding_shares=1_000_000.0, er_ratio=10000.0, direction="bull",
        listing_date=date(2024, 1, 1), maturity_date=date(2030, 12, 31),
        underlying="HSI", snapshot_date=date(2025, 1, 6),
    )
    return CbbcSnapshot(
        snapshot_date=date(2025, 1, 6), records=(rec_bull, rec_bear, rec_far)
    )


class _StubEngine:
    """Minimal stand-in for the real engine in the magnet overlay tests."""

    def __init__(self, *, layer_enabled: bool, degraded: bool, result, snapshot) -> None:
        self.cbbc_magnet_layer_enabled = layer_enabled
        self.cbbc_magnet_decay_points = 300.0
        self.cbbc_dense_band_pull_share = 0.40
        self.cbbc_dense_band_threshold_pts = 150.0
        self.cbbc_intraday_polling_suspended = False
        self.cbbc_intraday_poll_interval_seconds = 60.0
        self._result = result
        self._snapshot = snapshot
        self._degraded = degraded

    def _cbbc_magnet_is_degraded(self) -> bool:
        return self._degraded

    def _cbbc_latest_result(self):
        return self._result

    def _cbbc_data_service_snapshot(self):
        return self._snapshot


class BuildMagnetOverlayPayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        # Stash the real engine so we can restore it after each test.
        self._real_engine = main.engine

    def tearDown(self) -> None:
        main.engine = self._real_engine
        main._RECENT_VETOES.clear()

    def test_normal_mode_emits_call_levels_and_histogram(self) -> None:
        main.engine = _StubEngine(
            layer_enabled=True, degraded=False,
            result=_make_result(), snapshot=_make_snapshot(),
        )
        payload = main._build_magnet_overlay_payload()
        self.assertEqual(payload.decay_points, 300.0)
        self.assertFalse(payload.cbbc_magnet_degraded)
        self.assertFalse(payload.hsi_spot_stale)
        # Far record (22000) outside the decay band is dropped; the two
        # near records remain.
        codes = {cl.code for cl in payload.call_levels}
        self.assertIn("HK.50001", codes)
        self.assertIn("HK.50002", codes)
        self.assertNotIn("HK.50003", codes)
        self.assertEqual(len(payload.histogram), 2)
        # Dense-band threshold flows through to the payload (R8.2).
        self.assertEqual(payload.dense_band_threshold_pts, 150.0)


    def test_degraded_hides_decay_points(self) -> None:
        main.engine = _StubEngine(
            layer_enabled=True, degraded=True,
            result=_make_result(), snapshot=_make_snapshot(),
        )
        payload = main._build_magnet_overlay_payload()
        # decay_points=None in degraded mode tells the frontend to hide overlay.
        self.assertIsNone(payload.decay_points)
        self.assertTrue(payload.cbbc_magnet_degraded)

    def test_layer_disabled_hides_decay_points(self) -> None:
        main.engine = _StubEngine(
            layer_enabled=False, degraded=False,
            result=_make_result(), snapshot=_make_snapshot(),
        )
        payload = main._build_magnet_overlay_payload()
        self.assertIsNone(payload.decay_points)

    def test_hsi_stale_emits_stale_flag_and_no_call_levels(self) -> None:
        main.engine = _StubEngine(
            layer_enabled=True, degraded=False,
            result=_make_result(hsi_spot_stale=True),
            snapshot=_make_snapshot(),
        )
        payload = main._build_magnet_overlay_payload()
        self.assertTrue(payload.hsi_spot_stale)
        # When the spot is stale, no call-levels are emitted (frontend will
        # render the "magnet data unavailable" banner instead).
        self.assertEqual(payload.call_levels, [])
        self.assertEqual(payload.histogram, [])

    def test_recent_vetoes_pushed_into_payload(self) -> None:
        main.engine = _StubEngine(
            layer_enabled=True, degraded=False,
            result=_make_result(), snapshot=_make_snapshot(),
        )
        # Record two vetoes via the helper.
        consult_a = types.SimpleNamespace(
            vetoed_by_cbbc_magnet=True,
            ts_hk="2025-01-06 14:30:00",
            extreme_direction="BULL",
            reason_code="cbbc_dense_band_above",
        )
        consult_b = types.SimpleNamespace(
            vetoed_by_cbbc_magnet=True,
            ts_hk="2025-01-06 15:01:00",
            extreme_direction="BEAR",
            reason_code="cbbc_dense_band_below",
        )
        main._record_magnet_veto(consult_a)
        main._record_magnet_veto(consult_b)
        payload = main._build_magnet_overlay_payload()
        self.assertEqual(len(payload.recent_vetoes), 2)
        self.assertEqual(payload.recent_vetoes[0].reason_code, "cbbc_dense_band_above")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
