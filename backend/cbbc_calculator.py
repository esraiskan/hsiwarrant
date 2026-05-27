"""
CBBC 街货磁吸信号 - 量化计算层（任务 3.1 + 任务 3.2）。

本模块包含两层：

任务 3.1（纯函数式量化计算，无 I/O、无状态）：

- ``HistBucket``：5pt 桶直方图条目（不可变）。
- ``MagnetResult``：单次计算的不可变结果对象（含距离、bias、直方图、计数等）。
- ``compute_magnet(...)``：纯函数；接收 ``CbbcSnapshot`` + HSI 现价 + decay 参数，
  返回 ``MagnetResult``。

任务 3.2（状态容器与发布逻辑）：

- ``MagnetEngineError``：引擎层错误（``hsi_spot_stale`` / ``cbbc_magnet_decay_points_invalid``）。
- ``MagnetEngine``：包裹一份最近 ``MagnetResult`` 的状态容器。
  - ``update_hsi_spot(price, ts_hk)`` / ``update_snapshot(snapshot)`` /
    ``update_decay_points(value)`` 均为同步方法，无 I/O。
  - ``latest()`` 返回最近一次发布的（不可变）``MagnetResult``。
  - HSI_Spot 最近 ``hsi_stale_seconds`` 内未刷新时阻断新结果发布、把上一次结果的
    ``hsi_spot_stale`` 置为 ``True``，并向调用方抛 ``MagnetEngineError("hsi_spot_stale")``。
  - ``|new_hsi_spot - last_used_hsi_spot| > 5.0`` 时立即重算并发布最新结果。
  - ``update_decay_points`` 校验 ``(0.0, 10000.0]`` 与有限性，非法值保留旧值并抛
    ``MagnetEngineError("cbbc_magnet_decay_points_invalid")``。

并发模型：``MagnetEngine`` 用 ``threading.Lock`` 串行化 "compute → store new result"。
设计文档提到 ``asyncio.Lock``，但 ``compute_magnet`` 是纯 CPU、不 await，且各 ``update_*``
方法为同步入口（任意线程 / 协程都可调用），用 ``threading.Lock`` 即可保证发布原子性，
也无需把调用方约束为 ``async``。

公式（与设计文档 / R4 完全对齐）::

    distance_pts        = |Call_Level - HSI_Spot|
    weight              = max(0, 1 - distance_pts / decay_points)
    notional_hkd        = outstanding_shares * er_ratio * weight
    magnet_pull_bull    = sum(notional_hkd for r where direction=bull)
    magnet_pull_bear    = sum(notional_hkd for r where direction=bear)
    denom               = max(magnet_pull_bull + magnet_pull_bear, 1.0)
    magnet_bias         = clamp((magnet_pull_bear - magnet_pull_bull) / denom, -1.0, 1.0)
"""
from __future__ import annotations

import dataclasses
import math
import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Literal, get_args
from zoneinfo import ZoneInfo

from cbbc_storage import CbbcRecord, CbbcSnapshot


# 直方图覆盖距离上限（pt）。设计文档 / 任务 3.1 描述：``5pt 桶, 距离 ≤ 200pt``。
_HIST_DISTANCE_CAP_PTS: float = 200.0
_HIST_BUCKET_WIDTH_PTS: float = 5.0
# Bias 截断区间。
_BIAS_LOWER: float = -1.0
_BIAS_UPPER: float = 1.0


@dataclass(frozen=True)
class HistBucket:
    """直方图单桶。

    - ``bucket_low``：5pt 整数倍下沿（即 ``floor(distance / 5) * 5``）。
    - ``bucket_high``：``bucket_low + 5``。
    - ``pull_hkd``：该桶内 ``sum(notional_hkd)``，方向不区分。
    """
    bucket_low: float
    bucket_high: float
    pull_hkd: float


