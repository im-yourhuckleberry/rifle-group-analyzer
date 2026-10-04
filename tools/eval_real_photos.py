"""Real-photo evaluation: runs the photo pipeline on annotated photos and reports the P-11 real rows.

Usage:
    python -m tools.eval_real_photos [--dir data/real_photos] [--json report.json]
        [--overlay-dir DIR] [--coarse]

Every `<stem>.<jpg|jpeg|png|heic>` in the folder with a `<stem>.truth.json` (format: tests/real/README.md)
is run end to end: reference proposal, working image, per-group detection and shot resolution. The
resolved shots (what the app would show) are scored against the annotated hole centres with
`tools.eval_detection.score_blobs`.

Exit codes: 0 every P-11 real-photo row was evaluated and passes; 1 a row fails or could not be
evaluated (INCOMPLETE); 2 bad arguments or an invalid truth file; 3 "real-photo gate outstanding" (folder missing or empty, or no photo has a truth file).
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import cv2
import numpy as np

from rga.domain.types import Blob, BlobKind, ReferenceKind, ReferenceResult
from rga.vision.io import ImageRejected, load_oriented
from rga.vision.pipeline import (
    ReferenceProposal,
    analyze_group,
    build_working_image,
    propose_candidates,
    roi_to_working,
)
from rga.vision.transform import apply_h
from tools.eval_detection import (
    aggregate,
    format_real_table,
    load_real_counts,
    real_photo_scores,
    score_blobs,
)

FORMAT_VERSION = 1
TRUTH_SUFFIX = ".truth.json"
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".heic")
REAL_PHOTO_DIR = Path(__file__).resolve().parents[1] / "data" / "real_photos"
REPORT_SCHEMA = "rga-real-photo-report/1"
EXIT_PASS, EXIT_FAIL, EXIT_USAGE, EXIT_OUTSTANDING = 0, 1, 2, 3
OUTSTANDING_MESSAGE = "real-photo gate outstanding"
RECOMMENDED_PHOTOS = 10


class TruthError(ValueError):
    """A `<photo>.truth.json` file is malformed; the message names the file and the field."""


@dataclass(frozen=True)
class RealThresholds:
    """P-11 real-photo rows (spec.md section 6)."""

    scale_error_pct: float = 0.5
    center_p95_in: float = 0.05
    recall: float = 0.90
    false_positives_per_photo: int = 5
    es_error_in: float = 0.05
    runtime_s: float = 3.0


REAL_P11 = RealThresholds()


@dataclass(frozen=True)
class GroupTruth:
    """One group of a photo: its region, aim, expected shot count and the holes that belong to it."""

    name: str
    roi_px: tuple[float, float, float, float] | None
    aim_px: tuple[float, float] | None
    shots_expected: int
    hole_indices: tuple[int, ...]
    hand_es_in: float | None


@dataclass(frozen=True)
class PhotoTruth:
    """Parsed `<photo>.truth.json`. Coordinates are oriented-image px (EXIF applied, x right, y down)."""

    reference_kind: ReferenceKind
    distance_yd: float
    bullet_diameter_in: float
    holes_px: np.ndarray
    aim_px: tuple[float, float] | None
    hand_es_in: float | None
    reference_h_px_to_plane: np.ndarray | None
    reference_method: str | None
    groups: tuple[GroupTruth, ...]


# ----------------------------------------------------------------------------------- truth files


def _fail(source: str, field_name: str, reason: str) -> TruthError:
    return TruthError(f"{source}: {field_name}: {reason}")


def _number(source: str, name: str, value: object, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise _fail(source, name, "must be a finite number")
    if positive and not value > 0:
        raise _fail(source, name, "must be positive")
    return float(value)


def _point(source: str, name: str, value: object) -> tuple[float, float]:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise _fail(source, name, "must be [x, y]")
    return (_number(source, name, value[0]), _number(source, name, value[1]))


def _optional_es(source: str, name: str, value: object) -> float | None:
    if value is None:
        return None
    es = _number(source, name, value)
    if es < 0:
        raise _fail(source, name, "must not be negative")
    return es


def _parse_reference(source: str, raw: object) -> tuple[np.ndarray | None, str | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        raise _fail(source, "reference", "must be an object or null")
    method = raw.get("method", "manual")
    if method not in ("auto", "manual"):
        raise _fail(source, "reference.method", "must be 'auto' or 'manual'")
    rows = raw.get("h_px_to_plane")
    if not (
        isinstance(rows, list)
        and len(rows) == 3
        and all(isinstance(r, list) and len(r) == 3 for r in rows)
    ):
        raise _fail(source, "reference.h_px_to_plane", "must be a 3x3 matrix")
    h = np.array([[_number(source, "reference.h_px_to_plane", v) for v in r] for r in rows])
    if abs(np.linalg.det(h)) < 1e-18:
        raise _fail(source, "reference.h_px_to_plane", "must be invertible")
    return h, method


def _parse_group(source: str, index: int, raw: object, n_holes: int) -> GroupTruth:
    where = f"groups[{index}]"
    if not isinstance(raw, dict):
        raise _fail(source, where, "must be an object")
    roi = raw.get("roi_px")
    if roi is not None:
        if not isinstance(roi, list | tuple) or len(roi) != 4:
            raise _fail(source, f"{where}.roi_px", "must be [x0, y0, x1, y1]")
        roi = tuple(_number(source, f"{where}.roi_px", v) for v in roi)
        if not (roi[2] > roi[0] and roi[3] > roi[1]):
            raise _fail(source, f"{where}.roi_px", "needs x1 > x0 and y1 > y0")
    indices = raw.get("hole_indices")
    if not isinstance(indices, list) or not indices:
        raise _fail(source, f"{where}.hole_indices", "must be a non-empty list of hole indices")
    if any(isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < n_holes for i in indices):
        raise _fail(source, f"{where}.hole_indices", f"must be integers in 0..{n_holes - 1}")
    if len(set(indices)) != len(indices):
        raise _fail(source, f"{where}.hole_indices", "must be unique")
    expected = raw.get("shots_expected", len(indices))
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
        raise _fail(source, f"{where}.shots_expected", "must be an integer >= 1")
    aim = raw.get("aim_px")
    return GroupTruth(
        name=str(raw.get("name", f"group{index + 1}")),
        roi_px=roi,
        aim_px=None if aim is None else _point(source, f"{where}.aim_px", aim),
        shots_expected=expected,
        hole_indices=tuple(indices),
        hand_es_in=_optional_es(source, f"{where}.hand_es_in", raw.get("hand_es_in")),
    )


def parse_truth(data: object, source: str = "<truth>") -> PhotoTruth:
    """Validate a decoded truth document and return it as a `PhotoTruth`; raises `TruthError`."""
    if not isinstance(data, dict):
        raise TruthError(f"{source}: the document must be a JSON object")
    version = data.get("format_version", FORMAT_VERSION)
    if version != FORMAT_VERSION:
        raise _fail(source, "format_version", f"unsupported version {version!r}")
    try:
        kind = ReferenceKind(data.get("reference_kind"))
    except ValueError:
        raise _fail(
            source, "reference_kind", f"must be one of {[k.value for k in ReferenceKind]}"
        ) from None
    distance = _number(source, "distance_yd", data.get("distance_yd"), positive=True)
    diameter = _number(source, "bullet_diameter_in", data.get("bullet_diameter_in"), positive=True)
    if not 0.1 < diameter < 0.6:
        raise _fail(source, "bullet_diameter_in", "must be between 0.1 and 0.6 inches")
    holes = data.get("holes_px")
    if not isinstance(holes, list) or not holes:
        raise _fail(source, "holes_px", "must be a non-empty list of [x, y]")
    holes_px = np.array([_point(source, f"holes_px[{i}]", h) for i, h in enumerate(holes)])
    aim = data.get("aim_px")
    h_ref, method = _parse_reference(source, data.get("reference"))
    raw_groups = data.get("groups")
    if raw_groups is None:
        groups = (
            GroupTruth(
                name="all",
                roi_px=None,
                aim_px=None if aim is None else _point(source, "aim_px", aim),
                shots_expected=len(holes_px),
                hole_indices=tuple(range(len(holes_px))),
                hand_es_in=_optional_es(source, "hand_es_in", data.get("hand_es_in")),
            ),
        )
    else:
        if not isinstance(raw_groups, list) or not raw_groups:
            raise _fail(source, "groups", "must be a non-empty list")
        groups = tuple(_parse_group(source, i, g, len(holes_px)) for i, g in enumerate(raw_groups))
        claimed = [i for g in groups for i in g.hole_indices]
        if len(set(claimed)) != len(claimed):
            raise _fail(source, "groups", "a hole belongs to more than one group")
    return PhotoTruth(
        reference_kind=kind,
        distance_yd=distance,
        bullet_diameter_in=diameter,
        holes_px=holes_px,
        aim_px=None if aim is None else _point(source, "aim_px", aim),
        hand_es_in=_optional_es(source, "hand_es_in", data.get("hand_es_in")),
        reference_h_px_to_plane=h_ref,
        reference_method=method,
        groups=groups,
    )


def load_truth(path: Path) -> PhotoTruth:
    """Read and validate one truth file; raises `TruthError` (also for unreadable JSON)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TruthError(f"{path}: cannot read JSON ({exc})") from exc
    return parse_truth(data, str(path))


