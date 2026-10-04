"""T2: review editor slice (detect, fill-shortfall, review save, finalize, snap-aim, page)."""

from __future__ import annotations

import dataclasses
import html
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from rga.config import Settings
from rga.db import repo_groups
from rga.db.connection import connect
from rga.domain.types import Offset, PlanePoint, ReferenceKind
from rga.services import groups as group_service
from rga.services import review as review_service
from rga.vision.pipeline import build_working_image, propose_candidates, roi_to_working
from rga.vision.transform import apply_h
from rga.web import deps
from rga.web.app import create_app
from tests.support.live_server import run_live_server
from tests.support.seed import seed_context, seed_group, seed_load, seed_photo, seed_rifle
from tests.synth.generate import load_catalog, plane_to_image, render
from tools.eval_detection import hungarian

ORIGIN = "http://127.0.0.1:8765"
WEB = Path(__file__).resolve().parents[2] / "src" / "rga" / "web"
CATALOG = load_catalog()


@dataclass(frozen=True)
class Scene:
    """A rendered synthetic target, rectified with its truth reference."""

    truth: dict
    working: np.ndarray
    reference_json: str
    h_json: str
    ppi: float
    width: int
    height: int
    h_px_to_working: np.ndarray
    roi: tuple[int, int, int, int]
    expected: int


@cache
def scene(name: str) -> Scene:
    img, truth = render(CATALOG[name], 0)
    kind = ReferenceKind.CIRCLE_1IN
    radius = max(truth["reference"]["axes_px"]) / 2
    proposals = propose_candidates(img, kind)
    best = min(
        proposals,
        key=lambda p: math.dist(p.circle.center_px, truth["reference"]["ellipse_center_px"]),
    )
    assert math.dist(best.circle.center_px, truth["reference"]["ellipse_center_px"]) < 0.25 * radius
    working, frame, h = build_working_image(img, best.result)
    group = truth["groups"][0]
    x0, y0, x1, y1 = group["roi_px"]
    roi = roi_to_working(h, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], frame)
    result = best.result
    return Scene(
        truth=truth,
        working=working,
        reference_json=json.dumps({"kind": str(result.kind), "method": result.method}),
        h_json=json.dumps(np.asarray(result.h_px_to_plane).tolist()),
        ppi=frame.ppi,
        width=frame.width_px,
        height=frame.height_px,
        h_px_to_working=h,
        roi=roi,
        expected=len(group["hole_indices"]),
    )


def working_in(sc: Scene, plane_points) -> np.ndarray:
    """Truth plane (paper inches) points as working-plane inches."""
    return apply_h(sc.h_px_to_working, plane_to_image(sc.truth, plane_points)) / sc.ppi


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url=ORIGIN, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def conn(settings: Settings, client: TestClient) -> Iterator[sqlite3.Connection]:
    connection = connect(settings.db_path)
    yield connection
    connection.close()


@dataclass(frozen=True)
class World:
    rifle: object
    load: object
    context: object


@pytest.fixture
def world(conn: sqlite3.Connection) -> World:
    rifle = seed_rifle(conn, bullet_diameter_in=0.224)
    return World(rifle, seed_load(conn, rifle), seed_context(rifle, distance_yd=100))


def make_photo(conn, settings: Settings, name: str = "ragged_10x7", confirmed: bool = True):
    """A photo row with the scene's reference and its working image on disk."""
    sc = scene(name)
    photo = seed_photo(conn, confirmed=False)
    if confirmed:
        with conn:
            photo = repo_groups.update_photo_reference(
                conn,
                photo.id,
                reference_kind="circle_1in",
                reference_json=sc.reference_json,
                h_px_to_plane_json=sc.h_json,
                working_ppi=sc.ppi,
                working_w=sc.width,
                working_h=sc.height,
                axis_ratio=1.0,
                reference_confirmed=True,
            )
        path = settings.working_dir / f"{photo.id}.png"
        assert cv2.imwrite(str(path), sc.working)
    return photo


def make_group(conn, world: World, photo=None, expected: int = 10, roi=None, **kwargs):
    group = seed_group(
        conn,
        context=world.context,
        load=world.load,
        offsets=[],
        finalize=False,
        shots_expected=expected,
        photo=photo,
        roi_json=roi,
        **kwargs,
    )
    return group


def plane_shots(n: int, origin: str = "detected", x0: float = 4.0, y0: float = 5.0, step=0.4):
    return [
        {
            "x_in": x0 + i * step,
            "y_in": y0 + (i % 3) * 0.2,
            "origin": origin,
            "moved_by_user": False,
        }
        for i in range(n)
    ]


def put_review(client, group_id, shots, aim=(4.5, 5.5), expected=None, rejections=()):
    return client.put(
        f"/api/groups/{group_id}/review",
        json={
            "aim": None if aim is None else {"x_in": aim[0], "y_in": aim[1]},
            "shots": shots,
            "rejections": [
                {"x_in": x, "y_in": y, "reason": "not_a_bullet_hole"} for x, y in rejections
            ],
            "shots_expected": expected if expected is not None else len(shots),
        },
    )


def error_of(response) -> tuple[int, str]:
    assert set(response.json()) == {"detail", "code"}, response.text
    return response.status_code, response.json()["code"]


def roi_body(sc: Scene) -> dict:
    x0, y0, x1, y1 = sc.roi
    return {"roi_px": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}


def inside(outline_in: np.ndarray, ppi: float, x_in: float, y_in: float) -> bool:
    contour = np.rint(outline_in * ppi).astype(np.int32).reshape(-1, 1, 2)
    return cv2.pointPolygonTest(contour, (float(x_in * ppi), float(y_in * ppi)), False) >= 0


class TestDetect:
    def test_ragged_photo_proposes_ten_shots_with_three_estimated_inside_the_outline(
        self, client, conn, settings, world
    ):
        sc = scene("ragged_10x7")
        group = make_group(conn, world, make_photo(conn, settings))

        response = client.post(f"/api/groups/{group.id}/detect", json=roi_body(sc))

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ok"
        assert len(body["shots"]) == 10
        estimated = [s for s in body["shots"] if s["origin"] == "estimated"]
        assert len(estimated) == 3
        assert [s["seq"] for s in body["shots"]] == list(range(1, 11))
        outline = working_in(sc, sc.truth["ragged_outlines_in"][0])
        assert all(inside(outline, sc.ppi, s["x_in"], s["y_in"]) for s in estimated)
        assert body["frame"] == {"ppi": sc.ppi, "width_px": sc.width, "height_px": sc.height}
        assert body["bullet_diameter_in"] == 0.224
        assert all(s["dx_in"] is None and s["dy_mil"] is None for s in body["shots"])

    def test_detect_proposes_only_and_stores_the_region(self, client, conn, settings, world):
        sc = scene("ragged_10x7")
        group = make_group(conn, world, make_photo(conn, settings))

        client.post(f"/api/groups/{group.id}/detect", json=roi_body(sc))

        assert repo_groups.list_shots(conn, group.id) == []
        assert repo_groups.list_rejections(conn, group.id) == []
        stored = review_service.roi_from_json(repo_groups.get_group(conn, group.id).roi_json)
        assert stored == sc.roi

    def test_detect_without_a_region_uses_the_stored_one(self, client, conn, settings, world):
        sc = scene("ragged_10x7")
        group = make_group(conn, world, make_photo(conn, settings))
        first = client.post(f"/api/groups/{group.id}/detect", json=roi_body(sc)).json()

        again = client.post(f"/api/groups/{group.id}/detect", json={}).json()

        assert again["shots"] == first["shots"]

    def test_a_group_with_a_stored_region_is_detected_inside_it(
        self, client, conn, settings, world
    ):
        sc = scene("ragged_10x7")
        group = make_group(
            conn, world, make_photo(conn, settings), roi=review_service.roi_to_json(sc.roi)
        )

        body = client.post(f"/api/groups/{group.id}/detect", json={}).json()

        assert len(body["shots"]) == 10

    def test_no_holes_gives_no_shots_and_a_by_hand_message(self, client, conn, settings, world):
        group = make_group(conn, world, make_photo(conn, settings), expected=3)

        response = client.post(
            f"/api/groups/{group.id}/detect",
            json={"roi_px": {"x0": 5, "y0": 5, "x1": 80, "y1": 80}},
        )

        body = response.json()
        assert response.status_code == 200
        assert body["shots"] == []
        assert any("by hand" in m for m in body["messages"])

    def test_more_holes_than_expected_reports_too_many(self, client, conn, settings, world):
        group = make_group(conn, world, make_photo(conn, settings), expected=3)

        body = client.post(
            f"/api/groups/{group.id}/detect", json=roi_body(scene("ragged_10x7"))
        ).json()

        assert body["status"] == "too_many"
        assert any("delete detection errors or raise the shot count" in m for m in body["messages"])

    def test_several_groups_on_one_photo_each_detect_in_their_own_region(
        self, client, conn, settings, world
    ):
        sc = scene("ragged_10x7")
        photo = make_photo(conn, settings)
        near = make_group(conn, world, photo, lot_code="LOT-A")
        far = make_group(conn, world, photo, lot_code="LOT-B", expected=2)
        x0, y0, x1, y1 = sc.roi

        a = client.post(f"/api/groups/{near.id}/detect", json=roi_body(sc)).json()
        empty = client.post(
            f"/api/groups/{far.id}/detect",
            json={"roi_px": {"x0": 5, "y0": 5, "x1": 90, "y1": 90}},
        ).json()

        assert len(a["shots"]) == 10 and empty["shots"] == []
        assert review_service.roi_from_json(repo_groups.get_group(conn, near.id).roi_json) == sc.roi
        assert review_service.roi_from_json(repo_groups.get_group(conn, far.id).roi_json) == (
            5,
            5,
            90,
            90,
        )
        assert x1 > x0 and y1 > y0

    def test_a_region_beyond_the_photo_is_clamped_and_one_outside_it_is_refused(
        self, client, conn, settings, world
    ):
        sc = scene("ragged_10x7")
        group = make_group(conn, world, make_photo(conn, settings))
        x0, y0, _, _ = sc.roi

        clamped = client.post(
            f"/api/groups/{group.id}/detect",
            json={"roi_px": {"x0": x0, "y0": y0, "x1": sc.width + 500, "y1": sc.height + 500}},
        )
        outside = client.post(
            f"/api/groups/{group.id}/detect",
            json={"roi_px": {"x0": sc.width + 10, "y0": 0, "x1": sc.width + 100, "y1": 50}},
        )

        assert clamped.status_code == 200
        assert error_of(outside) == (422, "invalid_roi")

    def test_detect_errors_use_the_error_model(self, client, conn, settings, world):
        no_photo = make_group(conn, world)
        unconfirmed = make_group(conn, world, make_photo(conn, settings, confirmed=False))
        photo = make_photo(conn, settings)
        (settings.working_dir / f"{photo.id}.png").unlink()
        missing_file = make_group(conn, world, photo)
        done = make_group(conn, world, make_photo(conn, settings))
        with conn:
            repo_groups.replace_shots(conn, done.id, [])
            repo_groups.update_group(
                conn, done.id, status="final", aim_x_in=1.0, aim_y_in=1.0, finalized_at="2026-10-01"
            )

        def detect(group_id):
            return client.post(f"/api/groups/{group_id}/detect", json={})

        assert error_of(detect(no_photo.id)) == (409, "no_photo")
        assert error_of(detect(unconfirmed.id)) == (409, "reference_unconfirmed")
        assert error_of(detect(missing_file.id)) == (409, "working_image_missing")
        assert error_of(detect(done.id)) == (409, "group_final")
        assert error_of(detect(9999)) == (404, "group_not_found")

    @pytest.mark.parametrize(
        "body",
        [
            {"roi_px": {"x0": 10, "y0": 10, "x1": 10, "y1": 50}},
            {"roi_px": {"x0": -1, "y0": 0, "x1": 5, "y1": 5}},
            {"roi_px": {"x0": 0, "y0": 0, "x1": 5}},
            {"roi": [0, 0, 5, 5]},
        ],
    )
    def test_an_invalid_region_body_is_a_validation_error(
        self, client, conn, settings, world, body
    ):
        group = make_group(conn, world, make_photo(conn, settings))

        response = client.post(f"/api/groups/{group.id}/detect", json=body)

        assert error_of(response) == (422, "validation_error")

    def test_a_detector_failure_is_a_422_not_a_500(
        self, client, conn, settings, world, monkeypatch
    ):
        group = make_group(conn, world, make_photo(conn, settings))

        def boom(*args, **kwargs):
            raise ValueError("working must be a non-empty 2-D or 3-D image")

        monkeypatch.setattr(review_service, "analyze_group", boom)

        assert error_of(client.post(f"/api/groups/{group.id}/detect", json={})) == (
            422,
            "image_too_small",
        )


class TestFillShortfall:
    def test_fill_uses_the_stored_expected_count_and_region(self, client, conn, settings, world):
        sc = scene("ragged_10x7")
        group = make_group(conn, world, make_photo(conn, settings))
        client.post(f"/api/groups/{group.id}/detect", json=roi_body(sc))

        base = client.post(f"/api/groups/{group.id}/fill-shortfall").json()
        saved = put_review(client, group.id, plane_shots(2), expected=11)
        more = client.post(f"/api/groups/{group.id}/fill-shortfall").json()

        assert saved.status_code == 200
        assert len(base["shots"]) == 10 and len(more["shots"]) == 11
        assert sum(s["origin"] == "estimated" for s in base["shots"]) == 3
        assert sum(s["origin"] == "estimated" for s in more["shots"]) == 4
        assert more["status"] == "ok"

    def test_a_rejected_detection_is_not_proposed_again(self, client, conn, settings, world):
        sc = scene("ragged_10x7")
        group = make_group(conn, world, make_photo(conn, settings))
        first = client.post(f"/api/groups/{group.id}/detect", json=roi_body(sc)).json()
        victim = next(s for s in first["shots"] if s["origin"] == "detected")
        put_review(
            client,
            group.id,
            plane_shots(9),
            expected=10,
            rejections=[(victim["x_in"], victim["y_in"])],
        )

        refilled = client.post(f"/api/groups/{group.id}/fill-shortfall").json()

        reach = 0.224 / 2
        assert all(
            math.dist((s["x_in"], s["y_in"]), (victim["x_in"], victim["y_in"])) > reach
            for s in refilled["shots"]
            if s["origin"] == "detected"
        )
        assert len(refilled["shots"]) == 10
        assert sum(s["origin"] == "estimated" for s in refilled["shots"]) == 4

    def test_rejecting_an_estimated_shot_does_not_drop_the_ragged_blob(
        self, client, conn, settings, world
    ):
        sc = scene("ragged_10x7")
        group = make_group(conn, world, make_photo(conn, settings))
        first = client.post(f"/api/groups/{group.id}/detect", json=roi_body(sc)).json()
        estimated = next(s for s in first["shots"] if s["origin"] == "estimated")
        put_review(
            client,
            group.id,
            plane_shots(9),
            expected=10,
            rejections=[(estimated["x_in"], estimated["y_in"])],
        )

        refilled = client.post(f"/api/groups/{group.id}/fill-shortfall").json()

        refilled_estimated = [s for s in refilled["shots"] if s["origin"] == "estimated"]
        outline = working_in(sc, sc.truth["ragged_outlines_in"][0])
        assert len(refilled_estimated) == 3
        assert all(inside(outline, sc.ppi, s["x_in"], s["y_in"]) for s in refilled_estimated)
        assert len([s for s in refilled["shots"] if s["origin"] == "detected"]) == 7
        assert len(refilled["shots"]) == 10

    def test_fill_errors_and_denials(self, client, conn, settings, world):
        no_photo = make_group(conn, world)

        assert error_of(client.post(f"/api/groups/{no_photo.id}/fill-shortfall")) == (
            409,
            "no_photo",
        )
        assert error_of(client.post("/api/groups/9999/fill-shortfall")) == (404, "group_not_found")


