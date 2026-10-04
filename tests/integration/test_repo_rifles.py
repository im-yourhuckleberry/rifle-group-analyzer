"""T2: rifles/loads/lots repository against the migrated schema (real SQLite, no mocks)."""

from __future__ import annotations

import ast
import inspect
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from rga.db import repo_rifles
from rga.db.repo_rifles import (
    LOAD_UPDATABLE,
    RIFLE_UPDATABLE,
    LoadNotInRifle,
    create_load,
    create_rifle,
    get_load,
    get_lot,
    get_or_create_lot,
    get_rifle,
    list_loads,
    list_lots,
    list_rifles,
    set_zero_load,
    update_load,
    update_rifle,
)
from rga.db.rows import LoadRow, LotRow, RifleRow
from tests.support.db import migrated_conn  # noqa: F401

MODULE_PATH = Path(inspect.getsourcefile(repo_rifles))
SQL_START = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b")


def make_rifle(conn, name="Tikka", **kw) -> RifleRow:
    return create_rifle(
        conn,
        name=name,
        cartridge=kw.pop("cartridge", "6.5 Creedmoor"),
        bullet_diameter_in=kw.pop("bullet_diameter_in", 0.264),
        **kw,
    )


def make_load(conn, rifle_id, label="Factory A", **kw) -> LoadRow:
    return create_load(conn, rifle_id, kind=kw.pop("kind", "factory"), label=label, **kw)


def count(conn, table) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


T0 = "2026-10-01T12:00:00.000000Z"
RETIRED_COLUMNS = {"loads": {"powder", "charge_gr"}}
T1 = "2026-10-01T12:00:01.000000Z"
T2 = "2026-10-01T12:00:02.000000Z"
TIMESTAMP_FORMAT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")


