"""DB test fixtures: a migrated SQLite database in a temporary directory."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from rga.db.connection import connect, migrate


@pytest.fixture
def migrated_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a connection to a freshly migrated database at `tmp_path/rga.sqlite3`; closed on exit."""
    conn = connect(tmp_path / "rga.sqlite3")
    migrate(conn)
    try:
        yield conn
    finally:
        conn.close()
