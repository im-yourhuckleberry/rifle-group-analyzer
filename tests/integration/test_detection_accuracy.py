"""Detection accuracy against the P-11 synthetic thresholds, plus checks on the harness itself.

The catalog gate and the 12 MP runtime check carry the `accuracy` marker (`make accuracy`); the
detector contract and scorer-verification tests are fast and always run.
"""

import itertools
import math
import re
import time

import cv2
import numpy as np
import pytest

from rga.domain.types import Blob, BlobKind, WorkingFrame
from rga.vision import detect as detect_module
from rga.vision.detect import DetectParams, detect_holes
from rga.vision.transform import apply_h, build_working
from tests.synth.generate import load_catalog, render
from tools import eval_detection
from tools.eval_detection import (
    DESIGN_POINTS,
    P11,
    RenderScore,
    aggregate,
    build_report,
    evaluate_catalog,
    hungarian,
    is_noisy,
    main,
    parse_seeds,
    score_blobs,
    score_render,
    truth_holes_working_px,
    verdict,
    working_from_truth,
)

GATE_SEEDS = (0, 1, 2, 3, 4)
MIN_MEGAPIXELS = 12


# ---------------------------------------------------------------- the accuracy gate


@pytest.fixture(scope="module")
def catalog_results():
    return evaluate_catalog(load_catalog(), GATE_SEEDS)


@pytest.mark.accuracy
def test_gate_covers_every_catalog_scenario(catalog_results):
    assert {r.name for r in catalog_results} == set(load_catalog())
    assert len(catalog_results) >= 17
    assert all(r.seeds == len(GATE_SEEDS) for r in catalog_results)


@pytest.mark.accuracy
def test_every_scenario_meets_p11_thresholds(catalog_results):
    failing = {r.name: r.failures for r in catalog_results if not r.passed}
    assert failing == {}


@pytest.mark.accuracy
def test_gate_actually_exercised_holes_pairs_and_ragged_clusters(catalog_results):
    assert sum(r.n_isolated for r in catalog_results) > 400
    assert sum(r.pairs_total for r in catalog_results) >= 10
    assert sum(r.ragged_total for r in catalog_results) >= 10
    assert sum(r.subfloor_total for r in catalog_results) >= 10


@pytest.mark.accuracy
def test_12mp_render_detects_within_three_seconds():
    scenario = load_catalog()["hires_wipe_decoys"]
    img, truth = render(scenario, 0)
    assert img.shape[0] * img.shape[1] >= MIN_MEGAPIXELS * 1_000_000
    start = time.perf_counter()
    working, frame, _ = build_working(img, np.linalg.inv(np.array(truth["h_plane_to_image"])))
    blobs = detect_holes(working, frame, truth["bullet_diameter_in"])
    elapsed = time.perf_counter() - start
    assert elapsed <= P11.runtime_s, f"{elapsed:.2f} s for {img.shape[1]}x{img.shape[0]}"
    assert len(blobs) >= 8


@pytest.mark.accuracy
def test_ragged_cluster_reads_as_one_ragged_blob_over_thirty_more_seeds():
    results = evaluate_catalog(load_catalog(), range(10, 40), only=["ragged_10x7"])
    assert results[0].ragged_ok == results[0].ragged_total == 30


# ---------------------------------------------------------------- detector contract


@pytest.fixture(scope="module")
def clean_target():
    img, truth = render(load_catalog()["clean_circle1"], 0)
    working, frame, h_work = working_from_truth(img, truth)
    return working, frame, h_work, truth


def test_blob_structure_invariants(clean_target):
    working, frame, h_work, truth = clean_target
    blobs = detect_holes(working, frame, truth["bullet_diameter_in"])
    assert len(blobs) == len(truth["holes_in"]) == 5
    height, width = working.shape[:2]
    for blob in blobs:
        assert blob.kind == BlobKind.SINGLE
        assert len(blob.centers_px) == 1
        assert 0.0 <= blob.confidence <= 1.0
        assert blob.area_px > 0
        assert len(blob.contour_px) >= 8
        assert all(isinstance(v, int) for pt in blob.contour_px for v in pt)
        cx, cy = blob.centers_px[0]
        assert 0 <= cx < width and 0 <= cy < height
    order = [(b.centers_px[0][1], b.centers_px[0][0]) for b in blobs]
    assert order == sorted(order)


