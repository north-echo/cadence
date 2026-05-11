"""Smoke tests for the WP-01 skeleton."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from cadence import __version__
from cadence.cli import main
from cadence.db import apply_migrations, connect, list_migrations
from cadence.targets import ALL_REPOS, by_source, by_tier


def test_cli_help_runs() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for sub in ("db", "collect", "verify", "analyze", "report", "export", "health"):
        assert sub in result.output


def test_db_init_and_migrate_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "cadence.db"
    runner = CliRunner()

    result = runner.invoke(main, ["--db-path", str(db_path), "db", "init"])
    assert result.exit_code == 0, result.output
    assert db_path.exists()

    # Second run applies nothing.
    result = runner.invoke(main, ["--db-path", str(db_path), "db", "migrate"])
    assert result.exit_code == 0, result.output
    assert "Up to date" in result.output


def test_apply_migrations_records_all(tmp_path: Path) -> None:
    db_path = tmp_path / "cadence.db"
    with connect(db_path) as conn:
        applied = apply_migrations(conn)
        assert applied == list_migrations()

        # All expected tables exist.
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}
        for expected in (
            "rhsa",
            "rhsa_cve",
            "rhsa_package_fix",
            "rhsa_vex",
            "repo_observation",
            "repo_package",
            "container_image",
            "container_image_rpm",
            "catalog_advisory_mapping",
            "gap_measurement",
            "rebuild_interval",
            "tracked_repository",
        ):
            assert expected in tables, f"missing table: {expected}"


def test_targets_have_rationale() -> None:
    assert len(ALL_REPOS) >= 30
    for repo in ALL_REPOS:
        assert repo.rationale, f"empty rationale on {repo.repository}"
    assert by_source("catalog")
    assert by_source("quay")
    assert by_tier("ubi")
    assert by_tier("ocp_platform")


def test_version_exposed() -> None:
    assert __version__
