"""
临时探测脚本 — 拉富途 OpenAPI 的恒指牛熊证 (CBBC) 数据。

用途:
1. 验证账户有没有 BMP 权限阻挡 (输出 ret==RET_OK 或具体错误码)。
2. 看看 ``get_warrant`` 返回的 DataFrame 实际字段，确认与
   ``CbbcRecord`` 映射是否需要调整。
3. 统计 BULL / BEAR 数量与街货分布，肉眼校验数据合理性。

运行:
    backend/venv/Scripts/python.exe backend/probe_futu_warrants.py

无副作用：不写任何文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import FUTU_HOST, FUTU_PORT  # noqa: E402

# HSI 在富途的 stock_owner 代码。
HSI_STOCK_OWNER = "HK.800000"
PAGE_SIZE = 200  # API 单次最大值


def main() -> int:
    try:
        from futu import (  # noqa: WPS433
            OpenQuoteContext,
            RET_OK,
            SortField,
            WarrantRequest,
            WarrantStatus,
            WrtType,
        )
    except Exception as exc:
        print(f"[probe] 富途 SDK 未安装或加载失败: {exc!r}", file=sys.stderr)
        return 2

    ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
    try:
        all_rows: list = []
        for page in range(0, 30):  # 20 * 200 = 4000+，覆盖全市场
            req = WarrantRequest()
            req.begin = page * PAGE_SIZE
            req.num = PAGE_SIZE
            req.sort_field = SortField.STREET_VOL  # 按街货量降序，方便肉眼看
            req.ascend = False
            # 仅保留 BULL / BEAR (Callable Bull/Bear Contracts)。
            req.type_list = [WrtType.BULL, WrtType.BEAR]
            # 仅保留 NORMAL 状态的合约 — 排除已强制收回 / 已到期。
            req.status = WarrantStatus.NORMAL

            ret, ls = ctx.get_warrant(HSI_STOCK_OWNER, req)
            if ret != RET_OK:
                print(f"[probe] page={page} ret={ret} err={ls!r}", file=sys.stderr)
                return 3

            df, last_page, all_count = ls
            print(f"[probe] page={page} rows_in_page={len(df)} all_count={all_count} last_page={last_page}")
            if len(df) == 0:
                break
            all_rows.append(df)
            if last_page:
                break
    finally:
        ctx.close()

    if not all_rows:
        print("[probe] no warrant rows returned")
        return 0

    import pandas as pd
    df_all = pd.concat(all_rows, ignore_index=True)

    print()
    print("=" * 70)
    print(f"Total HSI CBBC contracts pulled: {len(df_all)}")
    print("=" * 70)

    # 1) 列名清单 — 与 CbbcRecord 字段映射对照。
    print("\nColumns:")
    for c in df_all.columns:
        print(f"  - {c}")

    # 2) 方向分布
    if "type" in df_all.columns:
        print("\nDirection distribution (raw `type` values):")
        print(df_all["type"].value_counts(dropna=False).to_string())

    # 3) 关键字段统计 (street_vol / recovery_price / conversion_ratio)
    for col in ("street_vol", "recovery_price", "conversion_ratio", "list_time", "maturity_time"):
        if col in df_all.columns:
            print(f"\n`{col}` head:")
            print(df_all[col].head(5).to_string())

    # 4) 给一个映射示意：取街货量最大的前 5 行渲染成 CbbcRecord 视图
    print("\nTop 5 by street_vol — sample as CbbcRecord shape:")
    pick_cols = [c for c in [
        "stock", "issuer", "type", "recovery_price", "street_vol",
        "conversion_ratio", "list_time", "maturity_time", "stock_owner",
    ] if c in df_all.columns]
    print(df_all[pick_cols].head(5).to_string())

    return 0


if __name__ == "__main__":
    sys.exit(main())
