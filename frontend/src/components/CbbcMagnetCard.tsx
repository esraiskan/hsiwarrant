/**
 * CBBC 磁吸面板（综合卡）— StatusPanel 上的"今天倾向哪边"卡。
 *
 * Inputs:
 *   - state: StrategyState (从 /api/state + WS 推送)
 *   - magnetOverlay: WS magnet_overlay 推送（提供 recent_vetoes、health flags）
 *
 * Renders:
 *   1. 磁吸方向 + 强度条
 *   2. 反向最近距离（关键否决信号）
 *   3. 数据层健康灯
 *   4. 今日否决次数 + "刚刚被否"提示
 *   5. 一句话操作建议
 */
import { Tag, Tooltip } from 'antd';
import { colors } from '../theme';
import type { MagnetOverlayPayload, MarketRegime, StrategyState } from '../types';
import { computeMagnetInsight, summarizeVetoes } from './magnetInsight';

interface Props {
  state: StrategyState | null;
  magnetOverlay: MagnetOverlayPayload | null;
  marketRegime: MarketRegime | null;
}

export default function CbbcMagnetCard({ state, magnetOverlay, marketRegime }: Props) {
  const insight = computeMagnetInsight(state, marketRegime);
  const vetoes = summarizeVetoes(magnetOverlay?.recent_vetoes ?? []);

  const layerEnabled = state?.cbbc_magnet_layer_enabled === true;
  const degraded = state?.cbbc_magnet_degraded === true || magnetOverlay?.cbbc_magnet_degraded === true;
  const hsiStale = magnetOverlay?.hsi_spot_stale === true;

  const tiltColor =
    insight.tilt === 'up' ? colors.bull
    : insight.tilt === 'down' ? colors.bear
    : colors.textMuted;

  const fillPct = Math.round(insight.fillRatio * 100);

  return (
    <div style={{
      background: colors.bgCard,
      border: `1px solid ${colors.border}`,
      borderRadius: 10,
      padding: '14px 16px',
      height: '100%',
      display: 'flex',
      flexDirection: 'column',
      gap: 10,
    }}>
      {/* 标题 + 健康灯 */}
      <CardHeader
        layerEnabled={layerEnabled}
        degraded={degraded}
        hsiStale={hsiStale}
      />

      {/* 磁吸方向 + 强度条 */}
      <BiasRow insight={insight} tiltColor={tiltColor} fillPct={fillPct} />

      {/* 反向最近距离 */}
      <DistanceRow insight={insight} />

      {/* 否决统计 */}
      <VetoRow vetoes={vetoes} />

      {/* 操作建议 */}
      <AdviceLine insight={insight} vetoes={vetoes} layerEnabled={layerEnabled} />
    </div>
  );
}


// --------------------------------------------------------------------------- //
// Sub-components
// --------------------------------------------------------------------------- //

function CardHeader({
  layerEnabled, degraded, hsiStale,
}: { layerEnabled: boolean; degraded: boolean; hsiStale: boolean }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
      <div style={{
        color: colors.textSecondary, fontSize: 11, letterSpacing: 0.5,
        textTransform: 'uppercase', fontWeight: 600,
      }}>
        CBBC 磁吸面板
      </div>
      <div style={{ display: 'flex', gap: 6 }}>
        {!layerEnabled && (
          <Tooltip title="磁吸层未启用 (cbbc_magnet_layer_enabled=false)">
            <Tag color="default" style={{ margin: 0, fontSize: 10 }}>OFF</Tag>
          </Tooltip>
        )}
        {layerEnabled && !degraded && !hsiStale && (
          <Tooltip title="磁吸层已启用且数据健康">
            <Tag color="green" style={{ margin: 0, fontSize: 10 }}>ON</Tag>
          </Tooltip>
        )}
        {degraded && (
          <Tooltip title="磁吸数据降级 (>36h 未刷新或外部异常)">
            <Tag color="red" style={{ margin: 0, fontSize: 10 }}>降级</Tag>
          </Tooltip>
        )}
        {hsiStale && (
          <Tooltip title="HSI 现价超 5 秒未刷新">
            <Tag color="orange" style={{ margin: 0, fontSize: 10 }}>HSI Stale</Tag>
          </Tooltip>
        )}
      </div>
    </div>
  );
}

function BiasRow({
  insight, tiltColor, fillPct,
}: { insight: ReturnType<typeof computeMagnetInsight>; tiltColor: string; fillPct: number }) {
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
        <span style={{
          fontSize: 22, fontWeight: 700, color: tiltColor,
          fontFamily: "'SF Mono', 'Cascadia Code', 'Consolas', monospace",
        }}>
          {insight.available ? insight.bias.toFixed(2) : '—'}
        </span>
        <span style={{ color: colors.textSecondary, fontSize: 11 }}>
          Magnet Bias (-1..+1)
        </span>
      </div>
      {/* 强度条：中点 50%，左右 ± 50% 表达 bias 方向 */}
      <div style={{
        marginTop: 6, height: 6, borderRadius: 3,
        background: colors.bgInput,
        position: 'relative', overflow: 'hidden',
      }}>
        {/* 中线 */}
        <div style={{
          position: 'absolute', left: '50%', top: 0, bottom: 0, width: 1,
          background: colors.borderLight,
        }} />
        {insight.available && (
          <div style={{
            position: 'absolute',
            // bias > 0 (向下吸) 填右半段；bias < 0 填左半段
            left: insight.bias >= 0 ? '50%' : `${50 - fillPct / 2}%`,
            width: `${fillPct / 2}%`,
            top: 0, bottom: 0,
            background: tiltColor,
            opacity: 0.85,
          }} />
        )}
      </div>
      <div style={{ marginTop: 6, color: tiltColor, fontSize: 12, fontWeight: 600 }}>
        {insight.label}
      </div>
    </div>
  );
}


