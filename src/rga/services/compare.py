"""Load comparison service: like-with-like tiers, verdicts and the POI shift against the zero load.

Reads only. Eligible data is finalized groups of the rifle, optionally filtered by position and
suppressor use. A group whose point of impact is masked (`groups.masks_poi`: any
zero_check group, a zero_step2 or zero_confirm group under 10 shots) never enters a tier, a pair
or the list, so no masked POI can reach a comparison (B-20). Groups of fewer than 10 shots stay
in the list with no tier but are never compared and never enter a POI shift. Errors are
`CompareError` subclasses with a stable snake_case `code` (`*_not_found` maps to 404, `invalid_*`
to 422, the rest to 409); `RifleError` is re-exported for the route.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from typing import cast

from rga.db import repo_groups, repo_rifles
from rga.db.rows import LoadRow, RifleRow
from rga.domain.comparison import (
    MIN_ROUNDS,
    ZERO_TO_HUNTING_LOAD_ADVICE,
    CompareGroup,
    LoadSummary,
    PairVerdict,
    Tier,
    compare_pair,
    conditions_warning,
    poi_shift_mil,
    summarize_load,
    tier_for,
)
from rga.domain.stats import AggregateInput, aggregate
from rga.domain.types import GroupStats, Offset, Position, ShotOrigin
from rga.services.analysis import SavedAggregate, direction_text, list_saved
from rga.services.groups import load_group, masks_poi
from rga.services.rifles import RifleError, RifleNotFound

SQLITE_MAX_INTEGER = 2**63 - 1
NO_SHARED_BUCKET_NOTE = (
    "shoot both loads on the same day, distance, position and suppressor setting "
    "to measure POI shift"
)
NO_ZERO_LOAD_NOTE = "set the rifle's zero load to measure POI shift"
IS_ZERO_LOAD_NOTE = "this is the rifle's zero load"
MIXED_CONDITIONS_NOTE = (
    "its groups were shot in different positions or with different suppressor use; "
    "save a combination of one setup to use it here"
)
OVERLAPS_AGGREGATE_NOTE = "overlaps another saved combination"
OVERLAPS_GROUP_NOTE = "it contains a group of 30 or more shots, which already counts on its own"

__all__ = [
    "IS_ZERO_LOAD_NOTE",
    "NO_SHARED_BUCKET_NOTE",
    "NO_ZERO_LOAD_NOTE",
    "CompareAggregateInfo",
    "CompareError",
    "CompareGroupInfo",
    "Comparison",
    "LoadComparison",
    "LoadNotFound",
    "LoadNotInRifle",
    "PairResult",
    "PoiShift",
    "RifleError",
    "RifleNotFound",
    "compare",
]


class CompareError(Exception):
    """Base error with a stable snake_case `code` and a user-facing message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LoadNotFound(CompareError):
    """A requested load does not exist."""

    def __init__(self, load_id: int) -> None:
        super().__init__("load_not_found", f"load {load_id} does not exist")


class LoadNotInRifle(CompareError):
    """A requested load belongs to a different rifle."""

    def __init__(self, rifle_id: int, load_id: int) -> None:
        super().__init__("load_not_in_rifle", f"load {load_id} is not a load of rifle {rifle_id}")


@dataclass(frozen=True)
class CompareGroupInfo:
    """A group listed under its load; `tier` is None for sizes in no tier."""

    group_id: int
    shot_date: str
    shot_time: str | None
    distance_yd: int
    position: Position
    suppressor: bool
    shots: int
    es_moa: float
    includes_estimated: bool
    tier: Tier | None


@dataclass(frozen=True)
class CompareAggregateInfo:
    """A saved aggregate of the load; `counted` says whether it is a tier30 entry, else `note`
    says why not (too few shots, no longer valid, mixed conditions, overlap with a big group).
    """

    aggregate_id: int
    name: str
    group_ids: list[int]
    distance_yd: int
    shots: int
    es_moa: float
    includes_estimated: bool
    tier: Tier | None
    counted: bool
    note: str | None


@dataclass(frozen=True)
class PoiShift:
    """Mean POI shift of a load against the zero load, in mil (right and up positive)."""

    right_mil: float
    up_mil: float
    days: int
    text: str


@dataclass(frozen=True)
class LoadComparison:
    """One load's tier summaries, status, verdict, POI shift (or why there is none) and groups."""

    load: LoadRow
    is_zero_load: bool
    summary: LoadSummary
    poi_shift: PoiShift | None
    poi_shift_note: str | None
    groups: list[CompareGroupInfo]
    aggregates: list[CompareAggregateInfo]


@dataclass(frozen=True)
class PairResult:
    """Verdicts for loads A and B on each tier."""

    a_load_id: int
    b_load_id: int
    tier10: PairVerdict
    tier30: PairVerdict


