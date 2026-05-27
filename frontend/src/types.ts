/** 仓位类型 */
export type PositionType = 'none' | 'bull' | 'bear';

/** 交易信号类型 */
export type TradeSignal =
  | 'buy_bull'
  | 'buy_bear'
  | 'take_profit'
  | 'stop_loss'
  | 'entry_pending'
  | 'entry_chasing'
  | 'stop_loss_pending'
  | 'stop_loss_chasing'
  | 'hold';

/** 富途交易环境 */
export type TradeEnv = 'SIMULATE' | 'REAL';

/** K 线数据 */
export interface KlineData {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  rsi: number | null;
  vwap: number | null;
  vwap_slope: number | null;
  vol_ma: number | null;
}

/** 盤中市況分類，只作顯示和參數建議，不參與交易邏輯 */
export interface MarketRegime {
  regime: string;
  label: string;
  bias: 'bullish' | 'bearish' | 'neutral' | 'repair';
  confidence: number;
  suggested_rsi_overbought_low: number;
  suggested_rsi_overbought_high: number;
  suggested_rsi_oversold_low: number;
  suggested_rsi_oversold_high: number;
  advice: string;
  reasons: string[];
  updated_at: string;
  update_interval_seconds: number;
  current_price: number | null;
  day_open: number | null;
  previous_close: number | null;
  opening_range_high: number | null;
  opening_range_low: number | null;
  opening_range_mid: number | null;
  day_position_pct: number | null;
}

/** 交易记录 */
export interface TradeRecord {
  time: string;
  signal: TradeSignal;
  price: number;
  rsi: number;
  position: PositionType;
  pnl: number | null;
  pnl_hkd: number | null;
  message: string;
}

/** CBBC 磁吸 consult 记录（每次咨询写一条） */
export interface MagnetConsultRecord {
  event: 'cbbc_magnet_consult' | 'cbbc_magnet_consult_unavailable';
  extreme_direction: 'BULL' | 'BEAR';
  nearest_bull_distance_pts: number | null;
  nearest_bear_distance_pts: number | null;
  magnet_bias: number | null;
  magnet_available: boolean;
  magnet_aligned_against_reversal: boolean;
  vetoed_by_cbbc_magnet: boolean;
  reason_code: string;
  ts_hk: string;
}

/** 策略状态 */
export interface StrategyState {
  position: PositionType;
  entry_price: number;
  current_price: number;
  unrealized_pnl: number;
  unrealized_pnl_hkd: number;
  total_pnl_hkd: number;
  breadth_raise_count: number;
  breadth_fall_count: number;
  breadth_equal_count: number;
  breadth_ratio: number | null;
  breadth_amplitude: number;
  breadth_time: string;
  trade_count: number;
  win_count: number;
  loss_count: number;
  is_running: boolean;
  // CBBC 磁吸信号层
  cbbc_magnet_layer_enabled?: boolean;
  cbbc_magnet_degraded?: boolean;
  cbbc_magnet_bias?: number | null;
  cbbc_nearest_bull_distance_pts?: number | null;
  cbbc_nearest_bear_distance_pts?: number | null;
  last_magnet_consult?: MagnetConsultRecord | null;
}

/** 策略配置 */
export interface StrategyConfig {
  symbol: string;
  er_ratio: number;
  share_count: number;
  target_pnl: number;
  stop_points: number;
  extreme_stop_pnl: number;
  extreme_stop_points: number;
  bull_warrant_code: string;
  bull_warrant_name: string;
  bear_warrant_code: string;
  bear_warrant_name: string;
  rsi_length: number;
  rsi_oversold: number;
  rsi_overbought: number;
  vol_ma_period: number;
  poll_interval: number;
  entry_order_wait_seconds: number;
  entry_cutoff_time: string;
  only_extreme_entries: boolean;
  enabled_strategies: BacktestStrategy[];
  enabled_extreme_branches: BacktestExtremeBranch[];
  extreme_rsi_stop_veto_enabled: boolean;
  extreme_rsi_stop_hard_ticks: number;
  extreme_rsi_stop_rearm_ticks: number;
  // CBBC 磁吸方向闸门
  cbbc_magnet_layer_enabled?: boolean;
  cbbc_magnet_direction_gate_enabled?: boolean;
  cbbc_magnet_direction_gate_threshold?: number;
  // CBBC AI 决策顾问 (read-only, 不影响交易决策)
  cbbc_ai_advisor_enabled?: boolean;
  cbbc_ai_advisor_base_url?: string;
  cbbc_ai_advisor_model?: string;
  cbbc_ai_advisor_api_key?: string;
  cbbc_ai_advisor_api_style?: 'openai' | 'anthropic';
}

/** OpenD 连接状态 */
export interface OpenDStatus {
  host: string;
  port: number;
  trade_env: TradeEnv;
  real_unlocked_today: boolean;
  trade_env_date: string | null;
  quote_connected: boolean;
  trade_connected: boolean;
  strategy_running: boolean;
}