def truth_to_json(doc: dict) -> str:
    """Serialize a truth document (as built by `truth_document`) with stable formatting."""
    return json.dumps(doc, indent=1) + "\n"


def truth_document(
    *,
    reference_kind: ReferenceKind,
    distance_yd: float,
    bullet_diameter_in: float,
    holes_px: Sequence[Sequence[float]],
    groups: Sequence[dict],
    reference: ReferenceResult | None = None,
    aim_px: Sequence[float] | None = None,
    hand_es_in: float | None = None,
) -> dict:
    """Build the JSON-ready truth document; the reference implementation of the app's exporter.

    `holes_px` and every group's `aim_px` / `roi_px` are oriented-image px. A group is
    `{"name", "roi_px", "aim_px", "shots_expected", "hole_indices", "hand_es_in"}`.
    """
    doc: dict = {
        "format_version": FORMAT_VERSION,
        "reference_kind": ReferenceKind(reference_kind).value,
        "distance_yd": distance_yd,
        "bullet_diameter_in": bullet_diameter_in,
        "holes_px": [[float(x), float(y)] for x, y in holes_px],
        "aim_px": None if aim_px is None else [float(aim_px[0]), float(aim_px[1])],
        "hand_es_in": hand_es_in,
        "groups": [dict(g) for g in groups],
    }
    if reference is not None:
        doc["reference"] = {
            "method": reference.method,
            "h_px_to_plane": [list(row) for row in reference.h_px_to_plane],
        }
    return doc


