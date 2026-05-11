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

/** WebSocket 消息 */
export type WSMessage =
  | { type: 'kline'; data: KlineData }
  | { type: 'kline_batch'; data: KlineData[] }
  | { type: 'trade'; data: TradeRecord }
  | { type: 'state'; data: StrategyState }
  | { type: 'pong' };
