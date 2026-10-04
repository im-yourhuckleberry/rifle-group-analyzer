"""T2: photos/groups (with their own context)/shots/rejections/search repository against the migrated schema."""

# ruff: noqa: F811

from __future__ import annotations

import ast
import inspect
import re
import sqlite3
from dataclasses import dataclass

import pytest

from rga.db import repo_groups as repo
from rga.db.repo_groups import NewShot
from rga.domain.types import PlanePoint, Shot, ShotOrigin
from tests.support.db import migrated_conn  # noqa: F401

STAMP = "2026-09-30T12:00:00.000000Z"


@dataclass(frozen=True)
class World:
    rifle_id: int
    other_rifle_id: int
    load_id: int
    other_load_id: int
    lot_a: int
    lot_b: int
    other_load_lot: int


def _insert(conn: sqlite3.Connection, sql: str, params: tuple) -> int:
    return int(conn.execute(sql, params).lastrowid)


def _rifle(conn, public_id: str) -> int:
    return _insert(
        conn,
        "INSERT INTO rifles (public_id, name, cartridge, bullet_diameter_in, created_at, "
        "updated_at) VALUES (?, 'T', '6.5 Creedmoor', 0.264, ?, ?)",
        (public_id, STAMP, STAMP),
    )


def _load(conn, rifle_id: int, public_id: str) -> int:
    return _insert(
        conn,
        "INSERT INTO loads (public_id, rifle_id, kind, label, created_at, updated_at) "
        "VALUES (?, ?, 'factory', 'Load', ?, ?)",
        (public_id, rifle_id, STAMP, STAMP),
    )


def _lot(conn, load_id: int, code: str) -> int:
    return _insert(
        conn,
        "INSERT INTO load_lots (load_id, lot_code, created_at) VALUES (?, ?, ?)",
        (load_id, code, STAMP),
    )


@pytest.fixture
def world(migrated_conn) -> World:
    conn = migrated_conn
    rifle = _rifle(conn, "r-1")
    other_rifle = _rifle(conn, "r-2")
    load = _load(conn, rifle, "l-1")
    other_load = _load(conn, other_rifle, "l-2")
    return World(
        rifle_id=rifle,
        other_rifle_id=other_rifle,
        load_id=load,
        other_load_id=other_load,
        lot_a=_lot(conn, load, "A"),
        lot_b=_lot(conn, load, "B"),
        other_load_lot=_lot(conn, other_load, "Z"),
    )


def context(**overrides):
    """The context a group carries (date, distance, position, suppressor), with overrides."""
    values = {
        "shot_date": "2026-09-30",
        "distance_yd": 100,
        "position": "bench",
        "suppressor": False,
    }
    return {**values, **overrides}


def new_group(conn, world: World, ctx: dict | None = None, **overrides):
    values = {
        **(ctx if ctx is not None else context()),
        "load_id": world.load_id,
        "lot_id": world.lot_a,
        "shots_expected": 10,
        "now": STAMP,
    }
    return repo.create_group(conn, **{**values, **overrides})


def finalize(conn, group_id: int, at: str):
    return repo.update_group(
        conn, group_id, status="final", aim_x_in=4.0, aim_y_in=5.0, finalized_at=at
    )


def shot(x: float, y: float, origin=ShotOrigin.DETECTED, **kw) -> NewShot:
    return NewShot(x_in=x, y_in=y, origin=origin, **kw)


class TestGroupContext:
    def test_create_round_trips_every_context_field(self, migrated_conn, world):
        named = repo.create_suppressor(migrated_conn, "Omega", now=STAMP)

        created = new_group(
            migrated_conn,
            world,
            shot_date="2026-09-30",
            shot_time="08:15:30",
            utc_offset="-06:00",
            shot_at_source="photo",
            distance_yd=300,
            position="prone_off_pack",
            suppressor=True,
            suppressor_id=named.id,
            temperature_f=61.5,
            density_altitude_ft=4200.0,
            wind_mph=7.0,
            wind_clock=3,
            weather_skipped=False,
            legacy_session_id=77,
            legacy_session_notes="gusty",
            public_id="g-fixed",
        )

        assert repo.get_group(migrated_conn, created.id) == created
        assert (created.shot_date, created.shot_time, created.utc_offset) == (
            "2026-09-30",
            "08:15:30",
            "-06:00",
        )
        assert (created.shot_at_source, created.distance_yd, created.position) == (
            "photo",
            300,
            "prone_off_pack",
        )
        assert (created.suppressor, created.suppressor_id) == (1, named.id)
        assert (created.temperature_f, created.density_altitude_ft, created.wind_mph) == (
            61.5,
            4200.0,
            7.0,
        )
        assert (created.wind_clock, created.weather_skipped) == (3, 0)
        assert (created.legacy_session_id, created.legacy_session_notes) == (77, "gusty")
        assert created.public_id == "g-fixed"

    def test_defaults_are_manual_time_no_weather_and_no_legacy_session(self, migrated_conn, world):
        group = new_group(migrated_conn, world)

        assert (group.shot_at_source, group.shot_time, group.utc_offset) == ("manual", None, None)
        assert (group.suppressor, group.suppressor_id, group.weather_skipped) == (0, None, 0)
        assert (group.temperature_f, group.density_altitude_ft) == (None, None)
        assert (group.wind_mph, group.wind_clock) == (None, None)
        assert (group.legacy_session_id, group.legacy_session_notes) == (None, "")

    def test_the_source_accepts_the_domain_enum(self, migrated_conn, world):
        from rga.domain.types import ShotTimeSource

        for source in ShotTimeSource:
            group = new_group(migrated_conn, world, shot_at_source=source)
            assert group.shot_at_source == source.value

    def test_the_context_is_the_groups_own(self, migrated_conn, world):
        near = new_group(migrated_conn, world, distance_yd=100, position="bench")
        far = new_group(
            migrated_conn, world, distance_yd=300, position="standing_field", shot_date="2026-10-02"
        )

        assert (near.distance_yd, near.position, near.shot_date) == (100, "bench", "2026-09-30")
        assert (far.distance_yd, far.position, far.shot_date) == (
            300,
            "standing_field",
            "2026-10-02",
        )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"position": "sandbags"},
            {"distance_yd": 0},
            {"wind_clock": 13},
            {"shot_date": "yesterday"},
            {"shot_date": "2026-02-30"},
            {"shot_time": "8:30"},
            {"utc_offset": "UTC"},
            {"shot_at_source": "guess"},
            {"suppressor_id": 999, "suppressor": True},
            {"suppressor_id": 1, "suppressor": False},
        ],
    )
    def test_schema_constraints_surface_as_integrity_errors(self, migrated_conn, world, overrides):
        with pytest.raises(sqlite3.IntegrityError):
            new_group(migrated_conn, world, **overrides)
        assert repo.list_groups(migrated_conn) == []

    def test_update_changes_context_columns_and_bumps_updated_at(self, migrated_conn, world):
        named = repo.create_suppressor(migrated_conn, "Omega", now=STAMP)
        group = new_group(migrated_conn, world, temperature_f=50.0)

        updated = repo.update_group(
            migrated_conn,
            group.id,
            shot_date="2026-10-05",
            shot_time="17:45:00",
            utc_offset="+01:00",
            shot_at_source="manual",
            distance_yd=200,
            position="kneeling_sitting",
            suppressor=True,
            suppressor_id=named.id,
            temperature_f=None,
            density_altitude_ft=900.0,
            wind_mph=4.0,
            wind_clock=11,
            weather_skipped=True,
            now="2026-10-01T00:00:00.000000Z",
        )

        assert updated is not None
        assert (updated.shot_date, updated.shot_time, updated.utc_offset) == (
            "2026-10-05",
            "17:45:00",
            "+01:00",
        )
        assert (updated.distance_yd, updated.position) == (200, "kneeling_sitting")
        assert (updated.suppressor, updated.suppressor_id) == (1, named.id)
        assert (updated.temperature_f, updated.density_altitude_ft) == (None, 900.0)
        assert (updated.wind_mph, updated.wind_clock, updated.weather_skipped) == (4.0, 11, 1)
        assert updated.updated_at == "2026-10-01T00:00:00.000000Z" and updated.created_at == STAMP

    def test_clearing_the_suppressor_needs_its_name_cleared_too(self, migrated_conn, world):
        named = repo.create_suppressor(migrated_conn, "Omega", now=STAMP)
        group = new_group(migrated_conn, world, suppressor=True, suppressor_id=named.id)

        with pytest.raises(sqlite3.IntegrityError):
            repo.update_group(migrated_conn, group.id, suppressor=False)
        cleared = repo.update_group(migrated_conn, group.id, suppressor=False, suppressor_id=None)

        assert cleared is not None and (cleared.suppressor, cleared.suppressor_id) == (0, None)

    def test_update_rejects_columns_that_are_not_updatable(self, migrated_conn, world):
        group = new_group(migrated_conn, world)

        for column in ("legacy_session_id", "legacy_session_notes", "session_id", "public_id"):
            with pytest.raises(ValueError, match=column):
                repo.update_group(migrated_conn, group.id, **{column: "x"})
        assert repo.get_group(migrated_conn, group.id) == group

    def test_update_value_is_parameterized_not_interpolated(self, migrated_conn, world):
        group = new_group(migrated_conn, world)
        nasty = "x'; DROP TABLE groups; --"

        repo.update_group(migrated_conn, group.id, notes=nasty)

        assert repo.get_group(migrated_conn, group.id).notes == nasty

    def test_update_violating_a_check_raises_and_leaves_the_row(self, migrated_conn, world):
        group = new_group(migrated_conn, world)

        with pytest.raises(sqlite3.IntegrityError):
            repo.update_group(migrated_conn, group.id, distance_yd=-5)
        assert repo.get_group(migrated_conn, group.id) == group


