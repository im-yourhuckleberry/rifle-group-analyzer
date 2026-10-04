"""The Settings page and its stored settings: the share-card defaults (`/api/settings/share-defaults`).

The Offline/Online switch on the page is display-only and stores nothing.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from rga.services import settings as service
from rga.web.app import ApiError
from rga.web.deps import get_db
from rga.web.schemas import ROUTE_SCHEMAS, ShareDefaults, ShareDefaultsOut

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_db)]
BUSY_RETRY_SECONDS = "1"
SHARE_LAYER_LABELS = (
    ("photo", "Photo"),
    ("spread", "Cone of fire"),
    ("thing_hitter", "“Thing-hitter” circle"),
    ("poi", "Mean POI"),
    ("correction", "Correction"),
    ("numbers", "Shot numbers"),
)


def _response(method: str, path: str):
    return ROUTE_SCHEMAS[(method, path)].response


@contextmanager
def _mapped_errors() -> Iterator[None]:
    """Turn service and database errors into `ApiError`: 422 for a rejected setting, 503 `busy`
    with a retry hint when the database is locked; never a 500 for those."""
    try:
        yield
    except service.SettingsError as exc:
        raise ApiError(422, exc.code, exc.message) from exc
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise ApiError(
                503,
                "busy",
                "The database is busy; wait a moment and try again.",
                headers={"Retry-After": BUSY_RETRY_SECONDS},
            ) from exc
        raise


@router.get(
    "/api/settings/share-defaults",
    response_model=_response("GET", "/api/settings/share-defaults"),
)
def get_share_defaults(conn: Conn) -> ShareDefaultsOut:
    """The share-card layers that start checked (the built-in set until the user saves one)."""
    with _mapped_errors():
        return ShareDefaultsOut(**service.get_share_defaults(conn))


@router.put(
    "/api/settings/share-defaults",
    response_model=_response("PUT", "/api/settings/share-defaults"),
)
def put_share_defaults(body: ShareDefaults, conn: Conn) -> ShareDefaultsOut:
    """Replace the share-card defaults; all six layers are required booleans."""
    with _mapped_errors():
        return ShareDefaultsOut(**service.set_share_defaults(conn, body.model_dump()))


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, conn: Conn) -> HTMLResponse:
    """Render the Settings page: the display-only mode switch and the share defaults form."""
    with _mapped_errors():
        defaults = service.get_share_defaults(conn)
    context = {
        "active": "settings",
        "share_defaults": defaults,
        "share_layers": SHARE_LAYER_LABELS,
        "state_json": json.dumps({"share_defaults": defaults}),
    }
    return request.app.state.templates.TemplateResponse(request, "settings.html", context)
