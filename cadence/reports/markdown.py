"""Comprehensive Markdown report generator (WP-11).

Produces a GitHub-renderable Markdown report. Chart images live in a
sibling ``charts/`` directory by default; the report references them with
relative paths so the bundle is self-contained.
"""

from __future__ import annotations

import io
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from cadence.analysis.gaps import gap_distribution
from cadence.analysis.intervals import interval_distribution
from cadence.analysis.reconstruct import DEFAULT_METHODOLOGY_VERSION
from cadence.analysis.slice import DistributionStats

SECONDS_PER_DAY = 86_400.0


def _days(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value / SECONDS_PER_DAY:,.1f}"


def _md_table(rows: list[DistributionStats], facet_label: str) -> str:
    if not rows:
        return "*No data.*\n"
    buf = io.StringIO()
    buf.write(
        f"| {facet_label} | N | median (d) | p25 | p75 | p90 | p99 | mean | "
        "low N? |\n"
    )
    buf.write(
        "|---|---:|---:|---:|---:|---:|---:|---:|:---:|\n"
    )
    for s in rows:
        warn = "⚠️" if s.low_n_warning else ""
        buf.write(
            f"| {s.facet} | {s.count} | {_days(s.median)} | {_days(s.p25)} | "
            f"{_days(s.p75)} | {_days(s.p90)} | {_days(s.p99)} | "
            f"{_days(s.mean)} | {warn} |\n"
        )
    return buf.getvalue()


def render_markdown(
    conn: sqlite3.Connection,
    output_path: Path,
    *,
    methodology_version: str = DEFAULT_METHODOLOGY_VERSION,
    charts_dir_relative: str = "charts",
) -> Path:
    """Write a full Markdown report to ``output_path``.

    The report references charts under ``charts_dir_relative`` — by default
    ``./charts`` next to the report — so `cadence report charts` should be
    invoked with the same parent directory.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    def chart(path: str, alt: str) -> str:
        return f"![{alt}]({charts_dir_relative}/{path})\n"

    lines: list[str] = []
    lines.append("# CADENCE patch-latency report\n")
    lines.append(
        f"*Generated {now} from methodology version `{methodology_version}`.*\n"
    )
    lines.append(
        "**Container Supply Chain Patch Latency Measurement** — see "
        "`CADENCE-SPEC.md` for project context and `docs/methodology.md` for "
        "the analysis approach.\n"
    )

    # 1. Headline ---------------------------------------------------------
    lines.append("## 1. Headline finding\n")
    lines.append(
        "End-to-end Gap C (RHSA publication → first downstream container "
        "image carrying the fix) varies sharply across product tiers. UBI "
        "and the OpenShift platform sit near the floor; layered Red Hat "
        "products and Quay-hosted content add real time.\n"
    )
    lines.append(chart("headline_gap_c_by_tier.png", "Gap C by tier"))
    by_tier = gap_distribution(
        conn, gap="C", slice_by="tier", methodology_version=methodology_version
    )
    lines.append(_md_table(by_tier, "Tier"))
    lines.append("\n")

    # 2. Gap C distribution -----------------------------------------------
    lines.append("## 2. Gap C distribution\n")
    lines.append(chart("histogram_gap_c_overall.png", "Gap C histogram"))
    lines.append(chart("cdf_abc.png", "A/B/C CDF overlay"))
    overall = gap_distribution(
        conn, gap="C", methodology_version=methodology_version
    )
    if overall:
        lines.append("**Overall Gap C:**\n\n")
        lines.append(_md_table(overall, "Scope"))
    lines.append("\n")

    # 3. Gap C by severity / architecture --------------------------------
    lines.append("## 3. Gap C by severity\n")
    lines.append(chart("gap_c_by_severity.png", "Gap C by severity"))
    rows = gap_distribution(
        conn, gap="C", slice_by="severity",
        methodology_version=methodology_version,
    )
    lines.append(_md_table(rows, "Severity"))
    lines.append("\n")

    lines.append("## 4. Gap C by architecture\n")
    lines.append(chart("gap_c_by_architecture.png", "Gap C by architecture"))
    rows = gap_distribution(
        conn, gap="C", slice_by="architecture",
        methodology_version=methodology_version,
    )
    lines.append(_md_table(rows, "Arch"))
    lines.append("\n")

    # 4. Inter-build interval --------------------------------------------
    lines.append("## 5. Inter-build interval\n")
    lines.append(chart("interval_by_tier.png", "Interval by tier"))
    lines.append(chart(
        "interval_monthly_median_by_tier.png", "Monthly median interval by tier"
    ))
    lines.append(_md_table(interval_distribution(conn, slice_by="tier"), "Tier"))
    lines.append("\n")

    # 5. Heatmap ----------------------------------------------------------
    lines.append("## 6. Top-20 packages x month — median Gap C\n")
    lines.append(chart(
        "gap_c_heatmap_package_month.png",
        "Gap C heatmap, top 20 packages x month",
    ))
    lines.append(
        "Yellow cells indicate longer Gap C; missing cells are months in "
        "which the package received no observed fix.\n"
    )

    # 6. About ------------------------------------------------------------
    lines.append("## 7. About this report\n")
    lines.append(
        "- Methodology version: `" + methodology_version + "`\n"
        "- All durations are reported in days; raw values are stored in "
        "seconds (`docs/data-dictionary.md`).\n"
        "- Slices with N < 30 are marked with ⚠️ — interpret percentiles "
        "cautiously.\n"
        "- Reproduction: `cadence analyze reconstruct --methodology-version "
        + methodology_version
        + "` followed by `cadence report markdown --output …` and "
        "`cadence report charts --output-dir …`.\n"
    )

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


__all__ = ["render_markdown"]