class TestSeedContextShim:
    """`seed_context` stands in for a session in seeds without touching the database."""

    def test_a_context_spreads_onto_create_group(self, migrated_conn, world):
        from tests.support.seed import SeedContext, seed_context

        ctx = seed_context(
            world.rifle_id,
            distance_yd=300,
            date="2026-10-03",
            position="prone_off_pack",
            suppressor=True,
            notes="old session note",
            time="09:10:11",
            wind_clock=5,
        )

        group = repo.create_group(
            migrated_conn,
            **ctx.group_fields(),
            load_id=world.load_id,
            lot_id=world.lot_a,
            shots_expected=3,
        )

        assert isinstance(ctx, SeedContext) and ctx.rifle_id == world.rifle_id
        assert (group.shot_date, group.shot_time, group.distance_yd) == (
            "2026-10-03",
            "09:10:11",
            300,
        )
        assert (group.position, group.suppressor, group.wind_clock) == ("prone_off_pack", 1, 5)
        assert group.legacy_session_notes == "old session note"
        assert migrated_conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0

    def test_a_context_is_immutable_and_not_a_row(self):
        from tests.support.seed import seed_context

        ctx = seed_context(1)
        with pytest.raises(AttributeError):
            ctx.distance_yd = 5  # type: ignore[misc]
        assert not hasattr(ctx, "id")


class TestLatestGroup:
    def test_it_is_the_highest_id_group_of_the_rifle_or_of_all(self, migrated_conn, world):
        assert repo.latest_group(migrated_conn) is None
        assert repo.latest_group(migrated_conn, world.rifle_id) is None
        first = new_group(migrated_conn, world, shot_date="2026-10-09")
        second = new_group(migrated_conn, world, shot_date="2026-01-01")
        other = new_group(
            migrated_conn, world, load_id=world.other_load_id, lot_id=world.other_load_lot
        )

        assert repo.latest_group(migrated_conn, world.rifle_id) == second
        assert repo.latest_group(migrated_conn, world.other_rifle_id) == other
        assert repo.latest_group(migrated_conn) == other
        assert repo.latest_group(migrated_conn, 9999) is None
        assert first.id < second.id < other.id


class TestPhotos:
    def test_create_get_and_lookup_by_sha(self, migrated_conn):
        photo = repo.create_photo(
            migrated_conn, sha256="a" * 64, ext="jpg", width_px=4000, height_px=3000, now=STAMP
        )

        assert repo.get_photo(migrated_conn, photo.id) == photo
        assert repo.get_photo_by_sha(migrated_conn, "a" * 64) == photo
        assert (photo.reference_confirmed, photo.reference_kind, photo.created_at) == (
            0,
            None,
            STAMP,
        )
        assert repo.get_photo(migrated_conn, 999) is None
        assert repo.get_photo_by_sha(migrated_conn, "b" * 64) is None

    def test_the_capture_time_round_trips_and_defaults_to_none(self, migrated_conn):
        plain = repo.create_photo(
            migrated_conn, sha256="a" * 64, ext="jpg", width_px=1, height_px=1
        )
        timed = repo.create_photo(
            migrated_conn,
            sha256="b" * 64,
            ext="jpg",
            width_px=1,
            height_px=1,
            taken_at="2026-09-30T08:15:30",
            taken_at_offset="-06:00",
        )

        assert (plain.taken_at, plain.taken_at_offset) == (None, None)
        assert (timed.taken_at, timed.taken_at_offset) == ("2026-09-30T08:15:30", "-06:00")
        assert repo.get_photo_by_sha(migrated_conn, "b" * 64) == timed

    def test_a_capture_time_is_only_ever_filled_in_never_replaced(self, migrated_conn):
        photo = repo.create_photo(
            migrated_conn, sha256="a" * 64, ext="jpg", width_px=1, height_px=1
        )

        filled = repo.set_photo_taken_at(migrated_conn, photo.id, "2026-09-30T08:15:30", "+01:00")
        kept = repo.set_photo_taken_at(migrated_conn, photo.id, "2027-01-01T00:00:00", None)

        assert filled is not None and kept is not None
        assert (filled.taken_at, filled.taken_at_offset) == ("2026-09-30T08:15:30", "+01:00")
        assert kept == filled
        assert repo.set_photo_taken_at(migrated_conn, 999, "2026-09-30T08:15:30", None) is None

    @pytest.mark.parametrize(
        ("taken_at", "offset"),
        [("2026-09-30 08:15:30", None), ("2026-02-30T08:15:30", None), ("nonsense", None)]
        + [("2026-09-30T08:15:30", "UTC")],
    )
    def test_a_malformed_capture_time_is_rejected(self, migrated_conn, taken_at, offset):
        with pytest.raises(sqlite3.IntegrityError):
            repo.create_photo(
                migrated_conn,
                sha256="c" * 64,
                ext="jpg",
                width_px=1,
                height_px=1,
                taken_at=taken_at,
                taken_at_offset=offset,
            )

    def test_duplicate_sha_is_rejected(self, migrated_conn):
        repo.create_photo(migrated_conn, sha256="a" * 64, ext="jpg", width_px=1, height_px=1)

        with pytest.raises(sqlite3.IntegrityError):
            repo.create_photo(migrated_conn, sha256="a" * 64, ext="png", width_px=2, height_px=2)

    def test_reference_can_be_confirmed_and_cleared(self, migrated_conn):
        photo = repo.create_photo(
            migrated_conn, sha256="c" * 64, ext="jpg", width_px=10, height_px=10
        )

        confirmed = repo.update_photo_reference(
            migrated_conn,
            photo.id,
            reference_kind="circle_1in",
            reference_json='{"m": 1}',
            h_px_to_plane_json="[[1,0,0],[0,1,0],[0,0,1]]",
            working_ppi=200.0,
            working_w=800,
            working_h=600,
            axis_ratio=0.99,
            reference_confirmed=True,
        )
        cleared = repo.update_photo_reference(
            migrated_conn,
            photo.id,
            reference_kind=None,
            reference_json=None,
            h_px_to_plane_json=None,
            working_ppi=None,
            working_w=None,
            working_h=None,
            axis_ratio=None,
            reference_confirmed=False,
        )

        assert confirmed is not None and cleared is not None
        assert (confirmed.reference_confirmed, confirmed.working_ppi) == (1, 200.0)
        assert (confirmed.reference_kind, confirmed.axis_ratio) == ("circle_1in", 0.99)
        assert (cleared.reference_confirmed, cleared.reference_kind, cleared.working_ppi) == (
            0,
            None,
            None,
        )
        assert cleared.sha256 == photo.sha256

    def test_confirming_without_a_transform_is_rejected_and_changes_nothing(self, migrated_conn):
        photo = repo.create_photo(
            migrated_conn, sha256="d" * 64, ext="jpg", width_px=10, height_px=10
        )

        with pytest.raises(sqlite3.IntegrityError):
            repo.update_photo_reference(
                migrated_conn,
                photo.id,
                reference_kind="circle_1in",
                reference_json=None,
                h_px_to_plane_json=None,
                working_ppi=None,
                working_w=None,
                working_h=None,
                axis_ratio=None,
                reference_confirmed=True,
            )
        assert repo.get_photo(migrated_conn, photo.id) == photo

    def test_unknown_reference_kind_is_rejected(self, migrated_conn):
        photo = repo.create_photo(
            migrated_conn, sha256="e" * 64, ext="jpg", width_px=10, height_px=10
        )

        with pytest.raises(sqlite3.IntegrityError):
            repo.update_photo_reference(
                migrated_conn,
                photo.id,
                reference_kind="square_9in",
                reference_json=None,
                h_px_to_plane_json=None,
                working_ppi=None,
                working_w=None,
                working_h=None,
                axis_ratio=None,
                reference_confirmed=False,
            )

    def test_updating_a_missing_photo_returns_none(self, migrated_conn):
        assert (
            repo.update_photo_reference(
                migrated_conn,
                999,
                reference_kind=None,
                reference_json=None,
                h_px_to_plane_json=None,
                working_ppi=None,
                working_w=None,
                working_h=None,
                axis_ratio=None,
                reference_confirmed=False,
            )
            is None
        )


