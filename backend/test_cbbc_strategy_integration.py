"""
strategy.py × CBBC magnet signal layer 集成测试 (cbbc-magnet-signal task 7.4).

聚焦点：
- 仅测试 task 7.2 / 7.3 接入的 5 个行为：
  1. layer disabled → 不影响入场（_maybe_apply_cbbc_magnet_veto 返回 False）。
  2. cbbc_dense_band_above / cbbc_dense_band_below 否决路径写"跳过"日志、上报 last_magnet_consult。
  3. magnet 不可用（fail-safe）→ 不否决，主策略继续。
  4. 适配器抛未捕获异常 → 主循环不阻塞，自动 mark degraded。
  5. /api/state 字段（StrategyState 中 6 个 cbbc 字段）正确填充。

为绕过本机 pandas / numpy / futu ABI 不一致，在 import strategy 之前用 sys.modules
注入轻量 stub。这些 stub 仅满足 strategy 顶层 import 需要的最小符号。
"""
from __future__ import annotations

import math
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch


# --------------------------------------------------------------------------- #
# 在 import strategy 之前，先 stub 掉本机环境里 ABI 不一致的依赖。
# 注意：本测试不调用任何真实的 pandas / numpy / futu API，纯 Python 层走通。
# --------------------------------------------------------------------------- #


def _ensure_module(name: str) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


def _try_real_import(name: str) -> bool:
    """Return True if the real module imports cleanly. If so, leave it alone."""
    try:
        __import__(name)
        return True
    except Exception:  # noqa: BLE001
        return False


