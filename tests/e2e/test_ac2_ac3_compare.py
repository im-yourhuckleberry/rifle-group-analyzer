"""AC-2, AC-3 and AC-11 journeys: comparing loads, the POI shift, saved aggregates and the new-lot flag.

The comparison runs through `/compare` with finalized groups at 100 yd. Where the photo flow is not
the point, groups are seeded with exact offsets (`tests.support.seed`); the headline journey puts
one 10-shot group per load through the full photo UI (reference, review, aim, finalize) so the
comparison is checked against rendered truth. Verdict, POI shift and tier30 texts are read from the
page; expected values are computed here from the seeded or rendered offsets, never from the app.
"""

from __future__ import annotations

import math
import re

import pytest
from playwright.sync_api import expect

from rga.domain.types import Offset
from tests.e2e.pages import App, GroupContext, RenderedTarget
from tests.support import seed

pytestmark = pytest.mark.e2e

MOA_IN_PER_100YD = 1.0471975512
IN_PER_MIL_100YD = 3.6
ADVICE = "Zero the rifle to your hunting load"
NO_DIFFERENCE = "no real difference (within normal group-to-group variation)"
TIER10 = "10-shot groups (10 to 19 shots)"
TIER30 = "groups of 30 or more shots"
NEW_LOT_BADGE = "new lot: check POI against your zero load"
SHIFT_TEXT = re.compile(
    r"(?:(?P<up>\d+\.\d) mil (?P<ud>high|low))?(?:, )?(?:(?P<side>\d+\.\d) mil (?P<sd>right|left))?"
    r" of the zero load \(mean of (?P<n>\d+) shared days?\)"
)


def ring(n: int, es_moa: float, center: tuple[float, float] = (0.0, 0.0)) -> list[Offset]:
    """`n` (even) shots on a circle whose diameter is `es_moa` at 100 yd; the centroid is `center`."""
    radius = es_moa * MOA_IN_PER_100YD / 2
    return [
        Offset(
            center[0] + radius * math.cos(2 * math.pi * k / n),
            center[1] + radius * math.sin(2 * math.pi * k / n),
        )
        for k in range(n)
    ]


def truth_center_in(target: RenderedTarget) -> tuple[float, float]:
    """Mean hole position (right, up) in inches from the aim, from the rendered truth."""
    group = target.truth["groups"][0]
    holes = [target.truth["holes_in"][i] for i in group["hole_indices"]]
    aim = group["aim_in"]
    return (
        sum(h[0] for h in holes) / len(holes) - aim[0],
        -(sum(h[1] for h in holes) / len(holes) - aim[1]),
    )


def truth_es_moa(target: RenderedTarget) -> float:
    holes = [target.truth["holes_in"][i] for i in target.truth["groups"][0]["hole_indices"]]
    return max(math.dist(a, b) for a in holes for b in holes) / MOA_IN_PER_100YD


def photo_group(
    app: App, ctx: GroupContext, scenario: str, kind: str, load_label: str, lot: str
) -> tuple[int, RenderedTarget]:
    """Add a group with a rendered photo for `load_label`, confirm the reference, mark the aim
    and finalize; returns the group id and the rendered target."""
    target = app.render_target(scenario)
    expected = len(target.truth["groups"][0]["hole_indices"])
    ref = app.add_group(ctx, lot=lot, shots=expected, load_label=load_label, photo=target)
    app.confirm_reference(kind, candidate="truth", truth=target.truth)
    app.wait_path(r"/groups/\d+/review.*")
    app.review_wait_ready()
    app.review_mark_aim(truth_aim_px=target.truth["groups"][0]["aim_px"], photo_id=ref.photo_id)
    assert app.review_counts() == (expected, expected), app.review_counter()
    app.review_finalize()
    return ref.group_id, target


def seed_groups(app: App, ctx: GroupContext, load_id: int, groups: list[list[Offset]], lot="LOT-1"):
    with app.seeding() as conn:
        return [
            seed.seed_group(conn, context=ctx, load=load_id, offsets=offsets, lot_code=lot).id
            for offsets in groups
        ]


def parse_shift(text: str) -> tuple[float, float, int]:
    """(right mil, up mil, shared days) from "0.2 mil high, 0.1 mil left of the zero load ..."."""
    match = SHIFT_TEXT.search(text)
    assert match, text
    up = float(match["up"] or 0) * (-1 if match["ud"] == "low" else 1)
    right = float(match["side"] or 0) * (-1 if match["sd"] == "left" else 1)
    return right, up, int(match["n"])


def card(app: App, label: str):
    return app.page.locator(
        "#comparison-results section.card", has=app.page.locator("h2", has_text=label)
    )


