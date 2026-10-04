"""T2: zeroing repository against the migrated schema (real SQLite, no mocks)."""

from __future__ import annotations

import ast
import inspect
import re
import sqlite3
from pathlib import Path

import pytest

from rga.db import repo_zeroing
from rga.db.repo_rifles import create_load, create_rifle, get_or_create_lot
from rga.db.repo_zeroing import (
    ZERO_UPDATABLE,
    GroupNotLinkable,
    LinkedGroup,
    ZeroListing,
    ZeroTargetMismatch,
    active_zero,
    create_zero,
    create_zero_check,
    get_zero,
    link_group,
    list_linked_groups,
    list_zero_checks,
    list_zeros_for_rifle,
    supersede_zeros,
    update_zero,
)
from rga.db.rows import ZeroCheckRow, ZeroRow
from tests.support.db import migrated_conn  # noqa: F401

MODULE_PATH = Path(inspect.getsourcefile(repo_zeroing))
SQL_START = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b")
T0 = "2026-10-01T12:00:00.000000Z"
T1 = "2026-10-01T12:00:01.000000Z"
T2 = "2026-10-01T12:00:02.000000Z"
TIMESTAMP_FORMAT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")
STATES = ["step1", "step2_initial", "step2_confirm", "step3_dot", "complete", "superseded"]


class World:
    """A rifle with two loads, two lots of the first, and a raw-SQL helper that inserts groups."""

    def __init__(self, conn: sqlite3.Connection, name: str = "R1"):
        self.conn = conn
        self.rifle = create_rifle(conn, name=name, cartridge="6.5 CM", bullet_diameter_in=0.264)
        self.load = create_load(conn, self.rifle.id, kind="factory", label="A")
        self.other_load = create_load(conn, self.rifle.id, kind="factory", label="B")
        self.lot = get_or_create_lot(conn, self.load.id, "L1")
        self.other_lot = get_or_create_lot(conn, self.load.id, "L2")

    def group(
        self,
        *,
        load_id=None,
        lot_id=None,
        purpose="zero_step2",
        shots=0,
        distance_yd=100,
        position="bench",
    ):
        n = self.conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
        cur = self.conn.execute(
            "INSERT INTO groups (public_id, load_id, lot_id, shots_expected, purpose, created_at,"
            " updated_at, shot_date, shot_at_source, distance_yd, position, suppressor)"
            " VALUES (?, ?, ?, 10, ?, ?, ?, '2026-10-01', 'manual', ?, ?, 0)",
            (
                f"g-{n}",
                load_id or self.load.id,
                lot_id or self.lot.id,
                purpose,
                T0,
                T0,
                distance_yd,
                position,
            ),
        )
        gid = cur.lastrowid
        for seq in range(shots):
            self.conn.execute(
                "INSERT INTO shots (group_id, seq, x_in, y_in, origin) VALUES (?, ?, 1.0, 1.0,"
                " 'detected')",
                (gid, seq),
            )
        return gid

    def zero(self, state="step2_initial", mode="existing", **kw) -> ZeroRow:
        return create_zero(
            self.conn,
            rifle_id=self.rifle.id,
            load_id=kw.pop("load_id", self.load.id),
            mode=mode,
            state=state,
            **kw,
        )

    def check_group(self, zero: ZeroRow, **kw) -> int:
        """A zero_check group linked to `zero`, ready for `create_zero_check`."""
        gid = self.group(purpose="zero_check", **kw)
        link_group(self.conn, zero.id, gid)
        return gid

    def group_zero_id(self, gid: int):
        return self.conn.execute("SELECT zero_id FROM groups WHERE id = ?", (gid,)).fetchone()[0]


@pytest.fixture
def world(migrated_conn):  # noqa: F811
    return World(migrated_conn)


def count(conn, table) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


