"""The "Add new group" entry point: the home page leads with it, the Groups tab has it at the top
right, its dialog lists the rifles (a rifle without a load cannot be picked), and the rifle page
starts its new-group form with the photo the dialog uploaded (`?photo_id=`). Also the same-zero
warning on the aggregate (Analysis) page. A Chromium journey runs dialog -> rifle form ->
reference page through the real server.
"""

from __future__ import annotations

import json
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
from rga.web.app import create_app
from tests.support.live_server import run_live_server
from tests.support.seed import seed_load, seed_rifle

ORIGIN = "http://127.0.0.1:8765"
WEB = Path(__file__).resolve().parents[2] / "src" / "rga" / "web"


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


def jpeg(seed: int = 0) -> bytes:
    rng = np.random.default_rng(seed)
    img = np.full((480, 640, 3), 235, np.uint8)
    cv2.circle(img, (320, 240), 90, (40, 90, 230), -1)
    img[rng.integers(0, 480, 50), rng.integers(0, 640, 50)] = 0
    ok, buffer = cv2.imencode(".jpg", img)
    assert ok
    return buffer.tobytes()


def dialog_options(html: str) -> list[tuple[str, bool, str]]:
    """(value, disabled, label) for each rifle option in the dialog."""
    select = re.search(r'<select id="ng-rifle".*?</select>', html, re.S)
    assert select, "the dialog has no rifle select"
    return [
        (m[1], "disabled" in m[2], m[3].strip())
        for m in re.finditer(r'<option value="(\d+)"([^>]*)>([^<]*)</option>', select[0])
    ]


def page_state(html: str) -> dict:
    raw = re.search(r'id="new-group-card"[^>]*data-state="([^"]*)"', html)[1]
    return json.loads(raw.replace("&#34;", '"').replace("&quot;", '"').replace("&amp;", "&"))


class TestStartPage:
    def test_the_home_address_opens_the_groups_tab(self, client):
        response = client.get("/", follow_redirects=False)

        assert response.status_code == 303 and response.headers["location"] == "/groups"
        html = client.get("/groups").text
        assert '<a class="brand" href="/groups">' in html
        assert "data-new-group-open" in html.split("<main", 1)[1]

    def test_the_dialog_lists_every_rifle_and_only_rifles_with_a_load_can_be_picked(
        self, client, conn
    ):
        ready = seed_rifle(conn, name="Tikka", cartridge=".223 Rem")
        seed_load(conn, ready)
        bare = seed_rifle(conn, name="Bare", cartridge="6.5 Creedmoor")

        options = dialog_options(client.get("/groups").text)

        by_id = {int(value): (disabled, label) for value, disabled, label in options}
        assert by_id[ready.id] == (False, "Tikka (.223 Rem)")
        assert by_id[bare.id][0] is True and "no loads yet" in by_id[bare.id][1]
        html = client.get("/groups").text
        assert 'id="ng-photo" name="photo" type="file" accept="image/*"' in html
        assert "needs a load before it can take a group" in html

    def test_with_no_usable_rifle_the_dialog_points_to_the_rifles_page(self, client, conn):
        seed_rifle(conn, name="Bare")

        html = client.get("/groups").text

        assert 'id="ng-no-rifle"' in html and 'href="/rifles"' in html
        assert 'id="ng-rifle"' not in html and 'id="ng-continue"' not in html

    def test_rifle_names_are_escaped_in_the_dialog(self, client, conn):
        rifle = seed_rifle(conn, name="<script>alert(1)</script>")
        seed_load(conn, rifle)

        html = client.get("/groups").text

        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


class TestGroupsPage:
    def test_the_button_sits_at_the_top_right_beside_the_heading(self, client, conn):
        rifle = seed_rifle(conn)
        seed_load(conn, rifle)

        html = client.get("/groups").text

        head = re.search(r'<div class="page-head">(.*?)</div>', html, re.S)[1]
        assert head.index("<h1>Groups</h1>") < head.index("data-new-group-open")
        assert 'id="new-group-dialog"' in html
        assert dialog_options(html)[0][0] == str(rifle.id)
        assert '<script type="module" src="/static/js/new_group.js"></script>' in html


