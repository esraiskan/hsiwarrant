import type {
  StrategyConfig, StrategyState, TradeRecord, KlineData,
  OpenDStatus, AccountInfo, MarketSnapshot, TradeEnvUpdateRequest, TradeEnvUpdateResponse,
  TodayPnl, BacktestRequest, BacktestResult, MarketRegime,
  CbbcAiAdviceResponse, HksiStyleAiAdviceResponse,
} from './types';

const BASE_URL = '/api';

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${url}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let message = `API Error: ${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body?.detail === 'string') {
        message = body.detail;
      }
    } catch {
      // Keep the HTTP status fallback when the response is not JSON.
    }
    throw new Error(message);
  }
  return res.json();
}

/** 获取策略配置 */
export const getConfig = () => request<StrategyConfig>('/config');

/** 更新策略配置 */
export const updateConfig = (config: Partial<StrategyConfig>) =>
  request<{ message: string }>('/config', {
    method: 'PUT',
    body: JSON.stringify(config),
  });

/** 获取策略状态 */
export const getState = () => request<StrategyState>('/state');

/** 启动策略 */
export const startStrategy = () => request<{ message: string }>('/start', { method: 'POST' });

/** 停止策略 */
export const stopStrategy = () => request<{ message: string }>('/stop', { method: 'POST' });

/** 重置策略 */
export const resetStrategy = () => request<{ message: string }>('/reset', { method: 'POST' });

/** 获取交易历史 */
export const getTrades = () => request<TradeRecord[]>('/trades');

/** 获取 K 线历史 */
export const getKlines = () => request<KlineData[]>('/klines');

/** 获取盘中市况 */
export const getMarketRegime = () => request<MarketRegime | null>('/market-regime');

/** 获取 OpenD 连接状态 */
export const getOpenDStatus = () => request<OpenDStatus>('/opend/status');

/** 切换富途交易环境 */
export const setTradeEnv = (payload: TradeEnvUpdateRequest) =>
  request<TradeEnvUpdateResponse>('/opend/trade-env', {
    method: 'POST',
    body: JSON.stringify(payload),
  });

/** 获取恒指实时快照 */
export const getSnapshot = () => request<MarketSnapshot>('/opend/snapshot');

/** 获取账户信息 */
export const getAccount = () => request<AccountInfo>('/opend/account');

/** 获取当前交易环境的当天 P&L */
export const getTodayPnl = () => request<TodayPnl>('/opend/today-pnl');

/** 获取持仓 */
export const getPositions = () => request<Record<string, unknown>[]>('/opend/positions');

/** 获取今日订单 */
export const getOrders = () => request<Record<string, unknown>[]>('/opend/orders');

/** 执行策略回测 */
export const runBacktest = (payload: BacktestRequest) =>
  request<BacktestResult>('/backtest', {
    method: 'POST',
    body: JSON.stringify(payload),
  });

/** 触发一次 CBBC AI 决策建议请求 (read-only, 不影响交易) */
export const requestCbbcAiAdvice = () =>
  request<CbbcAiAdviceResponse>('/cbbc/ai-advice', { method: 'POST' });

/** 觸發一次 HKSI-style AI 分析 (read-only, 不影響交易) */
export const requestHksiStyleAiAdvice = () =>
  request<HksiStyleAiAdviceResponse>('/cbbc/hksi-style-ai-advice', { method: 'POST' });