class TestCreateAndGetZero:
    def test_create_returns_the_stored_row(self, world):
        z = world.zero(state="step1", mode="new_setup", now=T0)
        assert isinstance(z, ZeroRow)
        assert (z.rifle_id, z.load_id, z.lot_id) == (world.rifle.id, world.load.id, None)
        assert (z.mode, z.state) == ("new_setup", "step1")
        assert z.dialed_up_mil is None
        assert (z.dialed_right_mil, z.zero_es_in, z.zero_shot_count) == (None, None, None)
        assert (z.dot_size_in, z.completed_at) == (None, None)
        assert (z.created_at, z.updated_at) == (T0, T0)
        assert get_zero(world.conn, z.id) == z

    def test_default_timestamps_are_utc_microsecond_iso(self, world):
        z = world.zero()
        assert TIMESTAMP_FORMAT.match(z.created_at)
        assert z.created_at == z.updated_at

    def test_create_with_a_lot_of_the_load(self, world):
        z = world.zero(lot_id=world.lot.id)
        assert z.lot_id == world.lot.id

    @pytest.mark.parametrize("state", STATES)
    @pytest.mark.parametrize("mode", ["new_setup", "existing"])
    def test_every_ddl_state_and_mode_is_accepted(self, world, mode, state):
        z = world.zero(state=state, mode=mode)
        assert (z.mode, z.state) == (mode, state)

    @pytest.mark.parametrize(
        ("mode", "state"),
        [("other", "step1"), ("existing", "done"), ("", "step1"), ("existing", "")],
    )
    def test_bad_mode_or_state_is_an_integrity_error_and_stores_nothing(self, world, mode, state):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            world.zero(mode=mode, state=state)
        assert count(world.conn, "zeros") == 0

    def test_load_of_another_rifle_is_rejected(self, world, migrated_conn):  # noqa: F811
        other = create_rifle(migrated_conn, name="R2", cartridge="x", bullet_diameter_in=0.3)
        theirs = create_load(migrated_conn, other.id, kind="factory", label="T")
        with pytest.raises(ZeroTargetMismatch):
            world.zero(load_id=theirs.id)
        assert count(migrated_conn, "zeros") == 0

    @pytest.mark.parametrize("what", ["missing_load", "missing_rifle"])
    def test_missing_load_or_rifle_is_rejected(self, world, what):
        with pytest.raises(ZeroTargetMismatch):
            if what == "missing_load":
                world.zero(load_id=999)
            else:
                create_zero(
                    world.conn, rifle_id=999, load_id=world.load.id, mode="existing", state="step1"
                )
        assert count(world.conn, "zeros") == 0

    def test_lot_of_another_load_is_rejected(self, world):
        foreign = get_or_create_lot(world.conn, world.other_load.id, "X")
        with pytest.raises(ZeroTargetMismatch):
            world.zero(lot_id=foreign.id)
        with pytest.raises(ZeroTargetMismatch):
            world.zero(lot_id=999)
        assert count(world.conn, "zeros") == 0

    def test_get_missing_is_none(self, world):
        assert get_zero(world.conn, 999) is None


class TestUpdateZero:
    def test_records_the_dialed_correction(self, world):
        z = world.zero(now=T0)
        u = update_zero(world.conn, z.id, dialed_up_mil=-0.2, dialed_right_mil=0.3, now=T1)
        assert (u.dialed_up_mil, u.dialed_right_mil) == (-0.2, 0.3)
        assert (u.state, u.mode, u.created_at, u.updated_at) == (z.state, z.mode, T0, T1)
        assert get_zero(world.conn, z.id) == u

    def test_records_the_dot_zero_es_and_completion(self, world):
        z = world.zero(state="step3_dot")
        u = update_zero(
            world.conn,
            z.id,
            dot_size_in=1.0,
            zero_es_in=1.2,
            zero_shot_count=20,
            state="complete",
            completed_at=T2,
        )
        assert (u.dot_size_in, u.zero_es_in, u.zero_shot_count) == (1.0, 1.2, 20)
        assert (u.state, u.completed_at) == ("complete", T2)

    def test_unspecified_fields_are_kept(self, world):
        z = world.zero()
        update_zero(world.conn, z.id, dialed_up_mil=0.1, dialed_right_mil=0.2)
        u = update_zero(world.conn, z.id, state="step2_confirm")
        assert (u.dialed_up_mil, u.dialed_right_mil, u.state) == (0.1, 0.2, "step2_confirm")

    def test_none_clears_a_nullable_field(self, world):
        z = world.zero()
        update_zero(world.conn, z.id, dot_size_in=1.0, completed_at=T1)
        u = update_zero(world.conn, z.id, dot_size_in=None, completed_at=None)
        assert (u.dot_size_in, u.completed_at) == (None, None)

    def test_every_updatable_field_round_trips(self, world):
        z = world.zero()
        values = {
            "state": "complete",
            "dialed_up_mil": 0.4,
            "dialed_right_mil": -0.1,
            "zero_es_in": 1.5,
            "zero_shot_count": 30,
            "dot_size_in": 1.25,
            "completed_at": T1,
        }
        assert set(values) == set(ZERO_UPDATABLE)
        u = update_zero(world.conn, z.id, **values)
        for name, value in values.items():
            assert getattr(u, name) == value

    def test_no_fields_returns_the_zero_unchanged_without_bumping(self, world):
        z = world.zero(now=T0)
        assert update_zero(world.conn, z.id, now=T2) == z
        assert update_zero(world.conn, 999) is None

    def test_missing_zero_is_none(self, world):
        assert update_zero(world.conn, 999, state="complete") is None

    @pytest.mark.parametrize(
        "changes",
        [
            {"rifle_id": 2},
            {"load_id": 2},
            {"lot_id": 1},
            {"lot_id": None},
            {"mode": "new_setup"},
            {"id": 9},
            {"created_at": "x"},
        ],
    )
    def test_immutable_fields_are_rejected(self, world, changes):
        z = world.zero()
        with pytest.raises(ValueError, match="cannot update"):
            update_zero(world.conn, z.id, **changes)
        with pytest.raises(ValueError, match="cannot update"):
            update_zero(world.conn, 999, **changes)
        assert get_zero(world.conn, z.id) == z

    @pytest.mark.parametrize(
        "changes", [{"state": "bogus"}, {"dot_size_in": 0}, {"dot_size_in": -1.0}]
    )
    def test_invalid_values_change_nothing(self, world, changes):
        z = world.zero()
        with pytest.raises(sqlite3.IntegrityError):
            update_zero(world.conn, z.id, **changes)
        assert get_zero(world.conn, z.id) == z

    def test_a_zeros_lot_is_fixed_at_creation_and_cannot_be_updated(self, world):
        z = world.zero(lot_id=world.lot.id, now=T0)
        for lot in (world.other_lot.id, world.lot.id, None, 999):
            with pytest.raises(ValueError, match="cannot update.*lot_id"):
                update_zero(world.conn, z.id, lot_id=lot, state="complete", now=T1)
        assert get_zero(world.conn, z.id) == z

    def test_a_zero_created_without_a_lot_cannot_be_given_one_later(self, world):
        z = world.zero()
        with pytest.raises(ValueError, match="cannot update"):
            update_zero(world.conn, z.id, lot_id=world.lot.id)
        assert get_zero(world.conn, z.id).lot_id is None

    def test_a_zero_with_groups_of_two_lots_cannot_be_pinned_to_one_of_them(self, world):
        z = world.zero()
        for lot in (world.lot, world.other_lot):
            link_group(world.conn, z.id, world.group(lot_id=lot.id))
        with pytest.raises(ValueError, match="cannot update"):
            update_zero(world.conn, z.id, lot_id=world.lot.id)
        assert get_zero(world.conn, z.id).lot_id is None
        link_group(world.conn, z.id, world.group(lot_id=world.other_lot.id))

    def test_other_updates_leave_the_lot_untouched(self, world):
        z = world.zero(lot_id=world.lot.id)
        u = update_zero(world.conn, z.id, state="complete", dot_size_in=1.0)
        assert (u.lot_id, u.state) == (world.lot.id, "complete")

    def test_lot_id_is_not_an_updatable_field(self):
        assert "lot_id" not in ZERO_UPDATABLE
        assert "lot_id" not in repo_zeroing._UPDATE_ZERO


