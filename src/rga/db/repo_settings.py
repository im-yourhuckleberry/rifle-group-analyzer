"""Repository for stored settings: one JSON document per key.

Every function takes the connection first and uses parameterized SQL only. Writes do not commit;
the caller owns the transaction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from datetime import UTC, datetime


@dataclass(frozen=True)
class SettingRow:
    """Row of `settings`: the key, its JSON text and the UTC time it was last written."""

    key: str
    value: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> SettingRow:
        """Build the dataclass from a row containing every field as a column."""
        return cls(**{f.name: row[f.name] for f in fields(cls)})


def get_setting(conn: sqlite3.Connection, key: str) -> SettingRow | None:
    """The stored row for `key`, or None when it was never saved."""
    row = conn.execute(
        "SELECT key, value, updated_at FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return None if row is None else SettingRow.from_row(row)


def put_setting(
    conn: sqlite3.Connection, key: str, value_json: str, now: str | None = None
) -> SettingRow:
    """Insert or replace the row for `key` with `value_json` (valid JSON text) and return it."""
    stamp = now or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    row = conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
        " RETURNING key, value, updated_at",
        (key, value_json, stamp),
    ).fetchone()
    return SettingRow.from_row(row)
