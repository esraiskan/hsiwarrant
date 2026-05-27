/**
 * Call_Level 列表浮层 — PriceChart 标题栏右上角显示，鼠标悬浮看每根
 * 水平线背后的牛熊证发行人 / 街货 / 距离。
 *
 * Recharts 的 ``ReferenceLine`` 不支持原生 hover 事件,所以这里把同一份
 * ``MagnetOverlayCallLevel`` 列表用 antd ``Popover`` + 表格的形式独立展示。
 */
import { Popover, Tag } from 'antd';
import { colors } from '../theme';
import type { MagnetOverlayCallLevel } from '../types';

interface Props {
  callLevels: ReadonlyArray<MagnetOverlayCallLevel>;
}

function formatShares(v?: number): string {
  if (v == null || !Number.isFinite(v)) return '—';
  if (v >= 1e8) return `${(v / 1e8).toFixed(2)}亿`;
  if (v >= 1e4) return `${(v / 1e4).toFixed(1)}万`;
  return v.toFixed(0);
}

export default function CallLevelLegend({ callLevels }: Props) {
  if (callLevels.length === 0) {
    return null;
  }

  // 按 |distance| 升序排，距离越近越在前。
  const sorted = [...callLevels].sort((a, b) => {
    const da = a.distance_pts ?? Math.abs(a.call_level);
    const db = b.distance_pts ?? Math.abs(b.call_level);
    return da - db;
  });

  const content = (
    <div style={{ minWidth: 380, maxWidth: 520, maxHeight: 480, overflow: 'auto' }}>
      <table style={{ width: '100%', fontSize: 11, borderCollapse: 'collapse' }}>
        <thead>
          <tr style={{ color: colors.textSecondary, textAlign: 'left' }}>
            <th style={{ padding: '4px 8px' }}>方向</th>
            <th style={{ padding: '4px 8px' }}>发行商</th>
            <th style={{ padding: '4px 8px' }}>代码</th>
            <th style={{ padding: '4px 8px', textAlign: 'right' }}>收回价</th>
            <th style={{ padding: '4px 8px', textAlign: 'right' }}>距离</th>
            <th style={{ padding: '4px 8px', textAlign: 'right' }}>街货</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((cl) => {
            const accent = cl.direction === 'bull' ? colors.bull : colors.bear;
            return (
              <tr
                key={cl.code}
                style={{
                  borderTop: `1px solid ${colors.border}`,
                  fontFamily: "'SF Mono', 'Cascadia Code', 'Consolas', monospace",
                }}
              >
                <td style={{ padding: '4px 8px' }}>
                  <Tag color={cl.direction === 'bull' ? 'green' : 'red'} style={{ margin: 0 }}>
                    {cl.direction === 'bull' ? '🐂 牛' : '🐻 熊'}
                  </Tag>
                </td>
                <td style={{ padding: '4px 8px', color: colors.text }}>{cl.issuer ?? '—'}</td>
                <td style={{ padding: '4px 8px', color: colors.textSecondary }}>{cl.code}</td>
                <td style={{ padding: '4px 8px', textAlign: 'right', color: accent, fontWeight: 600 }}>
                  {cl.call_level.toFixed(0)}
                </td>
                <td style={{ padding: '4px 8px', textAlign: 'right', color: colors.text }}>
                  {cl.distance_pts != null ? `${cl.distance_pts.toFixed(1)}pt` : '—'}
                </td>
                <td style={{ padding: '4px 8px', textAlign: 'right', color: colors.textSecondary }}>
                  {formatShares(cl.outstanding_shares)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );

  const bullCount = callLevels.filter((c) => c.direction === 'bull').length;
  const bearCount = callLevels.length - bullCount;

  return (
    <Popover
      content={content}
      title={
        <span style={{ fontSize: 12 }}>
          可见牛熊证 ({callLevels.length}) — 悬停查看详情
        </span>
      }
      placement="bottomRight"
      mouseEnterDelay={0.1}
    >
      <span style={{
        cursor: 'help',
        fontSize: 11,
        color: colors.textSecondary,
        display: 'inline-flex',
        gap: 6,
        alignItems: 'center',
      }}>
        <Tag color="green" style={{ margin: 0, fontSize: 10 }}>牛 {bullCount}</Tag>
        <Tag color="red" style={{ margin: 0, fontSize: 10 }}>熊 {bearCount}</Tag>
      </span>
    </Popover>
  );
}
