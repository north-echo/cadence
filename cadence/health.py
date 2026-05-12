"""Collector health check (WP-14).

Queries ``collection_run`` for the most recent successful run of each
source and reports staleness against per-source expected cadences. The
CLI exits non-zero when any tracked source has been silent for more than
twice its expected interval (the spec's definition of "stale").

Used by both:

* ``cadence health`` — human-readable Rich output, exit code reflects
  overall health.
* ``cadence metrics serve`` — the same data, scraped on each `/metrics`
  request and exposed as Prometheus gauges.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

# Cadence the WP-14 systemd timers ship with. Keep these in sync with
# `systemd/user/cadence-collect-*.timer`. If you change a timer's cadence
# in production, override the threshold here (e.g. via CADENCE_HEALTH_*).
EXPECTED_INTERVAL_SECONDS: dict[str, int] = {
    "rhsa": 4 * 3600,
    "csaf": 4 * 3600,
    "repodata": 4 * 3600,
    "catalog": 12 * 3600,
    "quay": 12 * 3600,
}

# Spec WP-14: "non-zero exit code if any source has been silent for more
# than 2x its expected interval."
STALENESS_MULTIPLIER = 2.0


@dataclass(frozen=True)
class SourceHealth:
    source: str
    last_success_at: str | None      # ISO 8601 of latest *successful* run
    last_records: int | None
    expected_interval_seconds: int
    age_seconds: float | None
    status: str                       # "ok" | "stale" | "never_ran"

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class HealthResult:
    checked_at: str
    sources: tuple[SourceHealth, ...]

    @property
    def overall_ok(self) -> bool:
        return all(s.ok for s in self.sources)


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _query_last_success(
    conn: sqlite3.Connection, source: str
) -> tuple[str, int] | None:
    """Return ``(completed_at_iso, records)`` for the latest successful run.

    A "successful" run mirrors the CLI's exit-code policy
    (``cadence.cli._finish_collect_run``): a run is failed only when it
    persisted *zero* records AND saw at least one per-record error. A run
    that persisted thousands of records alongside a handful of transient
    upstream 404s is healthy, not silent.
    """
    row = conn.execute(
        """
        SELECT completed_at, records
          FROM collection_run
         WHERE source = ? AND NOT (records = 0 AND errors > 0)
         ORDER BY completed_at DESC
         LIMIT 1
        """,
        (source,),
    ).fetchone()
    return (row[0], int(row[1])) if row else None


def health_check(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    expected_intervals: dict[str, int] = EXPECTED_INTERVAL_SECONDS,
    staleness_multiplier: float = STALENESS_MULTIPLIER,
) -> HealthResult:
    """Compute the per-source health snapshot.

    Parameters
    ----------
    now: timestamp to compare against. Defaults to ``datetime.now(UTC)``;
        passed explicitly by tests so behaviour is deterministic.
    """
    now = now or datetime.now(UTC)
    results: list[SourceHealth] = []
    for source, expected in expected_intervals.items():
        last = _query_last_success(conn, source)
        if last is None:
            results.append(SourceHealth(
                source=source, last_success_at=None, last_records=None,
                expected_interval_seconds=expected,
                age_seconds=None, status="never_ran",
            ))
            continue
        completed_at_iso, records = last
        age = (now - _parse_iso(completed_at_iso)).total_seconds()
        status = "stale" if age > expected * staleness_multiplier else "ok"
        results.append(SourceHealth(
            source=source, last_success_at=completed_at_iso,
            last_records=records,
            expected_interval_seconds=expected,
            age_seconds=age, status=status,
        ))
    return HealthResult(checked_at=now.isoformat(), sources=tuple(results))


def render_health(result: HealthResult, console) -> None:
    """Print ``result`` as a Rich table; intended for the CLI."""
    from rich.table import Table

    table = Table(
        title=f"CADENCE health @ {result.checked_at}",
        show_header=True, header_style="bold",
    )
    table.add_column("Source")
    table.add_column("Status")
    table.add_column("Last success", style="dim")
    table.add_column("Age", justify="right")
    table.add_column("Expected", justify="right")
    table.add_column("Last records", justify="right")
    for s in result.sources:
        if s.status == "ok":
            status_cell = "[green]ok[/green]"
        elif s.status == "stale":
            status_cell = "[red]stale[/red]"
        else:
            status_cell = "[yellow]never ran[/yellow]"
        age = f"{s.age_seconds / 3600:.1f}h" if s.age_seconds is not None else "—"
        last_at = s.last_success_at or "—"
        last_records = "—" if s.last_records is None else str(s.last_records)
        expected = f"{s.expected_interval_seconds / 3600:.0f}h"
        table.add_row(s.source, status_cell, last_at, age, expected, last_records)
    console.print(table)
    if not result.overall_ok:
        console.print(
            "[red]one or more sources are stale[/red] "
            "(exceeded 2x their expected cadence)."
        )


__all__ = [
    "EXPECTED_INTERVAL_SECONDS",
    "STALENESS_MULTIPLIER",
    "HealthResult",
    "SourceHealth",
    "health_check",
    "render_health",
]
