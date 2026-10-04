"""T1 tests for rga.domain.zeroing: corrections, Step 1/2 advice, dot size, zero check (D-04)."""

import math
import random
import re
from pathlib import Path

import pytest

from rga.domain import zeroing
from rga.domain.stats import extreme_spread_in
from rga.domain.types import Advice, AdviceKind, Correction, Offset, ShotOrigin
from rga.domain.units import in_to_mil, round_tenth
from rga.domain.zeroing import (
    STANDARD_DOTS_IN,
    ZERO_CHECK_LOST_ZERO_MESSAGE,
    ZERO_CHECK_OK_MESSAGE,
    correction_for,
    masks_poi,
    step1_advice,
    step2_confirm_advice,
    step2_initial_advice,
    suggest_dot,
    zero_check,
)
from rga.web.schemas import AdviceOut, ZeroCheckOut

D = ShotOrigin.DETECTED
E = ShotOrigin.ESTIMATED
IN_PER_MIL_100YD = 3.6
NO_ADJUSTMENT = Correction(0.0, 0.0, "No adjustment")


def offs(*pairs):
    return [Offset(dx_in=x, dy_in=y) for x, y in pairs]


def random_group(rng: random.Random, n: int, spread: float = 3.0) -> list[Offset]:
    return [Offset(rng.uniform(-spread, spread), rng.uniform(-spread, spread)) for _ in range(n)]


def ring(cx_in: float, cy_in: float, n: int = 10, radius_in: float = 0.5) -> list[Offset]:
    """`n` shots on a circle whose centroid is exactly (cx_in, cy_in)."""
    return [
        Offset(
            cx_in + radius_in * math.cos(2 * math.pi * i / n),
            cy_in + radius_in * math.sin(2 * math.pi * i / n),
        )
        for i in range(n)
    ]


def mean_of(offsets):
    return (
        sum(o.dx_in for o in offsets) / len(offsets),
        sum(o.dy_in for o in offsets) / len(offsets),
    )


def origins_for(*groups):
    return [D] * sum(len(g) for g in groups)


def shoot_more_text(n: int) -> str:
    return f"Shoot more shots: {n} of 10 so far, {10 - n} left to fire"


def confirm_short_text(number: int, shots: int) -> str:
    return (
        f"Confirm group {number} has {shots} of 10 shots: every confirm group needs 10 shots "
        "before the zero can be judged"
    )


def step1_more_text(n: int) -> str:
    return (
        "Step 1 only corrects from 1 to 3 shots. Continue to 10 shots for Step 2: "
        f"{n} of 10 so far, {10 - n} left to fire"
    )


def assert_no_number(advice: Advice) -> None:
    assert advice.correction is None
    assert advice.stats is None


# (mean dx mil, mean dy mil) -> expected (up_mil, right_mil, text); positive dx = right, dy = high
ONE_AXIS_AND_TENTH_CLICK_CASES = [
    pytest.param(0.0, 0.3, Correction(-0.3, 0.0, "Dial DOWN 0.3"), id="high-only"),
    pytest.param(0.0, -0.4, Correction(0.4, 0.0, "Dial UP 0.4"), id="low-only"),
    pytest.param(0.5, 0.0, Correction(0.0, -0.5, "Dial LEFT 0.5"), id="right-only"),
    pytest.param(-0.2, 0.0, Correction(0.0, 0.2, "Dial RIGHT 0.2"), id="left-only"),
    pytest.param(0.0, 0.1, Correction(-0.1, 0.0, "Dial DOWN 0.1"), id="high-exactly-0.1"),
    pytest.param(0.0, -0.1, Correction(0.1, 0.0, "Dial UP 0.1"), id="low-exactly-0.1"),
    pytest.param(0.1, 0.0, Correction(0.0, -0.1, "Dial LEFT 0.1"), id="right-exactly-0.1"),
    pytest.param(-0.1, 0.0, Correction(0.0, 0.1, "Dial RIGHT 0.1"), id="left-exactly-0.1"),
    pytest.param(0.1, 0.1, Correction(-0.1, -0.1, "Dial DOWN 0.1, LEFT 0.1"), id="both-0.1"),
    pytest.param(0.3, 0.2, Correction(-0.2, -0.3, "Dial DOWN 0.2, LEFT 0.3"), id="both-axes"),
]


def assert_correction_advice(advice: Advice, correction: Correction, prefix: str, n: int) -> None:
    assert advice.kind is AdviceKind.CORRECTION
    assert advice.correction == correction
    assert advice.message == f"{prefix}{correction.text}."
    assert "No adjustment" not in advice.message
    assert advice.stats is not None and advice.stats.n == n


