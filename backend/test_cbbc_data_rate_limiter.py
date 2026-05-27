"""
Task 4.1 单元测试：RateLimitedClient + 白名单 / 鉴权 / 限频守卫。

不依赖 httpx：使用注入的 fake HttpBackend。
"""
from __future__ import annotations

import asyncio
import logging
import unittest
from dataclasses import dataclass
from typing import Mapping

from cbbc_data import (
    CbbcDataError,
    HKEX_WHITELIST_HOSTS,
    HttpBackend,
    HttpResponse,
    RateLimitedClient,
)


@dataclass
class FakeResponse:
    status_code: int = 200
    text: str = ""
    content: bytes = b""


class FakeBackend:
    """记录所有 GET 调用，并返回预先排队的响应。"""

    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.calls: list[tuple[str, Mapping[str, str] | None, float]] = []
        self._responses: list[FakeResponse] = list(responses or [])
        self.closed = False

    def queue(self, response: FakeResponse) -> None:
        self._responses.append(response)

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        timeout: float,
    ) -> HttpResponse:
        self.calls.append((url, dict(headers) if headers else None, timeout))
        if self._responses:
            return self._responses.pop(0)
        return FakeResponse()

    async def aclose(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t: float = start

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _AsyncTestCase(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def setUp(self) -> None:
        self.handler = CapturingHandler()
        self.logger = logging.getLogger("cbbc_data")
        self._prev_propagate = self.logger.propagate
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

    def tearDown(self) -> None:
        self.logger.removeHandler(self.handler)
        self.logger.propagate = self._prev_propagate

    def events(self) -> list[str]:
        return [getattr(r, "event", None) for r in self.handler.records]


class WhitelistGuardTests(_AsyncTestCase):
    def test_allows_whitelisted_https_host(self) -> None:
        backend = FakeBackend([FakeResponse(status_code=200)])
        client = RateLimitedClient(http=backend)

        async def go():
            return await client.get(
                "https://www.hkex.com.hk/eng/cbbc/some.csv",
                timeout=5.0,
            )

        resp = self.run_async(go())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(backend.calls), 1)
        self.assertNotIn(
            "blocked_non_whitelisted_endpoint",
            self.events(),
        )

    def test_blocks_non_whitelisted_host(self) -> None:
        backend = FakeBackend([FakeResponse()])
        client = RateLimitedClient(http=backend)

        async def go():
            await client.get("https://evil.example.com/foo", timeout=5.0)

        with self.assertRaises(CbbcDataError) as ctx:
            self.run_async(go())
        self.assertEqual(ctx.exception.code, "blocked_non_whitelisted_endpoint")
        self.assertEqual(backend.calls, [])  # no HTTP request issued
        self.assertIn("blocked_non_whitelisted_endpoint", self.events())

    def test_blocks_non_https_scheme_even_for_whitelisted_host(self) -> None:
        backend = FakeBackend([FakeResponse()])
        client = RateLimitedClient(http=backend)

        async def go():
            await client.get("http://www.hkex.com.hk/path", timeout=5.0)

        with self.assertRaises(CbbcDataError) as ctx:
            self.run_async(go())
        self.assertEqual(ctx.exception.code, "blocked_non_whitelisted_endpoint")
        self.assertEqual(backend.calls, [])

    def test_blocks_other_whitelisted_subdomain_not_listed(self) -> None:
        backend = FakeBackend([FakeResponse()])
        client = RateLimitedClient(http=backend)

        async def go():
            # www2.hkexnews.hk is NOT on the whitelist.
            await client.get("https://www2.hkexnews.hk/x.csv", timeout=5.0)

        with self.assertRaises(CbbcDataError):
            self.run_async(go())

    def test_whitelist_set_constants(self) -> None:
        # Sanity check the constant doesn't drift.
        self.assertEqual(
            HKEX_WHITELIST_HOSTS,
            frozenset({"www.hkex.com.hk", "www1.hkexnews.hk"}),
        )


class AuthGuardTests(_AsyncTestCase):
    def test_blocks_request_with_cookie_header(self) -> None:
        backend = FakeBackend([FakeResponse()])
        client = RateLimitedClient(http=backend)

        async def go():
            await client.get(
                "https://www.hkex.com.hk/x",
                headers={"Cookie": "SESSION=secret-do-not-log"},
                timeout=5.0,
            )

        with self.assertRaises(CbbcDataError) as ctx:
            self.run_async(go())
        self.assertEqual(
            ctx.exception.code,
            "blocked_paywalled_or_authenticated_endpoint",
        )
        self.assertEqual(backend.calls, [])
        # Make sure no log record contains the credential value anywhere.
        for rec in self.handler.records:
            for value in rec.__dict__.values():
                self.assertNotIn("secret-do-not-log", str(value))

    def test_blocks_request_with_authorization_header_case_insensitive(self) -> None:
        backend = FakeBackend([FakeResponse()])
        client = RateLimitedClient(http=backend)

        async def go():
            await client.get(
                "https://www.hkex.com.hk/x",
                headers={"authorization": "Bearer xyz"},
                timeout=5.0,
            )

        with self.assertRaises(CbbcDataError) as ctx:
            self.run_async(go())
        self.assertEqual(
            ctx.exception.code,
            "blocked_paywalled_or_authenticated_endpoint",
        )

    def test_blocks_request_with_api_key_header(self) -> None:
        backend = FakeBackend([FakeResponse()])
        client = RateLimitedClient(http=backend)

        async def go():
            await client.get(
                "https://www.hkex.com.hk/x",
                headers={"X-API-Key": "abc"},
                timeout=5.0,
            )

        with self.assertRaises(CbbcDataError):
            self.run_async(go())

    def test_blocks_when_response_is_401(self) -> None:
        backend = FakeBackend([FakeResponse(status_code=401)])
        client = RateLimitedClient(http=backend)

        async def go():
            await client.get("https://www.hkex.com.hk/x", timeout=5.0)

        with self.assertRaises(CbbcDataError) as ctx:
            self.run_async(go())
        self.assertEqual(
            ctx.exception.code,
            "blocked_paywalled_or_authenticated_endpoint",
        )
        # The HTTP backend was actually called once (response-time check).
        self.assertEqual(len(backend.calls), 1)

    def test_blocks_when_response_is_403(self) -> None:
        backend = FakeBackend([FakeResponse(status_code=403)])
        client = RateLimitedClient(http=backend)

        async def go():
            await client.get("https://www.hkex.com.hk/x", timeout=5.0)

        with self.assertRaises(CbbcDataError) as ctx:
            self.run_async(go())
        self.assertEqual(
            ctx.exception.code,
            "blocked_paywalled_or_authenticated_endpoint",
        )

    def test_allows_request_with_only_benign_headers(self) -> None:
        backend = FakeBackend([FakeResponse(status_code=200)])
        client = RateLimitedClient(http=backend)

        async def go():
            return await client.get(
                "https://www.hkex.com.hk/x",
                headers={"User-Agent": "cbbc-magnet/1.0", "Accept": "text/csv"},
                timeout=5.0,
            )

        resp = self.run_async(go())
        self.assertEqual(resp.status_code, 200)


class RateLimitTests(_AsyncTestCase):
    def _make_client(
        self,
        backend: FakeBackend,
        clock: FakeClock,
        slept: list[float],
    ) -> RateLimitedClient:
        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            clock.advance(seconds)

        return RateLimitedClient(
            http=backend,
            max_per_window=6,
            window_seconds=60.0,
            clock=clock.now,
            sleep=fake_sleep,
        )

    def test_allows_six_requests_within_window_without_sleep(self) -> None:
        backend = FakeBackend([FakeResponse() for _ in range(6)])
        clock = FakeClock()
        slept: list[float] = []
        client = self._make_client(backend, clock, slept)

        async def go():
            for i in range(6):
                await client.get("https://www.hkex.com.hk/x", timeout=5.0)
                clock.advance(1.0)  # 1s between requests

        self.run_async(go())
        self.assertEqual(slept, [])
        self.assertEqual(len(backend.calls), 6)
        self.assertNotIn("rate_limit_deferred", self.events())

    def test_seventh_request_in_same_window_is_deferred(self) -> None:
        backend = FakeBackend([FakeResponse() for _ in range(7)])
        clock = FakeClock(start=1000.0)
        slept: list[float] = []
        client = self._make_client(backend, clock, slept)

        async def go():
            # Fire 6 requests at t=1000..1005 (Δ=1s).
            for _ in range(6):
                await client.get("https://www.hkex.com.hk/x", timeout=5.0)
                clock.advance(1.0)
            # Now t=1006. Oldest = 1000 → must wait until 1060 → 54s defer.
            await client.get("https://www.hkex.com.hk/x", timeout=5.0)

        self.run_async(go())

        # Exactly one sleep, ~= 54s.
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 54.0, places=3)
        self.assertEqual(len(backend.calls), 7)
        self.assertIn("rate_limit_deferred", self.events())

        # Verify the WARN log carries the relative defer seconds.
        warns = [
            r for r in self.handler.records
            if getattr(r, "event", None) == "rate_limit_deferred"
        ]
        self.assertEqual(len(warns), 1)
        self.assertAlmostEqual(warns[0].deferred_seconds, 54.0, places=3)
        self.assertEqual(warns[0].url, "https://www.hkex.com.hk/x")

    def test_rate_limit_is_per_url(self) -> None:
        backend = FakeBackend([FakeResponse() for _ in range(12)])
        clock = FakeClock()
        slept: list[float] = []
        client = self._make_client(backend, clock, slept)

        async def go():
            # 6 to URL A then 6 to URL B within the same window must NOT defer.
            for _ in range(6):
                await client.get("https://www.hkex.com.hk/a", timeout=5.0)
            for _ in range(6):
                await client.get("https://www.hkex.com.hk/b", timeout=5.0)

        self.run_async(go())
        self.assertEqual(slept, [])
        self.assertEqual(len(backend.calls), 12)

    def test_old_timestamps_are_pruned(self) -> None:
        backend = FakeBackend([FakeResponse() for _ in range(7)])
        clock = FakeClock(start=0.0)
        slept: list[float] = []
        client = self._make_client(backend, clock, slept)

        async def go():
            # 6 requests at t=0..5.
            for _ in range(6):
                await client.get("https://www.hkex.com.hk/x", timeout=5.0)
                clock.advance(1.0)
            # Jump past 60s so all old entries fall out of the window.
            clock.advance(60.0)
            # 7th request should NOT be deferred.
            await client.get("https://www.hkex.com.hk/x", timeout=5.0)

        self.run_async(go())
        self.assertEqual(slept, [])
        self.assertEqual(len(backend.calls), 7)

    def test_blocked_request_does_not_consume_rate_budget(self) -> None:
        backend = FakeBackend([FakeResponse() for _ in range(6)])
        clock = FakeClock()
        slept: list[float] = []
        client = self._make_client(backend, clock, slept)

        async def go():
            # Whitelist-blocked URL should not occupy a slot.
            for _ in range(3):
                with self.assertRaises(CbbcDataError):
                    await client.get("https://evil.example.com/x", timeout=5.0)
            # Now we should still be able to fire 6 to a real URL.
            for _ in range(6):
                await client.get("https://www.hkex.com.hk/x", timeout=5.0)

        self.run_async(go())
        self.assertEqual(slept, [])
        self.assertEqual(len(backend.calls), 6)


class CloseTests(_AsyncTestCase):
    def test_aclose_delegates_to_backend(self) -> None:
        backend = FakeBackend()
        client = RateLimitedClient(http=backend)

        async def go():
            await client.aclose()

        self.run_async(go())
        self.assertTrue(backend.closed)

    def test_async_context_manager_closes(self) -> None:
        backend = FakeBackend([FakeResponse()])

        async def go():
            async with RateLimitedClient(http=backend) as client:
                await client.get("https://www.hkex.com.hk/x", timeout=5.0)

        self.run_async(go())
        self.assertTrue(backend.closed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
