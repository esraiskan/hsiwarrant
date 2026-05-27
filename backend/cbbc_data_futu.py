"""
CBBC 数据源 — 富途 OpenAPI 适配 (cbbc-magnet-signal task 4.5 真实数据接入).

把富途 ``OpenQuoteContext.get_warrant`` 返回的 DataFrame 映射成
``CbbcRecord``，作为 HKEX HTTP 端点的替代数据源。本模块独立于
``cbbc_data.py`` 的白名单 / 限频客户端 — 富途连接由本地 OpenD 鉴权处理，
``RateLimitedClient`` 的 HKEX 域名白名单不适用于此路径。

主要类:
- ``FutuCbbcSource``: 同步拉取 HSI 全量 NORMAL 状态牛熊证;暴露
  ``fetch_outstanding(date) -> CbbcSnapshot`` 给 ``CbbcDataService`` 注入。

字段映射 (富途 → CbbcRecord):
- stock          → code
- issuer         → issuer
- type ∈ {BULL,BEAR} → direction (大写转小写)
- recovery_price → call_level
- street_vol     → outstanding_shares
- conversion_ratio → er_ratio
- list_time      → listing_date
- maturity_time  → maturity_date
- stock_owner == "HK.800000" → underlying = "HSI" (固定)
- snapshot_date  → 调用方传入

设计取舍:
- 富途接口默认按 ``street_vol`` 降序拉,本模块同样保留这个排序,便于人肉
  对比。``MagnetEngine`` 不依赖排序。
- ``status=NORMAL`` 过滤 — 排除已强制收回 / 已到期的合约 (同 HKEX 路径里
  ``read_snapshot`` 的生存偏差守卫思路一致)。
- 单次拉取耗时 ~5s,远小于 ``DailyFetcher`` 的 30s 超时,直接全量同步 OK。
- 不依赖 ``cbbc_data.RateLimitedClient`` — 富途自己有限频 (60 req / 30s),
  我们日内最多每分钟一次,不会触限。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date as _date
from typing import TYPE_CHECKING, Any, Callable, Optional, Sequence

from cbbc_storage import CbbcRecord, CbbcSnapshot


if TYPE_CHECKING:  # pragma: no cover
    from futu import OpenQuoteContext  # noqa: F401


logger = logging.getLogger("cbbc_data_futu")


# 富途 stock_owner 代码 — 恒指
HSI_STOCK_OWNER: str = "HK.800000"

# 单次 ``get_warrant`` 调用最多 200 条
_PAGE_SIZE: int = 200

# 全市场 HSI NORMAL 牛熊证大约 3000 个 — 17 页足够
_MAX_PAGES: int = 30


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class FutuCbbcError(Exception):
    """所有富途数据拉取失败的统一异常。

    ``code`` 用于结构化日志聚合,字符串值与设计文档约定的事件名对齐。
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


# --------------------------------------------------------------------------- #
# Field mapping
# --------------------------------------------------------------------------- #


