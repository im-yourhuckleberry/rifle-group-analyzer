"""Group context service: group creation with lot get-or-create, group patches (date, distance,
position, suppressor, weather), group defaults, suppressors and the data behind the rifle page.

Every group carries its own shooting context. Public functions that write commit. Errors are
`SessionError` subclasses (a `GroupError`) with a stable snake_case `code`; routes map `*_not_found`
to 404, `invalid_*` to 422 and the rest to 409. A change that alters derived offsets (a group's
distance or purpose) recomputes them through the group service inside the same write transaction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from rga.db import repo_groups, repo_rifles
from rga.db.repo_groups import GroupLinkMismatch
from rga.db.repo_rifles import LoadNotInRifle
from rga.db.repo_zeroing import GroupNotLinkable, ZeroTargetMismatch
from rga.db.rows import GroupRow, LoadRow, PhotoRow, SuppressorRow
from rga.domain.types import GroupPurpose, ShotTimeSource
from rga.services import groups as group_service
from rga.services.groups import (
    GroupError,
    GroupNotFound,
    _write_transaction,
    resolve_shot_time,
    weather_prompt,
)
from rga.services.review import RoiPx, roi_from_json

__all__ = [
    "DayGroups",
    "GroupDefaults",
    "GroupLinkMismatch",
    "GroupNotLinkable",
    "GroupRowView",
    "GroupView",
    "LoadChoice",
    "LoadNotInRifle",
    "SessionError",
    "SuppressorNotFound",
    "ZeroTargetMismatch",
    "create_group",
    "create_suppressor",
    "group_defaults",
    "group_view",
    "group_view_of",
    "list_suppressors",
    "load_choices",
    "rifle_group_days",
    "suppressor_names",
    "update_group",
    "weather_prompt",
]

WEATHER_FIELDS = ("temperature_f", "density_altitude_ft", "wind_mph", "wind_clock")
CONTEXT_PATCHABLE = frozenset(
    {
        "shot_date",
        "shot_time",
        "utc_offset",
        "distance_yd",
        "position",
        "suppressor",
        "suppressor_id",
        "weather_skipped",
        *WEATHER_FIELDS,
    }
)
GROUP_PATCHABLE = (
    frozenset({"shots_expected", "elevation_dialed_mil", "purpose", "zero_id", "photo_id", "notes"})
    | CONTEXT_PATCHABLE
)
GENERAL_PURPOSE = GroupPurpose.GENERAL.value
ZERO_ROUTES_ONLY_MESSAGE = (
    "A group can only be created or updated as a general group, without a zero: zeroing "
    "purposes and zero links are set only by the zeroing routes (link, check, detach)."
)
LINK_ERROR_CODES: tuple[tuple[type[Exception], str], ...] = (
    (GroupLinkMismatch, "group_link_mismatch"),
    (GroupNotLinkable, "group_not_linkable"),
    (ZeroTargetMismatch, "zero_target_mismatch"),
    (LoadNotInRifle, "load_not_in_rifle"),
)
LINK_ERRORS = tuple(kind for kind, _ in LINK_ERROR_CODES)
POSITION_LABELS = {
    "bench": "Bench",
    "prone_off_pack": "Prone off a pack",
    "kneeling_sitting": "Kneeling or sitting",
    "standing_field": "Standing, field",
}


class SessionError(GroupError):
    """Base error of this slice: a stable `code` plus a user-facing message."""


class RifleNotFound(SessionError):
    """The rifle id does not exist."""

    def __init__(self, rifle_id: int) -> None:
        super().__init__("rifle_not_found", f"rifle {rifle_id} does not exist")


class LoadNotFound(SessionError):
    """The load id does not exist."""

    def __init__(self, load_id: int) -> None:
        super().__init__("load_not_found", f"load {load_id} does not exist")


class SuppressorNotFound(SessionError):
    """The suppressor id does not exist."""

    def __init__(self, suppressor_id: int) -> None:
        super().__init__("suppressor_not_found", f"suppressor {suppressor_id} does not exist")


class PhotoNotFound(SessionError):
    """The photo id does not exist."""

    def __init__(self, photo_id: int) -> None:
        super().__init__("photo_not_found", f"photo {photo_id} does not exist")


class ZeroNotFound(SessionError):
    """The zero id does not exist."""

    def __init__(self, zero_id: int) -> None:
        super().__init__("zero_not_found", f"zero {zero_id} does not exist")


@dataclass(frozen=True)
class GroupView:
    """A group with its lot code, the new-lot flag, its parsed ROI (working px), its stored shot
    count (what `masks_poi` needs), the id of its rifle (through its load) and the name of its
    suppressor (None when it has none).
    """

    group: GroupRow
    lot_code: str
    new_lot: bool
    roi: RoiPx | None
    shot_count: int
    rifle_id: int
    suppressor_name: str | None


def group_view_of(conn: sqlite3.Connection, group: GroupRow) -> GroupView:
    """The view of an already loaded group row."""
    lot = repo_rifles.get_lot(conn, group.lot_id)
    load = repo_rifles.get_load(conn, group.load_id)
    can = repo_groups.get_suppressor(conn, group.suppressor_id) if group.suppressor_id else None
    return GroupView(
        group=group,
        lot_code=lot.lot_code if lot else "",
        new_lot=group_service.new_lot_flag(conn, group.id),
        roi=roi_from_json(group.roi_json),
        shot_count=len(repo_groups.list_shots(conn, group.id)),
        rifle_id=load.rifle_id if load else 0,
        suppressor_name=can.name if can else None,
    )


_view = group_view_of


def _require_group_row(conn: sqlite3.Connection, group_id: int) -> GroupRow:
    group = repo_groups.get_group(conn, group_id)
    if group is None:
        raise GroupNotFound(group_id)
    return group


def group_view(conn: sqlite3.Connection, group_id: int) -> GroupView:
    """One group's view: lot code, new-lot flag, ROI and shot count. Raises `GroupNotFound`."""
    return _view(conn, _require_group_row(conn, group_id))


