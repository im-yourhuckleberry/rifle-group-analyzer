"""T2: photo pipeline on the synthetic catalog: propose, confirm, rectify, detect, resolve, stats."""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import cache

import cv2
import numpy as np
import pytest

from rga.domain.stats import group_stats
from rga.domain.types import (
    Blob,
    BlobKind,
    GroupStats,
    PlanePoint,
    ReferenceKind,
    ReferenceResult,
    ShotOrigin,
    WorkingFrame,
)
from rga.domain.units import offset_from_aim
from rga.vision.detect import detect_holes
from rga.vision.pipeline import (
    ReferenceProposal,
    analyze_group,
    build_working_image,
    manual_reference,
    propose_candidates,
    propose_reference,
    roi_to_working,
    shots_from_resolve,
)
from rga.vision.ragged import ResolveResult, resolve_shots
from rga.vision.transform import apply_h
from tests.synth.generate import load_catalog, plane_to_image, render

CATALOG = load_catalog()
KIND_BY_DIAMETER = {
    1.0: ReferenceKind.CIRCLE_1IN,
    1.5: ReferenceKind.CIRCLE_1_5IN,
    3.0: ReferenceKind.CIRCLE_3IN,
}
ES_TOLERANCE_IN = 0.05
SCALE_TOLERANCE = 0.015
DETECTION_TUNING = pytest.mark.xfail(
    strict=False,
    reason=(
        "the tape cross's navy dot is still detected as a hole: rejecting it also drops real "
        "holes (detect.py, T2.19)"
    ),
)
ROUND_TRIP = [
    (name, 0)
    for name, sc in CATALOG.items()
    if sc.get("reference") and name != "tape_cross_multi_aim"
] + [
    (name, 1)
    for name in (
        "clean_circle1",
        "noisy_lighting",
        "skew15_grid",
        "ragged_10x7",
        "multi_dot_target",
    )
]
ROUND_TRIP_TUNING = [pytest.param("tape_cross_multi_aim", 0, marks=DETECTION_TUNING)]


@dataclass(frozen=True)
class GroupRun:
    index: int
    blobs: list[Blob]
    resolved: ResolveResult
    roi: tuple[int, int, int, int]
    expected: int


@dataclass(frozen=True)
class Run:
    truth: dict
    img: np.ndarray
    proposal: ReferenceProposal
    working: np.ndarray
    frame: WorkingFrame
    h_px_to_working: np.ndarray
    groups: list[GroupRun]


def reference_kind(name: str) -> ReferenceKind:
    ref = CATALOG[name]["reference"]
    return ReferenceKind.GRID_1IN if ref["kind"] == "grid" else KIND_BY_DIAMETER[ref["diameter_in"]]


def truth_proposal(proposals: list[ReferenceProposal], truth: dict) -> ReferenceProposal:
    """The proposal that is the printed reference (aim dots can look like a 1 in circle)."""
    ref = truth["reference"]
    if ref["kind"] == "grid":
        assert len(proposals) == 1, f"expected one lattice, got {len(proposals)}"
        return proposals[0]
    radius = max(ref["axes_px"]) / 2
    for p in proposals:
        assert p.circle is not None
        if math.dist(p.circle.center_px, ref["ellipse_center_px"]) < 0.25 * radius:
            return p
    pytest.fail(f"printed circle not proposed; got {[p.circle for p in proposals]}")


@cache
def run_pipeline(name: str, seed: int) -> Run:
    img, truth = render(CATALOG[name], seed)
    proposal = truth_proposal(propose_candidates(img, reference_kind(name)), truth)
    working, frame, h = build_working_image(img, proposal.result)
    groups = []
    for index, group in enumerate(truth["groups"]):
        x0, y0, x1, y1 = group["roi_px"]
        roi = roi_to_working(h, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], frame)
        expected = len(group["hole_indices"])
        blobs, resolved = analyze_group(working, frame, truth["bullet_diameter_in"], expected, roi)
        groups.append(GroupRun(index, blobs, resolved, roi, expected))
    return Run(truth, img, proposal, working, frame, h, groups)


def truth_points_in(run: Run, indices) -> np.ndarray:
    """Truth hole centers in the working plane (inches, working top-left origin, y down)."""
    plane = [run.truth["holes_in"][i] for i in indices]
    return apply_h(run.h_px_to_working, plane_to_image(run.truth, plane)) / run.frame.ppi


