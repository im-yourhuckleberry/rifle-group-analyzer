"""R11 (B-74): the Groups tab, `GET /groups`: every group of every rifle, newest first, size only.

Covers each filter alone and combined, ordering, 100-row paging, masked rows (size, never a point of
impact: leak scan), escaping, empty states, invalid and huge query values (422, never a 500), the
nav link on every page, the CSP-clean script, and a Chromium scroll, narrow and open check.
"""

from __future__ import annotations

import html as html_module
import os
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rga.config import Settings
from rga.db import repo_groups
from rga.db.connection import connect
from rga.domain.types import Offset, PlanePoint, ShotOrigin
from rga.services import analysis as analysis_service
from rga.web.app import create_app
from tests.support.live_server import run_live_server
from tests.support.seed import seed_context, seed_group, seed_load, seed_rifle

ORIGIN = "http://127.0.0.1:8765"
XSS = "<script>alert(1)</script>"
WEB = Path(__file__).resolve().parents[2] / "src" / "rga" / "web"
OFFSETS = [Offset(0.1 * i - 0.3, 0.05 * i - 0.1) for i in range(6)]
LEAK_WORDS = re.compile(
    r"\b(poi|point of impact|offset|correction|dialed|aim|high|low|left|right|mean)\b", re.I
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


def make(conn, rifle, load, **ctx_and_more):
    """Seed one group; `date`, `time`, `distance_yd`, `position`, `suppressor`, `suppressor_id` and
    `notes` go to the context, the rest to `seed_group`."""
    names = ("date", "time", "distance_yd", "position", "suppressor", "suppressor_id", "notes")
    ctx = {k: ctx_and_more.pop(k) for k in names if k in ctx_and_more}
    ctx_notes = ctx.pop("notes", "")
    context = seed_context(rifle, notes=ctx_notes, **ctx)
    ctx_and_more.setdefault("offsets", OFFSETS)
    return seed_group(conn, context=context, load=load, **ctx_and_more)


@pytest.fixture
def world(conn):
    """Two rifles, three loads, two suppressors and a spread of groups."""
    alpha = seed_rifle(conn, name="Alpha")
    beta = seed_rifle(conn, name="Beta")
    a_load = seed_load(conn, alpha, label="Alpha Load")
    a_load2 = seed_load(conn, alpha, label="Alpha Load Two")
    b_load = seed_load(conn, beta, label="Beta Load")
    can_a = repo_groups.create_suppressor(conn, "Can A")
    can_b = repo_groups.create_suppressor(conn, "Can B")
    conn.commit()
    g = {
        "a1": make(conn, alpha, a_load, date="2026-10-01", time="08:00:00", distance_yd=100),
        "a2": make(
            conn,
            alpha,
            a_load,
            date="2026-10-01",
            time="14:30:00",
            distance_yd=300,
            position="prone_off_pack",
            suppressor=True,
            suppressor_id=can_a.id,
        ),
        "a3": make(
            conn,
            alpha,
            a_load2,
            date="2026-10-02",
            distance_yd=100,
            suppressor=True,
            suppressor_id=can_b.id,
        ),
        "b1": make(
            conn, beta, b_load, date="2026-09-20", distance_yd=200, position="standing_field"
        ),
        "b2": make(conn, beta, b_load, date="2026-10-02", time="07:00:00", distance_yd=100),
        "draft": make(conn, beta, b_load, date="2026-10-03", distance_yd=100, finalize=False),
        "zcheck": make(
            conn,
            alpha,
            a_load,
            date="2026-09-25",
            distance_yd=100,
            purpose="zero_check",
            offsets=OFFSETS[:2],
        ),
    }
    return {
        "alpha": alpha,
        "beta": beta,
        "a_load": a_load,
        "a_load2": a_load2,
        "b_load": b_load,
        "can_a": can_a,
        "can_b": can_b,
        "groups": g,
        "ids": {name: group.id for name, group in g.items()},
    }


def get(client: TestClient, query: str = "") -> str:
    response = client.get("/groups" + query)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    return response.text


def row_ids(html: str) -> list[int]:
    return [int(i) for i in re.findall(r'<tr class="clickable" data-group-id="(\d+)"', html)]


def names(world, html: str) -> set[str]:
    by_id = {v: k for k, v in world["ids"].items()}
    return {by_id[i] for i in row_ids(html)}


def error(response, code: str = "validation_error") -> None:
    assert response.status_code == 422, (response.status_code, response.text[:200])
    body = response.json()
    assert set(body) == {"detail", "code"} and body["code"] == code, body


# ----------------------------------------------------------------------------------- listing


def test_every_group_of_every_rifle_is_listed_newest_first_by_date_time_then_id(client, world):
    html = get(client)

    ids = world["ids"]
    assert row_ids(html) == [
        ids["draft"],  # 2026-10-03
        ids["b2"],  # 2026-10-02 07:00 (a group with no time sorts after the timed ones that day)
        ids["a3"],  # 2026-10-02, no time
        ids["a2"],  # 2026-10-01 14:30
        ids["a1"],  # 2026-10-01 08:00
        ids["zcheck"],  # 2026-09-25
        ids["b1"],  # 2026-09-20
    ]
    assert "7 groups" in html


def test_a_row_shows_when_where_how_and_the_size(client, world):
    html = get(client, f"?rifle_id={world['alpha'].id}&distance_yd=300")

    [row] = re.findall(r'<tr class="clickable".*?</tr>', html, re.S)
    text = " ".join(re.sub(r"<[^>]+>", " ", row).split())
    gid = world["ids"]["a2"]
    assert (
        f'data-href="/groups/{gid}"' in row
        and f'<a href="/groups/{gid}">2026-10-01 14:30</a>' in row
    )
    for expected in (
        "Alpha",
        "Alpha Load",
        "LOT-1",
        "300 yd",
        "Prone off a pack",
        "Suppressed (Can A)",
        "6 of 6",
        "General",
        "Final",
    ):
        assert expected in text, expected
    assert re.search(r"\d+\.\d\d in · \d+\.\d\d MOA · \d+\.\d\d mil", text)


def test_the_size_in_mil_uses_the_groups_own_distance(client, conn, world):
    from rga.services import groups as group_service

    stats = group_service.group_stats(conn, world["ids"]["a2"])  # shot at 300 yd
    html = get(client, "?distance_yd=300")

    assert f"{stats.es_in * 1000 / (300 * 36):.2f} mil" in html
    assert f"{stats.es_in * 1000 / (100 * 36):.2f} mil" not in html


def test_groups_shot_on_the_same_date_and_time_list_the_higher_id_first(client, conn, world):
    first = make(conn, world["alpha"], world["a_load"], date="2026-11-01", time="09:00:00")
    second = make(conn, world["alpha"], world["a_load"], date="2026-11-01", time="09:00:00")

    assert row_ids(get(client, "?date_from=2026-11-01")) == [second.id, first.id]


def test_the_size_matches_the_group_statistics(client, conn, world):
    from rga.services import groups as group_service

    stats = group_service.group_stats(conn, world["ids"]["a1"])
    html = get(client, f"?rifle_id={world['alpha'].id}&distance_yd=100&date_to=2026-10-01")

    mil = stats.es_in * 1000 / (100 * 36)
    assert f"{stats.es_in:.2f} in · {stats.es_moa:.2f} MOA · {mil:.2f} mil" in html


def test_a_draft_is_listed_with_its_status_and_a_group_without_shots_says_none_yet(
    client, conn, world
):
    empty = repo_groups.create_group(
        conn,
        load_id=world["b_load"].id,
        lot_id=repo_groups.get_group(conn, world["ids"]["b1"]).lot_id,
        shots_expected=5,
        shot_date="2026-10-05",
        distance_yd=100,
        position="bench",
        suppressor=False,
    )
    conn.commit()

    html = get(client)

    draft_row = re.search(
        rf'<tr class="clickable" data-group-id="{world["ids"]["draft"]}".*?</tr>', html, re.S
    )[0]
    assert '<span class="badge warn">Draft</span>' in draft_row
    empty_row = re.search(rf'<tr class="clickable" data-group-id="{empty.id}".*?</tr>', html, re.S)[
        0
    ]
    assert "none yet" in empty_row and "0 of 5" in empty_row


def test_estimated_shots_carry_the_badge_and_other_rows_do_not(client, conn, world):
    estimated = make(
        conn,
        world["alpha"],
        world["a_load"],
        date="2026-10-09",
        origins=[ShotOrigin.DETECTED] * 5 + [ShotOrigin.ESTIMATED],
    )

    html = get(client)

    est_row = re.search(
        rf'<tr class="clickable" data-group-id="{estimated.id}".*?</tr>', html, re.S
    )[0]
    assert "includes estimated shots" in est_row
    assert html.count("includes estimated shots") == 1


def test_the_notes_snippet_collapses_whitespace_and_is_cut_with_an_ellipsis(client, conn, world):
    long = make(
        conn,
        world["alpha"],
        world["a_load"],
        date="2026-10-10",
        notes="word  " * 80,
    )

    html = get(client, "?date_from=2026-10-10")

    row = re.search(rf'<tr class="clickable" data-group-id="{long.id}".*?</tr>', html, re.S)[0]
    snippet = re.search(r'<td class="muted">(.*?)</td>', row, re.S)[1]
    assert len(snippet) == analysis_service.NOTES_SNIPPET_CHARS and snippet.endswith("…")
    assert "  " not in snippet


def test_a_migrated_groups_legacy_notes_are_the_snippet_when_it_has_no_notes_of_its_own(
    client, conn, world
):
    with conn:
        conn.execute(
            "UPDATE groups SET legacy_session_notes = 'from the old day', notes = '' WHERE id = ?",
            (world["ids"]["b1"],),
        )

    assert "from the old day" in get(client, "?date_to=2026-09-20")


# ----------------------------------------------------------------------------------- filters


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("rifle_id={alpha}", {"a1", "a2", "a3", "zcheck"}),
        ("rifle_id={beta}", {"b1", "b2", "draft"}),
        ("load_id={a_load2}", {"a3"}),
        ("load_id={b_load}", {"b1", "b2", "draft"}),
        ("distance_yd=100", {"a1", "a3", "b2", "draft", "zcheck"}),
        ("distance_yd=300", {"a2"}),
        ("distance_yd=250", set()),
        ("position=prone_off_pack", {"a2"}),
        ("position=standing_field", {"b1"}),
        ("position=bench", {"a1", "a3", "b2", "draft", "zcheck"}),
        ("suppressor=yes", {"a2", "a3"}),
        ("suppressor=none", {"a1", "b1", "b2", "draft", "zcheck"}),
        ("suppressor_id={can_a}", {"a2"}),
        ("suppressor_id={can_b}", {"a3"}),
        ("suppressor=yes&suppressor_id={can_b}", {"a3"}),
        ("suppressor=none&suppressor_id={can_b}", set()),
        ("status=draft", {"draft"}),
        ("status=final", {"a1", "a2", "a3", "b1", "b2", "zcheck"}),
        ("purpose=zero_check", {"zcheck"}),
        ("purpose=general", {"a1", "a2", "a3", "b1", "b2", "draft"}),
        ("purpose=zero_step1", set()),
        ("date_from=2026-10-02", {"a3", "b2", "draft"}),
        ("date_to=2026-09-25", {"b1", "zcheck"}),
        ("date_from=2026-10-01&date_to=2026-10-01", {"a1", "a2"}),
        ("date_from=2026-10-02&date_to=2026-10-01", set()),
    ],
)
def test_each_filter_narrows_the_list(client, world, query, expected):
    filled = query.format(
        alpha=world["alpha"].id,
        beta=world["beta"].id,
        a_load2=world["a_load2"].id,
        b_load=world["b_load"].id,
        can_a=world["can_a"].id,
        can_b=world["can_b"].id,
    )

    assert names(world, get(client, "?" + filled)) == expected


