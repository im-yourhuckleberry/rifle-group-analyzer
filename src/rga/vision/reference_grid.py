"""Printed 1-inch grid reference: automatic detection and the manual 4-corner fallback.

`find_grid(img)` locates grid line crossings (any line colour, any orientation), fits a projective
lattice and returns a `GridCandidate`; `grid_from_manual` builds one from four user clicks;
`reference_from_grid` turns a candidate into a `ReferenceResult`.

Plane convention: lattice spacing is 1 inch; plane x follows the image-horizontal grid direction and
y the image-vertical one, without mirroring. The plane origin is the lattice corner with the
smallest indices.
"""

import math
from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np

from rga.domain.types import ReferenceKind, ReferenceResult
from rga.vision.io import MAX_ASPECT_RATIO
from rga.vision.reference_grid_lines import find_line_lattice
from rga.vision.transform import apply_h, homography_from_points

MIN_INLIERS = 9  # fewest lattice crossings find_grid accepts
MIN_LATTICE_SPAN = 3  # fewest distinct crossing indices along each lattice axis
NON_PLANAR_RMS_IN = 0.05  # rms residual above which `non_planar` is warned
NON_PLANAR_WARNING = "non_planar: the grid lines are curved; flatten the target and re-shoot"
PARTIAL_GRID_WARNING = "Grid found from partial lines — check that one square is 1 inch"

_WORK_MAX_SIDE = 2000
_LINE_KERNEL_FRACTION = 0.012
_CANDIDATE_FRACTION = 0.15
_MIN_LINE_EXTENT_KERNELS = 5
_LINE_THRESHOLD_FRACTION = 0.7
_MAX_MASK_FRACTION = 0.2
_CANDIDATE_RADIUS_FRACTION = 0.03
_MAX_CANDIDATES = 1500
_SEED_TOLERANCE = 0.12
_GROW_TOLERANCE = 0.18
_MAX_GROW_PASSES = 40
_MAX_INDEX = 60
_LOCAL_FIT_POINTS = 12
_MAX_PROPAGATED_SEEDS = 40
_GLOBAL_TOLERANCE = 0.25
_SEED_OVERLAP_SKIP = 0.8
_MAX_SEEDS = 4000
_MAX_NEIGHBOURS = 6
_MIN_SPACING_FRACTION = 0.06
_MAX_SPACING_FRACTION = 0.5
_MANUAL_MAX_SQUARES = 100


@dataclass(frozen=True)
class GridCandidate:
    """A fitted lattice: crossings (px), their integer lattice indices (inches), and the px to plane map.

    `rms_residual_in` is the rms distance, in inches, between every matched crossing and its lattice
    position under `h_px_to_plane`. `score` is in (0, 1], higher for a fuller, tighter lattice; 1.0 for
    a manual grid. `from_partial_lines` is True for a lattice found by the thin-line fallback, whose
    unit is inferred from line spacing and must be confirmed by the user.
    """

    intersections_px: np.ndarray
    lattice_idx: np.ndarray
    h_px_to_plane: np.ndarray
    rms_residual_in: float
    score: float
    from_partial_lines: bool = False


# ------------------------------------------------------------------ line response and candidates


def _as_gray_min(img: np.ndarray) -> np.ndarray:
    """Darkest channel per pixel: dark for black, red, green and blue printed lines alike."""
    if not isinstance(img, np.ndarray) or img.dtype != np.uint8 or img.ndim not in (2, 3):
        raise ValueError("img must be a uint8 grayscale or BGR array")
    if img.ndim == 3 and img.shape[2] not in (3, 4):
        raise ValueError("img must have 3 (BGR) or 4 (BGRA) channels")
    if img.size == 0 or min(img.shape[:2]) < 32:
        raise ValueError("img is too small")
    if max(img.shape[:2]) > MAX_ASPECT_RATIO * min(img.shape[:2]):
        raise ValueError(f"img aspect ratio must not exceed {MAX_ASPECT_RATIO}:1")
    return img[:, :, :3].min(axis=2) if img.ndim == 3 else img


