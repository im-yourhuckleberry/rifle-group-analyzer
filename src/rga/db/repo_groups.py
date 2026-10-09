"""Repository for photos, suppressors, groups, shots, detection rejections and note search.

A group carries its own context (date, distance, position, suppressor, weather). Every function takes the connection first and never commits; callers own the
transaction. SQL is parameterized throughout (dynamic statements are assembled only from fixed
column fragments). Timestamps are UTC ISO-8601 with microseconds; create/update functions accept
`now` for tests.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from rga.db.rows import GroupRow, PhotoRow, RejectionRow, ShotRow, SuppressorRow
from rga.domain.types import RejectionReason, Shot, ShotOrigin, ShotTimeSource

__all__ = [
    "GroupLinkMismatch",
    "NewShot",
    "add_rejection",
    "delete_groups",
    "delete_unused_photos",
    "reference_kind_counts",
    "create_group",
    "create_photo",
    "create_suppressor",
    "first_group_of_lot",
    "get_group",
    "get_photo",
    "get_photo_by_sha",
    "get_suppressor",
    "latest_group",
    "list_groups",
    "list_rejections",
    "list_shots",
    "list_suppressors",
    "load_has_other_lot_groups",
    "replace_shots",
    "search_notes",
    "set_photo_taken_at",
    "set_shot_offsets",
    "update_group",
    "update_photo_reference",
]

SNIPPET_RADIUS = 40
DEFAULT_SEARCH_LIMIT = 50

_GROUP_FIELDS = frozenset(
    {
        "load_id",
        "lot_id",
        "photo_id",
        "roi_json",
        "shots_expected",
        "elevation_dialed_mil",
        "aim_x_in",
        "aim_y_in",
        "purpose",
        "zero_id",
        "status",
        "notes",
        "finalized_at",
        "shot_date",
        "shot_time",
        "utc_offset",
        "shot_at_source",
        "distance_yd",
        "position",
        "suppressor",
        "suppressor_id",
        "temperature_f",
        "density_altitude_ft",
        "wind_mph",
        "wind_clock",
        "weather_skipped",
    }
)
_GROUP_LINK_COLUMNS = ("load_id", "lot_id", "zero_id")
_BOOLEAN_FIELDS = frozenset({"suppressor", "weather_skipped"})
_STRING_FIELDS = frozenset({"position", "shot_at_source", "purpose", "status"})
_PHOTO_SELECT = (
    "SELECT id, sha256, ext, width_px, height_px, reference_kind, reference_json, "
    "h_px_to_plane_json, working_ppi, working_w, working_h, axis_ratio, reference_confirmed, "
    "created_at, taken_at, taken_at_offset FROM photos"
)
_GROUP_SELECT = (
    "SELECT g.id, g.public_id, g.load_id, g.lot_id, g.photo_id, g.roi_json, g.shots_expected, "
    "g.elevation_dialed_mil, g.aim_x_in, g.aim_y_in, g.purpose, g.zero_id, g.status, g.notes, "
    "g.finalized_at, g.created_at, g.updated_at, g.shot_date, g.shot_time, g.utc_offset, "
    "g.shot_at_source, g.distance_yd, g.position, g.suppressor, g.suppressor_id, "
    "g.temperature_f, g.density_altitude_ft, g.wind_mph, g.wind_clock, g.weather_skipped, "
    "g.legacy_session_id, g.legacy_session_notes FROM groups g"
)
_SHOT_SELECT = (
    "SELECT id, group_id, seq, x_in, y_in, dx_in, dy_in, dx_mil, dy_mil, origin, moved_by_user, "
    "confidence FROM shots"
)
_UPDATE_GROUP = "UPDATE groups SET "


class GroupLinkMismatch(ValueError):
    """Raised when a group's lot or zero disagree on load, rifle or lot."""


