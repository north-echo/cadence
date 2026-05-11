"""WP-11 publication charts.

The eight chart deliverables from CADENCE-SPEC.md §WP-11:

1. **headline_gap_c_by_tier** — box plot of Gap C by tier (the multi-hop
   finding).
2. **histogram_gap_c_overall** — histogram of Gap C across all rows.
3. **cdf_abc** — overlaid CDFs of Gap A, B, C.
4. **interval_by_tier** — box plot of inter-build interval by tier.
5. **gap_c_by_severity** — box plot of Gap C by RHSA severity.
6. **interval_monthly_median_by_tier** — time series of monthly median
   inter-build interval per tier (the "rebuild cadence accelerated"
   finding).
7. **gap_c_heatmap_package_month** — top-20 packages x month heatmap of
   Gap C medians.
8. **gap_c_by_architecture** — box plot of Gap C by architecture.

Each chart emits ``{slug}.png`` (matplotlib, 300 DPI) and ``{slug}.html``
(plotly). Days are the headline unit for gap and interval charts; raw
seconds remain in the database.

Palette
-------

Categorical charts (tier, severity, architecture) use the
**Okabe-Ito** colourblind-safe palette. The heatmap and the CDF use
matplotlib/plotly's perceptual continuous colormaps (``viridis``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Force a non-interactive backend so charts render the same on every host
# (including CI without a display).
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import structlog

from cadence.analysis.reconstruct import DEFAULT_METHODOLOGY_VERSION

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------


OKABE_ITO: tuple[str, ...] = (
    "#000000",  # black
    "#E69F00",  # orange
    "#56B4E9",  # sky blue
    "#009E73",  # bluish green
    "#F0E442",  # yellow
    "#0072B2",  # blue
    "#D55E00",  # vermillion
    "#CC79A7",  # reddish purple
)


SECONDS_PER_DAY = 86_400.0


def _to_days(seconds: float | int) -> float:
    return float(seconds) / SECONDS_PER_DAY


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------


def _gap_values_by(
    conn: sqlite3.Connection,
    *,
    gap_column: str,
    facet_expr: str,
    extra_join: str = "",
    methodology_version: str = DEFAULT_METHODOLOGY_VERSION,
    tier_filter: str | None = None,
) -> dict[str, list[float]]:
    sql = [
        f"SELECT {facet_expr} AS f, {gap_column}",
        "  FROM gap_measurement AS g",
        extra_join,
        " WHERE g.methodology_version = ?",
        f"   AND {gap_column} IS NOT NULL",
    ]
    params: list[Any] = [methodology_version]
    if tier_filter:
        sql.append("   AND g.tier = ?")
        params.append(tier_filter)
    bucket: dict[str, list[float]] = {}
    for facet, value in conn.execute("\n".join(sql), params).fetchall():
        key = "<null>" if facet is None else str(facet)
        bucket.setdefault(key, []).append(float(value))
    return bucket


def _interval_values_by(
    conn: sqlite3.Connection, *, facet_expr: str = "i.tier"
) -> dict[str, list[float]]:
    sql = f"SELECT {facet_expr} AS f, i.interval_seconds FROM rebuild_interval AS i"
    bucket: dict[str, list[float]] = {}
    for facet, value in conn.execute(sql).fetchall():
        key = "<null>" if facet is None else str(facet)
        bucket.setdefault(key, []).append(float(value))
    return bucket


def _monthly_interval_median_by_tier(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, float]]:
    """Returns ``{tier: {YYYY-MM: median_days}}``."""
    raw: dict[str, dict[str, list[float]]] = {}
    for tier, month, secs in conn.execute(
        """
        SELECT i.tier, substr(i.next_build_date, 1, 7), i.interval_seconds
          FROM rebuild_interval AS i
        """
    ).fetchall():
        if not tier or not month:
            continue
        raw.setdefault(str(tier), {}).setdefault(str(month), []).append(float(secs))
    return {
        tier: {month: float(np.median(values)) / SECONDS_PER_DAY
               for month, values in months.items()}
        for tier, months in raw.items()
    }


def _heatmap_package_month_gap_c(
    conn: sqlite3.Connection,
    *,
    top_n: int = 20,
    methodology_version: str = DEFAULT_METHODOLOGY_VERSION,
) -> tuple[list[str], list[str], list[list[float | None]]]:
    """Returns (packages, months, matrix-of-median-Gap-C-in-days)."""
    rows = conn.execute(
        """
        SELECT g.package_name,
               substr(g.rhsa_published_at, 1, 7) AS month,
               g.gap_c_seconds
          FROM gap_measurement AS g
         WHERE g.methodology_version = ?
           AND g.gap_c_seconds IS NOT NULL
        """,
        (methodology_version,),
    ).fetchall()

    pkg_counts: dict[str, int] = {}
    cell_values: dict[tuple[str, str], list[float]] = {}
    for pkg, month, secs in rows:
        if not pkg or not month:
            continue
        pkg_counts[pkg] = pkg_counts.get(pkg, 0) + 1
        cell_values.setdefault((pkg, month), []).append(float(secs))

    top_pkgs = sorted(pkg_counts, key=lambda p: (-pkg_counts[p], p))[:top_n]
    months = sorted({month for (_, month) in cell_values})

    matrix: list[list[float | None]] = []
    for pkg in top_pkgs:
        row: list[float | None] = []
        for month in months:
            values = cell_values.get((pkg, month))
            row.append(float(np.median(values)) / SECONDS_PER_DAY if values else None)
        matrix.append(row)
    return top_pkgs, months, matrix


# ---------------------------------------------------------------------------
# Rendering primitives
# ---------------------------------------------------------------------------


@dataclass
class ChartOutputs:
    """One chart's output paths, suitable for cross-referencing in markdown."""

    slug: str
    title: str
    png: Path
    html: Path
    skipped_empty: bool = False