def _line_response(dark: np.ndarray) -> tuple[np.ndarray, int]:
    """Black-hat of the darkest-channel image: thin dark structures narrower than the kernel."""
    height, width = dark.shape
    k = max(9, round(_LINE_KERNEL_FRACTION * min(height, width)) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    response = cv2.morphologyEx(dark.astype(np.float32), cv2.MORPH_BLACKHAT, kernel)
    return response, k


def _isodata_threshold(response: np.ndarray) -> float:
    """Two-class mean split (isodata) of the response histogram."""
    hist = np.bincount(np.clip(response, 0, 255).astype(np.uint8).ravel(), minlength=256).astype(
        np.float64
    )
    levels = np.arange(256, dtype=np.float64)
    t = float((hist * levels).sum() / hist.sum())
    for _ in range(50):
        below, above = levels <= t, levels > t
        if hist[below].sum() == 0 or hist[above].sum() == 0:
            break
        split = 0.5 * (
            (hist[below] * levels[below]).sum() / hist[below].sum()
            + (hist[above] * levels[above]).sum() / hist[above].sum()
        )
        if abs(split - t) < 0.5:
            return float(split)
        t = float(split)
    return t


def _line_mask(response: np.ndarray, k: int) -> np.ndarray | None:
    """Binary line pixels (above 0.7 of the isodata split); None when none survive or the mask is too dense.

    Connected fragments shorter than `_MIN_LINE_EXTENT_KERNELS` kernels are dropped.
    """
    binary = (response > max(10.0, _LINE_THRESHOLD_FRACTION * _isodata_threshold(response))).astype(
        np.uint8
    )
    _, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    extent = np.maximum(stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT])
    keep = extent >= _MIN_LINE_EXTENT_KERNELS * k
    keep[0] = False
    mask = keep[labels]
    return mask if 0 < mask.mean() <= _MAX_MASK_FRACTION else None


def _refined_candidates(response: np.ndarray, mask: np.ndarray, coarse: np.ndarray) -> np.ndarray:
    """Sub-pixel crossings; coarse peaks without two distinct line arms and duplicates within 3 px are dropped."""
    radius = int(np.clip(round(_CANDIDATE_RADIUS_FRACTION * min(mask.shape)), 10, 60))
    kept: list[np.ndarray] = []
    for c in coarse:
        refined = _refine_crossing(response, mask, c, radius)
        if refined is not None and all(np.linalg.norm(refined - q) > 3.0 for q in kept):
            kept.append(refined)
    return np.array(kept) if kept else np.empty((0, 2))


def _crossing_candidates(mask: np.ndarray, k: int) -> np.ndarray:
    """Coarse crossing positions (x, y): strongest minimum-eigenvalue peaks of the line mask."""
    blurred = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), 1.5)
    strength = cv2.cornerMinEigenVal(blurred, int(k * 0.75) | 1, ksize=3)
    peak_window = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    is_peak = (strength == cv2.dilate(strength, peak_window)) & (strength > 0)
    ys, xs = np.nonzero(is_peak)
    values = strength[ys, xs]
    if values.size == 0:
        return np.empty((0, 2))
    keep = values > _CANDIDATE_FRACTION * values.max()
    xs, ys, values = xs[keep], ys[keep], values[keep]
    order = np.argsort(-values)[:_MAX_CANDIDATES]
    return np.stack([xs[order], ys[order]], axis=1).astype(np.float64)


# ------------------------------------------------------------------ lattice fitting


def _unit_square_h(
    p: np.ndarray, q1: np.ndarray, q2: np.ndarray, q12: np.ndarray
) -> np.ndarray | None:
    src = np.array([p, q1, q2, q12], dtype=np.float32)
    dst = np.array([[0, 0], [1, 0], [0, 1], [1, 1]], dtype=np.float32)
    try:
        h = cv2.getPerspectiveTransform(src, dst)
    except cv2.error:
        return None
    return h.astype(np.float64) if np.all(np.isfinite(h)) else None


