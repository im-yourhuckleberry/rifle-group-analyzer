"""Review editor pointer behaviour, driven through the real UI.

- A click on empty space while a shot is selected only deselects; with nothing selected it adds.
- A dragged shot keeps the offset from where it was grabbed, so it moves by the pointer's travel
  instead of jumping its centre under the pointer.
- Undo (the button and Cmd/Ctrl+Z) takes back a removal, an added shot and a move, and an undone
  removal leaves no rejection behind when saved.
"""

from __future__ import annotations

import pytest

from tests.e2e.pages import App

pytestmark = pytest.mark.e2e

BULLET_IN = 0.224
GRAB_FRACTION = 0.8
DRAG_SCREEN = (60.0, -40.0)
POSITION_TOLERANCE_PX = 0.75


def open_editor(app: App) -> int:
    """A .224 rifle and a clean 1-inch-circle target in the review editor; returns the group id."""
    rifle_id = app.create_rifle(cartridge="223 Remington", bullet_diameter_in=BULLET_IN)
    app.create_load(rifle_id)
    ctx = app.group_context(rifle_id)
    ref, _ = app.photo_group_to_review(ctx, "clean_circle1")
    app.review_zoom("fit")
    return ref.group_id


def empty_point(app: App) -> tuple[float, float]:
    """A working-px point well away from every shot (the top-left corner region)."""
    shots = app.review_shots()
    reach = max(s["r"] for s in shots) * 4
    point = (reach, reach)
    assert all(abs(s["cx"] - point[0]) > reach or abs(s["cy"] - point[1]) > reach for s in shots)
    return point


def selected_count(app: App) -> int:
    return sum(1 for s in app.review_shots() if s["selected"])


def test_an_empty_click_deselects_before_it_adds(app: App) -> None:
    open_editor(app)
    shots, _ = app.review_counts()
    spot = empty_point(app)

    app.review_select_shot(0)
    assert selected_count(app) == 1
    app.review_click(*spot)
    assert selected_count(app) == 0
    assert app.review_counts()[0] == shots

    app.review_click(*spot)
    assert app.review_counts()[0] == shots + 1
    assert selected_count(app) == 0, "a new shot is not selected, so the next click adds again"
    app.review_click(spot[0] + 60, spot[1])
    assert app.review_counts()[0] == shots + 2


def test_a_dragged_shot_keeps_its_grab_offset(app: App) -> None:
    open_editor(app)
    shot = app.review_shots()[0]
    view = app.review_view()
    scale = view["width"] / view["w"]
    grab_x, grab_y = app.working_to_client(shot["cx"] + shot["r"] * GRAB_FRACTION, shot["cy"])

    app.page.mouse.move(grab_x, grab_y)
    app.page.mouse.down()
    app.page.mouse.move(grab_x + DRAG_SCREEN[0], grab_y + DRAG_SCREEN[1], steps=8)
    app.page.mouse.up()

    moved = app.review_shots()[0]
    assert moved["cx"] == pytest.approx(shot["cx"] + DRAG_SCREEN[0] / scale, abs=POSITION_TOLERANCE_PX)
    assert moved["cy"] == pytest.approx(shot["cy"] + DRAG_SCREEN[1] / scale, abs=POSITION_TOLERANCE_PX)


def test_undo_takes_back_remove_add_and_move(app: App) -> None:
    group_id = open_editor(app)
    undo = app.page.locator("#undo-button")
    original = app.review_shots()
    assert undo.is_disabled()

    app.review_select_shot(0)
    app.review_delete_selected()
    assert app.review_counts()[0] == len(original) - 1
    undo.click()
    assert app.review_shots() == [dict(s, selected=False) for s in original]
    assert undo.is_disabled()

    app.review_click(*empty_point(app))
    assert app.review_counts()[0] == len(original) + 1
    app.page.keyboard.press("ControlOrMeta+z")
    assert app.review_counts()[0] == len(original)

    app.review_drag_shot(0, 50, 50)
    assert app.review_shots()[0]["cx"] != original[0]["cx"]
    app.page.keyboard.press("ControlOrMeta+z")
    assert (app.review_shots()[0]["cx"], app.review_shots()[0]["cy"]) == (
        original[0]["cx"],
        original[0]["cy"],
    )

    app.review_save()
    with app.db() as conn:
        rejections = conn.execute(
            "SELECT COUNT(*) FROM detection_rejections WHERE group_id = ?", (group_id,)
        ).fetchone()[0]
    assert rejections == 0, "an undone removal must not be saved as a rejection"
