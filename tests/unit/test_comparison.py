"""T1 tests for rga.domain.comparison: tiers, status, verdict, pair comparison, POI shift."""

import math
import re

import pytest

from rga.domain import comparison as cmp_module
from rga.domain.comparison import (
    HIGH_CONFIDENCE_ROUNDS,
    MIN_ROUNDS,
    NORMAL_GROUP_VARIATION,
    ZERO_TO_HUNTING_LOAD_ADVICE,
    CompareGroup,
    LoadSummary,
    TierSummary,
    compare_pair,
    conditions_warning,
    poi_shift_mil,
    precision_verdict,
    summarize_load,
    tier_for,
)
from rga.domain.types import Position

BENCH = Position.BENCH
PRONE = Position.PRONE_OFF_PACK


def grp(es: float, n: int = 10, position=BENCH, suppressor=False, estimated=False) -> CompareGroup:
    return CompareGroup(
        es_moa=es, n=n, position=position, suppressor=suppressor, includes_estimated=estimated
    )


def load_with_means(tier10: float | None, tier30: float | None = None, load_id: int = 1):
    groups = []
    if tier10 is not None:
        groups.append(grp(tier10, 10))
    if tier30 is not None:
        groups.append(grp(tier30, 30))
    return summarize_load(load_id, groups)


def test_constants_match_the_spec():
    assert NORMAL_GROUP_VARIATION == 0.30
    assert MIN_ROUNDS == 10
    assert HIGH_CONFIDENCE_ROUNDS == 20


class TestTierFor:
    @pytest.mark.parametrize(
        ("shots", "tier"),
        [
            (1, None),
            (9, None),
            (10, "tier10"),
            (15, "tier10"),
            (19, "tier10"),
            (20, None),
            (25, None),
            (29, None),
            (30, "tier30"),
            (31, "tier30"),
            (90, "tier30"),
        ],
    )
    def test_boundaries(self, shots, tier):
        assert tier_for(shots) == tier


