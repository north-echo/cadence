"""Tests for cadence.analysis.export — WP-12 acceptance.

Acceptance points exercised here:

* Dataset loadable by pandas, polars, jq → loadable by pyarrow + jsonl/json read
* Manifest documents provenance enough for external verification
* Raw archive is reproducible-builds-friendly (same input → identical bytes)
"""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import zstandard

from cadence.analysis.export import (
    DATASET_COLUMNS,
    export_dataset,
    export_raw,
)
from cadence.config import Settings
from cadence.db import apply_migrations, connect


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        cache_dir=tmp_path / "cache",
        db_path=tmp_path / "cadence.db",
        rate_limit_per_host=0,
    )


def _init_db(settings: Settings) -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(settings.db_path) as conn:
        apply_migrations(conn)


def _seed_population(settings: Settings) -> None:
    pub = datetime(2025, 1, 15, tzinfo=UTC)
    now = datetime.now(UTC).isoformat()
    with connect(settings.db_path) as conn:
        for rhsa, sev, days in [("RHSA-2025:1", "critical", 0),
                                ("RHSA-2025:2", "low", 14)]:
            pub_dt = (pub + timedelta(days=days)).isoformat()
            conn.execute(
                """INSERT INTO rhsa
                     (rhsa_id, title, severity, published_at, source_url,
                      raw_json, collected_at)
                   VALUES (?, ?, ?, ?, 'x',
                           ?, ?)""",
                (rhsa, f"Test {rhsa}", sev, pub_dt,
                 json.dumps({"document": {"tracking": {"id": rhsa}}}),
                 pub_dt),
            )
            for tier, gap_days in (("ubi", 5), ("rh_layered", 26)):
                conn.execute(
                    """INSERT INTO gap_measurement
                         (rhsa_id, repository, tier, architecture,
                          package_name, fixed_version,
                          rhsa_published_at, repo_first_seen_at,
                          image_first_built_at, image_id,
                          gap_a_seconds, gap_b_seconds, gap_c_seconds,
                          computed_at, methodology_version)
                       VALUES (?, ?, ?, 'x86_64', 'pkg', '0:1-1.el9',
                               ?, NULL, NULL, NULL,
                               NULL, NULL, ?, ?, 'v1')""",
                    (rhsa, f"{tier}/r", tier, pub_dt, gap_days * 86400, now),
                )
        # And a container_image row with raw_json (raw archive consumer)
        conn.execute(
            """INSERT INTO container_image
                 (image_id, source, registry, repository, tier, tag, digest,
                  architecture, build_date, raw_json, collected_at)
               VALUES ('img-1', 'catalog', 'r', 'ubi9/ubi', 'ubi', 't',
                       'sha256:abc', 'x86_64', ?,
                       ?, ?)""",
            (pub.isoformat(),
             json.dumps({"_id": "img-1", "creation_date": pub.isoformat()}),
             now),
        )


# ----------------------------------------------------------------------
# export_dataset
# ----------------------------------------------------------------------


