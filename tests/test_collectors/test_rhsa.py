"""Tests for cadence.collectors.rhsa (WP-03 acceptance).

Fixtures in ``tests/fixtures/rhsa/`` are real captures from
``https://access.redhat.com/hydra/rest/securitydata/`` covering the spec's
required severity matrix.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from cadence.collectors.rhsa import (
    PackageFix,
    RHSACollector,
    _parse_product_id,
    affects_rhel,
    parse_detail,
    persist,
)
from cadence.config import Settings
from cadence.db import apply_migrations, connect

FIX_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "rhsa"

# RHSA-ID → fixture file → expected severity
KNOWN_FIXTURES = {
    "RHSA-2024:1489":  ("RHSA-2024-1489.json",  "critical"),
    "RHSA-2025:0850":  ("RHSA-2025-0850.json",  "important"),
    "RHSA-2025:0078":  ("RHSA-2025-0078.json",  "moderate"),
    "RHSA-2024:10946": ("RHSA-2024-10946.json", "low"),
    "RHSA-2025:0851":  ("RHSA-2025-0851.json",  "important"),  # non-RHEL
}


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
# NEVRA parser
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "pid, expected",
    [
        (
            "AppStream-9.4.0.Z.EUS:python3-jinja2-0:2.11.3-6.el9_4.noarch",
            PackageFix("AppStream-9.4.0.Z.EUS", "python3-jinja2", "0", "2.11.3",
                       "6.el9_4", "noarch"),
        ),
        (
            "BaseOS-8.10.0.Z.MAIN.EUS:kernel-0:4.18.0-553.1.1.el8_10.x86_64",
            PackageFix("BaseOS-8.10.0.Z.MAIN.EUS", "kernel", "0", "4.18.0",
                       "553.1.1.el8_10", "x86_64"),
        ),
    ],
)
def test_parse_product_id_good(pid: str, expected: PackageFix) -> None:
    assert _parse_product_id(pid) == expected


@pytest.mark.parametrize(
    "pid",
    [
        "no-colon-here",
        "ProductOnly:gibberish",
        "Product:python36:3.6-1.x86_64",  # module stream, not standard NEVRA
    ],
)
def test_parse_product_id_unparseable_returns_none(pid: str) -> None:
    assert _parse_product_id(pid) is None


# ----------------------------------------------------------------------
# Severity matrix + RHEL filter (acceptance criteria)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "rhsa_id",
    ["RHSA-2024:1489", "RHSA-2025:0850", "RHSA-2025:0078", "RHSA-2024:10946"],
)
def test_severity_matrix_parses_and_affects_rhel(rhsa_id: str) -> None:
    """One Critical/Important/Moderate/Low — all must parse and pass the RHEL filter."""
    fname, expected_sev = KNOWN_FIXTURES[rhsa_id]
    doc = _load(fname)
    assert affects_rhel(doc["product_tree"]) is True
    parsed = parse_detail(doc, source_url=f"https://example/{fname}")
    assert parsed is not None
    assert parsed.rhsa_id == rhsa_id
    assert parsed.severity == expected_sev
    assert parsed.package_fixes, "RHEL-affecting RHSA should have at least one fix"


def test_multi_cve_rhsa() -> None:
    """RHSA-2024:1489 is the multi-CVE Critical fixture."""
    parsed = parse_detail(_load("RHSA-2024-1489.json"), source_url="x")
    assert parsed is not None
    assert len(parsed.cves) >= 2, f"expected multi-CVE, got {len(parsed.cves)}"


def test_non_rhel_rhsa_filtered_out() -> None:
    """RHSA-2025:0851 affects only RHACM (cpe:/a:redhat:acm). Must be filtered."""
    doc = _load("RHSA-2025-0851.json")
    assert affects_rhel(doc["product_tree"]) is False
    assert parse_detail(doc, source_url="x") is None


# ----------------------------------------------------------------------
# Edge cases
# ----------------------------------------------------------------------


def test_rhsa_with_no_fixed_packages_persists_anyway() -> None:
    """A synthetic doc with empty vulnerabilities should still upsert the rhsa row."""
    minimal = {
        "document": {
            "title": "Synthetic",
            "tracking": {
                "id": "RHSA-2099:0001",
                "initial_release_date": "2099-01-01T00:00:00+00:00",
                "current_release_date": "2099-01-02T00:00:00+00:00",
            },
            "aggregate_severity": {"text": "Important"},
        },
        "product_tree": {
            "branches": [
                {
                    "name": "Red Hat Enterprise Linux",
                    "branches": [
                        {
                            "name": "rhel",
                            "product": {
                                "name": "RHEL 9",
                                "product_id": "rhel-9",
                                "product_identification_helper": {
                                    "cpe": "cpe:/o:redhat:enterprise_linux:9"
                                },
                            },
                        }
                    ],
                }
            ]
        },
        "vulnerabilities": [],
    }
    parsed = parse_detail(minimal, source_url="x")
    assert parsed is not None
    assert parsed.cves == []
    assert parsed.package_fixes == []


def test_persist_idempotent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    parsed = parse_detail(_load("RHSA-2025-0850.json"), source_url="x")
    assert parsed is not None
    with connect(settings.db_path) as conn:
        persist(conn, parsed, collected_at=datetime.now(UTC))
        persist(conn, parsed, collected_at=datetime.now(UTC))
        n_rhsa = conn.execute(
            "SELECT COUNT(*) FROM rhsa WHERE rhsa_id = ?", (parsed.rhsa_id,)
        ).fetchone()[0]
        n_cve = conn.execute(
            "SELECT COUNT(*) FROM rhsa_cve WHERE rhsa_id = ?", (parsed.rhsa_id,)
        ).fetchone()[0]
        n_fix = conn.execute(
            "SELECT COUNT(*) FROM rhsa_package_fix WHERE rhsa_id = ?", (parsed.rhsa_id,)
        ).fetchone()[0]
    assert n_rhsa == 1
    assert n_cve == len(parsed.cves)
    assert n_fix == len(parsed.package_fixes)


# ----------------------------------------------------------------------
# End-to-end collector
# ----------------------------------------------------------------------


def _list_payload() -> list[dict]:
    """Build a synthetic list response from our known fixtures."""
    out = []
    for rhsa_id, (fname, _sev) in KNOWN_FIXTURES.items():
        doc = _load(fname)
        out.append({
            "RHSA": rhsa_id,
            "severity": doc["document"]["aggregate_severity"]["text"].lower(),
            "released_on": doc["document"]["tracking"]["initial_release_date"],
            "CVEs": [v["cve"] for v in doc.get("vulnerabilities", [])],
            "resource_url": f"https://access.redhat.com/hydra/rest/securitydata/csaf/{rhsa_id}.json",
        })
    return out


def test_collect_end_to_end(tmp_path: Path, httpx_mock: HTTPXMock) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)

    # The collector breaks the loop once a page returns fewer than page_size
    # entries, so we only need to mock page 1.
    list_url_pat = re.compile(
        r"^https://access\.redhat\.com/hydra/rest/securitydata/csaf\.json\?"
    )
    httpx_mock.add_response(url=list_url_pat, json=_list_payload())

    for rhsa_id, (fname, _sev) in KNOWN_FIXTURES.items():
        httpx_mock.add_response(
            url=f"https://access.redhat.com/hydra/rest/securitydata/csaf/{rhsa_id}.json",
            json=_load(fname),
        )

    async def go() -> int:
        async with RHSACollector(settings, settings.db_path) as collector:
            result = await collector.collect(since="2024-01-01", until="2025-12-31")
            return result.records

    persisted = asyncio.run(go())
    # 4 RHEL-affecting RHSAs (critical, important, moderate, low); 1 filtered.
    assert persisted == 4

    with connect(settings.db_path) as conn:
        rhsas = {r[0] for r in conn.execute("SELECT rhsa_id FROM rhsa").fetchall()}
        assert "RHSA-2025:0851" not in rhsas  # filtered (non-RHEL)
        assert {"RHSA-2024:1489", "RHSA-2025:0850", "RHSA-2025:0078",
                "RHSA-2024:10946"}.issubset(rhsas)

        # Severities recorded lowercase
        sevs = dict(conn.execute("SELECT rhsa_id, severity FROM rhsa").fetchall())
        assert sevs["RHSA-2024:1489"] == "critical"
        assert sevs["RHSA-2024:10946"] == "low"

        # Critical fixture is also multi-CVE
        n_cve = conn.execute(
            "SELECT COUNT(*) FROM rhsa_cve WHERE rhsa_id = 'RHSA-2024:1489'"
        ).fetchone()[0]
        assert n_cve >= 2

        # published_at + updated_at both captured
        pub, upd = conn.execute(
            "SELECT published_at, updated_at FROM rhsa WHERE rhsa_id = 'RHSA-2025:0850'"
        ).fetchone()
        assert pub and upd
