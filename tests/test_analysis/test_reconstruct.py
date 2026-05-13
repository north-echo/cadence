"""Tests for cadence.analysis.reconstruct."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cadence.analysis.reconstruct import reconstruct
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


def _setup_minimal_fixture(settings: Settings) -> dict:
    """One RHSA, one fix, one tracked repo, one image with the fix.

    Timeline:
      RHSA published     2025-01-10 12:00 UTC
      repodata observed  2025-01-11 12:00 UTC  (Gap A = 86400s)
      image built        2025-01-13 12:00 UTC  (Gap B = 172800s, Gap C = 259200s)
    """
    pub = datetime(2025, 1, 10, 12, 0, tzinfo=UTC)
    rep = datetime(2025, 1, 11, 12, 0, tzinfo=UTC)
    img = datetime(2025, 1, 13, 12, 0, tzinfo=UTC)
    with connect(settings.db_path) as conn:
        conn.execute(
            """INSERT INTO rhsa
                 (rhsa_id, title, severity, published_at, source_url,
                  raw_json, collected_at)
               VALUES ('RHSA-2025:1', 't', 'important', ?, 'x', '{}', ?)""",
            (pub.isoformat(), pub.isoformat()),
        )
        conn.execute(
            """INSERT INTO rhsa_package_fix
                 (rhsa_id, package_name, fixed_version, arch, product)
               VALUES ('RHSA-2025:1', 'openssl', '0:3.0.7-25.el9_2',
                       'x86_64', 'AppStream-9.2.0.Z')""",
        )
        conn.execute(
            """INSERT INTO tracked_repository
                 (repository, source, registry, tier, rationale, added_at)
               VALUES ('ubi9/ubi', 'catalog', 'registry.access.redhat.com', 'ubi',
                       'test', ?)""",
            (pub.isoformat(),),
        )

        cur = conn.execute(
            """INSERT INTO repo_observation
                 (repo_id, observed_at, repomd_revision, primary_xml_sha256)
               VALUES ('ubi9/9/x86_64/baseos', ?, 'r', 's')""",
            (rep.isoformat(),),
        )
        obs_id = cur.lastrowid
        conn.execute(
            """INSERT INTO repo_package
                 (observation_id, package_name, version, arch,
                  build_time, file_time)
               VALUES (?, 'openssl', '0:3.0.7-25.el9_2', 'x86_64', ?, ?)""",
            (obs_id, rep.isoformat(), rep.isoformat()),
        )

        conn.execute(
            """INSERT INTO container_image
                 (image_id, source, registry, repository, tier, tag, digest,
                  architecture, build_date, raw_json, collected_at)
               VALUES ('img-1', 'catalog', 'registry.access.redhat.com', 'ubi9/ubi',
                       'ubi', '9.2-1', 'sha256:1', 'x86_64', ?, '{}', ?)""",
            (img.isoformat(), datetime.now(UTC).isoformat()),
        )
        conn.execute(
            """INSERT INTO container_image_rpm
                 (image_id, package_name, version, arch)
               VALUES ('img-1', 'openssl', '0:3.0.7-25.el9_2', 'x86_64')""",
        )
    return {"published_at": pub, "repo_at": rep, "image_at": img}


# ----------------------------------------------------------------------
# Synthetic gap-measurement correctness
# ----------------------------------------------------------------------


def test_reconstruct_computes_all_three_gaps(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _setup_minimal_fixture(settings)

    with connect(settings.db_path) as conn:
        result = reconstruct(conn)

    assert result.gap_rows_written == 1
    with connect(settings.db_path) as conn:
        row = conn.execute(
            """SELECT gap_a_seconds, gap_b_seconds, gap_c_seconds, image_id,
                      methodology_version
                 FROM gap_measurement"""
        ).fetchone()
    gap_a, gap_b, gap_c, image_id, version = row
    assert gap_a == 86_400          # one day RHSA→repodata
    assert gap_b == 2 * 86_400      # two days repodata→image
    assert gap_c == 3 * 86_400      # three days RHSA→image
    assert image_id == "img-1"
    assert version == "v1"


def test_reconstruct_gap_a_null_when_no_repo_observation(tmp_path: Path) -> None:
    """RHSA pre-dates our forward polling: Gap A NULL, Gap B NULL, Gap C still computed."""
    settings = _settings(tmp_path)
    _init_db(settings)
    _setup_minimal_fixture(settings)
    # Drop the repo_package row so Gap A has no anchor
    with connect(settings.db_path) as conn:
        conn.execute("DELETE FROM repo_package")
        conn.execute("DELETE FROM repo_observation")
        reconstruct(conn)
        row = conn.execute(
            "SELECT gap_a_seconds, gap_b_seconds, gap_c_seconds FROM gap_measurement"
        ).fetchone()
    gap_a, gap_b, gap_c = row
    assert gap_a is None
    assert gap_b is None        # no repo anchor, no Gap B either
    assert gap_c == 3 * 86_400  # but Gap C still falls out of RHSA pub + image build


def test_skip_quay_targets_omits_quay_rows(tmp_path: Path) -> None:
    """--skip-quay-targets drops the always-NULL Quay rows from the output."""
    settings = _settings(tmp_path)
    _init_db(settings)
    _setup_minimal_fixture(settings)
    with connect(settings.db_path) as conn:
        conn.execute(
            """INSERT INTO tracked_repository
                  (repository, source, registry, tier, rationale, added_at)
               VALUES ('cilium/cilium', 'quay', 'quay.io', 'quay_community',
                       'test', ?)""",
            (datetime.now(UTC).isoformat(),),
        )
        # With skip: Quay row should NOT be emitted.
        reconstruct(conn, skip_quay_targets=True)
        rows = conn.execute(
            "SELECT DISTINCT repository FROM gap_measurement"
        ).fetchall()
    repos = {r[0] for r in rows}
    assert "cilium/cilium" not in repos
    assert "ubi9/ubi" in repos


def test_reconstruct_quay_images_yield_null_image_gaps(tmp_path: Path) -> None:
    """Quay tracked repos have no container_image_rpm rows; gap_c stays NULL."""
    settings = _settings(tmp_path)
    _init_db(settings)
    _setup_minimal_fixture(settings)
    with connect(settings.db_path) as conn:
        conn.execute(
            """INSERT INTO tracked_repository
                  (repository, source, registry, tier, rationale, added_at)
               VALUES ('cilium/cilium', 'quay', 'quay.io', 'quay_community',
                       'test', ?)""",
            (datetime.now(UTC).isoformat(),),
        )
        reconstruct(conn)
        rows = conn.execute(
            """SELECT repository, gap_c_seconds, image_id
                 FROM gap_measurement
                ORDER BY repository"""
        ).fetchall()
    quay = next(r for r in rows if r[0] == "cilium/cilium")
    ubi = next(r for r in rows if r[0] == "ubi9/ubi")
    assert quay[1] is None and quay[2] is None
    assert ubi[1] == 3 * 86_400


def test_reconstruct_excludes_vex_not_affected(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _setup_minimal_fixture(settings)
    with connect(settings.db_path) as conn:
        # Mark the product not_affected
        conn.execute(
            """INSERT INTO rhsa_vex (rhsa_id, product_id, status)
               VALUES ('RHSA-2025:1', 'AppStream-9.2.0.Z:openssl-0:3.0.7-25.el9_2.x86_64',
                       'not_affected')"""
        )
        result = reconstruct(conn)
        n = conn.execute("SELECT COUNT(*) FROM gap_measurement").fetchone()[0]
    assert result.not_affected_skipped >= 1
    assert n == 0  # the only fix was skipped


# ----------------------------------------------------------------------
# Methodology versioning
# ----------------------------------------------------------------------


def test_reconstruct_idempotent_within_methodology_version(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _setup_minimal_fixture(settings)
    with connect(settings.db_path) as conn:
        reconstruct(conn)
        n1 = conn.execute(
            "SELECT COUNT(*) FROM gap_measurement WHERE methodology_version = 'v1'"
        ).fetchone()[0]
        reconstruct(conn)
        n2 = conn.execute(
            "SELECT COUNT(*) FROM gap_measurement WHERE methodology_version = 'v1'"
        ).fetchone()[0]
    assert n1 == n2 == 1


def test_two_methodology_versions_coexist(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _setup_minimal_fixture(settings)
    with connect(settings.db_path) as conn:
        reconstruct(conn, methodology_version="v1")
        reconstruct(conn, methodology_version="v1.1-exp")
        versions = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT methodology_version FROM gap_measurement"
            ).fetchall()
        }
    assert versions == {"v1", "v1.1-exp"}


# ----------------------------------------------------------------------
# Cross-validation against catalog_advisory_mapping
# ----------------------------------------------------------------------


def test_persist_streams_rows_in_bounded_batches(tmp_path: Path) -> None:
    """Regression test for the OOM that bit reconstruct at scale.

    Verifies ``persist`` drains an iterator and writes via batched
    executemany — peak memory is O(batch_size), not O(total rows). Tests
    by passing a generator that would be expensive to materialise and
    confirming the right number of rows lands.
    """
    from cadence.analysis.reconstruct import persist

    settings = _settings(tmp_path)
    _init_db(settings)

    # Synthetic batch of 50,000 rows. If `persist` accidentally collects
    # this into a list before inserting, that's still fine for 50k —
    # the *real* protection is the generator-shaped input. The point of
    # the test is to lock in the iterable contract.
    pub = datetime(2025, 1, 1, tzinfo=UTC).isoformat()
    now = datetime.now(UTC).isoformat()

    # Need a parent rhsa for FK
    with connect(settings.db_path) as conn:
        conn.execute(
            """INSERT INTO rhsa
                 (rhsa_id, title, severity, published_at, source_url,
                  raw_json, collected_at)
               VALUES ('RHSA-2099:9999', 'x', 'low', ?, 'x', '{}', ?)""",
            (pub, pub),
        )

        def gen():
            for i in range(50_000):
                yield (
                    "RHSA-2099:9999",
                    f"repo/{i % 4}",
                    "ubi",
                    "x86_64",
                    f"pkg-{i % 100}",
                    "0:1-1.el9",
                    pub, None, None, None,
                    None, None, None,
                    now, "v1",
                )

        written = persist(conn, gen(), methodology_version="v1", batch_size=500)
        assert written == 50_000

        n = conn.execute(
            "SELECT COUNT(*) FROM gap_measurement WHERE methodology_version = 'v1'"
        ).fetchone()[0]
        assert n == 50_000


def test_cross_check_against_catalog_advisory_mapping(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _setup_minimal_fixture(settings)
    with connect(settings.db_path) as conn:
        # Parent rows first (FK on catalog_advisory_mapping.image_id)
        conn.execute(
            """INSERT INTO container_image
                 (image_id, source, registry, repository, tier, tag, digest,
                  architecture, build_date, raw_json, collected_at)
               VALUES ('img-other', 'catalog', 'registry.access.redhat.com',
                       'ubi9/ubi', 'ubi', 'tag-other', 'sha256:other', 'x86_64',
                       ?, '{}', ?)""",
            (datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
        )
        # Matching mapping
        conn.execute(
            """INSERT INTO catalog_advisory_mapping (image_id, advisory_id, nvra)
               VALUES ('img-1', 'RHSA-2025:1', 'openssl-3.0.7-25.el9_2.x86_64')"""
        )
        # Non-matching mapping (different image)
        conn.execute(
            """INSERT INTO catalog_advisory_mapping (image_id, advisory_id, nvra)
               VALUES ('img-other', 'RHSA-2025:1', 'openssl-3.0.7-25.el9_2.x86_64')"""
        )

        result = reconstruct(conn)

    assert result.cross_check_total == 2
    assert result.cross_check_matched == 1
    assert result.cross_check_match_rate == 0.5
