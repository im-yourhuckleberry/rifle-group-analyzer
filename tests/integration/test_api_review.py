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

    def test_every_proposed_shot_lies_inside_the_region(self, client, conn, settings, world):
        # A region edge through a ragged hole once let its estimated shots land outside the region.
        sc = scene("ragged_10x7")
        group = make_group(conn, world, make_photo(conn, settings))
        whole = client.post(f"/api/groups/{group.id}/detect", json=roi_body(sc)).json()
        x0, y0, x1, y1 = sc.roi
        regions = [
            region
            for shot in whole["shots"]
            for dx in (-6, -3, 0, 3, 6)
            for region in (
                (x0, y0, int(shot["x_in"] * sc.ppi + dx), y1),
                (int(shot["x_in"] * sc.ppi + dx), y0, x1, y1),
            )
        ]

        proposed = 0
        for region in regions:
            body = {"roi_px": dict(zip(("x0", "y0", "x1", "y1"), region, strict=True))}
            detected = client.post(f"/api/groups/{group.id}/detect", json=body).json()
            for shot in detected["shots"]:
                x, y = shot["x_in"] * sc.ppi, shot["y_in"] * sc.ppi
                assert region[0] <= x <= region[2] and region[1] <= y <= region[3], (shot, region)
            proposed += len(detected["shots"])
        filled = client.post(f"/api/groups/{group.id}/fill-shortfall").json()
        for shot in filled["shots"]:
            x, y = shot["x_in"] * sc.ppi, shot["y_in"] * sc.ppi
            assert regions[-1][0] <= x <= regions[-1][2], (shot, regions[-1])
        assert proposed > 0

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
            'id="aim-confirm-button"',
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
            assert (
                state["return_to"] is None and state["next_url"] == f"/groups/{group.id}?share=1"
            ), bad
        rifle = state_for("/rifles/1")
        assert (
            rifle["return_to"] == "/rifles/1" and rifle["next_url"] == f"/groups/{group.id}?share=1"
        )
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

        # Go back sits in the aim step's bottom row, beside Confirm aim point.
        assert text.index('id="reference-change-button"') < text.index('id="shot-counter"')
        assert text.index('id="step-aim"') < text.index('id="reference-change-button"')
        assert text.index('id="reference-change-button"') < text.index('id="aim-confirm-button"')
        assert text.index('id="reference-change-button"') < text.index('id="step-shots"')
        assert ">Go back</button>" in text
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
            page = browser.new_page(
                viewport={"width": 1400, "height": 1000}, reduced_motion="reduce"
            )
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
        page.click("#zoom-in-button")
        again = full
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
        count_before = page.locator("#editor-svg circle.shot").count()
        # Room for one more shot: none is ever added past the number fired.
        page.fill("#expected-input", str(count_before + 1))
        page.dispatch_event("#expected-input", "change")
        page.click("#zoom-in-button")
        view = self._view(page)
        target = (view["left"] + 30, view["top"] + view["height"] - 30)
        expected = self._under(view, *target)

        page.mouse.click(*target)
        _save(page)

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
        _save(page)

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


