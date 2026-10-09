"""Groups tab photo previews: the `thumb` image variant (small, cached, oriented), the row markup
(a thumbnail cell only fetched on hover), the cache headers and build cookie that keep page scripts current, and a
Chromium check that hovering a row shows its photo at the row's right end, as tall as the row.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rga.config import Settings
from rga.db.connection import connect
from rga.domain.types import Offset
from rga.services import photos as photo_service
from rga.web.app import create_app
from tests.support.live_server import run_live_server
from tests.support.seed import seed_context, seed_group, seed_load, seed_rifle

ORIGIN = "http://127.0.0.1:8765"


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


def jpeg(width: int, height: int) -> bytes:
    img = np.full((height, width, 3), 230, np.uint8)
    cv2.circle(img, (width // 2, height // 2), min(width, height) // 4, (40, 90, 230), -1)
    ok, buffer = cv2.imencode(".jpg", img)
    assert ok
    return buffer.tobytes()


def upload(client: TestClient, width: int = 1200, height: int = 1600) -> dict:
    response = client.post(
        "/api/photos", files={"file": ("t.jpg", jpeg(width, height), "image/jpeg")}
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestThumbVariant:
    def test_the_thumbnail_keeps_the_aspect_and_fits_the_size_limit(self, client):
        photo = upload(client, 1200, 1600)

        response = client.get(f"/api/photos/{photo['id']}/image", params={"variant": "thumb"})

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        img = cv2.imdecode(np.frombuffer(response.content, np.uint8), cv2.IMREAD_COLOR)
        assert img.shape[:2] == (photo_service.THUMB_MAX_SIDE_PX, 180)
        assert len(response.content) < 60_000

    def test_a_small_photo_is_not_enlarged_and_the_file_is_cached(self, client, settings):
        photo = upload(client, 200, 120)

        first = client.get(f"/api/photos/{photo['id']}/image?variant=thumb")
        path = photo_service.thumb_path(settings, photo["id"])
        stamp = path.stat().st_mtime_ns
        second = client.get(f"/api/photos/{photo['id']}/image?variant=thumb")

        img = cv2.imdecode(np.frombuffer(first.content, np.uint8), cv2.IMREAD_COLOR)
        assert img.shape[:2] == (120, 200)
        assert second.content == first.content and path.stat().st_mtime_ns == stamp
        assert oct(path.stat().st_mode & 0o777) == "0o600"

    def test_an_unknown_photo_or_variant_is_refused(self, client):
        assert client.get("/api/photos/999/image?variant=thumb").status_code == 404
        photo = upload(client)
        assert client.get(f"/api/photos/{photo['id']}/image?variant=tiny").status_code == 422


class TestRowMarkup:
    def test_a_row_with_a_photo_has_a_lazy_thumbnail_and_one_without_has_none(self, client, conn):
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        photo = upload(client)
        with_photo = seed_group(
            conn,
            load=load,
            offsets=[],
            context=seed_context(rifle),
            photo=photo["id"],
            finalize=False,
            aim=None,
        )
        without = seed_group(
            conn, load=load, offsets=[], context=seed_context(rifle), finalize=False, aim=None
        )

        html = client.get("/groups").text

        def row(group_id: int) -> str:
            return re.search(
                rf'<tr class="clickable" data-group-id="{group_id}".*?</tr>', html, re.S
            )[0]

        assert re.search(
            rf'<td class="thumb-cell"><img class="row-thumb" alt="" data-thumb="/api/photos/{photo["id"]}/image\?variant=thumb"></td>',
            row(with_photo.id),
        )
        assert " src=" not in row(with_photo.id).split('class="thumb-cell"')[1]
        assert '<td class="thumb-cell"></td>' in row(without.id)
        assert (
            '<th class="thumb-cell"><span class="visually-hidden">Photo preview</span></th>' in html
        )


class TestCacheHeaders:
    def test_static_files_revalidate_and_pages_and_photos_do_not_get_the_header(self, client):
        static = client.get("/static/js/groups_list.js")
        assert static.headers["cache-control"] == "no-cache" and static.headers["etag"]
        assert "cache-control" not in client.get("/groups").headers

    def test_the_home_address_clears_the_sites_cache_on_its_way_to_groups(self, client):
        response = client.get("/", follow_redirects=False)

        assert response.status_code == 303 and response.headers["location"] == "/groups"
        assert response.headers["clear-site-data"] == '"cache"'

    def test_the_first_page_after_a_static_change_clears_the_cache_once(self, client):
        from rga.web.security import BUILD_COOKIE, static_build_id

        build = static_build_id()
        first = client.get("/groups")
        assert first.headers["clear-site-data"] == '"cache"'
        assert client.cookies.get(BUILD_COOKIE) == build

        second = client.get("/groups")
        assert "clear-site-data" not in second.headers

        client.cookies.clear()
        older = client.get("/groups", headers={"cookie": f"{BUILD_COOKIE}=an-older-build"})
        assert older.headers["clear-site-data"] == '"cache"'
        assert f"{BUILD_COOKIE}={build};" in older.headers["set-cookie"]

    def test_static_files_and_api_replies_never_clear_the_cache(self, client):
        assert "clear-site-data" not in client.get("/static/js/groups_list.js").headers
        assert "clear-site-data" not in client.get("/api/rifles").headers

    def test_the_build_id_follows_the_static_files(self, tmp_path):
        from rga.web.security import static_build_id

        (tmp_path / "a.js").write_text("one")
        before = static_build_id(tmp_path)
        assert static_build_id(tmp_path) == before
        (tmp_path / "a.js").write_text("two")
        assert static_build_id(tmp_path) != before


def _playwright_ready() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
def test_hovering_a_row_shows_its_photo_at_the_right_end_as_tall_as_the_row():
    sync_api = pytest.importorskip("playwright.sync_api")
    with run_live_server() as server, sync_api.sync_playwright() as playwright:
        settings = Settings(data_dir=server.data_dir, port=server.port)
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1500, "height": 900})
            problems: list[str] = []
            page.on("pageerror", lambda error: problems.append(str(error)))
            uploaded = page.context.request.post(
                f"{server}/api/photos",
                multipart={
                    "file": {"name": "t.jpg", "mimeType": "image/jpeg", "buffer": jpeg(1200, 1600)}
                },
                headers={"Origin": str(server)},
            )
            assert uploaded.status == 201, uploaded.text()
            connection = connect(settings.db_path)
            try:
                rifle = seed_rifle(connection)
                load = seed_load(connection, rifle)
                group = seed_group(
                    connection,
                    load=load,
                    offsets=[],
                    context=seed_context(rifle),
                    photo=uploaded.json()["id"],
                    finalize=False,
                    aim=None,
                )
            finally:
                connection.close()
            thumbs: list[str] = []
            page.on("request", lambda r: thumbs.append(r.url) if "variant=thumb" in r.url else None)
            page.goto(f"{server}/groups")
            row = page.locator(f'tr[data-group-id="{group.id}"]')
            img = row.locator("img.row-thumb")
            assert thumbs == [], "nothing is fetched before a row is hovered"
            assert float(img.evaluate("(e) => getComputedStyle(e).opacity")) == 0

            row.locator("td").nth(1).hover()
            page.wait_for_function(
                "(e) => e.complete && e.naturalWidth > 0 && getComputedStyle(e).opacity === '1'",
                arg=img.element_handle(),
            )

            box = img.bounding_box()
            row_box = row.bounding_box()
            notes_box = row.locator("td").nth(-2).bounding_box()
            assert abs(box["height"] - row_box["height"]) <= 2
            assert box["x"] >= notes_box["x"] + notes_box["width"] - 1
            assert box["width"] == pytest.approx(box["height"] * 180 / 240, abs=2)
            assert len(thumbs) == 1

            page.mouse.move(5, 5)
            page.wait_for_function(
                "(e) => getComputedStyle(e).opacity === '0'", arg=img.element_handle()
            )
            assert problems == []
        finally:
            browser.close()


def test_the_analysis_picker_shows_the_same_hover_preview(client):
    html = client.get("/analysis").text
    source = (Path(__file__).resolve().parents[2] / "src/rga/web/static/js/analysis.js").read_text()

    assert "<table data-thumb-table>" in html
    assert '<th class="thumb-cell"><span class="visually-hidden">Photo preview</span></th>' in html
    assert "thumb.dataset.thumb = `/api/photos/${group.photo_id}/image?variant=thumb`;" in source
    assert 'body.addEventListener("mouseover", reveal);' in source
    assert 'body.addEventListener("change", reveal);' in source


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
def test_a_selected_aggregate_row_keeps_showing_its_photo_until_it_is_unselected():
    sync_api = pytest.importorskip("playwright.sync_api")
    with run_live_server() as server, sync_api.sync_playwright() as playwright:
        settings = Settings(data_dir=server.data_dir, port=server.port)
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1500, "height": 900})
            problems: list[str] = []
            page.on("pageerror", lambda error: problems.append(str(error)))
            uploaded = page.context.request.post(
                f"{server}/api/photos",
                multipart={
                    "file": {"name": "t.jpg", "mimeType": "image/jpeg", "buffer": jpeg(1200, 1600)}
                },
                headers={"Origin": str(server)},
            )
            assert uploaded.status == 201, uploaded.text()
            connection = connect(settings.db_path)
            try:
                rifle = seed_rifle(connection)
                load = seed_load(connection, rifle)
                offsets = [Offset(0.1, 0.2), Offset(-0.2, 0.1), Offset(0.0, -0.3)]
                group = seed_group(connection, load=load, offsets=offsets)
                with connection:
                    connection.execute(
                        "UPDATE groups SET photo_id = ? WHERE id = ?",
                        (uploaded.json()["id"], group.id),
                    )
            finally:
                connection.close()
            page.goto(f"{server}/analysis")
            box = page.locator(f'#picker-body input[value="{group.id}"]')
            box.wait_for()
            row = page.locator("#picker-body tr", has=page.locator(f'input[value="{group.id}"]'))
            img = row.locator("img.row-thumb")
            opacity = "(e) => getComputedStyle(e).opacity"
            assert float(img.evaluate(opacity)) == 0

            box.check()
            page.mouse.move(5, 5)
            box.blur()
            page.wait_for_function(
                "(e) => e.complete && e.naturalWidth > 0 && getComputedStyle(e).opacity === '1'",
                arg=img.element_handle(),
            )
            page.wait_for_timeout(300)
            assert float(img.evaluate(opacity)) == 1, "a selected row keeps its photo shown"

            box.uncheck()
            box.blur()
            page.mouse.move(5, 5)
            page.wait_for_function(
                "(e) => getComputedStyle(e).opacity === '0'", arg=img.element_handle()
            )
            assert problems == []
        finally:
            browser.close()
