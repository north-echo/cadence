"""Tests for WP-14: collection_run recording, health, metrics, systemd units."""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from threading import Thread

from rich.console import Console

from cadence.config import Settings
from cadence.db import apply_migrations, connect, record_collection_run
from cadence.health import (
    EXPECTED_INTERVAL_SECONDS,
    health_check,
    render_health,
)
from cadence.metrics import render_metrics


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


def _record(
    settings: Settings, *, source: str, age_seconds: float,
    records: int = 1, errors: int = 0,
) -> None:
    completed = datetime.now(UTC) - timedelta(seconds=age_seconds)
    started = completed - timedelta(seconds=10)
    err_msgs = [f"err-{i}" for i in range(errors)]
    with connect(settings.db_path) as conn:
        record_collection_run(
            conn,
            source=source,
            started_at=started.isoformat(),
            completed_at=completed.isoformat(),
            records=records,
            errors=err_msgs or None,
        )


# ----------------------------------------------------------------------
# Migration smoke
# ----------------------------------------------------------------------


def test_migration_002_creates_collection_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    with connect(settings.db_path) as conn:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(collection_run)").fetchall()
        }
    assert cols == {
        "id", "source", "started_at", "completed_at",
        "records", "errors", "error_messages",
    }


# ----------------------------------------------------------------------
# record_collection_run
# ----------------------------------------------------------------------