def _inserted(row):
    if row is None:
        raise RuntimeError("inserted row could not be read back")
    return row


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sql_value(name: str, value: object) -> object:
    if name in _BOOLEAN_FIELDS and value is not None:
        return int(bool(value))
    if name in _STRING_FIELDS and value is not None:
        return str(value)
    return value


def _update_clause(fields: dict[str, object], allowed: frozenset[str], kind: str):
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise ValueError(f"unknown {kind} field(s): {', '.join(unknown)}")
    columns = sorted(fields)
    assignments = ", ".join(f"{name} = ?" for name in columns)
    return assignments, [_sql_value(name, fields[name]) for name in columns]


def _check_group_links(
    conn: sqlite3.Connection, load_id: int, lot_id: int, zero_id: int | None
) -> None:
    """Raise GroupLinkMismatch unless the lot is a lot of the load and any zero is of the same
    rifle and load and (when it has one) lot. Rows that do not exist are left to the foreign keys
    (IntegrityError)."""
    load = conn.execute("SELECT rifle_id FROM loads WHERE id = ?", (load_id,)).fetchone()
    if load is None:
        return
    lot = conn.execute("SELECT load_id FROM load_lots WHERE id = ?", (lot_id,)).fetchone()
    if lot is not None and lot["load_id"] != load_id:
        raise GroupLinkMismatch(f"lot {lot_id} is not a lot of load {load_id}")
    if zero_id is None:
        return
    zero = conn.execute(
        "SELECT rifle_id, load_id, lot_id FROM zeros WHERE id = ?", (zero_id,)
    ).fetchone()
    if zero is None:
        return
    if zero["rifle_id"] != load["rifle_id"]:
        raise GroupLinkMismatch(f"zero {zero_id} belongs to another rifle than load {load_id}")
    if zero["load_id"] != load_id:
        raise GroupLinkMismatch(f"zero {zero_id} belongs to another load than load {load_id}")
    if zero["lot_id"] is not None and zero["lot_id"] != lot_id:
        raise GroupLinkMismatch(f"zero {zero_id} is tied to another lot than lot {lot_id}")


# ---------------------------------------------------------------------------------------------
# Suppressors
# ---------------------------------------------------------------------------------------------


def create_suppressor(conn: sqlite3.Connection, name: str, now: str | None = None) -> SuppressorRow:
    """Insert a suppressor; a name that exists (any letter case) raises `sqlite3.IntegrityError`."""
    cursor = conn.execute(
        "INSERT INTO suppressors (name, created_at) VALUES (?, ?)", (name, now or _now())
    )
    return _inserted(get_suppressor(conn, int(cursor.lastrowid)))


def get_suppressor(conn: sqlite3.Connection, suppressor_id: int) -> SuppressorRow | None:
    """Return the suppressor with this id, or None."""
    row = conn.execute(
        "SELECT id, name, created_at FROM suppressors WHERE id = ?", (suppressor_id,)
    ).fetchone()
    return SuppressorRow.from_row(row) if row else None


def list_suppressors(conn: sqlite3.Connection) -> list[SuppressorRow]:
    """All suppressors ordered by name (case-insensitive), then id."""
    rows = conn.execute(
        "SELECT id, name, created_at FROM suppressors ORDER BY name COLLATE NOCASE, id"
    ).fetchall()
    return [SuppressorRow.from_row(r) for r in rows]


# ---------------------------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------------------------


def create_photo(
    conn: sqlite3.Connection,
    *,
    sha256: str,
    ext: str,
    width_px: int,
    height_px: int,
    taken_at: str | None = None,
    taken_at_offset: str | None = None,
    now: str | None = None,
) -> PhotoRow:
    """Insert a photo keyed by its sha256 (UNIQUE) and return it; a duplicate raises IntegrityError.

    `taken_at` is the local `YYYY-MM-DDTHH:MM:SS` capture time read from the file and
    `taken_at_offset` its `+HH:MM` offset; both may be None.
    """
    cursor = conn.execute(
        "INSERT INTO photos (sha256, ext, width_px, height_px, taken_at, taken_at_offset, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sha256, ext, width_px, height_px, taken_at, taken_at_offset, now or _now()),
    )
    return _inserted(get_photo(conn, int(cursor.lastrowid)))


