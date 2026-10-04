import cv2
import numpy as np
import pytest

from rga.domain.types import CircleCandidate, PlanePoint, WorkingFrame
from rga.vision.transform import (
    apply_h,
    build_working,
    homography_from_points,
    plane_to_working_px,
    similarity_from_circle,
    working_px_to_plane,
)

TOL = 1e-6

# A mild perspective map from a 1200x900 px photo to a ~12x9 in plane region (about 100 px/in).
PERSPECTIVE_SRC = np.array([[0, 0], [1200, 0], [1200, 900], [0, 900]], dtype=float)
PERSPECTIVE_DST = np.array([[0.0, 0.0], [12.0, 0.4], [11.6, 9.0], [0.3, 8.7]])


def _perspective_h():
    return homography_from_points(PERSPECTIVE_SRC, PERSPECTIVE_DST)


def _similarity_h(px_per_in: float) -> np.ndarray:
    s = 1.0 / px_per_in
    return np.array([[s, 0, 0], [0, s, 0], [0, 0, 1.0]])


def _dot_image(shape_hw, dot_xy, radius):
    img = np.zeros(shape_hw, dtype=np.uint8)
    cv2.circle(img, (round(dot_xy[0]), round(dot_xy[1])), radius, 255, -1)
    return img


def _centroid(img):
    weights = img.astype(np.float64)
    ys, xs = np.indices(img.shape)
    return np.array([(xs * weights).sum(), (ys * weights).sum()]) / weights.sum()


# ---------------------------------------------------------------- apply_h


def test_apply_h_identity_and_translation():
    pts = np.array([[1.0, 2.0], [-3.0, 4.5]])
    assert np.allclose(apply_h(np.eye(3), pts), pts, atol=TOL)
    shift = np.array([[1, 0, 5], [0, 1, -2], [0, 0, 1.0]])
    assert np.allclose(apply_h(shift, pts), pts + [5, -2], atol=TOL)


def test_apply_h_perspective_matches_manual_formula():
    h = np.array([[1.0, 0.1, 2.0], [0.0, 1.2, 1.0], [0.001, 0.002, 1.0]])
    x, y = 30.0, 40.0
    w = 0.001 * x + 0.002 * y + 1.0
    expected = [(1.0 * x + 0.1 * y + 2.0) / w, (1.2 * y + 1.0) / w]
    assert apply_h(h, np.array([[x, y]]))[0] == pytest.approx(expected, abs=TOL)


def test_apply_h_round_trip_through_inverse():
    h = _perspective_h()
    pts = np.random.default_rng(0).uniform(0, 1000, size=(50, 2))
    back = apply_h(np.linalg.inv(h), apply_h(h, pts))
    assert np.abs(back - pts).max() <= TOL


def test_apply_h_accepts_tuple_matrix_like_reference_result():
    h = tuple(tuple(row) for row in np.eye(3))
    assert apply_h(h, np.array([[1.0, 2.0]])).tolist() == [[1.0, 2.0]]


def test_apply_h_empty_points():
    assert apply_h(np.eye(3), np.empty((0, 2))).shape == (0, 2)


@pytest.mark.parametrize("pts", [np.array([1.0, 2.0]), np.ones((3, 3)), np.array([[np.nan, 1.0]])])
def test_apply_h_rejects_bad_points(pts):
    with pytest.raises(ValueError, match="points"):
        apply_h(np.eye(3), pts)


def test_apply_h_rejects_bad_matrix():
    with pytest.raises(ValueError, match="3x3"):
        apply_h(np.eye(2), np.array([[0.0, 0.0]]))
    with pytest.raises(ValueError, match="3x3"):
        apply_h(np.full((3, 3), np.inf), np.array([[0.0, 0.0]]))


def test_apply_h_rejects_point_at_infinity():
    h = np.array([[1.0, 0, 0], [0, 1.0, 0], [1.0, 0, 0]])  # w = x
    with pytest.raises(ValueError, match="infinity"):
        apply_h(h, np.array([[0.0, 1.0]]))


# ---------------------------------------------------------------- similarity_from_circle


