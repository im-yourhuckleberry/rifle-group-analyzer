"""T3.7: zeroing flow API (Step 1/2/3, zero check) and the stepper page, on the real app and a temp DB."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rga.config import Settings
from rga.db import repo_groups, repo_rifles, repo_zeroing
from rga.db.connection import connect
from rga.domain.types import (
    Advice,
    AdviceKind,
    Correction,
    GroupStats,
    Offset,
    PlanePoint,
    Shot,
    ShotOrigin,
)
from rga.domain.zeroing import ZERO_CHECK_LOST_ZERO_MESSAGE, ZERO_CHECK_OK_MESSAGE
from rga.services import groups as group_service
from rga.services import zeroing as service
from rga.web.app import create_app
from rga.web.routes.zeroing import advice_out
from rga.web.schemas import ROUTE_SCHEMAS, AdviceOut, GroupStatsOut, SizeOnlyStatsOut
from tests.support.live_server import run_live_server
from tests.support.seed import seed_context, seed_group, seed_load, seed_photo, seed_rifle

ORIGIN = "http://127.0.0.1:8765"
IN_PER_MIL = 3.6
ROOT = Path(__file__).resolve().parents[2]
SIZE_ONLY_KEYS = {"n", "es_in", "es_moa", "includes_estimated", "es_label"}
SHOOT_MORE_5 = "Shoot more shots: 5 of 10 so far, 5 left to fire"


def ring(cx_in: float, cy_in: float, n: int = 10, radius_in: float = 0.4) -> list[Offset]:
    """`n` shots on a circle whose centroid is exactly (cx_in, cy_in) of the aim (y up)."""
    return [
        Offset(
            cx_in + radius_in * math.cos(2 * math.pi * i / n),
            cy_in + radius_in * math.sin(2 * math.pi * i / n),
        )
        for i in range(n)
    ]


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
    s100: object
    s100_other: object
    s300: object

    def group(self, offsets, *, context=None, load=None, **kw):
        return seed_group(
            self.conn,
            context=context or self.s100,
            load=load or self.load,
            offsets=offsets,
            **kw,
        )


@pytest.fixture
def world(conn) -> World:
    rifle = seed_rifle(conn, name="Mine")
    other = seed_rifle(conn, name="Other")
    return World(
        conn,
        rifle,
        seed_load(conn, rifle),
        other,
        seed_load(conn, other, label="Theirs"),
        seed_context(rifle, distance_yd=100),
        seed_context(other, distance_yd=100),
        seed_context(rifle, distance_yd=300),
    )


def new_zero(client, world, mode="existing", **extra):
    response = client.post(
        "/api/zeros",
        json={"rifle_id": world.rifle.id, "load_id": world.load.id, "mode": mode, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


def link(client, zero_id, group_id):
    return client.post(f"/api/zeros/{zero_id}/groups", json={"group_id": group_id})


def dialed(client, zero_id, up=0.0, right=0.0):
    return client.post(f"/api/zeros/{zero_id}/dialed", json={"up_mil": up, "right_mil": right})


def complete_zero(client, world, dot=0.75, confirm_centroid=(0.0, 0.0)) -> dict:
    """Existing-setup zero taken through every step to `complete`."""
    zero = new_zero(client, world)
    assert link(client, zero["id"], world.group(ring(1.08, 0.72)).id).status_code == 200
    assert dialed(client, zero["id"], -0.2, -0.3).status_code == 200
    ready = link(client, zero["id"], world.group(ring(*confirm_centroid)).id)
    assert ready.json()["state"] == "step3_dot", ready.text
    done = client.post(f"/api/zeros/{zero['id']}/dot", json={"dot_size_in": dot})
    assert done.status_code == 200, done.text
    return done.json()


def stored(conn, group_id):
    return repo_groups.get_group(conn, group_id)


def offsets_of(conn, group_id):
    return [(s.dx_in, s.dy_in, s.dx_mil, s.dy_mil) for s in repo_groups.list_shots(conn, group_id)]


# ----------------------------------------------------------------------------- happy path


class TestHappyPath:
    def test_new_setup_runs_step_1_to_a_complete_zero_and_supersedes_the_old_one(
        self, client, world, conn
    ):
        old = complete_zero(client, world)
        zero = new_zero(client, world, "new_setup")
        zid = zero["id"]
        assert zero["state"] == "step1" and zero["advice"] is None and zero["groups"] == []

        step1 = world.group([Offset(0.36, 0.72)])
        after = link(client, zid, step1.id).json()
        assert after["state"] == "step1"
        assert after["groups"] == [
            {"group_id": step1.id, "purpose": "zero_step1", "status": "final", "shots": 1}
        ]
        assert after["advice"]["kind"] == "correction"
        assert after["advice"]["correction"] == {
            "up_mil": -0.2,
            "right_mil": -0.1,
            "text": "Dial DOWN 0.2, LEFT 0.1",
        }
        assert after["advice"]["stats"]["poi_text"] == "0.2 mil high, 0.1 mil right"
        assert stored(conn, step1.id).purpose == "zero_step1"

        assert dialed(client, zid, -0.2, -0.1).json()["state"] == "step2_initial"

        initial = world.group(ring(1.08, 0.72))
        advice = link(client, zid, initial.id).json()["advice"]
        assert advice["kind"] == "correction"
        assert advice["correction"]["text"] == "Dial DOWN 0.2, LEFT 0.3"
        assert advice["stats"]["n"] == 10
        assert stored(conn, initial.id).purpose == "zero_step2"

        assert dialed(client, zid, -0.2, -0.3).json()["state"] == "step2_confirm"
        assert dialed_value(client, zid) == (-0.2, -0.3)

        confirm = world.group(ring(0.1 * IN_PER_MIL, 0.0))
        judged = link(client, zid, confirm.id).json()
        assert judged["state"] == "step3_dot"
        assert judged["advice"]["kind"] == "touch_up_ok"
        assert judged["advice"]["correction"]["text"] == "Dial LEFT 0.1"
        assert set(judged["advice"]["stats"]) == SIZE_ONLY_KEYS
        assert judged["advice"]["stats"]["n"] == 20
        assert judged["zero_shot_count"] == 20
        assert judged["zero_es_in"] == pytest.approx(0.8)
        assert judged["suggested_dot_in"] == 0.5
        assert stored(conn, confirm.id).purpose == "zero_confirm"

        done = client.post(f"/api/zeros/{zid}/dot", json={"dot_size_in": 0.75}).json()
        assert done["state"] == "complete"
        assert done["dot_size_in"] == 0.75
        assert done["completed_at"] is not None
        assert client.get(f"/api/zeros/{old['id']}").json()["state"] == "superseded"
        active = repo_zeroing.active_zero(conn, world.rifle.id, world.load.id)
        assert active is not None and active.id == zid

    def test_a_second_confirm_group_gives_the_thirty_shot_judgment(self, client, world):
        zero = new_zero(client, world)
        link(client, zero["id"], world.group(ring(1.08, 0.72)).id)
        dialed(client, zero["id"], -0.2, -0.3)
        off = link(client, zero["id"], world.group(ring(0.3 * IN_PER_MIL, 0.2 * IN_PER_MIL)).id)
        assert off.json()["state"] == "step2_confirm"
        assert off.json()["advice"]["kind"] == "confirm_again"
        assert off.json()["advice"]["correction"]["text"] == "Dial DOWN 0.2, LEFT 0.3"
        assert off.json()["zero_es_in"] is None

        good = link(client, zero["id"], world.group(ring(0.0, 0.0)).id).json()

        assert good["state"] == "step3_dot"
        assert good["advice"]["kind"] == "ready_for_dot"
        assert good["advice"]["stats"]["n"] == 30
        assert "very high confidence" in good["advice"]["message"]
        assert good["zero_shot_count"] == 30

    def test_completing_only_supersedes_the_same_rifle_and_load(self, client, world, conn):
        other_load = seed_load(conn, world.rifle, label="Second load")
        first = complete_zero(client, world)
        other = client.post(
            "/api/zeros",
            json={"rifle_id": world.rifle.id, "load_id": other_load.id, "mode": "existing"},
        ).json()
        assert other["rifle_id"] == world.rifle.id
        assert client.get(f"/api/zeros/{first['id']}").json()["state"] == "complete"


def dialed_value(client, zero_id):
    zero = client.get(f"/api/zeros/{zero_id}").json()
    return zero["dialed_up_mil"], zero["dialed_right_mil"]


# ----------------------------------------------------------------------------- shoot more / masking


class TestShortGroupsNeverGetANumber:
    def test_a_five_shot_step_2_group_gets_shoot_more_and_no_correction(self, client, world):
        zero = new_zero(client, world)

        body = link(client, zero["id"], world.group(ring(1.08, 0.72, n=5)).id).json()

        advice = body["advice"]
        assert advice["kind"] == "shoot_more"
        assert advice["correction"] is None
        assert advice["message"] == SHOOT_MORE_5
        assert set(advice["stats"]) == SIZE_ONLY_KEYS
        assert advice["stats"]["n"] == 5
        text = json.dumps(advice).lower()
        for banned in ("mean", "poi", "mil", "dx", "dy", "dial", "high", "low", "left", "right"):
            assert banned not in text.replace("left to fire", "")
        assert body["state"] == "step2_initial"
        refused = dialed(client, zero["id"], -0.2, -0.3)
        assert refused.status_code == 409
        assert refused.json()["code"] == "initial_group_not_ready"

    @pytest.mark.parametrize("shots", range(1, 10))
    def test_every_short_step_2_and_confirm_group_is_size_only(self, client, world, conn, shots):
        zero = new_zero(client, world)
        group = world.group(ring(1.08, 0.72, n=shots))
        body = link(client, zero["id"], group.id).json()
        assert body["advice"]["kind"] == "shoot_more"
        assert body["advice"]["correction"] is None
        assert set(body["advice"]["stats"]) == SIZE_ONLY_KEYS
        for dx, dy, dx_mil, dy_mil in offsets_of(conn, group.id):
            assert dx is dy is dx_mil is dy_mil is None
        assert all(s.x_in is not None for s in repo_groups.list_shots(conn, group.id))

    def test_a_short_confirm_group_is_size_only_and_stored_without_offsets(
        self, client, world, conn
    ):
        zero = new_zero(client, world)
        link(client, zero["id"], world.group(ring(1.08, 0.72)).id)
        dialed(client, zero["id"], -0.2, -0.3)
        short = world.group(ring(0.5, 0.5, n=4))

        body = link(client, zero["id"], short.id).json()

        assert body["advice"]["kind"] == "shoot_more"
        assert body["advice"]["message"] == "Shoot more shots: 4 of 10 so far, 6 left to fire"
        assert body["advice"]["correction"] is None
        assert set(body["advice"]["stats"]) == SIZE_ONLY_KEYS
        assert all(set(o) == {None} for o in offsets_of(conn, short.id))
        assert stored(conn, short.id).purpose == "zero_confirm"

    def test_step_1_and_ten_shot_step_2_groups_keep_their_offsets(self, client, world, conn):
        zero = new_zero(client, world, "new_setup")
        step1 = world.group([Offset(0.36, 0.72), Offset(0.4, 0.7)])
        link(client, zero["id"], step1.id)
        assert all(o[0] is not None for o in offsets_of(conn, step1.id))
        dialed(client, zero["id"], -0.2, -0.1)
        initial = world.group(ring(1.08, 0.72))
        link(client, zero["id"], initial.id)
        assert all(o[0] is not None for o in offsets_of(conn, initial.id))

    def test_a_zero_check_group_has_no_offsets_even_with_one_shot(self, client, world, conn):
        done = complete_zero(client, world)
        check = world.group([Offset(0.1, 0.1)])
        assert all(o[0] is not None for o in offsets_of(conn, check.id))

        response = client.post(f"/api/zeros/{done['id']}/checks", json={"group_id": check.id})

        assert response.status_code == 201
        assert all(set(o) == {None} for o in offsets_of(conn, check.id))
        assert stored(conn, check.id).purpose == "zero_check"
        assert group_service.group_stats(conn, check.id).__class__ is group_service.SizeOnlyStats
        text = json.dumps(response.json()).lower()
        assert "mean" not in text and "mil" not in text and "dx" not in text

    def test_group_without_an_aim_point_is_invalid_advice_never_a_crash(self, client, world):
        zero = new_zero(client, world)
        group = world.group(ring(1.08, 0.72), aim=None, finalize=False, shots_expected=10)

        body = link(client, zero["id"], group.id).json()

        assert body["advice"]["kind"] == "invalid"
        assert body["advice"]["message"] == service.AIM_MISSING_MESSAGE
        assert body["advice"]["correction"] is None
        assert body["advice"]["stats"] is None


# ----------------------------------------------------------------------------- Advice -> AdviceOut


FULL_STATS = GroupStats(
    n=7,
    es_in=1.5,
    es_moa=1.43,
    mean_dx_in=0.72,
    mean_dy_in=-1.08,
    mean_dx_mil=0.2,
    mean_dy_mil=-0.3,
    includes_estimated=True,
)
CORRECTION = Correction(0.3, -0.2, "Dial UP 0.3, LEFT 0.2")


def view(advice: Advice, poi_text: str = "", size=None) -> service.AdviceView:
    return service.AdviceView(advice, poi_text, size)


class TestAdviceConversion:
    @pytest.mark.parametrize("kind", [AdviceKind.SHOOT_MORE, AdviceKind.INVALID])
    def test_no_number_kinds_drop_the_correction_and_every_poi_field(self, kind):
        advice = Advice(
            kind, "Shoot more shots: 7 of 10 so far, 3 left to fire", CORRECTION, FULL_STATS
        )

        out = advice_out(view(advice, "0.3 mil low, 0.2 mil right"))

        assert out.correction is None
        assert isinstance(out.stats, SizeOnlyStatsOut)
        assert out.stats.model_dump() == {
            "n": 7,
            "es_in": 1.5,
            "es_moa": 1.43,
            "includes_estimated": True,
            "es_label": out.stats.es_label,
        }
        assert AdviceOut.model_validate(out.model_dump()).kind is kind

    def test_the_judged_groups_size_is_preferred_for_no_number_kinds(self):
        size = group_service.SizeOnlyStats(n=4, es_in=0.9, es_moa=0.86, includes_estimated=False)

        out = advice_out(
            view(
                Advice(AdviceKind.SHOOT_MORE, "Shoot more shots: 4 of 10 so far, 6 left to fire"),
                "",
                size,
            )
        )

        assert out.stats.n == 4 and out.stats.es_in == 0.9

    @pytest.mark.parametrize("kind", [AdviceKind.SHOOT_MORE, AdviceKind.INVALID])
    def test_no_number_advice_without_stats_stays_without_stats(self, kind):
        assert (
            advice_out(view(Advice(kind, "No shots to analyze: add at least 1 shot."))).stats
            is None
        )

    @pytest.mark.parametrize("kind", [AdviceKind.CORRECTION, AdviceKind.NO_ADJUSTMENT])
    def test_correction_kinds_keep_the_mean_poi_with_direction_text(self, kind):
        out = advice_out(
            view(
                Advice(kind, "Step 2: Dial UP 0.3, LEFT 0.2.", CORRECTION, FULL_STATS),
                "0.3 mil low, 0.2 mil right",
            )
        )

        assert isinstance(out.stats, GroupStatsOut)
        assert out.stats.poi_text == "0.3 mil low, 0.2 mil right"
        assert out.stats.mean_dy_mil == -0.3 and out.stats.mean_dx_mil == 0.2
        assert out.correction.model_dump() == {
            "up_mil": 0.3,
            "right_mil": -0.2,
            "text": "Dial UP 0.3, LEFT 0.2",
        }

    @pytest.mark.parametrize(
        "kind", [AdviceKind.READY_FOR_DOT, AdviceKind.TOUCH_UP_OK, AdviceKind.CONFIRM_AGAIN]
    )
    def test_confirm_judgments_carry_the_overlay_size_never_its_centered_poi(self, kind):
        out = advice_out(view(Advice(kind, "judged", CORRECTION, FULL_STATS)))

        assert isinstance(out.stats, SizeOnlyStatsOut)
        assert out.stats.n == 7 and out.stats.es_in == 1.5 and out.stats.includes_estimated is True
        assert out.correction is not None

    def test_poi_text_names_direction_words_and_omits_a_zero_axis(self):
        assert service.poi_text(FULL_STATS) == "0.3 mil low, 0.2 mil right"
        up_only = GroupStats(10, 1.0, 1.0, 0.0, 0.72, 0.0, 0.2, False)
        assert service.poi_text(up_only) == "0.2 mil high"
        centered = GroupStats(10, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, False)
        assert service.poi_text(centered) == "centered on the aim point"

    def test_the_services_own_advice_converts_for_every_kind_the_api_can_produce(
        self, client, world
    ):
        zero = new_zero(client, world)
        seen = set()
        for offsets in (ring(0, 0, n=3), ring(1.08, 0.72, n=10), ring(0, 0, n=10)):
            body = link(client, zero["id"], world.group(offsets).id).json()
            seen.add(body["advice"]["kind"])
            AdviceOut.model_validate(body["advice"])
        assert {"shoot_more", "correction"} <= seen or {"shoot_more", "no_adjustment"} <= seen


# ----------------------------------------------------------------------------- zero check


class TestZeroCheck:
    def test_two_shots_inside_the_dot_pass(self, client, world, conn):
        done = complete_zero(client, world, dot=1.0)
        check = world.group([Offset(0.3, 0.0), Offset(0.0, -0.3)])

        response = client.post(f"/api/zeros/{done['id']}/checks", json={"group_id": check.id})

        assert response.status_code == 201
        body = response.json()
        assert body["result"] == "pass"
        assert body["message"] == ZERO_CHECK_OK_MESSAGE
        assert body["per_shot_inside"] == [True, True]
        assert body["route"] == []
        assert body["zero_id"] == done["id"] and body["group_id"] == check.id
        assert stored(conn, check.id).zero_id == done["id"]
        checks = client.get(f"/api/zeros/{done['id']}").json()["checks"]
        assert [c["result"] for c in checks] == ["pass"]
        assert checks[0]["message"] == ZERO_CHECK_OK_MESSAGE

    def test_the_touching_boundary_passes_and_a_hair_outside_fails(self, client, world):
        done = complete_zero(client, world, dot=1.0)  # limit = 0.5 + 0.132
        touching = world.group([Offset(0.631, 0.0)])
        outside = world.group([Offset(0.64, 0.0)])

        assert (
            client.post(f"/api/zeros/{done['id']}/checks", json={"group_id": touching.id}).json()[
                "result"
            ]
            == "pass"
        )
        assert (
            client.post(f"/api/zeros/{done['id']}/checks", json={"group_id": outside.id}).json()[
                "result"
            ]
            == "possible_lost_zero"
        )

    def test_a_shot_outside_is_a_possible_lost_zero_with_the_mount_then_scope_route(
        self, client, world
    ):
        done = complete_zero(client, world, dot=1.0)
        check = world.group([Offset(0.1, 0.0), Offset(2.0, 0.0)])

        body = client.post(f"/api/zeros/{done['id']}/checks", json={"group_id": check.id}).json()

        assert body["result"] == "possible_lost_zero"
        assert body["message"] == ZERO_CHECK_LOST_ZERO_MESSAGE
        assert body["per_shot_inside"] == [True, False]
        assert "mount" in body["route"][0].lower() and "#1" in body["route"][0]
        assert "scope" in body["route"][1].lower() and "#2" in body["route"][1]
        assert "step 2" in body["route"][2].lower()
        stored_checks = client.get(f"/api/zeros/{done['id']}").json()["checks"]
        assert stored_checks[0]["route"] == body["route"]

    @pytest.mark.parametrize("shots", [3, 5])
    def test_three_or_more_shots_are_refused_and_nothing_changes(self, client, world, conn, shots):
        done = complete_zero(client, world)
        check = world.group(ring(0.0, 0.0, n=shots))

        response = client.post(f"/api/zeros/{done['id']}/checks", json={"group_id": check.id})

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_zero_check_shots"
        assert stored(conn, check.id).zero_id is None
        assert stored(conn, check.id).purpose == "general"
        assert repo_zeroing.list_zero_checks(conn, done["id"]) == []

    def test_a_group_with_no_shots_is_refused(self, client, world, conn):
        done = complete_zero(client, world)
        draft = world.group([], shots_expected=1, finalize=False)

        response = client.post(f"/api/zeros/{done['id']}/checks", json={"group_id": draft.id})

        assert response.status_code == 409 and response.json()["code"] == "group_not_final"

    def test_the_zero_must_be_complete(self, client, world):
        zero = new_zero(client, world)
        check = world.group([Offset(0.1, 0.0)])

        response = client.post(f"/api/zeros/{zero['id']}/checks", json={"group_id": check.id})

        assert response.status_code == 409 and response.json()["code"] == "zero_not_complete"

    def test_the_group_must_be_finalized(self, client, world, conn):
        done = complete_zero(client, world)
        draft = world.group([Offset(0.1, 0.0)], finalize=False)

        response = client.post(f"/api/zeros/{done['id']}/checks", json={"group_id": draft.id})

        assert response.status_code == 409 and response.json()["code"] == "group_not_final"
        assert stored(conn, draft.id).zero_id is None

    def test_a_300_yd_group_is_refused(self, client, world, conn):
        done = complete_zero(client, world)
        far = world.group([Offset(0.1, 0.0)], context=world.s300)

        response = client.post(f"/api/zeros/{done['id']}/checks", json={"group_id": far.id})

        assert response.status_code == 422 and response.json()["code"] == "invalid_distance"
        assert stored(conn, far.id).zero_id is None

    def test_a_group_of_another_rifle_is_refused_and_nothing_is_stored(self, client, world, conn):
        done = complete_zero(client, world)
        foreign = world.group([Offset(0.1, 0.0)], context=world.s100_other, load=world.other_load)

        response = client.post(f"/api/zeros/{done['id']}/checks", json={"group_id": foreign.id})

        assert response.status_code == 409 and response.json()["code"] == "group_not_linkable"
        assert stored(conn, foreign.id).zero_id is None
        assert stored(conn, foreign.id).purpose == "general"
        assert repo_zeroing.list_zero_checks(conn, done["id"]) == []

    def test_a_group_can_only_be_checked_once(self, client, world):
        done = complete_zero(client, world)
        check = world.group([Offset(0.1, 0.0)])
        assert (
            client.post(f"/api/zeros/{done['id']}/checks", json={"group_id": check.id}).status_code
            == 201
        )

        again = client.post(f"/api/zeros/{done['id']}/checks", json={"group_id": check.id})

        assert again.status_code == 409 and again.json()["code"] == "group_already_checked"

    def test_a_zeroing_step_group_cannot_become_a_check(self, client, world):
        zero = new_zero(client, world)
        initial = world.group(ring(1.08, 0.72))
        link(client, zero["id"], initial.id)
        dialed(client, zero["id"], -0.2, -0.3)
        link(client, zero["id"], world.group(ring(0, 0)).id)
        client.post(f"/api/zeros/{zero['id']}/dot", json={"dot_size_in": 0.75})

        response = client.post(f"/api/zeros/{zero['id']}/checks", json={"group_id": initial.id})

        assert response.status_code == 409 and response.json()["code"] == "group_linked_elsewhere"

    def test_unknown_zero_and_group_are_404(self, client, world):
        done = complete_zero(client, world)
        check = world.group([Offset(0.1, 0.0)])

        assert client.post("/api/zeros/9999/checks", json={"group_id": check.id}).status_code == 404
        missing = client.post(f"/api/zeros/{done['id']}/checks", json={"group_id": 9999})
        assert missing.status_code == 404 and missing.json()["code"] == "group_not_found"

    def test_the_dot_comes_from_the_zero_named_in_the_request_not_the_active_zero(
        self, client, world
    ):
        first = complete_zero(client, world, dot=1.0)  # limit 0.632
        second = new_zero(client, world)  # now the active zero, no dot
        shot = world.group([Offset(0.55, 0.0)])

        before = client.post(f"/api/zeros/{first['id']}/checks", json={"group_id": shot.id})
        unfinished = client.post(f"/api/zeros/{second['id']}/checks", json={"group_id": shot.id})

        assert before.status_code == 201 and before.json()["result"] == "pass"
        assert unfinished.status_code == 409 and unfinished.json()["code"] == "zero_not_complete"

    def test_a_small_dot_on_the_named_zero_fails_a_shot_the_active_zeros_dot_would_pass(
        self, client, world
    ):
        first = complete_zero(client, world, dot=0.5)  # limit 0.382
        new_zero(client, world)  # the active zero has no dot at all
        shot = world.group([Offset(0.55, 0.0)])

        response = client.post(f"/api/zeros/{first['id']}/checks", json={"group_id": shot.id})

        assert response.status_code == 201
        assert response.json()["result"] == "possible_lost_zero"
        assert response.json()["per_shot_inside"] == [False]

    def test_after_a_newer_zero_completes_its_own_dot_decides_and_the_old_zero_is_refused(
        self, client, world
    ):
        first = complete_zero(client, world, dot=1.0)
        second = complete_zero(client, world, dot=0.5)  # limit 0.382; supersedes the first
        shot = world.group([Offset(0.55, 0.0)])

        old = client.post(f"/api/zeros/{first['id']}/checks", json={"group_id": shot.id})
        new = client.post(f"/api/zeros/{second['id']}/checks", json={"group_id": shot.id})

        assert old.status_code == 409 and old.json()["code"] == "zero_superseded"
        assert new.status_code == 201 and new.json()["result"] == "possible_lost_zero"

    def test_linking_and_recording_the_check_are_one_transaction(
        self, tolerant, world, conn, monkeypatch
    ):
        done = complete_zero(tolerant, world)
        check = world.group([Offset(0.1, 0.0)])

        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(service.repo_zeroing, "create_zero_check", boom)
        response = tolerant.post(f"/api/zeros/{done['id']}/checks", json={"group_id": check.id})

        assert response.status_code == 500
        row = stored(conn, check.id)
        assert row.zero_id is None and row.purpose == "general"
        assert all(o[0] is not None for o in offsets_of(conn, check.id))


# ----------------------------------------------------------------------------- state machine rules


class TestRules:
    def test_only_100_yd_groups_can_be_linked(self, client, world, conn):
        zero = new_zero(client, world)
        far = world.group(ring(0, 0), context=world.s300)

        response = link(client, zero["id"], far.id)

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_distance"
        assert stored(conn, far.id).zero_id is None and stored(conn, far.id).purpose == "general"

    def test_step_1_exists_only_in_new_setup_mode(self, client, world, conn):
        existing = new_zero(client, world, "existing")
        scratch = new_zero(client, world, "new_setup")
        g1, g2 = world.group([Offset(0.1, 0.1)]), world.group([Offset(0.1, 0.1)])

        assert existing["state"] == "step2_initial" and scratch["state"] == "step1"
        link(client, existing["id"], g1.id)
        link(client, scratch["id"], g2.id)
        assert stored(conn, g1.id).purpose == "zero_step2"
        assert stored(conn, g2.id).purpose == "zero_step1"

    def test_dialed_needs_a_judged_group_first(self, client, world):
        scratch = new_zero(client, world, "new_setup")
        existing = new_zero(client, world, "existing")

        a = dialed(client, scratch["id"], -0.1, 0.0)
        b = dialed(client, existing["id"], -0.1, 0.0)

        assert (a.status_code, a.json()["code"]) == (409, "nothing_to_dial")
        assert (b.status_code, b.json()["code"]) == (409, "initial_group_not_ready")

    @pytest.mark.parametrize("shots", [1, 2, 3])
    def test_a_step_1_group_of_one_to_three_shots_is_linked_and_corrected(
        self, client, world, shots
    ):
        zero = new_zero(client, world, "new_setup")
        group = world.group(ring(0.36, 0.72, n=shots, radius_in=0.1 if shots > 1 else 0.0))

        body = link(client, zero["id"], group.id)

        assert body.status_code == 200
        assert body.json()["advice"]["kind"] == "correction"
        assert body.json()["advice"]["correction"]["text"] == "Dial DOWN 0.2, LEFT 0.1"
        assert dialed(client, zero["id"], -0.2, -0.1).json()["state"] == "step2_initial"

    @pytest.mark.parametrize("shots", [4, 5, 9, 10, 15])
    def test_a_step_1_group_over_three_shots_is_refused_and_nothing_changes(
        self, client, world, conn, shots
    ):
        zero = new_zero(client, world, "new_setup")
        group = world.group(ring(0.36, 0.72, n=shots))

        response = link(client, zero["id"], group.id)

        assert response.status_code == 422
        assert response.json() == {
            "detail": service.STEP1_SHOTS_MESSAGE,
            "code": "invalid_step1_shots",
        }
        assert stored(conn, group.id).zero_id is None
        assert stored(conn, group.id).purpose == "general"
        body = client.get(f"/api/zeros/{zero['id']}").json()
        assert body["state"] == "step1" and body["groups"] == [] and body["advice"] is None
        assert "Dial" not in json.dumps(body) and "Get on paper" not in json.dumps(body)

    def test_the_step_1_refusal_text_points_to_step_2_and_carries_no_correction_text(self):
        message = service.STEP1_SHOTS_MESSAGE

        assert "1 to 3 shots" in message and "Step 2" in message and "10 shots" in message
        AdviceOut(kind=AdviceKind.INVALID, message=message)
        AdviceOut(kind=AdviceKind.SHOOT_MORE, message=message)

    def test_the_same_group_links_as_step_2_in_an_existing_zero(self, client, world, conn):
        zero = new_zero(client, world, "existing")
        group = world.group(ring(0.36, 0.72, n=5))

        body = link(client, zero["id"], group.id)

        assert body.status_code == 200 and body.json()["advice"]["kind"] == "shoot_more"
        assert stored(conn, group.id).purpose == "zero_step2"

    def test_an_empty_step_1_group_can_be_linked_before_its_photo_is_reviewed(self, client, world):
        zero = new_zero(client, world, "new_setup")
        draft = world.group([], shots_expected=3, finalize=False)

        assert link(client, zero["id"], draft.id).status_code == 200
        refused = dialed(client, zero["id"], -0.1, 0.0)
        assert (refused.status_code, refused.json()["code"]) == (409, "nothing_to_dial")

    def test_a_linked_step_1_group_that_grows_past_three_shots_gets_no_correction_or_dial(
        self, client, world, conn
    ):
        zero = new_zero(client, world, "new_setup")
        draft = world.group(ring(0.36, 0.72, n=2, radius_in=0.1), finalize=False, shots_expected=3)
        assert link(client, zero["id"], draft.id).json()["advice"]["kind"] == "correction"
        group = repo_groups.get_group(conn, draft.id)
        aim = PlanePoint(group.aim_x_in, group.aim_y_in)
        grown = [
            Shot(PlanePoint(aim.x_in + 0.1 * i, aim.y_in - 0.05 * i), ShotOrigin.DETECTED)
            for i in range(6)
        ]
        group_service.save_review(conn, draft.id, aim, grown, [], 6)

        body = client.get(f"/api/zeros/{zero['id']}").json()

        assert body["advice"]["kind"] == "shoot_more"
        assert body["advice"]["correction"] is None
        assert set(body["advice"]["stats"]) == SIZE_ONLY_KEYS
        assert "Get on paper" not in json.dumps(body)
        refused = dialed(client, zero["id"], -0.2, -0.1)
        assert (refused.status_code, refused.json()["code"]) == (422, "invalid_step1_shots")
        assert refused.json()["detail"] == service.STEP1_SHOTS_MESSAGE
        assert client.get(f"/api/zeros/{zero['id']}").json()["state"] == "step1"

    def test_dialed_is_recorded_but_never_changes_the_judgment(self, client, world):
        a, b = new_zero(client, world), new_zero(client, world)
        for zero, up in ((a, 9.0), (b, -1.0)):
            link(client, zero["id"], world.group(ring(1.08, 0.72)).id)
            dialed(client, zero["id"], up, 0.0)
        confirm = ring(0.0, 0.0)
        ra = link(client, a["id"], world.group(confirm).id).json()
        rb = link(client, b["id"], world.group(confirm).id).json()

        assert ra["advice"] == rb["advice"]
        assert (ra["dialed_up_mil"], rb["dialed_up_mil"]) == (9.0, -1.0)

    def test_dialed_is_validated_in_whole_tenths(self, client, world):
        zero = new_zero(client, world)
        response = dialed(client, zero["id"], 0.25, 0.0)
        assert response.status_code == 422 and response.json()["code"] == "validation_error"

    def test_the_dot_waits_for_a_ready_confirm_and_must_be_positive(self, client, world):
        zero = new_zero(client, world)
        early = client.post(f"/api/zeros/{zero['id']}/dot", json={"dot_size_in": 0.75})
        bad = client.post(f"/api/zeros/{zero['id']}/dot", json={"dot_size_in": 0})
        negative = client.post(f"/api/zeros/{zero['id']}/dot", json={"dot_size_in": -1})

        assert (early.status_code, early.json()["code"]) == (409, "zero_state")
        assert bad.status_code == 422 and negative.status_code == 422

    def test_the_dot_can_override_the_suggestion_and_needs_final_groups(self, client, world, conn):
        zero = new_zero(client, world)
        link(client, zero["id"], world.group(ring(1.08, 0.72)).id)
        dialed(client, zero["id"], -0.2, -0.3)
        draft = world.group(ring(0, 0), finalize=False, shots_expected=10)
        ready = link(client, zero["id"], draft.id).json()
        assert ready["state"] == "step3_dot" and ready["suggested_dot_in"] == 0.5

        refused = client.post(f"/api/zeros/{zero['id']}/dot", json={"dot_size_in": 1.5})
        assert (refused.status_code, refused.json()["code"]) == (409, "groups_not_final")

        group_service.finalize(conn, draft.id)
        done = client.post(f"/api/zeros/{zero['id']}/dot", json={"dot_size_in": 1.5}).json()
        assert done["state"] == "complete" and done["dot_size_in"] == 1.5

    def test_a_complete_zero_refuses_further_steps(self, client, world):
        done = complete_zero(client, world)
        group = world.group(ring(0, 0))

        for response in (
            link(client, done["id"], group.id),
            client.delete(f"/api/zeros/{done['id']}/groups/{group.id}"),
            dialed(client, done["id"], 0.1, 0.0),
            client.post(f"/api/zeros/{done['id']}/dot", json={"dot_size_in": 1.0}),
        ):
            assert response.status_code == 409 and response.json()["code"] == "zero_state"

    def test_a_superseded_zero_is_read_only(self, client, world):
        first = complete_zero(client, world)
        complete_zero(client, world)

        assert client.get(f"/api/zeros/{first['id']}").json()["state"] == "superseded"
        response = link(client, first["id"], world.group(ring(0, 0)).id)
        assert response.status_code == 409 and response.json()["code"] == "zero_state"

    def test_relinking_is_a_no_op_and_a_group_cannot_join_two_zeros(self, client, world):
        a, b = new_zero(client, world), new_zero(client, world)
        group = world.group(ring(1.08, 0.72))
        first = link(client, a["id"], group.id).json()

        again = link(client, a["id"], group.id)
        elsewhere = link(client, b["id"], group.id)

        assert again.status_code == 200 and again.json()["groups"] == first["groups"]
        assert elsewhere.status_code == 409 and elsewhere.json()["code"] == "group_linked_elsewhere"

    def test_a_later_initial_group_restarts_the_confirm_stage(self, client, world, conn):
        zero = new_zero(client, world)
        link(client, zero["id"], world.group(ring(1.08, 0.72)).id)
        dialed(client, zero["id"], -0.2, -0.3)
        assert link(client, zero["id"], world.group(ring(0, 0)).id).json()["state"] == "step3_dot"
        redo = world.group(ring(1.08, 0.72))
        repo_groups.update_group(conn, redo.id, purpose="zero_step2", zero_id=zero["id"])
        conn.commit()

        body = client.get(f"/api/zeros/{zero['id']}").json()

        assert body["state"] == "step2_confirm"
        assert body["advice"]["kind"] == "shoot_more"
        assert body["advice"]["message"] == "Shoot more shots: 0 of 10 so far, 10 left to fire"
        assert body["zero_es_in"] is None

    def test_a_zero_check_group_is_not_a_step_group(self, client, world):
        zero = new_zero(client, world)
        check = world.group([Offset(0.1, 0.0)], purpose="zero_check")

        response = link(client, zero["id"], check.id)

        assert response.status_code == 409 and response.json()["code"] == "group_is_zero_check"

    def test_a_group_of_another_rifle_or_lot_cannot_be_linked(self, client, world, conn):
        zero = new_zero(client, world, lot_code="LOT-A")
        foreign = world.group(ring(0, 0), context=world.s100_other, load=world.other_load)
        other_lot = world.group(ring(0, 0), lot_code="LOT-B")
        same_lot = world.group(ring(0, 0), lot_code="LOT-A")

        for group in (foreign, other_lot):
            response = link(client, zero["id"], group.id)
            assert response.status_code == 409 and response.json()["code"] == "group_not_linkable"
            assert stored(conn, group.id).zero_id is None
        assert link(client, zero["id"], same_lot.id).status_code == 200

    def test_unknown_ids_are_404(self, client, world):
        zero = new_zero(client, world)
        for response in (
            client.get("/api/zeros/9999"),
            link(client, 9999, 1),
            link(client, zero["id"], 9999),
            dialed(client, 9999),
            client.post("/api/zeros/9999/dot", json={"dot_size_in": 1}),
            client.delete(f"/api/zeros/{zero['id']}/groups/9999"),
        ):
            assert response.status_code == 404
        assert client.get("/api/zeros/9999").json()["code"] == "zero_not_found"


class TestCreate:
    def test_the_lot_is_created_and_tied_to_the_zero(self, client, world, conn):
        zero = new_zero(client, world, lot_code="LOT-Z")

        lots = repo_rifles.list_lots(conn, world.load.id)
        assert [lot.lot_code for lot in lots] == ["LOT-Z"]
        assert zero["lot_id"] == lots[0].id
        assert new_zero(client, world)["lot_id"] is None

    def test_unknown_rifle_load_and_foreign_load_are_refused(self, client, world):
        def create(**kw):
            body = {"rifle_id": world.rifle.id, "load_id": world.load.id, "mode": "existing", **kw}
            return client.post("/api/zeros", json=body)

        assert (create(rifle_id=9999).status_code, create(rifle_id=9999).json()["code"]) == (
            404,
            "rifle_not_found",
        )
        assert (create(load_id=9999).status_code, create(load_id=9999).json()["code"]) == (
            404,
            "load_not_found",
        )
        mismatch = create(load_id=world.other_load.id)
        assert (mismatch.status_code, mismatch.json()["code"]) == (409, "zero_target_mismatch")
        assert create(mode="bogus").status_code == 422
        assert create(lot_code="").status_code == 422

    def test_nothing_is_stored_when_creation_fails(self, client, world, conn):
        client.post(
            "/api/zeros",
            json={"rifle_id": world.rifle.id, "load_id": world.other_load.id, "mode": "new_setup"},
        )

        assert conn.execute("SELECT COUNT(*) FROM zeros").fetchone()[0] == 0


# ----------------------------------------------------------------------------- abandoned confirm groups (B-26)


class TestAbandonedConfirmGroups:
    def test_detaching_a_short_confirm_group_lets_a_fresh_ten_be_judged_normally(
        self, client, world, conn
    ):
        zero = new_zero(client, world)
        link(client, zero["id"], world.group(ring(1.08, 0.72)).id)
        dialed(client, zero["id"], -0.2, -0.3)
        abandoned = world.group(ring(0.5, 0.5, n=4), finalize=False, shots_expected=10)
        assert link(client, zero["id"], abandoned.id).json()["advice"]["kind"] == "shoot_more"
        assert all(set(o) == {None} for o in offsets_of(conn, abandoned.id))

        fresh = world.group(ring(0.0, 0.0))
        blocked = link(client, zero["id"], fresh.id).json()
        assert blocked["advice"]["kind"] == "invalid"
        assert blocked["advice"]["message"].startswith("Confirm group 1 has 4 of 10 shots")
        assert blocked["advice"]["correction"] is None
        assert blocked["state"] == "step2_confirm"

        detached = client.delete(f"/api/zeros/{zero['id']}/groups/{abandoned.id}")

        assert detached.status_code == 200
        body = detached.json()
        assert [g["group_id"] for g in body["groups"]] == [body["groups"][0]["group_id"], fresh.id]
        assert abandoned.id not in {g["group_id"] for g in body["groups"]}
        assert body["advice"]["kind"] == "ready_for_dot"
        assert body["state"] == "step3_dot"
        row = stored(conn, abandoned.id)
        assert row.zero_id is None and row.purpose == "general"
        assert all(o[0] is not None for o in offsets_of(conn, abandoned.id))

    def test_detaching_the_judged_group_returns_the_zero_to_the_confirm_stage(self, client, world):
        zero = new_zero(client, world)
        link(client, zero["id"], world.group(ring(1.08, 0.72)).id)
        dialed(client, zero["id"], -0.2, -0.3)
        confirm = world.group(ring(0, 0))
        assert link(client, zero["id"], confirm.id).json()["state"] == "step3_dot"

        body = client.delete(f"/api/zeros/{zero['id']}/groups/{confirm.id}").json()

        assert body["state"] == "step2_confirm"
        assert body["zero_es_in"] is None and body["zero_shot_count"] is None
        assert body["suggested_dot_in"] is None
        assert body["advice"]["kind"] == "shoot_more"

    def test_only_linked_step_groups_can_be_detached(self, client, world):
        zero, other = new_zero(client, world), new_zero(client, world)
        group = world.group(ring(0, 0))
        link(client, other["id"], group.id)
        loose = world.group(ring(0, 0))

        for gid in (group.id, loose.id):
            response = client.delete(f"/api/zeros/{zero['id']}/groups/{gid}")
            assert response.status_code == 409 and response.json()["code"] == "group_not_linked"


# ----------------------------------------------------------------------------- state follows the groups


class TestStateFollowsTheGroups:
    def test_a_stale_stored_state_is_corrected_on_read(self, client, world, conn):
        zero = new_zero(client, world)
        link(client, zero["id"], world.group(ring(1.08, 0.72)).id)
        dialed(client, zero["id"], -0.2, -0.3)
        draft = world.group(ring(0, 0, n=4), finalize=False, shots_expected=10)
        assert link(client, zero["id"], draft.id).json()["state"] == "step2_confirm"

        stored_aim = group_service.load_group(conn, draft.id).group
        plane = [(stored_aim.aim_x_in + o.dx_in, stored_aim.aim_y_in - o.dy_in) for o in ring(0, 0)]
        group_service.save_review(
            conn,
            draft.id,
            PlanePoint(stored_aim.aim_x_in, stored_aim.aim_y_in),
            [Shot(PlanePoint(x, y), ShotOrigin.DETECTED) for x, y in plane],
            [],
            10,
        )

        ready = client.get(f"/api/zeros/{zero['id']}").json()
        assert ready["state"] == "step3_dot"
        assert ready["advice"]["kind"] == "ready_for_dot"
        assert ready["zero_shot_count"] == 20 and ready["suggested_dot_in"] is not None

    def test_the_judged_ready_state_reverts_when_the_group_changes_back(self, client, world, conn):
        zero = new_zero(client, world)
        link(client, zero["id"], world.group(ring(1.08, 0.72)).id)
        dialed(client, zero["id"], -0.2, -0.3)
        draft = world.group(ring(0, 0), finalize=False, shots_expected=10)
        assert link(client, zero["id"], draft.id).json()["state"] == "step3_dot"
        for shot in repo_groups.list_shots(conn, draft.id)[:5]:
            conn.execute("DELETE FROM shots WHERE id = ?", (shot.id,))
        conn.commit()

        body = client.get(f"/api/zeros/{zero['id']}").json()

        assert body["state"] == "step2_confirm"
        assert body["advice"]["kind"] == "shoot_more" and body["advice"]["correction"] is None
        assert body["zero_es_in"] is None


# ----------------------------------------------------------------------------- errors and transport


class TestErrors:
    def test_a_locked_database_is_503_busy_not_500(self, tolerant, world, monkeypatch):
        zero = new_zero(tolerant, world)

        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(service, "get_zero", locked)
        response = tolerant.get(f"/api/zeros/{zero['id']}")

        assert response.status_code == 503
        assert response.json()["code"] == "busy"

    def test_other_database_errors_are_not_reported_as_busy(self, tolerant, world, monkeypatch):
        zero = new_zero(tolerant, world)

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("no such table: zeros")

        monkeypatch.setattr(service, "get_zero", broken)
        response = tolerant.get(f"/api/zeros/{zero['id']}")

        assert response.status_code == 500 and response.json()["code"] == "internal_error"

    def test_service_error_codes_map_to_statuses(self):
        from rga.web.routes.zeroing import _status_for

        assert _status_for("zero_not_found") == 404
        assert _status_for("group_not_found") == 404
        assert _status_for("invalid_distance") == 422
        assert _status_for("zero_state") == 409
        assert _status_for("group_not_linkable") == 409

    def test_cross_origin_writes_are_refused_before_the_service(self, client, world, conn):
        zero = new_zero(client, world)
        for method, path, body in (
            (
                "post",
                "/api/zeros",
                {"rifle_id": world.rifle.id, "load_id": world.load.id, "mode": "existing"},
            ),
            ("post", f"/api/zeros/{zero['id']}/groups", {"group_id": 1}),
            ("post", f"/api/zeros/{zero['id']}/dialed", {"up_mil": 0.1, "right_mil": 0}),
            ("post", f"/api/zeros/{zero['id']}/dot", {"dot_size_in": 1}),
            ("post", f"/api/zeros/{zero['id']}/checks", {"group_id": 1}),
        ):
            response = getattr(client, method)(
                path, json=body, headers={"Origin": "http://evil.example"}
            )
            assert response.status_code == 403
        detach = client.delete(
            f"/api/zeros/{zero['id']}/groups/1", headers={"Origin": "http://evil.example"}
        )
        assert detach.status_code == 403
        assert conn.execute("SELECT COUNT(*) FROM zeros").fetchone()[0] == 1

    def test_requests_with_extra_fields_or_bad_types_are_422(self, client, world):
        zero = new_zero(client, world)
        for path, body in (
            ("/api/zeros", {"rifle_id": "x", "load_id": 1, "mode": "existing"}),
            ("/api/zeros", {"rifle_id": 1, "load_id": 1, "mode": "existing", "extra": 1}),
            (f"/api/zeros/{zero['id']}/groups", {"group_id": 0}),
            (f"/api/zeros/{zero['id']}/dot", {"dot_size_in": "big"}),
            (f"/api/zeros/{zero['id']}/checks", {}),
        ):
            assert client.post(path, json=body).status_code == 422


# ----------------------------------------------------------------------------- page and script


class TestPage:
    def test_the_page_renders_with_an_external_module_script_and_security_headers(
        self, client, world
    ):
        zero = new_zero(client, world)

        response = client.get(f"/zeroing/{zero['id']}")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        html = response.text
        assert f'data-zero-id="{zero["id"]}"' in html
        assert '<script type="module" src="/static/js/zeroing.js"></script>' in html
        assert not re.search(r"<script(?![^>]*\bsrc=)", html)
        assert "style=" not in html and not re.search(r"\son[a-z]+=", html)
        assert "<form" not in html
        assert response.headers["x-frame-options"] == "DENY"
        assert "script-src 'self'" in response.headers["content-security-policy"]

    def test_an_unknown_zero_is_404_with_the_error_model(self, client):
        response = client.get("/zeroing/9999")

        assert response.status_code == 404
        assert response.json() == {"detail": "zero 9999 does not exist", "code": "zero_not_found"}

    def test_the_script_is_served(self, client):
        response = client.get("/static/js/zeroing.js")

        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]


class TestScript:
    SOURCE = (ROOT / "src/rga/web/static/js/zeroing.js").read_text(encoding="utf-8")

    def test_it_uses_only_the_api_wrapper_and_text_content(self):
        assert 'from "/static/js/api.js"' in self.SOURCE
        assert not re.search(r"\bfetch\s*\(", self.SOURCE)
        assert "innerHTML" not in self.SOURCE and "outerHTML" not in self.SOURCE
        assert "insertAdjacentHTML" not in self.SOURCE and "document.write" not in self.SOURCE

    def test_no_number_advice_is_never_rendered_with_a_correction(self):
        assert 'const NO_NUMBER = new Set(["shoot_more", "invalid"])' in self.SOURCE
        assert "!NO_NUMBER.has(advice.kind) && advice.correction !== null" in self.SOURCE
        assert "!NO_NUMBER.has(advice.kind) && stats && stats.poi_text" in self.SOURCE

    def test_every_api_path_it_calls_exists(self, app):
        registered = {
            (method.upper(), path)
            for path, item in app.openapi()["paths"].items()
            for method in item
        }
        calls = re.findall(
            r'api(?:Get|Send)\((?:"(GET|POST|PUT|PATCH|DELETE)",\s*)?[`"](/api/[^`"?]*)',
            self.SOURCE,
        )
        assert calls
        for method, raw in calls:
            path = re.sub(r"\$\{[^}]*\}", "{id}", raw)
            if path.startswith("/api/zeros"):
                real = path.replace("{id}", "{zero_id}", 1).replace("{id}", "{group_id}", 1)
                assert (method or "GET", real) in registered, (method, path)
            else:
                assert (method or "GET", path) in ROUTE_SCHEMAS, (method, path)

    def test_a_check_group_is_created_only_through_the_check_groups_route(self):
        creating = self.SOURCE.split("async function createGroup(checking)")[1].split(
            "async function onCreateGroup"
        )[0]
        checking_branch = creating.split("if (checking) {")[1].split("return { group };")[0]
        assert "/check-groups" in checking_branch
        assert '"/api/groups"' not in checking_branch
        assert "distance_yd" not in checking_branch and "session_id" not in checking_branch
        assert 'purpose: "zero_check"' not in self.SOURCE

    def test_it_parses_as_an_es_module(self):
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed: the script syntax check was not run")
        result = subprocess.run(
            [node, "--check", "--input-type=module"],
            input=self.SOURCE,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_the_detach_route_uses_the_registry_response_model():
    from rga.web.routes import zeroing

    detach = [
        r
        for r in zeroing.router.routes
        if r.path == "/api/zeros/{zero_id}/groups/{group_id}" and "DELETE" in r.methods
    ]
    assert len(detach) == 1
    assert (
        detach[0].response_model
        is ROUTE_SCHEMAS[("DELETE", "/api/zeros/{id}/groups/{gid}")].response
    )


def test_the_module_registers_exactly_the_t37_routes():
    from rga.web.routes import zeroing

    routes = {(method, route.path) for route in zeroing.router.routes for method in route.methods}
    assert routes == {
        ("POST", "/api/zeros"),
        ("GET", "/api/zeros/{zero_id}"),
        ("POST", "/api/zeros/{zero_id}/groups"),
        ("DELETE", "/api/zeros/{zero_id}/groups/{group_id}"),
        ("POST", "/api/zeros/{zero_id}/dialed"),
        ("POST", "/api/zeros/{zero_id}/dot"),
        ("POST", "/api/zeros/{zero_id}/checks"),
        ("POST", "/api/zeros/{zero_id}/check-groups"),
        ("GET", "/zeroing/{zero_id}"),
    }


# ----------------------------------------------------------------------------- review follow-ups


class TestOrderAndRecovery:
    def test_groups_must_be_linked_in_the_order_they_were_shot(self, client, world, conn):
        zero = new_zero(client, world)
        older = world.group(ring(1.08, 0.72))
        newer = world.group(ring(1.08, 0.72))
        assert link(client, zero["id"], newer.id).status_code == 200

        late = link(client, zero["id"], older.id)

        assert late.status_code == 409 and late.json()["code"] == "group_out_of_order"
        assert stored(conn, older.id).zero_id is None
        assert stored(conn, older.id).purpose == "general"
        assert link(client, zero["id"], newer.id).status_code == 200

    def test_a_detached_older_group_cannot_come_back_behind_a_newer_one(self, client, world):
        zero = new_zero(client, world)
        first = world.group(ring(1.08, 0.72, n=4))
        second = world.group(ring(1.08, 0.72))
        link(client, zero["id"], first.id)
        link(client, zero["id"], second.id)
        client.delete(f"/api/zeros/{zero['id']}/groups/{first.id}")

        response = link(client, zero["id"], first.id)

        assert response.status_code == 409 and response.json()["code"] == "group_out_of_order"

    def test_detaching_the_initial_group_in_the_confirm_stage_returns_to_step_2(
        self, client, world
    ):
        zero = new_zero(client, world)
        initial = world.group(ring(1.08, 0.72))
        link(client, zero["id"], initial.id)
        dialed(client, zero["id"], -0.2, -0.3)
        assert link(client, zero["id"], world.group(ring(0, 0)).id).json()["state"] == "step3_dot"

        body = client.delete(f"/api/zeros/{zero['id']}/groups/{initial.id}").json()

        assert body["state"] == "step2_initial"
        assert body["zero_es_in"] is None and body["zero_shot_count"] is None
        assert body["suggested_dot_in"] is None
        redo = link(client, zero["id"], world.group(ring(1.08, 0.72)).id).json()
        assert redo["advice"]["kind"] == "correction"
        assert dialed(client, zero["id"], -0.2, -0.3).json()["state"] == "step2_confirm"

    def test_detaching_a_confirm_group_keeps_the_initial_group_and_the_confirm_stage(
        self, client, world
    ):
        zero = new_zero(client, world)
        link(client, zero["id"], world.group(ring(1.08, 0.72)).id)
        dialed(client, zero["id"], -0.2, -0.3)
        confirm = world.group(ring(0, 0))
        link(client, zero["id"], confirm.id)

        body = client.delete(f"/api/zeros/{zero['id']}/groups/{confirm.id}").json()

        assert body["state"] == "step2_confirm"

    def test_completing_an_old_zero_leaves_a_newer_one_in_progress_alone(self, client, world):
        older = new_zero(client, world)
        link(client, older["id"], world.group(ring(1.08, 0.72)).id)
        dialed(client, older["id"], -0.2, -0.3)
        link(client, older["id"], world.group(ring(0, 0)).id)
        newer = new_zero(client, world)
        redo = world.group(ring(1.08, 0.72))
        link(client, newer["id"], redo.id)

        assert (
            client.post(f"/api/zeros/{older['id']}/dot", json={"dot_size_in": 0.75}).status_code
            == 200
        )

        assert client.get(f"/api/zeros/{newer['id']}").json()["state"] == "step2_initial"
        assert client.get(f"/api/zeros/{older['id']}").json()["state"] == "complete"
        assert dialed(client, newer["id"], -0.2, -0.3).json()["state"] == "step2_confirm"

    def test_completing_a_zero_supersedes_older_ones_even_when_a_newer_exists(self, client, world):
        oldest = complete_zero(client, world)
        middle = new_zero(client, world)
        link(client, middle["id"], world.group(ring(1.08, 0.72)).id)
        dialed(client, middle["id"], -0.2, -0.3)
        link(client, middle["id"], world.group(ring(0, 0)).id)
        new_zero(client, world)

        client.post(f"/api/zeros/{middle['id']}/dot", json={"dot_size_in": 1.0})

        assert client.get(f"/api/zeros/{oldest['id']}").json()["state"] == "superseded"


class TestIdsAndStoredChecks:
    def test_ids_beyond_the_database_range_are_client_errors_not_500s(self, tolerant, world):
        zero = new_zero(tolerant, world)
        huge = 10**30

        assert tolerant.get(f"/api/zeros/{huge}").status_code == 422
        assert tolerant.get("/api/zeros/0").status_code == 422
        assert tolerant.delete(f"/api/zeros/{zero['id']}/groups/{huge}").status_code == 422
        assert tolerant.get(f"/zeroing/{huge}").status_code == 422
        bodies = [
            ("POST", f"/api/zeros/{zero['id']}/groups", {"group_id": huge}),
            (
                "POST",
                "/api/zeros",
                {"rifle_id": huge, "load_id": world.load.id, "mode": "existing"},
            ),
            (
                "POST",
                "/api/zeros",
                {"rifle_id": world.rifle.id, "load_id": huge, "mode": "existing"},
            ),
            ("POST", f"/api/zeros/{zero['id']}/checks", {"group_id": huge}),
        ]
        for method, url, body in bodies:
            response = tolerant.request(method, url, json=body)
            assert (response.status_code, response.json()["code"]) == (422, "validation_error")

    def test_the_service_itself_treats_huge_ids_as_not_found(self, conn):
        for call in (
            lambda: service.get_zero(conn, 10**30),
            lambda: service.link_group(conn, 10**30, 1),
            lambda: service.record_dialed(conn, 10**30, 0.0, 0.0),
        ):
            with pytest.raises(service.ZeroingError) as caught:
                call()
            assert caught.value.code == "zero_not_found"

    def test_a_stored_result_decides_the_check_message_and_route(self):
        from rga.db.rows import ZeroCheckRow
        from rga.domain.types import ZeroCheckResult
        from rga.web.routes.zeroing import check_out

        def row(result):
            return ZeroCheckRow(1, 2, 3, result, "2026-10-01T00:00:00Z")

        lost_unavailable = check_out(service.CheckView(row("possible_lost_zero"), None))
        assert lost_unavailable.message == ZERO_CHECK_LOST_ZERO_MESSAGE
        assert lost_unavailable.per_shot_inside == []
        assert lost_unavailable.route == list(service.LOST_ZERO_ROUTE)

        passed_unavailable = check_out(service.CheckView(row("pass"), None))
        assert (
            passed_unavailable.message == ZERO_CHECK_OK_MESSAGE and passed_unavailable.route == []
        )

        agree = check_out(service.CheckView(row("pass"), ZeroCheckResult(True, (True, True), "x")))
        assert agree.per_shot_inside == [True, True] and agree.message == ZERO_CHECK_OK_MESSAGE

        contradict = check_out(
            service.CheckView(row("pass"), ZeroCheckResult(False, (True, False), "outside"))
        )
        assert contradict.per_shot_inside == [] and contradict.message == ZERO_CHECK_OK_MESSAGE
        assert contradict.route == []


# ----------------------------------------------------------------------------- 100 yd rule everywhere (B-44)


DISTANCE_TEXT = service.DISTANCE_MESSAGE


def tamper_to_300(conn, group_id: int) -> None:
    """Move a linked group to 300 yd behind the application's back."""
    with conn:
        conn.execute("UPDATE groups SET distance_yd = 300 WHERE id = ?", (group_id,))


