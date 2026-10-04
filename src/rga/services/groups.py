"""Group service: review saves, derived offsets, statistics, finalize guards, the new-lot flag and
the resolution of a group's shot date and time.

Plane coordinates on shots are the source of truth. Stored offsets (in and mil) are derived data,
written only by `recompute_offsets` and friends through `offset_from_aim` and `in_to_mil`. Public
functions that write commit; errors are `GroupError` subclasses carrying a stable `code`.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, time

from rga.db import repo_groups, repo_rifles
from rga.db.repo_groups import NewShot
from rga.db.rows import GroupRow, LoadRow, RifleRow, ShotRow
from rga.domain.stats import group_stats as compute_group_stats
from rga.domain.types import GroupStats, PlanePoint, Shot, ShotOrigin, ShotTimeSource
from rga.domain.units import in_to_mil, in_to_moa, offset_from_aim
from rga.domain.zeroing import masks_poi

REJECTION_MATCH_TOLERANCE_IN = 1e-6
WEATHER_PROMPT_MIN_YD = 200


def weather_prompt(distance_yd: int) -> bool:
    """True when the shooting distance is 200 yd or more, so the UI must ask for weather."""
    return distance_yd >= WEATHER_PROMPT_MIN_YD


class GroupError(Exception):
    """Base error with a stable snake_case `code` and a user-facing message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class GroupNotFound(GroupError):
    """The group id does not exist."""

    def __init__(self, group_id: int) -> None:
        super().__init__("group_not_found", f"group {group_id} does not exist")


class FinalizeError(GroupError):
    """A finalize guard failed; `code` is count_mismatch, no_aim or reference_unconfirmed."""


class ShotDroppedWithoutReason(GroupError):
    """A review would remove stored detected, manual or moved shots without a rejection each."""

    def __init__(self, dropped: int, rejections: int) -> None:
        super().__init__(
            "shot_dropped_without_reason",
            f"{dropped} stored shot(s) are missing and only {rejections} new rejection(s) were "
            "given: a shot can only be removed as not a bullet hole.",
        )


@dataclass(frozen=True)
class SizeOnlyStats:
    """Group size with no point of impact; what groups with a masked point of impact and aim-less drafts get."""

    n: int
    es_in: float
    es_moa: float
    includes_estimated: bool


@dataclass(frozen=True)
class GroupBundle:
    """A group with its load, rifle, shots and statistics (None when it has no shots).

    The shooting context (date, distance, position, suppressor, weather) is on `group`.
    """

    group: GroupRow
    load: LoadRow
    rifle: RifleRow
    shots: list[ShotRow]
    stats: GroupStats | SizeOnlyStats | None


is_short_zero_group = masks_poi


def _require_group(conn: sqlite3.Connection, group_id: int) -> GroupRow:
    group = repo_groups.get_group(conn, group_id)
    if group is None:
        raise GroupNotFound(group_id)
    return group


def _aim_of(group: GroupRow) -> PlanePoint | None:
    if group.aim_x_in is None or group.aim_y_in is None:
        return None
    return PlanePoint(group.aim_x_in, group.aim_y_in)


