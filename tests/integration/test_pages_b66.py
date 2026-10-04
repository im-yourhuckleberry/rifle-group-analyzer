"""B-66 R7: the pages without sessions: rifle page groups by day and the Add a group form, the
dashboard's recent groups, the zeroing group form, the group page header and Context card, and the
review page's "Draw a region first". HTML checks run in-process; the forms run in Chromium.
"""

from __future__ import annotations

import html as htmllib
import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rga.config import Settings
from rga.db import repo_groups
from rga.db.connection import connect
from rga.web.app import create_app
from tests.integration.test_group_plot import (
    _playwright_ready,
    main_of,
    photo_group,
    ring,
)
from tests.support.seed import seed_context, seed_group, seed_load, seed_rifle
from tests.unit.test_image_io import _jpeg_with
from tests.unit.test_image_io_taken_at import build_tiff, with_prefix

ORIGIN = "http://127.0.0.1:8765"
WEB = Path(__file__).resolve().parents[2] / "src" / "rga" / "web"
FORM_ORDER = [
    "g-photo",
    "g-date",
    "g-time",
    "g-distance",
    "g-position",
    "g-suppressor",
    "w-temp",
    "g-load",
    "g-lot",
    "g-shots",
    "g-elevation",
    "g-notes",
    "add-group",
]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url=ORIGIN) as c:
        yield c


@pytest.fixture
def conn(settings: Settings, client: TestClient) -> Iterator[sqlite3.Connection]:
    connection = connect(settings.db_path)
    try:
        yield connection
    finally:
        connection.close()


def shoot(conn, load, rifle, *, date, n=5, distance=100, time=None, **kwargs):
    context = seed_context(rifle, distance_yd=distance, date=date, time=time)
    return seed_group(conn, load=load, context=context, offsets=ring(0.1, 0.1, n=n), **kwargs)


def state_of(html: str) -> dict:
    raw = re.search(r'id="new-group-card"[^>]*data-state="([^"]*)"', html)[1]
    return json.loads(htmllib.unescape(raw))


# ------------------------------------------------------------------------------- rifle page