def test_filters_combine_with_and(client, world):
    query = (
        f"?rifle_id={world['alpha'].id}&distance_yd=100&position=bench"
        "&suppressor=yes&status=final&purpose=general&date_from=2026-10-02&date_to=2026-10-02"
    )

    assert names(world, get(client, query)) == {"a3"}
    assert names(world, get(client, query + "&load_id=" + str(world["a_load"].id))) == set()


def test_a_rifle_and_a_load_of_another_rifle_match_nothing(client, world):
    query = f"?rifle_id={world['beta'].id}&load_id={world['a_load'].id}"

    html = get(client, query)

    assert row_ids(html) == [] and "No groups match these filters" in html


def test_blank_values_and_unknown_parameters_are_ignored(client, world):
    everything = row_ids(get(client))

    blank = "?rifle_id=&load_id=&distance_yd=&position=&suppressor=&suppressor_id=&status=&purpose=&date_from=&date_to=&page="
    assert row_ids(get(client, blank)) == everything
    assert row_ids(get(client, "?utm_source=bookmark&q=x")) == everything
    assert row_ids(get(client, "?rifle_id=%20%20")) == everything


def test_the_form_keeps_the_chosen_values_and_offers_a_clear_link(client, world):
    query = (
        f"?rifle_id={world['beta'].id}&load_id={world['b_load'].id}&distance_yd=100"
        f"&position=bench&suppressor=none&status=final&purpose=general"
        f"&date_from=2026-09-01&date_to=2026-10-31"
    )

    html = get(client, query)

    form = html.split('data-form="group-filters"')[1].split("</form>")[0]
    selected = re.findall(r'<option value="([^"]*)"[^>]*? selected', form)
    assert set(selected) >= {
        str(world["beta"].id),
        str(world["b_load"].id),
        "100",
        "bench",
        "none",
        "final",
        "general",
    }
    assert 'value="2026-09-01"' in form and 'value="2026-10-31"' in form
    assert '<a class="button secondary" href="/groups" data-clear-filters>Clear filters</a>' in form
    assert 'method="get" action="/groups"' in html


