"""AC-1 and AC-12 journey: a photographed group becomes statistics, and the weather prompt gates it.

Create a rifle (the cartridge fills the bullet diameter) and a load; on the add-group form a 300 yd
distance makes the weather prompt block adding the group until the temperature is entered. Upload `clean_circle1`, confirm the
1 inch circle, mark the aim, finalize, and read extreme spread and mean POI against the rendered
truth. Also: the prompt starts at 200 yd and not at 199, and the group page toggles.
"""

from __future__ import annotations

import math

import pytest

from rga.domain.types import Offset
from tests.e2e.pages import App, RenderedTarget, Stats
from tests.support import seed

pytestmark = pytest.mark.e2e

ES_TOLERANCE_IN = 0.05
MEAN_TOLERANCE_MIL = 0.02
MOA_PER_MIL = 3.43774677
DISTANCE_YD = 300


def truth_stats(target: RenderedTarget, group: int = 0) -> tuple[float, float, float, int]:
    """(extreme spread in, mean right in, mean up in, shots) of a rendered group, from the truth."""
    truth = target.truth
    indices = truth["groups"][group]["hole_indices"]
    holes = [truth["holes_in"][i] for i in indices]
    aim = truth["groups"][group]["aim_in"]
    spread = max(math.dist(a, b) for a in holes for b in holes)
    right = sum(h[0] for h in holes) / len(holes) - aim[0]
    up = -(sum(h[1] for h in holes) / len(holes) - aim[1])
    return spread, right, up, len(holes)


def assert_stats_match_truth(stats: Stats, target: RenderedTarget, distance_yd: int) -> None:
    spread, right, up, shots = truth_stats(target)
    assert stats.n == shots
    assert stats.es_in == pytest.approx(spread, abs=ES_TOLERANCE_IN)
    assert stats.es_moa == pytest.approx(stats.es_in / (distance_yd / 100 * 1.0471975), abs=0.01)
    mean = stats.mean_mil(distance_yd)
    assert mean is not None, "the mean point of impact is not shown"
    scale = 1000.0 / (distance_yd * 36.0)
    assert mean[0] == pytest.approx(right * scale, abs=MEAN_TOLERANCE_MIL)
    assert mean[1] == pytest.approx(up * scale, abs=MEAN_TOLERANCE_MIL)


def test_caliber_prefills_the_bullet_diameter_on_the_rifle_form(app: App) -> None:
    app.goto("/rifles")
    form = app.main.locator('form[data-form="new-rifle"]')
    assert form.locator("#bullet_diameter_in").input_value() == ""
    form.locator("#cartridge").fill("6.5 Creedmoor")
    form.locator("#cartridge").dispatch_event("change")
    assert float(form.locator("#bullet_diameter_in").input_value()) == pytest.approx(0.264)


def test_photo_to_stats_at_300_yd_with_the_weather_gate(app: App) -> None:
    rifle_id = app.create_rifle(name="AC1 rifle", cartridge="6.5 Creedmoor")
    app.create_load(rifle_id, label="AC1 load")
    ctx = app.group_context(rifle_id, distance_yd=DISTANCE_YD)

    app.open_rifle(rifle_id)
    app.set_group_distance(DISTANCE_YD)
    assert app.weather_required_visible()
    assert app.page.locator("#add-group").is_disabled()
    assert app.page.locator("#group-blocked").is_visible()
    app.group_form_weather(68, density_altitude_ft=1500, wind_mph=4, wind_clock=3)
    assert app.page.locator("#add-group").is_enabled()
    assert not app.page.locator("#group-blocked").is_visible()

    ref, target = app.photo_group_to_review(
        ctx,
        "clean_circle1",
        weather={"temperature_f": 68, "density_altitude_ft": 1500, "wind_mph": 4, "wind_clock": 3},
    )
    _, _, _, shots = truth_stats(target)
    assert app.review_counts() == (shots, shots), app.review_counter()
    assert not app.review_finalize_enabled(), "finalize must wait for the aim point"
    assert app.review_finalize_reason()

    app.review_mark_aim(truth_aim_px=target.truth["groups"][0]["aim_px"], photo_id=ref.photo_id)
    assert app.review_finalize_enabled()
    app.review_finalize()

    assert_stats_match_truth(app.read_stats(), target, DISTANCE_YD)
    assert app.group_toggle("photo") and app.group_layer_visible("photo")
    assert app.group_toggle("circle") and app.group_layer_visible("circle")
    assert not app.group_toggle("poi") and not app.group_layer_visible("poi")
    app.group_toggle("photo", on=False)
    assert not app.group_layer_visible("photo")
    rows = app.group_rows(rifle_id)
    assert [r["id"] for r in rows] == [ref.group_id]
    assert (rows[0]["distance_yd"], rows[0]["status"]) == (DISTANCE_YD, "final")
    with app.db() as conn:
        group = conn.execute("SELECT * FROM groups").fetchone()
    assert (group["temperature_f"], group["density_altitude_ft"]) == (68.0, 1500.0)
    assert (group["wind_mph"], group["wind_clock"], group["weather_skipped"]) == (4.0, 3, 0)