def set_photo_taken_at(
    conn: sqlite3.Connection, photo_id: int, taken_at: str, taken_at_offset: str | None
) -> PhotoRow | None:
    """Store the capture time on a photo that has none yet; an existing value is never replaced.

    Returns the photo, or None if absent.
    """
    conn.execute(
        "UPDATE photos SET taken_at = ?, taken_at_offset = ? WHERE id = ? AND taken_at IS NULL",
        (taken_at, taken_at_offset, photo_id),
    )
    return get_photo(conn, photo_id)


def get_photo(conn: sqlite3.Connection, photo_id: int) -> PhotoRow | None:
    """Return the photo with this id, or None."""
    row = conn.execute(_PHOTO_SELECT + " WHERE id = ?", (photo_id,)).fetchone()
    return PhotoRow.from_row(row) if row else None


def get_photo_by_sha(conn: sqlite3.Connection, sha256: str) -> PhotoRow | None:
    """Return the photo with this content hash, or None."""
    row = conn.execute(_PHOTO_SELECT + " WHERE sha256 = ?", (sha256,)).fetchone()
    return PhotoRow.from_row(row) if row else None


def update_photo_reference(
    conn: sqlite3.Connection,
    photo_id: int,
    *,
    reference_kind: str | None,
    reference_json: str | None,
    h_px_to_plane_json: str | None,
    working_ppi: float | None,
    working_w: int | None,
    working_h: int | None,
    axis_ratio: float | None,
    reference_confirmed: bool,
) -> PhotoRow | None:
    """Replace the photo's reference and working-image fields; return the row, or None if absent.

    A confirmed reference needs the transform and working ppi (the schema CHECK raises
    IntegrityError otherwise). Passing Nones with `reference_confirmed=False` clears it.
    """
    conn.execute(
        "UPDATE photos SET reference_kind = ?, reference_json = ?, h_px_to_plane_json = ?, "
        "working_ppi = ?, working_w = ?, working_h = ?, axis_ratio = ?, reference_confirmed = ? "
        "WHERE id = ?",
        (
            reference_kind,
            reference_json,
            h_px_to_plane_json,
            working_ppi,
            working_w,
            working_h,
            axis_ratio,
            int(bool(reference_confirmed)),
            photo_id,
        ),
    )
    return get_photo(conn, photo_id)


# ---------------------------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------------------------


