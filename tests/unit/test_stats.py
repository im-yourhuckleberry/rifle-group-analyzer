"""T1 tests for rga.domain.stats: extreme spread, mean POI, includes_estimated, aggregate."""

import itertools
import math
import random

import pytest

from rga.domain import stats as stats_module
from rga.domain.stats import (
    AggregateInput,
    AggregateStats,
    aggregate,
    extreme_spread_in,
    group_stats,
)
from rga.domain.types import GroupStats, Offset, ShotOrigin
from rga.domain.units import in_to_mil, in_to_moa

D = ShotOrigin.DETECTED
E = ShotOrigin.ESTIMATED
MOA_IN_PER_100YD = 1.0471975512


def offs(*pairs: tuple[float, float]) -> list[Offset]:
    return [Offset(dx_in=x, dy_in=y) for x, y in pairs]


def brute_force_es(points: list[tuple[float, float]]) -> float:
    return max((math.dist(a, b) for a, b in itertools.combinations(points, 2)), default=0.0)


def random_points(rng: random.Random, n: int) -> list[tuple[float, float]]:
    return [(rng.uniform(-3, 3), rng.uniform(-3, 3)) for _ in range(n)]


class TestExtremeSpread:
    def test_empty_and_single_shot_are_zero(self):
        assert extreme_spread_in([]) == 0.0
        assert extreme_spread_in(offs((1.5, -2.0))) == 0.0

    def test_two_points_is_their_distance(self):
        assert extreme_spread_in(offs((0, 0), (3, 4))) == pytest.approx(5.0)

    def test_collinear_points_use_the_two_extremes(self):
        assert extreme_spread_in(offs((0, 0), (1, 1), (2, 2), (3, 3))) == pytest.approx(
            3 * math.sqrt(2)
        )

    def test_square_is_its_diagonal(self):
        assert extreme_spread_in(offs((0, 0), (1, 0), (1, 1), (0, 1))) == pytest.approx(
            math.sqrt(2)
        )

    def test_coincident_shots_are_zero(self):
        assert extreme_spread_in(offs((0.5, 0.5), (0.5, 0.5), (0.5, 0.5))) == 0.0

    def test_is_center_to_center_not_edge_to_edge(self):
        assert extreme_spread_in(offs((0, 0), (1.0, 0))) == pytest.approx(1.0)

    @pytest.mark.parametrize("seed", range(30))
    def test_matches_brute_force_on_random_sets(self, seed):
        rng = random.Random(seed)
        pts = random_points(rng, rng.randint(2, 40))
        assert extreme_spread_in(offs(*pts)) == pytest.approx(brute_force_es(pts), abs=1e-12)

    def test_translation_and_order_invariant(self):
        pts = random_points(random.Random(99), 12)
        base = extreme_spread_in(offs(*pts))
        shifted = extreme_spread_in(offs(*[(x + 10, y - 7) for x, y in pts]))
        shuffled = list(pts)
        random.Random(1).shuffle(shuffled)
        assert shifted == pytest.approx(base, abs=1e-12)
        assert extreme_spread_in(offs(*shuffled)) == base

    def test_non_finite_offsets_are_rejected(self):
        with pytest.raises(ValueError, match="finite"):
            extreme_spread_in(offs((0, 0), (math.nan, 1)))
        with pytest.raises(ValueError, match="finite"):
            extreme_spread_in(offs((0, 0), (math.inf, 1)))


