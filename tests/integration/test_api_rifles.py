"""T3.1: rifles, loads and lots API, the zero-load rule, the dashboard and the rifle pages."""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from rga.config import Settings
from rga.db.connection import connect
from rga.domain.calibers import CALIBERS
from rga.domain.types import Offset, Position
from rga.services import rifles as rifle_service
from rga.services import zeroing as zeroing_service
from rga.web.app import create_app
from rga.web.schemas import ROUTE_SCHEMAS, LoadOut, LotOut, RifleDetailOut, RifleOut
from tests.support.live_server import run_live_server
from tests.support.seed import seed_context, seed_group, seed_load, seed_rifle

ORIGIN = "http://127.0.0.1:8765"
XSS = "<script>alert(1)</script>"
WEB = Path(__file__).resolve().parents[2] / "src" / "rga" / "web"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=ORIGIN) as c:
        yield c


@pytest.fixture
def conn(settings: Settings, app: FastAPI) -> Iterator[sqlite3.Connection]:
    connection = connect(settings.db_path)
    try:
        yield connection
    finally:
        connection.close()


def make_rifle(client: TestClient, **overrides) -> dict:
    body = {"name": "Tikka", "cartridge": "6.5 Creedmoor", **overrides}
    response = client.post("/api/rifles", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def make_load(client: TestClient, rifle_id: int, **overrides) -> dict:
    body = {"kind": "factory", "label": "Fed GMM 168", "maker": "Federal", **overrides}
    response = client.post(f"/api/rifles/{rifle_id}/loads", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"detail", "code"} and body["code"] == code, body


# ----------------------------------------------------------------------------------- calibers


def test_calibers_return_the_table(client: TestClient) -> None:
    response = client.get("/api/calibers")
    assert response.status_code == 200
    rows = response.json()
    assert {r["cartridge"]: r["bullet_diameter_in"] for r in rows} == CALIBERS
    TypeAdapter(ROUTE_SCHEMAS[("GET", "/api/calibers")].response).validate_python(rows)


# ----------------------------------------------------------------------------------- rifles


def test_creating_a_rifle_prefills_the_diameter_from_the_caliber_table(client: TestClient) -> None:
    rifle = make_rifle(client, name="  Bergara  ", cartridge=" .308 win ", scope="NF 5-25")
    RifleOut.model_validate(rifle)
    assert rifle["bullet_diameter_in"] == 0.308 and rifle["name"] == "Bergara"
    assert rifle["scope"] == "NF 5-25" and rifle["zero_load_id"] is None and rifle["notes"] == ""
    assert re.fullmatch(r"[0-9a-f-]{36}", rifle["public_id"])


def test_an_explicit_diameter_overrides_the_table_and_unknown_cartridges_accept_one(
    client: TestClient,
) -> None:
    assert make_rifle(client, bullet_diameter_in=0.25)["bullet_diameter_in"] == 0.25
    custom = make_rifle(client, cartridge="6mm Dasher", bullet_diameter_in=0.243)
    assert custom["bullet_diameter_in"] == 0.243


def test_an_unknown_cartridge_without_a_diameter_is_a_422(client: TestClient, conn) -> None:
    error(
        client.post("/api/rifles", json={"name": "x", "cartridge": "6mm Dasher"}),
        422,
        "invalid_cartridge",
    )
    assert conn.execute("SELECT COUNT(*) FROM rifles").fetchone()[0] == 0


@pytest.mark.parametrize(
    "body",
    [
        {"cartridge": "6.5 Creedmoor"},
        {"name": "", "cartridge": "6.5 Creedmoor"},
        {"name": "x", "cartridge": "6.5 Creedmoor", "bullet_diameter_in": 0.1},
        {"name": "x", "cartridge": "6.5 Creedmoor", "bullet_diameter_in": 0.7},
        {"name": "x", "cartridge": "6.5 Creedmoor", "bolt": "Remington"},
        {"name": "x", "cartridge": "6.5 Creedmoor", "zero_load_id": 1},
    ],
)
def test_invalid_rifles_are_rejected_with_the_error_model(client: TestClient, body: dict) -> None:
    error(client.post("/api/rifles", json=body), 422, "validation_error")


def test_rifles_list_in_name_order_and_validate_against_the_schema(client: TestClient) -> None:
    make_rifle(client, name="zeta")
    make_rifle(client, name="Alpha")
    response = client.get("/api/rifles")
    assert response.status_code == 200
    assert [r["name"] for r in response.json()] == ["Alpha", "zeta"]
    TypeAdapter(ROUTE_SCHEMAS[("GET", "/api/rifles")].response).validate_python(response.json())


def test_rifle_detail_includes_its_loads(client: TestClient) -> None:
    rifle = make_rifle(client)
    other = make_rifle(client, name="Other")
    load = make_load(client, rifle["id"])
    make_load(client, other["id"], label="not mine")
    response = client.get(f"/api/rifles/{rifle['id']}")
    assert response.status_code == 200
    detail = RifleDetailOut.model_validate(response.json())
    assert [item.id for item in detail.loads] == [load["id"]]


def test_a_missing_rifle_is_a_404_everywhere(client: TestClient) -> None:
    error(client.get("/api/rifles/999"), 404, "rifle_not_found")
    error(client.patch("/api/rifles/999", json={"name": "x"}), 404, "rifle_not_found")
    error(client.put("/api/rifles/999/zero-load", json={"load_id": None}), 404, "rifle_not_found")
    error(client.get("/api/rifles/999/loads"), 404, "rifle_not_found")
    error(
        client.post("/api/rifles/999/loads", json={"kind": "factory", "label": "x"}),
        404,
        "rifle_not_found",
    )


def test_patching_a_rifle_changes_only_the_given_fields(client: TestClient) -> None:
    rifle = make_rifle(client, scope="old scope", notes="keep")
    response = client.patch(f"/api/rifles/{rifle['id']}", json={"name": "Renamed", "scope": None})
    assert response.status_code == 200
    updated = RifleOut.model_validate(response.json())
    assert updated.name == "Renamed" and updated.scope is None
    assert updated.cartridge == rifle["cartridge"] and updated.notes == "keep"
    assert updated.updated_at >= rifle["updated_at"]


@pytest.mark.parametrize(
    "body",
    [{}, {"name": None}, {"cartridge": None}, {"bullet_diameter_in": 0.6}, {"zero_load_id": 3}],
)
def test_invalid_rifle_patches_change_nothing(client: TestClient, body: dict) -> None:
    rifle = make_rifle(client)
    error(client.patch(f"/api/rifles/{rifle['id']}", json=body), 422, "validation_error")
    assert client.get(f"/api/rifles/{rifle['id']}").json()["name"] == rifle["name"]


# ----------------------------------------------------------------------------------- zero load


def test_the_zero_load_can_be_set_and_cleared(client: TestClient) -> None:
    rifle = make_rifle(client)
    load = make_load(client, rifle["id"])
    set_response = client.put(f"/api/rifles/{rifle['id']}/zero-load", json={"load_id": load["id"]})
    assert set_response.status_code == 200
    assert RifleOut.model_validate(set_response.json()).zero_load_id == load["id"]
    cleared = client.put(f"/api/rifles/{rifle['id']}/zero-load", json={"load_id": None})
    assert cleared.status_code == 200 and cleared.json()["zero_load_id"] is None


def test_a_zero_load_from_another_rifle_is_a_409_and_changes_nothing(client: TestClient) -> None:
    rifle = make_rifle(client)
    mine = make_load(client, rifle["id"])
    other = make_rifle(client, name="Other")
    foreign = make_load(client, other["id"], label="foreign")
    client.put(f"/api/rifles/{rifle['id']}/zero-load", json={"load_id": mine["id"]})
    error(
        client.put(f"/api/rifles/{rifle['id']}/zero-load", json={"load_id": foreign["id"]}),
        409,
        "load_not_in_rifle",
    )
    assert client.get(f"/api/rifles/{rifle['id']}").json()["zero_load_id"] == mine["id"]


def test_a_nonexistent_zero_load_is_a_409(client: TestClient) -> None:
    rifle = make_rifle(client)
    error(
        client.put(f"/api/rifles/{rifle['id']}/zero-load", json={"load_id": 4242}),
        409,
        "load_not_in_rifle",
    )


@pytest.mark.parametrize("body", [{}, {"load_id": 0}, {"load_id": "x"}, {"load_id": 1, "extra": 1}])
def test_a_malformed_zero_load_request_is_a_422(client: TestClient, body: dict) -> None:
    rifle = make_rifle(client)
    error(client.put(f"/api/rifles/{rifle['id']}/zero-load", json=body), 422, "validation_error")


# ----------------------------------------------------------------------------------- loads


def test_a_factory_load_stores_only_factory_fields(client: TestClient) -> None:
    rifle = make_rifle(client)
    load = make_load(
        client,
        rifle["id"],
        product="Gold Medal",
        bullet_weight_gr=168,
        bullet_type="BTHP",
        trued_mv_fps=2650,
        notes="match grade",
    )
    LoadOut.model_validate(load)
    assert load["kind"] == "factory" and load["maker"] == "Federal" and load["trued_mv_fps"] == 2650
    assert load["bullet"] is None and load["brass"] is None and load["coal_in"] is None
    assert load["notes"] == "match grade"


def test_a_handload_stores_only_handload_fields(client: TestClient) -> None:
    rifle = make_rifle(client)
    load = make_load(
        client,
        rifle["id"],
        kind="handload",
        label="116 gr TMK",
        maker=None,
        bullet_weight_gr=116,
        bullet="TMK",
        primer="CCI BR-2",
        brass="Lapua",
        coal_in=2.8,
        trued_mv_fps=2900,
    )
    LoadOut.model_validate(load)
    assert load["kind"] == "handload" and load["maker"] is None and load["product"] is None
    assert (load["bullet_weight_gr"], load["bullet"], load["primer"]) == (116, "TMK", "CCI BR-2")
    assert (load["brass"], load["coal_in"], load["trued_mv_fps"]) == ("Lapua", 2.8, 2900)
    assert "powder" not in load and "charge_gr" not in load


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "factory", "bullet": "TMK"},
        {"kind": "factory", "primer": "BR-2"},
        {"kind": "factory", "brass": "Lapua"},
        {"kind": "factory", "coal_in": 2.8},
        {"kind": "handload", "maker": "Federal"},
        {"kind": "handload", "product": "Gold Medal"},
        {"kind": "handload", "bullet_type": "BTHP"},
    ],
)
def test_loads_with_fields_of_the_other_kind_are_a_422(
    client: TestClient, conn, fields: dict
) -> None:
    rifle = make_rifle(client)
    body = {"label": "x", **fields}
    error(client.post(f"/api/rifles/{rifle['id']}/loads", json=body), 422, "validation_error")
    assert conn.execute("SELECT COUNT(*) FROM loads").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["handload", "factory"])
