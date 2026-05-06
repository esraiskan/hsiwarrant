"""
放量动能追价过滤。
"""
from typing import Literal

from config import (
    MOMENTUM_BEAR_MIN_RSI,
    MOMENTUM_BULL_MAX_RSI,
    MOMENTUM_BEAR_MAX_BREADTH_RATIO,
    MOMENTUM_BULL_MIN_BREADTH_RATIO,
)


MomentumDirection = Literal["bull", "bear"]


def get_momentum_filter_reasons(
    direction: MomentumDirection,
    rsi: float,
    breadth_ratio: float | None,
) -> list[str]:
    """返回放量动能需要跳过的原因；空列表代表允许入场。"""
    reasons: list[str] = []

    if direction == "bear":
        if rsi < MOMENTUM_BEAR_MIN_RSI:
            reasons.append(f"RSI oversold rsi={rsi:.2f}")
        if (
            breadth_ratio is not None
            and breadth_ratio > MOMENTUM_BEAR_MAX_BREADTH_RATIO
        ):
            reasons.append(f"bullish breadth ratio={breadth_ratio:.2f}")
    else:
        if rsi > MOMENTUM_BULL_MAX_RSI:
            reasons.append(f"RSI overbought rsi={rsi:.2f}")
        if (
            breadth_ratio is not None
            and breadth_ratio < MOMENTUM_BULL_MIN_BREADTH_RATIO
        ):
            reasons.append(f"bearish breadth ratio={breadth_ratio:.2f}")

    return reasons
