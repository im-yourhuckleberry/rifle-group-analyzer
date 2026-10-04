"""T1: circle reference candidates, manual fits, reference result and aim snap on exact renders."""

from __future__ import annotations

import math

import cv2
import numpy as np
import pytest

from rga.domain.types import CircleCandidate, ReferenceKind, ReferenceResult
from rga.vision.reference_circle import (
    _Ellipse,
    _fit_rays,
    _outer_first,
    _Refined,
    _round_edge_fit,
    _validate_coarse,
    circle_from_edge_points,
    find_circle_candidates,
    reference_from_circle,
    refine_circle_near,
)
from rga.vision.reference_grid import GridCandidate
from rga.vision.transform import apply_h

PAPER = (232, 238, 242)
GREEN = (60, 170, 40)
ORANGE = (30, 110, 235)
RED = (40, 70, 235)
BLACK = (18, 18, 18)
BLUE = (200, 110, 50)
SUPERSAMPLE = 8


# ----------------------------------------------------------------------------- exact renderer


class Canvas:
    """Supersampled drawing surface: shapes are placed at exact sub-pixel positions in image px."""

    def __init__(self, width: int, height: int, background=PAPER) -> None:
        self.width, self.height = width, height
        self.big = np.empty((height * SUPERSAMPLE, width * SUPERSAMPLE, 3), dtype=np.uint8)
        self.big[:] = background

    def _pt(self, p) -> tuple[int, int]:
        offset = (SUPERSAMPLE - 1) / 2
        return (
            round((p[0] * SUPERSAMPLE + offset) * 16),
            round((p[1] * SUPERSAMPLE + offset) * 16),
        )

    def ellipse(self, center, axes, angle_deg, color) -> Canvas:
        axes_big = (round(axes[0] / 2 * SUPERSAMPLE * 16), round(axes[1] / 2 * SUPERSAMPLE * 16))
        cv2.ellipse(
            self.big, self._pt(center), axes_big, angle_deg, 0, 360, color, -1, cv2.LINE_8, 4
        )
        return self

    def disc(self, center, diameter, color) -> Canvas:
        return self.ellipse(center, (diameter, diameter), 0, color)

    def polygon(self, points, color) -> Canvas:
        pts = np.array([self._pt(p) for p in points], dtype=np.int32)
        cv2.fillPoly(self.big, [pts], color, cv2.LINE_8, 4)
        return self

    def diamond(self, center, diagonal, color, angle_deg=0.0) -> Canvas:
        h = diagonal / 2
        base = np.array([[0, -h], [h, 0], [0, h], [-h, 0]])
        a = math.radians(angle_deg)
        rot = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
        return self.polygon(base @ rot.T + np.array(center), color)

    def rect(self, p0, p1, color) -> Canvas:
        return self.polygon([(p0[0], p0[1]), (p1[0], p0[1]), (p1[0], p1[1]), (p0[0], p1[1])], color)

    def line(self, p0, p1, width, color) -> Canvas:
        d = np.subtract(p1, p0, dtype=float)
        n = np.array([-d[1], d[0]]) / np.hypot(*d) * width / 2
        pts = [np.add(p0, n), np.add(p1, n), np.subtract(p1, n), np.subtract(p0, n)]
        return self.polygon(pts, color)

    def render(self, noise=2.0, blur=0.8, jpeg=92, seed=0) -> np.ndarray:
        img = cv2.resize(self.big, (self.width, self.height), interpolation=cv2.INTER_AREA)
        if blur > 0:
            img = cv2.GaussianBlur(img, (0, 0), blur)
        if noise > 0:
            rng = np.random.default_rng(seed)
            img = np.clip(img + rng.normal(0, noise, img.shape), 0, 255).astype(np.uint8)
        if jpeg:
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, jpeg])
            assert ok
            img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        return img


def mean_diameter(c: CircleCandidate) -> float:
    return (c.axes_px[0] + c.axes_px[1]) / 2


def nearest(candidates: list[CircleCandidate], center) -> CircleCandidate:
    assert candidates, "no candidates returned"
    return min(candidates, key=lambda c: math.dist(c.center_px, center))


# ----------------------------------------------------------------------------- manual fits


def _ellipse_points(center, axes, angle_deg, n, phase=0.3):
    t = np.linspace(0, 2 * np.pi, n, endpoint=False) + phase
    a = math.radians(angle_deg)
    x, y = axes[0] / 2 * np.cos(t), axes[1] / 2 * np.sin(t)
    return np.column_stack(
        [
            center[0] + x * math.cos(a) - y * math.sin(a),
            center[1] + x * math.sin(a) + y * math.cos(a),
        ]
    )


def test_three_points_give_the_exact_circle() -> None:
    pts = _ellipse_points((123.456, 78.9), (250.5, 250.5), 0, 3)
    c = circle_from_edge_points([tuple(p) for p in pts])
    assert c.center_px == pytest.approx((123.456, 78.9), abs=1e-9)
    assert c.axes_px == pytest.approx((250.5, 250.5), abs=1e-9)
    assert c.angle_deg == 0.0
    assert c.score == 1.0


def test_four_points_give_a_least_squares_circle() -> None:
    pts = _ellipse_points((50.0, 60.0), (100.0, 100.0), 0, 4)
    c = circle_from_edge_points(pts.tolist())
    assert c.center_px == pytest.approx((50.0, 60.0), abs=1e-9)
    assert c.axes_px == pytest.approx((100.0, 100.0), abs=1e-9)


