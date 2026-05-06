import { Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { colors } from '../theme';
import type { TradeRecord, TradeSignal } from '../types';

interface Props {
  trades: TradeRecord[];
}

const signalConfig: Record<TradeSignal, { color: string; bg: string; label: string }> = {
  buy_bull: { color: colors.bull, bg: 'rgba(0,212,170,0.12)', label: '买入牛证 🚀' },
  buy_bear: { color: colors.bear, bg: 'rgba(255,77,106,0.12)', label: '买入熊证 🔻' },
  take_profit: { color: '#f59e0b', bg: 'rgba(245,158,11,0.12)', label: '止盈平仓 💰' },
  stop_loss: { color: '#ef4444', bg: 'rgba(239,68,68,0.12)', label: '止损平仓 🛡️' },
  hold: { color: colors.textMuted, bg: 'transparent', label: '持仓' },
};

const columns: ColumnsType<TradeRecord & { key: number }> = [
  {
    title: '时间',
    dataIndex: 'time',
    width: 170,
    render: (t: string) => (
      <span style={{ fontFamily: "'SF Mono', monospace", fontSize: 12, color: colors.textSecondary }}>{t}</span>
    ),
  },
  {
    title: '信号',
    dataIndex: 'signal',
    width: 140,
    render: (s: TradeSignal) => {
      const cfg = signalConfig[s];
      return (
        <span style={{
          color: cfg.color, background: cfg.bg,
          padding: '3px 10px', borderRadius: 6, fontSize: 12, fontWeight: 600,
        }}>
          {cfg.label}
        </span>
      );
    },
  },
  {
    title: '价格',
    dataIndex: 'price',
    width: 100,
    render: (p: number) => (
      <span style={{ fontFamily: "'SF Mono', monospace", fontWeight: 600 }}>{p.toFixed(2)}</span>
    ),
  },
  {
    title: 'RSI',
    dataIndex: 'rsi',
    width: 70,
    render: (r: number) => {
      const c = r < 18 ? colors.up : r > 82 ? colors.down : colors.textSecondary;
      return <span style={{ color: c, fontWeight: 700, fontFamily: "'SF Mono', monospace" }}>{r.toFixed(1)}</span>;
    },
  },
  {
    title: '盈亏 (点)',
    dataIndex: 'pnl',
    width: 100,
    render: (v: number | null) => {
      if (v == null) return <span style={{ color: colors.textMuted }}>—</span>;
      const c = v >= 0 ? colors.up : colors.down;
      return <span style={{ color: c, fontWeight: 700, fontFamily: "'SF Mono', monospace" }}>{v >= 0 ? '+' : ''}{v.toFixed(2)}</span>;
    },
  },
  {
    title: '盈亏 (HKD)',
    dataIndex: 'pnl_hkd',
    width: 120,
    render: (v: number | null) => {
      if (v == null) return <span style={{ color: colors.textMuted }}>—</span>;
      const c = v >= 0 ? colors.up : colors.down;
      return <span style={{ color: c, fontWeight: 700, fontFamily: "'SF Mono', monospace" }}>{v >= 0 ? '+' : ''}{v.toFixed(2)}</span>;
    },
  },
  {
    title: '说明',
    dataIndex: 'message',
    ellipsis: true,
    render: (m: string) => <span style={{ color: colors.textSecondary, fontSize: 12 }}>{m}</span>,
  },
];

export default function TradeLog({ trades }: Props) {
  const data = [...trades].reverse().map((t, i) => ({ ...t, key: i }));

  return (
    <div style={{
      background: colors.bgCard,
      border: `1px solid ${colors.border}`,
      borderRadius: 10,
      padding: 16,
      marginTop: 16,
    }}>
      <div style={{ fontSize: 13, color: colors.textSecondary, marginBottom: 12, letterSpacing: 0.5 }}>
        📋 TRADE LOG
      </div>
      <Table
        columns={columns}
        dataSource={data}
        size="small"
        pagination={{ pageSize: 8, showSizeChanger: false }}
        scroll={{ x: 800 }}
        locale={{ emptyText: '暂无交易记录' }}
      />
    </div>
  );
}
