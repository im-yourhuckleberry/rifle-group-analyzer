"""Circle scale reference: automatic candidates, manual circle/ellipse fit, and aim snap.

Everything is in oriented image pixels (x right, y down). `find_circle_candidates` proposes
ranked circular marks (never a confirmed reference); `circle_from_edge_points` fits the user's
manual clicks; `reference_from_circle` turns a chosen candidate into a `ReferenceResult`;
`refine_circle_near` snaps a click to the center of a nearby printed or taped aim mark.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import cv2
import numpy as np

from rga.domain.types import (
    REFERENCE_SIZE_IN,
    CircleCandidate,
    ReferenceKind,
    ReferenceResult,
)
from rga.vision.io import MAX_ASPECT_RATIO
from rga.vision.reference_grid import GridCandidate
from rga.vision.transform import similarity_from_circle

CIRCLE_KINDS: tuple[ReferenceKind, ...] = (
    ReferenceKind.CIRCLE_1IN,
    ReferenceKind.CIRCLE_1_5IN,
    ReferenceKind.CIRCLE_3IN,
)
TILT_AXIS_RATIO = 0.98

# Allowed reference diameter as a fraction of the image's short side, per kind.
_SIZE_FRACTION: dict[ReferenceKind, tuple[float, float]] = {
    ReferenceKind.CIRCLE_1IN: (0.045, 0.40),
    ReferenceKind.CIRCLE_1_5IN: (0.060, 0.50),
    ReferenceKind.CIRCLE_3IN: (0.100, 0.95),
}
_DETECT_MAX_SIDE = 1280
_MIN_DETECT_DIAMETER_PX = 28
_MIN_IMAGE_SIDE = 64
_RAY_COUNT = 144
_RAY_STEP_PX = 0.25
_MIN_EDGE_CONTRAST = 10.0
_MAX_REFINE_CANDIDATES = 14
_GOOD_SCORE = 0.5
_MAX_RADIUS_TO_SPAN = 10.0
_MAX_FOUR_POINT_MISFIT = 0.15
_MIN_INLIER_FRACTION = 0.7
_SNAP_WINDOW_FACTORS = (2.0, 4.0)
_SNAP_MAX_AREA_FRACTION = 0.5
_SNAP_CONVEX_SOLIDITY = 0.7
_MIN_ROUND_EDGE_POINTS = 40
_NESTED_MIN_RATIO = 1.3
_GRID_SIZE_TOLERANCE = 0.15


# --------------------------------------------------------------------------- ellipse geometry and manual fits


@dataclass(frozen=True)
class _Ellipse:
    """Center, semi-axes (a >= b) and major-axis angle in radians (x toward +y)."""

    cx: float
    cy: float
    a: float
    b: float
    theta: float

    @property
    def mean_diameter(self) -> float:
        return self.a + self.b

    def frame(self, pts: np.ndarray) -> np.ndarray:
        """Coordinates of `pts` in the ellipse's own axes, centered on the ellipse."""
        c, s = math.cos(self.theta), math.sin(self.theta)
        d = pts - np.array([self.cx, self.cy])
        return np.column_stack([d[:, 0] * c + d[:, 1] * s, -d[:, 0] * s + d[:, 1] * c])

    def normalized_radius(self, pts: np.ndarray) -> np.ndarray:
        """sqrt((x/a)^2 + (y/b)^2) for each point; exactly 1 on the ellipse."""
        local = self.frame(pts)
        return np.hypot(local[:, 0] / self.a, local[:, 1] / self.b)

    def points(self, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Boundary points and unit outward normals at parameter angles `t`."""
        c, s = math.cos(self.theta), math.sin(self.theta)
        lx, ly = self.a * np.cos(t), self.b * np.sin(t)
        pts = np.column_stack([self.cx + lx * c - ly * s, self.cy + lx * s + ly * c])
        nx, ny = np.cos(t) / self.a, np.sin(t) / self.b
        n = np.column_stack([nx * c - ny * s, nx * s + ny * c])
        return pts, n / np.linalg.norm(n, axis=1, keepdims=True)

    def to_candidate(self, score: float) -> CircleCandidate:
        return CircleCandidate(
            center_px=(float(self.cx), float(self.cy)),
            axes_px=(float(2 * self.a), float(2 * self.b)),
            angle_deg=float(math.degrees(self.theta) % 180.0),
            score=float(score),
        )


def _as_points(points: Sequence[Sequence[float]], minimum: int) -> np.ndarray:
    pts = np.asarray(points, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[1] != 2 or not np.all(np.isfinite(pts)):
        raise ValueError("points must be a finite sequence of (x, y) pairs")
    if pts.shape[0] < minimum:
        raise ValueError(f"at least {minimum} points are required, got {pts.shape[0]}")
    return pts


def _circle_through_three(pts: np.ndarray) -> _Ellipse:
    (ax, ay), (bx, by), (cx, cy) = pts
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    scale = max(np.ptp(pts[:, 0]), np.ptp(pts[:, 1]), 1e-12)
    if abs(d) < 1e-9 * scale * scale:
        raise ValueError(
            "the three points are collinear or coincident; they do not define a circle"
        )
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    radius = math.hypot(ax - ux, ay - uy)
    if radius > _MAX_RADIUS_TO_SPAN * scale * math.sqrt(2):
        raise ValueError(
            "the three points are nearly collinear; they do not define a usable circle"
        )
    return _Ellipse(ux, uy, radius, radius, 0.0)


def _circle_least_squares(pts: np.ndarray) -> _Ellipse:
    mean = pts.mean(axis=0)
    q = pts - mean
    scale = float(np.abs(q).max())
    if scale < 1e-12:
        raise ValueError("points coincide; they do not define a circle")
    q = q / scale
    design = np.column_stack([2 * q[:, 0], 2 * q[:, 1], np.ones(len(q))])
    sol, *_ = np.linalg.lstsq(design, (q**2).sum(axis=1), rcond=None)
    radius_sq = sol[2] + sol[0] ** 2 + sol[1] ** 2
    if not radius_sq > 0:
        raise ValueError("points do not define a circle")
    radius = math.sqrt(radius_sq) * scale
    ellipse = _Ellipse(mean[0] + sol[0] * scale, mean[1] + sol[1] * scale, radius, radius, 0.0)
    spread = np.abs(np.hypot(*(pts - [ellipse.cx, ellipse.cy]).T) - radius).max()
    if spread > _MAX_FOUR_POINT_MISFIT * radius:
        raise ValueError("the four points do not lie on a circle (collinear or too uneven)")
    return ellipse


def _ellipse_direct_fit(pts: np.ndarray) -> _Ellipse:
    """Direct least-squares ellipse fit (Halir and Flusser), in float64."""
    mean = pts.mean(axis=0)
    scale = float(np.abs(pts - mean).max())
    if scale < 1e-12:
        raise ValueError("points coincide; they do not define an ellipse")
    x, y = ((pts - mean) / scale).T
    d1 = np.column_stack([x * x, x * y, y * y])
    d2 = np.column_stack([x, y, np.ones_like(x)])
    s1, s2, s3 = d1.T @ d1, d1.T @ d2, d2.T @ d2
    try:
        t = -np.linalg.solve(s3, s2.T)
        m = s1 + s2 @ t
        m = np.array([m[2] / 2, -m[1], m[0] / 2])
        eigval, eigvec = np.linalg.eig(
            np.linalg.solve(np.array([[0, 0, 2], [0, -1, 0], [2, 0, 0]]), m)
        )
    except np.linalg.LinAlgError as exc:
        raise ValueError("points do not define an ellipse") from exc
    eigvec = np.real(eigvec)
    cond = 4 * eigvec[0] * eigvec[2] - eigvec[1] ** 2
    if not np.any(cond > 0):
        raise ValueError("points do not lie on an ellipse")
    a1 = eigvec[:, int(np.argmax(cond))]
    conic = np.concatenate([a1, t @ a1])
    return _ellipse_from_conic(conic, mean, scale)


def _ellipse_from_conic(conic: np.ndarray, mean: np.ndarray, scale: float) -> _Ellipse:
    a, b, c, d, e, f = conic
    quad = np.array([[a, b / 2], [b / 2, c]])
    try:
        center = -np.linalg.solve(quad, np.array([d / 2, e / 2]))
    except np.linalg.LinAlgError as exc:
        raise ValueError("points do not define an ellipse") from exc
    f0 = f + (d * center[0] + e * center[1]) / 2
    if f0 > 0:
        quad, f0 = -quad, -f0
    eigval, eigvec = np.linalg.eigh(quad)
    if not (np.all(eigval < 0) and f0 < 0) and not np.all(eigval > 0):
        raise ValueError("points do not define an ellipse")
    axes = np.sqrt(-f0 / np.abs(eigval))
    major = int(np.argmax(axes))
    a_semi, b_semi = float(axes[major]), float(axes[1 - major])
    theta = math.atan2(eigvec[1, major], eigvec[0, major])
    if not (b_semi / a_semi > 0 and math.isfinite(a_semi)):
        raise ValueError("points do not define an ellipse")
    return _Ellipse(
        float(mean[0] + center[0] * scale),
        float(mean[1] + center[1] * scale),
        a_semi * scale,
        b_semi * scale,
        theta,
    )


def circle_from_edge_points(points_px: Sequence[tuple[float, float]]) -> CircleCandidate:
    """Fit the user's clicks on a reference circle's edge.

    3 points give the exact circle through them; 4 give a least-squares circle; 5 or more give a
    direct least-squares ellipse (so a tilted circle is recovered). `axes_px` are full axis
    lengths (major, minor) and `angle_deg` the major axis angle in [0, 180), x toward +y.
    The score is 1.0 (a user-supplied fit is not ranked). Raises ValueError for non-finite input,
    fewer than 3 points, collinear or coincident points, 4 points that are not roughly co-circular
    (misfit above 15 % of the radius), or 5 or more points that are not a plausible ellipse (axis
    ratio below 0.2).
    """
    pts = _as_points(points_px, 3)
    n = pts.shape[0]
    if n == 3:
        ellipse = _circle_through_three(pts)
    elif n == 4:
        ellipse = _circle_least_squares(pts)
    else:
        ellipse = _ellipse_direct_fit(pts)
        if ellipse.b / ellipse.a < 0.2:
            raise ValueError("points do not lie on a plausible circle or ellipse (collinear?)")
    return ellipse.to_candidate(1.0)


def reference_from_circle(c: CircleCandidate, kind: ReferenceKind, method: str) -> ReferenceResult:
    """Build the `ReferenceResult` for candidate `c` as reference `kind` ("auto" or "manual").

    `h_px_to_plane` is the isotropic similarity from `transform.similarity_from_circle` (circle
    center at the plane origin). A tilt warning is added when the axis ratio is below 0.98.
    Raises ValueError for an unknown or non-circle kind, an unknown method, or non-finite or
    non-positive center/axes.
    """
    try:
        kind = ReferenceKind(kind)
    except ValueError:
        raise ValueError(f"unknown reference kind {kind!r}") from None
    if kind not in CIRCLE_KINDS:
        raise ValueError(f"{kind.value} is not a circle reference")
    if method not in ("auto", "manual"):
        raise ValueError(f"method must be 'auto' or 'manual', got {method!r}")
    if not all(math.isfinite(v) for v in (*c.axes_px, *c.center_px)):
        raise ValueError("circle center and axes must be finite")
    major, minor = max(c.axes_px), min(c.axes_px)
    if not minor > 0:
        raise ValueError("circle axes must be positive")
    ratio = minor / major
    h = similarity_from_circle(c, REFERENCE_SIZE_IN[kind])
    warnings = ()
    if ratio < TILT_AXIS_RATIO:
        warnings = (
            f"Camera tilt detected (circle axis ratio {ratio:.2f}): scale is approximate. "
            "Re-shoot straight on, or use a grid reference.",
        )
    return ReferenceResult(
        kind=kind,
        method=method,  # type: ignore[arg-type]
        h_px_to_plane=tuple(tuple(float(v) for v in row) for row in h),
        axis_ratio=float(ratio),
        warnings=warnings,
    )


# --------------------------------------------------------------------------- candidate masks


def _validate_image(img: np.ndarray) -> np.ndarray:
    if not isinstance(img, np.ndarray) or img.dtype != np.uint8:
        raise ValueError("img must be a uint8 numpy array")
    if img.ndim not in (2, 3) or (img.ndim == 3 and img.shape[2] != 3):
        raise ValueError("img must be a BGR (H, W, 3) or gray (H, W) image")
    if min(img.shape[:2]) < _MIN_IMAGE_SIDE:
        raise ValueError(f"img must be at least {_MIN_IMAGE_SIDE} px on each side")
    if max(img.shape[:2]) > MAX_ASPECT_RATIO * min(img.shape[:2]):
        raise ValueError(f"img aspect ratio must not exceed {MAX_ASPECT_RATIO}:1")
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if img.ndim == 2 else img


def _linear_channels(bgr: np.ndarray) -> dict[str, np.ndarray]:
    """Channels that are linear in pixel values, so a blurred edge keeps its midpoint."""
    blue, green, red = cv2.split(bgr)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return {"B": blue, "G": green, "R": red, "gray": gray}


def _channels(bgr: np.ndarray) -> dict[str, np.ndarray]:
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lightness, a, b = cv2.split(lab)
    chroma = np.clip(np.hypot(a.astype(np.float32) - 128, b.astype(np.float32) - 128) * 2, 0, 255)
    return {
        "L": lightness,
        "a": a,
        "b": b,
        "S": hsv[:, :, 1],
        "chroma": chroma.astype(np.uint8),
    }


def _masks(bgr: np.ndarray) -> Iterator[np.ndarray]:
    """Binary masks that could isolate a uniformly colored disc: Otsu splits and Lab clusters."""
    smooth = cv2.GaussianBlur(bgr, (0, 0), 1.0)
    for channel in _channels(smooth).values():
        threshold, _ = cv2.threshold(channel, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        yield channel > threshold
        yield channel <= threshold
    lab = cv2.cvtColor(smooth, cv2.COLOR_BGR2LAB).astype(np.float32)
    flat = lab.reshape(-1, 3)
    step = max(1, flat.shape[0] // 40000)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.5)
    for k in (3, 5, 8):
        cv2.setRNGSeed(1234 + k)
        _, _, centers = cv2.kmeans(flat[::step], k, None, criteria, 2, cv2.KMEANS_PP_CENTERS)
        distances = ((flat[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        labels = distances.argmin(axis=1).reshape(lab.shape[:2])
        for j in range(k):
            yield labels == j


@dataclass(frozen=True)
class _Coarse:
    ellipse: _Ellipse
    quality: float


def _coarse_from_mask(mask: np.ndarray, d_min: float, d_max: float) -> Iterator[_Coarse]:
    height, width = mask.shape
    mask_u8 = mask.astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask_u8 = cv2.morphologyEx(mask_u8, cv2.MORPH_OPEN, kernel)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask_u8, connectivity=8)
    for i in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[i])
        if x <= 0 or y <= 0 or x + w >= width or y + h >= height:
            continue
        if not d_min <= (w + h) / 2 <= d_max or not 0.55 <= w / h <= 1.8 or area < 0.2 * w * h:
            continue
        sub = np.pad((labels[y : y + h, x : x + w] == i).astype(np.uint8), 1)
        contours, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        contour = max(contours, key=cv2.contourArea)
        if len(contour) < 20:
            continue
        pts = contour[:, 0, :].astype(np.float64) + [x - 1, y - 1]
        coarse = _validate_coarse(pts)
        if coarse is None:
            hull = cv2.convexHull(contour)[:, 0, :].astype(np.float64) + [x - 1, y - 1]
            hull = _densify(hull)
            coarse = _validate_coarse(hull)
            if coarse is not None and not _hull_is_filled(contour, hull):
                coarse = None
        if coarse is not None:
            yield coarse


def _densify(poly: np.ndarray, step: float = 1.0) -> np.ndarray:
    """Points along the closed polygon `poly` at about `step` px spacing."""
    nxt = np.roll(poly, -1, axis=0)
    out = []
    for p, q in zip(poly, nxt, strict=True):
        n = max(1, int(np.hypot(*(q - p)) / step))
        out.append(p + (q - p) * (np.arange(n) / n)[:, None])
    return np.vstack(out)


def _hull_is_filled(contour: np.ndarray, hull: np.ndarray) -> bool:
    return cv2.contourArea(contour) >= 0.85 * cv2.contourArea(hull.astype(np.float32))


def _validate_coarse(contour: np.ndarray) -> _Coarse | None:
    """Accept a contour only if it is a round ellipse (rejects diamonds, crosses, ragged blobs)."""
    (cx, cy), (w, h), angle = cv2.fitEllipse(contour.astype(np.float32))
    if min(w, h) <= 0:
        return None
    a_full, b_full = max(w, h), min(w, h)
    theta = math.radians(angle if w >= h else angle + 90.0)
    ellipse = _Ellipse(cx, cy, a_full / 2, b_full / 2, theta)
    if b_full / a_full < 0.55:
        return None
    radii = ellipse.normalized_radius(contour)
    pixel_term = 0.7 / ((ellipse.a + ellipse.b) / 2)
    if (
        radii.std() > 0.03 + pixel_term
        or np.percentile(np.abs(radii - 1), 95) > 0.08 + 2 * pixel_term
    ):
        return None
    quality = 1.0 - min(1.0, float(radii.std()) / (0.03 + pixel_term))
    return _Coarse(ellipse, quality)


def _dedupe_coarse(items: list[_Coarse]) -> list[_Coarse]:
    kept: list[_Coarse] = []
    for item in sorted(items, key=lambda c: -c.quality):
        e = item.ellipse
        if any(_same_circle(e, k.ellipse) for k in kept):
            continue
        kept.append(item)
    return kept


def _same_circle(e: _Ellipse, other: _Ellipse) -> bool:
    ratio = e.mean_diameter / other.mean_diameter
    return (
        math.hypot(e.cx - other.cx, e.cy - other.cy) < 0.3 * min(e.a, other.a)
        and 0.8 < ratio < 1.25
    )


# --------------------------------------------------------------------------- sub-pixel refinement


@dataclass(frozen=True)
class _Refined:
    ellipse: _Ellipse
    score: float


def _refine(img: np.ndarray, coarse: _Ellipse, slack_px: float) -> _Refined | None:
    """Fit the ellipse to sub-pixel edge crossings sampled along rays.

    Returns None when too few rays see an edge, too few survive outlier rejection, the fit is not
    round enough, or its size drifts from the coarse ellipse.
    """
    radius = coarse.mean_diameter / 2
    reach = coarse.a * 1.5 + 8
    x0, y0 = max(0, int(coarse.cx - reach)), max(0, int(coarse.cy - reach))
    x1, y1 = (
        min(img.shape[1], int(coarse.cx + reach) + 1),
        min(img.shape[0], int(coarse.cy + reach) + 1),
    )
    crop = cv2.GaussianBlur(img[y0:y1, x0:x1], (0, 0), 1.0)
    local = _Ellipse(coarse.cx - x0, coarse.cy - y0, coarse.a, coarse.b, coarse.theta)
    window = max(6.0, 0.10 * radius, slack_px)
    offsets = np.arange(-window, window + _RAY_STEP_PX / 2, _RAY_STEP_PX)
    t = np.linspace(0, 2 * math.pi, _RAY_COUNT, endpoint=False)
    base, normals = local.points(t)
    sample = base[:, None, :] + offsets[None, :, None] * normals[:, None, :]
    map_x = sample[:, :, 0].astype(np.float32)
    map_y = sample[:, :, 1].astype(np.float32)
    best: _Refined | None = None
    for channels in (_linear_channels(crop), _channels(crop)):
        for channel in channels.values():
            profiles = cv2.remap(
                channel.astype(np.float32),
                map_x,
                map_y,
                cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REPLICATE,
            )
            refined = _fit_rays(profiles, offsets, base, normals, radius, (x0, y0))
            if refined is not None and (best is None or refined.score > best.score):
                best = refined
        if best is not None and best.score >= _GOOD_SCORE:
            break
    return best


def _fit_rays(
    profiles: np.ndarray,
    offsets: np.ndarray,
    base: np.ndarray,
    normals: np.ndarray,
    radius: float,
    origin: tuple[int, int],
) -> _Refined | None:
    k = profiles.shape[1]
    edge = max(2, k * 3 // 10)
    contrast = profiles[:, :edge].mean(axis=1) - profiles[:, -edge:].mean(axis=1)
    sign = 1.0 if np.median(contrast) >= 0 else -1.0
    strength = -sign * (profiles[:, 2:] - profiles[:, :-2]) / 2
    peak = strength[:, 1:-1].argmax(axis=1) + 1
    rows = np.arange(len(profiles))
    left, mid, right = strength[rows, peak - 1], strength[rows, peak], strength[rows, peak + 1]
    denom = left - 2 * mid + right
    shift = np.where(
        np.abs(denom) > 1e-9, 0.5 * (left - right) / np.where(denom == 0, 1, denom), 0.0
    )
    t_edge = offsets[peak + 1] + np.clip(shift, -1, 1) * _RAY_STEP_PX
    valid = (sign * contrast >= _MIN_EDGE_CONTRAST) & (mid > 0)
    if valid.sum() < _MIN_INLIER_FRACTION * len(profiles):
        return None
    points = base + t_edge[:, None] * normals + np.array(origin, dtype=np.float64)
    keep = valid.copy()
    ellipse: _Ellipse | None = None
    for _ in range(4):
        if keep.sum() < _MIN_INLIER_FRACTION * len(profiles):
            return None
        try:
            ellipse = _ellipse_direct_fit(points[keep])
        except ValueError:
            return None
        residual = (ellipse.normalized_radius(points) - 1) * (ellipse.a + ellipse.b) / 2
        spread = float(np.median(np.abs(residual[keep])))
        limit = max(0.5, 3.0 * 1.4826 * spread)
        new_keep = valid & (np.abs(residual) <= limit)
        if np.array_equal(new_keep, keep):
            break
        keep = new_keep
    if ellipse is None:
        return None
    inlier_fraction = float(keep.sum()) / len(profiles)
    residual = (ellipse.normalized_radius(points[keep]) - 1) * (ellipse.a + ellipse.b) / 2
    rms_rel = float(np.sqrt(np.mean(residual**2))) / radius
    if rms_rel > 0.015 + 0.3 / radius:
        return None
    if ellipse.b / ellipse.a < 0.5 or abs(ellipse.mean_diameter / 2 - radius) > max(
        0.12 * radius, 3.0
    ):
        return None
    contrast_term = min(1.0, float(np.median(sign * contrast[keep])) / 50.0)
    circle_term = max(0.0, 1.0 - rms_rel / 0.015)
    return _Refined(ellipse, inlier_fraction * (0.5 + 0.5 * circle_term) * contrast_term)


# --------------------------------------------------------------------------- public detection


def find_circle_candidates(
    img: np.ndarray,
    kind: ReferenceKind,
    max_candidates: int = 5,
    grid: GridCandidate | None = None,
) -> list[CircleCandidate]:
    """Return up to `max_candidates` ranked circular marks that could be the `kind` reference.

    Candidates are round, uniformly colored discs (printed dots, orange stickers on paper or
    cardboard) whose diameter is plausible for `kind` relative to the image's short side. Diamonds,
    crosses, ragged bullet holes and paper edges fail the roundness tests, hole-sized marks fall
    below the size floor, and marks touching the image border are skipped. Every candidate carries
    a sub-pixel ellipse fit and a score in (0, 1] (edge coverage x roundness x edge contrast);
    the list is sorted by score, except that a disc comes before any concentric disc inside it (a
    paster's outer boundary is the reference, not its printed inner ring), and is empty when
    nothing qualifies. With `grid` (a detected 1 in lattice) candidates whose diameter in grid
    inches is within 15 % of the kind's size are moved ahead of the rest, keeping their order.
    Aim dots can look like the
    reference, so the caller must let the user confirm; nothing is ever auto-confirmed. Raises
    ValueError for a bad image, an unknown or non-circle `kind`, or `max_candidates < 1`.
    """
    try:
        kind = ReferenceKind(kind)
    except ValueError:
        raise ValueError(f"unknown reference kind {kind!r}") from None
    if kind not in CIRCLE_KINDS:
        raise ValueError(f"{kind.value} is not a circle reference")
    if max_candidates < 1:
        raise ValueError("max_candidates must be at least 1")
    img = _validate_image(img)
    height, width = img.shape[:2]
    scale = min(1.0, _DETECT_MAX_SIDE / max(height, width))
    small = (
        cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if scale < 1
        else img
    )
    low, high = _SIZE_FRACTION[kind]
    short = min(small.shape[:2])
    d_min, d_max = max(_MIN_DETECT_DIAMETER_PX, low * short), high * short

    found: list[_Coarse] = []
    for mask in _masks(small):
        found.extend(_coarse_from_mask(mask, d_min, d_max))
    coarse = _dedupe_coarse(found)[:_MAX_REFINE_CANDIDATES]

    refined: list[_Refined] = []
    for item in coarse:
        e = item.ellipse
        full = _Ellipse(
            (e.cx + 0.5) / scale - 0.5,
            (e.cy + 0.5) / scale - 0.5,
            e.a / scale,
            e.b / scale,
            e.theta,
        )
        result = _refine(img, full, slack_px=2.5 / scale)
        if result is not None:
            refined.append(result)
    ranked = _outer_first(sorted(refined, key=lambda r: -r.score))
    if grid is not None:
        ranked = _grid_matches_first(ranked, grid, kind)
    return [r.ellipse.to_candidate(r.score) for r in ranked[:max_candidates]]


def _encloses(outer: _Ellipse, inner: _Ellipse) -> bool:
    return (
        outer.mean_diameter > _NESTED_MIN_RATIO * inner.mean_diameter
        and math.hypot(outer.cx - inner.cx, outer.cy - inner.cy) < 0.3 * inner.b
    )


def _outer_first(ranked: list[_Refined]) -> list[_Refined]:
    """Score order, except that a disc comes before every concentric disc nested inside it.

    A chain of nested discs (a paster and its printed inner ring) sits at the rank of its best
    member, outermost first: the sticker's outer boundary is the reference.
    """
    chain_score = {
        id(r): max(
            o.score
            for o in ranked
            if o is r or _encloses(r.ellipse, o.ellipse) or _encloses(o.ellipse, r.ellipse)
        )
        for r in ranked
    }
    return sorted(ranked, key=lambda r: (-chain_score[id(r)], -r.ellipse.mean_diameter))


def _diameter_in(h: np.ndarray, e: _Ellipse) -> float | None:
    """Mean diameter of `e` in the plane of homography `h` (inches), from four axis-pair chords."""
    t = np.arange(4) * math.pi / 4
    near, _ = e.points(t)
    far, _ = e.points(t + math.pi)
    both = np.vstack([near, far])
    mapped = cv2.perspectiveTransform(both[None].astype(np.float64), h.astype(np.float64))[0]
    if not np.all(np.isfinite(mapped)):
        return None
    return float(np.mean(np.hypot(*(mapped[:4] - mapped[4:]).T)))


def _grid_matches_first(
    ranked: list[_Refined], grid: GridCandidate, kind: ReferenceKind
) -> list[_Refined]:
    """Stable partition: candidates whose size in grid inches matches `kind` come first."""
    wanted = REFERENCE_SIZE_IN[kind]

    def matches(r: _Refined) -> bool:
        inches = _diameter_in(grid.h_px_to_plane, r.ellipse)
        return inches is not None and abs(inches / wanted - 1) <= _GRID_SIZE_TOLERANCE

    flags = [matches(r) for r in ranked]
    return [r for r, ok in zip(ranked, flags, strict=True) if ok] + [
        r for r, ok in zip(ranked, flags, strict=True) if not ok
    ]


# --------------------------------------------------------------------------- aim snap


def refine_circle_near(
    img: np.ndarray, point_px: tuple[float, float], max_radius_px: float
) -> CircleCandidate | None:
    """Snap `point_px` to the center of the aim mark (dot, diamond or taped cross) near it.

    `max_radius_px` is how far the returned center may be from the click and the scale of the
    mark; pass roughly the mark's radius or less. A mark is whatever differs in color from the
    window's median color, searched in windows whose half-width is 2x and 4x that radius (a
    bullet hole inside a big dark dot is only a mark in the small window; the dot itself shows in
    the large one). Only pixels of the mark's own color count, so holes on the mark do not bend
    it. Its center is the middle of the distance-transform maximum (the junction of a cross, the
    middle of a diamond), or the center of an ellipse fitted to its clean outer edge when it is
    round; thin attached lines such as grid lines are ignored. The mark with the largest inscribed
    circle whose center lies within reach wins. `axes_px` of the result are the inscribed circle
    diameter of the mark, `angle_deg` is 0 and `score` is the mark's color contrast over 60 (capped
    at 1), not a confidence in the snap. Returns None when no mark is found within reach (the
    caller keeps the raw click). Raises ValueError for a bad image, a point outside it, or a
    non-positive or non-finite `max_radius_px`.
    """
    img = _validate_image(img)
    px, py = float(point_px[0]), float(point_px[1])
    height, width = img.shape[:2]
    if not (math.isfinite(px) and math.isfinite(py) and 0 <= px < width and 0 <= py < height):
        raise ValueError("point_px must lie inside the image")
    if not (math.isfinite(max_radius_px) and max_radius_px > 0):
        raise ValueError("max_radius_px must be positive")
    found = [_snap_in_window(img, px, py, max_radius_px, factor) for factor in _SNAP_WINDOW_FACTORS]
    marks = [m for m in found if m is not None]
    if not marks:
        return None
    peak, _, center, contrast = max(marks, key=lambda m: (m[0], m[1]))
    return CircleCandidate(
        center_px=(float(center[0]), float(center[1])),
        axes_px=(2.0 * peak, 2.0 * peak),
        angle_deg=0.0,
        score=float(min(1.0, contrast / 60.0)),
    )


def _mark_center(
    own: np.ndarray, excluded: np.ndarray, gap: int
) -> tuple[float, np.ndarray] | None:
    """Inscribed radius and center (patch coordinates) of the mark made of the `own` pixels.

    `excluded` holds foreground pixels that are not the mark's color (bullet holes, their halos,
    anti-aliased rims).
    Gaps up to `gap` px are closed; a mark that is then nearly convex is replaced by its convex
    hull so bites taken by holes do not move its center, and a round one is centered by an ellipse
    fitted to its clean outer edge. Returns None when the closed mask has no contour.
    """
    pad = gap // 2 + 2
    sub = cv2.morphologyEx(
        np.pad(own.astype(np.uint8), pad),
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (gap, gap)),
    )
    contours, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    filled = np.zeros_like(sub)
    cv2.drawContours(filled, contours, -1, 1, cv2.FILLED)
    hull_mask = np.zeros_like(sub)
    cv2.fillConvexPoly(hull_mask, cv2.convexHull(np.vstack(contours)), 1)
    convex = filled.sum() >= _SNAP_CONVEX_SOLIDITY * hull_mask.sum()
    dist = cv2.distanceTransform(hull_mask if convex else filled, cv2.DIST_L2, 5)
    peak = float(dist.max())
    ys, xs = np.nonzero(dist >= 0.9 * peak)
    weights = dist[ys, xs]
    center = np.array(
        [np.average(xs, weights=weights) - pad, np.average(ys, weights=weights) - pad]
    )
    if convex:
        round_fit = _round_edge_fit(own, excluded)
        if round_fit is not None:
            return round_fit
    return peak, center


def _round_edge_fit(own: np.ndarray, excluded: np.ndarray) -> tuple[float, np.ndarray] | None:
    """Ellipse fitted to the outer edge of `own`, skipping edge next to `excluded` pixels."""
    contours, _ = cv2.findContours(own.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    pts = np.vstack([c[:, 0, :] for c in contours])
    bulk = cv2.morphologyEx(
        excluded.astype(np.uint8),
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)),
    )
    near = cv2.dilate(bulk, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
    pts = pts[near[pts[:, 1], pts[:, 0]] == 0].astype(np.float64)
    ellipse = None
    for _ in range(3):
        if len(pts) < _MIN_ROUND_EDGE_POINTS:
            return None
        try:
            ellipse = _ellipse_direct_fit(pts)
        except ValueError:
            return None
        radius = (ellipse.a + ellipse.b) / 2
        residual = np.abs(ellipse.normalized_radius(pts) - 1) * radius
        limit = max(1.0, 3.0 * 1.4826 * float(np.median(residual)))
        if np.all(residual <= limit):
            break
        pts = pts[residual <= limit]
    else:
        if len(pts) < _MIN_ROUND_EDGE_POINTS:
            return None
        try:
            ellipse = _ellipse_direct_fit(pts)
        except ValueError:
            return None
        radius = (ellipse.a + ellipse.b) / 2
    if ellipse is None:
        return None
    local = pts - [ellipse.cx, ellipse.cy]
    angles = np.sort(np.arctan2(local[:, 1], local[:, 0]))
    largest_gap = np.max(np.diff(np.concatenate([angles, [angles[0] + 2 * np.pi]])))
    residual = (ellipse.normalized_radius(pts) - 1) * radius
    if largest_gap > math.radians(170) or np.sqrt(np.mean(residual**2)) > 0.012 * radius + 0.7:
        return None
    return ellipse.b, np.array([ellipse.cx, ellipse.cy])


def _snap_in_window(
    img: np.ndarray, px: float, py: float, max_radius_px: float, window_factor: float
) -> tuple[float, float, np.ndarray, float] | None:
    """Best mark in one window as (inscribed radius, -offset from click, center, contrast)."""
    height, width = img.shape[:2]
    half = int(math.ceil(window_factor * max_radius_px)) + 2
    x0, y0 = max(0, int(px) - half), max(0, int(py) - half)
    x1, y1 = min(width, int(px) + half + 1), min(height, int(py) + half + 1)
    crop = cv2.GaussianBlur(img[y0:y1, x0:x1], (0, 0), 1.0)
    lab = cv2.cvtColor(crop, cv2.COLOR_BGR2LAB).astype(np.float32)
    background = np.median(lab.reshape(-1, 3), axis=0)
    delta = np.linalg.norm(lab - background, axis=2)
    otsu, _ = cv2.threshold(
        np.clip(delta * 2, 0, 255).astype(np.uint8), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    threshold = min(max(float(otsu) / 2, 16.0), 30.0)
    thin = int(min(25, max(3, round(0.08 * max_radius_px)))) | 1
    mask = cv2.morphologyEx(
        (delta > threshold).astype(np.uint8),
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (thin, thin)),
    )
    gap = int(max(3, round(0.7 * max_radius_px))) | 1
    click = np.array([px - x0, py - y0])
    best: tuple[float, float, np.ndarray, float] | None = None
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    for i in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < 12:
            continue
        member = labels[y : y + h, x : x + w] == i
        patch = lab[y : y + h, x : x + w]
        mark_color = np.median(patch[member], axis=0)
        tolerance = float(np.clip(0.5 * np.linalg.norm(mark_color - background), 14.0, 40.0))
        own = member & (np.linalg.norm(patch - mark_color, axis=2) < tolerance)
        found = _mark_center(own, member & ~own, gap)
        if found is None:
            continue
        peak, local_center = found
        if peak * peak * math.pi > _SNAP_MAX_AREA_FRACTION * mask.size:
            continue
        center = local_center + np.array([x, y])
        offset = float(np.linalg.norm(center - click))
        if offset > max_radius_px or peak < 2.0:
            continue
        if best is None or (peak, -offset) > (best[0], best[1]):
            best = (peak, -offset, center, float(delta[y : y + h, x : x + w][own].mean()))
    if best is None:
        return None
    peak, neg_offset, center, contrast = best
    return peak, neg_offset, center + np.array([x0, y0]), contrast
