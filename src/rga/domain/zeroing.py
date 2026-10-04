"""Zeroing rules for a 100 yd zero (D-04): corrections, the Step 1/2 advice, dot size, zero check.

Offsets are aim-relative inches with y up (`units.offset_from_aim`). A numeric correction is only
ever produced from 1-3 shots in Step 1 or from a group of at least 10 shots. Advice that asks for
more shots, or that is invalid, is built only from fixed integer-count templates and carries no
correction and no statistics.
"""

import math
from collections.abc import Sequence

from rga.domain.stats import group_stats
from rga.domain.types import (
    Advice,
    AdviceKind,
    Correction,
    GroupPurpose,
    GroupStats,
    Offset,
    ShotOrigin,
    ZeroCheckResult,
)
from rga.domain.units import ZERO_DISTANCE_YD, in_to_mil, round_tenth

STANDARD_DOTS_IN = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
STEP1_MAX_SHOTS = 3
STEP2_MIN_SHOTS = 10
VERY_HIGH_CONFIDENCE_SHOTS = 30
ZERO_CHECK_MAX_SHOTS = 2
TOUCH_UP_MIL = 0.1

_NO_SHOTS_MESSAGE = "No shots to analyze: add at least 1 shot."
_STEP1_ONLY_MESSAGE = "Step 1 only corrects from 1 to 3 shots."
ZERO_CHECK_OK_MESSAGE = "Zero check passed: every shot is inside or touching the dot."
ZERO_CHECK_LOST_ZERO_MESSAGE = (
    "Possible lost zero. Check the mount first (#1 cause), then the scope (#2 cause), "
    "then go back to Step 2."
)
_TIE_TOLERANCE = 1e-9


_SHORT_ZERO_PURPOSES = frozenset({GroupPurpose.ZERO_STEP2.value, GroupPurpose.ZERO_CONFIRM.value})


def masks_poi(purpose: str, shot_count: int) -> bool:
    """True when no point of impact may be shown: any zero_check group, a zero_step2 or
    zero_confirm group with fewer than 10 shots, or a zero_step1 group with more than 3 shots
    (Step 1 only corrects from 1 to 3 shots). zero_step1 groups of 1 to 3 shots and general
    groups are never masked.
    """
    purpose = str(purpose)
    if purpose == GroupPurpose.ZERO_CHECK.value:
        return True
    if purpose == GroupPurpose.ZERO_STEP1.value:
        return shot_count > STEP1_MAX_SHOTS
    return purpose in _SHORT_ZERO_PURPOSES and shot_count < STEP2_MIN_SHOTS


def shoot_more_message(shots: int) -> str:
    """Fixed shoot-more text for `shots` fired so far; integer counts only."""
    return f"Shoot more shots: {shots} of {STEP2_MIN_SHOTS} so far, {STEP2_MIN_SHOTS - shots} left to fire"


def confirm_group_short_message(number: int, shots: int) -> str:
    """Fixed invalid-advice text for an earlier confirm group (1-based `number`) under 10 shots."""
    return (
        f"Confirm group {number} has {shots} of {STEP2_MIN_SHOTS} shots: every confirm group "
        f"needs {STEP2_MIN_SHOTS} shots before the zero can be judged"
    )


def _shoot_more(message: str) -> Advice:
    return Advice(kind=AdviceKind.SHOOT_MORE, message=message)


def _invalid(message: str) -> Advice:
    return Advice(kind=AdviceKind.INVALID, message=message)


def correction_for(mean_dx_mil: float, mean_dy_mil: float) -> Correction:
    """Scope correction that moves a group centered at the given mean POI onto the aim point.

    Each axis is rounded to 0.1 mil, halves away from zero. A group that is high gives a DOWN
    correction and one that is right gives a LEFT correction; a zero axis is left out of the text
    and a zero correction reads "No adjustment".
    """
    up_mil = round_tenth(-mean_dy_mil)
    right_mil = round_tenth(-mean_dx_mil)
    parts = []
    if up_mil != 0.0:
        parts.append(f"{'UP' if up_mil > 0 else 'DOWN'} {abs(up_mil):.1f}")
    if right_mil != 0.0:
        parts.append(f"{'RIGHT' if right_mil > 0 else 'LEFT'} {abs(right_mil):.1f}")
    text = f"Dial {', '.join(parts)}" if parts else "No adjustment"
    return Correction(up_mil=up_mil, right_mil=right_mil, text=text)


