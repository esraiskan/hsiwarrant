"""
Task 2.3 单元测试：``cbbc_storage.CbbcStorage`` parquet 读写、生存偏差守卫与不可覆盖语义。

覆盖：
- 成功写入与 round-trip（R3.1）
- 不可覆盖（R3.7）
- 缺失文件错误（R3.5）
- 非交易日错误（R3.6，含周末与港股公众假期）
- 生存偏差守卫（R3.3 — read 端只返回存活记录；磁盘上仍保留全部）
- ``latest_before`` 各类边界（含其自身的生存偏差过滤）
- ``reject_reverse_deduction``（R3.4）
- ``list_dates`` 排序与非匹配文件忽略
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from cbbc_storage import (
    CbbcRecord,
    CbbcSnapshot,
    CbbcStorage,
    SnapshotError,
    is_hk_trading_day,
)


# ---------------------------------------------------------------------------
# 测试夹具：固定的港股交易日（已通过 ``is_hk_trading_day`` 校验过）
# ---------------------------------------------------------------------------
# 这些日期都不是周末、不在 ``cbbc_storage`` 的港股公众假期清单内。
DATE_A = date(2025, 2, 3)   # Mon
DATE_B = date(2025, 2, 5)   # Wed
DATE_C = date(2025, 2, 7)   # Fri
DATE_D = date(2025, 2, 10)  # Mon (used as a "later" reference date)

# 非交易日：周末与已收录公众假期。
DATE_SAT = date(2025, 1, 4)        # Saturday
DATE_HOLIDAY = date(2025, 4, 4)    # 清明节 Ching Ming


def _make_record(
    code: str,
    *,
    direction: str,
    call_level: float,
    listing_date: date,
    maturity_date: date,
    snapshot_date: date,
    issuer: str = "TestIssuer",
    outstanding_shares: float = 1_000_000.0,
    er_ratio: float = 10000.0,
    underlying: str = "HSI",
) -> CbbcRecord:
    """构造一条 ``CbbcRecord`` 的测试夹具。所有字段都给了合理默认值。"""
    return CbbcRecord(
        issuer=issuer,
        code=code,
        call_level=float(call_level),
        outstanding_shares=float(outstanding_shares),
        er_ratio=float(er_ratio),
        direction=direction,  # type: ignore[arg-type]
        listing_date=listing_date,
        maturity_date=maturity_date,
        underlying=underlying,
        snapshot_date=snapshot_date,
    )


class TempPathMixin:
    """每个测试用例独立的临时目录，作为 ``CbbcStorage`` 的 ``base_dir``。"""

    def setUp(self) -> None:  # type: ignore[override]
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.storage = CbbcStorage(base_dir=self.tmp_dir)

    def tearDown(self) -> None:  # type: ignore[override]
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# Pre-flight：确认夹具日期与生产代码的交易日判断一致
# ---------------------------------------------------------------------------
class TradingDayFixtureSanityTests(unittest.TestCase):
    def test_chosen_dates_classify_correctly(self) -> None:
        for d in (DATE_A, DATE_B, DATE_C, DATE_D):
            self.assertTrue(is_hk_trading_day(d), f"{d} should be a trading day")
        self.assertFalse(is_hk_trading_day(DATE_SAT), "Saturday must not be trading day")
        self.assertFalse(
            is_hk_trading_day(DATE_HOLIDAY),
            "HK Ching Ming public holiday must not be trading day",
        )


# ---------------------------------------------------------------------------
# 1. 成功写入与 round-trip（R3.1）
# ---------------------------------------------------------------------------
class WriteAndRoundTripTests(TempPathMixin, unittest.TestCase):
    """R3.1：成功写入并能读回等价记录；文件名遵守 ``outstanding_YYYYMMDD.parquet``。"""

    def test_write_creates_file_with_expected_name(self) -> None:
        snap = CbbcSnapshot(
            snapshot_date=DATE_B,
            records=(
                _make_record(
                    "60001",
                    direction="bull",
                    call_level=20000.0,
                    listing_date=DATE_A,
                    maturity_date=DATE_C,
                    snapshot_date=DATE_B,
                ),
                _make_record(
                    "60002",
                    direction="bear",
                    call_level=22000.0,
                    listing_date=DATE_A,
                    maturity_date=DATE_C,
                    snapshot_date=DATE_B,
                ),
            ),
        )
        self.storage.write_snapshot(snap)
        target = self.tmp_dir / "outstanding_20250205.parquet"
        self.assertTrue(target.exists(), "snapshot file should land at expected path")

    def test_round_trip_returns_equivalent_records(self) -> None:
        # snapshot_date = DATE_B；所有记录的 [listing, maturity] 都覆盖 DATE_B，
        # 因此 read 端的生存偏差过滤不会丢任何记录。
        records = (
            _make_record(
                "60100",
                direction="bull",
                call_level=20000.0,
                listing_date=DATE_A,
                maturity_date=DATE_C,
                snapshot_date=DATE_B,
                issuer="IssuerOne",
                outstanding_shares=1_500_000.0,
                er_ratio=8000.0,
            ),
            _make_record(
                "60101",
                direction="bear",
                call_level=22500.5,
                listing_date=DATE_A,
                maturity_date=DATE_D,
                snapshot_date=DATE_B,
                issuer="IssuerTwo",
                outstanding_shares=2_000_000.0,
                er_ratio=12000.0,
            ),
            _make_record(
                "60102",
                direction="bull",
                call_level=19999.99,
                listing_date=DATE_A,
                maturity_date=DATE_D,
                snapshot_date=DATE_B,
            ),
        )
        snap = CbbcSnapshot(snapshot_date=DATE_B, records=records)
        self.storage.write_snapshot(snap)

        loaded = self.storage.read_snapshot(DATE_B)
        self.assertEqual(loaded.snapshot_date, DATE_B)
        # 数量与字段全等（顺序保持）。
        self.assertEqual(len(loaded.records), len(records))
        for original, restored in zip(records, loaded.records):
            self.assertEqual(restored, original)


# ---------------------------------------------------------------------------
# 2. 不可覆盖（R3.7）
# ---------------------------------------------------------------------------
class ImmutabilityTests(TempPathMixin, unittest.TestCase):
    """R3.7：同名 ``snapshot_date`` 的二次写入必须被拒绝；磁盘文件保持首次写入内容。"""

    def test_second_write_with_same_date_raises_immutable(self) -> None:
        first = CbbcSnapshot(
            snapshot_date=DATE_B,
            records=(
                _make_record(
                    "FIRST",
                    direction="bull",
                    call_level=20000.0,
                    listing_date=DATE_A,
                    maturity_date=DATE_C,
                    snapshot_date=DATE_B,
                ),
            ),
        )
        self.storage.write_snapshot(first)

        second = CbbcSnapshot(
            snapshot_date=DATE_B,
            records=(
                _make_record(
                    "SECOND",
                    direction="bear",
                    call_level=21000.0,
                    listing_date=DATE_A,
                    maturity_date=DATE_C,
                    snapshot_date=DATE_B,
                ),
                _make_record(
                    "THIRD",
                    direction="bull",
                    call_level=22000.0,
                    listing_date=DATE_A,
                    maturity_date=DATE_C,
                    snapshot_date=DATE_B,
                ),
            ),
        )
        with self.assertRaises(SnapshotError) as cm:
            self.storage.write_snapshot(second)
        self.assertEqual(cm.exception.code, "snapshot_immutable")

    def test_on_disk_content_unchanged_after_rejected_overwrite(self) -> None:
        first = CbbcSnapshot(
            snapshot_date=DATE_B,
            records=(
                _make_record(
                    "ORIGINAL",
                    direction="bull",
                    call_level=20000.0,
                    listing_date=DATE_A,
                    maturity_date=DATE_C,
                    snapshot_date=DATE_B,
                ),
            ),
        )
        self.storage.write_snapshot(first)

        # 第二次写入失败，但磁盘文件不应被破坏。
        second = CbbcSnapshot(
            snapshot_date=DATE_B,
            records=(
                _make_record(
                    "OVERWRITE_ATTEMPT",
                    direction="bear",
                    call_level=99999.0,
                    listing_date=DATE_A,
                    maturity_date=DATE_C,
                    snapshot_date=DATE_B,
                ),
            ),
        )
        with self.assertRaises(SnapshotError):
            self.storage.write_snapshot(second)

        loaded = self.storage.read_snapshot(DATE_B)
        self.assertEqual(len(loaded.records), 1)
        self.assertEqual(loaded.records[0].code, "ORIGINAL")
        self.assertEqual(loaded.records[0].call_level, 20000.0)


# ---------------------------------------------------------------------------
# 3. 缺失文件（R3.5）
# ---------------------------------------------------------------------------
class MissingFileTests(TempPathMixin, unittest.TestCase):
    def test_read_snapshot_missing_raises_snapshot_missing(self) -> None:
        # 交易日，但 ``base_dir`` 中没有对应文件。
        with self.assertRaises(SnapshotError) as cm:
            self.storage.read_snapshot(DATE_B)
        self.assertEqual(cm.exception.code, "snapshot_missing")


# ---------------------------------------------------------------------------
# 4. 非交易日（R3.6）
# ---------------------------------------------------------------------------
class NonTradingDayTests(TempPathMixin, unittest.TestCase):
    """R3.6：调用方传入周末或港股公众假期 → ``non_trading_day`` 错误。

    使用 ``is_hk_trading_day`` 自身校验夹具的非交易日属性，避免在测试里硬编码周几或假期清单。
    """

    def test_read_snapshot_on_weekend_rejected(self) -> None:
        # 前置：明确 DATE_SAT 不是交易日。
        self.assertFalse(is_hk_trading_day(DATE_SAT))
        with self.assertRaises(SnapshotError) as cm:
            self.storage.read_snapshot(DATE_SAT)
        self.assertEqual(cm.exception.code, "non_trading_day")

    def test_read_snapshot_on_public_holiday_rejected(self) -> None:
        self.assertFalse(is_hk_trading_day(DATE_HOLIDAY))
        with self.assertRaises(SnapshotError) as cm:
            self.storage.read_snapshot(DATE_HOLIDAY)
        self.assertEqual(cm.exception.code, "non_trading_day")


# ---------------------------------------------------------------------------
# 5. 生存偏差过滤（R3.3）
# ---------------------------------------------------------------------------
class SurvivorshipFilterTests(TempPathMixin, unittest.TestCase):
    """R3.3：``read_snapshot`` 仅返回 ``listing_date <= d`` 且 ``maturity_date >= d`` 的记录；
    磁盘文件保留全部记录。"""

    def test_read_filters_expired_and_future_records(self) -> None:
        d = DATE_B  # 关注日

        record_alive = _make_record(
            "ALIVE",
            direction="bull",
            call_level=20000.0,
            listing_date=DATE_A,    # < d
            maturity_date=DATE_C,   # > d
            snapshot_date=d,
        )
        record_expired = _make_record(
            "EXPIRED",
            direction="bear",
            call_level=21000.0,
            listing_date=date(2025, 1, 6),     # < d
            maturity_date=date(2025, 1, 31),   # < d，已到期
            snapshot_date=d,
        )
        record_future = _make_record(
            "FUTURE",
            direction="bull",
            call_level=22000.0,
            listing_date=DATE_C,    # > d，尚未上市
            maturity_date=DATE_D,
            snapshot_date=d,
        )
        snap = CbbcSnapshot(
            snapshot_date=d,
            records=(record_alive, record_expired, record_future),
        )
        self.storage.write_snapshot(snap)

        loaded = self.storage.read_snapshot(d)
        self.assertEqual(len(loaded.records), 1)
        self.assertEqual(loaded.records[0].code, "ALIVE")

    def test_on_disk_file_retains_all_records(self) -> None:
        d = DATE_B
        records = (
            _make_record(
                "ALIVE",
                direction="bull",
                call_level=20000.0,
                listing_date=DATE_A,
                maturity_date=DATE_C,
                snapshot_date=d,
            ),
            _make_record(
                "EXPIRED",
                direction="bear",
                call_level=21000.0,
                listing_date=date(2025, 1, 6),
                maturity_date=date(2025, 1, 31),
                snapshot_date=d,
            ),
            _make_record(
                "FUTURE",
                direction="bull",
                call_level=22000.0,
                listing_date=DATE_C,
                maturity_date=DATE_D,
                snapshot_date=d,
            ),
        )
        self.storage.write_snapshot(CbbcSnapshot(snapshot_date=d, records=records))

        # 直接读 parquet：filter 仅作用于读路径，不应改变持久化内容。
        path = self.tmp_dir / "outstanding_20250205.parquet"
        self.assertTrue(path.exists())
        df = pd.read_parquet(path, engine="pyarrow")
        self.assertEqual(len(df), 3)
        self.assertEqual(set(df["code"].tolist()), {"ALIVE", "EXPIRED", "FUTURE"})


# ---------------------------------------------------------------------------
# 6. ``latest_before`` 边界
# ---------------------------------------------------------------------------
class LatestBeforeTests(TempPathMixin, unittest.TestCase):
    def test_no_files_returns_none(self) -> None:
        self.assertIsNone(self.storage.latest_before(DATE_C))
        # 任意日期都返回 None。
        self.assertIsNone(self.storage.latest_before(DATE_A))
        self.assertIsNone(self.storage.latest_before(DATE_HOLIDAY))

    def test_strictly_less_than_returns_none_when_only_target_present(self) -> None:
        self.storage.write_snapshot(
            CbbcSnapshot(
                snapshot_date=DATE_A,
                records=(
                    _make_record(
                        "X",
                        direction="bull",
                        call_level=20000.0,
                        listing_date=DATE_A,
                        maturity_date=DATE_D,
                        snapshot_date=DATE_A,
                    ),
                ),
            )
        )
        # ``latest_before`` 是严格小于：传入 A 自身应返回 None。
        self.assertIsNone(self.storage.latest_before(DATE_A))

    def test_returns_snapshot_for_a_when_querying_a_plus_one_day(self) -> None:
        self.storage.write_snapshot(
            CbbcSnapshot(
                snapshot_date=DATE_A,
                records=(
                    _make_record(
                        "X",
                        direction="bull",
                        call_level=20000.0,
                        listing_date=DATE_A,
                        maturity_date=DATE_D,
                        snapshot_date=DATE_A,
                    ),
                ),
            )
        )
        result = self.storage.latest_before(DATE_A + timedelta(days=1))
        self.assertIsNotNone(result)
        assert result is not None  # narrow for the type checker
        self.assertEqual(result.snapshot_date, DATE_A)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].code, "X")

    def test_picks_latest_among_multiple_snapshots(self) -> None:
        for d in (DATE_A, DATE_B, DATE_C):
            self.storage.write_snapshot(
                CbbcSnapshot(
                    snapshot_date=d,
                    records=(
                        _make_record(
                            f"CODE_{d.isoformat()}",
                            direction="bull",
                            call_level=20000.0,
                            listing_date=DATE_A,
                            maturity_date=DATE_D,
                            snapshot_date=d,
                        ),
                    ),
                )
            )

        # D > C → 取 C
        beyond_c = self.storage.latest_before(DATE_D)
        self.assertIsNotNone(beyond_c)
        assert beyond_c is not None
        self.assertEqual(beyond_c.snapshot_date, DATE_C)

        # 严格小于 B → 取 A
        before_b = self.storage.latest_before(DATE_B)
        self.assertIsNotNone(before_b)
        assert before_b is not None
        self.assertEqual(before_b.snapshot_date, DATE_A)

        # 严格小于 A → None
        self.assertIsNone(self.storage.latest_before(DATE_A))

    def test_latest_before_applies_survivorship_filter_on_its_own_date(self) -> None:
        # 在 ``DATE_A`` 写入混合记录：一条在 A 当日仍存活，另一条在 A 之前已到期。
        # ``latest_before(DATE_B)`` 命中 A，应只返回存活记录。
        records = (
            _make_record(
                "ALIVE_AT_A",
                direction="bull",
                call_level=20000.0,
                listing_date=date(2025, 1, 6),
                maturity_date=DATE_D,             # >= A
                snapshot_date=DATE_A,
            ),
            _make_record(
                "EXPIRED_BY_A",
                direction="bear",
                call_level=21000.0,
                listing_date=date(2025, 1, 6),
                maturity_date=date(2025, 1, 31),  # < A，已到期
                snapshot_date=DATE_A,
            ),
        )
        self.storage.write_snapshot(
            CbbcSnapshot(snapshot_date=DATE_A, records=records)
        )

        result = self.storage.latest_before(DATE_B)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.snapshot_date, DATE_A)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].code, "ALIVE_AT_A")


# ---------------------------------------------------------------------------
# 7. ``reject_reverse_deduction``（R3.4）
# ---------------------------------------------------------------------------
class ReverseDeductionGuardTests(TempPathMixin, unittest.TestCase):
    def test_same_date_does_not_raise(self) -> None:
        # 不抛错。
        self.storage.reject_reverse_deduction(today=DATE_A, requested=DATE_A)

    def test_different_dates_raise_no_reverse_deduction(self) -> None:
        with self.assertRaises(SnapshotError) as cm:
            self.storage.reject_reverse_deduction(today=DATE_A, requested=DATE_B)
        self.assertEqual(cm.exception.code, "no_reverse_deduction_allowed")

    def test_requested_in_the_past_also_rejected(self) -> None:
        # 即便 requested 在过去也属于反推，应被拒绝。
        with self.assertRaises(SnapshotError) as cm:
            self.storage.reject_reverse_deduction(today=DATE_C, requested=DATE_A)
        self.assertEqual(cm.exception.code, "no_reverse_deduction_allowed")


# ---------------------------------------------------------------------------
# 8. ``list_dates``
# ---------------------------------------------------------------------------
class ListDatesTests(TempPathMixin, unittest.TestCase):
    def test_returns_sorted_ascending(self) -> None:
        # 写入顺序故意打乱。
        for d in (DATE_C, DATE_A, DATE_B):
            self.storage.write_snapshot(
                CbbcSnapshot(
                    snapshot_date=d,
                    records=(
                        _make_record(
                            "X",
                            direction="bull",
                            call_level=20000.0,
                            listing_date=DATE_A,
                            maturity_date=DATE_D,
                            snapshot_date=d,
                        ),
                    ),
                )
            )
        self.assertEqual(self.storage.list_dates(), [DATE_A, DATE_B, DATE_C])

    def test_ignores_non_matching_files(self) -> None:
        # 写一个合法 snapshot，再放几个不匹配命名规则的文件。
        self.storage.write_snapshot(
            CbbcSnapshot(
                snapshot_date=DATE_A,
                records=(
                    _make_record(
                        "X",
                        direction="bull",
                        call_level=20000.0,
                        listing_date=DATE_A,
                        maturity_date=DATE_D,
                        snapshot_date=DATE_A,
                    ),
                ),
            )
        )

        # 几种不应被识别的文件名：扩展名错、前缀错、日期段非法、目录而非文件。
        (self.tmp_dir / "random.parquet").write_bytes(b"")
        (self.tmp_dir / "outstanding.parquet").write_bytes(b"")
        (self.tmp_dir / "outstanding_2025-02-03.parquet").write_bytes(b"")
        (self.tmp_dir / "outstanding_20250203.txt").write_bytes(b"")
        (self.tmp_dir / "outstanding_invalid.parquet").write_bytes(b"")
        (self.tmp_dir / "subdir").mkdir()

        self.assertEqual(self.storage.list_dates(), [DATE_A])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