class TestGroups:
    def test_create_defaults_to_a_draft_without_aim_or_shots(self, migrated_conn, world):

        group = new_group(migrated_conn, world, notes="cold bore")

        assert repo.get_group(migrated_conn, group.id) == group
        assert (group.status, group.purpose, group.shots_expected) == ("draft", "general", 10)
        assert (group.aim_x_in, group.aim_y_in, group.finalized_at, group.photo_id) == (None,) * 4
        assert (group.notes, group.created_at, group.updated_at) == ("cold bore", STAMP, STAMP)
        assert re.fullmatch(r"[0-9a-f-]{36}", group.public_id)
        assert repo.get_group(migrated_conn, 999) is None

    def test_several_groups_share_one_photo_each_with_its_own_roi(self, migrated_conn, world):
        photo = repo.create_photo(
            migrated_conn, sha256="f" * 64, ext="jpg", width_px=10, height_px=10
        )

        left = new_group(migrated_conn, world, photo_id=photo.id, roi_json='{"x0":0}')
        right = new_group(migrated_conn, world, photo_id=photo.id, roi_json='{"x0":9}')

        assert left.photo_id == right.photo_id == photo.id
        assert (left.roi_json, right.roi_json) == ('{"x0":0}', '{"x0":9}')
        assert left.id != right.id

    def test_list_filters_combine_and_follow_creation_order(self, migrated_conn, world):
        g1 = new_group(migrated_conn, world)
        g2 = new_group(migrated_conn, world, lot_id=world.lot_b)
        g3 = new_group(migrated_conn, world, shot_date="2026-10-01")
        go = new_group(
            migrated_conn, world, load_id=world.other_load_id, lot_id=world.other_load_lot
        )
        finalize(migrated_conn, g2.id, "2026-09-30T13:00:00.000000Z")
        finalize(migrated_conn, go.id, "2026-09-30T14:00:00.000000Z")

        def ids(**filters):
            return [g.id for g in repo.list_groups(migrated_conn, **filters)]

        assert ids() == [g1.id, g2.id, g3.id, go.id]
        assert ids(load_id=world.load_id) == [g1.id, g2.id, g3.id]
        assert ids(rifle_id=world.rifle_id) == [g1.id, g2.id, g3.id]
        assert ids(rifle_id=world.other_rifle_id) == [go.id]
        assert ids(status="final") == [g2.id, go.id]
        assert ids(status="draft") == [g1.id, g3.id]
        assert ids(load_id=world.load_id, status="final") == [g2.id]
        assert ids(rifle_id=world.rifle_id, status="final") == [g2.id]
        assert ids(load_id=world.load_id, rifle_id=world.rifle_id, status="draft") == [
            g1.id,
            g3.id,
        ]
        assert ids(load_id=world.other_load_id, rifle_id=world.rifle_id) == []
        assert ids(rifle_id=9999) == []

    def test_update_changes_named_columns_and_bumps_updated_at(self, migrated_conn, world):
        group = new_group(migrated_conn, world)

        updated = repo.update_group(
            migrated_conn,
            group.id,
            shots_expected=7,
            elevation_dialed_mil=2.3,
            aim_x_in=4.1,
            aim_y_in=5.2,
            lot_id=world.lot_b,
            notes="edited",
            purpose="zero_step2",
            now="2026-10-01T00:00:00.000000Z",
        )

        assert updated is not None
        assert (updated.shots_expected, updated.elevation_dialed_mil) == (7, 2.3)
        assert (updated.aim_x_in, updated.aim_y_in, updated.lot_id) == (4.1, 5.2, world.lot_b)
        assert (updated.notes, updated.purpose, updated.status) == ("edited", "zero_step2", "draft")
        assert updated.updated_at == "2026-10-01T00:00:00.000000Z" and updated.created_at == STAMP

    def test_update_edge_cases(self, migrated_conn, world):
        group = new_group(migrated_conn, world)

        assert repo.update_group(migrated_conn, group.id) == group
        assert repo.update_group(migrated_conn, 999, notes="x") is None
        with pytest.raises(ValueError, match="public_id"):
            repo.update_group(migrated_conn, group.id, public_id="x")

    def test_a_final_group_needs_an_aim_point(self, migrated_conn, world):
        group = new_group(migrated_conn, world)

        with pytest.raises(sqlite3.IntegrityError):
            repo.update_group(migrated_conn, group.id, status="final", finalized_at=STAMP)
        done = finalize(migrated_conn, group.id, STAMP)

        assert done is not None and done.status == "final" and done.finalized_at == STAMP

    @pytest.mark.parametrize(
        "overrides",
        [
            {"shots_expected": 0},
            {"purpose": "warmup"},
            {"load_id": 9999},
            {"lot_id": 9999},
            {"photo_id": 9999},
            {"zero_id": 9999},
        ],
    )
    def test_schema_constraints_surface_as_integrity_errors(self, migrated_conn, world, overrides):

        with pytest.raises(sqlite3.IntegrityError):
            new_group(migrated_conn, world, **overrides)

    def test_deleting_a_group_removes_its_shots_and_rejections(self, migrated_conn, world):
        group = new_group(migrated_conn, world)
        repo.replace_shots(migrated_conn, group.id, [shot(1, 1)])
        repo.add_rejection(migrated_conn, group.id, 2.0, 2.0)

        migrated_conn.execute("DELETE FROM groups WHERE id = ?", (group.id,))

        assert repo.get_group(migrated_conn, group.id) is None
        assert repo.list_shots(migrated_conn, group.id) == []
        assert repo.list_rejections(migrated_conn, group.id) == []

    def test_a_load_with_groups_cannot_be_deleted(self, migrated_conn, world):
        new_group(migrated_conn, world)

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            migrated_conn.execute("DELETE FROM loads WHERE id = ?", (world.load_id,))


class TestShots:
    def _group(self, conn, world: World) -> int:
        return new_group(conn, world).id

    def test_replace_numbers_shots_from_one_and_round_trips_every_column(
        self, migrated_conn, world
    ):
        group_id = self._group(migrated_conn, world)

        stored = repo.replace_shots(
            migrated_conn,
            group_id,
            [
                shot(1.5, 2.5),
                shot(
                    3.0,
                    4.0,
                    ShotOrigin.ESTIMATED,
                    moved_by_user=True,
                    confidence=0.25,
                    dx_in=-1.0,
                    dy_in=2.0,
                    dx_mil=-0.28,
                    dy_mil=0.56,
                ),
                shot(5.0, 6.0, "split"),
            ],
        )

        assert [s.seq for s in stored] == [1, 2, 3]
        assert stored == repo.list_shots(migrated_conn, group_id)
        first, second, third = stored
        assert (first.x_in, first.y_in, first.origin, first.moved_by_user) == (
            1.5,
            2.5,
            "detected",
            0,
        )
        assert (first.dx_in, first.confidence) == (None, None)
        assert (second.origin, second.moved_by_user, second.confidence) == ("estimated", 1, 0.25)
        assert (second.dx_in, second.dy_in, second.dx_mil, second.dy_mil) == (
            -1.0,
            2.0,
            -0.28,
            0.56,
        )
        assert third.origin == "split"

    def test_replacing_again_renumbers_and_drops_the_old_shots(self, migrated_conn, world):
        group_id = self._group(migrated_conn, world)
        repo.replace_shots(migrated_conn, group_id, [shot(1, 1), shot(2, 2), shot(3, 3)])

        stored = repo.replace_shots(migrated_conn, group_id, [shot(9, 9), shot(8, 8)])

        assert [(s.seq, s.x_in) for s in stored] == [(1, 9), (2, 8)]
        assert len(repo.list_shots(migrated_conn, group_id)) == 2

    def test_empty_list_clears_the_group_and_other_groups_are_untouched(self, migrated_conn, world):
        g1 = new_group(migrated_conn, world).id
        g2 = new_group(migrated_conn, world).id
        repo.replace_shots(migrated_conn, g1, [shot(1, 1)])
        repo.replace_shots(migrated_conn, g2, [shot(2, 2), shot(3, 3)])

        assert repo.replace_shots(migrated_conn, g1, []) == []
        assert len(repo.list_shots(migrated_conn, g2)) == 2

    def test_an_invalid_origin_leaves_the_previous_shots(self, migrated_conn, world):
        group_id = self._group(migrated_conn, world)
        before = repo.replace_shots(migrated_conn, group_id, [shot(1, 1), shot(2, 2)])

        with pytest.raises(ValueError):
            repo.replace_shots(migrated_conn, group_id, [shot(5, 5), shot(6, 6, "flier")])

        assert repo.list_shots(migrated_conn, group_id) == before

    def test_a_database_error_midway_rolls_back_to_the_previous_shots(self, migrated_conn, world):
        group_id = self._group(migrated_conn, world)
        before = repo.replace_shots(migrated_conn, group_id, [shot(1, 1), shot(2, 2)])
        broken = NewShot(x_in=None, y_in=0.0, origin="detected")

        with pytest.raises(sqlite3.IntegrityError):
            repo.replace_shots(migrated_conn, group_id, [shot(5, 5), broken])

        assert repo.list_shots(migrated_conn, group_id) == before

    def test_an_unknown_group_is_a_foreign_key_error_and_leaves_nothing(self, migrated_conn):
        with pytest.raises(sqlite3.IntegrityError):
            repo.replace_shots(migrated_conn, 999, [shot(1, 1)])

        assert migrated_conn.execute("SELECT COUNT(*) FROM shots").fetchone()[0] == 0

    def test_it_never_commits_so_the_caller_can_roll_back(self, migrated_conn, world):
        group_id = self._group(migrated_conn, world)
        migrated_conn.commit()

        repo.replace_shots(migrated_conn, group_id, [shot(1, 1), shot(2, 2)])
        migrated_conn.rollback()

        assert repo.list_shots(migrated_conn, group_id) == []

    def test_inside_a_caller_transaction_it_commits_with_the_caller(self, migrated_conn, world):
        group_id = self._group(migrated_conn, world)
        migrated_conn.commit()

        with migrated_conn:
            repo.replace_shots(migrated_conn, group_id, [shot(1, 1)])
        migrated_conn.rollback()

        assert len(repo.list_shots(migrated_conn, group_id)) == 1

    def test_a_rolled_back_savepoint_does_not_end_the_callers_transaction(
        self, migrated_conn, world
    ):
        group_id = self._group(migrated_conn, world)
        migrated_conn.commit()
        repo.replace_shots(migrated_conn, group_id, [shot(1, 1)])

        with pytest.raises(ValueError):
            repo.replace_shots(migrated_conn, group_id, [shot(2, 2, "flier")])

        assert migrated_conn.in_transaction
        migrated_conn.rollback()
        assert repo.list_shots(migrated_conn, group_id) == []

    def test_from_shot_copies_the_domain_shot(self):
        domain = Shot(PlanePoint(1.25, 2.5), ShotOrigin.MANUAL, confidence=0.5, moved_by_user=True)

        built = NewShot.from_shot(domain)

        assert built == NewShot(1.25, 2.5, ShotOrigin.MANUAL, True, 0.5)

    def test_set_shot_offsets_writes_only_offsets(self, migrated_conn, world):
        group_id = self._group(migrated_conn, world)
        (stored,) = repo.replace_shots(migrated_conn, group_id, [shot(1.5, 2.5)])

        repo.set_shot_offsets(
            migrated_conn, stored.id, dx_in=0.5, dy_in=-0.5, dx_mil=0.14, dy_mil=-0.14
        )
        (after,) = repo.list_shots(migrated_conn, group_id)
        repo.set_shot_offsets(
            migrated_conn, stored.id, dx_in=None, dy_in=None, dx_mil=None, dy_mil=None
        )
        (nulled,) = repo.list_shots(migrated_conn, group_id)

        assert (after.dx_in, after.dy_in, after.dx_mil, after.dy_mil) == (0.5, -0.5, 0.14, -0.14)
        assert (after.x_in, after.y_in, after.origin, after.seq) == (1.5, 2.5, "detected", 1)
        assert (nulled.dx_in, nulled.dy_mil) == (None, None)