def _match_lattice(h: np.ndarray, pts: np.ndarray, tol: float) -> np.ndarray:
    """Indices of points within `tol` of an integer lattice position under `h`, one per cell."""
    try:
        uv = apply_h(h, pts)
    except ValueError:
        return np.empty(0, int)
    ij = np.rint(uv)
    residual = np.linalg.norm(uv - ij, axis=1)
    near = np.nonzero(residual < tol)[0]
    if near.size == 0:
        return near
    order = near[np.argsort(residual[near], kind="stable")]
    _, first = np.unique(ij[order], axis=0, return_index=True)
    return order[np.sort(first)]


def _seed_cells(
    pts: np.ndarray, dmin: float, dmax: float
) -> list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    """Candidate unit cells (p, q1, q2, q12): four crossings forming a quadrilateral with a clockwise turn."""
    dist = np.sqrt(((pts[:, None, :] - pts[None, :, :]) ** 2).sum(axis=2))
    cells = []
    for i in range(len(pts)):
        near = np.nonzero((dist[i] >= dmin) & (dist[i] <= dmax))[0]
        near = near[np.argsort(dist[i][near], kind="stable")][:_MAX_NEIGHBOURS]
        for a in range(len(near)):
            for b in range(a + 1, len(near)):
                va, vb = pts[near[a]] - pts[i], pts[near[b]] - pts[i]
                cross = va[0] * vb[1] - va[1] * vb[0]
                cos = (va @ vb) / (np.linalg.norm(va) * np.linalg.norm(vb))
                if abs(cos) > 0.6 or cross == 0:
                    continue
                q1, q2 = (pts[near[a]], pts[near[b]]) if cross > 0 else (pts[near[b]], pts[near[a]])
                gap = np.linalg.norm(pts - (q1 + q2 - pts[i]), axis=1)
                j = int(np.argmin(gap))
                scale = 0.5 * (np.linalg.norm(q1 - pts[i]) + np.linalg.norm(q2 - pts[i]))
                if gap[j] >= 0.2 * scale:
                    continue
                cells.append((pts[i], q1, q2, pts[j]))
    return cells[:_MAX_SEEDS]


def _ranked_seed_hypotheses(
    pts: np.ndarray, dmin: float, dmax: float
) -> list[tuple[np.ndarray, frozenset[int]]]:
    """Seed homographies (px to unit lattice) with their matched point indices, most points first."""
    scored: dict[tuple[int, ...], np.ndarray] = {}
    for p, q1, q2, q12 in _seed_cells(pts, dmin, dmax):
        h = _unit_square_h(p, q1, q2, q12)
        if h is None:
            continue
        key = tuple(sorted(int(v) for v in _match_lattice(h, pts, _SEED_TOLERANCE)))
        if len(key) >= 4 and key not in scored:
            scored[key] = h
    ranked = sorted(scored.items(), key=lambda kv: (-len(kv[0]), kv[0]))
    return [(h, frozenset(key)) for key, h in ranked[:_MAX_PROPAGATED_SEEDS]]


def _local_prediction(
    matched: dict[tuple[int, int], int], pts: np.ndarray, target: tuple[int, int]
) -> tuple[np.ndarray, float] | None:
    """Predicted px position of lattice index `target` and the local spacing, from nearby matched points."""
    idx = np.array(list(matched.keys()), dtype=np.float64)
    near = np.argsort(np.linalg.norm(idx - np.array(target), axis=1), kind="stable")[
        :_LOCAL_FIT_POINTS
    ]
    if len(near) < 4:
        return None
    lattice = idx[near]
    if np.linalg.matrix_rank(lattice - lattice.mean(axis=0)) < 2:
        return None
    px = pts[[matched[(int(i), int(j))] for i, j in lattice]]
    h, _ = cv2.findHomography(lattice, px, 0)
    if h is None or not np.all(np.isfinite(h)):
        return None
    try:
        here = apply_h(
            h, np.array([target, (target[0] + 1, target[1]), (target[0], target[1] + 1)], float)
        )
    except ValueError:
        return None
    spacing = 0.5 * (np.linalg.norm(here[1] - here[0]) + np.linalg.norm(here[2] - here[0]))
    return here[0], float(spacing)


