"""T1 tests for the synthetic target generator (tests/synth/generate.py)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

from tests.synth.generate import load_catalog, plane_to_image, project, render, render_named

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog()
DESIGN_SCENARIOS = {
    "clean_circle1", "clean_circle3", "clean_grid", "noisy_lighting", "skew10_grid",
    "skew15_grid", "merged_pairs", "ragged_10x7", "black_bull_light_holes", "splatter",
    "circle_tilt_warn", "no_reference", "multi_dot_target", "high_right_group",
}
EXTRA_SCENARIOS = {"hires_wipe_decoys", "close_holes_tight_pair", "black_dot_mixed"}
REAL_PHOTO_SCENARIOS = {
    "black_grid_light_holes_qr", "red_grid_band_light_diamond", "green_grid_upside_down_staples",
    "curved_green_grid", "cardboard_sticker_ragged", "tape_cross_multi_aim",
}
EXPECTED_SCENARIOS = DESIGN_SCENARIOS | EXTRA_SCENARIOS | REAL_PHOTO_SCENARIOS
GRID_SCENARIOS = [
    "clean_grid", "skew10_grid", "skew15_grid", "black_grid_light_holes_qr",
    "red_grid_band_light_diamond", "green_grid_upside_down_staples", "tape_cross_multi_aim",
]
NEW_TRUTH_KEYS = (
    "hole_style", "background", "paper_bgr", "non_planar", "non_planar_warp", "print_decoys_in",
    "staples_in", "wrinkles_in", "creases_in", "corrugation_period_in", "aim_marks_in",
)
CLEAN_HOLE_SCENARIOS = [
    "clean_circle1", "clean_circle3", "clean_grid", "skew10_grid", "skew15_grid",
    "high_right_group", "no_reference", "close_holes_tight_pair", "black_dot_mixed",
    "black_bull_light_holes",
]
RAGGED_SEEDS = range(8)


def _gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _dark_labels(img: np.ndarray) -> np.ndarray:
    return cv2.connectedComponents((_gray(img) < 90).astype(np.uint8))[1]


def _red_mask(img: np.ndarray) -> np.ndarray:
    b, g, r = (img[:, :, i].astype(int) for i in range(3))
    return ((r - b > 100) & (g < 150)).astype(np.uint8)


def _line_mask(img: np.ndarray) -> np.ndarray:
    """Thin printed ink of any color: pixels that differ from the local (31 px) median."""
    background = cv2.medianBlur(img, 31)
    return np.abs(img.astype(np.int16) - background.astype(np.int16)).max(axis=2) > 45


def _classify_ink(bgr) -> str:
    b, g, r = (int(v) for v in bgr)
    if r > g + 50 and r > b + 50:
        return "red"
    if g > r + 40 and g > b + 40:
        return "green"
    if b > r + 40 and b > g + 20:
        return "blue"
    if max(b, g, r) - min(b, g, r) < 40 and (b + g + r) / 3 < 125:
        return "black"
    return "other"


def _h(truth: dict) -> np.ndarray:
    return np.array(truth["h_plane_to_image"])


def _label_at(labels: np.ndarray, xy) -> int:
    return int(labels[int(round(xy[1])), int(round(xy[0]))])


def _single_indices(truth: dict) -> list[int]:
    composite = {i for grp in truth["ragged_groups"] for i in grp}
    composite |= {i for pair in truth["merged_pairs"] for i in pair["indices"]}
    return [i for i in range(len(truth["holes_in"])) if i not in composite]


def _centroid_error_px(img: np.ndarray, truth: dict) -> list[float]:
    """Pixel centroid of each isolated hole's core versus its projected truth center."""
    gray = _gray(img)
    r_px = truth["bullet_diameter_in"] / 2 * truth["ppi"]
    centers = project(_h(truth), truth["holes_in"])
    errors = []
    for idx in _single_indices(truth):
        center = centers[idx]
        light = truth["holes_light_core"][idx]
        win = int(round(1.3 * r_px))
        cx, cy = int(round(center[0])), int(round(center[1]))
        patch = gray[cy - win : cy + win + 1, cx - win : cx + win + 1]
        yy, xx = np.mgrid[-win : win + 1, -win : win + 1]
        inside = (xx + cx - center[0]) ** 2 + (yy + cy - center[1]) ** 2 <= (1.3 * r_px) ** 2
        mask = ((patch > 150) if light else (patch < 90)) & inside
        assert mask.sum() > 0.5 * np.pi * r_px**2, "hole core not found in render"
        mx, my = (xx[mask] + cx).mean(), (yy[mask] + cy).mean()
        errors.append(float(np.hypot(mx - center[0], my - center[1])))
    return errors


