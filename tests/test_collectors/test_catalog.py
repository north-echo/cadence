"""Tests for cadence.collectors.catalog (WP-06 acceptance)."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from cadence.collectors.catalog import (
    BASE_URL,
    CatalogCollector,
    epoch_from_srpm_nevra,
    list_url,
    normalize_arch,
    parse_image,
    parse_rpm_manifest,
    parse_tag,
    rpm_manifest_url,
    seed_tracked_repositories,
    to_catalog_arch,
)
from cadence.config import Settings
from cadence.db import apply_migrations, connect
from cadence.targets import ALL_REPOS, by_source

FIX_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "catalog"


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


def _load(name: str) -> dict:
    return json.loads((FIX_DIR / name).read_text())


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("9.5-1736", ("9.5", 1736)),
        ("9.0.0-1468", ("9.0.0", 1468)),
        ("10-001", ("10", 1)),
    ],
)
def test_parse_tag_good(tag: str, expected: tuple[str, int]) -> None:
    assert parse_tag(tag) == expected


@pytest.mark.parametrize("tag", ["latest", "9", "9.4", "9.0.0", "1736"])
def test_parse_tag_returns_none_for_floating(tag: str) -> None:
    assert parse_tag(tag) is None


def test_normalize_and_to_catalog_arch() -> None:
    assert normalize_arch("amd64") == "x86_64"
    assert normalize_arch("arm64") == "aarch64"
    assert normalize_arch("ppc64le") == "ppc64le"  # passthrough
    assert to_catalog_arch("x86_64") == "amd64"
    assert to_catalog_arch("aarch64") == "arm64"


@pytest.mark.parametrize(
    "srpm_nevra, expected",
    [
        ("attr-0:2.5.1-3.el9.src", "0"),
        ("openssl-1:3.0.7-25.el9_2.src", "1"),
        ("", "0"),
        (None, "0"),
        ("malformed", "0"),
    ],
)
def test_epoch_from_srpm_nevra(srpm_nevra: str | None, expected: str) -> None:
    assert epoch_from_srpm_nevra(srpm_nevra) == expected


def test_list_url_x86_64() -> None:
    url = list_url("ubi9/ubi", page=2, page_size=100, arch="x86_64")
    assert "architecture==amd64" in url
    assert "page=2" in url
    assert "page_size=100" in url
    assert "sort_by=creation_date%5Basc%5D" in url


def test_list_url_with_since() -> None:
    url = list_url("ubi9/ubi", page=0, page_size=100, arch="aarch64",
                   since="2024-01-01")
    assert "architecture==arm64" in url
    assert "creation_date>=2024-01-01" in url


def test_rpm_manifest_url() -> None:
    assert rpm_manifest_url("/v1/images/id/abc/rpm-manifest") == (
        f"{BASE_URL}/images/id/abc/rpm-manifest"
    )


# ----------------------------------------------------------------------
# Image parser
# ----------------------------------------------------------------------


def test_parse_image_pre_nov_2024_captures_advisory_rpm_mapping() -> None:
    page = _load("list_page_0.json")
    img = page["data"][0]  # 2024-08-27 image
    parsed = parse_image(img, repository="ubi9/ubi", tier="ubi")
    assert parsed is not None
    assert parsed.source == "catalog"
    assert parsed.registry == "registry.access.redhat.com"
    assert parsed.architecture == "x86_64"  # mapped from amd64
    assert parsed.image_id == "66cde1906bb7b043784b1714"
    assert parsed.parsed_build_num is not None  # tag matches X.Y-NNN
    assert parsed.parsed_version is not None
    assert parsed.rpm_manifest_href is not None
    # Pre-Nov-2024 image carries the legacy mapping
    assert len(parsed.advisory_rpm_mapping) > 0
    first = parsed.advisory_rpm_mapping[0]
    assert "nvra" in first and "advisory_ids" in first


def test_parse_image_post_nov_2024_has_no_advisory_rpm_mapping() -> None:
    page = _load("list_page_1.json")  # captured 2026 (recent)
    img = page["data"][0]
    parsed = parse_image(img, repository="ubi9/ubi", tier="ubi")
    assert parsed is not None
    # Recent images have advisory_rpm_mapping absent or empty
    assert parsed.advisory_rpm_mapping == []


def test_parse_image_unparseable_tag_is_not_fatal(caplog: pytest.LogCaptureFixture) -> None:
    raw = {
        "_id": "x",
        "architecture": "amd64",
        "creation_date": "2025-01-01T00:00:00+00:00",
        "_links": {"rpm_manifest": {"href": "/v1/images/id/x/rpm-manifest"}},
        "repositories": [
            {
                "registry": "registry.access.redhat.com",
                "repository": "ubi9/ubi",
                "manifest_schema2_digest": "sha256:deadbeef",
                "tags": [{"name": "latest"}, {"name": "9"}],
            }
        ],
    }
    parsed = parse_image(raw, repository="ubi9/ubi", tier="ubi")
    assert parsed is not None
    assert parsed.tag in ("latest", "9")
    assert parsed.parsed_build_num is None
    assert parsed.parsed_version is None


def test_parse_image_picks_highest_build_num_tag() -> None:
    raw = {
        "_id": "x",
        "architecture": "amd64",
        "creation_date": "2025-01-01T00:00:00+00:00",
        "brew": {"completion_date": "2025-01-01T00:00:00+00:00"},
        "_links": {"rpm_manifest": {"href": "/v1/images/id/x/rpm-manifest"}},
        "repositories": [
            {
                "registry": "registry.access.redhat.com",
                "repository": "ubi9/ubi",
                "manifest_schema2_digest": "sha256:d",
                "tags": [
                    {"name": "9.5-1700"},
                    {"name": "9.5-1736"},
                    {"name": "9.5"},
                    {"name": "latest"},
                ],
            }
        ],
    }
    parsed = parse_image(raw, repository="ubi9/ubi", tier="ubi")
    assert parsed is not None
    assert parsed.tag == "9.5-1736"
    assert parsed.parsed_build_num == 1736


# ----------------------------------------------------------------------
# RPM manifest parser
# ----------------------------------------------------------------------


def test_parse_rpm_manifest_extracts_epoch_from_srpm_nevra() -> None:
    doc = _load("rpm_66c2b8271db8d8285288aecd.json")
    rpms = parse_rpm_manifest(doc)
    assert rpms
    # basesystem-11-13.el9.noarch, srpm_nevra "basesystem-0:11-13.el9.src" → epoch 0
    bs = next((r for r in rpms if r.package_name == "basesystem"), None)
    assert bs is not None
    assert bs.version.startswith("0:")
    assert bs.arch == "noarch"


def test_parse_rpm_manifest_dedupes_on_name_arch() -> None:
    doc = {"rpms": [
        {"name": "x", "version": "1", "release": "1.el9", "architecture": "x86_64",
         "srpm_nevra": "x-0:1-1.el9.src"},
        {"name": "x", "version": "1", "release": "1.el9", "architecture": "x86_64",
         "srpm_nevra": "x-0:1-1.el9.src"},
    ]}
    assert len(parse_rpm_manifest(doc)) == 1


# ----------------------------------------------------------------------
# Seeding
# ----------------------------------------------------------------------


def test_seed_tracked_repositories_inserts_every_target(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    with connect(settings.db_path) as conn:
        added = seed_tracked_repositories(conn)
    assert added == len(ALL_REPOS)

    # Idempotent: a second call adds zero.
    with connect(settings.db_path) as conn:
        added2 = seed_tracked_repositories(conn)
        n = conn.execute("SELECT COUNT(*) FROM tracked_repository").fetchone()[0]
    assert added2 == 0
    assert n == len(ALL_REPOS)


def test_resolve_targets_rejects_unknown_repo() -> None:
    with pytest.raises(ValueError, match="unknown repository"):
        CatalogCollector._resolve_targets(["not/in/targets"])


def test_resolve_targets_rejects_quay_source() -> None:
    # cilium/cilium is a Quay-source repo; must be rejected for the catalog collector
    with pytest.raises(ValueError, match=r"source='quay'|cadence collect quay"):
        CatalogCollector._resolve_targets(["cilium/cilium"])


# ----------------------------------------------------------------------
# End-to-end collector
# ----------------------------------------------------------------------


_LIST_URL_PAT = re.compile(
    re.escape(BASE_URL)
    + r"/repositories/registry/registry\.access\.redhat\.com/repository/ubi9/ubi/images"
)


def _serve_two_pages(httpx_mock: HTTPXMock) -> None:
    """Mock catalog so paging stops after the second response.

    list_page_0.json declares total=5, page_size=2; serve page 0 (2 images),
    page 1 (2 images), page 2 (1 image) so the collector sees total = 5.
    """
    page0 = _load("list_page_0.json")
    page1 = _load("list_page_1.json")
    # Synthesize a third page with one image to round out total=5.
    page2 = {
        "data": page1["data"][:1],  # reuse one record as a stand-in
        "page": 2, "page_size": 2, "total": 5,
    }
    httpx_mock.add_response(url=_LIST_URL_PAT, json=page0)
    httpx_mock.add_response(url=_LIST_URL_PAT, json=page1)
    httpx_mock.add_response(url=_LIST_URL_PAT, json=page2)


def _mock_rpm_manifests(httpx_mock: HTTPXMock) -> None:
    for fixture in FIX_DIR.glob("rpm_*.json"):
        image_id = fixture.stem[len("rpm_"):]
        httpx_mock.add_response(
            url=f"{BASE_URL}/images/id/{image_id}/rpm-manifest",
            json=_load(fixture.name),
            is_reusable=True,
        )


def test_collect_end_to_end_paginates_and_persists(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _serve_two_pages(httpx_mock)
    _mock_rpm_manifests(httpx_mock)

    async def go() -> int:
        async with CatalogCollector(settings, settings.db_path) as coll:
            coll.page_size = 2  # match the fixture pagination
            result = await coll.collect(
                repos=["ubi9/ubi"], arches=("x86_64",)
            )
            return result.records

    n = asyncio.run(go())
    assert n >= 4  # 2+2+1 across three pages; some may share _id

    with connect(settings.db_path) as conn:
        n_images = conn.execute(
            "SELECT COUNT(DISTINCT image_id) FROM container_image"
        ).fetchone()[0]
        # All images normalized to kernel arch
        arches = {row[0] for row in conn.execute(
            "SELECT DISTINCT architecture FROM container_image"
        ).fetchall()}
        # advisory_rpm_mapping populated for pre-Nov-2024 records
        n_map = conn.execute("SELECT COUNT(*) FROM catalog_advisory_mapping").fetchone()[0]
        n_rpm = conn.execute("SELECT COUNT(*) FROM container_image_rpm").fetchone()[0]
        tiers = {row[0] for row in conn.execute(
            "SELECT DISTINCT tier FROM container_image"
        ).fetchall()}

    assert n_images >= 2
    assert arches == {"x86_64"}
    assert n_map > 0  # pre-Nov-2024 fixture contributed mappings
    assert n_rpm > 0
    assert tiers == {"ubi"}


def test_collect_default_targets_is_catalog_repos_only(tmp_path: Path) -> None:
    """Default repo set must be the catalog-source subset, not the whole list."""
    catalog_repos = by_source("catalog")
    resolved = CatalogCollector._resolve_targets(None)
    assert set(r.repository for r in resolved) == set(r.repository for r in catalog_repos)
