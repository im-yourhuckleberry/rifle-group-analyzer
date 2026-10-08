"""Finalize confirmation, press feedback, appearance and type, driven through the real UI.

- Finalize asks first: Cancel and Escape leave the group a draft; "Finalize group" locks it.
- Buttons respond on press (scale down) with motion on, and do not move under reduced motion.
- A dragged shot shows the grabbing cursor only while it is dragged.
- No stylesheet fixes the root font size, so the browser's text-size setting applies.
- Dark appearance and more-contrast change the palette; the plot's zoom controls are a blurred
  translucent material that turns solid under reduced transparency.
"""

from __future__ import annotations

import pytest

from tests.e2e.pages import App
from tests.e2e.test_review_editing import open_editor

pytestmark = pytest.mark.e2e


def computed(app: App, selector: str, prop: str) -> str:
    return app.page.evaluate(
        "([s, p]) => getComputedStyle(document.querySelector(s)).getPropertyValue(p).trim()",
        [selector, prop],
    )


def ready_to_finalize(app: App) -> int:
    group_id = open_editor(app)
    shots, _ = app.review_counts()
    app.review_set_expected(shots)
    shot = app.review_shots()[0]
    app.review_mark_aim((shot["cx"] + 40, shot["cy"] + 40))
    assert app.review_finalize_enabled(), app.review_finalize_reason()
    return group_id


def status(app: App, group_id: int) -> str:
    with app.db() as conn:
        return conn.execute("SELECT status FROM groups WHERE id = ?", (group_id,)).fetchone()[0]


def test_finalize_asks_before_it_locks_the_group(app: App) -> None:
    group_id = ready_to_finalize(app)
    box = app.page.locator("#finalize-confirm")
    assert box.is_hidden()

    app.page.locator("#finalize-button").click()
    assert box.is_visible()
    assert "can no longer be edited" in box.inner_text()
    app.page.locator("#finalize-cancel-button").click()
    assert box.is_hidden()
    assert status(app, group_id) == "draft"

    app.page.locator("#finalize-button").click()
    app.page.keyboard.press("Escape")
    assert box.is_hidden()
    assert status(app, group_id) == "draft"

    app.review_finalize()
    assert status(app, group_id) == "final"


@pytest.mark.motion
def test_a_button_scales_down_while_pressed(app: App) -> None:
    open_editor(app)
    button = app.page.locator("#zoom-fit-button")
    button.hover()
    app.page.mouse.down()
    app.page.wait_for_timeout(150)
    pressed = computed(app, "#zoom-fit-button", "transform")
    app.page.mouse.up()
    assert pressed.startswith("matrix(0.97")


def test_a_pressed_button_does_not_move_under_reduced_motion(app: App) -> None:
    open_editor(app)
    app.page.locator("#zoom-fit-button").hover()
    app.page.mouse.down()
    pressed = computed(app, "#zoom-fit-button", "transform")
    app.page.mouse.up()
    assert pressed == "none"


def test_a_dragged_shot_shows_the_grabbing_cursor_only_while_dragged(app: App) -> None:
    open_editor(app)
    shot = app.review_shots()[0]
    sx, sy = app.working_to_client(shot["cx"], shot["cy"])
    svg = app.page.locator("#editor-svg")

    app.page.mouse.move(sx, sy)
    app.page.mouse.down()
    app.page.mouse.move(sx + 20, sy + 10, steps=4)
    assert "is-grabbing" in svg.get_attribute("class")
    assert computed(app, "#editor-svg circle.shot", "cursor") == "grabbing"
    app.page.mouse.up()
    assert "is-grabbing" not in (svg.get_attribute("class") or "")


def test_the_root_font_size_follows_the_browser_setting(app: App) -> None:
    app.goto("/")
    fixed = app.page.evaluate(
        """() => [...document.styleSheets].flatMap((sheet) => [...sheet.cssRules])
          .filter((rule) => rule.selectorText === 'html' && rule.style.fontSize).length"""
    )
    assert fixed == 0


def test_dark_appearance_and_more_contrast_change_the_palette(app: App) -> None:
    app.goto("/")
    light_bg = computed(app, "body", "background-color")
    light_line = app.page.evaluate("() => getComputedStyle(document.documentElement).getPropertyValue('--line').trim()")

    app.page.emulate_media(color_scheme="dark")
    assert computed(app, "body", "background-color") == "rgb(20, 24, 22)"
    assert computed(app, "body", "color") == "rgb(231, 235, 232)"
    assert computed(app, "body", "background-color") != light_bg

    app.page.emulate_media(color_scheme="light", contrast="more")
    more_line = app.page.evaluate("() => getComputedStyle(document.documentElement).getPropertyValue('--line').trim()")
    assert more_line != light_line
    assert more_line == "#5f6862"


def test_plot_zoom_controls_are_a_material_that_turns_solid_under_reduced_transparency(app: App) -> None:
    group_id = ready_to_finalize(app)
    app.review_finalize()
    app.open_group(group_id)
    app.page.locator(".plot-zoom").wait_for()
    assert "blur(16px)" in computed(app, ".plot-zoom", "backdrop-filter")
    assert computed(app, ".plot-zoom", "background-color").startswith("rgba(")

    session = app.context.new_cdp_session(app.page)
    session.send(
        "Emulation.setEmulatedMedia",
        {"features": [{"name": "prefers-reduced-transparency", "value": "reduce"}]},
    )
    assert computed(app, ".plot-zoom", "backdrop-filter") == "none"
    assert computed(app, ".plot-zoom", "background-color") == "rgb(255, 255, 255)"
