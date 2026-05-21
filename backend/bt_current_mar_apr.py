"""
Backtest current strategy logic for Mar/Apr 2026 using Futu historical K-lines.

Assumptions for offline backtest:
- RSI length/thresholds are overridden to RSI(6), 20/80.
- Take-profit and stop-loss are both 20 HSI points.
- Each completed win/loss is counted as fixed +/-400 HKD.
- Entries are assumed filled immediately at the 1M close price.
- Historical breadth/order-book/pending-order behaviour is not replayed; signals that
  require unavailable historical breadth are skipped by the same conservative rule.
"""
from __future__ import annotations

import math
import sys
import time
import argparse
from dataclasses import dataclass
from datetime import date

import pandas as pd
from futu import AuType, KLType, OpenQuoteContext, RET_OK

sys.path.insert(0, ".")

from config import FUTU_HOST, FUTU_PORT, SYMBOL, VOL_MA_PERIOD
from futu_data import calc_rsi, calc_vwap
from models import PositionType
from momentum_filter import get_momentum_filter_reasons
from strategy import (
    CUM_TREND_ENTRY_MODE,
    CUM_TREND_RSI_BUFFER,
    CUM_TREND_VWAP_CONFIRM_BARS,
    EXTREME_COMPLETED_K_MAX_ADVERSE_MOVE_POINTS,
    EXTREME_COMPLETED_K_MAX_FAVORABLE_MOVE_POINTS,
    EXTREME_COMPLETED_K_RSI_BUFFER,
    EXTREME_ENTRY_MODES,
    EXTREME_STOP_REVERSAL_GUARD_SECONDS,
    EXTREME_VOLUME_SURGE_MULTIPLIER,
    FORCE_EXIT_TIME,
    MOMENTUM_ENTRY_MODE,
    MOMENTUM_LATE_ENTRY_TIME,
    MOMENTUM_LATE_VOLUME_SURGE_MULTIPLIER,
    MOMENTUM_MAX_K_BODY_POINTS,
    MOMENTUM_MIN_K_BODY_POINTS,
    MOMENTUM_VOLUME_SURGE_MULTIPLIER,
    SAME_SIDE_TAKE_PROFIT_COOLDOWN_SECONDS,
    VERY_EXTREME_AVG_VOLUME_MULTIPLIER,
    VERY_EXTREME_PULLBACK_POINTS,
    VERY_EXTREME_RSI_OVERBOUGHT,
    VERY_EXTREME_RSI_OVERSOLD,
    VERY_EXTREME_SHADOW_BEAR_ENTRY_MODE,
    VERY_EXTREME_SHADOW_BEAR_RSI,
    VERY_EXTREME_SHADOW_BULL_ENTRY_MODE,
    VERY_EXTREME_SHADOW_BULL_RSI,
    VERY_EXTREME_SHADOW_MAX_ENTRY_CHASE_POINTS,
    VERY_EXTREME_SHADOW_MIN_LOWER_SHADOW_POINTS,
    VERY_EXTREME_SHADOW_MIN_PULLBACK_POINTS,
    VERY_EXTREME_SHADOW_MIN_REBOUND_POINTS,
    VERY_EXTREME_SHADOW_MIN_UPPER_SHADOW_POINTS,
    VERY_EXTREME_SHADOW_MIN_VOLUME_RATIO,
)
from trend_filter import CUM_TREND_BOUNDARY_POINTS, get_cum_trend_boundary_filter_reasons


BT_RSI_LENGTH = 6
BT_RSI_OVERSOLD = 20.0
BT_RSI_OVERBOUGHT = 80.0
BT_TP_POINTS = 20.0
BT_SL_POINTS = 20.0
BT_FIXED_PNL_HKD = 400
ENTRY_CUTOFF_TIME = "15:45"
ONLY_EXTREME_ENTRIES = False
SUMMARY_ONLY = False
CUM_TREND_PROXY = False
INCLUDE_MOMENTUM = False
RSI_EXTREME_BULL_LENGTH: int | None = None
RSI_EXTREME_BEAR_LENGTH: int | None = None
RSI_MOMENTUM_LENGTH: int | None = None
RSI_CUM_TREND_LENGTH: int | None = None


@dataclass
class Trade:
    trade_date: str
    side: str
    mode: str
    entry_time: str
    exit_time: str
    entry: float
    exit: float
    points: float
    result: str
    pnl_hkd: int
    minutes: float
    desc: str


def _is_nan(value) -> bool:
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return True


def _time_part(ts: pd.Timestamp) -> str:
    return ts.strftime("%H:%M")