def test_export_dataset_writes_all_expected_files(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_population(settings)
    out = tmp_path / "out"
    with connect(settings.db_path) as conn:
        manifest = export_dataset(conn, out)

    # All bundled artefacts exist
    for name in ("cadence-dataset.parquet", "cadence-dataset.csv",
                 "cadence-dataset.json", "manifest.json"):
        assert (out / name).exists(), name

    # methodology.md + LICENSE copied from repo
    assert (out / "methodology.md").exists()
    assert (out / "LICENSE").exists()

    # Manifest content sanity
    manifest_obj = json.loads((out / "manifest.json").read_text())
    assert manifest_obj["methodology_version"] == "v1"
    assert manifest_obj["row_count"] == 4    # 2 RHSAs x 2 tiers
    assert manifest_obj["rhsa_count"] == 2
    assert manifest_obj["columns"] == list(DATASET_COLUMNS)
    assert manifest_obj["earliest_rhsa_published_at"]
    assert manifest_obj["latest_rhsa_published_at"]
    # Files block has sha256 + bytes for each artefact
    by_name = {f["name"]: f for f in manifest_obj["files"]}
    for f in ("cadence-dataset.parquet", "cadence-dataset.csv",
              "cadence-dataset.json"):
        assert f in by_name
        assert by_name[f]["bytes"] > 0
        assert len(by_name[f]["sha256"]) == 64
    # Sources block documents every upstream endpoint
    source_names = {s["name"] for s in manifest_obj["sources"]}
    assert {"Red Hat Security Data API", "cdn-ubi.redhat.com",
            "Red Hat Container Catalog API", "Quay.io"}.issubset(source_names)
    # Manifest object matches returned dataclass
    assert manifest.row_count == 4


def test_export_dataset_parquet_is_pyarrow_loadable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_population(settings)
    out = tmp_path / "out"
    with connect(settings.db_path) as conn:
        export_dataset(conn, out)

    table = pq.read_table(out / "cadence-dataset.parquet")
    assert table.num_rows == 4
    assert set(table.column_names) == set(DATASET_COLUMNS)
    severities = set(table.column("severity").to_pylist())
    assert severities == {"critical", "low"}


def test_export_dataset_jsonl_is_jq_loadable(tmp_path: Path) -> None:
    """jq treats the file as a stream of JSON values (one per line)."""
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_population(settings)
    out = tmp_path / "out"
    with connect(settings.db_path) as conn:
        export_dataset(conn, out)

    text = (out / "cadence-dataset.json").read_text()
    lines = [ln for ln in text.splitlines() if ln]
    assert len(lines) == 4
    for line in lines:
        obj = json.loads(line)
        assert set(obj.keys()) == set(DATASET_COLUMNS)

    # Verify with jq if available (mirrors what an external reader would do)
    from shutil import which

    if which("jq"):
        result = subprocess.run(
            ["jq", "-s", "length", str(out / "cadence-dataset.json")],
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "4"


def test_export_dataset_csv_round_trips(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_population(settings)
    out = tmp_path / "out"
    with connect(settings.db_path) as conn:
        export_dataset(conn, out)
    import csv

    with (out / "cadence-dataset.csv").open() as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    assert len(rows) == 4
    assert all(set(r.keys()) == set(DATASET_COLUMNS) for r in rows)


def test_export_dataset_methodology_version_filter(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_population(settings)
    # Insert one row at a different methodology version
    with connect(settings.db_path) as conn:
        conn.execute(
            """INSERT INTO gap_measurement
                 (rhsa_id, repository, tier, architecture,
                  package_name, fixed_version,
                  rhsa_published_at, computed_at, methodology_version)
               VALUES ('RHSA-2025:1', 'ubi9/ubi', 'ubi', 'x86_64',
                       'pkg', '0:1-1.el9',
                       '2025-01-15T00:00:00+00:00', ?, 'v2-experiment')""",
            (datetime.now(UTC).isoformat(),),
        )
    out_v1 = tmp_path / "v1"
    out_v2 = tmp_path / "v2"
    with connect(settings.db_path) as conn:
        m1 = export_dataset(conn, out_v1, methodology_version="v1")
        m2 = export_dataset(conn, out_v2, methodology_version="v2-experiment")
    assert m1.row_count == 4
    assert m2.row_count == 1


# ----------------------------------------------------------------------
# export_raw
# ----------------------------------------------------------------------


def _read_tar_entries(path: Path) -> list[tuple[str, int, bytes]]:
    raw = path.read_bytes()
    dctx = zstandard.ZstdDecompressor()
    decoded = dctx.decompress(raw)
    out = []
    with tarfile.open(fileobj=io.BytesIO(decoded), mode="r") as tar:
        for member in tar.getmembers():
            content = b""
            fh = tar.extractfile(member)
            if fh is not None:
                content = fh.read()
            out.append((member.name, member.mtime, content))
    return out


def test_export_raw_writes_expected_entries(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_population(settings)
    out = tmp_path / "raw.tar.zst"
    with connect(settings.db_path) as conn:
        n = export_raw(conn, out)
    assert n == 3  # 2 RHSAs + 1 container_image with raw_json

    entries = _read_tar_entries(out)
    names = sorted(e[0] for e in entries)
    assert names == [
        "container_image/img-1.json",
        "rhsa/RHSA-2025_1.json",  # colons sanitised
        "rhsa/RHSA-2025_2.json",
    ]
    # All entries have mtime=0 (deterministic)
    assert all(mtime == 0 for _, mtime, _ in entries)


def test_export_raw_is_deterministic_byte_for_byte(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_population(settings)
    a = tmp_path / "a.tar.zst"
    b = tmp_path / "b.tar.zst"
    with connect(settings.db_path) as conn:
        export_raw(conn, a)
        export_raw(conn, b)
    assert a.read_bytes() == b.read_bytes()


def test_export_raw_entries_in_sorted_order(tmp_path: Path) -> None:
    """Required for reproducibility: directory order can't leak into the archive."""
    settings = _settings(tmp_path)
    _init_db(settings)
    # Insert rows in deliberately-non-sorted order
    with connect(settings.db_path) as conn:
        for rhsa in ("RHSA-2025:9", "RHSA-2025:1", "RHSA-2025:5"):
            conn.execute(
                """INSERT INTO rhsa
                     (rhsa_id, title, severity, published_at, source_url,
                      raw_json, collected_at)
                   VALUES (?, 't', 'low', '2025-01-01T00:00:00+00:00',
                           'x', '{}', '2025-01-01T00:00:00+00:00')""",
                (rhsa,),
            )
    out = tmp_path / "raw.tar.zst"
    with connect(settings.db_path) as conn:
        export_raw(conn, out)
    names = [e[0] for e in _read_tar_entries(out)]
    assert names == sorted(names)


def test_export_raw_skips_empty_raw_json(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    with connect(settings.db_path) as conn:
        # Container row with an empty raw_json — must be skipped.
        conn.execute(
            """INSERT INTO container_image
                 (image_id, source, registry, repository, tier, tag, digest,
                  architecture, build_date, raw_json, collected_at)
               VALUES ('img-empty', 'catalog', 'r', 'ubi9/ubi', 'ubi', 't',
                       'd', 'x86_64', '2025-01-01T00:00:00+00:00',
                       '{}', '2025-01-01T00:00:00+00:00')""",
        )
    out = tmp_path / "raw.tar.zst"
    with connect(settings.db_path) as conn:
        n = export_raw(conn, out)
    assert n == 0  # the empty raw_json was skipped
