"""Zeroing routes: the `/api/zeros` endpoints and the `/zeroing/{zero_id}` stepper page.

Handlers call `rga.services.zeroing` only. `advice_out` is the one place a domain `Advice` becomes
an `AdviceOut`: advice that asks for more shots or is invalid never carries a correction or a mean
POI, only size-only stats.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import HTMLResponse

from rga.domain.types import AdviceKind, GroupPurpose, GroupStats, GroupStatus, ZeroState
from rga.services import group_records
from rga.services import zeroing as service
from rga.services.groups import GroupError, SizeOnlyStats
from rga.web.app import ApiError
from rga.web.deps import get_db
from rga.web.routes.groups import group_out
from rga.web.schemas import (
    ROUTE_SCHEMAS,
    AdviceOut,
    CorrectionOut,
    GroupOut,
    GroupStatsOut,
    SizeOnlyStatsOut,
    ZeroCheckCreate,
    ZeroCheckGroupCreate,
    ZeroCheckOut,
    ZeroCreate,
    ZeroDialed,
    ZeroDot,
    ZeroGroupOut,
    ZeroLinkGroup,
    ZeroOut,
)

router = APIRouter()
ZeroId = Annotated[int, Path(ge=1, le=service.SQLITE_MAX_INTEGER)]
GroupId = Annotated[int, Path(ge=1, le=service.SQLITE_MAX_INTEGER)]
Db = Annotated[sqlite3.Connection, Depends(get_db)]
_NO_NUMBER_KINDS = frozenset({AdviceKind.SHOOT_MORE, AdviceKind.INVALID})


def _status_for(code: str) -> int:
    if code.endswith("_not_found"):
        return 404
    if code.startswith("invalid_"):
        return 422
    return 409


@contextmanager
def _api_errors() -> Iterator[None]:
    """Map service errors to `{detail, code}` responses; a locked database is 503 `busy`."""
    try:
        yield
    except GroupError as exc:
        raise ApiError(_status_for(exc.code), exc.code, exc.message) from None
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        raise ApiError(503, "busy", "the database is busy: wait a moment and try again") from None


def _size_only_out(stats: SizeOnlyStats | GroupStats) -> SizeOnlyStatsOut:
    return SizeOnlyStatsOut(
        n=stats.n,
        es_in=stats.es_in,
        es_moa=stats.es_moa,
        includes_estimated=stats.includes_estimated,
    )


def advice_out(view: service.AdviceView) -> AdviceOut:
    """Convert domain advice to `AdviceOut`.

    Correction kinds keep their mean POI (`GroupStatsOut` with direction text); the confirm
    judgments (ready, touch-up, confirm again) carry the overlay size only, because the overlay is
    centered by construction; `shoot_more` and `invalid` get no correction and size-only stats
    (the judged group's, or those of the domain stats with every POI field dropped).
    """
    advice = view.advice
    correction = (
        CorrectionOut(
            up_mil=advice.correction.up_mil,
            right_mil=advice.correction.right_mil,
            text=advice.correction.text,
        )
        if advice.correction is not None
        else None
    )
    stats: GroupStatsOut | SizeOnlyStatsOut | None = None
    if advice.kind in _NO_NUMBER_KINDS:
        correction = None
        size = view.group_size or advice.stats
        stats = _size_only_out(size) if size is not None else None
    elif advice.stats is not None and advice.kind in service.CORRECTION_KINDS:
        stats = GroupStatsOut(
            n=advice.stats.n,
            es_in=advice.stats.es_in,
            es_moa=advice.stats.es_moa,
            mean_dx_in=advice.stats.mean_dx_in,
            mean_dy_in=advice.stats.mean_dy_in,
            mean_dx_mil=advice.stats.mean_dx_mil,
            mean_dy_mil=advice.stats.mean_dy_mil,
            includes_estimated=advice.stats.includes_estimated,
            poi_text=view.poi_text,
        )
    elif advice.stats is not None:
        stats = _size_only_out(advice.stats)
    return AdviceOut(kind=advice.kind, message=advice.message, correction=correction, stats=stats)


def check_out(view: service.CheckView) -> ZeroCheckOut:
    """A stored zero check: the stored result decides the message, and the per-shot result is
    shown only when the recomputed check agrees with it (otherwise it is left empty).
    """
    check = view.check
    passed = check.result == "pass"
    inside = list(view.result.per_shot_inside) if view.result else []
    return ZeroCheckOut(
        id=check.id,
        zero_id=check.zero_id,
        group_id=check.group_id,
        result=check.result,
        message=service.CHECK_MESSAGES[check.result],
        per_shot_inside=inside if inside and all(inside) == passed else [],
        route=[] if passed else list(service.LOST_ZERO_ROUTE),
        created_at=check.created_at,
    )


def zero_out(view: service.ZeroView) -> ZeroOut:
    """The zero, its linked groups (no POI fields), checks and latest advice."""
    zero = view.zero
    return ZeroOut(
        id=zero.id,
        rifle_id=zero.rifle_id,
        load_id=zero.load_id,
        lot_id=zero.lot_id,
        mode=zero.mode,
        state=ZeroState(zero.state),
        dialed_up_mil=zero.dialed_up_mil,
        dialed_right_mil=zero.dialed_right_mil,
        zero_es_in=zero.zero_es_in,
        zero_shot_count=zero.zero_shot_count,
        dot_size_in=zero.dot_size_in,
        suggested_dot_in=view.suggested_dot_in,
        completed_at=zero.completed_at,
        created_at=zero.created_at,
        updated_at=zero.updated_at,
        groups=[
            ZeroGroupOut(
                group_id=g.group_id,
                purpose=GroupPurpose(g.purpose),
                status=GroupStatus(g.status),
                shots=g.shot_count,
            )
            for g in view.groups
        ],
        checks=[check_out(c) for c in view.checks],
        advice=advice_out(view.advice) if view.advice is not None else None,
    )


@router.post("/api/zeros", status_code=201)
def create_zero(body: ZeroCreate, conn: Db) -> ZeroOut:
    """Start a zero for a rifle and load (`new_setup` at Step 1, `existing` at Step 2)."""
    with _api_errors():
        return zero_out(
            service.create_zero(conn, body.rifle_id, body.load_id, body.mode, body.lot_code)
        )


@router.get("/api/zeros/{zero_id}")
def get_zero(zero_id: ZeroId, conn: Db) -> ZeroOut:
    """The zero with its stepper state, linked groups, checks and latest advice."""
    with _api_errors():
        return zero_out(service.get_zero(conn, zero_id))


@router.post("/api/zeros/{zero_id}/groups")
def link_group(zero_id: ZeroId, body: ZeroLinkGroup, conn: Db) -> ZeroOut:
    """Attach a group as the next group of the zero's current step."""
    with _api_errors():
        return zero_out(service.link_group(conn, zero_id, body.group_id))


@router.delete(
    "/api/zeros/{zero_id}/groups/{group_id}",
    response_model=ROUTE_SCHEMAS[("DELETE", "/api/zeros/{id}/groups/{gid}")].response,
)
def detach_group(zero_id: ZeroId, group_id: GroupId, conn: Db):
    """Detach an abandoned step group; it becomes a general group again."""
    with _api_errors():
        return zero_out(service.detach_group(conn, zero_id, group_id))


@router.post("/api/zeros/{zero_id}/dialed")
def record_dialed(zero_id: ZeroId, body: ZeroDialed, conn: Db) -> ZeroOut:
    """Record the correction the user dialed and move to the next step."""
    with _api_errors():
        return zero_out(service.record_dialed(conn, zero_id, body.up_mil, body.right_mil))


@router.post("/api/zeros/{zero_id}/dot")
def set_dot(zero_id: ZeroId, body: ZeroDot, conn: Db) -> ZeroOut:
    """Choose the dot and complete the zero (supersedes the previous zero)."""
    with _api_errors():
        return zero_out(service.set_dot(conn, zero_id, body.dot_size_in))


@router.post("/api/zeros/{zero_id}/checks", status_code=201)
def create_check(zero_id: ZeroId, body: ZeroCheckCreate, conn: Db) -> ZeroCheckOut:
    """Judge a 1-2 shot group against the dot of this complete zero."""
    with _api_errors():
        return check_out(service.run_zero_check(conn, zero_id, body.group_id))


@router.post("/api/zeros/{zero_id}/check-groups", status_code=201)
def create_check_group(zero_id: ZeroId, body: ZeroCheckGroupCreate, conn: Db) -> GroupOut:
    """Create a zero-check group already linked to this complete zero (masked from creation)."""
    with _api_errors():
        group = service.create_check_group(
            conn,
            zero_id,
            lot_code=body.lot_code,
            shots_expected=body.shots_expected,
            notes=body.notes,
            shot_date=body.shot_date,
            shot_time=body.shot_time,
            position=body.position,
            suppressor=body.suppressor,
            suppressor_id=body.suppressor_id,
            photo_id=body.photo_id,
        )
        return group_out(group_records.group_view(conn, group.id))


@router.get("/zeroing/{zero_id}", response_class=HTMLResponse)
def zeroing_page(zero_id: ZeroId, request: Request, conn: Db) -> HTMLResponse:
    """The zeroing stepper page; its script loads everything through `/api/zeros/{id}`."""
    with _api_errors():
        service.require_zero(conn, zero_id)
    return request.app.state.templates.TemplateResponse(
        request,
        "zeroing.html",
        {
            "active": "rifles",
            "zero_id": zero_id,
            "suppressors": group_records.list_suppressors(conn),
            "position_labels": group_records.POSITION_LABELS,
        },
    )