class TestZeroingGroupsMustStayAt100Yd:
    def test_patching_the_group_distance_after_linking_is_refused(self, client, world, conn):
        zero = new_zero(client, world)
        group = world.group(ring(1.08, 0.72))
        assert link(client, zero["id"], group.id).status_code == 200

        response = client.patch(f"/api/groups/{group.id}", json={"distance_yd": 300})

        assert (response.status_code, response.json()["code"]) == (409, "group_linked_to_zero")
        assert stored(conn, group.id).distance_yd == 100
        assert client.patch(f"/api/groups/{group.id}", json={"notes": "n"}).status_code == 200
        same = client.patch(f"/api/groups/{group.id}", json={"distance_yd": 100})
        assert same.status_code == 200

    def test_the_distance_can_change_again_after_the_group_is_detached(self, client, world, conn):
        zero = new_zero(client, world)
        group = world.group(ring(1.08, 0.72))
        link(client, zero["id"], group.id)
        assert client.delete(f"/api/zeros/{zero['id']}/groups/{group.id}").status_code == 200

        response = client.patch(f"/api/groups/{group.id}", json={"distance_yd": 300})

        assert response.status_code == 200
        assert stored(conn, group.id).distance_yd == 300

    def test_an_unlinked_zeroing_purpose_group_does_not_block_the_distance(
        self, client, world, conn
    ):
        group = world.group(ring(0, 0), purpose="zero_step2")

        response = client.patch(f"/api/groups/{group.id}", json={"distance_yd": 200})

        assert response.status_code == 200

    def test_a_group_without_a_zero_changes_distance_freely(self, client, world, conn):
        group = world.group(ring(0, 0))

        response = client.patch(f"/api/groups/{group.id}", json={"distance_yd": 200})

        assert response.status_code == 200

    def test_a_linked_group_does_not_move_its_sibling_groups_distance(self, client, world, conn):
        zero = new_zero(client, world)
        linked = world.group(ring(1.08, 0.72))
        sibling = world.group(ring(0, 0))
        link(client, zero["id"], linked.id)

        response = client.patch(f"/api/groups/{sibling.id}", json={"distance_yd": 300})

        assert response.status_code == 200
        assert stored(conn, linked.id).distance_yd == 100

    def test_a_zero_group_cannot_be_created_at_300_yd(self, client, world, conn):
        zero = new_zero(client, world)
        body = {
            "load_id": world.load.id,
            "lot_code": "LOT-1",
            "shot_date": "2026-09-30",
            "distance_yd": 300,
            "position": "bench",
            "suppressor": False,
            "purpose": "zero_step2",
            "zero_id": zero["id"],
            "shots_expected": 10,
        }

        response = client.post("/api/groups", json=body)

        assert (response.status_code, response.json()["code"]) == (422, "invalid_purpose")
        assert conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0

    def test_a_300_yd_group_cannot_be_patched_into_a_zero(self, client, world, conn):
        zero = new_zero(client, world)
        group = world.group(ring(0, 0), context=world.s300)

        response = client.patch(
            f"/api/groups/{group.id}", json={"purpose": "zero_step2", "zero_id": zero["id"]}
        )

        assert (response.status_code, response.json()["code"]) == (422, "invalid_purpose")
        row = stored(conn, group.id)
        assert (row.purpose, row.zero_id) == ("general", None)

    def test_a_tampered_linked_group_is_judged_invalid_with_no_numbers(self, client, world, conn):
        zero = new_zero(client, world)
        group = world.group(ring(1.08, 0.72))
        link(client, zero["id"], group.id)
        tamper_to_300(conn, group.id)

        view = client.get(f"/api/zeros/{zero['id']}").json()

        assert view["advice"]["kind"] == "invalid"
        assert view["advice"]["message"] == DISTANCE_TEXT
        assert view["advice"]["correction"] is None and view["advice"]["stats"] is None
        assert not re.search(r"\d+\.\d", view["advice"]["message"])

    def test_a_tampered_group_is_refused_by_dialed_dot_and_check(self, client, world, conn):
        zero = new_zero(client, world)
        group = world.group(ring(1.08, 0.72))
        link(client, zero["id"], group.id)
        tamper_to_300(conn, group.id)

        refused = dialed(client, zero["id"], -0.2, -0.3)

        assert (refused.status_code, refused.json()["code"]) == (422, "invalid_distance")
        assert refused.json()["detail"] == DISTANCE_TEXT
        assert dialed_value(client, zero["id"]) == (None, None)

    def test_a_tampered_confirm_group_blocks_the_dot_and_complete_zero(self, client, world, conn):
        zero = new_zero(client, world)
        link(client, zero["id"], world.group(ring(1.08, 0.72)).id)
        dialed(client, zero["id"], -0.2, -0.3)
        confirm = world.group(ring(0, 0))
        ready = link(client, zero["id"], confirm.id)
        assert ready.json()["state"] == "step3_dot"
        tamper_to_300(conn, confirm.id)

        shown = client.get(f"/api/zeros/{zero['id']}").json()
        dot = client.post(f"/api/zeros/{zero['id']}/dot", json={"dot_size_in": 0.75})

        assert shown["advice"]["kind"] == "invalid" and shown["advice"]["message"] == DISTANCE_TEXT
        assert (dot.status_code, dot.json()["code"]) == (422, "invalid_distance")
        assert client.get(f"/api/zeros/{zero['id']}").json()["state"] != "complete"

    def test_a_tampered_check_group_is_refused(self, client, world, conn):
        done = complete_zero(client, world, dot=1.0)
        check = world.group([Offset(0.3, 0.0), Offset(0.0, -0.3)])
        tamper_to_300(conn, check.id)

        response = client.post(f"/api/zeros/{done['id']}/checks", json={"group_id": check.id})

        assert (response.status_code, response.json()["code"]) == (422, "invalid_distance")
        assert client.get(f"/api/zeros/{done['id']}").json()["checks"] == []