@dataclass(frozen=True)
class MagnetResult:
    """单次磁吸计算的不可变结果对象。

    字段说明：

    - ``generated_at_hk``：本次计算生成的 HK 时间戳（由调用方传入）。
    - ``hsi_spot``：本次计算所用的 HSI 现价（来自调用方传入；可能为 NaN/Inf）。
    - ``hsi_spot_stale``：HSI 现价是否陈旧（任务 3.2 的引擎层负责判定，本任务仅透传）。
    - ``decay_points``：本次计算所用的衰减距离（pt），见 R4.2、R4.7。
    - ``magnet_bias``：归一化方向偏向，∈ ``[-1.0, 1.0]``。
    - ``magnet_pull_bull`` / ``magnet_pull_bear``：方向 dollar-weighted 求和。
    - ``histogram``：5pt 桶直方图（``bucket_low`` 升序），覆盖距离 ``<= 200pt``，
      且仅包含 ``pull_hkd > 0`` 的桶。
    - ``record_count``：参与计算（即通过校验）的记录数。
    - ``skipped_count``：被跳过（未参与计算）的记录数。
    - ``nearest_bull_distance_pts`` / ``nearest_bear_distance_pts``：方向最近距离；
      跨所有非跳过记录取最小 ``distance_pts``。当对应方向无任何非跳过记录时为 ``None``。
      注意：``nearest`` 即使 ``weight == 0``（即距离 ≥ ``decay_points``）也会被参与统计，
      因为"最近"可能本来就在衰减窗口之外。
    """
    generated_at_hk: datetime
    hsi_spot: float
    hsi_spot_stale: bool
    decay_points: float
    magnet_bias: float
    magnet_pull_bull: float
    magnet_pull_bear: float
    histogram: tuple[HistBucket, ...]
    record_count: int
    skipped_count: int
    nearest_bull_distance_pts: float | None
    nearest_bear_distance_pts: float | None


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _is_valid_nonneg_finite(x: float | None) -> bool:
    """单字段校验：非 None、有限浮点（非 NaN / 非 ±Inf）、非负值。"""
    if x is None:
        return False
    try:
        if not math.isfinite(x):
            return False
    except TypeError:
        return False
    return x >= 0.0


def _record_is_valid(r: CbbcRecord) -> bool:
    """记录级校验。R4.11：``Call_Level`` / ``outstanding_shares`` / ``er_ratio`` 任一
    为空 / 非有限 / 负值，或 ``direction`` 不属于 {bull, bear} 时跳过。"""
    if not _is_valid_nonneg_finite(r.call_level):
        return False
    if not _is_valid_nonneg_finite(r.outstanding_shares):
        return False
    if not _is_valid_nonneg_finite(r.er_ratio):
        return False
    if r.direction not in ("bull", "bear"):
        return False
    return True


def _clamp(value: float, lower: float, upper: float) -> float:
    if value < lower:
        return lower
    if value > upper:
        return upper
    return value