def test_centers_match_truth_to_a_fraction_of_a_pixel(clean_target):
    working, frame, h_work, truth = clean_target
    blobs = detect_holes(working, frame, truth["bullet_diameter_in"])
    holes = truth_holes_working_px(truth, h_work)
    found = np.array([b.centers_px[0] for b in blobs])
    for hole in holes:
        assert np.hypot(*(found - hole).T).min() < 1.5


def test_detection_is_deterministic(clean_target):
    working, frame, _, truth = clean_target
    first = detect_holes(working, frame, truth["bullet_diameter_in"])
    second = detect_holes(working, frame, truth["bullet_diameter_in"])
    assert first == second


def test_roi_limits_blobs_to_those_centered_inside(clean_target):
    working, frame, h_work, truth = clean_target
    full = detect_holes(working, frame, truth["bullet_diameter_in"])
    target = full[0].centers_px[0]
    half = int(0.6 * frame.ppi)
    roi = (
        int(target[0]) - half,
        int(target[1]) - half,
        int(target[0]) + half,
        int(target[1]) + half,
    )
    inside = detect_holes(working, frame, truth["bullet_diameter_in"], roi)
    expected = [
        b
        for b in full
        if roi[0] <= b.centers_px[0][0] < roi[2] and roi[1] <= b.centers_px[0][1] < roi[3]
    ]
    assert len(inside) == len(expected) >= 1
    for got, want in zip(inside, expected, strict=True):
        assert np.allclose(got.centers_px[0], want.centers_px[0], atol=1.0)


@pytest.mark.parametrize(
    "roi", [(0, 0, 0, 0), (50, 50, 50, 90), (60, 60, 20, 20), (-500, -500, -10, -10)]
)
def test_empty_or_outside_roi_returns_nothing(clean_target, roi):
    working, frame, _, truth = clean_target
    assert detect_holes(working, frame, truth["bullet_diameter_in"], roi) == []


def test_roi_beyond_the_image_is_clamped(clean_target):
    working, frame, _, truth = clean_target
    huge = (-10_000, -10_000, 10_000, 10_000)
    assert len(detect_holes(working, frame, truth["bullet_diameter_in"], huge)) == 5


def test_grayscale_input_is_accepted(clean_target):
    import cv2

    working, frame, _, truth = clean_target
    gray = cv2.cvtColor(working, cv2.COLOR_BGR2GRAY)
    assert len(detect_holes(gray, frame, truth["bullet_diameter_in"])) == 5


def test_blank_page_has_no_holes():
    page = np.full((900, 700, 3), 235, dtype=np.uint8)
    assert detect_holes(page, WorkingFrame(200.0, 700, 900), 0.224) == []


@pytest.mark.parametrize(
    ("working", "diameter", "ppi"),
    [
        (np.zeros((0, 0, 3), np.uint8), 0.224, 200.0),
        (np.zeros((10,), np.uint8), 0.224, 200.0),
        (np.zeros((10, 10, 3), np.uint8), 0.0, 200.0),
        (np.zeros((10, 10, 3), np.uint8), -0.2, 200.0),
        (np.zeros((10, 10, 3), np.uint8), 0.224, 0.0),
    ],
)
def test_invalid_inputs_raise_value_error(working, diameter, ppi):
    with pytest.raises(ValueError):
        detect_holes(working, WorkingFrame(ppi, 10, 10), diameter)


@pytest.mark.parametrize(
    "bad",
    [
        {"dark_rel": 0},
        {"dark_rel": 0.7},
        {"gray_dark_rel": 1.0},
        {"min_area_a1": 9.0},
        {"light_min_a1": 4.0},
        {"halo_outer_r": 0.1},
        {"min_solidity": 1.2},
        {"elongation_split": 0.9},
        {"open_d": -1.0},
    ],
)
def test_invalid_params_are_rejected(bad):
    with pytest.raises(ValueError):
        DetectParams(**bad)