def _check_weather(skipped: bool, values: dict[str, Any]) -> None:
    if skipped and any(values[name] is not None for name in WEATHER_FIELDS):
        raise SessionError(
            "invalid_weather", "weather cannot be both entered and skipped; clear one of them"
        )


def _check_suppressor(
    conn: sqlite3.Connection, suppressor: bool, suppressor_id: int | None
) -> None:
    if suppressor_id is None:
        return
    if not suppressor:
        raise SessionError(
            "invalid_suppressor", "a suppressor can only be named when the suppressor is on"
        )
    if repo_groups.get_suppressor(conn, suppressor_id) is None:
        raise SuppressorNotFound(suppressor_id)


# ---------------------------------------------------------------------------------------------
# Suppressors
# ---------------------------------------------------------------------------------------------


def list_suppressors(conn: sqlite3.Connection) -> list[SuppressorRow]:
    """Every saved suppressor, by name (case-insensitive)."""
    return repo_groups.list_suppressors(conn)


def suppressor_names(conn: sqlite3.Connection) -> dict[int, str]:
    """Suppressor names keyed by id, for labelling groups."""
    return {s.id: s.name for s in repo_groups.list_suppressors(conn)}


def _clean_name(name: str) -> str:
    return " ".join(name.split())


def create_suppressor(conn: sqlite3.Connection, name: str) -> tuple[SuppressorRow, bool]:
    """Remember a suppressor by name; returns `(row, created)`.

    The name is trimmed and inner whitespace collapsed. A name that matches a saved one in any
    letter case (full Unicode case folding) returns the saved record, with its original spelling,
    and `created` False. Raises `SessionError` `invalid_name` for a blank name.
    """
    clean = _clean_name(name)
    if not clean:
        raise SessionError("invalid_name", "the suppressor name must not be blank")
    with _write_transaction(conn):
        for existing in repo_groups.list_suppressors(conn):
            if existing.name.casefold() == clean.casefold():
                return existing, False
        try:
            return repo_groups.create_suppressor(conn, clean), True
        except sqlite3.IntegrityError:
            for existing in repo_groups.list_suppressors(conn):
                if existing.name.casefold() == clean.casefold():
                    return existing, False
            raise


# ---------------------------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------------------------


