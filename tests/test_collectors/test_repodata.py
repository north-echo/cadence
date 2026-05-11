"""Tests for cadence.collectors.repodata (WP-05 acceptance)."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from cadence.collectors.repodata import (
    DEFAULT_REPOS,
    RepoDataCollector,
    RepoMD,
    RepoPackage,
    iter_primary_packages,
    observation_exists,
    parse_repomd,
    persist,
    primary_url,
    repomd_url,
)
from cadence.config import Settings
from cadence.db import apply_migrations, connect

FIX_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "repodata"
REPO_ID = "ubi9/9/x86_64/baseos"


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


def _real_repomd_bytes() -> bytes:
    return (FIX_DIR / "ubi9_baseos_x86_64_repomd.xml").read_bytes()


def _real_primary_bytes() -> bytes:
    return (FIX_DIR / "ubi9_baseos_x86_64_primary.xml.gz").read_bytes()


# ----------------------------------------------------------------------
# URL helpers
# ----------------------------------------------------------------------


def test_default_repos_contains_18() -> None:
    """3 majors x 2 arches x 3 repos = 18 entries."""
    assert len(DEFAULT_REPOS) == 18
    # Sanity check format
    assert "ubi9/9/x86_64/baseos" in DEFAULT_REPOS
    assert "ubi10/10/aarch64/codeready-builder" in DEFAULT_REPOS


def test_repomd_url() -> None:
    assert repomd_url(REPO_ID) == (
        "https://cdn-ubi.redhat.com/content/public/ubi/dist/"
        "ubi9/9/x86_64/baseos/os/repodata/repomd.xml"
    )


def test_primary_url_resolves_relative_href() -> None:
    assert primary_url(REPO_ID, "repodata/abc-primary.xml.gz") == (
        "https://cdn-ubi.redhat.com/content/public/ubi/dist/"
        "ubi9/9/x86_64/baseos/os/repodata/abc-primary.xml.gz"
    )


# ----------------------------------------------------------------------
# repomd parsing — namespace handling
# ----------------------------------------------------------------------


def test_parse_repomd_real_fixture() -> None:
    r = parse_repomd(_real_repomd_bytes())
    assert r.revision == "1778192199"
    assert r.primary_href.startswith("repodata/")
    assert r.primary_href.endswith("-primary.xml.gz")
    assert len(r.primary_sha256) == 64
    # The href and the sha256 hash match in real repomd.xml documents
    assert r.primary_sha256 in r.primary_href


def test_parse_repomd_missing_revision_raises() -> None:
    bad = b"""<?xml version="1.0"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo">
  <data type="primary"><location href="x"/></data>
</repomd>"""
    with pytest.raises(ValueError, match="revision"):
        parse_repomd(bad)


def test_parse_repomd_missing_primary_raises() -> None:
    bad = b"""<?xml version="1.0"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo">
  <revision>1</revision>
  <data type="filelists"><location href="x"/></data>
</repomd>"""
    with pytest.raises(ValueError, match="primary"):
        parse_repomd(bad)


# ----------------------------------------------------------------------
# primary.xml parsing
# ----------------------------------------------------------------------


def test_iter_primary_packages_real_fixture() -> None:
    pkgs = list(iter_primary_packages(_real_primary_bytes()))
    # The fixture's <metadata packages="569"> attribute is the ground truth.
    assert len(pkgs) == 569
    # Spot-check one known package
    first = next(p for p in pkgs if p.name == "NetworkManager-libnm" and p.arch == "i686")
    assert first.version.startswith("1:")
    assert first.build_time is not None
    assert first.file_time is not None
    assert first.file_time.tzinfo is UTC


def _make_synthetic_primary(packages: list[tuple[str, str, int, int]]) -> bytes:
    """Build a tiny primary.xml.gz with namespace prefixes matching real UBI output."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<metadata xmlns="http://linux.duke.edu/metadata/common"',
        '          xmlns:rpm="http://linux.duke.edu/metadata/rpm"',
        f'          packages="{len(packages)}">',
    ]
    for name, ver_rel, build_ts, file_ts in packages:
        ver, rel = ver_rel.split("-", 1)
        lines += [
            '  <package type="rpm">',
            f"    <name>{name}</name>",
            "    <arch>x86_64</arch>",
            f'    <version epoch="0" ver="{ver}" rel="{rel}"/>',
            f'    <time file="{file_ts}" build="{build_ts}"/>',
            "  </package>",
        ]
    lines.append("</metadata>")
    xml = "\n".join(lines).encode("utf-8")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(xml)
    return buf.getvalue()