def pair_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if " vs " in line and ", " in line]


def comparison_world(app: App, *, zero_label="Zero load Z", labels=("Load B",)):
    rifle_id = app.create_rifle(name="Compare rifle")
    zero = app.create_load(rifle_id, label=zero_label)
    others = [app.create_load(rifle_id, label=label) for label in labels]
    app.set_zero_load(rifle_id, zero_label)
    return rifle_id, zero, others


# ---------------------------------------------------------------------------------- AC-2 and AC-3


def test_two_loads_side_by_side_with_verdict_poi_shift_and_the_zero_to_hunting_load_advice(
    app: App,
) -> None:
    rifle_id, zero, (load_b,) = comparison_world(app)
    s1 = app.group_context(rifle_id, distance_yd=100, on="2026-09-01")
    s2 = app.group_context(rifle_id, distance_yd=100, on="2026-09-02")
    z_group, z_target = photo_group(app, s1, "clean_circle3", "circle3", "Zero load Z", "LOT-Z")
    b_group, b_target = photo_group(app, s1, "noisy_lighting", "circle1", "Load B", "LOT-B")
    seeded_es = 4.0
    seed_groups(app, s2, zero, [ring(10, seeded_es, (0.0, 0.0))], lot="LOT-Z")
    seed_groups(app, s2, load_b, [ring(10, seeded_es, (0.3, 0.2))], lot="LOT-B")

    mean_z = (truth_es_moa(z_target) + seeded_es) / 2
    mean_b = (truth_es_moa(b_target) + seeded_es) / 2
    assert abs(mean_z - mean_b) / ((mean_z + mean_b) / 2) < 0.25, "premise: within normal variation"

    app.compare_open(rifle_id)
    text = app.compare_run()

    assert (
        app.page.locator("#comparison-results section.card").count() == 3
    )  # two loads, head to head
    assert card(app, "Zero load Z").locator(".badge", has_text="zero load").count() == 1
    assert card(app, "Load B").locator(".badge", has_text="zero load").count() == 0
    for label in ("Zero load Z", "Load B"):
        body = card(app, label).inner_text()
        assert "Higher confidence (20 or more rounds)" in body
        assert "Precision (10-shot average):" in body
        assert "20" in body and "Groups included" in body
    assert "This is the rifle's zero load" in card(app, "Zero load Z").inner_text()
    assert f"Zero load Z vs Load B, {TIER10.lower()}: {NO_DIFFERENCE}" in text
    assert f"{TIER30}: insufficient data on one side, not ranked" in text
    assert ADVICE in text

    right, up, days = parse_shift(card(app, "Load B").inner_text())
    z_right, z_up = truth_center_in(z_target)
    b_right, b_up = truth_center_in(b_target)
    expected_right = ((b_right - z_right) + 0.3) / 2 / IN_PER_MIL_100YD
    expected_up = ((b_up - z_up) + 0.2) / 2 / IN_PER_MIL_100YD
    assert days == 2
    assert right == pytest.approx(expected_right, abs=0.1)
    assert up == pytest.approx(expected_up, abs=0.1)
    app.console.assert_clean()


@pytest.mark.parametrize(
    ("es_b", "expected"),
    [
        (1.28, NO_DIFFERENCE),
        (1.6, "Zero load Z is smaller"),
    ],
)
def test_the_verdict_is_no_real_difference_within_30_percent_and_ranks_beyond_it(
    app: App, es_b: float, expected: str
) -> None:
    rifle_id, zero, (load_b,) = comparison_world(app)
    for day in (1, 2):
        ctx = app.group_context(rifle_id, distance_yd=100, on=f"2026-09-0{day}")
        seed_groups(app, ctx, zero, [ring(10, 1.0)], lot="LOT-Z")
        seed_groups(app, ctx, load_b, [ring(10, es_b)], lot="LOT-B")
    app.compare_open(rifle_id)
    text = app.compare_run()
    assert f"Zero load Z vs Load B, {TIER10.lower()}: {expected}" in text
    assert "sufficient for deer/elk to 800 yd" in card(app, "Zero load Z").inner_text()


def test_a_load_with_five_rounds_is_insufficient_data_and_never_ranked(app: App) -> None:
    rifle_id, zero, (load_c,) = comparison_world(app, labels=("Load C",))
    ctx = app.group_context(rifle_id, distance_yd=100)
    seed_groups(app, ctx, zero, [ring(10, 1.0)], lot="LOT-Z")
    seed_groups(app, ctx, load_c, [ring(6, 0.6)[:5]], lot="LOT-C")
    app.compare_open(rifle_id)
    text = app.compare_run()
    body = card(app, "Load C").inner_text()
    assert "Insufficient data: not ranked" in body
    assert "needs at least one 10-shot group" in body
    assert "no tier" in body
    assert (
        f"Zero load Z vs Load C, {TIER10.lower()}: insufficient data on one side, not ranked"
        in text
    )
    assert "is smaller" not in text


