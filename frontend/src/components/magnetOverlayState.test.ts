/**
 * Tests for ``magnetOverlayState`` (cbbc-magnet-signal task 12.4).
 *
 * Uses ``node:test`` + the TypeScript-aware loader because the project
 * doesn't currently have Vitest / Jest configured. Run with:
 *
 *   node --experimental-strip-types --test \
 *       src/components/magnetOverlayState.test.ts
 *
 * Or via npx (when the npm install is healthy):
 *
 *   npx tsx --test src/components/magnetOverlayState.test.ts
 *
 * Coverage:
 *   - degraded payload hides the overlay (R8.6)
 *   - missing decay_points hides the overlay (R8.6)
 *   - dense buckets correctly identified at the 15% threshold (R8.3)
 *   - veto markers filtered to the visible window (R8.5)
 *   - decay_points filtering happens server-side; tests just confirm the
 *     helper passes call_levels through unchanged when active.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  computeOverlayState,
  formatVetoTime,
  isPriceInDenseBucket,
} from './magnetOverlayState.js';
import type { MagnetOverlayPayload } from '../types.js';


function basePayload(
  overrides: Partial<MagnetOverlayPayload> = {},
): MagnetOverlayPayload {
  return {
    decay_points: 300,
    dense_band_pull_share: 0.4,
    cbbc_magnet_degraded: false,
    hsi_spot_stale: false,
    call_levels: [
      { code: 'HK.50001', direction: 'bull', call_level: 19920 },
      { code: 'HK.50002', direction: 'bear', call_level: 20080 },
    ],
    histogram: [
      { bucket_low: 75, bucket_high: 80, pull_hkd: 200 },
      { bucket_low: 80, bucket_high: 85, pull_hkd: 1000 },
    ],
    recent_vetoes: [
      {
        kline_time: '2025-01-06 10:30:00',
        direction: 'BULL',
        reason_code: 'cbbc_dense_band_above',
      },
    ],
    ...overrides,
  };
}


describe('computeOverlayState', () => {
  it('hides the overlay when no payload is provided', () => {
    const state = computeOverlayState(null, []);
    assert.equal(state.active, false);
    assert.equal(state.showUnavailableBanner, false);
    assert.deepEqual(state.callLevels, []);
    assert.deepEqual(state.denseBuckets, []);
  });

  it('hides the overlay and shows the banner when degraded', () => {
    const state = computeOverlayState(
      basePayload({ cbbc_magnet_degraded: true }),
      ['10:30:00'],
    );
    assert.equal(state.active, false);
    assert.equal(state.showUnavailableBanner, true);
  });

  it('hides the overlay and shows the banner when decay_points missing', () => {
    const state = computeOverlayState(
      basePayload({ decay_points: undefined }),
      ['10:30:00'],
    );
    assert.equal(state.active, false);
    assert.equal(state.showUnavailableBanner, true);
  });

  it('passes call levels through and identifies dense buckets', () => {
    const state = computeOverlayState(basePayload(), ['10:30:00']);
    assert.equal(state.active, true);
    // Two call levels survive when active.
    assert.equal(state.callLevels.length, 2);
    // Bucket 80-85 has 1000/1200 ≈ 83% share → dense; 75-80 has 200/1200
    // ≈ 17% share which is also above the 15% threshold so both are dense.
    assert.equal(state.denseBuckets.length, 2);
  });
});


describe('isPriceInDenseBucket', () => {
  it('returns true when the price sits inside a bucket range', () => {
    const buckets = [{ bucket_low: 80, bucket_high: 85, pull_hkd: 1000 }];
    assert.equal(isPriceInDenseBucket(82, buckets), true);
    assert.equal(isPriceInDenseBucket(80, buckets), true);
  });

  it('treats bucket_high as exclusive', () => {
    const buckets = [{ bucket_low: 80, bucket_high: 85, pull_hkd: 1000 }];
    assert.equal(isPriceInDenseBucket(85, buckets), false);
  });

  it('returns false for prices outside any bucket', () => {
    const buckets = [{ bucket_low: 80, bucket_high: 85, pull_hkd: 1000 }];
    assert.equal(isPriceInDenseBucket(70, buckets), false);
  });
});


describe('formatVetoTime', () => {
  it('trims an ISO datetime to HH:MM:SS', () => {
    assert.equal(
      formatVetoTime('2025-01-06 10:30:00'),
      '10:30:00',
    );
  });

  it('passes already-short strings through unchanged', () => {
    assert.equal(formatVetoTime('10:30:00'), '10:30:00');
  });
});


describe('veto filtering', () => {
  it('only emits vetoes whose time is in the visible window', () => {
    const payload = basePayload({
      recent_vetoes: [
        {
          kline_time: '2025-01-06 10:30:00',
          direction: 'BULL',
          reason_code: 'cbbc_dense_band_above',
        },
        {
          kline_time: '2025-01-06 13:15:00',
          direction: 'BEAR',
          reason_code: 'cbbc_dense_band_below',
        },
      ],
    });
    // Only the 10:30:00 bar is visible.
    const state = computeOverlayState(payload, ['10:30:00']);
    assert.equal(state.visibleVetoes.length, 1);
    assert.equal(state.visibleVetoes[0].time, '10:30:00');
  });

  it('returns an empty veto list when overlay is inactive', () => {
    const payload = basePayload({ cbbc_magnet_degraded: true });
    const state = computeOverlayState(payload, ['10:30:00']);
    assert.deepEqual(state.visibleVetoes, []);
  });
});