def _bucket_low_for_distance(distance: float) -> float:
    """以 5pt 为桶宽，返回距离所在桶的下沿。``floor(distance / 5) * 5``。"""
    return math.floor(distance / _HIST_BUCKET_WIDTH_PTS) * _HIST_BUCKET_WIDTH_PTS


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def compute_magnet(
    snapshot: CbbcSnapshot,
    hsi_spot: float,
    decay_points: float,
    *,
    generated_at_hk: datetime,
    hsi_spot_stale: bool = False,
    today_low: float | None = None,
    today_high: float | None = None,
) -> MagnetResult:
    """从 CBBC 快照计算磁吸结果（纯函数）。

    Args:
        snapshot: 当前可用的 CBBC outstanding 全量快照。
        hsi_spot: 调用方传入的 HSI 现价（pt）。若非有限值，函数将返回降级结果
            （所有记录视为被跳过，bias=0，histogram=()，nearest 字段均为 ``None``）。
        decay_points: 衰减距离（pt）。**调用方必须保证** ``decay_points > 0`` 且为有限浮点；
            否则本函数抛出 ``ValueError``。运行时区间 ``(0.0, 10000.0]`` 由引擎层
            （任务 3.2 的 ``MagnetEngine.update_decay_points``）负责强制。
        generated_at_hk: 本次计算的 HK 时间戳，由调用方提供（保证可重现）。
        hsi_spot_stale: 调用方告知的 HSI 陈旧标志；本任务仅原样写入返回对象的同名字段。
        today_low: 当日 HSI 最低价 (HK)。当提供时,任何 ``call_level >= today_low``
            的 **bull-direction** 合约一律视为已强制收回 (street_vol = 0)。这是
            硬性兜底,补救快照刷新延迟期间的脏数据。``None`` 时跳过此过滤。
        today_high: 当日 HSI 最高价。同理,``call_level <= today_high`` 的
            **bear-direction** 合约一律视为已强制收回。

    Returns:
        ``MagnetResult``：计算结果对象（``frozen``）。

    Raises:
        ValueError: 当 ``decay_points`` 不是正有限浮点时抛出。

    边界与降级行为说明：

    - **空快照**（``snapshot.records`` 为空）：返回 ``magnet_bias=0.0``、
      ``record_count=0``、``skipped_count=0``、``histogram=()``、
      ``nearest_bull_distance_pts=None`` 与 ``nearest_bear_distance_pts=None``。
    - **HSI 现价非有限**（NaN / ±Inf）：本函数为保持确定性，**不**抛错；
      返回降级结果：``record_count=0``、``skipped_count=len(records)``、
      ``magnet_bias=0.0``、``magnet_pull_bull=0.0``、``magnet_pull_bear=0.0``、
      ``histogram=()``、两个 ``nearest`` 字段均为 ``None``；``hsi_spot`` 与
      ``hsi_spot_stale`` 仍按传入值原样回填，便于上层观察。
    - **单条记录非法**：见 R4.11，跳过并 ``skipped_count += 1``，不参与求和。
    - **bull / bear 双方求和均为 0**：``denom = max(0, 1.0) = 1.0``，
      ``magnet_bias = 0.0`` 自然落在 ``[-1, 1]`` 内。
    - **nearest 计算**：跨所有 *非跳过* 记录按方向取最小 ``distance_pts``；
      即使 ``weight == 0``（距离 ≥ ``decay_points``）也参与，因为"最近"
      并不要求落在衰减窗口内。
    """
    # decay_points 校验：调用方契约。
    if decay_points is None or not math.isfinite(decay_points) or decay_points <= 0.0:
        raise ValueError(
            f"decay_points must be a positive finite float, got {decay_points!r}"
        )

    records = snapshot.records
    total_records = len(records)

    # HSI 非有限 → 降级结果（记录全跳过，确定性返回）。
    if not math.isfinite(hsi_spot):
        return MagnetResult(
            generated_at_hk=generated_at_hk,
            hsi_spot=hsi_spot,
            hsi_spot_stale=hsi_spot_stale,
            decay_points=decay_points,
            magnet_bias=0.0,
            magnet_pull_bull=0.0,
            magnet_pull_bear=0.0,
            histogram=(),
            record_count=0,
            skipped_count=total_records,
            nearest_bull_distance_pts=None,
            nearest_bear_distance_pts=None,
        )

    pull_bull: float = 0.0
    pull_bear: float = 0.0
    record_count: int = 0
    skipped_count: int = 0
    nearest_bull: float | None = None
    nearest_bear: float | None = None
    # 5pt 桶累加器：bucket_low -> sum(notional_hkd)
    hist_acc: dict[float, float] = {}

    for r in records:
        if not _record_is_valid(r):
            skipped_count += 1
            continue

        distance = abs(r.call_level - hsi_spot)
        # ``outstanding_shares == 0`` 的合约对市场没有真实拉力 — HKEX 上常见
        # 已被全数赎回 / 新发未售出的占位记录。我们仍计入 ``record_count``
        # (它们通过了 R4.11 的字段校验),但**不参与 nearest 统计**和
        # ``magnet_pull_* / histogram`` 累计 — notional 本来就是 0,从循环里
        # 提前 ``continue`` 与"参与但贡献为 0"等价,代码更直接。
        if r.outstanding_shares <= 0.0:
            record_count += 1
            continue

        # 硬性兜底:如果今天 HSI 已经触及/穿越过该档收回价,则该合约一定已被
        # 强制收回 (HKEX 的 mandatory call mechanism)。我们当前的快照可能还
        # 是开盘前抓的旧数据,所以这里基于 ``today_low / today_high`` 做一次
        # 现场过滤,与 ``street_vol == 0`` 同等处理。
        # bull 收回价 >= today_low → 当天某一刻被吃过
        # bear 收回价 <= today_high → 当天某一刻被吃过
        if (
            today_low is not None
            and r.direction == "bull"
            and math.isfinite(today_low)
            and r.call_level >= today_low
        ):
            record_count += 1
            continue
        if (
            today_high is not None
            and r.direction == "bear"
            and math.isfinite(today_high)
            and r.call_level <= today_high
        ):
            record_count += 1
            continue

        # nearest 在 weight == 0 时也参与统计（设计要求）。
        if r.direction == "bull":
            if nearest_bull is None or distance < nearest_bull:
                nearest_bull = distance
        else:  # "bear"
            if nearest_bear is None or distance < nearest_bear:
                nearest_bear = distance

        weight = 1.0 - distance / decay_points
        if weight < 0.0:
            weight = 0.0
        # 注：当 distance == 0 时 weight == 1.0；当 distance == decay_points 时 weight == 0.0。

        notional = r.outstanding_shares * r.er_ratio * weight
        if r.direction == "bull":
            pull_bull += notional
        else:
            pull_bear += notional

        # 直方图：仅覆盖距离 ≤ 200pt 的桶；累加 dollar-weighted notional。
        if distance <= _HIST_DISTANCE_CAP_PTS:
            bucket_low = _bucket_low_for_distance(distance)
            hist_acc[bucket_low] = hist_acc.get(bucket_low, 0.0) + notional

        record_count += 1

    # bias：denom 取 max(sum, 1.0) 以避免零除；R4.5 / R4.6。
    denom = pull_bull + pull_bear
    if denom < 1.0:
        denom = 1.0
    raw_bias = (pull_bear - pull_bull) / denom
    magnet_bias = _clamp(raw_bias, _BIAS_LOWER, _BIAS_UPPER)

    # 直方图按 bucket_low 升序输出，且仅保留 pull_hkd > 0 的桶。
    histogram: tuple[HistBucket, ...] = tuple(
        HistBucket(
            bucket_low=low,
            bucket_high=low + _HIST_BUCKET_WIDTH_PTS,
            pull_hkd=hist_acc[low],
        )
        for low in sorted(hist_acc.keys())
        if hist_acc[low] > 0.0
    )

    return MagnetResult(
        generated_at_hk=generated_at_hk,
        hsi_spot=hsi_spot,
        hsi_spot_stale=hsi_spot_stale,
        decay_points=decay_points,
        magnet_bias=magnet_bias,
        magnet_pull_bull=pull_bull,
        magnet_pull_bear=pull_bear,
        histogram=histogram,
        record_count=record_count,
        skipped_count=skipped_count,
        nearest_bull_distance_pts=nearest_bull,
        nearest_bear_distance_pts=nearest_bear,
    )