class TestRejections:
    def test_a_rejection_is_stored_with_the_only_reason(self, migrated_conn, world):
        group = new_group(migrated_conn, world)

        first = repo.add_rejection(migrated_conn, group.id, 1.5, 2.5, now=STAMP)
        second = repo.add_rejection(migrated_conn, group.id, 3.5, 4.5)

        assert (first.group_id, first.x_in, first.y_in) == (group.id, 1.5, 2.5)
        assert first.reason == second.reason == "not_a_bullet_hole"
        assert first.created_at == STAMP
        assert repo.list_rejections(migrated_conn, group.id) == [first, second]

    def test_the_function_takes_no_reason_so_none_other_can_be_stored(self):
        assert "reason" not in inspect.signature(repo.add_rejection).parameters

    def test_the_schema_itself_refuses_any_other_reason(self, migrated_conn, world):
        group = new_group(migrated_conn, world)

        with pytest.raises(sqlite3.IntegrityError):
            migrated_conn.execute(
                "INSERT INTO detection_rejections (group_id, x_in, y_in, reason, created_at) "
                "VALUES (?, 0, 0, 'flier', ?)",
                (group.id, STAMP),
            )

    def test_unknown_group_is_a_foreign_key_error(self, migrated_conn):
        with pytest.raises(sqlite3.IntegrityError):
            repo.add_rejection(migrated_conn, 999, 1.0, 1.0)

    def test_rejections_are_listed_per_group(self, migrated_conn, world):
        g1 = new_group(migrated_conn, world)
        g2 = new_group(migrated_conn, world)
        repo.add_rejection(migrated_conn, g1.id, 1.0, 1.0)
        repo.add_rejection(migrated_conn, g2.id, 2.0, 2.0)

        assert [r.x_in for r in repo.list_rejections(migrated_conn, g1.id)] == [1.0]
        assert repo.list_rejections(migrated_conn, 999) == []


class TestSearchNotes:
    def _seed(self, conn, world: World):
        g1 = new_group(
            conn,
            world,
            notes="Cold bore first, it's low",
            legacy_session_notes="Windy morning, 100% humidity",
        )
        g2 = new_group(conn, world, notes="snake_case marker and back\\slash")
        g3 = new_group(conn, world, legacy_session_notes="calm day")
        return g1, g2, g3

    def test_finds_group_and_former_session_notes_case_insensitively_by_group_id(
        self, migrated_conn, world
    ):
        g1, g2, g3 = self._seed(migrated_conn, world)
        g4 = new_group(migrated_conn, world, notes="WINDY again")

        result = repo.search_notes(migrated_conn, "windy")

        assert [(kind, ident) for kind, ident, _ in result] == [("group", g1.id), ("group", g4.id)]
        assert all("indy" in snippet.lower() for _, _, snippet in result)
        assert repo.search_notes(migrated_conn, "COLD BORE") == [
            ("group", g1.id, "Cold bore first, it's low")
        ]
        assert {g2.id, g3.id}.isdisjoint(ident for _, ident, _ in result)

    def test_a_group_matching_in_both_notes_is_one_hit_showing_its_own_note(
        self, migrated_conn, world
    ):
        both = new_group(
            migrated_conn,
            world,
            notes="needle in the group note",
            legacy_session_notes="needle in the old session note",
        )
        only_old = new_group(migrated_conn, world, legacy_session_notes="a lone needle")

        result = repo.search_notes(migrated_conn, "needle")

        assert result == [
            ("group", both.id, "needle in the group note"),
            ("group", only_old.id, "a lone needle"),
        ]

    def test_percent_is_matched_literally(self, migrated_conn, world):
        g1, *_ = self._seed(migrated_conn, world)
        new_group(migrated_conn, world, legacy_session_notes="1000 yards")

        result = repo.search_notes(migrated_conn, "100%")

        assert [(k, i) for k, i, _ in result] == [("group", g1.id)]
        assert repo.search_notes(migrated_conn, "%") == [
            ("group", g1.id, "Windy morning, 100% humidity")
        ]

    def test_underscore_is_matched_literally(self, migrated_conn, world):
        _, g2, _ = self._seed(migrated_conn, world)
        new_group(migrated_conn, world, notes="snakeXcase")

        assert [(k, i) for k, i, _ in repo.search_notes(migrated_conn, "snake_case")] == [
            ("group", g2.id)
        ]
        assert [(k, i) for k, i, _ in repo.search_notes(migrated_conn, "_")] == [("group", g2.id)]

    def test_backslash_is_matched_literally(self, migrated_conn, world):
        _, g2, _ = self._seed(migrated_conn, world)

        assert [(k, i) for k, i, _ in repo.search_notes(migrated_conn, "back\\slash")] == [
            ("group", g2.id)
        ]
        assert repo.search_notes(migrated_conn, "\\") == [
            ("group", g2.id, "snake_case marker and back\\slash")
        ]

    def test_apostrophe_is_handled_as_data(self, migrated_conn, world):
        g1, _, _ = self._seed(migrated_conn, world)

        assert [(k, i) for k, i, _ in repo.search_notes(migrated_conn, "it's")] == [
            ("group", g1.id)
        ]

    def test_sql_injection_text_matches_nothing_and_breaks_nothing(self, migrated_conn, world):
        self._seed(migrated_conn, world)

        assert repo.search_notes(migrated_conn, "'; DROP TABLE groups; --") == []
        assert repo.search_notes(migrated_conn, "' OR '1'='1") == []
        assert len(repo.list_groups(migrated_conn)) == 3

    def test_blank_query_or_non_positive_limit_returns_nothing(self, migrated_conn, world):
        self._seed(migrated_conn, world)

        assert repo.search_notes(migrated_conn, "") == []
        assert repo.search_notes(migrated_conn, "   ") == []
        assert repo.search_notes(migrated_conn, "windy", limit=0) == []

    def test_limit_caps_the_results(self, migrated_conn, world):
        for _ in range(5):
            new_group(migrated_conn, world, notes="shared token")

        assert len(repo.search_notes(migrated_conn, "shared", limit=3)) == 3
        assert len(repo.search_notes(migrated_conn, "shared")) == 5

    def test_snippet_is_a_window_around_the_match(self, migrated_conn, world):
        long_note = "a" * 100 + " NEEDLE " + "b" * 100
        group = new_group(migrated_conn, world, legacy_session_notes=long_note)

        ((kind, ident, snippet),) = repo.search_notes(migrated_conn, "needle")

        assert (kind, ident) == ("group", group.id)
        assert snippet.startswith("...") and snippet.endswith("...")
        assert "NEEDLE" in snippet and len(snippet) < len(long_note)

    def test_short_note_snippet_is_the_whole_note(self, migrated_conn, world):
        new_group(migrated_conn, world, notes="tiny note")

        assert repo.search_notes(migrated_conn, "tiny")[0][2] == "tiny note"

    def test_rifle_and_load_notes_are_not_searched(self, migrated_conn, world):
        migrated_conn.execute("UPDATE rifles SET notes = 'secretword'")
        migrated_conn.execute("UPDATE loads SET notes = 'secretword'")

        assert repo.search_notes(migrated_conn, "secretword") == []


