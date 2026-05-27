"""
CBBC 街货磁吸信号 - 回测适配器（cbbc-magnet-signal task 10.1）。

把已实现的 ``MagnetEngine`` + ``MagnetSignalAdapter`` 接入 ``bt.py`` /
``backtest_service.run_backtest``，使每个 extreme 反转入场都能复用实盘相同的
密集带否决逻辑（R7.1–R7.7）。

设计要点：

1. **复用而非重写**：距离、bias、密集带阈值判定全部走 ``MagnetEngine`` +
   ``MagnetSignalAdapter``，不在本文件复刻任何公式（task 10.1 描述）。
2. **D-1 fallback**：``prepare_for_day(date)`` 优先 ``read_snapshot(date - 1d)``，
   不存在时回退到 ``latest_before(date)``；都不存在时返回 ``DayPreparation(missing=True)``，
   主流程整日跳过 magnet 否决并把 ``cbbc_snapshot_missing_days += 1``（R7.4）。
3. **layer disabled / 降级 / 参数无效不计 missing**：``cbbc_snapshot_missing_days``
   只统计因数据缺失（没有 D-1 base snapshot）而无法咨询的日子（R7.4 末段）。
4. **盘中新发注入**：``DayPreparation.intraday_new_listings`` 由 ``CbbcDataService``
   离线日志或 ``intraday_new_listings_<YYYYMMDD>.parquet`` 提供（task 4.4 完成后接入）；
   适配器仅负责按 ``listing_time <= ts`` 顺序注入，且每条同日仅注入一次（R7.5）。
5. **summary 计数**：四项计数与 design.md 完全一致：

       total_vetoed                   每次 consult 否决 +1
       vetoed_dense_band_above        BULL 反转被 cbbc_dense_band_above 否决
       vetoed_dense_band_below        BEAR 反转被 cbbc_dense_band_below 否决
       control_total                  每次 extreme 反转入场（不论是否启用 layer）+1
       cbbc_snapshot_missing_days     缺失 D-1 snapshot 的整日跳过日数

本文件不做 I/O 之外的副作用：所有日志通过 ``cbbc_signal_adapter`` 内部的
``logging.Logger("cbbc_magnet")`` 透传，便于回测期与实盘共享相同的事件名。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Iterable, Literal, Optional

from cbbc_calculator import MagnetEngine
from cbbc_signal_adapter import MagnetSignalAdapter
from cbbc_storage import CbbcSnapshot, CbbcStorage, SnapshotError
from models import BacktestMagnetSummary


if TYPE_CHECKING:
    from cbbc_signal_adapter import ConsultDecision


__all__ = [
    "DayPreparation",
    "CbbcBacktestAdapter",
    "BacktestMagnetSummary",
    "build_intraday_event",
    "IntradayNewListingEvent",
]


logger = logging.getLogger("cbbc_backtest")


# Direction strings used by extreme branches. Match the keys produced by
# ``backtest_service`` (mode = "极度超卖" → BULL, mode = "极度超买" → BEAR;
# shadow modes carry the same directional polarity).
_BULL_MODE_TOKENS: frozenset[str] = frozenset(
    {"极度超卖", "非常极端下影反抽"}
)
_BEAR_MODE_TOKENS: frozenset[str] = frozenset(
    {"极度超买", "非常极端上影回落"}
)


# Branches that should consult the magnet adapter (task 7.3 / 10.2).
_MAGNET_RELEVANT_BRANCHES: frozenset[str] = frozenset(
    {"b1_volume_extreme", "b2_very_extreme_pullback", "b3_completed_k", "b4_shadow_reversal"}
)


@dataclass(frozen=True)
class IntradayNewListingEvent:
    """One intraday new-listing event to be merged into the in-memory snapshot.

    ``listing_time`` is the wall-clock HK time of the listing; the adapter
    will inject the event the first time ``at_replay_ts(ts)`` is called with
    ``ts >= listing_time`` (R7.5).
    """
    listing_time: datetime
    record: Any  # CbbcRecord — typed loosely to avoid a cycle.


def build_intraday_event(listing_time: datetime, record: Any) -> IntradayNewListingEvent:
    """Tiny constructor that callers can use without importing the dataclass."""
    return IntradayNewListingEvent(listing_time=listing_time, record=record)


@dataclass
class DayPreparation:
    """Per-day fixture handed back by ``CbbcBacktestAdapter.prepare_for_day``.

    - ``missing=True`` means there is no usable D-1 base snapshot. The caller
      must skip magnet consults for the whole day and bump
      ``cbbc_snapshot_missing_days``.
    - ``base_snapshot`` is the snapshot used as the in-memory starting point.
    - ``intraday_new_listings`` is an ordered iterable of
      :class:`IntradayNewListingEvent` to merge into the snapshot during
      replay (R7.5).
    """
    missing: bool = False
    base_snapshot: CbbcSnapshot | None = None
    intraday_new_listings: tuple[IntradayNewListingEvent, ...] = ()
    fallback_used: bool = False  # True when we fell back via ``latest_before``


# Mode token classification for control_total / direction inference.
def _mode_to_direction(
    mode: str,
    side: Optional[str] = None,
) -> Literal["BULL", "BEAR"] | None:
    """Resolve an extreme-mode label to BULL/BEAR.

    Falls back to the optional ``side`` argument (the position type string
    "bull" / "bear" produced by backtest_service) so callers don't need to
    enumerate every possible mode token.
    """
    if mode in _BULL_MODE_TOKENS:
        return "BULL"
    if mode in _BEAR_MODE_TOKENS:
        return "BEAR"
    if side is not None:
        side_l = str(side).lower()
        if side_l == "bull":
            return "BULL"
        if side_l == "bear":
            return "BEAR"
    return None


class CbbcBacktestAdapter:
    """``CBBC_Backtest_Adapter``：把 magnet layer 接入 backtest 主循环。

    单实例可跨多个交易日复用：``prepare_for_day`` 在每个交易日开始时调用，
    重置内存中的快照与已注入的盘中事件标记；``at_replay_ts`` 在每根 1 分钟 K 线
    重放时按 ``listing_time <= ts`` 顺序合并新发记录、并把 HSI 现价喂给
    ``MagnetEngine``；``consult_extreme`` 仅在 extreme 反转入场前调用一次。

    Args:
        storage: ``CbbcStorage``。当 ``base_snapshot_loader`` 提供时可省略。
        config: 提供 magnet layer / 阈值 / pull_share 三个字段（duck-typed
            ``RuntimeConfigView``）。回测里通常是一个简单的 ``SimpleNamespace``。
        decay_points: 传给 ``MagnetEngine`` 的初始衰减距离（pt）。建议直接复用
            ``runtime_config_store`` 的默认值（300.0）。
        intraday_loader: 可选回调；输入 ``date``，返回该交易日 09:30–16:00
            内的盘中新发事件序列。当 ``CbbcDataService`` 在 task 4.4 完成前还没
            落盘任何 intraday 数据时，本回调返回空序列即可。
        clock: 仅用于注入到 ``MagnetSignalAdapter``；回测里使用 ``ts`` 作为
            magnet engine 的时间戳源，与 clock 无关。
    """

    def __init__(
        self,
        *,
        storage: CbbcStorage | None = None,
        config: Any,
        decay_points: float = 300.0,
        intraday_loader: Optional[
            "callable[[date], Iterable[IntradayNewListingEvent]]"  # type: ignore[name-defined]
        ] = None,
        clock=None,
        # Backtest replay ticks are typically 1 minute apart and consult
        # calls can lag arbitrary minutes behind the last replay tick (the
        # consult fires only on extreme bars, while replay ticks fire every
        # bar). Use a very generous default — the wall-clock stale guard is
        # not meaningful in a back-test anyway because the only "freshness"
        # signal available is the replay timestamp itself.
        hsi_stale_seconds: float = 86400.0,
    ) -> None:
        if storage is None:
            storage = CbbcStorage()
        self._storage = storage
        self._config = config
        self._intraday_loader = intraday_loader

        # The magnet engine is rebuilt per day to avoid state bleed between
        # trading sessions; the adapter wraps the live engine.
        self._engine: MagnetEngine | None = None
        self._adapter: MagnetSignalAdapter | None = None
        self._decay_points: float = float(decay_points)
        self._clock = clock
        self._hsi_stale_seconds: float = float(hsi_stale_seconds)

        # ``MagnetEngine`` uses a clock to detect stale HSI updates. In a
        # back-test we want "now" to be the current replay timestamp, not the
        # wall-clock — otherwise every consult shows ``hsi_spot_stale=True``
        # because last_hsi_ts is set to a backtest timestamp from years ago.
        # Hold onto the most recent ``ts`` seen by ``at_replay_ts`` and let
        # the engine read it via ``self._engine_clock``.
        self._replay_now: datetime | None = None

        # Aggregate counters returned by ``summary()``.
        self._total_vetoed: int = 0
        self._vetoed_above: int = 0
        self._vetoed_below: int = 0
        self._control_total: int = 0
        self._missing_days: int = 0

        # Per-day state.
        self._current_day: date | None = None
        self._current_records: list[Any] = []
        self._injected_codes: set[str] = set()
        self._pending_intraday: list[IntradayNewListingEvent] = []
        self._has_base: bool = False  # False ⇒ skip magnet consults for the day.

    def _engine_clock(self) -> datetime:
        """Clock the magnet engine uses for HSI staleness checks during replay."""
        return self._replay_now if self._replay_now is not None else datetime.now()

    # ------------------------------------------------------------------
    # Per-day fixture management
    # ------------------------------------------------------------------
    def prepare_for_day(self, day: date) -> DayPreparation:
        """Load the D-1 base snapshot + scheduled intraday events for ``day``.

        Falls back to ``latest_before(day)`` when ``read_snapshot(day - 1d)``
        is missing. Returns a :class:`DayPreparation` whose ``missing`` flag
        tells the caller whether to skip magnet consults for the whole day.
        """
        self._current_day = day
        self._current_records = []
        self._injected_codes = set()
        self._pending_intraday = []
        self._has_base = False
        # Always rebuild the engine + adapter for a new day. Use the replay
        # clock so the engine's stale-HSI detection works against backtest
        # timestamps instead of wall-clock now.
        self._replay_now = None
        self._engine = MagnetEngine(
            decay_points=self._decay_points,
            clock=self._engine_clock,
            hsi_stale_seconds=self._hsi_stale_seconds,
        )
        self._adapter = MagnetSignalAdapter(
            calculator=self._engine,
            config=self._config,
            clock=self._clock,
        )

        base_snapshot, fallback_used = self._load_base_snapshot(day)
        if base_snapshot is None:
            self._missing_days += 1
            logger.warning(
                "cbbc_backtest_snapshot_missing",
                extra={
                    "level": "WARN",
                    "source": "cbbc_backtest",
                    "event": "cbbc_backtest_snapshot_missing",
                    "trade_date": day.isoformat(),
                },
            )
            return DayPreparation(missing=True)

        self._has_base = True
        self._current_records = list(base_snapshot.records)
        try:
            self._engine.update_snapshot(base_snapshot)
        except Exception as exc:  # noqa: BLE001 - keep the day running
            logger.warning(
                "cbbc_backtest_snapshot_load_failed",
                extra={
                    "level": "WARN",
                    "source": "cbbc_backtest",
                    "event": "cbbc_backtest_snapshot_load_failed",
                    "trade_date": day.isoformat(),
                    "reason": type(exc).__name__,
                },
            )

        # Pending intraday events come pre-sorted; we sort defensively in case
        # the loader hands them back in random order.
        events = self._load_intraday_events(day)
        self._pending_intraday = sorted(
            events, key=lambda e: e.listing_time
        )

        return DayPreparation(
            missing=False,
            base_snapshot=base_snapshot,
            intraday_new_listings=tuple(self._pending_intraday),
            fallback_used=fallback_used,
        )

    # ------------------------------------------------------------------
    # Per-bar replay tick
    # ------------------------------------------------------------------
    def at_replay_ts(self, ts: datetime, hsi_spot: float) -> None:
        """Inject scheduled intraday listings up to ``ts`` and feed HSI spot.

        Both side effects must be no-ops when no day has been prepared or the
        day was missing-base.  Errors are swallowed so a single bad event
        cannot abort the back-test.
        """
        if not self._has_base or self._engine is None:
            return

        # Advance the replay clock so the engine's HSI-stale check compares
        # against backtest time, not wall-clock now.
        self._replay_now = ts

        # Pop scheduled intraday events with listing_time <= ts. The list is
        # kept sorted in prepare_for_day so we only need to walk the front.
        if self._pending_intraday:
            consumed = 0
            for event in self._pending_intraday:
                if event.listing_time > ts:
                    break
                consumed += 1
                rec = event.record
                code = getattr(rec, "code", None)
                if code is None or code in self._injected_codes:
                    continue
                self._injected_codes.add(code)
                self._current_records.append(rec)
            if consumed:
                del self._pending_intraday[:consumed]
                # Rebuild the snapshot for the engine. Snapshots are
                # immutable tuples; we splice a new one with the merged set.
                try:
                    new_snapshot = CbbcSnapshot(
                        snapshot_date=self._current_day or ts.date(),
                        records=tuple(self._current_records),
                    )
                    self._engine.update_snapshot(new_snapshot)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "cbbc_backtest_intraday_merge_failed",
                        extra={
                            "level": "WARN",
                            "source": "cbbc_backtest",
                            "event": "cbbc_backtest_intraday_merge_failed",
                            "reason": type(exc).__name__,
                        },
                    )

        try:
            self._engine.update_hsi_spot(float(hsi_spot), ts)
        except Exception:  # noqa: BLE001 - stale-spot path, fail-safe
            pass

    # ------------------------------------------------------------------
    # Consult
    # ------------------------------------------------------------------
    def consult_extreme(
        self,
        side: str,
        hsi_spot: float,
        ts: datetime,
        *,
        branch: str | None = None,
        mode: str | None = None,
    ) -> "ConsultDecision | None":
        """Bump ``control_total`` and (if the layer is enabled) consult magnet.

        Returns ``None`` when the layer is unavailable or the day has no base
        snapshot. Otherwise returns the ``ConsultDecision`` produced by
        ``MagnetSignalAdapter.consult_for_extreme``.

        The adapter mirrors task 7.3 behaviour: ``control_total`` increments
        for every extreme reversal *regardless* of layer state; veto counters
        only increment when the layer actually fires a veto.
        """
        # control_total counts ALL extreme reversals (R7.7).
        if branch is None or branch in _MAGNET_RELEVANT_BRANCHES:
            self._control_total += 1
        else:
            # Non-extreme branches must not consult magnet at all (R7.3 only
            # touches the extreme reversal path).
            return None

        if not self._has_base or self._adapter is None:
            return None

        # Keep the replay clock in sync with consult timestamps so the engine's
        # stale-HSI guard compares against backtest time, not wall-clock now.
        self._replay_now = ts

        # Push the consult's HSI spot/timestamp into the engine before asking
        # for a decision. In a backtest, consult_extreme is always called with
        # the freshest available spot for that bar; without this push the
        # engine could mark the prior at_replay_ts(ts) input as stale (the
        # gap between replay ticks and consult timestamps is bar-sized, not
        # millisecond-sized) and the adapter would short-circuit to
        # cbbc_magnet_consult_unavailable.
        try:
            if self._engine is not None:
                self._engine.update_hsi_spot(float(hsi_spot), ts)
        except Exception:  # noqa: BLE001 - keep the consult fail-safe
            pass

        direction = _mode_to_direction(mode or "", side)
        if direction is None:
            return None

        try:
            decision = self._adapter.consult_for_extreme(direction, float(hsi_spot), ts)
        except Exception as exc:  # noqa: BLE001 - R10.6
            logger.warning(
                "cbbc_backtest_consult_failed",
                extra={
                    "level": "WARN",
                    "source": "cbbc_backtest",
                    "event": "cbbc_backtest_consult_failed",
                    "reason": type(exc).__name__,
                    "trade_date": self._current_day.isoformat() if self._current_day else None,
                },
            )
            try:
                self._adapter.mark_degraded_due_to_external_failure(
                    reason=f"backtest_consult:{type(exc).__name__}",
                    ts_hk=ts,
                )
            except Exception:  # noqa: BLE001
                pass
            return None

        if decision.vetoed_by_cbbc_magnet:
            self._total_vetoed += 1
            if decision.reason_code == "cbbc_dense_band_above":
                self._vetoed_above += 1
            elif decision.reason_code == "cbbc_dense_band_below":
                self._vetoed_below += 1

        return decision

    # ------------------------------------------------------------------
    # Result aggregation
    # ------------------------------------------------------------------
    def summary(self) -> BacktestMagnetSummary:
        """Return the aggregate counters defined by R7.7 / design.md."""
        return BacktestMagnetSummary(
            total_vetoed=self._total_vetoed,
            vetoed_dense_band_above=self._vetoed_above,
            vetoed_dense_band_below=self._vetoed_below,
            control_total=self._control_total,
            cbbc_snapshot_missing_days=self._missing_days,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _load_base_snapshot(self, day: date) -> tuple[CbbcSnapshot | None, bool]:
        """Try ``read_snapshot(day - 1d)`` then ``latest_before(day)``.

        Returns ``(snapshot, fallback_used)``. ``fallback_used`` is ``True``
        when we fell back via ``latest_before``.
        """
        d_minus_1 = day - timedelta(days=1)
        try:
            snapshot = self._storage.read_snapshot(d_minus_1)
            return snapshot, False
        except SnapshotError as err:
            # Either non-trading-day or snapshot_missing — both fall through
            # to latest_before. Other codes (e.g. snapshot_immutable) shouldn't
            # come up on read; bubble them up since they indicate a coding bug.
            if err.code not in ("non_trading_day", "snapshot_missing"):
                raise
        except Exception as exc:  # noqa: BLE001 - keep day running on disk errors
            logger.warning(
                "cbbc_backtest_read_snapshot_failed",
                extra={
                    "level": "WARN",
                    "source": "cbbc_backtest",
                    "event": "cbbc_backtest_read_snapshot_failed",
                    "trade_date": d_minus_1.isoformat(),
                    "reason": type(exc).__name__,
                },
            )

        try:
            fallback = self._storage.latest_before(day)
            if fallback is None:
                return None, False
            return fallback, True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cbbc_backtest_latest_before_failed",
                extra={
                    "level": "WARN",
                    "source": "cbbc_backtest",
                    "event": "cbbc_backtest_latest_before_failed",
                    "trade_date": day.isoformat(),
                    "reason": type(exc).__name__,
                },
            )
            return None, False

    def _load_intraday_events(
        self, day: date
    ) -> Iterable[IntradayNewListingEvent]:
        if self._intraday_loader is None:
            return ()
        try:
            events = self._intraday_loader(day)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cbbc_backtest_intraday_load_failed",
                extra={
                    "level": "WARN",
                    "source": "cbbc_backtest",
                    "event": "cbbc_backtest_intraday_load_failed",
                    "trade_date": day.isoformat(),
                    "reason": type(exc).__name__,
                },
            )
            return ()
        return tuple(events) if events is not None else ()
