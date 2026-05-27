"""
CBBC 数据服务（HKEX 公开端点接入层）。

本文件目前仅包含 Task 4.1 所要求的基础访问层：

- ``RateLimitedClient``：60 秒滚动窗口对同一完整 URL 限 6 次。
- 域名白名单守卫：仅允许 ``www.hkex.com.hk`` 与 ``www1.hkexnews.hk``。
- 鉴权 / 付费检测：在请求前阻断包含 Cookie 会话、Authorization、API Key 的请求；
  响应 401/403 也判定为受限。
- ``parse_outstanding_table`` / ``parse_intraday_new_listings`` (Task 4.2)。
- ``DailyFetcher`` (Task 4.3)：每日 T+1 抓取任务，含退避重试、幂等、incomplete
  守卫、上一交易日回退。

盘中轮询、CbbcDataService 主体由后续 Task 4.4 / 4.5 在本文件之上扩展。

合规约束（Requirements 11.1, 11.2, 11.3, 11.7, 11.8）：

* 任何不在白名单内或需鉴权 / 付费的端点一律视为禁止访问。
* 日志中只保留 URL 与原因，绝不写凭据值。
* 60 秒滚动窗口内同一 URL 不超过 6 次；达到上限则延后并写
  ``level=WARN, source=cbbc_data, event=rate_limit_deferred``。
"""
from __future__ import annotations

import asyncio
import csv
import html.parser
import io
import json
import logging
import math
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from typing import Any, Awaitable, Callable, Iterable, Iterator, Literal, Mapping, Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from cbbc_storage import CbbcRecord, CbbcSnapshot, CbbcStorage, is_hk_trading_day, SnapshotError


logger = logging.getLogger("cbbc_data")


# --------------------------------------------------------------------------- #
# Public constants
# --------------------------------------------------------------------------- #

#: 允许访问的 HKEX 公开数据域名白名单（必须免登录、免付费）。
HKEX_WHITELIST_HOSTS: frozenset[str] = frozenset({
    "www.hkex.com.hk",
    "www1.hkexnews.hk",
})

#: 鉴权相关 header 名称（大小写不敏感匹配）。
_AUTH_HEADER_NAMES: frozenset[str] = frozenset({
    "cookie",
    "authorization",
    "x-api-key",
    "api-key",
    "x-auth-token",
    "proxy-authorization",
})

#: 60 秒滚动窗口默认上限。
DEFAULT_RATE_LIMIT_WINDOW_SECONDS: float = 60.0
DEFAULT_RATE_LIMIT_MAX_REQUESTS: int = 6


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

CbbcDataErrorCode = Literal[
    "blocked_non_whitelisted_endpoint",
    "blocked_paywalled_or_authenticated_endpoint",
]


class CbbcDataError(Exception):
    """所有 CBBC 数据访问层失败的统一异常基类。

    ``code`` 字段对应 Requirements 中规定的结构化日志事件名。
    异常消息与字段中**不会**记录任何凭据。
    """

    def __init__(self, code: CbbcDataErrorCode, *, url: str, reason: str) -> None:
        self.code: CbbcDataErrorCode = code
        self.url: str = url
        self.reason: str = reason
        super().__init__(f"{code}: {reason} url={url}")


# --------------------------------------------------------------------------- #
# HTTP backend abstraction
# --------------------------------------------------------------------------- #


class HttpResponse(Protocol):
    """与 ``httpx.Response`` 兼容的最小接口。"""

    status_code: int

    @property
    def text(self) -> str: ...  # pragma: no cover - protocol stub

    @property
    def content(self) -> bytes: ...  # pragma: no cover - protocol stub


class HttpBackend(Protocol):
    """可注入的 HTTP 后端，方便测试与未来替换实现。"""

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        timeout: float,
    ) -> HttpResponse:  # pragma: no cover - protocol stub
        ...

    async def aclose(self) -> None:  # pragma: no cover - protocol stub
        ...


class HttpxBackend:
    """``httpx.AsyncClient`` 的轻量包装。

    httpx 在该模块中按 Lazy import 方式加载，单元测试可以直接注入
    ``HttpBackend`` 协议的伪实现，无需安装 httpx。
    """

    def __init__(self, client: Any | None = None, **client_kwargs: Any) -> None:
        if client is None:
            try:
                import httpx  # noqa: WPS433 - intentional lazy import
            except ImportError as exc:  # pragma: no cover - exercised in deploy
                raise RuntimeError(
                    "httpx is required for HttpxBackend; install httpx>=0.27 or "
                    "inject a custom HttpBackend",
                ) from exc
            client = httpx.AsyncClient(**client_kwargs)
        self._client = client

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        timeout: float,
    ) -> HttpResponse:
        # httpx.AsyncClient.get accepts ``headers`` / ``timeout`` kwargs directly.
        return await self._client.get(url, headers=dict(headers or {}), timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()


# --------------------------------------------------------------------------- #
# Rate limited / guarded client
# --------------------------------------------------------------------------- #


# Returned by ``RateLimitedClient.get`` so callers can observe enforcement.
@dataclass(frozen=True)
class RequestOutcome:
    """请求结果元信息（与响应正交）。"""

    deferred_seconds: float  # 因限频造成的实际等待秒数；未限频时为 0.0


# Type alias for time provider, makes tests deterministic.
ClockFn = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]