class TestCorrectionFor:
    @pytest.mark.parametrize(
        ("mean_dx_mil", "mean_dy_mil", "up", "right", "text"),
        [
            (0.3, 0.2, -0.2, -0.3, "Dial DOWN 0.2, LEFT 0.3"),
            (-0.3, -0.2, 0.2, 0.3, "Dial UP 0.2, RIGHT 0.3"),
            (0.3, -0.2, 0.2, -0.3, "Dial UP 0.2, LEFT 0.3"),
            (-0.3, 0.2, -0.2, 0.3, "Dial DOWN 0.2, RIGHT 0.3"),
            (0.3, 0.0, 0.0, -0.3, "Dial LEFT 0.3"),
            (0.0, 0.4, -0.4, 0.0, "Dial DOWN 0.4"),
            (0.0, 0.0, 0.0, 0.0, "No adjustment"),
            (0.049, -0.049, 0.0, 0.0, "No adjustment"),
            (0.05, 0.05, -0.1, -0.1, "Dial DOWN 0.1, LEFT 0.1"),
            (-0.05, -0.05, 0.1, 0.1, "Dial UP 0.1, RIGHT 0.1"),
            (0.15, -0.15, 0.2, -0.2, "Dial UP 0.2, LEFT 0.2"),
            (1.26, 0.0, 0.0, -1.3, "Dial LEFT 1.3"),
        ],
    )
    def test_direction_words_and_half_away_rounding(
        self, mean_dx_mil, mean_dy_mil, up, right, text
    ):
        correction = correction_for(mean_dx_mil, mean_dy_mil)

        assert (correction.up_mil, correction.right_mil, correction.text) == (up, right, text)

    def test_zero_correction_is_never_negative_zero(self):
        correction = correction_for(0.01, 0.01)

        assert math.copysign(1.0, correction.up_mil) == 1.0
        assert math.copysign(1.0, correction.right_mil) == 1.0

    def test_text_never_mentions_a_ballistic_app_offset(self):
        for dx, dy in [(0.3, 0.2), (0.0, 0.0), (-1.0, 2.0)]:
            assert not re.search(r"ballistic|app|offset", correction_for(dx, dy).text, re.I)


class TestStep1:
    def test_one_to_three_shots_give_a_correction_off_the_mean(self):
        # mean (0.36 in, 0.72 in) = (0.1 mil right, 0.2 mil high) at 100 yd
        for shots in (
            offs((0.36, 0.72)),
            offs((0.0, 0.72), (0.72, 0.72)),
            offs((0.0, 0.0), (0.36, 0.72), (0.72, 1.44)),
        ):
            advice = step1_advice(shots)

            assert advice.kind is AdviceKind.CORRECTION
            assert advice.correction == Correction(-0.2, -0.1, "Dial DOWN 0.2, LEFT 0.1")
            assert advice.stats is not None and advice.stats.n == len(shots)

    def test_centered_shots_need_no_adjustment(self):
        advice = step1_advice(offs((0.01, 0.0), (-0.01, 0.0)))

        assert advice.kind is AdviceKind.NO_ADJUSTMENT
        assert advice.correction == NO_ADJUSTMENT
        assert advice.message == "No adjustment needed."

    @pytest.mark.parametrize("shots", [1, 3])
    @pytest.mark.parametrize(("dx_mil", "dy_mil", "correction"), ONE_AXIS_AND_TENTH_CLICK_CASES)
    def test_one_axis_and_exactly_one_click_corrections_read_as_a_correction(
        self, shots, dx_mil, dy_mil, correction
    ):
        mx, my = dx_mil * IN_PER_MIL_100YD, dy_mil * IN_PER_MIL_100YD
        spread = [(0.0, 0.0), (0.5, 0.3), (-0.5, -0.3)][:shots]
        group = offs(*[(mx + dx, my + dy) for dx, dy in spread])

        advice = step1_advice(group)

        assert_correction_advice(advice, correction, "Get on paper: ", shots)

    @pytest.mark.parametrize(("dx_mil", "dy_mil"), [(0.04, 0.0), (0.0, -0.04), (0.04, -0.04)])
    def test_less_than_half_a_click_on_both_axes_is_no_adjustment(self, dx_mil, dy_mil):
        advice = step1_advice(offs((dx_mil * IN_PER_MIL_100YD, dy_mil * IN_PER_MIL_100YD)))

        assert advice.kind is AdviceKind.NO_ADJUSTMENT
        assert advice.message == "No adjustment needed."
        assert advice.correction == NO_ADJUSTMENT

    def test_estimated_origin_is_carried_into_stats(self):
        advice = step1_advice(offs((0.36, 0.72), (0.4, 0.7)), [D, E])

        assert advice.stats is not None and advice.stats.includes_estimated

    @pytest.mark.parametrize("n", range(4, 10))
    def test_four_to_nine_shots_continue_to_ten_with_no_number(self, n):
        advice = step1_advice(random_group(random.Random(n), n))

        assert advice.kind is AdviceKind.SHOOT_MORE
        assert advice.message == step1_more_text(n)
        assert_no_number(advice)

    @pytest.mark.parametrize("n", [10, 11, 30])
    def test_ten_or_more_shots_are_sent_to_step_2_with_no_number(self, n):
        advice = step1_advice(random_group(random.Random(n), n))

        assert advice.kind is AdviceKind.SHOOT_MORE
        assert advice.message == (
            f"Step 1 only corrects from 1 to 3 shots. This group has {n} shots: "
            "use Step 2 for the correction."
        )
        assert_no_number(advice)

    def test_no_shots_is_invalid(self):
        advice = step1_advice([])

        assert advice.kind is AdviceKind.INVALID
        assert advice.message == "No shots to analyze: add at least 1 shot."
        assert_no_number(advice)


