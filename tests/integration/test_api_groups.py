"""R6 (B-66): groups carry their own context. Group create, patch and defaults, named suppressors,
masking, error mapping and the route table, against the real app and a temp database."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rga.config import Settings
from rga.db import repo_groups
from rga.db.connection import connect
from rga.db.repo_groups import GroupLinkMismatch
from rga.db.repo_rifles import LoadNotInRifle
from rga.db.repo_zeroing import GroupNotLinkable, ZeroTargetMismatch
from rga.domain.types import Offset
from rga.domain.units import in_to_mil
from rga.services import group_records as service
from rga.services import groups as group_service
from rga.web import routes
from rga.web.app import create_app
from rga.web.deps import get_db
from rga.web.schemas import GroupOut
from tests.support.seed import (
    seed_context,
    seed_group,
    seed_load,
    seed_photo,
    seed_rifle,
)

ORIGIN = "http://127.0.0.1:8765"
POSITIONS = ["bench", "prone_off_pack", "kneeling_sitting", "standing_field"]
TEN = [Offset(0.1 * i - 0.5, 0.05 * i - 0.2) for i in range(10)]
FIVE = TEN[:5]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, base_url=ORIGIN) as test_client:
        yield test_client


@pytest.fixture
def tolerant(app) -> Iterator[TestClient]:
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def conn(settings: Settings, client: TestClient) -> Iterator[sqlite3.Connection]:
    connection = connect(settings.db_path)
    try:
        yield connection
    finally:
        connection.close()


@dataclass
class World:
    conn: sqlite3.Connection
    rifle: object
    load: object
    other_rifle: object
    other_load: object

    def ctx(self, rifle=None, **kw):
        return seed_context(rifle or self.rifle, **kw)

    def group(self, *, offsets=FIVE, finalize=False, load=None, **kw):
        context = kw.pop("context", None) or self.ctx()
        return seed_group(
            self.conn,
            load=load or self.load,
            context=context,
            offsets=offsets,
            finalize=finalize,
            **kw,
        )


@pytest.fixture
def world(conn) -> World:
    rifle = seed_rifle(conn, name="Mine")
    other = seed_rifle(conn, name="Other")
    return World(conn, rifle, seed_load(conn, rifle), other, seed_load(conn, other, label="Theirs"))


def group_body(world: World, **overrides) -> dict:
    body = {
        "load_id": world.load.id,
        "lot_code": "LOT-1",
        "shots_expected": 5,
        "shot_date": "2026-09-30",
        "distance_yd": 100,
        "position": "bench",
        "suppressor": False,
    }
    return {**body, **overrides}


def stored_offsets(conn, group_id: int) -> list[tuple]:
    return [(s.dx_in, s.dy_in, s.dx_mil, s.dy_mil) for s in repo_groups.list_shots(conn, group_id)]


def set_taken_at(conn, photo_id: int, taken_at: str | None, offset: str | None = None) -> None:
    with conn:
        conn.execute(
            "UPDATE photos SET taken_at = ?, taken_at_offset = ? WHERE id = ?",
            (taken_at, offset, photo_id),
        )


def _zero(conn: sqlite3.Connection, rifle, load, lot_id: int | None = None) -> int:
    with conn:
        cursor = conn.execute(
            "INSERT INTO zeros (rifle_id, load_id, lot_id, mode, state, created_at, updated_at) "
            "VALUES (?, ?, ?, 'new_setup', 'step1', '2026-09-30T00:00:00.000000Z', "
            "'2026-09-30T00:00:00.000000Z')",
            (rifle.id, load.id, lot_id),
        )
    return int(cursor.lastrowid)


def link_to_zero(conn, group_id: int, zero_id: int, purpose: str = "zero_check") -> None:
    with conn:
        conn.execute(
            "UPDATE groups SET zero_id = ?, purpose = ? WHERE id = ?", (zero_id, purpose, group_id)
        )


# ------------------------------------------------------------------------------ create


class TestGroupCreate:
    def test_create_returns_201_with_the_whole_context_and_round_trips(self, client, world):
        body = group_body(
            world,
            shot_date="2026-09-29",
            shot_time="07:45",
            utc_offset="-06:00",
            distance_yd=300,
            position="prone_off_pack",
            suppressor=True,
            temperature_f=61.5,
            density_altitude_ft=2300.0,
            wind_mph=8.5,
            wind_clock=4,
            elevation_dialed_mil=1.3,
            notes="windy morning <b>x</b>",
        )

        created = client.post("/api/groups", json=body)
        fetched = client.get(f"/api/groups/{created.json()['id']}")

        out = created.json()
        assert created.status_code == 201
        assert out["shot_time"] == "07:45:00"
        for key, value in body.items():
            if key not in ("shot_time", "load_id", "lot_code"):
                assert out[key] == value, key
        assert (out["load_id"], out["rifle_id"], out["lot_code"]) == (
            world.load.id,
            world.rifle.id,
            "LOT-1",
        )
        assert (out["status"], out["purpose"], out["zero_id"], out["aim"]) == (
            "draft",
            "general",
            None,
            None,
        )
        assert (out["shot_at_source"], out["weather_skipped"], out["masked"]) == (
            "manual",
            False,
            False,
        )
        assert out["weather_prompt"] is True and out["new_lot"] is False
        assert "session_id" not in out
        assert re.fullmatch(r"[0-9a-f-]{36}", out["public_id"])
        assert fetched.status_code == 200
        detail = fetched.json()
        for key, value in out.items():
            assert detail[key] == value, key
        assert detail["legacy_session_notes"] == ""

    def test_the_rifle_comes_from_the_load(self, client, world):
        spare = seed_load(world.conn, world.other_rifle, label="Spare")
        mine = client.post("/api/groups", json=group_body(world)).json()
        theirs = client.post("/api/groups", json=group_body(world, load_id=spare.id)).json()

        assert spare.id not in (world.rifle.id, world.other_rifle.id)
        assert (mine["rifle_id"], theirs["rifle_id"]) == (world.rifle.id, world.other_rifle.id)
        assert client.get(f"/api/groups/{theirs['id']}").json()["rifle_id"] == world.other_rifle.id

    def test_notes_carried_over_from_a_former_session_are_read_only_detail(self, client, world):
        group = world.group(context=world.ctx(notes="old session note <b>x</b>"))

        detail = client.get(f"/api/groups/{group.id}").json()
        listed = next(g for g in client.get("/api/groups").json() if g["id"] == group.id)
        patched = client.patch(f"/api/groups/{group.id}", json={"notes": "mine"}).json()

        assert detail["legacy_session_notes"] == "old session note <b>x</b>"
        assert "legacy_session_notes" not in listed and "legacy_session_notes" not in patched
        again = client.get(f"/api/groups/{group.id}").json()
        assert (again["notes"], again["legacy_session_notes"]) == (
            "mine",
            "old session note <b>x</b>",
        )
        bad = client.patch(f"/api/groups/{group.id}", json={"legacy_session_notes": "x"})
        assert bad.status_code == 422

    @pytest.mark.parametrize("position", POSITIONS)
    def test_every_position_is_accepted(self, client, world, position):
        response = client.post("/api/groups", json=group_body(world, position=position))

        assert response.status_code == 201 and response.json()["position"] == position

    @pytest.mark.parametrize("position", ["sandbags", "Bench", "", "prone", None, 3])
    def test_other_positions_are_refused(self, client, world, position):
        response = client.post("/api/groups", json=group_body(world, position=position))

        assert response.status_code == 422 and response.json()["code"] == "validation_error"
        assert repo_groups.list_groups(world.conn) == []

    @pytest.mark.parametrize("missing", ["distance_yd", "position", "suppressor", "lot_code"])
    def test_a_missing_required_field_is_422_and_creates_nothing(self, client, world, missing):
        body = {k: v for k, v in group_body(world).items() if k != missing}

        response = client.post("/api/groups", json=body)

        assert response.status_code == 422 and response.json()["code"] == "validation_error"
        assert repo_groups.list_groups(world.conn) == []
        assert client.get(f"/api/loads/{world.load.id}/lots").json() == []

    @pytest.mark.parametrize(
        ("distance", "expected"), [(1, False), (100, False), (199, False), (200, True), (500, True)]
    )
    def test_weather_prompt_boundary_on_create_get_list_and_patch(
        self, client, world, distance, expected
    ):
        created = client.post("/api/groups", json=group_body(world, distance_yd=distance)).json()
        group_id = created["id"]

        assert created["weather_prompt"] is expected
        assert client.get(f"/api/groups/{group_id}").json()["weather_prompt"] is expected
        listed = client.get("/api/groups").json()
        assert [g["weather_prompt"] for g in listed if g["id"] == group_id] == [expected]
        patched = client.patch(f"/api/groups/{group_id}", json={"notes": "x"}).json()
        assert patched["weather_prompt"] is expected

    def test_weather_values_are_optional_at_any_distance_for_the_api(self, client, world):
        response = client.post("/api/groups", json=group_body(world, distance_yd=600))

        assert response.status_code == 201
        assert response.json()["temperature_f"] is None
        assert response.json()["weather_prompt"] is True

    def test_skipping_weather_is_recorded(self, client, world):
        created = client.post(
            "/api/groups", json=group_body(world, distance_yd=300, weather_skipped=True)
        ).json()

        assert created["weather_skipped"] is True and created["temperature_f"] is None

    @pytest.mark.parametrize(
        "values",
        [
            {"temperature_f": 60},
            {"density_altitude_ft": 3000},
            {"wind_mph": 4},
            {"wind_clock": 3},
        ],
    )
    def test_weather_cannot_be_both_entered_and_skipped_on_create(self, client, world, values):
        refused = client.post(
            "/api/groups", json=group_body(world, distance_yd=300, weather_skipped=True, **values)
        )

        assert refused.status_code == 422 and refused.json()["code"] == "invalid_weather"
        assert repo_groups.list_groups(world.conn) == []
        assert client.get(f"/api/loads/{world.load.id}/lots").json() == []

    @pytest.mark.parametrize(
        ("given", "stored"), [("09:05", "09:05:00"), ("23:59:59", "23:59:59"), (None, None)]
    )
    def test_the_time_is_stored_as_hh_mm_ss_or_left_out(self, client, world, given, stored):
        body = group_body(world) if given is None else group_body(world, shot_time=given)

        out = client.post("/api/groups", json=body).json()

        assert out["shot_time"] == stored
        assert repo_groups.get_group(world.conn, out["id"]).shot_time == stored

    @pytest.mark.parametrize(
        "bad",
        [
            {"shot_time": "24:00"},
            {"shot_time": "noon"},
            {"shot_time": 5},
            {"shot_date": "30/09/2026"},
            {"utc_offset": "UTC"},
            {"utc_offset": "-6:00"},
            {"distance_yd": 0},
            {"distance_yd": 5.5},
            {"wind_clock": 13},
            {"wind_clock": 0},
            {"temperature_f": "warm"},
            {"suppressor": "maybe"},
            {"purpose": "warmup"},
            {"shots_expected": 0},
            {"shots_expected": 3.5},
            {"lot_code": ""},
            {"lot_code": "x" * 65},
            {"load_id": 0},
            {"elevation_dialed_mil": "high"},
            {"session_id": 1},
            {"surprise": True},
        ],
    )
    def test_invalid_group_bodies_are_422(self, client, world, bad):
        response = client.post("/api/groups", json=group_body(world, **bad))

        assert response.status_code == 422 and response.json()["code"] == "validation_error"
        assert repo_groups.list_groups(world.conn) == []

    def test_a_time_without_a_date_is_422(self, client, world):
        body = {k: v for k, v in group_body(world, shot_time="10:15").items() if k != "shot_date"}

        response = client.post("/api/groups", json=body)

        assert response.status_code == 422 and response.json()["code"] == "validation_error"

    def test_the_lot_is_created_once_per_code_and_the_code_is_trimmed(self, client, world):
        first = client.post("/api/groups", json=group_body(world, lot_code="A1")).json()
        again = client.post("/api/groups", json=group_body(world, lot_code="  A1 ")).json()
        other = client.post("/api/groups", json=group_body(world, lot_code="B2")).json()

        assert first["lot_id"] == again["lot_id"] != other["lot_id"]
        assert again["lot_code"] == "A1"
        lots = client.get(f"/api/loads/{world.load.id}/lots").json()
        assert sorted(lot["lot_code"] for lot in lots) == ["A1", "B2"]

    @pytest.mark.parametrize("code", ["   ", "\t"])
    def test_a_blank_lot_code_is_422_and_creates_nothing(self, client, world, code):
        response = client.post("/api/groups", json=group_body(world, lot_code=code))

        assert response.status_code == 422 and response.json()["code"] == "validation_error"
        assert repo_groups.list_groups(world.conn) == []
        assert client.get(f"/api/loads/{world.load.id}/lots").json() == []

    def test_the_service_refuses_a_blank_lot_code_even_without_the_schema(self, world):
        with pytest.raises(service.SessionError) as caught:
            service.create_group(
                world.conn,
                load_id=world.load.id,
                lot_code="  ",
                shots_expected=5,
                distance_yd=100,
                position="bench",
                suppressor=False,
                shot_date="2026-09-30",
            )

        assert caught.value.code == "invalid_lot_code"
        assert repo_groups.list_groups(world.conn) == []

    def test_new_lot_flag_is_true_for_the_first_group_of_a_second_lot(self, client, world):
        world.group(offsets=TEN, finalize=True, lot_code="LOT-A")

        second_lot = client.post("/api/groups", json=group_body(world, lot_code="LOT-B")).json()
        same_lot = client.post("/api/groups", json=group_body(world, lot_code="LOT-A")).json()

        assert second_lot["new_lot"] is True and same_lot["new_lot"] is False
        listed = {g["id"]: g["new_lot"] for g in client.get("/api/groups").json()}
        assert listed[second_lot["id"]] is True and listed[same_lot["id"]] is False

    def test_new_lot_flag_is_false_for_the_first_group_of_a_first_lot(self, client, world):
        assert client.post("/api/groups", json=group_body(world)).json()["new_lot"] is False

    def test_new_lot_flag_ignores_finalized_groups_of_other_loads(self, client, world):
        sibling = seed_load(world.conn, world.rifle, label="Second load")
        world.group(offsets=TEN, finalize=True, load=sibling, lot_code="LOT-A")

        response = client.post("/api/groups", json=group_body(world, lot_code="LOT-Z"))

        assert response.json()["new_lot"] is False

    def test_a_group_can_link_to_a_photo_that_other_groups_share(self, client, world):
        photo = seed_photo(world.conn)

        left = client.post("/api/groups", json=group_body(world, photo_id=photo.id)).json()
        right = client.post("/api/groups", json=group_body(world, photo_id=photo.id)).json()

        assert left["photo_id"] == right["photo_id"] == photo.id and left["id"] != right["id"]

    @pytest.mark.parametrize("purpose", ["zero_step1", "zero_step2", "zero_confirm", "zero_check"])
    def test_zeroing_purposes_cannot_be_set_on_create(self, client, world, purpose):
        response = client.post("/api/groups", json=group_body(world, purpose=purpose))

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_purpose"
        assert repo_groups.list_groups(world.conn) == []

    def test_the_general_purpose_is_the_only_one_accepted_on_create(self, client, world):
        explicit = client.post("/api/groups", json=group_body(world, purpose="general"))
        default = client.post("/api/groups", json=group_body(world))

        assert explicit.status_code == default.status_code == 201
        assert explicit.json()["purpose"] == default.json()["purpose"] == "general"
        assert explicit.json()["zero_id"] is None

    def test_a_zero_id_is_refused_on_create_whatever_it_points_at(self, client, world):
        sibling = seed_load(world.conn, world.rifle, label="Second load")
        ids = [
            999,
            _zero(world.conn, world.other_rifle, world.other_load),
            _zero(world.conn, world.rifle, sibling),
            _zero(world.conn, world.rifle, world.load),
        ]

        for zero_id in ids:
            response = client.post("/api/groups", json=group_body(world, zero_id=zero_id))
            assert response.status_code == 422
            assert response.json()["code"] == "invalid_purpose"
        assert repo_groups.list_groups(world.conn) == []

    def test_missing_parents_are_404_and_create_nothing(self, client, world):
        no_load = client.post("/api/groups", json=group_body(world, load_id=999))
        no_photo = client.post("/api/groups", json=group_body(world, photo_id=999))

        assert (no_load.status_code, no_load.json()["code"]) == (404, "load_not_found")
        assert (no_photo.status_code, no_photo.json()["code"]) == (404, "photo_not_found")
        assert repo_groups.list_groups(world.conn) == []

    def test_notes_round_trip_verbatim(self, client, world):
        notes = "<script>alert(1)</script> & 100% \"quoted\" 'x'\nsecond line"

        created = client.post("/api/groups", json=group_body(world, notes=notes)).json()

        assert created["notes"] == notes
        assert client.get(f"/api/groups/{created['id']}").json()["notes"] == notes


class TestShotDateFromThePhoto:
    def test_a_missing_date_is_taken_from_the_photo_and_marked_as_photo(self, client, world):
        photo = seed_photo(world.conn)
        set_taken_at(world.conn, photo.id, "2026-09-12T08:15:22", "-06:00")
        body = {k: v for k, v in group_body(world, photo_id=photo.id).items() if k != "shot_date"}

        out = client.post("/api/groups", json=body).json()

        assert (out["shot_date"], out["shot_time"], out["utc_offset"]) == (
            "2026-09-12",
            "08:15:22",
            "-06:00",
        )
        assert out["shot_at_source"] == "photo"

    def test_no_date_and_a_photo_without_one_is_422_invalid_shot_date(self, client, world):
        photo = seed_photo(world.conn)
        body = {k: v for k, v in group_body(world, photo_id=photo.id).items() if k != "shot_date"}
        without_photo = {k: v for k, v in group_body(world).items() if k != "shot_date"}

        for payload in (body, without_photo):
            response = client.post("/api/groups", json=payload)
            assert (response.status_code, response.json()["code"]) == (422, "invalid_shot_date")
        assert repo_groups.list_groups(world.conn) == []
        assert client.get(f"/api/loads/{world.load.id}/lots").json() == []

    def test_a_date_and_time_equal_to_the_photo_count_as_the_photo_and_others_as_manual(
        self, client, world
    ):
        photo = seed_photo(world.conn)
        set_taken_at(world.conn, photo.id, "2026-09-12T08:15:22", "-06:00")

        same = client.post(
            "/api/groups",
            json=group_body(world, photo_id=photo.id, shot_date="2026-09-12", shot_time="08:15:22"),
        ).json()
        other_time = client.post(
            "/api/groups",
            json=group_body(world, photo_id=photo.id, shot_date="2026-09-12", shot_time="09:00:00"),
        ).json()
        other_day = client.post(
            "/api/groups", json=group_body(world, photo_id=photo.id, shot_date="2026-09-13")
        ).json()

        assert same["shot_at_source"] == "photo" and same["utc_offset"] == "-06:00"
        assert other_time["shot_at_source"] == "manual" and other_time["utc_offset"] is None
        assert other_day["shot_at_source"] == "manual" and other_day["shot_time"] is None

    def test_no_time_zone_conversion_is_applied_to_the_date(self, client, world):
        photo = seed_photo(world.conn)
        set_taken_at(world.conn, photo.id, "2026-09-12T23:59:30", "+09:00")
        body = {k: v for k, v in group_body(world, photo_id=photo.id).items() if k != "shot_date"}

        out = client.post("/api/groups", json=body).json()

        assert (out["shot_date"], out["shot_time"]) == ("2026-09-12", "23:59:30")

    def test_the_photo_date_pre_fills_the_check_group_too(self, client, world):
        photo = seed_photo(world.conn)
        set_taken_at(world.conn, photo.id, "2026-09-12T08:15:22")

        out = client.post("/api/groups", json={**group_body(world, photo_id=photo.id)}).json()

        assert out["shot_date"] == "2026-09-30"


# ------------------------------------------------------------------------------ defaults


class TestGroupDefaults:
    def get(self, client, rifle_id: int):
        return client.get(f"/api/rifles/{rifle_id}/group-defaults")

    def test_a_rifle_without_groups_and_an_empty_database_gives_all_nulls(self, client, world):
        response = self.get(client, world.rifle.id)

        assert response.status_code == 200
        assert response.json() == {
            "source_group_id": None,
            "load_id": None,
            "lot_code": None,
            "shots_expected": None,
            "shot_date": None,
            "distance_yd": None,
            "position": None,
            "suppressor": None,
            "suppressor_id": None,
            "temperature_f": None,
            "density_altitude_ft": None,
            "wind_mph": None,
            "wind_clock": None,
            "weather_skipped": None,
        }

    def test_the_latest_group_of_the_rifle_supplies_every_default(self, client, world):
        omega = client.post("/api/suppressors", json={"name": "Omega"}).json()
        client.post("/api/groups", json=group_body(world, lot_code="OLD", distance_yd=100))
        newest = client.post(
            "/api/groups",
            json=group_body(
                world,
                lot_code="NEW-7",
                shots_expected=8,
                shot_date="2026-10-01",
                distance_yd=300,
                position="prone_off_pack",
                suppressor=True,
                suppressor_id=omega["id"],
                temperature_f=48.5,
                density_altitude_ft=3100.0,
                wind_mph=6.0,
                wind_clock=9,
            ),
        ).json()

        out = self.get(client, world.rifle.id).json()

        assert out == {
            "source_group_id": newest["id"],
            "load_id": world.load.id,
            "lot_code": "NEW-7",
            "shots_expected": 8,
            "shot_date": "2026-10-01",
            "distance_yd": 300,
            "position": "prone_off_pack",
            "suppressor": True,
            "suppressor_id": omega["id"],
            "temperature_f": 48.5,
            "density_altitude_ft": 3100.0,
            "wind_mph": 6.0,
            "wind_clock": 9,
            "weather_skipped": False,
        }

    def test_a_skipped_weather_is_reported_as_skipped(self, client, world):
        client.post("/api/groups", json=group_body(world, distance_yd=300, weather_skipped=True))

        out = self.get(client, world.rifle.id).json()

        assert out["weather_skipped"] is True and out["temperature_f"] is None

    def test_the_highest_id_wins_not_the_latest_date(self, client, world):
        client.post("/api/groups", json=group_body(world, shot_date="2026-10-05", lot_code="A"))
        later_entered = client.post(
            "/api/groups", json=group_body(world, shot_date="2026-09-01", lot_code="B")
        ).json()

        out = self.get(client, world.rifle.id).json()

        assert (out["source_group_id"], out["lot_code"]) == (later_entered["id"], "B")

    def test_a_rifle_without_groups_borrows_only_the_context_of_the_latest_group_anywhere(
        self, client, world
    ):
        omega = client.post("/api/suppressors", json={"name": "Omega"}).json()
        client.post(
            "/api/groups",
            json=group_body(
                world,
                load_id=world.other_load.id,
                lot_code="THEIRS",
                shots_expected=9,
                distance_yd=400,
                position="standing_field",
                suppressor=True,
                suppressor_id=omega["id"],
                temperature_f=50.0,
            ),
        )

        out = self.get(client, world.rifle.id).json()

        assert (out["distance_yd"], out["position"], out["suppressor"]) == (
            400,
            "standing_field",
            True,
        )
        assert out["suppressor_id"] == omega["id"]
        for key in (
            "source_group_id",
            "load_id",
            "lot_code",
            "shots_expected",
            "shot_date",
            "temperature_f",
            "weather_skipped",
        ):
            assert out[key] is None, key

    def test_another_rifles_group_does_not_override_this_rifles_own(self, client, world):
        mine = client.post("/api/groups", json=group_body(world, distance_yd=100)).json()
        client.post(
            "/api/groups",
            json=group_body(world, load_id=world.other_load.id, distance_yd=500),
        )

        out = self.get(client, world.rifle.id).json()

        assert (out["source_group_id"], out["distance_yd"]) == (mine["id"], 100)

    def test_the_defaults_are_those_of_the_requested_rifle(self, client, world):
        spare = seed_load(world.conn, world.other_rifle, label="Spare")
        client.post("/api/groups", json=group_body(world, distance_yd=100, lot_code="MINE"))
        theirs = client.post(
            "/api/groups",
            json=group_body(world, load_id=spare.id, distance_yd=500, lot_code="THEIRS"),
        ).json()

        out = self.get(client, world.other_rifle.id).json()

        assert world.other_rifle.id != 1
        assert (out["source_group_id"], out["lot_code"], out["distance_yd"]) == (
            theirs["id"],
            "THEIRS",
            500,
        )
        assert out["load_id"] == spare.id

    def test_an_unknown_rifle_is_404(self, client):
        response = self.get(client, 999)

        assert (response.status_code, response.json()["code"]) == (404, "rifle_not_found")

    @pytest.mark.parametrize("bad", ["0", "-1", "abc", str(2**63)])
    def test_a_malformed_or_out_of_range_rifle_id_is_a_4xx_error_never_500(self, tolerant, bad):
        response = tolerant.get(f"/api/rifles/{bad}/group-defaults")

        assert response.status_code in (404, 422)
        assert set(response.json()) == {"detail", "code"}


# ------------------------------------------------------------------------------ list


class TestGroupList:
    def test_the_list_filters_by_rifle_load_and_status(self, client, world):
        mine = world.group(offsets=TEN, finalize=True)
        draft = world.group(offsets=FIVE)
        theirs = world.group(
            offsets=TEN, finalize=True, load=world.other_load, context=world.ctx(world.other_rifle)
        )

        everything = {g["id"] for g in client.get("/api/groups").json()}
        by_rifle = {g["id"] for g in client.get(f"/api/groups?rifle_id={world.rifle.id}").json()}
        by_load = {g["id"] for g in client.get(f"/api/groups?load_id={world.other_load.id}").json()}
        final = {g["id"] for g in client.get("/api/groups?status=final").json()}

        assert everything == {mine.id, draft.id, theirs.id}
        assert by_rifle == {mine.id, draft.id} and by_load == {theirs.id}
        assert final == {mine.id, theirs.id}

    def test_every_list_item_carries_its_context(self, client, world):
        group = world.group(
            offsets=TEN,
            finalize=True,
            context=world.ctx(distance_yd=300, position="standing_field", date="2026-09-20"),
        )

        item = next(g for g in client.get("/api/groups").json() if g["id"] == group.id)

        assert (item["shot_date"], item["distance_yd"], item["position"]) == (
            "2026-09-20",
            300,
            "standing_field",
        )
        assert (item["rifle_id"], item["suppressor"], item["weather_prompt"]) == (
            world.rifle.id,
            False,
            True,
        )

    @pytest.mark.parametrize(
        "query", ["session_id=1", "color=red", "rifle_id=0", "status=archived"]
    )
    def test_unknown_or_malformed_filters_are_422(self, client, query):
        assert client.get(f"/api/groups?{query}").status_code == 422

    def test_the_session_routes_and_page_no_longer_exist(self, client, world):
        for method, path in (
            ("GET", "/api/sessions"),
            ("POST", "/api/sessions"),
            ("GET", "/api/sessions/1"),
            ("PATCH", "/api/sessions/1"),
            ("POST", "/api/sessions/1/groups"),
            ("GET", "/sessions/1"),
        ):
            response = client.request(method, path, json={} if method != "GET" else None)
            assert response.status_code in (404, 405), (method, path)


# ------------------------------------------------------------------------------ patch


class TestGroupPatch:
    def test_fields_round_trip_and_unrelated_ones_stay(self, client, world):
        group = world.group(notes="a")

        patched = client.patch(
            f"/api/groups/{group.id}",
            json={"shots_expected": 7, "elevation_dialed_mil": 2.4, "notes": "windy <i>x</i>"},
        )

        body = patched.json()
        assert patched.status_code == 200
        assert (body["shots_expected"], body["elevation_dialed_mil"]) == (7, 2.4)
        assert body["notes"] == "windy <i>x</i>"
        assert (body["status"], body["purpose"], body["distance_yd"]) == ("draft", "general", 100)
        cleared = client.patch(f"/api/groups/{group.id}", json={"elevation_dialed_mil": None})
        assert cleared.json()["elevation_dialed_mil"] is None
        assert cleared.json()["shots_expected"] == 7

    def test_the_context_fields_can_be_changed_and_only_those_given(self, client, world):
        group = world.group(context=world.ctx(date="2026-09-01", position="bench"))

        patched = client.patch(
            f"/api/groups/{group.id}",
            json={
                "shot_date": "2026-09-02",
                "shot_time": "06:30",
                "utc_offset": "-07:00",
                "position": "prone_off_pack",
                "suppressor": True,
                "temperature_f": 41.0,
                "wind_clock": 3,
            },
        ).json()

        assert (patched["shot_date"], patched["shot_time"], patched["utc_offset"]) == (
            "2026-09-02",
            "06:30:00",
            "-07:00",
        )
        assert (patched["position"], patched["suppressor"]) == ("prone_off_pack", True)
        assert (patched["temperature_f"], patched["wind_clock"]) == (41.0, 3)
        assert (patched["distance_yd"], patched["notes"], patched["shots_expected"]) == (
            100,
            "",
            5,
        )

    def test_nullable_context_fields_can_be_cleared(self, client, world):
        group = world.group(
            context=world.ctx(
                time="08:00:00", utc_offset="-06:00", temperature_f=50.0, wind_clock=3
            )
        )

        patched = client.patch(
            f"/api/groups/{group.id}",
            json={"shot_time": None, "utc_offset": None, "temperature_f": None, "wind_clock": None},
        ).json()

        assert (patched["shot_time"], patched["utc_offset"]) == (None, None)
        assert (patched["temperature_f"], patched["wind_clock"]) == (None, None)

    @pytest.mark.parametrize("field", ["shot_date", "distance_yd", "position", "suppressor"])
    def test_required_context_fields_cannot_be_nulled(self, client, world, field):
        group = world.group()

        response = client.patch(f"/api/groups/{group.id}", json={field: None})

        assert response.status_code == 422 and response.json()["code"] == "validation_error"

    def test_a_new_date_or_time_makes_the_date_source_manual(self, client, world):
        photo = seed_photo(world.conn)
        set_taken_at(world.conn, photo.id, "2026-09-12T08:15:22", "-06:00")
        body = {k: v for k, v in group_body(world, photo_id=photo.id).items() if k != "shot_date"}
        created = client.post("/api/groups", json=body).json()
        assert created["shot_at_source"] == "photo"

        notes_only = client.patch(f"/api/groups/{created['id']}", json={"notes": "x"}).json()
        typed = client.patch(f"/api/groups/{created['id']}", json={"shot_time": "09:00"}).json()

        assert notes_only["shot_at_source"] == "photo"
        assert typed["shot_at_source"] == "manual"

    @pytest.mark.parametrize(("distance", "expected"), [(199, False), (200, True)])
    def test_weather_prompt_boundary_on_a_distance_patch(self, client, world, distance, expected):
        group = world.group()

        patched = client.patch(f"/api/groups/{group.id}", json={"distance_yd": distance})

        assert patched.status_code == 200
        assert patched.json()["distance_yd"] == distance
        assert patched.json()["weather_prompt"] is expected

    def test_weather_cannot_be_both_entered_and_skipped_on_patch(self, client, world):
        group = world.group(context=world.ctx(distance_yd=300, temperature_f=60.0))
        url = f"/api/groups/{group.id}"

        skip_over_values = client.patch(url, json={"weather_skipped": True})
        values_and_skip = client.patch(url, json={"weather_skipped": True, "wind_mph": 4})
        cleared = client.patch(url, json={"temperature_f": None, "weather_skipped": True})

        for refused in (skip_over_values, values_and_skip):
            assert refused.status_code == 422 and refused.json()["code"] == "invalid_weather"
        assert cleared.status_code == 200 and cleared.json()["weather_skipped"] is True

    def test_entering_weather_after_a_skip_requires_unskipping(self, client, world):
        group = world.group(context=world.ctx(distance_yd=300, weather_skipped=True))
        url = f"/api/groups/{group.id}"

        refused = client.patch(url, json={"temperature_f": 55})
        accepted = client.patch(url, json={"temperature_f": 55, "weather_skipped": False})

        assert refused.status_code == 422 and refused.json()["code"] == "invalid_weather"
        assert accepted.json()["temperature_f"] == 55
        assert accepted.json()["weather_skipped"] is False

    def test_a_load_cannot_be_changed(self, client, world):
        group = world.group()

        response = client.patch(f"/api/groups/{group.id}", json={"load_id": world.load.id})

        assert response.status_code == 422 and response.json()["code"] == "validation_error"

    def test_a_distance_patch_recomputes_stored_mil_offsets_in_the_same_request(
        self, client, world
    ):
        group = world.group(offsets=FIVE, finalize=True)
        before = stored_offsets(world.conn, group.id)

        patched = client.patch(f"/api/groups/{group.id}", json={"distance_yd": 300})
        after = stored_offsets(world.conn, group.id)

        assert patched.status_code == 200
        assert [(dx, dy) for dx, dy, _, _ in after] == [(dx, dy) for dx, dy, _, _ in before]
        for dx_in, dy_in, dx_mil, dy_mil in after:
            assert dx_mil == pytest.approx(in_to_mil(dx_in, 300))
            assert dy_mil == pytest.approx(in_to_mil(dy_in, 300))
        assert after != before

    def test_only_the_patched_groups_offsets_change(self, client, world):
        target = world.group(offsets=FIVE, finalize=True)
        bystander = world.group(offsets=FIVE, finalize=True)
        before = stored_offsets(world.conn, bystander.id)

        client.patch(f"/api/groups/{target.id}", json={"distance_yd": 300})

        assert stored_offsets(world.conn, bystander.id) == before

    def test_a_patch_that_keeps_the_distance_leaves_offsets_alone(self, client, world, monkeypatch):
        group = world.group(offsets=FIVE, finalize=True)

        def boom(*_a, **_k):
            raise AssertionError("must not recompute")

        monkeypatch.setattr(group_service, "recompute_offsets", boom)

        for body in ({"notes": "x"}, {"distance_yd": 100}, {"position": "standing_field"}):
            assert client.patch(f"/api/groups/{group.id}", json=body).status_code == 200

    def test_distance_recompute_and_update_share_one_transaction(
        self, tolerant, world, monkeypatch
    ):
        group = world.group(offsets=FIVE, finalize=True)
        before = stored_offsets(world.conn, group.id)

        def boom(conn, group_id):
            raise RuntimeError("recompute failed")

        monkeypatch.setattr(group_service, "recompute_offsets", boom)
        response = tolerant.patch(f"/api/groups/{group.id}", json={"distance_yd": 300})

        assert response.status_code == 500
        assert repo_groups.get_group(world.conn, group.id).distance_yd == 100
        assert stored_offsets(world.conn, group.id) == before

    def test_the_distance_of_a_zero_linked_group_cannot_change(self, client, world):
        group = world.group(offsets=FIVE)
        link_to_zero(world.conn, group.id, _zero(world.conn, world.rifle, world.load))
        before = stored_offsets(world.conn, group.id)

        for distance in (200, 300):
            response = client.patch(f"/api/groups/{group.id}", json={"distance_yd": distance})
            assert (response.status_code, response.json()["code"]) == (409, "group_linked_to_zero")
        assert "detach" in response.json()["detail"]
        assert repo_groups.get_group(world.conn, group.id).distance_yd == 100
        assert stored_offsets(world.conn, group.id) == before

    def test_a_zero_linked_group_still_takes_position_suppressor_date_and_weather(
        self, client, world
    ):
        group = world.group(offsets=FIVE)
        link_to_zero(world.conn, group.id, _zero(world.conn, world.rifle, world.load))

        patched = client.patch(
            f"/api/groups/{group.id}",
            json={
                "position": "prone_off_pack",
                "suppressor": True,
                "shot_date": "2026-09-01",
                "temperature_f": 40.0,
                "notes": "ok",
                "distance_yd": 100,
            },
        )

        assert patched.status_code == 200
        assert (patched.json()["position"], patched.json()["shot_date"]) == (
            "prone_off_pack",
            "2026-09-01",
        )
        assert patched.json()["distance_yd"] == 100

    def test_resetting_an_unlinked_zeroing_group_to_general_recomputes_its_offsets(
        self, client, world
    ):
        group = world.group(offsets=FIVE, purpose="zero_step2")
        assert stored_offsets(world.conn, group.id) == [(None, None, None, None)] * 5

        restored = client.patch(f"/api/groups/{group.id}", json={"purpose": "general"})

        assert restored.status_code == 200 and restored.json()["purpose"] == "general"
        assert all(None not in row for row in stored_offsets(world.conn, group.id))
        assert restored.json()["masked"] is False

    @pytest.mark.parametrize("purpose", ["zero_step1", "zero_step2", "zero_confirm", "zero_check"])
    def test_a_patch_cannot_set_a_zeroing_purpose(self, client, world, purpose):
        group = world.group(offsets=TEN)
        before = stored_offsets(world.conn, group.id)

        response = client.patch(f"/api/groups/{group.id}", json={"purpose": purpose})

        assert response.status_code == 422 and response.json()["code"] == "invalid_purpose"
        assert repo_groups.get_group(world.conn, group.id).purpose == "general"
        assert stored_offsets(world.conn, group.id) == before

    def test_purpose_change_and_recompute_share_one_transaction(self, tolerant, world, monkeypatch):
        group = world.group(offsets=FIVE, purpose="zero_step2")
        before = stored_offsets(world.conn, group.id)

        def boom(conn, group_id):
            raise RuntimeError("recompute failed")

        monkeypatch.setattr(group_service, "recompute_offsets", boom)
        response = tolerant.patch(f"/api/groups/{group.id}", json={"purpose": "general"})

        assert response.status_code == 500
        assert repo_groups.get_group(world.conn, group.id).purpose == "zero_step2"
        assert stored_offsets(world.conn, group.id) == before

    def test_the_response_never_carries_point_of_impact_data(self, client, world):
        group = world.group(offsets=FIVE)

        body = client.patch(f"/api/groups/{group.id}", json={"notes": "x"}).json()

        assert not any(
            re.search(r"dx|dy|mean|poi|mil", key) for key in body if key != "elevation_dialed_mil"
        )
        assert "shots" not in body

    def test_a_final_group_keeps_its_count_and_photo_but_takes_notes_and_context(
        self, client, world
    ):
        group = world.group(offsets=TEN, finalize=True)
        photo = seed_photo(world.conn)

        count = client.patch(f"/api/groups/{group.id}", json={"shots_expected": 11})
        new_photo = client.patch(f"/api/groups/{group.id}", json={"photo_id": photo.id})
        notes = client.patch(f"/api/groups/{group.id}", json={"notes": "good one"})
        context = client.patch(
            f"/api/groups/{group.id}",
            json={"position": "standing_field", "shot_date": "2026-09-02"},
        )
        same_count = client.patch(f"/api/groups/{group.id}", json={"shots_expected": 10})

        assert (count.status_code, count.json()["code"]) == (409, "group_final")
        assert (new_photo.status_code, new_photo.json()["code"]) == (409, "group_final")
        assert notes.status_code == 200 and notes.json()["notes"] == "good one"
        assert context.status_code == 200 and context.json()["position"] == "standing_field"
        assert same_count.status_code == 200
        assert repo_groups.get_group(world.conn, group.id).shots_expected == 10

    def test_a_photo_can_be_attached_to_an_empty_group_and_clears_the_roi(self, client, world):
        first = seed_photo(world.conn)
        second = seed_photo(world.conn)
        created = client.post("/api/groups", json=group_body(world)).json()
        with world.conn:
            world.conn.execute(
                "UPDATE groups SET roi_json = ? WHERE id = ?",
                ('{"x0": 1, "y0": 2, "x1": 30, "y1": 40}', created["id"]),
            )

        attached = client.patch(f"/api/groups/{created['id']}", json={"photo_id": first.id})
        swapped = client.patch(f"/api/groups/{created['id']}", json={"photo_id": second.id})
        removed = client.patch(f"/api/groups/{created['id']}", json={"photo_id": None})

        assert attached.json()["photo_id"] == first.id and attached.json()["roi_px"] is None
        assert swapped.json()["photo_id"] == second.id
        assert removed.json()["photo_id"] is None

    def test_the_roi_stays_when_the_same_photo_is_set_again(self, client, world):
        photo = seed_photo(world.conn)
        group = world.group(
            offsets=[],
            photo=photo,
            aim=None,
            roi_json='{"x0": 1, "y0": 2, "x1": 30, "y1": 40}',
        )

        response = client.patch(f"/api/groups/{group.id}", json={"photo_id": photo.id})

        assert response.json()["roi_px"] == {"x0": 1, "y0": 2, "x1": 30, "y1": 40}

    def test_a_photo_cannot_change_once_the_group_has_shots(self, client, world):
        photo = seed_photo(world.conn)
        other = seed_photo(world.conn)
        group = world.group(offsets=FIVE, photo=photo)

        response = client.patch(f"/api/groups/{group.id}", json={"photo_id": other.id})

        assert (response.status_code, response.json()["code"]) == (409, "group_has_shots")
        assert repo_groups.get_group(world.conn, group.id).photo_id == photo.id

    def test_unknown_references_are_404(self, client, world):
        group = world.group(offsets=[], aim=None)

        photo = client.patch(f"/api/groups/{group.id}", json={"photo_id": 999})
        missing = client.patch("/api/groups/999", json={"notes": "x"})
        suppressor = client.patch(
            f"/api/groups/{group.id}", json={"suppressor": True, "suppressor_id": 999}
        )

        assert (photo.status_code, photo.json()["code"]) == (404, "photo_not_found")
        assert (missing.status_code, missing.json()["code"]) == (404, "group_not_found")
        assert (suppressor.status_code, suppressor.json()["code"]) == (404, "suppressor_not_found")

    def test_a_patch_never_sets_or_clears_a_zero_link(self, client, world):
        group = world.group(offsets=FIVE)
        sibling = seed_load(world.conn, world.rifle, label="Second load")
        ids = [
            999,
            _zero(world.conn, world.other_rifle, world.other_load),
            _zero(world.conn, world.rifle, sibling),
            _zero(world.conn, world.rifle, world.load),
        ]

        for zero_id in [*ids, None]:
            response = client.patch(f"/api/groups/{group.id}", json={"zero_id": zero_id})
            assert (response.status_code, response.json()["code"]) == (422, "invalid_purpose")
        assert repo_groups.get_group(world.conn, group.id).zero_id is None

    def test_changing_the_purpose_of_a_zero_linked_group_is_refused(self, client, world):
        group = world.group(offsets=TEN)
        zero = _zero(world.conn, world.rifle, world.load)
        link_to_zero(world.conn, group.id, zero)

        response = client.patch(f"/api/groups/{group.id}", json={"purpose": "general"})

        assert (response.status_code, response.json()["code"]) == (409, "group_linked_to_zero")
        row = repo_groups.get_group(world.conn, group.id)
        assert (row.purpose, row.zero_id) == ("zero_check", zero)
        assert client.patch(f"/api/groups/{group.id}", json={"notes": "ok"}).status_code == 200

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"shots_expected": None},
            {"shots_expected": 0},
            {"purpose": "warmup"},
            {"purpose": None},
            {"notes": None},
            {"lot_id": 3},
            {"status": "final"},
            {"aim_x_in": 1.0},
            {"session_id": 1},
            {"shot_time": "25:00"},
            {"utc_offset": "UTC"},
            {"distance_yd": 0},
            {"wind_clock": 13},
            {"position": "sandbags"},
        ],
    )
    def test_invalid_patch_bodies_are_422(self, client, world, body):
        group = world.group(offsets=FIVE)

        response = client.patch(f"/api/groups/{group.id}", json=body)

        assert response.status_code == 422 and response.json()["code"] == "validation_error"


# ------------------------------------------------------------------------------ suppressors


class TestSuppressors:
    """T4.0n (B-65): named suppressors, remembered and chosen per group."""

    def save(self, client, name: str):
        return client.post("/api/suppressors", json={"name": name})

    def test_a_new_name_is_created_with_201_and_listed_by_name(self, client):
        zebra = self.save(client, "Zebra")
        alpha = self.save(client, "alpha 7")

        listing = client.get("/api/suppressors")

        assert (zebra.status_code, alpha.status_code) == (201, 201)
        assert set(zebra.json()) == {"id", "name", "created_at"}
        assert [s["name"] for s in listing.json()] == ["alpha 7", "Zebra"]
        assert listing.status_code == 200

    def test_a_case_different_name_returns_the_saved_record_with_200_and_adds_nothing(self, client):
        first = self.save(client, "Thunder Beast Ultra")

        again = self.save(client, "thunder beast ULTRA")
        spaced = self.save(client, "  Thunder   Beast Ultra  ")

        assert again.status_code == 200 and spaced.status_code == 200
        assert again.json() == first.json() == spaced.json()
        assert len(client.get("/api/suppressors").json()) == 1

    def test_unicode_case_differences_also_dedupe(self, client):
        first = self.save(client, "Straße Can")

        again = self.save(client, "STRASSE CAN")

        assert again.status_code == 200 and again.json()["id"] == first.json()["id"]

    @pytest.mark.parametrize(
        "body", [{"name": ""}, {"name": "   "}, {"name": "x" * 81}, {}, {"name": 5}]
    )
    def test_a_blank_overlong_or_missing_name_is_422_and_saves_nothing(self, client, body):
        response = client.post("/api/suppressors", json=body)

        assert (response.status_code, response.json()["code"]) == (422, "validation_error")
        assert client.get("/api/suppressors").json() == []

    def test_an_unknown_field_is_422(self, client):
        assert client.post("/api/suppressors", json={"name": "A", "id": 3}).status_code == 422

    def test_create_with_a_suppressor_id_stores_it_and_reports_the_name_everywhere(
        self, client, world
    ):
        omega = self.save(client, "Omega").json()

        created = client.post(
            "/api/groups", json=group_body(world, suppressor=True, suppressor_id=omega["id"])
        )
        fetched = client.get(f"/api/groups/{created.json()['id']}").json()
        listed = client.get("/api/groups").json()[0]

        assert created.status_code == 201
        for payload in (created.json(), fetched, listed):
            assert (payload["suppressor"], payload["suppressor_id"]) == (True, omega["id"])
            assert payload["suppressor_name"] == "Omega"

    def test_a_plain_suppressed_group_has_no_name(self, client, world):
        created = client.post("/api/groups", json=group_body(world, suppressor=True)).json()

        assert (created["suppressor_id"], created["suppressor_name"]) == (None, None)

    def test_an_unsuppressed_group_reports_no_suppressor_fields(self, client, world):
        created = client.post("/api/groups", json=group_body(world)).json()

        assert (created["suppressor"], created["suppressor_id"]) == (False, None)
        assert created["suppressor_name"] is None

    def test_create_with_an_id_but_suppressor_false_is_422(self, client, world):
        omega = self.save(client, "Omega").json()
        no_flag = {
            k: v
            for k, v in group_body(world, suppressor_id=omega["id"]).items()
            if k != "suppressor"
        }

        for body in (group_body(world, suppressor=False, suppressor_id=omega["id"]), no_flag):
            assert client.post("/api/groups", json=body).status_code == 422, body
        assert repo_groups.list_groups(world.conn) == []

    def test_create_with_an_unknown_suppressor_id_is_404(self, client, world):
        response = client.post(
            "/api/groups", json=group_body(world, suppressor=True, suppressor_id=999)
        )

        assert (response.status_code, response.json()["code"]) == (404, "suppressor_not_found")
        assert repo_groups.list_groups(world.conn) == []

    def test_patch_names_changes_and_clears_the_suppressor(self, client, world):
        omega = self.save(client, "Omega").json()
        other = self.save(client, "Other").json()
        url = f"/api/groups/{world.group().id}"

        named = client.patch(url, json={"suppressor": True, "suppressor_id": omega["id"]}).json()
        renamed = client.patch(url, json={"suppressor_id": other["id"]}).json()
        unnamed = client.patch(url, json={"suppressor_id": None}).json()

        assert (named["suppressor_id"], named["suppressor_name"]) == (omega["id"], "Omega")
        assert (renamed["suppressor_id"], renamed["suppressor_name"]) == (other["id"], "Other")
        assert (unnamed["suppressor"], unnamed["suppressor_id"]) == (True, None)
        assert unnamed["suppressor_name"] is None

    def test_unchecking_the_suppressor_clears_the_id(self, client, world):
        omega = self.save(client, "Omega").json()
        group = world.group()
        url = f"/api/groups/{group.id}"
        client.patch(url, json={"suppressor": True, "suppressor_id": omega["id"]})

        off = client.patch(url, json={"suppressor": False}).json()

        assert (off["suppressor"], off["suppressor_id"], off["suppressor_name"]) == (
            False,
            None,
            None,
        )
        assert repo_groups.get_group(world.conn, group.id).suppressor_id is None

    def test_patch_unrelated_field_keeps_the_named_suppressor(self, client, world):
        omega = self.save(client, "Omega").json()
        url = f"/api/groups/{world.group().id}"
        client.patch(url, json={"suppressor": True, "suppressor_id": omega["id"]})

        kept = client.patch(url, json={"notes": "windy"}).json()

        assert (kept["suppressor_id"], kept["notes"]) == (omega["id"], "windy")

    def test_patch_with_an_id_and_suppressor_false_is_422_and_changes_nothing(self, client, world):
        omega = self.save(client, "Omega").json()
        group = world.group()
        url = f"/api/groups/{group.id}"

        both = client.patch(url, json={"suppressor": False, "suppressor_id": omega["id"]})
        on_a_plain_group = client.patch(url, json={"suppressor_id": omega["id"]})

        assert both.status_code == 422 and on_a_plain_group.status_code == 422
        stored = repo_groups.get_group(world.conn, group.id)
        assert (stored.suppressor, stored.suppressor_id) == (0, None)

    def test_patch_with_an_unknown_suppressor_id_is_404_and_changes_nothing(self, client, world):
        group = world.group()

        response = client.patch(
            f"/api/groups/{group.id}", json={"suppressor": True, "suppressor_id": 999}
        )

        assert (response.status_code, response.json()["code"]) == (404, "suppressor_not_found")
        stored = repo_groups.get_group(world.conn, group.id)
        assert (stored.suppressor, stored.suppressor_id) == (0, None)

    @pytest.mark.parametrize("bad", [0, -1, 2**63, "abc", 1.5])
    def test_a_malformed_suppressor_id_is_422(self, client, world, bad):
        group = world.group()

        response = client.patch(
            f"/api/groups/{group.id}", json={"suppressor": True, "suppressor_id": bad}
        )

        assert (response.status_code, response.json()["code"]) == (422, "validation_error")

    def test_a_cross_origin_suppressor_write_is_refused_and_saves_nothing(self, client):
        response = client.post(
            "/api/suppressors", json={"name": "Omega"}, headers={"Origin": "http://evil.example"}
        )

        assert (response.status_code, response.json()["code"]) == (403, "bad_origin")
        assert client.get("/api/suppressors").json() == []

    def test_a_locked_database_is_503_busy_for_the_suppressor_routes(self, tolerant, monkeypatch):
        def locked(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(service, "list_suppressors", locked)
        monkeypatch.setattr(service, "create_suppressor", locked)

        got = tolerant.get("/api/suppressors")
        posted = tolerant.post("/api/suppressors", json={"name": "Omega"})

        for response in (got, posted):
            assert (response.status_code, response.json()["code"]) == (503, "busy")
            assert response.headers["retry-after"] == "1"


# ------------------------------------------------------------------------------ masking


class TestMaskedFlagAndAim:
    """T3.11 (B-43): `masked` is server-computed from the stored shot count and the aim point is
    withheld for masked groups in every group payload (list, create, patch)."""

    MASKED = [("zero_check", 2), ("zero_step2", 6), ("zero_step1", 5)]
    OPEN = [("zero_step1", 3), ("general", 4), ("zero_step2", 10)]
    AIM = {"x_in": 4.25, "y_in": 5.5}

    def seeded(self, world, cases):
        return {
            (purpose, n): world.group(
                offsets=[Offset(0.1 * i, 0.05 * i) for i in range(n)], purpose=purpose
            )
            for purpose, n in cases
        }

    def test_the_list_flags_masked_groups_and_withholds_their_aim(self, client, world):
        groups = self.seeded(world, self.MASKED + self.OPEN)

        rows = {g["id"]: g for g in client.get("/api/groups").json()}

        for key in self.MASKED:
            assert (rows[groups[key].id]["masked"], rows[groups[key].id]["aim"]) == (True, None), (
                key
            )
        for key in self.OPEN:
            assert (rows[groups[key].id]["masked"], rows[groups[key].id]["aim"]) == (
                False,
                self.AIM,
            ), key

    def test_a_patch_to_general_unmasks_a_stored_zeroing_group_and_reveals_the_aim(
        self, client, world
    ):
        group = self.seeded(world, [("zero_step1", 5)])[("zero_step1", 5)]
        url = f"/api/groups/{group.id}"
        assert client.get("/api/groups").json()[0]["masked"] is True

        back = client.patch(url, json={"purpose": "general"}).json()
        again = client.patch(url, json={"notes": "kept"}).json()

        assert (back["masked"], back["aim"]) == (False, self.AIM)
        assert (again["masked"], again["aim"]) == (False, self.AIM)

    def test_create_returns_masked_false_for_a_new_general_group_with_no_shots(self, client, world):
        general = client.post("/api/groups", json=group_body(world)).json()

        assert general["masked"] is False and general["purpose"] == "general"

    def test_masked_is_in_the_group_model_with_a_false_default(self):
        assert GroupOut.model_fields["masked"].default is False


# ------------------------------------------------------------- denial and error mapping


WRITES = (
    ("POST", "/api/groups", lambda w: group_body(w)),
    ("PATCH", "/api/groups/{group}", lambda w: {"notes": "x"}),
    ("POST", "/api/suppressors", lambda w: {"name": "Omega"}),
)


@pytest.fixture
def ids(world):
    return {"group": world.group().id, "rifle": world.rifle.id}


class TestDenialAndErrorMapping:
    @pytest.mark.parametrize(("method", "path", "body"), WRITES)
    def test_a_cross_origin_write_is_refused_and_changes_nothing(
        self, client, world, ids, method, path, body
    ):
        before = len(repo_groups.list_groups(world.conn))

        response = client.request(
            method, path.format(**ids), json=body(world), headers={"Origin": "http://evil.example"}
        )

        assert (response.status_code, response.json()["code"]) == (403, "bad_origin")
        assert len(repo_groups.list_groups(world.conn)) == before
        assert repo_groups.get_group(world.conn, ids["group"]).notes == ""
        assert repo_groups.list_suppressors(world.conn) == []

    @pytest.mark.parametrize(("method", "path", "body"), WRITES)
    def test_a_non_json_write_is_refused_with_415(self, client, world, ids, method, path, body):
        response = client.request(
            method,
            path.format(**ids),
            content=b"notes=x",
            headers={"Content-Type": "text/plain"},
        )

        assert response.status_code == 415

    @pytest.mark.parametrize(("method", "path", "body"), WRITES)
    def test_malformed_json_is_422_never_500(self, tolerant, world, ids, method, path, body):
        response = tolerant.request(
            method,
            path.format(**ids),
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 422 and response.json()["code"] == "validation_error"

    @pytest.mark.parametrize(
        "error", [GroupLinkMismatch, GroupNotLinkable, ZeroTargetMismatch, LoadNotInRifle]
    )
    @pytest.mark.parametrize(
        ("method", "path", "body", "function"),
        [
            ("POST", "/api/groups", lambda w: group_body(w), "create_group"),
            ("PATCH", "/api/groups/{group}", lambda w: {"notes": "x"}, "update_group"),
        ],
    )
    def test_link_errors_map_to_409_at_every_write_route(
        self, tolerant, world, ids, monkeypatch, error, method, path, body, function
    ):
        def raise_it(*_a, **_k):
            raise error("these do not belong together")

        monkeypatch.setattr(service, function, raise_it)

        response = tolerant.request(method, path.format(**ids), json=body(world))

        assert response.status_code == 409
        assert response.json()["code"] in {
            "group_link_mismatch",
            "group_not_linkable",
            "zero_target_mismatch",
            "load_not_in_rifle",
        }
        assert response.json()["detail"] == "these do not belong together"

    @pytest.mark.parametrize(
        ("method", "path", "body", "function"),
        [
            ("POST", "/api/groups", lambda w: group_body(w), "create_group"),
            ("PATCH", "/api/groups/{group}", lambda w: {"notes": "x"}, "update_group"),
            ("GET", "/api/rifles/{rifle}/group-defaults", None, "group_defaults"),
        ],
    )
    def test_a_locked_database_is_503_busy_with_a_retry_hint(
        self, tolerant, world, ids, monkeypatch, method, path, body, function
    ):
        def locked(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(service, function, locked)

        response = tolerant.request(method, path.format(**ids), json=body(world) if body else None)

        assert response.status_code == 503
        assert response.json()["code"] == "busy"
        assert "try again" in response.json()["detail"]
        assert response.headers["retry-after"] == "1"

    def test_other_operational_errors_are_not_disguised_as_busy(
        self, tolerant, world, ids, monkeypatch
    ):
        def broken(*_a, **_k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(service, "update_group", broken)

        response = tolerant.patch(f"/api/groups/{ids['group']}", json={"notes": "x"})

        assert response.status_code == 500 and response.json()["code"] == "internal_error"

    def test_integrity_errors_are_409_constraint_violation(self, tolerant, world, ids, monkeypatch):
        def violated(*_a, **_k):
            raise sqlite3.IntegrityError("CHECK constraint failed")

        monkeypatch.setattr(service, "create_group", violated)

        response = tolerant.post("/api/groups", json=group_body(world))

        assert (response.status_code, response.json()["code"]) == (409, "constraint_violation")

    def test_real_lock_contention_returns_503_not_500(self, app, settings, world, ids):
        def short_timeout_db() -> Iterator[sqlite3.Connection]:
            connection = connect(settings.db_path)
            connection.execute("PRAGMA busy_timeout = 50")
            try:
                yield connection
            finally:
                connection.close()

        app.dependency_overrides[get_db] = short_timeout_db
        holder = connect(settings.db_path)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as busy_client:
                locked = busy_client.post("/api/groups", json=group_body(world))
                locked_group = busy_client.patch(f"/api/groups/{ids['group']}", json={"notes": "n"})
        finally:
            holder.rollback()
            holder.close()
        with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as free_client:
            free = free_client.post("/api/groups", json=group_body(world))
        app.dependency_overrides.clear()

        assert (locked.status_code, locked.json()["code"]) == (503, "busy")
        assert (locked_group.status_code, locked_group.json()["code"]) == (503, "busy")
        assert free.status_code == 201

    def test_a_service_group_error_maps_by_its_code(self, tolerant, world, ids, monkeypatch):
        for code, status in (("group_not_found", 404), ("invalid_thing", 422), ("whatever", 409)):

            def failing(*_a, _code=code, **_k):
                raise group_service.GroupError(_code, "nope")

            monkeypatch.setattr(service, "update_group", failing)
            response = tolerant.patch(f"/api/groups/{ids['group']}", json={"notes": "x"})
            assert (response.status_code, response.json()["code"]) == (status, code)


def test_the_module_registers_exactly_the_group_routes():
    from rga.web.routes import groups

    methods_paths = set()
    for route in groups.router.routes:
        for method in route.methods:
            methods_paths.add((method, route.path))
    assert methods_paths == {
        ("POST", "/api/groups"),
        ("PATCH", "/api/groups/{group_id}"),
        ("GET", "/api/rifles/{rifle_id}/group-defaults"),
        ("GET", "/api/suppressors"),
        ("POST", "/api/suppressors"),
    }
    assert routes is not None


# ------------------------------------------------------------------------- page context


class _Captured:
    """Records the template name and context each page render is given."""

    def __init__(self, app, monkeypatch):
        self.calls: list[tuple[str, dict]] = []
        templates = app.state.templates
        original = templates.TemplateResponse

        def spy(request, name, context=None, *args, **kwargs):
            self.calls.append((name, context or {}))
            return original(request, name, context, *args, **kwargs)

        monkeypatch.setattr(templates, "TemplateResponse", spy)

    def context(self, name: str) -> dict:
        return next(ctx for template, ctx in reversed(self.calls) if template == name)


class TestPageContexts:
    def test_the_rifle_page_gets_days_defaults_suppressors_and_state(
        self, app, client, world, monkeypatch
    ):
        captured = _Captured(app, monkeypatch)
        omega = client.post("/api/suppressors", json={"name": "Omega"}).json()
        photo = seed_photo(world.conn)
        older = world.group(
            offsets=TEN, finalize=True, context=world.ctx(date="2026-09-01", distance_yd=100)
        )
        newer = world.group(
            offsets=FIVE,
            photo=photo,
            lot_code="LOT-Q",
            context=world.ctx(
                date="2026-09-20", distance_yd=300, suppressor=True, suppressor_id=omega["id"]
            ),
        )

        response = client.get(f"/rifles/{world.rifle.id}")
        context = captured.context("rifle_detail.html")
        state = __import__("json").loads(context["state_json"])

        assert response.status_code == 200
        days = context["detail"].days
        assert [d.shot_date for d in days] == ["2026-09-20", "2026-09-01"]
        assert days[0].distances == [300]
        assert [r.view.group.id for r in days[0].rows] == [newer.id]
        assert [r.view.group.id for r in days[1].rows] == [older.id]
        assert [s.name for s in context["detail"].suppressors] == ["Omega"]
        assert context["defaults"].source_group_id == newer.id
        assert context["position_labels"]["prone_off_pack"]
        assert state["rifle_id"] == world.rifle.id
        assert state["weather_prompt_yd"] == 200
        assert state["defaults"]["lot_code"] == "LOT-Q"
        assert state["defaults"]["suppressor_id"] == omega["id"]
        assert state["lots"][str(world.load.id)] == ["LOT-1", "LOT-Q"] or set(
            state["lots"][str(world.load.id)]
        ) == {"LOT-1", "LOT-Q"}
        assert state["photo_confirmed"] == {str(photo.id): True}

    def test_the_rifle_page_defaults_are_empty_for_a_new_rifle(
        self, app, client, world, monkeypatch
    ):
        captured = _Captured(app, monkeypatch)

        response = client.get(f"/rifles/{world.rifle.id}")
        context = captured.context("rifle_detail.html")

        assert response.status_code == 200
        assert context["detail"].days == []
        assert context["defaults"].source_group_id is None
        assert __import__("json").loads(context["state_json"])["defaults"]["distance_yd"] is None

    def test_the_dashboard_lists_the_latest_groups_by_date_time_and_id(
        self, app, client, world, monkeypatch
    ):
        captured = _Captured(app, monkeypatch)
        groups = [
            world.group(offsets=FIVE, context=world.ctx(date=day, time=clock))
            for day, clock in [
                ("2026-09-01", "09:00:00"),
                ("2026-09-02", None),
                ("2026-09-02", "07:00:00"),
                ("2026-09-02", "07:00:00"),
            ]
        ]

        response = client.get("/")
        recent = captured.context("index.html")["dashboard"].recent_groups

        assert response.status_code == 200
        assert [r.group.id for r in recent] == [
            groups[3].id,
            groups[2].id,
            groups[1].id,
            groups[0].id,
        ]
        assert recent[0].rifle_name == "Mine"

    def test_the_group_page_gets_suppressors_and_position_labels(
        self, app, client, world, monkeypatch
    ):
        captured = _Captured(app, monkeypatch)
        client.post("/api/suppressors", json={"name": "Omega"})
        group = world.group(offsets=TEN, finalize=True)

        response = client.get(f"/groups/{group.id}")
        context = captured.context("group.html")

        assert response.status_code == 200
        assert [s.name for s in context["suppressors"]] == ["Omega"]
        assert context["position_labels"]["bench"]

    def test_the_review_page_state_counts_the_other_groups_on_the_photo(
        self, app, client, world, monkeypatch
    ):
        captured = _Captured(app, monkeypatch)
        photo = seed_photo(world.conn)
        first = world.group(offsets=[], aim=None, photo=photo)
        world.group(offsets=[], aim=None, photo=photo)
        alone = world.group(offsets=[], aim=None)

        def state(group):
            assert client.get(f"/groups/{group.id}/review").status_code == 200
            return __import__("json").loads(captured.context("review.html")["state_json"])

        shared_state, alone_state = state(first), state(alone)

        assert shared_state["other_groups_on_photo"] == 1
        assert alone_state["other_groups_on_photo"] == 0
        assert shared_state["shot_date"] == "2026-09-30" and "session_id" not in shared_state
        assert shared_state["legacy_session_notes"] == ""
