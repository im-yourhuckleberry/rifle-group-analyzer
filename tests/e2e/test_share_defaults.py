"""B-77: the Settings page's Share section, driven in real Chromium against a live server."""

from __future__ import annotations

import json

import pytest

from tests.e2e.pages import App

pytestmark = pytest.mark.e2e

BUILT_IN = {
    "photo": True,
    "spread": True,
    "thing_hitter": False,
    "poi": False,
    "correction": False,
    "numbers": True,
}
LAYERS = list(BUILT_IN)


def boxes(app: App) -> dict[str, bool]:
    return app.page.evaluate(
        """() => Object.fromEntries([...document.querySelectorAll('[data-share-default]')]
          .map((b) => [b.dataset.shareDefault, b.checked]))"""
    )


def stored(app: App) -> list[tuple[str, str]]:
    with app.db() as conn:
        return [(r["key"], r["value"]) for r in conn.execute("SELECT key, value FROM settings")]


def test_the_share_section_starts_with_the_built_in_choice_and_the_mode_switch_stays(
    app: App,
) -> None:
    app.goto("/settings")

    assert boxes(app) == BUILT_IN
    assert list(boxes(app)) == LAYERS
    assert app.page.locator('input[name="ui-mode"]').count() == 2
    assert app.page.locator('input[value="online"]').is_disabled()
    assert stored(app) == []


def test_saving_stores_the_choice_and_it_survives_a_reload(app: App) -> None:
    app.goto("/settings")
    app.page.uncheck('[data-share-default="photo"]')
    app.page.check('[data-share-default="poi"]')
    app.page.check('[data-share-default="correction"]')

    app.page.click("#save-share-defaults")
    app.page.locator("#share-defaults-status", has_text="Saved").wait_for()

    expected = {**BUILT_IN, "photo": False, "poi": True, "correction": True}
    assert [(k, json.loads(v)) for k, v in stored(app)] == [("share_defaults", expected)]
    app.goto("/settings")
    assert boxes(app) == expected
    assert app.api("GET", "/api/settings/share-defaults") == expected


def test_changing_a_box_after_saving_clears_the_saved_message(app: App) -> None:
    app.goto("/settings")
    app.page.click("#save-share-defaults")
    app.page.locator("#share-defaults-status", has_text="Saved").wait_for()

    app.page.check('[data-share-default="poi"]')

    assert app.page.locator("#share-defaults-status").inner_text() == ""


def test_a_failed_save_shows_the_server_message_and_keeps_the_boxes(app: App) -> None:
    app.goto("/settings")
    app.page.route(
        "**/api/settings/share-defaults",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body=json.dumps({"detail": "The database is busy.", "code": "busy"}),
        ),
    )
    app.page.check('[data-share-default="poi"]')

    with app.console.allow("Failed to load resource"):
        app.page.click("#save-share-defaults")
        app.page.locator("#flash").wait_for(state="visible")

    assert "The database is busy." in (app.flash() or "")
    assert boxes(app)["poi"] is True
    assert app.page.locator("#share-defaults-status").inner_text() == ""
    assert app.page.locator("#save-share-defaults").is_enabled()
    assert stored(app) == []


def test_the_saved_choice_is_what_a_groups_share_card_opens_with(app: App) -> None:
    from rga.domain.types import Offset
    from tests.support import seed

    with app.seeding() as conn:
        rifle = seed.seed_rifle(conn)
        load = seed.seed_load(conn, rifle)
        group = seed.seed_group(
            conn,
            load=load,
            context=seed.seed_context(rifle),
            offsets=[Offset(0.1 * i - 0.5, 0.05 * i - 0.2) for i in range(10)],
        )
    app.goto("/settings")
    app.page.uncheck('[data-share-default="numbers"]')
    app.page.check('[data-share-default="thing_hitter"]')
    app.page.click("#save-share-defaults")
    app.page.locator("#share-defaults-status", has_text="Saved").wait_for()

    app.goto(f"/groups/{group.id}")
    app.page.click("#share-open")
    opened = app.page.evaluate(
        """() => Object.fromEntries([...document.querySelectorAll('[data-share-layer]')]
          .map((b) => [b.dataset.shareLayer, b.checked]))"""
    )

    assert opened == {
        "photo": False,
        "spread": True,
        "thing_hitter": True,
        "poi": False,
        "correction": False,
        "numbers": False,
    }, "photo is saved on but this group has none, so it stays off"


def test_the_boxes_show_what_the_server_says_was_saved(app: App) -> None:
    app.goto("/settings")
    reply = {**BUILT_IN, "photo": False, "correction": True}
    app.page.route(
        "**/api/settings/share-defaults",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(reply)
        ),
    )

    app.page.click("#save-share-defaults")
    app.page.locator("#share-defaults-status", has_text="Saved").wait_for()

    assert boxes(app) == reply
