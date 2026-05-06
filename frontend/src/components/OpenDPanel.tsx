import { useState, useEffect, useCallback } from 'react';
import { Button, Input, Modal, Segmented, message } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { colors } from '../theme';
import * as api from '../api';
import type { OpenDStatus, AccountInfo, MarketSnapshot, TradeEnv } from '../types';

function Dot({ active }: { active: boolean }) {
  return (
    <span style={{
      display: 'inline-block', width: 7, height: 7, borderRadius: '50%',
      background: active ? colors.up : colors.textMuted,
      boxShadow: active ? `0 0 6px ${colors.up}` : 'none',
      marginRight: 6,
    }} />
  );
}

interface Props {
  onTradeEnvChanged?: () => void | Promise<void>;
}

export default function OpenDPanel({ onTradeEnvChanged }: Props) {
  const [status, setStatus] = useState<OpenDStatus | null>(null);
  const [account, setAccount] = useState<AccountInfo | null>(null);
  const [, setSnapshot] = useState<MarketSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [passwordModalOpen, setPasswordModalOpen] = useState(false);
  const [tradePassword, setTradePassword] = useState('');

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [s, a, snap] = await Promise.allSettled([
        api.getOpenDStatus(), api.getAccount(), api.getSnapshot(),
      ]);
      if (s.status === 'fulfilled') setStatus(s.value);
      if (a.status === 'fulfilled') setAccount(a.value);
      if (snap.status === 'fulfilled') setSnapshot(snap.value);
    } catch { /* ignore */ }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30000);
    return () => clearInterval(t);
  }, [refresh]);

  const tradeEnv = status?.trade_env ?? 'SIMULATE';
  const isReal = tradeEnv === 'REAL';

  const switchToSimulate = async () => {
    setSwitching(true);
    try {
      const res = await api.setTradeEnv({ trade_env: 'SIMULATE' });
      if (res.success) message.success(res.message);
      else message.error(res.message || '切换失败');
      await refresh();
      await onTradeEnvChanged?.();
    } catch {
      message.error('切换模拟盘失败');
    } finally {
      setSwitching(false);
    }
  };

  const handleEnvChange = (value: TradeEnv) => {
    if (value === tradeEnv || switching) return;
    if (value === 'REAL') {
      setPasswordModalOpen(true);
      return;
    }
    void switchToSimulate();
  };

  const confirmRealTrade = async () => {
    if (!tradePassword) {
      message.error('请输入交易密码');
      return;
    }
    setSwitching(true);
    try {
      const res = await api.setTradeEnv({ trade_env: 'REAL', trade_password: tradePassword });
      if (res.success) {
        message.success(res.message);
        setPasswordModalOpen(false);
        setTradePassword('');
      } else {
        message.error(res.message || '真实盘解锁失败');
      }
      await refresh();
      await onTradeEnvChanged?.();
    } catch {
      message.error('真实盘解锁失败，请确认 OpenD 已启动');
    } finally {
      setSwitching(false);
    }
  };

  const closePasswordModal = () => {
    if (switching) return;
    setPasswordModalOpen(false);
    setTradePassword('');
  };

  return (
    <>
      <div style={{
        background: `linear-gradient(180deg, ${colors.bgCard} 0%, #111722 100%)`,
        border: `1px solid ${colors.borderLight}`,
        borderRadius: 8,
        padding: '12px 18px',
        marginBottom: 16,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        flexWrap: 'wrap',
        gap: '14px 24px',
        boxShadow: '0 10px 28px rgba(0,0,0,0.18)',
      }}>
      {/* 左：连接状态 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap', minWidth: 360, flex: '1 1 auto' }}>
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          paddingRight: 2,
        }}>
          <div>
            <div style={{
              fontSize: 10,
              color: colors.textMuted,
              marginBottom: 4,
              letterSpacing: 0.6,
              textTransform: 'uppercase',
            }}>OPEND</div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, fontSize: 13 }}>
              <span style={{ display: 'inline-flex', alignItems: 'center', color: colors.textSecondary }}>
                <Dot active={status?.quote_connected ?? false} />
                行情
              </span>
              <span style={{ display: 'inline-flex', alignItems: 'center', color: colors.textSecondary }}>
                <Dot active={status?.trade_connected ?? false} />
                交易
              </span>
            </div>
          </div>
        </div>
        <div style={{
          background: isReal ? 'rgba(239,68,68,0.15)' : 'rgba(99,102,241,0.15)',
          border: `1px solid ${isReal ? 'rgba(255,77,106,0.28)' : 'rgba(99,102,241,0.32)'}`,
          color: isReal ? colors.down : colors.info,
          padding: '4px 11px',
          borderRadius: 6,
          fontSize: 12,
          fontWeight: 700,
          lineHeight: 1,
        }}>
          {isReal ? '真实盘' : '模拟盘'}
        </div>
        <div style={{ minWidth: 170 }}>
          <Segmented
            className={`trade-env-switch ${isReal ? 'trade-env-switch-real' : 'trade-env-switch-simulate'}`}
            size="small"
            value={tradeEnv}
            disabled={switching}
            onChange={(value) => handleEnvChange(value as TradeEnv)}
            options={[
              {
                label: (
                  <span className="trade-env-option">
                    <span className="trade-env-dot trade-env-dot-simulate" />
                    模拟盘
                  </span>
                ),
                value: 'SIMULATE',
              },
              {
                label: (
                  <span className="trade-env-option">
                    <span className="trade-env-dot trade-env-dot-real" />
                    真实盘
                  </span>
                ),
                value: 'REAL',
              },
            ]}
            style={{
              background: colors.bgInput,
              border: `1px solid ${colors.borderLight}`,
              boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.03)',
            }}
          />
          {status?.real_unlocked_today && (
            <div style={{ fontSize: 10, color: colors.textMuted, marginTop: 4 }}>
              今日已输入交易密码
            </div>
          )}
        </div>
      </div>

      {/* 右：账户 + 刷新 */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'flex-end',
        gap: 14,
        minWidth: 220,
      }}>
        {account && !('error' in account) && (
          <div style={{ textAlign: 'right' }}>
            <div style={{
              fontSize: 10,
              color: colors.textMuted,
              marginBottom: 4,
              letterSpacing: 0.6,
              textTransform: 'uppercase',
            }}>可用资金</div>
            <div style={{
              fontSize: 15,
              fontWeight: 800,
              lineHeight: 1,
              color: colors.text,
              fontFamily: "'SF Mono', 'Cascadia Code', monospace",
            }}>
              {account.cash.toLocaleString('en', { minimumFractionDigits: 2 })}
              <span style={{ fontSize: 11, color: colors.textSecondary, marginLeft: 3 }}>HKD</span>
            </div>
          </div>
        )}
        <Button
          icon={<ReloadOutlined />}
          size="small"
          loading={loading}
          onClick={refresh}
          style={{
            background: colors.bgInput,
            border: `1px solid ${colors.borderLight}`,
            color: colors.textSecondary,
            width: 30,
            height: 30,
          }}
        />
      </div>
      </div>
      <Modal
        title="切换到真实盘"
        open={passwordModalOpen}
        okText="解锁并切换"
        cancelText="取消"
        confirmLoading={switching}
        onOk={confirmRealTrade}
        onCancel={closePasswordModal}
        destroyOnClose
      >
        <div style={{ color: colors.textSecondary, marginBottom: 12, lineHeight: 1.6 }}>
          请输入今天的富途交易密码。密码只会用于本次 OpenD 解锁，不会保存在前端或后端文件。
        </div>
        <Input.Password
          autoFocus
          value={tradePassword}
          onChange={(event) => setTradePassword(event.target.value)}
          onPressEnter={confirmRealTrade}
          placeholder="交易密码"
          disabled={switching}
        />
      </Modal>
    </>
  );
}
