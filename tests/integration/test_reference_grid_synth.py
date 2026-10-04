"""T2 tests: grid reference detection on the synthetic catalog (truth from tests/synth) and on real photos."""

import math
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from rga.vision.reference_grid import (
    NON_PLANAR_WARNING,
    PARTIAL_GRID_WARNING,
    find_grid,
    grid_from_manual,
    reference_from_grid,
)
from rga.vision.transform import apply_h, build_working
from tests.synth.generate import load_catalog, plane_to_image, render_named

PLANAR_GRID_SCENARIOS = ["clean_grid", "skew10_grid", "skew15_grid"]
TRAIT_GRID_SCENARIOS = [
    "black_grid_light_holes_qr",
    "red_grid_band_light_diamond",
    "green_grid_upside_down_staples",
    "tape_cross_multi_aim",
]
SEEDS = (0, 1, 2)
REAL_PHOTOS = Path(__file__).resolve().parents[2] / "data" / "real_photos"


def _grid_scenarios() -> list[str]:
    catalog = load_catalog()
    return [n for n, sc in catalog.items() if (sc.get("reference") or {}).get("kind") == "grid"]


def _non_grid_scenarios() -> list[str]:
    catalog = load_catalog()
    return [n for n, sc in catalog.items() if (sc.get("reference") or {}).get("kind") != "grid"]


def _hole_errors(truth: dict, g) -> tuple[np.ndarray, float, float]:
    """Truth hole centres through the detected rectification: (residuals, rotation deg, scale)."""
    holes = np.array(truth["holes_in"], dtype=np.float64)
    ours = apply_h(g.h_px_to_plane, plane_to_image(truth, holes))
    tc, oc = holes - holes.mean(axis=0), ours - ours.mean(axis=0)
    u, s, vt = np.linalg.svd(oc.T @ tc)
    d = np.sign(np.linalg.det(u @ vt))
    rot = u @ np.diag([1, d]) @ vt
    scale = float((s * [1, d]).sum() / (tc**2).sum())
    return (
        np.linalg.norm(oc - scale * tc @ rot.T, axis=1),
        math.degrees(math.atan2(rot[1, 0], rot[0, 0])),
        scale,
    )


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("name", PLANAR_GRID_SCENARIOS)
def test_blue_grids_rectify_holes_within_tolerance(name: str, seed: int) -> None:
    img, truth = render_named(name, seed)
    g = find_grid(img)
    assert g is not None
    errors, angle, scale = _hole_errors(truth, g)
    assert np.percentile(errors, 95) <= 0.03
    assert g.rms_residual_in <= 0.02
    assert abs(angle) < 0.2 and abs(scale - 1) < 0.003
    assert reference_from_grid(g, "auto").warnings == ()
    assert not g.from_partial_lines


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("name", TRAIT_GRID_SCENARIOS)
def test_black_red_green_grids_with_real_photo_traits(name: str, seed: int) -> None:
    img, truth = render_named(name, seed)
    g = find_grid(img)
    assert g is not None, f"{name} seed {seed}: grid not found"
    errors, angle, scale = _hole_errors(truth, g)
    assert np.percentile(errors, 95) <= 0.03
    assert g.rms_residual_in <= 0.03
    assert abs(scale - 1) < 0.005
    if name == "green_grid_upside_down_staples":
        assert abs(abs(angle) - 180.0) < 0.5
    else:
        assert abs(angle) < 0.5
    assert reference_from_grid(g, "auto").warnings == ()


def test_line_colours_in_the_catalog_cover_black_red_and_green() -> None:
    colours = {render_named(n, 0)[1]["reference"]["line_color"] for n in _grid_scenarios()}
    assert {"black", "red", "green"} <= colours


@pytest.mark.parametrize("seed", SEEDS)
def test_curved_sheet_is_flagged_non_planar(seed: int) -> None:
    img, truth = render_named("curved_green_grid", seed)
    assert truth["non_planar"] is True
    g = find_grid(img)
    assert g is not None
    assert reference_from_grid(g, "auto").warnings == (NON_PLANAR_WARNING,)


def test_every_flat_grid_scenario_is_not_flagged_and_every_curved_one_is() -> None:
    for name in _grid_scenarios():
        img, truth = render_named(name, 0)
        g = find_grid(img)
        assert g is not None, name
        warned = bool(reference_from_grid(g, "auto").warnings)
        assert not g.from_partial_lines, name
        assert warned == bool(truth["non_planar"]), name


@pytest.mark.parametrize("name", _non_grid_scenarios())
def test_scenarios_without_a_grid_return_none_quickly(name: str) -> None:
    for seed in (0, 1):
        img, _ = render_named(name, seed)
        start = time.perf_counter()
        assert find_grid(img) is None, f"{name} seed {seed}"
        assert time.perf_counter() - start < 3.0, f"{name} seed {seed} took too long to reject"


