"""Quay collector (WP-07).

Pulls tag history and per-arch manifest digests for the Quay-source repos in
``cadence/targets.py``. Persists rows to ``container_image`` with
``source='quay'``.

Scope: inter-build interval only. No RPM-level Gap C in v1 — Quay images don't
expose an analogue of the Red Hat Container Catalog's ``rpm-manifest``
endpoint, and extracting RPMs from registry-mounted images is out of scope
(see ``docs/methodology.md`` §10 and CADENCE-SPEC.md §WP-07).

Data flow
---------

Per tracked Quay repo:

1. Paginate ``/api/v1/repository/{ns}/{name}/tag/?onlyActiveTags=true``.
2. For each tag, classify by ``is_manifest_list``:

   * **manifest list:** fetch the OCI v2 manifest list at the tag's digest
     and emit one ``container_image`` row per child (digest + platform.arch).
   * **single manifest:** fetch the manifest + its config blob; the blob
     carries ``architecture`` and ``created``. One ``container_image`` row.

3. ``build_date`` is always ``start_ts`` from the tag listing — the moment
   Quay accepted the push, accurate to the second, identical across the
   children of a manifest list. ``created`` (in the config blob) is the
   image-build timestamp and may differ slightly; we prefer ``start_ts``
   for inter-build interval since that's what users see.
"""

from __future__ import annotations

import json
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
from cadence.collectors.catalog import seed_tracked_repositories
from cadence.config import Settings
from cadence.db import connect
from cadence.targets import TrackedRepo, by_source, find

log = structlog.get_logger(__name__)


API_BASE = "https://quay.io/api/v1"
REGISTRY = "quay.io"
REGISTRY_V2 = "https://quay.io/v2"
DEFAULT_TAG_PAGE_SIZE = 100

_MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
_BLOB_ACCEPT = "application/vnd.oci.image.config.v1+json, application/json, */*"

_MANIFEST_LIST_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def tags_url(repository: str, *, page: int, page_size: int) -> str:
    return (
        f"{API_BASE}/repository/{repository}/tag/?limit={page_size}"
        f"&page={page}&onlyActiveTags=true"
    )


def manifest_url(repository: str, reference: str) -> str:
    return f"{REGISTRY_V2}/{repository}/manifests/{reference}"


def blob_url(repository: str, digest: str) -> str:
    return f"{REGISTRY_V2}/{repository}/blobs/{digest}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuayImageRow:
    image_id: str        # the digest of *this* row (per-arch)
    tag: str
    digest: str
    architecture: str    # docker/OCI vocabulary: amd64, arm64, ppc64le, …
    build_date: datetime
    raw_json: str


def _epoch_to_iso(epoch_str: int | float | None) -> datetime | None:
    if epoch_str is None:
        return None
    try:
        return datetime.fromtimestamp(int(epoch_str), tz=UTC)
    except (ValueError, TypeError, OSError):
        return None


def is_manifest_list(manifest: dict[str, Any]) -> bool:
    return (manifest.get("mediaType") or "") in _MANIFEST_LIST_TYPES


