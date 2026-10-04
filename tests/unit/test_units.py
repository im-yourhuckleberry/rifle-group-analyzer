import math

import numpy as np
import pytest

from rga.domain.types import Offset, PlanePoint
from rga.domain.units import (
    CLICK_MIL,
    ZERO_DISTANCE_YD,
    in_to_mil,
    in_to_moa,
    mil_to_in,
    moa_to_in,
    offset_from_aim,
    round_tenth,
)


def test_constants():
    assert ZERO_DISTANCE_YD == 100
    assert CLICK_MIL == 0.1


@pytest.mark.parametrize(
    ("inches", "yd", "mil"),
    [(3.6, 100, 1.0), (10.8, 300, 1.0), (21.6, 600, 1.0), (0.36, 100, 0.1), (-3.6, 100, -1.0)],
)
def test_in_to_mil(inches, yd, mil):
    assert in_to_mil(inches, yd) == pytest.approx(mil, abs=1e-12)


def test_in_to_mil_exact_at_100_yd():
    assert in_to_mil(3.6, 100) == 1.0


@pytest.mark.parametrize("yd", [100, 300, 600])
def test_moa_conversion_at_distances(yd):
    one_moa_in = 1.0471975512 * yd / 100
    assert in_to_moa(one_moa_in, yd) == pytest.approx(1.0, abs=1e-12)
    assert moa_to_in(1.0, yd) == pytest.approx(one_moa_in, abs=1e-12)


def test_in_to_moa_reference_value():
    assert in_to_moa(1.0471975512, 100) == pytest.approx(1.0, abs=1e-12)


@pytest.mark.parametrize("yd", [50, 100, 300, 600])
def test_round_trips(yd):
    assert mil_to_in(in_to_mil(2.5, yd), yd) == pytest.approx(2.5)
    assert moa_to_in(in_to_moa(2.5, yd), yd) == pytest.approx(2.5)


@pytest.mark.parametrize("fn", [in_to_mil, mil_to_in, in_to_moa, moa_to_in])
@pytest.mark.parametrize("yd", [0, -100, math.nan])
def test_non_positive_or_nan_distance_rejected(fn, yd):
    with pytest.raises(ValueError, match="distance"):
        fn(1.0, yd)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.05, 0.1),
        (-0.05, -0.1),
        (0.149, 0.1),
        (0.15, 0.2),
        (-0.15, -0.2),
        (0.25, 0.3),
        (-0.25, -0.3),
        (0.35, 0.4),
        (0.45, 0.5),
        (0.04, 0.0),
        (1.0, 1.0),
        (2.675, 2.7),
        (0.0, 0.0),
    ],
)
def test_round_tenth_half_away_from_zero(value, expected):
    assert round_tenth(value) == expected


def test_round_tenth_never_returns_negative_zero():
    result = round_tenth(-0.04)
    assert result == 0.0
    assert math.copysign(1.0, result) == 1.0


def test_round_tenth_differs_from_builtin_round_on_halves():
    assert round(0.25, 1) != round_tenth(0.25)
    assert round_tenth(0.25) == 0.3


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_round_tenth_rejects_non_finite(bad):
    with pytest.raises(ValueError, match="non-finite"):
        round_tenth(bad)


def test_offset_from_aim_high_right_is_positive_both_axes():
    aim = PlanePoint(5.0, 5.0)
    shot = PlanePoint(5.6, 4.7)  # right of aim, above aim (y is down in plane space)
    offset = offset_from_aim(shot, aim)
    assert offset == Offset(dx_in=pytest.approx(0.6), dy_in=pytest.approx(0.3))
    assert offset.dx_in > 0
    assert offset.dy_in > 0


def test_offset_from_aim_low_left_is_negative_both_axes():
    offset = offset_from_aim(PlanePoint(4.0, 6.0), PlanePoint(5.0, 5.0))
    assert offset.dx_in < 0
    assert offset.dy_in < 0


def test_offset_from_aim_on_aim_is_zero():
    p = PlanePoint(3.0, 3.0)
    assert offset_from_aim(p, p) == Offset(0.0, 0.0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (np.float64(0.05), 0.1),
        (np.float64(-0.05), -0.1),
        (np.float64(0.15), 0.2),
        (np.float64(0.149), 0.1),
        (np.float32(0.05), 0.1),
        (np.float32(-0.05), -0.1),
        (np.float32(0.25), 0.3),
        (np.float32(-0.25), -0.3),
        (np.float64(-0.04), 0.0),
    ],
)
def test_round_tenth_accepts_numpy_floats(value, expected):
    result = round_tenth(value)
    assert result == expected
    assert type(result) is float


@pytest.mark.parametrize("value", [0.0, 0.05, -0.05, 0.149, 0.15, 0.25, 1.0, 2.675, -3.14159])
def test_round_tenth_numpy_float64_matches_python_float(value):
    assert round_tenth(np.float64(value)) == round_tenth(value)
    assert round_tenth(np.float32(value)) == round_tenth(float(np.float32(value)))


def test_round_tenth_numpy_non_finite_rejected():
    with pytest.raises(ValueError, match="non-finite"):
        round_tenth(np.float64("nan"))
    with pytest.raises(ValueError, match="non-finite"):
        round_tenth(np.float32("inf"))


def test_round_tenth_accepts_in_to_mil_numpy_result():
    mil = in_to_mil(np.float64(0.18), 100)
    assert round_tenth(mil) == 0.1