@pytest.mark.parametrize(
    "extra",
    [
        {"powder": "H4350"},
        {"charge_gr": 41.0},
        {"cbto_in": 2.2},
        {"velocity_sd_fps": 8},
        {"velocity_es_fps": 25},
        {"seating_depth_in": 0.02},
    ],
)
def test_the_removed_fields_are_not_accepted(client: TestClient, kind: str, extra: dict) -> None:
    rifle = make_rifle(client)
    body = {"kind": kind, "label": "x", **extra}
    error(client.post(f"/api/rifles/{rifle['id']}/loads", json=body), 422, "validation_error")
    hl = make_load(client, rifle["id"], kind="handload", label="hl", maker=None, bullet="TMK")
    error(client.patch(f"/api/loads/{hl['id']}", json=extra), 422, "validation_error")


@pytest.mark.parametrize("coal", [1.0, 2.8, 5.0, 3.125])
def test_coal_is_accepted_between_one_and_five_inches_with_three_decimals(
    client: TestClient, coal: float
) -> None:
    rifle = make_rifle(client)
    load = make_load(client, rifle["id"], kind="handload", label="c", maker=None, coal_in=coal)
    assert load["coal_in"] == coal
    patched = client.patch(f"/api/loads/{load['id']}", json={"coal_in": None})
    assert patched.status_code == 200 and patched.json()["coal_in"] is None


@pytest.mark.parametrize("coal", [0.999, 5.001, 0, -2.8, 2.8005, 2.12345])
def test_coal_outside_one_to_five_inches_or_beyond_three_decimals_is_a_422(
    client: TestClient, coal: float
) -> None:
    rifle = make_rifle(client)
    body = {"kind": "handload", "label": "c", "coal_in": coal}
    error(client.post(f"/api/rifles/{rifle['id']}/loads", json=body), 422, "validation_error")
    hl = make_load(client, rifle["id"], kind="handload", label="hl", maker=None)
    error(client.patch(f"/api/loads/{hl['id']}", json={"coal_in": coal}), 422, "validation_error")