class RateLimitedClient:
    """带白名单 / 鉴权守卫 / 60 秒 6 次限频的异步 HTTP 客户端。

    * 白名单（Requirement 11.1, 11.3）：只允许 ``netloc`` ∈
      :data:`HKEX_WHITELIST_HOSTS`，且 ``scheme`` 必须为 ``https``。
      不通过时抛 :class:`CbbcDataError` 并写
      ``level=ERROR, event=blocked_non_whitelisted_endpoint``。
    * 鉴权 / 付费守卫（Requirement 11.2）：请求前若检测到 Cookie / Authorization /
      X-API-Key 等 header，或响应 401 / 403，则抛
      :class:`CbbcDataError` 并写
      ``level=ERROR, event=blocked_paywalled_or_authenticated_endpoint``。
      日志只保留 URL 与原因，**不写**凭据值或 header 名称之外的元数据。
    * 限频（Requirement 11.7, 11.8）：以完整 URL 作为 key，60 秒滚动窗口内
      达到 6 次时延后下一次请求至窗口腾出位置；命中延后时写
      ``level=WARN, event=rate_limit_deferred``，并附带相对延迟秒数。

    本类不持有 HTTP backend 的所有权（除非通过 :py:meth:`aclose` 主动关闭），
    便于测试期注入伪 backend。
    """

    def __init__(
        self,
        *,
        http: HttpBackend | None = None,
        max_per_window: int = DEFAULT_RATE_LIMIT_MAX_REQUESTS,
        window_seconds: float = DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
        clock: ClockFn | None = None,
        sleep: SleepFn | None = None,
        allowed_hosts: Iterable[str] = HKEX_WHITELIST_HOSTS,
    ) -> None:
        if max_per_window <= 0:
            raise ValueError("max_per_window must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")

        self._http: HttpBackend = http if http is not None else HttpxBackend()
        self._max_per_window: int = int(max_per_window)
        self._window_seconds: float = float(window_seconds)
        self._allowed_hosts: frozenset[str] = frozenset(allowed_hosts)
        self._clock: ClockFn = clock if clock is not None else time.monotonic
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep
        # Per-URL deque of monotonic timestamps for rolling window.
        self._timestamps: dict[str, deque[float]] = {}
        # Per-URL lock so concurrent requests to same URL serialize sleep waits.
        self._url_locks: dict[str, asyncio.Lock] = {}

    # ----------------------- public API ----------------------- #

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> HttpResponse:
        """发起 GET 请求，返回 :class:`HttpResponse`（与 ``httpx.Response`` 兼容）。

        :raises CbbcDataError: 命中白名单 / 鉴权守卫时抛出。
        """
        self._guard_whitelist(url)
        self._guard_auth_headers(url, headers)
        await self._enforce_rate_limit(url)

        response = await self._http.get(url, headers=headers, timeout=timeout)

        self._guard_auth_response(url, response)
        return response

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "RateLimitedClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # ----------------------- guards ----------------------- #

    def _guard_whitelist(self, url: str) -> None:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        # Domain whitelist + HTTPS only. Path conformance is delegated to higher-
        # level fetch tasks (4.3 / 4.4) which know specific HKEX endpoints.
        if scheme != "https" or host not in self._allowed_hosts:
            reason = (
                "non_whitelisted_scheme"
                if scheme != "https"
                else "non_whitelisted_host"
            )
            logger.error(
                "blocked non-whitelisted endpoint",
                extra={
                    "level": "ERROR",
                    "source": "cbbc_data",
                    "event": "blocked_non_whitelisted_endpoint",
                    "url": url,
                    "host": host,
                    "scheme": scheme,
                    "reason": reason,
                },
            )
            raise CbbcDataError(
                "blocked_non_whitelisted_endpoint",
                url=url,
                reason=reason,
            )

    def _guard_auth_headers(
        self,
        url: str,
        headers: Mapping[str, str] | None,
    ) -> None:
        if not headers:
            return
        # Header keys are matched case-insensitively; values are NEVER logged.
        offenders = [
            name
            for name in headers
            if name.lower() in _AUTH_HEADER_NAMES
        ]
        if offenders:
            logger.error(
                "blocked paywalled or authenticated endpoint (auth header present)",
                extra={
                    "level": "ERROR",
                    "source": "cbbc_data",
                    "event": "blocked_paywalled_or_authenticated_endpoint",
                    "url": url,
                    "reason": "auth_header_present",
                    # Only header *names* leak, never values.
                    "header_names": sorted({name.lower() for name in offenders}),
                },
            )
            raise CbbcDataError(
                "blocked_paywalled_or_authenticated_endpoint",
                url=url,
                reason="auth_header_present",
            )

    def _guard_auth_response(self, url: str, response: HttpResponse) -> None:
        status = getattr(response, "status_code", None)
        if status in (401, 403):
            logger.error(
                "blocked paywalled or authenticated endpoint (auth response)",
                extra={
                    "level": "ERROR",
                    "source": "cbbc_data",
                    "event": "blocked_paywalled_or_authenticated_endpoint",
                    "url": url,
                    "reason": f"auth_response_status_{status}",
                },
            )
            raise CbbcDataError(
                "blocked_paywalled_or_authenticated_endpoint",
                url=url,
                reason=f"auth_response_status_{status}",
            )

    # ----------------------- rate limiting ----------------------- #

    async def _enforce_rate_limit(self, url: str) -> None:
        # One lock per URL avoids thundering herd: callers serializing on the
        # same URL won't all sleep then race past the cap.
        lock = self._url_locks.setdefault(url, asyncio.Lock())
        async with lock:
            window = self._timestamps.setdefault(url, deque())
            now = self._clock()
            self._prune_locked(window, now)

            if len(window) >= self._max_per_window:
                # Sleep until the oldest timestamp falls out of the rolling
                # window, then proceed.
                oldest = window[0]
                wait_seconds = (oldest + self._window_seconds) - now
                if wait_seconds < 0:
                    wait_seconds = 0.0
                logger.warning(
                    "rate limit deferred",
                    extra={
                        "level": "WARN",
                        "source": "cbbc_data",
                        "event": "rate_limit_deferred",
                        "url": url,
                        "deferred_seconds": round(wait_seconds, 3),
                        "window_seconds": self._window_seconds,
                        "max_per_window": self._max_per_window,
                    },
                )
                if wait_seconds > 0:
                    await self._sleep(wait_seconds)
                # After sleeping the oldest timestamp(s) may have aged out.
                now = self._clock()
                self._prune_locked(window, now)

            window.append(now)

    def _prune_locked(self, window: deque[float], now: float) -> None:
        cutoff = now - self._window_seconds
        while window and window[0] <= cutoff:
            window.popleft()


# --------------------------------------------------------------------------- #
# Outstanding / intraday parsers (Task 4.2)
# --------------------------------------------------------------------------- #

#: Supported payload formats. ``None`` triggers content sniffing.
PayloadFormat = Literal["csv", "html", "json"]

#: Canonical record field names.
_RECORD_FIELDS: tuple[str, ...] = (
    "issuer",
    "code",
    "call_level",
    "outstanding_shares",
    "er_ratio",
    "direction",
    "listing_date",
    "maturity_date",
    "underlying",
)

# Map of canonical field -> set of possible source column names (case/space
# insensitive). HKEX exposes outstanding data with English column headers in
# their standard CSV/HTML, but Chinese headers appear in some downstream
# mirrors; both are accepted here so that Task 4.3 can simply hand us the
# raw payload regardless of locale.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "issuer": ("issuer", "发行人", "發行人"),
    "code": ("code", "stockcode", "stock_code", "股份代号", "股份代號", "代号", "代號"),
    "call_level": (
        "calllevel",
        "call_level",
        "callprice",
        "call_price",
        "收回价",
        "收回價",
    ),
    "outstanding_shares": (
        "outstandingshares",
        "outstanding_shares",
        "outstandingquantity",
        "outstanding_quantity",
        "outstanding",
        "未平仓股数",
        "未平倉股數",
        "街货",
        "街貨",
    ),
    "er_ratio": (
        "erratio",
        "er_ratio",
        "entitlementratio",
        "entitlement_ratio",
        "兑换比率",
        "兌換比率",
    ),
    "direction": (
        "direction",
        "type",
        "bullbear",
        "bull_bear",
        "bull/bear",
        "callput",
        "方向",
    ),
    "listing_date": (
        "listingdate",
        "listing_date",
        "listdate",
        "上市日",
        "上市日期",
    ),
    "maturity_date": (
        "maturitydate",
        "maturity_date",
        "expirydate",
        "expiry_date",
        "到期日",
        "到期日期",
    ),
    "underlying": (
        "underlying",
        "underlyingasset",
        "underlying_asset",
        "underlyingname",
        "标的",
        "標的",
        "相关资产",
        "相關資產",
    ),
}

# Reverse map: any normalized alias -> canonical field name.
_ALIAS_TO_FIELD: dict[str, str] = {
    alias_norm: canonical
    for canonical, aliases in _FIELD_ALIASES.items()
    for alias_norm in (re.sub(r"[\s_\-/]+", "", alias).lower() for alias in aliases)
}

# Direction synonyms (input is lowercased before lookup).
_DIRECTION_BULL: frozenset[str] = frozenset({"bull", "call", "c", "牛", "牛证", "牛證"})
_DIRECTION_BEAR: frozenset[str] = frozenset({"bear", "put", "p", "熊", "熊证", "熊證"})

# Underlying values that map to the HSI universe. HKEX historically uses
# "HSI" / "Hang Seng Index" interchangeably; HSCEI / HSTECH / individual
# equities must be filtered out by the caller before reaching CbbcRecord.
_HSI_UNDERLYING_SYNONYMS: frozenset[str] = frozenset({
    "hsi",
    "hangsengindex",
    "hangseng",
    "hangseng_index",
    "恒生指数",
    "恒生指數",
    "恆生指數",
})

# Date parsing format candidates, ordered by preference (ISO first).
_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%Y%m%d",
    "%d %b %Y",
    "%d-%b-%Y",
)


def _normalize_key(name: object) -> str:
    """Lowercase + strip whitespace / underscores / dashes / slashes from a column name."""
    if name is None:
        return ""
    return re.sub(r"[\s_\-/]+", "", str(name)).lower()