def test_default_params_are_valid_and_frozen():
    params = DetectParams()
    with pytest.raises(AttributeError):
        params.dark_rel = 0.1  # type: ignore[misc]


def test_params_change_behaviour():
    img, truth = render(load_catalog()["clean_circle1"], 0)
    working, frame, _ = working_from_truth(img, truth)
    strict = DetectParams(dark_rel=0.05, gray_dark_rel=0.05)
    assert detect_holes(working, frame, truth["bullet_diameter_in"], None, strict) == []


# ---------------------------------------------------------------- acceptance rules


def _candidate(**over):
    base = {
        "contour": np.zeros((4, 1, 2), np.int32),
        "mask_patch": np.zeros((4, 4), np.uint8),
        "origin": (0, 0),
        "area": 1.0,
        "cue": "dark",
        "halo": 0.0,
        "contrast": 0.8,
        "core_fraction": 0.0,
        "roundness": 0.85,
        "crowding": 0.0,
    }
    base.update(over)
    return detect_module._Candidate(**base)


ACCEPT_CASES = [
    ("round hole with a halo", {"halo": 0.1}, 0.9, 1.05, True),
    ("round solid hole without a halo above the small-area gate", {}, 0.9, 1.05, True),
    ("small solid mark without a halo is dirt", {}, 0.5, 1.05, False),
    ("small mark with a halo is a hole", {"halo": 0.05}, 0.5, 1.05, True),
    ("small ring hole with an enclosed light core", {"core_fraction": 0.3}, 0.62, 1.05, True),
    (
        "small mark with a barely visible core is not a ring hole",
        {"core_fraction": 0.1},
        0.62,
        1.05,
        False,
    ),
    ("irregular blob such as a letter", {"roundness": 0.45, "core_fraction": 0.3}, 0.9, 1.1, False),
    (
        "elongated merged pair keeps its low roundness",
        {"roundness": 0.5, "halo": 0.1},
        1.6,
        1.8,
        True,
    ),
    ("blob inside print texture", {"halo": 0.2, "crowding": 0.5}, 0.9, 1.05, False),
    ("large dark mark without a halo is printed", {}, 3.0, 1.05, False),
    ("large cluster with a halo is a ragged hole", {"halo": 0.1}, 3.0, 1.05, True),
    ("light core blob skips the halo rules", {"cue": "light"}, 0.9, 1.05, True),
    (
        "irregular light blob is still rejected",
        {"cue": "light", "roundness": 0.4},
        0.9,
        1.05,
        False,
    ),
]


@pytest.mark.parametrize(("label", "over", "ratio", "axis_ratio", "expected"), ACCEPT_CASES)
def test_acceptance_rules(label, over, ratio, axis_ratio, expected):
    assert (
        detect_module._accepted(_candidate(**over), ratio, axis_ratio, DetectParams()) is expected
    ), label


def test_bright_blob_inside_a_ragged_dark_region_is_rejected():
    params = DetectParams()
    a1 = math.pi * 22.4**2
    compact = (
        np.array([[[0, 0]], [[400, 0]], [[400, 400]], [[0, 400]]], np.int32),
        400 * 400.0,
        0.95,
    )
    ragged = (compact[0], 400 * 400.0, 0.5)
    hole_sized = (np.array([[[0, 0]], [[40, 0]], [[40, 40]], [[0, 40]]], np.int32), 40 * 40.0, 0.95)
    centre = (200.0, 200.0)
    assert detect_module._in_acceptable_container(centre, [compact], a1, params)
    assert not detect_module._in_acceptable_container(centre, [ragged], a1, params)
    assert not detect_module._in_acceptable_container((20.0, 20.0), [hole_sized], a1, params)
    assert detect_module._in_acceptable_container((900.0, 900.0), [ragged], a1, params)


# ---------------------------------------------------------------- real-photo cues on drawn scenes

PAPER_BGR = (225, 230, 235)
BULLET_IN = 0.224
FRAME_PPI = 200.0


def _page(color=PAPER_BGR, height=500, width=700):
    return np.full((height, width, 3), color, np.uint8)


