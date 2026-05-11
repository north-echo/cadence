"""Dataset export for publication (WP-12).

Two entry points:

* :func:`export_dataset` writes the flat ``gap_measurement`` table (with
  ``rhsa.severity`` joined in) as Parquet + CSV + JSON-Lines, plus a
  ``manifest.json`` provenance file, a copy of ``methodology.md``, and the
  CC-BY-4.0 ``LICENSE`` for the produced dataset.
* :func:`export_raw` writes a deterministic ``.tar.zst`` archive of every
  ``raw_json`` column collected from upstream (``rhsa.raw_json``,
  ``container_image.raw_json``). Same inputs → identical bytes.

Reproducibility
---------------

* Rows are ordered by stable composite keys before serialisation so the
  output files are byte-for-byte identical across runs on the same input.
* The tar archive uses fixed mtime (UNIX epoch 0), uid/gid 0, and writes
  entries in sorted path order with no extended attributes.
* zstandard is invoked with default level (3) and no embedded timestamps;
  the format's container is deterministic for identical input bytes at
  identical compressor settings.

Dependencies
------------

``pyarrow`` (Parquet) and ``zstandard`` (tar.zst compression) are listed as
the ``[export]`` optional extra in :file:`pyproject.toml`. The module
imports them lazily and raises :class:`ExportDependencyMissing` with a
useful pip-install hint when they aren't available.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from cadence import __version__
from cadence.analysis.reconstruct import DEFAULT_METHODOLOGY_VERSION

log = structlog.get_logger(__name__)


REPO_ROOT = Path(__file__).resolve().parents[2]


class ExportDependencyMissing(RuntimeError):
    """Raised when an optional [export] dependency isn't importable."""


# ---------------------------------------------------------------------------
# Dataset (parquet/csv/jsonl) export
# ---------------------------------------------------------------------------


# Column order in every export artefact. Keep stable: external readers
# rely on it.
DATASET_COLUMNS: tuple[str, ...] = (
    "rhsa_id",
    "severity",
    "rhsa_published_at",
    "repository",
    "tier",
    "architecture",
    "package_name",
    "fixed_version",
    "repo_first_seen_at",
    "image_first_built_at",
    "image_id",
    "gap_a_seconds",
    "gap_b_seconds",
    "gap_c_seconds",
    "methodology_version",
    "computed_at",
)


_DATASET_SQL = """
    SELECT
        g.rhsa_id,
        r.severity,
        g.rhsa_published_at,
        g.repository,
        g.tier,
        g.architecture,
        g.package_name,
        g.fixed_version,
        g.repo_first_seen_at,
        g.image_first_built_at,
        g.image_id,
        g.gap_a_seconds,
        g.gap_b_seconds,
        g.gap_c_seconds,
        g.methodology_version,
        g.computed_at
      FROM gap_measurement AS g
      LEFT JOIN rhsa AS r ON r.rhsa_id = g.rhsa_id
     WHERE g.methodology_version = ?
     ORDER BY g.rhsa_id, g.repository, g.architecture,
              g.package_name, g.fixed_version
"""


@dataclass
class DatasetManifest:
    """Provenance + summary for ``manifest.json``."""

    cadence_version: str
    methodology_version: str
    generated_at: str
    row_count: int
    rhsa_count: int
    earliest_rhsa_published_at: str | None
    latest_rhsa_published_at: str | None
    columns: list[str]
    files: list[dict[str, Any]]
    sources: list[dict[str, str]]


def _ensure_dep(module: str, install_hint: str) -> Any:
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover — exercised in tests via mock
        raise ExportDependencyMissing(
            f"{module!r} is required for `cadence export`. "
            f"Install with: pip install '{install_hint}'."
        ) from exc


def _fetch_rows(
    conn: sqlite3.Connection, methodology_version: str
) -> list[dict[str, Any]]:
    rows = conn.execute(_DATASET_SQL, (methodology_version,)).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({col: row[i] for i, col in enumerate(DATASET_COLUMNS)})
    return out


def _write_parquet(rows: list[dict[str, Any]], path: Path) -> None:
    pa = _ensure_dep("pyarrow", "ne-cadence[export]")
    import pyarrow.parquet as pq

    columns: dict[str, list[Any]] = {col: [] for col in DATASET_COLUMNS}
    for row in rows:
        for col in DATASET_COLUMNS:
            columns[col].append(row[col])
    table = pa.table(columns)
    pq.write_table(table, path, compression="zstd")


def _write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(DATASET_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: (v if v is not None else "") for k, v in row.items()})


def _write_jsonl(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True))
            fh.write("\n")