def test_similarity_scale_is_diameter_over_mean_axes():
    c = CircleCandidate(center_px=(500.0, 400.0), axes_px=(98.0, 102.0), angle_deg=0.0, score=1.0)
    h = similarity_from_circle(c, 1.5)
    assert h[0, 0] == pytest.approx(1.5 / 100.0, abs=1e-12)
    assert h[1, 1] == pytest.approx(1.5 / 100.0, abs=1e-12)
    assert h[0, 1] == h[1, 0] == 0.0
    assert h[2].tolist() == [0.0, 0.0, 1.0]


def test_similarity_maps_center_to_origin_and_edge_to_half_diameter():
    c = CircleCandidate(center_px=(500.0, 400.0), axes_px=(120.0, 120.0), angle_deg=0.0, score=1.0)
    h = similarity_from_circle(c, 3.0)
    assert apply_h(h, np.array([[500.0, 400.0]]))[0] == pytest.approx([0.0, 0.0], abs=TOL)
    right, below = apply_h(h, np.array([[560.0, 400.0], [500.0, 460.0]]))
    assert right == pytest.approx([1.5, 0.0], abs=TOL)
    assert below == pytest.approx([0.0, 1.5], abs=TOL)  # y stays down


def test_similarity_round_trip():
    c = CircleCandidate(center_px=(321.5, 123.25), axes_px=(77.0, 81.0), angle_deg=30.0, score=0.9)
    h = similarity_from_circle(c, 1.0)
    pts = np.array([[10.0, 20.0], [900.0, 700.0]])
    assert np.abs(apply_h(np.linalg.inv(h), apply_h(h, pts)) - pts).max() <= TOL


@pytest.mark.parametrize("diameter", [0.0, -1.0, float("nan")])
def test_similarity_rejects_bad_diameter(diameter):
    c = CircleCandidate((0.0, 0.0), (10.0, 10.0), 0.0, 1.0)
    with pytest.raises(ValueError, match="diameter"):
        similarity_from_circle(c, diameter)


def test_similarity_rejects_zero_axes():
    c = CircleCandidate((0.0, 0.0), (0.0, 0.0), 0.0, 1.0)
    with pytest.raises(ValueError, match="axes"):
        similarity_from_circle(c, 1.0)


# ---------------------------------------------------------------- homography_from_points


def test_homography_four_points_is_exact_and_normalized():
    h = _perspective_h()
    assert h[2, 2] == pytest.approx(1.0)
    mapped = apply_h(h, PERSPECTIVE_SRC)
    assert np.abs(mapped - PERSPECTIVE_DST).max() <= TOL


def test_homography_round_trip_px_plane_px():
    h = _perspective_h()
    pts = np.random.default_rng(1).uniform(0, [1200, 900], size=(40, 2))
    back = apply_h(np.linalg.inv(h), apply_h(h, pts))
    assert np.abs(back - pts).max() <= TOL


def _truth_h():
    return np.array([[0.01, 0.0008, -1.0], [-0.0005, 0.011, 0.5], [1e-6, 2e-6, 1.0]])


def test_homography_ransac_recovers_truth_despite_outliers():
    truth = _truth_h()
    rng = np.random.default_rng(2)
    src = rng.uniform(0, [1200, 900], size=(16, 2))
    dst = apply_h(truth, src)
    dst[[2, 7, 11]] += [[3.0, -2.0], [-4.0, 5.0], [2.5, 6.0]]  # gross outliers (inches)
    h = homography_from_points(src, dst)
    probe = rng.uniform(0, [1200, 900], size=(30, 2))
    assert np.abs(apply_h(h, probe) - apply_h(truth, probe)).max() < 1e-3


def test_homography_more_than_four_clean_points_round_trips():
    truth = _truth_h()
    src = np.random.default_rng(3).uniform(0, [1200, 900], size=(9, 2))
    h = homography_from_points(src, apply_h(truth, src))
    assert np.abs(apply_h(np.linalg.inv(h), apply_h(h, src)) - src).max() <= TOL


def test_homography_ransac_tolerates_small_noise():
    truth = _truth_h()
    rng = np.random.default_rng(4)
    src = rng.uniform(0, [1200, 900], size=(25, 2))
    dst = apply_h(truth, src) + rng.normal(0, 0.003, size=(25, 2))
    h = homography_from_points(src, dst)
    assert np.abs(apply_h(h, src) - apply_h(truth, src)).max() < 0.02


def test_homography_rejects_too_few_points():
    with pytest.raises(ValueError, match="at least 4"):
        homography_from_points(PERSPECTIVE_SRC[:3], PERSPECTIVE_DST[:3])