def test_iter_primary_packages_synthetic_streaming() -> None:
    blob = _make_synthetic_primary(
        [
            ("openssl", "3.0.7-25.el9_2", 1685000000, 1686000000),
            ("kernel", "5.14.0-284.11.1.el9_2", 1684000000, 1685500000),
        ]
    )
    pkgs = list(iter_primary_packages(blob))
    assert [p.name for p in pkgs] == ["openssl", "kernel"]
    assert pkgs[0].version == "0:3.0.7-25.el9_2"
    assert pkgs[0].build_time == datetime.fromtimestamp(1685000000, tz=UTC)


# ----------------------------------------------------------------------
# Persistence + idempotency
# ----------------------------------------------------------------------


def test_persist_and_observation_exists(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    repomd = RepoMD(revision="r1", primary_href="x", primary_sha256="s")
    packages = [
        RepoPackage("a", "0:1-1.el9", "x86_64", None, None),
        RepoPackage("b", "0:2-1.el9", "x86_64", None, None),
    ]
    with connect(settings.db_path) as conn:
        assert not observation_exists(conn, REPO_ID, "r1")
        obs_id = persist(
            conn,
            repo_id=REPO_ID,
            repomd=repomd,
            packages=packages,
            observed_at=datetime.now(UTC),
        )
        assert obs_id > 0
        assert observation_exists(conn, REPO_ID, "r1")
        n = conn.execute(
            "SELECT COUNT(*) FROM repo_package WHERE observation_id = ?", (obs_id,)
        ).fetchone()[0]
    assert n == 2


# ----------------------------------------------------------------------
# End-to-end collector
# ----------------------------------------------------------------------


def _expected_primary_url(repomd_bytes: bytes) -> str:
    href = parse_repomd(repomd_bytes).primary_href
    return primary_url(REPO_ID, href)


def test_collect_initial_run_populates_observations(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    repomd_bytes = _real_repomd_bytes()
    primary_bytes = _real_primary_bytes()

    httpx_mock.add_response(url=repomd_url(REPO_ID), content=repomd_bytes)
    httpx_mock.add_response(
        url=_expected_primary_url(repomd_bytes), content=primary_bytes
    )

    async def go() -> int:
        async with RepoDataCollector(settings, settings.db_path) as coll:
            result = await coll.collect(repos=[REPO_ID])
            return result.records

    assert asyncio.run(go()) == 1

    with connect(settings.db_path) as conn:
        n_obs = conn.execute(
            "SELECT COUNT(*) FROM repo_observation WHERE repo_id = ?", (REPO_ID,)
        ).fetchone()[0]
        n_pkg = conn.execute("SELECT COUNT(*) FROM repo_package").fetchone()[0]
    assert n_obs == 1
    assert n_pkg == 569


def test_collect_idempotent_when_revision_unchanged(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """Re-running with the same repomd revision must not insert a second observation."""
    settings = _settings(tmp_path)
    _init_db(settings)
    repomd_bytes = _real_repomd_bytes()
    primary_bytes = _real_primary_bytes()

    # First run: full fetch
    httpx_mock.add_response(url=repomd_url(REPO_ID), content=repomd_bytes)
    httpx_mock.add_response(
        url=_expected_primary_url(repomd_bytes), content=primary_bytes
    )
    # Second run: only repomd is fetched; revision matches, so primary is not.
    httpx_mock.add_response(url=repomd_url(REPO_ID), content=repomd_bytes)

    async def go() -> tuple[int, int]:
        async with RepoDataCollector(settings, settings.db_path) as coll:
            first = await coll.collect(repos=[REPO_ID])
            second = await coll.collect(repos=[REPO_ID])
            return first.records, second.records

    first_new, second_new = asyncio.run(go())
    assert first_new == 1
    assert second_new == 0  # unchanged → no new observation

    # Verify only one observation exists in the database
    with connect(settings.db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM repo_observation").fetchone()[0]
    assert n == 1


def test_collect_detects_new_revision(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """When repomd revision changes, the collector must record a new observation."""
    settings = _settings(tmp_path)
    _init_db(settings)
    repomd_bytes = _real_repomd_bytes()
    primary_bytes = _real_primary_bytes()

    # First run: real repomd revision
    httpx_mock.add_response(url=repomd_url(REPO_ID), content=repomd_bytes)
    httpx_mock.add_response(
        url=_expected_primary_url(repomd_bytes), content=primary_bytes
    )

    # Second run: a new revision pointing at a synthetic small primary.
    synthetic_primary = _make_synthetic_primary(
        [("newpkg", "1.0-1.el9", 1700000000, 1700100000)]
    )
    sha = hashlib.sha256(synthetic_primary).hexdigest()
    repomd2 = (
        '<?xml version="1.0"?>\n'
        '<repomd xmlns="http://linux.duke.edu/metadata/repo">\n'
        "  <revision>9999999999</revision>\n"
        '  <data type="primary">\n'
        f'    <checksum type="sha256">{sha}</checksum>\n'
        f'    <location href="repodata/{sha}-primary.xml.gz"/>\n'
        "  </data>\n"
        "</repomd>\n"
    ).encode()

    httpx_mock.add_response(url=repomd_url(REPO_ID), content=repomd2)
    httpx_mock.add_response(
        url=_expected_primary_url(repomd2), content=synthetic_primary
    )

    async def go() -> tuple[int, int]:
        async with RepoDataCollector(settings, settings.db_path) as coll:
            r1 = await coll.collect(repos=[REPO_ID])
            r2 = await coll.collect(repos=[REPO_ID])
            return r1.records, r2.records

    r1, r2 = asyncio.run(go())
    assert r1 == 1
    assert r2 == 1  # new revision triggered a new observation

    with connect(settings.db_path) as conn:
        n_obs = conn.execute(
            "SELECT COUNT(*) FROM repo_observation WHERE repo_id = ?", (REPO_ID,)
        ).fetchone()[0]
        revs = {
            row[0]
            for row in conn.execute(
                "SELECT repomd_revision FROM repo_observation WHERE repo_id = ?",
                (REPO_ID,),
            ).fetchall()
        }
    assert n_obs == 2
    assert revs == {"1778192199", "9999999999"}


def test_collect_rejects_corrupted_primary(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """A primary.xml.gz whose sha256 doesn't match repomd must be rejected."""
    settings = _settings(tmp_path)
    _init_db(settings)
    repomd_bytes = _real_repomd_bytes()

    httpx_mock.add_response(url=repomd_url(REPO_ID), content=repomd_bytes)
    # Wrong content — checksum will not match
    httpx_mock.add_response(
        url=_expected_primary_url(repomd_bytes),
        content=b"definitely not the real primary",
    )

    async def go() -> list[str]:
        async with RepoDataCollector(settings, settings.db_path) as coll:
            result = await coll.collect(repos=[REPO_ID])
            return result.errors

    errors = asyncio.run(go())
    assert errors
    assert "checksum mismatch" in errors[0]

    # No observation persisted on failure
    with connect(settings.db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM repo_observation").fetchone()[0]
    assert n == 0
