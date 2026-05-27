/**
 * CBBC AI 决策顾问卡 (read-only,纯展示)
 * =====================================
 * 调用 ``POST /api/cbbc/ai-advice`` 触发 LLM 端点 (用户本机 OpenAI 兼容代理),
 * 把模型返回的结构化建议 (direction / 入场区 / 止盈 / 止损) 显示出来。
 *
 * 设计要点:
 * - 完全独立组件,**不接入任何交易决策路径**。
 * - 默认不主动请求 (避免每次磁吸刷新都打 LLM);仅在用户点击 "🔄 询问 AI" 时触发。
 * - 失败 / 超时 / 端点不可达统一显示为 ``error`` 状态,不会让页面崩溃。
 * - 后端默认关闭 ``cbbc_ai_advisor_enabled``;启用前需在配置里填好 api_key。
 */
import { useState } from 'react';
import { Button, Tag, Tooltip, message } from 'antd';
import { ReloadOutlined, RobotOutlined } from '@ant-design/icons';
import { colors } from '../theme';
import { requestCbbcAiAdvice } from '../api';
import type { CbbcAiAdviceResponse } from '../types';

interface Props {
  /** 用户是否已在配置里启用 AI 顾问 (cbbc_ai_advisor_enabled) */
  enabled: boolean;
}

