"""Zoom motion, aim placement and busy feedback in the review editor, driven through the real UI.

With motion on (`@pytest.mark.motion`):
- A zoom button animates from the view on screen to the exact target, then rests.
- Quick repeated presses compound from each target (three presses land at 1.5^3).
- A flick keeps coasting after release and comes to rest inside the photo.
- Panning past an edge rubber-bands (the view follows less than the pointer) and springs back.
With the default reduced-motion context:
- A zoom button lands in one step and a released pan does not coast.
Aim mode places the aim on release and pans on drag; a slow detect labels its button and marks
the photo busy until the response arrives.
"""

from __future__ import annotations

import re

import pytest

from tests.e2e.pages import App
from tests.e2e.test_review_editing import open_editor

pytestmark = pytest.mark.e2e

REST = "#editor-svg:not([data-animating])"
MOVING = "#editor-svg[data-animating]"


def view(app: App) -> dict:
    return app.review_view()


def settle(app: App) -> dict:
    app.page.locator(REST).wait_for()
    return view(app)


def photo_width(app: App) -> float:
    return app.page.evaluate(
        "() => Number(document.querySelector('#editor-svg image').getAttribute('width'))"
    )


def drag(app: App, dx: float, dy: float, steps: int) -> None:
    v = view(app)
    sx, sy = v["left"] + v["width"] * 0.3, v["top"] + v["height"] * 0.3
    app.page.mouse.move(sx, sy)
    app.page.mouse.down()
    app.page.mouse.move(sx + dx, sy + dy, steps=steps)


@pytest.mark.motion
def test_a_zoom_button_animates_to_its_exact_target(app: App) -> None:
    open_editor(app)
    width = photo_width(app)
    start = settle(app)

    app.page.locator("#zoom-in-button").click()
    app.page.locator(MOVING).wait_for()
    end = settle(app)

    assert end["w"] == pytest.approx(width / 1.5, rel=1e-6)
    assert end["x"] + end["w"] / 2 == pytest.approx(start["x"] + start["w"] / 2, abs=0.5)


@pytest.mark.motion
def test_quick_presses_compound_from_each_target(app: App) -> None:
    open_editor(app)
    width = photo_width(app)
    for _ in range(3):
        app.page.locator("#zoom-in-button").click()
    end = settle(app)
    assert width / end["w"] == pytest.approx(1.5**3, rel=1e-6)


@pytest.mark.motion
def test_a_flick_coasts_and_rests_inside_the_photo(app: App) -> None:
    open_editor(app)
    width = photo_width(app)
    for _ in range(2):
        app.page.locator("#zoom-in-button").click()
    before = settle(app)
    scale = before["width"] / before["w"]

    drag(app, -60, 0, steps=4)
    released = view(app)
    app.page.mouse.up()
    app.page.locator(MOVING).wait_for()
    rest = settle(app)

    assert released["x"] == pytest.approx(before["x"] + 60 / scale, abs=1)
    assert rest["x"] > released["x"] + 5 / scale, "the view keeps moving after release"
    assert 0 <= rest["x"] <= width - rest["w"] + 1e-6


@pytest.mark.motion
def test_panning_past_an_edge_rubber_bands_and_springs_back(app: App) -> None:
    open_editor(app)
    fit = settle(app)
    scale = fit["width"] / fit["w"]

    drag(app, 120, 0, steps=20)
    pulled = view(app)
    assert pulled["x"] < fit["x"], "the view follows past the edge"
    assert fit["x"] - pulled["x"] < 120 / scale * 0.9, "but less than the pointer moved"
    app.page.wait_for_timeout(150)
    app.page.mouse.up()
    rest = settle(app)
    assert rest["x"] == pytest.approx(fit["x"], abs=1e-6)


def test_reduced_motion_zooms_in_one_step_and_does_not_coast(app: App) -> None:
    open_editor(app)
    width = photo_width(app)

    app.page.locator("#zoom-in-button").click()
    assert app.page.locator(MOVING).count() == 0
    zoomed = view(app)
    assert zoomed["w"] == pytest.approx(width / 1.5, rel=1e-6)
    scale = zoomed["width"] / zoomed["w"]

    drag(app, -60, 0, steps=4)
    app.page.mouse.up()
    assert app.page.locator(MOVING).count() == 0
    assert view(app)["x"] == pytest.approx(zoomed["x"] + 60 / scale, abs=1)


def test_aim_mode_pans_on_drag_and_places_on_release(app: App) -> None:
    rifle_id = app.create_rifle()
    app.create_load(rifle_id)
    app.photo_group_to_review(app.group_context(rifle_id), "clean_circle1", aim=False)
    assert app.review_step() == "aim"
    app.review_zoom("in")
    before = view(app)

    drag(app, -50, -30, steps=6)
    app.page.mouse.up()
    assert view(app)["x"] != before["x"], "a drag in aim mode pans"
    assert app.review_aim_mode() == "center", "a pan leaves the aim on the circle's center"

    v = view(app)
    app.page.mouse.click(v["left"] + v["width"] / 2, v["top"] + v["height"] / 2)
    app.page.locator("#aim-status", has_text="Aim point is set where you clicked").wait_for()
    assert app.review_aim_mode() == "hand"


def test_a_slow_detect_labels_its_button_and_marks_the_photo_busy(app: App) -> None:
    open_editor(app)
    held = []
    app.page.route(re.compile(r"/api/groups/\d+/detect$"), lambda route: held.append(route))

    app.page.locator("#detect-button").click()
    app.page.locator("#detect-button", has_text="Detecting holes…").wait_for()
    assert app.page.locator("#editor-wrap").get_attribute("aria-busy") == "true"
    assert app.page.locator("#detect-button").is_disabled()

    assert len(held) == 1
    held[0].continue_()
    app.page.locator("#detect-button:enabled").wait_for()
    assert app.page.locator("#detect-button").inner_text().strip() == "Detect holes"
    assert app.page.locator("#editor-wrap").get_attribute("aria-busy") == "false"


def test_the_aim_goes_exactly_where_clicked_and_a_drag_on_it_moves_it_without_snapping(
    app: App,
) -> None:
    rifle_id = app.create_rifle()
    app.create_load(rifle_id)
    app.photo_group_to_review(app.group_context(rifle_id), "clean_circle1", aim=False)
    snaps: list[str] = []
    app.page.on("request", lambda r: snaps.append(r.url) if "snap-aim" in r.url else None)
    app.review_zoom("fit")

    def aim_px() -> tuple[float, float]:
        mark = app.page.locator("#editor-svg circle.aim-mark")
        return float(mark.get_attribute("cx")), float(mark.get_attribute("cy"))

    target = (120.0, 140.0)
    app.review_click(*target)
    placed = aim_px()
    per_px = app.review_view()["w"] / app.review_view()["width"]
    assert abs(placed[0] - target[0]) <= per_px and abs(placed[1] - target[1]) <= per_px

    sx, sy = app.working_to_client(*placed)
    grab = (sx + 3, sy + 2)
    app.page.mouse.move(*grab)
    app.page.mouse.down()
    app.page.mouse.move(grab[0] + 40, grab[1] + 25, steps=8)
    app.page.mouse.up()

    moved = aim_px()
    assert moved[0] - placed[0] == pytest.approx(40 * per_px, abs=per_px)
    assert moved[1] - placed[1] == pytest.approx(25 * per_px, abs=per_px)
    assert "dropped" in app.review_aim_status()
    assert snaps == [], "the aim never snaps to the printed mark"
