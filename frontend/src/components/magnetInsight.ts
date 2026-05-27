/**
 * Pure helpers that turn the raw CBBC magnet state into user-facing labels.
 *
 * Used by the StatusPanel cards. Keeping the logic here (instead of inline
 * inside the component) makes the bias-bucket cutoffs auditable and easy to
 * tune without touching React markup.
 *
 * "Resistance" vs "fuel" disambiguation
 * --------------------------------------
 * ``magnet_bias`` alone cannot tell whether a heavy bear-side cluster above
 * spot is *resistance* (will pull price down) or *fuel* (HSI is breaking
 * through, triggering forced recall and accelerating the move). Both produce
 * ``bias > 0``. We resolve the ambiguity by combining three signals:
 *
 *   - ``magnet_bias`` magnitude → strength bucket
 *   - ``nearest_bull_distance_pts`` vs ``nearest_bear_distance_pts``
 *     → which side is currently closer to spot
 *   - intraday HSI direction (``current_price`` vs ``day_open``)
 *     → are we already moving into the cluster?
 *
 * Decision matrix (bias > 0, "上方街货密集"):
 *   nearest_bear < nearest_bull (上方贴身) AND HSI 横盘/下行 → 阻力,警惕做多 ✓
 *   nearest_bear < nearest_bull (上方贴身) AND HSI 强势上行  → 燃料,顺势 (不警惕)
 *   nearest_bear > nearest_bull (上方已突破)                  → 已穿越,顺势 (不警惕)
 *
 * Same logic mirrored for ``bias < 0``.
 */
import type { MagnetOverlayVeto, MarketRegime, StrategyState } from '../types';

/** Bias magnitude bucket cutoffs (industry-typical thresholds). */
export const BIAS_NEUTRAL_LIMIT = 0.15;
export const BIAS_LIGHT_LIMIT = 0.4;
export const BIAS_STRONG_LIMIT = 0.7;

/** A bias > 0 means bear-direction pull dominates → magnet pulls price DOWN. */
export type MagnetTilt = 'up' | 'down' | 'neutral';
export type MagnetStrength = 'neutral' | 'light' | 'medium' | 'strong';

/** How we interpret the magnetic mass relative to spot.
 *
 *  - ``resistance``: 街货群在前方阻挡价格 → 警惕反向入场。
 *  - ``fuel``: 价格正在向街货群突破,触发收回潮 → 顺势加速。
 *  - ``balanced``: 双侧拉力相当或离得太远,无明确判断。
 *  - ``unknown``: 缺数据。
 */
export type MagnetMode = 'resistance' | 'fuel' | 'balanced' | 'unknown';

/** 视为"上方贴身"或"下方贴身"的距离差阈值 (pt)。
 *  当 ``|nearestBear - nearestBull| < ASYMMETRY_TOL`` 时不判定为单侧贴身。 */
export const NEAREST_ASYMMETRY_TOL = 30;

/** 把 HSI 当日涨跌幅 (相对 day_open) 视为"明显上行/下行"的最小阈值 (pt)。 */
export const DAY_MOVE_DIRECTIONAL_PTS = 50;

export interface MagnetInsight {
  /** ``true`` when ``magnet_bias`` is a finite number we can render. */
  available: boolean;
  /** Raw bias in [-1, 1]; NaN when not available. */
  bias: number;
  /** Direction the magnet is *pulling* HSI toward. */
  tilt: MagnetTilt;
  /** Magnitude bucket. */
  strength: MagnetStrength;
  /** "resistance" vs "fuel" interpretation. */
  mode: MagnetMode;
  /** Short Chinese label, e.g. "强烈偏向下行". */
  label: string;
  /** Bull / bear distances (in pts) ready for the UI. ``null`` when missing. */
  nearestBullPts: number | null;
  nearestBearPts: number | null;
  /** Suggested bar fill ratio (0..1) and color hex for the strength bar. */
  fillRatio: number;
}

const NEUTRAL_LABEL = '中性 — 街货拉力均衡';

function strengthFromMagnitude(mag: number): MagnetStrength {
  if (mag < BIAS_NEUTRAL_LIMIT) return 'neutral';
  if (mag < BIAS_LIGHT_LIMIT) return 'light';
  if (mag < BIAS_STRONG_LIMIT) return 'medium';
  return 'strong';
}

function strengthCn(s: MagnetStrength): string {
  switch (s) {
    case 'neutral': return '中性';
    case 'light': return '轻微';
    case 'medium': return '明显';
    case 'strong': return '强烈';
  }
}