@pytest.mark.parametrize("n", [5, 6, 9, 40])
@pytest.mark.parametrize("angle", [0.0, 30.0, 95.0, 160.0])
def test_five_or_more_points_recover_the_exact_tilted_ellipse(n: int, angle: float) -> None:
    pts = _ellipse_points((321.5, 210.25), (240.0, 180.0), angle, n)
    c = circle_from_edge_points(pts.tolist())
    assert c.center_px == pytest.approx((321.5, 210.25), abs=1e-6)
    assert c.axes_px == pytest.approx((240.0, 180.0), abs=1e-6)
    assert math.isclose(c.angle_deg % 180, angle % 180, abs_tol=1e-6)
    assert 0 <= c.angle_deg < 180
    assert c.score == 1.0


def test_a_circle_fitted_from_many_points_has_equal_axes() -> None:
    pts = _ellipse_points((10.0, 20.0), (80.0, 80.0), 0, 12)
    c = circle_from_edge_points(pts.tolist())
    assert c.axes_px == pytest.approx((80.0, 80.0), abs=1e-6)


def test_noisy_edge_clicks_still_fit_within_a_pixel() -> None:
    rng = np.random.default_rng(3)
    pts = _ellipse_points((400.0, 300.0), (200.0, 200.0), 0, 8) + rng.normal(0, 0.8, (8, 2))
    c = circle_from_edge_points(pts.tolist())
    assert math.dist(c.center_px, (400.0, 300.0)) < 1.0
    assert abs(mean_diameter(c) - 200.0) < 2.0


@pytest.mark.parametrize(
    "points",
    [
        [],
        [(0, 0)],
        [(0, 0), (1, 1)],
        [(0, 0), (1, 1), (2, 2)],
        [(5.0, 5.0)] * 3,
        [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4)],
        [(0, 0), (10, 0), (20, 0), (30, 0), (40, 0), (50, 0)],
        [(7.0, 7.0)] * 4,
        [(0, 0), (1, 0), (float("nan"), 1)],
        [(0, 0), (1, 0), (float("inf"), 1)],
        [(0, 0, 0), (1, 0, 0), (0, 1, 0)],
        [(0, 0), (1, 0), (2, 0), (3, 0)],
        [(0, 0), (100, 0.4), (200, 0)],
        [(0, 0), (10, 0.1), (20, -0.1), (30, 0.05), (40, 0.0), (50, 0.1)],
        [(0, 0), (100, 0), (100, 100), (50, 60)],
    ],
    ids=[
        "empty",
        "one",
        "two",
        "collinear3",
        "coincident3",
        "collinear5",
        "collinear6-axis",
        "coincident4",
        "nan",
        "inf",
        "3d",
        "collinear4",
        "nearly-collinear3",
        "nearly-collinear6",
        "uneven4",
    ],
)
def test_degenerate_manual_points_are_refused(points) -> None:
    with pytest.raises(ValueError):
        circle_from_edge_points(points)


# ----------------------------------------------------------------------------- reference result


def test_reference_from_circle_scales_the_circle_to_its_printed_diameter() -> None:
    c = CircleCandidate((400.0, 300.0), (200.0, 200.0), 0.0, 0.9)
    result = reference_from_circle(c, ReferenceKind.CIRCLE_1IN, "auto")
    assert isinstance(result, ReferenceResult)
    assert result.kind is ReferenceKind.CIRCLE_1IN
    assert result.method == "auto"
    assert result.axis_ratio == pytest.approx(1.0)
    assert result.warnings == ()
    h = np.array(result.h_px_to_plane)
    assert apply_h(h, np.array([[400.0, 300.0]]))[0] == pytest.approx((0.0, 0.0), abs=1e-12)
    assert apply_h(h, np.array([[500.0, 300.0]]))[0] == pytest.approx((0.5, 0.0), abs=1e-12)
    assert apply_h(h, np.array([[400.0, 400.0]]))[0] == pytest.approx((0.0, 0.5), abs=1e-12)


@pytest.mark.parametrize(
    ("kind", "inches"),
    [
        (ReferenceKind.CIRCLE_1IN, 1.0),
        (ReferenceKind.CIRCLE_1_5IN, 1.5),
        (ReferenceKind.CIRCLE_3IN, 3.0),
    ],
)
def test_reference_scale_follows_the_kind(kind: ReferenceKind, inches: float) -> None:
    c = CircleCandidate((0.0, 0.0), (300.0, 300.0), 0.0, 1.0)
    h = np.array(reference_from_circle(c, kind, "manual").h_px_to_plane)
    assert apply_h(h, np.array([[150.0, 0.0]]))[0][0] == pytest.approx(inches / 2)


def test_tilted_circle_gets_a_warning_and_a_level_one_does_not() -> None:
    tilted = CircleCandidate((10.0, 10.0), (200.0, 190.0), 20.0, 0.9)
    result = reference_from_circle(tilted, ReferenceKind.CIRCLE_1IN, "manual")
    assert result.method == "manual"
    assert result.axis_ratio == pytest.approx(0.95)
    assert len(result.warnings) == 1
    assert "tilt" in result.warnings[0].lower()
    edge = CircleCandidate((10.0, 10.0), (200.0, 196.0), 20.0, 0.9)
    assert reference_from_circle(edge, ReferenceKind.CIRCLE_1IN, "auto").warnings == ()
    just_under = CircleCandidate((10.0, 10.0), (200.0, 195.9), 20.0, 0.9)
    assert reference_from_circle(just_under, ReferenceKind.CIRCLE_1IN, "auto").warnings