class TestNewGroupPage:
    def test_the_page_is_the_rifles_group_form_with_the_photo_attached(self, client, conn):
        rifle = seed_rifle(conn, name="Tikka", cartridge=".223 Rem")
        seed_load(conn, rifle)
        photo = client.post("/api/photos", files={"file": ("t.jpg", jpeg(), "image/jpeg")}).json()

        response = client.get("/groups/new", params={"rifle_id": rifle.id, "photo_id": photo["id"]})

        assert response.status_code == 200
        html = response.text
        state = page_state(html)
        assert state["pending_photo"] == {
            "id": photo["id"],
            "taken_at": photo["taken_at"],
            "taken_at_offset": photo["taken_at_offset"],
            "reference_confirmed": False,
        }
        assert state["rifle_id"] == rifle.id and state["return_to"] == "/groups"
        assert "<h1>New group</h1>" in html and "Tikka &middot; .223 Rem" in html
        assert '<li class="current">1. Group details</li>' in html
        assert "Target photo (attached)" in html
        # The page shows the photo large, so it uses the full-size oriented image, not the thumb.
        assert f'src="/api/photos/{photo["id"]}/image?variant=oriented"' in html
        assert 'id="new-group-reused"' not in html
        assert '<script type="module" src="/static/js/group_form.js"></script>' in html
        # The page is only the form: none of the rifle page's other sections.
        assert 'data-form="edit-rifle"' not in html and 'data-section="zeroing"' not in html

    def test_a_photo_already_in_the_app_is_not_suggested_its_old_scale(self, client, conn):
        rifle = seed_rifle(conn)
        seed_load(conn, rifle)
        photo = client.post("/api/photos", files={"file": ("t.jpg", jpeg(), "image/jpeg")}).json()
        with conn:
            conn.execute(
                "UPDATE photos SET reference_confirmed = 1, reference_kind = 'circle_1in', "
                "h_px_to_plane_json = '[[0.01,0,0],[0,0.01,0],[0,0,1]]', working_ppi = 100 "
                "WHERE id = ?",
                (photo["id"],),
            )

        html = client.get(
            "/groups/new", params={"rifle_id": rifle.id, "photo_id": photo["id"]}
        ).text

        assert page_state(html)["pending_photo"]["reference_confirmed"] is True
        assert 'id="new-group-reused"' not in html, "the old scale is never suggested"

    @pytest.mark.parametrize(
        ("query", "location"),
        [
            ("", "/groups"),
            ("?rifle_id=999", "/groups"),
            ("?rifle_id=0", "/groups"),
            (f"?rifle_id={2**70}", "/groups"),
        ],
    )
    def test_without_a_known_rifle_it_goes_back_to_the_groups_tab(self, client, query, location):
        response = client.get(f"/groups/new{query}", follow_redirects=False)

        assert response.status_code == 303 and response.headers["location"] == location

    def test_a_rifle_without_a_load_goes_to_its_own_page(self, client, conn):
        rifle = seed_rifle(conn)

        response = client.get(f"/groups/new?rifle_id={rifle.id}", follow_redirects=False)

        assert response.status_code == 303 and response.headers["location"] == f"/rifles/{rifle.id}"

    @pytest.mark.parametrize("photo_id", ["999", "0", "-3", str(2**70)])
    def test_an_unknown_or_out_of_range_photo_is_ignored(self, client, conn, photo_id):
        rifle = seed_rifle(conn)
        seed_load(conn, rifle)

        response = client.get("/groups/new", params={"rifle_id": rifle.id, "photo_id": photo_id})

        assert response.status_code == 200
        assert page_state(response.text)["pending_photo"] is None
        assert "Target photo (optional)" in response.text

    def test_the_new_page_is_not_taken_by_the_group_page_route(self, client, conn):
        rifle = seed_rifle(conn)
        seed_load(conn, rifle)

        assert client.get(f"/groups/new?rifle_id={rifle.id}").status_code == 200
        assert client.get("/groups/new", follow_redirects=False).status_code == 303

    def test_the_rifle_page_keeps_its_own_form_and_never_takes_a_photo_id(self, client, conn):
        rifle = seed_rifle(conn)
        seed_load(conn, rifle)

        html = client.get(f"/rifles/{rifle.id}", params={"photo_id": 1}).text

        state = page_state(html)
        assert state["pending_photo"] is None and state["return_to"] == f"/rifles/{rifle.id}"
        assert "Target photo (optional)" in html

    def test_the_form_always_continues_to_the_reference_step(self):
        source = (WEB / "static/js/group_form.js").read_text()

        assert "const pending = state.pending_photo || null;" in source
        assert (
            "return `/photos/${photoId}/reference?group_id=${groupId}&return_to=${returnTo}`;"
            in source
        )
        assert "/review?return_to=" not in source
        dialog = (WEB / "static/js/new_group.js").read_text()
        assert "`/groups/new?rifle_id=${Number(rifle.value)}&photo_id=${uploaded.id}`" in dialog