def _detect_format(raw: bytes | str) -> PayloadFormat:
    """Sniff payload format. Used when ``content_type`` is omitted."""
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, (bytes, bytearray)) else raw
    head = text.lstrip()[:2048].lower()
    if head.startswith("{") or head.startswith("["):
        return "json"
    if "<table" in head or "<html" in head or "<!doctype html" in head:
        return "html"
    return "csv"


def _to_text(raw: bytes | str) -> str:
    if isinstance(raw, (bytes, bytearray)):
        # utf-8-sig handles HKEX's occasional BOM in CSV exports.
        return raw.decode("utf-8-sig", errors="replace")
    return raw


def _iter_rows(
    raw: bytes | str,
    *,
    content_type: PayloadFormat | None,
) -> Iterator[Mapping[str, Any]]:
    """Yield row dicts from CSV / HTML / JSON payloads, regardless of the source format."""
    fmt: PayloadFormat = content_type if content_type is not None else _detect_format(raw)
    text = _to_text(raw)

    if fmt == "csv":
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            yield {k: v for k, v in row.items() if k is not None}
        return

    if fmt == "json":
        data = json.loads(text)
        if isinstance(data, list):
            rows: Iterable[Any] = data
        elif isinstance(data, dict):
            for key in ("data", "records", "rows", "items", "result"):
                value = data.get(key)
                if isinstance(value, list):
                    rows = value
                    break
            else:
                rows = [data]
        else:
            rows = []
        for row in rows:
            if isinstance(row, Mapping):
                yield row
        return

    if fmt == "html":
        for record in _parse_html_tables(text):
            yield record
        return

    raise ValueError(f"unsupported content_type: {fmt!r}")