class TestStep2Initial:
    @pytest.mark.parametrize("n", range(1, 10))
    def test_must_never_number_from_fewer_than_ten_shots(self, n):
        rng = random.Random(1000 + n)
        for _ in range(200):
            group = random_group(rng, n, spread=rng.choice([0.2, 1.0, 6.0]))
            advice = step2_initial_advice(group, [D] * n)

            assert advice.kind is AdviceKind.SHOOT_MORE
            assert advice.message == shoot_more_text(n)
            assert_no_number(advice)

    def test_no_shots_is_invalid(self):
        advice = step2_initial_advice([], [])

        assert advice.kind is AdviceKind.INVALID
        assert advice.message == "No shots to analyze: add at least 1 shot."
        assert_no_number(advice)

    def test_ten_shots_high_and_right_say_down_and_left(self):
        # centroid 0.3 mil right (1.08 in) and 0.2 mil high (0.72 in)
        group = ring(1.08, 0.72, n=10)

        advice = step2_initial_advice(group, [D] * 10)

        assert advice.kind is AdviceKind.CORRECTION
        assert advice.correction == Correction(-0.2, -0.3, "Dial DOWN 0.2, LEFT 0.3")
        assert advice.stats is not None and advice.stats.n == 10

    def test_every_shot_counts_including_the_far_one(self):
        tight = ring(0.0, 0.0, n=9, radius_in=0.2)
        group = [*tight, Offset(0.0, 9.0)]
        mean_dy_mil = in_to_mil(0.9, 100)

        advice = step2_initial_advice(group, [D] * 10)

        assert advice.correction is not None
        assert advice.correction.up_mil == round_tenth(-mean_dy_mil)
        assert advice.correction.up_mil == -0.3

    def test_correction_matches_an_independent_mean_for_random_groups(self):
        rng = random.Random(77)
        for n in (10, 11, 15, 20, 30):
            for _ in range(20):
                group = random_group(rng, n)
                mx, my = mean_of(group)

                advice = step2_initial_advice(group, [D] * n)

                expected = correction_for(mx / 3.6, my / 3.6)
                assert advice.correction == expected
                moves = expected.up_mil != 0.0 or expected.right_mil != 0.0
                assert advice.kind is (AdviceKind.CORRECTION if moves else AdviceKind.NO_ADJUSTMENT)

    def test_centered_ten_shots_need_no_adjustment(self):
        advice = step2_initial_advice(ring(0.0, 0.0), [D] * 10)

        assert advice.kind is AdviceKind.NO_ADJUSTMENT
        assert advice.correction == NO_ADJUSTMENT
        assert advice.message == "No adjustment needed."

    @pytest.mark.parametrize(("dx_mil", "dy_mil", "correction"), ONE_AXIS_AND_TENTH_CLICK_CASES)
    def test_one_axis_and_exactly_one_click_corrections_read_as_a_correction(
        self, dx_mil, dy_mil, correction
    ):
        group = ring(dx_mil * IN_PER_MIL_100YD, dy_mil * IN_PER_MIL_100YD, n=10)

        advice = step2_initial_advice(group, [D] * 10)

        assert_correction_advice(advice, correction, "Step 2: ", 10)

    @pytest.mark.parametrize(("dx_mil", "dy_mil"), [(0.04, 0.0), (0.0, -0.04), (0.04, -0.04)])
    def test_less_than_half_a_click_on_both_axes_is_no_adjustment(self, dx_mil, dy_mil):
        group = ring(dx_mil * IN_PER_MIL_100YD, dy_mil * IN_PER_MIL_100YD)

        advice = step2_initial_advice(group, [D] * 10)

        assert advice.kind is AdviceKind.NO_ADJUSTMENT
        assert advice.message == "No adjustment needed."
        assert advice.correction == NO_ADJUSTMENT

    def test_mismatched_origins_are_rejected(self):
        with pytest.raises(ValueError, match="origins"):
            step2_initial_advice(ring(0.0, 0.0), [D] * 9)