def plane_in_to_oriented_px(
    points_in: Sequence[Sequence[float]], ppi: float, h_px_to_working: np.ndarray
) -> np.ndarray:
    """Oriented-image px of shots stored as working-plane inches (what the app keeps per shot).

    `ppi` is the working frame's ppi and `h_px_to_working` the matrix `build_working_image` returned
    for the confirmed reference.
    """
    pts = np.asarray(points_in, dtype=np.float64).reshape(-1, 2) * ppi
    return apply_h(np.linalg.inv(np.asarray(h_px_to_working, dtype=np.float64)), pts)


# ----------------------------------------------------------------------------------- evaluation


@dataclass
class GroupReport:
    """Scored result for one group of one photo."""

    name: str
    holes: int
    ragged_holes: int
    ragged_credited: int
    shots_expected: int
    detections: int
    matched: int
    recall: float
    false_positives: int
    center_errors_in: list[float]
    shots_resolved: int
    shots_estimated: int
    resolve_status: str
    es_app_in: float | None
    es_truth_in: float | None
    es_error_in: float | None
    es_hand_in: float | None
    es_vs_hand_in: float | None
    poi_error_in: float | None


@dataclass
class PhotoReport:
    """Scored result for one photo; `status` is 'ok' unless the photo could not be evaluated."""

    name: str
    status: str
    detail: str = ""
    reference_kind: str = ""
    reference_source: str = ""
    proposals: int = 0
    proposal_rank: int | None = None
    reference_warnings: list[str] = field(default_factory=list)
    scale_error_pct: float | None = None
    scale_status: str = ""
    runtime_s: float = 0.0
    groups: list[GroupReport] = field(default_factory=list)

    @property
    def false_positives(self) -> int:
        """Resolved shots of all groups that match no annotated hole and are not credited to a ragged blob."""
        return sum(g.false_positives for g in self.groups)


