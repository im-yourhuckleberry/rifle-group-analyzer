"""Review editor routes: detect, fill-shortfall, review save, finalize, aim snap, and the page."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from rga.config import Settings
from rga.domain.types import PlanePoint, Shot
from rga.services import groups as group_service
from rga.services import photos as photo_service
from rga.services import review as review_service
from rga.services.groups import GroupError, masks_poi
from rga.web.app import ApiError
from rga.web.deps import get_db, get_settings
from rga.web.routes.groups import group_context
from rga.web.schemas import (
    AimCandidateOut,
    DetectOut,
    DetectRequest,
    GroupDetailOut,
    PlanePointOut,
    RejectionOut,
    ReviewRequest,
    RoiPxOut,
    ShotOut,
    SnapAimOut,
    SnapAimRequest,
    WorkingFrameOut,
)
from rga.web.security import safe_return_to

router = APIRouter()

REFERENCE_IN_USE_REASON = "A finalized group uses this scale"
NOT_FOUND_CODES = frozenset({"group_not_found", "photo_not_found"})
UNPROCESSABLE_CODES = frozenset({"image_too_small"})
Connection = Annotated[sqlite3.Connection, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _status_for(code: str) -> int:
    if code in NOT_FOUND_CODES:
        return 404
    if code.startswith("invalid_") or code in UNPROCESSABLE_CODES:
        return 422
    return 409


@contextmanager
def _translated() -> Iterator[None]:
    """Turn service errors into `ApiError`: group errors by code, lock contention as 503 busy."""
    try:
        yield
    except GroupError as error:
        raise ApiError(_status_for(error.code), error.code, error.message) from error
    except sqlite3.OperationalError as error:
        if "locked" in str(error).lower():
            raise ApiError(503, "busy", "The database is busy; try again in a moment.") from error
        raise


def detail_out(view: review_service.GroupView) -> GroupDetailOut:
    """Build the `GroupDetailOut` body for a group view (shots already masked per `masks_poi`)."""
    group = view.group
    return GroupDetailOut(
        id=group.id,
        public_id=group.public_id,
        load_id=group.load_id,
        lot_id=group.lot_id,
        lot_code=view.lot_code,
        photo_id=group.photo_id,
        roi_px=RoiPxOut(**dict(zip(("x0", "y0", "x1", "y1"), view.roi, strict=True)))
        if view.roi
        else None,
        shots_expected=group.shots_expected,
        elevation_dialed_mil=group.elevation_dialed_mil,
        aim=PlanePointOut(x_in=group.aim_x_in, y_in=group.aim_y_in)
        if group.aim_x_in is not None and group.aim_y_in is not None
        else None,
        purpose=group.purpose,
        zero_id=group.zero_id,
        status=group.status,
        notes=group.notes,
        new_lot=view.new_lot,
        masked=masks_poi(group.purpose, len(view.shots)),
        finalized_at=group.finalized_at,
        created_at=group.created_at,
        updated_at=group.updated_at,
        shots=[
            ShotOut(
                seq=s.seq,
                x_in=s.x_in,
                y_in=s.y_in,
                dx_in=s.dx_in,
                dy_in=s.dy_in,
                dx_mil=s.dx_mil,
                dy_mil=s.dy_mil,
                origin=s.origin,
                moved_by_user=bool(s.moved_by_user),
                confidence=s.confidence,
            )
            for s in view.shots
        ],
        rejections=[
            RejectionOut(id=r.id, x_in=r.x_in, y_in=r.y_in, reason=r.reason)
            for r in view.rejections
        ],
        frame=WorkingFrameOut(
            ppi=view.frame.ppi, width_px=view.frame.width_px, height_px=view.frame.height_px
        )
        if view.frame
        else None,
        bullet_diameter_in=view.bullet_diameter_in,
        load_label=view.load.label,
        legacy_session_notes=group.legacy_session_notes,
        **group_context(group, view.rifle.id, view.suppressor_name),
    )


def _detect_out(proposal: review_service.Proposal) -> DetectOut:
    return DetectOut(
        status=proposal.status,
        messages=list(proposal.messages),
        shots=[
            ShotOut(
                seq=s.seq,
                x_in=s.x_in,
                y_in=s.y_in,
                origin=s.origin,
                confidence=s.confidence,
            )
            for s in proposal.shots
        ],
        frame=WorkingFrameOut(
            ppi=proposal.frame.ppi,
            width_px=proposal.frame.width_px,
            height_px=proposal.frame.height_px,
        ),
        bullet_diameter_in=proposal.bullet_diameter_in,
        aim_candidates=[
            AimCandidateOut(kind=a.kind, x_in=a.x_in, y_in=a.y_in, score=a.score)
            for a in proposal.aim_candidates
        ],
    )


@router.post("/api/groups/{group_id}/detect", response_model=DetectOut)
def detect_holes(
    group_id: int, body: DetectRequest, conn: Connection, settings: AppSettings
) -> DetectOut:
    """Detect holes (optionally in a region) and propose the shots; nothing is saved but the region."""
    roi = (body.roi_px.x0, body.roi_px.y0, body.roi_px.x1, body.roi_px.y1) if body.roi_px else None
    with _translated():
        return _detect_out(review_service.detect(conn, settings, group_id, roi))


@router.post("/api/groups/{group_id}/fill-shortfall", response_model=DetectOut)
def fill_shortfall(group_id: int, conn: Connection, settings: AppSettings) -> DetectOut:
    """Re-run detection with the stored expected count so any shortfall is filled as estimated."""
    with _translated():
        return _detect_out(review_service.fill_shortfall(conn, settings, group_id))


@router.put("/api/groups/{group_id}/review", response_model=GroupDetailOut)
def save_review(group_id: int, body: ReviewRequest, conn: Connection) -> GroupDetailOut:
    """Store the reviewed shots, rejections, aim and expected count of a draft group."""
    shots = [
        Shot(
            PlanePoint(s.x_in, s.y_in),
            s.origin,
            confidence=s.confidence,
            moved_by_user=s.moved_by_user,
        )
        for s in body.shots
    ]
    rejections = [PlanePoint(r.x_in, r.y_in) for r in body.rejections]
    aim = PlanePoint(body.aim.x_in, body.aim.y_in) if body.aim else None
    with _translated():
        group_service.save_review(conn, group_id, aim, shots, rejections, body.shots_expected)
        return detail_out(review_service.group_view(conn, group_id))


@router.post("/api/groups/{group_id}/finalize", response_model=GroupDetailOut)
def finalize_group(group_id: int, conn: Connection) -> GroupDetailOut:
    """Finalize a draft group; 409 `count_mismatch`, `no_aim` or `reference_unconfirmed` if guarded.

    The response carries no aim for a masked group (B-39); the editor's PUT response does.
    """
    with _translated():
        group_service.finalize(conn, group_id)
        detail = detail_out(review_service.group_view(conn, group_id))
    return detail.model_copy(update={"aim": None}) if detail.masked else detail


@router.post("/api/photos/{photo_id}/snap-aim", response_model=SnapAimOut)
def snap_aim(
    photo_id: int, body: SnapAimRequest, conn: Connection, settings: AppSettings
) -> SnapAimOut:
    """Snap a click on the working image to the aim mark's centre (or echo the click)."""
    with _translated():
        result = review_service.snap_aim(conn, settings, photo_id, body.x_px, body.y_px)
    return SnapAimOut(
        snapped=result.snapped,
        x_px=result.x_px,
        y_px=result.y_px,
        x_in=result.x_in,
        y_in=result.y_in,
    )