class TestRifles:
    def test_create_returns_the_stored_row(self, migrated_conn):  # noqa: F811
        r = make_rifle(migrated_conn, scope="Vortex 5-25", notes="elk rifle")
        assert isinstance(r, RifleRow)
        assert (r.name, r.cartridge, r.bullet_diameter_in) == ("Tikka", "6.5 Creedmoor", 0.264)
        assert (r.scope, r.notes, r.zero_load_id) == ("Vortex 5-25", "elk rifle", None)
        assert get_rifle(migrated_conn, r.id) == r

    def test_defaults_scope_none_and_notes_empty(self, migrated_conn):  # noqa: F811
        r = make_rifle(migrated_conn)
        assert (r.scope, r.notes) == (None, "")

    def test_public_id_is_a_unique_uuid4_and_timestamps_are_iso_utc(self, migrated_conn):  # noqa: F811
        a, b = make_rifle(migrated_conn, "A"), make_rifle(migrated_conn, "B")
        assert uuid.UUID(a.public_id).version == 4
        assert a.public_id != b.public_id
        for stamp in (a.created_at, a.updated_at):
            assert TIMESTAMP_FORMAT.match(stamp)
            assert datetime.fromisoformat(stamp).utcoffset().total_seconds() == 0
        assert a.created_at == a.updated_at

    def test_public_id_and_now_can_be_supplied(self, migrated_conn):  # noqa: F811
        r = make_rifle(migrated_conn, public_id="fixed-id", now=T0)
        assert (r.public_id, r.created_at, r.updated_at) == ("fixed-id", T0, T0)

    def test_get_missing_is_none(self, migrated_conn):  # noqa: F811
        assert get_rifle(migrated_conn, 999) is None

    def test_list_is_ordered_by_name_ignoring_case_then_id(self, migrated_conn):  # noqa: F811
        ids = {n: make_rifle(migrated_conn, n).id for n in ["beta", "Alpha", "Beta", "alpha"]}
        names = [(r.name, r.id) for r in list_rifles(migrated_conn)]
        assert names == [
            ("Alpha", ids["Alpha"]),
            ("alpha", ids["alpha"]),
            ("beta", ids["beta"]),
            ("Beta", ids["Beta"]),
        ]

    def test_list_empty(self, migrated_conn):  # noqa: F811
        assert list_rifles(migrated_conn) == []

    def test_update_changes_only_given_fields(self, migrated_conn):  # noqa: F811
        r = make_rifle(migrated_conn, scope="S1", notes="n1")
        u = update_rifle(migrated_conn, r.id, name="Bergara", notes="n2")
        assert (u.name, u.notes) == ("Bergara", "n2")
        assert (u.cartridge, u.bullet_diameter_in, u.scope) == (r.cartridge, 0.264, "S1")
        assert (u.id, u.public_id, u.created_at) == (r.id, r.public_id, r.created_at)
        assert get_rifle(migrated_conn, r.id) == u

    def test_update_can_clear_scope_with_none(self, migrated_conn):  # noqa: F811
        r = make_rifle(migrated_conn, scope="S1")
        assert update_rifle(migrated_conn, r.id, scope=None).scope is None

    def test_update_can_change_cartridge_and_diameter(self, migrated_conn):  # noqa: F811
        r = make_rifle(migrated_conn)
        u = update_rifle(migrated_conn, r.id, cartridge=".308 Win", bullet_diameter_in=0.308)
        assert (u.cartridge, u.bullet_diameter_in) == (".308 Win", 0.308)

    def test_update_bumps_updated_at_and_keeps_created_at(self, migrated_conn):  # noqa: F811
        r = make_rifle(migrated_conn, now=T0)
        u = update_rifle(migrated_conn, r.id, name="New", now=T1)
        assert (u.created_at, u.updated_at) == (T0, T1)

    def test_update_stamps_the_current_time_by_default(self, migrated_conn):  # noqa: F811
        r = make_rifle(migrated_conn, now="2020-01-01T00:00:00.000000Z")
        u = update_rifle(migrated_conn, r.id, name="New")
        assert TIMESTAMP_FORMAT.match(u.updated_at)
        assert u.updated_at > r.updated_at

    def test_update_without_fields_returns_the_rifle_unchanged(self, migrated_conn):  # noqa: F811
        r = make_rifle(migrated_conn, now=T0)
        assert update_rifle(migrated_conn, r.id, now=T1) == r
        assert update_rifle(migrated_conn, 999) is None

    def test_update_missing_rifle_is_none(self, migrated_conn):  # noqa: F811
        assert update_rifle(migrated_conn, 999, name="x") is None

    @pytest.mark.parametrize(
        "changes", [{"zero_load_id": 1}, {"id": 5}, {"public_id": "x"}, {"created_at": "x"}]
    )
    def test_update_rejects_non_updatable_fields(self, migrated_conn, changes):  # noqa: F811
        r = make_rifle(migrated_conn)
        with pytest.raises(ValueError, match="cannot update"):
            update_rifle(migrated_conn, r.id, **changes)
        with pytest.raises(ValueError, match="cannot update"):
            update_rifle(migrated_conn, 999, **changes)
        assert get_rifle(migrated_conn, r.id) == r

    @pytest.mark.parametrize("diameter", [0.1, 0.6, 0.0, -0.264])
    def test_create_enforces_the_diameter_range(self, migrated_conn, diameter):  # noqa: F811
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            make_rifle(migrated_conn, bullet_diameter_in=diameter)
        assert count(migrated_conn, "rifles") == 0

    def test_update_to_an_invalid_diameter_changes_nothing(self, migrated_conn):  # noqa: F811
        r = make_rifle(migrated_conn)
        with pytest.raises(sqlite3.IntegrityError):
            update_rifle(migrated_conn, r.id, bullet_diameter_in=0.7)
        assert get_rifle(migrated_conn, r.id) == r

    def test_name_cannot_be_set_to_null(self, migrated_conn):  # noqa: F811
        r = make_rifle(migrated_conn)
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            update_rifle(migrated_conn, r.id, name=None)

    def test_hostile_text_is_stored_literally(self, migrated_conn):  # noqa: F811
        nasty = "x'); DROP TABLE rifles;--"
        r = make_rifle(migrated_conn, nasty, notes="100% _wild_")
        assert get_rifle(migrated_conn, r.id).name == nasty
        assert get_rifle(migrated_conn, r.id).notes == "100% _wild_"
        assert count(migrated_conn, "rifles") == 1