def _stub_only_if_real_unavailable(name: str, attrs: dict[str, Any]) -> types.ModuleType:
    """Install a stub for ``name`` only if importing the real module fails.

    This keeps other test files (e.g. test_trade_log_store) able to import the
    real symbol; we only patch when the real one would crash on this dev box
    (pandas/numpy ABI mismatch path).
    """
    if _try_real_import(name):
        return sys.modules[name]
    mod = _ensure_module(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


# pandas / numpy stubs（strategy.py 不直接调用，但通过 futu_data / cbbc_storage 间接 import）。
_stub_only_if_real_unavailable("pandas", {
    "Series": type("Series", (), {}),
    "DataFrame": type("DataFrame", (), {}),
    "Timestamp": type("Timestamp", (), {}),
    "NaT": None,
    "isna": lambda x: x is None,
})
_stub_only_if_real_unavailable("numpy", {
    "nan": float("nan"),
    "ndarray": type("ndarray", (), {}),
    "float64": float,
    "int64": int,
})

# futu stubs（futu_data / futu_trader 顶层 from futu import ...）。
_futu_stub = _ensure_module("futu")
for _name in (
    "OpenQuoteContext",
    "OpenSecTradeContext",
    "OpenHKTradeContext",
    "RET_OK",
    "RET_ERROR",
    "KLType",
    "AuType",
    "SubType",
    "StockQuoteHandlerBase",
    "CurKlineHandlerBase",
    "TrdEnv",
    "TrdMarket",
    "OrderType",
    "TrdSide",
    "ModifyOrderOp",
    "OrderStatus",
):
    setattr(_futu_stub, _name, type(_name, (object,), {}))
_futu_stub.RET_OK = "OK"
_futu_stub.RET_ERROR = "ERR"

# 直接把 futu_data / futu_trader 替换为不会触发 import pandas 的 stub。
_stub_only_if_real_unavailable(
    "futu_data",
    {
        "FutuDataSource": type(
            "FutuDataSource",
            (object,),
            {
                "__init__": lambda self, *a, **kw: None,
                "connect": lambda self: None,
                "disconnect": lambda self: None,
                "is_connected": False,
            },
        ),
        "calc_rsi": lambda *a, **kw: None,
        "calc_vwap": lambda *a, **kw: None,
    },
)
_stub_only_if_real_unavailable(
    "futu_trader",
    {
        "FutuTrader": type(
            "FutuTrader",
            (object,),
            {
                "__init__": lambda self, *a, **kw: None,
                "connect": lambda self: None,
                "disconnect": lambda self: None,
                "is_connected": False,
                "real_unlocked_today": False,
                "trade_env_date": None,
                "get_trade_env": lambda self: "SIMULATE",
            },
        ),
    },
)

_stub_only_if_real_unavailable(
    "market_regime",
    {"classify_market_regime": lambda *a, **kw: None},
)
_stub_only_if_real_unavailable(
    "momentum_filter",
    {"get_momentum_filter_reasons": lambda *a, **kw: []},
)
_stub_only_if_real_unavailable(
    "trend_filter",
    {
        "CUM_TREND_BOUNDARY_POINTS": 30.0,
        "get_cum_trend_boundary_filter_reasons": lambda *a, **kw: [],
        "get_cum_trend_filter_reasons": lambda *a, **kw: [],
    },
)
_stub_only_if_real_unavailable(
    "trade_log_store",
    {
        "append_trade_log": lambda record: None,
        "load_trade_log": lambda: [],
        "load_today_trade_log": lambda: [],
    },
)
_stub_only_if_real_unavailable(
    "strategy_state_store",
    {
        "load_strategy_state": lambda: {},
        "save_strategy_state": lambda data: None,
    },
)

# strategy.py 还会 import futu_data / futu_trader 这两个项目内模块，里面会
# 触发 import pandas as pd / import numpy as np。让 stub 先存在即可。
sys.path.insert(0, str(Path(__file__).resolve().parent))


# 现在可以安全 import strategy。
import strategy  # noqa: E402
from cbbc_calculator import HistBucket, MagnetEngine, MagnetResult  # noqa: E402
from cbbc_signal_adapter import ConsultDecision, MagnetSignalAdapter  # noqa: E402
from models import PositionType, StrategyState  # noqa: E402


# --------------------------------------------------------------------------- #
# 测试夹具
# --------------------------------------------------------------------------- #


def _build_engine() -> "strategy.HSIStrategyEngine":
    """构造一个 HSIStrategyEngine（futu_data / futu_trader 已被 sys.modules stub 替换）。"""
    return strategy.HSIStrategyEngine()


def _make_magnet_result(
    *,
    bias: float = 0.0,
    pull_bull: float = 0.0,
    pull_bear: float = 0.0,
    nearest_bull: float | None = None,
    nearest_bear: float | None = None,
    hsi_spot_stale: bool = False,
) -> MagnetResult:
    return MagnetResult(
        generated_at_hk=datetime(2025, 1, 1, 14, 30, 0),
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


class _StubMagnetEngine:
    """不依赖快照与 HSI feed 的最小 MagnetEngine。直接返回固定 ``MagnetResult``。"""

    def __init__(self, result: MagnetResult | None) -> None:
        self._result = result
        self.update_calls: list[tuple[str, Any]] = []

    def latest(self) -> MagnetResult | None:
        return self._result

    def update_hsi_spot(self, price: float, ts_hk: datetime) -> None:
        self.update_calls.append(("hsi_spot", price))

    def update_snapshot(self, snapshot: Any) -> None:
        self.update_calls.append(("snapshot", snapshot))

    def update_decay_points(self, value: float) -> None:
        self.update_calls.append(("decay_points", value))


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


class CbbcMagnetVetoIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """task 7.3 / 7.4 — extreme 分支接入磁吸咨询，主循环 fail-safe."""

    async def asyncSetUp(self) -> None:
        self.engine = _build_engine()
        self.engine.cbbc_magnet_layer_enabled = True
        # 防止 _emit_strategy_disabled_skip 落到真实 trade-log 文件。
        self._emitted_records: list[Any] = []

        async def _capture(record):
            self._emitted_records.append(record)

        self.engine._emit_trade_record = _capture  # type: ignore[assignment]

    def _install_stub(self, result: MagnetResult | None) -> _StubMagnetEngine:
        stub = _StubMagnetEngine(result)
        self.engine._cbbc_magnet_engine = stub
        # 重新构造一个 adapter 指向新 stub，以隔离每个用例的状态。
        self.engine._cbbc_magnet_adapter = MagnetSignalAdapter(
            calculator=stub, config=self.engine
        )
        return stub

    # -------- layer disabled ---------- #
    async def test_layer_disabled_does_not_block_extreme_entry(self) -> None:
        self.engine.cbbc_magnet_layer_enabled = False
        # adapter 仍然存在，但 layer 关闭时 consult_for_extreme 直接返回不否决的 fail-safe。
        self._install_stub(_make_magnet_result(bias=0.5, pull_bear=1.0))
        vetoed = await self.engine._maybe_apply_cbbc_magnet_veto(
            PositionType.BULL, 20000.0, 18.0, "2025-01-02 10:00:00", "极度超卖"
        )
        self.assertFalse(vetoed)
        self.assertEqual(self._emitted_records, [])

    # -------- dense band veto ---------- #
    async def test_dense_band_above_vetoes_bull_extreme_entry(self) -> None:
        # BULL 反转看 bear 距离。bear 距离 50pt（< 阈值 150）+ pull 全在 bear 端 → 否决。
        result = _make_magnet_result(
            bias=0.8, pull_bull=0.0, pull_bear=1_000_000.0,
            nearest_bull=400.0, nearest_bear=50.0,
        )
        self._install_stub(result)
        vetoed = await self.engine._maybe_apply_cbbc_magnet_veto(
            PositionType.BULL, 20000.0, 18.0, "2025-01-02 10:00:00", "极度超卖"
        )
        self.assertTrue(vetoed)
        # 一条 emit + last_magnet_consult 已落地
        self.assertEqual(len(self._emitted_records), 1)
        rec = self.engine._last_magnet_consult
        assert rec is not None
        self.assertEqual(rec.extreme_direction, "BULL")
        self.assertTrue(rec.vetoed_by_cbbc_magnet)
        self.assertEqual(rec.reason_code, "cbbc_dense_band_above")

    async def test_dense_band_below_vetoes_bear_extreme_entry(self) -> None:
        result = _make_magnet_result(
            bias=-0.8, pull_bull=1_000_000.0, pull_bear=0.0,
            nearest_bull=50.0, nearest_bear=400.0,
        )
        self._install_stub(result)
        vetoed = await self.engine._maybe_apply_cbbc_magnet_veto(
            PositionType.BEAR, 20000.0, 82.0, "2025-01-02 14:00:00", "极度超买"
        )
        self.assertTrue(vetoed)
        rec = self.engine._last_magnet_consult
        assert rec is not None
        self.assertEqual(rec.reason_code, "cbbc_dense_band_below")

    # -------- clear path ---------- #
    async def test_dense_band_clear_does_not_veto(self) -> None:
        result = _make_magnet_result(
            bias=0.0, pull_bull=500.0, pull_bear=500.0,
            nearest_bull=400.0, nearest_bear=400.0,  # 远 > 阈值
        )
        self._install_stub(result)
        vetoed = await self.engine._maybe_apply_cbbc_magnet_veto(
            PositionType.BULL, 20000.0, 18.0, "2025-01-02 10:00:00", "极度超卖"
        )
        self.assertFalse(vetoed)
        rec = self.engine._last_magnet_consult
        assert rec is not None
        self.assertEqual(rec.reason_code, "cbbc_dense_band_clear")
        self.assertFalse(rec.vetoed_by_cbbc_magnet)

    # -------- fail-safe ---------- #
    async def test_unavailable_magnet_does_not_veto(self) -> None:
        # latest() 返回 None → 不可用 fail-safe。
        self._install_stub(None)
        vetoed = await self.engine._maybe_apply_cbbc_magnet_veto(
            PositionType.BULL, 20000.0, 18.0, "2025-01-02 10:00:00", "极度超卖"
        )
        self.assertFalse(vetoed)

    async def test_stale_hsi_spot_is_unavailable(self) -> None:
        result = _make_magnet_result(
            bias=0.8, pull_bear=1.0, nearest_bull=10.0, nearest_bear=10.0,
            hsi_spot_stale=True,
        )
        self._install_stub(result)
        vetoed = await self.engine._maybe_apply_cbbc_magnet_veto(
            PositionType.BULL, 20000.0, 18.0, "2025-01-02 10:00:00", "极度超卖"
        )
        self.assertFalse(vetoed)
        rec = self.engine._last_magnet_consult
        assert rec is not None
        self.assertEqual(rec.reason_code, "cbbc_magnet_consult_unavailable")

    # -------- adapter raises ---------- #
    async def test_adapter_exception_marks_degraded_and_does_not_block(self) -> None:
        class _Boom:
            def consult_for_extreme(self, *args, **kwargs):
                raise RuntimeError("boom")

            cbbc_magnet_degraded = False

            def mark_degraded_due_to_external_failure(self, **kwargs):
                self.cbbc_magnet_degraded = True

        self.engine._cbbc_magnet_adapter = _Boom()
        vetoed = await self.engine._maybe_apply_cbbc_magnet_veto(
            PositionType.BULL, 20000.0, 18.0, "2025-01-02 10:00:00", "极度超卖"
        )
        self.assertFalse(vetoed)  # fail-safe, do not block entry
        self.assertTrue(self.engine._cbbc_magnet_adapter.cbbc_magnet_degraded)

    # -------- adapter is missing ---------- #
    async def test_missing_adapter_does_not_veto(self) -> None:
        self.engine._cbbc_magnet_adapter = None
        vetoed = await self.engine._maybe_apply_cbbc_magnet_veto(
            PositionType.BULL, 20000.0, 18.0, "2025-01-02 10:00:00", "极度超卖"
        )
        self.assertFalse(vetoed)


class CbbcStateSnapshotTest(unittest.TestCase):
    """task 7.1 / 7.3 — /api/state 字段补全 + last_magnet_consult 上报."""

    def setUp(self) -> None:
        self.engine = _build_engine()
        # Defensive reset: the real backend/runtime_config.json on disk may
        # have layer_enabled=True from a previous live run. Force defaults
        # so the test asserts correct semantics in isolation.
        self.engine.cbbc_magnet_layer_enabled = False
        self.engine._last_magnet_consult = None

    def test_default_state_includes_cbbc_fields(self) -> None:
        with patch.object(self.engine, "sync_exit_order_if_filled"):
            state: StrategyState = self.engine.get_state(sync_exit=False)
        self.assertFalse(state.cbbc_magnet_layer_enabled)
        # adapter exists but layer disabled → not reported as degraded
        self.assertFalse(state.cbbc_magnet_degraded)
        self.assertIsNone(state.cbbc_magnet_bias)
        self.assertIsNone(state.cbbc_nearest_bull_distance_pts)
        self.assertIsNone(state.cbbc_nearest_bear_distance_pts)
        self.assertIsNone(state.last_magnet_consult)

    def test_state_reports_latest_magnet_metrics(self) -> None:
        result = _make_magnet_result(
            bias=0.42, pull_bear=1.0, nearest_bull=120.0, nearest_bear=80.0,
        )
        stub = _StubMagnetEngine(result)
        self.engine._cbbc_magnet_engine = stub
        with patch.object(self.engine, "sync_exit_order_if_filled"):
            state = self.engine.get_state(sync_exit=False)
        self.assertAlmostEqual(state.cbbc_magnet_bias, 0.42, places=6)
        self.assertAlmostEqual(state.cbbc_nearest_bull_distance_pts, 120.0, places=6)
        self.assertAlmostEqual(state.cbbc_nearest_bear_distance_pts, 80.0, places=6)


class CbbcRuntimeConfigSyncTest(unittest.TestCase):
    """task 7.2 — update_config 同步到 MagnetEngine."""

    def setUp(self) -> None:
        self.engine = _build_engine()

    def test_update_config_calls_engine_update_decay_points(self) -> None:
        stub = _StubMagnetEngine(None)
        self.engine._cbbc_magnet_engine = stub
        with patch.object(self.engine, "_save_runtime_config"):
            self.engine.update_config(cbbc_magnet_decay_points=400.0)
        # The engine should have received exactly one decay_points push.
        self.assertEqual(self.engine.cbbc_magnet_decay_points, 400.0)
        self.assertIn(("decay_points", 400.0), stub.update_calls)

    def test_update_config_swallows_engine_failure(self) -> None:
        class _BadEngine:
            def update_decay_points(self, value):
                raise RuntimeError("engine bad")

        self.engine._cbbc_magnet_engine = _BadEngine()
        with patch.object(self.engine, "_save_runtime_config"):
            # Should not propagate the engine error.
            self.engine.update_config(cbbc_magnet_decay_points=500.0)
        self.assertEqual(self.engine.cbbc_magnet_decay_points, 500.0)


class CbbcHsiSpotFeedTest(unittest.TestCase):
    """task 7.2 — _feed_cbbc_magnet_engine_hsi_spot 不会因异常阻塞主循环."""

    def setUp(self) -> None:
        self.engine = _build_engine()

    def test_feed_swallows_exception(self) -> None:
        class _Throws:
            def update_hsi_spot(self, price, ts_hk):
                raise RuntimeError("nope")

        self.engine._cbbc_magnet_engine = _Throws()
        # Should not raise.
        self.engine._feed_cbbc_magnet_engine_hsi_spot(20000.0)

    def test_feed_drops_non_finite(self) -> None:
        stub = _StubMagnetEngine(None)
        self.engine._cbbc_magnet_engine = stub
        self.engine._feed_cbbc_magnet_engine_hsi_spot(float("nan"))
        self.engine._feed_cbbc_magnet_engine_hsi_spot(float("inf"))
        # No update should have been recorded.
        self.assertEqual(stub.update_calls, [])

    def test_feed_pushes_finite_price(self) -> None:
        stub = _StubMagnetEngine(None)
        self.engine._cbbc_magnet_engine = stub
        self.engine._feed_cbbc_magnet_engine_hsi_spot(20000.5)
        self.assertEqual(len(stub.update_calls), 1)
        kind, value = stub.update_calls[0]
        self.assertEqual(kind, "hsi_spot")
        self.assertAlmostEqual(value, 20000.5)


class CbbcMagnetDirectionGateTest(unittest.IsolatedAsyncioTestCase):
    """UX 增强 — 全局方向闸门:bias 明确指向反方向时阻止入场。"""

    async def asyncSetUp(self) -> None:
        self.engine = _build_engine()
        # Force defaults that the live runtime_config.json may have skewed.
        self.engine.cbbc_magnet_layer_enabled = True
        self.engine.cbbc_magnet_direction_gate_enabled = True
        self.engine.cbbc_magnet_direction_gate_threshold = 0.15

        self._records: list = []

        async def _capture(record):
            self._records.append(record)

        self.engine._emit_trade_record = _capture  # type: ignore[assignment]

    def _install_result(
        self,
        bias: float,
        *,
        nearest_bull: float = 200.0,
        nearest_bear: float = 50.0,  # default: bear closer (resistance for BULL)
    ) -> None:
        result = _make_magnet_result(
            bias=bias, pull_bull=1.0, pull_bear=1.0,
            nearest_bull=nearest_bull, nearest_bear=nearest_bear,
        )
        self.engine._cbbc_magnet_engine = _StubMagnetEngine(result)
        # Provide a flat day so day_move='flat' → resistance interpretation kicks in.
        self.engine.market_regime = types.SimpleNamespace(
            day_open=20000.0, current_price=20000.0,
        )  # type: ignore[assignment]

    def test_block_bull_when_resistance_above(self) -> None:
        # bias > 0 + nearest_bear < nearest_bull + HSI flat
        # → 上方街货阻力 → 阻止 BULL,不阻止 BEAR
        self._install_result(0.8, nearest_bull=200.0, nearest_bear=50.0)
        bull_reason = self.engine._cbbc_magnet_direction_gate_block_reason(
            PositionType.BULL
        )
        bear_reason = self.engine._cbbc_magnet_direction_gate_block_reason(
            PositionType.BEAR
        )
        self.assertIsNotNone(bull_reason)
        assert bull_reason is not None
        self.assertIn("警惕做多", bull_reason)
        self.assertIsNone(bear_reason)

    def test_fuel_scenario_bias_positive_but_hsi_breaking_up(self) -> None:
        # bias > 0 + nearest_bear < nearest_bull + HSI 强势上行
        # → 燃料场景 → 不阻止 BULL,顺势加速
        # Build a result whose hsi_spot is 20100 (already moved up from the
        # 20000 day_open we set below) so the gate sees day_move = 'up'.
        result = _make_magnet_result(
            bias=0.8, pull_bull=1.0, pull_bear=1.0,
            nearest_bull=200.0, nearest_bear=50.0,
        )
        # _make_magnet_result hardcodes hsi_spot=20000; build a fresh frozen
        # MagnetResult with the spot we need by replacing via dataclasses.
        import dataclasses
        result = dataclasses.replace(result, hsi_spot=20100.0)
        self.engine._cbbc_magnet_engine = _StubMagnetEngine(result)
        self.engine.market_regime = types.SimpleNamespace(
            day_open=20000.0, current_price=20100.0,
        )  # type: ignore[assignment]
        bull_reason = self.engine._cbbc_magnet_direction_gate_block_reason(
            PositionType.BULL
        )
        self.assertIsNone(bull_reason)

    def test_fuel_scenario_bias_positive_but_already_passed_through(self) -> None:
        # bias > 0 但 nearest_bull < nearest_bear → 价格已穿越熊证密集带
        # → 不阻止任何方向
        self._install_result(0.8, nearest_bull=50.0, nearest_bear=200.0)
        self.assertIsNone(
            self.engine._cbbc_magnet_direction_gate_block_reason(PositionType.BULL)
        )
        self.assertIsNone(
            self.engine._cbbc_magnet_direction_gate_block_reason(PositionType.BEAR)
        )

    def test_block_bear_when_support_below(self) -> None:
        # bias < 0 + nearest_bull < nearest_bear + HSI flat
        # → 下方街货支撑 → 阻止 BEAR,不阻止 BULL
        self._install_result(-0.6, nearest_bull=50.0, nearest_bear=200.0)
        bear_reason = self.engine._cbbc_magnet_direction_gate_block_reason(
            PositionType.BEAR
        )
        bull_reason = self.engine._cbbc_magnet_direction_gate_block_reason(
            PositionType.BULL
        )
        self.assertIsNotNone(bear_reason)
        assert bear_reason is not None
        self.assertIn("警惕做空", bear_reason)
        self.assertIsNone(bull_reason)

    def test_neutral_bias_does_not_block(self) -> None:
        # |bias| < threshold → 中性 → 不阻止
        self._install_result(0.05)
        self.assertIsNone(
            self.engine._cbbc_magnet_direction_gate_block_reason(PositionType.BULL)
        )
        self.assertIsNone(
            self.engine._cbbc_magnet_direction_gate_block_reason(PositionType.BEAR)
        )

    def test_gate_disabled_does_not_block_even_at_strong_bias(self) -> None:
        self.engine.cbbc_magnet_direction_gate_enabled = False
        self._install_result(0.9)
        self.assertIsNone(
            self.engine._cbbc_magnet_direction_gate_block_reason(PositionType.BULL)
        )

    def test_layer_disabled_does_not_block(self) -> None:
        self.engine.cbbc_magnet_layer_enabled = False
        self._install_result(0.9)
        self.assertIsNone(
            self.engine._cbbc_magnet_direction_gate_block_reason(PositionType.BULL)
        )

    def test_no_result_does_not_block(self) -> None:
        self.engine._cbbc_magnet_engine = _StubMagnetEngine(None)
        self.assertIsNone(
            self.engine._cbbc_magnet_direction_gate_block_reason(PositionType.BULL)
        )

    def test_stale_hsi_does_not_block(self) -> None:
        result = _make_magnet_result(
            bias=0.9, nearest_bull=10.0, nearest_bear=10.0,
            hsi_spot_stale=True,
        )
        self.engine._cbbc_magnet_engine = _StubMagnetEngine(result)
        self.assertIsNone(
            self.engine._cbbc_magnet_direction_gate_block_reason(PositionType.BULL)
        )

    async def test_maybe_block_emits_skip_record_and_returns_true(self) -> None:
        # Resistance scenario: bear closer + flat HSI → blocks BULL.
        self._install_result(0.8, nearest_bull=200.0, nearest_bear=50.0)
        blocked = await self.engine._maybe_block_by_magnet_direction_gate(
            PositionType.BULL, 25500.0, 18.0, "2026-01-02 10:00:00", "极度超卖"
        )
        self.assertTrue(blocked)
        self.assertEqual(len(self._records), 1)
        self.assertIn("cbbc_magnet_direction_gate", self._records[0].message)

    async def test_maybe_block_returns_false_when_aligned(self) -> None:
        # bias positive resistance → BEAR aligned → not blocked
        self._install_result(0.8, nearest_bull=200.0, nearest_bear=50.0)
        blocked = await self.engine._maybe_block_by_magnet_direction_gate(
            PositionType.BEAR, 25500.0, 80.0, "2026-01-02 10:00:00", "极度超买"
        )
        self.assertFalse(blocked)
        self.assertEqual(self._records, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
