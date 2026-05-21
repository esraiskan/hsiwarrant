from __future__ import annotations

import calendar
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from futu import AuType, KLType, OpenQuoteContext, RET_OK

from config import FUTU_HOST, FUTU_PORT, SYMBOL, VOL_MA_PERIOD
from futu_data import calc_rsi, calc_vwap
from models import (
    BacktestBreakdownRow,
    BacktestRequest,
    BacktestResult,
    BacktestSummary,
    BacktestTrade,
    PositionType,
)
from momentum_filter import get_momentum_filter_reasons
from strategy import (
    CUM_TREND_ENTRY_MODE,
    CUM_TREND_RSI_BUFFER,
    CUM_TREND_VWAP_CONFIRM_BARS,
    EXTREME_COMPLETED_K_MAX_ADVERSE_MOVE_POINTS,
    EXTREME_COMPLETED_K_MAX_FAVORABLE_MOVE_POINTS,
    EXTREME_COMPLETED_K_RSI_BUFFER,
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
from trend_filter import (
    CUM_TREND_BOUNDARY_POINTS,
    get_cum_trend_boundary_filter_reasons,
    get_cum_trend_filter_reasons,
)


ENTRY_CUTOFF_TIME = "15:45"
MARKET_LOG_PATH = Path(__file__).with_name("market_log.csv")
RSI_DIVERGENCE_ENTRY_MODE = "RSI背离"
RSI_DIVERGENCE_MIN_SEPARATION_BARS = 7
RSI_DIVERGENCE_MAX_LEG_MINUTES = 30
RSI_DIVERGENCE_MAX_SPAN_MINUTES = 60
RSI_DIVERGENCE_PRICE_GAP_POINTS = 5.0
RSI_DIVERGENCE_RSI_STEP = 3.0
RSI_DIVERGENCE_TOTAL_RSI_STEP = 8.0
RSI_DIVERGENCE_MIN_DAY_MOVE_POINTS = 120.0
RSI_DIVERGENCE_MIN_DAY_RANGE_POINTS = 150.0
RSI_DIVERGENCE_BULL_MAX_RSI = 45.0
RSI_DIVERGENCE_BEAR_MIN_RSI = 55.0
RSI_DIVERGENCE_EXTREME_TOLERANCE_POINTS = 3.0
EXTREME_BRANCH_LABELS = {
    "b1_volume_extreme": "B1 极度RSI+1.3x放量",
    "b2_very_extreme_pullback": "B2 非常极端RSI+回抽",
    "b3_completed_k": "B3 已完成K线补单",
    "b4_shadow_reversal": "B4 非常极端影线反转",
}
STRATEGY_LABELS = {
    "普通超卖": "普通超卖",
    "普通超买": "普通超买",
    "极度超卖": "极度超卖",
    "极度超买": "极度超买",
    MOMENTUM_ENTRY_MODE: "放量动能",
    CUM_TREND_ENTRY_MODE: "累积趋势",
    RSI_DIVERGENCE_ENTRY_MODE: "RSI背离",
}


@dataclass
class _Trade:
    trade_date: str
    side: str
    mode: str
    branch: str
    entry_time: str
    exit_time: str
    entry: float
    exit: float
    points: float
    result: str
    pnl_hkd: float
    minutes: float
    desc: str


@dataclass
class _Runtime:
    request: BacktestRequest
    warnings: set[str]
    breadth_missing_count: int = 0
    breadth_used_count: int = 0
    breadth_proxy_count: int = 0


@dataclass
class _DivergencePoint:
    time: pd.Timestamp
    price: float
    rsi: float


@dataclass
class _RsiDivergenceState:
    bull_lows: list[_DivergencePoint]
    bear_highs: list[_DivergencePoint]
    last_bull_entry_time: pd.Timestamp | None = None
    last_bear_entry_time: pd.Timestamp | None = None
    used_bull_keys: set[str] | None = None
    used_bear_keys: set[str] | None = None
    used_bull_c_times: set[str] | None = None
    used_bear_c_times: set[str] | None = None

    @classmethod
    def create(cls) -> "_RsiDivergenceState":
        return cls(
            bull_lows=[],
            bear_highs=[],
            used_bull_keys=set(),
            used_bear_keys=set(),
            used_bull_c_times=set(),
            used_bear_c_times=set(),
        )


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


def resolve_backtest_range(payload: BacktestRequest) -> tuple[date, date]:
    if payload.period_mode == "months":
        if not payload.months:
            raise ValueError("月份模式必须选择至少一个月份")
        if len(payload.months) > 2:
            raise ValueError("单次最多选择 2 个月份")
        parsed = []
        for value in payload.months:
            try:
                year, month = [int(part) for part in value.split("-")]
                parsed.append((year, month))
            except Exception as exc:
                raise ValueError(f"月份格式无效: {value}") from exc
        parsed.sort()
        start_year, start_month = parsed[0]
        end_year, end_month = parsed[-1]
        start = date(start_year, start_month, 1)
        end = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
    else:
        if not payload.date_start or not payload.date_end:
            raise ValueError("日期范围模式必须提供开始和结束日期")
        start = date.fromisoformat(payload.date_start)
        end = date.fromisoformat(payload.date_end)
        if start > end:
            raise ValueError("开始日期不能晚于结束日期")
    if (end - start).days > 62:
        raise ValueError("单次回测最多支持 62 日")
    return start, end


def _fetch_history(ctx: OpenQuoteContext, ktype: KLType, start: date, end: date) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    page_req_key = None
    while True:
        ret, data, page_req_key = ctx.request_history_kline(
            SYMBOL,
            start=start.isoformat(),
            end=end.isoformat(),
            ktype=ktype,
            autype=AuType.QFQ,
            max_count=1000,
            page_req_key=page_req_key,
        )
        if ret != RET_OK:
            raise RuntimeError(str(data))
        if data is not None and len(data) > 0:
            frames.append(data)
        if page_req_key is None:
            break
        time.sleep(0.2)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["time_key"] = pd.to_datetime(out["time_key"])
    out = out.drop_duplicates(subset=["time_key"]).sort_values("time_key")
    out.set_index("time_key", inplace=True)
    return out


def _prepare_indicators(d1: pd.DataFrame, rsi_length: int) -> pd.DataFrame:
    out = d1.copy()
    out["RSI"] = calc_rsi(out["close"], length=rsi_length)
    vol_series = out["turnover"] if "turnover" in out.columns and out["turnover"].sum() > 0 else out["volume"]
    out["VWAP"] = calc_vwap(out["high"], out["low"], out["close"], vol_series)
    out["VWAP_SLOPE"] = out["VWAP"].diff()
    out["volume"] = vol_series
    out["VOL_MA"] = vol_series.rolling(window=VOL_MA_PERIOD).mean()
    return out


def _load_market_breadth(start: date, end: date, warnings: set[str]) -> pd.DataFrame:
    if not MARKET_LOG_PATH.exists():
        warnings.add("market_log_missing")
        return pd.DataFrame()
    try:
        df = pd.read_csv(MARKET_LOG_PATH)
    except Exception:
        warnings.add("market_log_read_failed")
        return pd.DataFrame()
    if df.empty or "time" not in df.columns or "ratio" not in df.columns:
        warnings.add("market_log_invalid")
        return pd.DataFrame()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time")
    df = df[(df["time"].dt.date >= start) & (df["time"].dt.date <= end)]
    if df.empty:
        warnings.add("market_log_no_range_data")
        return pd.DataFrame()
    df["minute"] = df["time"].dt.floor("min")
    df = df.drop_duplicates(subset=["minute"], keep="last")
    df.set_index("minute", inplace=True)
    return df


def _breadth_ratio_at(breadth: pd.DataFrame, ts: pd.Timestamp) -> float | None:
    if breadth.empty:
        return None
    minute = ts.floor("min")
    rows = breadth[breadth.index <= minute]
    if rows.empty:
        return None
    latest = rows.iloc[-1]
    try:
        return float(latest["ratio"])
    except Exception:
        return None


def _get_15m_row(d15: pd.DataFrame, ts: pd.Timestamp):
    rows = d15[d15.index <= ts]
    if rows.empty:
        return None
    return rows.iloc[-1]


def _extreme_signal_side(
    rsi: float,
    momentum_ratio: float,
    current_price: float,
    k_high: float,
    k_low: float,
    oversold: float,
    overbought: float,
) -> tuple[PositionType, str, str]:
    if rsi < oversold and momentum_ratio > EXTREME_VOLUME_SURGE_MULTIPLIER:
        return PositionType.BULL, "放量动能触发", "b1_volume_extreme"
    if rsi > overbought and momentum_ratio > EXTREME_VOLUME_SURGE_MULTIPLIER:
        return PositionType.BEAR, "放量动能触发", "b1_volume_extreme"
    if momentum_ratio <= VERY_EXTREME_AVG_VOLUME_MULTIPLIER:
        return PositionType.NONE, "", ""
    if rsi <= VERY_EXTREME_RSI_OVERSOLD and current_price >= k_low + VERY_EXTREME_PULLBACK_POINTS:
        return PositionType.BULL, "非常极端RSI均量反抽", "b2_very_extreme_pullback"
    if rsi >= VERY_EXTREME_RSI_OVERBOUGHT and current_price <= k_high - VERY_EXTREME_PULLBACK_POINTS:
        return PositionType.BEAR, "非常极端RSI均量回落", "b2_very_extreme_pullback"
    return PositionType.NONE, "", ""


def _very_extreme_pullback_signal_side(
    rsi: float,
    momentum_ratio: float,
    current_price: float,
    k_high: float,
    k_low: float,
) -> tuple[PositionType, str, str]:
    if momentum_ratio <= VERY_EXTREME_AVG_VOLUME_MULTIPLIER:
        return PositionType.NONE, "", ""
    if rsi <= VERY_EXTREME_RSI_OVERSOLD and current_price >= k_low + VERY_EXTREME_PULLBACK_POINTS:
        return PositionType.BULL, "非常极端RSI均量反抽", "b2_very_extreme_pullback"
    if rsi >= VERY_EXTREME_RSI_OVERBOUGHT and current_price <= k_high - VERY_EXTREME_PULLBACK_POINTS:
        return PositionType.BEAR, "非常极端RSI均量回落", "b2_very_extreme_pullback"
    return PositionType.NONE, "", ""


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


def _completed_extreme_signal(
    prev,
    current_row,
    payload: BacktestRequest,
) -> tuple[PositionType, str, str, str] | None:
    allowed = set(payload.selection.extreme_branches)
    price = float(current_row["close"])
    rsi = float(prev["RSI"])
    current_rsi = float(current_row["RSI"])
    close = float(prev["close"])
    open_price = float(prev["open"])
    high = float(prev["high"])
    low = float(prev["low"])
    volume = float(prev["volume"])
    vol_ma = float(prev["VOL_MA"])
    if _is_nan(rsi) or _is_nan(vol_ma) or vol_ma <= 0:
        return None
    momentum_ratio = volume / vol_ma

    if "b4_shadow_reversal" in allowed:
        lower_shadow = min(open_price, close) - low
        rebound_from_low = close - low
        move_from_signal = price - close
        if (
            rsi <= VERY_EXTREME_SHADOW_BULL_RSI
            and momentum_ratio >= VERY_EXTREME_SHADOW_MIN_VOLUME_RATIO
            and lower_shadow >= VERY_EXTREME_SHADOW_MIN_LOWER_SHADOW_POINTS
            and rebound_from_low >= VERY_EXTREME_SHADOW_MIN_REBOUND_POINTS
            and move_from_signal <= VERY_EXTREME_SHADOW_MAX_ENTRY_CHASE_POINTS
            and current_rsi <= VERY_EXTREME_SHADOW_BULL_RSI + EXTREME_COMPLETED_K_RSI_BUFFER
        ):
            desc = (
                f"上一根完成K触发 | {VERY_EXTREME_SHADOW_BULL_ENTRY_MODE} | RSI:{rsi:.2f} | "
                f"低位反抽{rebound_from_low:.1f}点 | 下影{lower_shadow:.1f}点 | {momentum_ratio:.2f}x量"
            )
            return PositionType.BULL, "极度超卖", "b4_shadow_reversal", desc

        upper_shadow = high - max(open_price, close)
        pullback_from_high = high - close
        if (
            rsi >= VERY_EXTREME_SHADOW_BEAR_RSI
            and momentum_ratio >= VERY_EXTREME_SHADOW_MIN_VOLUME_RATIO
            and upper_shadow >= VERY_EXTREME_SHADOW_MIN_UPPER_SHADOW_POINTS
            and pullback_from_high >= VERY_EXTREME_SHADOW_MIN_PULLBACK_POINTS
            and move_from_signal >= -VERY_EXTREME_SHADOW_MAX_ENTRY_CHASE_POINTS
            and current_rsi >= VERY_EXTREME_SHADOW_BEAR_RSI - EXTREME_COMPLETED_K_RSI_BUFFER
        ):
            desc = (
                f"上一根完成K触发 | {VERY_EXTREME_SHADOW_BEAR_ENTRY_MODE} | RSI:{rsi:.2f} | "
                f"高位回落{pullback_from_high:.1f}点 | 上影{upper_shadow:.1f}点 | {momentum_ratio:.2f}x量"
            )
            return PositionType.BEAR, "极度超买", "b4_shadow_reversal", desc

    if "b3_completed_k" not in allowed:
        return None
    k_change = close - open_price
    k_body_points = abs(k_change)
    if k_change == 0 or k_body_points < MOMENTUM_MIN_K_BODY_POINTS:
        return None
    side, trigger_label, branch = _extreme_signal_side(
        rsi,
        momentum_ratio,
        price,
        high,
        low,
        payload.rsi_oversold,
        payload.rsi_overbought,
    )
    if side == PositionType.NONE:
        return None
    move_from_signal = price - close
    if side == PositionType.BEAR:
        if current_rsi < payload.rsi_overbought - EXTREME_COMPLETED_K_RSI_BUFFER:
            return None
        if move_from_signal > EXTREME_COMPLETED_K_MAX_ADVERSE_MOVE_POINTS:
            return None
        if move_from_signal < -EXTREME_COMPLETED_K_MAX_FAVORABLE_MOVE_POINTS:
            return None
    elif side == PositionType.BULL:
        if current_rsi > payload.rsi_oversold + EXTREME_COMPLETED_K_RSI_BUFFER:
            return None
        if move_from_signal < -EXTREME_COMPLETED_K_MAX_ADVERSE_MOVE_POINTS:
            return None
        if move_from_signal > EXTREME_COMPLETED_K_MAX_FAVORABLE_MOVE_POINTS:
            return None
    desc = (
        f"上一根完成K触发 | {trigger_label} | RSI:{rsi:.2f} | "
        f"{'阳线涨' if k_change > 0 else '阴线跌'}{k_body_points:.1f}点 | {momentum_ratio:.2f}x量"
    )
    return side, "极度超卖" if side == PositionType.BULL else "极度超买", "b3_completed_k", desc


def _detect_cum_trend(
    d1: pd.DataFrame,
    i: int,
    payload: BacktestRequest,
    runtime: _Runtime,
    breadth: pd.DataFrame,
) -> tuple[PositionType, str, str, str] | None:
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
    rsi = float(prev["RSI"])
    breadth_ratio = None
    if payload.cum_trend_mode in ("market_log_breadth", "strict_breadth"):
        breadth_ratio = _breadth_ratio_at(breadth, d1.index[i])
        if breadth_ratio is None:
            runtime.breadth_missing_count += 1
            if payload.cum_trend_mode == "strict_breadth":
                runtime.warnings.add("breadth_missing_skipped")
                return None
            runtime.breadth_proxy_count += 1
            runtime.warnings.add("breadth_fallback_to_proxy")
        else:
            runtime.breadth_used_count += 1

    if completed_cum5 < -CUM_TREND_BOUNDARY_POINTS and price - open_price < 0:
        if rsi < payload.rsi_oversold + CUM_TREND_RSI_BUFFER:
            return None
        if not _completed_vwap_slopes_confirm(d1, i, PositionType.BEAR):
            return None
        reasons = []
        if breadth_ratio is not None:
            reasons.extend(get_cum_trend_filter_reasons("bear", breadth_ratio))
        reasons.extend(get_cum_trend_boundary_filter_reasons(
            "bear",
            completed_cum5,
            rsi,
            float(prev["close"]),
            price,
            float(recent_window["low"].min()),
            float(recent_window["high"].max()),
            payload.rsi_oversold,
            payload.rsi_overbought,
        ))
        if reasons:
            return None
        desc = f"5根累跌{completed_cum5:.1f}点 | 日内{day_high - day_low:.0f}点 | RSI:{rsi:.2f}"
        return PositionType.BEAR, CUM_TREND_ENTRY_MODE, "cum_trend", desc

    if completed_cum5 > CUM_TREND_BOUNDARY_POINTS and price - open_price > 0:
        if rsi > payload.rsi_overbought - CUM_TREND_RSI_BUFFER:
            return None
        if not _completed_vwap_slopes_confirm(d1, i, PositionType.BULL):
            return None
        reasons = []
        if breadth_ratio is not None:
            reasons.extend(get_cum_trend_filter_reasons("bull", breadth_ratio))
        reasons.extend(get_cum_trend_boundary_filter_reasons(
            "bull",
            completed_cum5,
            rsi,
            float(prev["close"]),
            price,
            float(recent_window["low"].min()),
            float(recent_window["high"].max()),
            payload.rsi_oversold,
            payload.rsi_overbought,
        ))
        if reasons:
            return None
        desc = f"5根累涨{completed_cum5:.1f}点 | 日内{day_high - day_low:.0f}点 | RSI:{rsi:.2f}"
        return PositionType.BULL, CUM_TREND_ENTRY_MODE, "cum_trend", desc
    return None


def _append_divergence_point(points: list[_DivergencePoint], point: _DivergencePoint, is_low: bool) -> None:
    if points:
        last = points[-1]
        if point.time == last.time:
            return
        gap_minutes = (point.time - last.time).total_seconds() / 60
        if gap_minutes > RSI_DIVERGENCE_MAX_LEG_MINUTES:
            points.clear()
    points.append(point)
    if len(points) > 20:
        del points[:-20]


def _point_key(a: _DivergencePoint, b: _DivergencePoint, c: _DivergencePoint) -> str:
    return f"{a.time.isoformat()}|{b.time.isoformat()}|{c.time.isoformat()}"


def _find_divergence_triplet(
    points: list[_DivergencePoint],
    side: PositionType,
    used_keys: set[str],
    used_c_times: set[str],
) -> tuple[_DivergencePoint, _DivergencePoint, _DivergencePoint] | None:
    if len(points) < 3:
        return None
    for ci in range(len(points) - 1, 1, -1):
        c = points[ci]
        if c.time.isoformat() in used_c_times:
            continue
        for bi in range(ci - 1, 0, -1):
            b = points[bi]
            bc_seconds = (c.time - b.time).total_seconds()
            if bc_seconds < RSI_DIVERGENCE_MIN_SEPARATION_BARS * 60:
                continue
            if bc_seconds > RSI_DIVERGENCE_MAX_LEG_MINUTES * 60:
                continue
            for ai in range(bi - 1, -1, -1):
                a = points[ai]
                ab_seconds = (b.time - a.time).total_seconds()
                ac_seconds = (c.time - a.time).total_seconds()
                if ab_seconds < RSI_DIVERGENCE_MIN_SEPARATION_BARS * 60:
                    continue
                if ab_seconds > RSI_DIVERGENCE_MAX_LEG_MINUTES * 60:
                    continue
                if ac_seconds > RSI_DIVERGENCE_MAX_SPAN_MINUTES * 60:
                    continue
                if side == PositionType.BULL:
                    if c.price > min(point.price for point in points[: ci + 1]) + RSI_DIVERGENCE_EXTREME_TOLERANCE_POINTS:
                        continue
                    price_ok = (
                        b.price <= a.price - RSI_DIVERGENCE_PRICE_GAP_POINTS
                        and c.price <= b.price - RSI_DIVERGENCE_PRICE_GAP_POINTS
                    )
                    rsi_ok = (
                        b.rsi >= a.rsi + RSI_DIVERGENCE_RSI_STEP
                        and c.rsi >= b.rsi + RSI_DIVERGENCE_RSI_STEP
                        and c.rsi >= a.rsi + RSI_DIVERGENCE_TOTAL_RSI_STEP
                    )
                    if price_ok and rsi_ok and c.rsi <= RSI_DIVERGENCE_BULL_MAX_RSI and _point_key(a, b, c) not in used_keys:
                        return a, b, c
                elif side == PositionType.BEAR:
                    if c.price < max(point.price for point in points[: ci + 1]) - RSI_DIVERGENCE_EXTREME_TOLERANCE_POINTS:
                        continue
                    price_ok = (
                        b.price >= a.price + RSI_DIVERGENCE_PRICE_GAP_POINTS
                        and c.price >= b.price + RSI_DIVERGENCE_PRICE_GAP_POINTS
                    )
                    rsi_ok = (
                        b.rsi <= a.rsi - RSI_DIVERGENCE_RSI_STEP
                        and c.rsi <= b.rsi - RSI_DIVERGENCE_RSI_STEP
                        and c.rsi <= a.rsi - RSI_DIVERGENCE_TOTAL_RSI_STEP
                    )
                    if price_ok and rsi_ok and c.rsi >= RSI_DIVERGENCE_BEAR_MIN_RSI and _point_key(a, b, c) not in used_keys:
                        return a, b, c
    return None


def _detect_rsi_divergence(
    d1: pd.DataFrame,
    i: int,
    state: _RsiDivergenceState,
) -> tuple[PositionType, str, str, str] | None:
    if i < 12:
        return None
    row = d1.iloc[i]
    pivot = d1.iloc[i - 1]
    ts = d1.index[i]
    pivot_ts = d1.index[i - 1]
    pivot_close = float(pivot["close"])
    pivot_rsi = float(pivot["RSI"])
    open_price = float(d1.iloc[0]["open"])
    day_high = float(d1.iloc[: i + 1]["high"].max())
    day_low = float(d1.iloc[: i + 1]["low"].min())
    day_range = day_high - day_low
    if day_range < RSI_DIVERGENCE_MIN_DAY_RANGE_POINTS:
        return None
    if _is_nan(pivot_rsi):
        return None

    prev2_closes = [float(value) for value in d1.iloc[i - 3:i - 1]["close"]]
    current_close = float(row["close"])
    pivot_is_local_close_low = pivot_close <= min(prev2_closes) and pivot_close <= current_close
    pivot_is_local_close_high = pivot_close >= max(prev2_closes) and pivot_close >= current_close

    if pivot_is_local_close_low and open_price - pivot_close >= RSI_DIVERGENCE_MIN_DAY_MOVE_POINTS:
        _append_divergence_point(
            state.bull_lows,
            _DivergencePoint(time=pivot_ts, price=pivot_close, rsi=pivot_rsi),
            is_low=True,
        )
    if pivot_is_local_close_high and pivot_close - open_price >= RSI_DIVERGENCE_MIN_DAY_MOVE_POINTS:
        _append_divergence_point(
            state.bear_highs,
            _DivergencePoint(time=pivot_ts, price=pivot_close, rsi=pivot_rsi),
            is_low=False,
        )

    if state.used_bull_keys is None:
        state.used_bull_keys = set()
    if state.used_bear_keys is None:
        state.used_bear_keys = set()
    if state.used_bull_c_times is None:
        state.used_bull_c_times = set()
    if state.used_bear_c_times is None:
        state.used_bear_c_times = set()

    bull_triplet = _find_divergence_triplet(
        state.bull_lows,
        PositionType.BULL,
        state.used_bull_keys,
        state.used_bull_c_times,
    )
    if bull_triplet is not None:
        a, b, c = bull_triplet
        if state.last_bull_entry_time is None or (ts - state.last_bull_entry_time).total_seconds() >= 15 * 60:
            state.last_bull_entry_time = ts
            state.used_bull_keys.add(_point_key(a, b, c))
            state.used_bull_c_times.add(c.time.isoformat())
            state.bull_lows.clear()
            desc = (
                f"连续底背离 | "
                f"{a.time.strftime('%H:%M')} {a.price:.1f}/RSI{a.rsi:.1f} -> "
                f"{b.time.strftime('%H:%M')} {b.price:.1f}/RSI{b.rsi:.1f} -> "
                f"{c.time.strftime('%H:%M')} {c.price:.1f}/RSI{c.rsi:.1f}"
            )
            return PositionType.BULL, RSI_DIVERGENCE_ENTRY_MODE, "bullish_divergence", desc

    bear_triplet = _find_divergence_triplet(
        state.bear_highs,
        PositionType.BEAR,
        state.used_bear_keys,
        state.used_bear_c_times,
    )
    if bear_triplet is not None:
        a, b, c = bear_triplet
        if state.last_bear_entry_time is None or (ts - state.last_bear_entry_time).total_seconds() >= 15 * 60:
            state.last_bear_entry_time = ts
            state.used_bear_keys.add(_point_key(a, b, c))
            state.used_bear_c_times.add(c.time.isoformat())
            state.bear_highs.clear()
            desc = (
                f"连续顶背离 | "
                f"{a.time.strftime('%H:%M')} {a.price:.1f}/RSI{a.rsi:.1f} -> "
                f"{b.time.strftime('%H:%M')} {b.price:.1f}/RSI{b.rsi:.1f} -> "
                f"{c.time.strftime('%H:%M')} {c.price:.1f}/RSI{c.rsi:.1f}"
            )
            return PositionType.BEAR, RSI_DIVERGENCE_ENTRY_MODE, "bearish_divergence", desc

    return None


def _detect_entry(
    d1: pd.DataFrame,
    d15: pd.DataFrame,
    i: int,
    payload: BacktestRequest,
    runtime: _Runtime,
    breadth: pd.DataFrame,
    last_completed_extreme_kline_time: str,
    divergence_state: _RsiDivergenceState,
) -> tuple[PositionType, str, str, str] | None:
    row = d1.iloc[i]
    prev = d1.iloc[i - 1]
    ts = d1.index[i]
    price = float(row["close"])
    rsi = float(row["RSI"])
    volume = float(row["volume"])
    vol_ma = float(row["VOL_MA"])
    if _is_nan(rsi) or _is_nan(vol_ma) or vol_ma <= 0:
        return None
    strategies = set(payload.selection.strategies)
    allowed_branches = set(payload.selection.extreme_branches)
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
    lower_shadow = min(k_open, k_close) - k_low
    upper_shadow = k_high - max(k_open, k_close)
    k_bull_pattern = k_close > k_open or lower_shadow > k_body * 1.0
    k_bear_pattern = k_close < k_open or upper_shadow > k_body * 1.0
    m15 = _get_15m_row(d15, ts)
    if m15 is None:
        return None
    m15_is_green = float(m15["close"]) > float(m15["open"])
    m15_is_red = float(m15["close"]) < float(m15["open"])

    if "normal" in strategies:
        if payload.rsi_oversold <= rsi < 25 and vwap_turning_up and vol_is_high and k_bull_pattern and m15_is_green:
            return PositionType.BULL, "普通超卖", "normal", f"HSI:{price:.2f} RSI:{rsi:.2f}"
        if 75 < rsi <= payload.rsi_overbought and vwap_turning_down and vol_is_high and k_bear_pattern and m15_is_red:
            return PositionType.BEAR, "普通超买", "normal", f"HSI:{price:.2f} RSI:{rsi:.2f}"

    k_change = k_close - k_open
    k_body_points = abs(k_change)
    momentum_ratio = volume / vol_ma
    min_body_ok = k_body_points >= MOMENTUM_MIN_K_BODY_POINTS
    momentum_body_ok = min_body_ok and k_body_points <= MOMENTUM_MAX_K_BODY_POINTS
    if "extreme" in strategies and min_body_ok and k_change != 0:
        side, trigger_label, branch = _extreme_signal_side(
            rsi,
            momentum_ratio,
            price,
            k_high,
            k_low,
            payload.rsi_oversold,
            payload.rsi_overbought,
        )
        if side != PositionType.NONE and branch in allowed_branches:
            desc = (
                f"{trigger_label} | RSI:{rsi:.2f} | "
                f"{'阳线涨' if k_change > 0 else '阴线跌'}{k_body_points:.1f}点 | {momentum_ratio:.2f}x量"
            )
            return side, "极度超卖" if side == PositionType.BULL else "极度超买", branch, desc

        if "b2_very_extreme_pullback" in allowed_branches:
            side, trigger_label, branch = _very_extreme_pullback_signal_side(
                rsi,
                momentum_ratio,
                price,
                k_high,
                k_low,
            )
            if side != PositionType.NONE:
                desc = (
                    f"{trigger_label} | RSI:{rsi:.2f} | "
                    f"{'阳线涨' if k_change > 0 else '阴线跌'}{k_body_points:.1f}点 | {momentum_ratio:.2f}x量"
                )
                return side, "极度超卖" if side == PositionType.BULL else "极度超买", branch, desc

    if "momentum" in strategies and momentum_body_ok:
        required_momentum_ratio = _required_momentum_volume_multiplier(ts)
        vol_surge = volume > vol_ma * required_momentum_ratio
        if vol_surge and k_change > 0 and not get_momentum_filter_reasons("bull", rsi, None):
            return PositionType.BULL, MOMENTUM_ENTRY_MODE, "momentum", f"阳线涨{k_change:.1f}点 | {momentum_ratio:.1f}x量"
        if vol_surge and k_change < 0 and not get_momentum_filter_reasons("bear", rsi, None):
            return PositionType.BEAR, MOMENTUM_ENTRY_MODE, "momentum", f"阴线跌{abs(k_change):.1f}点 | {momentum_ratio:.1f}x量"

    if "rsi_divergence" in strategies:
        divergence_entry = _detect_rsi_divergence(d1, i, divergence_state)
        if divergence_entry is not None:
            return divergence_entry

    completed_kline_time = d1.index[i - 1].strftime("%Y-%m-%d %H:%M:%S")
    if "extreme" in strategies and completed_kline_time != last_completed_extreme_kline_time:
        completed = _completed_extreme_signal(prev, row, payload)
        if completed is not None:
            side, mode, branch, desc = completed
            return side, mode, branch, desc

    if "cum_trend" in strategies:
        return _detect_cum_trend(d1, i, payload, runtime, breadth)
    return None


def _run_day(
    d1_day: pd.DataFrame,
    d15_day: pd.DataFrame,
    trade_date: date,
    payload: BacktestRequest,
    runtime: _Runtime,
    breadth: pd.DataFrame,
) -> list[_Trade]:
    trades: list[_Trade] = []
    position = PositionType.NONE
    entry = 0.0
    entry_time: pd.Timestamp | None = None
    entry_mode = ""
    entry_branch = ""
    entry_desc = ""
    last_tp_side = PositionType.NONE
    last_tp_time: pd.Timestamp | None = None
    last_completed_extreme_kline_time = ""
    last_extreme_stop_mode = ""
    last_extreme_stop_position = PositionType.NONE
    last_extreme_stop_time: pd.Timestamp | None = None
    divergence_state = _RsiDivergenceState.create()
    start_idx = max(VOL_MA_PERIOD, payload.rsi_length) + 1
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
            if diff >= payload.take_profit_points:
                exit_reason = "W"
            elif diff <= -payload.stop_loss_points:
                exit_reason = "L"
            elif _is_after_force_exit(ts):
                exit_reason = "W" if diff >= 0 else "L"
            if exit_reason:
                minutes = (ts - entry_time).total_seconds() / 60 if entry_time else 0.0
                pnl_hkd = payload.fixed_win_hkd if exit_reason == "W" else payload.fixed_loss_hkd
                trades.append(_Trade(
                    trade_date=str(trade_date),
                    side=position.value,
                    mode=entry_mode,
                    branch=entry_branch,
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
                elif entry_mode in ("极度超卖", "极度超买", VERY_EXTREME_SHADOW_BULL_ENTRY_MODE, VERY_EXTREME_SHADOW_BEAR_ENTRY_MODE):
                    last_extreme_stop_mode = entry_mode
                    last_extreme_stop_position = position
                    last_extreme_stop_time = ts
                position = PositionType.NONE
                entry = 0.0
                entry_time = None
                entry_mode = ""
                entry_branch = ""
                entry_desc = ""
            continue
        if _is_open_filter(ts) or _is_after_entry_cutoff(ts):
            continue
        detected = _detect_entry(
            d1_day,
            d15_day,
            i,
            payload,
            runtime,
            breadth,
            last_completed_extreme_kline_time,
            divergence_state,
        )
        if detected is None:
            continue
        side, mode, branch, desc = detected
        if branch in ("b3_completed_k", "b4_shadow_reversal"):
            last_completed_extreme_kline_time = d1_day.index[i - 1].strftime("%Y-%m-%d %H:%M:%S")
        if last_tp_side == side and last_tp_time is not None:
            if (ts - last_tp_time).total_seconds() < SAME_SIDE_TAKE_PROFIT_COOLDOWN_SECONDS:
                continue
        if last_extreme_stop_time is not None:
            elapsed = (ts - last_extreme_stop_time).total_seconds()
            if elapsed < EXTREME_STOP_REVERSAL_GUARD_SECONDS:
                if side == PositionType.BULL and last_extreme_stop_mode == "极度超买" and last_extreme_stop_position == PositionType.BEAR:
                    continue
                if side == PositionType.BEAR and last_extreme_stop_mode == "极度超卖" and last_extreme_stop_position == PositionType.BULL:
                    continue
        position = side
        entry = price
        entry_time = ts
        entry_mode = mode
        entry_branch = branch
        entry_desc = desc
    return trades


def _summary(trades: list[_Trade]) -> BacktestSummary:
    wins = sum(1 for trade in trades if trade.result == "W")
    losses = sum(1 for trade in trades if trade.result == "L")
    total = wins + losses
    pnl = sum(trade.pnl_hkd for trade in trades)
    return BacktestSummary(
        trades=total,
        wins=wins,
        losses=losses,
        win_rate=round(wins / total * 100, 1) if total else 0.0,
        pnl_hkd=round(pnl, 2),
    )


def _breakdown(trades: list[_Trade], key_fn, label_fn) -> list[BacktestBreakdownRow]:
    grouped: dict[str, list[_Trade]] = {}
    for trade in trades:
        key = key_fn(trade)
        grouped.setdefault(key, []).append(trade)
    rows = []
    for key in sorted(grouped.keys()):
        summary = _summary(grouped[key])
        rows.append(BacktestBreakdownRow(
            key=key,
            label=label_fn(key),
            trades=summary.trades,
            wins=summary.wins,
            losses=summary.losses,
            win_rate=summary.win_rate,
            pnl_hkd=summary.pnl_hkd,
        ))
    return rows


def run_backtest(payload: BacktestRequest) -> BacktestResult:
    start, end = resolve_backtest_range(payload)
    if payload.rsi_length not in (6, 8, 10, 12, 14):
        raise ValueError("RSI 周期只支持 6/8/10/12/14")
    if not payload.selection.strategies:
        raise ValueError("至少选择一个策略")
    if "extreme" in payload.selection.strategies and not payload.selection.extreme_branches:
        raise ValueError("选择极度策略时必须至少选择一个极度分支")

    warnings: set[str] = {"order_book_not_replayed"}
    runtime = _Runtime(request=payload, warnings=warnings)
    ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
    try:
        d1 = _fetch_history(ctx, KLType.K_1M, start, end)
        d15 = _fetch_history(ctx, KLType.K_15M, start, end)
    finally:
        ctx.close()
    if d1.empty or d15.empty:
        raise RuntimeError("没有拉到足够 Futu K 线")
    d1 = _prepare_indicators(d1, payload.rsi_length)
    d15 = _prepare_indicators(d15, payload.rsi_length)
    breadth = _load_market_breadth(start, end, warnings)
    if payload.cum_trend_mode == "kline_proxy":
        warnings.add("cum_trend_kline_proxy")
    elif "cum_trend" in payload.selection.strategies and breadth.empty:
        if payload.cum_trend_mode == "strict_breadth":
            warnings.add("breadth_missing_skipped")
        else:
            warnings.add("breadth_fallback_to_proxy")

    all_trades: list[_Trade] = []
    dates = sorted(d for d in set(d1.index.date) if start <= d <= end)
    for day in dates:
        d1_day = d1[d1.index.date == day]
        d15_day = d15[d15.index.date == day]
        if len(d1_day) < 30 or d15_day.empty:
            continue
        all_trades.extend(_run_day(d1_day, d15_day, day, payload, runtime, breadth))
    warnings.update(runtime.warnings)
    if runtime.breadth_missing_count:
        warnings.add(f"breadth_missing_count:{runtime.breadth_missing_count}")
    if runtime.breadth_used_count:
        warnings.add(f"breadth_used_count:{runtime.breadth_used_count}")

    data_start = d1.index.min().strftime("%Y-%m-%d %H:%M:%S")
    data_end = d1.index.max().strftime("%Y-%m-%d %H:%M:%S")
    monthly = _breakdown(
        all_trades,
        lambda trade: trade.trade_date[:7],
        lambda key: key,
    )
    daily = _breakdown(
        all_trades,
        lambda trade: trade.trade_date,
        lambda key: key,
    )
    strategy_breakdown = _breakdown(
        all_trades,
        lambda trade: trade.mode,
        lambda key: STRATEGY_LABELS.get(key, key),
    )
    extreme_branch_breakdown = _breakdown(
        [trade for trade in all_trades if trade.branch.startswith("b")],
        lambda trade: trade.branch,
        lambda key: EXTREME_BRANCH_LABELS.get(key, key),
    )
    return BacktestResult(
        requested_start=start.isoformat(),
        requested_end=end.isoformat(),
        data_start=data_start,
        data_end=data_end,
        summary=_summary(all_trades),
        monthly=monthly,
        daily=daily,
        strategy_breakdown=strategy_breakdown,
        extreme_branch_breakdown=extreme_branch_breakdown,
        trades=[BacktestTrade(**trade.__dict__) for trade in all_trades],
        warnings=sorted(warnings),
    )
