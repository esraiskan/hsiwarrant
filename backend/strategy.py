"""
核心策略引擎 - 连接富途 OpenD 实盘版
双频率架构：
  - 快速轮询 (3秒): 拉取实时价格，推送状态
  - 策略研判 (每轮): 1M K线做主信号(RSI/成交额/VWAP/形态) + 15M 跨周期确认
"""
import asyncio
import math
import re
from datetime import datetime
from typing import Callable, Optional

from config import (
    SYMBOL, ER_RATIO, SHARE_COUNT, TARGET_PNL, STOP_POINTS, EXTREME_STOP_PNL,
    BULL_WARRANT_CODE, BEAR_WARRANT_CODE,
    RSI_OVERSOLD, RSI_OVERBOUGHT, POLL_INTERVAL, ENTRY_ORDER_WAIT_SECONDS,
    ENTRY_CUTOFF_TIME,
)
from models import (
    PositionType, TradeSignal, TradeRecord, StrategyState, KlineData,
)
from futu_data import FutuDataSource
from futu_trader import FutuTrader
from runtime_config_store import load_runtime_config, save_runtime_config
from strategy_state_store import load_strategy_state, save_strategy_state
from trade_log_store import append_trade_log, load_trade_log
from momentum_filter import get_momentum_filter_reasons
from trend_filter import get_cum_trend_filter_reasons


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


def _is_filled_all(status: str) -> bool:
    return str(status or "").upper().endswith("FILLED_ALL")


def _order_status_name(status: str) -> str:
    return str(status or "").upper().split(".")[-1]