# ---------------------------------------------------------------------------
# 任务 3.2：MagnetEngine 状态容器与发布逻辑
# ---------------------------------------------------------------------------

_HK_TZ: ZoneInfo = ZoneInfo("Asia/Hong_Kong")

# 引擎层错误码：
# - ``hsi_spot_stale``：HSI_Spot 最近 ``hsi_stale_seconds`` 内未刷新（或 update_hsi_spot
#   调用之间的间隔超过 ``hsi_stale_seconds``）。新结果发布被阻断，上一次结果的
#   ``hsi_spot_stale`` 字段被翻为 ``True``。
# - ``cbbc_magnet_decay_points_invalid``：``update_decay_points`` 收到非有限值或
#   不在区间 ``(0.0, 10000.0]`` 内的更新。旧值保留。
MagnetEngineErrorCode = Literal[
    "hsi_spot_stale",
    "cbbc_magnet_decay_points_invalid",
]

_ALLOWED_ENGINE_ERROR_CODES: frozenset[str] = frozenset(get_args(MagnetEngineErrorCode))


class MagnetEngineError(Exception):
    """``MagnetEngine`` 层错误。

    ``code`` 严格限定在 ``MagnetEngineErrorCode`` 之内；构造时如果传入未知 code 会立即抛
    ``ValueError``，避免错误码 typo 被静默吞掉。
    """

    code: MagnetEngineErrorCode

    def __init__(self, code: MagnetEngineErrorCode, message: str | None = None) -> None:
        if code not in _ALLOWED_ENGINE_ERROR_CODES:
            raise ValueError(
                f"MagnetEngineError.code 必须是 {sorted(_ALLOWED_ENGINE_ERROR_CODES)} 之一，收到 {code!r}"
            )
        self.code = code
        super().__init__(message if message is not None else code)