def _categorical_box(
    data: dict[str, list[float]],
    *,
    output_dir: Path,
    slug: str,
    title: str,
    ylabel: str,
    xlabel: str,
    category_order: list[str] | None = None,
) -> ChartOutputs:
    if not data:
        return _empty_chart(output_dir=output_dir, slug=slug, title=title)
    labels = (
        [c for c in category_order if c in data]
        if category_order
        else sorted(data, key=lambda k: -len(data[k]))
    )
    values = [[_to_days(v) for v in data[k]] for k in labels]

    fig, ax = plt.subplots(figsize=(8, 5))
    bp = ax.boxplot(values, tick_labels=labels, showfliers=False, patch_artist=True)
    for patch, colour in zip(bp["boxes"], _palette_for(len(labels)), strict=False):
        patch.set_facecolor(colour)
        patch.set_alpha(0.7)
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", linestyle=":", alpha=0.5)
    if max(len(label) for label in labels) > 8:
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
    fig.tight_layout()
    png_path = output_dir / f"{slug}.png"
    fig.savefig(png_path, dpi=300)
    plt.close(fig)

    html_path = _write_box_html(
        labels=labels, values=values, output=output_dir / f"{slug}.html",
        title=title, ylabel=ylabel, xlabel=xlabel,
    )
    return ChartOutputs(slug=slug, title=title, png=png_path, html=html_path)


def _palette_for(n: int) -> list[str]:
    if n <= len(OKABE_ITO):
        return list(OKABE_ITO[:n])
    return [OKABE_ITO[i % len(OKABE_ITO)] for i in range(n)]


def _write_box_html(
    *,
    labels: list[str],
    values: list[list[float]],
    output: Path,
    title: str,
    ylabel: str,
    xlabel: str,
) -> Path:
    import plotly.graph_objects as go

    palette = _palette_for(len(labels))
    fig = go.Figure()
    for label, vals, colour in zip(labels, values, palette, strict=False):
        fig.add_trace(go.Box(
            y=vals, name=label, marker_color=colour, boxpoints=False,
        ))
    fig.update_layout(
        title=title, xaxis_title=xlabel, yaxis_title=ylabel,
        showlegend=False, template="plotly_white",
    )
    fig.write_html(output, include_plotlyjs="cdn")
    return output