class TestRiflePage:
    def test_groups_are_listed_by_day_newest_first_and_by_time_then_id_within_a_day(
        self, client, conn
    ):
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        older = shoot(conn, load, rifle, date="2026-09-01", distance=100)
        late = shoot(conn, load, rifle, date="2026-09-30", time="15:00:00", distance=300)
        early = shoot(conn, load, rifle, date="2026-09-30", time="08:30:00", distance=100)
        untimed = shoot(conn, load, rifle, date="2026-09-30", distance=100)

        html = client.get(f"/rifles/{rifle.id}").text

        days = re.findall(r'<div class="day" data-day="([\d-]+)">', html)
        assert days == ["2026-09-30", "2026-09-01"]
        first_day = html.split('data-day="2026-09-30"', 1)[1].split('data-day="2026-09-01"', 1)[0]
        order = [int(i) for i in re.findall(r'<tr data-group-id="(\d+)"', first_day)]
        assert order == [untimed.id, early.id, late.id]
        assert "100, 300 yd" in first_day and "100 yd" in html.split('data-day="2026-09-01"')[1]
        assert f'href="/groups/{older.id}"' in html
        assert "08:30" in first_day and "15:00" in first_day

    def test_no_session_remains_on_the_page(self, client, conn):
        rifle = seed_rifle(conn)
        seed_load(conn, rifle)

        html = client.get(f"/rifles/{rifle.id}").text

        assert not re.search(r"(?i)\bsessions?\b|new-session|/sessions/", html)

    def test_the_add_group_form_has_its_fields_in_the_agreed_order(self, client, conn):
        rifle = seed_rifle(conn)
        seed_load(conn, rifle)

        html = client.get(f"/rifles/{rifle.id}").text

        positions = [html.index(f'id="{name}"') for name in FORM_ORDER]
        assert positions == sorted(positions), FORM_ORDER
        assert "data-suppressor-picker" in html and 'id="g-suppressor-name"' in html
        assert "from photo" not in html.split('id="g-date-source"')[0]

    def test_the_form_is_prefilled_from_the_rifles_latest_group(self, client, conn):
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        other = seed_load(conn, rifle, label="Second load")
        shoot(
            conn,
            other,
            rifle,
            date="2026-09-29",
            n=7,
            distance=300,
            lot_code="LOT-77",
            shots_expected=7,
        )

        html = client.get(f"/rifles/{rifle.id}").text

        assert re.search(r'id="g-distance"[^>]*value="300"', html)
        assert re.search(r'id="g-lot"[^>]*value="LOT-77"', html)
        assert re.search(r'id="g-shots"[^>]*value="7"', html)
        assert re.search(rf'<option value="{other.id}" selected>', html)
        assert not re.search(rf'<option value="{load.id}" selected>', html)
        state = state_of(html)
        assert state["rifle_id"] == rifle.id and state["weather_prompt_yd"] == 200
        assert state["defaults"]["lot_code"] == "LOT-77"
        assert state["lots"][str(other.id)] == ["LOT-77"]

    def test_a_masked_group_row_has_no_new_lot_badge(self, client, conn):
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        shoot(conn, load, rifle, date="2026-09-01", lot_code="LOT-A")
        shoot(
            conn,
            load,
            rifle,
            date="2026-09-02",
            n=2,
            lot_code="LOT-B",
            purpose="zero_check",
            shots_expected=2,
        )

        html = client.get(f"/rifles/{rifle.id}").text

        rows = re.findall(r'<tr data-group-id="\d+".*?</tr>', html, re.DOTALL)
        assert len(rows) == 2 and sum("new lot" in row for row in rows) <= 1
        masked = next(row for row in rows if "zero check" in row)
        assert "new lot" not in masked

    def test_a_rifle_without_loads_asks_for_one_before_a_group(self, client, conn):
        rifle = seed_rifle(conn)

        html = client.get(f"/rifles/{rifle.id}").text

        assert 'id="add-group"' not in html and "Add a load above first" in html

    def test_the_scripts_are_modules_and_the_group_form_script_is_csp_clean(self, client, conn):
        rifle = seed_rifle(conn)
        seed_load(conn, rifle)
        html = client.get(f"/rifles/{rifle.id}").text
        source = (WEB / "static/js/group_form.js").read_text()

        assert '<script type="module" src="/static/js/group_form.js"></script>' in html
        assert not re.search(r"fetch\(|innerHTML|outerHTML|document\.write|eval\(", source)
        assert not re.search(r"setAttribute\(\s*[\"']style|\.style\b|cssText|https?://", source)
        assert not (WEB / "static/js/session.js").exists()
        assert not (WEB / "templates/session.html").exists()


# ----------------------------------------------------------------------------------- dashboard


class TestDashboard:
    def test_recent_groups_list_the_latest_ten_with_a_link_and_no_statistics(self, client, conn):
        rifle = seed_rifle(conn, name="Dash rifle")
        load = seed_load(conn, rifle)
        ids = [
            shoot(conn, load, rifle, date=f"2026-09-{day:02d}", time="09:00:00").id
            for day in range(1, 13)
        ]

        html = client.get("/").text

        rows = [int(i) for i in re.findall(r'<tr data-group-id="(\d+)"', html)]
        assert rows == list(reversed(ids))[:10]
        assert "Recent groups" in html and "Dash rifle" in html
        assert f'href="/groups/{ids[-1]}"' in html
        assert "09:00" in html and not re.search(r"(?i)\bsessions?\b|MOA|extreme spread", html)

    def test_no_groups_says_so(self, client):
        html = client.get("/").text

        assert "No groups yet" in html and "Recent groups" in html


# ------------------------------------------------------------------------------- group page