class TestGroupStats:
    def test_returns_group_stats_with_count(self):
        s = group_stats(offs((0, 0), (1, 0), (0, 1)), [D, D, D], 100)
        assert isinstance(s, GroupStats)
        assert s.n == 3

    def test_mean_poi_in_inches_and_mil(self):
        s = group_stats(offs((1.8, 3.6), (3.6, 0.0)), [D, D], 100)
        assert s.mean_dx_in == pytest.approx(2.7)
        assert s.mean_dy_in == pytest.approx(1.8)
        assert s.mean_dx_mil == pytest.approx(0.75)
        assert s.mean_dy_mil == pytest.approx(0.5)

    def test_mil_scales_with_distance(self):
        s = group_stats(offs((10.8, -10.8)), [D], 300)
        assert s.mean_dx_mil == pytest.approx(1.0)
        assert s.mean_dy_mil == pytest.approx(-1.0)
        assert s.mean_dx_in == pytest.approx(10.8)

    def test_es_moa_conversion(self):
        s = group_stats(offs((0, 0), (MOA_IN_PER_100YD, 0)), [D, D], 100)
        assert s.es_in == pytest.approx(MOA_IN_PER_100YD)
        assert s.es_moa == pytest.approx(1.0, abs=1e-9)
        at_300 = group_stats(offs((0, 0), (3 * MOA_IN_PER_100YD, 0)), [D, D], 300)
        assert at_300.es_moa == pytest.approx(1.0, abs=1e-9)

    def test_moa_and_mil_agree_with_unit_functions(self):
        pts = random_points(random.Random(5), 10)
        s = group_stats(offs(*pts), [D] * 10, 275)
        assert s.es_moa == in_to_moa(s.es_in, 275)
        assert s.mean_dx_mil == in_to_mil(s.mean_dx_in, 275)
        assert s.mean_dy_mil == in_to_mil(s.mean_dy_in, 275)

    def test_high_and_right_group_has_positive_means(self):
        s = group_stats(offs((0.4, 0.9), (0.8, 1.1), (0.6, 1.0)), [D] * 3, 100)
        assert s.mean_dx_in > 0
        assert s.mean_dy_in > 0
        assert s.mean_dx_mil > 0
        assert s.mean_dy_mil > 0

    def test_low_and_left_group_has_negative_means(self):
        s = group_stats(offs((-0.5, -0.7), (-0.7, -0.9)), [D, D], 100)
        assert s.mean_dx_in < 0
        assert s.mean_dy_in < 0

    def test_single_shot_group(self):
        s = group_stats(offs((0.3, -0.2)), [D], 100)
        assert (s.n, s.es_in, s.es_moa) == (1, 0.0, 0.0)
        assert s.mean_dx_in == pytest.approx(0.3)
        assert s.mean_dy_in == pytest.approx(-0.2)

    def test_es_and_mean_are_independent_of_each_other(self):
        base = offs((0, 0), (1, 0), (0, 1), (1, 1))
        moved = offs(*[(x + 5, y + 5) for x, y in [(0, 0), (1, 0), (0, 1), (1, 1)]])
        a = group_stats(base, [D] * 4, 100)
        b = group_stats(moved, [D] * 4, 100)
        assert a.es_in == pytest.approx(b.es_in)
        assert b.mean_dx_in == pytest.approx(a.mean_dx_in + 5)

    def test_mean_is_numerically_stable_for_many_shots(self):
        shots = offs(*([(0.1, 0.1)] * 1000))
        s = group_stats(shots, [D] * 1000, 100)
        assert s.mean_dx_in == pytest.approx(0.1, abs=1e-15)

    @pytest.mark.parametrize("origin", [ShotOrigin.DETECTED, ShotOrigin.SPLIT, ShotOrigin.MANUAL])
    def test_non_estimated_origins_do_not_set_includes_estimated(self, origin):
        assert not group_stats(offs((0, 0), (1, 1)), [origin, D], 100).includes_estimated

    def test_any_estimated_shot_sets_includes_estimated(self):
        s = group_stats(offs((0, 0), (1, 1), (2, 0)), [D, D, E], 100)
        assert s.includes_estimated is True
        assert group_stats(offs((0, 0), (1, 1)), [E, E], 100).includes_estimated is True

    def test_estimated_shots_count_in_every_statistic(self):
        with_est = group_stats(offs((0, 0), (1, 0), (4, 0)), [D, D, E], 100)
        without = group_stats(offs((0, 0), (1, 0)), [D, D], 100)
        assert with_est.n == 3
        assert with_est.es_in == pytest.approx(4.0)
        assert without.es_in == pytest.approx(1.0)
        assert with_est.mean_dx_in == pytest.approx(5 / 3)

    def test_no_shots_is_rejected(self):
        with pytest.raises(ValueError, match="at least one"):
            group_stats([], [], 100)

    def test_origins_length_must_match(self):
        with pytest.raises(ValueError, match="one to one"):
            group_stats(offs((0, 0), (1, 1)), [D], 100)
        with pytest.raises(ValueError, match="one to one"):
            group_stats(offs((0, 0)), [D, D], 100)

    @pytest.mark.parametrize("yd", [0, -100, math.nan])
    def test_non_positive_distance_is_rejected(self, yd):
        with pytest.raises(ValueError):
            group_stats(offs((0, 0), (1, 1)), [D, D], yd)

    def test_non_finite_offset_is_rejected(self):
        with pytest.raises(ValueError, match="finite"):
            group_stats(offs((0, math.nan)), [D], 100)


