"""Harness self-tests: the server, browser and `App` helpers work end to end.

Every nav page loads without JavaScript errors, the console collector really fails on an error,
and each helper family (rifle/load/group set-up, photo and reference, review editor, group page,
analysis, compare, dope) drives its screen. T4.2 onward builds the acceptance journeys on these.
"""

from __future__ import annotations

import math

import pytest

from rga.domain.types import Offset
from tests.e2e.pages import NAV_PATHS, App, ConsoleCollector
from tests.support import seed

pytestmark = pytest.mark.e2e


def test_every_nav_page_loads_without_console_errors(app: App) -> None:
    rifle_id = app.create_rifle()
    load_id = app.create_load(rifle_id)
    paths = [
        *NAV_PATHS,
        f"/rifles/{rifle_id}",
        f"/compare?rifle_id={rifle_id}",
        f"/dope?load_id={load_id}",
    ]
    for path in paths:
        app.goto(path)
        assert app.main.locator("h1").first.is_visible(), path
        assert app.page.title(), path
    assert app.nav_links(), "the header navigation is empty"
    app.console.assert_clean()


def test_console_collector_fails_on_a_page_error_and_can_allow_it(app: App) -> None:
    app.goto("/")
    app.page.evaluate("() => { setTimeout(() => { throw new Error('boom'); }, 0); }")
    app.page.wait_for_timeout(200)
    assert any("boom" in e for e in app.console.unexpected())
    with pytest.raises(AssertionError, match="boom"):
        app.console.assert_clean()
    app.console.clear()
    app.console.assert_clean()


def test_console_collector_records_failed_fetches_and_allow_scopes_them(app: App) -> None:
    app.goto("/")
    with app.console.allow(r"status of 404"):
        app.page.evaluate("async () => { await fetch('/api/no-such-thing'); }")
        app.page.wait_for_timeout(200)
    assert app.console.errors, "a 404 fetch is logged by the browser as a console error"
    app.console.assert_clean()
    app.page.evaluate("async () => { await fetch('/api/no-such-thing-either'); }")
    app.page.wait_for_timeout(200)
    assert len(app.console.unexpected()) == 1
    app.console.clear()


def test_rifle_load_and_group_helpers_return_real_ids(app: App) -> None:
    rifle_id = app.create_rifle(name="Harness rifle")
    load_id = app.create_load(rifle_id, label="Harness load")
    app.set_zero_load(rifle_id, "Harness load")
    ctx = app.group_context(rifle_id, distance_yd=100)
    with app.db() as conn:
        assert (
            conn.execute("SELECT name FROM rifles WHERE id=?", (rifle_id,)).fetchone()["name"]
            == "Harness rifle"
        )
        assert (
            conn.execute("SELECT label FROM loads WHERE id=?", (load_id,)).fetchone()["label"]
            == "Harness load"
        )
    group = app.add_group(ctx, lot="LOT-9", shots=3, notes="no photo")
    rows = app.group_rows(rifle_id)
    assert [r["id"] for r in rows] == [group.group_id] and "LOT-9" in rows[0]["load_lot"]
    assert (rows[0]["distance_yd"], rows[0]["day"], rows[0]["notes"]) == (
        100,
        ctx.date,
        "no photo",
    )
    with app.db() as conn:
        stored = conn.execute("SELECT * FROM groups WHERE id = ?", (group.group_id,)).fetchone()
    assert (stored["shot_date"], stored["distance_yd"], stored["shot_at_source"]) == (
        ctx.date,
        100,
        "manual",
    )