class TestSummarizeLoad:
    def test_no_groups_is_insufficient_with_empty_tiers(self):
        s = summarize_load(7, [])
        empty = TierSummary(rounds=0, groups=0, mean_es_moa=None, worst_es_moa=None)
        assert s == LoadSummary(7, empty, empty, "insufficient_data", None)

    def test_groups_under_ten_shots_are_never_eligible(self):
        s = summarize_load(1, [grp(0.5, 5), grp(0.6, 9), grp(0.7, 3)])
        assert s.tier10.groups == 0
        assert s.tier30.groups == 0
        assert s.status == "insufficient_data"
        assert s.precision_verdict is None

    def test_one_ten_shot_group_is_ok(self):
        s = summarize_load(1, [grp(1.2, 10)])
        assert s.tier10 == TierSummary(rounds=10, groups=1, mean_es_moa=1.2, worst_es_moa=1.2)
        assert s.status == "ok"

    def test_two_ten_shot_groups_are_high_confidence(self):
        s = summarize_load(1, [grp(1.0, 10), grp(2.0, 10)])
        assert s.tier10.rounds == 20
        assert s.status == "high_confidence"
        assert s.tier10.mean_es_moa == pytest.approx(1.5)
        assert s.tier10.worst_es_moa == 2.0

    def test_nineteen_round_tier10_is_ok_and_twenty_is_high_confidence(self):
        assert summarize_load(1, [grp(1.0, 10), grp(1.0, 9)]).status == "ok"
        assert summarize_load(1, [grp(1.0, 19), grp(1.0, 1)]).status == "ok"
        assert summarize_load(1, [grp(1.0, 10), grp(1.0, 10)]).status == "high_confidence"

    def test_a_single_nineteen_shot_group_is_ok_not_high_confidence(self):
        assert summarize_load(1, [grp(1.0, 19)]).status == "ok"

    def test_twenty_to_twenty_nine_shot_groups_belong_to_no_tier(self):
        s = summarize_load(1, [grp(1.0, 20), grp(1.1, 25), grp(1.2, 29)])
        assert s.tier10.groups == 0
        assert s.tier30.groups == 0
        assert s.status == "insufficient_data"

    def test_twenty_shot_group_does_not_add_to_tier10_rounds(self):
        s = summarize_load(1, [grp(1.0, 10), grp(1.0, 20)])
        assert s.tier10.rounds == 10
        assert s.status == "ok"

    def test_tier30_is_summarized_separately(self):
        s = summarize_load(1, [grp(1.0, 10), grp(2.0, 30), grp(3.0, 40)])
        assert s.tier30 == TierSummary(rounds=70, groups=2, mean_es_moa=2.5, worst_es_moa=3.0)
        assert s.tier10 == TierSummary(rounds=10, groups=1, mean_es_moa=1.0, worst_es_moa=1.0)

    def test_load_with_only_a_thirty_shot_group_is_insufficient_for_tier10(self):
        s = summarize_load(1, [grp(1.0, 30)])
        assert s.tier30.groups == 1
        assert s.status == "insufficient_data"
        assert s.precision_verdict is None

    def test_verdict_uses_tier10_mean_only_and_is_not_penalized_by_a_thirty_shot_group(self):
        mixed = summarize_load(1, [grp(1.5, 10), grp(2.6, 30)])
        only10 = summarize_load(2, [grp(1.5, 10)])
        assert mixed.precision_verdict == "sufficient for deer/elk to 800 yd"
        assert mixed.precision_verdict == only10.precision_verdict
        assert mixed.tier10 == only10.tier10
        assert mixed.status == only10.status
        assert compare_pair(mixed, only10) == "no_real_difference"
        assert compare_pair(only10, mixed) == "no_real_difference"

    def test_thirty_shot_group_does_not_change_the_tier10_ranking(self):
        good_with_big_group = summarize_load(1, [grp(1.0, 10), grp(4.0, 30)])
        plain = summarize_load(2, [grp(2.0, 10)])
        assert compare_pair(good_with_big_group, plain) == "a_smaller"

    def test_estimated_shots_count_like_any_other(self):
        with_est = summarize_load(1, [grp(1.0, 10, estimated=True), grp(1.0, 10)])
        assert with_est.status == "high_confidence"
        assert with_est.tier10.groups == 2

    def test_load_id_is_carried(self):
        assert summarize_load(42, []).load_id == 42

    @pytest.mark.parametrize("bad_n", [0, -1])
    def test_group_without_shots_is_rejected(self, bad_n):
        with pytest.raises(ValueError, match="at least one shot"):
            summarize_load(1, [grp(1.0, bad_n)])

    @pytest.mark.parametrize("bad_es", [-0.1, math.nan, math.inf])
    def test_bad_es_is_rejected(self, bad_es):
        with pytest.raises(ValueError, match="es_moa"):
            summarize_load(1, [grp(bad_es, 10)])

    @pytest.mark.parametrize("shots", [5, 25, 30])
    def test_bad_es_is_rejected_for_groups_outside_tier10_too(self, shots):
        with pytest.raises(ValueError, match="es_moa"):
            summarize_load(1, [grp(math.nan, shots)])
        with pytest.raises(ValueError, match="es_moa"):
            summarize_load(1, [grp(-1.0, shots)])

    def test_zero_es_is_valid(self):
        assert summarize_load(1, [grp(0.0, 10)]).precision_verdict == (
            "sufficient for deer/elk to 800 yd"
        )


