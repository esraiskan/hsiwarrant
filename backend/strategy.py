"""
核心策略引擎 - 连接富途 OpenD 实盘版
双频率架构：
  - 快速轮询 (3秒): 拉取实时价格，推送状态
  - 策略研判 (每轮): 1M K线做主信号(RSI/成交额/VWAP/形态) + 15M 跨周期确认
"""
import asyncio
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from config import (
    SYMBOL, ER_RATIO, SHARE_COUNT, TARGET_PNL, STOP_POINTS, EXTREME_STOP_PNL,
    BULL_WARRANT_CODE, BEAR_WARRANT_CODE,
    RSI_LENGTH, RSI_OVERSOLD, RSI_OVERBOUGHT, POLL_INTERVAL, ENTRY_ORDER_WAIT_SECONDS,
    ENTRY_CUTOFF_TIME, EXTREME_RSI_STOP_VETO_ENABLED, EXTREME_RSI_STOP_HARD_TICKS,
    EXTREME_RSI_STOP_REARM_TICKS,
)
from models import (
    PositionType, TradeSignal, TradeRecord, StrategyState, KlineData, MarketRegime,
)
from futu_data import FutuDataSource
from futu_trader import FutuTrader
from market_regime import classify_market_regime
from runtime_config_store import load_runtime_config, save_runtime_config
from strategy_state_store import load_strategy_state, save_strategy_state
from trade_log_store import append_trade_log, load_trade_log
from momentum_filter import get_momentum_filter_reasons
from trend_filter import (
    CUM_TREND_BOUNDARY_POINTS,
    get_cum_trend_boundary_filter_reasons,
    get_cum_trend_filter_reasons,
)


def _is_nan(value) -> bool:
    try:
        return math.isnan(float(value))
    except (ValueError, TypeError):
        return True


def normalize_warrant_code(value: str | None) -> str:
    code = (value or "").strip().upper()
    if not code:
        return ""
    if code.startswith("HK."):
        suffix = code[3:]
        return f"HK.{suffix.zfill(5)}" if suffix.isdigit() else code
    return f"HK.{code.zfill(5)}" if code.isdigit() else code


def _price_decimals(tick_size: float) -> int:
    text = f"{tick_size:.10f}".rstrip("0").rstrip(".")
    return len(text.split(".")[1]) if "." in text else 0


def _round_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return float(price)
    decimals = _price_decimals(tick_size)
    return round(round(float(price) / tick_size) * tick_size, decimals)


def _is_filled_all(status: str) -> bool:
    return str(status or "").upper().endswith("FILLED_ALL")


def _order_status_name(status: str) -> str:
    return str(status or "").upper().split(".")[-1]


VERY_EXTREME_SHADOW_BULL_ENTRY_MODE = "非常极端下影反抽"
VERY_EXTREME_SHADOW_BEAR_ENTRY_MODE = "非常极端上影回落"
EXTREME_ENTRY_MODES = {
    "极度超卖",
    "极度超买",
    VERY_EXTREME_SHADOW_BULL_ENTRY_MODE,
    VERY_EXTREME_SHADOW_BEAR_ENTRY_MODE,
}
DEFAULT_ENABLED_STRATEGIES = ["normal", "extreme", "momentum", "cum_trend"]
DEFAULT_ENABLED_EXTREME_BRANCHES = [
    "b1_volume_extreme",
    "b2_very_extreme_pullback",
    "b3_completed_k",
    "b4_shadow_reversal",
]
VALID_ENABLED_STRATEGIES = set(DEFAULT_ENABLED_STRATEGIES) | {"rsi_divergence"}
VALID_ENABLED_EXTREME_BRANCHES = set(DEFAULT_ENABLED_EXTREME_BRANCHES)
MOMENTUM_ENTRY_MODE = "放量动能"
CUM_TREND_ENTRY_MODE = "累积趋势"
RSI_DIVERGENCE_ENTRY_MODE = "RSI背离"
CUM_TREND_RSI_BUFFER = 3.0
CUM_TREND_PENDING_ADVERSE_MOVE_POINTS = 5.0
CUM_TREND_VWAP_CONFIRM_BARS = 2
CUM_TREND_SAME_SIDE_STOP_COOLDOWN_SECONDS = 180
CUM_TREND_BULL_OVERHEAT_RSI = 70.0
CUM_TREND_BULL_PULLBACK_RSI = 68.0
CUM_TREND_BEAR_OVERSOLD_RSI = 30.0
CUM_TREND_BEAR_REBOUND_RSI = 32.0
MOMENTUM_PENDING_ADVERSE_MOVE_POINTS = 5.0
EXTREME_VOLUME_SURGE_MULTIPLIER = 1.3
VERY_EXTREME_RSI_OVERBOUGHT = 85
VERY_EXTREME_RSI_OVERSOLD = 16
VERY_EXTREME_VOLUME_SURGE_MULTIPLIER = 1.25
VERY_EXTREME_AVG_VOLUME_MULTIPLIER = 1.0
VERY_EXTREME_PULLBACK_POINTS = 3.0
VERY_EXTREME_SHADOW_BULL_RSI = 10.0
VERY_EXTREME_SHADOW_BEAR_RSI = 90.0
VERY_EXTREME_SHADOW_MIN_REBOUND_POINTS = 8.0
VERY_EXTREME_SHADOW_MIN_LOWER_SHADOW_POINTS = 6.0
VERY_EXTREME_SHADOW_MIN_PULLBACK_POINTS = 8.0
VERY_EXTREME_SHADOW_MIN_UPPER_SHADOW_POINTS = 6.0
VERY_EXTREME_SHADOW_MIN_VOLUME_RATIO = 1.0
VERY_EXTREME_SHADOW_MAX_ENTRY_CHASE_POINTS = 4.0
MOMENTUM_VOLUME_SURGE_MULTIPLIER = 1.5
MOMENTUM_LATE_ENTRY_TIME = "15:30"
MOMENTUM_LATE_VOLUME_SURGE_MULTIPLIER = 1.8
MOMENTUM_MIN_K_BODY_POINTS = 5.0
MOMENTUM_MAX_K_BODY_POINTS = 30.0
MOMENTUM_PENDING_VWAP_SLOPE_BUFFER = 0.05
MOMENTUM_BOOK_MIN_BUY_RATIO = 0.50
MOMENTUM_BOOK_MAX_SPREAD_TICKS = 2
MOMENTUM_ENTRY_FAIL_FAST_SECONDS = 60
MOMENTUM_ENTRY_FAIL_FAST_POINTS = 8.0
MOMENTUM_ENTRY_FAIL_FAST_RSI = 50.0
EXTREME_COMPLETED_K_MAX_ADVERSE_MOVE_POINTS = 8.0
EXTREME_COMPLETED_K_MAX_FAVORABLE_MOVE_POINTS = 12.0
EXTREME_COMPLETED_K_RSI_BUFFER = 5.0
EXTREME_ENTRY_FIRST_WAIT_SECONDS = 10
EXTREME_ENTRY_CHASE_WAIT_SECONDS = 12
EXTREME_ENTRY_BOOK_BUY_RATIO = 0.55
EXTREME_ENTRY_FIRST_ASK_BUY_RATIO = 0.80
EXTREME_ENTRY_FIRST_ASK_MIN_FILL_RATIO = 1.0
EXTREME_ENTRY_ASK_THIN_RATIO = 0.35
EXTREME_ENTRY_CONFIRM_POINTS = 5.0
OTHER_ENTRY_DIRECT_SELL1_BUY_RATIO = 0.90
TERMINAL_UNFILLED_EXIT_STATUSES = {
    "CANCELLED_ALL",
    "CANCELLED_PART",
    "FAILED",
    "SUBMIT_FAILED",
    "DELETED",
    "DISABLED",
}
EXTREME_STOP_REVERSAL_GUARD_SECONDS = 5 * 60
SAME_SIDE_TAKE_PROFIT_COOLDOWN_SECONDS = 3 * 60
FORCE_EXIT_TIME = "15:55"
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


@dataclass
class RsiDivergencePoint:
    time: object
    price: float
    rsi: float


def _extreme_signal_side(
    rsi: float,
    momentum_ratio: float,
    current_price: float,
    k_high: float,
    k_low: float,
    rsi_oversold: float,
    rsi_overbought: float,
) -> tuple[PositionType, bool, str]:
    if rsi < rsi_oversold and momentum_ratio > EXTREME_VOLUME_SURGE_MULTIPLIER:
        return PositionType.BULL, False, "放量动能触发"
    if rsi > rsi_overbought and momentum_ratio > EXTREME_VOLUME_SURGE_MULTIPLIER:
        return PositionType.BEAR, False, "放量动能触发"

    if momentum_ratio <= VERY_EXTREME_AVG_VOLUME_MULTIPLIER:
        return PositionType.NONE, False, ""
    if (
        rsi <= VERY_EXTREME_RSI_OVERSOLD
        and current_price >= k_low + VERY_EXTREME_PULLBACK_POINTS
    ):
        return PositionType.BULL, True, "非常极端RSI均量反抽"
    if (
        rsi >= VERY_EXTREME_RSI_OVERBOUGHT
        and current_price <= k_high - VERY_EXTREME_PULLBACK_POINTS
    ):
        return PositionType.BEAR, True, "非常极端RSI均量回落"
    return PositionType.NONE, False, ""


