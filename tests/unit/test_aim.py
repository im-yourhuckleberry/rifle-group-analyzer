"""T1: `rga.vision.aim.propose_aims` on drawn marks and on rendered synthetic targets.

Drawn images pin each mark kind, the ranking rules (proximity, grid only without a stronger mark,
the confirmed reference first), the search window and the input contract. Rendered scenarios
(`tests/synth`) pin the acceptance: dots, diamonds and crosses within 0.03 in of the true aim and
grid crossings within 0.05 in, with bullet holes on and around the marks.
"""

from __future__ import annotations

import math

import cv2
import numpy as np
import pytest

from rga.vision.aim import MAX_CANDIDATES, AimCandidate, propose_aims
from tests.synth.aim_scenarios import aim_scene

PPI = 100.0
PAPER = (232, 233, 230)
KINDS = {"paster_center", "dot_center", "diamond_center", "cross_center", "grid_intersection"}
DOT_TOLERANCE_IN = 0.03
GRID_TOLERANCE_IN = 0.05


def paper(width: int = 900, height: int = 900, ppi: float = PPI) -> np.ndarray:
    """A plain sheet with a little texture so colour statistics are not degenerate."""
    rng = np.random.default_rng(3)
    image = np.full((height, width, 3), PAPER, np.float32)
    image += rng.normal(0, 1.5, image.shape)
    return np.clip(image, 0, 255).astype(np.uint8)


def draw_disc(image, center, diameter_in, color, ppi=PPI):
    radius = diameter_in * ppi / 2
    cv2.circle(
        image,
        (round(center[0] * 8), round(center[1] * 8)),
        round(radius * 8),
        color,
        -1,
        cv2.LINE_AA,
        3,
    )


def draw_diamond(image, center, diagonal_in, color, ppi=PPI, angle_deg=0.0):
    half = diagonal_in * ppi / 2
    theta = math.radians(angle_deg)
    corners = []
    for dx, dy in ((0, -half), (half, 0), (0, half), (-half, 0)):
        corners.append(
            (
                center[0] + dx * math.cos(theta) - dy * math.sin(theta),
                center[1] + dx * math.sin(theta) + dy * math.cos(theta),
            )
        )
    cv2.fillPoly(image, [np.round(np.array(corners) * 8).astype(np.int32)], color, cv2.LINE_AA, 3)


def draw_line(image, a, b, color, thickness_px=3):
    cv2.line(
        image,
        (round(a[0] * 8), round(a[1] * 8)),
        (round(b[0] * 8), round(b[1] * 8)),
        color,
        thickness_px,
        cv2.LINE_AA,
        3,
    )


def draw_holes(image, points, ppi=PPI):
    for point in points:
        draw_disc(image, point, 0.30, (150, 150, 150), ppi)
        draw_disc(image, point, 0.22, (25, 25, 25), ppi)


def roi_around(points, margin_px=25):
    pts = np.array(points, dtype=float)
    return (
        int(pts[:, 0].min() - margin_px),
        int(pts[:, 1].min() - margin_px),
        int(pts[:, 0].max() + margin_px),
        int(pts[:, 1].max() + margin_px),
    )


def error_in(candidate: AimCandidate, truth, ppi=PPI) -> float:
    return math.hypot(candidate.point_px[0] - truth[0], candidate.point_px[1] - truth[1]) / ppi


GROUP_HOLES = [(420, 400), (480, 440), (430, 470), (500, 400)]
GROUP_ROI = roi_around(GROUP_HOLES)