def agg_input(pairs, distance=100, load_id=1, origins=None) -> AggregateInput:
    return AggregateInput(
        offsets=offs(*pairs),
        origins=origins if origins is not None else [D] * len(pairs),
        distance_yd=distance,
        load_id=load_id,
    )


class TestAggregate:
    def test_three_ten_shot_groups_give_thirty_shots(self):
        rng = random.Random(3)
        groups = [agg_input(random_points(rng, 10)) for _ in range(3)]
        result = aggregate(groups)
        assert isinstance(result, AggregateStats)
        assert result.n == 30

    def test_es_and_center_are_those_of_the_union(self):
        rng = random.Random(4)
        sets = [random_points(rng, 10) for _ in range(3)]
        union = [p for s in sets for p in s]
        result = aggregate([agg_input(s) for s in sets])
        assert result.es_in == pytest.approx(brute_force_es(union), abs=1e-12)
        assert result.center_dx_in == pytest.approx(sum(p[0] for p in union) / 30)
        assert result.center_dy_in == pytest.approx(sum(p[1] for p in union) / 30)
        assert result.es_moa == pytest.approx(in_to_moa(result.es_in, 100))
        assert result.center_dx_mil == pytest.approx(in_to_mil(result.center_dx_in, 100))
        assert result.center_dy_mil == pytest.approx(in_to_mil(result.center_dy_in, 100))

    def test_groups_are_overlaid_on_their_own_aim_points(self):
        high_right = agg_input([(1.0, 1.0), (1.2, 1.0)])
        low_left = agg_input([(-1.0, -1.0), (-1.2, -1.0)])
        result = aggregate([high_right, low_left])
        assert result.center_dx_in == pytest.approx(0.0, abs=1e-12)
        assert result.center_dy_in == pytest.approx(0.0, abs=1e-12)
        assert result.es_in == pytest.approx(math.dist((1.2, 1.0), (-1.2, -1.0)))

    def test_identical_groups_do_not_grow_the_spread(self):
        pts = [(0, 0), (1, 0), (0, 2)]
        single = aggregate([agg_input(pts)])
        triple = aggregate([agg_input(pts), agg_input(pts), agg_input(pts)])
        assert triple.n == 9
        assert triple.es_in == pytest.approx(single.es_in)

    def test_order_of_groups_does_not_matter(self):
        rng = random.Random(8)
        groups = [agg_input(random_points(rng, 7), load_id=i) for i in range(4)]
        forward = aggregate(groups)
        backward = aggregate(list(reversed(groups)))
        assert forward.es_in == pytest.approx(backward.es_in, abs=1e-12)
        assert forward.center_dx_in == pytest.approx(backward.center_dx_in, abs=1e-12)

    def test_single_group_matches_group_stats(self):
        pts = random_points(random.Random(2), 10)
        single = aggregate([agg_input(pts, distance=200)])
        direct = group_stats(offs(*pts), [D] * 10, 200)
        assert single.es_in == direct.es_in
        assert single.es_moa == direct.es_moa
        assert single.center_dx_mil == direct.mean_dx_mil
        assert single.center_dy_mil == direct.mean_dy_mil

    def test_mixed_distances_are_rejected(self):
        with pytest.raises(ValueError, match="different distances"):
            aggregate([agg_input([(0, 0)], distance=100), agg_input([(1, 1)], distance=200)])

    def test_mixed_loads_are_flagged_not_blocked(self):
        result = aggregate([agg_input([(0, 0)], load_id=1), agg_input([(1, 1)], load_id=2)])
        assert result.mixed_loads is True
        assert result.n == 2

    def test_same_load_is_not_flagged(self):
        result = aggregate([agg_input([(0, 0)], load_id=7), agg_input([(1, 1)], load_id=7)])
        assert result.mixed_loads is False

    def test_unknown_load_ids_do_not_count_as_mixed_with_each_other(self):
        result = aggregate([agg_input([(0, 0)], load_id=None), agg_input([(1, 1)], load_id=None)])
        assert result.mixed_loads is False
        assert aggregate(
            [agg_input([(0, 0)], load_id=None), agg_input([(1, 1)], load_id=3)]
        ).mixed_loads

    def test_single_group_is_never_mixed(self):
        assert aggregate([agg_input([(0, 0), (1, 1)])]).mixed_loads is False

    def test_includes_estimated_if_any_group_has_one(self):
        clean = agg_input([(0, 0), (1, 1)])
        flagged = agg_input([(0, 1), (1, 0)], origins=[D, E])
        assert aggregate([clean, flagged]).includes_estimated is True
        assert aggregate([clean, clean]).includes_estimated is False

    def test_no_groups_is_rejected(self):
        with pytest.raises(ValueError, match="at least one group"):
            aggregate([])

    def test_groups_without_any_shot_are_rejected(self):
        with pytest.raises(ValueError, match="at least one"):
            aggregate([agg_input([])])

    def test_empty_group_beside_a_real_one_contributes_nothing(self):
        result = aggregate([agg_input([]), agg_input([(0, 0), (3, 4)])])
        assert result.n == 2
        assert result.es_in == pytest.approx(5.0)

    def test_origins_length_mismatch_is_rejected(self):
        with pytest.raises(ValueError, match="one to one"):
            aggregate([agg_input([(0, 0), (1, 1)], origins=[D])])

    def test_per_group_origins_mismatch_is_rejected_even_when_totals_match(self):
        short = agg_input([(0, 0), (1, 1)], origins=[D])
        long = agg_input([(2, 2)], origins=[D, E])
        with pytest.raises(ValueError, match="one to one"):
            aggregate([short, long])

    def test_aggregate_does_not_mutate_inputs(self):
        group = agg_input([(0, 0), (1, 1)])
        before = (list(group.offsets), list(group.origins))
        aggregate([group, group])
        assert (list(group.offsets), list(group.origins)) == before


def test_group_stats_and_aggregate_carry_exactly_the_documented_fields():
    assert set(GroupStats.__dataclass_fields__) == {
        "n",
        "es_in",
        "es_moa",
        "mean_dx_in",
        "mean_dy_in",
        "mean_dx_mil",
        "mean_dy_mil",
        "includes_estimated",
    }
    assert set(AggregateStats.__dataclass_fields__) == {
        "n",
        "es_in",
        "es_moa",
        "center_dx_in",
        "center_dy_in",
        "center_dx_mil",
        "center_dy_mil",
        "includes_estimated",
        "mixed_loads",
    }


def test_public_surface_of_stats_module():
    public = {n for n in vars(stats_module) if not n.startswith("_")}
    assert {"extreme_spread_in", "group_stats", "aggregate", "AggregateInput"} <= public
