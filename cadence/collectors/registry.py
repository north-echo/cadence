"""Registry verification via ``skopeo inspect`` (WP-08).

Cross-validates rows we've persisted in ``container_image`` against what the
live registry actually serves. The verification is observational, not
gating: discrepancies are reported, never raised, and never modify the
database. When ``skopeo`` is not installed the verifier degrades cleanly
and the operator is told.

When `skopeo inspect` and CADENCE disagree, **the catalog API is treated
as authoritative** (CADENCE-SPEC.md §WP-08 / docs/methodology.md §11):
the discrepancy is recorded for review but no row is rewritten on the
basis of a single registry snapshot.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)


SKOPEO_BINARY = "skopeo"
DEFAULT_TIMEOUT_SECONDS = 30.0


class SkopeoUnavailable(RuntimeError):
    """Raised when the skopeo binary is not on $PATH."""


class SkopeoError(RuntimeError):
    """Raised when skopeo exits non-zero or returns malformed JSON."""


# ---------------------------------------------------------------------------
# skopeo wrapper
# ---------------------------------------------------------------------------


def skopeo_available() -> bool:
    return shutil.which(SKOPEO_BINARY) is not None


def inspect(
    reference: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Run ``skopeo inspect docker://<reference>`` and return the parsed JSON.

    The ``runner`` hook exists so tests can swap subprocess invocation for a
    deterministic stand-in. Production calls leave it ``None`` and use
    :func:`subprocess.run` directly.
    """
    if not skopeo_available():
        raise SkopeoUnavailable("skopeo binary not found on PATH")
    runner = runner or subprocess.run
    proc = runner(
        [SKOPEO_BINARY, "inspect", f"docker://{reference}"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise SkopeoError(proc.stderr.strip() or f"exit code {proc.returncode}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SkopeoError(f"malformed skopeo output: {exc}") from exc


# ---------------------------------------------------------------------------
# Verification model
# ---------------------------------------------------------------------------


@dataclass
class VerificationResult:
    """One image's cross-check between the database and the registry."""

    image_id: str
    repository: str
    tag: str
    reference: str          # the docker://… reference we asked skopeo about
    status: str             # ok | drift | not_in_database | skopeo_unavailable | error
    discrepancies: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _build_reference(registry: str, repository: str, tag: str) -> str:
    return f"{registry}/{repository}:{tag}"


def _compare(
    *,
    image_id: str,
    repository: str,
    tag: str,
    reference: str,
    db_digest: str,
    db_architecture: str,
    inspect_doc: dict[str, Any],
) -> VerificationResult:
    """Diff the database row against one ``skopeo inspect`` payload."""
    discrepancies: list[str] = []
    skopeo_digest = str(inspect_doc.get("Digest") or "")
    if skopeo_digest and db_digest and skopeo_digest != db_digest:
        discrepancies.append(
            f"digest: db={db_digest} registry={skopeo_digest}"
        )

    skopeo_arch = _normalize_arch(str(inspect_doc.get("Architecture") or ""))
    if skopeo_arch and db_architecture and skopeo_arch != db_architecture:
        discrepancies.append(
            f"architecture: db={db_architecture} registry={skopeo_arch}"
        )

    status = "ok" if not discrepancies else "drift"
    return VerificationResult(
        image_id=image_id,
        repository=repository,
        tag=tag,
        reference=reference,
        status=status,
        discrepancies=discrepancies,
    )


_ARCH_TO_KERNEL = {"amd64": "x86_64", "arm64": "aarch64"}


def _normalize_arch(arch: str) -> str:
    """Mirror :func:`cadence.collectors.catalog.normalize_arch` for skopeo output."""
    return _ARCH_TO_KERNEL.get(arch, arch)


# ---------------------------------------------------------------------------
# Database lookups
# ---------------------------------------------------------------------------


def _rows_for(
    conn: sqlite3.Connection, repository: str, tag: str
) -> list[tuple[str, str, str, str]]:
    """Return ``[(image_id, registry, digest, architecture), …]`` for repo+tag."""
    rows = conn.execute(
        """
        SELECT image_id, registry, digest, architecture
          FROM container_image
         WHERE repository = ? AND tag = ?
        """,
        (repository, tag),
    ).fetchall()
    return [tuple(row) for row in rows]  # type: ignore[misc]


def _random_sample(
    conn: sqlite3.Connection, n: int
) -> list[tuple[str, str, str, str, str, str]]:
    rows = conn.execute(
        """
        SELECT image_id, registry, repository, tag, digest, architecture
          FROM container_image
         ORDER BY RANDOM()
         LIMIT ?
        """,
        (n,),
    ).fetchall()
    return [tuple(row) for row in rows]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Public verification API
# ---------------------------------------------------------------------------


def verify_image(
    conn: sqlite3.Connection,
    repository: str,
    tag: str,
    *,
    inspect_fn: Callable[[str], dict[str, Any]] = inspect,
) -> list[VerificationResult]:
    """Verify every database row matching ``(repository, tag)`` against the registry."""
    rows = _rows_for(conn, repository, tag)
    if not rows:
        return [
            VerificationResult(
                image_id="",
                repository=repository,
                tag=tag,
                reference=f"{repository}:{tag}",
                status="not_in_database",
            )
        ]

    results: list[VerificationResult] = []
    for image_id, registry, digest, architecture in rows:
        reference = _build_reference(registry, repository, tag)
        try:
            inspect_doc = inspect_fn(reference)
        except SkopeoUnavailable as exc:
            results.append(
                VerificationResult(
                    image_id=image_id,
                    repository=repository,
                    tag=tag,
                    reference=reference,
                    status="skopeo_unavailable",
                    error=str(exc),
                )
            )
            continue
        except SkopeoError as exc:
            results.append(
                VerificationResult(
                    image_id=image_id,
                    repository=repository,
                    tag=tag,
                    reference=reference,
                    status="error",
                    error=str(exc),
                )
            )
            continue
        results.append(
            _compare(
                image_id=image_id,
                repository=repository,
                tag=tag,
                reference=reference,
                db_digest=digest,
                db_architecture=architecture,
                inspect_doc=inspect_doc,
            )
        )
    return results


def verify_random_sample(
    conn: sqlite3.Connection,
    sample: int,
    *,
    inspect_fn: Callable[[str], dict[str, Any]] = inspect,
) -> list[VerificationResult]:
    """Verify ``sample`` randomly-chosen rows from ``container_image``."""
    if sample <= 0:
        return []
    selected = _random_sample(conn, sample)
    results: list[VerificationResult] = []
    for image_id, registry, repository, tag, digest, architecture in selected:
        reference = _build_reference(registry, repository, tag)
        try:
            inspect_doc = inspect_fn(reference)
        except SkopeoUnavailable as exc:
            results.append(
                VerificationResult(
                    image_id=image_id,
                    repository=repository,
                    tag=tag,
                    reference=reference,
                    status="skopeo_unavailable",
                    error=str(exc),
                )
            )
            # No point retrying every other row if skopeo isn't there.
            break
        except SkopeoError as exc:
            results.append(
                VerificationResult(
                    image_id=image_id,
                    repository=repository,
                    tag=tag,
                    reference=reference,
                    status="error",
                    error=str(exc),
                )
            )
            continue
        results.append(
            _compare(
                image_id=image_id,
                repository=repository,
                tag=tag,
                reference=reference,
                db_digest=digest,
                db_architecture=architecture,
                inspect_doc=inspect_doc,
            )
        )
    return results


# ---------------------------------------------------------------------------
# CLI summarization
# ---------------------------------------------------------------------------


def summarize(results: Iterable[VerificationResult]) -> dict[str, int]:
    """Count results by status, for CLI summaries and tests."""
    out: dict[str, int] = {}
    for r in results:
        out[r.status] = out.get(r.status, 0) + 1
    return out


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "SKOPEO_BINARY",
    "SkopeoError",
    "SkopeoUnavailable",
    "VerificationResult",
    "inspect",
    "skopeo_available",
    "summarize",
    "verify_image",
    "verify_random_sample",
]
