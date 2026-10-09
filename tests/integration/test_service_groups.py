"""T2: group service over a real migrated SQLite file (review saves, offsets, stats, finalize)."""

# ruff: noqa: F811

from __future__ import annotations

import dataclasses
import math
import sqlite3
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from rga.config import Settings
from rga.db import repo_groups, repo_rifles, repo_zeroing
from rga.db.connection import connect
from rga.db.rows import LoadRow, RifleRow
from rga.domain import zeroing as domain_zeroing
from rga.domain.stats import group_stats as domain_group_stats
from rga.domain.types import GroupPurpose, GroupStats, Offset, PlanePoint, Shot, ShotOrigin
from rga.services import groups as service
from rga.services.groups import (
    FinalizeError,
    GroupError,
    GroupNotFound,
    ShotDroppedWithoutReason,
    SizeOnlyStats,
)
from rga.web.app import create_app
from rga.web.schemas import ROUTE_SCHEMAS, GroupStatsOut, SizeOnlyStatsOut
from tests.support.db import migrated_conn  # noqa: F401
from tests.support.seed import (
    DEFAULT_AIM,
    SeedContext,
    seed_context,
    seed_group,
    seed_load,
    seed_photo,
    seed_rifle,
)

D = ShotOrigin.DETECTED
E = ShotOrigin.ESTIMATED
AIM = PlanePoint(4.0, 5.0)

OFFSETS_10 = [
    Offset(0.4 * math.cos(i * 0.628) + 0.3, 0.4 * math.sin(i * 0.628) + 0.2) for i in range(10)
]


@dataclass(frozen=True)
class Env:
    conn: sqlite3.Connection
    rifle: RifleRow
    load: LoadRow
    session: SeedContext


@pytest.fixture
def env(migrated_conn) -> Env:
    rifle = seed_rifle(migrated_conn)
    load = seed_load(migrated_conn, rifle)
    session = seed_context(rifle, distance_yd=100)
    return Env(migrated_conn, rifle, load, session)


def shots_at(*points, origin=D, moved=False) -> list[Shot]:
    return [Shot(PlanePoint(x, y), origin, moved_by_user=moved) for x, y in points]


def draft(
    env: Env, n_shots: int, expected: int | None = None, purpose="general", lot="LOT-1", **kw
):
    return seed_group(
        env.conn,
        context=env.session,
        load=env.load,
        offsets=OFFSETS_10[:n_shots],
        shots_expected=expected or n_shots,
        purpose=purpose,
        lot_code=lot,
        finalize=False,
        **kw,
    )


def fresh_offsets(env: Env, group_id: int, distance_yd: float | None = None):
    """Independent derivation of (dx_in, dy_in, dx_mil, dy_mil) per shot from plane positions."""
    group = repo_groups.get_group(env.conn, group_id)
    yd = distance_yd or group.distance_yd
    out = []
    for s in repo_groups.list_shots(env.conn, group_id):
        dx = s.x_in - group.aim_x_in
        dy = group.aim_y_in - s.y_in
        out.append((dx, dy, dx * 1000 / (yd * 36), dy * 1000 / (yd * 36)))
    return out


def shot_offsets(shot) -> tuple:
    return (shot.dx_in, shot.dy_in, shot.dx_mil, shot.dy_mil)


def stored_offsets(env: Env, group_id: int):
    return [
        (s.dx_in, s.dy_in, s.dx_mil, s.dy_mil) for s in repo_groups.list_shots(env.conn, group_id)
    ]


def assert_offsets_equal(actual, expected):
    assert len(actual) == len(expected)
    for got, want in zip(actual, expected, strict=True):
        assert got == pytest.approx(want, abs=1e-12)


class TestRecomputeOffsets:
    def test_a_seeded_group_stores_the_derived_offsets_with_y_up(self, env):
        group = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10)

        assert_offsets_equal(stored_offsets(env, group.id), fresh_offsets(env, group.id))
        first = repo_groups.list_shots(env.conn, group.id)[0]
        assert first.dx_in == pytest.approx(OFFSETS_10[0].dx_in)
        assert first.dy_in == pytest.approx(OFFSETS_10[0].dy_in)

    def test_high_and_right_shots_have_positive_dx_and_dy(self, env):
        offsets = [Offset(0.5, 0.5), Offset(0.7, 0.9)]
        group = seed_group(env.conn, context=env.session, load=env.load, offsets=offsets)

        for shot in repo_groups.list_shots(env.conn, group.id):
            assert shot.dx_in > 0 and shot.dy_in > 0 and shot.dx_mil > 0 and shot.dy_mil > 0
            assert shot.y_in < group.aim_y_in

    def test_invariant_after_an_aim_move(self, env):
        group = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10)

        with env.conn:
            repo_groups.update_group(env.conn, group.id, aim_x_in=5.5, aim_y_in=3.25)
        service.recompute_offsets(env.conn, group.id)

        assert_offsets_equal(stored_offsets(env, group.id), fresh_offsets(env, group.id))
        assert stored_offsets(env, group.id)[0][0] == pytest.approx(
            repo_groups.list_shots(env.conn, group.id)[0].x_in - 5.5
        )

    def test_invariant_after_a_group_distance_change(self, env):
        group = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10)
        before = stored_offsets(env, group.id)

        with env.conn:
            repo_groups.update_group(env.conn, group.id, distance_yd=300)
        service.recompute_offsets(env.conn, group.id)

        after = stored_offsets(env, group.id)
        assert_offsets_equal(after, fresh_offsets(env, group.id, 300))
        for b, a in zip(before, after, strict=True):
            assert a[0] == pytest.approx(b[0]) and a[1] == pytest.approx(b[1])
            assert a[2] == pytest.approx(b[2] / 3) and a[3] == pytest.approx(b[3] / 3)

    def test_invariant_after_a_shot_drag(self, env):
        group = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10)
        stored = repo_groups.list_shots(env.conn, group.id)
        moved = [Shot(PlanePoint(s.x_in, s.y_in), ShotOrigin(s.origin)) for s in stored]
        moved[3] = Shot(
            PlanePoint(stored[3].x_in + 0.6, stored[3].y_in - 0.4), D, moved_by_user=True
        )
        # a final group cannot be reviewed again, so drag on a draft of the same shape
        env.conn.execute(
            "UPDATE groups SET status = 'draft', finalized_at = NULL WHERE id = ?", (group.id,)
        )

        service.save_review(env.conn, group.id, AIM, moved, [], 10)

        assert_offsets_equal(stored_offsets(env, group.id), fresh_offsets(env, group.id))
        dragged = repo_groups.list_shots(env.conn, group.id)[3]
        assert dragged.moved_by_user == 1
        assert dragged.dx_in == pytest.approx(dragged.x_in - 4.0)

    def test_without_an_aim_offsets_are_null(self, env):
        group = draft(env, 5, aim=None)

        assert group.aim_x_in is None
        assert all(v is None for row in stored_offsets(env, group.id) for v in row)

    def test_offsets_are_recomputed_when_the_aim_is_set(self, env):
        group = draft(env, 1, expected=5, aim=None)
        service.save_review(env.conn, group.id, AIM, [Shot(PlanePoint(4.1, 4.9), D)], [], 5)

        (row,) = stored_offsets(env, group.id)
        assert row[0] == pytest.approx(0.1) and row[1] == pytest.approx(0.1)

    def test_recomputes_are_committed_and_touch_only_the_named_group(self, env):
        first = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10[:3])
        second = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10[:4])
        other_session = seed_context(env.rifle, distance_yd=200)
        third = seed_group(env.conn, context=other_session, load=env.load, offsets=OFFSETS_10[:3])
        with env.conn:
            env.conn.execute(
                "UPDATE shots SET dx_in = NULL, dy_in = NULL, dx_mil = NULL, dy_mil = NULL"
            )

        service.recompute_offsets(env.conn, first.id)
        service.recompute_offsets(env.conn, second.id)
        path = env.conn.execute("PRAGMA database_list").fetchone()["file"]

        other = connect(path)
        try:
            for group in (first, second):
                assert all(
                    v is not None
                    for s in repo_groups.list_shots(other, group.id)
                    for v in (s.dx_in, s.dy_in, s.dx_mil, s.dy_mil)
                )
            assert all(s.dx_in is None for s in repo_groups.list_shots(other, third.id))
        finally:
            other.close()

    def test_unknown_group_raises(self, env):
        with pytest.raises(GroupNotFound) as error:
            service.recompute_offsets(env.conn, 999)
        assert error.value.code == "group_not_found"


class TestStats:
    def test_stats_match_the_domain_computation_and_count_estimated_shots(self, env):
        origins = [E] * 2 + [D] * 8
        group = seed_group(
            env.conn, context=env.session, load=env.load, offsets=OFFSETS_10, origins=origins
        )

        stats = service.group_stats(env.conn, group.id)
        expected = domain_group_stats(OFFSETS_10, origins, 100)

        assert isinstance(stats, GroupStats)
        assert stats.n == 10 and stats.includes_estimated
        assert stats.es_in == pytest.approx(expected.es_in)
        assert stats.mean_dx_mil == pytest.approx(expected.mean_dx_mil)
        assert stats.mean_dy_in == pytest.approx(expected.mean_dy_in)

    def test_stats_are_computed_fresh_from_plane_positions_not_stored_offsets(self, env):
        group = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10)
        env.conn.execute("UPDATE shots SET dx_in = 99, dy_in = 99, dx_mil = 99, dy_mil = 99")
        expected = domain_group_stats(OFFSETS_10, [D] * 10, 100)

        stats = service.group_stats(env.conn, group.id)

        assert stats.mean_dx_in == pytest.approx(expected.mean_dx_in)
        assert stats.mean_dy_mil == pytest.approx(expected.mean_dy_mil)

    def test_stats_use_the_groups_distance(self, env):
        far = seed_context(env.rifle, distance_yd=300)
        group = seed_group(env.conn, context=far, load=env.load, offsets=OFFSETS_10)

        stats = service.group_stats(env.conn, group.id)

        assert stats.es_moa == pytest.approx(domain_group_stats(OFFSETS_10, [D] * 10, 300).es_moa)

    def test_size_only_stats_report_the_spread_in_moa_at_the_groups_distance(self, env):
        far = seed_context(env.rifle, distance_yd=300)
        group = seed_group(
            env.conn,
            context=far,
            load=env.load,
            offsets=OFFSETS_10[:6],
            purpose="zero_step2",
        )

        stats = service.group_stats(env.conn, group.id)

        expected = domain_group_stats(OFFSETS_10[:6], [D] * 6, 300)
        assert isinstance(stats, SizeOnlyStats)
        assert stats.es_in == pytest.approx(expected.es_in)
        assert stats.es_moa == pytest.approx(expected.es_moa)

    def test_a_group_without_shots_has_no_stats(self, env):
        group = draft(env, 0, expected=3)

        assert service.group_stats(env.conn, group.id) is None

    def test_a_draft_without_an_aim_reports_size_only(self, env):
        group = draft(env, 6, aim=None)

        stats = service.group_stats(env.conn, group.id)

        assert isinstance(stats, SizeOnlyStats)
        assert stats.n == 6
        assert stats.es_in == pytest.approx(domain_group_stats(OFFSETS_10[:6], [D] * 6, 100).es_in)
        assert not hasattr(stats, "mean_dx_mil")

    def test_unknown_group_raises(self, env):
        with pytest.raises(GroupNotFound):
            service.group_stats(env.conn, 999)


