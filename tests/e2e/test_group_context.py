"""B-66 journeys: a group carries its own context (date, time, distance, position, suppressor,
weather), shown and edited on the rifle page and the group page.

Photo date pre-fill versus manual entry, previous-group pre-fill, the 199/200 yd weather gate on
the group form and the context card, and the distance lock on a group that is part of a zero.
"""

from __future__ import annotations

import io
import math

import pytest
from PIL import Image
from playwright.sync_api import expect

from rga.domain.types import Offset
from tests.e2e.pages import App
from tests.e2e.zeroing_support import LOT, shoot, zero_world
from tests.support import seed

pytestmark = pytest.mark.e2e

PHOTO_DATE = "2026:08:15 09:30:45"
PHOTO_OFFSET = "-06:00"


def photo_file(app: App, name: str, taken: str | None, offset: str | None = None):
    """A small JPEG, with an Exif capture time when `taken` is given, written under tmp_path."""
    exif = Image.Exif()
    sub = exif.get_ifd(0x8769)
    if taken is not None:
        sub[0x9003] = taken
    if offset is not None:
        sub[0x9011] = offset
    exif[0x0112] = 1
    buffer = io.BytesIO()
    Image.effect_noise((320, 240), 40).convert("RGB").save(buffer, "JPEG", exif=exif)
    path = app.tmp_path / name
    path.write_bytes(buffer.getvalue())
    return path


def stored_group(app: App, group_id: int):
    with app.db() as conn:
        return conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()


def ring(count: int = 10) -> list[Offset]:
    return [
        Offset(0.5 + 0.3 * math.cos(i * 2.1), 0.2 + 0.3 * math.sin(i * 2.1)) for i in range(count)
    ]


def world(app: App) -> tuple[int, int]:
    rifle_id = app.create_rifle()
    return rifle_id, app.create_load(rifle_id)


# ------------------------------------------------------------------------------ photo date


def test_a_photo_with_a_capture_time_fills_the_date_and_time_and_the_group_keeps_them(
    app: App,
) -> None:
    rifle_id, _ = world(app)
    app.open_rifle(rifle_id)

    source = app.group_form_photo(photo_file(app, "dated.jpg", PHOTO_DATE, PHOTO_OFFSET))

    assert source == "from photo"
    assert app.page.input_value("#g-date") == "2026-08-15"
    assert app.page.input_value("#g-time") == "09:30"
    app.page.locator("#g-lot").fill(LOT)
    app.page.locator("#add-group").click()
    app.wait_path(r"/photos/\d+/reference\?group_id=\d+")
    group = int(app.page.url.split("group_id=")[1].split("&")[0])

    row = stored_group(app, group)
    assert row["shot_date"] == "2026-08-15"
    assert row["shot_time"] is not None and row["shot_time"].startswith("09:30")
    assert row["shot_at_source"] == "photo"
    assert row["utc_offset"] == PHOTO_OFFSET
    app.open_group(group)
    assert "2026-08-15" in app.group_header() and "(from photo)" in app.group_header()


def test_typing_a_different_date_after_the_photo_makes_the_group_entered_not_from_photo(
    app: App,
) -> None:
    rifle_id, _ = world(app)
    app.open_rifle(rifle_id)
    app.group_form_photo(photo_file(app, "dated.jpg", PHOTO_DATE, PHOTO_OFFSET))

    app.page.locator("#g-date").fill("2026-08-01")
    app.page.locator("#g-lot").fill(LOT)
    app.page.locator("#add-group").click()
    app.wait_path(r"/photos/\d+/reference\?group_id=\d+")
    group = int(app.page.url.split("group_id=")[1].split("&")[0])

    row = stored_group(app, group)
    assert (row["shot_date"], row["shot_at_source"]) == ("2026-08-01", "manual")
    app.open_group(group)
    assert "(entered)" in app.group_header() and "(from photo)" not in app.group_header()


def test_a_photo_without_a_capture_time_leaves_the_date_to_the_shooter(app: App) -> None:
    rifle_id, _ = world(app)
    app.open_rifle(rifle_id)
    today = app.page.input_value("#g-date")

    source = app.group_form_photo(photo_file(app, "undated.jpg", None))

    assert source == "entered (the photo has no date)"
    assert app.page.input_value("#g-date") == today
    app.page.locator("#g-date").fill("2026-07-04")
    app.page.locator("#g-lot").fill(LOT)
    app.page.locator("#add-group").click()
    app.wait_path(r"/photos/\d+/reference\?group_id=\d+")
    group = int(app.page.url.split("group_id=")[1].split("&")[0])
    row = stored_group(app, group)
    assert (row["shot_date"], row["shot_time"], row["shot_at_source"]) == (
        "2026-07-04",
        None,
        "manual",
    )