class HSIStrategyEngine:

    def __init__(self):
        self.data_source = FutuDataSource()
        self.trader = FutuTrader()

        self.position = PositionType.NONE
        self.entry_price = 0.0
        self.current_price = 0.0
        self.total_pnl_hkd = 0.0
        self.trade_count = 0
        self.win_count = 0
        self.loss_count = 0
        self.latest_breadth: dict | None = None
        self.is_running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._strategy_task: Optional[asyncio.Task] = None

        self.current_warrant_code: str = ""
        self.pending_entry_side: PositionType = PositionType.NONE
        self.pending_buy_order_id: str = ""
        self.exit_order_id: str = ""
        self.entry_order_time: datetime | None = None
        self.entry_chase_count = 0
        self.warrant_entry_price = 0.0
        self.warrant_exit_price = 0.0
        self.warrant_tick_size = 0.0
        self.warrant_qty = 0.0
        self.stop_loss_order_sent = False
        self.entry_mode = ""
        self.momentum_entry_trigger_price = 0.0
        self.entry_fill_time: datetime | None = None
        self.momentum_fail_fast_sent = False
        self.last_extreme_stop_mode = ""
        self.last_extreme_stop_position = PositionType.NONE
        self.last_extreme_stop_time: datetime | None = None
        self.last_reversal_guard_log_key = ""
        self.last_take_profit_position = PositionType.NONE
        self.last_take_profit_time: datetime | None = None
        self.last_take_profit_cooldown_log_key = ""
        self.last_completed_extreme_kline_time = ""
        self.last_cum_trend_trigger_key = ""
        self.last_cum_trend_stop_position = PositionType.NONE
        self.last_cum_trend_stop_time: datetime | None = None
        self.rsi_divergence_bull_lows: list[RsiDivergencePoint] = []
        self.rsi_divergence_bear_highs: list[RsiDivergencePoint] = []
        self.rsi_divergence_used_bull_keys: set[str] = set()
        self.rsi_divergence_used_bear_keys: set[str] = set()
        self.rsi_divergence_used_bull_c_times: set[str] = set()
        self.rsi_divergence_used_bear_c_times: set[str] = set()
        self.rsi_divergence_last_bull_entry_time = None
        self.rsi_divergence_last_bear_entry_time = None
        self.rsi_divergence_last_processed_pivot_time = ""
        self.rsi_divergence_day = ""
        self.extreme_rsi_stop_veto_active = False
        self.extreme_rsi_stop_veto_price = 0.0
        self.extreme_rsi_stop_hard_price = 0.0
        self.extreme_rsi_stop_rearm_price = 0.0
        self.extreme_rsi_stop_veto_count = 0
        self.extreme_rsi_stop_veto_rsi = 0.0
        self.last_stop_loss_failure_log_key = ""

        # 回调
        self.on_kline_update: Optional[Callable] = None
        self.on_kline_batch: Optional[Callable] = None
        self.on_trade_signal: Optional[Callable] = None
        self.on_state_update: Optional[Callable] = None
        self.on_market_regime_update: Optional[Callable] = None

        # 历史记录
        self.trade_history: list[TradeRecord] = load_trade_log()
        self.kline_history_1m: list[KlineData] = []

        # 最新指标快照
        self._last_rsi: float | None = None
        self._last_vwap: float | None = None
        self._last_vwap_slope: float | None = None
        self._last_vol_ma: float | None = None
        self.market_regime: MarketRegime | None = None
        self._last_market_regime_kline_time = ""

        # 运行时可调参数
        self.er_ratio = ER_RATIO
        self.share_count = SHARE_COUNT
        self.target_pnl = TARGET_PNL
        self.extreme_stop_pnl = EXTREME_STOP_PNL
        self.stop_points = STOP_POINTS
        self.rsi_length = RSI_LENGTH
        self.rsi_oversold = RSI_OVERSOLD
        self.rsi_overbought = RSI_OVERBOUGHT
        self.poll_interval = POLL_INTERVAL
        self.entry_order_wait_seconds = ENTRY_ORDER_WAIT_SECONDS
        self.entry_cutoff_time = ENTRY_CUTOFF_TIME
        self.only_extreme_entries = False
        self.enabled_strategies = DEFAULT_ENABLED_STRATEGIES.copy()
        self.enabled_extreme_branches = DEFAULT_ENABLED_EXTREME_BRANCHES.copy()
        self.extreme_rsi_stop_veto_enabled = EXTREME_RSI_STOP_VETO_ENABLED
        self.extreme_rsi_stop_hard_ticks = EXTREME_RSI_STOP_HARD_TICKS
        self.extreme_rsi_stop_rearm_ticks = EXTREME_RSI_STOP_REARM_TICKS
        self.bull_warrant_code = BULL_WARRANT_CODE
        self.bear_warrant_code = BEAR_WARRANT_CODE
        self._load_runtime_config()
        self._load_runtime_state()

    def get_state(self, sync_exit: bool = True) -> StrategyState:
        if sync_exit:
            self.sync_exit_order_if_filled()
        self.trade_count = max(self.trade_count, self.win_count + self.loss_count)
        unrealized_pnl = 0.0
        unrealized_pnl_hkd = 0.0
        if self.position == PositionType.BULL:
            unrealized_pnl = self.current_price - self.entry_price
            unrealized_pnl_hkd = (unrealized_pnl / self.er_ratio) * self.share_count
        elif self.position == PositionType.BEAR:
            unrealized_pnl = self.entry_price - self.current_price
            unrealized_pnl_hkd = (unrealized_pnl / self.er_ratio) * self.share_count
        breadth_raise_count = 0
        breadth_fall_count = 0
        breadth_equal_count = 0
        breadth_ratio = None
        breadth_amplitude = 0.0
        breadth_time = ""
        if self.latest_breadth:
            breadth_raise_count = int(self.latest_breadth.get("raise_count", 0))
            breadth_fall_count = int(self.latest_breadth.get("fall_count", 0))
            breadth_equal_count = int(self.latest_breadth.get("equal_count", 0))
            if breadth_raise_count or breadth_fall_count:
                breadth_ratio = round(breadth_raise_count / max(breadth_fall_count, 1), 2)
            breadth_amplitude = round(float(self.latest_breadth.get("amplitude", 0) or 0), 2)
            breadth_time = str(self.latest_breadth.get("time", ""))
        return StrategyState(
            position=self.position,
            entry_price=self.entry_price,
            current_price=self.current_price,
            unrealized_pnl=round(unrealized_pnl, 2),
            unrealized_pnl_hkd=round(unrealized_pnl_hkd, 2),
            total_pnl_hkd=round(self.total_pnl_hkd, 2),
            breadth_raise_count=breadth_raise_count,
            breadth_fall_count=breadth_fall_count,
            breadth_equal_count=breadth_equal_count,
            breadth_ratio=breadth_ratio,
            breadth_amplitude=breadth_amplitude,
            breadth_time=breadth_time,
            trade_count=self.trade_count,
            win_count=self.win_count,
            loss_count=self.loss_count,
            is_running=self.is_running,
        )

    def update_config(self, **kwargs):
        for key, value in kwargs.items():
            if value is not None and hasattr(self, key):
                if key in ("bull_warrant_code", "bear_warrant_code"):
                    setattr(self, key, normalize_warrant_code(value))
                elif key == "rsi_length":
                    rsi_length = int(value)
                    if rsi_length not in (6, 8, 10, 12, 14):
                        raise ValueError("rsi_length 只支持 6/8/10/12/14")
                    setattr(self, key, rsi_length)
                elif key == "enabled_strategies":
                    self.enabled_strategies = self._normalize_enabled_strategies(value)
                    self.only_extreme_entries = False
                elif key == "enabled_extreme_branches":
                    self.enabled_extreme_branches = self._normalize_enabled_extreme_branches(value)
                else:
                    setattr(self, key, value)
        if "target_pnl" in kwargs or "er_ratio" in kwargs or "share_count" in kwargs:
            self.stop_points = self._stop_points_for_pnl(self.target_pnl)
        self._save_runtime_config()

    def _normalize_enabled_strategies(self, value) -> list[str]:
        if value is None:
            return DEFAULT_ENABLED_STRATEGIES.copy()
        if not isinstance(value, list):
            raise ValueError("enabled_strategies 必须是列表")
        out: list[str] = []
        for item in value:
            text = str(item)
            if text not in VALID_ENABLED_STRATEGIES:
                raise ValueError(f"未知策略: {text}")
            if text not in out:
                out.append(text)
        if not out:
            raise ValueError("至少选择一个实盘策略")
        return out

    def _normalize_enabled_extreme_branches(self, value) -> list[str]:
        if value is None:
            return DEFAULT_ENABLED_EXTREME_BRANCHES.copy()
        if not isinstance(value, list):
            raise ValueError("enabled_extreme_branches 必须是列表")
        out: list[str] = []
        for item in value:
            text = str(item)
            if text not in VALID_ENABLED_EXTREME_BRANCHES:
                raise ValueError(f"未知极度分支: {text}")
            if text not in out:
                out.append(text)
        return out

    def _strategy_enabled(self, key: str) -> bool:
        return key in set(self.enabled_strategies)

    def _extreme_branch_enabled(self, key: str) -> bool:
        return key in set(self.enabled_extreme_branches)

    def _mode_allowed_by_strategy_selection(self, mode: str) -> bool:
        if mode in {"普通超卖", "普通超买"}:
            return self._strategy_enabled("normal")
        if mode in EXTREME_ENTRY_MODES:
            return self._strategy_enabled("extreme")
        if mode == MOMENTUM_ENTRY_MODE:
            return self._strategy_enabled("momentum")
        if mode == CUM_TREND_ENTRY_MODE:
            return self._strategy_enabled("cum_trend")
        if mode == RSI_DIVERGENCE_ENTRY_MODE:
            return self._strategy_enabled("rsi_divergence")
        return True

    def _completed_extreme_branch_from_message(self, message: str) -> str:
        if "非常极端下影反抽" in message or "非常极端上影回落" in message:
            return "b4_shadow_reversal"
        return "b3_completed_k"

    async def _emit_strategy_disabled_skip(
        self,
        side: PositionType,
        hsi_price: float,
        rsi: float,
        current_time: str,
        mode: str,
        reason: str,
    ):
        label = "牛证" if side == PositionType.BULL else "熊证"
        await self._emit_trade_record(TradeRecord(
            time=current_time,
            signal=TradeSignal.ENTRY_PENDING,
            price=hsi_price,
            rsi=round(rsi, 2),
            position=side,
            message=f"【{label}·{mode}】跳过: {reason}",
        ))

    def _stop_points_for_pnl(self, pnl_hkd: float) -> float:
        if self.share_count <= 0:
            return 0.0
        return (float(pnl_hkd) * self.er_ratio) / self.share_count

    def _extreme_stop_points(self) -> float:
        return self._stop_points_for_pnl(self.extreme_stop_pnl)

    def _active_stop_pnl(self) -> float:
        return self.extreme_stop_pnl if self.entry_mode in EXTREME_ENTRY_MODES else self.target_pnl

    def _active_stop_points(self) -> float:
        return self._stop_points_for_pnl(self._active_stop_pnl())

    def _runtime_config_payload(self) -> dict:
        return {
            "er_ratio": self.er_ratio,
            "share_count": self.share_count,
            "target_pnl": self.target_pnl,
            "extreme_stop_pnl": self.extreme_stop_pnl,
            "bull_warrant_code": self.bull_warrant_code,
            "bear_warrant_code": self.bear_warrant_code,
            "rsi_length": self.rsi_length,
            "rsi_oversold": self.rsi_oversold,
            "rsi_overbought": self.rsi_overbought,
            "poll_interval": self.poll_interval,
            "entry_order_wait_seconds": self.entry_order_wait_seconds,
            "only_extreme_entries": self.only_extreme_entries,
            "enabled_strategies": self.enabled_strategies,
            "enabled_extreme_branches": self.enabled_extreme_branches,
            "extreme_rsi_stop_veto_enabled": self.extreme_rsi_stop_veto_enabled,
            "extreme_rsi_stop_hard_ticks": self.extreme_rsi_stop_hard_ticks,
            "extreme_rsi_stop_rearm_ticks": self.extreme_rsi_stop_rearm_ticks,
        }

    def _save_runtime_config(self):
        save_runtime_config(self._runtime_config_payload())

    def _load_runtime_config(self):
        data = load_runtime_config()
        if not data:
            self.stop_points = self._stop_points_for_pnl(self.target_pnl)
            return
        int_fields = {
            "er_ratio",
            "share_count",
            "target_pnl",
            "extreme_stop_pnl",
            "rsi_length",
            "rsi_oversold",
            "rsi_overbought",
            "poll_interval",
            "entry_order_wait_seconds",
            "extreme_rsi_stop_hard_ticks",
            "extreme_rsi_stop_rearm_ticks",
        }
        try:
            for key in int_fields:
                if key not in data:
                    continue
                value = int(data[key])
                if value <= 0:
                    raise ValueError(f"{key} 必须大于 0")
                if key == "rsi_length" and value not in (6, 8, 10, 12, 14):
                    raise ValueError("rsi_length 只支持 6/8/10/12/14")
                setattr(self, key, value)
            for key in ("bull_warrant_code", "bear_warrant_code"):
                if key in data:
                    setattr(self, key, normalize_warrant_code(data.get(key)))
            if "only_extreme_entries" in data:
                self.only_extreme_entries = bool(data["only_extreme_entries"])
                if self.only_extreme_entries and "enabled_strategies" not in data:
                    self.enabled_strategies = ["extreme"]
            if "enabled_strategies" in data:
                self.enabled_strategies = self._normalize_enabled_strategies(data["enabled_strategies"])
                self.only_extreme_entries = False
            if "enabled_extreme_branches" in data:
                self.enabled_extreme_branches = self._normalize_enabled_extreme_branches(data["enabled_extreme_branches"])
            if "extreme_rsi_stop_veto_enabled" in data:
                self.extreme_rsi_stop_veto_enabled = bool(data["extreme_rsi_stop_veto_enabled"])
            self.stop_points = self._stop_points_for_pnl(self.target_pnl)
        except Exception as e:
            print(f"[RuntimeConfig] 配置内容无效，使用默认配置: {e}")
            self.er_ratio = ER_RATIO
            self.share_count = SHARE_COUNT
            self.target_pnl = TARGET_PNL
            self.extreme_stop_pnl = EXTREME_STOP_PNL
            self.stop_points = self._stop_points_for_pnl(self.target_pnl)
            self.rsi_length = RSI_LENGTH
            self.rsi_oversold = RSI_OVERSOLD
            self.rsi_overbought = RSI_OVERBOUGHT
            self.poll_interval = POLL_INTERVAL
            self.entry_order_wait_seconds = ENTRY_ORDER_WAIT_SECONDS
            self.only_extreme_entries = False
            self.enabled_strategies = DEFAULT_ENABLED_STRATEGIES.copy()
            self.enabled_extreme_branches = DEFAULT_ENABLED_EXTREME_BRANCHES.copy()
            self.extreme_rsi_stop_veto_enabled = EXTREME_RSI_STOP_VETO_ENABLED
            self.extreme_rsi_stop_hard_ticks = EXTREME_RSI_STOP_HARD_TICKS
            self.extreme_rsi_stop_rearm_ticks = EXTREME_RSI_STOP_REARM_TICKS
            self.bull_warrant_code = BULL_WARRANT_CODE
            self.bear_warrant_code = BEAR_WARRANT_CODE

    async def _emit_trade_record(self, record: TradeRecord):
        original_time = record.time
        record.time = self._current_time_for_trade_record()
        if (
            original_time
            and original_time != record.time
            and record.signal not in {TradeSignal.TAKE_PROFIT, TradeSignal.STOP_LOSS}
            and "信号K线:" not in record.message
        ):
            record.message = f"{record.message} | 信号K线:{original_time}"
        append_trade_log(record)
        self.trade_history.append(record)
        print(f"  >>> {record.message}")
        if self.on_trade_signal:
            await self.on_trade_signal(record)

    def _runtime_state_payload(self) -> dict:
        return {
            "position": self.position.value,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "total_pnl_hkd": self.total_pnl_hkd,
            "trade_count": self.trade_count,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "current_warrant_code": self.current_warrant_code,
            "pending_entry_side": self.pending_entry_side.value,
            "pending_buy_order_id": self.pending_buy_order_id,
            "exit_order_id": self.exit_order_id,
            "entry_order_time": self.entry_order_time.isoformat() if self.entry_order_time else "",
            "entry_chase_count": self.entry_chase_count,
            "warrant_entry_price": self.warrant_entry_price,
            "warrant_exit_price": self.warrant_exit_price,
            "warrant_tick_size": self.warrant_tick_size,
            "warrant_qty": self.warrant_qty,
            "stop_loss_order_sent": self.stop_loss_order_sent,
            "entry_mode": self.entry_mode,
            "momentum_entry_trigger_price": self.momentum_entry_trigger_price,
            "entry_fill_time": self.entry_fill_time.isoformat() if self.entry_fill_time else "",
            "momentum_fail_fast_sent": self.momentum_fail_fast_sent,
            "last_extreme_stop_mode": self.last_extreme_stop_mode,
            "last_extreme_stop_position": self.last_extreme_stop_position.value,
            "last_extreme_stop_time": self.last_extreme_stop_time.isoformat() if self.last_extreme_stop_time else "",
            "last_reversal_guard_log_key": self.last_reversal_guard_log_key,
            "last_take_profit_position": self.last_take_profit_position.value,
            "last_take_profit_time": self.last_take_profit_time.isoformat() if self.last_take_profit_time else "",
            "last_completed_extreme_kline_time": self.last_completed_extreme_kline_time,
            "last_cum_trend_trigger_key": self.last_cum_trend_trigger_key,
            "last_cum_trend_stop_position": self.last_cum_trend_stop_position.value,
            "last_cum_trend_stop_time": self.last_cum_trend_stop_time.isoformat() if self.last_cum_trend_stop_time else "",
            "extreme_rsi_stop_veto_active": self.extreme_rsi_stop_veto_active,
            "extreme_rsi_stop_veto_price": self.extreme_rsi_stop_veto_price,
            "extreme_rsi_stop_hard_price": self.extreme_rsi_stop_hard_price,
            "extreme_rsi_stop_rearm_price": self.extreme_rsi_stop_rearm_price,
            "extreme_rsi_stop_veto_count": self.extreme_rsi_stop_veto_count,
            "extreme_rsi_stop_veto_rsi": self.extreme_rsi_stop_veto_rsi,
        }

    def _save_runtime_state(self):
        save_strategy_state(self._runtime_state_payload())

    def _load_runtime_state(self):
        data = load_strategy_state()
        if not data:
            self._restore_open_position_from_trade_history()
            return
        try:
            self.position = PositionType(data.get("position", PositionType.NONE.value))
            self.entry_price = float(data.get("entry_price", 0.0))
            self.current_price = float(data.get("current_price", 0.0))
            self.total_pnl_hkd = float(data.get("total_pnl_hkd", 0.0))
            self.trade_count = int(data.get("trade_count", 0))
            self.win_count = int(data.get("win_count", 0))
            self.loss_count = int(data.get("loss_count", 0))
            self.current_warrant_code = str(data.get("current_warrant_code", ""))
            self.pending_entry_side = PositionType(data.get("pending_entry_side", PositionType.NONE.value))
            self.pending_buy_order_id = str(data.get("pending_buy_order_id", ""))
            self.exit_order_id = str(data.get("exit_order_id", ""))
            entry_order_time = data.get("entry_order_time")
            self.entry_order_time = datetime.fromisoformat(entry_order_time) if entry_order_time else None
            self.entry_chase_count = int(data.get("entry_chase_count", 0))
            self.warrant_entry_price = float(data.get("warrant_entry_price", 0.0))
            self.warrant_exit_price = float(data.get("warrant_exit_price", 0.0))
            self.warrant_tick_size = float(data.get("warrant_tick_size", 0.0))
            self.warrant_qty = float(data.get("warrant_qty", 0.0))
            self.stop_loss_order_sent = bool(data.get("stop_loss_order_sent", False))
            self.entry_mode = str(data.get("entry_mode", ""))
            self.momentum_entry_trigger_price = float(data.get("momentum_entry_trigger_price", 0.0))
            entry_fill_time = data.get("entry_fill_time")
            self.entry_fill_time = datetime.fromisoformat(entry_fill_time) if entry_fill_time else None
            self.momentum_fail_fast_sent = bool(data.get("momentum_fail_fast_sent", False))
            self.last_extreme_stop_mode = str(data.get("last_extreme_stop_mode", ""))
            self.last_extreme_stop_position = PositionType(
                data.get("last_extreme_stop_position", PositionType.NONE.value)
            )
            last_stop_time = data.get("last_extreme_stop_time")
            self.last_extreme_stop_time = datetime.fromisoformat(last_stop_time) if last_stop_time else None
            self.last_reversal_guard_log_key = str(data.get("last_reversal_guard_log_key", ""))
            self.last_take_profit_position = PositionType(
                data.get("last_take_profit_position", PositionType.NONE.value)
            )
            last_take_profit_time = data.get("last_take_profit_time")
            self.last_take_profit_time = (
                datetime.fromisoformat(last_take_profit_time) if last_take_profit_time else None
            )
            self.last_completed_extreme_kline_time = str(data.get("last_completed_extreme_kline_time", ""))
            self.last_cum_trend_trigger_key = str(data.get("last_cum_trend_trigger_key", ""))
            self.last_cum_trend_stop_position = PositionType(
                data.get("last_cum_trend_stop_position", PositionType.NONE.value)
            )
            last_cum_stop_time = data.get("last_cum_trend_stop_time")
            self.last_cum_trend_stop_time = (
                datetime.fromisoformat(last_cum_stop_time) if last_cum_stop_time else None
            )
            self.extreme_rsi_stop_veto_active = bool(data.get("extreme_rsi_stop_veto_active", False))
            self.extreme_rsi_stop_veto_price = float(data.get("extreme_rsi_stop_veto_price", 0.0))
            self.extreme_rsi_stop_hard_price = float(data.get("extreme_rsi_stop_hard_price", 0.0))
            self.extreme_rsi_stop_rearm_price = float(data.get("extreme_rsi_stop_rearm_price", 0.0))
            self.extreme_rsi_stop_veto_count = int(data.get("extreme_rsi_stop_veto_count", 0))
            self.extreme_rsi_stop_veto_rsi = float(data.get("extreme_rsi_stop_veto_rsi", 0.0))
            if self.position != PositionType.NONE and (
                not self.current_warrant_code or not self.exit_order_id or self.warrant_entry_price <= 0
            ):
                self._restore_open_position_from_trade_history()
            if self.pending_buy_order_id and self.entry_order_time:
                elapsed = (datetime.now() - self.entry_order_time).total_seconds()
                if elapsed >= self.entry_order_wait_seconds * 2:
                    print(
                        f"[StrategyState] 清理过期买入 pending: "
                        f"order_id={self.pending_buy_order_id}"
                    )
                    self._reset_order_state()
                    self._save_runtime_state()
        except Exception as e:
            print(f"[StrategyState] 状态内容无效，忽略: {e}")
            self._restore_open_position_from_trade_history()

    def _restore_open_position_from_trade_history(self):
        for record in reversed(self.trade_history):
            if record.signal == TradeSignal.TAKE_PROFIT or (
                record.signal == TradeSignal.STOP_LOSS and "卖出成交" in record.message
            ):
                return
            if record.signal in {TradeSignal.BUY_BULL, TradeSignal.BUY_BEAR} and "买入全数成交" in record.message:
                code_match = re.search(r"(HK\.\d+)", record.message)
                exit_match = re.search(r"已挂 \+2格卖出 @ ([0-9.]+) \| order_id:([^\s|]+)", record.message)
                entry_match = re.search(r"x([0-9.]+) @ ([0-9.]+)", record.message)
                mode_match = re.search(r"入[场場]模式[:：]\s*([^;|]+)", record.message)
                self.position = record.position
                self.entry_price = float(record.price)
                self.current_price = self.current_price or float(record.price)
                self.current_warrant_code = code_match.group(1) if code_match else self.current_warrant_code
                self.entry_mode = mode_match.group(1).strip() if mode_match else ""
                if exit_match:
                    self.warrant_exit_price = float(exit_match.group(1))
                    self.exit_order_id = exit_match.group(2)
                if entry_match:
                    self.warrant_qty = float(entry_match.group(1))
                    self.warrant_entry_price = float(entry_match.group(2))
                if self.warrant_entry_price > 0 and self.warrant_exit_price > self.warrant_entry_price:
                    self.warrant_tick_size = round((self.warrant_exit_price - self.warrant_entry_price) / 2, 6)
                if self.current_warrant_code and self.exit_order_id and self.warrant_entry_price > 0:
                    self._save_runtime_state()
                print(
                    f"[StrategyState] 已由 Trade Log 恢复未平仓显示状态: "
                    f"{self.position.value} @ {self.entry_price:.2f}"
                )
                return

    def _get_live_hsi_price(self, fallback_price: float) -> float:
        snapshot = self.data_source.get_realtime_price()
        if snapshot and snapshot.get("last_price"):
            return float(snapshot["last_price"])
        return float(fallback_price)

    def _current_time_for_trade_record(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _last_rsi_for_trade_record(self) -> float:
        return float(self._last_rsi) if self._last_rsi is not None else 0.0

    def _build_exit_fill_record(self, order: dict, hsi_price: float, rsi: float) -> TradeRecord:
        exit_avg = order["dealt_avg_price"] or self.warrant_exit_price
        dealt_qty = order["dealt_qty"]
        pnl_hkd = (exit_avg - self.warrant_entry_price) * dealt_qty
        diff = hsi_price - self.entry_price if self.position == PositionType.BULL else self.entry_price - hsi_price
        signal = TradeSignal.TAKE_PROFIT if pnl_hkd >= 0 else TradeSignal.STOP_LOSS
        prefix = "止盈卖出成交" if pnl_hkd >= 0 else "止损卖出成交"
        return TradeRecord(
            time=self._current_time_for_trade_record(),
            signal=signal,
            price=hsi_price,
            rsi=round(rsi, 2),
            position=self.position,
            pnl=round(diff, 2),
            pnl_hkd=round(pnl_hkd, 2),
            message=(
                f"{prefix}: {self.current_warrant_code} x{dealt_qty:.0f} "
                f"@ {exit_avg:.3f} | 成交时HSI:{hsi_price:.2f} | 证价PnL {pnl_hkd:.2f} HKD"
            ),
        )

    def _apply_exit_fill_to_state(self, order: dict):
        exit_avg = order["dealt_avg_price"] or self.warrant_exit_price
        pnl_hkd = (exit_avg - self.warrant_entry_price) * order["dealt_qty"]
        old_position = self.position
        old_entry_mode = self.entry_mode
        self.total_pnl_hkd += pnl_hkd
        if pnl_hkd >= 0:
            self.win_count += 1
            self.last_take_profit_position = old_position
            self.last_take_profit_time = datetime.now()
        else:
            self.loss_count += 1
        self.trade_count = max(self.trade_count, self.win_count + self.loss_count)
        self._mark_cum_trend_stop_if_needed(old_entry_mode, old_position, pnl_hkd)
        self._update_extreme_stop_reversal_guard(old_entry_mode, old_position, pnl_hkd)
        self.position = PositionType.NONE
        self.entry_price = 0.0
        self._reset_order_state()
        self._save_runtime_state()

    async def _finalize_exit_fill(self, order: dict, hsi_price: float, rsi: float):
        record = self._build_exit_fill_record(order, hsi_price, rsi)
        await self._emit_trade_record(record)
        self._apply_exit_fill_to_state(order)

    def sync_exit_order_if_filled(self) -> bool:
        if not self.exit_order_id or self.position == PositionType.NONE:
            return False
        order = self.trader.get_order(self.exit_order_id)
        if order is None or not _is_filled_all(order.get("order_status", "")):
            return False

        hsi_price = self._get_live_hsi_price(self.current_price or self.entry_price)
        record = self._build_exit_fill_record(order, hsi_price, self._last_rsi_for_trade_record())
        append_trade_log(record)
        self.trade_history.append(record)
        print(f"  >>> {record.message}")
        self._apply_exit_fill_to_state(order)
        return True

    def _reset_order_state(self):
        self.current_warrant_code = ""
        self.pending_entry_side = PositionType.NONE
        self.pending_buy_order_id = ""
        self.exit_order_id = ""
        self.entry_order_time = None
        self.entry_chase_count = 0
        self.warrant_entry_price = 0.0
        self.warrant_exit_price = 0.0
        self.warrant_tick_size = 0.0
        self.warrant_qty = 0.0
        self.stop_loss_order_sent = False
        self.entry_mode = ""
        self.momentum_entry_trigger_price = 0.0
        self.entry_fill_time = None
        self.momentum_fail_fast_sent = False
        self._reset_extreme_rsi_stop_veto()
        self.last_stop_loss_failure_log_key = ""

    def _reset_extreme_stop_reversal_guard(self):
        self.last_extreme_stop_mode = ""
        self.last_extreme_stop_position = PositionType.NONE
        self.last_extreme_stop_time = None
        self.last_reversal_guard_log_key = ""

    def _reset_extreme_rsi_stop_veto(self, reset_count: bool = True):
        self.extreme_rsi_stop_veto_active = False
        self.extreme_rsi_stop_veto_price = 0.0
        self.extreme_rsi_stop_hard_price = 0.0
        self.extreme_rsi_stop_rearm_price = 0.0
        self.extreme_rsi_stop_veto_rsi = 0.0
        if reset_count:
            self.extreme_rsi_stop_veto_count = 0

    def _same_side_take_profit_cooldown_remaining(self, side: PositionType) -> int:
        if (
            side == PositionType.NONE
            or self.last_take_profit_position != side
            or self.last_take_profit_time is None
        ):
            return 0

        elapsed = (datetime.now() - self.last_take_profit_time).total_seconds()
        remaining = SAME_SIDE_TAKE_PROFIT_COOLDOWN_SECONDS - elapsed
        return max(0, int(remaining))

    def _entry_wait_seconds_for_pending_order(self) -> int:
        if self.entry_mode in EXTREME_ENTRY_MODES:
            if self.entry_chase_count > 0:
                return EXTREME_ENTRY_CHASE_WAIT_SECONDS
            return EXTREME_ENTRY_FIRST_WAIT_SECONDS
        return self.entry_order_wait_seconds

    def _warrant_book_stats(self, snapshot: dict) -> dict:
        bid = float(snapshot.get("bid_price", 0.0) or 0.0)
        ask = float(snapshot.get("ask_price", 0.0) or 0.0)
        tick = float(snapshot.get("price_spread", 0.0) or 0.0)
        bid_volume = float(snapshot.get("bid_volume", 0.0) or 0.0)
        ask_volume = float(snapshot.get("ask_volume", 0.0) or 0.0)
        total_volume = bid_volume + ask_volume
        buy_ratio = bid_volume / total_volume if total_volume > 0 else 0.0
        spread_ticks = round((ask - bid) / tick) if bid > 0 and ask > 0 and tick > 0 else 0
        return {
            "bid": bid,
            "ask": ask,
            "tick": tick,
            "bid_volume": bid_volume,
            "ask_volume": ask_volume,
            "buy_ratio": buy_ratio,
            "spread_ticks": spread_ticks,
            "has_book_volume": bid_volume > 0 and ask_volume > 0,
        }

    def _format_entry_book(self, snapshot: dict) -> str:
        stats = self._warrant_book_stats(snapshot)
        ratio_text = f"{stats['buy_ratio'] * 100:.1f}%" if stats["has_book_volume"] else "N/A"
        return (
            f"bid:{stats['bid']:.3f} ask:{stats['ask']:.3f} "
            f"spread:{stats['spread_ticks']}格 "
            f"bid量:{stats['bid_volume']:.0f} ask量:{stats['ask_volume']:.0f} "
            f"買盤佔比:{ratio_text}"
        )

    def _momentum_book_block_reason(self, side: PositionType, snapshot: dict) -> str:
        stats = self._warrant_book_stats(snapshot)
        if stats["bid"] <= 0 or stats["ask"] <= 0 or stats["tick"] <= 0:
            return "買賣價或最小價位無效"
        if stats["spread_ticks"] > MOMENTUM_BOOK_MAX_SPREAD_TICKS:
            return f"spread:{stats['spread_ticks']}格大過{MOMENTUM_BOOK_MAX_SPREAD_TICKS}格"
        if not stats["has_book_volume"]:
            return "盤口量不足"
        if stats["buy_ratio"] < MOMENTUM_BOOK_MIN_BUY_RATIO:
            label = "牛證" if side == PositionType.BULL else "熊證"
            return f"{label}買盤佔比{stats['buy_ratio'] * 100:.1f}%低過{MOMENTUM_BOOK_MIN_BUY_RATIO * 100:.0f}%"
        return ""

    def _is_late_momentum_time(self, current_time: str) -> bool:
        try:
            return datetime.strptime(current_time[-8:], "%H:%M:%S").time() >= datetime.strptime(
                MOMENTUM_LATE_ENTRY_TIME,
                "%H:%M",
            ).time()
        except ValueError:
            return False

    def _required_momentum_volume_multiplier(self, current_time: str) -> float:
        if self._is_late_momentum_time(current_time):
            return MOMENTUM_LATE_VOLUME_SURGE_MULTIPLIER
        return MOMENTUM_VOLUME_SURGE_MULTIPLIER

    def _extreme_entry_hsi_confirmed(self, side: PositionType, hsi_price: float) -> bool:
        trigger_price = self.momentum_entry_trigger_price
        if trigger_price <= 0:
            return False
        if side == PositionType.BULL:
            return hsi_price >= trigger_price + EXTREME_ENTRY_CONFIRM_POINTS
        if side == PositionType.BEAR:
            return hsi_price <= trigger_price - EXTREME_ENTRY_CONFIRM_POINTS
        return False

    def _extreme_entry_one_tick_chase_price(
        self,
        snapshot: dict,
        side: PositionType,
        hsi_price: float,
    ) -> tuple[float | None, str]:
        stats = self._warrant_book_stats(snapshot)
        if stats["bid"] <= 0 or stats["ask"] <= 0 or stats["tick"] <= 0:
            return None, "買賣價或最小價位無效"
        if stats["spread_ticks"] != 1:
            return None, f"spread:{stats['spread_ticks']}格，唔追一格"
        if not stats["has_book_volume"]:
            return None, "盤口量不足，未用佔比追一格"
        if stats["buy_ratio"] < EXTREME_ENTRY_BOOK_BUY_RATIO:
            return None, f"買盤佔比{stats['buy_ratio'] * 100:.1f}%未達門檻"
        if stats["ask_volume"] > stats["bid_volume"] * EXTREME_ENTRY_ASK_THIN_RATIO:
            return None, "賣一未夠薄"
        if not self._extreme_entry_hsi_confirmed(side, hsi_price):
            return None, f"HSI未確認{EXTREME_ENTRY_CONFIRM_POINTS:.0f}點方向"

        decimals = _price_decimals(stats["tick"])
        return round(stats["bid"] + stats["tick"], decimals), "強買盤且賣一薄，追一格"

    def _extreme_entry_initial_price(self, snapshot: dict, qty: int) -> tuple[float, str, str]:
        stats = self._warrant_book_stats(snapshot)
        if stats["bid"] <= 0:
            return float(snapshot["bid_price"]), "buy1", "buy1"
        if stats["ask"] <= 0 or stats["tick"] <= 0:
            return stats["bid"], "buy1", "buy1"
        if stats["spread_ticks"] != 1 or not stats["has_book_volume"]:
            return stats["bid"], "buy1", "buy1"
        if stats["buy_ratio"] < EXTREME_ENTRY_FIRST_ASK_BUY_RATIO:
            return stats["bid"], "buy1", "buy1"
        if stats["ask_volume"] < qty * EXTREME_ENTRY_FIRST_ASK_MIN_FILL_RATIO:
            return stats["bid"], "buy1", "buy1"

        return (
            stats["ask"],
            "sell1",
            (
                f"超強買盤直接打 sell1 "
                f"買盤佔比:{stats['buy_ratio'] * 100:.1f}% ask量:{stats['ask_volume']:.0f}"
            ),
        )

    def _standard_entry_initial_price(self, snapshot: dict) -> tuple[float, str, str]:
        stats = self._warrant_book_stats(snapshot)
        if stats["bid"] <= 0:
            return float(snapshot["bid_price"]), "buy1", "buy1"
        if stats["ask"] <= 0 or stats["tick"] <= 0:
            return stats["bid"], "buy1", "buy1"

        decimals = _price_decimals(stats["tick"])
        if stats["spread_ticks"] == 2:
            return (
                round(stats["bid"] + stats["tick"], decimals),
                "中間",
                "buy1和sell1相隔1格，掛中間",
            )
        if stats["spread_ticks"] != 1 or not stats["has_book_volume"]:
            return stats["bid"], "buy1", "buy1"
        if stats["buy_ratio"] > OTHER_ENTRY_DIRECT_SELL1_BUY_RATIO:
            return (
                stats["ask"],
                "sell1",
                f"買盤佔比:{stats['buy_ratio'] * 100:.1f}% > {OTHER_ENTRY_DIRECT_SELL1_BUY_RATIO * 100:.0f}%",
            )
        return stats["bid"], "buy1", "buy1"

    def _cum_trend_stop_cooldown_remaining(self, side: PositionType) -> int:
        if (
            side == PositionType.NONE
            or self.last_cum_trend_stop_position != side
            or self.last_cum_trend_stop_time is None
        ):
            return 0

        elapsed = (datetime.now() - self.last_cum_trend_stop_time).total_seconds()
        remaining = CUM_TREND_SAME_SIDE_STOP_COOLDOWN_SECONDS - elapsed
        return max(0, int(remaining))

    def _mark_cum_trend_stop_if_needed(
        self,
        entry_mode: str,
        side: PositionType,
        pnl_hkd: float,
    ):
        if entry_mode != CUM_TREND_ENTRY_MODE or side == PositionType.NONE or pnl_hkd >= 0:
            return
        self.last_cum_trend_stop_position = side
        self.last_cum_trend_stop_time = datetime.now()

    def _cum_trend_trigger_key(self, signal_kline_time: str, side: PositionType) -> str:
        return f"{signal_kline_time}:{side.value}"

    def _is_duplicate_cum_trend_signal(self, signal_kline_time: str, side: PositionType) -> bool:
        return self.last_cum_trend_trigger_key == self._cum_trend_trigger_key(signal_kline_time, side)

    def _mark_cum_trend_signal_submitted(self, signal_kline_time: str, side: PositionType):
        self.last_cum_trend_trigger_key = self._cum_trend_trigger_key(signal_kline_time, side)
        self._save_runtime_state()

    def _completed_cum5(self, df_1m) -> float:
        if len(df_1m) < 7:
            return 0.0
        completed = df_1m.iloc[:-1]
        recent_closes = [completed.iloc[j]["close"] for j in range(len(completed) - 6, len(completed))]
        return float(recent_closes[-1] - recent_closes[0])

    def _completed_vwap_slopes_confirm(self, df_1m, side: PositionType) -> bool:
        if len(df_1m) < CUM_TREND_VWAP_CONFIRM_BARS + 1:
            return False
        completed = df_1m.iloc[:-1]
        slopes = completed.tail(CUM_TREND_VWAP_CONFIRM_BARS)["VWAP_SLOPE"]
        if any(_is_nan(value) for value in slopes):
            return False
        if side == PositionType.BULL:
            return all(float(value) > 0 for value in slopes)
        if side == PositionType.BEAR:
            return all(float(value) < 0 for value in slopes)
        return False

    def _cum_trend_entry_block_reasons(
        self,
        side: PositionType,
        signal_kline_time: str,
        completed_rsi: float,
        df_1m,
    ) -> list[str]:
        reasons: list[str] = []
        if self._is_duplicate_cum_trend_signal(signal_kline_time, side):
            reasons.append(f"同K线已尝试: {signal_kline_time}")

        cooldown_remaining = self._cum_trend_stop_cooldown_remaining(side)
        if cooldown_remaining > 0:
            reasons.append(f"同方向累积趋势止损后冷却中 remaining:{cooldown_remaining}s")

        if not self._completed_vwap_slopes_confirm(df_1m, side):
            direction = "向上" if side == PositionType.BULL else "向下"
            reasons.append(f"VWAP未连续{direction}")

        if side == PositionType.BULL and completed_rsi > CUM_TREND_BULL_PULLBACK_RSI:
            label = "RSI过热" if completed_rsi >= CUM_TREND_BULL_OVERHEAT_RSI else "RSI未回踩"
            reasons.append(
                f"{label}等待回踩 rsi={completed_rsi:.2f} "
                f"resume<={CUM_TREND_BULL_PULLBACK_RSI:.0f}"
            )
        elif side == PositionType.BEAR and completed_rsi < CUM_TREND_BEAR_REBOUND_RSI:
            label = "RSI过冷" if completed_rsi <= CUM_TREND_BEAR_OVERSOLD_RSI else "RSI未反弹"
            reasons.append(
                f"{label}等待反弹 rsi={completed_rsi:.2f} "
                f"resume>={CUM_TREND_BEAR_REBOUND_RSI:.0f}"
            )
        return reasons

    def _reset_rsi_divergence_state(self):
        self.rsi_divergence_bull_lows.clear()
        self.rsi_divergence_bear_highs.clear()
        self.rsi_divergence_used_bull_keys.clear()
        self.rsi_divergence_used_bear_keys.clear()
        self.rsi_divergence_used_bull_c_times.clear()
        self.rsi_divergence_used_bear_c_times.clear()
        self.rsi_divergence_last_bull_entry_time = None
        self.rsi_divergence_last_bear_entry_time = None
        self.rsi_divergence_last_processed_pivot_time = ""

    def _append_rsi_divergence_point(self, points: list[RsiDivergencePoint], point: RsiDivergencePoint):
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

    def _rsi_divergence_point_key(
        self,
        a: RsiDivergencePoint,
        b: RsiDivergencePoint,
        c: RsiDivergencePoint,
    ) -> str:
        return f"{a.time.isoformat()}|{b.time.isoformat()}|{c.time.isoformat()}"

    def _find_rsi_divergence_triplet(
        self,
        points: list[RsiDivergencePoint],
        side: PositionType,
        used_keys: set[str],
        used_c_times: set[str],
    ) -> tuple[RsiDivergencePoint, RsiDivergencePoint, RsiDivergencePoint] | None:
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
                    key = self._rsi_divergence_point_key(a, b, c)
                    if key in used_keys:
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
                        if price_ok and rsi_ok and c.rsi <= RSI_DIVERGENCE_BULL_MAX_RSI:
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
                        if price_ok and rsi_ok and c.rsi >= RSI_DIVERGENCE_BEAR_MIN_RSI:
                            return a, b, c
        return None

    def _detect_rsi_divergence_signal(
        self,
        df_1m,
    ) -> tuple[PositionType, str, str] | None:
        if len(df_1m) < 13:
            return None

        ts = df_1m.index[-1]
        pivot_ts = df_1m.index[-2]
        day_key = ts.strftime("%Y-%m-%d")
        if self.rsi_divergence_day != day_key:
            self._reset_rsi_divergence_state()
            self.rsi_divergence_day = day_key

        pivot_key = pivot_ts.strftime("%Y-%m-%d %H:%M:%S")
        if self.rsi_divergence_last_processed_pivot_time == pivot_key:
            return None
        self.rsi_divergence_last_processed_pivot_time = pivot_key

        row = df_1m.iloc[-1]
        pivot = df_1m.iloc[-2]
        pivot_close = float(pivot["close"])
        pivot_rsi = float(pivot["RSI"])
        if _is_nan(pivot_rsi):
            return None

        open_price = float(df_1m.iloc[0]["open"])
        day_high = float(df_1m["high"].max())
        day_low = float(df_1m["low"].min())
        if day_high - day_low < RSI_DIVERGENCE_MIN_DAY_RANGE_POINTS:
            return None

        prev2_closes = [float(value) for value in df_1m.iloc[-4:-2]["close"]]
        current_close = float(row["close"])
        pivot_is_local_close_low = pivot_close <= min(prev2_closes) and pivot_close <= current_close
        pivot_is_local_close_high = pivot_close >= max(prev2_closes) and pivot_close >= current_close

        if pivot_is_local_close_low and open_price - pivot_close >= RSI_DIVERGENCE_MIN_DAY_MOVE_POINTS:
            self._append_rsi_divergence_point(
                self.rsi_divergence_bull_lows,
                RsiDivergencePoint(time=pivot_ts, price=pivot_close, rsi=pivot_rsi),
            )
        if pivot_is_local_close_high and pivot_close - open_price >= RSI_DIVERGENCE_MIN_DAY_MOVE_POINTS:
            self._append_rsi_divergence_point(
                self.rsi_divergence_bear_highs,
                RsiDivergencePoint(time=pivot_ts, price=pivot_close, rsi=pivot_rsi),
            )

        bull_triplet = self._find_rsi_divergence_triplet(
            self.rsi_divergence_bull_lows,
            PositionType.BULL,
            self.rsi_divergence_used_bull_keys,
            self.rsi_divergence_used_bull_c_times,
        )
        if bull_triplet is not None:
            a, b, c = bull_triplet
            if (
                self.rsi_divergence_last_bull_entry_time is None
                or (ts - self.rsi_divergence_last_bull_entry_time).total_seconds() >= 15 * 60
            ):
                self.rsi_divergence_last_bull_entry_time = ts
                self.rsi_divergence_used_bull_keys.add(self._rsi_divergence_point_key(a, b, c))
                self.rsi_divergence_used_bull_c_times.add(c.time.isoformat())
                self.rsi_divergence_bull_lows.clear()
                desc = (
                    f"连续底背离 | "
                    f"{a.time.strftime('%H:%M')} {a.price:.1f}/RSI{a.rsi:.1f} -> "
                    f"{b.time.strftime('%H:%M')} {b.price:.1f}/RSI{b.rsi:.1f} -> "
                    f"{c.time.strftime('%H:%M')} {c.price:.1f}/RSI{c.rsi:.1f}"
                )
                return PositionType.BULL, "bullish_divergence", desc

        bear_triplet = self._find_rsi_divergence_triplet(
            self.rsi_divergence_bear_highs,
            PositionType.BEAR,
            self.rsi_divergence_used_bear_keys,
            self.rsi_divergence_used_bear_c_times,
        )
        if bear_triplet is not None:
            a, b, c = bear_triplet
            if (
                self.rsi_divergence_last_bear_entry_time is None
                or (ts - self.rsi_divergence_last_bear_entry_time).total_seconds() >= 15 * 60
            ):
                self.rsi_divergence_last_bear_entry_time = ts
                self.rsi_divergence_used_bear_keys.add(self._rsi_divergence_point_key(a, b, c))
                self.rsi_divergence_used_bear_c_times.add(c.time.isoformat())
                self.rsi_divergence_bear_highs.clear()
                desc = (
                    f"连续顶背离 | "
                    f"{a.time.strftime('%H:%M')} {a.price:.1f}/RSI{a.rsi:.1f} -> "
                    f"{b.time.strftime('%H:%M')} {b.price:.1f}/RSI{b.rsi:.1f} -> "
                    f"{c.time.strftime('%H:%M')} {c.price:.1f}/RSI{c.rsi:.1f}"
                )
                return PositionType.BEAR, "bearish_divergence", desc

        return None

    def _completed_extreme_signal(
        self,
        row,
        current_price: float,
        current_rsi: float,
    ) -> tuple[PositionType, str]:
        rsi = float(row["RSI"])
        close = float(row["close"])
        open_price = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        vol = float(row["volume"])
        vol_ma = float(row["VOL_MA"])
        if _is_nan(rsi) or _is_nan(vol_ma) or vol_ma <= 0:
            return PositionType.NONE, ""

        k_change = close - open_price
        k_body_points = abs(k_change)
        if k_change == 0:
            return PositionType.NONE, ""

        shadow_side, shadow_message = self._completed_very_extreme_shadow_bull_signal(
            row,
            current_price,
            current_rsi,
            close,
            open_price,
            low,
            vol,
            vol_ma,
            rsi,
        )
        if shadow_side != PositionType.NONE:
            return shadow_side, shadow_message
        shadow_side, shadow_message = self._completed_very_extreme_shadow_bear_signal(
            row,
            current_price,
            current_rsi,
            close,
            open_price,
            high,
            vol,
            vol_ma,
            rsi,
        )
        if shadow_side != PositionType.NONE:
            return shadow_side, shadow_message

        if k_body_points < MOMENTUM_MIN_K_BODY_POINTS:
            return PositionType.NONE, ""

        momentum_ratio = vol / vol_ma
        side, dynamic_low_volume, trigger_label = _extreme_signal_side(
            rsi,
            momentum_ratio,
            current_price,
            high,
            low,
            self.rsi_oversold,
            self.rsi_overbought,
        )
        if side == PositionType.NONE:
            return PositionType.NONE, ""

        move_from_signal = current_price - close

        if side == PositionType.BEAR:
            if current_rsi < self.rsi_overbought - EXTREME_COMPLETED_K_RSI_BUFFER:
                return PositionType.NONE, ""
            if move_from_signal > EXTREME_COMPLETED_K_MAX_ADVERSE_MOVE_POINTS:
                return PositionType.NONE, ""
            if move_from_signal < -EXTREME_COMPLETED_K_MAX_FAVORABLE_MOVE_POINTS:
                return PositionType.NONE, ""
            return PositionType.BEAR, (
                f"上一根完成K触发 | {trigger_label} | RSI:{rsi:.2f} | "
                f"{'阳线涨' if k_change > 0 else '阴线跌'}{k_body_points:.1f}点 | "
                f"{momentum_ratio:.2f}x量 | 当前偏离:{move_from_signal:+.1f}点"
            )

        if side == PositionType.BULL:
            if current_rsi > self.rsi_oversold + EXTREME_COMPLETED_K_RSI_BUFFER:
                return PositionType.NONE, ""
            if move_from_signal < -EXTREME_COMPLETED_K_MAX_ADVERSE_MOVE_POINTS:
                return PositionType.NONE, ""
            if move_from_signal > EXTREME_COMPLETED_K_MAX_FAVORABLE_MOVE_POINTS:
                return PositionType.NONE, ""
            return PositionType.BULL, (
                f"上一根完成K触发 | {trigger_label} | RSI:{rsi:.2f} | "
                f"{'阳线涨' if k_change > 0 else '阴线跌'}{k_body_points:.1f}点 | "
                f"{momentum_ratio:.2f}x量 | 当前偏离:{move_from_signal:+.1f}点"
            )

        return PositionType.NONE, ""

    def _completed_very_extreme_shadow_bull_signal(
        self,
        row,
        current_price: float,
        current_rsi: float,
        close: float,
        open_price: float,
        low: float,
        vol: float,
        vol_ma: float,
        rsi: float,
    ) -> tuple[PositionType, str]:
        if rsi > VERY_EXTREME_SHADOW_BULL_RSI:
            return PositionType.NONE, ""

        momentum_ratio = vol / vol_ma
        if momentum_ratio < VERY_EXTREME_SHADOW_MIN_VOLUME_RATIO:
            return PositionType.NONE, ""

        lower_shadow = min(open_price, close) - low
        rebound_from_low = close - low
        if lower_shadow < VERY_EXTREME_SHADOW_MIN_LOWER_SHADOW_POINTS:
            return PositionType.NONE, ""
        if rebound_from_low < VERY_EXTREME_SHADOW_MIN_REBOUND_POINTS:
            return PositionType.NONE, ""

        move_from_signal = current_price - close
        if move_from_signal > VERY_EXTREME_SHADOW_MAX_ENTRY_CHASE_POINTS:
            return PositionType.NONE, ""
        if current_rsi > VERY_EXTREME_SHADOW_BULL_RSI + EXTREME_COMPLETED_K_RSI_BUFFER:
            return PositionType.NONE, ""

        return PositionType.BULL, (
            f"上一根完成K触发 | {VERY_EXTREME_SHADOW_BULL_ENTRY_MODE} | RSI:{rsi:.2f} | "
            f"低位反抽{rebound_from_low:.1f}点 | 下影{lower_shadow:.1f}点 | "
            f"{momentum_ratio:.2f}x量 | 当前偏离:{move_from_signal:+.1f}点"
        )

    def _completed_very_extreme_shadow_bear_signal(
        self,
        row,
        current_price: float,
        current_rsi: float,
        close: float,
        open_price: float,
        high: float,
        vol: float,
        vol_ma: float,
        rsi: float,
    ) -> tuple[PositionType, str]:
        if rsi < VERY_EXTREME_SHADOW_BEAR_RSI:
            return PositionType.NONE, ""

        momentum_ratio = vol / vol_ma
        if momentum_ratio < VERY_EXTREME_SHADOW_MIN_VOLUME_RATIO:
            return PositionType.NONE, ""

        upper_shadow = high - max(open_price, close)
        pullback_from_high = high - close
        if upper_shadow < VERY_EXTREME_SHADOW_MIN_UPPER_SHADOW_POINTS:
            return PositionType.NONE, ""
        if pullback_from_high < VERY_EXTREME_SHADOW_MIN_PULLBACK_POINTS:
            return PositionType.NONE, ""

        move_from_signal = current_price - close
        if move_from_signal < -VERY_EXTREME_SHADOW_MAX_ENTRY_CHASE_POINTS:
            return PositionType.NONE, ""
        if current_rsi < VERY_EXTREME_SHADOW_BEAR_RSI - EXTREME_COMPLETED_K_RSI_BUFFER:
            return PositionType.NONE, ""

        return PositionType.BEAR, (
            f"上一根完成K触发 | {VERY_EXTREME_SHADOW_BEAR_ENTRY_MODE} | RSI:{rsi:.2f} | "
            f"高位回落{pullback_from_high:.1f}点 | 上影{upper_shadow:.1f}点 | "
            f"{momentum_ratio:.2f}x量 | 当前偏离:{move_from_signal:+.1f}点"
        )

    def _update_extreme_stop_reversal_guard(
        self,
        entry_mode: str,
        position: PositionType,
        pnl_hkd: float,
    ):
        if pnl_hkd < 0 and entry_mode in EXTREME_ENTRY_MODES:
            self.last_extreme_stop_mode = entry_mode
            self.last_extreme_stop_position = position
            self.last_extreme_stop_time = datetime.now()
            self.last_reversal_guard_log_key = ""

    def _is_blocked_by_extreme_stop_reversal_guard(
        self,
        direction: str,
        current_time: str,
    ) -> tuple[bool, int]:
        if not self.last_extreme_stop_time:
            return False, 0

        try:
            now = datetime.strptime(current_time, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            now = datetime.now()
        elapsed = max((now - self.last_extreme_stop_time).total_seconds(), 0)
        remaining = max(int(EXTREME_STOP_REVERSAL_GUARD_SECONDS - elapsed), 0)
        if remaining <= 0:
            return False, 0

        if (
            direction == "bull"
            and self.last_extreme_stop_mode == "极度超买"
            and self.last_extreme_stop_position == PositionType.BEAR
        ):
            return True, remaining
        if (
            direction == "bear"
            and self.last_extreme_stop_mode == "极度超卖"
            and self.last_extreme_stop_position == PositionType.BULL
        ):
            return True, remaining
        return False, 0

    async def _emit_reversal_guard_skip(
        self,
        current_time: str,
        hsi_price: float,
        rsi: float,
        direction: str,
        remaining_seconds: int,
    ):
        minute_key = current_time[:16] if len(current_time) >= 16 else current_time
        log_key = f"{minute_key}:{direction}"
        if self.last_reversal_guard_log_key == log_key:
            return
        self.last_reversal_guard_log_key = log_key
        self._save_runtime_state()
        remaining_minutes = max(1, math.ceil(remaining_seconds / 60))
        await self._emit_trade_record(TradeRecord(
            time=current_time,
            signal=TradeSignal.HOLD,
            price=hsi_price,
            rsi=round(rsi, 2),
            position=PositionType.NONE,
            message=(
                f"跳过累积趋势反手: {self.last_extreme_stop_mode}止损后 "
                f"5分钟保护中 | direction:{direction} | remaining:{remaining_minutes}m"
            ),
        ))

    def _is_after_entry_cutoff(self, current_time: str) -> bool:
        time_part = current_time[11:16] if len(current_time) >= 16 else current_time[:5]
        return time_part >= self.entry_cutoff_time

    def _is_after_force_exit_time(self, current_time: str) -> bool:
        time_part = current_time[11:16] if len(current_time) >= 16 else current_time[:5]
        return time_part >= FORCE_EXIT_TIME

    async def _cancel_pending_entry_after_cutoff(self, current_time: str, hsi_price: float, rsi: float):
        if not self.pending_buy_order_id:
            return
        order = self.trader.get_order(self.pending_buy_order_id, force_refresh=True)
        if order is not None and _is_filled_all(order.get("order_status", "")):
            await self._monitor_entry_order(current_time, hsi_price, rsi)
            return
        result = self.trader.cancel_order(self.pending_buy_order_id)
        await self._emit_trade_record(TradeRecord(
            time=current_time,
            signal=TradeSignal.ENTRY_CHASING if self.entry_chase_count > 0 else TradeSignal.ENTRY_PENDING,
            price=hsi_price,
            rsi=round(rsi, 2),
            position=PositionType.NONE,
            message=(
                f"{self.entry_cutoff_time}后不再开新仓，已取消未成交买入单: "
                f"order_id:{self.pending_buy_order_id} | {result.get('message')}"
            ),
        ))
        self._reset_order_state()
        self._save_runtime_state()

    async def _cancel_degraded_cum_trend_entry(
        self,
        current_time: str,
        hsi_price: float,
        rsi: float,
        curr_slope: float,
        cum5: float,
    ) -> bool:
        if (
            self.entry_mode != CUM_TREND_ENTRY_MODE
            or not self.pending_buy_order_id
            or self.position != PositionType.NONE
            or self.momentum_entry_trigger_price <= 0
        ):
            return False

        side = self.pending_entry_side
        trigger_price = self.momentum_entry_trigger_price
        adverse_move = 0.0
        reasons: list[str] = []
        if side == PositionType.BEAR:
            adverse_move = hsi_price - trigger_price
            if adverse_move >= CUM_TREND_PENDING_ADVERSE_MOVE_POINTS:
                reasons.append(f"反弹{adverse_move:.1f}点")
            if cum5 >= -CUM_TREND_BOUNDARY_POINTS:
                reasons.append(f"累跌收窄 cum5:{cum5:.1f}")
            if curr_slope >= 0:
                reasons.append("VWAP斜率不再向下")
        elif side == PositionType.BULL:
            adverse_move = trigger_price - hsi_price
            if adverse_move >= CUM_TREND_PENDING_ADVERSE_MOVE_POINTS:
                reasons.append(f"回落{adverse_move:.1f}点")
            if cum5 <= CUM_TREND_BOUNDARY_POINTS:
                reasons.append(f"累涨收窄 cum5:{cum5:.1f}")
            if curr_slope <= 0:
                reasons.append("VWAP斜率不再向上")
        else:
            return False

        if not reasons:
            return False

        order = self.trader.get_order(self.pending_buy_order_id, force_refresh=True)
        if order is not None and _is_filled_all(order.get("order_status", "")):
            await self._monitor_entry_order(current_time, hsi_price, rsi)
            return True

        result = self.trader.cancel_order(self.pending_buy_order_id)
        await self._emit_trade_record(TradeRecord(
            time=current_time,
            signal=TradeSignal.ENTRY_PENDING,
            price=hsi_price,
            rsi=round(rsi, 2),
            position=PositionType.NONE,
            message=(
                f"累积趋势买入挂单期间信号退化，已取消: "
                f"order_id:{self.pending_buy_order_id} | 触发价:{trigger_price:.2f} "
                f"| {'; '.join(reasons)} | {result.get('message')}"
            ),
        ))
        self._reset_order_state()
        self._save_runtime_state()
        return True

    async def _cancel_degraded_momentum_entry(
        self,
        current_time: str,
        hsi_price: float,
        rsi: float,
        curr_slope: float,
    ) -> bool:
        if (
            self.entry_mode != MOMENTUM_ENTRY_MODE
            or not self.pending_buy_order_id
            or self.position != PositionType.NONE
            or self.momentum_entry_trigger_price <= 0
        ):
            return False

        side = self.pending_entry_side
        trigger_price = self.momentum_entry_trigger_price
        reasons: list[str] = []
        if side == PositionType.BULL:
            adverse_move = trigger_price - hsi_price
            if adverse_move >= MOMENTUM_PENDING_ADVERSE_MOVE_POINTS:
                reasons.append(f"回落{adverse_move:.1f}点")
            if curr_slope <= -MOMENTUM_PENDING_VWAP_SLOPE_BUFFER:
                reasons.append(f"VWAP斜率反向<=-{MOMENTUM_PENDING_VWAP_SLOPE_BUFFER:.2f}")
        elif side == PositionType.BEAR:
            adverse_move = hsi_price - trigger_price
            if adverse_move >= MOMENTUM_PENDING_ADVERSE_MOVE_POINTS:
                reasons.append(f"反弹{adverse_move:.1f}点")
            if curr_slope >= MOMENTUM_PENDING_VWAP_SLOPE_BUFFER:
                reasons.append(f"VWAP斜率反向>={MOMENTUM_PENDING_VWAP_SLOPE_BUFFER:.2f}")
        else:
            return False

        if not reasons:
            return False

        order = self.trader.get_order(self.pending_buy_order_id, force_refresh=True)
        if order is not None and _is_filled_all(order.get("order_status", "")):
            await self._monitor_entry_order(current_time, hsi_price, rsi)
            return True

        result = self.trader.cancel_order(self.pending_buy_order_id)
        await self._emit_trade_record(TradeRecord(
            time=current_time,
            signal=TradeSignal.ENTRY_PENDING,
            price=hsi_price,
            rsi=round(rsi, 2),
            position=PositionType.NONE,
            message=(
                f"放量动能买入挂单期间信号退化，已取消: "
                f"order_id:{self.pending_buy_order_id} | 触发价:{trigger_price:.2f} "
                f"| {'; '.join(reasons)} | {result.get('message')}"
            ),
        ))
        self._reset_order_state()
        self._save_runtime_state()
        return True

    async def _force_exit_position_after_cutoff(self, current_time: str, hsi_price: float, rsi: float):
        if self.position == PositionType.NONE or not self.current_warrant_code:
            return

        order = self.trader.get_order(self.exit_order_id) if self.exit_order_id else None
        if order is not None and _is_filled_all(order.get("order_status", "")):
            await self._finalize_exit_fill(order, hsi_price, rsi)
            return
        if self.stop_loss_order_sent:
            return

        exit_price, reason = self._get_stop_exit_price(self.current_warrant_code)
        if exit_price is None:
            await self._emit_trade_record(TradeRecord(
                time=current_time,
                signal=TradeSignal.STOP_LOSS_PENDING,
                price=hsi_price,
                rsi=round(rsi, 2),
                position=self.position,
                message=f"15:55强制平仓失败: {reason} | order_id:{self.exit_order_id or '-'}",
            ))
            return

        if order is not None and self._is_terminal_unfilled_exit_status(order.get("order_status", "")):
            remain_qty = self._remaining_exit_qty(order)
            if remain_qty <= 0:
                await self._mark_exit_complete_from_order(order, current_time, hsi_price, rsi)
                return
            result = self.trader.place_order(self.current_warrant_code, exit_price, remain_qty, "SELL")
            action = "卖单失效，已按最新 buy1 补挂强平卖单"
        elif self.exit_order_id:
            target_qty = float(order.get("qty", self.warrant_qty or self.share_count)) if order else float(self.warrant_qty or self.share_count)
            result = self.trader.modify_order(self.exit_order_id, exit_price, target_qty)
            action = "已改现有卖单到最新 buy1 强平"
        else:
            result = self.trader.place_order(
                self.current_warrant_code,
                exit_price,
                int(self.warrant_qty or self.share_count),
                "SELL",
            )
            action = "已按最新 buy1 补挂强平卖单"

        if result.get("success"):
            if not self.exit_order_id or action.startswith("卖单失效") or action.startswith("已按"):
                self.exit_order_id = result["order_id"]
            self.stop_loss_order_sent = True
            self.warrant_exit_price = exit_price
            self._save_runtime_state()

        dealt_qty = float(order.get("dealt_qty", 0.0)) if order else 0.0
        remain_qty = self._remaining_exit_qty(order)
        await self._emit_trade_record(TradeRecord(
            time=current_time,
            signal=TradeSignal.STOP_LOSS_PENDING,
            price=hsi_price,
            rsi=round(rsi, 2),
            position=self.position,
            message=(
                f"15:55强制平仓，{action}: {self.current_warrant_code} "
                f"@ {exit_price:.3f} | order_id:{self.exit_order_id or '-'} "
                f"| 已成交:{dealt_qty:.0f} 剩余:{remain_qty} | {result.get('message')}"
            ),
        ))

    async def _submit_entry_order(
        self,
        side: PositionType,
        hsi_price: float,
        rsi: float,
        current_time: str,
        mode: str,
        extra_message: str = "",
    ) -> bool:
        if self.position != PositionType.NONE or self.pending_buy_order_id:
            return False

        label = "牛证" if side == PositionType.BULL else "熊证"
        if self._is_after_entry_cutoff(current_time):
            await self._emit_trade_record(TradeRecord(
                time=current_time, signal=TradeSignal.ENTRY_PENDING, price=hsi_price, rsi=round(rsi, 2),
                position=side,
                message=f"【{label}·{mode}】跳过: {self.entry_cutoff_time}后不再开新仓",
            ))
            return False

        if not self._mode_allowed_by_strategy_selection(mode):
            await self._emit_trade_record(TradeRecord(
                time=current_time, signal=TradeSignal.ENTRY_PENDING, price=hsi_price, rsi=round(rsi, 2),
                position=side,
                message=f"【{label}·{mode}】跳过: 当前实盘策略选择未启用此策略",
            ))
            return False

        cooldown_remaining = self._same_side_take_profit_cooldown_remaining(side)
        if cooldown_remaining > 0:
            remaining_minutes = max(1, math.ceil(cooldown_remaining / 60))
            log_key = f"{side.value}:{mode}:{remaining_minutes}"
            if log_key != self.last_take_profit_cooldown_log_key:
                self.last_take_profit_cooldown_log_key = log_key
                await self._emit_trade_record(TradeRecord(
                    time=current_time, signal=TradeSignal.ENTRY_PENDING, price=hsi_price, rsi=round(rsi, 2),
                    position=side,
                    message=(
                        f"【{label}·{mode}】跳过: 同方向止盈后冷却中 "
                        f"remaining:{remaining_minutes}分钟"
                    ),
                ))
            return False

        raw_code = self.bull_warrant_code if side == PositionType.BULL else self.bear_warrant_code
        code = normalize_warrant_code(raw_code)
        signal = TradeSignal.BUY_BULL if side == PositionType.BULL else TradeSignal.BUY_BEAR
        if not code:
            await self._emit_trade_record(TradeRecord(
                time=current_time, signal=TradeSignal.ENTRY_PENDING, price=hsi_price, rsi=round(rsi, 2),
                position=side, message=f"【{label}·{mode}】未下单: 未配置{label} number",
            ))
            return False

        snapshot = self.data_source.get_security_snapshot(
            code,
            include_order_book=True,
        )
        if snapshot is None:
            await self._emit_trade_record(TradeRecord(
                time=current_time, signal=TradeSignal.ENTRY_PENDING, price=hsi_price, rsi=round(rsi, 2),
                position=side, message=f"【{label}·{mode}】未下单: {code} 买一/卖一/价差无效",
            ))
            return False

        book_message = self._format_entry_book(snapshot)
        if mode == MOMENTUM_ENTRY_MODE:
            block_reason = self._momentum_book_block_reason(side, snapshot)
            if block_reason:
                await self._emit_trade_record(TradeRecord(
                    time=current_time,
                    signal=TradeSignal.ENTRY_PENDING,
                    price=hsi_price,
                    rsi=round(rsi, 2),
                    position=side,
                    message=f"【{label}·{mode}】跳过: {block_reason} | {book_message}",
                ))
                return False

        if mode in EXTREME_ENTRY_MODES:
            buy_price, entry_price_label, entry_price_reason = self._extreme_entry_initial_price(
                snapshot,
                self.share_count,
            )
        else:
            buy_price, entry_price_label, entry_price_reason = self._standard_entry_initial_price(snapshot)
        result = self.trader.place_order(code, buy_price, self.share_count, "BUY")
        if not result.get("success"):
            await self._emit_trade_record(TradeRecord(
                time=current_time, signal=TradeSignal.ENTRY_PENDING, price=hsi_price, rsi=round(rsi, 2),
                position=side,
                message=(
                    f"【{label}·{mode}】买入挂单失败: {code} @ {buy_price:.3f} "
                    f"| {book_message} | {result.get('message')}"
                ),
            ))
            return False

        self.current_warrant_code = code
        self.pending_entry_side = side
        self.pending_buy_order_id = result["order_id"]
        self.entry_order_time = datetime.now()
        self.entry_chase_count = 0
        self.warrant_tick_size = snapshot["price_spread"]
        self.warrant_qty = float(self.share_count)
        self.entry_mode = mode
        self.momentum_entry_trigger_price = (
            hsi_price if mode in {MOMENTUM_ENTRY_MODE, CUM_TREND_ENTRY_MODE} or mode in EXTREME_ENTRY_MODES else 0.0
        )
        self._save_runtime_state()

        suffix = f" | {extra_message}" if extra_message else ""
        await self._emit_trade_record(TradeRecord(
            time=current_time, signal=TradeSignal.ENTRY_PENDING, price=hsi_price, rsi=round(rsi, 2),
            position=side,
            message=(
                f"【{label}·{mode}】挂 {entry_price_label} 买入: {code} x{self.share_count} "
                f"@ {buy_price:.3f} | {book_message} "
                f"| 定價:{entry_price_reason} | order_id:{self.pending_buy_order_id}{suffix}"
            ),
        ))
        return True

    async def _monitor_entry_order(self, current_time: str, hsi_price: float, rsi: float):
        if not self.pending_buy_order_id:
            return

        order = self.trader.get_order(self.pending_buy_order_id)
        if order is None:
            if self.entry_order_time is None:
                return
            elapsed = (datetime.now() - self.entry_order_time).total_seconds()
            if elapsed < self._entry_wait_seconds_for_pending_order() * 2:
                return
            await self._emit_trade_record(TradeRecord(
                time=current_time,
                signal=TradeSignal.ENTRY_PENDING,
                price=hsi_price,
                rsi=round(rsi, 2),
                position=PositionType.NONE,
                message=(
                    f"买入挂单查不到且已超时，清空旧 pending: "
                    f"order_id:{self.pending_buy_order_id}"
                ),
            ))
            self._reset_order_state()
            self._save_runtime_state()
            return

        status = order["order_status"]
        status_name = _order_status_name(status)
        if status_name == "FILLED_ALL":
            fill_hsi_price = self._get_live_hsi_price(hsi_price)
            dealt_qty = order["dealt_qty"]
            dealt_avg_price = order["dealt_avg_price"]
            if dealt_qty <= 0 or dealt_avg_price <= 0:
                return

            self.position = self.pending_entry_side
            self.entry_price = fill_hsi_price
            self.entry_fill_time = datetime.now()
            self.momentum_fail_fast_sent = False
            self.trade_count += 1
            self.warrant_qty = dealt_qty
            self.warrant_entry_price = dealt_avg_price
            target_price = round(
                dealt_avg_price + 2 * self.warrant_tick_size,
                _price_decimals(self.warrant_tick_size),
            )
            sell_result = self.trader.place_order(
                self.current_warrant_code,
                target_price,
                int(dealt_qty),
                "SELL",
            )
            if sell_result.get("success"):
                self.exit_order_id = sell_result["order_id"]
                self.warrant_exit_price = target_price
                self.pending_buy_order_id = ""
                self.entry_order_time = None
                self._save_runtime_state()
                label = "牛证" if self.position == PositionType.BULL else "熊证"
                active_stop_points = self._active_stop_points()
                active_stop_pnl = self._active_stop_pnl()
                await self._emit_trade_record(TradeRecord(
                    time=current_time,
                    signal=TradeSignal.BUY_BULL if self.position == PositionType.BULL else TradeSignal.BUY_BEAR,
                    price=fill_hsi_price,
                    rsi=round(rsi, 2),
                    position=self.position,
                    message=(
                        f"【{label}】买入全数成交: {self.current_warrant_code} "
                        f"x{dealt_qty:.0f} @ {dealt_avg_price:.3f}; "
                        f"成交时HSI:{fill_hsi_price:.2f}; "
                        f"已挂 +2格卖出 @ {target_price:.3f} | order_id:{self.exit_order_id}; "
                        f"入场模式:{self.entry_mode or '-'}; "
                        f"止损阈值:{active_stop_points:.1f}点 / {active_stop_pnl:.0f}HKD"
                    ),
                ))
            else:
                self.pending_buy_order_id = ""
                self.entry_order_time = None
                self._save_runtime_state()
                await self._emit_trade_record(TradeRecord(
                    time=current_time,
                    signal=TradeSignal.BUY_BULL if self.position == PositionType.BULL else TradeSignal.BUY_BEAR,
                    price=fill_hsi_price,
                    rsi=round(rsi, 2),
                    position=self.position,
                    message=f"买入已成交，但 +2格 卖单失败: {sell_result.get('message')}",
                ))
            return

        if status_name in {"CANCELLED_ALL", "CANCELLED_PART", "FAILED", "SUBMIT_FAILED", "DELETED", "DISABLED"}:
            await self._emit_trade_record(TradeRecord(
                time=current_time,
                signal=TradeSignal.ENTRY_CHASING if self.entry_chase_count > 0 else TradeSignal.ENTRY_PENDING,
                price=hsi_price,
                rsi=round(rsi, 2),
                position=PositionType.NONE,
                message=f"买入挂单已结束未全数成交: order_id:{self.pending_buy_order_id} status:{status}",
            ))
            self._reset_order_state()
            self._save_runtime_state()
            return

        if self.entry_order_time is None:
            return

        elapsed = (datetime.now() - self.entry_order_time).total_seconds()
        wait_seconds = self._entry_wait_seconds_for_pending_order()
        if elapsed < wait_seconds:
            return

        if self.entry_chase_count == 0:
            if self.entry_mode == CUM_TREND_ENTRY_MODE:
                result = self.trader.cancel_order(self.pending_buy_order_id)
                await self._emit_trade_record(TradeRecord(
                    time=current_time,
                    signal=TradeSignal.ENTRY_PENDING,
                    price=hsi_price,
                    rsi=round(rsi, 2),
                    position=PositionType.NONE,
                    message=(
                        f"累积趋势买入未成交，不追价，已取消今轮信号: "
                        f"order_id:{self.pending_buy_order_id} | {result.get('message')}"
                    ),
                ))
                self._reset_order_state()
                self._save_runtime_state()
                return

            snapshot = self.data_source.get_security_snapshot(
                self.current_warrant_code,
                include_order_book=self.entry_mode in EXTREME_ENTRY_MODES,
            )
            if snapshot is None:
                return
            new_price = snapshot["bid_price"]
            chase_reason = "追價到最新 buy1"
            conservative_extreme_buy1 = False
            if self.entry_mode in EXTREME_ENTRY_MODES:
                chase_price, chase_reason = self._extreme_entry_one_tick_chase_price(
                    snapshot,
                    self.pending_entry_side,
                    hsi_price,
                )
                if chase_price is None:
                    conservative_extreme_buy1 = True
                    chase_reason = f"{chase_reason}，保守继续挂最新 buy1"
                else:
                    new_price = chase_price
            price_decimals = _price_decimals(snapshot["price_spread"])
            current_order_price = float(order.get("price", 0.0))
            price_unchanged = (
                current_order_price > 0
                and round(new_price, price_decimals) == round(current_order_price, price_decimals)
            )
            if conservative_extreme_buy1 and price_unchanged:
                self.entry_chase_count = 1
                self.entry_order_time = datetime.now()
                self.warrant_tick_size = snapshot["price_spread"]
                self._save_runtime_state()
                await self._emit_trade_record(TradeRecord(
                    time=current_time,
                    signal=TradeSignal.ENTRY_CHASING,
                    price=hsi_price,
                    rsi=round(rsi, 2),
                    position=PositionType.NONE,
                    message=(
                        f"极度买入未成交，未追一格，继续挂 buy1: {self.current_warrant_code} "
                        f"@ {new_price:.3f} | {self._format_entry_book(snapshot)} "
                        f"| 原因:{chase_reason}"
                    ),
                ))
                return
            if self.entry_mode == MOMENTUM_ENTRY_MODE and price_unchanged:
                if elapsed < self.entry_order_wait_seconds * 2:
                    return
                result = self.trader.cancel_order(self.pending_buy_order_id)
                await self._emit_trade_record(TradeRecord(
                    time=current_time,
                    signal=TradeSignal.ENTRY_PENDING,
                    price=hsi_price,
                    rsi=round(rsi, 2),
                    position=PositionType.NONE,
                    message=(
                        f"放量动能买入未成交，最新 buy1 未变，不追价并取消今轮信号: "
                        f"{self.current_warrant_code} @ {new_price:.3f} "
                        f"| order_id:{self.pending_buy_order_id} | {result.get('message')}"
                    ),
                ))
                self._reset_order_state()
                self._save_runtime_state()
                return

            result = self.trader.modify_order(
                self.pending_buy_order_id,
                new_price,
                self.share_count,
            )
            if result.get("success"):
                self.entry_chase_count = 1
                self.entry_order_time = datetime.now()
                self.warrant_tick_size = snapshot["price_spread"]
                self._save_runtime_state()
                if conservative_extreme_buy1:
                    message = (
                        f"极度买入未成交，未追一格，已改挂最新 buy1: {self.current_warrant_code} "
                        f"{current_order_price:.3f}->{new_price:.3f} "
                        f"| {self._format_entry_book(snapshot)} | 原因:{chase_reason}"
                    )
                else:
                    message = (
                        f"买入未成交，已追价一次: {self.current_warrant_code} "
                        f"{current_order_price:.3f}->{new_price:.3f} "
                        f"| {self._format_entry_book(snapshot)} | 原因:{chase_reason}"
                    )
                await self._emit_trade_record(TradeRecord(
                    time=current_time,
                    signal=TradeSignal.ENTRY_CHASING,
                    price=hsi_price,
                    rsi=round(rsi, 2),
                    position=PositionType.NONE,
                    message=message,
                ))
            return

        result = self.trader.cancel_order(self.pending_buy_order_id)
        await self._emit_trade_record(TradeRecord(
            time=current_time,
            signal=TradeSignal.ENTRY_CHASING,
            price=hsi_price,
            rsi=round(rsi, 2),
            position=PositionType.NONE,
            message=(
                f"买入追价后仍未全数成交，已取消今轮信号: "
                f"order_id:{self.pending_buy_order_id} | {result.get('message')}"
            ),
        ))
        self._reset_order_state()
        self._save_runtime_state()

    async def _monitor_exit_order(self, current_time: str, hsi_price: float, rsi: float):
        if self.position == PositionType.NONE:
            return
        if not self.exit_order_id:
            if self.stop_loss_order_sent:
                await self._chase_stop_loss_exit_order(current_time, hsi_price, rsi, None)
            return

        order = self.trader.get_order(self.exit_order_id)
        if order is None:
            if self.stop_loss_order_sent:
                await self._chase_stop_loss_exit_order(current_time, hsi_price, rsi, None)
            return
        if not _is_filled_all(order["order_status"]):
            if self.stop_loss_order_sent:
                await self._chase_stop_loss_exit_order(current_time, hsi_price, rsi, order)
            return

        fill_hsi_price = self._get_live_hsi_price(hsi_price)
        await self._finalize_exit_fill(order, fill_hsi_price, rsi)

    def _get_stop_exit_price(self, code: str) -> tuple[float | None, str]:
        snapshot = self.data_source.get_security_snapshot(code)
        if snapshot is None:
            return None, f"{code} buy1 无效，未能改卖单"
        tick_size = float(snapshot.get("price_spread", 0.0) or 0.0)
        if tick_size > 0:
            self.warrant_tick_size = tick_size
        stop_price = snapshot.get("bid_price")
        if stop_price is None or stop_price <= 0:
            return None, f"{code} buy1 无效: {stop_price}"
        return float(stop_price), ""

    def _extreme_rsi_allows_stop_veto(self, rsi: float) -> bool:
        if not self.extreme_rsi_stop_veto_enabled:
            return False
        if self.position == PositionType.BULL:
            return rsi <= self.rsi_oversold
        if self.position == PositionType.BEAR:
            return rsi >= self.rsi_overbought
        return False

    def _set_extreme_rsi_stop_veto(self, stop_price: float, rsi: float):
        tick_size = self.warrant_tick_size
        hard_price = max(
            tick_size,
            _round_to_tick(
                stop_price - self.extreme_rsi_stop_hard_ticks * tick_size,
                tick_size,
            ),
        )
        rearm_price = _round_to_tick(
            stop_price + self.extreme_rsi_stop_rearm_ticks * tick_size,
            tick_size,
        )
        self.extreme_rsi_stop_veto_active = True
        self.extreme_rsi_stop_veto_price = stop_price
        self.extreme_rsi_stop_hard_price = hard_price
        self.extreme_rsi_stop_rearm_price = rearm_price
        self.extreme_rsi_stop_veto_rsi = float(rsi)
        self.extreme_rsi_stop_veto_count += 1
        self._save_runtime_state()

    async def _emit_extreme_rsi_stop_veto(
        self,
        current_time: str,
        hsi_price: float,
        rsi: float,
        diff: float,
        actual_pnl: float,
        active_stop_points: float,
    ):
        await self._emit_trade_record(TradeRecord(
            time=current_time,
            signal=TradeSignal.STOP_LOSS_PENDING,
            price=hsi_price,
            rsi=round(rsi, 2),
            position=self.position,
            pnl=round(diff, 2),
            pnl_hkd=round(actual_pnl, 2),
            message=(
                f"极端RSI取消本次普通止损: {self.current_warrant_code} "
                f"原止损@{self.extreme_rsi_stop_veto_price:.3f} "
                f"硬止损@{self.extreme_rsi_stop_hard_price:.3f} "
                f"重新武装@{self.extreme_rsi_stop_rearm_price:.3f} | "
                f"RSI:{rsi:.2f} 阈值:{self.rsi_oversold}/{self.rsi_overbought} | "
                f"止损点数:{active_stop_points:.1f}"
            ),
        ))

    async def _place_stop_loss_exit_order(
        self,
        current_time: str,
        hsi_price: float,
        rsi: float,
        diff: float,
        actual_pnl: float,
        stop_price: float,
        message_prefix: str,
        extra_text: str,
    ):
        if self.exit_order_id:
            result = self.trader.modify_order(self.exit_order_id, stop_price, self.warrant_qty or self.share_count)
            action = "改价到 buy1"
        else:
            result = self.trader.place_order(
                self.current_warrant_code,
                stop_price,
                int(self.warrant_qty or self.share_count),
                "SELL",
            )
            action = "补挂 buy1 卖单"
            if result.get("success"):
                self.exit_order_id = result["order_id"]

        if result.get("success"):
            self.stop_loss_order_sent = True
            self.warrant_exit_price = stop_price
            self._reset_extreme_rsi_stop_veto(reset_count=False)
            self._save_runtime_state()
        await self._emit_trade_record(TradeRecord(
            time=current_time,
            signal=TradeSignal.STOP_LOSS_PENDING,
            price=hsi_price,
            rsi=round(rsi, 2),
            position=self.position,
            pnl=round(diff, 2),
            pnl_hkd=round(actual_pnl, 2),
            message=(
                f"{message_prefix}，{action}: {self.current_warrant_code} "
                f"@ {stop_price:.3f} | {extra_text} | {result.get('message')}"
            ),
        ))

    async def _handle_extreme_rsi_vetoed_stop(
        self,
        current_time: str,
        hsi_price: float,
        rsi: float,
        diff: float,
        actual_pnl: float,
    ) -> bool:
        stop_price, reason = self._get_stop_exit_price(self.current_warrant_code)
        if stop_price is None:
            await self._emit_stop_chase_failure(current_time, hsi_price, rsi, reason)
            return True

        if stop_price <= self.extreme_rsi_stop_hard_price:
            await self._place_stop_loss_exit_order(
                current_time,
                hsi_price,
                rsi,
                diff,
                actual_pnl,
                stop_price,
                "RSI取消后硬止损触发",
                (
                    f"当前buy1已到硬止损 "
                    f"{stop_price:.3f}<={self.extreme_rsi_stop_hard_price:.3f}，不再判断RSI"
                ),
            )
            return True

        if stop_price >= self.extreme_rsi_stop_rearm_price:
            veto_price = self.extreme_rsi_stop_veto_price
            rearm_price = self.extreme_rsi_stop_rearm_price
            self._reset_extreme_rsi_stop_veto(reset_count=False)
            self._save_runtime_state()
            await self._emit_trade_record(TradeRecord(
                time=current_time,
                signal=TradeSignal.HOLD,
                price=hsi_price,
                rsi=round(rsi, 2),
                position=self.position,
                pnl=round(diff, 2),
                pnl_hkd=round(actual_pnl, 2),
                message=(
                    f"RSI止损取消已重新武装: {self.current_warrant_code} "
                    f"buy1 {stop_price:.3f}>={rearm_price:.3f}，"
                    f"下次再触发 {veto_price:.3f} 附近止损时重新判断RSI"
                ),
            ))
            return True
        return True

    def _is_terminal_unfilled_exit_status(self, status: str) -> bool:
        return _order_status_name(status) in TERMINAL_UNFILLED_EXIT_STATUSES

    def _remaining_exit_qty(self, order: dict | None) -> int:
        dealt_qty = float(order.get("dealt_qty", 0.0)) if order else 0.0
        base_qty = float(self.warrant_qty or self.share_count)
        return max(int(round(base_qty - dealt_qty)), 0)

    async def _mark_exit_complete_from_order(self, order: dict, current_time: str, hsi_price: float, rsi: float):
        fill_hsi_price = self._get_live_hsi_price(hsi_price)
        complete_order = dict(order)
        if not complete_order.get("dealt_avg_price"):
            complete_order["dealt_avg_price"] = self.warrant_exit_price
        if not complete_order.get("dealt_qty"):
            complete_order["dealt_qty"] = self.warrant_qty
        await self._finalize_exit_fill(complete_order, fill_hsi_price, rsi)

    async def _emit_stop_chase_failure(
        self,
        current_time: str,
        hsi_price: float,
        rsi: float,
        reason: str,
        order: dict | None = None,
    ):
        dealt_qty = float(order.get("dealt_qty", 0.0)) if order else 0.0
        remain_qty = self._remaining_exit_qty(order)
        log_key = (
            f"stop_chase_failure:{self.current_warrant_code}:{self.exit_order_id}:"
            f"{reason}:{dealt_qty:.0f}:{remain_qty}"
        )
        if log_key == self.last_stop_loss_failure_log_key:
            return
        self.last_stop_loss_failure_log_key = log_key
        await self._emit_trade_record(TradeRecord(
            time=current_time,
            signal=TradeSignal.STOP_LOSS_CHASING,
            price=hsi_price,
            rsi=round(rsi, 2),
            position=self.position,
            message=(
                f"止损卖单追价失败: {reason} | order_id:{self.exit_order_id or '-'} "
                f"| 已成交:{dealt_qty:.0f} 剩余:{remain_qty}"
            ),
        ))

    async def _chase_stop_loss_exit_order(
        self,
        current_time: str,
        hsi_price: float,
        rsi: float,
        order: dict | None,
    ):
        if self.position == PositionType.NONE or not self.current_warrant_code:
            return

        stop_price, reason = self._get_stop_exit_price(self.current_warrant_code)
        if stop_price is None:
            await self._emit_stop_chase_failure(current_time, hsi_price, rsi, reason, order)
            return
        self.last_stop_loss_failure_log_key = ""

        if order is not None and self._is_terminal_unfilled_exit_status(order.get("order_status", "")):
            remain_qty = self._remaining_exit_qty(order)
            if remain_qty <= 0:
                await self._mark_exit_complete_from_order(order, current_time, hsi_price, rsi)
                return
            result = self.trader.place_order(
                self.current_warrant_code,
                stop_price,
                remain_qty,
                "SELL",
            )
            if result.get("success"):
                self.exit_order_id = result["order_id"]
                self.warrant_exit_price = stop_price
                self._save_runtime_state()
            await self._emit_trade_record(TradeRecord(
                time=current_time,
                signal=TradeSignal.STOP_LOSS_CHASING,
                price=hsi_price,
                rsi=round(rsi, 2),
                position=self.position,
                message=(
                    f"止损卖单失效，已按最新 buy1 补挂: {self.current_warrant_code} "
                    f"x{remain_qty} @ {stop_price:.3f} | order_id:{self.exit_order_id or '-'} "
                    f"| 已成交:{order.get('dealt_qty', 0):.0f} 剩余:{remain_qty} | {result.get('message')}"
                ),
            ))
            return

        target_qty = float(order.get("qty", self.warrant_qty or self.share_count)) if order else float(self.warrant_qty or self.share_count)
        if not self.exit_order_id:
            result = self.trader.place_order(
                self.current_warrant_code,
                stop_price,
                int(target_qty),
                "SELL",
            )
            action = "补挂最新 buy1 止损卖单"
            if result.get("success"):
                self.exit_order_id = result["order_id"]
        else:
            result = self.trader.modify_order(self.exit_order_id, stop_price, target_qty)
            action = "追价到最新 buy1"

        if result.get("success"):
            self.warrant_exit_price = stop_price
            self.last_stop_loss_failure_log_key = ""
            self._save_runtime_state()
        dealt_qty = float(order.get("dealt_qty", 0.0)) if order else 0.0
        remain_qty = self._remaining_exit_qty(order)
        await self._emit_trade_record(TradeRecord(
            time=current_time,
            signal=TradeSignal.STOP_LOSS_CHASING,
            price=hsi_price,
            rsi=round(rsi, 2),
            position=self.position,
            message=(
                f"止损卖单未成交，{action}: {self.current_warrant_code} "
                f"@ {stop_price:.3f} | order_id:{self.exit_order_id or '-'} "
                f"| 已成交:{dealt_qty:.0f} 剩余:{remain_qty} | {result.get('message')}"
            ),
        ))

    async def _handle_stop_loss(
        self,
        current_time: str,
        hsi_price: float,
        rsi: float,
        diff: float,
        actual_pnl: float,
        active_stop_points: float,
    ):
        if self.stop_loss_order_sent:
            order = self.trader.get_order(self.exit_order_id) if self.exit_order_id else None
            await self._chase_stop_loss_exit_order(current_time, hsi_price, rsi, order)
            return
        if not self.current_warrant_code:
            return
        if self.extreme_rsi_stop_veto_active:
            await self._handle_extreme_rsi_vetoed_stop(
                current_time, hsi_price, rsi, diff, actual_pnl
            )
            return

        stop_price, reason = self._get_stop_exit_price(self.current_warrant_code)
        if stop_price is None:
            log_key = (
                f"stop_pending_failure:{self.current_warrant_code}:{self.entry_mode}:"
                f"{reason}:{active_stop_points:.1f}"
            )
            if log_key == self.last_stop_loss_failure_log_key:
                return
            self.last_stop_loss_failure_log_key = log_key
            await self._emit_trade_record(TradeRecord(
                time=current_time,
                signal=TradeSignal.STOP_LOSS_PENDING,
                price=hsi_price,
                rsi=round(rsi, 2),
                position=self.position,
                pnl=round(diff, 2),
                pnl_hkd=round(actual_pnl, 2),
                message=(
                    f"止损触发，但 {reason} | "
                    f"入场模式:{self.entry_mode or '-'} | 阈值:{active_stop_points:.1f}点"
                ),
            ))
            return
        self.last_stop_loss_failure_log_key = ""

        if self.warrant_tick_size > 0 and self._extreme_rsi_allows_stop_veto(rsi):
            self._set_extreme_rsi_stop_veto(stop_price, rsi)
            await self._emit_extreme_rsi_stop_veto(
                current_time, hsi_price, rsi, diff, actual_pnl, active_stop_points
            )
            return

        await self._place_stop_loss_exit_order(
            current_time,
            hsi_price,
            rsi,
            diff,
            actual_pnl,
            stop_price,
            "止损触发",
            (
                f"入场模式:{self.entry_mode or '-'} | "
                f"阈值:{active_stop_points:.1f}点"
            ),
        )

    def _momentum_fail_fast_reason(self, hsi_price: float, rsi: float) -> str:
        if (
            self.entry_mode != MOMENTUM_ENTRY_MODE
            or self.position == PositionType.NONE
            or self.entry_fill_time is None
            or self.momentum_fail_fast_sent
            or self.stop_loss_order_sent
        ):
            return ""

        elapsed = (datetime.now() - self.entry_fill_time).total_seconds()
        if elapsed < MOMENTUM_ENTRY_FAIL_FAST_SECONDS:
            return ""

        if self.position == PositionType.BULL:
            adverse_move = self.entry_price - hsi_price
            if adverse_move >= MOMENTUM_ENTRY_FAIL_FAST_POINTS and rsi <= MOMENTUM_ENTRY_FAIL_FAST_RSI:
                return (
                    f"成交後{elapsed:.0f}秒牛證動能失敗: "
                    f"回落{adverse_move:.1f}點 RSI:{rsi:.2f}"
                )
        elif self.position == PositionType.BEAR:
            adverse_move = hsi_price - self.entry_price
            if adverse_move >= MOMENTUM_ENTRY_FAIL_FAST_POINTS and rsi >= 100.0 - MOMENTUM_ENTRY_FAIL_FAST_RSI:
                return (
                    f"成交後{elapsed:.0f}秒熊證動能失敗: "
                    f"反彈{adverse_move:.1f}點 RSI:{rsi:.2f}"
                )
        return ""

    async def _handle_momentum_fail_fast_exit(
        self,
        current_time: str,
        hsi_price: float,
        rsi: float,
        diff: float,
        actual_pnl: float,
        reason_text: str,
    ):
        if not self.current_warrant_code:
            return

        stop_price, reason = self._get_stop_exit_price(self.current_warrant_code)
        if stop_price is None:
            await self._emit_trade_record(TradeRecord(
                time=current_time,
                signal=TradeSignal.STOP_LOSS_PENDING,
                price=hsi_price,
                rsi=round(rsi, 2),
                position=self.position,
                pnl=round(diff, 2),
                pnl_hkd=round(actual_pnl, 2),
                message=f"放量动能失败早退触发，但 {reason} | {reason_text}",
            ))
            return

        if self.exit_order_id:
            result = self.trader.modify_order(self.exit_order_id, stop_price, self.warrant_qty or self.share_count)
            action = "改价到 buy1"
        else:
            result = self.trader.place_order(
                self.current_warrant_code,
                stop_price,
                int(self.warrant_qty or self.share_count),
                "SELL",
            )
            action = "补挂 buy1 卖单"
            if result.get("success"):
                self.exit_order_id = result["order_id"]

        if result.get("success"):
            self.stop_loss_order_sent = True
            self.momentum_fail_fast_sent = True
            self.warrant_exit_price = stop_price
            self._save_runtime_state()
        await self._emit_trade_record(TradeRecord(
            time=current_time,
            signal=TradeSignal.STOP_LOSS_PENDING,
            price=hsi_price,
            rsi=round(rsi, 2),
            position=self.position,
            pnl=round(diff, 2),
            pnl_hkd=round(actual_pnl, 2),
            message=(
                f"放量动能失败早退，{action}: {self.current_warrant_code} "
                f"@ {stop_price:.3f} | {reason_text} | {result.get('message')}"
            ),
        ))

    # ================================================================
    #  实时报价推送回调 (由 OpenD 主动推送，价格一变就触发)
    # ================================================================
    #  实时报价推送 → 状态面板价格跳动
    # ================================================================
    def _on_price_push(self, price_data: dict):
        self.current_price = price_data["last_price"]
        if self._loop and self.on_state_update:
            asyncio.run_coroutine_threadsafe(
                self.on_state_update(self.get_state(sync_exit=False)),
                self._loop,
            )

    # ================================================================
    #  记录涨跌家数比到日志文件 (用于后续分析)
    # ================================================================
    def _log_market_state(self, breadth: dict):
        import csv, os
        filepath = os.path.join(os.path.dirname(__file__), "market_log.csv")
        exists = os.path.exists(filepath)
        with open(filepath, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not exists:
                writer.writerow([
                    "time", "price", "raise_count", "fall_count", "ratio",
                    "amplitude", "position", "rsi",
                ])
            ratio = breadth["raise_count"] / max(breadth["fall_count"], 1)
            writer.writerow([
                breadth["time"], round(self.current_price, 2),
                breadth["raise_count"], breadth["fall_count"],
                round(ratio, 3), breadth["amplitude"],
                self.position.value, self._last_rsi or "",
            ])

    # ================================================================
    #  策略研判：1M 主信号 + 15M 跨周期确认
    #  同时推送完整 K 线批次给前端图表
    # ================================================================
    async def run_strategy_check(self):
        loop = asyncio.get_event_loop()

        # 拉取 1 分钟 K 线 (主信号: RSI / 成交额 / VWAP / K线形态)
        df_1m = await loop.run_in_executor(
            None, self.data_source.get_kline_with_indicators, "1m", self.rsi_length
        )
        # 拉取 15 分钟 K 线 (跨周期确认)
        df_15m = await loop.run_in_executor(
            None, self.data_source.get_kline_with_indicators, "15m", self.rsi_length
        )

        # 拉取涨跌家数比 (市场情绪指标，用于后续分析)
        snapshot = await loop.run_in_executor(
            None, self.data_source.get_market_breadth
        )
        if snapshot:
            self.latest_breadth = snapshot
            self._log_market_state(snapshot)

        if df_1m is None or df_15m is None or len(df_1m) < 20 or len(df_15m) < 2:
            print("[Strategy] 数据不足 (1M:%s 15M:%s)，跳过" % (
                len(df_1m) if df_1m is not None else 0,
                len(df_15m) if df_15m is not None else 0))
            return

        curr_1m = df_1m.iloc[-1]
        prev_1m = df_1m.iloc[-2]
        curr_15m = df_15m.iloc[-1]

        price = curr_1m["close"]
        rsi = curr_1m["RSI"]
        vol = curr_1m["volume"]       # turnover (成交额)
        vol_ma = curr_1m["VOL_MA"]    # 20 周期成交额均值

        self.current_price = price

        # 缓存指标
        self._last_rsi = round(rsi, 2) if not _is_nan(rsi) else None
        self._last_vwap = round(curr_1m["VWAP"], 2) if not _is_nan(curr_1m["VWAP"]) else None
        self._last_vwap_slope = round(curr_1m["VWAP_SLOPE"], 4) if not _is_nan(curr_1m["VWAP_SLOPE"]) else None
        self._last_vol_ma = round(vol_ma, 2) if not _is_nan(vol_ma) else None

        # ---- 推送 1M K 线批次给前端图表 ----
        kline_batch = []
        for idx in range(max(0, len(df_1m) - 80), len(df_1m)):
            row = df_1m.iloc[idx]
            t = df_1m.index[idx].strftime("%Y-%m-%d %H:%M:%S")
            kline_batch.append(KlineData(
                time=t,
                open=round(row["open"], 2),
                high=round(row["high"], 2),
                low=round(row["low"], 2),
                close=round(row["close"], 2),
                volume=row["volume"],
                rsi=round(row["RSI"], 2) if not _is_nan(row["RSI"]) else None,
                vwap=round(row["VWAP"], 2) if not _is_nan(row["VWAP"]) else None,
                vwap_slope=round(row["VWAP_SLOPE"], 4) if not _is_nan(row["VWAP_SLOPE"]) else None,
                vol_ma=round(row["VOL_MA"], 2) if not _is_nan(row["VOL_MA"]) else None,
            ))
        self.kline_history_1m = kline_batch
        if self.on_kline_batch:
            await self.on_kline_batch(kline_batch)

        market_regime_kline_time = df_1m.index[-1].strftime("%Y-%m-%d %H:%M:%S")
        if market_regime_kline_time != self._last_market_regime_kline_time:
            self._last_market_regime_kline_time = market_regime_kline_time
            self.market_regime = classify_market_regime(df_1m, snapshot)
            if self.on_market_regime_update:
                await self.on_market_regime_update(self.market_regime)

        # 推送状态
        if self.on_state_update:
            await self.on_state_update(self.get_state())

        if _is_nan(rsi) or _is_nan(vol_ma):
            return

        # ============== 核心信号判定 (基于 1M K 线) ==============

        # 1. VWAP 斜率拐头
        curr_slope = curr_1m["VWAP_SLOPE"]
        prev_slope = prev_1m["VWAP_SLOPE"] if not _is_nan(prev_1m["VWAP_SLOPE"]) else 0
        vwap_turning_up = (prev_slope <= 0 and curr_slope > 0) or (curr_slope > 0 and curr_slope > prev_slope)
        vwap_turning_down = (prev_slope >= 0 and curr_slope < 0) or (curr_slope < 0 and curr_slope < prev_slope)

        # 2. 成交额放量：当前 1M 成交额 > 20 周期均值
        vol_is_high = vol > vol_ma

        # 3. 1M K 线形态
        k_open = curr_1m["open"]
        k_close = curr_1m["close"]
        k_high = curr_1m["high"]
        k_low = curr_1m["low"]
        k_body = abs(k_close - k_open)

        k_is_green = k_close > k_open
        lower_shadow = min(k_open, k_close) - k_low
        k_has_lower_shadow = lower_shadow > k_body * 1.0
        k_bull_pattern = k_is_green or k_has_lower_shadow  # 阳线或下影线

        k_is_red = k_close < k_open
        upper_shadow = k_high - max(k_open, k_close)
        k_has_upper_shadow = upper_shadow > k_body * 1.0
        k_bear_pattern = k_is_red or k_has_upper_shadow  # 阴线或上影线

        # 4. 15M 跨周期确认
        m15_is_green = curr_15m["close"] > curr_15m["open"]
        m15_is_red = curr_15m["close"] < curr_15m["open"]
        m15_change = curr_15m["close"] - curr_15m["open"]

        # 5. 累积涨跌幅。日志保留实时值；累积趋势入场另用完成K线计算。
        cum5 = 0.0
        if len(df_1m) >= 6:
            recent_closes = [df_1m.iloc[j]["close"] for j in range(len(df_1m)-6, len(df_1m))]
            cum5 = recent_closes[-1] - recent_closes[0]
        completed_cum5 = self._completed_cum5(df_1m)

        breadth_ratio = None
        if snapshot:
            breadth_ratio = snapshot["raise_count"] / max(snapshot["fall_count"], 1)

        current_time = df_1m.index[-1].strftime("%Y-%m-%d %H:%M:%S")
        vwap_status = "拐↑" if vwap_turning_up else ("拐↓" if vwap_turning_down else "平")

        await self._monitor_entry_order(current_time, price, rsi)
        await self._monitor_exit_order(current_time, price, rsi)

        if self._is_after_force_exit_time(current_time):
            await self._cancel_pending_entry_after_cutoff(current_time, price, rsi)
            await self._force_exit_position_after_cutoff(current_time, price, rsi)
            if self.on_state_update:
                await self.on_state_update(self.get_state())
            return

        print(f"[{current_time}] 价格:{price:.2f} RSI:{rsi:.2f} "
              f"VWAP:{vwap_status}({curr_slope:.2f}/{prev_slope:.2f}) "
              f"放量:{'是' if vol_is_high else '否'} "
              f"1M:{'阳' if k_is_green else '阴'}{'↓影' if k_has_lower_shadow else ''}{'↑影' if k_has_upper_shadow else ''} "
              f"15M:{'阳' if m15_is_green else '阴'} "
              f"累5:{cum5:+.1f} "
              f"仓位:{self.position.value}")

        # ============== 交易执行 (分级 RSI) ==============
        # 过滤开盘前5分钟 (集合竞价放量不是真信号)
        t_str = df_1m.index[-1].strftime("%H:%M")
        in_open_filter = t_str < "09:35" or ("13:00" <= t_str < "13:05")
        if in_open_filter:
            if self.on_state_update:
                await self.on_state_update(self.get_state())
            return

        if self._is_after_entry_cutoff(current_time):
            await self._cancel_pending_entry_after_cutoff(current_time, price, rsi)
            if self.on_state_update:
                await self.on_state_update(self.get_state())
            return

        if await self._cancel_degraded_momentum_entry(current_time, price, rsi, curr_slope):
            if self.on_state_update:
                await self.on_state_update(self.get_state())
            return

        if await self._cancel_degraded_cum_trend_entry(current_time, price, rsi, curr_slope, completed_cum5):
            if self.on_state_update:
                await self.on_state_update(self.get_state())
            return

        if self.position == PositionType.NONE:
            # --- 牛证 ---
            # 普通超卖 (18 <= RSI < 25): 需要全部5个条件
            bull_normal = (self.rsi_oversold <= rsi < 25
                           and vwap_turning_up and vol_is_high
                           and k_bull_pattern and m15_is_green)

            if bull_normal:
                await self._submit_entry_order(
                    PositionType.BULL, price, rsi, current_time, "普通超卖",
                    extra_message=f"HSI:{price:.2f} RSI:{rsi:.2f}",
                )

            # --- 熊证 ---
            # 普通超买 (75 < RSI <= 85): 需要全部5个条件
            elif rsi > 75:
                bear_normal = (75 < rsi <= self.rsi_overbought
                               and vwap_turning_down and vol_is_high
                               and k_bear_pattern and m15_is_red)

                if bear_normal:
                    await self._submit_entry_order(
                        PositionType.BEAR, price, rsi, current_time, "普通超买",
                        extra_message=f"HSI:{price:.2f} RSI:{rsi:.2f}",
                    )

            # --- 放量动能信号 ---
            # 先命中放量动能；若 RSI 到达极度阈值，优先走极度反转，否则跟随放量方向。
            if self.position == PositionType.NONE and not self.pending_buy_order_id:
                k_change = k_close - k_open
                k_body_points = abs(k_change)
                extreme_vol_surge = vol > vol_ma * EXTREME_VOLUME_SURGE_MULTIPLIER
                required_momentum_ratio = self._required_momentum_volume_multiplier(current_time)
                vol_surge = vol > vol_ma * required_momentum_ratio
                momentum_ratio = vol / vol_ma if vol_ma > 0 else 0.0
                extreme_side, dynamic_low_volume, extreme_trigger_label = _extreme_signal_side(
                    float(rsi),
                    float(momentum_ratio),
                    float(price),
                    float(k_high),
                    float(k_low),
                    self.rsi_oversold,
                    self.rsi_overbought,
                )
                extreme_branch = (
                    "b2_very_extreme_pullback"
                    if dynamic_low_volume and extreme_side != PositionType.NONE
                    else "b1_volume_extreme"
                    if extreme_side != PositionType.NONE
                    else ""
                )
                momentum_body_ok = (
                    MOMENTUM_MIN_K_BODY_POINTS <= k_body_points <= MOMENTUM_MAX_K_BODY_POINTS
                )
                min_body_ok = k_body_points >= MOMENTUM_MIN_K_BODY_POINTS
                extreme_body_ok = min_body_ok

                if (
                    extreme_side != PositionType.NONE
                    and k_change != 0
                    and not extreme_body_ok
                ):
                    print(
                        f"  >>> Skip extreme: k_body={k_body_points:.1f} "
                        f"min={MOMENTUM_MIN_K_BODY_POINTS:.1f}"
                    )

                elif (
                    extreme_side == PositionType.BULL
                    and k_change != 0
                    and extreme_body_ok
                    and self._strategy_enabled("extreme")
                    and self._extreme_branch_enabled(extreme_branch)
                ):
                    await self._submit_entry_order(
                        PositionType.BULL, price, rsi, current_time, "极度超卖",
                        extra_message=(
                            f"{extreme_trigger_label} | RSI:{rsi:.2f} | "
                            f"{'阳线涨' if k_change > 0 else '阴线跌'}{abs(k_change):.1f}点 | "
                            f"{momentum_ratio:.2f}x量"
                        ),
                    )

                elif (
                    extreme_side == PositionType.BEAR
                    and k_change != 0
                    and extreme_body_ok
                    and self._strategy_enabled("extreme")
                    and self._extreme_branch_enabled(extreme_branch)
                ):
                    await self._submit_entry_order(
                        PositionType.BEAR, price, rsi, current_time, "极度超买",
                        extra_message=(
                            f"{extreme_trigger_label} | RSI:{rsi:.2f} | "
                            f"{'阳线涨' if k_change > 0 else '阴线跌'}{abs(k_change):.1f}点 | "
                            f"{momentum_ratio:.2f}x量"
                        ),
                    )

                elif vol_surge and k_change != 0 and not momentum_body_ok and self._strategy_enabled("momentum"):
                    print(
                        f"  >>> Skip momentum: k_body={k_body_points:.1f} "
                        f"range={MOMENTUM_MIN_K_BODY_POINTS:.1f}-{MOMENTUM_MAX_K_BODY_POINTS:.1f}"
                    )

                elif vol_surge and k_change > 0 and self._strategy_enabled("momentum"):
                    skip_reasons = get_momentum_filter_reasons(
                        "bull", float(rsi), breadth_ratio
                    )
                    if skip_reasons:
                        print(f"  >>> Skip bull momentum: {'; '.join(skip_reasons)}")
                    else:
                        await self._submit_entry_order(
                            PositionType.BULL, price, rsi, current_time, MOMENTUM_ENTRY_MODE,
                            extra_message=(
                                f"阳线涨{k_change:.1f}点 | {momentum_ratio:.1f}x量"
                            ),
                        )

                elif vol_surge and k_change < 0 and self._strategy_enabled("momentum"):
                    skip_reasons = get_momentum_filter_reasons(
                        "bear", float(rsi), breadth_ratio
                    )
                    if skip_reasons:
                        print(f"  >>> Skip bear momentum: {'; '.join(skip_reasons)}")
                    else:
                        await self._submit_entry_order(
                            PositionType.BEAR, price, rsi, current_time, MOMENTUM_ENTRY_MODE,
                            extra_message=(
                                f"阴线跌{abs(k_change):.1f}点 | {momentum_ratio:.1f}x量"
                            ),
                        )

            if (
                self.position == PositionType.NONE
                and not self.pending_buy_order_id
                and self._strategy_enabled("rsi_divergence")
            ):
                divergence_signal = self._detect_rsi_divergence_signal(df_1m)
                if divergence_signal is not None:
                    divergence_side, divergence_branch, divergence_message = divergence_signal
                    await self._submit_entry_order(
                        divergence_side,
                        price,
                        rsi,
                        current_time,
                        RSI_DIVERGENCE_ENTRY_MODE,
                        extra_message=f"{divergence_branch} | {divergence_message}",
                    )

            if self.position == PositionType.NONE and not self.pending_buy_order_id:
                completed_kline_time = df_1m.index[-2].strftime("%Y-%m-%d %H:%M:%S")
                if completed_kline_time != self.last_completed_extreme_kline_time:
                    completed_side, completed_message = self._completed_extreme_signal(
                        prev_1m, float(price), float(rsi)
                    )
                    completed_branch = self._completed_extreme_branch_from_message(completed_message)
                    if (
                        completed_side != PositionType.NONE
                        and self._strategy_enabled("extreme")
                        and self._extreme_branch_enabled(completed_branch)
                    ):
                        self.last_completed_extreme_kline_time = completed_kline_time
                        self._save_runtime_state()
                        completed_mode = (
                            VERY_EXTREME_SHADOW_BULL_ENTRY_MODE
                            if "非常极端下影反抽" in completed_message
                            else (
                                VERY_EXTREME_SHADOW_BEAR_ENTRY_MODE
                                if "非常极端上影回落" in completed_message
                                else ("极度超卖" if completed_side == PositionType.BULL else "极度超买")
                            )
                        )
                        await self._submit_entry_order(
                            completed_side,
                            price,
                            rsi,
                            completed_kline_time,
                            completed_mode,
                            extra_message=completed_message,
                        )

            # --- 累积趋势信号 (温水煮青蛙式单边) ---
            # 最近5根1M累积跌/涨 > 30点 + VWAP方向一致
            # 过滤条件：
            #   1. 日内区间 >= 100点 (确认有波动)
            #   2. 信号方向必须和开盘以来的整体方向一致
            #   3. 涨跌家数比必须支持信号方向
            #   4. 30-40点边界单必须有额外延续确认
            if (
                self.position == PositionType.NONE
                and self._strategy_enabled("cum_trend")
                and abs(completed_cum5) > CUM_TREND_BOUNDARY_POINTS
            ):
                day_open = float(snapshot.get("open_price", 0)) if snapshot else 0.0
                day_high = float(snapshot.get("high_price", 0)) if snapshot else 0.0
                day_low = float(snapshot.get("low_price", 0)) if snapshot else 0.0
                if day_open <= 0 or day_high <= 0 or day_low <= 0 or day_high < day_low:
                    day_open = float(df_1m.iloc[0]["open"])
                    day_high = float(df_1m["high"].max())
                    day_low = float(df_1m["low"].min())
                day_range = day_high - day_low
                day_trend = price - day_open
                completed_window = df_1m.iloc[:-1].tail(6)
                recent_low = float(completed_window["low"].min()) if len(completed_window) >= 6 else None
                recent_high = float(completed_window["high"].max()) if len(completed_window) >= 6 else None
                prev_close = float(prev_1m["close"])
                signal_kline_time = df_1m.index[-2].strftime("%Y-%m-%d %H:%M:%S")
                completed_rsi = float(prev_1m["RSI"])

                if day_range >= 100:
                    # 做空信号：累积跌 + 当天整体也在跌
                    if completed_cum5 < -CUM_TREND_BOUNDARY_POINTS and day_trend < 0:
                        if completed_rsi < self.rsi_oversold + CUM_TREND_RSI_BUFFER:
                            print(
                                f"  >>> Skip bear cumtrend: RSI extreme oversold "
                                f"rsi={completed_rsi:.2f} threshold={self.rsi_oversold + CUM_TREND_RSI_BUFFER:.2f}"
                            )
                        else:
                            side = PositionType.BEAR
                            skip_reasons = get_cum_trend_filter_reasons("bear", breadth_ratio)
                            skip_reasons.extend(
                                get_cum_trend_boundary_filter_reasons(
                                    "bear", float(completed_cum5), float(completed_rsi), prev_close,
                                    float(price), recent_low, recent_high,
                                    float(self.rsi_oversold), float(self.rsi_overbought),
                                )
                            )
                            skip_reasons.extend(
                                self._cum_trend_entry_block_reasons(
                                    side, signal_kline_time, completed_rsi, df_1m
                                )
                            )
                            if skip_reasons:
                                print(f"  >>> Skip bear cumtrend: {'; '.join(skip_reasons)}")
                            else:
                                blocked, remaining = self._is_blocked_by_extreme_stop_reversal_guard(
                                    "bear", current_time
                                )
                                if blocked:
                                    await self._emit_reversal_guard_skip(
                                        current_time, price, rsi, "bear", remaining
                                    )
                                    return
                                ratio = breadth_ratio
                                submitted = await self._submit_entry_order(
                                    side, price, completed_rsi, signal_kline_time, "累积趋势",
                                    extra_message=(
                                        f"5根累跌{completed_cum5:.1f}点 | "
                                        f"日内{day_range:.0f}点 | R:{ratio:.2f}"
                                    ),
                                )
                                if submitted:
                                    self._mark_cum_trend_signal_submitted(signal_kline_time, side)

                    # 做多信号：累积涨 + 当天整体也在涨
                    elif completed_cum5 > CUM_TREND_BOUNDARY_POINTS and day_trend > 0:
                        if completed_rsi > self.rsi_overbought - CUM_TREND_RSI_BUFFER:
                            print(
                                f"  >>> Skip bull cumtrend: RSI extreme overbought "
                                f"rsi={completed_rsi:.2f} threshold={self.rsi_overbought - CUM_TREND_RSI_BUFFER:.2f}"
                            )
                        else:
                            side = PositionType.BULL
                            skip_reasons = get_cum_trend_filter_reasons("bull", breadth_ratio)
                            skip_reasons.extend(
                                get_cum_trend_boundary_filter_reasons(
                                    "bull", float(completed_cum5), float(completed_rsi), prev_close,
                                    float(price), recent_low, recent_high,
                                    float(self.rsi_oversold), float(self.rsi_overbought),
                                )
                            )
                            skip_reasons.extend(
                                self._cum_trend_entry_block_reasons(
                                    side, signal_kline_time, completed_rsi, df_1m
                                )
                            )
                            if skip_reasons:
                                print(f"  >>> Skip bull cumtrend: {'; '.join(skip_reasons)}")
                            else:
                                blocked, remaining = self._is_blocked_by_extreme_stop_reversal_guard(
                                    "bull", current_time
                                )
                                if blocked:
                                    await self._emit_reversal_guard_skip(
                                        current_time, price, rsi, "bull", remaining
                                    )
                                    return
                                ratio = breadth_ratio
                                submitted = await self._submit_entry_order(
                                    side, price, completed_rsi, signal_kline_time, "累积趋势",
                                    extra_message=(
                                        f"5根累涨{completed_cum5:.1f}点 | "
                                        f"日内{day_range:.0f}点 | R:{ratio:.2f}"
                                    ),
                                )
                                if submitted:
                                    self._mark_cum_trend_signal_submitted(signal_kline_time, side)

        else:
            # 止盈止损
            diff = 0.0
            if self.position == PositionType.BULL:
                diff = price - self.entry_price
            elif self.position == PositionType.BEAR:
                diff = self.entry_price - price
            actual_pnl = (diff / self.er_ratio) * self.share_count

            active_stop_points = self._active_stop_points()
            if self.extreme_rsi_stop_veto_active and not self.stop_loss_order_sent:
                await self._handle_extreme_rsi_vetoed_stop(
                    current_time,
                    price,
                    rsi,
                    diff,
                    actual_pnl,
                )
                return
            fail_fast_reason = self._momentum_fail_fast_reason(price, rsi)
            if fail_fast_reason:
                await self._handle_momentum_fail_fast_exit(
                    current_time,
                    price,
                    rsi,
                    diff,
                    actual_pnl,
                    fail_fast_reason,
                )
                return
            if diff <= -active_stop_points and not self.stop_loss_order_sent:
                await self._handle_stop_loss(current_time, price, rsi, diff, actual_pnl, active_stop_points)

    # ================================================================
    async def start(self):
        if self.is_running:
            return
        connected = self.data_source.connect()
        if not connected:
            raise RuntimeError("无法连接 OpenD，请确认 OpenD 已启动")

        # 绑定实时推送回调 (报价推送驱动价格跳动)
        self.data_source.on_price_push = self._on_price_push
        # 确保订阅
        self.data_source._ensure_subscribed()

        self.trader.connect()
        self.is_running = True
        self._loop = asyncio.get_event_loop()
        # 只启动策略研判任务，价格由推送驱动
        self._strategy_task = asyncio.create_task(self._strategy_loop())

    async def _strategy_loop(self):
        try:
            while self.is_running:
                try:
                    await self.run_strategy_check()
                except Exception as e:
                    print(f"[Strategy] 异常: {e}")
                    import traceback; traceback.print_exc()
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        self.is_running = False
        if self._strategy_task:
            self._strategy_task.cancel()
            try:
                await self._strategy_task
            except asyncio.CancelledError:
                pass
        self._strategy_task = None
        self._loop = None
        self.data_source.on_price_push = None
        self.data_source.disconnect()
        self.trader.disconnect()

    def reset(self):
        self.position = PositionType.NONE
        self.entry_price = 0.0
        self.current_price = 0.0
        self.total_pnl_hkd = 0.0
        self.trade_count = 0
        self.win_count = 0
        self.loss_count = 0
        self.kline_history_1m.clear()
        self._reset_order_state()
        self.latest_breadth = None
        self._last_rsi = None
        self._last_vwap = None
        self._last_vwap_slope = None
        self._last_vol_ma = None
        self.market_regime = None
        self._last_market_regime_kline_time = ""
        self._reset_rsi_divergence_state()
        self.rsi_divergence_day = ""
        self._reset_extreme_stop_reversal_guard()
        self._save_runtime_state()