def _empty_chart(
    *, output_dir: Path, slug: str, title: str
) -> ChartOutputs:
    """Emit a placeholder image saying "no data" so downstream artefacts exist."""
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.text(0.5, 0.5, "no data", ha="center", va="center",
            transform=ax.transAxes, fontsize=20, color="gray")
    ax.set_title(title)
    ax.set_xticks([])
    ax.set_yticks([])
    png_path = output_dir / f"{slug}.png"
    fig.savefig(png_path, dpi=300)
    plt.close(fig)

    html_path = output_dir / f"{slug}.html"
    html_path.write_text(
        f"<!doctype html><meta charset='utf-8'><title>{title}</title>"
        f"<body><h1>{title}</h1><p>No data available.</p></body>",
        encoding="utf-8",
    )
    return ChartOutputs(
        slug=slug, title=title, png=png_path, html=html_path, skipped_empty=True
    )


# ---------------------------------------------------------------------------
# Individual charts
# ---------------------------------------------------------------------------


TIER_ORDER = [
    "ubi", "ocp_platform", "rh_layered",
    "quay_redhat", "quay_community", "quay_partner",
]
SEVERITY_ORDER = ["critical", "important", "moderate", "low"]


def chart_headline_gap_c_by_tier(
    conn: sqlite3.Connection, output_dir: Path,
    *, methodology_version: str = DEFAULT_METHODOLOGY_VERSION,
) -> ChartOutputs:
    data = _gap_values_by(
        conn, gap_column="g.gap_c_seconds", facet_expr="g.tier",
        methodology_version=methodology_version,
    )
    return _categorical_box(
        data, output_dir=output_dir, slug="headline_gap_c_by_tier",
        title="Gap C (RHSA → first downstream image) by tier",
        xlabel="Tier", ylabel="Gap C (days)",
        category_order=TIER_ORDER,
    )


def chart_histogram_gap_c_overall(
    conn: sqlite3.Connection, output_dir: Path,
    *, methodology_version: str = DEFAULT_METHODOLOGY_VERSION,
) -> ChartOutputs:
    rows = conn.execute(
        """SELECT gap_c_seconds FROM gap_measurement
            WHERE gap_c_seconds IS NOT NULL AND methodology_version = ?""",
        (methodology_version,),
    ).fetchall()
    values = [_to_days(r[0]) for r in rows]
    slug = "histogram_gap_c_overall"
    title = "Gap C distribution across all observations"
    if not values:
        return _empty_chart(output_dir=output_dir, slug=slug, title=title)

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.hist(values, bins=30, color=OKABE_ITO[2], edgecolor="black", alpha=0.8)
    ax.set_xlabel("Gap C (days)")
    ax.set_ylabel("Count")
    ax.set_title(title)
    ax.grid(axis="y", linestyle=":", alpha=0.5)
    fig.tight_layout()
    png = output_dir / f"{slug}.png"
    fig.savefig(png, dpi=300)
    plt.close(fig)

    import plotly.graph_objects as go
    figh = go.Figure(go.Histogram(x=values, marker_color=OKABE_ITO[2], nbinsx=30))
    figh.update_layout(
        title=title, xaxis_title="Gap C (days)", yaxis_title="Count",
        template="plotly_white",
    )
    html = output_dir / f"{slug}.html"
    figh.write_html(html, include_plotlyjs="cdn")
    return ChartOutputs(slug=slug, title=title, png=png, html=html)


def chart_cdf_abc(
    conn: sqlite3.Connection, output_dir: Path,
    *, methodology_version: str = DEFAULT_METHODOLOGY_VERSION,
) -> ChartOutputs:
    slug = "cdf_abc"
    title = "Gap A / B / C cumulative distribution"
    series: dict[str, list[float]] = {}
    for label, column in (("Gap A", "gap_a_seconds"),
                          ("Gap B", "gap_b_seconds"),
                          ("Gap C", "gap_c_seconds")):
        rows = conn.execute(
            f"""SELECT {column} FROM gap_measurement
                 WHERE {column} IS NOT NULL AND methodology_version = ?""",
            (methodology_version,),
        ).fetchall()
        if rows:
            series[label] = sorted(_to_days(r[0]) for r in rows)
    if not series:
        return _empty_chart(output_dir=output_dir, slug=slug, title=title)

    fig, ax = plt.subplots(figsize=(8, 5))
    for (label, vals), colour in zip(series.items(),
                                     (OKABE_ITO[1], OKABE_ITO[2], OKABE_ITO[3]),
                                     strict=False):
        ys = np.arange(1, len(vals) + 1) / len(vals)
        ax.plot(vals, ys, label=label, color=colour, linewidth=2)
    ax.set_xlabel("Gap (days)")
    ax.set_ylabel("Cumulative fraction")
    ax.set_title(title)
    ax.set_ylim(0, 1.0)
    ax.grid(linestyle=":", alpha=0.5)
    ax.legend()
    fig.tight_layout()
    png = output_dir / f"{slug}.png"
    fig.savefig(png, dpi=300)
    plt.close(fig)

    import plotly.graph_objects as go
    figh = go.Figure()
    for label, vals in series.items():
        ys = list(np.arange(1, len(vals) + 1) / len(vals))
        figh.add_trace(go.Scatter(x=vals, y=ys, mode="lines", name=label))
    figh.update_layout(
        title=title, xaxis_title="Gap (days)", yaxis_title="Cumulative fraction",
        template="plotly_white",
    )
    html = output_dir / f"{slug}.html"
    figh.write_html(html, include_plotlyjs="cdn")
    return ChartOutputs(slug=slug, title=title, png=png, html=html)