def _origins_or_detected(
    offsets: Sequence[Offset], origins: Sequence[ShotOrigin] | None
) -> Sequence[ShotOrigin]:
    return origins if origins is not None else [ShotOrigin.DETECTED] * len(offsets)


def _correction_advice(stats: GroupStats, message_prefix: str) -> Advice:
    correction = correction_for(stats.mean_dx_mil, stats.mean_dy_mil)
    if correction.up_mil == 0.0 and correction.right_mil == 0.0:
        return Advice(AdviceKind.NO_ADJUSTMENT, "No adjustment needed.", correction, stats)
    return Advice(AdviceKind.CORRECTION, f"{message_prefix}{correction.text}.", correction, stats)


def step1_advice(offsets: Sequence[Offset], origins: Sequence[ShotOrigin] | None = None) -> Advice:
    """Step 1 (get on paper): 1-3 shots give a correction, 4 or more are never corrected here.

    0 shots is INVALID. `origins` (default all detected) only feeds `includes_estimated`.
    """
    shots = len(offsets)
    if shots == 0:
        return _invalid(_NO_SHOTS_MESSAGE)
    if shots > STEP1_MAX_SHOTS:
        if shots < STEP2_MIN_SHOTS:
            return _shoot_more(
                f"{_STEP1_ONLY_MESSAGE} Continue to {STEP2_MIN_SHOTS} shots for Step 2: "
                f"{shots} of {STEP2_MIN_SHOTS} so far, {STEP2_MIN_SHOTS - shots} left to fire"
            )
        return _shoot_more(
            f"{_STEP1_ONLY_MESSAGE} This group has {shots} shots: use Step 2 for the correction."
        )
    stats = group_stats(offsets, _origins_or_detected(offsets, origins), ZERO_DISTANCE_YD)
    return _correction_advice(stats, "Get on paper: ")


def step2_initial_advice(offsets: Sequence[Offset], origins: Sequence[ShotOrigin]) -> Advice:
    """Step 2 initial group: 10 or more shots give a correction off the mean POI of all shots.

    Fewer than 10 shots is SHOOT_MORE, never a number; 0 shots is INVALID.
    """
    if len(origins) != len(offsets):
        raise ValueError("origins must match offsets one to one")
    shots = len(offsets)
    if shots == 0:
        return _invalid(_NO_SHOTS_MESSAGE)
    if shots < STEP2_MIN_SHOTS:
        return _shoot_more(shoot_more_message(shots))
    stats = group_stats(offsets, origins, ZERO_DISTANCE_YD)
    return _correction_advice(stats, "Step 2: ")


def _centered(offsets: Sequence[Offset]) -> list[Offset]:
    cx = math.fsum(o.dx_in for o in offsets) / len(offsets)
    cy = math.fsum(o.dy_in for o in offsets) / len(offsets)
    return [Offset(o.dx_in - cx, o.dy_in - cy) for o in offsets]