class TestACompleteZeroWhoseGroupMovedCannotBeChecked:
    def test_a_check_against_a_zero_whose_confirm_group_left_100_yd_is_refused(
        self, client, world, conn
    ):
        done = complete_zero(client, world, dot=1.0)
        confirm = next(g for g in done["groups"] if g["purpose"] == "zero_confirm")
        tamper_to_300(conn, confirm["group_id"])
        check = world.group([Offset(0.3, 0.0), Offset(0.0, -0.3)])

        response = client.post(f"/api/zeros/{done['id']}/checks", json={"group_id": check.id})

        assert (response.status_code, response.json()["code"]) == (422, "invalid_distance")
        assert stored(conn, check.id).zero_id is None


class TestZeroPurposesAreSetOnlyByTheZeroRoutes:
    def test_review_20_exploit_a_two_shot_check_cannot_be_flipped_to_general(
        self, client, world, conn
    ):
        done = complete_zero(client, world, dot=1.0)
        check = world.group([Offset(0.3, 0.0), Offset(0.0, -0.3)])
        assert (
            client.post(f"/api/zeros/{done['id']}/checks", json={"group_id": check.id}).status_code
            == 201
        )

        flip = client.patch(f"/api/groups/{check.id}", json={"purpose": "general"})

        assert (flip.status_code, flip.json()["code"]) == (409, "group_linked_to_zero")
        row = stored(conn, check.id)
        assert (row.purpose, row.zero_id) == ("zero_check", done["id"])
        shown = client.get(f"/api/groups/{check.id}").json()
        assert shown["masked"] is True and shown["aim"] is None

    def test_a_step_group_linked_to_a_zero_keeps_its_purpose(self, client, world, conn):
        zero = new_zero(client, world)
        group = world.group(ring(1.08, 0.72))
        link(client, zero["id"], group.id)

        flip = client.patch(f"/api/groups/{group.id}", json={"purpose": "general"})

        assert (flip.status_code, flip.json()["code"]) == (409, "group_linked_to_zero")
        assert stored(conn, group.id).purpose == "zero_step2"

    def test_detaching_makes_the_purpose_editable_again(self, client, world, conn):
        zero = new_zero(client, world)
        group = world.group(ring(1.08, 0.72))
        link(client, zero["id"], group.id)
        client.delete(f"/api/zeros/{zero['id']}/groups/{group.id}")

        response = client.patch(f"/api/groups/{group.id}", json={"purpose": "general"})

        assert response.status_code == 200 and response.json()["purpose"] == "general"

    def test_group_create_cannot_borrow_the_zero_check_purpose(self, client, world, conn):
        done = complete_zero(client, world, dot=1.0)
        body = {
            "load_id": world.load.id,
            "lot_code": "LOT-1",
            "purpose": "zero_check",
            "zero_id": done["id"],
            "shots_expected": 2,
            "shot_date": "2026-09-30",
            "distance_yd": 100,
            "position": "bench",
            "suppressor": False,
        }

        response = client.post("/api/groups", json=body)

        assert (response.status_code, response.json()["code"]) == (422, "invalid_purpose")
        assert [g.purpose for g in repo_groups.list_groups(conn)].count("zero_check") == 0