def create_group(
    conn: sqlite3.Connection,
    *,
    load_id: int,
    lot_id: int,
    shots_expected: int,
    shot_date: str,
    distance_yd: int,
    position: str,
    suppressor: bool,
    shot_time: str | None = None,
    utc_offset: str | None = None,
    shot_at_source: ShotTimeSource | str = ShotTimeSource.MANUAL,
    suppressor_id: int | None = None,
    temperature_f: float | None = None,
    density_altitude_ft: float | None = None,
    wind_mph: float | None = None,
    wind_clock: int | None = None,
    weather_skipped: bool = False,
    photo_id: int | None = None,
    roi_json: str | None = None,
    elevation_dialed_mil: float | None = None,
    aim_x_in: float | None = None,
    aim_y_in: float | None = None,
    purpose: str = "general",
    zero_id: int | None = None,
    notes: str = "",
    legacy_session_id: int | None = None,
    legacy_session_notes: str = "",
    public_id: str | None = None,
    now: str | None = None,
) -> GroupRow:
    """Insert a draft group with its own context and return it. Several groups may share a photo.

    `shot_date` is the local `YYYY-MM-DD` day, `shot_time` an optional local `HH:MM:SS`, and
    `utc_offset` an optional `+HH:MM` (information only). Raises `GroupLinkMismatch` when the lot
    is not a lot of the load or `zero_id` is a zero of another rifle, load or lot, and
    `sqlite3.IntegrityError` for any value the schema refuses (bad date, source, position,
    weather clock, a suppressor id without the suppressor flag, an unknown suppressor id).
    """
    _check_group_links(conn, load_id, lot_id, zero_id)
    stamp = now or _now()
    cursor = conn.execute(
        "INSERT INTO groups (public_id, load_id, lot_id, photo_id, roi_json, shots_expected, "
        "elevation_dialed_mil, aim_x_in, aim_y_in, purpose, zero_id, status, notes, created_at, "
        "updated_at, shot_date, shot_time, utc_offset, shot_at_source, distance_yd, position, "
        "suppressor, suppressor_id, temperature_f, density_altitude_ft, wind_mph, wind_clock, "
        "weather_skipped, legacy_session_id, legacy_session_notes) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?)",
        (
            public_id or str(uuid.uuid4()),
            load_id,
            lot_id,
            photo_id,
            roi_json,
            shots_expected,
            elevation_dialed_mil,
            aim_x_in,
            aim_y_in,
            str(purpose),
            zero_id,
            notes,
            stamp,
            stamp,
            shot_date,
            shot_time,
            utc_offset,
            str(shot_at_source),
            distance_yd,
            str(position),
            int(bool(suppressor)),
            suppressor_id,
            temperature_f,
            density_altitude_ft,
            wind_mph,
            wind_clock,
            int(bool(weather_skipped)),
            legacy_session_id,
            legacy_session_notes,
        ),
    )
    return _inserted(get_group(conn, int(cursor.lastrowid)))


def delete_groups(conn: sqlite3.Connection, group_ids: Sequence[int]) -> int:
    """Delete these groups (their shots, rejections and zero checks go with them by ON DELETE
    CASCADE) and every saved aggregate that included one of them, since it no longer is the
    combination that was saved (its stored group count would be wrong); returns how many groups
    were deleted."""
    marks = ",".join("?" * len(group_ids))
    params = tuple(group_ids)
    conn.execute(
        "DELETE FROM aggregates WHERE id IN "
        f"(SELECT aggregate_id FROM aggregate_groups WHERE group_id IN ({marks}))",
        params,
    )
    return conn.execute(f"DELETE FROM groups WHERE id IN ({marks})", params).rowcount