class TestPrecisionVerdict:
    @pytest.mark.parametrize(
        ("es", "text"),
        [
            (0.0, "sufficient for deer/elk to 800 yd"),
            (0.8, "sufficient for deer/elk to 800 yd"),
            (1.5, "sufficient for deer/elk to 800 yd"),
            (1.5001, "sufficient for deer/elk to 600 yd"),
            (1.99, "sufficient for deer/elk to 600 yd"),
            (2.0, "sufficient for deer/elk to 600 yd"),
            (2.0001, "above Form's 2 MOA / 10-shot bar"),
            (5.0, "above Form's 2 MOA / 10-shot bar"),
        ],
    )
    def test_bands_at_1_5_and_2_0(self, es, text):
        assert precision_verdict(es) == text

    @pytest.mark.parametrize("bad", [-0.01, math.nan, math.inf, -math.inf])
    def test_invalid_input_is_rejected(self, bad):
        with pytest.raises(ValueError):
            precision_verdict(bad)

    def test_summary_verdict_follows_the_tier10_mean(self):
        assert summarize_load(1, [grp(1.3, 10), grp(1.6, 10)]).precision_verdict == (
            "sufficient for deer/elk to 800 yd"
        )
        assert summarize_load(1, [grp(1.6, 10)]).precision_verdict == (
            "sufficient for deer/elk to 600 yd"
        )
        assert summarize_load(1, [grp(2.4, 10)]).precision_verdict == (
            "above Form's 2 MOA / 10-shot bar"
        )


class TestComparePair:
    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            (1.0, 1.0, "no_real_difference"),
            (17.0, 23.0, "no_real_difference"),
            (1.19, 1.61, "no_real_difference"),
            (2.55, 3.45, "no_real_difference"),
            (1.0, 1.29, "no_real_difference"),
            (1.0, 1.34, "no_real_difference"),
            (1.0, 1.36, "a_smaller"),
            (1.36, 1.0, "b_smaller"),
            (0.855, 1.145, "no_real_difference"),
            (0.845, 1.155, "a_smaller"),
            (1.155, 0.845, "b_smaller"),
        ],
    )
    def test_thirty_percent_of_pair_mean_threshold(self, a, b, expected):
        assert compare_pair(load_with_means(a), load_with_means(b)) == expected

    def test_relative_difference_029_is_no_real_difference_and_031_is_ranked(self):
        def rel_pair(rel):
            return load_with_means(1 - rel / 2), load_with_means(1 + rel / 2)

        a, b = rel_pair(0.29)
        assert compare_pair(a, b) == "no_real_difference"
        a, b = rel_pair(0.31)
        assert compare_pair(a, b) == "a_smaller"

    def test_exact_boundary_is_no_real_difference_despite_float_noise(self):
        a, b = load_with_means(0.85), load_with_means(1.15)
        assert compare_pair(a, b) == "no_real_difference"

    def test_threshold_is_relative_to_pair_mean_not_to_either_load(self):
        assert compare_pair(load_with_means(1.0), load_with_means(1.35)) == "no_real_difference"
        assert compare_pair(load_with_means(1.0), load_with_means(1.36)) == "a_smaller"

    def test_identical_zero_means_are_no_real_difference(self):
        assert compare_pair(load_with_means(0.0), load_with_means(0.0)) == "no_real_difference"

    def test_zero_versus_positive_is_ranked(self):
        assert compare_pair(load_with_means(0.0), load_with_means(1.0)) == "a_smaller"

    def test_is_symmetric(self):
        for x, y in [(1.0, 1.1), (1.0, 2.0), (0.7, 0.71)]:
            ab = compare_pair(load_with_means(x), load_with_means(y))
            ba = compare_pair(load_with_means(y), load_with_means(x))
            flipped = {"a_smaller": "b_smaller", "b_smaller": "a_smaller"}.get(ba, ba)
            assert ab == flipped

    def test_missing_tier10_on_either_side_is_insufficient(self):
        have, missing = load_with_means(1.0), load_with_means(None)
        assert compare_pair(have, missing) == "insufficient_data"
        assert compare_pair(missing, have) == "insufficient_data"
        assert compare_pair(missing, missing) == "insufficient_data"

    def test_sub_ten_shot_loads_are_insufficient(self):
        few = summarize_load(1, [grp(0.5, 9)])
        assert compare_pair(few, load_with_means(1.0)) == "insufficient_data"

    def test_tier30_compares_only_tier30_data(self):
        a = load_with_means(1.0, 1.0, load_id=1)
        b = load_with_means(1.0, 2.0, load_id=2)
        assert compare_pair(a, b, "tier10") == "no_real_difference"
        assert compare_pair(a, b, "tier30") == "a_smaller"

    def test_tier30_missing_on_one_side_is_insufficient_while_tier10_still_ranks(self):
        a = load_with_means(1.0, 1.0)
        b = load_with_means(2.0, None)
        assert compare_pair(a, b, "tier30") == "insufficient_data"
        assert compare_pair(a, b, "tier10") == "a_smaller"

    def test_tier10_missing_does_not_borrow_from_tier30(self):
        a = load_with_means(None, 1.0)
        b = load_with_means(None, 3.0)
        assert compare_pair(a, b, "tier10") == "insufficient_data"
        assert compare_pair(a, b, "tier30") == "a_smaller"

    def test_default_tier_is_tier10(self):
        a, b = load_with_means(1.0, 1.0), load_with_means(3.0, 1.0)
        assert compare_pair(a, b) == compare_pair(a, b, "tier10") == "a_smaller"

    def test_unknown_tier_is_rejected(self):
        with pytest.raises(ValueError, match="tier"):
            compare_pair(load_with_means(1.0), load_with_means(1.0), "tier20")  # type: ignore[arg-type]

    def test_twenty_to_twenty_nine_shot_groups_never_rank(self):
        a = summarize_load(1, [grp(1.0, 25)])
        b = summarize_load(2, [grp(3.0, 25)])
        assert compare_pair(a, b, "tier10") == "insufficient_data"
        assert compare_pair(a, b, "tier30") == "insufficient_data"

    def test_verdicts_are_the_schema_pair_verdict_values(self):
        from typing import get_args

        from rga.web.schemas import CompareStatus, PairVerdict

        assert set(get_args(cmp_module.PairVerdict)) == set(get_args(PairVerdict))
        assert set(get_args(cmp_module.LoadStatus)) == set(get_args(CompareStatus))


