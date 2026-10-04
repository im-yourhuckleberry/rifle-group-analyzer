"""Rifles, loads and lots: JSON API and the `/rifles` and `/rifles/{id}` pages."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from rga.domain.types import LoadKind, Position
from rga.services import group_records as group_service
from rga.services import rifles as service
from rga.services import zeroing as zeroing_service
from rga.services.groups import WEATHER_PROMPT_MIN_YD
from rga.web.app import ApiError
from rga.web.deps import get_db
from rga.web.schemas import (
    ROUTE_SCHEMAS,
    CaliberOut,
    LoadCreate,
    LoadOut,
    LoadUpdate,
    LotCreate,
    LotOut,
    RifleCreate,
    RifleDetailOut,
    RifleOut,
    RifleUpdate,
    ZeroLoadSet,
)

router = APIRouter()
ZERO_STATE_LABELS = {
    "step1": "Step 1: first group",
    "step2_initial": "Step 2: initial group",
    "step2_confirm": "Step 2: confirm group",
    "step3_dot": "Step 3: dot",
    "complete": "Complete",
    "superseded": "Superseded",
}
Conn = Annotated[sqlite3.Connection, Depends(get_db)]


def _response(method: str, path: str):
    return ROUTE_SCHEMAS[(method, path)].response


@contextmanager
def _mapped_errors() -> Iterator[None]:
    """Turn service and database errors into `ApiError`: 404, 422, 409, 503 `busy`; never a 500."""
    try:
        yield
    except service.RifleError as exc:
        if exc.code.endswith("_not_found"):
            status = 404
        elif exc.code.startswith("invalid_"):
            status = 422
        else:
            status = 409
        raise ApiError(status, exc.code, exc.message) from exc
    except sqlite3.IntegrityError as exc:
        raise ApiError(
            409, "constraint_violation", "The change conflicts with stored data"
        ) from exc
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise ApiError(503, "busy", "The database is busy; try again") from exc
        raise


@router.get("/api/calibers", response_model=_response("GET", "/api/calibers"))
def get_calibers() -> list[CaliberOut]:
    """The caliber table used to pre-fill the bullet diameter."""
    return [CaliberOut(cartridge=name, bullet_diameter_in=d) for name, d in service.list_calibers()]


@router.get("/api/rifles", response_model=_response("GET", "/api/rifles"))
def get_rifles(conn: Conn) -> list[RifleOut]:
    """All rifles ordered by name."""
    with _mapped_errors():
        return [RifleOut.model_validate(r) for r in service.list_rifles(conn)]


@router.post("/api/rifles", status_code=201, response_model=_response("POST", "/api/rifles"))
def post_rifle(body: RifleCreate, conn: Conn) -> RifleOut:
    """Create a rifle; the bullet diameter defaults from the caliber table."""
    with _mapped_errors():
        return RifleOut.model_validate(service.create_rifle(conn, **body.model_dump()))


@router.get("/api/rifles/{rifle_id}", response_model=_response("GET", "/api/rifles/{id}"))
def get_rifle(rifle_id: int, conn: Conn) -> RifleDetailOut:
    """One rifle with its loads."""
    with _mapped_errors():
        rifle, loads = service.rifle_with_loads(conn, rifle_id)
    return RifleDetailOut(
        **RifleOut.model_validate(rifle).model_dump(),
        loads=[LoadOut.model_validate(load) for load in loads],
    )


@router.patch("/api/rifles/{rifle_id}", response_model=_response("PATCH", "/api/rifles/{id}"))
def patch_rifle(rifle_id: int, body: RifleUpdate, conn: Conn) -> RifleOut:
    """Change the given rifle fields."""
    with _mapped_errors():
        rifle = service.update_rifle(conn, rifle_id, body.model_dump(exclude_unset=True))
    return RifleOut.model_validate(rifle)


@router.put(
    "/api/rifles/{rifle_id}/zero-load",
    response_model=_response("PUT", "/api/rifles/{id}/zero-load"),
)
def put_zero_load(rifle_id: int, body: ZeroLoadSet, conn: Conn) -> RifleOut:
    """Set or clear the rifle's zero load; a load of another rifle is a 409."""
    with _mapped_errors():
        return RifleOut.model_validate(service.set_zero_load(conn, rifle_id, body.load_id))


