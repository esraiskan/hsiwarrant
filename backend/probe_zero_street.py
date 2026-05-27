"""Count snapshots with street_vol == 0 to confirm whether they are skewing nearest_distance."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cbbc_storage import CbbcStorage  # noqa: E402


def main() -> int:
    today_hk = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    storage = CbbcStorage()
    snap = storage.read_snapshot(today_hk)

    total = len(snap.records)
    zero = [r for r in snap.records if r.outstanding_shares <= 0]
    nonzero = [r for r in snap.records if r.outstanding_shares > 0]

    print(f"date: {today_hk}")
    print(f"total: {total}")
    print(f"  street_vol > 0: {len(nonzero)}")
    print(f"  street_vol == 0: {len(zero)}")

    # Show 10 zero-street records and the closest 5 to spot.
    if zero:
        print("\nFirst 10 zero-street records (sample):")
        for r in zero[:10]:
            print(f"  {r.code:10s} {r.issuer:>4s}  {r.direction:4s}  call_level={r.call_level:.0f}  "
                  f"er_ratio={r.er_ratio:.0f}  list={r.listing_date}")

    # Find the smallest distance to spot among zero-street records (most
    # likely the cause of the suspicious nearest_distance).
    if zero:
        # Read latest spot from /api/state to compare.
        try:
            import json, urllib.request
            with urllib.request.urlopen('http://127.0.0.1:6000/api/state', timeout=5) as resp:
                state = json.loads(resp.read())
            spot = state['current_price']
            print(f"\nHSI spot from /api/state: {spot}")
            sorted_zero = sorted(zero, key=lambda r: abs(r.call_level - spot))[:5]
            print("Top 5 nearest zero-street records to current spot:")
            for r in sorted_zero:
                d = abs(r.call_level - spot)
                print(f"  {r.code:10s} {r.issuer:>4s} {r.direction:4s} call={r.call_level:.0f} "
                      f"distance={d:.1f}pt")
        except Exception as e:
            print(f"(couldn't fetch live spot: {e})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
