"""Repository query for the per-load dope table.

`dope_rows` only selects; the group's center offset is computed by the service from
the shots. The function never writes and never commits.
"""

from __future__ import annotations

import sqlite3

from rga.db.rows import DopeRow

MIN_ZEROING_SHOTS = 10

_DOPE_ROWS = (
    "SELECT groups.id AS group_id, groups.shot_date AS shot_date, groups.shot_time AS shot_time,"
    " groups.distance_yd AS distance_yd, groups.elevation_dialed_mil AS elevation_dialed_mil,"
    " (SELECT COUNT(*) FROM shots WHERE shots.group_id = groups.id) AS shot_count"
    " FROM groups"
    " WHERE groups.load_id = ? AND groups.status = 'final'"
    " AND groups.elevation_dialed_mil IS NOT NULL"
    " AND groups.purpose NOT IN ('zero_step1', 'zero_check')"
    " AND (groups.purpose NOT IN ('zero_step2', 'zero_confirm')"
    " OR (SELECT COUNT(*) FROM shots WHERE shots.group_id = groups.id) >= ?)"
    " ORDER BY groups.distance_yd, groups.shot_date, coalesce(groups.shot_time, ''), groups.id"
)


def dope_rows(conn: sqlite3.Connection, load_id: int) -> list[DopeRow]:
    """Return the load's dope rows, ordered by distance, shot date, shot time (none first), group id.

    Only finalized groups with `elevation_dialed_mil` set qualify. Groups with purpose
    `zero_step1` or `zero_check` are always excluded, and groups with purpose `zero_step2` or
    `zero_confirm` are excluded below `MIN_ZEROING_SHOTS` stored shots (B-15, B-17). The bar is
    fixed, so a short zeroing group never yields a center offset. Uses `ix_groups_load_status`.
    """
    return [DopeRow.from_row(r) for r in conn.execute(_DOPE_ROWS, (load_id, MIN_ZEROING_SHOTS))]
