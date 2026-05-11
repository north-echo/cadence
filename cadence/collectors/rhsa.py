"""RHSA collector (WP-03).

Pulls Red Hat Security Advisories from the Red Hat Security Data API and
persists them to ``rhsa``, ``rhsa_cve``, and ``rhsa_package_fix``. Filters to
advisories that affect RHEL 8, 9, or 10 (per the CPE in the CSAF product
tree); advisories that only affect layered products (RHACM, OCP tools, etc.)
are skipped at this stage and picked up by their container-image rebuilds
elsewhere.

The spec (CADENCE-SPEC.md §WP-03) refers to ``/hydra/rest/securitydata/cvrf.json``.
Red Hat retired CVRF in favor of CSAF v2; the live endpoint is now
``csaf.json`` (list) and ``csaf/{RHSA}.json`` (detail). The data model is
unchanged; only the URL and the document schema moved.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from cadence.collectors.base import (
    BaseCollector,
    CachedResponse,
    CollectionResult,
    HTTPClient,
)
from cadence.config import Settings
from cadence.db import connect

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


# Matches "<name>-<epoch>:<version>-<release>.<arch>" where:
#   * <name> may itself contain dashes (lazy)
#   * <epoch> is digits
#   * <version> contains no dashes
#   * <release> is the remaining stretch up to the final dot-separated <arch>
_NEVRA_RE = re.compile(
    r"^(?P<name>.+?)-(?P<epoch>\d+):(?P<version>[^-]+)-(?P<release>.+)\.(?P<arch>[^.]+)$"
)


# OS-level RHEL CPEs: enterprise_linux (mainline) and the extended-lifecycle
# variants (EUS, AUS, E4S, TUS). Layered products like rhacm, ocp_tools,
# red_hat_single_sign_on, etc. do not match.
_RHEL_CPE_RE = re.compile(
    r"cpe:/[ao]:redhat:(enterprise_linux|rhel_eus|rhel_aus|rhel_e4s|rhel_tus)"
    r":(?P<major>\d+)"
)


@dataclass(frozen=True)
class PackageFix:
    product: str
    name: str
    epoch: str
    version: str
    release: str
    arch: str

    @property
    def fixed_version(self) -> str:
        """Return ``epoch:version-release`` (canonical NEVRA-without-arch)."""
        return f"{self.epoch}:{self.version}-{self.release}"


@dataclass
class ParsedRHSA:
    rhsa_id: str
    title: str
    severity: str
    published_at: datetime
    updated_at: datetime | None
    source_url: str
    raw_json: str
    cves: list[ParsedCVE]
    package_fixes: list[PackageFix]


@dataclass(frozen=True)
class ParsedCVE:
    cve_id: str
    cvss3_score: float | None
    cvss3_vector: str | None


def _parse_product_id(product_id: str) -> PackageFix | None:
    """Split a CSAF ``product_id`` into a :class:`PackageFix`.

    The product_id format is ``{PRODUCT}:{name}-{E}:{V}-{R}.{arch}``. Module
    streams and other non-NEVRA forms return ``None`` and are skipped by the
    caller.
    """
    if ":" not in product_id:
        return None
    product, nvra = product_id.split(":", 1)
    match = _NEVRA_RE.match(nvra)
    if match is None:
        return None
    return PackageFix(
        product=product,
        name=match.group("name"),
        epoch=match.group("epoch"),
        version=match.group("version"),
        release=match.group("release"),
        arch=match.group("arch"),
    )


def _iter_cpes(product_tree: dict[str, Any]) -> Iterator[str]:
    """Yield every CPE under a CSAF product_tree."""
    stack: list[Any] = [product_tree]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        helper = node.get("product_identification_helper") or {}
        cpe = helper.get("cpe")
        if cpe:
            yield cpe
        if "branches" in node:
            stack.extend(node["branches"])
        product = node.get("product")
        if isinstance(product, dict):
            stack.append(product)


def affects_rhel(product_tree: dict[str, Any], majors: tuple[int, ...] = (8, 9, 10)) -> bool:
    """True iff any CPE in the product tree targets RHEL 8/9/10 (OS-level)."""
    wanted = {str(m) for m in majors}
    for cpe in _iter_cpes(product_tree):
        match = _RHEL_CPE_RE.search(cpe)
        if match and match.group("major") in wanted:
            return True
    return False


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _extract_cves(vulnerabilities: list[dict[str, Any]]) -> list[ParsedCVE]:
    out: list[ParsedCVE] = []
    for vuln in vulnerabilities:
        cve_id = vuln.get("cve")
        if not cve_id:
            continue
        score: float | None = None
        vector: str | None = None
        for entry in vuln.get("scores") or []:
            cvss = entry.get("cvss_v3")
            if not cvss:
                continue
            score = cvss.get("baseScore")
            vector = cvss.get("vectorString")
            if score is not None:
                break
        out.append(ParsedCVE(cve_id=cve_id, cvss3_score=score, cvss3_vector=vector))
    return out


def _extract_package_fixes(
    vulnerabilities: list[dict[str, Any]],
) -> list[PackageFix]:
    """Collect unique PackageFix entries from every vulnerability's ``fixed`` set."""
    seen: dict[tuple[str, str, str, str], PackageFix] = {}
    for vuln in vulnerabilities:
        product_status = vuln.get("product_status") or {}
        for product_id in product_status.get("fixed") or []:
            fix = _parse_product_id(product_id)
            if fix is None:
                log.debug("rhsa.skip_unparseable_product_id", product_id=product_id)
                continue
            key = (fix.product, fix.name, fix.fixed_version, fix.arch)
            seen.setdefault(key, fix)
    return list(seen.values())