class TestListZerosForRifle:
    def test_returns_every_zero_newest_first_with_its_load_label_and_lot_code(self, world):
        older = world.zero(state="complete", lot_id=world.lot.id, now=T0)
        newer = world.zero(state="step1", mode="new_setup", load_id=world.other_load.id, now=T1)
        listed = list_zeros_for_rifle(world.conn, world.rifle.id)
        assert all(isinstance(item, ZeroListing) for item in listed)
        assert [item.zero for item in listed] == [newer, older]
        assert [(item.load_label, item.lot_code) for item in listed] == [("B", None), ("A", "L1")]

    def test_each_item_carries_the_stored_row_unchanged(self, world):
        z = world.zero(state="step2_confirm", lot_id=world.lot.id)
        update_zero(world.conn, z.id, dialed_up_mil=-0.2, dot_size_in=1.0)
        (item,) = list_zeros_for_rifle(world.conn, world.rifle.id)
        assert item.zero == get_zero(world.conn, z.id)
        assert (item.zero.dialed_up_mil, item.zero.dot_size_in) == (-0.2, 1.0)

    def test_superseded_and_completed_zeros_are_listed_too(self, world):
        states = ["complete", "superseded", "step1", "step3_dot"]
        zeros = [world.zero(state=state) for state in states]
        listed = list_zeros_for_rifle(world.conn, world.rifle.id)
        assert [item.zero.id for item in listed] == [z.id for z in reversed(zeros)]
        assert [item.zero.state for item in listed] == list(reversed(states))

    def test_a_zero_without_a_lot_is_kept_by_the_left_join(self, world):
        world.zero(lot_id=world.lot.id)
        world.zero()
        assert sorted(
            item.lot_code is None for item in list_zeros_for_rifle(world.conn, world.rifle.id)
        ) == [
            False,
            True,
        ]

    def test_only_the_requested_rifles_zeros_are_listed(self, world, migrated_conn):  # noqa: F811
        mine = world.zero()
        theirs = World(migrated_conn, "R2").zero()
        assert [i.zero.id for i in list_zeros_for_rifle(migrated_conn, world.rifle.id)] == [mine.id]
        assert [i.zero.rifle_id for i in list_zeros_for_rifle(migrated_conn, theirs.rifle_id)] == [
            theirs.rifle_id
        ]

    def test_a_rifle_without_zeros_or_an_unknown_rifle_gives_an_empty_list(self, world):
        assert list_zeros_for_rifle(world.conn, world.rifle.id) == []
        assert list_zeros_for_rifle(world.conn, 999) == []

    def test_ordering_follows_the_row_id_not_the_creation_timestamp(self, world):
        first = world.zero(now="2026-12-31T00:00:00.000000Z")
        second = world.zero(now="2026-01-01T00:00:00.000000Z")
        assert [i.zero.id for i in list_zeros_for_rifle(world.conn, world.rifle.id)] == [
            second.id,
            first.id,
        ]

    def test_the_load_label_is_the_loads_current_label(self, world):
        from rga.db.repo_rifles import update_load

        world.zero()
        update_load(world.conn, world.load.id, label="Renamed load")
        assert list_zeros_for_rifle(world.conn, world.rifle.id)[0].load_label == "Renamed load"

    def test_listing_is_read_only_and_opens_no_transaction(self, world):
        world.zero()
        world.conn.commit()
        before = world.conn.total_changes
        list_zeros_for_rifle(world.conn, world.rifle.id)
        assert world.conn.total_changes == before
        assert not world.conn.in_transaction

    def test_the_listing_feeds_the_api_zero_model(self, world):
        from rga.web.schemas import ZeroOut

        world.zero(state="step3_dot", mode="new_setup", lot_id=world.lot.id)
        (item,) = list_zeros_for_rifle(world.conn, world.rifle.id)
        assert ZeroOut.model_validate(item.zero).state.value == "step3_dot"


