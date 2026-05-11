"""CSAF/VEX collector (WP-04).

Pulls CSAF v2 documents from Red Hat's bulk distribution and extracts VEX
statements into ``rhsa_vex``. The document served at WP-04's URL is the same
CSAF v2 advisory as the one WP-03 already fetches via the hydra API — only
the path is different — but we re-fetch via the spec'd URL so this collector
can be run standalone (e.g., to backfill VEX for RHSAs already collected).

VEX status mapping
------------------

CSAF v2 ``product_status`` keys are richer than the four buckets in
CADENCE's ``rhsa_vex.status`` enum. The mapping:

* ``fixed``                → ``fixed``
* ``first_fixed``          → ``fixed``
* ``known_affected``       → ``affected``
* ``first_affected``       → ``affected``
* ``last_affected``        → ``affected``
* ``known_not_affected``   → ``not_affected``
* ``under_investigation``  → ``under_investigation``
* ``recommended``          → skipped (this is a workaround pointer, not a
                              vulnerability state)

A note on reality: RHSA-level CSAF documents almost always carry only
``fixed`` statuses, because an RHSA exists to announce a fix. The other
three statuses live in CVE-level CSAF documents on the same bulk site
(out of scope for WP-04). Synthetic test fixtures exercise the parser
for all four buckets.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog

from cadence.collectors.base import BaseCollector, CollectionResult, HTTPClient
from cadence.config import Settings
from cadence.db import connect

log = structlog.get_logger(__name__)


# CSAF product_status key → CADENCE rhsa_vex.status bucket
_STATUS_MAP: dict[str, str] = {
    "fixed": "fixed",
    "first_fixed": "fixed",
    "known_affected": "affected",
    "first_affected": "affected",
    "last_affected": "affected",
    "known_not_affected": "not_affected",
    "under_investigation": "under_investigation",
}


# Bulk-distribution base. The /security/data/csaf/ path on access.redhat.com
# 301-redirects to security.access.redhat.com; the HTTPClient follows
# redirects transparently, so we keep the spec'd URL here.
_CSAF_URL_TEMPLATE = (
    "https://access.redhat.com/security/data/csaf/v2/advisories/{year}/{slug}.json"
)


def csaf_url_for(rhsa_id: str) -> str:
    """Return the bulk-distribution URL for ``rhsa_id``.

    Red Hat's path scheme is ``/v2/advisories/{year}/{rhsa-lowercase}.json``
    where the colon in the RHSA-ID becomes an underscore: e.g. ``RHSA-2025:0850``
    becomes ``rhsa-2025_0850.json`` under the ``2025/`` directory.
    """
    if not rhsa_id.startswith("RHSA-"):
        raise ValueError(f"not an RHSA id: {rhsa_id!r}")
    body = rhsa_id[len("RHSA-"):]
    if ":" not in body:
        raise ValueError(f"unexpected RHSA-ID format: {rhsa_id!r}")
    year, num = body.split(":", 1)
    slug = f"rhsa-{year}_{num}".lower()
    return _CSAF_URL_TEMPLATE.format(year=year, slug=slug)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VEXStatement:
    product_id: str
    status: str  # fixed | affected | not_affected | under_investigation
    justification: str | None


def _justification_for(
    flags: list[dict[str, Any]] | None,
    product_id: str,
) -> str | None:
    """Find a CSAF flag whose ``product_ids`` includes ``product_id``.

    Flag categories are documented short labels such as
    ``vulnerable_code_not_in_execute_path`` and
    ``component_not_present``. They are only relevant for the
    ``known_not_affected`` status, but we capture them whenever present.
    """
    if not flags:
        return None
    for flag in flags:
        product_ids = flag.get("product_ids") or []
        if product_id in product_ids:
            label = flag.get("label") or flag.get("category")
            if label:
                return str(label)
    return None


def extract_vex(doc: dict[str, Any]) -> list[VEXStatement]:
    """Walk every vulnerability's ``product_status`` and yield VEX statements.

    Duplicate ``(product_id, status)`` rows are de-duplicated; if more than
    one vulnerability in the same document yields the same row, the first
    non-empty ``justification`` wins.
    """
    seen: dict[tuple[str, str], VEXStatement] = {}
    for vuln in doc.get("vulnerabilities") or []:
        product_status = vuln.get("product_status") or {}
        flags = vuln.get("flags")
        for csaf_key, product_ids in product_status.items():
            bucket = _STATUS_MAP.get(csaf_key)
            if bucket is None:
                continue
            for product_id in product_ids or []:
                justification = _justification_for(flags, product_id)
                key = (product_id, bucket)
                existing = seen.get(key)
                if existing is None or (
                    existing.justification is None and justification is not None
                ):
                    seen[key] = VEXStatement(product_id, bucket, justification)
    return list(seen.values())


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist(
    conn: sqlite3.Connection, rhsa_id: str, statements: list[VEXStatement]
) -> None:
    """Replace the VEX statement set for ``rhsa_id`` atomically."""
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM rhsa_vex WHERE rhsa_id = ?", (rhsa_id,))
        conn.executemany(
            """
            INSERT INTO rhsa_vex (rhsa_id, product_id, status, justification)
            VALUES (?, ?, ?, ?)
            """,
            [
                (rhsa_id, s.product_id, s.status, s.justification)
                for s in statements
            ],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class CSAFCollector(BaseCollector):
    """Per-RHSA CSAF fetch + VEX persistence."""

    name = "csaf"

    def __init__(
        self,
        settings: Settings,
        db_path: Path,
        *,
        client: HTTPClient | None = None,
    ) -> None:
        super().__init__(settings, client=client)
        self.db_path = db_path

    async def collect(  # type: ignore[override]
        self,
        *,
        rhsa_ids: list[str] | None = None,
        all_known: bool = False,
        **_: Any,
    ) -> CollectionResult:
        started = datetime.now(UTC)
        result = CollectionResult(
            name=self.name, started_at=started, completed_at=started
        )

        with connect(self.db_path) as conn:
            targets = self._resolve_targets(conn, rhsa_ids=rhsa_ids, all_known=all_known)
            for rhsa_id in targets:
                try:
                    if await self._collect_one(conn, rhsa_id):
                        result.records += 1
                except Exception as exc:  # collector-wide isolation
                    msg = f"{rhsa_id}: {exc}"
                    result.errors.append(msg)
                    self.log.exception("csaf.collect_one_failed", rhsa_id=rhsa_id)

        result.completed_at = datetime.now(UTC)
        self.log.info(
            "csaf.collect_done",
            records=result.records,
            errors=len(result.errors),
            duration_seconds=round(result.duration_seconds, 2),
        )
        return result

    @staticmethod
    def _resolve_targets(
        conn: sqlite3.Connection,
        *,
        rhsa_ids: list[str] | None,
        all_known: bool,
    ) -> list[str]:
        if rhsa_ids:
            return list(rhsa_ids)
        if not all_known:
            return []
        rows = conn.execute("SELECT rhsa_id FROM rhsa ORDER BY rhsa_id").fetchall()
        return [row[0] for row in rows]

    async def _collect_one(self, conn: sqlite3.Connection, rhsa_id: str) -> bool:
        url = csaf_url_for(rhsa_id)
        try:
            response = await self.client.get(
                url, ttl_seconds=self.settings.cache_ttl_stable_seconds
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                self.log.warning("csaf.missing_document", rhsa_id=rhsa_id, url=url)
                return False
            raise

        statements = extract_vex(response.json())
        persist(conn, rhsa_id, statements)
        self.log.debug("csaf.persisted", rhsa_id=rhsa_id, statements=len(statements))
        return True


__all__ = [
    "CSAFCollector",
    "VEXStatement",
    "csaf_url_for",
    "extract_vex",
    "persist",
]