class TestGroupPage:
    def _seeded(self, conn, **context):
        rifle = seed_rifle(conn, name="Ctx rifle")
        load = seed_load(conn, rifle, label="Ctx load")
        ctx = seed_context(rifle, **context)
        group = seed_group(conn, load=load, context=ctx, offsets=ring(0.2, 0.1, n=10))
        return rifle, group

    def test_header_and_back_link_describe_the_groups_own_context(self, client, conn):
        rifle, group = self._seeded(
            conn,
            distance_yd=300,
            date="2026-09-30",
            time="08:15:00",
            position="prone_off_pack",
            suppressor=True,
            temperature_f=61.0,
            wind_mph=5.0,
            wind_clock=3,
        )

        html = client.get(f"/groups/{group.id}").text

        assert f'<a href="/rifles/{rifle.id}">&larr; Ctx rifle</a>' in html
        header = re.search(r'<p class="muted" id="group-header">(.*?)</p>', html, re.DOTALL)[1]
        text = " ".join(re.sub(r"<[^>]+>", " ", header).split())
        assert "Ctx rifle" in text and "Ctx load" in text and "2026-09-30 08:15" in text
        assert "300 yd" in text and "Prone off a pack" in text and "suppressed" in text
        assert "61.0" in text and "wind 5.0 mph at 3 o'clock" in text
        assert "/sessions/" not in html

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({"weather_skipped": True}, "weather skipped"),
            ({}, "no weather"),
        ],
    )
    def test_weather_wording(self, client, conn, fields, expected):
        _, group = self._seeded(conn, **fields)

        html = client.get(f"/groups/{group.id}").text

        assert expected in re.search(r'id="group-header">(.*?)</p>', html, re.DOTALL)[1]

    def test_the_context_card_has_every_editable_field_and_a_distance_that_is_not_locked(
        self, client, conn
    ):
        _, group = self._seeded(conn, distance_yd=150)

        html = client.get(f"/groups/{group.id}").text

        card = html.split('id="context-card"', 1)[1].split("</section>", 1)[0]
        for name in (
            "c-date",
            "c-time",
            "c-distance",
            "c-position",
            "c-suppressor",
            "c-temp",
            "c-save",
        ):
            assert f'id="{name}"' in card, name
        assert (
            re.search(r'id="c-distance"[^>]*value="150"', card)
            and "disabled" not in re.search(r'<input id="c-distance"[^>]*>', card)[0]
        )
        assert "c-distance-locked" not in card
        assert "mil" not in card, "the recalculation warning is added by the script, not shipped"

    def test_the_distance_field_is_disabled_with_its_reason_while_the_group_is_in_a_zero(
        self, client, conn
    ):
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        group = seed_group(conn, load=load, offsets=ring(0.1, 0.1, n=10), purpose="general")
        zero = client.post(
            "/api/zeros",
            json={"rifle_id": rifle.id, "load_id": load.id, "mode": "existing"},
            headers={"Origin": ORIGIN},
        ).json()
        linked = client.post(
            f"/api/zeros/{zero['id']}/groups",
            json={"group_id": group.id},
            headers={"Origin": ORIGIN},
        )
        assert linked.status_code == 200, linked.text

        html = client.get(f"/groups/{group.id}").text

        field = re.search(r'<input id="c-distance"[^>]*>', html)[0]
        assert "disabled" in field
        assert "Detach it from the zero before changing the distance" in html

    def test_legacy_session_notes_show_only_when_present(self, client, conn):
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        noted = seed_group(
            conn,
            load=load,
            context=seed_context(rifle, notes="windy morning at the range"),
            offsets=ring(0.1, 0.1, n=5),
        )
        plain = seed_group(conn, load=load, offsets=ring(0.1, 0.1, n=5))

        with_notes = client.get(f"/groups/{noted.id}").text
        without = client.get(f"/groups/{plain.id}").text

        assert "Earlier notes (carried over)" in with_notes
        assert "windy morning at the range" in with_notes
        assert "Earlier notes (carried over)" not in without
        assert "Group notes" in without

    @pytest.mark.parametrize(
        ("source", "expected"),
        [("photo", "(from photo)"), ("manual", "(entered)"), ("session", "(carried over)")],
    )
    def test_the_header_names_where_the_date_came_from(self, client, conn, source, expected):
        _, group = self._seeded(conn)
        with conn:
            conn.execute("UPDATE groups SET shot_at_source = ? WHERE id = ?", (source, group.id))

        html = client.get(f"/groups/{group.id}").text

        header = re.search(r'id="group-header">(.*?)</p>', html, re.DOTALL)[1]
        assert expected in " ".join(header.split())

    def test_no_template_or_script_mentions_a_session(self):
        for folder in ("templates", "static"):
            for path in (WEB / folder).rglob("*"):
                if path.suffix in {".html", ".js", ".css"}:
                    assert not re.search(r"(?i)\bsessions?\b|session_id", path.read_text()), path

    def test_the_context_script_is_csp_clean_and_warns_in_script_only(self):
        source = (WEB / "static/js/group_context.js").read_text()

        assert not re.search(r"fetch\(|innerHTML|outerHTML|document\.write|eval\(", source)
        assert not re.search(r"setAttribute\(\s*[\"']style|\.style\b|cssText|https?://", source)
        assert "recalculates every mil value" in source


