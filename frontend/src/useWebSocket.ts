import { useEffect, useRef, useCallback, useState } from 'react';
import type { MutableRefObject } from 'react';
import type { WSMessage, KlineData, TradeRecord, StrategyState, TodayPnl, MarketRegime } from './types';
import * as api from './api';

const WS_PROTOCOL = window.location.protocol === 'https:' ? 'wss' : 'ws';
const WS_URL = `${WS_PROTOCOL}://${window.location.host}/ws`;
const RECONNECT_DELAY = 3000;
const PING_INTERVAL = 15000;
const STORAGE_KEY = 'hsi-dashboard-data-v1';

interface PersistedData {
  state: StrategyState | null;
  klines: KlineData[];
  trades: TradeRecord[];
  marketRegime: MarketRegime | null;
}

const emptyPersistedData: PersistedData = {
  state: null,
  klines: [],
  trades: [],
  marketRegime: null,
};

const HIGH_CONFIDENCE_REGIMES = new Set([
  'weak_continuation',
  'breakdown_failure_repair',
  'strong_trend',
]);

function nextStableMarketRegime(
  current: MarketRegime | null,
  pendingRef: MutableRefObject<{ regime: string; count: number } | null>,
  incoming: MarketRegime,
) {
  if (!current || incoming.regime === current.regime || HIGH_CONFIDENCE_REGIMES.has(incoming.regime)) {
    pendingRef.current = null;
    return incoming;
  }

  const pending = pendingRef.current;
  const count = pending?.regime === incoming.regime ? pending.count + 1 : 1;
  pendingRef.current = { regime: incoming.regime, count };
  return count >= 2 ? incoming : current;
}

function todayKey() {
  const formatter = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Hong_Kong',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  });
  return formatter.format(new Date());
}

function isTodayTrade(trade: TradeRecord) {
  return trade.time?.slice(0, 10) === todayKey();
}

function filterTodayTrades(trades: TradeRecord[]) {
  return trades.filter(isTodayTrade);
}

function readPersistedData(): PersistedData {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return emptyPersistedData;

    const parsed = JSON.parse(raw) as Partial<PersistedData>;
    return {
      state: parsed.state ?? null,
      klines: Array.isArray(parsed.klines) ? parsed.klines : [],
      trades: Array.isArray(parsed.trades) ? filterTodayTrades(parsed.trades) : [],
      marketRegime: parsed.marketRegime ?? null,
    };
  } catch {
    return emptyPersistedData;
  }
}

function writePersistedData(data: PersistedData) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  } catch {
    // Ignore storage quota/private mode errors.
  }
}

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null);
  const pingRef = useRef<ReturnType<typeof setInterval>>();
  const reconnectRef = useRef<ReturnType<typeof setTimeout>>();
  const shouldReconnectRef = useRef(true);
  const initialDataRef = useRef<PersistedData | null>(null);

  if (initialDataRef.current === null) {
    initialDataRef.current = readPersistedData();
  }

  const [connected, setConnected] = useState(false);
  const [state, setState] = useState<StrategyState | null>(initialDataRef.current.state);
  const [klines, setKlines] = useState<KlineData[]>(initialDataRef.current.klines);
  const [trades, setTrades] = useState<TradeRecord[]>(initialDataRef.current.trades);
  const [marketRegime, setMarketRegime] = useState<MarketRegime | null>(initialDataRef.current.marketRegime);
  const [todayPnl, setTodayPnl] = useState<TodayPnl | null>(null);
  const pendingRegimeRef = useRef<{ regime: string; count: number } | null>(null);

  const refreshTodayPnl = useCallback(async () => {
    try {
      setTodayPnl(await api.getTodayPnl());
    } catch {
      setTodayPnl(null);
    }
  }, []);

  const refreshData = useCallback(async () => {
    const [stateResult, tradesResult, klinesResult, todayPnlResult, marketRegimeResult] = await Promise.allSettled([
      api.getState(),
      api.getTrades(),
      api.getKlines(),
      api.getTodayPnl(),
      api.getMarketRegime(),
    ]);

    if (stateResult.status === 'fulfilled') {
      setState(stateResult.value);
    }
    if (tradesResult.status === 'fulfilled') {
      setTrades(filterTodayTrades(tradesResult.value));
    }
    if (klinesResult.status === 'fulfilled') {
      setKlines(klinesResult.value);
    }
    if (todayPnlResult.status === 'fulfilled') {
      setTodayPnl(todayPnlResult.value);
    }
    if (marketRegimeResult.status === 'fulfilled') {
      setMarketRegime(marketRegimeResult.value);
    }
  }, []);

  const connect = useCallback(() => {
    if (
      wsRef.current?.readyState === WebSocket.OPEN ||
      wsRef.current?.readyState === WebSocket.CONNECTING
    ) return;

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      // 心跳
      pingRef.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send('ping');
        }
      }, PING_INTERVAL);
    };

    ws.onmessage = (event) => {
      try {
        const msg: WSMessage = JSON.parse(event.data);
        switch (msg.type) {
          case 'kline':
            // 实时 K 线推送：更新最后一根或追加新根
            setKlines((prev) => {
              if (prev.length > 0 && prev[prev.length - 1].time === msg.data.time) {
                // 同一根 K 线，更新最后一个
                const updated = [...prev];
                updated[updated.length - 1] = msg.data;
                return updated;
              }
              // 新 K 线，追加
              return [...prev.slice(-199), msg.data];
            });
            break;
          case 'kline_batch':
            setKlines(msg.data);
            break;
          case 'market_regime':
            setMarketRegime((prev) => nextStableMarketRegime(prev, pendingRegimeRef, msg.data));
            break;
          case 'trade':
            if (isTodayTrade(msg.data)) {
              setTrades((prev) => [...prev, msg.data]);
            }
            break;
          case 'state':
            setState(msg.data);
            break;
          case 'pong':
            break;
        }
      } catch {
        // ignore parse errors
      }
    };

    ws.onclose = () => {
      setConnected(false);
      if (pingRef.current) clearInterval(pingRef.current);
      // 自动重连
      if (shouldReconnectRef.current) {
        reconnectRef.current = setTimeout(connect, RECONNECT_DELAY);
      }
    };

    ws.onerror = () => {
      ws.close();
    };
  }, []);

  useEffect(() => {
    shouldReconnectRef.current = true;
    let cancelled = false;

    refreshData().finally(() => {
      if (!cancelled) connect();
    });

    return () => {
      cancelled = true;
      shouldReconnectRef.current = false;
      if (pingRef.current) clearInterval(pingRef.current);
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
      wsRef.current?.close();
    };
  }, [connect, refreshData]);

  useEffect(() => {
    const timer = setInterval(refreshTodayPnl, 30000);
    return () => clearInterval(timer);
  }, [refreshTodayPnl]);

  useEffect(() => {
    writePersistedData({ state, klines, trades: filterTodayTrades(trades), marketRegime });
  }, [state, klines, trades, marketRegime]);

  const clearData = useCallback(() => {
    setKlines([]);
    setTrades([]);
    setState(null);
    setMarketRegime(null);
    pendingRegimeRef.current = null;
    try {
      window.localStorage.removeItem(STORAGE_KEY);
    } catch {
      // Ignore storage errors.
    }
  }, []);

  return { connected, state, klines, trades, marketRegime, todayPnl, clearData, refreshData, refreshTodayPnl };
}