@pytest.mark.parametrize("name", PLANAR_GRID_SCENARIOS + TRAIT_GRID_SCENARIOS)
def test_manual_corners_are_exact_on_synthetic_truth(name: str) -> None:
    img, truth = render_named(name, 0)
    corners_px = np.array(truth["reference"]["corners_px"], dtype=np.float64)
    order = _display_order(corners_px)
    g = grid_from_manual(
        corners_px[order], truth["reference"]["squares_across"], truth["reference"]["squares_down"]
    )
    errors, angle, scale = _hole_errors(truth, g)
    assert errors.max() < 1e-6
    assert abs(scale - 1) < 1e-6
    assert reference_from_grid(g, "manual").method == "manual"


def _display_order(corners_px: np.ndarray) -> list[int]:
    """Indices that reorder truth corners into TL, TR, BR, BL as they appear in the image."""
    by_y = np.argsort(corners_px[:, 1])
    top = sorted(by_y[:2], key=lambda i: corners_px[i, 0])
    bottom = sorted(by_y[2:], key=lambda i: -corners_px[i, 0])
    return [top[0], top[1], bottom[0], bottom[1]]


def test_automatic_and_manual_references_agree() -> None:
    img, truth = render_named("skew10_grid", 0)
    auto = find_grid(img)
    assert auto is not None
    corners_px = np.array(truth["reference"]["corners_px"], dtype=np.float64)
    manual = grid_from_manual(corners_px[_display_order(corners_px)], 8, 10)
    probe = plane_to_image(truth, np.array(truth["holes_in"]))
    a, m = apply_h(auto.h_px_to_plane, probe), apply_h(manual.h_px_to_plane, probe)
    assert np.abs((a - a[0]) - (m - m[0])).max() < 0.005


def test_working_image_from_a_detected_grid_has_one_inch_equal_to_ppi_pixels() -> None:
    img, truth = render_named("skew15_grid", 0)
    g = find_grid(img)
    assert g is not None
    working, frame, h_px_to_working = build_working(img, g.h_px_to_plane)
    holes_px = plane_to_image(truth, np.array(truth["holes_in"]))
    holes_w = apply_h(h_px_to_working, holes_px) / frame.ppi
    truth_in = np.array(truth["holes_in"])
    d = np.linalg.norm(
        (holes_w - holes_w.mean(axis=0)) - (truth_in - truth_in.mean(axis=0)), axis=1
    )
    assert working.shape[:2] == (frame.height_px, frame.width_px)
    assert d.max() < 0.03


def test_high_resolution_photo_goes_through_the_downscaled_path_without_losing_accuracy() -> None:
    img, truth = render_named("clean_grid", 0)
    big = cv2.resize(img, None, fx=2.2, fy=2.2, interpolation=cv2.INTER_CUBIC)
    assert max(big.shape[:2]) > 2000
    g = find_grid(big)
    assert g is not None
    holes = np.array(truth["holes_in"])
    ours = apply_h(g.h_px_to_plane, plane_to_image(truth, holes) * 2.2 + (2.2 - 1) / 2)
    d = (ours - ours.mean(axis=0)) - (holes - holes.mean(axis=0))
    assert np.abs(d).max() < 0.03


@pytest.mark.skipif(not REAL_PHOTOS.is_dir(), reason="real photos are not checked in")
@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
def test_real_photos_run_cleanly(n: int) -> None:
    """Qualitative smoke only (not an accuracy gate): no exception, and any lattice is finite and sane."""
    path = REAL_PHOTOS / f"real_0{n}.jpg"
    if not path.is_file():
        pytest.skip(f"{path.name} not present")
    img = cv2.imread(str(path))
    assert img is not None
    g = find_grid(img)
    if g is not None:
        assert len(g.intersections_px) >= 4
        assert np.isfinite(g.h_px_to_plane).all() and np.isfinite(g.rms_residual_in)
        assert g.from_partial_lines == (n == 2)
        assert (PARTIAL_GRID_WARNING in reference_from_grid(g, "auto").warnings) == (n == 2)


@pytest.mark.skipif(not REAL_PHOTOS.is_dir(), reason="real photos are not checked in")
def test_real_02_partial_grid_gives_the_one_inch_spacing() -> None:
    """real_02: a thick red band, an orange paster and cut-off lines leave 6 crossings; the thin
    line fallback must still give about 478 px per inch (hand measured from the line positions)."""
    path = REAL_PHOTOS / "real_02.jpg"
    if not path.is_file():
        pytest.skip(f"{path.name} not present")
    img = cv2.imread(str(path))
    assert img is not None
    g = find_grid(img)
    assert g is not None
    centre = apply_h(g.h_px_to_plane, np.array([[img.shape[1] / 2, img.shape[0] / 2]]))[0]
    back = apply_h(
        np.linalg.inv(g.h_px_to_plane), np.array([centre, centre + [1, 0], centre + [0, 1]])
    )
    for ppi in (np.linalg.norm(back[1] - back[0]), np.linalg.norm(back[2] - back[0])):
        assert ppi == pytest.approx(478, rel=0.03)
    assert reference_from_grid(g, "auto").warnings == (PARTIAL_GRID_WARNING,)
