"""AC-4 journey: overlaying finalized groups on /analysis.

Three 10-shot groups aggregate to 30 shots with the extreme spread and combined center of the union
(expected values computed here from the seeded offsets). A group at another distance, a zeroing
group whose point of impact is hidden, and invalid saves are refused visibly. A combination can be
saved, listed, persisted across a reload and deleted. Groups are seeded with exact offsets; the
photo flow is covered by AC-1.
"""

from __future__ import annotations

import itertools
import math
import re

import numpy as np
import pytest
from playwright.sync_api import expect

from rga.domain.types import Offset, ShotOrigin
from tests.e2e.pages import App, GroupContext
from tests.support import seed

pytestmark = pytest.mark.e2e

MOA_IN_PER_100YD = 1.0471975512


def scatter(seed_value: int, n: int = 10, spread: float = 0.6) -> list[Offset]:
    rng = np.random.default_rng(seed_value)
    return [Offset(float(x), float(y)) for x, y in rng.normal(0.1, spread / 2, (n, 2))]


def extreme_spread(points: list[Offset]) -> float:
    return max(
        math.dist((a.dx_in, a.dy_in), (b.dx_in, b.dy_in))
        for a, b in itertools.combinations(points, 2)
    )


class World:
    """A rifle with two loads and contexts at 100 and 200 yd, seeded through the real repos."""

    def __init__(self, app: App) -> None:
        self.app = app
        self.rifle = app.create_rifle(name="Aggregate rifle")
        self.load_a = app.create_load(self.rifle, label="Load A")
        self.load_b = app.create_load(self.rifle, label="Load B")
        self.near = app.group_context(self.rifle, distance_yd=100, on="2026-09-01")
        self.far = app.group_context(self.rifle, distance_yd=200, on="2026-09-02")

    def group(self, ctx: GroupContext, load: int, offsets: list[Offset], **kw) -> int:
        with self.app.seeding() as conn:
            return seed.seed_group(conn, context=ctx, load=load, offsets=offsets, **kw).id


@pytest.fixture
def world(app: App) -> World:
    return World(app)


def tile(result: dict, name: str) -> tuple[str, str]:
    return result["tiles"][name]


def test_three_ten_shot_groups_overlay_to_thirty_shots_with_the_union_statistics(
    app: App, world: World
) -> None:
    sets = [scatter(1), scatter(2), scatter(3)]
    ids = [world.group(world.near, world.load_a, pts) for pts in sets]
    union = [o for pts in sets for o in pts]

    app.analysis_open()
    assert app.page.locator("#aggregate-button").is_disabled()
    app.analysis_pick(ids)
    result = app.analysis_aggregate()
    assert result is not None, app.flash()

    assert tile(result, "groups")[0] == "3" and tile(result, "shots")[0] == "30"
    es_in, es_moa = tile(result, "extreme spread")
    expected_es = extreme_spread(union)
    assert float(es_in.split()[0]) == pytest.approx(expected_es, abs=0.006)
    assert float(es_moa.split()[0]) == pytest.approx(expected_es / MOA_IN_PER_100YD, abs=0.006)
    words, offsets_text = tile(result, "combined center")
    match = re.match(r"(-?[\d.]+) in right, (-?[\d.]+) in up", offsets_text)
    assert match, offsets_text
    assert float(match[1]) == pytest.approx(np.mean([o.dx_in for o in union]), abs=0.006)
    assert float(match[2]) == pytest.approx(np.mean([o.dy_in for o in union]), abs=0.006)
    assert "mil" in words or words == "on the aim point"
    assert result["plot_shots"] == 30
    assert result["warnings"] == [] and result["badges"] == []
    note = app.page.locator("#aggregate-note").inner_text()
    assert "smallest target every shot hit (center-to-center)" in note and "3 group(s)" in note
    assert not re.search(r"mean\s*radius|best[\s-]*(of|n)\b", app.main.inner_text(), re.IGNORECASE)
    assert app.flash() is None
    app.console.assert_clean()


def test_a_group_at_another_distance_is_refused_with_a_visible_message(
    app: App, world: World
) -> None:
    near = world.group(world.near, world.load_a, scatter(4))
    far = world.group(world.far, world.load_a, scatter(5))
    app.analysis_open()
    app.analysis_pick([near, far])
    with app.console.allow(r"status of 422"):
        assert app.analysis_aggregate() is None
    message = app.flash()
    assert message and "100 yd" in message and "200 yd" in message
    assert not app.page.locator("#aggregate-result").is_visible()

    app.analysis_pick([near])
    result = app.analysis_aggregate()
    assert result is not None and tile(result, "shots")[0] == "10"
    assert app.flash() is None, "the error clears on the next successful aggregate"