class TestLoads:
    def test_factory_load_round_trip(self, migrated_conn):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        load = make_load(
            migrated_conn,
            rifle.id,
            "Hornady 143 ELD-X",
            maker="Hornady",
            product="Precision Hunter",
            bullet_weight_gr=143.0,
            bullet_type="ELD-X",
            notes="lot varies",
        )
        assert isinstance(load, LoadRow)
        assert (load.rifle_id, load.kind, load.label) == (rifle.id, "factory", "Hornady 143 ELD-X")
        assert (load.maker, load.product, load.bullet_weight_gr) == (
            "Hornady",
            "Precision Hunter",
            143.0,
        )
        assert (load.bullet_type, load.notes) == ("ELD-X", "lot varies")
        assert (load.bullet, load.primer, load.brass, load.coal_in) == (None,) * 4
        assert load.trued_mv_fps is None
        assert get_load(migrated_conn, load.id) == load

    def test_handload_round_trip_with_trued_mv(self, migrated_conn):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        load = make_load(
            migrated_conn,
            rifle.id,
            "Handload A",
            kind="handload",
            bullet_weight_gr=143.0,
            bullet="ELD-X",
            primer="CCI BR-2",
            brass="Lapua",
            coal_in=2.8,
            trued_mv_fps=2712.0,
        )
        assert (load.kind, load.bullet_weight_gr, load.bullet) == ("handload", 143.0, "ELD-X")
        assert (load.primer, load.brass, load.coal_in) == ("CCI BR-2", "Lapua", 2.8)
        assert load.trued_mv_fps == 2712.0
        assert (load.maker, load.product, load.bullet_type) == (None,) * 3
        assert get_load(migrated_conn, load.id) == load

    def test_public_id_and_timestamps(self, migrated_conn):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        a, b = make_load(migrated_conn, rifle.id, "A"), make_load(migrated_conn, rifle.id, "B")
        assert uuid.UUID(a.public_id).version == 4
        assert a.public_id != b.public_id
        assert a.created_at == a.updated_at
        assert TIMESTAMP_FORMAT.match(a.created_at)

    def test_public_id_and_now_can_be_supplied(self, migrated_conn):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        load = make_load(migrated_conn, rifle.id, public_id="load-fixed", now=T0)
        assert (load.public_id, load.created_at, load.updated_at) == ("load-fixed", T0, T0)

    def test_coal_is_a_field_cbto_is_not_and_powder_and_charge_are_retired(self, migrated_conn):  # noqa: F811
        columns = {r["name"] for r in migrated_conn.execute("PRAGMA table_info(loads)")}
        assert not {c for c in columns if "cbto" in c.lower() or "seating" in c.lower()}
        params = set(inspect.signature(create_load).parameters)
        assert {"coal_in", "brass", "bullet_weight_gr", "bullet", "primer"} <= params
        assert not {p for p in params | set(LOAD_UPDATABLE) if "cbto" in p}
        assert not ({"powder", "charge_gr"} & (params | set(LOAD_UPDATABLE)))
        assert {"powder", "charge_gr"} <= columns, "retired columns stay in the table, unused"
        assert not ({"powder", "charge_gr"} & set(LoadRow.__dataclass_fields__))

    @pytest.mark.parametrize("coal", [0.999, 5.001, 0.0, -1.0])
    def test_coal_outside_one_to_five_inches_is_refused_by_the_table(self, migrated_conn, coal):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            make_load(migrated_conn, rifle.id, kind="handload", coal_in=coal)

    @pytest.mark.parametrize("coal", [1.0, 5.0])
    def test_coal_at_the_bounds_is_stored(self, migrated_conn, coal):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        assert make_load(migrated_conn, rifle.id, kind="handload", coal_in=coal).coal_in == coal

    def test_get_missing_is_none(self, migrated_conn):  # noqa: F811
        assert get_load(migrated_conn, 999) is None

    def test_list_returns_only_that_rifles_loads_in_creation_order(self, migrated_conn):  # noqa: F811
        r1, r2 = make_rifle(migrated_conn, "R1"), make_rifle(migrated_conn, "R2")
        b = make_load(migrated_conn, r1.id, "B")
        other = make_load(migrated_conn, r2.id, "other")
        a = make_load(migrated_conn, r1.id, "A")
        assert list_loads(migrated_conn, r1.id) == [b, a]
        assert list_loads(migrated_conn, r2.id) == [other]
        assert list_loads(migrated_conn, 999) == []

    @pytest.mark.parametrize("kind", ["other", "", "Factory"])
    def test_kind_must_be_factory_or_handload(self, migrated_conn, kind):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            make_load(migrated_conn, rifle.id, kind=kind)
        assert count(migrated_conn, "loads") == 0

    @pytest.mark.parametrize("mv", [0, -5.0])
    def test_trued_mv_must_be_positive(self, migrated_conn, mv):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            make_load(migrated_conn, rifle.id, trued_mv_fps=mv)

    def test_load_needs_an_existing_rifle(self, migrated_conn):  # noqa: F811
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            make_load(migrated_conn, 999)
        assert count(migrated_conn, "loads") == 0

    def test_update_changes_only_given_fields(self, migrated_conn):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        load = make_load(migrated_conn, rifle.id, maker="Hornady", bullet_weight_gr=143.0)
        u = update_load(migrated_conn, load.id, label="Renamed", trued_mv_fps=2700.0)
        assert (u.label, u.trued_mv_fps) == ("Renamed", 2700.0)
        assert (u.maker, u.bullet_weight_gr) == ("Hornady", 143.0)
        assert (u.id, u.public_id, u.rifle_id, u.kind, u.created_at) == (
            load.id,
            load.public_id,
            load.rifle_id,
            load.kind,
            load.created_at,
        )
        assert get_load(migrated_conn, load.id) == u

    def test_update_can_clear_optional_fields(self, migrated_conn):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        load = make_load(migrated_conn, rifle.id, maker="Hornady", trued_mv_fps=2700.0)
        u = update_load(migrated_conn, load.id, maker=None, trued_mv_fps=None)
        assert (u.maker, u.trued_mv_fps) == (None, None)

    def test_update_every_updatable_field(self, migrated_conn):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        load = make_load(migrated_conn, rifle.id, kind="handload")
        changes = {
            n: (
                f"v-{n}"
                if n
                in {
                    "label",
                    "maker",
                    "product",
                    "bullet_type",
                    "bullet",
                    "primer",
                    "brass",
                    "notes",
                }
                else 1.5
            )
            for n in LOAD_UPDATABLE
        }
        u = update_load(migrated_conn, load.id, **changes)
        for name, value in changes.items():
            assert getattr(u, name) == value

    def test_update_bumps_updated_at(self, migrated_conn):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        load = make_load(migrated_conn, rifle.id, now=T0)
        u = update_load(migrated_conn, load.id, notes="n", now=T2)
        assert (u.created_at, u.updated_at) == (T0, T2)

    def test_update_without_fields_returns_the_load_unchanged(self, migrated_conn):  # noqa: F811
        load = make_load(migrated_conn, make_rifle(migrated_conn).id, now=T0)
        assert update_load(migrated_conn, load.id, now=T1) == load
        assert update_load(migrated_conn, 999) is None

    @pytest.mark.parametrize("changes", [{"kind": "handload"}, {"rifle_id": 2}, {"id": 9}])
    def test_update_rejects_immutable_fields(self, migrated_conn, changes):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        load = make_load(migrated_conn, rifle.id)
        with pytest.raises(ValueError, match="cannot update"):
            update_load(migrated_conn, load.id, **changes)
        assert get_load(migrated_conn, load.id) == load

    def test_update_missing_load_is_none(self, migrated_conn):  # noqa: F811
        assert update_load(migrated_conn, 999, label="x") is None

    def test_update_to_invalid_mv_changes_nothing(self, migrated_conn):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        load = make_load(migrated_conn, rifle.id)
        with pytest.raises(sqlite3.IntegrityError):
            update_load(migrated_conn, load.id, trued_mv_fps=0)
        assert get_load(migrated_conn, load.id) == load