def _is_open_filter(ts: pd.Timestamp) -> bool:
    text = _time_part(ts)
    return text < "09:35" or ("13:00" <= text < "13:05")


def _is_after_entry_cutoff(ts: pd.Timestamp) -> bool:
    return _time_part(ts) >= ENTRY_CUTOFF_TIME


def _is_after_force_exit(ts: pd.Timestamp) -> bool:
    return _time_part(ts) >= FORCE_EXIT_TIME


def _required_momentum_volume_multiplier(ts: pd.Timestamp) -> float:
    if _time_part(ts) >= MOMENTUM_LATE_ENTRY_TIME:
        return MOMENTUM_LATE_VOLUME_SURGE_MULTIPLIER
    return MOMENTUM_VOLUME_SURGE_MULTIPLIER


def _extreme_signal_side(
    rsi: float,
    momentum_ratio: float,
    current_price: float,
    k_high: float,
    k_low: float,
) -> tuple[PositionType, str]:
    if rsi < BT_RSI_OVERSOLD and momentum_ratio > EXTREME_VOLUME_SURGE_MULTIPLIER:
        return PositionType.BULL, "放量动能触发"
    if rsi > BT_RSI_OVERBOUGHT and momentum_ratio > EXTREME_VOLUME_SURGE_MULTIPLIER:
        return PositionType.BEAR, "放量动能触发"

    if momentum_ratio <= VERY_EXTREME_AVG_VOLUME_MULTIPLIER:
        return PositionType.NONE, ""
    if rsi <= VERY_EXTREME_RSI_OVERSOLD and current_price >= k_low + VERY_EXTREME_PULLBACK_POINTS:
        return PositionType.BULL, "非常极端RSI均量反抽"
    if rsi >= VERY_EXTREME_RSI_OVERBOUGHT and current_price <= k_high - VERY_EXTREME_PULLBACK_POINTS:
        return PositionType.BEAR, "非常极端RSI均量回落"
    return PositionType.NONE, ""


def _rsi_col(length: int | None = None) -> str:
    active_length = BT_RSI_LENGTH if length is None else int(length)
    return "RSI" if active_length == BT_RSI_LENGTH else f"RSI_{active_length}"


def _row_rsi(row, length: int | None = None) -> float:
    return float(row[_rsi_col(length)])


def _completed_very_extreme_shadow_signal(
    row,
    current_price: float,
    current_bull_rsi: float,
    current_bear_rsi: float,
    bull_rsi: float,
    bear_rsi: float,
) -> tuple[PositionType, str]:
    close = float(row["close"])
    open_price = float(row["open"])
    high = float(row["high"])
    low = float(row["low"])
    volume = float(row["volume"])
    vol_ma = float(row["VOL_MA"])
    if (_is_nan(bull_rsi) and _is_nan(bear_rsi)) or _is_nan(vol_ma) or vol_ma <= 0:
        return PositionType.NONE, ""

    momentum_ratio = volume / vol_ma
    if momentum_ratio < VERY_EXTREME_SHADOW_MIN_VOLUME_RATIO:
        return PositionType.NONE, ""

    if bull_rsi <= VERY_EXTREME_SHADOW_BULL_RSI:
        lower_shadow = min(open_price, close) - low
        rebound_from_low = close - low
        move_from_signal = current_price - close
        if (
            lower_shadow >= VERY_EXTREME_SHADOW_MIN_LOWER_SHADOW_POINTS
            and rebound_from_low >= VERY_EXTREME_SHADOW_MIN_REBOUND_POINTS
            and move_from_signal <= VERY_EXTREME_SHADOW_MAX_ENTRY_CHASE_POINTS
            and current_bull_rsi <= VERY_EXTREME_SHADOW_BULL_RSI + EXTREME_COMPLETED_K_RSI_BUFFER
        ):
            return PositionType.BULL, (
                f"上一根完成K触发 | {VERY_EXTREME_SHADOW_BULL_ENTRY_MODE} | "
                f"RSI:{bull_rsi:.2f} | 低位反抽{rebound_from_low:.1f}点 | "
                f"下影{lower_shadow:.1f}点 | {momentum_ratio:.2f}x量 | "
                f"当前偏离:{move_from_signal:+.1f}点"
            )

    if bear_rsi >= VERY_EXTREME_SHADOW_BEAR_RSI:
        upper_shadow = high - max(open_price, close)
        pullback_from_high = high - close
        move_from_signal = current_price - close
        if (
            upper_shadow >= VERY_EXTREME_SHADOW_MIN_UPPER_SHADOW_POINTS
            and pullback_from_high >= VERY_EXTREME_SHADOW_MIN_PULLBACK_POINTS
            and move_from_signal >= -VERY_EXTREME_SHADOW_MAX_ENTRY_CHASE_POINTS
            and current_bear_rsi >= VERY_EXTREME_SHADOW_BEAR_RSI - EXTREME_COMPLETED_K_RSI_BUFFER
        ):
            return PositionType.BEAR, (
                f"上一根完成K触发 | {VERY_EXTREME_SHADOW_BEAR_ENTRY_MODE} | "
                f"RSI:{bear_rsi:.2f} | 高位回落{pullback_from_high:.1f}点 | "
                f"上影{upper_shadow:.1f}点 | {momentum_ratio:.2f}x量 | "
                f"当前偏离:{move_from_signal:+.1f}点"
            )

    return PositionType.NONE, ""