def _px_per_in(h_px_to_plane: np.ndarray, at_px: np.ndarray) -> float:
    """Local image px per plane inch of `h_px_to_plane` at `at_px`."""
    step = 1.0
    pts = np.array([at_px, at_px + [step, 0.0], at_px + [0.0, step]])
    plane = apply_h(h_px_to_plane, pts)
    jac = np.column_stack([plane[1] - plane[0], plane[2] - plane[0]]) / step
    return 1.0 / math.sqrt(abs(np.linalg.det(jac)))


def _pick_proposal(
    proposals: Sequence[ReferenceProposal], truth: PhotoTruth
) -> tuple[ReferenceProposal, int]:
    """Play the user's confirm step: the proposal closest to the annotated reference, else rank 1."""
    if truth.reference_h_px_to_plane is None or len(proposals) == 1:
        return proposals[0], 1
    if truth.reference_kind is ReferenceKind.GRID_1IN:
        return proposals[0], 1
    origin = apply_h(np.linalg.inv(truth.reference_h_px_to_plane), np.zeros((1, 2)))[0]
    best = min(
        range(len(proposals)),
        key=lambda i: (
            math.dist(proposals[i].circle.center_px, origin) if proposals[i].circle else math.inf
        ),
    )
    return proposals[best], best + 1


def _extreme_spread(points: np.ndarray) -> float | None:
    if len(points) < 2:
        return None
    return max(math.dist(a, b) for a, b in itertools.combinations(points, 2))


def _owners(points: np.ndarray, contours: list[np.ndarray], tolerance_px: float) -> list[int]:
    """Index of the first contour containing each point (within `tolerance_px`), -1 when none does."""
    owners = []
    for x, y in points:
        owner = -1
        for k, contour in enumerate(contours):
            if cv2.pointPolygonTest(contour, (float(x), float(y)), True) >= -tolerance_px:
                owner = k
                break
        owners.append(owner)
    return owners


def _shot_blobs(shots_px: np.ndarray) -> list[Blob]:
    return [
        Blob(
            kind=BlobKind.SINGLE,
            contour_px=(),
            area_px=0.0,
            centers_px=((float(x), float(y)),),
            confidence=1.0,
        )
        for x, y in shots_px
    ]


def _score_group(
    group: GroupTruth,
    truth: PhotoTruth,
    blobs,
    resolved,
    frame,
    h_px_to_working: np.ndarray,
) -> GroupReport:
    """Score the shots the app would show (after `resolve_shots`) against the annotated holes.

    Shots and holes outside every ragged outline are matched one to one (Hungarian, 0.5 bullet
    diameters). Inside a ragged outline, recall is credited only up to the number of resolved shots
    placed there; annotated holes beyond that are misses and surplus shots are false positives.
    """
    holes = truth.holes_px[list(group.hole_indices)]
    holes_work = apply_h(h_px_to_working, holes)
    shots_px = np.array([(x, y) for x, y, _, _ in resolved.shots_px]).reshape(-1, 2)
    d_px = truth.bullet_diameter_in * frame.ppi
    contours = [
        np.array(b.contour_px, dtype=np.float32).reshape(-1, 1, 2)
        for b in blobs
        if b.kind is BlobKind.RAGGED and len(b.contour_px) >= 3
    ]
    hole_owner = _owners(holes_work, contours, 0.3 * d_px)
    shot_owner = _owners(shots_px, contours, 0.3 * d_px)
    credited = extra_shots = 0
    for k in range(len(contours)):
        n_holes, n_shots = hole_owner.count(k), shot_owner.count(k)
        credited += min(n_holes, n_shots)
        extra_shots += max(0, n_shots - n_holes)
    free_holes = [i for i, o in enumerate(hole_owner) if o < 0]
    free_shots = np.array([p for p, o in zip(shots_px, shot_owner, strict=True) if o < 0]).reshape(
        -1, 2
    )
    score_truth = {
        "bullet_diameter_in": truth.bullet_diameter_in,
        "holes_in": holes[free_holes].tolist(),
        "h_plane_to_image": np.eye(3).tolist(),
        "merged_pairs": [],
        "ragged_groups": [],
        "groups": [],
    }
    score = score_blobs(group.name, 0, _shot_blobs(free_shots), score_truth, frame, h_px_to_working)
    matched = score.matched_isolated + credited
    holes_in = holes_work / frame.ppi
    shots_in = shots_px / frame.ppi
    es_app, es_truth = _extreme_spread(shots_in), _extreme_spread(holes_in)
    poi = (
        math.dist(shots_in.mean(axis=0), holes_in.mean(axis=0))
        if len(shots_in) and len(holes_in)
        else None
    )
    return GroupReport(
        name=group.name,
        holes=len(holes),
        ragged_holes=sum(1 for o in hole_owner if o >= 0),
        ragged_credited=credited,
        shots_expected=group.shots_expected,
        detections=len(blobs),
        matched=matched,
        recall=matched / len(holes) if len(holes) else 1.0,
        false_positives=score.false_positives + extra_shots,
        center_errors_in=list(score.isolated_errors_in),
        shots_resolved=len(resolved.shots_px),
        shots_estimated=sum(
            1 for _, _, origin, _ in resolved.shots_px if origin.value == "estimated"
        ),
        resolve_status=resolved.status,
        es_app_in=es_app,
        es_truth_in=es_truth,
        es_error_in=None if es_app is None or es_truth is None else abs(es_app - es_truth),
        es_hand_in=group.hand_es_in,
        es_vs_hand_in=None
        if es_app is None or group.hand_es_in is None
        else abs(es_app - group.hand_es_in),
        poi_error_in=poi,
    )