class TestDrawnMarks:
    def test_a_coloured_disc_is_a_paster_centre(self):
        image = paper()
        center = (451.3, 431.6)
        draw_disc(image, center, 1.0, (60, 160, 60))
        draw_holes(image, GROUP_HOLES)
        found = propose_aims(image, GROUP_ROI, PPI)
        assert found[0].kind == "paster_center"
        assert error_in(found[0], center) < DOT_TOLERANCE_IN

    def test_a_black_disc_is_a_dot_centre(self):
        image = paper()
        center = (451.3, 431.6)
        draw_disc(image, center, 1.0, (20, 20, 20))
        found = propose_aims(image, GROUP_ROI, PPI)
        assert found[0].kind == "dot_center"
        assert error_in(found[0], center) < DOT_TOLERANCE_IN

    @pytest.mark.parametrize("angle", [0.0, 45.0, 17.0])
    def test_a_black_diamond_is_found_at_its_centre_at_any_rotation(self, angle):
        image = paper()
        center = (447.2, 428.9)
        draw_diamond(image, center, 1.0, (20, 20, 20), angle_deg=angle)
        found = propose_aims(image, GROUP_ROI, PPI)
        assert found[0].kind == "diamond_center"
        assert error_in(found[0], center) < DOT_TOLERANCE_IN

    def test_bullet_holes_biting_a_diamond_do_not_move_its_centre(self):
        image = paper()
        center = (447.2, 428.9)
        draw_diamond(image, center, 1.0, (20, 120, 20), angle_deg=45.0)
        draw_holes(
            image,
            [
                (center[0] - 28, center[1] - 30),
                (center[0] + 30, center[1] + 25),
                (center[0] + 5, center[1] - 48),
            ],
        )
        found = propose_aims(
            image,
            roi_around([(center[0] - 28, center[1] - 30), (center[0] + 30, center[1] + 25)]),
            PPI,
        )
        assert found[0].kind == "diamond_center"
        assert error_in(found[0], center) < DOT_TOLERANCE_IN

    @pytest.mark.parametrize("kind", ["plus", "x"])
    def test_a_pen_cross_is_found_where_its_strokes_meet(self, kind):
        image = paper()
        center = (440.4, 421.7)
        arm = 50.0
        if kind == "plus":
            segments = [
                ((center[0] - arm, center[1]), (center[0] + arm, center[1])),
                ((center[0], center[1] - arm), (center[0], center[1] + arm)),
            ]
        else:
            d = arm / math.sqrt(2)
            segments = [
                ((center[0] - d, center[1] - d), (center[0] + d, center[1] + d)),
                ((center[0] - d, center[1] + d), (center[0] + d, center[1] - d)),
            ]
        for a, b in segments:
            draw_line(image, a, b, (20, 20, 20), 3)
        found = propose_aims(image, GROUP_ROI, PPI)
        assert found[0].kind == "cross_center"
        assert error_in(found[0], center) < DOT_TOLERANCE_IN

    def test_a_thick_tape_cross_is_found_at_its_centre(self):
        image = paper()
        center = (450.0, 430.0)
        for a, b in (
            ((center[0] - 80, center[1]), (center[0] + 80, center[1])),
            ((center[0], center[1] - 80), (center[0], center[1] + 80)),
        ):
            draw_line(image, a, b, (200, 120, 40), 22)
        found = propose_aims(image, GROUP_ROI, PPI)
        assert found[0].kind == "cross_center"
        assert error_in(found[0], center) < DOT_TOLERANCE_IN

    def test_grid_crossings_are_offered_when_there_is_no_mark(self):
        image = paper()
        for k in range(1, 9):
            draw_line(image, (k * 100.0 + 1.3, 0), (k * 100.0 + 1.3, 899), (60, 160, 60), 3)
            draw_line(image, (0, k * 100.0 - 0.7), (899, k * 100.0 - 0.7), (60, 160, 60), 3)
        found = propose_aims(image, GROUP_ROI, PPI)
        assert found and all(c.kind == "grid_intersection" for c in found)
        roi_centre = ((GROUP_ROI[0] + GROUP_ROI[2]) / 2, (GROUP_ROI[1] + GROUP_ROI[3]) / 2)
        nearest = min(
            ((x * 100.0 + 1.3, y * 100.0 - 0.7) for x in range(1, 9) for y in range(1, 9)),
            key=lambda p: math.hypot(p[0] - roi_centre[0], p[1] - roi_centre[1]),
        )
        assert error_in(found[0], nearest) < GRID_TOLERANCE_IN
        assert found[0].score < 0.5

    def test_grid_crossings_are_left_out_when_a_stronger_mark_exists(self):
        image = paper()
        for k in range(1, 9):
            draw_line(image, (k * 100.0, 0), (k * 100.0, 899), (60, 160, 60), 3)
            draw_line(image, (0, k * 100.0), (899, k * 100.0), (60, 160, 60), 3)
        draw_diamond(image, (452.0, 433.0), 1.0, (20, 20, 20), angle_deg=45.0)
        found = propose_aims(image, GROUP_ROI, PPI)
        assert found[0].kind == "diamond_center"
        assert not any(c.kind == "grid_intersection" for c in found)

    def test_grid_junctions_beside_a_big_mark_are_not_taken_for_crosses(self):
        image = paper()
        for k in range(1, 10):
            draw_line(image, (k * 90.0, 0), (k * 90.0, 899), (60, 60, 200), 3)
            draw_line(image, (0, k * 90.0), (899, k * 90.0), (60, 60, 200), 3)
        draw_disc(image, (452.0, 433.0), 1.6, (40, 120, 235))
        draw_diamond(image, (452.0, 433.0), 0.6, (20, 20, 20), angle_deg=45.0)
        found = propose_aims(image, GROUP_ROI, PPI)
        assert found[0].kind == "diamond_center"
        assert not any(c.kind == "cross_center" for c in found)

    def test_junctions_of_an_irregular_grid_are_not_taken_for_crosses(self):
        image = paper()
        for k in range(1, 10):
            draw_line(image, (k * 90.0, 0), (k * 90.0, 899), (60, 60, 200), 3)
        for y in (70, 190, 250, 400, 520, 610, 780):
            draw_line(image, (0, float(y)), (899, float(y)), (60, 60, 200), 3)
        draw_diamond(image, (452.0, 433.0), 0.6, (20, 20, 20), angle_deg=45.0)
        found = propose_aims(image, GROUP_ROI, PPI)
        assert found[0].kind == "diamond_center"
        assert not any(c.kind == "cross_center" for c in found)

    def test_blank_paper_has_no_candidates(self):
        assert propose_aims(paper(), GROUP_ROI, PPI) == []

    def test_bullet_holes_alone_are_not_aim_candidates(self):
        image = paper()
        draw_holes(image, GROUP_HOLES)
        assert propose_aims(image, GROUP_ROI, PPI) == []