# ------------------------------------------------------------------------------- zeroing page


class TestZeroingPage:
    def test_the_group_form_has_the_new_context_fields_and_no_session_select(self, client, conn):
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        zero = client.post(
            "/api/zeros",
            json={"rifle_id": rifle.id, "load_id": load.id, "mode": "new_setup"},
            headers={"Origin": ORIGIN},
        ).json()

        html = client.get(f"/zeroing/{zero['id']}").text

        for name in ("ag-photo", "ag-date", "ag-time", "ag-position", "ag-suppressor", "ag-lot"):
            assert f'id="{name}"' in html, name
        assert "Distance: 100 yd" in html
        assert "ag-session" not in html and not re.search(r"(?i)\bsessions?\b", html)
        for value in ("bench", "prone_off_pack", "kneeling_sitting", "standing_field"):
            assert f'<option value="{value}">' in html

    def test_the_script_posts_the_new_bodies_and_never_a_session(self):
        source = (WEB / "static/js/zeroing.js").read_text()

        assert "/api/sessions" not in source and "session_id" not in source
        assert '"/api/groups"' in source and "distance_yd: 100" in source
        assert "return_to=${back}" in source


# --------------------------------------------------------------------------------- review page


class TestReviewPage:
    def test_the_script_asks_for_a_region_before_detecting_a_shared_photo(self):
        source = (WEB / "static/js/review.js").read_text()

        assert "Draw a region first" in source
        assert "server.other_groups_on_photo > 0 && model.region === null" in source

    def test_the_state_carries_the_count_of_other_groups_on_the_photo(self, client, conn, settings):
        from tests.integration.test_group_plot import make_world

        world = make_world(conn)
        first = photo_group(conn, settings, world, ring(0.2, 0.1, n=5), aim=None)
        load, context = world
        second = seed_group(
            conn,
            load=load,
            context=context,
            offsets=ring(0.4, 0.2, n=5),
            photo=first.photo,
            finalize=False,
            aim=None,
        )

        raw = re.search(
            r'id="review-root"[^>]*data-state="([^"]*)"',
            client.get(f"/groups/{second.id}/review").text,
        )[1]
        state = json.loads(htmllib.unescape(raw))

        assert state["other_groups_on_photo"] == 1
        assert main_of(client.get(f"/groups/{first.group.id}").text)


# ----------------------------------------------------------------------------- real browser


