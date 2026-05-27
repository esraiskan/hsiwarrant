import { useState } from 'react';
import { Alert, Button, Col, Collapse, Empty, Row, Spin, Tag, message } from 'antd';
import {
  ReloadOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import { requestHksiStyleAiAdvice } from '../api';
import { colors } from '../theme';
import type {
  HksiStyleAiAdviceResponse,
  HksiStyleEntryPlan,
  HksiStyleExecutionEntryRule,
  HksiStyleTradePlan,
} from '../types';

interface Props {
  enabled: boolean;
}

export default function HksiStyleAiAdvisorPanel({ enabled }: Props) {
  const [advice, setAdvice] = useState<HksiStyleAiAdviceResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const onRefresh = async () => {
    setLoading(true);
    try {
      const result = await requestHksiStyleAiAdvice();
      setAdvice(result);
      if (result.ok) {
        message.success('AI 分析已更新');
      } else {
        message.warning(result.error || 'AI 分析失敗');
      }
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error);
      setAdvice({ ok: false, generated_at: '', source: 'error', error: errorMessage });
      message.error(errorMessage);
    } finally {
      setLoading(false);
    }
  };

  const review = advice?.review ?? null;

  return (
    <div style={pageStyle}>
      <div style={headerStyle}>
        <div>
          <div style={titleStyle}>
            <RobotOutlined />
            <span>AI 分析</span>
          </div>
          <div style={subtitleStyle}>
            HKSI-style 交易卡審閱，使用本系統 AI 設定，只作 read-only 參考。
          </div>
        </div>
        <Button
          type="primary"
          icon={<ReloadOutlined />}
          loading={loading}
          disabled={!enabled}
          onClick={onRefresh}
        >
          AI 審閱
        </Button>
      </div>

      {!enabled && (
        <Alert
          type="warning"
          showIcon
          style={alertStyle}
          message="AI 顧問未啟用"
          description="請先喺配置區啟用 cbbc_ai_advisor_enabled，並填好 api_key / base_url / model。"
        />
      )}

      {loading && (
        <div style={loadingStyle}>
          <Spin />
          <span>正在整理即市資料並要求 AI 輸出 HKSI-style 交易計劃...</span>
        </div>
      )}

      {!loading && !advice && (
        <div style={emptyStyle}>
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="未有 AI 分析。按右上角 AI 審閱，會用當前 K 線、VWAP、RSI、市況、CBBC zones 同磁吸狀態生成交易卡式建議。"
          />
        </div>
      )}

      {advice && !advice.ok && (
        <Alert
          type="error"
          showIcon
          style={alertStyle}
          message="AI 分析失敗"
          description={advice.error || '未知錯誤'}
        />
      )}

      {review && (
        <>
          <div style={topBarStyle}>
            <div>
              <span style={mutedLabelStyle}>更新時間</span>
              <strong style={metaValueStyle}>{advice?.generated_at || '—'}</strong>
            </div>
            <div>
              <span style={mutedLabelStyle}>模型</span>
              <strong style={metaValueStyle}>{advice?.model || '—'}</strong>
            </div>
            <div>
              <span style={mutedLabelStyle}>耗時</span>
              <strong style={metaValueStyle}>
                {advice?.elapsed_seconds != null ? `${advice.elapsed_seconds.toFixed(2)}s` : '—'}
              </strong>
            </div>
            <div style={{ textAlign: 'right' }}>
              <RiskTag risk={review.risk_level} />
              <ActionabilityTag value={review.actionability} />
            </div>
          </div>

          <TradePlanBox plan={review.trade_plan} />

          <Row gutter={16} style={{ marginTop: 16 }}>
            <Col xs={24} lg={14}>
              <InfoPanel title="支持因素" items={review.key_supporting_points} accent={colors.bull} />
            </Col>
            <Col xs={24} lg={10}>
              <InfoPanel title="矛盾 / 風險" items={review.conflicts} accent={colors.warning} icon="warning" />
            </Col>
          </Row>

          <Row gutter={16} style={{ marginTop: 16 }}>
            <Col xs={24} lg={12}>
              <InfoPanel title="觀察位" items={review.watch_levels} accent={colors.primary} />
            </Col>
            <Col xs={24} lg={12}>
              <InfoPanel title="資料質素 / 限制" items={[...review.data_quality_notes, ...review.limitations]} accent={colors.textSecondary} />
            </Col>
          </Row>

          <ExecutionPanel advice={review} />

          <div style={summaryStyle}>
            <SafetyCertificateOutlined />
            <span>{review.suggested_user_action || review.summary}</span>
          </div>
        </>
      )}
    </div>
  );
}

