"""
拉一次富途 HSI 牛熊证全量,落盘成 ``backend/data/cbbc/outstanding_YYYYMMDD.parquet``。

用途:
- 即时给 ``CbbcStorage`` 灌入今天的真实数据,让 PriceChart 上的
  CBBC overlay 立刻有数显示。
- 验证 ``FutuCbbcSource`` 全链路正常 (拉取 → 字段映射 → parquet 写入)。

运行 (手动 seed):
    backend/venv/Scripts/python.exe backend/seed_cbbc_from_futu.py

无副作用前提:
- 同一天的 parquet 已存在 → 抛 ``snapshot_immutable``;脚本退出非零。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cbbc_data_futu import FutuCbbcError, FutuCbbcSource  # noqa: E402
from cbbc_storage import CbbcStorage, SnapshotError  # noqa: E402
from config import FUTU_HOST, FUTU_PORT  # noqa: E402


def main() -> int:
    today_hk = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    print(f"[seed] target date (HK): {today_hk.isoformat()}")

    src = FutuCbbcSource(host=FUTU_HOST, port=FUTU_PORT)
    storage = CbbcStorage()

    # 1. 拉取
    try:
        snapshot = src.fetch_outstanding(today_hk)
    except FutuCbbcError as exc:
        print(f"[seed] fetch failed: code={exc.code} msg={exc!s}", file=sys.stderr)
        return 2

    print(
        f"[seed] fetched {len(snapshot.records)} records "
        f"(BULL={sum(1 for r in snapshot.records if r.direction == 'bull')} "
        f"BEAR={sum(1 for r in snapshot.records if r.direction == 'bear')})"
    )

    if not snapshot.records:
        print("[seed] no records returned; abort write", file=sys.stderr)
        return 3

    # 2. 写盘
    try:
        storage.write_snapshot(snapshot)
    except SnapshotError as exc:
        print(f"[seed] write failed: code={exc.code} msg={exc!s}", file=sys.stderr)
        return 4

    fname = f"outstanding_{today_hk.strftime('%Y%m%d')}.parquet"
    print(f"[seed] wrote {storage.base_dir / fname}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