def evaluate_photo(
    image_path: Path, truth: PhotoTruth, overlay_dir: Path | None = None
) -> PhotoReport:
    """Run proposal, working image, per-group detection and resolution; score against `truth`."""
    report = PhotoReport(
        name=image_path.stem, status="ok", reference_kind=truth.reference_kind.value
    )
    try:
        img, _ = load_oriented(image_path.read_bytes())
    except (OSError, ImageRejected) as exc:
        return PhotoReport(image_path.stem, "unreadable", str(exc), truth.reference_kind.value)
    height, width = img.shape[:2]
    outside = [
        i for i, (x, y) in enumerate(truth.holes_px) if not (0 <= x < width and 0 <= y < height)
    ]
    if outside:
        return PhotoReport(
            image_path.stem,
            "bad_truth",
            f"holes_px {outside[:5]} lie outside the {width}x{height} oriented image",
            truth.reference_kind.value,
        )

    truth_ref = (
        None
        if truth.reference_h_px_to_plane is None
        else ReferenceResult(
            truth.reference_kind,
            "manual",
            tuple(tuple(float(v) for v in row) for row in truth.reference_h_px_to_plane),
            None,
            (),
        )
    )
    start = time.perf_counter()
    try:
        proposals = propose_candidates(img, truth.reference_kind)
    except ValueError as exc:
        return PhotoReport(image_path.stem, "unreadable", str(exc), truth.reference_kind.value)
    report.proposals = len(proposals)
    if proposals:
        picked, report.proposal_rank = _pick_proposal(proposals, truth)
        reference, report.reference_source = picked.result, "auto"
        report.reference_warnings = list(picked.result.warnings)
    elif truth_ref is not None:
        reference, report.reference_source = truth_ref, "annotated"
    else:
        report.status = "no_reference"
        report.detail = "no reference proposed and the truth file has no confirmed reference"
        return report
    report.scale_status = (
        "manual_reference_needed"
        if report.reference_source != "auto"
        else "no_annotated_reference"
        if truth_ref is None
        else "evaluated"
    )
    if report.scale_status == "evaluated":
        centre = truth.holes_px.mean(axis=0)
        ours = _px_per_in(np.array(reference.h_px_to_plane), centre)
        theirs = _px_per_in(truth.reference_h_px_to_plane, centre)
        report.scale_error_pct = 100.0 * (ours / theirs - 1.0)

    try:
        working, frame, h_px_to_working = build_working_image(img, reference)
    except ValueError as exc:
        report.status, report.detail = "reference_unusable", str(exc)
        return report
    overlay: list[tuple[GroupTruth, list]] = []
    for group in truth.groups:
        roi = None
        if group.roi_px is not None:
            x0, y0, x1, y1 = group.roi_px
            roi = roi_to_working(h_px_to_working, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], frame)
        try:
            blobs, resolved = analyze_group(
                working, frame, truth.bullet_diameter_in, group.shots_expected, roi
            )
        except ValueError as exc:
            report.status, report.detail = "group_failed", f"{group.name}: {exc}"
            return report
        report.groups.append(_score_group(group, truth, blobs, resolved, frame, h_px_to_working))
        overlay.append((group, blobs))
    report.runtime_s = time.perf_counter() - start
    if overlay_dir is not None:
        _write_overlay(
            overlay_dir / f"{report.name}.png", working, frame, h_px_to_working, truth, overlay
        )
    return report