def _fit_red_ellipse(img: np.ndarray):
    contours, _ = cv2.findContours(_red_mask(img), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    return cv2.fitEllipse(max(contours, key=cv2.contourArea))


def _outline_iou_and_component(img: np.ndarray, truth: dict, blob: int) -> tuple[float, int]:
    labels = _dark_labels(img)
    members = truth["ragged_groups"][blob]
    centers = project(_h(truth), [truth["holes_in"][i] for i in members])
    comp = _label_at(labels, centers[0])
    poly = np.round(project(_h(truth), truth["ragged_outlines_in"][blob]) * 16).astype(np.int32)
    outline = np.zeros(labels.shape, dtype=np.uint8)
    cv2.fillPoly(outline, [poly], 1, cv2.LINE_8, 4)
    component = labels == comp
    union = np.logical_or(outline > 0, component).sum()
    return float(np.logical_and(outline > 0, component).sum() / union), comp


def test_catalog_keeps_the_14_design_scenarios_and_adds_the_wider_and_real_photo_ones():
    assert set(CATALOG) >= DESIGN_SCENARIOS | EXTRA_SCENARIOS
    assert set(CATALOG) == EXPECTED_SCENARIOS
    assert len(CATALOG) == 23
    assert all(sc["name"] == name for name, sc in CATALOG.items())


@pytest.mark.parametrize("name", sorted(EXPECTED_SCENARIOS))
def test_every_scenario_renders_with_a_well_formed_truth(name):
    img, truth = render_named(name, 0)
    assert img.dtype == np.uint8 and img.ndim == 3 and img.shape[2] == 3
    assert truth["image_size"] == [img.shape[1], img.shape[0]]
    for key in ("holes_in", "ragged_groups", "reference", "h_plane_to_image", "ppi",
                "bullet_diameter_in", "decoys_in", "holes_light_core", "merged_pairs",
                *NEW_TRUTH_KEYS):
        assert key in truth
    assert np.array(truth["h_plane_to_image"]).shape == (3, 3)
    json.dumps(truth)
    holes = project(np.array(truth["h_plane_to_image"]), truth["holes_in"])
    assert (holes >= 0).all() and (holes[:, 0] < img.shape[1]).all()
    assert (holes[:, 1] < img.shape[0]).all()


def test_render_is_deterministic_per_seed_and_varies_across_seeds():
    img_a, truth_a = render(CATALOG["clean_circle1"], 7)
    img_b, truth_b = render(CATALOG["clean_circle1"], 7)
    img_c, truth_c = render(CATALOG["clean_circle1"], 8)
    assert img_a.tobytes() == img_b.tobytes()
    assert json.dumps(truth_a) == json.dumps(truth_b)
    assert img_a.tobytes() != img_c.tobytes()
    assert truth_a["holes_in"] != truth_c["holes_in"]


@pytest.mark.parametrize("name", sorted(EXPECTED_SCENARIOS))
def test_render_determinism_holds_for_every_catalog_scenario(name):
    """Covers the decoy, wipe, splatter, cluster and pair RNG paths: an unseeded draw fails."""
    first = render(CATALOG[name], 3)
    second = render(CATALOG[name], 3)
    assert first[0].tobytes() == second[0].tobytes()
    assert json.dumps(first[1]) == json.dumps(second[1])


@pytest.mark.parametrize("name", CLEAN_HOLE_SCENARIOS)
@pytest.mark.parametrize("seed", [0, 1])
def test_isolated_hole_centroids_are_within_half_a_pixel_of_truth(name, seed):
    img, truth = render(CATALOG[name], seed)
    errors = _centroid_error_px(img, truth)
    assert errors and max(errors) <= 0.5, f"{name}: worst centroid error {max(errors):.3f}px"


def test_ragged_10x7_has_seven_singles_and_one_ragged_blob_of_three():
    _, truth = render(CATALOG["ragged_10x7"], 0)
    assert len(truth["holes_in"]) == 10
    assert len(truth["ragged_groups"]) == 1
    members = truth["ragged_groups"][0]
    assert len(members) == 3
    assert len(set(range(10)) - set(members)) == 7


@pytest.mark.parametrize("seed", RAGGED_SEEDS)
def test_ragged_members_stay_below_the_split_floor_and_fill_the_area_band(seed):
    _, truth = render(CATALOG["ragged_10x7"], seed)
    d = truth["bullet_diameter_in"]
    members = truth["ragged_groups"][0]
    pts = [np.array(truth["holes_in"][i]) for i in members]
    seps = [np.hypot(*(a - b)) / d for i, a in enumerate(pts) for b in pts[i + 1 :]]
    assert max(seps) <= 0.5
    assert max(seps) >= 0.4
    outline = np.array(truth["ragged_outlines_in"][0], dtype=np.float32)
    ratio = cv2.contourArea(outline) / (np.pi * (d / 2) ** 2)
    assert 1.5 <= ratio <= 3.0, f"outline area is {ratio:.2f}x one hole"
    for x, y in pts:
        assert cv2.pointPolygonTest(outline, (float(x), float(y)), False) > 0
    for idx in set(range(len(truth["holes_in"]))) - set(members):
        x, y = truth["holes_in"][idx]
        assert cv2.pointPolygonTest(outline, (x, y), False) < 0


@pytest.mark.parametrize("seed", RAGGED_SEEDS)
def test_ragged_cluster_renders_as_one_dark_component_with_no_single_attached(seed):
    img, truth = render(CATALOG["ragged_10x7"], seed)
    labels = _dark_labels(img)
    members = truth["ragged_groups"][0]
    centers = project(_h(truth), truth["holes_in"])
    comps = {_label_at(labels, centers[i]) for i in members}
    assert len(comps) == 1 and 0 not in comps
    comp = comps.pop()
    others = [i for i in range(len(centers)) if i not in members]
    assert all(_label_at(labels, centers[i]) != comp for i in others)
    assert len({_label_at(labels, centers[i]) for i in others}) == len(others)


@pytest.mark.parametrize("seed", RAGGED_SEEDS)
def test_ragged_outline_matches_the_rendered_dark_component(seed):
    img, truth = render(CATALOG["ragged_10x7"], seed)
    iou, _ = _outline_iou_and_component(img, truth, 0)
    assert iou >= 0.9, f"outline/render IoU {iou:.3f}"


def test_distinct_holes_are_separated_in_the_clean_scenarios():
    _, truth = render(CATALOG["clean_circle3"], 0)
    pts = np.array(truth["holes_in"])
    d = truth["bullet_diameter_in"]
    dist = np.hypot(*(pts[:, None, :] - pts[None, :, :]).transpose(2, 0, 1))
    np.fill_diagonal(dist, np.inf)
    assert dist.min() >= 2.0 * d - 1e-9


def test_merged_pairs_have_the_requested_separations():
    _, truth = render(CATALOG["merged_pairs"], 0)
    d = truth["bullet_diameter_in"]
    seps = sorted(
        np.hypot(*(np.array(truth["holes_in"][i]) - np.array(truth["holes_in"][j]))) / d
        for i, j in (p["indices"] for p in truth["merged_pairs"])
    )
    assert seps == pytest.approx([0.5, 0.65, 0.8, 1.0], abs=1e-6)
    assert len(truth["holes_in"]) == 12


@pytest.mark.parametrize("name", ["merged_pairs", "close_holes_tight_pair"])
@pytest.mark.parametrize("seed", range(6))
def test_merged_pairs_render_connected_exactly_as_truth_core_gap_predicts(name, seed):
    """Overlapping or near-touching (<= 1.5 px gap, bridged by blur) cores are one dark component;
    a gap of 3 px or more is two."""
    img, truth = render(CATALOG[name], seed)
    labels = _dark_labels(img)
    centers = project(_h(truth), truth["holes_in"])
    for pair in truth["merged_pairs"]:
        i, j = pair["indices"]
        connected = _label_at(labels, centers[i]) == _label_at(labels, centers[j]) != 0
        gap_px = pair["core_gap_in"] * truth["ppi"]
        assert pair["cores_overlap"] == (pair["core_gap_in"] <= 0)
        if pair["separation_d"] <= 0.8:
            assert pair["cores_overlap"] and connected
        if gap_px <= 1.5:
            assert connected
        elif gap_px >= 3.0:
            assert not connected


def test_the_1_0_d_pair_is_marginal_and_truth_reports_its_core_gap():
    """At 1.0 d the cores nearly touch; core_gap_in tells a consumer which way a seed fell."""
    gaps = []
    for seed in range(12):
        _, truth = render(CATALOG["merged_pairs"], seed)
        gaps += [p["core_gap_in"] for p in truth["merged_pairs"] if p["separation_d"] == 1.0]
    d = 0.224
    assert len(gaps) == 12
    assert all(-0.15 * d < g < 0.3 * d for g in gaps)
    assert min(gaps) < 0.05 * d and max(gaps) > 0.1 * d


def test_circle_tilt_warn_axis_ratio_is_about_0_95_in_truth_and_in_the_image():
    img, truth = render(CATALOG["circle_tilt_warn"], 0)
    ratio = truth["reference"]["axis_ratio"]
    assert 0.94 <= ratio <= 0.96
    (_, (w, h), _) = _fit_red_ellipse(img)
    assert min(w, h) / max(w, h) == pytest.approx(ratio, abs=0.01)


def test_untilted_circle_scenarios_have_unit_axis_ratio_and_exact_scale():
    for name in ("clean_circle1", "clean_circle3"):
        _, truth = render(CATALOG[name], 0)
        ref = truth["reference"]
        assert ref["axis_ratio"] == pytest.approx(1.0, abs=1e-6)
        assert ref["px_per_in_at_ref"] == pytest.approx(truth["ppi"], rel=1e-4)


def test_high_right_group_is_above_and_right_of_the_aim_point():
    img, truth = render(CATALOG["high_right_group"], 0)
    holes = np.array(truth["holes_in"])
    aim = np.array(truth["aim_in"])
    mean = holes.mean(axis=0)
    assert mean[0] - aim[0] > 0.5
    assert aim[1] - mean[1] > 0.4  # plane y is down, so "above" is a smaller y

    b, g, r = (img[:, :, i].astype(int) for i in range(3))
    dot = np.argwhere((g - r > 60) & (g - b > 60))
    dot_y, dot_x = dot.mean(axis=0)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    ys, xs = np.nonzero(gray < 90)
    assert xs.mean() > dot_x and ys.mean() < dot_y
    assert (dot_x, dot_y) == pytest.approx(truth["aim_px"], abs=2.0)


def test_no_reference_scenario_has_no_reference_and_no_reference_ink():
    img, truth = render(CATALOG["no_reference"], 0)
    assert truth["reference"] is None
    assert "reference" not in CATALOG["no_reference"] or CATALOG["no_reference"]["reference"] is None
    b, g, r = (img[:, :, i].astype(int) for i in range(3))
    assert not ((r - b > 150) & (g < 120)).any()
    assert len(truth["holes_in"]) == 5


def _angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


@pytest.mark.parametrize("name", GRID_SCENARIOS + ["curved_green_grid"])
def test_grid_truth_corners_are_ordered_in_plane_coordinates(name):
    """TL, TR, BR, BL follow the sheet, not the image: the TL->TR direction in the image is the
    plane +x direction, i.e. the scenario rotation, so a 180 degree sheet puts TL bottom-right."""
    _, truth = render(CATALOG[name], 0)
    ref = truth["reference"]
    assert ref["kind"] == "grid" and ref["squares_across"] == 8 and ref["squares_down"] == 10
    tl, tr, br, bl = (np.array(c) for c in ref["corners_px"])
    for a, b, c in ((tl, tr, br), (tr, br, bl), (br, bl, tl), (bl, tl, tr)):
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        assert cross > 0
    rotation = CATALOG[name]["rotation_deg"]
    to_right = np.degrees(np.arctan2((tr - tl)[1], (tr - tl)[0]))
    to_down = np.degrees(np.arctan2((bl - tl)[1], (bl - tl)[0]))
    assert _angle_diff(to_right, rotation) < 20
    assert _angle_diff(to_down, rotation + 90) < 20


def test_a_180_degree_sheet_has_its_tl_corner_bottom_right_and_upside_down_text():
    sc = CATALOG["green_grid_upside_down_staples"]
    assert sc["rotation_deg"] == 180
    _, truth = render(sc, 0)
    tl, br = (np.array(truth["reference"]["corners_px"][i]) for i in (0, 2))
    assert tl[0] > br[0] and tl[1] > br[1]
    label = next(d for d in truth["print_decoys_in"] if d["text"] == "1L")
    x0, y0, x1, y1 = label["rect_in"]
    cx, cy = plane_to_image(truth, [[(x0 + x1) / 2, (y0 + y1) / 2]])[0]
    assert cy < truth["image_size"][1] / 4, "label printed at the plane bottom is at the image top"


def _near_line(line: np.ndarray, xy, radius: int = 2) -> bool:
    x, y = int(round(xy[0])), int(round(xy[1]))
    if not (radius <= x < line.shape[1] - radius and radius <= y < line.shape[0] - radius):
        return False
    return bool(line[y - radius : y + radius + 1, x - radius : x + radius + 1].any())


def _seg_dist(p, a, b) -> float:
    a, b, p = (np.asarray(v, dtype=float) for v in (a, b, p))
    t = np.clip((p - a) @ (b - a) / ((b - a) @ (b - a)), 0, 1)
    return float(np.hypot(*(p - (a + t * (b - a)))))


def _near_printed(sc: dict, truth: dict, p, margin: float = 0.2) -> bool:
    """True when plane point p is within `margin` inches of a hole or a non-grid printed item."""
    p = np.asarray(p, dtype=float)
    r_in = truth["bullet_diameter_in"] / 2
    if any(np.hypot(*(p - h)) < 3 * r_in for h in truth["holes_in"]):
        return True
    for dot in sc.get("aim_dots", []):
        if np.hypot(*(p - dot["center_in"])) < dot["diameter_in"] / 2 + margin:
            return True
    for mark in sc.get("overlay_marks", []):
        kind, c = mark["kind"], mark.get("center_in")
        if kind == "diamond" and np.hypot(*(p - c)) < mark["diameter_in"] / 2 + margin:
            return True
        if kind == "ring" and abs(np.hypot(*(p - c)) - mark["diameter_in"] / 2) < margin:
            return True
        if kind == "line" and _seg_dist(p, mark["p0_in"], mark["p1_in"]) < margin:
            return True
        if kind == "tape_cross":
            dx, dy = np.abs(p - c)
            if (dx < mark["arm_in"] + margin and dy < mark["width_in"] / 2 + margin) or (
                dy < mark["arm_in"] + margin and dx < mark["width_in"] / 2 + margin
            ):
                return True
    for st in sc.get("staples", []):
        if np.hypot(*(p - st["center_in"])) < st["length_in"] / 2 + 0.25:
            return True
    for item in truth["print_decoys_in"]:
        x0, y0, x1, y1 = item["rect_in"]
        if x0 - margin < p[0] < x1 + margin and y0 - margin < p[1] < y1 + margin:
            return True
    return False


def _in_band(truth: dict, p, slack: float = 0.05) -> bool:
    band = truth["reference"].get("band")
    return bool(band) and abs(p[0] - band["center_in"]) <= band["width_in"] / 2 + slack


def _lattice_samples(truth: dict):
    ref = truth["reference"]
    across, down = ref["squares_across"], ref["squares_down"]
    for i in range(across + 1):
        for j in range(down + 1):
            for si, sj in ((0, 0), (0.5, 0), (0, 0.5), (0.25, 0), (0, 0.25)):
                if (si and i == across) or (sj and j == down):
                    continue
                yield i, j, si, sj


def _lattice_check(img, truth, sc, from_corners: bool = True):
    """Lattice rebuilt from `corners_px` alone; returns (samples checked, samples off a line)."""
    ref = truth["reference"]
    across, down = ref["squares_across"], ref["squares_down"]
    line = _line_mask(img)
    unit = np.array([[0, 0], [across, 0], [across, down], [0, down]], dtype=np.float32)
    h_lat = cv2.getPerspectiveTransform(unit, np.array(ref["corners_px"], dtype=np.float32))
    ox, oy = ref["corners_in"][0]
    checked = misses = 0
    for i, j, si, sj in _lattice_samples(truth):
        plane = (ox + i + si, oy + j + sj)
        if _near_printed(sc, truth, plane) or _in_band(truth, plane):
            continue
        px = project(h_lat, [[i + si, j + sj]])[0] if from_corners else plane_to_image(truth, [plane])[0]
        checked += 1
        misses += not _near_line(line, px)
    return checked, misses


@pytest.mark.parametrize("name", GRID_SCENARIOS)
def test_grid_truth_corners_define_a_lattice_that_lies_on_every_rendered_line(name):
    """Every visible lattice point and every point along each lattice edge, boundary included,
    must sit on a drawn line (any ink color); each corner needs an L-junction."""
    sc = CATALOG[name]
    img, truth = render(sc, 0)
    ref = truth["reference"]
    across, down = ref["squares_across"], ref["squares_down"]
    checked, misses = _lattice_check(img, truth, sc)
    assert checked > 0.7 * (across + 1) * (down + 1) * 3
    assert misses == 0, f"{misses} of {checked} lattice samples are not on a rendered line"

    line = _line_mask(img)
    unit = np.array([[0, 0], [across, 0], [across, down], [0, down]], dtype=np.float32)
    h_lat = cv2.getPerspectiveTransform(unit, np.array(ref["corners_px"], dtype=np.float32))
    ox, oy = ref["corners_in"][0]

    def to_px(i: float, j: float) -> np.ndarray:
        return project(h_lat, [[i, j]])[0]

    errors = []
    for i in range(1, across):
        for j in range(1, down):
            plane = (ox + i, oy + j)
            if _near_printed(sc, truth, plane) or _in_band(truth, plane, slack=0.4):
                continue
            x, y = to_px(i, j)
            xi, yi = int(round(x)), int(round(y))
            yy, xx = np.nonzero(line[yi - 8 : yi + 9, xi - 8 : xi + 9])
            errors.append(np.hypot(xx.mean() + xi - 8 - x, yy.mean() + yi - 8 - y))
    assert len(errors) >= 15
    assert np.median(errors) < 0.7
    assert np.mean(np.array(errors) <= 1.2) >= 0.9

    for i, j in ((0.5, 0.5), (across - 0.5, 0.5), (0.5, down - 0.5), (across - 0.5, down - 0.5),
                 (across / 2 + 0.5, down / 2 + 0.5)):
        plane = (ox + i, oy + j)
        if _near_printed(sc, truth, plane) or _in_band(truth, plane, slack=0.4):
            continue
        assert not _near_line(line, to_px(i, j), radius=1), "square interior shows a line"

    corner_steps = {
        (0, 0): ((1, 0), (0, 1)),
        (across, 0): ((-1, 0), (0, 1)),
        (across, down): ((-1, 0), (0, -1)),
        (0, down): ((1, 0), (0, -1)),
    }
    for (ci, cj), ((ax, ay), (bx, by)) in corner_steps.items():
        assert _near_line(line, to_px(ci + ax * 0.5, cj + ay * 0.5))
        assert _near_line(line, to_px(ci + bx * 0.5, cj + by * 0.5))
        assert not _near_line(line, to_px(ci - ax * 0.5, cj - ay * 0.5), radius=1)
        assert not _near_line(line, to_px(ci - bx * 0.5, cj - by * 0.5), radius=1)


def test_multi_dot_target_has_three_disjoint_groups_with_holes_inside_their_roi():
    _, truth = render(CATALOG["multi_dot_target"], 0)
    groups = truth["groups"]
    assert len(groups) == 3
    assert sorted(i for g in groups for i in g["hole_indices"]) == list(range(15))
    h = np.array(truth["h_plane_to_image"])
    for g in groups:
        x0, y0, x1, y1 = g["roi_px"]
        px = project(h, [truth["holes_in"][i] for i in g["hole_indices"]])
        assert ((px[:, 0] >= x0) & (px[:, 0] <= x1) & (px[:, 1] >= y0) & (px[:, 1] <= y1)).all()
    rois = [g["roi_px"] for g in groups]
    for a in range(3):
        for b in range(a + 1, 3):
            disjoint = rois[a][2] <= rois[b][0] or rois[b][2] <= rois[a][0] or (
                rois[a][3] <= rois[b][1] or rois[b][3] <= rois[a][1]
            )
            assert disjoint


@pytest.mark.parametrize(
    "name", ["clean_circle1", "clean_circle3", "circle_tilt_warn", "merged_pairs", "splatter"]
)
def test_reference_circle_truth_matches_an_ellipse_fit_of_the_render(name):
    img, truth = render(CATALOG[name], 0)
    ref = truth["reference"]
    (cx, cy), (w, h), angle = _fit_red_ellipse(img)
    assert (cx, cy) == pytest.approx(ref["ellipse_center_px"], abs=0.5)
    assert (cx, cy) == pytest.approx(ref["center_px"], abs=1.0)
    assert (w + h) / 2 == pytest.approx(sum(ref["axes_px"]) / 2, rel=0.005)
    assert min(w, h) / max(w, h) == pytest.approx(ref["axis_ratio"], abs=0.01)
    assert (w + h) / 2 / ref["diameter_in"] == pytest.approx(ref["px_per_in_at_ref"], rel=0.005)
    if ref["axis_ratio"] < 0.98:
        diff = abs(angle - ref["angle_deg"]) % 180
        assert min(diff, 180 - diff) < 5


def test_no_reference_aim_mark_is_a_diamond_not_a_circle():
    sc = CATALOG["no_reference"]
    assert all(dot.get("shape") == "diamond" for dot in sc["aim_dots"])
    img, truth = render(sc, 0)
    b, g, r = (img[:, :, i].astype(int) for i in range(3))
    mask = ((g - r > 60) & (g - b > 60)).astype(np.uint8)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    hull = cv2.convexHull(max(contours, key=cv2.contourArea))
    diag_px = sc["aim_dots"][0]["diameter_in"] * truth["ppi"]
    ratio = cv2.contourArea(hull) / (diag_px**2 / 2)
    assert 0.9 <= ratio <= 1.1, f"hull area is {ratio:.2f}x a diamond (a circle would be 1.57)"


def test_hires_scenario_exercises_the_working_image_downscale():
    sc = CATALOG["hires_wipe_decoys"]
    assert sc["ppi"] >= 350
    img, truth = render(sc, 0)
    assert img.shape[0] * img.shape[1] > 12_000_000
    assert max(img.shape[:2]) > 4000 and truth["ppi"] > 200


def test_bullet_wipe_rings_are_drawn_and_enlarge_the_dark_area():
    img, truth = render(CATALOG["hires_wipe_decoys"], 0)
    assert any(truth["holes_wipe"]) and not all(truth["holes_wipe"])
    gray = _gray(img)
    centers = project(_h(truth), truth["holes_in"])
    r_px = truth["bullet_diameter_in"] / 2 * truth["ppi"]
    areas = {True: [], False: []}
    for idx, (x, y) in enumerate(centers):
        win = int(round(1.3 * r_px))
        patch = gray[int(y) - win : int(y) + win + 1, int(x) - win : int(x) + win + 1]
        areas[truth["holes_wipe"][idx]].append(float((patch < 90).sum()) / (np.pi * r_px**2))
    assert np.mean(areas[True]) > np.mean(areas[False]) + 0.1


def test_decoy_marks_render_above_the_area_floor_and_match_their_truth_area():
    img, truth = render(CATALOG["hires_wipe_decoys"], 0)
    assert len(truth["decoys_in"]) == 4
    labels = _dark_labels(img)
    r_in = truth["bullet_diameter_in"] / 2
    a1_px = np.pi * (r_in * truth["ppi"]) ** 2
    holes = np.array(truth["holes_in"])
    for decoy in truth["decoys_in"]:
        assert np.min(np.hypot(*(holes - decoy["center_in"]).T)) > 5 * r_in
        x, y = project(_h(truth), [decoy["center_in"]])[0]
        comp = _label_at(labels, (x, y))
        assert comp != 0, "decoy is not dark in the render"
        rendered = float((labels == comp).sum())
        expected = np.pi * (decoy["radius_in"] * truth["ppi"]) ** 2
        assert rendered / a1_px >= 0.35, f"rendered decoy is {rendered / a1_px:.2f} A1"
        assert rendered == pytest.approx(expected, rel=0.1)


def _hole_patch(gray: np.ndarray, truth: dict, idx: int, radius_factor: float):
    r_px = truth["bullet_diameter_in"] / 2 * truth["ppi"]
    x, y = project(_h(truth), [truth["holes_in"][idx]])[0]
    win = int(round(radius_factor * r_px))
    cx, cy = int(round(x)), int(round(y))
    patch = gray[cy - win : cy + win + 1, cx - win : cx + win + 1]
    yy, xx = np.mgrid[-win : win + 1, -win : win + 1]
    inside = (xx + cx - x) ** 2 + (yy + cy - y) ** 2 <= (radius_factor * r_px) ** 2
    return patch, inside, r_px


@pytest.mark.parametrize(
    "name", ["black_bull_light_holes", "black_dot_mixed", "clean_circle1", "multi_dot_target"]
)
def test_holes_light_core_flags_match_each_hole_core_in_the_render(name):
    img, truth = render(CATALOG[name], 0)
    gray = _gray(img)
    for idx, flag in enumerate(truth["holes_light_core"]):
        patch, inside, _ = _hole_patch(gray, truth, idx, 0.4)
        assert (float(patch[inside].mean()) > 150) == flag, f"hole {idx}"


@pytest.mark.parametrize("seed", [0, 1])
def test_holes_wipe_flags_match_each_hole_in_the_render(seed):
    """A wipe ring is a plateau of mid-dark gray (46-72) outside the near-black core; unwiped
    holes only show the thin blur transition in that gray band."""
    img, truth = render(CATALOG["hires_wipe_decoys"], seed)
    gray = _gray(img)
    assert any(truth["holes_wipe"]) and not all(truth["holes_wipe"])
    for idx, flag in enumerate(truth["holes_wipe"]):
        patch, inside, r_px = _hole_patch(gray, truth, idx, 1.3)
        band = float(((patch >= 46) & (patch <= 72) & inside).sum()) / (2 * np.pi * r_px)
        assert (band > 1.5) == flag, f"hole {idx}: band width {band:.2f}px"


def test_close_holes_scenario_has_singles_closer_than_composite_spacing_and_a_0_4_d_pair():
    _, truth = render(CATALOG["close_holes_tight_pair"], 0)
    d = truth["bullet_diameter_in"]
    singles = np.array([truth["holes_in"][i] for i in _single_indices(truth)])
    dist = np.hypot(*(singles[:, None, :] - singles[None, :, :]).transpose(2, 0, 1))
    np.fill_diagonal(dist, np.inf)
    assert 1.3 * d - 1e-9 <= dist.min() < 2.0 * d
    assert [p["separation_d"] for p in truth["merged_pairs"]] == [0.4]


def test_black_dot_scenario_mixes_light_and_dark_holes_and_none_straddles_the_edge():
    sc = CATALOG["black_dot_mixed"]
    for seed in range(4):
        _, truth = render(sc, seed)
        dot = sc["aim_dots"][0]
        radius, center = dot["diameter_in"] / 2, np.array(dot["center_in"])
        extent = truth["bullet_diameter_in"] / 2
        for (x, y), light in zip(truth["holes_in"], truth["holes_light_core"], strict=True):
            dist = np.hypot(*(np.array([x, y]) - center))
            assert abs(radius - dist) >= extent
            assert light == (dist < radius)
    flags = {f for seed in range(4) for f in render(sc, seed)[1]["holes_light_core"]}
    assert flags == {True, False}


# ------------------------------------------------------------------ real-photo traits (T2.18)


def _px(truth: dict, pts_in) -> np.ndarray:
    return plane_to_image(truth, pts_in)


def _clean_centroid_errors(img: np.ndarray, truth: dict) -> list[float]:
    """Centroid error of isolated holes whose dark core is not touched by any other dark ink:
    the dark pixels inside 1.3 r must be the hole alone (no dark pixel in the 1.35-1.55 r shell)."""
    gray = _gray(img)
    r_px = truth["bullet_diameter_in"] / 2 * truth["ppi"]
    centers = _px(truth, truth["holes_in"])
    errors = []
    for idx in _single_indices(truth):
        x, y = centers[idx]
        win = int(round(1.6 * r_px))
        cx, cy = int(round(x)), int(round(y))
        patch = gray[cy - win : cy + win + 1, cx - win : cx + win + 1]
        yy, xx = np.mgrid[-win : win + 1, -win : win + 1]
        dist = np.hypot(xx + cx - x, yy + cy - y)
        dark = patch < 90
        if (dark & (dist > 1.35 * r_px) & (dist <= 1.55 * r_px)).any():
            continue
        mask = dark & (dist <= 1.3 * r_px)
        if mask.sum() < 0.3 * np.pi * r_px**2:
            continue
        mx, my = (xx[mask] + cx).mean(), (yy[mask] + cy).mean()
        errors.append(float(np.hypot(mx - x, my - y)))
    return errors


@pytest.mark.parametrize("name", sorted(REAL_PHOTO_SCENARIOS))
@pytest.mark.parametrize("seed", [0, 1])
def test_real_photo_scenarios_hole_centroids_match_truth_through_the_warp(name, seed):
    img, truth = render(CATALOG[name], seed)
    errors = _clean_centroid_errors(img, truth)
    assert len(errors) >= 3, "too few isolated holes to verify"
    assert max(errors) <= 0.6, f"{name}: worst centroid error {max(errors):.3f}px"


@pytest.mark.parametrize(
    "name", sorted(REAL_PHOTO_SCENARIOS - {"cardboard_sticker_ragged", "curved_green_grid"})
)
def test_light_core_holes_have_a_dark_rim_and_a_light_core_in_the_render(name):
    img, truth = render(CATALOG[name], 0)
    gray = _gray(img)
    assert truth["hole_style"] == "light_core"
    r_px = truth["bullet_diameter_in"] / 2 * truth["ppi"]
    centers = _px(truth, truth["holes_in"])
    singles = _single_indices(truth)
    assert singles and all(truth["holes_light_core"][i] for i in singles)
    for idx in singles:
        x, y = centers[idx]
        win = int(round(r_px)) + 2
        cx, cy = int(round(x)), int(round(y))
        patch = gray[cy - win : cy + win + 1, cx - win : cx + win + 1].astype(float)
        gy, gx = np.mgrid[-win : win + 1, -win : win + 1]
        dist = np.hypot(gx + cx - x, gy + cy - y)
        assert patch[dist <= 0.4 * r_px].mean() > 150, f"hole {idx}: core is not light"
        rim = patch[(dist >= 0.72 * r_px) & (dist <= 0.84 * r_px)].mean()
        assert rim < 100, f"hole {idx}: rim mean gray {rim:.0f}"


def test_light_core_holes_appear_on_white_and_on_grey_paper():
    greys = {}
    for name in ("black_grid_light_holes_qr", "red_grid_band_light_diamond"):
        img, truth = render(CATALOG[name], 0)
        sc = CATALOG[name]
        gray_in = 0.114 * truth["paper_bgr"][0] + 0.587 * truth["paper_bgr"][1] + 0.299 * truth["paper_bgr"][2]
        ink = _line_mask(img)
        samples = []
        ox, oy = truth["reference"]["corners_in"][0]
        for i in range(8):
            for j in range(10):
                p = (ox + i + 0.5, oy + j + 0.5)
                if _near_printed(sc, truth, p) or _in_band(truth, p, slack=0.4):
                    continue
                x, y = _px(truth, [p])[0]
                if not ink[int(y) - 6 : int(y) + 7, int(x) - 6 : int(x) + 7].any():
                    samples.append(_gray(img)[int(y), int(x)])
        greys[name] = (gray_in, float(np.median(samples)))
        assert greys[name][1] == pytest.approx(gray_in, rel=0.2)
    assert greys["black_grid_light_holes_qr"][0] < 190 < greys["red_grid_band_light_diamond"][0]


@pytest.mark.parametrize("name", ["black_grid_light_holes_qr", "red_grid_band_light_diamond",
                                  "green_grid_upside_down_staples", "curved_green_grid",
                                  "tape_cross_multi_aim"])
def test_grid_line_color_in_truth_matches_the_rendered_lines(name):
    sc = CATALOG[name]
    img, truth = render(sc, 0)
    ref = truth["reference"]
    assert ref["line_color"] == sc["reference"]["line_color"]
    line = _line_mask(img)
    bg = cv2.medianBlur(img, 31).astype(np.int16)
    ox, oy = ref["corners_in"][0]
    seen = []
    for i in range(ref["squares_across"] + 1):
        for j in range(ref["squares_down"]):
            plane = (ox + i, oy + j + 0.5)
            if _near_printed(sc, truth, plane) or _in_band(truth, plane, slack=0.3):
                continue
            x, y = (int(round(v)) for v in _px(truth, [plane])[0])
            win = line[y - 3 : y + 4, x - 3 : x + 4]
            if not win.any():
                continue
            diff = np.abs(img[y - 3 : y + 4, x - 3 : x + 4].astype(np.int16) - bg[y - 3 : y + 4, x - 3 : x + 4]).max(axis=2)
            yy, xx = np.unravel_index(int(diff.argmax()), diff.shape)
            seen.append(_classify_ink(img[y - 3 + yy, x - 3 + xx]))
    assert len(seen) >= 8
    assert np.mean([c == ref["line_color"] for c in seen]) >= 0.9, seen


@pytest.mark.parametrize("name", ["red_grid_band_light_diamond", "green_grid_upside_down_staples"])
def test_thick_colored_band_is_rendered_with_the_truth_width_and_line_color(name):
    sc = CATALOG[name]
    img, truth = render(sc, 0)
    ref = truth["reference"]
    band = ref["band"]
    assert band["width_in"] >= 0.4
    half = band["width_in"] / 2
    oy = ref["corners_in"][0][1]
    fractions = []
    for y in np.arange(oy + 0.7, oy + 9.5, 0.55):
        if _near_printed(sc, truth, (band["center_in"], y), margin=0.5):
            continue
        left, right = (band["center_in"] - half - 0.3, y), (band["center_in"] + half + 0.3, y)
        ts = np.linspace(0, 1, 41)
        pts = np.array(left) * (1 - ts[:, None]) + np.array(right) * ts[:, None]
        px = _px(truth, pts).round().astype(int)
        inside = np.abs(pts[:, 0] - band["center_in"]) <= half - 0.03
        outside = np.abs(pts[:, 0] - band["center_in"]) >= half + 0.06
        classes = np.array([_classify_ink(img[yy, xx]) for xx, yy in px])
        fractions.append((np.mean(classes[inside] == ref["line_color"]),
                          np.mean(classes[outside] == ref["line_color"])))
    assert len(fractions) >= 8
    assert np.median([f[0] for f in fractions]) >= 0.95
    assert np.median([f[1] for f in fractions]) <= 0.1


@pytest.mark.parametrize("name", ["black_grid_light_holes_qr", "red_grid_band_light_diamond",
                                  "green_grid_upside_down_staples"])
def test_print_decoys_are_inked_in_the_render_and_clear_of_the_holes(name):
    img, truth = render(CATALOG[name], 0)
    line = _line_mask(img)
    gray = _gray(img)
    r_in = truth["bullet_diameter_in"] / 2
    assert truth["print_decoys_in"]
    for item in truth["print_decoys_in"]:
        x0, y0, x1, y1 = item["rect_in"]
        quad = _px(truth, [[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
        mask = np.zeros(gray.shape, dtype=np.uint8)
        cv2.fillPoly(mask, [quad.round().astype(np.int32)], 1)
        inked = float(line[mask > 0].mean())
        if item["kind"] == "qr":
            dark = float((gray[mask > 0] < 90).mean())
            assert dark > 0.3, f"QR is only {dark:.2f} dark"
        else:
            assert inked > 0.08, f"text '{item['text']}' ink fraction {inked:.3f}"
        holes = np.array(truth["holes_in"])
        near = np.hypot(np.clip(holes[:, 0], x0, x1) - holes[:, 0], np.clip(holes[:, 1], y0, y1) - holes[:, 1])
        assert near.min() > 3 * r_in


def test_qr_rect_is_tight_around_the_rendered_code():
    """Finder patterns sit in three corners of the truth rect: dark just inside each corner, white
    quiet zone just outside it. A displaced or resized rect fails."""
    img, truth = render(CATALOG["black_grid_light_holes_qr"], 0)
    gray = _gray(img)
    qr = next(d for d in truth["print_decoys_in"] if d["kind"] == "qr")
    x0, y0, x1, y1 = qr["rect_in"]
    module = (x1 - x0) / 21

    def gray_at(x, y):
        px, py = _px(truth, [[x, y]])[0].round().astype(int)
        return float(gray[py - 1 : py + 2, px - 1 : px + 2].mean())

    for cx, cy, sx, sy in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1)):
        assert gray_at(cx + sx * 0.5 * module, cy + sy * 0.5 * module) < 90, "finder ring missing"
        assert gray_at(cx - sx * 0.5 * module, cy - sy * 0.5 * module) > 190, "quiet zone is inked"
        assert gray_at(cx + sx * 3.5 * module, cy + sy * 3.5 * module) < 90, "finder core missing"


@pytest.mark.parametrize(
    "name", ["green_grid_upside_down_staples", "red_grid_band_light_diamond"]
)
def test_text_rects_are_tight_around_the_rendered_ink(name):
    sc = CATALOG[name]
    img, truth = render(sc, 0)
    line = _line_mask(img)
    inverse = np.linalg.inv(_h(truth))
    texts = [d for d in truth["print_decoys_in"] if d["kind"] == "text"]
    assert texts
    for item in texts:
        x0, y0, x1, y1 = item["rect_in"]
        # the bottom edge is probed only 0.02 in out: texts sit near the paper edge, whose
        # paper-to-table step also reads as ink
        window = [[x0 - 0.06, y0 - 0.06], [x1 + 0.06, y0 - 0.06], [x1 + 0.06, y1 + 0.02], [x0 - 0.06, y1 + 0.02]]
        quad = _px(truth, window)
        mask = np.zeros(line.shape, dtype=np.uint8)
        cv2.fillPoly(mask, [quad.round().astype(np.int32)], 1)
        ys, xs = np.nonzero((mask > 0) & line)
        plane = project(inverse, np.column_stack([xs, ys]))
        lo, hi = plane.min(axis=0), plane.max(axis=0)
        assert lo == pytest.approx([x0, y0], abs=0.04), f"{item['text']}: ink starts at {lo}"
        assert hi == pytest.approx([x1, y1], abs=0.04), f"{item['text']}: ink ends at {hi}"


def test_qr_and_text_are_the_only_ink_in_the_decoy_rect_of_a_blank_paper_corner():
    """Verify-the-verifier: a same-size rect on blank paper is not 'inked'."""
    img, truth = render(CATALOG["black_grid_light_holes_qr"], 0)
    line = _line_mask(img)
    qr = next(d for d in truth["print_decoys_in"] if d["kind"] == "qr")
    x0, y0, x1, y1 = qr["rect_in"]
    shifted = [[x0 - 5.3, y0 - 0.6], [x1 - 5.3, y0 - 0.6], [x1 - 5.3, y1 - 0.6], [x0 - 5.3, y1 - 0.6]]
    mask = np.zeros(line.shape, dtype=np.uint8)
    cv2.fillPoly(mask, [_px(truth, shifted).round().astype(np.int32)], 1)
    assert float((_gray(img)[mask > 0] < 90).mean()) < 0.1


@pytest.mark.parametrize("name", ["green_grid_upside_down_staples"])
def test_staples_are_drawn_where_and_how_truth_says(name):
    img, truth = render(CATALOG[name], 0)
    gray = _gray(img)
    assert len(truth["staples_in"]) == 3
    for st in truth["staples_in"]:
        cx, cy = st["center_in"]
        ang = np.radians(st["angle_deg"])
        half = st["length_in"] / 2
        ends = _px(truth, [[cx - half * np.cos(ang), cy - half * np.sin(ang)],
                           [cx + half * np.cos(ang), cy + half * np.sin(ang)]])
        centre = _px(truth, [[cx, cy]])[0]
        win = int(1.2 * truth["ppi"] * st["length_in"] / 2)
        xi, yi = int(round(centre[0])), int(round(centre[1]))
        patch = gray[yi - win : yi + win + 1, xi - win : xi + win + 1]
        ys, xs = np.nonzero(patch < 100)
        assert len(xs) >= 30, "staple outline not found"
        pts = np.column_stack([xs, ys]).astype(float)
        centroid = pts.mean(axis=0) + [xi - win, yi - win]
        pts -= pts.mean(axis=0)
        evals, evecs = np.linalg.eigh(np.cov(pts.T))
        axis = evecs[:, int(evals.argmax())]
        expected = (ends[1] - ends[0]) / np.hypot(*(ends[1] - ends[0]))
        assert abs(float(axis @ expected)) > 0.97, "staple axis does not match the truth angle"
        assert np.hypot(*(centroid - centre)) < 0.15 * truth["ppi"], "staple is off its truth center"


@pytest.mark.parametrize("name", ["black_grid_light_holes_qr", "cardboard_sticker_ragged"])
def test_wrinkles_darken_the_render_along_their_ridge(name):
    img, truth = render(CATALOG[name], 0)
    gray = _gray(img).astype(float)
    assert truth["wrinkles_in"]
    for wr in truth["wrinkles_in"]:
        p0, p1 = np.array(wr["p0_in"]), np.array(wr["p1_in"])
        u = (p1 - p0) / np.hypot(*(p1 - p0))
        n = np.array([-u[1], u[0]])
        ts = np.linspace(0.25, 0.75, 7)[:, None]
        ridge = p0 * (1 - ts) + p1 * ts

        def median_gray(points):
            vals = []
            for x, y in _px(truth, points).round().astype(int):
                vals.append(np.median(gray[y - 5 : y + 6, x - 5 : x + 6]))
            return float(np.median(vals))

        ref = max(median_gray(ridge + 2.8 * wr["width_in"] * n), median_gray(ridge - 2.8 * wr["width_in"] * n))
        assert median_gray(ridge) < ref * (1 - 0.5 * wr["strength"]), f"ridge not dark: {wr}"


def test_cardboard_background_is_corrugated_creased_and_brown():
    sc = CATALOG["cardboard_sticker_ragged"]
    img, truth = render(sc, 0)
    gray = _gray(img).astype(float)
    assert truth["background"] == "cardboard"
    period = truth["corrugation_period_in"]
    ts = np.arange(0.5, 8.0, 0.02)
    pts = np.column_stack([ts, np.full_like(ts, 2.8)])
    px = _px(truth, pts).round().astype(int)
    profile = np.array([gray[y - 2 : y + 3, x - 1 : x + 2].mean() for x, y in px])
    profile -= np.polyval(np.polyfit(ts, profile, 2), ts)
    spectrum = np.abs(np.fft.rfft(profile * np.hanning(len(profile))))
    freqs = np.fft.rfftfreq(len(profile), d=0.02)
    peak = freqs[1 + int(spectrum[1:].argmax())]
    assert 1 / peak == pytest.approx(period, rel=0.06), f"stripe period {1 / peak:.3f} in"

    b, g, r = (float(np.median(img[:, :, c])) for c in range(3))
    assert r > b + 40 and g > b
    background = float(np.median(gray))
    assert truth["creases_in"]
    dark = []
    for crease in truth["creases_in"]:
        for x, y in _px(truth, crease).round().astype(int):
            dark.append(gray[y - 2 : y + 3, x - 2 : x + 3].min() < background - 35)
    assert np.mean(dark) >= 0.7


def test_sticker_disc_is_the_circle_reference_and_holes_on_it_have_light_cores():
    sc = CATALOG["cardboard_sticker_ragged"]
    img, truth = render(sc, 0)
    ref = truth["reference"]
    contours, _ = cv2.findContours(_red_mask(img), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    (cx, cy), (w, h), _ = cv2.fitEllipse(max(contours, key=cv2.contourArea))
    assert (cx, cy) == pytest.approx(ref["ellipse_center_px"], abs=0.7)
    assert (w + h) / 2 == pytest.approx(sum(ref["axes_px"]) / 2, rel=0.01)
    inside = [np.hypot(*(np.array(c) - ref["center_in"])) < ref["diameter_in"] / 2
              for c in truth["holes_in"]]
    members = {i for g in truth["ragged_groups"] for i in g}
    for idx, on_disc in enumerate(inside):
        assert truth["holes_light_core"][idx] == (on_disc and idx not in members)
    assert any(inside) and not all(inside)
    assert len(truth["ragged_groups"]) == 1 and len(truth["ragged_groups"][0]) == 4


def test_curved_sheet_flag_warp_and_render_agree():
    sc = CATALOG["curved_green_grid"]
    img, truth = render(sc, 0)
    assert truth["non_planar"] is True
    assert truth["non_planar_warp"] == {"amplitude_in": 0.3}
    assert all(render(CATALOG[n], 0)[1]["non_planar"] is False for n in ("clean_grid", "no_reference"))
    line = _line_mask(img)
    ref = truth["reference"]
    ox, oy = ref["corners_in"][0]
    h_only = lambda plane: project(_h(truth), [plane])[0]  # noqa: E731
    sagged = 0
    used = 0
    for i in range(1, 8):
        plane = (ox + i + 0.5, oy + 5)
        if _near_printed(sc, truth, plane):
            continue
        used += 1
        assert _near_line(line, _px(truth, [plane])[0]), "bowed line is not where plane_to_image says"
        sagged += not _near_line(line, h_only(plane), radius=3)
    assert used >= 4
    assert sagged >= 3, "flat homography still lands on the line: the sheet is not bowed in the render"
    checked, misses = _lattice_check(img, truth, sc)
    assert misses > 0.2 * checked, "four corners should NOT explain a curved sheet"
    checked, misses = _lattice_check(img, truth, sc, from_corners=False)
    assert misses == 0


def test_tape_cross_aim_mark_is_rendered_where_truth_places_it():
    sc = CATALOG["tape_cross_multi_aim"]
    img, truth = render(sc, 0)
    mark = next(m for m in sc["overlay_marks"] if m["kind"] == "tape_cross")
    assert {"kind": "tape_cross", "center_in": mark["center_in"]} in truth["aim_marks_in"]
    cx, cy = mark["center_in"]
    arm = mark["arm_in"]

    def klass(plane):
        x, y = _px(truth, [plane])[0].round().astype(int)
        return _classify_ink(img[y, x]), _gray(img)[y, x]

    for dx, dy in ((0.5, 0), (-0.5, 0), (0, 0.5), (0, -0.5)):
        assert klass((cx + dx * arm, cy + dy * arm))[0] == "blue"
    assert klass((cx + 0.5 * arm, cy + 0.3))[0] != "blue"
    assert klass((cx + 0.3, cy + 0.5 * arm))[0] != "blue"
    assert klass((cx + 1.15 * arm, cy))[0] != "blue"
    assert klass((cx, cy))[1] < 80, "center dot"
    group = truth["groups"][2]
    assert group["aim_in"] == mark["center_in"]


@pytest.mark.parametrize("name", sorted(EXPECTED_SCENARIOS))
def test_group_roi_contains_every_hole_of_its_group(name):
    _, truth = render(CATALOG[name], 0)
    r_px = truth["bullet_diameter_in"] / 2 * truth["ppi"]
    width, height = truth["image_size"]
    centers = _px(truth, truth["holes_in"])
    covered = set()
    for grp in truth["groups"]:
        x0, y0, x1, y1 = grp["roi_px"]
        assert 0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height
        for idx in grp["hole_indices"]:
            x, y = centers[idx]
            assert x - r_px >= x0 and x + r_px <= x1 and y - r_px >= y0 and y + r_px <= y1, (
                f"hole {idx} of {name} is outside its group roi"
            )
            covered.add(idx)
    assert covered == set(range(len(truth["holes_in"])))


def test_aim_marks_in_truth_include_every_group_aim_in_the_new_scenarios():
    for name in REAL_PHOTO_SCENARIOS - {"cardboard_sticker_ragged"}:
        _, truth = render(CATALOG[name], 0)
        centers = [np.array(m["center_in"]) for m in truth["aim_marks_in"]]
        for grp in truth["groups"]:
            assert any(np.hypot(*(np.array(grp["aim_in"]) - c)) < 0.01 for c in centers), name


def test_old_scenarios_still_render_without_the_real_photo_traits():
    _, truth = render(CATALOG["clean_circle1"], 0)
    assert truth["hole_style"] == "dark" and truth["background"] == "paper"
    assert truth["print_decoys_in"] == [] and truth["staples_in"] == [] and truth["wrinkles_in"] == []
    assert truth["creases_in"] == [] and truth["corrugation_period_in"] is None


def test_ppi_override_scales_the_image_for_large_renders():
    img, truth = render({**CATALOG["clean_circle1"], "ppi": 350}, 0)
    assert img.shape[0] * img.shape[1] > 11_000_000
    assert truth["ppi"] == 350


@pytest.mark.parametrize(
    "scenario, seed",
    [
        ({"unknown_key": 1}, 0),
        ({"reference": {"kind": "triangle"}}, 0),
        ({"groups": [{"n_single": 3}]}, 0),
        ({"ppi": 1000}, 0),
        ({}, -1),
    ],
)
def test_invalid_input_is_rejected(scenario, seed):
    with pytest.raises(ValueError):
        render(scenario, seed)


def test_unplaceable_group_fails_loudly_instead_of_overlapping():
    sc = {"groups": [{"aim_in": [4.25, 5.5], "spread_in": 0.2, "n_single": 20}]}
    with pytest.raises(RuntimeError, match="could not place"):
        render(sc, 0)


def test_unknown_scenario_name_lists_valid_names():
    with pytest.raises(KeyError, match="clean_circle1"):
        render_named("nope", 0)


def test_cli_writes_png_and_truth_json_matching_render(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "tests.synth.generate", "--scenario", "no_reference",
         "--seed", "5", "--out", str(tmp_path)],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    png, meta = tmp_path / "no_reference.png", tmp_path / "no_reference.truth.json"
    assert png.exists() and meta.exists()
    img, truth = render_named("no_reference", 5)
    assert np.array_equal(cv2.imread(str(png)), img)
    assert json.loads(meta.read_text()) == json.loads(json.dumps(truth))


def test_cli_unknown_scenario_exits_nonzero(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "tests.synth.generate", "--scenario", "nope", "--out", str(tmp_path)],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False, timeout=60,
    )
    assert proc.returncode == 2
    assert "unknown scenario" in proc.stderr