def _lattice_extent(matched: dict[tuple[int, int], int], pts: np.ndarray) -> float:
    """Hull area (px^2) of the matched points that one projective map explains.

    0.0 when fewer than `MIN_INLIERS` points fit one map within `_GLOBAL_TOLERANCE` lattice units.
    """
    if len(matched) < MIN_INLIERS:
        return 0.0
    cells = np.array(list(matched), dtype=np.float64)
    px = pts[list(matched.values())]
    h, inliers = cv2.findHomography(px, cells, cv2.RANSAC, _GLOBAL_TOLERANCE)
    if h is None or inliers is None or int(inliers.sum()) < MIN_INLIERS:
        return 0.0
    return float(cv2.contourArea(cv2.convexHull(px[inliers.ravel() > 0].astype(np.float32))))


def _propagate(seed_h: np.ndarray, pts: np.ndarray) -> dict[tuple[int, int], int]:
    """Grow a lattice outward from a seed cell by predicting each neighbouring crossing from nearby ones.

    Returns lattice index -> candidate index.
    """
    seed = _match_lattice(seed_h, pts, _SEED_TOLERANCE)
    uv = np.rint(apply_h(seed_h, pts[seed])).astype(int)
    matched = {(int(i), int(j)): int(k) for (i, j), k in zip(uv, seed, strict=True)}
    used = set(matched.values())
    for _ in range(_MAX_GROW_PASSES):
        frontier = sorted(
            {
                (i + di, j + dj)
                for (i, j) in matched
                for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1))
                if (i + di, j + dj) not in matched
                and abs(i + di) <= _MAX_INDEX
                and abs(j + dj) <= _MAX_INDEX
            }
        )
        added = False
        for target in frontier:
            prediction = _local_prediction(matched, pts, target)
            if prediction is None:
                continue
            expected, spacing = prediction
            gap = np.linalg.norm(pts - expected, axis=1)
            for k in np.argsort(gap, kind="stable")[:3]:
                if gap[k] > _GROW_TOLERANCE * spacing:
                    break
                if int(k) not in used:
                    matched[target] = int(k)
                    used.add(int(k))
                    added = True
                    break
        if not added:
            break
    return matched


# ------------------------------------------------------------------ sub-pixel crossing refinement


def _line_fit(points: np.ndarray, weights: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mean = (points * weights[:, None]).sum(axis=0) / weights.sum()
    centred = points - mean
    cov = (centred * weights[:, None]).T @ centred
    _, vecs = np.linalg.eigh(cov)
    return mean, vecs[:, 1]


def _orientation_window(response: np.ndarray, x0: int, x1: int, y0: int, y1: int) -> np.ndarray:
    """Local line direction (radians, mod pi) at every pixel of a window, from a smoothed structure tensor."""
    pad = 6
    crop = response[max(0, y0 - pad) : y1 + pad, max(0, x0 - pad) : x1 + pad]
    smooth = cv2.GaussianBlur(crop, (0, 0), 1.2)
    gx, gy = (
        cv2.Sobel(smooth, cv2.CV_32F, 1, 0, ksize=3),
        cv2.Sobel(smooth, cv2.CV_32F, 0, 1, ksize=3),
    )
    jxx, jyy, jxy = (cv2.GaussianBlur(v, (0, 0), 2.5) for v in (gx * gx, gy * gy, gx * gy))
    theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy) + np.pi / 2
    top, left = y0 - max(0, y0 - pad), x0 - max(0, x0 - pad)
    return np.mod(theta[top : top + (y1 - y0), left : left + (x1 - x0)], np.pi)