function DistanceRow({
  insight,
}: { insight: ReturnType<typeof computeMagnetInsight> }) {
  const fmt = (v: number | null) => (v == null ? '—' : `${v.toFixed(0)}pt`);
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
      <DistanceCell
        label="最近 牛证距离"
        value={fmt(insight.nearestBullPts)}
        accent={colors.bull}
        tip="HSI 距最近一档 bull-direction Call_Level 的距离 (pt)"
      />
      <DistanceCell
        label="最近 熊证距离"
        value={fmt(insight.nearestBearPts)}
        accent={colors.bear}
        tip="HSI 距最近一档 bear-direction Call_Level 的距离 (pt)"
      />
    </div>
  );
}

function DistanceCell({
  label, value, accent, tip,
}: { label: string; value: string; accent: string; tip: string }) {
  return (
    <Tooltip title={tip}>
      <div style={{
        flex: 1,
        background: colors.bgInput,
        borderLeft: `2px solid ${accent}`,
        borderRadius: 4,
        padding: '6px 10px',
      }}>
        <div style={{ color: colors.textMuted, fontSize: 10 }}>{label}</div>
        <div style={{
          fontSize: 14, fontWeight: 600, color: colors.text,
          fontFamily: "'SF Mono', 'Cascadia Code', 'Consolas', monospace",
        }}>{value}</div>
      </div>
    </Tooltip>
  );
}

function VetoRow({ vetoes }: { vetoes: ReturnType<typeof summarizeVetoes> }) {
  if (vetoes.totalToday === 0) {
    return (
      <div style={{ color: colors.textMuted, fontSize: 11 }}>
        今日否决 <span style={{ color: colors.textSecondary }}>0</span> 次
      </div>
    );
  }
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 11 }}>
      <span style={{ color: colors.textSecondary }}>今日否决</span>
      <Tag color="cyan" style={{ margin: 0 }}>
        BULL · {vetoes.bullVetoes}
      </Tag>
      <Tag color="magenta" style={{ margin: 0 }}>
        BEAR · {vetoes.bearVetoes}
      </Tag>
      {vetoes.isRecentActive && (
        <Tooltip title={`最近 5 分钟有 ${vetoes.recentCount} 次否决，原因：${vetoes.latestReason ?? '—'}`}>
          <Tag color="orange" style={{ margin: 0 }}>⚠ 刚刚被否</Tag>
        </Tooltip>
      )}
    </div>
  );
}


function AdviceLine({
  insight, vetoes, layerEnabled,
}: {
  insight: ReturnType<typeof computeMagnetInsight>;
  vetoes: ReturnType<typeof summarizeVetoes>;
  layerEnabled: boolean;
}) {
  const text = buildAdviceText(insight, vetoes, layerEnabled);
  return (
    <div style={{
      marginTop: 'auto',
      padding: '8px 10px',
      borderRadius: 6,
      background: colors.bgInput,
      borderLeft: `3px solid ${colors.info}`,
      color: colors.textSecondary,
      fontSize: 11,
      lineHeight: 1.45,
    }}>
      {text}
    </div>
  );
}

export function buildAdviceText(
  insight: ReturnType<typeof computeMagnetInsight>,
  vetoes: ReturnType<typeof summarizeVetoes>,
  layerEnabled: boolean,
): string {
  if (!layerEnabled) {
    return '磁吸层未启用：本面板仅展示街货状态，不参与入场否决。';
  }
  if (!insight.available) {
    return '等待磁吸数据加载…';
  }
  if (vetoes.isRecentActive) {
    return `5 分钟内刚发生否决（${vetoes.latestReason ?? ''}），CBBC 街货密集带正在阻挡反转入场，建议观望。`;
  }
  if (insight.strength === 'neutral' || insight.mode === 'balanced') {
    return '街货拉力均衡，磁吸层不会主动否决；按主策略 RSI / VWAP 信号操作即可。';
  }
  const sCn = insight.strength === 'strong' ? '强烈' : insight.strength === 'medium' ? '明显' : '轻微';
  if (insight.mode === 'resistance') {
    if (insight.tilt === 'down') {
      return `上方街货${sCn}阻挡 — 反弹做多胜率受密集带压制，倾向等待回吐做空。`;
    }
    return `下方街货${sCn}支撑 — 回踩做空胜率受密集带支撑，倾向等待反弹做多。`;
  }
  // fuel
  if (insight.tilt === 'up') {
    return `价格正在向上突破上方${sCn}街货群 — 触发收回潮即顺势加速,倾向跟随做多。`;
  }
  return `价格正在向下突破下方${sCn}街货群 — 触发收回潮即顺势加速,倾向跟随做空。`;
}