class TestZeroLoad:
    def test_set_and_clear(self, migrated_conn):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        load = make_load(migrated_conn, rifle.id)
        assert set_zero_load(migrated_conn, rifle.id, load.id).zero_load_id == load.id
        assert get_rifle(migrated_conn, rifle.id).zero_load_id == load.id
        assert set_zero_load(migrated_conn, rifle.id, None).zero_load_id is None
        assert get_rifle(migrated_conn, rifle.id).zero_load_id is None

    def test_can_switch_to_another_load_of_the_same_rifle(self, migrated_conn):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        a, b = make_load(migrated_conn, rifle.id, "A"), make_load(migrated_conn, rifle.id, "B")
        set_zero_load(migrated_conn, rifle.id, a.id)
        assert set_zero_load(migrated_conn, rifle.id, b.id).zero_load_id == b.id

    def test_rejects_a_load_from_another_rifle_and_changes_nothing(self, migrated_conn):  # noqa: F811
        r1, r2 = make_rifle(migrated_conn, "R1"), make_rifle(migrated_conn, "R2")
        mine, theirs = make_load(migrated_conn, r1.id), make_load(migrated_conn, r2.id)
        set_zero_load(migrated_conn, r1.id, mine.id)
        before = get_rifle(migrated_conn, r1.id)
        with pytest.raises(LoadNotInRifle):
            set_zero_load(migrated_conn, r1.id, theirs.id)
        assert get_rifle(migrated_conn, r1.id) == before
        assert get_rifle(migrated_conn, r2.id).zero_load_id is None

    def test_rejects_a_missing_load(self, migrated_conn):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        with pytest.raises(LoadNotInRifle):
            set_zero_load(migrated_conn, rifle.id, 999)
        assert get_rifle(migrated_conn, rifle.id).zero_load_id is None

    def test_missing_rifle_is_none_even_with_a_real_load(self, migrated_conn):  # noqa: F811
        load = make_load(migrated_conn, make_rifle(migrated_conn).id)
        assert set_zero_load(migrated_conn, 999, load.id) is None
        assert set_zero_load(migrated_conn, 999, None) is None

    def test_bumps_updated_at(self, migrated_conn):  # noqa: F811
        rifle = make_rifle(migrated_conn, now=T0)
        load = make_load(migrated_conn, rifle.id)
        updated = set_zero_load(migrated_conn, rifle.id, load.id, now=T2)
        assert (updated.created_at, updated.updated_at) == (T0, T2)

    def test_deleting_the_zero_load_clears_the_reference(self, migrated_conn):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        load = make_load(migrated_conn, rifle.id)
        set_zero_load(migrated_conn, rifle.id, load.id)
        migrated_conn.execute("DELETE FROM loads WHERE id = ?", (load.id,))
        assert get_rifle(migrated_conn, rifle.id).zero_load_id is None


