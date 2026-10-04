"""Review-editor service: hole detection proposals, shortfall fill, aim snap and the group view.

Detection and fill only propose shots (nothing but a group's region is stored); the user's reviewed
state is saved through `services.groups.save_review` and finalized through `groups.finalize`.
Positions are plane inches (origin top-left, y down); working px = inches * ppi.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

import cv2
import numpy as np

from rga.config import Settings
from rga.db import repo_groups, repo_rifles
from rga.db.rows import GroupRow, LoadRow, PhotoRow, RejectionRow, RifleRow, ShotRow
from rga.domain.types import Blob, BlobKind, ShotOrigin, WorkingFrame
from rga.services import groups as group_service
from rga.services.groups import GroupError, GroupNotFound, _write_transaction
from rga.vision.aim import propose_aims
from rga.vision.pipeline import analyze_group, shots_from_resolve
from rga.vision.ragged import resolve_shots
from rga.vision.reference_circle import CIRCLE_KINDS, refine_circle_near
from rga.vision.transform import apply_h

RoiPx = tuple[int, int, int, int]
SNAP_REACH_IN = 0.5
SHOT_REGION_PAD_IN = 0.3
_LOG = logging.getLogger(__name__)


class ReviewError(GroupError):
    """A review operation cannot run; `code` names why (see the route's status mapping)."""


@dataclass(frozen=True)
class ProposedShot:
    """A proposed shot in plane inches; `seq` is its 1-based position in the proposal."""

    seq: int
    x_in: float
    y_in: float
    origin: ShotOrigin
    confidence: float | None


@dataclass(frozen=True)
class ProposedAim:
    """A proposed aim point in plane inches; `kind` names the mark it was found on."""

    kind: str
    x_in: float
    y_in: float
    score: float


@dataclass(frozen=True)
class Proposal:
    """Result of detect or fill-shortfall: shots to review, the resolve status and messages.

    `aim_candidates` is filled by detect while the group has no aim, never by fill-shortfall.
    """

    status: str
    messages: tuple[str, ...]
    shots: tuple[ProposedShot, ...]
    frame: WorkingFrame
    bullet_diameter_in: float
    aim_candidates: tuple[ProposedAim, ...] = ()


@dataclass(frozen=True)
class SnapResult:
    """An aim click after snapping; `snapped` is False when the raw click is echoed."""

    snapped: bool
    x_px: float
    y_px: float
    x_in: float
    y_in: float


@dataclass(frozen=True)
class GroupView:
    """Everything the review editor and `GroupDetailOut` need about one group.

    `shots` are masked per `masks_poi`; `frame` is None until the photo's reference is confirmed.
    `other_groups_on_photo` counts the other groups that share this group's photo (0 without one);
    `suppressor_name` is the name of the group's suppressor, None when it has none.
    """

    group: GroupRow
    lot_code: str
    load: LoadRow
    rifle: RifleRow
    shots: list[ShotRow]
    rejections: list[RejectionRow]
    photo: PhotoRow | None
    frame: WorkingFrame | None
    roi: RoiPx | None
    new_lot: bool
    bullet_diameter_in: float
    other_groups_on_photo: int = 0
    suppressor_name: str | None = None


def roi_from_json(text: str | None) -> RoiPx | None:
    """Parse a stored `roi_json`; None for empty or malformed values."""
    if not text:
        return None
    try:
        data = json.loads(text)
        roi = (int(data["x0"]), int(data["y0"]), int(data["x1"]), int(data["y1"]))
    except ValueError, KeyError, TypeError:
        return None
    return roi if roi[2] > roi[0] and roi[3] > roi[1] else None


def roi_to_json(roi: RoiPx) -> str:
    """Serialize a working-px ROI as stored in `groups.roi_json`."""
    return json.dumps(dict(zip(("x0", "y0", "x1", "y1"), roi, strict=True)))


def _frame_of(photo: PhotoRow | None) -> WorkingFrame | None:
    if photo is None or not photo.reference_confirmed:
        return None
    if photo.working_ppi is None or photo.working_w is None or photo.working_h is None:
        return None
    return WorkingFrame(ppi=photo.working_ppi, width_px=photo.working_w, height_px=photo.working_h)


def group_view(conn: sqlite3.Connection, group_id: int) -> GroupView:
    """Load a group with its context for the editor. Raises `GroupNotFound`."""
    bundle = group_service.load_group(conn, group_id)
    group = bundle.group
    lot = repo_rifles.get_lot(conn, group.lot_id)
    photo = repo_groups.get_photo(conn, group.photo_id) if group.photo_id is not None else None
    return GroupView(
        group=group,
        lot_code=lot.lot_code if lot else "",
        load=bundle.load,
        rifle=bundle.rifle,
        shots=bundle.shots,
        rejections=repo_groups.list_rejections(conn, group_id),
        photo=photo,
        frame=_frame_of(photo),
        roi=roi_from_json(group.roi_json),
        new_lot=group_service.new_lot_flag(conn, group_id),
        bullet_diameter_in=bundle.rifle.bullet_diameter_in,
        other_groups_on_photo=_other_groups_on_photo(conn, group),
        suppressor_name=_suppressor_name(conn, group),
    )


def _suppressor_name(conn: sqlite3.Connection, group: GroupRow) -> str | None:
    if group.suppressor_id is None:
        return None
    suppressor = repo_groups.get_suppressor(conn, group.suppressor_id)
    return suppressor.name if suppressor else None


def _other_groups_on_photo(conn: sqlite3.Connection, group: GroupRow) -> int:
    if group.photo_id is None:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) FROM groups WHERE photo_id = ? AND id <> ?", (group.photo_id, group.id)
    ).fetchone()
    return int(row[0])