class TestReferenceStepForAConfirmedPhoto:
    def _confirmed_photo(self, client, conn, *, locked: bool):
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        photo = client.post("/api/photos", files={"file": ("t.jpg", jpeg(), "image/jpeg")}).json()
        with conn:
            conn.execute(
                "UPDATE photos SET reference_confirmed = 1, reference_kind = 'circle_1_5in', "
                "h_px_to_plane_json = '[[0.01,0,0],[0,0.01,0],[0,0,1]]', working_ppi = 100, "
                "working_w = 640, working_h = 480 WHERE id = ?",
                (photo["id"],),
            )
        from tests.support.seed import seed_context, seed_group

        draft = seed_group(
            conn,
            load=load,
            offsets=[],
            context=seed_context(rifle),
            photo=photo["id"],
            finalize=False,
            aim=None,
        )
        if locked:
            from rga.domain.types import Offset

            seed_group(
                conn,
                load=load,
                offsets=[Offset(0.1, 0.0), Offset(-0.1, 0.1), Offset(0.0, -0.1)],
                context=seed_context(rifle),
                photo=photo["id"],
            )
        return photo, draft

    def test_the_old_scale_is_never_offered_and_the_step_works_as_new(self, client, conn):
        photo, draft = self._confirmed_photo(client, conn, locked=False)

        html = client.get(f"/photos/{photo['id']}/reference", params={"group_id": draft.id}).text

        assert "already confirmed" not in html and "keep-reference" not in html
        assert '<div id="reference-controls" class="stack">' in html
        assert '<button type="button" id="reference-next" disabled>Next</button>' in html

    def test_a_locked_scale_hides_the_controls_that_would_be_refused(self, client, conn):
        photo, draft = self._confirmed_photo(client, conn, locked=True)

        html = client.get(f"/photos/{photo['id']}/reference", params={"group_id": draft.id}).text

        assert "keep-reference" not in html
        assert '<div id="reference-controls" class="stack" hidden>' in html
        assert "fixed by another group on it" in html
        assert '<button type="button" id="reference-next">Next</button>' in html


