"""Inter-build interval reconstruction (WP-09 deliverable).

For each ``(repository, architecture)`` in ``container_image``, sort by
``build_date`` and emit one ``rebuild_interval`` row per consecutive pair.

Idempotent: a re-run deletes every row in ``rebuild_interval`` and rewrites
the table from current ``container_image`` state. The schema does not carry
a methodology version on this table — interval computation is mechanical,
not interpretive — so a full rewrite is the right policy.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class IntervalRow:
    repository: str
    tier: str
    architecture: str
    prior_image_id: str
    next_image_id: str
    prior_build_date: str  # ISO 8601
    next_build_date: str
    interval_seconds: int


def _parse_iso(value: str) -> datetime:
    """Parse the ISO-8601 timestamps we wrote. Tolerates ``Z`` suffix."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def compute_intervals(conn: sqlite3.Connection) -> list[IntervalRow]:
    """Return one ``IntervalRow`` per consecutive pair across all repos/arches."""
    rows = conn.execute(
        """
        SELECT repository, tier, architecture, image_id, build_date
          FROM container_image
         ORDER BY repository, architecture, build_date, image_id
        """
    ).fetchall()

    out: list[IntervalRow] = []
    prev_key: tuple[str, str] | None = None
    prev_image_id: str | None = None
    prev_tier: str | None = None
    prev_build_date: str | None = None

    for repo, tier, arch, image_id, build_date in rows:
        key = (repo, arch)
        if key != prev_key:
            prev_key = key
            prev_image_id = image_id
            prev_tier = tier
            prev_build_date = build_date
            continue

        try:
            prev_dt = _parse_iso(prev_build_date or "")
            next_dt = _parse_iso(build_date)
        except (ValueError, TypeError):
            log.warning(
                "intervals.unparseable_timestamp",
                repository=repo,
                prior=prev_build_date,
                next=build_date,
            )
            prev_image_id = image_id
            prev_tier = tier
            prev_build_date = build_date
            continue

        delta = int((next_dt - prev_dt).total_seconds())
        if delta < 0:
            # ORDER BY should have prevented this; defensively skip.
            log.warning(
                "intervals.negative_delta",
                repository=repo,
                prior=prev_build_date,
                next=build_date,
            )
        else:
            out.append(
                IntervalRow(
                    repository=repo,
                    tier=prev_tier or tier,
                    architecture=arch,
                    prior_image_id=prev_image_id or "",
                    next_image_id=image_id,
                    prior_build_date=prev_build_date or "",
                    next_build_date=build_date,
                    interval_seconds=delta,
                )
            )

        prev_image_id = image_id
        prev_tier = tier
        prev_build_date = build_date

    return out


def persist_intervals(
    conn: sqlite3.Connection,
    intervals: list[IntervalRow],
    *,
    computed_at: datetime,
) -> None:
    """Replace the entire ``rebuild_interval`` table in one transaction."""
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM rebuild_interval")
        conn.executemany(
            """
            INSERT INTO rebuild_interval (
                repository, tier, architecture,
                prior_image_id, next_image_id,
                prior_build_date, next_build_date,
                interval_seconds, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    iv.repository, iv.tier, iv.architecture,
                    iv.prior_image_id, iv.next_image_id,
                    iv.prior_build_date, iv.next_build_date,
                    iv.interval_seconds, computed_at.isoformat(),
                )
                for iv in intervals
            ],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def reconstruct_intervals(conn: sqlite3.Connection) -> int:
    """Public entry point. Returns the number of intervals persisted."""
    intervals = compute_intervals(conn)
    persist_intervals(conn, intervals, computed_at=datetime.now(UTC))
    log.info("intervals.reconstructed", count=len(intervals))
    return len(intervals)


def interval_distribution(
    conn: sqlite3.Connection,
    *,
    slice_by: str | None = None,
    tier: str | None = None,
    top_n: int | None = None,
) -> list:
    """Aggregate ``rebuild_interval.interval_seconds`` into per-facet stats.

    Returns a list of ``DistributionStats`` (imported lazily to avoid a
    cycle with :mod:`cadence.analysis.slice`).
    """
    from cadence.analysis.slice import (
        DistributionStats,
        compute_distribution,
        resolve_interval_facet,
    )

    facet = resolve_interval_facet(slice_by)
    sql_parts = [
        f"SELECT {facet.select_expr} AS facet_value, i.interval_seconds AS value",
        "  FROM rebuild_interval AS i",
        " WHERE 1=1",
    ]
    params: list[object] = []
    if tier:
        sql_parts.append("   AND i.tier = ?")
        params.append(tier)
    sql = "\n".join(sql_parts)

    bucket: dict[str, list[float]] = {}
    for facet_value, value in conn.execute(sql, params).fetchall():
        key = "<null>" if facet_value is None else str(facet_value)
        bucket.setdefault(key, []).append(float(value))

    stats: list[DistributionStats] = [
        compute_distribution(values, facet=key)
        for key, values in bucket.items()
    ]
    stats.sort(key=lambda s: (-s.count, s.facet))
    if top_n is not None and slice_by is not None:
        stats = stats[:top_n]
    return stats


__all__ = [
    "IntervalRow",
    "compute_intervals",
    "interval_distribution",
    "persist_intervals",
    "reconstruct_intervals",
]