def _earliest_latest(rows: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    published = [r["rhsa_published_at"] for r in rows if r["rhsa_published_at"]]
    if not published:
        return None, None
    return min(published), max(published)


def _build_manifest(
    rows: list[dict[str, Any]],
    *,
    methodology_version: str,
    files: list[dict[str, Any]],
) -> DatasetManifest:
    earliest, latest = _earliest_latest(rows)
    return DatasetManifest(
        cadence_version=__version__,
        methodology_version=methodology_version,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        row_count=len(rows),
        rhsa_count=len({r["rhsa_id"] for r in rows if r["rhsa_id"]}),
        earliest_rhsa_published_at=earliest,
        latest_rhsa_published_at=latest,
        columns=list(DATASET_COLUMNS),
        files=files,
        sources=[
            {
                "name": "Red Hat Security Data API",
                "url": "https://access.redhat.com/hydra/rest/securitydata/csaf.json",
                "purpose": "RHSA list + per-advisory CSAF",
            },
            {
                "name": "cdn-ubi.redhat.com",
                "url": "https://cdn-ubi.redhat.com/content/public/ubi/",
                "purpose": "UBI repodata (forward-only)",
            },
            {
                "name": "Red Hat Container Catalog API",
                "url": "https://catalog.redhat.com/api/containers/v1/",
                "purpose": "Image metadata + RPM manifests",
            },
            {
                "name": "Quay.io",
                "url": "https://quay.io/api/v1/",
                "purpose": "Quay-hosted tag history and OCI manifests",
            },
        ],
    )


def _hash_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _maybe_copy(source: Path, dest: Path) -> bool:
    if not source.exists():
        return False
    dest.write_bytes(source.read_bytes())
    return True


def export_dataset(
    conn: sqlite3.Connection,
    output_dir: Path,
    *,
    methodology_version: str = DEFAULT_METHODOLOGY_VERSION,
    repo_root: Path | None = None,
) -> DatasetManifest:
    """Write the publishable dataset bundle to ``output_dir``.

    Returns the :class:`DatasetManifest` for tests / callers that want it.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    repo_root = repo_root or REPO_ROOT
    rows = _fetch_rows(conn, methodology_version)

    parquet_path = output_dir / "cadence-dataset.parquet"
    csv_path = output_dir / "cadence-dataset.csv"
    jsonl_path = output_dir / "cadence-dataset.json"
    _write_parquet(rows, parquet_path)
    _write_csv(rows, csv_path)
    _write_jsonl(rows, jsonl_path)

    # Methodology + license copies for self-contained distribution
    _maybe_copy(repo_root / "docs" / "methodology.md", output_dir / "methodology.md")
    _maybe_copy(repo_root / "DATASET-LICENSE", output_dir / "LICENSE")

    files = [
        {"name": p.name, "bytes": p.stat().st_size, "sha256": _hash_file(p)}
        for p in (parquet_path, csv_path, jsonl_path)
    ]
    manifest = _build_manifest(
        rows, methodology_version=methodology_version, files=files
    )
    (output_dir / "manifest.json").write_text(
        json.dumps(_manifest_to_dict(manifest), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    log.info(
        "export.dataset_done",
        rows=manifest.row_count,
        rhsa_count=manifest.rhsa_count,
        output_dir=str(output_dir),
    )
    return manifest


def _manifest_to_dict(m: DatasetManifest) -> dict[str, Any]:
    return {
        "cadence_version": m.cadence_version,
        "methodology_version": m.methodology_version,
        "generated_at": m.generated_at,
        "row_count": m.row_count,
        "rhsa_count": m.rhsa_count,
        "earliest_rhsa_published_at": m.earliest_rhsa_published_at,
        "latest_rhsa_published_at": m.latest_rhsa_published_at,
        "columns": m.columns,
        "files": m.files,
        "sources": m.sources,
    }


# ---------------------------------------------------------------------------
# Raw archive (tar.zst)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RawEntry:
    path: str       # in-archive path, sorted-order key
    content: bytes


def _collect_raw_entries(conn: sqlite3.Connection) -> list[_RawEntry]:
    entries: list[_RawEntry] = []
    for rhsa_id, raw_json in conn.execute(
        "SELECT rhsa_id, raw_json FROM rhsa WHERE raw_json IS NOT NULL"
    ).fetchall():
        if raw_json:
            entries.append(_RawEntry(
                path=f"rhsa/{_sanitize(rhsa_id)}.json",
                content=raw_json.encode("utf-8"),
            ))
    for image_id, raw_json in conn.execute(
        "SELECT image_id, raw_json FROM container_image "
        "WHERE raw_json IS NOT NULL AND raw_json != '' AND raw_json != '{}'"
    ).fetchall():
        if raw_json:
            entries.append(_RawEntry(
                path=f"container_image/{_sanitize(image_id)}.json",
                content=raw_json.encode("utf-8"),
            ))
    entries.sort(key=lambda e: e.path)
    return entries


def _sanitize(value: str) -> str:
    return value.replace("/", "_").replace(":", "_")


def export_raw(conn: sqlite3.Connection, output_file: Path) -> int:
    """Write ``output_file`` as a deterministic ``tar.zst`` of raw_json columns.

    Returns the number of entries written.
    """
    zstd = _ensure_dep("zstandard", "ne-cadence[export]")
    entries = _collect_raw_entries(conn)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    tar_buf = io.BytesIO()
    # Open the tar without the `format=` kwarg defaulting to PAX, which embeds
    # additional headers with file-creation metadata. POSIX USTAR is enough
    # for our payload and writes fewer non-deterministic bytes.
    with tarfile.open(fileobj=tar_buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for entry in entries:
            info = tarfile.TarInfo(name=entry.path)
            info.size = len(entry.content)
            info.mtime = 0           # fixed timestamp
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(entry.content))

    raw_bytes = tar_buf.getvalue()
    compressor = zstd.ZstdCompressor(level=3, threads=0)
    compressed = compressor.compress(raw_bytes)
    output_file.write_bytes(compressed)
    log.info(
        "export.raw_done",
        entries=len(entries),
        bytes=output_file.stat().st_size,
        output_file=str(output_file),
    )
    return len(entries)


__all__: Iterable[str] = (
    "DATASET_COLUMNS",
    "DatasetManifest",
    "ExportDependencyMissing",
    "export_dataset",
    "export_raw",
)
