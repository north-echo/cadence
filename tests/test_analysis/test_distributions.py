"""Tests for cadence.analysis.slice / gaps / intervals — WP-10 acceptance.

Acceptance points exercised here:

* "Test dataset with known statistics produces correct percentiles."
* "Slicing matches manually-verified subsets."
* "Warns when N<30 for percentile calculation."
* "Headline tier comparison reproduces the spike's qualitative finding"
  (UBI ≈ ocp_platform fast; rh_layered slower) — exercised on the synthetic
  fixture, not real data.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cadence.analysis.gaps import gap_distribution
from cadence.analysis.intervals import interval_distribution
from cadence.analysis.slice import (
    LOW_N_THRESHOLD,
    compute_distribution,
    resolve_gap_facet,
    resolve_interval_facet,
    supported_for_gaps,
    supported_for_intervals,
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


# ----------------------------------------------------------------------
# compute_distribution — known statistics
# ----------------------------------------------------------------------


def test_compute_distribution_empty() -> None:
    s = compute_distribution([])
    assert s.count == 0
    assert s.mean is None
    assert s.median is None
    assert s.low_n_warning is True  # zero counts as low-N


def test_compute_distribution_singleton() -> None:
    s = compute_distribution([42])
    assert s.count == 1
    assert s.mean == 42.0
    assert s.median == 42.0
    assert s.stddev == 0.0
    assert s.low_n_warning is True


def test_compute_distribution_known_percentiles() -> None:
    """The classic [1..101] dataset has well-known percentiles."""
    values = list(range(1, 102))   # 1, 2, …, 101  (count=101)
    s = compute_distribution(values, facet="tens")
    assert s.count == 101
    assert s.median == 51.0
    assert s.p25 == 26.0  # 0.25 * 100 = 25 → index 25 (value 26)
    assert s.p75 == 76.0
    assert s.p90 == 91.0
    assert s.p95 == 96.0
    assert s.p99 == 100.0  # 0.99 * 100 = 99 → index 99 (value 100)
    assert s.mean == statistics.fmean(values)
    assert s.low_n_warning is False


def test_compute_distribution_low_n_warning_threshold() -> None:
    """Boundary check: N == 29 warns, N == 30 doesn't."""
    just_under = compute_distribution(list(range(LOW_N_THRESHOLD - 1)))
    at_threshold = compute_distribution(list(range(LOW_N_THRESHOLD)))
    assert just_under.low_n_warning is True
    assert at_threshold.low_n_warning is False


# ----------------------------------------------------------------------
# Facet resolution
# ----------------------------------------------------------------------


def test_resolve_gap_facet_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown gap slice-by"):
        resolve_gap_facet("frobnicate")


def test_resolve_interval_facet_rejects_gap_only_facet() -> None:
    """`severity` is gap-only; intervals should refuse it."""
    with pytest.raises(ValueError, match="unknown interval slice-by"):
        resolve_interval_facet("severity")


def test_supported_lists_align_with_facet_definitions() -> None:
    assert "tier" in supported_for_gaps()
    assert "tier" in supported_for_intervals()
    assert "severity" in supported_for_gaps()
    assert "severity" not in supported_for_intervals()
    assert "package" in supported_for_gaps()
    assert "package" not in supported_for_intervals()


# ----------------------------------------------------------------------
# gap_distribution end-to-end with synthetic data
# ----------------------------------------------------------------------


