"""The home page: rifles at a glance and the most recent groups."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from rga.services import rifles as service
from rga.services import zeroing as zeroing_service
from rga.web.deps import get_db

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(request: Request, conn: Annotated[sqlite3.Connection, Depends(get_db)]) -> HTMLResponse:
    """Render the dashboard: every rifle with its zero load and active zeros, and the latest groups.

    An active zero is a complete zero that has not been superseded.
    """
    dashboard = service.dashboard(conn)
    active_zeros = {
        summary.rifle.id: [
            listing
            for listing in zeroing_service.list_zeros_for_rifle(conn, summary.rifle.id)
            if listing.zero.state == "complete"
        ]
        for summary in dashboard.rifles
    }
    context = {"active": "home", "dashboard": dashboard, "active_zeros": active_zeros}
    return request.app.state.templates.TemplateResponse(request, "index.html", context)