def _draw_hole(img, center, core=21, halo=30, gray=30):
    cv2.circle(img, center, halo, (165, 165, 165), -1, cv2.LINE_AA)
    cv2.circle(img, center, core, (gray, gray, gray), -1, cv2.LINE_AA)


def _detect(img, roi=None):
    frame = WorkingFrame(FRAME_PPI, img.shape[1], img.shape[0])
    return detect_holes(cv2.GaussianBlur(img, (0, 0), 0.8), frame, BULLET_IN, roi)


def _centers(blobs):
    return [tuple(round(v) for v in c) for b in blobs for c in b.centers_px]


def test_drawn_hole_is_found_at_its_centre():
    img = _page()
    _draw_hole(img, (300, 250))
    (blob,) = _detect(img)
    assert blob.kind == BlobKind.SINGLE
    assert np.allclose(blob.centers_px[0], (300, 250), atol=1.0)


def test_saturated_print_is_never_dark():
    img = _page()
    cv2.rectangle(img, (200, 0), (330, 500), (40, 40, 200), -1)
    cv2.circle(img, (550, 150), 80, (70, 170, 60), -1)
    cv2.circle(img, (550, 380), 60, (30, 140, 250), -1)
    cv2.circle(img, (450, 60), 22, (40, 40, 200), -1)
    cv2.circle(img, (450, 440), 22, (60, 170, 40), -1)
    assert _detect(img) == []


@pytest.mark.parametrize(
    ("background", "center"),
    [
        ((40, 40, 200), (265, 250)),
        ((70, 170, 60), (330, 260)),
    ],
    ids=["red_band", "green_dot"],
)
def test_hole_on_coloured_print_is_still_found(background, center):
    img = _page()
    cv2.rectangle(img, (200, 0), (330, 500), background, -1)
    cv2.circle(img, (300, 250), 120, background, -1)
    _draw_hole(img, center)
    assert _centers(_detect(img)) == [center]


def test_tan_core_without_a_dark_rim_is_a_hole():
    img = _page()
    cv2.circle(img, (300, 250), 13, (135, 175, 200), -1, cv2.LINE_AA)
    assert _centers(_detect(img)) == [(300, 250)]


def test_tan_blob_on_cardboard_background_is_not_a_hole():
    img = _page(color=(115, 155, 190))
    cv2.circle(img, (300, 250), 13, (135, 175, 200), -1, cv2.LINE_AA)
    assert _detect(img) == []


def test_small_neutral_disc_without_halo_is_dirt():
    img = _page()
    cv2.circle(img, (300, 250), 13, (120, 120, 120), -1, cv2.LINE_AA)
    assert _detect(img) == []


def test_chain_of_touching_holes_is_split_into_one_centre_each():
    img = _page()
    centers = [(250, 250), (292, 250), (334, 250)]
    for c in centers:
        cv2.circle(img, c, 31, (165, 165, 165), -1, cv2.LINE_AA)
    for c in centers:
        cv2.circle(img, c, 21, (30, 30, 30), -1, cv2.LINE_AA)
    (blob,) = _detect(img)
    assert blob.kind == BlobKind.SPLIT
    assert len(blob.centers_px) == 3
    for got, want in zip(sorted(blob.centers_px), centers, strict=True):
        assert np.allclose(got, want, atol=1.5)


def test_hole_with_a_printed_line_stub_stays_a_single_hole():
    img = _page()
    _draw_hole(img, (300, 250))
    cv2.line(img, (322, 250), (420, 250), (30, 30, 30), 9, cv2.LINE_AA)
    (blob,) = _detect(img)
    assert blob.kind == BlobKind.SINGLE
    assert np.allclose(blob.centers_px[0], (300, 250), atol=1.5)


def test_hole_touching_a_large_black_diamond_separates_from_it():
    img = _page()
    diamond = np.array([[300, 100], [420, 220], [300, 340], [180, 220]], np.int32)
    cv2.fillPoly(img, [diamond], (10, 10, 10), cv2.LINE_AA)
    assert _detect(img) == []
    _draw_hole(img, (300, 92))
    assert _centers(_detect(img)) == [(300, 92)]