/** 交易环境切换请求 */
export interface TradeEnvUpdateRequest {
  trade_env: TradeEnv;
  trade_password?: string;
}

/** 交易环境切换结果 */
export interface TradeEnvUpdateResponse {
  success: boolean;
  message: string;
  trade_env: TradeEnv;
  real_unlocked_today: boolean;
  trade_env_date: string | null;
}

/** 账户信息 */
export interface AccountInfo {
  total_assets: number;
  cash: number;
  market_val: number;
  frozen_cash: number;
  available_funds: number;
}

/** 当前交易环境的当天 P&L */
export interface TodayPnl {
  success: boolean;
  trade_env: TradeEnv;
  date: string;
  today_pnl_hkd: number;
  fallback_pnl_hkd?: number;
  source: string;
  message: string;
  positions: Array<{
    code: string;
    name: string;
    qty: number;
    market_val: number;
    today_pl_val: number;
    pl_val: number;
    unrealized_pl: number;
    realized_pl: number;
    position_side: string;
  }>;
}

/** 恒指快照 */
export interface MarketSnapshot {
  code: string;
  name: string;
  last_price: number;
  open_price: number;
  high_price: number;
  low_price: number;
  volume: number;
  turnover: number;
}

export type BacktestPeriodMode = 'months' | 'date_range';
export type BacktestStrategy = 'normal' | 'extreme' | 'momentum' | 'cum_trend' | 'rsi_divergence';
export type BacktestExtremeBranch =
  | 'b1_volume_extreme'
  | 'b2_very_extreme_pullback'
  | 'b3_completed_k'
  | 'b4_shadow_reversal';
export type BacktestCumTrendMode = 'strict_breadth' | 'market_log_breadth' | 'kline_proxy';

export interface BacktestRequest {
  period_mode: BacktestPeriodMode;
  months: string[];
  date_start?: string;
  date_end?: string;
  rsi_length: number;
  rsi_oversold: number;
  rsi_overbought: number;
  take_profit_points: number;
  stop_loss_points: number;
  fixed_win_hkd: number;
  fixed_loss_hkd: number;
  selection: {
    strategies: BacktestStrategy[];
    extreme_branches: BacktestExtremeBranch[];
  };
  cum_trend_mode: BacktestCumTrendMode;
}

export interface BacktestSummary {
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  pnl_hkd: number;
}

export interface BacktestBreakdownRow extends BacktestSummary {
  key: string;
  label: string;
}

export interface BacktestTrade {
  trade_date: string;
  side: 'bull' | 'bear';
  mode: string;
  branch: string;
  entry_time: string;
  exit_time: string;
  entry: number;
  exit: number;
  points: number;
  result: 'W' | 'L';
  pnl_hkd: number;
  minutes: number;
  desc: string;
}

export interface BacktestResult {
  requested_start: string;
  requested_end: string;
  data_start: string;
  data_end: string;
  summary: BacktestSummary;
  monthly: BacktestBreakdownRow[];
  daily: BacktestBreakdownRow[];
  strategy_breakdown: BacktestBreakdownRow[];
  extreme_branch_breakdown: BacktestBreakdownRow[];
  trades: BacktestTrade[];
  warnings: string[];
}

/** CBBC 磁吸 overlay - 单个 Call_Level */
export interface MagnetOverlayCallLevel {
  code: string;
  direction: 'bull' | 'bear';
  call_level: number;
  /** 发行商代码 (UB / SG / BP / JP / HSBC ...) */
  issuer?: string;
  /** 街货量 (股) */
  outstanding_shares?: number;
  /** ``|call_level - HSI|`` 在推送瞬间的距离 (pt) */
  distance_pts?: number;
}

/** CBBC 磁吸 overlay - 5 点价格桶的 Notional 拉力 */
export interface MagnetOverlayHistogramBucket {
  bucket_low: number;
  bucket_high: number;
  pull_hkd: number;
}

/** CBBC 磁吸 overlay - 最近一次否决记录 */
export interface MagnetOverlayVeto {
  kline_time: string;
  direction: 'BULL' | 'BEAR';
  reason_code: string;
}

/** CBBC 磁吸 overlay 推送载荷（WebSocket type=magnet_overlay） */
export interface MagnetOverlayPayload {
  decay_points?: number;
  dense_band_pull_share: number;
  /** 密集带距离阈值（pt），用于前端画 spot ± threshold 的边界虚线 */
  dense_band_threshold_pts?: number;
  cbbc_magnet_degraded: boolean;
  hsi_spot_stale: boolean;
  call_levels: MagnetOverlayCallLevel[];
  histogram: MagnetOverlayHistogramBucket[];
  recent_vetoes: MagnetOverlayVeto[];
}