class TestActiveZeroAndSupersede:
    def test_no_zero_means_none(self, world):
        assert active_zero(world.conn, world.rifle.id, world.load.id) is None

    def test_returns_the_newest_non_superseded_zero(self, world):
        old = world.zero(state="complete")
        new = world.zero(state="step2_initial")
        assert active_zero(world.conn, world.rifle.id, world.load.id) == new
        update_zero(world.conn, new.id, state="superseded")
        assert active_zero(world.conn, world.rifle.id, world.load.id) == old

    def test_superseded_zeros_are_never_active(self, world):
        z = world.zero(state="superseded")
        assert z.state == "superseded"
        assert active_zero(world.conn, world.rifle.id, world.load.id) is None

    def test_scoped_to_rifle_and_load(self, world, migrated_conn):  # noqa: F811
        mine = world.zero()
        other_rifle = World(migrated_conn, "R2")
        theirs = other_rifle.zero()
        assert active_zero(migrated_conn, world.rifle.id, world.load.id) == mine
        assert active_zero(migrated_conn, other_rifle.rifle.id, other_rifle.load.id) == theirs
        assert active_zero(migrated_conn, world.rifle.id, world.other_load.id) is None

    def test_supersede_leaves_exactly_the_kept_zero_active(self, world):
        a, b = world.zero(state="complete"), world.zero(state="complete")
        c = world.zero(state="step2_confirm")
        changed = supersede_zeros(world.conn, world.rifle.id, world.load.id, c.id, now=T2)
        assert changed == 2
        states = {z.id: get_zero(world.conn, z.id).state for z in (a, b, c)}
        assert states == {a.id: "superseded", b.id: "superseded", c.id: "step2_confirm"}
        assert active_zero(world.conn, world.rifle.id, world.load.id).id == c.id
        active = world.conn.execute(
            "SELECT COUNT(*) FROM zeros WHERE rifle_id = ? AND load_id = ? AND state != 'superseded'",
            (world.rifle.id, world.load.id),
        ).fetchone()[0]
        assert active == 1

    def test_supersede_bumps_updated_at_only_on_changed_rows(self, world):
        a = world.zero(state="complete", now=T0)
        c = world.zero(state="complete", now=T0)
        supersede_zeros(world.conn, world.rifle.id, world.load.id, c.id, now=T2)
        assert get_zero(world.conn, a.id).updated_at == T2
        assert get_zero(world.conn, c.id).updated_at == T0
        assert get_zero(world.conn, a.id).created_at == T0

    def test_supersede_is_idempotent(self, world):
        world.zero(state="complete")
        c = world.zero(state="complete")
        assert supersede_zeros(world.conn, world.rifle.id, world.load.id, c.id) == 1
        assert supersede_zeros(world.conn, world.rifle.id, world.load.id, c.id) == 0

    def test_supersede_does_not_touch_other_loads_or_rifles(self, world, migrated_conn):  # noqa: F811
        keep = world.zero()
        other_load = world.zero(load_id=world.other_load.id)
        other_rifle = World(migrated_conn, "R2").zero()
        supersede_zeros(migrated_conn, world.rifle.id, world.load.id, keep.id)
        assert get_zero(migrated_conn, other_load.id).state == other_load.state
        assert get_zero(migrated_conn, other_rifle.id).state == other_rifle.state

    def test_supersede_with_no_other_zero_changes_nothing(self, world):
        only = world.zero()
        assert supersede_zeros(world.conn, world.rifle.id, world.load.id, only.id) == 0
        assert get_zero(world.conn, only.id) == only

    def test_complete_then_supersede_flow_leaves_one_active_zero(self, world):
        first = world.zero(state="complete")
        second = world.zero(state="step1", mode="new_setup")
        update_zero(world.conn, second.id, state="complete", completed_at=T1)
        supersede_zeros(world.conn, world.rifle.id, world.load.id, second.id)
        assert get_zero(world.conn, first.id).state == "superseded"
        assert active_zero(world.conn, world.rifle.id, world.load.id).id == second.id


