"""Slicing facets and distribution primitives for WP-10 analysis.

This module provides three things:

* :class:`DistributionStats` — the row shape every report uses. Holds the
  facet label and the spec-mandated descriptive statistics (count, mean,
  stddev, median, p25/p75/p90/p95/p99).
* :func:`compute_distribution` — turns a list of numbers into a
  :class:`DistributionStats`, warning when N is small enough that percentiles
  start losing meaning.
* :class:`Facet` and :data:`FACETS` — the menu of named slicing dimensions
  available to ``cadence analyze gaps`` and ``cadence analyze intervals``,
  with the SQL fragments and joins each requires.

Low-N warning
-------------

Spec WP-10: "Warns when N<30 for percentile calculation." We compute the
percentiles anyway (you can't compute them at all below the cut-points), but
flag the slice via :attr:`DistributionStats.low_n_warning` so the CLI/JSON/
CSV consumers can mark it.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

LOW_N_THRESHOLD = 30


# ---------------------------------------------------------------------------
# DistributionStats
# ---------------------------------------------------------------------------


@dataclass
class DistributionStats:
    """One row of the analyze output: a facet value + summary statistics."""

    facet: str
    count: int = 0
    mean: float | None = None
    stddev: float | None = None
    median: float | None = None
    p25: float | None = None
    p75: float | None = None
    p90: float | None = None
    p95: float | None = None
    p99: float | None = None
    low_n_warning: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _quantile(sorted_values: list[float], q: float) -> float:
    """Return the ``q``-th percentile of an already-sorted list, q ∈ [0, 1].

    Uses linear interpolation between the two surrounding samples — the same
    method ``numpy.percentile(..., method='linear')`` uses, and the de facto
    standard for the "p25 / p90" reporting CADENCE produces.
    """
    if not sorted_values:
        raise ValueError("quantile of empty list")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return float(sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac)


def compute_distribution(
    values: Iterable[float | int],
    *,
    facet: str = "",
) -> DistributionStats:
    """Summarize a list of observations as a :class:`DistributionStats` row."""
    nums = sorted(float(v) for v in values)
    n = len(nums)
    out = DistributionStats(facet=facet, count=n)
    if n == 0:
        out.low_n_warning = True
        return out
    out.mean = statistics.fmean(nums)
    out.stddev = statistics.pstdev(nums) if n >= 2 else 0.0
    out.median = _quantile(nums, 0.50)
    out.p25 = _quantile(nums, 0.25)
    out.p75 = _quantile(nums, 0.75)
    out.p90 = _quantile(nums, 0.90)
    out.p95 = _quantile(nums, 0.95)
    out.p99 = _quantile(nums, 0.99)
    out.low_n_warning = n < LOW_N_THRESHOLD
    return out


# ---------------------------------------------------------------------------
# Facet menu
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Facet:
    """One named slicing dimension.

    The ``gap_select`` / ``interval_select`` strings are the SQL fragments
    that produce the facet column in a ``SELECT … FROM …`` against
    :class:`gap_measurement` / :class:`rebuild_interval` respectively, with
    any required JOIN expressed via :attr:`extra_join`. Either fragment may
    be empty if the facet doesn't apply to that side.
    """

    name: str
    description: str
    gap_select: str = ""
    interval_select: str = ""
    extra_join: str = ""


def _ubi_major_case(column: str) -> str:
    """SQL CASE that pulls the UBI major version from a repository name."""
    return (
        f"CASE "
        f"WHEN {column} LIKE 'ubi8/%' THEN '8' "
        f"WHEN {column} LIKE 'ubi9/%' THEN '9' "
        f"WHEN {column} LIKE 'ubi10/%' THEN '10' "
        f"ELSE NULL END"
    )


# Facet definitions ---------------------------------------------------------

FACETS: dict[str, Facet] = {
    "tier": Facet(
        name="tier",
        description="Tier (ubi / ocp_platform / rh_layered / quay_*).",
        gap_select="g.tier",
        interval_select="i.tier",
    ),
    "severity": Facet(
        name="severity",
        description="RHSA severity (critical/important/moderate/low).",
        gap_select="r.severity",
        extra_join="JOIN rhsa AS r ON r.rhsa_id = g.rhsa_id",
        # severity has no analogue for intervals; left empty.
    ),
    "repository": Facet(
        name="repository",
        description="Container repository (image variant).",
        gap_select="g.repository",
        interval_select="i.repository",
    ),
    "ubi_major": Facet(
        name="ubi_major",
        description="UBI major version (8/9/10), derived from repository.",
        gap_select=_ubi_major_case("g.repository"),
        interval_select=_ubi_major_case("i.repository"),
    ),
    "architecture": Facet(
        name="architecture",
        description="Architecture (x86_64/aarch64/…).",
        gap_select="g.architecture",
        interval_select="i.architecture",
    ),
    "package": Facet(
        name="package",
        description="Fixed package name (top-N supported via --top).",
        gap_select="g.package_name",
        # No package analogue for intervals (intervals are image-level).
    ),
    "month": Facet(
        name="month",
        description="Calendar month (YYYY-MM) of RHSA pub / next build.",
        gap_select="substr(g.rhsa_published_at, 1, 7)",
        interval_select="substr(i.next_build_date, 1, 7)",
    ),
    "dow": Facet(
        name="dow",
        description="Day of week (0=Sun … 6=Sat).",
        gap_select="strftime('%w', g.rhsa_published_at)",
        interval_select="strftime('%w', i.next_build_date)",
    ),
    "dom": Facet(
        name="dom",
        description="Day of month (01..31).",
        gap_select="strftime('%d', g.rhsa_published_at)",
        interval_select="strftime('%d', i.next_build_date)",
    ),
}


def supported_for_gaps() -> list[str]:
    return [name for name, f in FACETS.items() if f.gap_select]


def supported_for_intervals() -> list[str]:
    return [name for name, f in FACETS.items() if f.interval_select]


@dataclass
class FacetResolution:
    """Resolved query pieces for a facet, ready to splice into SELECT/JOIN."""

    select_expr: str
    join_clause: str = ""
    overall_label: str = field(default="<overall>")


def resolve_gap_facet(slice_by: str | None) -> FacetResolution:
    if slice_by is None:
        return FacetResolution(select_expr="'<overall>'")
    facet = FACETS.get(slice_by)
    if facet is None or not facet.gap_select:
        raise ValueError(
            f"unknown gap slice-by: {slice_by!r}; "
            f"supported: {supported_for_gaps()}"
        )
    return FacetResolution(
        select_expr=facet.gap_select, join_clause=facet.extra_join
    )


def resolve_interval_facet(slice_by: str | None) -> FacetResolution:
    if slice_by is None:
        return FacetResolution(select_expr="'<overall>'")
    facet = FACETS.get(slice_by)
    if facet is None or not facet.interval_select:
        raise ValueError(
            f"unknown interval slice-by: {slice_by!r}; "
            f"supported: {supported_for_intervals()}"
        )
    return FacetResolution(select_expr=facet.interval_select)


__all__ = [
    "FACETS",
    "LOW_N_THRESHOLD",
    "DistributionStats",
    "Facet",
    "FacetResolution",
    "compute_distribution",
    "resolve_gap_facet",
    "resolve_interval_facet",
    "supported_for_gaps",
    "supported_for_intervals",
]
