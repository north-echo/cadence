"""Gap-A/B/C reconstruction from collected raw tables (WP-09 deliverable).

For each ``(rhsa_id, repository, architecture, package_name, fixed_version)``
we look up:

* the earliest ``repo_package`` row (per UBI repodata observation) whose
  ``(package_name, arch)`` matches and whose version is ``>=`` the fixed
  version — this is **Gap A**'s anchor (RPM available in cdn-ubi);
* the earliest ``container_image`` for that repository/arch whose
  ``container_image_rpm`` carries the fixed package at ``>=`` version — this
  is **Gap B**'s right edge and the source for **Gap C**.

Gap definitions
---------------

* ``gap_a_seconds`` = ``repo_first_seen_at - rhsa.published_at``
* ``gap_b_seconds`` = ``image_first_built_at - repo_first_seen_at``
* ``gap_c_seconds`` = ``image_first_built_at - rhsa.published_at`` (end-to-end)

Any of these can be ``NULL`` (recorded as ``None``):

* Gap A is NULL when no UBI repodata observation has the fix at-or-above
  the fixed version. Most often because the fix landed before our forward
  polling started (CADENCE-SPEC.md §13.5 — Gap A is forward-only).
* Gap B is NULL when either side is missing.
* Gap C is NULL when no image-with-the-fix has been observed. Always NULL
  for Quay images (no RPM manifests in v1; see methodology.md §11).

Edge cases handled
------------------

* Per-architecture: each (rhsa, repo, package, arch) is its own row.
* VEX ``not_affected`` exclusion: when ``rhsa_vex`` says a product is
  ``not_affected``, no row is emitted for fixes against that product.
* RHSAs whose fix never appears in our data: row emitted with NULL gaps,
  so the dataset records the *existence* of the unobserved fix.
* RHSAs published before our earliest observation: same as above —
  Gap A is NULL.

Cross-validation
----------------

For every (image_id, advisory_id) in ``catalog_advisory_mapping`` we check
whether our computation also bound that RHSA→image. The match rate is
reported but never gates the run; the legacy mapping field is a
cross-validation signal only (CADENCE-SPEC.md §1, §WP-09).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from cadence.analysis.nevra import evr_ge

log = structlog.get_logger(__name__)


DEFAULT_METHODOLOGY_VERSION = "v1"


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ReconstructResult:
    methodology_version: str
    gap_rows_written: int = 0
    intervals_written: int = 0
    not_affected_skipped: int = 0
    cross_check_total: int = 0
    cross_check_matched: int = 0
    duration_seconds: float = 0.0

    @property
    def cross_check_match_rate(self) -> float | None:
        if self.cross_check_total == 0:
            return None
        return self.cross_check_matched / self.cross_check_total


@dataclass(frozen=True)
class _Fix:
    rhsa_id: str
    published_at: str
    package_name: str
    fixed_version: str
    fix_arch: str            # "src", "noarch", "x86_64", "aarch64", …
    product: str             # rhsa_package_fix.product (e.g. "AppStream-9.4.0.Z.EUS")


@dataclass(frozen=True)
class _TrackedTarget:
    repository: str
    tier: str
    source: str              # "catalog" | "quay"


@dataclass
class _GapInputs:
    """The four moments that, together, determine the three gaps for one row."""

    rhsa_published_at: str
    repo_first_seen_at: str | None = None
    image_first_built_at: str | None = None
    image_id: str | None = None


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def _seconds_between(later: str, earlier: str) -> int | None:
    try:
        a = datetime.fromisoformat(later.replace("Z", "+00:00"))
        b = datetime.fromisoformat(earlier.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return int((a - b).total_seconds())


def _arches_for_search(fix_arch: str) -> tuple[str, ...]:
    """Which container arches share an RPM with this fix entry.

    Source RPMs (``src``) don't ship in container images, so we skip them.
    Noarch RPMs apply to every container arch.
    """
    if fix_arch == "src":
        return ()
    if fix_arch == "noarch":
        return ("x86_64", "aarch64")
    return (fix_arch,)


def _fetch_fixes(conn: sqlite3.Connection) -> list[_Fix]:
    rows = conn.execute(
        """
        SELECT r.rhsa_id, r.published_at, pf.package_name, pf.fixed_version,
               pf.arch, pf.product
          FROM rhsa AS r
          JOIN rhsa_package_fix AS pf USING (rhsa_id)
        """
    ).fetchall()
    return [_Fix(*row) for row in rows]


def _fetch_tracked_targets(conn: sqlite3.Connection) -> list[_TrackedTarget]:
    rows = conn.execute(
        "SELECT repository, tier, source FROM tracked_repository"
    ).fetchall()
    return [_TrackedTarget(*row) for row in rows]


def _fetch_not_affected_products(
    conn: sqlite3.Connection,
) -> set[tuple[str, str]]:
    """Return ``{(rhsa_id, product), …}`` for every ``not_affected`` VEX row."""
    rows = conn.execute(
        """
        SELECT rhsa_id, product_id
          FROM rhsa_vex
         WHERE status = 'not_affected'
        """
    ).fetchall()
    out: set[tuple[str, str]] = set()
    for rhsa_id, product_id in rows:
        # rhsa_vex.product_id is "PRODUCT:NVRA"; we only need PRODUCT here.
        product = product_id.split(":", 1)[0]
        out.add((rhsa_id, product))
    return out


def _repo_first_seen_at(
    conn: sqlite3.Connection, *, package_name: str, fix_arch: str, fixed_version: str
) -> str | None:
    """Earliest UBI repo_observation in which this fix's RPM became visible."""
    rows = conn.execute(
        """
        SELECT obs.observed_at, rp.version
          FROM repo_package AS rp
          JOIN repo_observation AS obs ON obs.id = rp.observation_id
         WHERE rp.package_name = ? AND rp.arch = ?
         ORDER BY obs.observed_at
        """,
        (package_name, fix_arch),
    ).fetchall()
    for observed_at, observed_version in rows:
        if evr_ge(observed_version, fixed_version):
            return observed_at
    return None