def test_pair_path_needs_two_distance_peaks():
    img = _page()
    _draw_hole(img, (300, 250))
    cv2.line(img, (322, 250), (350, 250), (30, 30, 30), 18, cv2.LINE_AA)
    blobs = _detect(img)
    assert all(b.kind != BlobKind.SPLIT for b in blobs)


def _mark(img, center, core_radius, halo_gray=(165, 165, 165), core_gray=(30, 30, 30)):
    cv2.circle(img, center, core_radius + 8, halo_gray, -1, cv2.LINE_AA)
    cv2.circle(img, center, core_radius, core_gray, -1, cv2.LINE_AA)


def _a1_radius(ratio):
    return round(math.sqrt(ratio) * 0.5 * BULLET_IN * FRAME_PPI)


def test_hole_core_at_half_its_disc_brightness_is_found():
    """dark_rel 0.5: on a coloured disc a hole core is only 0.4-0.5 of the disc brightness."""
    img = _page()
    cv2.circle(img, (300, 250), 150, (70, 170, 60), -1)
    _mark(img, (310, 260), 21, halo_gray=(150, 130, 130), core_gray=(80, 45, 45))
    assert _centers(_detect(img)) == [(310, 260)]


@pytest.mark.parametrize(("ratio", "expected"), [(0.45, 0), (0.75, 1)])
def test_round_marks_below_the_area_floor_are_never_holes(ratio, expected):
    """min_area_a1 0.55: text and QR fragments are 0.35-0.5 of a hole area, holes at least 0.65."""
    img = _page()
    _mark(img, (300, 250), _a1_radius(ratio))
    assert len(_detect(img)) == expected


@pytest.mark.parametrize(("ratio", "expected"), [(0.9, 1), (4.0, 0)])
def test_tan_blob_larger_than_a_hole_core_is_background(ratio, expected):
    """tan_max_a1: a tan region several hole areas wide is cardboard or a hanger hole, not a core."""
    img = _page()
    cv2.circle(img, (300, 250), _a1_radius(ratio), (135, 175, 200), -1, cv2.LINE_AA)
    assert len(_detect(img)) == expected


def test_dark_brown_dirt_mark_is_not_a_tan_core():
    """tan_min_value: a hole's backing is lit paper or cardboard; dark pixels carry a tan hue only by noise."""
    img = _page()
    cv2.circle(img, (300, 250), 13, (25, 42, 58), -1, cv2.LINE_AA)
    assert _detect(img) == []


def test_grey_rimmed_hole_just_above_the_core_threshold_is_found():
    """gray_dark_rel: an unsaturated core at about 0.52 of the paper is dark by the grey route only."""
    img = _page()
    _mark(img, (300, 250), 21, core_gray=(118, 112, 106))
    assert _centers(_detect(img)) == [(300, 250)]


def test_coloured_core_at_the_same_brightness_is_not_dark():
    """max_saturation: the grey route must not admit coloured print at 0.5-0.55 of the paper."""
    img = _page()
    _mark(img, (300, 250), 21, core_gray=(118, 60, 60))
    assert _detect(img) == []


def test_streak_shaped_tan_blob_is_not_a_hole_core():
    img = _page()
    cv2.rectangle(img, (280, 246), (322, 254), (135, 175, 200), -1)
    assert _detect(img) == []


def test_clump_centres_follow_each_hole_when_sizes_differ():
    img = _page()
    discs = [((250, 250), 24), ((292, 262), 18), ((330, 250), 22)]
    for c, r in discs:
        cv2.circle(img, c, r + 9, (165, 165, 165), -1, cv2.LINE_AA)
    for c, r in discs:
        cv2.circle(img, c, r, (30, 30, 30), -1, cv2.LINE_AA)
    blobs = _detect(img)
    found = sorted(c for b in blobs for c in b.centers_px)
    assert len(found) == 3
    for got, (want, _) in zip(found, discs, strict=True):
        assert np.allclose(got, want, atol=1.5)


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_print_heavy_group_yields_one_centre_per_hole(seed):
    """black_grid_light_holes_qr: a hole touching a printed arc must not read as a pair."""
    img, truth = render(load_catalog()["black_grid_light_holes_qr"], seed)
    working, frame, h_work = working_from_truth(img, truth)
    (group,) = truth["groups"]
    x0, y0, x1, y1 = group["roi_px"]
    corners = apply_h(h_work, np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], float))
    roi = (
        int(corners[:, 0].min()),
        int(corners[:, 1].min()),
        int(np.ceil(corners[:, 0].max())),
        int(np.ceil(corners[:, 1].max())),
    )
    blobs = detect_holes(working, frame, truth["bullet_diameter_in"], roi)
    assert sum(len(b.centers_px) for b in blobs) == len(group["hole_indices"])