def test_axis_order_does_not_change_the_ratio() -> None:
    flipped = CircleCandidate((0.0, 0.0), (190.0, 200.0), 0.0, 1.0)
    assert reference_from_circle(flipped, ReferenceKind.CIRCLE_1IN, "auto").axis_ratio == (
        pytest.approx(0.95)
    )


@pytest.mark.parametrize(
    ("kind", "method", "axes"),
    [
        (ReferenceKind.GRID_1IN, "auto", (100.0, 100.0)),
        ("grid_2in", "auto", (100.0, 100.0)),
        (ReferenceKind.CIRCLE_1IN, "confirmed", (100.0, 100.0)),
        (ReferenceKind.CIRCLE_1IN, "auto", (0.0, 0.0)),
        (ReferenceKind.CIRCLE_1IN, "auto", (100.0, 0.0)),
    ],
)
def test_reference_from_circle_rejects_bad_arguments(kind, method, axes) -> None:
    with pytest.raises(ValueError):
        reference_from_circle(CircleCandidate((0.0, 0.0), axes, 0.0, 1.0), kind, method)


@pytest.mark.parametrize(
    "candidate",
    [
        CircleCandidate((float("nan"), 0.0), (100.0, 100.0), 0.0, 1.0),
        CircleCandidate((0.0, float("inf")), (100.0, 100.0), 0.0, 1.0),
        CircleCandidate((0.0, 0.0), (float("inf"), 100.0), 0.0, 1.0),
        CircleCandidate((0.0, 0.0), (float("nan"), float("nan")), 0.0, 1.0),
    ],
    ids=["nan-center", "inf-center", "inf-axis", "nan-axes"],
)
def test_reference_from_circle_rejects_non_finite_geometry(candidate) -> None:
    with pytest.raises(ValueError, match="finite"):
        reference_from_circle(candidate, ReferenceKind.CIRCLE_1IN, "manual")


# ----------------------------------------------------------------------------- candidates


def _paper_scene(color=GREEN, diameter=200.0, center=(400.37, 300.61), size=(1000, 800)):
    return Canvas(*size).disc(center, diameter, color)


@pytest.mark.parametrize("color", [GREEN, ORANGE, RED, BLACK, BLUE], ids=str)
def test_a_one_inch_disc_is_found_within_a_third_of_a_percent(color) -> None:
    img = _paper_scene(color).render()
    c = nearest(find_circle_candidates(img, ReferenceKind.CIRCLE_1IN), (400.37, 300.61))
    assert math.dist(c.center_px, (400.37, 300.61)) < 0.3
    assert abs(mean_diameter(c) - 200.0) <= 0.003 * 200.0
    assert min(c.axes_px) / max(c.axes_px) > 0.995
    assert 0.5 < c.score <= 1.0


@pytest.mark.parametrize("diameter", [150.0, 300.0, 450.0])
def test_scale_error_stays_under_a_third_of_a_percent_across_sizes(diameter: float) -> None:
    img = _paper_scene(ORANGE, diameter, size=(1600, 1200)).render(noise=3.0, seed=2)
    kind = ReferenceKind.CIRCLE_3IN
    c = nearest(find_circle_candidates(img, kind), (400.37, 300.61))
    assert abs(mean_diameter(c) - diameter) <= 0.003 * diameter
    assert math.dist(c.center_px, (400.37, 300.61)) < 0.3


def test_a_tilted_disc_is_recovered_as_an_ellipse() -> None:
    img = Canvas(1000, 800).ellipse((500.2, 400.7), (220.0, 176.0), 30.0, RED).render()
    c = nearest(find_circle_candidates(img, ReferenceKind.CIRCLE_1IN), (500.2, 400.7))
    assert c.axes_px[0] == pytest.approx(220.0, rel=0.005)
    assert c.axes_px[1] == pytest.approx(176.0, rel=0.005)
    assert abs(c.angle_deg - 30.0) < 2.0
    assert math.dist(c.center_px, (500.2, 400.7)) < 0.4
    assert reference_from_circle(c, ReferenceKind.CIRCLE_1IN, "auto").warnings


def test_noise_and_heavy_jpeg_keep_the_fit_within_half_a_percent() -> None:
    img = _paper_scene(GREEN, 200.0).render(noise=8.0, blur=1.5, jpeg=60, seed=5)
    c = nearest(find_circle_candidates(img, ReferenceKind.CIRCLE_1IN), (400.37, 300.61))
    assert abs(mean_diameter(c) - 200.0) <= 0.005 * 200.0


def _cardboard(width=1400, height=1000) -> Canvas:
    canvas = Canvas(width, height, background=(120, 158, 190))
    rng = np.random.default_rng(11)
    for x in range(0, width, 18):
        canvas.rect((x, 0), (x + 8, height), (104, 140, 172))
    for _ in range(6):
        p0 = rng.uniform([0, 0], [width, height])
        p1 = p0 + rng.uniform(-300, 300, 2)
        canvas.line(p0, p1, 3, (60, 80, 100))
    return canvas


