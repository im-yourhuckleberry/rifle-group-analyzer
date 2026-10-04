"""Ids at or beyond 2**63 never produce a 500: path ids, query ids and body ids across every route.

The sweep is driven by the app's own route table (`app.openapi()`), cross-checked against the
`ROUTE_SCHEMAS` registry, so a new route with an id parameter must be added here or this file fails.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rga.config import Settings
from rga.web import app as app_module
from rga.web.app import create_app
from rga.web.schemas import ROUTE_SCHEMAS, SQLITE_MAX_INTEGER, PositiveId

ORIGIN = "http://127.0.0.1:8765"
BIG_IDS = [2**63, 2**64]
GROUP_BODY = {
    "load_id": 1,
    "lot_code": "L",
    "shots_expected": 1,
    "shot_date": "2026-10-01",
    "distance_yd": 100,
    "position": "bench",
    "suppressor": False,
}
REVIEW_BODY = {"shots": [], "shots_expected": 1}
CHECK_GROUP_BODY = {
    "lot_code": "L",
    "shots_expected": 1,
    "notes": "n",
    "position": "bench",
    "suppressor": False,
}

# (method, openapi path) -> JSON body for the routes that take one. A route with a body in the
# OpenAPI document but no entry here fails `test_the_sweep_covers_every_route_with_an_id_parameter`.
BODIES: dict[tuple[str, str], dict | None] = {
    ("PATCH", "/api/groups/{group_id}"): {"notes": "x"},
    ("POST", "/api/groups/{group_id}/detect"): {},
    ("PUT", "/api/groups/{group_id}/review"): REVIEW_BODY,
    ("PATCH", "/api/loads/{load_id}"): {"label": "x"},
    ("POST", "/api/loads/{load_id}/lots"): {"lot_code": "A"},
    ("POST", "/api/photos/{id}/reference/confirm"): {"kind": "circle_1in", "candidate_index": 0},
    ("POST", "/api/photos/{id}/reference/propose"): {"kind": "circle_1in"},
    ("POST", "/api/photos/{photo_id}/snap-aim"): {"x_px": 1, "y_px": 1},
    ("PATCH", "/api/rifles/{rifle_id}"): {"name": "x"},
    ("POST", "/api/rifles/{rifle_id}/loads"): {"kind": "factory", "label": "x"},
    ("PUT", "/api/rifles/{rifle_id}/zero-load"): {"load_id": None},
    ("POST", "/api/zeros/{zero_id}/check-groups"): CHECK_GROUP_BODY,
    ("POST", "/api/zeros/{zero_id}/checks"): {"group_id": 1},
    ("POST", "/api/zeros/{zero_id}/dialed"): {"up_mil": 0.1, "right_mil": 0.1},
    ("POST", "/api/zeros/{zero_id}/dot"): {"dot_size_in": 1.0},
    ("POST", "/api/zeros/{zero_id}/groups"): {"group_id": 1},
}

# Required query parameters for routes whose path alone would be a validation error.
QUERIES = {("GET", "/api/rifles/{rifle_id}/comparison"): "?load_ids=1"}

# Ids carried in a body or a query string: (method, path with valid ids, body or None, must be 422).
OTHER_IDS = [
    ("PUT", "/api/rifles/1/zero-load", {"load_id": 2**63}, True),
    ("POST", "/api/groups", {**GROUP_BODY, "load_id": 2**63}, True),
    ("POST", "/api/groups", {**GROUP_BODY, "load_id": 2**64}, True),
    ("POST", "/api/groups", {**GROUP_BODY, "zero_id": 2**63}, True),
    ("POST", "/api/groups", {**GROUP_BODY, "photo_id": 2**63}, True),
    ("POST", "/api/groups", {**GROUP_BODY, "suppressor": True, "suppressor_id": 2**63}, True),
    ("POST", "/api/groups", {**GROUP_BODY, "suppressor": True, "suppressor_id": 2**64}, True),
    ("PATCH", "/api/groups/1", {"suppressor": True, "suppressor_id": 2**63}, True),
    ("PATCH", "/api/groups/1", {"photo_id": 2**63}, True),
    ("PATCH", "/api/groups/1", {"zero_id": 2**63}, True),
    ("POST", "/api/aggregate", {"group_ids": [2**63]}, True),
    ("POST", "/api/aggregate", {"group_ids": [1, 2**64]}, True),
    ("POST", "/api/aggregates", {"name": "x", "group_ids": [2**63, 1]}, True),
    ("POST", "/api/aggregates", {"name": "x", "group_ids": [1, 2**64]}, True),
    ("GET", f"/api/aggregates?load_id={2**63}", None, True),
    ("POST", "/api/zeros", {"rifle_id": 2**63, "load_id": 1, "mode": "new_setup"}, True),
    ("POST", "/api/zeros", {"rifle_id": 1, "load_id": 2**63, "mode": "new_setup"}, True),
    ("POST", "/api/zeros/1/groups", {"group_id": 2**63}, True),
    ("POST", "/api/zeros/1/checks", {"group_id": 2**63}, True),
    ("POST", "/api/zeros/1/check-groups", {**CHECK_GROUP_BODY, "photo_id": 2**63}, True),
    ("POST", "/api/zeros/1/check-groups", {**CHECK_GROUP_BODY, "photo_id": 2**64}, True),
    (
        "POST",
        "/api/zeros/1/check-groups",
        {**CHECK_GROUP_BODY, "suppressor": True, "suppressor_id": 2**63},
        True,
    ),
    ("GET", f"/api/groups?load_id={2**64}", None, True),
    ("GET", f"/api/groups?rifle_id={2**63}", None, True),
    ("GET", f"/api/rifles/{2**63}/group-defaults", None, False),
    ("GET", f"/api/rifles/1/comparison?load_ids={2**63}", None, True),
    ("GET", f"/api/rifles/1/comparison?load_ids=1,{2**64}", None, True),
    ("GET", f"/api/groups/{2**63}/share.png", None, True),
    ("GET", f"/api/groups/{2**64}/share.png?layers=poi", None, True),
    ("GET", f"/compare?rifle_id={2**63}", None, False),
    ("GET", f"/dope?load_id={2**63}", None, False),
    ("GET", f"/groups?rifle_id={2**63}", None, True),
    ("GET", f"/groups?rifle_id={2**64}", None, True),
    ("GET", f"/groups?load_id={2**63}", None, True),
    ("GET", f"/groups?suppressor_id={2**63}", None, True),
    ("GET", f"/photos/1/reference?group_id={2**63}", None, False),
]


@pytest.fixture(scope="module")
def app(tmp_path_factory) -> FastAPI:
    return create_app(Settings(data_dir=tmp_path_factory.mktemp("id_bounds") / "data"))


@pytest.fixture(scope="module")
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as c:
        yield c


def openapi_routes(app: FastAPI) -> list[tuple[str, str]]:
    return sorted(
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if "{" in path
        for method in operations
    )


def fill(path: str, value: int, method: str = "GET") -> str:
    return re.sub(r"\{\w+\}", str(value), path) + QUERIES.get((method, path), "")


def send(client: TestClient, method: str, path: str, body: dict | None):
    return (
        client.request(method, path, json=body)
        if body is not None
        else client.request(method, path)
    )


def assert_error_model(response, *statuses: int) -> None:
    assert response.status_code in statuses, (response.status_code, response.text[:200])
    body = response.json()
    assert set(body) == {"detail", "code"}, body


@pytest.fixture(scope="module")
def sweep(app: FastAPI) -> list[tuple[str, str]]:
    return openapi_routes(app)


# ----------------------------------------------------------------------------------- schema bound


def test_positive_id_is_bounded_by_the_sqlite_integer_range() -> None:
    from pydantic import TypeAdapter, ValidationError

    adapter = TypeAdapter(PositiveId)
    assert adapter.validate_python(SQLITE_MAX_INTEGER) == 2**63 - 1
    for bad in (0, -1, 2**63, 2**64):
        with pytest.raises(ValidationError):
            adapter.validate_python(bad)


# ----------------------------------------------------------------------------------- the sweep


def test_requests_in_this_file_reach_the_handlers_not_the_host_guard(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200
    wrong_host = client.get("/healthz", headers={"Host": "testserver"})
    assert wrong_host.status_code == 400, (
        "the default TestClient host would make every sweep vacuous"
    )
    missing = client.get("/api/rifles/1")
    assert_error_model(missing, 404)
    assert missing.json()["code"] == "rifle_not_found", "a lookup, not a guard, produced this 404"


def test_the_sweep_covers_every_route_with_an_id_parameter(sweep) -> None:
    bodyless = {
        "/api/photos",
        "/api/groups/{group_id}/fill-shortfall",
        "/api/groups/{group_id}/finalize",
        "/api/photos/{id}/reference/reset",
    }
    needs_body = {(m, p) for m, p in sweep if m in {"POST", "PUT", "PATCH"} and p not in bodyless}
    assert needs_body <= set(BODIES), sorted(needs_body - set(BODIES))
    assert set(BODIES) <= set(sweep), sorted(set(BODIES) - set(sweep))


def test_the_share_image_route_is_in_the_sweep(sweep) -> None:
    assert ("GET", "/api/groups/{group_id}/share.png") in sweep


def test_every_registry_route_with_an_id_is_in_the_sweep(sweep) -> None:
    swept = {(m, re.sub(r"\{\w+\}", "{id}", p)) for m, p in sweep}
    registry = {
        (m, re.sub(r"\{\w+\}", "{id}", p)) for m, p in ROUTE_SCHEMAS if re.search(r"\{\w+\}", p)
    }
    assert registry <= swept, sorted(registry - swept)
    assert len(registry) >= 25


@pytest.mark.parametrize("big", BIG_IDS, ids=["2**63", "2**64"])
def test_no_route_returns_a_500_for_an_id_beyond_the_sqlite_range(
    app, client, sweep, big: int
) -> None:
    failures = []
    for method, template in sweep:
        response = send(client, method, fill(template, big, method), BODIES.get((method, template)))
        if not (400 <= response.status_code < 500) or set(response.json()) != {"detail", "code"}:
            failures.append((method, template, response.status_code, response.text[:80]))
    assert failures == []
    assert len(sweep) >= 30


@pytest.mark.parametrize("big", BIG_IDS, ids=["2**63", "2**64"])
def test_check_groups_path_id_beyond_the_range_is_exactly_a_422_validation_error(
    client, big: int
) -> None:
    """The zero id is bounded at the path, so validation answers before the handler can say 404."""
    response = client.post(f"/api/zeros/{big}/check-groups", json=CHECK_GROUP_BODY)
    assert_error_model(response, 422)
    assert response.json()["code"] == "validation_error"


def test_check_groups_path_id_at_the_top_of_the_range_is_zero_not_found(client) -> None:
    response = client.post(f"/api/zeros/{SQLITE_MAX_INTEGER}/check-groups", json=CHECK_GROUP_BODY)
    assert_error_model(response, 404)
    assert response.json()["code"] == "zero_not_found"


def test_ids_at_the_top_of_the_range_are_ordinary_not_found(client, sweep) -> None:
    top = SQLITE_MAX_INTEGER
    for method, template in sweep:
        response = send(client, method, fill(template, top, method), BODIES.get((method, template)))
        assert_error_model(response, 404)


def test_positive_control_small_ids_reach_the_handlers_and_are_not_found(client, sweep) -> None:
    """With valid bodies and a small missing id every route answers from its handler (404)."""
    for method, template in sweep:
        response = send(client, method, fill(template, 1, method), BODIES.get((method, template)))
        assert_error_model(response, 404)
        assert response.json()["code"].endswith("not_found"), (method, template, response.json())


@pytest.mark.parametrize(("method", "path", "body", "must_be_422"), OTHER_IDS)
def test_body_and_query_ids_beyond_the_range_are_rejected(
    client, method, path, body, must_be_422
) -> None:
    response = send(client, method, path, body)
    assert_error_model(response, 422) if must_be_422 else assert_error_model(response, 404, 422)
    if must_be_422:
        assert response.json()["code"] == "validation_error"


def shrink(value):
    """The same request with every id beyond the SQLite range replaced by 1."""
    return json.loads(re.sub(r"\b\d{19,}\b", "1", json.dumps(value)))


@pytest.mark.parametrize(("method", "path", "body", "must_be_422"), OTHER_IDS)
def test_control_the_same_requests_with_in_range_ids_are_not_id_validation_errors(
    client, method, path, body, must_be_422
) -> None:
    small_path = re.sub(r"\b\d{19,}\b", "1", path)
    response = send(client, method, small_path, None if body is None else shrink(body))
    assert response.status_code < 500
    if response.status_code == 422:
        assert "less than or equal" not in response.json()["detail"]


# ----------------------------------------------------------------------------------- the handler


def test_the_overflow_handler_only_claims_the_sqlite_bind_error(app) -> None:
    @app.get("/api/_overflow_sqlite")
    def sqlite_overflow() -> None:
        sqlite3.connect(":memory:").execute("SELECT ?", (2**63,))

    @app.get("/api/_overflow_other")
    def other_overflow() -> None:
        raise OverflowError("math range error")

    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as lenient:
        assert_error_model(lenient.get("/api/_overflow_sqlite"), 404)
        assert lenient.get("/api/_overflow_sqlite").json()["code"] == "not_found"
        other = lenient.get("/api/_overflow_other")
        assert_error_model(other, 500)
        assert other.json()["code"] == "internal_error"
    assert app_module.SQLITE_OVERFLOW_MESSAGE in "Python int too large to convert to SQLite INTEGER"
