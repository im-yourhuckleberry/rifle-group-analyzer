"""Group analysis service: group lists and detail, statistics with the point-of-impact mask,
the aggregate overlay, notes search and the group page's plot.

Reads only. A group whose point of impact is masked (`groups.masks_poi`: any zero_check group, a
zero_step2 or zero_confirm group under 10 shots, or a group with no aim) yields size-only
statistics and no per-shot offsets, and can never be part of an aggregate, so no mean point of
impact, offset or direction text can be derived from it. Errors are `GroupError` subclasses with
a stable snake_case `code` (`*_not_found` maps to 404, `invalid_*` to 422, the rest to 409).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from rga.db import repo_aggregates, repo_groups, repo_rifles
from rga.db.repo_aggregates import AggregateRow
from rga.db.rows import GroupRow, LoadRow, RifleRow, SuppressorRow
from rga.domain.comparison import tier_for
from rga.domain.enclosing import circle_about, smallest_enclosing_circle
from rga.domain.stats import AggregateInput, AggregateStats
from rga.domain.stats import aggregate as aggregate_groups
from rga.domain.types import GroupStats, PlanePoint, ShotOrigin
from rga.domain.units import (
    ZERO_DISTANCE_YD,
    in_to_mil,
    in_to_moa,
    offset_from_aim,
    round_tenth,
)
from rga.domain.zeroing import STEP2_MIN_SHOTS, correction_for
from rga.services import groups as group_service
from rga.services import review as review_service
from rga.services.group_records import GroupView, group_view_of
from rga.services.groups import (
    GroupBundle,
    GroupError,
    GroupNotFound,
    SizeOnlyStats,
    _write_transaction,
)
from rga.services.review import GroupView as GroupDetail

SEARCH_LIMIT = 50
GROUP_LIST_PAGE_SIZE = 100
NOTES_SNIPPET_CHARS = 120
PURPOSE_LABELS = {
    "general": "General",
    "zero_step1": "Zero step 1",
    "zero_step2": "Zero step 2",
    "zero_confirm": "Zero confirm",
    "zero_check": "Zero check",
}
MIXED_LOADS_WARNING = "These groups were shot with different loads; the combined numbers mix them."
PLOT_PX_PER_IN = 100.0
PLOT_MARGIN_IN = 0.6
PLOT_MIN_HALF_IN = 1.5
PLOT_PHOTO_MARGIN_IN = 0.5

__all__ = [
    "AggregateNotFound",
    "AggregateResult",
    "AggregateShot",
    "AnalysisError",
    "GroupDetail",
    "GroupError",
    "GroupFilterOptions",
    "GroupListFilters",
    "GroupListPage",
    "GroupListRow",
    "GroupNotFound",
    "GroupPage",
    "GroupView",
    "Plot",
    "PlotCircle",
    "PlotCorrection",
    "PlotPhoto",
    "PlotShot",
    "SavedAggregate",
    "SizeOnlyStats",
    "aggregate",
    "delete_saved",
    "direction_text",
    "group_detail",
    "group_filter_options",
    "group_list",
    "group_page",
    "group_stats",
    "list_groups",
    "list_saved",
    "save_aggregate",
    "search_notes",
]


class AnalysisError(GroupError):
    """Base error of the analysis slice: a stable `code` plus a user-facing message."""


def direction_text(dx_mil: float, dy_mil: float) -> str:
    """Mean point of impact in words, e.g. "0.3 mil high, 0.2 mil right" (to the nearest 0.1 mil).

    A description only, never a correction. Both axes within half a click read "on the aim point".
    """
    parts = []
    dy, dx = round_tenth(dy_mil), round_tenth(dx_mil)
    if dy:
        parts.append(f"{abs(dy):.1f} mil {'high' if dy > 0 else 'low'}")
    if dx:
        parts.append(f"{abs(dx):.1f} mil {'right' if dx > 0 else 'left'}")
    return ", ".join(parts) if parts else "on the aim point"


# ---------------------------------------------------------------------------------------------
# Lists and detail
# ---------------------------------------------------------------------------------------------


def list_groups(
    conn: sqlite3.Connection,
    load_id: int | None = None,
    rifle_id: int | None = None,
    status: str | None = None,
) -> list[GroupView]:
    """Groups in creation order filtered by any combination of load, rifle and status.

    Views carry the lot code, the new-lot flag and the parsed ROI; no shots or statistics, so the
    list can never expose a masked point of impact.
    """
    views = []
    for group in repo_groups.list_groups(conn, load_id=load_id, rifle_id=rifle_id, status=status):
        views.append(group_view_of(conn, group))
    return views


@dataclass(frozen=True)
class GroupListFilters:
    """What the groups list narrows to; every field is optional and they combine with AND.

    `suppressor` is "yes" (any suppressor on), "none" (suppressor off) or None (any); `suppressor_id`
    picks one named suppressor. `date_from` and `date_to` are inclusive `YYYY-MM-DD` bounds.
    """

    rifle_id: int | None = None
    load_id: int | None = None
    distance_yd: int | None = None
    position: str | None = None
    suppressor: str | None = None
    suppressor_id: int | None = None
    status: str | None = None
    purpose: str | None = None
    date_from: str | None = None
    date_to: str | None = None


@dataclass(frozen=True)
class GroupListRow:
    """One line of the groups list: the group, where it belongs and its size.

    `size` is `(n, es_in, es_moa, es_mil, includes_estimated)` or None without shots. It is size
    only: no point of impact, offset, aim or correction is carried, so a masked zeroing group can
    be listed like any other.
    """

    group: GroupRow
    rifle_name: str
    load_label: str
    lot_code: str
    suppressor_name: str | None
    shot_count: int
    size: tuple[int, float, float, float, bool] | None
    notes_snippet: str
    purpose_label: str


@dataclass(frozen=True)
class GroupListPage:
    """One page of the groups list, newest first."""

    rows: list[GroupListRow]
    total: int
    page: int
    pages: int
    page_size: int


@dataclass(frozen=True)
class GroupFilterOptions:
    """The choices the groups list's filter form offers."""

    rifles: list[RifleRow]
    loads: list[LoadRow]
    suppressors: list[SuppressorRow]
    distances: list[int]