def _write_offsets(conn: sqlite3.Connection, group: GroupRow, shots: Sequence[ShotRow]) -> None:
    aim = _aim_of(group)
    withheld = aim is None or masks_poi(group.purpose, len(shots))
    for stored in shots:
        if withheld:
            repo_groups.set_shot_offsets(
                conn, stored.id, dx_in=None, dy_in=None, dx_mil=None, dy_mil=None
            )
            continue
        offset = offset_from_aim(PlanePoint(stored.x_in, stored.y_in), aim)
        repo_groups.set_shot_offsets(
            conn,
            stored.id,
            dx_in=offset.dx_in,
            dy_in=offset.dy_in,
            dx_mil=in_to_mil(offset.dx_in, group.distance_yd),
            dy_mil=in_to_mil(offset.dy_in, group.distance_yd),
        )


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run the block, guard reads included, in one transaction that holds the write lock.

    Takes `BEGIN IMMEDIATE` first, so no other connection can change what the guards read before
    this block writes. If the caller already has a transaction open, the block runs in a
    savepoint instead: an error undoes only the block, and success commits the whole transaction.
    Commits on success and rolls back on any exception.
    """
    nested = conn.in_transaction
    conn.execute("SAVEPOINT group_service" if nested else "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if nested:
            conn.execute("ROLLBACK TO group_service")
            conn.execute("RELEASE group_service")
        else:
            conn.rollback()
        raise
    if nested:
        conn.execute("RELEASE group_service")
    conn.commit()


def _recompute_group(conn: sqlite3.Connection, group_id: int) -> None:
    group = _require_group(conn, group_id)
    _write_offsets(conn, group, repo_groups.list_shots(conn, group_id))


def recompute_offsets(conn: sqlite3.Connection, group_id: int) -> None:
    """Rewrite every shot's stored dx/dy (in and mil) from its plane position, the group's aim
    and the group's distance, then commit. Offsets are NULL without an aim and for short
    zeroing groups. Raises `GroupNotFound`.
    """
    with _write_transaction(conn):
        _recompute_group(conn, group_id)


def _stats_of(group: GroupRow, shots: Sequence[ShotRow]) -> GroupStats | SizeOnlyStats | None:
    if not shots:
        return None
    aim = _aim_of(group)
    reference = aim or PlanePoint(0.0, 0.0)
    offsets = [offset_from_aim(PlanePoint(s.x_in, s.y_in), reference) for s in shots]
    origins = [ShotOrigin(s.origin) for s in shots]
    stats = compute_group_stats(offsets, origins, group.distance_yd)
    if aim is None or masks_poi(group.purpose, len(shots)):
        return SizeOnlyStats(
            n=stats.n,
            es_in=stats.es_in,
            es_moa=in_to_moa(stats.es_in, group.distance_yd),
            includes_estimated=stats.includes_estimated,
        )
    return stats


def _masked(group: GroupRow, shots: list[ShotRow]) -> list[ShotRow]:
    if not masks_poi(group.purpose, len(shots)):
        return shots
    return [replace(s, dx_in=None, dy_in=None, dx_mil=None, dy_mil=None) for s in shots]


def _bundle(conn: sqlite3.Connection, group: GroupRow) -> GroupBundle:
    load = repo_rifles.get_load(conn, group.load_id)
    rifle = repo_rifles.get_rifle(conn, load.rifle_id) if load is not None else None
    if load is None or rifle is None:
        raise GroupNotFound(group.id)
    shots = repo_groups.list_shots(conn, group.id)
    return GroupBundle(group, load, rifle, _masked(group, shots), _stats_of(group, shots))


def load_group(conn: sqlite3.Connection, group_id: int) -> GroupBundle:
    """The group with load, rifle, shots and stats.

    Stats come fresh from plane coordinates. When the group's point of impact is masked (any
    zero_check group, a zero_step2/zero_confirm group under 10 shots, a zero_step1 group over 3
    shots, or no aim) stats are
    `SizeOnlyStats` and the returned shots carry no offsets. Raises `GroupNotFound`.
    """
    return _bundle(conn, _require_group(conn, group_id))


def group_stats(conn: sqlite3.Connection, group_id: int) -> GroupStats | SizeOnlyStats | None:
    """Statistics of the group (see `load_group`); None when it has no shots."""
    return load_group(conn, group_id).stats


def _close(a: PlanePoint, b: PlanePoint) -> bool:
    return (
        abs(a.x_in - b.x_in) <= REJECTION_MATCH_TOLERANCE_IN
        and abs(a.y_in - b.y_in) <= REJECTION_MATCH_TOLERANCE_IN
    )


def _new_rejections(
    conn: sqlite3.Connection, group_id: int, rejections: Sequence[PlanePoint]
) -> list[PlanePoint]:
    """Rejections in `rejections` not already stored, counted per shot.

    The client resends its whole list on every save, so each stored rejection is matched by at
    most one incoming rejection at the same point; any further one (two co-located shots) is new.
    """
    unmatched = [PlanePoint(r.x_in, r.y_in) for r in repo_groups.list_rejections(conn, group_id)]
    fresh: list[PlanePoint] = []
    for point in rejections:
        match = next((i for i, other in enumerate(unmatched) if _close(point, other)), None)
        if match is None:
            fresh.append(point)
        else:
            del unmatched[match]
    return fresh


def _is_placeholder(origin: str | ShotOrigin, moved_by_user: bool | int) -> bool:
    """An unmoved estimated shot is an app placeholder, not a detection (AC-10, B-44)."""
    return str(origin) == ShotOrigin.ESTIMATED.value and not moved_by_user


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


def save_review(
    conn: sqlite3.Connection,
    group_id: int,
    aim: PlanePoint | None,
    shots: Sequence[Shot],
    rejections: Sequence[PlanePoint],
    shots_expected: int,
) -> GroupBundle:
    """Store the reviewed state of a draft group in one transaction and return the bundle.

    Replaces the shots (seq 1..n), records rejections not already stored (rejections are
    append-only and counted per shot; a repeated PUT does not duplicate them), sets the aim and expected count, then
    recomputes offsets. The guard reads and the writes run in one `BEGIN IMMEDIATE` transaction,
    so a concurrent request on another connection waits (or fails as locked) instead of
    interleaving. Raises `GroupNotFound`; `GroupError` `group_final` for a final group,
    `invalid_shots_expected`, `invalid_coordinates`; `ShotDroppedWithoutReason` when stored
    detected, manual or moved shots go missing without one new rejection each (unmoved estimated
    shots are placeholders and may be dropped or replaced freely). On any error nothing is
    changed.
    """
    if shots_expected < 1:
        raise GroupError("invalid_shots_expected", "the expected shot count must be at least 1")
    coordinates = [v for s in shots for v in (s.point.x_in, s.point.y_in)]
    coordinates += [v for r in rejections for v in (r.x_in, r.y_in)]
    coordinates += [aim.x_in, aim.y_in] if aim else []
    if not _finite(*coordinates):
        raise GroupError("invalid_coordinates", "shot, rejection and aim positions must be finite")

    with _write_transaction(conn):
        group = _require_group(conn, group_id)
        if group.status == "final":
            raise GroupError("group_final", "a finalized group can no longer be reviewed")
        protected_stored = sum(
            not _is_placeholder(s.origin, s.moved_by_user)
            for s in repo_groups.list_shots(conn, group_id)
        )
        protected_new = sum(not _is_placeholder(s.origin, s.moved_by_user) for s in shots)
        fresh = _new_rejections(conn, group_id, rejections)
        dropped = protected_stored - protected_new
        if dropped > len(fresh):
            raise ShotDroppedWithoutReason(dropped, len(fresh))
        repo_groups.replace_shots(conn, group_id, [NewShot.from_shot(s) for s in shots])
        for point in fresh:
            repo_groups.add_rejection(conn, group_id, point.x_in, point.y_in)
        updated = repo_groups.update_group(
            conn,
            group_id,
            aim_x_in=None if aim is None else aim.x_in,
            aim_y_in=None if aim is None else aim.y_in,
            shots_expected=shots_expected,
        )
        _write_offsets(conn, updated, repo_groups.list_shots(conn, group_id))
    return load_group(conn, group_id)


def finalize(conn: sqlite3.Connection, group_id: int) -> GroupBundle:
    """Finalize a draft group and return the bundle; finalizing a final group changes nothing.

    Guards, in order: `reference_unconfirmed` (the group's photo has no confirmed reference),
    `no_aim`, `count_mismatch` (shot count differs from the expected count). Raises
    `FinalizeError` with that code, or `GroupNotFound`. The guards and the update share one
    `BEGIN IMMEDIATE` transaction.
    """
    with _write_transaction(conn):
        group = _require_group(conn, group_id)
        if group.status == "final":
            return _bundle(conn, group)
        if group.photo_id is not None:
            photo = repo_groups.get_photo(conn, group.photo_id)
            if photo is None or not photo.reference_confirmed:
                raise FinalizeError(
                    "reference_unconfirmed",
                    "confirm the reference object on the photo before finalizing",
                )
        if _aim_of(group) is None:
            raise FinalizeError("no_aim", "mark the aim point before finalizing")
        count = len(repo_groups.list_shots(conn, group_id))
        if count != group.shots_expected:
            advice = (
                "delete detection errors or raise the shot count"
                if count > group.shots_expected
                else "add the missing shots or lower the expected count"
            )
            raise FinalizeError(
                "count_mismatch", f"{count} of {group.shots_expected} shots: {advice}"
            )
        final = repo_groups.update_group(
            conn, group_id, status="final", finalized_at=repo_rifles.now_iso()
        )
        _write_offsets(conn, final, repo_groups.list_shots(conn, group_id))
    return load_group(conn, group_id)


def new_lot_flag(conn: sqlite3.Connection, group_id: int) -> bool:
    """True when the group is the first of a new lot of a load that already has other lots.

    A final group is flagged if it is the earliest finalized group of its lot and the load has an
    earlier finalized group under a different lot; earlier means by shot date, shot time, then
    finalization time and id. A draft is flagged prospectively: its lot has
    no finalized group yet and the load has finalized groups under another lot.
    Raises `GroupNotFound`.
    """
    group = _require_group(conn, group_id)
    if group.status == "final":
        first = repo_groups.first_group_of_lot(conn, group.lot_id)
        if first is None or first.id != group.id:
            return False
        return repo_groups.load_has_other_lot_groups(
            conn, group.load_id, group.lot_id, earlier_than=group
        )
    if repo_groups.first_group_of_lot(conn, group.lot_id) is not None:
        return False
    return repo_groups.load_has_other_lot_groups(conn, group.load_id, group.lot_id)


def _iso(value: object) -> str:
    return value.isoformat() if isinstance(value, date | time) else str(value)


def _clock(value: object) -> str:
    """`HH:MM:SS` for a time or a `HH:MM[:SS]` text."""
    text = _iso(value)
    return f"{text}:00" if len(text) == 5 else text


def resolve_shot_time(
    conn: sqlite3.Connection,
    *,
    photo_id: int | None,
    shot_date: object | None,
    shot_time: object | None,
    utc_offset: str | None,
) -> tuple[str, str | None, str | None, str]:
    """Settle a new group's `(shot_date, shot_time, utc_offset, source)`.

    With no `shot_date` the photo's capture time supplies the date, the time and the offset
    (source `photo`). A `shot_date` and `shot_time` equal to the photo's capture time are the photo's
    (source `photo`, the offset filled from the photo when none was given). Everything else is typed
    in (source `manual`); the date, time and offset are returned as given, with the time as
    `HH:MM:SS`. Nothing is converted between time zones. Raises `GroupError` `invalid_shot_date`
    (422) when no date is given and the photo has none, and `photo_not_found` (404).
    """
    taken_at = taken_offset = None
    if photo_id is not None:
        photo = repo_groups.get_photo(conn, photo_id)
        if photo is None:
            raise GroupError("photo_not_found", f"photo {photo_id} does not exist")
        taken_at, taken_offset = photo.taken_at, photo.taken_at_offset
    photo_date, _, photo_clock = (taken_at or "").partition("T")
    if shot_date is None:
        if not taken_at:
            raise GroupError(
                "invalid_shot_date",
                "the shot date is missing and the photo carries none; enter the date",
            )
        return photo_date, photo_clock, utc_offset or taken_offset, ShotTimeSource.PHOTO.value
    day = _iso(shot_date)
    clock = None if shot_time is None else _clock(shot_time)
    if taken_at and day == photo_date and clock is not None and clock == photo_clock:
        return day, clock, utc_offset or taken_offset, ShotTimeSource.PHOTO.value
    return day, clock, utc_offset, ShotTimeSource.MANUAL.value
