"""Hole-detector selection: seg-v1 (learned, offline) by default, cv-v1 as the fallback.

`detect_holes` has the same signature and contract as `rga.vision.detect.detect_holes`. seg-v1 is
built once on first use; if its model file is missing, fails its SHA-256 check or has invalid
operating values, a warning is logged and every call uses cv-v1 instead. seg-v1 always runs on the
whole working image and the ROI only filters by centre (as cv-v1 does), so a region detect returns
exactly the holes a whole-photo detect found there. Nothing here opens a socket.
"""

from __future__ import annotations

import logging
import threading

import cv2
import numpy as np

from rga.domain.types import Blob, WorkingFrame
from rga.vision import detect as cv_v1
from rga.vision.learned import _common

_LOG = logging.getLogger(__name__)

_lock = threading.Lock()
_seg: object | None = None
_seg_failed = False


def active_detector() -> str:
    """Name of the detector `detect_holes` uses now ("seg-v1" or "cv-v1")."""
    return "seg-v1" if _seg_detector() is not None else "cv-v1"


def detect_holes(
    working: np.ndarray,
    frame: WorkingFrame,
    bullet_diameter_in: float,
    roi_px: tuple[int, int, int, int] | None = None,
    params: cv_v1.DetectParams | None = None,
) -> list[Blob]:
    """Detect bullet holes with the active detector; `params` only applies to cv-v1."""
    seg = _seg_detector()
    if seg is None:
        return cv_v1.detect_holes(working, frame, bullet_diameter_in, roi_px, params)
    blobs = seg.detect(working, frame, bullet_diameter_in, None)
    if roi_px is None:
        return blobs
    roi = _common.clamp_roi(roi_px, working.shape[1], working.shape[0])
    if roi[2] <= roi[0] or roi[3] <= roi[1]:
        return []
    return _common.finalize(blobs, roi)


def _seg_detector():
    global _seg, _seg_failed
    with _lock:
        if _seg is None and not _seg_failed:
            from rga.vision.learned.seg import SegHoleDetector

            try:
                _seg = SegHoleDetector()
                _LOG.info("hole detector: %s %s", _seg.name, _seg.version)
            except (RuntimeError, OSError, ValueError, cv2.error) as exc:
                _seg_failed = True
                _LOG.warning("seg-v1 unavailable, using cv-v1: %s", exc)
        return _seg