class TestStep2Confirm:
    DIALED = Correction(-0.2, -0.3, "Dial DOWN 0.2, LEFT 0.3")

    def _confirm(self, confirm_centroid_in, initial=None, extra_confirms=()):
        initial = initial if initial is not None else ring(1.0, 1.0)
        confirms = [ring(*confirm_centroid_in), *extra_confirms]
        return step2_confirm_advice(initial, self.DIALED, confirms, origins_for(initial, *confirms))

    def test_overlay_of_same_shape_one_inch_apart_measures_that_shape_only(self):
        shape = ring(0.0, 0.0, radius_in=0.5)
        moved = [Offset(o.dx_in + 1.0, o.dy_in) for o in shape]

        advice = step2_confirm_advice(shape, self.DIALED, [moved], origins_for(shape, moved))

        assert advice.stats is not None
        assert advice.stats.n == 20
        assert advice.stats.es_in == pytest.approx(extreme_spread_in(shape), abs=1e-9)
        assert advice.stats.mean_dx_in == pytest.approx(0.0, abs=1e-9)
        assert advice.stats.mean_dy_in == pytest.approx(0.0, abs=1e-9)

    def test_dialed_correction_never_enters_the_result(self):
        initial = ring(1.0, 1.0)
        confirm = ring(0.2, 0.0)
        other = Correction(5.0, -7.0, "Dial UP 5.0, LEFT 7.0")
        origins = origins_for(initial, confirm)

        a = step2_confirm_advice(initial, self.DIALED, [confirm], origins)
        b = step2_confirm_advice(initial, other, [confirm], origins)

        assert a == b

    def test_zero_is_judged_on_the_confirm_groups_own_centroid(self):
        advice = self._confirm((0.0, 0.0), initial=ring(3.0, -2.0))

        assert advice.kind is AdviceKind.READY_FOR_DOT
        assert advice.correction is None

    @pytest.mark.parametrize(
        ("dx_mil", "dy_mil", "kind"),
        [
            (0.0, 0.0, AdviceKind.READY_FOR_DOT),
            (0.04, -0.04, AdviceKind.READY_FOR_DOT),
            (0.06, 0.0, AdviceKind.TOUCH_UP_OK),
            (0.0, -0.14, AdviceKind.TOUCH_UP_OK),
            (-0.1, 0.1, AdviceKind.TOUCH_UP_OK),
            (0.16, 0.0, AdviceKind.CONFIRM_AGAIN),
            (0.0, -0.2, AdviceKind.CONFIRM_AGAIN),
            (0.1, 0.3, AdviceKind.CONFIRM_AGAIN),
            (-0.5, -0.5, AdviceKind.CONFIRM_AGAIN),
        ],
    )
    def test_threshold_between_ready_touch_up_and_confirm_again(self, dx_mil, dy_mil, kind):
        advice = self._confirm((dx_mil * IN_PER_MIL_100YD, dy_mil * IN_PER_MIL_100YD))

        assert advice.kind is kind

    def test_touch_up_and_confirm_again_carry_a_correction_with_direction_words(self):
        touch_up = self._confirm((0.1 * IN_PER_MIL_100YD, 0.0))
        again = self._confirm((0.3 * IN_PER_MIL_100YD, 0.2 * IN_PER_MIL_100YD))

        assert touch_up.correction == Correction(0.0, -0.1, "Dial LEFT 0.1")
        assert touch_up.message == (
            "A 0.1 mil touch-up is acceptable: Dial LEFT 0.1. Then move on to the dot."
        )
        assert again.correction == Correction(-0.2, -0.3, "Dial DOWN 0.2, LEFT 0.3")
        assert again.message == (
            "Dial DOWN 0.2, LEFT 0.3, then shoot another 10-shot confirm group."
        )

    def test_a_confirm_group_under_ten_shots_gets_no_number(self):
        initial = ring(1.0, 1.0)
        for n in range(1, 10):
            confirm = random_group(random.Random(n), n)
            advice = step2_confirm_advice(
                initial, self.DIALED, [confirm], origins_for(initial, confirm)
            )

            assert advice.kind is AdviceKind.SHOOT_MORE
            assert advice.message == shoot_more_text(n)
            assert_no_number(advice)

    @pytest.mark.parametrize("latest_shots", range(1, 10))
    def test_must_never_number_when_the_latest_confirm_is_short_after_a_full_one(
        self, latest_shots
    ):
        initial, full = ring(1.0, 1.0), ring(0.5, 0.0)
        latest = random_group(random.Random(latest_shots), latest_shots)
        for confirms in ([full, latest], [full, ring(0.2, 0.2), latest]):
            advice = step2_confirm_advice(
                initial, self.DIALED, confirms, origins_for(initial, *confirms)
            )

            assert advice.kind is AdviceKind.SHOOT_MORE
            assert advice.message == shoot_more_text(latest_shots)
            assert_no_number(advice)
            AdviceOut.model_validate(advice)

    def test_must_never_number_for_random_short_latest_groups_on_the_multi_confirm_path(self):
        rng = random.Random(4242)
        initial = ring(1.0, 1.0)
        for _ in range(200):
            earlier = [random_group(rng, 10, spread=rng.choice([0.3, 2.0, 8.0]))]
            latest = random_group(rng, rng.randint(1, 9), spread=rng.choice([0.3, 2.0, 8.0]))
            confirms = [*earlier, latest]

            advice = step2_confirm_advice(
                initial, self.DIALED, confirms, origins_for(initial, *confirms)
            )

            assert advice.kind is AdviceKind.SHOOT_MORE
            assert advice.message == shoot_more_text(len(latest))
            assert_no_number(advice)

    def test_a_full_confirm_followed_by_an_empty_one_asks_for_ten_more(self):
        initial, full = ring(1.0, 1.0), ring(0.5, 0.0)

        advice = step2_confirm_advice(initial, self.DIALED, [full, []], origins_for(initial, full))

        assert advice.kind is AdviceKind.SHOOT_MORE
        assert advice.message == shoot_more_text(0)
        assert_no_number(advice)

    @pytest.mark.parametrize("shots", range(0, 10))
    def test_a_short_or_empty_middle_confirm_group_is_invalid_never_overlaid(self, shots):
        initial, last = ring(1.0, 1.0), ring(0.0, 0.0)
        middle = random_group(random.Random(shots), shots)
        confirms = [middle, last]

        advice = step2_confirm_advice(
            initial, self.DIALED, confirms, origins_for(initial, *confirms)
        )

        assert advice.kind is AdviceKind.INVALID
        assert advice.message == confirm_short_text(1, shots)
        assert_no_number(advice)
        AdviceOut.model_validate(advice)

    def test_the_short_group_is_named_by_its_position_among_the_confirm_groups(self):
        initial, full = ring(1.0, 1.0), ring(0.5, 0.0)
        short = random_group(random.Random(8), 4)
        confirms = [full, short, full]

        advice = step2_confirm_advice(
            initial, self.DIALED, confirms, origins_for(initial, *confirms)
        )

        assert advice.kind is AdviceKind.INVALID
        assert advice.message == confirm_short_text(2, 4)

    def test_a_short_group_between_full_ones_cannot_reach_the_thirty_shot_label(self):
        initial = ring(0.0, 0.0, n=15)
        five, ten = ring(0.0, 0.0, n=5), ring(0.0, 0.0)
        confirms = [five, ten]

        advice = step2_confirm_advice(
            initial, self.DIALED, confirms, origins_for(initial, *confirms)
        )

        assert advice.kind is AdviceKind.INVALID
        assert "confidence" not in advice.message
        assert advice.stats is None

    def test_an_empty_middle_group_is_invalid_not_a_division_by_zero(self):
        initial, last = ring(1.0, 1.0), ring(0.0, 0.0)

        advice = step2_confirm_advice(initial, self.DIALED, [[], last], origins_for(initial, last))

        assert advice.kind is AdviceKind.INVALID
        assert advice.message == confirm_short_text(1, 0)

    def test_no_confirm_group_yet_asks_for_ten_shots(self):
        initial = ring(1.0, 1.0)

        advice = step2_confirm_advice(initial, self.DIALED, [], origins_for(initial))

        assert advice.kind is AdviceKind.SHOOT_MORE
        assert advice.message == shoot_more_text(0)
        assert_no_number(advice)

    def test_initial_group_under_ten_shots_is_invalid(self):
        initial = random_group(random.Random(3), 6)
        confirm = ring(0.0, 0.0)

        advice = step2_confirm_advice(
            initial, self.DIALED, [confirm], origins_for(initial, confirm)
        )

        assert advice.kind is AdviceKind.INVALID
        assert advice.message == (
            "The first Step 2 group needs 10 shots before a confirm group can be judged: "
            "6 of 10 so far."
        )
        assert_no_number(advice)

    def test_thirty_shot_path_overlays_all_three_groups_and_judges_the_latest(self):
        initial = ring(1.0, 1.0)
        first_confirm = ring(0.9, 0.0)
        second_confirm = ring(0.0, 0.0)
        confirms = [first_confirm, second_confirm]

        advice = step2_confirm_advice(
            initial, self.DIALED, confirms, origins_for(initial, *confirms)
        )

        assert advice.kind is AdviceKind.READY_FOR_DOT
        assert advice.stats is not None and advice.stats.n == 30
        assert "very high confidence" in advice.message

    def test_three_groups_judge_both_axes_on_the_latest_confirm_not_a_superseded_one(self):
        initial = ring(1.0, 1.0)
        first_confirm = ring(0.9, 1.0)
        second_confirm = ring(0.0, 0.0)
        confirms = [first_confirm, second_confirm]

        advice = step2_confirm_advice(
            initial, self.DIALED, confirms, origins_for(initial, *confirms)
        )

        assert advice.kind is AdviceKind.READY_FOR_DOT
        assert advice.correction is None
        assert advice.stats is not None and advice.stats.n == 30

    def test_the_correction_comes_from_the_latest_confirm_on_both_axes(self):
        # 0.72 in right / 1.08 in high of aim = 0.2 mil right / 0.3 mil high; the superseded
        # first confirm is offset the opposite way on both axes
        initial = ring(1.0, 1.0)
        first_confirm = ring(-0.9, -1.0)
        latest = ring(0.72, 1.08)
        confirms = [first_confirm, latest]

        advice = step2_confirm_advice(
            initial, self.DIALED, confirms, origins_for(initial, *confirms)
        )

        assert advice.kind is AdviceKind.CONFIRM_AGAIN
        assert advice.correction == Correction(-0.3, -0.2, "Dial DOWN 0.3, LEFT 0.2")
        assert advice.message == (
            "Dial DOWN 0.3, LEFT 0.2, then shoot another 10-shot confirm group."
        )

    @pytest.mark.parametrize("initial_shots", range(0, 10))
    def test_an_initial_group_under_ten_shots_is_never_ready_and_never_a_correction(
        self, initial_shots
    ):
        initial = random_group(random.Random(300 + initial_shots), initial_shots)
        full = ring(0.0, 0.0)
        for confirms in ([full], [full, full], []):
            advice = step2_confirm_advice(
                initial, self.DIALED, confirms, origins_for(initial, *confirms)
            )

            assert advice.kind is AdviceKind.INVALID
            assert advice.message == (
                "The first Step 2 group needs 10 shots before a confirm group can be judged: "
                f"{initial_shots} of 10 so far."
            )
            assert_no_number(advice)
            AdviceOut.model_validate(advice)

    def test_an_initial_group_of_exactly_ten_shots_can_be_judged(self):
        initial, confirm = ring(1.0, 1.0), ring(0.0, 0.0)

        advice = step2_confirm_advice(
            initial, self.DIALED, [confirm], origins_for(initial, confirm)
        )

        assert advice.kind is AdviceKind.READY_FOR_DOT

    @pytest.mark.parametrize(
        ("initial_shots", "latest_shots"),
        [(10, 10), (10, 15), (10, 20), (10, 30), (12, 10), (25, 20), (14, 30)],
    )
    def test_the_correction_is_the_latest_confirm_groups_own_centroid_for_any_group_size(
        self, initial_shots, latest_shots
    ):
        # centroid 1.8 in right / 1.08 in high of aim = 0.5 mil right / 0.3 mil high
        initial = ring(-1.0, 2.0, n=initial_shots)
        latest = ring(0.5 * IN_PER_MIL_100YD, 0.3 * IN_PER_MIL_100YD, n=latest_shots)

        advice = step2_confirm_advice(initial, self.DIALED, [latest], origins_for(initial, latest))

        assert advice.kind is AdviceKind.CONFIRM_AGAIN
        assert advice.correction == Correction(-0.3, -0.5, "Dial DOWN 0.3, LEFT 0.5")
        assert advice.message == (
            "Dial DOWN 0.3, LEFT 0.5, then shoot another 10-shot confirm group."
        )

    @pytest.mark.parametrize("latest_shots", [10, 15, 20, 30])
    def test_a_centered_latest_group_of_any_size_is_ready_whatever_the_initial_size(
        self, latest_shots
    ):
        initial = ring(2.0, -1.0, n=13)
        latest = ring(0.0, 0.0, n=latest_shots)

        advice = step2_confirm_advice(initial, self.DIALED, [latest], origins_for(initial, latest))

        assert advice.kind is AdviceKind.READY_FOR_DOT

    def test_judging_uses_the_latest_confirm_not_the_best_or_closest_one(self):
        initial = ring(1.0, 1.0)
        centered = ring(0.0, 0.0)
        off = ring(0.1 * IN_PER_MIL_100YD, 0.3 * IN_PER_MIL_100YD)

        worse_last = step2_confirm_advice(
            initial, self.DIALED, [centered, off], origins_for(initial, centered, off)
        )
        better_last = step2_confirm_advice(
            initial, self.DIALED, [off, centered], origins_for(initial, off, centered)
        )
        middle_off = step2_confirm_advice(
            initial,
            self.DIALED,
            [centered, off, centered],
            origins_for(initial, centered, off, centered),
        )
        last_off = step2_confirm_advice(
            initial,
            self.DIALED,
            [centered, centered, off],
            origins_for(initial, centered, centered, off),
        )

        assert worse_last.kind is AdviceKind.CONFIRM_AGAIN
        assert worse_last.correction == Correction(-0.3, -0.1, "Dial DOWN 0.3, LEFT 0.1")
        assert better_last.kind is AdviceKind.READY_FOR_DOT
        assert middle_off.kind is AdviceKind.READY_FOR_DOT
        assert last_off.kind is AdviceKind.CONFIRM_AGAIN
        assert last_off.correction == Correction(-0.3, -0.1, "Dial DOWN 0.3, LEFT 0.1")

    def test_twenty_shots_are_not_labeled_very_high_confidence(self):
        advice = self._confirm((0.0, 0.0))

        assert advice.stats is not None and advice.stats.n == 20
        assert "very high confidence" not in advice.message

    def test_estimated_shot_marks_the_overlay_stats(self):
        initial = ring(1.0, 1.0)
        confirm = ring(0.0, 0.0)
        origins = [D] * 19 + [E]

        advice = step2_confirm_advice(initial, self.DIALED, [confirm], origins)

        assert advice.stats is not None and advice.stats.includes_estimated

    def test_mismatched_origins_are_rejected(self):
        initial, confirm = ring(1.0, 1.0), ring(0.0, 0.0)

        with pytest.raises(ValueError, match="origins_all"):
            step2_confirm_advice(initial, self.DIALED, [confirm], [D] * 19)


