"""Unit conversions, half-away-from-zero rounding, and the single y-flip into offset space."""

import math
from decimal import ROUND_HALF_UP, Decimal

from rga.domain.types import Offset, PlanePoint

ZERO_DISTANCE_YD = 100
CLICK_MIL = 0.1
MOA_IN_PER_100YD = 1.0471975512


def _require_distance(yd: float) -> None:
    if not yd > 0:
        raise ValueError(f"distance must be positive, got {yd!r}")


def in_to_mil(inches: float, yd: float) -> float:
    """Convert inches at `yd` yards to mil (small angle). Raises ValueError if yd <= 0."""
    _require_distance(yd)
    return inches * 1000 / (yd * 36)


def mil_to_in(mil: float, yd: float) -> float:
    """Convert mil at `yd` yards to inches. Raises ValueError if yd <= 0."""
    _require_distance(yd)
    return mil * yd * 36 / 1000


def in_to_moa(inches: float, yd: float) -> float:
    """Convert inches at `yd` yards to MOA (1.0471975512 in per MOA per 100 yd)."""
    _require_distance(yd)
    return inches / (MOA_IN_PER_100YD * yd / 100)


def moa_to_in(moa: float, yd: float) -> float:
    """Convert MOA at `yd` yards to inches."""
    _require_distance(yd)
    return moa * MOA_IN_PER_100YD * yd / 100


def round_tenth(x: float) -> float:
    """Round to one decimal, halves away from zero, using the shortest decimal repr of `x`.

    Accepts Python and numpy floating scalars. Raises ValueError for NaN or infinity. Never
    returns -0.0.
    """
    x = float(x)
    if not math.isfinite(x):
        raise ValueError(f"cannot round non-finite value {x!r}")
    magnitude = Decimal(repr(abs(x))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    result = float(magnitude)
    if result == 0.0:
        return 0.0
    return math.copysign(result, x)


def offset_from_aim(shot: PlanePoint, aim: PlanePoint) -> Offset:
    """Offset of `shot` from `aim` with y flipped to up-positive; the only y-flip in the codebase."""
    return Offset(dx_in=shot.x_in - aim.x_in, dy_in=aim.y_in - shot.y_in)