def chart_interval_by_tier(
    conn: sqlite3.Connection, output_dir: Path,
) -> ChartOutputs:
    return _categorical_box(
        _interval_values_by(conn),
        output_dir=output_dir, slug="interval_by_tier",
        title="Inter-build interval by tier",
        xlabel="Tier", ylabel="Inter-build interval (days)",
        category_order=TIER_ORDER,
    )


def chart_gap_c_by_severity(
    conn: sqlite3.Connection, output_dir: Path,
    *, methodology_version: str = DEFAULT_METHODOLOGY_VERSION,
) -> ChartOutputs:
    data = _gap_values_by(
        conn, gap_column="g.gap_c_seconds", facet_expr="r.severity",
        extra_join="JOIN rhsa AS r ON r.rhsa_id = g.rhsa_id",
        methodology_version=methodology_version,
    )
    return _categorical_box(
        data, output_dir=output_dir, slug="gap_c_by_severity",
        title="Gap C by RHSA severity",
        xlabel="Severity", ylabel="Gap C (days)",
        category_order=SEVERITY_ORDER,
    )


def chart_interval_monthly_median_by_tier(
    conn: sqlite3.Connection, output_dir: Path,
) -> ChartOutputs:
    slug = "interval_monthly_median_by_tier"
    title = "Monthly median inter-build interval by tier"
    by_tier = _monthly_interval_median_by_tier(conn)
    if not by_tier:
        return _empty_chart(output_dir=output_dir, slug=slug, title=title)

    ordered_tiers = [t for t in TIER_ORDER if t in by_tier] + [
        t for t in by_tier if t not in TIER_ORDER
    ]
    all_months = sorted({m for tm in by_tier.values() for m in tm})

    fig, ax = plt.subplots(figsize=(10, 5))
    palette = _palette_for(len(ordered_tiers))
    for tier, colour in zip(ordered_tiers, palette, strict=False):
        months = sorted(by_tier[tier])
        values = [by_tier[tier][m] for m in months]
        ax.plot(months, values, marker="o", label=tier, color=colour, linewidth=2)
    ax.set_xlabel("Month")
    ax.set_ylabel("Median inter-build interval (days)")
    ax.set_title(title)
    if len(all_months) > 10:
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    ax.grid(linestyle=":", alpha=0.5)
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    png = output_dir / f"{slug}.png"
    fig.savefig(png, dpi=300)
    plt.close(fig)

    import plotly.graph_objects as go
    figh = go.Figure()
    for tier, colour in zip(ordered_tiers, palette, strict=False):
        months = sorted(by_tier[tier])
        figh.add_trace(go.Scatter(
            x=months, y=[by_tier[tier][m] for m in months],
            mode="lines+markers", name=tier, line=dict(color=colour),
        ))
    figh.update_layout(
        title=title, xaxis_title="Month",
        yaxis_title="Median inter-build interval (days)",
        template="plotly_white",
    )
    html = output_dir / f"{slug}.html"
    figh.write_html(html, include_plotlyjs="cdn")
    return ChartOutputs(slug=slug, title=title, png=png, html=html)


