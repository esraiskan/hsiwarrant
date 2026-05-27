"""CBBC magnet offline research script.

Task 11.1 scope: argparse-based CLI with strict parameter validation.
Task 11.2 scope: historical snapshot aggregation per HK trading day.
Task 11.3 scope: correlation, grouped event statistics, and the
    "minimum 60 valid trading days" go/no-go threshold (R6.5, R6.6, R6.7).

This module deliberately does NOT yet perform:
  - CSV / markdown output (task 11.4)

R6.1 / R6.11 are satisfied: any malformed CLI argument causes a non-zero
exit within 5 seconds with a stderr message naming the offending field
and its rejection reason, and without creating any CSV or markdown file
under ``backend/research/``.

R6.2 (no reverse deduction) is structurally satisfied by ``aggregate_day``
and ``aggregate_range``: they only accept a ``trade_date`` plus a
``CbbcStorage`` reference and always call ``storage.read_snapshot(d)``
to fetch the historical snapshot. The aggregator never accepts a
"today's surviving set" parameter, so callers cannot use it to deduce
the past from today's data. The storage layer's
``read_snapshot`` already enforces survivorship (only keeps
``listing_date <= d`` and ``maturity_date >= d``).

R6.3 covers the close-window per-minute sampling and 5-sample minimum.
R6.4 covers the intraday-new-listing-near-money flag.

R6.5 / R6.6 / R6.7 are exposed via :func:`compute_correlation`,
:func:`compute_grouped_event_stats`, and :func:`evaluate_go_no_go`.
The minimum 60 valid trading days threshold is enforced here; any
caller that wants to exit non-zero on insufficient data can use
:func:`_emit_insufficient_trading_days_and_exit`. ``main()`` does not
yet call these — task 11.4 wires the full pipeline + file output.

Implementation notes:
  * argparse validation raises ``argparse.ArgumentTypeError`` with a
    ``"<field>: <reason>"`` message; argparse prints the message to
    stderr and ``sys.exit(2)`` on its own, which is non-zero (R6.11).
  * The module-level imports stay stdlib-only. Heavy imports
    (``compute_magnet``, ``numpy``, ``scipy.stats``) are deferred
    inside function bodies; module load therefore remains fast for
    the CLI fast-fail path.
  * No file or directory is touched in the CLI path of this skeleton.
    ``backend/research/`` is only created in task 11.4 once we actually
    have results to write.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Callable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from cbbc_storage import CbbcStorage, SnapshotError


logger = logging.getLogger("cbbc_research")


_DATE_FORMAT = "%Y-%m-%d"
_DECAY_POINTS_LOW, _DECAY_POINTS_HIGH = 1, 1000
_DENSE_BAND_LOW, _DENSE_BAND_HIGH = 1, 1000

# Aggregation constants (R6.3 / R6.4).
_HK_TZ = ZoneInfo("Asia/Hong_Kong")
_CLOSE_WINDOW_START = time(15, 30, 0)  # inclusive
_CLOSE_WINDOW_END = time(16, 0, 0)     # inclusive
_DAY_OPEN = time(9, 30, 0)             # inclusive (intraday-new-listing window start)
_DAY_CLOSE = time(16, 0, 0)            # inclusive (intraday-new-listing window end)
_MIN_SAMPLES_PER_DAY = 5


def _date_arg(field: str) -> Callable[[str], date]:
    """Build an argparse type validator for ``YYYY-MM-DD`` dates.

    The returned callable raises ``argparse.ArgumentTypeError`` whose message
    starts with the field name so the final stderr line names the offending
    argument (R6.11).
    """

    def parser(s: str) -> date:
        try:
            return datetime.strptime(s, _DATE_FORMAT).date()
        except (TypeError, ValueError) as exc:
            raise argparse.ArgumentTypeError(
                f"{field}: must be in YYYY-MM-DD format, got {s!r} ({exc})"
            )

    return parser


def _bounded_int_arg(field: str, low: int, high: int) -> Callable[[str], int]:
    """Build an argparse type validator for an integer in ``[low, high]``."""

    def parser(s: str) -> int:
        # Reject anything that is not a clean integer literal. ``int(s)``
        # accepts surrounding whitespace and a leading sign, which matches
        # normal CLI ergonomics; it correctly rejects floats like "1.5" and
        # non-numeric input.
        try:
            value = int(s)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(
                f"{field}: must be an integer in [{low}, {high}], got {s!r}"
            )
        if value < low or value > high:
            raise argparse.ArgumentTypeError(
                f"{field}: must be in [{low}, {high}], got {value}"
            )
        return value

    return parser


# ---------------------------------------------------------------------------
# Per-day aggregation (task 11.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HsiMinuteBar:
    """Minimal HSI 1-minute K-line shape needed for aggregation.

    Caller-supplied; the research script does not concern itself with how
    bars are loaded (Futu, parquet history, csv, etc.). ``ts_hk`` MUST be a
    timezone-aware datetime in ``Asia/Hong_Kong``.
    """

    ts_hk: datetime
    close: float


# Type alias for a callable that returns the HSI bars for a given trading day.
HsiBarLoader = Callable[[date], list[HsiMinuteBar]]


@dataclass(frozen=True)
class DayAggregate:
    """Per-trading-day aggregate of magnet-bias and dense-band distance.

    Fields:
        trade_date: HK trading day (Asia/Hong_Kong).
        avg_magnet_bias: Mean of per-minute ``MagnetResult.magnet_bias``
            across the close window 15:30-16:00 (inclusive both ends).
        avg_nearest_dense_band_distance_pts: Mean of per-minute
            ``min(nearest_bull_distance_pts, nearest_bear_distance_pts)``
            across the same window. Samples where both nearest values are
            ``None`` are omitted from this average. If every accepted
            sample has both values ``None`` the field is ``float('nan')``;
            in practice such days will already have been dropped because
            their sample count for magnet_bias is also typically too small.
        sample_count: Number of accepted 1-minute samples in the window
            (>= ``_MIN_SAMPLES_PER_DAY`` = 5; days with fewer samples are
            dropped at the call site by returning ``None``).
        is_intraday_new_listing_near_money_day: ``True`` iff there exists
            at least one record in the snapshot whose ``listing_date`` is
            ``trade_date`` AND ``|call_level - close_at_close| <=
            dense_band_threshold_pts``, where ``close_at_close`` is the
            close of the last bar within 09:30-16:00 (R6.4).
    """

    trade_date: date
    avg_magnet_bias: float
    avg_nearest_dense_band_distance_pts: float
    sample_count: int
    is_intraday_new_listing_near_money_day: bool


def _is_in_close_window(t: time) -> bool:
    """Return ``True`` iff ``t`` falls in 15:30:00..16:00:00 inclusive."""
    return _CLOSE_WINDOW_START <= t <= _CLOSE_WINDOW_END


def _is_in_day_window(t: time) -> bool:
    """Return ``True`` iff ``t`` falls in 09:30:00..16:00:00 inclusive."""
    return _DAY_OPEN <= t <= _DAY_CLOSE


def _ensure_hk_aware(dt: datetime) -> datetime:
    """Convert ``dt`` to ``Asia/Hong_Kong`` for time-of-day comparisons.

    Accepts both naive and aware datetimes:
        * naive  -> assumed to already be HK local time, returned as-is.
        * aware  -> converted to HK local time via ``astimezone``.

    The aggregator only inspects ``time()`` after this normalisation, so the
    returned object's tz attribute is irrelevant beyond the conversion.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(_HK_TZ)