def test_an_orange_disc_on_corrugated_cardboard_is_found() -> None:
    center, diameter = (700.4, 520.3), 380.0
    canvas = _cardboard().disc(center, diameter, (30, 120, 235))
    canvas.diamond(center, 90.0, BLACK)
    for dx, dy in ((150, 0), (-150, 0), (0, 150), (0, -150)):
        canvas.line(
            (center[0] + dx * 0.6, center[1] + dy * 0.6), (center[0] + dx, center[1] + dy), 4, BLACK
        )
    canvas.disc((300.0, 880.0), 22.0, (40, 40, 40))
    img = canvas.render(noise=5.0, blur=1.2, jpeg=85, seed=4)
    candidates = find_circle_candidates(img, ReferenceKind.CIRCLE_3IN)
    c = nearest(candidates, center)
    assert math.dist(c.center_px, center) < 0.6
    assert abs(mean_diameter(c) - diameter) <= 0.005 * diameter
    assert all(mean_diameter(x) > 100 for x in candidates)


def test_a_disc_with_a_diamond_inside_never_proposes_the_diamond() -> None:
    center = (500.0, 400.0)
    img = Canvas(1000, 800).disc(center, 360.0, ORANGE).diamond(center, 150.0, BLACK).render()
    for kind in (ReferenceKind.CIRCLE_1IN, ReferenceKind.CIRCLE_3IN):
        for c in find_circle_candidates(img, kind):
            assert mean_diameter(c) > 300.0


@pytest.mark.parametrize("angle", [0.0, 17.0, 45.0])
def test_a_printed_diamond_is_not_a_circle(angle: float) -> None:
    img = Canvas(1000, 800).diamond((500.0, 400.0), 220.0, BLACK, angle).render()
    for kind in (ReferenceKind.CIRCLE_1IN, ReferenceKind.CIRCLE_1_5IN, ReferenceKind.CIRCLE_3IN):
        assert find_circle_candidates(img, kind) == []


def test_a_square_is_not_a_circle() -> None:
    img = Canvas(1000, 800).rect((380, 280), (620, 520), GREEN).render()
    assert find_circle_candidates(img, ReferenceKind.CIRCLE_1IN) == []


def test_a_taped_cross_is_not_a_circle() -> None:
    canvas = Canvas(1000, 800)
    canvas.rect((380, 330), (620, 370), BLUE).rect((480, 230), (520, 470), BLUE)
    for x in range(0, 1000, 100):
        canvas.line((x, 0), (x, 800), 3, GREEN)
    img = canvas.render()
    assert find_circle_candidates(img, ReferenceKind.CIRCLE_1IN) == []


def test_blank_and_noise_only_images_have_no_candidates() -> None:
    blank = Canvas(800, 600).render(noise=0, blur=0, jpeg=0)
    noise = Canvas(800, 600).render(noise=25.0, jpeg=70, seed=9)
    for img in (blank, noise):
        assert find_circle_candidates(img, ReferenceKind.CIRCLE_1IN) == []


def test_a_disc_cut_off_by_the_image_edge_is_not_proposed() -> None:
    img = Canvas(1000, 800).disc((60.0, 400.0), 220.0, GREEN).render()
    assert find_circle_candidates(img, ReferenceKind.CIRCLE_1IN) == []


def test_a_disc_barely_clipped_by_the_edge_is_still_skipped_but_a_whole_one_is_found() -> None:
    clipped = Canvas(1000, 800).disc((105.0, 400.0), 220.0, GREEN).render()
    whole = Canvas(1000, 800).disc((115.0, 400.0), 220.0, GREEN).render()
    assert find_circle_candidates(clipped, ReferenceKind.CIRCLE_1IN) == []
    assert find_circle_candidates(whole, ReferenceKind.CIRCLE_1IN)


def test_a_strongly_eccentric_ellipse_is_not_a_circle_reference() -> None:
    img = Canvas(1000, 800).ellipse((500.0, 400.0), (300.0, 110.0), 20.0, GREEN).render()
    assert find_circle_candidates(img, ReferenceKind.CIRCLE_1IN) == []


def _ray_inputs(radius=100.0, window=8.0, step=0.25):
    ellipse = _Ellipse(200.0, 200.0, radius, radius, 0.0)
    t = np.linspace(0, 2 * math.pi, 144, endpoint=False)
    base, normals = ellipse.points(t)
    offsets = np.arange(-window, window + step / 2, step)
    return base, normals, offsets


@pytest.mark.parametrize(("hidden_fraction", "expected"), [(0.0, True), (0.25, True), (0.4, False)])
def test_ray_fit_needs_most_of_the_edge_to_be_visible(hidden_fraction, expected) -> None:
    base, normals, offsets = _ray_inputs()
    profiles = np.where(offsets < 0.3, 200.0, 40.0)[None, :].repeat(144, axis=0)
    profiles[: int(144 * hidden_fraction)] = 120.0
    fit = _fit_rays(profiles.astype(np.float32), offsets, base, normals, 100.0, (0, 0))
    assert (fit is not None) is expected
    if fit is not None:
        assert math.dist((fit.ellipse.cx, fit.ellipse.cy), (200.0, 200.0)) < 0.2
        assert fit.ellipse.a == pytest.approx(100.3, abs=0.2)