def _seed_gap_rows(
    settings: Settings,
    *,
    rhsa_id: str = "RHSA-2025:1",
    severity: str = "important",
    methodology_version: str = "v1",
    tier_gap_seconds: dict[str, list[int]] | None = None,
) -> None:
    """Insert one RHSA plus N gap_measurement rows per requested tier."""
    pub = datetime(2025, 1, 1, tzinfo=UTC).isoformat()
    now = datetime.now(UTC).isoformat()
    tier_gap_seconds = tier_gap_seconds or {}
    with connect(settings.db_path) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO rhsa
                 (rhsa_id, title, severity, published_at, source_url,
                  raw_json, collected_at)
               VALUES (?, 't', ?, ?, 'x', '{}', ?)""",
            (rhsa_id, severity, pub, pub),
        )
        for tier, gaps in tier_gap_seconds.items():
            for i, gap_c in enumerate(gaps):
                conn.execute(
                    """INSERT INTO gap_measurement
                         (rhsa_id, repository, tier, architecture,
                          package_name, fixed_version,
                          rhsa_published_at, repo_first_seen_at,
                          image_first_built_at, image_id,
                          gap_a_seconds, gap_b_seconds, gap_c_seconds,
                          computed_at, methodology_version)
                       VALUES (?, ?, ?, 'x86_64', 'p', '0:1-1.el9', ?,
                               NULL, NULL, NULL, NULL, NULL, ?, ?, ?)""",
                    (rhsa_id, f"{tier}/repo-{i}", tier, pub, gap_c,
                     now, methodology_version),
                )


def test_gap_distribution_overall(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    # 30 observations of Gap C; ensure threshold not warning
    _seed_gap_rows(
        settings,
        tier_gap_seconds={"ubi": list(range(1, 31))},
    )
    with connect(settings.db_path) as conn:
        stats = gap_distribution(conn, gap="C")
    assert len(stats) == 1
    s = stats[0]
    assert s.facet == "<overall>"
    assert s.count == 30
    assert s.median == 15.5
    assert s.low_n_warning is False


def test_gap_distribution_sliced_by_tier(tmp_path: Path) -> None:
    """Headline-facet test: ubi/ocp_platform fast; rh_layered slow."""
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_gap_rows(
        settings,
        tier_gap_seconds={
            "ubi": [5, 5, 5, 5, 5],
            "ocp_platform": [7, 7, 7, 7, 7],
            "rh_layered": [26, 26, 26, 26, 26],
        },
    )
    with connect(settings.db_path) as conn:
        stats = gap_distribution(conn, gap="C", slice_by="tier")
    by_tier = {s.facet: s for s in stats}
    assert set(by_tier) == {"ubi", "ocp_platform", "rh_layered"}
    assert by_tier["ubi"].median == 5
    assert by_tier["ocp_platform"].median == 7
    assert by_tier["rh_layered"].median == 26
    # Spike's qualitative finding direction is reproduced:
    assert by_tier["ubi"].median < by_tier["rh_layered"].median
    assert by_tier["ocp_platform"].median < by_tier["rh_layered"].median
    # All slices have N=5 → low-N warning
    assert all(s.low_n_warning for s in stats)


def test_gap_distribution_sliced_by_severity(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_gap_rows(
        settings, rhsa_id="RHSA-2025:1", severity="critical",
        tier_gap_seconds={"ubi": [10, 12, 14]},
    )
    _seed_gap_rows(
        settings, rhsa_id="RHSA-2025:2", severity="low",
        tier_gap_seconds={"ubi": [50, 60, 70]},
    )
    with connect(settings.db_path) as conn:
        stats = gap_distribution(conn, gap="C", slice_by="severity")
    by_sev = {s.facet: s for s in stats}
    assert by_sev["critical"].median == 12
    assert by_sev["low"].median == 60


def test_gap_distribution_filters_to_methodology_version(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_gap_rows(
        settings, methodology_version="v1",
        tier_gap_seconds={"ubi": [1, 2, 3]},
    )
    _seed_gap_rows(
        settings, rhsa_id="RHSA-2025:2", methodology_version="v2-experiment",
        tier_gap_seconds={"ubi": [100, 200, 300]},
    )
    with connect(settings.db_path) as conn:
        v1 = gap_distribution(conn, gap="C", methodology_version="v1")
        v2 = gap_distribution(conn, gap="C", methodology_version="v2-experiment")
    assert v1[0].median == 2
    assert v2[0].median == 200


def test_gap_distribution_tier_filter(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_gap_rows(
        settings,
        tier_gap_seconds={
            "ubi": [1, 2, 3],
            "rh_layered": [99, 99, 99],
        },
    )
    with connect(settings.db_path) as conn:
        stats = gap_distribution(conn, gap="C", tier="ubi")
    assert len(stats) == 1 and stats[0].count == 3
    assert stats[0].median == 2


def test_gap_distribution_top_n(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_gap_rows(
        settings,
        tier_gap_seconds={
            "ubi":          list(range(10)),       # 10 obs
            "ocp_platform": list(range(5)),        # 5
            "rh_layered":   list(range(20)),       # 20
        },
    )
    with connect(settings.db_path) as conn:
        stats = gap_distribution(conn, gap="C", slice_by="tier", top_n=2)
    facets = [s.facet for s in stats]
    assert facets == ["rh_layered", "ubi"]  # top two by N, then alpha


def test_gap_distribution_excludes_null_values(tmp_path: Path) -> None:
    """Rows with NULL gap_c don't contribute to the distribution."""
    settings = _settings(tmp_path)
    _init_db(settings)
    pub = datetime(2025, 1, 1, tzinfo=UTC).isoformat()
    now = datetime.now(UTC).isoformat()
    with connect(settings.db_path) as conn:
        conn.execute(
            """INSERT INTO rhsa
                 (rhsa_id, title, severity, published_at, source_url,
                  raw_json, collected_at)
               VALUES ('RHSA-X', 't', 'important', ?, 'x', '{}', ?)""",
            (pub, pub),
        )
        for gap_c in (10, 20, None, 30, None):
            conn.execute(
                """INSERT INTO gap_measurement
                     (rhsa_id, repository, tier, architecture,
                      package_name, fixed_version,
                      rhsa_published_at, repo_first_seen_at,
                      image_first_built_at, image_id,
                      gap_a_seconds, gap_b_seconds, gap_c_seconds,
                      computed_at, methodology_version)
                   VALUES ('RHSA-X', 'r', 'ubi', 'x86_64', 'p', 'v',
                           ?, NULL, NULL, NULL, NULL, NULL, ?, ?, 'v1')""",
                (pub, gap_c, now),
            )
        stats = gap_distribution(conn, gap="C")
    assert stats[0].count == 3   # NULLs filtered out
    assert stats[0].median == 20


