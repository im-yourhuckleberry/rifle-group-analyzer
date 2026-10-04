"""Dope table: `GET /api/loads/{id}/dope` and the `/dope` page (a load picker without `load_id`)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from rga.services import dope as service
from rga.web.app import ApiError
from rga.web.deps import get_db
from rga.web.schemas import ROUTE_SCHEMAS, DopeOut, DopeRowOut

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_db)]


@contextmanager
def _mapped_errors() -> Iterator[None]:
    """Turn service and database errors into `ApiError`: 404, 503 `busy`; never a 500."""
    try:
        yield
    except service.RifleError as exc:
        status = 404 if exc.code.endswith("_not_found") else 409
        raise ApiError(status, exc.code, exc.message) from exc
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise ApiError(503, "busy", "The database is busy; try again") from exc
        raise


def _rows(table: service.DopeTable) -> list[DopeRowOut]:
    return [
        DopeRowOut(
            group_id=line.group_id,
            shot_date=line.shot_date,
            shot_time=line.shot_time,
            distance_yd=line.distance_yd,
            elevation_dialed_mil=line.elevation_dialed_mil,
            center_dy_mil=line.center_dy_mil,
            centered_elevation_mil=line.centered_elevation_mil,
            shots=line.shots,
            includes_estimated=line.includes_estimated,
        )
        for line in table.lines
    ]


@router.get(
    "/api/loads/{load_id}/dope",
    response_model=ROUTE_SCHEMAS[("GET", "/api/loads/{id}/dope")].response,
)
def get_dope(load_id: int, conn: Conn) -> DopeOut:
    """Elevation dialed, group center and centered elevation per finalized group of the load."""
    with _mapped_errors():
        table = service.dope_table(conn, load_id)
    return DopeOut(load_id=load_id, rows=_rows(table))


@router.get("/dope", response_class=HTMLResponse)
def dope_page(request: Request, conn: Conn, load_id: int | None = None) -> HTMLResponse:
    """The dope page for `load_id`, or a load picker when none is given."""
    with _mapped_errors():
        if load_id is None:
            context = {"active": "dope", "load": None, "choices": service.load_choices(conn)}
        else:
            table = service.dope_table(conn, load_id)
            context = {
                "active": "dope",
                "load": table.load,
                "rifle": table.rifle,
                "choices": service.load_choices(conn),
            }
    return request.app.state.templates.TemplateResponse(request, "dope.html", context)