def test_the_filter_panel_is_closed_on_load_with_and_without_active_filters(client, world):
    for query in ("", f"?rifle_id={world['alpha'].id}", "?status=final&position=bench&page=2"):
        html = get(client, query)

        details = re.search(r"<details\b[^>]*>", html)[0]
        assert "open" not in details and "data-filters-panel" in details
        assert html.count("<details") == 1 and "<summary>" in html


@pytest.mark.parametrize(
    ("query", "label"),
    [
        ("", "Filters"),
        ("?rifle_id=&status=&page=", "Filters"),
        ("?utm_source=bookmark", "Filters"),
        ("?page=1", "Filters"),
        ("?status=final", "Filters (1 active)"),
        ("?distance_yd=100&position=bench", "Filters (2 active)"),
        ("?date_from=2026-10-01&date_to=2026-10-31&purpose=general", "Filters (3 active)"),
        ("?suppressor=yes&suppressor_id=1&status=draft&page=2", "Filters (3 active)"),
        (
            "?rifle_id=1&load_id=1&distance_yd=100&position=bench&suppressor=none&suppressor_id=1"
            "&status=final&purpose=general&date_from=2026-01-01&date_to=2026-12-31",
            "Filters (10 active)",
        ),
    ],
)
def test_the_summary_counts_the_active_filters(client, world, query, label):
    html = get(client, query)

    assert re.search(r"<summary>(.*?)</summary>", html)[1] == label


