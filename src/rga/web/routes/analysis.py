"""Group analysis: group list and detail, statistics, aggregate overlay, notes search and pages.

Short zeroing groups (`masks_poi`) get size-only statistics and no per-shot offsets everywhere;
the aggregate refuses them. `PATCH /api/groups/{id}` (notes) lives in `routes/groups.py`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse

from rga.config import Settings
from rga.domain.types import GroupStats
from rga.services import analysis as service
from rga.services import group_records as context_service
from rga.services import settings as settings_service
from rga.services.analysis import GroupError
from rga.web.app import ApiError
from rga.web.deps import get_db, get_settings
from rga.web.routes.groups import group_out
from rga.web.routes.review import detail_out
from rga.web.schemas import (
    ES_LABEL,
    ROUTE_SCHEMAS,
    AggregateListQuery,
    AggregateOut,
    AggregateRequest,
    AggregateShotOut,
    GroupDetailOut,
    GroupListQuery,
    GroupOut,
    GroupStatsOut,
    SaveAggregateRequest,
    SavedAggregateOut,
    SearchHitOut,
    SearchQuery,
    SizeOnlyStatsOut,
)

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
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
    """Turn service and database errors into `ApiError`: 404, 422, 409 and 503 `busy`; never a 500."""
    try:
        yield
    except GroupError as exc:
        raise ApiError(_status_for(exc.code), exc.code, exc.message) from exc
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise ApiError(
                503,
                "busy",
                "The database is busy; wait a moment and try again.",
                headers={"Retry-After": BUSY_RETRY_SECONDS},
            ) from exc
        raise


def stats_out(stats: GroupStats | service.SizeOnlyStats) -> GroupStatsOut | SizeOnlyStatsOut:
    """Schema body for statistics: full with direction words, or size only for a masked group."""
    if isinstance(stats, GroupStats):
        return GroupStatsOut(
            n=stats.n,
            es_in=stats.es_in,
            es_moa=stats.es_moa,
            mean_dx_in=stats.mean_dx_in,
            mean_dy_in=stats.mean_dy_in,
            mean_dx_mil=stats.mean_dx_mil,
            mean_dy_mil=stats.mean_dy_mil,
            includes_estimated=stats.includes_estimated,
            poi_text=service.direction_text(stats.mean_dx_mil, stats.mean_dy_mil),
        )
    return SizeOnlyStatsOut(
        n=stats.n,
        es_in=stats.es_in,
        es_moa=stats.es_moa,
        includes_estimated=stats.includes_estimated,
    )


@router.get("/api/groups", response_model=_response("GET", "/api/groups"))
def get_groups(query: Annotated[GroupListQuery, Query()], conn: Conn) -> list[GroupOut]:
    """Groups filtered by `load_id`, `rifle_id` and `status` (all optional)."""
    with _mapped_errors():
        views = service.list_groups(
            conn,
            load_id=query.load_id,
            rifle_id=query.rifle_id,
            status=None if query.status is None else query.status.value,
        )
    return [group_out(view) for view in views]


@router.get("/api/groups/{group_id}", response_model=_response("GET", "/api/groups/{id}"))
def get_group(group_id: int, conn: Conn) -> GroupDetailOut:
    """One group with shots, rejections and geometry.

    A masked group (`masks_poi`, flagged `masked`) has no per-shot offsets and `aim` null;
    positions stay for drawing.
    """
    with _mapped_errors():
        view = service.group_detail(conn, group_id)
    detail = detail_out(view)
    return detail.model_copy(update={"aim": None}) if detail.masked else detail


@router.get(
    "/api/groups/{group_id}/stats", response_model=_response("GET", "/api/groups/{id}/stats")
)
def get_group_stats(group_id: int, conn: Conn) -> GroupStatsOut | SizeOnlyStatsOut:
    """Group statistics; size only when masked; 409 `group_has_no_shots` for an empty group."""
    with _mapped_errors():
        return stats_out(service.group_stats(conn, group_id))


def _aggregate_fields(result: service.AggregateResult) -> dict:
    stats = result.stats
    return {
        "group_count": result.group_count,
        "n": stats.n,
        "es_in": stats.es_in,
        "es_moa": stats.es_moa,
        "center_dx_in": stats.center_dx_in,
        "center_dy_in": stats.center_dy_in,
        "center_dx_mil": stats.center_dx_mil,
        "center_dy_mil": stats.center_dy_mil,
        "includes_estimated": stats.includes_estimated,
        "mixed_loads": stats.mixed_loads,
        "warnings": result.warnings,
        "shots": [
            AggregateShotOut(group_id=s.group_id, dx_in=s.dx_in, dy_in=s.dy_in, origin=s.origin)
            for s in result.shots
        ],
    }


def saved_out(saved: service.SavedAggregate) -> SavedAggregateOut:
    """Schema body for a saved aggregate: its live statistics plus identity and validity."""
    row = saved.row
    return SavedAggregateOut(
        **_aggregate_fields(saved.result),
        id=row.id,
        public_id=row.public_id,
        name=row.name,
        rifle_id=row.rifle_id,
        load_id=row.load_id,
        distance_yd=row.distance_yd,
        group_ids=saved.member_ids,
        created_at=row.created_at,
        valid=saved.valid,
        invalid_reason=saved.invalid_reason,
        tier=saved.tier,
    )


@router.post("/api/aggregate", response_model=_response("POST", "/api/aggregate"))
def post_aggregate(body: AggregateRequest, conn: Conn) -> AggregateOut:
    """Overlay finalized groups of one rifle and distance; masked groups are a 409."""
    with _mapped_errors():
        result = service.aggregate(conn, body.group_ids)
    return AggregateOut(**_aggregate_fields(result))


@router.post(
    "/api/aggregates", status_code=201, response_model=_response("POST", "/api/aggregates")
)
def post_saved_aggregate(body: SaveAggregateRequest, conn: Conn) -> SavedAggregateOut:
    """Save a named combination (at least two groups, one load, rifle and distance)."""
    with _mapped_errors():
        return saved_out(service.save_aggregate(conn, body.name, body.group_ids))


@router.get("/api/aggregates", response_model=_response("GET", "/api/aggregates"))
def get_saved_aggregates(
    query: Annotated[AggregateListQuery, Query()], conn: Conn
) -> list[SavedAggregateOut]:
    """Saved aggregates with live statistics, optionally only those of one load."""
    with _mapped_errors():
        return [saved_out(saved) for saved in service.list_saved(conn, query.load_id)]


@router.delete("/api/aggregates/{aggregate_id}", status_code=204)
def delete_saved_aggregate(aggregate_id: int, conn: Conn) -> Response:
    """Delete a saved aggregate; its groups are untouched."""
    with _mapped_errors():
        service.delete_saved(conn, aggregate_id)
    return Response(status_code=204)


@router.get("/api/search", response_model=_response("GET", "/api/search"))
def get_search(query: Annotated[SearchQuery, Query()], conn: Conn) -> list[SearchHitOut]:
    """Group notes (including notes carried over from older records) containing `q`
    (case-insensitive; `%` and `_` are literal)."""
    with _mapped_errors():
        hits = service.search_notes(conn, query.q)
    return [SearchHitOut(kind=kind, id=row_id, snippet=snippet) for kind, row_id, snippet in hits]


MASKED_SHARE_LAYERS = frozenset({"thing_hitter", "poi", "correction"})


def _share_defaults_for(conn: sqlite3.Connection, masked: bool) -> dict[str, bool]:
    """The saved share defaults; a masked group is given only the layers it may show."""
    defaults = settings_service.get_share_defaults(conn)
    if not masked:
        return defaults
    return {layer: on for layer, on in defaults.items() if layer not in MASKED_SHARE_LAYERS}


@router.get("/groups/{group_id}", response_class=HTMLResponse)
def group_page(group_id: int, request: Request, conn: Conn, settings: AppSettings) -> HTMLResponse:
    """The group page: plot, statistics with the ES label, notes and links."""
    with _mapped_errors():
        page = service.group_page(conn, group_id, settings.working_dir)
    context = {
        "active": "analysis",
        "page": page,
        "es_label": ES_LABEL,
        "suppressors": context_service.list_suppressors(conn),
        "position_labels": context_service.POSITION_LABELS,
        "share_defaults_json": json.dumps(_share_defaults_for(conn, page.masked)),
    }
    return request.app.state.templates.TemplateResponse(request, "group.html", context)


@router.get("/analysis", response_class=HTMLResponse)
def analysis_page(request: Request) -> HTMLResponse:
    """The group picker with the aggregate overlay, plus notes search."""
    return request.app.state.templates.TemplateResponse(
        request, "analysis.html", {"active": "analysis"}
    )