def test_record_collection_run_no_errors(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _record(settings, source="rhsa", age_seconds=60, records=42)
    with connect(settings.db_path) as conn:
        row = conn.execute(
            "SELECT source, records, errors, error_messages FROM collection_run"
        ).fetchone()
    assert row == ("rhsa", 42, 0, None)


def test_record_collection_run_with_errors_stores_json(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _record(settings, source="csaf", age_seconds=0, records=10, errors=2)
    with connect(settings.db_path) as conn:
        row = conn.execute(
            "SELECT errors, error_messages FROM collection_run"
        ).fetchone()
    assert row[0] == 2
    import json
    assert json.loads(row[1]) == ["err-0", "err-1"]


# ----------------------------------------------------------------------
# health_check
# ----------------------------------------------------------------------


def test_health_check_never_ran_when_table_empty(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    with connect(settings.db_path) as conn:
        result = health_check(conn)
    statuses = {s.source: s.status for s in result.sources}
    assert set(statuses.values()) == {"never_ran"}
    assert not result.overall_ok


def test_health_check_ok_when_all_fresh(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    for src in EXPECTED_INTERVAL_SECONDS:
        _record(settings, source=src, age_seconds=60)
    with connect(settings.db_path) as conn:
        result = health_check(conn)
    assert {s.status for s in result.sources} == {"ok"}
    assert result.overall_ok


def test_health_check_stale_when_one_source_silent(tmp_path: Path) -> None:
    """One source is past 2x interval — overall must report stale."""
    settings = _settings(tmp_path)
    _init_db(settings)
    for src in EXPECTED_INTERVAL_SECONDS:
        if src == "rhsa":
            continue
        _record(settings, source=src, age_seconds=60)
    # rhsa only has a too-old run (4h interval, so >8h age qualifies)
    _record(settings, source="rhsa", age_seconds=9 * 3600)
    with connect(settings.db_path) as conn:
        result = health_check(conn)
    stale = [s for s in result.sources if s.status == "stale"]
    assert len(stale) == 1 and stale[0].source == "rhsa"
    assert not result.overall_ok


def test_health_check_ignores_fully_failed_runs(tmp_path: Path) -> None:
    """A run with records=0 AND errors>0 doesn't reset the staleness clock.

    Mirrors the CLI exit-code policy: "completely useless" runs are not
    counted as freshness; partial-success runs (records>0, errors>0) are.
    """
    settings = _settings(tmp_path)
    _init_db(settings)
    _record(settings, source="rhsa", age_seconds=9 * 3600, errors=0)
    # Recent but useless: records=0 + errors=3
    _record(settings, source="rhsa", age_seconds=60, records=0, errors=3)
    with connect(settings.db_path) as conn:
        result = health_check(conn)
    rhsa = next(s for s in result.sources if s.source == "rhsa")
    # Most recent useful run is 9h old; rhsa expected 4h → 9 > 2x4 → stale.
    assert rhsa.status == "stale"


def test_health_check_partial_success_is_fresh(tmp_path: Path) -> None:
    """The original 'errors=0 only' policy mis-classified 9400-records/1-error
    runs as silent. Verify the new semantics treat that as fresh.
    """
    settings = _settings(tmp_path)
    _init_db(settings)
    _record(settings, source="rhsa", age_seconds=60, records=9403, errors=1)
    with connect(settings.db_path) as conn:
        result = health_check(conn)
    rhsa = next(s for s in result.sources if s.source == "rhsa")
    assert rhsa.status == "ok"
    assert rhsa.last_records == 9403


def test_health_check_picks_latest_successful_run(tmp_path: Path) -> None:
    """Multiple successful runs — the most recent one is reported."""
    settings = _settings(tmp_path)
    _init_db(settings)
    _record(settings, source="catalog", age_seconds=3600 * 12)
    _record(settings, source="catalog", age_seconds=600)   # fresher
    with connect(settings.db_path) as conn:
        result = health_check(conn)
    catalog = next(s for s in result.sources if s.source == "catalog")
    assert catalog.status == "ok"
    assert catalog.age_seconds is not None and catalog.age_seconds < 700


def test_render_health_writes_status_for_each_source(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _record(settings, source="rhsa", age_seconds=60)
    with connect(settings.db_path) as conn:
        result = health_check(conn)
    buf = StringIO()
    console = Console(file=buf, width=160, force_terminal=False, color_system=None)
    render_health(result, console)
    out = buf.getvalue()
    assert "rhsa" in out
    assert "csaf" in out
    assert "never ran" in out  # csaf has no record


# ----------------------------------------------------------------------
# metrics
# ----------------------------------------------------------------------


def test_render_metrics_emits_prometheus_text(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _init_db(settings)
    _record(settings, source="rhsa", age_seconds=600, records=17)
    text = render_metrics(settings.db_path)
    # Mandatory exposition-format pieces
    assert "# TYPE cadence_collection_ok gauge" in text
    assert "# HELP cadence_database_bytes" in text
    assert 'cadence_collection_ok{source="rhsa"} 1.0' in text
    # The records gauge reflects the row we just wrote
    assert 'cadence_collection_last_records{source="rhsa"} 17.0' in text
    # cadence_up is always 1
    assert "cadence_up 1.0" in text
    # Database bytes is set
    m = re.search(r"^cadence_database_bytes (\d+)", text, re.M)
    assert m is not None and int(m.group(1)) > 0


def test_render_metrics_on_missing_db_still_serves(tmp_path: Path) -> None:
    """The metrics endpoint must not crash when the DB hasn't been initialised."""
    missing = tmp_path / "absent.db"
    text = render_metrics(missing)
    assert "cadence_up 1.0" in text
    assert "cadence_database_bytes 0" in text
    # Every source reports ok=0 with no last-success line
    assert 'cadence_collection_ok{source="rhsa"} 0.0' in text


def test_metrics_endpoint_serves_over_http(tmp_path: Path) -> None:
    """End-to-end: start the HTTP server, scrape /metrics, shut it down."""
    import socket

    from cadence.metrics import _Handler, _MetricsServer

    settings = _settings(tmp_path)
    _init_db(settings)
    _record(settings, source="rhsa", age_seconds=60)

    # Bind to an ephemeral port so two test runs don't collide.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = _MetricsServer(("127.0.0.1", port), _Handler)
    server.db_path = settings.db_path
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/metrics", timeout=2,
        ) as resp:
            body = resp.read().decode("utf-8")
            assert resp.status == 200
            assert "cadence_up" in body
        # /
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/", timeout=2,
        ) as resp:
            assert b"CADENCE metrics" in resp.read()
        # 404
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/nope", timeout=2,
            )
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()
        server.server_close()


# ----------------------------------------------------------------------
# systemd unit syntax sanity
# ----------------------------------------------------------------------


_UNIT_DIR = Path(__file__).resolve().parents[1] / "systemd" / "user"

EXPECTED_SERVICES = {
    "cadence-collect-rhsa.service",
    "cadence-collect-csaf.service",
    "cadence-collect-repodata.service",
    "cadence-collect-catalog.service",
    "cadence-collect-quay.service",
}
EXPECTED_TIMERS = {s.replace(".service", ".timer") for s in EXPECTED_SERVICES}


def test_systemd_units_all_present() -> None:
    present = {p.name for p in _UNIT_DIR.iterdir()}
    assert EXPECTED_SERVICES.issubset(present)
    assert EXPECTED_TIMERS.issubset(present)


def test_systemd_services_have_required_directives() -> None:
    for name in EXPECTED_SERVICES:
        text = (_UNIT_DIR / name).read_text()
        assert "[Service]" in text, name
        assert "ExecStart=" in text, name
        assert "Type=oneshot" in text, name
        # Cohabitation guards
        assert "Nice=10" in text, name
        assert "MemoryMax=1G" in text, name


def test_systemd_timers_have_required_directives() -> None:
    for name in EXPECTED_TIMERS:
        text = (_UNIT_DIR / name).read_text()
        assert "[Timer]" in text, name
        assert "OnCalendar=" in text, name
        assert "RandomizedDelaySec=10min" in text, name
        assert "Persistent=true" in text, name


def test_systemd_timer_minute_offsets_avoid_round_marks() -> None:
    """Spec WP-14: minute offsets at :17 or :47, never :00/:15/:30/:45."""
    for name in EXPECTED_TIMERS:
        text = (_UNIT_DIR / name).read_text()
        oncal_lines = [ln for ln in text.splitlines()
                       if ln.startswith("OnCalendar=")]
        assert oncal_lines, name
        for ln in oncal_lines:
            # Parse out the minute portion of HH:MM:SS
            match = re.search(r":(\d{2}):\d{2}", ln)
            assert match is not None, f"{name}: {ln}"
            assert match.group(1) in ("17", "47"), f"{name}: {ln}"


def test_cache_prune_unit_present_and_well_formed() -> None:
    """Post-incident addition: nightly cache-prune timer."""
    svc = _UNIT_DIR / "cadence-cache-prune.service"
    tmr = _UNIT_DIR / "cadence-cache-prune.timer"
    assert svc.exists() and tmr.exists()

    svc_text = svc.read_text()
    assert "ExecStart=" in svc_text
    assert "cadence cache prune" in svc_text
    assert "Type=oneshot" in svc_text

    tmr_text = tmr.read_text()
    assert "OnCalendar=" in tmr_text
    assert "Persistent=true" in tmr_text
    # Must use the :17/:47 offset convention
    m = re.search(r":(\d{2}):\d{2}", tmr_text)
    assert m is not None and m.group(1) in ("17", "47")


def test_systemd_timers_dont_collide_within_the_same_hour() -> None:
    """Different timers should not fire at the same HH:MM."""
    seen: dict[tuple[str, str], str] = {}
    pat = re.compile(r"OnCalendar=\*-\*-\* ([\d,]+):(\d{2}):\d{2}")
    for name in EXPECTED_TIMERS:
        text = (_UNIT_DIR / name).read_text()
        m = pat.search(text)
        assert m is not None, name
        hours, minute = m.group(1), m.group(2)
        for hour in hours.split(","):
            key = (hour, minute)
            assert key not in seen, (
                f"{name} collides with {seen.get(key)} at {hour}:{minute}"
            )
            seen[key] = name
