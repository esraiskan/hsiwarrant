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

/** WebSocket 消息 */
export type WSMessage =
  | { type: 'kline'; data: KlineData }
  | { type: 'kline_batch'; data: KlineData[] }
  | { type: 'market_regime'; data: MarketRegime }
  | { type: 'trade'; data: TradeRecord }
  | { type: 'state'; data: StrategyState }
  | { type: 'pong' };
