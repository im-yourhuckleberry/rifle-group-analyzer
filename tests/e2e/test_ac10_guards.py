"""AC-10 (T4.11): the guards a shooter runs into, driven through the real UI.

- A photo with no printed reference offers no proposal, so the reference must be marked by hand
  before the editor will detect anything.
- The one way to remove a shot is "Not a bullet hole"; Finalize stays off at 9 of 10 and the
  server refuses it too.
- Two groups share one multi-aim photo, each fenced by its own region (no rejection is ever needed to clear the other groups' holes); the aim starts on the circle center
  and each group's own aim is clicked by hand (T4.0a).

Rifles use a .224 bullet because the synthetic targets are shot with one; the default 6.5 Creedmoor
diameter (.264) would size the detector's holes wrongly for these photos.
"""

from __future__ import annotations

import re

import numpy as np
import pytest

from tests.e2e.pages import App
from tests.synth.generate import plane_to_image

pytestmark = pytest.mark.e2e

BULLET_IN = 0.224
ONE_INCH_CIRCLE_POINTS = 4
REGION_TOLERANCE_PX = 20.0


def start(app: App) -> int:
    """A .224 rifle, a load and a 100 yd context; returns the context."""
    rifle_id = app.create_rifle(cartridge="223 Remington", bullet_diameter_in=BULLET_IN)
    app.create_load(rifle_id)
    return app.group_context(rifle_id)


def group_row(app: App, group_id: int):
    with app.db() as conn:
        return conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()


def shots_of(app: App, group_id: int):
    with app.db() as conn:
        return conn.execute(
            "SELECT x_in, y_in FROM shots WHERE group_id = ? ORDER BY seq", (group_id,)
        ).fetchall()


# ------------------------------------------------------------------ a photo with no reference


def test_a_photo_without_a_reference_needs_a_manual_one_before_detect(app: App) -> None:
    ctx = start(app)
    target = app.render_target("no_reference")
    expected = len(target.truth["groups"][0]["hole_indices"])
    ref = app.add_group(ctx, shots=expected, photo=target)

    assert app.find_references("circle1") == 0
    assert app.page.locator("#manual-panel").is_visible(), (
        "nothing found: switch to marking by hand"
    )
    assert "Nothing was found automatically" in app.page.locator("#warnings").inner_text()
    assert app.page.locator("#reference-next").is_disabled()
    assert app.find_references("grid") == 0
    assert "/reference" in app.path()

    app.goto(f"/groups/{ref.group_id}/review")
    app.page.locator("#editor-svg, #review-notices").first.wait_for()
    assert "not confirmed" in app.page.locator("#review-notices").inner_text()
    assert app.page.locator("#detect-button").is_disabled()
    refused = app.api("POST", f"/api/groups/{ref.group_id}/detect", {}, status=409)
    assert refused["code"] == "reference_unconfirmed"
    with app.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM shots").fetchone()[0] == 0

    app.goto(f"/photos/{ref.photo_id}/reference?group_id={ref.group_id}")
    width, height = target.size_px
    radius = 0.5 * target.truth["ppi"]
    marks = [
        (
            width / 2 + radius * np.cos(2 * np.pi * k / ONE_INCH_CIRCLE_POINTS),
            height / 2 + radius * np.sin(2 * np.pi * k / ONE_INCH_CIRCLE_POINTS),
        )
        for k in range(ONE_INCH_CIRCLE_POINTS)
    ]
    app.confirm_reference("circle1", manual=marks)
    app.wait_path(r"/groups/\d+/review.*")
    app.review_wait_ready()
    assert app.review_step() == "aim"
    app.review_mark_aim(truth_aim_px=target.truth["groups"][0]["aim_px"], photo_id=ref.photo_id)

    assert app.page.locator("#detect-button").is_enabled()
    assert app.review_counts()[1] == expected
    assert app.review_counts()[0] > 0


# ----------------------------------------------------------------------- removing and finalizing


def test_the_only_delete_control_is_not_a_bullet_hole(app: App) -> None:
    ctx = start(app)
    ref, _ = app.photo_group_to_review(ctx, "clean_circle1")
    shots, expected = app.review_counts()

    assert app.page.locator("#delete-button").is_disabled()
    app.review_select_shot(0)
    assert app.page.locator("#delete-button").inner_text().strip() == "Not a bullet hole"
    assert app.main.locator("button.danger:visible").all_inner_texts() == ["Not a bullet hole"]
    assert (
        app.main.get_by_role(
            "button", name=re.compile(r"delete|remove|discard|clear", re.I)
        ).count()
        == 0
    )
    assert app.main.locator("aside select, aside input[type=radio]").count() == 0

    app.review_delete_selected()
    assert app.review_counts() == (shots - 1, expected)
    app.review_save()
    with app.db() as conn:
        rows = conn.execute(
            "SELECT reason FROM detection_rejections WHERE group_id = ?", (ref.group_id,)
        ).fetchall()
    assert [r["reason"] for r in rows] == ["not_a_bullet_hole"]