class TestSaveReview:
    def test_a_review_is_stored_and_returned_with_its_aim_and_offsets(self, client, conn, world):
        group = make_group(conn, world, expected=3)
        shots = plane_shots(3)
        shots[1]["origin"] = "estimated"
        shots[1]["moved_by_user"] = True

        response = put_review(client, group.id, shots, aim=(4.5, 5.5), expected=3)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["aim"] == {"x_in": 4.5, "y_in": 5.5}
        assert body["shots_expected"] == 3 and body["status"] == "draft"
        assert [s["seq"] for s in body["shots"]] == [1, 2, 3]
        assert [s["origin"] for s in body["shots"]] == ["detected", "estimated", "detected"]
        assert body["shots"][1]["moved_by_user"] is True
        first = body["shots"][0]
        assert first["dx_in"] == pytest.approx(shots[0]["x_in"] - 4.5)
        assert first["dy_in"] == pytest.approx(5.5 - shots[0]["y_in"])
        assert first["dx_mil"] == pytest.approx(first["dx_in"] * 1000 / 3600)
        assert body["distance_yd"] == 100 and body["bullet_diameter_in"] == 0.224
        assert body["load_label"] == world.load.label

    def test_dragging_an_estimated_shot_keeps_its_origin_flags_it_and_changes_the_stats(
        self, client, conn, world
    ):
        group = make_group(conn, world, expected=3)
        shots = plane_shots(3)
        shots[2]["origin"] = "estimated"
        put_review(client, group.id, shots)
        before = group_service.group_stats(conn, group.id)
        shots[2].update(
            x_in=shots[2]["x_in"] + 2.0, y_in=shots[2]["y_in"] + 1.0, moved_by_user=True
        )

        body = put_review(client, group.id, shots).json()
        after = group_service.group_stats(conn, group.id)

        assert body["shots"][2]["origin"] == "estimated"
        assert body["shots"][2]["moved_by_user"] is True
        assert after.es_in > before.es_in
        assert after.includes_estimated is True

    def test_rejections_are_stored_once_and_the_flier_reason_is_refused(self, client, conn, world):
        group = make_group(conn, world, expected=2)
        shots = plane_shots(2)

        put_review(client, group.id, shots, rejections=[(1.5, 2.5)])
        body = put_review(client, group.id, shots, rejections=[(1.5, 2.5)]).json()
        bad = client.put(
            f"/api/groups/{group.id}/review",
            json={
                "aim": None,
                "shots": shots,
                "rejections": [{"x_in": 1.0, "y_in": 1.0, "reason": "flier"}],
                "shots_expected": 2,
            },
        )
        missing_reason = client.put(
            f"/api/groups/{group.id}/review",
            json={"shots": shots, "rejections": [{"x_in": 1.0, "y_in": 1.0}], "shots_expected": 2},
        )

        assert [(r["x_in"], r["y_in"], r["reason"]) for r in body["rejections"]] == [
            (1.5, 2.5, "not_a_bullet_hole")
        ]
        assert error_of(bad) == (422, "validation_error")
        assert error_of(missing_reason) == (422, "validation_error")
        assert len(repo_groups.list_rejections(conn, group.id)) == 1

    def test_no_silent_drops_refused_and_accepted_paths(self, client, conn, world):
        group = make_group(conn, world, expected=3)
        three = plane_shots(3)
        put_review(client, group.id, three)
        before = repo_groups.list_shots(conn, group.id)

        refused = put_review(client, group.id, three[:2], expected=3)
        assert error_of(refused) == (409, "shot_dropped_without_reason")
        assert repo_groups.list_shots(conn, group.id) == before
        assert repo_groups.list_rejections(conn, group.id) == []

        accepted = put_review(
            client,
            group.id,
            three[:2],
            expected=3,
            rejections=[(three[2]["x_in"], three[2]["y_in"])],
        )

        assert accepted.status_code == 200
        assert len(accepted.json()["shots"]) == 2
        assert len(accepted.json()["rejections"]) == 1

    @pytest.mark.parametrize(
        ("mutate", "code"),
        [
            (lambda b: b.update(shots_expected=0), "validation_error"),
            (lambda b: b.update(shots_expected=201), "validation_error"),
            (lambda b: b["shots"][0].update(origin="flier"), "validation_error"),
            (lambda b: b["shots"][0].update(x_in="x"), "validation_error"),
            (lambda b: b.update(extra=1), "validation_error"),
            (lambda b: b.pop("shots"), "validation_error"),
        ],
    )
    def test_malformed_bodies_are_validation_errors(self, client, conn, world, mutate, code):
        group = make_group(conn, world, expected=2)
        body = {"aim": None, "shots": plane_shots(2), "rejections": [], "shots_expected": 2}
        mutate(body)

        response = client.put(f"/api/groups/{group.id}/review", json=body)

        assert error_of(response) == (422, code)

    def test_review_errors_map_group_errors(self, client, conn, world):
        group = make_group(conn, world, expected=2)
        done = seed_group(conn, context=world.context, load=world.load, offsets=[Offset(0.1, 0.1)])

        assert error_of(put_review(client, 9999, plane_shots(1))) == (404, "group_not_found")
        assert error_of(put_review(client, done.id, plane_shots(1))) == (409, "group_final")
        assert (
            error_of(
                client.put(
                    f"/api/groups/{group.id}/review",
                    json={
                        "shots": plane_shots(1),
                        "shots_expected": 2,
                        "aim": {"x_in": 1e12, "y_in": 1},
                    },
                )
            )[0]
            == 422
        )

    def test_a_group_can_be_saved_without_an_aim_while_a_draft(self, client, conn, world):
        group = make_group(conn, world, expected=3)

        body = put_review(client, group.id, plane_shots(2), aim=None, expected=3).json()

        assert body["aim"] is None
        assert all(s["dx_in"] is None for s in body["shots"])


class TestMasking:
    """B-09/B-14/B-18: masked groups carry null per-shot offsets everywhere the editor shows."""

    @pytest.mark.parametrize(
        ("purpose", "n", "masked"),
        [
            ("zero_check", 2, True),
            ("zero_check", 10, True),
            ("zero_step2", 5, True),
            ("zero_confirm", 9, True),
            ("zero_step2", 10, False),
            ("zero_step1", 3, False),
            ("general", 4, False),
        ],
    )
    def test_review_response_and_page_state_follow_masks_poi(
        self, client, conn, world, purpose, n, masked
    ):
        group = make_group(conn, world, expected=n, purpose=purpose)

        body = put_review(client, group.id, plane_shots(n)).json()
        page = client.get(f"/groups/{group.id}/review")
        state = json.loads(html.unescape(re.search(r'data-state="([^"]*)"', page.text).group(1)))

        for shots in (body["shots"], state["shots"]):
            for shot in shots:
                values = (shot["dx_in"], shot["dy_in"], shot["dx_mil"], shot["dy_mil"])
                assert (values == (None,) * 4) is masked
                assert shot["x_in"] is not None and shot["y_in"] is not None

    def test_finalized_masked_group_is_still_masked(self, client, conn, world):
        group = make_group(conn, world, expected=2, purpose="zero_check")
        put_review(client, group.id, plane_shots(2))

        body = client.post(f"/api/groups/{group.id}/finalize").json()

        assert body["status"] == "final"
        assert all(s["dx_mil"] is None and s["dy_in"] is None for s in body["shots"])


class TestFinalize:
    def test_a_complete_group_is_finalized(self, client, conn, world):
        group = make_group(conn, world, expected=3)
        put_review(client, group.id, plane_shots(3))

        response = client.post(f"/api/groups/{group.id}/finalize")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "final" and body["finalized_at"]
        assert len(body["shots"]) == 3 and all(s["dx_mil"] is not None for s in body["shots"])
        assert (
            client.post(f"/api/groups/{group.id}/finalize").json()["finalized_at"]
            == body["finalized_at"]
        )
        assert error_of(put_review(client, group.id, plane_shots(3))) == (409, "group_final")

    def test_nine_of_ten_is_a_count_mismatch(self, client, conn, world):
        group = make_group(conn, world, expected=10)
        put_review(client, group.id, plane_shots(9), expected=10)

        response = client.post(f"/api/groups/{group.id}/finalize")

        assert error_of(response) == (409, "count_mismatch")
        assert "9 of 10 shots" in response.json()["detail"]
        assert repo_groups.get_group(conn, group.id).status == "draft"

    def test_eleven_of_ten_is_a_count_mismatch_with_delete_advice(self, client, conn, world):
        group = make_group(conn, world, expected=10)
        put_review(client, group.id, plane_shots(11), expected=10)

        response = client.post(f"/api/groups/{group.id}/finalize")

        assert error_of(response) == (409, "count_mismatch")
        assert "raise the shot count" in response.json()["detail"]

    def test_without_an_aim_it_is_refused(self, client, conn, world):
        group = make_group(conn, world, expected=3)
        put_review(client, group.id, plane_shots(3), aim=None)

        assert error_of(client.post(f"/api/groups/{group.id}/finalize")) == (409, "no_aim")

    def test_an_unconfirmed_reference_is_refused(self, client, conn, settings, world):
        group = make_group(conn, world, make_photo(conn, settings, confirmed=False), expected=3)
        put_review(client, group.id, plane_shots(3))

        assert error_of(client.post(f"/api/groups/{group.id}/finalize")) == (
            409,
            "reference_unconfirmed",
        )

    def test_unknown_group_is_a_404(self, client):
        assert error_of(client.post("/api/groups/9999/finalize")) == (404, "group_not_found")