export default function CbbcAiAdvisorCard({ enabled }: Props) {
  const [advice, setAdvice] = useState<CbbcAiAdviceResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [lastFetched, setLastFetched] = useState<string | null>(null);

  const onClick = async () => {
    setLoading(true);
    try {
      const resp = await requestCbbcAiAdvice();
      setAdvice(resp);
      setLastFetched(new Date().toLocaleTimeString('zh-CN'));
      if (!resp.ok) {
        message.warning(`AI 顾问返回错误: ${resp.error ?? '未知'}`);
      }
    } catch (e) {
      const errorMsg = e instanceof Error ? e.message : String(e);
      setAdvice({ ok: false, error: errorMsg });
      message.error(`请求失败: ${errorMsg}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={cardStyle}>
      <div style={titleRowStyle}>
        <span>
          <RobotOutlined style={{ marginRight: 6 }} />
          AI 决策建议 (read-only)
        </span>
        <Tooltip
          title={
            enabled
              ? '点击后端 LLM 端点获取一次性建议 — 不影响交易'
              : '请先在 ControlPanel 启用 cbbc_ai_advisor_enabled 并填好 api_key'
          }
        >
          <Button
            size="small"
            type="primary"
            icon={<ReloadOutlined />}
            loading={loading}
            disabled={!enabled}
            onClick={onClick}
          >
            询问 AI
          </Button>
        </Tooltip>
      </div>

      {!enabled && (
        <div style={emptyHintStyle}>
          AI 顾问已关闭。在配置区启用 ``cbbc_ai_advisor_enabled`` 并填好 api_key/base_url 后,
          点击右上角按钮即可获取一次性建议。
        </div>
      )}

      {enabled && !advice && !loading && (
        <div style={emptyHintStyle}>
          点击 "询问 AI" 让模型基于当前 zones / 磁吸 / 市况 给出方向 + 入场 + 止盈止损 建议。
        </div>
      )}

      {advice?.ok && advice.direction && <AdviceBody advice={advice} />}
      {advice && !advice.ok && (
        <div style={errorBodyStyle}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>⚠ AI 顾问出错</div>
          <div style={{ fontSize: 11, color: colors.textSecondary }}>{advice.error}</div>
          {advice.error?.includes('速率限制') && (
            <div style={{ marginTop: 6, fontSize: 11, color: colors.textMuted, lineHeight: 1.6 }}>
              💡 提示:代理被打爆了。可以稍等再试,或在配置区把 ``cbbc_ai_advisor_model`` 改为 ``claude-haiku-4-7`` (更轻量,通常没那么挤)。
            </div>
          )}
          {advice.raw_model_text && (
            <details style={{ marginTop: 6 }}>
              <summary style={{ fontSize: 10, color: colors.textMuted, cursor: 'pointer' }}>
                模型原始输出
              </summary>
              <pre style={{
                fontSize: 10,
                background: colors.bgInput,
                padding: 6,
                marginTop: 4,
                borderRadius: 3,
                whiteSpace: 'pre-wrap',
                maxHeight: 120,
                overflow: 'auto',
              }}>{advice.raw_model_text}</pre>
            </details>
          )}
        </div>
      )}

      {advice?.ok && (
        <div style={metaRowStyle}>
          <span>模型: {advice.model ?? '—'}</span>
          {advice.elapsed_seconds != null && (
            <span>耗时: {advice.elapsed_seconds.toFixed(2)}s</span>
          )}
          {lastFetched && <span>更新: {lastFetched}</span>}
        </div>
      )}
    </div>
  );
}


function AdviceBody({ advice }: { advice: CbbcAiAdviceResponse }) {
  const dir = advice.direction;
  const skipRecommended = dir === 'skip';
  const accent = dir === 'bull' ? colors.bull : dir === 'bear' ? colors.bear : colors.textMuted;
  const dirLabel =
    dir === 'bull' ? '🐂 做多 BULL' :
    dir === 'bear' ? '🐻 做空 BEAR' :
    '⏸ 观望 SKIP';
  const confidencePct = advice.confidence != null
    ? `${Math.round(advice.confidence * 100)}%` : '—';

  return (
    <div style={{ marginTop: 10 }}>
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '8px 10px',
        borderLeft: `3px solid ${accent}`,
        background: colors.bgInput,
        borderRadius: 4,
      }}>
        <span style={{ color: accent, fontSize: 14, fontWeight: 700 }}>{dirLabel}</span>
        <Tag color={advice.confidence && advice.confidence >= 0.6 ? 'green' : 'orange'}>
          信心 {confidencePct}
        </Tag>
      </div>

      {!skipRecommended && (
        <div style={{ marginTop: 10, fontSize: 11, lineHeight: 1.7 }}>
          <PriceRow label="入场区" value={
            advice.entry_low != null && advice.entry_high != null
              ? `${advice.entry_low.toFixed(0)} - ${advice.entry_high.toFixed(0)}`
              : '—'
          } color={colors.text} bold />
          <PriceRow
            label="止盈 1"
            value={advice.take_profit_1?.toFixed(0) ?? '—'}
            color={accent}
            bold
          />
          {advice.take_profit_2 != null
            && advice.take_profit_2 !== advice.take_profit_1 && (
            <PriceRow
              label="止盈 2"
              value={advice.take_profit_2.toFixed(0)}
              color={accent}
            />
          )}
          <PriceRow
            label="止损"
            value={advice.stop_loss?.toFixed(0) ?? '—'}
            color={colors.warning}
            bold
          />
        </div>
      )}

      {advice.rationale && (
        <div style={{
          marginTop: 10,
          padding: '6px 8px',
          background: colors.bgInput,
          borderRadius: 3,
          fontSize: 11,
          lineHeight: 1.6,
          color: colors.textSecondary,
        }}>
          💡 {advice.rationale}
        </div>
      )}
    </div>
  );
}


function PriceRow({
  label, value, color, bold,
}: { label: string; value: string; color: string; bold?: boolean }) {
  return (
    <div style={{
      display: 'flex',
      justifyContent: 'space-between',
      fontFamily: "'SF Mono', 'Cascadia Code', 'Consolas', monospace",
    }}>
      <span style={{ color: colors.textMuted }}>{label}</span>
      <span style={{ color, fontWeight: bold ? 700 : 500 }}>{value}</span>
    </div>
  );
}


const cardStyle: React.CSSProperties = {
  background: colors.bgCard,
  border: `1px solid ${colors.border}`,
  borderRadius: 10,
  padding: '14px 16px',
  height: '100%',
};

const titleRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  fontSize: 12,
  color: colors.textSecondary,
  letterSpacing: 0.5,
  fontWeight: 600,
};

const emptyHintStyle: React.CSSProperties = {
  marginTop: 12,
  padding: '10px 12px',
  background: colors.bgInput,
  borderRadius: 4,
  color: colors.textMuted,
  fontSize: 11,
  lineHeight: 1.6,
};

const errorBodyStyle: React.CSSProperties = {
  marginTop: 10,
  padding: '8px 10px',
  background: colors.bgInput,
  borderLeft: `3px solid ${colors.warning}`,
  borderRadius: 4,
  color: colors.warning,
  fontSize: 12,
};

const metaRowStyle: React.CSSProperties = {
  marginTop: 10,
  display: 'flex',
  justifyContent: 'space-between',
  fontSize: 10,
  color: colors.textMuted,
};