class TestShortZeroGroupGuard:
    """B-09 / B-14 / B-18: no POI, offsets or direction for zero_check groups and for short
    zero_step2 / zero_confirm groups."""

    @pytest.mark.parametrize("purpose", ["zero_step2", "zero_confirm"])
    @pytest.mark.parametrize("n", range(1, 10))
    def test_size_only_stats_and_null_offsets_below_ten_shots(self, env, purpose, n):
        group = seed_group(
            env.conn,
            context=env.session,
            load=env.load,
            offsets=[Offset(1.0 + i, 2.0 + i) for i in range(n)],
            purpose=purpose,
        )

        bundle = service.load_group(env.conn, group.id)

        assert isinstance(bundle.stats, SizeOnlyStats)
        assert bundle.stats.n == n
        for leaked in ("mean_dx_in", "mean_dy_in", "mean_dx_mil", "mean_dy_mil"):
            assert not hasattr(bundle.stats, leaked)
        assert all(
            (s.dx_in, s.dy_in, s.dx_mil, s.dy_mil) == (None, None, None, None) for s in bundle.shots
        )
        assert all(row == (None, None, None, None) for row in stored_offsets(env, group.id))
        assert [(s.x_in, s.y_in) for s in bundle.shots] == [
            (DEFAULT_AIM.x_in + 1.0 + i, DEFAULT_AIM.y_in - 2.0 - i) for i in range(n)
        ]

    @pytest.mark.parametrize("purpose", ["zero_step2", "zero_confirm"])
    def test_ten_shots_report_the_full_stats_and_offsets(self, env, purpose):
        group = seed_group(
            env.conn, context=env.session, load=env.load, offsets=OFFSETS_10, purpose=purpose
        )

        bundle = service.load_group(env.conn, group.id)

        assert isinstance(bundle.stats, GroupStats)
        assert all(s.dx_mil is not None and s.dy_in is not None for s in bundle.shots)

    @pytest.mark.parametrize("n", [1, 2, 3, 10, 14])
    def test_a_zero_check_group_of_any_size_is_masked(self, env, n):
        group = seed_group(
            env.conn,
            context=env.session,
            load=env.load,
            offsets=[Offset(1.0 + i * 0.1, 2.0 + i * 0.1) for i in range(n)],
            purpose="zero_check",
        )

        bundle = service.load_group(env.conn, group.id)

        assert isinstance(bundle.stats, SizeOnlyStats) and bundle.stats.n == n
        for leaked in ("mean_dx_in", "mean_dy_in", "mean_dx_mil", "mean_dy_mil"):
            assert not hasattr(bundle.stats, leaked)
        assert all(
            (s.dx_in, s.dy_in, s.dx_mil, s.dy_mil) == (None, None, None, None) for s in bundle.shots
        )
        assert all(row == (None,) * 4 for row in stored_offsets(env, group.id))
        assert [(s.x_in, s.y_in) for s in bundle.shots][0] == (
            DEFAULT_AIM.x_in + 1.0,
            DEFAULT_AIM.y_in - 2.0,
        )
        assert bundle.group.status == "final"

    def test_a_zero_check_group_stays_masked_after_an_aim_move_and_when_stale(self, env):
        group = seed_group(
            env.conn,
            context=env.session,
            load=env.load,
            offsets=OFFSETS_10[:2],
            purpose="zero_check",
        )
        with env.conn:
            repo_groups.update_group(env.conn, group.id, aim_x_in=1.0, aim_y_in=1.0)
        service.recompute_offsets(env.conn, group.id)
        assert all(row == (None,) * 4 for row in stored_offsets(env, group.id))
        env.conn.execute("UPDATE shots SET dx_in = 1, dy_in = 2, dx_mil = 3, dy_mil = 4")

        bundle = service.load_group(env.conn, group.id)

        assert bundle.shots
        assert all(shot_offsets(s) == (None,) * 4 for s in bundle.shots)
        assert isinstance(bundle.stats, SizeOnlyStats)

    def test_a_zero_check_draft_is_masked_while_reviewing_and_when_finalized(self, env):
        group = draft(env, 2, purpose="zero_check")

        reviewed = service.save_review(
            env.conn, group.id, AIM, shots_at((4.1, 5.1), (4.2, 5.0)), [], 2
        )
        finalized = service.finalize(env.conn, group.id)

        for bundle in (reviewed, finalized):
            assert isinstance(bundle.stats, SizeOnlyStats)
            assert all(s.dx_in is None and s.dy_mil is None for s in bundle.shots)
        assert all(row == (None,) * 4 for row in stored_offsets(env, group.id))

    @pytest.mark.parametrize("purpose", ["general", "zero_step1"])
    def test_other_purposes_keep_their_stats_even_when_short(self, env, purpose):
        group = seed_group(
            env.conn, context=env.session, load=env.load, offsets=OFFSETS_10[:3], purpose=purpose
        )

        bundle = service.load_group(env.conn, group.id)

        assert isinstance(bundle.stats, GroupStats)
        assert all(s.dx_mil is not None for s in bundle.shots)

    def test_offsets_appear_at_ten_shots_and_disappear_again_below_ten(self, env):
        group = draft(env, 9, expected=10, purpose="zero_step2")
        assert all(row == (None,) * 4 for row in stored_offsets(env, group.id))

        service.save_review(
            env.conn, group.id, AIM, shots_at(*[(4 + i * 0.1, 5.0) for i in range(10)]), [], 10
        )
        assert all(v is not None for row in stored_offsets(env, group.id) for v in row)
        assert isinstance(service.group_stats(env.conn, group.id), GroupStats)

        service.save_review(
            env.conn,
            group.id,
            AIM,
            shots_at(*[(4 + i * 0.1, 5.0) for i in range(9)]),
            [PlanePoint(9.0, 9.0)],
            10,
        )
        assert all(row == (None,) * 4 for row in stored_offsets(env, group.id))
        assert isinstance(service.group_stats(env.conn, group.id), SizeOnlyStats)

    def test_stale_stored_offsets_are_masked_when_the_group_is_loaded(self, env):
        group = seed_group(
            env.conn,
            context=env.session,
            load=env.load,
            offsets=OFFSETS_10[:4],
            purpose="zero_confirm",
        )
        env.conn.execute("UPDATE shots SET dx_in = 1, dy_in = 2, dx_mil = 3, dy_mil = 4")

        bundle = service.load_group(env.conn, group.id)

        assert bundle.shots
        assert all(shot_offsets(s) == (None,) * 4 for s in bundle.shots)

    def test_the_masking_rule(self):
        assert service.masks_poi("zero_step2", 9)
        assert not service.masks_poi("zero_step2", 10)
        assert service.masks_poi("zero_confirm", 1)
        assert not service.masks_poi("zero_confirm", 30)
        for n in (0, 1, 2, 3, 10, 50):
            assert service.masks_poi("zero_check", n)
        assert not service.masks_poi("general", 1)
        assert not service.masks_poi("zero_step1", 3)
        assert not service.masks_poi("zero_step1", 1)

    def test_the_old_helper_name_is_an_alias(self):
        assert service.is_short_zero_group is service.masks_poi


class TestOversizeStep1Groups:
    """B-41: Step 1 only corrects from 1 to 3 shots, so a zero_step1 group over 3 shots is masked
    like the other zeroing groups that must not show a point of impact."""

    @pytest.mark.parametrize(
        ("n", "masked"),
        [
            (0, False),
            (1, False),
            (2, False),
            (3, False),
            (4, True),
            (5, True),
            (9, True),
            (10, True),
            (15, True),
            (50, True),
        ],
    )
    def test_the_step_1_boundary_is_three_shots(self, n, masked):
        assert service.masks_poi("zero_step1", n) is masked
        assert service.masks_poi(GroupPurpose.ZERO_STEP1, n) is masked

    def test_the_other_purposes_keep_their_own_rules(self):
        assert service.masks_poi("zero_step2", 3) and not service.masks_poi("zero_step2", 10)
        assert service.masks_poi("zero_confirm", 9) and not service.masks_poi("zero_confirm", 10)
        assert all(service.masks_poi("zero_check", n) for n in (0, 1, 3, 20))
        assert not any(service.masks_poi("general", n) for n in (0, 3, 4, 9, 10, 50))

    @pytest.mark.parametrize("n", [1, 3])
    def test_one_to_three_shot_step_1_groups_keep_offsets_and_full_stats(self, env, n):
        group = seed_group(
            env.conn,
            context=env.session,
            load=env.load,
            offsets=OFFSETS_10[:n],
            purpose="zero_step1",
        )

        bundle = service.load_group(env.conn, group.id)

        assert isinstance(bundle.stats, GroupStats) and bundle.stats.n == n
        assert all(v is not None for row in stored_offsets(env, group.id) for v in row)
        assert all(s.dx_mil is not None and s.dy_in is not None for s in bundle.shots)

    @pytest.mark.parametrize("n", [4, 5, 9, 10])
    def test_a_step_1_group_over_three_shots_is_size_only_with_null_offsets(self, env, n):
        group = seed_group(
            env.conn,
            context=env.session,
            load=env.load,
            offsets=OFFSETS_10[:n],
            purpose="zero_step1",
        )

        bundle = service.load_group(env.conn, group.id)

        assert isinstance(bundle.stats, SizeOnlyStats) and bundle.stats.n == n
        for leaked in ("mean_dx_in", "mean_dy_in", "mean_dx_mil", "mean_dy_mil"):
            assert not hasattr(bundle.stats, leaked)
        assert all(row == (None,) * 4 for row in stored_offsets(env, group.id))
        assert all(shot_offsets(s) == (None,) * 4 for s in bundle.shots)
        assert len(bundle.shots) == n and all(s.x_in is not None for s in bundle.shots)

    def test_growing_past_three_shots_nulls_the_offsets_and_shrinking_restores_them(self, env):
        group = draft(env, 3, expected=4, purpose="zero_step1")
        assert all(v is not None for row in stored_offsets(env, group.id) for v in row)

        grown = shots_at(*[(4 + i * 0.1, 5.0 + i * 0.05) for i in range(4)])
        service.save_review(env.conn, group.id, AIM, grown, [], 4)
        assert all(row == (None,) * 4 for row in stored_offsets(env, group.id))
        assert isinstance(service.group_stats(env.conn, group.id), SizeOnlyStats)

        service.save_review(env.conn, group.id, AIM, grown[:3], [PlanePoint(9.0, 9.0)], 3)
        assert_offsets_equal(stored_offsets(env, group.id), fresh_offsets(env, group.id))
        assert isinstance(service.group_stats(env.conn, group.id), GroupStats)

    def test_finalizing_an_oversize_step_1_draft_stores_no_offsets(self, env):
        group = draft(env, 5, purpose="zero_step1")
        assert all(row == (None,) * 4 for row in stored_offsets(env, group.id))

        service.finalize(env.conn, group.id)

        assert all(row == (None,) * 4 for row in stored_offsets(env, group.id))
        assert isinstance(service.group_stats(env.conn, group.id), SizeOnlyStats)

    def test_a_purpose_change_recompute_masks_and_unmasks(self, env):
        group = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10[:6])
        assert_offsets_equal(stored_offsets(env, group.id), fresh_offsets(env, group.id))

        repo_groups.update_group(env.conn, group.id, purpose="zero_step1")
        service.recompute_offsets(env.conn, group.id)
        assert all(row == (None,) * 4 for row in stored_offsets(env, group.id))

        repo_groups.update_group(env.conn, group.id, purpose="general")
        service.recompute_offsets(env.conn, group.id)
        assert_offsets_equal(stored_offsets(env, group.id), fresh_offsets(env, group.id))

    def test_a_distance_recompute_keeps_the_oversize_group_masked(self, env):
        big = seed_group(
            env.conn,
            context=env.session,
            load=env.load,
            offsets=OFFSETS_10[:5],
            purpose="zero_step1",
        )
        small = seed_group(
            env.conn,
            context=env.session,
            load=env.load,
            offsets=OFFSETS_10[:3],
            purpose="zero_step1",
        )

        for group in (big, small):
            service.recompute_offsets(env.conn, group.id)

        assert all(row == (None,) * 4 for row in stored_offsets(env, big.id))
        assert_offsets_equal(stored_offsets(env, small.id), fresh_offsets(env, small.id))

    def test_the_api_masks_a_group_when_its_purpose_becomes_step_1_over_three_shots(
        self, env, tmp_path
    ):
        app = create_app(Settings(data_dir=tmp_path))
        big = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10[:6])
        small = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10[:3])
        size_keys = {"n", "es_in", "es_moa", "includes_estimated", "es_label"}

        def set_purpose(group_id: int, purpose: str) -> None:
            with env.conn:
                repo_groups.update_group(env.conn, group_id, purpose=purpose)
            service.recompute_offsets(env.conn, group_id)

        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            before = client.get(f"/api/groups/{big.id}/stats").json()
            assert "mean_dx_mil" in before

            set_purpose(big.id, "zero_step1")
            masked = client.get(f"/api/groups/{big.id}/stats").json()
            detail = client.get(f"/api/groups/{big.id}").json()

            assert set(masked) == size_keys and masked["n"] == 6
            assert all(shot["dx_in"] is None and shot["dy_mil"] is None for shot in detail["shots"])
            assert all(row == (None,) * 4 for row in stored_offsets(env, big.id))
            assert detail["aim"] is None
            assert "mean" not in str(masked) and "poi" not in str(masked)

            set_purpose(small.id, "zero_step1")
            assert "mean_dx_mil" in client.get(f"/api/groups/{small.id}/stats").json()

            set_purpose(big.id, "general")
            assert "mean_dx_mil" in client.get(f"/api/groups/{big.id}/stats").json()
            assert_offsets_equal(stored_offsets(env, big.id), fresh_offsets(env, big.id))

    def test_an_oversize_step_1_group_cannot_join_an_aggregate_but_a_small_one_can(
        self, env, tmp_path
    ):
        app = create_app(Settings(data_dir=tmp_path))
        general = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10)
        small = seed_group(
            env.conn,
            context=env.session,
            load=env.load,
            offsets=OFFSETS_10[:3],
            purpose="zero_step1",
        )
        big = seed_group(
            env.conn,
            context=env.session,
            load=env.load,
            offsets=OFFSETS_10[:4],
            purpose="zero_step1",
        )
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            ok = client.post("/api/aggregate", json={"group_ids": [general.id, small.id]})
            refused = client.post("/api/aggregate", json={"group_ids": [general.id, big.id]})

        assert ok.status_code == 200
        assert (refused.status_code, refused.json()["code"]) == (409, "masked_group")


class TestOneMaskingRule:
    """B-43: the canonical rule is `domain.zeroing.masks_poi`; the service name is the same object."""

    PURPOSES = ["general", "zero_step1", "zero_step2", "zero_confirm", "zero_check"]

    @staticmethod
    def expected(purpose: str, n: int) -> bool:
        """The rule written out independently of the implementation."""
        if purpose == "zero_check":
            return True
        if purpose == "zero_step1":
            return n > 3
        if purpose in ("zero_step2", "zero_confirm"):
            return n < 10
        return False

    def test_the_service_names_are_the_domain_function(self):
        assert service.masks_poi is domain_zeroing.masks_poi
        assert service.is_short_zero_group is domain_zeroing.masks_poi

    def test_every_purpose_and_size_gives_the_same_answer_from_both_names(self):
        for purpose in self.PURPOSES:
            for n in range(0, 41):
                want = self.expected(purpose, n)
                assert domain_zeroing.masks_poi(purpose, n) is want, (purpose, n)
                assert service.masks_poi(purpose, n) is want, (purpose, n)
                assert service.is_short_zero_group(purpose, n) is want, (purpose, n)

    def test_the_stored_bundle_follows_the_rule_for_every_purpose_at_the_boundaries(self, env):
        for purpose in self.PURPOSES:
            for n in (1, 3, 4, 9, 10):
                group = seed_group(
                    env.conn,
                    context=env.session,
                    load=env.load,
                    offsets=OFFSETS_10[:n],
                    purpose=purpose,
                    lot_code=f"{purpose}-{n}",
                )
                bundle = service.load_group(env.conn, group.id)
                masked = self.expected(purpose, n)
                assert isinstance(bundle.stats, SizeOnlyStats) is masked, (purpose, n)
                assert (stored_offsets(env, group.id)[0] == (None,) * 4) is masked, (purpose, n)