class TestDeleteGroups:
    def _groups(self, conn, n: int):
        from tests.support.seed import seed_context, seed_group

        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        return [
            seed_group(
                conn, load=load, offsets=[], context=seed_context(rifle), finalize=False, aim=None
            )
            for _ in range(n)
        ]

    def test_the_chosen_groups_and_their_rows_go_and_the_others_stay(self, client, conn):
        a, b, c = self._groups(conn, 3)
        with conn:
            conn.execute(
                "INSERT INTO shots (group_id, seq, x_in, y_in, origin) VALUES (?, 1, 1, 1, 'manual')",
                (a.id,),
            )

        response = client.post("/api/groups/delete", json={"group_ids": [a.id, b.id, a.id]})

        assert response.status_code == 200 and response.json() == {"deleted": 2}
        left = [r[0] for r in conn.execute("SELECT id FROM groups ORDER BY id")]
        assert left == [c.id]
        assert conn.execute("SELECT COUNT(*) FROM shots").fetchone()[0] == 0

    def test_an_unknown_id_deletes_nothing(self, client, conn):
        a, b = self._groups(conn, 2)

        response = client.post("/api/groups/delete", json={"group_ids": [a.id, 999]})

        assert response.status_code == 404 and response.json()["code"] == "group_not_found"
        assert conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 2

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"group_ids": []},
            {"group_ids": [0]},
            {"group_ids": ["x"]},
            {"group_ids": [2**63]},
            {"group_ids": list(range(1, 502))},
            {"group_ids": [1], "x": 1},
        ],
    )
    def test_a_bad_body_is_a_validation_error(self, client, conn, body):
        self._groups(conn, 1)

        response = client.post("/api/groups/delete", json=body)

        assert response.status_code == 422
        assert conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 1

    def test_a_foreign_origin_cannot_delete(self, client, conn):
        (a,) = self._groups(conn, 1)

        response = client.post(
            "/api/groups/delete",
            json={"group_ids": [a.id]},
            headers={"Origin": "http://evil.example"},
        )

        assert response.status_code == 403
        assert conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 1

    def test_a_saved_combination_that_included_a_deleted_group_is_removed(self, client, conn):
        a, b, c = self._groups(conn, 3)
        with conn:
            for name, members in (("ab", (a, b)), ("bc", (b, c)), ("ac", (a, c))):
                cur = conn.execute(
                    "INSERT INTO aggregates (public_id, name, rifle_id, load_id, distance_yd, "
                    "group_count, created_at) SELECT ?, ?, l.rifle_id, l.id, 100, 2, "
                    "'2026-10-08T00:00:00' FROM loads l WHERE l.id = ?",
                    (name, name, a.load_id),
                )
                for g in members:
                    conn.execute(
                        "INSERT INTO aggregate_groups (aggregate_id, group_id) VALUES (?, ?)",
                        (cur.lastrowid, g.id),
                    )

        assert client.post("/api/groups/delete", json={"group_ids": [b.id]}).status_code == 200

        assert [r[0] for r in conn.execute("SELECT name FROM aggregates ORDER BY name")] == ["ac"]
        assert conn.execute("SELECT COUNT(*) FROM aggregate_groups").fetchone()[0] == 2

    def test_a_photo_no_group_uses_is_deleted_with_its_files_and_can_be_added_afresh(
        self, client, conn, settings
    ):
        from rga.services import photos as photo_service
        from tests.support.seed import seed_context, seed_group

        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        data = jpeg(7)
        photo = client.post("/api/photos", files={"file": ("t.jpg", data, "image/jpeg")}).json()
        assert client.get(f"/api/photos/{photo['id']}/image?variant=thumb").status_code == 200
        only = seed_group(
            conn,
            load=load,
            offsets=[],
            context=seed_context(rifle),
            photo=photo["id"],
            finalize=False,
            aim=None,
        )
        row = photo_service.get_photo(conn, photo["id"])
        files = [
            photo_service.original_path(settings, row),
            photo_service.oriented_path(settings, row.id),
            photo_service.thumb_path(settings, row.id),
        ]
        assert all(path.is_file() for path in files)

        assert client.post("/api/groups/delete", json={"group_ids": [only.id]}).status_code == 200

        assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0
        assert not any(path.exists() for path in files)
        again = client.post("/api/photos", files={"file": ("t.jpg", data, "image/jpeg")})
        assert again.status_code == 201 and again.json()["duplicate"] is False
        assert again.json()["reference_confirmed"] is False

    def test_a_photo_another_group_still_uses_stays(self, client, conn, settings):
        from rga.services import photos as photo_service
        from tests.support.seed import seed_context, seed_group

        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        photo = client.post("/api/photos", files={"file": ("t.jpg", jpeg(8), "image/jpeg")}).json()
        first, second = (
            seed_group(
                conn,
                load=load,
                offsets=[],
                context=seed_context(rifle),
                photo=photo["id"],
                finalize=False,
                aim=None,
            )
            for _ in range(2)
        )

        assert client.post("/api/groups/delete", json={"group_ids": [first.id]}).status_code == 200

        row = photo_service.get_photo(conn, photo["id"])
        assert photo_service.original_path(settings, row).is_file()
        assert [r[0] for r in conn.execute("SELECT id FROM groups")] == [second.id]

    def test_the_groups_page_has_edit_checkboxes_and_a_cancel_first_confirmation(
        self, client, conn
    ):
        (a,) = self._groups(conn, 1)

        html = client.get("/groups").text

        assert (
            '<button type="button" class="secondary" id="groups-edit" aria-pressed="false" aria-controls="groups-edit-bar">Edit</button>'
            in html
        )
        assert f'<input type="checkbox" class="group-select" value="{a.id}"' in html
        dialog = re.search(r'<dialog id="groups-delete-dialog".*?</dialog>', html, re.S)[0]
        assert dialog.index('id="groups-delete-cancel"') < dialog.index(
            'id="groups-delete-confirm"'
        )
        assert 'class="confirm-cancel" autofocus>Cancel</button>' in dialog
        assert '<script type="module" src="/static/js/groups_edit.js"></script>' in html

    def test_an_empty_list_has_no_edit_button(self, client):
        assert 'id="groups-edit"' not in client.get("/groups").text