def _angle_gap(a: np.ndarray | float, b: float) -> np.ndarray | float:
    d = np.abs(a - b)
    return np.minimum(d, np.pi - d)


def _refine_crossing(
    response: np.ndarray, mask: np.ndarray, center: np.ndarray, radius: int
) -> np.ndarray | None:
    """Intersection of the two line arms around `center`; None when two distinct arms are not found."""
    height, width = mask.shape
    found: np.ndarray | None = None
    for _ in range(2):
        cx, cy = center
        x0, x1 = max(0, int(cx) - radius), min(width, int(cx) + radius + 1)
        y0, y1 = max(0, int(cy) - radius), min(height, int(cy) + radius + 1)
        ys, xs = np.nonzero(mask[y0:y1, x0:x1])
        if xs.size < 16:
            return found
        theta = _orientation_window(response, x0, x1, y0, y1)[ys, xs]
        pts = np.stack([xs + x0, ys + y0], axis=1).astype(np.float64)
        r = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
        keep = (r >= 0.2 * radius) & (r <= radius)
        pts, theta = pts[keep], theta[keep]
        if len(pts) < 16:
            return found
        w = response[pts[:, 1].astype(int), pts[:, 0].astype(int)].astype(np.float64)
        hist, edges = np.histogram(theta, bins=36, range=(0, np.pi), weights=w)
        centers = (edges[:-1] + edges[1:]) / 2
        p1 = int(np.argmax(hist))
        far = _angle_gap(centers, centers[p1]) > math.radians(30)
        if not far.any() or hist[far].max() < 0.2 * hist[p1]:
            return found
        p2 = int(np.nonzero(far)[0][np.argmax(hist[far])])
        d1, d2 = _angle_gap(theta, centers[p1]), _angle_gap(theta, centers[p2])
        arms = []
        for sel in (d1 < np.minimum(d2, math.radians(20)), d2 < np.minimum(d1, math.radians(20))):
            if sel.sum() < 8:
                return found
            arms.append(_line_fit(pts[sel], w[sel]))
        (m1, u1), (m2, u2) = arms
        det = u1[0] * (-u2[1]) - u1[1] * (-u2[0])
        if abs(det) < 0.5:
            return found
        t = ((m2[0] - m1[0]) * (-u2[1]) - (m2[1] - m1[1]) * (-u2[0])) / det
        refined = m1 + t * u1
        if np.linalg.norm(refined - center) > 0.5 * radius:
            return found
        center = found = refined
    return found


# ------------------------------------------------------------------ orientation and assembly


_ROTATIONS = [
    np.array([[1, 0], [0, 1]]),
    np.array([[0, -1], [1, 0]]),
    np.array([[-1, 0], [0, -1]]),
    np.array([[0, 1], [-1, 0]]),
]


def _image_aligned_labels(h: np.ndarray, idx: np.ndarray, px: np.ndarray) -> np.ndarray:
    """Relabel lattice indices by a quarter-turn so plane +x/+y follow image +x/+y as closely as possible."""
    center = px.mean(axis=0)
    base = apply_h(h, center[None])[0]
    steps = apply_h(np.linalg.inv(h), np.array([base + [1, 0], base + [0, 1]])) - center
    best, best_score = _ROTATIONS[0], -np.inf
    for rot in _ROTATIONS:
        ex = steps.T @ rot.T @ np.array([1.0, 0.0])
        ey = steps.T @ rot.T @ np.array([0.0, 1.0])
        score = ex[0] / np.linalg.norm(ex) + ey[1] / np.linalg.norm(ey)
        if score > best_score:
            best, best_score = rot, score
    return idx @ best.T


