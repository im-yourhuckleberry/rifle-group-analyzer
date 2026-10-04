"""Application factory: migrates the database, mounts static files and templates, installs
the security middleware and the `{detail, code}` error model, auto-discovers routers and
serves `/healthz`.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from rga.config import Settings, load_settings
from rga.web import routes
from rga.web.deps import current_schema_version, get_db, init_database
from rga.web.schemas import ErrorResponse
from rga.web.security import SECURITY_HEADERS, OriginGuard, SecurityHeaders, host_guard

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
TEMPLATES_DIR = WEB_DIR / "templates"
SQLITE_OVERFLOW_MESSAGE = "too large to convert to SQLite INTEGER"


class ApiError(Exception):
    """Error that route code raises to produce `{detail, code}` with `status_code` (400-599).

    `code` must be snake_case (`^[a-z][a-z0-9_]*$`); an invalid code or status raises
    `ValueError` at construction. Optional `headers` are sent with the response.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if not 400 <= status_code <= 599:
            raise ValueError(f"status_code must be 400-599, got {status_code}")
        ErrorResponse(detail=detail, code=code)
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.headers = dict(headers) if headers else None


def _error_response(
    status_code: int, code: str, detail: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    body = ErrorResponse(detail=detail, code=code).model_dump()
    return JSONResponse(body, status_code=status_code, headers=dict(headers) if headers else None)


def _detail_text(detail: object) -> str:
    return detail if isinstance(detail, str) else json.dumps(detail, default=str)


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.detail, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error_response(
            exc.status_code, f"http_{exc.status_code}", _detail_text(exc.detail), exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        return _error_response(422, "validation_error", problems or "Invalid request")

    @app.exception_handler(OverflowError)
    async def integer_overflow(request: Request, exc: OverflowError) -> JSONResponse:
        if SQLITE_OVERFLOW_MESSAGE in str(exc):
            return _error_response(404, "not_found", "The requested record does not exist")
        return _error_response(
            500, "internal_error", "Internal server error", headers=SECURITY_HEADERS
        )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        return _error_response(
            500, "internal_error", "Internal server error", headers=SECURITY_HEADERS
        )


def discover_routers(package: ModuleType = routes) -> list[APIRouter]:
    """Import every public module of `package` and return the `router` each one exports.

    Modules without a `router` attribute and names starting with '_' are skipped; import
    errors propagate. Order is alphabetical by module name.
    """
    found: list[APIRouter] = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            found.append(router)
    return found


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app for `settings` (default: `load_settings()`).

    Creates the data directories and migrates the database before returning. Middleware,
    outermost first: `SecurityHeaders`, the Host allowlist, `OriginGuard`. Every error
    response from the app (HTTP, validation, `ApiError`, unhandled) is `{detail, code}`;
    the Host guard's 400 stays plain text. API docs pages are disabled (they load scripts
    from a CDN).
    """
    settings = settings or load_settings()
    init_database(settings)

    app = FastAPI(title="Rifle Group Analyzer", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.templates = Jinja2Templates(directory=TEMPLATES_DIR)

    _install_error_handlers(app)
    app.add_middleware(OriginGuard)
    app.add_middleware(host_guard)
    app.add_middleware(SecurityHeaders)

    @app.get("/healthz")
    def healthz(conn: Annotated[sqlite3.Connection, Depends(get_db)]) -> dict[str, object]:
        return {"status": "ok", "schema_version": current_schema_version(conn)}

    for router in discover_routers():
        app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")
    return app
