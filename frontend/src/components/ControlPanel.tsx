import { useState, useEffect } from 'react';
import { Button, Input, InputNumber, Form, Select, message } from 'antd';
import {
  PlayCircleOutlined,
  PauseCircleOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { colors } from '../theme';
import * as api from '../api';
import type { StrategyConfig } from '../types';

interface Props {
  isRunning: boolean;
  actionLoading: 'start' | 'stop' | 'reset' | null;
  onConfigLoaded?: (config: StrategyConfig) => void;
  onStart: () => void;
  onStop: () => void;
  onReset: () => void;
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div style={{
      fontSize: 11, color: colors.textMuted, letterSpacing: 0.5,
      textTransform: 'uppercase', marginBottom: 10, marginTop: 16,
    }}>
      {children}
    </div>
  );
}

export default function ControlPanel({ isRunning, actionLoading, onConfigLoaded, onStart, onStop, onReset }: Props) {
  const [config, setConfig] = useState<StrategyConfig | null>(null);
  const [loading, setLoading] = useState(false);
  const [form] = Form.useForm();

  useEffect(() => {
    api.getConfig().then((loaded) => {
      setConfig(loaded);
      onConfigLoaded?.(loaded);
    }).catch(() => {});
  }, [onConfigLoaded]);

  useEffect(() => {
    if (config) form.setFieldsValue(config);
  }, [config, form]);

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      setLoading(true);
      await api.updateConfig(values);
      const updated = await api.getConfig();
      setConfig(updated);
      onConfigLoaded?.(updated);
      message.success('配置已更新');
    } catch { message.error('保存失败'); }
    finally { setLoading(false); }
  };

  return (
    <div style={{
      background: colors.bgCard,
      border: `1px solid ${colors.border}`,
      borderRadius: 10,
      padding: 20,
      height: '100%',
    }}>
      <div style={{ fontSize: 13, color: colors.textSecondary, marginBottom: 16, letterSpacing: 0.5 }}>
        ⚙️ CONTROL
      </div>

      {/* 操作按钮 */}
      <div style={{ display: 'flex', gap: 10, marginBottom: 8 }}>
        {!isRunning ? (
          <Button
            type="primary"
            icon={<PlayCircleOutlined />}
            size="large"
            onClick={onStart}
            loading={actionLoading === 'start'}
            disabled={actionLoading !== null}
            block
            style={{
              background: colors.up, borderColor: colors.up, color: '#000',
              fontWeight: 700, height: 48, borderRadius: 10, fontSize: 15,
            }}
          >
            启动策略
          </Button>
        ) : (
          <Button
            icon={<PauseCircleOutlined />}
            size="large"
            onClick={onStop}
            loading={actionLoading === 'stop'}
            disabled={actionLoading !== null}
            block
            style={{
              background: colors.down, borderColor: colors.down, color: '#fff',
              fontWeight: 700, height: 48, borderRadius: 10, fontSize: 15,
            }}
          >
            停止策略
          </Button>
        )}
        <Button
          icon={<ReloadOutlined />}
          size="large"
          onClick={onReset}
          loading={actionLoading === 'reset'}
          disabled={isRunning || actionLoading !== null}
          style={{
            background: colors.bgInput, borderColor: colors.borderLight, color: colors.textSecondary,
            height: 48, borderRadius: 10, width: 48, minWidth: 48,
          }}
        />
      </div>

      {/* 当前配置 */}
      {config && (
        <>
          <SectionTitle>当前配置</SectionTitle>
          <div style={{
            display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8,
          }}>
            {[
              ['止盈止损', `±${config.stop_points} 点`],
              ['极度止损', `±${config.extreme_stop_points} 点`],
              ['换股比率', config.er_ratio.toLocaleString()],
              ['开仓数量', `${config.share_count.toLocaleString()} 份`],
              ['RSI 周期', `${config.rsi_length}`],
              ['掛單等待', `${config.entry_order_wait_seconds} 秒`],
              ['牛证', config.bull_warrant_code ? `${config.bull_warrant_code} ${config.bull_warrant_name || ''}` : '未设置'],
              ['熊证', config.bear_warrant_code ? `${config.bear_warrant_code} ${config.bear_warrant_name || ''}` : '未设置'],
            ].map(([label, value]) => (
              <div key={label as string} style={{
                background: colors.bgInput, borderRadius: 8, padding: '8px 12px',
              }}>
                <div style={{ fontSize: 10, color: colors.textMuted }}>{label}</div>
                <div style={{ fontSize: 13, fontWeight: 600, fontFamily: "'SF Mono', monospace" }}>{value}</div>
              </div>
            ))}
          </div>
        </>
      )}

      {/* 参数调整 */}
      <SectionTitle>参数调整</SectionTitle>
      <Form form={form} layout="vertical" size="small">
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 12px' }}>
          <Form.Item label="牛证 Number" name="bull_warrant_code" style={{ marginBottom: 10 }}>
            <Input
              placeholder="例如 61234"
              style={{
                background: colors.upBg,
                borderColor: colors.up,
              }}
            />
          </Form.Item>
          <Form.Item label="熊证 Number" name="bear_warrant_code" style={{ marginBottom: 10 }}>
            <Input
              placeholder="例如 61234"
              style={{
                background: colors.downBg,
                borderColor: colors.down,
              }}
            />
          </Form.Item>
          <Form.Item label="RSI 周期" name="rsi_length" style={{ marginBottom: 10, gridColumn: '1 / -1' }}>
            <Select
              options={[
                { value: 6, label: '6' },
                { value: 8, label: '8' },
                { value: 10, label: '10' },
                { value: 12, label: '12' },
                { value: 14, label: '14' },
              ]}
            />
          </Form.Item>
          <Form.Item label="RSI 超卖" name="rsi_oversold" style={{ marginBottom: 10 }}>
            <InputNumber min={5} max={40} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item label="RSI 超买" name="rsi_overbought" style={{ marginBottom: 10 }}>
            <InputNumber min={60} max={95} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item label="目标盈亏 (HKD)" name="target_pnl" style={{ marginBottom: 10 }}>
            <InputNumber min={100} max={10000} step={100} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item label="开仓数量" name="share_count" style={{ marginBottom: 10 }}>
            <InputNumber min={1} step={1000} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item label="极度止损 (HKD)" name="extreme_stop_pnl" style={{ marginBottom: 10 }}>
            <InputNumber min={100} max={10000} step={100} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item label="轮询间隔 (秒)" name="poll_interval" style={{ marginBottom: 10 }}>
            <InputNumber min={1} max={60} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item label="掛單等待 (秒)" name="entry_order_wait_seconds" style={{ marginBottom: 10 }}>
            <InputNumber min={5} max={300} style={{ width: '100%' }} />
          </Form.Item>
        </div>
        <Button
          type="primary"
          onClick={handleSave}
          loading={loading}
          disabled={isRunning}
          block
          style={{
            background: colors.primary, borderColor: colors.primary,
            borderRadius: 8, fontWeight: 600, marginTop: 4,
          }}
        >
          保存配置
        </Button>
      </Form>
    </div>
  );
}
