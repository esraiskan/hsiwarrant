/**
 * CBBC 街货密集区卡 (read-only,纯展示)
 * ======================================
 * 把 ``GET /api/cbbc/zones`` (或 WS ``cbbc_zones`` 推送) 的数据渲染成
 * "今日目标位 / 支撑位"两列。设计意图是给操作员一眼看到:
 *
 * - 当前 HSI 上方第一档 / 第二档熊证密集带 (做多目标位)
 * - 当前 HSI 下方第一档 / 第二档牛证密集带 (做空目标 / 止损支撑)
 * - 每档拉力 (notional 亿 HKD)、合约数、距离
 *
 * 完全独立组件,不接入任何交易决策路径,可随时下线或切换。
 */
import { Tag, Tooltip } from 'antd';
import { colors } from '../theme';
import type { CbbcTradeSetup, CbbcZoneCluster, CbbcZonesPayload } from '../types';

interface Props {
  zones: CbbcZonesPayload | null;
}

function formatNotional(hkd: number): string {
  if (!Number.isFinite(hkd) || hkd <= 0) return '—';
  if (hkd >= 1e12) return `${(hkd / 1e12).toFixed(2)}万亿`;
  if (hkd >= 1e8) return `${(hkd / 1e8).toFixed(0)}亿`;
  if (hkd >= 1e4) return `${(hkd / 1e4).toFixed(0)}万`;
  return `${hkd.toFixed(0)}`;
}

function formatShares(v: number): string {
  if (!Number.isFinite(v) || v <= 0) return '—';
  if (v >= 1e8) return `${(v / 1e8).toFixed(2)}亿`;
  if (v >= 1e4) return `${(v / 1e4).toFixed(1)}万`;
  return v.toFixed(0);
}

export default function CbbcZonesCard({ zones }: Props) {
  if (!zones || zones.spot <= 0) {
    return (
      <div style={cardStyle}>
        <div style={titleRowStyle}>
          <span>📍 街货密集区</span>
          <Tag color="default" style={{ margin: 0, fontSize: 10 }}>等待数据</Tag>
        </div>
      </div>
    );
  }

  const noTargets = zones.targets_above.length === 0;
  const noSupports = zones.supports_below.length === 0;

  return (
    <div style={cardStyle}>
      <div style={titleRowStyle}>
        <span>📍 街货密集区 (read-only)</span>
        <span style={{ fontSize: 11, color: colors.textMuted }}>
          spot {zones.spot.toFixed(0)} · {zones.bucket_pts}pt 桶 · 活{zones.live_record_count} 灭{zones.killed_record_count}
        </span>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 10 }}>
        {/* 上方目标 */}
        <div>
          <SectionHeader text={`🎯 上方目标 (做多止盈)`} accent={colors.bear} count={zones.targets_above.length} />
          {noTargets ? (
            <EmptyHint text="上方无显著街货群" />
          ) : (
            zones.targets_above.map((z, i) => (
              <ZoneRow key={`t-${z.bucket_low}`} z={z} rank={i + 1} accent={colors.bear} />
            ))
          )}
        </div>

        {/* 下方支撑 */}
        <div>
          <SectionHeader text={`🛡 下方支撑 (做空目标 / 止损线)`} accent={colors.bull} count={zones.supports_below.length} />
          {noSupports ? (
            <EmptyHint text="下方无显著街货群" />
          ) : (
            zones.supports_below.map((z, i) => (
              <ZoneRow key={`s-${z.bucket_low}`} z={z} rank={i + 1} accent={colors.bull} />
            ))
          )}
        </div>
      </div>

      {(zones.today_low != null || zones.today_high != null) && (
        <div style={{ marginTop: 8, fontSize: 10, color: colors.textMuted, textAlign: 'center' }}>
          今日 HSI low {zones.today_low?.toFixed(0) ?? '—'} ~ high {zones.today_high?.toFixed(0) ?? '—'}
          {' · '}
          已过滤被吃合约
        </div>
      )}

      {/* 操作建议 (read-only,不参与交易决策) */}
      {(zones.bull_setup || zones.bear_setup) && (
        <div style={{ marginTop: 12, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          {zones.bull_setup && <SetupCard setup={zones.bull_setup} />}
          {zones.bear_setup && <SetupCard setup={zones.bear_setup} />}
        </div>
      )}
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

function SectionHeader({ text, accent, count }: { text: string; accent: string; count: number }) {
  return (
    <div style={{
      display: 'flex',
      alignItems: 'baseline',
      justifyContent: 'space-between',
      borderBottom: `1px solid ${colors.border}`,
      paddingBottom: 4,
      marginBottom: 6,
    }}>
      <span style={{ color: accent, fontSize: 11, fontWeight: 600 }}>{text}</span>
      <span style={{ fontSize: 10, color: colors.textMuted }}>{count} 档</span>
    </div>
  );
}

function EmptyHint({ text }: { text: string }) {
  return (
    <div style={{ color: colors.textMuted, fontSize: 11, padding: '8px 4px', textAlign: 'center' }}>
      {text}
    </div>
  );
}

function ZoneRow({
  z, rank, accent,
}: { z: CbbcZoneCluster; rank: number; accent: string }) {
  // 真实档位:桶下沿 25375 但桶内最近活合约可能在 25368,直接显示真实价。
  const displayLevel = Number.isFinite(z.nearest_call_level) && z.nearest_call_level > 0
    ? z.nearest_call_level
    : z.bucket_low;
  const distAbs = Math.abs(z.distance_pts);
  const distSign = z.distance_pts >= 0 ? '+' : '−';
  const isTopRank = rank <= 2;
  // 安全余量小 (≤ 5pt) → 警告:这一档随时可能被吃,不应当作可靠目标
  const margin = z.safety_margin_pts;
  const isFragile = margin != null && Number.isFinite(margin) && margin <= 5;
  return (
    <Tooltip
      title={
        <div style={{ fontSize: 11 }}>
          <div>真实最近合约: {displayLevel.toFixed(0)}</div>
          <div>桶范围: {z.bucket_low.toFixed(0)} - {z.bucket_high.toFixed(0)}</div>
          <div>方向: {z.direction === 'bull' ? '🐂 牛证' : '🐻 熊证'}</div>
          <div>合约数: {z.contract_count}</div>
          <div>街货: {formatShares(z.outstanding_shares)} 股</div>
          <div>拉力: {formatNotional(z.notional_hkd)} HKD</div>
          {margin != null && Number.isFinite(margin) && (
            <div style={{ color: isFragile ? colors.warning : colors.textSecondary }}>
              距今日{z.direction === 'bull' ? '低' : '高'}: {margin.toFixed(1)}pt
              {isFragile && ' ⚠ 易被吃'}
            </div>
          )}
        </div>
      }
    >
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '4px 6px',
        borderLeft: `2px solid ${accent}`,
        marginBottom: 3,
        background: isTopRank ? colors.bgInput : 'transparent',
        borderRadius: 3,
        opacity: isFragile ? 0.6 : 1,
      }}>
        <span style={{ width: 18, color: colors.textMuted, fontSize: 10 }}>#{rank}</span>
        <span style={{
          flex: 1,
          fontFamily: "'SF Mono', 'Cascadia Code', 'Consolas', monospace",
          color: accent,
          fontWeight: isTopRank ? 700 : 500,
          fontSize: 13,
          textDecoration: isFragile ? 'line-through' : 'none',
        }}>
          {displayLevel.toFixed(0)}
        </span>
        {isFragile && <span style={{ color: colors.warning, fontSize: 10 }}>⚠</span>}
        <span style={{ color: colors.textSecondary, fontSize: 10 }}>
          {distSign}{distAbs.toFixed(0)}pt
        </span>
        <span style={{ color: colors.text, fontSize: 11, minWidth: 56, textAlign: 'right' }}>
          {formatNotional(z.notional_hkd)}
        </span>
        <span style={{ color: colors.textMuted, fontSize: 10, minWidth: 24, textAlign: 'right' }}>
          ×{z.contract_count}
        </span>
      </div>
    </Tooltip>
  );
}