class TestPoiShift:
    def test_no_pairs_is_none(self):
        assert poi_shift_mil([]) is None

    def test_single_pair_is_load_minus_zero(self):
        assert poi_shift_mil([((0.5, 0.2), (0.1, 0.5))]) == pytest.approx((0.4, -0.3))

    def test_mean_over_shared_buckets(self):
        pairs = [((0.4, 0.0), (0.0, 0.0)), ((0.0, 0.4), (0.2, 0.0))]
        assert poi_shift_mil(pairs) == pytest.approx((0.1, 0.2))

    def test_a_pair_is_a_shared_day_distance_position_and_suppressor_bucket(self):
        doc = (poi_shift_mil.__doc__ or "").lower()
        assert "bucket" in doc and "session" not in doc

    def test_right_and_up_are_positive(self):
        right, up = poi_shift_mil([((1.0, 1.0), (0.0, 0.0))])
        assert right > 0
        assert up > 0
        left, down = poi_shift_mil([((0.0, 0.0), (1.0, 1.0))])
        assert left < 0
        assert down < 0

    def test_load_equal_to_zero_load_has_no_shift(self):
        assert poi_shift_mil([((0.3, -0.2), (0.3, -0.2))]) == pytest.approx((0.0, 0.0))

    def test_returns_a_two_tuple_of_floats(self):
        result = poi_shift_mil([((0.1, 0.1), (0.0, 0.0))])
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_non_finite_values_are_rejected(self):
        with pytest.raises(ValueError, match="finite"):
            poi_shift_mil([((math.nan, 0.0), (0.0, 0.0))])
        with pytest.raises(ValueError, match="finite"):
            poi_shift_mil([((0.0, 0.0), (0.0, math.inf))])