class TestSaveReview:
    def test_stores_shots_in_order_with_origin_flags_aim_and_expected(self, env):
        group = draft(env, 0, expected=4)
        review = [
            Shot(PlanePoint(4.1, 5.1), D, confidence=0.9),
            Shot(PlanePoint(4.2, 5.2), E, moved_by_user=True),
            Shot(PlanePoint(4.3, 5.3), ShotOrigin.SPLIT),
            Shot(PlanePoint(4.4, 5.4), ShotOrigin.MANUAL),
        ]

        bundle = service.save_review(env.conn, group.id, AIM, review, [], 6)

        assert [(s.seq, s.x_in, s.origin, s.moved_by_user) for s in bundle.shots] == [
            (1, 4.1, "detected", 0),
            (2, 4.2, "estimated", 1),
            (3, 4.3, "split", 0),
            (4, 4.4, "manual", 0),
        ]
        assert bundle.shots[0].confidence == 0.9
        assert (bundle.group.aim_x_in, bundle.group.aim_y_in, bundle.group.shots_expected) == (
            4.0,
            5.0,
            6,
        )
        assert bundle.group.status == "draft"
        assert isinstance(bundle.stats, GroupStats) and bundle.stats.includes_estimated

    def test_an_estimated_shot_dragged_keeps_its_origin_and_changes_the_stats(self, env):
        group = draft(env, 0, expected=3)
        base = [(4.0, 5.0), (4.2, 5.0), (4.1, 5.2)]
        first = service.save_review(
            env.conn, group.id, AIM, shots_at(*base[:2]) + shots_at(base[2], origin=E), [], 3
        )
        dragged = [Shot(PlanePoint(4.1, 6.0), E, moved_by_user=True)]

        second = service.save_review(env.conn, group.id, AIM, shots_at(*base[:2]) + dragged, [], 3)

        assert second.shots[2].origin == "estimated" and second.shots[2].moved_by_user == 1
        assert second.stats.es_in > first.stats.es_in

    def test_the_aim_can_be_cleared_while_a_draft(self, env):
        group = draft(env, 1, expected=3)

        bundle = service.save_review(env.conn, group.id, None, shots_at((1, 1)), [], 3)

        assert (bundle.group.aim_x_in, bundle.group.aim_y_in) == (None, None)
        assert isinstance(bundle.stats, SizeOnlyStats)

    def test_a_repeated_save_does_not_duplicate_a_rejection_but_a_second_one_at_the_point_does(
        self, env
    ):
        group = draft(env, 0, expected=2)
        rejected = [PlanePoint(1.5, 2.5)]
        shots = shots_at((4, 5), (4.1, 5.1))

        service.save_review(env.conn, group.id, AIM, shots, rejected, 2)
        service.save_review(env.conn, group.id, AIM, shots, rejected, 2)
        once = repo_groups.list_rejections(env.conn, group.id)
        twice_list = [*rejected, PlanePoint(1.5, 2.5)]
        service.save_review(env.conn, group.id, AIM, shots, twice_list, 2)
        service.save_review(env.conn, group.id, AIM, shots, twice_list, 2)

        assert [(r.x_in, r.y_in, r.reason) for r in once] == [(1.5, 2.5, "not_a_bullet_hole")]
        stored = repo_groups.list_rejections(env.conn, group.id)
        assert [(r.x_in, r.y_in) for r in stored] == [(1.5, 2.5), (1.5, 2.5)]

    def test_new_rejections_are_added_per_shot_and_old_ones_are_never_removed(self, env):
        group = draft(env, 0, expected=2)
        shots = shots_at((4, 5), (4.1, 5.1))
        service.save_review(env.conn, group.id, AIM, shots, [PlanePoint(1.0, 1.0)], 2)

        service.save_review(
            env.conn, group.id, AIM, shots, [PlanePoint(2.0, 2.0), PlanePoint(2.0, 2.0)], 2
        )
        service.save_review(env.conn, group.id, AIM, shots, [], 2)

        assert [(r.x_in, r.y_in) for r in repo_groups.list_rejections(env.conn, group.id)] == [
            (1.0, 1.0),
            (2.0, 2.0),
            (2.0, 2.0),
        ]

    def test_a_stored_shot_cannot_vanish_without_a_new_rejection(self, env):
        group = draft(env, 0, expected=3)
        three = shots_at((4, 5), (4.1, 5.1), (4.2, 5.2))
        service.save_review(env.conn, group.id, AIM, three, [], 3)
        before = (
            repo_groups.list_shots(env.conn, group.id),
            repo_groups.get_group(env.conn, group.id),
        )

        with pytest.raises(ShotDroppedWithoutReason) as error:
            service.save_review(env.conn, group.id, PlanePoint(9, 9), three[:2], [], 2)

        assert error.value.code == "shot_dropped_without_reason"
        assert (
            repo_groups.list_shots(env.conn, group.id),
            repo_groups.get_group(env.conn, group.id),
        ) == before
        assert repo_groups.list_rejections(env.conn, group.id) == []

    def test_dropping_a_shot_with_its_rejection_is_accepted(self, env):
        group = draft(env, 0, expected=3)
        three = shots_at((4, 5), (4.1, 5.1), (4.2, 5.2))
        service.save_review(env.conn, group.id, AIM, three, [], 3)

        bundle = service.save_review(env.conn, group.id, AIM, three[:2], [PlanePoint(4.2, 5.2)], 2)

        assert len(bundle.shots) == 2
        assert len(repo_groups.list_rejections(env.conn, group.id)) == 1

    def test_dropping_two_shots_with_one_rejection_is_refused(self, env):
        group = draft(env, 0, expected=3)
        three = shots_at((4, 5), (4.1, 5.1), (4.2, 5.2))
        service.save_review(env.conn, group.id, AIM, three, [], 3)

        with pytest.raises(ShotDroppedWithoutReason):
            service.save_review(env.conn, group.id, AIM, three[:1], [PlanePoint(4.2, 5.2)], 1)

        assert len(repo_groups.list_shots(env.conn, group.id)) == 3
        assert repo_groups.list_rejections(env.conn, group.id) == []

    def test_an_already_stored_rejection_does_not_excuse_a_new_drop(self, env):
        group = draft(env, 0, expected=3)
        three = shots_at((4, 5), (4.1, 5.1), (4.2, 5.2))
        service.save_review(env.conn, group.id, AIM, three, [], 3)
        service.save_review(env.conn, group.id, AIM, three[:2], [PlanePoint(4.2, 5.2)], 2)

        with pytest.raises(ShotDroppedWithoutReason):
            service.save_review(env.conn, group.id, AIM, three[:1], [PlanePoint(4.2, 5.2)], 1)

    def test_adding_or_moving_shots_needs_no_rejection(self, env):
        group = draft(env, 0, expected=3)
        service.save_review(env.conn, group.id, AIM, shots_at((4, 5), (4.1, 5.1)), [], 3)

        bundle = service.save_review(
            env.conn, group.id, AIM, shots_at((5, 6), (5.1, 6.1), (5.2, 6.2)), [], 3
        )

        assert len(bundle.shots) == 3

    def test_a_final_group_cannot_be_reviewed(self, env):
        group = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10[:3])

        with pytest.raises(GroupError) as error:
            service.save_review(env.conn, group.id, AIM, shots_at((1, 1)), [], 3)

        assert error.value.code == "group_final"
        assert len(repo_groups.list_shots(env.conn, group.id)) == 3

    def test_invalid_expected_count_and_non_finite_positions_change_nothing(self, env):
        group = draft(env, 3)
        before = repo_groups.list_shots(env.conn, group.id)

        with pytest.raises(GroupError) as bad_count:
            service.save_review(env.conn, group.id, AIM, shots_at((1, 1)), [], 0)
        with pytest.raises(GroupError) as bad_point:
            service.save_review(env.conn, group.id, AIM, shots_at((float("nan"), 1)), [], 3)
        with pytest.raises(GroupError) as bad_aim:
            service.save_review(
                env.conn,
                group.id,
                PlanePoint(float("inf"), 1),
                shots_at((1, 1), (2, 2), (3, 3)),
                [],
                3,
            )

        assert bad_count.value.code == "invalid_shots_expected"
        assert bad_point.value.code == bad_aim.value.code == "invalid_coordinates"
        assert repo_groups.list_shots(env.conn, group.id) == before

    def test_unknown_group_raises(self, env):
        with pytest.raises(GroupNotFound):
            service.save_review(env.conn, 999, AIM, [], [], 1)

    def test_the_save_is_committed(self, env):
        group = draft(env, 0, expected=2)
        service.save_review(
            env.conn, group.id, AIM, shots_at((4, 5), (4.1, 5.1)), [PlanePoint(1, 1)], 2
        )
        path = env.conn.execute("PRAGMA database_list").fetchone()["file"]

        other = connect(path)
        try:
            assert len(repo_groups.list_shots(other, group.id)) == 2
            assert len(repo_groups.list_rejections(other, group.id)) == 1
            assert repo_groups.get_group(other, group.id).aim_x_in == 4.0
        finally:
            other.close()

    def test_a_failure_midway_rolls_everything_back(self, env, monkeypatch):
        group = draft(env, 3)
        before_shots = repo_groups.list_shots(env.conn, group.id)

        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(repo_groups, "update_group", boom)
        with pytest.raises(sqlite3.OperationalError):
            service.save_review(
                env.conn, group.id, AIM, shots_at((7, 7), (8, 8), (9, 9)), [PlanePoint(1, 1)], 3
            )
        monkeypatch.undo()

        assert repo_groups.list_shots(env.conn, group.id) == before_shots
        assert repo_groups.list_rejections(env.conn, group.id) == []
        assert repo_groups.get_group(env.conn, group.id).aim_x_in == DEFAULT_AIM.x_in

    def test_user_notes_are_never_altered(self, env):
        notes = "  windy \n high  left  100%  "
        group = draft(env, 3, notes=notes)

        service.save_review(
            env.conn, group.id, AIM, shots_at((4, 5), (4.1, 5.1), (4.2, 5.2)), [], 3
        )
        service.finalize(env.conn, group.id)

        assert repo_groups.get_group(env.conn, group.id).notes == notes


class TestFinalize:
    def test_finalizing_a_complete_group_marks_it_final_with_a_timestamp_and_offsets(self, env):
        group = draft(env, 10)

        bundle = service.finalize(env.conn, group.id)

        assert bundle.group.status == "final"
        assert bundle.group.finalized_at and bundle.group.finalized_at.endswith("Z")
        assert all(s.dx_mil is not None for s in bundle.shots)
        assert isinstance(bundle.stats, GroupStats)

    @pytest.mark.parametrize(("stored", "expected"), [(9, 10), (11, 10), (0, 3)])
    def test_a_count_that_differs_from_expected_is_a_count_mismatch(self, env, stored, expected):
        group = draft(env, stored, expected=expected) if stored <= 10 else draft(env, 10)
        if stored > 10:
            service.save_review(
                env.conn, group.id, AIM, shots_at(*[(i * 0.1, 5.0) for i in range(11)]), [], 10
            )

        with pytest.raises(FinalizeError) as error:
            service.finalize(env.conn, group.id)

        assert error.value.code == "count_mismatch"
        assert f"{stored} of {expected} shots" in error.value.message
        assert repo_groups.get_group(env.conn, group.id).status == "draft"

    def test_too_many_and_too_few_give_different_advice(self, env):
        few = draft(env, 9, expected=10)
        many = draft(env, 10)
        service.save_review(
            env.conn, many.id, AIM, shots_at(*[(i * 0.1, 5.0) for i in range(11)]), [], 10
        )

        with pytest.raises(FinalizeError) as few_error:
            service.finalize(env.conn, few.id)
        with pytest.raises(FinalizeError) as many_error:
            service.finalize(env.conn, many.id)

        assert "add the missing shots or lower the expected count" in few_error.value.message
        assert "delete detection errors or raise the shot count" in many_error.value.message

    def test_a_group_without_an_aim_is_refused(self, env):
        group = draft(env, 5, aim=None)

        with pytest.raises(FinalizeError) as error:
            service.finalize(env.conn, group.id)

        assert error.value.code == "no_aim"
        assert repo_groups.get_group(env.conn, group.id).status == "draft"

    def test_an_unconfirmed_photo_reference_is_refused(self, env):
        photo = seed_photo(env.conn, confirmed=False)
        group = draft(env, 5, photo=photo)

        with pytest.raises(FinalizeError) as error:
            service.finalize(env.conn, group.id)

        assert error.value.code == "reference_unconfirmed"

    def test_a_confirmed_photo_reference_allows_finalizing(self, env):
        photo = seed_photo(env.conn, confirmed=True)
        group = draft(env, 5, photo=photo)

        assert service.finalize(env.conn, group.id).group.status == "final"

    def test_guards_are_checked_reference_then_aim_then_count(self, env):
        photo = seed_photo(env.conn, confirmed=False)
        group = draft(env, 5, expected=8, photo=photo, aim=None)

        with pytest.raises(FinalizeError) as first:
            service.finalize(env.conn, group.id)
        env.conn.execute("UPDATE photos SET reference_confirmed = 0 WHERE id = ?", (photo.id,))
        repo_groups.update_photo_reference(
            env.conn,
            photo.id,
            reference_kind="circle_1in",
            reference_json="{}",
            h_px_to_plane_json="[]",
            working_ppi=200.0,
            working_w=1,
            working_h=1,
            axis_ratio=1.0,
            reference_confirmed=True,
        )
        with pytest.raises(FinalizeError) as second:
            service.finalize(env.conn, group.id)
        service.save_review(env.conn, group.id, AIM, shots_at(*[(4, 5)] * 5), [], 8)
        with pytest.raises(FinalizeError) as third:
            service.finalize(env.conn, group.id)

        assert [first.value.code, second.value.code, third.value.code] == [
            "reference_unconfirmed",
            "no_aim",
            "count_mismatch",
        ]

    def test_finalize_rederives_offsets_after_the_aim_moved_behind_its_back(self, env):
        group = draft(env, 4)
        with env.conn:
            repo_groups.update_group(env.conn, group.id, aim_x_in=6.0, aim_y_in=2.0)

        service.finalize(env.conn, group.id)

        assert_offsets_equal(stored_offsets(env, group.id), fresh_offsets(env, group.id))

    def test_finalizing_twice_changes_nothing(self, env):
        group = draft(env, 4)
        first = service.finalize(env.conn, group.id)

        second = service.finalize(env.conn, group.id)

        assert second.group == first.group

    def test_finalize_is_committed(self, env):
        group = draft(env, 4)
        service.finalize(env.conn, group.id)
        path = env.conn.execute("PRAGMA database_list").fetchone()["file"]

        other = connect(path)
        try:
            assert repo_groups.get_group(other, group.id).status == "final"
        finally:
            other.close()

    def test_a_short_zero_group_finalizes_without_offsets(self, env):
        group = draft(env, 6, purpose="zero_step2")

        bundle = service.finalize(env.conn, group.id)

        assert bundle.group.status == "final"
        assert isinstance(bundle.stats, SizeOnlyStats)
        assert all(row == (None,) * 4 for row in stored_offsets(env, group.id))

    def test_unknown_group_raises(self, env):
        with pytest.raises(GroupNotFound):
            service.finalize(env.conn, 999)