def _reference_change(conn: sqlite3.Connection, view: review_service.GroupView) -> dict | None:
    """What "Change scale reference" does for this photo: `direct`, `confirm` or `blocked`.

    None when the group has no photo or the photo's reference is not confirmed yet.
    `draft_groups` counts the photo's draft groups with saved shots or an aim; `reason` explains a
    blocked button.
    """
    if view.photo is None or view.frame is None:
        return None
    work = photo_service.reference_work(conn, view.photo.id)
    if work.any_final:
        mode, reason = "blocked", REFERENCE_IN_USE_REASON
    else:
        mode, reason = ("confirm" if work.draft_groups else "direct"), None
    return {"mode": mode, "draft_groups": work.draft_groups, "reason": reason}


def _next_url(group_id: int, destination: str | None) -> str:
    """Where finalizing goes: back into a guided zeroing flow, else the group's share card."""
    if destination is not None and destination.startswith("/zeroing/"):
        return destination
    return f"/groups/{group_id}?share=1"


@router.get("/groups/{group_id}/review", response_class=HTMLResponse)
def review_page(
    request: Request,
    group_id: int,
    conn: Connection,
    return_to: Annotated[str | None, Query()] = None,
):
    """The review editor page; the group's state is embedded as `data-state` for the page script.

    The state is the group detail plus `image_url`, `reference_confirmed`, `other_groups_on_photo`
    (how many other groups share the photo), `reference_change` (the "Change scale reference"
    mode), `return_to` and `next_url`.
    """
    with _translated():
        view = review_service.group_view(conn, group_id)
        reference_change = _reference_change(conn, view)
    detail = detail_out(view)
    destination = safe_return_to(return_to)
    center = review_service.reference_center_in(view)
    state = detail.model_dump(mode="json")
    state.update(
        {
            "image_url": f"/api/photos/{view.photo.id}/image?variant=working"
            if view.photo
            else None,
            "reference_confirmed": view.frame is not None,
            "reference_center": {"x_in": center[0], "y_in": center[1]} if center else None,
            "reference_kind": view.photo.reference_kind if view.photo else None,
            "other_groups_on_photo": view.other_groups_on_photo,
            "reference_change": reference_change,
            "return_to": destination,
            "next_url": _next_url(group_id, destination),
        }
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "review.html",
        {
            "active": "groups",
            "group_id": group_id,
            "return_to": destination,
            "state_json": json.dumps(state),
            "is_final": view.group.status == "final",
        },
    )