def reference_kind_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many groups used each kind of confirmed scale reference (via their photo)."""
    rows = conn.execute(
        "SELECT p.reference_kind, COUNT(g.id) FROM groups g JOIN photos p ON p.id = g.photo_id "
        "WHERE p.reference_confirmed = 1 AND p.reference_kind IS NOT NULL GROUP BY p.reference_kind"
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def delete_unused_photos(conn: sqlite3.Connection, photo_ids: Sequence[int]) -> list[PhotoRow]:
    """Delete those of these photos that no group uses any more; returns the deleted rows (so the
    caller can remove their files)."""
    if not photo_ids:
        return []
    marks = ",".join("?" * len(photo_ids))
    rows = conn.execute(
        f"SELECT * FROM photos WHERE id IN ({marks}) "
        "AND NOT EXISTS (SELECT 1 FROM groups g WHERE g.photo_id = photos.id)",
        tuple(photo_ids),
    ).fetchall()
    unused = [PhotoRow.from_row(row) for row in rows]
    if unused:
        gone = ",".join("?" * len(unused))
        conn.execute(f"DELETE FROM photos WHERE id IN ({gone})", tuple(p.id for p in unused))
    return unused


def get_group(conn: sqlite3.Connection, group_id: int) -> GroupRow | None:
    """Return the group with this id, or None."""
    row = conn.execute(_GROUP_SELECT + " WHERE g.id = ?", (group_id,)).fetchone()
    return GroupRow.from_row(row) if row else None


def _group_filter(
    *,
    load_id: int | None,
    rifle_id: int | None,
    status: str | None,
    distance_yd: int | None,
    position: str | None,
    suppressor: bool | None,
    suppressor_id: int | None,
    purpose: str | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, list[object]]:
    """The `WHERE` clause (with a leading space, or empty) and its bound parameters."""
    clauses: list[str] = []
    params: list[object] = []
    if rifle_id is not None:
        clauses.append("g.load_id IN (SELECT id FROM loads WHERE rifle_id = ?)")
        params.append(rifle_id)
    for column, value in (
        ("g.load_id = ?", load_id),
        ("g.status = ?", None if status is None else str(status)),
        ("g.distance_yd = ?", distance_yd),
        ("g.position = ?", None if position is None else str(position)),
        ("g.suppressor = ?", None if suppressor is None else int(suppressor)),
        ("g.suppressor_id = ?", suppressor_id),
        ("g.purpose = ?", None if purpose is None else str(purpose)),
        ("g.shot_date >= ?", date_from),
        ("g.shot_date <= ?", date_to),
    ):
        if value is not None:
            clauses.append(column)
            params.append(value)
    return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), params


def list_groups(
    conn: sqlite3.Connection,
    load_id: int | None = None,
    rifle_id: int | None = None,
    status: str | None = None,
    *,
    distance_yd: int | None = None,
    position: str | None = None,
    suppressor: bool | None = None,
    suppressor_id: int | None = None,
    purpose: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    newest_first: bool = False,
) -> list[GroupRow]:
    """Groups filtered by any combination of the arguments (all optional, combined with AND).

    `rifle_id` matches through the group's load; `date_from` and `date_to` are inclusive
    `YYYY-MM-DD` bounds on `shot_date`; `suppressor` is the on/off flag and `suppressor_id` names
    the suppressor. Order is creation order, or with `newest_first` `(shot_date, shot_time, id)`
    descending. No filter returns every group.
    """
    where, params = _group_filter(
        load_id=load_id,
        rifle_id=rifle_id,
        status=status,
        distance_yd=distance_yd,
        position=position,
        suppressor=suppressor,
        suppressor_id=suppressor_id,
        purpose=purpose,
        date_from=date_from,
        date_to=date_to,
    )
    order = (
        " ORDER BY g.shot_date DESC, coalesce(g.shot_time, '') DESC, g.id DESC"
        if newest_first
        else " ORDER BY g.id"
    )
    rows = conn.execute(_GROUP_SELECT + where + order, params).fetchall()
    return [GroupRow.from_row(r) for r in rows]


def latest_group(conn: sqlite3.Connection, rifle_id: int | None = None) -> GroupRow | None:
    """The highest-id group of the rifle (through its load), or of all rifles when `rifle_id` is
    None; None when there is none. This is the group a new group's defaults are copied from."""
    if rifle_id is None:
        row = conn.execute(_GROUP_SELECT + " ORDER BY g.id DESC LIMIT 1").fetchone()
    else:
        row = conn.execute(
            _GROUP_SELECT
            + " JOIN loads l ON l.id = g.load_id WHERE l.rifle_id = ? ORDER BY g.id DESC LIMIT 1",
            (rifle_id,),
        ).fetchone()
    return GroupRow.from_row(row) if row else None


def update_group(
    conn: sqlite3.Connection, group_id: int, *, now: str | None = None, **fields: object
) -> GroupRow | None:
    """Update the named group columns and bump `updated_at`; return the row, or None if absent.

    Raises ValueError for a column that is not updatable (`legacy_*` and the ids are not). With
    no fields the row is returned as is. A final group without an aim point, or a suppressor id
    without the suppressor flag, raises IntegrityError (schema CHECK). Changing `load_id`,
    `lot_id` or `zero_id` raises `GroupLinkMismatch` unless the resulting group still passes the
    `create_group` consistency rules.
    """
    if fields:
        assignments, values = _update_clause(fields, _GROUP_FIELDS, "group")
        if fields.keys() & set(_GROUP_LINK_COLUMNS):
            current = conn.execute(
                "SELECT load_id, lot_id, zero_id FROM groups WHERE id = ?", (group_id,)
            ).fetchone()
            if current is not None:
                links = dict(current)
                links.update({name: fields[name] for name in _GROUP_LINK_COLUMNS if name in fields})
                _check_group_links(conn, links["load_id"], links["lot_id"], links["zero_id"])
        conn.execute(
            _UPDATE_GROUP + assignments + ", updated_at = ? WHERE id = ?",
            (*values, now or _now(), group_id),
        )
    return get_group(conn, group_id)