class TestLinkGroup:
    def test_links_a_group_and_lists_it_with_purpose_status_and_shots(self, world):
        z = world.zero()
        g1 = world.group(purpose="zero_step2", shots=10)
        g2 = world.group(purpose="zero_confirm", shots=3)
        link_group(world.conn, z.id, g1)
        link_group(world.conn, z.id, g2)
        assert world.group_zero_id(g1) == z.id
        assert list_linked_groups(world.conn, z.id) == [
            LinkedGroup(group_id=g1, purpose="zero_step2", status="draft", shot_count=10),
            LinkedGroup(group_id=g2, purpose="zero_confirm", status="draft", shot_count=3),
        ]

    @pytest.mark.parametrize(
        "purpose", ["zero_step1", "zero_step2", "zero_confirm", "zero_check", "general"]
    )
    def test_each_group_purpose_is_reported(self, world, purpose):
        z = world.zero()
        gid = world.group(purpose=purpose)
        link_group(world.conn, z.id, gid)
        assert list_linked_groups(world.conn, z.id)[0].purpose == purpose

    def test_relinking_moves_the_group(self, world):
        a, b = world.zero(), world.zero()
        gid = world.group()
        link_group(world.conn, a.id, gid)
        link_group(world.conn, b.id, gid)
        assert list_linked_groups(world.conn, a.id) == []
        assert [g.group_id for g in list_linked_groups(world.conn, b.id)] == [gid]

    def test_linking_is_idempotent_and_bumps_the_group(self, world):
        z = world.zero()
        gid = world.group()
        link_group(world.conn, z.id, gid, now=T1)
        link_group(world.conn, z.id, gid, now=T2)
        assert len(list_linked_groups(world.conn, z.id)) == 1
        stamp = world.conn.execute("SELECT updated_at FROM groups WHERE id = ?", (gid,)).fetchone()[
            0
        ]
        assert stamp == T2

    def test_unlinked_zero_lists_nothing(self, world):
        assert list_linked_groups(world.conn, world.zero().id) == []
        assert list_linked_groups(world.conn, 999) == []

    def test_group_of_another_load_is_rejected(self, world):
        z = world.zero()
        gid = world.group(load_id=world.other_load.id)
        with pytest.raises(GroupNotLinkable, match="does not match"):
            link_group(world.conn, z.id, gid)
        assert world.group_zero_id(gid) is None

    def test_group_of_another_rifle_is_rejected(self, world, migrated_conn):  # noqa: F811
        z = world.zero()
        other = World(migrated_conn, "R2")
        gid = other.group()
        with pytest.raises(GroupNotLinkable, match="does not match"):
            link_group(migrated_conn, z.id, gid)
        assert world.group_zero_id(gid) is None

    def test_a_groups_own_context_never_decides_whether_it_links(self, world):
        z = world.zero()
        far = world.group(distance_yd=600, position="standing_field")
        link_group(world.conn, z.id, far)
        assert world.group_zero_id(far) == z.id

    def test_the_link_statement_checks_load_and_lot_only(self):
        sql = repo_zeroing._LINK_GROUP
        assert "sessions" not in sql and "session_id" not in sql
        assert "groups.load_id = zeros.load_id" in sql
        assert "zeros.lot_id IS NULL OR zeros.lot_id = groups.lot_id" in sql

    def test_zero_with_a_lot_only_accepts_groups_of_that_lot(self, world):
        z = world.zero(lot_id=world.lot.id)
        good = world.group(lot_id=world.lot.id)
        bad = world.group(lot_id=world.other_lot.id)
        link_group(world.conn, z.id, good)
        with pytest.raises(GroupNotLinkable, match="does not match"):
            link_group(world.conn, z.id, bad)
        assert world.group_zero_id(bad) is None

    def test_zero_without_a_lot_accepts_any_lot_of_the_load(self, world):
        z = world.zero()
        for lot in (world.lot, world.other_lot):
            link_group(world.conn, z.id, world.group(lot_id=lot.id))
        assert len(list_linked_groups(world.conn, z.id)) == 2

    def test_missing_zero_or_group_is_rejected_with_the_reason(self, world):
        z = world.zero()
        gid = world.group()
        with pytest.raises(GroupNotLinkable, match=r"zero 999 does not exist"):
            link_group(world.conn, 999, gid)
        with pytest.raises(GroupNotLinkable, match=r"group 999 does not exist"):
            link_group(world.conn, z.id, 999)

    def test_deleting_a_zero_unlinks_its_groups(self, world):
        z = world.zero()
        gid = world.group()
        link_group(world.conn, z.id, gid)
        world.conn.execute("DELETE FROM zeros WHERE id = ?", (z.id,))
        assert world.group_zero_id(gid) is None