def test_loads_shot_on_different_days_say_to_shoot_both_on_the_same_day(app: App) -> None:
    rifle_id, zero, (load_d,) = comparison_world(app, labels=("Load D",))
    s1 = app.group_context(rifle_id, distance_yd=100, on="2026-09-01")
    s2 = app.group_context(rifle_id, distance_yd=100, on="2026-09-02")
    seed_groups(app, s1, zero, [ring(10, 1.0)], lot="LOT-Z")
    seed_groups(app, s2, load_d, [ring(10, 1.1, (0.2, 0.0))], lot="LOT-D")
    app.compare_open(rifle_id)
    app.compare_run()
    body = card(app, "Load D").inner_text()
    assert (
        "shoot both loads on the same day, distance, position and suppressor setting"
        in body.lower()
    )
    assert "mil" not in body.split("POI shift from the zero load:")[1].split("\n")[0]


def test_without_a_zero_load_the_page_says_to_set_one_and_shows_no_shift(app: App) -> None:
    rifle_id = app.create_rifle(name="No zero")
    load_a = app.create_load(rifle_id, label="Load A")
    load_b = app.create_load(rifle_id, label="Load B")
    ctx = app.group_context(rifle_id, distance_yd=100)
    seed_groups(app, ctx, load_a, [ring(10, 1.0)])
    seed_groups(app, ctx, load_b, [ring(10, 1.4, (0.3, 0.3))])
    app.compare_open(rifle_id)
    app.compare_run()
    for label in ("Load A", "Load B"):
        line = (
            card(app, label).inner_text().split("POI shift from the zero load:")[1].split("\n")[0]
        )
        assert "Set the rifle's zero load to measure POI shift" in line and "mil" not in line


def test_the_position_filter_and_the_conditions_warning(app: App) -> None:
    rifle_id, zero, (load_b,) = comparison_world(app)
    bench = app.group_context(rifle_id, distance_yd=100, on="2026-09-01")
    prone = app.group_context(rifle_id, distance_yd=100, on="2026-09-02", position="prone_off_pack")
    seed_groups(app, bench, zero, [ring(10, 1.0)], lot="LOT-Z")
    seed_groups(app, prone, load_b, [ring(10, 1.0)], lot="LOT-B")
    app.compare_open(rifle_id)
    text = app.compare_run()
    assert "different shooting position" in text
    results = app.page.locator("#comparison-results")
    app.compare_run(position="bench")
    expect(results).not_to_contain_text("different shooting position")
    assert (
        "No finalized groups of 10 or more shots match these filters."
        in card(app, "Load B").inner_text()
    )


# ---------------------------------------------------------------------------------- saved aggregates


def save_aggregate(app: App, group_ids: list[int], name: str) -> dict:
    app.analysis_open()
    app.analysis_pick(group_ids)
    result = app.analysis_aggregate()
    assert result is not None, app.flash()
    app.analysis_save_combination(name)
    return result


def test_saved_aggregates_feed_the_thirty_shot_comparison(app: App) -> None:
    rifle_id, zero, (load_b,) = comparison_world(app)
    ctx = app.group_context(rifle_id, distance_yd=100)
    z_ids = seed_groups(app, ctx, zero, [ring(10, 1.0)] * 3, lot="LOT-Z")
    b_ids = seed_groups(app, ctx, load_b, [ring(10, 2.0)] * 3, lot="LOT-B")

    app.compare_open(rifle_id)
    before = app.compare_run()
    assert f"{TIER30}: insufficient data on one side, not ranked" in before

    aggregate = save_aggregate(app, z_ids, "Z thirty")
    assert aggregate["tiles"]["shots"][0] == "30" and aggregate["tiles"]["groups"][0] == "3"
    save_aggregate(app, b_ids, "B thirty")
    assert any("Z thirty" in item for item in app.analysis_saved())

    app.compare_open(rifle_id)
    text = app.compare_run()
    assert f"Zero load Z vs Load B, {TIER30}: Zero load Z is smaller" in text
    for label, name in (("Zero load Z", "Z thirty"), ("Load B", "B thirty")):
        body = card(app, label).inner_text()
        assert "Saved combinations" in body and name in body
        assert "counted in the 30-shot comparison" in body
        tier30 = body.split("Groups of 30 or more shots")[1].split("Precision")[0]
        assert "30" in tier30 and "No groups in this tier" not in tier30
    # the member groups still count as three ten-shot groups
    assert "Higher confidence (20 or more rounds)" in card(app, "Zero load Z").inner_text()