def create_group(
    conn: sqlite3.Connection,
    *,
    load_id: int,
    lot_code: str,
    shots_expected: int,
    distance_yd: int,
    position: Any,
    suppressor: bool,
    suppressor_id: int | None = None,
    shot_date: Any = None,
    shot_time: Any = None,
    utc_offset: str | None = None,
    temperature_f: float | None = None,
    density_altitude_ft: float | None = None,
    wind_mph: float | None = None,
    wind_clock: int | None = None,
    weather_skipped: bool = False,
    elevation_dialed_mil: float | None = None,
    purpose: Any = GroupPurpose.GENERAL,
    zero_id: int | None = None,
    photo_id: int | None = None,
    notes: str = "",
) -> GroupView:
    """Create a draft group with its own context, creating the lot when its code is new.

    The lot code is trimmed. The date and time come from the photo's capture time when no date is
    given (see `resolve_shot_time`). Zero purposes and `zero_id` are not accepted here: they are
    set only by the zeroing routes. Raises `LoadNotFound`, `PhotoNotFound`, `SuppressorNotFound`,
    `SessionError` `invalid_lot_code` for a blank code, `invalid_purpose` for any purpose other
    than general or any `zero_id`, `invalid_weather` when weather is both entered and skipped,
    `invalid_suppressor` for a suppressor name on an unsuppressed group, and `invalid_shot_date`.
    Nothing is written on any error.
    """
    code = lot_code.strip()
    if not code:
        raise SessionError("invalid_lot_code", "the lot code must not be blank")
    if str(purpose) != GENERAL_PURPOSE or zero_id is not None:
        raise SessionError("invalid_purpose", ZERO_ROUTES_ONLY_MESSAGE)
    weather = {
        "temperature_f": temperature_f,
        "density_altitude_ft": density_altitude_ft,
        "wind_mph": wind_mph,
        "wind_clock": wind_clock,
    }
    _check_weather(weather_skipped, weather)
    with _write_transaction(conn):
        if repo_rifles.get_load(conn, load_id) is None:
            raise LoadNotFound(load_id)
        if photo_id is not None and repo_groups.get_photo(conn, photo_id) is None:
            raise PhotoNotFound(photo_id)
        _check_suppressor(conn, bool(suppressor), suppressor_id)
        day, clock, offset, source = resolve_shot_time(
            conn, photo_id=photo_id, shot_date=shot_date, shot_time=shot_time, utc_offset=utc_offset
        )
        lot = repo_rifles.get_or_create_lot(conn, load_id, code)
        group = repo_groups.create_group(
            conn,
            load_id=load_id,
            lot_id=lot.id,
            shots_expected=shots_expected,
            shot_date=day,
            shot_time=clock,
            utc_offset=offset,
            shot_at_source=source,
            distance_yd=distance_yd,
            position=str(position),
            suppressor=bool(suppressor),
            suppressor_id=suppressor_id,
            weather_skipped=weather_skipped,
            photo_id=photo_id,
            elevation_dialed_mil=elevation_dialed_mil,
            purpose=GENERAL_PURPOSE,
            notes=notes,
            **weather,
        )
        return _view(conn, group)


def update_group(conn: sqlite3.Connection, group_id: int, changes: dict[str, Any]) -> GroupView:
    """Apply the patched group fields; return the view.

    `purpose` may only be set to general and `zero_id` may not be set at all (`invalid_purpose`;
    zeroing purposes and links belong to the zeroing routes); changing the purpose of a group that
    is linked to a zero is refused (`group_linked_to_zero`, detach it first), and so is changing its
    distance (zeroing is 100 yd only). A final group cannot change `shots_expected` or `photo_id`
    (`group_final`). A photo can only be attached, replaced or removed while the group has no shots
    and no aim (`group_has_shots`); a different photo clears the stored ROI. A new date or time makes
    the date source `manual`; a new distance or purpose recomputes the group's offsets. Weather is
    judged on the resulting row (`invalid_weather`), as is the suppressor name
    (`invalid_suppressor`; switching the suppressor off clears the name). A group cannot move to
    another load (`invalid_load_change`). Raises `GroupNotFound`, `PhotoNotFound`,
    `SuppressorNotFound`.
    """
    if "load_id" in changes:
        raise SessionError("invalid_load_change", "a group cannot move to another load")
    unknown = sorted(set(changes) - GROUP_PATCHABLE)
    if unknown:
        raise ValueError(f"unknown group field(s): {', '.join(unknown)}")
    with _write_transaction(conn):
        group = _require_group_row(conn, group_id)
        stored = _group_fields(conn, group, changes)
        updated = repo_groups.update_group(conn, group_id, **stored)
        if updated is None:
            raise GroupNotFound(group_id)
        moved = "distance_yd" in stored and stored["distance_yd"] != group.distance_yd
        if moved or ("purpose" in stored and stored["purpose"] != group.purpose):
            group_service.recompute_offsets(conn, group_id)
            updated = _require_group_row(conn, group_id)
        return _view(conn, updated)


