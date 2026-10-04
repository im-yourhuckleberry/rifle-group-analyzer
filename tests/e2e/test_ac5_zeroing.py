"""AC-5 (T4.6): the full zeroing flow through the real UI, Step 1 to a saved dot.

Groups are created and linked on the zeroing page; their shots are stored through the group
service (the photo and review journeys are AC-1 to AC-4). Starting the zero uses the harness's
`create_zero` until the rifle page has a screen for it.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from rga.domain.types import Offset
from tests.e2e.pages import App
from tests.e2e.zeroing_support import (
    DIGIT_BEARING_CORRECTION,
    LOT,
    page_text,
    ring,
    shoot,
    zero_world,
)

pytestmark = pytest.mark.e2e


def expect_flash(app: App, text: str) -> None:
    """The success message is still on screen once the page has finished re-rendering."""
    flash = app.page.locator("#flash")
    expect(flash).to_be_visible()
    expect(flash).to_have_text(text)
    assert app.flash() == text


def zero_row(app: App, zero_id: int):
    with app.db() as conn:
        return conn.execute("SELECT * FROM zeros WHERE id = ?", (zero_id,)).fetchone()


def group_rows(app: App):
    with app.db() as conn:
        return conn.execute("SELECT * FROM groups ORDER BY id").fetchall()


def test_new_setup_zero_runs_step_1_to_a_saved_dot(app: App) -> None:
    world = zero_world(app)
    zero_id = app.create_zero(world.rifle_id, world.load_id, LOT, "new_setup")
    assert [s for s in app.zeroing_steps() if s[1] == "current"][0][0].startswith("Step 1")

    step1 = app.zeroing_add_group(shots=3)
    shoot(app, step1, [Offset(0.36, 0.72), Offset(0.36, 0.72), Offset(0.36, 0.72)])
    app.goto(f"/zeroing/{zero_id}")
    advice = app.zeroing_advice()
    assert advice["correction"] == "Dial DOWN 0.2, LEFT 0.1"
    assert (
        app.page.locator("#dl-up").input_value(),
        app.page.locator("#dl-right").input_value(),
    ) == (
        "-0.2",
        "-0.1",
    )
    assert app.zeroing_groups() == [
        {"group": step1, "step": "Step 1 (on paper)", "status": "final", "shots": 3}
    ]

    app.zeroing_record_dialed(-0.2, -0.1)
    expect_flash(app, "Dialed correction recorded.")
    assert [s for s in app.zeroing_steps() if s[1] == "current"][0][0].startswith("Step 2: initial")

    initial = app.zeroing_add_group(shots=10)
    shoot(app, initial, ring(1.08, 0.72), finalize=True)
    app.goto(f"/zeroing/{zero_id}")
    assert app.zeroing_advice()["correction"] == "Dial DOWN 0.2, LEFT 0.3"
    assert app.zeroing_groups()[-1]["step"] == "Step 2 initial group"
    app.zeroing_record_dialed(-0.2, -0.3)
    expect_flash(app, "Dialed correction recorded.")
    assert [s for s in app.zeroing_steps() if s[1] == "current"][0][0].startswith("Step 2: confirm")

    confirm = app.zeroing_add_group(shots=10)
    shoot(app, confirm, ring(0.0, 0.0))
    app.goto(f"/zeroing/{zero_id}")
    overlay = app.zeroing_advice()
    assert "20" in overlay["stats"][0]
    suggestion, dot_field = app.zeroing_dot()
    assert suggestion.startswith("Suggested dot:") and "20-shot overlay" in suggestion
    assert float(dot_field) > 0

    app.zeroing_set_dot()
    expect_flash(app, "The zero is complete.")
    app.goto(f"/zeroing/{zero_id}")
    assert "complete" in app.zeroing_summary()
    steps = app.zeroing_steps()
    assert steps[-1][0] == "Zero check" and all(state for _, state in steps)
    row = zero_row(app, zero_id)
    assert row["state"] == "complete"
    assert row["dot_size_in"] == pytest.approx(float(dot_field))
    assert [g["purpose"] for g in group_rows(app)] == ["zero_step1", "zero_step2", "zero_confirm"]
    assert app.page.locator("#dot-size").is_disabled()


def test_a_five_shot_step_2_group_shows_shoot_more_and_no_correction_anywhere(app: App) -> None:
    world = zero_world(app)
    zero_id = app.create_zero(world.rifle_id, world.load_id, LOT, "existing")
    initial = app.zeroing_add_group(shots=10)

    shoot(app, initial, ring(1.08, 0.72, n=5), finalize=False)
    app.goto(f"/zeroing/{zero_id}")

    advice = app.zeroing_advice()
    assert advice["visible"] and "shoot more" in advice["message"].lower()
    assert advice["correction"] is None
    assert app.page.locator("#advice-poi").is_hidden()
    assert app.page.locator("#advice-correction").is_hidden()
    assert not DIGIT_BEARING_CORRECTION.search(page_text(app)), page_text(app)
    assert not DIGIT_BEARING_CORRECTION.search(advice["message"])
    for tile in advice["stats"]:
        assert "mil" not in tile.lower() and "high" not in tile and "right" not in tile
    detail = app.api("GET", f"/api/groups/{initial}")
    assert (detail["masked"], detail["aim"]) == (True, None)
    assert app.page.locator("#dl-up").input_value() == "0"
    assert app.page.locator("#dl-right").input_value() == "0"

    with app.console.allow(r"status of 409"):
        with app.page.expect_response(r"**/api/zeros/*/dialed") as sent:
            app.page.locator("#dl-save").click()
        assert sent.value.status == 409
        app.page.locator("#flash:not([hidden])").wait_for()
    assert "Dialed correction recorded" not in (app.flash() or "")
    assert zero_row(app, zero_id)["state"] == "step2_initial"

    shoot(app, initial, ring(1.08, 0.72, n=10), finalize=False)
    app.goto(f"/zeroing/{zero_id}")
    assert app.zeroing_advice()["correction"] == "Dial DOWN 0.2, LEFT 0.3"


def test_step_1_refuses_a_group_of_four_or_more_shots(app: App) -> None:
    world = zero_world(app)
    zero_id = app.create_zero(world.rifle_id, world.load_id, LOT, "new_setup")
    group_id = app.zeroing_add_group(shots=4)
    shoot(app, group_id, ring(0.5, 0.5, n=4), finalize=False)
    app.goto(f"/zeroing/{zero_id}")

    advice = app.zeroing_advice()
    assert advice["correction"] is None
    assert not DIGIT_BEARING_CORRECTION.search(page_text(app))
    with app.console.allow(r"status of 422"):
        app.page.locator("#dl-save").click()
        app.page.locator("#flash", has_text="Step 1 only takes a group of 1 to 3 shots").wait_for()
    assert zero_row(app, zero_id)["state"] == "step1"

    detach = app.page.locator("#groups-body button", has_text="Detach")
    detach.click()
    app.page.locator("#groups-empty").wait_for()
    expect_flash(app, f"Group #{group_id} was detached and is a general group again.")
    with app.db() as conn:
        row = conn.execute(
            "SELECT purpose, zero_id FROM groups WHERE id = ?", (group_id,)
        ).fetchone()
    assert (row["purpose"], row["zero_id"]) == ("general", None)

    with app.console.allow(r"status of 422"):
        refused = app.api(
            "POST", f"/api/zeros/{zero_id}/groups", {"group_id": group_id}, status=422
        )
    assert refused["code"] == "invalid_step1_shots"
    assert zero_row(app, zero_id)["state"] == "step1"


def test_the_zeroing_page_makes_100_yd_groups_with_the_forms_date_and_position(app: App) -> None:
    world = zero_world(app)
    zero_id = app.create_zero(world.rifle_id, world.load_id, LOT, "existing")
    app.page.locator("#add-group-card").wait_for()

    assert app.page.locator("#ag-distance").inner_text().strip() == "Distance: 100 yd"
    assert app.page.locator("#ag-distance").evaluate("e => e.tagName") != "INPUT"
    group = app.zeroing_add_group(
        shots=10, on="2026-08-02", time="07:45", position="prone_off_pack"
    )

    with app.db() as conn:
        row = conn.execute("SELECT * FROM groups WHERE id = ?", (group,)).fetchone()
    assert (row["distance_yd"], row["shot_date"], row["position"]) == (
        100,
        "2026-08-02",
        "prone_off_pack",
    )
    assert (row["shot_time"], row["shot_at_source"], row["zero_id"]) == (
        "07:45:00",
        "manual",
        zero_id,
    )


def test_a_group_that_is_not_100_yd_cannot_feed_a_zero(app: App) -> None:
    world = zero_world(app)
    zero_id = app.create_zero(world.rifle_id, world.load_id, LOT, "existing")
    far = app.create_group(
        app.group_context(world.rifle_id, distance_yd=300),
        lot=LOT,
        shots=10,
        weather={"weather_skipped": True},
    )

    with app.console.allow(r"status of 422"):
        refused = app.api(
            "POST", f"/api/zeros/{zero_id}/groups", {"group_id": far.group_id}, status=422
        )
    assert refused["code"] == "invalid_distance"
    with app.db() as conn:
        row = conn.execute("SELECT purpose, zero_id FROM groups").fetchone()
    assert (row["purpose"], row["zero_id"]) == ("general", None)
    assert group_rows(app)[0]["purpose"] == "general"
