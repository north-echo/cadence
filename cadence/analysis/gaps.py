"""Gap distributions and slicing (WP-10 deliverable).

Reads the rows ``cadence/analysis/reconstruct.py`` wrote into
``gap_measurement`` and aggregates them into per-facet
:class:`DistributionStats`. Only non-NULL gap values participate in
percentile calculations; a row whose Gap A is NULL but Gap C is not still
contributes to the Gap C distribution.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from cadence.analysis.reconstruct import DEFAULT_METHODOLOGY_VERSION
from cadence.analysis.slice import (
    DistributionStats,
    compute_distribution,
    resolve_gap_facet,
)

GapName = Literal["A", "B", "C"]

_GAP_COLUMN = {
    "A": "g.gap_a_seconds",
    "B": "g.gap_b_seconds",
    "C": "g.gap_c_seconds",
}


def gap_distribution(
    conn: sqlite3.Connection,
    *,
    gap: GapName = "C",
    slice_by: str | None = None,
    methodology_version: str = DEFAULT_METHODOLOGY_VERSION,
    tier: str | None = None,
    top_n: int | None = None,
) -> list[DistributionStats]:
    """Aggregate Gap A/B/C values into :class:`DistributionStats` per facet.

    Parameters
    ----------
    gap: which gap column to summarize.
    slice_by: facet name (see ``cadence.analysis.slice.FACETS``) or ``None``
        for an overall single-row summary.
    methodology_version: only rows tagged with this version are included.
    tier: restrict to a single tier (post-filter).
    top_n: when slicing by package or repository, keep only the N facets
        with the most observations.
    """
    if gap not in _GAP_COLUMN:
        raise ValueError(f"unknown gap: {gap!r}")
    facet = resolve_gap_facet(slice_by)
    gap_column = _GAP_COLUMN[gap]

    sql_parts = [
        f"SELECT {facet.select_expr} AS facet_value, {gap_column} AS value",
        "  FROM gap_measurement AS g",
        facet.join_clause,
        " WHERE g.methodology_version = ?",
        f"   AND {gap_column} IS NOT NULL",
    ]
    params: list[object] = [methodology_version]
    if tier:
        sql_parts.append("   AND g.tier = ?")
        params.append(tier)

    sql = "\n".join(p for p in sql_parts if p)

    bucket: dict[str, list[float]] = {}
    for facet_value, value in conn.execute(sql, params).fetchall():
        key = "<null>" if facet_value is None else str(facet_value)
        bucket.setdefault(key, []).append(float(value))

    stats = [
        compute_distribution(values, facet=key)
        for key, values in bucket.items()
    ]
    stats.sort(key=lambda s: (-s.count, s.facet))
    if top_n is not None and slice_by is not None:
        stats = stats[:top_n]
    return stats


__all__ = ["GapName", "gap_distribution"]
