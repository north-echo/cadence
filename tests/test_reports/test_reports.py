"""Tests for cadence.reports — WP-11 acceptance.

The chart-rendering tests use matplotlib's Agg backend (forced inside
``cadence.reports.charts``) and only check that the expected files were
written, not that they look correct pixel-by-pixel. Visual review is
manual.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from cadence.config import Settings
from cadence.db import apply_migrations, connect
from cadence.reports.charts import all_chart_functions, render_all
from cadence.reports.markdown import render_markdown
from cadence.reports.summary import render_summary


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


def _seed_population(settings: Settings) -> None:
    """Synthetic data covering every tier/severity/arch the charts care about."""
    pub = datetime(2025, 1, 15, tzinfo=UTC)
    now = datetime.now(UTC).isoformat()
    with connect(settings.db_path) as conn:
        # Two RHSAs at different severities, two months apart
        for rhsa, sev, month in (
            ("RHSA-2025:1", "critical", 1), ("RHSA-2025:2", "low", 3)
        ):
            pub_dt = (pub + timedelta(days=(month - 1) * 30)).isoformat()
            conn.execute(
                """INSERT INTO rhsa
                     (rhsa_id, title, severity, published_at, source_url,
                      raw_json, collected_at)
                   VALUES (?, ?, ?, ?, 'x', '{}', ?)""",
                (rhsa, f"Test {rhsa}", sev, pub_dt, pub_dt),
            )
            # 60 gap_measurement rows per RHSA across the tiers/arches we
            # care about, so percentile slices have N >= 30 in most places.
            for tier, gap_days in [("ubi", 5), ("ocp_platform", 7),
                                   ("rh_layered", 26), ("quay_community", 14)]:
                for arch in ("x86_64", "aarch64"):
                    for k in range(8):
                        gap_c = (gap_days + (k % 3) - 1) * 86400
                        repo = f"{tier}/pkg-{k % 4}"
                        conn.execute(
                            """INSERT INTO gap_measurement
                                 (rhsa_id, repository, tier, architecture,
                                  package_name, fixed_version,
                                  rhsa_published_at, repo_first_seen_at,
                                  image_first_built_at, image_id,
                                  gap_a_seconds, gap_b_seconds, gap_c_seconds,
                                  computed_at, methodology_version)
                               VALUES (?, ?, ?, ?, ?, '0:1-1.el9', ?,
                                       NULL, NULL, NULL,
                                       ?, ?, ?, ?, 'v1')""",
                            (
                                rhsa, repo, tier, arch,
                                f"pkg-{k % 4}", pub_dt,
                                86400 * 1, 86400 * 2, gap_c,
                                now,
                            ),
                        )

        # Inter-build intervals for a couple of (repo, arch) groups across
        # several months so the time-series chart has something to draw.
        base = pub - timedelta(days=120)
        for tier, repo, days in (("ubi", "ubi9/ubi", 5),
                                 ("rh_layered", "rhacm2/console-rhel9", 26)):
            for arch in ("x86_64", "aarch64"):
                last = base
                for j in range(10):
                    nxt = last + timedelta(days=days)
                    for img_id, dt in (
                        (f"{tier}-{arch}-{j}-a", last.isoformat()),
                        (f"{tier}-{arch}-{j}-b", nxt.isoformat()),
                    ):
                        conn.execute(
                            """INSERT INTO container_image
                                 (image_id, source, registry, repository,
                                  tier, tag, digest, architecture, build_date,
                                  raw_json, collected_at)
                               VALUES (?, 'catalog', 'r.r.com', ?, ?, 't',
                                       'd', ?, ?, '{}', ?)""",
                            (img_id, repo, tier, arch, dt, now),
                        )
                    conn.execute(
                        """INSERT INTO rebuild_interval
                             (repository, tier, architecture,
                              prior_image_id, next_image_id,
                              prior_build_date, next_build_date,
                              interval_seconds, computed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (repo, tier, arch,
                         f"{tier}-{arch}-{j}-a", f"{tier}-{arch}-{j}-b",
                         last.isoformat(), nxt.isoformat(),
                         days * 86400, now),
                    )
                    last = nxt


