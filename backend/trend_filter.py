"""
累积趋势市宽方向过滤。
"""
from typing import Literal

from config import (
    CUM_TREND_BULL_MIN_BREADTH_RATIO,
    CUM_TREND_BEAR_MAX_BREADTH_RATIO,
)


TrendDirection = Literal["bull", "bear"]

CUM_TREND_BOUNDARY_POINTS = 30.0
CUM_TREND_DIRECT_POINTS = 40.0
CUM_TREND_BOUNDARY_BEAR_MAX_RSI = 35.0
CUM_TREND_BOUNDARY_BULL_MIN_RSI = 65.0
CUM_TREND_BOUNDARY_EDGE_BUFFER_POINTS = 8.0
CUM_TREND_BOUNDARY_CONTINUATION_POINTS = 2.0


def _is_missing(value: float | None) -> bool:
    if value is None:
        return True
    try:
        return value != value
    except Exception:
        return True


def get_cum_trend_filter_reasons(
    direction: TrendDirection,
    breadth_ratio: float | None,
) -> list[str]:
    """返回累积趋势需要跳过的原因；空列表代表允许入场。"""
    if breadth_ratio is None:
        return ["missing breadth"]

    if direction == "bear":
        if breadth_ratio > CUM_TREND_BEAR_MAX_BREADTH_RATIO:
            return [f"bullish breadth ratio={breadth_ratio:.2f}"]
    else:
        if breadth_ratio < CUM_TREND_BULL_MIN_BREADTH_RATIO:
            return [f"bearish breadth ratio={breadth_ratio:.2f}"]

    return []


def get_cum_trend_boundary_filter_reasons(
    direction: TrendDirection,
    cum5: float,
    rsi: float,
    prev_close: float | None,
    price: float,
    recent_low: float | None,
    recent_high: float | None,
    rsi_oversold: float,
    rsi_overbought: float,
) -> list[str]:
    """
    过滤 30-40 点之间的累积趋势边界单。

    abs(cum5) >= 40 视为强趋势，交回原本趋势规则处理。
    30 < abs(cum5) < 40 必须有 RSI 区间、离近5分钟极值有空间、
    以及当前K线延续确认，避免追在短线尾段。
    """
    if abs(cum5) >= CUM_TREND_DIRECT_POINTS:
        return []

    if abs(cum5) <= CUM_TREND_BOUNDARY_POINTS:
        return [f"weak cum5={cum5:.1f}"]

    reasons: list[str] = []

    if direction == "bear":
        if rsi < rsi_oversold:
            reasons.append(f"RSI extreme oversold rsi={rsi:.2f}")
        if rsi > CUM_TREND_BOUNDARY_BEAR_MAX_RSI:
            reasons.append(f"boundary RSI too high rsi={rsi:.2f}")
        if _is_missing(recent_low):
            reasons.append("missing recent low")
        else:
            low_gap = price - float(recent_low)
            if low_gap < CUM_TREND_BOUNDARY_EDGE_BUFFER_POINTS:
                reasons.append(f"near recent low gap={low_gap:.1f}")
        if _is_missing(prev_close):
            reasons.append("missing previous close")
        else:
            continuation = float(prev_close) - price
            if continuation < CUM_TREND_BOUNDARY_CONTINUATION_POINTS:
                reasons.append(f"no down continuation={continuation:.1f}")

    else:
        if rsi > rsi_overbought:
            reasons.append(f"RSI extreme overbought rsi={rsi:.2f}")
        if rsi < CUM_TREND_BOUNDARY_BULL_MIN_RSI:
            reasons.append(f"boundary RSI too low rsi={rsi:.2f}")
        if _is_missing(recent_high):
            reasons.append("missing recent high")
        else:
            high_gap = float(recent_high) - price
            if high_gap < CUM_TREND_BOUNDARY_EDGE_BUFFER_POINTS:
                reasons.append(f"near recent high gap={high_gap:.1f}")
        if _is_missing(prev_close):
            reasons.append("missing previous close")
        else:
            continuation = price - float(prev_close)
            if continuation < CUM_TREND_BOUNDARY_CONTINUATION_POINTS:
                reasons.append(f"no up continuation={continuation:.1f}")

    return reasons