def test_new_params_are_validated():
    for bad in (
        {"max_cluster_area_a1": 4.0},
        {"cluster_min_radius_r": 1.5, "cluster_max_radius_r": 1.2},
    ):
        with pytest.raises(ValueError):
            DetectParams(**bad)


# ---------------------------------------------------------------- verifying the verifier


@pytest.fixture(scope="module")
def pair_target():
    scenario = load_catalog()["merged_pairs"]
    img, truth = render(scenario, 0)
    working, frame, h_work = working_from_truth(img, truth)
    return scenario, truth, frame, h_work


def _blob(kind, centers, area=1000.0):
    ring = tuple(
        (int(cx) + dx, int(cy) + dy)
        for cx, cy in centers[:1]
        for dx, dy in ((0, 0), (5, 0), (5, 5))
    )
    return Blob(kind, ring, area, tuple((float(x), float(y)) for x, y in centers), 0.9)


def oracle_blobs(truth, frame, h_work, *, shift_px=0.0, drop=None, extra=(), pairs_as_one=False):
    """Blobs a perfect detector would return, optionally corrupted in one specific way."""
    holes = truth_holes_working_px(truth, h_work)
    in_pair = {i: m for m in truth["merged_pairs"] for i in m["indices"]}
    in_ragged = {i for g in truth["ragged_groups"] for i in g}
    blobs, done = [], set()
    for i, pt in enumerate(holes):
        if i == drop or i in done or i in in_ragged:
            continue
        pt = pt + np.array([shift_px, 0.0])
        if i in in_pair:
            members = in_pair[i]["indices"]
            done.update(members)
            pts = holes[members]
            if pairs_as_one or in_pair[i]["separation_d"] < 0.6:
                blobs.append(_blob(BlobKind.SINGLE, [tuple(pts.mean(axis=0))], area=1500.0))
            else:
                blobs.append(_blob(BlobKind.SPLIT, [tuple(p) for p in pts], area=1800.0))
        else:
            blobs.append(_blob(BlobKind.SINGLE, [tuple(pt)]))
    for group in truth["ragged_groups"]:
        blobs.append(_blob(BlobKind.RAGGED, [tuple(holes[group].mean(axis=0))], area=2000.0))
    blobs.extend(_blob(BlobKind.SINGLE, [pt]) for pt in extra)
    return blobs


def _score(scenario, truth, frame, h_work, blobs):
    one = score_blobs(scenario["name"], 0, blobs, truth, frame, h_work)
    return aggregate(scenario["name"], is_noisy(scenario), [one])


def test_scorer_passes_a_perfect_oracle(pair_target):
    scenario, truth, frame, h_work = pair_target
    result = _score(scenario, truth, frame, h_work, oracle_blobs(truth, frame, h_work))
    assert result.passed, result.failures
    assert result.recall == 1.0 and result.fp_max == 0
    assert result.pairs_total >= 1 and result.pairs_resolved == result.pairs_total
    assert result.subfloor_total >= 1 and result.subfloor_covered == result.subfloor_total


def test_scorer_fails_when_centers_are_off_by_more_than_the_limit(pair_target):
    scenario, truth, frame, h_work = pair_target
    off = 0.06 * frame.ppi
    blobs = oracle_blobs(truth, frame, h_work, shift_px=off)
    result = _score(scenario, truth, frame, h_work, blobs)
    assert not result.passed
    assert any("center" in f for f in result.failures)