class TestAggregateZeroWarning:
    def test_the_picker_and_the_result_both_warn_about_the_same_zero(self, client):
        html = client.get("/analysis").text

        notice = re.search(
            r'<div class="notice warn" id="aggregate-zero-notice"[^>]*>(.*?)</div>', html, re.S
        )
        result = re.search(
            r'<div class="notice warn" id="aggregate-zero-warning"[^>]*>(.*?)</div>', html, re.S
        )
        assert notice and "not accurate" in notice[1] and "same zero" in notice[1]
        assert result and "same zero" in result[1] and 'role="alert"' in result[0]
        assert html.index('id="aggregate-zero-warning"') > html.index('id="aggregate-result"')


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
@pytest.mark.parametrize("start", ["/", "/groups"])
def test_the_dialog_uploads_the_photo_and_the_rifle_form_carries_on_to_the_reference(
    start, tmp_path
):
    sync_api = pytest.importorskip("playwright.sync_api")
    picture = tmp_path / "target.jpg"
    picture.write_bytes(jpeg(1))
    with run_live_server() as server, sync_api.sync_playwright() as playwright:
        settings = Settings(data_dir=server.data_dir, port=server.port)
        connection = connect(settings.db_path)
        try:
            rifle = seed_rifle(connection, name="Tikka", cartridge=".223 Rem")
            seed_load(connection, rifle)
        finally:
            connection.close()
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1200, "height": 900})
            problems: list[str] = []
            page.on("pageerror", lambda error: problems.append(str(error)))
            page.goto(f"{server}{start}")

            page.click("[data-new-group-open]")
            dialog = page.locator("#new-group-dialog")
            assert dialog.is_visible()
            page.click("#ng-continue")
            assert "Choose the target photo." in page.inner_text("#ng-error")
            page.set_input_files("#ng-photo", str(picture))
            page.click("#ng-continue")

            page.wait_for_url(re.compile(rf"/groups/new\?rifle_id={rifle.id}&photo_id=\d+$"))
            assert page.inner_text("h1") == "New group"
            assert page.inner_text('label[for="g-photo"]') == "Target photo (attached)"
            assert "attached" in page.inner_text("#g-photo-note")
            page.fill("#g-lot", "LOT-1")
            page.click("#add-group")

            page.wait_for_url(
                re.compile(r"/photos/\d+/reference\?group_id=\d+&return_to=%2Fgroups$")
            )
            assert problems == []
        finally:
            browser.close()


class TestReferenceDefaultKind:
    def _confirmed_group(self, client, conn, rifle, load, kind: str, seed: int):
        from tests.support.seed import seed_context, seed_group

        photo = client.post(
            "/api/photos", files={"file": ("t.jpg", jpeg(seed), "image/jpeg")}
        ).json()
        with conn:
            conn.execute(
                "UPDATE photos SET reference_confirmed = 1, reference_kind = ?, "
                "h_px_to_plane_json = '[[0.01,0,0],[0,0.01,0],[0,0,1]]', working_ppi = 100 "
                "WHERE id = ?",
                (kind, photo["id"]),
            )
        return seed_group(
            conn,
            load=load,
            offsets=[],
            context=seed_context(rifle),
            photo=photo["id"],
            finalize=False,
            aim=None,
        )

    def _selected(self, html: str) -> list[str]:
        return re.findall(r'<option value="([a-z0-9_]+)" selected>', html)

    def test_the_kind_past_groups_used_most_is_preselected(self, client, conn):
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        for seed, kind in enumerate(("circle_2in", "circle_2in", "circle_1in")):
            self._confirmed_group(client, conn, rifle, load, kind, 20 + seed)
        fresh = client.post("/api/photos", files={"file": ("t.jpg", jpeg(30), "image/jpeg")}).json()

        html = client.get(f"/photos/{fresh['id']}/reference").text

        assert self._selected(html) == ["circle_2in"]

    def test_with_no_history_nothing_is_forced_and_a_confirmed_photo_keeps_its_own_kind(
        self, client, conn
    ):
        fresh = client.post("/api/photos", files={"file": ("t.jpg", jpeg(31), "image/jpeg")}).json()
        assert self._selected(client.get(f"/photos/{fresh['id']}/reference").text) == []

        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        for seed in range(3):
            self._confirmed_group(client, conn, rifle, load, "grid_1in", 40 + seed)
        own = self._confirmed_group(client, conn, rifle, load, "circle_3in", 50)
        html = client.get(f"/photos/{own.photo_id}/reference").text
        assert self._selected(html) == ["circle_3in"]