def test_a_manually_entered_group_has_its_date_time_and_source_on_the_list_and_header(
    app: App,
) -> None:
    rifle_id, _ = world(app)
    ctx = app.group_context(rifle_id, on="2026-06-10", time="16:20:00", position="standing_field")

    group = app.add_group(ctx, lot=LOT, shots=4)

    row = next(r for r in app.group_rows(rifle_id) if r["id"] == group.group_id)
    assert (row["day"], row["time"], row["distance_yd"]) == ("2026-06-10", "16:20", 100)
    assert "Standing" in row["position"]
    stored = stored_group(app, group.group_id)
    assert (stored["shot_time"], stored["shot_at_source"]) == ("16:20:00", "manual")
    app.open_group(group.group_id)
    header = app.group_header()
    assert "2026-06-10" in header and "16:20" in header and "(entered)" in header


# ------------------------------------------------------------------------- previous-group pre-fill


def test_the_next_group_starts_from_the_previous_one_and_weather_only_for_the_same_day(
    app: App,
) -> None:
    rifle_id, _ = world(app)
    ctx = app.group_context(
        rifle_id,
        distance_yd=300,
        on="2026-09-30",
        position="prone_off_pack",
        suppressor_name="Omega",
    )
    app.add_group(
        ctx, lot="LOT-PREV", shots=7, weather={"temperature_f": 70, "wind_mph": 4, "wind_clock": 3}
    )

    app.open_rifle(rifle_id)
    page = app.page
    assert page.input_value("#g-distance") == "300"
    assert page.input_value("#g-position") == "prone_off_pack"
    assert page.is_checked("#g-suppressor")
    assert page.locator("#g-suppressor-name option:checked").inner_text() == "Omega"
    assert page.input_value("#g-lot") == "LOT-PREV" and page.input_value("#g-shots") == "7"
    assert page.input_value("#w-temp") == ""

    page.fill("#g-date", "2026-09-30")
    expect(page.locator("#w-temp")).to_have_value("70")
    assert page.input_value("#w-wind") == "4" and page.input_value("#w-clock") == "3"
    assert page.is_enabled("#add-group")

    page.fill("#g-date", "2026-10-01")
    expect(page.locator("#w-temp")).to_have_value("")
    assert page.is_disabled("#add-group") and page.is_visible("#group-blocked")


def test_a_rifle_with_no_group_borrows_distance_position_and_suppressor_from_the_latest_group(
    app: App,
) -> None:
    first_rifle, _ = world(app)
    app.add_group(
        app.group_context(
            first_rifle, distance_yd=100, position="kneeling_sitting", suppressor=True
        ),
        lot="LOT-A",
        shots=5,
    )
    second_rifle, _ = world(app)

    app.open_rifle(second_rifle)

    page = app.page
    assert page.input_value("#g-distance") == "100"
    assert page.input_value("#g-position") == "kneeling_sitting"
    assert page.is_checked("#g-suppressor")
    assert page.input_value("#g-lot") == "" and page.input_value("#g-shots") != ""


# ------------------------------------------------------------------------------- weather gate


@pytest.mark.parametrize(("distance_yd", "gated"), [(199, False), (200, True)])
def test_the_weather_gate_on_the_group_form_starts_at_200_yd(
    app: App, distance_yd: int, gated: bool
) -> None:
    rifle_id, _ = world(app)
    app.open_rifle(rifle_id)
    app.set_group_distance(distance_yd)

    assert app.weather_required_visible() is gated
    assert app.add_group_enabled() is not gated
    assert app.page.locator("#group-blocked").is_visible() is gated
    assert app.page.locator("#weather-panel").get_attribute("open") is not None or not gated
    if gated:
        app.page.fill("#w-temp", "55")
        assert app.add_group_enabled()
        app.page.fill("#w-temp", "")
        assert not app.add_group_enabled()
        app.group_form_skip_weather()
        assert app.add_group_enabled()
        app.page.fill("#w-temp", "55")
        assert app.page.locator("#weather-skipped-badge").is_hidden()
    group = app.add_group_from_form(lot=LOT, shots=3)
    row = stored_group(app, group)
    assert row["distance_yd"] == distance_yd
    assert (row["temperature_f"] is not None) is gated