class TestLotQueries:
    def test_first_group_of_lot_is_none_without_finalized_groups(self, migrated_conn, world):
        new_group(migrated_conn, world)

        assert repo.first_group_of_lot(migrated_conn, world.lot_a) is None
        assert repo.first_group_of_lot(migrated_conn, 9999) is None

    def test_first_group_is_the_earliest_finalized_one_of_that_lot(self, migrated_conn, world):
        later = new_group(migrated_conn, world)
        earlier = new_group(migrated_conn, world)
        draft = new_group(migrated_conn, world)
        elsewhere = new_group(migrated_conn, world, lot_id=world.lot_b)
        finalize(migrated_conn, later.id, "2026-09-30T15:00:00.000000Z")
        finalize(migrated_conn, earlier.id, "2026-09-30T13:00:00.000000Z")
        finalize(migrated_conn, elsewhere.id, "2026-09-30T01:00:00.000000Z")

        first = repo.first_group_of_lot(migrated_conn, world.lot_a)

        assert first is not None and first.id == earlier.id
        assert draft.id != first.id
        assert repo.first_group_of_lot(migrated_conn, world.lot_b).id == elsewhere.id

    def test_the_shot_date_orders_before_the_finalize_time(self, migrated_conn, world):
        shot_later_finalized_first = new_group(migrated_conn, world, shot_date="2026-10-02")
        shot_first_finalized_last = new_group(migrated_conn, world, shot_date="2026-09-20")
        finalize(migrated_conn, shot_later_finalized_first.id, "2026-10-03T08:00:00.000000Z")
        finalize(migrated_conn, shot_first_finalized_last.id, "2026-10-04T08:00:00.000000Z")

        first = repo.first_group_of_lot(migrated_conn, world.lot_a)

        assert first is not None and first.id == shot_first_finalized_last.id

    def test_the_shot_time_orders_within_a_day_and_a_missing_time_comes_first(
        self, migrated_conn, world
    ):
        noon = new_group(migrated_conn, world, shot_time="12:00:00")
        morning = new_group(migrated_conn, world, shot_time="08:30:00")
        untimed = new_group(migrated_conn, world, shot_time=None)
        for group in (noon, morning, untimed):
            finalize(migrated_conn, group.id, "2026-10-04T08:00:00.000000Z")

        assert repo.first_group_of_lot(migrated_conn, world.lot_a).id == untimed.id
        repo.update_group(migrated_conn, untimed.id, shot_time="23:00:00")
        assert repo.first_group_of_lot(migrated_conn, world.lot_a).id == morning.id

    def test_ties_on_everything_but_the_id_go_to_the_lowest_id(self, migrated_conn, world):
        a = new_group(migrated_conn, world)
        b = new_group(migrated_conn, world)
        finalize(migrated_conn, b.id, "2026-09-30T13:00:00.000000Z")
        finalize(migrated_conn, a.id, "2026-09-30T13:00:00.000000Z")

        assert repo.first_group_of_lot(migrated_conn, world.lot_a).id == a.id

    def test_other_lot_groups_must_be_finalized_and_in_a_different_lot(self, migrated_conn, world):
        same_lot = new_group(migrated_conn, world, lot_id=world.lot_a)
        draft_other = new_group(migrated_conn, world, lot_id=world.lot_b)
        finalize(migrated_conn, same_lot.id, "2026-09-30T13:00:00.000000Z")

        assert not repo.load_has_other_lot_groups(migrated_conn, world.load_id, world.lot_a)

        finalize(migrated_conn, draft_other.id, "2026-09-30T14:00:00.000000Z")

        assert repo.load_has_other_lot_groups(migrated_conn, world.load_id, world.lot_a)
        assert repo.load_has_other_lot_groups(migrated_conn, world.load_id, world.lot_b)

    def test_other_loads_do_not_count(self, migrated_conn, world):
        group = new_group(
            migrated_conn, world, load_id=world.other_load_id, lot_id=world.other_load_lot
        )
        finalize(migrated_conn, group.id, "2026-09-30T13:00:00.000000Z")

        assert not repo.load_has_other_lot_groups(migrated_conn, world.load_id, world.lot_a)
        assert not repo.load_has_other_lot_groups(
            migrated_conn, world.other_load_id, world.other_load_lot
        )

    def test_earlier_than_counts_only_strictly_earlier_groups_in_the_shot_order(
        self, migrated_conn, world
    ):
        a = new_group(migrated_conn, world, lot_id=world.lot_a, shot_date="2026-09-01")
        b = new_group(migrated_conn, world, lot_id=world.lot_b, shot_date="2026-09-02")
        a = finalize(migrated_conn, a.id, "2026-10-09T13:00:00.000000Z")
        b = finalize(migrated_conn, b.id, "2026-10-01T14:00:00.000000Z")

        load = world.load_id
        assert repo.load_has_other_lot_groups(migrated_conn, load, world.lot_b, earlier_than=b)
        assert not repo.load_has_other_lot_groups(migrated_conn, load, world.lot_a, earlier_than=a)
        assert not repo.load_has_other_lot_groups(migrated_conn, load, world.lot_b, earlier_than=a)

    def test_the_time_of_day_decides_between_groups_of_one_day(self, migrated_conn, world):
        timed = new_group(migrated_conn, world, lot_id=world.lot_a, shot_time="10:00:00")
        untimed_other = new_group(migrated_conn, world, lot_id=world.lot_b, shot_time=None)
        morning_other = new_group(migrated_conn, world, lot_id=world.lot_b, shot_time="09:00:00")
        evening_other = new_group(migrated_conn, world, lot_id=world.lot_b, shot_time="18:00:00")
        timed = finalize(migrated_conn, timed.id, "2026-09-30T08:00:00.000000Z")
        finalize(migrated_conn, evening_other.id, "2026-09-30T08:00:00.000000Z")

        load = world.load_id
        assert not repo.load_has_other_lot_groups(
            migrated_conn, load, world.lot_a, earlier_than=timed
        )
        finalize(migrated_conn, untimed_other.id, "2026-09-30T20:00:00.000000Z")
        assert repo.load_has_other_lot_groups(migrated_conn, load, world.lot_a, earlier_than=timed)
        repo.update_group(migrated_conn, untimed_other.id, status="draft", finalized_at=None)
        assert not repo.load_has_other_lot_groups(
            migrated_conn, load, world.lot_a, earlier_than=timed
        )
        finalize(migrated_conn, morning_other.id, "2026-09-30T23:00:00.000000Z")
        assert repo.load_has_other_lot_groups(migrated_conn, load, world.lot_a, earlier_than=timed)


def _zero(conn, rifle_id: int, load_id: int, lot_id: int | None = None) -> int:
    return _insert(
        conn,
        "INSERT INTO zeros (rifle_id, load_id, lot_id, mode, state, created_at, updated_at) "
        "VALUES (?, ?, ?, 'new_setup', 'step1', ?, ?)",
        (rifle_id, load_id, lot_id, STAMP, STAMP),
    )


def _snapshot(conn) -> dict[str, list[tuple]]:
    tables = ("photos", "groups", "shots", "detection_rejections")
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY id")] for t in tables}


@pytest.fixture
def sibling_load(migrated_conn, world):
    """A second load of the same rifle, with its own lot."""
    load = _load(migrated_conn, world.rifle_id, "l-3")
    return load, _lot(migrated_conn, load, "Y")