class TestLeaveConfirmation:
    def test_the_creation_pages_carry_it_and_a_final_group_does_not(self, client, conn):
        from rga.domain.types import Offset
        from tests.support.seed import seed_context, seed_group

        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        photo = client.post("/api/photos", files={"file": ("t.jpg", jpeg(60), "image/jpeg")}).json()
        draft = seed_group(
            conn,
            load=load,
            offsets=[],
            context=seed_context(rifle),
            photo=photo["id"],
            finalize=False,
            aim=None,
        )
        final = seed_group(
            conn,
            load=load,
            offsets=[Offset(0.1, 0.0), Offset(-0.1, 0.1)],
            context=seed_context(rifle),
        )

        new_page = client.get(f"/groups/new?rifle_id={rifle.id}&photo_id={photo['id']}").text
        reference = client.get(f"/photos/{photo['id']}/reference?group_id={draft.id}").text
        review = client.get(f"/groups/{draft.id}/review?return_to=/groups").text
        finished = client.get(f"/groups/{final.id}/review?return_to=/groups").text
        bare_reference = client.get(f"/photos/{photo['id']}/reference").text

        for html in (new_page, reference, review):
            dialog = re.search(r'<dialog id="leave-dialog".*?</dialog>', html, re.S)[0]
            assert (
                "progress you haven&#39;t saved" in dialog.lower()
                or "progress you haven't saved" in dialog.lower()
            )
            assert dialog.index('id="leave-stay"') < dialog.index('id="leave-go"')
            assert 'class="confirm-cancel" autofocus>Keep working</button>' in dialog
            assert '<script type="module" src="/static/js/leave_guard.js"></script>' in html
        assert 'id="new-group-cancel" data-leave>Cancel</a>' in new_page
        assert 'id="back-link" href="/groups" data-leave>Exit</a>' in review
        assert 'id="leave-dialog"' not in finished and 'id="leave-dialog"' not in bare_reference

    def test_exit_no_longer_saves_on_its_way_out(self):
        source = (WEB / "static/js/review.js").read_text()

        assert "saveAndExit" not in source and "back-link" not in source


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
def test_leaving_a_group_in_progress_asks_first_and_keep_working_stays():
    sync_api = pytest.importorskip("playwright.sync_api")
    with run_live_server() as server, sync_api.sync_playwright() as playwright:
        settings = Settings(data_dir=server.data_dir, port=server.port)
        connection = connect(settings.db_path)
        try:
            rifle = seed_rifle(connection)
            seed_load(connection, rifle)
        finally:
            connection.close()
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1200, "height": 900})
            problems: list[str] = []
            page.on("pageerror", lambda error: problems.append(str(error)))
            page.goto(f"{server}/groups/new?rifle_id={rifle.id}")
            dialog = page.locator("#leave-dialog")

            page.click('.site-nav a[href="/rifles"]')
            assert dialog.is_visible()
            assert page.evaluate("document.activeElement.id") == "leave-stay"
            page.click("#leave-stay")
            assert dialog.is_hidden() and page.url.endswith(f"/groups/new?rifle_id={rifle.id}")

            page.click("#new-group-cancel")
            assert dialog.is_visible()
            page.click("#leave-go")
            page.wait_for_url(re.compile(r"/groups$"))
            assert problems == []
        finally:
            browser.close()


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
def test_the_reference_step_toggles_between_the_two_ways_and_one_next_confirms():
    sync_api = pytest.importorskip("playwright.sync_api")
    from tests.synth.generate import render_named

    img, _truth = render_named("clean_circle1", 0)
    ok, buffer = cv2.imencode(".jpg", img)
    assert ok
    with run_live_server() as server, sync_api.sync_playwright() as playwright:
        settings = Settings(data_dir=server.data_dir, port=server.port)
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1300, "height": 950})
            problems: list[str] = []
            page.on("pageerror", lambda error: problems.append(str(error)))
            uploaded = page.context.request.post(
                f"{server}/api/photos",
                multipart={
                    "file": {"name": "t.jpg", "mimeType": "image/jpeg", "buffer": buffer.tobytes()}
                },
                headers={"Origin": str(server)},
            )
            photo = uploaded.json()
            connection = connect(settings.db_path)
            try:
                from tests.support.seed import seed_context, seed_group

                rifle = seed_rifle(connection)
                load = seed_load(connection, rifle)
                group = seed_group(
                    connection,
                    load=load,
                    offsets=[],
                    context=seed_context(rifle),
                    photo=photo["id"],
                    finalize=False,
                    aim=None,
                )
            finally:
                connection.close()
            page.goto(f"{server}/photos/{photo['id']}/reference?group_id={group.id}")
            page.select_option("#kind", "circle_1in")
            assert page.is_disabled("#reference-next")

            page.click("#find")
            page.locator("#find:enabled").wait_for()
            assert page.get_attribute("#find", "aria-pressed") == "true"
            assert page.is_visible("#candidates-panel") and page.is_hidden("#manual-panel")
            single = page.locator("#single-candidate")
            if single.count():
                assert single.inner_text().strip() == "Circle outlined on the photo."
                assert page.locator('#candidates input[name="candidate"]').count() == 0
                assert page.is_enabled("#reference-next"), "one outline is already the choice"
            else:
                # Several outlines: the biggest circle starts selected, so Next is ready.
                checked = page.locator('#candidates input[name="candidate"]:checked')
                assert checked.count() == 1
                label = checked.locator("xpath=..").inner_text()
                assert "the biggest circle" in label or "the circle outlined" in label
                assert page.is_enabled("#reference-next")
                assert "Option 1" in page.inner_text("#candidates")
                assert "axis ratio" not in page.inner_text("#candidates")

            page.click("#manual-toggle")
            assert page.get_attribute("#manual-toggle", "aria-pressed") == "true"
            assert page.get_attribute("#find", "aria-pressed") == "false"
            assert page.is_hidden("#candidates-panel") and page.is_visible("#manual-panel")
            assert page.is_disabled("#reference-next")
            assert page.locator("#overlay ellipse").count() == 0

            page.click("#find")
            assert page.is_visible("#candidates-panel") and page.is_hidden("#manual-panel")
            if not single.count():
                page.locator('#candidates input[name="candidate"]').first.check()
            page.click("#reference-next")
            page.wait_for_url(re.compile(rf"/groups/{group.id}/review$"))
            assert page.is_visible("#step-aim")
            assert problems == []
        finally:
            browser.close()


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
def test_with_several_outlines_the_biggest_circle_starts_selected():
    """The propose reply is fixed (three circles, the biggest second) so the default is tested
    whatever the detector finds."""
    sync_api = pytest.importorskip("playwright.sync_api")
    from tests.synth.generate import render_named

    img, _truth = render_named("clean_circle1", 0)
    ok, buffer = cv2.imencode(".jpg", img)
    assert ok

    def circle(index: int, size: float) -> dict:
        return {
            "index": index,
            "method": "auto",
            "axis_ratio": 0.99,
            "warnings": [],
            "circle": {
                "center_x_px": 300.0,
                "center_y_px": 300.0,
                "axis_major_px": size,
                "axis_minor_px": size,
                "angle_deg": 0.0,
            },
            "grid_points_px": None,
        }

    with run_live_server() as server, sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1300, "height": 950})
            problems: list[str] = []
            page.on("pageerror", lambda error: problems.append(str(error)))
            uploaded = page.context.request.post(
                f"{server}/api/photos",
                multipart={
                    "file": {"name": "t.jpg", "mimeType": "image/jpeg", "buffer": buffer.tobytes()}
                },
                headers={"Origin": str(server)},
            )
            photo = uploaded.json()
            reply = {
                "kind": "circle_1in",
                "candidates": [circle(0, 80), circle(1, 200), circle(2, 40)],
            }
            page.route(
                "**/reference/propose",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(reply)
                ),
            )
            page.goto(f"{server}/photos/{photo['id']}/reference")
            page.select_option("#kind", "circle_1in")
            page.click("#find")
            page.locator('#candidates input[name="candidate"]').nth(2).wait_for()

            radios = page.locator('#candidates input[name="candidate"]')
            assert [radios.nth(i).is_checked() for i in range(3)] == [False, True, False]
            assert "Option 2: the biggest circle" in page.inner_text("#candidates")
            assert page.is_enabled("#reference-next")

            radios.nth(2).check()
            assert radios.nth(2).is_checked() and not radios.nth(1).is_checked()
            assert problems == []

            # Nothing found: no default to pick, and the page switches to marking by hand.
            reply["candidates"] = []
            page.select_option("#kind", "circle_1_5in")  # a new kind searches again
            page.locator("#manual-panel").wait_for()
            assert page.get_attribute("#manual-toggle", "aria-pressed") == "true"
            assert page.is_disabled("#reference-next")
            assert problems == []
        finally:
            browser.close()


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
@pytest.mark.parametrize("size", [(1280, 800), (1440, 900), (1920, 1080)])
def test_on_a_desktop_screen_the_new_group_page_fits_without_scrolling(size):
    sync_api = pytest.importorskip("playwright.sync_api")
    with run_live_server() as server, sync_api.sync_playwright() as playwright:
        settings = Settings(data_dir=server.data_dir, port=server.port)
        connection = connect(settings.db_path)
        try:
            rifle = seed_rifle(connection, name="Tikka", cartridge=".223 Rem")
            seed_load(connection, rifle)
        finally:
            connection.close()
        browser = playwright.chromium.launch()
        try:
            width, height = size
            page = browser.new_page(viewport={"width": width, "height": height})
            problems: list[str] = []
            page.on("pageerror", lambda error: problems.append(str(error)))
            uploaded = page.context.request.post(
                f"{server}/api/photos",
                multipart={"file": {"name": "t.jpg", "mimeType": "image/jpeg", "buffer": jpeg(3)}},
                headers={"Origin": str(server)},
            )
            photo = uploaded.json()
            page.goto(f"{server}/groups/new?rifle_id={rifle.id}&photo_id={photo['id']}")
            page.locator("#new-group-photo").wait_for()

            def scroll_height() -> int:
                return page.evaluate("() => document.documentElement.scrollHeight")

            assert scroll_height() <= height, "the whole form fits in the window"
            add = page.locator("#add-group").bounding_box()
            assert add["y"] + add["height"] <= height
            # The fields run four to a row and the photo sits to the right of the form.
            first_row = [
                page.locator(f"#{i}").bounding_box()["y"]
                for i in ("g-date", "g-time", "g-distance", "g-position")
            ]
            assert max(first_row) - min(first_row) < 2
            form = page.locator("#new-group-card").bounding_box()
            picture = page.locator("#new-group-photo").bounding_box()
            assert picture["x"] >= form["x"] + form["width"]
            assert picture["height"] > height * 0.4
            if height >= 900:
                page.click("#weather-panel > summary")
                page.check("#g-suppressor")
                assert scroll_height() <= height, "still fits with weather and suppressor open"
                page.uncheck("#g-suppressor")

            # Step 2 with the real photo: it is sized to the window, the controls beside it.
            page.fill("#g-lot", "LOT-1")
            page.click("#add-group")
            page.wait_for_url(re.compile(r"/photos/\d+/reference\?"))
            page.wait_for_function("() => document.getElementById('photo').naturalWidth > 0")
            assert scroll_height() <= height, "the reference step fits"
            shown = page.locator("#viewer").bounding_box()
            assert shown["y"] + shown["height"] <= height and shown["height"] > height * 0.5
            panel = page.locator("aside.flow-panel").bounding_box()
            assert panel["x"] >= shown["x"] + shown["width"]
            page.click("#manual-toggle")
            assert scroll_height() <= height, "the reference step fits while marking by hand"
            assert problems == []
        finally:
            browser.close()