def _completed_extreme_signal(row, current_price: float, current_row) -> tuple[PositionType, str, str]:
    bull_rsi = _row_rsi(row, RSI_EXTREME_BULL_LENGTH)
    bear_rsi = _row_rsi(row, RSI_EXTREME_BEAR_LENGTH)
    current_bull_rsi = _row_rsi(current_row, RSI_EXTREME_BULL_LENGTH)
    current_bear_rsi = _row_rsi(current_row, RSI_EXTREME_BEAR_LENGTH)
    shadow_side, shadow_message = _completed_very_extreme_shadow_signal(
        row,
        current_price,
        current_bull_rsi,
        current_bear_rsi,
        bull_rsi,
        bear_rsi,
    )
    if shadow_side != PositionType.NONE:
        mode = VERY_EXTREME_SHADOW_BULL_ENTRY_MODE if shadow_side == PositionType.BULL else VERY_EXTREME_SHADOW_BEAR_ENTRY_MODE
        return shadow_side, mode, shadow_message

    close = float(row["close"])
    open_price = float(row["open"])
    high = float(row["high"])
    low = float(row["low"])
    volume = float(row["volume"])
    vol_ma = float(row["VOL_MA"])
    if (_is_nan(bull_rsi) and _is_nan(bear_rsi)) or _is_nan(vol_ma) or vol_ma <= 0:
        return PositionType.NONE, "", ""

    k_change = close - open_price
    k_body_points = abs(k_change)
    if k_change == 0 or not (MOMENTUM_MIN_K_BODY_POINTS <= k_body_points <= MOMENTUM_MAX_K_BODY_POINTS):
        return PositionType.NONE, "", ""

    momentum_ratio = volume / vol_ma
    bull_side, bull_trigger = _extreme_signal_side(bull_rsi, momentum_ratio, current_price, high, low)
    bear_side, bear_trigger = _extreme_signal_side(bear_rsi, momentum_ratio, current_price, high, low)
    side = PositionType.NONE
    trigger_label = ""
    rsi = bull_rsi
    if bull_side == PositionType.BULL:
        side = bull_side
        trigger_label = bull_trigger
        rsi = bull_rsi
    elif bear_side == PositionType.BEAR:
        side = bear_side
        trigger_label = bear_trigger
        rsi = bear_rsi
    if side == PositionType.NONE:
        return PositionType.NONE, "", ""

    move_from_signal = current_price - close
    mode = "极度超卖" if side == PositionType.BULL else "极度超买"

    if side == PositionType.BEAR:
        if current_bear_rsi < BT_RSI_OVERBOUGHT - EXTREME_COMPLETED_K_RSI_BUFFER:
            return PositionType.NONE, "", ""
        if move_from_signal > EXTREME_COMPLETED_K_MAX_ADVERSE_MOVE_POINTS:
            return PositionType.NONE, "", ""
        if move_from_signal < -EXTREME_COMPLETED_K_MAX_FAVORABLE_MOVE_POINTS:
            return PositionType.NONE, "", ""
    elif side == PositionType.BULL:
        if current_bull_rsi > BT_RSI_OVERSOLD + EXTREME_COMPLETED_K_RSI_BUFFER:
            return PositionType.NONE, "", ""
        if move_from_signal < -EXTREME_COMPLETED_K_MAX_ADVERSE_MOVE_POINTS:
            return PositionType.NONE, "", ""
        if move_from_signal > EXTREME_COMPLETED_K_MAX_FAVORABLE_MOVE_POINTS:
            return PositionType.NONE, "", ""

    desc = (
        f"上一根完成K触发 | {trigger_label} | RSI:{rsi:.2f} | "
        f"{'阳线涨' if k_change > 0 else '阴线跌'}{k_body_points:.1f}点 | "
        f"{momentum_ratio:.2f}x量 | 当前偏离:{move_from_signal:+.1f}点"
    )
    return side, mode, desc


