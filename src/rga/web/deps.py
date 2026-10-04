"""FastAPI dependencies and the web layer's only gateway to `rga.db.connection`."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import Request

from rga.config import Settings
from rga.db.connection import connect, migrate, schema_version


def get_settings(request: Request) -> Settings:
    """Return the `Settings` the app was created with."""
    return request.app.state.settings


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """Yield one connection per request, closed afterwards. Does not commit; services own transactions."""
    conn = connect(get_settings(request).db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_database(settings: Settings) -> None:
    """Create the data directories (mode 0700, tightened if they already exist) and migrate."""
    for directory in (settings.data_dir, settings.photos_dir, settings.working_dir):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    conn = connect(settings.db_path)
    try:
        migrate(conn)
    finally:
        conn.close()


def current_schema_version(conn: sqlite3.Connection) -> int:
    """Return the database's schema version."""
    return schema_version(conn)