@dataclass(frozen=True)
class Comparison:
    """The whole comparison: filters, loads, pair verdicts, conditions warning and advice."""

    rifle: RifleRow
    position: Position | None
    suppressor: bool | None
    loads: list[LoadComparison]
    pairs: list[PairResult]
    conditions_warning: str | None
    advice: str


@dataclass(frozen=True)
class _Eligible:
    info: CompareGroupInfo
    compare: CompareGroup
    offsets: list[Offset]
    origins: list[ShotOrigin]


def _require_load(conn: sqlite3.Connection, rifle_id: int, load_id: int) -> LoadRow:
    load = repo_rifles.get_load(conn, load_id) if load_id <= SQLITE_MAX_INTEGER else None
    if load is None:
        raise LoadNotFound(load_id)
    if load.rifle_id != rifle_id:
        raise LoadNotInRifle(rifle_id, load_id)
    return load


def _eligible_groups(
    conn: sqlite3.Connection,
    load_id: int,
    position: Position | None,
    suppressor: bool | None,
) -> list[_Eligible]:
    """Finalized, unmasked groups of the load that pass the filters, in creation order."""
    found = []
    for group in repo_groups.list_groups(conn, load_id=load_id, status="final"):
        if position is not None and group.position != position.value:
            continue
        if suppressor is not None and bool(group.suppressor) != suppressor:
            continue
        bundle = load_group(conn, group.id)
        shots = bundle.shots
        if masks_poi(group.purpose, len(shots)):
            continue
        stats = cast(GroupStats, bundle.stats)
        offsets = [Offset(cast(float, s.dx_in), cast(float, s.dy_in)) for s in shots]
        info = CompareGroupInfo(
            group_id=group.id,
            shot_date=group.shot_date,
            shot_time=group.shot_time,
            distance_yd=group.distance_yd,
            position=Position(group.position),
            suppressor=bool(group.suppressor),
            shots=stats.n,
            es_moa=stats.es_moa,
            includes_estimated=stats.includes_estimated,
            tier=tier_for(stats.n),
        )
        found.append(
            _Eligible(
                info=info,
                compare=CompareGroup(
                    es_moa=stats.es_moa,
                    n=stats.n,
                    position=info.position,
                    suppressor=info.suppressor,
                    includes_estimated=stats.includes_estimated,
                ),
                offsets=offsets,
                origins=[ShotOrigin(s.origin) for s in shots],
            )
        )
    return found


def _aggregate_entry(
    saved: SavedAggregate,
    position: Position | None,
    suppressor: bool | None,
    claimed: set[int],
) -> tuple[CompareAggregateInfo, CompareGroup | None] | None:
    """The listing of a saved aggregate and its tier30 entry, or None when the filters exclude it.

    An aggregate counts only when valid, of 30 or more shots, shot in one position with one
    suppressor setting, free of member groups that are tier30 groups themselves, and sharing no
    group with an aggregate that already counted. `claimed` holds the member group ids of the
    aggregates counted so far for this load (creation order) and is extended when this one counts,
    so every group counts at most once per load.
    """
    if position is not None and saved.positions != {position.value}:
        return None
    if suppressor is not None and saved.suppressors != {suppressor}:
        return None
    stats = saved.result.stats
    note: str | None = None
    if not saved.valid:
        note = f"no longer valid: {saved.invalid_reason}"
    elif len(saved.positions) > 1 or len(saved.suppressors) > 1:
        note = MIXED_CONDITIONS_NOTE
    elif saved.tier != "tier30":
        note = f"needs 30 or more shots to count here; it has {stats.n}"
    else:
        per_group = Counter(shot.group_id for shot in saved.result.shots)
        if any(tier_for(count) == "tier30" for count in per_group.values()):
            note = OVERLAPS_GROUP_NOTE
        elif claimed & set(saved.member_ids):
            note = OVERLAPS_AGGREGATE_NOTE
    counted = note is None
    if counted:
        claimed.update(saved.member_ids)
    info = CompareAggregateInfo(
        aggregate_id=saved.row.id,
        name=saved.row.name,
        group_ids=saved.member_ids,
        distance_yd=saved.row.distance_yd,
        shots=stats.n,
        es_moa=stats.es_moa,
        includes_estimated=stats.includes_estimated,
        tier=saved.tier,
        counted=counted,
        note=note,
    )
    entry = (
        CompareGroup(
            es_moa=stats.es_moa,
            n=stats.n,
            position=Position(next(iter(saved.positions))),
            suppressor=next(iter(saved.suppressors)),
            includes_estimated=stats.includes_estimated,
        )
        if counted
        else None
    )
    return info, entry


BucketKey = tuple[str, int, str, bool]