def test_a_clear_link_sits_next_to_the_summary_only_while_filters_are_active(client, world):
    plain, filtered = get(client), get(client, "?status=final")

    assert "data-clear-active" not in plain
    link = re.search(
        r'<a class="button secondary" href="/groups" data-clear-active>Clear</a>', filtered
    )
    assert link and filtered.index("</details>") < link.start()
    for html in (plain, filtered):
        panel = html.split("<details")[1].split("</details>")[0]
        assert (
            '<a class="button secondary" href="/groups" data-clear-filters>Clear filters</a>'
            in panel
        )


def test_the_load_list_keeps_every_load_but_hides_the_other_rifles_when_a_rifle_is_chosen(
    client, world
):
    html = get(client, f"?rifle_id={world['alpha'].id}")

    load_select = html.split('id="f-load"')[1].split("</select>")[0]
    options = re.findall(
        r"<option value=\"(\d+)\"[^>]*data-rifle-id=\"(\d+)\"([^>]*)>", load_select
    )
    assert {value for value, _, _ in options} == {
        str(world[k].id) for k in ("a_load", "a_load2", "b_load")
    }
    hidden = {value for value, rifle, attrs in options if "hidden disabled" in attrs}
    assert hidden == {str(world["b_load"].id)}
    assert not {
        value
        for value, rifle, attrs in options
        if rifle == str(world["alpha"].id) and "hidden" in attrs
    }


