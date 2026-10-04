"""Repository for rifles, loads and load lots.

Every function takes the connection first, uses parameterized SQL only, and never commits; the
caller owns the transaction. Constraint violations (bad enum, diameter range, missing parent)
propagate as `sqlite3.IntegrityError`. Create and update functions accept an optional `now`
timestamp (and creates an optional `public_id`) so callers and tests control them.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from rga.db.rows import LoadRow, LotRow, RifleRow

RIFLE_UPDATABLE = ("name", "cartridge", "bullet_diameter_in", "scope", "notes")
LOAD_UPDATABLE = (
    "label",
    "maker",
    "product",
    "bullet_weight_gr",
    "bullet_type",
    "bullet",
    "primer",
    "brass",
    "coal_in",
    "trued_mv_fps",
    "notes",
)


class LoadNotInRifle(ValueError):
    """Raised when a zero load is set to a load that does not exist or belongs to another rifle."""


_INSERT_RIFLE = (
    "INSERT INTO rifles (public_id, name, cartridge, bullet_diameter_in, scope, notes,"
    " zero_load_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?) RETURNING *"
)
_SELECT_RIFLE = "SELECT * FROM rifles WHERE id = ?"
_SELECT_RIFLES = "SELECT * FROM rifles ORDER BY name COLLATE NOCASE, id"
_UPDATE_RIFLE = (
    "UPDATE rifles SET name = ?, cartridge = ?, bullet_diameter_in = ?, scope = ?, notes = ?,"
    " updated_at = ? WHERE id = ? RETURNING *"
)
_SET_ZERO_LOAD = (
    "UPDATE rifles SET zero_load_id = ?, updated_at = ? WHERE id = ?"
    " AND (? IS NULL OR EXISTS (SELECT 1 FROM loads WHERE id = ? AND rifle_id = ?))"
    " RETURNING *"
)
_INSERT_LOAD = (
    "INSERT INTO loads (public_id, rifle_id, kind, label, maker, product, bullet_weight_gr,"
    " bullet_type, bullet, primer, brass, coal_in, trued_mv_fps, notes, created_at,"
    " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *"
)
_SELECT_LOAD = "SELECT * FROM loads WHERE id = ?"
_SELECT_LOADS = "SELECT * FROM loads WHERE rifle_id = ? ORDER BY id"
_UPDATE_LOAD = (
    "UPDATE loads SET label = ?, maker = ?, product = ?, bullet_weight_gr = ?, bullet_type = ?,"
    " bullet = ?, primer = ?, brass = ?, coal_in = ?, trued_mv_fps = ?, notes = ?,"
    " updated_at = ? WHERE id = ? RETURNING *"
)
_INSERT_LOT = (
    "INSERT INTO load_lots (load_id, lot_code, created_at) VALUES (?, ?, ?)"
    " ON CONFLICT (load_id, lot_code) DO NOTHING"
)
_SELECT_LOT = "SELECT * FROM load_lots WHERE id = ?"
_SELECT_LOT_BY_CODE = "SELECT * FROM load_lots WHERE load_id = ? AND lot_code = ?"
_SELECT_LOTS = "SELECT * FROM load_lots WHERE load_id = ? ORDER BY id"


def now_iso() -> str:
    """Current UTC time as `YYYY-MM-DDTHH:MM:SS.ffffffZ`, the timestamp format stored by repos."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _returning(cursor: sqlite3.Cursor) -> sqlite3.Row | None:
    """Drain a RETURNING cursor so the statement finishes before the caller commits."""
    rows = cursor.fetchall()
    return rows[0] if rows else None


def _check_changes(changes: dict[str, Any], allowed: tuple[str, ...]) -> None:
    unknown = sorted(set(changes) - set(allowed))
    if unknown:
        raise ValueError(f"cannot update field(s): {', '.join(unknown)}")


def _merge(current: sqlite3.Row, changes: dict[str, Any], allowed: tuple[str, ...]) -> list[Any]:
    return [changes[name] if name in changes else current[name] for name in allowed]


def create_rifle(
    conn: sqlite3.Connection,
    *,
    name: str,
    cartridge: str,
    bullet_diameter_in: float,
    scope: str | None = None,
    notes: str = "",
    public_id: str | None = None,
    now: str | None = None,
) -> RifleRow:
    """Insert a rifle (fresh uuid4 `public_id` unless given) with no zero load; returns the row."""
    now = now or now_iso()
    row = _returning(
        conn.execute(
            _INSERT_RIFLE,
            (
                public_id or str(uuid.uuid4()),
                name,
                cartridge,
                bullet_diameter_in,
                scope,
                notes,
                now,
                now,
            ),
        )
    )
    return RifleRow.from_row(row)


def get_rifle(conn: sqlite3.Connection, rifle_id: int) -> RifleRow | None:
    """Return the rifle with `rifle_id`, or None."""
    row = conn.execute(_SELECT_RIFLE, (rifle_id,)).fetchone()
    return None if row is None else RifleRow.from_row(row)


def list_rifles(conn: sqlite3.Connection) -> list[RifleRow]:
    """Return all rifles ordered by name (case-insensitive), then id."""
    return [RifleRow.from_row(r) for r in conn.execute(_SELECT_RIFLES)]