def test_a_handload_bullet_is_a_weight_and_a_line_that_patch_independently(
    client: TestClient,
) -> None:
    rifle = make_rifle(client)
    load = make_load(
        client,
        rifle["id"],
        kind="handload",
        label="116 gr TMK",
        maker=None,
        bullet_weight_gr=116,
        bullet="TMK",
    )
    first = client.patch(f"/api/loads/{load['id']}", json={"bullet_weight_gr": 120}).json()
    assert (first["bullet_weight_gr"], first["bullet"]) == (120, "TMK")
    second = client.patch(f"/api/loads/{load['id']}", json={"bullet": "ELD-M"}).json()
    assert (second["bullet_weight_gr"], second["bullet"]) == (120, "ELD-M")
    error(
        client.patch(f"/api/loads/{load['id']}", json={"bullet_weight_gr": 0}),
        422,
        "validation_error",
    )


@pytest.mark.parametrize(
    "body",
    [
        {"label": "x"},
        {"kind": "factory"},
        {"kind": "rimfire", "label": "x"},
        {"kind": "factory", "label": ""},
        {"kind": "factory", "label": "x", "trued_mv_fps": 0},
        {"kind": "factory", "label": "x", "bullet_weight_gr": -1},
        {"kind": "handload", "label": "x", "bullet_weight_gr": 0},
        {"kind": "handload", "label": "x", "coal_in": 0.5},
    ],
)
def test_invalid_loads_are_a_422(client: TestClient, body: dict) -> None:
    rifle = make_rifle(client)
    error(client.post(f"/api/rifles/{rifle['id']}/loads", json=body), 422, "validation_error")


def test_loads_list_in_creation_order(client: TestClient) -> None:
    rifle = make_rifle(client)
    ids = [make_load(client, rifle["id"], label=f"L{i}")["id"] for i in range(3)]
    response = client.get(f"/api/rifles/{rifle['id']}/loads")
    assert [load["id"] for load in response.json()] == ids
    TypeAdapter(ROUTE_SCHEMAS[("GET", "/api/rifles/{id}/loads")].response).validate_python(
        response.json()
    )


def test_a_rifles_load_list_holds_only_its_own_loads(client: TestClient) -> None:
    first, second = make_rifle(client, name="One"), make_rifle(client, name="Two")
    make_load(client, first["id"], label="first's")
    mine = make_load(client, second["id"], label="second's")
    response = client.get(f"/api/rifles/{second['id']}/loads")
    assert [load["id"] for load in response.json()] == [mine["id"]]


def test_the_service_refuses_other_kind_fields_even_without_the_schema(conn) -> None:
    rifle = seed_rifle(conn)
    with pytest.raises(rifle_service.RifleError) as caught:
        rifle_service.create_load(
            conn, rifle.id, {"kind": "factory", "label": "x", "brass": "Lapua"}
        )
    assert caught.value.code == "invalid_load_fields"
    assert conn.execute("SELECT COUNT(*) FROM loads").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("kind", "foreign"),
    [
        *[
            ("factory", {name: value})
            for name, value in (
                ("bullet", "TMK"),
                ("primer", "BR-2"),
                ("brass", "Lapua"),
                ("coal_in", 2.8),
            )
        ],
        *[
            ("handload", {name: value})
            for name, value in (("maker", "Fed"), ("product", "GMM"), ("bullet_type", "BTHP"))
        ],
    ],
)
def test_the_service_refuses_every_field_of_the_other_kind_on_create_and_update(
    conn, kind: str, foreign: dict
) -> None:
    rifle = seed_rifle(conn)
    with pytest.raises(rifle_service.RifleError) as created:
        rifle_service.create_load(conn, rifle.id, {"kind": kind, "label": "x", **foreign})
    assert created.value.code == "invalid_load_fields"
    own = seed_load(conn, rifle, kind=kind, label="own")
    with pytest.raises(rifle_service.RifleError) as updated:
        rifle_service.update_load(conn, own.id, foreign)
    assert updated.value.code == "invalid_load_fields"
    assert conn.execute("SELECT COUNT(*) FROM loads").fetchone()[0] == 1


def test_the_bullet_weight_is_shared_by_both_kinds(conn) -> None:
    rifle = seed_rifle(conn)
    for kind in ("factory", "handload"):
        load = rifle_service.create_load(
            conn, rifle.id, {"kind": kind, "label": kind, "bullet_weight_gr": 140.0}
        )
        assert load.bullet_weight_gr == 140.0
        assert (
            rifle_service.update_load(conn, load.id, {"bullet_weight_gr": 150.0}).bullet_weight_gr
            == 150.0
        )


def test_patching_a_load_changes_the_given_fields_and_can_clear_the_trued_mv(
    client: TestClient,
) -> None:
    rifle = make_rifle(client)
    load = make_load(client, rifle["id"], trued_mv_fps=2700)
    response = client.patch(
        f"/api/loads/{load['id']}",
        json={"label": "Renamed", "maker": "Hornady", "trued_mv_fps": None},
    )
    assert response.status_code == 200
    updated = LoadOut.model_validate(response.json())
    assert (updated.label, updated.maker, updated.trued_mv_fps) == ("Renamed", "Hornady", None)
    assert updated.kind.value == "factory" and updated.rifle_id == rifle["id"]


@pytest.mark.parametrize(
    "body",
    [{}, {"kind": "handload"}, {"rifle_id": 2}, {"label": None}, {"trued_mv_fps": 0}],
)
def test_invalid_load_patches_are_a_422(client: TestClient, body: dict) -> None:
    rifle = make_rifle(client)
    load = make_load(client, rifle["id"])
    error(client.patch(f"/api/loads/{load['id']}", json=body), 422, "validation_error")


def test_patching_a_load_with_the_other_kinds_fields_is_a_422_and_changes_nothing(
    client: TestClient,
) -> None:
    rifle = make_rifle(client)
    factory = make_load(client, rifle["id"])
    handload = make_load(
        client, rifle["id"], kind="handload", label="hl", maker=None, primer="BR-2"
    )
    error(
        client.patch(f"/api/loads/{factory['id']}", json={"label": "z", "brass": "Lapua"}),
        422,
        "invalid_load_fields",
    )
    error(
        client.patch(f"/api/loads/{handload['id']}", json={"maker": "Federal"}),
        422,
        "invalid_load_fields",
    )
    loads = {load["id"]: load for load in client.get(f"/api/rifles/{rifle['id']}/loads").json()}
    assert loads[factory["id"]]["label"] == "Fed GMM 168" and loads[factory["id"]]["brass"] is None
    assert loads[handload["id"]]["maker"] is None