class TestGroupLinkConsistency:
    """create_group / update_group keep load, lot and zero consistent (the rifle is the load's)."""

    def test_the_error_is_a_value_error_subclass(self):
        assert issubclass(repo.GroupLinkMismatch, ValueError)
        assert "GroupLinkMismatch" in repo.__all__

    def test_create_rejects_a_lot_of_another_load(self, migrated_conn, world):

        with pytest.raises(repo.GroupLinkMismatch, match="not a lot of load"):
            new_group(migrated_conn, world, lot_id=world.other_load_lot)

        assert repo.list_groups(migrated_conn) == []

    @pytest.mark.parametrize(
        ("zero_args", "message"),
        [
            ({"rifle": "other"}, "another rifle"),
            ({"load": "sibling"}, "another load"),
            ({"lot": "b"}, "another lot"),
        ],
    )
    def test_create_rejects_a_zero_of_another_rifle_load_or_lot(
        self, migrated_conn, world, sibling_load, zero_args, message
    ):
        if zero_args.get("rifle") == "other":
            zero = _zero(migrated_conn, world.other_rifle_id, world.other_load_id)
        elif zero_args.get("load") == "sibling":
            zero = _zero(migrated_conn, world.rifle_id, sibling_load[0])
        else:
            zero = _zero(migrated_conn, world.rifle_id, world.load_id, world.lot_b)

        with pytest.raises(repo.GroupLinkMismatch, match=message):
            new_group(migrated_conn, world, zero_id=zero)

        assert repo.list_groups(migrated_conn) == []

    def test_create_accepts_a_zero_with_no_lot_or_the_same_lot(self, migrated_conn, world):
        open_zero = _zero(migrated_conn, world.rifle_id, world.load_id)
        lot_zero = _zero(migrated_conn, world.rifle_id, world.load_id, world.lot_a)

        first = new_group(migrated_conn, world, zero_id=open_zero)
        second = new_group(migrated_conn, world, zero_id=lot_zero)

        assert (first.zero_id, second.zero_id) == (open_zero, lot_zero)

    def test_missing_parents_still_fail_with_integrity_error_not_a_mismatch(
        self, migrated_conn, world
    ):

        for overrides in (
            {"load_id": 999},
            {"lot_id": 999},
            {"zero_id": 999},
            {"photo_id": 999},
            {"suppressor_id": 999, "suppressor": True},
        ):
            with pytest.raises(sqlite3.IntegrityError):
                new_group(migrated_conn, world, **overrides)

    def test_the_rifle_of_a_group_is_its_loads_so_a_move_to_another_rifles_load_follows_it(
        self, migrated_conn, world
    ):
        group = new_group(migrated_conn, world)

        moved = repo.update_group(
            migrated_conn,
            group.id,
            load_id=world.other_load_id,
            lot_id=world.other_load_lot,
        )

        assert moved is not None and moved.load_id == world.other_load_id
        assert [g.id for g in repo.list_groups(migrated_conn, rifle_id=world.other_rifle_id)] == [
            group.id
        ]
        assert repo.list_groups(migrated_conn, rifle_id=world.rifle_id) == []

    def test_a_zero_linked_group_cannot_move_to_another_rifles_load(self, migrated_conn, world):
        zero = _zero(migrated_conn, world.rifle_id, world.load_id)
        group = new_group(migrated_conn, world, zero_id=zero)

        with pytest.raises(repo.GroupLinkMismatch):
            repo.update_group(
                migrated_conn,
                group.id,
                load_id=world.other_load_id,
                lot_id=world.other_load_lot,
            )

        assert repo.get_group(migrated_conn, group.id) == group

    def test_update_rejects_a_lot_that_does_not_belong_to_the_current_load(
        self, migrated_conn, world, sibling_load
    ):
        group = new_group(migrated_conn, world)

        with pytest.raises(repo.GroupLinkMismatch, match="not a lot of load"):
            repo.update_group(migrated_conn, group.id, lot_id=sibling_load[1])
        with pytest.raises(repo.GroupLinkMismatch, match="not a lot of load"):
            repo.update_group(migrated_conn, group.id, load_id=sibling_load[0])

        assert repo.get_group(migrated_conn, group.id) == group

    def test_update_moves_to_a_sibling_load_only_together_with_one_of_its_lots(
        self, migrated_conn, world, sibling_load
    ):
        group = new_group(migrated_conn, world)

        moved = repo.update_group(
            migrated_conn, group.id, load_id=sibling_load[0], lot_id=sibling_load[1]
        )

        assert moved is not None
        assert (moved.load_id, moved.lot_id) == sibling_load

    @pytest.mark.parametrize("which", ["rifle", "load", "lot"])
    def test_update_rejects_linking_a_zero_that_does_not_match(
        self, migrated_conn, world, sibling_load, which
    ):
        group = new_group(migrated_conn, world)
        zero = {
            "rifle": lambda: _zero(migrated_conn, world.other_rifle_id, world.other_load_id),
            "load": lambda: _zero(migrated_conn, world.rifle_id, sibling_load[0]),
            "lot": lambda: _zero(migrated_conn, world.rifle_id, world.load_id, world.lot_b),
        }[which]()

        with pytest.raises(repo.GroupLinkMismatch):
            repo.update_group(migrated_conn, group.id, zero_id=zero)

        assert repo.get_group(migrated_conn, group.id) == group

    def test_update_links_a_matching_zero_and_can_unlink_it(self, migrated_conn, world):
        group = new_group(migrated_conn, world)
        zero = _zero(migrated_conn, world.rifle_id, world.load_id, world.lot_a)

        linked = repo.update_group(migrated_conn, group.id, zero_id=zero)
        unlinked = repo.update_group(migrated_conn, group.id, zero_id=None)

        assert linked is not None and unlinked is not None
        assert (linked.zero_id, unlinked.zero_id) == (zero, None)

    def test_update_rejects_moving_a_zero_linked_group_to_another_lot(self, migrated_conn, world):
        zero = _zero(migrated_conn, world.rifle_id, world.load_id, world.lot_a)
        group = new_group(migrated_conn, world, zero_id=zero)

        with pytest.raises(repo.GroupLinkMismatch, match="another lot"):
            repo.update_group(migrated_conn, group.id, lot_id=world.lot_b)

    def test_unrelated_updates_and_unknown_groups_are_unaffected(self, migrated_conn, world):
        group = new_group(migrated_conn, world)

        updated = repo.update_group(migrated_conn, group.id, notes="windy")

        assert updated is not None and updated.notes == "windy"
        assert repo.update_group(migrated_conn, 999, load_id=world.load_id) is None


class TestNeverCommits:
    """design §4.6: the caller owns the transaction, so no write function may end it."""

    MODULE_PATH = inspect.getsourcefile(repo)

    def _tree(self) -> ast.Module:
        with open(self.MODULE_PATH, encoding="utf-8") as fh:
            return ast.parse(fh.read())

    def test_module_source_never_calls_commit_rollback_or_executescript(self):
        called = {
            n.func.attr
            for n in ast.walk(self._tree())
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }

        assert not called & {"commit", "rollback", "executescript"}

    def test_module_sql_never_ends_a_transaction_except_the_savepoint_rollback(self):
        ending = re.compile(r"^\s*(COMMIT|END|ROLLBACK)\b", re.IGNORECASE)
        literals = [
            n.value
            for n in ast.walk(self._tree())
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]

        offenders = [s for s in literals if ending.match(s) and s != "ROLLBACK TO replace_shots"]

        assert offenders == []
        assert "ROLLBACK TO replace_shots" in literals

    def test_every_write_function_leaves_the_transaction_open_and_rollback_undoes_it(
        self, migrated_conn, world
    ):
        conn = migrated_conn
        photo = repo.create_photo(conn, sha256="1" * 64, ext="jpg", width_px=1, height_px=1)
        group = new_group(conn, world)
        repo.replace_shots(conn, group.id, [shot(1, 1)])
        conn.commit()
        before = _snapshot(conn)

        new_photo = repo.create_photo(
            conn,
            sha256="2" * 64,
            ext="png",
            width_px=2,
            height_px=3,
            taken_at="2026-10-01T09:30:00",
            taken_at_offset="-06:00",
        )
        repo.set_photo_taken_at(conn, photo.id, "2026-10-02T10:00:00", None)
        repo.update_photo_reference(
            conn,
            photo.id,
            reference_kind="circle_1in",
            reference_json="{}",
            h_px_to_plane_json="[]",
            working_ppi=100.0,
            working_w=1,
            working_h=1,
            axis_ratio=1.0,
            reference_confirmed=True,
        )
        created = new_group(conn, world, photo_id=new_photo.id)
        repo.update_group(conn, group.id, notes="changed")
        repo.replace_shots(conn, group.id, [shot(5, 5), shot(6, 6)])
        repo.set_shot_offsets(
            conn,
            repo.list_shots(conn, group.id)[0].id,
            dx_in=1.0,
            dy_in=2.0,
            dx_mil=3.0,
            dy_mil=4.0,
        )
        repo.add_rejection(conn, created.id, 1.0, 2.0)

        assert conn.in_transaction
        assert _snapshot(conn) != before
        conn.rollback()
        assert _snapshot(conn) == before

    def test_uncommitted_writes_are_invisible_to_a_second_connection(
        self, migrated_conn, world, tmp_path
    ):
        migrated_conn.commit()
        path = migrated_conn.execute("PRAGMA database_list").fetchone()["file"]
        group = new_group(migrated_conn, world)
        other = sqlite3.connect(path)
        try:
            assert other.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0
            migrated_conn.commit()
            assert other.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 1
            assert group.id == 1
        finally:
            other.close()