def update_rifle(
    conn: sqlite3.Connection, rifle_id: int, *, now: str | None = None, **changes: Any
) -> RifleRow | None:
    """Update the given fields (`RIFLE_UPDATABLE`) and bump `updated_at`; None if no such rifle.

    No fields returns the rifle unchanged. Raises ValueError for a field outside `RIFLE_UPDATABLE`.
    """
    _check_changes(changes, RIFLE_UPDATABLE)
    current = conn.execute(_SELECT_RIFLE, (rifle_id,)).fetchone()
    if current is None:
        return None
    if not changes:
        return RifleRow.from_row(current)
    row = _returning(
        conn.execute(
            _UPDATE_RIFLE, (*_merge(current, changes, RIFLE_UPDATABLE), now or now_iso(), rifle_id)
        )
    )
    return RifleRow.from_row(row)


def set_zero_load(
    conn: sqlite3.Connection, rifle_id: int, load_id: int | None, *, now: str | None = None
) -> RifleRow | None:
    """Set (or clear with None) the rifle's zero load; returns the rifle, or None if it is missing.

    Raises `LoadNotInRifle` when `load_id` does not exist or belongs to a different rifle.
    """
    row = _returning(
        conn.execute(
            _SET_ZERO_LOAD, (load_id, now or now_iso(), rifle_id, load_id, load_id, rifle_id)
        )
    )
    if row is not None:
        return RifleRow.from_row(row)
    if get_rifle(conn, rifle_id) is None:
        return None
    raise LoadNotInRifle(f"load {load_id} is not a load of rifle {rifle_id}")


def create_load(
    conn: sqlite3.Connection,
    rifle_id: int,
    *,
    kind: str,
    label: str,
    maker: str | None = None,
    product: str | None = None,
    bullet_weight_gr: float | None = None,
    bullet_type: str | None = None,
    bullet: str | None = None,
    primer: str | None = None,
    brass: str | None = None,
    coal_in: float | None = None,
    trued_mv_fps: float | None = None,
    notes: str = "",
    public_id: str | None = None,
    now: str | None = None,
) -> LoadRow:
    """Insert a load under `rifle_id`: factory fields, or handload fields (bullet weight and line,
    primer, brass, COAL), plus an optional trued MV.

    Raises `sqlite3.IntegrityError` for an unknown rifle, a kind other than factory/handload, a
    non-positive trued MV, or a COAL outside 1 to 5 in.
    """
    now = now or now_iso()
    row = _returning(
        conn.execute(
            _INSERT_LOAD,
            (
                public_id or str(uuid.uuid4()),
                rifle_id,
                kind,
                label,
                maker,
                product,
                bullet_weight_gr,
                bullet_type,
                bullet,
                primer,
                brass,
                coal_in,
                trued_mv_fps,
                notes,
                now,
                now,
            ),
        )
    )
    return LoadRow.from_row(row)


def get_load(conn: sqlite3.Connection, load_id: int) -> LoadRow | None:
    """Return the load with `load_id`, or None."""
    row = conn.execute(_SELECT_LOAD, (load_id,)).fetchone()
    return None if row is None else LoadRow.from_row(row)


def list_loads(conn: sqlite3.Connection, rifle_id: int) -> list[LoadRow]:
    """Return the rifle's loads in creation order (uses `ix_loads_rifle`)."""
    return [LoadRow.from_row(r) for r in conn.execute(_SELECT_LOADS, (rifle_id,))]


def update_load(
    conn: sqlite3.Connection, load_id: int, *, now: str | None = None, **changes: Any
) -> LoadRow | None:
    """Update the given fields (`LOAD_UPDATABLE`) and bump `updated_at`; None if no such load.

    `kind` and `rifle_id` are immutable. No fields returns the load unchanged. Raises ValueError
    for any field outside `LOAD_UPDATABLE`.
    """
    _check_changes(changes, LOAD_UPDATABLE)
    current = conn.execute(_SELECT_LOAD, (load_id,)).fetchone()
    if current is None:
        return None
    if not changes:
        return LoadRow.from_row(current)
    row = _returning(
        conn.execute(
            _UPDATE_LOAD, (*_merge(current, changes, LOAD_UPDATABLE), now or now_iso(), load_id)
        )
    )
    return LoadRow.from_row(row)


def get_or_create_lot(
    conn: sqlite3.Connection, load_id: int, lot_code: str, *, now: str | None = None
) -> LotRow:
    """Return the lot `lot_code` of `load_id`, creating it if absent; repeated calls return the
    same row. Raises ValueError for a blank code and `sqlite3.IntegrityError` for an unknown load.
    """
    if not lot_code.strip():
        raise ValueError("lot_code must not be blank")
    conn.execute(_INSERT_LOT, (load_id, lot_code, now or now_iso()))
    return LotRow.from_row(conn.execute(_SELECT_LOT_BY_CODE, (load_id, lot_code)).fetchone())


def get_lot(conn: sqlite3.Connection, lot_id: int) -> LotRow | None:
    """Return the lot with `lot_id`, or None."""
    row = conn.execute(_SELECT_LOT, (lot_id,)).fetchone()
    return None if row is None else LotRow.from_row(row)


def list_lots(conn: sqlite3.Connection, load_id: int) -> list[LotRow]:
    """Return the load's lots in creation order."""
    return [LotRow.from_row(r) for r in conn.execute(_SELECT_LOTS, (load_id,))]