def find_grid(img: np.ndarray) -> GridCandidate | None:
    """Detect a printed 1-inch grid in a BGR, BGRA or grayscale uint8 image; None when no lattice is found.

    Any line colour and any 90 degree rotation. Needs at least `MIN_INLIERS` crossings spanning at least
    `MIN_LATTICE_SPAN` indices along each axis. When the crossing search finds no lattice (a band, a
    paster or the sheet edge leaves too few crossings), falls back to the long thin lines themselves:
    at least two lines per direction (three in one), consistent unit spacing, and four crossings
    (see `reference_grid_lines`). Curved paper returns a candidate with a large `rms_residual_in`
    (see `reference_from_grid`). Raises ValueError for a bad image.
    """
    dark = _as_gray_min(img)
    full_h, full_w = dark.shape
    scale = min(1.0, _WORK_MAX_SIDE / max(full_h, full_w))
    work = (
        cv2.resize(dark, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if scale < 1
        else dark
    )

    response, k = _line_response(work)
    mask = _line_mask(response, k)
    if mask is None:
        return None
    found = _crossing_lattice(work, response, mask, k, scale)
    return found if found is not None else _line_lattice(response, mask, k, scale)


def _crossing_lattice(
    work: np.ndarray, response: np.ndarray, mask: np.ndarray, k: int, scale: float
) -> GridCandidate | None:
    """Lattice grown from line crossings; None when fewer than `MIN_INLIERS` form one."""
    pts = _refined_candidates(response, mask, _crossing_candidates(mask, k))
    if len(pts) < MIN_INLIERS:
        return None

    short_side = min(work.shape)
    best: dict[tuple[int, int], int] = {}
    best_extent = 0.0
    explored: set[int] = set()
    for seed_h, seed_points in _ranked_seed_hypotheses(
        pts, _MIN_SPACING_FRACTION * short_side, _MAX_SPACING_FRACTION * short_side
    ):
        if len(seed_points & explored) >= _SEED_OVERLAP_SKIP * len(seed_points):
            continue
        grown = _propagate(seed_h, pts)
        explored.update(grown.values())
        extent = _lattice_extent(grown, pts)
        if extent > best_extent:
            best, best_extent = grown, extent
    if len(best) < MIN_INLIERS:
        return None

    cells = list(best)
    crossings = pts[[best[c] for c in cells]]
    return _assemble(crossings, np.array(cells, dtype=int), scale)


def _line_lattice(
    response: np.ndarray, mask: np.ndarray, k: int, scale: float
) -> GridCandidate | None:
    """Lattice from partial thin-line evidence; `rms_residual_in` includes the lines' straightness."""
    found = find_line_lattice(mask, response, k)
    if found is None:
        return None
    candidate = _assemble(found.crossings_px, found.lattice_idx, scale, min_span=2)
    if candidate is None:
        return None
    straightness_in = found.straightness_px / found.unit_px
    rms = max(candidate.rms_residual_in, straightness_in)
    penalty = (1 + (candidate.rms_residual_in / NON_PLANAR_RMS_IN) ** 2) / (
        1 + (rms / NON_PLANAR_RMS_IN) ** 2
    )
    return GridCandidate(
        intersections_px=candidate.intersections_px,
        lattice_idx=candidate.lattice_idx,
        h_px_to_plane=candidate.h_px_to_plane,
        rms_residual_in=rms,
        score=float(candidate.score * penalty),
        from_partial_lines=True,
    )


def _assemble(
    px_work: np.ndarray, idx: np.ndarray, scale: float, min_span: int = MIN_LATTICE_SPAN
) -> GridCandidate | None:
    """Build the candidate from work-scale crossings and lattice indices; None for a degenerate fit.

    Needs at least `min_span` distinct lattice indices along each axis.
    """
    px = (px_work + 0.5) / scale - 0.5
    h_guess, _ = cv2.findHomography(px, idx.astype(np.float64), 0)
    if h_guess is None or not np.all(np.isfinite(h_guess)) or abs(np.linalg.det(h_guess)) < 1e-18:
        return None
    labels = _image_aligned_labels(h_guess, idx, px)
    labels = labels - labels.min(axis=0)
    if len(np.unique(labels[:, 0])) < min_span or len(np.unique(labels[:, 1])) < min_span:
        return None

    try:
        h = homography_from_points(px, labels.astype(np.float64))
    except ValueError:
        return None
    residual = np.linalg.norm(apply_h(h, px) - labels, axis=1)
    rms = float(np.sqrt(np.mean(residual**2)))
    spanned = (np.ptp(labels[:, 0]) + 1) * (np.ptp(labels[:, 1]) + 1)
    coverage = min(1.0, len(px) / spanned)
    return GridCandidate(
        intersections_px=px,
        lattice_idx=labels.astype(int),
        h_px_to_plane=h,
        rms_residual_in=rms,
        score=float(coverage / (1.0 + (rms / NON_PLANAR_RMS_IN) ** 2)),
    )


# ------------------------------------------------------------------ manual fallback and result


def grid_from_manual(
    corners_px: list[tuple[float, float]] | np.ndarray, squares_x: int, squares_y: int
) -> GridCandidate:
    """Candidate from four clicked grid crossings (TL, TR, BR, BL as displayed) spanning the given squares.

    `rms_residual_in` is 0.0 and `score` is 1.0. Raises ValueError for the wrong point count, non-finite
    or non-convex or wrongly ordered corners, or squares that are not integers from 1 to 100.
    """
    corners = np.asarray(corners_px, dtype=np.float64)
    if corners.shape != (4, 2) or not np.all(np.isfinite(corners)):
        raise ValueError("corners_px must be four finite (x, y) points")
    for n in (squares_x, squares_y):
        if (
            not isinstance(n, int | np.integer)
            or isinstance(n, bool)
            or not 1 <= n <= _MANUAL_MAX_SQUARES
        ):
            raise ValueError(f"squares must be integers from 1 to {_MANUAL_MAX_SQUARES}")
    edges = np.roll(corners, -1, axis=0) - corners
    turns = edges[:, 0] * np.roll(edges[:, 1], -1) - edges[:, 1] * np.roll(edges[:, 0], -1)
    if not np.all(turns > 0):
        raise ValueError("corners must be a convex quadrilateral in the order TL, TR, BR, BL")
    plane = np.array(
        [[0, 0], [squares_x, 0], [squares_x, squares_y], [0, squares_y]], dtype=np.float64
    )
    h = homography_from_points(corners, plane)
    return GridCandidate(
        intersections_px=corners,
        lattice_idx=plane.astype(int),
        h_px_to_plane=h,
        rms_residual_in=0.0,
        score=1.0,
    )


def reference_from_grid(g: GridCandidate, method: Literal["auto", "manual"]) -> ReferenceResult:
    """Wrap a candidate as a `ReferenceResult` of kind `GRID_1IN`.

    Adds `NON_PLANAR_WARNING` unless `rms_residual_in <= NON_PLANAR_RMS_IN`, and
    `PARTIAL_GRID_WARNING` for a lattice found from partial lines. Raises ValueError if `method` is
    not 'auto' or 'manual'.
    """
    if method not in ("auto", "manual"):
        raise ValueError("method must be 'auto' or 'manual'")
    warnings = ()
    if not g.rms_residual_in <= NON_PLANAR_RMS_IN:
        warnings = (NON_PLANAR_WARNING,)
    if g.from_partial_lines:
        warnings = (*warnings, PARTIAL_GRID_WARNING)
    h = tuple(tuple(float(v) for v in row) for row in g.h_px_to_plane)
    return ReferenceResult(
        kind=ReferenceKind.GRID_1IN,
        method=method,
        h_px_to_plane=h,
        axis_ratio=None,
        warnings=warnings,
    )
