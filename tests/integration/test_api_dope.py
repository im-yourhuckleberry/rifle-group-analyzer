"""T3.8: the dope table API and page.

Groups are seeded through the real repos and services at hand-picked distances so the centered
elevations can be computed by hand: one mil is 3.6 in at 100 yd, 7.2 in at 200 yd and 10.8 in at
300 yd, and centered elevation is `dialed - center_dy_mil`.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rga.config import Settings
from rga.db import repo_groups
from rga.db.connection import connect
from rga.domain.types import Offset, ShotOrigin
from rga.services import dope as dope_service
from rga.web.app import create_app
from rga.web.schemas import DopeOut, DopeRowOut, ErrorResponse
from tests.support.live_server import run_live_server
from tests.support.seed import seed_context, seed_group, seed_load, seed_rifle

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


@pytest.fixture
def rifle(conn):
    return seed_rifle(conn)


@pytest.fixture
def load(conn, rifle):
    return seed_load(conn, rifle, label="Factory 140 ELD-M")


def vertical(*dy_in: float) -> list[Offset]:
    """Shots on the aim's vertical line at the given heights (inches, y up)."""
    return [Offset(0.0, y) for y in dy_in]


def add_group(conn, rifle, load, *, yd, date, offsets, elevation, time=None, **fields):
    context = seed_context(
        rifle, distance_yd=yd, date=date, **({} if time is None else {"time": time})
    )
    return seed_group(
        conn, context=context, load=load, offsets=offsets, elevation_dialed_mil=elevation, **fields
    )