def test_scorer_fails_when_a_hole_is_missed(pair_target):
    scenario, truth, frame, h_work = pair_target
    isolated = next(
        i
        for i in range(len(truth["holes_in"]))
        if all(i not in m["indices"] for m in truth["merged_pairs"])
    )
    blobs = oracle_blobs(truth, frame, h_work, drop=isolated)
    result = _score(scenario, truth, frame, h_work, blobs)
    assert result.recall < 1.0
    assert any("recall" in f for f in result.failures)


def test_scorer_counts_unmatched_detections_as_false_positives(pair_target):
    scenario, truth, frame, h_work = pair_target
    far = [(50.0, 50.0), (60.0, 400.0)]
    result = _score(scenario, truth, frame, h_work, oracle_blobs(truth, frame, h_work, extra=far))
    assert result.fp_max == 2
    assert any("false positives" in f for f in result.failures)


def test_scorer_fails_when_separable_pairs_are_not_resolved(pair_target):
    scenario, truth, frame, h_work = pair_target
    blobs = oracle_blobs(truth, frame, h_work, pairs_as_one=True)
    result = _score(scenario, truth, frame, h_work, blobs)
    assert result.pairs_resolved < result.pairs_total
    assert any("pair split rate" in f for f in result.failures)


def test_scorer_subfloor_pair_read_as_one_blob_is_covered_not_a_false_positive(pair_target):
    scenario, truth, frame, h_work = pair_target
    result = _score(scenario, truth, frame, h_work, oracle_blobs(truth, frame, h_work))
    assert result.subfloor_total >= 1
    assert result.fp_max == 0


def test_scorer_requires_the_ragged_cluster_to_be_a_ragged_blob():
    scenario = load_catalog()["ragged_10x7"]
    img, truth = render(scenario, 0)
    _, frame, h_work = working_from_truth(img, truth)
    good = oracle_blobs(truth, frame, h_work)
    assert _score(scenario, truth, frame, h_work, good).ragged_ok == 1
    as_single = [
        _blob(BlobKind.SINGLE, b.centers_px, b.area_px) if b.kind == BlobKind.RAGGED else b
        for b in good
    ]
    bad = _score(scenario, truth, frame, h_work, as_single)
    assert bad.ragged_ok == 0 and any("ragged" in f for f in bad.failures)
    missing = [b for b in good if b.kind != BlobKind.RAGGED]
    assert not _score(scenario, truth, frame, h_work, missing).passed


def test_scorer_fails_a_detector_that_finds_nothing():
    scenario = load_catalog()["clean_circle1"]
    img, truth = render(scenario, 0)
    result = score_render("clean_circle1", 0, img, truth, detect=lambda *a, **k: [])
    agg = aggregate("clean_circle1", False, [result])
    assert agg.recall == 0.0 and not agg.passed


def test_scorer_runtime_row_fails_a_slow_detector():
    scenario = load_catalog()["clean_circle1"]
    img, truth = render(scenario, 0)
    score = score_render("clean_circle1", 0, img, truth)
    assert verdict(aggregate("clean_circle1", False, [score])) == []
    score.runtime_s = 3.5
    assert any("runtime" in f for f in verdict(aggregate("clean_circle1", False, [score])))


def test_noisy_classification_follows_the_scenario_fields():
    catalog = load_catalog()
    assert not is_noisy(catalog["clean_circle1"])
    assert is_noisy(catalog["noisy_lighting"])
    assert is_noisy(catalog["hires_wipe_decoys"])
    assert is_noisy(catalog["cardboard_sticker_ragged"])


# ---------------------------------------------------------------- the CLI and the report


def _failing_result():
    one = RenderScore(
        scenario="synthetic",
        seed=0,
        n_isolated=10,
        matched_isolated=10,
        isolated_single=10,
        isolated_errors_in=[0.001] * 10,
        false_positives=7,
        pairs=[],
        clusters=[],
        es_errors_in=[],
        runtime_s=0.1,
        n_blobs=17,
    )
    return aggregate("synthetic", False, [one])


@pytest.mark.parametrize(
    "argv",
    [
        ["--only", "clean_gird"],
        ["--only", "clean_circle1,clean_gird"],
        ["--seeds", "4-0"],
        ["--seeds", "abc"],
        ["--seeds", "-1"],
    ],
)
def test_cli_exits_non_zero_when_nothing_would_be_evaluated(argv, capsys):
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert "nothing was evaluated" in err


