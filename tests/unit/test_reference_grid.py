"""T1 tests for rga.vision.reference_grid: detection on self-rendered grids, the manual fallback, and warnings."""

import math

import cv2
import numpy as np
import pytest

from rga.domain.types import ReferenceKind, ReferenceResult
from rga.vision import reference_grid as rg
from rga.vision.reference_grid import (
    NON_PLANAR_RMS_IN,
    NON_PLANAR_WARNING,
    PARTIAL_GRID_WARNING,
    GridCandidate,
    find_grid,
    grid_from_manual,
    reference_from_grid,
)
from rga.vision.reference_grid_lines import LineLattice, find_line_lattice
from rga.vision.transform import apply_h

COLORS_BGR = {
    "blue": (200, 150, 90),
    "black": (25, 25, 25),
    "red": (40, 50, 220),
    "green": (60, 170, 40),
    "dark_gray": (70, 70, 70),
}
PAPER_BGR = (236, 238, 240)
PX_QUAD = np.array([[110, 90], [1010, 130], [980, 1330], [95, 1290]], dtype=np.float64)


def _plane_to_px() -> np.ndarray:
    plane = np.array([[0, 0], [8, 0], [8, 10], [0, 10]], dtype=np.float64)
    return cv2.getPerspectiveTransform(plane.astype(np.float32), PX_QUAD.astype(np.float32)).astype(
        np.float64
    )