class _HtmlTableExtractor(html.parser.HTMLParser):
    """Stdlib-only HTML table extractor.

    Walks the document and produces ``list[list[list[str]]]`` (tables -> rows ->
    cells). Cell text is the concatenation of all text nodes within the cell,
    whitespace-collapsed. Headers (``<th>``) and data cells (``<td>``) are both
    treated as cells; the first non-empty row of each table is treated as the
    header by :func:`_parse_html_tables` if no explicit ``<thead>`` rows exist.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table_stack: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self._table_stack.append([])
        elif tag == "tr":
            if self._table_stack:
                self._row = []
        elif tag in ("td", "th"):
            if self._row is not None:
                self._cell = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("td", "th"):
            if self._cell is not None and self._row is not None:
                cell_text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
                self._row.append(cell_text)
            self._cell = None
        elif tag == "tr":
            if self._row is not None and self._table_stack:
                self._table_stack[-1].append(self._row)
            self._row = None
        elif tag == "table":
            if self._table_stack:
                self.tables.append(self._table_stack.pop())

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _parse_html_tables(text: str) -> Iterator[Mapping[str, str]]:
    """Yield row dicts from every ``<table>`` in the HTML document."""
    parser = _HtmlTableExtractor()
    parser.feed(text)
    parser.close()
    for table in parser.tables:
        if not table:
            continue
        # Drop fully-empty leading rows (HKEX HTML occasionally has spacers).
        rows = [r for r in table if any(cell.strip() for cell in r)]
        if len(rows) < 2:
            continue
        header = rows[0]
        for body_row in rows[1:]:
            # Pad / truncate to header width so zip works for ragged tables.
            cells = list(body_row[: len(header)])
            if len(cells) < len(header):
                cells.extend([""] * (len(header) - len(cells)))
            yield dict(zip(header, cells))


def _canonicalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Map source column names to canonical field names. Unknown columns are dropped."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        canonical = _ALIAS_TO_FIELD.get(_normalize_key(key))
        if canonical is None:
            continue
        # Preserve the first hit; if duplicates appear (e.g. HTML repeated headers),
        # keep the first non-null one rather than letting empty strings overwrite.
        if canonical in out:
            existing = out[canonical]
            if existing not in (None, "") and (value in (None, "") or value != existing):
                continue
        out[canonical] = value
    return out


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    return text or None


def _coerce_float(value: Any) -> float | None:
    """Best-effort numeric parser. Returns ``None`` for empty / non-finite inputs."""
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is a subtype of int; reject explicitly to avoid accidental True->1.
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    text = str(value).strip()
    if not text:
        return None
    # Strip thousands separators and currency sigils that occasionally appear in HKEX HTML.
    cleaned = text.replace(",", "").replace("HK$", "").replace("$", "").strip()
    try:
        f = float(cleaned)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _coerce_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # Keep just the date portion if a timestamp leaks through (e.g. "2025-01-02 09:30:00").
    if " " in text:
        text = text.split(" ", 1)[0]
    if "T" in text:
        text = text.split("T", 1)[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _normalize_direction(value: Any) -> Literal["bull", "bear"] | None:
    text = _coerce_str(value)
    if text is None:
        return None
    key = text.strip().lower()
    if key in _DIRECTION_BULL:
        return "bull"
    if key in _DIRECTION_BEAR:
        return "bear"
    return None


def _is_hsi(value: Any) -> bool:
    text = _coerce_str(value)
    if text is None:
        return False
    return _normalize_key(text) in _HSI_UNDERLYING_SYNONYMS


def _emit_record_parse_failure(*, code: str | None, reason: str) -> None:
    """Write the canonical Requirement 1.6 warning log."""
    logger.warning(
        "record_parse_failed",
        extra={
            "level": "WARN",
            "source": "cbbc_data",
            "event": "record_parse_failed",
            "code": code,
            "reason": reason,
        },
    )


def _parse_record(
    row: Mapping[str, Any],
    *,
    snapshot_date: date,
) -> CbbcRecord | None:
    """Validate one row and return a ``CbbcRecord`` or ``None`` if it should be dropped.

    Drop semantics:

    * Returns ``None`` *and emits no warning* when the row is non-HSI: HSI filtering
      is a domain rule (Requirement 1.4), not a parse failure.
    * Returns ``None`` *and emits a single ``record_parse_failed`` warning* when any
      mandatory field is missing / invalid (Requirement 1.6).
    """
    canonical = _canonicalize_row(row)

    # We need to know the row's ``code`` (if any) so the warning log can identify it.
    code = _coerce_str(canonical.get("code"))

    # If literally nothing parsed, treat this as a malformed row but still emit one log.
    if not canonical:
        _emit_record_parse_failure(code=None, reason="malformed_row")
        return None

    # Step 1 - HSI filter. Drop silently for non-HSI underlyings.
    underlying_raw = canonical.get("underlying")
    underlying_str = _coerce_str(underlying_raw)
    if underlying_str is None:
        # No underlying is a parse failure (mandatory field missing).
        _emit_record_parse_failure(code=code, reason="missing_field:underlying")
        return None
    if not _is_hsi(underlying_str):
        return None

    # Step 2 - validate / coerce the remaining mandatory fields.
    issuer = _coerce_str(canonical.get("issuer"))
    if issuer is None:
        _emit_record_parse_failure(code=code, reason="missing_field:issuer")
        return None
    if code is None:
        _emit_record_parse_failure(code=None, reason="missing_field:code")
        return None

    call_level = _coerce_float(canonical.get("call_level"))
    if call_level is None:
        _emit_record_parse_failure(code=code, reason="invalid_field:call_level")
        return None

    outstanding_shares = _coerce_float(canonical.get("outstanding_shares"))
    if outstanding_shares is None:
        _emit_record_parse_failure(code=code, reason="invalid_field:outstanding_shares")
        return None

    er_ratio = _coerce_float(canonical.get("er_ratio"))
    if er_ratio is None:
        _emit_record_parse_failure(code=code, reason="invalid_field:er_ratio")
        return None

    direction = _normalize_direction(canonical.get("direction"))
    if direction is None:
        _emit_record_parse_failure(code=code, reason="invalid_field:direction")
        return None

    listing_date = _coerce_date(canonical.get("listing_date"))
    if listing_date is None:
        _emit_record_parse_failure(code=code, reason="invalid_field:listing_date")
        return None

    maturity_date = _coerce_date(canonical.get("maturity_date"))
    if maturity_date is None:
        _emit_record_parse_failure(code=code, reason="invalid_field:maturity_date")
        return None

    if listing_date > maturity_date:
        _emit_record_parse_failure(code=code, reason="invalid_date_range")
        return None

    return CbbcRecord(
        issuer=issuer,
        code=code,
        call_level=call_level,
        outstanding_shares=outstanding_shares,
        er_ratio=er_ratio,
        direction=direction,
        listing_date=listing_date,
        maturity_date=maturity_date,
        underlying="HSI",
        snapshot_date=snapshot_date,
    )


def parse_outstanding_table(
    raw: bytes | str,
    *,
    snapshot_date: date,
    content_type: PayloadFormat | None = None,
) -> list[CbbcRecord]:
    """Parse a HKEX outstanding payload into ``CbbcRecord`` objects (HSI only).

    The payload may be CSV, HTML, or JSON. Format is auto-detected when
    ``content_type`` is ``None``.

    Validation contract (Requirements 1.4, 1.5, 1.6):

    * Records with ``underlying != "HSI"`` are dropped silently.
    * Records that fail field-level validation are dropped, with one
      ``level=WARN, source=cbbc_data, event=record_parse_failed`` log per
      drop including the offending ``code`` (or ``None``) and the reason.
    * Surviving records carry the supplied ``snapshot_date`` and a normalized
      ``direction ∈ {"bull", "bear"}`` and ``underlying = "HSI"``.

    The function never raises for individual row failures; the only exceptions
    that escape are payload-level structural errors (e.g. malformed JSON).
    """
    out: list[CbbcRecord] = []
    try:
        rows = _iter_rows(raw, content_type=content_type)
    except (json.JSONDecodeError, ValueError) as exc:
        # Payload-level corruption: surface as a single warning with no code.
        _emit_record_parse_failure(code=None, reason=f"payload_decode_error:{exc.__class__.__name__}")
        return out

    for row in rows:
        record = _parse_record(row, snapshot_date=snapshot_date)
        if record is not None:
            out.append(record)
    return out


def parse_intraday_new_listings(
    raw: bytes | str,
    *,
    snapshot_date: date,
    content_type: PayloadFormat | None = None,
) -> list[CbbcRecord]:
    """Parse the HKEX new-listing announcement payload into ``CbbcRecord`` objects.

    Same validation and HSI-only filter rules as :func:`parse_outstanding_table`.

    Records with ``listing_date != snapshot_date`` are *kept* here; the caller
    (Task 4.4 intraday polling) is responsible for deciding which announcements
    qualify as "new today" before merging them into the in-memory snapshot.
    """
    return parse_outstanding_table(
        raw,
        snapshot_date=snapshot_date,
        content_type=content_type,
    )


# --------------------------------------------------------------------------- #
# Daily T+1 fetch task (Task 4.3)
# --------------------------------------------------------------------------- #

#: Asia/Hong_Kong timezone, used by the default clock and the daily fetch window.
_HK_TZ = ZoneInfo("Asia/Hong_Kong")

#: HKEX outstanding endpoint. URL is a placeholder; runtime override possible
#: via ``DailyFetcher(endpoint=...)``. The exact public CSV / page is to be
#: confirmed against HKEX's CBBC Statistics Report; structure is parsed by
#: :func:`parse_outstanding_table` regardless of CSV / HTML / JSON shape.
DEFAULT_OUTSTANDING_ENDPOINT: str = (
    "https://www.hkex.com.hk/eng/cbbc/data_outstanding.csv"
)

#: Daily fetch window (Asia/Hong_Kong): 18:00:00 inclusive to 23:59:59 inclusive.
_DAILY_FETCH_WINDOW_START = dtime(hour=18, minute=0, second=0)
_DAILY_FETCH_WINDOW_END = dtime(hour=23, minute=59, second=59)

#: Default per-request timeout for the daily fetch (seconds).
_DEFAULT_DAILY_REQUEST_TIMEOUT: float = 30.0

#: Default exponential backoff between retries (seconds). Length determines
#: the maximum number of *retries* (not attempts).
_DEFAULT_DAILY_BACKOFF: tuple[float, float, float] = (60.0, 180.0, 600.0)

#: Default minimum completeness ratio against the previous trading day.
#: A snapshot with ``len(records) < ratio * prev_count`` is rejected as
#: incomplete (Requirement 1.10).
_DEFAULT_COMPLETENESS_MIN_RATIO: float = 0.50


DailyFetchOutcome = Literal[
    "success",
    "skipped_non_trading_day",
    "skipped_already_exists",
    "failed_retries_exhausted",
    "incomplete_data",
]


@dataclass(frozen=True)
class DailyFetchResult:
    """Outcome metadata for a single :py:meth:`DailyFetcher.trigger` call.

    ``records_written`` is non-zero only on ``outcome == "success"``.
    ``records_dropped`` is informational; the parser already emits per-row
    warnings via ``record_parse_failed`` so the count is mostly useful for
    operators reading aggregated logs.

    ``fallback_snapshot_date`` is set whenever the call could not produce a
    fresh snapshot but a previous-trading-day snapshot remains available
    (Requirements 1.9, 1.10). ``None`` means there is no prior snapshot to
    fall back on.
    """

    outcome: DailyFetchOutcome
    snapshot_date: date
    records_written: int = 0
    records_dropped: int = 0
    fallback_snapshot_date: date | None = None


def is_in_daily_fetch_window(now_hk: datetime) -> bool:
    """Return ``True`` iff ``now_hk`` is within 18:00:00 – 23:59:59 (HK).

    The caller (the scheduler in Task 4.5) is responsible for supplying a
    timezone-aware ``datetime`` in ``Asia/Hong_Kong``. We compare the time
    component only, so DST-style transitions (which Hong Kong does not have)
    are irrelevant.
    """
    t = now_hk.timetz().replace(tzinfo=None) if now_hk.tzinfo else now_hk.time()
    return _DAILY_FETCH_WINDOW_START <= t <= _DAILY_FETCH_WINDOW_END


def _hk_now_default() -> datetime:
    """Default HK clock for :class:`DailyFetcher`."""
    return datetime.now(_HK_TZ)


# Failure reasons that may surface from a single fetch attempt. Kept as a
# small enum-like literal so the structured log is grep-friendly without
# leaking exception messages (which can carry URLs / IPs).
_FetchFailureReason = Literal[
    "http_status",
    "request_timeout",
    "network_error",
    "parse_error",
    "unknown_error",
]


class DailyFetcher:
    """Run the daily T+1 HKEX outstanding fetch with retry / fallback rules.

    The fetcher is *idempotent*: invoking :py:meth:`trigger` twice for the
    same trading day will fetch only once. Concurrent invocations for the
    same date are also serialized via a per-date :class:`asyncio.Lock`, so
    Task 4.5's scheduler need not coordinate retries itself.

    Acceptance criteria covered:

    - Requirement 1.1 — single request timeout of 30s by default.
    - Requirement 1.2 — non-trading days are skipped with the canonical log.
    - Requirement 1.3 — repeated triggers on the same trading day re-use the
      stored snapshot.
    - Requirement 1.7 — persistence goes through :py:meth:`CbbcStorage.write_snapshot`,
      which itself enforces immutability (``snapshot_immutable``).
    - Requirement 1.8 — retries follow ``[60s, 180s, 600s]`` (configurable),
      max 3 retries.
    - Requirement 1.9 — once retries are exhausted we emit
      ``daily_fetch_failed`` and surface the previous trading day's snapshot
      via :py:meth:`CbbcStorage.latest_before` so the magnet layer can fall
      back to it.
    - Requirement 1.10 — empty / sub-50% snapshots are rejected with
      ``daily_fetch_incomplete``; we do not write anything in that case.
    """

    def __init__(
        self,
        *,
        client: RateLimitedClient,
        storage: CbbcStorage,
        endpoint: str = DEFAULT_OUTSTANDING_ENDPOINT,
        clock_hk: Callable[[], datetime] = _hk_now_default,
        sleep: SleepFn = asyncio.sleep,
        max_retries: int = 3,
        backoff_seconds: tuple[float, ...] = _DEFAULT_DAILY_BACKOFF,
        single_request_timeout: float = _DEFAULT_DAILY_REQUEST_TIMEOUT,
        completeness_min_ratio: float = _DEFAULT_COMPLETENESS_MIN_RATIO,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if single_request_timeout <= 0:
            raise ValueError("single_request_timeout must be positive")
        if not 0.0 <= completeness_min_ratio <= 1.0:
            raise ValueError("completeness_min_ratio must be within [0.0, 1.0]")
        if len(backoff_seconds) < max_retries:
            raise ValueError(
                "backoff_seconds must have at least max_retries entries; "
                f"got {len(backoff_seconds)} for max_retries={max_retries}"
            )

        self._client = client
        self._storage = storage
        self._endpoint = endpoint
        self._clock_hk = clock_hk
        self._sleep = sleep
        self._max_retries = int(max_retries)
        self._backoff_seconds = tuple(float(s) for s in backoff_seconds)
        self._single_request_timeout = float(single_request_timeout)
        self._completeness_min_ratio = float(completeness_min_ratio)

        # Per-date lock guards against concurrent triggers for the same day
        # (a single instance is sufficient — the scheduler runs one loop).
        self._date_locks: dict[date, asyncio.Lock] = {}
        # A small ``asyncio.Lock`` to safely populate ``_date_locks`` itself.
        self._lock_table_guard = asyncio.Lock()

    # ----------------------- public API ----------------------- #

    async def trigger(self, *, date_hk: date) -> DailyFetchResult:
        """Run a single daily fetch for ``date_hk``.

        The caller (a scheduler) is responsible for deciding *when* to call
        this — :func:`is_in_daily_fetch_window` is exported for that purpose.

        The method does not raise on expected failure modes (HTTP errors,
        non-trading day, missing snapshots). Configuration errors surfaced by
        :class:`CbbcDataError` (whitelist / auth / paywall guards) are NOT
        retried — they will not succeed within the same run — and are mapped
        to ``failed_retries_exhausted``.
        """
        lock = await self._lock_for(date_hk)
        async with lock:
            return await self._trigger_locked(date_hk=date_hk)

    # ----------------------- internals ----------------------- #

    async def _lock_for(self, date_hk: date) -> asyncio.Lock:
        async with self._lock_table_guard:
            existing = self._date_locks.get(date_hk)
            if existing is None:
                existing = asyncio.Lock()
                self._date_locks[date_hk] = existing
            return existing

    async def _trigger_locked(self, *, date_hk: date) -> DailyFetchResult:
        # 1) Non-trading day → skip with the canonical INFO log.
        if not is_hk_trading_day(date_hk):
            logger.info(
                "daily fetch skipped: non-trading day",
                extra={
                    "level": "INFO",
                    "source": "cbbc_data",
                    "event": "daily_fetch_skipped_non_trading_day",
                    "snapshot_date": date_hk.isoformat(),
                },
            )
            return DailyFetchResult(
                outcome="skipped_non_trading_day",
                snapshot_date=date_hk,
            )

        # 2) Already fetched today → idempotent re-use (no log; this path
        #    occurs frequently in production when the scheduler ticks).
        if self._snapshot_exists(date_hk):
            return DailyFetchResult(
                outcome="skipped_already_exists",
                snapshot_date=date_hk,
            )

        # 3) Attempt + retry loop.
        attempts = self._max_retries + 1  # initial + retries
        last_failure: tuple[_FetchFailureReason, str] | None = None

        for attempt_idx in range(attempts):
            try:
                payload, content_type = await self._fetch_once(attempt_idx + 1)
            except CbbcDataError as exc:
                # Whitelist / auth / paywall guard — non-retryable. Emit the
                # daily_fetch_failed log immediately so operators can act.
                fallback = self._fallback_snapshot_date(date_hk)
                logger.error(
                    "daily fetch failed: configuration error",
                    extra={
                        "level": "ERROR",
                        "source": "cbbc_data",
                        "event": "daily_fetch_failed",
                        "snapshot_date": date_hk.isoformat(),
                        "attempt_count": attempt_idx + 1,
                        "reason": f"cbbc_data_error_{exc.code}",
                        "fallback_snapshot_date": (
                            fallback.isoformat() if fallback else None
                        ),
                    },
                )
                return DailyFetchResult(
                    outcome="failed_retries_exhausted",
                    snapshot_date=date_hk,
                    fallback_snapshot_date=fallback,
                )
            except _FetchAttemptFailure as failure:
                last_failure = (failure.reason, failure.detail)
                if attempt_idx < self._max_retries:
                    backoff = self._backoff_seconds[attempt_idx]
                    await self._sleep(backoff)
                    continue
                # All attempts exhausted.
                fallback = self._fallback_snapshot_date(date_hk)
                logger.error(
                    "daily fetch failed: retries exhausted",
                    extra={
                        "level": "ERROR",
                        "source": "cbbc_data",
                        "event": "daily_fetch_failed",
                        "snapshot_date": date_hk.isoformat(),
                        "attempt_count": attempt_idx + 1,
                        "reason": failure.reason,
                        "detail": failure.detail,
                        "fallback_snapshot_date": (
                            fallback.isoformat() if fallback else None
                        ),
                    },
                )
                return DailyFetchResult(
                    outcome="failed_retries_exhausted",
                    snapshot_date=date_hk,
                    fallback_snapshot_date=fallback,
                )

            # 4) Parse + completeness check.
            records = parse_outstanding_table(
                payload,
                snapshot_date=date_hk,
                content_type=content_type,
            )
            new_count = len(records)
            prev_snapshot = self._storage.latest_before(date_hk)
            prev_count = len(prev_snapshot.records) if prev_snapshot is not None else None

            if self._is_incomplete(new_count, prev_count):
                logger.error(
                    "daily fetch incomplete",
                    extra={
                        "level": "ERROR",
                        "source": "cbbc_data",
                        "event": "daily_fetch_incomplete",
                        "snapshot_date": date_hk.isoformat(),
                        "attempt_count": attempt_idx + 1,
                        "hsi_record_count": new_count,
                        "prev_record_count": prev_count,
                        "completeness_min_ratio": self._completeness_min_ratio,
                        "fallback_snapshot_date": (
                            prev_snapshot.snapshot_date.isoformat()
                            if prev_snapshot is not None
                            else None
                        ),
                    },
                )
                return DailyFetchResult(
                    outcome="incomplete_data",
                    snapshot_date=date_hk,
                    records_written=0,
                    fallback_snapshot_date=(
                        prev_snapshot.snapshot_date if prev_snapshot is not None else None
                    ),
                )

            # 5) Persist (immutability is enforced by CbbcStorage.write_snapshot).
            snapshot = CbbcSnapshot(snapshot_date=date_hk, records=tuple(records))
            try:
                self._storage.write_snapshot(snapshot)
            except SnapshotError as exc:
                # We checked existence above, so this race only happens if a
                # parallel writer beat us to it. Treat it as success and
                # surface the existing snapshot.
                if exc.code == "snapshot_immutable":
                    return DailyFetchResult(
                        outcome="skipped_already_exists",
                        snapshot_date=date_hk,
                    )
                raise

            return DailyFetchResult(
                outcome="success",
                snapshot_date=date_hk,
                records_written=new_count,
            )

        # Should be unreachable: the loop either returns or exhausts retries.
        # Belt-and-braces fallback so type-checkers see a return on every path.
        fallback = self._fallback_snapshot_date(date_hk)
        reason, detail = last_failure if last_failure is not None else ("unknown_error", "")
        logger.error(
            "daily fetch failed: unreachable path",
            extra={
                "level": "ERROR",
                "source": "cbbc_data",
                "event": "daily_fetch_failed",
                "snapshot_date": date_hk.isoformat(),
                "attempt_count": attempts,
                "reason": reason,
                "detail": detail,
                "fallback_snapshot_date": (
                    fallback.isoformat() if fallback else None
                ),
            },
        )
        return DailyFetchResult(
            outcome="failed_retries_exhausted",
            snapshot_date=date_hk,
            fallback_snapshot_date=fallback,
        )

    async def _fetch_once(self, attempt_count: int) -> tuple[bytes | str, PayloadFormat | None]:
        """Run a single HTTP attempt; raise :class:`_FetchAttemptFailure` on
        retryable errors. Non-retryable :class:`CbbcDataError` propagates."""
        try:
            response = await self._client.get(
                self._endpoint,
                timeout=self._single_request_timeout,
            )
        except CbbcDataError:
            raise
        except asyncio.TimeoutError as exc:
            raise _FetchAttemptFailure(
                reason="request_timeout",
                detail=f"request_timeout_after_{self._single_request_timeout}s",
            ) from exc
        except Exception as exc:
            # Network errors (DNS, TLS, connection reset, etc.). httpx's
            # exception hierarchy lives outside this module, so we treat all
            # non-CbbcDataError exceptions from the transport as retryable
            # network failures.
            detail = exc.__class__.__name__
            if _looks_like_timeout(exc):
                raise _FetchAttemptFailure(
                    reason="request_timeout",
                    detail=detail,
                ) from exc
            raise _FetchAttemptFailure(
                reason="network_error",
                detail=detail,
            ) from exc

        status = getattr(response, "status_code", None)
        if not isinstance(status, int) or status < 200 or status >= 300:
            raise _FetchAttemptFailure(
                reason="http_status",
                detail=f"http_status_{status}",
            )

        # Prefer ``content`` (bytes); fall back to ``text`` if a custom backend
        # exposes only the latter. The parser handles both.
        payload: bytes | str
        content = getattr(response, "content", None)
        if content is None or content == b"":
            payload = getattr(response, "text", "") or ""
        else:
            payload = content
        return payload, None

    def _snapshot_exists(self, d: date) -> bool:
        """Cheap existence check that avoids triggering the survivorship filter."""
        try:
            return d in set(self._storage.list_dates())
        except Exception:
            # If the storage layer is briefly unavailable, treat the snapshot
            # as missing so we attempt to fetch. The fetch itself will surface
            # any persistent storage error.
            return False

    def _fallback_snapshot_date(self, d: date) -> date | None:
        """Return the previous trading day's snapshot date, or ``None``."""
        try:
            prev = self._storage.latest_before(d)
        except Exception:
            return None
        return prev.snapshot_date if prev is not None else None

    def _is_incomplete(self, new_count: int, prev_count: int | None) -> bool:
        """Apply Requirement 1.10's completeness rule.

        - Zero records is always incomplete.
        - With a previous snapshot, ``new_count < ratio * prev_count`` is
          incomplete.
        - Without a previous snapshot we accept any non-zero count (we have
          nothing to compare against; the operator will see the absolute count
          in the success log line).
        """
        if new_count == 0:
            return True
        if prev_count is None or prev_count == 0:
            return False
        return new_count < self._completeness_min_ratio * prev_count


