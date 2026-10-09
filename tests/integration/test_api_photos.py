"""T3.3: photo import, image download, reference proposal and confirmation, and the reference page.

Real synthetic targets (tests/synth) are encoded to JPEG/PNG and sent through the real app. The
rules under test: a reference is never confirmed by the app itself, a working image and scale exist
only after confirmation, uploads are validated and size-limited (413/415, tiny images 422), no
client-supplied name or path reaches the filesystem, and no input produces a 500.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import sqlite3
import struct
import zlib
from collections.abc import Iterator
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from rga.config import Settings
from rga.db.connection import connect
from rga.domain.types import Offset, PlanePoint
from rga.services import photos as photo_service
from rga.vision.io import MAX_UPLOAD_BYTES
from rga.web.app import create_app
from rga.web.schemas import ErrorResponse, PhotoOut, ReferenceConfirmOut, ReferenceProposeOut
from tests.support.seed import seed_context, seed_group, seed_load, seed_photo, seed_rifle
from tests.synth.generate import render_named

ORIGIN = "http://127.0.0.1:8765"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def conn(settings: Settings, client: TestClient) -> Iterator[sqlite3.Connection]:
    connection = connect(settings.db_path)
    yield connection
    connection.close()


def encode(img: np.ndarray, ext: str = ".jpg") -> bytes:
    ok, buffer = cv2.imencode(ext, img)
    assert ok
    return buffer.tobytes()


def synth_bytes(name: str, ext: str = ".jpg", seed: int = 0) -> tuple[bytes, dict]:
    img, truth = render_named(name, seed)
    return encode(img, ext), truth


def upload(client: TestClient, data: bytes, filename: str = "photo.jpg", mime: str = "image/jpeg"):
    return client.post("/api/photos", files={"file": (filename, data, mime)})


def assert_error(response, status: int, code: str) -> str:
    assert response.status_code == status, response.text
    body = ErrorResponse.model_validate(response.json())
    assert body.code == code
    return body.detail


def photo_row(conn: sqlite3.Connection, photo_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()


def count_files(directory: Path) -> int:
    return sum(1 for p in directory.iterdir())


@pytest.fixture(scope="module")
def circle1() -> tuple[bytes, dict]:
    return synth_bytes("clean_circle1")


@pytest.fixture(scope="module")
def grid() -> tuple[bytes, dict]:
    return synth_bytes("clean_grid")


def nearest_candidate(candidates: list[dict], truth_center: list[float]) -> dict:
    return min(
        (c for c in candidates if c["circle"]),
        key=lambda c: np.hypot(
            c["circle"]["center_x_px"] - truth_center[0],
            c["circle"]["center_y_px"] - truth_center[1],
        ),
    )


# ---------------------------------------------------------------- upload


class TestUpload:
    def test_jpeg_is_stored_by_content_hash_and_returned(self, client, settings, circle1):
        data, _ = circle1
        response = upload(client, data)
        assert response.status_code == 201
        photo = PhotoOut.model_validate(response.json())
        assert photo.sha256 == hashlib.sha256(data).hexdigest()
        assert (photo.ext, photo.duplicate, photo.reference_confirmed) == ("jpg", False, False)
        assert (
            photo.working_ppi is None and photo.working_w is None and photo.reference_kind is None
        )
        stored = settings.photos_dir / f"{photo.sha256}.jpg"
        assert stored.read_bytes() == data
        decoded = cv2.imread(str(stored))
        assert (photo.height_px, photo.width_px) == decoded.shape[:2]

    def test_png_keeps_its_sniffed_extension(self, client, settings, circle1):
        img, _ = render_named("clean_circle1", 0)
        response = upload(client, encode(img, ".png"), "scan.PNG", "image/png")
        assert response.status_code == 201
        assert response.json()["ext"] == "png"
        assert (settings.photos_dir / f"{response.json()['sha256']}.png").is_file()

    def test_identical_bytes_return_the_same_photo(self, client, conn, circle1):
        first = upload(client, circle1[0])
        second = upload(client, circle1[0], filename="another-name.jpg")
        assert (first.status_code, second.status_code) == (201, 200)
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["duplicate"] is True and first.json()["duplicate"] is False
        assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 1

    def test_client_filename_never_reaches_the_filesystem(self, client, settings, circle1):
        for hostile in ("../../etc/evil.jpg", "..\\..\\evil.jpg", "/abs/path.jpg", "a\x00b.jpg"):
            response = upload(client, circle1[0], hostile)
            assert response.status_code in (200, 201), hostile
        sha = hashlib.sha256(circle1[0]).hexdigest()
        assert sorted(p.name for p in settings.photos_dir.iterdir()) == [f"{sha}.jpg"]
        outside = [
            p for p in settings.data_dir.rglob("*") if p.is_file() and "evil" in p.name
        ] + list(settings.data_dir.parent.glob("evil*"))
        assert outside == []

    def test_original_files_are_owner_only(self, client, settings, circle1):
        upload(client, circle1[0])
        for directory in (settings.photos_dir, settings.working_dir):
            for path in directory.iterdir():
                assert path.stat().st_mode & 0o077 == 0

    def test_exif_orientation_is_applied_to_the_stored_dimensions(self, client):
        img = Image.new("RGB", (400, 200), (200, 200, 200))
        exif = Image.Exif()
        exif[0x0112] = 6
        buffer = io.BytesIO()
        img.save(buffer, "JPEG", exif=exif)
        response = upload(client, buffer.getvalue())
        assert response.status_code == 201
        assert (response.json()["width_px"], response.json()["height_px"]) == (200, 400)

    @pytest.mark.parametrize(
        ("data", "label"),
        [
            (b"hello this is not an image", "text"),
            (b"", "empty"),
            (b"\xff\xd8\xff\xe0" + b"\x00" * 64, "truncated jpeg"),
            (b"%PDF-1.4 fake", "pdf"),
        ],
    )
    def test_non_images_are_415_and_store_nothing(self, client, conn, settings, data, label):
        response = upload(client, data, "evil.jpg", "image/jpeg")
        detail = assert_error(response, 415, "unsupported_image")
        assert detail
        assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0
        assert count_files(settings.photos_dir) == 0, label

    def test_text_file_with_an_image_mime_type_is_415(self, client):
        assert_error(
            upload(client, b"just text\n", "x.txt", "image/jpeg"), 415, "unsupported_image"
        )

    def test_extreme_aspect_ratio_is_415(self, client):
        wide = np.full((30, 1000, 3), 128, np.uint8)
        assert_error(
            upload(client, encode(wide, ".png"), "w.png", "image/png"), 415, "unsupported_image"
        )

    def test_oversize_file_is_413(self, client, conn, settings):
        data = b"\xff\xd8\xff" + b"\x00" * MAX_UPLOAD_BYTES
        assert len(data) > MAX_UPLOAD_BYTES
        assert_error(upload(client, data), 413, "upload_too_large")
        assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0
        assert count_files(settings.photos_dir) == 0

    def test_pixel_cap_is_413(self, client, conn, settings):
        """A tiny PNG whose header declares more than the pixel limit is refused before decoding."""

        def chunk(kind: bytes, body: bytes) -> bytes:
            crc = zlib.crc32(kind + body) & 0xFFFFFFFF
            return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

        header = struct.pack(">IIBBBBB", 9000, 9000, 8, 2, 0, 0, 0)
        data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")
        assert_error(upload(client, data, "huge.png", "image/png"), 413, "upload_too_large")
        assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0
        assert count_files(settings.photos_dir) == 0

    def test_declared_length_far_over_the_limit_is_refused_before_reading(self, client):
        response = client.post(
            "/api/photos",
            content=b"x",
            headers={
                "Content-Type": "multipart/form-data; boundary=zzz",
                "Content-Length": str(MAX_UPLOAD_BYTES * 3),
            },
        )
        assert_error(response, 413, "upload_too_large")

    def test_chunked_body_is_cut_off_at_the_limit_without_reading_it_all(self, app):
        chunk = b"\x00" * (1024 * 1024)
        boundary = "BOUNDARY"
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="x.jpg"\r\n'
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode()
        supplied = {"chunks": 0}
        total_chunks = 200

        async def run() -> tuple[int, bytes]:
            queue = [head] + [chunk] * total_chunks

            async def receive():
                if not queue:
                    return {"type": "http.disconnect"}
                supplied["chunks"] += 1
                body = queue.pop(0)
                return {"type": "http.request", "body": body, "more_body": bool(queue)}

            sent: list[dict] = []

            async def send(message):
                sent.append(message)

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/photos",
                "raw_path": b"/api/photos",
                "query_string": b"",
                "root_path": "",
                "client": ("127.0.0.1", 5000),
                "server": ("127.0.0.1", 8765),
                "headers": [
                    (b"host", b"127.0.0.1:8765"),
                    (b"origin", ORIGIN.encode()),
                    (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
                ],
            }
            await app(scope, receive, send)
            status = next(m["status"] for m in sent if m["type"] == "http.response.start")
            body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
            return status, body

        status, body = asyncio.run(run())
        assert status == 413
        assert json.loads(body)["code"] == "upload_too_large"
        assert supplied["chunks"] < total_chunks // 2

    @pytest.mark.parametrize("field", ["image", "files", "upload"])
    def test_wrong_field_name_is_422(self, client, circle1, field):
        response = client.post("/api/photos", files={field: ("p.jpg", circle1[0], "image/jpeg")})
        assert_error(response, 422, "missing_file")

    def test_json_body_is_refused(self, client):
        response = client.post("/api/photos", json={"file": "x"})
        assert response.status_code in (415, 422)

    def test_no_body_and_no_content_type_is_4xx_not_500(self, client, conn):
        response = client.post("/api/photos")
        assert_error(response, 415, "unsupported_media_type")
        assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0

    @pytest.mark.parametrize(
        "content_type", ["application/json", "text/plain", "application/octet-stream", "image/jpeg"]
    )
    def test_a_non_multipart_body_is_415(self, client, content_type):
        response = client.post(
            "/api/photos",
            content=b"\xff\xd8\xff\xe0 not multipart",
            headers={"Content-Type": content_type},
        )
        assert_error(response, 415, "unsupported_media_type")

    def test_multipart_without_a_boundary_is_422(self, client):
        response = client.post(
            "/api/photos", content=b"--zzz--\r\n", headers={"Content-Type": "multipart/form-data"}
        )
        assert_error(response, 422, "invalid_upload")

    def test_multipart_with_an_empty_body_is_422_missing_file(self, client):
        response = client.post(
            "/api/photos",
            content=b"",
            headers={"Content-Type": "multipart/form-data; boundary=zzz"},
        )
        assert_error(response, 422, "missing_file")

    def test_the_content_type_check_is_case_insensitive_and_ignores_parameters(
        self, client, circle1
    ):
        boundary = "bnd"
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="p.jpg"\r\n'
                "Content-Type: image/jpeg\r\n\r\n"
            ).encode()
            + circle1[0]
            + f"\r\n--{boundary}--\r\n".encode()
        )
        response = client.post(
            "/api/photos",
            content=body,
            headers={"Content-Type": f"Multipart/Form-Data ; boundary={boundary}"},
        )
        assert response.status_code == 201, response.text

    def test_foreign_origin_cannot_upload(self, client, conn, circle1):
        response = client.post(
            "/api/photos",
            files={"file": ("p.jpg", circle1[0], "image/jpeg")},
            headers={"Origin": "http://evil.example"},
        )
        assert response.status_code == 403
        assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0

    def test_database_busy_is_503_and_other_database_errors_are_not_masked(
        self, client, monkeypatch, circle1
    ):
        def locked(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(photo_service, "store_upload", locked)
        assert_error(upload(client, circle1[0]), 503, "busy")

        def broken(*_a, **_k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(photo_service, "store_upload", broken)
        assert_error(upload(client, circle1[0]), 500, "internal_error")


class TestTinyImages:
    @pytest.mark.parametrize("size", [1, 40, 63])
    @pytest.mark.parametrize("kind", ["circle_1in", "circle_3in", "grid_1in"])
    def test_propose_on_a_tiny_image_is_422_image_too_small(self, client, size, kind):
        tiny = np.full((size, size, 3), 200, np.uint8)
        response = upload(client, encode(tiny, ".png"), "t.png", "image/png")
        assert response.status_code == 201
        photo_id = response.json()["id"]
        proposed = client.post(f"/api/photos/{photo_id}/reference/propose", json={"kind": kind})
        assert_error(proposed, 422, "image_too_small")

    def test_manual_confirm_on_a_tiny_image_is_422_and_nothing_is_confirmed(self, client, conn):
        tiny = np.full((40, 40, 3), 200, np.uint8)
        photo_id = upload(client, encode(tiny, ".png"), "t.png", "image/png").json()["id"]
        marks = [{"x_px": 5, "y_px": 20}, {"x_px": 35, "y_px": 20}, {"x_px": 20, "y_px": 5}]
        response = client.post(
            f"/api/photos/{photo_id}/reference/confirm",
            json={"kind": "circle_1in", "manual": {"edge_points_px": marks}},
        )
        assert_error(response, 422, "image_too_small")
        assert photo_row(conn, photo_id)["reference_confirmed"] == 0

    def test_a_64_pixel_image_is_big_enough_to_be_searched(self, client):
        small = np.full((64, 64, 3), 200, np.uint8)
        photo_id = upload(client, encode(small, ".png"), "s.png", "image/png").json()["id"]
        response = client.post(
            f"/api/photos/{photo_id}/reference/propose", json={"kind": "circle_1in"}
        )
        assert response.status_code == 200 and response.json()["candidates"] == []


# ---------------------------------------------------------------- images


class TestImages:
    def test_oriented_image_is_served_with_the_stored_dimensions(self, client, circle1):
        photo = upload(client, circle1[0]).json()
        response = client.get(f"/api/photos/{photo['id']}/image?variant=oriented")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "no-store"
        decoded = cv2.imdecode(np.frombuffer(response.content, np.uint8), cv2.IMREAD_COLOR)
        assert decoded.shape[:2] == (photo["height_px"], photo["width_px"])

    def test_default_variant_is_oriented(self, client, circle1):
        photo = upload(client, circle1[0]).json()
        assert client.get(f"/api/photos/{photo['id']}/image").status_code == 200

    def test_working_image_before_confirm_is_409(self, client, conn, settings, circle1):
        photo = upload(client, circle1[0]).json()
        assert_error(
            client.get(f"/api/photos/{photo['id']}/image?variant=working"),
            409,
            "reference_unconfirmed",
        )
        assert not (settings.working_dir / f"{photo['id']}.png").exists()

    @pytest.mark.parametrize(
        "query",
        [
            "variant=../../etc/passwd",
            "variant=oriented&path=/etc/passwd",
            "variant=%2Fetc%2Fpasswd",
            "variant=working%00",
            "file=../secret",
        ],
    )
    def test_client_paths_are_never_accepted(self, client, circle1, query):
        photo = upload(client, circle1[0]).json()
        response = client.get(f"/api/photos/{photo['id']}/image?{query}")
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"

    def test_unknown_photo_is_404_everywhere(self, client):
        assert_error(client.get("/api/photos/999/image"), 404, "photo_not_found")
        assert_error(
            client.post("/api/photos/999/reference/propose", json={"kind": "grid_1in"}),
            404,
            "photo_not_found",
        )
        assert_error(
            client.post(
                "/api/photos/999/reference/confirm", json={"kind": "grid_1in", "candidate_index": 0}
            ),
            404,
            "photo_not_found",
        )

    def test_deleted_derived_files_are_rebuilt_from_the_original(self, client, settings, circle1):
        photo = upload(client, circle1[0]).json()
        oriented = settings.working_dir / f"{photo['id']}_oriented.jpg"
        oriented.unlink()
        assert client.get(f"/api/photos/{photo['id']}/image?variant=oriented").status_code == 200
        assert oriented.is_file()


# ---------------------------------------------------------------- propose and confirm


def propose(client, photo_id: int, kind: str) -> ReferenceProposeOut:
    response = client.post(f"/api/photos/{photo_id}/reference/propose", json={"kind": kind})
    assert response.status_code == 200, response.text
    return ReferenceProposeOut.model_validate(response.json())


class TestCircleReference:
    def test_propose_never_confirms_and_never_builds_a_working_image(
        self, client, conn, settings, circle1
    ):
        data, truth = circle1
        photo_id = upload(client, data).json()["id"]
        proposal = propose(client, photo_id, "circle_1in")
        assert proposal.kind.value == "circle_1in" and proposal.candidates
        assert [c.index for c in proposal.candidates] == list(range(len(proposal.candidates)))
        assert all(c.method == "auto" for c in proposal.candidates)
        row = photo_row(conn, photo_id)
        assert row["reference_confirmed"] == 0 and row["working_ppi"] is None
        assert row["h_px_to_plane_json"] is None
        assert not (settings.working_dir / f"{photo_id}.png").exists()

    def test_true_circle_is_proposed_with_overlay_geometry(self, client, circle1):
        data, truth = circle1
        photo_id = upload(client, data).json()["id"]
        candidates = [c.model_dump() for c in propose(client, photo_id, "circle_1in").candidates]
        best = nearest_candidate(candidates, truth["reference"]["center_px"])
        circle = best["circle"]
        assert circle["axis_major_px"] >= circle["axis_minor_px"] > 0
        assert abs(circle["axis_major_px"] - truth["reference"]["axes_px"][0]) < 0.02 * 200
        assert best["grid_points_px"] is None

    def test_confirming_a_candidate_sets_the_scale_and_stores_the_working_image(
        self, client, conn, settings, circle1
    ):
        data, truth = circle1
        photo_id = upload(client, data).json()["id"]
        candidates = [c.model_dump() for c in propose(client, photo_id, "circle_1in").candidates]
        index = nearest_candidate(candidates, truth["reference"]["center_px"])["index"]
        response = client.post(
            f"/api/photos/{photo_id}/reference/confirm",
            json={"kind": "circle_1in", "candidate_index": index},
        )
        assert response.status_code == 200, response.text
        out = ReferenceConfirmOut.model_validate(response.json())
        assert out.photo.reference_confirmed and out.photo.reference_kind.value == "circle_1in"
        assert out.photo.working_ppi == pytest.approx(200.0, rel=0.01)
        assert out.warnings == []
        row = photo_row(conn, photo_id)
        assert row["reference_confirmed"] == 1 and row["working_ppi"] == out.photo.working_ppi
        assert json.loads(row["reference_json"])["method"] == "auto"
        assert np.array(json.loads(row["h_px_to_plane_json"])).shape == (3, 3)
        working = cv2.imread(str(settings.working_dir / f"{photo_id}.png"))
        assert working.shape[:2] == (row["working_h"], row["working_w"])
        served = client.get(f"/api/photos/{photo_id}/image?variant=working")
        assert served.status_code == 200 and served.headers["content-type"] == "image/png"

    def test_tilted_circle_confirms_with_the_tilt_warning(self, client):
        data, _ = synth_bytes("circle_tilt_warn")
        photo_id = upload(client, data).json()["id"]
        proposal = propose(client, photo_id, "circle_1in")
        assert any("looks tilted" in w for w in proposal.candidates[0].warnings)
        response = client.post(
            f"/api/photos/{photo_id}/reference/confirm",
            json={"kind": "circle_1in", "candidate_index": 0},
        )
        assert response.status_code == 200
        assert any("looks tilted" in w for w in response.json()["warnings"])
        assert response.json()["photo"]["axis_ratio"] < 0.98

    def test_no_reference_scene_proposes_nothing_and_manual_marks_confirm(
        self, client, conn, settings
    ):
        data, truth = synth_bytes("no_reference")
        assert truth["reference"] is None
        photo = upload(client, data).json()
        assert propose(client, photo["id"], "circle_1in").candidates == []
        centre, radius = (700.0, 900.0), 100.0
        marks = [
            {"x_px": centre[0] + radius * np.cos(a), "y_px": centre[1] + radius * np.sin(a)}
            for a in (0.2, 2.3, 4.4)
        ]
        response = client.post(
            f"/api/photos/{photo['id']}/reference/confirm",
            json={"kind": "circle_1in", "manual": {"edge_points_px": marks}},
        )
        assert response.status_code == 200, response.text
        assert response.json()["photo"]["working_ppi"] == pytest.approx(200.0, rel=0.01)
        row = photo_row(conn, photo["id"])
        assert json.loads(row["reference_json"])["method"] == "manual"
        assert (settings.working_dir / f"{photo['id']}.png").is_file()

    def test_reconfirming_replaces_the_reference(self, client, conn, circle1):
        data, truth = circle1
        photo_id = upload(client, data).json()["id"]
        candidates = [c.model_dump() for c in propose(client, photo_id, "circle_1in").candidates]
        index = nearest_candidate(candidates, truth["reference"]["center_px"])["index"]
        body = {"kind": "circle_1in", "candidate_index": index}
        first = client.post(f"/api/photos/{photo_id}/reference/confirm", json=body)
        second = client.post(f"/api/photos/{photo_id}/reference/confirm", json=body)
        assert first.status_code == second.status_code == 200
        assert first.json()["photo"]["id"] == second.json()["photo"]["id"]

    def test_deleted_working_image_is_rebuilt_from_the_stored_transform(
        self, client, settings, circle1
    ):
        data, truth = circle1
        photo_id = upload(client, data).json()["id"]
        candidates = [c.model_dump() for c in propose(client, photo_id, "circle_1in").candidates]
        index = nearest_candidate(candidates, truth["reference"]["center_px"])["index"]
        client.post(
            f"/api/photos/{photo_id}/reference/confirm",
            json={"kind": "circle_1in", "candidate_index": index},
        )
        path = settings.working_dir / f"{photo_id}.png"
        before = cv2.imread(str(path))
        path.unlink()
        assert client.get(f"/api/photos/{photo_id}/image?variant=working").status_code == 200
        assert cv2.imread(str(path)).shape == before.shape


class TestGridReference:
    def test_grid_is_proposed_as_four_corner_points(self, client, grid):
        data, _ = grid
        photo_id = upload(client, data).json()["id"]
        proposal = propose(client, photo_id, "grid_1in")
        assert len(proposal.candidates) == 1
        candidate = proposal.candidates[0]
        assert candidate.circle is None and len(candidate.grid_points_px) == 4

    def test_confirming_the_grid_candidate_sets_the_scale(self, client, grid):
        data, _ = grid
        photo_id = upload(client, data).json()["id"]
        propose(client, photo_id, "grid_1in")
        response = client.post(
            f"/api/photos/{photo_id}/reference/confirm",
            json={"kind": "grid_1in", "candidate_index": 0},
        )
        assert response.status_code == 200, response.text
        assert response.json()["photo"]["working_ppi"] == pytest.approx(160.0, rel=0.02)

    def test_manual_four_corners_confirm_the_grid(self, client, grid):
        data, truth = grid
        photo_id = upload(client, data).json()["id"]
        ref = truth["reference"]
        corners = [{"x_px": x, "y_px": y} for x, y in ref["corners_px"]]
        response = client.post(
            f"/api/photos/{photo_id}/reference/confirm",
            json={
                "kind": "grid_1in",
                "manual": {
                    "grid": {
                        "corners_px": corners,
                        "squares_x": ref["squares_across"],
                        "squares_y": ref["squares_down"],
                    }
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["photo"]["working_ppi"] == pytest.approx(160.0, rel=0.02)
        assert response.json()["warnings"] == []

    def test_curved_sheet_shows_the_non_planar_warning_in_proposal_and_confirmation(self, client):
        data, truth = synth_bytes("curved_green_grid")
        assert truth["non_planar"] is True
        photo_id = upload(client, data).json()["id"]
        proposal = propose(client, photo_id, "grid_1in")
        assert any("non_planar" in w and "re-shoot" in w for w in proposal.candidates[0].warnings)
        response = client.post(
            f"/api/photos/{photo_id}/reference/confirm",
            json={"kind": "grid_1in", "candidate_index": 0},
        )
        assert response.status_code == 200
        assert any("non_planar" in w for w in response.json()["warnings"])


class TestConfirmDenials:
    @pytest.fixture
    def photo_id(self, client, circle1) -> int:
        return upload(client, circle1[0]).json()["id"]

    def confirm(self, client, photo_id, body):
        return client.post(f"/api/photos/{photo_id}/reference/confirm", json=body)

    def test_candidate_index_out_of_range_is_422(self, client, conn, photo_id):
        response = self.confirm(client, photo_id, {"kind": "circle_1in", "candidate_index": 99})
        assert_error(response, 422, "invalid_candidate_index")
        assert photo_row(conn, photo_id)["reference_confirmed"] == 0

    def test_confirming_a_candidate_when_nothing_was_found_is_422(self, client):
        data, _ = synth_bytes("no_reference")
        photo_id = upload(client, data).json()["id"]
        response = self.confirm(client, photo_id, {"kind": "circle_1in", "candidate_index": 0})
        assert_error(response, 422, "invalid_candidate_index")

    def test_candidate_and_manual_together_or_neither_are_422(self, client, photo_id):
        marks = [{"x_px": 1, "y_px": 2}, {"x_px": 5, "y_px": 9}, {"x_px": 8, "y_px": 3}]
        both = {"kind": "circle_1in", "candidate_index": 0, "manual": {"edge_points_px": marks}}
        assert_error_code(self.confirm(client, photo_id, both), 422, "validation_error")
        assert_error_code(
            self.confirm(client, photo_id, {"kind": "circle_1in"}), 422, "validation_error"
        )

    def test_manual_marks_must_match_the_kind(self, client, photo_id):
        marks = [{"x_px": 1, "y_px": 2}, {"x_px": 5, "y_px": 9}, {"x_px": 8, "y_px": 3}]
        response = self.confirm(
            client, photo_id, {"kind": "grid_1in", "manual": {"edge_points_px": marks}}
        )
        assert_error_code(response, 422, "validation_error")

    def test_collinear_marks_are_422_invalid_reference(self, client, conn, photo_id):
        marks = [{"x_px": 100 + 50 * i, "y_px": 300} for i in range(3)]
        response = self.confirm(
            client, photo_id, {"kind": "circle_1in", "manual": {"edge_points_px": marks}}
        )
        assert_error(response, 422, "invalid_reference")
        assert photo_row(conn, photo_id)["reference_confirmed"] == 0

    def test_degenerate_grid_marks_are_422_invalid_reference(self, client, photo_id):
        corners = [{"x_px": 100, "y_px": 100}] * 4
        response = self.confirm(
            client,
            photo_id,
            {
                "kind": "grid_1in",
                "manual": {"grid": {"corners_px": corners, "squares_x": 4, "squares_y": 4}},
            },
        )
        assert_error(response, 422, "invalid_reference")

    def test_unknown_kind_and_extra_fields_are_422(self, client, photo_id):
        assert_error_code(
            self.confirm(client, photo_id, {"kind": "circle_9in", "candidate_index": 0}),
            422,
            "validation_error",
        )
        extra = {"kind": "circle_1in", "candidate_index": 0, "confirmed": True}
        assert_error_code(self.confirm(client, photo_id, extra), 422, "validation_error")
        response = client.post(f"/api/photos/{photo_id}/reference/propose", json={"kind": "bogus"})
        assert_error_code(response, 422, "validation_error")

    def test_foreign_origin_cannot_confirm_or_propose(self, client, conn, photo_id):
        headers = {"Origin": "http://evil.example"}
        body = {"kind": "circle_1in", "candidate_index": 0}
        assert (
            client.post(
                f"/api/photos/{photo_id}/reference/confirm", json=body, headers=headers
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/photos/{photo_id}/reference/propose",
                json={"kind": "circle_1in"},
                headers=headers,
            ).status_code
            == 403
        )
        assert photo_row(conn, photo_id)["reference_confirmed"] == 0

    def test_reference_is_locked_once_groups_hold_measurements(self, client, conn, circle1):
        data, truth = circle1
        photo = upload(client, data).json()
        candidates = [c.model_dump() for c in propose(client, photo["id"], "circle_1in").candidates]
        index = nearest_candidate(candidates, truth["reference"]["center_px"])["index"]
        body = {"kind": "circle_1in", "candidate_index": index}
        assert self.confirm(client, photo["id"], body).status_code == 200
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        context = seed_context(rifle)
        seed_group(
            conn,
            context=context,
            load=load,
            offsets=[Offset(0.1, 0.1), Offset(-0.1, 0.0), Offset(0.0, -0.1)],
            photo=photo["id"],
        )
        response = self.confirm(client, photo["id"], body)
        assert_error(response, 409, "reference_locked")

    def test_locked_reference_is_refused_before_the_image_is_even_loaded(
        self, client, conn, monkeypatch, circle1
    ):
        photo = upload(client, circle1[0]).json()
        monkeypatch.setattr(photo_service, "_photo_has_measurements", lambda *_a: True)

        def never(*_a, **_k):
            raise AssertionError("the original must not be decoded for a locked photo")

        monkeypatch.setattr(photo_service, "_load_original", never)
        body = {"kind": "circle_1in", "candidate_index": 0}
        assert_error(self.confirm(client, photo["id"], body), 409, "reference_locked")

    def test_a_group_created_during_confirmation_still_blocks_it(
        self, client, conn, settings, monkeypatch, circle1
    ):
        data, truth = circle1
        photo = upload(client, data).json()
        candidates = [c.model_dump() for c in propose(client, photo["id"], "circle_1in").candidates]
        index = nearest_candidate(candidates, truth["reference"]["center_px"])["index"]
        answers = iter([False, True])
        monkeypatch.setattr(photo_service, "_photo_has_measurements", lambda *_a: next(answers))
        body = {"kind": "circle_1in", "candidate_index": index}
        assert_error(self.confirm(client, photo["id"], body), 409, "reference_locked")
        assert photo_row(conn, photo["id"])["reference_confirmed"] == 0
        assert not (settings.working_dir / f"{photo['id']}.png").exists()

    def test_a_stored_region_alone_does_not_lock_and_confirming_clears_it(
        self, client, conn, circle1
    ):
        data, truth = circle1
        photo = upload(client, data).json()
        index = right_candidate_index(client, photo["id"], truth)
        mine = seed_draft_group(conn, photo["id"], roi_json=ROI_JSON)
        other = seed_draft_group(conn, photo["id"], roi_json=ROI_JSON)
        elsewhere_photo = seed_photo(conn)
        elsewhere = seed_draft_group(conn, elsewhere_photo.id, roi_json=ROI_JSON)
        response = self.confirm(
            client, photo["id"], {"kind": "circle_1in", "candidate_index": index}
        )
        assert response.status_code == 200, response.text
        assert roi_of(conn, mine.id) is None and roi_of(conn, other.id) is None
        assert roi_of(conn, elsewhere.id) == ROI_JSON

    def test_a_group_with_shots_and_no_aim_locks_the_reference(self, client, conn, circle1):
        data, truth = circle1
        photo = upload(client, data).json()
        index = right_candidate_index(client, photo["id"], truth)
        seed_group_with_shots(conn, photo["id"], aim=None)
        response = self.confirm(
            client, photo["id"], {"kind": "circle_1in", "candidate_index": index}
        )
        assert_error(response, 409, "reference_locked")

    def test_a_group_with_an_aim_only_locks_the_reference(self, client, conn, circle1):
        data, truth = circle1
        photo = upload(client, data).json()
        index = right_candidate_index(client, photo["id"], truth)
        seed_draft_group(conn, photo["id"], aim=PlanePoint(4.0, 5.0), roi_json=ROI_JSON)
        response = self.confirm(
            client, photo["id"], {"kind": "circle_1in", "candidate_index": index}
        )
        assert_error(response, 409, "reference_locked")
        assert photo_row(conn, photo["id"])["reference_confirmed"] == 0

    def test_a_refused_confirmation_leaves_the_regions_alone(self, client, conn, circle1):
        data, truth = circle1
        photo = upload(client, data).json()
        index = right_candidate_index(client, photo["id"], truth)
        seed_group_with_shots(conn, photo["id"], roi_json=ROI_JSON)
        response = self.confirm(
            client, photo["id"], {"kind": "circle_1in", "candidate_index": index}
        )
        assert_error(response, 409, "reference_locked")
        assert conn.execute("SELECT roi_json FROM groups").fetchone()[0] == ROI_JSON

    def test_a_failed_confirmation_does_not_clear_the_regions(self, client, conn, circle1):
        photo = upload(client, circle1[0]).json()
        group = seed_draft_group(conn, photo["id"], roi_json=ROI_JSON)
        response = self.confirm(client, photo["id"], {"kind": "circle_1in", "candidate_index": 99})
        assert_error(response, 422, "invalid_candidate_index")
        assert roi_of(conn, group.id) == ROI_JSON

    def test_wrong_circle_then_detect_then_clear_then_reconfirm_succeeds(
        self, client, conn, circle1
    ):
        """The review-21 path: the user picked the wrong circle and wants to start over."""
        data, truth = circle1
        photo = upload(client, data).json()
        pid = photo["id"]
        candidates = [c.model_dump() for c in propose(client, pid, "circle_1in").candidates]
        right = nearest_candidate(candidates, truth["reference"]["center_px"])["index"]
        wrong = next(c["index"] for c in candidates if c["index"] != right)
        assert (
            self.confirm(client, pid, {"kind": "circle_1in", "candidate_index": wrong}).status_code
            == 200
        )

        group = seed_draft_group(conn, pid, shots_expected=3)
        detect = client.post(
            f"/api/groups/{group.id}/detect",
            json={"roi_px": {"x0": 10, "y0": 10, "x1": 400, "y1": 400}},
        )
        assert detect.status_code == 200, detect.text
        assert roi_of(conn, group.id) is not None
        saved = client.put(
            f"/api/groups/{group.id}/review",
            json={
                "aim": {"x_in": 4.0, "y_in": 5.0},
                "shots": [
                    {"x_in": 4.1 + i * 0.3, "y_in": 5.0, "origin": "manual", "moved_by_user": False}
                    for i in range(3)
                ],
                "rejections": [],
                "shots_expected": 3,
            },
        )
        assert saved.status_code == 200, saved.text

        body = {"kind": "circle_1in", "candidate_index": right}
        assert_error(self.confirm(client, pid, body), 409, "reference_locked")

        cleared = client.put(
            f"/api/groups/{group.id}/review",
            json={
                "aim": None,
                "shots": [],
                "rejections": [
                    {"x_in": 4.1 + i * 0.3, "y_in": 5.0, "reason": "not_a_bullet_hole"}
                    for i in range(3)
                ],
                "shots_expected": 3,
            },
        )
        assert cleared.status_code == 200, cleared.text
        assert roi_of(conn, group.id) is not None

        again = self.confirm(client, pid, body)
        assert again.status_code == 200, again.text
        assert roi_of(conn, group.id) is None
        assert photo_row(conn, pid)["reference_confirmed"] == 1

    def test_a_group_without_measurements_does_not_lock_the_reference(self, client, conn, circle1):
        data, truth = circle1
        photo = upload(client, data).json()
        candidates = [c.model_dump() for c in propose(client, photo["id"], "circle_1in").candidates]
        index = nearest_candidate(candidates, truth["reference"]["center_px"])["index"]
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        context = seed_context(rifle)
        seed_group(
            conn,
            context=context,
            load=load,
            offsets=[],
            shots_expected=5,
            finalize=False,
            aim=None,
            photo=photo["id"],
        )
        response = self.confirm(
            client, photo["id"], {"kind": "circle_1in", "candidate_index": index}
        )
        assert response.status_code == 200


ROI_JSON = '{"x0": 10, "y0": 10, "x1": 200, "y1": 200}'


def roi_of(conn: sqlite3.Connection, group_id: int) -> str | None:
    return conn.execute("SELECT roi_json FROM groups WHERE id = ?", (group_id,)).fetchone()[0]


def right_candidate_index(client, photo_id: int, truth: dict) -> int:
    candidates = [c.model_dump() for c in propose(client, photo_id, "circle_1in").candidates]
    return nearest_candidate(candidates, truth["reference"]["center_px"])["index"]


def seed_draft_group(conn, photo_id: int, *, aim=None, roi_json=None, shots_expected: int = 5):
    rifle = seed_rifle(conn)
    return seed_group(
        conn,
        context=seed_context(rifle),
        load=seed_load(conn, rifle),
        offsets=[],
        shots_expected=shots_expected,
        finalize=False,
        aim=aim,
        photo=photo_id,
        roi_json=roi_json,
    )


def seed_group_with_shots(conn, photo_id: int, *, roi_json=None, aim=None):
    rifle = seed_rifle(conn)
    return seed_group(
        conn,
        context=seed_context(rifle),
        load=seed_load(conn, rifle),
        offsets=[Offset(0.1, 0.1), Offset(-0.1, 0.0), Offset(0.0, -0.1)],
        finalize=False,
        aim=aim,
        photo=photo_id,
        roi_json=roi_json,
    )


def assert_error_code(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["code"] == code


# ---------------------------------------------------------------- reset the reference (B-79)


def shot_count(conn: sqlite3.Connection, group_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM shots WHERE group_id = ?", (group_id,)).fetchone()[0]


def rejection_count(conn: sqlite3.Connection, group_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM detection_rejections WHERE group_id = ?", (group_id,)
    ).fetchone()[0]


def aim_of(conn: sqlite3.Connection, group_id: int):
    return conn.execute(
        "SELECT aim_x_in, aim_y_in FROM groups WHERE id = ?", (group_id,)
    ).fetchone()[:]


def final_group(conn, photo_id: int):
    rifle = seed_rifle(conn)
    return seed_group(
        conn,
        context=seed_context(rifle),
        load=seed_load(conn, rifle),
        offsets=[Offset(0.1, 0.1), Offset(-0.1, 0.0), Offset(0.0, -0.1)],
        photo=photo_id,
    )


class TestResetReference:
    @pytest.fixture
    def confirmed(self, client, circle1) -> int:
        data, truth = circle1
        pid = upload(client, data).json()["id"]
        body = {"kind": "circle_1in", "candidate_index": right_candidate_index(client, pid, truth)}
        assert client.post(f"/api/photos/{pid}/reference/confirm", json=body).status_code == 200
        return pid

    def reset(self, client, photo_id, **kwargs):
        return client.post(f"/api/photos/{photo_id}/reference/reset", **kwargs)

    def test_it_clears_the_reference_and_every_draft_groups_work_and_nothing_else(
        self, client, conn, confirmed
    ):
        worked = seed_group_with_shots(conn, confirmed, roi_json=ROI_JSON, aim=PlanePoint(4.0, 5.0))
        with conn:
            conn.execute(
                "INSERT INTO detection_rejections (group_id, x_in, y_in, reason, created_at) "
                "VALUES (?, 1, 1, 'not_a_bullet_hole', '2026-10-02T00:00:00')",
                (worked.id,),
            )
        region_only = seed_draft_group(conn, confirmed, roi_json=ROI_JSON)
        other_photo = upload(client, synth_bytes("clean_circle1", seed=7)[0]).json()["id"]
        elsewhere = seed_group_with_shots(
            conn, other_photo, roi_json=ROI_JSON, aim=PlanePoint(4.0, 5.0)
        )

        response = self.reset(client, confirmed)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["cleared_groups"] == 1
        assert body["photo"]["id"] == confirmed and body["photo"]["reference_confirmed"] is False
        row = photo_row(conn, confirmed)
        assert row["reference_confirmed"] == 0
        assert all(
            row[name] is None
            for name in (
                "reference_kind",
                "reference_json",
                "h_px_to_plane_json",
                "working_ppi",
                "working_w",
                "working_h",
                "axis_ratio",
            )
        )
        assert (shot_count(conn, worked.id), rejection_count(conn, worked.id)) == (0, 0)
        assert aim_of(conn, worked.id) == (None, None)
        assert roi_of(conn, worked.id) is None and roi_of(conn, region_only.id) is None
        assert shot_count(conn, elsewhere.id) == 3
        assert aim_of(conn, elsewhere.id) == (4.0, 5.0) and roi_of(conn, elsewhere.id) == ROI_JSON
        assert client.get(f"/api/photos/{confirmed}/image?variant=working").status_code == 409

    def test_a_different_reference_can_be_confirmed_afterwards(self, client, conn, confirmed):
        seed_group_with_shots(conn, confirmed, aim=PlanePoint(4.0, 5.0))
        assert_error(
            client.post(
                f"/api/photos/{confirmed}/reference/confirm",
                json={"kind": "circle_1in", "candidate_index": 0},
            ),
            409,
            "reference_locked",
        )

        assert self.reset(client, confirmed).status_code == 200
        again = client.post(
            f"/api/photos/{confirmed}/reference/confirm",
            json={"kind": "circle_1in", "candidate_index": 0},
        )

        assert again.status_code == 200, again.text
        assert photo_row(conn, confirmed)["reference_confirmed"] == 1

    def test_nothing_saved_is_a_clean_reset_with_zero_cleared(self, client, conn, confirmed):
        group = seed_draft_group(conn, confirmed, roi_json=ROI_JSON)

        response = self.reset(client, confirmed)

        assert response.status_code == 200 and response.json()["cleared_groups"] == 0
        assert roi_of(conn, group.id) is None and photo_row(conn, confirmed)["working_ppi"] is None

    def test_resetting_an_unconfirmed_photo_is_harmless(self, client, circle1):
        pid = upload(client, circle1[0]).json()["id"]

        response = self.reset(client, pid)

        assert response.status_code == 200
        assert response.json()["cleared_groups"] == 0

    def test_a_finalized_group_on_the_photo_refuses_and_changes_nothing(
        self, client, conn, confirmed
    ):
        done = final_group(conn, confirmed)
        draft = seed_group_with_shots(conn, confirmed, roi_json=ROI_JSON, aim=PlanePoint(4.0, 5.0))

        response = self.reset(client, confirmed)

        assert_error(response, 409, "reference_in_use")
        assert photo_row(conn, confirmed)["reference_confirmed"] == 1
        assert shot_count(conn, draft.id) == 3 and roi_of(conn, draft.id) == ROI_JSON
        assert aim_of(conn, draft.id) == (4.0, 5.0)
        assert shot_count(conn, done.id) == 3 and aim_of(conn, done.id)[0] is not None

    def test_a_finalized_group_on_another_photo_does_not_block(self, client, conn, confirmed):
        final_group(conn, seed_photo(conn).id)

        assert self.reset(client, confirmed).status_code == 200

    def test_a_failure_part_way_rolls_everything_back(self, conn, confirmed, client, monkeypatch):
        draft = seed_group_with_shots(conn, confirmed, roi_json=ROI_JSON, aim=PlanePoint(4.0, 5.0))

        def explode(*_a, **_k):
            raise sqlite3.IntegrityError("injected failure")

        monkeypatch.setattr(photo_service.repo_groups, "update_photo_reference", explode)
        with pytest.raises(sqlite3.IntegrityError):
            photo_service.reset_reference(conn, confirmed)

        assert not conn.in_transaction
        assert photo_row(conn, confirmed)["reference_confirmed"] == 1
        assert shot_count(conn, draft.id) == 3 and roi_of(conn, draft.id) == ROI_JSON
        assert aim_of(conn, draft.id) == (4.0, 5.0)

    def test_unknown_photo_is_404(self, client):
        assert_error(self.reset(client, 9999), 404, "photo_not_found")
        assert self.reset(client, 2**63).status_code == 404

    def test_a_foreign_origin_is_403_and_changes_nothing(self, client, conn, confirmed):
        draft = seed_group_with_shots(conn, confirmed, aim=PlanePoint(4.0, 5.0))

        response = self.reset(client, confirmed, headers={"Origin": "http://evil.example"})

        assert response.status_code == 403
        assert photo_row(conn, confirmed)["reference_confirmed"] == 1
        assert shot_count(conn, draft.id) == 3

    def test_a_non_json_body_is_415_and_changes_nothing(self, client, conn, confirmed):
        response = self.reset(
            client, confirmed, content="x=1", headers={"Content-Type": "text/plain"}
        )

        assert response.status_code == 415
        assert photo_row(conn, confirmed)["reference_confirmed"] == 1

    def test_a_busy_database_is_503_and_other_database_errors_are_not_masked(
        self, client, monkeypatch, confirmed
    ):
        def locked(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(photo_service, "reset_reference", locked)
        assert_error(self.reset(client, confirmed), 503, "busy")

        def broken(*_a, **_k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(photo_service, "reset_reference", broken)
        assert_error(self.reset(client, confirmed), 500, "internal_error")

    def test_the_route_is_registered_without_a_request_body(self):
        from rga.web.schemas import ROUTE_SCHEMAS, ReferenceResetOut

        route = ROUTE_SCHEMAS[("POST", "/api/photos/{id}/reference/reset")]
        assert route.request is None and route.response is ReferenceResetOut

    def test_the_work_summary_counts_drafts_with_work_and_flags_a_final_group(
        self, client, conn, confirmed
    ):
        assert photo_service.reference_work(conn, confirmed) == photo_service.ReferenceWork(
            0, False
        )
        seed_draft_group(conn, confirmed)
        seed_draft_group(conn, confirmed, aim=PlanePoint(4.0, 5.0))
        seed_group_with_shots(conn, confirmed)
        assert photo_service.reference_work(conn, confirmed) == photo_service.ReferenceWork(
            2, False
        )
        final_group(conn, confirmed)
        assert photo_service.reference_work(conn, confirmed) == photo_service.ReferenceWork(2, True)


# ---------------------------------------------------------------- the page


class TestReferencePage:
    def test_page_renders_with_the_photo_and_all_four_reference_kinds(self, client, circle1):
        photo = upload(client, circle1[0]).json()
        response = client.get(f"/photos/{photo['id']}/reference")
        assert response.status_code == 200
        html = response.text
        assert response.headers["content-type"].startswith("text/html")
        assert f'data-photo-id="{photo["id"]}"' in html
        assert f'data-width="{photo["width_px"]}"' in html
        for kind in (
            "grid_1in",
            "circle_1in",
            "circle_1_5in",
            "circle_2in",
            "circle_2_5in",
            "circle_3in",
        ):
            assert f'<option value="{kind}">' in html
        assert f"/api/photos/{photo['id']}/image?variant=oriented" in html
        assert '<script type="module" src="/static/js/reference.js"></script>' in html
        assert "already confirmed" not in html

    def test_page_obeys_the_csp_rules(self, client, circle1):
        photo = upload(client, circle1[0]).json()
        html = client.get(f"/photos/{photo['id']}/reference").text
        assert not re.search(r"<script(?![^>]*\bsrc=)", html)
        assert "<style" not in html and " style=" not in html
        assert not re.search(r"\son[a-z]+\s*=", html) and "javascript:" not in html
        assert "http://" not in html.replace("http://www.w3.org/2000/svg", "")

    def test_group_id_builds_the_review_link_and_return_to_is_validated(
        self, client, conn, circle1
    ):
        photo = upload(client, circle1[0]).json()
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        context = seed_context(rifle)
        group = seed_group(
            conn, context=context, load=load, offsets=[], shots_expected=5, finalize=False, aim=None
        )
        page = f"/photos/{photo['id']}/reference"
        plain = client.get(f"{page}?group_id={group.id}").text
        assert f'data-review-url="/groups/{group.id}/review"' in plain
        safe = client.get(f"{page}?group_id={group.id}&return_to=/rifles/3").text
        assert f'data-review-url="/groups/{group.id}/review?return_to=/rifles/3"' in safe
        assert 'data-return-to="/rifles/3"' in safe
        for hostile in ("//evil.example/x", "https://evil.example", "/a\\b"):
            html = client.get(page, params={"group_id": group.id, "return_to": hostile}).text
            assert "evil" not in html and "\\" not in html.split("data-return-to=")[1][:40]
            assert 'data-return-to="/"' in html

    def test_unknown_photo_or_group_is_404(self, client, circle1):
        photo = upload(client, circle1[0]).json()
        assert_error(client.get("/photos/999/reference"), 404, "photo_not_found")
        assert_error(
            client.get(f"/photos/{photo['id']}/reference?group_id=999"), 404, "group_not_found"
        )

    def test_a_confirmed_photo_is_never_offered_its_old_scale(self, client, conn):
        seeded = seed_photo(conn, confirmed=True)
        html = client.get(f"/photos/{seeded.id}/reference").text
        assert "already confirmed" not in html and "keep-reference" not in html
        assert '<div id="reference-controls" class="stack">' in html
        assert 'data-confirmed="1"' in html and 'data-locked="0"' in html

    def test_a_region_alone_does_not_lock_the_scale(self, client, conn):
        seeded = seed_photo(conn, confirmed=True)
        seed_draft_group(conn, seeded.id, roi_json=ROI_JSON)
        html = client.get(f"/photos/{seeded.id}/reference").text
        assert 'data-locked="0"' in html and 'id="locked-scale"' not in html

    @pytest.mark.parametrize("kind", ["shots", "aim"])
    def test_a_group_with_shots_or_an_aim_fixes_the_scale_and_next_goes_on(
        self, client, conn, kind
    ):
        seeded = seed_photo(conn, confirmed=True)
        if kind == "shots":
            seed_group_with_shots(conn, seeded.id)
        else:
            seed_draft_group(conn, seeded.id, aim=PlanePoint(4.0, 5.0))
        html = client.get(f"/photos/{seeded.id}/reference").text
        assert 'data-locked="1"' in html and 'id="locked-scale"' in html
        assert '<div id="reference-controls" class="stack" hidden>' in html
        assert '<button type="button" id="reference-next">Next</button>' in html
        assert "keep-reference" not in html

    def test_page_script_only_uses_api_helpers(self):
        script = (PROJECT_ROOT / "src/rga/web/static/js/reference.js").read_text(encoding="utf-8")
        assert (
            "fetch(" not in script
            and "innerHTML" not in script
            and 'setAttribute("style"' not in script
        )
        assert "reference/confirm" in script
        assert script.count("reference/confirm") == 2
        for forbidden in ("mean_radius", "best_of", "group_reduction"):
            assert forbidden not in script


# ----------------------------------------------------------------------------------- capture time


def exif_jpeg(taken: str | None, offset: str | None = None, *, size=(240, 180)) -> bytes:
    """A JPEG whose Exif sub-IFD carries DateTimeOriginal (and OffsetTimeOriginal)."""
    exif = Image.Exif()
    sub = exif.get_ifd(0x8769)
    if taken is not None:
        sub[0x9003] = taken
    if offset is not None:
        sub[0x9011] = offset
    exif[0x0112] = 1
    img = Image.effect_noise(size, 40).convert("RGB")
    buffer = io.BytesIO()
    img.save(buffer, "JPEG", exif=exif)
    return buffer.getvalue()


class TestCaptureTime:
    def test_the_exif_capture_time_and_offset_are_stored_and_returned(self, client, conn):
        response = upload(client, exif_jpeg("2026:09:12 08:15:22", "-06:00"))

        body = response.json()
        assert response.status_code == 201
        PhotoOut.model_validate(body)
        assert (body["taken_at"], body["taken_at_offset"]) == ("2026-09-12T08:15:22", "-06:00")
        row = photo_row(conn, body["id"])
        assert (row["taken_at"], row["taken_at_offset"]) == ("2026-09-12T08:15:22", "-06:00")

    def test_a_missing_offset_tag_leaves_the_offset_null(self, client):
        body = upload(client, exif_jpeg("2026:09:12 08:15:22")).json()

        assert body["taken_at"] == "2026-09-12T08:15:22" and body["taken_at_offset"] is None

    @pytest.mark.parametrize(
        "taken",
        [None, "0000:00:00 00:00:00", "2026:13:45 25:61:61", "not a date", "1850:01:01 00:00:00"],
    )
    def test_a_missing_or_malformed_date_never_rejects_the_image(self, client, taken):
        response = upload(client, exif_jpeg(taken))

        assert response.status_code == 201
        assert (response.json()["taken_at"], response.json()["taken_at_offset"]) == (None, None)

    def test_a_photo_without_exif_has_no_capture_time(self, client, circle1):
        body = upload(client, circle1[0]).json()

        assert (body["taken_at"], body["taken_at_offset"]) == (None, None)

    def test_a_duplicate_upload_returns_the_stored_capture_time(self, client):
        data = exif_jpeg("2026:09:12 08:15:22", "+09:00")
        first = upload(client, data).json()

        again = upload(client, data).json()

        assert again["id"] == first["id"] and again["duplicate"] is True
        assert (again["taken_at"], again["taken_at_offset"]) == ("2026-09-12T08:15:22", "+09:00")

    def test_a_stored_photo_missing_its_time_is_backfilled_by_a_duplicate_upload(
        self, client, conn
    ):
        data = exif_jpeg("2026:09:12 08:15:22", "-06:00")
        first = upload(client, data).json()
        with conn:
            conn.execute(
                "UPDATE photos SET taken_at = NULL, taken_at_offset = NULL WHERE id = ?",
                (first["id"],),
            )

        again = upload(client, data).json()

        assert again["id"] == first["id"]
        assert (again["taken_at"], again["taken_at_offset"]) == ("2026-09-12T08:15:22", "-06:00")
        assert photo_row(conn, first["id"])["taken_at"] == "2026-09-12T08:15:22"

    def test_a_group_created_from_the_uploaded_photo_takes_its_date(self, client, conn):
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        photo = upload(client, exif_jpeg("2026:09:12 08:15:22", "-06:00")).json()

        response = client.post(
            "/api/groups",
            json={
                "load_id": load.id,
                "lot_code": "L1",
                "shots_expected": 5,
                "distance_yd": 100,
                "position": "bench",
                "suppressor": False,
                "photo_id": photo["id"],
            },
        )

        out = response.json()
        assert response.status_code == 201
        assert (out["shot_date"], out["shot_time"], out["utc_offset"], out["shot_at_source"]) == (
            "2026-09-12",
            "08:15:22",
            "-06:00",
            "photo",
        )