def test_ray_fit_rejects_edges_with_too_little_contrast() -> None:
    base, normals, offsets = _ray_inputs()
    profiles = np.where(offsets < 0.3, 126.0, 120.0)[None, :].repeat(144, axis=0)
    assert _fit_rays(profiles.astype(np.float32), offsets, base, normals, 100.0, (0, 0)) is None


def test_diameter_must_be_plausible_for_the_kind() -> None:
    tiny = Canvas(1000, 800).disc((500.0, 400.0), 20.0, BLACK).render()
    huge = Canvas(1000, 800).disc((500.0, 400.0), 760.0, GREEN).render()
    assert find_circle_candidates(tiny, ReferenceKind.CIRCLE_1IN) == []
    assert find_circle_candidates(huge, ReferenceKind.CIRCLE_1IN) == []
    assert find_circle_candidates(huge, ReferenceKind.CIRCLE_3IN)


def test_candidates_are_ranked_by_score_and_capped() -> None:
    canvas = Canvas(1400, 1000).disc((300.0, 300.0), 200.0, GREEN)
    canvas.disc((800.0, 300.0), 200.0, ORANGE).disc((300.0, 700.0), 200.0, BLUE)
    canvas.disc((800.0, 700.0), 200.0, (150, 160, 150))
    img = canvas.render()
    everything = find_circle_candidates(img, ReferenceKind.CIRCLE_1IN, max_candidates=10)
    assert len(everything) == 4
    assert [c.score for c in everything] == sorted((c.score for c in everything), reverse=True)
    assert find_circle_candidates(img, ReferenceKind.CIRCLE_1IN, max_candidates=2) == everything[:2]
    assert len(find_circle_candidates(img, ReferenceKind.CIRCLE_1IN)) == 4
    assert all(isinstance(c, CircleCandidate) for c in everything)
    assert all(0 < c.score <= 1 for c in everything)


def test_detection_is_deterministic() -> None:
    img = _paper_scene(GREEN).render(noise=6.0, seed=1)
    first = find_circle_candidates(img, ReferenceKind.CIRCLE_1IN)
    assert first == find_circle_candidates(img, ReferenceKind.CIRCLE_1IN)


def test_a_gray_image_is_accepted() -> None:
    gray = cv2.cvtColor(_paper_scene(BLACK).render(), cv2.COLOR_BGR2GRAY)
    assert find_circle_candidates(gray, ReferenceKind.CIRCLE_1IN)


@pytest.mark.parametrize(
    ("img", "kind", "count"),
    [
        (np.zeros((200, 200, 3), dtype=np.float32), ReferenceKind.CIRCLE_1IN, 5),
        (np.zeros((200, 200, 4), dtype=np.uint8), ReferenceKind.CIRCLE_1IN, 5),
        (np.zeros((20, 200, 3), dtype=np.uint8), ReferenceKind.CIRCLE_1IN, 5),
        (np.zeros((200, 200, 3), dtype=np.uint8), ReferenceKind.GRID_1IN, 5),
        (np.zeros((200, 200, 3), dtype=np.uint8), "circle_9in", 5),
        (np.zeros((200, 200, 3), dtype=np.uint8), ReferenceKind.CIRCLE_1IN, 0),
        ([[0]], ReferenceKind.CIRCLE_1IN, 5),
    ],
    ids=["float", "bgra", "tiny", "grid-kind", "unknown-kind", "zero-cap", "list"],
)
def test_find_circle_candidates_rejects_bad_arguments(img, kind, count) -> None:
    with pytest.raises(ValueError):
        find_circle_candidates(img, kind, count)


# ----------------------------------------------------------------------------- aim snap


def _snap(img, center, offset=(28.0, -22.0), radius=60.0):
    click = (center[0] + offset[0], center[1] + offset[1])
    return refine_circle_near(img, click, radius)


def test_snap_finds_the_center_of_a_printed_dot() -> None:
    center = (300.37, 200.62)
    img = Canvas(700, 500).disc(center, 100.0, GREEN).render()
    c = _snap(img, center)
    assert c is not None
    assert math.dist(c.center_px, center) < 0.5
    assert c.axes_px[0] == pytest.approx(100.0, abs=3.0)
    assert c.angle_deg == 0.0
    assert 0 < c.score <= 1


@pytest.mark.parametrize("angle", [0.0, 25.0, 45.0])
def test_snap_finds_the_center_of_a_diamond(angle: float) -> None:
    center = (310.41, 205.83)
    img = Canvas(700, 500).diamond(center, 120.0, BLACK, angle).render()
    c = _snap(img, center)
    assert c is not None
    assert math.dist(c.center_px, center) < 0.6
    assert c.axes_px[0] == pytest.approx(120.0 / math.sqrt(2), abs=4.0)


def test_snap_finds_the_junction_of_an_asymmetric_taped_cross() -> None:
    junction = (330.5, 240.25)
    canvas = Canvas(700, 500)
    canvas.rect((junction[0] - 70, junction[1] - 11), (junction[0] + 120, junction[1] + 11), BLUE)
    canvas.rect((junction[0] - 11, junction[1] - 55), (junction[0] + 11, junction[1] + 100), BLUE)
    img = canvas.render()
    c = _snap(img, junction)
    assert c is not None
    assert math.dist(c.center_px, junction) < 1.0


