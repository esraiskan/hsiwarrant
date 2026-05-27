"""
CBBC 街货磁吸信号 - 存储层数据类与错误码（任务 2.1）。

本模块仅定义：
- ``CbbcRecord``：单只 CBBC 的不可变记录（10 个字段）。
- ``CbbcSnapshot``：某一交易日的全量 CBBC 快照（不可变，``records`` 为 ``tuple``）。
- ``SnapshotError``：存储层错误，``code`` 限定四个允许值。
- ``is_hk_trading_day(d)`` / ``hk_public_holidays()``：港股交易日工具函数。

不在本任务实现 ``CbbcStorage`` 类、parquet 读写、生存偏差守卫等逻辑（属于任务 2.2）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, get_args


SnapshotErrorCode = Literal[
    "non_trading_day",
    "snapshot_missing",
    "snapshot_immutable",
    "no_reverse_deduction_allowed",
]

_ALLOWED_SNAPSHOT_ERROR_CODES: frozenset[str] = frozenset(get_args(SnapshotErrorCode))

CbbcDirection = Literal["bull", "bear"]


@dataclass(frozen=True)
class CbbcRecord:
    """单只 CBBC 街货记录。

    所有字段在解析阶段必须非空；``direction`` 仅允许 ``"bull"`` 或 ``"bear"``；
    ``underlying`` 在 HSI 磁吸场景下必须为 ``"HSI"``。
    """
    issuer: str
    code: str
    call_level: float
    outstanding_shares: float
    er_ratio: float
    direction: CbbcDirection
    listing_date: date
    maturity_date: date
    underlying: str
    snapshot_date: date


@dataclass(frozen=True)
class CbbcSnapshot:
    """某一交易日的 CBBC outstanding 全量快照。

    ``records`` 使用 ``tuple`` 以保证整体不可变；写入持久化文件后，调用方应视为只读。
    """
    snapshot_date: date
    records: tuple[CbbcRecord, ...]


class SnapshotError(Exception):
    """CBBC 存储层错误。

    ``code`` 字段被限定为四个允许值之一：
    - ``"non_trading_day"``：调用方传入的日期不是港股交易日。
    - ``"snapshot_missing"``：请求日期对应的快照文件不存在。
    - ``"snapshot_immutable"``：尝试覆盖已存在的同名快照。
    - ``"no_reverse_deduction_allowed"``：尝试以"今日存活集合"反推历史快照。
    """

    code: SnapshotErrorCode

    def __init__(self, code: SnapshotErrorCode, message: str | None = None) -> None:
        if code not in _ALLOWED_SNAPSHOT_ERROR_CODES:
            raise ValueError(
                f"SnapshotError.code 必须是 {sorted(_ALLOWED_SNAPSHOT_ERROR_CODES)} 之一，收到 {code!r}"
            )
        self.code = code
        super().__init__(message if message is not None else code)


# ---------------------------------------------------------------------------
# HK 交易日工具函数
# ---------------------------------------------------------------------------

# 港股公众假期清单（来源：Hong Kong gazette / GovHK general holidays）。
# 记录覆盖 2023–2026 四年；保留每年的全部一般假期，包括落在周末的项，
# 这样集合本身可直接复用作 ``hk_public_holidays()`` 的返回值，``is_hk_trading_day``
# 中对周末的短路逻辑仍能正确处理。
_HK_PUBLIC_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2023
        date(2023, 1, 2),    # The day following the first day of January
        date(2023, 1, 23),   # The second day of Lunar New Year
        date(2023, 1, 24),   # The third day of Lunar New Year
        date(2023, 1, 25),   # The fourth day of Lunar New Year
        date(2023, 4, 5),    # Ching Ming Festival
        date(2023, 4, 7),    # Good Friday
        date(2023, 4, 8),    # The day following Good Friday (Saturday)
        date(2023, 4, 10),   # Easter Monday
        date(2023, 5, 1),    # Labour Day
        date(2023, 5, 26),   # Birthday of the Buddha
        date(2023, 6, 22),   # Tuen Ng Festival
        date(2023, 7, 1),    # HKSAR Establishment Day (Saturday)
        date(2023, 9, 30),   # The day following the Chinese Mid-Autumn Festival (Saturday)
        date(2023, 10, 2),   # The day following National Day
        date(2023, 10, 23),  # Chung Yeung Festival
        date(2023, 12, 25),  # Christmas Day
        date(2023, 12, 26),  # The first weekday after Christmas Day
        # 2024
        date(2024, 1, 1),    # The first day of January
        date(2024, 2, 10),   # Lunar New Year's Day (Saturday)
        date(2024, 2, 12),   # The third day of Lunar New Year
        date(2024, 2, 13),   # The fourth day of Lunar New Year
        date(2024, 3, 29),   # Good Friday
        date(2024, 3, 30),   # The day following Good Friday (Saturday)
        date(2024, 4, 1),    # Easter Monday
        date(2024, 4, 4),    # Ching Ming Festival
        date(2024, 5, 1),    # Labour Day
        date(2024, 5, 15),   # Birthday of the Buddha
        date(2024, 6, 10),   # Tuen Ng Festival
        date(2024, 7, 1),    # HKSAR Establishment Day
        date(2024, 9, 18),   # The day following the Chinese Mid-Autumn Festival
        date(2024, 10, 1),   # National Day
        date(2024, 10, 11),  # Chung Yeung Festival
        date(2024, 12, 25),  # Christmas Day
        date(2024, 12, 26),  # The first weekday after Christmas Day
        # 2025
        date(2025, 1, 1),    # The first day of January
        date(2025, 1, 29),   # Lunar New Year's Day
        date(2025, 1, 30),   # The second day of Lunar New Year
        date(2025, 1, 31),   # The third day of Lunar New Year
        date(2025, 4, 4),    # Ching Ming Festival
        date(2025, 4, 18),   # Good Friday
        date(2025, 4, 19),   # The day following Good Friday (Saturday)
        date(2025, 4, 21),   # Easter Monday
        date(2025, 5, 1),    # Labour Day
        date(2025, 5, 5),    # Birthday of the Buddha
        date(2025, 5, 31),   # Tuen Ng Festival (Saturday)
        date(2025, 7, 1),    # HKSAR Establishment Day
        date(2025, 10, 1),   # National Day
        date(2025, 10, 7),   # The day following the Chinese Mid-Autumn Festival
        date(2025, 10, 29),  # Chung Yeung Festival
        date(2025, 12, 25),  # Christmas Day
        date(2025, 12, 26),  # The first weekday after Christmas Day
        # 2026
        date(2026, 1, 1),    # The first day of January
        date(2026, 2, 17),   # Lunar New Year's Day
        date(2026, 2, 18),   # The second day of Lunar New Year
        date(2026, 2, 19),   # The third day of Lunar New Year
        date(2026, 4, 3),    # Good Friday
        date(2026, 4, 4),    # The day following Good Friday (Saturday)
        date(2026, 4, 6),    # The day following Ching Ming Festival
        date(2026, 4, 7),    # The day following Easter Monday
        date(2026, 5, 1),    # Labour Day
        date(2026, 5, 25),   # The day following the Birthday of the Buddha
        date(2026, 6, 19),   # Tuen Ng Festival
        date(2026, 7, 1),    # HKSAR Establishment Day
        date(2026, 9, 26),   # The day following the Chinese Mid-Autumn Festival (Saturday)
        date(2026, 10, 1),   # National Day
        date(2026, 10, 19),  # The day following Chung Yeung Festival
        date(2026, 12, 25),  # Christmas Day
        date(2026, 12, 26),  # The first weekday after Christmas Day (Saturday)
    }
)


def hk_public_holidays() -> frozenset[date]:
    """返回 HKEX / GovHK 公布的港股公众假期清单（覆盖 2023–2026）。

    返回值是 ``frozenset``，对调用方只读。后续年度需要扩充时仅在本模块内追加。
    """
    return _HK_PUBLIC_HOLIDAYS


def is_hk_trading_day(d: date) -> bool:
    """判断 ``d`` 是否为港股交易日。

    定义：当且仅当 ``d`` 不是周六/周日，且 ``d`` 不在 ``hk_public_holidays()`` 集合内时，
    才返回 ``True``。其余情况一律返回 ``False``。
    """
    # 周末（Saturday=5, Sunday=6）
    if d.weekday() >= 5:
        return False
    if d in _HK_PUBLIC_HOLIDAYS:
        return False
    return True


# ---------------------------------------------------------------------------
# CbbcStorage（任务 2.2）：parquet 读写、生存偏差守卫、原子写入
# ---------------------------------------------------------------------------

import os
import re
from pathlib import Path

# pandas / pyarrow are only required for parquet I/O on ``CbbcStorage``; the
# bare ``CbbcRecord`` / ``CbbcSnapshot`` data classes (task 2.1) must remain
# importable on machines without those binary deps so that downstream
# modules (cbbc_calculator, cbbc_signal_adapter, strategy.py) can be
# unit-tested even when pandas / numpy ABIs are mismatched.
try:
    import pandas as pd
    _HAS_PANDAS = True
except Exception:  # noqa: BLE001 - import-time env failures must not propagate
    pd = None  # type: ignore[assignment]
    _HAS_PANDAS = False


def _require_pandas() -> "pd":
    if not _HAS_PANDAS or pd is None:
        raise RuntimeError(
            "pandas is required for CbbcStorage parquet I/O but is unavailable; "
            "install pandas + pyarrow or stub the storage layer in tests"
        )
    return pd


_DEFAULT_BASE_DIR = Path(__file__).resolve().parent / "data" / "cbbc"

# 文件名格式：outstanding_YYYYMMDD.parquet
_SNAPSHOT_FILENAME_RE = re.compile(r"^outstanding_(\d{8})\.parquet$")
_SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "issuer",
    "code",
    "call_level",
    "outstanding_shares",
    "er_ratio",
    "direction",
    "listing_date",
    "maturity_date",
    "underlying",
    "snapshot_date",
)


def _format_snapshot_date(d: date) -> str:
    """``date`` → ``"YYYYMMDD"`` 文件名片段。"""
    return d.strftime("%Y%m%d")


def _parse_snapshot_filename(name: str) -> date | None:
    """解析 ``outstanding_YYYYMMDD.parquet`` 文件名为 ``date``；解析失败返回 ``None``。"""
    m = _SNAPSHOT_FILENAME_RE.match(name)
    if m is None:
        return None
    try:
        return date(int(m.group(1)[0:4]), int(m.group(1)[4:6]), int(m.group(1)[6:8]))
    except ValueError:
        return None


def _records_to_dataframe(records: tuple[CbbcRecord, ...]) -> "pd.DataFrame":
    """把 ``CbbcRecord`` 元组序列化成 DataFrame。``date`` 字段统一转为 ISO 字符串。"""
    pandas_mod = _require_pandas()
    rows: list[dict[str, object]] = []
    for r in records:
        rows.append(
            {
                "issuer": r.issuer,
                "code": r.code,
                "call_level": float(r.call_level),
                "outstanding_shares": float(r.outstanding_shares),
                "er_ratio": float(r.er_ratio),
                "direction": r.direction,
                "listing_date": r.listing_date.isoformat(),
                "maturity_date": r.maturity_date.isoformat(),
                "underlying": r.underlying,
                "snapshot_date": r.snapshot_date.isoformat(),
            }
        )
    return pandas_mod.DataFrame(rows, columns=list(_SNAPSHOT_COLUMNS))


def _dataframe_to_records(df: "pd.DataFrame") -> tuple[CbbcRecord, ...]:
    """把 DataFrame 反序列化成 ``CbbcRecord`` 元组（``date`` 字段从 ISO 字符串解析）。"""
    if df.empty:
        return ()
    records: list[CbbcRecord] = []
    for row in df.itertuples(index=False):
        direction = str(row.direction)
        if direction not in ("bull", "bear"):
            # 与任务 2.1 的合约一致：direction 必须是 bull / bear；脏数据直接跳过。
            continue
        records.append(
            CbbcRecord(
                issuer=str(row.issuer),
                code=str(row.code),
                call_level=float(row.call_level),
                outstanding_shares=float(row.outstanding_shares),
                er_ratio=float(row.er_ratio),
                direction=direction,  # type: ignore[arg-type]
                listing_date=date.fromisoformat(str(row.listing_date)),
                maturity_date=date.fromisoformat(str(row.maturity_date)),
                underlying=str(row.underlying),
                snapshot_date=date.fromisoformat(str(row.snapshot_date)),
            )
        )
    return tuple(records)


class CbbcStorage:
    """CBBC 历史快照的本地持久化层。

    职责：
    - parquet 读写（``backend/data/cbbc/outstanding_YYYYMMDD.parquet``）。
    - 生存偏差守卫：``read_snapshot(d)`` 仅返回 ``listing_date <= d`` 且 ``maturity_date >= d`` 的记录。
    - 原子写入：先写到临时文件再 ``os.replace`` 到目标路径，避免半文件。
    - 不可覆盖：目标 ``snapshot_date`` 已存在时抛 ``SnapshotError("snapshot_immutable")``。
    - ``reject_reverse_deduction`` 用作显式守卫，由 Research_Script / Backtest_Adapter 在检测到
      "今日存活集合反推历史" 的误用模式时主动调用。
    """

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else _DEFAULT_BASE_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 路径辅助
    # ------------------------------------------------------------------
    def _path_for(self, d: date) -> Path:
        return self.base_dir / f"outstanding_{_format_snapshot_date(d)}.parquet"

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def write_snapshot(self, snapshot: CbbcSnapshot) -> None:
        """把 ``snapshot`` 持久化到 parquet。

        - 若目标文件已存在 → 抛 ``SnapshotError("snapshot_immutable")``，绝不覆盖。
        - 写入采用 "临时文件 + 原子重命名"，避免半文件。
        """
        target = self._path_for(snapshot.snapshot_date)
        if target.exists():
            raise SnapshotError(
                "snapshot_immutable",
                f"snapshot for {snapshot.snapshot_date.isoformat()} already exists at {target}",
            )

        self.base_dir.mkdir(parents=True, exist_ok=True)
        df = _records_to_dataframe(snapshot.records)

        tmp_path = target.with_suffix(target.suffix + ".tmp")
        # 兜底：若上次写入异常残留 .tmp，先清理。
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

        df.to_parquet(tmp_path, engine="pyarrow", index=False)
        os.replace(tmp_path, target)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def read_snapshot(self, d: date) -> CbbcSnapshot:
        """读取交易日 ``d`` 的快照。

        生存偏差守卫：返回的 records 仅保留 ``listing_date <= d`` 且 ``maturity_date >= d``，
        即在 ``d`` 当日"还活着"的 CBBC。
        """
        if not is_hk_trading_day(d):
            raise SnapshotError("non_trading_day", f"{d.isoformat()} is not a HK trading day")

        path = self._path_for(d)
        if not path.exists():
            raise SnapshotError("snapshot_missing", f"no snapshot file at {path}")

        df = _require_pandas().read_parquet(path, engine="pyarrow")
        all_records = _dataframe_to_records(df)
        alive = tuple(
            r for r in all_records if r.listing_date <= d and r.maturity_date >= d
        )
        return CbbcSnapshot(snapshot_date=d, records=alive)

    def latest_before(self, d: date) -> CbbcSnapshot | None:
        """返回 ``snapshot_date`` 严格小于 ``d`` 且最近的存活快照；都不存在则返回 ``None``。

        用于每日抓取失败时的回退（保留上一交易日快照作为当前可用快照）。
        不对 ``d`` 做交易日校验：这是 fallback 用法，调用方可能传入任意日期。
        """
        candidates = [snap_d for snap_d in self.list_dates() if snap_d < d]
        if not candidates:
            return None
        latest = max(candidates)
        # 这里不能复用 ``read_snapshot``（它会做交易日校验，而历史已经是发布过的快照
        # 不需再校验）。直接读文件并按生存偏差规则过滤。
        path = self._path_for(latest)
        df = _require_pandas().read_parquet(path, engine="pyarrow")
        all_records = _dataframe_to_records(df)
        alive = tuple(
            r for r in all_records if r.listing_date <= latest and r.maturity_date >= latest
        )
        return CbbcSnapshot(snapshot_date=latest, records=alive)

    def list_dates(self) -> list[date]:
        """枚举本地所有快照文件的日期，升序返回。"""
        if not self.base_dir.exists():
            return []
        result: list[date] = []
        for entry in self.base_dir.iterdir():
            if not entry.is_file():
                continue
            parsed = _parse_snapshot_filename(entry.name)
            if parsed is not None:
                result.append(parsed)
        result.sort()
        return result

    # ------------------------------------------------------------------
    # 生存偏差守卫
    # ------------------------------------------------------------------
    def reject_reverse_deduction(self, *, today: date, requested: date) -> None:
        """生存偏差显式守卫。

        调用语义：当调用方（典型为 Research_Script / Backtest_Adapter）只持有"今日仍存活的
        CBBC 集合"，却试图把它当作历史日期 ``requested`` 的快照来用时，应在使用前调用本方法。

        - 若 ``requested != today``：视为对历史的反推，立即抛
          ``SnapshotError("no_reverse_deduction_allowed")``。
        - 若 ``requested == today``：合法（即调用方就是想用今日数据描述今日），不抛错。
        """
        if requested != today:
            raise SnapshotError(
                "no_reverse_deduction_allowed",
                (
                    f"refuse to use today's surviving CBBC set ({today.isoformat()}) "
                    f"to describe historical date {requested.isoformat()}"
                ),
            )