export function computeMagnetInsight(
  state: StrategyState | null,
  marketRegime: MarketRegime | null = null,
): MagnetInsight {
  const bias = state?.cbbc_magnet_bias;
  const nearestBull = state?.cbbc_nearest_bull_distance_pts ?? null;
  const nearestBear = state?.cbbc_nearest_bear_distance_pts ?? null;

  if (bias == null || !Number.isFinite(bias)) {
    return {
      available: false,
      bias: Number.NaN,
      tilt: 'neutral',
      strength: 'neutral',
      mode: 'unknown',
      label: '等待数据',
      nearestBullPts: nearestBull,
      nearestBearPts: nearestBear,
      fillRatio: 0,
    };
  }

  const mag = Math.abs(bias);
  const strength = strengthFromMagnitude(mag);

  if (strength === 'neutral') {
    return {
      available: true,
      bias,
      tilt: 'neutral',
      strength: 'neutral',
      mode: 'balanced',
      label: NEUTRAL_LABEL,
      nearestBullPts: nearestBull,
      nearestBearPts: nearestBear,
      fillRatio: Math.min(1, mag),
    };
  }

  // 判定 resistance vs fuel:
  //   1. nearest 距离方向比对 — 哪一侧更贴近 spot。
  //   2. 当日 HSI 涨跌方向 — 价格在向密集带方向"突破"还是"被阻"。
  const nearestSide: 'bull' | 'bear' | 'symmetric' = (() => {
    if (nearestBull == null || nearestBear == null) return 'symmetric';
    const diff = nearestBear - nearestBull;
    if (Math.abs(diff) < NEAREST_ASYMMETRY_TOL) return 'symmetric';
    return diff > 0 ? 'bull' : 'bear';  // bull side closer / bear side closer
  })();

  const dayMove: 'up' | 'down' | 'flat' = (() => {
    const open = marketRegime?.day_open;
    const cur = marketRegime?.current_price;
    if (open == null || cur == null) return 'flat';
    const move = cur - open;
    if (Math.abs(move) < DAY_MOVE_DIRECTIONAL_PTS) return 'flat';
    return move > 0 ? 'up' : 'down';
  })();

  // bias > 0 = bear pull dominates; bias < 0 = bull pull dominates.
  const dominantSide: 'bull' | 'bear' = bias > 0 ? 'bear' : 'bull';

  // Decision matrix:
  //   - dominant side is "bear" (bias > 0):
  //       上方街货群在贴身阻挡 (nearest_bear < nearest_bull) AND HSI 在 flat/down
  //         → 真阻力,警惕做多
  //       上方贴身 AND HSI 强势上行
  //         → 价格正在屠杀熊证,燃料场景,顺势
  //       下方反而更近 (nearest_bull < nearest_bear)
  //         → 价格已穿越熊证密集带,反向阻挡变成支撑或后视镜,顺势
  //   - dominant side is "bull" (bias < 0): 镜像。
  let mode: MagnetMode;
  let tilt: MagnetTilt;
  if (dominantSide === 'bear') {
    if (nearestSide === 'bear' && dayMove !== 'up') {
      mode = 'resistance';
      tilt = 'down';  // 警惕做多
    } else if (nearestSide === 'bear' && dayMove === 'up') {
      mode = 'fuel';
      tilt = 'up';    // 顺势加速
    } else {
      // nearestSide === 'bull' or 'symmetric' → 价格已穿越密集带或两侧均衡
      mode = 'fuel';
      tilt = 'up';
    }
  } else {
    // dominantSide === 'bull'
    if (nearestSide === 'bull' && dayMove !== 'down') {
      mode = 'resistance';
      tilt = 'up';    // 警惕做空
    } else if (nearestSide === 'bull' && dayMove === 'down') {
      mode = 'fuel';
      tilt = 'down';  // 顺势加速
    } else {
      mode = 'fuel';
      tilt = 'down';
    }
  }

  // 文案:resistance 用"警惕反向"措辞,fuel 用"顺势"措辞。
  const sCn = strengthCn(strength);
  let label: string;
  if (mode === 'resistance') {
    if (tilt === 'down') {
      label = `${sCn}阻力在上 — 警惕做多`;
    } else {
      label = `${sCn}支撑在下 — 警惕做空`;
    }
  } else {
    // fuel
    if (tilt === 'up') {
      label = `${sCn}街货燃料在上 — 顺势做多`;
    } else {
      label = `${sCn}街货燃料在下 — 顺势做空`;
    }
  }

  return {
    available: true,
    bias,
    tilt,
    strength,
    mode,
    label,
    nearestBullPts: nearestBull,
    nearestBearPts: nearestBear,
    fillRatio: Math.min(1, mag),
  };
}


// --------------------------------------------------------------------------- //
// Veto aggregation
// --------------------------------------------------------------------------- //

export interface VetoSummary {
  totalToday: number;
  bullVetoes: number;
  bearVetoes: number;
  /** Count of vetoes whose kline_time falls within ``recentWindowSeconds`` of
   *  ``nowMs``. ``null`` when the rolling buffer has none we can place. */
  recentCount: number;
  /** Most recent veto reason_code, for the "刚刚被否" hint. */
  latestReason: string | null;
  /** Whether the most recent veto is within the recent window. */
  isRecentActive: boolean;
}

/** Parse a backend ``ts_hk`` / ``kline_time`` string to a JS millis timestamp.
 *  The backend emits either ``"YYYY-MM-DD HH:MM:SS"`` or an ISO string; both
 *  parse natively when we replace the space with a ``T``. */
export function parseTsHkToMs(ts: string): number | null {
  if (!ts) return null;
  const s = ts.includes('T') ? ts : ts.replace(' ', 'T');
  const t = Date.parse(s);
  return Number.isFinite(t) ? t : null;
}

export function summarizeVetoes(
  vetoes: ReadonlyArray<MagnetOverlayVeto>,
  options?: { recentWindowSeconds?: number; nowMs?: number },
): VetoSummary {
  const recentWindowSeconds = options?.recentWindowSeconds ?? 300;
  const nowMs = options?.nowMs ?? Date.now();
  const total = vetoes.length;
  let bull = 0;
  let bear = 0;
  let latest: MagnetOverlayVeto | null = null;
  let latestMs = -Infinity;
  let recentCount = 0;
  const cutoffMs = nowMs - recentWindowSeconds * 1000;

  for (const v of vetoes) {
    if (v.direction === 'BULL') bull += 1;
    else if (v.direction === 'BEAR') bear += 1;
    const t = parseTsHkToMs(v.kline_time);
    if (t == null) continue;
    if (t > latestMs) {
      latestMs = t;
      latest = v;
    }
    if (t >= cutoffMs) recentCount += 1;
  }

  return {
    totalToday: total,
    bullVetoes: bull,
    bearVetoes: bear,
    recentCount,
    latestReason: latest?.reason_code ?? null,
    isRecentActive: latestMs > -Infinity && latestMs >= cutoffMs,
  };
}
