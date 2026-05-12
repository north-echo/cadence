"""Shared collector infrastructure.

This module is the WP-02 deliverable. It provides:

* :class:`BaseCollector` — ABC that every concrete collector subclasses.
* :class:`HTTPClient` — async httpx wrapper that layers per-host rate limiting,
  retry-with-jitter on 429/5xx, and a persistent disk cache on top of plain
  HTTP GET.
* :class:`RateLimiter` — per-host token-spacing limiter (default 1 req/sec).
* :class:`DiskCache` — JSON-on-disk response cache with TTL.

Design notes
------------

* The cache TTL is per-request: callers pass ``ttl_seconds=`` to distinguish
  stable historical data (default 24h) from current-state polling (default
  1h). Defaults come from :class:`~cadence.config.Settings`.
* The rate limiter uses ``time.monotonic`` so wall-clock jumps cannot cause
  spurious throttling.
* Retry-After is honored when present on a 429 response; otherwise we fall
  back to exponential backoff with jitter.
* Cache entries are JSON with the body base64-encoded. This makes them
  trivially inspectable with ``jq`` at the cost of ~33% disk overhead, which
  is acceptable for a research tool that does not need to be lean.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

import httpx
import structlog

from cadence.config import Settings

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CachedResponse:
    """An HTTP response (live or cached) as exposed to collectors."""

    url: str
    status_code: int
    headers: dict[str, str]
    content: bytes
    fetched_at: datetime
    ttl_seconds: int
    from_cache: bool = False

    def json(self) -> Any:
        return json.loads(self.content)

    def text(self, encoding: str = "utf-8") -> str:
        return self.content.decode(encoding)


@dataclass
class CollectionResult:
    """Summary returned by :meth:`BaseCollector.collect`."""

    name: str
    started_at: datetime
    completed_at: datetime
    records: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return (self.completed_at - self.started_at).total_seconds()


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Per-host minimum-interval limiter.

    With ``per_host_rps=1.0``, two consecutive requests to the same host are
    spaced at least one second apart. Requests to different hosts do not
    block each other.
    """

    def __init__(self, per_host_rps: float) -> None:
        self.min_interval = 1.0 / per_host_rps if per_host_rps > 0 else 0.0
        self._next_allowed: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, host: str) -> None:
        if self.min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            next_ok = self._next_allowed.get(host, 0.0)
            wait = next_ok - now
            scheduled = max(now, next_ok) + self.min_interval
            self._next_allowed[host] = scheduled
        if wait > 0:
            await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------


