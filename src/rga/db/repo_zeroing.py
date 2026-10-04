"""Repository for zeros, their linked groups, and zero checks.

Every function takes the connection first, uses parameterized SQL only, and never commits; the
caller owns the transaction. Constraint violations (state/mode/result enums, positive dot size,
one check per group, missing parents) propagate as `sqlite3.IntegrityError`. Create and update
functions accept an optional `now` timestamp so callers and tests control it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from rga.db.repo_rifles import now_iso
from rga.db.rows import ZeroCheckRow, ZeroRow

ZERO_UPDATABLE = (
    "state",
    "dialed_up_mil",
    "dialed_right_mil",
    "zero_es_in",
    "zero_shot_count",
    "dot_size_in",
    "completed_at",
)


class ZeroTargetMismatch(ValueError):
    """Raised when a zero's load is not a load of its rifle, or its lot is not a lot of its load."""


class GroupNotLinkable(ValueError):
    """Raised when a group cannot be linked: zero or group missing, or a different load or lot."""


@dataclass(frozen=True)
class LinkedGroup:
    """A group linked to a zero: its purpose, status and stored shot count."""

    group_id: int
    purpose: str
    status: str
    shot_count: int


@dataclass(frozen=True)
class ZeroListing:
    """A zero with the label of its load and its lot code (None when the zero has no lot)."""

    zero: ZeroRow
    load_label: str
    lot_code: str | None


_LIST_ZEROS_FOR_RIFLE = (
    "SELECT zeros.*, loads.label AS load_label, load_lots.lot_code AS lot_code FROM zeros"
    " JOIN loads ON loads.id = zeros.load_id"
    " LEFT JOIN load_lots ON load_lots.id = zeros.lot_id"
    " WHERE zeros.rifle_id = ? ORDER BY zeros.id DESC"
)
_INSERT_ZERO = (
    "INSERT INTO zeros (rifle_id, load_id, lot_id, mode, state, created_at, updated_at)"
    " SELECT ?, loads.id, ?, ?, ?, ?, ? FROM loads WHERE loads.id = ? AND loads.rifle_id = ?"
    " AND (? IS NULL OR EXISTS (SELECT 1 FROM load_lots WHERE load_lots.id = ?"
    " AND load_lots.load_id = loads.id)) RETURNING *"
)
_SELECT_ZERO = "SELECT * FROM zeros WHERE id = ?"
_ACTIVE_ZERO = (
    "SELECT * FROM zeros WHERE rifle_id = ? AND load_id = ? AND state != 'superseded'"
    " ORDER BY id DESC LIMIT 1"
)
_UPDATE_ZERO = (
    "UPDATE zeros SET state = ?, dialed_up_mil = ?, dialed_right_mil = ?, zero_es_in = ?,"
    " zero_shot_count = ?, dot_size_in = ?, completed_at = ?, updated_at = ?"
    " WHERE id = ? RETURNING *"
)
_SUPERSEDE_ZEROS = (
    "UPDATE zeros SET state = 'superseded', updated_at = ? WHERE rifle_id = ? AND load_id = ?"
    " AND id != ? AND state != 'superseded'"
)
_LINK_GROUP = (
    "UPDATE groups SET zero_id = ?, updated_at = ? WHERE id = ? AND EXISTS ("
    "SELECT 1 FROM zeros WHERE zeros.id = ? AND groups.load_id = zeros.load_id"
    " AND (zeros.lot_id IS NULL OR zeros.lot_id = groups.lot_id))"
)
_GROUP_EXISTS = "SELECT 1 FROM groups WHERE id = ?"
_LIST_LINKED_GROUPS = (
    "SELECT groups.id AS group_id, groups.purpose AS purpose, groups.status AS status,"
    " (SELECT COUNT(*) FROM shots WHERE shots.group_id = groups.id) AS shot_count"
    " FROM groups WHERE groups.zero_id = ? ORDER BY groups.id"
)
_INSERT_ZERO_CHECK = (
    "INSERT INTO zero_checks (zero_id, group_id, result, created_at)"
    " SELECT ?, groups.id, ?, ? FROM groups"
    " WHERE groups.id = ? AND groups.zero_id = ? AND groups.purpose = 'zero_check' RETURNING *"
)
_LIST_ZERO_CHECKS = "SELECT * FROM zero_checks WHERE zero_id = ? ORDER BY id"


def _returning(cursor: sqlite3.Cursor) -> sqlite3.Row | None:
    """Drain a RETURNING cursor so the statement finishes before the caller commits."""
    rows = cursor.fetchall()
    return rows[0] if rows else None


def create_zero(
    conn: sqlite3.Connection,
    *,
    rifle_id: int,
    load_id: int,
    mode: str,
    state: str,
    lot_id: int | None = None,
    now: str | None = None,
) -> ZeroRow:
    """Insert a zero for `rifle_id` and `load_id` in the given `mode` and starting `state`.

    Raises `ZeroTargetMismatch` when the rifle or load is missing, the load is not a load of the
    rifle, or the lot is missing or not a lot of the load, and `sqlite3.IntegrityError` for a bad
    mode or state.
    """
    now = now or now_iso()
    row = _returning(
        conn.execute(
            _INSERT_ZERO,
            (rifle_id, lot_id, mode, state, now, now, load_id, rifle_id, lot_id, lot_id),
        )
    )
    if row is None:
        raise ZeroTargetMismatch(
            f"load {load_id} is not a load of rifle {rifle_id}, or lot {lot_id} is not a lot of it"
        )
    return ZeroRow.from_row(row)


