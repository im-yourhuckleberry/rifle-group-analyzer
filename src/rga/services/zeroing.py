"""Zeroing service: the Step 1/2/3 state machine, judged advice, the dot and the zero check.

A zero walks `step1 -> step2_initial -> step2_confirm -> step3_dot -> complete` (an `existing`
zero starts at `step2_initial`; completing a zero supersedes the earlier one for the rifle and
load). Linked groups are judged by the domain rules from their stored plane positions and aim, at
100 yd only. Advice that asks for more shots or is invalid never carries a correction. Public
functions that write commit; errors are `ZeroingError` (a `GroupError` with a stable `code`).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from rga.db import repo_groups, repo_rifles, repo_zeroing
from rga.db.repo_groups import GroupLinkMismatch
from rga.db.repo_rifles import now_iso
from rga.db.repo_zeroing import GroupNotLinkable, LinkedGroup, ZeroListing, ZeroTargetMismatch
from rga.db.rows import GroupRow, ZeroCheckRow, ZeroRow
from rga.domain.stats import group_stats as compute_group_stats
from rga.domain.types import (
    Advice,
    AdviceKind,
    Correction,
    GroupPurpose,
    GroupStats,
    Offset,
    PlanePoint,
    Position,
    ShotOrigin,
    ZeroCheckResult,
    ZeroState,
)
from rga.domain.units import ZERO_DISTANCE_YD, in_to_moa, offset_from_aim
from rga.domain.zeroing import (
    ZERO_CHECK_LOST_ZERO_MESSAGE,
    ZERO_CHECK_OK_MESSAGE,
    step1_advice,
    step2_confirm_advice,
    step2_initial_advice,
    suggest_dot,
    zero_check,
)
from rga.services.groups import (
    GroupError,
    SizeOnlyStats,
    _recompute_group,
    _write_transaction,
    resolve_shot_time,
)

SQLITE_MAX_INTEGER = 2**63 - 1
MODES = ("new_setup", "existing")
START_STATE = {"new_setup": ZeroState.STEP1.value, "existing": ZeroState.STEP2_INITIAL.value}
STEP_PURPOSE = {
    ZeroState.STEP1.value: GroupPurpose.ZERO_STEP1.value,
    ZeroState.STEP2_INITIAL.value: GroupPurpose.ZERO_STEP2.value,
    ZeroState.STEP2_CONFIRM.value: GroupPurpose.ZERO_CONFIRM.value,
    ZeroState.STEP3_DOT.value: GroupPurpose.ZERO_CONFIRM.value,
}
STEP_PURPOSES = frozenset(STEP_PURPOSE.values())
OPEN_STATES = frozenset(STEP_PURPOSE)
CONFIRM_STATES = frozenset({"step2_confirm", "step3_dot", "complete", "superseded"})
READY_KINDS = frozenset({AdviceKind.READY_FOR_DOT, AdviceKind.TOUCH_UP_OK})
CORRECTION_KINDS = frozenset({AdviceKind.CORRECTION, AdviceKind.NO_ADJUSTMENT})
ZERO_CHECK_PURPOSE = GroupPurpose.ZERO_CHECK.value
MAX_CHECK_SHOTS = 2
GENERAL_PURPOSE = GroupPurpose.GENERAL.value
CHECK_MESSAGES = {"pass": ZERO_CHECK_OK_MESSAGE, "possible_lost_zero": ZERO_CHECK_LOST_ZERO_MESSAGE}
LOST_ZERO_ROUTE = (
    "Check the scope mount first (the #1 cause).",
    "Then check the scope itself (the #2 cause).",
    "Then go back to Step 2: start a new zero for this rifle and load as an existing setup.",
)
STEP1_MAX_SHOTS = 3
STEP1_SHOTS_MESSAGE = (
    "Step 1 only takes a group of 1 to 3 shots. For a bigger group use Step 2, which needs "
    "10 shots: start the zero as an existing setup."
)
DISTANCE_MESSAGE = (
    "Zeroing groups must be shot at 100 yd: a group of this zero was shot at another "
    "distance, so it cannot be judged."
)
AIM_MISSING_MESSAGE = "Mark the aim point on every zeroing group before it can be judged."


class ZeroingError(GroupError):
    """A zeroing rule refused the request; `code` is stable and snake_case.

    Codes ending in `_not_found` map to 404, codes starting `invalid_` to 422, the rest to 409.
    """


@dataclass(frozen=True)
class AdviceView:
    """Domain advice plus what the web layer needs to render it.

    `poi_text` is the direction text of the mean POI (only for correction kinds), and
    `group_size` the size-only stats of the judged group (for advice that carries none).
    """

    advice: Advice
    poi_text: str
    group_size: SizeOnlyStats | None


@dataclass(frozen=True)
class CheckView:
    """A stored zero check with its recomputed domain result (None if it cannot be recomputed)."""

    check: ZeroCheckRow
    result: ZeroCheckResult | None


@dataclass(frozen=True)
class ZeroView:
    """A zero with its linked groups, checks, latest advice and the suggested dot."""

    zero: ZeroRow
    groups: list[LinkedGroup]
    checks: list[CheckView]
    advice: AdviceView | None
    suggested_dot_in: float | None


@dataclass(frozen=True)
class _Inputs:
    group: GroupRow | None
    offsets: list[Offset]
    origins: list[ShotOrigin]
    at_zero_distance: bool = True


_NO_GROUP = _Inputs(None, [], [])


# --------------------------------------------------------------------------- loading


def _fits_sqlite(value: int) -> bool:
    return 1 <= value <= SQLITE_MAX_INTEGER


def require_zero(conn: sqlite3.Connection, zero_id: int) -> ZeroRow:
    """The zero with `zero_id`; raises `ZeroingError` `zero_not_found`."""
    zero = repo_zeroing.get_zero(conn, zero_id) if _fits_sqlite(zero_id) else None
    if zero is None:
        raise ZeroingError("zero_not_found", f"zero {zero_id} does not exist")
    return zero


def _require_group(conn: sqlite3.Connection, group_id: int) -> GroupRow:
    group = repo_groups.get_group(conn, group_id) if _fits_sqlite(group_id) else None
    if group is None:
        raise ZeroingError("group_not_found", f"group {group_id} does not exist")
    return group


def _inputs(conn: sqlite3.Connection, group_id: int) -> _Inputs | None:
    """Aim-relative offsets (inches, y up) and origins from stored plane positions; None w/o aim."""
    group = _require_group(conn, group_id)
    if group.aim_x_in is None or group.aim_y_in is None:
        return None
    aim = PlanePoint(group.aim_x_in, group.aim_y_in)
    shots = repo_groups.list_shots(conn, group_id)
    return _Inputs(
        group,
        [offset_from_aim(PlanePoint(s.x_in, s.y_in), aim) for s in shots],
        [ShotOrigin(s.origin) for s in shots],
        group.distance_yd == ZERO_DISTANCE_YD,
    )


def _size_only(inputs: _Inputs) -> SizeOnlyStats | None:
    if not inputs.offsets:
        return None
    stats = compute_group_stats(inputs.offsets, inputs.origins, ZERO_DISTANCE_YD)
    return SizeOnlyStats(
        n=stats.n,
        es_in=stats.es_in,
        es_moa=in_to_moa(stats.es_in, ZERO_DISTANCE_YD),
        includes_estimated=stats.includes_estimated,
    )


def poi_text(stats: GroupStats) -> str:
    """Direction words for a mean POI, e.g. "0.3 mil high, 0.2 mil right"."""
    parts = []
    for mil, positive, negative in (
        (stats.mean_dy_mil, "high", "low"),
        (stats.mean_dx_mil, "right", "left"),
    ):
        if round(mil, 1) != 0.0:
            parts.append(f"{abs(mil):.1f} mil {positive if mil > 0 else negative}")
    return ", ".join(parts) if parts else "centered on the aim point"


def _advice_view(
    advice: Advice, judged: Sequence[_Inputs], single: _Inputs | None = None
) -> AdviceView:
    text = poi_text(advice.stats) if advice.kind in CORRECTION_KINDS and advice.stats else ""
    size = None
    if advice.kind in (AdviceKind.SHOOT_MORE, AdviceKind.INVALID):
        latest = single or (judged[-1] if judged else None)
        size = _size_only(latest) if latest is not None else None
    return AdviceView(advice, text, size)


def _aim_missing() -> AdviceView:
    return AdviceView(Advice(AdviceKind.INVALID, AIM_MISSING_MESSAGE), "", None)


def _wrong_distance() -> AdviceView:
    return AdviceView(Advice(AdviceKind.INVALID, DISTANCE_MESSAGE), "", None)


def _distance_problem(view: ZeroView) -> bool:
    return view.advice is not None and view.advice.advice.message == DISTANCE_MESSAGE


def _judge_single(
    conn: sqlite3.Connection,
    linked: Sequence[LinkedGroup],
    purpose: str,
    rule: Callable[[list[Offset], list[ShotOrigin]], Advice],
) -> AdviceView | None:
    groups = [g for g in linked if g.purpose == purpose]
    if not groups:
        return None
    inputs = _inputs(conn, groups[-1].group_id)
    if inputs is None:
        return _aim_missing()
    if not inputs.at_zero_distance:
        return _wrong_distance()
    return _advice_view(rule(inputs.offsets, inputs.origins), [inputs])


def _confirm_groups(linked: Sequence[LinkedGroup]) -> tuple[LinkedGroup | None, list[LinkedGroup]]:
    """The latest Step 2 group and the confirm groups shot after it, oldest first."""
    initials = [g for g in linked if g.purpose == GroupPurpose.ZERO_STEP2.value]
    initial = initials[-1] if initials else None
    confirms = [
        g
        for g in linked
        if g.purpose == GroupPurpose.ZERO_CONFIRM.value
        and (initial is None or g.group_id > initial.group_id)
    ]
    return initial, confirms


def _judge_confirm(
    conn: sqlite3.Connection, zero: ZeroRow, linked: Sequence[LinkedGroup]
) -> AdviceView | None:
    initial, confirms = _confirm_groups(linked)
    if initial is None and not confirms:
        return None
    initial_inputs = _inputs(conn, initial.group_id) if initial else _NO_GROUP
    confirm_inputs = [_inputs(conn, g.group_id) for g in confirms]
    present = [c for c in confirm_inputs if c is not None]
    if initial_inputs is None or len(present) != len(confirm_inputs):
        return _aim_missing()
    judged = [initial_inputs, *present]
    if not all(g.at_zero_distance for g in judged):
        return _wrong_distance()
    dialed = Correction(zero.dialed_up_mil or 0.0, zero.dialed_right_mil or 0.0, "")
    advice = step2_confirm_advice(
        initial_inputs.offsets,
        dialed,
        [c.offsets for c in present],
        [o for g in judged for o in g.origins],
    )
    return _advice_view(advice, judged)


def _judge(
    conn: sqlite3.Connection, zero: ZeroRow, linked: Sequence[LinkedGroup]
) -> AdviceView | None:
    if zero.state == ZeroState.STEP1.value:
        return _judge_single(conn, linked, GroupPurpose.ZERO_STEP1.value, step1_advice)
    if zero.state == ZeroState.STEP2_INITIAL.value:
        return _judge_single(conn, linked, GroupPurpose.ZERO_STEP2.value, step2_initial_advice)
    return _judge_confirm(conn, zero, linked)


def _view(conn: sqlite3.Connection, zero: ZeroRow) -> ZeroView:
    linked = repo_zeroing.list_linked_groups(conn, zero.id)
    advice = _judge(conn, zero, linked)
    rifle = repo_rifles.get_rifle(conn, zero.rifle_id)
    suggested = None
    if (
        rifle is not None
        and zero.zero_es_in is not None
        and zero.state in (ZeroState.STEP3_DOT.value, ZeroState.COMPLETE.value)
    ):
        suggested = suggest_dot(zero.zero_es_in, rifle.bullet_diameter_in)
    checks = [
        CheckView(row, _recheck(conn, zero, row))
        for row in repo_zeroing.list_zero_checks(conn, zero.id)
    ]
    return ZeroView(zero, linked, checks, advice, suggested)


def _recheck(
    conn: sqlite3.Connection, zero: ZeroRow, check: ZeroCheckRow
) -> ZeroCheckResult | None:
    inputs = _inputs(conn, check.group_id)
    rifle = repo_rifles.get_rifle(conn, zero.rifle_id)
    if inputs is None or rifle is None or zero.dot_size_in is None:
        return None
    try:
        return zero_check(inputs.offsets, zero.dot_size_in, rifle.bullet_diameter_in)
    except ValueError:
        return None


# --------------------------------------------------------------------------- state sync


def _target(
    zero: ZeroRow, advice: AdviceView | None
) -> tuple[str, float | None, int | None] | None:
    """State, zero ES and shot count the confirm-stage zero should hold, or None if not applicable."""
    if zero.state not in (ZeroState.STEP2_CONFIRM.value, ZeroState.STEP3_DOT.value):
        return None
    if advice and advice.advice.kind in READY_KINDS and advice.advice.stats is not None:
        stats = advice.advice.stats
        return ZeroState.STEP3_DOT.value, stats.es_in, stats.n
    return ZeroState.STEP2_CONFIRM.value, None, None


def _sync_needed(zero: ZeroRow, advice: AdviceView | None) -> bool:
    target = _target(zero, advice)
    return target is not None and target != (zero.state, zero.zero_es_in, zero.zero_shot_count)


def _sync(conn: sqlite3.Connection, zero_id: int) -> ZeroRow:
    """Bring the stored state and zero ES in line with the judged confirm groups (write lock held)."""
    zero = require_zero(conn, zero_id)
    linked = repo_zeroing.list_linked_groups(conn, zero.id)
    target = _target(zero, _judge(conn, zero, linked))
    if target is not None and target != (zero.state, zero.zero_es_in, zero.zero_shot_count):
        state, es_in, shot_count = target
        return _updated(
            repo_zeroing.update_zero(
                conn, zero.id, state=state, zero_es_in=es_in, zero_shot_count=shot_count
            ),
            zero.id,
        )
    return zero


def _updated(row: ZeroRow | None, zero_id: int) -> ZeroRow:
    if row is None:
        raise ZeroingError("zero_not_found", f"zero {zero_id} does not exist")
    return row


def get_zero(conn: sqlite3.Connection, zero_id: int) -> ZeroView:
    """The zero's view. The confirm-stage state follows the judged groups, so a stale state
    (a confirm group finalized or edited elsewhere) is corrected and committed first.
    Raises `ZeroingError` `zero_not_found`.
    """
    zero = require_zero(conn, zero_id)
    linked = repo_zeroing.list_linked_groups(conn, zero.id)
    if _sync_needed(zero, _judge(conn, zero, linked)):
        with _write_transaction(conn):
            _sync(conn, zero_id)
        zero = require_zero(conn, zero_id)
    return _view(conn, zero)


# --------------------------------------------------------------------------- commands


def create_zero(
    conn: sqlite3.Connection,
    rifle_id: int,
    load_id: int,
    mode: str,
    lot_code: str | None = None,
) -> ZeroView:
    """Start a zero: `new_setup` at Step 1, `existing` at Step 2 (initial group).

    `lot_code` (get-or-create under the load) ties the zero to a lot. Raises `ZeroingError`
    `rifle_not_found`, `load_not_found`, `invalid_mode`, or `zero_target_mismatch` (the load is
    not a load of the rifle).
    """
    if mode not in MODES:
        raise ZeroingError("invalid_mode", f"mode must be one of {', '.join(MODES)}")
    with _write_transaction(conn):
        if not _fits_sqlite(rifle_id) or repo_rifles.get_rifle(conn, rifle_id) is None:
            raise ZeroingError("rifle_not_found", f"rifle {rifle_id} does not exist")
        load = repo_rifles.get_load(conn, load_id) if _fits_sqlite(load_id) else None
        if load is None:
            raise ZeroingError("load_not_found", f"load {load_id} does not exist")
        lot_id = repo_rifles.get_or_create_lot(conn, load_id, lot_code).id if lot_code else None
        try:
            zero = repo_zeroing.create_zero(
                conn,
                rifle_id=rifle_id,
                load_id=load_id,
                lot_id=lot_id,
                mode=mode,
                state=START_STATE[mode],
            )
        except ZeroTargetMismatch as exc:
            raise ZeroingError("zero_target_mismatch", str(exc)) from exc
    return _view(conn, zero)


def list_zeros_for_rifle(conn: sqlite3.Connection, rifle_id: int) -> list[ZeroListing]:
    """The rifle's zeros, newest first, with load label and lot code; no advice or correction.

    An unknown or out-of-range rifle id gives an empty list.
    """
    if not _fits_sqlite(rifle_id):
        return []
    return repo_zeroing.list_zeros_for_rifle(conn, rifle_id)


def _require_open(zero: ZeroRow) -> None:
    if zero.state not in OPEN_STATES:
        raise ZeroingError(
            "zero_state", f"this zero is {zero.state}: its steps can no longer be changed"
        )


def _require_100_yd(group: GroupRow) -> None:
    if group.distance_yd != ZERO_DISTANCE_YD:
        raise ZeroingError("invalid_distance", DISTANCE_MESSAGE)


def _link(conn: sqlite3.Connection, zero: ZeroRow, group: GroupRow, purpose: str) -> None:
    try:
        repo_zeroing.link_group(conn, zero.id, group.id)
    except GroupNotLinkable as exc:
        raise ZeroingError("group_not_linkable", str(exc)) from exc
    repo_groups.update_group(conn, group.id, purpose=purpose)
    _recompute_group(conn, group.id)


def link_group(conn: sqlite3.Connection, zero_id: int, group_id: int) -> ZeroView:
    """Attach a group as the next group of the zero's current step and judge it.

    The group takes the step's purpose (Step 1, Step 2 initial, or confirm), its offsets are
    recomputed (short Step 2 groups lose theirs), and a ready confirm group moves the zero to
    Step 3. Linking again is a no-op. Raises `ZeroingError`: `zero_not_found`, `group_not_found`,
    `zero_state` (complete or superseded), `invalid_distance` (the group was not shot at 100 yd),
    `invalid_step1_shots` (a Step 1 group over 3 shots),
    `group_linked_elsewhere`, `group_is_zero_check`, `group_out_of_order` (older than a group
    already linked, since the newest linked group of a step is the one judged),
    `group_not_linkable` (other rifle, load or lot).
    """
    with _write_transaction(conn):
        zero = require_zero(conn, zero_id)
        _require_open(zero)
        group = _require_group(conn, group_id)
        if group.zero_id not in (None, zero.id):
            raise ZeroingError(
                "group_linked_elsewhere", f"group {group_id} belongs to another zero"
            )
        if group.purpose == ZERO_CHECK_PURPOSE:
            raise ZeroingError("group_is_zero_check", "a zero-check group is not a zeroing step")
        _require_100_yd(group)
        if group.zero_id == zero.id and group.purpose in STEP_PURPOSES:
            return _view(conn, _sync(conn, zero_id))
        linked = repo_zeroing.list_linked_groups(conn, zero.id)
        if any(g.group_id > group.id for g in linked if g.purpose in STEP_PURPOSES):
            raise ZeroingError(
                "group_out_of_order",
                "link groups in the order they were shot: this group is older than one "
                "already linked to this zero",
            )
        purpose = STEP_PURPOSE[zero.state]
        if (
            purpose == GroupPurpose.ZERO_STEP1.value
            and len(repo_groups.list_shots(conn, group.id)) > STEP1_MAX_SHOTS
        ):
            raise ZeroingError("invalid_step1_shots", STEP1_SHOTS_MESSAGE)
        _link(conn, zero, group, purpose)
        zero = _sync(conn, zero_id)
    return _view(conn, zero)


def detach_group(conn: sqlite3.Connection, zero_id: int, group_id: int) -> ZeroView:
    """Detach an abandoned step group: it becomes a general group and its offsets are recomputed.

    Only linked groups are judged, so detaching a short or unfinished confirm group lets a fresh
    one be judged normally. Detaching the only initial group during the confirm stage returns the
    zero to Step 2 initial. Raises `ZeroingError`: `zero_not_found`, `group_not_found`,
    `zero_state` (complete or superseded), `group_not_linked`.
    """
    with _write_transaction(conn):
        zero = require_zero(conn, zero_id)
        _require_open(zero)
        group = _require_group(conn, group_id)
        if group.zero_id != zero.id or group.purpose not in STEP_PURPOSES:
            raise ZeroingError(
                "group_not_linked", f"group {group_id} is not a step group of zero {zero_id}"
            )
        repo_groups.update_group(conn, group.id, zero_id=None, purpose=GENERAL_PURPOSE)
        _recompute_group(conn, group.id)
        if (
            group.purpose == GroupPurpose.ZERO_STEP2.value
            and zero.state in (ZeroState.STEP2_CONFIRM.value, ZeroState.STEP3_DOT.value)
            and not any(
                g.purpose == GroupPurpose.ZERO_STEP2.value
                for g in repo_zeroing.list_linked_groups(conn, zero.id)
            )
        ):
            _updated(
                repo_zeroing.update_zero(
                    conn,
                    zero_id,
                    state=ZeroState.STEP2_INITIAL.value,
                    zero_es_in=None,
                    zero_shot_count=None,
                ),
                zero_id,
            )
        zero = _sync(conn, zero_id)
    return _view(conn, zero)


def record_dialed(
    conn: sqlite3.Connection, zero_id: int, up_mil: float, right_mil: float
) -> ZeroView:
    """Record the correction the user dialed (it never enters the judging math).

    Step 1 needs a linked Step 1 group of 1 to 3 shots and moves to Step 2; Step 2 initial needs a
    judged initial group (10 or more shots) and moves to the confirm stage; later stages only
    record it. Raises `ZeroingError`: `zero_not_found`, `zero_state` (complete or superseded),
    `nothing_to_dial`, `invalid_step1_shots`, `initial_group_not_ready`.
    """
    with _write_transaction(conn):
        zero = require_zero(conn, zero_id)
        _require_open(zero)
        view = _view(conn, zero)
        if _distance_problem(view):
            raise ZeroingError("invalid_distance", DISTANCE_MESSAGE)
        next_state = zero.state
        if zero.state == ZeroState.STEP1.value:
            judged = [g for g in view.groups if g.purpose == GroupPurpose.ZERO_STEP1.value]
            if not judged or judged[-1].shot_count < 1:
                raise ZeroingError("nothing_to_dial", "shoot and link a Step 1 group first")
            if judged[-1].shot_count > STEP1_MAX_SHOTS:
                raise ZeroingError("invalid_step1_shots", STEP1_SHOTS_MESSAGE)
            next_state = ZeroState.STEP2_INITIAL.value
        elif zero.state == ZeroState.STEP2_INITIAL.value:
            if view.advice is None or view.advice.advice.kind not in CORRECTION_KINDS:
                raise ZeroingError(
                    "initial_group_not_ready", "the initial Step 2 group needs 10 judged shots"
                )
            next_state = ZeroState.STEP2_CONFIRM.value
        _updated(
            repo_zeroing.update_zero(
                conn, zero_id, state=next_state, dialed_up_mil=up_mil, dialed_right_mil=right_mil
            ),
            zero_id,
        )
        updated = _sync(conn, zero_id)
    return _view(conn, updated)


def _judged_groups_final(conn: sqlite3.Connection, zero: ZeroRow) -> bool:
    initial, confirms = _confirm_groups(repo_zeroing.list_linked_groups(conn, zero.id))
    judged = ([initial] if initial else []) + confirms
    return all(g.status == "final" for g in judged)


def set_dot(conn: sqlite3.Connection, zero_id: int, dot_size_in: float) -> ZeroView:
    """Choose the dot (the suggestion or an override) and complete the zero.

    The zero must be at Step 3 (a ready confirm judgment) with every judged group finalized. The
    older zeros of the rifle and load are superseded (a newer zero still in progress is left alone).
    Raises `ZeroingError`: `zero_not_found`,
    `zero_state`, `groups_not_final`, `invalid_dot_size`.
    """
    if not dot_size_in > 0:
        raise ZeroingError("invalid_dot_size", "the dot size must be positive")
    with _write_transaction(conn):
        zero = _sync(conn, zero_id)
        if _distance_problem(_view(conn, zero)):
            raise ZeroingError("invalid_distance", DISTANCE_MESSAGE)
        if zero.state != ZeroState.STEP3_DOT.value:
            raise ZeroingError(
                "zero_state", "judge a ready confirm group (Step 2) before choosing the dot"
            )
        if not _judged_groups_final(conn, zero):
            raise ZeroingError(
                "groups_not_final", "finalize every zeroing group before choosing the dot"
            )
        updated = _updated(
            repo_zeroing.update_zero(
                conn,
                zero_id,
                state=ZeroState.COMPLETE.value,
                dot_size_in=dot_size_in,
                completed_at=now_iso(),
            ),
            zero_id,
        )
        newer = conn.execute(
            "SELECT id, state FROM zeros WHERE rifle_id = ? AND load_id = ? AND id > ?"
            " AND state != 'superseded'",
            (zero.rifle_id, zero.load_id, zero.id),
        ).fetchall()
        repo_zeroing.supersede_zeros(conn, zero.rifle_id, zero.load_id, zero.id)
        for row in newer:
            repo_zeroing.update_zero(conn, row["id"], state=row["state"])
    return _view(conn, updated)


def create_check_group(
    conn: sqlite3.Connection,
    zero_id: int,
    *,
    lot_code: str,
    shots_expected: int,
    position: Position | str,
    suppressor: bool,
    notes: str = "",
    shot_date: str | None = None,
    shot_time: str | None = None,
    suppressor_id: int | None = None,
    photo_id: int | None = None,
) -> GroupRow:
    """Create a draft zero-check group at 100 yd, linked to the complete zero from birth.

    The group is a `zero_check` group of the zero's rifle and load from its first moment, so it is
    masked (no point of impact) before it has any shots; `run_zero_check` later judges it. Its
    distance is always 100 yd. The lot is get-or-created from `lot_code`; the date and time come
    from `shot_date`/`shot_time`, or from the photo's own date when `shot_date` is omitted
    (`resolve_shot_time`). Nothing is written on any error. Raises `ZeroingError`: `zero_not_found`,
    `zero_superseded`, `zero_not_complete`, `invalid_distance` (a group of this zero was not shot
    at 100 yd), `invalid_lot_code`, `invalid_shots_expected` (a check takes 1 or 2 shots),
    `suppressor_not_found`, `group_not_linkable` (a lot other than the zero's own); and
    `GroupError` `invalid_shot_date` (no date given and the photo has none), `photo_not_found`.
    """
    code = lot_code.strip()
    if not code:
        raise ZeroingError("invalid_lot_code", "the lot code must not be blank")
    if not 1 <= shots_expected <= MAX_CHECK_SHOTS:
        raise ZeroingError(
            "invalid_shots_expected", f"a zero check takes 1 or {MAX_CHECK_SHOTS} shots"
        )
    with _write_transaction(conn):
        zero = require_zero(conn, zero_id)
        if zero.state == ZeroState.SUPERSEDED.value:
            raise ZeroingError("zero_superseded", "this zero was superseded by a newer one")
        if zero.state != ZeroState.COMPLETE.value or zero.dot_size_in is None:
            raise ZeroingError("zero_not_complete", "choose the dot to complete the zero first")
        if _distance_problem(_view(conn, zero)):
            raise ZeroingError("invalid_distance", DISTANCE_MESSAGE)
        if (
            suppressor
            and suppressor_id is not None
            and repo_groups.get_suppressor(conn, suppressor_id) is None
        ):
            raise ZeroingError("suppressor_not_found", f"suppressor {suppressor_id} does not exist")
        date, time, offset, source = resolve_shot_time(
            conn, photo_id=photo_id, shot_date=shot_date, shot_time=shot_time, utc_offset=None
        )
        lot = repo_rifles.get_or_create_lot(conn, zero.load_id, code)
        try:
            return repo_groups.create_group(
                conn,
                load_id=zero.load_id,
                lot_id=lot.id,
                shots_expected=shots_expected,
                shot_date=date,
                shot_time=time,
                utc_offset=offset,
                shot_at_source=source,
                distance_yd=ZERO_DISTANCE_YD,
                position=Position(position).value,
                suppressor=suppressor,
                suppressor_id=suppressor_id if suppressor else None,
                photo_id=photo_id,
                purpose=ZERO_CHECK_PURPOSE,
                zero_id=zero.id,
                notes=notes,
            )
        except GroupLinkMismatch as exc:
            raise ZeroingError("group_not_linkable", str(exc)) from exc


def run_zero_check(conn: sqlite3.Connection, zero_id: int, group_id: int) -> CheckView:
    """Judge a finalized 1-2 shot group against the dot of this (complete) zero and record it.

    The dot comes from the zero named in the request, not from whichever zero is active. The
    group is linked to the zero and given the zero-check purpose, then the check is stored, all
    in one transaction (nothing changes when any guard fails). A shot passes when it is inside
    or touching the dot. Raises `ZeroingError`: `zero_not_found`, `group_not_found`,
    `zero_not_complete`, `zero_superseded`, `group_not_final`, `invalid_distance`,
    `invalid_zero_check_shots`, `group_linked_elsewhere`, `group_not_linkable`,
    `group_already_checked`.
    """
    with _write_transaction(conn):
        zero = require_zero(conn, zero_id)
        if zero.state == ZeroState.SUPERSEDED.value:
            raise ZeroingError("zero_superseded", "this zero was superseded by a newer one")
        if zero.state != ZeroState.COMPLETE.value or zero.dot_size_in is None:
            raise ZeroingError("zero_not_complete", "choose the dot to complete the zero first")
        if _distance_problem(_view(conn, zero)):
            raise ZeroingError("invalid_distance", DISTANCE_MESSAGE)
        group = _require_group(conn, group_id)
        if group.status != "final":
            raise ZeroingError("group_not_final", "finalize the zero-check group first")
        if group.zero_id not in (None, zero.id) or (
            group.zero_id == zero.id and group.purpose in STEP_PURPOSES
        ):
            raise ZeroingError(
                "group_linked_elsewhere", f"group {group_id} is already part of a zero"
            )
        _require_100_yd(group)
        inputs = _inputs(conn, group_id)
        shots = 0 if inputs is None else len(inputs.offsets)
        if inputs is None or not 1 <= shots <= 2:
            raise ZeroingError(
                "invalid_zero_check_shots", f"a zero check takes 1 or 2 shots, got {shots}"
            )
        rifle = repo_rifles.get_rifle(conn, zero.rifle_id)
        if rifle is None:
            raise ZeroingError("rifle_not_found", f"rifle {zero.rifle_id} does not exist")
        result = zero_check(inputs.offsets, zero.dot_size_in, rifle.bullet_diameter_in)
        _link(conn, zero, group, ZERO_CHECK_PURPOSE)
        try:
            row = repo_zeroing.create_zero_check(
                conn,
                zero_id=zero.id,
                group_id=group_id,
                result="pass" if result.passed else "possible_lost_zero",
            )
        except sqlite3.IntegrityError as exc:
            raise ZeroingError(
                "group_already_checked", f"group {group_id} already has a zero check"
            ) from exc
        except GroupNotLinkable as exc:
            raise ZeroingError("group_not_linkable", str(exc)) from exc
    return CheckView(row, result)