class DiskCache:
    """JSON-on-disk response cache with TTL.

    Keys are sha256 hex digests. Entries shard into 2-char subdirectories to
    avoid huge flat directories.
    """

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir

    @staticmethod
    def key_for(method: str, url: str) -> str:
        return hashlib.sha256(f"{method.upper()}:{url}".encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    def get(self, key: str) -> CachedResponse | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        ttl = int(data["ttl_seconds"])
        expires_at = fetched_at.timestamp() + ttl
        if expires_at < time.time():
            return None
        return CachedResponse(
            url=data["url"],
            status_code=int(data["status_code"]),
            headers=dict(data["headers"]),
            content=base64.b64decode(data["content_b64"]),
            fetched_at=fetched_at,
            ttl_seconds=ttl,
            from_cache=True,
        )

    def put(self, key: str, response: CachedResponse) -> None:
        """Persist a response to disk; tolerant of ENOSPC and friends.

        The cache is an optimization, not a correctness requirement. If the
        write fails for any OS reason (disk full, read-only fs, permission,
        directory mid-prune) we log and continue — the collector returns the
        response just fine, the next request just re-fetches.
        """
        path = self._path(key)
        payload = {
            "url": response.url,
            "status_code": response.status_code,
            "headers": response.headers,
            "content_b64": base64.b64encode(response.content).decode("ascii"),
            "fetched_at": response.fetched_at.isoformat(),
            "ttl_seconds": response.ttl_seconds,
        }
        tmp = path.with_suffix(".json.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.warning(
                "cache.put_failed", url=response.url, error=str(exc),
            )
            # Best-effort cleanup of a partial write so the next put doesn't
            # see stale .tmp garbage.
            import contextlib
            with contextlib.suppress(OSError):
                tmp.unlink()


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


_RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class HTTPClient:
    """Async HTTP client with rate limiting, retry-with-jitter, and cache.

    Instantiate once per process and share across collectors. The underlying
    :class:`httpx.AsyncClient` is created lazily on first request so that
    construction is cheap and free of side effects.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        max_retries: int = 5,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
        request_timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.request_timeout_seconds = request_timeout_seconds

        self.rate_limiter = RateLimiter(settings.rate_limit_per_host)
        self.cache = DiskCache(settings.cache_dir)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    # -- lifecycle ----------------------------------------------------------

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": self.settings.user_agent},
                timeout=httpx.Timeout(self.request_timeout_seconds),
                transport=self._transport,
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> HTTPClient:
        self._ensure_client()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- core fetch ---------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped at backoff_max."""
        exp = self.backoff_base_seconds * (2**attempt)
        return min(self.backoff_max_seconds, exp) * (0.5 + random.random() * 0.5)

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None

    async def get(
        self,
        url: str,
        *,
        ttl_seconds: int | None = None,
        bypass_cache: bool = False,
        headers: dict[str, str] | None = None,
    ) -> CachedResponse:
        """Async GET with cache, per-host rate limit, and retry on 429/5xx.

        Parameters
        ----------
        ttl_seconds:
            Override the cache TTL for this request. Defaults to
            :attr:`Settings.cache_ttl_stable_seconds` (24h).
        bypass_cache:
            Skip the cache for both read and write.
        headers:
            Per-request HTTP headers (merged on top of the client defaults).
            Note: changes here do *not* affect the cache key, so callers that
            need response-by-Accept differentiation must use distinct URLs or
            pass ``bypass_cache=True``.
        """
        ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else self.settings.cache_ttl_stable_seconds
        )
        cache_key = DiskCache.key_for("GET", url)

        if not bypass_cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                log.debug("http.cache_hit", url=url)
                return cached

        client = self._ensure_client()
        host = urlparse(url).netloc
        attempt = 0
        last_exc: Exception | None = None

        while True:
            await self.rate_limiter.acquire(host)
            try:
                response = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    log.error("http.giving_up", url=url, error=str(exc), attempts=attempt + 1)
                    raise
                wait = self._backoff(attempt)
                log.warning(
                    "http.transport_error",
                    url=url,
                    error=str(exc),
                    attempt=attempt,
                    wait_seconds=wait,
                )
                await asyncio.sleep(wait)
                attempt += 1
                continue

            if response.status_code in _RETRYABLE_STATUSES:
                if attempt >= self.max_retries:
                    log.error(
                        "http.giving_up",
                        url=url,
                        status=response.status_code,
                        attempts=attempt + 1,
                    )
                    response.raise_for_status()
                retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
                wait = retry_after if retry_after is not None else self._backoff(attempt)
                log.warning(
                    "http.retryable_status",
                    url=url,
                    status=response.status_code,
                    attempt=attempt,
                    wait_seconds=wait,
                )
                await asyncio.sleep(wait)
                attempt += 1
                continue

            response.raise_for_status()
            cached = CachedResponse(
                url=url,
                status_code=response.status_code,
                headers={k.lower(): v for k, v in response.headers.items()},
                content=response.content,
                fetched_at=datetime.now(UTC),
                ttl_seconds=ttl,
                from_cache=False,
            )
            if not bypass_cache:
                self.cache.put(cache_key, cached)
            log.debug(
                "http.fetched",
                url=url,
                status=response.status_code,
                bytes=len(response.content),
                attempts=attempt + 1,
            )
            return cached

        # Unreachable; the loop either returns or raises.
        raise RuntimeError(f"unreachable retry loop exited for {url}: {last_exc}")


# ---------------------------------------------------------------------------
# BaseCollector
# ---------------------------------------------------------------------------


class BaseCollector(ABC):
    """Abstract base for every concrete collector.

    Subclasses set the ``name`` class variable (used in logs and the
    :class:`CollectionResult`) and implement :meth:`collect`.
    """

    name: ClassVar[str]

    def __init__(
        self,
        settings: Settings,
        client: HTTPClient | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self.client = client or HTTPClient(settings)
        self.log = structlog.get_logger(__name__).bind(collector=self.name)

    @abstractmethod
    async def collect(self, **kwargs: Any) -> CollectionResult:
        """Run the collector once. Must be overridden."""

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def __aenter__(self) -> BaseCollector:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
