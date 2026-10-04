"""Groups and suppressors: JSON API (group create, patch, defaults; remembered suppressors)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from rga.services import group_records as service
from rga.services.groups import GroupError, masks_poi
from rga.web.app import ApiError
from rga.web.deps import get_db
from rga.web.schemas import (
    ROUTE_SCHEMAS,
    GroupCreate,
    GroupDefaultsOut,
    GroupOut,
    GroupUpdate,
    PlanePointOut,
    RoiPxOut,
    SuppressorCreate,
    SuppressorOut,
)

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_db)]
BUSY_RETRY_SECONDS = "1"


def _response(method: str, path: str):
    return ROUTE_SCHEMAS[(method, path)].response


def _status_for(code: str) -> int:
    if code.endswith("_not_found"):
        return 404
    if code.startswith("invalid_"):
        return 422
    return 409


@contextmanager
def _mapped_errors() -> Iterator[None]:
    """Turn service and database errors into `ApiError`: 404, 422, 409 (link and constraint
    errors included) and 503 `busy` with a retry hint; never a 500."""
    try:
        yield
    except GroupError as exc:
        raise ApiError(_status_for(exc.code), exc.code, exc.message) from exc
    except service.LINK_ERRORS as exc:
        code = next(code for kind, code in service.LINK_ERROR_CODES if isinstance(exc, kind))
        raise ApiError(409, code, str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise ApiError(
            409, "constraint_violation", "The change conflicts with stored data"
        ) from exc
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise ApiError(
                503,
                "busy",
                "The database is busy; wait a moment and try again.",
                headers={"Retry-After": BUSY_RETRY_SECONDS},
            ) from exc
        raise


def group_context(group, rifle_id: int, suppressor_name: str | None) -> dict:
    """The context fields shared by `GroupOut` and `GroupDetailOut` for a group row: its date and
    time, distance, position, suppressor (with its name), weather and the 200 yd weather prompt."""
    return {
        "rifle_id": rifle_id,
        "shot_date": group.shot_date,
        "shot_time": group.shot_time,
        "utc_offset": group.utc_offset,
        "shot_at_source": group.shot_at_source,
        "distance_yd": group.distance_yd,
        "position": group.position,
        "suppressor": bool(group.suppressor),
        "suppressor_id": group.suppressor_id,
        "suppressor_name": suppressor_name,
        "temperature_f": group.temperature_f,
        "density_altitude_ft": group.density_altitude_ft,
        "wind_mph": group.wind_mph,
        "wind_clock": group.wind_clock,
        "weather_skipped": bool(group.weather_skipped),
        "weather_prompt": service.weather_prompt(group.distance_yd),
    }


def group_out(view: service.GroupView) -> GroupOut:
    """Build a `GroupOut` from a group view.

    `masked` is true when `masks_poi` hides the point of impact for the group's purpose and stored
    shot count; the aim point is then withheld (null), since aim plus shot positions would give
    the offset back.
    """
    group = view.group
    masked = masks_poi(group.purpose, view.shot_count)
    return GroupOut(
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
        if not masked and group.aim_x_in is not None and group.aim_y_in is not None
        else None,
        purpose=group.purpose,
        zero_id=group.zero_id,
        status=group.status,
        notes=group.notes,
        new_lot=view.new_lot,
        masked=masked,
        finalized_at=group.finalized_at,
        created_at=group.created_at,
        updated_at=group.updated_at,
        **group_context(group, view.rifle_id, view.suppressor_name),
    )


@router.get("/api/suppressors", response_model=_response("GET", "/api/suppressors"))
def get_suppressors(conn: Conn) -> list[SuppressorOut]:
    """Every remembered suppressor, by name."""
    with _mapped_errors():
        return [SuppressorOut.model_validate(s) for s in service.list_suppressors(conn)]


@router.post("/api/suppressors", response_model=_response("POST", "/api/suppressors"))
def post_suppressor(body: SuppressorCreate, conn: Conn, response: Response) -> SuppressorOut:
    """Remember a suppressor: 201 when created, 200 with the saved record when the name matches one
    already saved (any letter case)."""
    with _mapped_errors():
        row, created = service.create_suppressor(conn, body.name)
    response.status_code = 201 if created else 200
    return SuppressorOut.model_validate(row)


@router.post("/api/groups", status_code=201, response_model=_response("POST", "/api/groups"))
def post_group(body: GroupCreate, conn: Conn) -> GroupOut:
    """Create a draft group with its own context; the lot is created when its code is new and
    `new_lot` is prospective. A missing `shot_date` is taken from the photo's capture time."""
    with _mapped_errors():
        return group_out(service.create_group(conn, **dict(body)))


@router.get(
    "/api/rifles/{rifle_id}/group-defaults",
    response_model=_response("GET", "/api/rifles/{id}/group-defaults"),
)
def get_group_defaults(rifle_id: int, conn: Conn) -> GroupDefaultsOut:
    """Values to pre-fill a new group of the rifle (all null when there is no earlier group)."""
    with _mapped_errors():
        return GroupDefaultsOut.model_validate(service.group_defaults(conn, rifle_id))


@router.patch("/api/groups/{group_id}", response_model=_response("PATCH", "/api/groups/{id}"))
def patch_group(group_id: int, body: GroupUpdate, conn: Conn) -> GroupOut:
    """Change the given group fields: context (date, time, distance, position, suppressor, weather),
    expected count, elevation, photo and notes. A distance change recomputes the group's offsets."""
    with _mapped_errors():
        return group_out(service.update_group(conn, group_id, body.model_dump(exclude_unset=True)))