def _client_merge(
    stored: list[dict],
    proposal: dict,
    replace_unsaved: bool = False,
    region_px: tuple[float, float, float, float] | None = None,
) -> list[dict]:
    """What the page does with a proposal: replace unmoved estimated shots, keep every other.

    With `replace_unsaved` (a Detect inside a region) the unsaved, unmoved, non-manual shots of an
    earlier proposal go too; a stored dict is unsaved unless it carries `saved: True`. With
    `region_px` every unmoved, non-manual shot outside the region goes as well, saved or not.

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

    def held(s: dict) -> bool:
        if region_px is None or s["origin"] == "manual" or s.get("moved_by_user", False):
            return True
        ppi = proposal["frame"]["ppi"]
        x0, y0, x1, y1 = region_px
        return x0 <= s["x_in"] * ppi <= x1 and y0 <= s["y_in"] * ppi <= y1

    kept = [s for s in stored if not replaceable(s) and held(s)]
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
        if held({**p, "moved_by_user": False})
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
        assert "model.shots.filter((s) => !replaceable(s) && inRegionOrUserPlaced(s))" in source
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
        # Detect re-runs with the saved count and region, placing estimated shots in ragged holes
        # (what the removed Fill shortfall button did).
        page.click("#detect-button")
        page.locator("#detect-button:enabled").wait_for()

    @staticmethod
    def _save(page) -> None:
        _save(page)

    def _set_expected(self, page, value: int) -> None:
        page.fill("#expected-input", str(value))
        page.dispatch_event("#expected-input", "change")

    def test_lowering_the_count_and_detecting_never_produces_a_409(self, browser_page):
        page, server, settings, group, sc, problems = browser_page
        self._save(page)
        assert self._count(page) == "10 of 10 shots"

        self._set_expected(page, 9)
        self._fill(page)
        assert self._count(page) == "9 of 9 shots"
        assert _ready(page)

        self._save(page)
        assert _saved_without_error(page)
        page.click("#finalize-button")
        page.click("#finalize-confirm-button")
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
        assert not _ready(page)
        assert "remove 1 with Not a bullet hole, or raise the expected count" in page.inner_text(
            "#finalize-reason"
        )
        self._save(page)
        assert _saved_without_error(page)

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
        assert _ready(page)
        self._save(page)
        assert _saved_without_error(page)
        page.click("#finalize-button")
        page.click("#finalize-confirm-button")
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


@pytest.fixture
def cv_v1_for_merged_pairs(request, monkeypatch):
    """merged_pairs' two estimated siblings come from cv-v1 reading one pair as a ragged hole.

    seg-v1 (the app's detector) finds 11 of the 12 holes directly and leaves one estimate, so these
    in-process tests pin the scenario to the detector it was built on.
    """
    callspec = getattr(request.node, "callspec", None)
    if callspec is not None and callspec.params.get("name") == "merged_pairs":
        from rga.vision import detect as cv_v1
        from rga.vision import pipeline

        monkeypatch.setattr(pipeline, "detect_holes", cv_v1.detect_holes)


@pytest.mark.usefixtures("cv_v1_for_merged_pairs")
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
    def test_the_aim_is_the_circle_center_or_placed_by_hand_with_no_snap(self):
        source = (WEB / "static/js/review.js").read_text()
        markup = (WEB / "templates/review.html").read_text()

        # One on/off pair, no "use the center" button.
        assert (
            'id="aim-center-toggle" aria-pressed="true">Use the circle\'s center</button>' in markup
        )
        assert 'id="aim-hand-toggle" aria-pressed="false">Mark it by hand</button>' in markup
        for gone in ("aim-center-button", "aim-suggestion", "aim-use-button", "aim-manual-button"):
            assert f'id="{gone}"' not in markup
        assert "snap-aim" not in source, "a click never snaps to the printed mark"
        assert "placeAim({ x_in: xPx / server.frame.ppi, y_in: yPx / server.frame.ppi }" in source
        assert "const circleCenter = server.reference_center;" in source
        # The aim is only ever the circle's centre or a hand-placed point (click, drag, restore).
        assert source.count("model.aim = { ...circleCenter }") == 2

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
            page = browser.new_page(
                viewport={"width": 1400, "height": 1000}, reduced_motion="reduce"
            )
            problems: list[str] = []
            page.on("pageerror", lambda error: problems.append(str(error)))
            page.on(
                "console",
                lambda message: problems.append(message.text) if message.type == "error" else None,
            )
            if before_load is not None:
                before_load(page)
            page.goto(f"{server}/groups/{group.id}/review")
            if with_aim:
                page.locator("#editor-svg circle.shot").nth(expected_shots - 1).wait_for()
            else:
                page.locator('#editor-wrap[aria-busy="false"]').wait_for()
            yield page, server, settings, group, sc, problems
        finally:
            browser.close()


def _save(page) -> None:
    """Press Done (it always saves), wait for the save, and decline the lock offer if shown."""
    with page.expect_response(re.compile(r"/api/groups/\d+/review$")):
        page.click("#finalize-button")
    page.locator("#finalize-button:enabled").wait_for()
    if page.locator("#finalize-confirm").is_visible():
        page.click("#finalize-cancel-button")


def _saved_without_error(page) -> bool:
    """After Done: no error was shown (it either says what is left, or offered to lock)."""
    flash = page.inner_text("#flash").strip() if page.is_visible("#flash") else ""
    return flash == "" or flash.startswith("Saved.")


def _ready(page) -> bool:
    """True when Done would finish the group (nothing blocks finalizing)."""
    return page.inner_text("#finalize-reason").strip() == ""


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
class TestAimStepInARealBrowser:
    """The aim step in real Chromium: no detect and no shots; the aim starts on the circle's
    center (unsaved), "Mark it by hand" or a click places it exactly (no snap)."""

    def _aim(self, page) -> tuple[float, float]:
        mark = page.locator("#editor-svg circle.aim-mark")
        return float(mark.get_attribute("cx")), float(mark.get_attribute("cy"))

    def _client(self, page, x: float, y: float) -> tuple[float, float]:
        page.click("#zoom-fit-button")
        view = page.evaluate(
            """() => {
              const svg = document.getElementById('editor-svg');
              const [x, y, w, h] = svg.getAttribute('viewBox').split(' ').map(Number);
              const r = svg.getBoundingClientRect();
              return {x, y, w, h, left: r.left, top: r.top, width: r.width, height: r.height};
            }"""
        )
        return (
            view["left"] + (x - view["x"]) / view["w"] * view["width"],
            view["top"] + (y - view["y"]) / view["h"] * view["height"],
        )

    def test_the_aim_starts_on_the_circle_center_unsaved_and_nothing_is_detected(self):
        posts: list[str] = []

        def watch(page):
            page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)

        with _live_review("ragged_10x7", with_aim=False, expected_shots=10, before_load=watch) as (
            page,
            _server,
            settings,
            group,
            _sc,
            problems,
        ):
            state = json.loads(page.locator("#review-root").get_attribute("data-state"))
            center = state["reference_center"]
            ppi = state["frame"]["ppi"]
            assert page.is_visible("#step-aim") and page.locator("#step-shots").is_hidden()
            assert posts == [], "the aim step runs no detection"
            assert page.locator("#editor-svg circle.shot").count() == 0
            assert page.get_attribute("#aim-center-toggle", "aria-pressed") == "true"
            assert page.get_attribute("#aim-hand-toggle", "aria-pressed") == "false"
            assert page.locator("#editor-svg circle.aim-candidate").count() == 0
            assert self._aim(page) == pytest.approx(
                (center["x_in"] * ppi, center["y_in"] * ppi), abs=1e-6
            )
            assert page.inner_text("#aim-status") == "Aim point: the circle's exact center."
            assert page.is_enabled("#aim-confirm-button")
            assert _group_after(settings, group.id)[0].aim_x_in is None, "not saved before confirm"

            page.click("#aim-confirm-button")
            page.locator("#editor-svg circle.shot").nth(9).wait_for()
            row, _ = _group_after(settings, group.id)
            assert (row.aim_x_in, row.aim_y_in) == pytest.approx(
                (center["x_in"], center["y_in"]), abs=1e-9
            )
            assert problems == []

    def test_mark_it_by_hand_waits_for_a_click_and_the_center_toggle_puts_it_back(self):
        with _live_review("ragged_10x7", with_aim=False, expected_shots=10) as (
            page,
            _server,
            _settings,
            _group,
            _sc,
            problems,
        ):
            state = json.loads(page.locator("#review-root").get_attribute("data-state"))
            center = state["reference_center"]
            ppi = state["frame"]["ppi"]

            page.click("#aim-hand-toggle")
            assert page.get_attribute("#aim-hand-toggle", "aria-pressed") == "true"
            assert page.locator("#editor-svg circle.aim-mark").count() == 0
            assert page.inner_text("#aim-status") == "Click the photo where you aimed."
            assert page.is_disabled("#aim-confirm-button")

            page.mouse.click(*self._client(page, 150.0, 160.0))
            placed = self._aim(page)
            assert page.is_enabled("#aim-confirm-button")

            page.click("#aim-center-toggle")
            assert self._aim(page) == pytest.approx(
                (center["x_in"] * ppi, center["y_in"] * ppi), abs=1e-6
            )
            page.click("#aim-hand-toggle")
            assert self._aim(page) == pytest.approx(placed, abs=1e-6), "the hand aim comes back"
            assert problems == []

    def test_a_click_on_the_photo_in_center_mode_switches_to_by_hand(self):
        with _live_review("ragged_10x7", with_aim=False, expected_shots=10) as (
            page,
            _server,
            _settings,
            _group,
            _sc,
            problems,
        ):
            assert page.get_attribute("#aim-center-toggle", "aria-pressed") == "true"
            page.mouse.click(*self._client(page, 150.0, 160.0))
            assert page.get_attribute("#aim-hand-toggle", "aria-pressed") == "true"
            assert page.inner_text("#aim-status") == "Aim point is set where you clicked."
            assert problems == []

    def test_a_click_places_the_aim_exactly_there_and_a_drag_moves_it(self):
        with _live_review("ragged_10x7", with_aim=False, expected_shots=10) as (
            page,
            _server,
            settings,
            group,
            _sc,
            problems,
        ):
            snaps: list[str] = []
            page.on("request", lambda r: snaps.append(r.url) if "snap-aim" in r.url else None)
            target = (150.0, 160.0)

            page.mouse.click(*self._client(page, *target))

            placed = self._aim(page)
            per_px = page.evaluate(
                "() => { const s = document.getElementById('editor-svg'); return Number(s.getAttribute('viewBox').split(' ')[2]) / s.getBoundingClientRect().width; }"
            )
            assert placed == pytest.approx(target, abs=per_px)
            assert page.inner_text("#aim-status") == "Aim point is set where you clicked."
            sx, sy = self._client(page, *placed)
            page.mouse.move(sx + 2, sy + 2)
            page.mouse.down()
            page.mouse.move(sx + 32, sy + 22, steps=6)
            page.mouse.up()
            moved = self._aim(page)
            assert moved[0] - placed[0] == pytest.approx(30 * per_px, abs=per_px)
            assert moved[1] - placed[1] == pytest.approx(20 * per_px, abs=per_px)

            page.click("#aim-confirm-button")
            page.locator("#step-shots").wait_for()
            row, _ = _group_after(settings, group.id)
            ppi = json.loads(page.locator("#review-root").get_attribute("data-state"))["frame"][
                "ppi"
            ]
            assert (row.aim_x_in * ppi, row.aim_y_in * ppi) == pytest.approx(moved, abs=0.01)
            assert snaps == []
            assert problems == []


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestSiblingNudgeInARealBrowser:
    """G3c2-W3 in real Chromium: nudge one estimated sibling toward the other, then Fill."""

    # merged_pairs is API-only now: the live server runs seg-v1, which leaves one estimate there.
    @pytest.mark.parametrize(("name", "expected"), [("close_holes_tight_pair", 8)])
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

            page.click("#detect-button")
            page.locator("#detect-button:enabled").wait_for()

            assert page.inner_text("#shot-counter") == f"{expected} of {expected} shots"
            assert page.locator("#editor-svg circle.shot.estimated").count() == 2
            assert _ready(page)
            _save(page)
            assert _saved_without_error(page)
            page.click("#finalize-button")
            page.click("#finalize-confirm-button")
            page.wait_for_url(f"{server}/groups/{group.id}")
            row, shots = _group_after(settings, group.id)
            assert row.status == "final" and len(shots) == expected
            assert problems == []


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestSameDirectionDragInARealBrowser:
    """FG3c.2 / G3c3-W4: drag both estimated siblings the same way, then Fill."""

    # merged_pairs is API-only now: the live server runs seg-v1, which leaves one estimate there.
    @pytest.mark.parametrize(("name", "expected"), [("close_holes_tight_pair", 8)])
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

            page.click("#detect-button")
            page.locator("#detect-button:enabled").wait_for()

            assert page.inner_text("#shot-counter") == f"{expected} of {expected} shots"
            assert _ready(page)
            _save(page)
            page.click("#finalize-button")
            page.click("#finalize-confirm-button")
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

    def test_saved_detected_shots_outside_the_region_go_moved_and_manual_stay(
        self, client, conn, settings, world
    ):
        sc, group = self._group(conn, world, settings)
        whole = client.post(f"/api/groups/{group.id}/detect", json={}).json()
        stored = _as_payload(whole["shots"])
        region = client.post(f"/api/groups/{group.id}/detect", json=roi_body(sc)).json()
        inside = {(round(s["x_in"], 4), round(s["y_in"], 4)) for s in region["shots"]}
        outside = [s for s in stored if (round(s["x_in"], 4), round(s["y_in"], 4)) not in inside]
        outside[0]["saved"] = True
        outside[1]["moved_by_user"] = True
        outside.append({"x_in": 0.5, "y_in": 0.5, "origin": "manual", "moved_by_user": False})

        merged = _client_merge(
            stored + [outside[-1]], region, replace_unsaved=True, region_px=sc.roi
        )

        # A saved detected shot outside the region goes; the user's moved and manual shots stay.
        kept = [s for s in merged if (round(s["x_in"], 4), round(s["y_in"], 4)) not in inside]
        assert len(kept) == 2
        assert {id(s) for s in kept} == {id(outside[1]), id(outside[-1])}
        assert len(merged) == 5 + 2

    def test_the_page_script_replaces_unsaved_proposals_only_for_a_region_detect(self):
        source = (WEB / "static/js/review.js").read_text()

        assert "function isUnsavedProposal(shot)" in source
        assert 'return !shot.saved && !shot.moved_by_user && shot.origin !== "manual";' in source
        assert "mergeProposal(proposal, model.region !== null, !automatic);" in source
        assert "fill-shortfall" not in source, "the page has no Fill shortfall any more"


def _store_region_and_reload(page, server, group, region) -> None:
    """Give the group a region (working px) the way the editor's Detect stores one, then reopen the
    editor: there is no region button, so a region on a photo of one group comes from the API."""
    x0, y0, x1, y1 = region
    stored = page.context.request.post(
        f"{server}/api/groups/{group.id}/detect",
        data={"roi_px": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}},
        headers={"Origin": str(server)},
    )
    assert stored.status == 200, stored.text()
    page.reload()
    page.locator("#editor-svg image").wait_for()
    page.locator("#detect-button:enabled").wait_for()


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
        with self._open() as (page, server, settings, group, sc, problems):
            page.locator("#shot-counter").filter(has_text="15 of 5 shots").wait_for()

            _store_region_and_reload(page, server, group, self._region(sc))
            page.locator("#shot-counter").filter(has_text=re.compile(r"^5 of 5 shots$")).wait_for()
            page.click("#detect-button")
            page.locator("#shot-counter").filter(has_text=re.compile(r"^5 of 5 shots$")).wait_for()

            assert page.locator("#editor-svg circle.shot").count() == 5
            _save(page)
            connection = connect(settings.db_path)
            try:
                assert repo_groups.list_rejections(connection, group.id) == []
                assert len(repo_groups.list_shots(connection, group.id)) == 5
                assert repo_groups.get_group(connection, group.id).roi_json
            finally:
                connection.close()
            assert _ready(page)
            assert problems == []

    def test_moved_and_manual_shots_survive_a_region_detect(self):
        with self._open() as (page, server, _settings, group, sc, problems):
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
            # Room for one more shot than the 15 proposed (no shot is ever added past the count).
            page.fill("#expected-input", "16")
            page.dispatch_event("#expected-input", "change")
            # The dragged shot is selected, so the first empty click only deselects it.
            page.mouse.click(*client(150, 150))
            assert page.locator("#editor-svg circle.shot.selected").count() == 0
            assert page.locator("#editor-svg circle.shot.manual").count() == 0
            page.mouse.click(*client(150, 150))
            assert page.locator("#editor-svg circle.shot.manual").count() == 1
            _save(page)

            _store_region_and_reload(page, server, group, self._region(sc))
            page.click("#detect-button")
            page.locator("#detect-button:enabled").wait_for()

            # The hand-placed shot and the dragged one stay, wherever they are; no unmoved detected
            # shot outside the region is left. (Detect also tops the count up with estimated shots
            # inside the region, since 16 were declared.)
            x0, y0, x1, y1 = self._region(sc)
            drawn = page.evaluate(
                """() => [...document.querySelectorAll('#editor-svg circle.shot')].map((c) => (
                  {cls: c.getAttribute('class'), x: Number(c.getAttribute('cx')), y: Number(c.getAttribute('cy'))}))"""
            )
            assert sum("manual" in d["cls"] for d in drawn) == 1
            assert any(abs(d["x"] - far["x"] - 12 / view["width"] * view["w"]) < 3 for d in drawn)
            outside = [
                d
                for d in drawn
                if "detected" in d["cls"] and not (x0 <= d["x"] <= x1 and y0 <= d["y"] <= y1)
            ]
            assert len(outside) == 1, outside  # only the dragged shot
            assert problems == []

    def test_saved_detected_shots_outside_the_region_are_dropped(self):
        with self._open() as (page, server, _settings, group, sc, problems):
            page.locator("#shot-counter").filter(has_text="15 of 5 shots").wait_for()
            _save(page)

            _store_region_and_reload(page, server, group, self._region(sc))
            assert page.inner_text("#shot-counter") == "15 of 5 shots"
            page.click("#detect-button")
            page.locator("#detect-button:enabled").wait_for()

            assert page.inner_text("#shot-counter") == "5 of 5 shots"
            page.click("#undo-button")
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
                page = browser.new_page(
                    viewport={"width": 1400, "height": 1000}, reduced_motion="reduce"
                )
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
                # The aim is saved, so the page opens on the shots step; Go back reaches the aim
                # step, whose Go back returns to the reference.
                page.locator("#shots-back-button").click()
                button = page.locator("#reference-change-button")
                button.wait_for()
                assert button.inner_text() == "Go back"
                assert page.locator("#step-shots").is_hidden()
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
                if len(centres) > 1:
                    page.locator('#candidates input[name="candidate"]').nth(best).check()
                page.locator("#reference-next").click()
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
                page = browser.new_page(
                    viewport={"width": 1400, "height": 1000}, reduced_motion="reduce"
                )
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
                    draft = make_group(
                        connection,
                        World(rifle, seed_load(connection, rifle), seed_context(rifle)),
                        photo_id,
                        expected=3,
                        aim=None,
                    )
                finally:
                    connection.close()
                # The final group itself has no aim step; a draft sharing its photo shows the
                # Go back button disabled.
                page.goto(f"{server}/groups/{group.id}/review")
                page.locator("#editor-svg image").wait_for()
                assert page.locator("#step-aim").is_hidden()
                assert page.locator("#shots-back-button").is_hidden()
                page.goto(f"{server}/groups/{draft.id}/review")
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
                page = browser.new_page(
                    viewport={"width": 1400, "height": 1000}, reduced_motion="reduce"
                )
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


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
@pytest.mark.parametrize("size", [(1280, 800), (1440, 900)])
def test_on_a_desktop_screen_every_step_fits_without_scrolling(size):
    """The reference, aim and shots steps fit the window: the photo is sized to the window's
    height and the controls (with the detect messages and the mouse help) sit in the panel
    beside it."""
    width, height = size

    def resize(page):
        page.set_viewport_size({"width": width, "height": height})

    def fits(page) -> bool:
        return page.evaluate("() => document.documentElement.scrollHeight") <= height

    with _live_review("ragged_10x7", with_aim=False, expected_shots=10, before_load=resize) as (
        page,
        server,
        _settings,
        group,
        _sc,
        problems,
    ):
        assert page.is_visible("#step-aim") and fits(page), "aim step"
        photo = page.locator("#editor-wrap").bounding_box()
        panel = page.locator("aside.flow-panel").bounding_box()
        assert photo["y"] + photo["height"] <= height
        assert panel["x"] >= photo["x"] + photo["width"], "the controls sit beside the photo"
        assert page.locator("aside.flow-panel #editor-hint").count() == 1
        # Scaled to fit without letterboxing: the drawing keeps the photo's shape exactly.
        box_ratio, view_ratio = page.evaluate(
            """() => { const s = document.getElementById('editor-svg');
              const r = s.getBoundingClientRect(); const v = s.viewBox.baseVal;
              return [r.width / r.height, v.width / v.height]; }"""
        )
        assert box_ratio == pytest.approx(view_ratio, rel=0.01)

        page.click("#aim-confirm-button")
        page.locator("#editor-svg circle.shot").nth(9).wait_for()
        assert page.locator("aside.flow-panel #review-notices").inner_text().strip() != ""
        assert fits(page), "shots step, with the detect message shown"

        state = json.loads(page.locator("#review-root").get_attribute("data-state"))
        page.goto(f"{server}/photos/{state['photo_id']}/reference?group_id={group.id}")
        page.locator("#photo").wait_for()
        assert fits(page), "reference step"
        page.click("#manual-toggle")
        assert fits(page), "reference step, marking by hand"
        # The fixture photo has no stored original, so the reference page's full image is a 409.
        assert [p for p in problems if "409" not in p] == []