def test_snap_ignores_thin_grid_lines_touching_the_mark() -> None:
    junction = (330.5, 240.25)
    canvas = Canvas(700, 500)
    for x in range(20, 700, 60):
        canvas.line((x, 0), (x, 500), 3, GREEN)
    for y in range(10, 500, 60):
        canvas.line((0, y), (700, y), 3, GREEN)
    canvas.rect((junction[0] - 80, junction[1] - 11), (junction[0] + 80, junction[1] + 11), BLUE)
    canvas.rect((junction[0] - 11, junction[1] - 80), (junction[0] + 11, junction[1] + 80), BLUE)
    canvas.disc(junction, 44.0, BLUE)
    img = canvas.render(noise=4.0, jpeg=85, seed=3)
    c = _snap(img, junction, radius=90.0)
    assert c is not None
    assert math.dist(c.center_px, junction) < 1.0


def test_snap_survives_noise_blur_and_jpeg() -> None:
    center = (300.0, 200.0)
    img = (
        Canvas(700, 500)
        .diamond(center, 120.0, BLACK, 10.0)
        .render(noise=9.0, blur=1.5, jpeg=60, seed=6)
    )
    c = _snap(img, center)
    assert c is not None
    assert math.dist(c.center_px, center) < 1.5


def test_snap_prefers_the_big_dark_dot_over_a_bullet_hole_nearer_the_click() -> None:
    center, hole = (300.0, 250.0), (330.0, 245.0)
    canvas = Canvas(700, 500).disc(center, 360.0, BLACK).disc(hole, 24.0, (170, 190, 200))
    click = (332.0, 247.0)
    assert math.dist(click, hole) < 5 < math.dist(click, center) - 25
    c = refine_circle_near(canvas.render(), click, 60.0)
    assert c is not None
    assert math.dist(c.center_px, center) < 1.5
    assert c.axes_px[0] > 300


def test_snap_ignores_the_paper_ring_around_a_mark_that_nearly_fills_the_window() -> None:
    center = (400.0, 300.0)
    img = Canvas(800, 600).disc(center, 460.0, GREEN).render()
    click = (center[0] + 40.0, center[1] + 25.0)
    c = refine_circle_near(img, click, 140.0)
    assert c is not None
    assert math.dist(c.center_px, center) < 1.0


def test_snap_finds_a_diamond_inside_a_larger_colored_disc() -> None:
    center = (500.0, 400.0)
    img = Canvas(1000, 800).disc(center, 520.0, ORANGE).diamond(center, 140.0, BLACK).render()
    c = refine_circle_near(img, (520.0, 380.0), 60.0)
    assert c is not None
    assert math.dist(c.center_px, center) < 1.0
    assert c.axes_px[0] == pytest.approx(140.0 / math.sqrt(2), abs=4.0)


def test_snap_widens_the_window_for_a_mark_larger_than_the_search_radius() -> None:
    center = (400.0, 300.0)
    img = Canvas(800, 600).disc(center, 300.0, GREEN).render()
    c = refine_circle_near(img, (420.0, 310.0), 60.0)
    assert c is not None
    assert math.dist(c.center_px, center) < 1.0
    assert c.axes_px[0] == pytest.approx(300.0, abs=4.0)


def test_snap_returns_none_on_bare_paper() -> None:
    img = Canvas(700, 500).render(noise=3.0)
    assert refine_circle_near(img, (300.0, 200.0), 60.0) is None


def test_snap_returns_none_when_the_mark_is_out_of_reach() -> None:
    img = Canvas(900, 500).disc((700.0, 250.0), 100.0, GREEN).render()
    assert refine_circle_near(img, (200.0, 250.0), 60.0) is None
    assert refine_circle_near(img, (550.0, 250.0), 60.0) is None
    assert refine_circle_near(img, (650.0, 250.0), 60.0) is not None


def test_snap_works_near_the_image_border() -> None:
    center = (40.0, 40.0)
    img = Canvas(400, 300).disc(center, 70.0, GREEN).render()
    c = refine_circle_near(img, (50.0, 35.0), 50.0)
    assert c is not None
    assert math.dist(c.center_px, center) < 1.5


@pytest.mark.parametrize(
    ("point", "radius"),
    [
        ((-1.0, 50.0), 20.0),
        ((50.0, 5000.0), 20.0),
        ((float("nan"), 50.0), 20.0),
        ((50.0, 50.0), 0.0),
        ((50.0, 50.0), -3.0),
        ((50.0, 50.0), float("nan")),
        ((50.0, 50.0), float("inf")),
    ],
)
def test_snap_rejects_bad_arguments(point, radius) -> None:
    img = Canvas(300, 200).render()
    with pytest.raises(ValueError):
        refine_circle_near(img, point, radius)


def test_snap_rejects_a_bad_image() -> None:
    with pytest.raises(ValueError):
        refine_circle_near(np.zeros((200, 200), dtype=np.float64), (50.0, 50.0), 20.0)


