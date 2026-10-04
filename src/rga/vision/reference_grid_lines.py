"""Partial thin-line lattice detection: the fallback of `reference_grid.find_grid`.

A printed 1-inch grid that is interrupted (a thick band, a paster, the sheet edge) can leave too few
line crossings for the crossing search, yet still show several long, thin, parallel line segments in
each direction. `find_line_lattice` finds those lines in a thin-structure mask, groups them into two
near-orthogonal families, fits one lattice unit per family and returns the crossings of lines that
both have pixels there.

Only structures narrower than the black-hat kernel reach the mask, so a band or a paster is never a
line. Work-scale pixel coordinates (x, y); lattice indices are (x index, y index), increasing along
image x and image y so the labelling is never mirrored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

_HOUGH_THETA = math.pi / 1440
_MIN_SUPPORT_FRACTION = 0.2
_MIN_COVERED_FRACTION = 0.25
_MIN_MASK_PIXELS = 200
_MAX_LINES = 24
_BAND_KERNELS = 0.75
_FAMILY_ANGLE_DEG = 10.0
_ORTHOGONAL_TOLERANCE_DEG = 20.0
_DUPLICATE_ANGLE_DEG = 2.0
_GAP_TOLERANCE = 0.12
_UNIT_AGREEMENT = 0.25
_MAX_INDEX_GAP = 2
_MAX_UNIT_MULTIPLE = 6
_MAX_LINES_PER_FAMILY = 12
_SUPPORT_REACH_KERNELS = 1.5
_MIN_CROSSINGS = 4
_MIN_SPACING_FRACTION = 0.06
_MAX_SPACING_FRACTION = 0.5


@dataclass(frozen=True)
class LineLattice:
    """Crossings (px), their (x, y) lattice indices, the mean unit in px and the line straightness in px."""

    crossings_px: np.ndarray
    lattice_idx: np.ndarray
    unit_px: float
    straightness_px: float


@dataclass(eq=False)
class _Line:
    point: np.ndarray
    direction: np.ndarray
    covered_px: float
    straightness_px: float
    along: np.ndarray

    @property
    def angle_deg(self) -> float:
        return math.degrees(math.atan2(self.direction[1], self.direction[0])) % 180.0

    @property
    def normal(self) -> np.ndarray:
        return np.array([-self.direction[1], self.direction[0]])


def _angle_gap(a: float, b: float) -> float:
    gap = abs(a - b) % 180.0
    return min(gap, 180.0 - gap)


def _strongest_hough_line(
    coords: np.ndarray, shape: tuple[int, int], short_side: int
) -> tuple[float, float] | None:
    """(rho, theta) of the strongest line through the remaining mask pixels, or None."""
    small = np.zeros((shape[0] // 2 + 1, shape[1] // 2 + 1), np.uint8)
    small[(coords[:, 1] / 2).astype(int), (coords[:, 0] / 2).astype(int)] = 255
    votes = max(20, int(_MIN_SUPPORT_FRACTION * short_side / 2))
    found = cv2.HoughLines(small, 1, _HOUGH_THETA, votes)
    if found is None:
        return None
    rho, theta = found[0, 0]
    return float(rho) * 2.0, float(theta)


def _refine_line(
    coords: np.ndarray, weights: np.ndarray, rho: float, theta: float, k: int
) -> _Line | None:
    """Trimmed weighted straight-line fit around a Hough line; None with too few pixels."""
    normal = np.array([math.cos(theta), math.sin(theta)])
    direction = np.array([-normal[1], normal[0]])
    point = normal * rho
    reach = _BAND_KERNELS * k
    for tolerance in (reach, 0.6 * reach, 0.45 * reach):
        near = np.abs((coords - point) @ normal) <= tolerance
        if near.sum() < 30:
            return None
        pts, w = coords[near], weights[near]
        centre = (pts * w[:, None]).sum(axis=0) / w.sum()
        scatter = ((pts - centre) * w[:, None]).T @ (pts - centre) / w.sum()
        direction = np.linalg.eigh(scatter)[1][:, 1]
        normal = np.array([-direction[1], direction[0]])
        point = centre
    if direction[0] < 0 or (direction[0] == 0 and direction[1] < 0):
        direction, normal = -direction, -normal
    rel = coords - point
    core = np.abs(rel @ normal) <= 0.45 * reach
    along = rel[core] @ direction
    covered = len(np.unique(np.floor(along / k))) * k

    wide = np.abs(rel @ normal) <= k
    wide_along, wide_across = rel[wide] @ direction, rel[wide] @ normal
    chunk = np.floor(wide_along / (2 * k)).astype(int)
    medians = [
        float(np.median(wide_across[chunk == c]))
        for c in np.unique(chunk)
        if (chunk == c).sum() >= 8
    ]
    straightness = float(np.sqrt(np.mean(np.square(medians)))) if len(medians) >= 2 else 0.0
    return _Line(point, direction, float(covered), straightness, along)


def _peel_lines(coords: np.ndarray, weights: np.ndarray, shape: tuple[int, int], k: int):
    """Long lines found one at a time; each fitted line removes its pixels before the next search."""
    short_side = min(shape)
    alive = np.ones(len(coords), bool)
    lines: list[_Line] = []
    for _ in range(_MAX_LINES):
        if alive.sum() < _MIN_MASK_PIXELS:
            break
        peak = _strongest_hough_line(coords[alive], shape, short_side)
        line = None if peak is None else _refine_line(coords[alive], weights[alive], *peak, k)
        if line is None:
            break
        alive &= np.abs((coords - line.point) @ line.normal) > _BAND_KERNELS * k
        if line.covered_px >= _MIN_COVERED_FRACTION * short_side:
            lines.append(line)
    return lines


def _drop_duplicates(lines: list[_Line], k: int) -> list[_Line]:
    kept: list[_Line] = []
    for line in sorted(lines, key=lambda ln: -ln.covered_px):
        duplicate = any(
            _angle_gap(line.angle_deg, other.angle_deg) < _DUPLICATE_ANGLE_DEG
            and abs((line.point - other.point) @ other.normal) < 0.6 * k
            for other in kept
        )
        if not duplicate:
            kept.append(line)
    return kept


def _split_families(lines: list[_Line]) -> tuple[list[_Line], list[_Line]]:
    """The two near-orthogonal line families with the most lines, then the most coverage."""
    best_score, best = -1.0, ([], [])
    for seed in lines:
        first = [
            ln for ln in lines if _angle_gap(ln.angle_deg, seed.angle_deg) <= _FAMILY_ANGLE_DEG
        ]
        second = [
            ln
            for ln in lines
            if abs(_angle_gap(ln.angle_deg, seed.angle_deg) - 90.0) <= _ORTHOGONAL_TOLERANCE_DEG
        ]
        score = min(len(first), len(second)) * 1e6 + sum(ln.covered_px for ln in first + second)
        if score > best_score:
            best_score, best = score, (first, second)
    return best


def _sorted_by_offset(family: list[_Line], centre: np.ndarray, axis: np.ndarray):
    """Lines and their signed offsets from `centre` along the normal that points along `axis`."""
    offsets = []
    for line in family:
        normal = line.normal if line.normal @ axis >= 0 else -line.normal
        offsets.append(float((line.point - centre) @ normal))
    order = np.argsort(offsets)
    return [family[i] for i in order], np.array([offsets[i] for i in order])


def _candidate_units(
    offsets: np.ndarray, lo: float, hi: float
) -> list[tuple[float, int, np.ndarray]]:
    """(unit, lines explained, index per line or -1) for each unit that explains two or more lines.

    A unit is a gap divided by 1 to 6. Adjacent explained lines may differ by at most
    `_MAX_INDEX_GAP` indices and no two lines may share an index.
    """
    gaps = [
        offsets[j] - offsets[i] for i in range(len(offsets)) for j in range(i + 1, len(offsets))
    ]
    units = sorted(
        {round(g / n) for g in gaps for n in range(1, _MAX_UNIT_MULTIPLE + 1) if lo <= g / n <= hi}
    )
    found = []
    for unit in units:
        best: tuple[int, np.ndarray, np.ndarray] | None = None
        for anchor in offsets:
            steps = (offsets - anchor) / unit
            on = np.abs(steps - np.round(steps)) <= _GAP_TOLERANCE
            indices = np.round(steps).astype(int)[on]
            if on.sum() < 2 or len(set(indices.tolist())) != len(indices):
                continue
            if np.max(np.diff(np.sort(indices))) > _MAX_INDEX_GAP:
                continue
            if best is None or on.sum() > best[0]:
                best = (int(on.sum()), np.round(steps).astype(int), on)
        if best is not None:
            count, rounded, on = best
            found.append((float(unit), count, np.where(on, rounded - rounded[on].min(), -1)))
    return found


def _longest_first(line: _Line) -> float:
    return -line.covered_px


def _intersection(a: _Line, b: _Line) -> np.ndarray | None:
    system = np.array([a.direction, -b.direction]).T
    if abs(np.linalg.det(system)) < 1e-6:
        return None
    return a.point + np.linalg.solve(system, b.point - a.point)[0] * a.direction


def _has_pixels_near(line: _Line, p: np.ndarray, k: int) -> bool:
    s = float((p - line.point) @ line.direction)
    return bool(np.any(np.abs(line.along - s) <= _SUPPORT_REACH_KERNELS * k))


def find_line_lattice(mask: np.ndarray, response: np.ndarray, k: int) -> LineLattice | None:
    """Lattice of long thin lines in a binary thin-structure `mask`, or None.

    `response` is the black-hat line response (weights) and `k` its kernel size in px. Needs at least
    two lines per family and at least three in one of them (two lines show no spacing to check, so a
    hidden middle line would silently double the unit), unit spacing
    that both families agree on within 25 percent, and four crossings both of whose lines have pixels
    there. Every line must have pixels over at least a quarter of the image's short side.
    """
    height, width = mask.shape
    short_side = min(height, width)
    ys, xs = np.nonzero(mask)
    if len(xs) < _MIN_MASK_PIXELS:
        return None
    coords = np.stack([xs, ys], axis=1).astype(np.float64)
    lines = _drop_duplicates(
        _peel_lines(coords, response[ys, xs].astype(np.float64), mask.shape, k), k
    )
    if len(lines) < 4:
        return None
    first, second = _split_families(lines)
    if len(first) < 2 or len(second) < 2:
        return None
    vertical = sorted(first, key=_longest_first)[:_MAX_LINES_PER_FAMILY]
    horizontal = sorted(second, key=_longest_first)[:_MAX_LINES_PER_FAMILY]
    if np.mean([abs(ln.direction[1]) for ln in vertical]) < np.mean(
        [abs(ln.direction[1]) for ln in horizontal]
    ):
        vertical, horizontal = horizontal, vertical
    centre = np.array([width / 2.0, height / 2.0])
    vertical, v_offsets = _sorted_by_offset(vertical, centre, np.array([1.0, 0.0]))
    horizontal, h_offsets = _sorted_by_offset(horizontal, centre, np.array([0.0, 1.0]))

    lo, hi = _MIN_SPACING_FRACTION * short_side, _MAX_SPACING_FRACTION * short_side
    best = None
    for v_unit, v_count, v_idx in _candidate_units(v_offsets, lo, hi):
        for h_unit, h_count, h_idx in _candidate_units(h_offsets, lo, hi):
            ratio = h_unit / v_unit
            if not 1 - _UNIT_AGREEMENT <= ratio <= 1 / (1 - _UNIT_AGREEMENT):
                continue
            if min(v_count, h_count) < 2 or max(v_count, h_count) < 3:
                continue
            key = (v_count + h_count, v_unit + h_unit)
            if best is None or key > best[0]:
                best = (key, v_unit, h_unit, v_idx, h_idx)
    if best is None:
        return None
    _, v_unit, h_unit, v_idx, h_idx = best

    crossings, indices = [], []
    for i, v_line in enumerate(vertical):
        for j, h_line in enumerate(horizontal):
            if v_idx[i] < 0 or h_idx[j] < 0:
                continue
            p = _intersection(v_line, h_line)
            if p is None or not (
                -0.1 * width <= p[0] <= 1.1 * width and -0.1 * height <= p[1] <= 1.1 * height
            ):
                continue
            if _has_pixels_near(v_line, p, k) and _has_pixels_near(h_line, p, k):
                crossings.append(p)
                indices.append((int(v_idx[i]), int(h_idx[j])))
    if len(crossings) < _MIN_CROSSINGS:
        return None
    used = [ln for i, ln in enumerate(vertical) if v_idx[i] >= 0] + [
        ln for j, ln in enumerate(horizontal) if h_idx[j] >= 0
    ]
    straightness = float(np.sqrt(np.mean([ln.straightness_px**2 for ln in used])))
    return LineLattice(
        np.array(crossings), np.array(indices), 0.5 * (v_unit + h_unit), straightness
    )