# ----------------------------------------------------------------------------- check groups masked from creation (B-50)


def check_group_body(world, **overrides) -> dict:
    return {
        "lot_code": "LOT-1",
        "shots_expected": 2,
        "notes": "",
        "shot_date": "2026-09-30",
        "position": "bench",
        "suppressor": False,
        **overrides,
    }


def make_check_group(client, world, zero_id, **overrides) -> dict:
    response = client.post(
        f"/api/zeros/{zero_id}/check-groups", json=check_group_body(world, **overrides)
    )
    assert response.status_code == 201, response.text
    return response.json()


def put_shots(conn, group_id, offsets):
    """Store `offsets` (inches from the aim at the origin) like a finished review would."""
    with conn:
        repo_groups.update_group(conn, group_id, aim_x_in=0.0, aim_y_in=0.0)
        repo_groups.replace_shots(
            conn,
            group_id,
            [repo_groups.NewShot(x_in=o.dx_in, y_in=o.dy_in, origin="detected") for o in offsets],
        )
        group_service.recompute_offsets(conn, group_id)


class TestACheckGroupIsMaskedFromCreation:
    def test_the_new_group_is_a_linked_zero_check_group_with_no_point_of_impact(
        self, client, world, conn
    ):
        done = complete_zero(client, world, dot=1.0)

        body = make_check_group(client, world, done["id"], notes="windy")

        assert body["purpose"] == "zero_check" and body["zero_id"] == done["id"]
        assert body["masked"] is True and body["aim"] is None
        assert body["status"] == "draft" and body["shots_expected"] == 2
        assert body["lot_code"] == "LOT-1" and "session_id" not in body
        assert body["notes"] == "windy" and body["load_id"] == world.load.id
        row = stored(conn, body["id"])
        assert (row.purpose, row.zero_id) == ("zero_check", done["id"])

    def test_the_group_carries_its_own_context_at_100_yd(self, client, world, conn):
        done = complete_zero(client, world, dot=1.0)

        body = make_check_group(
            client,
            world,
            done["id"],
            shot_date="2026-10-01",
            shot_time="07:45",
            position="prone_off_pack",
        )

        assert (body["distance_yd"], body["position"], body["suppressor"]) == (
            100,
            "prone_off_pack",
            False,
        )
        assert (body["shot_date"], body["shot_time"]) == ("2026-10-01", "07:45:00")
        assert body["shot_at_source"] == "manual"
        row = stored(conn, body["id"])
        assert (row.distance_yd, row.position, row.shot_date) == (
            100,
            "prone_off_pack",
            "2026-10-01",
        )

    def test_a_request_distance_is_refused_because_the_body_has_none(self, client, world, conn):
        done = complete_zero(client, world, dot=1.0)
        before = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]

        response = client.post(
            f"/api/zeros/{done['id']}/check-groups",
            json=check_group_body(world, distance_yd=300),
        )

        assert (response.status_code, response.json()["code"]) == (422, "validation_error")
        assert conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == before

    def test_a_named_suppressor_is_stored_with_the_group(self, client, world, conn):
        done = complete_zero(client, world, dot=1.0)
        suppressor = client.post("/api/suppressors", json={"name": "Can A"}).json()

        body = make_check_group(
            client, world, done["id"], suppressor=True, suppressor_id=suppressor["id"]
        )

        assert body["suppressor"] is True
        assert stored(conn, body["id"]).suppressor_id == suppressor["id"]

    def test_the_date_comes_from_the_photo_when_none_is_given(self, client, world, conn):
        done = complete_zero(client, world, dot=1.0)
        photo = seed_photo(conn)
        with conn:
            repo_groups.set_photo_taken_at(conn, photo.id, "2026-08-15T06:30:10", "-06:00")

        body = make_check_group(client, world, done["id"], shot_date=None, photo_id=photo.id)

        assert (body["shot_date"], body["shot_time"]) == ("2026-08-15", "06:30:10")
        assert (body["shot_at_source"], body["photo_id"]) == ("photo", photo.id)
        assert body["utc_offset"] == "-06:00" and stored(conn, body["id"]).utc_offset == "-06:00"

    def test_it_is_masked_at_every_later_moment_including_two_shots_with_an_aim(
        self, client, world, conn
    ):
        done = complete_zero(client, world, dot=1.0)
        group_id = make_check_group(client, world, done["id"])["id"]
        listing = lambda: client.get(f"/api/groups/{group_id}").json()  # noqa: E731
        assert (listing()["masked"], listing()["aim"]) == (True, None)

        put_shots(conn, group_id, [Offset(0.3, 0.1), Offset(-0.2, 0.2)])

        assert (listing()["masked"], listing()["aim"]) == (True, None)
        stats = client.get(f"/api/groups/{group_id}/stats").json()
        assert "mean_dx_mil" not in json.dumps(stats) or stats.get("mean_dx_mil") is None
        assert stored(conn, group_id).purpose == "zero_check"
        assert all(None in row for row in offsets_of(conn, group_id))

    def test_the_check_then_runs_on_the_group_it_created(self, client, world, conn):
        done = complete_zero(client, world, dot=1.0)
        group_id = make_check_group(client, world, done["id"])["id"]
        put_shots(conn, group_id, [Offset(0.3, 0.0), Offset(0.0, -0.3)])
        group_service.finalize(conn, group_id)

        response = client.post(f"/api/zeros/{done['id']}/checks", json={"group_id": group_id})

        assert response.status_code == 201 and response.json()["result"] == "pass"
        assert stored(conn, group_id).purpose == "zero_check"

    def test_the_lot_is_created_for_the_zeros_load_and_reused(self, client, world, conn):
        done = complete_zero(client, world, dot=1.0)

        first = make_check_group(client, world, done["id"], lot_code="  NEW-LOT ")
        second = make_check_group(client, world, done["id"], lot_code="NEW-LOT")

        assert first["lot_code"] == "NEW-LOT" and first["lot_id"] == second["lot_id"]
        assert first["new_lot"] is True
        assert {lot.lot_code for lot in repo_rifles.list_lots(conn, world.load.id)} >= {"NEW-LOT"}

    def test_a_zero_tied_to_a_lot_takes_check_groups_of_that_lot_only(self, client, world, conn):
        zero = new_zero(client, world, lot_code="LOT-Z")
        link(client, zero["id"], world.group(ring(1.08, 0.72), lot_code="LOT-Z").id)
        dialed(client, zero["id"], -0.2, -0.3)
        link(client, zero["id"], world.group(ring(0, 0), lot_code="LOT-Z").id)
        client.post(f"/api/zeros/{zero['id']}/dot", json={"dot_size_in": 1.0})
        before = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]

        wrong = client.post(
            f"/api/zeros/{zero['id']}/check-groups", json=check_group_body(world, lot_code="OTHER")
        )

        assert (wrong.status_code, wrong.json()["code"]) == (409, "group_not_linkable")
        assert conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == before
        assert "OTHER" not in {lot.lot_code for lot in repo_rifles.list_lots(conn, world.load.id)}
        right = client.post(
            f"/api/zeros/{zero['id']}/check-groups", json=check_group_body(world, lot_code="LOT-Z")
        )
        assert right.status_code == 201


