"""Group statistics: extreme spread (center to center), mean point of impact, and aggregates.

Offsets are aim-relative inches with y up (see `units.offset_from_aim`). Every shot counts,
estimated ones included; results carry `includes_estimated`. No other group metric is computed.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from rga.domain.types import GroupStats, Offset, ShotOrigin
from rga.domain.units import in_to_mil, in_to_moa


@dataclass(frozen=True)
class AggregateInput:
    """One selected group: aim-relative offsets, shot origins, distance (yd) and its load id."""

    offsets: Sequence[Offset]
    origins: Sequence[ShotOrigin]
    distance_yd: float
    load_id: int | None = None


@dataclass(frozen=True)
class AggregateStats:
    """Combined statistics of several groups overlaid on their own aim points."""

    n: int
    es_in: float
    es_moa: float
    center_dx_in: float
    center_dy_in: float
    center_dx_mil: float
    center_dy_mil: float
    includes_estimated: bool
    mixed_loads: bool


def _points(offsets: Sequence[Offset]) -> np.ndarray:
    pts = np.array([(o.dx_in, o.dy_in) for o in offsets], dtype=np.float64).reshape(-1, 2)
    if not np.all(np.isfinite(pts)):
        raise ValueError("offsets must be finite")
    return pts


def _es_of_points(pts: np.ndarray) -> float:
    if len(pts) < 2:
        return 0.0
    delta = pts[:, None, :] - pts[None, :, :]
    return float(math.sqrt(float(np.max(np.einsum("ijk,ijk->ij", delta, delta)))))


def _center_of_points(pts: np.ndarray) -> tuple[float, float]:
    return math.fsum(pts[:, 0]) / len(pts), math.fsum(pts[:, 1]) / len(pts)


def extreme_spread_in(offsets: Sequence[Offset]) -> float:
    """Largest center-to-center distance between any two shots, in inches; 0.0 for fewer than 2."""
    return _es_of_points(_points(offsets))


def group_stats(
    offsets: Sequence[Offset], origins: Sequence[ShotOrigin], distance_yd: float
) -> GroupStats:
    """ES (in, MOA) and mean POI (in, mil) of one group; `includes_estimated` if any origin is
    ESTIMATED. Raises ValueError for no shots, mismatched lengths, non-finite offsets, or
    distance <= 0.
    """
    if len(offsets) == 0:
        raise ValueError("a group needs at least one shot")
    if len(origins) != len(offsets):
        raise ValueError("origins must match offsets one to one")
    pts = _points(offsets)
    es_in = _es_of_points(pts)
    mean_dx, mean_dy = _center_of_points(pts)
    return GroupStats(
        n=len(pts),
        es_in=es_in,
        es_moa=in_to_moa(es_in, distance_yd),
        mean_dx_in=mean_dx,
        mean_dy_in=mean_dy,
        mean_dx_mil=in_to_mil(mean_dx, distance_yd),
        mean_dy_mil=in_to_mil(mean_dy, distance_yd),
        includes_estimated=ShotOrigin.ESTIMATED in origins,
    )


def aggregate(groups: Sequence[AggregateInput]) -> AggregateStats:
    """Overlay groups on their own aim points (offsets are already aim-relative) and measure the
    union. Raises ValueError for no groups or shots, mixed distances, or mismatched lengths.
    `mixed_loads` is True when the groups do not all share one load id.
    """
    if len(groups) == 0:
        raise ValueError("aggregate needs at least one group")
    distances = {g.distance_yd for g in groups}
    if len(distances) != 1:
        raise ValueError(f"cannot aggregate groups at different distances: {sorted(distances)}")
    distance_yd = distances.pop()
    for g in groups:
        if len(g.origins) != len(g.offsets):
            raise ValueError("origins must match offsets one to one")
    all_offsets = [o for g in groups for o in g.offsets]
    all_origins = [s for g in groups for s in g.origins]
    stats = group_stats(all_offsets, all_origins, distance_yd)
    return AggregateStats(
        n=stats.n,
        es_in=stats.es_in,
        es_moa=stats.es_moa,
        center_dx_in=stats.mean_dx_in,
        center_dy_in=stats.mean_dy_in,
        center_dx_mil=stats.mean_dx_mil,
        center_dy_mil=stats.mean_dy_mil,
        includes_estimated=stats.includes_estimated,
        mixed_loads=len({g.load_id for g in groups}) > 1,
    )