def _validate_decay_points(value: object) -> float:
    """校验 ``decay_points``：必须是有限实数且属于 ``(0.0, 10000.0]``。

    校验失败时统一抛 ``MagnetEngineError("cbbc_magnet_decay_points_invalid")``。
    成功时返回归一化后的 ``float`` 值。
    """
    if value is None or isinstance(value, bool):
        raise MagnetEngineError(
            "cbbc_magnet_decay_points_invalid",
            f"decay_points must be a real finite number, got {value!r}",
        )
    if not isinstance(value, (int, float)):
        raise MagnetEngineError(
            "cbbc_magnet_decay_points_invalid",
            f"decay_points must be a real finite number, got {type(value).__name__}",
        )
    fv = float(value)
    if not math.isfinite(fv):
        raise MagnetEngineError(
            "cbbc_magnet_decay_points_invalid",
            f"decay_points must be finite, got {fv!r}",
        )
    if fv <= 0.0 or fv > 10000.0:
        raise MagnetEngineError(
            "cbbc_magnet_decay_points_invalid",
            f"decay_points must be in (0.0, 10000.0], got {fv!r}",
        )
    return fv


def _is_finite_real(value: object) -> bool:
    """判断 ``value`` 是否为有限实数（非 ``None``、非 ``bool``、非 NaN / ±Inf）。"""
    if value is None or isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