def test_homography_rejects_mismatched_shapes():
    with pytest.raises(ValueError, match="same shape"):
        homography_from_points(PERSPECTIVE_SRC, PERSPECTIVE_DST[:3])


def test_homography_rejects_collinear_points():
    src = np.array([[0.0, 0], [1, 0], [2, 0], [3, 0]])
    dst = np.array([[0.0, 0], [1, 1], [2, 2], [3, 3]])
    with pytest.raises(ValueError, match="degenerate"):
        homography_from_points(src, dst)


# ---------------------------------------------------------------- build_working


@pytest.mark.parametrize(
    ("px_per_in", "expected_ppi", "expected_size"),
    [
        (100.0, 100.0, (1000, 500)),  # native below cap: ppi = native
        (200.0, 200.0, (1000, 500)),  # exactly at cap
        (400.0, 200.0, (500, 250)),  # capped, image shrinks
    ],
)
def test_build_working_ppi_cap(px_per_in, expected_ppi, expected_size):
    img = np.full((500, 1000), 128, dtype=np.uint8)
    working, frame, _ = build_working(img, _similarity_h(px_per_in))
    assert frame.ppi == pytest.approx(expected_ppi)
    assert (frame.width_px, frame.height_px) == expected_size
    assert working.shape[:2] == (frame.height_px, frame.width_px)


def test_build_working_long_side_cap():
    img = np.zeros((1000, 6000, 3), dtype=np.uint8)
    working, frame, _ = build_working(img, _similarity_h(150.0))  # 40 in wide: 200 ppi would exceed
    assert frame.width_px == 4000
    assert frame.ppi == pytest.approx(4000 / 40.0)
    assert max(working.shape[:2]) <= 4000
    assert working.shape == (frame.height_px, frame.width_px, 3)


def test_build_working_respects_custom_caps():
    img = np.zeros((300, 600), dtype=np.uint8)
    _, frame, _ = build_working(img, _similarity_h(100.0), ppi_cap=50, max_side=10_000)
    assert frame.ppi == pytest.approx(50.0)
    _, frame, _ = build_working(img, _similarity_h(100.0), max_side=300)
    assert max(frame.width_px, frame.height_px) <= 300


def test_build_working_translation_puts_content_at_origin():
    h = _similarity_h(100.0)
    h[0, 2], h[1, 2] = -7.0, 3.0  # arbitrary plane origin
    _, frame, h_work = build_working(np.zeros((500, 1000), dtype=np.uint8), h)
    corners = apply_h(h_work, np.array([[0.0, 0.0], [1000.0, 500.0]]))
    assert corners[0] == pytest.approx([0.0, 0.0], abs=TOL)
    assert corners[1] == pytest.approx([frame.width_px, frame.height_px], abs=1.0)


@pytest.mark.parametrize(
    ("h_factory", "dot", "radius"),
    [
        (lambda: _similarity_h(100.0), (300.0, 200.0), 6),
        (lambda: _similarity_h(400.0), (700.0, 300.0), 10),  # capped to 200 ppi
        (lambda: _similarity_h(1000.0), (800.0, 350.0), 24),  # pre-shrink path, tight tolerance
        (_perspective_h, (400.0, 600.0), 6),
    ],
    ids=["native", "capped", "heavy_shrink", "perspective"],
)
def test_build_working_h_matches_warped_pixels(h_factory, dot, radius):
    h = h_factory()
    img = _dot_image((900, 1200), dot, radius=radius)
    working, frame, h_work = build_working(img, h)
    expected = apply_h(h_work, np.array([dot]))[0]
    tolerance = 0.06 if radius == 24 else 0.75
    assert _centroid(working) == pytest.approx(expected, abs=tolerance)
    assert 0 <= expected[0] < frame.width_px and 0 <= expected[1] < frame.height_px


def test_build_working_plane_coordinates_agree_with_reference_homography():
    h = _perspective_h()
    _, frame, h_work = build_working(np.zeros((900, 1200), dtype=np.uint8), h)
    a, b = np.array([[100.0, 150.0], [1000.0, 700.0]])
    plane_a, plane_b = working_px_to_plane(apply_h(h_work, np.array([a, b])), frame)
    ref_a, ref_b = apply_h(h, np.array([a, b]))
    assert plane_b.x_in - plane_a.x_in == pytest.approx(ref_b[0] - ref_a[0], abs=TOL)
    assert plane_b.y_in - plane_a.y_in == pytest.approx(ref_b[1] - ref_a[1], abs=TOL)