def test_the_distance_filter_narrows_the_picker_to_one_distance(app: App, world: World) -> None:
    near = world.group(world.near, world.load_a, scatter(6))
    far = world.group(world.far, world.load_a, scatter(7))
    app.analysis_open()
    boxes = app.page.locator("#picker-body input[type=checkbox]")
    assert boxes.count() == 2
    app.page.locator("#filter-distance").select_option("200")
    expect(boxes).to_have_count(1)
    assert int(boxes.first.get_attribute("value")) == far
    app.page.locator("#filter-distance").select_option("")
    expect(boxes).to_have_count(2)
    assert near != far


@pytest.mark.parametrize(("purpose", "shots"), [("zero_check", 2), ("zero_step2", 5)])
def test_a_zeroing_group_with_a_hidden_point_of_impact_cannot_be_picked(
    app: App, world: World, purpose: str, shots: int
) -> None:
    masked = world.group(world.near, world.load_a, scatter(8, shots), purpose=purpose)
    open_group = world.group(world.near, world.load_a, scatter(9))
    app.analysis_open()
    box = app.page.locator(f'#picker-body input[value="{masked}"]')
    assert box.is_disabled()
    row = app.page.locator("#picker-body tr", has=app.page.locator(f'input[value="{masked}"]'))
    assert "cannot be combined" in row.inner_text()
    app.analysis_pick([masked, open_group])
    result = app.analysis_aggregate()
    assert result is not None and tile(result, "groups")[0] == "1"
    assert result["plot_shots"] == 10, "the masked group's shots are not in the overlay"


def test_groups_of_different_loads_combine_with_a_warning(app: App, world: World) -> None:
    ids = [
        world.group(world.near, world.load_a, scatter(10)),
        world.group(world.near, world.load_b, scatter(11)),
    ]
    app.analysis_open()
    app.analysis_pick(ids)
    result = app.analysis_aggregate()
    assert result is not None and tile(result, "shots")[0] == "20"
    assert result["warnings"] and "different loads" in result["warnings"][0]


def test_estimated_shots_show_the_includes_estimated_badge(app: App, world: World) -> None:
    origins = [ShotOrigin.DETECTED] * 9 + [ShotOrigin.ESTIMATED]
    group = world.group(world.near, world.load_a, scatter(12), origins=origins)
    app.analysis_open()
    app.analysis_pick([group])
    result = app.analysis_aggregate()
    assert result is not None and "includes estimated shots" in result["badges"]
    assert app.page.locator("#aggregate-plot circle.shot.estimated").count() == 1


def test_a_combination_is_saved_listed_persisted_and_deleted(app: App, world: World) -> None:
    ids = [world.group(world.near, world.load_a, scatter(20 + i)) for i in range(3)]
    app.analysis_open()
    assert app.page.locator("#saved-empty").is_visible()
    app.analysis_pick(ids)
    assert app.analysis_aggregate() is not None
    app.analysis_save_combination("Three of ten")
    [entry] = app.analysis_saved()
    assert "Three of ten" in entry and "3 groups, 30 shots" in entry and "at 100 yd" in entry
    assert "eligible for the 30-shot comparison" in entry
    assert not app.page.locator("#saved-empty").is_visible()

    app.analysis_open()
    assert [e for e in app.analysis_saved() if "Three of ten" in e], "persisted across a reload"
    app.analysis_delete_saved("Three of ten")
    assert app.analysis_saved() == []
    expect(app.page.locator("#saved-empty")).to_be_visible()


def test_saving_needs_a_name_two_groups_and_one_load(app: App, world: World) -> None:
    a1 = world.group(world.near, world.load_a, scatter(30))
    a2 = world.group(world.near, world.load_a, scatter(31))
    b1 = world.group(world.near, world.load_b, scatter(32))
    app.analysis_open()
    save = app.page.locator('[data-form="save-aggregate"] button[type=submit]')

    app.analysis_pick([a1, a2])
    assert app.analysis_aggregate() is not None
    save.click()
    expect(app.page.locator("#flash")).to_contain_text("Give this combination a name")
    assert app.analysis_saved() == []

    app.page.locator("#aggregate-name").fill("Mixed loads")
    app.analysis_pick([a1, b1])
    assert app.analysis_aggregate() is not None
    with app.console.allow(r"status of 422"):
        save.click()
        expect(app.page.locator("#flash")).to_contain_text("same load")
    assert app.analysis_saved() == []

    app.analysis_pick([a1])
    assert app.analysis_aggregate() is not None
    with app.console.allow(r"status of 422"):
        app.page.locator("#aggregate-name").fill("Single")
        save.click()
        expect(app.page.locator("#flash")).to_be_visible()
    assert app.analysis_saved() == []

    app.analysis_pick([a1, a2])
    assert app.analysis_aggregate() is not None
    app.analysis_save_combination("Two of one load")
    assert len(app.analysis_saved()) == 1