class TestRoundTripsAgainstInputs:
    """Every written column is compared with the INPUT, using a distinct value per column."""

    def test_create_photo_stores_width_and_height_as_given(self, migrated_conn):
        photo = repo.create_photo(
            migrated_conn, sha256="e" * 64, ext="png", width_px=4032, height_px=3024, now=STAMP
        )

        assert (photo.sha256, photo.ext, photo.width_px, photo.height_px, photo.created_at) == (
            "e" * 64,
            "png",
            4032,
            3024,
            STAMP,
        )

    def test_update_photo_reference_stores_every_column_as_given(self, migrated_conn):
        photo = repo.create_photo(
            migrated_conn, sha256="f" * 64, ext="jpg", width_px=10, height_px=10
        )

        row = repo.update_photo_reference(
            migrated_conn,
            photo.id,
            reference_kind="grid_1in",
            reference_json='{"corners": [1, 2, 3, 4]}',
            h_px_to_plane_json="[[1,2,3],[4,5,6],[7,8,9]]",
            working_ppi=187.5,
            working_w=1234,
            working_h=987,
            axis_ratio=0.971,
            reference_confirmed=True,
        )

        assert row is not None
        assert (row.reference_kind, row.reference_json) == ("grid_1in", '{"corners": [1, 2, 3, 4]}')
        assert row.h_px_to_plane_json == "[[1,2,3],[4,5,6],[7,8,9]]"
        assert (row.working_ppi, row.working_w, row.working_h) == (187.5, 1234, 987)
        assert (row.axis_ratio, row.reference_confirmed) == (0.971, 1)

    def test_create_group_stores_every_column_as_given(self, migrated_conn, world):
        photo = repo.create_photo(
            migrated_conn, sha256="a1" * 32, ext="jpg", width_px=10, height_px=10
        )
        zero = _zero(migrated_conn, world.rifle_id, world.load_id, world.lot_b)
        named = repo.create_suppressor(migrated_conn, "Omega", now=STAMP)

        group = repo.create_group(
            migrated_conn,
            load_id=world.load_id,
            lot_id=world.lot_b,
            shots_expected=7,
            shot_date="2026-11-12",
            shot_time="06:45:10",
            utc_offset="+02:00",
            shot_at_source="photo",
            distance_yd=273,
            position="kneeling_sitting",
            suppressor=True,
            suppressor_id=named.id,
            temperature_f=61.5,
            density_altitude_ft=2300.0,
            wind_mph=8.5,
            wind_clock=4,
            weather_skipped=False,
            photo_id=photo.id,
            roi_json='{"x0": 11, "y0": 22, "x1": 33, "y1": 44}',
            elevation_dialed_mil=1.3,
            aim_x_in=3.25,
            aim_y_in=5.75,
            purpose="zero_check",
            zero_id=zero,
            notes="note",
            legacy_session_id=12,
            legacy_session_notes="old",
            public_id="g-public",
            now=STAMP,
        )

        assert (group.load_id, group.lot_id) == (world.load_id, world.lot_b)
        assert (group.photo_id, group.zero_id) == (photo.id, zero)
        assert group.roi_json == '{"x0": 11, "y0": 22, "x1": 33, "y1": 44}'
        assert (group.shots_expected, group.elevation_dialed_mil) == (7, 1.3)
        assert (group.aim_x_in, group.aim_y_in) == (3.25, 5.75)
        assert (group.purpose, group.status, group.notes) == ("zero_check", "draft", "note")
        assert (group.public_id, group.created_at, group.updated_at) == ("g-public", STAMP, STAMP)
        assert (group.shot_date, group.shot_time, group.utc_offset) == (
            "2026-11-12",
            "06:45:10",
            "+02:00",
        )
        assert (group.shot_at_source, group.distance_yd, group.position) == (
            "photo",
            273,
            "kneeling_sitting",
        )
        assert (group.suppressor, group.suppressor_id) == (1, named.id)
        assert (group.temperature_f, group.density_altitude_ft) == (61.5, 2300.0)
        assert (group.wind_mph, group.wind_clock, group.weather_skipped) == (8.5, 4, 0)
        assert (group.legacy_session_id, group.legacy_session_notes) == (12, "old")

    def test_update_group_stores_every_updatable_column_as_given(
        self, migrated_conn, world, sibling_load
    ):
        photo = repo.create_photo(
            migrated_conn, sha256="b2" * 32, ext="jpg", width_px=10, height_px=10
        )
        zero = _zero(migrated_conn, world.rifle_id, sibling_load[0], sibling_load[1])
        named = repo.create_suppressor(migrated_conn, "Beta", now=STAMP)
        group = new_group(migrated_conn, world)

        row = repo.update_group(
            migrated_conn,
            group.id,
            load_id=sibling_load[0],
            lot_id=sibling_load[1],
            photo_id=photo.id,
            roi_json='{"r": 5}',
            shots_expected=9,
            elevation_dialed_mil=2.4,
            aim_x_in=6.5,
            aim_y_in=8.25,
            purpose="zero_step2",
            zero_id=zero,
            status="final",
            notes="n2",
            finalized_at="2026-10-02T09:08:07.000000Z",
            shot_date="2026-11-13",
            shot_time="18:01:02",
            utc_offset="-05:30",
            shot_at_source="manual",
            distance_yd=411,
            position="standing_field",
            suppressor=True,
            suppressor_id=named.id,
            temperature_f=44.25,
            density_altitude_ft=1800.0,
            wind_mph=3.5,
            wind_clock=11,
            weather_skipped=True,
            now="2026-10-02T10:00:00.000000Z",
        )

        assert row is not None
        assert (row.load_id, row.lot_id) == sibling_load
        assert (row.photo_id, row.zero_id, row.roi_json) == (photo.id, zero, '{"r": 5}')
        assert (row.shots_expected, row.elevation_dialed_mil) == (9, 2.4)
        assert (row.aim_x_in, row.aim_y_in) == (6.5, 8.25)
        assert (row.purpose, row.status, row.notes) == ("zero_step2", "final", "n2")
        assert (row.finalized_at, row.updated_at) == (
            "2026-10-02T09:08:07.000000Z",
            "2026-10-02T10:00:00.000000Z",
        )
        assert (row.shot_date, row.shot_time, row.utc_offset) == (
            "2026-11-13",
            "18:01:02",
            "-05:30",
        )
        assert (row.shot_at_source, row.distance_yd, row.position) == (
            "manual",
            411,
            "standing_field",
        )
        assert (row.suppressor, row.suppressor_id) == (1, named.id)
        assert (row.temperature_f, row.density_altitude_ft) == (44.25, 1800.0)
        assert (row.wind_mph, row.wind_clock, row.weather_skipped) == (3.5, 11, 1)

    def test_shot_and_rejection_columns_round_trip_against_the_inputs(self, migrated_conn, world):
        group = new_group(migrated_conn, world)

        stored = repo.replace_shots(
            migrated_conn,
            group.id,
            [
                NewShot(
                    x_in=3.5,
                    y_in=7.25,
                    origin=ShotOrigin.MANUAL,
                    moved_by_user=True,
                    confidence=0.6,
                    dx_in=0.1,
                    dy_in=0.2,
                    dx_mil=0.3,
                    dy_mil=0.4,
                )
            ],
        )
        repo.set_shot_offsets(
            migrated_conn, stored[0].id, dx_in=1.5, dy_in=2.5, dx_mil=3.5, dy_mil=4.5
        )
        reread = repo.list_shots(migrated_conn, group.id)[0]
        rejection = repo.add_rejection(migrated_conn, group.id, 8.5, 9.75, now=STAMP)

        assert (stored[0].x_in, stored[0].y_in, stored[0].origin) == (3.5, 7.25, "manual")
        assert (stored[0].moved_by_user, stored[0].confidence) == (1, 0.6)
        assert (stored[0].dx_in, stored[0].dy_in, stored[0].dx_mil, stored[0].dy_mil) == (
            0.1,
            0.2,
            0.3,
            0.4,
        )
        assert (reread.dx_in, reread.dy_in, reread.dx_mil, reread.dy_mil) == (1.5, 2.5, 3.5, 4.5)
        assert (reread.x_in, reread.y_in) == (3.5, 7.25)
        assert (rejection.x_in, rejection.y_in, rejection.created_at) == (8.5, 9.75, STAMP)


