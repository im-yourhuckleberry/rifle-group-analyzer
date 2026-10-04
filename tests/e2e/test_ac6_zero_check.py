"""AC-6 (T4.7): the zero check through the real UI.

The check group is created on the zeroing page; its shots are stored through the group service
(photo import and review are covered by AC-1 to AC-4). Starting and completing the zero reuses
the page flow of AC-5 via `complete_zero`.
"""

from __future__ import annotations

import pytest

from rga.domain.types import Offset
from tests.e2e.pages import App
from tests.e2e.zeroing_support import (
    DIGIT_BEARING_CORRECTION,
    complete_zero,
    page_text,
    shoot,
    zero_world,
)

pytestmark = pytest.mark.e2e

BULLET_IN = 0.264


def group_row(app: App, group_id: int):
    with app.db() as conn:
        return conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()


def reload_zero(app: App, zero_id: int) -> None:
    app.goto(f"/zeroing/{zero_id}")
    app.page.locator("#stepper li").first.wait_for()


def test_two_shots_inside_the_dot_pass_and_the_group_is_masked_the_whole_time(app: App) -> None:
    world = zero_world(app)
    zero_id, dot = complete_zero(app, world)
    assert app.page.locator("#check-card").is_visible()

    check = app.zeroing_add_group(shots=2)

    row = group_row(app, check)
    assert (row["purpose"], row["zero_id"], row["status"]) == ("zero_check", zero_id, "draft")
    assert app.api("GET", f"/api/groups/{check}")["masked"] is True

    inside = dot / 4
    shoot(app, check, [Offset(inside, 0.0), Offset(0.0, -inside)])
    detail = app.api("GET", f"/api/groups/{check}")
    assert detail["masked"] is True and detail["aim"] is None
    app.open_group(check)
    stats = app.read_stats()
    assert stats.n == 2 and stats.mean_dx_in is None and stats.poi_text is None
    assert app.group_correction_text() is None
    assert not DIGIT_BEARING_CORRECTION.search(page_text(app)), page_text(app)
    assert any(
        r["id"] == check and "zero check" in r["label"] for r in app.group_rows(world.rifle_id)
    )

    reload_zero(app, zero_id)
    result = app.zeroing_run_check(check)
    assert result["passed"] is True
    assert result["message"].startswith("Zero check passed")
    assert result["route"] == []
    assert app.zeroing_check_history() == [f"Group #{check}: pass. {result['message']}"]
    assert group_row(app, check)["purpose"] == "zero_check"
    assert app.page.locator("#ck-run").is_disabled()
    assert app.page.locator("#ck-hint").inner_text().startswith("No finalized 1 or 2 shot group")


def test_a_shot_outside_the_dot_is_a_possible_lost_zero_with_the_mount_then_scope_route(
    app: App,
) -> None:
    world = zero_world(app)
    zero_id, dot = complete_zero(app, world)
    check = app.zeroing_add_group(shots=1)
    shoot(app, check, [Offset(dot / 2 + BULLET_IN / 2 + 1.0, 0.0)])

    reload_zero(app, zero_id)
    result = app.zeroing_run_check(check)

    assert result["passed"] is False
    assert result["message"].startswith("Possible lost zero")
    mount, scope = result["message"].index("mount"), result["message"].index("scope")
    assert mount < scope
    assert len(result["route"]) == 3
    assert "mount" in result["route"][0] and "scope" in result["route"][1]
    assert "Step 2" in result["route"][2]
    assert app.zeroing_check_history() == [
        f"Group #{check}: possible lost zero. {result['message']}"
    ]
    assert not DIGIT_BEARING_CORRECTION.search(page_text(app)), page_text(app)
    assert app.page.locator("#advice-poi").is_hidden()


def test_a_shot_exactly_touching_the_dot_passes_and_a_hair_outside_fails(app: App) -> None:
    world = zero_world(app)
    zero_id, dot = complete_zero(app, world)
    limit = dot / 2 + BULLET_IN / 2
    touching = app.zeroing_add_group(shots=1)
    shoot(app, touching, [Offset(limit - 0.01, 0.0)])
    beyond = app.zeroing_add_group(shots=1)
    shoot(app, beyond, [Offset(limit + 0.05, 0.0)])

    reload_zero(app, zero_id)
    assert app.zeroing_run_check(touching)["passed"] is True
    assert app.zeroing_run_check(beyond)["passed"] is False
    assert len(app.zeroing_check_history()) == 2


def test_one_of_two_shots_outside_the_dot_fails_the_whole_check(app: App) -> None:
    world = zero_world(app)
    zero_id, dot = complete_zero(app, world)
    check = app.zeroing_add_group(shots=2)
    shoot(app, check, [Offset(0.0, 0.0), Offset(dot / 2 + BULLET_IN / 2 + 1.0, 0.0)])

    reload_zero(app, zero_id)
    result = app.zeroing_run_check(check)

    assert result["passed"] is False and result["message"].startswith("Possible lost zero")


def test_three_shots_are_not_a_zero_check(app: App) -> None:
    world = zero_world(app)
    zero_id, dot = complete_zero(app, world)
    before = len(app.api("GET", f"/api/zeros/{zero_id}")["groups"])

    app.page.locator("#add-group-card").wait_for()
    app.page.locator("#ag-shots").fill("3")
    with app.console.allow(r"status of 422"):
        with app.page.expect_response(r"**/api/zeros/*/check-groups") as sent:
            app.page.locator("#ag-create").click()
        assert sent.value.status == 422
        app.page.locator("#flash:not([hidden])").wait_for()
    assert app.page.locator("#ag-next").is_hidden()
    with app.db() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM groups WHERE purpose = 'zero_check'").fetchone()[0]
            == 0
        )

    far = app.add_group(app.group_context(world.rifle_id), lot="LOT-1", shots=3)
    shoot(app, far.group_id, [Offset(0.1, 0.0), Offset(0.0, 0.1), Offset(-0.1, 0.0)])
    reload_zero(app, zero_id)
    options = app.page.locator("#ck-group option").all_inner_texts()
    assert all(f"#{far.group_id}," not in option for option in options)
    with app.console.allow(r"status of 422"):
        refused = app.api(
            "POST", f"/api/zeros/{zero_id}/checks", {"group_id": far.group_id}, status=422
        )
    assert refused["code"] == "invalid_zero_check_shots"
    assert app.api("GET", f"/api/zeros/{zero_id}")["checks"] == []
    assert len(app.api("GET", f"/api/zeros/{zero_id}")["groups"]) == before
    assert group_row(app, far.group_id)["purpose"] == "general"
    assert dot > 0
