"""
CBBC 街货密集区分析 (read-only,不参与交易决策)。

把当前内存里的 ``CbbcSnapshot`` + 当日 HSI 高低聚合成"目标位 / 支撑位"
列表,供前端展示用。**严格只读**:不发起任何状态变更,不被引用于
``HSIStrategyEngine`` 的入场 / 闸门 / consult 路径。

主要数据结构:
- ``ZoneCluster``: 单个 25pt 桶的聚合结果 (notional / shares / 距离)
- ``ZonesPayload``: ``GET /api/cbbc/zones`` + WS ``type=cbbc_zones`` 的载荷

主要函数:
- ``compute_zones(snapshot, spot, today_low=None, today_high=None) -> ZonesPayload``
  纯函数。0 副作用。

filters:
- 跳过 ``outstanding_shares <= 0`` 的占位合约
- 跳过被今天 HSI 高低吃过的合约 (``call_level >= today_low`` 的牛证 /
  ``call_level <= today_high`` 的熊证) — 与 ``compute_magnet`` 的兜底过滤
  逻辑一致,确保展示与磁吸引擎看到的是同一份"真实活跃"数据。
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Literal, Sequence


if TYPE_CHECKING:  # pragma: no cover
    from cbbc_storage import CbbcRecord, CbbcSnapshot


# 25pt 桶宽 — 与 HSI 牛熊证常见 strike 间距对齐,可读性好。
DEFAULT_BUCKET_PTS: int = 25
# 桶过滤阈值 — 拉力 < 该值的桶不出现在结果里 (避免太多噪音条)。
# 单位:HKD。1×10^11 ≈ 1 千亿,取 5 个数量级以下的零碎都过滤。
DEFAULT_MIN_NOTIONAL_HKD: float = 1.0e11


@dataclass(frozen=True)
class ZoneCluster:
    """单个 25pt 桶的聚合结果。"""
    bucket_low: float           # 桶下沿 (整数 25pt 倍数)
    bucket_high: float          # 桶上沿 = bucket_low + bucket_pts
    direction: Literal["bull", "bear"]
    distance_pts: float         # 桶中点距离 spot (带方向: 正 = 上方, 负 = 下方)
    notional_hkd: float         # 该桶 sum(outstanding * er_ratio)
    contract_count: int         # 该桶有效合约数
    outstanding_shares: float   # 该桶 sum(outstanding_shares)
    # 桶内"代表性"收回价 — 距 spot 最近的活合约,用作 UI 上的真实档位
    # 显示。例如桶下沿 25375 但真实活档在 25368,UI 应该显示 25368 而不是
    # 25375,否则用户看到 today_low=25369 < 25375 会以为这档已经死了 (实际
    # 上 25368 还差 1.66pt 才会被吃)。
    nearest_call_level: float
    # 桶内距 today_low / today_high 的距离 (按 direction 取相应一侧):
    # bull → today_low - nearest_call_level (正数 = 还活着,越小越接近被吃)
    # bear → nearest_call_level - today_high (正数 = 还活着,越小越接近被吃)
    safety_margin_pts: float | None = None


@dataclass(frozen=True)
class ZonesPayload:
    """API 载荷:目标位 (上方 bear) + 支撑位 (下方 bull)。"""
    spot: float
    today_low: float | None
    today_high: float | None
    bucket_pts: int
    # 上方目标 (bear-direction),按 distance 升序 (最近的在前)
    targets_above: tuple[ZoneCluster, ...]
    # 下方支撑 (bull-direction),按 distance 升序绝对值 (最近的在前)
    supports_below: tuple[ZoneCluster, ...]
    # 总活跃合约数 (过滤后)
    live_record_count: int
    # 因今日高低被过滤掉的合约数 (B 兜底)
    killed_record_count: int
    # 衍生的操作建议 (read-only,纯展示;不参与交易决策)
    bull_setup: "TradeSetup | None" = None
    bear_setup: "TradeSetup | None" = None


@dataclass(frozen=True)
class TradeSetup:
    """单方向的操作建议 (read-only)。

    全部字段都是从 ``ZonesPayload`` 用纯函数推导出来,**不**会被引用于
    任何交易决策路径 (``_submit_entry_order`` / 闸门 / consult)。
    """
    direction: Literal["bull", "bear"]
    # 入场区:[low, high] 价格范围;UI 上展示成 "25620 - 25640"
    entry_low: float
    entry_high: float
    # 第一止盈:最近一档反向街货墙
    take_profit_1: float
    # 第二止盈:更远一档;没有则与第一止盈相同
    take_profit_2: float
    # 硬止损:对侧最近"墙"或当日极值附近,取较保守值
    stop_loss: float
    # 风险:reward (target_1 / stop_distance);<1.0 时建议跳过
    risk_reward: float
    # 一句话理由 (供 tooltip 展示)
    rationale: str


def _bucket_low_for(call_level: float, bucket_pts: int) -> int:
    """``call_level`` → 桶下沿。例如 25817 / 25pt → 25800。"""
    return int(round(call_level / bucket_pts) * bucket_pts)


def compute_zones(
    snapshot: "CbbcSnapshot | None",
    spot: float,
    *,
    today_low: float | None = None,
    today_high: float | None = None,
    bucket_pts: int = DEFAULT_BUCKET_PTS,
    min_notional_hkd: float = DEFAULT_MIN_NOTIONAL_HKD,
    max_targets: int = 6,
    max_supports: int = 6,
) -> ZonesPayload:
    """聚合 ``snapshot`` 里 ``street_vol > 0`` 且未被今日 HSI 高低吃掉的合约,
    按 25pt 桶分组,返回上方 / 下方各前 N 档。

    返回的 ``ZoneCluster`` 已按距离升序排列 (最近的在 ``[0]``)。
    """
    if snapshot is None or not math.isfinite(spot) or spot <= 0:
        return ZonesPayload(
            spot=float(spot) if math.isfinite(spot) else 0.0,
            today_low=today_low,
            today_high=today_high,
            bucket_pts=bucket_pts,
            targets_above=(),
            supports_below=(),
            live_record_count=0,
            killed_record_count=0,
            bull_setup=None,
            bear_setup=None,
        )

    bear_buckets: dict[int, list] = defaultdict(list)  # bucket_low → [records]
    bull_buckets: dict[int, list] = defaultdict(list)
    live_count = 0
    killed_count = 0

    for r in snapshot.records:
        try:
            outstanding = float(r.outstanding_shares)
            er = float(r.er_ratio)
            cl = float(r.call_level)
        except Exception:  # noqa: BLE001
            continue
        if outstanding <= 0 or er <= 0 or not math.isfinite(cl):
            continue
        # B 兜底过滤 — 与 compute_magnet 一致
        if r.direction == "bull" and today_low is not None and cl >= today_low:
            killed_count += 1
            continue
        if r.direction == "bear" and today_high is not None and cl <= today_high:
            killed_count += 1
            continue

        live_count += 1
        bucket = _bucket_low_for(cl, bucket_pts)
        if r.direction == "bull":
            bull_buckets[bucket].append(r)
        elif r.direction == "bear":
            bear_buckets[bucket].append(r)

    # 上方目标:bear 桶,bucket_low >= spot
    targets: list[ZoneCluster] = []
    for bl, recs in bear_buckets.items():
        if bl < spot:
            continue
        notional = sum(float(r.outstanding_shares) * float(r.er_ratio) for r in recs)
        if notional < min_notional_hkd:
            continue
        # 桶内距 spot 最近的活合约 call_level (bear 是 spot 上方,取最小者)
        nearest_cl = min(float(r.call_level) for r in recs)
        margin = (nearest_cl - today_high) if today_high is not None and math.isfinite(today_high) else None
        targets.append(ZoneCluster(
            bucket_low=float(bl),
            bucket_high=float(bl + bucket_pts),
            direction="bear",
            distance_pts=float(bl - spot),
            notional_hkd=notional,
            contract_count=len(recs),
            outstanding_shares=sum(float(r.outstanding_shares) for r in recs),
            nearest_call_level=nearest_cl,
            safety_margin_pts=margin,
        ))

    # 下方支撑:bull 桶,bucket_low < spot
    supports: list[ZoneCluster] = []
    for bl, recs in bull_buckets.items():
        if bl >= spot:
            continue
        notional = sum(float(r.outstanding_shares) * float(r.er_ratio) for r in recs)
        if notional < min_notional_hkd:
            continue
        # 桶内距 spot 最近的活合约 call_level (bull 是 spot 下方,取最大者)
        nearest_cl = max(float(r.call_level) for r in recs)
        margin = (today_low - nearest_cl) if today_low is not None and math.isfinite(today_low) else None
        supports.append(ZoneCluster(
            bucket_low=float(bl),
            bucket_high=float(bl + bucket_pts),
            direction="bull",
            distance_pts=float(bl - spot),  # 负数
            notional_hkd=notional,
            contract_count=len(recs),
            outstanding_shares=sum(float(r.outstanding_shares) for r in recs),
            nearest_call_level=nearest_cl,
            safety_margin_pts=margin,
        ))

    # 排序:targets 按 distance 升序 (最近的最有意义);supports 按 |distance| 升序
    targets.sort(key=lambda z: z.distance_pts)
    supports.sort(key=lambda z: -z.distance_pts)  # closest to spot first (least negative)

    targets_tup = tuple(targets[:max_targets])
    supports_tup = tuple(supports[:max_supports])

    bull_setup = _derive_bull_setup(spot, targets_tup, today_low)
    bear_setup = _derive_bear_setup(spot, supports_tup, today_high)

    return ZonesPayload(
        spot=float(spot),
        today_low=today_low,
        today_high=today_high,
        bucket_pts=bucket_pts,
        targets_above=targets_tup,
        supports_below=supports_tup,
        live_record_count=live_count,
        killed_record_count=killed_count,
        bull_setup=bull_setup,
        bear_setup=bear_setup,
    )


# --------------------------------------------------------------------------- #
# Trade setup derivation (read-only suggestions)
# --------------------------------------------------------------------------- #

# 入场区上下浮动,默认 ±10pt
_DEFAULT_ENTRY_BAND_PTS: float = 10.0
# 止损与现价的最大距离,避免在密集带太远时把止损扛到空旷区
_DEFAULT_MAX_STOP_DISTANCE_PTS: float = 50.0
# 止损与今日极值的距离 (穿刺保护)
_STOP_BEYOND_DAY_EXTREME_PTS: float = 5.0
# 风险/回报比阈值,小于此值不出建议
_MIN_RR_RATIO: float = 1.0


def _derive_bull_setup(
    spot: float,
    targets: Sequence[ZoneCluster],
    today_low: float | None,
) -> "TradeSetup | None":
    """根据当前 zones 推导做多建议;无法构造时返回 None。"""
    if not targets:
        return None
    tp1 = targets[0].bucket_low
    if tp1 <= spot:
        return None  # 数据异常 — 上方目标不应低于现价
    tp2 = targets[1].bucket_low if len(targets) > 1 else tp1

    entry_low = spot - _DEFAULT_ENTRY_BAND_PTS
    entry_high = spot + _DEFAULT_ENTRY_BAND_PTS

    # 止损取两个候选中较近的:今日低点 + 5pt 缓冲 / 现价 - 50pt
    stop_candidates: list[float] = []
    if today_low is not None and math.isfinite(today_low) and today_low < spot:
        stop_candidates.append(today_low - _STOP_BEYOND_DAY_EXTREME_PTS)
    stop_candidates.append(spot - _DEFAULT_MAX_STOP_DISTANCE_PTS)
    # 距离现价最近的止损 (保守)
    stop_loss = max(stop_candidates)
    if stop_loss >= spot:
        return None

    risk = spot - stop_loss
    reward = tp1 - spot
    rr = reward / risk if risk > 0 else 0.0
    if rr < _MIN_RR_RATIO:
        return TradeSetup(
            direction="bull",
            entry_low=round(entry_low, 1),
            entry_high=round(entry_high, 1),
            take_profit_1=round(tp1, 1),
            take_profit_2=round(tp2, 1),
            stop_loss=round(stop_loss, 1),
            risk_reward=round(rr, 2),
            rationale=f"risk_reward {rr:.2f} < {_MIN_RR_RATIO},不建议入场 — 第一目标距离 {reward:.0f}pt,止损距离 {risk:.0f}pt",
        )

    rationale = (
        f"第一目标 {tp1:.0f} ({reward:+.0f}pt,熊证 {targets[0].contract_count} 只 / "
        f"{targets[0].notional_hkd / 1e9:.1f}十亿 HKD); "
        f"第二目标 {tp2:.0f} ({tp2 - spot:+.0f}pt); "
        f"止损 {stop_loss:.0f} ({stop_loss - spot:+.0f}pt); "
        f"R:R = {rr:.2f}"
    )
    return TradeSetup(
        direction="bull",
        entry_low=round(entry_low, 1),
        entry_high=round(entry_high, 1),
        take_profit_1=round(tp1, 1),
        take_profit_2=round(tp2, 1),
        stop_loss=round(stop_loss, 1),
        risk_reward=round(rr, 2),
        rationale=rationale,
    )


def _derive_bear_setup(
    spot: float,
    supports: Sequence[ZoneCluster],
    today_high: float | None,
) -> "TradeSetup | None":
    """根据当前 zones 推导做空建议;无法构造时返回 None。"""
    if not supports:
        return None
    tp1 = supports[0].bucket_low
    if tp1 >= spot:
        return None
    tp2 = supports[1].bucket_low if len(supports) > 1 else tp1

    entry_low = spot - _DEFAULT_ENTRY_BAND_PTS
    entry_high = spot + _DEFAULT_ENTRY_BAND_PTS

    stop_candidates: list[float] = []
    if today_high is not None and math.isfinite(today_high) and today_high > spot:
        stop_candidates.append(today_high + _STOP_BEYOND_DAY_EXTREME_PTS)
    stop_candidates.append(spot + _DEFAULT_MAX_STOP_DISTANCE_PTS)
    # 距离现价最近的止损
    stop_loss = min(stop_candidates)
    if stop_loss <= spot:
        return None

    risk = stop_loss - spot
    reward = spot - tp1
    rr = reward / risk if risk > 0 else 0.0
    if rr < _MIN_RR_RATIO:
        return TradeSetup(
            direction="bear",
            entry_low=round(entry_low, 1),
            entry_high=round(entry_high, 1),
            take_profit_1=round(tp1, 1),
            take_profit_2=round(tp2, 1),
            stop_loss=round(stop_loss, 1),
            risk_reward=round(rr, 2),
            rationale=f"risk_reward {rr:.2f} < {_MIN_RR_RATIO},不建议入场 — 第一目标距离 {reward:.0f}pt,止损距离 {risk:.0f}pt",
        )

    rationale = (
        f"第一目标 {tp1:.0f} ({tp1 - spot:+.0f}pt,牛证 {supports[0].contract_count} 只 / "
        f"{supports[0].notional_hkd / 1e9:.1f}十亿 HKD); "
        f"第二目标 {tp2:.0f} ({tp2 - spot:+.0f}pt); "
        f"止损 {stop_loss:.0f} ({stop_loss - spot:+.0f}pt); "
        f"R:R = {rr:.2f}"
    )
    return TradeSetup(
        direction="bear",
        entry_low=round(entry_low, 1),
        entry_high=round(entry_high, 1),
        take_profit_1=round(tp1, 1),
        take_profit_2=round(tp2, 1),
        stop_loss=round(stop_loss, 1),
        risk_reward=round(rr, 2),
        rationale=rationale,
    )


__all__ = [
    "DEFAULT_BUCKET_PTS",
    "DEFAULT_MIN_NOTIONAL_HKD",
    "ZoneCluster",
    "ZonesPayload",
    "TradeSetup",
    "compute_zones",
]