class TestNewLotFlag:
    def _final(self, env, lot, at, n=3):
        group = seed_group(
            env.conn, context=env.session, load=env.load, offsets=OFFSETS_10[:n], lot_code=lot
        )
        with env.conn:
            repo_groups.update_group(env.conn, group.id, finalized_at=at)
        return group

    def test_the_first_group_of_a_new_lot_of_an_existing_load_is_flagged(self, env):
        a1 = self._final(env, "A", "2026-09-01T00:00:00.000000Z")
        b1 = self._final(env, "B", "2026-09-02T00:00:00.000000Z")
        b2 = self._final(env, "B", "2026-09-03T00:00:00.000000Z")

        assert not service.new_lot_flag(env.conn, a1.id)
        assert service.new_lot_flag(env.conn, b1.id)
        assert not service.new_lot_flag(env.conn, b2.id)

    def test_a_loads_first_ever_lot_is_not_flagged_even_after_other_lots_exist(self, env):
        a1 = self._final(env, "A", "2026-09-01T00:00:00.000000Z")
        self._final(env, "B", "2026-09-05T00:00:00.000000Z")

        assert not service.new_lot_flag(env.conn, a1.id)

    def test_a_third_lot_is_flagged_too(self, env):
        self._final(env, "A", "2026-09-01T00:00:00.000000Z")
        self._final(env, "B", "2026-09-02T00:00:00.000000Z")
        c1 = self._final(env, "C", "2026-09-03T00:00:00.000000Z")

        assert service.new_lot_flag(env.conn, c1.id)

    def test_a_draft_is_flagged_prospectively(self, env):
        self._final(env, "A", "2026-09-01T00:00:00.000000Z")
        new_lot_draft = draft(env, 3, lot="B")
        same_lot_draft = draft(env, 3, lot="A")

        assert service.new_lot_flag(env.conn, new_lot_draft.id)
        assert not service.new_lot_flag(env.conn, same_lot_draft.id)

    def test_a_draft_of_the_first_lot_of_a_load_is_not_flagged(self, env):
        group = draft(env, 3, lot="A")

        assert not service.new_lot_flag(env.conn, group.id)

    def test_another_loads_lots_do_not_count(self, env):
        other_load = seed_load(env.conn, env.rifle, label="Other load")
        seed_group(
            env.conn, context=env.session, load=other_load, offsets=OFFSETS_10[:3], lot_code="X"
        )
        only = self._final(env, "A", "2026-09-01T00:00:00.000000Z")

        assert not service.new_lot_flag(env.conn, only.id)

    def test_unknown_group_raises(self, env):
        with pytest.raises(GroupNotFound):
            service.new_lot_flag(env.conn, 999)


class TestLoadGroup:
    def test_the_bundle_carries_load_rifle_shots_and_the_group_context(self, env):
        group = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10)

        bundle = service.load_group(env.conn, group.id)

        assert bundle.group == repo_groups.get_group(env.conn, group.id)
        assert not hasattr(bundle, "session")
        assert (bundle.group.distance_yd, bundle.group.position) == (100, "bench")
        assert bundle.load.id == env.load.id
        assert bundle.rifle.id == env.rifle.id
        assert [s.seq for s in bundle.shots] == list(range(1, 11))

    def test_unknown_group_raises(self, env):
        with pytest.raises(GroupNotFound):
            service.load_group(env.conn, 12345)


class TestSeedHelpers:
    def test_seed_group_round_trips_offsets_through_the_single_y_flip(self, env):
        offsets = [Offset(0.25, 0.75), Offset(-0.5, -0.125), Offset(0.0, 1.5)]

        group = seed_group(env.conn, context=env.session, load=env.load, offsets=offsets)

        rows = repo_groups.list_shots(env.conn, group.id)
        assert [(r.dx_in, r.dy_in) for r in rows] == [(o.dx_in, o.dy_in) for o in offsets]
        assert group.status == "final" and group.shots_expected == 3

    def test_seed_group_defaults_and_overrides(self, env):
        group = seed_group(
            env.conn,
            context=env.session,
            load=env.load.id,
            offsets=OFFSETS_10[:4],
            origins=[D, E, D, D],
            lot_code="L9",
            elevation_dialed_mil=2.5,
            notes="note",
            finalize=False,
            shots_expected=6,
        )

        assert (group.status, group.shots_expected, group.elevation_dialed_mil) == ("draft", 6, 2.5)
        assert group.notes == "note"
        assert [s.origin for s in repo_groups.list_shots(env.conn, group.id)] == [
            "detected",
            "estimated",
            "detected",
            "detected",
        ]

    def test_seed_group_with_mismatched_origins_is_rejected(self, env):
        with pytest.raises(ValueError, match="origins"):
            seed_group(
                env.conn, context=env.session, load=env.load, offsets=OFFSETS_10[:3], origins=[D]
            )

    def test_seed_group_finalize_failure_surfaces_the_guard(self, env):
        with pytest.raises(FinalizeError):
            seed_group(
                env.conn, context=env.session, load=env.load, offsets=OFFSETS_10[:3], aim=None
            )

    def test_seeded_rows_are_committed(self, env):
        group = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10[:3])
        path = env.conn.execute("PRAGMA database_list").fetchone()["file"]

        other = connect(path)
        try:
            assert repo_groups.get_group(other, group.id) is not None
            assert len(repo_groups.list_shots(other, group.id)) == 3
        finally:
            other.close()

    def test_two_photos_get_distinct_hashes(self, env):
        a = seed_photo(env.conn)
        b = seed_photo(env.conn, confirmed=False)

        assert a.sha256 != b.sha256 and a.reference_confirmed == 1 and b.reference_confirmed == 0


def second_connection(env: Env, busy_timeout_ms: int = 0) -> sqlite3.Connection:
    path = env.conn.execute("PRAGMA database_list").fetchone()["file"]
    other = connect(path)
    other.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    return other


class TestOffsetMaskOnWriteAndRead:
    """B-14 / B-18: stored offsets are NULLed when a group becomes masked, and masked on read."""

    @pytest.mark.parametrize(
        ("purpose", "n"),
        [("zero_check", 2), ("zero_check", 10), ("zero_step2", 9), ("zero_confirm", 3)],
    )
    @pytest.mark.parametrize("path", ["group", "again"])
    def test_changing_a_general_group_to_a_masked_purpose_nulls_all_four_stored_columns(
        self, env, purpose, n, path
    ):
        group = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10[:n])
        assert all(v is not None for row in stored_offsets(env, group.id) for v in row)

        with env.conn:
            repo_groups.update_group(env.conn, group.id, purpose=purpose)
        service.recompute_offsets(env.conn, group.id)
        if path == "again":
            service.recompute_offsets(env.conn, group.id)

        other = second_connection(env)
        try:
            rows = repo_groups.list_shots(other, group.id)
        finally:
            other.close()
        assert len(rows) == n
        assert all(shot_offsets(r) == (None,) * 4 for r in rows)
        assert [(r.x_in, r.y_in) for r in rows] == [
            (r.x_in, r.y_in) for r in repo_groups.list_shots(env.conn, group.id)
        ]

    @pytest.mark.parametrize(
        ("purpose", "n"), [("zero_step2", 10), ("zero_confirm", 12), ("zero_step1", 2)]
    )
    def test_a_purpose_that_is_not_masked_keeps_every_column_populated(self, env, purpose, n):
        group = seed_group(
            env.conn, context=env.session, load=env.load, offsets=OFFSETS_10[:2] * (n // 2)
        )

        with env.conn:
            repo_groups.update_group(env.conn, group.id, purpose=purpose)
        service.recompute_offsets(env.conn, group.id)

        assert all(v is not None for row in stored_offsets(env, group.id) for v in row)
        assert_offsets_equal(stored_offsets(env, group.id), fresh_offsets(env, group.id))

    def test_changing_a_masked_group_back_to_general_populates_the_columns(self, env):
        group = seed_group(
            env.conn,
            context=env.session,
            load=env.load,
            offsets=OFFSETS_10[:4],
            purpose="zero_confirm",
        )
        assert all(row == (None,) * 4 for row in stored_offsets(env, group.id))

        with env.conn:
            repo_groups.update_group(env.conn, group.id, purpose="general")
        service.recompute_offsets(env.conn, group.id)

        assert_offsets_equal(stored_offsets(env, group.id), fresh_offsets(env, group.id))

    @pytest.mark.parametrize("purpose", ["zero_check", "zero_step2", "zero_confirm"])
    def test_every_field_of_a_stale_row_is_masked_on_read(self, env, purpose):
        group = seed_group(
            env.conn, context=env.session, load=env.load, offsets=OFFSETS_10[:2], purpose=purpose
        )
        for field in ("dx_in", "dy_in", "dx_mil", "dy_mil"):
            env.conn.execute(f"UPDATE shots SET {field} = 7.5")

        bundle = service.load_group(env.conn, group.id)

        assert [shot_offsets(s) for s in bundle.shots] == [(None,) * 4] * 2


class TestSizeOnlyContract:
    def _estimated(self, env, purpose="general", aim=DEFAULT_AIM, n=4, with_estimated=True):
        origins = [E if with_estimated and i == 1 else D for i in range(n)]
        return seed_group(
            env.conn,
            context=env.session,
            load=env.load,
            offsets=OFFSETS_10[:n],
            origins=origins,
            purpose=purpose,
            aim=aim,
            finalize=aim is not None,
            shots_expected=n,
        )

    @pytest.mark.parametrize(
        ("purpose", "aim", "n"),
        [("zero_check", DEFAULT_AIM, 2), ("zero_step2", DEFAULT_AIM, 4), ("general", None, 3)],
    )
    def test_size_only_stats_carry_includes_estimated(self, env, purpose, aim, n):
        with_estimated = self._estimated(env, purpose, aim, n, True)
        without = self._estimated(env, purpose, aim, n, False)

        flagged = service.group_stats(env.conn, with_estimated.id)
        clean = service.group_stats(env.conn, without.id)

        assert isinstance(flagged, SizeOnlyStats) and isinstance(clean, SizeOnlyStats)
        assert flagged.includes_estimated is True
        assert clean.includes_estimated is False

    def test_size_only_stats_have_exactly_the_fields_of_the_response_model(self):
        assert {f.name for f in dataclasses.fields(SizeOnlyStats)} == set(
            SizeOnlyStatsOut.model_fields
        ) - {"es_label"}

    def test_real_stats_validate_through_the_stats_route_schema(self, env):
        adapter = TypeAdapter(ROUTE_SCHEMAS[("GET", "/api/groups/{id}/stats")].response)
        masked = self._estimated(env, "zero_check", DEFAULT_AIM, 2)
        full = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10)

        masked_stats = service.group_stats(env.conn, masked.id)
        full_stats = service.group_stats(env.conn, full.id)
        masked_out = adapter.validate_python(masked_stats, from_attributes=True)
        full_out = adapter.validate_python(full_stats, from_attributes=True)

        assert isinstance(masked_out, SizeOnlyStatsOut)
        assert (
            masked_out.n,
            masked_out.es_in,
            masked_out.es_moa,
            masked_out.includes_estimated,
        ) == (
            masked_stats.n,
            masked_stats.es_in,
            masked_stats.es_moa,
            True,
        )
        assert isinstance(full_out, GroupStatsOut)
        assert full_out.mean_dx_mil == pytest.approx(full_stats.mean_dx_mil)
        assert full_out.includes_estimated is False


