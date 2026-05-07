import { useCallback, useState } from 'react';
import { Row, Col, message, ConfigProvider, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import StatusPanel from './components/StatusPanel';
import PriceChart from './components/PriceChart';
import TradeLog from './components/TradeLog';
import ControlPanel from './components/ControlPanel';
import OpenDPanel from './components/OpenDPanel';
import { useWebSocket } from './useWebSocket';
import { colors } from './theme';
import * as api from './api';
import type { StrategyConfig } from './types';

export default function App() {
  const { connected, state, klines, trades, todayPnl, clearData, refreshData, refreshTodayPnl } = useWebSocket();
  const [actionLoading, setActionLoading] = useState<'start' | 'stop' | 'reset' | null>(null);
  const [rsiLength, setRsiLength] = useState(14);

  const handleConfigLoaded = useCallback((config: StrategyConfig) => {
    setRsiLength(config.rsi_length);
  }, []);

  const handleStart = useCallback(async () => {
    try {
      setActionLoading('start');
      const res = await api.startStrategy();
      await refreshData();
      await refreshTodayPnl();
      message.success(res.message);
    } catch { message.error('启动失败，请确认 OpenD 已启动'); }
    finally { setActionLoading(null); }
  }, [refreshData]);

  const handleStop = useCallback(async () => {
    try {
      setActionLoading('stop');
      const res = await api.stopStrategy();
      await refreshData();
      await refreshTodayPnl();
      message.success(res.message);
    }
    catch { message.error('停止失败'); }
    finally { setActionLoading(null); }
  }, [refreshData]);

  const handleReset = useCallback(async () => {
    try {
      setActionLoading('reset');
      await api.resetStrategy();
      clearData();
      await refreshData();
      await refreshTodayPnl();
      message.success('策略已重置');
    }
    catch { message.error('重置失败'); }
    finally { setActionLoading(null); }
  }, [clearData, refreshData]);

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: theme.darkAlgorithm,
        token: {
          colorPrimary: colors.primary,
          borderRadius: 8,
          colorBgContainer: colors.bgCard,
          colorBgElevated: colors.bgCard,
          colorBorder: colors.border,
          colorText: colors.text,
          colorTextSecondary: colors.textSecondary,
        },
      }}
    >
      <div style={{ minHeight: '100vh', background: colors.bg }}>
        {/* Header */}
        <header style={{
          background: colors.bgHeader,
          borderBottom: `1px solid ${colors.border}`,
          padding: '0 24px',
          height: 52,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <span style={{ fontSize: 20 }}>🤖</span>
            <span style={{ fontSize: 16, fontWeight: 700, color: colors.text, letterSpacing: -0.3 }}>
              HSI CBBC Algo
            </span>
            <span style={{
              fontSize: 11, color: colors.textMuted, background: colors.bgInput,
              padding: '2px 8px', borderRadius: 4, marginLeft: 4,
            }}>
              v2.0
            </span>
          </div>
          <div style={{ fontSize: 12, color: colors.textMuted }}>
            恒指牛熊证智能交易系统
          </div>
        </header>

        {/* Content */}
        <div style={{ padding: '16px 20px', maxWidth: 1440, margin: '0 auto' }}>
          <OpenDPanel onTradeEnvChanged={refreshTodayPnl} />
          <StatusPanel state={state} connected={connected} todayPnl={todayPnl} />

          <Row gutter={16}>
            <Col xs={24} lg={17}>
              <PriceChart klines={klines} trades={trades} entryPrice={state?.entry_price ?? 0} rsiLength={rsiLength} />
            </Col>
            <Col xs={24} lg={7}>
              <ControlPanel
                isRunning={state?.is_running ?? false}
                actionLoading={actionLoading}
                onConfigLoaded={handleConfigLoaded}
                onStart={handleStart}
                onStop={handleStop}
                onReset={handleReset}
              />
            </Col>
          </Row>

          <TradeLog trades={trades} />
        </div>
      </div>
    </ConfigProvider>
  );
}