class TestGoingBackToTheReference:
    def _setup(self, client, conn):
        from rga.domain.types import PlanePoint
        from tests.support.seed import seed_context, seed_group

        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        photo = client.post("/api/photos", files={"file": ("t.jpg", jpeg(70), "image/jpeg")}).json()
        with conn:
            conn.execute(
                "UPDATE photos SET reference_confirmed = 1, reference_kind = 'circle_1_5in', "
                "h_px_to_plane_json = '[[0.01,0,0],[0,0.01,0],[0,0,1]]', working_ppi = 100, "
                "working_w = 640, working_h = 480 WHERE id = ?",
                (photo["id"],),
            )
        mine = seed_group(
            conn,
            load=load,
            offsets=[],
            context=seed_context(rifle),
            photo=photo["id"],
            finalize=False,
            aim=PlanePoint(1.0, 1.0),
        )
        return rifle, load, photo, mine

    def test_a_groups_own_aim_does_not_lock_its_reference_step(self, client, conn):
        _, _, photo, mine = self._setup(client, conn)

        html = client.get(f"/photos/{photo['id']}/reference", params={"group_id": mine.id}).text

        assert 'data-locked="0"' in html and 'data-own-work="1"' in html
        assert 'id="locked-scale"' not in html
        assert (
            "clears this group&#39;s aim point and shots" in html
            or "clears this group's aim point and shots" in html
        )
        assert '<div id="reference-controls" class="stack">' in html

    def test_another_groups_work_still_locks_it(self, client, conn):
        from rga.domain.types import PlanePoint
        from tests.support.seed import seed_context, seed_group

        rifle, load, photo, mine = self._setup(client, conn)
        seed_group(
            conn,
            load=load,
            offsets=[],
            context=seed_context(rifle),
            photo=photo["id"],
            finalize=False,
            aim=PlanePoint(2.0, 2.0),
        )

        html = client.get(f"/photos/{photo['id']}/reference", params={"group_id": mine.id}).text

        assert 'data-locked="1"' in html and 'id="locked-scale"' in html
        assert 'data-own-work="0"' in html

    def test_next_clears_the_groups_own_work_before_confirming(self):
        source = (WEB / "static/js/reference.js").read_text()

        reset = 'if (ownWork) await apiSend("POST", `/api/photos/${photoId}/reference/reset`);'
        assert reset in source
        assert source.index(reset) < source.index(
            'if (state.mode === "auto") await confirmCandidate();'
        )
