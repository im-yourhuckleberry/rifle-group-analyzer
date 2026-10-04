"""Repository for saved aggregates: a named set of finalized groups of one load.

Every function takes the connection first, uses parameterized SQL only, and never commits; the
caller owns the transaction. A missing rifle, load or group propagates as
`sqlite3.IntegrityError`. Deleting a group, load or rifle deletes the membership rows (and a load
or rifle also deletes its aggregates) through foreign-key cascades.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime

_INSERT = (
    "INSERT INTO aggregates (public_id, name, rifle_id, load_id, distance_yd, group_count,"
    " created_at) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING *"
)


@dataclass(frozen=True)
class AggregateRow:
    """Row of `aggregates`."""

    id: int
    public_id: str
    name: str
    rifle_id: int
    load_id: int
    distance_yd: int
    group_count: int
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> AggregateRow:
        """Build the dataclass from a row containing every field as a column."""
        return cls(**{f.name: row[f.name] for f in fields(cls)})


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def create_aggregate(
    conn: sqlite3.Connection,
    *,
    name: str,
    rifle_id: int,
    load_id: int,
    distance_yd: int,
    group_ids: Sequence[int],
    public_id: str | None = None,
    now: str | None = None,
) -> AggregateRow:
    """Insert an aggregate and its member rows; `group_count` is the number of `group_ids`."""
    cursor = conn.execute(
        _INSERT,
        (
            public_id or str(uuid.uuid4()),
            name,
            rifle_id,
            load_id,
            distance_yd,
            len(group_ids),
            now or _now(),
        ),
    )
    row = AggregateRow.from_row(cursor.fetchall()[0])
    conn.executemany(
        "INSERT INTO aggregate_groups (aggregate_id, group_id) VALUES (?, ?)",
        [(row.id, group_id) for group_id in group_ids],
    )
    return row


def get_aggregate(conn: sqlite3.Connection, aggregate_id: int) -> AggregateRow | None:
    """The aggregate with `aggregate_id`, or None."""
    row = conn.execute("SELECT * FROM aggregates WHERE id = ?", (aggregate_id,)).fetchone()
    return None if row is None else AggregateRow.from_row(row)


def list_aggregates(conn: sqlite3.Connection, load_id: int | None = None) -> list[AggregateRow]:
    """Aggregates in creation order, optionally only those of one load."""
    if load_id is None:
        rows = conn.execute("SELECT * FROM aggregates ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM aggregates WHERE load_id = ? ORDER BY id", (load_id,)
        ).fetchall()
    return [AggregateRow.from_row(r) for r in rows]


def member_group_ids(conn: sqlite3.Connection, aggregate_id: int) -> list[int]:
    """Ids of the groups still in the aggregate, ascending."""
    rows = conn.execute(
        "SELECT group_id FROM aggregate_groups WHERE aggregate_id = ? ORDER BY group_id",
        (aggregate_id,),
    )
    return [int(r["group_id"]) for r in rows]


def delete_aggregate(conn: sqlite3.Connection, aggregate_id: int) -> bool:
    """Delete the aggregate and its member rows; True when it existed."""
    return conn.execute("DELETE FROM aggregates WHERE id = ?", (aggregate_id,)).rowcount > 0