def render_grid(
    *,
    color: str = "green",
    thickness: int = 3,
    across: int = 8,
    down: int = 10,
    band: bool = False,
    clutter: bool = False,
    bow_in: float = 0.0,
    rotate: int = 0,
    noise: float = 2.0,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """Render a grid photo; returns (BGR image, 3x3 plane-to-px homography before rotation)."""
    h = _plane_to_px()
    rng = np.random.default_rng(seed)
    img = np.full((1400, 1100, 3), PAPER_BGR, dtype=np.uint8)

    def to_px(pts: np.ndarray) -> np.ndarray:
        pts = np.asarray(pts, dtype=np.float64).copy()
        if bow_in:
            pts[:, 1] += bow_in * (1 - ((pts[:, 0] - 4.0) / 4.0) ** 2)
        return apply_h(h, pts)

    def polyline(a, b, col, th):
        t = np.linspace(0, 1, 60)[:, None]
        pts = to_px(np.asarray(a) + t * (np.asarray(b) - np.asarray(a)))
        fixed = np.round(pts * 16).astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(img, [fixed], False, col, th, cv2.LINE_AA, shift=4)

    if band:
        corners = to_px(np.array([[3.2, -0.2], [4.4, -0.2], [4.4, down + 0.2], [3.2, down + 0.2]]))
        cv2.fillPoly(img, [np.round(corners).astype(np.int32)], COLORS_BGR[color])
    for i in range(across + 1):
        polyline((i, 0), (i, down), COLORS_BGR[color], thickness)
    for j in range(down + 1):
        polyline((0, j), (across, j), COLORS_BGR[color], thickness)
    if clutter:
        x0, y0 = apply_h(h, np.array([[5.3, 7.4]]))[0]
        for r in range(13):
            for c in range(13):
                if rng.random() < 0.5:
                    cv2.rectangle(
                        img,
                        (int(x0 + c * 8), int(y0 + r * 8)),
                        (int(x0 + c * 8 + 7), int(y0 + r * 8 + 7)),
                        (20, 20, 20),
                        -1,
                    )
        cv2.putText(
            img, "GRID 1 IN  QR 12 345", (150, 1360), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (30, 30, 30), 2
        )
        for hx, hy in ((2.5, 3.5), (3.4, 4.6), (5.5, 2.4)):
            cv2.circle(
                img,
                tuple(np.round(apply_h(h, np.array([[hx, hy]]))[0]).astype(int)),
                12,
                (30, 30, 30),
                -1,
            )
    img = cv2.GaussianBlur(img, (0, 0), 0.8)
    if noise:
        img = np.clip(img + rng.normal(0, noise, img.shape), 0, 255).astype(np.uint8)
    if rotate:
        code = {
            90: cv2.ROTATE_90_CLOCKWISE,
            180: cv2.ROTATE_180,
            270: cv2.ROTATE_90_COUNTERCLOCKWISE,
        }[rotate]
        img = cv2.rotate(img, code)
    return img, h


def _rotate_h(h: np.ndarray, rotate: int, width: int = 1100, height: int = 1400) -> np.ndarray:
    """Plane-to-px homography after cv2.rotate by 0/90/180/270 degrees clockwise."""
    if rotate == 0:
        return h
    r = {
        90: np.array([[0, -1, height - 1], [1, 0, 0], [0, 0, 1]], dtype=np.float64),
        180: np.array([[-1, 0, width - 1], [0, -1, height - 1], [0, 0, 1]], dtype=np.float64),
        270: np.array([[0, 1, 0], [-1, 0, width - 1], [0, 0, 1]], dtype=np.float64),
    }[rotate]
    return r @ h


def alignment(h_true_plane_to_px: np.ndarray, g: GridCandidate) -> tuple[float, float, float]:
    """Similarity fit from true plane inches to the candidate's plane: (angle deg, scale, max residual in)."""
    truth = np.array(
        [[x, y] for x in np.linspace(1, 7, 7) for y in np.linspace(1, 9, 9)], dtype=np.float64
    )
    ours = apply_h(g.h_px_to_plane, apply_h(h_true_plane_to_px, truth))
    tc, oc = truth - truth.mean(axis=0), ours - ours.mean(axis=0)
    u, s, vt = np.linalg.svd(oc.T @ tc)
    d = np.sign(np.linalg.det(u @ vt))
    rot = u @ np.diag([1, d]) @ vt
    scale = float((s * [1, d]).sum() / (tc**2).sum())
    fitted = scale * tc @ rot.T
    err = np.linalg.norm(oc - fitted, axis=1).max()
    return math.degrees(math.atan2(rot[1, 0], rot[0, 0])), scale, float(err)


SPARSE_QUAD = np.array([[60, 60], [1400, 90], [1380, 1020], [80, 990]], dtype=np.float64)


def render_sparse_grid_with_clutter(seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """A few large grid cells under a ring, an orange disc, a QR-like patch and jagged holes."""
    rng = np.random.default_rng(seed)
    plane = np.array([[0, 0], [4, 0], [4, 3], [0, 3]], dtype=np.float32)
    h = cv2.getPerspectiveTransform(plane, SPARSE_QUAD.astype(np.float32)).astype(np.float64)
    img = np.full((1100, 1500, 3), (214, 218, 220), dtype=np.uint8)

    def px(pt) -> tuple[int, int]:
        return tuple(int(v) for v in np.round(apply_h(h, np.array([pt], dtype=np.float64))[0]))

    for i in range(5):
        cv2.line(img, px((i, 0)), px((i, 3)), (25, 25, 25), 4, cv2.LINE_AA)
    for j in range(4):
        cv2.line(img, px((0, j)), px((4, j)), (25, 25, 25), 4, cv2.LINE_AA)
    cv2.circle(img, px((2.5, 1.5)), 250, (25, 25, 25), 9, cv2.LINE_AA)
    cv2.circle(img, px((2.5, 1.5)), 130, (30, 60, 220), -1, cv2.LINE_AA)
    qx, qy = px((3.15, 1.6))
    for r in range(21):
        for c in range(21):
            finder = (r < 7 and c < 7) or (r < 7 and c > 13) or (r > 13 and c < 7)
            if finder or rng.random() < 0.5:
                cv2.rectangle(
                    img,
                    (qx + c * 14, qy + r * 14),
                    (qx + c * 14 + 13, qy + r * 14 + 13),
                    (15, 15, 15),
                    -1,
                )
    for _ in range(14):
        cx, cy = int(rng.integers(150, 1000)), int(rng.integers(150, 900))
        pts = [
            (cx + math.cos(a) * rng.uniform(26, 40), cy + math.sin(a) * rng.uniform(26, 40))
            for a in np.linspace(0, 2 * math.pi, 14, endpoint=False)
        ]
        cv2.fillPoly(img, [np.round(np.array(pts)).astype(np.int32)], (30, 40, 50))
        cv2.circle(img, (cx, cy), 14, (150, 170, 190), -1)
    img = cv2.GaussianBlur(img, (0, 0), 0.9)
    return np.clip(img + rng.normal(0, 3, img.shape), 0, 255).astype(np.uint8), h


# ------------------------------------------------------------------------------ detection


@pytest.mark.parametrize("color", sorted(COLORS_BGR))
def test_finds_the_grid_for_any_line_colour(color: str) -> None:
    img, h = render_grid(color=color)
    g = find_grid(img)
    assert g is not None
    angle, scale, err = alignment(h, g)
    assert abs(angle) < 0.1 and abs(scale - 1) < 0.002 and err < 0.01
    assert g.rms_residual_in < 0.01
    assert len(g.intersections_px) >= 80


def test_candidate_has_the_design_contract_shapes() -> None:
    img, _ = render_grid()
    g = find_grid(img)
    assert g is not None
    n = len(g.intersections_px)
    assert g.intersections_px.shape == (n, 2) and g.intersections_px.dtype == np.float64
    assert g.lattice_idx.shape == (n, 2) and np.issubdtype(g.lattice_idx.dtype, np.integer)
    assert g.h_px_to_plane.shape == (3, 3)
    assert tuple(g.lattice_idx.min(axis=0)) == (0, 0)
    assert tuple(g.lattice_idx.max(axis=0)) == (8, 10)
    assert 0.0 < g.score <= 1.0
    assert len(np.unique(g.lattice_idx, axis=0)) == n


def test_lattice_indices_are_consistent_with_the_homography() -> None:
    img, _ = render_grid()
    g = find_grid(img)
    assert g is not None
    mapped = apply_h(g.h_px_to_plane, g.intersections_px)
    assert np.abs(mapped - g.lattice_idx).max() < 0.02


def test_one_inch_scale_is_exact_on_a_clean_perspective_grid() -> None:
    img, h = render_grid(color="black")
    g = find_grid(img)
    assert g is not None
    p0 = apply_h(h, np.array([[2.0, 3.0]]))
    p1 = apply_h(h, np.array([[5.0, 3.0]]))
    q = apply_h(g.h_px_to_plane, np.vstack([p0, p1]))
    assert np.linalg.norm(q[1] - q[0]) == pytest.approx(3.0, abs=0.01)


@pytest.mark.parametrize("rotate", [180, 90, 270])
def test_rotated_sheets_keep_plane_axes_aligned_with_the_image(rotate: int) -> None:
    img, h = render_grid(color="green", rotate=rotate)
    g = find_grid(img)
    assert g is not None
    u = apply_h(np.linalg.inv(g.h_px_to_plane), np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]]))
    ex, ey = u[1] - u[0], u[2] - u[0]
    assert ex[0] > 0.8 * np.linalg.norm(ex) and ey[1] > 0.8 * np.linalg.norm(ey)
    angle, scale, err = alignment(_rotate_h(h, rotate), g)
    assert abs(scale - 1) < 0.002 and err < 0.01
    assert abs(abs(angle) - {180: 180, 90: 90, 270: 90}[rotate]) < 0.2