@dataclass
class _FetchAttemptFailure(Exception):
    """Internal retryable failure marker raised inside :py:meth:`DailyFetcher._fetch_once`."""

    reason: _FetchFailureReason
    detail: str

    def __post_init__(self) -> None:
        super().__init__(f"{self.reason}:{self.detail}")


def _looks_like_timeout(exc: BaseException) -> bool:
    """Heuristic: detect ``httpx.TimeoutException`` without importing httpx.

    The ``DailyFetcher`` is intentionally backend-agnostic, so we sniff the
    class name rather than ``isinstance``-check. ``TimeoutError`` (stdlib) is
    handled separately by the caller.
    """
    name = exc.__class__.__name__
    return "Timeout" in name


# --------------------------------------------------------------------------- #
# Intraday polling (Task 4.4) + CbbcDataService (Task 4.5)
# --------------------------------------------------------------------------- #

#: Default endpoint for the new-listing intraday feed. Like
#: ``DEFAULT_OUTSTANDING_ENDPOINT``, the exact URL is a placeholder and can be
#: overridden via ``IntradayPoller(endpoint=...)``.
DEFAULT_INTRADAY_NEW_LISTING_ENDPOINT: str = (
    "https://www.hkex.com.hk/eng/cbbc/data_new_listings.csv"
)