def first_group_of_lot(conn: sqlite3.Connection, lot_id: int) -> GroupRow | None:
    """The earliest finalized group of the lot, or None.

    Order is `(shot_date, shot_time or '', finalized_at, id)`.
    """
    row = conn.execute(
        _GROUP_SELECT + " WHERE g.lot_id = ? AND g.status = 'final'"
        " ORDER BY g.shot_date, coalesce(g.shot_time, ''), g.finalized_at, g.id LIMIT 1",
        (lot_id,),
    ).fetchone()
    return GroupRow.from_row(row) if row else None


def load_has_other_lot_groups(
    conn: sqlite3.Connection,
    load_id: int,
    lot_id: int,
    earlier_than: GroupRow | None = None,
) -> bool:
    """True if the load has a finalized group under a different lot.

    With `earlier_than`, only groups strictly earlier in the order
    `(shot_date, shot_time or '', finalized_at, id)` count.
    """
    sql = "SELECT 1 FROM groups g WHERE g.load_id = ? AND g.status = 'final' AND g.lot_id <> ?"
    params: list[object] = [load_id, lot_id]
    if earlier_than is not None:
        key = (
            earlier_than.shot_date,
            earlier_than.shot_time or "",
            earlier_than.finalized_at or "",
            earlier_than.id,
        )
        sql += (
            " AND (g.shot_date, coalesce(g.shot_time, ''), coalesce(g.finalized_at, ''), g.id)"
            " < (?, ?, ?, ?)"
        )
        params.extend(key)
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


# ---------------------------------------------------------------------------------------------
# Shots
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NewShot:
    """A shot to store: plane position and provenance, plus optional derived offsets."""

    x_in: float
    y_in: float
    origin: ShotOrigin | str
    moved_by_user: bool = False
    confidence: float | None = None
    dx_in: float | None = None
    dy_in: float | None = None
    dx_mil: float | None = None
    dy_mil: float | None = None

    @classmethod
    def from_shot(cls, shot: Shot) -> NewShot:
        """Build from a domain `Shot` (no offsets)."""
        return cls(
            x_in=shot.point.x_in,
            y_in=shot.point.y_in,
            origin=shot.origin,
            moved_by_user=shot.moved_by_user,
            confidence=shot.confidence,
        )


def replace_shots(
    conn: sqlite3.Connection, group_id: int, shots: Sequence[NewShot]
) -> list[ShotRow]:
    """Replace all of the group's shots with `shots`, numbered seq 1..n, atomically.

    If any shot is invalid (bad origin, unknown group), the previous shots are left untouched.
    Opens a transaction if none is open but never commits it.
    """
    origins = [ShotOrigin(shot.origin).value for shot in shots]
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT replace_shots")
    try:
        conn.execute("DELETE FROM shots WHERE group_id = ?", (group_id,))
        conn.executemany(
            "INSERT INTO shots (group_id, seq, x_in, y_in, dx_in, dy_in, dx_mil, dy_mil, origin, "
            "moved_by_user, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    group_id,
                    seq,
                    shot.x_in,
                    shot.y_in,
                    shot.dx_in,
                    shot.dy_in,
                    shot.dx_mil,
                    shot.dy_mil,
                    origin,
                    int(bool(shot.moved_by_user)),
                    shot.confidence,
                )
                for seq, (shot, origin) in enumerate(zip(shots, origins, strict=True), start=1)
            ],
        )
    except BaseException:
        conn.execute("ROLLBACK TO replace_shots")
        conn.execute("RELEASE replace_shots")
        raise
    conn.execute("RELEASE replace_shots")
    return list_shots(conn, group_id)


