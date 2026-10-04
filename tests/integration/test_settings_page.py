"""T2: the Settings page (B-60, B-77): the display-only Offline/Online switch, then the Share section.

Offline is checked and current; Online is a disabled, unfocusable radio with the caption
"Online mode — coming later". The mode switch has no behaviour, route or stored setting; the only
stored setting is the share-card defaults (`/api/settings/share-defaults`, first stored in 006).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rga.config import Settings
from rga.web.app import create_app, discover_routers
from rga.web.schemas import ROUTE_SCHEMAS
from rga.web.security import SECURITY_HEADERS
from tests.unit.test_templates_shell import (
    NAV_HREFS,
    ONLINE_CAPTION,
    _header,
    _mode_switch,
    _radios,
    _start_tags,
    external_references,
)

ORIGIN = "http://127.0.0.1"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url=ORIGIN) as test_client:
        yield test_client


@pytest.fixture
def app_lenient(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def page(client: TestClient) -> str:
    response = client.get("/settings")
    assert response.status_code == 200
    return response.text


def test_the_page_renders_with_the_standard_security_headers(client: TestClient) -> None:
    response = client.get("/settings")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, name
    assert 'data-page="settings"' in response.text
    assert "<h1>Settings</h1>" in response.text
    assert "<title>Settings" in response.text


def test_offline_is_checked_and_shown_as_the_current_mode(page: str) -> None:
    radios = _radios(page)
    assert set(radios) == {"offline", "online"}
    offline = radios["offline"]
    assert "checked" in offline and "disabled" not in offline
    assert re.search(
        r'<label class="mode-option current"><input[^>]*value="offline"[^>]*> Offline</label>', page
    )


def test_online_is_disabled_unfocusable_and_explained(page: str) -> None:
    online = _radios(page)["online"]
    assert "disabled" in online
    assert "checked" not in online
    assert "tabindex" not in online
    description_id = online["aria-describedby"]
    assert description_id
    note = re.search(rf'<span id="{description_id}"[^>]*>(.*?)</span>', page, re.S)
    assert note and note.group(1) == ONLINE_CAPTION
    assert re.search(rf'<label class="mode-option unavailable" title="{ONLINE_CAPTION}">', page)


def test_the_switch_appears_once_inside_the_content_and_not_in_the_header(page: str) -> None:
    switch = _mode_switch(page)
    assert page.index("<main") < page.index(switch) < page.index("</main>")
    assert switch not in _header(page)
    assert page.count('name="ui-mode"') == 2


def test_the_switch_is_plain_markup_with_no_behaviour(page: str) -> None:
    fragment = _mode_switch(page)
    tags = _start_tags(fragment)
    assert {tag for tag, _ in tags} <= {"div", "label", "input", "span"}
    for tag, attrs in tags:
        forbidden = [
            name
            for name in attrs
            if name in {"href", "src", "action", "formaction", "style", "tabindex", "target"}
            or name.startswith(("on", "data-", "hx-"))
        ]
        assert forbidden == [], f"<{tag}> carries {forbidden}"
    assert [a["id"] for _, a in tags if "id" in a] == ["mode-online-note"]
    assert not re.search(r"<(?:form|a|button|script|style|iframe|img|link)\b", fragment, re.I)


def test_the_page_adds_only_its_own_module_script_and_no_inline_code_or_external_reference(
    page: str,
) -> None:
    references = set(re.findall(r'(?:href|src|action)="([^"]*)"', page))
    assert references == {"/static/css/app.css", "/static/js/settings.js", *NAV_HREFS}
    assert re.findall(r"<link\b[^>]*>", page) == [
        '<link rel="stylesheet" href="/static/css/app.css">'
    ]
    assert re.findall(r"<script\b[^>]*>", page) == [
        '<script type="module" src="/static/js/settings.js">'
    ]
    assert not re.search(r"<(?:form|style|iframe|img|object|embed)\b", page, re.I)
    assert not re.search(r"\sstyle\s*=", page, re.I)
    assert not re.search(r"\son[a-z]+\s*=", page, re.I)
    assert "javascript:" not in page.lower()
    assert external_references(page) == []


def test_the_nav_marks_settings_as_the_current_page_once(page: str) -> None:
    assert re.search(r'<a href="/settings" class="active" aria-current="page">Settings</a>', page)
    assert page.count('aria-current="page"') == 1


def test_the_page_is_read_only_and_no_api_route_exists_for_the_mode(client: TestClient) -> None:
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)("/settings")
        assert response.status_code == 405, (method, response.status_code)
        assert response.json()["code"] == "http_405"
    for path in ("/api/settings", "/api/mode", "/api/settings/mode", "/api/preferences"):
        for method in ("get", "put", "post"):
            response = getattr(client, method)(path)
            assert response.status_code in (404, 405), (method, path, response.status_code)
            assert response.json()["code"] in ("http_404", "http_405")


def test_only_the_page_and_the_share_defaults_routes_exist_for_settings() -> None:
    routes = {
        (route.path, tuple(sorted(route.methods - {"HEAD"})))
        for router in discover_routers()
        for route in router.routes  # type: ignore[attr-defined]
        if "settings" in route.path or "mode" in route.path
    }
    assert routes == {
        ("/settings", ("GET",)),
        ("/api/settings/share-defaults", ("GET",)),
        ("/api/settings/share-defaults", ("PUT",)),
    }
    assert {path for _, path in ROUTE_SCHEMAS if "settings" in path or "mode" in path} == {
        "/api/settings/share-defaults"
    }


def _stored(settings: Settings) -> list[tuple[str, str]]:
    with sqlite3.connect(settings.db_path) as conn:
        return [tuple(r) for r in conn.execute("SELECT key, value FROM settings ORDER BY key")]


def test_nothing_is_stored_for_the_mode_or_by_viewing_the_page(
    client: TestClient, settings: Settings
) -> None:
    assert _stored(settings) == []
    assert client.get("/settings").status_code == 200
    assert client.post("/settings", json={"mode": "online"}).status_code == 405
    assert _stored(settings) == []
    with sqlite3.connect(settings.db_path) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master")]
    assert not [n for n in names if "mode" in n or "pref" in n]


# --------------------------------------------------------------------------- the Share section

LAYERS = ("photo", "spread", "thing_hitter", "poi", "correction", "numbers")
BUILT_IN = {
    "photo": True,
    "spread": True,
    "thing_hitter": False,
    "poi": False,
    "correction": False,
    "numbers": True,
}
URL = "/api/settings/share-defaults"


def _share_boxes(html: str) -> dict[str, bool]:
    found = re.findall(r'<input type="checkbox" data-share-default="(\w+)"([^>]*)>', html)
    return {key: "checked" in rest for key, rest in found}


def test_the_share_section_lists_six_checkboxes_with_the_built_in_defaults(page: str) -> None:
    assert "<h2>Share</h2>" in page and 'id="save-share-defaults"' in page
    assert _share_boxes(page) == BUILT_IN
    assert list(_share_boxes(page)) == list(LAYERS)


def test_the_share_section_shows_the_saved_choice(client: TestClient) -> None:
    saved = {**BUILT_IN, "photo": False, "poi": True, "correction": True}
    assert client.put(URL, json=saved).status_code == 200

    assert _share_boxes(client.get("/settings").text) == saved


def test_get_returns_the_built_in_defaults_when_nothing_was_saved(client: TestClient) -> None:
    response = client.get(URL)

    assert response.status_code == 200 and response.json() == BUILT_IN


def test_put_round_trips_and_get_returns_what_was_saved(
    client: TestClient, settings: Settings
) -> None:
    body = {
        "photo": False,
        "spread": False,
        "thing_hitter": True,
        "poi": True,
        "correction": True,
        "numbers": False,
    }

    put = client.put(URL, json=body)

    assert put.status_code == 200 and put.json() == body
    assert client.get(URL).json() == body
    assert _stored(settings) == [("share_defaults", __import__("json").dumps(body))]


def test_put_replaces_the_whole_set_and_keeps_one_row(
    client: TestClient, settings: Settings
) -> None:
    client.put(URL, json={**BUILT_IN, "poi": True})
    client.put(URL, json=BUILT_IN)

    assert client.get(URL).json() == BUILT_IN
    assert len(_stored(settings)) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.pop("numbers"),
        lambda b: b.update(extra=True),
        lambda b: b.update(photo=1),
        lambda b: b.update(photo=0),
        lambda b: b.update(photo="true"),
        lambda b: b.update(photo=None),
        lambda b: b.update(spread="yes"),
        lambda b: b.update(poi=[True]),
        lambda b: b.clear(),
    ],
)
def test_put_rejects_missing_unknown_and_non_boolean_values_and_stores_nothing(
    client: TestClient, settings: Settings, mutate
) -> None:
    body = dict(BUILT_IN)
    mutate(body)

    response = client.put(URL, json=body)

    assert (response.status_code, response.json()["code"]) == (422, "validation_error")
    assert _stored(settings) == [] and client.get(URL).json() == BUILT_IN


@pytest.mark.parametrize("body", [[], "yes", 5, None])
def test_put_rejects_a_body_that_is_not_an_object(client: TestClient, body) -> None:
    assert client.put(URL, json=body).status_code == 422


def test_put_with_malformed_json_is_422_never_500(app_lenient: TestClient) -> None:
    response = app_lenient.put(
        URL, content=b"{not json", headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422 and response.json()["code"] == "validation_error"


def test_a_cross_origin_put_is_refused_and_changes_nothing(
    client: TestClient, settings: Settings
) -> None:
    response = client.put(URL, json=BUILT_IN, headers={"Origin": "http://evil.example"})

    assert (response.status_code, response.json()["code"]) == (403, "bad_origin")
    assert _stored(settings) == []


def test_a_non_json_put_is_refused_with_415(client: TestClient, settings: Settings) -> None:
    response = client.put(URL, content=b"photo=1", headers={"Content-Type": "text/plain"})

    assert response.status_code == 415 and _stored(settings) == []


def test_other_methods_on_the_share_defaults_route_are_405(client: TestClient) -> None:
    for method in ("post", "patch", "delete"):
        assert getattr(client, method)(URL).status_code == 405, method


def test_a_damaged_stored_row_never_breaks_reading(client: TestClient, settings: Settings) -> None:
    def store(text: str) -> None:
        with sqlite3.connect(settings.db_path) as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES ('share_defaults', ?, 't')"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (text,),
            )

    store('["not", "an", "object"]')
    assert client.get(URL).json() == BUILT_IN
    store('{"photo": false, "poi": "yes", "numbers": 0, "surprise": true}')
    expected = {**BUILT_IN, "photo": False}
    assert client.get(URL).json() == expected
    assert _share_boxes(client.get("/settings").text) == expected


def test_a_locked_database_is_503_busy_for_the_share_routes(
    app_lenient: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rga.services import settings as service

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service, "get_share_defaults", locked)
    monkeypatch.setattr(service, "set_share_defaults", locked)

    for response in (app_lenient.get(URL), app_lenient.put(URL, json=BUILT_IN)):
        assert (response.status_code, response.json()["code"]) == (503, "busy")
        assert response.headers["retry-after"] == "1"


def test_the_page_script_goes_through_api_js_only(client: TestClient) -> None:
    source = client.get("/static/js/settings.js").text

    assert (
        'from "/static/js/api.js"' in source
        and 'apiSend("PUT", "/api/settings/share-defaults"' in source
    )
    assert not re.search(
        r"\bfetch\s*\(|innerHTML|outerHTML|insertAdjacentHTML|document\.write", source
    )
    assert not re.search(r"https?://", source)