class TestZeroChecks:
    def test_create_returns_the_stored_row(self, world):
        z = world.zero()
        gid = world.check_group(z)
        c = create_zero_check(world.conn, zero_id=z.id, group_id=gid, result="pass", now=T1)
        assert isinstance(c, ZeroCheckRow)
        assert (c.zero_id, c.group_id, c.result, c.created_at) == (z.id, gid, "pass", T1)
        assert list_zero_checks(world.conn, z.id) == [c]

    def test_default_timestamp_format(self, world):
        z = world.zero()
        c = create_zero_check(
            world.conn, zero_id=z.id, group_id=world.check_group(z), result="possible_lost_zero"
        )
        assert TIMESTAMP_FORMAT.match(c.created_at)

    def test_a_group_can_have_only_one_check(self, world):
        z1, z2 = world.zero(), world.zero()
        gid = world.check_group(z1)
        create_zero_check(world.conn, zero_id=z1.id, group_id=gid, result="pass")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            create_zero_check(world.conn, zero_id=z1.id, group_id=gid, result="possible_lost_zero")
        link_group(world.conn, z2.id, gid)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            create_zero_check(world.conn, zero_id=z2.id, group_id=gid, result="pass")
        assert count(world.conn, "zero_checks") == 1
        assert list_zero_checks(world.conn, z1.id)[0].result == "pass"

    @pytest.mark.parametrize("result", ["fail", "", "PASS", "lost"])
    def test_result_must_be_pass_or_possible_lost_zero(self, world, result):
        z = world.zero()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            create_zero_check(
                world.conn, zero_id=z.id, group_id=world.check_group(z), result=result
            )
        assert count(world.conn, "zero_checks") == 0

    def test_a_group_not_linked_to_the_zero_is_rejected(self, world):
        z = world.zero()
        unlinked = world.group(purpose="zero_check")
        with pytest.raises(GroupNotLinkable, match="not a zero_check group linked"):
            create_zero_check(world.conn, zero_id=z.id, group_id=unlinked, result="pass")
        assert count(world.conn, "zero_checks") == 0

    def test_a_group_linked_to_a_different_zero_is_rejected(self, world):
        z1, z2 = world.zero(), world.zero()
        gid = world.check_group(z1)
        with pytest.raises(GroupNotLinkable):
            create_zero_check(world.conn, zero_id=z2.id, group_id=gid, result="pass")
        assert count(world.conn, "zero_checks") == 0

    @pytest.mark.parametrize("purpose", ["general", "zero_step1", "zero_step2", "zero_confirm"])
    def test_a_linked_group_of_another_purpose_is_rejected(self, world, purpose):
        z = world.zero()
        gid = world.group(purpose=purpose)
        link_group(world.conn, z.id, gid)
        with pytest.raises(GroupNotLinkable):
            create_zero_check(world.conn, zero_id=z.id, group_id=gid, result="pass")
        assert count(world.conn, "zero_checks") == 0

    def test_a_group_of_another_rifle_cannot_be_checked_against_the_zero(
        self,
        world,
        migrated_conn,  # noqa: F811
    ):
        z = world.zero()
        other = World(migrated_conn, "R2")
        foreign = other.group(purpose="zero_check")
        with pytest.raises(GroupNotLinkable):
            link_group(migrated_conn, z.id, foreign)
        with pytest.raises(GroupNotLinkable):
            create_zero_check(migrated_conn, zero_id=z.id, group_id=foreign, result="pass")
        assert count(migrated_conn, "zero_checks") == 0

    def test_missing_zero_or_group_is_rejected(self, world):
        z = world.zero()
        gid = world.check_group(z)
        with pytest.raises(GroupNotLinkable):
            create_zero_check(world.conn, zero_id=999, group_id=gid, result="pass")
        with pytest.raises(GroupNotLinkable):
            create_zero_check(world.conn, zero_id=z.id, group_id=999, result="pass")
        assert count(world.conn, "zero_checks") == 0

    def test_a_rejected_check_does_not_disturb_an_existing_one(self, world):
        z = world.zero()
        gid = world.check_group(z)
        first = create_zero_check(world.conn, zero_id=z.id, group_id=gid, result="pass")
        with pytest.raises(GroupNotLinkable):
            create_zero_check(
                world.conn, zero_id=z.id, group_id=world.group(purpose="zero_check"), result="pass"
            )
        assert list_zero_checks(world.conn, z.id) == [first]

    def test_list_is_per_zero_in_creation_order(self, world):
        z1, z2 = world.zero(), world.zero()
        a = create_zero_check(
            world.conn, zero_id=z1.id, group_id=world.check_group(z1), result="pass"
        )
        other = create_zero_check(
            world.conn, zero_id=z2.id, group_id=world.check_group(z2), result="pass"
        )
        b = create_zero_check(
            world.conn, zero_id=z1.id, group_id=world.check_group(z1), result="possible_lost_zero"
        )
        assert list_zero_checks(world.conn, z1.id) == [a, b]
        assert list_zero_checks(world.conn, z2.id) == [other]
        assert list_zero_checks(world.conn, 999) == []

    def test_deleting_a_zero_cascades_to_its_checks_only(self, world):
        z1, z2 = world.zero(), world.zero()
        create_zero_check(world.conn, zero_id=z1.id, group_id=world.check_group(z1), result="pass")
        keep = create_zero_check(
            world.conn, zero_id=z2.id, group_id=world.check_group(z2), result="pass"
        )
        world.conn.execute("DELETE FROM zeros WHERE id = ?", (z1.id,))
        assert list_zero_checks(world.conn, z1.id) == []
        assert list_zero_checks(world.conn, z2.id) == [keep]

    def test_deleting_the_group_cascades_to_its_check(self, world):
        z = world.zero()
        gid = world.check_group(z)
        create_zero_check(world.conn, zero_id=z.id, group_id=gid, result="pass")
        world.conn.execute("DELETE FROM groups WHERE id = ?", (gid,))
        assert list_zero_checks(world.conn, z.id) == []