class TestSnapAim:
    def test_a_click_near_the_aim_dot_snaps_to_its_centre(self, client, conn, settings, world):
        sc = scene("clean_circle1")
        photo = make_photo(conn, settings, "clean_circle1")
        truth_aim = apply_h(sc.h_px_to_working, [sc.truth["aim_px"]])[0]

        response = client.post(
            f"/api/photos/{photo.id}/snap-aim",
            json={"x_px": float(truth_aim[0] + 14), "y_px": float(truth_aim[1] - 11)},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["snapped"] is True
        assert math.dist((body["x_px"], body["y_px"]), truth_aim) < 0.05 * sc.ppi
        assert body["x_in"] == pytest.approx(body["x_px"] / sc.ppi)
        assert body["y_in"] == pytest.approx(body["y_px"] / sc.ppi)

    def test_a_click_on_blank_paper_is_echoed_unsnapped(self, client, conn, settings, world):
        photo = make_photo(conn, settings, "clean_circle1")

        body = client.post(
            f"/api/photos/{photo.id}/snap-aim", json={"x_px": 60.0, "y_px": 60.0}
        ).json()

        assert body["snapped"] is False
        assert (body["x_px"], body["y_px"]) == (60.0, 60.0)
        assert body["x_in"] == pytest.approx(60.0 / scene("clean_circle1").ppi)

    def test_errors(self, client, conn, settings):
        sc = scene("clean_circle1")
        photo = make_photo(conn, settings, "clean_circle1")
        unconfirmed = make_photo(conn, settings, confirmed=False)

        def snap(photo_id, x=100.0, y=100.0):
            return client.post(f"/api/photos/{photo_id}/snap-aim", json={"x_px": x, "y_px": y})

        assert error_of(snap(9999)) == (404, "photo_not_found")
        assert error_of(snap(unconfirmed.id)) == (409, "reference_unconfirmed")
        assert error_of(snap(photo.id, x=sc.width + 5.0)) == (422, "invalid_point")
        assert error_of(snap(photo.id, y=sc.height + 5.0)) == (422, "invalid_point")
        assert error_of(client.post(f"/api/photos/{photo.id}/snap-aim", json={"x_px": 1}))[0] == 422
        assert (
            error_of(client.post(f"/api/photos/{photo.id}/snap-aim", json={"x_px": -1, "y_px": 1}))[
                0
            ]
            == 422
        )
        (settings.working_dir / f"{photo.id}.png").unlink()
        assert error_of(snap(photo.id)) == (409, "working_image_missing")


class TestBusyAndDenials:
    def test_a_locked_database_is_a_503_busy_never_a_500(
        self, client, conn, world, settings, monkeypatch
    ):
        group = make_group(conn, world, expected=2)
        rival = connect(settings.db_path)
        rival.execute("BEGIN IMMEDIATE")

        def short_wait(path):
            connection = connect(path)
            connection.execute("PRAGMA busy_timeout = 0")
            return connection

        monkeypatch.setattr(deps, "connect", short_wait)
        try:
            saved = put_review(client, group.id, plane_shots(2))
            finalized = client.post(f"/api/groups/{group.id}/finalize")
        finally:
            rival.rollback()
            rival.close()

        assert error_of(saved) == (503, "busy")
        assert error_of(finalized) == (503, "busy")
        monkeypatch.undo()
        assert put_review(client, group.id, plane_shots(2)).status_code == 200

    def test_other_database_errors_are_not_reported_as_busy(self, client, conn, world, monkeypatch):
        group = make_group(conn, world, expected=2)

        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(group_service, "finalize", boom)

        response = client.post(f"/api/groups/{group.id}/finalize")

        assert response.status_code == 500 and response.json()["code"] == "internal_error"

    def test_every_write_refuses_a_cross_origin_request(self, client, conn, settings, world):
        group = make_group(conn, world, make_photo(conn, settings), expected=3)
        evil = {"Origin": "http://evil.example"}
        calls = [
            (
                "put",
                f"/api/groups/{group.id}/review",
                {"shots": plane_shots(3), "shots_expected": 3},
            ),
            ("post", f"/api/groups/{group.id}/finalize", None),
            ("post", f"/api/groups/{group.id}/detect", {}),
            ("post", f"/api/groups/{group.id}/fill-shortfall", None),
            ("post", f"/api/photos/{group.photo_id}/snap-aim", {"x_px": 5, "y_px": 5}),
        ]

        for method, path, body in calls:
            response = getattr(client, method)(
                path, headers=evil, **({"json": body} if body is not None else {})
            )
            assert response.status_code == 403, (method, path, response.text)
        assert repo_groups.list_shots(conn, group.id) == []

    def test_a_form_style_body_is_refused(self, client, conn, world):
        group = make_group(conn, world, expected=2)

        response = client.put(
            f"/api/groups/{group.id}/review",
            content="shots_expected=2",
            headers={"Content-Type": "text/plain"},
        )

        assert response.status_code == 415


class TestReviewPage:
    def _state(self, response) -> dict:
        match = re.search(r'data-state="([^"]*)"', response.text)
        assert match, response.text[:500]
        return json.loads(html.unescape(match.group(1)))

    def test_the_page_embeds_the_group_state_and_the_editor_controls(
        self, client, conn, settings, world
    ):
        photo = make_photo(conn, settings)
        group = make_group(conn, world, photo, notes="  keep <b>verbatim</b>  ")
        put_review(client, group.id, plane_shots(3), expected=10)

        response = client.get(f"/groups/{group.id}/review")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        state = self._state(response)
        assert state["id"] == group.id and len(state["shots"]) == 3
        assert state["image_url"] == f"/api/photos/{photo.id}/image?variant=working"
        assert (
            state["reference_confirmed"] is True
            and state["frame"]["ppi"] == scene("ragged_10x7").ppi
        )
        assert state["bullet_diameter_in"] == 0.224 and state["distance_yd"] == 100
        assert state["notes"] == "  keep <b>verbatim</b>  "
        for element in (
            'id="review-root"',
            'id="expected-input"',
            'id="shot-counter"',
            'id="finalize-button"',
            'id="detect-button"',
            'id="fill-button"',
            'id="aim-button"',
            'id="delete-button"',
            '<script type="module" src="/static/js/review.js">',
        ):
            assert element in response.text
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]

    def test_the_only_delete_wording_is_not_a_bullet_hole(self, client, conn, world):
        group = make_group(conn, world)
        text = client.get(f"/groups/{group.id}/review").text
        labels = re.findall(r"<button[^>]*>([^<]*)</button>", text)

        assert "Not a bullet hole" in labels
        assert sum(label == "Not a bullet hole" for label in labels) == 1
        assert not [
            label
            for label in labels
            if re.search(r"delete|remove|discard|flier|outlier|reject", label, re.IGNORECASE)
        ]
        scripts = (WEB / "static/js/review.js").read_text() + (
            WEB / "static/js/svg_editor.js"
        ).read_text()
        assert not re.search(r"""["'`][^"'`]*\b(flier|outlier|discard)\b""", scripts, re.IGNORECASE)

    def test_return_to_must_be_a_same_site_path(self, client, conn, world):
        group = make_group(conn, world)

        def state_for(target):
            return self._state(
                client.get(f"/groups/{group.id}/review", params={"return_to": target})
            )

        good = state_for("/zeroing/4")
        assert good["return_to"] == "/zeroing/4" and good["next_url"] == "/zeroing/4"
        for bad in ("//evil.example/x", "https://evil.example", "/\\evil.example", "/a\tb", "evil"):
            state = state_for(bad)
            assert state["return_to"] is None and state["next_url"] == f"/groups/{group.id}", bad
        response = client.get(f"/groups/{group.id}/review", params={"return_to": "//evil.example"})
        assert "evil" not in response.text
        assert 'id="back-link"' not in response.text
        with_back = client.get(f"/groups/{group.id}/review", params={"return_to": "/zeroing/4"})
        assert 'id="back-link" href="/zeroing/4"' in with_back.text

    def test_the_page_states_for_a_final_unphotographed_and_unconfirmed_group(
        self, client, conn, settings, world
    ):
        done = seed_group(conn, context=world.context, load=world.load, offsets=[Offset(0.1, 0.1)])
        bare = make_group(conn, world)
        unconfirmed = make_group(conn, world, make_photo(conn, settings, confirmed=False))

        final_state = self._state(client.get(f"/groups/{done.id}/review"))
        bare_state = self._state(client.get(f"/groups/{bare.id}/review"))
        open_state = self._state(client.get(f"/groups/{unconfirmed.id}/review"))

        assert final_state["status"] == "final"
        assert bare_state["image_url"] is None and bare_state["frame"] is None
        assert open_state["reference_confirmed"] is False and open_state["frame"] is None

    def test_the_new_lot_flag_reaches_the_page(self, client, conn, world):
        seed_group(
            conn, context=world.context, load=world.load, offsets=[Offset(0.1, 0.1)], lot_code="A"
        )
        fresh = make_group(conn, world, lot_code="B")

        assert self._state(client.get(f"/groups/{fresh.id}/review"))["new_lot"] is True

    def test_unknown_group_is_a_404_error_body(self, client):
        response = client.get("/groups/9999/review")

        assert error_of(response) == (404, "group_not_found")

    def test_page_text_has_no_point_of_impact_for_a_masked_group(self, client, conn, world):
        group = make_group(conn, world, expected=2, purpose="zero_check")
        put_review(client, group.id, plane_shots(2))

        text = client.get(f"/groups/{group.id}/review").text

        assert not re.search(r"point of impact|\bPOI\b|\bmil\b", text, re.IGNORECASE)


class TestChangeScaleReferenceState:
    """B-79: the page state says what the "Change scale reference" button does."""

    def _change(self, client, group_id: int):
        response = client.get(f"/groups/{group_id}/review")
        assert response.status_code == 200
        match = re.search(r'data-state="([^"]*)"', response.text)
        return json.loads(html.unescape(match.group(1)))["reference_change"], response.text

    def test_nothing_saved_goes_straight_to_the_reference_page(self, client, conn, settings, world):
        photo = make_photo(conn, settings)
        group = make_group(conn, world, photo, aim=None)

        change, _ = self._change(client, group.id)

        assert change == {"mode": "direct", "draft_groups": 0, "reason": None}

    def test_a_region_alone_is_not_saved_work(self, client, conn, settings, world):
        photo = make_photo(conn, settings)
        group = make_group(
            conn, world, photo, aim=None, roi=review_service.roi_to_json(scene("ragged_10x7").roi)
        )

        assert self._change(client, group.id)[0]["mode"] == "direct"

    def test_saved_shots_on_a_draft_group_need_a_confirmation(self, client, conn, settings, world):
        photo = make_photo(conn, settings)
        group = make_group(conn, world, photo, expected=3, aim=None)
        put_review(client, group.id, plane_shots(3), aim=None)

        change, _ = self._change(client, group.id)

        assert change == {"mode": "confirm", "draft_groups": 1, "reason": None}

    def test_a_saved_aim_alone_counts_and_every_group_on_the_photo_is_counted(
        self, client, conn, settings, world
    ):
        photo = make_photo(conn, settings)
        mine = make_group(conn, world, photo, expected=3, aim=None)
        aim_only = make_group(conn, world, photo, expected=3, aim=None)
        shots_only = make_group(conn, world, photo, expected=3, aim=None)
        make_group(conn, world, photo, expected=3, aim=None)
        put_review(client, aim_only.id, [], aim=(4.5, 5.5), expected=3)
        put_review(client, shots_only.id, plane_shots(2), aim=None, expected=3)

        change, _ = self._change(client, mine.id)

        assert change["mode"] == "confirm" and change["draft_groups"] == 2

    def test_a_finalized_group_on_the_photo_blocks_with_the_reason(
        self, client, conn, settings, world
    ):
        photo = make_photo(conn, settings)
        done = make_group(conn, world, photo, expected=3, aim=None)
        put_review(client, done.id, plane_shots(3))
        assert client.post(f"/api/groups/{done.id}/finalize").status_code == 200
        sibling = make_group(conn, world, photo, expected=3, aim=None)
        put_review(client, sibling.id, plane_shots(2), expected=3)

        for group in (done, sibling):
            change, _ = self._change(client, group.id)
            assert change["mode"] == "blocked"
            assert change["reason"] == "A finalized group uses this scale"

    def test_work_and_final_groups_on_another_photo_do_not_count(
        self, client, conn, settings, world
    ):
        mine = make_group(conn, world, make_photo(conn, settings), expected=3, aim=None)
        other_photo = make_photo(conn, settings)
        other = make_group(conn, world, other_photo, expected=3, aim=None)
        put_review(client, other.id, plane_shots(3))
        assert client.post(f"/api/groups/{other.id}/finalize").status_code == 200

        assert self._change(client, mine.id)[0]["mode"] == "direct"

    def test_a_group_without_a_photo_or_with_an_unconfirmed_reference_has_no_button_state(
        self, client, conn, settings, world
    ):
        no_photo = make_group(conn, world, aim=None)
        unconfirmed = make_group(conn, world, make_photo(conn, settings, confirmed=False), aim=None)

        assert self._change(client, no_photo.id)[0] is None
        assert self._change(client, unconfirmed.id)[0] is None

    def test_the_button_sits_above_the_shot_counter_and_the_panel_is_inline(
        self, client, conn, settings, world
    ):
        group = make_group(conn, world, make_photo(conn, settings), aim=None)

        text = client.get(f"/groups/{group.id}/review").text

        assert text.index('id="reference-change-button"') < text.index('id="shot-counter"')
        assert "Change scale reference" in text
        for element in (
            'id="reference-change-confirm"',
            'id="reference-change-continue"',
            'id="reference-change-cancel"',
            'id="reference-change-reason"',
        ):
            assert element in text

    def test_the_script_has_no_native_confirm_and_resets_through_the_route(self):
        source = (WEB / "static/js/review.js").read_text()
        assert not re.search(r"\b(?:window\.)?(?:confirm|alert|prompt)\s*\(", source)
        assert 'apiSend("POST", `/api/photos/${server.photo_id}/reference/reset`)' in source
        assert "/photos/${server.photo_id}/reference?group_id=${groupId}" in source
        assert "A finalized group uses this scale" not in source
        assert (
            "clears the saved shots and aim of ${count} draft group(s) on this photo. Continue?"
            in source
        )


class TestScriptContract:
    """The page script talks only to registered routes, through api.js."""

    def _routes(self, client) -> set[tuple[str, str]]:
        spec = client.app.openapi()
        return {
            (method.upper(), path)
            for path, operations in spec["paths"].items()
            for method in operations
        }

    def test_every_api_call_in_review_js_is_a_registered_route(self, client):
        source = (WEB / "static/js/review.js").read_text()
        calls = re.findall(r'apiSend\(\s*"([A-Z]+)",\s*[`"](/api/[^`"]*)[`"]', source)
        assert {(m, re.sub(r"\$\{[^}]*\}", "{id}", path)) for m, path in calls} == {
            ("PUT", "/api/groups/{id}/review"),
            ("POST", "/api/groups/{id}/detect"),
            ("POST", "/api/groups/{id}/fill-shortfall"),
            ("POST", "/api/photos/{id}/snap-aim"),
            ("POST", "/api/groups/{id}/finalize"),
            ("POST", "/api/photos/{id}/reference/reset"),
        }
        routes = self._routes(client)
        for method, path in calls:
            shaped = re.sub(r"\$\{[^}]*\}", "{id}", path)
            normalised = {
                (m, re.sub(r"\{[a-z_]+\}", "{id}", p)) for m, p in routes if p.startswith("/api/")
            }
            assert (method, shaped) in normalised, (method, path)

    def test_review_js_never_calls_fetch_or_writes_html(self):
        for name in ("review.js", "svg_editor.js"):
            source = (WEB / "static/js" / name).read_text()
            assert not re.search(
                r"\bfetch\(|innerHTML|outerHTML|insertAdjacentHTML|document\.write", source
            )

    def test_the_editor_draws_estimated_shots_dashed_and_labelled(self):
        source = (WEB / "static/js/svg_editor.js").read_text()
        css = (WEB / "static/css/app.css").read_text()

        assert '"est"' in source
        assert re.search(r"\.shot\.estimated\s*\{[^}]*stroke-dasharray", css)
        assert "radiusIn * ppi" in source


class TestServiceHelpers:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('{"x0": 1, "y0": 2, "x1": 30, "y1": 40}', (1, 2, 30, 40)),
            (None, None),
            ("", None),
            ("not json", None),
            ('{"x0": 1}', None),
            ('{"x0": 10, "y0": 2, "x1": 10, "y1": 40}', None),
            ('{"x0": "a", "y0": 2, "x1": 10, "y1": 40}', None),
            ("[1, 2, 3, 4]", None),
        ],
    )
    def test_roi_from_json(self, text, expected):
        assert review_service.roi_from_json(text) == expected

    def test_roi_json_round_trips(self):
        assert review_service.roi_from_json(review_service.roi_to_json((3, 4, 50, 60))) == (
            3,
            4,
            50,
            60,
        )


class TestZoomPanStatic:
    """T3.4z: zoom and pan are viewBox changes, CSP-clean, with their own module-scoped CSS."""

    def _files(self) -> dict[str, str]:
        return {
            name: (WEB / path).read_text()
            for name, path in {
                "editor": "static/js/svg_editor.js",
                "zoom": "static/js/svg_zoom.js",
                "review": "static/js/review.js",
                "css": "static/css/review.css",
                "page": "templates/review.html",
            }.items()
        }

    def test_zoom_is_a_viewbox_change_mapped_through_the_screen_matrix(self):
        files = self._files()
        editor, zoom = files["editor"], files["zoom"]

        assert 'setAttribute("viewBox"' in zoom
        assert "getScreenCTM().inverse()" in zoom
        assert re.search(r'addEventListener\(\s*"wheel"', zoom)
        assert "passive: false" in zoom
        assert "event.ctrlKey" in zoom
        for name in ("zoomBy", "fit", "zoomLevel", "getView"):
            assert name in zoom and name in editor
        assert "zoomToRegion" in editor
        assert "MAX_ZOOM = 16" in zoom
        assert 'from "/static/js/svg_zoom.js"' in editor and "createZoom(svg" in editor
        assert not re.search(r'addEventListener\(\s*"wheel"', editor), "no duplicated zoom logic"

    def test_the_image_lives_inside_the_svg_so_it_zooms_with_the_markers(self):
        files = self._files()

        assert 'svgElement("image"' in files["editor"]
        assert "<img" not in files["page"]
        assert "createEditor(els.svg" in files["review"] and "imageUrl" in files["review"]

    def test_markers_keep_a_constant_on_screen_stroke_and_labels_a_constant_size(self):
        files = self._files()

        assert re.search(
            r"\.review-editor \.shot,\s*\.review-editor \.aim-mark\s*\{[^}]*vector-effect:\s*non-scaling-stroke",
            files["css"],
        )
        assert "scale(${labelScale})" in files["editor"]

    def test_the_toolbar_and_stylesheet_are_wired_into_the_page(self, client, conn, world):
        group = make_group(conn, world)

        page = client.get(f"/groups/{group.id}/review").text
        css = client.get("/static/css/review.css")

        assert '<link rel="stylesheet" href="/static/css/review.css">' in page
        for element in (
            'id="zoom-in-button"',
            'id="zoom-out-button"',
            'id="zoom-fit-button"',
            'id="zoom-region-button"',
            'id="zoom-level"',
            'class="editor-wrap review-editor"',
        ):
            assert element in page
        assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")

    def test_nothing_in_the_zoom_code_writes_inline_style(self):
        for name, source in self._files().items():
            assert not re.search(r"setAttribute\(\s*[\"']style[\"']", source), name
            assert not re.search(r"\.style\s*[.\[=]|\.style\.|cssText", source), name
            assert not re.search(r"\sstyle\s*=", source), name
            assert not re.search(r"\bon[a-z]+\s*=\s*[\"']", source), name
            assert not re.search(
                r"https?://|//cdn", source.replace("http://www.w3.org/2000/svg", "")
            ), name

    def test_app_css_is_left_alone_and_review_css_has_no_external_assets(self):
        css = self._files()["css"]

        assert "url(" not in css and "@import" not in css