def _prepare_indicators(d1: pd.DataFrame) -> pd.DataFrame:
    out = d1.copy()
    out["RSI"] = calc_rsi(out["close"], length=BT_RSI_LENGTH)
    for length in (6, 8, 10, 14):
        if length != BT_RSI_LENGTH:
            out[f"RSI_{length}"] = calc_rsi(out["close"], length=length)
    vol_series = out["turnover"] if "turnover" in out.columns and out["turnover"].sum() > 0 else out["volume"]
    out["VWAP"] = calc_vwap(out["high"], out["low"], out["close"], vol_series)
    out["VWAP_SLOPE"] = out["VWAP"].diff()
    out["volume"] = vol_series
    out["VOL_MA"] = vol_series.rolling(window=VOL_MA_PERIOD).mean()
    return out


def _get_15m_row(d15: pd.DataFrame, ts: pd.Timestamp):
    rows = d15[d15.index <= ts]
    if rows.empty:
        return None
    return rows.iloc[-1]


def _detect_entry(
    d1: pd.DataFrame,
    d15: pd.DataFrame,
    i: int,
    last_completed_extreme_kline_time: str,
    enable_cum_trend_proxy: bool,
) -> tuple[PositionType, str, str, str] | None:
    row = d1.iloc[i]
    prev = d1.iloc[i - 1]
    ts = d1.index[i]
    price = float(row["close"])
    rsi = float(row["RSI"])
    bull_extreme_rsi = _row_rsi(row, RSI_EXTREME_BULL_LENGTH)
    bear_extreme_rsi = _row_rsi(row, RSI_EXTREME_BEAR_LENGTH)
    momentum_rsi = _row_rsi(row, RSI_MOMENTUM_LENGTH)
    volume = float(row["volume"])
    vol_ma = float(row["VOL_MA"])
    if _is_nan(rsi) or _is_nan(vol_ma) or vol_ma <= 0:
        return None

    curr_slope = float(row["VWAP_SLOPE"])
    prev_slope = float(prev["VWAP_SLOPE"]) if not _is_nan(prev["VWAP_SLOPE"]) else 0.0
    vwap_turning_up = (prev_slope <= 0 and curr_slope > 0) or (curr_slope > 0 and curr_slope > prev_slope)
    vwap_turning_down = (prev_slope >= 0 and curr_slope < 0) or (curr_slope < 0 and curr_slope < prev_slope)
    vol_is_high = volume > vol_ma

    k_open = float(row["open"])
    k_close = float(row["close"])
    k_high = float(row["high"])
    k_low = float(row["low"])
    k_body = abs(k_close - k_open)
    k_is_green = k_close > k_open
    k_is_red = k_close < k_open
    lower_shadow = min(k_open, k_close) - k_low
    upper_shadow = k_high - max(k_open, k_close)
    k_bull_pattern = k_is_green or lower_shadow > k_body * 1.0
    k_bear_pattern = k_is_red or upper_shadow > k_body * 1.0

    m15 = _get_15m_row(d15, ts)
    if m15 is None:
        return None
    m15_is_green = float(m15["close"]) > float(m15["open"])
    m15_is_red = float(m15["close"]) < float(m15["open"])

    bull_normal = (
        BT_RSI_OVERSOLD <= rsi < 25
        and vwap_turning_up
        and vol_is_high
        and k_bull_pattern
        and m15_is_green
    )
    if not ONLY_EXTREME_ENTRIES and bull_normal:
        return PositionType.BULL, "普通超卖", f"HSI:{price:.2f} RSI:{rsi:.2f}", ""

    bear_normal = (
        75 < rsi <= BT_RSI_OVERBOUGHT
        and vwap_turning_down
        and vol_is_high
        and k_bear_pattern
        and m15_is_red
    )
    if not ONLY_EXTREME_ENTRIES and bear_normal:
        return PositionType.BEAR, "普通超买", f"HSI:{price:.2f} RSI:{rsi:.2f}", ""

    k_change = k_close - k_open
    k_body_points = abs(k_change)
    momentum_ratio = volume / vol_ma
    momentum_body_ok = MOMENTUM_MIN_K_BODY_POINTS <= k_body_points <= MOMENTUM_MAX_K_BODY_POINTS
    bull_extreme_side, bull_extreme_trigger = _extreme_signal_side(
        bull_extreme_rsi,
        momentum_ratio,
        price,
        k_high,
        k_low,
    )
    bear_extreme_side, bear_extreme_trigger = _extreme_signal_side(
        bear_extreme_rsi,
        momentum_ratio,
        price,
        k_high,
        k_low,
    )
    required_momentum_ratio = _required_momentum_volume_multiplier(ts)
    vol_surge = volume > vol_ma * required_momentum_ratio

    if momentum_body_ok and bull_extreme_side == PositionType.BULL and k_change != 0:
        desc = (
            f"{bull_extreme_trigger} | RSI:{bull_extreme_rsi:.2f} | "
            f"{'阳线涨' if k_change > 0 else '阴线跌'}{k_body_points:.1f}点 | "
            f"{momentum_ratio:.2f}x量"
        )
        return PositionType.BULL, "极度超卖", desc, ""

    if momentum_body_ok and bear_extreme_side == PositionType.BEAR and k_change != 0:
        desc = (
            f"{bear_extreme_trigger} | RSI:{bear_extreme_rsi:.2f} | "
            f"{'阳线涨' if k_change > 0 else '阴线跌'}{k_body_points:.1f}点 | "
            f"{momentum_ratio:.2f}x量"
        )
        return PositionType.BEAR, "极度超买", desc, ""

    if INCLUDE_MOMENTUM and momentum_body_ok and vol_surge and k_change > 0:
        if not get_momentum_filter_reasons("bull", momentum_rsi, None):
            return PositionType.BULL, MOMENTUM_ENTRY_MODE, f"阳线涨{k_change:.1f}点 | {momentum_ratio:.1f}x量", ""

    if INCLUDE_MOMENTUM and momentum_body_ok and vol_surge and k_change < 0:
        if not get_momentum_filter_reasons("bear", momentum_rsi, None):
            return PositionType.BEAR, MOMENTUM_ENTRY_MODE, f"阴线跌{abs(k_change):.1f}点 | {momentum_ratio:.1f}x量", ""

    completed_kline_time = d1.index[i - 1].strftime("%Y-%m-%d %H:%M:%S")
    if completed_kline_time != last_completed_extreme_kline_time:
        side, mode, message = _completed_extreme_signal(prev, price, row)
        if side != PositionType.NONE:
            return side, mode, message, completed_kline_time

    if enable_cum_trend_proxy:
        cumtrend = _detect_cum_trend_proxy(d1, i)
        if cumtrend is not None:
            side, mode, desc = cumtrend
            return side, mode, desc, ""

    # Current code requires breadth for cum-trend. Historical K-line replay has no
    # breadth series, so this path remains conservatively skipped.
    return None