class TestSuggestDot:
    def test_forms_example_one_point_two_inch_group_gets_a_one_inch_dot(self):
        assert suggest_dot(1.2, 0.224) == 1.0

    @pytest.mark.parametrize(
        ("es_in", "bullet_in", "dot"),
        [
            (0.3, 0.224, 0.5),
            (0.5, 0.224, 0.5),
            (0.9, 0.224, 0.75),
            (1.0, 0.224, 0.75),
            (1.5, 0.224, 1.25),
            (2.0, 0.224, 2.0),
            (2.8, 0.308, 2.5),
            (3.5, 0.224, 3.0),
            (9.0, 0.308, 3.0),
            (0.0, 0.224, 0.5),
        ],
    )
    def test_nearest_standard_dot_to_es_minus_bullet(self, es_in, bullet_in, dot):
        assert suggest_dot(es_in, bullet_in) == dot

    def test_exact_standard_sizes_map_to_themselves(self):
        for dot in STANDARD_DOTS_IN:
            assert suggest_dot(dot + 0.25, 0.25) == dot

    def test_a_tie_goes_to_the_larger_dot(self):
        assert suggest_dot(0.625 + 0.25, 0.25) == 0.75

    def test_result_is_always_a_standard_dot(self):
        rng = random.Random(5)
        for _ in range(200):
            assert suggest_dot(rng.uniform(0, 6), rng.uniform(0.17, 0.4)) in STANDARD_DOTS_IN

    @pytest.mark.parametrize(
        ("es_in", "bullet_in"),
        [(-0.1, 0.224), (float("nan"), 0.224), (float("inf"), 0.224), (1.0, 0.0), (1.0, -0.2)],
    )
    def test_invalid_inputs_are_rejected(self, es_in, bullet_in):
        with pytest.raises(ValueError):
            suggest_dot(es_in, bullet_in)