def test_a_missing_load_is_a_404(client: TestClient) -> None:
    error(client.patch("/api/loads/999", json={"label": "x"}), 404, "load_not_found")
    error(client.get("/api/loads/999/lots"), 404, "load_not_found")
    error(client.post("/api/loads/999/lots", json={"lot_code": "A"}), 404, "load_not_found")


# ----------------------------------------------------------------------------------- lots


def test_a_lot_is_created_once_and_returned_again(client: TestClient) -> None:
    rifle = make_rifle(client)
    load = make_load(client, rifle["id"])
    first = client.post(f"/api/loads/{load['id']}/lots", json={"lot_code": " A1 "})
    assert first.status_code == 201
    again = client.post(f"/api/loads/{load['id']}/lots", json={"lot_code": "A1"})
    assert again.status_code == 200 and again.json() == first.json()
    LotOut.model_validate(first.json())
    other = client.post(f"/api/loads/{load['id']}/lots", json={"lot_code": "B2"})
    assert other.status_code == 201 and other.json()["id"] != first.json()["id"]
    listed = client.get(f"/api/loads/{load['id']}/lots")
    assert [lot["lot_code"] for lot in listed.json()] == ["A1", "B2"]
    TypeAdapter(ROUTE_SCHEMAS[("GET", "/api/loads/{id}/lots")].response).validate_python(
        listed.json()
    )


def test_the_same_lot_code_under_two_loads_is_two_lots(client: TestClient) -> None:
    rifle = make_rifle(client)
    a, b = make_load(client, rifle["id"], label="a"), make_load(client, rifle["id"], label="b")
    ids = {
        client.post(f"/api/loads/{x['id']}/lots", json={"lot_code": "L"}).json()["id"]
        for x in (a, b)
    }
    assert len(ids) == 2


@pytest.mark.parametrize(
    "body",
    [{}, {"lot_code": ""}, {"lot_code": "   "}, {"lot_code": "x" * 65}, {"lot_code": "A", "x": 1}],
)
def test_invalid_lots_are_a_422(client: TestClient, body: dict) -> None:
    rifle = make_rifle(client)
    load = make_load(client, rifle["id"])
    error(client.post(f"/api/loads/{load['id']}/lots", json=body), 422, "validation_error")


# ----------------------------------------------------------------------------------- guards and errors


WRITES = [
    ("POST", "/api/rifles", {"name": "x", "cartridge": ".308 Win"}),
    ("PATCH", "/api/rifles/{r}", {"name": "z"}),
    ("PUT", "/api/rifles/{r}/zero-load", {"load_id": None}),
    ("POST", "/api/rifles/{r}/loads", {"kind": "factory", "label": "z"}),
    ("PATCH", "/api/loads/{l}", {"label": "z"}),
    ("POST", "/api/loads/{l}/lots", {"lot_code": "ZZ"}),
]


def snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    return {
        table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
        for table in ("rifles", "loads", "load_lots")
    }


@pytest.mark.parametrize(("method", "path", "body"), WRITES, ids=[f"{m} {p}" for m, p, _ in WRITES])
def test_every_write_refuses_a_cross_origin_request_and_changes_nothing(
    client: TestClient, conn, method: str, path: str, body: dict
) -> None:
    rifle = make_rifle(client)
    load = make_load(client, rifle["id"])
    before = snapshot(conn)
    url = path.format(r=rifle["id"], l=load["id"])
    response = client.request(method, url, json=body, headers={"Origin": "http://evil.example"})
    assert response.status_code == 403
    assert snapshot(conn) == before


@pytest.mark.parametrize(("method", "path", "body"), WRITES, ids=[f"{m} {p}" for m, p, _ in WRITES])
def test_every_write_refuses_a_non_json_content_type(
    client: TestClient, conn, method: str, path: str, body: dict
) -> None:
    rifle = make_rifle(client)
    load = make_load(client, rifle["id"])
    before = snapshot(conn)
    url = path.format(r=rifle["id"], l=load["id"])
    response = client.request(
        method, url, content=b"name=x", headers={"Content-Type": "text/plain"}
    )
    assert response.status_code == 415
    assert snapshot(conn) == before


def test_a_locked_database_is_a_503_busy_not_a_500(client: TestClient, monkeypatch) -> None:
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(rifle_service, "create_rifle", locked)
    error(client.post("/api/rifles", json={"name": "x", "cartridge": ".308 Win"}), 503, "busy")


def test_a_constraint_violation_is_a_409_not_a_500(client: TestClient, monkeypatch) -> None:
    def violated(*args, **kwargs):
        raise sqlite3.IntegrityError("CHECK constraint failed")

    monkeypatch.setattr(rifle_service, "create_rifle", violated)
    error(
        client.post("/api/rifles", json={"name": "x", "cartridge": ".308 Win"}),
        409,
        "constraint_violation",
    )


def test_other_database_errors_are_not_disguised(app: FastAPI, monkeypatch) -> None:
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(rifle_service, "create_rifle", broken)
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as lenient:
        response = lenient.post("/api/rifles", json={"name": "x", "cartridge": ".308 Win"})
    error(response, 500, "internal_error")


def test_a_failed_write_rolls_back(client: TestClient, conn, monkeypatch) -> None:
    rifle = make_rifle(client)
    original = rifle_service.repo_rifles.create_load

    def create_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.IntegrityError("late failure")

    monkeypatch.setattr(rifle_service.repo_rifles, "create_load", create_then_fail)
    error(
        client.post(f"/api/rifles/{rifle['id']}/loads", json={"kind": "factory", "label": "x"}),
        409,
        "constraint_violation",
    )
    assert conn.execute("SELECT COUNT(*) FROM loads").fetchone()[0] == 0