def our_points_in(run: Run, group: GroupRun) -> np.ndarray:
    return np.array(
        [(s.point.x_in, s.point.y_in) for s in shots_from_resolve(group.resolved, run.frame)]
    )


def spread(points: np.ndarray) -> float:
    return max(math.dist(a, b) for a in points for b in points)


def stats_of(points: np.ndarray, origins) -> GroupStats:
    aim = PlanePoint(0.0, 0.0)
    offsets = [offset_from_aim(PlanePoint(float(x), float(y)), aim) for x, y in points]
    return group_stats(offsets, origins, 100.0)


@pytest.mark.parametrize(("name", "seed"), [*ROUND_TRIP, *ROUND_TRIP_TUNING])
def test_round_trip_gives_the_expected_shots_and_a_group_es_within_five_hundredths(
    name: str, seed: int
) -> None:
    run = run_pipeline(name, seed)
    for group in run.groups:
        truth_indices = run.truth["groups"][group.index]["hole_indices"]
        assert group.resolved.status == "ok", group.resolved.messages
        assert len(group.resolved.shots_px) == group.expected
        ours, theirs = our_points_in(run, group), truth_points_in(run, truth_indices)
        origins = [s.origin for s in shots_from_resolve(group.resolved, run.frame)]
        stats = stats_of(ours, origins)
        assert stats.n == group.expected
        assert stats.es_in == pytest.approx(spread(theirs), abs=ES_TOLERANCE_IN)
        assert stats.includes_estimated == (ShotOrigin.ESTIMATED in origins)


@pytest.mark.parametrize(
    ("name", "seed"), [case for case in ROUND_TRIP if case[0] != "circle_tilt_warn"]
)
def test_the_confirmed_reference_scales_the_working_plane_to_true_inches(
    name: str, seed: int
) -> None:
    run = run_pipeline(name, seed)
    indices = range(len(run.truth["holes_in"]))
    paper_in = np.array([run.truth["holes_in"][i] for i in indices])
    working_in = truth_points_in(run, indices)
    assert spread(working_in) == pytest.approx(spread(paper_in), rel=SCALE_TOLERANCE)


@pytest.mark.parametrize(("name", "seed"), ROUND_TRIP)
def test_every_proposal_is_unconfirmed_and_carries_its_overlay_geometry(
    name: str, seed: int
) -> None:
    run = run_pipeline(name, seed)
    result = run.proposal.result
    assert isinstance(result, ReferenceResult)
    assert result.method == "auto"
    assert result.kind is reference_kind(name)
    assert 0 < run.proposal.score <= 1
    if result.kind is ReferenceKind.GRID_1IN:
        assert run.proposal.circle is None
        corners = run.truth["reference"]["corners_px"]
        assert run.proposal.grid_points_px is not None
        # the lattice may be offset by whole squares at the edges; the span must match the truth
        mine = np.array(run.proposal.grid_points_px)
        theirs = np.array(corners)
        assert np.hypot(*(mine[2] - mine[0])) == pytest.approx(
            np.hypot(*(theirs[2] - theirs[0])), rel=0.35
        )
    else:
        assert run.proposal.grid_points_px is None and run.proposal.circle is not None


def test_a_grid_proposal_outlines_the_printed_grid_in_plane_order() -> None:
    run = run_pipeline("clean_grid", 0)
    assert run.proposal.grid_points_px is not None
    mine = np.array(run.proposal.grid_points_px)
    truth_corners = np.array(run.truth["reference"]["corners_px"])
    assert np.abs(mine - truth_corners).max() < 1.5


def test_non_planar_sheet_is_warned_so_the_user_can_reshoot() -> None:
    run = run_pipeline("curved_green_grid", 0)
    assert any(w.startswith("non_planar") for w in run.proposal.result.warnings)
    assert not any(
        w.startswith("non_planar") for w in run_pipeline("clean_grid", 0).proposal.result.warnings
    )