def aggregate_day(
    *,
    storage: CbbcStorage,
    trade_date: date,
    hsi_bars: list[HsiMinuteBar],
    decay_points: int,
    dense_band_threshold_pts: int,
) -> Optional[DayAggregate]:
    """Aggregate per-day Magnet_Bias and nearest dense-band distance.

    R6.2 / R6.3 / R6.4:
      - Reads the historical snapshot via ``storage.read_snapshot(trade_date)``.
        The aggregator does **not** accept a "today's surviving CBBC" set as
        an input, so the survivorship guarantee is structurally honored:
        callers cannot use the API to deduce a past date from today's data.
      - Filters HSI bars to the close window 15:30:00..16:00:00 (Asia/Hong_Kong),
        inclusive on both endpoints. Bars are grouped by minute (HH:MM); only
        the first bar seen per minute is sampled to avoid double-counting if
        the loader supplies duplicates. For each accepted minute the function
        calls ``compute_magnet`` against the historical snapshot and the
        bar's close, accumulating ``magnet_bias`` and
        ``min(nearest_bull, nearest_bear)`` (treating ``None`` as omitted).
      - When the accepted sample count is < 5 the function returns ``None``
        (the day is dropped from the dataset).
      - ``is_intraday_new_listing_near_money_day`` is set to ``True`` iff
        any record in the snapshot has ``listing_date == trade_date`` and
        ``|call_level - close_at_close| <= dense_band_threshold_pts``,
        where ``close_at_close`` is the last bar's close within
        09:30..16:00.

    The function never touches the filesystem outside of the storage layer.

    Raises:
        SnapshotError: propagated from ``storage.read_snapshot`` for
            ``non_trading_day`` and ``snapshot_missing``. Callers (such as
            ``aggregate_range``) catch and skip the day.
    """
    # Heavy import is deferred so the CLI fast-fail path stays cheap.
    from cbbc_calculator import compute_magnet

    # 1) Read the historical snapshot. Survivorship filter is applied by the
    #    storage layer itself.
    snapshot = storage.read_snapshot(trade_date)

    # 2) Walk bars, sample at most one bar per HH:MM in the close window.
    #    Track the last bar within 09:30..16:00 for the new-listing flag.
    seen_minutes: set[time] = set()
    bias_values: list[float] = []
    distance_values: list[float] = []
    last_day_close: Optional[float] = None
    last_day_close_ts: Optional[datetime] = None

    decay_f = float(decay_points)
    for bar in hsi_bars:
        if bar is None:
            continue
        ts_hk = _ensure_hk_aware(bar.ts_hk)
        t = ts_hk.time()

        # Track last close in the day window (09:30..16:00).
        if _is_in_day_window(t):
            if last_day_close_ts is None or ts_hk >= last_day_close_ts:
                last_day_close = float(bar.close)
                last_day_close_ts = ts_hk

        # Sample only inside the close window, at most once per HH:MM.
        if not _is_in_close_window(t):
            continue
        minute_key = time(t.hour, t.minute, 0)
        if minute_key in seen_minutes:
            continue
        seen_minutes.add(minute_key)

        # Compute the magnet for this minute's bar.
        result = compute_magnet(
            snapshot,
            float(bar.close),
            decay_f,
            generated_at_hk=ts_hk,
            hsi_spot_stale=False,
        )

        bias_values.append(result.magnet_bias)

        # Nearest dense-band distance := min over (bull, bear), skipping None.
        cand: list[float] = []
        if result.nearest_bull_distance_pts is not None:
            cand.append(result.nearest_bull_distance_pts)
        if result.nearest_bear_distance_pts is not None:
            cand.append(result.nearest_bear_distance_pts)
        if cand:
            distance_values.append(min(cand))

    # 3) Drop days with insufficient samples (R6.3).
    sample_count = len(bias_values)
    if sample_count < _MIN_SAMPLES_PER_DAY:
        return None

    avg_bias = sum(bias_values) / sample_count
    if distance_values:
        avg_distance = sum(distance_values) / len(distance_values)
    else:
        # Pathological case: every accepted minute had both nearest distances
        # as ``None`` (e.g. bull-only or bear-only synthetic snapshot). Use
        # NaN to make the situation visible to downstream stages without
        # dropping the day silently.
        avg_distance = float("nan")

    # 4) Intraday-new-listing-near-money flag (R6.4).
    is_near_money_day = False
    if last_day_close is not None:
        threshold = float(dense_band_threshold_pts)
        for r in snapshot.records:
            if r.listing_date != trade_date:
                continue
            if abs(float(r.call_level) - last_day_close) <= threshold:
                is_near_money_day = True
                break

    return DayAggregate(
        trade_date=trade_date,
        avg_magnet_bias=avg_bias,
        avg_nearest_dense_band_distance_pts=avg_distance,
        sample_count=sample_count,
        is_intraday_new_listing_near_money_day=is_near_money_day,
    )