class TestTransactionOwnership:
    def test_functions_leave_the_transaction_open_and_rollback_undoes_them(self, world):
        world.conn.commit()
        z = world.zero(state="complete")
        gid = world.check_group(z, shots=2)
        update_zero(world.conn, z.id, dot_size_in=1.0)
        supersede_zeros(world.conn, world.rifle.id, world.load.id, z.id)
        create_zero_check(world.conn, zero_id=z.id, group_id=gid, result="pass")
        assert world.conn.in_transaction
        world.conn.rollback()
        assert count(world.conn, "zeros") == 0
        assert count(world.conn, "zero_checks") == 0

    def test_uncommitted_work_is_invisible_to_a_second_connection(self, world, tmp_path):
        from rga.db.connection import connect

        world.conn.commit()
        z = world.zero()
        other = connect(tmp_path / "rga.sqlite3")
        try:
            assert get_zero(other, z.id) is None
            world.conn.commit()
            assert get_zero(other, z.id) == z
        finally:
            other.close()

    def test_module_source_never_calls_commit_or_rollback(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        called = {
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert not called & {"commit", "rollback", "executescript"}


def module_sql_literals() -> dict[str, str]:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                continue
            if isinstance(value, str) and SQL_START.match(value):
                found[node.targets[0].id] = value
    return found


def bind_placeholders(sql: str) -> tuple[None, ...]:
    return (None,) * sql.count("?")


def plan_of(conn, sql: str) -> str:
    return " | ".join(
        r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, bind_placeholders(sql))
    )


class TestDataSchemaContract:
    def test_the_module_defines_sql_and_all_of_it_is_constant_text(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        every_literal = [
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and SQL_START.match(n.value)
        ]
        assert len(module_sql_literals()) == 11
        assert sorted(every_literal) == sorted(module_sql_literals().values())
        interpolated = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.JoinedStr)
            and SQL_START.match("".join(v.value for v in n.values if isinstance(v, ast.Constant)))
        ]
        assert interpolated == []

    def test_every_statement_passes_explain_against_the_migrated_schema(self, migrated_conn):  # noqa: F811
        for name, sql in module_sql_literals().items():
            try:
                migrated_conn.execute("EXPLAIN " + sql, bind_placeholders(sql)).fetchall()
            except sqlite3.Error as exc:
                pytest.fail(f"{name} fails EXPLAIN: {exc}")

    def test_active_zero_uses_ix_zeros_rifle_load(self, migrated_conn):  # noqa: F811
        plan = plan_of(migrated_conn, repo_zeroing._ACTIVE_ZERO)
        assert "USING INDEX ix_zeros_rifle_load" in plan
        assert "rifle_id=?" in plan
        assert "load_id=?" in plan
        assert "SCAN" not in plan

    def test_listing_zeros_for_a_rifle_uses_ix_zeros_rifle_load_and_primary_key_joins(
        self,
        migrated_conn,  # noqa: F811
    ):
        plan = plan_of(migrated_conn, repo_zeroing._LIST_ZEROS_FOR_RIFLE)
        assert "SEARCH zeros USING INDEX ix_zeros_rifle_load (rifle_id=?)" in plan
        assert "SEARCH loads USING INTEGER PRIMARY KEY" in plan
        assert "SEARCH load_lots USING INTEGER PRIMARY KEY" in plan
        assert "LEFT-JOIN" in plan
        assert not re.search(r"\bSCAN (zeros|loads|load_lots)\b", plan)

    def test_the_listing_selects_exactly_the_zero_columns_plus_the_two_labels(self, migrated_conn):  # noqa: F811
        sql = repo_zeroing._LIST_ZEROS_FOR_RIFLE
        cursor = migrated_conn.execute(sql, bind_placeholders(sql))
        zero_columns = [r["name"] for r in migrated_conn.execute("PRAGMA table_info(zeros)")]
        assert [d[0] for d in cursor.description] == [*zero_columns, "load_label", "lot_code"]
        assert set(ZeroRow.__dataclass_fields__) == set(zero_columns)
        assert list(ZeroListing.__dataclass_fields__) == ["zero", "load_label", "lot_code"]

    def test_supersede_uses_ix_zeros_rifle_load(self, migrated_conn):  # noqa: F811
        plan = plan_of(migrated_conn, repo_zeroing._SUPERSEDE_ZEROS)
        assert "ix_zeros_rifle_load" in plan
        assert "SCAN" not in plan

    @pytest.mark.parametrize("name", ["_SELECT_ZERO", "_UPDATE_ZERO", "_GROUP_EXISTS"])
    def test_by_id_statements_use_the_primary_key(self, migrated_conn, name):  # noqa: F811
        plan = plan_of(migrated_conn, getattr(repo_zeroing, name))
        assert "USING INTEGER PRIMARY KEY" in plan
        assert "SCAN" not in plan

    def test_linked_group_listing_uses_ix_groups_zero(self, migrated_conn):  # noqa: F811
        plan = plan_of(migrated_conn, repo_zeroing._LIST_LINKED_GROUPS)
        assert "USING INDEX ix_groups_zero" in plan
        assert "zero_id=?" in plan
        assert "SCAN groups" not in plan

    def test_zero_check_insert_finds_the_group_by_primary_key(self, migrated_conn):  # noqa: F811
        plan = plan_of(migrated_conn, repo_zeroing._INSERT_ZERO_CHECK)
        assert "SEARCH groups USING INTEGER PRIMARY KEY" in plan

    def test_link_update_finds_the_group_by_primary_key(self, migrated_conn):  # noqa: F811
        plan = plan_of(migrated_conn, repo_zeroing._LINK_GROUP)
        assert "SEARCH groups USING INTEGER PRIMARY KEY" in plan

    def test_zero_check_listing_uses_an_index_on_zero_id_or_scans_a_tiny_table_knowingly(
        self,
        migrated_conn,  # noqa: F811
    ):
        indexes = {
            i["name"]: [c["name"] for c in migrated_conn.execute(f"PRAGMA index_info({i['name']})")]
            for i in migrated_conn.execute("PRAGMA index_list(zero_checks)")
        }
        plan = plan_of(migrated_conn, repo_zeroing._LIST_ZERO_CHECKS)
        if any(cols[:1] == ["zero_id"] for cols in indexes.values()):
            assert "USING INDEX" in plan
        else:
            assert "SCAN zero_checks" in plan

    def test_group_id_uniqueness_the_check_insert_relies_on_is_in_the_schema(self, migrated_conn):  # noqa: F811
        unique = [
            [c["name"] for c in migrated_conn.execute(f"PRAGMA index_info({i['name']})")]
            for i in migrated_conn.execute("PRAGMA index_list(zero_checks)")
            if i["unique"]
        ]
        assert ["group_id"] in unique

    @pytest.mark.parametrize(
        ("table", "insert", "row_cls"),
        [("zero_checks", "_INSERT_ZERO_CHECK", ZeroCheckRow), ("zeros", "_INSERT_ZERO", ZeroRow)],
    )
    def test_insert_columns_are_row_fields_and_the_rest_are_nullable(
        self,
        migrated_conn,  # noqa: F811
        table,
        insert,
        row_cls,
    ):
        sql = getattr(repo_zeroing, insert)
        written = [
            c.strip()
            for c in re.search(r"\(([^)]*)\)\s+(?:VALUES|SELECT)", sql).group(1).split(",")
        ]
        info = {r["name"]: r for r in migrated_conn.execute(f"PRAGMA table_info({table})")}
        assert sorted(info) == sorted(row_cls.__dataclass_fields__)
        assert set(written) <= set(info)
        for name, col in info.items():
            if name != "id" and name not in written:
                assert col["notnull"] == 0 or col["dflt_value"] is not None, name

    def test_update_sql_sets_the_updatable_columns_in_order_then_updated_at(self):
        assigned = re.findall(r"(\w+) = \?", repo_zeroing._UPDATE_ZERO.split(" WHERE ")[0])
        assert assigned == [*ZERO_UPDATABLE, "updated_at"]

    def test_updatable_fields_exclude_identity_and_ownership_columns(self):
        assert not {"id", "rifle_id", "load_id", "mode", "created_at", "updated_at"} & set(
            ZERO_UPDATABLE
        )

    def test_states_and_modes_used_here_match_the_ddl_checks(self, migrated_conn):  # noqa: F811
        ddl = migrated_conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'zeros'"
        ).fetchone()[0]
        assert set(re.findall(r"'(\w+)'", re.search(r"state TEXT[^)]*\)", ddl).group(0))) == set(
            STATES
        )
        assert set(re.findall(r"'(\w+)'", re.search(r"mode TEXT[^)]*\)", ddl).group(0))) == {
            "new_setup",
            "existing",
        }

    def test_domain_zero_states_equal_the_ddl_states(self):
        from rga.domain.types import ZeroState

        assert {s.value for s in ZeroState} == set(STATES)

    def test_rows_validate_against_the_api_schema_models(self, world):
        from rga.web.schemas import ZeroCheckOut, ZeroOut

        z = world.zero(state="step3_dot", mode="new_setup")
        z = update_zero(world.conn, z.id, dialed_up_mil=-0.2, dialed_right_mil=0.1, dot_size_in=1.0)
        gid = world.check_group(z)
        c = create_zero_check(world.conn, zero_id=z.id, group_id=gid, result="pass")
        out = ZeroOut.model_validate(z)
        assert (out.state.value, out.mode, out.dot_size_in) == ("step3_dot", "new_setup", 1.0)
        check = ZeroCheckOut.model_validate(c)
        assert (check.result, check.zero_id, check.group_id) == ("pass", z.id, gid)

    def test_linked_groups_validate_against_zero_group_out(self, world):
        from rga.web.schemas import ZeroGroupOut

        z = world.zero()
        gid = world.group(purpose="zero_confirm", shots=4)
        link_group(world.conn, z.id, gid)
        (linked,) = list_linked_groups(world.conn, z.id)
        out = ZeroGroupOut(
            group_id=linked.group_id,
            purpose=linked.purpose,
            status=linked.status,
            shots=linked.shot_count,
        )
        assert (out.purpose.value, out.shots) == ("zero_confirm", 4)
