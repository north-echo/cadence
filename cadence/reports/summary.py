"""Rich-formatted CLI summary report (WP-11)."""

from __future__ import annotations

import sqlite3

from rich.console import Console
from rich.table import Table

from cadence.analysis.gaps import gap_distribution
from cadence.analysis.intervals import interval_distribution
from cadence.analysis.reconstruct import DEFAULT_METHODOLOGY_VERSION
from cadence.analysis.slice import DistributionStats

SECONDS_PER_DAY = 86_400.0


def _days(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value / SECONDS_PER_DAY:,.1f}d"


def _stats_table(
    title: str, rows: list[DistributionStats], facet_label: str
) -> Table:
    table = Table(title=title, show_header=True, header_style="bold")
    table.add_column(facet_label)
    table.add_column("N", justify="right")
    table.add_column("median", justify="right")
    table.add_column("p25", justify="right")
    table.add_column("p75", justify="right")
    table.add_column("p90", justify="right")
    table.add_column("p99", justify="right")
    table.add_column("mean", justify="right")
    for s in rows:
        warn = " [yellow]⚠[/yellow]" if s.low_n_warning else ""
        table.add_row(
            f"{s.facet}{warn}", str(s.count),
            _days(s.median), _days(s.p25), _days(s.p75),
            _days(s.p90), _days(s.p99), _days(s.mean),
        )
    return table


def render_summary(
    conn: sqlite3.Connection,
    console: Console,
    *,
    methodology_version: str = DEFAULT_METHODOLOGY_VERSION,
) -> None:
    """Print the full WP-11 CLI summary: gaps + intervals across major slices."""
    console.rule("[bold]CADENCE summary[/bold]")
    console.print(
        f"Methodology version: [cyan]{methodology_version}[/cyan]"
    )

    # Headline first — the multi-tier finding.
    console.print()
    console.print(_stats_table(
        "Gap C by tier (RHSA → first downstream image)",
        gap_distribution(conn, gap="C", slice_by="tier",
                         methodology_version=methodology_version),
        "Tier",
    ))

    # Overall gap distributions
    for gap in ("A", "B", "C"):
        rows = gap_distribution(
            conn, gap=gap, methodology_version=methodology_version
        )
        if rows:
            console.print(_stats_table(f"Gap {gap} overall", rows, "Scope"))

    # Severity + arch + ubi_major slices for Gap C
    for slice_by, label in (("severity", "Severity"),
                            ("architecture", "Architecture"),
                            ("ubi_major", "UBI major")):
        try:
            rows = gap_distribution(
                conn, gap="C", slice_by=slice_by,
                methodology_version=methodology_version,
            )
        except ValueError:
            continue
        if rows:
            console.print(_stats_table(
                f"Gap C by {label.lower()}", rows, label
            ))

    # Top-10 most-patched packages
    rows = gap_distribution(
        conn, gap="C", slice_by="package",
        methodology_version=methodology_version, top_n=10,
    )
    if rows:
        console.print(_stats_table(
            "Gap C — top 10 packages by observation count", rows, "Package"
        ))

    console.rule("[bold]Inter-build interval[/bold]")
    console.print(_stats_table(
        "Inter-build interval overall",
        interval_distribution(conn), "Scope",
    ))
    console.print(_stats_table(
        "Inter-build interval by tier",
        interval_distribution(conn, slice_by="tier"), "Tier",
    ))


__all__ = ["render_summary"]