def _image_first_built_at(
    conn: sqlite3.Connection,
    *,
    repository: str,
    arch: str,
    package_name: str,
    fixed_version: str,
) -> tuple[str, str] | None:
    """Earliest container_image carrying ``>= fixed_version`` of ``package_name``.

    Returns ``(image_id, build_date)`` or ``None``.
    """
    rows = conn.execute(
        """
        SELECT ci.image_id, ci.build_date, ir.version
          FROM container_image AS ci
          JOIN container_image_rpm AS ir USING (image_id)
         WHERE ci.repository = ? AND ci.architecture = ? AND ir.package_name = ?
           AND ir.arch IN (?, 'noarch')
         ORDER BY ci.build_date
        """,
        (repository, arch, package_name, arch),
    ).fetchall()
    for image_id, build_date, observed_version in rows:
        if evr_ge(observed_version, fixed_version):
            return image_id, build_date
    return None


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------


DEFAULT_BATCH_SIZE = 10_000


def iter_gap_rows(
    conn: sqlite3.Connection,
    *,
    methodology_version: str,
    result: ReconstructResult,
) -> Iterator[tuple]:
    """Stream gap_measurement rows for every (RHSA, fix, tracked-repo, arch).

    Yields tuples whose positions match the INSERT in :func:`persist`. Using a
    generator keeps memory bounded — the materialised list approach used in
    earlier versions allocated O(N) Python tuples (~75 M on a full UBI
    backfill, ~6 GB by 38 minutes) and would OOM the OptiPlex.
    """
    fixes = _fetch_fixes(conn)
    targets = _fetch_tracked_targets(conn)
    not_affected = _fetch_not_affected_products(conn)
    now_iso = datetime.now(UTC).isoformat()

    for fix in fixes:
        if (fix.rhsa_id, fix.product) in not_affected:
            result.not_affected_skipped += 1
            continue
        search_arches = _arches_for_search(fix.fix_arch)
        if not search_arches:
            continue
        repo_seen_cache: dict[str, str | None] = {}

        for target in targets:
            for arch in search_arches:
                inputs = _GapInputs(rhsa_published_at=fix.published_at)

                if arch not in repo_seen_cache:
                    repo_seen_cache[arch] = _repo_first_seen_at(
                        conn,
                        package_name=fix.package_name,
                        fix_arch=arch,
                        fixed_version=fix.fixed_version,
                    )
                inputs.repo_first_seen_at = repo_seen_cache[arch]

                if target.source == "catalog":
                    found = _image_first_built_at(
                        conn,
                        repository=target.repository,
                        arch=arch,
                        package_name=fix.package_name,
                        fixed_version=fix.fixed_version,
                    )
                    if found is not None:
                        inputs.image_id, inputs.image_first_built_at = found

                gap_a = (
                    _seconds_between(inputs.repo_first_seen_at, inputs.rhsa_published_at)
                    if inputs.repo_first_seen_at
                    else None
                )
                gap_b = (
                    _seconds_between(inputs.image_first_built_at, inputs.repo_first_seen_at)
                    if inputs.image_first_built_at and inputs.repo_first_seen_at
                    else None
                )
                gap_c = (
                    _seconds_between(inputs.image_first_built_at, inputs.rhsa_published_at)
                    if inputs.image_first_built_at
                    else None
                )

                yield (
                    fix.rhsa_id, target.repository, target.tier, arch,
                    fix.package_name, fix.fixed_version,
                    inputs.rhsa_published_at,
                    inputs.repo_first_seen_at, inputs.image_first_built_at,
                    inputs.image_id,
                    gap_a, gap_b, gap_c,
                    now_iso, methodology_version,
                )


