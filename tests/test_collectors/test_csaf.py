"""Tests for cadence.collectors.csaf (WP-04 acceptance)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from cadence.collectors.csaf import (
    CSAFCollector,
    VEXStatement,
    csaf_url_for,
    extract_vex,
    persist,
)
from cadence.config import Settings
from cadence.db import apply_migrations, connect

FIX_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "rhsa"


def _load(name: str) -> dict:
    return json.loads((FIX_DIR / name).read_text())


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


# ----------------------------------------------------------------------
# csaf_url_for
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "rhsa_id, expected",
    [
        (
            "RHSA-2025:0850",
            "https://access.redhat.com/security/data/csaf/v2/advisories/2025/rhsa-2025_0850.json",
        ),
        (
            "RHSA-2024:1489",
            "https://access.redhat.com/security/data/csaf/v2/advisories/2024/rhsa-2024_1489.json",
        ),
    ],
)
def test_csaf_url_for(rhsa_id: str, expected: str) -> None:
    assert csaf_url_for(rhsa_id) == expected


@pytest.mark.parametrize("bad", ["RHBA-2025:0001", "RHSA-2025-0850", "not-an-rhsa"])
def test_csaf_url_for_rejects_bad_ids(bad: str) -> None:
    with pytest.raises(ValueError):
        csaf_url_for(bad)


# ----------------------------------------------------------------------
# VEX extraction — acceptance: cover all four statuses
# ----------------------------------------------------------------------


def test_extract_vex_covers_all_four_statuses() -> None:
    doc = _load("synthetic_all_statuses.json")
    statements = {s.status for s in extract_vex(doc)}
    assert statements == {"fixed", "affected", "not_affected", "under_investigation"}


def test_extract_vex_skips_unmapped_status() -> None:
    """`recommended` is documented as ignored — it's a workaround pointer, not VEX."""
    doc = _load("synthetic_all_statuses.json")
    products = {s.product_id for s in extract_vex(doc)}
    assert not any("recommendation-pkg" in p for p in products)


def test_extract_vex_captures_flag_justification() -> None:
    doc = _load("synthetic_all_statuses.json")
    not_affected = [
        s for s in extract_vex(doc) if s.status == "not_affected"
    ]
    assert len(not_affected) == 1
    assert not_affected[0].justification == "vulnerable_code_not_in_execute_path"


def test_extract_vex_real_rhsa_has_only_fixed() -> None:
    """Real-world RHSAs almost always carry `fixed` only (documented in NOTES.md)."""
    doc = _load("RHSA-2025-0850.json")
    statements = extract_vex(doc)
    assert statements, "non-empty"
    assert {s.status for s in statements} == {"fixed"}


def test_extract_vex_dedupes_same_product_status_pair() -> None:
    doc = {
        "vulnerabilities": [
            {"product_status": {"fixed": ["p:n-0:1-1.x"]}},
            {"product_status": {"fixed": ["p:n-0:1-1.x"]}},
        ]
    }
    assert len(extract_vex(doc)) == 1


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------


def test_persist_replaces_existing_set(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    rhsa_id = "RHSA-2099:0001"
    first = [
        VEXStatement("p1", "fixed", None),
        VEXStatement("p2", "affected", "reason"),
    ]
    second = [VEXStatement("p3", "not_affected", "vulnerable_code_not_in_execute_path")]

    with connect(settings.db_path) as conn:
        # Need a parent rhsa row for the FK
        conn.execute(
            """
            INSERT INTO rhsa (rhsa_id, title, severity, published_at, source_url,
                              raw_json, collected_at)
            VALUES (?, 'x', 'moderate', '2099-01-01T00:00:00+00:00',
                    'x', '{}', '2099-01-01T00:00:00+00:00')
            """,
            (rhsa_id,),
        )
        persist(conn, rhsa_id, first)
        persist(conn, rhsa_id, second)
        rows = conn.execute(
            "SELECT product_id, status, justification FROM rhsa_vex WHERE rhsa_id = ?",
            (rhsa_id,),
        ).fetchall()

    assert rows == [("p3", "not_affected", "vulnerable_code_not_in_execute_path")]


# ----------------------------------------------------------------------
# End-to-end collector
# ----------------------------------------------------------------------


def _seed_rhsa(settings: Settings, rhsa_id: str) -> None:
    with connect(settings.db_path) as conn:
        conn.execute(
            """
            INSERT INTO rhsa (rhsa_id, title, severity, published_at, source_url,
                              raw_json, collected_at)
            VALUES (?, ?, 'important', '2025-01-30T18:06:01+00:00',
                    'x', '{}', '2025-01-30T18:06:01+00:00')
            """,
            (rhsa_id, rhsa_id),
        )


def test_collect_single_rhsa(tmp_path: Path, httpx_mock: HTTPXMock) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_rhsa(settings, "RHSA-2025:0850")

    httpx_mock.add_response(
        url=csaf_url_for("RHSA-2025:0850"),
        json=_load("RHSA-2025-0850.json"),
    )

    async def go() -> int:
        async with CSAFCollector(settings, settings.db_path) as coll:
            result = await coll.collect(rhsa_ids=["RHSA-2025:0850"])
            return result.records

    n = asyncio.run(go())
    assert n == 1

    with connect(settings.db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM rhsa_vex WHERE rhsa_id = 'RHSA-2025:0850'"
        ).fetchone()[0]
    assert count >= 1


def test_collect_all_known_iterates_rhsa_table(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_rhsa(settings, "RHSA-2025:0850")
    _seed_rhsa(settings, "RHSA-2025:0078")

    httpx_mock.add_response(
        url=csaf_url_for("RHSA-2025:0850"), json=_load("RHSA-2025-0850.json")
    )
    httpx_mock.add_response(
        url=csaf_url_for("RHSA-2025:0078"), json=_load("RHSA-2025-0078.json")
    )

    async def go() -> int:
        async with CSAFCollector(settings, settings.db_path) as coll:
            result = await coll.collect(all_known=True)
            return result.records

    assert asyncio.run(go()) == 2


def test_collect_handles_missing_csaf_gracefully(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_rhsa(settings, "RHSA-2025:0850")

    httpx_mock.add_response(url=csaf_url_for("RHSA-2025:0850"), status_code=404)

    async def go() -> tuple[int, list[str]]:
        async with CSAFCollector(settings, settings.db_path) as coll:
            result = await coll.collect(rhsa_ids=["RHSA-2025:0850"])
            return result.records, result.errors

    records, errors = asyncio.run(go())
    assert records == 0  # nothing persisted
    assert errors == []  # not treated as a failure


def test_collect_raises_on_other_http_errors(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _seed_rhsa(settings, "RHSA-2025:0850")

    # Repeat the 500 enough times to exhaust the default 5 retries (6 total attempts).
    for _ in range(6):
        httpx_mock.add_response(
            url=csaf_url_for("RHSA-2025:0850"), status_code=500
        )

    async def go() -> list[str]:
        # Tighten the client so the test isn't slow.
        from cadence.collectors.base import HTTPClient

        client = HTTPClient(settings, backoff_base_seconds=0.001)
        try:
            coll = CSAFCollector(settings, settings.db_path, client=client)
            result = await coll.collect(rhsa_ids=["RHSA-2025:0850"])
            return result.errors
        finally:
            await client.aclose()

    errors = asyncio.run(go())
    assert errors, "500s should surface as a collector error"
    assert "RHSA-2025:0850" in errors[0]


# Surface httpx in scope so the file is self-contained when read in isolation.
_ = httpx