def get_zero(conn: sqlite3.Connection, zero_id: int) -> ZeroRow | None:
    """Return the zero with `zero_id`, or None."""
    row = conn.execute(_SELECT_ZERO, (zero_id,)).fetchone()
    return None if row is None else ZeroRow.from_row(row)


def list_zeros_for_rifle(conn: sqlite3.Connection, rifle_id: int) -> list[ZeroListing]:
    """Every zero of the rifle, newest first, with its load label and lot code."""
    return [
        ZeroListing(ZeroRow.from_row(row), row["load_label"], row["lot_code"])
        for row in conn.execute(_LIST_ZEROS_FOR_RIFLE, (rifle_id,))
    ]


def active_zero(conn: sqlite3.Connection, rifle_id: int, load_id: int) -> ZeroRow | None:
    """Return the newest non-superseded zero of the rifle and load, or None (uses
    `ix_zeros_rifle_load`).
    """
    row = conn.execute(_ACTIVE_ZERO, (rifle_id, load_id)).fetchone()
    return None if row is None else ZeroRow.from_row(row)


def update_zero(
    conn: sqlite3.Connection, zero_id: int, *, now: str | None = None, **changes: Any
) -> ZeroRow | None:
    """Update the given fields (`ZERO_UPDATABLE`: state, dialed correction, zero ES and shot count,
    dot size, completion time) and bump `updated_at`; None if no such zero.

    No fields returns the zero unchanged; None clears a nullable field. `rifle_id`, `load_id`,
    `lot_id` and `mode` are fixed at creation. Raises ValueError for any field outside
    `ZERO_UPDATABLE` and `sqlite3.IntegrityError` for a bad state or a non-positive dot size.
    """
    unknown = sorted(set(changes) - set(ZERO_UPDATABLE))
    if unknown:
        raise ValueError(f"cannot update field(s): {', '.join(unknown)}")
    current = conn.execute(_SELECT_ZERO, (zero_id,)).fetchone()
    if current is None:
        return None
    if not changes:
        return ZeroRow.from_row(current)
    values = [changes[n] if n in changes else current[n] for n in ZERO_UPDATABLE]
    row = _returning(conn.execute(_UPDATE_ZERO, (*values, now or now_iso(), zero_id)))
    return ZeroRow.from_row(row)


def supersede_zeros(
    conn: sqlite3.Connection,
    rifle_id: int,
    load_id: int,
    except_id: int,
    *,
    now: str | None = None,
) -> int:
    """Mark every other non-superseded zero of the rifle and load as superseded; returns how many.

    After `except_id` completes, it is the only active zero of that rifle and load.
    """
    cursor = conn.execute(_SUPERSEDE_ZEROS, (now or now_iso(), rifle_id, load_id, except_id))
    return cursor.rowcount


def link_group(
    conn: sqlite3.Connection, zero_id: int, group_id: int, *, now: str | None = None
) -> None:
    """Attach a group to a zero (`groups.zero_id`); relinking moves the group.

    The group must be of the zero's load (so of its rifle), and of its lot when the zero has one.
    Raises `GroupNotLinkable` otherwise, or when either row is missing.
    """
    cursor = conn.execute(_LINK_GROUP, (zero_id, now or now_iso(), group_id, zero_id))
    if cursor.rowcount == 1:
        return
    if conn.execute(_SELECT_ZERO, (zero_id,)).fetchone() is None:
        raise GroupNotLinkable(f"zero {zero_id} does not exist")
    if conn.execute(_GROUP_EXISTS, (group_id,)).fetchone() is None:
        raise GroupNotLinkable(f"group {group_id} does not exist")
    raise GroupNotLinkable(f"group {group_id} does not match the load or lot of zero {zero_id}")


def list_linked_groups(conn: sqlite3.Connection, zero_id: int) -> list[LinkedGroup]:
    """Return the groups linked to the zero, by group id, with purpose, status and shot count."""
    return [LinkedGroup(**dict(r)) for r in conn.execute(_LIST_LINKED_GROUPS, (zero_id,))]


def create_zero_check(
    conn: sqlite3.Connection,
    *,
    zero_id: int,
    group_id: int,
    result: str,
    now: str | None = None,
) -> ZeroCheckRow:
    """Record the outcome (`pass` or `possible_lost_zero`) of a zero-check group.

    The group must be linked to the zero and have purpose `zero_check`; otherwise
    `GroupNotLinkable` is raised and nothing is stored. Raises `sqlite3.IntegrityError` when the
    group already has a check or the result is not allowed.
    """
    row = _returning(
        conn.execute(_INSERT_ZERO_CHECK, (zero_id, result, now or now_iso(), group_id, zero_id))
    )
    if row is None:
        raise GroupNotLinkable(
            f"group {group_id} is not a zero_check group linked to zero {zero_id}"
        )
    return ZeroCheckRow.from_row(row)


def list_zero_checks(conn: sqlite3.Connection, zero_id: int) -> list[ZeroCheckRow]:
    """Return the zero's checks in creation order."""
    return [ZeroCheckRow.from_row(r) for r in conn.execute(_LIST_ZERO_CHECKS, (zero_id,))]