def test_an_aggregate_containing_a_thirty_shot_group_is_listed_with_the_overlap_note(
    app: App,
) -> None:
    rifle_id, zero, _ = comparison_world(app, labels=())
    ctx = app.group_context(rifle_id, distance_yd=100)
    big, small = seed_groups(app, ctx, zero, [ring(30, 1.0), ring(10, 1.0)], lot="LOT-Z")
    save_aggregate(app, [big, small], "Big plus small")
    app.compare_open(rifle_id)
    app.compare_run()
    body = card(app, "Zero load Z").inner_text()
    assert "Big plus small" in body
    assert (
        "not counted: it contains a group of 30 or more shots, which already counts on its own"
        in body
    )
    tier30 = body.split("Groups of 30 or more shots")[1].split("Precision")[0]
    assert re.search(r"ROUNDS\s+30\s+GROUPS\s+1\b", tier30), tier30


def test_a_deleted_saved_combination_leaves_the_comparison(app: App) -> None:
    rifle_id, zero, _ = comparison_world(app, labels=())
    ctx = app.group_context(rifle_id, distance_yd=100)
    ids = seed_groups(app, ctx, zero, [ring(10, 1.0)] * 3, lot="LOT-Z")
    save_aggregate(app, ids, "Soon gone")
    app.compare_open(rifle_id)
    app.compare_run()
    assert "Soon gone" in card(app, "Zero load Z").inner_text()
    app.analysis_open()
    app.analysis_delete_saved("Soon gone")
    app.compare_open(rifle_id)
    app.compare_run()
    assert "Soon gone" not in card(app, "Zero load Z").inner_text()


# ---------------------------------------------------------------------------------- AC-11


def row_badges(app: App, rifle_id: int) -> dict[int, bool]:
    app.open_rifle(rifle_id)
    rows = app.main.locator("#groups-card tr[data-group-id]")
    return {
        int(rows.nth(i).get_attribute("data-group-id")): rows.nth(i)
        .locator(".badge", has_text=NEW_LOT_BADGE)
        .count()
        == 1
        for i in range(rows.count())
    }


def test_a_new_lot_code_shows_the_new_lot_flag_on_the_rifle_page(app: App) -> None:
    rifle_id, zero, _ = comparison_world(app, labels=())
    ctx = app.group_context(rifle_id, distance_yd=100)
    (first_a,) = seed_groups(app, ctx, zero, [ring(10, 1.0)], lot="LOT-A")
    assert row_badges(app, rifle_id) == {first_a: False}, "the first lot of a load is not flagged"

    new_lot = app.add_group(ctx, lot="LOT-B", shots=10).group_id
    same_lot = app.add_group(ctx, lot="LOT-A", shots=10).group_id
    flags = row_badges(app, rifle_id)
    assert flags == {first_a: False, new_lot: True, same_lot: False}

    second_b = app.add_group(ctx, lot="LOT-B", shots=10).group_id
    flags = row_badges(app, rifle_id)
    assert flags[new_lot] is True and flags[second_b] is True, "drafts of a new lot are all flagged"


def test_the_first_group_of_a_load_is_not_flagged_even_as_a_draft(app: App) -> None:
    rifle_id, _, _ = comparison_world(app, labels=())
    ctx = app.group_context(rifle_id, distance_yd=100)
    first = app.add_group(ctx, lot="LOT-FIRST", shots=10).group_id
    assert row_badges(app, rifle_id) == {first: False}
    second = app.add_group(ctx, lot="LOT-FIRST", shots=10).group_id
    assert row_badges(app, rifle_id) == {first: False, second: False}


def test_finalized_groups_keep_the_flag_only_on_the_first_group_of_the_new_lot(app: App) -> None:
    rifle_id, zero, _ = comparison_world(app, labels=())
    ctx = app.group_context(rifle_id, distance_yd=100)
    a1 = seed_groups(app, ctx, zero, [ring(10, 1.0)], lot="LOT-A")[0]
    b1 = seed_groups(app, ctx, zero, [ring(10, 1.0)], lot="LOT-B")[0]
    a2 = seed_groups(app, ctx, zero, [ring(10, 1.0)], lot="LOT-A")[0]
    b2 = seed_groups(app, ctx, zero, [ring(10, 1.0)], lot="LOT-B")[0]
    assert row_badges(app, rifle_id) == {a1: False, b1: True, a2: False, b2: False}