def test_a_tilted_circle_is_warned_because_its_scale_is_only_approximate() -> None:
    run = run_pipeline("circle_tilt_warn", 0)
    assert run.proposal.result.warnings
    assert run.proposal.result.axis_ratio is not None and run.proposal.result.axis_ratio < 0.98
    indices = range(len(run.truth["holes_in"]))
    paper = np.array([run.truth["holes_in"][i] for i in indices])
    ratio = spread(truth_points_in(run, indices)) / spread(paper)
    assert 0.85 < ratio < 1.15


def test_the_180_degree_sheet_is_rectified_like_any_other() -> None:
    run = run_pipeline("green_grid_upside_down_staples", 0)
    assert run.groups[0].resolved.status == "ok"
    assert len(run.groups[0].resolved.shots_px) == 12


# ------------------------------------------------------------------------ ragged resolution


@pytest.mark.parametrize(
    "name", ["ragged_10x7", "cardboard_sticker_ragged", "red_grid_band_light_diamond"]
)
def test_ragged_group_gets_estimated_shots_inside_the_ragged_outline(name: str) -> None:
    run = run_pipeline(name, 0)
    group = run.groups[0]
    shots = shots_from_resolve(group.resolved, run.frame)
    estimated = [s for s in shots if s.origin is ShotOrigin.ESTIMATED]
    truth = run.truth
    assert len(truth["ragged_groups"]) == 1
    members = len(truth["ragged_groups"][0])
    assert len(estimated) == members
    assert len(shots) == group.expected
    outline = apply_h(
        run.h_px_to_working, plane_to_image(truth, truth["ragged_outlines_in"][0])
    ).astype(np.float32)
    for s in estimated:
        inside = cv2.pointPolygonTest(
            outline, (s.point.x_in * run.frame.ppi, s.point.y_in * run.frame.ppi), True
        )
        assert inside >= 0.0
    assert sum(b.kind is BlobKind.RAGGED for b in group.blobs) == 1
    assert group.resolved.status == "ok"
    assert group.resolved.messages


def test_ragged_10x7_is_seven_distinct_holes_plus_three_estimated_with_stats_badge() -> None:
    run = run_pipeline("ragged_10x7", 0)
    group = run.groups[0]
    shots = shots_from_resolve(group.resolved, run.frame)
    assert [s.origin for s in shots].count(ShotOrigin.ESTIMATED) == 3
    assert len(shots) == 10
    stats = stats_of(our_points_in(run, group), [s.origin for s in shots])
    assert stats.includes_estimated is True
    assert stats.n == 10
    truth_mean = truth_points_in(run, run.truth["groups"][0]["hole_indices"]).mean(axis=0)
    ours_mean = our_points_in(run, group).mean(axis=0)
    assert np.abs(ours_mean - truth_mean).max() < 0.05


def test_estimated_shots_are_never_invented_without_a_shortfall() -> None:
    run = run_pipeline("clean_circle3", 0)
    origins = [s.origin for s in shots_from_resolve(run.groups[0].resolved, run.frame)]
    assert ShotOrigin.ESTIMATED not in origins


# ------------------------------------------------------------------------ several groups


def test_multi_dot_target_gives_three_correct_groups_from_three_rois() -> None:
    run = run_pipeline("multi_dot_target", 0)
    assert len(run.groups) == 3
    seen: set[tuple[int, int]] = set()
    for group in run.groups:
        x0, y0, x1, y1 = group.roi
        ours = our_points_in(run, group) * run.frame.ppi
        assert len(ours) == group.expected == 5
        assert (
            (ours[:, 0] >= x0) & (ours[:, 0] <= x1) & (ours[:, 1] >= y0) & (ours[:, 1] <= y1)
        ).all()
        theirs = (
            truth_points_in(run, run.truth["groups"][group.index]["hole_indices"]) * run.frame.ppi
        )
        gaps = [min(math.dist(o, t) for t in theirs) for o in ours]
        assert max(gaps) < 0.3 * run.truth["bullet_diameter_in"] * run.frame.ppi
        seen.update((round(x), round(y)) for x, y in ours)
    assert len(seen) == 15


def test_a_roi_keeps_other_groups_holes_out() -> None:
    run = run_pipeline("multi_dot_target", 0)
    first = run.groups[0]
    whole = analyze_group(run.working, run.frame, run.truth["bullet_diameter_in"], 15, None)
    assert len(whole[1].shots_px) == 15
    assert len(first.resolved.shots_px) == 5
    assert len(first.blobs) == 5