function SetupCard({ setup }: { setup: CbbcTradeSetup }) {
  const isBull = setup.direction === 'bull';
  const accent = isBull ? colors.bull : colors.bear;
  const label = isBull ? '🐂 做多建议' : '🐻 做空建议';
  const rrAcceptable = setup.risk_reward >= 1.0;
  const rrColor = rrAcceptable ? colors.up : colors.warning;
  return (
    <Tooltip title={setup.rationale}>
      <div style={{
        background: colors.bgInput,
        borderLeft: `3px solid ${accent}`,
        borderRadius: 4,
        padding: '8px 10px',
        cursor: 'help',
      }}>
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
          marginBottom: 4,
        }}>
          <span style={{ color: accent, fontSize: 12, fontWeight: 700 }}>{label}</span>
          <Tag color={rrAcceptable ? 'green' : 'orange'} style={{ margin: 0, fontSize: 10 }}>
            R:R {setup.risk_reward.toFixed(2)}
          </Tag>
        </div>
        <div style={{
          fontSize: 11, color: colors.textSecondary,
          fontFamily: "'SF Mono', 'Cascadia Code', 'Consolas', monospace",
          lineHeight: 1.6,
        }}>
          <SetupRow label="入场区" value={`${setup.entry_low.toFixed(0)} - ${setup.entry_high.toFixed(0)}`} color={colors.text} />
          <SetupRow label="止盈 1" value={setup.take_profit_1.toFixed(0)} color={accent} bold />
          {setup.take_profit_2 !== setup.take_profit_1 && (
            <SetupRow label="止盈 2" value={setup.take_profit_2.toFixed(0)} color={accent} />
          )}
          <SetupRow label="止损" value={setup.stop_loss.toFixed(0)} color={colors.warning} bold />
        </div>
        {!rrAcceptable && (
          <div style={{ marginTop: 4, fontSize: 10, color: colors.warning, fontWeight: 600 }}>
            ⚠ R:R &lt; 1.0,建议跳过
          </div>
        )}
      </div>
    </Tooltip>
  );
}

function SetupRow({
  label, value, color, bold,
}: { label: string; value: string; color: string; bold?: boolean }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between' }}>
      <span style={{ color: colors.textMuted }}>{label}</span>
      <span style={{ color, fontWeight: bold ? 700 : 500 }}>{value}</span>
    </div>
  );
}
