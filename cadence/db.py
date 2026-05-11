"""SQLite connection and migration runner.

Migrations live in ``cadence/schema/*.sql`` and are applied in lexicographic
order. Each file is wrapped in a single transaction; partial application is
not possible. Applied migrations are recorded in ``schema_migrations``.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

DEFAULT_DB_PATH = Path(
    os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
) / "cadence" / "cadence.db"

SCHEMA_PACKAGE = "cadence.schema"


def list_migrations() -> list[str]:
    """Return migration filenames in apply order."""
    files = (
        r.name
        for r in resources.files(SCHEMA_PACKAGE).iterdir()
        if r.name.endswith(".sql") and r.is_file()
    )
    return sorted(files)


def _read_migration(name: str) -> str:
    return resources.files(SCHEMA_PACKAGE).joinpath(name).read_text(encoding="utf-8")


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with sensible defaults for CADENCE."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # detect_types is intentionally NOT enabled. Python 3.12 deprecated the
    # default TIMESTAMP converter (which couldn't parse `+00:00` offsets
    # anyway). Timestamps are stored and returned as ISO 8601 strings;
    # callers that need `datetime` objects parse on read.
    conn = sqlite3.connect(
        db_path,
        isolation_level=None,  # explicit transactions via BEGIN / COMMIT
    )
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
    finally:
        conn.close()


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


_COMMENT_RE = re.compile(r"^\s*--.*$", re.MULTILINE)


def _split_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements.

    Strips ``--`` line comments first, then splits on semicolons. Adequate for
    the schema CADENCE ships, which contains only DDL and no string literals
    that include semicolons or comment markers.
    """
    cleaned = _COMMENT_RE.sub("", sql)
    return [stmt.strip() for stmt in cleaned.split(";") if stmt.strip()]


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Apply any not-yet-applied migration files. Returns the list applied this run."""
    _ensure_migrations_table(conn)
    cur = conn.execute("SELECT name FROM schema_migrations")
    already = {row[0] for row in cur.fetchall()}
    applied: list[str] = []
    for name in list_migrations():
        if name in already:
            continue
        statements = _split_statements(_read_migration(name))
        conn.execute("BEGIN")
        try:
            for stmt in statements:
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations(name) VALUES (?)", (name,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(name)
    return applied


__all__ = ["DEFAULT_DB_PATH", "apply_migrations", "connect", "list_migrations"]
