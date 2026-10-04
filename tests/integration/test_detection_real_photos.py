"""Per-photo regression for hole detection on the supplied real photos (data/real_photos).

The photos are gitignored, so the per-photo tests skip with a declared message when they are
absent. `tests/real/counts.json` holds hand-read visual hole counts and approximate hole centres
(not exact ground truth) plus the baselines the detector must not fall below.
"""

import json

import numpy as np
import pytest

from rga.domain.types import Blob, BlobKind
from tools.eval_detection import (
    REAL_COUNTS_PATH,
    REAL_PHOTO_DIR,
    detect_real_photo,
    load_real_counts,
    real_photo_scores,
    score_real_photo,
)

COUNTS = load_real_counts()
PHOTOS = sorted(COUNTS["photos"])
PHOTOS_PRESENT = REAL_PHOTO_DIR.is_dir() and all(
    any(REAL_PHOTO_DIR.glob(f"{name}.*")) for name in PHOTOS
)
needs_photos = pytest.mark.skipif(
    not PHOTOS_PRESENT,
    reason="data/real_photos/real_01..05 are not present (gitignored); real-photo regression skipped",
)


@pytest.fixture(scope="module")
def scores():
    return {s.name: s for s in real_photo_scores(counts=COUNTS)}


# ---------------------------------------------------------------- the counts file


def test_counts_file_describes_five_photos_with_visual_hole_lists():
    assert PHOTOS == ["real_01", "real_02", "real_03", "real_04", "real_05"]
    for name, entry in COUNTS["photos"].items():
        assert entry["visual_count"] == len(entry["holes"]) > 0, name
        assert entry["px_per_in"] > 0 and entry["bullet_d_in"] > 0, name
        assert entry["count_note"], name
        for hole in entry["holes"]:
            assert hole["x"] >= 0 and hole["y"] >= 0
            assert isinstance(hole.get("uncertain", False), bool)
        for cluster in entry.get("clusters", []):
            assert all(0 <= i < len(entry["holes"]) for i in cluster["members"])
            assert len(cluster["members"]) >= 2
        for decoy in entry.get("decoys", []):
            assert decoy["note"]


def test_counts_file_flags_the_uncertain_entries_for_review():
    flagged = {
        n: sum(h.get("uncertain", False) for h in e["holes"]) for n, e in COUNTS["photos"].items()
    }
    assert flagged["real_01"] >= 1 and flagged["real_04"] >= 1
    assert "baseline" in COUNTS["meta"] and {"before", "after"} <= set(COUNTS["meta"]["baseline"])


def test_baselines_cover_every_photo_and_show_an_improvement():
    base = COUNTS["meta"]["baseline"]
    for name in PHOTOS:
        for phase in ("before", "after"):
            assert {"found", "false_positives"} <= set(base[phase][name])
    total = {ph: sum(base[ph][n]["found"] for n in PHOTOS) for ph in ("before", "after")}
    fps = {ph: sum(base[ph][n]["false_positives"] for n in PHOTOS) for ph in ("before", "after")}
    assert total["after"] > total["before"]
    assert fps["after"] < fps["before"]


def test_counts_file_is_valid_json_text():
    json.loads(REAL_COUNTS_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- the detector on real photos


@needs_photos
@pytest.mark.parametrize("name", PHOTOS)
def test_found_holes_do_not_regress_below_baseline(scores, name):
    floor = COUNTS["meta"]["baseline"]["after"][name]["found"]
    assert scores[name].found >= floor, f"{name}: found {scores[name].found} < baseline {floor}"


@needs_photos
@pytest.mark.parametrize("name", PHOTOS)
def test_false_positives_do_not_regress_above_baseline(scores, name):
    ceiling = COUNTS["meta"]["baseline"]["after"][name]["false_positives"]
    got = scores[name].false_positives
    assert got <= ceiling, f"{name}: {got} false positives > baseline {ceiling}"


@needs_photos
def test_detection_beats_the_pre_tuning_baseline_overall(scores):
    before = COUNTS["meta"]["baseline"]["before"]
    assert sum(s.found for s in scores.values()) > sum(before[n]["found"] for n in PHOTOS)
    assert sum(s.false_positives for s in scores.values()) < sum(
        before[n]["false_positives"] for n in PHOTOS
    )


@needs_photos
@pytest.mark.parametrize("name", PHOTOS)
def test_each_photo_detects_within_three_seconds(scores, name):
    assert scores[name].runtime_s <= 3.0


@needs_photos
def test_detection_on_a_photo_is_deterministic():
    entry = COUNTS["photos"]["real_02"]
    path = next(iter(sorted(REAL_PHOTO_DIR.glob("real_02.*"))))
    first = detect_real_photo(path, entry)[0]
    second = detect_real_photo(path, entry)[0]
    assert np.array_equal(first, second)


# ---------------------------------------------------------------- verifying the scorer (no photos needed)


def _blob(x, y, kind=BlobKind.SINGLE):
    ring = ((int(x), int(y)), (int(x) + 4, int(y)), (int(x) + 4, int(y) + 4))
    return Blob(kind, ring, 100.0, ((float(x), float(y)),), 0.9)


ENTRY = {
    "px_per_in": 100.0,
    "bullet_d_in": 0.2,
    "holes": [
        {"x": 100, "y": 100},
        {"x": 300, "y": 100},
        {"x": 500, "y": 100},
        {"x": 520, "y": 118},
    ],
    "clusters": [{"members": [2, 3], "kind": "touching"}],
    "sheet_polygon": [[0, 0], [800, 0], [800, 400], [0, 400]],
    "decoys": [{"x": 600, "y": 300, "note": "text"}],
}


def _score(centers):
    blobs = [_blob(*c) for c in centers]
    return score_real_photo("synthetic", ENTRY, np.array(centers, float).reshape(-1, 2), blobs)


def test_scorer_perfect_detections_find_everything_without_false_positives():
    result = _score([(100, 100), (300, 100), (500, 100), (520, 118)])
    assert (result.found, result.false_positives, result.missed) == (4, 0, [])
    assert result.clusters_touched == result.clusters_total == 1


def test_scorer_gate_is_0_6_bullet_diameters():
    d = 0.2 * 100.0
    inside = _score([(100 + 0.55 * d, 100)])
    outside = _score([(100 + 0.7 * d, 100)])
    assert inside.found == 1 and inside.false_positives == 0
    assert outside.found == 0 and outside.false_positives == 1


def test_scorer_classifies_extra_detections():
    result = _score([(100, 100), (210, 250), (600, 305), (900, 100), (510, 109)])
    assert result.found == 1
    assert result.other_false == 1  # (210, 250)
    assert result.known_decoy_hits == 1  # near the listed decoy
    assert result.outside_sheet == 1  # beyond the sheet polygon
    assert result.absorbed_in_clusters == 1  # extra centre inside the touching-cluster zone
    assert result.false_positives == 2


def test_scorer_reports_missed_hole_indices_and_recall():
    result = _score([(100, 100)])
    assert result.missed == [1, 2, 3] and result.recall == 0.25


def test_scorer_does_not_match_one_detection_to_two_holes():
    result = _score([(507, 107)])
    assert result.found == 1
    assert result.missed == [0, 1, 3]
