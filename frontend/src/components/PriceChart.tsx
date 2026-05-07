import {
  ComposedChart,
  Line,
  Bar,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
  Cell,
} from 'recharts';
import { colors } from '../theme';
import type { KlineData, TradeRecord } from '../types';

interface Props {
  klines: KlineData[];
  trades: TradeRecord[];
  entryPrice: number;
  rsiLength: number;
}

const chartMargin = { top: 8, right: 16, bottom: 4, left: 8 };

function ChartCard({ title, children, height }: { title: string; children: React.ReactNode; height: number }) {
  return (
    <div style={{
      background: colors.bgCard,
      border: `1px solid ${colors.border}`,
      borderRadius: 10,
      padding: '12px 8px 4px',
      marginBottom: 12,
    }}>
      <div style={{
        fontSize: 12, color: colors.textSecondary, letterSpacing: 0.5,
        paddingLeft: 12, marginBottom: 8,
      }}>
        {title}
      </div>
      <ResponsiveContainer width="100%" height={height}>
        {children as React.ReactElement}
      </ResponsiveContainer>
    </div>
  );
}

const darkTooltipStyle = {
  contentStyle: {
    background: '#1a1f2e',
    border: `1px solid ${colors.border}`,
    borderRadius: 8,
    fontSize: 12,
    color: colors.text,
    boxShadow: '0 12px 28px rgba(0,0,0,0.32)',
  },
  labelStyle: {
    color: colors.textSecondary,
    marginBottom: 4,
  },
  itemStyle: {
    paddingTop: 3,
    paddingBottom: 3,
  },
  wrapperStyle: {
    outline: 'none',
  },
  cursor: {
    fill: 'rgba(230,237,243,0.06)',
    stroke: colors.borderLight,
    strokeWidth: 1,
  },
};

export default function PriceChart({ klines, trades, entryPrice, rsiLength }: Props) {
  const chartData = klines.slice(-80).map((k) => ({
    time: k.time.length > 10 ? k.time.slice(11, 19) : k.time,
    price: k.close,
    vwap: k.vwap,
    rsi: k.rsi,
    volume: k.volume,
    vol_ma: k.vol_ma,
    high: k.high,
    low: k.low,
    volumeColor: k.close > k.open ? colors.down : k.close < k.open ? colors.up : colors.textMuted,
  }));

  return (
    <div>
      {/* 价格 + VWAP */}
      <ChartCard title="PRICE · VWAP" height={280}>
        <ComposedChart data={chartData} margin={chartMargin}>
          <defs>
            <linearGradient id="priceGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={colors.primary} stopOpacity={0.15} />
              <stop offset="100%" stopColor={colors.primary} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke={colors.border} />
          <XAxis dataKey="time" tick={{ fontSize: 10, fill: colors.textMuted }} axisLine={{ stroke: colors.border }} tickLine={false} interval="preserveStartEnd" />
          <YAxis domain={['auto', 'auto']} tick={{ fontSize: 10, fill: colors.textMuted }} axisLine={false} tickLine={false} width={60} />
          <Tooltip {...darkTooltipStyle} />
          <Area type="monotone" dataKey="price" stroke="transparent" fill="url(#priceGrad)" />
          <Line type="monotone" dataKey="price" stroke={colors.primary} strokeWidth={2} dot={false} name="价格" />
          <Line type="monotone" dataKey="vwap" stroke={colors.warning} strokeWidth={1.5} strokeDasharray="4 4" dot={false} name="VWAP" />
          {entryPrice > 0 && (
            <ReferenceLine y={entryPrice} stroke={colors.down} strokeDasharray="3 3"
              label={{ value: `入场 ${entryPrice.toFixed(0)}`, fill: colors.down, fontSize: 10 }} />
          )}
        </ComposedChart>
      </ChartCard>

      {/* RSI */}
      <ChartCard title={`RSI (${rsiLength})`} height={140}>
        <ComposedChart data={chartData} margin={chartMargin}>
          <defs>
            <linearGradient id="rsiGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={colors.info} stopOpacity={0.12} />
              <stop offset="100%" stopColor={colors.info} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke={colors.border} />
          <XAxis dataKey="time" tick={{ fontSize: 10, fill: colors.textMuted }} axisLine={{ stroke: colors.border }} tickLine={false} interval="preserveStartEnd" />
          <YAxis domain={[0, 100]} tick={{ fontSize: 10, fill: colors.textMuted }} axisLine={false} tickLine={false} width={30} ticks={[0, 18, 50, 82, 100]} />
          <Tooltip {...darkTooltipStyle} />
          <ReferenceLine y={82} stroke={colors.down} strokeDasharray="3 3" strokeOpacity={0.6} />
          <ReferenceLine y={18} stroke={colors.up} strokeDasharray="3 3" strokeOpacity={0.6} />
          {/* 超买超卖区域背景 */}
          <Area type="monotone" dataKey="rsi" stroke="transparent" fill="url(#rsiGrad)" />
          <Line type="monotone" dataKey="rsi" stroke={colors.info} strokeWidth={2} dot={false} name="RSI" />
        </ComposedChart>
      </ChartCard>

      {/* 成交额 */}
      <ChartCard title="TURNOVER" height={120}>
        <ComposedChart data={chartData} margin={chartMargin}>
          <CartesianGrid strokeDasharray="3 3" stroke={colors.border} />
          <XAxis dataKey="time" tick={{ fontSize: 10, fill: colors.textMuted }} axisLine={{ stroke: colors.border }} tickLine={false} interval="preserveStartEnd" />
          <YAxis tick={{ fontSize: 10, fill: colors.textMuted }} axisLine={false} tickLine={false} width={60}
            tickFormatter={(v: number) => v >= 1e9 ? `${(v / 1e9).toFixed(1)}B` : v >= 1e6 ? `${(v / 1e6).toFixed(0)}M` : `${v}`} />
          <Tooltip {...darkTooltipStyle} />
          <Bar dataKey="volume" fill={colors.up} fillOpacity={0.45} radius={[2, 2, 0, 0]} name="成交额">
            {chartData.map((item) => (
              <Cell key={`turnover-${item.time}`} fill={item.volumeColor} />
            ))}
          </Bar>
          <Line type="monotone" dataKey="vol_ma" stroke={colors.warning} strokeWidth={1.5} dot={false} name="MA(20)" />
        </ComposedChart>
      </ChartCard>
    </div>
  );
}
