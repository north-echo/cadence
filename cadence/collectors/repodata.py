"""UBI repodata collector (WP-05, forward-only).

Polls UBI repository metadata at ``cdn-ubi.redhat.com`` and persists package
observations to ``repo_observation`` and ``repo_package``.

Forward-only by design: ``cdn-ubi.redhat.com`` exposes only current repodata,
not an archive (validated during the pre-flight spike, recorded in
CADENCE-SPEC.md §13.5). Gap A precision is therefore bounded by the polling
interval; WP-14 timers run this collector every four hours by default.

UBI ``updateinfo.xml`` is empty and CADENCE does not consume it (CADENCE-SPEC
§13.6).

URL layout
----------

    https://cdn-ubi.redhat.com/content/public/ubi/dist/
        ubi{major}/{ver}/{arch}/{repo}/os/repodata/{repomd.xml | …-primary.xml.gz}

For UBI the rolling-pointer convention is ``{major} == {ver}`` — e.g.
``ubi9/9/x86_64/baseos``. We use the slash-delimited tail as the canonical
``repo_id``.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import sqlite3
import xml.etree.ElementTree as ET
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

CDN_BASE = "https://cdn-ubi.redhat.com/content/public/ubi/dist"

_REPO_NS = "http://linux.duke.edu/metadata/repo"
_COMMON_NS = "http://linux.duke.edu/metadata/common"


# ---------------------------------------------------------------------------
# Default repo set: UBI {8,9,10} x{baseos,appstream,codeready-builder} x{x86_64,aarch64}
# ---------------------------------------------------------------------------


def _build_default_repos() -> tuple[str, ...]:
    out = []
    for major in (8, 9, 10):
        for arch in ("x86_64", "aarch64"):
            for repo in ("baseos", "appstream", "codeready-builder"):
                out.append(f"ubi{major}/{major}/{arch}/{repo}")
    return tuple(out)


DEFAULT_REPOS: tuple[str, ...] = _build_default_repos()


def repomd_url(repo_id: str) -> str:
    return f"{CDN_BASE}/{repo_id}/os/repodata/repomd.xml"


def primary_url(repo_id: str, location_href: str) -> str:
    """Resolve a relative ``location`` from repomd.xml to an absolute URL.

    repomd.xml ``location`` hrefs are relative to the repo root, e.g.
    ``repodata/abcdef-primary.xml.gz``.
    """
    return f"{CDN_BASE}/{repo_id}/os/{location_href}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoMD:
    revision: str
    primary_href: str
    primary_sha256: str


@dataclass(frozen=True)
class RepoPackage:
    name: str
    version: str  # epoch:ver-rel
    arch: str
    build_time: datetime | None
    file_time: datetime | None


def parse_repomd(xml_bytes: bytes) -> RepoMD:
    """Parse repomd.xml for the revision and the primary.xml.gz location + checksum.

    Raises ``ValueError`` if the document is missing required fields.
    """
    root = ET.fromstring(xml_bytes)
    revision_el = root.find(f"{{{_REPO_NS}}}revision")
    if revision_el is None or not revision_el.text:
        raise ValueError("repomd.xml missing <revision>")

    primary_node = None
    for data in root.findall(f"{{{_REPO_NS}}}data"):
        if data.get("type") == "primary":
            primary_node = data
            break
    if primary_node is None:
        raise ValueError("repomd.xml missing <data type='primary'>")

    location_el = primary_node.find(f"{{{_REPO_NS}}}location")
    if location_el is None or not location_el.get("href"):
        raise ValueError("primary data missing <location href>")

    sha256: str | None = None
    for checksum in primary_node.findall(f"{{{_REPO_NS}}}checksum"):
        if checksum.get("type") == "sha256" and checksum.text:
            sha256 = checksum.text
            break
    if sha256 is None:
        raise ValueError("primary data missing sha256 checksum")

    return RepoMD(
        revision=revision_el.text,
        primary_href=location_el.get("href") or "",
        primary_sha256=sha256,
    )


def _epoch_to_iso(epoch_str: str | None) -> datetime | None:
    if not epoch_str:
        return None
    try:
        return datetime.fromtimestamp(int(epoch_str), tz=UTC)
    except ValueError:
        return None


def _parse_package(elem: ET.Element) -> RepoPackage | None:
    """Parse one ``<package>`` element from primary.xml."""
    name_el = elem.find(f"{{{_COMMON_NS}}}name")
    arch_el = elem.find(f"{{{_COMMON_NS}}}arch")
    version_el = elem.find(f"{{{_COMMON_NS}}}version")
    time_el = elem.find(f"{{{_COMMON_NS}}}time")
    if name_el is None or arch_el is None or version_el is None:
        return None
    epoch = version_el.get("epoch") or "0"
    ver = version_el.get("ver")
    rel = version_el.get("rel")
    if not ver or not rel:
        return None
    return RepoPackage(
        name=name_el.text or "",
        version=f"{epoch}:{ver}-{rel}",
        arch=arch_el.text or "",
        build_time=_epoch_to_iso(time_el.get("build") if time_el is not None else None),
        file_time=_epoch_to_iso(time_el.get("file") if time_el is not None else None),
    )


def iter_primary_packages(primary_gz_bytes: bytes) -> Iterator[RepoPackage]:
    """Stream-parse ``<package>`` entries from a gzipped primary.xml.

    Uses ``iterparse`` + ``elem.clear()`` so large repos (appstream can run
    multiple MB uncompressed) don't blow up memory.
    """
    with gzip.open(io.BytesIO(primary_gz_bytes)) as fh:
        package_tag = f"{{{_COMMON_NS}}}package"
        for _event, elem in ET.iterparse(fh, events=("end",)):
            if elem.tag != package_tag:
                continue
            pkg = _parse_package(elem)
            elem.clear()
            if pkg is not None:
                yield pkg


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def observation_exists(
    conn: sqlite3.Connection, repo_id: str, revision: str
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM repo_observation WHERE repo_id = ? AND repomd_revision = ? LIMIT 1",
        (repo_id, revision),
    ).fetchone()
    return row is not None


def persist(
    conn: sqlite3.Connection,
    *,
    repo_id: str,
    repomd: RepoMD,
    packages: list[RepoPackage],
    observed_at: datetime,
) -> int:
    """Insert one observation + every package row; returns the observation id."""
    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            """
            INSERT INTO repo_observation
                (repo_id, observed_at, repomd_revision, primary_xml_sha256)
            VALUES (?, ?, ?, ?)
            """,
            (repo_id, observed_at.isoformat(), repomd.revision, repomd.primary_sha256),
        )
        obs_id = int(cur.lastrowid or 0)
        conn.executemany(
            """
            INSERT INTO repo_package
                (observation_id, package_name, version, arch, build_time, file_time)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    obs_id,
                    p.name,
                    p.version,
                    p.arch,
                    p.build_time.isoformat() if p.build_time else None,
                    p.file_time.isoformat() if p.file_time else None,
                )
                for p in packages
            ],
        )
        conn.execute("COMMIT")
        return obs_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class RepoDataCollector(BaseCollector):
    """Polls UBI repodata and persists package observations."""

    name = "repodata"

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
        repos: list[str] | None = None,
        **_: Any,
    ) -> CollectionResult:
        started = datetime.now(UTC)
        result = CollectionResult(
            name=self.name, started_at=started, completed_at=started
        )
        targets = list(repos) if repos else list(DEFAULT_REPOS)
        with connect(self.db_path) as conn:
            for repo_id in targets:
                try:
                    if await self._collect_one(conn, repo_id):
                        result.records += 1
                except Exception as exc:  # per-repo isolation
                    msg = f"{repo_id}: {exc}"
                    result.errors.append(msg)
                    self.log.exception("repodata.collect_one_failed", repo_id=repo_id)
        result.completed_at = datetime.now(UTC)
        self.log.info(
            "repodata.collect_done",
            repos_polled=len(targets),
            new_observations=result.records,
            errors=len(result.errors),
            duration_seconds=round(result.duration_seconds, 2),
        )
        return result

    async def _collect_one(self, conn: sqlite3.Connection, repo_id: str) -> bool:
        repomd_resp = await self.client.get(
            repomd_url(repo_id),
            ttl_seconds=self.settings.cache_ttl_current_seconds,
            bypass_cache=True,  # repomd.xml is the freshness signal — never cache it
        )
        repomd = parse_repomd(repomd_resp.content)

        if observation_exists(conn, repo_id, repomd.revision):
            self.log.debug(
                "repodata.unchanged", repo_id=repo_id, revision=repomd.revision
            )
            return False

        primary_resp = await self.client.get(
            primary_url(repo_id, repomd.primary_href),
            ttl_seconds=self.settings.cache_ttl_stable_seconds,
        )
        self._verify_primary(primary_resp, repomd.primary_sha256)
        packages = list(iter_primary_packages(primary_resp.content))

        persist(
            conn,
            repo_id=repo_id,
            repomd=repomd,
            packages=packages,
            observed_at=datetime.now(UTC),
        )
        self.log.info(
            "repodata.persisted",
            repo_id=repo_id,
            revision=repomd.revision,
            packages=len(packages),
        )
        return True

    @staticmethod
    def _verify_primary(response: CachedResponse, expected_sha256: str) -> None:
        actual = hashlib.sha256(response.content).hexdigest()
        if actual != expected_sha256:
            raise ValueError(
                f"primary.xml.gz checksum mismatch: "
                f"expected {expected_sha256}, got {actual}"
            )


__all__ = [
    "CDN_BASE",
    "DEFAULT_REPOS",
    "RepoDataCollector",
    "RepoMD",
    "RepoPackage",
    "iter_primary_packages",
    "observation_exists",
    "parse_repomd",
    "persist",
    "primary_url",
    "repomd_url",
]
