"""
数据模型定义
"""
from pydantic import BaseModel, Field
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


class MagnetConsultRecord(BaseModel):
    """CBBC 磁吸咨询的结构化日志载体（每次 consult 写一条）"""
    event: Literal["cbbc_magnet_consult", "cbbc_magnet_consult_unavailable"]
    extreme_direction: Literal["BULL", "BEAR"]
    nearest_bull_distance_pts: Optional[float] = None
    nearest_bear_distance_pts: Optional[float] = None
    magnet_bias: Optional[float] = None
    magnet_available: bool
    magnet_aligned_against_reversal: bool
    vetoed_by_cbbc_magnet: bool
    reason_code: str
    ts_hk: str


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
    # CBBC magnet signal layer (fail-safe defaults: layer off, not degraded, no consult)
    cbbc_magnet_layer_enabled: bool = False
    cbbc_magnet_degraded: bool = False
    cbbc_magnet_bias: Optional[float] = None
    cbbc_nearest_bull_distance_pts: Optional[float] = None
    cbbc_nearest_bear_distance_pts: Optional[float] = None
    last_magnet_consult: Optional[MagnetConsultRecord] = None


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
    # CBBC magnet signal layer runtime config (R9.1, R9.3, R9.4)
    cbbc_magnet_layer_enabled: bool
    cbbc_intraday_polling_suspended: bool
    cbbc_magnet_decay_points: float
    cbbc_dense_band_threshold_pts: float
    cbbc_dense_band_pull_share: float
    cbbc_intraday_poll_interval_seconds: float
    cbbc_magnet_direction_gate_enabled: bool
    cbbc_magnet_direction_gate_threshold: float
    # CBBC AI 决策顾问 (read-only, 不影响交易决策)
    cbbc_ai_advisor_enabled: bool
    cbbc_ai_advisor_base_url: str
    cbbc_ai_advisor_model: str
    cbbc_ai_advisor_api_key: str
    cbbc_ai_advisor_api_style: str


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
    # CBBC magnet signal layer runtime config (partial-update friendly)
    cbbc_magnet_layer_enabled: Optional[bool] = None
    cbbc_intraday_polling_suspended: Optional[bool] = None
    cbbc_magnet_decay_points: Optional[float] = None
    cbbc_dense_band_threshold_pts: Optional[float] = None
    cbbc_dense_band_pull_share: Optional[float] = None
    cbbc_intraday_poll_interval_seconds: Optional[float] = None
    cbbc_magnet_direction_gate_enabled: Optional[bool] = None
    cbbc_magnet_direction_gate_threshold: Optional[float] = None
    # CBBC AI 决策顾问 (partial-update friendly)
    cbbc_ai_advisor_enabled: Optional[bool] = None
    cbbc_ai_advisor_base_url: Optional[str] = None
    cbbc_ai_advisor_model: Optional[str] = None
    cbbc_ai_advisor_api_key: Optional[str] = None
    cbbc_ai_advisor_api_style: Optional[str] = None


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


# ----------------------------------------------------------------------------
# CBBC magnet signal layer — overlay & backtest summary models
# ----------------------------------------------------------------------------


class MagnetOverlayCallLevel(BaseModel):
    code: str
    direction: Literal["bull", "bear"]
    call_level: float
    # 可选字段:让前端 hover 时展示发行商 / 街货量 / 距离。
    issuer: Optional[str] = None
    outstanding_shares: Optional[float] = None
    distance_pts: Optional[float] = None


class MagnetOverlayHistogramBucket(BaseModel):
    bucket_low: float
    bucket_high: float
    pull_hkd: float


class MagnetOverlayVeto(BaseModel):
    kline_time: str
    direction: Literal["BULL", "BEAR"]
    reason_code: str


class MagnetOverlayPayload(BaseModel):
    """WebSocket type=magnet_overlay 推送载荷 (设计文档 R8.1/R8.6)"""
    decay_points: Optional[float] = None
    dense_band_pull_share: float = 0.40
    # 密集带阈值 (pt) - 用于前端绘制 spot ± threshold 的边界虚线 (R8.2)
    dense_band_threshold_pts: float = 150.0
    cbbc_magnet_degraded: bool = False
    hsi_spot_stale: bool = False
    call_levels: list[MagnetOverlayCallLevel] = []
    histogram: list[MagnetOverlayHistogramBucket] = []
    recent_vetoes: list[MagnetOverlayVeto] = []


class BacktestMagnetSummary(BaseModel):
    """回测 CBBC 磁吸否决统计 (设计文档 R7.7)"""
    total_vetoed: int = 0
    vetoed_dense_band_above: int = 0
    vetoed_dense_band_below: int = 0
    control_total: int = 0
    cbbc_snapshot_missing_days: int = 0


# ----------------------------------------------------------------------------
# CBBC 街货密集区 (read-only,纯展示;不参与交易决策)
# ----------------------------------------------------------------------------


class CbbcZoneCluster(BaseModel):
    """单个 25pt 桶聚合结果。"""
    bucket_low: float
    bucket_high: float
    direction: Literal["bull", "bear"]
    distance_pts: float       # 正 = 上方目标, 负 = 下方支撑
    notional_hkd: float
    contract_count: int
    outstanding_shares: float
    # 桶内"代表性"收回价 — 距 spot 最近的活合约,UI 显示的真实档位 (R: avoid bucket_low misleading)
    nearest_call_level: float
    # 距今日高低的安全余量 (pt);bull 用 today_low - nearest_cl,bear 用 nearest_cl - today_high
    safety_margin_pts: Optional[float] = None