def test_build_working_preserves_channels_and_dtype():
    img = np.zeros((90, 120, 3), dtype=np.uint8)
    working, _, _ = build_working(img, _similarity_h(40.0))
    assert working.dtype == np.uint8
    assert working.ndim == 3


def test_build_working_perspective_frame_is_consistent():
    img = np.zeros((900, 1200, 3), dtype=np.uint8)
    working, frame, h_work = build_working(img, _perspective_h())
    assert working.shape == (frame.height_px, frame.width_px, 3)
    assert frame.ppi <= 200
    warped_corners = apply_h(h_work, np.array([[0, 0], [1200, 0], [1200, 900], [0, 900]], float))
    assert warped_corners.min() >= -TOL
    assert warped_corners[:, 0].max() <= frame.width_px + 1
    assert warped_corners[:, 1].max() <= frame.height_px + 1


def test_build_working_rejects_empty_image():
    with pytest.raises(ValueError, match="non-empty"):
        build_working(np.zeros((0, 10), dtype=np.uint8), np.eye(3))


def test_build_working_rejects_bad_homography():
    img = np.zeros((10, 10), dtype=np.uint8)
    with pytest.raises(ValueError, match="3x3"):
        build_working(img, np.eye(2))


def test_build_working_rejects_singular_homography():
    singular = np.array([[1.0, 0, 0], [0, 0.0, 0], [0, 0, 1.0]])
    with pytest.raises(ValueError):
        build_working(np.zeros((10, 10), dtype=np.uint8), singular)


def test_build_working_rejects_homography_through_infinity():
    h = np.array([[0.01, 0, 0], [0, 0.01, 0], [0.01, 0, 1.0]])  # w = 1 + 0.01x crosses 0 at x=-100
    h[2, 0] = -0.01  # w = 1 - 0.01x -> zero at x=100, inside a 200 px image
    with pytest.raises(ValueError, match="infinity|behind"):
        build_working(np.zeros((100, 200), dtype=np.uint8), h)


@pytest.mark.parametrize(("kwargs"), [{"ppi_cap": 0}, {"max_side": 0}])
def test_build_working_rejects_bad_caps(kwargs):
    with pytest.raises(ValueError, match="positive"):
        build_working(np.zeros((10, 10), dtype=np.uint8), _similarity_h(10.0), **kwargs)


# ---------------------------------------------------------------- working px <-> plane


def test_px_plane_conversions_are_exact_inverses():
    frame = WorkingFrame(ppi=137.25, width_px=1000, height_px=800)
    pts = np.random.default_rng(5).uniform(0, 1000, size=(100, 2))
    plane = working_px_to_plane(pts, frame)
    back = plane_to_working_px(plane, frame)
    assert np.abs(back - pts).max() <= 1e-9
    again = working_px_to_plane(back, frame)
    assert all(
        a.x_in == pytest.approx(b.x_in, abs=1e-12) and a.y_in == pytest.approx(b.y_in, abs=1e-12)
        for a, b in zip(plane, again, strict=True)
    )


def test_px_to_plane_is_px_over_ppi_with_y_down():
    frame = WorkingFrame(ppi=200.0, width_px=400, height_px=400)
    (p,) = working_px_to_plane(np.array([[100.0, 300.0]]), frame)
    assert p == PlanePoint(0.5, 1.5)  # larger px y -> larger plane y (no flip)
    assert plane_to_working_px([PlanePoint(0.5, 1.5)], frame).tolist() == [[100.0, 300.0]]


def test_px_plane_empty_inputs():
    frame = WorkingFrame(ppi=100.0, width_px=10, height_px=10)
    assert working_px_to_plane(np.empty((0, 2)), frame) == []
    assert plane_to_working_px([], frame).shape == (0, 2)


def test_working_px_to_plane_rejects_bad_points():
    frame = WorkingFrame(ppi=100.0, width_px=10, height_px=10)
    with pytest.raises(ValueError, match="points"):
        working_px_to_plane(np.array([1.0, 2.0, 3.0]), frame)