class TestRanking:
    def test_the_mark_nearer_the_group_ranks_first(self):
        image = paper(1100, 900)
        near, far = (451.0, 431.0), (451.0 + 190, 431.0)
        draw_disc(image, near, 1.0, (60, 160, 60))
        draw_disc(image, far, 1.0, (60, 160, 60))
        found = propose_aims(image, GROUP_ROI, PPI)
        assert [round(error_in(c, near)) for c in found[:2]] == [0, 2]
        assert found[0].score > found[1].score

    def test_a_mark_beyond_the_search_window_is_not_proposed(self):
        image = paper(1400, 900)
        draw_disc(image, (1100.0, 431.0), 1.0, (60, 160, 60))
        assert propose_aims(image, GROUP_ROI, PPI) == []

    def test_a_confirmed_circle_reference_near_the_group_is_the_top_candidate(self):
        image = paper()
        draw_diamond(image, (452.0, 433.0), 1.0, (20, 20, 20), angle_deg=45.0)
        reference = (430.0, 415.0)
        found = propose_aims(image, GROUP_ROI, PPI, reference_center_px=reference)
        assert found[0].kind == "paster_center"
        assert found[0].point_px == reference
        assert found[0].score == 1.0
        assert any(c.kind == "diamond_center" for c in found[1:])

    def test_a_reference_far_from_the_group_is_ignored(self):
        image = paper(1400, 900)
        found = propose_aims(image, GROUP_ROI, PPI, reference_center_px=(1300.0, 100.0))
        assert found == []

    def test_candidates_are_sorted_scored_and_capped(self):
        image = paper(1000, 1000)
        for i, y in enumerate((300, 400, 500, 600, 700, 800)):
            draw_disc(image, (450.0 + 30 * i, float(y)), 0.9, (60, 160, 60))
        found = propose_aims(image, (300, 250, 700, 850), PPI, max_candidates=3)
        assert len(found) == 3
        assert [c.score for c in found] == sorted((c.score for c in found), reverse=True)
        assert all(0 < c.score <= 1 and c.kind in KINDS for c in found)
        assert len(propose_aims(image, (300, 250, 700, 850), PPI)) <= MAX_CANDIDATES

    def test_the_same_input_gives_the_same_answer(self):
        image = paper()
        draw_diamond(image, (452.0, 433.0), 1.0, (20, 20, 20), angle_deg=45.0)
        assert propose_aims(image, GROUP_ROI, PPI) == propose_aims(image, GROUP_ROI, PPI)

    @pytest.mark.parametrize("ppi", [60.0, 200.0])
    def test_the_result_does_not_depend_on_the_working_scale(self, ppi):
        scale = ppi / PPI
        image = cv2.resize(paper(), None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
        center = (447.2 * scale, 428.9 * scale)
        draw_diamond(image, center, 1.0, (20, 20, 20), ppi=ppi, angle_deg=45.0)
        roi = tuple(int(v * scale) for v in GROUP_ROI)
        found = propose_aims(image, roi, ppi)
        assert found[0].kind == "diamond_center"
        assert error_in(found[0], center, ppi) < DOT_TOLERANCE_IN


class TestBulletHolesAreNotAims:
    """FG3c.2 / G3c3-W2: hole-sized or shot-coincident marks are never proposed."""

    BULLET_IN = 0.224

    def test_a_candidate_on_a_shot_is_dropped_and_the_real_mark_stays(self):
        image = paper()
        draw_diamond(image, (452.0, 433.0), 0.8, (20, 20, 20), angle_deg=45.0)
        draw_diamond(image, (330.0, 520.0), 0.6, (20, 20, 20), angle_deg=45.0)
        without = propose_aims(image, GROUP_ROI, PPI)
        with_shot = propose_aims(
            image, GROUP_ROI, PPI, bullet_diameter_in=self.BULLET_IN, shots_px=[(330.0, 520.0)]
        )
        assert len(without) == 2
        assert [round(error_in(c, (452.0, 433.0))) for c in with_shot] == [0]

    def test_a_shot_just_outside_one_bullet_diameter_keeps_the_candidate(self):
        image = paper()
        draw_diamond(image, (452.0, 433.0), 0.5, (20, 20, 20), angle_deg=45.0)
        far = (452.0 + 0.224 * PPI + 3, 433.0)
        near = (452.0 + 0.224 * PPI - 3, 433.0)
        assert propose_aims(image, GROUP_ROI, PPI, bullet_diameter_in=0.224, shots_px=[far])
        assert not propose_aims(image, GROUP_ROI, PPI, bullet_diameter_in=0.224, shots_px=[near])

    def test_a_big_mark_with_a_hit_near_its_centre_stays(self):
        image = paper()
        draw_disc(image, (452.0, 433.0), 1.0, (60, 160, 60))
        draw_holes(image, [(460.0, 440.0)])
        found = propose_aims(
            image, GROUP_ROI, PPI, bullet_diameter_in=0.224, shots_px=[(460.0, 440.0)]
        )
        assert found[0].kind == "paster_center"
        assert error_in(found[0], (452.0, 433.0)) < DOT_TOLERANCE_IN

    def test_the_confirmed_reference_centre_is_exempt_from_the_shot_filter(self):
        found = propose_aims(
            paper(),
            GROUP_ROI,
            PPI,
            reference_center_px=(430.0, 415.0),
            bullet_diameter_in=0.224,
            shots_px=[(430.0, 415.0)],
        )
        assert [c.point_px for c in found] == [(430.0, 415.0)]

    def test_a_diamond_must_be_longer_than_one_and_four_tenths_bullets(self):
        image = paper()
        draw_diamond(image, (452.0, 433.0), 0.30, (20, 20, 20), angle_deg=45.0)
        assert [c.kind for c in propose_aims(image, GROUP_ROI, PPI)] == ["diamond_center"]
        assert propose_aims(image, GROUP_ROI, PPI, bullet_diameter_in=0.224) == []
        draw_diamond(image, (452.0, 433.0), 0.40, (20, 20, 20), angle_deg=45.0)
        assert (
            propose_aims(image, GROUP_ROI, PPI, bullet_diameter_in=0.224)[0].kind
            == "diamond_center"
        )

    @pytest.mark.parametrize("seed", [0, 1, 2])
    def test_no_candidate_sits_on_a_hole_in_the_red_grid_band_scene(self, seed):
        scene = aim_scene("red_grid_band_light_diamond", seed)
        found = propose_aims(
            scene.working,
            scene.roi,
            scene.ppi,
            bullet_diameter_in=self.BULLET_IN,
            shots_px=scene.holes_px,
        )
        reach = self.BULLET_IN * scene.ppi
        assert all(math.dist(c.point_px, hole) >= reach for c in found for hole in scene.holes_px)

    @pytest.mark.parametrize("seed", [0, 1])
    def test_the_real_diamond_leads_once_the_holes_are_excluded(self, seed):
        scene = aim_scene("red_grid_band_light_diamond", seed)
        found = propose_aims(
            scene.working,
            scene.roi,
            scene.ppi,
            bullet_diameter_in=self.BULLET_IN,
            shots_px=scene.holes_px,
        )
        assert found[0].kind == "diamond_center"
        assert error_in(found[0], scene.aim_px, scene.ppi) < DOT_TOLERANCE_IN


class TestDegenerateImages:
    """FG3c.2 / G3c3-W1: OpenCV failures on tiny images surface as ValueError, never cv2.error."""

    def test_a_tiny_working_image_is_a_value_error(self):
        tiny = np.random.default_rng(0).integers(0, 255, (8, 11, 3), dtype=np.uint8)
        with pytest.raises(ValueError, match="cannot be analysed"):
            propose_aims(tiny, (0, 0, 11, 8), 200.0)

    @pytest.mark.parametrize("shape", [(30, 40), (60, 60)])
    def test_small_images_give_no_candidates_or_a_value_error(self, shape):
        image = np.random.default_rng(0).integers(0, 255, (*shape, 3), dtype=np.uint8)
        try:
            found = propose_aims(image, (0, 0, shape[1], shape[0]), 200.0)
        except ValueError:
            return
        assert isinstance(found, list)


class TestInputContract:
    def test_a_grey_image_is_refused(self):
        with pytest.raises(ValueError, match="BGR"):
            propose_aims(np.zeros((100, 100), np.uint8), (10, 10, 50, 50), PPI)

    @pytest.mark.parametrize("ppi", [0.0, -5.0, float("nan"), float("inf")])
    def test_a_bad_ppi_is_refused(self, ppi):
        with pytest.raises(ValueError, match="ppi"):
            propose_aims(paper(200, 200), (10, 10, 50, 50), ppi)

    def test_a_non_positive_candidate_cap_is_refused(self):
        with pytest.raises(ValueError, match="max_candidates"):
            propose_aims(paper(200, 200), (10, 10, 50, 50), PPI, max_candidates=0)

    @pytest.mark.parametrize(
        "roi", [(50, 50, 50, 80), (60, 10, 20, 80), (-50, -50, -10, -10), (500, 500, 600, 600)]
    )
    def test_an_empty_or_outside_roi_is_refused(self, roi):
        with pytest.raises(ValueError, match="roi"):
            propose_aims(paper(200, 200), roi, PPI)

    def test_a_roi_reaching_past_the_image_is_clamped(self):
        image = paper()
        draw_disc(image, (451.0, 431.0), 1.0, (60, 160, 60))
        assert propose_aims(image, (300, 300, 5000, 5000), PPI)[0].kind == "paster_center"


# ------------------------------------------------------------------- rendered synthetic targets


def _best_error(name: str, seed: int, group: int = 0) -> tuple[AimCandidate, float]:
    scene = aim_scene(name, seed, group)
    found = propose_aims(scene.working, scene.roi, scene.ppi)
    assert found, f"{name} seed {seed} group {group}: no candidates"
    return found[0], error_in(found[0], scene.aim_px, scene.ppi)


ROUND_CASES = [
    ("clean_circle1", 0, 0),
    ("clean_circle1", 1, 0),
    ("noisy_lighting", 0, 0),
    ("ragged_10x7", 0, 0),
    ("black_dot_mixed", 0, 0),
    ("black_bull_light_holes", 0, 0),
    ("clean_grid", 0, 0),
    ("multi_dot_target", 0, 0),
    ("multi_dot_target", 0, 2),
]
DIAMOND_CASES = [
    ("no_reference", 0, 0),
    ("no_reference", 2, 0),
    ("multi_dot_target", 0, 1),
    ("multi_dot_target", 1, 1),
    ("curved_green_grid", 0, 0),
    ("green_grid_upside_down_staples", 0, 0),
    ("tape_cross_multi_aim", 0, 0),
    ("tape_cross_multi_aim", 1, 1),
]
CROSS_CASES = [
    ("tape_cross_multi_aim", 0, 2),
    ("tape_cross_multi_aim", 4, 2),
    ("pen_plus_circle_sheet", 0, 0),
    ("pen_x_circle_sheet", 1, 0),
    ("pen_x_noisy", 1, 0),
]
GRID_CASES = [
    ("grid_crossing_clean", 0, 0),
    ("grid_crossing_skew10", 0, 0),
    ("grid_crossing_curved", 0, 0),
    ("pen_plus_grid_sheet", 0, 0),
]


class TestSyntheticAcceptance:
    @pytest.mark.parametrize(("name", "seed", "group"), ROUND_CASES)
    def test_a_dot_or_paster_is_recovered_within_0_03_in(self, name, seed, group):
        best, error = _best_error(name, seed, group)
        assert best.kind in {"paster_center", "dot_center"}
        assert error < DOT_TOLERANCE_IN

    @pytest.mark.parametrize(("name", "seed", "group"), DIAMOND_CASES)
    def test_a_diamond_is_recovered_within_0_03_in(self, name, seed, group):
        best, error = _best_error(name, seed, group)
        assert best.kind == "diamond_center"
        assert error < DOT_TOLERANCE_IN

    @pytest.mark.parametrize(("name", "seed", "group"), CROSS_CASES)
    def test_a_cross_is_recovered_within_0_03_in(self, name, seed, group):
        best, error = _best_error(name, seed, group)
        assert best.kind == "cross_center"
        assert error < DOT_TOLERANCE_IN

    @pytest.mark.parametrize(("name", "seed", "group"), GRID_CASES)
    def test_a_grid_crossing_is_recovered_within_0_05_in(self, name, seed, group):
        best, error = _best_error(name, seed, group)
        assert best.kind == "grid_intersection"
        assert error < GRID_TOLERANCE_IN

    @pytest.mark.parametrize(
        ("name", "seed"), [("grid_crossing_clean", 0), ("grid_crossing_skew10", 1)]
    )
    def test_a_grid_crossing_on_a_regular_sheet_is_precise_to_0_01_in(self, name, seed):
        best, error = _best_error(name, seed)
        assert best.kind == "grid_intersection"
        assert error < 0.01

    def test_on_a_multi_aim_sheet_each_group_finds_its_own_mark_first(self):
        for group in range(3):
            _, error = _best_error("tape_cross_multi_aim", 0, group)
            assert error < DOT_TOLERANCE_IN
