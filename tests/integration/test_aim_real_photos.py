"""UF2 on the supplied real photos (data/real_photos, gitignored): the aim marks are found first.

The photos are private and never copied; the tests skip with a declared message when they are
absent. Each photo is rescaled to 200 ppi from its hand-measured grid pitch (as the detection
regression does) and the group region is the box of the hand-listed holes of that group. The expected
aim centres were read off the photos by eye (about 0.03 in), so the tolerances are wider than the
synthetic ones.
"""

from __future__ import annotations

import math
import time

import cv2
import numpy as np
import pytest

from rga.vision.aim import propose_aims
from tools.eval_detection import REAL_PHOTO_DIR, WORKING_PPI, load_real_counts

COUNTS = load_real_counts()["photos"]
PHOTOS_PRESENT = REAL_PHOTO_DIR.is_dir() and all(
    any(REAL_PHOTO_DIR.glob(f"{name}.*")) for name in ("real_02", "real_03", "real_04", "real_05")
)
needs_photos = pytest.mark.skipif(
    not PHOTOS_PRESENT,
    reason="data/real_photos/real_02..05 are not present (gitignored); real-photo aim check skipped",
)
ROI_MARGIN_IN = 0.2
MAX_SECONDS = 8.0

# photo -> (group label, hole indices into counts.json, aim centre in file px)
GROUPS = {
    "real_02": ("sheet", list(range(9)), (727.5, 1104.0)),
    "real_03": ("sheet", list(range(22)), (670.0, 1076.0)),
    "real_04": ("lower sheet", list(range(14)), (758.5, 1129.0)),
    "real_05 diamond": ("diamond group", list(range(4, 15)), (941.5, 715.0)),
    "real_05 tape": ("tape cross group", list(range(15, 20)), (1321.0, 1240.0)),
}


def _photo_name(key: str) -> str:
    return key.split()[0]


@pytest.fixture(scope="module")
def results():
    out = {}
    for key, (_, holes, aim_file_px) in GROUPS.items():
        name = _photo_name(key)
        path = next(iter(sorted(REAL_PHOTO_DIR.glob(f"{name}.*"))), None)
        if path is None:
            continue
        entry = COUNTS[name]
        scale = WORKING_PPI / float(entry["px_per_in"])
        image = cv2.imread(str(path))
        working = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        pts = (
            np.array([[entry["holes"][i]["x"], entry["holes"][i]["y"]] for i in holes], float)
            * scale
        )
        margin = ROI_MARGIN_IN * WORKING_PPI
        roi = (
            int(max(0, pts[:, 0].min() - margin)),
            int(max(0, pts[:, 1].min() - margin)),
            int(min(working.shape[1], pts[:, 0].max() + margin)),
            int(min(working.shape[0], pts[:, 1].max() + margin)),
        )
        started = time.perf_counter()
        found = propose_aims(
            working,
            roi,
            WORKING_PPI,
            bullet_diameter_in=float(entry["bullet_d_in"]),
            shots_px=[tuple(p) for p in pts],
        )
        elapsed = time.perf_counter() - started
        truth = (aim_file_px[0] * scale, aim_file_px[1] * scale)
        errors = [math.dist(c.point_px, truth) / WORKING_PPI for c in found]
        holes_px = [tuple(p) for p in pts]
        out[key] = (found, errors, elapsed, holes_px, float(entry["bullet_d_in"]))
    return out


@needs_photos
def test_real_02_paster_and_diamond_centre_is_the_first_candidate(results):
    found, errors, *_ = results["real_02"]
    assert found[0].kind in {"diamond_center", "paster_center"}
    assert errors[0] < 0.05


@needs_photos
@pytest.mark.parametrize("name", ["real_03", "real_04"])
def test_the_diamond_is_the_first_candidate(results, name):
    found, errors, *_ = results[name]
    assert found[0].kind == "diamond_center"
    assert errors[0] < 0.06


@needs_photos
def test_real_05_diamond_group_finds_its_diamond_first(results):
    found, errors, *_ = results["real_05 diamond"]
    assert found[0].kind == "diamond_center"
    assert errors[0] < 0.05


@needs_photos
def test_real_05_tape_cross_is_among_the_top_three_for_its_group(results):
    found, errors, *_ = results["real_05 tape"]
    assert any(error < 0.12 for error in errors[:3])


@needs_photos
@pytest.mark.parametrize("key", list(GROUPS))
def test_candidates_are_ranked_and_each_photo_is_quick(results, key):
    found, _, elapsed, *_ = results[key]
    assert 1 <= len(found) <= 5
    assert [c.score for c in found] == sorted((c.score for c in found), reverse=True)
    assert elapsed <= MAX_SECONDS


@needs_photos
@pytest.mark.parametrize("key", list(GROUPS))
def test_no_candidate_sits_on_a_listed_hole(results, key):
    found, _, _, holes_px, bullet = results[key]
    reach = bullet * WORKING_PPI
    assert all(math.dist(c.point_px, hole) >= reach for c in found for hole in holes_px)


@needs_photos
def test_real_04_has_only_its_diamond_once_holes_are_excluded(results):
    found, errors, *_ = results["real_04"]
    assert [c.kind for c in found] == ["diamond_center"] and errors[0] < 0.06


@needs_photos
def test_the_answer_is_deterministic():
    entry = COUNTS["real_04"]
    path = next(iter(sorted(REAL_PHOTO_DIR.glob("real_04.*"))))
    scale = WORKING_PPI / float(entry["px_per_in"])
    working = cv2.resize(
        cv2.imread(str(path)), None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
    )
    roi = (400, 1400, 1300, 2000)
    assert propose_aims(working, roi, WORKING_PPI) == propose_aims(working, roi, WORKING_PPI)
