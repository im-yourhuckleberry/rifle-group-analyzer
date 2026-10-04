"""Aim-point proposals: ranked marks found near a group's region of interest.

`propose_aims` looks for the things a shooter aims at (a paster or dot, a diamond, a cross or X,
a printed grid crossing) in a window around the group's ROI and returns up to five ranked
candidates in working px. Nothing here confirms an aim; the caller shows the candidates and the
user accepts or places one. Marks are found by colour: scale-space blob seeds are grown into
their own-colour region, which is classified as round, a quadrilateral with near-equal diagonals,
or a plus/X, and thin crossing strokes and printed lattices are found from stroke masks. Only the
public ellipse helpers of `reference_circle` are used for round marks.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np

from rga.vision.reference_circle import circle_from_edge_points, refine_circle_near

RoiPx = tuple[int, int, int, int]
AimKind = Literal[
    "paster_center", "dot_center", "diamond_center", "cross_center", "grid_intersection"
]

MAX_CANDIDATES = 5
SEARCH_PAD_IN = 1.6
CONTEXT_PAD_IN = 1.6
WORK_PPI = 100.0
SEED_PPI = 50.0
SEED_SIGMA_PX = 5.0
SEED_RADII_IN = (0.15, 0.22, 0.32, 0.46, 0.66, 0.95, 1.4)
SEEDS_PER_SCALE = 6
MIN_SEED_RESPONSE = 10.0
MIN_CONTRAST = 28.0
MIN_ROUND_DIAMETER_IN = 0.40
MIN_DIAMOND_DIAGONAL_IN = 0.28
MIN_DIAMOND_BULLETS = 1.4
MAX_MARK_DIAMETER_IN = 3.6
DARK_L = 70.0
ROUND_TIGHT_ERROR = 0.03
ROUND_LOOSE_ERROR = 0.06
MIN_QUAD_SUPPORT = 0.6
MIN_QUAD_SOLIDITY = 0.6
TOLERANCE_FRACTION = 0.5
TOLERANCE_MAX = 40.0
DARK_TOLERANCE_FRACTION = 0.25
DARK_TOLERANCE_MAX = 22.0
HOLE_FILL_IN = 0.34
PATCH_MIN_HALF_IN = 0.9
PATCH_MAX_HALF_IN = 2.1
MARK_OPEN_IN = 0.05
MAX_STROKE_IN = 0.07
STROKE_PPI = 45.0
STROKE_RESPONSE_MIN = 12.0
CROSS_ANGLES = 12
ARM_NEAR_IN = 0.22
ARM_FAR_IN = 0.4
ARM_CONTINUE_FROM_IN = 0.9
ARM_CONTINUE_TO_IN = 1.25
MIN_OPEN_ENDS = 3
MIN_GRID_PITCH_IN = 0.4
MAX_GRID_PITCH_IN = 2.6
GRID_REACH_IN = 2.3
BIG_BLOB_IN = 0.4
REFINE_CLEARANCE_IN = 0.07
GRID_SCORE = 0.3
STRONG_MARK_SCORE = 0.5
PROXIMITY_SCALE_IN = 0.8
DEDUPE_IN = 0.12
SNAP_LIMIT = 0.15
SMALL_MARK_BULLETS = 3.0
SMALL_DIAMOND_IN = 0.5
MIN_SMALL_DIAMOND_FACTOR = 0.55
REFERENCE_SCORE = 1.0
GENERIC_SCORE_CAP = 0.99


@dataclass(frozen=True)
class AimCandidate:
    """One proposed aim point: `point_px` in working px, `score` in (0, 1], `kind` of mark."""

    kind: AimKind
    point_px: tuple[float, float]
    score: float


def propose_aims(
    working_img: np.ndarray,
    roi: RoiPx,
    ppi: float,
    reference_center_px: tuple[float, float] | None = None,
    max_candidates: int = MAX_CANDIDATES,
    bullet_diameter_in: float | None = None,
    shots_px: Sequence[tuple[float, float]] = (),
) -> list[AimCandidate]:
    """Return up to `max_candidates` ranked aim candidates near `roi` of the working image.

    `roi` is (x0, y0, x1, y1) in working px, `ppi` the working scale. Marks are searched within
    1.6 in of the ROI; the score is the mark's shape and contrast quality, lowered with distance
    from the ROI. A paster or dot is the centre of a fitted ellipse, a diamond the centre of a
    fitted near-square quadrilateral, a cross the crossing of its two strokes or arms, and grid
    crossings (the two nearest the ROI centre) are offered only when no mark scores above 0.5.
    `reference_center_px`, the centre of a confirmed circle reference, is the top candidate when
    it lies within the search window. With `bullet_diameter_in` a diamond must be at least 1.4
    times that long on its longer diagonal, and a hole-sized candidate (under three bullet
    diameters across, or a grid crossing) within one bullet diameter of a point of `shots_px`
    (detected or proposed shots, working px) is dropped, so a bullet hole is not taken for an
    aim; a larger mark with a hit near its centre stays, and the reference centre is exempt. The
    list is sorted by score and may be empty. Raises ValueError for a bad image, an empty ROI, a
    non-positive `ppi`,
    `max_candidates < 1`, or an image too small or degenerate for OpenCV to analyse.
    """
    if working_img.ndim != 3 or working_img.shape[2] != 3 or working_img.size == 0:
        raise ValueError("working_img must be a non-empty BGR image")
    if not (math.isfinite(ppi) and ppi > 0):
        raise ValueError("ppi must be positive")
    if max_candidates < 1:
        raise ValueError("max_candidates must be at least 1")
    height, width = working_img.shape[:2]
    x0, y0, x1, y1 = (int(v) for v in roi)
    roi = (max(0, x0), max(0, y0), min(width, x1), min(height, y1))
    if roi[2] <= roi[0] or roi[3] <= roi[1]:
        raise ValueError("roi must be (x0, y0, x1, y1) with x1 > x0 and y1 > y0, inside the image")
    min_diamond = MIN_DIAMOND_DIAGONAL_IN
    if bullet_diameter_in is not None and bullet_diameter_in > 0:
        min_diamond = max(min_diamond, MIN_DIAMOND_BULLETS * bullet_diameter_in)
    bullet_in = (bullet_diameter_in or 0.0) if len(shots_px) else 0.0
    try:
        return _search(
            working_img,
            roi,
            ppi,
            reference_center_px,
            max_candidates,
            min_diamond,
            shots_px,
            bullet_in,
        )
    except cv2.error as error:
        raise ValueError(f"the image cannot be analysed for aim marks: {error}") from error


def _search(
    working_img, roi, ppi, reference_center_px, max_candidates, min_diamond, shots_px, bullet_in
):
    window = _box(roi, working_img.shape, SEARCH_PAD_IN * ppi)
    context = _box(roi, working_img.shape, (SEARCH_PAD_IN + CONTEXT_PAD_IN) * ppi)
    scale = min(1.0, WORK_PPI / ppi)
    crop = working_img[context[1] : context[3], context[0] : context[2]]
    if scale < 1.0:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    lab = cv2.cvtColor(cv2.GaussianBlur(crop, (0, 0), 0.8), cv2.COLOR_BGR2LAB).astype(np.float32)
    sppi = ppi * scale

    def to_working(point: tuple[float, float]) -> tuple[float, float]:
        return (
            (point[0] + 0.5) / scale - 0.5 + context[0],
            (point[1] + 0.5) / scale - 0.5 + context[1],
        )

    def in_window(point: tuple[float, float]) -> bool:
        x, y = to_working(point)
        return window[0] <= x < window[2] and window[1] <= y < window[3]

    marks = [m for m in _mark_candidates(lab, crop, sppi, min_diamond) if in_window(m.point_px)]
    thin = [m for m in _thin_cross_candidates(lab, sppi) if in_window(m.point_px)]
    marks += thin
    roi_centre = ((roi[0] + roi[2]) / 2, (roi[1] + roi[3]) / 2)
    near = (
        (roi_centre[0] - context[0]) * scale - 0.5 * (1 - scale),
        (roi_centre[1] - context[1]) * scale - 0.5 * (1 - scale),
    )
    lattice = _lattice_points(lab, sppi, near)
    marks = [
        m
        for m in marks
        if m.kind != "cross_center"
        or all(
            math.hypot(m.point_px[0] - gx, m.point_px[1] - gy) > 0.1 * sppi
            for gx, gy, _, _ in lattice
        )
    ]
    found = []
    for m in marks:
        point = to_working(m.point_px)
        hole_sized = m.size_px / sppi < SMALL_MARK_BULLETS * bullet_in
        if hole_sized and not _clear_of_shots(point, shots_px, bullet_in * ppi):
            continue
        quality = m.score
        if m.kind == "cross_center" and m in thin:
            refined = _refined_at_full_resolution(
                working_img,
                point,
                ppi,
                lambda lab, centre, scale, angle=m.angle: _refine_cross(lab, centre, angle, scale),
            )
            point, quality = (point, 0.8 * quality) if refined is None else (refined, quality)
        score = min(GENERIC_SCORE_CAP, quality * _proximity(point, roi, ppi))
        found.append(AimCandidate(m.kind, point, score))
    found.sort(key=lambda c: -c.score)
    kept: list[AimCandidate] = []
    if reference_center_px is not None and _inside(reference_center_px, window):
        kept.append(AimCandidate("paster_center", reference_center_px, REFERENCE_SCORE))
    for candidate in found:
        if all(_distance(candidate.point_px, k.point_px) > DEDUPE_IN * ppi for k in kept):
            kept.append(candidate)
    if not any(c.score >= STRONG_MARK_SCORE for c in kept):
        grid = _grid_candidates(working_img, lattice, near, to_working, window, kept, ppi)
        kept.extend(c for c in grid if _clear_of_shots(c.point_px, shots_px, bullet_in * ppi))
    return kept[:max_candidates]


def _box(roi: RoiPx, shape: tuple[int, ...], pad: float) -> tuple[int, int, int, int]:
    pad_px = int(round(pad))
    height, width = shape[:2]
    return (
        max(0, roi[0] - pad_px),
        max(0, roi[1] - pad_px),
        min(width, roi[2] + pad_px),
        min(height, roi[3] + pad_px),
    )


def _clear_of_shots(point, shots_px, reach: float) -> bool:
    return reach <= 0 or all(_distance(point, shot) >= reach for shot in shots_px)


def _inside(point: tuple[float, float], box: tuple[int, int, int, int]) -> bool:
    return box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3]


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _proximity(point: tuple[float, float], roi: RoiPx, ppi: float) -> float:
    dx = max(roi[0] - point[0], 0.0, point[0] - roi[2])
    dy = max(roi[1] - point[1], 0.0, point[1] - roi[3])
    return 1.0 / (1.0 + (math.hypot(dx, dy) / ppi / PROXIMITY_SCALE_IN) ** 2)


@dataclass(frozen=True)
class _Mark:
    kind: AimKind
    point_px: tuple[float, float]
    score: float
    angle: float = 0.0
    size_px: float = 0.0


def _grid_candidates(working, lattice, near, to_working, window, kept, ppi) -> list[AimCandidate]:
    ranked = sorted(lattice, key=lambda g: math.hypot(g[0] - near[0], g[1] - near[1]))
    out: list[AimCandidate] = []
    for gx, gy, t1, t2 in ranked:
        coarse = to_working((gx, gy))
        if not _inside(coarse, window):
            continue
        point = (
            _refined_at_full_resolution(
                working,
                coarse,
                ppi,
                lambda lab, centre, scale, thetas=(t1, t2): _refine_junction(
                    lab, centre, thetas, scale
                ),
            )
            or coarse
        )
        if all(_distance(point, k.point_px) > DEDUPE_IN * ppi for k in [*kept, *out]):
            out.append(AimCandidate("grid_intersection", point, GRID_SCORE))
        if len(out) == 2:
            break
    return out


def _refined_at_full_resolution(working, point, ppi, refine) -> tuple[float, float] | None:
    """`refine(lab_patch, local_point, ppi)` on a working-resolution patch around `point`."""
    reach = int(round(0.8 * ppi))
    height, width = working.shape[:2]
    x0, y0 = max(0, int(point[0]) - reach), max(0, int(point[1]) - reach)
    x1, y1 = min(width, int(point[0]) + reach + 1), min(height, int(point[1]) + reach + 1)
    patch = cv2.cvtColor(cv2.GaussianBlur(working[y0:y1, x0:x1], (0, 0), 0.8), cv2.COLOR_BGR2LAB)
    refined = refine(patch.astype(np.float32), (point[0] - x0, point[1] - y0), ppi)
    return None if refined is None else (refined[0] + x0, refined[1] + y0)


# --------------------------------------------------------------------------- solid marks


def _mark_candidates(
    lab: np.ndarray, bgr: np.ndarray, ppi: float, min_diamond: float
) -> list[_Mark]:
    sample = lab[:: max(1, lab.shape[0] // 120), :: max(1, lab.shape[1] // 120)].reshape(-1, 3)
    background = _dominant_color(sample, step=14.0)
    out: list[_Mark] = []
    for sx, sy, radius_in in _seeds(lab, ppi):
        grown = _grow_in(lab, sx, sy, radius_in * ppi, ppi, background)
        if grown is None:
            continue
        filled, contrast, mark_color, (gx, gy) = grown
        found = _classify(filled, contrast, mark_color, ppi, min_diamond)
        if found is None:
            continue
        kind, (cx, cy), shape_score, radius_px, size_px = found
        cx, cy = cx + gx, cy + gy
        if kind in ("paster_center", "dot_center"):
            snapped = refine_circle_near(bgr, (cx, cy), 0.6 * radius_px)
            if (
                snapped is not None
                and _distance(snapped.center_px, (cx, cy)) < SNAP_LIMIT * radius_px
            ):
                cx, cy = snapped.center_px
        out.append(_Mark(kind, (cx, cy), shape_score * min(1.0, contrast / 50.0), 0.0, size_px))
    return out


def _seeds(lab: np.ndarray, ppi: float) -> list[tuple[float, float, float]]:
    """Scale-space colour blob maxima as (x, y, radius_in) in crop px."""
    scale = min(1.0, SEED_PPI / ppi)
    small = cv2.resize(lab, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    sppi = ppi * scale
    height, width = small.shape[:2]
    layers = []
    for radius in SEED_RADII_IN:
        sigma = max(0.8, radius * sppi / 1.4)
        shrink = min(1.0, SEED_SIGMA_PX / sigma)
        level = (
            small
            if shrink >= 1.0
            else cv2.resize(small, None, fx=shrink, fy=shrink, interpolation=cv2.INTER_AREA)
        )
        sigma *= shrink
        response = np.linalg.norm(
            cv2.GaussianBlur(level, (0, 0), sigma) - cv2.GaussianBlur(level, (0, 0), sigma * 1.8),
            axis=2,
        )
        layers.append(
            response
            if shrink >= 1.0
            else cv2.resize(response, (width, height), interpolation=cv2.INTER_LINEAR)
        )
    stack = np.stack(layers)
    kernel = np.ones((3, 3), np.uint8)
    grown = [cv2.dilate(layer, kernel) for layer in stack]
    seeds: list[tuple[float, float, float]] = []
    for level, radius in enumerate(SEED_RADII_IN):
        ok = (stack[level] >= grown[level]) & (stack[level] > MIN_SEED_RESPONSE)
        for other in (level - 1, level + 1):
            if 0 <= other < len(SEED_RADII_IN):
                ok &= stack[level] >= grown[other]
        ys, xs = np.nonzero(ok)
        strength = stack[level][ys, xs]
        best = (
            np.argsort(-strength)[:SEEDS_PER_SCALE]
            if len(strength) <= 4 * SEEDS_PER_SCALE
            else np.argpartition(-strength, SEEDS_PER_SCALE)[:SEEDS_PER_SCALE]
        )
        seeds.extend(
            ((xs[k] + 0.5) / scale - 0.5, (ys[k] + 0.5) / scale - 0.5, radius) for k in best
        )
    return seeds


def _dominant_color(pixels: np.ndarray, step: float = 10.0) -> np.ndarray:
    """Mean colour around the most populated neighbourhood of colour cells of `pixels`."""
    if len(pixels) > 4000:
        pixels = pixels[:: len(pixels) // 4000]
    cells = np.floor(pixels / step).astype(np.int32)
    keys, inverse, counts = np.unique(cells, axis=0, return_inverse=True, return_counts=True)
    reach = (np.abs(keys[:, None, :] - keys[None, :, :]).max(axis=2) <= 1).astype(np.int64)
    best = int(np.argmax(reach @ counts))
    return pixels[(reach[best] > 0)[inverse.ravel()]].mean(axis=0)


def _disc(shape: tuple[int, ...], cx: float, cy: float, radius: float) -> np.ndarray:
    mask = np.zeros(shape[:2], np.uint8)
    cv2.circle(mask, (int(round(cx)), int(round(cy))), max(1, int(round(radius))), 1, -1)
    return mask.astype(bool)


def _grow_in(lab, x, y, radius_px, ppi, background):
    """`_grow` on the patch around the seed; the patch origin is returned with the result.

    A region reaching the patch edge (where the patch is not also the crop's edge) is cut off and
    may be part of a bigger mark, so it is grown again in a larger patch and dropped when it still
    does not fit.
    """
    halves = (max(3.0 * radius_px + 0.5 * ppi, PATCH_MIN_HALF_IN * ppi), PATCH_MAX_HALF_IN * ppi)
    for half_float in halves:
        half = int(min(max(lab.shape), half_float))
        px0, py0 = max(0, int(x) - half), max(0, int(y) - half)
        px1, py1 = min(lab.shape[1], int(x) + half + 1), min(lab.shape[0], int(y) + half + 1)
        grown = _grow(
            lab[py0:py1, px0:px1],
            x - px0,
            y - py0,
            radius_px,
            background,
            int(round(MARK_OPEN_IN * ppi)) | 1,
            int(round((HOLE_FILL_IN * ppi) ** 2)),
        )
        if grown is None:
            return None
        region = grown[0]
        cut = (
            (px0 > 0 and region[:, 0].any())
            or (py0 > 0 and region[0, :].any())
            or (px1 < lab.shape[1] and region[:, -1].any())
            or (py1 < lab.shape[0] and region[-1, :].any())
        )
        if not cut:
            return (*grown, (px0, py0))
    return None


def _grow(lab, x, y, radius_px, background, open_px, hole_px):
    """Own-colour region around a seed as (filled mask, contrast, colour), or None."""
    inner = _disc(lab.shape, x, y, 0.8 * radius_px)
    ring = _disc(lab.shape, x, y, 2.4 * radius_px) & ~_disc(lab.shape, x, y, 1.5 * radius_px)
    if inner.sum() < 4 or ring.sum() < 8:
        return None
    mark = _dominant_color(lab[inner])
    around = _dominant_color(lab[ring])
    contrast = float(np.linalg.norm(mark - around))
    if contrast < MIN_CONTRAST or float(np.linalg.norm(mark - background)) < MIN_CONTRAST:
        return None
    if mark[0] < DARK_L:
        tolerance = float(np.clip(DARK_TOLERANCE_FRACTION * contrast, 12.0, DARK_TOLERANCE_MAX))
    else:
        tolerance = float(np.clip(TOLERANCE_FRACTION * contrast, 12.0, TOLERANCE_MAX))
    mask = (np.linalg.norm(lab - mark, axis=2) < tolerance).astype(np.uint8)
    mask = _fill_small_holes(mask, hole_px)
    size = max(open_px, int(round(0.04 * radius_px))) | 1
    sturdy = cv2.morphologyEx(
        mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    )
    region = _component_at(sturdy, inner)
    return None if region is None else (region, contrast, mark)


def _fill_small_holes(mask: np.ndarray, max_area: int) -> np.ndarray:
    """`mask` with enclosed gaps smaller than `max_area` px filled (bullet holes, tiny dots)."""
    if max_area <= 0:
        return mask
    count, labels, stats, _ = cv2.connectedComponentsWithStats(1 - mask, connectivity=4)
    out = mask.copy()
    height, width = mask.shape
    for i in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[i])
        touches = x == 0 or y == 0 or x + w >= width or y + h >= height
        if not touches and area < max_area:
            out[labels == i] = 1
    return out


def _component_at(mask: np.ndarray, inner: np.ndarray) -> np.ndarray | None:
    """The component of `mask` overlapping `inner` most, with enclosed holes filled."""
    count, labels = cv2.connectedComponents(mask, connectivity=8)
    if count < 2:
        return None
    overlap = np.bincount(labels[inner], minlength=count)
    overlap[0] = 0
    best = int(np.argmax(overlap))
    if overlap[best] == 0:
        return None
    component = (labels == best).astype(np.uint8)
    contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    filled = np.zeros_like(component)
    cv2.drawContours(filled, contours, -1, 1, cv2.FILLED)
    return filled


def _classify(
    filled: np.ndarray, contrast: float, mark_color: np.ndarray, ppi: float, min_diamond: float
):
    """(kind, centre, shape score, radius px, size px) of a grown region, or None."""
    contours, _ = cv2.findContours(filled, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    area = float(cv2.contourArea(contour))
    hull = cv2.convexHull(contour)
    hull_area = float(cv2.contourArea(hull))
    if hull_area < 1:
        return None
    solidity = area / hull_area
    diameter_in = 2 * math.sqrt(area / math.pi) / ppi
    if diameter_in > MAX_MARK_DIAMETER_IN:
        return None
    if solidity < 0.7:
        cross = _cross_center(filled, ppi)
        if cross is not None:
            return "cross_center", cross[0], 0.5 + 0.5 * cross[1], 0.0, math.inf
        if solidity < MIN_QUAD_SOLIDITY:
            return None
    hull_points = hull[:, 0, :].astype(np.float64)
    dense = _densify(hull_points)
    ellipse, ratio, error = _fit_round(dense)
    roundish = solidity >= 0.7 and diameter_in >= MIN_ROUND_DIAMETER_IN and ellipse is not None
    if roundish and ratio > 0.6 and error < ROUND_TIGHT_ERROR:
        return _round_mark(ellipse, ratio, error, solidity, mark_color)
    approx = _approx_quad(hull, hull_area)
    if approx is not None:
        quad = _quad_center(contour[:, 0, :].astype(np.float64), approx)
        if quad is not None:
            centre, d1, d2, sides, corners = quad
            diagonal_ratio = min(d1, d2) / max(d1, d2)
            side_ratio = min(sides) / max(sides)
            supported = _outline_support(dense, corners) >= MIN_QUAD_SUPPORT
            long_enough = max(d1, d2) / ppi >= min_diamond
            if diagonal_ratio > 0.82 and side_ratio > 0.7 and long_enough and supported:
                size = min(1.0, max(MIN_SMALL_DIAMOND_FACTOR, max(d1, d2) / ppi / SMALL_DIAMOND_IN))
                score = (0.6 * diagonal_ratio + 0.4 * side_ratio) * size
                return (
                    "diamond_center",
                    (float(centre[0]), float(centre[1])),
                    score,
                    0.0,
                    max(d1, d2),
                )
    if roundish and ratio > 0.6 and error < ROUND_LOOSE_ERROR:
        return _round_mark(ellipse, ratio, error, solidity, mark_color)
    return None


def _fit_round(dense: np.ndarray):
    """(ellipse, axis ratio, relative fit error) of the outline `dense`, or (None, 0, 1)."""
    try:
        ellipse = circle_from_edge_points([tuple(p) for p in dense[:: max(1, len(dense) // 400)]])
    except ValueError:
        return None, 0.0, 1.0
    return ellipse, min(ellipse.axes_px) / max(ellipse.axes_px), _round_fit_error(dense, ellipse)


def _round_mark(ellipse, ratio, error, solidity, mark_color):
    kind = "dot_center" if mark_color[0] < DARK_L else "paster_center"
    score = min(1.0, solidity + 0.3) * ratio * (1 - error / 0.1)
    return (
        kind,
        ellipse.center_px,
        score,
        float(sum(ellipse.axes_px) / 4),
        float(sum(ellipse.axes_px) / 2),
    )


def _outline_support(outline: np.ndarray, corners: np.ndarray) -> float:
    """Fraction of `outline` points lying on the boundary of the polygon `corners`."""
    polygon = corners.astype(np.float32).reshape(-1, 1, 2)
    tolerance = 0.03 * float(np.hypot(*(corners[2] - corners[0]))) + 1.5
    step = max(1, len(outline) // 200)
    close = [
        abs(cv2.pointPolygonTest(polygon, (float(x), float(y)), True)) <= tolerance
        for x, y in outline[::step]
    ]
    return float(np.mean(close))


def _densify(polygon: np.ndarray, step: float = 2.0) -> np.ndarray:
    out = []
    for a, b in zip(polygon, np.roll(polygon, -1, axis=0), strict=True):
        n = max(1, int(np.hypot(*(b - a)) / step))
        out.append(a + (b - a) * (np.arange(n)[:, None] / n))
    return np.vstack(out)


def _round_fit_error(points: np.ndarray, ellipse) -> float:
    """Relative RMS deviation of `points` from `ellipse` along its normalised radius."""
    a, b = ellipse.axes_px[0] / 2, ellipse.axes_px[1] / 2
    theta = math.radians(ellipse.angle_deg)
    d = points - np.array(ellipse.center_px)
    u = d[:, 0] * math.cos(theta) + d[:, 1] * math.sin(theta)
    v = -d[:, 0] * math.sin(theta) + d[:, 1] * math.cos(theta)
    radius = np.sqrt((u / a) ** 2 + (v / b) ** 2)
    return float(np.sqrt(np.mean((radius - 1.0) ** 2)))


def _approx_quad(hull: np.ndarray, hull_area: float):
    """The hull simplified to four vertices that still cover most of it, or None."""
    perimeter = cv2.arcLength(hull, True)
    for eps in (0.03, 0.04, 0.05, 0.065, 0.08, 0.10):
        approx = cv2.approxPolyDP(hull, eps * perimeter, True)
        if len(approx) < 4:
            break
        if len(approx) == 4:
            if cv2.contourArea(approx) >= 0.88 * hull_area:
                return approx
            break
    return _quad_from_long_edges(hull, perimeter)


def _quad_from_long_edges(hull: np.ndarray, perimeter: float):
    """Quadrilateral bounded by the four longest straight hull edges (ignores small bulges)."""
    poly = cv2.approxPolyDP(hull, 0.012 * perimeter, True).reshape(-1, 2).astype(np.float64)
    if len(poly) < 4:
        return None
    following = np.roll(poly, -1, axis=0)
    lengths = np.hypot(*(following - poly).T)
    longest = np.argsort(-lengths)[:4]
    if lengths[longest].sum() < 0.6 * perimeter:
        return None
    centre = poly.mean(axis=0)
    edges = sorted(
        longest,
        key=lambda i: math.atan2(*((poly[i] + following[i]) / 2 - centre)[::-1]),
    )
    lines = [(poly[i], (following[i] - poly[i]) / lengths[i]) for i in edges]
    verts = []
    for k in range(4):
        corner = _intersect(lines[k], lines[(k + 1) % 4], 0.25)
        if corner is None:
            return None
        verts.append(corner)
    quad = np.array(verts, dtype=np.float32).reshape(-1, 1, 2)
    return quad.astype(np.int32) if cv2.isContourConvex(quad) else None


def _intersect(line_a, line_b, min_det: float = 1e-3):
    (p, d), (q, e) = line_a, line_b
    m = np.array([[d[0], -e[0]], [d[1], -e[1]]])
    if abs(np.linalg.det(m)) < min_det:
        return None
    s, _ = np.linalg.solve(m, q - p)
    return p + s * d


def _quad_center(points: np.ndarray, approx: np.ndarray):
    """Centre, diagonals, sides and corners of the parallelogram fitted to the outline `points`.

    Opposite sides share one direction fitted to both of them, and the side lines are re-fitted a
    few times to the outline points close to them, so bulges (bullet holes touching the mark)
    and bites do not pull the sides.
    """
    quad = approx.reshape(-1, 2).astype(np.float64)
    centre = quad.mean(axis=0)
    verts = quad[np.argsort(np.arctan2(quad[:, 1] - centre[1], quad[:, 0] - centre[0]))]
    result = None
    for iteration in range(4):
        fitted = _fit_parallelogram(points, verts, iteration)
        if fitted is None:
            break
        centre, verts = fitted
        diagonal_a, diagonal_b = verts[2] - verts[0], verts[3] - verts[1]
        lengths = [float(np.hypot(*(verts[(i + 1) % 4] - verts[i]))) for i in range(4)]
        result = (
            centre,
            float(np.hypot(*diagonal_a)),
            float(np.hypot(*diagonal_b)),
            lengths,
            verts,
        )
    return result


def _fit_parallelogram(points: np.ndarray, verts: np.ndarray, iteration: int):
    """One refit of the parallelogram to the outline points near the current sides."""
    sides = []
    for i in range(4):
        a, b = verts[i], verts[(i + 1) % 4]
        segment = b - a
        length = float(np.hypot(*segment))
        if length < 3:
            return None
        tolerance = 0.12 * length + 2 if iteration == 0 else max(1.5, 0.02 * length)
        rel = points - a
        along = (rel @ segment) / length**2
        across = np.abs((segment[0] * rel[:, 1] - segment[1] * rel[:, 0]) / length)
        chosen = points[(along > 0.12) & (along < 0.88) & (across < tolerance)]
        if len(chosen) < 8:
            return None
        sides.append(chosen)
    fits = []
    for pair in ((0, 2), (1, 3)):
        pooled = np.vstack([sides[k] - sides[k].mean(axis=0) for k in pair])
        direction = np.linalg.svd(pooled, full_matrices=False)[2][0]
        normal = np.array([-direction[1], direction[0]])
        fits.append((normal, [float(sides[k].mean(axis=0) @ normal) for k in pair]))
    (n_a, off_a), (n_b, off_b) = fits
    if abs(n_a[0] * n_b[1] - n_a[1] * n_b[0]) < 0.25:
        return None
    basis = np.array([n_a, n_b])
    centre = np.linalg.solve(basis, np.array([sum(off_a) / 2, sum(off_b) / 2]))
    corners = np.array(
        [
            np.linalg.solve(basis, np.array([off_a[ia], off_b[ib]]))
            for ia, ib in ((0, 0), (0, 1), (1, 1), (1, 0))
        ]
    )
    corners = corners[np.argsort(np.arctan2(corners[:, 1] - centre[1], corners[:, 0] - centre[0]))]
    return centre, corners


def _cross_center(filled: np.ndarray, ppi: float):
    """(centre, regularity) of a plus-, X- or T-shaped solid mark such as a tape cross, or None."""
    dist = cv2.distanceTransform(filled, cv2.DIST_L2, 5)
    peak = float(dist.max())
    if peak < 0.04 * ppi:
        return None
    ys, xs = np.nonzero(dist >= 0.9 * peak)
    centre = np.array([np.average(xs, weights=dist[ys, xs]), np.average(ys, weights=dist[ys, xs])])
    pys, pxs = np.nonzero(filled)
    reach = float(np.hypot(pxs - centre[0], pys - centre[1]).max())
    if reach < 2.4 * peak or reach > 2.2 * ppi:
        return None
    angles = np.linspace(0, 2 * np.pi, 360, endpoint=False)
    found = None
    for factor in (0.7, 0.55, 0.85, 0.45):
        rho = factor * reach
        sx = np.clip(np.rint(centre[0] + rho * np.cos(angles)).astype(int), 0, filled.shape[1] - 1)
        sy = np.clip(np.rint(centre[1] + rho * np.sin(angles)).astype(int), 0, filled.shape[0] - 1)
        ring = filled[sy, sx] > 0
        if ring.all() or not ring.any():
            continue
        starts = np.nonzero(ring & ~np.roll(ring, 1))[0]
        if len(starts) not in (3, 4):
            continue
        gaps = np.diff(np.concatenate([starts, [starts[0] + 360]]))
        if len(starts) == 4 and (gaps.min() < 55 or gaps.max() > 125):
            continue
        if len(starts) == 3 and not (145 <= gaps.max() <= 215 and gaps.min() >= 55):
            continue
        found = (ring, starts, gaps)
        break
    if found is None:
        return None
    ring, starts, gaps = found
    three_arms = len(starts) == 3
    regular = 0.5 if three_arms else max(0.0, 1.0 - (gaps.max() - gaps.min()) / 180.0)
    run_angles = []
    for start in starts:
        length = 0
        while ring[(start + length) % 360]:
            length += 1
        run_angles.append(np.deg2rad(start + (length - 1) / 2.0))
    arm_of = _assign_arms(pxs, pys, centre, run_angles, 1.2 * peak)
    if three_arms:
        stem = (int(np.argmax(gaps)) + 2) % 3
        arm_sets = (tuple(i for i in range(3) if i != stem), (stem,))
    else:
        arm_sets = ((0, 2), (1, 3))
    lines = []
    for arms in arm_sets:
        chosen = np.isin(arm_of, arms)
        if chosen.sum() < 20:
            return (float(centre[0]), float(centre[1])), regular
        vx, vy, x0, y0 = cv2.fitLine(
            np.column_stack([pxs[chosen], pys[chosen]]).astype(np.float32),
            cv2.DIST_L2,
            0,
            0.01,
            0.01,
        ).ravel()
        lines.append((np.array([x0, y0]), np.array([vx, vy])))
    refined = _intersect(lines[0], lines[1], 0.2)
    if refined is None or np.hypot(*(refined - centre)) > 1.5 * peak:
        refined = centre
    return (float(refined[0]), float(refined[1])), regular


def _assign_arms(pxs, pys, centre, run_angles, min_radius) -> np.ndarray:
    angle = np.arctan2(pys - centre[1], pxs - centre[0])
    diff = np.abs(np.angle(np.exp(1j * (angle[:, None] - np.array(run_angles)[None, :]))))
    arm = np.argmin(diff, axis=1)
    arm[np.hypot(pxs - centre[0], pys - centre[1]) < min_radius] = -1
    return arm


# --------------------------------------------------------------------------- thin strokes


def _line_response(patch: np.ndarray, ppi: float) -> np.ndarray:
    """Per-pixel strength of thin dark or bright strokes (top/black-hat over L, a and b)."""
    size = max(3, int(round((MAX_STROKE_IN + 0.02) * ppi))) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    total = np.zeros(patch.shape[:2], np.float32)
    for c in range(3):
        channel = patch[:, :, c]
        closed = cv2.morphologyEx(channel, cv2.MORPH_CLOSE, kernel)
        opened = cv2.morphologyEx(channel, cv2.MORPH_OPEN, kernel)
        total += np.maximum(closed - channel, channel - opened) ** 2
    return np.sqrt(total)


def _blobs(patch: np.ndarray, ppi: float, grow_in: float = 0.03) -> np.ndarray:
    """0/1 mask of wide marks (bullet holes, dots) grown a little, so strokes can avoid them."""
    sample = patch[:: max(1, patch.shape[0] // 80), :: max(1, patch.shape[1] // 80)]
    sheet = _dominant_color(sample.reshape(-1, 3), step=14.0)
    mask = (np.linalg.norm(patch - sheet, axis=2) > MIN_CONTRAST).astype(np.uint8)
    size = int(round((MAX_STROKE_IN + 0.03) * ppi)) | 1
    wide = cv2.morphologyEx(
        mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    )
    grow = 2 * int(round(grow_in * ppi)) + 1
    return cv2.dilate(wide, np.ones((grow, grow), np.uint8))


def _stroke_mask(lab: np.ndarray, ppi: float) -> tuple[np.ndarray, np.ndarray, float]:
    """Thin strokes and wide blobs of `lab` as float masks at STROKE_PPI, plus the scale."""
    scale = min(1.0, STROKE_PPI / ppi)
    small = cv2.resize(lab, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    sppi = ppi * scale
    strokes = (_line_response(small, sppi) > STROKE_RESPONSE_MIN).astype(np.float32)
    blobs = _blobs(small, sppi)
    return strokes * (1 - blobs), blobs.astype(np.float32), scale


def _hole_sized(blobs: np.ndarray, ppi: float) -> np.ndarray:
    """The part of `blobs` made of hole-sized marks: whatever survives no opening by a big dot."""
    size = int(round(BIG_BLOB_IN * ppi)) | 1
    big = cv2.morphologyEx(
        blobs, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    )
    return blobs * (1 - cv2.dilate(big, np.ones((3, 3), np.uint8)))


def _thin_pixels(patch: np.ndarray, ppi: float) -> np.ndarray:
    response = _line_response(patch, ppi)
    threshold = max(STROKE_RESPONSE_MIN, 0.3 * float(np.percentile(response, 99.5)))
    return (response > threshold) & ~_blobs(patch, ppi, REFINE_CLEARANCE_IN).astype(bool)


def _ray_kernel(angle: float, r0: float, r1: float, size: int) -> np.ndarray:
    kernel = np.zeros((size, size), np.float32)
    c = size // 2
    p0 = (int(round(c + r0 * math.cos(angle))), int(round(c + r0 * math.sin(angle))))
    p1 = (int(round(c + r1 * math.cos(angle))), int(round(c + r1 * math.sin(angle))))
    cv2.line(kernel, p0, p1, 1.0, 1, cv2.LINE_8)
    total = kernel.sum()
    return kernel / total if total else kernel


def _arm_clutter(strokes: np.ndarray, x: int, y: int, arm_angle: float, sppi: float) -> float:
    """Fraction of stroke pixels in the annulus around (x, y) that lie off the four arms."""
    radii = np.arange(0.45 * sppi, 0.9 * sppi, 1.0)
    angles = np.deg2rad(np.arange(0, 360, 2.0))
    rr, aa = np.meshgrid(radii, angles)
    off_arm = np.abs(np.angle(np.exp(4j * (aa - arm_angle)))) / 4 > np.deg2rad(15)
    px = np.clip(np.rint(x + rr * np.cos(aa)).astype(int), 0, strokes.shape[1] - 1)
    py = np.clip(np.rint(y + rr * np.sin(aa)).astype(int), 0, strokes.shape[0] - 1)
    return float(strokes[py, px][off_arm].mean()) if off_arm.any() else 0.0


def _thin_cross_candidates(lab: np.ndarray, ppi: float) -> list[_Mark]:
    """Thin crossing strokes whose four arms end near the crossing, as marks in crop px."""
    strokes, blobs, scale = _stroke_mask(lab, ppi)
    sppi = ppi * scale
    near_mask = np.maximum(strokes, blobs)
    far_mask = np.maximum(strokes, _hole_sized(blobs, sppi))
    size = 2 * (int(math.ceil(ARM_FAR_IN * sppi)) + 2) + 1
    best = np.zeros(strokes.shape, np.float32)
    best_angle = np.zeros(strokes.shape, np.float32)
    for i in range(CROSS_ANGLES):
        base = math.pi / 2 * i / CROSS_ANGLES
        near, far = [], []
        for q in range(4):
            angle = base + q * math.pi / 2
            near_kernel = _ray_kernel(angle, 0.05 * sppi, ARM_NEAR_IN * sppi, size)
            far_kernel = _ray_kernel(angle, ARM_NEAR_IN * sppi, ARM_FAR_IN * sppi, size)
            near.append(cv2.filter2D(near_mask, -1, near_kernel, borderType=cv2.BORDER_CONSTANT))
            far.append(cv2.filter2D(far_mask, -1, far_kernel, borderType=cv2.BORDER_CONSTANT))
        response = np.minimum(
            np.partition(np.stack(near), 1, axis=0)[1], np.partition(np.stack(far), 1, axis=0)[1]
        )
        better = response > best
        best = np.where(better, response, best)
        best_angle = np.where(better, base, best_angle)
    reach = 2 * int(round(0.06 * sppi)) + 1
    loose = cv2.dilate(strokes, np.ones((reach, reach), np.uint8))
    peaks = (best >= cv2.dilate(best, np.ones((7, 7), np.uint8))) & (best > 0.55)
    ys, xs = np.nonzero(peaks)
    out: list[_Mark] = []
    strength = best[ys, xs]
    top = np.argsort(-strength)[:12] if len(strength) <= 48 else np.argpartition(-strength, 12)[:12]
    for k in top:
        y, x = int(ys[k]), int(xs[k])
        open_ends = 0
        for q in range(4):
            angle = float(best_angle[y, x]) + q * math.pi / 2
            radii = np.arange(ARM_CONTINUE_FROM_IN * sppi, ARM_CONTINUE_TO_IN * sppi, 1.0)
            px = np.clip(np.rint(x + radii * math.cos(angle)).astype(int), 0, loose.shape[1] - 1)
            py = np.clip(np.rint(y + radii * math.sin(angle)).astype(int), 0, loose.shape[0] - 1)
            open_ends += int(loose[py, px].mean() < 0.35)
        if open_ends < MIN_OPEN_ENDS:
            continue
        clutter = _arm_clutter(strokes, x, y, float(best_angle[y, x]), sppi)
        score = 0.85 * float(best[y, x]) * max(0.3, 1.0 - clutter / 0.06)
        coarse = ((x + 0.5) / scale - 0.5, (y + 0.5) / scale - 0.5)
        out.append(_Mark("cross_center", coarse, score, float(best_angle[y, x])))
    out.sort(key=lambda m: -m.score)
    return out[:6]


def _refine_cross(
    lab: np.ndarray, centre: tuple[float, float], angle: float, ppi: float
) -> tuple[float, float] | None:
    """Sub-pixel crossing of the two stroke lines through a coarse thin-cross centre, or None.

    Each stroke is fitted from the thin-stroke pixels in a narrow band along its coarse direction
    (`angle` and `angle` + 90 degrees), away from the crossing itself; the band follows each fit.
    """
    reach = int(round(0.6 * ppi))
    cx, cy = centre
    x0, y0 = max(0, int(cx) - reach), max(0, int(cy) - reach)
    x1, y1 = min(lab.shape[1], int(cx) + reach + 1), min(lab.shape[0], int(cy) + reach + 1)
    ys, xs = np.nonzero(_thin_pixels(lab[y0:y1, x0:x1], ppi))
    px, py = xs + x0 + 0.0, ys + y0 + 0.0
    point = np.array([cx, cy])
    angles = [angle, angle + math.pi / 2]
    for _ in range(3):
        fits = []
        for k, theta in enumerate(angles):
            direction = np.array([math.cos(theta), math.sin(theta)])
            normal = np.array([-direction[1], direction[0]])
            rel = np.column_stack([px - point[0], py - point[1]])
            along = np.abs(rel @ direction)
            chosen = (
                (np.abs(rel @ normal) < 0.05 * ppi) & (along > 0.06 * ppi) & (along < 0.5 * ppi)
            )
            if chosen.sum() < 20:
                return None
            pts = np.column_stack([px[chosen], py[chosen]]).astype(np.float32)
            vx, vy, lx, ly = cv2.fitLine(pts, cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
            fits.append((np.array([lx, ly]), np.array([vx, vy])))
            angles[k] = math.atan2(vy, vx)
        crossing = _intersect(fits[0], fits[1], 0.25)
        if crossing is None:
            return None
        point = crossing
    if np.hypot(point[0] - cx, point[1] - cy) > 0.12 * ppi:
        return None
    return float(point[0]), float(point[1])


# --------------------------------------------------------------------------- printed grid


def _local(strokes: np.ndarray, centre: tuple[float, float], reach: float) -> np.ndarray:
    out = np.zeros_like(strokes)
    x0, y0 = max(0, int(centre[0] - reach)), max(0, int(centre[1] - reach))
    x1, y1 = int(centre[0] + reach) + 1, int(centre[1] + reach) + 1
    out[y0:y1, x0:x1] = strokes[y0:y1, x0:x1]
    return out


def _profile(strokes: np.ndarray, angle: float, reference: tuple[float, float]):
    ys, xs = np.nonzero(strokes)
    proj = (xs - reference[0]) * math.cos(angle) + (ys - reference[1]) * math.sin(angle)
    low = int(math.floor(proj.min())) - 1
    hist = np.bincount(np.floor(proj).astype(int) - low, minlength=int(proj.max() - proj.min()) + 3)
    return hist.astype(np.float64), low


def _line_families(strokes: np.ndarray, sppi: float, reference: tuple[float, float]):
    """Two orthogonal families of long straight strokes as [(angle, line offsets)] or None.

    Offsets are positions along the family normal relative to `reference` (stroke-mask px),
    found as peaks of the stroke-pixel projection.
    """
    strokes = _local(strokes, reference, GRID_REACH_IN * sppi)
    mask = (strokes > 0).astype(np.float32)
    if mask.sum() < 50:
        return None
    blurred = cv2.GaussianBlur(mask, (0, 0), 1.2)
    gx = cv2.Sobel(blurred, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(blurred, cv2.CV_32F, 0, 1)
    mean = np.sum(np.hypot(gx, gy) * np.exp(4j * np.arctan2(gy, gx)))
    if abs(mean) < 1e-6:
        return None
    first = float(np.mod(np.angle(mean) / 4, math.pi / 2))
    families = []
    for rough in (first, first + math.pi / 2):
        best = None
        for delta in np.deg2rad(np.arange(-2.0, 2.01, 0.1)):
            hist, low = _profile(mask > 0, rough + delta, reference)
            sharpness = float(np.sum(hist * hist))
            if best is None or sharpness > best[0]:
                best = (sharpness, rough + delta, hist, low)
        _, angle, hist, low = best
        smooth = np.convolve(hist, [1.0, 2.0, 1.0], "same") / 4.0
        threshold = 0.35 * smooth.max()
        peaks = [
            k
            for k in range(1, len(smooth) - 1)
            if smooth[k] >= threshold and smooth[k] >= smooth[k - 1] and smooth[k] > smooth[k + 1]
        ]
        offsets = []
        for k in peaks:
            lo, hi = max(0, k - 2), min(len(hist), k + 3)
            weights = hist[lo:hi]
            offsets.append(float((np.arange(lo, hi) * weights).sum() / weights.sum()) + low + 0.5)
        families.append((float(angle), np.array(sorted(offsets))))
    return families


def _lattice(offsets: np.ndarray, sppi: float):
    """(positions, pitch) when the line `offsets` form a regular lattice, else None."""
    if len(offsets) < 2:
        return None
    groups = [[offsets[0]]]
    for offset in offsets[1:]:
        if offset - groups[-1][-1] <= 0.08 * sppi:
            groups[-1].append(offset)
        else:
            groups.append([offset])
    positions = np.array([np.mean(g) for g in groups])
    if len(positions) < 3:
        return None
    gaps = np.diff(positions)
    pitch = float(np.median(gaps))
    if not MIN_GRID_PITCH_IN * sppi <= pitch <= MAX_GRID_PITCH_IN * sppi:
        return None
    multiples = gaps / pitch
    if (np.abs(multiples - np.rint(multiples)) < 0.12).mean() < 0.7:
        return None
    return positions, pitch


def _lattice_points(lab: np.ndarray, ppi: float, near: tuple[float, float]):
    """(x, y, angle_a, angle_b) of every crossing of a detected printed grid, in crop px."""
    strokes, _blobs_unused, scale = _stroke_mask(lab, ppi)
    sppi = ppi * scale
    reference = (near[0] * scale, near[1] * scale)
    families = _line_families(strokes, sppi, reference)
    if families is None:
        return []
    (t1, o1), (t2, o2) = families
    lattice_a, lattice_b = _lattice(o1, sppi), _lattice(o2, sppi)
    if lattice_a is None or lattice_b is None:
        return []
    basis = np.array([[math.cos(t1), math.sin(t1)], [math.cos(t2), math.sin(t2)]])
    out = []
    for a in lattice_a[0]:
        for b in lattice_b[0]:
            xy = (np.linalg.solve(basis, np.array([a, b])) + np.array(reference)) / scale
            out.append((float(xy[0]), float(xy[1]), t1, t2))
    return out


def _refine_junction(
    lab: np.ndarray, xy: tuple[float, float], thetas: tuple[float, float], ppi: float
) -> tuple[float, float]:
    """Sub-pixel crossing of the two grid lines near `xy` (crop px) from local line fits."""
    reach = int(round(0.7 * ppi))
    cx, cy = xy
    x0, y0 = max(0, int(cx) - reach), max(0, int(cy) - reach)
    x1, y1 = min(lab.shape[1], int(cx) + reach + 1), min(lab.shape[0], int(cy) + reach + 1)
    ys, xs = np.nonzero(_thin_pixels(lab[y0:y1, x0:x1], ppi))
    px, py = xs + x0 + 0.0, ys + y0 + 0.0
    point = np.array([cx, cy])
    for _ in range(3):
        fits = []
        for theta in thetas:
            normal = np.array([math.cos(theta), math.sin(theta)])
            direction = np.array([-normal[1], normal[0]])
            rel = np.column_stack([px - point[0], py - point[1]])
            chosen = (np.abs(rel @ normal) < 0.05 * ppi) & (np.abs(rel @ direction) < 0.6 * ppi)
            if chosen.sum() < 20:
                return xy
            pts = np.column_stack([px[chosen], py[chosen]]).astype(np.float32)
            vx, vy, lx, ly = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01).ravel()
            fits.append((np.array([lx, ly]), np.array([vx, vy])))
        crossing = _intersect(fits[0], fits[1], 0.3)
        if crossing is None:
            return xy
        point = crossing
    if np.hypot(point[0] - cx, point[1] - cy) > 0.3 * ppi:
        return xy
    return float(point[0]), float(point[1])