def _write_overlay(out: Path, working, frame, h_px_to_working, truth: PhotoTruth, overlay) -> None:
    """Working image with truth holes (green) and detected centres (red) for visual review."""
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas = working.copy()
    radius = max(3, round(truth.bullet_diameter_in * frame.ppi / 2))
    for x, y in apply_h(h_px_to_working, truth.holes_px):
        cv2.circle(canvas, (round(x), round(y)), radius, (0, 200, 0), 2)
    for _, blobs in overlay:
        for blob in blobs:
            for cx, cy in blob.centers_px:
                cv2.circle(canvas, (round(cx), round(cy)), 4, (0, 0, 255), -1)
    cv2.imwrite(str(out), canvas)


# ----------------------------------------------------------------------------------- summary


def summarize(reports: Sequence[PhotoReport], t: RealThresholds = REAL_P11) -> dict:
    """Pooled P-11 real-photo metrics, the rows that fail, and the rows that could not be evaluated.

    A row with nothing to evaluate (no matched hole for the centre error, no photo with both an
    automatic and an annotated reference for the scale, no group eligible for the ES row) is listed
    under `incomplete` and never counts as a pass.
    """
    scored = [r for r in reports if r.status == "ok"]
    groups = [g for r in scored for g in r.groups]
    pooled = aggregate("real", False, [_RenderScoreView(g) for g in groups]) if groups else None
    scale_errors = [abs(r.scale_error_pct) for r in scored if r.scale_error_pct is not None]
    es_errors = [
        g.es_error_in for g in groups if g.es_error_in is not None and g.shots_estimated == 0
    ]
    hand_errors = [
        g.es_vs_hand_in for g in groups if g.es_vs_hand_in is not None and g.shots_estimated == 0
    ]
    missing_reference = [r.name for r in scored if r.scale_status == "no_annotated_reference"]
    manual_needed = [r.name for r in scored if r.scale_status == "manual_reference_needed"]
    summary = {
        "photos_total": len(reports),
        "photos_scored": len(scored),
        "photos_unscored": [
            {"name": r.name, "status": r.status, "detail": r.detail}
            for r in reports
            if r.status != "ok"
        ],
        "groups": len(groups),
        "holes": sum(g.holes for g in groups),
        "ragged_holes": sum(g.ragged_holes for g in groups),
        "ragged_credited": sum(g.ragged_credited for g in groups),
        "recall": pooled.recall if pooled else float("nan"),
        "center_p50_in": pooled.p50_in if pooled else float("nan"),
        "center_p95_in": pooled.p95_in if pooled else float("nan"),
        "center_max_in": pooled.max_in if pooled else float("nan"),
        "false_positives_max_per_photo": max((r.false_positives for r in scored), default=0),
        "scale_error_max_pct": max(scale_errors, default=float("nan")),
        "scale_error_photos": len(scale_errors),
        "scale_photos_without_annotated_reference": missing_reference,
        "scale_photos_needing_manual_reference": manual_needed,
        "es_error_max_in": max(es_errors, default=float("nan")),
        "es_error_groups": len(es_errors),
        "es_vs_hand_max_in": max(hand_errors, default=float("nan")),
        "es_vs_hand_groups": len(hand_errors),
        "runtime_max_s": max((r.runtime_s for r in scored), default=0.0),
    }
    failures: list[str] = []
    incomplete: list[str] = []
    if summary["photos_unscored"]:
        failures.append(
            "photos that could not be evaluated: "
            + ", ".join(p["name"] for p in summary["photos_unscored"])
        )
    if not groups:
        incomplete.append("no photo was scored")
        return {"summary": summary, "failures": failures, "incomplete": incomplete}
    if summary["recall"] < t.recall:
        failures.append(f"recall {summary['recall']:.3f} < {t.recall}")
    if pooled is not None and math.isnan(pooled.p95_in):
        incomplete.append("center error row: no annotated hole was matched by a shot")
    elif summary["center_p95_in"] > t.center_p95_in:
        failures.append(f"center error p95 {summary['center_p95_in']:.4f} in > {t.center_p95_in}")
    if summary["false_positives_max_per_photo"] > t.false_positives_per_photo:
        failures.append(
            f"false positives {summary['false_positives_max_per_photo']} > {t.false_positives_per_photo} on a photo"
        )
    if scale_errors and summary["scale_error_max_pct"] > t.scale_error_pct:
        failures.append(
            f"reference scale error {summary['scale_error_max_pct']:.2f} % > {t.scale_error_pct}"
        )
    if missing_reference:
        incomplete.append(
            "scale row: no confirmed reference in the truth file of " + ", ".join(missing_reference)
        )
    if not scale_errors:
        incomplete.append(
            "scale row: no photo has both an automatic proposal and an annotated reference"
        )
    if es_errors and summary["es_error_max_in"] > t.es_error_in:
        failures.append(f"group ES error {summary['es_error_max_in']:.3f} in > {t.es_error_in}")
    if not es_errors:
        incomplete.append("group ES row: no group has two or more shots and none estimated")
    if hand_errors and summary["es_vs_hand_max_in"] > t.es_error_in:
        failures.append(
            f"app ES vs hand_es_in {summary['es_vs_hand_max_in']:.3f} in > {t.es_error_in}"
        )
    if summary["runtime_max_s"] > t.runtime_s:
        failures.append(f"runtime {summary['runtime_max_s']:.2f} s > {t.runtime_s}")
    return {"summary": summary, "failures": failures, "incomplete": incomplete}