class TestLots:
    def test_create_returns_the_stored_row(self, migrated_conn):  # noqa: F811
        load = make_load(migrated_conn, make_rifle(migrated_conn).id)
        lot = get_or_create_lot(migrated_conn, load.id, "LOT-123")
        assert isinstance(lot, LotRow)
        assert (lot.load_id, lot.lot_code) == (load.id, "LOT-123")
        assert TIMESTAMP_FORMAT.match(lot.created_at)
        assert get_lot(migrated_conn, lot.id) == lot

    def test_is_idempotent_and_keeps_the_original_row(self, migrated_conn):  # noqa: F811
        load = make_load(migrated_conn, make_rifle(migrated_conn).id)
        first = get_or_create_lot(migrated_conn, load.id, "A1", now=T0)
        again = get_or_create_lot(migrated_conn, load.id, "A1", now=T2)
        assert first.created_at == T0
        assert again == first
        assert count(migrated_conn, "load_lots") == 1

    def test_same_code_under_different_loads_gives_different_lots(self, migrated_conn):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        l1, l2 = make_load(migrated_conn, rifle.id, "1"), make_load(migrated_conn, rifle.id, "2")
        a, b = (
            get_or_create_lot(migrated_conn, l1.id, "X"),
            get_or_create_lot(migrated_conn, l2.id, "X"),
        )
        assert a.id != b.id
        assert (a.load_id, b.load_id) == (l1.id, l2.id)

    def test_codes_are_case_and_whitespace_exact(self, migrated_conn):  # noqa: F811
        load = make_load(migrated_conn, make_rifle(migrated_conn).id)
        ids = {get_or_create_lot(migrated_conn, load.id, c).id for c in ["a", "A", "a "]}
        assert len(ids) == 3

    @pytest.mark.parametrize("code", ["", "   ", "\t\n"])
    def test_blank_code_is_rejected(self, migrated_conn, code):  # noqa: F811
        load = make_load(migrated_conn, make_rifle(migrated_conn).id)
        with pytest.raises(ValueError, match="blank"):
            get_or_create_lot(migrated_conn, load.id, code)
        assert count(migrated_conn, "load_lots") == 0

    def test_unknown_load_is_a_foreign_key_error_and_creates_nothing(self, migrated_conn):  # noqa: F811
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            get_or_create_lot(migrated_conn, 999, "A1")
        assert count(migrated_conn, "load_lots") == 0

    def test_hostile_codes_are_stored_literally(self, migrated_conn):  # noqa: F811
        load = make_load(migrated_conn, make_rifle(migrated_conn).id)
        for code in ["a'b", "100%_", "x'); DROP TABLE load_lots;--"]:
            assert get_or_create_lot(migrated_conn, load.id, code).lot_code == code
        assert count(migrated_conn, "load_lots") == 3

    def test_get_missing_is_none(self, migrated_conn):  # noqa: F811
        assert get_lot(migrated_conn, 999) is None

    def test_list_is_per_load_in_creation_order(self, migrated_conn):  # noqa: F811
        rifle = make_rifle(migrated_conn)
        l1, l2 = make_load(migrated_conn, rifle.id, "1"), make_load(migrated_conn, rifle.id, "2")
        z = get_or_create_lot(migrated_conn, l1.id, "Z")
        other = get_or_create_lot(migrated_conn, l2.id, "M")
        a = get_or_create_lot(migrated_conn, l1.id, "A")
        assert list_lots(migrated_conn, l1.id) == [z, a]
        assert list_lots(migrated_conn, l2.id) == [other]
        assert list_lots(migrated_conn, 999) == []


