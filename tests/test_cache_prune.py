"""Tests for cadence.cache — pruning behaviour and disk-full tolerance."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cadence.cache import DEFAULT_MAX_BYTES, cache_size_bytes, prune
from cadence.collectors.base import CachedResponse, DiskCache


def _write_cache_entry(cache_dir: Path, *, key: str, size: int, mtime: float) -> Path:
    """Write a junk-payload cache file of approximately ``size`` bytes."""
    path = cache_dir / key[:2] / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"x": "a" * max(0, size - 16)}
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


# ----------------------------------------------------------------------
# cache_size_bytes
# ----------------------------------------------------------------------


def test_cache_size_bytes_returns_zero_for_missing_dir(tmp_path: Path) -> None:
    assert cache_size_bytes(tmp_path / "absent") == 0


def test_cache_size_bytes_sums_files(tmp_path: Path) -> None:
    _write_cache_entry(tmp_path, key="ab" + "0" * 62, size=1024, mtime=time.time())
    _write_cache_entry(tmp_path, key="cd" + "1" * 62, size=2048, mtime=time.time())
    n = cache_size_bytes(tmp_path)
    assert 3000 <= n <= 3500  # JSON envelope adds a bit


def test_cache_size_bytes_includes_orphan_tmp(tmp_path: Path) -> None:
    """Orphaned .tmp files (from interrupted writes) count toward total."""
    p = tmp_path / "ab" / "abcdef.json.tmp"
    p.parent.mkdir(parents=True)
    p.write_text("X" * 4096)
    assert cache_size_bytes(tmp_path) >= 4096


# ----------------------------------------------------------------------
# prune
# ----------------------------------------------------------------------


def test_prune_noop_when_under_cap(tmp_path: Path) -> None:
    _write_cache_entry(tmp_path, key="aa" + "0" * 62, size=1024, mtime=time.time())
    result = prune(tmp_path, max_bytes=1_000_000)
    assert result.files_removed == 0
    assert result.bytes_freed == 0


def test_prune_evicts_oldest_first(tmp_path: Path) -> None:
    """Older mtime files must be removed before newer ones."""
    now = time.time()
    old = _write_cache_entry(tmp_path, key="aa" + "0" * 62, size=1024, mtime=now - 1000)
    _write_cache_entry(tmp_path, key="bb" + "0" * 62, size=1024, mtime=now - 500)
    new = _write_cache_entry(tmp_path, key="cc" + "0" * 62, size=1024, mtime=now)

    # Set cap below current total → some must go
    before = cache_size_bytes(tmp_path)
    cap = before - 1500  # forces at least one eviction
    result = prune(tmp_path, max_bytes=cap)

    assert result.files_removed >= 1
    assert result.total_bytes_after <= cap
    assert not old.exists()         # oldest evicted
    assert new.exists()             # newest survives


def test_prune_handles_orphan_tmp_files(tmp_path: Path) -> None:
    tmp_file = tmp_path / "xx" / "xxxxxx.json.tmp"
    tmp_file.parent.mkdir(parents=True)
    tmp_file.write_text("Y" * 4096)
    os.utime(tmp_file, (time.time() - 9999, time.time() - 9999))
    # Also a fresh real entry — make the cap force eviction
    _write_cache_entry(tmp_path, key="zz" + "0" * 62, size=4096, mtime=time.time())

    result = prune(tmp_path, max_bytes=4500)
    # The orphan tmp file should be the oldest and the first to go
    assert not tmp_file.exists()
    assert result.files_removed >= 1


def test_prune_returns_zero_freed_on_missing_dir(tmp_path: Path) -> None:
    result = prune(tmp_path / "absent", max_bytes=1024)
    assert result.bytes_freed == 0
    assert result.files_removed == 0


def test_default_max_bytes_is_one_gib() -> None:
    assert DEFAULT_MAX_BYTES == 1024 * 1024 * 1024


# ----------------------------------------------------------------------
# DiskCache.put tolerant of OSError
# ----------------------------------------------------------------------


def test_diskcache_put_tolerates_os_error_from_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate ENOSPC mid-write: put() must NOT raise."""
    cache = DiskCache(tmp_path)
    response = CachedResponse(
        url="https://example/x",
        status_code=200,
        headers={},
        content=b"hello",
        fetched_at=datetime.now(UTC),
        ttl_seconds=60,
    )

    real_write = Path.write_text

    def boom(self, *args, **kwargs):
        if self.suffix == ".tmp":
            raise OSError(28, "No space left on device")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", boom)
    # Must not raise.
    cache.put(DiskCache.key_for("GET", response.url), response)

    # No partial .tmp lying around
    leftovers = list(tmp_path.rglob("*.tmp"))
    assert not leftovers


def test_diskcache_put_tolerates_mkdir_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = DiskCache(tmp_path)
    response = CachedResponse(
        url="https://example/x",
        status_code=200,
        headers={},
        content=b"hello",
        fetched_at=datetime.now(UTC),
        ttl_seconds=60,
    )
    real_mkdir = Path.mkdir

    def boom(self, *args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "mkdir", boom)
    cache.put(DiskCache.key_for("GET", response.url), response)  # must not raise

    monkeypatch.setattr(Path, "mkdir", real_mkdir)
