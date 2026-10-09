"""Shared helpers for the learned hole detectors: input checks, ROI handling, scale normalisation
and `Blob` construction.

Every model sees the image rescaled so that one bullet diameter is `D_PX` pixels ("normalised
px"). Outputs are mapped back to working px with `to_working`. Only `numpy`, `cv2` and
`rga.domain.types` are imported, so the adapters ship without new dependencies.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import cv2
import numpy as np

from rga.domain.types import Blob, BlobKind, WorkingFrame

D_PX = 16.0
MARGIN_D = 2.0
MIN_CONFIDENCE = 0.01
MIN_D_PX = 1.0  # a bullet narrower than one working pixel cannot be detected
MAX_D_PX = 1000.0
MAX_SCALED_PIXELS = 16_000_000  # keeps the rescaled ROI inside the time and memory budget
_BG_DOWNSCALE = 8


@dataclass(frozen=True)
class Prepared:
    """A crop of the working image rescaled to `D_PX` px per bullet diameter.

    `image` is BGR uint8; normalised px `p` maps to working px `p / scale + (x0, y0)`.
    """

    image: np.ndarray
    scale: float
    x0: int
    y0: int
    d_px: float


def validate(working: object, frame: WorkingFrame, bullet_diameter_in: float) -> np.ndarray:
    """Check the detector inputs and return the image as 3-channel BGR uint8.

    Raises ValueError (and nothing else) for a non-image array, a non-finite or non-positive
    bullet diameter, or a non-finite or non-positive ppi.
    """
    if not isinstance(working, np.ndarray):
        raise ValueError("working must be a numpy array")
    if working.ndim not in (2, 3) or working.size == 0:
        raise ValueError("working must be a non-empty 2-D or 3-D image")
    if working.dtype != np.uint8:
        raise ValueError("working must be uint8")
    if working.ndim == 3 and working.shape[2] not in (1, 3):
        raise ValueError("working must have 1 or 3 channels")
    try:
        d = float(bullet_diameter_in)
        ppi = float(frame.ppi)
    except (TypeError, AttributeError, OverflowError) as exc:
        raise ValueError("bullet_diameter_in and frame.ppi must be numbers") from exc
    if not (math.isfinite(d) and d > 0 and math.isfinite(ppi) and ppi > 0):
        raise ValueError("bullet_diameter_in and frame.ppi must be positive")
    if not MIN_D_PX <= d * ppi <= MAX_D_PX:
        raise ValueError(
            f"bullet diameter of {d * ppi:.3g} px is outside [{MIN_D_PX}, {MAX_D_PX}] px"
        )
    if working.ndim == 2 or working.shape[2] == 1:
        return cv2.cvtColor(working.reshape(working.shape[:2]), cv2.COLOR_GRAY2BGR)
    return working


def clamp_roi(
    roi_px: tuple[int, int, int, int] | None, width: int, height: int
) -> tuple[int, int, int, int]:
    """Clamp (x0, y0, x1, y1) to the image; None means the whole image. Empty if x1 <= x0 or y1 <= y0."""
    if roi_px is None:
        return 0, 0, width, height
    try:
        x0, y0, x1, y1 = (int(v) for v in roi_px)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("roi_px must be four finite numbers (x0, y0, x1, y1)") from exc
    return max(0, x0), max(0, y0), min(width, x1), min(height, y1)


def prepare(
    img_bgr: np.ndarray, ppi: float, bullet_diameter_in: float, roi: tuple[int, int, int, int]
) -> Prepared:
    """Crop `roi` plus a 2-diameter margin (clamped) and rescale to `D_PX` px per diameter."""
    d = bullet_diameter_in * ppi
    margin = int(math.ceil(MARGIN_D * d))
    h, w = img_bgr.shape[:2]
    x0, y0 = max(0, roi[0] - margin), max(0, roi[1] - margin)
    x1, y1 = min(w, roi[2] + margin), min(h, roi[3] + margin)
    crop = img_bgr[y0:y1, x0:x1]
    scale = D_PX / d
    size = (max(1, round(crop.shape[1] * scale)), max(1, round(crop.shape[0] * scale)))
    if size[0] * size[1] > MAX_SCALED_PIXELS:
        raise ValueError("ROI too large for this bullet size; pass a smaller roi_px")
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    return Prepared(cv2.resize(crop, size, interpolation=interp), scale, x0, y0, D_PX)


def normalise(image_u8: np.ndarray) -> np.ndarray:
    """BGR uint8 -> float32 CHW divided by the local paper level and clipped to [0, 1.5].

    The paper level is a smooth upper envelope of each channel: a 1.5-diameter dilation removes
    holes and thin print, then a 3-diameter Gaussian smooths it (computed at 1/8 resolution).
    Dividing by it flattens shadows and lighting gradients.
    """
    h, w = image_u8.shape[:2]
    f = _BG_DOWNSCALE if min(h, w) >= 4 * _BG_DOWNSCALE else 1
    small = cv2.resize(image_u8, (max(1, w // f), max(1, h // f)), interpolation=cv2.INTER_AREA)
    k = max(1, int(round(1.5 * D_PX / f))) | 1
    small = cv2.dilate(small, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    sigma = 3.0 * D_PX / f
    small = cv2.GaussianBlur(small.astype(np.float32), (0, 0), sigma, borderType=cv2.BORDER_REFLECT)
    paper = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR).reshape(h, w, -1)
    out = image_u8.astype(np.float32) / np.maximum(paper, 8.0)
    return np.ascontiguousarray(np.clip(out, 0.0, 1.5).transpose(2, 0, 1))


def to_working(pts_s: np.ndarray, prep: Prepared) -> np.ndarray:
    """Normalised px (N x 2) -> working px."""
    return np.asarray(pts_s, dtype=np.float64) / prep.scale + (prep.x0, prep.y0)


def circle_contour(cx: float, cy: float, r: float, n: int = 24) -> np.ndarray:
    """An n-point circle polygon (n x 2, float64)."""
    t = np.arange(n) * (2 * math.pi / n)
    return np.stack([cx + r * np.cos(t), cy + r * np.sin(t)], axis=1)


def make_blob(
    kind: BlobKind,
    contour_s: np.ndarray,
    centers_s: Sequence[tuple[float, float]],
    confidence: float,
    prep: Prepared,
) -> Blob:
    """Build a working-px `Blob` from a normalised-px contour and centres.

    A contour that collapses to fewer than 3 distinct integer points becomes a one-diameter circle
    around the first centre. `area_px` is the area of the integer contour actually returned.
    """
    centers = to_working(np.asarray(centers_s, dtype=np.float64).reshape(-1, 2), prep)
    contour = np.rint(to_working(np.asarray(contour_s, dtype=np.float64).reshape(-1, 2), prep))
    pts = _dedupe(contour.astype(np.int64))
    if len(pts) < 3 or abs(cv2.contourArea(pts.astype(np.int32).reshape(-1, 1, 2))) == 0:
        r = max(prep.d_px / prep.scale / 2.0, 1.5)
        pts = _dedupe(np.rint(circle_contour(*centers[0], r)).astype(np.int64))
    area = abs(float(cv2.contourArea(pts.astype(np.int32).reshape(-1, 1, 2))))
    conf = float(min(1.0, max(MIN_CONFIDENCE, confidence)))
    return Blob(
        kind=kind,
        contour_px=tuple((int(x), int(y)) for x, y in pts),
        area_px=area,
        centers_px=tuple((float(x), float(y)) for x, y in centers),
        confidence=conf,
    )


def finalize(blobs: Iterable[Blob], roi: tuple[int, int, int, int]) -> list[Blob]:
    """Keep blobs whose first centre lies in [x0, x1) x [y0, y1); sort by (y, x) of that centre."""
    x0, y0, x1, y1 = roi
    kept = [b for b in blobs if x0 <= b.centers_px[0][0] < x1 and y0 <= b.centers_px[0][1] < y1]
    return sorted(kept, key=lambda b: (b.centers_px[0][1], b.centers_px[0][0]))


def _dedupe(pts: np.ndarray) -> np.ndarray:
    """Drop consecutive duplicate points (and a closing duplicate)."""
    if len(pts) == 0:
        return pts
    keep = [pts[0]]
    for p in pts[1:]:
        if (p != keep[-1]).any():
            keep.append(p)
    if len(keep) > 1 and (keep[-1] == keep[0]).all():
        keep.pop()
    return np.array(keep)
