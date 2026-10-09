"""The home address: the Groups tab is the app's starting page, so `/` sends the browser there."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    """Redirect to the Groups tab (303). Opening the app's address also drops this site's HTTP
    cache, so a browser that kept a page script from an older version fetches the current one
    (static files are served `no-cache` and photos `no-store`, so nothing else is lost)."""
    return RedirectResponse("/groups", status_code=303, headers={"Clear-Site-Data": '"cache"'})