class TestTransactionOwnership:
    def test_repo_functions_leave_the_transaction_open_and_rollback_undoes_them(
        self,
        migrated_conn,  # noqa: F811
    ):
        rifle = make_rifle(migrated_conn)
        load = make_load(migrated_conn, rifle.id)
        get_or_create_lot(migrated_conn, load.id, "L1")
        set_zero_load(migrated_conn, rifle.id, load.id)
        update_rifle(migrated_conn, rifle.id, name="x")
        update_load(migrated_conn, load.id, label="y")
        assert migrated_conn.in_transaction
        migrated_conn.rollback()
        assert (count(migrated_conn, "rifles"), count(migrated_conn, "loads")) == (0, 0)
        assert count(migrated_conn, "load_lots") == 0

    def test_committed_work_survives_on_a_second_connection(self, migrated_conn, tmp_path):  # noqa: F811
        from rga.db.connection import connect

        rifle = make_rifle(migrated_conn)
        migrated_conn.commit()
        other = connect(tmp_path / "rga.sqlite3")
        try:
            assert get_rifle(other, rifle.id) == rifle
        finally:
            other.close()

    def test_uncommitted_work_is_invisible_to_a_second_connection(self, migrated_conn, tmp_path):  # noqa: F811
        from rga.db.connection import connect

        rifle = make_rifle(migrated_conn)
        other = connect(tmp_path / "rga.sqlite3")
        try:
            assert get_rifle(other, rifle.id) is None
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
    """SQL string constants assigned at module level, found from the source (not by name)."""
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


