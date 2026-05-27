"""
CBBC 街货磁吸信号适配器（cbbc-magnet-signal）。

本文件提供 ``Magnet_Signal_Adapter``：

- ``ConsultDecision``：``HSIStrategyEngine`` extreme 分支咨询磁吸时的不可变决策对象，
  包含 7 种 ``reason_code`` 枚举（与 design.md 中 Magnet_Signal_Adapter 一节定义一致）。
- ``ClockProtocol`` / ``SystemClock``：注入 HK 当地时间的钩子，便于测试。
- ``RuntimeConfigView``：适配器只读的配置接口（duck-typed Protocol），不直接依赖
  ``runtime_config_store``，方便单元测试以小型 fake 替换。
- ``MagnetSignalAdapter``：注入 ``MagnetEngine`` / ``RuntimeConfigView`` / ``ClockProtocol``，
  以及 task 6.3 新增的 ``last_refresh_provider`` / ``snapshot_provider`` 两个回调。

降级生命周期（task 6.3，R10.1–R10.5）由本类内部维护：

- ``evaluate_degradation(now_hk)``：每次 ``consult_for_extreme`` 进入主路径前调用一次，
  根据 ``last_refresh_provider()`` 判断是否需要进入或退出降级。
- ``last_refresh_provider() is None`` 时（尚未挂入 ``CbbcDataService``，常见于单元测试），
  生命周期检查"静默禁用"——既不会进入降级也不会尝试恢复，保持 task 6.2 的原行为。
- ``now_hk - last_refresh > 36h`` 时，若未处于降级则恰好写一条
  ``level=WARN, source=cbbc_magnet, event=degraded_no_data`` 日志并把
  ``cbbc_magnet_degraded`` 置 ``True``；若已处于降级则不重复写日志。
- ``now_hk - last_refresh ≤ 36h`` 且当前处于降级时，尝试一次显式恢复初始化：
  (a) 通过 ``snapshot_provider()`` 取最新快照 → (b) 调用 ``calculator.update_snapshot``
  强制重算 → (c) 调用 ``calculator.latest()`` 取得新结果。任一步抛异常、产出无效结果
  （``None`` 或 ``hsi_spot_stale=True``）或整体超过 5 秒都视为恢复失败：写一条
  ``level=WARN, event=recovery_failed``，保持降级。下一次 evaluate_degradation 触发时再试。
- 三步全部成功且在 5 秒内完成时，**先**写一条 ``level=INFO, event=recovery_initialized``、
  **再**把 ``cbbc_magnet_degraded`` 翻为 ``False``。

``mark_degraded_due_to_external_failure()`` 提供 R10.6 的入口：当
``HSIStrategyEngine`` 在 try/except 中包裹 consult 调用并捕获到任何未处理异常时，
调用该方法立即把适配器置为降级（同时只在状态转换时写一条 ``degraded_no_data``）。

``consult_for_extreme`` 在 task 6.2 已实现完整决策矩阵（与 design.md /
``Magnet_Signal_Adapter`` 表格、R5.1–R5.12、R9.2 完全对齐）：

    ┌─────────────────────────────────────────────────────────────────────────┐
    │ 状态                                  │ 决策                              │
    ├─────────────────────────────────────────────────────────────────────────┤
    │ cbbc_magnet_layer_enabled=False      │ vetoed=False,                     │
    │                                       │ reason=cbbc_magnet_layer_disabled,│
    │                                       │ 不写 consult 日志                  │
    │ cbbc_magnet_degraded=True            │ vetoed=False,                     │
    │                                       │ reason=cbbc_magnet_degraded,      │
    │                                       │ 写 cbbc_magnet_consult_unavailable │
    │ result/HSI/Bias/Call_Level 不可用,    │ vetoed=False,                     │
    │ 或 pull_bull+pull_bear=0             │ reason=cbbc_magnet_consult_unavail │
    │                                       │ 写 cbbc_magnet_consult_unavailable │
    │ 反向距离 > 阈值                        │ vetoed=False,                     │
    │                                       │ reason=cbbc_dense_band_clear      │
    │ 反向距离 ≤ 阈值 且 share ≥ 阈值        │ vetoed=True,                      │
    │                                       │ reason=cbbc_dense_band_above /    │
    │                                       │        cbbc_dense_band_below      │
    │ 反向距离 ≤ 阈值 但 share < 阈值        │ vetoed=False,                     │
    │                                       │ reason=cbbc_dense_band_pull_share_│
    │                                       │        below                      │
    └─────────────────────────────────────────────────────────────────────────┘

降级生命周期（task 6.3）已实现：``evaluate_degradation`` 在每次 ``consult_for_extreme``
进入主路径前调用一次，按 R10.1–R10.5 的规则维护 ``_cbbc_magnet_degraded``。
``mark_degraded_due_to_external_failure`` 用于上层捕获到未处理异常后强制降级（R10.6）。

不在本文件做任何 I/O（除 ``logging``）、不持有全局状态。
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Literal, Protocol


if TYPE_CHECKING:
    # 仅在类型检查阶段引入；运行时 ``cbbc_calculator`` / ``cbbc_storage`` 可能尚未到位
    # （task 3.x / 2.x），因此通过字符串前向引用避免硬依赖（见 task 6.1 描述）。
    from cbbc_calculator import MagnetEngine, MagnetResult  # noqa: F401
    from cbbc_storage import CbbcSnapshot  # noqa: F401


__all__ = [
    "ConsultReasonCode",
    "ConsultDecision",
    "ClockProtocol",
    "SystemClock",
    "RuntimeConfigView",
    "MagnetSignalAdapter",
]


# 模块级 logger；与 R5.7 / R5.3 / R5.8 / R5.11 中要求的 ``source=cbbc_magnet``
# 字段一一对应。
logger = logging.getLogger("cbbc_magnet")


# 与 design.md ``Magnet_Signal_Adapter`` 一节中的 Literal 完全一致。
ConsultReasonCode = Literal[
    "cbbc_dense_band_clear",
    "cbbc_dense_band_above",
    "cbbc_dense_band_below",
    "cbbc_dense_band_pull_share_below",
    "cbbc_magnet_consult_unavailable",
    "cbbc_magnet_layer_disabled",
    "cbbc_magnet_degraded",
]


# 当 Runtime_Config 写入的值越界 / 非有限 / 类型错误时，适配器自身做兜底回退
# （R5.7 / R5.3）。这两个常量与 ``runtime_config_store.CBBC_FIELD_DEFAULTS``
# 中的默认值一致；如需修改请同步更新两处。
_DEFAULT_DENSE_BAND_THRESHOLD_PTS: float = 150.0
_DEFAULT_DENSE_BAND_PULL_SHARE: float = 0.40

# 与 ``runtime_config_store._CBBC_FLOAT_RANGES`` 保持一致的有效区间。
_THRESHOLD_PTS_LOW: float = 10.0
_THRESHOLD_PTS_HIGH: float = 1000.0
_PULL_SHARE_LOW: float = 0.0
_PULL_SHARE_HIGH: float = 1.0


# 降级生命周期阈值与超时（task 6.3 / R10.1, R10.5）。
# - ``_DEGRADED_REFRESH_AGE_SECONDS``：``last_refresh_ts_hk`` 与 ``now_hk`` 之差严格大于
#   该值时进入降级；小于等于则尝试退出降级。36h = 36 * 3600 秒。
# - ``_RECOVERY_DEADLINE_SECONDS``：显式恢复初始化的总耗时上限。任一步异常或 5s 超时
#   均视为恢复失败。
_DEGRADED_REFRESH_AGE_SECONDS: float = 36.0 * 3600.0
_RECOVERY_DEADLINE_SECONDS: float = 5.0


@dataclass(frozen=True)
class ConsultDecision:
    """单次磁吸咨询的不可变结果。

    字段语义参考 design.md / requirements.md（R5.1, R5.4, R5.6, R5.8, R5.11）。
    ``magnet_available=False`` 表示当前快照不足以做出有效判定（例如 stale HSI、
    缺失 Magnet_Bias、layer 关闭、降级等）。
    """

    vetoed_by_cbbc_magnet: bool
    reason_code: ConsultReasonCode
    nearest_bull_distance_pts: float | None
    nearest_bear_distance_pts: float | None
    magnet_bias: float | None
    magnet_aligned_against_reversal: bool
    magnet_available: bool

    @classmethod
    def fail_safe(
        cls,
        reason: ConsultReasonCode = "cbbc_magnet_consult_unavailable",
    ) -> "ConsultDecision":
        """返回一个不否决的 fail-safe 决策。

        当组件抛出未捕获异常、layer 未启用或快照不可用时使用，确保主策略
        循环不会因辅助模块故障被阻断（R10.6）。
        """
        return cls(
            vetoed_by_cbbc_magnet=False,
            reason_code=reason,
            nearest_bull_distance_pts=None,
            nearest_bear_distance_pts=None,
            magnet_bias=None,
            magnet_aligned_against_reversal=False,
            magnet_available=False,
        )


class ClockProtocol(Protocol):
    """提供香港当地时间的钩子，便于测试期注入伪造时钟。"""

    def now_hk(self) -> datetime:  # pragma: no cover - Protocol stub
        ...


class SystemClock:
    """默认 ``ClockProtocol`` 实现，使用本机时间（假设运行环境已为 Asia/Hong_Kong）。

    实盘环境下可由调用方替换为基于 ``zoneinfo`` 的实现；此处保持最简，
    避免在骨架阶段引入额外依赖。
    """

    def now_hk(self) -> datetime:
        return datetime.now()


class RuntimeConfigView(Protocol):
    """适配器仅依赖以下三个只读字段（与 ``runtime_config_store`` 中
    ``CBBC_FIELD_DEFAULTS`` 保持名字一致）。"""

    cbbc_magnet_layer_enabled: bool
    cbbc_dense_band_threshold_pts: float
    cbbc_dense_band_pull_share: float


def _is_valid_float_in_range(
    value: Any,
    *,
    low: float,
    high: float,
) -> bool:
    """是否为闭区间 ``[low, high]`` 内的有限浮点（不接受 ``bool``）。"""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    f = float(value)
    if not math.isfinite(f):
        return False
    return low <= f <= high


class MagnetSignalAdapter:
    """``Magnet_Signal_Adapter``：在 extreme 反转入场前咨询 CBBC 磁吸。

    本类的职责（与 design.md / R5 / R9.2 一致）：

    1. 在 ``cbbc_magnet_layer_enabled=False`` 时立刻返回 layer-disabled 的
       fail-safe，**不**写 consult 日志（R9.2）。
    2. 在 ``cbbc_magnet_degraded=True`` 时返回 reason=cbbc_magnet_degraded 的
       fail-safe，并写一条 ``event=cbbc_magnet_consult_unavailable`` 日志。
    3. 在快照 / HSI / Magnet_Bias / Call_Level 任一不可用，或
       ``pull_bull + pull_bear == 0`` 时进入不可用路径（写
       ``event=cbbc_magnet_consult_unavailable``，不否决）。
    4. 否则按"反向距离 vs 阈值"+"反向 pull share vs 阈值"进行决策，写一条
       ``event=cbbc_magnet_consult`` 日志。

    ``cbbc_dense_band_threshold_pts`` / ``cbbc_dense_band_pull_share`` 越界、
    非有限或类型错误时，适配器自身回退到默认值并写一条 WARN 日志（R5.7 / R5.3）。
    """

    def __init__(
        self,
        *,
        calculator: "MagnetEngine | Any",
        config: RuntimeConfigView,
        clock: ClockProtocol | None = None,
        last_refresh_provider: Callable[[], datetime | None] | None = None,
        snapshot_provider: Callable[[], "CbbcSnapshot | None"] | None = None,
        recovery_deadline_seconds: float = _RECOVERY_DEADLINE_SECONDS,
        degraded_refresh_age_seconds: float = _DEGRADED_REFRESH_AGE_SECONDS,
    ) -> None:
        self._calculator = calculator
        self._config = config
        self._clock: ClockProtocol = clock if clock is not None else SystemClock()
        # task 6.3 lifecycle hooks. Both providers are optional: when either is
        # ``None`` ``evaluate_degradation`` becomes a silent no-op so unit
        # tests that don't care about the lifecycle still see task 6.2's
        # baseline behaviour.
        self._last_refresh_provider: Callable[[], datetime | None] | None = (
            last_refresh_provider
        )
        self._snapshot_provider: Callable[[], "CbbcSnapshot | None"] | None = (
            snapshot_provider
        )
        self._recovery_deadline_seconds: float = float(recovery_deadline_seconds)
        self._degraded_refresh_age_seconds: float = float(degraded_refresh_age_seconds)
        # 降级状态由 task 6.3 维护。
        self._cbbc_magnet_degraded: bool = False

    @property
    def cbbc_magnet_degraded(self) -> bool:
        return self._cbbc_magnet_degraded

    def mark_degraded_due_to_external_failure(
        self,
        *,
        reason: str,
        ts_hk: datetime | None = None,
    ) -> None:
        """R10.6 入口：上层（``HSIStrategyEngine``）捕获到未处理异常后调用本方法。

        立即把 ``cbbc_magnet_degraded`` 置 ``True``，仅在状态转换时写一条
        ``level=WARN, source=cbbc_magnet, event=degraded_no_data`` 日志。
        ``reason`` 简短地描述触发原因（不含异常堆栈、不含凭据），方便日志聚合。

        本方法不会因日志失败而抛错；调用方应当在所有 catch 路径里直接调用。
        """
        if self._cbbc_magnet_degraded:
            # 已经降级：不重复写日志，幂等。
            return
        self._cbbc_magnet_degraded = True
        ts = ts_hk if ts_hk is not None else self._clock.now_hk()
        try:
            logger.warning(
                "degraded_no_data",
                extra={
                    "level": "WARN",
                    "source": "cbbc_magnet",
                    "event": "degraded_no_data",
                    "reason": reason,
                    "ts_hk": ts.isoformat(),
                },
            )
        except Exception:  # noqa: BLE001 - logging 故障不应反向阻断主策略
            pass

    # ------------------------------------------------------------------
    # task 6.3 — Degradation lifecycle (R10.1–R10.5)
    # ------------------------------------------------------------------
    def evaluate_degradation(self, now_hk: datetime | None = None) -> None:
        """Reconcile ``cbbc_magnet_degraded`` against ``last_refresh_provider``.

        Behaviour (per design.md R10.1–R10.5):

        - If ``last_refresh_provider`` is ``None`` (lifecycle wiring not yet
          installed, common in unit tests) the method is a silent no-op.
        - If ``last_refresh_provider() is None`` we treat it as "never
          refreshed yet" and force degradation: write exactly one
          ``event=degraded_no_data`` log on the state transition; do not log
          again while the adapter stays degraded.
        - If ``now_hk - last_refresh > 36h``: enter degradation (or stay in
          degradation; log only on transition).
        - If ``now_hk - last_refresh ≤ 36h`` AND currently degraded: try a
          single explicit recovery initialization. (a) load the latest
          snapshot via ``snapshot_provider``, (b) push it to
          ``calculator.update_snapshot``, (c) ask ``calculator.latest()`` for
          a fresh result. If any step raises, the result is missing
          (``None``) or carries ``hsi_spot_stale=True``, OR the total wall
          time exceeds ``recovery_deadline_seconds`` → log ``event=recovery_failed``
          and stay degraded. Otherwise → log ``event=recovery_initialized`` then
          flip ``cbbc_magnet_degraded=False``.
        """
        if self._last_refresh_provider is None:
            return

        now = now_hk if now_hk is not None else self._clock.now_hk()

        try:
            last_refresh = self._last_refresh_provider()
        except Exception as exc:  # noqa: BLE001 - never let the lifecycle block
            self._enter_degraded(
                reason=f"last_refresh_provider:{type(exc).__name__}",
                ts_hk=now,
            )
            return

        if last_refresh is None:
            self._enter_degraded(reason="no_refresh_yet", ts_hk=now)
            return

        try:
            age_seconds = (now - last_refresh).total_seconds()
        except TypeError:
            # tz-aware vs naive datetime mismatch: be defensive and bail.
            self._enter_degraded(reason="refresh_ts_typeerror", ts_hk=now)
            return

        if age_seconds > self._degraded_refresh_age_seconds:
            self._enter_degraded(reason="refresh_age_exceeded_36h", ts_hk=now)
            return

        # Refresh is fresh enough. If we're currently degraded, attempt
        # explicit recovery initialization.
        if self._cbbc_magnet_degraded:
            self._attempt_recovery(now_hk=now)

    def _enter_degraded(self, *, reason: str, ts_hk: datetime) -> None:
        """Set the degraded flag and emit one ``degraded_no_data`` log on the
        state transition. Idempotent for re-entry (no duplicate log)."""
        if self._cbbc_magnet_degraded:
            return
        self._cbbc_magnet_degraded = True
        try:
            logger.warning(
                "degraded_no_data",
                extra={
                    "level": "WARN",
                    "source": "cbbc_magnet",
                    "event": "degraded_no_data",
                    "reason": reason,
                    "ts_hk": ts_hk.isoformat(),
                },
            )
        except Exception:  # noqa: BLE001
            pass

    def _attempt_recovery(self, *, now_hk: datetime) -> None:
        """Try a single explicit recovery initialization (R10.4–R10.5).

        Steps run in order — any step that fails (exception / invalid
        result / deadline exceeded) leaves the adapter degraded and emits
        exactly one ``recovery_failed`` log.
        """
        if self._snapshot_provider is None:
            # Cannot recover without a way to pull a fresh snapshot; treat
            # like a missed step and emit the failure log once.
            self._emit_recovery_failed(
                reason="snapshot_provider_missing",
                ts_hk=now_hk,
            )
            return

        deadline = time.monotonic() + self._recovery_deadline_seconds

        # (a) reload snapshot
        try:
            snapshot = self._snapshot_provider()
        except Exception as exc:  # noqa: BLE001
            self._emit_recovery_failed(
                reason=f"snapshot_provider:{type(exc).__name__}",
                ts_hk=now_hk,
            )
            return
        if snapshot is None:
            self._emit_recovery_failed(reason="snapshot_unavailable", ts_hk=now_hk)
            return
        if time.monotonic() > deadline:
            self._emit_recovery_failed(reason="deadline_exceeded", ts_hk=now_hk)
            return

        # (b) push to engine
        if self._calculator is not None and hasattr(self._calculator, "update_snapshot"):
            try:
                self._calculator.update_snapshot(snapshot)
            except Exception as exc:  # noqa: BLE001
                self._emit_recovery_failed(
                    reason=f"update_snapshot:{type(exc).__name__}",
                    ts_hk=now_hk,
                )
                return
        if time.monotonic() > deadline:
            self._emit_recovery_failed(reason="deadline_exceeded", ts_hk=now_hk)
            return

        # (c) ask for the latest result
        try:
            result = (
                self._calculator.latest()
                if (self._calculator is not None and hasattr(self._calculator, "latest"))
                else None
            )
        except Exception as exc:  # noqa: BLE001
            self._emit_recovery_failed(
                reason=f"latest:{type(exc).__name__}",
                ts_hk=now_hk,
            )
            return
        if time.monotonic() > deadline:
            self._emit_recovery_failed(reason="deadline_exceeded", ts_hk=now_hk)
            return
        if result is None or getattr(result, "hsi_spot_stale", False):
            self._emit_recovery_failed(reason="invalid_result", ts_hk=now_hk)
            return

        # All three steps succeeded — emit the INFO log first, then flip the
        # flag (design.md spells out this exact ordering).
        try:
            logger.info(
                "recovery_initialized",
                extra={
                    "level": "INFO",
                    "source": "cbbc_magnet",
                    "event": "recovery_initialized",
                    "ts_hk": now_hk.isoformat(),
                },
            )
        except Exception:  # noqa: BLE001
            pass
        self._cbbc_magnet_degraded = False

    def _emit_recovery_failed(self, *, reason: str, ts_hk: datetime) -> None:
        try:
            logger.warning(
                "recovery_failed",
                extra={
                    "level": "WARN",
                    "source": "cbbc_magnet",
                    "event": "recovery_failed",
                    "reason": reason,
                    "ts_hk": ts_hk.isoformat(),
                },
            )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 参数 fallback（R5.7 / R5.3）
    # ------------------------------------------------------------------
    def _resolve_threshold_pts(self) -> float:
        """读取 ``cbbc_dense_band_threshold_pts``；越界 / 非有限时回退默认并写 WARN。"""
        raw = getattr(
            self._config,
            "cbbc_dense_band_threshold_pts",
            _DEFAULT_DENSE_BAND_THRESHOLD_PTS,
        )
        if not _is_valid_float_in_range(
            raw, low=_THRESHOLD_PTS_LOW, high=_THRESHOLD_PTS_HIGH
        ):
            logger.warning(
                "cbbc_dense_band_threshold_pts_invalid_fallback",
                extra={
                    "level": "WARN",
                    "source": "cbbc_magnet",
                    "event": "cbbc_dense_band_threshold_pts_invalid_fallback",
                    "value": raw,
                    "fallback_to": _DEFAULT_DENSE_BAND_THRESHOLD_PTS,
                },
            )
            return _DEFAULT_DENSE_BAND_THRESHOLD_PTS
        return float(raw)

    def _resolve_pull_share(self) -> float:
        """读取 ``cbbc_dense_band_pull_share``；越界 / 非有限时回退默认并写 WARN。"""
        raw = getattr(
            self._config,
            "cbbc_dense_band_pull_share",
            _DEFAULT_DENSE_BAND_PULL_SHARE,
        )
        if not _is_valid_float_in_range(
            raw, low=_PULL_SHARE_LOW, high=_PULL_SHARE_HIGH
        ):
            logger.warning(
                "cbbc_dense_band_pull_share_invalid_fallback",
                extra={
                    "level": "WARN",
                    "source": "cbbc_magnet",
                    "event": "cbbc_dense_band_pull_share_invalid_fallback",
                    "value": raw,
                    "fallback_to": _DEFAULT_DENSE_BAND_PULL_SHARE,
                },
            )
            return _DEFAULT_DENSE_BAND_PULL_SHARE
        return float(raw)

    # ------------------------------------------------------------------
    # 结构化日志（R5.8, R5.11）
    # ------------------------------------------------------------------
    def _emit_consult_log(
        self,
        *,
        event: Literal["cbbc_magnet_consult", "cbbc_magnet_consult_unavailable"],
        extreme_direction: Literal["BULL", "BEAR"],
        decision: ConsultDecision,
        ts_hk: datetime,
    ) -> None:
        """写入恰好一条 ``event=cbbc_magnet_consult[_unavailable]`` 结构化日志。

        包含 R5.8 列出的所有字段：``event`` / 信号方向 /
        ``nearest_bull_distance_pts`` / ``nearest_bear_distance_pts`` /
        ``magnet_bias`` / ``magnet_available`` / ``magnet_aligned_against_reversal`` /
        是否否决 / 原因码。
        """
        logger.info(
            event,
            extra={
                "level": "INFO",
                "source": "cbbc_magnet",
                "event": event,
                "extreme_direction": extreme_direction,
                "nearest_bull_distance_pts": decision.nearest_bull_distance_pts,
                "nearest_bear_distance_pts": decision.nearest_bear_distance_pts,
                "magnet_bias": decision.magnet_bias,
                "magnet_available": decision.magnet_available,
                "magnet_aligned_against_reversal": decision.magnet_aligned_against_reversal,
                "vetoed_by_cbbc_magnet": decision.vetoed_by_cbbc_magnet,
                "reason_code": decision.reason_code,
                "ts_hk": ts_hk.isoformat(),
            },
        )

    def _build_unavailable(
        self,
        *,
        reason: ConsultReasonCode,
        result: "MagnetResult | None",
        magnet_aligned_against_reversal: bool = False,
    ) -> ConsultDecision:
        """构造"不可用 / 降级"路径的 ConsultDecision。

        当 ``result`` 仍然提供时，距离字段沿用 result 的值，便于上层观察；
        ``magnet_bias`` 一律置 ``None`` 以表达"无可信偏向"，``magnet_available=False``。
        """
        return ConsultDecision(
            vetoed_by_cbbc_magnet=False,
            reason_code=reason,
            nearest_bull_distance_pts=(
                result.nearest_bull_distance_pts if result is not None else None
            ),
            nearest_bear_distance_pts=(
                result.nearest_bear_distance_pts if result is not None else None
            ),
            magnet_bias=None,
            magnet_aligned_against_reversal=magnet_aligned_against_reversal,
            magnet_available=False,
        )

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def consult_for_extreme(
        self,
        extreme_direction: Literal["BULL", "BEAR"],
        hsi_spot: float,
        ts_hk: datetime,
    ) -> ConsultDecision:
        """咨询磁吸是否否决一次 extreme 反转入场。"""
        # R9.2：layer 关闭时短路返回，不写 consult 日志。
        if not self._config.cbbc_magnet_layer_enabled:
            return ConsultDecision.fail_safe("cbbc_magnet_layer_disabled")

        # task 6.3 (R10.1–R10.5): reconcile degradation lifecycle before any
        # decision logic. evaluate_degradation is a no-op when no
        # last_refresh_provider is wired in (common in unit tests that only
        # exercise the consult matrix).
        try:
            self.evaluate_degradation(now_hk=ts_hk)
        except Exception:  # noqa: BLE001 - lifecycle must never break consult
            pass

        # R5.7 / R5.3：参数 fallback（每次咨询都会校验一次配置）。
        threshold_pts = self._resolve_threshold_pts()
        pull_share_min = self._resolve_pull_share()

        # 降级模式（task 6.3 完成 36h / 恢复逻辑后会动态切换）：
        # 当前实现保证主路径行为正确——降级时进入"不可用"日志路径，
        # 但 reason_code 单独标记为 cbbc_magnet_degraded，便于上层区分。
        if self._cbbc_magnet_degraded:
            decision = self._build_unavailable(
                reason="cbbc_magnet_degraded", result=None
            )
            self._emit_consult_log(
                event="cbbc_magnet_consult_unavailable",
                extreme_direction=extreme_direction,
                decision=decision,
                ts_hk=ts_hk,
            )
            return decision

        # 取最近一次磁吸结果（duck-typed：calculator 可能是 None / 暂无结果）。
        result: "MagnetResult | None" = None
        if self._calculator is not None:
            try:
                result = self._calculator.latest()
            except Exception:  # noqa: BLE001 - 任何取数异常都进入 fail-safe
                result = None

        # 不可用路径（R5.11）：
        # - result 为 None；
        # - HSI 现价标记为 stale；
        # - 调用方传入的 hsi_spot 非有限；
        # - 总 pull = 0（无任何方向有效磁吸）。
        if result is None:
            decision = self._build_unavailable(
                reason="cbbc_magnet_consult_unavailable", result=None
            )
            self._emit_consult_log(
                event="cbbc_magnet_consult_unavailable",
                extreme_direction=extreme_direction,
                decision=decision,
                ts_hk=ts_hk,
            )
            return decision

        if getattr(result, "hsi_spot_stale", False):
            decision = self._build_unavailable(
                reason="cbbc_magnet_consult_unavailable", result=result
            )
            self._emit_consult_log(
                event="cbbc_magnet_consult_unavailable",
                extreme_direction=extreme_direction,
                decision=decision,
                ts_hk=ts_hk,
            )
            return decision

        if hsi_spot is None or not math.isfinite(hsi_spot):
            decision = self._build_unavailable(
                reason="cbbc_magnet_consult_unavailable", result=result
            )
            self._emit_consult_log(
                event="cbbc_magnet_consult_unavailable",
                extreme_direction=extreme_direction,
                decision=decision,
                ts_hk=ts_hk,
            )
            return decision

        pull_total = float(result.magnet_pull_bull) + float(result.magnet_pull_bear)
        if pull_total <= 0.0:
            decision = self._build_unavailable(
                reason="cbbc_magnet_consult_unavailable", result=result
            )
            self._emit_consult_log(
                event="cbbc_magnet_consult_unavailable",
                extreme_direction=extreme_direction,
                decision=decision,
                ts_hk=ts_hk,
            )
            return decision

        # 选取与 extreme 反转方向相反的一侧距离与 pull（R5.1 / R5.4）。
        if extreme_direction == "BULL":
            relevant_distance = result.nearest_bear_distance_pts
            relevant_pull = float(result.magnet_pull_bear)
        else:  # "BEAR"
            relevant_distance = result.nearest_bull_distance_pts
            relevant_pull = float(result.magnet_pull_bull)

        # Call_Level 不可用（对应方向无任何有效记录）→ 不可用路径（R5.11）。
        if relevant_distance is None:
            decision = self._build_unavailable(
                reason="cbbc_magnet_consult_unavailable", result=result
            )
            self._emit_consult_log(
                event="cbbc_magnet_consult_unavailable",
                extreme_direction=extreme_direction,
                decision=decision,
                ts_hk=ts_hk,
            )
            return decision

        # 与磁吸偏向是否对齐（R5.6）：``Magnet_Bias > 0`` 偏向"向下磁吸更强"，
        # 与 BULL 反转背离即"对齐"（系统语义上 magnet_aligned_against_reversal=True）。
        bias = float(result.magnet_bias)
        if extreme_direction == "BULL":
            magnet_aligned_against_reversal = bias > 0.0
        else:  # "BEAR"
            magnet_aligned_against_reversal = bias < 0.0
        # bias == 0.0 不视为背离（R5.6 末句）。

        # 距离 vs 阈值 / share（R5.2 / R5.3 / R5.5 / R5.10 / R5.12）。
        if relevant_distance > threshold_pts:
            vetoed = False
            reason_code: ConsultReasonCode = "cbbc_dense_band_clear"
        else:
            share = relevant_pull / pull_total
            if share >= pull_share_min:
                vetoed = True
                reason_code = (
                    "cbbc_dense_band_above"
                    if extreme_direction == "BULL"
                    else "cbbc_dense_band_below"
                )
            else:
                vetoed = False
                reason_code = "cbbc_dense_band_pull_share_below"

        decision = ConsultDecision(
            vetoed_by_cbbc_magnet=vetoed,
            reason_code=reason_code,
            nearest_bull_distance_pts=result.nearest_bull_distance_pts,
            nearest_bear_distance_pts=result.nearest_bear_distance_pts,
            magnet_bias=bias,
            magnet_aligned_against_reversal=magnet_aligned_against_reversal,
            magnet_available=True,
        )
        self._emit_consult_log(
            event="cbbc_magnet_consult",
            extreme_direction=extreme_direction,
            decision=decision,
            ts_hk=ts_hk,
        )
        return decision