EXTREME_ENTRY_MODES = {"极度超卖", "极度超买"}
TERMINAL_UNFILLED_EXIT_STATUSES = {
    "CANCELLED_ALL",
    "CANCELLED_PART",
    "FAILED",
    "SUBMIT_FAILED",
    "DELETED",
    "DISABLED",
}


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

        # 回调
        self.on_kline_update: Optional[Callable] = None
        self.on_kline_batch: Optional[Callable] = None
        self.on_trade_signal: Optional[Callable] = None
        self.on_state_update: Optional[Callable] = None

        # 历史记录
        self.trade_history: list[TradeRecord] = load_trade_log()
        self.kline_history_1m: list[KlineData] = []

        # 最新指标快照
        self._last_rsi: float | None = None
        self._last_vwap: float | None = None
        self._last_vwap_slope: float | None = None
        self._last_vol_ma: float | None = None

        # 运行时可调参数
        self.er_ratio = ER_RATIO
        self.share_count = SHARE_COUNT
        self.target_pnl = TARGET_PNL
        self.extreme_stop_pnl = EXTREME_STOP_PNL
        self.stop_points = STOP_POINTS
        self.rsi_oversold = RSI_OVERSOLD
        self.rsi_overbought = RSI_OVERBOUGHT
        self.poll_interval = POLL_INTERVAL
        self.entry_order_wait_seconds = ENTRY_ORDER_WAIT_SECONDS
        self.entry_cutoff_time = ENTRY_CUTOFF_TIME
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
                else:
                    setattr(self, key, value)
        if "target_pnl" in kwargs or "er_ratio" in kwargs or "share_count" in kwargs:
            self.stop_points = self._stop_points_for_pnl(self.target_pnl)
        self._save_runtime_config()

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
            "rsi_oversold": self.rsi_oversold,
            "rsi_overbought": self.rsi_overbought,
            "poll_interval": self.poll_interval,
            "entry_order_wait_seconds": self.entry_order_wait_seconds,
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
            "rsi_oversold",
            "rsi_overbought",
            "poll_interval",
            "entry_order_wait_seconds",
        }
        try:
            for key in int_fields:
                if key not in data:
                    continue
                value = int(data[key])
                if value <= 0:
                    raise ValueError(f"{key} 必须大于 0")
                setattr(self, key, value)
            for key in ("bull_warrant_code", "bear_warrant_code"):
                if key in data:
                    setattr(self, key, normalize_warrant_code(data.get(key)))
            self.stop_points = self._stop_points_for_pnl(self.target_pnl)
        except Exception as e:
            print(f"[RuntimeConfig] 配置内容无效，使用默认配置: {e}")
            self.er_ratio = ER_RATIO
            self.share_count = SHARE_COUNT
            self.target_pnl = TARGET_PNL
            self.extreme_stop_pnl = EXTREME_STOP_PNL
            self.stop_points = self._stop_points_for_pnl(self.target_pnl)
            self.rsi_oversold = RSI_OVERSOLD
            self.rsi_overbought = RSI_OVERBOUGHT
            self.poll_interval = POLL_INTERVAL
            self.entry_order_wait_seconds = ENTRY_ORDER_WAIT_SECONDS
            self.bull_warrant_code = BULL_WARRANT_CODE
            self.bear_warrant_code = BEAR_WARRANT_CODE

    async def _emit_trade_record(self, record: TradeRecord):
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
        self.total_pnl_hkd += pnl_hkd
        if pnl_hkd >= 0:
            self.win_count += 1
        else:
            self.loss_count += 1
        self.trade_count = max(self.trade_count, self.win_count + self.loss_count)
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

    def _is_after_entry_cutoff(self, current_time: str) -> bool:
        time_part = current_time[11:16] if len(current_time) >= 16 else current_time[:5]
        return time_part >= self.entry_cutoff_time

    async def _cancel_pending_entry_after_cutoff(self, current_time: str, hsi_price: float, rsi: float):
        if not self.pending_buy_order_id:
            return
        result = self.trader.cancel_order(self.pending_buy_order_id)
        await self._emit_trade_record(TradeRecord(
            time=current_time,
            signal=TradeSignal.HOLD,
            price=hsi_price,
            rsi=round(rsi, 2),
            position=PositionType.NONE,
            message=(
                f"15:50后不再开新仓，已取消未成交买入单: "
                f"order_id:{self.pending_buy_order_id} | {result.get('message')}"
            ),
        ))
        self._reset_order_state()
        self._save_runtime_state()

    async def _submit_entry_order(
        self,
        side: PositionType,
        hsi_price: float,
        rsi: float,
        current_time: str,
        mode: str,
        extra_message: str = "",
    ):
        if self.position != PositionType.NONE or self.pending_buy_order_id:
            return

        raw_code = self.bull_warrant_code if side == PositionType.BULL else self.bear_warrant_code
        code = normalize_warrant_code(raw_code)
        signal = TradeSignal.BUY_BULL if side == PositionType.BULL else TradeSignal.BUY_BEAR
        label = "牛证" if side == PositionType.BULL else "熊证"
        if not code:
            await self._emit_trade_record(TradeRecord(
                time=current_time, signal=signal, price=hsi_price, rsi=round(rsi, 2),
                position=side, message=f"【{label}·{mode}】未下单: 未配置{label} number",
            ))
            return

        snapshot = self.data_source.get_security_snapshot(code)
        if snapshot is None:
            await self._emit_trade_record(TradeRecord(
                time=current_time, signal=signal, price=hsi_price, rsi=round(rsi, 2),
                position=side, message=f"【{label}·{mode}】未下单: {code} 买一/卖一/价差无效",
            ))
            return

        buy_price = snapshot["bid_price"]
        result = self.trader.place_order(code, buy_price, self.share_count, "BUY")
        if not result.get("success"):
            await self._emit_trade_record(TradeRecord(
                time=current_time, signal=signal, price=hsi_price, rsi=round(rsi, 2),
                position=side, message=f"【{label}·{mode}】买入挂单失败: {code} @ {buy_price:.3f} | {result.get('message')}",
            ))
            return

        self.current_warrant_code = code
        self.pending_entry_side = side
        self.pending_buy_order_id = result["order_id"]
        self.entry_order_time = datetime.now()
        self.entry_chase_count = 0
        self.warrant_tick_size = snapshot["price_spread"]
        self.warrant_qty = float(self.share_count)
        self.entry_mode = mode
        self._save_runtime_state()

        suffix = f" | {extra_message}" if extra_message else ""
        await self._emit_trade_record(TradeRecord(
            time=current_time, signal=signal, price=hsi_price, rsi=round(rsi, 2),
            position=side,
            message=(
                f"【{label}·{mode}】挂 buy1 买入: {code} x{self.share_count} "
                f"@ {buy_price:.3f} | order_id:{self.pending_buy_order_id}{suffix}"
            ),
        ))

    async def _monitor_entry_order(self, current_time: str, hsi_price: float, rsi: float):
        if not self.pending_buy_order_id:
            return

        order = self.trader.get_order(self.pending_buy_order_id)
        if order is None:
            if self.entry_order_time is None:
                return
            elapsed = (datetime.now() - self.entry_order_time).total_seconds()
            if elapsed < self.entry_order_wait_seconds * 2:
                return
            await self._emit_trade_record(TradeRecord(
                time=current_time,
                signal=TradeSignal.HOLD,
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
                signal=TradeSignal.HOLD,
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
        if elapsed < self.entry_order_wait_seconds:
            return

        if self.entry_chase_count == 0:
            snapshot = self.data_source.get_security_snapshot(self.current_warrant_code)
            if snapshot is None:
                return
            new_price = snapshot["bid_price"]
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
                await self._emit_trade_record(TradeRecord(
                    time=current_time,
                    signal=TradeSignal.HOLD,
                    price=hsi_price,
                    rsi=round(rsi, 2),
                    position=PositionType.NONE,
                    message=f"买入未成交，已追价一次到最新 buy1: {self.current_warrant_code} @ {new_price:.3f}",
                ))
            return

        result = self.trader.cancel_order(self.pending_buy_order_id)
        await self._emit_trade_record(TradeRecord(
            time=current_time,
            signal=TradeSignal.HOLD,
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
        stop_price = snapshot.get("bid_price")
        if stop_price is None or stop_price <= 0:
            return None, f"{code} buy1 无效: {stop_price}"
        return float(stop_price), ""

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
        await self._emit_trade_record(TradeRecord(
            time=current_time,
            signal=TradeSignal.STOP_LOSS,
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
                signal=TradeSignal.STOP_LOSS,
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
            self._save_runtime_state()
        dealt_qty = float(order.get("dealt_qty", 0.0)) if order else 0.0
        remain_qty = self._remaining_exit_qty(order)
        await self._emit_trade_record(TradeRecord(
            time=current_time,
            signal=TradeSignal.STOP_LOSS,
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

        stop_price, reason = self._get_stop_exit_price(self.current_warrant_code)
        if stop_price is None:
            await self._emit_trade_record(TradeRecord(
                time=current_time,
                signal=TradeSignal.STOP_LOSS,
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
            self._save_runtime_state()
        await self._emit_trade_record(TradeRecord(
            time=current_time,
            signal=TradeSignal.STOP_LOSS,
            price=hsi_price,
            rsi=round(rsi, 2),
            position=self.position,
            pnl=round(diff, 2),
            pnl_hkd=round(actual_pnl, 2),
            message=(
                f"止损触发，{action}: {self.current_warrant_code} "
                f"@ {stop_price:.3f} | {result.get('message')} | "
                f"入场模式:{self.entry_mode or '-'} | 阈值:{active_stop_points:.1f}点"
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
            None, self.data_source.get_kline_with_indicators, "1m"
        )
        # 拉取 15 分钟 K 线 (跨周期确认)
        df_15m = await loop.run_in_executor(
            None, self.data_source.get_kline_with_indicators, "15m"
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

        # 5. 累积涨跌幅 (最近5根1M K线)
        cum5 = 0.0
        if len(df_1m) >= 6:
            recent_closes = [df_1m.iloc[j]["close"] for j in range(len(df_1m)-6, len(df_1m))]
            cum5 = recent_closes[-1] - recent_closes[0]

        breadth_ratio = None
        if snapshot:
            breadth_ratio = snapshot["raise_count"] / max(snapshot["fall_count"], 1)

        current_time = df_1m.index[-1].strftime("%Y-%m-%d %H:%M:%S")
        vwap_status = "拐↑" if vwap_turning_up else ("拐↓" if vwap_turning_down else "平")

        await self._monitor_entry_order(current_time, price, rsi)
        await self._monitor_exit_order(current_time, price, rsi)

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

        if self.position == PositionType.NONE:
            # --- 牛证 ---
            # 极度超卖 (RSI < 18): 只要放量就入场，不等形态确认
            # 普通超卖 (18 <= RSI < 25): 需要全部5个条件
            bull_extreme = rsi < self.rsi_oversold and vol_is_high
            bull_normal = (self.rsi_oversold <= rsi < 25
                           and vwap_turning_up and vol_is_high
                           and k_bull_pattern and m15_is_green)

            if bull_extreme or bull_normal:
                mode = "极度超卖" if bull_extreme else "普通超卖"
                await self._submit_entry_order(
                    PositionType.BULL, price, rsi, current_time, mode,
                    extra_message=f"HSI:{price:.2f} RSI:{rsi:.2f}",
                )

            # --- 熊证 ---
            # 极度超买 (RSI > 82): 只要放量就入场
            # 普通超买 (75 < RSI <= 82): 需要全部5个条件
            elif rsi > 75:
                bear_extreme = rsi > self.rsi_overbought and vol_is_high
                bear_normal = (75 < rsi <= self.rsi_overbought
                               and vwap_turning_down and vol_is_high
                               and k_bear_pattern and m15_is_red)

                if bear_extreme or bear_normal:
                    mode = "极度超买" if bear_extreme else "普通超买"
                    await self._submit_entry_order(
                        PositionType.BEAR, price, rsi, current_time, mode,
                        extra_message=f"HSI:{price:.2f} RSI:{rsi:.2f}",
                    )

            # --- 放量动能信号 (不以 RSI 触发，但用 RSI / 市宽过滤追价) ---
            # 成交额 > 1.5 倍均量 + 1M K 线大幅单边 (>10点) + VWAP 方向一致
            if self.position == PositionType.NONE:
                k_change = k_close - k_open
                vol_surge = vol > vol_ma * 1.5

                if vol_surge and k_change > 10 and curr_slope > 0:
                    skip_reasons = get_momentum_filter_reasons(
                        "bull", float(rsi), breadth_ratio
                    )
                    if skip_reasons:
                        print(f"  >>> Skip bull momentum: {'; '.join(skip_reasons)}")
                    else:
                        await self._submit_entry_order(
                            PositionType.BULL, price, rsi, current_time, "放量动能",
                            extra_message=f"涨{k_change:.1f}点 | {vol/vol_ma:.1f}x量",
                        )

                elif vol_surge and k_change < -10 and curr_slope < 0:
                    skip_reasons = get_momentum_filter_reasons(
                        "bear", float(rsi), breadth_ratio
                    )
                    if skip_reasons:
                        print(f"  >>> Skip bear momentum: {'; '.join(skip_reasons)}")
                    else:
                        await self._submit_entry_order(
                            PositionType.BEAR, price, rsi, current_time, "放量动能",
                            extra_message=f"跌{abs(k_change):.1f}点 | {vol/vol_ma:.1f}x量",
                        )

            # --- 累积趋势信号 (温水煮青蛙式单边) ---
            # 最近5根1M累积跌/涨 > 40点 + VWAP方向一致
            # 过滤条件：
            #   1. 日内区间 >= 150点 (确认有波动)
            #   2. 信号方向必须和开盘以来的整体方向一致
            #   3. 涨跌家数比必须支持信号方向
            if self.position == PositionType.NONE and abs(cum5) >= 40:
                day_high = df_1m["high"].max()
                day_low = df_1m["low"].min()
                day_range = day_high - day_low
                day_trend = price - df_1m.iloc[0]["open"]

                if day_range >= 150:
                    # 做空信号：累积跌 + 当天整体也在跌
                    if cum5 < -40 and curr_slope < 0 and day_trend < 0:
                        skip_reasons = get_cum_trend_filter_reasons(
                            "bear", breadth_ratio
                        )
                        if skip_reasons:
                            print(f"  >>> Skip bear cumtrend: {'; '.join(skip_reasons)}")
                        else:
                            ratio = breadth_ratio
                            await self._submit_entry_order(
                                PositionType.BEAR, price, rsi, current_time, "累积趋势",
                                extra_message=f"5根累跌{cum5:.1f}点 | 日内{day_range:.0f}点 | R:{ratio:.2f}",
                            )

                    # 做多信号：累积涨 + 当天整体也在涨
                    elif cum5 > 40 and curr_slope > 0 and day_trend > 0:
                        skip_reasons = get_cum_trend_filter_reasons(
                            "bull", breadth_ratio
                        )
                        if skip_reasons:
                            print(f"  >>> Skip bull cumtrend: {'; '.join(skip_reasons)}")
                        else:
                            ratio = breadth_ratio
                            await self._submit_entry_order(
                                PositionType.BULL, price, rsi, current_time, "累积趋势",
                                extra_message=f"5根累涨{cum5:.1f}点 | 日内{day_range:.0f}点 | R:{ratio:.2f}",
                            )

        else:
            # 止盈止损
            diff = 0.0
            if self.position == PositionType.BULL:
                diff = price - self.entry_price
            elif self.position == PositionType.BEAR:
                diff = self.entry_price - price
            actual_pnl = (diff / self.er_ratio) * self.share_count

            active_stop_points = self._active_stop_points()
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
        self._save_runtime_state()