def test_photo_flow_reference_review_finalize_and_read_stats(app: App) -> None:
    rifle_id = app.create_rifle()
    app.create_load(rifle_id)
    ctx = app.group_context(rifle_id)
    ref, target = app.photo_group_to_review(ctx, "clean_circle1")
    expected_total = len(target.truth["groups"][0]["hole_indices"])
    shots, expected = app.review_counts()
    assert (shots, expected) == (expected_total, expected_total), app.review_counter()
    assert "Aim point is set" not in app.review_aim_status()
    aim = app.review_mark_aim(
        truth_aim_px=target.truth["groups"][0]["aim_px"], photo_id=ref.photo_id
    )
    assert "Aim" in aim
    app.review_save()
    app.review_finalize()
    stats = app.read_stats()
    holes = target.truth["holes_in"]
    indices = target.truth["groups"][0]["hole_indices"]
    truth_es = max(math.dist(holes[i], holes[j]) for i in indices for j in indices)
    assert stats.n == expected_total
    assert abs(stats.es_in - truth_es) < 0.05, (stats, truth_es)
    aim_in = target.truth["groups"][0]["aim_in"]
    truth_dx = sum(holes[i][0] for i in indices) / len(indices) - aim_in[0]
    truth_dy = -(sum(holes[i][1] for i in indices) / len(indices) - aim_in[1])
    assert stats.mean_dx_in == pytest.approx(truth_dx, abs=0.05)
    assert stats.mean_dy_in == pytest.approx(truth_dy, abs=0.05)


def test_manual_reference_confirms_from_clicks(app: App) -> None:
    rifle_id = app.create_rifle()
    app.create_load(rifle_id)
    ctx = app.group_context(rifle_id)
    target = app.render_target("clean_circle1")
    expected = len(target.truth["groups"][0]["hole_indices"])
    app.add_group(ctx, shots=expected, photo=target)
    app.confirm_reference("circle1", manual=app.circle_edge_points(target.truth))
    app.wait_path(r"/groups/\d+/review.*")
    app.review_wait_ready()
    assert app.review_counts()[1] == expected


def test_review_zoom_helpers_change_the_view_and_pointer_mapping_stays_exact(app: App) -> None:
    rifle_id = app.create_rifle()
    app.create_load(rifle_id)
    ctx = app.group_context(rifle_id)
    ref, target = app.photo_group_to_review(ctx, "clean_circle1")
    fit = app.review_view()
    level = app.review_zoom_to(2.0)
    zoomed = app.review_view()
    assert level >= 2.0 and zoomed["w"] < fit["w"]
    shots = app.review_shots()
    assert shots
    first = shots[0]
    cx, cy = app.working_to_client(first["cx"], first["cy"])
    hit = app.page.evaluate(
        "([x, y]) => { const e = document.elementFromPoint(x, y); return e ? e.getAttribute('class') : null; }",
        [cx, cy],
    )
    assert hit and "shot" in hit, hit
    app.review_zoom("fit")
    assert app.review_view()["w"] == pytest.approx(fit["w"])


def test_review_aim_suggestion_has_no_preselection_and_next_then_use_sets_it(app: App) -> None:
    rifle_id = app.create_rifle()
    app.create_load(rifle_id)
    ctx = app.group_context(rifle_id)
    app.photo_group_to_review(ctx, "clean_circle1")
    if not app.review_aim_suggestion_visible():
        pytest.skip("this scenario offers no aim suggestion")
    assert "Aim point is set" not in app.review_aim_status()
    app.review_use_suggested_aim()
    assert "Aim point is set" in app.review_aim_status()


def test_group_page_helpers_read_toggles_and_correction(app: App) -> None:
    rifle_id = app.create_rifle()
    load_id = app.create_load(rifle_id)
    ctx = app.group_context(rifle_id)
    with app.seeding() as conn:
        offsets = [Offset(0.3 * math.cos(i), 0.3 * math.sin(i)) for i in range(10)]
        group_id = seed.seed_group(conn, context=ctx, load=load_id, offsets=offsets).id
    app.open_group(group_id)
    assert app.group_toggle("circle") in (True, False)
    app.group_toggle("circle", on=True)
    assert app.group_layer_visible("circle")
    app.group_toggle("circle", on=False)
    assert not app.group_layer_visible("circle")
    assert isinstance(app.read_stats().n, int)