def interleave(monkeypatch, name: str, action) -> list[bool]:
    """Run `action` once, right after the first call of `repo_groups.<name>` (a guard read)."""
    original = getattr(repo_groups, name)
    fired: list[bool] = []

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        if not fired:
            fired.append(True)
            action()
        return result

    monkeypatch.setattr(repo_groups, name, wrapper)
    return fired


def must_be_locked(call) -> None:
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        call()


class TestWriteLockBeforeGuardReads:
    """G2-W1: the guards and the write share one BEGIN IMMEDIATE transaction."""

    def _first_statement(self, env, call) -> str:
        statements: list[str] = []
        env.conn.set_trace_callback(statements.append)
        try:
            call()
        finally:
            env.conn.set_trace_callback(None)
        return statements[0]

    def test_every_write_path_takes_the_write_lock_before_its_first_read(self, env):
        group = draft(env, 3)
        one = shots_at((4, 5), (4.1, 5.1), (4.2, 5.2))
        calls = [
            lambda: service.save_review(env.conn, group.id, AIM, one, [], 3),
            lambda: service.finalize(env.conn, group.id),
            lambda: service.recompute_offsets(env.conn, group.id),
        ]

        assert [self._first_statement(env, c) for c in calls] == ["BEGIN IMMEDIATE"] * 3

    def test_a_save_cannot_land_between_finalizes_count_check_and_its_update(
        self, env, monkeypatch
    ):
        group = draft(env, 3)
        rival = second_connection(env)
        four = shots_at((4, 5), (4.1, 5.1), (4.2, 5.2), (4.3, 5.3))
        fired = interleave(
            monkeypatch,
            "list_shots",
            lambda: must_be_locked(lambda: service.save_review(rival, group.id, AIM, four, [], 3)),
        )

        bundle = service.finalize(env.conn, group.id)

        assert fired
        assert bundle.group.status == "final"
        assert len(bundle.shots) == bundle.group.shots_expected == 3
        rival.close()

    def test_a_concurrent_save_cannot_hide_stored_shots_from_the_drop_guard(self, env, monkeypatch):
        group = draft(env, 2)
        rival = second_connection(env)
        five = shots_at(*[(4 + i * 0.1, 5.0) for i in range(5)])
        fired = interleave(
            monkeypatch,
            "list_shots",
            lambda: must_be_locked(lambda: service.save_review(rival, group.id, AIM, five, [], 5)),
        )

        service.save_review(env.conn, group.id, AIM, shots_at((4, 5), (4.1, 5.1)), [], 2)

        assert fired
        assert len(repo_groups.list_shots(env.conn, group.id)) == 2
        assert repo_groups.list_rejections(env.conn, group.id) == []
        service.save_review(rival, group.id, AIM, five, [], 5)
        assert len(repo_groups.list_shots(env.conn, group.id)) == 5
        rival.close()

    def test_a_group_finalized_in_the_gap_is_not_overwritten_by_a_review(self, env, monkeypatch):
        group = draft(env, 3)
        rival = second_connection(env)
        fired = interleave(
            monkeypatch,
            "list_shots",
            lambda: must_be_locked(lambda: service.finalize(rival, group.id)),
        )

        service.save_review(
            env.conn, group.id, AIM, shots_at((4, 5), (4.1, 5.1), (4.2, 5.2)), [], 3
        )

        assert fired
        assert repo_groups.get_group(env.conn, group.id).status == "draft"
        rival.close()

    def test_a_double_submitted_rejection_is_stored_once(self, env, monkeypatch):
        group = draft(env, 0, expected=2)
        rival = second_connection(env)
        two = shots_at((4, 5), (4.1, 5.1))
        reject = [PlanePoint(1.5, 2.5)]
        fired = interleave(
            monkeypatch,
            "list_rejections",
            lambda: must_be_locked(
                lambda: service.save_review(rival, group.id, AIM, two, reject, 2)
            ),
        )

        service.save_review(env.conn, group.id, AIM, two, reject, 2)
        service.save_review(rival, group.id, AIM, two, reject, 2)

        assert fired
        assert len(repo_groups.list_rejections(env.conn, group.id)) == 1
        rival.close()

    def test_a_distance_change_cannot_land_between_the_recompute_read_and_write(
        self, env, monkeypatch
    ):
        group = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10)
        rival = second_connection(env)

        def change_distance():
            with rival:
                repo_groups.update_group(rival, group.id, distance_yd=300)

        fired = interleave(monkeypatch, "list_shots", lambda: must_be_locked(change_distance))

        service.recompute_offsets(env.conn, group.id)

        assert fired
        assert_offsets_equal(stored_offsets(env, group.id), fresh_offsets(env, group.id, 100))
        change_distance()
        service.recompute_offsets(env.conn, group.id)
        assert_offsets_equal(stored_offsets(env, group.id), fresh_offsets(env, group.id, 300))
        rival.close()

    def test_a_refused_call_releases_the_lock(self, env):
        group = draft(env, 2, expected=5)
        rival = second_connection(env)

        with pytest.raises(FinalizeError):
            service.finalize(env.conn, group.id)
        with pytest.raises(GroupNotFound):
            service.recompute_offsets(env.conn, 999)

        rival.execute("BEGIN IMMEDIATE")
        rival.rollback()
        assert not env.conn.in_transaction
        rival.close()

    def test_a_callers_open_transaction_survives_a_refused_call_and_commits_with_a_good_one(
        self, env
    ):
        group = draft(env, 2, expected=5)
        env.conn.commit()
        env.conn.execute("UPDATE groups SET elevation_dialed_mil = 4.5 WHERE id = ?", (group.id,))

        with pytest.raises(FinalizeError):
            service.finalize(env.conn, group.id)
        assert env.conn.in_transaction
        assert repo_groups.get_group(env.conn, group.id).elevation_dialed_mil == 4.5

        service.save_review(env.conn, group.id, AIM, shots_at((4, 5), (4.1, 5.1)), [], 2)
        other = second_connection(env)
        try:
            saved = repo_groups.get_group(other, group.id)
        finally:
            other.close()
        assert saved.elevation_dialed_mil == 4.5 and saved.shots_expected == 2

    def test_a_write_failing_midway_inside_a_callers_transaction_undoes_only_the_service_work(
        self, env, monkeypatch
    ):
        group = draft(env, 2)
        before = repo_groups.list_shots(env.conn, group.id)
        env.conn.commit()
        env.conn.execute("UPDATE groups SET elevation_dialed_mil = 4.5 WHERE id = ?", (group.id,))

        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(repo_groups, "update_group", boom)
        with pytest.raises(sqlite3.OperationalError):
            service.save_review(
                env.conn, group.id, AIM, shots_at((7, 7), (8, 8), (9, 9)), [PlanePoint(1, 1)], 3
            )
        monkeypatch.undo()

        assert env.conn.in_transaction
        assert repo_groups.list_shots(env.conn, group.id) == before
        assert repo_groups.list_rejections(env.conn, group.id) == []
        assert repo_groups.get_group(env.conn, group.id).elevation_dialed_mil == 4.5


def interleave_before(monkeypatch, module, name: str, action, nth: int = 1) -> list[bool]:
    """Run `action` once, immediately before the `nth` call of `module.<name>` (a write)."""
    original = getattr(module, name)
    calls: list[int] = []
    fired: list[bool] = []

    def wrapper(*args, **kwargs):
        calls.append(1)
        if len(calls) == nth and not fired:
            fired.append(True)
            action()
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)
    return fired


def trace_statements(conn: sqlite3.Connection, call) -> list[str]:
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        call()
    finally:
        conn.set_trace_callback(None)
    return [stmt.strip() for stmt in statements]


TRANSACTION_CONTROL = ("BEGIN", "COMMIT", "END", "ROLLBACK")
WRITES = ("INSERT", "UPDATE", "DELETE", "REPLACE")


def assert_one_transaction(statements: list[str]) -> None:
    """BEGIN IMMEDIATE first, exactly one COMMIT, nothing transactional or written outside."""
    verbs = [stmt.split(None, 1)[0].upper() if stmt else "" for stmt in statements]
    assert statements[0].upper() == "BEGIN IMMEDIATE", statements[:3]
    commits = [i for i, verb in enumerate(verbs) if verb == "COMMIT"]
    assert len(commits) == 1, f"expected exactly one COMMIT, saw {len(commits)}: {statements}"
    commit = commits[0]
    inside = verbs[1:commit]
    assert not any(v in TRANSACTION_CONTROL for v in inside), statements[: commit + 1]
    after = verbs[commit + 1 :]
    assert not any(v in WRITES or v == "BEGIN" for v in after), statements[commit:]
    assert any(v in WRITES for v in inside), "the transaction wrote nothing"


class TestOneTransactionPerWritePath:
    """G2-W1-R: the guard reads and the writes of each path share ONE transaction."""

    def _two_groups(self, env):
        first = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10[:3])
        second = seed_group(
            env.conn, context=env.session, load=env.load, offsets=OFFSETS_10[:4], lot_code="LOT-2"
        )
        return first, second

    def test_save_review_is_one_transaction_with_rejections_and_a_dropped_shot(self, env):
        group = draft(env, 3)
        reduced = shots_at((4, 5), (4.1, 5.1))

        statements = trace_statements(
            env.conn,
            lambda: service.save_review(
                env.conn, group.id, AIM, reduced, [PlanePoint(4.2, 5.2)], 3
            ),
        )

        assert_one_transaction(statements)
        assert any(
            stmt.upper().startswith("INSERT INTO DETECTION_REJECTIONS") for stmt in statements
        )

    def test_finalize_is_one_transaction(self, env):
        group = draft(env, 3)

        assert_one_transaction(
            trace_statements(env.conn, lambda: service.finalize(env.conn, group.id))
        )

    def test_recompute_offsets_is_one_transaction(self, env):
        group, _ = self._two_groups(env)

        assert_one_transaction(
            trace_statements(env.conn, lambda: service.recompute_offsets(env.conn, group.id))
        )

    def test_the_checker_rejects_split_and_unlocked_traces(self):
        good = ["BEGIN IMMEDIATE", "SELECT 1", "UPDATE t SET a = 1", "COMMIT", "SELECT 2"]
        assert_one_transaction(good)
        split = ["BEGIN IMMEDIATE", "SELECT 1", "COMMIT", "BEGIN", "UPDATE t SET a = 1", "COMMIT"]
        late_write = ["BEGIN IMMEDIATE", "UPDATE t SET a = 1", "COMMIT", "UPDATE t SET a = 2"]
        two_commits = ["BEGIN IMMEDIATE", "UPDATE t SET a = 1", "COMMIT", "SELECT 1", "COMMIT"]
        deferred = ["BEGIN", "UPDATE t SET a = 1", "COMMIT"]
        inner_begin = ["BEGIN IMMEDIATE", "BEGIN", "UPDATE t SET a = 1", "COMMIT"]
        for bad in (split, late_write, two_commits, deferred, inner_begin):
            with pytest.raises(AssertionError):
                assert_one_transaction(bad)


class TestRivalAtTheWriteIsLockedOut:
    """G2-W1-R: a rival fired just before each write is refused, so no gap exists."""

    def test_finalize_refuses_a_rival_save_fired_at_its_update(self, env, monkeypatch):
        group = draft(env, 3)
        rival = second_connection(env)
        four = shots_at((4, 5), (4.1, 5.1), (4.2, 5.2), (4.3, 5.3))
        fired = interleave_before(
            monkeypatch,
            repo_rifles,
            "now_iso",
            lambda: must_be_locked(lambda: service.save_review(rival, group.id, AIM, four, [], 3)),
        )

        bundle = service.finalize(env.conn, group.id)

        assert fired
        assert bundle.group.status == "final"
        assert len(bundle.shots) == bundle.group.shots_expected == 3
        rival.close()

    @pytest.mark.parametrize("write", ["replace_shots", "add_rejection", "update_group"])
    def test_save_review_refuses_a_rival_fired_before_each_of_its_writes(
        self, env, monkeypatch, write
    ):
        group = draft(env, 3)
        rival = second_connection(env)
        five = shots_at(*[(4 + i * 0.1, 5.0) for i in range(5)])
        fired = interleave_before(
            monkeypatch,
            repo_groups,
            write,
            lambda: must_be_locked(lambda: service.save_review(rival, group.id, AIM, five, [], 5)),
        )

        service.save_review(
            env.conn, group.id, AIM, shots_at((4, 5), (4.1, 5.1)), [PlanePoint(4.2, 5.2)], 3
        )

        assert fired
        assert len(repo_groups.list_shots(env.conn, group.id)) == 2
        assert len(repo_groups.list_rejections(env.conn, group.id)) == 1
        rival.close()

    def test_recompute_offsets_refuses_a_distance_change_fired_at_its_first_write(
        self, env, monkeypatch
    ):
        group = seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10)
        rival = second_connection(env)

        def change_distance():
            with rival:
                repo_groups.update_group(rival, group.id, distance_yd=300)

        fired = interleave_before(
            monkeypatch, repo_groups, "set_shot_offsets", lambda: must_be_locked(change_distance)
        )

        service.recompute_offsets(env.conn, group.id)

        assert fired
        assert_offsets_equal(stored_offsets(env, group.id), fresh_offsets(env, group.id, 100))
        rival.close()


