/**
 * Pure helpers for the CBBC magnet overlay rendered by ``PriceChart``.
 *
 * The visibility / dense-band / veto-marker rules live here (rather than
 * inline in the component) so they can be unit-tested without spinning up
 * a React renderer (cbbc-magnet-signal task 12.4).
 *
 * R8.6 visibility rule: the overlay is visible iff
 *   - ``magnetOverlay != null``
 *   - ``decay_points`` is a finite number
 *   - ``cbbc_magnet_degraded == false``
 *
 * Otherwise the component MUST hide all call-level lines, dense-band shading
 * and veto markers, and the title banner shows a "CBBC 磁吸数据不可用" hint.
 */
import type {
  MagnetOverlayCallLevel,
  MagnetOverlayHistogramBucket,
  MagnetOverlayPayload,
  MagnetOverlayVeto,
} from '../types';

/** Threshold (15%) above which a 5pt bucket is "dense" (R8.3). */
export const DENSE_BUCKET_SHARE_THRESHOLD = 0.15;

export interface MagnetOverlayState {
  active: boolean;
  callLevels: MagnetOverlayCallLevel[];
  denseBuckets: MagnetOverlayHistogramBucket[];
  visibleVetoes: VisibleVeto[];
  /** True when overlay is hidden but a payload exists; the UI shows the
   *  "数据不可用" banner. False when no overlay payload has arrived yet. */
  showUnavailableBanner: boolean;
}

export interface VisibleVeto extends MagnetOverlayVeto {
  /** ``HH:MM:SS`` — derived from ``kline_time`` via the shared formatter. */
  time: string;
}

/** Trim ``kline_time`` to the chart-level "HH:MM:SS" axis label format. */
export function formatVetoTime(kline_time: string): string {
  return kline_time.length > 10 ? kline_time.slice(11, 19) : kline_time;
}

/** Decide whether a price falls into a "dense" bucket (≥15% share). */
export function isPriceInDenseBucket(
  price: number,
  buckets: MagnetOverlayHistogramBucket[],
): boolean {
  return buckets.some((b) => price >= b.bucket_low && price < b.bucket_high);
}

/**
 * Compute the overlay state given a payload and the set of x-axis labels
 * currently visible on the chart.
 */
export function computeOverlayState(
  overlay: MagnetOverlayPayload | null | undefined,
  klineTimes: Iterable<string>,
): MagnetOverlayState {
  const active =
    overlay != null &&
    typeof overlay.decay_points === 'number' &&
    Number.isFinite(overlay.decay_points) &&
    !overlay.cbbc_magnet_degraded;

  if (!active) {
    return {
      active: false,
      callLevels: [],
      denseBuckets: [],
      visibleVetoes: [],
      showUnavailableBanner: overlay != null,
    };
  }

  const histogramTotal = (overlay!.histogram ?? []).reduce(
    (acc, b) => acc + Math.max(0, b.pull_hkd),
    0,
  );
  const denseBuckets = (overlay!.histogram ?? []).filter(
    (b) =>
      histogramTotal > 0 &&
      b.pull_hkd / histogramTotal >= DENSE_BUCKET_SHARE_THRESHOLD,
  );

  const klineTimeSet = new Set(klineTimes);
  const visibleVetoes = (overlay!.recent_vetoes ?? [])
    .map((v) => ({ ...v, time: formatVetoTime(v.kline_time) }))
    .filter((v) => klineTimeSet.has(v.time));

  return {
    active: true,
    callLevels: overlay!.call_levels ?? [],
    denseBuckets,
    visibleVetoes,
    showUnavailableBanner: false,
  };
}