def _bucket_centers(groups: list[_Eligible]) -> dict[BucketKey, tuple[float, float]]:
    """Pooled mean POI in mil per (shot date, distance, position, suppressor) bucket.

    Only groups of at least MIN_ROUNDS shots count.
    """
    by_bucket: dict[BucketKey, list[_Eligible]] = {}
    for item in groups:
        if item.info.shots >= MIN_ROUNDS:
            info = item.info
            key = (info.shot_date, info.distance_yd, info.position.value, info.suppressor)
            by_bucket.setdefault(key, []).append(item)
    centers = {}
    for key, items in by_bucket.items():
        pooled = aggregate(
            [AggregateInput(i.offsets, i.origins, i.info.distance_yd) for i in items]
        )
        centers[key] = (pooled.center_dx_mil, pooled.center_dy_mil)
    return centers


def _poi_shift(
    load_groups: list[_Eligible], zero_groups: list[_Eligible]
) -> tuple[PoiShift | None, str | None]:
    load_centers, zero_centers = _bucket_centers(load_groups), _bucket_centers(zero_groups)
    shared = sorted(load_centers.keys() & zero_centers.keys())
    shift = poi_shift_mil([(load_centers[k], zero_centers[k]) for k in shared])
    if shift is None:
        return None, NO_SHARED_BUCKET_NOTE
    right, up = shift
    where = direction_text(right, up)
    subject = (
        "the same point of impact as the zero load"
        if where == "on the aim point"
        else f"{where} of the zero load"
    )
    days = len({key[0] for key in shared})
    plural = "s" if days != 1 else ""
    text = f"{subject} (mean of {days} shared day{plural})"
    return PoiShift(right_mil=right, up_mil=up, days=days, text=text), None


def compare(
    conn: sqlite3.Connection,
    rifle_id: int,
    load_ids: list[int],
    position: Position | None = None,
    suppressor: bool | None = None,
) -> Comparison:
    """Compare `load_ids` of the rifle on their finalized, unmasked groups.

    Each load gets tier10 and tier30 summaries (P-13), a status and precision verdict from tier10,
    and a POI shift against the rifle's zero load from shared buckets only (same day, distance,
    position and suppressor setting); every unordered
    pair of loads gets a verdict per tier. Raises `RifleNotFound`, `LoadNotFound`, or
    `LoadNotInRifle`.
    """
    rifle = repo_rifles.get_rifle(conn, rifle_id) if rifle_id <= SQLITE_MAX_INTEGER else None
    if rifle is None:
        raise RifleNotFound(rifle_id)
    loads = [_require_load(conn, rifle_id, load_id) for load_id in load_ids]
    zero_groups = (
        _eligible_groups(conn, rifle.zero_load_id, position, suppressor)
        if rifle.zero_load_id is not None
        else []
    )
    results: list[LoadComparison] = []
    pooled_by_load: dict[int, list[CompareGroup]] = {}
    for load in loads:
        eligible = _eligible_groups(conn, load.id, position, suppressor)
        is_zero = load.id == rifle.zero_load_id
        if is_zero:
            shift, note = None, IS_ZERO_LOAD_NOTE
        elif rifle.zero_load_id is None:
            shift, note = None, NO_ZERO_LOAD_NOTE
        else:
            shift, note = _poi_shift(eligible, zero_groups)
        claimed: set[int] = set()
        entries = [
            entry
            for saved in list_saved(conn, load.id)
            if (entry := _aggregate_entry(saved, position, suppressor, claimed)) is not None
        ]
        pooled = [group for _, group in entries if group is not None]
        results.append(
            LoadComparison(
                load=load,
                is_zero_load=is_zero,
                summary=summarize_load(load.id, [e.compare for e in eligible] + pooled),
                poi_shift=shift,
                poi_shift_note=note,
                groups=[e.info for e in eligible],
                aggregates=[info for info, _ in entries],
            )
        )
        pooled_by_load[load.id] = pooled
    pairs = [
        PairResult(
            a_load_id=a.load.id,
            b_load_id=b.load.id,
            tier10=compare_pair(a.summary, b.summary, "tier10"),
            tier30=compare_pair(a.summary, b.summary, "tier30"),
        )
        for a, b in combinations(results, 2)
    ]
    tiered = {
        r.load.id: [
            CompareGroup(g.es_moa, g.shots, g.position, g.suppressor, g.includes_estimated)
            for g in r.groups
            if g.tier is not None
        ]
        + pooled_by_load[r.load.id]
        for r in results
    }
    warning = next(
        (
            w
            for a, b in combinations(results, 2)
            if (w := conditions_warning(tiered[a.load.id], tiered[b.load.id])) is not None
        ),
        None,
    )
    return Comparison(
        rifle=rifle,
        position=position,
        suppressor=suppressor,
        loads=results,
        pairs=pairs,
        conditions_warning=warning,
        advice=ZERO_TO_HUNTING_LOAD_ADVICE,
    )