def _completed_vwap_slopes_confirm(d1: pd.DataFrame, i: int, side: PositionType) -> bool:
    if i < CUM_TREND_VWAP_CONFIRM_BARS + 1:
        return False
    completed = d1.iloc[:i]
    slopes = completed.tail(CUM_TREND_VWAP_CONFIRM_BARS)["VWAP_SLOPE"]
    if any(_is_nan(value) for value in slopes):
        return False
    if side == PositionType.BULL:
        return all(float(value) > 0 for value in slopes)
    if side == PositionType.BEAR:
        return all(float(value) < 0 for value in slopes)
    return False


def _detect_cum_trend_proxy(d1: pd.DataFrame, i: int) -> tuple[PositionType, str, str] | None:
    """K-line proxy for cumulative trend. Breadth filter is not available in history."""
    if i < 7:
        return None
    row = d1.iloc[i]
    prev = d1.iloc[i - 1]
    recent_window = d1.iloc[max(0, i - 6):i]
    recent_closes = [d1.iloc[j]["close"] for j in range(i - 6, i)]
    completed_cum5 = float(recent_closes[-1] - recent_closes[0])
    if abs(completed_cum5) <= CUM_TREND_BOUNDARY_POINTS:
        return None
    price = float(row["close"])
    open_price = float(d1.iloc[0]["open"])
    day_high = float(d1.iloc[: i + 1]["high"].max())
    day_low = float(d1.iloc[: i + 1]["low"].min())
    if day_high - day_low < 100:
        return None
    rsi = _row_rsi(prev, RSI_CUM_TREND_LENGTH)

    if completed_cum5 < -CUM_TREND_BOUNDARY_POINTS and price - open_price < 0:
        if rsi < BT_RSI_OVERSOLD + CUM_TREND_RSI_BUFFER:
            return None
        if not _completed_vwap_slopes_confirm(d1, i, PositionType.BEAR):
            return None
        reasons = get_cum_trend_boundary_filter_reasons(
            "bear",
            completed_cum5,
            rsi,
            float(prev["close"]),
            price,
            float(recent_window["low"].min()),
            float(recent_window["high"].max()),
            BT_RSI_OVERSOLD,
            BT_RSI_OVERBOUGHT,
        )
        if reasons:
            return None
        desc = (
            f"累积趋势代理 | 5根累跌{completed_cum5:.1f}点 | "
            f"日内{day_high - day_low:.0f}点 | 完成K RSI:{rsi:.2f}"
        )
        return PositionType.BEAR, CUM_TREND_ENTRY_MODE, desc

    if completed_cum5 > CUM_TREND_BOUNDARY_POINTS and price - open_price > 0:
        if rsi > BT_RSI_OVERBOUGHT - CUM_TREND_RSI_BUFFER:
            return None
        if not _completed_vwap_slopes_confirm(d1, i, PositionType.BULL):
            return None
        reasons = get_cum_trend_boundary_filter_reasons(
            "bull",
            completed_cum5,
            rsi,
            float(prev["close"]),
            price,
            float(recent_window["low"].min()),
            float(recent_window["high"].max()),
            BT_RSI_OVERSOLD,
            BT_RSI_OVERBOUGHT,
        )
        if reasons:
            return None
        desc = (
            f"累积趋势代理 | 5根累涨{completed_cum5:.1f}点 | "
            f"日内{day_high - day_low:.0f}点 | 完成K RSI:{rsi:.2f}"
        )
        return PositionType.BULL, CUM_TREND_ENTRY_MODE, desc

    return None