@router.get(
    "/api/rifles/{rifle_id}/loads", response_model=_response("GET", "/api/rifles/{id}/loads")
)
def get_loads(rifle_id: int, conn: Conn) -> list[LoadOut]:
    """The rifle's loads."""
    with _mapped_errors():
        return [LoadOut.model_validate(load) for load in service.list_loads(conn, rifle_id)]


@router.post(
    "/api/rifles/{rifle_id}/loads",
    status_code=201,
    response_model=_response("POST", "/api/rifles/{id}/loads"),
)
def post_load(rifle_id: int, body: LoadCreate, conn: Conn) -> LoadOut:
    """Create a factory or handload load; fields of the other kind are a 422."""
    fields = body.model_dump()
    fields["kind"] = body.kind.value
    with _mapped_errors():
        return LoadOut.model_validate(service.create_load(conn, rifle_id, fields))


@router.patch("/api/loads/{load_id}", response_model=_response("PATCH", "/api/loads/{id}"))
def patch_load(load_id: int, body: LoadUpdate, conn: Conn) -> LoadOut:
    """Change the given load fields; the kind cannot change."""
    with _mapped_errors():
        load = service.update_load(conn, load_id, body.model_dump(exclude_unset=True))
    return LoadOut.model_validate(load)


@router.get("/api/loads/{load_id}/lots", response_model=_response("GET", "/api/loads/{id}/lots"))
def get_lots(load_id: int, conn: Conn) -> list[LotOut]:
    """The load's lots."""
    with _mapped_errors():
        return [LotOut.model_validate(lot) for lot in service.list_lots(conn, load_id)]


@router.post("/api/loads/{load_id}/lots", response_model=_response("POST", "/api/loads/{id}/lots"))
def post_lot(load_id: int, body: LotCreate, conn: Conn, response: Response) -> LotOut:
    """Get or create a lot by code: 201 when created, 200 when it already existed."""
    with _mapped_errors():
        lot, created = service.get_or_create_lot(conn, load_id, body.lot_code)
    response.status_code = 201 if created else 200
    return LotOut.model_validate(lot)


def _render(request: Request, template: str, context: dict) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(request, template, context)


@router.get("/rifles", response_class=HTMLResponse)
def rifles_page(request: Request, conn: Conn) -> HTMLResponse:
    """The rifle list with the new-rifle form."""
    with _mapped_errors():
        summaries = service.rifle_summaries(conn)
    return _render(
        request,
        "rifles.html",
        {"active": "rifles", "summaries": summaries, "diameters": service.DIAMETER_CHOICES},
    )


@router.get("/rifles/{rifle_id}", response_class=HTMLResponse)
def rifle_page(rifle_id: int, request: Request, conn: Conn) -> HTMLResponse:
    """One rifle: edit form, zero load, zeroing (start form and its zeros), loads, its groups by day
    and the new-group form (pre-filled from `defaults`; `state_json` feeds the page script)."""
    with _mapped_errors():
        detail = service.rifle_page_data(conn, rifle_id)
        defaults = group_service.group_defaults(conn, rifle_id)
    photos = {photo.id: photo for day in detail.days for photo in day.photos}
    state = {
        "rifle_id": rifle_id,
        "today": date.today().isoformat(),
        "defaults": asdict(defaults),
        "weather_prompt_yd": WEATHER_PROMPT_MIN_YD,
        "lots": {
            str(entry.load.id): [lot.lot_code for lot in entry.lots] for entry in detail.loads
        },
        "photo_confirmed": {
            str(pid): bool(photo.reference_confirmed) for pid, photo in photos.items()
        },
    }
    context = {
        "active": "rifles",
        "detail": detail,
        "defaults": defaults,
        "state_json": json.dumps(state),
        "position_labels": group_service.POSITION_LABELS,
        "zeros": zeroing_service.list_zeros_for_rifle(conn, rifle_id),
        "zero_states": ZERO_STATE_LABELS,
        "positions": [p.value for p in Position],
        "load_kinds": [k.value for k in LoadKind],
        "today": date.today().isoformat(),
        "diameters": service.DIAMETER_CHOICES,
    }
    return _render(request, "rifle_detail.html", context)