function TradePlanBox({ plan }: { plan: HksiStyleTradePlan }) {
  return (
    <div style={tradePlanStyle}>
      <PlanRow label="主方向" value={<DirectionText text={plan.main_direction} />} strong />
      <PlanRow label="暫時" value={<StatusTag status={plan.status} />} strong />
      <PlanRow label="第一選擇" value={formatEntryPlan(plan.entry_plan_1)} />
      <PlanRow label="第二選擇" value={formatEntryPlan(plan.entry_plan_2)} />
      <PlanRow label="止蝕" value={formatList(plan.stop_loss)} />
      <PlanRow label="食糊" value={formatList(plan.take_profit)} />
      <PlanRow label="放棄條件" value={formatList(plan.give_up_conditions)} />
      <div style={warningLineStyle}>
        <WarningOutlined />
        <span>{plan.product_warning || '牛熊證避免太近收回價，需預留足夠安全距離。'}</span>
      </div>
      <div style={planSummaryStyle}>{plan.summary || '方向仍需確認，先以風險控制為主。'}</div>
    </div>
  );
}

function PlanRow({
  label,
  value,
  strong,
}: {
  label: string;
  value: React.ReactNode;
  strong?: boolean;
}) {
  return (
    <div style={planRowStyle}>
      <span style={planLabelStyle}>{label}</span>
      <strong style={{ ...planValueStyle, fontWeight: strong ? 800 : 650 }}>{value}</strong>
    </div>
  );
}

function InfoPanel({
  title,
  items,
  accent,
  icon,
}: {
  title: string;
  items: string[];
  accent: string;
  icon?: 'warning';
}) {
  return (
    <div style={panelStyle}>
      <div style={{ ...panelTitleStyle, color: accent }}>
        {icon === 'warning' && <WarningOutlined />}
        <span>{title}</span>
      </div>
      {items.length ? (
        <ul style={listStyle}>
          {items.map((item, index) => (
            <li key={`${title}-${index}`}>{item}</li>
          ))}
        </ul>
      ) : (
        <div style={emptyHintStyle}>暫無資料</div>
      )}
    </div>
  );
}

function ExecutionPanel({ advice }: { advice: NonNullable<HksiStyleAiAdviceResponse['review']> }) {
  const { execution_plan: execution, trade_coefficients: coefficients } = advice;
  return (
    <Collapse
      style={collapseStyle}
      items={[
        {
          key: 'execution',
          label: '機器可讀入場 / 止盈止損參數',
          children: (
            <div style={{ display: 'grid', gap: 14 }}>
              <div style={metricGridStyle}>
                <Metric label="執行狀態" value={execution.enabled ? 'Enabled' : 'Disabled'} accent={execution.enabled ? colors.bull : colors.textSecondary} />
                <Metric label="方向" value={execution.side} accent={directionColor(execution.side)} />
                <Metric label="最大金額" value={formatCurrency(execution.max_position_hkd)} />
                <Metric label="係數信心" value={`${Math.round((coefficients.confidence || 0) * 100)}%`} />
                <Metric label="入場區" value={formatRange(coefficients.entry_min, coefficients.entry_max)} />
                <Metric label="止賺區" value={formatRange(coefficients.take_profit_min, coefficients.take_profit_max)} accent={colors.bull} />
                <Metric label="止蝕區" value={formatRange(coefficients.stop_loss_min, coefficients.stop_loss_max)} accent={colors.warning} />
                <Metric label="建議股數" value={coefficients.share_count ? coefficients.share_count.toLocaleString('en-HK') : '0'} />
              </div>
              {execution.entry_rules.length ? (
                <div style={{ display: 'grid', gap: 8 }}>
                  {execution.entry_rules.map((rule, index) => (
                    <ExecutionRuleRow key={`${rule.label}-${index}`} rule={rule} />
                  ))}
                </div>
              ) : (
                <div style={emptyHintStyle}>暫無正式 entry rule</div>
              )}
              {execution.notes && <div style={noteStyle}>{execution.notes}</div>}
            </div>
          ),
        },
      ]}
    />
  );
}

