"""
累积趋势市宽方向过滤。
"""
from typing import Literal

from config import (
    CUM_TREND_BULL_MIN_BREADTH_RATIO,
    CUM_TREND_BEAR_MAX_BREADTH_RATIO,
)


TrendDirection = Literal["bull", "bear"]


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