# ----------------------------------------------------------------------
# interval_distribution
# ----------------------------------------------------------------------


def _seed_interval_rows(settings: Settings, rows: list[tuple]) -> None:
    """rows = [(repository, tier, arch, interval_seconds, next_dt), …]."""
    now = datetime.now(UTC).isoformat()
    with connect(settings.db_path) as conn:
        for i, (repo, tier, arch, interval, next_dt) in enumerate(rows):
            prior_id = f"prior-{i}"
            next_id = f"next-{i}"
            prior_dt = (next_dt - timedelta(seconds=interval)).isoformat()
            # Need parent container_image rows for FK
            for img_id, dt in ((prior_id, prior_dt), (next_id, next_dt.isoformat())):
                conn.execute(
                    """INSERT INTO container_image
                         (image_id, source, registry, repository, tier, tag,
                          digest, architecture, build_date, raw_json,
                          collected_at)
                       VALUES (?, 'catalog', 'r', ?, ?, 't', 'd', ?, ?, '{}', ?)""",
                    (img_id, repo, tier, arch, dt, now),
                )
            conn.execute(
                """INSERT INTO rebuild_interval
                     (repository, tier, architecture,
                      prior_image_id, next_image_id,
                      prior_build_date, next_build_date,
                      interval_seconds, computed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (repo, tier, arch, prior_id, next_id,
                 prior_dt, next_dt.isoformat(), interval, now),
            )


def test_interval_distribution_overall_and_sliced(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    base = datetime(2025, 1, 1, tzinfo=UTC)
    _seed_interval_rows(
        settings,
        [
            ("ubi9/ubi", "ubi", "x86_64", 5 * 86400, base + timedelta(days=5)),
            ("ubi9/ubi", "ubi", "x86_64", 6 * 86400, base + timedelta(days=11)),
            ("ubi9/ubi", "ubi", "x86_64", 5 * 86400, base + timedelta(days=16)),
            ("rhacm2/console-rhel9", "rh_layered", "x86_64",
             26 * 86400, base + timedelta(days=26)),
            ("rhacm2/console-rhel9", "rh_layered", "x86_64",
             30 * 86400, base + timedelta(days=56)),
        ],
    )

    with connect(settings.db_path) as conn:
        overall = interval_distribution(conn)
        by_tier = interval_distribution(conn, slice_by="tier")

    assert overall[0].count == 5
    by_tier_map = {s.facet: s for s in by_tier}
    assert by_tier_map["ubi"].count == 3
    assert by_tier_map["rh_layered"].count == 2
    # UBI is the faster cadence
    assert by_tier_map["ubi"].median < by_tier_map["rh_layered"].median


def test_interval_distribution_top_n(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    base = datetime(2025, 1, 1, tzinfo=UTC)
    # 3 repos with different N each: keep only the top 2
    repos = [("a", 5), ("b", 3), ("c", 8)]
    rows: list[tuple] = []
    for repo, n in repos:
        for i in range(n):
            rows.append((repo, "ubi", "x86_64", 86400,
                         base + timedelta(days=i + 1)))
    _seed_interval_rows(settings, rows)
    with connect(settings.db_path) as conn:
        stats = interval_distribution(conn, slice_by="repository", top_n=2)
    facets = [s.facet for s in stats]
    assert facets == ["c", "a"]


def test_interval_distribution_tier_filter(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    base = datetime(2025, 1, 1, tzinfo=UTC)
    _seed_interval_rows(
        settings,
        [
            ("ubi9/ubi", "ubi", "x86_64", 5 * 86400, base + timedelta(days=5)),
            ("ubi9/ubi", "ubi", "x86_64", 7 * 86400, base + timedelta(days=12)),
            ("rhacm2/console-rhel9", "rh_layered", "x86_64",
             40 * 86400, base + timedelta(days=40)),
        ],
    )
    with connect(settings.db_path) as conn:
        ubi = interval_distribution(conn, tier="ubi")
    assert ubi[0].count == 2
    assert ubi[0].median == 6 * 86400


# ----------------------------------------------------------------------
# DistributionStats.as_dict round-trip (JSON/CSV consumers depend on it)
# ----------------------------------------------------------------------


def test_distribution_stats_as_dict_contains_all_fields() -> None:
    s = compute_distribution([1, 2, 3])
    d = s.as_dict()
    for key in ("facet", "count", "mean", "stddev", "median",
                "p25", "p75", "p90", "p95", "p99", "low_n_warning"):
        assert key in d