class TestOtherLotFilters:
    """AC-11: every filter of load_has_other_lot_groups, on both branches."""

    @pytest.fixture
    def groups(self, migrated_conn, world):
        other_load_group = new_group(
            migrated_conn, world, load_id=world.other_load_id, lot_id=world.other_load_lot
        )
        same_lot = new_group(migrated_conn, world, lot_id=world.lot_a)
        other_load_group = finalize(
            migrated_conn, other_load_group.id, "2026-09-30T12:00:00.000000Z"
        )
        same_lot = finalize(migrated_conn, same_lot.id, "2026-09-30T13:00:00.000000Z")
        return other_load_group, same_lot

    def test_an_earlier_other_load_group_and_an_earlier_same_lot_group_do_not_count(
        self, migrated_conn, world, groups
    ):
        _, same_lot = groups
        later = new_group(migrated_conn, world, shot_date="2026-10-05", lot_id=world.lot_a)
        later = finalize(migrated_conn, later.id, "2026-10-05T14:00:00.000000Z")

        assert not repo.load_has_other_lot_groups(
            migrated_conn, world.load_id, world.lot_a, earlier_than=later
        )
        assert not repo.load_has_other_lot_groups(migrated_conn, world.load_id, world.lot_a)
        assert same_lot.lot_id == world.lot_a

    def test_an_earlier_same_load_other_lot_group_counts(self, migrated_conn, world, groups):
        other_lot = new_group(migrated_conn, world, lot_id=world.lot_b)
        finalize(migrated_conn, other_lot.id, "2026-09-30T13:30:00.000000Z")
        _, same_lot = groups

        assert not repo.load_has_other_lot_groups(
            migrated_conn, world.load_id, world.lot_a, earlier_than=same_lot
        )
        later = new_group(migrated_conn, world, shot_date="2026-10-05", lot_id=world.lot_a)
        later = finalize(migrated_conn, later.id, "2026-10-05T14:00:00.000000Z")
        assert repo.load_has_other_lot_groups(
            migrated_conn, world.load_id, world.lot_a, earlier_than=later
        )
        assert repo.load_has_other_lot_groups(migrated_conn, world.load_id, world.lot_a)

    def test_a_later_other_lot_group_does_not_count_before_its_time(
        self, migrated_conn, world, groups
    ):
        _, same_lot = groups
        later = new_group(migrated_conn, world, lot_id=world.lot_b, shot_date="2026-10-20")
        finalize(migrated_conn, later.id, "2026-09-01T08:00:00.000000Z")

        assert not repo.load_has_other_lot_groups(
            migrated_conn, world.load_id, world.lot_a, earlier_than=same_lot
        )

    def test_a_draft_other_lot_group_never_counts_even_with_an_earlier_timestamp(
        self, migrated_conn, world, groups
    ):
        _, same_lot = groups
        draft = new_group(migrated_conn, world, lot_id=world.lot_b, shot_date="2026-09-01")
        repo.update_group(migrated_conn, draft.id, finalized_at="2026-09-30T12:30:00.000000Z")

        assert not repo.load_has_other_lot_groups(
            migrated_conn, world.load_id, world.lot_a, earlier_than=same_lot
        )
        assert not repo.load_has_other_lot_groups(migrated_conn, world.load_id, world.lot_a)


class TestDataSchemaContract:
    """Every statement the module runs is valid against the migrated schema and uses its indexes."""

    @staticmethod
    def _capture(conn, call) -> list[str]:
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            call()
        finally:
            conn.set_trace_callback(None)
        return statements

    @staticmethod
    def _plan(conn, statement: str) -> str:
        rows = conn.execute(f"EXPLAIN QUERY PLAN {statement}").fetchall()
        return " | ".join(row["detail"] for row in rows)

    def _plan_of(self, conn, call) -> str:
        statements = [
            s
            for s in self._capture(conn, call)
            if s.lstrip().upper().startswith(("SELECT", "UPDATE", "DELETE"))
        ]
        assert statements, "the call ran no data statement"
        return self._plan(conn, statements[-1])

    def _exercise_everything(self, conn, world: World, called: set[str]) -> None:
        def run(fn, *args, **kwargs):
            called.add(fn.__name__)
            return fn(conn, *args, **kwargs)

        named = run(repo.create_suppressor, "Thunder Beast Ultra 7")
        run(repo.get_suppressor, named.id)
        run(repo.list_suppressors)
        photo = run(repo.create_photo, sha256="9" * 64, ext="jpg", width_px=10, height_px=10)
        run(repo.get_photo, photo.id)
        run(repo.get_photo_by_sha, "9" * 64)
        run(
            repo.update_photo_reference,
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
        group = run(
            repo.create_group,
            load_id=world.load_id,
            lot_id=world.lot_a,
            shots_expected=3,
            shot_date="2026-09-30",
            distance_yd=100,
            position="bench",
            suppressor=True,
            suppressor_id=named.id,
            photo_id=photo.id,
            notes="cold bore",
            legacy_session_notes="windy",
        )
        run(repo.get_group, group.id)
        for filters in (
            {},
            {"load_id": world.load_id},
            {"rifle_id": world.rifle_id},
            {"status": "draft"},
            {"load_id": world.load_id, "status": "final"},
            {"load_id": world.load_id, "rifle_id": world.rifle_id, "status": "draft"},
        ):
            run(repo.list_groups, **filters)
        run(repo.latest_group)
        run(repo.latest_group, world.rifle_id)
        run(repo.update_group, group.id, shot_date="2026-10-01", distance_yd=200, notes="moved")
        run(
            repo.update_group,
            group.id,
            status="final",
            aim_x_in=1.0,
            aim_y_in=1.0,
            finalized_at=STAMP,
        )
        group = repo.get_group(conn, group.id)
        run(repo.replace_shots, group.id, [shot(1, 1), shot(2, 2)])
        stored = run(repo.list_shots, group.id)
        run(
            repo.set_shot_offsets,
            stored[0].id,
            dx_in=0.0,
            dy_in=0.0,
            dx_mil=0.0,
            dy_mil=0.0,
        )
        run(repo.add_rejection, group.id, 1.0, 1.0)
        run(repo.list_rejections, group.id)
        run(repo.search_notes, "windy")
        run(repo.first_group_of_lot, world.lot_a)
        run(repo.load_has_other_lot_groups, world.load_id, world.lot_a)
        run(repo.load_has_other_lot_groups, world.load_id, world.lot_a, earlier_than=group)
        run(repo.set_photo_taken_at, photo.id, "2026-09-30T08:00:00", "+00:00")

    def test_every_public_function_is_exercised_and_every_statement_passes_explain(
        self, migrated_conn, world
    ):
        called: set[str] = set()
        statements = self._capture(
            migrated_conn, lambda: self._exercise_everything(migrated_conn, world, called)
        )
        public = {
            name
            for name, member in inspect.getmembers(repo, inspect.isfunction)
            if member.__module__ == repo.__name__ and not name.startswith("_")
        }

        assert called == public == set(repo.__all__) - {"NewShot", "GroupLinkMismatch"}
        checked = 0
        for statement in statements:
            if statement.lstrip().upper().startswith(("SELECT", "INSERT", "UPDATE", "DELETE")):
                migrated_conn.execute(f"EXPLAIN {statement}").fetchall()
                checked += 1
        assert checked >= 35

    def test_list_groups_by_load_and_status_uses_the_load_status_index(self, migrated_conn, world):
        plan = self._plan_of(
            migrated_conn,
            lambda: repo.list_groups(migrated_conn, load_id=world.load_id, status="final"),
        )

        assert "ix_groups_load_status" in plan and "SCAN g" not in plan

    def test_list_groups_by_rifle_walks_the_rifles_loads_then_the_load_index(
        self, migrated_conn, world
    ):
        plan = self._plan_of(
            migrated_conn, lambda: repo.list_groups(migrated_conn, rifle_id=world.rifle_id)
        )

        assert "ix_loads_rifle" in plan and "ix_groups_load_status" in plan
        assert "SCAN g" not in plan

    def test_latest_group_of_a_rifle_walks_its_loads_then_the_load_index(
        self, migrated_conn, world
    ):
        plan = self._plan_of(
            migrated_conn, lambda: repo.latest_group(migrated_conn, world.rifle_id)
        )

        assert "ix_loads_rifle" in plan and "ix_groups_load_status" in plan
        assert "SCAN g" not in plan

    def test_the_day_index_serves_a_load_day_and_distance_lookup(self, migrated_conn, world):
        plan = self._plan_of(
            migrated_conn,
            lambda: migrated_conn.execute(
                "SELECT id FROM groups WHERE load_id = ? AND shot_date = ? AND distance_yd = ?",
                (world.load_id, "2026-09-30", 100),
            ).fetchall(),
        )

        assert "ix_groups_load_day" in plan and "SCAN" not in plan

    def test_first_group_of_lot_uses_the_lot_index(self, migrated_conn, world):
        plan = self._plan_of(
            migrated_conn, lambda: repo.first_group_of_lot(migrated_conn, world.lot_a)
        )

        assert "ix_groups_lot" in plan and "SCAN" not in plan

    def test_other_lot_check_uses_the_load_status_index(self, migrated_conn, world):
        plan = self._plan_of(
            migrated_conn,
            lambda: repo.load_has_other_lot_groups(migrated_conn, world.load_id, world.lot_a),
        )

        assert "ix_groups_load_status" in plan and "SCAN" not in plan

    def test_shot_and_photo_lookups_use_their_unique_indexes(self, migrated_conn, world):
        shots_plan = self._plan_of(migrated_conn, lambda: repo.list_shots(migrated_conn, 1))
        sha_plan = self._plan_of(migrated_conn, lambda: repo.get_photo_by_sha(migrated_conn, "a"))

        assert "sqlite_autoindex_shots_1" in shots_plan and "SCAN" not in shots_plan
        assert "sqlite_autoindex_photos_1" in sha_plan and "SCAN" not in sha_plan

    def test_the_plan_checks_would_fail_for_an_unindexed_query(self, migrated_conn, world):
        plan = self._plan_of(
            migrated_conn,
            lambda: migrated_conn.execute("SELECT id FROM groups WHERE notes = 'x'").fetchall(),
        )

        assert "ix_groups" not in plan and "SCAN" in plan