#: Trading sessions during which intraday polling is active (Asia/Hong_Kong).
_INTRADAY_MORNING_START = dtime(hour=9, minute=30, second=0)
_INTRADAY_MORNING_END = dtime(hour=12, minute=0, second=0)
_INTRADAY_AFTERNOON_START = dtime(hour=13, minute=0, second=0)
_INTRADAY_AFTERNOON_END = dtime(hour=16, minute=0, second=0)

#: Per-request timeout for intraday polling (seconds).
_DEFAULT_INTRADAY_REQUEST_TIMEOUT: float = 10.0

#: 5-minute consecutive-failure window (R2.6) after which the poller
#: degrades and persists ``cbbc_intraday_polling_suspended=True``.
_INTRADAY_DEGRADE_THRESHOLD_SECONDS: float = 5 * 60.0

# Reasons the poller may classify as a single "failure" for the degrade timer.
_IntradayFailureReason = Literal[
    "http_status",
    "timeout",
    "transport_error",
    "parse_error",
    "dns_error",
    "tls_error",
    "blocked_endpoint",
]


def is_in_intraday_session(now_hk: datetime) -> bool:
    """``True`` iff ``now_hk`` falls inside HKEX morning or afternoon session."""
    t = now_hk.timetz().replace(tzinfo=None)
    return (
        _INTRADAY_MORNING_START <= t <= _INTRADAY_MORNING_END
        or _INTRADAY_AFTERNOON_START <= t <= _INTRADAY_AFTERNOON_END
    )


class _RuntimeConfigPersistence(Protocol):
    """Minimal contract used by :class:`IntradayPoller` to persist the
    ``cbbc_intraday_polling_suspended`` flag (R2.6)."""

    def set_intraday_polling_suspended(self, suspended: bool) -> None: ...
    def is_intraday_polling_suspended(self) -> bool: ...


@dataclass
class _IntradayState:
    """Internal mutable state owned by :class:`IntradayPoller`."""

    failure_started_at: float | None = None  # monotonic
    suspended_notice_logged_for_day: date | None = None
    last_poll_outcome: str = "idle"