def parse_detail(doc: dict[str, Any], *, source_url: str) -> ParsedRHSA | None:
    """Parse a CSAF v2 advisory document.

    Returns ``None`` if the advisory does not affect RHEL 8/9/10 OS-level
    products. The ``raw_json`` field on the result holds the verbatim upstream
    document so re-analysis with new methodology versions is possible without
    re-fetching.
    """
    document = doc.get("document") or {}
    tracking = document.get("tracking") or {}
    rhsa_id = tracking.get("id")
    if not rhsa_id:
        return None

    product_tree = doc.get("product_tree") or {}
    if not affects_rhel(product_tree):
        return None

    vulnerabilities = doc.get("vulnerabilities") or []
    severity = (document.get("aggregate_severity") or {}).get("text") or "unknown"
    published = _parse_iso(tracking.get("initial_release_date"))
    if published is None:
        log.warning("rhsa.no_published_at", rhsa_id=rhsa_id)
        return None

    return ParsedRHSA(
        rhsa_id=rhsa_id,
        title=document.get("title") or rhsa_id,
        severity=severity.lower(),
        published_at=published,
        updated_at=_parse_iso(tracking.get("current_release_date")),
        source_url=source_url,
        raw_json=json.dumps(doc, sort_keys=True),
        cves=_extract_cves(vulnerabilities),
        package_fixes=_extract_package_fixes(vulnerabilities),
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist(conn: sqlite3.Connection, parsed: ParsedRHSA, *, collected_at: datetime) -> None:
    """Upsert one RHSA and its child rows, idempotently, in a single transaction."""
    conn.execute("BEGIN")
    try:
        conn.execute(
            """
            INSERT INTO rhsa (
                rhsa_id, title, severity, published_at, updated_at,
                source_url, raw_json, collected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rhsa_id) DO UPDATE SET
                title = excluded.title,
                severity = excluded.severity,
                published_at = excluded.published_at,
                updated_at = excluded.updated_at,
                source_url = excluded.source_url,
                raw_json = excluded.raw_json,
                collected_at = excluded.collected_at
            """,
            (
                parsed.rhsa_id,
                parsed.title,
                parsed.severity,
                parsed.published_at.isoformat(),
                parsed.updated_at.isoformat() if parsed.updated_at else None,
                parsed.source_url,
                parsed.raw_json,
                collected_at.isoformat(),
            ),
        )
        conn.execute("DELETE FROM rhsa_cve WHERE rhsa_id = ?", (parsed.rhsa_id,))
        conn.executemany(
            "INSERT INTO rhsa_cve (rhsa_id, cve_id, cvss3_score, cvss3_vector) VALUES (?, ?, ?, ?)",
            [
                (parsed.rhsa_id, cve.cve_id, cve.cvss3_score, cve.cvss3_vector)
                for cve in parsed.cves
            ],
        )
        conn.execute(
            "DELETE FROM rhsa_package_fix WHERE rhsa_id = ?", (parsed.rhsa_id,)
        )
        conn.executemany(
            """
            INSERT INTO rhsa_package_fix
                (rhsa_id, package_name, fixed_version, arch, product)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    parsed.rhsa_id,
                    fix.name,
                    fix.fixed_version,
                    fix.arch,
                    fix.product,
                )
                for fix in parsed.package_fixes
            ],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


_LIST_URL = "https://access.redhat.com/hydra/rest/securitydata/csaf.json"


class RHSACollector(BaseCollector):
    """Collects RHSAs affecting RHEL 8/9/10 from access.redhat.com."""

    name = "rhsa"
    page_size: int = 100

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
        since: str | None = None,
        until: str | None = None,
        max_pages: int = 500,
        **_: Any,
    ) -> CollectionResult:
        started = datetime.now(UTC)
        result = CollectionResult(
            name=self.name, started_at=started, completed_at=started
        )
        with connect(self.db_path) as conn:
            page = 1
            while page <= max_pages:
                summaries = await self._fetch_list(since=since, until=until, page=page)
                if not summaries:
                    break
                for summary in summaries:
                    try:
                        if await self._collect_one(conn, summary):
                            result.records += 1
                    except Exception as exc:
                        rhsa_id = summary.get("RHSA", "<unknown>")
                        msg = f"{rhsa_id}: {exc}"
                        result.errors.append(msg)
                        self.log.exception("rhsa.collect_one_failed", rhsa_id=rhsa_id)
                if len(summaries) < self.page_size:
                    break
                page += 1
        result.completed_at = datetime.now(UTC)
        self.log.info(
            "rhsa.collect_done",
            records=result.records,
            errors=len(result.errors),
            duration_seconds=round(result.duration_seconds, 2),
        )
        return result

    async def _fetch_list(
        self, *, since: str | None, until: str | None, page: int
    ) -> list[dict[str, Any]]:
        params: list[str] = [f"per_page={self.page_size}", f"page={page}"]
        if since:
            params.append(f"after={since}")
        if until:
            params.append(f"before={until}")
        url = f"{_LIST_URL}?{'&'.join(params)}"
        response = await self.client.get(
            url, ttl_seconds=self.settings.cache_ttl_current_seconds
        )
        return self._json_list(response)

    @staticmethod
    def _json_list(response: CachedResponse) -> list[dict[str, Any]]:
        payload = response.json()
        if not isinstance(payload, list):
            return []
        return [r for r in payload if isinstance(r, dict)]

    async def _collect_one(
        self, conn: sqlite3.Connection, summary: dict[str, Any]
    ) -> bool:
        rhsa_id = summary.get("RHSA")
        resource_url = summary.get("resource_url")
        if not rhsa_id or not resource_url:
            return False
        if not rhsa_id.startswith("RHSA-"):
            return False  # skip RHBAs and RHEAs

        detail = await self.client.get(
            resource_url, ttl_seconds=self.settings.cache_ttl_stable_seconds
        )
        parsed = parse_detail(detail.json(), source_url=resource_url)
        if parsed is None:
            self.log.debug("rhsa.skipped_non_rhel", rhsa_id=rhsa_id)
            return False
        persist(conn, parsed, collected_at=datetime.now(UTC))
        self.log.debug(
            "rhsa.persisted",
            rhsa_id=parsed.rhsa_id,
            cves=len(parsed.cves),
            fixes=len(parsed.package_fixes),
        )
        return True


__all__ = [
    "PackageFix",
    "ParsedCVE",
    "ParsedRHSA",
    "RHSACollector",
    "affects_rhel",
    "parse_detail",
    "persist",
]