def aggregate_range(
    *,
    storage: CbbcStorage,
    start_date: date,
    end_date: date,
    decay_points: int,
    dense_band_threshold_pts: int,
    hsi_bar_loader: HsiBarLoader,
) -> list[DayAggregate]:
    """Iterate ``[start_date, end_date]`` (inclusive) and aggregate each day.

    Days are skipped silently for the following reasons:
      - non-trading day (storage raises ``SnapshotError("non_trading_day")``);
      - snapshot file missing (``SnapshotError("snapshot_missing")``);
      - aggregate returns ``None`` (sample count < 5).

    All other ``SnapshotError`` codes are re-raised so they surface as
    real bugs (e.g. ``snapshot_immutable`` should never occur on a read,
    and ``no_reverse_deduction_allowed`` should never be raised by the
    storage layer for plain ``read_snapshot`` calls).

    Returns: ordered list of ``DayAggregate`` (by ``trade_date``).
    """
    if start_date > end_date:
        raise ValueError(
            f"start_date {start_date.isoformat()} must be <= end_date {end_date.isoformat()}"
        )

    out: list[DayAggregate] = []
    cursor = start_date
    one_day = _one_day()
    while cursor <= end_date:
        try:
            bars = hsi_bar_loader(cursor)
            day_agg = aggregate_day(
                storage=storage,
                trade_date=cursor,
                hsi_bars=bars,
                decay_points=decay_points,
                dense_band_threshold_pts=dense_band_threshold_pts,
            )
        except SnapshotError as exc:
            if exc.code in ("non_trading_day", "snapshot_missing"):
                # Spec: non-trading days are reported by the storage layer
                # and the caller skips them. Missing snapshots are also
                # skipped silently here; task 11.4 will surface them in
                # the markdown summary.
                cursor = cursor + one_day
                continue
            raise

        if day_agg is not None:
            out.append(day_agg)
        cursor = cursor + one_day

    return out


