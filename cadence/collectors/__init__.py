"""CADENCE collectors.

Each collector subclasses :class:`cadence.collectors.base.BaseCollector` and
implements an async ``collect()`` method. Collectors share an
:class:`~cadence.collectors.base.HTTPClient`, which handles per-host rate
limiting, retry-with-backoff on 429/5xx (honoring ``Retry-After``), and a
persistent disk-backed response cache.
"""

from cadence.collectors.base import (
    BaseCollector,
    CachedResponse,
    CollectionResult,
    DiskCache,
    HTTPClient,
    RateLimiter,
)

__all__ = [
    "BaseCollector",
    "CachedResponse",
    "CollectionResult",
    "DiskCache",
    "HTTPClient",
    "RateLimiter",
]