@pytest.fixture
def live():
    """A live server, a Chromium page, and a connection bound to its data directory."""
    sync_api = pytest.importorskip("playwright.sync_api")
    from tests.support.live_server import run_live_server

    with run_live_server() as server, sync_api.sync_playwright() as playwright:
        settings = Settings(data_dir=server.data_dir, port=server.port)
        connection = connect(settings.db_path)
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1300, "height": 1100})
            problems: list[str] = []
            page.on("pageerror", lambda error: problems.append(str(error)))
            page.on(
                "console",
                lambda m: problems.append(m.text) if m.type == "error" else None,
            )
            yield page, server, connection, settings, problems
        finally:
            browser.close()
            connection.close()


def groups_of(page, server, rifle_id: int) -> list[dict]:
    response = page.request.get(f"{server}/api/groups?rifle_id={rifle_id}")
    assert response.status == 200
    return response.json()


def exif_jpeg(date: bytes = b"2024:10:05 14:30:15", offset: bytes = b"-06:00") -> bytes:
    tiff = with_prefix(build_tiff(date=date, offset=offset))
    segment = b"\xff\xe1" + (len(tiff) + 2).to_bytes(2, "big") + tiff
    return _jpeg_with(segment)


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestAddGroupFormInARealBrowser:
    def _world(self, conn):
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        return rifle, load

    def test_defaults_come_from_the_previous_group_and_weather_follows_the_date(self, live):
        page, server, conn, _, problems = live
        rifle, load = self._world(conn)
        with conn:
            suppressor = repo_groups.create_suppressor(conn, "Quiet one")
        context = seed_context(
            rifle,
            distance_yd=300,
            date="2026-09-30",
            position="prone_off_pack",
            suppressor=True,
            suppressor_id=suppressor.id,
            temperature_f=70.0,
            wind_mph=4.0,
        )
        seed_group(
            conn,
            load=load,
            context=context,
            offsets=ring(0.1, 0.1, n=7),
            lot_code="LOT-7",
            shots_expected=7,
        )

        page.goto(f"{server}/rifles/{rifle.id}")

        assert page.input_value("#g-distance") == "300"
        assert page.input_value("#g-position") == "prone_off_pack"
        assert page.is_checked("#g-suppressor")
        assert page.input_value("#g-suppressor-name") == str(suppressor.id)
        assert page.input_value("#g-lot") == "LOT-7" and page.input_value("#g-shots") == "7"
        assert page.input_value("#w-temp") == ""
        page.fill("#g-date", "2026-09-30")
        assert page.input_value("#w-temp") == "70" and page.input_value("#w-wind") == "4"
        assert page.is_enabled("#add-group")
        page.fill("#g-date", "2026-10-01")
        assert page.input_value("#w-temp") == "" and page.is_disabled("#add-group")
        assert page.is_visible("#group-blocked")
        assert problems == []

    @pytest.mark.parametrize(("distance", "blocked"), [("199", False), ("200", True)])
    def test_the_weather_gate_starts_at_200_yd(self, live, distance, blocked):
        page, server, conn, _, problems = live
        rifle, _ = self._world(conn)
        page.goto(f"{server}/rifles/{rifle.id}")

        page.fill("#g-distance", distance)

        assert page.is_visible("#group-blocked") is blocked
        assert page.is_disabled("#add-group") is blocked
        assert page.is_visible("#weather-required") is blocked
        if blocked:
            page.click("#skip-weather")
            assert page.is_enabled("#add-group") and page.is_visible("#weather-skipped-badge")
            page.fill("#w-temp", "55")
            assert page.is_enabled("#add-group") and not page.is_visible("#weather-skipped-badge")
            page.fill("#w-temp", "")
            assert page.is_disabled("#add-group")
        assert problems == []

    def test_adding_a_group_without_a_photo_lists_it_under_its_day(self, live):
        page, server, conn, _, problems = live
        rifle, load = self._world(conn)
        page.goto(f"{server}/rifles/{rifle.id}")
        page.fill("#g-date", "2026-09-30")
        page.fill("#g-time", "07:45")
        page.fill("#g-distance", "150")
        page.select_option("#g-position", "kneeling_sitting")
        page.fill("#g-lot", "LOT-NEW")
        page.fill("#g-shots", "4")
        page.fill("#g-notes", "first form group")

        with page.expect_navigation():
            page.click("#add-group")

        created = groups_of(page, server, rifle.id)
        assert len(created) == 1
        group = created[0]
        assert (group["shot_date"], group["shot_time"], group["distance_yd"]) == (
            "2026-09-30",
            "07:45:00",
            150,
        )
        assert group["position"] == "kneeling_sitting" and group["shot_at_source"] == "manual"
        assert group["lot_code"] == "LOT-NEW" and group["shots_expected"] == 4
        assert page.locator('.day[data-day="2026-09-30"] tr[data-group-id]').count() == 1
        assert page.inner_text(".group-notes") == "first form group"
        assert problems == []

    def test_a_photo_fills_the_date_and_time_and_the_group_is_marked_from_photo(self, live):
        page, server, conn, _, problems = live
        rifle, _ = self._world(conn)
        page.goto(f"{server}/rifles/{rifle.id}")

        page.set_input_files(
            "#g-photo", files=[{"name": "p.jpg", "mimeType": "image/jpeg", "buffer": exif_jpeg()}]
        )
        from playwright.sync_api import expect

        expect(page.locator("#g-date-source")).to_have_text("from photo")

        assert page.input_value("#g-date") == "2024-10-05"
        assert page.input_value("#g-time") == "14:30"
        page.fill("#g-lot", "LOT-P")
        with page.expect_navigation():
            page.click("#add-group")

        assert "/photos/" in page.url and "/reference" in page.url
        assert f"return_to=%2Frifles%2F{rifle.id}" in page.url
        group = groups_of(page, server, rifle.id)[0]
        assert group["shot_at_source"] == "photo" and group["utc_offset"] == "-06:00"
        assert (group["shot_date"], group["shot_time"]) == ("2024-10-05", "14:30:15")
        assert problems == []

    def test_editing_the_time_after_a_photo_makes_the_group_entered(self, live):
        page, server, conn, _, problems = live
        rifle, _ = self._world(conn)
        page.goto(f"{server}/rifles/{rifle.id}")
        page.set_input_files(
            "#g-photo", files=[{"name": "p.jpg", "mimeType": "image/jpeg", "buffer": exif_jpeg()}]
        )
        from playwright.sync_api import expect

        expect(page.locator("#g-date-source")).to_have_text("from photo")
        page.fill("#g-time", "09:00")
        expect(page.locator("#g-date-source")).to_have_text("entered")
        page.fill("#g-lot", "LOT-P")

        with page.expect_navigation():
            page.click("#add-group")

        group = groups_of(page, server, rifle.id)[0]
        assert group["shot_at_source"] == "manual" and group["shot_time"] == "09:00:00"
        assert problems == []

    def test_a_row_without_a_photo_can_import_one_and_use_another_groups_photo(self, live):
        page, server, conn, settings, problems = live
        rifle, load = self._world(conn)
        first = photo_group(
            conn,
            settings,
            (load, seed_context(rifle, date="2026-09-30")),
            ring(0.2, 0.1, n=5),
            aim=None,
        )
        bare = seed_group(
            conn,
            load=load,
            context=seed_context(rifle, date="2026-09-30"),
            offsets=[],
            shots_expected=5,
            finalize=False,
            aim=None,
        )
        page.goto(f"{server}/rifles/{rifle.id}")

        select = page.locator(f'.js-existing[data-group-id="{bare.id}"]')
        assert select.locator("option").all_inner_texts() == [
            "Use existing photo…",
            f"Photo of group #{first.group.id}",
        ]
        select.select_option(str(first.photo.id))
        with page.expect_navigation():
            page.click(f'.js-use-existing[data-group-id="{bare.id}"]')

        assert f"/groups/{bare.id}/review" in page.url
        assert f"return_to=%2Frifles%2F{rifle.id}" in page.url
        assert problems == []


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestGroupContextCardInARealBrowser:
    def _group(self, conn, **context):
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        ctx = seed_context(rifle, **context)
        group = seed_group(conn, load=load, context=ctx, offsets=ring(0.2, 0.1, n=10))
        return rifle, load, group

    def test_saving_the_context_updates_the_header_and_the_distance_warns(self, live):
        page, server, conn, _, problems = live
        _, _, group = self._group(conn, distance_yd=100, date="2026-09-30")
        page.goto(f"{server}/groups/{group.id}")
        assert not page.is_visible("#c-distance-warning")

        page.fill("#c-distance", "250")
        assert page.is_visible("#c-distance-warning")
        assert "recalculates every mil value" in page.inner_text("#c-distance-warning")
        page.fill("#c-date", "2026-09-28")
        page.fill("#c-time", "10:20")
        page.select_option("#c-position", "standing_field")
        page.fill("#c-temp", "48")
        with page.expect_navigation():
            page.click("#c-save")

        header = page.inner_text("#group-header")
        assert "2026-09-28 10:20" in header and "(entered)" in header
        assert "250 yd" in header and "Standing, field" in header and "48.0" in header
        assert page.input_value("#c-distance") == "250"
        assert not page.is_visible("#c-distance-warning")
        assert problems == []

    def test_a_group_at_200_yd_needs_weather_or_a_skip_before_saving(self, live):
        page, server, conn, _, problems = live
        _, _, group = self._group(conn, distance_yd=100)
        page.goto(f"{server}/groups/{group.id}")
        page.fill("#c-distance", "200")

        assert page.is_visible("#c-temp"), "the weather panel opens at 200 yd"
        page.click("#c-save")

        assert "Enter the temperature, or skip weather" in page.inner_text("#flash")
        page.check("#c-skip-weather")
        with page.expect_navigation():
            page.click("#c-save")
        assert "weather skipped" in page.inner_text("#group-header")
        assert problems == []

    def test_distance_is_locked_while_the_group_is_in_a_zero(self, live):
        page, server, conn, _, problems = live
        rifle, load, group = self._group(conn)
        zero = page.request.post(
            f"{server}/api/zeros",
            data=json.dumps({"rifle_id": rifle.id, "load_id": load.id, "mode": "existing"}),
            headers={"Content-Type": "application/json", "Origin": server},
        ).json()
        linked = page.request.post(
            f"{server}/api/zeros/{zero['id']}/groups",
            data=json.dumps({"group_id": group.id}),
            headers={"Content-Type": "application/json", "Origin": server},
        )
        assert linked.status == 200
        page.goto(f"{server}/groups/{group.id}")

        assert page.is_disabled("#c-distance")
        assert "Detach it from the zero" in page.inner_text("#c-distance-locked")
        page.select_option("#c-position", "bench")
        page.fill("#c-date", "2026-09-27")
        with page.expect_navigation():
            page.click("#c-save")
        assert "2026-09-27" in page.inner_text("#group-header")
        assert problems == []


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestReviewDrawRegionFirstInARealBrowser:
    def test_a_shared_photo_without_a_region_waits_for_the_user_instead_of_detecting(self, live):
        page, server, conn, settings, problems = live
        from tests.integration.test_group_plot import make_world

        world = make_world(conn)
        first = photo_group(conn, settings, world, ring(0.2, 0.1, n=5), aim=None)
        load, context = world
        second = seed_group(
            conn,
            load=load,
            context=context,
            offsets=[],
            photo=first.photo,
            finalize=False,
            aim=None,
            shots_expected=5,
        )
        posts: list[str] = []
        page.on(
            "request",
            lambda r: posts.append(r.url) if r.method == "POST" and "detect" in r.url else None,
        )

        page.goto(f"{server}/groups/{second.id}/review")
        page.locator("#editor-svg image").wait_for()

        assert "Draw a region first" in page.inner_text("#review-notices")
        assert "Drag a rectangle" in page.inner_text("#editor-hint")
        assert posts == []
        page.click("#detect-button")
        assert posts == [] and "Draw a region first" in page.inner_text("#review-notices")
        assert problems == []

    def test_a_group_alone_on_its_photo_still_detects_at_once(self, live):
        page, server, conn, settings, problems = live
        from tests.integration.test_group_plot import make_world

        world = make_world(conn)
        seeded = photo_group(conn, settings, world, ring(0.2, 0.1, n=5), aim=None)
        with conn:
            conn.execute("DELETE FROM shots WHERE group_id = ?", (seeded.group.id,))
        posts: list[str] = []
        page.on(
            "request",
            lambda r: posts.append(r.url) if r.method == "POST" and "detect" in r.url else None,
        )

        page.goto(f"{server}/groups/{seeded.group.id}/review")
        page.locator("#editor-svg image").wait_for()
        page.locator("#detect-button:enabled").wait_for()

        assert len(posts) == 1
        assert "Draw a region first" not in page.inner_text("#review-notices")
        assert problems == []