class TestRivalAtEveryStatementIsLockedOut:
    """G2-W1-R: a rival write attempted before every statement of a write path is refused."""

    def _gaps(self, env, call) -> tuple[list[str], list[tuple[int, str]]]:
        rival = second_connection(env)
        seen: list[str] = []
        succeeded: list[tuple[int, str]] = []

        def attempt(statement: str) -> None:
            index = len(seen)
            seen.append(statement.strip())
            try:
                with rival:
                    rival.execute("UPDATE rifles SET notes = notes WHERE id = ?", (env.rifle.id,))
            except sqlite3.OperationalError as error:
                assert "database is locked" in str(error)
            else:
                succeeded.append((index, statement.strip()))

        env.conn.set_trace_callback(attempt)
        try:
            call()
        finally:
            env.conn.set_trace_callback(None)
            rival.close()
        last_commit = max(i for i, stmt in enumerate(seen) if stmt.upper() == "COMMIT")
        return seen, [(i, stmt) for i, stmt in succeeded if 0 < i <= last_commit]

    def _paths(self, env):
        group = draft(env, 3)
        other = draft(env, 3)
        two = seed_group(
            env.conn, context=env.session, load=env.load, offsets=OFFSETS_10[:4], lot_code="LOT-2"
        )
        reduced = shots_at((4, 5), (4.1, 5.1))
        return {
            "save_review": lambda: service.save_review(
                env.conn, group.id, AIM, reduced, [PlanePoint(4.2, 5.2)], 3
            ),
            "finalize": lambda: service.finalize(env.conn, other.id),
            "recompute_offsets": lambda: service.recompute_offsets(env.conn, two.id),
        }

    @pytest.mark.parametrize("path", ["save_review", "finalize", "recompute_offsets"])
    def test_no_rival_write_gets_in_between_the_first_statement_and_the_commit(self, env, path):
        seen, gaps = self._gaps(env, self._paths(env)[path])

        assert seen[0].upper() == "BEGIN IMMEDIATE"
        assert len(seen) > 5
        assert gaps == []

    def test_the_probe_sees_a_gap_when_a_path_commits_in_the_middle(self, env, monkeypatch):
        group = draft(env, 3)

        def split_finalize():
            service.finalize(env.conn, group.id)

        original = service._write_offsets

        def commit_then_write(conn, *args, **kwargs):
            conn.commit()
            return original(conn, *args, **kwargs)

        monkeypatch.setattr(service, "_write_offsets", commit_then_write)
        seen, gaps = self._gaps(env, split_finalize)

        assert gaps, seen


def moved_shot(x, y, origin=E) -> Shot:
    return Shot(PlanePoint(x, y), origin, moved_by_user=True)


class TestPlaceholdersAndPerShotRejections:
    """G3-W5, G3-W7, B-44: unmoved estimated shots are placeholders; all else needs a reason."""

    def _saved(self, env, shots, expected=None):
        group = draft(env, 0, expected=expected or len(shots))
        service.save_review(env.conn, group.id, AIM, shots, [], expected or len(shots))
        return group

    def test_an_unmoved_estimated_shot_can_be_dropped_without_a_rejection(self, env):
        shots = shots_at((4, 5), (4.1, 5.1)) + shots_at((4.2, 5.2), origin=E)
        group = self._saved(env, shots)

        bundle = service.save_review(env.conn, group.id, AIM, shots[:2], [], 2)

        assert len(bundle.shots) == 2
        assert repo_groups.list_rejections(env.conn, group.id) == []

    def test_two_co_located_estimated_shots_can_both_be_removed(self, env):
        shots = shots_at((4, 5), (4.1, 5.1)) + shots_at((4.3, 5.3), (4.3, 5.3), origin=E)
        group = self._saved(env, shots)

        one = service.save_review(env.conn, group.id, AIM, shots[:3], [], 3)
        none = service.save_review(env.conn, group.id, AIM, shots[:2], [], 2)

        assert len(one.shots) == 3 and len(none.shots) == 2
        assert repo_groups.list_rejections(env.conn, group.id) == []

    def test_placeholders_can_be_replaced_by_other_placeholders_and_by_more_or_fewer(self, env):
        shots = shots_at((4, 5)) + shots_at((4.3, 5.3), (4.3, 5.3), origin=E)
        group = self._saved(env, shots)
        replacement = shots_at((4, 5)) + shots_at((6.0, 6.0), origin=E)

        bundle = service.save_review(env.conn, group.id, AIM, replacement, [], 2)
        grown = service.save_review(
            env.conn,
            group.id,
            AIM,
            shots_at((4, 5)) + shots_at((5, 5), (6, 6), (7, 7), origin=E),
            [],
            4,
        )

        assert [s.origin for s in bundle.shots] == ["detected", "estimated"]
        assert len(grown.shots) == 4

    @pytest.mark.parametrize(
        "protected",
        [
            Shot(PlanePoint(4.2, 5.2), D),
            Shot(PlanePoint(4.2, 5.2), ShotOrigin.MANUAL),
            Shot(PlanePoint(4.2, 5.2), ShotOrigin.SPLIT),
            Shot(PlanePoint(4.2, 5.2), E, moved_by_user=True),
            Shot(PlanePoint(4.2, 5.2), D, moved_by_user=True),
        ],
        ids=["detected", "manual", "split", "moved-estimated", "moved-detected"],
    )
    def test_detected_manual_split_and_moved_shots_still_need_a_rejection(self, env, protected):
        shots = shots_at((4, 5), (4.1, 5.1)) + [protected]
        group = self._saved(env, shots)
        before = repo_groups.list_shots(env.conn, group.id)

        with pytest.raises(ShotDroppedWithoutReason) as error:
            service.save_review(env.conn, group.id, AIM, shots[:2], [], 2)
        accepted = service.save_review(
            env.conn, group.id, AIM, shots[:2], [PlanePoint(4.2, 5.2)], 2
        )

        assert error.value.code == "shot_dropped_without_reason"
        assert len(before) == 3 and len(accepted.shots) == 2
        assert len(repo_groups.list_rejections(env.conn, group.id)) == 1

    def test_a_placeholder_does_not_stand_in_for_a_dropped_detected_shot(self, env):
        shots = shots_at((4, 5), (4.1, 5.1))
        group = self._saved(env, shots)
        swapped = shots_at((4, 5)) + shots_at((4.1, 5.1), origin=E)

        with pytest.raises(ShotDroppedWithoutReason):
            service.save_review(env.conn, group.id, AIM, swapped, [], 2)

        assert [s.origin for s in repo_groups.list_shots(env.conn, group.id)] == [
            "detected",
            "detected",
        ]

    def test_a_mixed_drop_needs_a_rejection_only_for_the_protected_shot(self, env):
        shots = shots_at((4, 5), (4.1, 5.1)) + shots_at((4.2, 5.2), (4.3, 5.3), origin=E)
        group = self._saved(env, shots)

        with pytest.raises(ShotDroppedWithoutReason):
            service.save_review(env.conn, group.id, AIM, shots[:1], [], 1)
        bundle = service.save_review(env.conn, group.id, AIM, shots[:1], [PlanePoint(4.1, 5.1)], 1)

        assert len(bundle.shots) == 1
        assert len(repo_groups.list_rejections(env.conn, group.id)) == 1

    def test_two_co_located_detected_shots_need_one_rejection_each(self, env):
        shots = shots_at((4, 5)) + shots_at((4.4, 5.4), (4.4, 5.4))
        group = self._saved(env, shots)
        point = PlanePoint(4.4, 5.4)

        with pytest.raises(ShotDroppedWithoutReason):
            service.save_review(env.conn, group.id, AIM, shots[:1], [point], 1)
        service.save_review(env.conn, group.id, AIM, shots[:2], [point], 2)
        done = service.save_review(env.conn, group.id, AIM, shots[:1], [point, point], 1)
        again = service.save_review(env.conn, group.id, AIM, shots[:1], [point, point], 1)

        assert len(done.shots) == len(again.shots) == 1
        stored = repo_groups.list_rejections(env.conn, group.id)
        assert [(r.x_in, r.y_in) for r in stored] == [(4.4, 5.4), (4.4, 5.4)]

    def test_a_stored_rejection_does_not_pay_for_a_later_co_located_drop(self, env):
        shots = shots_at((4, 5)) + shots_at((4.4, 5.4), (4.4, 5.4))
        group = self._saved(env, shots)
        point = PlanePoint(4.4, 5.4)
        service.save_review(env.conn, group.id, AIM, shots[:2], [point], 2)

        with pytest.raises(ShotDroppedWithoutReason):
            service.save_review(env.conn, group.id, AIM, shots[:1], [point], 1)

    def test_placeholders_never_count_for_finalizing_beyond_the_expected_total(self, env):
        shots = shots_at((4, 5), (4.1, 5.1)) + shots_at((4.2, 5.2), origin=E)
        group = self._saved(env, shots)
        service.save_review(env.conn, group.id, AIM, shots[:2], [], 2)

        assert service.finalize(env.conn, group.id).group.status == "final"


# ----------------------------------------------------------------------------------- B-66 context

from datetime import date as _date  # noqa: E402
from datetime import time as _time  # noqa: E402

from rga.services import analysis as analysis_service  # noqa: E402
from rga.services import group_records as group_context  # noqa: E402
from rga.services import review as review_service  # noqa: E402
from rga.services import rifles as rifle_service  # noqa: E402
from rga.services.groups import resolve_shot_time  # noqa: E402


def make_photo(conn, taken_at: str | None, offset: str | None = None, n: int = 1):
    with conn:
        return repo_groups.create_photo(
            conn,
            sha256=f"{n:064x}",
            ext="jpg",
            width_px=100,
            height_px=100,
            taken_at=taken_at,
            taken_at_offset=offset,
        )


def new_group(env: Env, **overrides):
    values = {
        "load_id": env.load.id,
        "lot_code": "LOT-1",
        "shots_expected": 5,
        "distance_yd": 100,
        "position": "bench",
        "suppressor": False,
        "shot_date": "2026-10-01",
    }
    return group_context.create_group(env.conn, **{**values, **overrides})


class TestResolveShotTime:
    def test_the_photo_supplies_the_date_time_and_offset_when_no_date_is_given(self, env):
        photo = make_photo(env.conn, "2026-09-30T07:45:10", "-06:00")

        got = resolve_shot_time(
            env.conn, photo_id=photo.id, shot_date=None, shot_time=None, utc_offset=None
        )

        assert got == ("2026-09-30", "07:45:10", "-06:00", "photo")

    def test_a_date_and_time_equal_to_the_photos_are_the_photos(self, env):
        photo = make_photo(env.conn, "2026-09-30T07:45:10", "-06:00")

        for shot_time in ("07:45:10", _time(7, 45, 10)):
            got = resolve_shot_time(
                env.conn,
                photo_id=photo.id,
                shot_date=_date(2026, 9, 30),
                shot_time=shot_time,
                utc_offset=None,
            )
            assert got == ("2026-09-30", "07:45:10", "-06:00", "photo")

    @pytest.mark.parametrize(
        ("shot_date", "shot_time"),
        [("2026-09-29", "07:45:10"), ("2026-09-30", "07:45:11"), ("2026-09-30", None)],
    )
    def test_anything_else_is_typed_in(self, env, shot_date, shot_time):
        photo = make_photo(env.conn, "2026-09-30T07:45:10", "-06:00")

        day, clock, offset, source = resolve_shot_time(
            env.conn, photo_id=photo.id, shot_date=shot_date, shot_time=shot_time, utc_offset=None
        )

        assert (day, source) == (shot_date, "manual")
        assert clock == shot_time and offset is None

    def test_an_hh_mm_time_is_returned_as_hh_mm_ss_and_a_given_offset_is_kept(self, env):
        got = resolve_shot_time(
            env.conn, photo_id=None, shot_date="2026-10-01", shot_time="08:30", utc_offset="+02:00"
        )

        assert got == ("2026-10-01", "08:30:00", "+02:00", "manual")

    def test_a_date_without_a_time_stays_without_one(self, env):
        got = resolve_shot_time(
            env.conn, photo_id=None, shot_date="2026-10-01", shot_time=None, utc_offset=None
        )

        assert got == ("2026-10-01", None, None, "manual")

    def test_no_date_and_no_photo_date_is_invalid_shot_date(self, env):
        photo = make_photo(env.conn, None)
        for photo_id in (None, photo.id):
            with pytest.raises(GroupError) as caught:
                resolve_shot_time(
                    env.conn, photo_id=photo_id, shot_date=None, shot_time=None, utc_offset=None
                )
            assert caught.value.code == "invalid_shot_date"

    def test_an_unknown_photo_is_photo_not_found(self, env):
        with pytest.raises(GroupError) as caught:
            resolve_shot_time(
                env.conn, photo_id=999, shot_date="2026-10-01", shot_time=None, utc_offset=None
            )
        assert caught.value.code == "photo_not_found"

    def test_a_photo_time_never_overrides_a_given_date_and_nothing_is_converted(self, env):
        photo = make_photo(env.conn, "2026-09-30T23:50:00", "-06:00")

        day, clock, _, source = resolve_shot_time(
            env.conn, photo_id=photo.id, shot_date=None, shot_time=None, utc_offset=None
        )

        assert (day, clock, source) == ("2026-09-30", "23:50:00", "photo")


