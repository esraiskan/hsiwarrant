"""
分析今天 CBBC 街货分布,找出当前价上方/下方的密集区间,作为目标位 / 支撑位。

逻辑:
- 仅用 NORMAL + street_vol > 0 的合约 (与 magnet 引擎一致)
- 按 25pt 桶聚合 dollar-weighted notional (street_vol * er_ratio)
- 上方 (bear-direction) 桶按距离升序 = 目标位顺序
- 下方 (bull-direction) 桶按距离升序 = 支撑位顺序
"""
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

BUCKET_PTS = 25  # 25pt bucket — readable, matches typical HSI strike spacing


def main() -> int:
    today_hk = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    storage = CbbcStorage()
    snap = storage.read_snapshot(today_hk)
    with urllib.request.urlopen("http://127.0.0.1:6000/api/state", timeout=5) as resp:
        spot = float(json.loads(resp.read())["current_price"])

    # Filter to live contracts.
    live = [r for r in snap.records if r.outstanding_shares > 0]

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
    print(f"HSI: {spot:.0f}  |  date: {today_hk}  |  bucket: {BUCKET_PTS}pt")
    print(f"{'='*78}")

    # --- 上方目标 (bear-side resistance / fuel) ---
    above = sorted([b for b in bear_buckets if b >= spot])[:8]
    print("\n>> 上方街货群 (做多目标 / 被吸吃熊证):")
    print(f"{'桶':>8s}  {'距离':>6s}  {'notional 拉力':>20s}  {'合约数':>6s}  {'累计街货 (亿股)':>14s}")
    cumulative = 0.0
    for b in above:
        n = bear_buckets[b]
        codes = bear_codes[b]
        total_shares = sum(c[2] for c in codes) / 1e8
        cumulative += total_shares
        d = b - spot
        bar = "█" * min(40, int(n / 1e9))
        print(f"  {b:>6d}  {d:>+5.0f}pt  {n:>16,.0f} HKD  {len(codes):>6d}  {total_shares:>10.2f} {bar}")

    # --- 下方支撑 (bull-side support) ---
    below = sorted([b for b in bull_buckets if b < spot], reverse=True)[:8]
    print("\n<< 下方街货群 (做空目标 / 被吸吃牛证):")
    print(f"{'桶':>8s}  {'距离':>6s}  {'notional 拉力':>20s}  {'合约数':>6s}  {'累计街货 (亿股)':>14s}")
    for b in below:
        n = bull_buckets[b]
        codes = bull_codes[b]
        total_shares = sum(c[2] for c in codes) / 1e8
        d = b - spot
        bar = "█" * min(40, int(n / 1e9))
        print(f"  {b:>6d}  {d:>+5.0f}pt  {n:>16,.0f} HKD  {len(codes):>6d}  {total_shares:>10.2f} {bar}")

    # --- 推荐目标位:从上方按 notional 加权找前 2 个高峰 ---
    print(f"\n{'='*78}")
    print("📌 目标位建议 (按 notional 拉力排序,选拉力 > 阈值的前 2 个上方区间):")
    threshold = max(1e10, max(bear_buckets.values(), default=0) * 0.3) if bear_buckets else 0
    candidates = [(b, bear_buckets[b]) for b in above if bear_buckets[b] >= threshold]
    candidates.sort(key=lambda x: x[0])  # by distance
    if len(candidates) >= 1:
        b1, n1 = candidates[0]
        print(f"  🎯 第一目标: {b1} (距 +{b1-spot:.0f}pt, 拉力 {n1/1e10:.2f}×10¹⁰ HKD)")
    if len(candidates) >= 2:
        b2, n2 = candidates[1]
        print(f"  🎯 第二目标: {b2} (距 +{b2-spot:.0f}pt, 拉力 {n2/1e10:.2f}×10¹⁰ HKD)")
    if len(candidates) == 0:
        print("  (无显著上方密集带)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