def test_the_context_card_needs_weather_from_200_yd_and_saves_it(app: App) -> None:
    rifle_id, _ = world(app)
    group = app.create_group(app.group_context(rifle_id, distance_yd=100), shots=3).group_id
    app.open_group(group)

    app.edit_group_context(distance_yd=200, save=False)
    app.page.locator("#c-save").click()
    app.page.locator("#flash:not([hidden])").wait_for()
    assert stored_group(app, group)["distance_yd"] == 100
    assert "weather" in (app.flash() or "").lower()

    app.edit_group_context(temperature_f=61)
    row = stored_group(app, group)
    assert (row["distance_yd"], row["temperature_f"]) == (200, 61.0)

    app.edit_group_context(distance_yd=100)
    assert stored_group(app, group)["distance_yd"] == 100


def test_changing_the_distance_warns_and_recomputes_the_mil_values(app: App) -> None:
    rifle_id, load_id = world(app)
    with app.seeding() as conn:
        group = seed.seed_group(
            conn,
            context=app.group_context(rifle_id, distance_yd=100),
            load=load_id,
            offsets=ring(),
        ).id
    with app.db() as conn:
        before = [r["dx_mil"] for r in conn.execute("SELECT dx_mil FROM shots ORDER BY id")]
    app.open_group(group)
    assert app.page.locator("#c-distance-warning").is_hidden()

    app.page.locator("#c-distance").fill("200")
    app.page.locator("#c-distance").dispatch_event("input")
    expect(app.page.locator("#c-distance-warning")).to_be_visible()
    app.edit_group_context(skip_weather=True)

    with app.db() as conn:
        after = [r["dx_mil"] for r in conn.execute("SELECT dx_mil FROM shots ORDER BY id")]
    assert [round(a * 2, 6) for a in after] == [round(b, 6) for b in before]


# ------------------------------------------------------------------------ distance lock on a zero


def test_a_group_that_is_part_of_a_zero_cannot_change_its_distance_but_can_change_the_rest(
    app: App,
) -> None:
    w = zero_world(app)
    zero_id = app.create_zero(w.rifle_id, w.load_id, LOT, "existing")
    group = app.zeroing_add_group(shots=10)
    shoot(app, group, ring(), finalize=False)

    app.open_group(group)
    assert app.page.locator("#c-distance").is_disabled()
    assert app.page.locator("#c-distance-locked").is_visible()
    assert "zero" in app.page.locator("#c-distance-locked").inner_text().lower()
    with app.console.allow(r"status of 409"):
        refused = app.api("PATCH", f"/api/groups/{group}", {"distance_yd": 200}, status=409)
    assert refused["code"] == "group_linked_to_zero"
    assert stored_group(app, group)["distance_yd"] == 100

    app.edit_group_context(position="kneeling_sitting")
    row = stored_group(app, group)
    assert (row["position"], row["distance_yd"], row["zero_id"]) == (
        "kneeling_sitting",
        100,
        zero_id,
    )

    app.goto(f"/zeroing/{zero_id}")
    app.page.locator("#groups-body button", has_text="Detach").click()
    app.page.locator("#groups-empty").wait_for()
    app.open_group(group)
    assert app.page.locator("#c-distance").is_enabled()
    assert app.page.locator("#c-distance-locked").count() == 0 or (
        not app.page.locator("#c-distance-locked").is_visible()
    )


def test_a_zero_check_group_has_its_distance_locked_too(app: App) -> None:
    from tests.e2e.zeroing_support import complete_zero

    w = zero_world(app)
    zero_id, _ = complete_zero(app, w)
    app.goto(f"/zeroing/{zero_id}")
    check = app.zeroing_add_group(shots=2)

    app.open_group(check)

    assert app.page.locator("#c-distance").is_disabled()
    assert app.page.locator("#c-distance-locked").is_visible()
    with app.console.allow(r"status of 409"):
        refused = app.api("PATCH", f"/api/groups/{check}", {"distance_yd": 300}, status=409)
    assert refused["code"] == "group_linked_to_zero"