def test_the_routes_of_this_slice_match_the_schema_registry(app: FastAPI) -> None:
    served = {
        (method.upper(), re.sub(r"\{\w+\}", "{id}", path))
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    mine = [
        key
        for key in ROUTE_SCHEMAS
        if key[1].startswith(("/api/rifles", "/api/calibers", "/api/loads"))
        and not key[1].startswith("/api/rifles/{id}/comparison")
        and not (key[0] == "GET" and key[1] == "/api/loads/{id}/dope")
    ]
    assert len(mine) == 12
    assert set(mine) <= served


# ----------------------------------------------------------------------------------- pages


def page(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    return response.text


def test_the_rifles_page_lists_rifles_and_has_the_new_rifle_form(client: TestClient, conn) -> None:
    seed_rifle(conn, name="List Rifle", cartridge=".243 Win")
    html = page(client, "/rifles")
    assert "List Rifle" in html and ".243 Win" in html
    assert 'data-form="new-rifle"' in html and 'list="cartridge-list"' in html
    assert '<script type="module" src="/static/js/rifles.js">' in html


def test_the_rifle_page_shows_loads_lots_and_the_zero_load_selector(
    client: TestClient, conn
) -> None:
    rifle = seed_rifle(conn, name="Detail Rifle")
    factory = seed_load(conn, rifle, label="Factory A", maker="Federal", bullet_weight_gr=168.0)
    seed_load(
        conn,
        rifle,
        kind="handload",
        label="Hand B",
        bullet_weight_gr=116.0,
        bullet="TMK",
        primer="CCI BR-2",
        brass="Lapua",
        coal_in=2.8,
    )
    with conn:
        conn.execute(
            "INSERT INTO load_lots (load_id, lot_code, created_at) VALUES (?, 'LOT-77', 't')",
            (factory.id,),
        )
    html = page(client, f"/rifles/{rifle.id}")
    for expected in (
        "Detail Rifle",
        "Factory A",
        "Hand B",
        "LOT-77",
        "Federal",
        "116 gr TMK",
        "CCI BR-2",
        "Lapua",
        "2.800 in",
        'data-form="zero-load"',
    ):
        assert expected in html, expected
    assert f'data-rifle-id="{rifle.id}"' in html
    assert 'id="new-group-card"' in html and 'data-form="new-load"' in html


def test_user_text_is_escaped_on_every_page(client: TestClient, conn) -> None:
    rifle = seed_rifle(conn, name=XSS, scope=XSS, notes=XSS)
    load = seed_load(conn, rifle, label=XSS, maker=XSS, notes=XSS)
    with conn:
        conn.execute(
            "INSERT INTO load_lots (load_id, lot_code, created_at) VALUES (?, ?, 't')",
            (load.id, XSS),
        )
        conn.execute("UPDATE rifles SET zero_load_id = ? WHERE id = ?", (load.id, rifle.id))
    seed_group(
        conn,
        context=seed_context(rifle, notes=XSS),
        load=load,
        offsets=[Offset(0.1, 0.1)],
        notes=XSS,
        lot_code=XSS,
    )
    for path in ("/", "/rifles", f"/rifles/{rifle.id}"):
        html = page(client, path)
        assert XSS not in html, path
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html, path
        assert all('src="/static/js/' in tag for tag in re.findall(r"<script\b[^>]*>", html)), path


def test_the_pages_have_no_native_form_posts_and_no_inline_code(client: TestClient, conn) -> None:
    rifle = seed_rifle(conn)
    seed_load(conn, rifle)
    for path in ("/rifles", f"/rifles/{rifle.id}"):
        html = page(client, path)
        for form in re.findall(r"<form\b[^>]*>", html):
            assert "action=" not in form and "method=" not in form, form
        assert not re.search(r"\sstyle\s*=|\son[a-z]+\s*=|javascript:", html), path
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), path


def field_names(fragment: str) -> set[str]:
    return set(re.findall(r'name="([a-z_]+)"', fragment))


def test_the_load_forms_offer_exactly_the_b64_handload_fields(client: TestClient, conn) -> None:
    rifle = seed_rifle(conn)
    seed_load(conn, rifle)
    seed_load(conn, rifle, kind="handload", label="Hand", bullet_weight_gr=116.0, bullet="TMK")
    html = page(client, f"/rifles/{rifle.id}")
    new_form = html.split('data-form="new-load"')[1].split("</form>")[0]
    handload = new_form.split('data-kind-fields="handload"')[1].split('data-kind-fields="')[0]
    handload = handload.split("trued_mv_fps")[0]
    assert field_names(handload) == {
        "handload_bullet_weight_gr",
        "bullet",
        "primer",
        "brass",
        "coal_in",
    }
    factory = new_form.split('data-kind-fields="factory"')[1].split('data-kind-fields="handload"')[
        0
    ]
    assert field_names(factory) == {"maker", "product", "bullet_weight_gr", "bullet_type"}
    assert field_names(new_form) >= {"kind", "label", "trued_mv_fps", "notes"}
    edit = html.split('data-kind="handload"')[1].split("</form>")[0]
    assert field_names(edit) == {
        "label",
        "bullet_weight_gr",
        "bullet",
        "primer",
        "brass",
        "coal_in",
        "trued_mv_fps",
        "notes",
    }
    for text in ("weight (gr)", "line, e.g. TMK", "COAL (in)", "Brass", "Primer"):
        assert text in new_form, text
    assert 'name="coal_in" step="0.001" min="1" max="5"' in edit
    lowered = html.lower()
    for banned in (
        "powder",
        "charge",
        "cbto",
        "seating",
        "velocity sd",
        "velocity es",
        "standard deviation",
    ):
        assert banned not in lowered, banned
    assert "trued_mv_fps" in html and "lot_code" in html


def test_a_handload_card_shows_the_bullet_as_weight_gr_and_line(client: TestClient, conn) -> None:
    rifle = seed_rifle(conn)
    seed_load(conn, rifle, kind="handload", label="L1", bullet_weight_gr=116.0, bullet="TMK")
    seed_load(conn, rifle, kind="handload", label="L2", bullet_weight_gr=140.5)
    seed_load(conn, rifle, kind="handload", label="L3", bullet="Custom pill")
    html = page(client, f"/rifles/{rifle.id}")
    cards = re.findall(r'<article class="card" data-load-id="\d+">(.*?)</article>', html, re.S)
    text = [" ".join(re.sub(r"<[^>]+>", " ", c).split()) for c in cards]
    assert "116 gr TMK" in text[0] and "140.5 gr" in text[1] and "Custom pill" in text[2]
    assert "116.0" not in text[0]


def test_the_group_form_offers_every_position_and_not_sandbags(client: TestClient, conn) -> None:
    rifle = seed_rifle(conn)
    seed_load(conn, rifle)
    html = page(client, f"/rifles/{rifle.id}")
    options = re.findall(
        r'<option value="([a-z_]+)"[^>]*>', html.split('id="g-position"')[1].split("</select>")[0]
    )
    assert options == [p.value for p in Position]
    assert "sandbags" not in html


def test_a_missing_rifle_page_is_a_404_in_the_error_model(client: TestClient) -> None:
    error(client.get("/rifles/999"), 404, "rifle_not_found")


def test_the_page_script_is_served_and_only_writes_through_api_js(client: TestClient) -> None:
    response = client.get("/static/js/rifles.js")
    assert response.status_code == 200
    source = response.text
    assert 'from "/static/js/api.js"' in source
    assert not re.search(r"\bfetch\s*\(", source) and ".submit(" not in source
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", source)
    submit = source.split("function onSubmit")[1].split("\nfunction reload")[0]
    assert "catch (error)" in submit and "showError(error)" in submit
    assert source.count("apiSend(") == 7 and "event.preventDefault()" in source
    for method, path in (
        ("POST", "/api/rifles"),
        ("PATCH", "/api/rifles/"),
        ("PUT", "/api/rifles/"),
        ("POST", "/api/rifles/${rifleId}/loads"),
        ("PATCH", "/api/loads/"),
        ("POST", "/api/loads/"),
        ("POST", "/api/zeros"),
    ):
        assert (
            f'apiSend("{method}", `{path}' in source or f'apiSend("{method}", "{path}' in source
        ), (method, path)


# ----------------------------------------------------------------------------------- zeroing section

DIALED_UP, DIALED_RIGHT = 7.654321, 2.345678


def start_zero(client: TestClient, rifle_id: int, load_id: int, **overrides) -> dict:
    body = {"rifle_id": rifle_id, "load_id": load_id, "mode": "new_setup", **overrides}
    response = client.post("/api/zeros", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def set_state(conn, zero_id: int, state: str, *, dialed: bool = False) -> None:
    with conn:
        conn.execute(
            "UPDATE zeros SET state = ?, dialed_up_mil = ?, dialed_right_mil = ? WHERE id = ?",
            (state, DIALED_UP if dialed else None, DIALED_RIGHT if dialed else None, zero_id),
        )


def zero_rows(html: str) -> list[tuple[int, str]]:
    return [
        (int(i), state)
        for i, state in re.findall(r'<tr data-zero-id="(\d+)" data-state="(\w+)"', html)
    ]


def test_the_rifle_page_has_a_start_a_zero_form_that_defaults_to_the_zero_load(
    client: TestClient,
) -> None:
    rifle = make_rifle(client)
    first = make_load(client, rifle["id"], label="Load One")
    second = make_load(client, rifle["id"], label="Load Two")
    assert client.put(
        f"/api/rifles/{rifle['id']}/zero-load", json={"load_id": second["id"]}
    ).is_success
    html = page(client, f"/rifles/{rifle['id']}")
    form = html.split('data-form="new-zero"')[1].split("</form>")[0]
    assert "Start a zero" in form and 'name="lot_code"' in form
    assert f'<option value="{first["id"]}">Load One</option>' in form
    assert f'<option value="{second["id"]}" selected>Load Two</option>' in form
    assert (
        'value="new_setup">New setup / bore-sighted' in form and 'value="existing">Existing' in form
    )


def test_a_rifle_without_loads_has_no_start_form_and_no_zeros(client: TestClient) -> None:
    rifle = make_rifle(client)
    html = page(client, f"/rifles/{rifle['id']}")
    assert 'data-form="new-zero"' not in html and "No zeros yet" in html
    assert "Add a load first" in html


def test_the_rifle_page_lists_zeros_newest_first_with_state_load_lot_date_and_link(
    client: TestClient, conn
) -> None:
    rifle = make_rifle(client)
    load_a = make_load(client, rifle["id"], label="Load Alpha")
    load_b = make_load(client, rifle["id"], label="Load Beta")
    old = start_zero(client, rifle["id"], load_a["id"], lot_code="LOT-A1")
    mid = start_zero(client, rifle["id"], load_a["id"], mode="existing")
    new = start_zero(client, rifle["id"], load_b["id"])
    set_state(conn, old["id"], "superseded")
    set_state(conn, mid["id"], "complete")
    html = page(client, f"/rifles/{rifle['id']}")
    assert zero_rows(html) == [
        (new["id"], "step1"),
        (mid["id"], "complete"),
        (old["id"], "superseded"),
    ]
    table = html.split("data-zeros")[1]
    for expected in ("Step 1: first group", "Complete", "Superseded", "Load Alpha", "Load Beta"):
        assert expected in table, expected
    assert "LOT-A1" in table
    for zero in (old, mid, new):
        assert f'href="/zeroing/{zero["id"]}"' in table
    row = re.search(rf'<tr data-zero-id="{old["id"]}".*?</tr>', table, re.S)[0]
    assert '<span class="badge warn">Superseded</span>' in row
    done = re.search(rf'<tr data-zero-id="{mid["id"]}".*?</tr>', table, re.S)[0]
    assert '<span class="badge">Complete</span>' in done
    assert "Load Alpha" in row and "LOT-A1" in row and "Superseded" in row
    stamp = conn.execute("SELECT created_at FROM zeros WHERE id = ?", (old["id"],)).fetchone()[0]
    assert stamp[:10] in row


def test_every_zero_state_has_a_label_and_existing_zeros_start_at_step_two(
    client: TestClient, conn
) -> None:
    rifle = make_rifle(client)
    load = make_load(client, rifle["id"])
    ids = {
        state: start_zero(client, rifle["id"], load["id"])["id"]
        for state in ("step1", "step2_initial", "step2_confirm", "step3_dot", "complete")
    }
    for state, zero_id in ids.items():
        set_state(conn, zero_id, state)
    html = page(client, f"/rifles/{rifle['id']}")
    labels = {
        "step1": "Step 1: first group",
        "step2_initial": "Step 2: initial group",
        "step2_confirm": "Step 2: confirm group",
        "step3_dot": "Step 3: dot",
        "complete": "Complete",
    }
    for state, zero_id in ids.items():
        row = re.search(rf'<tr data-zero-id="{zero_id}".*?</tr>', html, re.S)[0]
        assert labels[state] in row, state
    existing = start_zero(client, rifle["id"], load["id"], mode="existing")
    assert existing["state"] == "step2_initial"


def test_only_this_rifles_zeros_are_listed(client: TestClient) -> None:
    mine, other = make_rifle(client, name="Mine"), make_rifle(client, name="Other")
    zero = start_zero(client, mine["id"], make_load(client, mine["id"])["id"])
    theirs = start_zero(client, other["id"], make_load(client, other["id"])["id"])
    html = page(client, f"/rifles/{mine['id']}")
    assert [i for i, _ in zero_rows(html)] == [zero["id"]]
    assert f'href="/zeroing/{theirs["id"]}"' not in html


def test_no_correction_or_dialed_number_appears_on_the_rifle_page(client: TestClient, conn) -> None:
    rifle = make_rifle(client)
    load = make_load(client, rifle["id"])
    zero = start_zero(client, rifle["id"], load["id"])
    set_state(conn, zero["id"], "complete", dialed=True)
    for path in (f"/rifles/{rifle['id']}",):
        html = page(client, path)
        assert f'href="/zeroing/{zero["id"]}"' in html
        for leaked in (str(DIALED_UP), str(DIALED_RIGHT), "7.65", "2.34"):
            assert leaked not in html, (path, leaked)
        shown = html.replace("Elevation dialed (mil, optional)", "")
        assert not re.search(r"\bmils?\b|dialed|correction", shown, re.IGNORECASE), path


def test_a_zero_that_is_superseded_after_completion_says_so_on_the_rifle_page(
    client: TestClient, conn
) -> None:
    rifle = make_rifle(client)
    load = make_load(client, rifle["id"])
    zero = start_zero(client, rifle["id"], load["id"])
    set_state(conn, zero["id"], "complete")
    set_state(conn, zero["id"], "superseded")
    assert "Superseded" in page(client, f"/rifles/{rifle['id']}")


def test_zero_labels_and_lots_are_escaped_on_the_rifle_page(client: TestClient, conn) -> None:
    rifle = make_rifle(client, name="Escape Rifle")
    load = make_load(client, rifle["id"], label=XSS)
    zero = start_zero(client, rifle["id"], load["id"], lot_code=XSS)
    set_state(conn, zero["id"], "complete")
    for path in (f"/rifles/{rifle['id']}",):
        html = page(client, path)
        assert XSS not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html, path
        tags = re.findall(r"<script\b[^>]*>", html)
        assert all('src="/static/js/' in tag for tag in tags), path


def test_the_service_lists_a_rifles_zeros_newest_first_and_empty_for_unknown_ids(
    client: TestClient, conn
) -> None:
    rifle = make_rifle(client)
    load = make_load(client, rifle["id"], label="Svc load")
    first = start_zero(client, rifle["id"], load["id"], lot_code="L1")
    second = start_zero(client, rifle["id"], load["id"])
    listed = zeroing_service.list_zeros_for_rifle(conn, rifle["id"])
    assert [(z.zero.id, z.load_label, z.lot_code) for z in listed] == [
        (second["id"], "Svc load", None),
        (first["id"], "Svc load", "L1"),
    ]
    assert zeroing_service.list_zeros_for_rifle(conn, 999) == []
    assert zeroing_service.list_zeros_for_rifle(conn, 2**63) == []
    assert zeroing_service.list_zeros_for_rifle(conn, 0) == []


def test_starting_a_zero_is_denied_for_cross_origin_non_json_and_foreign_targets(
    client: TestClient,
) -> None:
    mine, other = make_rifle(client, name="Mine"), make_rifle(client, name="Other")
    load = make_load(client, mine["id"])
    foreign = make_load(client, other["id"])
    body = {"rifle_id": mine["id"], "load_id": load["id"], "mode": "new_setup"}
    evil = {"Origin": "http://evil.example"}
    assert client.post("/api/zeros", json=body, headers=evil).status_code == 403
    plain = client.post("/api/zeros", content=b"x", headers={"Content-Type": "text/plain"})
    assert plain.status_code == 415
    error(
        client.post("/api/zeros", json={**body, "load_id": foreign["id"]}),
        409,
        "zero_target_mismatch",
    )
    error(client.post("/api/zeros", json={**body, "load_id": 9999}), 404, "load_not_found")
    error(client.post("/api/zeros", json={**body, "rifle_id": 9999}), 404, "rifle_not_found")
    error(client.post("/api/zeros", json={**body, "mode": "fast"}), 422, "validation_error")
    error(client.post("/api/zeros", json={**body, "lot_code": ""}), 422, "validation_error")
    assert zero_rows(page(client, f"/rifles/{mine['id']}")) == []


def test_the_start_a_zero_script_posts_the_form_through_api_js_and_opens_the_zeroing_page() -> None:
    source = (WEB / "static" / "js" / "rifles.js").read_text()
    wire = source.split("function wireNewZero")[1].split("\nfunction ")[0]
    assert 'apiSend("POST", "/api/zeros", body)' in wire
    assert (
        "rifle_id: rifleId" in wire
        and "load_id: Number(" in wire
        and 'control(form, "mode")' in wire
    )
    assert 'textValue(form, "lot_code")' in wire and "body.lot_code = lot" in wire
    assert "window.location.assign(`/zeroing/${zero.id}`)" in wire
    assert "textContent" not in wire and "innerHTML" not in wire


def test_the_rifle_route_reads_zeros_only_through_the_service() -> None:
    source = (WEB / "routes" / "rifles.py").read_text()
    assert "zeroing as zeroing_service" in source
    assert "repo_zeroing" not in source and "rga.db" not in source
    assert "rga.db" not in (WEB / "routes" / "dashboard.py").read_text()


def _playwright_ready() -> bool:
    browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""))
    return browsers.is_dir() and any(browsers.glob("chromium*"))


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
def test_clicking_start_a_zero_opens_the_new_zeros_page_and_lists_it() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    with run_live_server() as server, sync_api.sync_playwright() as playwright:
        settings = Settings(data_dir=server.data_dir, port=server.port)
        db = connect(settings.db_path)
        try:
            rifle = seed_rifle(db)
            seed_load(db, rifle, label="Load One")
            second = seed_load(db, rifle, label="Load Two")
            with db:
                db.execute("UPDATE rifles SET zero_load_id = ? WHERE id = ?", (second.id, rifle.id))
        finally:
            db.close()
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            problems: list[str] = []
            page.on("pageerror", lambda error: problems.append(str(error)))
            page.goto(f"{server}/rifles/{rifle.id}")
            assert page.locator("#zero-start-load").input_value() == str(second.id)
            page.fill("#zero-start-lot", "LOT-E2E")
            page.get_by_role("button", name="Start a zero").click()
            page.wait_for_url(re.compile(r"/zeroing/\d+$"))
            zero_id = int(page.url.rsplit("/", 1)[1])
            assert page.locator("main h1").first.is_visible()
            page.goto(f"{server}/rifles/{rifle.id}")
            row = page.locator(f'tr[data-zero-id="{zero_id}"]')
            text = row.inner_text()
            assert "Load Two" in text and "LOT-E2E" in text and "Step 1" in text
            row.get_by_role("link", name="Open").click()
            page.wait_for_url(re.compile(rf"/zeroing/{zero_id}$"))
        finally:
            browser.close()
        assert problems == []


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
def test_the_handload_form_in_chromium_saves_bullet_primer_brass_and_coal() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    with run_live_server() as server, sync_api.sync_playwright() as playwright:
        settings = Settings(data_dir=server.data_dir, port=server.port)
        db = connect(settings.db_path)
        try:
            rifle = seed_rifle(db)
        finally:
            db.close()
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            problems: list[str] = []
            page.on("pageerror", lambda error: problems.append(str(error)))
            page.goto(f"{server}/rifles/{rifle.id}")
            form = page.locator('form[data-form="new-load"]')
            form.locator("#kind").select_option("handload")
            assert form.locator('[data-kind-fields="handload"]').is_visible()
            assert not form.locator('[data-kind-fields="factory"]').is_visible()
            for banned in ("powder", "charge_gr"):
                assert form.locator(f'[name="{banned}"]').count() == 0
            form.locator("#handload_bullet_weight_gr").fill("116")
            form.locator("#bullet").fill("TMK")
            form.locator("#primer").fill("CCI BR-2")
            form.locator("#brass").fill("Lapua")
            form.locator("#coal_in").fill("2.800")
            form.get_by_role("button", name="Add load").click()
            card = page.locator("article[data-load-id]").first
            card.wait_for()
            assert "116 gr TMK" in card.locator("[data-bullet]").inner_text()
            text = card.inner_text()
            assert "CCI BR-2" in text and "Lapua" in text and "2.800 in" in text
            assert "116 gr TMK" in card.locator("h3").first.inner_text(), (
                "the label defaults to the bullet"
            )

            edit = card.locator('form[data-form="edit-load"]')
            card.locator("summary").click()
            edit.locator('[name="coal_in"]').fill("3.010")
            edit.locator('[name="bullet_weight_gr"]').fill("120")
            edit.get_by_role("button", name="Save load").click()
            page.locator("article[data-load-id] [data-bullet]", has_text="120 gr TMK").wait_for()
            assert "3.010 in" in page.locator("article[data-load-id]").first.inner_text()
        finally:
            browser.close()
        assert problems == []
        db = connect(settings.db_path)
        try:
            row = db.execute("SELECT * FROM loads").fetchone()
        finally:
            db.close()
        assert (row["bullet_weight_gr"], row["bullet"], row["brass"], row["coal_in"]) == (
            120.0,
            "TMK",
            "Lapua",
            3.01,
        )
        assert row["powder"] is None and row["charge_gr"] is None


