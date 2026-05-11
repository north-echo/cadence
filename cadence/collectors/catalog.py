"""Red Hat Container Catalog collector (WP-06).

Pulls per-image metadata + RPM manifests from
``catalog.redhat.com/api/containers/v1/`` for the catalog-source repos in
``cadence/targets.py``. Persists to ``container_image``,
``container_image_rpm``, and (when present in older records)
``catalog_advisory_mapping``.

A few real-world quirks worth knowing
-------------------------------------

* The catalog API uses Docker/OCI arch names (``amd64``, ``arm64``); RPMs are
  named with kernel arch (``x86_64``, ``aarch64``). We translate at the URL
  boundary and store kernel-arch internally so ``container_image.architecture``
  joins cleanly to ``container_image_rpm.arch`` and to ``repo_package.arch``.
* The ``repositories[].comparison.advisory_rpm_mapping`` field stopped being
  populated around November 2024 (spike §13.4). We capture it when present and
  use it as a cross-validation signal in WP-09; we don't compute against it.
* A single catalog image record can be tagged multiple times. We prefer the
  build-specific tag matching ``X[.Y[.Z]]-NNN`` because that's what aligns with
  rebuild cadence; a "floating" tag like ``9.4`` or ``latest`` is recorded only
  if no build-specific tag exists.
* The catalog's ``_id`` (a MongoDB ObjectId) is what we use for
  ``container_image.image_id``. The image content digest goes into ``digest``.
  This avoids primary-key collisions when the same content is published under
  multiple records.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from cadence.collectors.base import (
    BaseCollector,
    CollectionResult,
    HTTPClient,
)
from cadence.config import Settings
from cadence.db import connect
from cadence.targets import ALL_REPOS, TrackedRepo, by_source, find

log = structlog.get_logger(__name__)


BASE_URL = "https://catalog.redhat.com/api/containers/v1"
DEFAULT_ARCHES: tuple[str, ...] = ("x86_64", "aarch64")
DEFAULT_PAGE_SIZE = 100

_ARCH_KERNEL_TO_CATALOG = {"x86_64": "amd64", "aarch64": "arm64"}
_ARCH_CATALOG_TO_KERNEL = {v: k for k, v in _ARCH_KERNEL_TO_CATALOG.items()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_TAG_RE = re.compile(
    r"^(?P<version>\d+(?:\.\d+)*)-(?P<build>\d+)$"
)
_EPOCH_RE = re.compile(r"-(\d+):")


def parse_tag(tag: str) -> tuple[str, int] | None:
    """Split ``X[.Y[.Z]]-NNN`` tags into ``(version, build_num)``.

    Returns ``None`` for floating tags (``latest``, ``9.4``, ``9``) and any
    other shape; callers log-but-don't-fail per spec WP-06 acceptance.
    """
    m = _TAG_RE.match(tag)
    if m is None:
        return None
    return m.group("version"), int(m.group("build"))


def normalize_arch(catalog_arch: str) -> str:
    """Map Docker/OCI arch names to kernel arch (amd64 → x86_64, etc.)."""
    return _ARCH_CATALOG_TO_KERNEL.get(catalog_arch, catalog_arch)


def to_catalog_arch(kernel_arch: str) -> str:
    """Inverse of :func:`normalize_arch`."""
    return _ARCH_KERNEL_TO_CATALOG.get(kernel_arch, kernel_arch)


def epoch_from_srpm_nevra(srpm_nevra: str | None) -> str:
    if not srpm_nevra:
        return "0"
    m = _EPOCH_RE.search(srpm_nevra)
    return m.group(1) if m else "0"


def list_url(repository: str, *, page: int, page_size: int, arch: str,
             since: str | None = None) -> str:
    """Build the catalog images-list URL for one (repo, arch) page.

    ``arch`` is the kernel arch; we translate to the catalog vocabulary here.
    """
    catalog_arch = to_catalog_arch(arch)
    filter_parts = [f"architecture=={catalog_arch}"]
    if since:
        filter_parts.append(f"creation_date>={since}")
    filter_str = " and ".join(filter_parts)
    return (
        f"{BASE_URL}/repositories/registry/registry.access.redhat.com/repository/"
        f"{repository}/images?page_size={page_size}&page={page}"
        f"&sort_by=creation_date%5Basc%5D&filter={filter_str}"
    )


def rpm_manifest_url(rpm_manifest_href: str) -> str:
    """Resolve the ``_links.rpm_manifest.href`` (server-relative) to absolute."""
    if rpm_manifest_href.startswith("http://") or rpm_manifest_href.startswith("https://"):
        return rpm_manifest_href
    # The catalog uses paths like /v1/images/id/{_id}/rpm-manifest;
    # BASE_URL ends in /v1, so strip one of them.
    href = rpm_manifest_href.lstrip("/")
    if href.startswith("v1/"):
        href = href[len("v1/"):]
    return f"{BASE_URL}/{href}"


# ---------------------------------------------------------------------------
# Parsed-row dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ParsedImage:
    image_id: str
    source: str  # "catalog"
    registry: str
    repository: str
    tier: str
    tag: str
    digest: str
    architecture: str
    build_date: str  # ISO 8601
    parsed_version: str | None
    parsed_build_num: int | None
    raw_json: str
    rpm_manifest_href: str | None
    advisory_rpm_mapping: list[dict[str, Any]]


@dataclass(frozen=True)
class ParsedRPM:
    package_name: str
    version: str  # epoch:version-release
    arch: str


def _pick_tag(repo_record: dict[str, Any]) -> tuple[str, str | None, int | None]:
    """Choose the most-informative tag for an image-in-a-repository.

    Returns ``(tag_string, parsed_version, parsed_build_num)``. The tag string
    is always non-empty: when no tag matches the build-num pattern, we fall
    back to the first listed tag (or ``"<untagged>"`` if there are none).
    """
    tags = repo_record.get("tags") or []
    best: str | None = None
    best_parsed: tuple[str, int] | None = None
    fallback: str | None = None
    for entry in tags:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not name:
            continue
        if fallback is None:
            fallback = name
        parsed = parse_tag(name)
        if parsed is not None and (best_parsed is None or parsed[1] > best_parsed[1]):
            best = name
            best_parsed = parsed
    if best is not None and best_parsed is not None:
        return best, best_parsed[0], best_parsed[1]
    if fallback is not None:
        return fallback, None, None
    return "<untagged>", None, None


def parse_image(
    raw: dict[str, Any], *, repository: str, tier: str
) -> ParsedImage | None:
    """Project one catalog image-record into a :class:`ParsedImage`.

    Returns ``None`` if the record has no matching repositories entry
    (shouldn't happen given how we query, but defensive).
    """
    image_id = str(raw.get("_id") or "")
    if not image_id:
        return None

    target_repo: dict[str, Any] | None = None
    for repo_record in raw.get("repositories") or []:
        if (
            repo_record.get("repository") == repository
            and repo_record.get("registry") == "registry.access.redhat.com"
        ):
            target_repo = repo_record
            break
    if target_repo is None:
        return None

    tag, parsed_version, parsed_build_num = _pick_tag(target_repo)
    if parsed_version is None:
        log.debug(
            "catalog.unparseable_tag",
            repository=repository,
            image_id=image_id,
            tag=tag,
        )

    digest = (
        target_repo.get("manifest_schema2_digest")
        or target_repo.get("manifest_list_digest")
        or raw.get("image_id")  # the catalog's own sha256 field
        or ""
    )

    architecture = normalize_arch(str(raw.get("architecture", "")))

    build_date_str = (
        (raw.get("brew") or {}).get("completion_date")
        or target_repo.get("push_date")
        or raw.get("creation_date")
        or ""
    )
    if not build_date_str:
        log.warning("catalog.no_build_date", repository=repository, image_id=image_id)
        return None

    rpm_link = (raw.get("_links") or {}).get("rpm_manifest") or {}
    rpm_href = rpm_link.get("href") if isinstance(rpm_link, dict) else None

    comparison = target_repo.get("comparison") or {}
    advisory_rpm_mapping = comparison.get("advisory_rpm_mapping") or []

    return ParsedImage(
        image_id=image_id,
        source="catalog",
        registry="registry.access.redhat.com",
        repository=repository,
        tier=tier,
        tag=tag,
        digest=str(digest),
        architecture=architecture,
        build_date=str(build_date_str),
        parsed_version=parsed_version,
        parsed_build_num=parsed_build_num,
        raw_json=json.dumps(raw, sort_keys=True),
        rpm_manifest_href=rpm_href,
        advisory_rpm_mapping=advisory_rpm_mapping,
    )


def parse_rpm_manifest(doc: dict[str, Any]) -> list[ParsedRPM]:
    """Project an rpm-manifest payload into a list of :class:`ParsedRPM`."""
    out: dict[tuple[str, str], ParsedRPM] = {}
    for entry in doc.get("rpms") or []:
        name = entry.get("name")
        version = entry.get("version")
        release = entry.get("release")
        arch = entry.get("architecture")
        if not (name and version and release and arch):
            continue
        epoch = epoch_from_srpm_nevra(entry.get("srpm_nevra"))
        rpm = ParsedRPM(
            package_name=str(name),
            version=f"{epoch}:{version}-{release}",
            arch=str(arch),
        )
        out.setdefault((rpm.package_name, rpm.arch), rpm)
    return list(out.values())


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def seed_tracked_repositories(
    conn: sqlite3.Connection, repos: tuple[TrackedRepo, ...] = ALL_REPOS
) -> int:
    """Ensure every spec-§6 repo exists in ``tracked_repository``.

    Idempotent: existing rows keep their original ``added_at``. New rows are
    added with ``added_at = now``. Returns the count of new rows.
    """
    now = datetime.now(UTC).isoformat()
    added = 0
    for repo in repos:
        cur = conn.execute(
            """
            INSERT INTO tracked_repository
                (repository, source, registry, tier, rationale, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(repository) DO NOTHING
            """,
            (repo.repository, repo.source, repo.registry, repo.tier, repo.rationale, now),
        )
        added += cur.rowcount or 0
    return added


def persist_image(
    conn: sqlite3.Connection,
    *,
    parsed: ParsedImage,
    rpms: list[ParsedRPM],
    collected_at: datetime,
) -> None:
    """Upsert one image and replace its RPM + advisory-mapping rows atomically."""
    conn.execute("BEGIN")
    try:
        conn.execute(
            """
            INSERT INTO container_image (
                image_id, source, registry, repository, tier, tag, digest,
                architecture, build_date, parsed_version, parsed_build_num,
                raw_json, collected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(image_id) DO UPDATE SET
                source = excluded.source,
                registry = excluded.registry,
                repository = excluded.repository,
                tier = excluded.tier,
                tag = excluded.tag,
                digest = excluded.digest,
                architecture = excluded.architecture,
                build_date = excluded.build_date,
                parsed_version = excluded.parsed_version,
                parsed_build_num = excluded.parsed_build_num,
                raw_json = excluded.raw_json,
                collected_at = excluded.collected_at
            """,
            (
                parsed.image_id,
                parsed.source,
                parsed.registry,
                parsed.repository,
                parsed.tier,
                parsed.tag,
                parsed.digest,
                parsed.architecture,
                parsed.build_date,
                parsed.parsed_version,
                parsed.parsed_build_num,
                parsed.raw_json,
                collected_at.isoformat(),
            ),
        )

        conn.execute(
            "DELETE FROM container_image_rpm WHERE image_id = ?",
            (parsed.image_id,),
        )
        conn.executemany(
            """
            INSERT INTO container_image_rpm
                (image_id, package_name, version, arch)
            VALUES (?, ?, ?, ?)
            """,
            [(parsed.image_id, r.package_name, r.version, r.arch) for r in rpms],
        )

        conn.execute(
            "DELETE FROM catalog_advisory_mapping WHERE image_id = ?",
            (parsed.image_id,),
        )
        rows: list[tuple[str, str, str]] = []
        for entry in parsed.advisory_rpm_mapping:
            nvra = entry.get("nvra")
            advisory_ids = entry.get("advisory_ids") or []
            if not nvra:
                continue
            for aid in advisory_ids:
                rows.append((parsed.image_id, str(aid), str(nvra)))
        conn.executemany(
            """
            INSERT INTO catalog_advisory_mapping (image_id, advisory_id, nvra)
            VALUES (?, ?, ?)
            ON CONFLICT(image_id, advisory_id, nvra) DO NOTHING
            """,
            rows,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class CatalogCollector(BaseCollector):
    """Iterates configured catalog repos and persists image + RPM data."""

    name = "catalog"
    page_size: int = DEFAULT_PAGE_SIZE

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
        arches: tuple[str, ...] = DEFAULT_ARCHES,
        since: str | None = None,
        **_: Any,
    ) -> CollectionResult:
        started = datetime.now(UTC)
        result = CollectionResult(name=self.name, started_at=started, completed_at=started)

        with connect(self.db_path) as conn:
            seed_tracked_repositories(conn)
            targets = self._resolve_targets(repos)

        for repo in targets:
            for arch in arches:
                try:
                    n = await self._collect_repo_arch(
                        repository=repo.repository, tier=repo.tier, arch=arch, since=since
                    )
                    result.records += n
                except Exception as exc:  # per-(repo,arch) isolation
                    msg = f"{repo.repository} [{arch}]: {exc}"
                    result.errors.append(msg)
                    self.log.exception(
                        "catalog.collect_repo_arch_failed",
                        repository=repo.repository,
                        arch=arch,
                    )

        result.completed_at = datetime.now(UTC)
        self.log.info(
            "catalog.collect_done",
            images=result.records,
            errors=len(result.errors),
            duration_seconds=round(result.duration_seconds, 2),
        )
        return result

    @staticmethod
    def _resolve_targets(repos: list[str] | None) -> list[TrackedRepo]:
        if repos:
            out: list[TrackedRepo] = []
            for name in repos:
                tracked = find(name)
                if tracked is None:
                    raise ValueError(
                        f"unknown repository {name!r}; not in cadence/targets.py"
                    )
                if tracked.source != "catalog":
                    raise ValueError(
                        f"{name!r} is source={tracked.source!r}; "
                        "use `cadence collect quay` for Quay-sourced repos"
                    )
                out.append(tracked)
            return out
        return list(by_source("catalog"))

    async def _collect_repo_arch(
        self, *, repository: str, tier: str, arch: str, since: str | None
    ) -> int:
        count = 0
        async for raw_image in self._iter_images(
            repository=repository, arch=arch, since=since
        ):
            try:
                parsed = parse_image(raw_image, repository=repository, tier=tier)
                if parsed is None:
                    continue
                rpms = await self._fetch_rpm_manifest(parsed)
                with connect(self.db_path) as conn:
                    persist_image(
                        conn, parsed=parsed, rpms=rpms, collected_at=datetime.now(UTC)
                    )
                count += 1
            except Exception:
                self.log.exception(
                    "catalog.image_failed",
                    repository=repository,
                    image_id=raw_image.get("_id"),
                )
                raise
        return count

    async def _iter_images(
        self, *, repository: str, arch: str, since: str | None
    ) -> AsyncIterator[dict[str, Any]]:
        page = 0
        while True:
            url = list_url(
                repository, page=page, page_size=self.page_size, arch=arch, since=since
            )
            response = await self.client.get(
                url, ttl_seconds=self.settings.cache_ttl_current_seconds
            )
            payload = response.json()
            data = payload.get("data") or []
            if not data:
                return
            for entry in data:
                if isinstance(entry, dict):
                    yield entry
            total = int(payload.get("total") or 0)
            page_size = int(payload.get("page_size") or self.page_size)
            if (page + 1) * page_size >= total:
                return
            page += 1

    async def _fetch_rpm_manifest(self, parsed: ParsedImage) -> list[ParsedRPM]:
        if not parsed.rpm_manifest_href:
            return []
        url = rpm_manifest_url(parsed.rpm_manifest_href)
        response = await self.client.get(
            url, ttl_seconds=self.settings.cache_ttl_stable_seconds
        )
        return parse_rpm_manifest(response.json())


__all__ = [
    "BASE_URL",
    "DEFAULT_ARCHES",
    "CatalogCollector",
    "ParsedImage",
    "ParsedRPM",
    "epoch_from_srpm_nevra",
    "list_url",
    "normalize_arch",
    "parse_image",
    "parse_rpm_manifest",
    "parse_tag",
    "persist_image",
    "rpm_manifest_url",
    "seed_tracked_repositories",
    "to_catalog_arch",
]