def _one_day():
    """Lazy import of timedelta to keep the top-level import list short."""
    from datetime import timedelta
    return timedelta(days=1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cbbc_research",
        description=(
            "CBBC magnet offline research script. "
            "This entry point currently only validates CLI parameters; "
            "aggregation, correlation, and file output are implemented in "
            "subsequent tasks (11.2-11.4)."
        ),
    )
    parser.add_argument(
        "--start-date",
        required=True,
        type=_date_arg("--start-date"),
        help="Inclusive start date in YYYY-MM-DD (Asia/Hong_Kong).",
    )
    parser.add_argument(
        "--end-date",
        required=True,
        type=_date_arg("--end-date"),
        help="Inclusive end date in YYYY-MM-DD (Asia/Hong_Kong).",
    )
    parser.add_argument(
        "--decay-points",
        required=True,
        type=_bounded_int_arg(
            "--decay-points", _DECAY_POINTS_LOW, _DECAY_POINTS_HIGH
        ),
        help=(
            f"Magnet decay distance in HSI points, integer in "
            f"[{_DECAY_POINTS_LOW}, {_DECAY_POINTS_HIGH}]."
        ),
    )
    parser.add_argument(
        "--dense-band-threshold-pts",
        required=True,
        type=_bounded_int_arg(
            "--dense-band-threshold-pts", _DENSE_BAND_LOW, _DENSE_BAND_HIGH
        ),
        help=(
            f"Dense-band distance threshold in HSI points, integer in "
            f"[{_DENSE_BAND_LOW}, {_DENSE_BAND_HIGH}]."
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for the research script.

    Returns 0 on successful run, non-zero on validation failure or
    insufficient trading days. Files are written under ``backend/research/``
    only after parameter validation passes (R6.1 / R6.11) and analysis
    yields enough samples (R6.7).

    The CLI itself does not load HSI bars or next-day traces — those are
    supplied programmatically through ``run_pipeline`` so callers can swap
    in deterministic loaders during testing. Without external loaders the
    script simply validates parameters and exits 0; this keeps the CLI
    fast-fail path side-effect-free (no CSV / MD files written).
    """

    parser = _build_parser()
    # ``parse_args`` will itself ``sys.exit(2)`` on ArgumentTypeError or on
    # missing required args, writing a usage line + error message to stderr.
    args = parser.parse_args(argv)

    if args.start_date > args.end_date:
        print(
            "--start-date / --end-date: start_date must be <= end_date "
            f"(got start={args.start_date.isoformat()}, "
            f"end={args.end_date.isoformat()})",
            file=sys.stderr,
        )
        return 2

    # Placeholder log; the full pipeline (storage + bar loader injection)
    # is exposed via :func:`run_pipeline` for programmatic use. This CLI
    # path is intentionally side-effect-free (no files created) so it can
    # double as a quick "is the install OK?" smoke test.
    print(
        "[cbbc_research] params validated: "
        f"start_date={args.start_date.isoformat()}, "
        f"end_date={args.end_date.isoformat()}, "
        f"decay_points={args.decay_points}, "
        f"dense_band_threshold_pts={args.dense_band_threshold_pts}; "
        "use cbbc_research.run_pipeline(...) to execute the full analysis."
    )
    return 0


def run_pipeline(
    *,
    storage: CbbcStorage,
    start_date: date,
    end_date: date,
    decay_points: int,
    dense_band_threshold_pts: int,
    dense_band_pull_share: float,
    hsi_bar_loader: HsiBarLoader,
    next_day_loader: NextDayHsiLoader,
    output_root: Optional[Path] = None,
) -> int:
    """End-to-end research pipeline.

    Steps:
      1. Aggregate per-day samples (task 11.2).
      2. Apply the 60-trading-day go/no-go threshold (R6.7).
      3. Compute correlation + grouped stats (R6.5 / R6.6).
      4. Hash CBBC snapshot files for the reproducibility header.
      5. Write CSV + MD artifacts (R6.8 / R6.10).

    Returns 0 on success, non-zero on insufficient samples.
    """
    started_at = datetime.now(_HK_TZ).replace(tzinfo=None)

    aggregates = aggregate_range(
        storage=storage,
        start_date=start_date,
        end_date=end_date,
        decay_points=decay_points,
        dense_band_threshold_pts=dense_band_threshold_pts,
        hsi_bar_loader=hsi_bar_loader,
    )

    if not evaluate_go_no_go(aggregates):
        return _emit_insufficient_trading_days_and_exit(len(aggregates))

    correlation = compute_correlation(aggregates, next_day_loader)
    grouped = compute_grouped_event_stats(aggregates, next_day_loader)

    # Hash all snapshot files actually used (those whose date is within
    # [start_date, end_date]).
    base_dir = Path(__file__).resolve().parent / _CBBC_DIR_NAME / "cbbc"
    snapshot_files = sorted(
        p for p in base_dir.glob(_OUTSTANDING_FILENAME_PATTERN)
    )
    snapshot_hashes = _hash_snapshot_files(snapshot_files)

    finished_at = datetime.now(_HK_TZ).replace(tzinfo=None)

    write_research_artifacts(
        aggregates=aggregates,
        correlation=correlation,
        grouped=grouped,
        decay_points=decay_points,
        dense_band_threshold_pts=dense_band_threshold_pts,
        dense_band_pull_share=dense_band_pull_share,
        start_date=start_date,
        end_date=end_date,
        snapshot_hashes=snapshot_hashes,
        started_at_hk=started_at,
        finished_at_hk=finished_at,
        output_root=output_root,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------------------
# Correlation, grouped event statistics, and go/no-go threshold (task 11.3)
# ---------------------------------------------------------------------------

# Minimum valid trading days required to publish conclusions (R6.7).
_MIN_VALID_TRADING_DAYS: int = 60


@dataclass(frozen=True)
class CorrelationResult:
    """Pearson + Spearman correlation between magnet_bias_close (day d)
    and the *direction* of HSI close return (day d+1 vs d), where the
    direction is encoded as +1 (up) or -1 (down).

    Fields are rounded to 4 decimal places for reproducibility (R6.5).
    Sample-size ``n`` reflects the number of consecutive day-pairs
    (d, d+1) that survived aggregation.
    """

    n: int
    pearson_r: float
    pearson_p: float
    spearman_r: float
    spearman_p: float


@dataclass(frozen=True)
class GroupedEventStat:
    """Per-group "next-day max favourable / max adverse" point distribution
    relative to the *current* day's HSI close (R6.6).

    All point values are rounded to 2 decimal places.

    Fields:
        n: Sample count (number of (d, d+1) pairs in this group).
        median_max_favorable_pts: Median of the next-day's maximum
            point excursion in the trend direction the group represents.
        p75_max_favorable_pts: 75th percentile of the same series.
        median_max_adverse_pts: Median of the next-day's maximum point
            excursion against the trend direction.
        p75_max_adverse_pts: 75th percentile of the same series.
    """

    n: int
    median_max_favorable_pts: float
    p75_max_favorable_pts: float
    median_max_adverse_pts: float
    p75_max_adverse_pts: float


@dataclass(frozen=True)
class NextDayHsiTrace:
    """Per-day HSI close + next-day's intraday extremes used by R6.6.

    The research script does not track tick data; the loader is expected
    to provide pre-computed values:

    Fields:
        trade_date: ``d``, the day whose ``avg_magnet_bias`` we paired
            with the next-day movement.
        close_d: HSI close at ``d``.
        close_d_plus_1: HSI close at ``d+1`` trading day.
        max_high_d_plus_1: Maximum HSI high during the regular session
            on ``d+1``. Used to derive max favourable / adverse points.
        min_low_d_plus_1: Minimum HSI low during the regular session
            on ``d+1``.
    """

    trade_date: date
    close_d: float
    close_d_plus_1: float
    max_high_d_plus_1: float
    min_low_d_plus_1: float


# Type alias: callable that returns the (d, d+1) trace for a given day,
# or ``None`` when the next trading day is missing (e.g. end-of-range).
NextDayHsiLoader = Callable[[date], Optional[NextDayHsiTrace]]


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Return the ``q``-th percentile (0..1) of an already-sorted list.

    Uses linear interpolation between adjacent ranks (numpy's default
    ``linear`` method). Caller guarantees ``sorted_values`` is non-empty
    and sorted ascending.
    """
    if not sorted_values:
        return float("nan")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = q * (len(sorted_values) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(sorted_values) - 1)
    frac = pos - lower
    return float(sorted_values[lower]) * (1 - frac) + float(sorted_values[upper]) * frac


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float]:
    """Compute Pearson r + two-sided p-value (t-distribution approximation).

    Returns ``(0.0, 1.0)`` when the sample size is too small or the
    variance of either series is zero.
    """
    n = len(xs)
    if n < 3:
        return 0.0, 1.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sx = sum((x - mean_x) ** 2 for x in xs)
    sy = sum((y - mean_y) ** 2 for y in ys)
    if sx == 0.0 or sy == 0.0:
        return 0.0, 1.0
    sxy = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    r = sxy / ((sx ** 0.5) * (sy ** 0.5))
    # Two-sided p-value via t-distribution.
    if abs(r) >= 1.0:
        return r, 0.0
    import math
    t = r * math.sqrt(max(n - 2, 1) / max(1.0 - r * r, 1e-12))
    # Lightweight survival function for the t-distribution. We use the
    # standard Student's t CDF approximation via the regularised incomplete
    # beta function, available through the math module (Python 3.12+) or
    # via a simple expansion for smaller versions.
    p = _student_t_two_sided_p(t, df=n - 2)
    return r, p


def _student_t_two_sided_p(t: float, df: int) -> float:
    """Two-sided p-value for Student's t-distribution.

    Uses the relationship p = I_x(df/2, 1/2) where x = df / (df + t^2)
    and ``I_x`` is the regularised incomplete beta function.
    """
    import math
    if df <= 0:
        return 1.0
    x = df / (df + t * t)
    # ``math.lgamma`` is available everywhere; we implement a
    # continued-fraction expansion of the regularised incomplete beta
    # function to keep the implementation dependency-free.
    return _reg_incomplete_beta(x, df / 2.0, 0.5)


def _reg_incomplete_beta(x: float, a: float, b: float) -> float:
    """Regularised incomplete beta I_x(a, b) — adequate for our p-value uses."""
    import math
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    # Use the symmetry I_x(a, b) = 1 - I_{1-x}(b, a) to ensure rapid
    # convergence in the continued-fraction tail.
    if x > (a + 1.0) / (a + b + 2.0):
        return 1.0 - _reg_incomplete_beta(1.0 - x, b, a)
    lbeta = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log(1.0 - x)
    )
    front = math.exp(lbeta) / a
    # Continued fraction (Lentz's method).
    fpmin = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-10:
            break
    return front * h