def list_shots(conn: sqlite3.Connection, group_id: int) -> list[ShotRow]:
    """The group's shots ordered by seq."""
    rows = conn.execute(_SHOT_SELECT + " WHERE group_id = ? ORDER BY seq", (group_id,)).fetchall()
    return [ShotRow.from_row(r) for r in rows]


def set_shot_offsets(
    conn: sqlite3.Connection,
    shot_id: int,
    *,
    dx_in: float | None,
    dy_in: float | None,
    dx_mil: float | None,
    dy_mil: float | None,
) -> None:
    """Write one shot's derived offsets (or NULLs); positions are never touched."""
    conn.execute(
        "UPDATE shots SET dx_in = ?, dy_in = ?, dx_mil = ?, dy_mil = ? WHERE id = ?",
        (dx_in, dy_in, dx_mil, dy_mil, shot_id),
    )


# ---------------------------------------------------------------------------------------------
# Detection rejections
# ---------------------------------------------------------------------------------------------


def add_rejection(
    conn: sqlite3.Connection, group_id: int, x_in: float, y_in: float, now: str | None = None
) -> RejectionRow:
    """Record a deleted detection; the reason is always `not_a_bullet_hole` (schema CHECK)."""
    cursor = conn.execute(
        "INSERT INTO detection_rejections (group_id, x_in, y_in, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (group_id, x_in, y_in, RejectionReason.NOT_A_BULLET_HOLE.value, now or _now()),
    )
    row = conn.execute(
        "SELECT id, group_id, x_in, y_in, reason, created_at FROM detection_rejections "
        "WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return RejectionRow.from_row(row)


def list_rejections(conn: sqlite3.Connection, group_id: int) -> list[RejectionRow]:
    """The group's rejections in the order they were recorded."""
    rows = conn.execute(
        "SELECT id, group_id, x_in, y_in, reason, created_at FROM detection_rejections "
        "WHERE group_id = ? ORDER BY id",
        (group_id,),
    ).fetchall()
    return [RejectionRow.from_row(r) for r in rows]


# ---------------------------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------------------------


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _snippet(text: str, query: str) -> str:
    start = text.casefold().find(query.casefold())
    start = max(start, 0)
    lo = max(0, start - SNIPPET_RADIUS)
    hi = min(len(text), start + len(query) + SNIPPET_RADIUS)
    return f"{'...' if lo else ''}{text[lo:hi]}{'...' if hi < len(text) else ''}"


def search_notes(
    conn: sqlite3.Connection, query: str, limit: int = DEFAULT_SEARCH_LIMIT
) -> list[tuple[str, int, str]]:
    """Case-insensitive substring search of group notes and the earlier notes carried over to a group.

    Returns `(kind, id, snippet)` with kind always `group`, one hit per group by id (a group
    whose own notes match shows that snippet, otherwise the carried-over notes). `%`, `_` and `\\`
    in `query` match literally. A blank query returns nothing.
    """
    text = query.strip()
    if not text or limit < 1:
        return []
    pattern = _like_pattern(text)
    rows: Iterable[sqlite3.Row] = conn.execute(
        "SELECT id, notes, legacy_session_notes, notes LIKE ? ESCAPE '\\' AS own FROM groups "
        "WHERE notes LIKE ? ESCAPE '\\' OR legacy_session_notes LIKE ? ESCAPE '\\' "
        "ORDER BY id LIMIT ?",
        (pattern, pattern, pattern, limit),
    )
    return [
        (
            "group",
            int(row["id"]),
            _snippet(row["notes"] if row["own"] else row["legacy_session_notes"], text),
        )
        for row in rows
    ]