@pytest.mark.parametrize(("distance_yd", "prompted"), [(199, False), (200, True)])
def test_weather_prompt_starts_at_200_yd(app: App, distance_yd: int, prompted: bool) -> None:
    rifle_id = app.create_rifle()
    app.create_load(rifle_id)
    ctx = app.group_context(rifle_id, distance_yd=distance_yd)
    app.open_rifle(rifle_id)
    app.set_group_distance(distance_yd)
    assert app.weather_required_visible() is prompted
    assert app.page.locator("#weather-optional").is_visible() is not prompted
    assert app.page.locator("#add-group").is_disabled() is prompted
    assert app.page.locator("#group-blocked").is_visible() is prompted
    if prompted:
        app.group_form_skip_weather()
        assert app.page.locator("#add-group").is_enabled()
        assert not app.page.locator("#group-blocked").is_visible()
    group = app.add_group(ctx, shots=3)
    with app.db() as conn:
        row = conn.execute("SELECT * FROM groups WHERE id = ?", (group.group_id,)).fetchone()
    assert row["distance_yd"] == distance_yd
    assert bool(row["weather_skipped"]) is prompted


def _offsets(count: int) -> list[Offset]:
    return [
        Offset(0.5 + 0.4 * math.cos(i * 2.1), 0.3 + 0.4 * math.sin(i * 2.1)) for i in range(count)
    ]


def test_group_page_toggles_and_correction_gate(app: App) -> None:
    rifle_id = app.create_rifle()
    load_id = app.create_load(rifle_id)
    ctx = app.group_context(rifle_id, distance_yd=100)
    with app.seeding() as conn:
        few = seed.seed_group(conn, context=ctx, load=load_id, offsets=_offsets(5)).id
        ten = seed.seed_group(conn, context=ctx, load=load_id, offsets=_offsets(10)).id
        step1 = seed.seed_group(
            conn, context=ctx, load=load_id, offsets=_offsets(3), purpose="zero_step1"
        ).id

    app.open_group(few)
    assert app.group_toggle("circle") and app.group_layer_visible("circle")
    assert not app.group_toggle("poi") and not app.group_layer_visible("poi")
    app.group_toggle("poi", on=True)
    assert app.group_layer_visible("poi")
    app.group_toggle("circle", on=False)
    assert not app.group_layer_visible("circle")
    assert app.group_toggle_available("hitter") and not app.group_toggle("hitter")
    assert not app.group_layer_visible("hitter")
    app.group_toggle("hitter", on=True)
    assert app.group_layer_visible("hitter")
    assert "“Thing-hitter” circle" in app.page.locator(".plot-legend").inner_text()
    assert not app.group_toggle_available("correction")
    assert app.group_correction_reason()
    assert app.group_correction_text() is None

    app.open_group(ten)
    assert app.group_toggle_available("correction")
    assert app.group_correction_text() is None
    app.group_toggle("correction", on=True)
    assert "mil" in (app.group_correction_text() or "")
    assert app.group_info_card(open_=True)
    assert not app.group_info_card(open_=False)

    app.open_group(step1)
    assert app.group_toggle_available("correction"), "a step 1 group of 1 to 3 shots is offered one"