def test_orientation_is_never_mirrored() -> None:
    for rotate in (0, 180):
        img, _ = render_grid(rotate=rotate)
        g = find_grid(img)
        assert g is not None
        assert (
            np.linalg.det(
                g.h_px_to_plane[:2, :2] - np.outer(g.h_px_to_plane[:2, 2], g.h_px_to_plane[2, :2])
            )
            > 0
        )


def test_thick_band_text_qr_and_holes_do_not_break_detection() -> None:
    img, h = render_grid(color="red", band=True, clutter=True, seed=3)
    g = find_grid(img)
    assert g is not None
    _, scale, err = alignment(h, g)
    assert abs(scale - 1) < 0.003 and err < 0.02
    assert reference_from_grid(g, "auto").warnings == ()


def test_thin_black_grid_with_clutter_on_a_noisy_photo() -> None:
    img, h = render_grid(color="black", thickness=2, clutter=True, noise=5.0, seed=5)
    g = find_grid(img)
    assert g is not None
    assert alignment(h, g)[2] < 0.02


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_sparse_true_grid_beats_denser_clutter(seed: int) -> None:
    img, h = render_sparse_grid_with_clutter(seed)
    g = find_grid(img)
    assert g is not None
    assert len(g.intersections_px) >= 12
    assert g.rms_residual_in < 0.003
    assert tuple(np.ptp(g.lattice_idx, axis=0)) in {(4, 3), (3, 3), (4, 2)}
    truth = np.array(
        [[x, y] for x in (0.5, 1.5, 2.5, 3.5) for y in (0.5, 1.5, 2.5)], dtype=np.float64
    )
    ours = apply_h(g.h_px_to_plane, apply_h(h, truth))
    tc, oc = truth - truth.mean(axis=0), ours - ours.mean(axis=0)
    assert np.linalg.norm(oc - tc, axis=1).max() < 0.05, (
        "plane is not a pure translation of the true plane"
    )


def test_seeds_inside_an_already_grown_lattice_are_not_grown_again(monkeypatch) -> None:
    img, _ = render_grid(color="green")
    calls: list[int] = []
    original = rg._propagate

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(rg, "_propagate", counting)
    assert find_grid(img) is not None
    assert len(calls) <= 3


def test_final_fit_ignores_a_few_mismatched_crossings() -> None:
    h = _plane_to_px()
    idx = np.array([(i, j) for i in range(9) for j in range(11)], dtype=int)
    px = apply_h(h, idx.astype(np.float64))
    corrupted = px.copy()
    for k in (7, 30, 55, 80):
        corrupted[k] += np.array([40.0, -35.0])
    g = rg._assemble(corrupted, idx, 1.0)
    assert g is not None
    probe = apply_h(h, np.array([[1.5, 2.5], [4.0, 5.0], [6.5, 8.0]]))
    mapped = apply_h(g.h_px_to_plane, probe)
    assert (
        np.abs(
            (mapped - mapped[0]) - (np.array([[1.5, 2.5], [4.0, 5.0], [6.5, 8.0]]) - [1.5, 2.5])
        ).max()
        < 0.005
    )
    assert g.rms_residual_in > NON_PLANAR_RMS_IN, (
        "rms covers every matched crossing, so the mismatches show up as non-planar"
    )


def test_work_scale_crossings_map_back_with_the_resize_pixel_centre_convention() -> None:
    idx = np.array([(i, j) for i in range(4) for j in range(4)], dtype=int)
    work_px = idx * 40.0 + 100.0
    g = rg._assemble(work_px, idx, 0.5)
    assert g is not None
    assert np.allclose(g.intersections_px, (work_px + 0.5) / 0.5 - 0.5)


def test_detection_ignores_opencv_global_rng_and_thread_state() -> None:
    img, _ = render_grid(clutter=True, seed=2)
    baseline = find_grid(img)
    assert baseline is not None
    threads = cv2.getNumThreads()
    try:
        for rng_seed, n_threads in ((1, 1), (12345, 2), (2**31 - 1, 8)):
            cv2.setRNGSeed(rng_seed)
            cv2.setNumThreads(n_threads)
            again = find_grid(img)
            assert again is not None
            assert np.array_equal(again.h_px_to_plane, baseline.h_px_to_plane)
            assert np.array_equal(again.intersections_px, baseline.intersections_px)
            assert again.rms_residual_in == baseline.rms_residual_in
    finally:
        cv2.setNumThreads(threads)


