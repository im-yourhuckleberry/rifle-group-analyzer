"""Photo routes: upload, image download, reference proposal and confirmation, and the page.

The upload body is read as a stream and cut off as soon as it exceeds the limit (413) before the
whole body is received. A reference is never confirmed on the user's behalf: `propose` only
returns candidates and `confirm` records the one the user picked or marked.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from rga.config import Settings
from rga.domain.types import ReferenceKind
from rga.services import photos as photo_service
from rga.services.photos import MAX_UPLOAD_BYTES, PhotoRow, ReferenceProposal
from rga.web.app import ApiError
from rga.web.deps import get_db, get_settings
from rga.web.schemas import (
    CircleOut,
    ImageQuery,
    PhotoOut,
    ReferenceCandidateOut,
    ReferenceConfirmOut,
    ReferenceConfirmRequest,
    ReferenceProposeOut,
    ReferenceProposeRequest,
    ReferenceResetOut,
)
from rga.web.security import safe_return_to

router = APIRouter()

UPLOAD_OVERHEAD_BYTES = 64 * 1024
KIND_LABELS: tuple[tuple[str, str], ...] = (
    (ReferenceKind.GRID_1IN.value, "1 inch grid"),
    (ReferenceKind.CIRCLE_1IN.value, "1 inch circle"),
    (ReferenceKind.CIRCLE_1_5IN.value, "1.5 inch circle"),
    (ReferenceKind.CIRCLE_2IN.value, "2 inch circle"),
    (ReferenceKind.CIRCLE_2_5IN.value, "2.5 inch circle"),
    (ReferenceKind.CIRCLE_3IN.value, "3 inch circle"),
)
MEDIA_TYPES = {".jpg": "image/jpeg", ".png": "image/png"}

Conn = Annotated[sqlite3.Connection, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


@contextmanager
def _mapped_errors() -> Iterator[None]:
    """Turn service errors into `{detail, code}` responses; a locked database is 503 `busy`."""
    try:
        yield
    except photo_service.PhotoError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc):
            raise ApiError(503, "busy", "The database is busy; try again in a moment") from exc
        raise


def _photo_out(photo: PhotoRow, duplicate: bool = False) -> PhotoOut:
    return PhotoOut(
        id=photo.id,
        sha256=photo.sha256,
        ext=photo.ext,
        width_px=photo.width_px,
        height_px=photo.height_px,
        reference_kind=ReferenceKind(photo.reference_kind) if photo.reference_kind else None,
        reference_confirmed=bool(photo.reference_confirmed),
        working_ppi=photo.working_ppi,
        working_w=photo.working_w,
        working_h=photo.working_h,
        axis_ratio=photo.axis_ratio,
        taken_at=photo.taken_at,
        taken_at_offset=photo.taken_at_offset,
        created_at=photo.created_at,
        duplicate=duplicate,
    )


def _manual_kwargs(body: ReferenceConfirmRequest) -> dict | None:
    if body.manual is None:
        return None
    if body.manual.grid is not None:
        grid = body.manual.grid
        return {
            "grid_corners_px": [(p.x_px, p.y_px) for p in grid.corners_px],
            "squares": (grid.squares_x, grid.squares_y),
        }
    return {"edge_points_px": [(p.x_px, p.y_px) for p in body.manual.edge_points_px or []]}


def _candidate_out(index: int, proposal: ReferenceProposal) -> ReferenceCandidateOut:
    circle = None
    if proposal.circle is not None:
        major, minor = sorted(proposal.circle.axes_px, reverse=True)
        circle = CircleOut(
            center_x_px=proposal.circle.center_px[0],
            center_y_px=proposal.circle.center_px[1],
            axis_major_px=major,
            axis_minor_px=minor,
            angle_deg=proposal.circle.angle_deg,
        )
    return ReferenceCandidateOut(
        index=index,
        method=proposal.result.method,
        axis_ratio=proposal.result.axis_ratio,
        warnings=list(proposal.result.warnings),
        circle=circle,
        grid_points_px=[(x, y) for x, y in proposal.grid_points_px]
        if proposal.grid_points_px is not None
        else None,
    )


async def _guarded_stream(request: Request) -> AsyncGenerator[bytes]:
    """Yield the request body, raising 413 as soon as it exceeds the upload limit."""
    limit = MAX_UPLOAD_BYTES + UPLOAD_OVERHEAD_BYTES
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise ApiError(413, "upload_too_large", f"upload exceeds {MAX_UPLOAD_BYTES} bytes")
        yield chunk


def _require_multipart(request: Request) -> None:
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != "multipart/form-data":
        raise ApiError(
            415,
            "unsupported_media_type",
            "Send the image as multipart/form-data with a 'file' field",
        )


async def _read_upload(request: Request) -> bytes:
    _require_multipart(request)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES + UPLOAD_OVERHEAD_BYTES:
        raise ApiError(413, "upload_too_large", f"upload exceeds {MAX_UPLOAD_BYTES} bytes")
    try:
        parser = MultiPartParser(
            request.headers, _guarded_stream(request), max_files=1, max_fields=1, max_part_size=4096
        )
        form = await parser.parse()
    except MultiPartException as exc:
        raise ApiError(
            422, "invalid_upload", f"The upload could not be parsed: {exc.message}"
        ) from exc
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ApiError(422, "missing_file", "Send the image as a multipart field named 'file'")
        data = await upload.read(MAX_UPLOAD_BYTES + 1)
    finally:
        await form.close()
    return data


@router.post("/api/photos", response_model=PhotoOut)
async def upload_photo(
    request: Request, response: Response, conn: Conn, settings: AppSettings
) -> PhotoOut:
    """Store an uploaded JPEG, PNG or HEIC; 201 for a new photo, 200 when the bytes already exist."""
    data = await _read_upload(request)
    with _mapped_errors():
        stored = await run_in_threadpool(photo_service.store_upload, conn, settings, data)
    response.status_code = 200 if stored.duplicate else 201
    return _photo_out(stored.photo, stored.duplicate)


@router.get("/api/photos/{id}/image")
async def photo_image(
    id: int, query: Annotated[ImageQuery, Query()], conn: Conn, settings: AppSettings
) -> FileResponse:
    """The photo's oriented image, or its working image once the reference is confirmed."""
    with _mapped_errors():
        path = await run_in_threadpool(
            photo_service.image_file, conn, settings, id, query.variant.value
        )
    return FileResponse(
        path, media_type=MEDIA_TYPES[path.suffix], headers={"Cache-Control": "no-store"}
    )