class TestZeroCheck:
    def test_shot_exactly_touching_the_dot_passes(self):
        # dot 1.0 in, bullet 0.25 in: pass radius 0.5 + 0.125 = 0.625 (exact in binary)
        result = zero_check(offs((0.625, 0.0)), 1.0, 0.25)

        assert result.passed
        assert result.per_shot_inside == (True,)
        assert result.message == ZERO_CHECK_OK_MESSAGE

    def test_a_thousandth_of_an_inch_further_fails(self):
        result = zero_check(offs((0.626, 0.0)), 1.0, 0.25)

        assert not result.passed
        assert result.per_shot_inside == (False,)
        assert result.message == ZERO_CHECK_LOST_ZERO_MESSAGE

    def test_touching_boundary_is_measured_in_any_direction(self):
        for dx, dy in [(0.0, 0.625), (-0.625, 0.0), (0.375, -0.5)]:
            assert zero_check(offs((dx, dy)), 1.0, 0.25).passed

    def test_realistic_sizes_at_the_boundary_pass(self):
        dot, bullet = 0.75, 0.224
        radius = dot / 2 + bullet / 2

        assert zero_check(offs((radius, 0.0)), dot, bullet).passed
        assert not zero_check(offs((radius + 0.001, 0.0)), dot, bullet).passed

    def test_two_shots_must_both_be_inside(self):
        result = zero_check(offs((0.1, 0.1), (0.9, 0.0)), 1.0, 0.25)

        assert not result.passed
        assert result.per_shot_inside == (True, False)

    def test_two_shots_inside_pass(self):
        result = zero_check(offs((0.1, -0.1), (-0.3, 0.2)), 1.0, 0.25)

        assert result.passed
        assert result.per_shot_inside == (True, True)

    @pytest.mark.parametrize("count", [0, 3, 10])
    def test_zero_or_more_than_two_shots_are_rejected(self, count):
        with pytest.raises(ValueError, match="1 or 2 shots"):
            zero_check(offs(*[(0.0, 0.0)] * count), 1.0, 0.25)

    @pytest.mark.parametrize(
        ("dot", "bullet"), [(0.0, 0.25), (-1.0, 0.25), (1.0, 0.0), (1.0, -0.1)]
    )
    def test_non_positive_sizes_are_rejected(self, dot, bullet):
        with pytest.raises(ValueError):
            zero_check(offs((0.0, 0.0)), dot, bullet)

    def test_fail_message_names_mount_before_scope_and_routes_to_step_2(self):
        message = ZERO_CHECK_LOST_ZERO_MESSAGE

        assert message == (
            "Possible lost zero. Check the mount first (#1 cause), then the scope (#2 cause), "
            "then go back to Step 2."
        )
        assert message.index("mount") < message.index("scope") < message.index("Step 2")


