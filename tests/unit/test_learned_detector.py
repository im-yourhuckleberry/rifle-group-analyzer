"""seg-v1 as the app's hole detector, with cv-v1 as the fallback (rga.vision.learned)."""

import logging
import shutil

import cv2
import numpy as np
import pytest

from rga.domain.types import WorkingFrame
from rga.vision import detect as cv_v1
from rga.vision import learned, pipeline
from rga.vision.learned import seg

PPI = 100.0
BULLET_IN = 0.3  # 30 working px


@pytest.fixture(autouse=True)
def fresh_selector(monkeypatch):
    monkeypatch.setattr(learned, "_seg", None)
    monkeypatch.setattr(learned, "_seg_failed", False)


def _target(centres):
    img = np.full((400, 500, 3), 200, np.uint8)
    rng = np.random.default_rng(0)
    img = np.clip(img + rng.normal(0, 4, img.shape), 0, 255).astype(np.uint8)
    for x, y in centres:
        cv2.circle(img, (x, y), 15, (40, 40, 40), -1, cv2.LINE_AA)
    return img, WorkingFrame(ppi=PPI, width_px=500, height_px=400)


CENTRES = [(100, 100), (300, 120), (200, 280), (400, 300)]


def _found(blobs):
    return [c for b in blobs for c in b.centers_px]


def test_pipeline_uses_the_selector():
    assert pipeline.detect_holes is learned.detect_holes


def test_default_is_seg_v1_and_finds_holes():
    img, frame = _target(CENTRES)
    blobs = learned.detect_holes(img, frame, BULLET_IN)
    assert learned.active_detector() == "seg-v1"
    assert learned._seg.version == f"{seg.SEMVER}+8c0e6b3bb59c"
    found = _found(blobs)
    for x, y in CENTRES:
        assert min(np.hypot(fx - x, fy - y) for fx, fy in found) < 0.25 * BULLET_IN * PPI
    assert len(found) == len(CENTRES)


def test_roi_limits_results():
    img, frame = _target(CENTRES)
    found = _found(learned.detect_holes(img, frame, BULLET_IN, (0, 0, 250, 200)))
    assert len(found) == 1 and np.hypot(found[0][0] - 100, found[0][1] - 100) < 8


def test_region_detect_matches_the_whole_photo_detect():
    img, frame = _target(CENTRES)
    whole = learned.detect_holes(img, frame, BULLET_IN)
    region = learned.detect_holes(img, frame, BULLET_IN, (250, 50, 500, 400))
    assert region == [b for b in whole if b.centers_px[0][0] >= 250 and b.centers_px[0][1] >= 50]
    assert learned.detect_holes(img, frame, BULLET_IN, (600, 600, 700, 700)) == []


@pytest.mark.parametrize("damage", ["missing", "corrupt", "bad_operating"])
def test_broken_model_falls_back_to_cv_v1(tmp_path, monkeypatch, caplog, damage):
    for name in ("seg-v1.onnx", "seg-v1.json", "SHA256SUMS"):
        shutil.copy(seg.MODEL_DIR / name, tmp_path / name)
    model = tmp_path / "seg-v1.onnx"
    if damage == "missing":
        model.unlink()
    elif damage == "corrupt":
        model.write_bytes(model.read_bytes()[:-10])
    else:
        (tmp_path / "seg-v1.json").write_text('{"operating": {"tau_c": 5}}')
    monkeypatch.setattr(seg, "DEFAULT_MODEL", model)
    img, frame = _target(CENTRES)
    with caplog.at_level(logging.WARNING, logger="rga.vision.learned"):
        got = learned.detect_holes(img, frame, BULLET_IN)
    assert "using cv-v1" in caplog.text and learned.active_detector() == "cv-v1"
    assert [b.centers_px for b in got] == [
        b.centers_px for b in cv_v1.detect_holes(img, frame, BULLET_IN)
    ]


@pytest.mark.parametrize(
    "working, diameter",
    [
        (np.zeros((0, 0, 3), np.uint8), BULLET_IN),
        (np.zeros((50, 50, 3), np.uint8), 0.0),
        (np.zeros((50, 50, 3), np.uint8), float("nan")),
    ],
)
def test_bad_input_raises_value_error(working, diameter):
    with pytest.raises(ValueError):
        learned.detect_holes(working, WorkingFrame(ppi=PPI, width_px=50, height_px=50), diameter)


def test_model_files_ship_with_the_package():
    sums = (seg.MODEL_DIR / "SHA256SUMS").read_text().split()
    assert sums == [
        "8c0e6b3bb59cfbe1e0ce41bfa3ea2bd7c874be3eda575bb36f6ffe98f1a6e697",
        "seg-v1.onnx",
    ]
    assert (seg.MODEL_DIR / "seg-v1.json").is_file()
    assert seg.SegHoleDetector.requires_network is False