def test_the_filter_options_list_rifles_loads_suppressors_and_the_distances_present(client, world):
    html = get(client)

    for text in ("Alpha", "Beta", "Alpha Load Two", "Can A", "Can B"):
        assert text in html
    distances = re.findall(r'<option value="(\d+)"[^>]*>(\d+) yd</option>', html)
    assert [d for d, _ in distances] == ["100", "200", "300"]


# ----------------------------------------------------------------------------------- paging


def _bulk(conn, world, count: int, **kw):
    rifle, load = world["alpha"], world["a_load"]
    for n in range(count):
        make(
            conn,
            rifle,
            load,
            date="2026-08-01",
            time=f"{n // 60:02d}:{n % 60:02d}:00",
            offsets=OFFSETS[:3],
            **kw,
        )


def test_paging_is_100_rows_with_links_that_keep_the_filters(client, conn, world):
    _bulk(conn, world, 205)
    filters = f"rifle_id={world['alpha'].id}&distance_yd=100&date_to=2026-08-31"

    first = get(client, "?" + filters)
    second = get(client, "?" + filters + "&page=2")
    third = get(client, "?" + filters + "&page=3")

    assert (len(row_ids(first)), len(row_ids(second)), len(row_ids(third))) == (100, 100, 5)
    assert not set(row_ids(first)) & set(row_ids(second))
    assert "205 groups match" in first and "Page 1 of 3" in first and "Page 3 of 3" in third
    assert 'rel="prev"' not in first and 'rel="next"' not in third
    next_href = re.search(r'<a class="button secondary" href="([^"]+)" rel="next">', first)[1]
    next_href = html_module.unescape(next_href)
    parsed = parse_qs(urlsplit(next_href).query)
    assert urlsplit(next_href).path == "/groups"
    assert parsed == {
        "rifle_id": [str(world["alpha"].id)],
        "distance_yd": ["100"],
        "date_to": ["2026-08-31"],
        "page": ["2"],
    }
    prev_href = html_module.unescape(
        re.search(r'<a class="button secondary" href="([^"]+)" rel="prev">', second)[1]
    )
    assert "page" not in parse_qs(urlsplit(prev_href).query)


def test_exactly_100_rows_is_one_page_and_101_is_two(client, conn, world):
    _bulk(conn, world, 93)  # the world already holds 7 groups
    assert "Page 1 of 1" in get(client) and len(row_ids(get(client))) == 100
    _bulk(conn, world, 1)
    html = get(client)
    assert "Page 1 of 2" in html and len(row_ids(get(client, "?page=2"))) == 1


def test_the_order_holds_across_the_page_boundary(client, conn, world):
    _bulk(conn, world, 120)

    both = row_ids(get(client)) + row_ids(get(client, "?page=2"))

    keys = [
        (g.shot_date, g.shot_time or "", g.id)
        for g in (repo_groups.get_group(conn, i) for i in both)
    ]
    assert keys == sorted(keys, reverse=True) and len(both) == 127


def test_a_page_past_the_last_is_empty_with_a_way_back(client, world):
    html = get(client, "?page=9")

    assert row_ids(html) == []
    assert "There are no groups on this page" in html and 'href="/groups"' in html


# ----------------------------------------------------------------------------------- masking