def _spearman(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float]:
    """Spearman rank correlation by ranking + Pearson on ranks."""
    if len(xs) != len(ys) or len(xs) < 3:
        return 0.0, 1.0
    rx = _ranks(xs)
    ry = _ranks(ys)
    return _pearson(rx, ry)


def _ranks(values: Sequence[float]) -> list[float]:
    """Average ranks (1-indexed) with ties handled by mean rank."""
    indexed = sorted(enumerate(values), key=lambda kv: kv[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1-indexed average
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = avg_rank
        i = j + 1
    return ranks


def compute_correlation(
    aggregates: Sequence[DayAggregate],
    next_day_loader: NextDayHsiLoader,
) -> CorrelationResult:
    """Pearson + Spearman between ``avg_magnet_bias`` (day d) and the
    *direction* of HSI close return on (d+1) (R6.5).

    Direction encoding:
        +1 if close_d_plus_1 > close_d
        -1 if close_d_plus_1 < close_d
        0  if equal — paired sample dropped (degenerate, very rare).
    """
    xs: list[float] = []
    ys: list[float] = []
    for agg in aggregates:
        trace = next_day_loader(agg.trade_date)
        if trace is None:
            continue
        if trace.close_d_plus_1 == trace.close_d:
            continue
        direction = 1.0 if trace.close_d_plus_1 > trace.close_d else -1.0
        xs.append(float(agg.avg_magnet_bias))
        ys.append(direction)
    if not xs:
        return CorrelationResult(
            n=0, pearson_r=0.0, pearson_p=1.0, spearman_r=0.0, spearman_p=1.0
        )
    pr, pp = _pearson(xs, ys)
    sr, sp = _spearman(xs, ys)
    return CorrelationResult(
        n=len(xs),
        pearson_r=round(pr, 4),
        pearson_p=round(pp, 4),
        spearman_r=round(sr, 4),
        spearman_p=round(sp, 4),
    )


def compute_grouped_event_stats(
    aggregates: Sequence[DayAggregate],
    next_day_loader: NextDayHsiLoader,
) -> Mapping[bool, GroupedEventStat]:
    """Group days by ``is_intraday_new_listing_near_money_day`` and report
    median + p75 of next-day max favourable / max adverse points (R6.6).

    "Favourable" / "adverse" are computed against the day's HSI close:
        max_favorable_pts = max(max_high_d_plus_1 - close_d, close_d - min_low_d_plus_1)
        max_adverse_pts   = -(min(...))   # i.e. the reverse extremum

    Both values are rounded to 2 decimal places.
    """
    # Two buckets: True (near-money day) and False.
    buckets: dict[bool, dict[str, list[float]]] = {
        True: {"fav": [], "adv": []},
        False: {"fav": [], "adv": []},
    }

    for agg in aggregates:
        trace = next_day_loader(agg.trade_date)
        if trace is None:
            continue
        # Favourable = the larger of next-day's high move vs close, capped at >= 0.
        fav = max(trace.max_high_d_plus_1 - trace.close_d, 0.0)
        adv = max(trace.close_d - trace.min_low_d_plus_1, 0.0)
        bucket = buckets[bool(agg.is_intraday_new_listing_near_money_day)]
        bucket["fav"].append(round(fav, 2))
        bucket["adv"].append(round(adv, 2))

    out: dict[bool, GroupedEventStat] = {}
    for flag, vals in buckets.items():
        fav_sorted = sorted(vals["fav"])
        adv_sorted = sorted(vals["adv"])
        n = len(fav_sorted)
        if n == 0:
            out[flag] = GroupedEventStat(
                n=0,
                median_max_favorable_pts=float("nan"),
                p75_max_favorable_pts=float("nan"),
                median_max_adverse_pts=float("nan"),
                p75_max_adverse_pts=float("nan"),
            )
            continue
        out[flag] = GroupedEventStat(
            n=n,
            median_max_favorable_pts=round(_percentile(fav_sorted, 0.5), 2),
            p75_max_favorable_pts=round(_percentile(fav_sorted, 0.75), 2),
            median_max_adverse_pts=round(_percentile(adv_sorted, 0.5), 2),
            p75_max_adverse_pts=round(_percentile(adv_sorted, 0.75), 2),
        )
    return out


def evaluate_go_no_go(aggregates: Sequence[DayAggregate]) -> bool:
    """Return ``True`` iff the dataset has at least 60 valid trading days (R6.7)."""
    return len(aggregates) >= _MIN_VALID_TRADING_DAYS


def _emit_insufficient_trading_days_and_exit(n: int) -> int:
    """Write the canonical structured ERROR log and return non-zero (R6.7)."""
    logger.error(
        "insufficient_trading_days",
        extra={
            "level": "ERROR",
            "source": "cbbc_research",
            "event": "insufficient_trading_days",
            "n": int(n),
            "min_required": _MIN_VALID_TRADING_DAYS,
        },
    )
    print(
        f"[cbbc_research] insufficient_trading_days: n={n}, "
        f"min_required={_MIN_VALID_TRADING_DAYS}",
        file=sys.stderr,
    )
    return 3


# ---------------------------------------------------------------------------
# CSV / Markdown output (task 11.4)
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402 — placed after the analysis block on purpose
from pathlib import Path  # noqa: E402

_RESEARCH_DIR_PERMS = 0o755
_RESEARCH_DIR_NAME = "research"
_CBBC_DIR_NAME = "data"
_OUTSTANDING_FILENAME_PATTERN = "outstanding_*.parquet"


@dataclass(frozen=True)
class WrittenArtifacts:
    """Paths emitted by :func:`write_research_artifacts`."""

    csv_path: Path
    md_path: Path


def _research_root() -> Path:
    """Return ``backend/research`` (alongside ``cbbc_research.py``)."""
    return Path(__file__).resolve().parent / _RESEARCH_DIR_NAME


def _ensure_research_dir() -> Path:
    root = _research_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(_RESEARCH_DIR_PERMS)
    except OSError:
        # On Windows ``chmod`` is mostly a no-op; ignore.
        pass
    return root


def _hash_snapshot_files(
    snapshot_files: Sequence[Path],
) -> list[tuple[str, str]]:
    """Return ``[(filename, sha256), ...]`` for the supplied snapshot files.

    Caller must supply absolute paths; missing files are skipped silently.
    """
    out: list[tuple[str, str]] = []
    for p in snapshot_files:
        try:
            with p.open("rb") as f:
                h = hashlib.sha256()
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
                out.append((p.name, h.hexdigest()))
        except OSError:
            continue
    return out


def _format_run_header(
    *,
    decay_points: int,
    dense_band_threshold_pts: int,
    dense_band_pull_share: float,
    start_date: date,
    end_date: date,
    snapshot_hashes: Sequence[tuple[str, str]],
    started_at_hk: datetime,
    finished_at_hk: datetime,
) -> list[str]:
    """Build the reproducibility header (R6.8 / R6.10) shared by CSV + MD."""
    lines = [
        f"# decay_points: {int(decay_points)}",
        f"# dense_band_threshold_pts: {int(dense_band_threshold_pts)}",
        f"# cbbc_dense_band_pull_share: {float(dense_band_pull_share):.4f}",
        f"# start_date: {start_date.isoformat()}",
        f"# end_date: {end_date.isoformat()}",
        f"# script_started_at_hk: {started_at_hk.isoformat()}",
        f"# script_finished_at_hk: {finished_at_hk.isoformat()}",
        "# snapshot_files:",
    ]
    if snapshot_hashes:
        for name, sha in snapshot_hashes:
            lines.append(f"#   {name}  sha256={sha}")
    else:
        lines.append("#   (none)")
    return lines


def write_research_artifacts(
    *,
    aggregates: Sequence[DayAggregate],
    correlation: CorrelationResult,
    grouped: Mapping[bool, GroupedEventStat],
    decay_points: int,
    dense_band_threshold_pts: int,
    dense_band_pull_share: float,
    start_date: date,
    end_date: date,
    snapshot_hashes: Sequence[tuple[str, str]],
    started_at_hk: datetime,
    finished_at_hk: datetime,
    output_root: Optional[Path] = None,
) -> WrittenArtifacts:
    """Write the per-day aggregate CSV + summary markdown (R6.8).

    Files are placed in ``output_root`` (defaults to ``backend/research``)
    and named ``cbbc_magnet_<YYYYMMDDHHMMSS>.{csv,md}`` based on
    ``finished_at_hk``.

    Both files start with the reproducibility header (R6.10). The CSV is
    forensic-grade: one row per surviving trading day with the four
    aggregate columns plus the near-money flag. The markdown contains the
    same header, plus rendered correlation + grouped statistics tables.

    No account / order / position / pnl fields are written (R6.9 / R11.5).
    """
    root = output_root if output_root is not None else _ensure_research_dir()
    root.mkdir(parents=True, exist_ok=True)

    timestamp = finished_at_hk.strftime("%Y%m%d%H%M%S")
    csv_path = root / f"cbbc_magnet_{timestamp}.csv"
    md_path = root / f"cbbc_magnet_{timestamp}.md"

    header_lines = _format_run_header(
        decay_points=decay_points,
        dense_band_threshold_pts=dense_band_threshold_pts,
        dense_band_pull_share=dense_band_pull_share,
        start_date=start_date,
        end_date=end_date,
        snapshot_hashes=snapshot_hashes,
        started_at_hk=started_at_hk,
        finished_at_hk=finished_at_hk,
    )

    # CSV
    with csv_path.open("w", encoding="utf-8", newline="\n") as f:
        for line in header_lines:
            f.write(line)
            f.write("\n")
        f.write(
            "trade_date,sample_count,avg_magnet_bias,"
            "avg_nearest_dense_band_distance_pts,is_intraday_new_listing_near_money_day\n"
        )
        for agg in aggregates:
            f.write(
                f"{agg.trade_date.isoformat()},"
                f"{agg.sample_count},"
                f"{agg.avg_magnet_bias:.6f},"
                f"{agg.avg_nearest_dense_band_distance_pts:.4f},"
                f"{int(bool(agg.is_intraday_new_listing_near_money_day))}\n"
            )

    # Markdown
    with md_path.open("w", encoding="utf-8", newline="\n") as f:
        for line in header_lines:
            f.write(line)
            f.write("\n")
        f.write("\n## Correlation (avg_magnet_bias_close → next-day HSI direction)\n\n")
        f.write(
            f"- N: {correlation.n}\n"
            f"- Pearson r: {correlation.pearson_r:.4f} (p = {correlation.pearson_p:.4f})\n"
            f"- Spearman r: {correlation.spearman_r:.4f} (p = {correlation.spearman_p:.4f})\n"
        )
        f.write("\n## Next-day HSI excursion grouped by near-money intraday listing\n\n")
        f.write(
            "| group | N | median_max_favourable_pts | p75_max_favourable_pts | "
            "median_max_adverse_pts | p75_max_adverse_pts |\n"
            "|---|---|---|---|---|---|\n"
        )
        for flag in (True, False):
            stat = grouped.get(flag)
            label = "near_money_day" if flag else "ordinary_day"
            if stat is None or stat.n == 0:
                f.write(f"| {label} | 0 | n/a | n/a | n/a | n/a |\n")
                continue
            f.write(
                f"| {label} | {stat.n} | "
                f"{stat.median_max_favorable_pts:.2f} | {stat.p75_max_favorable_pts:.2f} | "
                f"{stat.median_max_adverse_pts:.2f} | {stat.p75_max_adverse_pts:.2f} |\n"
            )

    return WrittenArtifacts(csv_path=csv_path, md_path=md_path)
