"""U1: on the private GunFun photo `data/real_photos/real_02.jpg` the paster's outer disc is the reference.

The photo is gitignored and never committed; the test skips when it is absent.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from rga.domain.types import ReferenceKind
from rga.vision.pipeline import propose_candidates
from rga.vision.reference_circle import find_circle_candidates
from rga.vision.reference_grid import GridCandidate, find_grid

PHOTO = Path(__file__).resolve().parents[2] / "data" / "real_photos" / "real_02.jpg"
HAND_MEASURED_GRID_PX_PER_INCH = 470.0
PASTER_DIAMETER_IN = 1.5
INNER_RING_DIAMETER_PX = 344.0

pytestmark = pytest.mark.skipif(
    not PHOTO.exists(), reason="private photo real_02.jpg is not present"
)


@pytest.fixture(scope="module")
def photo() -> np.ndarray:
    img = cv2.imread(str(PHOTO))
    assert img is not None
    return img


def _hand_built_grid() -> GridCandidate:
    scale = 1.0 / HAND_MEASURED_GRID_PX_PER_INCH
    h = np.diag([scale, scale, 1.0])
    return GridCandidate(np.zeros((0, 2)), np.zeros((0, 2), dtype=int), h, 0.02, 1.0)


def _mean_diameter(c) -> float:
    return (c.axes_px[0] + c.axes_px[1]) / 2


@pytest.mark.parametrize("kind", [ReferenceKind.CIRCLE_1_5IN, ReferenceKind.CIRCLE_3IN])
def test_the_outer_orange_disc_ranks_first_with_the_inner_ring_second(photo, kind) -> None:
    found = find_circle_candidates(photo, kind)
    expected = PASTER_DIAMETER_IN * HAND_MEASURED_GRID_PX_PER_INCH
    assert abs(_mean_diameter(found[0]) - expected) <= 0.05 * expected
    assert abs(_mean_diameter(found[1]) - INNER_RING_DIAMETER_PX) <= 0.05 * INNER_RING_DIAMETER_PX
    assert abs(found[0].center_px[0] - found[1].center_px[0]) < 10
    assert abs(found[0].center_px[1] - found[1].center_px[1]) < 10


def test_with_a_grid_the_outer_disc_measures_one_and_a_half_inches(photo) -> None:
    grid = _hand_built_grid()
    found = find_circle_candidates(photo, ReferenceKind.CIRCLE_1_5IN, grid=grid)
    inches = _mean_diameter(found[0]) / HAND_MEASURED_GRID_PX_PER_INCH
    assert abs(inches - PASTER_DIAMETER_IN) <= 0.05 * PASTER_DIAMETER_IN


def test_a_grid_that_makes_the_inner_ring_the_better_match_prefers_it(photo) -> None:
    scale = 1.5 / INNER_RING_DIAMETER_PX
    h = np.diag([scale, scale, 1.0])
    grid = GridCandidate(np.zeros((0, 2)), np.zeros((0, 2), dtype=int), h, 0.02, 1.0)
    found = find_circle_candidates(photo, ReferenceKind.CIRCLE_1_5IN, grid=grid)
    assert abs(_mean_diameter(found[0]) - INNER_RING_DIAMETER_PX) <= 0.05 * INNER_RING_DIAMETER_PX


def test_the_one_inch_kind_is_not_given_the_outer_disc(photo) -> None:
    found = find_circle_candidates(photo, ReferenceKind.CIRCLE_1IN)
    assert all(_mean_diameter(c) < 0.4 * min(photo.shape[:2]) for c in found)


def test_end_to_end_the_detected_grid_and_the_circle_search_rank_the_outer_disc_first(
    photo,
) -> None:
    grid = find_grid(photo)
    assert grid is not None
    found = find_circle_candidates(photo, ReferenceKind.CIRCLE_1_5IN, grid=grid)
    expected = PASTER_DIAMETER_IN * HAND_MEASURED_GRID_PX_PER_INCH
    assert abs(_mean_diameter(found[0]) - expected) <= 0.05 * expected


def test_the_pipeline_proposes_the_outer_disc_first_for_the_paster_kinds(photo) -> None:
    expected = PASTER_DIAMETER_IN * HAND_MEASURED_GRID_PX_PER_INCH
    for kind in (ReferenceKind.CIRCLE_1_5IN, ReferenceKind.CIRCLE_3IN):
        proposals = propose_candidates(photo, kind)
        assert abs(_mean_diameter(proposals[0].circle) - expected) <= 0.05 * expected