def _working_image(settings: Settings, photo: PhotoRow, frame: WorkingFrame) -> np.ndarray:
    path = Path(settings.working_dir) / f"{photo.id}.png"
    image = cv2.imread(str(path), cv2.IMREAD_COLOR) if path.is_file() else None
    if image is None:
        raise ReviewError("working_image_missing", "the working image for this photo is missing")
    if image.shape[1] != frame.width_px or image.shape[0] != frame.height_px:
        raise ReviewError(
            "working_image_mismatch", "the working image does not match the photo's reference"
        )
    return image


def _clamped_roi(roi: RoiPx, frame: WorkingFrame) -> RoiPx:
    x0, y0, x1, y1 = roi
    clamped = (max(0, x0), max(0, y0), min(frame.width_px, x1), min(frame.height_px, y1))
    if clamped[2] <= clamped[0] or clamped[3] <= clamped[1]:
        raise ReviewError("invalid_roi", "the region lies outside the photo")
    return clamped


def _without_rejected(
    blobs: list[Blob], rejections: list[RejectionRow], frame: WorkingFrame, radius_px: float
) -> list[Blob]:
    """Drop detections the user already rejected (a centre within `radius_px` of a rejection)."""
    points = [(r.x_in * frame.ppi, r.y_in * frame.ppi) for r in rejections]

    def rejected(center: tuple[float, float]) -> bool:
        return any(
            (center[0] - px) ** 2 + (center[1] - py) ** 2 <= radius_px**2 for px, py in points
        )

    kept: list[Blob] = []
    for blob in blobs:
        if blob.kind is BlobKind.RAGGED:
            if not (blob.centers_px and rejected(blob.centers_px[0])):
                kept.append(blob)
            continue
        centers = tuple(c for c in blob.centers_px if not rejected(c))
        if centers:
            kept.append(replace(blob, centers_px=centers))
    return kept