class TestCreateGroupWithContext:
    def test_the_context_is_stored_on_the_group_and_the_view_matches(self, env):
        view = new_group(
            env,
            distance_yd=300,
            position="prone_off_pack",
            suppressor=True,
            shot_time="08:30",
            utc_offset="-06:00",
            temperature_f=61.5,
            wind_mph=4.0,
            wind_clock=3,
            notes="n",
        )

        g = view.group
        assert (g.shot_date, g.shot_time, g.utc_offset, g.shot_at_source) == (
            "2026-10-01",
            "08:30:00",
            "-06:00",
            "manual",
        )
        assert (g.distance_yd, g.position, g.suppressor, g.weather_skipped) == (
            300,
            "prone_off_pack",
            1,
            0,
        )
        assert (g.temperature_f, g.wind_mph, g.wind_clock) == (61.5, 4.0, 3)
        assert view.lot_code == "LOT-1" and view.shot_count == 0 and g.status == "draft"
        assert group_context.group_view(env.conn, g.id) == view

    def test_a_photo_date_is_used_when_none_is_given_and_marked_as_from_the_photo(self, env):
        photo = make_photo(env.conn, "2026-09-30T07:45:10", "-06:00")

        g = new_group(env, shot_date=None, photo_id=photo.id).group

        assert (g.shot_date, g.shot_time, g.utc_offset, g.shot_at_source) == (
            "2026-09-30",
            "07:45:10",
            "-06:00",
            "photo",
        )

    def test_no_date_and_no_photo_date_creates_nothing(self, env):
        before = env.conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]

        with pytest.raises(GroupError) as caught:
            new_group(env, shot_date=None)

        assert caught.value.code == "invalid_shot_date"
        assert env.conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == before

    @pytest.mark.parametrize(
        "weather",
        [
            {"temperature_f": 50.0},
            {"wind_mph": 3.0},
            {"wind_clock": 6},
            {"density_altitude_ft": 2000},
        ],
    )
    def test_weather_entered_together_with_skipped_is_invalid_weather(self, env, weather):
        with pytest.raises(GroupError) as caught:
            new_group(env, weather_skipped=True, **weather)
        assert caught.value.code == "invalid_weather"
        assert env.conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0

    def test_skipped_weather_alone_and_entered_weather_alone_are_fine(self, env):
        assert new_group(env, weather_skipped=True).group.weather_skipped == 1
        assert new_group(env, temperature_f=70.0).group.temperature_f == 70.0

    def test_a_suppressor_name_needs_the_suppressor_on_and_must_exist(self, env):
        can, _ = group_context.create_suppressor(env.conn, "Can A")
        with pytest.raises(GroupError) as off:
            new_group(env, suppressor=False, suppressor_id=can.id)
        with pytest.raises(GroupError) as missing:
            new_group(env, suppressor=True, suppressor_id=999)
        assert off.value.code == "invalid_suppressor"
        assert missing.value.code == "suppressor_not_found"
        assert new_group(env, suppressor=True, suppressor_id=can.id).group.suppressor_id == can.id

    @pytest.mark.parametrize(
        ("overrides", "code"),
        [
            ({"lot_code": "  "}, "invalid_lot_code"),
            ({"purpose": "zero_check"}, "invalid_purpose"),
            ({"zero_id": 1}, "invalid_purpose"),
            ({"load_id": 999}, "load_not_found"),
            ({"photo_id": 999}, "photo_not_found"),
        ],
    )
    def test_refusals_write_nothing(self, env, overrides, code):
        with pytest.raises(GroupError) as caught:
            new_group(env, **overrides)
        assert caught.value.code == code
        assert env.conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0
        assert env.conn.execute("SELECT COUNT(*) FROM load_lots").fetchone()[0] == 0

    def test_the_lot_is_created_once_and_trimmed(self, env):
        new_group(env, lot_code=" LOT-9 ")
        new_group(env, lot_code="LOT-9")
        assert [lot.lot_code for lot in repo_rifles.list_lots(env.conn, env.load.id)] == ["LOT-9"]


class TestViewsCarryRifleAndSuppressorName:
    def test_the_group_view_has_the_rifle_id_through_the_load_and_the_suppressor_name(self, env):
        can, _ = group_context.create_suppressor(env.conn, "Can A")
        named = new_group(env, suppressor=True, suppressor_id=can.id)
        plain = new_group(env, lot_code="L2")

        assert (named.rifle_id, named.suppressor_name) == (env.rifle.id, "Can A")
        assert (plain.rifle_id, plain.suppressor_name) == (env.rifle.id, None)
        listed = {v.group.id: v for v in analysis_service.list_groups(env.conn)}
        assert listed[named.group.id] == named and listed[plain.group.id] == plain

    def test_the_other_rifles_group_view_has_its_own_rifle_id(self, env):
        other = seed_rifle(env.conn, name="Second")
        view = group_context.create_group(
            env.conn,
            load_id=seed_load(env.conn, other).id,
            lot_code="X",
            shots_expected=4,
            distance_yd=100,
            position="bench",
            suppressor=False,
            shot_date="2026-10-01",
        )

        assert view.rifle_id == other.id != env.rifle.id

    def test_the_review_view_has_the_suppressor_name(self, env):
        can, _ = group_context.create_suppressor(env.conn, "Can A")
        named = new_group(env, suppressor=True, suppressor_id=can.id).group
        plain = new_group(env, lot_code="L2").group

        assert review_service.group_view(env.conn, named.id).suppressor_name == "Can A"
        assert review_service.group_view(env.conn, plain.id).suppressor_name is None

    def test_a_suppressor_name_follows_the_suppressor_switch(self, env):
        can, _ = group_context.create_suppressor(env.conn, "Can A")
        group = new_group(env, suppressor=True, suppressor_id=can.id).group

        group_context.update_group(env.conn, group.id, {"suppressor": False})

        assert group_context.group_view(env.conn, group.id).suppressor_name is None


class TestUpdateGroupContext:
    def _flat(self, env, **kw):
        return seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10, **kw)

    def test_a_distance_change_recomputes_that_groups_offsets_only(self, env):
        group, other = self._flat(env), self._flat(env, lot_code="LOT-2")
        before_other = stored_offsets(env, other.id)

        view = group_context.update_group(env.conn, group.id, {"distance_yd": 300})

        assert view.group.distance_yd == 300
        assert_offsets_equal(stored_offsets(env, group.id), fresh_offsets(env, group.id, 300))
        assert stored_offsets(env, other.id) == before_other

    def test_the_group_stats_follow_the_new_distance(self, env):
        group = self._flat(env)
        near = service.group_stats(env.conn, group.id).es_moa

        group_context.update_group(env.conn, group.id, {"distance_yd": 300})

        assert service.group_stats(env.conn, group.id).es_moa == pytest.approx(near / 3)

    def test_an_unchanged_distance_leaves_the_stored_offsets_untouched(self, env):
        group = self._flat(env)
        with env.conn:
            env.conn.execute("UPDATE shots SET dx_in = 99 WHERE group_id = ?", (group.id,))

        group_context.update_group(env.conn, group.id, {"distance_yd": 100, "notes": "x"})

        assert all(s.dx_in == 99 for s in repo_groups.list_shots(env.conn, group.id))

    def _linked(self, env):
        group = self._flat(env, purpose="general")
        zero = repo_zeroing.create_zero(
            env.conn,
            rifle_id=env.rifle.id,
            load_id=env.load.id,
            mode="existing",
            state="step2_initial",
        )
        with env.conn:
            repo_groups.update_group(env.conn, group.id, zero_id=zero.id)
        return group

    def test_a_zero_linked_group_cannot_change_distance_but_can_change_everything_else(self, env):
        group = self._linked(env)

        with pytest.raises(GroupError) as caught:
            group_context.update_group(env.conn, group.id, {"distance_yd": 300})
        assert caught.value.code == "group_linked_to_zero"
        assert "100 yd" in caught.value.message and "detach" in caught.value.message
        assert repo_groups.get_group(env.conn, group.id).distance_yd == 100

        view = group_context.update_group(
            env.conn,
            group.id,
            {
                "distance_yd": 100,
                "position": "prone_off_pack",
                "shot_date": "2026-10-05",
                "temperature_f": 40.0,
                "suppressor": True,
            },
        )
        assert (view.group.position, view.group.shot_date, view.group.suppressor) == (
            "prone_off_pack",
            "2026-10-05",
            1,
        )

    def test_editing_the_date_or_time_makes_the_source_manual(self, env):
        photo = make_photo(env.conn, "2026-09-30T07:45:10", "-06:00")
        group = new_group(env, shot_date=None, photo_id=photo.id).group
        assert group.shot_at_source == "photo"

        edited = group_context.update_group(env.conn, group.id, {"shot_time": "09:00"}).group

        assert (edited.shot_time, edited.shot_at_source) == ("09:00:00", "manual")
        again = group_context.update_group(env.conn, group.id, {"shot_date": _date(2026, 10, 2)})
        assert again.group.shot_date == "2026-10-02"

    def test_a_group_without_a_date_is_refused(self, env):
        group = new_group(env).group
        with pytest.raises(GroupError) as caught:
            group_context.update_group(env.conn, group.id, {"shot_date": None})
        assert caught.value.code == "invalid_shot_date"

    def test_weather_is_judged_on_the_resulting_row(self, env):
        group = new_group(env, temperature_f=60.0).group

        with pytest.raises(GroupError) as caught:
            group_context.update_group(env.conn, group.id, {"weather_skipped": True})
        assert caught.value.code == "invalid_weather"

        cleared = group_context.update_group(
            env.conn, group.id, {"weather_skipped": True, "temperature_f": None}
        ).group
        assert (cleared.weather_skipped, cleared.temperature_f) == (1, None)

        with pytest.raises(GroupError) as again:
            group_context.update_group(env.conn, group.id, {"wind_mph": 5.0})
        assert again.value.code == "invalid_weather"
        assert repo_groups.get_group(env.conn, group.id).wind_mph is None

    def test_the_suppressor_name_follows_the_suppressor_switch(self, env):
        can, _ = group_context.create_suppressor(env.conn, "Can A")
        group = new_group(env, suppressor=True, suppressor_id=can.id).group

        off = group_context.update_group(env.conn, group.id, {"suppressor": False}).group
        assert (off.suppressor, off.suppressor_id) == (0, None)

        with pytest.raises(GroupError) as caught:
            group_context.update_group(env.conn, group.id, {"suppressor_id": can.id})
        assert caught.value.code == "invalid_suppressor"

        on = group_context.update_group(
            env.conn, group.id, {"suppressor": True, "suppressor_id": can.id}
        ).group
        assert on.suppressor_id == can.id
        with pytest.raises(GroupError) as missing:
            group_context.update_group(env.conn, group.id, {"suppressor_id": 999})
        assert missing.value.code == "suppressor_not_found"

    def test_a_group_cannot_move_to_another_load(self, env):
        other = seed_load(env.conn, env.rifle, label="Other")
        group = new_group(env).group

        with pytest.raises(GroupError) as caught:
            group_context.update_group(env.conn, group.id, {"load_id": other.id})

        assert caught.value.code == "invalid_load_change"
        assert repo_groups.get_group(env.conn, group.id).load_id == env.load.id

    def test_a_refused_patch_changes_nothing(self, env):
        group = new_group(env, temperature_f=60.0).group
        before = repo_groups.get_group(env.conn, group.id)

        with pytest.raises(GroupError):
            group_context.update_group(
                env.conn, group.id, {"distance_yd": 300, "weather_skipped": True}
            )

        assert repo_groups.get_group(env.conn, group.id) == before

    def test_unknown_fields_are_refused(self, env):
        with pytest.raises(ValueError, match="unknown group field"):
            group_context.update_group(env.conn, new_group(env).group.id, {"lot_id": 1})
        with pytest.raises(GroupNotFound):
            group_context.update_group(env.conn, 999, {"notes": "x"})


class TestNewLotFlagUsesShotTime:
    def _final(self, env, lot, day, time=None):
        group = seed_group(
            env.conn,
            context=seed_context_for(env, date=day, time=time),
            load=env.load,
            offsets=OFFSETS_10[:3],
            lot_code=lot,
        )
        return group

    def test_the_lot_that_was_shot_later_is_flagged_even_if_finalized_first(self, env):
        later = self._final(env, "LOT-B", "2026-10-02")
        earlier = self._final(env, "LOT-A", "2026-10-01")

        assert service.new_lot_flag(env.conn, later.id) is True
        assert service.new_lot_flag(env.conn, earlier.id) is False

    def test_same_day_groups_order_by_time(self, env):
        morning = self._final(env, "LOT-A", "2026-10-01", "07:00:00")
        noon = self._final(env, "LOT-B", "2026-10-01", "12:00:00")

        assert service.new_lot_flag(env.conn, noon.id) is True
        assert service.new_lot_flag(env.conn, morning.id) is False

    def test_a_second_group_of_the_same_lot_is_never_flagged(self, env):
        self._final(env, "LOT-A", "2026-10-01")
        first_b = self._final(env, "LOT-B", "2026-10-02")
        second_b = self._final(env, "LOT-B", "2026-10-03")

        assert service.new_lot_flag(env.conn, first_b.id) is True
        assert service.new_lot_flag(env.conn, second_b.id) is False


def seed_context_for(env: Env, **fields):
    from tests.support.seed import seed_context

    return seed_context(env.rifle, **fields)