class TestConditionsWarning:
    def test_matching_conditions_have_no_warning(self):
        a = [grp(1.0, position=BENCH, suppressor=False)]
        b = [grp(1.2, position=BENCH, suppressor=False)]
        assert conditions_warning(a, b) is None

    def test_position_mismatch_warns_and_names_position_only(self):
        warning = conditions_warning([grp(1.0, position=BENCH)], [grp(1.0, position=PRONE)])
        assert warning is not None
        assert "position" in warning
        assert "suppressor" not in warning

    def test_suppressor_mismatch_warns_and_names_suppressor_only(self):
        warning = conditions_warning([grp(1.0, suppressor=True)], [grp(1.0, suppressor=False)])
        assert warning is not None
        assert "suppressor" in warning
        assert "position" not in warning

    def test_both_mismatches_name_both(self):
        warning = conditions_warning(
            [grp(1.0, position=BENCH, suppressor=True)],
            [grp(1.0, position=PRONE, suppressor=False)],
        )
        assert "position" in warning
        assert "suppressor" in warning

    def test_mixed_conditions_within_a_load_are_compared_as_sets(self):
        a = [grp(1.0, position=BENCH), grp(1.0, position=PRONE)]
        b = [grp(1.0, position=PRONE), grp(1.0, position=BENCH)]
        assert conditions_warning(a, b) is None
        assert conditions_warning(a, [grp(1.0, position=BENCH)]) is not None

    def test_one_side_being_a_subset_of_the_other_still_warns_in_both_orders(self):
        wide = [grp(1.0, position=BENCH), grp(1.0, position=PRONE)]
        narrow = [grp(1.0, position=BENCH)]
        assert conditions_warning(wide, narrow) is not None
        assert conditions_warning(narrow, wide) is not None
        mixed = [grp(1.0, suppressor=True), grp(1.0, suppressor=False)]
        assert conditions_warning(mixed, [grp(1.0, suppressor=True)]) is not None
        assert conditions_warning([grp(1.0, suppressor=True)], mixed) is not None

    def test_an_empty_side_has_nothing_to_compare(self):
        assert conditions_warning([], [grp(1.0)]) is None
        assert conditions_warning([grp(1.0)], []) is None
        assert conditions_warning([], []) is None

    def test_is_symmetric(self):
        a, b = [grp(1.0, position=BENCH)], [grp(1.0, position=PRONE)]
        assert conditions_warning(a, b) == conditions_warning(b, a)


class TestAdvice:
    def test_says_to_zero_to_the_hunting_load(self):
        text = ZERO_TO_HUNTING_LOAD_ADVICE.lower()
        assert "zero" in text
        assert "hunting load" in text

    def test_never_uses_offset_wording(self):
        assert "offset" not in ZERO_TO_HUNTING_LOAD_ADVICE.lower()

    def test_never_tells_the_user_to_enter_an_app_correction(self):
        text = ZERO_TO_HUNTING_LOAD_ADVICE.lower()
        assert not re.search(r"\b(enter|dial|apply|add|use)\b[^.;]*\b(app|offset|correction)", text)


def test_tier_summary_fields_match_the_api_schema():
    from rga.web.schemas import LoadComparisonOut, TierSummaryOut

    assert set(TierSummary.__dataclass_fields__) == set(TierSummaryOut.model_fields)
    domain = set(LoadSummary.__dataclass_fields__)
    assert {"tier10", "tier30", "status", "precision_verdict"} <= domain
    assert {"tier10", "tier30", "status", "precision_verdict"} <= set(
        LoadComparisonOut.model_fields
    )


def test_every_summary_converts_to_the_schema_model():
    from rga.web.schemas import TierSummaryOut

    s = summarize_load(1, [grp(1.0, 10), grp(1.4, 12), grp(2.0, 30)])
    for tier in (s.tier10, s.tier30):
        out = TierSummaryOut(**vars(tier))
        assert out.rounds == tier.rounds