class TestTemplatesPassTheSchema:
    """Every fixed template, and every real advice, must be accepted by the response models."""

    def _templates(self) -> list[tuple[AdviceKind, str]]:
        cases = [(AdviceKind.INVALID, "No shots to analyze: add at least 1 shot.")]
        cases += [(AdviceKind.SHOOT_MORE, shoot_more_text(n)) for n in range(0, 10)]
        cases += [(AdviceKind.SHOOT_MORE, step1_more_text(n)) for n in range(4, 10)]
        cases += [
            (
                AdviceKind.SHOOT_MORE,
                f"Step 1 only corrects from 1 to 3 shots. This group has {n} shots: "
                "use Step 2 for the correction.",
            )
            for n in (10, 11, 30)
        ]
        cases.append(
            (
                AdviceKind.INVALID,
                "The first Step 2 group needs 10 shots before a confirm group can be judged: "
                "6 of 10 so far.",
            )
        )
        cases += [
            (AdviceKind.INVALID, confirm_short_text(number, shots))
            for number in (1, 2, 3)
            for shots in range(0, 10)
        ]
        cases += [
            (AdviceKind.INVALID, ZERO_CHECK_LOST_ZERO_MESSAGE),
            (AdviceKind.INVALID, ZERO_CHECK_OK_MESSAGE),
        ]
        return cases

    def test_each_fixed_template_validates_as_shoot_more_and_invalid_advice(self):
        for _, message in self._templates():
            for strict_kind in (AdviceKind.SHOOT_MORE, AdviceKind.INVALID):
                AdviceOut(kind=strict_kind, message=message)

    def test_schema_guard_rejects_a_template_with_a_number_and_direction(self):
        for message in ("Shoot more: group is 0.3 high", "Shoot more, 2 right", "Dial DOWN first"):
            with pytest.raises(ValueError):
                AdviceOut(kind=AdviceKind.SHOOT_MORE, message=message)

    def test_domain_templates_match_the_expected_strings_the_tests_assert(self):
        produced = {
            step1_advice([]).message,
            *(
                step2_initial_advice(random_group(random.Random(n), n), [D] * n).message
                for n in range(1, 10)
            ),
            *(step1_advice(random_group(random.Random(n), n)).message for n in range(4, 12)),
            *(
                step2_confirm_advice(
                    ring(0.0, 0.0),
                    NO_ADJUSTMENT,
                    [random_group(random.Random(n), n), ring(0.0, 0.0)],
                    [D] * (20 + n),
                ).message
                for n in range(0, 10)
            ),
        }
        expected = {message for _, message in self._templates()}

        assert produced <= expected

    def test_zero_check_messages_validate_through_zero_check_out(self):
        for passed in (True, False):
            result = zero_check(offs((0.1, 0.0) if passed else (5.0, 0.0)), 1.0, 0.25)

            out = ZeroCheckOut(
                id=1,
                zero_id=1,
                group_id=1,
                result="pass" if result.passed else "possible_lost_zero",
                message=result.message,
                per_shot_inside=list(result.per_shot_inside),
                created_at="2026-10-01T00:00:00Z",
            )

            assert out.message == result.message

    def test_real_advice_of_every_kind_validates_through_advice_out(self):
        initial, confirm = ring(1.0, 1.0), ring(0.0, 0.0)
        advices = [
            step1_advice(offs((0.36, 0.72))),
            step1_advice(offs((0.0, 0.0))),
            step1_advice(random_group(random.Random(1), 5)),
            step1_advice([]),
            step2_initial_advice(ring(1.08, 0.72), [D] * 10),
            step2_initial_advice(ring(0.0, 0.0), [D] * 10),
            step2_initial_advice(random_group(random.Random(2), 4), [D] * 4),
            step2_confirm_advice(initial, NO_ADJUSTMENT, [confirm], [D] * 20),
            step2_confirm_advice(initial, NO_ADJUSTMENT, [ring(0.36, 0.0)], [D] * 20),
            step2_confirm_advice(initial, NO_ADJUSTMENT, [ring(1.0, 0.0)], [D] * 20),
            step2_confirm_advice(initial, NO_ADJUSTMENT, [], [D] * 10),
        ]
        assert {a.kind for a in advices} == set(AdviceKind)

        for advice in advices:
            out = AdviceOut.model_validate(advice)
            assert out.kind is advice.kind