def chart_gap_c_heatmap_package_month(
    conn: sqlite3.Connection, output_dir: Path,
    *, methodology_version: str = DEFAULT_METHODOLOGY_VERSION,
) -> ChartOutputs:
    slug = "gap_c_heatmap_package_month"
    title = "Median Gap C — top 20 packages x month"
    pkgs, months, matrix = _heatmap_package_month_gap_c(
        conn, top_n=20, methodology_version=methodology_version
    )
    if not pkgs or not months:
        return _empty_chart(output_dir=output_dir, slug=slug, title=title)

    arr = np.array(
        [[np.nan if v is None else v for v in row] for row in matrix], dtype=float
    )
    fig, ax = plt.subplots(figsize=(max(8, len(months) * 0.4),
                                    max(4, len(pkgs) * 0.4)))
    im = ax.imshow(arr, aspect="auto", cmap="viridis")
    ax.set_xticks(range(len(months)))
    ax.set_xticklabels(months, rotation=45, ha="right")
    ax.set_yticks(range(len(pkgs)))
    ax.set_yticklabels(pkgs)
    ax.set_title(title)
    cb = fig.colorbar(im, ax=ax)
    cb.set_label("Median Gap C (days)")
    fig.tight_layout()
    png = output_dir / f"{slug}.png"
    fig.savefig(png, dpi=300)
    plt.close(fig)

    import plotly.graph_objects as go
    figh = go.Figure(go.Heatmap(
        z=[[None if v is None else v for v in row] for row in matrix],
        x=months, y=pkgs, colorscale="Viridis",
        colorbar=dict(title="Median Gap C (days)"),
    ))
    figh.update_layout(title=title, template="plotly_white",
                       xaxis_title="Month", yaxis_title="Package")
    html = output_dir / f"{slug}.html"
    figh.write_html(html, include_plotlyjs="cdn")
    return ChartOutputs(slug=slug, title=title, png=png, html=html)


def chart_gap_c_by_architecture(
    conn: sqlite3.Connection, output_dir: Path,
    *, methodology_version: str = DEFAULT_METHODOLOGY_VERSION,
) -> ChartOutputs:
    data = _gap_values_by(
        conn, gap_column="g.gap_c_seconds", facet_expr="g.architecture",
        methodology_version=methodology_version,
    )
    return _categorical_box(
        data, output_dir=output_dir, slug="gap_c_by_architecture",
        title="Gap C by architecture",
        xlabel="Architecture", ylabel="Gap C (days)",
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def all_chart_functions() -> tuple:
    return (
        chart_headline_gap_c_by_tier,
        chart_histogram_gap_c_overall,
        chart_cdf_abc,
        chart_interval_by_tier,
        chart_gap_c_by_severity,
        chart_interval_monthly_median_by_tier,
        chart_gap_c_heatmap_package_month,
        chart_gap_c_by_architecture,
    )


def render_all(
    conn: sqlite3.Connection,
    output_dir: Path,
    *,
    methodology_version: str = DEFAULT_METHODOLOGY_VERSION,
) -> list[ChartOutputs]:
    """Render every WP-11 chart into ``output_dir``."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out: list[ChartOutputs] = []
    for fn in all_chart_functions():
        # Each chart fetcher takes care of its own filter on
        # methodology_version (interval charts ignore it; gap charts honour
        # it).
        try:
            result = _invoke(fn, conn, output_dir, methodology_version)
        except Exception:
            log.exception("charts.render_failed", chart=fn.__name__)
            raise
        out.append(result)
    log.info(
        "charts.render_all_done",
        charts=len(out),
        empty=sum(1 for c in out if c.skipped_empty),
        output_dir=str(output_dir),
    )
    return out


def _invoke(fn, conn, output_dir, methodology_version):
    # Some chart functions take methodology_version as a kwarg; others don't.
    import inspect

    params = inspect.signature(fn).parameters
    if "methodology_version" in params:
        return fn(conn, output_dir, methodology_version=methodology_version)
    return fn(conn, output_dir)


__all__: Iterable[str] = (
    "OKABE_ITO",
    "ChartOutputs",
    "all_chart_functions",
    "chart_cdf_abc",
    "chart_gap_c_by_architecture",
    "chart_gap_c_by_severity",
    "chart_gap_c_heatmap_package_month",
    "chart_headline_gap_c_by_tier",
    "chart_histogram_gap_c_overall",
    "chart_interval_by_tier",
    "chart_interval_monthly_median_by_tier",
    "render_all",
)
