"""检查 25450 附近的牛证街货是不是已经在今天的低点 25431 被全数收回了。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cbbc_storage import CbbcStorage  # noqa: E402

today = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
snap = CbbcStorage().read_snapshot(today)

bull_above_low = [r for r in snap.records if r.direction == "bull" and 25420 <= r.call_level <= 25470]
print(f"Bull contracts with call_level ∈ [25420, 25470] (snapshot from {today}):")
print(f"{'code':10s} {'issuer':>4s} {'call_level':>10s} {'street_vol':>15s} {'list_date':>12s}")
for r in sorted(bull_above_low, key=lambda r: r.call_level):
    print(f"{r.code:10s} {r.issuer:>4s} {r.call_level:>10.0f} {r.outstanding_shares:>15,.0f} {str(r.listing_date):>12s}")
print(f"\nLow today: 25431.17 → 任何 call_level >= 25431 的牛证今天都应该已被强制收回。")
print(f"但快照里街货量显示 0 的是这种状态;街货量 > 0 的说明快照是今天早上抓的、之后才被收回。")