def group_filter_options(conn: sqlite3.Connection) -> GroupFilterOptions:
    """Every rifle, load and saved suppressor, and each distance any group was shot at."""
    rifles = repo_rifles.list_rifles(conn)
    loads = [load for rifle in rifles for load in repo_rifles.list_loads(conn, rifle.id)]
    distances = sorted({g.distance_yd for g in repo_groups.list_groups(conn)})
    return GroupFilterOptions(rifles, loads, repo_groups.list_suppressors(conn), distances)


def _snippet(group: GroupRow) -> str:
    text = " ".join((group.notes or group.legacy_session_notes).split())
    return text if len(text) <= NOTES_SNIPPET_CHARS else text[: NOTES_SNIPPET_CHARS - 1] + "…"


def group_list(
    conn: sqlite3.Connection, filters: GroupListFilters | None = None, page: int = 1
) -> GroupListPage:
    """A page of `GROUP_LIST_PAGE_SIZE` groups of every rifle, newest first by shot date, shot time
    and id, narrowed by `filters`. A page past the last is empty; a page below 1 is page 1.
    """
    f = filters or GroupListFilters()
    suppressor = {"yes": True, "none": False}.get(f.suppressor or "")
    matching = repo_groups.list_groups(
        conn,
        load_id=f.load_id,
        rifle_id=f.rifle_id,
        status=f.status,
        distance_yd=f.distance_yd,
        position=f.position,
        suppressor=suppressor,
        suppressor_id=f.suppressor_id,
        purpose=f.purpose,
        date_from=f.date_from,
        date_to=f.date_to,
        newest_first=True,
    )
    total = len(matching)
    pages = max(1, -(-total // GROUP_LIST_PAGE_SIZE))
    page = max(1, page)
    window = matching[(page - 1) * GROUP_LIST_PAGE_SIZE : page * GROUP_LIST_PAGE_SIZE]
    loads = {
        load.id: load
        for rifle in repo_rifles.list_rifles(conn)
        for load in repo_rifles.list_loads(conn, rifle.id)
    }
    rifle_names = {rifle.id: rifle.name for rifle in repo_rifles.list_rifles(conn)}
    can_names = {can.id: can.name for can in repo_groups.list_suppressors(conn)}
    rows = []
    for group in window:
        load = loads.get(group.load_id)
        lot = repo_rifles.get_lot(conn, group.lot_id)
        stats = group_service.group_stats(conn, group.id)
        rows.append(
            GroupListRow(
                group=group,
                rifle_name=rifle_names.get(load.rifle_id, "") if load else "",
                load_label=load.label if load else "",
                lot_code=lot.lot_code if lot else "",
                suppressor_name=can_names.get(group.suppressor_id) if group.suppressor_id else None,
                shot_count=len(repo_groups.list_shots(conn, group.id)),
                size=None
                if stats is None
                else (
                    stats.n,
                    stats.es_in,
                    stats.es_moa,
                    in_to_mil(stats.es_in, group.distance_yd),
                    stats.includes_estimated,
                ),
                notes_snippet=_snippet(group),
                purpose_label=PURPOSE_LABELS.get(group.purpose, group.purpose),
            )
        )
    return GroupListPage(rows, total, page, pages, GROUP_LIST_PAGE_SIZE)


def group_detail(conn: sqlite3.Connection, group_id: int) -> GroupDetail:
    """The group with context, shots (offsets withheld when masked) and rejections.

    Raises `GroupNotFound`.
    """
    return review_service.group_view(conn, group_id)


def group_stats(conn: sqlite3.Connection, group_id: int) -> GroupStats | SizeOnlyStats:
    """Statistics of one group: full for an unmasked group, size only for a masked one.

    Raises `GroupNotFound`, or `AnalysisError` `group_has_no_shots` for a group without shots.
    """
    stats = group_service.group_stats(conn, group_id)
    if stats is None:
        raise AnalysisError("group_has_no_shots", f"group {group_id} has no shots yet")
    return stats


# ---------------------------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AggregateShot:
    """One shot of the overlay, offset from its own group's aim point (inches, y up)."""

    group_id: int
    dx_in: float
    dy_in: float
    origin: ShotOrigin


@dataclass(frozen=True)
class AggregateResult:
    """Combined statistics of the selected groups, the overlay points and any warnings."""

    stats: AggregateStats
    group_count: int
    warnings: list[str]
    shots: list[AggregateShot]


def _check_aggregatable(bundles: list[GroupBundle]) -> None:
    for bundle in bundles:
        group = bundle.group
        if group.status != "final":
            raise AnalysisError(
                "group_not_final", f"group {group.id} is a draft; finalize it before aggregating"
            )
        if group_service.masks_poi(group.purpose, len(bundle.shots)):
            raise AnalysisError(
                "masked_group",
                f"group {group.id} is a zeroing group whose point of impact is hidden; "
                "it cannot be part of an aggregate",
            )
    if len({b.rifle.id for b in bundles}) > 1:
        raise AnalysisError(
            "invalid_mixed_rifle", "groups must all belong to the same rifle to be aggregated"
        )
    distances = sorted({b.group.distance_yd for b in bundles})
    if len(distances) > 1:
        raise AnalysisError(
            "invalid_mixed_distance",
            "groups must all be shot at the same distance to be aggregated; got "
            + ", ".join(f"{d} yd" for d in distances),
        )


def _pool(bundles: list[GroupBundle]) -> AggregateResult:
    """Pool already validated bundles on their own aim points."""
    inputs, shots = [], []
    for bundle in bundles:
        group = bundle.group
        aim = PlanePoint(group.aim_x_in, group.aim_y_in)
        offsets = [offset_from_aim(PlanePoint(s.x_in, s.y_in), aim) for s in bundle.shots]
        origins = [ShotOrigin(s.origin) for s in bundle.shots]
        inputs.append(
            AggregateInput(offsets, origins, bundle.group.distance_yd, load_id=group.load_id)
        )
        shots.extend(
            AggregateShot(group.id, o.dx_in, o.dy_in, origin)
            for o, origin in zip(offsets, origins, strict=True)
        )
    stats = aggregate_groups(inputs)
    warnings = []
    if stats.mixed_loads:
        warnings.append(MIXED_LOADS_WARNING)
    return AggregateResult(stats, len(bundles), warnings, shots)


def aggregate(conn: sqlite3.Connection, group_ids: list[int]) -> AggregateResult:
    """Overlay finalized groups on their own aim points and measure the union.

    Raises `GroupNotFound` for an unknown id; `AnalysisError` `group_not_final` (409),
    `masked_group` (409, any zero_check group or a short zero_step2/zero_confirm group),
    `invalid_mixed_rifle` or `invalid_mixed_distance` (422). Groups of different loads combine with
    a warning and `stats.mixed_loads`.
    """
    bundles = [group_service.load_group(conn, group_id) for group_id in group_ids]
    _check_aggregatable(bundles)
    return _pool(bundles)


# ---------------------------------------------------------------------------------------------
# Saved aggregates
# ---------------------------------------------------------------------------------------------


class AggregateNotFound(AnalysisError):
    """The saved aggregate id does not exist."""

    def __init__(self, aggregate_id: int) -> None:
        super().__init__("aggregate_not_found", f"aggregate {aggregate_id} does not exist")


@dataclass(frozen=True)
class SavedAggregate:
    """A saved aggregate with its live statistics over the members that are still usable.

    `valid` is False when a member was deleted or is no longer a finalized, unmasked group at one
    shared distance (`invalid_reason` says which). `positions` and `suppressors` are those of the
    usable members, for the comparison's filters. `row.distance_yd` is the members' live
    shared distance (it follows edits to their groups); only when they share none is it the saved one.
    """

    row: AggregateRow
    result: AggregateResult
    member_ids: list[int]
    valid: bool
    invalid_reason: str | None
    tier: str | None
    positions: frozenset[str]
    suppressors: frozenset[bool]


_EMPTY = AggregateStats(
    n=0,
    es_in=0.0,
    es_moa=0.0,
    center_dx_in=0.0,
    center_dy_in=0.0,
    center_dx_mil=0.0,
    center_dy_mil=0.0,
    includes_estimated=False,
    mixed_loads=False,
)


def _live_distance(row: AggregateRow, bundles: list[GroupBundle]) -> int:
    """The members' current shared distance; the saved one when they have none or disagree."""
    distances = {b.group.distance_yd for b in bundles}
    return next(iter(distances)) if len(distances) == 1 else row.distance_yd


def _evaluate(conn: sqlite3.Connection, row: AggregateRow) -> SavedAggregate:
    member_ids = repo_aggregates.member_group_ids(conn, row.id)
    reasons: list[str] = []
    if len(member_ids) < row.group_count:
        reasons.append(
            f"{row.group_count - len(member_ids)} group(s) of this combination were deleted"
        )
    usable: list[GroupBundle] = []
    members: list[GroupBundle] = []
    for group_id in member_ids:
        bundle = group_service.load_group(conn, group_id)
        members.append(bundle)
        if bundle.group.status != "final":
            reasons.append(f"group {group_id} is no longer finalized")
        elif group_service.masks_poi(bundle.group.purpose, len(bundle.shots)):
            reasons.append(f"group {group_id} is now a zeroing group with a hidden point of impact")
        else:
            usable.append(bundle)
    distances = sorted({b.group.distance_yd for b in usable})
    if len(distances) > 1:
        listing = ", ".join(str(d) for d in distances)
        reasons.append(f"the remaining groups are no longer at one distance ({listing} yd)")
        usable = []
    result = _pool(usable) if usable else AggregateResult(_EMPTY, 0, [], [])
    valid = not reasons
    return SavedAggregate(
        row=replace(row, distance_yd=_live_distance(row, usable or members)),
        result=result,
        member_ids=member_ids,
        valid=valid,
        invalid_reason="; ".join(reasons) if reasons else None,
        tier=tier_for(result.stats.n) if valid else None,
        positions=frozenset(b.group.position for b in usable),
        suppressors=frozenset(bool(b.group.suppressor) for b in usable),
    )


def save_aggregate(conn: sqlite3.Connection, name: str, group_ids: list[int]) -> SavedAggregate:
    """Save a named combination of finalized groups of one load, rifle and distance.

    Same validation as `aggregate` (`GroupNotFound`, `group_not_final`, `masked_group` as 409,
    `invalid_mixed_rifle` and `invalid_mixed_distance` as 422) plus `invalid_mixed_load` (422), and
    `duplicate_aggregate` (409) when the load already has a combination of exactly these groups.
    """
    with _write_transaction(conn):
        bundles = [group_service.load_group(conn, group_id) for group_id in group_ids]
        _check_aggregatable(bundles)
        if len({b.group.load_id for b in bundles}) > 1:
            raise AnalysisError(
                "invalid_mixed_load",
                "groups must all use the same load to be saved as one combination",
            )
        wanted = set(group_ids)
        for existing in repo_aggregates.list_aggregates(conn, bundles[0].group.load_id):
            if set(repo_aggregates.member_group_ids(conn, existing.id)) == wanted:
                raise AnalysisError(
                    "duplicate_aggregate",
                    f'the saved combination "{existing.name}" already holds exactly these groups',
                )
        row = repo_aggregates.create_aggregate(
            conn,
            name=name,
            rifle_id=bundles[0].rifle.id,
            load_id=bundles[0].group.load_id,
            distance_yd=bundles[0].group.distance_yd,
            group_ids=sorted(group_ids),
        )
    return _evaluate(conn, row)


def list_saved(conn: sqlite3.Connection, load_id: int | None = None) -> list[SavedAggregate]:
    """Saved aggregates in creation order, optionally only those of one load."""
    return [_evaluate(conn, row) for row in repo_aggregates.list_aggregates(conn, load_id)]


def delete_saved(conn: sqlite3.Connection, aggregate_id: int) -> None:
    """Delete a saved aggregate; raises `AggregateNotFound`."""
    with _write_transaction(conn):
        if not repo_aggregates.delete_aggregate(conn, aggregate_id):
            raise AggregateNotFound(aggregate_id)


# ---------------------------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------------------------


def search_notes(conn: sqlite3.Connection, query: str) -> list[tuple[str, int, str]]:
    """Case-insensitive substring search of group notes and the legacy notes kept on migrated
    groups; `%` and `_` match literally.

    Returns up to `SEARCH_LIMIT` `("group", id, snippet)` tuples, one per group.
    """
    return repo_groups.search_notes(conn, query, SEARCH_LIMIT)


# ---------------------------------------------------------------------------------------------
# Group page
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlotShot:
    """A shot circle in plot units (`PLOT_PX_PER_IN` per inch, y down)."""

    cx: float
    cy: float
    r: float
    css_class: str
    label: str


@dataclass(frozen=True)
class PlotPhoto:
    """The working image placed in plot units: the same frame as the shots (`PLOT_PX_PER_IN`)."""

    href: str
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class PlotCircle:
    """The spread circle in plot units. `diameter_in` is None when sizes beyond n and ES are hidden."""

    cx: float
    cy: float
    r: float
    diameter_in: float | None
    diameter_moa: float | None = None
    diameter_mil: float | None = None
    distance_yd: int | None = None


@dataclass(frozen=True)
class PlotHitter:
    """The "thing hitter" circle: centered on the aim point (the plot origin), reaching the
    farthest shot center. Sizes are at the group's distance. Exists only for an unmasked group
    with an aim point: its radius encodes the zero offset.
    """

    r: float
    diameter_in: float
    diameter_moa: float
    diameter_mil: float
    distance_yd: int


HITTER_BLOCKED = "The “Thing-hitter” circle needs an aim point and is not shown for zeroing groups."


@dataclass(frozen=True)
class PlotCorrection:
    """The turret correction for a group, offered only where Form allows one.

    `up_mil` and `right_mil` are the signed 0.1 mil amounts (`domain.zeroing.correction_for`, the
    zeroing flow's function). `corner` is the elbow of the dotted path from the mean point of
    impact to the aim point, in plot units.
    """

    up_mil: float
    right_mil: float
    text: str
    distance_yd: int
    corner: tuple[float, float]


CORRECTION_BLOCKED_NO_AIM = "Mark the aim point in the editor to see a correction."
CORRECTION_BLOCKED_ZERO_CHECK = (
    "A zero check is pass or fail against the dot; it never gives a correction. "
    "If it fails, go back to Step 2."
)
CORRECTION_BLOCKED_STEP1 = (
    "Step 1 only corrects from 1 to 3 shots; shoot a 10-shot group for Step 2."
)
CORRECTION_BLOCKED_TOO_FEW = "Form: don't correct off fewer than 10 shots; shoot a 10-shot group."


def correction_gate(purpose: str, shot_count: int, has_aim: bool) -> str | None:
    """None when a correction may be offered, else the fixed reason it is withheld.

    Offered for an unmasked group with an aim point and 10 or more shots, or a Step 1 group of 1
    to 3 shots. The reasons contain no number of mils and no direction.
    """
    purpose = str(purpose)
    if group_service.masks_poi(purpose, shot_count):
        if purpose == "zero_check":
            return CORRECTION_BLOCKED_ZERO_CHECK
        if purpose == "zero_step1":
            return CORRECTION_BLOCKED_STEP1
        return CORRECTION_BLOCKED_TOO_FEW
    if purpose == "general" and shot_count < STEP2_MIN_SHOTS:
        return CORRECTION_BLOCKED_TOO_FEW
    return None if has_aim else CORRECTION_BLOCKED_NO_AIM


def correction_text(up_mil: float, right_mil: float, distance_yd: int) -> str:
    """The correction in words, e.g. "adjust 0.3 mil down, 0.1 mil left"; names a non-100 yd distance."""
    parts = []
    if up_mil:
        parts.append(f"{abs(up_mil):.1f} mil {'up' if up_mil > 0 else 'down'}")
    if right_mil:
        parts.append(f"{abs(right_mil):.1f} mil {'right' if right_mil > 0 else 'left'}")
    body = "adjust " + ", ".join(parts) if parts else "no adjustment needed"
    if distance_yd != ZERO_DISTANCE_YD:
        return f"To center at {distance_yd} yd: {body}."
    return f"{body[0].upper()}{body[1:]}."


@dataclass(frozen=True)
class Plot:
    """The group plot: its box, the shot circles, and the optional layers.

    `aim` and `poi` (the mean point of impact, plot units) exist only for an unmasked group with an
    aim point; `photo` only when the photo's working image is on disk; `circle` needs two shots.
    """

    box: tuple[float, float, float, float]
    shots: list[PlotShot]
    aim: tuple[float, float] | None
    photo: PlotPhoto | None = None
    circle: PlotCircle | None = None
    poi: tuple[float, float] | None = None
    correction: PlotCorrection | None = None
    correction_blocked: str | None = None
    hitter: PlotHitter | None = None

    @property
    def hitter_blocked(self) -> str | None:
        """The fixed reason there is no thing hitter circle, or None when there is one."""
        return None if self.hitter else HITTER_BLOCKED

    @property
    def view_box(self) -> str:
        """The SVG viewBox attribute value."""
        x, y, w, h = self.box
        return f"{x:.1f} {y:.1f} {w:.1f} {h:.1f}"


@dataclass(frozen=True)
class GroupPage:
    """Everything the group page renders."""

    view: GroupDetail
    stats: GroupStats | SizeOnlyStats | None
    poi_text: str | None
    masked: bool
    plot: Plot
    legacy_session_notes: str

    @property
    def es_mil(self) -> float | None:
        """Extreme spread in mil at the group's distance, rounded to 0.01; None without stats."""
        if self.stats is None:
            return None
        return round(in_to_mil(self.stats.es_in, self.view.group.distance_yd), 2)


def _photo_layer(
    view: GroupDetail, origin: tuple[float, float], working_dir: Path | None
) -> PlotPhoto | None:
    frame, photo = view.frame, view.photo
    if frame is None or photo is None or working_dir is None:
        return None
    if not (Path(working_dir) / f"{photo.id}.png").is_file():
        return None
    scale = PLOT_PX_PER_IN
    return PlotPhoto(
        href=f"/api/photos/{photo.id}/image?variant=working",
        x=-origin[0] * scale,
        y=-origin[1] * scale,
        width=frame.width_px / frame.ppi * scale,
        height=frame.height_px / frame.ppi * scale,
    )


def _box(
    reach: float, photo: PlotPhoto | None, view: GroupDetail, origin: tuple[float, float]
) -> tuple[float, float, float, float]:
    x0 = y0 = -reach
    x1 = y1 = reach
    if photo is not None and view.roi is not None and view.frame is not None:
        margin = PLOT_PHOTO_MARGIN_IN * PLOT_PX_PER_IN
        rx0, ry0, rx1, ry1 = (v / view.frame.ppi * PLOT_PX_PER_IN for v in view.roi)
        x0 = min(x0, max(rx0 - margin - origin[0] * PLOT_PX_PER_IN, photo.x))
        y0 = min(y0, max(ry0 - margin - origin[1] * PLOT_PX_PER_IN, photo.y))
        x1 = max(x1, min(rx1 + margin - origin[0] * PLOT_PX_PER_IN, photo.x + photo.width))
        y1 = max(y1, min(ry1 + margin - origin[1] * PLOT_PX_PER_IN, photo.y + photo.height))
    return x0, y0, x1 - x0, y1 - y0


def _plot(
    view: GroupDetail,
    masked: bool,
    stats: GroupStats | SizeOnlyStats | None,
    working_dir: Path | None,
) -> Plot:
    group, shots = view.group, view.shots
    has_aim = group.aim_x_in is not None and group.aim_y_in is not None
    if has_aim and not masked:
        ox, oy = group.aim_x_in, group.aim_y_in
    elif shots:
        ox = sum(s.x_in for s in shots) / len(shots)
        oy = sum(s.y_in for s in shots) / len(shots)
    else:
        ox = oy = 0.0
    radius_in = view.bullet_diameter_in / 2
    drawn = [
        PlotShot(
            cx=(s.x_in - ox) * PLOT_PX_PER_IN,
            cy=(s.y_in - oy) * PLOT_PX_PER_IN,
            r=radius_in * PLOT_PX_PER_IN,
            css_class=f"shot {s.origin}",
            label=str(s.seq),
        )
        for s in shots
    ]
    reach = max(
        [PLOT_MIN_HALF_IN * PLOT_PX_PER_IN]
        + [max(abs(p.cx), abs(p.cy)) + p.r + PLOT_MARGIN_IN * PLOT_PX_PER_IN for p in drawn]
    )
    aim = (0.0, 0.0) if has_aim and not masked else None
    hitter = None
    if aim is not None and shots:
        about = circle_about((ox, oy), [(s.x_in, s.y_in) for s in shots])
        distance = view.group.distance_yd
        hitter = PlotHitter(
            r=about.r * PLOT_PX_PER_IN,
            diameter_in=2 * about.r,
            diameter_moa=in_to_moa(2 * about.r, distance),
            diameter_mil=in_to_mil(2 * about.r, distance),
            distance_yd=distance,
        )
        reach = max(reach, hitter.r + PLOT_MARGIN_IN * PLOT_PX_PER_IN)
    photo = _photo_layer(view, (ox, oy), working_dir)
    circle = None
    if len(shots) >= 2:
        enclosing = smallest_enclosing_circle([(s.x_in, s.y_in) for s in shots])
        circle = PlotCircle(
            cx=(enclosing.cx - ox) * PLOT_PX_PER_IN,
            cy=(enclosing.cy - oy) * PLOT_PX_PER_IN,
            r=enclosing.r * PLOT_PX_PER_IN,
            diameter_in=None if masked else 2 * enclosing.r,
            diameter_moa=None if masked else in_to_moa(2 * enclosing.r, view.group.distance_yd),
            diameter_mil=None if masked else in_to_mil(2 * enclosing.r, view.group.distance_yd),
            distance_yd=view.group.distance_yd,
        )
    poi = None
    if aim is not None and isinstance(stats, GroupStats):
        poi = (stats.mean_dx_in * PLOT_PX_PER_IN, -stats.mean_dy_in * PLOT_PX_PER_IN)
    blocked = correction_gate(group.purpose, len(shots), aim is not None)
    correction = None
    if blocked is None and poi is not None and isinstance(stats, GroupStats):
        amount = correction_for(stats.mean_dx_mil, stats.mean_dy_mil)
        correction = PlotCorrection(
            up_mil=amount.up_mil,
            right_mil=amount.right_mil,
            text=correction_text(amount.up_mil, amount.right_mil, view.group.distance_yd),
            distance_yd=view.group.distance_yd,
            corner=(poi[0], 0.0),
        )
    return Plot(
        box=_box(reach, photo, view, (ox, oy)),
        shots=drawn,
        aim=aim,
        photo=photo,
        circle=circle,
        poi=poi,
        correction=correction,
        correction_blocked=None if correction else (blocked or CORRECTION_BLOCKED_NO_AIM),
        hitter=hitter,
    )


def group_page(
    conn: sqlite3.Connection, group_id: int, working_dir: Path | None = None
) -> GroupPage:
    """Assemble the group page: detail, statistics (masked where required), plot and notes.

    The plot carries the photo layer only when `working_dir` holds the group's working image.
    Raises `GroupNotFound`.
    """
    view = group_detail(conn, group_id)
    stats = group_service.group_stats(conn, group_id)
    masked = group_service.masks_poi(view.group.purpose, len(view.shots))
    poi = (
        direction_text(stats.mean_dx_mil, stats.mean_dy_mil)
        if isinstance(stats, GroupStats)
        else None
    )
    plot = _plot(view, masked, stats, working_dir)
    return GroupPage(view, stats, poi, masked, plot, view.group.legacy_session_notes)