def _playwright_ready() -> bool:
    browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""))
    return browsers.is_dir() and any(browsers.glob("chromium*"))


@pytest.fixture
def browser_page(tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    sc = scene("ragged_10x7")
    with run_live_server() as server, sync_api.sync_playwright() as playwright:
        settings = Settings(data_dir=server.data_dir, port=server.port)
        connection = connect(settings.db_path)
        try:
            rifle = seed_rifle(connection, bullet_diameter_in=0.224)
            load = seed_load(connection, rifle)
            context = seed_context(rifle, distance_yd=100)
            photo = make_photo(connection, settings)
            group = make_group(
                connection,
                World(rifle, load, context),
                photo,
                roi=review_service.roi_to_json(sc.roi),
            )
        finally:
            connection.close()
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            problems: list[str] = []
            page.on("pageerror", lambda error: problems.append(str(error)))
            page.on(
                "console",
                lambda message: problems.append(message.text) if message.type == "error" else None,
            )
            page.goto(f"{server}/groups/{group.id}/review")
            page.locator("#editor-svg circle.shot").nth(9).wait_for()
            yield page, server, settings, group, sc, problems
        finally:
            browser.close()


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestZoomPanInARealBrowser:
    """T3.4z in Chromium against the real server: zoom, pan and drag accuracy at 4x and beyond."""

    @staticmethod
    def _view(page) -> dict:
        return page.evaluate(
            """() => {
              const svg = document.getElementById('editor-svg');
              const [x, y, w, h] = svg.getAttribute('viewBox').split(' ').map(Number);
              const rect = svg.getBoundingClientRect();
              return {x, y, w, h, left: rect.left, top: rect.top, width: rect.width, height: rect.height};
            }"""
        )

    @staticmethod
    def _under(view: dict, client_x: float, client_y: float) -> tuple[float, float]:
        return (
            view["x"] + (client_x - view["left"]) / view["width"] * view["w"],
            view["y"] + (client_y - view["top"]) / view["height"] * view["h"],
        )

    def test_the_region_is_framed_on_load_and_zoom_controls_work(self, browser_page):
        page, _, _, _, sc, problems = browser_page

        start = self._view(page)
        assert sc.width / start["w"] > 2
        assert page.inner_text("#zoom-level").startswith(f"{sc.width / start['w']:.1f}")
        page.click("#zoom-fit-button")
        full = self._view(page)
        assert (full["x"], full["y"], full["w"], full["h"]) == (0, 0, sc.width, sc.height)
        page.click("#zoom-region-button")
        again = self._view(page)
        assert again["w"] == pytest.approx(start["w"]) and again["x"] == pytest.approx(start["x"])
        page.click("#zoom-in-button")
        assert self._view(page)["w"] == pytest.approx(again["w"] / 1.5)
        page.click("#zoom-out-button")
        assert self._view(page)["w"] == pytest.approx(again["w"])
        assert problems == []

    def test_the_wheel_zooms_around_the_cursor_and_is_clamped(self, browser_page):
        page, _, _, _, sc, _ = browser_page
        page.click("#zoom-fit-button")
        view = self._view(page)
        cursor = (view["left"] + view["width"] * 0.6, view["top"] + view["height"] * 0.55)
        before = self._under(view, *cursor)

        page.mouse.move(*cursor)
        page.mouse.wheel(0, -400)
        zoomed = self._view(page)

        assert zoomed["w"] < view["w"] * 0.8
        after = self._under(zoomed, *cursor)
        assert after == pytest.approx(before, abs=2.0)
        for _ in range(20):
            page.mouse.wheel(0, -1500)
        assert sc.width / self._view(page)["w"] == pytest.approx(16, rel=1e-6)
        for _ in range(40):
            page.mouse.wheel(0, 2500)
        assert self._view(page)["w"] == pytest.approx(sc.width)
        assert (self._view(page)["x"], self._view(page)["y"]) == (0, 0)

    def test_dragging_empty_space_pans_without_adding_a_shot(self, browser_page):
        page, _, _, _, _, _ = browser_page
        page.click("#zoom-region-button")
        page.click("#zoom-in-button")
        before = self._view(page)
        count = page.inner_text("#shot-counter")
        free_x, free_y = before["left"] + 40, before["top"] + 40

        page.mouse.move(free_x, free_y)
        page.mouse.down()
        page.mouse.move(free_x + 60, free_y + 35, steps=6)
        page.mouse.up()

        after = self._view(page)
        scale = before["width"] / before["w"]
        assert after["w"] == pytest.approx(before["w"])
        assert after["x"] == pytest.approx(before["x"] - 60 / scale, abs=0.5)
        assert after["y"] == pytest.approx(before["y"] - 35 / scale, abs=0.5)
        assert page.inner_text("#shot-counter") == count

    def test_a_click_on_empty_space_still_adds_a_shot_at_the_clicked_point(self, browser_page):
        page, _, settings, group, sc, _ = browser_page
        page.click("#zoom-in-button")
        view = self._view(page)
        count_before = page.locator("#editor-svg circle.shot").count()
        target = (view["left"] + 30, view["top"] + view["height"] - 30)
        expected = self._under(view, *target)

        page.mouse.click(*target)
        page.click("#save-button")
        page.wait_for_selector("#flash:not([hidden])")

        assert page.locator("#editor-svg circle.shot").count() == count_before + 1
        conn = connect(settings.db_path)
        try:
            manual = [s for s in repo_groups.list_shots(conn, group.id) if s.origin == "manual"]
        finally:
            conn.close()
        assert len(manual) == 1
        assert manual[0].x_in * sc.ppi == pytest.approx(expected[0], abs=0.75)
        assert manual[0].y_in * sc.ppi == pytest.approx(expected[1], abs=0.75)

    def test_a_shot_dragged_at_four_times_zoom_saves_the_right_working_pixel_position(
        self, browser_page
    ):
        page, _, settings, group, sc, problems = browser_page
        page.click("#zoom-fit-button")
        while sc.width / self._view(page)["w"] < 4:
            page.click("#zoom-in-button")
        view = self._view(page)
        zoom = sc.width / view["w"]
        assert 4 <= zoom < 6.2
        shots = page.evaluate(
            """() => [...document.querySelectorAll('#editor-svg circle.shot.detected')].map((c) => {
              const r = c.getBoundingClientRect();
              return {cx: Number(c.getAttribute('cx')), cy: Number(c.getAttribute('cy')),
                      sx: r.left + r.width / 2, sy: r.top + r.height / 2};
            })"""
        )
        visible = [
            s
            for s in shots
            if view["left"] + 20 < s["sx"] < view["left"] + view["width"] - 60
            and view["top"] + 20 < s["sy"] < view["top"] + view["height"] - 40
        ]
        assert visible, "no detected shot is inside the 4x window"
        target = visible[0]
        scale = view["width"] / view["w"]
        drag_by = (37, 23)

        page.mouse.move(target["sx"], target["sy"])
        page.mouse.down()
        page.mouse.move(target["sx"] + drag_by[0], target["sy"] + drag_by[1], steps=8)
        page.mouse.up()
        page.click("#save-button")
        page.wait_for_selector("#flash:not([hidden])")

        expected = (target["cx"] + drag_by[0] / scale, target["cy"] + drag_by[1] / scale)
        conn = connect(settings.db_path)
        try:
            saved = repo_groups.list_shots(conn, group.id)
        finally:
            conn.close()
        moved = [s for s in saved if s.moved_by_user]
        assert len(moved) == 1
        assert moved[0].origin == "detected"
        assert moved[0].x_in * sc.ppi == pytest.approx(expected[0], abs=0.75)
        assert moved[0].y_in * sc.ppi == pytest.approx(expected[1], abs=0.75)
        unmoved = [s for s in saved if not s.moved_by_user]
        assert len(unmoved) == 9
        assert problems == []

    def test_marker_strokes_do_not_grow_with_zoom(self, browser_page):
        page, _, _, _, _, _ = browser_page
        probe = (
            "() => { const c = document.querySelector('#editor-svg circle.shot');"
            " const s = getComputedStyle(c);"
            " return {effect: s.vectorEffect, width: s.strokeWidth}; }"
        )

        page.click("#zoom-fit-button")
        wide = page.evaluate(probe)
        for _ in range(4):
            page.click("#zoom-in-button")
        deep = page.evaluate(probe)

        assert wide == deep
        assert wide["effect"] == "non-scaling-stroke"

    def test_the_est_label_keeps_its_on_screen_size(self, browser_page):
        page, _, _, _, _, _ = browser_page
        probe = (
            "() => { const t = document.querySelector('#editor-svg text.shot-label');"
            " const r = t.getBoundingClientRect(); return r.height; }"
        )

        page.click("#zoom-fit-button")
        small = page.evaluate(probe)
        for _ in range(3):
            page.click("#zoom-in-button")
        large = page.evaluate(probe)

        assert large == pytest.approx(small, rel=0.2)

    def test_a_dragged_shot_follows_the_pointer_while_dragging_at_zoom(self, browser_page):
        page, _, _, _, sc, _ = browser_page
        while sc.width / self._view(page)["w"] < 4:
            page.click("#zoom-in-button")
        view = self._view(page)
        shots = page.evaluate(
            """() => [...document.querySelectorAll('#editor-svg circle.shot.detected')].map((c) => {
              const r = c.getBoundingClientRect();
              return {sx: r.left + r.width / 2, sy: r.top + r.height / 2};
            })"""
        )
        target = next(
            s
            for s in shots
            if view["left"] + 20 < s["sx"] < view["left"] + view["width"] - 80
            and view["top"] + 20 < s["sy"] < view["top"] + view["height"] - 60
        )

        page.mouse.move(target["sx"], target["sy"])
        page.mouse.down()
        page.mouse.move(target["sx"] + 41, target["sy"] + 29, steps=7)
        centres = page.evaluate(
            """() => [...document.querySelectorAll('#editor-svg circle.shot.detected.selected')].map((c) => {
              const r = c.getBoundingClientRect();
              return [r.left + r.width / 2, r.top + r.height / 2];
            })"""
        )
        page.mouse.up()

        assert len(centres) == 1
        assert centres[0][0] == pytest.approx(target["sx"] + 41, abs=1.0)
        assert centres[0][1] == pytest.approx(target["sy"] + 29, abs=1.0)

    def test_panning_never_leaves_the_photo(self, browser_page):
        page, _, _, _, sc, _ = browser_page
        page.click("#zoom-region-button")
        view = self._view(page)
        for dx, dy in ((4000, 4000), (-4000, -4000), (4000, -4000)):
            page.mouse.move(view["left"] + 30, view["top"] + 30)
            page.mouse.down()
            page.mouse.move(view["left"] + 30 + dx / 8, view["top"] + 30 + dy / 8, steps=4)
            page.mouse.move(view["left"] + 30 + dx / 4, view["top"] + 30 + dy / 4, steps=4)
            page.mouse.up()
            now = self._view(page)
            assert now["x"] >= 0 and now["y"] >= 0
            assert (
                now["x"] + now["w"] <= sc.width + 1e-6 and now["y"] + now["h"] <= sc.height + 1e-6
            )
            assert now["w"] == pytest.approx(view["w"])

    def test_a_click_just_outside_a_shot_circle_still_selects_it_at_fit(self, browser_page):
        page, _, _, _, _, _ = browser_page
        page.click("#zoom-fit-button")
        count = page.locator("#editor-svg circle.shot").count()
        shot = page.evaluate(
            """() => { const c = document.querySelector('#editor-svg circle.shot.detected');
              const r = c.getBoundingClientRect();
              return {sx: r.left + r.width / 2, sy: r.top + r.height / 2, radius: r.width / 2}; }"""
        )

        page.mouse.click(shot["sx"] + shot["radius"] + 1.2, shot["sy"])

        assert page.locator("#editor-svg circle.shot").count() == count
        assert page.is_enabled("#delete-button")


def _client_merge(stored: list[dict], proposal: dict, replace_unsaved: bool = False) -> list[dict]:
    """What the page does with a proposal: replace unmoved estimated shots, keep every other.

    With `replace_unsaved` (a Detect inside a region) the unsaved, unmoved, non-manual shots of an
    earlier proposal go too; a stored dict is unsaved unless it carries `saved: True`.

    Each kept shot suppresses at most one proposed shot within reach, paired by the optimal
    one-to-one assignment (most pairs first, then least total distance), as in review.js.
    """

    def replaceable(s: dict) -> bool:
        if s["origin"] == "estimated" and not s.get("moved_by_user", False):
            return True
        return (
            replace_unsaved
            and not s.get("saved", False)
            and not s.get("moved_by_user", False)
            and s["origin"] != "manual"
        )

    kept = [s for s in stored if not replaceable(s)]
    reach = proposal["bullet_diameter_in"]
    candidates = [
        {
            "x_in": p["x_in"],
            "y_in": p["y_in"],
            "origin": p["origin"],
            "moved_by_user": False,
            "confidence": p["confidence"],
        }
        for p in proposal["shots"]
    ]
    distance = np.array(
        [
            [math.dist((k["x_in"], k["y_in"]), (c["x_in"], c["y_in"])) for c in candidates]
            for k in kept
        ]
    ).reshape(len(kept), len(candidates))
    cost = np.where(distance < reach, distance, NO_PAIR_COST)
    used = {j for i, j in hungarian(cost) if cost[i, j] < NO_PAIR_COST}
    fresh = [c for j, c in enumerate(candidates) if j not in used]
    return [*kept, *fresh]


NO_PAIR_COST = 1e6


def _as_payload(shots: list[dict]) -> list[dict]:
    return [
        {
            "x_in": s["x_in"],
            "y_in": s["y_in"],
            "origin": s["origin"],
            "moved_by_user": bool(s.get("moved_by_user", False)),
            "confidence": s.get("confidence"),
        }
        for s in shots
    ]


class TestPlaceholderSemantics:
    """G3-W5 / G3-W7 / B-44 at the API: unmoved estimated shots are placeholders."""

    def _detected_group(self, client, conn, settings, world):
        sc = scene("ragged_10x7")
        group = make_group(conn, world, make_photo(conn, settings))
        proposal = client.post(f"/api/groups/{group.id}/detect", json=roi_body(sc)).json()
        saved = put_review(client, group.id, _as_payload(proposal["shots"]), expected=10)
        assert saved.status_code == 200, saved.text
        return group, saved.json()

    def test_lowering_the_expected_count_and_refilling_never_dead_ends(
        self, client, conn, settings, world
    ):
        group, saved = self._detected_group(client, conn, settings, world)
        assert sum(s["origin"] == "estimated" for s in saved["shots"]) == 3
        stored = _as_payload(saved["shots"])
        put_review(client, group.id, stored, expected=9)

        proposal = client.post(f"/api/groups/{group.id}/fill-shortfall").json()
        merged = _client_merge(stored, proposal)
        after = put_review(client, group.id, _as_payload(merged), expected=9)
        finalized = client.post(f"/api/groups/{group.id}/finalize")

        assert len(merged) == 9
        assert after.status_code == 200, after.text
        assert sum(s["origin"] == "estimated" for s in after.json()["shots"]) == 2
        assert finalized.status_code == 200 and finalized.json()["status"] == "final"
        assert repo_groups.list_rejections(conn, group.id) == []

    def test_refilling_with_a_moved_estimated_shot_over_counts_without_a_409(
        self, client, conn, settings, world
    ):
        group, saved = self._detected_group(client, conn, settings, world)
        stored = _as_payload(saved["shots"])
        moved_index = next(i for i, s in enumerate(stored) if s["origin"] == "estimated")
        stored[moved_index].update(x_in=stored[moved_index]["x_in"] + 1.0, moved_by_user=True)
        put_review(client, group.id, stored, expected=9)
        proposal = client.post(f"/api/groups/{group.id}/fill-shortfall").json()
        merged = _client_merge(stored, proposal)

        over = put_review(client, group.id, _as_payload(merged), expected=9)
        refused = client.post(f"/api/groups/{group.id}/finalize")
        victim = next(s for s in merged if s.get("moved_by_user"))
        trimmed = [s for s in merged if s is not victim]
        without_reason = put_review(client, group.id, _as_payload(trimmed), expected=9)
        with_reason = put_review(
            client,
            group.id,
            _as_payload(trimmed),
            expected=9,
            rejections=[(victim["x_in"], victim["y_in"])],
        )
        done = client.post(f"/api/groups/{group.id}/finalize")

        assert len(merged) == 10 and over.status_code == 200, over.text
        assert error_of(refused) == (409, "count_mismatch")
        assert error_of(without_reason) == (409, "shot_dropped_without_reason")
        assert with_reason.status_code == 200 and len(with_reason.json()["shots"]) == 9
        assert done.status_code == 200
        assert len(repo_groups.list_rejections(conn, group.id)) == 1

    def test_two_co_located_estimated_shots_can_both_be_removed(self, client, conn, world):
        group = make_group(conn, world, expected=3)
        stacked = [
            {"x_in": 4.0, "y_in": 5.0, "origin": "detected", "moved_by_user": False},
            {"x_in": 5.1, "y_in": 5.1, "origin": "estimated", "moved_by_user": False},
            {"x_in": 5.1, "y_in": 5.1, "origin": "estimated", "moved_by_user": False},
        ]
        put_review(client, group.id, stacked, expected=3)

        one = put_review(client, group.id, stacked[:2], expected=3)
        none = put_review(client, group.id, stacked[:1], expected=3)

        assert one.status_code == none.status_code == 200
        assert len(none.json()["shots"]) == 1
        assert repo_groups.list_rejections(conn, group.id) == []

    def test_two_co_located_detected_shots_need_a_rejection_each(self, client, conn, world):
        group = make_group(conn, world, expected=3)
        stacked = [
            {"x_in": 4.0, "y_in": 5.0, "origin": "detected", "moved_by_user": False},
            {"x_in": 5.1, "y_in": 5.1, "origin": "detected", "moved_by_user": False},
            {"x_in": 5.1, "y_in": 5.1, "origin": "detected", "moved_by_user": False},
        ]
        put_review(client, group.id, stacked, expected=3)

        one_reason = put_review(client, group.id, stacked[:1], expected=3, rejections=[(5.1, 5.1)])
        two_reasons = put_review(
            client, group.id, stacked[:1], expected=3, rejections=[(5.1, 5.1), (5.1, 5.1)]
        )

        assert error_of(one_reason) == (409, "shot_dropped_without_reason")
        assert two_reasons.status_code == 200
        assert len(repo_groups.list_rejections(conn, group.id)) == 2

    def test_a_detected_shot_dropped_without_a_reason_is_still_refused(self, client, conn, world):
        group = make_group(conn, world, expected=2)
        shots = [
            {"x_in": 4.0, "y_in": 5.0, "origin": "detected", "moved_by_user": False},
            {"x_in": 4.5, "y_in": 5.0, "origin": "detected", "moved_by_user": False},
        ]
        put_review(client, group.id, shots, expected=2)

        refused = put_review(client, group.id, shots[:1], expected=2)

        assert error_of(refused) == (409, "shot_dropped_without_reason")
        assert len(repo_groups.list_shots(conn, group.id)) == 2


class TestFinalizeAimForMaskedGroups:
    """B-39 amended: the finalize response nulls a masked group's aim; the editor's PUT keeps it."""

    def test_masked_group_finalize_has_no_aim_but_the_put_response_and_database_keep_it(
        self, client, conn, world
    ):
        group = make_group(conn, world, expected=2, purpose="zero_check")

        saved = put_review(client, group.id, plane_shots(2), aim=(4.5, 5.5), expected=2).json()
        finalized = client.post(f"/api/groups/{group.id}/finalize").json()

        assert saved["masked"] is True and saved["aim"] == {"x_in": 4.5, "y_in": 5.5}
        assert finalized["masked"] is True and finalized["aim"] is None
        assert finalized["status"] == "final"
        stored = repo_groups.get_group(conn, group.id)
        assert (stored.aim_x_in, stored.aim_y_in) == (4.5, 5.5)

    def test_an_unmasked_group_finalize_keeps_the_aim(self, client, conn, world):
        group = make_group(conn, world, expected=3)
        put_review(client, group.id, plane_shots(3), aim=(4.5, 5.5), expected=3)

        finalized = client.post(f"/api/groups/{group.id}/finalize").json()

        assert finalized["masked"] is False
        assert finalized["aim"] == {"x_in": 4.5, "y_in": 5.5}

    def test_a_repeated_finalize_of_a_masked_group_still_nulls_the_aim(self, client, conn, world):
        group = make_group(conn, world, expected=2, purpose="zero_check")
        put_review(client, group.id, plane_shots(2), expected=2)
        client.post(f"/api/groups/{group.id}/finalize")

        again = client.post(f"/api/groups/{group.id}/finalize").json()

        assert again["aim"] is None


SHOT_KINDS_NAMED = [
    ("unsaved detected", {"origin": "detected", "saved": False, "moved_by_user": False}),
    ("unsaved split", {"origin": "split", "saved": False, "moved_by_user": False}),
    (
        "unmoved estimated (saved or not)",
        {"origin": "estimated", "saved": True, "moved_by_user": False},
    ),
    ("saved detected", {"origin": "detected", "saved": True, "moved_by_user": False}),
    ("moved detected", {"origin": "detected", "saved": False, "moved_by_user": True}),
    ("moved estimated", {"origin": "estimated", "saved": False, "moved_by_user": True}),
    ("manual unsaved", {"origin": "manual", "saved": False, "moved_by_user": False}),
    ("manual saved", {"origin": "manual", "saved": True, "moved_by_user": False}),
]
SHOT_KINDS = [shot for _, shot in SHOT_KINDS_NAMED]


class TestPlaceholderScript:
    def test_the_page_script_replaces_only_unmoved_estimated_shots(self):
        source = (WEB / "static/js/review.js").read_text()

        assert re.search(
            r'function isPlaceholder\(shot\)\s*\{\s*return shot\.origin === "estimated" && !shot\.moved_by_user;',
            source,
        )
        assert (
            "const replaceable = (s) => isPlaceholder(s) || "
            "(replaceUnsavedProposals && isUnsavedProposal(s));"
        ) in source
        assert "model.shots.filter((s) => !replaceable(s))" in source
        assert "isPlaceholder(shot) &&" in source

    @pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
    def test_saved_moved_and_manual_shots_are_never_replaced_by_either_kind_of_detect(self):
        source = (WEB / "static/js/review.js").read_text()
        functions = [
            re.search(rf"function {name}\(shot\) \{{.*?\n\}}", source, re.S)[0]
            for name in ("isPlaceholder", "isUnsavedProposal")
        ]
        predicate = re.search(r"const replaceable = .*?;\n", source)[0]
        script = (
            "\n".join(functions)
            + "\nfunction run(replaceUnsavedProposals) {\n"
            + predicate
            + "  return shots.map((s) => replaceable(s));\n}\n"
            + "const shots = "
            + json.dumps(SHOT_KINDS)
            + ";\nconsole.log(JSON.stringify([run(false), run(true)]));\n"
        )
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        plain, in_region = json.loads(result.stdout)

        names = [name for name, _ in SHOT_KINDS_NAMED]
        assert dict(zip(names, plain, strict=True)) == {
            "unsaved detected": False,
            "unsaved split": False,
            "unmoved estimated (saved or not)": True,
            "saved detected": False,
            "moved detected": False,
            "moved estimated": False,
            "manual unsaved": False,
            "manual saved": False,
        }
        assert dict(zip(names, in_region, strict=True)) == {
            "unsaved detected": True,
            "unsaved split": True,
            "unmoved estimated (saved or not)": True,
            "saved detected": False,
            "moved detected": False,
            "moved estimated": False,
            "manual unsaved": False,
            "manual saved": False,
        }

    def test_the_over_count_reason_names_how_many_to_remove(self):
        source = (WEB / "static/js/review.js").read_text()

        assert (
            "remove ${count - model.expected} with Not a bullet hole, or raise the expected count"
            in source
        )


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestReview21FlowInARealBrowser:
    """Real Chromium on `ragged_10x7`: lower the expected count, Fill shortfall, Save, Finalize."""

    @staticmethod
    def _count(page) -> str:
        return page.inner_text("#shot-counter")

    @staticmethod
    def _group_row(settings, group_id):
        conn = connect(settings.db_path)
        try:
            return repo_groups.get_group(conn, group_id), repo_groups.list_shots(conn, group_id)
        finally:
            conn.close()

    @staticmethod
    def _fill(page) -> None:
        page.click("#fill-button")
        page.locator("#fill-button:enabled").wait_for()

    @staticmethod
    def _save(page) -> None:
        page.click("#save-button")
        page.locator("#save-button:enabled").wait_for()

    def _set_expected(self, page, value: int) -> None:
        page.fill("#expected-input", str(value))
        page.dispatch_event("#expected-input", "change")

    def test_lowering_the_count_and_filling_never_produces_a_409(self, browser_page):
        page, server, settings, group, sc, problems = browser_page
        self._save(page)
        assert self._count(page) == "10 of 10 shots"

        self._set_expected(page, 9)
        self._fill(page)
        assert self._count(page) == "9 of 9 shots"
        assert page.is_enabled("#finalize-button")

        self._save(page)
        assert page.inner_text("#flash") == "Saved."
        page.click("#finalize-button")
        page.wait_for_url(f"{server}/groups/{group.id}")

        row, shots = self._group_row(settings, group.id)
        assert row.status == "final" and len(shots) == 9
        assert sum(s.origin == "estimated" for s in shots) == 2
        assert problems == []

    def test_an_over_count_disables_finalize_with_a_reason_and_recovers_without_a_409(
        self, browser_page
    ):
        page, server, settings, group, sc, problems = browser_page
        estimated = page.locator("#editor-svg circle.shot.estimated").first
        box = estimated.bounding_box()
        centre = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.move(*centre)
        page.mouse.down()
        page.mouse.move(centre[0] + 110, centre[1] - 40, steps=8)
        page.mouse.up()
        self._save(page)

        self._set_expected(page, 9)
        self._fill(page)

        assert self._count(page) == "10 of 9 shots"
        assert page.is_disabled("#finalize-button")
        assert "remove 1 with Not a bullet hole, or raise the expected count" in page.inner_text(
            "#finalize-reason"
        )
        self._save(page)
        assert page.inner_text("#flash") == "Saved."

        _, shots = self._group_row(settings, group.id)
        moved = next(s for s in shots if s.moved_by_user)
        target = page.evaluate(
            """([x, y]) => {
              const circles = [...document.querySelectorAll('#editor-svg circle.shot.estimated')];
              const best = circles.map((c) => ({c, d: Math.hypot(c.getAttribute('cx') - x, c.getAttribute('cy') - y)}))
                .sort((a, b) => a.d - b.d)[0].c;
              const r = best.getBoundingClientRect();
              return [r.left + r.width / 2, r.top + r.height / 2];
            }""",
            [moved.x_in * sc.ppi, moved.y_in * sc.ppi],
        )
        page.mouse.click(*target)
        page.click("#delete-button")
        page.locator("#shot-counter").filter(has_text="9 of 9 shots").wait_for()
        assert page.is_enabled("#finalize-button")
        self._save(page)
        assert page.inner_text("#flash") == "Saved."
        page.click("#finalize-button")
        page.wait_for_url(f"{server}/groups/{group.id}")

        row, shots = self._group_row(settings, group.id)
        assert row.status == "final" and len(shots) == 9
        conn = connect(settings.db_path)
        try:
            assert len(repo_groups.list_rejections(conn, group.id)) == 1
        finally:
            conn.close()
        assert problems == []


SIBLING_NUDGE_IN = 0.06


def _estimated_siblings(shots: list[dict]) -> tuple[int, int]:
    estimated = [i for i, s in enumerate(shots) if s["origin"] == "estimated"]
    assert len(estimated) == 2, "the scenario must hold exactly two estimated siblings"
    return estimated[0], estimated[1]


class TestFillAfterNudgingASibling:
    """G3c2-W3: a kept (nudged) estimated shot never hides its sibling's re-proposal."""

    @pytest.mark.parametrize("name", ["merged_pairs", "close_holes_tight_pair"])
    def test_refill_restores_the_count_after_a_nudge_toward_the_sibling(
        self, client, conn, settings, world, name
    ):
        sc = scene(name)
        group = make_group(
            conn,
            world,
            make_photo(conn, settings, name),
            expected=sc.expected,
            roi=review_service.roi_to_json(sc.roi),
        )
        first = client.post(f"/api/groups/{group.id}/detect", json={}).json()
        stored = _as_payload(first["shots"])
        moved, sibling = _estimated_siblings(stored)
        gap = math.dist(
            (stored[moved]["x_in"], stored[moved]["y_in"]),
            (stored[sibling]["x_in"], stored[sibling]["y_in"]),
        )
        assert gap == pytest.approx(first["bullet_diameter_in"], rel=1e-6)
        towards = (
            (stored[sibling]["x_in"] - stored[moved]["x_in"]) / gap,
            (stored[sibling]["y_in"] - stored[moved]["y_in"]) / gap,
        )
        stored[moved].update(
            x_in=stored[moved]["x_in"] + towards[0] * SIBLING_NUDGE_IN,
            y_in=stored[moved]["y_in"] + towards[1] * SIBLING_NUDGE_IN,
            moved_by_user=True,
        )
        assert put_review(client, group.id, stored, expected=sc.expected).status_code == 200

        proposal = client.post(f"/api/groups/{group.id}/fill-shortfall").json()
        merged = _client_merge(stored, proposal)
        after = put_review(client, group.id, _as_payload(merged), expected=sc.expected)

        assert len(merged) == sc.expected
        assert after.status_code == 200, after.text
        assert sum(s["origin"] == "estimated" for s in merged) == 2
        assert sum(bool(s.get("moved_by_user")) for s in merged) == 1
        assert client.post(f"/api/groups/{group.id}/finalize").status_code == 200

    def test_a_kept_shot_suppresses_only_its_nearest_proposed_shot(self):
        stored = [{"x_in": 1.0, "y_in": 1.0, "origin": "manual", "moved_by_user": False}]
        proposal = {
            "bullet_diameter_in": 0.224,
            "shots": [
                {"x_in": 1.05, "y_in": 1.0, "origin": "detected", "confidence": 1.0},
                {"x_in": 1.0, "y_in": 1.1, "origin": "detected", "confidence": 1.0},
            ],
        }
        merged = _client_merge(stored, proposal)
        assert len(merged) == 2
        assert {(s["x_in"], s["y_in"]) for s in merged} == {(1.0, 1.0), (1.0, 1.1)}

    def test_the_page_script_pairs_kept_and_proposed_shots_by_optimal_assignment(self):
        source = (WEB / "static/js/review.js").read_text()

        assert "function unmatchedProposals(kept, candidates, reach)" in source
        assert "function assignMinimum(cost)" in source
        assert "assignMinimum(cost)" in source.split("function assignMinimum")[0]
        assert "pairs.sort(" not in source
        assert "kept.some((k) => Math.hypot" not in source

    @pytest.mark.parametrize("name", ["merged_pairs", "close_holes_tight_pair"])
    @pytest.mark.parametrize("fraction", [0.6, 0.45, 0.75])
    def test_dragging_both_siblings_the_same_way_does_not_over_count(
        self, client, conn, settings, world, name, fraction
    ):
        sc = scene(name)
        group = make_group(
            conn,
            world,
            make_photo(conn, settings, name),
            expected=sc.expected,
            roi=review_service.roi_to_json(sc.roi),
        )
        first = client.post(f"/api/groups/{group.id}/detect", json={}).json()
        stored = _as_payload(first["shots"])
        a, b = _estimated_siblings(stored)
        d = first["bullet_diameter_in"]
        ux = (stored[b]["x_in"] - stored[a]["x_in"]) / d
        uy = (stored[b]["y_in"] - stored[a]["y_in"]) / d
        for index in (a, b):
            stored[index].update(
                x_in=stored[index]["x_in"] + ux * fraction * d,
                y_in=stored[index]["y_in"] + uy * fraction * d,
                moved_by_user=True,
            )
        assert put_review(client, group.id, stored, expected=sc.expected).status_code == 200

        proposal = client.post(f"/api/groups/{group.id}/fill-shortfall").json()
        merged = _client_merge(stored, proposal)

        assert len(merged) == sc.expected
        assert (
            put_review(client, group.id, _as_payload(merged), expected=sc.expected).status_code
            == 200
        )
        assert client.post(f"/api/groups/{group.id}/finalize").status_code == 200

    def test_the_optimal_assignment_beats_the_greedy_nearest_pair(self):
        stored = [
            {"x_in": 0.6, "y_in": 0.0, "origin": "manual", "moved_by_user": False},
            {"x_in": 1.6, "y_in": 0.0, "origin": "manual", "moved_by_user": False},
        ]
        proposal = {
            "bullet_diameter_in": 1.0,
            "shots": [
                {"x_in": 0.0, "y_in": 0.0, "origin": "detected", "confidence": 1.0},
                {"x_in": 1.0, "y_in": 0.0, "origin": "detected", "confidence": 1.0},
            ],
        }
        assert len(_client_merge(stored, proposal)) == 2


def _aim_group(conn, world, settings, name: str):
    sc = scene(name)
    group = make_group(
        conn,
        world,
        make_photo(conn, settings, name),
        expected=sc.expected,
        roi=review_service.roi_to_json(sc.roi),
        aim=None,
    )
    return sc, group


def _true_aim_in(sc: Scene, group_index: int = 0) -> np.ndarray:
    return working_in(sc, [sc.truth["groups"][group_index]["aim_in"]])[0]


class TestAimCandidates:
    """UF2 at the API: detect proposes aim points while the group has no aim; nothing is stored."""

    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("ragged_10x7", "paster_center"),
            ("clean_circle1", "paster_center"),
            ("black_dot_mixed", "dot_center"),
        ],
    )
    def test_detect_proposes_the_aim_mark_within_0_03_in(
        self, client, conn, settings, world, name, kind
    ):
        sc, group = _aim_group(conn, world, settings, name)

        body = client.post(f"/api/groups/{group.id}/detect", json={}).json()

        top = body["aim_candidates"][0]
        truth = _true_aim_in(sc)
        assert top["kind"] == kind
        assert math.dist((top["x_in"], top["y_in"]), truth) < 0.03

    def test_candidates_are_ranked_valid_and_never_stored(self, client, conn, settings, world):
        sc, group = _aim_group(conn, world, settings, "multi_dot_target")

        body = client.post(f"/api/groups/{group.id}/detect", json={}).json()
        stored = repo_groups.get_group(conn, group.id)

        found = body["aim_candidates"]
        assert 2 <= len(found) <= 5
        assert [c["score"] for c in found] == sorted((c["score"] for c in found), reverse=True)
        assert all(
            set(c) == {"kind", "x_in", "y_in", "score"} and 0 <= c["score"] <= 1 for c in found
        )
        assert {c["kind"] for c in found} <= {
            "paster_center",
            "dot_center",
            "diamond_center",
            "cross_center",
            "grid_intersection",
        }
        assert (stored.aim_x_in, stored.aim_y_in) == (None, None)

    def test_no_candidates_once_the_group_has_an_aim(self, client, conn, settings, world):
        sc = scene("ragged_10x7")
        group = make_group(
            conn,
            world,
            make_photo(conn, settings),
            roi=review_service.roi_to_json(sc.roi),
            aim=PlanePoint(4.0, 5.0),
        )

        body = client.post(f"/api/groups/{group.id}/detect", json={}).json()

        assert body["aim_candidates"] == []

    def test_fill_shortfall_never_proposes_aims(self, client, conn, settings, world):
        _, group = _aim_group(conn, world, settings, "ragged_10x7")

        body = client.post(f"/api/groups/{group.id}/fill-shortfall").json()

        assert body["aim_candidates"] == []

    def test_without_a_stored_region_the_proposed_shots_define_where_to_look(
        self, client, conn, settings, world
    ):
        sc = scene("ragged_10x7")
        group = make_group(conn, world, make_photo(conn, settings), aim=None)

        body = client.post(f"/api/groups/{group.id}/detect", json={}).json()

        assert body["shots"]
        top = body["aim_candidates"][0]
        assert math.dist((top["x_in"], top["y_in"]), _true_aim_in(sc)) < 0.03

    def test_a_blank_photo_region_has_no_candidates_and_still_detects(
        self, client, conn, settings, world
    ):
        sc = scene("ragged_10x7")
        group = make_group(conn, world, make_photo(conn, settings), expected=3, aim=None)

        body = client.post(
            f"/api/groups/{group.id}/detect",
            json={"roi_px": {"x0": 5, "y0": 5, "x1": 80, "y1": 80}},
        ).json()

        assert body["shots"] == [] and body["aim_candidates"] == []
        assert sc.roi  # the scene itself still has a region the first test searches

    def test_the_circle_reference_centre_is_the_plane_origin_in_working_px(
        self, conn, settings, world
    ):
        sc = scene("ragged_10x7")
        photo = make_photo(conn, settings)
        frame = review_service.group_view(conn, make_group(conn, world, photo, aim=None).id).frame
        sized = dataclasses.replace(
            photo, width_px=sc.truth["image_size"][0], height_px=sc.truth["image_size"][1]
        )

        centre = review_service._reference_center_px(sized, frame)

        expected = apply_h(
            sc.h_px_to_working, np.array([sc.truth["reference"]["ellipse_center_px"]])
        )[0]
        assert centre is not None
        assert math.dist(centre, expected) / sc.ppi < 0.03

    def test_a_grid_reference_has_no_centre_to_offer(self, conn, settings, world):
        photo = make_photo(conn, settings)
        grid = dataclasses.replace(photo, reference_kind="grid_1in")
        frame = review_service.group_view(conn, make_group(conn, world, photo, aim=None).id).frame

        assert review_service._reference_center_px(grid, frame) is None
        assert (
            review_service._reference_center_px(
                dataclasses.replace(photo, h_px_to_plane_json=None), frame
            )
            is None
        )


