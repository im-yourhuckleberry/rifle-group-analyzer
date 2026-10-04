"""Load comparison guardrails (D-15): like-with-like tiers, precision verdict, POI shift.

Tiers (P-13): tier10 is groups of 10-19 shots and tier30 is groups or aggregates of 30 or more;
groups under 10 shots or of 20-29 shots belong to no tier. Loads are only ever compared within a
tier, so a larger group never penalizes a load. Every group size counts the same, estimated
shots included.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from rga.domain.types import Position

NORMAL_GROUP_VARIATION = 0.30
MIN_ROUNDS = 10
HIGH_CONFIDENCE_ROUNDS = 20
TIER10_MIN_SHOTS = 10
TIER10_MAX_SHOTS = 19
TIER30_MIN_SHOTS = 30

SUFFICIENT_TO_800_MOA = 1.5
SUFFICIENT_TO_600_MOA = 2.0
VERDICT_800_YD = "sufficient for deer/elk to 800 yd"
VERDICT_600_YD = "sufficient for deer/elk to 600 yd"
VERDICT_ABOVE_BAR = "above Form's 2 MOA / 10-shot bar"
ZERO_TO_HUNTING_LOAD_ADVICE = (
    "Zero the rifle to your hunting load so point of aim equals point of impact; "
    "do not adjust your ballistic app to compensate for another load."
)

Tier = Literal["tier10", "tier30"]
PairVerdict = Literal["insufficient_data", "no_real_difference", "a_smaller", "b_smaller"]
LoadStatus = Literal["insufficient_data", "ok", "high_confidence"]

_RELATIVE_TOLERANCE = 1e-9


@dataclass(frozen=True)
class CompareGroup:
    """A finalized group as seen by the comparison: size in MOA, shots, and shooting conditions."""

    es_moa: float
    n: int
    position: Position
    suppressor: bool
    includes_estimated: bool


@dataclass(frozen=True)
class TierSummary:
    """Rounds, group count, mean and worst extreme spread (MOA) of one tier; None when empty."""

    rounds: int
    groups: int
    mean_es_moa: float | None
    worst_es_moa: float | None


@dataclass(frozen=True)
class LoadSummary:
    """A load's tier10 and tier30 summaries; status and verdict are derived from tier10 only."""

    load_id: int
    tier10: TierSummary
    tier30: TierSummary
    status: LoadStatus
    precision_verdict: str | None


def tier_for(shots: int) -> Tier | None:
    """Tier of a group of `shots` shots, or None when it belongs to no tier."""
    if TIER10_MIN_SHOTS <= shots <= TIER10_MAX_SHOTS:
        return "tier10"
    if shots >= TIER30_MIN_SHOTS:
        return "tier30"
    return None


def precision_verdict(es_moa: float) -> str:
    """Plain verdict for a 10-shot mean extreme spread: <= 1.5 MOA to 800 yd, <= 2.0 to 600 yd.

    Raises ValueError for a negative or non-finite value.
    """
    if not math.isfinite(es_moa) or es_moa < 0:
        raise ValueError(f"es_moa must be a finite non-negative number, got {es_moa!r}")
    if es_moa <= SUFFICIENT_TO_800_MOA:
        return VERDICT_800_YD
    if es_moa <= SUFFICIENT_TO_600_MOA:
        return VERDICT_600_YD
    return VERDICT_ABOVE_BAR


def _summarize_tier(groups: Sequence[CompareGroup]) -> TierSummary:
    if not groups:
        return TierSummary(rounds=0, groups=0, mean_es_moa=None, worst_es_moa=None)
    values = [g.es_moa for g in groups]
    return TierSummary(
        rounds=sum(g.n for g in groups),
        groups=len(groups),
        mean_es_moa=math.fsum(values) / len(values),
        worst_es_moa=max(values),
    )


def summarize_load(load_id: int, groups: Sequence[CompareGroup]) -> LoadSummary:
    """Summarize a load's groups per tier. Status is `insufficient_data` without tier10 groups,
    `high_confidence` at 20 or more tier10 rounds, else `ok`; the verdict uses the tier10 mean.
    Raises ValueError for a group with n < 1 or a negative or non-finite ES.
    """
    for g in groups:
        if g.n < 1:
            raise ValueError(f"group needs at least one shot, got n={g.n!r}")
        if not math.isfinite(g.es_moa) or g.es_moa < 0:
            raise ValueError(f"es_moa must be finite and non-negative, got {g.es_moa!r}")
    tier10 = _summarize_tier([g for g in groups if tier_for(g.n) == "tier10"])
    tier30 = _summarize_tier([g for g in groups if tier_for(g.n) == "tier30"])
    if tier10.mean_es_moa is None:
        return LoadSummary(load_id, tier10, tier30, "insufficient_data", None)
    status: LoadStatus = "high_confidence" if tier10.rounds >= HIGH_CONFIDENCE_ROUNDS else "ok"
    return LoadSummary(load_id, tier10, tier30, status, precision_verdict(tier10.mean_es_moa))


def compare_pair(a: LoadSummary, b: LoadSummary, tier: Tier = "tier10") -> PairVerdict:
    """Compare two loads on one tier's mean ES. A tier missing on either side is
    `insufficient_data`; a relative difference of at most NORMAL_GROUP_VARIATION of the pair mean
    is `no_real_difference`; otherwise the smaller mean wins. Raises ValueError for an unknown tier.
    """
    if tier not in ("tier10", "tier30"):
        raise ValueError(f"unknown tier {tier!r}")
    mean_a = getattr(a, tier).mean_es_moa
    mean_b = getattr(b, tier).mean_es_moa
    if mean_a is None or mean_b is None:
        return "insufficient_data"
    pair_mean = (mean_a + mean_b) / 2
    if (
        pair_mean == 0
        or abs(mean_a - mean_b) / pair_mean <= NORMAL_GROUP_VARIATION + _RELATIVE_TOLERANCE
    ):
        return "no_real_difference"
    return "a_smaller" if mean_a < mean_b else "b_smaller"


def poi_shift_mil(
    pairs: Sequence[tuple[tuple[float, float], tuple[float, float]]],
) -> tuple[float, float] | None:
    """Mean of (load POI - zero-load POI) over shared buckets (same day, distance, position and suppressor setting), as (right_mil, up_mil).

    Each pair is `((load_dx, load_dy), (zero_dx, zero_dy))` in mil for one shared bucket.
    Returns None for no pairs; raises ValueError for non-finite values.
    """
    if not pairs:
        return None
    rights, ups = [], []
    for (load_dx, load_dy), (zero_dx, zero_dy) in pairs:
        values = (load_dx, load_dy, zero_dx, zero_dy)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("POI values must be finite")
        rights.append(load_dx - zero_dx)
        ups.append(load_dy - zero_dy)
    return math.fsum(rights) / len(rights), math.fsum(ups) / len(ups)


def conditions_warning(a: Sequence[CompareGroup], b: Sequence[CompareGroup]) -> str | None:
    """Warning when the two loads' groups were shot from different positions or with different
    suppressor use; None when they match or either side has no groups.
    """
    if not a or not b:
        return None
    differences = []
    positions_a, positions_b = {g.position for g in a}, {g.position for g in b}
    if positions_a != positions_b:
        differences.append("shooting position")
    if {g.suppressor for g in a} != {g.suppressor for g in b}:
        differences.append("suppressor use")
    if not differences:
        return None
    return (
        f"The compared loads were shot with different {' and '.join(differences)}; "
        "compare again with the conditions filtered to a single setup."
    )
