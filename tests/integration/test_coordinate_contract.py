"""T2 contract: synthetic truth (paper-origin plane, y down) -> image px -> build_working ->
working plane -> offset_from_aim (the single y-flip) -> group_stats agree, and high-right stays
positive/positive.
"""

import cv2
import numpy as np
import pytest

from rga.domain.stats import group_stats
from rga.domain.types import Offset, PlanePoint, ShotOrigin
from rga.domain.units import offset_from_aim
from rga.vision.transform import (
    apply_h,
    build_working,
    plane_to_working_px,
    working_px_to_plane,
)
from tests.synth.generate import load_catalog, render_named

DISTANCE_YD = 100
CHAIN_SCENARIOS = [
    "high_right_group",
    "clean_circle1",
    "clean_grid",
    "skew10_grid",
    "skew15_grid",
    "multi_dot_target",
    "merged_pairs",
    "ragged_10x7",
    "circle_tilt_warn",
]
ES_TOL_IN = 0.003
MEAN_TOL_IN = 0.002


def _truth_offsets(truth: dict, group: dict) -> list[Offset]:
    aim = PlanePoint(*group["aim_in"])
    return [offset_from_aim(PlanePoint(*truth["holes_in"][i]), aim) for i in group["hole_indices"]]


def _chain_offsets(img: np.ndarray, truth: dict, group: dict):
    """Truth holes and aim carried through image px and the working image to plane offsets."""
    h_plane_to_image = np.array(truth["h_plane_to_image"], dtype=np.float64)
    working, frame, h_px_to_working = build_working(img, np.linalg.inv(h_plane_to_image))
    holes_px = apply_h(
        h_plane_to_image, np.array([truth["holes_in"][i] for i in group["hole_indices"]])
    )
    aim_px = np.array([group["aim_px"]], dtype=np.float64)
    holes = working_px_to_plane(apply_h(h_px_to_working, holes_px), frame)
    aim = working_px_to_plane(apply_h(h_px_to_working, aim_px), frame)[0]
    return working, frame, holes, aim, [offset_from_aim(p, aim) for p in holes]


@pytest.fixture(scope="module")
def renders():
    cache: dict[str, tuple[np.ndarray, dict]] = {}

    def get(name: str):
        if name not in cache:
            cache[name] = render_named(name, 0)
        return cache[name]

    return get


def test_chain_scenarios_exist_in_the_catalog():
    assert set(CHAIN_SCENARIOS) <= set(load_catalog())


class TestHighRightGroupConvention:
    def test_truth_group_is_high_and_right_of_aim(self, renders):
        _, truth = renders("high_right_group")
        group = truth["groups"][0]
        hole_ys = [truth["holes_in"][i][1] for i in group["hole_indices"]]
        hole_xs = [truth["holes_in"][i][0] for i in group["hole_indices"]]
        assert np.mean(hole_ys) < group["aim_in"][1]  # y-down paper plane: smaller y is higher
        assert np.mean(hole_xs) > group["aim_in"][0]

    def test_offsets_from_truth_have_positive_mean_dx_and_dy(self, renders):
        _, truth = renders("high_right_group")
        group = truth["groups"][0]
        offsets = _truth_offsets(truth, group)
        stats = group_stats(offsets, [ShotOrigin.DETECTED] * len(offsets), DISTANCE_YD)
        assert stats.n == 10
        assert stats.mean_dx_in == pytest.approx(1.2, abs=0.5)
        assert stats.mean_dy_in == pytest.approx(0.9, abs=0.5)
        assert stats.mean_dx_mil > 0
        assert stats.mean_dy_mil > 0

    def test_offsets_through_the_working_image_stay_high_and_right(self, renders):
        img, truth = renders("high_right_group")
        group = truth["groups"][0]
        *_, offsets = _chain_offsets(img, truth, group)
        stats = group_stats(offsets, [ShotOrigin.DETECTED] * len(offsets), DISTANCE_YD)
        assert stats.mean_dx_in > 0
        assert stats.mean_dy_in > 0

    def test_dark_pixels_of_the_rendered_group_sit_high_and_right_of_the_aim(self, renders):
        img, truth = renders("high_right_group")
        group = truth["groups"][0]
        working, frame, _, aim, _ = _chain_offsets(img, truth, group)
        gray = cv2.cvtColor(working, cv2.COLOR_BGR2GRAY)
        ys, xs = np.nonzero(gray < 90)
        assert len(xs) > 500
        centroid = working_px_to_plane([[xs.mean(), ys.mean()]], frame)[0]
        poi = offset_from_aim(centroid, aim)
        assert poi.dx_in > 0.5
        assert poi.dy_in > 0.3