@router.post("/api/photos/{id}/reference/propose", response_model=ReferenceProposeOut)
async def propose_reference(
    id: int, body: ReferenceProposeRequest, conn: Conn, settings: AppSettings
) -> ReferenceProposeOut:
    """Ranked reference candidates; nothing is stored or confirmed."""
    with _mapped_errors():
        proposals = await run_in_threadpool(photo_service.propose, conn, settings, id, body.kind)
    return ReferenceProposeOut(
        kind=body.kind,
        candidates=[_candidate_out(i, p) for i, p in enumerate(proposals)],
    )


@router.post("/api/photos/{id}/reference/confirm", response_model=ReferenceConfirmOut)
async def confirm_reference(
    id: int, body: ReferenceConfirmRequest, conn: Conn, settings: AppSettings
) -> ReferenceConfirmOut:
    """Record the candidate the user picked, or their manual marks, and build the working image."""
    manual = _manual_kwargs(body)
    with _mapped_errors():
        confirmed = await run_in_threadpool(
            lambda: photo_service.confirm_reference(
                conn,
                settings,
                id,
                body.kind,
                candidate_index=body.candidate_index,
                manual=manual,
            )
        )
    return ReferenceConfirmOut(photo=_photo_out(confirmed.photo), warnings=list(confirmed.warnings))


@router.post("/api/photos/{id}/reference/reset", response_model=ReferenceResetOut)
def reset_reference(id: int, conn: Conn) -> ReferenceResetOut:
    """Clear the photo's reference and its draft groups' shots, aim, region and rejections.

    409 `reference_in_use` when any group on the photo is final.
    """
    with _mapped_errors():
        result = photo_service.reset_reference(conn, id)
    return ReferenceResetOut(photo=_photo_out(result.photo), cleared_groups=result.cleared_groups)


@router.get("/photos/{id}/reference", response_class=HTMLResponse)
def reference_page(
    id: int,
    request: Request,
    conn: Conn,
    group_id: int | None = None,
    return_to: str | None = None,
) -> HTMLResponse:
    """The reference page: pick or mark the printed reference, then continue to review."""
    with _mapped_errors():
        photo = photo_service.page_photo(conn, id, group_id)
        # On a group's own reference step only the other groups can fix the scale.
        locked = photo_service.reference_locked(conn, id, group_id)
        own_work = group_id is not None and photo_service.group_has_work(conn, group_id)
        preferred = photo_service.preferred_reference_kind(conn)
    # The dropdown starts on this photo's own kind, else the kind past groups used most.
    default_kind = (photo.reference_kind if photo.reference_confirmed else None) or preferred
    back = safe_return_to(return_to) or "/"
    review_url = None
    if group_id is not None:
        review_url = f"/groups/{group_id}/review"
        if safe_return_to(return_to):
            review_url += f"?return_to={quote(back, safe='/')}"
    return request.app.state.templates.TemplateResponse(
        request,
        "reference.html",
        {
            "active": "groups",
            "photo": _photo_out(photo),
            "locked": locked,
            "group_id": group_id,
            "review_url": review_url,
            "return_to": back,
            "kinds": KIND_LABELS,
            "default_kind": default_kind,
            "own_work": own_work and not locked,
        },
    )