function ExecutionRuleRow({ rule }: { rule: HksiStyleExecutionEntryRule }) {
  const actionLabel = formatExecutionAction(rule.action);
  const shouldShowAmount = rule.action === 'buy_bull' || rule.action === 'buy_bear';

  return (
    <div style={ruleRowStyle}>
      <div>
        <strong style={{ color: colors.text }}>{rule.label || `Rule ${rule.priority}`}</strong>
        <div style={{ color: colors.textMuted, fontSize: 11, marginTop: 4 }}>{rule.comment || '—'}</div>
      </div>
      <div style={ruleMetaStyle}>
        <Tag color={rule.action === 'buy_bull' ? 'green' : rule.action === 'buy_bear' ? 'red' : 'default'}>
          {actionLabel}
        </Tag>
        <span>{formatRange(rule.price_min, rule.price_max)}</span>
        <span>RSI {rule.rsi_min.toFixed(0)}-{rule.rsi_max.toFixed(0)}</span>
        <span>VWAP {rule.vwap_relation}</span>
        <span>{shouldShowAmount ? formatCurrency(rule.amount) : '不落單'}</span>
      </div>
    </div>
  );
}

function Metric({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: string;
}) {
  return (
    <div style={metricStyle}>
      <span>{label}</span>
      <strong style={{ color: accent || colors.text }}>{value}</strong>
    </div>
  );
}

function RiskTag({ risk }: { risk: string }) {
  const color = risk === 'high' ? 'red' : risk === 'medium' ? 'orange' : 'green';
  const label = risk === 'high' ? '高風險' : risk === 'medium' ? '中風險' : '低風險';
  return <Tag color={color} style={{ marginRight: 8 }}>{label}</Tag>;
}

function ActionabilityTag({ value }: { value: string }) {
  const label = ({
    observe: '觀察',
    wait_for_confirmation: '等確認',
    reduce_size: '降低注碼',
    avoid_trade: '避免交易',
  } as Record<string, string>)[value] || value;
  return <Tag color={value === 'avoid_trade' ? 'red' : value === 'reduce_size' ? 'green' : 'blue'}>{label}</Tag>;
}

function StatusTag({ status }: { status: string }) {
  const color = status === '不交易' ? 'red' : status === '等待確認' ? 'orange' : status === '空倉等待' ? 'green' : 'blue';
  return <Tag color={color} style={{ margin: 0 }}>{status}</Tag>;
}

function DirectionText({ text }: { text: string }) {
  const color = text.includes('[up]') ? colors.bull : text.includes('[down]') ? colors.bear : colors.text;
  return <span style={{ color }}>{text}</span>;
}

function formatEntryPlan(plan: HksiStyleEntryPlan): string {
  const condition = plan.condition || '--';
  const action = plan.action || '不做';
  if (action === '不做' || !Number.isFinite(plan.amount) || plan.amount <= 0) {
    return `${condition}，暫不入場，繼續觀察`;
  }
  return `${condition}，${action}，注碼 ${formatCurrency(plan.amount)}`;
}

function formatList(items: string[]): string {
  return items.filter(Boolean).join('；') || '--';
}

function formatCurrency(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '$0';
  return `$${value.toLocaleString('en-HK', { maximumFractionDigits: 0 })}`;
}

function formatRange(low: number, high: number): string {
  if (!low && !high) return '0';
  if (low === high) return low.toFixed(0);
  return `${low.toFixed(0)}-${high.toFixed(0)}`;
}

function formatExecutionAction(action: string): string {
  return ({
    buy_bull: '買升',
    buy_bear: '買跌',
    none: '不落單',
  } as Record<string, string>)[action] || action;
}

function directionColor(side: string): string {
  if (side === 'bull') return colors.bull;
  if (side === 'bear') return colors.bear;
  return colors.textSecondary;
}

const pageStyle: React.CSSProperties = {
  display: 'grid',
  gap: 16,
};

const headerStyle: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  gap: 18,
  alignItems: 'center',
  padding: '18px 20px',
  background: colors.bgCard,
  border: `1px solid ${colors.border}`,
  borderRadius: 8,
};

const titleStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  color: colors.text,
  fontSize: 20,
  fontWeight: 800,
};