def _reference_center_px(photo: PhotoRow, frame: WorkingFrame) -> tuple[float, float] | None:
    """Working-px centre of the photo's confirmed circle reference (the plane origin), if any."""
    if photo.reference_kind not in {kind.value for kind in CIRCLE_KINDS}:
        return None
    if not photo.h_px_to_plane_json or photo.width_px <= 0 or photo.height_px <= 0:
        return None
    h = np.array(json.loads(photo.h_px_to_plane_json), dtype=np.float64)
    width, height = photo.width_px, photo.height_px
    plane = apply_h(
        h, np.array([[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float64)
    )
    low = plane.min(axis=0)
    return float(-low[0] * frame.ppi), float(-low[1] * frame.ppi)


def _aim_region(
    explicit: RoiPx | None, shots: tuple[ProposedShot, ...], frame: WorkingFrame
) -> RoiPx | None:
    """The region aims are searched around: the group's region, else the proposed shots' box."""
    if explicit is not None:
        return explicit
    if not shots:
        return None
    pad = SHOT_REGION_PAD_IN * frame.ppi
    xs = [s.x_in * frame.ppi for s in shots]
    ys = [s.y_in * frame.ppi for s in shots]
    try:
        return _clamped_roi(
            (int(min(xs) - pad), int(min(ys) - pad), int(max(xs) + pad), int(max(ys) + pad)), frame
        )
    except ReviewError:
        return None


def _aim_candidates(
    view: GroupView,
    working: np.ndarray,
    frame: WorkingFrame,
    region: RoiPx | None,
    shots: tuple[ProposedShot, ...],
) -> tuple[ProposedAim, ...]:
    """Aim candidates for a group without an aim; best-effort, so any failure gives none."""
    if view.group.aim_x_in is not None or view.photo is None:
        return ()
    try:
        target = _aim_region(region, shots, frame)
        if target is None:
            return ()
        shot_points = [(s.x_in * frame.ppi, s.y_in * frame.ppi) for s in shots]
        shot_points += [(s.x_in * frame.ppi, s.y_in * frame.ppi) for s in view.shots]
        found = propose_aims(
            working,
            target,
            frame.ppi,
            _reference_center_px(view.photo, frame),
            bullet_diameter_in=view.bullet_diameter_in,
            shots_px=shot_points,
        )
    except Exception:
        _LOG.warning("aim proposals skipped for group %s", view.group.id, exc_info=True)
        return ()
    return tuple(
        ProposedAim(c.kind, c.point_px[0] / frame.ppi, c.point_px[1] / frame.ppi, c.score)
        for c in found
    )


def _propose(
    conn: sqlite3.Connection,
    settings: Settings,
    group_id: int,
    roi: RoiPx | None,
    with_aims: bool = False,
) -> Proposal:
    view = group_view(conn, group_id)
    group = view.group
    if group.status == "final":
        raise ReviewError("group_final", "a finalized group can no longer be reviewed")
    if view.photo is None:
        raise ReviewError("no_photo", "this group has no photo: add the shots by hand")
    if view.frame is None:
        raise ReviewError(
            "reference_unconfirmed", "confirm the reference object on the photo before detecting"
        )
    frame = view.frame
    explicit = _clamped_roi(roi, frame) if roi is not None else None
    effective = explicit or (_clamped_roi(view.roi, frame) if view.roi else None)
    working = _working_image(settings, view.photo, frame)
    diameter = view.bullet_diameter_in
    try:
        blobs, resolved = analyze_group(working, frame, diameter, group.shots_expected, effective)
    except ValueError as error:
        raise ReviewError(
            "image_too_small", f"the photo is too small to analyze: {error}"
        ) from error
    if view.rejections:
        remaining = _without_rejected(blobs, view.rejections, frame, diameter * frame.ppi / 2.0)
        if remaining != blobs:
            resolved = resolve_shots(remaining, group.shots_expected, diameter * frame.ppi)
    if explicit is not None:
        with _write_transaction(conn):
            repo_groups.update_group(conn, group_id, roi_json=roi_to_json(explicit))
    shots = tuple(
        ProposedShot(seq, s.point.x_in, s.point.y_in, s.origin, s.confidence)
        for seq, s in enumerate(shots_from_resolve(resolved, frame), start=1)
    )
    aims = _aim_candidates(view, working, frame, effective, shots) if with_aims else ()
    return Proposal(resolved.status, resolved.messages, shots, frame, diameter, aims)


def detect(
    conn: sqlite3.Connection, settings: Settings, group_id: int, roi: RoiPx | None = None
) -> Proposal:
    """Detect holes in the group's photo and propose `shots_expected` shots (nothing is saved).

    `roi` (working px) restricts detection and is stored as the group's region; without it the
    stored region (or the whole photo) is used. Detections the user already rejected are not
    proposed again. While the group has no aim the proposal also carries ranked aim candidates
    searched around the region (or the proposed shots). With no holes found the proposal has no
    shots and a message to add them by hand. Raises `ReviewError` (`group_final`, `no_photo`, `reference_unconfirmed`,
    `working_image_missing`, `invalid_roi`, `image_too_small`) and `GroupNotFound`.
    """
    return _propose(conn, settings, group_id, roi, with_aims=True)


def fill_shortfall(conn: sqlite3.Connection, settings: Settings, group_id: int) -> Proposal:
    """Re-run detection with the group's stored expected count and region, filling any shortfall
    as estimated shots inside ragged holes. Save the new expected count first. Nothing is saved.
    """
    return _propose(conn, settings, group_id, None)


def snap_aim(
    conn: sqlite3.Connection, settings: Settings, photo_id: int, x_px: float, y_px: float
) -> SnapResult:
    """Snap a click (working px) to the centre of the aim mark near it, or echo the click.

    Reach is half an inch around the click. Raises `ReviewError` (`photo_not_found`,
    `reference_unconfirmed`, `working_image_missing`, `invalid_point`).
    """
    photo = repo_groups.get_photo(conn, photo_id)
    if photo is None:
        raise ReviewError("photo_not_found", f"photo {photo_id} does not exist")
    frame = _frame_of(photo)
    if frame is None:
        raise ReviewError(
            "reference_unconfirmed",
            "confirm the reference object on the photo before marking the aim",
        )
    working = _working_image(settings, photo, frame)
    if not (0 <= x_px < frame.width_px and 0 <= y_px < frame.height_px):
        raise ReviewError("invalid_point", "the click lies outside the photo")
    try:
        mark = refine_circle_near(working, (x_px, y_px), SNAP_REACH_IN * frame.ppi)
    except ValueError as error:
        raise ReviewError("invalid_point", str(error)) from error
    cx, cy = mark.center_px if mark is not None else (float(x_px), float(y_px))
    return SnapResult(mark is not None, cx, cy, cx / frame.ppi, cy / frame.ppi)


__all__ = [
    "GroupError",
    "GroupNotFound",
    "GroupView",
    "ProposedShot",
    "Proposal",
    "ReviewError",
    "SnapResult",
    "detect",
    "fill_shortfall",
    "group_view",
    "roi_from_json",
    "roi_to_json",
    "snap_aim",
]
