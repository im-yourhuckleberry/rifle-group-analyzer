"""T2: every error the app produces has the `{detail, code}` body that api.js and ErrorResponse expect."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from rga.config import Settings
from rga.web.app import ApiError, create_app
from rga.web.schemas import ErrorResponse

ORIGIN = "http://127.0.0.1:8765"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Payload(BaseModel):
    n: int = Field(gt=0)
    label: str


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    application = create_app(Settings(data_dir=tmp_path / "data"))

    @application.get("/api/_http404")
    def http404() -> None:
        raise HTTPException(404, "Rifle 7 not found")

    @application.get("/api/_http_dict")
    def http_dict() -> None:
        raise HTTPException(400, {"field": "x"})

    @application.get("/api/_http_headers")
    def http_headers() -> None:
        raise HTTPException(429, "slow down", headers={"Retry-After": "5"})

    @application.get("/api/_api_error")
    def api_error() -> None:
        raise ApiError(409, "duplicate_lot", "Lot LOT1 already exists", headers={"X-Hint": "1"})

    @application.post("/api/_validated")
    def validated(payload: Payload) -> dict[str, int]:
        return {"n": payload.n}

    @application.get("/api/_query")
    def query(limit: int) -> dict[str, int]:
        return {"limit": limit}

    @application.get("/api/_boom")
    def boom() -> None:
        raise RuntimeError("secret-internal-detail /Users/someone/rga.sqlite3")

    @application.post("/api/_post_only")
    def post_only() -> dict[str, bool]:
        return {"ok": True}

    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as test_client:
        yield test_client


def assert_error_model(response, status: int, code: str) -> dict[str, str]:
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) == {"detail", "code"}
    assert isinstance(body["detail"], str) and body["detail"]
    ErrorResponse.model_validate(body)
    assert body["code"] == code
    return body


def test_http_exception_uses_http_status_code_and_keeps_detail(client: TestClient) -> None:
    body = assert_error_model(client.get("/api/_http404"), 404, "http_404")
    assert body["detail"] == "Rifle 7 not found"


def test_http_exception_with_non_string_detail_is_serialized_to_text(client: TestClient) -> None:
    body = assert_error_model(client.get("/api/_http_dict"), 400, "http_400")
    assert body["detail"] == '{"field": "x"}'


def test_http_exception_headers_are_preserved(client: TestClient) -> None:
    response = client.get("/api/_http_headers")
    assert_error_model(response, 429, "http_429")
    assert response.headers["retry-after"] == "5"


def test_api_error_carries_its_custom_code_status_and_headers(client: TestClient) -> None:
    response = client.get("/api/_api_error")
    body = assert_error_model(response, 409, "duplicate_lot")
    assert body["detail"] == "Lot LOT1 already exists"
    assert response.headers["x-hint"] == "1"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((200, "ok_code", "d"), "status_code"),
        ((400, "Bad-Code", "d"), "code"),
        ((400, "", "d"), "code"),
    ],
)
def test_api_error_rejects_invalid_status_or_code_at_construction(
    args: tuple[int, str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ApiError(*args)


def test_unknown_route_and_wrong_method_use_the_model(client: TestClient) -> None:
    assert_error_model(client.get("/api/_nope"), 404, "http_404")
    response = client.get("/api/_post_only")
    assert_error_model(response, 405, "http_405")
    assert "POST" in response.headers["allow"]


def test_missing_static_file_uses_the_model(client: TestClient) -> None:
    assert_error_model(client.get("/static/css/does-not-exist.css"), 404, "http_404")


def test_body_validation_error_is_422_validation_error_naming_the_field(client: TestClient) -> None:
    body = assert_error_model(
        client.post("/api/_validated", json={"n": 0, "label": "x"}), 422, "validation_error"
    )
    assert body["detail"].startswith("body.n: ")


def test_missing_and_malformed_bodies_are_validation_errors(client: TestClient) -> None:
    missing = assert_error_model(
        client.post("/api/_validated", json={"n": 1}), 422, "validation_error"
    )
    assert "body.label" in missing["detail"]
    malformed = client.post(
        "/api/_validated", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert_error_model(malformed, 422, "validation_error")


def test_query_validation_error_names_the_parameter(client: TestClient) -> None:
    body = assert_error_model(client.get("/api/_query?limit=abc"), 422, "validation_error")
    assert "query.limit" in body["detail"]


def test_validation_detail_does_not_echo_submitted_values(client: TestClient) -> None:
    response = client.post("/api/_validated", json={"n": "SECRET-VALUE", "label": "x"})
    assert response.status_code == 422
    assert "SECRET-VALUE" not in response.text


def test_unhandled_exception_is_a_generic_500_without_leaks(client: TestClient) -> None:
    response = client.get("/api/_boom")
    body = assert_error_model(response, 500, "internal_error")
    assert body == {"detail": "Internal server error", "code": "internal_error"}
    for leaked in ("secret-internal-detail", "Traceback", "RuntimeError", "/Users/"):
        assert leaked not in response.text


def test_middleware_refusals_are_unchanged(client: TestClient) -> None:
    bad_host = client.get("/healthz", headers={"Host": "evil.example"})
    assert bad_host.status_code == 400
    assert bad_host.headers["content-type"].startswith("text/plain")
    cross = client.post("/api/_post_only", headers={"Origin": "http://evil.example"}, json={})
    assert_error_model(cross, 403, "bad_origin")
    text = client.post("/api/_post_only", headers={"Content-Type": "text/plain"}, content=b"x")
    assert_error_model(text, 415, "unsupported_media_type")


def test_api_js_reads_the_detail_and_code_fields_this_model_emits() -> None:
    source = (PROJECT_ROOT / "src/rga/web/static/js/api.js").read_text(encoding="utf-8")
    assert "body.detail" in source and "body.code" in source
