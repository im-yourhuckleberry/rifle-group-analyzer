"""T1: smallest enclosing circle (the group page's spread circle)."""

import itertools
import math
import random

import pytest

from rga.domain.enclosing import Circle, circle_about, smallest_enclosing_circle


def brute_force(points):
    """Smallest circle through any two (as a diameter) or three points that holds every point."""
    from rga.domain.enclosing import _circumcircle

    candidates = [
        Circle((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, math.dist(a, b) / 2)
        for a, b in itertools.combinations(points, 2)
    ]
    for a, b, c in itertools.combinations(points, 3):
        circle = _circumcircle(a, b, c)
        if circle is not None:
            candidates.append(circle)
    return min((c for c in candidates if all(c.contains(p) for p in points)), key=lambda c: c.r)


def test_a_single_point_is_a_circle_of_radius_zero():
    assert smallest_enclosing_circle([(3.0, -2.0)]) == Circle(3.0, -2.0, 0.0)


def test_two_points_give_the_circle_on_their_diameter():
    circle = smallest_enclosing_circle([(0.0, 0.0), (6.0, 8.0)])

    assert (circle.cx, circle.cy, circle.r) == (3.0, 4.0, 5.0)


def test_an_equilateral_triangle_gets_its_circumcircle():
    side = 2.0
    points = [(0.0, 0.0), (side, 0.0), (side / 2, side * math.sqrt(3) / 2)]

    circle = smallest_enclosing_circle(points)

    assert circle.r == pytest.approx(side / math.sqrt(3))
    assert circle.cx == pytest.approx(1.0)
    assert circle.cy == pytest.approx(side * math.sqrt(3) / 6)


def test_a_point_inside_does_not_change_the_circle():
    base = smallest_enclosing_circle([(0.0, 0.0), (4.0, 0.0)])

    assert smallest_enclosing_circle([(0.0, 0.0), (4.0, 0.0), (2.0, 0.5)]) == base


def test_collinear_and_duplicate_points_are_handled():
    line = smallest_enclosing_circle([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (5.0, 0.0)])
    repeated = smallest_enclosing_circle([(1.0, 1.0)] * 5 + [(3.0, 1.0)])

    assert (line.cx, line.r) == (2.5, 2.5)
    assert (repeated.cx, repeated.cy, repeated.r) == (2.0, 1.0, 1.0)


def test_it_matches_brute_force_on_random_sets():
    rng = random.Random(7)
    for _ in range(300):
        n = rng.randint(2, 12)
        points = [(rng.uniform(-3, 3), rng.uniform(-3, 3)) for _ in range(n)]

        circle = smallest_enclosing_circle(points)

        assert circle.r == pytest.approx(brute_force(points).r, abs=1e-7)
        assert all(circle.contains(p) for p in points)


def test_a_bigger_random_group_is_enclosed_and_touched_by_two_or_more_points():
    rng = random.Random(3)
    points = [(rng.gauss(0, 1), rng.gauss(0, 1)) for _ in range(60)]

    circle = smallest_enclosing_circle(points)

    on_edge = [p for p in points if math.dist(p, (circle.cx, circle.cy)) > circle.r * (1 - 1e-7)]
    assert all(circle.contains(p) for p in points)
    assert len(on_edge) >= 2


def test_the_result_does_not_depend_on_input_order_or_the_global_random_state():
    points = [(0.1, 0.2), (1.5, -0.3), (0.7, 2.2), (-1.0, 0.4), (0.2, -1.1)]
    reference = smallest_enclosing_circle(points)
    random.seed(99)

    shuffled = list(reversed(points))

    again = smallest_enclosing_circle(shuffled)
    assert again.r == pytest.approx(reference.r) and again.cx == pytest.approx(reference.cx)
    assert smallest_enclosing_circle(points) == reference


@pytest.mark.parametrize("bad", [[], [(0.0, float("nan"))], [(float("inf"), 0.0), (1.0, 1.0)]])
def test_empty_or_non_finite_input_is_rejected(bad):
    with pytest.raises(ValueError):
        smallest_enclosing_circle(bad)


class TestCircleAbout:
    def test_radius_is_the_distance_to_the_farthest_point(self):
        circle = circle_about((0.0, 0.0), [(3.0, 4.0), (1.0, 1.0), (-0.5, 0.0)])

        assert (circle.cx, circle.cy) == (0.0, 0.0)
        assert circle.r == pytest.approx(5.0)

    def test_it_is_centered_where_asked_not_on_the_minimal_circle(self):
        points = [(2.0, 0.0), (-2.0, 0.0)]

        about = circle_about((1.0, 0.0), points)

        assert about.r == pytest.approx(3.0)
        assert smallest_enclosing_circle(points).r == pytest.approx(2.0)
        assert all(about.contains(p) for p in points)

    def test_a_single_point_at_the_center_has_radius_zero(self):
        assert circle_about((2.0, 3.0), [(2.0, 3.0)]).r == 0.0

    def test_the_radius_is_at_least_half_the_extreme_spread_of_the_points(self):
        rng = random.Random(8)
        points = [(rng.gauss(0, 1), rng.gauss(0, 1)) for _ in range(25)]
        spread = max(math.dist(a, b) for a, b in itertools.combinations(points, 2))

        assert circle_about((0.2, -0.1), points).r >= spread / 2 - 1e-12

    @pytest.mark.parametrize(
        ("center", "points"),
        [
            ((0.0, 0.0), []),
            ((float("nan"), 0.0), [(1.0, 1.0)]),
            ((0.0, 0.0), [(float("inf"), 0.0)]),
        ],
    )
    def test_empty_or_non_finite_input_is_rejected(self, center, points):
        with pytest.raises(ValueError):
            circle_about(center, points)