class TestCheckGroupGuards:
    def count(self, conn) -> int:
        return conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]

    def test_no_date_and_no_photo_date_is_refused_and_nothing_is_created(self, client, world, conn):
        done = complete_zero(client, world)
        before = self.count(conn)

        response = client.post(
            f"/api/zeros/{done['id']}/check-groups", json=check_group_body(world, shot_date=None)
        )

        assert (response.status_code, response.json()["code"]) == (422, "invalid_shot_date")
        assert self.count(conn) == before

    def test_an_unknown_photo_is_a_404_and_nothing_is_created(self, client, world, conn):
        done = complete_zero(client, world)
        before = self.count(conn)

        response = client.post(
            f"/api/zeros/{done['id']}/check-groups", json=check_group_body(world, photo_id=999)
        )

        assert (response.status_code, response.json()["code"]) == (404, "photo_not_found")
        assert self.count(conn) == before

    def test_an_unknown_suppressor_is_a_404_and_nothing_is_created(self, client, world, conn):
        done = complete_zero(client, world)
        before = self.count(conn)

        response = client.post(
            f"/api/zeros/{done['id']}/check-groups",
            json=check_group_body(world, suppressor=True, suppressor_id=999),
        )

        assert (response.status_code, response.json()["code"]) == (404, "suppressor_not_found")
        assert self.count(conn) == before

    def test_a_zero_that_is_not_complete_is_refused(self, client, world, conn):
        zero = new_zero(client, world)

        response = client.post(
            f"/api/zeros/{zero['id']}/check-groups", json=check_group_body(world)
        )

        assert (response.status_code, response.json()["code"]) == (409, "zero_not_complete")
        assert self.count(conn) == 0

    def test_a_superseded_zero_is_refused(self, client, world, conn):
        old = complete_zero(client, world)
        complete_zero(client, world)
        assert client.get(f"/api/zeros/{old['id']}").json()["state"] == "superseded"
        before = self.count(conn)

        response = client.post(f"/api/zeros/{old['id']}/check-groups", json=check_group_body(world))

        assert (response.status_code, response.json()["code"]) == (409, "zero_superseded")
        assert self.count(conn) == before

    def test_a_zero_whose_group_left_100_yd_is_refused(self, client, world, conn):
        done = complete_zero(client, world)
        confirm = next(g for g in done["groups"] if g["purpose"] == "zero_confirm")
        tamper_to_300(conn, confirm["group_id"])
        before = self.count(conn)

        response = client.post(
            f"/api/zeros/{done['id']}/check-groups", json=check_group_body(world)
        )

        assert (response.status_code, response.json()["code"]) == (422, "invalid_distance")
        assert self.count(conn) == before

    def test_a_missing_zero_is_a_404(self, client, world, conn):
        complete_zero(client, world)
        before = self.count(conn)

        response = client.post("/api/zeros/999/check-groups", json=check_group_body(world))

        assert (response.status_code, response.json()["code"]) == (404, "zero_not_found")
        assert self.count(conn) == before

    @pytest.mark.parametrize(
        "overrides",
        [
            {"shots_expected": 0},
            {"shots_expected": 3},
            {"lot_code": ""},
            {"notes": 5},
            {"extra": 1},
            {"session_id": 1},
            {"position": "hanging"},
            {"suppressor": "maybe"},
            {"suppressor_id": 1},
            {"suppressor_id": 0, "suppressor": True},
            {"shot_date": "yesterday"},
            {"shot_date": None, "shot_time": "07:45"},
            {"shot_time": "25:00"},
            {"photo_id": 0},
            {"photo_id": 2**63},
            {"position": None},
        ],
    )
    def test_a_malformed_body_is_422_validation_error(self, client, world, conn, overrides):
        done = complete_zero(client, world)

        response = client.post(
            f"/api/zeros/{done['id']}/check-groups", json=check_group_body(world, **overrides)
        )

        assert (response.status_code, response.json()["code"]) == (422, "validation_error")

    def test_the_service_refuses_a_blank_lot_and_an_oversize_check(self, client, world, conn):
        done = complete_zero(client, world)
        before = self.count(conn)

        for kwargs, code in (
            ({"lot_code": "  "}, "invalid_lot_code"),
            ({"shots_expected": 3}, "invalid_shots_expected"),
            ({"shots_expected": 0}, "invalid_shots_expected"),
        ):
            args = {
                "lot_code": "L",
                "shots_expected": 2,
                "position": "bench",
                "suppressor": False,
                "shot_date": "2026-09-30",
                **kwargs,
            }
            with pytest.raises(service.ZeroingError) as caught:
                service.create_check_group(conn, done["id"], **args)
            assert caught.value.code == code
        assert self.count(conn) == before

    def test_a_cross_origin_write_is_refused_and_creates_nothing(self, client, world, conn):
        done = complete_zero(client, world)
        before = self.count(conn)

        response = client.post(
            f"/api/zeros/{done['id']}/check-groups",
            json=check_group_body(world),
            headers={"Origin": "http://evil.example"},
        )

        assert (response.status_code, response.json()["code"]) == (403, "bad_origin")
        assert self.count(conn) == before

    def test_the_route_is_in_the_registry_with_the_group_response(self):
        route = ROUTE_SCHEMAS[("POST", "/api/zeros/{id}/check-groups")]
        assert route.request.__name__ == "ZeroCheckGroupCreate"
        assert route.response.__name__ == "GroupOut"


