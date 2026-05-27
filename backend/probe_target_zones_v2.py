"""分析今天 CBBC 街货分布,使用 today_low/today_high 兜底过滤,
找出真正有意义的目标位 / 支撑位。"""
from __future__ import annotations

import json
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cbbc_storage import CbbcStorage  # noqa: E402

BUCKET_PTS = 25


def main() -> int:
    today_hk = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    storage = CbbcStorage()
    snap = storage.read_snapshot(today_hk)

    state = json.loads(urllib.request.urlopen("http://127.0.0.1:6000/api/state").read())
    spot = float(state["current_price"])

    extremes = json.loads(
        urllib.request.urlopen("http://127.0.0.1:6000/api/cbbc/today-extremes").read()
    )
    today_low = extremes.get("today_low")
    today_high = extremes.get("today_high")

    # Filter pipeline: street_vol > 0 AND not killed by today's high/low.
    live = []
    killed_count = 0
    for r in snap.records:
        if r.outstanding_shares <= 0:
            continue
        if r.direction == "bull" and today_low is not None and r.call_level >= today_low:
            killed_count += 1
            continue
        if r.direction == "bear" and today_high is not None and r.call_level <= today_high:
            killed_count += 1
            continue
        live.append(r)

    bear_buckets: dict[int, float] = defaultdict(float)
    bull_buckets: dict[int, float] = defaultdict(float)
    bear_codes: dict[int, list] = defaultdict(list)
    bull_codes: dict[int, list] = defaultdict(list)

    for r in live:
        notional = float(r.outstanding_shares) * float(r.er_ratio)
        bucket = int(round(r.call_level / BUCKET_PTS) * BUCKET_PTS)
        if r.direction == "bear":
            bear_buckets[bucket] += notional
            bear_codes[bucket].append((r.code, r.issuer, r.outstanding_shares, r.call_level))
        elif r.direction == "bull":
            bull_buckets[bucket] += notional
            bull_codes[bucket].append((r.code, r.issuer, r.outstanding_shares, r.call_level))

    print(f"\n{'='*78}")
    print(f"HSI: {spot:.0f}  | low {today_low} ~ high {today_high}  | filtered out {killed_count} 已收回合约")
    print(f"{'='*78}")

    above = sorted([b for b in bear_buckets if b >= spot])[:10]
    print("\n>> 上方街货群 (做多目标):")
    print(f"{'桶':>6s}  {'距离':>7s}  {'notional 拉力':>22s}  {'合约数':>4s}  {'累计街货 (亿股)':>14s}")
    for b in above:
        n = bear_buckets[b]
        codes = bear_codes[b]
        total_shares = sum(c[2] for c in codes) / 1e8
        d = b - spot
        bar = "█" * min(40, int(n / 5e10))
        print(f"{b:>6d}  {d:>+5.0f}pt  {n:>18,.0f} HKD  {len(codes):>4d}  {total_shares:>10.2f} {bar}")

    below = sorted([b for b in bull_buckets if b < spot], reverse=True)[:10]
    print("\n<< 下方街货群 (做空目标):")
    print(f"{'桶':>6s}  {'距离':>7s}  {'notional 拉力':>22s}  {'合约数':>4s}  {'累计街货 (亿股)':>14s}")
    for b in below:
        n = bull_buckets[b]
        codes = bull_codes[b]
        total_shares = sum(c[2] for c in codes) / 1e8
        d = b - spot
        bar = "█" * min(40, int(n / 5e10))
        print(f"{b:>6d}  {d:>+5.0f}pt  {n:>18,.0f} HKD  {len(codes):>4d}  {total_shares:>10.2f} {bar}")

    print(f"\n{'='*78}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
