"""Tests for cadence.collectors.base (WP-02 acceptance)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from cadence.collectors import (
    BaseCollector,
    CachedResponse,
    CollectionResult,
    DiskCache,
    HTTPClient,
    RateLimiter,
)
from cadence.config import Settings


def _settings(tmp_path: Path, *, rate: float = 0.0) -> Settings:
    """Build a Settings pinned to tmp_path with rate limiting off by default.

    rate=0 disables the limiter, which is what we want for any test that
    isn't specifically asserting throttling behavior.
    """
    return Settings(
        cache_dir=tmp_path / "cache",
        db_path=tmp_path / "cadence.db",
        rate_limit_per_host=rate,
    )


class EchoCollector(BaseCollector):
    """Sample collector. Fetches one URL and returns its body length."""

    name = "echo"

    async def collect(  # type: ignore[override]
        self, *, url: str, ttl_seconds: int | None = None, **_: Any
    ) -> CollectionResult:
        started = datetime.now(UTC)
        response = await self.client.get(url, ttl_seconds=ttl_seconds)
        completed = datetime.now(UTC)
        self.log.info("echoed", url=url, bytes=len(response.content))
        return CollectionResult(
            name=self.name,
            started_at=started,
            completed_at=completed,
            records=len(response.content),
        )


# ----------------------------------------------------------------------
# DiskCache
# ----------------------------------------------------------------------


def test_disk_cache_round_trip(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path)
    key = DiskCache.key_for("GET", "https://example.invalid/x")
    payload = CachedResponse(
        url="https://example.invalid/x",
        status_code=200,
        headers={"content-type": "application/json"},
        content=b'{"ok":true}',
        fetched_at=datetime.now(UTC),
        ttl_seconds=60,
    )
    cache.put(key, payload)
    got = cache.get(key)
    assert got is not None
    assert got.content == payload.content
    assert got.headers["content-type"] == "application/json"
    assert got.from_cache is True


def test_disk_cache_expires(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path)
    key = "deadbeef" * 8
    expired = CachedResponse(
        url="https://example.invalid/expired",
        status_code=200,
        headers={},
        content=b"old",
        fetched_at=datetime.fromtimestamp(time.time() - 3600, tz=UTC),
        ttl_seconds=60,
    )
    cache.put(key, expired)
    assert cache.get(key) is None


def test_disk_cache_corrupt_file_returns_none(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path)
    key = "cafebabe" * 8
    path = cache._path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json{{{")
    assert cache.get(key) is None


# ----------------------------------------------------------------------
# RateLimiter
# ----------------------------------------------------------------------


def test_rate_limiter_throttles_same_host() -> None:
    """Three back-to-back acquisitions at 10 rps should take ~0.2s."""
    limiter = RateLimiter(per_host_rps=10.0)  # 100ms interval

    async def burst() -> float:
        t0 = time.monotonic()
        for _ in range(3):
            await limiter.acquire("a.example")
        return time.monotonic() - t0

    elapsed = asyncio.run(burst())
    # With 100ms interval and 3 calls, the spacing forces ~200ms min total.
    # Allow some slack but assert real throttling.
    assert 0.15 < elapsed < 0.6, f"expected ~0.2s, got {elapsed:.3f}s"


def test_rate_limiter_independent_hosts() -> None:
    """Different hosts must not block each other."""
    limiter = RateLimiter(per_host_rps=10.0)

    async def two_hosts() -> float:
        t0 = time.monotonic()
        await limiter.acquire("a.example")
        await limiter.acquire("b.example")
        return time.monotonic() - t0

    elapsed = asyncio.run(two_hosts())
    assert elapsed < 0.05, f"hosts blocked each other: {elapsed:.3f}s"


def test_rate_limiter_disabled_when_rate_zero() -> None:
    limiter = RateLimiter(per_host_rps=0)

    async def burst() -> float:
        t0 = time.monotonic()
        for _ in range(5):
            await limiter.acquire("a.example")
        return time.monotonic() - t0

    assert asyncio.run(burst()) < 0.05


# ----------------------------------------------------------------------
# HTTPClient
# ----------------------------------------------------------------------


def test_http_get_fetches_and_caches(tmp_path: Path, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.example.invalid/v1/data",
        json={"hello": "world"},
        headers={"content-type": "application/json"},
    )

    async def go() -> tuple[CachedResponse, CachedResponse]:
        async with HTTPClient(_settings(tmp_path)) as client:
            first = await client.get("https://api.example.invalid/v1/data")
            second = await client.get("https://api.example.invalid/v1/data")
            return first, second

    first, second = asyncio.run(go())
    assert first.from_cache is False
    assert first.json() == {"hello": "world"}
    assert second.from_cache is True
    assert second.content == first.content
    # Exactly one upstream call despite two get()s.
    assert len(httpx_mock.get_requests()) == 1


def test_http_retries_on_500_then_succeeds(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    url = "https://api.example.invalid/flaky"
    httpx_mock.add_response(url=url, status_code=500)
    httpx_mock.add_response(url=url, status_code=502)
    httpx_mock.add_response(url=url, status_code=200, text="ok")

    async def go() -> CachedResponse:
        client = HTTPClient(_settings(tmp_path), backoff_base_seconds=0.01)
        try:
            return await client.get(url)
        finally:
            await client.aclose()

    resp = asyncio.run(go())
    assert resp.status_code == 200
    assert resp.text() == "ok"
    assert len(httpx_mock.get_requests()) == 3


def test_http_honors_retry_after_on_429(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    url = "https://api.example.invalid/throttled"
    httpx_mock.add_response(url=url, status_code=429, headers={"Retry-After": "0.05"})
    httpx_mock.add_response(url=url, status_code=200, text="finally")

    async def go() -> tuple[CachedResponse, float]:
        client = HTTPClient(_settings(tmp_path), backoff_base_seconds=10.0)
        # Backoff is set to 10s; if Retry-After were ignored the test would
        # exceed any reasonable timeout. Honoring it should keep us under ~1s.
        t0 = time.monotonic()
        try:
            resp = await client.get(url)
            return resp, time.monotonic() - t0
        finally:
            await client.aclose()

    resp, elapsed = asyncio.run(go())
    assert resp.status_code == 200
    assert elapsed < 1.0, f"Retry-After ignored, took {elapsed:.3f}s"


def test_http_raises_after_max_retries(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    url = "https://api.example.invalid/always-500"
    # max_retries=2 → 3 total attempts (initial + 2 retries).
    for _ in range(3):
        httpx_mock.add_response(url=url, status_code=500)

    async def go() -> None:
        client = HTTPClient(
            _settings(tmp_path),
            max_retries=2,
            backoff_base_seconds=0.005,
        )
        try:
            await client.get(url)
        finally:
            await client.aclose()

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(go())


def test_http_bypass_cache(tmp_path: Path, httpx_mock: HTTPXMock) -> None:
    url = "https://api.example.invalid/no-cache"
    httpx_mock.add_response(url=url, text="one")
    httpx_mock.add_response(url=url, text="two")

    async def go() -> tuple[str, str]:
        async with HTTPClient(_settings(tmp_path)) as client:
            a = await client.get(url, bypass_cache=True)
            b = await client.get(url, bypass_cache=True)
            return a.text(), b.text()

    a, b = asyncio.run(go())
    assert (a, b) == ("one", "two")


# ----------------------------------------------------------------------
# BaseCollector / EchoCollector
# ----------------------------------------------------------------------


def test_echo_collector_fetches_and_caches(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    url = "https://api.example.invalid/echo"
    httpx_mock.add_response(url=url, text="payload")

    async def go() -> tuple[CollectionResult, CollectionResult]:
        async with EchoCollector(_settings(tmp_path)) as coll:
            r1 = await coll.collect(url=url)
            r2 = await coll.collect(url=url)
            return r1, r2

    r1, r2 = asyncio.run(go())
    assert r1.records == len(b"payload")
    assert r2.records == len(b"payload")
    assert r1.name == "echo"
    assert r1.duration_seconds >= 0
    assert len(httpx_mock.get_requests()) == 1  # second call hit the cache


def test_echo_collector_retries_then_succeeds(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    url = "https://api.example.invalid/retry"
    httpx_mock.add_response(url=url, status_code=503)
    httpx_mock.add_response(url=url, text="hello")

    async def go() -> CollectionResult:
        client = HTTPClient(_settings(tmp_path), backoff_base_seconds=0.005)
        async with EchoCollector(_settings(tmp_path), client=client) as coll:
            return await coll.collect(url=url)

    result = asyncio.run(go())
    assert result.records == len(b"hello")
    assert len(httpx_mock.get_requests()) == 2


def test_base_collector_requires_collect_method(tmp_path: Path) -> None:
    """ABC enforcement: a subclass without collect() cannot be instantiated."""

    class Incomplete(BaseCollector):  # type: ignore[abstract]
        name = "incomplete"

    with pytest.raises(TypeError):
        Incomplete(_settings(tmp_path))  # type: ignore[abstract]
