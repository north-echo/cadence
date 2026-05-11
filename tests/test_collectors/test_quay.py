"""Tests for cadence.collectors.quay (WP-07 acceptance)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from cadence.collectors.quay import (
    API_BASE,
    REGISTRY_V2,
    QuayCollector,
    QuayImageRow,
    arch_from_config_blob,
    blob_url,
    config_digest_of,
    is_manifest_list,
    manifest_list_children,
    manifest_url,
    persist_image,
    tags_url,
)
from cadence.config import Settings
from cadence.db import apply_migrations, connect
from cadence.targets import by_source

FIX_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "quay"


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
# URL helpers
# ----------------------------------------------------------------------


def test_tags_url() -> None:
    assert tags_url("cilium/cilium", page=2, page_size=50) == (
        f"{API_BASE}/repository/cilium/cilium/tag/?limit=50"
        "&page=2&onlyActiveTags=true"
    )


def test_manifest_and_blob_urls() -> None:
    assert manifest_url("cilium/cilium", "sha256:abc") == (
        f"{REGISTRY_V2}/cilium/cilium/manifests/sha256:abc"
    )
    assert blob_url("cilium/cilium", "sha256:def") == (
        f"{REGISTRY_V2}/cilium/cilium/blobs/sha256:def"
    )


# ----------------------------------------------------------------------
# Manifest classification
# ----------------------------------------------------------------------


def test_is_manifest_list_real_fixture() -> None:
    assert is_manifest_list(_load("manifest_list.json")) is True
    assert is_manifest_list(_load("single_manifest.json")) is False


def test_manifest_list_children_real_fixture() -> None:
    children = manifest_list_children(_load("manifest_list.json"))
    assert len(children) == 2
    arches = {arch for _digest, arch in children}
    assert arches == {"amd64", "arm64"}
    # All digests are sha256:...
    for digest, _ in children:
        assert digest.startswith("sha256:")


def test_manifest_list_children_drops_unknown_arch_entries() -> None:
    """OCI image indexes can carry attestation entries with platform.architecture='unknown'."""
    fake = {
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {"digest": "sha256:a", "platform": {"architecture": "amd64", "os": "linux"}},
            {"digest": "sha256:b", "platform": {"architecture": "unknown", "os": "unknown"}},
            {"digest": "sha256:c", "platform": {}},  # no architecture
            {"platform": {"architecture": "arm64"}},  # no digest
        ],
    }
    children = manifest_list_children(fake)
    assert children == [("sha256:a", "amd64")]


def test_config_digest_and_arch_real_fixture() -> None:
    manifest = _load("single_manifest.json")
    config = _load("config_blob.json")
    digest = config_digest_of(manifest)
    assert digest is not None and digest.startswith("sha256:")
    assert arch_from_config_blob(config) == "amd64"


# ----------------------------------------------------------------------
# Resolve targets
# ----------------------------------------------------------------------


def test_resolve_default_targets_is_quay_only() -> None:
    quay = by_source("quay")
    resolved = QuayCollector._resolve_targets(None)
    assert {r.repository for r in resolved} == {r.repository for r in quay}


def test_resolve_targets_rejects_catalog_source() -> None:
    with pytest.raises(ValueError, match=r"cadence collect catalog"):
        QuayCollector._resolve_targets(["ubi9/ubi"])


def test_resolve_targets_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown repository"):
        QuayCollector._resolve_targets(["bogus/repo"])


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------


def test_persist_upserts_on_image_id_collision(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    row = QuayImageRow(
        image_id="sha256:abc",
        tag="v1",
        digest="sha256:abc",
        architecture="amd64",
        build_date=datetime.now(UTC),
        raw_json="{}",
    )
    later = QuayImageRow(
        image_id="sha256:abc",
        tag="v1.1",
        digest="sha256:abc",
        architecture="amd64",
        build_date=datetime.now(UTC),
        raw_json='{"updated": true}',
    )
    with connect(settings.db_path) as conn:
        persist_image(conn, row=row, repository="cilium/cilium", tier="quay_community",
                      collected_at=datetime.now(UTC))
        persist_image(conn, row=later, repository="cilium/cilium", tier="quay_community",
                      collected_at=datetime.now(UTC))
        rows = conn.execute(
            "SELECT image_id, tag, source FROM container_image"
        ).fetchall()
    assert rows == [("sha256:abc", "v1.1", "quay")]  # later tag wins, source preserved


# ----------------------------------------------------------------------
# End-to-end collector
# ----------------------------------------------------------------------


def _mock_tag_pages(httpx_mock: HTTPXMock, repo: str) -> None:
    """Serve the two captured tag-list pages with has_additional flipped on page 2."""
    p1 = _load("tags_page_1.json")
    p2 = _load("tags_page_2.json")
    # Truncate to keep the test small + flip has_additional so iteration stops.
    p1 = {**p1, "tags": p1["tags"][:2], "has_additional": True}
    p2 = {**p2, "tags": p2["tags"][:1], "has_additional": False}
    httpx_mock.add_response(
        url=tags_url(repo, page=1, page_size=QuayCollector.tag_page_size), json=p1
    )
    httpx_mock.add_response(
        url=tags_url(repo, page=2, page_size=QuayCollector.tag_page_size), json=p2
    )


def _mock_manifest_list_for_tag(
    httpx_mock: HTTPXMock, repo: str, tag: dict
) -> None:
    """Serve manifest_list.json for every tag whose top digest matches the fixture."""
    httpx_mock.add_response(
        url=manifest_url(repo, tag["manifest_digest"]),
        json=_load("manifest_list.json"),
        is_reusable=True,
    )


def test_collect_end_to_end_manifest_list_resolution(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    repo = "cilium/cilium"

    _mock_tag_pages(httpx_mock, repo)

    # All real fixture tags happen to be manifest lists.
    page1_tags = _load("tags_page_1.json")["tags"][:2]
    page2_tags = _load("tags_page_2.json")["tags"][:1]
    for tag in page1_tags + page2_tags:
        _mock_manifest_list_for_tag(httpx_mock, repo, tag)

    async def go() -> int:
        async with QuayCollector(settings, settings.db_path) as coll:
            result = await coll.collect(repos=[repo])
            return result.records

    n = asyncio.run(go())
    assert n > 0

    with connect(settings.db_path) as conn:
        rows = conn.execute(
            """SELECT source, registry, repository, tier, architecture
               FROM container_image"""
        ).fetchall()
    assert rows, "expected at least one persisted row"
    for source, registry, repository, tier, architecture in rows:
        assert source == "quay"
        assert registry == "quay.io"
        assert repository == repo
        assert tier == "quay_community"  # cilium is in QUAY_COMMUNITY_REPOS
        assert architecture in {"amd64", "arm64"}


def test_collect_single_manifest_path_uses_config_blob(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """A tag whose manifest is NOT a list must trigger the config-blob fetch."""
    settings = _settings(tmp_path)
    _init_db(settings)
    repo = "cilium/cilium"

    # Build a single-tag page pointing at single_manifest.json
    manifest = _load("single_manifest.json")
    config_blob = _load("config_blob.json")
    top_digest = "sha256:single-arch-tag-digest"
    config_digest = manifest["config"]["digest"]
    tag_row = {
        "name": "single-arch-tag",
        "start_ts": 1700000000,
        "manifest_digest": top_digest,
        "is_manifest_list": False,
        "last_modified": "Mon, 14 Nov 2023 22:13:20 -0000",
    }
    page = {"page": 1, "has_additional": False, "tags": [tag_row]}

    httpx_mock.add_response(
        url=tags_url(repo, page=1, page_size=QuayCollector.tag_page_size), json=page
    )
    httpx_mock.add_response(url=manifest_url(repo, top_digest), json=manifest)
    httpx_mock.add_response(url=blob_url(repo, config_digest), json=config_blob)

    async def go() -> int:
        async with QuayCollector(settings, settings.db_path) as coll:
            result = await coll.collect(repos=[repo])
            return result.records

    n = asyncio.run(go())
    assert n == 1

    with connect(settings.db_path) as conn:
        row = conn.execute(
            "SELECT image_id, architecture, tag FROM container_image"
        ).fetchone()
    assert row == (top_digest, "amd64", "single-arch-tag")


def test_collect_idempotent_rerun_does_not_duplicate(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    repo = "cilium/cilium"

    # Cache TTL means the second run hits the cache for both tag pages and
    # manifests; we only register each mock once.
    _mock_tag_pages(httpx_mock, repo)
    page1_tags = _load("tags_page_1.json")["tags"][:2]
    page2_tags = _load("tags_page_2.json")["tags"][:1]
    for tag in page1_tags + page2_tags:
        httpx_mock.add_response(
            url=manifest_url(repo, tag["manifest_digest"]),
            json=_load("manifest_list.json"),
            is_reusable=True,
        )

    async def go() -> None:
        async with QuayCollector(settings, settings.db_path) as coll:
            await coll.collect(repos=[repo])
            await coll.collect(repos=[repo])

    asyncio.run(go())
    with connect(settings.db_path) as conn:
        # Each row keyed by per-arch digest — manifest_list_children yields 2
        # per top-level tag, and our fixture has 3 distinct top digests, all
        # mocked to the same manifest list, so unique image_ids = 2 (amd64+arm64).
        n = conn.execute(
            "SELECT COUNT(DISTINCT image_id) FROM container_image"
        ).fetchone()[0]
    assert n == 2  # not duplicated by the second run
