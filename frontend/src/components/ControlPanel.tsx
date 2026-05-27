import { useState, useEffect } from 'react';
import { Button, Checkbox, Input, InputNumber, Form, Select, Switch, message } from 'antd';
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

const strategyOptions = [
  { label: '普通超買/超賣', value: 'normal' },
  { label: '極度策略', value: 'extreme' },
  { label: '放量動能', value: 'momentum' },
  { label: '累積趨勢', value: 'cum_trend' },
  { label: 'RSI背離', value: 'rsi_divergence' },
];

const extremeBranchOptions = [
  { label: 'B1 極度RSI+放量', value: 'b1_volume_extreme' },
  { label: 'B2 非常極端回抽', value: 'b2_very_extreme_pullback' },
  { label: 'B3 完成K補單', value: 'b3_completed_k' },
  { label: 'B4 上下影反轉', value: 'b4_shadow_reversal' },
];

export default function ControlPanel({ isRunning, actionLoading, onConfigLoaded, onStart, onStop, onReset }: Props) {
  const [config, setConfig] = useState<StrategyConfig | null>(null);
  const [loading, setLoading] = useState(false);
  const [form] = Form.useForm();
  const enabledStrategies = Form.useWatch('enabled_strategies', form) || [];
  const hasExtreme = enabledStrategies.includes('extreme');

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
      if (!values.enabled_strategies?.length) {
        message.error('請至少選擇一個實盤策略');
        return;
      }
      if (values.enabled_strategies.includes('extreme') && !values.enabled_extreme_branches?.length) {
        message.error('啟用極度策略時，請至少選擇一個極度分支');
        return;
      }
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
              ['實盤策略', config.enabled_strategies?.length ? `${config.enabled_strategies.length} 個啟用` : '未設定'],
              ['RSI止損取消', config.extreme_rsi_stop_veto_enabled ? `開 / 硬-${config.extreme_rsi_stop_hard_ticks}格` : '關'],
              ['CBBC 磁吸層', config.cbbc_magnet_layer_enabled ? '開' : '關'],
              ['磁吸方向閘門', config.cbbc_magnet_direction_gate_enabled
                ? `開 / |bias|≥${(config.cbbc_magnet_direction_gate_threshold ?? 0.15).toFixed(2)}`
                : '關'],
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
          <Form.Item
            label="可交易策略"
            name="enabled_strategies"
            style={{ marginBottom: 10, gridColumn: '1 / -1' }}
          >
            <Checkbox.Group options={strategyOptions} />
          </Form.Item>
          <Form.Item
            label="極度分支"
            name="enabled_extreme_branches"
            style={{ marginBottom: 10, gridColumn: '1 / -1' }}
          >
            <Checkbox.Group disabled={!hasExtreme} options={extremeBranchOptions} />
          </Form.Item>
          <Form.Item
            label="極端RSI取消止損"
            name="extreme_rsi_stop_veto_enabled"
            valuePropName="checked"
            style={{ marginBottom: 10 }}
          >
            <Switch />
          </Form.Item>
          <Form.Item label="取消後硬止損 (格)" name="extreme_rsi_stop_hard_ticks" style={{ marginBottom: 10 }}>
            <InputNumber min={1} max={10} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item label="重新武裝 (格)" name="extreme_rsi_stop_rearm_ticks" style={{ marginBottom: 10 }}>
            <InputNumber min={1} max={10} style={{ width: '100%' }} />
          </Form.Item>
          {/* CBBC 磁吸层 + 方向闸门 (cbbc-magnet-signal UX 增强) */}
          <Form.Item
            label="CBBC 磁吸层"
            name="cbbc_magnet_layer_enabled"
            valuePropName="checked"
            style={{ marginBottom: 10 }}
            tooltip="启用后,extreme 反转分支会咨询 CBBC 街货密集带是否阻止入场"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            label="磁吸方向闸门"
            name="cbbc_magnet_direction_gate_enabled"
            valuePropName="checked"
            style={{ marginBottom: 10 }}
            tooltip="启用后,|bias| 超过阈值时只准跟随磁吸方向入场:bias>0 警惕做多 → 只准 BEAR;bias<0 警惕做空 → 只准 BULL"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            label="闸门阈值 |bias|"
            name="cbbc_magnet_direction_gate_threshold"
            style={{ marginBottom: 10 }}
            tooltip="|bias| 大于等于此值才触发闸门;0.15 = 中性区间 ±0.15"
          >
            <InputNumber min={0} max={1} step={0.05} style={{ width: '100%' }} />
          </Form.Item>

          {/* CBBC AI 决策顾问 (read-only, 不影响交易) */}
          <Form.Item
            label="🤖 AI 决策顾问"
            name="cbbc_ai_advisor_enabled"
            valuePropName="checked"
            style={{ marginBottom: 10 }}
            tooltip="启用后, StatusPanel 显示一个 'AI 决策建议' 卡片,可手动触发一次性请求。read-only,完全不影响交易决策。"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            label="AI base_url"
            name="cbbc_ai_advisor_base_url"
            style={{ marginBottom: 10 }}
            tooltip="OpenAI 兼容端点根地址,例如本机代理 http://127.0.0.1:8765"
          >
            <Input placeholder="http://127.0.0.1:8765" />
          </Form.Item>
          <Form.Item
            label="AI model"
            name="cbbc_ai_advisor_model"
            style={{ marginBottom: 10 }}
            tooltip="模型名称 (透传给端点);用本地代理时通常忽略此字段"
          >
            <Input placeholder="gpt-4o-mini" />
          </Form.Item>
          <Form.Item
            label="AI api_key"
            name="cbbc_ai_advisor_api_key"
            style={{ marginBottom: 10 }}
            tooltip="OpenAI 兼容 Bearer Token;本机代理可填任意非空字符串"
          >
            <Input.Password placeholder="sk-..." visibilityToggle />
          </Form.Item>
          <Form.Item
            label="AI 协议"
            name="cbbc_ai_advisor_api_style"
            style={{ marginBottom: 10 }}
            tooltip="openai → /v1/chat/completions + Authorization Bearer;anthropic → /v1/messages + x-api-key"
          >
            <Select
              options={[
                { value: 'openai', label: 'OpenAI 兼容 (/v1/chat/completions)' },
                { value: 'anthropic', label: 'Anthropic 原生 (/v1/messages)' },
              ]}
            />
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