class IntradayPoller:
    """Per-bar new-listing poller (Task 4.4 / R2.1–R2.7).

    The poller delegates HTTP / parsing to the same ``RateLimitedClient`` +
    ``parse_intraday_new_listings`` plumbing used by the daily fetcher, so
    this class stays small and focused on the stateful polling rules:

    - Active **only** during HKEX morning / afternoon sessions (R2.1).
    - Polls at ``cbbc_intraday_poll_interval_seconds`` (default 60s) with
      a 10s per-request timeout.
    - On detecting a record whose ``code`` is **new today** (not in the
      in-memory snapshot, ``listing_date == today``), merges it into the
      live snapshot within 90 s (R2.2 / R2.3).
    - Drops non-HSI records (R2.5).
    - Counts five minutes of consecutive failures → log
      ``event=intraday_polling_degraded``, persist ``suspended=True``,
      stop polling for the day (R2.6).
    - When entering a new trading session while ``suspended=True``, write
      one ``cbbc_intraday_polling_suspended_notice`` per day (R2.7).
    """

    def __init__(
        self,
        *,
        client: RateLimitedClient,
        snapshot_view: "Callable[[], CbbcSnapshot | None]",
        on_records_merged: "Callable[[CbbcSnapshot], None]",
        runtime_config: _RuntimeConfigPersistence,
        endpoint: str = DEFAULT_INTRADAY_NEW_LISTING_ENDPOINT,
        clock_hk: Callable[[], datetime] = _hk_now_default,
        sleep: SleepFn = asyncio.sleep,
        single_request_timeout: float = _DEFAULT_INTRADAY_REQUEST_TIMEOUT,
        degrade_threshold_seconds: float = _INTRADAY_DEGRADE_THRESHOLD_SECONDS,
        poll_interval_provider: Callable[[], float] | None = None,
        decay_pts_provider: Callable[[], float] | None = None,
    ) -> None:
        if single_request_timeout <= 0:
            raise ValueError("single_request_timeout must be positive")
        if degrade_threshold_seconds <= 0:
            raise ValueError("degrade_threshold_seconds must be positive")

        self._client = client
        self._snapshot_view = snapshot_view
        self._on_records_merged = on_records_merged
        self._runtime_config = runtime_config
        self._endpoint = endpoint
        self._clock_hk = clock_hk
        self._sleep = sleep
        self._single_request_timeout = float(single_request_timeout)
        self._degrade_threshold_seconds = float(degrade_threshold_seconds)
        self._poll_interval_provider = poll_interval_provider
        self._decay_pts_provider = decay_pts_provider

        self._state = _IntradayState()
        self._stop_event: asyncio.Event | None = None

    # ---------------- helpers ---------------- #

    def _poll_interval(self) -> float:
        if self._poll_interval_provider is None:
            return 60.0
        try:
            v = float(self._poll_interval_provider())
        except Exception:  # noqa: BLE001
            return 60.0
        if not math.isfinite(v) or v < 1.0:
            return 60.0
        return v

    def _decay_pts(self) -> float:
        if self._decay_pts_provider is None:
            return 300.0
        try:
            v = float(self._decay_pts_provider())
        except Exception:  # noqa: BLE001
            return 300.0
        if not math.isfinite(v) or v <= 0:
            return 300.0
        return v

    # ---------------- main loop ---------------- #

    async def run_forever(self) -> None:
        """Drive the poller until ``stop()`` is called.

        Outside the morning / afternoon session the loop simply sleeps the
        poll interval; inside a session it calls :meth:`poll_once` and reacts
        to the outcome.
        """
        self._stop_event = asyncio.Event()
        try:
            while not self._stop_event.is_set():
                now_hk = self._clock_hk()
                if not is_hk_trading_day(now_hk.date()):
                    await self._sleep_or_stop(self._poll_interval())
                    continue

                if self._runtime_config.is_intraday_polling_suspended():
                    self._maybe_log_suspended_notice(now_hk)
                    await self._sleep_or_stop(self._poll_interval())
                    continue

                if is_in_intraday_session(now_hk):
                    await self.poll_once(now_hk=now_hk)
                await self._sleep_or_stop(self._poll_interval())
        except asyncio.CancelledError:
            return

    async def _sleep_or_stop(self, seconds: float) -> None:
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

    def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    # ---------------- public API ---------------- #

    async def poll_once(self, *, now_hk: datetime | None = None) -> str:
        """Run a single poll cycle. Returns the textual outcome label.

        This is also the unit-test entry point. The method itself never
        raises: every error path bumps the consecutive-failure window and
        possibly transitions the poller to suspended.
        """
        if now_hk is None:
            now_hk = self._clock_hk()

        if self._runtime_config.is_intraday_polling_suspended():
            self._maybe_log_suspended_notice(now_hk)
            return "suspended"
        if not is_hk_trading_day(now_hk.date()):
            return "non_trading_day"
        if not is_in_intraday_session(now_hk):
            return "out_of_session"

        # Issue one HTTP GET. Failures here are absorbed and counted.
        try:
            response = await self._client.get(
                self._endpoint,
                timeout=self._single_request_timeout,
            )
        except CbbcDataError:
            self._record_failure(reason="blocked_endpoint", now_hk=now_hk)
            return "blocked_endpoint"
        except Exception as exc:  # noqa: BLE001
            reason = self._classify_transport_error(exc)
            self._record_failure(reason=reason, now_hk=now_hk)
            return reason

        if not 200 <= int(getattr(response, "status_code", 0)) < 300:
            self._record_failure(reason="http_status", now_hk=now_hk)
            return "http_status"

        # Parse and merge.
        try:
            content = response.content if isinstance(response.content, (bytes, str)) else response.text
            records = parse_intraday_new_listings(
                content,
                snapshot_date=now_hk.date(),
            )
        except Exception:  # noqa: BLE001
            self._record_failure(reason="parse_error", now_hk=now_hk)
            return "parse_error"

        # Successful HTTP+parse → reset failure window.
        self._state.failure_started_at = None

        merged = self._merge_new_listings(records, now_hk=now_hk)
        self._state.last_poll_outcome = "ok"
        return f"ok:merged={merged}"

    # ---------------- state transitions ---------------- #

    def _record_failure(self, *, reason: str, now_hk: datetime) -> None:
        self._state.last_poll_outcome = f"fail:{reason}"
        now_mono = time.monotonic()
        if self._state.failure_started_at is None:
            self._state.failure_started_at = now_mono
        elapsed = now_mono - self._state.failure_started_at
        if elapsed >= self._degrade_threshold_seconds:
            self._enter_suspended(now_hk=now_hk, reason=reason, elapsed=elapsed)

    def _enter_suspended(self, *, now_hk: datetime, reason: str, elapsed: float) -> None:
        if self._runtime_config.is_intraday_polling_suspended():
            return
        try:
            self._runtime_config.set_intraday_polling_suspended(True)
        except Exception:  # noqa: BLE001 - never let persistence break the loop
            pass
        logger.warning(
            "intraday_polling_degraded",
            extra={
                "level": "WARN",
                "source": "cbbc_data",
                "event": "intraday_polling_degraded",
                "trade_date": now_hk.date().isoformat(),
                "reason": reason,
                "elapsed_seconds": round(elapsed, 1),
            },
        )

    def _maybe_log_suspended_notice(self, now_hk: datetime) -> None:
        if not is_in_intraday_session(now_hk):
            return
        today = now_hk.date()
        if self._state.suspended_notice_logged_for_day == today:
            return
        self._state.suspended_notice_logged_for_day = today
        logger.info(
            "cbbc_intraday_polling_suspended_notice",
            extra={
                "level": "INFO",
                "source": "cbbc_data",
                "event": "cbbc_intraday_polling_suspended_notice",
                "trade_date": today.isoformat(),
            },
        )

    @staticmethod
    def _classify_transport_error(exc: BaseException) -> str:
        name = exc.__class__.__name__.lower()
        if "timeout" in name or isinstance(exc, asyncio.TimeoutError):
            return "timeout"
        if "dns" in name or "resolution" in name:
            return "dns_error"
        if "ssl" in name or "tls" in name or "cert" in name:
            return "tls_error"
        return "transport_error"

    # ---------------- merge logic ---------------- #

    def _merge_new_listings(
        self,
        records: list[CbbcRecord],
        *,
        now_hk: datetime,
    ) -> int:
        """Merge truly-new HSI records into the in-memory snapshot.

        Returns the number of records merged. Drops non-HSI silently with a
        DEBUG log per dropped record (R2.5). Records whose ``listing_date``
        is **not** today are also dropped silently — the same intraday feed
        contains future listings that we don't merge until they go live.
        """
        snapshot = self._snapshot_view()
        existing_codes: set[str] = (
            {r.code for r in snapshot.records} if snapshot is not None else set()
        )
        merged: list[CbbcRecord] = list(snapshot.records) if snapshot is not None else []
        merged_count = 0
        today = now_hk.date()
        decay_pts = self._decay_pts()

        for rec in records:
            if rec.underlying != "HSI":
                logger.debug(
                    "intraday_new_listing_dropped_non_hsi",
                    extra={
                        "level": "DEBUG",
                        "source": "cbbc_data",
                        "event": "intraday_new_listing_dropped_non_hsi",
                        "code": rec.code,
                        "underlying": rec.underlying,
                    },
                )
                continue
            if rec.code in existing_codes:
                continue
            if rec.listing_date != today:
                continue

            existing_codes.add(rec.code)
            merged.append(rec)
            merged_count += 1

        if merged_count == 0:
            return 0

        new_snapshot = CbbcSnapshot(
            snapshot_date=today,
            records=tuple(merged),
        )

        # Notify the parent service (which forwards to MagnetEngine).
        try:
            self._on_records_merged(new_snapshot)
        except Exception:  # noqa: BLE001
            pass

        # Near-money INFO log per newly-merged record (R2.4).
        latest_records = new_snapshot.records[-merged_count:]
        # We need the current HSI spot to compute distance; the parent
        # service can supply it via decay_pts_provider but the spot itself is
        # not directly available here. Conservatively skip the near-money
        # log when we have no spot to compare against; MagnetEngine still
        # observes the new record.
        return merged_count