# ------------------------------------------------------------ B-82 bullet-diameter dropdown
DIAMETER_VALUES = ["0.224", "0.243", "0.257", "0.264", "0.277", "0.284", "0.308", "0.338"]


def _diameter_options(html: str) -> list[tuple[str, bool, str]]:
    select = html.split('<select id="bullet_diameter_in"', 1)[1].split("</select>", 1)[0]
    return [
        (value, " selected" in attrs, label.strip())
        for value, attrs, label in re.findall(
            r'<option value="([^"]*)"([^>]*)>([^<]*)</option>', select
        )
    ]


def test_the_new_rifle_form_offers_exactly_the_eight_diameters(client: TestClient) -> None:
    options = _diameter_options(client.get("/rifles").text)
    assert [value for value, _, _ in options] == ["", *DIAMETER_VALUES]
    assert [label for _, _, label in options[1:]] == [f"{v} in" for v in DIAMETER_VALUES]
    assert not any(selected for _, selected, _ in options)


def test_the_edit_form_selects_the_rifles_listed_diameter(client: TestClient, conn) -> None:
    rifle = seed_rifle(conn, bullet_diameter_in=0.308, cartridge=".308 Win")
    options = _diameter_options(client.get(f"/rifles/{rifle.id}").text)
    assert [value for value, _, _ in options] == DIAMETER_VALUES
    assert [value for value, selected, _ in options if selected] == ["0.308"]


