"""
盘中市况分类器。

只负责根据已出现的 1M K 线和快照计算市场状态，不参与交易决策。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

import pandas as pd

from models import MarketRegime


OPENING_RANGE_END = time(10, 0)
EARLY_REPAIR_POINTS = 60.0
EARLY_REPAIR_MINUTES_LOW = 15.0
EARLY_REPAIR_MINUTES_HIGH = 25.0
MIN_REPAIR_POINTS = 80.0
FAST_REPAIR_MINUTES = 75.0


@dataclass
class _IntradayContext:
    current_time: datetime
    current_price: float
    day_open: float
    day_high: float
    day_low: float
    prev_close: float | None
    or_high: float | None
    or_low: float | None
    or_mid: float | None
    or_range: float | None
    vwap: float | None
    vwap_slope: float | None
    had_breakdown: bool
    breakdown_low: float | None
    breakdown_time: datetime | None
    position_pct: float | None


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(result) or result <= 0:
        return None
    return result


def _market_snapshot_value(snapshot: dict | None, key: str) -> float | None:
    if not snapshot:
        return None
    return _safe_float(snapshot.get(key))


def _latest_trading_day(df: pd.DataFrame) -> pd.DataFrame:
    latest_date = df.index[-1].date()
    return df[df.index.date == latest_date]


def _opening_range(day_df: pd.DataFrame) -> tuple[float | None, float | None]:
    opening_df = day_df[day_df.index.time <= OPENING_RANGE_END]
    if len(opening_df) < 10:
        return None, None
    return float(opening_df["high"].max()), float(opening_df["low"].min())


def _build_context(df_1m: pd.DataFrame, snapshot: dict | None) -> _IntradayContext | None:
    if df_1m is None or len(df_1m) < 2:
        return None

    day_df = _latest_trading_day(df_1m)
    if len(day_df) < 2:
        return None

    current_time = day_df.index[-1].to_pydatetime()
    current_row = day_df.iloc[-1]
    current_price = float(current_row["close"])
    day_open = _market_snapshot_value(snapshot, "open_price") or float(day_df.iloc[0]["open"])
    day_high = _market_snapshot_value(snapshot, "high_price") or float(day_df["high"].max())
    day_low = _market_snapshot_value(snapshot, "low_price") or float(day_df["low"].min())
    prev_close = _market_snapshot_value(snapshot, "prev_close")

    or_high, or_low = _opening_range(day_df)
    or_mid = (or_high + or_low) / 2.0 if or_high is not None and or_low is not None else None
    or_range = or_high - or_low if or_high is not None and or_low is not None else None

    vwap = _safe_float(current_row.get("VWAP"))
    vwap_slope = _safe_float(current_row.get("VWAP_SLOPE"))
    if vwap_slope is None and "VWAP_SLOPE" in current_row:
        try:
            raw_slope = float(current_row["VWAP_SLOPE"])
            vwap_slope = None if pd.isna(raw_slope) else raw_slope
        except (TypeError, ValueError):
            vwap_slope = None

    had_breakdown = False
    breakdown_low = None
    breakdown_time = None
    if or_low is not None:
        after_or = day_df[day_df.index.time > OPENING_RANGE_END]
        breakdown_df = after_or[after_or["low"] < or_low - 3.0]
        if not breakdown_df.empty:
            had_breakdown = True
            low_idx = breakdown_df["low"].idxmin()
            breakdown_low = float(breakdown_df.loc[low_idx, "low"])
            breakdown_time = low_idx.to_pydatetime()

    day_range = day_high - day_low
    position_pct = None if day_range <= 0 else ((current_price - day_low) / day_range) * 100.0

    return _IntradayContext(
        current_time=current_time,
        current_price=current_price,
        day_open=day_open,
        day_high=day_high,
        day_low=day_low,
        prev_close=prev_close,
        or_high=or_high,
        or_low=or_low,
        or_mid=or_mid,
        or_range=or_range,
        vwap=vwap,
        vwap_slope=vwap_slope,
        had_breakdown=had_breakdown,
        breakdown_low=breakdown_low,
        breakdown_time=breakdown_time,
        position_pct=position_pct,
    )


def _base_payload(ctx: _IntradayContext, *, regime: str, label: str, bias: str,
                  confidence: int, overbought: tuple[float, float],
                  oversold: tuple[float, float], advice: str,
                  reasons: list[str]) -> MarketRegime:
    return MarketRegime(
        regime=regime,
        label=label,
        bias=bias,
        confidence=confidence,
        suggested_rsi_overbought_low=overbought[0],
        suggested_rsi_overbought_high=overbought[1],
        suggested_rsi_oversold_low=oversold[0],
        suggested_rsi_oversold_high=oversold[1],
        advice=advice,
        reasons=reasons,
        updated_at=ctx.current_time.strftime("%Y-%m-%d %H:%M:%S"),
        update_interval_seconds=60,
        current_price=round(ctx.current_price, 2),
        day_open=round(ctx.day_open, 2),
        previous_close=round(ctx.prev_close, 2) if ctx.prev_close is not None else None,
        opening_range_high=round(ctx.or_high, 2) if ctx.or_high is not None else None,
        opening_range_low=round(ctx.or_low, 2) if ctx.or_low is not None else None,
        opening_range_mid=round(ctx.or_mid, 2) if ctx.or_mid is not None else None,
        day_position_pct=round(ctx.position_pct, 1) if ctx.position_pct is not None else None,
    )


def classify_market_regime(df_1m: pd.DataFrame, snapshot: dict | None = None) -> MarketRegime:
    """根据截至当前 1M K 线的数据分类盘中市况。"""
    ctx = _build_context(df_1m, snapshot)
    if ctx is None:
        return MarketRegime(
            regime="unknown",
            label="等待資料",
            bias="neutral",
            confidence=0,
            suggested_rsi_overbought_low=68,
            suggested_rsi_overbought_high=72,
            suggested_rsi_oversold_low=28,
            suggested_rsi_oversold_high=32,
            advice="K線資料不足，暫時用中性 RSI 門檻觀察。",
            reasons=["等待足夠 1 分鐘 K 線"],
            updated_at="",
            update_interval_seconds=60,
        )

    reasons: list[str] = []
    price = ctx.current_price
    below_vwap = ctx.vwap is not None and price < ctx.vwap
    above_vwap = ctx.vwap is not None and price > ctx.vwap
    vwap_down = ctx.vwap_slope is not None and ctx.vwap_slope < 0
    vwap_up = ctx.vwap_slope is not None and ctx.vwap_slope > 0
    below_or_low = ctx.or_low is not None and price < ctx.or_low
    above_or_high = ctx.or_high is not None and price > ctx.or_high
    above_or_mid = ctx.or_mid is not None and price > ctx.or_mid
    below_or_mid = ctx.or_mid is not None and price < ctx.or_mid
    above_prev = ctx.prev_close is not None and price > ctx.prev_close
    below_prev = ctx.prev_close is not None and price < ctx.prev_close
    weak_position = ctx.position_pct is not None and ctx.position_pct <= 30
    strong_position = ctx.position_pct is not None and ctx.position_pct >= 70

    fast_repair = False
    if ctx.had_breakdown and ctx.breakdown_low is not None and ctx.breakdown_time is not None:
        minutes_since_low = (ctx.current_time - ctx.breakdown_time).total_seconds() / 60.0
        repair_points = price - ctx.breakdown_low
        repair_threshold = max(MIN_REPAIR_POINTS, (ctx.or_range or 0.0) * 0.6)
        fast_repair = (
            minutes_since_low <= FAST_REPAIR_MINUTES
            and repair_points >= repair_threshold
            and above_or_mid
        )
        if fast_repair:
            reasons.append(f"{minutes_since_low:.0f}分鐘內由低位反彈{repair_points:.0f}點")

    if fast_repair and (above_prev or above_or_high):
        reasons.extend(["跌穿早段低位後快速收復", "已升穿前收或早段高位"])
        return _base_payload(
            ctx,
            regime="breakdown_failure_repair",
            label="急彈修復",
            bias="repair",
            confidence=88,
            overbought=(78, 82),
            oversold=(28, 35),
            advice="弱勢跌穿失敗，低 RSI 超買買熊應暫停，等價格轉弱確認。",
            reasons=reasons,
        )

    if ctx.had_breakdown and ctx.breakdown_low is not None and ctx.breakdown_time is not None:
        minutes_since_low = (ctx.current_time - ctx.breakdown_time).total_seconds() / 60.0
        repair_points = price - ctx.breakdown_low
        early_repair = (
            EARLY_REPAIR_MINUTES_LOW <= minutes_since_low <= EARLY_REPAIR_MINUTES_HIGH
            and repair_points >= EARLY_REPAIR_POINTS
            and below_or_mid
        )
        if early_repair:
            reasons.extend([
                f"{minutes_since_low:.0f}分鐘內由低位反彈{repair_points:.0f}點",
                "仍未升穿早段中軸",
            ])
            return _base_payload(
                ctx,
                regime="early_repair_rally",
                label="急彈修復早段",
                bias="repair",
                confidence=80,
                overbought=(80, 85),
                oversold=(28, 35),
                advice="跌穿早段低位後快速反抽，暫停 RSI 超買買熊，等轉弱確認。",
                reasons=reasons,
            )

    if ctx.had_breakdown and above_or_mid:
        reasons.extend(["跌穿後已收復早段中軸", "弱勢判斷降級"])
        return _base_payload(
            ctx,
            regime="repair_warning",
            label="弱勢失效預警",
            bias="repair",
            confidence=72,
            overbought=(72, 78),
            oversold=(28, 34),
            advice="反彈修復中，避免用低超買值追買熊。",
            reasons=reasons,
        )

    if below_or_low and weak_position and (below_vwap or below_prev):
        if below_or_low:
            reasons.append("跌穿開市首30分鐘低位")
        if below_vwap:
            reasons.append("價格低於VWAP")
        if vwap_down:
            reasons.append("VWAP斜率向下")
        if below_prev:
            reasons.append("價格低於前收")
        return _base_payload(
            ctx,
            regime="weak_continuation",
            label="弱勢延續",
            bias="bearish",
            confidence=84,
            overbought=(55, 60),
            oversold=(15, 22),
            advice="反彈偏沽，超買區下移；仍需留意快速收復早段中軸的失效訊號。",
            reasons=reasons,
        )

    if below_or_mid and weak_position:
        reasons.append("價格處於日內區間下方")
        if below_vwap:
            reasons.append("價格低於VWAP")
        return _base_payload(
            ctx,
            regime="weak_bounce",
            label="弱勢偏淡",
            bias="bearish",
            confidence=66,
            overbought=(58, 62),
            oversold=(18, 25),
            advice="反彈仍偏弱，超買值可低於中性，但要等跌穿失敗訊號解除。",
            reasons=reasons,
        )

    if above_or_high and strong_position and (above_vwap or vwap_up):
        reasons.append("升穿早段高位")
        if above_vwap:
            reasons.append("價格高於VWAP")
        if vwap_up:
            reasons.append("VWAP斜率向上")
        return _base_payload(
            ctx,
            regime="strong_trend",
            label="強勢單邊",
            bias="bullish",
            confidence=82,
            overbought=(80, 85),
            oversold=(35, 45),
            advice="強勢市不宜用低超買值估頂，熊證訊號要等明確轉弱。",
            reasons=reasons,
        )

    reasons.append("未有效突破早段區間或方向訊號混合")
    if above_vwap:
        reasons.append("價格暫在VWAP上方")
    elif below_vwap:
        reasons.append("價格暫在VWAP下方")
    return _base_payload(
        ctx,
        regime="neutral_range",
        label="中性震盪",
        bias="neutral",
        confidence=55,
        overbought=(68, 72),
        oversold=(28, 32),
        advice="按區間處理，RSI 超買超賣用中性門檻。",
        reasons=reasons,
    )