class TestNoBallisticAppAdvice:
    def _all_messages(self) -> list[str]:
        initial, confirm = ring(1.0, 1.0), ring(0.3, 0.2)
        rng = random.Random(9)
        advices = [step1_advice(random_group(rng, n)) for n in range(0, 13)]
        advices += [step2_initial_advice(random_group(rng, n), [D] * n) for n in range(0, 13)]
        advices += [
            step2_confirm_advice(initial, NO_ADJUSTMENT, [confirm], [D] * 20),
            step2_confirm_advice(initial, NO_ADJUSTMENT, [ring(0.0, 0.0)], [D] * 20),
            step2_confirm_advice(initial, NO_ADJUSTMENT, [ring(0.0, 0.0)] * 2, [D] * 30),
            step2_confirm_advice(initial, NO_ADJUSTMENT, [], [D] * 10),
        ]
        messages = [a.message for a in advices] + [
            ZERO_CHECK_OK_MESSAGE,
            ZERO_CHECK_LOST_ZERO_MESSAGE,
        ]
        return messages + [a.correction.text for a in advices if a.correction]

    def test_no_message_recommends_an_app_offset(self):
        for message in self._all_messages():
            assert not re.search(r"ballistic|\bapp\b|offset|solver", message, re.I), message

    def test_module_source_has_no_ballistic_app_wording(self):
        source = Path(zeroing.__file__).read_text(encoding="utf-8")

        assert not re.search(r"ballistic|\bapp\b", source, re.I)

    def test_domain_module_does_not_import_the_web_layer(self):
        source = Path(zeroing.__file__).read_text(encoding="utf-8")

        assert "rga.web" not in source and "rga.services" not in source


class TestMasksPoi:
    """The canonical masking rule lives in the domain (B-43)."""

    @pytest.mark.parametrize(
        ("shots", "masked"),
        [(0, False), (1, False), (3, False), (4, True), (9, True), (10, True), (50, True)],
    )
    def test_step_1_masks_above_three_shots(self, shots, masked):
        assert masks_poi("zero_step1", shots) is masked

    @pytest.mark.parametrize("purpose", ["zero_step2", "zero_confirm"])
    @pytest.mark.parametrize(
        ("shots", "masked"),
        [(0, True), (1, True), (9, True), (10, False), (11, False), (50, False)],
    )
    def test_step_2_and_confirm_mask_below_ten_shots(self, purpose, shots, masked):
        assert masks_poi(purpose, shots) is masked

    @pytest.mark.parametrize("shots", [0, 1, 2, 3, 9, 10, 40])
    def test_zero_check_is_always_masked_and_general_never(self, shots):
        assert masks_poi("zero_check", shots) is True
        assert masks_poi("general", shots) is False

    def test_enum_members_and_strings_give_the_same_answer(self):
        from rga.domain.types import GroupPurpose

        for purpose in GroupPurpose:
            for shots in range(0, 15):
                assert masks_poi(purpose, shots) is masks_poi(purpose.value, shots)

    def test_an_unknown_purpose_is_not_masked(self):
        assert masks_poi("something_else", 2) is False
