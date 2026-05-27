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
  ReferenceArea,
  ReferenceDot,
  Cell,
} from 'recharts';
import { colors } from '../theme';
import {
  computeOverlayState,
  isPriceInDenseBucket,
} from './magnetOverlayState';
import CallLevelLegend from './CallLevelLegend';
import type { KlineData, MagnetOverlayPayload, TradeRecord } from '../types';

interface Props {
  klines: KlineData[];
  trades: TradeRecord[];
  entryPrice: number;
  rsiLength: number;
  magnetOverlay?: MagnetOverlayPayload | null;
}

const chartMargin = { top: 8, right: 16, bottom: 4, left: 8 };

function ChartCard({ title, children, height, badge }: { title: string; children: React.ReactNode; height: number; badge?: React.ReactNode }) {
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
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
      }}>
        <span>{title}</span>
        {badge && <span style={{ paddingRight: 12 }}>{badge}</span>}
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

export default function PriceChart({ klines, trades, entryPrice, rsiLength, magnetOverlay }: Props) {
  const chartData = klines.slice(-80).map((k) => ({
    time: k.time.length > 10 ? k.time.slice(11, 19) : k.time,
    fullTime: k.time,
    price: k.close,
    vwap: k.vwap,
    rsi: k.rsi,
    volume: k.volume,
    vol_ma: k.vol_ma,
    high: k.high,
    low: k.low,
    volumeColor: k.close > k.open ? colors.down : k.close < k.open ? colors.up : colors.textMuted,
  }));

  // ---- CBBC magnet overlay (cbbc-magnet-signal task 12.2 / 12.3) -----------
  // Pure helpers compute the "should we render?" + dense-band + visible
  // veto state. The component then iterates over the result to draw the
  // ReferenceLine / ReferenceArea / ReferenceDot elements.
  const overlayState = computeOverlayState(
    magnetOverlay,
    chartData.map((d) => d.time),
  );
  const overlayActive = overlayState.active;

  // ``decayPoints`` / ``dense_band_pull_share`` are payload-level metadata.
  // They are intentionally not used here because the backend already
  // filtered the visible call_levels list and the histogram. Keeping the
  // payload shape stable on the wire avoids breaking other consumers.

  const denseBuckets = overlayState.denseBuckets;
  // 同 5pt 桶内,只保留街货量最大的一条 Call_Level 渲染,避免标签互相覆盖。
  // 桶下沿 = floor(call_level / 5) * 5,与后端 histogram 桶宽对齐。
  const visibleCallLevels = (() => {
    const bucketed = new Map<number, typeof overlayState.callLevels[number]>();
    for (const cl of overlayState.callLevels) {
      const bucket = Math.floor(cl.call_level / 5) * 5;
      const incumbent = bucketed.get(bucket);
      const incumbentSize = incumbent?.outstanding_shares ?? 0;
      const challengerSize = cl.outstanding_shares ?? 0;
      if (!incumbent || challengerSize > incumbentSize) {
        bucketed.set(bucket, cl);
      }
    }
    return Array.from(bucketed.values());
  })();
  const visibleVetoes = overlayState.visibleVetoes;

  // ---- 密集带边界 (R8.2) ---------------------------------------------------
  // 当 overlay active 时,把 ``hsi_spot ± dense_band_threshold_pts`` 画成两条
  // 虚线,让操作员一眼看到价格离否决边界还有多远。spot 取最近一根 K 线的收盘
  // (chartData 末尾),fallback 到 entryPrice。
  const lastClose = chartData.length > 0 ? chartData[chartData.length - 1].price : 0;
  const spotForBand = lastClose > 0 ? lastClose : entryPrice;
  const bandThreshold = magnetOverlay?.dense_band_threshold_pts;
  const showDenseBandBoundary =
    overlayActive &&
    typeof bandThreshold === 'number' &&
    Number.isFinite(bandThreshold) &&
    spotForBand > 0;
  const denseBandUpper = showDenseBandBoundary ? spotForBand + bandThreshold! : 0;
  const denseBandLower = showDenseBandBoundary ? spotForBand - bandThreshold! : 0;

  const overlayBadge = overlayState.showUnavailableBanner ? (
    <span style={{ color: colors.warning, fontSize: 11 }}>CBBC 磁吸数据不可用</span>
  ) : overlayActive && overlayState.callLevels.length > 0 ? (
    <CallLevelLegend callLevels={overlayState.callLevels} />
  ) : undefined;

  return (
    <div>
      {/* 价格 + VWAP */}
      <ChartCard title="PRICE · VWAP" height={280} badge={overlayBadge}>
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
          {/* CBBC magnet — dense band 边界虚线 (R8.2) */}
          {showDenseBandBoundary && (
            <>
              <ReferenceLine
                y={denseBandUpper}
                stroke={colors.warning}
                strokeDasharray="2 4"
                strokeOpacity={0.5}
                ifOverflow="hidden"
                label={{
                  value: `密集带 ${denseBandUpper.toFixed(0)}`,
                  fill: colors.warning,
                  fontSize: 9,
                  position: 'insideTopRight',
                }}
              />
              <ReferenceLine
                y={denseBandLower}
                stroke={colors.warning}
                strokeDasharray="2 4"
                strokeOpacity={0.5}
                ifOverflow="hidden"
                label={{
                  value: `密集带 ${denseBandLower.toFixed(0)}`,
                  fill: colors.warning,
                  fontSize: 9,
                  position: 'insideBottomRight',
                }}
              />
            </>
          )}
          {/* CBBC magnet — dense band shading (5pt buckets with >=15% share) */}
          {denseBuckets.map((b, idx) => (
            <ReferenceArea
              key={`dense-${idx}-${b.bucket_low}`}
              y1={b.bucket_low}
              y2={b.bucket_high}
              fill={colors.warning}
              fillOpacity={0.25}
              ifOverflow="hidden"
            />
          ))}
          {/* CBBC magnet — Call_Level horizontal lines */}
          {visibleCallLevels.map((cl) => {
            const isDense = isPriceInDenseBucket(cl.call_level, denseBuckets);
            const issuerTag = cl.issuer ? `${cl.issuer} ` : '';
            const dirCn = cl.direction === 'bull' ? '牛' : '熊';
            return (
              <ReferenceLine
                key={`cl-${cl.code}`}
                y={cl.call_level}
                stroke={cl.direction === 'bull' ? colors.bull : colors.bear}
                strokeOpacity={0.85}
                strokeWidth={isDense ? 2 : 1}
                label={{
                  value: `${issuerTag}${dirCn} ${cl.call_level.toFixed(0)}`,
                  fill: cl.direction === 'bull' ? colors.bull : colors.bear,
                  fontSize: 10,
                  // 放在图区内的右侧而不是右沿,避免被裁切。
                  position: 'insideRight',
                }}
              />
            );
          })}
          <Area type="monotone" dataKey="price" stroke="transparent" fill="url(#priceGrad)" />
          <Line type="monotone" dataKey="price" stroke={colors.primary} strokeWidth={2} dot={false} activeDot={false} name="价格" />
          <Line type="monotone" dataKey="vwap" stroke={colors.warning} strokeWidth={1.5} strokeDasharray="4 4" dot={false} activeDot={false} name="VWAP" />
          {entryPrice > 0 && (
            <ReferenceLine y={entryPrice} stroke={colors.down} strokeDasharray="3 3"
              label={{ value: `入场 ${entryPrice.toFixed(0)}`, fill: colors.down, fontSize: 10 }} />
          )}
          {/* CBBC magnet — veto markers (R8.5) */}
          {overlayActive && visibleVetoes.map((v, idx) => {
            const bar = chartData.find((d) => d.time === v.time);
            if (!bar) return null;
            return (
              <ReferenceDot
                key={`veto-${idx}-${v.time}`}
                x={v.time}
                y={bar.high + 5}
                r={5}
                fill={v.direction === 'BULL' ? colors.bull : colors.bear}
                stroke="#ffffff"
                strokeWidth={1}
                ifOverflow="visible"
                label={{
                  value: '⊘',
                  fill: '#ffffff',
                  fontSize: 8,
                  position: 'center',
                }}
              />
            );
          })}
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
          <Line type="monotone" dataKey="rsi" stroke={colors.info} strokeWidth={2} dot={false} activeDot={false} name="RSI" />
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
          <Line type="monotone" dataKey="vol_ma" stroke={colors.warning} strokeWidth={1.5} dot={false} activeDot={false} name="MA(20)" />
        </ComposedChart>
      </ChartCard>
    </div>
  );
}
