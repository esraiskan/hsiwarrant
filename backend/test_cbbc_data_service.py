"""Unit tests for CbbcDataService + IntradayPoller (cbbc-magnet-signal task 4.6)."""
from __future__ import annotations

import asyncio
import sys
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def _ensure(name: str) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


def _try_real_import(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _stub_if_unavailable(name: str, attrs: dict) -> None:
    if _try_real_import(name):
        return
    mod = _ensure(name)
    for k, v in attrs.items():
        setattr(mod, k, v)


_stub_if_unavailable("pandas", {"DataFrame": type("DataFrame", (), {})})
_stub_if_unavailable("numpy", {"nan": float("nan")})

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cbbc_data import (  # noqa: E402
    CbbcDataError,
    CbbcDataService,
    IntradayPoller,
    is_in_intraday_session,
)
from cbbc_storage import CbbcRecord, CbbcSnapshot  # noqa: E402

_HK = ZoneInfo("Asia/Hong_Kong")


def _hk(*args) -> datetime:
    return datetime(*args, tzinfo=_HK)


def _record(code: str, direction: str, listing_d: date) -> CbbcRecord:
    return CbbcRecord(
        issuer="HSBC", code=code, call_level=20100.0,
        outstanding_shares=1_000_000.0, er_ratio=10000.0,
        direction=direction, listing_date=listing_d,
        maturity_date=date(2030, 12, 31),
        underlying="HSI", snapshot_date=listing_d,
    )


class _StubResponse:
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self._body = body

    @property
    def text(self) -> str:
        return self._body

    @property
    def content(self) -> bytes:
        return self._body.encode("utf-8")


class _StubClient:
    """Replaces RateLimitedClient with deterministic fake responses."""

    def __init__(self) -> None:
        self.responses: list = []
        self.calls: list[str] = []

    def queue(self, response_or_exc) -> None:
        self.responses.append(response_or_exc)

    async def get(self, url, *, headers=None, timeout):
        self.calls.append(url)
        if not self.responses:
            raise RuntimeError("no fake response queued")
        next_value = self.responses.pop(0)
        if isinstance(next_value, BaseException):
            raise next_value
        return next_value

    async def aclose(self) -> None:
        pass


class _RuntimeConfigStub:
    def __init__(self, *, suspended: bool = False) -> None:
        self.suspended = suspended
        self.history: list[bool] = []

    def is_intraday_polling_suspended(self) -> bool:
        return self.suspended

    def set_intraday_polling_suspended(self, value: bool) -> None:
        self.suspended = bool(value)
        self.history.append(self.suspended)


# Pre-built CSV fragments used by several poll tests.
_CSV_HEADER = (
    "issuer,code,callprice,outstanding,erratio,bullbear,"
    "listingdate,maturitydate,underlying\n"
)


def _csv_row(code: str, direction: str, listing: str) -> str:
    return (
        f"HSBC,{code},20100.0,1000000,10000,{direction},"
        f"{listing},2030-12-31,HSI\n"
    )


def _trading_day_morning_ts() -> datetime:
    # 2025-01-06 is a Monday and a trading day.
    return _hk(2025, 1, 6, 10, 0)


# --------------------------------------------------------------------------- #
# is_in_intraday_session
# --------------------------------------------------------------------------- #


class IntradaySessionWindowTest(unittest.TestCase):
    def test_morning(self) -> None:
        self.assertTrue(is_in_intraday_session(_hk(2025, 1, 6, 10, 0)))
        self.assertTrue(is_in_intraday_session(_hk(2025, 1, 6, 12, 0)))

    def test_afternoon(self) -> None:
        self.assertTrue(is_in_intraday_session(_hk(2025, 1, 6, 13, 0)))
        self.assertTrue(is_in_intraday_session(_hk(2025, 1, 6, 16, 0)))

    def test_lunch_break(self) -> None:
        self.assertFalse(is_in_intraday_session(_hk(2025, 1, 6, 12, 30)))

    def test_after_close(self) -> None:
        self.assertFalse(is_in_intraday_session(_hk(2025, 1, 6, 16, 30)))


# --------------------------------------------------------------------------- #
# IntradayPoller
# --------------------------------------------------------------------------- #


def _build_poller(
    *,
    client: _StubClient,
    runtime: _RuntimeConfigStub,
    snapshot_view=lambda: None,
    on_records_merged=lambda snap: None,
    clock_now=_trading_day_morning_ts(),
    degrade_threshold: float = 5 * 60.0,
):
    return IntradayPoller(
        client=client,  # type: ignore[arg-type]
        snapshot_view=snapshot_view,
        on_records_merged=on_records_merged,
        runtime_config=runtime,  # type: ignore[arg-type]
        clock_hk=lambda: clock_now,
        degrade_threshold_seconds=degrade_threshold,
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class IntradayPollerOutcomeTest(unittest.TestCase):
    def test_out_of_session(self) -> None:
        client = _StubClient()
        runtime = _RuntimeConfigStub()
        poller = _build_poller(
            client=client, runtime=runtime, clock_now=_hk(2025, 1, 6, 12, 30)
        )
        outcome = _run(poller.poll_once())
        self.assertEqual(outcome, "out_of_session")
        self.assertEqual(client.calls, [])

    def test_non_trading_day(self) -> None:
        # 2025-01-04 is a Saturday.
        client = _StubClient()
        runtime = _RuntimeConfigStub()
        poller = _build_poller(
            client=client, runtime=runtime, clock_now=_hk(2025, 1, 4, 10, 0)
        )
        outcome = _run(poller.poll_once())
        self.assertEqual(outcome, "non_trading_day")
        self.assertEqual(client.calls, [])


    def test_suspended_returns_suspended(self) -> None:
        client = _StubClient()
        runtime = _RuntimeConfigStub(suspended=True)
        poller = _build_poller(client=client, runtime=runtime)
        outcome = _run(poller.poll_once())
        self.assertEqual(outcome, "suspended")
        self.assertEqual(client.calls, [])

    def test_blocked_endpoint_records_failure(self) -> None:
        client = _StubClient()
        client.queue(CbbcDataError(
            "blocked_non_whitelisted_endpoint",
            url="x", reason="non_whitelisted_host",
        ))
        runtime = _RuntimeConfigStub()
        poller = _build_poller(client=client, runtime=runtime)
        outcome = _run(poller.poll_once())
        self.assertEqual(outcome, "blocked_endpoint")

    def test_http_status_failure(self) -> None:
        client = _StubClient()
        client.queue(_StubResponse(503, ""))
        runtime = _RuntimeConfigStub()
        poller = _build_poller(client=client, runtime=runtime)
        outcome = _run(poller.poll_once())
        self.assertEqual(outcome, "http_status")

    def test_5min_failure_window_triggers_suspended(self) -> None:
        client = _StubClient()
        runtime = _RuntimeConfigStub()
        # Tiny threshold so the very next failure is already past the
        # degrade window — unit-friendly substitute for the real 5-minute
        # window without sleeping.
        poller = _build_poller(
            client=client, runtime=runtime, degrade_threshold=0.05
        )
        client.queue(_StubResponse(500, ""))
        _run(poller.poll_once())
        # Sleep past the threshold so the next failure crosses the boundary.
        import time
        time.sleep(0.1)
        client.queue(_StubResponse(500, ""))
        _run(poller.poll_once())
        self.assertTrue(runtime.suspended)


class IntradayPollerMergeTest(unittest.TestCase):
    def test_merges_new_listing_today_when_csv_returned(self) -> None:
        today = date(2025, 1, 6)
        body = _CSV_HEADER + _csv_row("HK.50001", "bull", today.isoformat())
        client = _StubClient()
        client.queue(_StubResponse(200, body))
        runtime = _RuntimeConfigStub()

        merged_calls = []

        def on_merged(snap):
            merged_calls.append(snap)

        poller = _build_poller(
            client=client, runtime=runtime,
            snapshot_view=lambda: CbbcSnapshot(snapshot_date=today, records=()),
            on_records_merged=on_merged,
            clock_now=_hk(2025, 1, 6, 10, 30),
        )
        outcome = _run(poller.poll_once())
        self.assertTrue(outcome.startswith("ok:merged="))
        self.assertEqual(len(merged_calls), 1)
        self.assertEqual(len(merged_calls[0].records), 1)

    def test_drops_records_listed_yesterday(self) -> None:
        # listing_date != today should not be merged (R2.2).
        today = date(2025, 1, 6)
        body = _CSV_HEADER + _csv_row("HK.50001", "bull", "2024-12-31")
        client = _StubClient()
        client.queue(_StubResponse(200, body))
        runtime = _RuntimeConfigStub()

        merged_calls = []
        poller = _build_poller(
            client=client, runtime=runtime,
            snapshot_view=lambda: CbbcSnapshot(snapshot_date=today, records=()),
            on_records_merged=lambda s: merged_calls.append(s),
            clock_now=_hk(2025, 1, 6, 10, 30),
        )
        outcome = _run(poller.poll_once())
        self.assertEqual(outcome, "ok:merged=0")
        self.assertEqual(merged_calls, [])

    def test_duplicate_code_not_merged_twice(self) -> None:
        today = date(2025, 1, 6)
        existing = (_record("HK.50001", "bull", today),)
        body = _CSV_HEADER + _csv_row("HK.50001", "bull", today.isoformat())
        client = _StubClient()
        client.queue(_StubResponse(200, body))
        runtime = _RuntimeConfigStub()

        merged_calls = []
        poller = _build_poller(
            client=client, runtime=runtime,
            snapshot_view=lambda: CbbcSnapshot(snapshot_date=today, records=existing),
            on_records_merged=lambda s: merged_calls.append(s),
            clock_now=_hk(2025, 1, 6, 10, 30),
        )
        outcome = _run(poller.poll_once())
        self.assertEqual(outcome, "ok:merged=0")
        self.assertEqual(merged_calls, [])


# --------------------------------------------------------------------------- #
# CbbcDataService minimal lifecycle wiring
# --------------------------------------------------------------------------- #


class _StorageStub:
    def __init__(self) -> None:
        self.snapshots = {}
        self.latest = None

    def read_snapshot(self, d):
        from cbbc_storage import SnapshotError
        if d in self.snapshots:
            return self.snapshots[d]
        raise SnapshotError("snapshot_missing", "x")

    def latest_before(self, d):
        return self.latest


class CbbcDataServiceTest(unittest.TestCase):
    def test_current_snapshot_empty_initially(self) -> None:
        runtime = _RuntimeConfigStub()
        svc = CbbcDataService(
            storage=_StorageStub(),
            runtime_config=runtime,
            client=_StubClient(),  # type: ignore[arg-type]
            clock_hk=lambda: _hk(2025, 1, 6, 14, 0),
        )
        self.assertIsNone(svc.current_snapshot())
        self.assertFalse(svc.is_intraday_polling_suspended())

    def test_is_intraday_polling_suspended_reflects_runtime(self) -> None:
        runtime = _RuntimeConfigStub(suspended=True)
        svc = CbbcDataService(
            storage=_StorageStub(),
            runtime_config=runtime,
            client=_StubClient(),  # type: ignore[arg-type]
            clock_hk=lambda: _hk(2025, 1, 6, 14, 0),
        )
        self.assertTrue(svc.is_intraday_polling_suspended())

    def test_reload_snapshot_uses_latest_before_when_today_missing(self) -> None:
        runtime = _RuntimeConfigStub()
        storage = _StorageStub()
        latest = CbbcSnapshot(
            snapshot_date=date(2025, 1, 3),
            records=(_record("HK.51000", "bull", date(2025, 1, 3)),),
        )
        storage.latest = latest
        observed = []
        svc = CbbcDataService(
            storage=storage,
            runtime_config=runtime,
            client=_StubClient(),  # type: ignore[arg-type]
            clock_hk=lambda: _hk(2025, 1, 6, 14, 0),
            on_snapshot_changed=lambda snap: observed.append(snap),
        )
        svc._reload_snapshot_for_today()
        self.assertEqual(svc.current_snapshot(), latest)
        self.assertEqual(observed, [latest])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
