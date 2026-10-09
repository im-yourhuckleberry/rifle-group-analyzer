"""Photo pipeline orchestration: reference proposals, manual references and per-group analysis.

The pipeline only proposes. `propose_reference` and `manual_reference` return unconfirmed
`ReferenceResult`s; a caller builds the working image from the one the user confirmed with
`build_working_image`, then runs `analyze_group` once per group (each with its own ROI, in working
px). `detect_holes` output feeds `resolve_shots` without adaptation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from rga.domain.types import (
    Blob,
    CircleCandidate,
    ReferenceKind,
    ReferenceResult,
    Shot,
    WorkingFrame,
)
from rga.vision.learned import detect_holes
from rga.vision.ragged import ResolveResult, resolve_shots
from rga.vision.reference_circle import (
    circle_from_edge_points,
    find_circle_candidates,
    reference_from_circle,
)
from rga.vision.reference_grid import (
    GridCandidate,
    find_grid,
    grid_from_manual,
    reference_from_grid,
)
from rga.vision.transform import apply_h, build_working, working_px_to_plane

RoiPx = tuple[int, int, int, int]


@dataclass(frozen=True)
class ReferenceProposal:
    """One ranked, unconfirmed reference candidate with the geometry a UI needs to draw it.

    `circle` is set for circle kinds (oriented image px). `grid_points_px` holds the four lattice
    corners in plane order (TL, TR, BR, BL of the detected lattice) for grid kinds.
    """

    result: ReferenceResult
    score: float
    circle: CircleCandidate | None = None
    grid_points_px: tuple[tuple[float, float], ...] | None = None


def _as_kind(kind: ReferenceKind | str) -> ReferenceKind:
    try:
        return ReferenceKind(kind)
    except ValueError:
        raise ValueError(f"unknown reference kind {kind!r}") from None


def propose_candidates(
    img: np.ndarray, kind: ReferenceKind | str, max_candidates: int = 5
) -> list[ReferenceProposal]:
    """Ranked, unconfirmed reference candidates of `kind` found in the oriented image `img`.

    Circle kinds return up to `max_candidates` circles (outer disc first, best score next, a printed
    grid in the photo moves the size-matching ones ahead); the grid kind returns
    at most one lattice. Every `result.method` is "auto". An empty list means the user must mark
    the reference by hand. Raises ValueError for a bad image or kind (see the detectors).
    """
    kind = _as_kind(kind)
    if kind is ReferenceKind.GRID_1IN:
        grid = find_grid(img)
        if grid is None:
            return []
        return [
            ReferenceProposal(
                result=reference_from_grid(grid, "auto"),
                score=float(grid.score),
                grid_points_px=_lattice_corners_px(grid),
            )
        ]
    return [
        ReferenceProposal(
            result=reference_from_circle(c, kind, "auto"), score=float(c.score), circle=c
        )
        for c in find_circle_candidates(img, kind, max_candidates, grid=find_grid(img))
    ]


def propose_reference(img: np.ndarray, kind: ReferenceKind | str) -> list[ReferenceResult]:
    """Ranked `ReferenceResult`s for `kind` (see `propose_candidates`); never auto-confirmed."""
    return [p.result for p in propose_candidates(img, kind)]


def manual_reference(
    kind: ReferenceKind | str,
    *,
    edge_points_px: Sequence[tuple[float, float]] | None = None,
    grid_corners_px: Sequence[tuple[float, float]] | None = None,
    squares: tuple[int, int] | None = None,
) -> ReferenceResult:
    """Reference from the user's own marks: circle edge points, or four grid corners + squares.

    A circle kind needs `edge_points_px` (3, 4 or 5+ points); the grid kind needs
    `grid_corners_px` (TL, TR, BR, BL as displayed) and `squares=(across, down)`. Raises
    ValueError for marks that do not match the kind or are degenerate.
    """
    kind = _as_kind(kind)
    if kind is ReferenceKind.GRID_1IN:
        if grid_corners_px is None or squares is None or edge_points_px is not None:
            raise ValueError("a grid reference needs grid_corners_px and squares only")
        return reference_from_grid(grid_from_manual(list(grid_corners_px), *squares), "manual")
    if edge_points_px is None or grid_corners_px is not None or squares is not None:
        raise ValueError("a circle reference needs edge_points_px only")
    return reference_from_circle(circle_from_edge_points(edge_points_px), kind, "manual")


def build_working_image(
    img: np.ndarray, reference: ReferenceResult
) -> tuple[np.ndarray, WorkingFrame, np.ndarray]:
    """Rectified working image for the user-confirmed `reference`.

    Returns `(working, frame, h_px_to_working)` exactly as `transform.build_working` does, with
    `h_px_to_working` acting on the oriented image px.
    """
    return build_working(img, np.array(reference.h_px_to_plane, dtype=np.float64))


def analyze_group(
    working: np.ndarray,
    frame: WorkingFrame,
    bullet_diameter_in: float,
    shots_expected: int,
    roi_px: RoiPx | None = None,
) -> tuple[list[Blob], ResolveResult]:
    """Detect holes in the group's ROI and resolve them to `shots_expected` shots.

    `roi_px` is (x0, y0, x1, y1) in working px (None = the whole image). Returns the detected
    blobs and the `ResolveResult`; positions are working px. With no holes and a shortfall the
    result has no shots and a message to add them by hand (status "ok"), so callers must treat an
    empty `shots` as manual entry. Raises ValueError for an empty ROI, `shots_expected < 1`, or a
    non-positive bullet diameter or ppi.
    """
    if roi_px is not None and not (roi_px[2] > roi_px[0] and roi_px[3] > roi_px[1]):
        raise ValueError("roi_px must be (x0, y0, x1, y1) with x1 > x0 and y1 > y0")
    blobs = detect_holes(working, frame, bullet_diameter_in, roi_px)
    resolved = resolve_shots(blobs, shots_expected, bullet_diameter_in * frame.ppi)
    return blobs, resolved


def shots_from_resolve(resolved: ResolveResult, frame: WorkingFrame) -> list[Shot]:
    """Domain `Shot`s (plane inches, y down) for a `ResolveResult` in working px."""
    if not resolved.shots_px:
        return []
    points = working_px_to_plane([(x, y) for x, y, _, _ in resolved.shots_px], frame)
    return [
        Shot(point=point, origin=origin, confidence=confidence)
        for point, (_, _, origin, confidence) in zip(points, resolved.shots_px, strict=True)
    ]


def roi_to_working(
    h_px_to_working: np.ndarray, corners_px: Sequence[tuple[float, float]], frame: WorkingFrame
) -> RoiPx:
    """Axis-aligned working-px ROI covering oriented-image-px `corners_px`, clamped to the frame."""
    mapped = apply_h(h_px_to_working, np.asarray(corners_px, dtype=np.float64))
    x0, y0 = np.floor(mapped.min(axis=0)).astype(int)
    x1, y1 = np.ceil(mapped.max(axis=0)).astype(int)
    return (
        int(max(0, x0)),
        int(max(0, y0)),
        int(min(frame.width_px, x1)),
        int(min(frame.height_px, y1)),
    )


def _lattice_corners_px(grid: GridCandidate) -> tuple[tuple[float, float], ...]:
    idx = np.asarray(grid.lattice_idx, dtype=np.float64)
    lo, hi = idx.min(axis=0), idx.max(axis=0)
    plane = np.array([[lo[0], lo[1]], [hi[0], lo[1]], [hi[0], hi[1]], [lo[0], hi[1]]])
    h_plane_to_px = np.linalg.inv(np.asarray(grid.h_px_to_plane, dtype=np.float64))
    return tuple((float(x), float(y)) for x, y in apply_h(h_plane_to_px, plane))


__all__ = [
    "ReferenceProposal",
    "analyze_group",
    "build_working_image",
    "manual_reference",
    "propose_candidates",
    "propose_reference",
    "roi_to_working",
    "shots_from_resolve",
]