# ------------------------------------------------------------------- share header (B-81)


class TestShareHeaderKeepsTheConditions:
    NAME = "Thunder Beast Arms Ultra 7 Titanium"

    @staticmethod
    def drawn(monkeypatch) -> list[str]:
        from PIL import ImageDraw

        texts: list[str] = []
        real = ImageDraw.ImageDraw.text

        def spy(self, xy, text, *args, **kwargs):
            texts.append(text)
            return real(self, xy, text, *args, **kwargs)

        monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
        return texts

    @pytest.mark.parametrize("position", ["bench", "kneeling_sitting", "standing_field"])
    def test_a_long_suppressor_name_and_any_position_are_never_truncated(
        self, client, conn, monkeypatch, position
    ):
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        with conn:
            named = repo_groups.create_suppressor(conn, self.NAME)
        context = seed_context(
            rifle,
            distance_yd=300,
            position=position,
            suppressor=True,
            suppressor_id=named.id,
            time="08:15:00",
        )
        group = seed_group(conn, load=load, context=context, offsets=ring(0.2, 0.1, n=10))
        texts = self.drawn(monkeypatch)

        response = client.get(f"/api/groups/{group.id}/share.png?layers=")

        assert response.status_code == 200
        header = [t for t in texts if t.startswith("09/30/2026") or t.startswith("Suppressed")]
        assert header, "control: the spy must see the conditions line"
        joined = " | ".join(header)
        assert "..." not in joined
        assert f"Suppressed ({self.NAME})" in " ".join(joined.split(" | "))
        assert position.replace("_", " ") in joined
        assert "09/30/2026 08:15" in joined and "300 yd" in joined

    def test_a_short_header_stays_on_one_line(self, client, conn, monkeypatch):
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        group = seed_group(conn, load=load, offsets=ring(0.2, 0.1, n=10))
        texts = self.drawn(monkeypatch)

        client.get(f"/api/groups/{group.id}/share.png?layers=")

        assert "09/30/2026 | 100 yd | bench | Unsuppressed" in texts

    def test_an_edit_through_the_api_shows_on_the_next_image_without_a_cache(
        self, client, conn, monkeypatch
    ):
        rifle = seed_rifle(conn)
        load = seed_load(conn, rifle)
        group = seed_group(conn, load=load, offsets=ring(0.2, 0.1, n=10))
        with conn:
            named = repo_groups.create_suppressor(conn, "Omega 300")
        texts = self.drawn(monkeypatch)
        before = client.get(f"/api/groups/{group.id}/share.png?layers=")
        patched = client.patch(
            f"/api/groups/{group.id}",
            json={"suppressor": True, "suppressor_id": named.id},
            headers={"Origin": ORIGIN},
        )
        texts.clear()

        after = client.get(f"/api/groups/{group.id}/share.png?layers=")

        assert patched.status_code == 200, patched.text
        assert "no-store" in after.headers["cache-control"]
        assert after.content != before.content
        assert any("Suppressed (Omega 300)" in t for t in texts)