def test_masked_groups_show_size_and_the_page_never_shows_a_point_of_impact(client, conn, world):
    rifle, load = world["alpha"], world["a_load"]
    masked = [
        make(
            conn,
            rifle,
            load,
            date="2026-10-20",
            purpose="zero_check",
            offsets=OFFSETS[:2],
            aim=PlanePoint(7.123457, 2.654321),
        ),
        make(
            conn,
            rifle,
            load,
            date="2026-10-21",
            purpose="zero_step2",
            offsets=OFFSETS[:5],
            aim=PlanePoint(7.123457, 2.654321),
        ),
        make(
            conn,
            rifle,
            load,
            date="2026-10-22",
            purpose="zero_confirm",
            offsets=OFFSETS[:4],
            aim=PlanePoint(7.123457, 2.654321),
        ),
    ]

    html = get(client)

    for group in masked:
        row = re.search(rf'<tr class="clickable" data-group-id="{group.id}".*?</tr>', html, re.S)[0]
        assert re.search(r"\d+\.\d\d in · \d+\.\d\d MOA · \d+\.\d\d mil", row)
    main = html.split("<main", 1)[1]
    text = re.sub(r"<[^>]+>", " ", main)
    assert not LEAK_WORDS.search(text.replace("Extreme spread", "")), LEAK_WORDS.search(text)
    for needle in ("7.123457", "2.654321", "mean_dx", "mean_dy", "aim_x", "offset"):
        assert needle not in html


def test_a_general_groups_row_carries_no_point_of_impact_either(client, world):
    html = get(client, "?purpose=general")

    main = html.split("<main", 1)[1]
    assert not LEAK_WORDS.search(re.sub(r"<[^>]+>", " ", main))


def test_the_row_model_has_no_point_of_impact_fields():
    fields = set(analysis_service.GroupListRow.__dataclass_fields__)

    assert not {f for f in fields if re.search(r"poi|offset|aim|correction|mean", f)}
    assert fields == {
        "group",
        "rifle_name",
        "load_label",
        "lot_code",
        "suppressor_name",
        "shot_count",
        "size",
        "notes_snippet",
        "purpose_label",
    }


# ----------------------------------------------------------------------------------- escaping


def test_user_text_is_escaped_everywhere_on_the_page(client, conn):
    rifle = seed_rifle(conn, name=XSS)
    load = seed_load(conn, rifle, label=XSS)
    can = repo_groups.create_suppressor(conn, XSS)
    conn.commit()
    make(
        conn,
        rifle,
        load,
        date="2026-10-01",
        suppressor=True,
        suppressor_id=can.id,
        lot_code=XSS,
        notes=XSS,
    )

    html = get(client)

    assert XSS not in html
    assert html.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 6
    assert len(re.findall(r"<script\b", html)) == 1


def test_group_notes_are_escaped_in_the_snippet(client, conn, world):
    group = make(conn, world["alpha"], world["a_load"], date="2026-10-11")
    with conn:
        conn.execute("UPDATE groups SET notes = ? WHERE id = ?", (XSS, group.id))

    html = get(client, "?date_from=2026-10-11")

    assert XSS not in html and "&lt;script&gt;" in html


# ----------------------------------------------------------------------------------- empty states


def test_with_no_groups_the_page_says_so_and_points_at_the_rifles_page(client):
    html = get(client)

    assert "0 groups" in html and "No groups yet" in html and 'href="/rifles"' in html
    assert "Page " not in html and "<table" not in html


def test_filters_that_match_nothing_offer_to_clear_them(client, world):
    html = get(client, "?distance_yd=999")

    assert "0 groups match" in html and "No groups match these filters" in html
    assert '<a href="/groups">Clear filters</a>' in html


# ----------------------------------------------------------------------------------- bad input


@pytest.mark.parametrize(
    "query",
    [
        "rifle_id=0",
        "rifle_id=-1",
        "rifle_id=abc",
        "rifle_id=1.5",
        f"rifle_id={2**63}",
        f"rifle_id={2**64}",
        f"load_id={2**63}",
        f"suppressor_id={2**63}",
        "suppressor_id=0",
        "distance_yd=0",
        "distance_yd=-5",
        "distance_yd=abc",
        "distance_yd=100001",
        f"distance_yd={2**70}",
        "position=sideways",
        "suppressor=maybe",
        "status=archived",
        "purpose=zero_everything",
        "date_from=2026-02-30",
        "date_to=2026-13-01",
        "date_from=2026/10/01",
        "date_from=20261001",
        "date_to=2026-10-01T10:00:00",
        "date_from=tomorrow",
        "page=0",
        "page=-1",
        "page=abc",
        "page=1.5",
        f"page={10**7}",
        f"page={2**70}",
    ],
)
def test_malformed_or_huge_values_are_a_422_with_the_error_model_never_a_500(client, world, query):
    error(client.get("/groups?" + query))