def rendered_dope_rows(url: str) -> list[list[str]]:
    """Open the dope page in headless Chromium and return the text of every table row's cells."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url)
            page.wait_for_selector("#dope-table table")
            return page.eval_on_selector_all(
                "#dope-table tbody tr",
                "rows => rows.map(r => Array.from(r.cells).map(c => c.textContent))",
            )
        finally:
            browser.close()


def get_dope(client: TestClient, load_id: int) -> DopeOut:
    response = client.get(f"/api/loads/{load_id}/dope")
    assert response.status_code == 200, response.text
    return DopeOut.model_validate(response.json())


@pytest.fixture
def three_distances(conn, rifle, load):
    """100 yd: center +0.1 mil; 200 yd: +0.2 mil; 300 yd: -0.2 mil (centers by hand below)."""
    near = add_group(
        conn,
        rifle,
        load,
        yd=100,
        date="2026-09-01",
        elevation=2.0,
        offsets=vertical(0.18, 0.36, 0.54),
    )  # mean 0.36 in = 0.1 mil @100
    mid = add_group(
        conn,
        rifle,
        load,
        yd=200,
        date="2026-09-02",
        elevation=4.6,
        offsets=vertical(1.44, 1.44, 1.44),
    )  # 1.44 in / 7.2 = 0.2 mil
    far = add_group(
        conn,
        rifle,
        load,
        yd=300,
        date="2026-09-03",
        elevation=7.0,
        offsets=vertical(-2.16, -2.16, -2.16, -2.16),
    )  # -2.16 in / 10.8 = -0.2 mil
    return near, mid, far


# ---------------------------------------------------------------- the API


class TestDopeApi:
    def test_three_distances_give_the_hand_computed_centered_elevations(
        self, client, load, three_distances
    ):
        dope = get_dope(client, load.id)
        assert dope.load_id == load.id
        assert [r.distance_yd for r in dope.rows] == [100, 200, 300]
        expected = [(2.0, 0.1, 1.9, 3), (4.6, 0.2, 4.4, 3), (7.0, -0.2, 7.2, 4)]
        for row, (dialed, center, centered, shots) in zip(dope.rows, expected, strict=True):
            assert row.elevation_dialed_mil == pytest.approx(dialed)
            assert row.center_dy_mil == pytest.approx(center, abs=1e-9)
            assert row.centered_elevation_mil == pytest.approx(centered, abs=1e-9)
            assert row.shots == shots
            assert row.includes_estimated is False

    def test_row_identifies_its_group_and_its_own_date_and_time(self, client, conn, rifle, load):
        timed = add_group(
            conn,
            rifle,
            load,
            yd=100,
            date="2026-09-01",
            time="07:30:15",
            elevation=2.0,
            offsets=vertical(0.1, 0.2),
        )
        untimed = add_group(
            conn, rifle, load, yd=200, date="2026-09-02", elevation=4.0, offsets=vertical(0.1, 0.2)
        )
        first, second = get_dope(client, load.id).rows
        assert (first.group_id, str(first.shot_date), first.shot_time) == (
            timed.id,
            "2026-09-01",
            "07:30:15",
        )
        assert (second.group_id, str(second.shot_date), second.shot_time) == (
            untimed.id,
            "2026-09-02",
            None,
        )
        body = client.get(f"/api/loads/{load.id}/dope").json()["rows"][0]
        assert "session_id" not in body and "date" not in body

    def test_rows_are_ordered_by_distance_then_date_then_time_then_group(
        self, conn, client, rifle, load
    ):
        later = add_group(
            conn, rifle, load, yd=100, date="2026-09-09", elevation=2.0, offsets=vertical(0.0, 0.0)
        )
        earlier = add_group(
            conn, rifle, load, yd=100, date="2026-09-01", elevation=2.1, offsets=vertical(0.0, 0.0)
        )
        far = add_group(
            conn, rifle, load, yd=50, date="2026-09-20", elevation=1.0, offsets=vertical(0.0, 0.0)
        )
        rows = get_dope(client, load.id).rows
        assert [r.group_id for r in rows] == [far.id, earlier.id, later.id]

    def test_groups_of_one_day_and_distance_are_ordered_by_time_with_no_time_first(
        self, conn, client, rifle, load
    ):
        evening = add_group(
            conn,
            rifle,
            load,
            yd=100,
            date="2026-09-01",
            time="18:00:00",
            elevation=2.0,
            offsets=vertical(0.0, 0.0),
        )
        morning = add_group(
            conn,
            rifle,
            load,
            yd=100,
            date="2026-09-01",
            time="06:00:00",
            elevation=2.0,
            offsets=vertical(0.0, 0.0),
        )
        untimed = add_group(
            conn, rifle, load, yd=100, date="2026-09-01", elevation=2.0, offsets=vertical(0.0, 0.0)
        )
        rows = get_dope(client, load.id).rows
        assert [r.group_id for r in rows] == [untimed.id, morning.id, evening.id]

    def test_a_groups_own_distance_is_what_the_row_shows(self, conn, client, rifle, load):
        add_group(
            conn, rifle, load, yd=100, date="2026-09-01", elevation=2.0, offsets=vertical(0.1, 0.2)
        )
        same_day_far = add_group(
            conn, rifle, load, yd=300, date="2026-09-01", elevation=7.0, offsets=vertical(0.1, 0.2)
        )
        rows = get_dope(client, load.id).rows
        assert [(r.distance_yd, str(r.shot_date)) for r in rows] == [
            (100, "2026-09-01"),
            (300, "2026-09-01"),
        ]
        assert rows[1].group_id == same_day_far.id

    def test_the_center_follows_the_stored_shot_positions_not_the_dialed_value(
        self, conn, client, rifle, load
    ):
        add_group(
            conn,
            rifle,
            load,
            yd=100,
            date="2026-09-01",
            elevation=3.0,
            offsets=vertical(0.36, 0.0, -0.36),
        )
        row = get_dope(client, load.id).rows[0]
        assert row.center_dy_mil == pytest.approx(0.0, abs=1e-9)
        assert row.centered_elevation_mil == pytest.approx(3.0)

    def test_includes_estimated_is_reported(self, conn, client, rifle, load):
        add_group(
            conn,
            rifle,
            load,
            yd=100,
            date="2026-09-01",
            elevation=2.0,
            offsets=vertical(0.1, 0.2, 0.3),
            origins=[ShotOrigin.DETECTED, ShotOrigin.ESTIMATED, ShotOrigin.DETECTED],
        )
        assert get_dope(client, load.id).rows[0].includes_estimated is True

    def test_rows_carry_no_velocity_fields(self, conn, client, rifle):
        fast = seed_load(conn, rifle, label="Handload", trued_mv_fps=2715.0)
        add_group(
            conn, rifle, fast, yd=100, date="2026-09-01", elevation=2.0, offsets=vertical(0.1, 0.2)
        )
        body = client.get(f"/api/loads/{fast.id}/dope").json()
        assert set(body) == {"load_id", "rows"}
        assert set(body["rows"][0]) == set(DopeRowOut.model_fields)
        assert "2715" not in client.get(f"/api/loads/{fast.id}/dope").text
        assert not any("mv" in key or "fps" in key or "velocity" in key for key in body["rows"][0])

    def test_a_load_without_groups_has_no_rows(self, client, load):
        assert get_dope(client, load.id).rows == []

    def test_drafts_are_excluded(self, conn, client, rifle, load):
        add_group(
            conn, rifle, load, yd=100, date="2026-09-01", elevation=2.0, offsets=vertical(0.1, 0.2)
        )
        add_group(
            conn,
            rifle,
            load,
            yd=200,
            date="2026-09-02",
            elevation=4.0,
            offsets=vertical(0.1),
            shots_expected=5,
            finalize=False,
        )
        rows = get_dope(client, load.id).rows
        assert [r.distance_yd for r in rows] == [100]

    def test_groups_without_a_recorded_elevation_are_excluded(self, conn, client, rifle, load):
        add_group(
            conn, rifle, load, yd=100, date="2026-09-01", elevation=None, offsets=vertical(0.1, 0.2)
        )
        add_group(
            conn, rifle, load, yd=200, date="2026-09-02", elevation=4.0, offsets=vertical(0.1, 0.2)
        )
        assert [r.distance_yd for r in get_dope(client, load.id).rows] == [200]

    def test_other_loads_groups_are_not_mixed_in(self, conn, client, rifle, load):
        other = seed_load(conn, rifle, label="Other load")
        add_group(
            conn, rifle, load, yd=100, date="2026-09-01", elevation=2.0, offsets=vertical(0.1, 0.2)
        )
        add_group(
            conn, rifle, other, yd=300, date="2026-09-02", elevation=7.0, offsets=vertical(0.1, 0.2)
        )
        assert [r.distance_yd for r in get_dope(client, load.id).rows] == [100]
        assert [r.distance_yd for r in get_dope(client, other.id).rows] == [300]

    def test_unknown_load_is_404(self, client):
        response = client.get("/api/loads/999/dope")
        assert response.status_code == 404
        assert ErrorResponse.model_validate(response.json()).code == "load_not_found"

    def test_bad_load_id_is_422_not_500(self, client):
        response = client.get("/api/loads/abc/dope")
        assert response.status_code == 422 and response.json()["code"] == "validation_error"

    def test_busy_database_is_503(self, client, load, monkeypatch):
        def locked(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(dope_service, "dope_table", locked)
        response = client.get(f"/api/loads/{load.id}/dope")
        assert response.status_code == 503 and response.json()["code"] == "busy"


# ---------------------------------------------------------------- zeroing groups never appear


ZEROING_CASES = [
    ("zero_step1", 3, False),
    ("zero_check", 2, False),
    ("zero_step2", 6, False),
    ("zero_confirm", 9, False),
    ("zero_step2", 10, True),
    ("zero_confirm", 12, True),
]


class TestZeroingGroupsAreExcluded:
    @pytest.mark.parametrize(("purpose", "shots", "listed"), ZEROING_CASES)
    def test_only_full_step2_and_confirm_groups_reach_the_api(
        self, conn, client, rifle, load, purpose, shots, listed
    ):
        offsets = [Offset(0.05 * i, 0.9) for i in range(shots)]
        group = add_group(
            conn,
            rifle,
            load,
            yd=100,
            date="2026-09-01",
            elevation=1.5,
            offsets=offsets,
            purpose=purpose,
        )
        ids = [r.group_id for r in get_dope(client, load.id).rows]
        assert (group.id in ids) is listed
        page = client.get(f"/dope?load_id={load.id}")
        assert page.status_code == 200 and 'id="dope-table"' in page.text

    def test_the_rendered_dope_table_lists_only_eligible_groups(self):
        """The table is drawn in the browser from the API, so this drives a real page.

        Every group has its own date and elevation, so a row can be recognised by what the page
        renders (date and dialed columns), not by an id the page never shows.
        """
        eligible = {
            "2026-03-14": ("general", 5, 2.7),
            "2026-02-21": ("zero_step2", 10, 1.9),
            "2026-02-22": ("zero_confirm", 12, 1.8),
        }
        hidden = {
            "2026-01-11": ("zero_step1", 3, 4.1),
            "2026-01-12": ("zero_check", 2, 4.2),
            "2026-01-13": ("zero_step2", 6, 4.3),
            "2026-01-14": ("zero_confirm", 9, 4.4),
        }
        with run_live_server() as server:
            db = connect(server.data_dir / "rga.sqlite3")
            try:
                shooter = seed_rifle(db)
                ammo = seed_load(db, shooter, label="Factory 140 ELD-M")
                for date, (purpose, shots, elevation) in {**eligible, **hidden}.items():
                    add_group(
                        db,
                        shooter,
                        ammo,
                        yd=100,
                        date=date,
                        elevation=elevation,
                        offsets=[Offset(0.05 * i, 0.9) for i in range(shots)],
                        purpose=purpose,
                    )
            finally:
                db.close()
            rows = rendered_dope_rows(f"{server.url}/dope?load_id={ammo.id}")

        dates = [row[1] for row in rows]
        dialed = [row[2] for row in rows]
        assert sorted(dates) == sorted(eligible), "positive control: the eligible groups are drawn"
        assert sorted(dialed) == sorted(f"{e[2]:.2f}" for e in eligible.values())
        for date, (_, _, elevation) in hidden.items():
            assert date not in dates and f"{elevation:.2f}" not in dialed

    def test_the_service_never_turns_a_masked_group_into_a_center(
        self, conn, client, rifle, load, monkeypatch
    ):
        """Even if the repo query leaked a masked group, no point of impact is produced for it."""
        check = add_group(
            conn,
            rifle,
            load,
            yd=100,
            date="2026-09-01",
            elevation=1.5,
            offsets=[Offset(0.0, 0.9), Offset(0.0, 1.0)],
            purpose="zero_check",
        )
        short = add_group(
            conn,
            rifle,
            load,
            yd=100,
            date="2026-09-02",
            elevation=1.5,
            offsets=[Offset(0.0, 0.9)] * 4,
            purpose="zero_step2",
        )
        good = add_group(
            conn,
            rifle,
            load,
            yd=100,
            date="2026-09-03",
            elevation=1.5,
            offsets=[Offset(0.0, 0.36)] * 3,
        )
        real = dope_service.repo_dope.dope_rows
        rows = {r.group_id: r for r in real(conn, load.id)}
        leaked = [
            replace_row(rows[good.id], group_id=check.id),
            replace_row(rows[good.id], group_id=short.id),
            rows[good.id],
        ]
        monkeypatch.setattr(dope_service.repo_dope, "dope_rows", lambda *_a: leaked)
        ids = [r.group_id for r in get_dope(client, load.id).rows]
        assert ids == [good.id]


def replace_row(row, **changes):
    from dataclasses import replace

    return replace(row, **changes)


# ---------------------------------------------------------------- the page


class TestDopePage:
    def test_without_a_query_string_the_page_is_a_load_picker(self, conn, client, rifle):
        first = seed_load(conn, rifle, label="Factory 140")
        second = seed_load(conn, rifle, label="Handload 143")
        response = client.get("/dope")
        assert response.status_code == 200
        html = response.text
        assert "Choose a load" in html
        for load in (first, second):
            assert f'href="/dope?load_id={load.id}"' in html
        assert "dope-table" not in html and "dope.js" not in html

    def test_picker_says_so_when_there_are_no_loads(self, client):
        html = client.get("/dope").text
        assert "no loads yet" in html and 'href="/rifles"' in html

    def test_load_page_has_the_table_mount_and_script(self, client, load):
        html = client.get(f"/dope?load_id={load.id}").text
        assert f'data-load-id="{load.id}"' in html and 'id="dope-table"' in html
        assert '<script type="module" src="/static/js/dope.js"></script>' in html
        assert load.label in html

    def test_trued_mv_is_shown_as_information_only_when_stored(self, conn, client, rifle):
        with_mv = seed_load(conn, rifle, label="Trued", trued_mv_fps=2715.0)
        without = seed_load(conn, rifle, label="Plain")
        shown = client.get(f"/dope?load_id={with_mv.id}").text
        assert "2715 fps" in shown and "information only" in shown
        assert "fps" not in client.get(f"/dope?load_id={without.id}").text

    def test_unknown_load_is_404(self, client):
        response = client.get("/dope?load_id=999")
        assert response.status_code == 404 and response.json()["code"] == "load_not_found"

    def test_labels_are_escaped(self, conn, client, rifle):
        nasty = seed_load(conn, rifle, label="<script>alert(1)</script>")
        html = client.get(f"/dope?load_id={nasty.id}").text
        assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html

    def test_page_obeys_the_csp_rules(self, client, load):
        for url in ("/dope", f"/dope?load_id={load.id}"):
            html = client.get(url).text
            assert not re.search(r"<script(?![^>]*\bsrc=)", html)
            assert "<style" not in html and " style=" not in html
            assert not re.search(r"\son[a-z]+\s*=", html) and "javascript:" not in html
            assert "http://" not in html and "https://" not in html

    def test_script_uses_only_api_helpers_and_text_content(self):
        script = (PROJECT_ROOT / "src/rga/web/static/js/dope.js").read_text(encoding="utf-8")
        assert "fetch(" not in script and "innerHTML" not in script and "eval(" not in script
        assert "apiGet" in script and "/dope" in script
        for forbidden in ("mean_radius", "best_of", "group_reduction", "velocity", "fps"):
            assert forbidden not in script


class TestLayering:
    def test_the_route_imports_only_services_schemas_and_web_helpers(self):
        source = (PROJECT_ROOT / "src/rga/web/routes/dope.py").read_text(encoding="utf-8")
        modules = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("rga"):
                modules.add(node.module)
        assert modules <= {"rga.services", "rga.web.app", "rga.web.deps", "rga.web.schemas"}, (
            modules
        )

    def test_the_route_never_mentions_a_repo_module(self):
        assert "repo_" not in (PROJECT_ROOT / "src/rga/web/routes/dope.py").read_text(
            encoding="utf-8"
        )


def test_repo_view_has_what_the_service_needs(conn, rifle, load):
    group = add_group(
        conn, rifle, load, yd=100, date="2026-09-01", elevation=2.0, offsets=vertical(0.1, 0.2)
    )
    assert repo_groups.get_group(conn, group.id).status == "final"
