"""The Groups tab: `GET /groups` lists every measured group of every rifle, newest first.

Filters are query-string parameters of a GET form, so a filtered view can be bookmarked. A blank
value is ignored and a malformed one is a 422 `validation_error`; nothing here is a 500. Rows carry
size only, never a point of impact, offset or correction. Handlers call `rga.services` only.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, StringConstraints, ValidationError, field_validator

from rga.domain.types import GroupPurpose, GroupStatus, Position
from rga.services import analysis as service
from rga.services import group_records as context_service
from rga.services import rifles as rifles_service
from rga.web.app import ApiError
from rga.web.deps import get_db
from rga.web.schemas import PositiveId

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_db)]
MAX_PAGE = 1_000_000
DATE_TEXT = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
FILTER_NAMES = (
    "rifle_id",
    "load_id",
    "distance_yd",
    "position",
    "suppressor",
    "suppressor_id",
    "status",
    "purpose",
    "date_from",
    "date_to",
)
STATUS_LABELS = {GroupStatus.DRAFT.value: "Draft", GroupStatus.FINAL.value: "Final"}


class GroupsQuery(BaseModel):
    """The groups page's query string; unknown parameters are ignored."""

    rifle_id: PositiveId | None = None
    load_id: PositiveId | None = None
    distance_yd: int | None = None
    position: Position | None = None
    suppressor: Literal["none", "yes"] | None = None
    suppressor_id: PositiveId | None = None
    status: GroupStatus | None = None
    purpose: GroupPurpose | None = None
    date_from: DATE_TEXT | None = None
    date_to: DATE_TEXT | None = None
    page: int = 1

    @field_validator("distance_yd")
    @classmethod
    def _distance(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 100_000:
            raise ValueError("distance_yd must be between 1 and 100000")
        return value

    @field_validator("page")
    @classmethod
    def _page(cls, value: int) -> int:
        if not 1 <= value <= MAX_PAGE:
            raise ValueError(f"page must be between 1 and {MAX_PAGE}")
        return value

    @field_validator("date_from", "date_to")
    @classmethod
    def _real_date(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                dt.date.fromisoformat(value)
            except ValueError:
                raise ValueError("must be a real date, YYYY-MM-DD") from None
        return value

    def filters(self) -> service.GroupListFilters:
        """The validated filters as the service takes them."""
        return service.GroupListFilters(
            rifle_id=self.rifle_id,
            load_id=self.load_id,
            distance_yd=self.distance_yd,
            position=None if self.position is None else self.position.value,
            suppressor=self.suppressor,
            suppressor_id=self.suppressor_id,
            status=None if self.status is None else self.status.value,
            purpose=None if self.purpose is None else self.purpose.value,
            date_from=self.date_from,
            date_to=self.date_to,
        )

    def chosen(self) -> dict[str, str]:
        """The filters in effect as the strings the form and the page links carry."""
        found = {}
        for name in FILTER_NAMES:
            value = getattr(self, name)
            if value is not None:
                found[name] = getattr(value, "value", value)
        return {name: str(value) for name, value in found.items()}


def _parse(request: Request) -> GroupsQuery:
    given = {k: v for k, v in request.query_params.items() if v.strip() != ""}
    try:
        return GroupsQuery.model_validate(given)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ApiError(422, "validation_error", problems) from None


def _page_link(chosen: dict[str, str], page: int) -> str:
    params = dict(chosen)
    if page > 1:
        params["page"] = str(page)
    return "/groups?" + urlencode(params) if params else "/groups"


@router.get("/groups", response_class=HTMLResponse)
def groups_page(request: Request, conn: Conn) -> HTMLResponse:
    """Every group, newest first, with quick size details and filters; 100 rows per page."""
    query = _parse(request)
    try:
        listing = service.group_list(conn, query.filters(), query.page)
        options = service.group_filter_options(conn)
        new_group_rifles = rifles_service.rifle_summaries(conn)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise ApiError(
                503,
                "busy",
                "The database is busy; wait a moment and try again.",
                headers={"Retry-After": "1"},
            ) from exc
        raise
    chosen = query.chosen()
    context = {
        "active": "groups",
        "listing": listing,
        "new_group_rifles": new_group_rifles,
        "options": options,
        "chosen": chosen,
        "filtered": bool(chosen),
        "active_count": len(chosen),
        "position_labels": context_service.POSITION_LABELS,
        "status_labels": STATUS_LABELS,
        "purpose_labels": service.PURPOSE_LABELS,
        "previous_link": _page_link(chosen, listing.page - 1) if listing.page > 1 else None,
        "next_link": _page_link(chosen, listing.page + 1) if listing.page < listing.pages else None,
        "first_row": (listing.page - 1) * listing.page_size + 1 if listing.rows else 0,
    }
    return request.app.state.templates.TemplateResponse(request, "groups.html", context)
