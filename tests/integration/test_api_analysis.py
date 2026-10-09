"""T3.5: group list, detail and statistics, the point-of-impact mask, the aggregate overlay,
notes search, and the group and analysis pages."""

from __future__ import annotations

import html as html_module
import itertools
import json
import math
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from rga.config import Settings
from rga.db import repo_groups
from rga.db.connection import connect
from rga.domain.types import Offset, PlanePoint, ShotOrigin
from rga.services import analysis as analysis_service
from rga.services import groups as group_service
from rga.web.app import create_app
from rga.web.schemas import (
    ES_LABEL,
    ROUTE_SCHEMAS,
    AggregateOut,
    GroupDetailOut,
    GroupOut,
    GroupStatsOut,
    SavedAggregateOut,
    SearchHitOut,
    SizeOnlyStatsOut,
)
from tests.support.seed import seed_context, seed_group, seed_load, seed_rifle

ORIGIN = "http://127.0.0.1:8765"
XSS = "<script>alert(1)</script>"
WEB = Path(__file__).resolve().parents[2] / "src" / "rga" / "web"
MOA_IN_PER_100YD = 1.0471975512
FULL_KEYS = {
    "n",
    "es_in",
    "es_moa",
    "mean_dx_in",
    "mean_dy_in",
    "mean_dx_mil",
    "mean_dy_mil",
    "includes_estimated",
    "es_label",
    "poi_text",
}
SIZE_KEYS = {"n", "es_in", "es_moa", "includes_estimated", "es_label"}
POI_WORDS = re.compile(r"\b(mils?|high|low|left|right|POI)\b|mean_d[xy]|poi_text", re.IGNORECASE)


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


def offsets(seed: int, n: int = 10, spread: float = 0.6) -> list[Offset]:
    rng = np.random.default_rng(seed)
    return [Offset(float(x), float(y)) for x, y in rng.normal(0.1, spread / 2, (n, 2))]


def error(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"detail", "code"} and body["code"] == code, body
    return body


def brute_force_es(points: list[Offset]) -> float:
    return max(
        math.dist((a.dx_in, a.dy_in), (b.dx_in, b.dy_in))
        for a, b in itertools.combinations(points, 2)
    )


@pytest.fixture
def world(conn):
    """One rifle with two loads, 100 yd and 200 yd shooting contexts, and final general groups."""
    rifle = seed_rifle(conn)
    load_a = seed_load(conn, rifle, label="Load A")
    load_b = seed_load(conn, rifle, label="Load B")
    near = seed_context(rifle, distance_yd=100, notes="windy morning 50% humidity")
    far = seed_context(rifle, distance_yd=200, date="2026-10-01")
    return rifle, load_a, load_b, near, far


# ----------------------------------------------------------------------------------- aggregate


def test_three_ten_shot_groups_aggregate_to_thirty_shots_with_brute_force_es(
    client, conn, world
) -> None:
    rifle, load_a, _, near, _ = world
    sets = [offsets(seed) for seed in (1, 2, 3)]
    groups = [seed_group(conn, context=near, load=load_a, offsets=pts) for pts in sets]
    response = client.post("/api/aggregate", json={"group_ids": [g.id for g in groups]})
    assert response.status_code == 200, response.text
    out = AggregateOut.model_validate(response.json())
    everything = [o for pts in sets for o in pts]
    assert out.n == 30 and out.group_count == 3 and len(out.shots) == 30
    assert out.es_in == pytest.approx(brute_force_es(everything), abs=1e-9)
    assert out.es_moa == pytest.approx(out.es_in / (MOA_IN_PER_100YD * 1.0), abs=1e-9)
    assert out.center_dx_in == pytest.approx(np.mean([o.dx_in for o in everything]), abs=1e-9)
    assert out.center_dy_in == pytest.approx(np.mean([o.dy_in for o in everything]), abs=1e-9)
    assert out.center_dx_mil == pytest.approx(out.center_dx_in * 1000 / (100 * 36), abs=1e-9)
    assert out.mixed_loads is False and out.warnings == [] and out.includes_estimated is False
    first = [s for s in out.shots if s.group_id == groups[0].id]
    flat = [value for s in first for value in (s.dx_in, s.dy_in)]
    assert flat == pytest.approx([value for o in sets[0] for value in (o.dx_in, o.dy_in)])
    assert {s.group_id for s in out.shots} == {g.id for g in groups}


