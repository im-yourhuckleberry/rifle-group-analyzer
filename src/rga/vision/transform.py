"""Coordinate transforms between oriented image px, the target plane (inches) and working image px.

Conventions (design.md section 3): every space is x right, y down. Plane coordinates of a shot are
working px / ppi, with the origin at the working image's top-left corner. Homographies are 3x3
float64 matrices acting on column vectors in OpenCV pixel coordinates.
"""

import math
from collections.abc import Sequence

import cv2
import numpy as np

from rga.domain.types import CircleCandidate, PlanePoint, WorkingFrame

_W_EPS = 1e-12
_RANSAC_THRESHOLD_IN = 0.05


def _as_matrix(h) -> np.ndarray:
    m = np.asarray(h, dtype=np.float64)
    if m.shape != (3, 3) or not np.all(np.isfinite(m)):
        raise ValueError("homography must be a finite 3x3 matrix")
    return m


def _as_points(pts, name: str = "points") -> np.ndarray:
    a = np.asarray(pts, dtype=np.float64)
    if a.size == 0:
        return a.reshape(0, 2)
    if a.ndim != 2 or a.shape[1] != 2 or not np.all(np.isfinite(a)):
        raise ValueError(f"{name} must be a finite (N, 2) array")
    return a


def apply_h(h: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Apply homography `h` to an (N, 2) array of points; returns (N, 2) float64.

    Raises ValueError for a malformed matrix/points or a point mapped to infinity.
    """
    m = _as_matrix(h)
    p = _as_points(pts)
    if p.shape[0] == 0:
        return p
    homog = np.hstack([p, np.ones((p.shape[0], 1))]) @ m.T
    w = homog[:, 2:3]
    if np.any(np.abs(w) < _W_EPS):
        raise ValueError("point maps to infinity under the homography")
    return homog[:, :2] / w


def similarity_from_circle(c: CircleCandidate, diameter_in: float) -> np.ndarray:
    """Isotropic px -> plane map for a reference circle: scale = diameter_in / mean(axes_px).

    `c.axes_px` are full axis lengths. The circle center maps to the plane origin (0, 0); there is
    no rotation, so image orientation is preserved. Raises ValueError for non-positive inputs.
    """
    if not diameter_in > 0:
        raise ValueError(f"diameter_in must be positive, got {diameter_in!r}")
    mean_axis = (c.axes_px[0] + c.axes_px[1]) / 2
    if not mean_axis > 0:
        raise ValueError("circle axes must be positive")
    s = diameter_in / mean_axis
    cx, cy = c.center_px
    return np.array([[s, 0.0, -s * cx], [0.0, s, -s * cy], [0.0, 0.0, 1.0]])


def homography_from_points(src_px: np.ndarray, dst_in: np.ndarray) -> np.ndarray:
    """Homography mapping `src_px` to `dst_in` (>= 4 correspondences), normalized so h[2, 2] == 1.

    Exact fit for 4 points; RANSAC (0.05 in reprojection threshold, then inlier refinement) for more.
    Raises ValueError for mismatched/too few/degenerate (e.g. collinear) point sets.
    """
    src = _as_points(src_px, "src_px")
    dst = _as_points(dst_in, "dst_in")
    if src.shape != dst.shape:
        raise ValueError("src_px and dst_in must have the same shape")
    if src.shape[0] < 4:
        raise ValueError("at least 4 point correspondences are required")
    method = cv2.RANSAC if src.shape[0] > 4 else 0
    h, _ = cv2.findHomography(src, dst, method, _RANSAC_THRESHOLD_IN)
    if h is None or not np.all(np.isfinite(h)) or abs(h[2, 2]) < _W_EPS:
        raise ValueError("degenerate point configuration: no homography found")
    h = h / h[2, 2]
    if abs(np.linalg.det(h)) < 1e-18:
        raise ValueError("degenerate point configuration: singular homography")
    return h


def _native_ppi(h: np.ndarray, width: int, height: int) -> float:
    """Image pixels per plane inch at the image center (inverse sqrt of the local Jacobian det)."""
    x, y = width / 2, height / 2
    w = h[2, 0] * x + h[2, 1] * y + h[2, 2]
    qx = (h[0, 0] * x + h[0, 1] * y + h[0, 2]) / w
    qy = (h[1, 0] * x + h[1, 1] * y + h[1, 2]) / w
    jac = np.array(
        [
            [h[0, 0] - h[2, 0] * qx, h[0, 1] - h[2, 1] * qx],
            [h[1, 0] - h[2, 0] * qy, h[1, 1] - h[2, 1] * qy],
        ]
    )
    area_scale = abs(np.linalg.det(jac)) / (w * w)
    if not area_scale > 0:
        raise ValueError("homography is singular at the image center")
    return 1.0 / math.sqrt(area_scale)


def build_working(
    img: np.ndarray,
    h_px_to_plane: np.ndarray,
    ppi_cap: float = 200,
    max_side: int = 4000,
) -> tuple[np.ndarray, WorkingFrame, np.ndarray]:
    """Rectify `img` into the working image; returns (working_img, frame, h_px_to_working).

    ppi = min(native ppi at the image center, ppi_cap, whatever keeps the long side <= max_side).
    The working image spans the bounding box of the warped source image, so working px / ppi gives
    plane coordinates with the origin at its top-left. Raises ValueError for an empty image, a
    malformed/singular homography, or one that sends part of the image through infinity.
    """
    if img.ndim not in (2, 3) or img.size == 0:
        raise ValueError("image must be a non-empty 2-D or 3-D array")
    if not (ppi_cap > 0 and max_side > 0):
        raise ValueError("ppi_cap and max_side must be positive")
    h = _as_matrix(h_px_to_plane)
    height, width = img.shape[:2]

    corners = np.array([[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float64)
    center_w = h[2, 0] * width / 2 + h[2, 1] * height / 2 + h[2, 2]
    corner_w = corners @ h[2, :2] + h[2, 2]
    if abs(center_w) < _W_EPS or np.any(corner_w * center_w <= 0):
        raise ValueError("homography maps part of the image to infinity or behind the camera")
    plane_corners = apply_h(h, corners)
    min_xy = plane_corners.min(axis=0)
    span_in = plane_corners.max(axis=0) - min_xy
    if not np.all(span_in > 0):
        raise ValueError("homography collapses the image to a line")

    ppi = min(_native_ppi(h, width, height), float(ppi_cap), max_side / float(span_in.max()))
    out_w = min(max_side, max(1, math.ceil(span_in[0] * ppi - 1e-9)))
    out_h = min(max_side, max(1, math.ceil(span_in[1] * ppi - 1e-9)))

    to_working = np.array(
        [[ppi, 0.0, -ppi * min_xy[0]], [0.0, ppi, -ppi * min_xy[1]], [0.0, 0.0, 1.0]]
    )
    h_px_to_working = to_working @ h

    native_to_working_scale = ppi / _native_ppi(h, width, height)
    src, warp_h = img, h_px_to_working
    if native_to_working_scale < 0.5:
        r = 2 * native_to_working_scale
        small_w, small_h = max(1, round(width * r)), max(1, round(height * r))
        src = cv2.resize(img, (small_w, small_h), interpolation=cv2.INTER_AREA)
        kx, ky = width / small_w, height / small_h
        # cv2.resize maps dst px d to src px (d + 0.5) * k - 0.5; compose its inverse into the warp.
        small_to_big = np.array(
            [[kx, 0.0, 0.5 * kx - 0.5], [0.0, ky, 0.5 * ky - 0.5], [0.0, 0.0, 1.0]]
        )
        warp_h = h_px_to_working @ small_to_big

    working = cv2.warpPerspective(
        src, warp_h, (out_w, out_h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
    )
    return working, WorkingFrame(ppi=ppi, width_px=out_w, height_px=out_h), h_px_to_working


def working_px_to_plane(pts, frame: WorkingFrame) -> list[PlanePoint]:
    """Convert (N, 2) working px to plane points: inches = px / ppi (y stays down)."""
    p = _as_points(pts)
    return [PlanePoint(x_in=float(x) / frame.ppi, y_in=float(y) / frame.ppi) for x, y in p]


def plane_to_working_px(pts: Sequence[PlanePoint], frame: WorkingFrame) -> np.ndarray:
    """Convert plane points to an (N, 2) float64 array of working px: px = inches * ppi."""
    if len(pts) == 0:
        return np.empty((0, 2), dtype=np.float64)
    return np.array([[p.x_in * frame.ppi, p.y_in * frame.ppi] for p in pts], dtype=np.float64)