class _RenderScoreView:
    """Adapts a `GroupReport` to the fields `aggregate` reads from a `RenderScore`."""

    def __init__(self, g: GroupReport) -> None:
        self.n_isolated = g.holes
        self.matched_isolated = g.matched
        self.isolated_single = g.matched
        self.isolated_errors_in = g.center_errors_in
        self.false_positives = g.false_positives
        self.pairs: list[dict] = []
        self.clusters: list[dict] = []
        self.es_errors_in: list[float] = []
        self.runtime_s = 0.0


def build_report(reports: Sequence[PhotoReport], t: RealThresholds = REAL_P11) -> dict:
    """JSON-ready report: thresholds, per-photo results, pooled summary and the verdict.

    `verdict` is FAIL when a row misses its limit, INCOMPLETE when a row could not be evaluated, and
    PASS only when every row was evaluated and met.
    """
    result = summarize(reports, t)
    verdict = "FAIL" if result["failures"] else "INCOMPLETE" if result["incomplete"] else "PASS"
    return {
        "schema": REPORT_SCHEMA,
        "thresholds": asdict(t),
        "photos": [asdict(r) for r in reports],
        "summary": result["summary"],
        "failures": result["failures"],
        "incomplete": result["incomplete"],
        "verdict": verdict,
        "passed": verdict == "PASS",
        "provisional": result["summary"]["photos_scored"] < RECOMMENDED_PHOTOS,
    }


def _fmt(value: float | None, spec: str) -> str:
    return (
        "-"
        if value is None or (isinstance(value, float) and math.isnan(value))
        else format(value, spec)
    )


def format_table(reports: Sequence[PhotoReport]) -> str:
    """One line per group plus a header; photos that could not be evaluated get a status line."""
    lines = [
        f"{'photo':<14}{'group':<10}{'ref':<9}{'scale%':>7}{'holes':>6}{'found':>6}{'FP':>4}"
        f"{'p95 in':>8}{'ES app':>8}{'ES err':>8}{'hand':>8}{'s':>6}"
    ]
    for r in reports:
        if r.status != "ok":
            lines.append(f"{r.name:<14}{r.status.upper()}: {r.detail}")
            continue
        for g in r.groups:
            p95 = float(np.percentile(g.center_errors_in, 95)) if g.center_errors_in else None
            lines.append(
                f"{r.name:<14}{g.name:<10}{r.reference_source:<9}{_fmt(r.scale_error_pct, '.2f'):>7}"
                f"{g.holes:>6}{g.matched:>6}{g.false_positives:>4}{_fmt(p95, '.4f'):>8}"
                f"{_fmt(g.es_app_in, '.3f'):>8}{_fmt(g.es_error_in, '.3f'):>8}"
                f"{_fmt(g.es_vs_hand_in, '.3f'):>8}{r.runtime_s:>6.2f}"
            )
    return "\n".join(lines)


