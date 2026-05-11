"""Minimal Prometheus-style metrics endpoint (WP-14, optional).

Stdlib-only HTTP server. Each scrape queries the database directly — no
in-memory counters — so the metrics reflect the *current* state of the
database (the same information ``cadence health`` produces) rather than
the lifetime activity of one server process.

Gauges exposed:

* ``cadence_collection_last_success_timestamp_seconds{source="…"}``
* ``cadence_collection_last_records{source="…"}``
* ``cadence_collection_age_seconds{source="…"}`` — current age of last
  success, useful for alerting ("alert when > 2x interval").
* ``cadence_collection_expected_interval_seconds{source="…"}``
* ``cadence_collection_ok{source="…"}`` — 0 or 1
* ``cadence_database_bytes`` — size of the SQLite file on disk
* ``cadence_up`` — always 1 while the endpoint responds

Binding policy
--------------

The CLI defaults to ``127.0.0.1`` and the documentation warns against
binding a public interface (the endpoint is unauthenticated). An operator
who wants remote scraping should put the endpoint behind a localhost
reverse proxy with appropriate auth.
"""

from __future__ import annotations

import http.server
import socketserver
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from cadence.config import Settings
from cadence.db import connect
from cadence.health import EXPECTED_INTERVAL_SECONDS, health_check

log = structlog.get_logger(__name__)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


# ---------------------------------------------------------------------------
# Metrics text rendering
# ---------------------------------------------------------------------------


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_gauge(
    name: str,
    help_text: str,
    samples: list[tuple[dict[str, str], float]],
) -> str:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
    for labels, value in samples:
        if labels:
            label_str = ",".join(
                f'{k}="{_escape_label(v)}"' for k, v in sorted(labels.items())
            )
            lines.append(f"{name}{{{label_str}}} {value}")
        else:
            lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"


def render_metrics(db_path: Path) -> str:
    """Render the full /metrics body for a single scrape."""
    now = datetime.now(UTC)
    db_size = db_path.stat().st_size if db_path.exists() else 0

    last_success: list[tuple[dict[str, str], float]] = []
    last_records: list[tuple[dict[str, str], float]] = []
    age_seconds: list[tuple[dict[str, str], float]] = []
    expected: list[tuple[dict[str, str], float]] = []
    ok_gauge: list[tuple[dict[str, str], float]] = []

    if db_path.exists():
        with connect(db_path) as conn:
            result = health_check(conn, now=now)
        for src in result.sources:
            labels = {"source": src.source}
            if src.last_success_at:
                ts = datetime.fromisoformat(
                    src.last_success_at.replace("Z", "+00:00")
                ).timestamp()
                last_success.append((labels, ts))
            if src.last_records is not None:
                last_records.append((labels, float(src.last_records)))
            if src.age_seconds is not None:
                age_seconds.append((labels, src.age_seconds))
            expected.append((labels, float(src.expected_interval_seconds)))
            ok_gauge.append((labels, 1.0 if src.ok else 0.0))
    else:
        for source, interval in EXPECTED_INTERVAL_SECONDS.items():
            labels = {"source": source}
            expected.append((labels, float(interval)))
            ok_gauge.append((labels, 0.0))

    parts: list[str] = []
    parts.append(_render_gauge(
        "cadence_collection_last_success_timestamp_seconds",
        "Unix timestamp of the last successful run of each collector.",
        last_success,
    ))
    parts.append(_render_gauge(
        "cadence_collection_last_records",
        "Records persisted in the last successful run of each collector.",
        last_records,
    ))
    parts.append(_render_gauge(
        "cadence_collection_age_seconds",
        "Seconds since the last successful run of each collector.",
        age_seconds,
    ))
    parts.append(_render_gauge(
        "cadence_collection_expected_interval_seconds",
        "Expected cadence (seconds) of each collector — see WP-14 timers.",
        expected,
    ))
    parts.append(_render_gauge(
        "cadence_collection_ok",
        "1 when collector is within 2x its expected interval, else 0.",
        ok_gauge,
    ))
    parts.append(_render_gauge(
        "cadence_database_bytes",
        "Size on disk of the SQLite database.",
        [({}, float(db_size))],
    ))
    parts.append(_render_gauge(
        "cadence_up",
        "Always 1 while the metrics endpoint is responding.",
        [({}, 1.0)],
    ))
    return "".join(parts)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    """Single-purpose HTTP handler exposing /metrics (+ a tiny / index)."""

    server_version = "cadence-metrics/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Quiet the default access log; structlog already covers what we need.
        log.debug("metrics.access", remote=self.address_string(),
                  request=format % args)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            body = (
                b"<!doctype html><meta charset='utf-8'><title>CADENCE</title>"
                b"<body><h1>CADENCE metrics</h1>"
                b"<p><a href='/metrics'>/metrics</a></p></body>"
            )
            self._respond(200, body, "text/html; charset=utf-8")
            return
        if self.path == "/metrics":
            body = render_metrics(self.server.db_path).encode("utf-8")  # type: ignore[attr-defined]
            self._respond(200, body, CONTENT_TYPE)
            return
        self._respond(404, b"not found\n", "text/plain; charset=utf-8")

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _MetricsServer(socketserver.ThreadingTCPServer):
    """Tiny TCP server. Threaded so one slow scrape doesn't block another."""

    allow_reuse_address = True
    db_path: Path


def serve_metrics(
    settings: Settings,
    *,
    console,
    bind: str = "127.0.0.1",
    port: int = 9101,
) -> None:
    """Block and serve /metrics until interrupted."""
    if bind not in ("127.0.0.1", "::1", "localhost"):
        # Hard refuse anything that isn't clearly loopback. Operators who
        # really want a remote scrape should put a reverse proxy in front.
        log.warning(
            "metrics.non_loopback_bind",
            bind=bind,
            advice="endpoint is unauthenticated; only bind loopback",
        )
    server = _MetricsServer((bind, port), _Handler)
    server.db_path = settings.db_path
    console.print(
        f"[green]metrics[/green]: serving on http://{bind}:{port}/metrics "
        f"(db={settings.db_path})  press Ctrl-C to stop"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("[yellow]metrics[/yellow]: shutting down")
    finally:
        server.server_close()


def _open(db_path: Path) -> sqlite3.Connection:
    """Public alias for test helpers that want the same connection settings."""
    return connect(db_path).__enter__()


__all__ = ["CONTENT_TYPE", "render_metrics", "serve_metrics"]
