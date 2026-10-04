"""Resolve detected blobs into the expected number of shots (FR-3.7, D-18, P-05).

`resolve_shots` turns single/split blob centers into distinct shots, then places any shortfall
against the expected count as `estimated` shots inside ragged blobs (or, when there are none,
stacked in the largest distinct hole). Pure and deterministic; coordinates are working px.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np

from rga.domain.types import Blob, BlobKind, ShotOrigin

ESTIMATED_CONFIDENCE = 0.25
KMEANS_SEED = 20240601
KMEANS_ATTEMPTS = 5
REPULSION_ITERATIONS = 50
_MASK_PAD = 3
_EPS = 1e-9

ShotPx = tuple[float, float, ShotOrigin, float]
Status = Literal["ok", "too_many", "unassigned_ragged"]


@dataclass(frozen=True)
class ResolveResult:
    """Resolved shots as (x_px, y_px, origin, confidence), a status, and user-facing messages."""

    shots_px: tuple[ShotPx, ...]
    status: Status
    messages: tuple[str, ...]


def resolve_shots(
    blobs: Sequence[Blob], shots_expected: int, bullet_diameter_px: float
) -> ResolveResult:
    """Return distinct shots plus estimated fill so the count reaches `shots_expected`.

    Single blobs give `detected` shots and split blobs give `split` shots. A shortfall is shared
    across ragged blobs in proportion to excess area and placed inside their outlines as
    `estimated`; with no ragged blob it is stacked at the largest distinct hole. Status is
    `too_many` when distinct shots exceed the expected count (no fill is made),
    `unassigned_ragged` when ragged blobs outnumber the shortfall. Raises ValueError on a
    non-positive expected count or bullet diameter.
    """
    if shots_expected < 1:
        raise ValueError("shots_expected must be at least 1")
    if not bullet_diameter_px > 0:
        raise ValueError("bullet_diameter_px must be positive")

    distinct = [_distinct_shots(b) for b in blobs]
    distinct_count = sum(len(shots) for shots in distinct)
    ragged = [i for i, b in enumerate(blobs) if b.kind == BlobKind.RAGGED]

    if distinct_count > shots_expected:
        return ResolveResult(
            tuple(s for shots in distinct for s in shots),
            "too_many",
            (
                f"{distinct_count} distinct holes were found but {shots_expected} shots are "
                "expected: delete detection errors or raise the shot count.",
            ),
        )

    shortfall = shots_expected - distinct_count
    estimated: dict[int, list[ShotPx]] = {}
    messages: list[str] = []
    status: Status = "ok"

    if ragged:
        counts = _allocate([_excess_area(blobs[i], bullet_diameter_px) for i in ragged], shortfall)
        r_px = bullet_diameter_px / 2.0
        for blob_index, count in zip(ragged, counts, strict=True):
            if count:
                centers = _place_in_blob(blobs[blob_index], count, r_px)
                estimated[blob_index] = [
                    (x, y, ShotOrigin.ESTIMATED, ESTIMATED_CONFIDENCE) for x, y in centers
                ]
        unassigned = sum(1 for count in counts if count == 0)
        if unassigned:
            status = "unassigned_ragged"
            messages.append(
                f"{unassigned} ragged hole(s) have no shot assigned: raise the shot count "
                "or delete detection errors."
            )
        if shortfall:
            filled = sum(1 for count in counts if count)
            messages.append(
                f"Placed {shortfall} estimated shot(s) inside {filled} ragged hole(s); "
                "drag them to adjust."
            )
    elif shortfall:
        target = _largest_distinct_hole(blobs)
        if target is None:
            messages.append(
                f"No holes were detected for {shortfall} missing shot(s): add them by hand."
            )
        else:
            stacked = (target[0], target[1], ShotOrigin.ESTIMATED, ESTIMATED_CONFIDENCE)
            estimated[target[2]] = [stacked] * shortfall
            messages.append(
                f"Placed {shortfall} estimated shot(s) at the center of the largest hole; "
                "drag them to adjust."
            )

    shots: list[ShotPx] = []
    for i, shots_i in enumerate(distinct):
        shots.extend(shots_i)
        shots.extend(estimated.get(i, ()))
    return ResolveResult(tuple(shots), status, tuple(messages))


def _distinct_shots(blob: Blob) -> list[ShotPx]:
    if blob.kind == BlobKind.RAGGED:
        return []
    origin = ShotOrigin.SPLIT if blob.kind == BlobKind.SPLIT else ShotOrigin.DETECTED
    centers = blob.centers_px or (_contour_mean(blob),)
    return [(float(x), float(y), origin, float(blob.confidence)) for x, y in centers]


def _contour_mean(blob: Blob) -> tuple[float, float]:
    if not blob.contour_px:
        raise ValueError("blob has neither centers nor a contour")
    pts = np.asarray(blob.contour_px, dtype=np.float64)
    return float(pts[:, 0].mean()), float(pts[:, 1].mean())


def _excess_area(blob: Blob, bullet_diameter_px: float) -> float:
    single_area = math.pi * (bullet_diameter_px / 2.0) ** 2
    return max(blob.area_px - single_area, 0.0)


def _allocate(excess: Sequence[float], shortfall: int) -> list[int]:
    """Share `shortfall` across blobs by excess area (largest remainder, >=1 each if possible)."""
    n = len(excess)
    if shortfall <= 0:
        return [0] * n
    if shortfall < n:
        order = sorted(range(n), key=lambda i: (-excess[i], i))
        chosen = set(order[:shortfall])
        return [1 if i in chosen else 0 for i in range(n)]

    total = sum(excess)
    weights = [e / total for e in excess] if total > _EPS else [1.0 / n] * n
    ideal = [shortfall * w for w in weights]
    counts = [max(1, math.floor(x)) for x in ideal]
    while sum(counts) < shortfall:
        counts[max(range(n), key=lambda i: (ideal[i] - counts[i], -i))] += 1
    while sum(counts) > shortfall:
        removable = [i for i in range(n) if counts[i] > 1]
        counts[min(removable, key=lambda i: (ideal[i] - counts[i], i))] -= 1
    return counts


def _largest_distinct_hole(blobs: Sequence[Blob]) -> tuple[float, float, int] | None:
    best: tuple[float, float, int] | None = None
    best_area = -1.0
    for index, blob in enumerate(blobs):
        if blob.kind == BlobKind.RAGGED:
            continue
        centers = blob.centers_px or (_contour_mean(blob),)
        share = blob.area_px / len(centers)
        if share > best_area + _EPS:
            best_area = share
            best = (float(centers[0][0]), float(centers[0][1]), index)
    return best


@dataclass(frozen=True)
class _Region:
    contour: np.ndarray
    origin_xy: tuple[int, int]
    interior: np.ndarray
    candidates: np.ndarray


def _place_in_blob(blob: Blob, count: int, r_px: float) -> list[tuple[float, float]]:
    """Place `count` points inside the blob outline, spread as far apart as the outline allows."""
    if len(blob.contour_px) < 3:
        fallback = blob.centers_px[0] if blob.centers_px else _contour_mean(blob)
        return [(float(fallback[0]), float(fallback[1]))] * count

    region = _build_region(blob)
    if len(region.candidates) == 0:
        x, y = _contour_mean(blob)
        return [(x, y)] * count

    seeds = _kmeans_centers(region.candidates, count)
    points = np.array([_project(region, p) for p in seeds], dtype=np.float64)
    points = _spread(region, points, separation=2.0 * r_px)
    order = np.lexsort((points[:, 0], points[:, 1]))
    return [(float(points[i, 0]), float(points[i, 1])) for i in order]


def _build_region(blob: Blob) -> _Region:
    contour = np.asarray(blob.contour_px, dtype=np.int32).reshape(-1, 1, 2)
    x0, y0, w, h = cv2.boundingRect(contour)
    ox, oy = x0 - _MASK_PAD, y0 - _MASK_PAD
    mask = np.zeros((h + 2 * _MASK_PAD + 1, w + 2 * _MASK_PAD + 1), dtype=np.uint8)
    cv2.fillPoly(mask, [contour - np.array([[ox, oy]], dtype=np.int32)], 255)
    ys, xs = np.nonzero(mask)
    candidates = np.column_stack((xs + ox, ys + oy)).astype(np.float32)
    dist = cv2.distanceTransform(mask, cv2.DIST_L2, 3)
    threshold = min(2.0, float(dist.max())) if len(xs) else 1.0
    return _Region(contour, (ox, oy), dist >= threshold, candidates)


def _kmeans_centers(candidates: np.ndarray, count: int) -> np.ndarray:
    if count == 1:
        return candidates.mean(axis=0, keepdims=True).astype(np.float64)
    if len(candidates) < count:
        index = np.arange(count) % len(candidates)
        return candidates[index].astype(np.float64)
    cv2.setRNGSeed(KMEANS_SEED)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 0.01)
    _, _, centers = cv2.kmeans(
        candidates, count, None, criteria, KMEANS_ATTEMPTS, cv2.KMEANS_PP_CENTERS
    )
    return centers.astype(np.float64)


def _inside(region: _Region, x: float, y: float) -> bool:
    return cv2.pointPolygonTest(region.contour, (float(x), float(y)), False) >= 0


def _in_interior(region: _Region, x: float, y: float) -> bool:
    col = int(round(x)) - region.origin_xy[0]
    row = int(round(y)) - region.origin_xy[1]
    rows, cols = region.interior.shape
    return 0 <= row < rows and 0 <= col < cols and bool(region.interior[row, col])


def _project(region: _Region, point: np.ndarray) -> np.ndarray:
    """Return `point` if it lies in the eroded interior, else the nearest interior pixel."""
    x, y = float(point[0]), float(point[1])
    if _in_interior(region, x, y) and _inside(region, x, y):
        return np.array([x, y], dtype=np.float64)

    ox, oy = region.origin_xy
    rows, cols = np.nonzero(region.interior)
    pool = np.column_stack((cols + ox, rows + oy)).astype(np.float64)
    if len(pool) == 0:
        pool = region.candidates.astype(np.float64)
    order = np.argsort(((pool - (x, y)) ** 2).sum(axis=1), kind="stable")
    for index in order:
        px, py = pool[index]
        if _inside(region, px, py):
            return np.array([px, py], dtype=np.float64)
    vertex = region.contour[0, 0]
    return np.array([float(vertex[0]), float(vertex[1])], dtype=np.float64)


def _min_pair_distance(points: np.ndarray) -> float:
    count = len(points)
    if count < 2:
        return math.inf
    return min(
        float(np.hypot(*(points[i] - points[j]))) for i in range(count) for j in range(i + 1, count)
    )


def _spread(region: _Region, points: np.ndarray, separation: float) -> np.ndarray:
    """Push points apart toward `separation`, projecting back inside; return the best layout."""
    count = len(points)
    if count < 2:
        return points
    best, best_min = points.copy(), _min_pair_distance(points)
    for _ in range(REPULSION_ITERATIONS):
        if best_min >= separation:
            break
        push = np.zeros_like(points)
        for i in range(count):
            for j in range(i + 1, count):
                delta = points[i] - points[j]
                dist = float(np.hypot(delta[0], delta[1]))
                if dist >= separation:
                    continue
                if dist < _EPS:
                    angle = 2.0 * math.pi * (i * count + j + 1) / (count * count + 1)
                    direction = np.array([math.cos(angle), math.sin(angle)])
                else:
                    direction = delta / dist
                shift = direction * (separation - dist) / 2.0
                push[i] += shift
                push[j] -= shift
        points = np.array([_project(region, p + q) for p, q in zip(points, push, strict=True)])
        current = _min_pair_distance(points)
        if current > best_min:
            best, best_min = points.copy(), current
    return best