def _playwright_ready() -> bool:
    browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""))
    return browsers.is_dir() and any(browsers.glob("chromium*"))


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestTheZeroingPageCreatesAMaskedCheckGroup:
    """Real Chromium: Create zero-check group posts only to the check-groups route."""

    def test_the_created_group_is_a_masked_linked_zero_check_group_from_its_first_moment(self):
        sync_api = pytest.importorskip("playwright.sync_api")
        with run_live_server() as server, sync_api.sync_playwright() as playwright:
            settings = Settings(data_dir=server.data_dir, port=server.port)
            db = connect(settings.db_path)
            try:
                rifle = seed_rifle(db)
                load = seed_load(db, rifle)
                with db:
                    zero = repo_zeroing.create_zero(
                        db, rifle_id=rifle.id, load_id=load.id, mode="existing", state="complete"
                    )
                    repo_zeroing.update_zero(db, zero.id, dot_size_in=1.0)
            finally:
                db.close()
            writes: list[tuple[str, str]] = []
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                problems: list[str] = []
                page.on("pageerror", lambda error: problems.append(str(error)))
                page.on(
                    "request",
                    lambda r: writes.append((r.method, r.url)) if r.method != "GET" else None,
                )
                page.goto(f"{server}/zeroing/{zero.id}")
                page.locator("#add-group-card:not([hidden])").wait_for()
                page.fill("#ag-date", "2026-09-30")
                page.select_option("#ag-position", "prone_off_pack")
                page.fill("#ag-lot", "LOT-PAGE")
                page.click("#ag-create")
                page.locator("#ag-next:not([hidden])").wait_for()
            finally:
                browser.close()
            db = connect(settings.db_path)
            try:
                groups = repo_groups.list_groups(db)
            finally:
                db.close()
            detail = json.loads(server.request("GET", f"/api/groups/{groups[0].id}").body)

        assert [(m, u.removeprefix(str(server))) for m, u in writes] == [
            ("POST", f"/api/zeros/{zero.id}/check-groups")
        ]
        assert [(g.purpose, g.zero_id) for g in groups] == [("zero_check", zero.id)]
        assert (detail["masked"], detail["aim"]) == (True, None)
        assert (detail["distance_yd"], detail["position"]) == (100, "prone_off_pack")
        assert detail["shot_date"] == "2026-09-30"
        assert problems == []