@pytest.mark.parametrize("name", CHAIN_SCENARIOS)
def test_stats_through_build_working_match_stats_from_truth(name, renders):
    img, truth = renders(name)
    assert truth["groups"], name
    for group in truth["groups"]:
        expected_offsets = _truth_offsets(truth, group)
        origins = [ShotOrigin.DETECTED] * len(expected_offsets)
        expected = group_stats(expected_offsets, origins, DISTANCE_YD)
        *_, offsets = _chain_offsets(img, truth, group)
        got = group_stats(offsets, origins, DISTANCE_YD)
        assert got.n == expected.n
        assert got.es_in == pytest.approx(expected.es_in, abs=ES_TOL_IN), name
        assert got.mean_dx_in == pytest.approx(expected.mean_dx_in, abs=MEAN_TOL_IN), name
        assert got.mean_dy_in == pytest.approx(expected.mean_dy_in, abs=MEAN_TOL_IN), name


def _isolated_hole_indices(truth: dict, group: dict) -> list[int]:
    """Group holes drawn dark and not part of a merged pair or ragged cluster."""
    excluded = {i for cluster in truth["ragged_groups"] for i in cluster}
    excluded.update(i for pair in truth["merged_pairs"] for i in pair["indices"])
    return [
        i for i in group["hole_indices"] if i not in excluded and not truth["holes_light_core"][i]
    ]


@pytest.mark.parametrize("name", CHAIN_SCENARIOS)
def test_projected_truth_holes_coincide_with_rendered_holes_in_the_working_image(name, renders):
    img, truth = renders(name)
    deviations_in = []
    for group in truth["groups"]:
        working, frame, holes, _, _ = _chain_offsets(img, truth, group)
        gray = cv2.cvtColor(working, cv2.COLOR_BGR2GRAY).astype(np.float64)
        half = int(1.1 * truth["bullet_diameter_in"] * frame.ppi / 2)
        offsets_px = np.mgrid[-half : half + 1, -half : half + 1]
        isolated = set(_isolated_hole_indices(truth, group))
        for idx, point in zip(group["hole_indices"], holes, strict=True):
            if idx not in isolated:
                continue
            x, y = plane_to_working_px([point], frame)[0]
            x0, y0 = int(round(x)), int(round(y))
            patch = gray[y0 - half : y0 + half + 1, x0 - half : x0 + half + 1]
            weight = np.clip(150.0 - patch, 0.0, None)
            assert weight.sum() > 0, (name, idx)
            cx = x0 + (weight * offsets_px[1]).sum() / weight.sum()
            cy = y0 + (weight * offsets_px[0]).sum() / weight.sum()
            deviations_in.append(np.hypot(cx - x, cy - y) / frame.ppi)
    assert len(deviations_in) >= 3, f"{name}: too few holes checked to prove registration"
    assert np.median(deviations_in) <= 0.002, (name, np.median(deviations_in))
    assert max(deviations_in) <= 0.008, (name, max(deviations_in))


def test_aim_point_maps_consistently_with_the_hole_frame(renders):
    img, truth = renders("high_right_group")
    group = truth["groups"][0]
    _, frame, holes, aim, offsets = _chain_offsets(img, truth, group)
    for hole, off in zip(holes, offsets, strict=True):
        assert off.dx_in == pytest.approx(hole.x_in - aim.x_in)
        assert off.dy_in == pytest.approx(aim.y_in - hole.y_in)
    assert frame.ppi > 0