def run_day(d1_day: pd.DataFrame, d15_day: pd.DataFrame, trade_date: date) -> tuple[list[Trade], int]:
    trades: list[Trade] = []
    position = PositionType.NONE
    entry = 0.0
    entry_time: pd.Timestamp | None = None
    entry_mode = ""
    entry_desc = ""
    last_tp_side = PositionType.NONE
    last_tp_time: pd.Timestamp | None = None
    last_completed_extreme_kline_time = ""
    last_extreme_stop_mode = ""
    last_extreme_stop_position = PositionType.NONE
    last_extreme_stop_time: pd.Timestamp | None = None
    skipped_cum_trend_candidates = 0

    start_idx = max(VOL_MA_PERIOD, BT_RSI_LENGTH) + 1
    for i in range(start_idx, len(d1_day)):
        row = d1_day.iloc[i]
        ts = d1_day.index[i]
        price = float(row["close"])
        rsi = float(row["RSI"])
        if _is_nan(rsi) or _is_nan(row["VOL_MA"]):
            continue

        if position != PositionType.NONE:
            diff = price - entry if position == PositionType.BULL else entry - price
            exit_reason = ""
            if diff >= BT_TP_POINTS:
                exit_reason = "W"
            elif diff <= -BT_SL_POINTS:
                exit_reason = "L"
            elif _is_after_force_exit(ts):
                exit_reason = "W" if diff >= 0 else "L"

            if exit_reason:
                minutes = (ts - entry_time).total_seconds() / 60 if entry_time else 0.0
                pnl_hkd = BT_FIXED_PNL_HKD if exit_reason == "W" else -BT_FIXED_PNL_HKD
                trades.append(Trade(
                    trade_date=str(trade_date),
                    side=position.value,
                    mode=entry_mode,
                    entry_time=entry_time.strftime("%H:%M") if entry_time else "",
                    exit_time=ts.strftime("%H:%M"),
                    entry=entry,
                    exit=price,
                    points=diff,
                    result=exit_reason,
                    pnl_hkd=pnl_hkd,
                    minutes=minutes,
                    desc=entry_desc,
                ))
                if exit_reason == "W":
                    last_tp_side = position
                    last_tp_time = ts
                elif entry_mode in EXTREME_ENTRY_MODES:
                    last_extreme_stop_mode = entry_mode
                    last_extreme_stop_position = position
                    last_extreme_stop_time = ts
                position = PositionType.NONE
                entry = 0.0
                entry_time = None
                entry_mode = ""
                entry_desc = ""
            continue

        if _is_open_filter(ts) or _is_after_entry_cutoff(ts):
            continue

        if CUM_TREND_PROXY and _detect_cum_trend_proxy(d1_day, i) is not None:
            skipped_cum_trend_candidates += 1

        detected = _detect_entry(d1_day, d15_day, i, last_completed_extreme_kline_time, CUM_TREND_PROXY)
        if detected is None:
            continue

        side, mode, desc, completed_kline_time = detected
        if completed_kline_time:
            last_completed_extreme_kline_time = completed_kline_time

        if last_tp_side == side and last_tp_time is not None:
            elapsed = (ts - last_tp_time).total_seconds()
            if elapsed < SAME_SIDE_TAKE_PROFIT_COOLDOWN_SECONDS:
                continue

        if last_extreme_stop_time is not None:
            elapsed = (ts - last_extreme_stop_time).total_seconds()
            if elapsed < EXTREME_STOP_REVERSAL_GUARD_SECONDS:
                if (
                    side == PositionType.BULL
                    and last_extreme_stop_mode == "极度超买"
                    and last_extreme_stop_position == PositionType.BEAR
                ):
                    continue
                if (
                    side == PositionType.BEAR
                    and last_extreme_stop_mode == "极度超卖"
                    and last_extreme_stop_position == PositionType.BULL
                ):
                    continue

        position = side
        entry = price
        entry_time = ts
        entry_mode = mode
        entry_desc = desc

    return trades, skipped_cum_trend_candidates