def _group_fields(
    conn: sqlite3.Connection, group: GroupRow, changes: dict[str, Any]
) -> dict[str, Any]:
    if "zero_id" in changes:
        raise SessionError("invalid_purpose", ZERO_ROUTES_ONLY_MESSAGE)
    if "purpose" in changes:
        if str(changes["purpose"]) != GENERAL_PURPOSE:
            raise SessionError("invalid_purpose", ZERO_ROUTES_ONLY_MESSAGE)
        if group.zero_id is not None:
            raise SessionError(
                "group_linked_to_zero",
                "this group is linked to a zero: detach it from the zero before changing its purpose",
            )
    if (
        "distance_yd" in changes
        and changes["distance_yd"] != group.distance_yd
        and group.zero_id is not None
    ):
        raise SessionError(
            "group_linked_to_zero",
            "this group is linked to a zero, which must be shot at 100 yd: "
            "detach it from the zero before changing the distance",
        )
    stored: dict[str, Any] = {}
    for name, value in changes.items():
        if name in ("shot_date", "shot_time", "position", "purpose"):
            stored[name] = None if value is None else _text(name, value)
        else:
            stored[name] = value
    if "shot_date" in changes or "shot_time" in changes:
        if changes.get("shot_date", group.shot_date) is None:
            raise SessionError("invalid_shot_date", "a group needs a shot date")
        stored["shot_at_source"] = ShotTimeSource.MANUAL.value
    _check_weather_on_row(group, changes)
    if "suppressor" in changes or "suppressor_id" in changes:
        stored["suppressor_id"] = _resulting_suppressor_id(conn, group, changes)
    if group.status == "final":
        for name in ("shots_expected", "photo_id"):
            if name in changes and changes[name] != getattr(group, name):
                raise SessionError("group_final", f"a finalized group cannot change {name}")
    if "photo_id" in changes and changes["photo_id"] != group.photo_id:
        new_photo = changes["photo_id"]
        if new_photo is not None and repo_groups.get_photo(conn, new_photo) is None:
            raise PhotoNotFound(new_photo)
        has_work = bool(repo_groups.list_shots(conn, group.id)) or group.aim_x_in is not None
        if has_work:
            raise SessionError(
                "group_has_shots",
                "the group already has shots or an aim point; the photo cannot be changed",
            )
        stored["roi_json"] = None
    return stored


def _text(name: str, value: Any) -> str:
    if name == "shot_time":
        return group_service._clock(value)
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _check_weather_on_row(group: GroupRow, changes: dict[str, Any]) -> None:
    merged = {name: getattr(group, name) for name in WEATHER_FIELDS}
    merged.update({k: v for k, v in changes.items() if k in WEATHER_FIELDS})
    _check_weather(bool(changes.get("weather_skipped", group.weather_skipped)), merged)


def _resulting_suppressor_id(
    conn: sqlite3.Connection, current: GroupRow, changes: dict[str, Any]
) -> int | None:
    on = bool(changes["suppressor"]) if "suppressor" in changes else bool(current.suppressor)
    wanted = changes.get("suppressor_id", current.suppressor_id)
    if not on:
        if changes.get("suppressor_id") is not None:
            raise SessionError(
                "invalid_suppressor", "a suppressor can only be named when the suppressor is on"
            )
        return None
    if wanted is not None and repo_groups.get_suppressor(conn, wanted) is None:
        raise SuppressorNotFound(wanted)
    return wanted


# ---------------------------------------------------------------------------------------------
# Defaults for the next group
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupDefaults:
    """Values to pre-fill the next group; every field is None when there is nothing to copy.

    `source_group_id` is the rifle's own latest group. Without one, distance, position, suppressor
    and its name come from the latest group of any rifle and everything else stays None.
    """

    source_group_id: int | None = None
    load_id: int | None = None
    lot_code: str | None = None
    shots_expected: int | None = None
    shot_date: str | None = None
    distance_yd: int | None = None
    position: str | None = None
    suppressor: bool | None = None
    suppressor_id: int | None = None
    temperature_f: float | None = None
    density_altitude_ft: float | None = None
    wind_mph: float | None = None
    wind_clock: int | None = None
    weather_skipped: bool | None = None