class CbbcTradeSetup(BaseModel):
    """单方向操作建议 (read-only,纯展示)。"""
    direction: Literal["bull", "bear"]
    entry_low: float
    entry_high: float
    take_profit_1: float
    take_profit_2: float
    stop_loss: float
    risk_reward: float
    rationale: str


class CbbcZonesResponse(BaseModel):
    """``GET /api/cbbc/zones`` 与 WS ``type=cbbc_zones`` 推送的载荷。"""
    spot: float
    today_low: Optional[float] = None
    today_high: Optional[float] = None
    bucket_pts: int
    targets_above: list[CbbcZoneCluster] = []
    supports_below: list[CbbcZoneCluster] = []
    live_record_count: int = 0
    killed_record_count: int = 0
    bull_setup: Optional[CbbcTradeSetup] = None
    bear_setup: Optional[CbbcTradeSetup] = None


# ----------------------------------------------------------------------------
# CBBC AI 决策顾问响应 (read-only,纯展示;不参与交易决策)
# ----------------------------------------------------------------------------


class CbbcAiAdviceResponse(BaseModel):
    """``POST /api/cbbc/ai-advice`` 返回的载荷。

    ``ok=True`` 时所有交易字段有效;``ok=False`` 时只看 ``error``。
    """
    ok: bool
    direction: Optional[Literal["bull", "bear", "skip"]] = None
    confidence: Optional[float] = None  # 0..1
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    stop_loss: Optional[float] = None
    rationale: Optional[str] = None
    error: Optional[str] = None
    raw_model_text: Optional[str] = None
    model: Optional[str] = None
    elapsed_seconds: Optional[float] = None


class HksiStyleEntryPlan(BaseModel):
    condition: str = ""
    action: Literal["買升", "買跌", "不做"] = "不做"
    amount: int = 0


class HksiStyleTradePlan(BaseModel):
    main_direction: str = "中性 [neutral]"
    status: Literal["空倉等待", "持倉觀察", "等待確認", "不交易"] = "不交易"
    entry_plan_1: HksiStyleEntryPlan = Field(default_factory=HksiStyleEntryPlan)
    entry_plan_2: HksiStyleEntryPlan = Field(default_factory=HksiStyleEntryPlan)
    stop_loss: list[str] = Field(default_factory=list)
    take_profit: list[str] = Field(default_factory=list)
    give_up_conditions: list[str] = Field(default_factory=list)
    product_warning: str = ""
    summary: str = ""


class HksiStyleExecutionEntryRule(BaseModel):
    label: str = ""
    action: Literal["buy_bull", "buy_bear", "none"] = "none"
    price_min: float = 0
    price_max: float = 0
    rsi_min: float = 0
    rsi_max: float = 100
    vwap_relation: Literal["any", "above", "below"] = "any"
    amount: int = 0
    priority: int = 1
    comment: str = ""


class HksiStyleExecutionPlan(BaseModel):
    enabled: bool = False
    side: Literal["bull", "bear", "none"] = "none"
    entry_rules: list[HksiStyleExecutionEntryRule] = Field(default_factory=list)
    take_profit_levels: list[float] = Field(default_factory=list)
    stop_loss_levels: list[float] = Field(default_factory=list)
    give_up_levels: list[float] = Field(default_factory=list)
    max_position_hkd: int = 0
    time_in_force: str = "intraday"
    notes: str = ""


class HksiStyleTradeCoefficients(BaseModel):
    direction: Literal["UP", "DOWN", "NEUTRAL"] = "NEUTRAL"
    entry_min: float = 0
    entry_max: float = 0
    share_count: int = 0
    take_profit_min: float = 0
    take_profit_max: float = 0
    stop_loss_min: float = 0
    stop_loss_max: float = 0
    confidence: float = 0
    notes: str = ""


class HksiStyleAiReview(BaseModel):
    summary: str = ""
    trade_plan: HksiStyleTradePlan = Field(default_factory=HksiStyleTradePlan)
    execution_plan: HksiStyleExecutionPlan = Field(default_factory=HksiStyleExecutionPlan)
    trade_coefficients: HksiStyleTradeCoefficients = Field(default_factory=HksiStyleTradeCoefficients)
    risk_level: Literal["low", "medium", "high"] = "medium"
    confidence_comment: str = ""
    key_supporting_points: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    watch_levels: list[str] = Field(default_factory=list)
    data_quality_notes: list[str] = Field(default_factory=list)
    suggested_user_action: str = ""
    actionability: Literal["observe", "wait_for_confirmation", "reduce_size", "avoid_trade"] = "observe"
    limitations: list[str] = Field(default_factory=list)


class HksiStyleAiAdviceResponse(BaseModel):
    """``POST /api/cbbc/hksi-style-ai-advice`` 返回的 HKSI-style AI 分析。

    只作展示用途，不參與交易決策或下單。
    """
    ok: bool
    generated_at: str = ""
    source: str = "openai"
    model: Optional[str] = None
    review: Optional[HksiStyleAiReview] = None
    context: dict = Field(default_factory=dict)
    error: Optional[str] = None
    raw_model_text: Optional[str] = None
    elapsed_seconds: Optional[float] = None