def test_analysis_compare_and_dope_pages_are_drivable(app: App) -> None:
    rifle_id = app.create_rifle()
    load_id = app.create_load(rifle_id)
    app.analysis_open()
    assert app.analysis_saved() == []
    app.compare_open(rifle_id)
    assert app.page.locator("#run-compare").is_visible()
    app.dope_open(load_id)
    assert app.dope_rows() == [] or isinstance(app.dope_rows()[0], list)


def test_a_fresh_console_collector_starts_clean(app: App) -> None:
    assert ConsoleCollector(app.page).unexpected() == []


def test_create_zero_drives_the_rifle_page_form_and_lists_the_zero(app: App) -> None:
    rifle_id = app.create_rifle()
    load_id = app.create_load(rifle_id)
    zero_id = app.create_zero(rifle_id, load_id, lot_code="LOT-Z", mode="existing")
    assert app.path() == f"/zeroing/{zero_id}"
    rows = app.zero_rows(rifle_id)
    assert [r["id"] for r in rows] == [zero_id] and "LOT-Z" in rows[0]["cells"]
    api_id = app.create_zero(rifle_id, load_id, lot_code=None, via_api=True)
    assert api_id != zero_id


def test_compare_run_returns_the_new_results_not_the_previous_ones(app: App) -> None:
    rifle_id = app.create_rifle()
    load_a = app.create_load(rifle_id, label="Alpha load")
    load_b = app.create_load(rifle_id, label="Bravo load")
    app.compare_open(rifle_id)
    only_a = app.compare_run(load_ids=[load_a])
    both = app.compare_run(load_ids=[load_a, load_b])
    again_a = app.compare_run(load_ids=[load_a])
    assert "Bravo load" not in only_a and "Bravo load" in both and "Bravo load" not in again_a
    assert "Alpha load" in only_a and "Alpha load" in both


def test_settings_page_mode_panel_is_a_card_with_visible_choices(app: App) -> None:
    app.goto("/settings")
    panel = app.main.locator(".mode-panel")
    assert "site-header" not in (panel.get_attribute("class") or "")
    current = panel.locator(".mode-option.current")
    assert current.locator("input").is_checked()
    border = current.evaluate("e => getComputedStyle(e).borderTopColor")
    assert border != "rgba(255, 255, 255, 0.4)"
    background = panel.evaluate("e => getComputedStyle(e).backgroundColor")
    assert background in ("rgba(0, 0, 0, 0)", "rgb(255, 255, 255)"), "not the green header"
    accent = current.locator("input").evaluate("e => getComputedStyle(e).accentColor")
    assert accent == "rgb(59, 93, 58)"
    assert panel.locator(".mode-option.unavailable input").is_disabled()


def test_group_api_back_door_creates_groups_that_carry_their_own_context(app: App) -> None:
    rifle_id = app.create_rifle()
    app.create_load(rifle_id)
    near = app.group_context(rifle_id, on="2026-09-30", time="08:15:00")
    far = app.group_context(
        rifle_id,
        distance_yd=300,
        on="2026-10-01",
        position="prone_off_pack",
        suppressor_name="Omega",
    )

    first = app.create_group(near, lot="LOT-A", shots=3, notes="cold bore")
    second = app.create_group(far, lot="LOT-B", shots=5, weather={"weather_skipped": True})

    one = app.api("GET", f"/api/groups/{first.group_id}")
    two = app.api("GET", f"/api/groups/{second.group_id}")
    assert (one["shot_date"], one["shot_time"], one["distance_yd"], one["shot_at_source"]) == (
        "2026-09-30",
        "08:15:00",
        100,
        "manual",
    )
    assert (two["distance_yd"], two["position"], two["suppressor"], two["suppressor_name"]) == (
        300,
        "prone_off_pack",
        True,
        "Omega",
    )
    assert two["weather_skipped"] is True and one["notes"] == "cold bore"
    assert app.api("GET", f"/api/rifles/{rifle_id}/group-defaults")["source_group_id"] == (
        second.group_id
    )