def _coerce_float(value: Any) -> float | None:
    """把富途 DataFrame 单元格转成 ``float``;非有限或非数值返回 ``None``。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None  # 布尔不应被当作街货量
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    try:
        f = float(text.replace(",", ""))
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _coerce_int(value: Any) -> int | None:
    f = _coerce_float(value)
    if f is None:
        return None
    return int(f)


def _coerce_date(value: Any) -> _date | None:
    """``"2026-04-02"`` → ``date(2026, 4, 2)``;接受 ``pd.Timestamp``。"""
    if value is None:
        return None
    # pandas Timestamp / datetime
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime().date()
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "date") and not isinstance(value, str):
        try:
            return value.date()
        except Exception:  # noqa: BLE001
            pass
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    # "YYYY-MM-DD" 优先
    if " " in text:
        text = text.split(" ", 1)[0]
    if "T" in text:
        text = text.split("T", 1)[0]
    try:
        from datetime import datetime
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _normalize_direction(value: Any) -> str | None:
    """``"BULL"`` / ``"BEAR"`` (or 富途常量) → ``"bull"`` / ``"bear"``。"""
    if value is None:
        return None
    text = str(value).strip().upper()
    if text == "BULL":
        return "bull"
    if text == "BEAR":
        return "bear"
    return None


def _row_to_record(row: Any, *, snapshot_date: _date) -> CbbcRecord | None:
    """把富途返回 DataFrame 的一行映射成 ``CbbcRecord``;字段缺失或非法返回 ``None``。

    Args:
        row: ``df.iloc[i]`` (pandas Series) 或一个普通 ``dict``。
        snapshot_date: 调用方提供的 HK 交易日。

    Returns:
        合法时返回 ``CbbcRecord``,任一关键字段缺失 / 非法时返回 ``None`` 并写
        ``level=WARN, source=cbbc_data_futu, event=record_parse_failed`` 日志。
    """
    def _get(key: str) -> Any:
        if hasattr(row, "get"):
            return row.get(key)
        try:
            return row[key]
        except Exception:  # noqa: BLE001
            return None

    code = str(_get("stock") or "").strip()
    if not code:
        logger.warning(
            "record_parse_failed: missing stock",
            extra={
                "level": "WARN",
                "source": "cbbc_data_futu",
                "event": "record_parse_failed",
                "code": None,
                "reason": "missing_field:stock",
            },
        )
        return None

    issuer = str(_get("issuer") or "").strip()
    if not issuer:
        logger.warning(
            "record_parse_failed: missing issuer",
            extra={
                "level": "WARN", "source": "cbbc_data_futu",
                "event": "record_parse_failed",
                "code": code, "reason": "missing_field:issuer",
            },
        )
        return None

    direction = _normalize_direction(_get("type"))
    if direction is None:
        logger.warning(
            "record_parse_failed: invalid direction",
            extra={
                "level": "WARN", "source": "cbbc_data_futu",
                "event": "record_parse_failed",
                "code": code, "reason": "invalid_field:type",
            },
        )
        return None

    call_level = _coerce_float(_get("recovery_price"))
    if call_level is None or call_level <= 0:
        logger.warning(
            "record_parse_failed: invalid recovery_price",
            extra={
                "level": "WARN", "source": "cbbc_data_futu",
                "event": "record_parse_failed",
                "code": code, "reason": "invalid_field:recovery_price",
            },
        )
        return None

    outstanding = _coerce_float(_get("street_vol"))
    if outstanding is None or outstanding < 0:
        logger.warning(
            "record_parse_failed: invalid street_vol",
            extra={
                "level": "WARN", "source": "cbbc_data_futu",
                "event": "record_parse_failed",
                "code": code, "reason": "invalid_field:street_vol",
            },
        )
        return None

    er_ratio = _coerce_float(_get("conversion_ratio"))
    if er_ratio is None or er_ratio <= 0:
        logger.warning(
            "record_parse_failed: invalid conversion_ratio",
            extra={
                "level": "WARN", "source": "cbbc_data_futu",
                "event": "record_parse_failed",
                "code": code, "reason": "invalid_field:conversion_ratio",
            },
        )
        return None

    list_d = _coerce_date(_get("list_time"))
    if list_d is None:
        logger.warning(
            "record_parse_failed: invalid list_time",
            extra={
                "level": "WARN", "source": "cbbc_data_futu",
                "event": "record_parse_failed",
                "code": code, "reason": "invalid_field:list_time",
            },
        )
        return None

    maturity_d = _coerce_date(_get("maturity_time"))
    if maturity_d is None:
        logger.warning(
            "record_parse_failed: invalid maturity_time",
            extra={
                "level": "WARN", "source": "cbbc_data_futu",
                "event": "record_parse_failed",
                "code": code, "reason": "invalid_field:maturity_time",
            },
        )
        return None

    if list_d > maturity_d:
        logger.warning(
            "record_parse_failed: invalid_date_range",
            extra={
                "level": "WARN", "source": "cbbc_data_futu",
                "event": "record_parse_failed",
                "code": code, "reason": "invalid_date_range",
            },
        )
        return None

    return CbbcRecord(
        issuer=issuer,
        code=code,
        call_level=call_level,
        outstanding_shares=outstanding,
        er_ratio=er_ratio,
        direction=direction,  # type: ignore[arg-type]
        listing_date=list_d,
        maturity_date=maturity_d,
        underlying="HSI",
        snapshot_date=snapshot_date,
    )


# --------------------------------------------------------------------------- #
# Source class
# --------------------------------------------------------------------------- #


@dataclass
class FutuCbbcSource:
    """``DailyFetcher`` / ``IntradayPoller`` 的富途版替代。

    构造时不强求富途 SDK 已加载;只在 ``fetch_outstanding`` 真正调用时才
    ``import futu``,这样模块自身在没有 OpenD 的开发机上仍能 import。

    Attributes:
        host / port: OpenD 监听地址,默认沿用 ``config.FUTU_HOST/PORT``。
        stock_owner: 标的代码,默认 ``HK.800000`` (HSI)。
        page_size: 单次请求条数,默认 200 (API 上限)。
    """

    host: str = "127.0.0.1"
    port: int = 11111
    stock_owner: str = HSI_STOCK_OWNER
    page_size: int = _PAGE_SIZE

    def fetch_outstanding(self, snapshot_date: _date) -> CbbcSnapshot:
        """同步拉取所有 NORMAL 状态的 HSI 牛熊证 → ``CbbcSnapshot``。

        失败时抛 ``FutuCbbcError``;调用方应在 try/except 中包裹,失败后
        回退到 ``CbbcStorage.latest_before`` 上一交易日快照,与 ``DailyFetcher``
        的 R1.7 行为一致。

        Returns:
            CbbcSnapshot: 包含所有合法 ``CbbcRecord`` 的不可变快照。
        """
        try:
            from futu import (  # noqa: WPS433 - lazy import keeps dev-box import OK
                OpenQuoteContext,
                RET_OK,
                SortField,
                WarrantRequest,
                WarrantStatus,
                WrtType,
            )
        except Exception as exc:  # noqa: BLE001
            raise FutuCbbcError(
                "futu_sdk_unavailable",
                f"futu SDK import failed: {exc!r}",
            ) from exc

        ctx = OpenQuoteContext(host=self.host, port=self.port)
        try:
            records: list[CbbcRecord] = []
            page = 0
            while page < _MAX_PAGES:
                req = WarrantRequest()
                req.begin = page * self.page_size
                req.num = self.page_size
                req.sort_field = SortField.STREET_VOL
                req.ascend = False
                req.type_list = [WrtType.BULL, WrtType.BEAR]
                req.status = WarrantStatus.NORMAL

                ret, ls = ctx.get_warrant(self.stock_owner, req)
                if ret != RET_OK:
                    raise FutuCbbcError(
                        "futu_get_warrant_failed",
                        f"page={page} ret={ret} err={ls!r}",
                    )

                df, last_page, _all_count = ls
                if len(df) == 0:
                    break

                # ``df.iterrows`` 在 pandas 里是常用模式;每行通过 mapping 函数
                # 转 CbbcRecord,失败的单行已经在 ``_row_to_record`` 里写日志。
                for _, row in df.iterrows():
                    rec = _row_to_record(row, snapshot_date=snapshot_date)
                    if rec is not None:
                        records.append(rec)

                if last_page:
                    break
                page += 1
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass

        return CbbcSnapshot(
            snapshot_date=snapshot_date,
            records=tuple(records),
        )


__all__ = [
    "FutuCbbcError",
    "FutuCbbcSource",
    "HSI_STOCK_OWNER",
]
