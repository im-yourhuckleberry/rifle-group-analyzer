"""Reference page zoom and hand marking, driven through the real UI.

- The photo zooms (toolbar), and a mark placed while zoomed lands on the clicked image pixel.
- Marks keep a constant on-screen size at any zoom, so they never cover the point being marked.
- A placed mark can be dragged; it keeps the offset from where it was grabbed.
- Dragging empty space pans the zoomed photo and does not place a mark.
"""

from __future__ import annotations

import pytest

from tests.e2e.pages import App

pytestmark = pytest.mark.e2e

BULLET_IN = 0.224
MARK_SCREEN_PX = 5.0
ZOOM_PRESSES = 3
CLICK_TOLERANCE_PX = 1.5
DRAG_SCREEN = (40.0, 25.0)
GRAB_OFFSET_SCREEN = 3.0


def open_marking(app: App) -> tuple[int, int]:
    """A group with a reference-less photo, on the reference page in manual circle mode;
    returns the photo size in image px."""
    rifle_id = app.create_rifle(cartridge="223 Remington", bullet_diameter_in=BULLET_IN)
    app.create_load(rifle_id)
    ctx = app.group_context(rifle_id)
    target = app.render_target("no_reference")
    app.add_group(ctx, shots=len(target.truth["groups"][0]["hole_indices"]), photo=target)
    app.page.locator("#overlay").wait_for()
    app.page.locator("#kind").select_option("circle_1in")
    app.page.locator("#manual-toggle").click()
    return target.size_px


def marks(app: App) -> list[dict]:
    return app.page.evaluate(
        """() => [...document.querySelectorAll('#overlay > circle.manual')].map((c) => ({
          cx: Number(c.getAttribute('cx')), cy: Number(c.getAttribute('cy')), r: Number(c.getAttribute('r'))}))"""
    )


def css_per_image_px(app: App, width: int) -> float:
    return app.page.locator("#overlay").bounding_box()["width"] / width


def zoom_in(app: App, presses: int) -> float:
    for _ in range(presses):
        app.page.locator("#zoom-in-button").click()
    return float(app.page.locator("#zoom-level").inner_text().rstrip("×x").strip())


def test_a_mark_placed_while_zoomed_lands_on_the_clicked_pixel(app: App) -> None:
    width, height = open_marking(app)
    assert zoom_in(app, ZOOM_PRESSES) > 3
    point = (width / 2 + 30, height / 2 - 20)

    app.page.mouse.click(*app._overlay_client(*point))

    [mark] = marks(app)
    assert mark["cx"] == pytest.approx(point[0], abs=CLICK_TOLERANCE_PX)
    assert mark["cy"] == pytest.approx(point[1], abs=CLICK_TOLERANCE_PX)
    assert app.page.locator("#mark-count").inner_text() == "1"


def test_marks_keep_a_constant_on_screen_size(app: App) -> None:
    width, height = open_marking(app)
    app.page.mouse.click(*app._overlay_client(width / 2, height / 2))
    at_fit = marks(app)[0]["r"] * css_per_image_px(app, width)

    zoom_in(app, ZOOM_PRESSES)
    zoomed = marks(app)[0]["r"] * css_per_image_px(app, width)

    assert at_fit == pytest.approx(MARK_SCREEN_PX, rel=0.05)
    assert zoomed == pytest.approx(MARK_SCREEN_PX, rel=0.05)


def test_a_dragged_mark_keeps_its_grab_offset(app: App) -> None:
    width, height = open_marking(app)
    app.page.mouse.click(*app._overlay_client(width / 2, height / 2))
    before = marks(app)[0]
    scale = css_per_image_px(app, width)
    gx, gy = app._overlay_client(before["cx"], before["cy"])
    gx += GRAB_OFFSET_SCREEN

    app.page.mouse.move(gx, gy)
    app.page.mouse.down()
    app.page.mouse.move(gx + DRAG_SCREEN[0], gy + DRAG_SCREEN[1], steps=8)
    app.page.mouse.up()

    [after] = marks(app)
    tolerance = 0.6 / scale
    assert after["cx"] == pytest.approx(before["cx"] + DRAG_SCREEN[0] / scale, abs=tolerance)
    assert after["cy"] == pytest.approx(before["cy"] + DRAG_SCREEN[1] / scale, abs=tolerance)
    assert app.page.locator("#mark-count").inner_text() == "1"


def test_dragging_empty_space_pans_without_marking(app: App) -> None:
    width, height = open_marking(app)
    zoom_in(app, ZOOM_PRESSES)
    box = app.page.locator("#viewer").bounding_box()
    before = app.page.locator("#overlay").bounding_box()
    sx, sy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    app.page.mouse.move(sx, sy)
    app.page.mouse.down()
    app.page.mouse.move(sx + 80, sy + 50, steps=8)
    app.page.mouse.up()

    after = app.page.locator("#overlay").bounding_box()
    assert after["x"] == pytest.approx(before["x"] + 80, abs=1)
    assert after["y"] == pytest.approx(before["y"] + 50, abs=1)
    assert marks(app) == []