def test_cli_exit_codes_for_pass_and_fail(monkeypatch, tmp_path, capsys):
    assert main(["--only", "clean_circle1", "--seeds", "0"]) == 0
    assert "1/1 scenarios pass" in capsys.readouterr().out

    monkeypatch.setattr(eval_detection, "evaluate_catalog", lambda *a, **k: [_failing_result()])
    report = tmp_path / "report.md"
    assert main(["--report", str(report), "--real-dir", str(tmp_path / "no_photos")]) == 1
    text = report.read_text(encoding="utf-8")
    assert "P-11 synthetic gate: FAIL" in text
    assert "B-08 escalation: **REQUIRED**" in text
    assert "not triggered" not in text and "gate: PASS" not in text


def test_evaluate_catalog_refuses_empty_selections():
    catalog = load_catalog()
    with pytest.raises(ValueError, match="no seeds"):
        evaluate_catalog(catalog, [])
    with pytest.raises(ValueError, match="unknown scenario"):
        evaluate_catalog(catalog, [0], only=["nope"])
    with pytest.raises(ValueError, match="no scenarios"):
        evaluate_catalog({}, [0])


def test_aggregate_with_no_renders_never_passes():
    result = aggregate("synthetic", False, [])
    assert not result.passed and result.failures == ["no renders scored"]


def test_parse_seeds_accepts_lists_and_rejects_bad_input():
    assert parse_seeds("0-2,7") == [0, 1, 2, 7]
    for bad in ("3-1", "x", ""):
        with pytest.raises(ValueError):
            parse_seeds(bad)


def test_report_conclusions_follow_the_verdict():
    passing = aggregate_passing()
    params = DetectParams()
    ok = build_report([passing], [0], params, [], eval_detection.load_real_counts())
    assert "P-11 synthetic gate: PASS" in ok
    assert "B-08 escalation: **not triggered**" in ok
    assert "REQUIRED" not in ok

    bad = build_report(
        [passing, _failing_result()], [0], params, [], eval_detection.load_real_counts()
    )
    assert "P-11 synthetic gate: FAIL" in bad and "`synthetic`" in bad
    assert "B-08 escalation: **REQUIRED**" in bad
    assert "not triggered" not in bad
    assert "gate: PASS" not in bad


def aggregate_passing():
    img, truth = render(load_catalog()["clean_circle1"], 0)
    return aggregate("clean_circle1", False, [score_render("clean_circle1", 0, img, truth)])


def test_design_points_only_cite_tests_that_exist():
    cited = set(re.findall(r"`(test_[a-z0-9_]+)`", DESIGN_POINTS))
    assert len(cited) >= 8
    defined = {name for name in globals() if name.startswith("test_")}
    assert cited <= defined, sorted(cited - defined)


# ---------------------------------------------------------------- assignment solver


def _brute_force_cost(cost):
    n, m = cost.shape
    if n <= m:
        return min(
            sum(cost[i, p[i]] for i in range(n)) for p in itertools.permutations(range(m), n)
        )
    return min(sum(cost[p[j], j] for j in range(m)) for p in itertools.permutations(range(n), m))


@pytest.mark.parametrize("shape", [(1, 1), (3, 3), (2, 5), (5, 2), (4, 6), (6, 4), (5, 5)])
def test_hungarian_matches_brute_force(shape):
    rng = np.random.default_rng(shape[0] * 10 + shape[1])
    for _ in range(25):
        cost = rng.uniform(0, 10, size=shape)
        pairs = hungarian(cost)
        assert len(pairs) == min(shape)
        assert len({i for i, _ in pairs}) == len(pairs) and len({j for _, j in pairs}) == len(pairs)
        assert math.isclose(
            sum(cost[i, j] for i, j in pairs), _brute_force_cost(cost), abs_tol=1e-9
        )


def test_hungarian_empty_matrix():
    assert hungarian(np.empty((0, 3))) == []
    assert hungarian(np.empty((3, 0))) == []
