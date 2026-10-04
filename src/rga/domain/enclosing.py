"""Smallest circle that contains a set of points (Welzl's algorithm, deterministic).

Used for the group page's "spread circle": the minimum enclosing circle of the shot centers; `circle_about` gives the "thing hitter" circle about the point of aim. It
is a second view of a group's size; extreme spread (the largest center-to-center distance) stays
the headline measure.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

_MIX = 2654435761
_MOD = 2**32
_EPS = 1e-9


@dataclass(frozen=True)
class Circle:
    """A circle: center (`cx`, `cy`) and radius `r`, in the units of the input points."""

    cx: float
    cy: float
    r: float

    def contains(self, point: tuple[float, float]) -> bool:
        """True if `point` is inside or on the circle (within a tiny tolerance)."""
        return math.hypot(point[0] - self.cx, point[1] - self.cy) <= self.r * (1 + _EPS) + _EPS


def _diameter(a: tuple[float, float], b: tuple[float, float]) -> Circle:
    return Circle((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, math.dist(a, b) / 2)


def _circumcircle(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
) -> Circle | None:
    ox = (min(a[0], b[0], c[0]) + max(a[0], b[0], c[0])) / 2
    oy = (min(a[1], b[1], c[1]) + max(a[1], b[1], c[1])) / 2
    ax, ay, bx, by, cx, cy = a[0] - ox, a[1] - oy, b[0] - ox, b[1] - oy, c[0] - ox, c[1] - oy
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if d == 0:
        return None
    sa, sb, sc = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    x = ox + (sa * (by - cy) + sb * (cy - ay) + sc * (ay - by)) / d
    y = oy + (sa * (cx - bx) + sb * (ax - cx) + sc * (bx - ax)) / d
    return Circle(x, y, max(math.dist((x, y), p) for p in (a, b, c)))


def _cross(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _with_two(
    points: Sequence[tuple[float, float]], p: tuple[float, float], q: tuple[float, float]
):
    circle = _diameter(p, q)
    left: Circle | None = None
    right: Circle | None = None
    for r in points:
        if circle.contains(r):
            continue
        candidate = _circumcircle(p, q, r)
        if candidate is None:
            continue
        side = _cross(p, q, r)
        along = _cross(p, q, (candidate.cx, candidate.cy))
        if side > 0 and (left is None or along > _cross(p, q, (left.cx, left.cy))):
            left = candidate
        elif side < 0 and (right is None or along < _cross(p, q, (right.cx, right.cy))):
            right = candidate
    if left is None and right is None:
        return circle
    if left is None:
        return right
    if right is None:
        return left
    return left if left.r <= right.r else right


def _with_one(points: Sequence[tuple[float, float]], p: tuple[float, float]) -> Circle:
    circle = Circle(p[0], p[1], 0.0)
    for i, q in enumerate(points):
        if circle.contains(q):
            continue
        circle = _diameter(p, q) if circle.r == 0.0 else _with_two(points[: i + 1], p, q)
    return circle


def smallest_enclosing_circle(points: Sequence[tuple[float, float]]) -> Circle:
    """The minimum enclosing circle of `points` (radius 0 for a single point).

    Deterministic: the same points always give the same circle. Raises ValueError for no points
    or a non-finite coordinate.
    """
    if not points:
        raise ValueError("at least one point is required")
    ordered = sorted((float(x), float(y)) for x, y in points)
    if not all(math.isfinite(v) for p in ordered for v in p):
        raise ValueError("points must be finite")
    shuffled = [p for _, p in sorted(((i * _MIX) % _MOD, p) for i, p in enumerate(ordered))]
    circle = Circle(shuffled[0][0], shuffled[0][1], 0.0)
    for i, p in enumerate(shuffled):
        if not circle.contains(p):
            circle = _with_one(shuffled[: i + 1], p)
    return circle


def circle_about(center: tuple[float, float], points: Sequence[tuple[float, float]]) -> Circle:
    """The circle centered on `center` that just reaches the farthest of `points`.

    Radius is the largest distance from `center` to a point. Raises ValueError for no points or a
    non-finite coordinate.
    """
    if not points:
        raise ValueError("at least one point is required")
    values = [*center, *(c for point in points for c in point)]
    if not all(math.isfinite(v) for v in values):
        raise ValueError("coordinates must be finite")
    return Circle(center[0], center[1], max(math.dist(center, p) for p in points))
