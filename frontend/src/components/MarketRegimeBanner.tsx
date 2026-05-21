import { Alert, Col, Row, Tag, Tooltip } from 'antd';
import { colors } from '../theme';
import type { MarketRegime } from '../types';

interface Props {
  marketRegime: MarketRegime | null;
}

const biasColor: Record<MarketRegime['bias'], string> = {
  bullish: colors.up,
  bearish: colors.down,
  neutral: colors.info,
  repair: colors.warning,
};

function fmt(value: number | null | undefined, decimals = 0) {
  return typeof value === 'number' ? value.toFixed(decimals) : '—';
}

function getTradeWarning(regime: MarketRegime): { text: string; color: string } {
  if (regime.regime === 'breakdown_failure_repair' || regime.regime === 'repair_warning') {
    return { text: '暫停買熊', color: colors.down };
  }
  if (regime.regime === 'early_repair_rally') {
    return { text: '暫停買熊', color: colors.down };
  }
  if (regime.regime === 'weak_continuation' || regime.regime === 'weak_bounce') {
    return { text: '暫停買牛', color: colors.up };
  }
  if (regime.regime === 'strong_trend') {
    return { text: '暫停買熊', color: colors.down };
  }
  return { text: '兩邊等確認', color: colors.warning };
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div style={{
      minWidth: 116,
      padding: '6px 10px',
      background: colors.bgInput,
      border: `1px solid ${colors.border}`,
      borderRadius: 6,
    }}>
      <div style={{ color: colors.textMuted, fontSize: 10, marginBottom: 2 }}>{label}</div>
      <div style={{ color: colors.text, fontSize: 13, fontWeight: 700 }}>{value}</div>
    </div>
  );
}

export default function MarketRegimeBanner({ marketRegime }: Props) {
  if (!marketRegime) {
    return (
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12, background: colors.bgCard, borderColor: colors.border }}
        message="市況判斷：等待 1 分鐘 K 線資料"
        description="此功能只作實時顯示和 RSI 門檻建議，不會影響現有交易邏輯。建議每分鐘計算一次，市況標籤需確認後先切換。"
      />
    );
  }

  const accent = biasColor[marketRegime.bias];
  const overbought = `${fmt(marketRegime.suggested_rsi_overbought_low)}-${fmt(marketRegime.suggested_rsi_overbought_high)}`;
  const oversold = `${fmt(marketRegime.suggested_rsi_oversold_low)}-${fmt(marketRegime.suggested_rsi_oversold_high)}`;
  const reasons = marketRegime.reasons.length > 0 ? marketRegime.reasons.join('；') : '暫無明確原因';
  const tradeWarning = getTradeWarning(marketRegime);

  return (
    <div style={{
      marginBottom: 12,
      padding: '12px 14px',
      background: colors.bgCard,
      border: `1px solid ${colors.border}`,
      borderLeft: `4px solid ${accent}`,
      borderRadius: 8,
    }}>
      <Row gutter={[12, 10]} align="middle">
        <Col flex="auto">
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <span style={{ color: colors.textSecondary, fontSize: 12 }}>市況判斷</span>
            <Tag color={accent} style={{ marginInlineEnd: 0, fontWeight: 700 }}>
              {marketRegime.label}
            </Tag>
            <Tag style={{ marginInlineEnd: 0, background: colors.bgInput, borderColor: colors.border, color: colors.textSecondary }}>
              信心 {marketRegime.confidence}%
            </Tag>
            <Tag color={tradeWarning.color} style={{ marginInlineEnd: 0, fontWeight: 700 }}>
              {tradeWarning.text}
            </Tag>
            <span style={{ color: colors.textMuted, fontSize: 12 }}>
              更新 {marketRegime.updated_at || '—'} · 每 {marketRegime.update_interval_seconds} 秒計算
            </span>
          </div>
          <Tooltip title={reasons}>
            <div style={{
              color: colors.text,
              fontSize: 13,
              marginTop: 6,
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}>
              {marketRegime.advice}
            </div>
          </Tooltip>
          <div style={{ color: tradeWarning.color, fontSize: 12, marginTop: 4, fontWeight: 700 }}>
            {tradeWarning.text === '暫停買熊'
              ? '当前状态偏向修复或强势，熊证容易被夹。'
              : tradeWarning.text === '暫停買牛'
                ? '当前状态偏向弱势，牛证容易被压。'
                : '先等价格转弱或转强确认，再决定方向。'}
          </div>
        </Col>

        <Col>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
            <Metric label="建議超買 RSI" value={overbought} />
            <Metric label="建議超賣 RSI" value={oversold} />
            <Metric label="區間位置" value={`${fmt(marketRegime.day_position_pct, 1)}%`} />
          </div>
        </Col>
      </Row>
    </div>
  );
}