def manifest_list_children(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``[(child_digest, architecture), …]`` from an OCI image index.

    Entries without a digest or platform.architecture are dropped (rare; e.g.,
    attestation manifests with ``platform.architecture == 'unknown'``).
    """
    out: list[tuple[str, str]] = []
    for entry in manifest.get("manifests") or []:
        if not isinstance(entry, dict):
            continue
        digest = entry.get("digest")
        platform = entry.get("platform") or {}
        arch = platform.get("architecture")
        if not digest or not arch or arch == "unknown":
            continue
        out.append((str(digest), str(arch)))
    return out


def config_digest_of(manifest: dict[str, Any]) -> str | None:
    config = manifest.get("config") or {}
    digest = config.get("digest")
    return str(digest) if digest else None


def arch_from_config_blob(config_blob: dict[str, Any]) -> str | None:
    arch = config_blob.get("architecture")
    return str(arch) if arch else None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist_image(
    conn: sqlite3.Connection,
    *,
    row: QuayImageRow,
    repository: str,
    tier: str,
    collected_at: datetime,
) -> None:
    """UPSERT one container_image row (Quay source)."""
    conn.execute(
        """
        INSERT INTO container_image (
            image_id, source, registry, repository, tier, tag, digest,
            architecture, build_date, parsed_version, parsed_build_num,
            raw_json, collected_at
        ) VALUES (?, 'quay', 'quay.io', ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
        ON CONFLICT(image_id) DO UPDATE SET
            tag = excluded.tag,
            tier = excluded.tier,
            architecture = excluded.architecture,
            build_date = excluded.build_date,
            raw_json = excluded.raw_json,
            collected_at = excluded.collected_at
        """,
        (
            row.image_id,
            repository,
            tier,
            row.tag,
            row.digest,
            row.architecture,
            row.build_date.isoformat(),
            row.raw_json,
            collected_at.isoformat(),
        ),
    )


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class QuayCollector(BaseCollector):
    """Iterates configured Quay repos and persists per-arch container images."""

    name = "quay"
    tag_page_size: int = DEFAULT_TAG_PAGE_SIZE

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
        result = CollectionResult(name=self.name, started_at=started, completed_at=started)

        with connect(self.db_path) as conn:
            seed_tracked_repositories(conn)
            targets = self._resolve_targets(repos)

        for tracked in targets:
            try:
                n = await self._collect_repo(tracked)
                result.records += n
            except Exception as exc:  # per-repo isolation
                msg = f"{tracked.repository}: {exc}"
                result.errors.append(msg)
                self.log.exception(
                    "quay.collect_repo_failed", repository=tracked.repository
                )

        result.completed_at = datetime.now(UTC)
        self.log.info(
            "quay.collect_done",
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
                if tracked.source != "quay":
                    raise ValueError(
                        f"{name!r} is source={tracked.source!r}; "
                        "use `cadence collect catalog` for catalog-sourced repos"
                    )
                out.append(tracked)
            return out
        return list(by_source("quay"))

    async def _collect_repo(self, tracked: TrackedRepo) -> int:
        count = 0
        # Track digests we've already resolved within this run to avoid
        # refetching the same manifest for different tag aliases.
        seen_top_digests: set[str] = set()
        async for tag in self._iter_tags(tracked.repository):
            top_digest = tag.get("manifest_digest")
            if not top_digest:
                continue
            try:
                rows = await self._rows_for_tag(tracked.repository, tag, seen_top_digests)
            except Exception:
                self.log.exception(
                    "quay.tag_failed",
                    repository=tracked.repository,
                    tag=tag.get("name"),
                )
                continue
            if not rows:
                continue
            with connect(self.db_path) as conn:
                for row in rows:
                    persist_image(
                        conn,
                        row=row,
                        repository=tracked.repository,
                        tier=tracked.tier,
                        collected_at=datetime.now(UTC),
                    )
                count += len(rows)
        return count

    async def _iter_tags(self, repository: str) -> AsyncIterator[dict[str, Any]]:
        page = 1
        while True:
            url = tags_url(repository, page=page, page_size=self.tag_page_size)
            response = await self.client.get(
                url, ttl_seconds=self.settings.cache_ttl_current_seconds
            )
            payload = response.json()
            for tag in payload.get("tags") or []:
                if isinstance(tag, dict):
                    yield tag
            if not payload.get("has_additional"):
                return
            page += 1

    async def _rows_for_tag(
        self,
        repository: str,
        tag: dict[str, Any],
        seen_top_digests: set[str],
    ) -> list[QuayImageRow]:
        tag_name = str(tag.get("name") or "")
        top_digest = str(tag.get("manifest_digest") or "")
        if not top_digest:
            return []
        build_date = _epoch_to_iso(tag.get("start_ts"))
        if build_date is None:
            self.log.warning(
                "quay.no_start_ts", repository=repository, tag=tag_name
            )
            return []

        manifest = await self._fetch_manifest(repository, top_digest)
        raw_tag = json.dumps(tag, sort_keys=True)

        if is_manifest_list(manifest):
            children = manifest_list_children(manifest)
            if not children:
                self.log.warning(
                    "quay.empty_manifest_list",
                    repository=repository,
                    tag=tag_name,
                    digest=top_digest,
                )
                return []
            seen_top_digests.add(top_digest)
            return [
                QuayImageRow(
                    image_id=child_digest,
                    tag=tag_name,
                    digest=child_digest,
                    architecture=arch,
                    build_date=build_date,
                    raw_json=raw_tag,
                )
                for child_digest, arch in children
            ]

        # Single manifest: fetch config blob to get architecture.
        config_digest = config_digest_of(manifest)
        if not config_digest:
            self.log.warning(
                "quay.no_config_digest",
                repository=repository,
                tag=tag_name,
                digest=top_digest,
            )
            return []
        config_blob = await self._fetch_blob(repository, config_digest)
        arch = arch_from_config_blob(config_blob)
        if not arch:
            self.log.warning(
                "quay.no_architecture",
                repository=repository,
                tag=tag_name,
                digest=top_digest,
            )
            return []
        seen_top_digests.add(top_digest)
        return [
            QuayImageRow(
                image_id=top_digest,
                tag=tag_name,
                digest=top_digest,
                architecture=arch,
                build_date=build_date,
                raw_json=raw_tag,
            )
        ]

    async def _fetch_manifest(
        self, repository: str, reference: str
    ) -> dict[str, Any]:
        response = await self.client.get(
            manifest_url(repository, reference),
            ttl_seconds=self.settings.cache_ttl_stable_seconds,
            headers={"Accept": _MANIFEST_ACCEPT},
        )
        return response.json()

    async def _fetch_blob(self, repository: str, digest: str) -> dict[str, Any]:
        response = await self.client.get(
            blob_url(repository, digest),
            ttl_seconds=self.settings.cache_ttl_stable_seconds,
            headers={"Accept": _BLOB_ACCEPT},
        )
        return response.json()


__all__ = [
    "API_BASE",
    "REGISTRY",
    "REGISTRY_V2",
    "QuayCollector",
    "QuayImageRow",
    "arch_from_config_blob",
    "blob_url",
    "config_digest_of",
    "is_manifest_list",
    "manifest_list_children",
    "manifest_url",
    "persist_image",
    "tags_url",
]
