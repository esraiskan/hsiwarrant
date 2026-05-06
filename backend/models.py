"""
数据模型定义
"""
from pydantic import BaseModel
from typing import Literal, Optional
from enum import Enum


class PositionType(str, Enum):
    NONE = "none"
    BULL = "bull"  # 牛证
    BEAR = "bear"  # 熊证


class TradeSignal(str, Enum):
    BUY_BULL = "buy_bull"
    BUY_BEAR = "buy_bear"
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    HOLD = "hold"


class KlineData(BaseModel):
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    rsi: Optional[float] = None
    vwap: Optional[float] = None
    vwap_slope: Optional[float] = None
    vol_ma: Optional[float] = None


class TradeRecord(BaseModel):
    time: str
    signal: TradeSignal
    price: float
    rsi: float
    position: PositionType
    pnl: Optional[float] = None
    pnl_hkd: Optional[float] = None
    message: str


class StrategyState(BaseModel):
    position: PositionType = PositionType.NONE
    entry_price: float = 0.0
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_hkd: float = 0.0
    total_pnl_hkd: float = 0.0
    breadth_raise_count: int = 0
    breadth_fall_count: int = 0
    breadth_equal_count: int = 0
    breadth_ratio: Optional[float] = None
    breadth_amplitude: float = 0.0
    breadth_time: str = ""
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    is_running: bool = False


class ConfigResponse(BaseModel):
    symbol: str
    er_ratio: int
    share_count: int
    target_pnl: int
    stop_points: float
    extreme_stop_pnl: int
    extreme_stop_points: float
    bull_warrant_code: str
    bull_warrant_name: str
    bear_warrant_code: str
    bear_warrant_name: str
    rsi_oversold: int
    rsi_overbought: int
    vol_ma_period: int
    poll_interval: int
    entry_order_wait_seconds: int
    entry_cutoff_time: str


class ConfigUpdate(BaseModel):
    er_ratio: Optional[int] = None
    share_count: Optional[int] = None
    target_pnl: Optional[int] = None
    extreme_stop_pnl: Optional[int] = None
    bull_warrant_code: Optional[str] = None
    bear_warrant_code: Optional[str] = None
    rsi_oversold: Optional[int] = None
    rsi_overbought: Optional[int] = None
    vol_ma_period: Optional[int] = None
    poll_interval: Optional[int] = None
    entry_order_wait_seconds: Optional[int] = None


class TradeEnvUpdate(BaseModel):
    trade_env: Literal["SIMULATE", "REAL"]
    trade_password: Optional[str] = None


class TradeEnvUpdateResponse(BaseModel):
    success: bool
    message: str
    trade_env: Literal["SIMULATE", "REAL"]
    real_unlocked_today: bool
    trade_env_date: Optional[str] = None