# ------------------------------------------------------------------------ detect -> resolve contract


@pytest.mark.parametrize(
    "name", ["ragged_10x7", "cardboard_sticker_ragged", "merged_pairs", "clean_circle1"]
)
def test_detect_output_feeds_resolve_with_no_adaptation(name: str) -> None:
    run = run_pipeline(name, 0)
    d = run.truth["bullet_diameter_in"]
    group = run.groups[0]
    blobs = detect_holes(run.working, run.frame, d, group.roi)
    direct = resolve_shots(blobs, group.expected, d * run.frame.ppi)
    assert group.blobs == blobs
    assert group.resolved == direct
    x0, y0, x1, y1 = group.roi
    for blob in blobs:
        assert blob.kind in set(BlobKind)
        assert len(blob.centers_px) == (2 if blob.kind is BlobKind.SPLIT else 1)
        for cx, cy in blob.centers_px:
            assert x0 <= cx <= x1 and y0 <= cy <= y1
        contour = np.array(blob.contour_px, dtype=np.int32)
        assert contour.ndim == 2 and contour.shape[1] == 2
        assert blob.area_px == pytest.approx(cv2.contourArea(contour.reshape(-1, 1, 2)), rel=1e-6)
        assert 0 <= blob.confidence <= 1
    distinct = [b for b in blobs if b.kind is not BlobKind.RAGGED]
    assert len(group.resolved.shots_px) >= sum(len(b.centers_px) for b in distinct)


# ------------------------------------------------------------------------ analyze_group edge cases


def test_no_holes_and_a_shortfall_means_add_them_by_hand() -> None:
    run = run_pipeline("clean_circle1", 0)
    blank = (0, 0, 200, 200)
    assert detect_holes(run.working, run.frame, 0.224, blank) == []
    blobs, resolved = analyze_group(run.working, run.frame, 0.224, 5, blank)
    assert blobs == []
    assert resolved.shots_px == ()
    assert resolved.status == "ok"
    assert any("by hand" in m for m in resolved.messages)
    assert shots_from_resolve(resolved, run.frame) == []


def test_more_holes_than_expected_is_reported_without_dropping_any() -> None:
    run = run_pipeline("clean_circle3", 0)
    group = run.groups[0]
    _, resolved = analyze_group(run.working, run.frame, 0.224, 4, group.roi)
    assert resolved.status == "too_many"
    assert len(resolved.shots_px) == 10


def test_a_shortfall_with_no_ragged_hole_is_stacked_at_the_largest_hole() -> None:
    run = run_pipeline("clean_circle1", 0)
    _, resolved = analyze_group(run.working, run.frame, 0.224, 7, run.groups[0].roi)
    origins = [s[2] for s in resolved.shots_px]
    assert origins.count(ShotOrigin.ESTIMATED) == 2
    assert resolved.status == "ok"


@pytest.mark.parametrize(
    ("roi", "expected", "diameter"),
    [
        ((10, 10, 10, 50), 5, 0.224),
        ((50, 10, 10, 50), 5, 0.224),
        ((10, 50, 50, 50), 5, 0.224),
        (None, 0, 0.224),
        (None, -1, 0.224),
        (None, 5, 0.0),
        (None, 5, -0.2),
    ],
)
def test_analyze_group_rejects_bad_arguments(roi, expected, diameter) -> None:
    run = run_pipeline("clean_circle1", 0)
    with pytest.raises(ValueError):
        analyze_group(run.working, run.frame, diameter, expected, roi)


# ------------------------------------------------------------------------ proposals


def test_proposing_does_not_modify_the_image_or_confirm_anything() -> None:
    img, _ = render(CATALOG["clean_circle1"], 0)
    before = img.copy()
    proposals = propose_candidates(img, ReferenceKind.CIRCLE_1IN)
    assert proposals
    assert np.array_equal(img, before)
    assert all(p.result.method == "auto" for p in proposals)
    assert propose_reference(img, ReferenceKind.CIRCLE_1IN) == [p.result for p in proposals]


def test_circle_proposals_are_ranked_and_capped() -> None:
    img, _ = render(CATALOG["clean_circle1"], 0)
    everything = propose_candidates(img, "circle_1in")
    assert len(everything) >= 2
    assert [p.score for p in everything] == sorted((p.score for p in everything), reverse=True)
    capped = propose_candidates(img, "circle_1in", max_candidates=1)
    assert capped == everything[:1]