/** CBBC 街货密集区 (read-only,纯展示) - 单个 25pt 桶 */
export interface CbbcZoneCluster {
  bucket_low: number;
  bucket_high: number;
  direction: 'bull' | 'bear';
  /** 距 spot 的点数;正 = 上方目标, 负 = 下方支撑 */
  distance_pts: number;
  notional_hkd: number;
  contract_count: number;
  outstanding_shares: number;
  /** 桶内距 spot 最近的活合约 call_level (UI 显示的真实档位) */
  nearest_call_level: number;
  /** 距今日高低的安全余量 (pt) */
  safety_margin_pts?: number | null;
}

/** 单方向操作建议 (read-only) */
export interface CbbcTradeSetup {
  direction: 'bull' | 'bear';
  entry_low: number;
  entry_high: number;
  take_profit_1: number;
  take_profit_2: number;
  stop_loss: number;
  risk_reward: number;
  rationale: string;
}

/** ``GET /api/cbbc/zones`` + WS ``cbbc_zones`` 推送的载荷 */
export interface CbbcZonesPayload {
  spot: number;
  today_low: number | null;
  today_high: number | null;
  bucket_pts: number;
  targets_above: CbbcZoneCluster[];
  supports_below: CbbcZoneCluster[];
  live_record_count: number;
  killed_record_count: number;
  bull_setup?: CbbcTradeSetup | null;
  bear_setup?: CbbcTradeSetup | null;
}

/** ``POST /api/cbbc/ai-advice`` 返回的载荷 (read-only,不影响交易) */
export interface CbbcAiAdviceResponse {
  ok: boolean;
  direction?: 'bull' | 'bear' | 'skip' | null;
  confidence?: number | null;
  entry_low?: number | null;
  entry_high?: number | null;
  take_profit_1?: number | null;
  take_profit_2?: number | null;
  stop_loss?: number | null;
  rationale?: string | null;
  error?: string | null;
  raw_model_text?: string | null;
  model?: string | null;
  elapsed_seconds?: number | null;
}

export interface HksiStyleEntryPlan {
  condition: string;
  action: '買升' | '買跌' | '不做';
  amount: number;
}

export interface HksiStyleTradePlan {
  main_direction: string;
  status: '空倉等待' | '持倉觀察' | '等待確認' | '不交易';
  entry_plan_1: HksiStyleEntryPlan;
  entry_plan_2: HksiStyleEntryPlan;
  stop_loss: string[];
  take_profit: string[];
  give_up_conditions: string[];
  product_warning: string;
  summary: string;
}

export interface HksiStyleExecutionEntryRule {
  label: string;
  action: 'buy_bull' | 'buy_bear' | 'none';
  price_min: number;
  price_max: number;
  rsi_min: number;
  rsi_max: number;
  vwap_relation: 'any' | 'above' | 'below';
  amount: number;
  priority: number;
  comment: string;
}

export interface HksiStyleExecutionPlan {
  enabled: boolean;
  side: 'bull' | 'bear' | 'none';
  entry_rules: HksiStyleExecutionEntryRule[];
  take_profit_levels: number[];
  stop_loss_levels: number[];
  give_up_levels: number[];
  max_position_hkd: number;
  time_in_force: string;
  notes: string;
}

export interface HksiStyleTradeCoefficients {
  direction: 'UP' | 'DOWN' | 'NEUTRAL';
  entry_min: number;
  entry_max: number;
  share_count: number;
  take_profit_min: number;
  take_profit_max: number;
  stop_loss_min: number;
  stop_loss_max: number;
  confidence: number;
  notes: string;
}

export interface HksiStyleAiReview {
  summary: string;
  trade_plan: HksiStyleTradePlan;
  execution_plan: HksiStyleExecutionPlan;
  trade_coefficients: HksiStyleTradeCoefficients;
  risk_level: 'low' | 'medium' | 'high';
  confidence_comment: string;
  key_supporting_points: string[];
  conflicts: string[];
  watch_levels: string[];
  data_quality_notes: string[];
  suggested_user_action: string;
  actionability: 'observe' | 'wait_for_confirmation' | 'reduce_size' | 'avoid_trade';
  limitations: string[];
}

export interface HksiStyleAiAdviceResponse {
  ok: boolean;
  generated_at: string;
  source: string;
  model?: string | null;
  review?: HksiStyleAiReview | null;
  context?: Record<string, unknown>;
  error?: string | null;
  raw_model_text?: string | null;
  elapsed_seconds?: number | null;
}

/** WebSocket 消息 */
export type WSMessage =
  | { type: 'kline'; data: KlineData }
  | { type: 'kline_batch'; data: KlineData[] }
  | { type: 'market_regime'; data: MarketRegime }
  | { type: 'trade'; data: TradeRecord }
  | { type: 'state'; data: StrategyState }
  | { type: 'magnet_overlay'; data: MagnetOverlayPayload }
  | { type: 'cbbc_zones'; data: CbbcZonesPayload }
  | { type: 'pong' };