# ----------------------------------------------------------------------------------- command line


def find_photos(folder: Path) -> list[Path]:
    """Photos in `folder` (sorted); a truth file is not a photo."""
    return sorted(
        p
        for p in folder.iterdir()
        if p.is_file()
        and p.suffix.lower() in PHOTO_EXTENSIONS
        and not p.name.endswith(TRUTH_SUFFIX)
    )


def truth_path_for(photo: Path) -> Path:
    """`<stem>.truth.json` next to the photo."""
    return photo.with_name(photo.stem + TRUTH_SUFFIX)


def _verdict_line(report: dict) -> str:
    if report["verdict"] == "PASS":
        return "PASS"
    reasons = report["failures"] + report["incomplete"]
    return f"{report['verdict']}: " + "; ".join(reasons)


def main(argv: Sequence[str] | None = None) -> int:
    """Evaluate the annotated photos in `--dir` and return the exit code (module docstring).

    Prints a per-group table, the pooled summary and the verdict (PASS, FAIL or INCOMPLETE);
    `--json` also writes the full report.
    """
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default=str(REAL_PHOTO_DIR))
    ap.add_argument("--json", default="")
    ap.add_argument("--overlay-dir", default="")
    ap.add_argument(
        "--scale-pct",
        type=float,
        default=REAL_P11.scale_error_pct,
        help="reference scale error limit in percent",
    )
    ap.add_argument(
        "--coarse",
        action="store_true",
        help="also compare unannotated photos with tests/real/counts.json",
    )
    args = ap.parse_args(argv)

    folder = Path(args.dir)
    photos = find_photos(folder) if folder.is_dir() else []
    annotated = [p for p in photos if truth_path_for(p).is_file()]
    if not annotated:
        reason = (
            f"{folder} does not exist"
            if not folder.is_dir()
            else f"{folder} has no photos"
            if not photos
            else f"{len(photos)} photo(s) in {folder} but no <photo>{TRUTH_SUFFIX} file"
        )
        print(f"{OUTSTANDING_MESSAGE}: {reason} (format: tests/real/README.md)", file=sys.stderr)
        if args.coarse and photos:
            _print_coarse(folder)
        return EXIT_OUTSTANDING

    truths: dict[Path, PhotoTruth] = {}
    problems = []
    for photo in annotated:
        try:
            truths[photo] = load_truth(truth_path_for(photo))
        except TruthError as exc:
            problems.append(str(exc))
    if problems:
        print("invalid truth file(s):\n  " + "\n  ".join(problems), file=sys.stderr)
        return EXIT_USAGE

    overlay = Path(args.overlay_dir) if args.overlay_dir else None
    reports = [evaluate_photo(p, truths[p], overlay) for p in annotated]
    skipped = [p.name for p in photos if p not in truths]
    print(format_table(reports))
    report = build_report(reports, replace(REAL_P11, scale_error_pct=args.scale_pct))
    s = report["summary"]
    print(
        f"\n{s['photos_scored']}/{s['photos_total']} photos scored, {s['holes']} holes in {s['groups']} groups: "
        f"recall {_fmt(s['recall'], '.3f')}, centre p95 {_fmt(s['center_p95_in'], '.4f')} in, "
        f"max FP/photo {s['false_positives_max_per_photo']}, scale err max {_fmt(s['scale_error_max_pct'], '.2f')} %, "
        f"ES err max {_fmt(s['es_error_max_in'], '.3f')} in, runtime max {s['runtime_max_s']:.2f} s"
    )
    if skipped:
        print(f"not annotated (not scored): {', '.join(skipped)}")
    if report["provisional"]:
        print(f"provisional: the gate expects {RECOMMENDED_PHOTOS}-20 annotated photos")
    print(_verdict_line(report))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    if args.coarse and skipped:
        _print_coarse(folder)
    return EXIT_PASS if report["passed"] else EXIT_FAIL


def _print_coarse(folder: Path) -> None:
    counts = load_real_counts()
    print("\ncoarse mode: found vs approximate visual count (tests/real/counts.json), not gated")
    print(format_real_table(real_photo_scores(folder, counts=counts), counts))


if __name__ == "__main__":
    sys.exit(main())