const subtitleStyle: React.CSSProperties = {
  marginTop: 6,
  color: colors.textSecondary,
  fontSize: 13,
};

const alertStyle: React.CSSProperties = {
  borderRadius: 8,
};

const loadingStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  padding: 24,
  color: colors.textSecondary,
  background: colors.bgCard,
  border: `1px solid ${colors.border}`,
  borderRadius: 8,
};

const emptyStyle: React.CSSProperties = {
  padding: 34,
  background: colors.bgCard,
  border: `1px solid ${colors.border}`,
  borderRadius: 8,
};

const topBarStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(4, minmax(0, 1fr))',
  gap: 12,
  padding: '14px 16px',
  background: colors.bgCard,
  border: `1px solid ${colors.border}`,
  borderRadius: 8,
};

const mutedLabelStyle: React.CSSProperties = {
  display: 'block',
  color: colors.textMuted,
  fontSize: 11,
};

const metaValueStyle: React.CSSProperties = {
  display: 'block',
  marginTop: 4,
  color: colors.text,
  fontSize: 13,
};

const tradePlanStyle: React.CSSProperties = {
  display: 'grid',
  gap: 0,
  padding: '14px 18px',
  background: colors.bgCard,
  border: `1px solid rgba(245, 158, 11, 0.34)`,
  borderRadius: 8,
};

const planRowStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: '120px minmax(0, 1fr)',
  gap: 16,
  padding: '12px 0',
  borderBottom: `1px solid ${colors.border}`,
};

const planLabelStyle: React.CSSProperties = {
  color: colors.textSecondary,
  fontSize: 13,
};

const planValueStyle: React.CSSProperties = {
  color: colors.text,
  fontSize: 17,
  lineHeight: 1.55,
};

const warningLineStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'flex-start',
  gap: 8,
  padding: '14px 0 8px',
  color: '#f7d37a',
  lineHeight: 1.6,
  fontSize: 15,
};

const planSummaryStyle: React.CSSProperties = {
  paddingTop: 8,
  color: colors.text,
  fontWeight: 800,
  fontSize: 18,
  lineHeight: 1.6,
};

const panelStyle: React.CSSProperties = {
  height: '100%',
  padding: '16px 18px',
  background: colors.bgCard,
  border: `1px solid ${colors.border}`,
  borderRadius: 8,
};

const panelTitleStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  marginBottom: 10,
  fontSize: 13,
  fontWeight: 800,
};

const listStyle: React.CSSProperties = {
  margin: 0,
  paddingLeft: 18,
  color: colors.text,
  lineHeight: 1.75,
  fontSize: 14,
};

const emptyHintStyle: React.CSSProperties = {
  color: colors.textMuted,
  fontSize: 13,
  padding: '8px 0',
};

const collapseStyle: React.CSSProperties = {
  marginTop: 16,
  background: colors.bgCard,
  border: `1px solid ${colors.border}`,
  borderRadius: 8,
};

const metricGridStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(4, minmax(0, 1fr))',
  gap: 10,
};

const metricStyle: React.CSSProperties = {
  padding: '10px 12px',
  background: colors.bgInput,
  border: `1px solid ${colors.border}`,
  borderRadius: 6,
};

const ruleRowStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'minmax(220px, 1fr) auto',
  gap: 16,
  alignItems: 'center',
  padding: '10px 12px',
  background: colors.bgInput,
  border: `1px solid ${colors.border}`,
  borderRadius: 6,
};

const ruleMetaStyle: React.CSSProperties = {
  display: 'flex',
  gap: 8,
  alignItems: 'center',
  flexWrap: 'wrap',
  color: colors.textSecondary,
  fontFamily: "'SF Mono', 'Cascadia Code', 'Consolas', monospace",
  fontSize: 12,
};

const noteStyle: React.CSSProperties = {
  color: colors.textSecondary,
  lineHeight: 1.6,
};

const summaryStyle: React.CSSProperties = {
  display: 'flex',
  gap: 10,
  alignItems: 'flex-start',
  padding: '14px 16px',
  background: colors.bgInput,
  border: `1px solid ${colors.border}`,
  borderLeft: `3px solid ${colors.primary}`,
  borderRadius: 8,
  color: colors.textSecondary,
  lineHeight: 1.65,
};