class TestGroupDefaults:
    def test_nothing_to_copy_gives_all_none(self, env):
        defaults = group_context.group_defaults(env.conn, env.rifle.id)

        assert all(v is None for v in dataclasses.asdict(defaults).values())

    def test_an_unknown_rifle_is_rifle_not_found(self, env):
        with pytest.raises(GroupError) as caught:
            group_context.group_defaults(env.conn, 999)
        assert caught.value.code == "rifle_not_found"

    def test_the_rifles_latest_group_supplies_everything(self, env):
        can, _ = group_context.create_suppressor(env.conn, "Can A")
        new_group(env, lot_code="OLD", distance_yd=100)
        latest = new_group(
            env,
            lot_code="LOT-7",
            shots_expected=7,
            distance_yd=300,
            position="prone_off_pack",
            suppressor=True,
            suppressor_id=can.id,
            temperature_f=55.0,
            wind_clock=9,
        ).group

        d = group_context.group_defaults(env.conn, env.rifle.id)

        assert (d.source_group_id, d.load_id, d.lot_code, d.shots_expected) == (
            latest.id,
            env.load.id,
            "LOT-7",
            7,
        )
        assert (d.shot_date, d.distance_yd, d.position, d.suppressor, d.suppressor_id) == (
            "2026-10-01",
            300,
            "prone_off_pack",
            True,
            can.id,
        )
        assert (d.temperature_f, d.wind_clock, d.weather_skipped) == (55.0, 9, False)

    def test_a_rifle_without_groups_borrows_only_distance_position_and_suppressor(self, env):
        other = seed_rifle(env.conn, name="Second")
        other_load = seed_load(env.conn, other)
        can, _ = group_context.create_suppressor(env.conn, "Can A")
        group_context.create_group(
            env.conn,
            load_id=other_load.id,
            lot_code="X",
            shots_expected=4,
            distance_yd=500,
            position="standing_field",
            suppressor=True,
            suppressor_id=can.id,
            shot_date="2026-09-01",
            temperature_f=40.0,
        )

        d = group_context.group_defaults(env.conn, env.rifle.id)

        assert (d.distance_yd, d.position, d.suppressor, d.suppressor_id) == (
            500,
            "standing_field",
            True,
            can.id,
        )
        assert (d.source_group_id, d.load_id, d.lot_code, d.shots_expected, d.shot_date) == (
            None,
        ) * 5
        assert d.temperature_f is None and d.weather_skipped is None

    def test_the_rifles_own_group_wins_over_a_newer_group_of_another_rifle(self, env):
        mine = new_group(env, distance_yd=200).group
        other = seed_rifle(env.conn, name="Second")
        group_context.create_group(
            env.conn,
            load_id=seed_load(env.conn, other).id,
            lot_code="X",
            shots_expected=4,
            distance_yd=500,
            position="bench",
            suppressor=False,
            shot_date="2026-10-09",
        )

        d = group_context.group_defaults(env.conn, env.rifle.id)

        assert (d.source_group_id, d.distance_yd) == (mine.id, 200)


class TestRifleGroupDays:
    def test_groups_are_listed_by_day_newest_first_and_by_time_within_a_day(self, env):
        a = new_group(env, shot_date="2026-10-01", shot_time="14:00", distance_yd=300).group
        b = new_group(env, shot_date="2026-10-01", shot_time="08:00", distance_yd=100).group
        c = new_group(env, shot_date="2026-10-01", distance_yd=100).group
        d = new_group(env, shot_date="2026-10-03").group

        days = group_context.rifle_group_days(env.conn, env.rifle.id)

        assert [day.shot_date for day in days] == ["2026-10-03", "2026-10-01"]
        assert [r.view.group.id for r in days[0].rows] == [d.id]
        assert [r.view.group.id for r in days[1].rows] == [c.id, b.id, a.id]
        assert days[1].distances == [100, 300]

    def test_rows_carry_load_label_weather_need_and_suppressor_name(self, env):
        can, _ = group_context.create_suppressor(env.conn, "Can A")
        new_group(env, distance_yd=300, suppressor=True, suppressor_id=can.id)
        new_group(env, distance_yd=300, weather_skipped=True)
        new_group(env, distance_yd=300, temperature_f=50.0)
        new_group(env, distance_yd=100)

        rows = group_context.rifle_group_days(env.conn, env.rifle.id)[0].rows

        assert {r.load_label for r in rows} == {env.load.label}
        assert [r.weather_prompt for r in rows] == [True, True, True, False]
        assert [r.weather_needed for r in rows] == [True, False, False, False]
        assert [r.suppressor_name for r in rows] == ["Can A", None, None, None]

    def test_only_this_rifles_groups_appear(self, env):
        new_group(env)
        other = seed_rifle(env.conn, name="Second")
        group_context.create_group(
            env.conn,
            load_id=seed_load(env.conn, other).id,
            lot_code="X",
            shots_expected=4,
            distance_yd=100,
            position="bench",
            suppressor=False,
            shot_date="2026-10-01",
        )

        days = group_context.rifle_group_days(env.conn, env.rifle.id)

        assert sum(len(day.rows) for day in days) == 1
        with pytest.raises(GroupError):
            group_context.rifle_group_days(env.conn, 999)

    def test_a_days_photos_are_offered_once_each_and_only_from_that_day(self, env):
        photo = make_photo(env.conn, None, n=1)
        elsewhere = make_photo(env.conn, None, n=2)
        new_group(env, shot_date="2026-10-01", photo_id=photo.id)
        new_group(env, shot_date="2026-10-01", photo_id=photo.id, lot_code="L2")
        new_group(env, shot_date="2026-10-02", photo_id=elsewhere.id)

        days = {d.shot_date: d for d in group_context.rifle_group_days(env.conn, env.rifle.id)}

        assert [p.id for p in days["2026-10-01"].photos] == [photo.id]
        assert [p.id for p in days["2026-10-02"].photos] == [elsewhere.id]

    def test_a_masked_group_row_carries_size_but_no_point_of_impact(self, env):
        seed_group(
            env.conn,
            context=env.session,
            load=env.load,
            offsets=OFFSETS_10[:2],
            purpose="zero_check",
        )

        [row] = group_context.rifle_group_days(env.conn, env.rifle.id)[0].rows

        assert row.masked is True and row.size is not None and len(row.size) == 4
        assert not any(hasattr(row, name) for name in ("mean_dx_mil", "aim", "stats"))

    def test_the_page_data_uses_groups(self, env):
        for day in range(1, 13):
            new_group(env, shot_date=f"2026-10-{day:02d}")
        new_group(env, shot_date="2026-10-12", shot_time="09:30")

        page = rifle_service.rifle_page_data(env.conn, env.rifle.id)

        assert [d.shot_date for d in page.days][:2] == ["2026-10-12", "2026-10-11"]
        assert len(page.days[0].rows) == 2 and page.suppressors == []
        assert not hasattr(page, "sessions")


class TestAnalysisUsesGroupContext:
    def test_the_aggregate_refuses_groups_shot_at_different_distances_in_one_day(self, env):
        near = seed_group(
            env.conn,
            context=seed_context_for(env, distance_yd=100),
            load=env.load,
            offsets=OFFSETS_10,
        )
        far = seed_group(
            env.conn,
            context=seed_context_for(env, distance_yd=200),
            load=env.load,
            offsets=OFFSETS_10,
        )

        with pytest.raises(analysis_service.AnalysisError) as caught:
            analysis_service.aggregate(env.conn, [near.id, far.id])

        assert caught.value.code == "invalid_mixed_distance"
        assert "100 yd" in caught.value.message and "200 yd" in caught.value.message

    def test_groups_of_different_days_at_one_distance_aggregate(self, env):
        ids = [
            seed_group(
                env.conn,
                context=seed_context_for(env, date=f"2026-10-0{day}"),
                load=env.load,
                offsets=OFFSETS_10,
            ).id
            for day in (1, 2, 3)
        ]

        result = analysis_service.aggregate(env.conn, ids)

        assert result.group_count == 3 and result.stats.n == 30

    def test_a_saved_aggregate_follows_a_members_changed_distance(self, env):
        ids = [
            seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10).id
            for _ in range(3)
        ]
        saved = analysis_service.save_aggregate(env.conn, "three", ids)
        assert saved.valid and saved.row.distance_yd == 100

        group_context.update_group(env.conn, ids[0], {"distance_yd": 300})
        listed = analysis_service.list_saved(env.conn)[0]

        assert listed.valid is False and "no longer at one distance" in listed.invalid_reason
        assert listed.positions == frozenset() and listed.suppressors == frozenset()

    def test_saved_aggregate_positions_and_suppressors_come_from_the_groups(self, env):
        ids = [
            seed_group(
                env.conn,
                context=seed_context_for(env, position=position, suppressor=on),
                load=env.load,
                offsets=OFFSETS_10,
            ).id
            for position, on in (("bench", False), ("prone_off_pack", True))
        ]

        saved = analysis_service.save_aggregate(env.conn, "mixed", ids)

        assert saved.positions == frozenset({"bench", "prone_off_pack"})
        assert saved.suppressors == frozenset({False, True})

    def test_search_finds_group_notes_and_legacy_notes_once_per_group(self, env):
        both = seed_group(
            env.conn,
            context=seed_context_for(env, notes="former windy"),
            load=env.load,
            offsets=OFFSETS_10,
            notes="windy day",
        )
        legacy_only = seed_group(
            env.conn,
            context=seed_context_for(env, notes="windy morning"),
            load=env.load,
            offsets=OFFSETS_10,
        )

        hits = analysis_service.search_notes(env.conn, "WINDY")

        assert sorted((kind, gid) for kind, gid, _ in hits) == sorted(
            [("group", both.id), ("group", legacy_only.id)]
        )

    def test_list_groups_no_longer_filters_by_a_container_and_still_by_rifle(self, env):
        seed_group(env.conn, context=env.session, load=env.load, offsets=OFFSETS_10)
        other = seed_rifle(env.conn, name="Second")
        seed_group(
            env.conn,
            context=seed_context_for(env),
            load=seed_load(env.conn, other),
            offsets=OFFSETS_10,
        )

        assert len(analysis_service.list_groups(env.conn)) == 2
        assert len(analysis_service.list_groups(env.conn, rifle_id=env.rifle.id)) == 1
        assert "session_id" not in analysis_service.list_groups.__code__.co_varnames

    def test_the_group_page_uses_the_groups_distance_and_legacy_notes(self, env):
        group = seed_group(
            env.conn,
            context=seed_context_for(env, distance_yd=300, notes="old notes"),
            load=env.load,
            offsets=OFFSETS_10,
        )

        page = analysis_service.group_page(env.conn, group.id)

        assert page.legacy_session_notes == "old notes"
        assert page.view.group.distance_yd == 300
        assert page.es_mil == pytest.approx(
            round(page.stats.es_in * 1000 / (300 * 36), 2), abs=0.01
        )
        assert not hasattr(page.view, "session")


class TestReviewViewCountsGroupsOnThePhoto:
    def test_other_groups_sharing_the_photo_are_counted(self, env):
        photo = make_photo(env.conn, None)
        a = new_group(env, photo_id=photo.id).group
        new_group(env, photo_id=photo.id, lot_code="L2")
        solo = new_group(env, lot_code="L3").group

        assert review_service.group_view(env.conn, a.id).other_groups_on_photo == 1
        assert review_service.group_view(env.conn, solo.id).other_groups_on_photo == 0
        assert not hasattr(review_service.group_view(env.conn, a.id), "session")


class TestUploadKeepsTheCaptureTime:
    def _exif_jpeg(self) -> bytes:
        from tests.unit.test_image_io_taken_at import CARRIERS, GOOD_TIFF

        return CARRIERS["jpeg"](GOOD_TIFF)

    @pytest.fixture
    def settings(self, tmp_path):
        s = Settings(data_dir=tmp_path / "data")
        s.photos_dir.mkdir(parents=True)
        s.working_dir.mkdir(parents=True)
        return s

    def test_a_new_upload_stores_the_exif_capture_time(self, env, settings):
        from rga.services import photos as photo_service

        stored = photo_service.store_upload(env.conn, settings, self._exif_jpeg())

        assert stored.duplicate is False
        assert (stored.photo.taken_at, stored.photo.taken_at_offset) == (
            "2024-10-05T14:30:15",
            "-06:00",
        )
        assert repo_groups.get_photo(env.conn, stored.photo.id).taken_at == "2024-10-05T14:30:15"

    def test_an_image_without_a_date_has_none(self, env, settings):
        import cv2
        import numpy as np

        from rga.services import photos as photo_service

        data = cv2.imencode(".jpg", np.full((64, 64, 3), 128, np.uint8))[1].tobytes()

        stored = photo_service.store_upload(env.conn, settings, data)

        assert (stored.photo.taken_at, stored.photo.taken_at_offset) == (None, None)

    def test_a_duplicate_upload_fills_a_missing_capture_time_but_never_replaces_one(
        self, env, settings
    ):
        from rga.services import photos as photo_service
        from rga.vision.io import sha256_hex

        data = self._exif_jpeg()
        digest = sha256_hex(data)
        (settings.photos_dir / f"{digest}.jpg").write_bytes(data)
        with env.conn:
            existing = repo_groups.create_photo(
                env.conn, sha256=digest, ext="jpg", width_px=10, height_px=10
            )
        assert existing.taken_at is None

        again = photo_service.store_upload(env.conn, settings, data)

        assert again.duplicate is True and again.photo.id == existing.id
        assert again.photo.taken_at == "2024-10-05T14:30:15"
        with env.conn:
            env.conn.execute("UPDATE photos SET taken_at = '2020-01-01T00:00:00' WHERE id = 1")
        kept = photo_service.store_upload(env.conn, settings, data)
        assert kept.photo.taken_at == "2020-01-01T00:00:00"


class TestNoSessionLeftInTheServices:
    @pytest.mark.parametrize(
        "name",
        [
            "create_session",
            "update_session",
            "get_session",
            "list_sessions",
            "session_detail",
            "session_page",
            "SessionNotFound",
            "SessionPage",
        ],
    )
    def test_the_session_api_is_gone(self, name):
        assert not hasattr(group_context, name)
        assert not hasattr(service, name)

    def test_recompute_session_offsets_is_gone(self):
        assert not hasattr(service, "recompute_session_offsets")

    def test_weather_prompt_starts_at_200_yd_and_is_reexported(self):
        assert group_context.weather_prompt is service.weather_prompt
        assert [service.weather_prompt(d) for d in (100, 199, 200, 500)] == [
            False,
            False,
            True,
            True,
        ]