def step2_confirm_advice(
    initial: Sequence[Offset],
    dialed: Correction,
    confirms: Sequence[Sequence[Offset]],
    origins_all: Sequence[ShotOrigin],
) -> Advice:
    """Judge the confirm group of Step 2 (center-on-center overlay, P-02).

    `origins_all` lists the shot origins of the initial group followed by each confirm group, in
    order. The zero is judged on the latest confirm group's own centroid: both axes round to 0.0
    -> READY_FOR_DOT, the larger rounds to 0.1 -> TOUCH_UP_OK, 0.2 or more -> CONFIRM_AGAIN. The
    attached stats are the overlay of every group, each translated to its own centroid, so ES
    measures cone size only. `dialed` is recorded by the caller and never enters the math. A
    latest confirm group under 10 shots is SHOOT_MORE; an initial group or an earlier confirm
    group under 10 shots is INVALID (every group in the overlay is a full group).
    """
    groups = [list(initial), *(list(c) for c in confirms)]
    if len(origins_all) != sum(len(g) for g in groups):
        raise ValueError("origins_all must match the initial and confirm shots one to one")
    if len(initial) < STEP2_MIN_SHOTS:
        return _invalid(
            f"The first Step 2 group needs {STEP2_MIN_SHOTS} shots before a confirm group can be "
            f"judged: {len(initial)} of {STEP2_MIN_SHOTS} so far."
        )
    for number, earlier in enumerate(groups[1:-1], start=1):
        if len(earlier) < STEP2_MIN_SHOTS:
            return _invalid(confirm_group_short_message(number, len(earlier)))
    latest = groups[-1]
    if len(groups) == 1 or len(latest) < STEP2_MIN_SHOTS:
        return _shoot_more(shoot_more_message(0 if len(groups) == 1 else len(latest)))

    overlay = [o for g in groups for o in _centered(g)]
    overlay_stats = group_stats(overlay, origins_all, ZERO_DISTANCE_YD)
    confirm_mean_dx = math.fsum(o.dx_in for o in latest) / len(latest)
    confirm_mean_dy = math.fsum(o.dy_in for o in latest) / len(latest)
    correction = correction_for(
        in_to_mil(confirm_mean_dx, ZERO_DISTANCE_YD), in_to_mil(confirm_mean_dy, ZERO_DISTANCE_YD)
    )
    worst = max(abs(correction.up_mil), abs(correction.right_mil))
    confidence = (
        " The 30-shot overlay gives very high confidence."
        if overlay_stats.n >= VERY_HIGH_CONFIDENCE_SHOTS
        else ""
    )
    if worst == 0.0:
        return Advice(
            AdviceKind.READY_FOR_DOT,
            f"Zero confirmed: ready for the dot.{confidence}",
            None,
            overlay_stats,
        )
    if worst <= TOUCH_UP_MIL + _TIE_TOLERANCE:
        return Advice(
            AdviceKind.TOUCH_UP_OK,
            f"A 0.1 mil touch-up is acceptable: {correction.text}. Then move on to the dot."
            f"{confidence}",
            correction,
            overlay_stats,
        )
    return Advice(
        AdviceKind.CONFIRM_AGAIN,
        f"{correction.text}, then shoot another {STEP2_MIN_SHOTS}-shot confirm group.",
        correction,
        overlay_stats,
    )


def suggest_dot(zero_es_in: float, bullet_diameter_in: float) -> float:
    """Standard dot size nearest to `zero_es_in - bullet_diameter_in` (P-01); ties go larger.

    Raises ValueError for a negative or non-finite ES, or a non-positive bullet diameter.
    """
    if not math.isfinite(zero_es_in) or zero_es_in < 0:
        raise ValueError(f"zero ES must be a non-negative number, got {zero_es_in!r}")
    if not (math.isfinite(bullet_diameter_in) and bullet_diameter_in > 0):
        raise ValueError(f"bullet diameter must be positive, got {bullet_diameter_in!r}")
    target = zero_es_in - bullet_diameter_in
    return min(STANDARD_DOTS_IN, key=lambda dot: (round(abs(dot - target), 9), -dot))


def zero_check(
    offsets: Sequence[Offset], dot_size_in: float, bullet_diameter_in: float
) -> ZeroCheckResult:
    """Zero check of 1-2 shots against the dot: a shot passes when inside or touching it.

    A shot passes when its distance from the aim is at most dot radius plus bullet radius. The
    check passes only if every shot does. Raises ValueError unless there are 1-2 shots and both
    sizes are positive.
    """
    if not 1 <= len(offsets) <= ZERO_CHECK_MAX_SHOTS:
        raise ValueError(f"a zero check takes 1 or 2 shots, got {len(offsets)}")
    if not (math.isfinite(dot_size_in) and dot_size_in > 0):
        raise ValueError(f"dot size must be positive, got {dot_size_in!r}")
    if not (math.isfinite(bullet_diameter_in) and bullet_diameter_in > 0):
        raise ValueError(f"bullet diameter must be positive, got {bullet_diameter_in!r}")
    limit = dot_size_in / 2.0 + bullet_diameter_in / 2.0
    inside = tuple(math.hypot(o.dx_in, o.dy_in) <= limit + _TIE_TOLERANCE for o in offsets)
    passed = all(inside)
    return ZeroCheckResult(
        passed, inside, ZERO_CHECK_OK_MESSAGE if passed else ZERO_CHECK_LOST_ZERO_MESSAGE
    )
