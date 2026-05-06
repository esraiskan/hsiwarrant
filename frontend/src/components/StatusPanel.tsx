import { Row, Col } from 'antd';
import { colors } from '../theme';
import type { StrategyState, TodayPnl } from '../types';

interface Props {
  state: StrategyState | null;
  connected: boolean;
  todayPnl: TodayPnl | null;
}

function MetricCard({ label, value, suffix, color, size = 'normal', note }: {
  label: string; value: string | number; suffix?: string; color?: string; size?: 'normal' | 'large';
  note?: string;
}) {
  return (
    <div style={{
      background: colors.bgCard,
      border: `1px solid ${colors.border}`,
      borderRadius: 10,
      padding: size === 'large' ? '16px 20px' : '12px 16px',
      height: '100%',
    }}>
      <div style={{ color: colors.textSecondary, fontSize: 11, letterSpacing: 0.5, textTransform: 'uppercase', marginBottom: 6 }}>
        {label}
      </div>
      <div style={{
        fontSize: size === 'large' ? 28 : 20,
        fontWeight: 700,
        color: color || colors.text,
        fontFamily: "'SF Mono', 'Cascadia Code', 'Consolas', monospace",
        lineHeight: 1.2,
      }}>
        {value}
        {suffix && <span style={{ fontSize: 12, color: colors.textSecondary, marginLeft: 4, fontWeight: 400 }}>{suffix}</span>}
      </div>
      {note && (
        <div style={{ color: colors.textMuted, fontSize: 10, marginTop: 5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {note}
        </div>
      )}
    </div>
  );
}

export default function StatusPanel({ state, connected, todayPnl }: Props) {
  const pos = state?.position ?? 'none';
  const posLabel = pos === 'bull' ? '🐂 牛证' : pos === 'bear' ? '🐻 熊证' : '— 空仓';
  const posColor = pos === 'bull' ? colors.bull : pos === 'bear' ? colors.bear : colors.textMuted;

  const pnlHkd = state?.unrealized_pnl_hkd ?? 0;
  const pnlColor = pnlHkd > 0 ? colors.up : pnlHkd < 0 ? colors.down : colors.textMuted;
  const pnlSign = pnlHkd > 0 ? '+' : '';

  const totalPnl = todayPnl?.success
    ? todayPnl.today_pnl_hkd
    : todayPnl?.fallback_pnl_hkd ?? state?.total_pnl_hkd ?? 0;
  const totalColor = totalPnl > 0 ? colors.up : totalPnl < 0 ? colors.down : colors.textMuted;
  const totalSign = totalPnl > 0 ? '+' : '';
  const totalPnlSource = todayPnl?.source === 'opend_deal_fifo_estimate'
    ? '成交估算'
    : todayPnl?.source === 'opend_position_pl_val'
      ? '持仓P&L'
      : '今日P&L';
  const totalPnlNote = todayPnl?.success
    ? `${todayPnl.trade_env === 'REAL' ? '真实盘' : '模拟盘'} · ${totalPnlSource}`
    : '策略累计';

  const winRate = (state?.trade_count ?? 0) > 0
    ? ((state?.win_count ?? 0) / state!.trade_count * 100).toFixed(1)
    : '—';
  const breadthRaise = state?.breadth_raise_count ?? 0;
  const breadthFall = state?.breadth_fall_count ?? 0;
  const breadthEqual = state?.breadth_equal_count ?? 0;
  const breadthRatio = state?.breadth_ratio ?? null;
  const breadthColor = breadthRatio === null
    ? colors.textMuted
    : breadthRatio > 1
      ? colors.up
      : breadthRatio < 1
        ? colors.down
        : colors.textMuted;
  const breadthNote = breadthRatio === null
    ? '等待数据'
    : `涨 ${breadthRaise} 家 / 跌 ${breadthFall} 家 / 平 ${breadthEqual} 家`;

  return (
    <div style={{ marginBottom: 16 }}>
      {/* 连接状态指示灯 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
        <div style={{
          width: 8, height: 8, borderRadius: '50%',
          background: connected ? colors.up : colors.down,
          boxShadow: connected ? `0 0 8px ${colors.up}` : `0 0 8px ${colors.down}`,
        }} />
        <span style={{ color: colors.textSecondary, fontSize: 12 }}>
          {connected ? 'WebSocket 已连接' : 'WebSocket 未连接'}
          {state?.is_running && ' · 策略运行中'}
        </span>
      </div>

      <Row gutter={[12, 12]}>
        <Col xs={12} sm={8} md={4}>
          <MetricCard label="当前价格" value={(state?.current_price ?? 0).toFixed(2)} suffix="点" size="large" color={colors.primary} />
        </Col>
        <Col xs={12} sm={8} md={4}>
          <MetricCard label="当前仓位" value={posLabel} color={posColor} size="large" />
        </Col>
        <Col xs={12} sm={8} md={4}>
          <MetricCard label="浮动盈亏" value={`${pnlSign}${pnlHkd.toFixed(2)}`} suffix="HKD" color={pnlColor} />
        </Col>
        <Col xs={12} sm={8} md={4}>
          <MetricCard label="累计盈亏" value={`${totalSign}${totalPnl.toFixed(2)}`} suffix="HKD" color={totalColor} note={totalPnlNote} />
        </Col>
        <Col xs={12} sm={8} md={4}>
          <MetricCard
            label="涨跌家数比"
            value={breadthRatio === null ? '—' : breadthRatio.toFixed(2)}
            suffix={breadthRatio === null ? '' : 'R/F'}
            color={breadthColor}
            note={breadthNote}
          />
        </Col>
        <Col xs={12} sm={8} md={4}>
          <MetricCard
            label="胜率"
            value={`${state?.win_count ?? 0}W ${state?.loss_count ?? 0}L`}
            suffix={winRate !== '—' ? `(${winRate}%)` : ''}
          />
        </Col>
      </Row>
    </div>
  );
}