class TestDataSchemaContract:
    def test_the_module_defines_sql_and_all_of_it_is_constant_text(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        every_literal = [
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and SQL_START.match(n.value)
        ]
        assert len(module_sql_literals()) == 13
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

    def test_user_values_are_only_ever_bound_never_interpolated(self):
        for name, sql in module_sql_literals().items():
            assert not re.search(r"['\"]|%s|\{", sql.replace("''", "")), name

    def test_list_loads_uses_ix_loads_rifle_without_a_sort(self, migrated_conn):  # noqa: F811
        plan = " | ".join(
            r["detail"]
            for r in migrated_conn.execute("EXPLAIN QUERY PLAN " + repo_rifles._SELECT_LOADS, (1,))
        )
        assert "USING INDEX ix_loads_rifle" in plan
        assert "SCAN" not in plan
        assert "TEMP B-TREE" not in plan

    @pytest.mark.parametrize(
        "name", ["_SELECT_RIFLE", "_SELECT_LOAD", "_SELECT_LOT", "_UPDATE_RIFLE", "_UPDATE_LOAD"]
    )
    def test_by_id_statements_use_the_primary_key(self, migrated_conn, name):  # noqa: F811
        sql = getattr(repo_rifles, name)
        plan = " | ".join(
            r["detail"]
            for r in migrated_conn.execute("EXPLAIN QUERY PLAN " + sql, bind_placeholders(sql))
        )
        assert "USING INTEGER PRIMARY KEY" in plan
        assert "SCAN" not in plan

    def test_lot_lookups_use_the_load_id_lot_code_unique_index(self, migrated_conn):  # noqa: F811
        for name in ("_SELECT_LOT_BY_CODE", "_SELECT_LOTS"):
            sql = getattr(repo_rifles, name)
            plan = " | ".join(
                r["detail"]
                for r in migrated_conn.execute("EXPLAIN QUERY PLAN " + sql, bind_placeholders(sql))
            )
            assert "sqlite_autoindex_load_lots_1" in plan, name
            assert "load_id=?" in plan, name

    def test_only_list_rifles_scans_a_table(self, migrated_conn):  # noqa: F811
        scans = {}
        for name, sql in module_sql_literals().items():
            if sql.startswith("INSERT"):
                continue
            plan = " | ".join(
                r["detail"]
                for r in migrated_conn.execute("EXPLAIN QUERY PLAN " + sql, bind_placeholders(sql))
            )
            if re.search(r"\bSCAN (?!CONSTANT)", plan):
                scans[name] = plan
        assert set(scans) == {"_SELECT_RIFLES"}

    def test_the_lot_unique_index_the_upsert_targets_exists(self, migrated_conn):  # noqa: F811
        indexes = migrated_conn.execute("PRAGMA index_list(load_lots)").fetchall()
        unique_cols = [
            [c["name"] for c in migrated_conn.execute(f"PRAGMA index_info({i['name']})")]
            for i in indexes
            if i["unique"]
        ]
        assert ["load_id", "lot_code"] in unique_cols

    @pytest.mark.parametrize(
        ("table", "insert", "row_cls"),
        [
            ("rifles", "_INSERT_RIFLE", RifleRow),
            ("loads", "_INSERT_LOAD", LoadRow),
            ("load_lots", "_INSERT_LOT", LotRow),
        ],
    )
    def test_each_insert_writes_every_column_except_the_primary_key(
        self,
        migrated_conn,  # noqa: F811
        table,
        insert,
        row_cls,
    ):
        sql = getattr(repo_rifles, insert)
        written = [c.strip() for c in re.search(r"\(([^)]*)\)\s+VALUES", sql).group(1).split(",")]
        retired = RETIRED_COLUMNS.get(table, set())
        table_cols = [
            r["name"]
            for r in migrated_conn.execute(f"PRAGMA table_info({table})")
            if r["name"] not in retired
        ]
        assert sorted(written) == sorted(c for c in table_cols if c != "id")
        assert sorted(table_cols) == sorted(row_cls.__dataclass_fields__)

    @pytest.mark.parametrize(
        ("update", "updatable"),
        [("_UPDATE_RIFLE", RIFLE_UPDATABLE), ("_UPDATE_LOAD", LOAD_UPDATABLE)],
    )
    def test_update_sql_sets_the_updatable_columns_in_order_then_updated_at(
        self, update, updatable
    ):
        sql = getattr(repo_rifles, update)
        assigned = re.findall(r"(\w+) = \?", sql.split(" WHERE ")[0])
        assert assigned == [*updatable, "updated_at"]

    def test_updatable_fields_exclude_identity_and_ownership_columns(self):
        immutable = {
            "id",
            "public_id",
            "created_at",
            "updated_at",
            "rifle_id",
            "kind",
            "zero_load_id",
        }
        assert not immutable & (set(RIFLE_UPDATABLE) | set(LOAD_UPDATABLE))

    def test_api_schema_models_accept_the_row_dataclasses(self, migrated_conn):  # noqa: F811
        from rga.web.schemas import LoadOut, LotOut, RifleOut

        rifle = make_rifle(migrated_conn)
        load = make_load(migrated_conn, rifle.id, kind="handload", brass="Lapua")
        lot = get_or_create_lot(migrated_conn, load.id, "L1")
        assert RifleOut.model_validate(rifle).id == rifle.id
        assert LoadOut.model_validate(load).kind.value == "handload"
        assert LotOut.model_validate(lot).lot_code == "L1"

    def test_connection_enforces_foreign_keys(self, migrated_conn):  # noqa: F811
        assert migrated_conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
