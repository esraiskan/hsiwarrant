/**
 * Mock data for frontend development without backend.
 */
import type {
  StrategyConfig, StrategyState, TradeRecord, KlineData,
  OpenDStatus, AccountInfo, MarketSnapshot, TodayPnl, MarketRegime,
  BacktestResult,
} from '../types';

const now = new Date();
const todayStr = now.toISOString().slice(0, 10);

function timeStr(minutesAgo: number): string {
  const d = new Date(now.getTime() - minutesAgo * 60000);
  return d.toISOString().replace('T', ' ').slice(0, 19);
}

function generateKlines(count: number): KlineData[] {
  const klines: KlineData[] = [];
  let price = 20500;
  for (let i = count; i >= 0; i--) {
    const open = price;
    const change = (Math.random() - 0.48) * 60;
    const close = open + change;
    const high = Math.max(open, close) + Math.random() * 30;
    const low = Math.min(open, close) - Math.random() * 30;
    const volume = Math.floor(5000 + Math.random() * 15000);
    const rsi = 30 + Math.random() * 40;
    const vwap = (open + close + high + low) / 4;
    klines.push({
      time: timeStr(i),
      open: Math.round(open),
      high: Math.round(high),
      low: Math.round(low),
      close: Math.round(close),
      volume,
      rsi: Math.round(rsi * 100) / 100,
      vwap: Math.round(vwap),
      vwap_slope: Math.round((Math.random() - 0.5) * 10 * 100) / 100,
      vol_ma: Math.floor(8000 + Math.random() * 4000),
    });
    price = close;
  }
  return klines;
}

export const mockKlines: KlineData[] = generateKlines(100);
const lastPrice = mockKlines[mockKlines.length - 1].close;

export const mockState: StrategyState = {
  position: 'bull',
  entry_price: lastPrice - 30,
  current_price: lastPrice,
  unrealized_pnl: 30,
  unrealized_pnl_hkd: 1500,
  total_pnl_hkd: 3200,
  breadth_raise_count: 12,
  breadth_fall_count: 8,
  breadth_equal_count: 3,
  breadth_ratio: 0.6,
  breadth_amplitude: 45,
  breadth_time: timeStr(1),
  trade_count: 5,
  win_count: 3,
  loss_count: 2,
  is_running: true,
};

export const mockConfig: StrategyConfig = {
  symbol: 'HK.HSImain',
  er_ratio: 1.5,
  share_count: 10000,
  target_pnl: 50,
  stop_points: 30,
  extreme_stop_pnl: 80,
  extreme_stop_points: 50,
  bull_warrant_code: 'HK.61234',
  bull_warrant_name: '恒指牛A',
  bear_warrant_code: 'HK.62345',
  bear_warrant_name: '恒指熊B',
  rsi_length: 14,
  rsi_oversold: 30,
  rsi_overbought: 70,
  vol_ma_period: 20,
  poll_interval: 3,
  entry_order_wait_seconds: 8,
  entry_cutoff_time: '15:45',
  only_extreme_entries: false,
  enabled_strategies: ['normal', 'extreme'],
  enabled_extreme_branches: ['b1_volume_extreme', 'b3_completed_k'],
  extreme_rsi_stop_veto_enabled: true,
  extreme_rsi_stop_hard_ticks: 5,
  extreme_rsi_stop_rearm_ticks: 3,
};

export const mockTrades: TradeRecord[] = [
  {
    time: `${todayStr} 09:35:00`,
    signal: 'buy_bull',
    price: lastPrice - 80,
    rsi: 28.5,
    position: 'bull',
    pnl: null,
    pnl_hkd: null,
    message: 'RSI超卖，买入牛证',
  },
  {
    time: `${todayStr} 10:12:00`,
    signal: 'take_profit',
    price: lastPrice - 30,
    rsi: 65.2,
    position: 'none',
    pnl: 50,
    pnl_hkd: 2500,
    message: '止盈平仓',
  },
  {
    time: `${todayStr} 11:05:00`,
    signal: 'buy_bull',
    price: lastPrice - 30,
    rsi: 31.0,
    position: 'bull',
    pnl: null,
    pnl_hkd: null,
    message: '极端信号入场',
  },
];

export const mockOpenDStatus: OpenDStatus = {
  host: '127.0.0.1',
  port: 11111,
  trade_env: 'SIMULATE',
  real_unlocked_today: false,
  trade_env_date: todayStr,
  quote_connected: true,
  trade_connected: true,
  strategy_running: true,
};

export const mockAccount: AccountInfo = {
  total_assets: 500000,
  cash: 350000,
  market_val: 150000,
  frozen_cash: 0,
  available_funds: 350000,
};

export const mockSnapshot: MarketSnapshot = {
  code: 'HK.HSImain',
  name: '恒生指数期货',
  last_price: lastPrice,
  open_price: lastPrice - 50,
  high_price: lastPrice + 80,
  low_price: lastPrice - 120,
  volume: 85000,
  turnover: 1750000000,
};

export const mockTodayPnl: TodayPnl = {
  success: true,
  trade_env: 'SIMULATE',
  date: todayStr,
  today_pnl_hkd: 3200,
  source: 'mock',
  message: 'Mock P&L data',
  positions: [
    {
      code: 'HK.61234',
      name: '恒指牛A',
      qty: 10000,
      market_val: 15000,
      today_pl_val: 1500,
      pl_val: 1500,
      unrealized_pl: 1500,
      realized_pl: 1700,
      position_side: 'LONG',
    },
  ],
};

export const mockMarketRegime: MarketRegime = {
  regime: 'range_bound',
  label: '震荡市',
  bias: 'neutral',
  confidence: 0.72,
  suggested_rsi_overbought_low: 68,
  suggested_rsi_overbought_high: 75,
  suggested_rsi_oversold_low: 25,
  suggested_rsi_oversold_high: 32,
  advice: '区间震荡，适合高抛低吸策略',
  reasons: ['日内波幅收窄', 'RSI在40-60区间', '成交量低于均值'],
  updated_at: timeStr(2),
  update_interval_seconds: 300,
  current_price: lastPrice,
  day_open: lastPrice - 50,
  previous_close: lastPrice - 80,
  opening_range_high: lastPrice + 30,
  opening_range_low: lastPrice - 60,
  opening_range_mid: lastPrice - 15,
  day_position_pct: 0.55,
};

export const mockBacktestResult: BacktestResult = {
  requested_start: '2026-04-01',
  requested_end: '2026-04-30',
  data_start: '2026-04-01',
  data_end: '2026-04-30',
  summary: { trades: 42, wins: 26, losses: 16, win_rate: 0.619, pnl_hkd: 18500 },
  monthly: [
    { key: '2026-04', label: '2026年4月', trades: 42, wins: 26, losses: 16, win_rate: 0.619, pnl_hkd: 18500 },
  ],
  daily: [],
  strategy_breakdown: [
    { key: 'normal', label: '普通策略', trades: 28, wins: 17, losses: 11, win_rate: 0.607, pnl_hkd: 11000 },
    { key: 'extreme', label: '极端策略', trades: 14, wins: 9, losses: 5, win_rate: 0.643, pnl_hkd: 7500 },
  ],
  extreme_branch_breakdown: [],
  trades: [],
  warnings: [],
};
