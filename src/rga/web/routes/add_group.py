"""The dedicated New group page, `GET /groups/new?rifle_id=&photo_id=`: the "Add new group" dialog
uploads the target photo and lands here, on that rifle's group form with the photo attached.
Adding the group continues to the photo's reference step, then aim and shots.

Routers are included in module-name order; this module sorts before `analysis`, whose
`/groups/{group_id}` page would otherwise take `/groups/new`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from rga.services import group_records as group_service
from rga.services import photos as photo_service
from rga.services import rifles as rifle_service
from rga.services.groups import WEATHER_PROMPT_MIN_YD
from rga.web.deps import get_db

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_db)]
MAX_ID = 2**63 - 1


def pending_photo(conn: sqlite3.Connection, photo_id: int | None) -> dict | None:
    """The photo uploaded from "Add new group", handed to the group form; None when unknown."""
    if photo_id is None or not 0 < photo_id <= MAX_ID:
        return None
    try:
        photo = photo_service.get_photo(conn, photo_id)
    except photo_service.PhotoNotFound:
        return None
    return {
        "id": photo.id,
        "taken_at": photo.taken_at,
        "taken_at_offset": photo.taken_at_offset,
        "reference_confirmed": bool(photo.reference_confirmed),
    }


def form_state(
    detail: rifle_service.RiflePage,
    defaults: group_service.GroupDefaults,
    photo: dict | None,
    return_to: str,
    photo_confirmed: dict[str, bool] | None = None,
) -> dict:
    """What group_form.js reads from the form card's `data-state`."""
    return {
        "rifle_id": detail.rifle.id,
        "today": date.today().isoformat(),
        "defaults": asdict(defaults),
        "weather_prompt_yd": WEATHER_PROMPT_MIN_YD,
        "lots": {str(e.load.id): [lot.lot_code for lot in e.lots] for e in detail.loads},
        "photo_confirmed": photo_confirmed or {},
        "pending_photo": photo,
        "return_to": return_to,
    }


@router.get("/groups/new", response_class=HTMLResponse, response_model=None)
def new_group_page(
    request: Request,
    conn: Conn,
    rifle_id: Annotated[int | None, Query()] = None,
    photo_id: Annotated[int | None, Query()] = None,
) -> HTMLResponse | RedirectResponse:
    """The group form of one rifle with the dialog's photo attached. Without a known rifle that has
    a load there is nothing to add to, so the browser goes back to the Groups tab."""
    if rifle_id is None or not 0 < rifle_id <= MAX_ID:
        return RedirectResponse("/groups", status_code=303)
    try:
        detail = rifle_service.rifle_page_data(conn, rifle_id)
    except rifle_service.RifleNotFound:
        return RedirectResponse("/groups", status_code=303)
    if not detail.loads:
        return RedirectResponse(f"/rifles/{rifle_id}", status_code=303)
    defaults = group_service.group_defaults(conn, rifle_id)
    photo = pending_photo(conn, photo_id)
    state = form_state(detail, defaults, photo, "/groups")
    return request.app.state.templates.TemplateResponse(
        request,
        "group_new.html",
        {
            "active": "groups",
            "detail": detail,
            "defaults": defaults,
            "pending_photo": photo,
            "position_labels": group_service.POSITION_LABELS,
            "today": date.today().isoformat(),
            "state_json": json.dumps(state),
        },
    )