def test_the_edit_form_keeps_an_unlisted_diameter_as_the_current_option(
    client: TestClient, conn
) -> None:
    rifle = seed_rifle(conn, bullet_diameter_in=0.172, cartridge=".17 HMR")
    options = _diameter_options(client.get(f"/rifles/{rifle.id}").text)
    assert [value for value, _, _ in options][:8] == DIAMETER_VALUES
    assert options[8:] == [("0.172", True, "0.172 in (current)")]


def test_the_api_still_accepts_an_unlisted_diameter(client: TestClient) -> None:
    body = {"name": "Rimfire", "cartridge": ".17 HMR", "bullet_diameter_in": 0.172}
    response = client.post("/api/rifles", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["bullet_diameter_in"] == pytest.approx(0.172)


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
def test_the_diameter_dropdown_in_chromium_prefills_saves_and_keeps_an_unlisted_value() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    with run_live_server() as server, sync_api.sync_playwright() as playwright:
        settings = Settings(data_dir=server.data_dir, port=server.port)
        db = connect(settings.db_path)
        try:
            rimfire = seed_rifle(db, name="Rimfire", cartridge=".17 HMR", bullet_diameter_in=0.172)
        finally:
            db.close()
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            problems: list[str] = []
            page.on("pageerror", lambda error: problems.append(str(error)))
            page.goto(f"{server}/rifles")
            form = page.locator('form[data-form="new-rifle"]')
            form.locator("#name").fill("T3x")
            page.wait_for_function("document.querySelectorAll('#cartridge-list option').length > 0")
            form.locator("#cartridge").fill(".308 Win")
            form.locator("#cartridge").dispatch_event("change")
            assert form.locator("#bullet_diameter_in").input_value() == "0.308"
            form.locator("#cartridge").fill(".223 Rem")
            form.locator("#cartridge").dispatch_event("change")
            assert form.locator("#bullet_diameter_in").input_value() == "0.224"
            form.get_by_role("button", name="Add rifle").click()
            page.wait_for_url(re.compile(r"/rifles/\d+$"))
            new_id = int(page.url.rsplit("/", 1)[1])
            saved = page.request.get(f"{server}/api/rifles/{new_id}").json()
            assert saved["bullet_diameter_in"] == pytest.approx(0.224)

            page.goto(f"{server}/rifles/{rimfire.id}")
            edit = page.locator('form[data-form="edit-rifle"]')
            assert edit.locator("#bullet_diameter_in").input_value() == "0.172"
            edit.locator("#r-scope").fill("Scope A")
            edit.get_by_role("button", name="Save rifle").click()
            page.wait_for_load_state("networkidle")
            kept = page.request.get(f"{server}/api/rifles/{rimfire.id}").json()
            assert kept["bullet_diameter_in"] == pytest.approx(0.172)
            assert kept["scope"] == "Scope A"
        finally:
            browser.close()
        assert problems == []