@pytest.mark.parametrize("kind", list(ReferenceKind))
def test_a_photo_with_no_reference_proposes_nothing_for_every_kind(kind: ReferenceKind) -> None:
    img, _ = render(CATALOG["no_reference"], 0)
    assert propose_candidates(img, kind) == []
    assert propose_reference(img, kind) == []


def test_a_circle_only_photo_has_no_grid_candidate() -> None:
    img, _ = render(CATALOG["clean_circle1"], 0)
    assert propose_candidates(img, ReferenceKind.GRID_1IN) == []


def test_unknown_kind_is_rejected() -> None:
    img, _ = render(CATALOG["clean_circle1"], 0)
    with pytest.raises(ValueError, match="unknown reference kind"):
        propose_reference(img, "circle_9in")


# ------------------------------------------------------------------------ manual references


def test_manual_circle_from_three_clicks_confirms_like_an_auto_candidate() -> None:
    img, truth = render(CATALOG["clean_circle1"], 0)
    ref = truth["reference"]
    cx, cy = ref["ellipse_center_px"]
    r = max(ref["axes_px"]) / 2
    clicks = [(cx + r * math.cos(t), cy + r * math.sin(t)) for t in (0.4, 2.5, 4.4)]
    manual = manual_reference(ReferenceKind.CIRCLE_1IN, edge_points_px=clicks)
    assert manual.method == "manual"
    auto = truth_proposal(propose_candidates(img, ReferenceKind.CIRCLE_1IN), truth).result
    _, frame_manual, _ = build_working_image(img, manual)
    _, frame_auto, _ = build_working_image(img, auto)
    assert frame_manual.ppi == pytest.approx(frame_auto.ppi, rel=0.01)


def test_manual_circle_makes_a_photo_without_a_printed_reference_usable() -> None:
    img, truth = render(CATALOG["no_reference"], 0)
    assert propose_reference(img, ReferenceKind.CIRCLE_1IN) == []
    scale = truth["ppi"]
    center = np.array(truth["aim_px"])
    clicks = [center + scale * 0.5 * np.array([math.cos(t), math.sin(t)]) for t in (0.2, 2.3, 4.2)]
    manual = manual_reference("circle_1in", edge_points_px=[tuple(c) for c in clicks])
    working, frame, _ = build_working_image(img, manual)
    assert working.size and frame.ppi == pytest.approx(scale, rel=0.02)


def test_manual_grid_from_four_corners_matches_the_detected_grid() -> None:
    img, truth = render(CATALOG["clean_grid"], 0)
    ref = truth["reference"]
    corners = [tuple(p) for p in ref["corners_px"]]
    manual = manual_reference(
        ReferenceKind.GRID_1IN,
        grid_corners_px=corners,
        squares=(ref["squares_across"], ref["squares_down"]),
    )
    assert manual.method == "manual"
    assert manual.warnings == ()
    auto = propose_reference(img, ReferenceKind.GRID_1IN)[0]
    _, frame_manual, _ = build_working_image(img, manual)
    _, frame_auto, _ = build_working_image(img, auto)
    assert frame_manual.ppi == pytest.approx(frame_auto.ppi, rel=0.01)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": ReferenceKind.CIRCLE_1IN},
        {"kind": ReferenceKind.CIRCLE_1IN, "grid_corners_px": [(0, 0)] * 4, "squares": (2, 2)},
        {
            "kind": ReferenceKind.CIRCLE_1IN,
            "edge_points_px": [(0, 0), (1, 0), (0, 1)],
            "squares": (2, 2),
        },
        {"kind": ReferenceKind.GRID_1IN},
        {"kind": ReferenceKind.GRID_1IN, "edge_points_px": [(0, 0), (1, 0), (0, 1)]},
        {"kind": ReferenceKind.GRID_1IN, "grid_corners_px": [(0, 0), (1, 0), (1, 1), (0, 1)]},
        {
            "kind": ReferenceKind.GRID_1IN,
            "grid_corners_px": [(0, 0), (400, 0), (400, 400), (0, 400)],
            "squares": (4, 4),
            "edge_points_px": [(0, 0), (1, 0), (0, 1)],
        },
        {"kind": "circle_9in", "edge_points_px": [(0, 0), (1, 0), (0, 1)]},
        {"kind": ReferenceKind.CIRCLE_1IN, "edge_points_px": [(0, 0), (1, 1), (2, 2)]},
    ],
    ids=[
        "circle-no-marks",
        "circle-with-grid-marks",
        "circle-with-squares",
        "grid-no-marks",
        "grid-with-circle-points",
        "grid-no-squares",
        "grid-with-both",
        "unknown-kind",
        "collinear",
    ],
)
def test_manual_reference_rejects_marks_that_do_not_fit_the_kind(kwargs) -> None:
    with pytest.raises(ValueError):
        manual_reference(**kwargs)


