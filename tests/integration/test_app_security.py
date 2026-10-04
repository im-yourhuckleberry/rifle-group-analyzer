"""T2: app factory, Host/Origin/content-type guards, DB dependency and router discovery."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from rga.config import Settings
from rga.db.connection import latest_version
from rga.web import app as app_module
from rga.web.app import create_app, discover_routers
from rga.web.deps import get_db
from rga.web.security import SECURITY_HEADERS, safe_return_to

ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    application = create_app(settings)
    seen: list[sqlite3.Connection] = []
    application.state.seen_connections = seen

    async def echo(request: Request) -> dict[str, bool]:
        return {"ok": True}

    def db_probe(conn: Annotated[sqlite3.Connection, Depends(get_db)]) -> dict[str, int]:
        seen.append(conn)
        return {"foreign_keys": conn.execute("PRAGMA foreign_keys").fetchone()[0]}

    for method in ("POST", "PUT", "PATCH", "DELETE", "GET"):
        application.add_api_route("/api/_echo", echo, methods=[method])
    application.add_api_route("/api/_db", db_probe, methods=["GET"])

    @application.get("/_page", response_class=HTMLResponse)
    def page() -> str:
        return "<!doctype html><title>t</title>"

    @application.get("/api/_typed")
    def typed(n: int) -> dict[str, int]:
        return {"n": n}

    @application.get("/api/_boom")
    def boom() -> None:
        raise RuntimeError("boom")

    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=ORIGIN) as test_client:
        yield test_client


def test_healthz_reports_schema_version(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "schema_version": latest_version()}


def test_create_app_creates_data_dirs_and_migrated_database(settings: Settings) -> None:
    create_app(settings)
    assert settings.db_path.is_file()
    assert settings.photos_dir.is_dir() and settings.working_dir.is_dir()
    conn = sqlite3.connect(settings.db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"rifles", "groups", "shots"} <= tables


def test_create_app_twice_on_the_same_data_dir_is_idempotent(settings: Settings) -> None:
    create_app(settings)
    create_app(settings)


def test_api_docs_pages_that_would_need_a_cdn_are_not_served(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:8765",
        "127.0.0.1.evil.example",
        "localhost.evil.example",
        "127.0.0.1@evil.example",
        "0.0.0.0:8765",
        "",
    ],
)
def test_foreign_host_header_is_refused_with_400(client: TestClient, host: str) -> None:
    response = client.get("/healthz", headers={"Host": host})
    assert response.status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765", "localhost"])
def test_loopback_host_headers_are_served(client: TestClient, host: str) -> None:
    assert client.get("/healthz", headers={"Host": host}).status_code == 200


def test_host_check_applies_to_writes_and_static_files_too(client: TestClient) -> None:
    evil = {"Host": "evil.example", "Content-Type": "application/json"}
    assert client.post("/api/_echo", headers=evil, content=b"{}").status_code == 400
    assert client.get("/static/css/app.css", headers={"Host": "evil.example"}).status_code == 400


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_cross_origin_write_is_refused_with_403(client: TestClient, method: str) -> None:
    response = client.request(
        method,
        "/api/_echo",
        headers={"Origin": "http://evil.example", "Content-Type": "application/json"},
        content=b"{}",
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Cross-origin request refused", "code": "bad_origin"}


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "http://127.0.0.1:9999",
        "http://127.0.0.1",
        "https://127.0.0.1:8765",
        "http://localhost:8765",
        "http://127.0.0.1:8765.evil.example",
    ],
)
def test_origin_must_equal_the_server_origin_exactly(client: TestClient, origin: str) -> None:
    response = client.post(
        "/api/_echo", headers={"Origin": origin, "Content-Type": "application/json"}, content=b"{}"
    )
    assert response.status_code == 403


def test_same_origin_json_write_passes(client: TestClient) -> None:
    response = client.post("/api/_echo", headers={"Origin": ORIGIN}, json={"a": 1})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_same_origin_check_follows_the_host_header(client: TestClient) -> None:
    headers = {"Host": "localhost:8765", "Origin": "http://localhost:8765"}
    assert client.post("/api/_echo", headers=headers, json={}).status_code == 200


def test_write_without_origin_passes_for_non_browser_clients(client: TestClient) -> None:
    assert client.post("/api/_echo", json={}).status_code == 200


def test_cross_origin_reads_are_not_blocked_by_the_origin_guard(client: TestClient) -> None:
    assert client.get("/healthz", headers={"Origin": "http://evil.example"}).status_code == 200


@pytest.mark.parametrize(
    "content_type",
    [
        "text/plain",
        "application/x-www-form-urlencoded",
        "text/plain;charset=UTF-8",
        "application/xml",
    ],
)
def test_non_json_non_multipart_write_is_refused_with_415(
    client: TestClient, content_type: str
) -> None:
    response = client.post(
        "/api/_echo", headers={"Origin": ORIGIN, "Content-Type": content_type}, content=b"a=b"
    )
    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"


def test_write_with_a_body_and_no_content_type_is_refused_with_415(client: TestClient) -> None:
    assert client.post("/api/_echo", content=b"payload").status_code == 415


def test_bodyless_write_needs_no_content_type(client: TestClient) -> None:
    assert client.post("/api/_echo").status_code == 200
    assert client.delete("/api/_echo").status_code == 200


def test_json_with_charset_and_multipart_are_accepted(client: TestClient) -> None:
    ok_json = client.post(
        "/api/_echo",
        headers={"Content-Type": "application/json; charset=utf-8"},
        content=b"{}",
    )
    ok_multipart = client.post("/api/_echo", files={"file": ("x.jpg", b"abc", "image/jpeg")})
    assert (ok_json.status_code, ok_multipart.status_code) == (200, 200)


def test_origin_refusal_takes_precedence_over_media_type_refusal(client: TestClient) -> None:
    response = client.post(
        "/api/_echo",
        headers={"Origin": "http://evil.example", "Content-Type": "text/plain"},
        content=b"x",
    )
    assert response.status_code == 403


def test_get_db_yields_a_foreign_keys_connection_per_request_and_closes_it(
    client: TestClient, app: FastAPI
) -> None:
    assert client.get("/api/_db").json() == {"foreign_keys": 1}
    assert client.get("/api/_db").json() == {"foreign_keys": 1}
    first, second = app.state.seen_connections
    assert first is not second
    for conn in (first, second):
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_static_files_and_templates_are_wired(client: TestClient, app: FastAPI) -> None:
    css = client.get("/static/css/app.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    rendered = app.state.templates.env.from_string("{{ note }}").render(note="<script>x</script>")
    assert rendered == "&lt;script&gt;x&lt;/script&gt;"
    assert "base.html" in app.state.templates.env.list_templates()


@pytest.mark.parametrize("path", ["/", "/groups/3", "/groups/3/review?x=1&y=2", "/a#frag"])
def test_safe_return_to_accepts_same_site_paths(path: str) -> None:
    assert safe_return_to(path) == path


@pytest.mark.parametrize(
    "path",
    [
        None,
        "",
        "//evil.example",
        "///evil.example",
        "https://x",
        "http://x/y",
        "javascript:alert(1)",
        "evil.example/path",
        "/\\evil.example",
        "/\t/evil.example",
        "/\n/evil.example",
        "/ok\r\nSet-Cookie: a=b",
        " /leading-space",
    ],
)
def test_safe_return_to_rejects_everything_else(path: str | None) -> None:
    assert safe_return_to(path) is None


def _write_routes_package(root: Path, name: str, modules: dict[str, str]) -> None:
    package = root / name
    package.mkdir()
    (package / "__init__.py").write_text("")
    for module, source in modules.items():
        (package / f"{module}.py").write_text(textwrap.dedent(source))


def test_discover_routers_collects_exported_routers_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    router_source = """
        from fastapi import APIRouter
        router = APIRouter()
        @router.get("/api/{name}")
        def ping():
            return {}
    """
    _write_routes_package(
        tmp_path,
        "fake_routes_ok",
        {
            "b_second": router_source.replace("{name}", "second"),
            "a_first": router_source.replace("{name}", "first"),
            "helper": "VALUE = 1\n",
            "_private": router_source.replace("{name}", "private"),
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    import fake_routes_ok  # type: ignore[import-not-found]

    found = discover_routers(fake_routes_ok)
    assert [r.routes[0].path for r in found] == ["/api/first", "/api/second"]  # type: ignore[attr-defined]
    monkeypatch.delitem(sys.modules, "fake_routes_ok")


def test_discover_routers_lets_import_errors_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_routes_package(tmp_path, "fake_routes_bad", {"broken": "raise RuntimeError('boom')\n"})
    monkeypatch.syspath_prepend(str(tmp_path))
    import fake_routes_bad  # type: ignore[import-not-found]

    with pytest.raises(RuntimeError, match="boom"):
        discover_routers(fake_routes_bad)
    monkeypatch.delitem(sys.modules, "fake_routes_bad")


def test_create_app_mounts_routers_discovered_in_the_routes_package(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/api/discovered")
    def discovered() -> dict[str, bool]:
        return {"found": True}

    monkeypatch.setattr(app_module, "discover_routers", lambda: [router])
    with TestClient(create_app(settings), base_url=ORIGIN) as test_client:
        assert test_client.get("/api/discovered").json() == {"found": True}


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _data_dirs(settings: Settings) -> list[Path]:
    return [settings.data_dir, settings.photos_dir, settings.working_dir]


def test_new_data_directories_are_private(settings: Settings) -> None:
    create_app(settings)
    assert [_mode(d) for d in _data_dirs(settings)] == [0o700, 0o700, 0o700]


def test_new_data_directories_are_private_even_with_a_permissive_umask(
    settings: Settings,
) -> None:
    previous = os.umask(0)
    try:
        create_app(settings)
    finally:
        os.umask(previous)
    assert [_mode(d) for d in _data_dirs(settings)] == [0o700, 0o700, 0o700]


def test_existing_wider_data_directories_are_tightened_to_0700(settings: Settings) -> None:
    for directory in _data_dirs(settings):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)
    create_app(settings)
    assert [_mode(d) for d in _data_dirs(settings)] == [0o700, 0o700, 0o700]


EXPECTED_SECURITY_HEADERS = {
    "x-frame-options": "DENY",
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}


def test_security_header_constants_match_design_section_11() -> None:
    assert {k.lower(): v for k, v in SECURITY_HEADERS.items()} == EXPECTED_SECURITY_HEADERS


def _assert_security_headers(response) -> None:
    for name, value in EXPECTED_SECURITY_HEADERS.items():
        assert response.headers.get_list(name) == [value], name


def test_headers_are_on_pages_api_static_and_not_found_responses(client: TestClient) -> None:
    for path in ("/_page", "/healthz", "/static/css/app.css", "/no/such/page"):
        _assert_security_headers(client.get(path))


def test_headers_are_on_guard_refusals_and_405(client: TestClient) -> None:
    cross_origin = client.post("/api/_echo", headers={"Origin": "http://evil.example"}, json={})
    assert cross_origin.status_code == 403
    _assert_security_headers(cross_origin)
    wrong_type = client.post("/api/_echo", headers={"Content-Type": "text/plain"}, content=b"x")
    assert wrong_type.status_code == 415
    _assert_security_headers(wrong_type)
    bad_host = client.get("/healthz", headers={"Host": "evil.example"})
    assert bad_host.status_code == 400
    _assert_security_headers(bad_host)
    method_not_allowed = client.put("/healthz")
    assert method_not_allowed.status_code == 405
    _assert_security_headers(method_not_allowed)


def test_headers_are_on_the_422_and_500_error_model_responses(app: FastAPI) -> None:
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as lenient:
        unhandled = lenient.get("/api/_boom")
        assert unhandled.status_code == 500
        _assert_security_headers(unhandled)
        invalid = lenient.get("/api/_typed", params={"n": "abc"})
        assert invalid.status_code == 422
        _assert_security_headers(invalid)


def test_a_response_that_already_sets_a_header_keeps_one_value(app: FastAPI) -> None:
    @app.get("/api/_custom_frame")
    def custom() -> HTMLResponse:
        return HTMLResponse("x", headers={"X-Frame-Options": "SAMEORIGIN"})

    with TestClient(app, base_url=ORIGIN) as c:
        assert c.get("/api/_custom_frame").headers.get_list("x-frame-options") == ["SAMEORIGIN"]


WEB_ROOT = Path(__file__).resolve().parents[2] / "src" / "rga" / "web"
INLINE_PATTERNS = {
    "inline script": re.compile(r"<script(?![^>]*\bsrc\s*=)[^>]*>", re.IGNORECASE),
    "style element": re.compile(r"<style\b", re.IGNORECASE),
    "style attribute": re.compile(r"<[a-z][^>]*\sstyle\s*=", re.IGNORECASE),
    "event handler attribute": re.compile(r"<[a-z][^>]*\son[a-z]+\s*=", re.IGNORECASE),
    "javascript: url": re.compile(r"javascript:", re.IGNORECASE),
    "setAttribute style": re.compile(r"setAttribute\(\s*[\"']style[\"']"),
    "eval": re.compile(r"\beval\s*\(|new\s+Function\s*\("),
}


def _web_assets() -> list[Path]:
    files = [*(WEB_ROOT / "templates").rglob("*.html"), *(WEB_ROOT / "static").rglob("*.js")]
    assert files, "no templates or scripts found"
    return sorted(files)


@pytest.mark.parametrize("asset", _web_assets(), ids=lambda p: str(p.relative_to(WEB_ROOT)))
def test_templates_and_scripts_have_no_inline_code_the_csp_would_block(asset: Path) -> None:
    text = asset.read_text(encoding="utf-8")
    violations = [name for name, pattern in INLINE_PATTERNS.items() if pattern.search(text)]
    assert violations == [], f"{asset.name}: {violations}"


def test_inline_code_patterns_do_detect_violations() -> None:
    samples = {
        "inline script": "<script>alert(1)</script>",
        "style element": "<style>a{}</style>",
        "style attribute": '<div style="color:red">',
        "event handler attribute": '<button onclick="go()">',
        "javascript: url": '<a href="javascript:void(0)">',
        "setAttribute style": "el.setAttribute('style', 'x')",
        "eval": "eval('1')",
    }
    assert set(samples) == set(INLINE_PATTERNS)
    for name, sample in samples.items():
        assert INLINE_PATTERNS[name].search(sample), name
    assert not INLINE_PATTERNS["inline script"].search(
        '<script type="module" src="/static/js/a.js">'
    )