def _contour_of(draw) -> np.ndarray:
    canvas = np.zeros((400, 400), dtype=np.uint8)
    draw(canvas)
    contours, _ = cv2.findContours(canvas, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    return max(contours, key=cv2.contourArea)[:, 0, :].astype(np.float64)


@pytest.mark.parametrize(
    ("name", "draw", "accepted"),
    [
        ("circle", lambda c: cv2.circle(c, (200, 200), 100, 255, -1), True),
        (
            "tilted-ellipse",
            lambda c: cv2.ellipse(c, (200, 200), (110, 90), 30, 0, 360, 255, -1),
            True,
        ),
        (
            "eccentric-ellipse",
            lambda c: cv2.ellipse(c, (200, 200), (150, 55), 0, 0, 360, 255, -1),
            False,
        ),
        (
            "diamond",
            lambda c: cv2.fillPoly(
                c, [np.array([[200, 90], [310, 200], [200, 310], [90, 200]])], 255
            ),
            False,
        ),
        ("square", lambda c: cv2.rectangle(c, (100, 100), (300, 300), 255, -1), False),
        (
            "cross",
            lambda c: (
                cv2.rectangle(c, (90, 180), (310, 220), 255, -1),
                cv2.rectangle(c, (180, 90), (220, 310), 255, -1),
            ),
            False,
        ),
    ],
)
def test_coarse_shape_test_accepts_only_round_ellipses(name, draw, accepted) -> None:
    coarse = _validate_coarse(_contour_of(draw))
    assert (coarse is not None) is accepted, name


def _disc_mask(size=160, radius=60.0):
    yy, xx = np.mgrid[0:size, 0:size]
    dist = np.hypot(xx - size / 2, yy - size / 2)
    angle = np.degrees(np.arctan2(yy - size / 2, xx - size / 2)) % 360
    return dist <= radius, angle


@pytest.mark.parametrize(("removed_degrees", "usable"), [(0, True), (60, True), (200, False)])
def test_round_edge_fit_needs_most_of_the_outline_to_be_clean(removed_degrees, usable) -> None:
    disc, angle = _disc_mask()
    sector = disc & (angle < removed_degrees)
    fit = _round_edge_fit(disc & ~sector, sector)
    assert (fit is not None) is usable
    if fit is not None:
        radius, center = fit
        assert center == pytest.approx((80.0, 80.0), abs=0.6)
        assert radius == pytest.approx(60.0, abs=0.8)


# ----------------------------------------------------------------------------- degenerate shapes

DEGENERATE_SHAPES = [(64, 1400), (1400, 64), (70, 1500), (1500, 70), (64, 937_500), (937_500, 64)]


@pytest.mark.parametrize("shape", DEGENERATE_SHAPES)
@pytest.mark.parametrize("channels", [None, 3])
def test_extreme_aspect_ratios_raise_value_error_not_an_opencv_error(shape, channels):
    img = np.full((*shape, channels) if channels else shape, 200, dtype=np.uint8)
    with pytest.raises(ValueError, match="aspect ratio"):
        find_circle_candidates(img, ReferenceKind.CIRCLE_1IN)
    with pytest.raises(ValueError, match="aspect ratio"):
        refine_circle_near(img, (shape[1] / 2, shape[0] / 2), 30.0)


def test_an_aspect_ratio_of_exactly_twenty_to_one_is_still_processed():
    img = np.full((64, 1280, 3), 200, dtype=np.uint8)
    assert find_circle_candidates(img, ReferenceKind.CIRCLE_1IN) == []


def test_just_over_twenty_to_one_is_rejected():
    with pytest.raises(ValueError, match="aspect ratio"):
        find_circle_candidates(
            np.full((64, 1281, 3), 200, dtype=np.uint8), ReferenceKind.CIRCLE_1IN
        )


@pytest.mark.parametrize("shape", [(63, 640, 3), (640, 10, 3), (0, 100, 3), (100, 0, 3), (5, 5)])
def test_images_below_the_minimum_side_raise_value_error(shape):
    with pytest.raises(ValueError):
        find_circle_candidates(np.zeros(shape, dtype=np.uint8), ReferenceKind.CIRCLE_1IN)


# ----------------------------------------------------------------------------- paster: outer disc first (U1)


def _paster(center=(450.3, 520.7), outer=400.0, ring=200.0) -> np.ndarray:
    """Orange paster with a printed black ring and diamond, crosshair slits, a shot bite and a red bar."""
    cx, cy = center
    canvas = Canvas(900, 1100)
    canvas.rect((cx - 60, 0), (cx + 60, cy), RED)
    canvas.disc(center, outer, ORANGE)
    canvas.disc(center, ring + 8.0, BLACK).disc(center, ring, ORANGE)
    canvas.diamond(center, ring * 0.45, BLACK)
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        canvas.line(
            (cx + dx * ring * 0.6, cy + dy * ring * 0.6),
            (cx + dx * (outer / 2 + 6), cy + dy * (outer / 2 + 6)),
            5,
            BLACK,
        )
    canvas.disc((cx - 40.0, cy - outer / 2 - 4.0), 40.0, PAPER)
    return canvas.render(noise=3.0, blur=1.0, jpeg=90, seed=3)


@pytest.mark.parametrize("kind", [ReferenceKind.CIRCLE_1_5IN, ReferenceKind.CIRCLE_3IN])
def test_a_paster_proposes_its_outer_disc_first_and_the_inner_ring_second(kind) -> None:
    center, outer, ring = (450.3, 520.7), 400.0, 200.0
    found = find_circle_candidates(_paster(center, outer, ring), kind)
    assert len(found) >= 2
    first, second = found[0], found[1]
    assert math.dist(first.center_px, center) < 1.0
    assert abs(mean_diameter(first) - outer) <= 0.01 * outer
    assert abs(mean_diameter(second) - ring) <= 0.03 * ring
    assert math.dist(second.center_px, center) < 1.0


def test_the_one_inch_kind_still_gets_only_the_candidates_that_fit_its_size() -> None:
    found = find_circle_candidates(_paster(), ReferenceKind.CIRCLE_1IN)
    assert found
    assert all(mean_diameter(c) < 0.4 * 900 for c in found)


def _refined(cx: float, diameter: float, score: float) -> _Refined:
    r = diameter / 2
    return _Refined(_Ellipse(cx, 100.0, r, r, 0.0), score)


def test_nested_discs_are_ordered_outermost_first_at_the_best_members_rank() -> None:
    outer, inner = _refined(100.0, 400.0, 0.6), _refined(101.0, 150.0, 0.9)
    elsewhere = _refined(900.0, 300.0, 0.8)
    ordered = _outer_first(sorted([outer, inner, elsewhere], key=lambda r: -r.score))
    assert ordered == [outer, inner, elsewhere]


def test_discs_that_are_not_nested_keep_score_order() -> None:
    beside = _refined(300.0, 150.0, 0.9)
    big = _refined(100.0, 400.0, 0.6)
    off_center = _refined(100.0 + 0.5 * 200, 150.0, 0.95)
    assert _outer_first([off_center, beside, big]) == [off_center, beside, big]
    similar = _refined(100.0, 360.0, 0.99)
    assert _outer_first([similar, big]) == [similar, big]


def _grid_with(px_per_inch: float) -> GridCandidate:
    h = np.diag([1.0 / px_per_inch, 1.0 / px_per_inch, 1.0])
    return GridCandidate(np.zeros((0, 2)), np.zeros((0, 2), dtype=int), h, 0.01, 1.0)


@pytest.mark.parametrize(
    ("px_per_inch", "kind", "expected_first"),
    [
        (400.0 / 3.0, ReferenceKind.CIRCLE_3IN, 400.0),
        (400.0 / 3.0, ReferenceKind.CIRCLE_1_5IN, 200.0),
        (400.0 / 1.5, ReferenceKind.CIRCLE_1_5IN, 400.0),
        (200.0 / 1.5, ReferenceKind.CIRCLE_1_5IN, 200.0),
    ],
)
def test_the_candidate_that_measures_the_kinds_size_in_grid_inches_goes_first(
    px_per_inch, kind, expected_first
) -> None:
    img = _paster()
    found = find_circle_candidates(img, kind, grid=_grid_with(px_per_inch))
    assert abs(mean_diameter(found[0]) - expected_first) <= 0.03 * expected_first


def test_a_grid_that_matches_no_candidate_changes_nothing() -> None:
    img = _paster()
    plain = find_circle_candidates(img, ReferenceKind.CIRCLE_1_5IN)
    assert find_circle_candidates(img, ReferenceKind.CIRCLE_1_5IN, grid=_grid_with(37.0)) == plain


def test_no_grid_means_the_unchanged_ranking() -> None:
    img = _paster()
    assert find_circle_candidates(img, ReferenceKind.CIRCLE_3IN) == find_circle_candidates(
        img, ReferenceKind.CIRCLE_3IN, grid=None
    )


def _slotted_disc(slots: int, slot_deg: float, depth: float = 0.9) -> np.ndarray:
    """A disc of diameter 400 with `slots` paper wedges cut from its rim to `1 - depth` of the radius."""
    center, radius = (500.0, 400.0), 200.0
    canvas = Canvas(1000, 800).disc(center, 2 * radius, ORANGE)
    for k in range(slots):
        mid = 2 * math.pi * k / slots
        half = math.radians(slot_deg) / 2
        pts = [
            (
                center[0] + r * math.cos(mid + sign * half),
                center[1] + r * math.sin(mid + sign * half),
            )
            for r, sign in ((radius * (1 - depth), 0), (radius + 4, -1), (radius + 4, 1))
        ]
        canvas.polygon(pts, PAPER)
    return canvas.render()


def test_a_disc_whose_outline_is_mostly_bites_is_not_a_reference() -> None:
    found = find_circle_candidates(_slotted_disc(12, 6.0), ReferenceKind.CIRCLE_3IN)
    assert all(mean_diameter(c) < 300.0 for c in found)


def test_a_disc_with_a_few_thin_slits_is_still_the_reference() -> None:
    found = find_circle_candidates(_slotted_disc(4, 1.5), ReferenceKind.CIRCLE_3IN)
    assert found
    assert abs(mean_diameter(found[0]) - 400.0) <= 0.01 * 400.0


def test_diameter_in_inches_averages_chords_through_an_anisotropic_homography() -> None:
    from rga.vision.reference_circle import _diameter_in

    h = np.diag([0.01, 0.02, 1.0])
    inches = _diameter_in(h, _Ellipse(300.0, 300.0, 100.0, 100.0, 0.0))
    assert inches == pytest.approx((2.0 + 2 * math.hypot(1.414213, 2.828427) + 4.0) / 4, abs=0.01)
