"""Tests for cadence.collectors.registry (WP-08 acceptance)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cadence.collectors.registry import (
    SkopeoError,
    SkopeoUnavailable,
    VerificationResult,
    inspect,
    summarize,
    verify_image,
    verify_random_sample,
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


def _insert_image(
    settings: Settings,
    *,
    image_id: str,
    registry: str = "registry.access.redhat.com",
    repository: str,
    tag: str,
    digest: str,
    architecture: str = "x86_64",
    source: str = "catalog",
    tier: str = "ubi",
) -> None:
    with connect(settings.db_path) as conn:
        conn.execute(
            """
            INSERT INTO container_image
                (image_id, source, registry, repository, tier, tag, digest,
                 architecture, build_date, raw_json, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
            """,
            (
                image_id, source, registry, repository, tier, tag, digest,
                architecture, datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )


# ----------------------------------------------------------------------
# inspect() wrapper
# ----------------------------------------------------------------------


def _ok_runner(stdout: str, returncode: int = 0, stderr: str = ""):
    def _run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0], returncode=returncode, stdout=stdout, stderr=stderr
        )
    return _run


def test_inspect_returns_parsed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cadence.collectors.registry.skopeo_available", lambda: True
    )
    doc = inspect(
        "registry.access.redhat.com/ubi9/ubi:9.5",
        runner=_ok_runner('{"Digest": "sha256:abc"}'),
    )
    assert doc == {"Digest": "sha256:abc"}


def test_inspect_raises_skopeo_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cadence.collectors.registry.skopeo_available", lambda: False
    )
    with pytest.raises(SkopeoUnavailable):
        inspect("anything")


def test_inspect_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cadence.collectors.registry.skopeo_available", lambda: True
    )
    with pytest.raises(SkopeoError, match="manifest unknown"):
        inspect(
            "anything",
            runner=_ok_runner("", returncode=1, stderr="manifest unknown"),
        )


def test_inspect_raises_on_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cadence.collectors.registry.skopeo_available", lambda: True
    )
    with pytest.raises(SkopeoError, match="malformed"):
        inspect("anything", runner=_ok_runner("not json"))


# ----------------------------------------------------------------------
# verify_image — known-good and drift detection
# ----------------------------------------------------------------------


def test_verify_image_known_good(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _insert_image(
        settings,
        image_id="img-1",
        repository="ubi9/ubi",
        tag="9.5-1736",
        digest="sha256:goodgood",
        architecture="x86_64",
    )

    def fake_inspect(_ref: str) -> dict:
        return {"Digest": "sha256:goodgood", "Architecture": "amd64"}

    with connect(settings.db_path) as conn:
        results = verify_image(conn, "ubi9/ubi", "9.5-1736", inspect_fn=fake_inspect)
    assert len(results) == 1
    assert results[0].status == "ok"
    assert results[0].discrepancies == []
    assert results[0].reference == "registry.access.redhat.com/ubi9/ubi:9.5-1736"


def test_verify_image_detects_digest_drift(tmp_path: Path) -> None:
    """The acceptance criterion: corrupted DB state must surface as drift."""
    settings = _settings(tmp_path)
    _init_db(settings)
    _insert_image(
        settings,
        image_id="img-2",
        repository="ubi9/ubi",
        tag="9.5-1736",
        digest="sha256:WRONG",  # intentionally-corrupted database state
        architecture="x86_64",
    )

    def fake_inspect(_ref: str) -> dict:
        return {"Digest": "sha256:goodgood", "Architecture": "amd64"}

    with connect(settings.db_path) as conn:
        results = verify_image(conn, "ubi9/ubi", "9.5-1736", inspect_fn=fake_inspect)
    assert len(results) == 1
    assert results[0].status == "drift"
    assert any("digest" in d for d in results[0].discrepancies)


def test_verify_image_detects_architecture_drift(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _insert_image(
        settings,
        image_id="img-3",
        repository="ubi9/ubi",
        tag="9.5-1736",
        digest="sha256:same",
        architecture="aarch64",  # DB says aarch64
    )

    def fake_inspect(_ref: str) -> dict:
        return {"Digest": "sha256:same", "Architecture": "amd64"}  # registry says amd64

    with connect(settings.db_path) as conn:
        results = verify_image(conn, "ubi9/ubi", "9.5-1736", inspect_fn=fake_inspect)
    assert results[0].status == "drift"
    assert any("architecture" in d for d in results[0].discrepancies)


def test_verify_image_not_in_database(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)

    with connect(settings.db_path) as conn:
        results = verify_image(conn, "ubi9/ubi", "9.5-1736",
                               inspect_fn=lambda r: {})
    assert len(results) == 1
    assert results[0].status == "not_in_database"


def test_verify_image_skopeo_unavailable_is_graceful(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _insert_image(
        settings,
        image_id="img-4",
        repository="ubi9/ubi",
        tag="9.5-1736",
        digest="sha256:x",
    )

    def raises(_ref: str) -> dict:
        raise SkopeoUnavailable("not installed")

    with connect(settings.db_path) as conn:
        results = verify_image(conn, "ubi9/ubi", "9.5-1736", inspect_fn=raises)
    assert results[0].status == "skopeo_unavailable"
    assert results[0].error == "not installed"


def test_verify_image_skopeo_error_reported(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _insert_image(
        settings,
        image_id="img-5",
        repository="ubi9/ubi",
        tag="9.5-1736",
        digest="sha256:x",
    )

    def raises(_ref: str) -> dict:
        raise SkopeoError("manifest unknown")

    with connect(settings.db_path) as conn:
        results = verify_image(conn, "ubi9/ubi", "9.5-1736", inspect_fn=raises)
    assert results[0].status == "error"
    assert results[0].error == "manifest unknown"


def test_verify_image_two_arches_both_verified(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _insert_image(settings, image_id="img-a", repository="ubi9/ubi",
                  tag="9.5-1736", digest="sha256:a", architecture="x86_64")
    _insert_image(settings, image_id="img-b", repository="ubi9/ubi",
                  tag="9.5-1736", digest="sha256:b", architecture="aarch64")

    seen: list[str] = []

    def fake(reference: str) -> dict:
        seen.append(reference)
        # Round-trip whatever digest+arch the DB has, so both verify "ok"
        if "ubi9/ubi" in reference:
            return {"Digest": "sha256:a", "Architecture": "amd64"}
        return {}

    with connect(settings.db_path) as conn:
        results = verify_image(conn, "ubi9/ubi", "9.5-1736", inspect_fn=fake)
    assert len(results) == 2
    # One row matches digest+arch (img-a); the other reports drift because the
    # fake registry only returns the amd64 view.
    statuses = sorted(r.status for r in results)
    assert statuses == ["drift", "ok"]


# ----------------------------------------------------------------------
# verify_random_sample
# ----------------------------------------------------------------------


def test_verify_random_sample_zero_returns_empty(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    with connect(settings.db_path) as conn:
        assert verify_random_sample(conn, 0, inspect_fn=lambda r: {}) == []


def test_verify_random_sample_n_against_db(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    for i in range(3):
        _insert_image(
            settings,
            image_id=f"img-{i}",
            repository=f"ubi9/v{i}",
            tag=f"t-{i}",
            digest=f"sha256:d{i}",
        )

    def fake(reference: str) -> dict:
        # Pick out the digest from the repo path (we crafted them above)
        for i in range(3):
            if f"v{i}" in reference:
                return {"Digest": f"sha256:d{i}", "Architecture": "amd64"}
        return {}

    with connect(settings.db_path) as conn:
        results = verify_random_sample(conn, 2, inspect_fn=fake)
    assert len(results) == 2
    assert all(r.status == "ok" for r in results)


def test_verify_random_sample_stops_on_skopeo_unavailable(tmp_path: Path) -> None:
    """No point pestering skopeo for 100 rows once we know it's not installed."""
    settings = _settings(tmp_path)
    _init_db(settings)
    for i in range(5):
        _insert_image(
            settings, image_id=f"img-{i}", repository="x/y", tag=f"t-{i}",
            digest=f"sha256:{i}",
        )

    def raises(_ref: str) -> dict:
        raise SkopeoUnavailable("no skopeo")

    with connect(settings.db_path) as conn:
        results = verify_random_sample(conn, 5, inspect_fn=raises)
    assert len(results) == 1
    assert results[0].status == "skopeo_unavailable"


# ----------------------------------------------------------------------
# summarize
# ----------------------------------------------------------------------


def test_summarize_counts_by_status() -> None:
    rs = [
        VerificationResult("a", "r", "t", "r:t", "ok"),
        VerificationResult("b", "r", "t", "r:t", "drift", discrepancies=["x"]),
        VerificationResult("c", "r", "t", "r:t", "ok"),
        VerificationResult("d", "r", "t", "r:t", "error", error="boom"),
    ]
    assert summarize(rs) == {"ok": 2, "drift": 1, "error": 1}
