/**
 * Tests for ``magnetInsight`` helpers.
 *
 * Run with (when test runner available):
 *   npx tsx --test src/components/magnetInsight.test.ts
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  BIAS_LIGHT_LIMIT,
  BIAS_NEUTRAL_LIMIT,
  BIAS_STRONG_LIMIT,
  computeMagnetInsight,
  parseTsHkToMs,
  summarizeVetoes,
} from './magnetInsight.js';
import type { MagnetOverlayVeto, StrategyState } from '../types.js';


function stateWith(overrides: Partial<StrategyState>): StrategyState {
  return {
    position: 'none',
    entry_price: 0,
    current_price: 0,
    unrealized_pnl: 0,
    unrealized_pnl_hkd: 0,
    total_pnl_hkd: 0,
    breadth_raise_count: 0,
    breadth_fall_count: 0,
    breadth_equal_count: 0,
    breadth_ratio: null,
    breadth_amplitude: 0,
    breadth_time: '',
    trade_count: 0,
    win_count: 0,
    loss_count: 0,
    is_running: false,
    ...overrides,
  };
}


describe('computeMagnetInsight', () => {
  it('returns unavailable when bias is missing', () => {
    const result = computeMagnetInsight(stateWith({}));
    assert.equal(result.available, false);
    assert.equal(result.tilt, 'neutral');
  });

  it('classifies neutral when |bias| < 0.15', () => {
    const result = computeMagnetInsight(stateWith({ cbbc_magnet_bias: 0.1 }));
    assert.equal(result.available, true);
    assert.equal(result.tilt, 'neutral');
    assert.equal(result.strength, 'neutral');
  });

  it('classifies strong DOWN tilt when bias is large positive', () => {
    const result = computeMagnetInsight(stateWith({ cbbc_magnet_bias: 0.85 }));
    assert.equal(result.tilt, 'down');
    assert.equal(result.strength, 'strong');
  });

  it('classifies medium UP tilt when bias is mid negative', () => {
    const result = computeMagnetInsight(stateWith({ cbbc_magnet_bias: -0.5 }));
    assert.equal(result.tilt, 'up');
    assert.equal(result.strength, 'medium');
  });

  it('respects the bucket cutoff constants', () => {
    // Just below the strong cutoff (default 0.7) should be 'medium'.
    const just_below = computeMagnetInsight(stateWith({
      cbbc_magnet_bias: BIAS_STRONG_LIMIT - 0.001,
    }));
    assert.equal(just_below.strength, 'medium');
    // At the strong cutoff exactly should clamp to 'strong'.
    const at_strong = computeMagnetInsight(stateWith({
      cbbc_magnet_bias: BIAS_STRONG_LIMIT,
    }));
    assert.equal(at_strong.strength, 'strong');
    // Below the neutral cutoff should be neutral.
    const at_neutral = computeMagnetInsight(stateWith({
      cbbc_magnet_bias: BIAS_NEUTRAL_LIMIT - 0.001,
    }));
    assert.equal(at_neutral.strength, 'neutral');
    // Light bucket exists between neutral and light cutoff.
    const at_light = computeMagnetInsight(stateWith({
      cbbc_magnet_bias: BIAS_LIGHT_LIMIT - 0.001,
    }));
    assert.equal(at_light.strength, 'light');
  });

  it('clamps fillRatio to [0, 1] even for out-of-range bias', () => {
    const oversized = computeMagnetInsight(stateWith({ cbbc_magnet_bias: 1.5 }));
    assert.equal(oversized.fillRatio, 1);
  });
});


describe('parseTsHkToMs', () => {
  it('parses space-separated HK timestamps', () => {
    const ms = parseTsHkToMs('2025-01-06 10:30:00');
    assert.equal(typeof ms, 'number');
    assert.ok(ms! > 0);
  });

  it('parses ISO timestamps', () => {
    const ms = parseTsHkToMs('2025-01-06T10:30:00');
    assert.ok(ms! > 0);
  });

  it('returns null for empty input', () => {
    assert.equal(parseTsHkToMs(''), null);
  });
});


describe('summarizeVetoes', () => {
  const vetoes: MagnetOverlayVeto[] = [
    { kline_time: '2025-01-06 10:00:00', direction: 'BULL', reason_code: 'cbbc_dense_band_above' },
    { kline_time: '2025-01-06 10:30:00', direction: 'BULL', reason_code: 'cbbc_dense_band_above' },
    { kline_time: '2025-01-06 14:00:00', direction: 'BEAR', reason_code: 'cbbc_dense_band_below' },
  ];

  it('counts BULL/BEAR direction buckets', () => {
    const s = summarizeVetoes(vetoes, { nowMs: Date.parse('2025-01-06T15:00:00') });
    assert.equal(s.totalToday, 3);
    assert.equal(s.bullVetoes, 2);
    assert.equal(s.bearVetoes, 1);
  });

  it('flags isRecentActive when latest veto is within window', () => {
    // Now is 14:02; latest is 14:00 → recent.
    const s = summarizeVetoes(vetoes, {
      nowMs: Date.parse('2025-01-06T14:02:00'),
      recentWindowSeconds: 300,
    });
    assert.equal(s.isRecentActive, true);
    assert.equal(s.latestReason, 'cbbc_dense_band_below');
  });

  it('clears isRecentActive when latest veto is older than window', () => {
    const s = summarizeVetoes(vetoes, {
      nowMs: Date.parse('2025-01-06T16:00:00'),
      recentWindowSeconds: 300,
    });
    assert.equal(s.isRecentActive, false);
  });

  it('handles empty list', () => {
    const s = summarizeVetoes([]);
    assert.equal(s.totalToday, 0);
    assert.equal(s.isRecentActive, false);
    assert.equal(s.latestReason, null);
  });
});