def test_a_huge_query_string_and_odd_characters_never_break_the_page(client, world):
    for query in (
        "rifle_id=" + "9" * 5000,
        "position=%00",
        "date_from=%E2%80%AE2026",
        "status=" + "x" * 5000,
        "rifle_id=1&rifle_id=2",
    ):
        assert client.get("/groups?" + query).status_code in (200, 422)


def test_a_busy_database_is_a_503_with_a_retry_hint(client, monkeypatch):
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(analysis_service, "group_list", locked)

    response = client.get("/groups")

    assert response.status_code == 503 and response.json()["code"] == "busy"
    assert response.headers["retry-after"] == "1"


def test_another_database_error_is_not_disguised(app, monkeypatch):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(analysis_service, "group_list", broken)
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as lenient:
        assert lenient.get("/groups").status_code == 500


def test_the_page_only_reads_nothing_is_written(client, conn, world):
    before = [tuple(r) for r in conn.execute("SELECT * FROM groups ORDER BY id")]

    get(client, f"?rifle_id={world['alpha'].id}")

    assert [tuple(r) for r in conn.execute("SELECT * FROM groups ORDER BY id")] == before


# ----------------------------------------------------------------------------------- nav and code


PAGES = ["/", "/rifles", "/groups", "/analysis", "/compare", "/dope", "/settings"]


@pytest.mark.parametrize("path", PAGES)
def test_the_groups_link_is_in_the_nav_of_every_page_and_active_only_on_its_own(client, path):
    html = client.get(path).text

    nav = re.search(r"<nav\b.*?</nav>", html, re.S)[0]
    assert '<a href="/groups"' in nav and ">Groups</a>" in nav
    active = '<a href="/groups" class="active" aria-current="page">Groups</a>' in nav
    assert active == (path == "/groups")


def test_the_nav_puts_groups_after_rifles(client):
    nav = re.search(r"<nav\b.*?</nav>", client.get("/").text, re.S)[0]

    hrefs = re.findall(r'href="([^"]+)"', nav)
    assert hrefs.index("/groups") == hrefs.index("/rifles") + 1


def test_the_script_is_a_csp_clean_module_that_never_writes(client):
    html = get(client)
    source = (WEB / "static" / "js" / "groups_list.js").read_text()

    assert '<script type="module" src="/static/js/groups_list.js"></script>' in html
    assert not re.search(r"\bfetch\s*\(|apiSend|apiGet|XMLHttpRequest|\.submit\(", source)
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(", source)
    assert not re.search(r"\sstyle\s*=|\son[a-z]+\s*=|javascript:", html)
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"<style", html)
    assert client.get("/static/js/groups_list.js").status_code == 200


def test_the_page_has_no_native_post_form_and_one_get_form(client):
    html = get(client)

    forms = re.findall(r"<form\b[^>]*>", html)
    assert forms == ['<form method="get" action="/groups" data-form="group-filters" class="stack">']


def test_the_route_reads_through_services_only():
    source = (WEB / "routes" / "groups_page.py").read_text()

    assert "rga.db" not in source and "repo_" not in source
    assert "from rga.services import analysis as service" in source


def test_no_forbidden_metric_or_group_reduction_text_is_on_the_page(client, world):
    html = get(client)

    assert not re.search(r"mean\s*radius|best[\s-]*(of|n)\b|group\s*reduc", html, re.IGNORECASE)


# ----------------------------------------------------------------------------------- services


def test_the_service_filters_and_pages_like_the_page(conn, world):
    page = analysis_service.group_list(conn, analysis_service.GroupListFilters(distance_yd=100))

    assert page.total == 5 and page.pages == 1 and page.page == 1 and page.page_size == 100
    assert len(page.rows) == 5
    assert analysis_service.group_list(conn, page=0).page == 1
    assert analysis_service.group_list(conn, page=5).rows == []


