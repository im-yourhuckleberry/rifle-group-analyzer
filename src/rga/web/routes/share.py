"""Group share image: `GET /api/groups/{id}/share.png?layers=photo,spread,thing_hitter,poi,correction`.

A PNG card of the group page's plot with the rifle, scope, suppressor, load, date, distance and the
group's specs. Layers the group page would not show are left out; `X-Share-Layers` lists the
layers drawn and `X-Share-Omitted` the requested ones that were not (both comma lists, possibly
empty). Errors use the shared `{detail, code}` body: 404 `group_not_found`, 422 for a bad `layers`
value, 503 `share_busy` / `share_timeout` / `busy`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from pydantic import ValidationError

from rga.config import Settings
from rga.services import share as service
from rga.services.groups import GroupError
from rga.web.app import ApiError
from rga.web.deps import get_db, get_settings
from rga.web.schemas import SQLITE_MAX_INTEGER, ShareQuery

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
GroupId = Annotated[int, Path(ge=1, le=SQLITE_MAX_INTEGER)]
RETRY_AFTER_SECONDS = "1"
PNG_RESPONSE = {200: {"content": {"image/png": {}}, "description": "The share card as a PNG."}}


def _status_for(code: str) -> int:
    if code.endswith("_not_found"):
        return 404
    if code.startswith("invalid_"):
        return 422
    if code in {"share_busy", "share_timeout"}:
        return 503
    return 409


@contextmanager
def _mapped_errors() -> Iterator[None]:
    """Turn service and database errors into `ApiError`: 404, 422, 409 and 503; never a 500."""
    try:
        yield
    except GroupError as exc:
        status = _status_for(exc.code)
        headers = {"Retry-After": RETRY_AFTER_SECONDS} if status == 503 else None
        raise ApiError(status, exc.code, exc.message, headers=headers) from exc
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise ApiError(
                503,
                "busy",
                "The database is busy; wait a moment and try again.",
                headers={"Retry-After": RETRY_AFTER_SECONDS},
            ) from exc
        raise


@router.get("/api/groups/{group_id}/share.png", response_class=Response, responses=PNG_RESPONSE)
def get_share_image(
    group_id: GroupId, request: Request, conn: Conn, settings: AppSettings
) -> Response:
    """The group's share card as a PNG download; `layers` is a comma list (default `photo,spread`)."""
    try:
        query = ShareQuery.model_validate(dict(request.query_params))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ApiError(422, "validation_error", problems) from None
    with _mapped_errors():
        image = service.render_share(conn, group_id, query.layers, settings.working_dir)
    return Response(
        content=image.png,
        media_type="image/png",
        headers={
            "Content-Disposition": f'attachment; filename="group-{group_id}.png"',
            "Cache-Control": "no-store",
            "X-Share-Layers": ",".join(image.shown),
            "X-Share-Omitted": ",".join(image.omitted),
        },
    )