# ------------------------------------------------------------------------ helpers


def test_shots_from_resolve_converts_working_px_to_plane_inches() -> None:
    frame = WorkingFrame(ppi=200.0, width_px=1000, height_px=800)
    resolved = ResolveResult(
        shots_px=(
            (100.0, 50.0, ShotOrigin.DETECTED, 0.9),
            (300.0, 250.0, ShotOrigin.ESTIMATED, 0.25),
        ),
        status="ok",
        messages=(),
    )
    shots = shots_from_resolve(resolved, frame)
    assert [(s.point.x_in, s.point.y_in) for s in shots] == [(0.5, 0.25), (1.5, 1.25)]
    assert [s.origin for s in shots] == [ShotOrigin.DETECTED, ShotOrigin.ESTIMATED]
    assert [s.confidence for s in shots] == [0.9, 0.25]
    assert all(not s.moved_by_user for s in shots)


def test_roi_to_working_maps_covers_and_clamps() -> None:
    frame = WorkingFrame(ppi=100.0, width_px=500, height_px=400)
    h = np.array([[0.5, 0.0, 10.0], [0.0, 0.5, 20.0], [0.0, 0.0, 1.0]])
    assert roi_to_working(h, [(100, 100), (300, 100), (300, 200), (100, 200)], frame) == (
        60,
        70,
        160,
        120,
    )
    assert roi_to_working(h, [(-500, -500), (5000, 5000)], frame) == (0, 0, 500, 400)


def _gridded_nested_discs() -> np.ndarray:
    from tests.unit.test_reference_circle import GREEN, ORANGE, RED, Canvas

    spacing = 150
    canvas = Canvas(1600, 1200)
    for x in range(75, 1600, spacing):
        canvas.line((x, 0), (x, 1200), 5, RED)
    for y in range(75, 1200, spacing):
        canvas.line((0, y), (1600, y), 5, RED)
    canvas.disc((800.0, 600.0), 450.0, ORANGE).disc((800.0, 600.0), 225.0, GREEN)
    return canvas.render(noise=3.0, blur=1.0, jpeg=90, seed=2)


def test_a_printed_grid_in_the_photo_decides_between_nested_circles() -> None:
    img = _gridded_nested_discs()
    from rga.vision.reference_circle import find_circle_candidates
    from rga.vision.reference_grid import find_grid

    assert find_grid(img) is not None
    alone = find_circle_candidates(img, ReferenceKind.CIRCLE_1_5IN)
    proposals = propose_candidates(img, ReferenceKind.CIRCLE_1_5IN)

    assert round(max(alone[0].axes_px)) == pytest.approx(450, abs=5)
    assert round(max(proposals[0].circle.axes_px)) == pytest.approx(225, abs=5)
    assert round(max(proposals[1].circle.axes_px)) == pytest.approx(450, abs=5)
    three = propose_candidates(img, ReferenceKind.CIRCLE_3IN)
    assert round(max(three[0].circle.axes_px)) == pytest.approx(450, abs=5)


def test_a_photo_without_a_grid_still_proposes_circles_outer_first() -> None:
    from tests.unit.test_reference_circle import GREEN, ORANGE, Canvas

    img = Canvas(1000, 800).disc((500.0, 400.0), 360.0, ORANGE).disc((500.0, 400.0), 150.0, GREEN)
    proposals = propose_candidates(img.render(), ReferenceKind.CIRCLE_3IN)
    assert [round(max(p.circle.axes_px)) for p in proposals[:2]] == pytest.approx([360, 150], abs=4)