def test_the_repo_orders_newest_first_and_filters_by_every_column(conn, world):
    listed = repo_groups.list_groups(conn, newest_first=True)

    keys = [(g.shot_date, g.shot_time or "", g.id) for g in listed]
    assert keys == sorted(keys, reverse=True)
    assert [g.id for g in repo_groups.list_groups(conn)] == sorted(g.id for g in listed)
    assert {g.id for g in repo_groups.list_groups(conn, suppressor=True)} == {
        world["ids"]["a2"],
        world["ids"]["a3"],
    }
    assert {g.id for g in repo_groups.list_groups(conn, suppressor_id=world["can_a"].id)} == {
        world["ids"]["a2"]
    }
    assert repo_groups.list_groups(conn, date_from="2027-01-01") == []


# ----------------------------------------------------------------------------------- Chromium


def _playwright_ready() -> bool:
    browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""))
    return browsers.is_dir() and any(browsers.glob("chromium*"))


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
def test_chromium_scrolls_the_list_narrows_the_loads_and_opens_a_group_from_its_row() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    with run_live_server() as server, sync_api.sync_playwright() as playwright:
        settings = Settings(data_dir=server.data_dir, port=server.port)
        db = connect(settings.db_path)
        try:
            alpha, beta = seed_rifle(db, name="Alpha"), seed_rifle(db, name="Beta")
            a_load = seed_load(db, alpha, label="Alpha Load")
            b_load = seed_load(db, beta, label="Beta Load")
            for n in range(40):
                make(
                    db,
                    alpha,
                    a_load,
                    date=f"2026-09-{(n % 28) + 1:02d}",
                    time=f"{n % 24:02d}:00:00",
                    offsets=OFFSETS[:3],
                )
            target = make(db, beta, b_load, date="2026-10-05", offsets=OFFSETS[:3], notes="open me")
        finally:
            db.close()
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1300, "height": 700})
            problems: list[str] = []
            page.on("pageerror", lambda error: problems.append(str(error)))
            page.goto(f"{server}/groups")
            rows = page.locator("tr.clickable")
            assert rows.count() == 41
            assert rows.first.get_attribute("data-group-id") == str(target.id)
            rows.last.scroll_into_view_if_needed()

            panel = page.locator("details[data-filters-panel]")
            assert panel.get_attribute("open") is None
            assert not page.locator("#f-rifle").is_visible()
            assert page.locator("summary").inner_text() == "Filters"
            page.locator("summary").click()
            assert panel.get_attribute("open") is not None
            assert page.locator("#f-rifle").is_visible()

            page.select_option("#f-rifle", label="Beta")
            hidden = page.locator("#f-load option[disabled]")
            assert [o.inner_text() for o in hidden.all()] == ["Alpha Load"]
            page.select_option("#f-load", label="Beta Load")
            page.get_by_role("button", name="Apply filters").click()
            page.wait_for_url("**/groups?*")
            query = parse_qs(urlsplit(page.url).query)
            assert query == {"rifle_id": [str(beta.id)], "load_id": [str(b_load.id)]}
            assert page.locator("tr.clickable").count() == 1
            assert panel.get_attribute("open") is None, "the panel is closed again after Apply"
            assert page.locator("summary").inner_text() == "Filters (2 active)"
            assert page.locator("a[data-clear-active]").is_visible()
            page.locator("summary").click()
            assert page.locator("#f-rifle").input_value() == str(beta.id)

            page.locator("tr.clickable td").nth(2).click()
            page.wait_for_url(f"**/groups/{target.id}")
            assert page.locator("main h1").first.is_visible()

            page.goto(f"{server}/groups?rifle_id={beta.id}")
            assert page.locator("summary").inner_text() == "Filters (1 active)"
            page.locator("a[data-clear-active]").click()
            page.wait_for_url(f"{server}/groups")
            assert page.locator("tr.clickable").count() == 41
            assert page.locator("a[data-clear-active]").count() == 0

            page.goto(f"{server}/groups?rifle_id={beta.id}")
            page.locator("summary").click()
            page.get_by_role("link", name="Clear filters").click()
            page.wait_for_url(f"{server}/groups")
            assert page.locator("tr.clickable").count() == 41
        finally:
            browser.close()
        assert problems == []