def persist(
    conn: sqlite3.Connection,
    rows: Iterable[tuple],
    *,
    methodology_version: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Replace gap_measurement rows for the given methodology version.

    Drains ``rows`` (any iterable, typically the :func:`iter_gap_rows`
    generator) into batched ``executemany`` calls, all inside a single
    ``BEGIN/COMMIT``. Peak memory is O(batch_size) regardless of total
    row count.
    """
    _insert_sql = """
        INSERT INTO gap_measurement (
            rhsa_id, repository, tier, architecture,
            package_name, fixed_version,
            rhsa_published_at, repo_first_seen_at, image_first_built_at,
            image_id,
            gap_a_seconds, gap_b_seconds, gap_c_seconds,
            computed_at, methodology_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    conn.execute("BEGIN")
    try:
        conn.execute(
            "DELETE FROM gap_measurement WHERE methodology_version = ?",
            (methodology_version,),
        )
        total = 0
        batch: list[tuple] = []
        for row in rows:
            batch.append(row)
            if len(batch) >= batch_size:
                conn.executemany(_insert_sql, batch)
                total += len(batch)
                batch.clear()
        if batch:
            conn.executemany(_insert_sql, batch)
            total += len(batch)
        conn.execute("COMMIT")
        return total
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Cross-validation against the legacy advisory_rpm_mapping field
# ---------------------------------------------------------------------------


def cross_check(
    conn: sqlite3.Connection,
    *,
    methodology_version: str,
    result: ReconstructResult,
) -> None:
    """Compare our (rhsa→image) pairs against ``catalog_advisory_mapping``.

    Records counts on the ``ReconstructResult``; the match rate is exposed via
    :attr:`ReconstructResult.cross_check_match_rate`.
    """
    legacy = conn.execute(
        "SELECT DISTINCT image_id, advisory_id FROM catalog_advisory_mapping"
    ).fetchall()
    if not legacy:
        return

    computed = {
        (image_id, rhsa_id)
        for image_id, rhsa_id in conn.execute(
            """
            SELECT DISTINCT image_id, rhsa_id
              FROM gap_measurement
             WHERE image_id IS NOT NULL
               AND methodology_version = ?
            """,
            (methodology_version,),
        ).fetchall()
    }

    matched = 0
    for image_id, advisory_id in legacy:
        result.cross_check_total += 1
        if (image_id, advisory_id) in computed:
            matched += 1
        else:
            log.debug(
                "reconstruct.legacy_mapping_unmatched",
                image_id=image_id,
                advisory_id=advisory_id,
            )
    result.cross_check_matched = matched
    log.info(
        "reconstruct.cross_check",
        total=result.cross_check_total,
        matched=result.cross_check_matched,
        match_rate=result.cross_check_match_rate,
    )


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def reconstruct(
    conn: sqlite3.Connection,
    *,
    methodology_version: str = DEFAULT_METHODOLOGY_VERSION,
) -> ReconstructResult:
    """Run the full WP-09 reconstruction pipeline."""
    from cadence.analysis.intervals import reconstruct_intervals

    started = datetime.now(UTC)
    result = ReconstructResult(methodology_version=methodology_version)

    row_iter = iter_gap_rows(
        conn, methodology_version=methodology_version, result=result
    )
    result.gap_rows_written = persist(
        conn, row_iter, methodology_version=methodology_version
    )
    result.intervals_written = reconstruct_intervals(conn)
    cross_check(conn, methodology_version=methodology_version, result=result)

    result.duration_seconds = (datetime.now(UTC) - started).total_seconds()
    log.info(
        "reconstruct.done",
        methodology_version=methodology_version,
        gap_rows=result.gap_rows_written,
        intervals=result.intervals_written,
        not_affected_skipped=result.not_affected_skipped,
        cross_check_matched=result.cross_check_matched,
        cross_check_total=result.cross_check_total,
        duration_seconds=round(result.duration_seconds, 2),
    )
    return result


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_METHODOLOGY_VERSION",
    "ReconstructResult",
    "cross_check",
    "iter_gap_rows",
    "persist",
    "reconstruct",
]