def test_one_group_aggregates_to_itself(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    pts = offsets(7)
    group = seed_group(conn, context=near, load=load_a, offsets=pts)
    out = client.post("/api/aggregate", json={"group_ids": [group.id]}).json()
    assert out["n"] == 10 and out["es_in"] == pytest.approx(brute_force_es(pts))


def test_mixed_loads_combine_with_a_warning_flag(client, conn, world) -> None:
    _, load_a, load_b, near, _ = world
    ids = [
        seed_group(conn, context=near, load=load_a, offsets=offsets(1)).id,
        seed_group(conn, context=near, load=load_b, offsets=offsets(2)).id,
    ]
    out = client.post("/api/aggregate", json={"group_ids": ids}).json()
    assert out["mixed_loads"] is True and out["n"] == 20
    assert out["warnings"] and "different loads" in out["warnings"][0]


def test_estimated_shots_set_the_includes_estimated_flag(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    origins = [ShotOrigin.DETECTED] * 9 + [ShotOrigin.ESTIMATED]
    group = seed_group(conn, context=near, load=load_a, offsets=offsets(4), origins=origins)
    out = client.post("/api/aggregate", json={"group_ids": [group.id]}).json()
    assert out["includes_estimated"] is True
    assert [s["origin"] for s in out["shots"]].count("estimated") == 1


def test_mixed_distances_are_a_422_with_a_message(client, conn, world) -> None:
    _, load_a, _, near, far = world
    ids = [
        seed_group(conn, context=near, load=load_a, offsets=offsets(1)).id,
        seed_group(conn, context=far, load=load_a, offsets=offsets(2)).id,
    ]
    body = error(
        client.post("/api/aggregate", json={"group_ids": ids}), 422, "invalid_mixed_distance"
    )
    assert "100 yd" in body["detail"] and "200 yd" in body["detail"]


def test_mixed_rifles_are_a_422(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    other = seed_rifle(conn, name="Other")
    other_load = seed_load(conn, other)
    other_context = seed_context(other, distance_yd=100)
    ids = [
        seed_group(conn, context=near, load=load_a, offsets=offsets(1)).id,
        seed_group(conn, context=other_context, load=other_load, offsets=offsets(2)).id,
    ]
    error(client.post("/api/aggregate", json={"group_ids": ids}), 422, "invalid_mixed_rifle")


def test_a_draft_group_is_a_409(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    draft = seed_group(conn, context=near, load=load_a, offsets=offsets(1), finalize=False)
    error(client.post("/api/aggregate", json={"group_ids": [draft.id]}), 409, "group_not_final")


def test_an_unknown_group_is_a_404(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    real = seed_group(conn, context=near, load=load_a, offsets=offsets(1))
    error(
        client.post("/api/aggregate", json={"group_ids": [real.id, 9999]}), 404, "group_not_found"
    )


@pytest.mark.parametrize(
    "body",
    [{}, {"group_ids": []}, {"group_ids": [1, 1]}, {"group_ids": [0]}, {"group_ids": [1], "x": 1}],
)
def test_invalid_aggregate_requests_are_a_422(client, body: dict) -> None:
    error(client.post("/api/aggregate", json=body), 422, "validation_error")


@pytest.mark.parametrize(
    ("purpose", "shots"),
    [
        ("zero_check", 1),
        ("zero_check", 2),
        ("zero_check", 10),
        ("zero_step2", 5),
        ("zero_step2", 9),
        ("zero_confirm", 9),
    ],
)
def test_masked_groups_are_refused_by_the_aggregate(
    client, conn, world, purpose: str, shots: int
) -> None:
    _, load_a, _, near, _ = world
    masked = seed_group(conn, context=near, load=load_a, offsets=offsets(5, shots), purpose=purpose)
    assert group_service.masks_poi(purpose, shots)
    body = error(
        client.post("/api/aggregate", json={"group_ids": [masked.id]}), 409, "masked_group"
    )
    assert not POI_WORDS.search(body["detail"].replace("point of impact", ""))


def test_a_masked_group_cannot_hide_inside_a_selection_with_unmasked_groups(
    client, conn, world
) -> None:
    _, load_a, _, near, _ = world
    general = seed_group(conn, context=near, load=load_a, offsets=offsets(1))
    masked = seed_group(
        conn, context=near, load=load_a, offsets=offsets(2, 5), purpose="zero_step2"
    )
    response = client.post("/api/aggregate", json={"group_ids": [general.id, masked.id]})
    error(response, 409, "masked_group")
    assert "mean_dx" not in response.text and "center_dx" not in response.text


@pytest.mark.parametrize(
    ("purpose", "shots"),
    [("zero_step2", 10), ("zero_confirm", 12), ("zero_step1", 3), ("general", 4)],
)
def test_unmasked_purposes_aggregate(client, conn, world, purpose: str, shots: int) -> None:
    _, load_a, _, near, _ = world
    group = seed_group(conn, context=near, load=load_a, offsets=offsets(5, shots), purpose=purpose)
    assert not group_service.masks_poi(purpose, shots)
    assert client.post("/api/aggregate", json={"group_ids": [group.id]}).status_code == 200


# ----------------------------------------------------------------------------------- saved aggregates


def three_groups(conn, context, load, seeds=(1, 2, 3)):
    return [seed_group(conn, context=context, load=load, offsets=offsets(seed)) for seed in seeds]


def save(client, ids, name="Three of ten"):
    return client.post("/api/aggregates", json={"name": name, "group_ids": ids})


def test_saving_three_ten_shot_groups_gives_a_201_with_the_pooled_statistics(
    client, conn, world
) -> None:
    rifle, load_a, _, near, _ = world
    groups = three_groups(conn, near, load_a)
    ids = [g.id for g in groups]
    response = save(client, list(reversed(ids)))
    assert response.status_code == 201, response.text
    out = SavedAggregateOut.model_validate(response.json())
    pooled = AggregateOut.model_validate(
        client.post("/api/aggregate", json={"group_ids": ids}).json()
    )
    assert out.n == 30 and out.group_count == 3 and out.tier == "tier30" and out.valid is True
    assert out.invalid_reason is None and out.group_ids == sorted(ids)
    assert (out.rifle_id, out.load_id, out.distance_yd) == (rifle.id, load_a.id, 100)
    assert out.es_moa == pytest.approx(pooled.es_moa, abs=1e-12)
    assert out.es_in == pytest.approx(pooled.es_in, abs=1e-12)
    assert out.public_id and out.id >= 1 and out.created_at
    assert len(out.shots) == 30 and out.name == "Three of ten"


def test_saved_aggregates_are_listed_filtered_by_load_and_deleted_without_touching_groups(
    client, conn, world
) -> None:
    _, load_a, load_b, near, _ = world
    first = save(client, [g.id for g in three_groups(conn, near, load_a)], "A set").json()
    second = save(
        client, [g.id for g in three_groups(conn, near, load_b, (4, 5, 6))], "B set"
    ).json()
    listed = client.get("/api/aggregates").json()
    assert [a["name"] for a in listed] == ["A set", "B set"]
    assert [a["id"] for a in client.get(f"/api/aggregates?load_id={load_b.id}").json()] == [
        second["id"]
    ]
    assert client.get("/api/aggregates?load_id=999").json() == []
    groups_before = client.get("/api/groups").json()
    assert client.delete(f"/api/aggregates/{first['id']}").status_code == 204
    assert [a["name"] for a in client.get("/api/aggregates").json()] == ["B set"]
    assert client.get("/api/groups").json() == groups_before
    error(client.delete(f"/api/aggregates/{first['id']}"), 404, "aggregate_not_found")
    error(client.delete("/api/aggregates/99999"), 404, "aggregate_not_found")


@pytest.mark.parametrize(
    "body",
    [
        {"group_ids": [1, 2]},
        {"name": "x"},
        {"name": "", "group_ids": [1, 2]},
        {"name": "   ", "group_ids": [1, 2]},
        {"name": "x", "group_ids": [1]},
        {"name": "x", "group_ids": [1, 1]},
        {"name": "x", "group_ids": [0, 2]},
        {"name": "x", "group_ids": [1, 2], "extra": 1},
        {"name": "x" * 500, "group_ids": [1, 2]},
        {"name": "x", "group_ids": list(range(1, 102))},
    ],
)
def test_invalid_save_requests_are_a_422_and_save_nothing(client, body: dict) -> None:
    error(client.post("/api/aggregates", json=body), 422, "validation_error")
    assert client.get("/api/aggregates").json() == []


def test_saving_applies_the_same_group_rules_as_the_overlay_and_leaves_no_row(
    client, conn, world
) -> None:
    _, load_a, load_b, near, far = world
    a1 = seed_group(conn, context=near, load=load_a, offsets=offsets(1))
    a2 = seed_group(conn, context=near, load=load_a, offsets=offsets(2))
    b1 = seed_group(conn, context=near, load=load_b, offsets=offsets(3))
    far1 = seed_group(conn, context=far, load=load_a, offsets=offsets(4))
    draft = seed_group(conn, context=near, load=load_a, offsets=offsets(5), finalize=False)
    masked = seed_group(
        conn, context=near, load=load_a, offsets=offsets(6, 5), purpose="zero_step2"
    )
    other = seed_rifle(conn, name="Other")
    other_group = seed_group(
        conn,
        context=seed_context(other, distance_yd=100),
        load=seed_load(conn, other),
        offsets=offsets(7),
    )
    error(save(client, [a1.id, b1.id]), 422, "invalid_mixed_load")
    error(save(client, [a1.id, far1.id]), 422, "invalid_mixed_distance")
    error(save(client, [a1.id, other_group.id]), 422, "invalid_mixed_rifle")
    error(save(client, [a1.id, draft.id]), 409, "group_not_final")
    body = error(save(client, [a1.id, masked.id]), 409, "masked_group")
    assert not POI_WORDS.search(body["detail"].replace("point of impact", ""))
    error(save(client, [a1.id, 99999]), 404, "group_not_found")
    assert client.get("/api/aggregates").json() == []
    assert save(client, [a1.id, a2.id]).status_code == 201


def test_a_masked_groups_offsets_never_appear_in_a_saved_aggregate_response(
    client, conn, world
) -> None:
    _, load_a, _, near, _ = world
    masked = seed_group(
        conn, context=near, load=load_a, offsets=offsets(2, 10), purpose="zero_confirm"
    )
    assert not group_service.masks_poi("zero_confirm", 10)
    ok = three_groups(conn, near, load_a, (11, 12))
    ids = [g.id for g in ok] + [masked.id]
    saved = save(client, ids).json()
    assert saved["valid"] is True
    with conn:
        repo_groups.update_group(conn, masked.id, purpose="zero_check")
    again = client.get("/api/aggregates").json()[0]
    assert again["valid"] is False and "zeroing group" in again["invalid_reason"]
    assert masked.id in again["group_ids"] and masked.id not in {
        s["group_id"] for s in again["shots"]
    }
    only_ok = client.post("/api/aggregate", json={"group_ids": [g.id for g in ok]}).json()
    assert again["n"] == only_ok["n"] == 20
    assert again["es_in"] == pytest.approx(only_ok["es_in"], abs=1e-12)
    assert again["center_dx_in"] == pytest.approx(only_ok["center_dx_in"], abs=1e-12)
    assert again["tier"] is None


def test_editing_the_notes_of_a_member_group_leaves_the_saved_aggregate_unchanged(
    client, conn, world
) -> None:
    _, load_a, _, near, _ = world
    groups = three_groups(conn, near, load_a)
    before = save(client, [g.id for g in groups]).json()
    assert client.patch(f"/api/groups/{groups[0].id}", json={"notes": "edited"}).is_success
    after = client.get("/api/aggregates").json()[0]
    assert after == before


def test_deleting_a_member_group_makes_the_saved_aggregate_invalid_not_an_error(
    client, conn, world
) -> None:
    _, load_a, _, near, _ = world
    groups = three_groups(conn, near, load_a, (1, 2, 3, 4))
    saved = save(client, [g.id for g in groups]).json()
    assert saved["tier"] == "tier30" and saved["n"] == 40
    with conn:
        conn.execute("DELETE FROM groups WHERE id = ?", (groups[0].id,))
    response = client.get("/api/aggregates")
    assert response.status_code == 200
    after = response.json()[0]
    assert after["valid"] is False and "deleted" in after["invalid_reason"]
    assert after["group_ids"] == sorted(g.id for g in groups[1:])
    assert after["n"] == 30 and after["group_count"] == 3 and after["tier"] is None
    assert client.delete(f"/api/aggregates/{saved['id']}").status_code == 204


def test_saving_the_exact_same_groups_again_is_a_409_and_creates_nothing(
    client, conn, world
) -> None:
    _, load_a, _, near, _ = world
    groups = three_groups(conn, near, load_a)
    ids = [g.id for g in groups]
    first = save(client, ids, "first").json()

    again = error(save(client, ids, "same set, other name"), 409, "duplicate_aggregate")
    shuffled = error(save(client, list(reversed(ids)), "shuffled"), 409, "duplicate_aggregate")

    assert '"first"' in again["detail"] and shuffled["code"] == "duplicate_aggregate"
    assert [a["id"] for a in client.get("/api/aggregates").json()] == [first["id"]]


def test_only_an_exact_set_is_a_duplicate_subsets_supersets_and_overlaps_are_allowed(
    client, conn, world
) -> None:
    _, load_a, _, near, _ = world
    g = three_groups(conn, near, load_a, (1, 2, 3, 4))
    assert save(client, [x.id for x in g[:3]], "abc").status_code == 201
    assert save(client, [x.id for x in g[:2]], "subset").status_code == 201
    assert save(client, [x.id for x in g], "superset").status_code == 201
    assert save(client, [x.id for x in g[1:]], "overlap").status_code == 201
    assert len(client.get("/api/aggregates").json()) == 4


def test_a_duplicate_is_allowed_again_once_the_original_is_deleted(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    ids = [g.id for g in three_groups(conn, near, load_a)]
    first = save(client, ids).json()
    error(save(client, ids), 409, "duplicate_aggregate")
    assert client.delete(f"/api/aggregates/{first['id']}").status_code == 204

    assert save(client, ids).status_code == 201


def test_the_same_member_set_in_the_service_is_refused_without_a_row(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    ids = [g.id for g in three_groups(conn, near, load_a)]
    analysis_service.save_aggregate(conn, "one", ids)

    with pytest.raises(analysis_service.AnalysisError) as caught:
        analysis_service.save_aggregate(conn, "two", ids)

    assert caught.value.code == "duplicate_aggregate"
    assert conn.execute("SELECT count(*) FROM aggregates").fetchone()[0] == 1


def move(client, groups, distance_yd: int) -> None:
    for group in groups:
        response = client.patch(f"/api/groups/{group.id}", json={"distance_yd": distance_yd})
        assert response.status_code == 200, response.text


def test_a_group_distance_edit_moves_the_saved_aggregates_distance_with_it(
    client, conn, world
) -> None:
    _, load_a, _, near, _ = world
    groups = three_groups(conn, near, load_a)
    saved = save(client, [g.id for g in groups]).json()
    assert saved["distance_yd"] == 100
    before = client.get("/api/aggregates").json()[0]

    move(client, groups, 300)
    after = client.get("/api/aggregates").json()[0]
    one = client.get(f"/api/aggregates?load_id={load_a.id}").json()[0]

    assert after["distance_yd"] == one["distance_yd"] == 300
    assert after["valid"] is True and after["tier"] == "tier30"
    pooled = client.post("/api/aggregate", json={"group_ids": [g.id for g in groups]}).json()
    assert after["es_moa"] == pytest.approx(pooled["es_moa"], abs=1e-12)
    assert after["es_in"] == pytest.approx(before["es_in"], abs=1e-12)
    assert after["es_moa"] < before["es_moa"], "the same inches are a smaller angle at 300 yd"


def test_a_saved_aggregate_whose_members_no_longer_share_a_distance_is_invalid_and_says_so(
    client, conn, world
) -> None:
    _, load_a, _, near, _ = world
    other = seed_context(world[0], distance_yd=100, date="2026-10-03")
    first = seed_group(conn, context=near, load=load_a, offsets=offsets(1))
    second = seed_group(conn, context=other, load=load_a, offsets=offsets(2))
    save(client, [first.id, second.id])

    move(client, [second], 300)
    out = client.get("/api/aggregates").json()[0]

    assert out["valid"] is False and out["tier"] is None
    assert "no longer at one distance" in out["invalid_reason"]
    assert "100, 300 yd" in out["invalid_reason"]
    assert out["n"] == 0


def test_moving_every_group_of_an_invalid_aggregate_to_one_distance_makes_it_valid_again(
    client, conn, world
) -> None:
    _, load_a, _, near, _ = world
    other = seed_context(world[0], distance_yd=100, date="2026-10-03")
    first = seed_group(conn, context=near, load=load_a, offsets=offsets(1))
    second = seed_group(conn, context=other, load=load_a, offsets=offsets(2))
    save(client, [first.id, second.id])
    move(client, [second], 300)
    assert client.get("/api/aggregates").json()[0]["valid"] is False

    move(client, [first], 300)
    out = client.get("/api/aggregates").json()[0]

    assert out["valid"] is True and out["distance_yd"] == 300


def test_the_distance_follows_the_groups_even_when_no_member_is_usable_any_more(
    client, conn, world
) -> None:
    _, load_a, _, near, _ = world
    groups = three_groups(conn, near, load_a)
    save(client, [g.id for g in groups])
    with conn:
        conn.execute("UPDATE groups SET status = 'draft', finalized_at = NULL")
    move(client, groups, 400)

    out = client.get("/api/aggregates").json()[0]

    assert out["valid"] is False and "no longer finalized" in out["invalid_reason"]
    assert out["distance_yd"] == 400


def test_the_analysis_script_no_longer_promises_that_a_saved_combination_counts() -> None:
    """/compare decides what counts (overlaps, mixed conditions); /analysis only says eligible."""
    script = (WEB / "static" / "js" / "analysis.js").read_text()

    assert "counts in the 30-shot comparison" not in script
    assert (
        "30+ shots: eligible for the 30-shot comparison (Compare shows whether it's counted)"
        in script
    )
    assert script.count("eligible for the 30-shot comparison") == 1
    template = (WEB / "templates" / "analysis.html").read_text()
    assert "counts in the 30-shot comparison" not in template
    assert (
        "A saved combination of 30 or more shots is eligible for the 30-shot comparison; "
        "Compare shows whether it&#39;s counted."
        in template
        or "A saved combination of 30 or more shots is eligible for the 30-shot comparison; "
        "Compare shows whether it's counted."
        in template
    )


def test_deleting_every_member_group_removes_the_saved_aggregate_cleanly(
    client, conn, world
) -> None:
    _, load_a, _, near, _ = world
    groups = three_groups(conn, near, load_a)
    save(client, [g.id for g in groups])
    with conn:
        conn.execute("DELETE FROM groups")
    out = client.get("/api/aggregates").json()
    assert len(out) == 1 and out[0]["valid"] is False and out[0]["n"] == 0
    assert out[0]["group_ids"] == [] and out[0]["shots"] == []


def test_deleting_the_groups_load_or_rifle_removes_its_saved_aggregates(
    client, conn, world
) -> None:
    rifle, load_a, _, near, _ = world
    save(client, [g.id for g in three_groups(conn, near, load_a)])
    with conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM rifles WHERE id = ?", (rifle.id,))
        conn.execute("DELETE FROM groups")
        conn.execute("DELETE FROM rifles WHERE id = ?", (rifle.id,))
        assert conn.execute("SELECT count(*) FROM aggregates").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM aggregate_groups").fetchone()[0] == 0


def test_a_saved_name_is_returned_verbatim_and_escaped_on_the_page(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    saved = save(client, [g.id for g in three_groups(conn, near, load_a)], XSS).json()
    assert saved["name"] == XSS and client.get("/api/aggregates").json()[0]["name"] == XSS


def test_saved_aggregate_writes_refuse_cross_origin_and_non_json(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    ids = [g.id for g in three_groups(conn, near, load_a)]
    evil = {"Origin": "http://evil.example"}
    assert (
        client.post(
            "/api/aggregates", json={"name": "x", "group_ids": ids}, headers=evil
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/aggregates", content=b"x", headers={"Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    saved = save(client, ids).json()
    assert client.delete(f"/api/aggregates/{saved['id']}", headers=evil).status_code == 403
    assert len(client.get("/api/aggregates").json()) == 1


def test_a_locked_database_is_a_503_busy_for_saved_aggregates(client, monkeypatch) -> None:
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(analysis_service, "list_saved", locked)
    response = client.get("/api/aggregates")
    error(response, 503, "busy")
    assert response.headers["retry-after"] == "1"


def test_saved_aggregate_routes_match_the_registry_and_do_not_import_the_repo(app) -> None:
    served = {
        (method.upper(), re.sub(r"\{\w+\}", "{id}", path))
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    mine = {
        ("POST", "/api/aggregates"),
        ("GET", "/api/aggregates"),
        ("DELETE", "/api/aggregates/{id}"),
    }
    registry = {(m, re.sub(r"\{\w+\}", "{id}", p)) for m, p in ROUTE_SCHEMAS}
    assert mine <= registry and mine <= served
    for source in (WEB / "routes").glob("*.py"):
        assert "repo_aggregates" not in source.read_text(), source.name


def test_the_analysis_page_has_the_save_form_and_the_script_posts_through_api_js(client) -> None:
    html = page(client, "/analysis")
    assert 'data-form="save-aggregate"' in html and "Save this combination" in html
    assert 'id="saved-list"' in html
    script = (WEB / "static" / "js" / "analysis.js").read_text()
    assert 'apiSend("POST", "/api/aggregates", { name, group_ids: getIds() })' in script
    assert 'apiGet("/api/aggregates")' in script and "fetch(" not in script
    assert 'apiSend("DELETE", `/api/aggregates/${target.id}`)' in script
    registry = {(m, re.sub(r"\{\w+\}", "{id}", p)) for m, p in ROUTE_SCHEMAS}
    calls = re.findall(
        r'api(?:Get|Send)\((?:"(GET|POST|PUT|PATCH|DELETE)",\s*)?[`"](/api/[^`"?]*)', script
    )
    assert calls
    for method, raw in calls:
        path = re.sub(r"\$\{[^}]*\}", "{id}", raw)
        assert (method or "GET", path) in registry, (method, path)


def test_the_page_scripts_parse_as_es_modules() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: the script syntax check was not run")
    for name in ("analysis.js", "compare.js"):
        result = subprocess.run(
            [node, "--check", "--input-type=module"],
            input=(WEB / "static" / "js" / name).read_text(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, (name, result.stderr)


# ----------------------------------------------------------------------------------- stats and detail


def test_stats_for_a_general_group_carry_direction_words(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    pts = [Offset(0.20, 0.30), Offset(0.40, 0.30), Offset(0.30, 0.10), Offset(0.30, 0.50)]
    group = seed_group(conn, context=near, load=load_a, offsets=pts)
    response = client.get(f"/api/groups/{group.id}/stats")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == FULL_KEYS
    stats = GroupStatsOut.model_validate(body)
    assert stats.n == 4 and stats.es_in == pytest.approx(brute_force_es(pts))
    assert stats.es_label == ES_LABEL == "smallest target every shot hit (center-to-center)"
    assert stats.mean_dx_in == pytest.approx(0.30) and stats.mean_dy_in == pytest.approx(0.30)
    assert stats.mean_dy_mil == pytest.approx(0.30 * 1000 / 3600)
    assert stats.poi_text == "0.1 mil high, 0.1 mil right"


def test_direction_text_covers_each_quadrant_and_the_centre() -> None:
    d = analysis_service.direction_text
    assert d(0.3, 0.2) == "0.2 mil high, 0.3 mil right"
    assert d(-0.3, -0.2) == "0.2 mil low, 0.3 mil left"
    assert d(0.0, 0.5) == "0.5 mil high"
    assert d(-0.04, 0.04) == "on the aim point"


@pytest.mark.parametrize(
    ("purpose", "shots"), [("zero_check", 2), ("zero_step2", 7), ("zero_confirm", 3)]
)
def test_stats_for_a_masked_group_are_size_only(
    client, conn, world, purpose: str, shots: int
) -> None:
    _, load_a, _, near, _ = world
    group = seed_group(conn, context=near, load=load_a, offsets=offsets(9, shots), purpose=purpose)
    response = client.get(f"/api/groups/{group.id}/stats")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == SIZE_KEYS
    SizeOnlyStatsOut.model_validate(body)
    assert body["n"] == shots
    assert not POI_WORDS.search(response.text.replace("smallest target every shot hit", ""))


def test_a_masked_group_at_ten_shots_gets_full_stats(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    group = seed_group(
        conn, context=near, load=load_a, offsets=offsets(9, 10), purpose="zero_step2"
    )
    assert set(client.get(f"/api/groups/{group.id}/stats").json()) == FULL_KEYS


def test_a_group_without_an_aim_gets_size_only_stats(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    draft = seed_group(
        conn, context=near, load=load_a, offsets=offsets(3, 4), aim=None, finalize=False
    )
    assert set(client.get(f"/api/groups/{draft.id}/stats").json()) == SIZE_KEYS


def test_a_group_with_no_shots_has_a_defined_409_for_stats(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    empty = seed_group(
        conn, context=near, load=load_a, offsets=[], finalize=False, shots_expected=5
    )
    error(client.get(f"/api/groups/{empty.id}/stats"), 409, "group_has_no_shots")
    detail = client.get(f"/api/groups/{empty.id}")
    assert detail.status_code == 200 and detail.json()["shots"] == []


def test_a_missing_group_is_a_404_for_detail_and_stats(client) -> None:
    error(client.get("/api/groups/999"), 404, "group_not_found")
    error(client.get("/api/groups/999/stats"), 404, "group_not_found")


def test_group_detail_conforms_to_the_schema_and_carries_offsets(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    pts = offsets(11, 5)
    group = seed_group(
        conn, context=near, load=load_a, offsets=pts, lot_code="LOT-9", notes="my notes"
    )
    response = client.get(f"/api/groups/{group.id}")
    assert response.status_code == 200
    detail = GroupDetailOut.model_validate(response.json())
    assert (
        detail.lot_code == "LOT-9" and detail.load_label == "Load A" and detail.distance_yd == 100
    )
    assert detail.notes == "my notes" and detail.status.value == "final"
    assert [s.dx_in for s in detail.shots] == pytest.approx([o.dx_in for o in pts])
    assert all(s.dx_mil is not None and s.dy_mil is not None for s in detail.shots)


@pytest.mark.parametrize(
    ("purpose", "shots"), [("zero_check", 2), ("zero_step2", 6), ("zero_confirm", 9)]
)
def test_group_detail_nulls_per_shot_offsets_but_keeps_positions_for_masked_groups(
    client, conn, world, purpose: str, shots: int
) -> None:
    _, load_a, _, near, _ = world
    group = seed_group(conn, context=near, load=load_a, offsets=offsets(12, shots), purpose=purpose)
    body = client.get(f"/api/groups/{group.id}").json()
    assert len(body["shots"]) == shots
    for shot in body["shots"]:
        assert shot["x_in"] is not None and shot["y_in"] is not None
        assert shot["dx_in"] is None and shot["dy_in"] is None
        assert shot["dx_mil"] is None and shot["dy_mil"] is None


@pytest.mark.parametrize(
    ("purpose", "shots"),
    [("zero_check", 2), ("zero_step2", 6), ("zero_confirm", 9), ("zero_check", 10)],
)
def test_masked_group_detail_has_a_null_aim_and_keeps_positions(
    client, conn, world, purpose: str, shots: int
) -> None:
    _, load_a, _, near, _ = world
    group = seed_group(conn, context=near, load=load_a, offsets=offsets(41, shots), purpose=purpose)
    body = client.get(f"/api/groups/{group.id}").json()
    assert "aim" in body and body["aim"] is None
    assert len(body["shots"]) == shots
    assert all(s["x_in"] is not None and s["y_in"] is not None for s in body["shots"])
    assert all(s["dx_in"] is None and s["dy_mil"] is None for s in body["shots"])
    GroupDetailOut.model_validate(body)
    assert page(client, f"/groups/{group.id}")


@pytest.mark.parametrize(
    ("purpose", "shots"),
    [("general", 4), ("zero_step1", 3), ("zero_step2", 10), ("zero_confirm", 12)],
)
def test_unmasked_group_detail_keeps_its_aim(client, conn, world, purpose: str, shots: int) -> None:
    _, load_a, _, near, _ = world
    group = seed_group(conn, context=near, load=load_a, offsets=offsets(42, shots), purpose=purpose)
    body = client.get(f"/api/groups/{group.id}").json()
    assert body["aim"] == {"x_in": 4.25, "y_in": 5.5}
    assert page(client, f"/groups/{group.id}")


SEEDED_AIM_X, SEEDED_AIM_Y = 7.123457, 2.654321  # seven digits: cannot collide with timestamps


def test_a_masked_groups_aim_cannot_be_recovered_from_the_detail_json(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    group = seed_group(
        conn,
        context=near,
        load=load_a,
        offsets=offsets(43, 5),
        purpose="zero_step2",
        aim=PlanePoint(SEEDED_AIM_X, SEEDED_AIM_Y),
    )
    response = client.get(f"/api/groups/{group.id}")
    assert response.json()["aim"] is None
    for seeded in ("7.123457", "2.654321"):
        assert seeded not in response.text


def test_the_review_editor_keeps_its_own_aim_for_a_masked_group(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    group = seed_group(
        conn,
        context=near,
        load=load_a,
        offsets=offsets(44, 5),
        purpose="zero_step2",
        aim=PlanePoint(7.5, 2.25),
        finalize=False,
    )
    editor = client.get(f"/groups/{group.id}/review")
    assert editor.status_code == 200
    state = json.loads(
        html_module.unescape(re.search(r'data-state="([^"]*)"', editor.text).group(1))
    )
    assert state["aim"] == {"x_in": 7.5, "y_in": 2.25}
    assert client.get(f"/api/groups/{group.id}").json()["aim"] is None


# ----------------------------------------------------------------------------------- list


def test_the_group_list_is_a_bare_array_in_creation_order(client, conn, world) -> None:
    _, load_a, load_b, near, far = world
    made = [
        seed_group(conn, context=near, load=load_a, offsets=offsets(1)),
        seed_group(conn, context=near, load=load_b, offsets=offsets(2), finalize=False),
        seed_group(conn, context=far, load=load_a, offsets=offsets(3)),
    ]
    response = client.get("/api/groups")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list) and [g["id"] for g in body] == [g.id for g in made]
    TypeAdapter(ROUTE_SCHEMAS[("GET", "/api/groups")].response).validate_python(body)
    GroupOut.model_validate(body[0])
    assert body[0]["lot_code"] == "LOT-1"


def test_the_group_list_filters(client, conn, world) -> None:
    rifle, load_a, load_b, near, far = world
    other_rifle = seed_rifle(conn, name="Other")
    other_load = seed_load(conn, other_rifle)
    other_context = seed_context(other_rifle)
    g1 = seed_group(conn, context=near, load=load_a, offsets=offsets(1))
    g2 = seed_group(conn, context=near, load=load_b, offsets=offsets(2), finalize=False)
    g3 = seed_group(conn, context=far, load=load_a, offsets=offsets(3))
    g4 = seed_group(conn, context=other_context, load=other_load, offsets=offsets(4))

    def ids(query: str) -> list[int]:
        response = client.get(f"/api/groups{query}")
        assert response.status_code == 200, response.text
        return [g["id"] for g in response.json()]

    assert ids(f"?load_id={load_a.id}") == [g1.id, g3.id]
    assert ids(f"?rifle_id={rifle.id}") == [g1.id, g2.id, g3.id]
    assert ids(f"?rifle_id={other_rifle.id}") == [g4.id]
    assert ids("?status=draft") == [g2.id]
    assert ids("?status=final") == [g1.id, g3.id, g4.id]
    assert ids(f"?rifle_id={rifle.id}&status=final&load_id={load_a.id}") == [g1.id, g3.id]
    assert ids("?rifle_id=9999") == []


@pytest.mark.parametrize(
    "query", ["?status=archived", "?session_id=1", "?rifle_id=abc", "?owner=1"]
)
def test_invalid_list_filters_are_a_422(client, query: str) -> None:
    error(client.get(f"/api/groups{query}"), 422, "validation_error")


def test_the_list_carries_no_statistics_or_offsets_for_any_group(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    seed_group(conn, context=near, load=load_a, offsets=offsets(1, 4), purpose="zero_check")
    text = client.get("/api/groups").text
    assert not re.search(r"dx_|dy_|mean_|poi_text|es_in|center_", text)


# ----------------------------------------------------------------------------------- search


def test_search_finds_group_and_legacy_notes_and_percent_is_literal(client, conn, world) -> None:
    _, load_a, _, near, far = world
    group = seed_group(
        conn, context=near, load=load_a, offsets=offsets(1, 3), notes="sighted in at 500 yards"
    )
    seed_group(conn, context=far, load=load_a, offsets=offsets(2, 3), notes="plain note")
    hits = client.get("/api/search", params={"q": "windy"}).json()
    assert [(h["kind"], h["id"]) for h in hits] == [("group", group.id)]
    SearchHitOut.model_validate(hits[0])
    assert "windy morning" in hits[0]["snippet"]
    assert [
        (h["kind"], h["id"]) for h in client.get("/api/search", params={"q": "SIGHTED"}).json()
    ] == [("group", group.id)]
    percent = client.get("/api/search", params={"q": "0%"}).json()
    assert [(h["kind"], h["id"]) for h in percent] == [("group", group.id)]
    assert client.get("/api/search", params={"q": "5%"}).json() == []
    assert client.get("/api/search", params={"q": "%"}).json()[0]["id"] == group.id
    assert client.get("/api/search", params={"q": "_"}).json() == []


def test_search_hits_are_groups_only_one_per_group_and_show_notes_verbatim(
    client, conn, world
) -> None:
    rifle, load_a, _, near, _ = world
    both = seed_group(
        conn,
        context=seed_context(rifle, notes=f"shared {XSS}"),
        load=load_a,
        offsets=offsets(1, 3),
        notes=f"shared {XSS}",
    )
    legacy_only = seed_group(
        conn,
        context=seed_context(rifle, notes=f"shared {XSS}"),
        load=load_a,
        offsets=offsets(2, 3),
    )
    hits = client.get("/api/search", params={"q": "shared"}).json()
    assert sorted((h["kind"], h["id"]) for h in hits) == sorted(
        [("group", both.id), ("group", legacy_only.id)]
    )
    assert all(XSS in h["snippet"] for h in hits)


def test_search_with_no_match_is_an_empty_array(client, world) -> None:
    response = client.get("/api/search", params={"q": "zzzz-nothing"})
    assert response.status_code == 200 and response.json() == []


@pytest.mark.parametrize(
    "params", [{}, {"q": ""}, {"q": "   "}, {"q": "x" * 201}, {"q": "a", "extra": "1"}]
)
def test_invalid_search_requests_are_a_422(client, params: dict) -> None:
    error(client.get("/api/search", params=params), 422, "validation_error")


# ----------------------------------------------------------------------------------- guards and errors


def test_the_aggregate_refuses_cross_origin_and_non_json_writes(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    group = seed_group(conn, context=near, load=load_a, offsets=offsets(1))
    body = {"group_ids": [group.id]}
    assert (
        client.post(
            "/api/aggregate", json=body, headers={"Origin": "http://evil.example"}
        ).status_code
        == 403
    )
    response = client.post("/api/aggregate", content=b"x", headers={"Content-Type": "text/plain"})
    assert response.status_code == 415


def test_a_locked_database_is_a_503_busy_with_a_retry_hint(client, monkeypatch) -> None:
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(analysis_service, "aggregate", locked)
    response = client.post("/api/aggregate", json={"group_ids": [1]})
    error(response, 503, "busy")
    assert response.headers["retry-after"] == "1"


def test_other_database_errors_are_not_disguised(app, monkeypatch) -> None:
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(analysis_service, "search_notes", broken)
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as lenient:
        error(lenient.get("/api/search", params={"q": "x"}), 500, "internal_error")


def test_the_routes_of_this_slice_match_the_registry_and_patch_is_not_registered_here(app) -> None:
    served = {
        (method.upper(), re.sub(r"\{\w+\}", "{id}", path))
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    mine = {
        ("GET", "/api/groups"),
        ("GET", "/api/groups/{id}"),
        ("GET", "/api/groups/{id}/stats"),
        ("POST", "/api/aggregate"),
        ("GET", "/api/search"),
    }
    assert mine <= set(ROUTE_SCHEMAS) and mine <= served
    source = (WEB / "routes" / "analysis.py").read_text()
    assert "@router.patch" not in source and "rga.db" not in source


# ----------------------------------------------------------------------------------- pages


def page(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    return response.text


def test_the_group_page_shows_the_plot_stats_label_poi_words_and_notes(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    pts = [Offset(0.20, 0.30), Offset(0.40, 0.30), Offset(0.30, 0.10), Offset(0.30, 0.50)]
    origins = [ShotOrigin.DETECTED] * 3 + [ShotOrigin.ESTIMATED]
    group = seed_group(
        conn, context=near, load=load_a, offsets=pts, origins=origins, notes="group note here"
    )
    html = page(client, f"/groups/{group.id}")
    assert ES_LABEL in html
    assert html.count('<circle class="shot ') == 4 and 'class="aim-mark"' in html
    assert "0.1 mil high, 0.1 mil right" in html and "includes estimated shots" not in html
    assert "windy morning 50% humidity" in html and "group note here" in html
    assert f'data-group-id="{group.id}"' in html and 'data-form="group-notes"' in html
    assert '<script type="module" src="/static/js/analysis.js">' in html


def test_the_group_page_uses_the_servers_statistics(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    pts = offsets(21, 10)
    group = seed_group(conn, context=near, load=load_a, offsets=pts)
    stats = client.get(f"/api/groups/{group.id}/stats").json()
    html = page(client, f"/groups/{group.id}")
    assert f"{stats['es_in']:.2f} in" in html and f"{stats['es_moa']:.2f} MOA" in html


@pytest.mark.parametrize(("purpose", "shots"), [("zero_check", 2), ("zero_step2", 6)])
def test_a_masked_group_page_shows_no_poi_aim_offsets_or_direction_text(
    client, conn, world, purpose: str, shots: int
) -> None:
    rifle = seed_rifle(conn, name="Plain Rifle")
    load = seed_load(conn, rifle, label="Plain load")
    context = seed_context(rifle, notes="plain former-session note")
    group = seed_group(
        conn, context=context, load=load, offsets=offsets(31, shots), purpose=purpose
    )
    html = page(client, f"/groups/{group.id}")
    body = html.split("<main", 1)[1]
    assert ES_LABEL in html and '<circle class="shot ' in html
    assert 'class="aim-mark"' not in html
    size_only = re.sub(r'<span class="muted es-mil"[^>]*>.*?</span>', "", body)
    assert not POI_WORDS.search(size_only.replace(ES_LABEL, "")), POI_WORDS.search(size_only)
    assert "Offset" not in html and "hidden for this zeroing group" in html
    circles = re.findall(r'<circle class="shot [a-z]+" cx="([-\d.]+)" cy="([-\d.]+)"', html)
    assert len(circles) == shots
    assert abs(sum(float(x) for x, _ in circles) / shots) < 0.1, (
        "plot must not be centred on the aim"
    )
    assert abs(sum(float(y) for _, y in circles) / shots) < 0.1, (
        "plot must not be centred on the aim"
    )


def test_the_group_page_escapes_user_text_and_shows_notes_verbatim(client, conn) -> None:
    rifle = seed_rifle(conn, name=XSS)
    load = seed_load(conn, rifle, label=XSS)
    context = seed_context(rifle, notes=XSS)
    group = seed_group(
        conn, context=context, load=load, offsets=offsets(1, 3), notes=XSS, lot_code=XSS
    )
    html = page(client, f"/groups/{group.id}")
    assert XSS not in html and html.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 4
    scripts = re.findall(r"<script\b[^>]*>", html)
    assert len(scripts) >= 2 and all('src="/static/js/' in tag for tag in scripts)


def test_a_missing_group_page_is_a_404_in_the_error_model(client) -> None:
    error(client.get("/groups/999"), 404, "group_not_found")


def test_notes_saved_through_the_patch_route_appear_on_the_page(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    group = seed_group(conn, context=near, load=load_a, offsets=offsets(1, 3))
    response = client.patch(f"/api/groups/{group.id}", json={"notes": "new <b>note</b>"})
    if response.status_code == 404 or response.status_code == 405:
        pytest.skip("PATCH /api/groups/{id} (T3.2) is not registered yet")
    assert response.status_code == 200
    assert "new &lt;b&gt;note&lt;/b&gt;" in page(client, f"/groups/{group.id}")


def test_the_analysis_page_has_the_picker_and_the_search(client) -> None:
    html = page(client, "/analysis")
    for needle in (
        'data-page="analysis"',
        'id="picker-body"',
        'id="aggregate-button"',
        'data-form="search"',
        'id="filter-rifle"',
    ):
        assert needle in html, needle
    assert '<script type="module" src="/static/js/analysis.js">' in html


def test_neither_page_mentions_mean_radius_or_best_n_or_uses_native_forms(
    client, conn, world
) -> None:
    _, load_a, _, near, _ = world
    group = seed_group(conn, context=near, load=load_a, offsets=offsets(1, 5))
    for path in ("/analysis", f"/groups/{group.id}"):
        html = page(client, path)
        assert not re.search(r"mean\s*radius|best[\s-]*(of|n)\b|group\s*reduc", html, re.IGNORECASE)
        for form in re.findall(r"<form\b[^>]*>", html):
            assert "action=" not in form and "method=" not in form
        assert not re.search(r"\sstyle\s*=|\son[a-z]+\s*=|javascript:", html)
        assert not re.search(r"<script(?![^>]*\bsrc=)", html)


def test_the_group_plot_centres_on_the_aim_and_scales_shots_to_the_bullet(
    client, conn, world
) -> None:
    _, load_a, _, near, _ = world
    group = seed_group(
        conn, context=near, load=load_a, offsets=[Offset(0.5, 0.25), Offset(-0.5, -0.25)]
    )
    html = page(client, f"/groups/{group.id}")
    circles = re.findall(
        r'<circle class="shot detected" cx="([-\d.]+)" cy="([-\d.]+)" r="([\d.]+)"', html
    )
    assert [(float(x), float(y)) for x, y, _ in circles] == [(50.0, -25.0), (-50.0, 25.0)]
    assert {float(r) for _, _, r in circles} == {13.2}


def test_the_script_is_served_and_writes_only_through_api_js(client) -> None:
    source = client.get("/static/js/analysis.js").text
    assert 'from "/static/js/api.js"' in source
    assert not re.search(
        r"\bfetch\s*\(|innerHTML|outerHTML|insertAdjacentHTML|document\.write|\.submit\(", source
    )
    assert 'apiSend("PATCH", `/api/groups/${groupId}`, { notes' in source
    assert 'apiSend("POST", "/api/aggregate"' in source
    assert (
        "/api/search?q=${encodeURIComponent(q)}" in source and "/api/groups?status=final" in source
    )
    assert "event.preventDefault()" in source and "textContent" in source
    assert not re.search(r"mean\s*radius|best[\s-]*(of|n)\b", source, re.IGNORECASE)


# ------------------------------------------------------------------ one masking rule (T3.11, B-43)

MASKED_CASES = [("zero_check", 2), ("zero_step2", 6), ("zero_step1", 5)]
OPEN_CASES = [("zero_step1", 3), ("general", 4), ("zero_step2", 10)]
AIM = {"x_in": 7.5, "y_in": 2.25}


def _seed_cases(conn, world, cases) -> list:
    _, load_a, _, near, _ = world
    return [
        seed_group(
            conn,
            context=near,
            load=load_a,
            offsets=offsets(60 + i, n),
            purpose=purpose,
            aim=PlanePoint(AIM["x_in"], AIM["y_in"]),
        )
        for i, (purpose, n) in enumerate(cases)
    ]


def test_the_group_list_flags_masked_groups_and_withholds_their_aim(client, conn, world) -> None:
    masked = _seed_cases(conn, world, MASKED_CASES)
    open_ = _seed_cases(conn, world, OPEN_CASES)

    rows = {g["id"]: g for g in client.get("/api/groups").json()}

    for group in masked:
        assert rows[group.id]["masked"] is True and rows[group.id]["aim"] is None
    for group in open_:
        assert rows[group.id]["masked"] is False and rows[group.id]["aim"] == AIM
    assert str(AIM["y_in"]) not in json.dumps([rows[g.id] for g in masked])
    for row in rows.values():
        GroupOut.model_validate(row)


def test_the_group_detail_flags_masked_groups_and_withholds_their_aim(client, conn, world) -> None:
    masked = _seed_cases(conn, world, MASKED_CASES)
    open_ = _seed_cases(conn, world, OPEN_CASES)

    for group in masked:
        body = client.get(f"/api/groups/{group.id}").json()
        assert body["masked"] is True and body["aim"] is None
        assert all(s["dx_in"] is None and s["dy_mil"] is None for s in body["shots"])
    for group in open_:
        body = client.get(f"/api/groups/{group.id}").json()
        assert body["masked"] is False and body["aim"] == AIM
        assert all(s["dx_in"] is not None for s in body["shots"])


def test_list_and_detail_agree_on_the_flag_for_every_purpose_and_size(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    purposes = ["general", "zero_step1", "zero_step2", "zero_confirm", "zero_check"]
    groups = [
        (
            purpose,
            n,
            seed_group(conn, context=near, load=load_a, offsets=offsets(n, n), purpose=purpose),
        )
        for purpose in purposes
        for n in (1, 3, 4, 9, 10)
    ]
    listed = {g["id"]: g["masked"] for g in client.get("/api/groups").json()}

    for purpose, n, group in groups:
        detail = client.get(f"/api/groups/{group.id}").json()["masked"]
        assert listed[group.id] is detail is group_service.masks_poi(purpose, n), (purpose, n)


def test_the_review_editor_payload_is_flagged_but_keeps_its_aim(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    group = seed_group(
        conn,
        context=near,
        load=load_a,
        offsets=offsets(44, 5),
        purpose="zero_step1",
        aim=PlanePoint(AIM["x_in"], AIM["y_in"]),
        finalize=False,
    )
    editor = client.get(f"/groups/{group.id}/review")
    state = json.loads(
        html_module.unescape(re.search(r'data-state="([^"]*)"', editor.text).group(1))
    )

    assert state["aim"] == AIM
    assert client.get(f"/api/groups/{group.id}").json()["masked"] is True


def test_analysis_js_has_no_masking_rule_of_its_own() -> None:
    source = (WEB / "static" / "js" / "analysis.js").read_text(encoding="utf-8")

    assert "box.disabled = group.masked;" in source
    assert "if (group.masked) purpose.appendChild" in source
    for forbidden in (
        "isMasked",
        "SHORT_ZERO",
        "zero_step",
        "zero_confirm",
        "zero_check",
        "shots_expected <",
        "shots_expected >",
    ):
        assert forbidden not in source, forbidden


def test_masked_follows_the_stored_shot_count_not_the_expected_count(client, conn, world) -> None:
    _, load_a, _, near, _ = world
    aim = PlanePoint(AIM["x_in"], AIM["y_in"])
    step1 = seed_group(
        conn,
        context=near,
        load=load_a,
        offsets=offsets(70, 5),
        purpose="zero_step1",
        shots_expected=3,
        finalize=False,
        aim=aim,
    )
    step2 = seed_group(
        conn,
        context=near,
        load=load_a,
        offsets=offsets(71, 10),
        purpose="zero_step2",
        shots_expected=6,
        finalize=False,
        aim=aim,
    )

    rows = {g["id"]: g for g in client.get("/api/groups").json()}

    assert (rows[step1.id]["masked"], rows[step1.id]["aim"]) == (True, None)
    assert (rows[step2.id]["masked"], rows[step2.id]["aim"]) == (False, AIM)
    assert client.get(f"/api/groups/{step1.id}").json()["masked"] is True
    assert client.get(f"/api/groups/{step2.id}").json()["masked"] is False
