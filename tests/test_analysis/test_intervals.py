"""Tests for cadence.analysis.intervals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from cadence.analysis.intervals import (
    compute_intervals,
    reconstruct_intervals,
)
from cadence.config import Settings
from cadence.db import apply_migrations, connect


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        cache_dir=tmp_path / "cache",
        db_path=tmp_path / "cadence.db",
        rate_limit_per_host=0,
    )


def _init_db(settings: Settings) -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(settings.db_path) as conn:
        apply_migrations(conn)


def _add_image(conn, *, image_id: str, repository: str, tier: str,
               architecture: str, build_date: datetime) -> None:
    conn.execute(
        """
        INSERT INTO container_image
            (image_id, source, registry, repository, tier, tag, digest,
             architecture, build_date, raw_json, collected_at)
        VALUES (?, 'catalog', 'registry.access.redhat.com', ?, ?, ?, ?,
                ?, ?, '{}', ?)
        """,
        (
            image_id, repository, tier, f"tag-{image_id}", f"sha256:{image_id}",
            architecture, build_date.isoformat(), datetime.now(UTC).isoformat(),
        ),
    )


def test_compute_intervals_consecutive_pairs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    base = datetime(2025, 1, 1, tzinfo=UTC)
    with connect(settings.db_path) as conn:
        for i in range(4):
            _add_image(
                conn,
                image_id=f"img-{i}",
                repository="ubi9/ubi",
                tier="ubi",
                architecture="x86_64",
                build_date=base + timedelta(days=i * 5),
            )
        intervals = compute_intervals(conn)
    # 4 images → 3 consecutive pairs
    assert len(intervals) == 3
    assert all(iv.interval_seconds == 5 * 86400 for iv in intervals)
    assert all(iv.repository == "ubi9/ubi" and iv.architecture == "x86_64"
               for iv in intervals)
    # Pair ordering: oldest pair first
    assert intervals[0].prior_image_id == "img-0"
    assert intervals[0].next_image_id == "img-1"
    assert intervals[-1].next_image_id == "img-3"


def test_compute_intervals_independent_per_repo_arch(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    base = datetime(2025, 1, 1, tzinfo=UTC)
    with connect(settings.db_path) as conn:
        # Two (repo, arch) groups
        _add_image(conn, image_id="a0", repository="ubi9/ubi", tier="ubi",
                   architecture="x86_64", build_date=base)
        _add_image(conn, image_id="a1", repository="ubi9/ubi", tier="ubi",
                   architecture="x86_64", build_date=base + timedelta(days=3))
        _add_image(conn, image_id="b0", repository="ubi9/ubi", tier="ubi",
                   architecture="aarch64", build_date=base)
        _add_image(conn, image_id="b1", repository="ubi9/ubi", tier="ubi",
                   architecture="aarch64", build_date=base + timedelta(days=7))
        _add_image(conn, image_id="c0", repository="rhacm2/console-rhel9",
                   tier="rh_layered", architecture="x86_64",
                   build_date=base + timedelta(days=1))
        intervals = compute_intervals(conn)
    # One interval per (repo,arch) pair that has >=2 images
    by_key = {(iv.repository, iv.architecture): iv for iv in intervals}
    assert ("ubi9/ubi", "x86_64") in by_key
    assert ("ubi9/ubi", "aarch64") in by_key
    assert by_key[("ubi9/ubi", "x86_64")].interval_seconds == 3 * 86400
    assert by_key[("ubi9/ubi", "aarch64")].interval_seconds == 7 * 86400
    # Single-image group produces no interval
    assert ("rhacm2/console-rhel9", "x86_64") not in by_key


def test_compute_intervals_includes_quay_source(tmp_path: Path) -> None:
    """Quay images (source='quay') still participate in interval reconstruction."""
    settings = _settings(tmp_path)
    _init_db(settings)
    base = datetime(2025, 1, 1, tzinfo=UTC)
    with connect(settings.db_path) as conn:
        conn.execute(
            """INSERT INTO container_image
                  (image_id, source, registry, repository, tier, tag, digest,
                   architecture, build_date, raw_json, collected_at)
               VALUES ('q0', 'quay', 'quay.io', 'cilium/cilium', 'quay_community',
                       't0', 'sha256:0', 'amd64', ?, '{}', ?)""",
            (base.isoformat(), datetime.now(UTC).isoformat()),
        )
        conn.execute(
            """INSERT INTO container_image
                  (image_id, source, registry, repository, tier, tag, digest,
                   architecture, build_date, raw_json, collected_at)
               VALUES ('q1', 'quay', 'quay.io', 'cilium/cilium', 'quay_community',
                       't1', 'sha256:1', 'amd64', ?, '{}', ?)""",
            ((base + timedelta(days=2)).isoformat(), datetime.now(UTC).isoformat()),
        )
        intervals = compute_intervals(conn)
    assert len(intervals) == 1
    assert intervals[0].repository == "cilium/cilium"
    assert intervals[0].interval_seconds == 2 * 86400


def test_persist_intervals_replaces_table(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    base = datetime(2025, 1, 1, tzinfo=UTC)
    with connect(settings.db_path) as conn:
        _add_image(conn, image_id="a", repository="ubi9/ubi", tier="ubi",
                   architecture="x86_64", build_date=base)
        _add_image(conn, image_id="b", repository="ubi9/ubi", tier="ubi",
                   architecture="x86_64", build_date=base + timedelta(days=1))
        # Run twice — second run replaces first
        reconstruct_intervals(conn)
        reconstruct_intervals(conn)
        n = conn.execute("SELECT COUNT(*) FROM rebuild_interval").fetchone()[0]
    assert n == 1