def group_defaults(conn: sqlite3.Connection, rifle_id: int) -> GroupDefaults:
    """Pre-fill values for a new group of the rifle. Raises `RifleNotFound`.

    Load, lot, shots, date, weather and the context come from the rifle's highest-id group; with
    none, only distance, position, suppressor and its name come from the highest-id group overall.
    """
    if repo_rifles.get_rifle(conn, rifle_id) is None:
        raise RifleNotFound(rifle_id)
    source = repo_groups.latest_group(conn, rifle_id)
    if source is None:
        other = repo_groups.latest_group(conn)
        if other is None:
            return GroupDefaults()
        return GroupDefaults(
            distance_yd=other.distance_yd,
            position=other.position,
            suppressor=bool(other.suppressor),
            suppressor_id=other.suppressor_id,
        )
    lot = repo_rifles.get_lot(conn, source.lot_id)
    return GroupDefaults(
        source_group_id=source.id,
        load_id=source.load_id,
        lot_code=lot.lot_code if lot else None,
        shots_expected=source.shots_expected,
        shot_date=source.shot_date,
        distance_yd=source.distance_yd,
        position=source.position,
        suppressor=bool(source.suppressor),
        suppressor_id=source.suppressor_id,
        temperature_f=source.temperature_f,
        density_altitude_ft=source.density_altitude_ft,
        wind_mph=source.wind_mph,
        wind_clock=source.wind_clock,
        weather_skipped=bool(source.weather_skipped),
    )


# ---------------------------------------------------------------------------------------------
# Rifle page
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupRowView:
    """One line of the rifle page's group list: the group, its size statistics and its photo state.

    `size` is `(n, es_in, es_moa, includes_estimated)` or None without shots. No point of impact
    is carried, so masked short zeroing groups cannot leak one. `weather_prompt` is true from 200 yd; `weather_needed` when it is and no weather was
    entered or skipped.
    """

    view: GroupView
    load_label: str
    shot_count: int
    size: tuple[int, float, float, bool] | None
    masked: bool
    photo: PhotoRow | None
    weather_prompt: bool
    weather_needed: bool
    suppressor_name: str | None


@dataclass(frozen=True)
class DayGroups:
    """One shooting day of a rifle: its distances, groups (by time, then id) and the photos of
    those groups, which another group of the day can reuse.
    """

    shot_date: str
    distances: list[int]
    rows: list[GroupRowView]
    photos: list[PhotoRow]


@dataclass(frozen=True)
class LoadChoice:
    """A load of a rifle with its known lot codes, for the new-group form."""

    load: LoadRow
    lots: list[str]


def _row_view(
    conn: sqlite3.Connection, group: GroupRow, load_labels: dict[int, str], names: dict[int, str]
) -> GroupRowView:
    view = _view(conn, group)
    stats = group_service.group_stats(conn, group.id)
    masked = group_service.masks_poi(group.purpose, view.shot_count)
    size = (stats.n, stats.es_in, stats.es_moa, stats.includes_estimated) if stats else None
    photo = repo_groups.get_photo(conn, group.photo_id) if group.photo_id else None
    prompt = weather_prompt(group.distance_yd)
    entered = group.temperature_f is not None or group.weather_skipped
    return GroupRowView(
        view=view,
        load_label=load_labels.get(group.load_id, ""),
        shot_count=view.shot_count,
        size=size,
        masked=masked,
        photo=photo,
        weather_prompt=prompt,
        weather_needed=prompt and not entered,
        suppressor_name=names.get(group.suppressor_id) if group.suppressor_id else None,
    )


def rifle_group_days(conn: sqlite3.Connection, rifle_id: int) -> list[DayGroups]:
    """The rifle's groups grouped by shot date, newest day first; each day's rows run by time, then
    id. Raises `RifleNotFound`.
    """
    if repo_rifles.get_rifle(conn, rifle_id) is None:
        raise RifleNotFound(rifle_id)
    labels = {load.id: load.label for load in repo_rifles.list_loads(conn, rifle_id)}
    names = suppressor_names(conn)
    groups = repo_groups.list_groups(conn, rifle_id=rifle_id)
    by_day: dict[str, list[GroupRow]] = {}
    for group in groups:
        by_day.setdefault(group.shot_date, []).append(group)
    days = []
    for day in sorted(by_day, reverse=True):
        ordered = sorted(by_day[day], key=lambda g: (g.shot_time or "", g.id))
        rows = [_row_view(conn, g, labels, names) for g in ordered]
        photos: dict[int, PhotoRow] = {r.photo.id: r.photo for r in rows if r.photo is not None}
        days.append(
            DayGroups(
                shot_date=day,
                distances=sorted({g.distance_yd for g in ordered}),
                rows=rows,
                photos=list(photos.values()),
            )
        )
    return days


def load_choices(conn: sqlite3.Connection, rifle_id: int) -> list[LoadChoice]:
    """The rifle's loads with their lot codes (creation order)."""
    return [
        LoadChoice(load, [lot.lot_code for lot in repo_rifles.list_lots(conn, load.id)])
        for load in repo_rifles.list_loads(conn, rifle_id)
    ]
