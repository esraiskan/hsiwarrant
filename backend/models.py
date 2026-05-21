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
    ENTRY_PENDING = "entry_pending"
    ENTRY_CHASING = "entry_chasing"
    STOP_LOSS_PENDING = "stop_loss_pending"
    STOP_LOSS_CHASING = "stop_loss_chasing"
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


class MarketRegime(BaseModel):
    regime: str
    label: str
    bias: Literal["bullish", "bearish", "neutral", "repair"]
    confidence: int
    suggested_rsi_overbought_low: float
    suggested_rsi_overbought_high: float
    suggested_rsi_oversold_low: float
    suggested_rsi_oversold_high: float
    advice: str
    reasons: list[str]
    updated_at: str
    update_interval_seconds: int
    current_price: Optional[float] = None
    day_open: Optional[float] = None
    previous_close: Optional[float] = None
    opening_range_high: Optional[float] = None
    opening_range_low: Optional[float] = None
    opening_range_mid: Optional[float] = None
    day_position_pct: Optional[float] = None


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
    rsi_length: int
    rsi_oversold: int
    rsi_overbought: int
    vol_ma_period: int
    poll_interval: int
    entry_order_wait_seconds: int
    entry_cutoff_time: str
    only_extreme_entries: bool
    enabled_strategies: list[str]
    enabled_extreme_branches: list[str]
    extreme_rsi_stop_veto_enabled: bool
    extreme_rsi_stop_hard_ticks: int
    extreme_rsi_stop_rearm_ticks: int


class ConfigUpdate(BaseModel):
    er_ratio: Optional[int] = None
    share_count: Optional[int] = None
    target_pnl: Optional[int] = None
    extreme_stop_pnl: Optional[int] = None
    bull_warrant_code: Optional[str] = None
    bear_warrant_code: Optional[str] = None
    rsi_length: Optional[int] = None
    rsi_oversold: Optional[int] = None
    rsi_overbought: Optional[int] = None
    vol_ma_period: Optional[int] = None
    poll_interval: Optional[int] = None
    entry_order_wait_seconds: Optional[int] = None
    only_extreme_entries: Optional[bool] = None
    enabled_strategies: Optional[list[str]] = None
    enabled_extreme_branches: Optional[list[str]] = None
    extreme_rsi_stop_veto_enabled: Optional[bool] = None
    extreme_rsi_stop_hard_ticks: Optional[int] = None
    extreme_rsi_stop_rearm_ticks: Optional[int] = None


class TradeEnvUpdate(BaseModel):
    trade_env: Literal["SIMULATE", "REAL"]
    trade_password: Optional[str] = None


class TradeEnvUpdateResponse(BaseModel):
    success: bool
    message: str
    trade_env: Literal["SIMULATE", "REAL"]
    real_unlocked_today: bool
    trade_env_date: Optional[str] = None


BacktestPeriodMode = Literal["months", "date_range"]
BacktestStrategy = Literal["normal", "extreme", "momentum", "cum_trend", "rsi_divergence"]
BacktestExtremeBranch = Literal[
    "b1_volume_extreme",
    "b2_very_extreme_pullback",
    "b3_completed_k",
    "b4_shadow_reversal",
]
BacktestCumTrendMode = Literal["strict_breadth", "market_log_breadth", "kline_proxy"]


class BacktestStrategySelection(BaseModel):
    strategies: list[BacktestStrategy]
    extreme_branches: list[BacktestExtremeBranch] = []


class BacktestRequest(BaseModel):
    period_mode: BacktestPeriodMode
    months: list[str] = []
    date_start: Optional[str] = None
    date_end: Optional[str] = None
    rsi_length: int = 6
    rsi_oversold: float = 20.0
    rsi_overbought: float = 80.0
    take_profit_points: float = 20.0
    stop_loss_points: float = 20.0
    fixed_win_hkd: float = 400.0
    fixed_loss_hkd: float = -400.0
    selection: BacktestStrategySelection
    cum_trend_mode: BacktestCumTrendMode = "market_log_breadth"


class BacktestSummary(BaseModel):
    trades: int
    wins: int
    losses: int
    win_rate: float
    pnl_hkd: float


class BacktestBreakdownRow(BaseModel):
    key: str
    label: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    pnl_hkd: float


class BacktestTrade(BaseModel):
    trade_date: str
    side: Literal["bull", "bear"]
    mode: str
    branch: str
    entry_time: str
    exit_time: str
    entry: float
    exit: float
    points: float
    result: Literal["W", "L"]
    pnl_hkd: float
    minutes: float
    desc: str


class BacktestResult(BaseModel):
    requested_start: str
    requested_end: str
    data_start: str
    data_end: str
    summary: BacktestSummary
    monthly: list[BacktestBreakdownRow]
    daily: list[BacktestBreakdownRow]
    strategy_breakdown: list[BacktestBreakdownRow]
    extreme_branch_breakdown: list[BacktestBreakdownRow]
    trades: list[BacktestTrade]
    warnings: list[str]