def test_detection_is_deterministic() -> None:
    img, _ = render_grid(clutter=True, seed=2)
    a, b = find_grid(img), find_grid(img)
    assert a is not None and b is not None
    assert np.array_equal(a.h_px_to_plane, b.h_px_to_plane)
    assert np.array_equal(a.lattice_idx, b.lattice_idx)


def test_large_photos_are_handled_through_the_downscaled_working_image() -> None:
    img, h = render_grid(color="green")
    big = cv2.resize(img, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC)
    g = find_grid(big)
    assert g is not None
    scale_h = np.diag([2.5, 2.5, 1.0])
    scale_h[:2, 2] = 1.25 - 0.5  # pixel-centre convention of cv2.resize
    angle, scale, err = alignment(scale_h @ h, g)
    assert abs(angle) < 0.1 and abs(scale - 1) < 0.003 and err < 0.01


def test_grayscale_and_bgra_inputs_are_accepted() -> None:
    img, h = render_grid(color="black")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    bgra = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    for variant in (gray, bgra):
        g = find_grid(variant)
        assert g is not None and alignment(h, g)[2] < 0.01


# ------------------------------------------------------------------------------ rejection


def test_rejects_a_grid_too_small_for_long_thin_lines() -> None:
    """Two squares across and one down: fewer than nine crossings, and every line is far shorter than a
    quarter of the image, so neither the crossing search nor the line fallback accepts it."""
    img, _ = render_grid(across=2, down=1)
    assert find_grid(img) is None


def test_accepts_the_smallest_lattice_of_nine_crossings() -> None:
    img, h = render_grid(across=2, down=2)
    g = find_grid(img)
    assert g is not None and len(g.intersections_px) == rg.MIN_INLIERS == 9
    assert alignment(h, g)[2] < 0.03


def test_rejects_a_single_row_of_crossings() -> None:
    img, _ = render_grid(across=12, down=1)
    assert find_grid(img) is None


