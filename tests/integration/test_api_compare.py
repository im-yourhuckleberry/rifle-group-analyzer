"""T3.6: load comparison API and page (like-with-like tiers, verdicts, POI shift, masking)."""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from rga.config import Settings
from rga.db import repo_aggregates, repo_groups
from rga.db.connection import connect
from rga.domain.comparison import ZERO_TO_HUNTING_LOAD_ADVICE
from rga.domain.types import Offset, ShotOrigin
from rga.services import compare as compare_service
from rga.web.app import create_app
from rga.web.schemas import ROUTE_SCHEMAS, ComparisonOut
from tests.support.seed import seed_context, seed_group, seed_load, seed_rifle

ORIGIN = "http://127.0.0.1:8765"
XSS = "<script>alert(1)</script>"
MOA_IN_PER_100YD = 1.0471975512
WEB = Path(__file__).resolve().parents[2] / "src" / "rga" / "web"
NO_SHARED = (
    "shoot both loads on the same day, distance, position and suppressor setting "
    "to measure POI shift"
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def conn(settings: Settings, app: FastAPI) -> Iterator[sqlite3.Connection]:
    connection = connect(settings.db_path)
    try:
        yield connection
    finally:
        connection.close()


def ring(n: int, es_moa: float, *, distance_yd: int = 100, center: tuple[float, float] = (0, 0)):
    """`n` (even) shots on a circle whose diameter is `es_moa` at `distance_yd`; centroid = center."""
    es_in = es_moa * MOA_IN_PER_100YD * distance_yd / 100
    radius = es_in / 2
    return [
        Offset(
            center[0] + radius * math.cos(2 * math.pi * k / n),
            center[1] + radius * math.sin(2 * math.pi * k / n),
        )
        for k in range(n)
    ]


def mil_to_in(mil: float, distance_yd: int = 100) -> float:
    return mil * distance_yd * 36 / 1000


class World:
    """A rifle with a zero load Z and two more loads, seeded through the real repos."""

    def __init__(self, conn: sqlite3.Connection, client: TestClient):
        self.conn, self.client = conn, client
        self.rifle = seed_rifle(conn)
        self.zero = seed_load(conn, self.rifle, label="Zero load Z")
        self.a = seed_load(conn, self.rifle, label="Load A")
        self.b = seed_load(conn, self.rifle, label="Load B")
        assert client.put(
            f"/api/rifles/{self.rifle.id}/zero-load", json={"load_id": self.zero.id}
        ).is_success
        self._days = 0

    def context(self, **kw):
        """A new group context; each call is a new calendar day unless `date` is given."""
        self._days += 1
        kw.setdefault("date", f"2026-09-{self._days:02d}")
        return seed_context(self.rifle, **kw)

    def group(self, context, load, offsets, **kw):
        return seed_group(self.conn, context=context, load=load, offsets=offsets, **kw)

    def compare(self, *loads, **params):
        ids = ",".join(str(load.id) for load in loads)
        query = "&".join([f"load_ids={ids}", *(f"{k}={v}" for k, v in params.items())])
        return self.client.get(f"/api/rifles/{self.rifle.id}/comparison?{query}")


@pytest.fixture
def world(conn: sqlite3.Connection, client: TestClient) -> World:
    return World(conn, client)


def by_load(body: dict) -> dict[int, dict]:
    return {item["load_id"]: item for item in body["loads"]}


class TestTiersAndVerdicts:
    def test_a_five_round_load_is_insufficient_and_unranked(self, world):
        context = world.context()
        world.group(context, world.a, ring(10, 1.0))
        world.group(context, world.b, ring(6, 0.5)[:5])
        body = world.compare(world.a, world.b).json()
        small = by_load(body)[world.b.id]
        assert small["status"] == "insufficient_data"
        assert small["precision_verdict"] is None
        assert small["tier10"] == {
            "rounds": 0,
            "groups": 0,
            "mean_es_moa": None,
            "worst_es_moa": None,
        }
        assert [g["tier"] for g in small["groups"]] == [None]
        assert small["groups"][0]["shots"] == 5
        assert body["pairs"] == [
            {
                "a_load_id": world.a.id,
                "b_load_id": world.b.id,
                "tier10": "insufficient_data",
                "tier30": "insufficient_data",
            }
        ]

    def test_two_ten_shot_groups_per_load_at_25_percent_are_no_real_difference(self, world):
        for load, es in ((world.a, 1.0), (world.b, 1.28)):
            for _ in range(2):
                world.group(world.context(), load, ring(10, es))
        body = world.compare(world.a, world.b).json()
        loads = by_load(body)
        assert loads[world.a.id]["status"] == "high_confidence"
        assert loads[world.a.id]["tier10"]["rounds"] == 20
        assert loads[world.a.id]["tier10"]["mean_es_moa"] == pytest.approx(1.0, abs=1e-6)
        assert loads[world.b.id]["tier10"]["mean_es_moa"] == pytest.approx(1.28, abs=1e-6)
        assert body["pairs"][0]["tier10"] == "no_real_difference"
        assert body["pairs"][0]["tier30"] == "insufficient_data"

    def test_a_larger_difference_ranks_the_smaller_group_first(self, world):
        world.group(world.context(), world.a, ring(10, 1.0))
        world.group(world.context(), world.b, ring(10, 1.6))
        pair = world.compare(world.a, world.b).json()["pairs"][0]
        assert pair["tier10"] == "a_smaller"
        reverse = world.compare(world.b, world.a).json()["pairs"][0]
        assert reverse["tier10"] == "b_smaller"
        assert (reverse["a_load_id"], reverse["b_load_id"]) == (world.b.id, world.a.id)

    @pytest.mark.parametrize(
        ("es", "verdict"),
        [
            (1.2, "sufficient for deer/elk to 800 yd"),
            (1.8, "sufficient for deer/elk to 600 yd"),
            (2.4, "above Form's 2 MOA / 10-shot bar"),
        ],
    )
    def test_the_precision_verdict_follows_the_ten_shot_average(self, world, es, verdict):
        world.group(world.context(), world.a, ring(10, es))
        assert by_load(world.compare(world.a).json())[world.a.id]["precision_verdict"] == verdict

    def test_a_thirty_shot_group_does_not_change_the_ten_shot_verdict_or_ranking(self, world):
        world.group(world.context(), world.a, ring(10, 1.45))
        world.group(world.context(), world.a, ring(30, 2.6))
        world.group(world.context(), world.b, ring(10, 1.45))
        body = world.compare(world.a, world.b).json()
        a = by_load(body)[world.a.id]
        assert a["precision_verdict"] == "sufficient for deer/elk to 800 yd"
        assert a["tier10"]["mean_es_moa"] == pytest.approx(1.45, abs=1e-6)
        assert a["tier30"]["groups"] == 1 and a["tier30"]["rounds"] == 30
        assert a["tier30"]["mean_es_moa"] == pytest.approx(2.6, abs=1e-6)
        assert body["pairs"][0]["tier10"] == "no_real_difference"
        assert body["pairs"][0]["tier30"] == "insufficient_data"

    def test_tier30_is_compared_separately_when_both_loads_have_it(self, world):
        world.group(world.context(), world.a, ring(30, 1.0))
        world.group(world.context(), world.b, ring(30, 2.0))
        pair = world.compare(world.a, world.b).json()["pairs"][0]
        assert pair["tier10"] == "insufficient_data"
        assert pair["tier30"] == "a_smaller"

    def test_groups_of_20_to_29_shots_are_listed_but_belong_to_no_tier(self, world):
        world.group(world.context(), world.a, ring(24, 1.0))
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["tier10"]["groups"] == 0 and a["tier30"]["groups"] == 0
        assert [(g["shots"], g["tier"]) for g in a["groups"]] == [(24, None)]
        assert a["status"] == "insufficient_data"

    def test_a_nine_shot_group_is_excluded_from_every_tier(self, world):
        world.group(world.context(), world.a, ring(10, 1.0)[:9])
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["tier10"]["groups"] == 0
        assert a["status"] == "insufficient_data"
        assert a["groups"][0]["tier"] is None

    def test_a_ten_shot_group_enters_tier10(self, world):
        world.group(world.context(), world.a, ring(10, 1.0))
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["tier10"]["groups"] == 1 and a["status"] == "ok"

    def test_draft_groups_never_count(self, world):
        context = world.context()
        world.group(context, world.a, ring(10, 1.0), finalize=False)
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["groups"] == []

    def test_estimated_shots_count_and_are_flagged(self, world):
        origins = [ShotOrigin.DETECTED] * 9 + [ShotOrigin.ESTIMATED]
        world.group(world.context(), world.a, ring(10, 1.0), origins=origins)
        group = by_load(world.compare(world.a).json())[world.a.id]["groups"][0]
        assert group["includes_estimated"] is True and group["tier"] == "tier10"

    def test_the_group_rows_carry_their_own_context(self, world):
        context = world.context(
            distance_yd=300,
            date="2026-08-15",
            time="07:45:00",
            position="prone_off_pack",
            suppressor=True,
        )
        gid = world.group(context, world.a, ring(10, 1.0, distance_yd=300)).id
        (group,) = by_load(world.compare(world.a).json())[world.a.id]["groups"]
        assert group["group_id"] == gid and "session_id" not in group and "date" not in group
        assert (group["shot_date"], group["shot_time"]) == ("2026-08-15", "07:45:00")
        assert group["distance_yd"] == 300
        assert (group["position"], group["suppressor"]) == ("prone_off_pack", True)
        assert group["es_moa"] == pytest.approx(1.0, abs=1e-6)

    def test_loads_come_back_in_the_requested_order_and_every_pair_is_compared(self, world):
        body = world.compare(world.b, world.zero, world.a).json()
        assert [item["load_id"] for item in body["loads"]] == [
            world.b.id,
            world.zero.id,
            world.a.id,
        ]
        pairs = [(p["a_load_id"], p["b_load_id"]) for p in body["pairs"]]
        assert pairs == [
            (world.b.id, world.zero.id),
            (world.b.id, world.a.id),
            (world.zero.id, world.a.id),
        ]

    def test_a_single_load_has_no_pairs(self, world):
        assert world.compare(world.a).json()["pairs"] == []

    def test_the_response_matches_the_registered_schema_and_carries_the_advice(self, world):
        world.group(world.context(), world.a, ring(10, 1.0))
        body = world.compare(world.a, world.b).json()
        TypeAdapter(ROUTE_SCHEMAS[("GET", "/api/rifles/{id}/comparison")].response).validate_python(
            body
        )
        ComparisonOut.model_validate(body)
        assert body["zero_to_hunting_load_advice"] == ZERO_TO_HUNTING_LOAD_ADVICE
        assert "offset" not in body["zero_to_hunting_load_advice"].lower()
        assert body["rifle_id"] == world.rifle.id
        assert body["filters"] == {"position": None, "suppressor": None}

    def test_groups_of_other_rifles_and_loads_are_never_mixed_in(self, world, conn):
        other = seed_rifle(conn, name="Other")
        other_load = seed_load(conn, other, label="Other load")
        seed_group(conn, context=seed_context(other), load=other_load, offsets=ring(10, 0.3))
        world.group(world.context(), world.a, ring(10, 1.0))
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["tier10"]["mean_es_moa"] == pytest.approx(1.0, abs=1e-6)


class TestPoiShift:
    def test_two_shared_days_give_the_hand_computed_mean(self, world):
        s1, s2 = world.context(), world.context()
        zero_centers = {s1: (0.0, 0.0), s2: (mil_to_in(-0.5), 0.0)}
        load_centers = {s1: (mil_to_in(1.0), 0.0), s2: (0.0, mil_to_in(0.5))}
        for context in (s1, s2):
            world.group(context, world.zero, ring(10, 1.0, center=zero_centers[context]))
            world.group(context, world.a, ring(10, 1.0, center=load_centers[context]))
        only_a = world.context()
        world.group(only_a, world.a, ring(10, 1.0, center=(mil_to_in(9.0), mil_to_in(9.0))))
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["poi_shift_note"] is None
        assert a["poi_shift"]["right_mil"] == pytest.approx((1.0 + 0.5) / 2, abs=1e-6)
        assert a["poi_shift"]["up_mil"] == pytest.approx((0.0 + 0.5) / 2, abs=1e-6)
        assert (
            "0.8 mil right" in a["poi_shift"]["text"] or "0.7 mil right" in a["poi_shift"]["text"]
        )
        assert "2 shared days" in a["poi_shift"]["text"]
        assert "zero load" in a["poi_shift"]["text"]

    def test_the_shift_text_uses_direction_words_without_advice(self, world):
        context = world.context()
        world.group(context, world.zero, ring(10, 1.0))
        world.group(context, world.a, ring(10, 1.0, center=(mil_to_in(0.3), mil_to_in(-0.2))))
        shift = by_load(world.compare(world.a).json())[world.a.id]["poi_shift"]
        assert shift["text"] == "0.2 mil low, 0.3 mil right of the zero load (mean of 1 shared day)"
        assert not re.search(r"\b(dial|adjust|correct|offset)\b", shift["text"], re.I)

    def test_two_groups_of_one_bucket_are_pooled(self, world):
        context = world.context()
        world.group(context, world.zero, ring(10, 1.0))
        world.group(context, world.a, ring(10, 1.0, center=(mil_to_in(0.4), 0.0)))
        world.group(context, world.a, ring(10, 1.0, center=(mil_to_in(0.8), 0.0)))
        shift = by_load(world.compare(world.a).json())[world.a.id]["poi_shift"]
        assert shift["right_mil"] == pytest.approx(0.6, abs=1e-6)
        assert shift["up_mil"] == pytest.approx(0.0, abs=1e-6)

    def test_no_shared_bucket_gives_null_and_the_message(self, world):
        world.group(world.context(), world.zero, ring(10, 1.0))
        world.group(world.context(), world.a, ring(10, 1.0, center=(5, 5)))
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["poi_shift"] is None
        assert a["poi_shift_note"] == NO_SHARED

    def test_groups_at_different_distances_are_never_paired(self, world):
        world.group(world.context(distance_yd=100), world.zero, ring(10, 1.0))
        world.group(
            world.context(distance_yd=200), world.a, ring(10, 1.0, distance_yd=200, center=(5, 5))
        )
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["poi_shift"] is None and a["poi_shift_note"] == NO_SHARED

    def test_the_same_distance_on_another_day_is_never_paired(self, world):
        world.group(world.context(date="2026-09-01"), world.zero, ring(10, 1.0))
        world.group(
            world.context(date="2026-09-02"), world.a, ring(10, 1.0, center=(mil_to_in(1.0), 0.0))
        )
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["poi_shift"] is None and a["poi_shift_note"] == NO_SHARED

    def test_another_position_or_suppressor_setting_on_the_same_day_is_never_paired(self, world):
        day = {"date": "2026-09-05"}
        world.group(world.context(position="bench", **day), world.zero, ring(10, 1.0))
        world.group(
            world.context(position="prone_off_pack", **day),
            world.a,
            ring(10, 1.0, center=(mil_to_in(1.0), 0.0)),
        )
        world.group(
            world.context(position="bench", suppressor=True, **day),
            world.b,
            ring(10, 1.0, center=(mil_to_in(1.0), 0.0)),
        )
        by_id = by_load(world.compare(world.a, world.b).json())
        for load in (world.a, world.b):
            assert by_id[load.id]["poi_shift"] is None
            assert by_id[load.id]["poi_shift_note"] == NO_SHARED

    def test_the_same_day_distance_position_and_suppressor_pair_whatever_the_time_or_weather(
        self, world
    ):
        morning = world.context(date="2026-09-07", time="06:10:00")
        evening = world.context(date="2026-09-07", time="18:45:00", temperature_f=88.0)
        world.group(morning, world.zero, ring(10, 1.0))
        world.group(evening, world.a, ring(10, 1.0, center=(mil_to_in(0.6), 0.0)))
        shift = by_load(world.compare(world.a).json())[world.a.id]["poi_shift"]
        assert shift["right_mil"] == pytest.approx(0.6, abs=1e-6)
        assert "1 shared day" in shift["text"]

    def test_two_shared_distances_on_one_day_average_the_buckets_and_count_one_day(self, world):
        near = world.context(date="2026-09-08", distance_yd=100)
        far = world.context(date="2026-09-08", distance_yd=200)
        world.group(near, world.zero, ring(10, 1.0))
        world.group(far, world.zero, ring(10, 1.0, distance_yd=200))
        world.group(near, world.a, ring(10, 1.0, center=(mil_to_in(0.2), 0.0)))
        world.group(far, world.a, ring(10, 1.0, distance_yd=200, center=(mil_to_in(0.6, 200), 0.0)))
        shift = by_load(world.compare(world.a).json())[world.a.id]["poi_shift"]
        assert shift["right_mil"] == pytest.approx(0.4, abs=1e-6)
        assert "(mean of 1 shared day)" in shift["text"]

    def test_only_the_shared_buckets_count_when_others_exist(self, world):
        shared = world.context(date="2026-09-09")
        world.group(shared, world.zero, ring(10, 1.0))
        world.group(shared, world.a, ring(10, 1.0, center=(mil_to_in(0.5), 0.0)))
        world.group(world.context(date="2026-09-10"), world.a, ring(10, 1.0, center=(9, 9)))
        world.group(world.context(date="2026-09-11"), world.zero, ring(10, 1.0, center=(-9, -9)))
        shift = by_load(world.compare(world.a).json())[world.a.id]["poi_shift"]
        assert shift["right_mil"] == pytest.approx(0.5, abs=1e-6)
        assert shift["up_mil"] == pytest.approx(0.0, abs=1e-6)
        assert "1 shared day" in shift["text"]

    def test_the_zero_load_itself_has_no_shift(self, world):
        world.group(world.context(), world.zero, ring(10, 1.0))
        zero = by_load(world.compare(world.zero).json())[world.zero.id]
        assert zero["is_zero_load"] is True
        assert zero["poi_shift"] is None and "zero load" in zero["poi_shift_note"]

    def test_a_rifle_without_a_zero_load_has_no_shifts(self, world, client):
        assert client.put(
            f"/api/rifles/{world.rifle.id}/zero-load", json={"load_id": None}
        ).is_success
        context = world.context()
        world.group(context, world.zero, ring(10, 1.0))
        world.group(context, world.a, ring(10, 1.0, center=(3, 3)))
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["is_zero_load"] is False
        assert a["poi_shift"] is None and "set the rifle's zero load" in a["poi_shift_note"]

    def test_the_zero_load_need_not_be_among_the_compared_loads(self, world):
        context = world.context()
        world.group(context, world.zero, ring(10, 1.0))
        world.group(context, world.b, ring(10, 1.0, center=(mil_to_in(0.5), 0.0)))
        body = world.compare(world.b).json()
        assert [item["load_id"] for item in body["loads"]] == [world.b.id]
        assert body["loads"][0]["poi_shift"]["right_mil"] == pytest.approx(0.5, abs=1e-6)

    def test_groups_of_under_ten_shots_never_enter_a_poi_pair(self, world):
        context = world.context()
        world.group(context, world.zero, ring(10, 1.0))
        world.group(context, world.a, ring(10, 1.0, center=(mil_to_in(2.0), 0.0))[:9])
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["poi_shift"] is None and a["poi_shift_note"] == NO_SHARED
        assert a["groups"][0]["shots"] == 9 and a["groups"][0]["tier"] is None

    def test_a_nine_shot_zero_load_group_does_not_form_a_pair_either(self, world):
        context = world.context()
        world.group(context, world.zero, ring(10, 1.0, center=(mil_to_in(2.0), 0.0))[:9])
        world.group(context, world.a, ring(10, 1.0))
        assert by_load(world.compare(world.a).json())[world.a.id]["poi_shift"] is None

    def test_a_group_of_20_to_29_shots_does_enter_a_poi_pair(self, world):
        context = world.context()
        world.group(context, world.zero, ring(10, 1.0))
        world.group(context, world.a, ring(24, 1.0, center=(mil_to_in(0.5), 0.0)))
        shift = by_load(world.compare(world.a).json())[world.a.id]["poi_shift"]
        assert shift["right_mil"] == pytest.approx(0.5, abs=1e-6)

    def test_position_and_suppressor_filters_apply_to_the_pairs_too(self, world):
        bench = world.context(position="bench")
        prone = world.context(position="prone_off_pack")
        for context in (bench, prone):
            world.group(context, world.zero, ring(10, 1.0))
        world.group(bench, world.a, ring(10, 1.0, center=(mil_to_in(1.0), 0.0)))
        world.group(prone, world.a, ring(10, 1.0, center=(mil_to_in(3.0), 0.0)))
        both = by_load(world.compare(world.a).json())[world.a.id]["poi_shift"]
        only_prone = by_load(world.compare(world.a, position="prone_off_pack").json())[world.a.id][
            "poi_shift"
        ]
        assert both["right_mil"] == pytest.approx(2.0, abs=1e-6)
        assert only_prone["right_mil"] == pytest.approx(3.0, abs=1e-6)


class TestMaskedGroupsNeverEnterAComparison:
    """B-20: groups whose point of impact is masked stay out of tiers, pairs and the list."""

    def masked_groups(self, world, context, load, *, center=(0.0, 0.0), es=0.2):
        """A zero_check group (any size), and zero_step2/zero_confirm groups under 10 shots."""
        return [
            world.group(context, load, ring(12, es, center=center), purpose="zero_check"),
            world.group(context, load, ring(8, es, center=center), purpose="zero_step2"),
            world.group(context, load, ring(6, es, center=center), purpose="zero_confirm"),
        ]

    def test_masked_groups_do_not_enter_tiers_or_the_group_list(self, world):
        context = world.context()
        world.group(context, world.a, ring(10, 1.45))
        masked = self.masked_groups(world, context, world.a)
        body = world.compare(world.a).json()
        a = by_load(body)[world.a.id]
        listed = {g["group_id"] for g in a["groups"]}
        assert listed.isdisjoint(masked)
        assert len(listed) == 1
        assert a["tier10"] == {
            "rounds": 10,
            "groups": 1,
            "mean_es_moa": pytest.approx(1.45, abs=1e-6),
            "worst_es_moa": pytest.approx(1.45, abs=1e-6),
        }
        assert a["precision_verdict"] == "sufficient for deer/elk to 800 yd"

    def test_a_masked_group_alone_leaves_the_load_with_no_data(self, world):
        self.masked_groups(world, world.context(), world.a)
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["groups"] == [] and a["status"] == "insufficient_data"
        assert a["tier10"]["groups"] == 0 and a["precision_verdict"] is None

    def test_a_masked_zero_check_group_of_twelve_shots_cannot_improve_a_load(self, world):
        context = world.context()
        world.group(context, world.a, ring(10, 2.5))
        world.group(context, world.a, ring(12, 0.1), purpose="zero_check")
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["tier10"]["mean_es_moa"] == pytest.approx(2.5, abs=1e-6)
        assert a["tier10"]["rounds"] == 10
        assert a["precision_verdict"] == "above Form's 2 MOA / 10-shot bar"

    def test_masked_groups_never_form_poi_shift_pairs(self, world):
        shared = world.context()
        world.group(shared, world.zero, ring(10, 1.0))
        world.group(shared, world.a, ring(10, 1.0, center=(mil_to_in(1.0), 0.0)))
        self.masked_groups(world, shared, world.a, center=(mil_to_in(8.0), mil_to_in(8.0)))
        self.masked_groups(world, shared, world.zero, center=(mil_to_in(-8.0), mil_to_in(-8.0)))
        shift = by_load(world.compare(world.a).json())[world.a.id]["poi_shift"]
        assert shift["right_mil"] == pytest.approx(1.0, abs=1e-6)
        assert shift["up_mil"] == pytest.approx(0.0, abs=1e-6)
        assert "mean of 1 shared day)" in shift["text"]

    def test_a_session_with_only_masked_groups_of_a_load_is_not_shared(self, world):
        context = world.context()
        world.group(context, world.zero, ring(10, 1.0))
        self.masked_groups(world, context, world.a, center=(mil_to_in(4.0), 0.0))
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["poi_shift"] is None and a["poi_shift_note"] == NO_SHARED

    def test_masked_zero_load_groups_alone_make_no_shift(self, world):
        context = world.context()
        self.masked_groups(world, context, world.zero, center=(mil_to_in(4.0), 0.0))
        world.group(context, world.a, ring(10, 1.0))
        assert by_load(world.compare(world.a).json())[world.a.id]["poi_shift"] is None

    def test_a_zero_step2_group_of_ten_shots_is_not_masked_and_does_count(self, world):
        gid = world.group(world.context(), world.a, ring(10, 1.0), purpose="zero_step2").id
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert [g["group_id"] for g in a["groups"]] == [gid]
        assert a["tier10"]["groups"] == 1

    def test_the_response_has_no_per_shot_offsets_or_direction_text_for_groups(self, world):
        context = world.context()
        world.group(context, world.a, ring(10, 1.0, center=(2, 2)))
        text = world.compare(world.a).text
        for key in ('"dx_', '"dy_', '"mean_dx', '"mean_dy', '"offsets"', '"shots_detail"'):
            assert key not in text

    def test_service_level_masking_matches_the_group_service_rule(self, world, conn):
        from rga.services.groups import masks_poi

        context = world.context()
        world.group(context, world.a, ring(12, 1.0), purpose="zero_check")
        world.group(context, world.a, ring(8, 1.0), purpose="zero_confirm")
        world.group(context, world.a, ring(10, 1.0), purpose="zero_confirm")
        result = compare_service.compare(conn, world.rifle.id, [world.a.id])
        (load,) = result.loads
        assert [g.shots for g in load.groups] == [10]
        assert masks_poi("zero_check", 12) and masks_poi("zero_confirm", 8)
        assert not masks_poi("zero_confirm", 10)


class TestFiltersAndWarning:
    def seed(self, world):
        bench = world.context(position="bench", suppressor=False)
        prone = world.context(position="prone_off_pack", suppressor=True)
        world.group(bench, world.a, ring(10, 1.0))
        world.group(prone, world.a, ring(10, 2.0))
        world.group(bench, world.b, ring(10, 1.4))
        return bench, prone

    def test_the_position_filter_keeps_only_matching_groups(self, world):
        self.seed(world)
        body = world.compare(world.a, world.b, position="bench").json()
        a = by_load(body)[world.a.id]
        assert [g["position"] for g in a["groups"]] == ["bench"]
        assert a["tier10"]["mean_es_moa"] == pytest.approx(1.0, abs=1e-6)
        assert body["filters"]["position"] == "bench"

    def test_the_suppressor_filter_keeps_only_matching_groups(self, world):
        self.seed(world)
        a = by_load(world.compare(world.a, suppressor="true").json())[world.a.id]
        assert [g["suppressor"] for g in a["groups"]] == [True]
        assert a["tier10"]["mean_es_moa"] == pytest.approx(2.0, abs=1e-6)
        off = by_load(world.compare(world.a, suppressor="false").json())[world.a.id]
        assert off["tier10"]["mean_es_moa"] == pytest.approx(1.0, abs=1e-6)

    def test_mismatched_conditions_warn_and_a_filter_clears_the_warning(self, world):
        self.seed(world)
        mixed = world.compare(world.a, world.b).json()
        assert mixed["conditions_warning"] is not None
        assert "position" in mixed["conditions_warning"]
        assert "suppressor" in mixed["conditions_warning"]
        assert (
            world.compare(world.a, world.b, position="bench", suppressor="false").json()[
                "conditions_warning"
            ]
            is None
        )

    def test_a_group_under_ten_shots_in_other_conditions_does_not_trigger_the_warning(self, world):
        bench = world.context(position="bench")
        prone = world.context(position="prone_off_pack", suppressor=True)
        world.group(bench, world.a, ring(10, 1.0))
        world.group(bench, world.b, ring(10, 1.2))
        world.group(prone, world.b, ring(10, 1.2)[:9])
        assert world.compare(world.a, world.b).json()["conditions_warning"] is None

    def test_matching_conditions_have_no_warning(self, world):
        context = world.context()
        world.group(context, world.a, ring(10, 1.0))
        world.group(context, world.b, ring(10, 1.2))
        assert world.compare(world.a, world.b).json()["conditions_warning"] is None


class TestErrors:
    def test_an_unknown_rifle_is_a_404(self, client):
        response = client.get("/api/rifles/999/comparison?load_ids=1")
        assert response.status_code == 404
        assert response.json()["code"] == "rifle_not_found"

    def test_an_unknown_load_is_a_404(self, world):
        response = world.client.get(
            f"/api/rifles/{world.rifle.id}/comparison?load_ids={world.a.id},999"
        )
        assert response.status_code == 404
        assert response.json()["code"] == "load_not_found"

    def test_a_load_of_another_rifle_is_a_409(self, world, conn):
        other = seed_load(conn, seed_rifle(conn, name="Other"), label="Elsewhere")
        response = world.client.get(f"/api/rifles/{world.rifle.id}/comparison?load_ids={other.id}")
        assert response.status_code == 409
        assert response.json()["code"] == "load_not_in_rifle"

    @pytest.mark.parametrize(
        "query",
        [
            "",
            "load_ids=",
            "load_ids=a,b",
            "load_ids=0",
            "load_ids=-1",
            "load_ids=1,1",
            "load_ids=1&position=sideways",
            "load_ids=1&suppressor=maybe",
            "load_ids=1&extra=1",
            "load_ids=" + ",".join(str(i) for i in range(1, 22)),
        ],
    )
    def test_bad_queries_are_422_validation_errors(self, world, query):
        response = world.client.get(f"/api/rifles/{world.rifle.id}/comparison?{query}")
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"

    @pytest.mark.parametrize("rifle_id", ["0", "-3", "abc", str(2**63)])
    def test_bad_rifle_ids_are_422(self, client, rifle_id):
        response = client.get(f"/api/rifles/{rifle_id}/comparison?load_ids=1")
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"

    @pytest.mark.parametrize("load_id", [2**63, 2**70])
    def test_a_load_id_too_big_for_sqlite_is_a_422_not_a_500(self, world, load_id):
        response = world.client.get(f"/api/rifles/{world.rifle.id}/comparison?load_ids={load_id}")
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"

    def test_a_locked_database_is_a_503_busy_with_retry_after(self, world, monkeypatch):
        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(compare_service, "compare", locked)
        response = world.compare(world.a)
        assert response.status_code == 503
        assert response.json()["code"] == "busy"
        assert response.headers["retry-after"] == "1"

    def test_the_service_refuses_ids_too_big_for_sqlite_without_touching_the_database(
        self, world, conn
    ):
        with pytest.raises(compare_service.RifleNotFound):
            compare_service.compare(conn, 2**70, [world.a.id])
        with pytest.raises(compare_service.LoadNotFound):
            compare_service.compare(conn, world.rifle.id, [2**70])

    def test_the_route_is_read_only(self, world):
        assert (
            world.client.post(f"/api/rifles/{world.rifle.id}/comparison", json={}).status_code
            == 405
        )


class TestSavedAggregates:
    """Saved single-load combinations of 30 or more shots feed tier30 by their pooled ES."""

    def three_tens(self, world, load, es, *, session_kw=None, centers=((0, 0), (0.2, 0), (0, 0.2))):
        groups = []
        for center in centers:
            context = world.context(**(session_kw or {}))
            groups.append(world.group(context, load, ring(10, es, center=center)))
        return groups

    def save(self, world, groups, name="Three of ten"):
        response = world.client.post(
            "/api/aggregates", json={"name": name, "group_ids": [g.id for g in groups]}
        )
        assert response.status_code == 201, response.text
        return response.json()

    def pooled_es(self, world, groups):
        response = world.client.post("/api/aggregate", json={"group_ids": [g.id for g in groups]})
        return response.json()["es_moa"]

    def test_three_tens_saved_for_each_of_two_loads_give_a_tier30_pairwise_verdict(self, world):
        a_groups = self.three_tens(world, world.a, 1.0)
        b_groups = self.three_tens(world, world.b, 2.0)
        before = world.compare(world.a, world.b).json()
        assert before["pairs"][0]["tier30"] == "insufficient_data"
        self.save(world, a_groups)
        self.save(world, b_groups)
        body = world.compare(world.a, world.b).json()
        ComparisonOut.model_validate(body)
        loads = by_load(body)
        for load, groups in ((world.a, a_groups), (world.b, b_groups)):
            tier30 = loads[load.id]["tier30"]
            assert tier30["groups"] == 1 and tier30["rounds"] == 30
            assert tier30["mean_es_moa"] == pytest.approx(self.pooled_es(world, groups), abs=1e-9)
            assert loads[load.id]["tier10"]["groups"] == 3
        assert body["pairs"][0]["tier30"] == "a_smaller"
        assert body["pairs"][0]["tier10"] == before["pairs"][0]["tier10"] == "a_smaller"
        listed = loads[world.a.id]["aggregates"]
        assert [(a["name"], a["shots"], a["counted"], a["tier"], a["note"]) for a in listed] == [
            ("Three of ten", 30, True, "tier30", None)
        ]
        assert listed[0]["group_ids"] == sorted(g.id for g in a_groups)

    def test_the_pooled_es_is_not_the_mean_of_the_member_group_es_values(self, world):
        groups = self.three_tens(world, world.a, 1.0, centers=((0, 0), (1.0, 0), (0, 1.0)))
        self.save(world, groups)
        tier30 = by_load(world.compare(world.a).json())[world.a.id]["tier30"]
        assert tier30["mean_es_moa"] > 1.5

    def test_two_tens_saved_are_listed_but_not_counted(self, world):
        self.save(world, self.three_tens(world, world.a, 1.0, centers=((0, 0), (0.1, 0))))
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["tier30"]["groups"] == 0
        [entry] = a["aggregates"]
        assert entry["counted"] is False and entry["shots"] == 20 and entry["tier"] is None
        assert "30 or more shots" in entry["note"]

    def test_an_aggregate_with_a_thirty_shot_group_does_not_count_that_group_twice(self, world):
        big = world.group(world.context(), world.a, ring(30, 1.0))
        small = world.group(world.context(), world.a, ring(10, 1.0))
        self.save(world, [big, small])
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["tier30"]["groups"] == 1 and a["tier30"]["rounds"] == 30
        [entry] = a["aggregates"]
        assert entry["counted"] is False and "already counts" in entry["note"]

    def test_a_deleted_member_group_stops_the_aggregate_counting_without_an_error(
        self, world, conn
    ):
        a_groups = self.three_tens(world, world.a, 1.0)
        self.save(world, a_groups)
        self.save(world, self.three_tens(world, world.b, 2.0))
        assert world.compare(world.a, world.b).json()["pairs"][0]["tier30"] == "a_smaller"
        with conn:
            conn.execute("DELETE FROM groups WHERE id = ?", (a_groups[0].id,))
        response = world.compare(world.a, world.b)
        assert response.status_code == 200
        body = response.json()
        a = by_load(body)[world.a.id]
        assert a["tier30"]["groups"] == 0 and body["pairs"][0]["tier30"] == "insufficient_data"
        [entry] = a["aggregates"]
        assert entry["counted"] is False and entry["note"].startswith("no longer valid")
        assert "deleted" in entry["note"] and entry["shots"] == 20
        assert a["tier10"]["groups"] == 2

    def test_a_member_that_becomes_a_zeroing_group_stops_counting_and_its_poi_stays_out(
        self, world, conn
    ):
        groups = self.three_tens(world, world.a, 1.0, centers=((0, 0), (0.3, 0.1), (0, 0.4)))
        self.save(world, groups)
        with conn:
            repo_groups.update_group(conn, groups[0].id, purpose="zero_check")
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["tier30"]["groups"] == 0
        assert a["aggregates"][0]["counted"] is False
        assert groups[0].id not in {g["group_id"] for g in a["groups"]}
        assert "zeroing group" in a["aggregates"][0]["note"]

    def save_raw(self, world, load, groups, name):
        """Insert a combination behind the API's back, as an older database could hold it."""
        with world.conn:
            return repo_aggregates.create_aggregate(
                world.conn,
                name=name,
                rifle_id=world.rifle.id,
                load_id=load.id,
                distance_yd=100,
                group_ids=sorted(g.id for g in groups),
            )

    def tier30(self, world, load):
        return by_load(world.compare(world.a, world.b).json())[load.id]["tier30"]

    def test_re_saving_the_same_combination_never_changes_the_tier30_verdict(self, world):
        tight = self.three_tens(world, world.a, 0.6)
        wide = self.three_tens(world, world.a, 1.8)
        mid = self.three_tens(world, world.b, 1.2)
        self.save(world, tight, "tight")
        self.save(world, wide, "wide")
        self.save(world, mid, "mid")
        before = world.compare(world.a, world.b).json()
        assert before["pairs"][0]["tier30"] == "no_real_difference"
        a_before = by_load(before)[world.a.id]["tier30"]
        assert (a_before["groups"], a_before["rounds"]) == (2, 60)

        for copy in range(5):
            self.save_raw(world, world.a, tight, f"tight again {copy}")
        after = world.compare(world.a, world.b).json()

        a_after = by_load(after)[world.a.id]
        assert after["pairs"][0]["tier30"] == "no_real_difference"
        assert (a_after["tier30"]["groups"], a_after["tier30"]["rounds"]) == (2, 60)
        assert a_after["tier30"]["mean_es_moa"] == pytest.approx(a_before["mean_es_moa"], abs=1e-9)
        notes = [(e["name"], e["counted"], e["note"]) for e in a_after["aggregates"]]
        assert notes[:2] == [("tight", True, None), ("wide", True, None)]
        assert (
            all(
                counted is False and note == "overlaps another saved combination"
                for _, counted, note in notes[2:]
            )
            and len(notes) == 7
        )

    def test_an_overlapping_combination_is_listed_but_its_shots_count_once(self, world):
        groups = self.three_tens(
            world, world.a, 1.0, centers=((0, 0), (0.2, 0), (0, 0.2), (0.2, 0.2))
        )
        self.save(world, groups[:3], "first three")
        self.save_raw(world, world.a, groups[1:], "last three")

        a = by_load(world.compare(world.a).json())[world.a.id]

        assert (a["tier30"]["groups"], a["tier30"]["rounds"]) == (1, 30)
        first, second = a["aggregates"]
        assert (first["counted"], first["note"]) == (True, None)
        assert (second["counted"], second["note"]) == (False, "overlaps another saved combination")
        assert second["shots"] == 30 and second["tier"] == "tier30"

    def test_a_single_shared_group_is_enough_to_overlap(self, world):
        groups = self.three_tens(
            world, world.a, 1.0, centers=((0, 0), (0.2, 0), (0, 0.2), (0.2, 0.2), (0.1, 0.1))
        )
        self.save(world, groups[:3], "one")
        self.save_raw(world, world.a, [groups[2], groups[3], groups[4]], "two")

        a = by_load(world.compare(world.a).json())[world.a.id]

        assert (a["tier30"]["groups"], a["tier30"]["rounds"]) == (1, 30)
        assert [e["counted"] for e in a["aggregates"]] == [True, False]

    def test_disjoint_combinations_both_count(self, world):
        groups = self.three_tens(world, world.a, 1.0, centers=((0, 0),) * 6)
        self.save(world, groups[:3], "left")
        self.save(world, groups[3:], "right")

        a = by_load(world.compare(world.a).json())[world.a.id]

        assert (a["tier30"]["groups"], a["tier30"]["rounds"]) == (2, 60)
        assert [e["counted"] for e in a["aggregates"]] == [True, True]

    def test_counting_is_greedy_in_creation_order_and_only_counted_ones_claim_groups(self, world):
        groups = self.three_tens(
            world, world.a, 1.0, centers=((0, 0), (0.2, 0), (0, 0.2), (0.2, 0.2))
        )
        self.save(world, groups[:2], "too small to count")
        self.save(world, groups[1:], "counts")
        self.save_raw(world, world.a, groups[:3], "overlaps the one that counts")

        a = by_load(world.compare(world.a).json())[world.a.id]

        names = [(e["name"], e["counted"]) for e in a["aggregates"]]
        assert names == [
            ("too small to count", False),
            ("counts", True),
            ("overlaps the one that counts", False),
        ]
        assert (a["tier30"]["groups"], a["tier30"]["rounds"]) == (1, 30)

    def test_the_comparison_lists_a_saved_combination_at_its_members_current_distance(self, world):
        contexts = [world.context(distance_yd=100) for _ in range(3)]
        groups = [world.group(c, world.a, ring(10, 1.0)) for c in contexts]
        self.save(world, groups)
        before = by_load(world.compare(world.a).json())[world.a.id]
        assert before["aggregates"][0]["distance_yd"] == 100
        assert before["groups"][0]["distance_yd"] == 100

        for group in groups:
            assert world.client.patch(
                f"/api/groups/{group.id}", json={"distance_yd": 300}
            ).is_success
        after = by_load(world.compare(world.a).json())[world.a.id]

        assert {g["distance_yd"] for g in after["groups"]} == {300}
        [entry] = after["aggregates"]
        assert entry["distance_yd"] == 300 and entry["counted"] is True
        assert after["tier30"]["rounds"] == 30

    def test_a_combination_whose_members_were_split_across_distances_is_not_counted(self, world):
        contexts = [world.context(distance_yd=100) for _ in range(3)]
        groups = [world.group(c, world.a, ring(10, 1.0)) for c in contexts]
        self.save(world, groups)

        assert world.client.patch(
            f"/api/groups/{groups[0].id}", json={"distance_yd": 200}
        ).is_success
        a = by_load(world.compare(world.a).json())[world.a.id]

        [entry] = a["aggregates"]
        assert entry["counted"] is False and "no longer at one distance" in entry["note"]
        assert a["tier30"]["groups"] == 0

    def test_overlaps_are_per_load(self, world):
        a_groups = self.three_tens(world, world.a, 1.0)
        self.save(world, a_groups, "A set")
        b_groups = self.three_tens(world, world.b, 1.0)
        self.save(world, b_groups, "B set")

        loads = by_load(world.compare(world.a, world.b).json())

        assert loads[world.a.id]["tier30"]["rounds"] == 30
        assert loads[world.b.id]["tier30"]["rounds"] == 30

    def test_deleting_the_saved_aggregate_removes_it_from_the_comparison(self, world):
        saved = self.save(world, self.three_tens(world, world.a, 1.0))
        assert world.client.delete(f"/api/aggregates/{saved['id']}").status_code == 204
        a = by_load(world.compare(world.a).json())[world.a.id]
        assert a["aggregates"] == [] and a["tier30"]["groups"] == 0

    def test_an_aggregate_is_listed_only_under_its_own_load(self, world):
        self.save(world, self.three_tens(world, world.a, 1.0))
        loads = by_load(world.compare(world.a, world.b).json())
        assert len(loads[world.a.id]["aggregates"]) == 1
        assert loads[world.b.id]["aggregates"] == []
        assert loads[world.b.id]["tier30"]["groups"] == 0

    def test_position_and_suppressor_filters_apply_to_the_members_conditions(self, world):
        prone = self.three_tens(world, world.a, 1.0, session_kw={"position": "prone_off_pack"})
        self.save(world, prone)
        for query, listed in (
            ({}, 1),
            ({"position": "prone_off_pack"}, 1),
            ({"position": "bench"}, 0),
            ({"suppressor": "false"}, 1),
            ({"suppressor": "true"}, 0),
        ):
            a = by_load(world.compare(world.a, **query).json())[world.a.id]
            assert len(a["aggregates"]) == listed, query
            assert a["tier30"]["groups"] == listed, query

    def test_an_aggregate_mixing_positions_is_listed_unfiltered_but_never_counted(self, world):
        bench = world.group(world.context(), world.a, ring(10, 1.0))
        prone = world.group(
            world.context(position="prone_off_pack"), world.a, ring(10, 1.0, center=(0.1, 0))
        )
        third = world.group(world.context(), world.a, ring(10, 1.0, center=(0, 0.1)))
        self.save(world, [bench, prone, third])
        a = by_load(world.compare(world.a).json())[world.a.id]
        [entry] = a["aggregates"]
        assert entry["counted"] is False and "different positions" in entry["note"]
        assert a["tier30"]["groups"] == 0
        assert (
            by_load(world.compare(world.a, position="bench").json())[world.a.id]["aggregates"] == []
        )

    def test_the_conditions_warning_sees_the_conditions_of_a_counted_aggregate(self, world):
        self.save(
            world, self.three_tens(world, world.a, 1.0, session_kw={"position": "prone_off_pack"})
        )
        world.group(world.context(), world.b, ring(30, 1.0))
        body = world.compare(world.a, world.b).json()
        assert body["pairs"][0]["tier30"] != "insufficient_data"
        assert "shooting position" in body["conditions_warning"]

    def test_the_conditions_warning_sees_an_aggregate_of_groups_too_small_for_any_tier(self, world):
        small = [
            world.group(world.context(position="prone_off_pack"), world.a, ring(6, 1.0)[:5])
            for _ in range(6)
        ]
        self.save(world, small)
        world.group(world.context(), world.b, ring(30, 1.0))
        body = world.compare(world.a, world.b).json()
        a = by_load(body)[world.a.id]
        assert [g["tier"] for g in a["groups"]] == [None] * 6
        assert a["aggregates"][0]["counted"] is True
        assert "shooting position" in body["conditions_warning"]

    def test_the_compare_page_script_lists_saved_combinations_with_text_content_only(self):
        script = (WEB / "static" / "js" / "compare.js").read_text()
        assert "Saved combinations" in script and "aggregatesList(load.aggregates)" in script
        assert "if (load.aggregates.length)" in script
        assert "innerHTML" not in script and "insertAdjacentHTML" not in script
        assert "Analysis" in (WEB / "templates" / "compare.html").read_text()


class TestPage:
    def test_compare_with_no_query_string_shows_a_rifle_picker(self, client, conn):
        seed_rifle(conn, name="Tikka T3x")
        seed_rifle(conn, name=XSS)
        response = client.get("/compare")
        assert response.status_code == 200
        assert 'data-page="compare-picker"' in response.text
        assert "Tikka T3x" in response.text
        assert XSS not in response.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
        assert re.search(r'href="/compare\?rifle_id=\d+"', response.text)
        assert 'aria-current="page"' in response.text

    def test_the_picker_works_with_no_rifles(self, client):
        response = client.get("/compare")
        assert response.status_code == 200
        assert "No rifles yet" in response.text

    def test_a_rifle_page_lists_loads_filters_and_the_script(self, world):
        response = world.client.get(f"/compare?rifle_id={world.rifle.id}")
        assert response.status_code == 200
        text = response.text
        for load in (world.zero, world.a, world.b):
            assert f'data-load-id="{load.id}"' in text and load.label in text
        assert "zero load</span>" in text
        assert 'id="filter-position"' in text and 'id="filter-suppressor"' in text
        assert 'id="run-compare"' in text
        assert 'src="/static/js/compare.js"' in text
        assert "smallest target every shot hit (center-to-center)" in text

    def test_load_notes_are_shown_verbatim_and_escaped(self, world, conn):
        note = f"Lot 4, chrono'd 2750 fps & {XSS}\nsecond line"
        seed_load(conn, world.rifle, label="Noted", notes=note)
        text = world.client.get(f"/compare?rifle_id={world.rifle.id}").text
        assert XSS not in text
        assert (
            "Lot 4, chrono&#39;d 2750 fps &amp; &lt;script&gt;alert(1)&lt;/script&gt;\nsecond line"
            in text
        )

    def test_a_rifle_without_loads_says_so(self, client, conn):
        rifle = seed_rifle(conn, name="Bare")
        text = client.get(f"/compare?rifle_id={rifle.id}").text
        assert "no loads yet" in text
        assert 'id="run-compare"' not in text
        assert "compare.js" not in text

    def test_page_errors_are_json_with_a_code(self, client):
        missing = client.get("/compare?rifle_id=999")
        assert (missing.status_code, missing.json()["code"]) == (404, "rifle_not_found")
        assert client.get("/compare?rifle_id=0").status_code == 422
        assert client.get(f"/compare?rifle_id={2**70}").status_code == 404
        assert client.get("/compare?rifle_id=abc").status_code == 422

    def test_the_script_uses_api_js_and_text_content_only(self):
        source = (WEB / "static" / "js" / "compare.js").read_text(encoding="utf-8")
        assert 'from "/static/js/api.js"' in source
        assert not re.search(r"\bfetch\s*\(", source)
        assert "innerHTML" not in source and "insertAdjacentHTML" not in source
        assert "/api/rifles/${rifleId}/comparison?" in source
        assert "textContent" in source

    def test_the_comparison_template_has_no_form_posts_or_inline_code(self):
        source = (WEB / "templates" / "compare.html").read_text(encoding="utf-8")
        assert "<form" not in source
        assert not re.search(r"<script(?![^>]*\bsrc=)", source)
        assert "style=" not in source and not re.search(r"\son\w+=", source)
        for banned in ("mean radius", "mean_radius", "best-of", "best of"):
            assert banned not in source.lower()