class MagnetEngine:
    """磁吸计算的状态容器与发布器（任务 3.2）。

    设计要点：

    - 所有 ``update_*`` 入口同步、不 await；适用于同步与异步调用方。
    - ``threading.Lock`` 串行化 "compute → 替换 ``_last_result``" 的临界区，
      多线程 / 多协程并发调用 ``update_*`` 仍能保证发布原子性。
    - ``latest()`` 返回 ``_last_result``；当 HSI 已经陈旧（最近 ``hsi_stale_seconds``
      未刷新）时，``latest()`` 返回的对象的 ``hsi_spot_stale`` 字段为 ``True``
      （通过 ``dataclasses.replace`` 构造一个新的不可变对象，原对象不被修改）。
    - 仅当 snapshot 与 hsi_spot 都已知、且最近一次 update_hsi_spot 没有触发 stale 时，
      才会有 ``_last_result``。

    关于"发布"与"返回错误"的契约：

    - ``update_hsi_spot``：
      - 若新输入 ``ts_hk`` 与上一次 ``_last_hsi_ts`` 的间隔严格大于 ``hsi_stale_seconds``，
        视为"HSI 在过去 N 秒内未刷新"。此时不发布新结果对象，把已有 ``_last_result`` 的
        ``hsi_spot_stale`` 翻为 ``True``，并抛 ``MagnetEngineError("hsi_spot_stale")``。
        注意：当前这次 ``update_hsi_spot`` 调用本身就是一次刷新，因此调用结束后
        引擎仍会更新 ``_last_hsi_ts`` / ``_last_hsi_spot``，下一次有效 update 才能恢复。
      - 否则按需发布：当 snapshot 已加载且
        ``|new_hsi_spot - last_used_hsi_spot| > hsi_recompute_threshold_pts`` 时（或当前
        没有 ``_last_result``）立刻调用 ``compute_magnet`` 并替换 ``_last_result``。

    - ``update_snapshot``：
      - 仅替换内部快照。如果当前持有有效（未陈旧）的 hsi_spot，重算并替换 ``_last_result``。

    - ``update_decay_points``：
      - 校验失败时**保留旧值**并抛 ``MagnetEngineError("cbbc_magnet_decay_points_invalid")``。
      - 成功后若 snapshot+hsi_spot 都已知且未陈旧，立即重算并替换 ``_last_result``。
    """

    def __init__(
        self,
        *,
        decay_points: float = 300.0,
        hsi_stale_seconds: float = 5.0,
        hsi_recompute_threshold_pts: float = 5.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # 初始 decay_points 与运行时一样要求合法；非法值在 __init__ 里也直接抛。
        self._decay_points: float = _validate_decay_points(decay_points)

        if not math.isfinite(hsi_stale_seconds) or hsi_stale_seconds <= 0.0:
            raise ValueError(
                f"hsi_stale_seconds must be a positive finite float, got {hsi_stale_seconds!r}"
            )
        if not math.isfinite(hsi_recompute_threshold_pts) or hsi_recompute_threshold_pts < 0.0:
            raise ValueError(
                "hsi_recompute_threshold_pts must be a non-negative finite float, "
                f"got {hsi_recompute_threshold_pts!r}"
            )

        self._hsi_stale_seconds: float = float(hsi_stale_seconds)
        self._hsi_recompute_threshold_pts: float = float(hsi_recompute_threshold_pts)
        self._clock: Callable[[], datetime] = clock if clock is not None else self._default_clock

        self._lock: threading.Lock = threading.Lock()

        self._last_snapshot: CbbcSnapshot | None = None
        # 最近一次 update_hsi_spot 收到的输入（不论是否触发了重算）。
        self._last_hsi_spot: float | None = None
        self._last_hsi_ts: datetime | None = None
        # 最近一次"用于发布 _last_result"的 HSI 现价；用于 |Δ| > threshold 判断。
        self._last_used_hsi_spot: float | None = None
        self._last_result: MagnetResult | None = None
        # 当日 HSI 最低 / 最高价。每次 update_hsi_spot 自动维护;跨日时调用方
        # 应主动调用 ``reset_day_extremes()`` 清零。``compute_magnet`` 用它们
        # 把 call_level 已被吃过的合约硬性当作 0 街货处理 (兜底数据延迟)。
        self._today_low: float | None = None
        self._today_high: float | None = None
        self._today_extremes_date: date | None = None

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _default_clock() -> datetime:
        """默认时钟：返回 Asia/Hong_Kong 时区的当前 ``datetime``。"""
        return datetime.now(_HK_TZ)

    def _now_hk(self) -> datetime:
        return self._clock()

    @staticmethod
    def _gap_seconds(later: datetime, earlier: datetime) -> float:
        """计算两个 ``datetime`` 间隔（秒）。允许跨时区比较（自动归一化到同一 epoch）。"""
        return (later - earlier).total_seconds()

    def _compute_and_publish_locked(self, ts_hk: datetime) -> None:
        """假定已持锁。基于当前 snapshot/hsi_spot/decay_points 计算并替换 ``_last_result``。

        调用前必须确保 ``_last_snapshot is not None`` 且 ``_last_hsi_spot is not None``。
        """
        snapshot = self._last_snapshot
        hsi_spot = self._last_hsi_spot
        assert snapshot is not None and hsi_spot is not None

        new_result = compute_magnet(
            snapshot,
            hsi_spot,
            self._decay_points,
            generated_at_hk=ts_hk,
            hsi_spot_stale=False,
            today_low=self._today_low,
            today_high=self._today_high,
        )
        self._last_result = new_result
        self._last_used_hsi_spot = hsi_spot

    def _mark_existing_result_stale_locked(self) -> None:
        """假定已持锁。把已有 ``_last_result`` 的 ``hsi_spot_stale`` 翻为 ``True``（构造新对象）。"""
        if self._last_result is None:
            return
        if self._last_result.hsi_spot_stale:
            return
        self._last_result = dataclasses.replace(self._last_result, hsi_spot_stale=True)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    def set_today_extremes(
        self,
        *,
        today_low: float | None = None,
        today_high: float | None = None,
        for_date: date | None = None,
    ) -> None:
        """显式设置当日 HSI 高低,用于初始化 / 跨日重置。

        ``for_date`` 提供时,内部 ``_today_extremes_date`` 同步更新,后续
        ``update_hsi_spot`` 在跨日时会自动清零。``None`` 时只清空最高 / 最低值。
        """
        with self._lock:
            self._today_extremes_date = for_date
            self._today_low = (
                float(today_low) if today_low is not None and math.isfinite(today_low) else None
            )
            self._today_high = (
                float(today_high) if today_high is not None and math.isfinite(today_high) else None
            )

    def reset_day_extremes(self) -> None:
        """跨日时调用,清空当日高低记录。"""
        with self._lock:
            self._today_extremes_date = None
            self._today_low = None
            self._today_high = None

    def get_today_extremes(self) -> tuple[float | None, float | None]:
        """返回当前缓存的 ``(today_low, today_high)``;首次 update 之前两者均为 ``None``。"""
        with self._lock:
            return self._today_low, self._today_high

    def update_decay_points(self, value: float) -> None:
        """更新 ``decay_points``。

        校验失败 → 抛 ``MagnetEngineError("cbbc_magnet_decay_points_invalid")``，旧值保留，
        不触发任何重算 / 发布。

        校验成功 → 更新 ``_decay_points``；若 snapshot+hsi_spot 都已知且未陈旧，立刻
        基于新 decay 值重算并替换 ``_last_result``。
        """
        # 注：先校验、后取锁。校验失败抛错时不影响任何状态。
        new_decay = _validate_decay_points(value)
        with self._lock:
            self._decay_points = new_decay
            if self._last_snapshot is None or self._last_hsi_spot is None:
                return
            # 是否陈旧：最近一次有效 hsi 时间戳 vs now。陈旧时不发布新对象，仅记录新 decay。
            if self._last_hsi_ts is not None:
                gap = self._gap_seconds(self._now_hk(), self._last_hsi_ts)
                if gap > self._hsi_stale_seconds:
                    self._mark_existing_result_stale_locked()
                    return
            self._compute_and_publish_locked(
                ts_hk=self._last_hsi_ts if self._last_hsi_ts is not None else self._now_hk()
            )

    def update_snapshot(self, snapshot: CbbcSnapshot) -> None:
        """更新内存中的 CBBC 快照。

        若 hsi_spot 已经设置且未陈旧，立即重算并发布最新 ``MagnetResult``。
        若 hsi_spot 还未设置，只缓存 snapshot；首次 ``update_hsi_spot`` 时再发布。
        """
        with self._lock:
            self._last_snapshot = snapshot
            if self._last_hsi_spot is None or self._last_hsi_ts is None:
                return
            gap = self._gap_seconds(self._now_hk(), self._last_hsi_ts)
            if gap > self._hsi_stale_seconds:
                # 已陈旧：标记现有结果，但不发布新对象。
                self._mark_existing_result_stale_locked()
                return
            self._compute_and_publish_locked(ts_hk=self._last_hsi_ts)

    def update_hsi_spot(self, price: float, ts_hk: datetime) -> None:
        """更新 HSI 现价。

        - ``price`` 非有限 → 静默拒绝（保持状态不变），不抛错也不重算。这是保守选择：
          引擎层一律不污染状态；调用方有责任送进合法值。
        - 与上一次 ``ts_hk`` 间隔严格大于 ``hsi_stale_seconds`` → 把已有 ``_last_result``
          标记为 ``hsi_spot_stale=True``，**不**发布新对象，**仍**记录本次 ``ts_hk``/``price``
          以便下一次刷新可以恢复正常发布；最后抛 ``MagnetEngineError("hsi_spot_stale")``。
        - 否则：按需发布。当 snapshot 已加载且
          ``|price - last_used_hsi_spot| > hsi_recompute_threshold_pts`` 或当前还没有
          ``_last_result`` 时，立刻重算并替换。
        """
        if not _is_finite_real(price):
            return
        if not isinstance(ts_hk, datetime):
            raise TypeError(f"ts_hk must be a datetime, got {type(ts_hk).__name__}")

        price_f = float(price)

        stale_to_raise: bool = False
        with self._lock:
            # 在写入新 ts/price 之前判定 staleness：
            # 若上一次 ts 存在且间隔 > 阈值，则视为 stale 事件。
            if self._last_hsi_ts is not None:
                gap = self._gap_seconds(ts_hk, self._last_hsi_ts)
                if gap > self._hsi_stale_seconds:
                    stale_to_raise = True
                    self._mark_existing_result_stale_locked()

            # 始终更新最新输入（无论是否 stale，便于下一次 update 恢复）。
            self._last_hsi_spot = price_f
            self._last_hsi_ts = ts_hk

            # 自动维护当日 HSI 高低 (B 兜底过滤的输入)。跨日时清零。
            try:
                day = ts_hk.date()
            except Exception:  # noqa: BLE001
                day = None
            if day is not None:
                if self._today_extremes_date != day:
                    self._today_extremes_date = day
                    self._today_low = price_f
                    self._today_high = price_f
                else:
                    if self._today_low is None or price_f < self._today_low:
                        self._today_low = price_f
                    if self._today_high is None or price_f > self._today_high:
                        self._today_high = price_f

            if stale_to_raise:
                # stale 事件：本次不发布新结果对象。
                pass
            else:
                # 正常路径：按需发布
                if self._last_snapshot is not None:
                    needs_recompute: bool
                    if self._last_result is None or self._last_used_hsi_spot is None:
                        needs_recompute = True
                    else:
                        delta = abs(price_f - self._last_used_hsi_spot)
                        needs_recompute = delta > self._hsi_recompute_threshold_pts
                    if needs_recompute:
                        self._compute_and_publish_locked(ts_hk=ts_hk)

        if stale_to_raise:
            raise MagnetEngineError(
                "hsi_spot_stale",
                "HSI_Spot has not been refreshed within hsi_stale_seconds; "
                "publication blocked and existing result marked stale.",
            )

    def latest(self) -> MagnetResult | None:
        """返回最近一次发布的 ``MagnetResult``（``frozen``）。

        若引擎检测到 HSI 已陈旧（``now - last_hsi_ts > hsi_stale_seconds``），返回的
        结果对象的 ``hsi_spot_stale`` 字段为 ``True``（通过 ``dataclasses.replace`` 构造
        新实例返回，并把该新对象作为 ``_last_result`` 的最新版本缓存）。

        若从未发布过结果（snapshot 或 hsi_spot 缺失，或首次 update 就直接 stale），返回 ``None``。
        """
        with self._lock:
            if self._last_result is None:
                return self._last_result
            if self._last_hsi_ts is None:
                return self._last_result
            gap = self._gap_seconds(self._now_hk(), self._last_hsi_ts)
            if gap > self._hsi_stale_seconds and not self._last_result.hsi_spot_stale:
                self._mark_existing_result_stale_locked()
            return self._last_result


__all__ = [
    "HistBucket",
    "MagnetResult",
    "compute_magnet",
    "MagnetEngine",
    "MagnetEngineError",
]