def fetch_history(ctx: OpenQuoteContext, ktype: KLType, start: str, end: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    page_req_key = None
    while True:
        ret, data, page_req_key = ctx.request_history_kline(
            SYMBOL,
            start=start,
            end=end,
            ktype=ktype,
            autype=AuType.QFQ,
            max_count=1000,
            page_req_key=page_req_key,
        )
        if ret != RET_OK:
            raise RuntimeError(f"Futu request_history_kline failed: {data}")
        if data is not None and len(data) > 0:
            frames.append(data)
        if page_req_key is None:
            break
        time.sleep(0.25)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["time_key"] = pd.to_datetime(out["time_key"])
    out = out.drop_duplicates(subset=["time_key"]).sort_values("time_key")
    out.set_index("time_key", inplace=True)
    return out


def summarize(label: str, trades: list[Trade], trading_days: list[date]) -> None:
    wins = sum(1 for trade in trades if trade.result == "W")
    losses = sum(1 for trade in trades if trade.result == "L")
    total = wins + losses
    pnl = sum(trade.pnl_hkd for trade in trades)
    win_rate = wins / total * 100 if total else 0.0
    print(f"\n【{label}總結】交易日:{len(trading_days)} 筆數:{total} {wins}W/{losses}L 勝率:{win_rate:.1f}% PnL:{pnl:+.0f} HKD")
    by_mode: dict[str, list[int]] = {}
    for trade in trades:
        by_mode.setdefault(trade.mode, [0, 0, 0])
        if trade.result == "W":
            by_mode[trade.mode][0] += 1
        else:
            by_mode[trade.mode][1] += 1
        by_mode[trade.mode][2] += trade.pnl_hkd
    if by_mode:
        print("  按買入策略:")
        for mode, (w, l, mode_pnl) in sorted(by_mode.items()):
            count = w + l
            wr = w / count * 100 if count else 0.0
            print(f"    {mode}: {count}筆 {w}W/{l}L 勝率:{wr:.1f}% PnL:{mode_pnl:+.0f} HKD")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest Mar/Apr current strategy with Futu K-lines.")
    parser.add_argument("--rsi-length", type=int, default=6, help="RSI length, e.g. 6 or 14.")
    parser.add_argument("--rsi-extreme-bull", type=int, default=None, help="RSI length for extreme oversold/bull entries.")
    parser.add_argument("--rsi-extreme-bear", type=int, default=None, help="RSI length for extreme overbought/bear entries.")
    parser.add_argument("--rsi-momentum", type=int, default=None, help="RSI length for momentum filters.")
    parser.add_argument("--rsi-cumtrend", type=int, default=None, help="RSI length for cumulative trend proxy.")
    parser.add_argument("--only-extreme", action="store_true", help="Only count extreme entry modes.")
    parser.add_argument("--cumtrend-proxy", action="store_true", help="Enable K-line proxy for cumulative trend mode.")
    parser.add_argument("--include-momentum", action="store_true", help="Include momentum entry mode.")
    parser.add_argument("--summary-only", action="store_true", help="Do not print per-trade detail.")
    return parser.parse_args()


def main() -> int:
    global BT_RSI_LENGTH, ONLY_EXTREME_ENTRIES, SUMMARY_ONLY, CUM_TREND_PROXY, INCLUDE_MOMENTUM
    global RSI_EXTREME_BULL_LENGTH, RSI_EXTREME_BEAR_LENGTH, RSI_MOMENTUM_LENGTH, RSI_CUM_TREND_LENGTH
    args = parse_args()
    BT_RSI_LENGTH = int(args.rsi_length)
    RSI_EXTREME_BULL_LENGTH = args.rsi_extreme_bull
    RSI_EXTREME_BEAR_LENGTH = args.rsi_extreme_bear
    RSI_MOMENTUM_LENGTH = args.rsi_momentum
    RSI_CUM_TREND_LENGTH = args.rsi_cumtrend
    ONLY_EXTREME_ENTRIES = bool(args.only_extreme)
    CUM_TREND_PROXY = bool(args.cumtrend_proxy)
    INCLUDE_MOMENTUM = bool(args.include_momentum)
    SUMMARY_ONLY = bool(args.summary_only)

    print("連接 Futu OpenD...")
    ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
    try:
        print("拉取 1M K 線 2026-03-01 至 2026-04-30...")
        d1 = fetch_history(ctx, KLType.K_1M, "2026-03-01", "2026-04-30")
        print("拉取 15M K 線 2026-03-01 至 2026-04-30...")
        d15 = fetch_history(ctx, KLType.K_15M, "2026-03-01", "2026-04-30")
    finally:
        ctx.close()

    if d1.empty or d15.empty:
        print("沒有拉到足夠 K 線。")
        return 1

    d1 = _prepare_indicators(d1)
    d15 = _prepare_indicators(d15)

    dates = sorted(d for d in set(d1.index.date) if d.month in (3, 4))
    all_trades: list[Trade] = []
    total_skipped_cum = 0
    extreme_text = "，只計極度類型買入" if ONLY_EXTREME_ENTRIES else ""
    cumtrend_text = "，包含累積趨勢代理" if CUM_TREND_PROXY else ""
    momentum_text = "，包含放量动能" if INCLUDE_MOMENTUM else ""
    mix_text = ""
    if any(v is not None for v in (RSI_EXTREME_BULL_LENGTH, RSI_EXTREME_BEAR_LENGTH, RSI_MOMENTUM_LENGTH, RSI_CUM_TREND_LENGTH)):
        mix_text = (
            f"，混合RSI bull:{RSI_EXTREME_BULL_LENGTH or BT_RSI_LENGTH}"
            f"/bear:{RSI_EXTREME_BEAR_LENGTH or BT_RSI_LENGTH}"
            f"/mom:{RSI_MOMENTUM_LENGTH or BT_RSI_LENGTH}"
            f"/cum:{RSI_CUM_TREND_LENGTH or BT_RSI_LENGTH}"
        )
    print(f"\n參數: RSI({BT_RSI_LENGTH}), thresholds 20/80, TP=20點, SL=20點, 每次勝負固定 +/-400 HKD{extreme_text}{cumtrend_text}{momentum_text}{mix_text}")
    print("成交假設: HSI 1M close 即時成交；歷史 breadth/order-book 不重播。")
    print("=" * 120)

    for day in dates:
        d1_day = d1[d1.index.date == day].copy()
        d15_day = d15[d15.index.date == day].copy()
        if len(d1_day) < 30 or d15_day.empty:
            continue
        trades, skipped_cum = run_day(d1_day, d15_day, day)
        total_skipped_cum += skipped_cum
        all_trades.extend(trades)

        wins = sum(1 for trade in trades if trade.result == "W")
        losses = sum(1 for trade in trades if trade.result == "L")
        day_pnl = sum(trade.pnl_hkd for trade in trades)
        if SUMMARY_ONLY:
            if trades:
                print(f"{day}: {len(trades)}筆 {wins}W/{losses}L PnL:{day_pnl:+.0f} HKD")
            else:
                print(f"{day}: 無交易")
        elif trades:
            print(f"{day}: {len(trades)}筆 {wins}W/{losses}L PnL:{day_pnl:+.0f} HKD")
            for trade in trades:
                side_label = "牛證" if trade.side == PositionType.BULL.value else "熊證"
                print(
                    f"  {trade.result} {trade.entry_time}->{trade.exit_time} "
                    f"{side_label} {trade.entry:.0f}->{trade.exit:.0f} "
                    f"{trade.points:+.0f}點 {trade.pnl_hkd:+.0f}HKD "
                    f"{trade.minutes:.0f}min | 買入策略:{trade.mode} | {trade.desc}"
                )
        else:
            print(f"{day}: 無交易")

    mar_days = [d for d in dates if d.month == 3]
    apr_days = [d for d in dates if d.month == 4]
    mar_trades = [trade for trade in all_trades if trade.trade_date.startswith("2026-03")]
    apr_trades = [trade for trade in all_trades if trade.trade_date.startswith("2026-04")]

    summarize("三月", mar_trades, mar_days)
    summarize("四月", apr_trades, apr_days)
    summarize("三四月合計", all_trades, dates)
    if CUM_TREND_PROXY:
        print(f"\n累積趨勢備註: 使用 K 線代理版，不含歷史 breadth；代理條件曾出現 {total_skipped_cum} 次候選但未入場。")
    else:
        print(f"\n累積趨勢備註: 因歷史 breadth 未由 K 線提供，按現碼 conservative skip；K線條件曾出現 {total_skipped_cum} 次候選但未入場。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
