"""Load comparison: `GET /api/rifles/{id}/comparison` and the `/compare` page.

Comparisons are like with like (tier10 and tier30), built from finalized groups whose point of
impact is not masked; handlers call `rga.services.compare` and `rga.services.rifles` only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from rga.domain.types import Position
from rga.services import compare as service
from rga.services import rifles as rifle_service
from rga.web.app import ApiError
from rga.web.deps import get_db
from rga.web.schemas import (
    ROUTE_SCHEMAS,
    CompareAggregateOut,
    CompareGroupOut,
    ComparisonFiltersOut,
    ComparisonOut,
    ComparisonQuery,
    LoadComparisonOut,
    PairComparisonOut,
    PoiShiftOut,
    TierSummaryOut,
)

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_db)]
RifleId = Annotated[int, Path(ge=1, le=service.SQLITE_MAX_INTEGER)]
POSITION_LABELS = {
    Position.BENCH: "Bench",
    Position.PRONE_OFF_PACK: "Prone off pack",
    Position.KNEELING_SITTING: "Kneeling or sitting",
    Position.STANDING_FIELD: "Standing (field)",
}


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
    """Turn service and database errors into `ApiError`: 404, 422, 409, 503 `busy`; never a 500."""
    try:
        yield
    except (service.CompareError, service.RifleError) as exc:
        raise ApiError(_status_for(exc.code), exc.code, exc.message) from exc
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise ApiError(
                503,
                "busy",
                "The database is busy; wait a moment and try again.",
                headers={"Retry-After": "1"},
            ) from exc
        raise


def _group_out(info: service.CompareGroupInfo) -> CompareGroupOut:
    return CompareGroupOut(
        group_id=info.group_id,
        shot_date=info.shot_date,
        shot_time=info.shot_time,
        distance_yd=info.distance_yd,
        position=info.position,
        suppressor=info.suppressor,
        shots=info.shots,
        es_moa=info.es_moa,
        includes_estimated=info.includes_estimated,
        tier=info.tier,
    )


def _aggregate_out(info: service.CompareAggregateInfo) -> CompareAggregateOut:
    return CompareAggregateOut(**vars(info))


def _load_out(item: service.LoadComparison) -> LoadComparisonOut:
    summary = item.summary
    shift = item.poi_shift
    return LoadComparisonOut(
        load_id=item.load.id,
        label=item.load.label,
        is_zero_load=item.is_zero_load,
        tier10=TierSummaryOut(**vars(summary.tier10)),
        tier30=TierSummaryOut(**vars(summary.tier30)),
        status=summary.status,
        precision_verdict=summary.precision_verdict,
        poi_shift=None
        if shift is None
        else PoiShiftOut(right_mil=shift.right_mil, up_mil=shift.up_mil, text=shift.text),
        poi_shift_note=item.poi_shift_note,
        groups=[_group_out(g) for g in item.groups],
        aggregates=[_aggregate_out(a) for a in item.aggregates],
    )


@router.get(
    "/api/rifles/{rifle_id}/comparison",
    response_model=_response("GET", "/api/rifles/{id}/comparison"),
)
def get_comparison(rifle_id: RifleId, request: Request, conn: Conn) -> ComparisonOut:
    """Compare loads of a rifle by tier; `load_ids` is a comma list, `position` and `suppressor`
    filter the groups. Masked groups and groups under 10 shots never enter a comparison.
    """
    try:
        query = ComparisonQuery.model_validate(dict(request.query_params))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ApiError(422, "validation_error", problems) from None
    with _mapped_errors():
        result = service.compare(conn, rifle_id, query.load_ids, query.position, query.suppressor)
    return ComparisonOut(
        rifle_id=result.rifle.id,
        filters=ComparisonFiltersOut(position=result.position, suppressor=result.suppressor),
        loads=[_load_out(item) for item in result.loads],
        pairs=[
            PairComparisonOut(
                a_load_id=p.a_load_id, b_load_id=p.b_load_id, tier10=p.tier10, tier30=p.tier30
            )
            for p in result.pairs
        ],
        conditions_warning=result.conditions_warning,
        zero_to_hunting_load_advice=result.advice,
    )


@router.get("/compare", response_class=HTMLResponse)
def compare_page(
    request: Request, conn: Conn, rifle_id: Annotated[int | None, Query(ge=1)] = None
) -> HTMLResponse:
    """The comparison page: a rifle picker with no query string, else the load chooser."""
    context: dict = {
        "active": "compare",
        "rifle": None,
        "loads": [],
        "rifles": [],
        "positions": [(p.value, label) for p, label in POSITION_LABELS.items()],
    }
    with _mapped_errors():
        if rifle_id is None:
            context["rifles"] = rifle_service.list_rifles(conn)
        else:
            if rifle_id > service.SQLITE_MAX_INTEGER:
                raise service.RifleNotFound(rifle_id)
            context["rifle"], context["loads"] = rifle_service.rifle_with_loads(conn, rifle_id)
    return request.app.state.templates.TemplateResponse(request, "compare.html", context)
