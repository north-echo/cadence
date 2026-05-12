"""HTTP cache management — pruning, sizing, and stats.

The disk cache (``cadence.collectors.base.DiskCache``) has no built-in
size limit; cache entries persist until manually evicted or until their
TTL is consulted on read. On a host with a backfill-heavy workload the
cache can grow into the gigabytes. This module exposes a simple
oldest-first pruner the CLI (``cadence cache prune``) and a daily
systemd timer can call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


DEFAULT_MAX_BYTES = 1 * 1024 * 1024 * 1024  # 1 GiB


@dataclass(frozen=True)
class PruneResult:
    total_bytes_before: int
    total_bytes_after: int
    files_removed: int

    @property
    def bytes_freed(self) -> int:
        return self.total_bytes_before - self.total_bytes_after


def _iter_cache_files(cache_dir: Path):
    """Yield ``(mtime, size, path)`` for every cache file under ``cache_dir``."""
    if not cache_dir.exists():
        return
    for path in cache_dir.rglob("*.json"):
        try:
            st = path.stat()
        except OSError:
            continue
        yield st.st_mtime, st.st_size, path
    # Also catch orphaned .tmp files (from interrupted writes).
    for path in cache_dir.rglob("*.json.tmp"):
        try:
            st = path.stat()
        except OSError:
            continue
        yield st.st_mtime, st.st_size, path


def cache_size_bytes(cache_dir: Path) -> int:
    """Return the current total byte size of the disk cache."""
    return sum(size for _, size, _ in _iter_cache_files(cache_dir))


def prune(cache_dir: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> PruneResult:
    """Delete cache files (oldest first) until total size <= ``max_bytes``.

    Empty directories left behind are not removed — they're harmless and
    avoid races with concurrent collectors that might be re-populating
    them at the same time.
    """
    files = sorted(_iter_cache_files(cache_dir))
    total = sum(size for _, size, _ in files)
    before = total
    removed = 0

    if total <= max_bytes:
        log.info(
            "cache.prune_noop",
            cache_dir=str(cache_dir), total_bytes=total, max_bytes=max_bytes,
        )
        return PruneResult(
            total_bytes_before=before, total_bytes_after=total, files_removed=0,
        )

    for _mtime, size, path in files:
        if total <= max_bytes:
            break
        try:
            path.unlink()
            total -= size
            removed += 1
        except OSError as exc:
            log.warning("cache.prune_unlink_failed", path=str(path), error=str(exc))

    log.info(
        "cache.prune_done",
        cache_dir=str(cache_dir),
        bytes_before=before, bytes_after=total, files_removed=removed,
    )
    return PruneResult(
        total_bytes_before=before, total_bytes_after=total, files_removed=removed,
    )


__all__ = [
    "DEFAULT_MAX_BYTES",
    "PruneResult",
    "cache_size_bytes",
    "prune",
]