def test_finalize_is_off_at_nine_of_ten_and_on_again_at_ten(app: App) -> None:
    ctx = start(app)
    app.render_target("ragged_10x7")
    ref, target = app.photo_group_to_review(ctx, "ragged_10x7")
    assert app.review_counts() == (10, 10)
    app.review_mark_aim(truth_aim_px=target.truth["groups"][0]["aim_px"], photo_id=ref.photo_id)
    assert app.review_finalize_enabled(), app.review_finalize_reason()

    removed = app.review_shots()[0]
    app.review_select_shot(0)
    app.review_delete_selected()

    assert app.review_counts() == (9, 10)
    assert not app.review_finalize_enabled()
    assert "add 1 missing shot(s)" in app.review_finalize_reason()
    app.review_save()
    refused = app.api("POST", f"/api/groups/{ref.group_id}/finalize", status=409)
    assert refused["code"] == "count_mismatch"
    assert group_row(app, ref.group_id)["status"] == "draft"

    app.review_click(removed["cx"], removed["cy"])
    assert app.review_counts() == (10, 10)
    assert app.review_finalize_enabled(), app.review_finalize_reason()
    app.review_finalize()
    assert app.read_stats().n == 10
    assert group_row(app, ref.group_id)["status"] == "final"


# --------------------------------------------------------------- two groups on one photo (P-08)


def truth_holes_working(app: App, target, photo_id: int, group: int) -> list[tuple[float, float]]:
    truth = target.truth
    plane = np.array([truth["holes_in"][i] for i in truth["groups"][group]["hole_indices"]])
    pixels = plane_to_image(truth, plane)
    return app.truth_to_working([(float(x), float(y)) for x, y in pixels], photo_id)


def test_two_groups_share_one_photo_each_fenced_by_its_region_and_aims_are_never_preselected(
    app: App,
) -> None:
    ctx = start(app)
    target = app.render_target("multi_dot_target")
    groups = target.truth["groups"]
    made: list[tuple[int, int]] = []
    for k in (0, 1):
        ref = app.add_group(ctx, shots=len(groups[k]["hole_indices"]), photo=target)
        if k == 0:
            app.confirm_reference("circle1", candidate="truth", truth=target.truth)
        else:
            # The same photo again: the first group's saved shots fix its scale, so Next goes on.
            assert app.page.locator("#locked-scale").is_visible()
            app.page.locator("#reference-next").click()
        app.wait_path(r"/groups/\d+/review.*")
        app.review_wait_ready()
        group_id = int(app.wait_path(r"/groups/(\d+)/review")[1])
        x0, y0, x1, y1 = groups[k]["roi_px"]
        region = app.truth_to_working([(x0, y0), (x1, y1)], ref.photo_id)
        own = app.truth_to_working([tuple(groups[k]["aim_px"])], ref.photo_id)[0]

        # The aim comes first, alone: no shot is drawn until it is confirmed.
        assert app.review_step() == "aim"
        assert app.review_shots() == [], "no shot is shown before the aim is confirmed"
        assert group_row(app, group_id)["aim_x_in"] is None
        # The aim starts on the circle's center (not saved until confirmed); each group's own aim
        # is placed by hand, exactly where it is clicked, which switches to "Mark it by hand".
        assert app.review_aim_mode() == "center"
        assert app.page.locator("#editor-svg circle.aim-candidate").count() == 0
        app.review_mark_aim(own, confirm=False)
        assert app.review_aim_mode() == "hand"
        app.review_confirm_aim()
        assert app.review_counts()[0] > 5, "the whole-photo guess proposes every group's holes"
        # The editor has no region control; the group's region is set through the API the
        # editor's Detect uses, then the editor is reopened.
        roi = {
            "x0": int(region[0][0]),
            "y0": int(region[0][1]),
            "x1": int(region[1][0]),
            "y1": int(region[1][1]),
        }
        app.api("POST", f"/api/groups/{group_id}/detect", {"roi_px": roi})
        app.page.reload()
        app.review_wait_ready()
        app.review_detect()
        assert app.review_counts() == (5, 5), app.review_counter()
        app.review_save()
        app.review_finalize(expect_url=r"/(?:groups|rifles)/\d+(?:\?.*)?$")
        made.append((group_id, ref.photo_id))

    with app.db() as conn:
        photo_ppi = conn.execute("SELECT working_ppi FROM photos").fetchone()[0]
        assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 1
    assert made[0][1] == made[1][1]
    rois = []
    for k, (group_id, photo_id) in enumerate(made):
        row = group_row(app, group_id)
        assert row["status"] == "final" and row["photo_id"] == photo_id
        rois.append(row["roi_json"])
        own_holes = np.array(truth_holes_working(app, target, photo_id, k))
        other_holes = np.array(truth_holes_working(app, target, photo_id, 1 - k))
        stored = np.array([[s["x_in"], s["y_in"]] for s in shots_of(app, group_id)]) * photo_ppi
        assert len(stored) == 5
        for shot in stored:
            assert np.min(np.hypot(*(own_holes - shot).T)) < 0.1 * photo_ppi, (k, shot)
            assert np.min(np.hypot(*(other_holes - shot).T)) > 0.3 * photo_ppi, (k, shot)
        aim = target.truth["groups"][k]["aim_px"]
        aim_working = np.array(app.truth_to_working([tuple(aim)], photo_id)[0])
        assert (
            np.hypot(
                row["aim_x_in"] * photo_ppi - aim_working[0],
                row["aim_y_in"] * photo_ppi - aim_working[1],
            )
            < 0.05 * photo_ppi
        )
    assert rois[0] != rois[1] and all(rois)
    with app.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM detection_rejections").fetchone()[0] == 0