# ----------------------------------------------------------------------
# render_summary
# ----------------------------------------------------------------------


def test_summary_runs_and_emits_known_sections(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_population(settings)
    buf = StringIO()
    console = Console(file=buf, width=160, force_terminal=False, color_system=None)
    with connect(settings.db_path) as conn:
        render_summary(conn, console)
    out = buf.getvalue()
    # Headline tier table present
    assert "Gap C by tier" in out
    # Other distributions
    assert "Gap C overall" in out or "Gap A overall" in out
    assert "severity" in out.lower()
    assert "Inter-build interval" in out


# ----------------------------------------------------------------------
# render_markdown
# ----------------------------------------------------------------------


def test_markdown_writes_self_contained_report(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_population(settings)
    out = tmp_path / "report.md"
    with connect(settings.db_path) as conn:
        render_markdown(conn, out)
    text = out.read_text()
    # Headline + at least one chart reference per spec
    assert text.startswith("# CADENCE patch-latency report")
    assert "headline_gap_c_by_tier.png" in text
    assert "gap_c_by_severity.png" in text
    assert "interval_by_tier.png" in text
    # GitHub-renderable image references are relative
    assert "(charts/" in text
    # Markdown tables present (pipes + dashes)
    assert "| Tier |" in text
    assert "|---|" in text or "|---:" in text


def test_markdown_respects_custom_charts_dir(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_population(settings)
    out = tmp_path / "report.md"
    with connect(settings.db_path) as conn:
        render_markdown(conn, out, charts_dir_relative="figures")
    text = out.read_text()
    assert "(figures/headline_gap_c_by_tier.png)" in text
    assert "(charts/" not in text


# ----------------------------------------------------------------------
# render_all — charts
# ----------------------------------------------------------------------


_EXPECTED_SLUGS = [
    "headline_gap_c_by_tier",
    "histogram_gap_c_overall",
    "cdf_abc",
    "interval_by_tier",
    "gap_c_by_severity",
    "interval_monthly_median_by_tier",
    "gap_c_heatmap_package_month",
    "gap_c_by_architecture",
]


def test_render_all_produces_eight_charts_per_format(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_population(settings)
    out = tmp_path / "charts"
    with connect(settings.db_path) as conn:
        results = render_all(conn, out)
    slugs = {r.slug for r in results}
    assert slugs == set(_EXPECTED_SLUGS)
    for slug in _EXPECTED_SLUGS:
        png = out / f"{slug}.png"
        html = out / f"{slug}.html"
        assert png.exists() and png.stat().st_size > 1024, slug
        assert html.exists() and html.stat().st_size > 256, slug
    # And the chart count matches all_chart_functions
    assert len(results) == len(all_chart_functions())


def test_render_all_on_empty_db_emits_placeholders(tmp_path: Path) -> None:
    """Per acceptance: reports run successfully even when there's no data."""
    settings = _settings(tmp_path)
    _init_db(settings)
    out = tmp_path / "charts"
    with connect(settings.db_path) as conn:
        results = render_all(conn, out)
    assert len(results) == len(_EXPECTED_SLUGS)
    # Every chart is an empty placeholder
    assert all(r.skipped_empty for r in results)
    for r in results:
        assert r.png.exists()
        assert r.html.exists()


def test_chart_image_has_correct_dpi(tmp_path: Path) -> None:
    """300 DPI per spec WP-11."""
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_population(settings)
    out = tmp_path / "charts"
    with connect(settings.db_path) as conn:
        render_all(conn, out)
    # Use PIL via matplotlib (pillow is a matplotlib transitive dep) to read DPI.
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("Pillow not installed")
    img = Image.open(out / "headline_gap_c_by_tier.png")
    dpi = img.info.get("dpi")
    assert dpi is not None
    assert round(dpi[0]) == 300, f"expected 300 DPI, got {dpi}"