class TestAimSearchIsBestEffort:
    """FG3c.2 / G3c3-W1: nothing inside the aim search can turn detect into an error."""

    def test_detect_on_a_degenerate_manual_circle_is_200_with_no_candidates(
        self, client, conn, world
    ):
        rng = np.random.default_rng(0)
        image = rng.normal(200, 10, (600, 800, 3)).clip(0, 255).astype(np.uint8)
        _, jpeg = cv2.imencode(".jpg", image)
        photo_id = client.post(
            "/api/photos", files={"file": ("p.jpg", jpeg.tobytes(), "image/jpeg")}
        ).json()["id"]
        flat_arc = [
            {"x_px": 100, "y_px": 300},
            {"x_px": 400, "y_px": 294},
            {"x_px": 700, "y_px": 300},
        ]
        confirmed = client.post(
            f"/api/photos/{photo_id}/reference/confirm",
            json={"kind": "circle_1in", "manual": {"edge_points_px": flat_arc}},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert (confirmed.json()["photo"]["working_w"], confirmed.json()["photo"]["working_h"]) == (
            11,
            8,
        )
        group = make_group(conn, world, photo_id, expected=3, aim=None)

        plain = client.post(f"/api/groups/{group.id}/detect", json={})
        with_region = client.post(
            f"/api/groups/{group.id}/detect",
            json={"roi_px": {"x0": 0, "y0": 0, "x1": 11, "y1": 8}},
        )

        for response in (plain, with_region):
            assert response.status_code == 200, response.text
            assert response.json()["aim_candidates"] == []

    @pytest.mark.parametrize("error", [RuntimeError("boom"), ValueError("bad"), cv2.error("cv")])
    def test_any_failure_in_the_search_gives_no_candidates_and_a_log_line(
        self, client, conn, settings, world, monkeypatch, caplog, error
    ):
        _, group = _aim_group(conn, world, settings, "ragged_10x7")

        def failing(*_a, **_k):
            raise error

        monkeypatch.setattr(review_service, "propose_aims", failing)
        with caplog.at_level("WARNING", logger="rga.services.review"):
            response = client.post(f"/api/groups/{group.id}/detect", json={})

        assert response.status_code == 200, response.text
        assert response.json()["aim_candidates"] == [] and response.json()["shots"]
        assert any("aim proposals skipped" in r.getMessage() for r in caplog.records)


class TestAimCandidatesAvoidShots:
    """FG3c.2 / G3c3-W2: the service tells the search where the shots are."""

    def test_the_search_gets_every_proposed_shot_and_the_bullet_diameter(
        self, client, conn, settings, world, monkeypatch
    ):
        sc, group = _aim_group(conn, world, settings, "ragged_10x7")
        seen: dict = {}
        real = review_service.propose_aims

        def spy(*args, **kwargs):
            seen.update(kwargs)
            return real(*args, **kwargs)

        monkeypatch.setattr(review_service, "propose_aims", spy)
        body = client.post(f"/api/groups/{group.id}/detect", json={}).json()

        assert seen["bullet_diameter_in"] == pytest.approx(body["bullet_diameter_in"])
        points = {(round(x, 3), round(y, 3)) for x, y in seen["shots_px"]}
        assert {
            (round(s["x_in"] * sc.ppi, 3), round(s["y_in"] * sc.ppi, 3)) for s in body["shots"]
        } <= points
        reach = body["bullet_diameter_in"]
        assert all(
            math.dist((a["x_in"], a["y_in"]), (s["x_in"], s["y_in"])) >= reach
            for a in body["aim_candidates"]
            for s in body["shots"]
        )


class TestAimPageContract:
    def test_the_page_offers_use_next_and_place_it_yourself_and_never_sets_the_aim_itself(self):
        source = (WEB / "static/js/review.js").read_text()
        markup = (WEB / "templates/review.html").read_text()

        for control in ("aim-use-button", "aim-next-button", "aim-manual-button", "aim-suggestion"):
            assert f'id="{control}"' in markup
        assert "Use this aim" in markup and "Next" in markup and "Place it yourself" in markup
        assert "PRESELECT" not in source
        assert ".score" not in source
        assert (
            "model.suggestionIndex = -1;"
            in source.split("function suggestAims")[1].split("function useSuggestedAim")[0]
        )
        assert "suggestionIndex: -1," in source
        assert "function useSuggestedAim()" in source
        assert "suggestAims(proposal.aim_candidates" in source
        suggest = source[
            source.index("function suggestAims") : source.index("function useSuggestedAim")
        ]
        assert "model.aim =" not in suggest

    def test_the_editor_draws_labelled_non_interactive_rings(self):
        editor = (WEB / "static/js/svg_editor.js").read_text()
        css = (WEB / "static/css/review.css").read_text()

        assert "aim-candidate-label" in editor and "aimCandidates" in editor
        assert re.search(r"\.review-editor \.aim-candidate\s*\{[^}]*pointer-events:\s*none", css)
        assert re.search(
            r"\.review-editor \.aim-candidate\s*\{[^}]*vector-effect:\s*non-scaling-stroke", css
        )
        assert "style=" not in (WEB / "templates/review.html").read_text()


@contextmanager
def _live_review(
    scene_name: str,
    *,
    with_aim: bool,
    expected_shots: int,
    before_load=None,
    with_region: bool = True,
):
    """A real Chromium page on a group of `scene_name`, optionally with no aim set yet."""
    sync_api = pytest.importorskip("playwright.sync_api")
    sc = scene(scene_name)
    with run_live_server() as server, sync_api.sync_playwright() as playwright:
        settings = Settings(data_dir=server.data_dir, port=server.port)
        connection = connect(settings.db_path)
        try:
            rifle = seed_rifle(connection, bullet_diameter_in=0.224)
            load = seed_load(connection, rifle)
            context = seed_context(rifle, distance_yd=100)
            photo = make_photo(connection, settings, scene_name)
            group = make_group(
                connection,
                World(rifle, load, context),
                photo,
                expected=expected_shots,
                roi=review_service.roi_to_json(sc.roi) if with_region else None,
                **({} if with_aim else {"aim": None}),
            )
        finally:
            connection.close()
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            problems: list[str] = []
            page.on("pageerror", lambda error: problems.append(str(error)))
            page.on(
                "console",
                lambda message: problems.append(message.text) if message.type == "error" else None,
            )
            if before_load is not None:
                before_load(page)
            page.goto(f"{server}/groups/{group.id}/review")
            page.locator("#editor-svg circle.shot").nth(expected_shots - 1).wait_for()
            yield page, server, settings, group, sc, problems
        finally:
            browser.close()


def _scale_detect_scores(top_score: float):
    """A `before_load` hook that rewrites the detect response so the best aim scores `top_score`."""

    def hook(page):
        def handler(route):
            response = route.fetch()
            body = response.json()
            for rank, candidate in enumerate(body["aim_candidates"]):
                candidate["score"] = max(0.0, top_score - 0.05 * rank)
            route.fulfill(response=response, json=body)

        page.route("**/api/groups/*/detect", handler)

    return hook


def _group_after(settings: Settings, group_id: int):
    connection = connect(settings.db_path)
    try:
        return repo_groups.get_group(connection, group_id), repo_groups.list_shots(
            connection, group_id
        )
    finally:
        connection.close()


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestAimSuggestionsInARealBrowser:
    """UF2 in real Chromium: rings with labels, Use / Next / Place it yourself, nothing automatic."""

    def test_no_ring_is_preselected_and_the_aim_is_set_only_by_clicking_a_ring_then_use(self):
        with _live_review("ragged_10x7", with_aim=False, expected_shots=10) as (
            page,
            _server,
            settings,
            group,
            sc,
            problems,
        ):
            page.locator("#aim-suggestion:not([hidden])").wait_for()
            assert "none selected" in page.inner_text("#aim-suggestion-text")
            assert page.locator("#editor-svg circle.aim-candidate").count() == 1
            assert page.locator("#editor-svg circle.aim-candidate.active").count() == 0
            assert (
                "1 paster center"
                in page.locator("#editor-svg text.aim-candidate-label").text_content()
            )
            assert page.is_disabled("#aim-use-button") and page.is_enabled("#aim-next-button")
            assert page.inner_text("#aim-status") == "Aim point is not set."
            assert page.is_disabled("#finalize-button")
            assert _group_after(settings, group.id)[0].aim_x_in is None
            shots_before = page.locator("#editor-svg circle.shot").count()

            ring = page.locator("#editor-svg circle.aim-candidate").bounding_box()
            page.mouse.click(ring["x"] + ring["width"] / 2, ring["y"] + ring["height"] / 2)

            assert "Suggested aim 1 of 1: paster center" in page.inner_text("#aim-suggestion-text")
            assert page.locator("#editor-svg circle.aim-candidate.active").count() == 1
            assert page.locator("#editor-svg circle.shot").count() == shots_before
            assert page.is_enabled("#aim-use-button")
            assert page.inner_text("#aim-status") == "Aim point is not set."
            assert _group_after(settings, group.id)[0].aim_x_in is None

            page.click("#aim-use-button")

            assert page.locator("#aim-suggestion").is_hidden()
            assert page.locator("#editor-svg circle.aim-candidate").count() == 0
            assert page.inner_text("#aim-status") == "Aim point is set."
            assert page.is_enabled("#finalize-button")
            page.click("#save-button")
            page.locator("#save-button:enabled").wait_for()
            row, _ = _group_after(settings, group.id)
            assert math.dist((row.aim_x_in, row.aim_y_in), _true_aim_in(sc)) < 0.03
            assert problems == []

    def test_next_alone_selects_a_ring_and_use_then_sets_the_aim(self):
        with _live_review("ragged_10x7", with_aim=False, expected_shots=10) as (
            page,
            _server,
            settings,
            group,
            sc,
            problems,
        ):
            page.locator("#aim-suggestion:not([hidden])").wait_for()
            assert page.is_disabled("#aim-use-button")

            page.click("#aim-next-button")

            assert page.is_enabled("#aim-use-button")
            page.click("#aim-use-button")
            page.click("#save-button")
            page.locator("#save-button:enabled").wait_for()
            row, _ = _group_after(settings, group.id)
            assert math.dist((row.aim_x_in, row.aim_y_in), _true_aim_in(sc)) < 0.03
            assert problems == []

    def test_next_walks_the_rings_and_wraps_and_use_takes_the_one_shown(self):
        with _live_review("multi_dot_target", with_aim=False, expected_shots=5) as (
            page,
            _server,
            settings,
            group,
            sc,
            problems,
        ):
            page.locator("#aim-suggestion:not([hidden])").wait_for()
            assert page.locator("#editor-svg circle.aim-candidate").count() >= 2
            assert "none selected" in page.inner_text("#aim-suggestion-text")
            assert page.locator("#editor-svg circle.aim-candidate.active").count() == 0

            page.click("#aim-next-button")
            first = page.inner_text("#aim-suggestion-text")
            assert first.startswith("Suggested aim 1 of ")

            page.click("#aim-next-button")
            second = page.inner_text("#aim-suggestion-text")
            assert second.startswith("Suggested aim 2 of ") and second != first
            active = page.locator("#editor-svg circle.aim-candidate.active")
            assert active.count() == 1

            total = int(first.split(" of ")[1].split(":")[0])
            for _ in range(total - 1):
                page.click("#aim-next-button")
            assert page.inner_text("#aim-suggestion-text") == first

            page.click("#aim-use-button")
            page.click("#save-button")
            page.locator("#save-button:enabled").wait_for()
            row, _ = _group_after(settings, group.id)
            assert math.dist((row.aim_x_in, row.aim_y_in), _true_aim_in(sc)) < 0.03
            assert problems == []

    def test_place_it_yourself_clears_the_rings_and_leaves_manual_marking_to_the_user(self):
        with _live_review("ragged_10x7", with_aim=False, expected_shots=10) as (
            page,
            _server,
            settings,
            group,
            sc,
            problems,
        ):
            page.locator("#aim-suggestion:not([hidden])").wait_for()

            page.click("#aim-manual-button")

            assert page.locator("#aim-suggestion").is_hidden()
            assert page.locator("#editor-svg circle.aim-candidate").count() == 0
            assert "Click on the aim mark" in page.inner_text("#editor-hint")
            assert page.inner_text("#aim-status") == "Aim point is not set."
            assert _group_after(settings, group.id)[0].aim_x_in is None
            assert problems == []

    def test_a_manual_aim_click_still_snaps_and_dismisses_the_suggestion(self):
        with _live_review("ragged_10x7", with_aim=False, expected_shots=10) as (
            page,
            _server,
            _settings,
            _group,
            sc,
            problems,
        ):
            page.locator("#aim-suggestion:not([hidden])").wait_for()
            page.click("#aim-button")
            ring = page.locator("#editor-svg circle.aim-candidate").first.bounding_box()
            page.mouse.click(ring["x"] + ring["width"] / 2 + 4, ring["y"] + ring["height"] / 2)
            page.locator("#aim-suggestion[hidden]").wait_for(state="attached")

            assert page.inner_text("#aim-status") == "Aim point is set."
            assert page.locator("#aim-suggestion").is_hidden()
            assert page.locator("#editor-svg circle.aim-candidate").count() == 0
            assert page.is_enabled("#finalize-button")
            page.click("#save-button")
            page.locator("#save-button:enabled").wait_for()
            row, _ = _group_after(_settings, _group.id)
            assert math.dist((row.aim_x_in, row.aim_y_in), _true_aim_in(sc)) < 0.05
            assert problems == []


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestSiblingNudgeInARealBrowser:
    """G3c2-W3 in real Chromium: nudge one estimated sibling toward the other, then Fill."""

    @pytest.mark.parametrize(
        ("name", "expected"), [("merged_pairs", 12), ("close_holes_tight_pair", 8)]
    )
    def test_fill_after_a_nudge_toward_the_sibling_restores_the_count(self, name, expected):
        with _live_review(name, with_aim=True, expected_shots=expected) as (
            page,
            server,
            settings,
            group,
            sc,
            problems,
        ):
            assert page.inner_text("#shot-counter") == f"{expected} of {expected} shots"
            boxes = page.evaluate(
                """() => [...document.querySelectorAll('#editor-svg circle.shot.estimated')].map((c) => {
                  const r = c.getBoundingClientRect();
                  return [r.left + r.width / 2, r.top + r.height / 2];
                })"""
            )
            assert len(boxes) == 2
            (ax, ay), (bx, by) = boxes
            page.mouse.move(ax, ay)
            page.mouse.down()
            page.mouse.move(ax + (bx - ax) * 0.45, ay + (by - ay) * 0.45, steps=6)
            page.mouse.up()
            assert page.locator("#editor-svg circle.shot.estimated").count() == 2

            page.click("#fill-button")
            page.locator("#fill-button:enabled").wait_for()

            assert page.inner_text("#shot-counter") == f"{expected} of {expected} shots"
            assert page.locator("#editor-svg circle.shot.estimated").count() == 2
            assert page.is_enabled("#finalize-button")
            page.click("#save-button")
            page.locator("#save-button:enabled").wait_for()
            assert page.inner_text("#flash") == "Saved."
            page.click("#finalize-button")
            page.wait_for_url(f"{server}/groups/{group.id}")
            row, shots = _group_after(settings, group.id)
            assert row.status == "final" and len(shots) == expected
            assert problems == []


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestNoPreselectionInARealBrowser:
    """B-58 / T4.0a: whatever the score, no ring is pre-selected; the user clicks a ring or Next."""

    @pytest.mark.parametrize("top_score", [0.99, 0.6, 0.3])
    def test_no_candidate_is_preselected_at_any_score(self, top_score):
        with _live_review(
            "multi_dot_target",
            with_aim=False,
            expected_shots=5,
            before_load=_scale_detect_scores(top_score),
        ) as (page, _server, settings, group, _sc, problems):
            page.locator("#aim-suggestion:not([hidden])").wait_for()

            assert "none selected" in page.inner_text("#aim-suggestion-text")
            assert page.locator("#editor-svg circle.aim-candidate").count() >= 2
            assert page.locator("#editor-svg circle.aim-candidate.active").count() == 0
            assert page.is_disabled("#aim-use-button")
            assert page.inner_text("#aim-status") == "Aim point is not set."
            assert _group_after(settings, group.id)[0].aim_x_in is None
            assert problems == []

    def test_clicking_the_second_ring_then_use_sets_that_candidates_aim(self):
        with _live_review(
            "multi_dot_target",
            with_aim=False,
            expected_shots=5,
            before_load=_scale_detect_scores(0.99),
        ) as (page, _server, settings, group, _sc, problems):
            page.locator("#aim-suggestion:not([hidden])").wait_for()
            page.click("#zoom-fit-button")
            centres = page.evaluate(
                """() => [...document.querySelectorAll('#editor-svg circle.aim-candidate')].map((c) => {
                  const r = c.getBoundingClientRect();
                  return [r.left + r.width / 2, r.top + r.height / 2,
                          Number(c.getAttribute('cx')), Number(c.getAttribute('cy'))];
                })"""
            )
            sx, sy, cx, cy = centres[1]

            page.mouse.click(sx, sy)

            assert page.inner_text("#aim-suggestion-text").startswith("Suggested aim 2 of ")
            assert page.locator("#editor-svg circle.aim-candidate.active").count() == 1
            page.click("#aim-use-button")
            page.click("#save-button")
            page.locator("#save-button:enabled").wait_for()
            row, _ = _group_after(settings, group.id)
            ppi = scene("multi_dot_target").ppi
            assert math.dist((row.aim_x_in, row.aim_y_in), (cx / ppi, cy / ppi)) < 0.01
            assert problems == []

    def test_a_click_away_from_every_ring_still_adds_a_shot(self):
        with _live_review("ragged_10x7", with_aim=False, expected_shots=10) as (
            page,
            _server,
            _settings,
            _group,
            _sc,
            problems,
        ):
            page.locator("#aim-suggestion:not([hidden])").wait_for()
            before = page.locator("#editor-svg circle.shot").count()
            box = page.locator("#editor-svg").bounding_box()

            page.mouse.click(box["x"] + 30, box["y"] + 30)

            assert page.locator("#editor-svg circle.shot").count() == before + 1
            assert page.locator("#editor-svg circle.aim-candidate.active").count() == 0
            assert problems == []


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestSameDirectionDragInARealBrowser:
    """FG3c.2 / G3c3-W4: drag both estimated siblings the same way, then Fill."""

    @pytest.mark.parametrize(
        ("name", "expected"), [("merged_pairs", 12), ("close_holes_tight_pair", 8)]
    )
    def test_fill_after_dragging_both_siblings_the_same_way_keeps_the_count(self, name, expected):
        with _live_review(name, with_aim=True, expected_shots=expected) as (
            page,
            server,
            settings,
            group,
            _sc,
            problems,
        ):
            boxes = page.evaluate(
                """() => [...document.querySelectorAll('#editor-svg circle.shot.estimated')].map((c) => {
                  const r = c.getBoundingClientRect();
                  return [r.left + r.width / 2, r.top + r.height / 2];
                })"""
            )
            (ax, ay), (bx, by) = boxes
            dx, dy = (bx - ax) * 0.6, (by - ay) * 0.6
            for x, y in ((bx, by), (ax, ay)):
                page.mouse.move(x, y)
                page.mouse.down()
                page.mouse.move(x + dx, y + dy, steps=6)
                page.mouse.up()
            assert page.locator("#editor-svg circle.shot.estimated").count() == 2

            page.click("#fill-button")
            page.locator("#fill-button:enabled").wait_for()

            assert page.inner_text("#shot-counter") == f"{expected} of {expected} shots"
            assert page.is_enabled("#finalize-button")
            page.click("#save-button")
            page.locator("#save-button:enabled").wait_for()
            page.click("#finalize-button")
            page.wait_for_url(f"{server}/groups/{group.id}")
            row, shots = _group_after(settings, group.id)
            assert row.status == "final" and len(shots) == expected
            assert problems == []


class TestRegionDetectReplacesTheWholePhotoGuess:
    """T4.0m at the API: the whole-photo proposal, then a Detect inside the group's region."""

    def _group(self, conn, world, settings):
        sc = scene("multi_dot_target")
        group = make_group(conn, world, make_photo(conn, settings, "multi_dot_target"), expected=5)
        return sc, group

    def test_the_region_proposal_replaces_the_unsaved_whole_photo_proposal(
        self, client, conn, settings, world
    ):
        sc, group = self._group(conn, world, settings)
        whole = client.post(f"/api/groups/{group.id}/detect", json={}).json()
        assert len(whole["shots"]) == 15 and whole["status"] == "too_many"
        stored = _as_payload(whole["shots"])

        region = client.post(f"/api/groups/{group.id}/detect", json=roi_body(sc)).json()
        merged = _client_merge(stored, region, replace_unsaved=True)

        assert len(region["shots"]) == 5 and region["status"] == "ok"
        assert len(merged) == 5
        assert len(_client_merge(stored, region)) == 15
        saved = put_review(client, group.id, _as_payload(merged), expected=5)
        assert saved.status_code == 200, saved.text
        assert repo_groups.list_rejections(conn, group.id) == []
        assert client.post(f"/api/groups/{group.id}/finalize").status_code == 200

    def test_saved_moved_and_manual_shots_stay(self, client, conn, settings, world):
        sc, group = self._group(conn, world, settings)
        whole = client.post(f"/api/groups/{group.id}/detect", json={}).json()
        stored = _as_payload(whole["shots"])
        region = client.post(f"/api/groups/{group.id}/detect", json=roi_body(sc)).json()
        inside = {(round(s["x_in"], 4), round(s["y_in"], 4)) for s in region["shots"]}
        outside = [s for s in stored if (round(s["x_in"], 4), round(s["y_in"], 4)) not in inside]
        outside[0]["saved"] = True
        outside[1]["moved_by_user"] = True
        outside.append({"x_in": 0.5, "y_in": 0.5, "origin": "manual", "moved_by_user": False})

        merged = _client_merge(stored + [outside[-1]], region, replace_unsaved=True)

        kept = [s for s in merged if (round(s["x_in"], 4), round(s["y_in"], 4)) not in inside]
        assert len(kept) == 3
        assert {id(s) for s in kept} == {id(outside[0]), id(outside[1]), id(outside[-1])}
        assert len(merged) == 5 + 3

    def test_the_page_script_replaces_unsaved_proposals_only_for_a_region_detect(self):
        source = (WEB / "static/js/review.js").read_text()

        assert "function isUnsavedProposal(shot)" in source
        assert 'return !shot.saved && !shot.moved_by_user && shot.origin !== "manual";' in source
        assert "mergeProposal(proposal, model.region !== null);" in source
        assert (
            'mergeProposal(await apiSend("POST", `/api/groups/${groupId}/fill-shortfall`));'
            in source
        )


def _drag_region(page, region) -> None:
    """Draw working-px `region` (x0, y0, x1, y1) with the Set region tool; the editor is at Fit."""
    page.click("#zoom-fit-button")
    view = page.evaluate(
        """() => {
          const svg = document.getElementById('editor-svg');
          const [x, y, w, h] = svg.getAttribute('viewBox').split(' ').map(Number);
          const r = svg.getBoundingClientRect();
          return {x, y, w, h, left: r.left, top: r.top, width: r.width, height: r.height};
        }"""
    )

    def client(px, py):
        return (
            view["left"] + (px - view["x"]) / view["w"] * view["width"],
            view["top"] + (py - view["y"]) / view["h"] * view["height"],
        )

    page.click("#region-button")
    sx, sy = client(region[0], region[1])
    ex, ey = client(region[2], region[3])
    page.mouse.move(sx, sy)
    page.mouse.down()
    page.mouse.move(ex, ey, steps=8)
    page.mouse.up()


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestRegionDetectInARealBrowser:
    """T4.0m in real Chromium on the multi-aim photo: no false rejections are ever needed."""

    def _open(self):
        return _live_review("multi_dot_target", with_aim=True, expected_shots=5, with_region=False)

    @staticmethod
    def _region(sc):
        x0, y0, x1, y1 = sc.roi
        return (x0, y0, x1, y1)

    def test_a_region_detect_after_the_whole_photo_guess_gives_five_of_five(self):
        with self._open() as (page, _server, settings, group, sc, problems):
            page.locator("#shot-counter").filter(has_text="15 of 5 shots").wait_for()

            _drag_region(page, self._region(sc))
            page.click("#detect-button")
            page.locator("#shot-counter").filter(has_text=re.compile(r"^5 of 5 shots$")).wait_for()

            assert page.locator("#editor-svg circle.shot").count() == 5
            page.click("#save-button")
            page.locator("#save-button:enabled").wait_for()
            connection = connect(settings.db_path)
            try:
                assert repo_groups.list_rejections(connection, group.id) == []
                assert len(repo_groups.list_shots(connection, group.id)) == 5
                assert repo_groups.get_group(connection, group.id).roi_json
            finally:
                connection.close()
            assert page.is_enabled("#finalize-button")
            assert problems == []

    def test_moved_and_manual_shots_survive_a_region_detect(self):
        with self._open() as (page, _server, _settings, _group, sc, problems):
            page.locator("#shot-counter").filter(has_text="15 of 5 shots").wait_for()
            far = page.evaluate(
                """() => [...document.querySelectorAll('#editor-svg circle.shot')]
                  .map((c) => ({x: Number(c.getAttribute('cx')), y: Number(c.getAttribute('cy'))}))
                  .sort((a, b) => b.x - a.x)[0]"""
            )
            page.click("#zoom-fit-button")
            view = page.evaluate(
                """() => {
                  const svg = document.getElementById('editor-svg');
                  const [x, y, w, h] = svg.getAttribute('viewBox').split(' ').map(Number);
                  const r = svg.getBoundingClientRect();
                  return {x, y, w, h, left: r.left, top: r.top, width: r.width, height: r.height};
                }"""
            )

            def client(px, py):
                return (
                    view["left"] + (px - view["x"]) / view["w"] * view["width"],
                    view["top"] + (py - view["y"]) / view["h"] * view["height"],
                )

            sx, sy = client(far["x"], far["y"])
            page.mouse.move(sx, sy)
            page.mouse.down()
            page.mouse.move(sx + 12, sy + 12, steps=4)
            page.mouse.up()
            page.mouse.click(*client(150, 150))
            assert page.locator("#editor-svg circle.shot.manual").count() == 1

            _drag_region(page, self._region(sc))
            page.click("#detect-button")
            page.locator("#shot-counter").filter(has_text="7 of 5 shots").wait_for()

            assert page.locator("#editor-svg circle.shot.manual").count() == 1
            assert page.locator("#editor-svg circle.shot").count() == 7
            assert problems == []

    def test_saved_shots_survive_a_region_detect(self):
        with self._open() as (page, _server, _settings, _group, sc, problems):
            page.locator("#shot-counter").filter(has_text="15 of 5 shots").wait_for()
            page.click("#save-button")
            page.locator("#save-button:enabled").wait_for()

            _drag_region(page, self._region(sc))
            page.click("#detect-button")
            page.locator("#detect-button:enabled").wait_for()

            assert page.inner_text("#shot-counter") == "15 of 5 shots"
            assert problems == []


def _upload_confirmed(page, base: str, kind_candidate: str):
    """Upload the clean circle target through the API and confirm a proposal of it.

    `kind_candidate` is "wrong" (a candidate that is not the printed circle) or "right".
    Returns (photo id, truth).
    """
    from tests.synth.generate import render_named

    img, truth = render_named("clean_circle1", 0)
    ok, buffer = cv2.imencode(".jpg", img)
    assert ok
    headers = {"Origin": base}
    uploaded = page.context.request.post(
        f"{base}/api/photos",
        multipart={"file": {"name": "t.jpg", "mimeType": "image/jpeg", "buffer": buffer.tobytes()}},
        headers=headers,
    )
    assert uploaded.status == 201, uploaded.text()
    photo_id = uploaded.json()["id"]
    proposed = page.context.request.post(
        f"{base}/api/photos/{photo_id}/reference/propose",
        data={"kind": "circle_1in"},
        headers=headers,
    ).json()["candidates"]
    centre = truth["reference"]["ellipse_center_px"]

    def distance(candidate):
        circle = candidate["circle"]
        return math.dist((circle["center_x_px"], circle["center_y_px"]), centre)

    ranked = sorted(proposed, key=distance)
    chosen = ranked[0] if kind_candidate == "right" else ranked[-1]
    confirmed = page.context.request.post(
        f"{base}/api/photos/{photo_id}/reference/confirm",
        data={"kind": "circle_1in", "candidate_index": chosen["index"]},
        headers=headers,
    )
    assert confirmed.status == 200, confirmed.text()
    return photo_id, truth


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestChangeScaleReferenceInARealBrowser:
    """B-79 in real Chromium: cancel, confirm, reference page, re-confirm, back to the review."""

    def test_confirm_then_reconfirm_a_different_reference_and_return_to_the_review(self):
        sync_api = pytest.importorskip("playwright.sync_api")
        with run_live_server() as server, sync_api.sync_playwright() as playwright:
            settings = Settings(data_dir=server.data_dir, port=server.port)
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 1000})
                problems: list[str] = []
                page.on("pageerror", lambda error: problems.append(str(error)))
                photo_id, truth = _upload_confirmed(page, str(server), "wrong")
                connection = connect(settings.db_path)
                try:
                    rifle = seed_rifle(connection, bullet_diameter_in=0.224)
                    group = make_group(
                        connection,
                        World(rifle, seed_load(connection, rifle), seed_context(rifle)),
                        photo_id,
                        expected=3,
                    )
                    wrong_h = repo_groups.get_photo(connection, photo_id).h_px_to_plane_json
                finally:
                    connection.close()
                put = page.context.request.put(
                    f"{server}/api/groups/{group.id}/review",
                    data={
                        "aim": {"x_in": 4.5, "y_in": 5.5},
                        "shots": plane_shots(3),
                        "rejections": [],
                        "shots_expected": 3,
                    },
                    headers={"Origin": str(server)},
                )
                assert put.status == 200, put.text()

                page.goto(f"{server}/groups/{group.id}/review?return_to=%2Frifles")
                button = page.locator("#reference-change-button")
                button.wait_for()
                assert button.inner_text() == "Change scale reference"
                button_box = button.bounding_box()
                counter_box = page.locator("#shot-counter").bounding_box()
                assert button_box["y"] + button_box["height"] <= counter_box["y"]
                assert page.locator("#reference-change-confirm").is_hidden()

                button.click()
                panel = page.locator("#reference-change-confirm")
                panel.wait_for()
                assert (
                    "This clears the saved shots and aim of 1 draft group(s) on this photo. Continue?"
                    in panel.inner_text()
                )
                page.locator("#reference-change-cancel").click()
                assert panel.is_hidden()
                still = _group_after(settings, group.id)
                assert len(still[1]) == 3 and still[0].aim_x_in is not None

                button.click()
                page.locator("#reference-change-continue").click()
                page.wait_for_url(re.compile(rf"/photos/{photo_id}/reference\?group_id={group.id}"))
                assert parse_qs(urlparse(page.url).query)["return_to"] == ["/rifles"]
                after_reset = _group_after(settings, group.id)
                assert after_reset[1] == [] and after_reset[0].aim_x_in is None
                connection = connect(settings.db_path)
                try:
                    assert repo_groups.get_photo(connection, photo_id).reference_confirmed == 0
                finally:
                    connection.close()

                page.locator("#kind").select_option("circle_1in")
                page.locator("#find").click()
                page.locator("#candidates-panel").wait_for()
                page.locator("#find:enabled").wait_for()
                centres = page.evaluate(
                    """() => [...document.querySelectorAll('#overlay > ellipse')].map(
                      (n) => [Number(n.getAttribute('cx')), Number(n.getAttribute('cy'))])"""
                )
                target = truth["reference"]["ellipse_center_px"]
                best = min(range(len(centres)), key=lambda i: math.dist(centres[i], target))
                page.locator('#candidates input[name="candidate"]').nth(best).check()
                page.locator("#confirm-candidate").click()
                page.wait_for_url(re.compile(rf"/groups/{group.id}/review"))
                page.locator("#reference-change-button").wait_for()
                assert parse_qs(urlparse(page.url).query)["return_to"] == ["/rifles"]
            finally:
                browser.close()
            connection = connect(settings.db_path)
            try:
                photo = repo_groups.get_photo(connection, photo_id)
            finally:
                connection.close()

        assert photo.reference_confirmed == 1 and photo.h_px_to_plane_json != wrong_h
        assert problems == []

    def test_a_finalized_group_disables_the_button_with_its_reason(self):
        sync_api = pytest.importorskip("playwright.sync_api")
        with run_live_server() as server, sync_api.sync_playwright() as playwright:
            settings = Settings(data_dir=server.data_dir, port=server.port)
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 1000})
                photo_id, _ = _upload_confirmed(page, str(server), "right")
                connection = connect(settings.db_path)
                try:
                    rifle = seed_rifle(connection, bullet_diameter_in=0.224)
                    group = seed_group(
                        connection,
                        context=seed_context(rifle),
                        load=seed_load(connection, rifle),
                        offsets=[Offset(0.1, 0.1), Offset(-0.1, 0.0), Offset(0.0, -0.1)],
                        photo=photo_id,
                    )
                finally:
                    connection.close()
                page.goto(f"{server}/groups/{group.id}/review")
                button = page.locator("#reference-change-button")
                button.wait_for()
                assert button.is_disabled()
                assert (
                    "A finalized group uses this scale"
                    in page.locator("#reference-change-reason").inner_text()
                )
                button.click(force=True)
                assert page.locator("#reference-change-confirm").is_hidden()
                assert "/reference" not in page.url
            finally:
                browser.close()

    def test_nothing_saved_navigates_straight_to_the_reference_page_without_clearing(self):
        sync_api = pytest.importorskip("playwright.sync_api")
        with run_live_server() as server, sync_api.sync_playwright() as playwright:
            settings = Settings(data_dir=server.data_dir, port=server.port)
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 1000})
                requests: list[str] = []
                page.on("request", lambda r: requests.append(f"{r.method} {r.url}"))
                photo_id, _ = _upload_confirmed(page, str(server), "right")
                connection = connect(settings.db_path)
                try:
                    rifle = seed_rifle(connection, bullet_diameter_in=0.224)
                    group = make_group(
                        connection,
                        World(rifle, seed_load(connection, rifle), seed_context(rifle)),
                        photo_id,
                        expected=3,
                        aim=None,
                    )
                finally:
                    connection.close()
                page.goto(f"{server}/groups/{group.id}/review?return_to=%2Fgroups")
                button = page.locator("#reference-change-button")
                page.locator("#reference-change-button:enabled").wait_for()
                button.click()
                page.wait_for_url(re.compile(rf"/photos/{photo_id}/reference\?group_id={group.id}"))
                assert parse_qs(urlparse(page.url).query)["return_to"] == ["/groups"]
            finally:
                browser.close()
            connection = connect(settings.db_path)
            try:
                photo = repo_groups.get_photo(connection, photo_id)
            finally:
                connection.close()

        assert photo.reference_confirmed == 1
        assert not [r for r in requests if r.startswith("POST") and "/reference/reset" in r]