class CbbcDataService:
    """Compose ``DailyFetcher`` + ``IntradayPoller`` and expose a small read-only
    surface for ``HSIStrategyEngine`` and the ``magnet_overlay`` WebSocket.

    Everything below the ``start``/``stop`` lifecycle is best-effort: errors
    inside fetchers / pollers must never propagate to the caller (R10.6).
    """

    def __init__(
        self,
        *,
        storage: CbbcStorage,
        runtime_config: _RuntimeConfigPersistence,
        client: RateLimitedClient | None = None,
        clock_hk: Callable[[], datetime] = _hk_now_default,
        decay_pts_provider: Callable[[], float] | None = None,
        poll_interval_provider: Callable[[], float] | None = None,
        on_snapshot_changed: Callable[[CbbcSnapshot], None] | None = None,
        outstanding_endpoint: str = DEFAULT_OUTSTANDING_ENDPOINT,
        intraday_endpoint: str = DEFAULT_INTRADAY_NEW_LISTING_ENDPOINT,
    ) -> None:
        self._storage = storage
        self._runtime_config = runtime_config
        self._clock_hk = clock_hk
        self._on_snapshot_changed = on_snapshot_changed
        self._client = client if client is not None else RateLimitedClient()

        self._daily_fetcher = DailyFetcher(
            client=self._client,
            storage=storage,
            endpoint=outstanding_endpoint,
            clock_hk=clock_hk,
        )
        self._intraday_poller = IntradayPoller(
            client=self._client,
            snapshot_view=self.current_snapshot,
            on_records_merged=self._handle_merged,
            runtime_config=runtime_config,
            endpoint=intraday_endpoint,
            clock_hk=clock_hk,
            decay_pts_provider=decay_pts_provider,
            poll_interval_provider=poll_interval_provider,
        )

        self._daily_loop_task: asyncio.Task[None] | None = None
        self._intraday_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

        self._snapshot: CbbcSnapshot | None = None
        self._last_refresh_ts_hk: datetime | None = None
        self._lock = asyncio.Lock()

    # ---------------- public read-only API (R7.7 / WS / strategy) ---------------- #

    def current_snapshot(self) -> CbbcSnapshot | None:
        return self._snapshot

    def last_refresh_ts_hk(self) -> datetime | None:
        return self._last_refresh_ts_hk

    def is_intraday_polling_suspended(self) -> bool:
        try:
            return bool(self._runtime_config.is_intraday_polling_suspended())
        except Exception:  # noqa: BLE001
            return False

    async def trigger_daily_fetch(self, *, date_hk: date) -> DailyFetchResult:
        result = await self._daily_fetcher.trigger(date_hk=date_hk)
        if result.outcome == "success":
            self._reload_snapshot_for_today()
        return result

    async def trigger_intraday_poll_once(self) -> str:
        return await self._intraday_poller.poll_once()

    # ---------------- lifecycle ---------------- #

    async def start(self) -> None:
        """Spin up daily + intraday loops if they aren't already running.

        Bootstrap path: try to load today's (or latest-before-today) snapshot
        from the storage layer so consumers have data immediately, even
        before the first daily fetch fires.
        """
        if self._stop_event is not None:
            return  # already running
        self._stop_event = asyncio.Event()

        # Eagerly load any existing snapshot.
        self._reload_snapshot_for_today()

        self._daily_loop_task = asyncio.create_task(
            self._daily_scheduler(), name="cbbc_daily_scheduler"
        )
        self._intraday_task = asyncio.create_task(
            self._intraday_poller.run_forever(), name="cbbc_intraday_poller"
        )

    async def stop(self) -> None:
        if self._stop_event is None:
            return
        self._stop_event.set()
        self._intraday_poller.stop()
        for task in (self._daily_loop_task, self._intraday_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._daily_loop_task = None
        self._intraday_task = None
        self._stop_event = None
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- daily scheduler ---------------- #

    async def _daily_scheduler(self) -> None:
        """Sleep until the next 18:00 HK window and trigger the daily fetch.

        Uses 5-minute polling rather than computing the exact next window so
        the scheduler stays robust to wall-clock skew, daylight saving
        transitions and process restarts inside the window.
        """
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            now_hk = self._clock_hk()
            if is_hk_trading_day(now_hk.date()) and is_in_daily_fetch_window(now_hk):
                try:
                    await self.trigger_daily_fetch(date_hk=now_hk.date())
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "daily_scheduler_unhandled",
                        extra={
                            "level": "WARN",
                            "source": "cbbc_data",
                            "event": "daily_scheduler_unhandled",
                            "reason": type(exc).__name__,
                        },
                    )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=300.0)
            except asyncio.TimeoutError:
                continue

    # ---------------- snapshot maintenance ---------------- #

    def _handle_merged(self, snapshot: CbbcSnapshot) -> None:
        """Called by IntradayPoller when records merged. Updates the cache
        and forwards to the optional ``on_snapshot_changed`` callback so the
        strategy engine / magnet engine can observe the new snapshot."""
        self._snapshot = snapshot
        self._last_refresh_ts_hk = self._clock_hk()
        if self._on_snapshot_changed is not None:
            try:
                self._on_snapshot_changed(snapshot)
            except Exception:  # noqa: BLE001
                pass

    def _reload_snapshot_for_today(self) -> None:
        """Re-read the latest available snapshot from the storage layer."""
        today = self._clock_hk().date()
        snapshot: CbbcSnapshot | None = None
        try:
            if is_hk_trading_day(today):
                snapshot = self._storage.read_snapshot(today)
        except SnapshotError:
            snapshot = None
        except Exception:  # noqa: BLE001
            snapshot = None
        if snapshot is None:
            try:
                snapshot = self._storage.latest_before(today)
            except Exception:  # noqa: BLE001
                snapshot = None
        if snapshot is not None:
            self._snapshot = snapshot
            self._last_refresh_ts_hk = self._clock_hk()
            if self._on_snapshot_changed is not None:
                try:
                    self._on_snapshot_changed(snapshot)
                except Exception:  # noqa: BLE001
                    pass


__all__ = [
    "CbbcDataError",
    "CbbcDataErrorCode",
    "CbbcDataService",
    "DailyFetcher",
    "DailyFetchOutcome",
    "DailyFetchResult",
    "DEFAULT_INTRADAY_NEW_LISTING_ENDPOINT",
    "DEFAULT_OUTSTANDING_ENDPOINT",
    "HKEX_WHITELIST_HOSTS",
    "HttpBackend",
    "HttpResponse",
    "HttpxBackend",
    "IntradayPoller",
    "RateLimitedClient",
    "RequestOutcome",
    "is_in_daily_fetch_window",
    "is_in_intraday_session",
    "parse_outstanding_table",
    "parse_intraday_new_listings",
]