@pytest.mark.parametrize("kind", ["blank", "noise", "texture", "circles"])
def test_images_without_a_grid_return_none(kind: str) -> None:
    rng = np.random.default_rng(1)
    if kind == "blank":
        img = np.full((900, 1200, 3), 235, np.uint8)
    elif kind == "noise":
        img = np.clip(rng.normal(150, 12, (900, 1200, 3)), 0, 255).astype(np.uint8)
    elif kind == "texture":
        img = cv2.GaussianBlur(rng.integers(0, 255, (900, 1200, 3), dtype=np.uint8), (0, 0), 3)
    else:
        img = np.full((900, 1200, 3), 235, np.uint8)
        for cx, cy, r in ((300, 300, 80), (700, 500, 150), (1000, 250, 40)):
            cv2.circle(img, (cx, cy), r, (30, 30, 30), 6)
            cv2.circle(img, (cx, cy), r // 3, (30, 30, 200), -1)
    assert find_grid(img) is None


def test_isolated_stripes_that_are_not_a_lattice_return_none() -> None:
    img = np.full((900, 1200, 3), 235, np.uint8)
    for x in (100, 340, 620, 1010):
        cv2.line(img, (x, 0), (x + 30, 899), (30, 30, 30), 3)
    assert find_grid(img) is None


@pytest.mark.parametrize(
    "bad",
    [
        np.zeros((100, 100, 3), dtype=np.float32),
        np.zeros((100, 100, 2), dtype=np.uint8),
        np.zeros((100, 100, 3, 1), dtype=np.uint8),
        np.zeros((10, 10, 3), dtype=np.uint8),
        np.zeros((0, 0, 3), dtype=np.uint8),
        "image.jpg",
        None,
    ],
)
def test_bad_inputs_raise_value_error(bad) -> None:
    with pytest.raises(ValueError):
        find_grid(bad)


# ------------------------------------------------------------------------------ planarity


def test_flat_paper_gets_no_non_planar_warning() -> None:
    img, _ = render_grid(color="green")
    g = find_grid(img)
    assert g is not None
    assert g.rms_residual_in <= NON_PLANAR_RMS_IN
    assert reference_from_grid(g, "auto").warnings == ()


def test_curved_paper_gets_the_non_planar_warning() -> None:
    img, _ = render_grid(color="green", bow_in=0.35)
    g = find_grid(img)
    assert g is not None
    assert g.rms_residual_in > NON_PLANAR_RMS_IN
    assert reference_from_grid(g, "auto").warnings == (NON_PLANAR_WARNING,)


def test_mild_curvature_below_the_threshold_is_not_flagged() -> None:
    img, _ = render_grid(color="green", bow_in=0.04)
    g = find_grid(img)
    assert g is not None
    assert g.rms_residual_in < NON_PLANAR_RMS_IN
    assert reference_from_grid(g, "auto").warnings == ()


def test_non_planar_warning_text_asks_for_flattening_and_a_reshoot() -> None:
    assert NON_PLANAR_WARNING.startswith("non_planar")
    assert "flatten the target and re-shoot" in NON_PLANAR_WARNING


def _candidate(rms: float) -> GridCandidate:
    return grid_candidate_with_rms(rms)


def grid_candidate_with_rms(rms: float) -> GridCandidate:
    base = grid_from_manual([(0, 0), (80, 0), (80, 80), (0, 80)], 2, 2)
    return GridCandidate(
        intersections_px=base.intersections_px,
        lattice_idx=base.lattice_idx,
        h_px_to_plane=base.h_px_to_plane,
        rms_residual_in=rms,
        score=0.5,
    )


def test_a_non_finite_residual_is_treated_as_non_planar() -> None:
    assert reference_from_grid(_candidate(float("nan")), "auto").warnings == (NON_PLANAR_WARNING,)
    assert reference_from_grid(_candidate(float("inf")), "auto").warnings == (NON_PLANAR_WARNING,)


def test_warning_threshold_boundary_is_exactly_the_named_constant() -> None:
    assert reference_from_grid(_candidate(NON_PLANAR_RMS_IN), "auto").warnings == ()
    above = reference_from_grid(_candidate(math.nextafter(NON_PLANAR_RMS_IN, 1.0)), "auto")
    assert above.warnings == (NON_PLANAR_WARNING,)


# ------------------------------------------------------------------------------ manual fallback


MANUAL_CORNERS = [(110.0, 90.0), (1010.0, 130.0), (980.0, 1330.0), (95.0, 1290.0)]


def test_manual_grid_maps_the_clicked_corners_exactly() -> None:
    g = grid_from_manual(MANUAL_CORNERS, 8, 10)
    mapped = apply_h(g.h_px_to_plane, np.array(MANUAL_CORNERS))
    assert np.allclose(mapped, [[0, 0], [8, 0], [8, 10], [0, 10]], atol=1e-9)
    assert g.rms_residual_in == 0.0
    assert np.array_equal(g.lattice_idx, [[0, 0], [8, 0], [8, 10], [0, 10]])
    assert np.array_equal(g.intersections_px, np.array(MANUAL_CORNERS))


def test_manual_grid_is_exact_for_interior_points_of_a_true_perspective() -> None:
    h = _plane_to_px()
    corners = apply_h(h, np.array([[0, 0], [8, 0], [8, 10], [0, 10]], dtype=np.float64))
    g = grid_from_manual(corners, 8, 10)
    inner = apply_h(g.h_px_to_plane, apply_h(h, np.array([[3.0, 4.0], [6.5, 8.25]])))
    assert np.allclose(inner, [[3.0, 4.0], [6.5, 8.25]], atol=1e-9)


def test_manual_grid_agrees_with_automatic_detection() -> None:
    img, h = render_grid(color="green")
    auto = find_grid(img)
    assert auto is not None
    corners = apply_h(h, np.array([[0, 0], [8, 0], [8, 10], [0, 10]], dtype=np.float64))
    manual = grid_from_manual(corners, 8, 10)
    probe = apply_h(h, np.array([[1.5, 2.5], [6.0, 8.0], [4.0, 5.0]]))
    a, m = apply_h(auto.h_px_to_plane, probe), apply_h(manual.h_px_to_plane, probe)
    assert np.allclose(a - a[0], m - m[0], atol=0.005)


def test_manual_grid_accepts_numpy_corners_and_numpy_integers() -> None:
    g = grid_from_manual(np.array(MANUAL_CORNERS), np.int64(3), np.int32(2))
    assert np.array_equal(g.lattice_idx[2], [3, 2])


def test_manual_grid_works_on_a_sheet_photographed_upside_down() -> None:
    h = _rotate_h(_plane_to_px(), 180)
    plane = np.array([[0, 0], [8, 0], [8, 10], [0, 10]], dtype=np.float64)
    px = apply_h(h, plane)
    displayed = [px[2], px[3], px[0], px[1]]  # TL, TR, BR, BL as seen on screen
    g = grid_from_manual(displayed, 8, 10)
    assert np.allclose(
        apply_h(g.h_px_to_plane, np.array(displayed)), [[0, 0], [8, 0], [8, 10], [0, 10]], atol=1e-9
    )


@pytest.mark.parametrize(
    ("corners", "sx", "sy"),
    [
        (MANUAL_CORNERS[:3], 8, 10),
        ([*MANUAL_CORNERS, (1.0, 1.0)], 8, 10),
        (MANUAL_CORNERS[::-1], 8, 10),
        ([MANUAL_CORNERS[1], MANUAL_CORNERS[0], MANUAL_CORNERS[3], MANUAL_CORNERS[2]], 8, 10),
        ([(0, 0), (100, 0), (200, 0), (300, 0)], 8, 10),
        ([(0, 0), (100, 0), (100, 100), (50, 50)], 8, 10),
        ([(0, 0), (100, 0), (100, 100), (0, float("nan"))], 8, 10),
        (MANUAL_CORNERS, 0, 10),
        (MANUAL_CORNERS, 8, 0),
        (MANUAL_CORNERS, 101, 10),
        (MANUAL_CORNERS, 8, -1),
        (MANUAL_CORNERS, 2.5, 10),
        (MANUAL_CORNERS, True, 10),
    ],
)
def test_manual_grid_rejects_bad_corners_and_counts(corners, sx, sy) -> None:
    with pytest.raises(ValueError):
        grid_from_manual(corners, sx, sy)


# ------------------------------------------------------------------------------ reference result


def test_reference_from_grid_builds_a_grid_reference_result() -> None:
    g = grid_from_manual(MANUAL_CORNERS, 8, 10)
    ref = reference_from_grid(g, "manual")
    assert isinstance(ref, ReferenceResult)
    assert ref.kind is ReferenceKind.GRID_1IN
    assert ref.method == "manual"
    assert ref.axis_ratio is None
    assert ref.warnings == ()
    assert isinstance(ref.h_px_to_plane, tuple) and all(
        isinstance(row, tuple) for row in ref.h_px_to_plane
    )
    assert np.allclose(np.array(ref.h_px_to_plane), g.h_px_to_plane)


def test_reference_from_grid_marks_automatic_results() -> None:
    img, _ = render_grid()
    g = find_grid(img)
    assert g is not None
    assert reference_from_grid(g, "auto").method == "auto"


@pytest.mark.parametrize("method", ["", "AUTO", "semi", None])
def test_reference_from_grid_rejects_unknown_methods(method) -> None:
    g = grid_from_manual(MANUAL_CORNERS, 8, 10)
    with pytest.raises(ValueError):
        reference_from_grid(g, method)


# ----------------------------------------------------------------------------- degenerate shapes

DEGENERATE_IMAGES = [
    ((h, w), channels)
    for h, w in [(40, 900), (900, 40), (64, 1400), (1400, 64)]
    for channels in (None, 3, 4)
] + [((64, 937_500), None), ((937_500, 64), None)]


@pytest.mark.parametrize(("shape", "channels"), DEGENERATE_IMAGES)
def test_extreme_aspect_ratios_raise_value_error_not_an_opencv_error(shape, channels):
    img = np.full((*shape, channels) if channels else shape, 200, dtype=np.uint8)
    with pytest.raises(ValueError, match="aspect ratio"):
        find_grid(img)


def test_an_aspect_ratio_of_exactly_twenty_to_one_is_still_processed():
    assert find_grid(np.full((40, 800), 200, dtype=np.uint8)) is None


def test_just_over_twenty_to_one_is_rejected():
    with pytest.raises(ValueError, match="aspect ratio"):
        find_grid(np.full((40, 801), 200, dtype=np.uint8))


@pytest.mark.parametrize("shape", [(31, 640), (640, 10), (0, 100), (100, 0)])
def test_images_below_the_minimum_side_raise_value_error(shape):
    with pytest.raises(ValueError):
        find_grid(np.zeros(shape, dtype=np.uint8))


# ------------------------------------------------------------------------------ partial thin lines

PARTIAL_UNIT_PX = 480


def render_partial_grid(
    color: str = "red", rotate_deg: float = 0.0, bow_px: float = 0.0
) -> np.ndarray:
    """A portrait 1 inch grid of 480 px cells that crossings alone cannot reconstruct (8 are visible): a
    240 px wide band of the line colour over the middle column, an orange paster, and blank sheet
    edges that cut every line."""
    height, width, unit = 2000, 1500, PARTIAL_UNIT_PX
    ink = COLORS_BGR[color]
    img = np.full((height, width, 3), PAPER_BGR, dtype=np.uint8)
    for i in range(-1, 4):
        cv2.line(img, (230 + i * unit, 0), (230 + i * unit, height), ink, 9)
    for j in range(-1, 5):
        cv2.line(img, (0, 170 + j * unit), (width, 170 + j * unit), ink, 9)
    cv2.rectangle(img, (230 + unit - 120, 0), (230 + unit + 120, height), ink, -1)
    cv2.circle(img, (230 + unit, 650), 150, (20, 110, 250), -1)
    for edge in (img[:, :60], img[:, -60:], img[:60], img[-60:]):
        edge[:] = PAPER_BGR
    if bow_px:
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        img = cv2.remap(
            img,
            xx + bow_px * np.sin(yy / height * 2 * np.pi).astype(np.float32),
            yy + bow_px * np.sin(xx / width * 2 * np.pi).astype(np.float32),
            cv2.INTER_LINEAR,
            borderValue=PAPER_BGR,
        )
    if rotate_deg:
        m = cv2.getRotationMatrix2D((width / 2, height / 2), rotate_deg, 1.0)
        img = cv2.warpAffine(img, m, (width, height), borderValue=PAPER_BGR)
    return img


def px_per_inch(g: GridCandidate, shape: tuple[int, ...]) -> tuple[float, float]:
    """Pixels per plane inch along plane x and y at the image centre."""
    centre = apply_h(g.h_px_to_plane, np.array([[shape[1] / 2, shape[0] / 2]]))[0]
    back = apply_h(
        np.linalg.inv(g.h_px_to_plane), np.array([centre, centre + [1, 0], centre + [0, 1]])
    )
    return float(np.linalg.norm(back[1] - back[0])), float(np.linalg.norm(back[2] - back[0]))


@pytest.mark.parametrize("rotate_deg", [0.0, 4.0, -7.0])
@pytest.mark.parametrize("color", ["red", "black", "green", "blue"])
def test_finds_the_lattice_from_thin_lines_around_a_band_and_a_paster(
    color: str, rotate_deg: float
) -> None:
    img = render_partial_grid(color, rotate_deg)
    g = find_grid(img)
    assert g is not None
    assert len(g.intersections_px) >= 4
    for ppi in px_per_inch(g, img.shape):
        assert ppi == pytest.approx(PARTIAL_UNIT_PX, rel=0.03)
    assert g.rms_residual_in <= NON_PLANAR_RMS_IN
    assert reference_from_grid(g, "auto").warnings == (PARTIAL_GRID_WARNING,)


def test_the_band_is_not_a_line_and_its_hidden_column_still_gets_its_index() -> None:
    g = find_grid(render_partial_grid("red"))
    assert g is not None
    columns = sorted({int(c) for c in g.lattice_idx[:, 0]})
    assert columns[-1] - columns[0] == len(columns)  # the column under the band is skipped
    xs = g.intersections_px[:, 0]
    assert not np.any((xs > 230 + PARTIAL_UNIT_PX - 100) & (xs < 230 + PARTIAL_UNIT_PX + 100))


@pytest.mark.parametrize("bow_px", [45.0, 60.0])
def test_a_strongly_bowed_partial_grid_is_never_accepted_as_planar(bow_px: float) -> None:
    g = find_grid(render_partial_grid("red", bow_px=bow_px))
    assert g is None or reference_from_grid(g, "auto").warnings == (NON_PLANAR_WARNING,)


def test_line_straightness_grows_with_the_bow_of_the_lines() -> None:
    def straightness(bow_px: float) -> float:
        dark = rg._as_gray_min(render_partial_grid("red", bow_px=bow_px))
        response, k = rg._line_response(dark)
        found = find_line_lattice(rg._line_mask(response, k), response, k)
        assert found is not None
        return found.straightness_px

    flat, mild, bowed = straightness(0.0), straightness(10.0), straightness(20.0)
    assert flat < 1.0 < mild < bowed


def test_a_partial_grid_labels_its_lattice_without_mirroring() -> None:
    img = render_partial_grid("black", 3.0)
    g = find_grid(img)
    assert g is not None
    centre = np.array([[img.shape[1] / 2, img.shape[0] / 2]])
    p0, px, py = apply_h(g.h_px_to_plane, np.vstack([centre, centre + [1, 0], centre + [0, 1]]))
    dx, dy = px - p0, py - p0
    assert dx[0] * dy[1] - dx[1] * dy[0] > 0
    assert dx[0] > 0 and dy[1] > 0


def _lines_image(xs: list[int], ys: list[int], band: bool = False) -> np.ndarray:
    img = np.full((1500, 2000, 3), PAPER_BGR, dtype=np.uint8)
    for x in xs:
        cv2.line(img, (x, 0), (x, 1500), COLORS_BGR["red"], 9)
    for y in ys:
        cv2.line(img, (0, y), (2000, y), COLORS_BGR["red"], 9)
    if band:
        cv2.rectangle(img, (700, 0), (1000, 1500), COLORS_BGR["red"], -1)
    return img


def _hash_mark_image() -> np.ndarray:
    img = np.full((1500, 2000, 3), PAPER_BGR, dtype=np.uint8)
    for x in (700, 1100):
        cv2.line(img, (x, 300), (x, 1200), COLORS_BGR["red"], 9)
    for y in (600, 900):
        cv2.line(img, (500, y), (1300, y), COLORS_BGR["red"], 9)
    return img


def _hidden_middle_lines_image() -> np.ndarray:
    """A 210 px/in grid whose middle line in each direction is under a band: two lines per family."""
    img = _lines_image([500, 920], [400, 820])
    cv2.rectangle(img, (630, 0), (790, 1500), COLORS_BGR["red"], -1)
    cv2.rectangle(img, (0, 530), (2000, 690), COLORS_BGR["red"], -1)
    return img


@pytest.mark.parametrize(
    "case",
    [
        "band_only",
        "one_vertical_three_horizontal",
        "rectangle_of_four_lines_two_to_one",
        "uneven_spacing",
        "no_vertical_lines",
        "lone_square_of_four_lines",
        "hash_mark",
        "hidden_middle_line_in_each_direction",
    ],
)
def test_line_evidence_that_is_not_a_lattice_returns_none(case: str) -> None:
    img = {
        "band_only": _lines_image([], [], band=True),
        "one_vertical_three_horizontal": _lines_image([400], [200, 680, 1160]),
        "rectangle_of_four_lines_two_to_one": _lines_image([300, 1260], [200, 680]),
        "uneven_spacing": _lines_image([300, 1260], [200, 680, 1000]),
        "no_vertical_lines": _lines_image([], [200, 680, 1160]),
        "lone_square_of_four_lines": _lines_image([300, 780], [200, 680]),
        "hash_mark": _hash_mark_image(),
        "hidden_middle_line_in_each_direction": _hidden_middle_lines_image(),
    }[case]
    assert find_grid(img) is None


def test_two_lines_per_direction_never_give_a_unit_even_when_square_spaced() -> None:
    for xs, ys in (([300, 780], [200, 680]), ([300, 1260], [200, 680]), ([300, 1260], [200, 1160])):
        assert find_grid(_lines_image(xs, ys)) is None


def test_three_lines_in_one_direction_are_enough_to_measure_the_unit() -> None:
    g = find_grid(_lines_image([300, 780], [200, 680, 1160]))
    assert g is not None
    assert px_per_inch(g, (1500, 2000))[0] == pytest.approx(480, rel=0.03)
    assert reference_from_grid(g, "auto").warnings == (PARTIAL_GRID_WARNING,)


def test_a_full_grid_keeps_the_crossing_search_result() -> None:
    img, h = render_grid()
    g = find_grid(img)
    assert g is not None and len(g.intersections_px) > rg.MIN_INLIERS
    assert alignment(h, g)[2] < 0.03


def test_the_partial_grid_fixture_defeats_the_crossing_search() -> None:
    img = render_partial_grid("red")
    dark = rg._as_gray_min(img)
    response, k = rg._line_response(dark)
    mask = rg._line_mask(response, k)
    assert mask is not None
    assert rg._crossing_lattice(dark, response, mask, k, 1.0) is None


def test_lines_shorter_than_a_quarter_of_the_image_are_not_evidence() -> None:
    img = np.full((1500, 2000, 3), PAPER_BGR, dtype=np.uint8)
    for x in (600, 740, 880):
        cv2.line(img, (x, 480), (x, 820), COLORS_BGR["red"], 9)
    for y in (540, 680, 820 - 20):
        cv2.line(img, (570, y), (910, y), COLORS_BGR["red"], 9)
    dark = rg._as_gray_min(img)
    response, k = rg._line_response(dark)
    mask = rg._line_mask(response, k)
    assert mask is not None
    assert find_line_lattice(mask, response, k) is None


def test_both_families_must_agree_on_the_unit_so_cells_stay_square() -> None:
    img = _lines_image([300, 780, 1260], [200, 440, 680])
    dark = rg._as_gray_min(img)
    response, k = rg._line_response(dark)
    mask = rg._line_mask(response, k)
    assert mask is not None
    found = find_line_lattice(mask, response, k)
    assert found is not None
    assert sorted({int(i) for i in found.lattice_idx[:, 0]}) == [0, 2, 4]
    assert sorted({int(i) for i in found.lattice_idx[:, 1]}) == [0, 1, 2]
    assert found.unit_px == pytest.approx(240, rel=0.03)


def test_line_straightness_enters_the_residual_and_triggers_the_non_planar_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    img = render_partial_grid("red")
    dark = rg._as_gray_min(img)
    response, k = rg._line_response(dark)
    mask = rg._line_mask(response, k)
    flat = find_line_lattice(mask, response, k)
    assert flat is not None
    wavy = LineLattice(flat.crossings_px, flat.lattice_idx, flat.unit_px, 0.2 * flat.unit_px)
    monkeypatch.setattr(rg, "find_line_lattice", lambda *_: wavy)
    candidate = rg._line_lattice(response, mask, k, 1.0)
    assert candidate is not None
    assert candidate.rms_residual_in == pytest.approx(0.2)
    assert NON_PLANAR_WARNING in reference_from_grid(candidate, "auto").warnings
    monkeypatch.setattr(rg, "find_line_lattice", lambda *_: flat)
    straight = rg._line_lattice(response, mask, k, 1.0)
    assert straight is not None and straight.rms_residual_in <= NON_PLANAR_RMS_IN


def test_the_coarsest_unit_that_explains_the_lines_wins() -> None:
    img = _lines_image([300, 780, 1260], [200, 680, 1160])
    dark = rg._as_gray_min(img)
    response, k = rg._line_response(dark)
    mask = rg._line_mask(response, k)
    assert mask is not None
    found = find_line_lattice(mask, response, k)
    assert found is not None and len(found.crossings_px) == 9
    assert sorted({int(i) for i in found.lattice_idx[:, 0]}) == [0, 1, 2]
    assert found.unit_px == pytest.approx(480, rel=0.03)


def test_the_partial_grid_warning_text_asks_the_user_to_check_one_square() -> None:
    assert PARTIAL_GRID_WARNING == "Grid found from partial lines — check that one square is 1 inch"


def test_every_fallback_result_carries_the_partial_warning_and_crossing_results_do_not() -> None:
    partial = find_grid(render_partial_grid("green", 2.0))
    assert partial is not None and partial.from_partial_lines
    assert reference_from_grid(partial, "auto").warnings == (PARTIAL_GRID_WARNING,)
    for img, _ in (render_grid(), render_grid(color="black", band=True)):
        found = find_grid(img)
        assert found is not None and not found.from_partial_lines
        assert PARTIAL_GRID_WARNING not in reference_from_grid(found, "auto").warnings
    manual = grid_from_manual(MANUAL_CORNERS, 8, 10)
    assert not manual.from_partial_lines
    assert reference_from_grid(manual, "manual").warnings == ()


def test_a_partial_grid_that_is_also_bowed_carries_both_warnings() -> None:
    base = find_grid(render_partial_grid("red"))
    assert base is not None
    bowed = GridCandidate(
        base.intersections_px,
        base.lattice_idx,
        base.h_px_to_plane,
        2 * NON_PLANAR_RMS_IN,
        base.score,
        True,
    )
    assert reference_from_grid(bowed, "auto").warnings == (
        NON_PLANAR_WARNING,
        PARTIAL_GRID_WARNING,
    )


def test_the_pipeline_proposal_for_a_partial_grid_carries_the_warning() -> None:
    from rga.vision.pipeline import propose_candidates

    [proposal] = propose_candidates(render_partial_grid("blue"), ReferenceKind.GRID_1IN)
    assert proposal.result.warnings == (PARTIAL_GRID_WARNING,)
