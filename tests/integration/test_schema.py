"""T2: migrated SQLite schema, constraints, connection settings and migration runner."""

from __future__ import annotations

import itertools
import sqlite3
import tomllib
from dataclasses import fields
from pathlib import Path

import pytest

from rga.db import connection as db_connection
from rga.db import repo_aggregates, repo_settings, rows
from rga.db.connection import MigrationError, connect, latest_version, migrate, schema_version
from tests.support.db import migrated_conn  # noqa: F401

NOW = "2026-09-30T12:00:00+00:00"
LATEST = latest_version()
NEXT = LATEST + 1
SAVED_AGGREGATE_TABLES = {"aggregates", "aggregate_groups"}
SETTINGS_TABLES = {"settings"}
RETIRED_COLUMNS = {"loads": {"powder", "charge_gr"}}

TABLE_ROWS = {
    "rifles": rows.RifleRow,
    "loads": rows.LoadRow,
    "load_lots": rows.LotRow,
    "sessions": rows.SessionRow,
    "photos": rows.PhotoRow,
    "groups": rows.GroupRow,
    "shots": rows.ShotRow,
    "detection_rejections": rows.RejectionRow,
    "zeros": rows.ZeroRow,
    "zero_checks": rows.ZeroCheckRow,
    "suppressors": rows.SuppressorRow,
}

EXPECTED_INDEXES = {
    "ix_loads_rifle": ("loads", ["rifle_id"]),
    "ix_sessions_rifle_date": ("sessions", ["rifle_id", "date"]),
    "ix_zeros_rifle_load": ("zeros", ["rifle_id", "load_id", "state"]),
    "ix_groups_load_status": ("groups", ["load_id", "status"]),
    "ix_groups_lot": ("groups", ["lot_id", "finalized_at"]),
    "ix_groups_zero": ("groups", ["zero_id"]),
    "ix_groups_load_day": ("groups", ["load_id", "shot_date", "distance_yd"]),
    "ix_groups_suppressor": ("groups", ["suppressor_id"]),
}


def insert_rifle(conn: sqlite3.Connection, public_id: str = "r-1", diameter: float = 0.264) -> int:
    cur = conn.execute(
        "INSERT INTO rifles (public_id, name, cartridge, bullet_diameter_in, created_at, updated_at)"
        " VALUES (?, 'Test', '6.5 Creedmoor', ?, ?, ?)",
        (public_id, diameter, NOW, NOW),
    )
    return int(cur.lastrowid)


def insert_load(conn: sqlite3.Connection, rifle_id: int, public_id: str = "l-1") -> int:
    cur = conn.execute(
        "INSERT INTO loads (public_id, rifle_id, kind, label, created_at, updated_at)"
        " VALUES (?, ?, 'factory', 'Factory A', ?, ?)",
        (public_id, rifle_id, NOW, NOW),
    )
    return int(cur.lastrowid)


def insert_lot(conn: sqlite3.Connection, load_id: int, code: str = "LOT1") -> int:
    cur = conn.execute(
        "INSERT INTO load_lots (load_id, lot_code, created_at) VALUES (?, ?, ?)",
        (load_id, code, NOW),
    )
    return int(cur.lastrowid)


def insert_session(conn: sqlite3.Connection, rifle_id: int, public_id: str = "s-1") -> int:
    cur = conn.execute(
        "INSERT INTO sessions (public_id, rifle_id, date, distance_yd, position, suppressor,"
        " created_at, updated_at) VALUES (?, ?, '2026-09-30', 100, 'bench', 0, ?, ?)",
        (public_id, rifle_id, NOW, NOW),
    )
    return int(cur.lastrowid)


GROUP_CONTEXT: dict[str, object] = {
    "shot_date": "2026-09-30",
    "shot_at_source": "manual",
    "distance_yd": 100,
    "position": "bench",
    "suppressor": 0,
}


def insert_group(
    conn: sqlite3.Connection,
    load_id: int,
    lot_id: int,
    *,
    public_id: str = "g-1",
    status: str = "draft",
    aim: tuple[float, float] | None = None,
    **context: object,
) -> int:
    aim_x, aim_y = aim if aim else (None, None)
    values = {**GROUP_CONTEXT, **context}
    cur = conn.execute(
        "INSERT INTO groups (public_id, load_id, lot_id, shots_expected, aim_x_in, aim_y_in,"
        f" status, created_at, updated_at, {', '.join(values)})"
        f" VALUES (?, ?, ?, 3, ?, ?, ?, ?, ?, {', '.join('?' for _ in values)})",
        (public_id, load_id, lot_id, aim_x, aim_y, status, NOW, NOW, *values.values()),
    )
    return int(cur.lastrowid)


def insert_photo(conn: sqlite3.Connection, **overrides: object) -> int:
    values: dict[str, object] = {
        "sha256": "a" * 64,
        "ext": "jpg",
        "width_px": 4000,
        "height_px": 3000,
        "created_at": NOW,
    }
    values.update(overrides)
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO photos ({columns}) VALUES ({marks})", tuple(values.values()))
    return int(cur.lastrowid)


class Seed:
    """Inserts valid rows into any table, one override at a time, creating parents on demand.

    Every row gets fresh unique values (public ids, hashes, lot codes, seq numbers), so a test
    that overrides a single column can only fail on the constraint under test.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._counter = itertools.count(1)
        self.rifle = self.insert("rifles")
        self.load = self.insert("loads", rifle_id=self.rifle)
        self.lot = self.insert("load_lots", load_id=self.load)
        self.session = self.insert("sessions", rifle_id=self.rifle)

    def uid(self, prefix: str) -> str:
        return f"{prefix}-{next(self._counter)}"

    def values(self, table: str) -> dict[str, object]:
        """Valid column values for `table`; parent rows are inserted when a FK is needed."""
        timestamps = {"created_at": NOW, "updated_at": NOW}
        match table:
            case "rifles":
                return {
                    "public_id": self.uid("r"),
                    "name": "Test",
                    "cartridge": "6.5 Creedmoor",
                    "bullet_diameter_in": 0.264,
                    **timestamps,
                }
            case "loads":
                return {
                    "public_id": self.uid("l"),
                    "rifle_id": self.rifle,
                    "kind": "factory",
                    "label": "Factory A",
                    **timestamps,
                }
            case "load_lots":
                return {"load_id": self.load, "lot_code": self.uid("LOT"), "created_at": NOW}
            case "sessions":
                return {
                    "public_id": self.uid("s"),
                    "rifle_id": self.rifle,
                    "date": "2026-09-30",
                    "distance_yd": 100,
                    "position": "bench",
                    "suppressor": 0,
                    **timestamps,
                }
            case "suppressors":
                return {"name": self.uid("Suppressor"), "created_at": NOW}
            case "photos":
                return {
                    "sha256": self.uid("sha"),
                    "ext": "jpg",
                    "width_px": 4000,
                    "height_px": 3000,
                    "created_at": NOW,
                }
            case "zeros":
                return {
                    "rifle_id": self.rifle,
                    "load_id": self.load,
                    "mode": "new_setup",
                    "state": "step1",
                    **timestamps,
                }
            case "groups":
                return {
                    "public_id": self.uid("g"),
                    "load_id": self.load,
                    "lot_id": self.lot,
                    "shots_expected": 3,
                    **GROUP_CONTEXT,
                    **timestamps,
                }
            case "shots":
                return {
                    "group_id": self.insert("groups"),
                    "seq": next(self._counter),
                    "x_in": 1.0,
                    "y_in": 2.0,
                    "origin": "detected",
                }
            case "detection_rejections":
                return {
                    "group_id": self.insert("groups"),
                    "x_in": 1.0,
                    "y_in": 2.0,
                    "reason": "not_a_bullet_hole",
                    "created_at": NOW,
                }
            case "zero_checks":
                return {
                    "zero_id": self.insert("zeros"),
                    "group_id": self.insert("groups"),
                    "result": "pass",
                    "created_at": NOW,
                }
        raise AssertionError(f"no valid defaults for table {table}")

    def insert(self, table: str, **overrides: object) -> int:
        """Insert a valid row with `overrides` applied; return its id. Raises sqlite3 errors."""
        values = {**self.values(table), **overrides}
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = self.conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
        )
        return int(cur.lastrowid)


@pytest.fixture
def seed(migrated_conn: sqlite3.Connection) -> Seed:  # noqa: F811
    return Seed(migrated_conn)


@pytest.fixture
def chain(migrated_conn: sqlite3.Connection) -> dict[str, int]:  # noqa: F811
    """A rifle -> load -> lot -> session chain, committed."""
    rifle_id = insert_rifle(migrated_conn)
    load_id = insert_load(migrated_conn, rifle_id)
    lot_id = insert_lot(migrated_conn, load_id)
    session_id = insert_session(migrated_conn, rifle_id)
    migrated_conn.commit()
    return {"rifle": rifle_id, "load": load_id, "lot": lot_id, "session": session_id}


def test_all_tables_exist(migrated_conn: sqlite3.Connection) -> None:  # noqa: F811
    names = {
        r["name"]
        for r in migrated_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert names == set(TABLE_ROWS) | SAVED_AGGREGATE_TABLES | SETTINGS_TABLES


@pytest.mark.parametrize(("table", "row_cls"), sorted(TABLE_ROWS.items()))
def test_row_dataclass_mirrors_table_columns_in_order(
    migrated_conn: sqlite3.Connection,  # noqa: F811
    table: str,
    row_cls: type,
) -> None:
    retired = RETIRED_COLUMNS.get(table, set())
    columns = [
        r["name"]
        for r in migrated_conn.execute(f"PRAGMA table_info({table})")
        if r["name"] not in retired
    ]
    assert [f.name for f in fields(row_cls)] == columns


def test_expected_indexes_exist_on_expected_columns(
    migrated_conn: sqlite3.Connection,  # noqa: F811
) -> None:
    for index, (table, columns) in EXPECTED_INDEXES.items():
        listed = {r["name"]: r for r in migrated_conn.execute(f"PRAGMA index_list({table})")}
        assert index in listed, f"{index} missing on {table}"
        indexed = [r["name"] for r in migrated_conn.execute(f"PRAGMA index_info({index})")]
        assert indexed == columns


def test_connection_pragmas_and_row_factory(migrated_conn: sqlite3.Connection) -> None:  # noqa: F811
    assert migrated_conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert migrated_conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert migrated_conn.row_factory is sqlite3.Row
    assert schema_version(migrated_conn) == LATEST
    assert migrated_conn.execute("PRAGMA user_version").fetchone()[0] == LATEST


def test_connect_applies_pragmas_to_a_fresh_unmigrated_file(tmp_path: Path) -> None:
    conn = connect(tmp_path / "fresh.sqlite3")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert schema_version(conn) == 0
    finally:
        conn.close()


def test_migration_file_is_declared_package_data_and_discovered_at_runtime(
    project_root: Path,
) -> None:
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text())
    patterns = pyproject["tool"]["setuptools"]["package-data"]["rga"]
    assert "db/migrations/*.sql" in patterns
    names = [name for _, name, _ in db_connection._migration_files()]
    assert names[:2] == ["001_initial.sql", "002_aggregates.sql"]
    assert latest_version() == len(names)


def test_migrate_twice_is_a_noop(migrated_conn: sqlite3.Connection, chain: dict[str, int]) -> None:  # noqa: F811
    before = [tuple(r) for r in migrated_conn.execute("SELECT type, name, sql FROM sqlite_master")]
    assert migrate(migrated_conn) == LATEST
    assert migrate(migrated_conn) == LATEST
    after = [tuple(r) for r in migrated_conn.execute("SELECT type, name, sql FROM sqlite_master")]
    assert after == before
    assert migrated_conn.execute("SELECT count(*) FROM rifles").fetchone()[0] == 1
    assert schema_version(migrated_conn) == LATEST


def test_migrate_applies_only_files_newer_than_user_version(
    migrated_conn: sqlite3.Connection,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = db_connection._migration_files()
    sql = f"CREATE TABLE extra (id INTEGER); PRAGMA user_version = {NEXT};"
    files.append((NEXT, f"{NEXT:03d}_extra.sql", sql))
    monkeypatch.setattr(db_connection, "_migration_files", lambda: files)
    assert migrate(migrated_conn) == NEXT
    assert migrated_conn.execute("SELECT count(*) FROM extra").fetchone()[0] == 0


def test_failing_migration_rolls_back_completely(
    migrated_conn: sqlite3.Connection,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = "CREATE TABLE half_done (id INTEGER); INSERT INTO missing_table VALUES (1);"
    files = [*db_connection._migration_files(), (NEXT, f"{NEXT:03d}_broken.sql", broken)]
    monkeypatch.setattr(db_connection, "_migration_files", lambda: files)
    with pytest.raises(sqlite3.OperationalError):
        migrate(migrated_conn)
    assert schema_version(migrated_conn) == LATEST
    assert not migrated_conn.in_transaction
    tables = {r["name"] for r in migrated_conn.execute("SELECT name FROM sqlite_master")}
    assert "half_done" not in tables


BAD_MIGRATIONS = {
    "forgot-user-version": (
        "CREATE TABLE t (id INTEGER);",
        f"left user_version at {LATEST}, expected {NEXT}",
    ),
    "wrong-user-version": (
        f"CREATE TABLE t (id INTEGER); PRAGMA user_version = {NEXT + 1};",
        f"left user_version at {NEXT + 1}, expected {NEXT}",
    ),
    "unterminated-comment": (
        f"CREATE TABLE t (id INTEGER); PRAGMA user_version = {NEXT}; /* never closed",
        "not a complete SQL script",
    ),
    "unterminated-string": (
        f"CREATE TABLE t (id INTEGER); PRAGMA user_version = {NEXT}; SELECT 'never closed",
        "not a complete SQL script",
    ),
    "missing-final-semicolon": (
        f"CREATE TABLE t (id INTEGER); PRAGMA user_version = {NEXT}",
        "not a complete SQL script",
    ),
}


def _snapshot(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (r["name"], r["sql"])
        for r in conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name")
    ]


@pytest.mark.parametrize("case", sorted(BAD_MIGRATIONS))
def test_bad_migration_raises_and_leaves_the_database_unchanged(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sql, message = BAD_MIGRATIONS[case]
    path = tmp_path / "rga.sqlite3"
    conn = connect(path)
    migrate(conn)
    before = _snapshot(conn)
    files = [*db_connection._migration_files(), (NEXT, f"{NEXT:03d}_bad.sql", sql)]
    monkeypatch.setattr(db_connection, "_migration_files", lambda: files)

    with pytest.raises(MigrationError, match=message):
        migrate(conn)
    assert not conn.in_transaction
    assert schema_version(conn) == LATEST
    assert _snapshot(conn) == before
    with pytest.raises(MigrationError, match=message):
        migrate(conn)

    conn.close()
    reopened = connect(path)
    try:
        assert schema_version(reopened) == LATEST
        assert _snapshot(reopened) == before
    finally:
        reopened.close()


def test_migration_that_ends_its_own_transaction_is_an_error(
    migrated_conn: sqlite3.Connection,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = f"CREATE TABLE t (id INTEGER); PRAGMA user_version = {NEXT}; COMMIT;"
    files = [*db_connection._migration_files(), (NEXT, f"{NEXT:03d}_selfcommit.sql", sql)]
    monkeypatch.setattr(db_connection, "_migration_files", lambda: files)
    with pytest.raises(MigrationError, match="ended its own transaction"):
        migrate(migrated_conn)
    assert not migrated_conn.in_transaction


def test_good_migration_after_a_bad_one_still_applies(
    migrated_conn: sqlite3.Connection,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_files = db_connection._migration_files()
    bad = [
        *real_files,
        (NEXT, f"{NEXT:03d}_bad.sql", "CREATE TABLE t (id INTEGER);"),
    ]
    monkeypatch.setattr(db_connection, "_migration_files", lambda: bad)
    with pytest.raises(MigrationError):
        migrate(migrated_conn)
    good = [
        *real_files,
        (
            NEXT,
            f"{NEXT:03d}_good.sql",
            f"CREATE TABLE t (id INTEGER); PRAGMA user_version = {NEXT};",
        ),
    ]
    monkeypatch.setattr(db_connection, "_migration_files", lambda: good)
    assert migrate(migrated_conn) == NEXT


def test_database_newer_than_code_is_refused(migrated_conn: sqlite3.Connection) -> None:  # noqa: F811
    migrated_conn.execute("PRAGMA user_version = 99")
    with pytest.raises(MigrationError, match="newer than code"):
        migrate(migrated_conn)


def test_reason_flier_is_rejected(migrated_conn: sqlite3.Connection, chain: dict[str, int]) -> None:  # noqa: F811
    group_id = insert_group(migrated_conn, chain["load"], chain["lot"])
    insert = "INSERT INTO detection_rejections (group_id, x_in, y_in, reason, created_at) VALUES (?, 1, 1, ?, ?)"
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        migrated_conn.execute(insert, (group_id, "flier", NOW))
    migrated_conn.execute(insert, (group_id, "not_a_bullet_hole", NOW))


def test_final_group_without_aim_is_rejected(
    migrated_conn: sqlite3.Connection,  # noqa: F811
    chain: dict[str, int],
) -> None:
    args = (migrated_conn, chain["load"], chain["lot"])
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_group(*args, public_id="g-final-noaim", status="final", aim=None)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_group(*args, public_id="g-final-halfaim", status="final", aim=(1.0, None))  # type: ignore[arg-type]
    insert_group(*args, public_id="g-draft", status="draft", aim=None)
    insert_group(*args, public_id="g-final-ok", status="final", aim=(3.0, 4.0))


def test_finalizing_an_existing_draft_without_aim_is_rejected(
    migrated_conn: sqlite3.Connection,  # noqa: F811
    chain: dict[str, int],
) -> None:
    group_id = insert_group(migrated_conn, chain["load"], chain["lot"])
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        migrated_conn.execute("UPDATE groups SET status = 'final' WHERE id = ?", (group_id,))


def test_confirmed_photo_requires_transform_and_ppi(migrated_conn: sqlite3.Connection) -> None:  # noqa: F811
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_photo(migrated_conn, sha256="b" * 64, reference_confirmed=1)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_photo(
            migrated_conn, sha256="c" * 64, reference_confirmed=1, h_px_to_plane_json="[[1]]"
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_photo(migrated_conn, sha256="d" * 64, reference_confirmed=1, working_ppi=200.0)
    insert_photo(migrated_conn, sha256="e" * 64)
    insert_photo(
        migrated_conn,
        sha256="f" * 64,
        reference_confirmed=1,
        h_px_to_plane_json="[[1]]",
        working_ppi=200.0,
    )


def test_foreign_key_violation_is_rejected(migrated_conn: sqlite3.Connection) -> None:  # noqa: F811
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        insert_load(migrated_conn, rifle_id=9999)


def test_foreign_keys_are_enforced_on_every_new_connection(tmp_path: Path) -> None:
    path = tmp_path / "again.sqlite3"
    first = connect(path)
    migrate(first)
    first.close()
    second = connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            insert_load(second, rifle_id=9999)
    finally:
        second.close()


def test_load_referenced_by_a_group_cannot_be_deleted(
    migrated_conn: sqlite3.Connection,  # noqa: F811
    chain: dict[str, int],
) -> None:
    insert_group(migrated_conn, chain["load"], chain["lot"])
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        migrated_conn.execute("DELETE FROM loads WHERE id = ?", (chain["load"],))


def test_deleting_zero_load_nulls_the_rifle_pointer(
    migrated_conn: sqlite3.Connection,  # noqa: F811
    chain: dict[str, int],
) -> None:
    migrated_conn.execute("UPDATE rifles SET zero_load_id = ?", (chain["load"],))
    migrated_conn.execute("DELETE FROM loads WHERE id = ?", (chain["load"],))
    assert migrated_conn.execute("SELECT zero_load_id FROM rifles").fetchone()[0] is None


def test_deleting_a_group_cascades_to_shots_and_rejections(
    migrated_conn: sqlite3.Connection,  # noqa: F811
    chain: dict[str, int],
) -> None:
    group_id = insert_group(migrated_conn, chain["load"], chain["lot"])
    migrated_conn.execute(
        "INSERT INTO shots (group_id, seq, x_in, y_in, origin) VALUES (?, 1, 1, 1, 'detected')",
        (group_id,),
    )
    migrated_conn.execute(
        "INSERT INTO detection_rejections (group_id, x_in, y_in, reason, created_at)"
        " VALUES (?, 1, 1, 'not_a_bullet_hole', ?)",
        (group_id, NOW),
    )
    migrated_conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    assert migrated_conn.execute("SELECT count(*) FROM shots").fetchone()[0] == 0
    assert migrated_conn.execute("SELECT count(*) FROM detection_rejections").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        (
            "INSERT INTO rifles (public_id,name,cartridge,bullet_diameter_in,created_at,updated_at)"
            " VALUES ('x','n','c',0.1,?,?)",
            (NOW, NOW),
        ),
        (
            "INSERT INTO rifles (public_id,name,cartridge,bullet_diameter_in,created_at,updated_at)"
            " VALUES ('x','n','c',0.6,?,?)",
            (NOW, NOW),
        ),
        (
            "INSERT INTO loads (public_id,rifle_id,kind,label,created_at,updated_at)"
            " VALUES ('x',1,'reload','l',?,?)",
            (NOW, NOW),
        ),
        (
            "INSERT INTO sessions (public_id,rifle_id,date,distance_yd,position,suppressor,created_at,updated_at)"
            " VALUES ('x',1,'d',100,'prone',0,?,?)",
            (NOW, NOW),
        ),
        (
            "INSERT INTO sessions (public_id,rifle_id,date,distance_yd,position,suppressor,created_at,updated_at)"
            " VALUES ('x',1,'d',100,'sandbags',0,?,?)",
            (NOW, NOW),
        ),
        (
            "INSERT INTO sessions (public_id,rifle_id,date,distance_yd,position,suppressor,created_at,updated_at)"
            " VALUES ('x',1,'d',0,'bench',0,?,?)",
            (NOW, NOW),
        ),
        (
            "INSERT INTO sessions (public_id,rifle_id,date,distance_yd,position,suppressor,wind_clock,"
            "created_at,updated_at) VALUES ('x',1,'d',100,'bench',0,13,?,?)",
            (NOW, NOW),
        ),
        ("INSERT INTO shots (group_id,seq,x_in,y_in,origin) VALUES (1,1,0,0,'guessed')", ()),
    ],
    ids=[
        "diameter-low",
        "diameter-high",
        "load-kind",
        "position",
        "position-sandbags-removed",
        "distance",
        "wind-clock",
        "shot-origin",
    ],
)
def test_value_checks_reject_out_of_domain_values(
    migrated_conn: sqlite3.Connection,  # noqa: F811
    chain: dict[str, int],
    sql: str,
    params: tuple[object, ...],
) -> None:
    group_id = insert_group(migrated_conn, chain["load"], chain["lot"])
    assert chain["rifle"] == 1 and group_id == 1
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        migrated_conn.execute(sql, params)


@pytest.mark.parametrize(
    "position", ["bench", "prone_off_pack", "kneeling_sitting", "standing_field"]
)
def test_every_remaining_position_is_accepted(
    migrated_conn: sqlite3.Connection,  # noqa: F811
    chain: dict[str, int],
    position: str,
) -> None:
    migrated_conn.execute(
        "INSERT INTO sessions (public_id,rifle_id,date,distance_yd,position,suppressor,"
        "created_at,updated_at) VALUES (?,?,'d',100,?,0,?,?)",
        (f"s-{position}", chain["rifle"], position, NOW, NOW),
    )


def test_unique_constraints(migrated_conn: sqlite3.Connection, chain: dict[str, int]) -> None:  # noqa: F811
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert_lot(migrated_conn, chain["load"], code="LOT1")
    group_id = insert_group(migrated_conn, chain["load"], chain["lot"])
    shot = "INSERT INTO shots (group_id, seq, x_in, y_in, origin) VALUES (?, 1, 0, 0, 'manual')"
    migrated_conn.execute(shot, (group_id,))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        migrated_conn.execute(shot, (group_id,))


def test_from_row_round_trips_a_stored_row(migrated_conn: sqlite3.Connection) -> None:  # noqa: F811
    rifle_id = insert_rifle(migrated_conn)
    row = migrated_conn.execute("SELECT * FROM rifles WHERE id = ?", (rifle_id,)).fetchone()
    rifle = rows.RifleRow.from_row(row)
    assert rifle == rows.RifleRow(
        id=rifle_id,
        public_id="r-1",
        name="Test",
        cartridge="6.5 Creedmoor",
        bullet_diameter_in=0.264,
        scope=None,
        notes="",
        zero_load_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    with pytest.raises(AttributeError):
        rifle.name = "other"  # type: ignore[misc]


CHECK_VIOLATIONS = [
    ("groups", {"shots_expected": 0}, "shots_expected >= 1"),
    ("groups", {"shots_expected": -3}, "shots_expected >= 1"),
    ("groups", {"purpose": "zero_step3"}, "purpose IN"),
    ("groups", {"status": "archived", "aim_x_in": 1.0, "aim_y_in": 1.0}, "status IN"),
    ("zeros", {"mode": "fresh"}, "mode IN"),
    ("zeros", {"state": "step4"}, "state IN"),
    ("zero_checks", {"result": "maybe"}, "result IN"),
    ("photos", {"reference_kind": "circle_2in"}, "reference_kind IS NULL OR reference_kind IN"),
    (
        "photos",
        {"reference_confirmed": 2, "h_px_to_plane_json": "[[1]]", "working_ppi": 200.0},
        "reference_confirmed IN (0,1)",
    ),
    ("loads", {"trued_mv_fps": 0}, "trued_mv_fps IS NULL OR trued_mv_fps > 0"),
    ("loads", {"trued_mv_fps": -2800.0}, "trued_mv_fps IS NULL OR trued_mv_fps > 0"),
    ("zeros", {"dot_size_in": 0}, "dot_size_in IS NULL OR dot_size_in > 0"),
    ("zeros", {"dot_size_in": -1.0}, "dot_size_in IS NULL OR dot_size_in > 0"),
    ("sessions", {"suppressor": 2}, "suppressor IN (0,1)"),
    ("sessions", {"wind_clock": 0}, "wind_clock IS NULL OR wind_clock BETWEEN 1 AND 12"),
    ("sessions", {"wind_clock": -1}, "wind_clock IS NULL OR wind_clock BETWEEN 1 AND 12"),
    ("sessions", {"wind_clock": 13}, "wind_clock IS NULL OR wind_clock BETWEEN 1 AND 12"),
    ("shots", {"moved_by_user": 2}, "moved_by_user IN (0,1)"),
    ("groups", {"shot_date": "2026-13-45"}, "date(shot_date) IS shot_date"),
    ("groups", {"shot_date": "yesterday"}, "date(shot_date) IS shot_date"),
    ("groups", {"shot_date": "2026-9-3"}, "date(shot_date) IS shot_date"),
    ("groups", {"shot_time": "25:00:00"}, "shot_time IS NULL OR time(shot_time) IS shot_time"),
    ("groups", {"shot_time": "9:30"}, "shot_time IS NULL OR time(shot_time) IS shot_time"),
    ("groups", {"utc_offset": "UTC"}, "utc_offset IS NULL OR utc_offset GLOB"),
    ("groups", {"utc_offset": "+5:00"}, "utc_offset IS NULL OR utc_offset GLOB"),
    ("groups", {"utc_offset": "+05:60"}, "utc_offset IS NULL OR utc_offset GLOB"),
    ("groups", {"shot_at_source": "guess"}, "shot_at_source IN"),
    ("groups", {"distance_yd": 0}, "distance_yd > 0"),
    ("groups", {"distance_yd": -100}, "distance_yd > 0"),
    ("groups", {"position": "prone"}, "position IN"),
    ("groups", {"suppressor": 2}, "suppressor IN (0,1)"),
    ("groups", {"wind_clock": 0}, "wind_clock IS NULL OR wind_clock BETWEEN 1 AND 12"),
    ("groups", {"wind_clock": 13}, "wind_clock IS NULL OR wind_clock BETWEEN 1 AND 12"),
    ("groups", {"weather_skipped": 2}, "weather_skipped IN (0,1)"),
    ("groups", {"suppressor": 0, "suppressor_id": 5}, "suppressor = 1 OR suppressor_id IS NULL"),
    ("photos", {"taken_at": "2026-09-30 10:00:00"}, "taken_at IS NULL OR strftime"),
    ("photos", {"taken_at": "2026-02-30T10:00:00"}, "taken_at IS NULL OR strftime"),
    ("photos", {"taken_at_offset": "UTC"}, "taken_at_offset IS NULL OR taken_at_offset GLOB"),
]


@pytest.mark.parametrize(
    ("table", "overrides", "constraint"),
    CHECK_VIOLATIONS,
    ids=[f"{t}.{next(iter(o))}={next(iter(o.values()))}" for t, o, _ in CHECK_VIOLATIONS],
)
def test_each_check_constraint_rejects_a_violating_value(
    seed: Seed, table: str, overrides: dict[str, object], constraint: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed") as excinfo:
        seed.insert(table, **overrides)
    assert constraint in str(excinfo.value)


@pytest.mark.parametrize(
    ("table", "overrides"),
    [
        ("groups", {"shots_expected": 1}),
        ("groups", {"purpose": "zero_check"}),
        ("groups", {"status": "final", "aim_x_in": 0.0, "aim_y_in": 0.0}),
        ("zeros", {"mode": "existing", "state": "superseded"}),
        ("zeros", {"dot_size_in": 0.5}),
        ("zero_checks", {"result": "possible_lost_zero"}),
        ("photos", {"reference_kind": "grid_1in"}),
        (
            "photos",
            {"reference_confirmed": 1, "h_px_to_plane_json": "[[1]]", "working_ppi": 200.0},
        ),
        ("loads", {"trued_mv_fps": 2800.0}),
        ("loads", {"trued_mv_fps": None}),
        ("groups", {"shot_time": "08:05:09", "utc_offset": "-05:00"}),
        ("groups", {"shot_time": None, "utc_offset": "+00:00"}),
        ("groups", {"utc_offset": "+13:45"}),
        ("groups", {"shot_at_source": "photo"}),
        ("groups", {"shot_at_source": "session"}),
        ("groups", {"distance_yd": 1, "wind_clock": 1, "weather_skipped": 1}),
        ("groups", {"wind_clock": 12, "suppressor": 1}),
        ("groups", {"temperature_f": None, "density_altitude_ft": None, "wind_mph": None}),
        ("photos", {"taken_at": "2026-09-30T10:00:00", "taken_at_offset": "+02:00"}),
        ("photos", {"taken_at": None, "taken_at_offset": None}),
        ("sessions", {"suppressor": 1}),
        ("sessions", {"wind_clock": 1}),
        ("sessions", {"wind_clock": 12}),
        ("sessions", {"wind_clock": None}),
        ("shots", {"moved_by_user": 1}),
    ],
)
def test_boundary_values_next_to_each_check_are_accepted(
    seed: Seed, table: str, overrides: dict[str, object]
) -> None:
    seed.insert(table, **overrides)


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("rifles", "public_id"),
        ("loads", "public_id"),
        ("sessions", "public_id"),
        ("groups", "public_id"),
        ("photos", "sha256"),
        ("zero_checks", "group_id"),
    ],
)
def test_each_unique_column_rejects_a_duplicate(seed: Seed, table: str, column: str) -> None:
    first = seed.values(table)
    seed.conn.execute(
        f"INSERT INTO {table} ({', '.join(first)}) VALUES ({', '.join('?' for _ in first)})",
        tuple(first.values()),
    )
    with pytest.raises(sqlite3.IntegrityError, match=f"UNIQUE constraint failed: {table}.{column}"):
        seed.insert(table, **{column: first[column]})


def test_photo_sha256_unique_is_the_dedupe_backstop(seed: Seed) -> None:
    seed.insert("photos", sha256="a" * 64)
    with pytest.raises(sqlite3.IntegrityError, match="photos.sha256"):
        seed.insert("photos", sha256="a" * 64)
    seed.insert("photos", sha256="b" * 64)


@pytest.mark.parametrize(
    "column",
    ["shot_date", "shot_at_source", "distance_yd", "position", "suppressor", "weather_skipped"],
)
def test_group_context_columns_are_required(seed: Seed, column: str) -> None:
    with pytest.raises(
        sqlite3.IntegrityError, match=f"NOT NULL constraint failed: groups.{column}"
    ):
        seed.insert("groups", **{column: None})


def test_deleting_a_suppressor_leaves_its_groups_as_plain_suppressed(seed: Seed) -> None:
    sup = int(
        seed.conn.execute(
            "INSERT INTO suppressors (name, created_at) VALUES ('Omega', ?)", (NOW,)
        ).lastrowid
    )
    group_id = seed.insert("groups", suppressor=1, suppressor_id=sup)
    seed.conn.execute("DELETE FROM suppressors WHERE id = ?", (sup,))
    row = seed.conn.execute(
        "SELECT suppressor, suppressor_id FROM groups WHERE id = ?", (group_id,)
    ).fetchone()
    assert (row["suppressor"], row["suppressor_id"]) == (1, None)


def test_group_lot_id_is_required(seed: Seed) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed: groups.lot_id"):
        seed.insert("groups", lot_id=None)


def test_deleting_a_zero_nulls_the_group_pointer_and_keeps_the_group(seed: Seed) -> None:
    zero_id = seed.insert("zeros")
    group_id = seed.insert("groups", zero_id=zero_id)
    seed.conn.execute("DELETE FROM zeros WHERE id = ?", (zero_id,))
    row = seed.conn.execute("SELECT zero_id FROM groups WHERE id = ?", (group_id,)).fetchone()
    assert row is not None
    assert row["zero_id"] is None


def test_deleting_a_rifle_cascades_to_its_loads(seed: Seed) -> None:
    rifle_id = seed.insert("rifles")
    seed.insert("loads", rifle_id=rifle_id)
    seed.conn.execute("DELETE FROM rifles WHERE id = ?", (rifle_id,))
    count = seed.conn.execute("SELECT count(*) FROM loads WHERE rifle_id = ?", (rifle_id,))
    assert count.fetchone()[0] == 0


def test_deleting_a_rifle_cascades_to_its_sessions(seed: Seed) -> None:
    rifle_id = seed.insert("rifles")
    seed.insert("sessions", rifle_id=rifle_id)
    seed.conn.execute("DELETE FROM rifles WHERE id = ?", (rifle_id,))
    count = seed.conn.execute("SELECT count(*) FROM sessions WHERE rifle_id = ?", (rifle_id,))
    assert count.fetchone()[0] == 0


def test_a_rifle_with_groups_cannot_be_deleted_until_its_groups_are(seed: Seed) -> None:
    rifle_id = seed.insert("rifles")
    load_id = seed.insert("loads", rifle_id=rifle_id)
    lot_id = seed.insert("load_lots", load_id=load_id)
    group_id = seed.insert("groups", load_id=load_id, lot_id=lot_id)
    seed.insert("shots", group_id=group_id)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        seed.conn.execute("DELETE FROM rifles WHERE id = ?", (rifle_id,))
    for table, row_id in (("rifles", rifle_id), ("loads", load_id), ("groups", group_id)):
        found = seed.conn.execute(f"SELECT count(*) FROM {table} WHERE id = ?", (row_id,))
        assert found.fetchone()[0] == 1, table

    seed.conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    seed.conn.execute("DELETE FROM rifles WHERE id = ?", (rifle_id,))
    for table, row_id in (("loads", load_id), ("load_lots", lot_id)):
        found = seed.conn.execute(f"SELECT count(*) FROM {table} WHERE id = ?", (row_id,))
        assert found.fetchone()[0] == 0, table
    shots = seed.conn.execute("SELECT count(*) FROM shots WHERE group_id = ?", (group_id,))
    assert shots.fetchone()[0] == 0


@pytest.mark.parametrize(
    ("table", "column", "parent"),
    [
        ("groups", "load_id", "loads"),
        ("groups", "photo_id", "photos"),
        ("groups", "suppressor_id", "suppressors"),
        ("zeros", "lot_id", "load_lots"),
    ],
)
def test_foreign_key_column_rejects_a_missing_parent_and_accepts_a_real_one(
    seed: Seed, table: str, column: str, parent: str
) -> None:
    extra = {"suppressor": 1} if column == "suppressor_id" else {}
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        seed.insert(table, **{column: 9999, **extra})
    assert (
        seed.conn.execute(f"SELECT count(*) FROM {table} WHERE {column} = 9999").fetchone()[0] == 0
    )
    seed.insert(table, **{column: seed.insert(parent), **extra})


def test_deleting_a_zero_deletes_its_zero_checks(seed: Seed) -> None:
    zero_id = seed.insert("zeros")
    check_id = seed.insert("zero_checks", zero_id=zero_id)
    other_check_id = seed.insert("zero_checks")
    seed.conn.execute("DELETE FROM zeros WHERE id = ?", (zero_id,))
    remaining = [r["id"] for r in seed.conn.execute("SELECT id FROM zero_checks")]
    assert check_id not in remaining
    assert other_check_id in remaining


def test_saved_aggregate_tables_match_their_row_dataclass_and_constraints(
    migrated_conn: sqlite3.Connection,  # noqa: F811
    chain: dict[str, int],
) -> None:
    columns = [r["name"] for r in migrated_conn.execute("PRAGMA table_info(aggregates)")]
    assert [f.name for f in fields(repo_aggregates.AggregateRow)] == columns
    member_columns = [
        r["name"] for r in migrated_conn.execute("PRAGMA table_info(aggregate_groups)")
    ]
    assert member_columns == ["aggregate_id", "group_id"]
    insert = (
        "INSERT INTO aggregates (public_id, name, rifle_id, load_id, distance_yd, group_count,"
        " created_at) VALUES (?, 'n', ?, ?, ?, ?, ?)"
    )
    args = (chain["rifle"], chain["load"])
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        migrated_conn.execute(insert, ("a1", *args, 100, 1, NOW))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        migrated_conn.execute(insert, ("a2", *args, 0, 2, NOW))
    migrated_conn.execute(insert, ("a3", *args, 100, 2, NOW))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        migrated_conn.execute(insert, ("a3", *args, 100, 2, NOW))
    migrated_conn.execute("DELETE FROM loads WHERE id = ?", (chain["load"],))
    assert migrated_conn.execute("SELECT count(*) FROM aggregates").fetchone()[0] == 0


# ----------------------------------------------------------------------------------- migration 003

BULLET_CASES = [
    # (stored bullet text, expected weight, expected line)
    ("116 TMK", 116.0, "TMK"),
    ("140gr ELD-M", 140.0, "ELD-M"),
    ("140 gr ELD-M", 140.0, "ELD-M"),
    ("140 grain ELD-M", 140.0, "ELD-M"),
    ("140grains ELD", 140.0, "ELD"),
    ("143 ELD-X", 143.0, "ELD-X"),
    ("168-gr SMK", 168.0, "SMK"),
    ("  95 Hornady V-Max ", 95.0, "Hornady V-Max"),
    ("150.5 gr Berger", 150.5, "Berger"),
    ("140gr.TMK", 140.0, "TMK"),
    ("140 grizzly", 140.0, "grizzly"),
    ("116", 116.0, None),
    ("116 gr", 116.0, None),
    ("6.5 140 ELD", None, "6.5 140 ELD"),
    ("300 Win Mag 180 gr", None, "300 Win Mag 180 gr"),
    ("ELD-X 143", None, "ELD-X 143"),
    ("1.2.3 foo", None, "1.2.3 foo"),
    ("1500 big", None, "1500 big"),
    ("9 tiny", None, "9 tiny"),
    ("Custom pill", None, "Custom pill"),
    ("", None, ""),
]


def build_v2_database(path: Path) -> sqlite3.Connection:
    """A database exactly as the app left it at schema version 2, with handloads and a factory load."""
    conn = connect(path)
    for version, _, sql in db_connection._migration_files():
        if version <= 2:
            conn.executescript(sql)
    assert schema_version(conn) == 2
    rifle = insert_rifle(conn)
    for index, (bullet, _, _) in enumerate(BULLET_CASES):
        conn.execute(
            "INSERT INTO loads (public_id, rifle_id, kind, label, bullet, notes, created_at,"
            " updated_at) VALUES (?, ?, 'handload', ?, ?, '', ?, ?)",
            (f"hb-{index}", rifle, f"hl{index}", bullet, NOW, NOW),
        )
    conn.execute(
        "INSERT INTO loads (public_id, rifle_id, kind, label, bullet, powder, charge_gr, primer,"
        " trued_mv_fps, notes, created_at, updated_at) VALUES ('pc-1', ?, 'handload', 'full',"
        " '143 ELD-X', 'H4350', 41.5, 'CCI BR-2', 2712, 'old note', ?, ?)",
        (rifle, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO loads (public_id, rifle_id, kind, label, powder, charge_gr, notes, created_at,"
        " updated_at) VALUES ('pw-2', ?, 'handload', 'whole charge', 'Varget', 40.0, '', ?, ?)",
        (rifle, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO loads (public_id, rifle_id, kind, label, powder, notes, created_at, updated_at)"
        " VALUES ('po-1', ?, 'handload', 'powder only', ' Varget ', '', ?, ?)",
        (rifle, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO loads (public_id, rifle_id, kind, label, charge_gr, notes, created_at,"
        " updated_at) VALUES ('ch-1', ?, 'handload', 'charge only', 41.0, 'n1', ?, ?)",
        (rifle, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO loads (public_id, rifle_id, kind, label, powder, notes, created_at, updated_at)"
        " VALUES ('bl-1', ?, 'handload', 'blank powder', '  ', 'keep', ?, ?)",
        (rifle, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO loads (public_id, rifle_id, kind, label, maker, product, bullet_weight_gr,"
        " bullet_type, notes, created_at, updated_at) VALUES ('fa-1', ?, 'factory', 'Fed', 'Federal',"
        " 'GMM', 168, 'BTHP', 'factory note', ?, ?)",
        (rifle, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO loads (public_id, rifle_id, kind, label, bullet, bullet_weight_gr, notes,"
        " created_at, updated_at) VALUES ('hw-1', ?, 'handload', 'has weight', '175 SMK', 168, '',"
        " ?, ?)",
        (rifle, NOW, NOW),
    )
    lot_load = int(conn.execute("SELECT id FROM loads WHERE public_id = 'pc-1'").fetchone()[0])
    insert_lot(conn, lot_load, "LOT-X")
    conn.execute("UPDATE rifles SET zero_load_id = ? WHERE id = ?", (lot_load, rifle))
    conn.commit()
    return conn


def snapshot(conn: sqlite3.Connection, table: str, columns: list[str]) -> list[tuple]:
    return [tuple(r) for r in conn.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY id")]


def test_migration_003_is_packaged_and_sets_version_3() -> None:
    names = [name for _, name, _ in db_connection._migration_files()]
    assert "003_handload_fields.sql" in names and LATEST >= 3


def test_a_v2_database_with_handloads_migrates_without_losing_any_value(tmp_path: Path) -> None:
    conn = build_v2_database(tmp_path / "v2.sqlite3")
    old_columns = [r["name"] for r in conn.execute("PRAGMA table_info(loads)")]
    before = {r["public_id"]: dict(r) for r in conn.execute("SELECT * FROM loads ORDER BY id")}
    lots_before = snapshot(conn, "load_lots", ["id", "load_id", "lot_code", "created_at"])
    rifles_before = snapshot(conn, "rifles", ["id", "name", "zero_load_id"])

    assert migrate(conn) == LATEST >= 3
    new_columns = [r["name"] for r in conn.execute("PRAGMA table_info(loads)")]
    assert new_columns[: len(old_columns)] == old_columns, "no column was dropped or reordered"
    assert {"brass", "coal_in"} <= set(new_columns)
    after = {r["public_id"]: dict(r) for r in conn.execute("SELECT * FROM loads ORDER BY id")}
    assert after.keys() == before.keys()

    for index, (bullet, weight, line) in enumerate(BULLET_CASES):
        row, was = after[f"hb-{index}"], before[f"hb-{index}"]
        assert row["bullet_weight_gr"] == weight, bullet
        assert row["bullet"] == (line if line else (None if weight is not None else bullet)), bullet
        assert row["notes"] == "" and row["powder"] is None and row["charge_gr"] is None
        assert (row["brass"], row["coal_in"]) == (None, None)
        assert row["label"] == was["label"] and row["id"] == was["id"]
        if weight is not None:  # the split loses nothing: weight and line are both in the text
            assert bullet.strip().startswith(f"{weight:g}"), bullet
            assert line is None or line in bullet, bullet

    full = after["pc-1"]
    assert (full["bullet_weight_gr"], full["bullet"], full["primer"], full["trued_mv_fps"]) == (
        143.0,
        "ELD-X",
        "CCI BR-2",
        2712.0,
    )
    assert full["notes"] == "old note\nPowder: H4350, charge: 41.5 gr"
    assert after["pw-2"]["notes"] == "Powder: Varget, charge: 40 gr"
    assert after["po-1"]["notes"] == "Powder: Varget"
    assert after["ch-1"]["notes"] == "n1\nCharge: 41 gr"
    assert after["bl-1"]["notes"] == "keep"
    for name in ("pc-1", "po-1", "ch-1"):
        assert after[name]["powder"] == before[name]["powder"], "retired columns keep their values"
        assert after[name]["charge_gr"] == before[name]["charge_gr"]

    factory_before, factory_after = before["fa-1"], after["fa-1"]
    assert {
        k: v for k, v in factory_after.items() if k not in ("brass", "coal_in")
    } == factory_before
    keep = after["hw-1"]
    assert (keep["bullet_weight_gr"], keep["bullet"]) == (168.0, "175 SMK"), (
        "an existing weight is kept"
    )

    assert snapshot(conn, "load_lots", ["id", "load_id", "lot_code", "created_at"]) == lots_before
    assert snapshot(conn, "rifles", ["id", "name", "zero_load_id"]) == rifles_before
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrate(conn) == LATEST
    assert {r["public_id"]: dict(r) for r in conn.execute("SELECT * FROM loads")} == after
    conn.close()


def test_every_powder_and_charge_value_is_in_the_notes_after_migration(tmp_path: Path) -> None:
    conn = build_v2_database(tmp_path / "v2.sqlite3")
    pairs = [
        (r["powder"], r["charge_gr"], r["public_id"])
        for r in conn.execute(
            "SELECT * FROM loads WHERE powder IS NOT NULL OR charge_gr IS NOT NULL"
        )
    ]
    migrate(conn)
    notes = {r["public_id"]: r["notes"] for r in conn.execute("SELECT public_id, notes FROM loads")}
    assert len(pairs) == 5
    for powder, charge, public_id in pairs:
        if powder and powder.strip():
            assert powder.strip() in notes[public_id], public_id
        if charge is not None:
            assert f"{charge:g}" in notes[public_id], public_id
    conn.close()


def test_the_new_columns_accept_only_coal_between_one_and_five_inches(tmp_path: Path) -> None:
    conn = build_v2_database(tmp_path / "v2.sqlite3")
    migrate(conn)
    for bad in (0.999, 5.001, 0, -1):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute("UPDATE loads SET coal_in = ? WHERE public_id = 'pc-1'", (bad,))
    for good in (1.0, 2.8, 5.0, None):
        conn.execute("UPDATE loads SET coal_in = ? WHERE public_id = 'pc-1'", (good,))
    conn.close()


def test_a_v2_data_directory_upgrades_to_the_latest_schema_when_opened(tmp_path: Path) -> None:
    path = tmp_path / "data" / "rga.sqlite3"
    path.parent.mkdir(parents=True)
    build_v2_database(path).close()
    conn = connect(path)
    try:
        assert migrate(conn) == LATEST
        row = conn.execute("SELECT * FROM loads WHERE public_id = 'hb-0'").fetchone()
        assert (row["bullet_weight_gr"], row["bullet"]) == (116.0, "TMK")
    finally:
        conn.close()


# ----------------------------------------------------------------------------------- migration 005
# Sessions are gone: groups carry their own context. The user's database is at schema v2 with one
# rifle, one load, three sessions (one with no groups), four groups, 40 shots and two photos.

DIRECTIVE = "-- rga: foreign_keys=off"
SHOT_COLUMNS = (
    "id, group_id, seq, x_in, y_in, dx_in, dy_in, dx_mil, dy_mil, origin, moved_by_user, confidence"
)
PHOTO_COLUMNS_V4 = (
    "id, sha256, ext, width_px, height_px, reference_kind, reference_json, h_px_to_plane_json,"
    " working_ppi, working_w, working_h, axis_ratio, reference_confirmed, created_at"
)
SESSION_ROWS = [
    # date, distance, position, suppressor, temp, DA, wind mph, wind clock, skipped, notes
    ("2026-09-01", 100, "bench", 0, 70.0, 1200.0, 5.0, 3, 0, "range day one"),
    ("2026-09-15", 200, "prone_off_pack", 1, None, None, None, None, 1, "windy <b>day</b>"),
    ("2026-09-20", 300, "standing_field", 0, None, None, None, None, 0, "nothing shot"),
]
GROUP_ROWS = [
    # session index, lot code, photo index, status, aim, elevation, purpose, notes
    (0, "LOT-A", 0, "final", (4.25, 5.5), 2.4, "general", "first"),
    (0, "LOT-A", None, "draft", None, None, "general", ""),
    (1, "LOT-B", None, "final", (4.0, 5.0), 3.1, "general", "second <i>group</i>"),
    (1, "LOT-B", 1, "draft", None, None, "general", ""),
]


def build_legacy_database(
    path: Path, version: int, *, extras: bool = False, orphan: bool = False
) -> sqlite3.Connection:
    """A database as the app left it at schema `version` (2 to 4), shaped like the user's.

    `extras` adds a zero, a zero check on group 3 and a saved combination, so every table that
    points at `groups` has rows. `orphan` adds a group whose session does not exist.
    """
    conn = connect(path)
    for number, _, sql in db_connection._migration_files():
        if number <= version:
            conn.executescript(sql)
    assert schema_version(conn) == version
    rifle = insert_rifle(conn)
    load = insert_load(conn, rifle)
    lots = {code: insert_lot(conn, load, code) for code in ("LOT-A", "LOT-B")}
    photos = [
        insert_photo(
            conn,
            sha256="1" * 64,
            reference_kind="circle_1in",
            reference_json="{}",
            h_px_to_plane_json="[[1,0,0],[0,1,0],[0,0,1]]",
            working_ppi=200.0,
            working_w=1600,
            working_h=1200,
            axis_ratio=1.0,
            reference_confirmed=1,
        ),
        insert_photo(conn, sha256="2" * 64),
    ]
    sessions = []
    for index, (
        day,
        distance,
        position,
        suppressor,
        temp,
        alt,
        wind,
        clock,
        skipped,
        notes,
    ) in enumerate(SESSION_ROWS):
        cur = conn.execute(
            "INSERT INTO sessions (public_id, rifle_id, date, distance_yd, position, suppressor,"
            " temperature_f, density_altitude_ft, wind_mph, wind_clock, weather_skipped, notes,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"s-{index}", rifle, day, distance, position, suppressor, temp, alt, wind, clock)
            + (skipped, notes, NOW, NOW),
        )
        sessions.append(int(cur.lastrowid))
    if version >= 4:
        conn.execute("INSERT INTO suppressors (name, created_at) VALUES ('Omega', ?)", (NOW,))
        conn.execute("UPDATE sessions SET suppressor_id = 1 WHERE id = ?", (sessions[1],))
    for index, (sess, lot, photo, status, aim, elevation, purpose, notes) in enumerate(GROUP_ROWS):
        aim_x, aim_y = aim if aim else (None, None)
        conn.execute(
            "INSERT INTO groups (public_id, session_id, load_id, lot_id, photo_id, roi_json,"
            " shots_expected, elevation_dialed_mil, aim_x_in, aim_y_in, purpose, status, notes,"
            " finalized_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 10, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?)",
            (
                f"g-{index}",
                sessions[sess],
                load,
                lots[lot],
                None if photo is None else photos[photo],
                None if photo is None else '{"x0":1,"y0":2,"x1":30,"y1":40}',
                elevation,
                aim_x,
                aim_y,
                purpose,
                status,
                notes,
                NOW if status == "final" else None,
                NOW,
                NOW,
            ),
        )
    for group in range(1, len(GROUP_ROWS) + 1):
        for seq in range(1, 11):
            conn.execute(
                "INSERT INTO shots (group_id, seq, x_in, y_in, dx_in, dy_in, dx_mil, dy_mil,"
                " origin, moved_by_user, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    group,
                    seq,
                    group + seq / 7,
                    group * 2 - seq / 11,
                    seq / 13,
                    -seq / 17,
                    seq / 3.6,
                    -seq / 3.7,
                    "detected" if seq % 5 else "estimated",
                    seq % 2,
                    None if seq % 3 else 0.5 + seq / 100,
                ),
            )
    for x in (1.5, 2.5):
        conn.execute(
            "INSERT INTO detection_rejections (group_id, x_in, y_in, reason, created_at)"
            " VALUES (1, ?, ?, 'not_a_bullet_hole', ?)",
            (x, x * 2, NOW),
        )
    if extras:
        conn.execute(
            "INSERT INTO zeros (rifle_id, load_id, mode, state, dot_size_in, created_at,"
            " updated_at) VALUES (?, ?, 'existing', 'complete', 1.0, ?, ?)",
            (rifle, load, NOW, NOW),
        )
        conn.execute("UPDATE groups SET purpose = 'zero_check', zero_id = 1 WHERE id = 3")
        conn.execute(
            "INSERT INTO zero_checks (zero_id, group_id, result, created_at)"
            " VALUES (1, 3, 'pass', ?)",
            (NOW,),
        )
        conn.execute(
            "INSERT INTO aggregates (public_id, name, rifle_id, load_id, distance_yd,"
            " group_count, created_at) VALUES ('a-1', 'combo', ?, ?, 100, 2, ?)",
            (rifle, load, NOW),
        )
        conn.execute("INSERT INTO aggregate_groups (aggregate_id, group_id) VALUES (1, 1), (1, 3)")
    if orphan:
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO groups (public_id, session_id, load_id, lot_id, shots_expected,"
            " created_at, updated_at) VALUES ('g-orphan', 999, ?, ?, 3, ?, ?)",
            (load, lots["LOT-A"], NOW, NOW),
        )
        conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    return conn


def child_dump(
    conn: sqlite3.Connection, columns: dict[str, list[str]] | None = None
) -> tuple[dict[str, list[tuple]], dict[str, list[str]]]:
    """Every table the rebuild must leave byte-equal, as value tuples ordered by rowid.

    Returns the dump and the columns it used; pass those columns to dump a later schema version
    of the same tables (earlier migrations only add columns).
    """
    if columns is None:
        tables = ["rifles", "loads", "load_lots", "sessions", "zeros", "zero_checks", "shots"]
        tables += ["detection_rejections", "aggregates", "aggregate_groups", "photos"]
        columns = {t: [r["name"] for r in conn.execute(f"PRAGMA table_info({t})")] for t in tables}
        columns["photos"] = [c for c in columns["photos"] if not c.startswith("taken_at")]
    dump = {
        table: [
            tuple(r) for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY rowid")
        ]
        for table, cols in columns.items()
    }
    return dump, columns


OLD_GROUP_COLUMNS = (
    "id, public_id, load_id, lot_id, photo_id, roi_json, shots_expected, elevation_dialed_mil,"
    " aim_x_in, aim_y_in, purpose, zero_id, status, notes, finalized_at, created_at, updated_at"
)
UPGRADE_CASES = [(2, False), (2, True), (4, False), (4, True)]
UPGRADE_IDS = [f"v{v}{'-extras' if e else ''}" for v, e in UPGRADE_CASES]


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.pre-*"))


@pytest.mark.parametrize(("start", "extras"), UPGRADE_CASES, ids=UPGRADE_IDS)
def test_migration_005_loses_nothing_and_keeps_every_id(
    tmp_path: Path, start: int, extras: bool
) -> None:
    path = tmp_path / "rga.sqlite3"
    conn = build_legacy_database(path, start, extras=extras)
    before, used = child_dump(conn)
    groups_before = [
        tuple(r) for r in conn.execute(f"SELECT {OLD_GROUP_COLUMNS} FROM groups ORDER BY id")
    ]
    counts = {t: len(before[t]) for t in ("sessions", "shots", "photos", "detection_rejections")}
    assert counts == {"sessions": 3, "shots": 40, "photos": 2, "detection_rejections": 2}

    assert migrate(conn) == LATEST >= 5

    assert child_dump(conn, used)[0] == before
    assert [
        tuple(r) for r in conn.execute(f"SELECT {OLD_GROUP_COLUMNS} FROM groups ORDER BY id")
    ] == groups_before
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(groups)")]
    assert "session_id" not in columns and {"shot_date", "legacy_session_id"} <= set(columns)
    photo = conn.execute("SELECT taken_at, taken_at_offset FROM photos").fetchall()
    assert [tuple(r) for r in photo] == [(None, None), (None, None)]
    conn.close()


@pytest.mark.parametrize(("start", "extras"), UPGRADE_CASES, ids=UPGRADE_IDS)
def test_each_group_takes_the_context_of_the_session_it_was_in(
    tmp_path: Path, start: int, extras: bool
) -> None:
    conn = build_legacy_database(tmp_path / "rga.sqlite3", start, extras=extras)
    sessions = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM sessions")}
    old_session_of = {
        r["id"]: r["session_id"] for r in conn.execute("SELECT id, session_id FROM groups")
    }
    migrate(conn)

    for row in conn.execute("SELECT * FROM groups ORDER BY id"):
        session = sessions[old_session_of[row["id"]]]
        assert row["legacy_session_id"] == session["id"]
        assert row["legacy_session_notes"] == session["notes"]
        assert (row["shot_date"], row["shot_time"], row["utc_offset"]) == (
            session["date"],
            None,
            None,
        )
        assert row["shot_at_source"] == "session"
        assert (row["distance_yd"], row["position"]) == (
            session["distance_yd"],
            session["position"],
        )
        assert row["suppressor"] == session["suppressor"]
        assert row["suppressor_id"] == (
            session.get("suppressor_id") if session["suppressor"] else None
        )
        for column in ("temperature_f", "density_altitude_ft", "wind_mph", "wind_clock"):
            assert row[column] == session[column], column
        assert row["weather_skipped"] == session["weather_skipped"]
    suppressed = conn.execute(
        "SELECT suppressor, suppressor_id FROM groups WHERE id = 3"
    ).fetchone()
    assert tuple(suppressed) == (1, 1 if start >= 4 else None)
    conn.close()


def test_the_session_without_groups_is_kept(tmp_path: Path) -> None:
    conn = build_legacy_database(tmp_path / "rga.sqlite3", 2)
    migrate(conn)
    sessions = conn.execute("SELECT date, distance_yd, notes FROM sessions ORDER BY id").fetchall()
    assert [tuple(r) for r in sessions] == [
        ("2026-09-01", 100, "range day one"),
        ("2026-09-15", 200, "windy <b>day</b>"),
        ("2026-09-20", 300, "nothing shot"),
    ]
    used = {r[0] for r in conn.execute("SELECT legacy_session_id FROM groups")}
    assert 3 not in used and conn.execute("SELECT count(*) FROM groups").fetchone()[0] == 4
    conn.close()


@pytest.mark.parametrize(("start", "extras"), [(2, False), (4, True)], ids=["v2", "v4-extras"])
def test_foreign_keys_are_on_and_clean_after_the_rebuild(
    tmp_path: Path, start: int, extras: bool
) -> None:
    conn = build_legacy_database(tmp_path / "rga.sqlite3", start, extras=extras)
    migrate(conn)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    for child in ("shots", "detection_rejections", "zero_checks", "aggregate_groups"):
        targets = {r["table"] for r in conn.execute(f"PRAGMA foreign_key_list({child})")}
        assert "groups" in targets, child
        assert not any(t.endswith("_new") for t in targets), child
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO shots (group_id, seq, x_in, y_in, origin) VALUES (999, 1, 0, 0, 'manual')"
        )
    conn.rollback()
    conn.execute("DELETE FROM groups WHERE id = 1")
    left = conn.execute("SELECT count(*) FROM shots WHERE group_id = 1").fetchone()[0]
    assert left == 0, "deleting a group still cascades to its shots"
    assert conn.execute("SELECT count(*) FROM detection_rejections").fetchone()[0] == 0
    indexes = {r["name"] for r in conn.execute("PRAGMA index_list(groups)")}
    assert set(EXPECTED_INDEXES) & {n for n in indexes if n.startswith("ix_groups")} == {
        n for n in EXPECTED_INDEXES if n.startswith("ix_groups")
    }
    conn.close()


def test_the_upgrade_is_idempotent_and_leaves_one_backup(tmp_path: Path) -> None:
    path = tmp_path / "rga.sqlite3"
    conn = build_legacy_database(path, 2)
    migrate(conn)
    after, used = child_dump(conn)
    assert migrate(conn) == LATEST
    assert child_dump(conn, used)[0] == after
    assert [b.name for b in backups(path)] == ["rga.sqlite3.pre-005"]
    conn.close()


def test_the_backup_is_a_private_copy_of_the_database_as_it_was(tmp_path: Path) -> None:
    path = tmp_path / "rga.sqlite3"
    conn = build_legacy_database(path, 2, extras=True)
    before, used = child_dump(conn)
    groups_before = [tuple(r) for r in conn.execute("SELECT * FROM groups ORDER BY id")]
    migrate(conn)

    (backup,) = backups(path)
    assert backup.name == "rga.sqlite3.pre-005"
    assert backup.stat().st_mode & 0o777 == 0o600
    copy = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    copy.row_factory = sqlite3.Row
    try:
        assert schema_version(copy) == 2
        assert [tuple(r) for r in copy.execute("SELECT * FROM groups ORDER BY id")] == groups_before
        assert copy.execute("SELECT count(*) FROM shots").fetchone()[0] == 40
        assert copy.execute("SELECT count(*) FROM sessions").fetchone()[0] == 3
        assert child_dump(copy, used)[0] == before
    finally:
        copy.close()
    conn.close()


def test_an_existing_backup_is_never_replaced(tmp_path: Path) -> None:
    path = tmp_path / "rga.sqlite3"
    conn = build_legacy_database(path, 4)
    older = tmp_path / "rga.sqlite3.pre-005"
    older.write_bytes(b"an earlier backup")
    migrate(conn)
    assert older.read_bytes() == b"an earlier backup"
    newer = tmp_path / "rga.sqlite3.pre-005.1"
    assert newer.exists() and newer.stat().st_mode & 0o777 == 0o600
    conn.close()


def test_a_database_created_by_the_run_gets_no_backup(tmp_path: Path) -> None:
    path = tmp_path / "rga.sqlite3"
    conn = connect(path)
    assert migrate(conn) == LATEST
    assert backups(path) == []
    conn.close()


def test_a_database_that_is_already_current_gets_no_backup(tmp_path: Path) -> None:
    path = tmp_path / "rga.sqlite3"
    conn = connect(path)
    migrate(conn)
    migrate(conn)
    assert backups(path) == []
    conn.close()


def v5_sql() -> str:
    return next(sql for number, _, sql in db_connection._migration_files() if number == 5)


def with_files(
    monkeypatch: pytest.MonkeyPatch, *extra: tuple[int, str, str], replace5: str | None = None
):
    files = [
        (n, name, replace5 if (n == 5 and replace5 is not None) else sql)
        for n, name, sql in db_connection._migration_files()
    ]
    monkeypatch.setattr(db_connection, "_migration_files", lambda: [*files, *extra])


def full_dump(conn: sqlite3.Connection) -> tuple[list[tuple], dict[str, list[tuple]]]:
    master = [
        tuple(r) for r in conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name")
    ]
    data = {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
        for t in ("groups", "shots", "sessions", "photos")
    }
    return master, data


def test_a_failing_005_leaves_the_v4_database_untouched_with_foreign_keys_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rga.sqlite3"
    conn = build_legacy_database(path, 4, extras=True)
    before = full_dump(conn)
    broken = v5_sql().replace(
        "PRAGMA user_version = 5;", "SELECT * FROM nope;\nPRAGMA user_version = 5;"
    )
    with_files(monkeypatch, replace5=broken)

    with pytest.raises(sqlite3.OperationalError):
        migrate(conn)

    assert schema_version(conn) == 4 and not conn.in_transaction
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert full_dump(conn) == before
    assert [b.name for b in backups(path)] == ["rga.sqlite3.pre-005"]

    monkeypatch.undo()
    assert migrate(conn) == LATEST
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert [b.name for b in backups(path)] == ["rga.sqlite3.pre-005", "rga.sqlite3.pre-005.1"]
    conn.close()


def test_a_session_less_group_aborts_005_instead_of_being_dropped(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rga.sqlite3"
    conn = build_legacy_database(path, 2, orphan=True)
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed"):
        migrate(conn)
    assert schema_version(conn) == 4 and not conn.in_transaction
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(groups)")]
    assert "session_id" in columns and "shot_date" not in columns
    assert conn.execute("SELECT count(*) FROM groups").fetchone()[0] == 5
    assert conn.execute("SELECT count(*) FROM shots").fetchone()[0] == 40
    assert (
        conn.execute("SELECT name FROM sqlite_master WHERE name = 'groups_new'").fetchone() is None
    )
    conn.close()


def fake_migration(first_line: str, body: str | None = None) -> tuple[int, str, str]:
    body = body or (
        "INSERT INTO shots (group_id, seq, x_in, y_in, origin) VALUES (999, 1, 0, 0, 'manual');\n"
        "DELETE FROM shots WHERE group_id = 999;\n"
    )
    return (NEXT, f"{NEXT:03d}_fake.sql", f"{first_line}\n{body}PRAGMA user_version = {NEXT};\n")


def test_the_directive_on_the_first_line_turns_foreign_keys_off_for_that_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rga.sqlite3"
    conn = connect(path)
    migrate(conn)
    with_files(monkeypatch, fake_migration(DIRECTIVE))
    assert migrate(conn) == NEXT
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert [b.name for b in backups(path)] == [f"rga.sqlite3.pre-{NEXT:03d}"]
    conn.close()


@pytest.mark.parametrize(
    "first_line",
    [
        "SELECT 1;\n" + DIRECTIVE,
        "\n" + DIRECTIVE,
        " " + DIRECTIVE,
        DIRECTIVE + " ",
        DIRECTIVE.upper(),
        "--rga: foreign_keys=off",
        "-- rga: foreign_keys=on",
    ],
    ids=[
        "second-line",
        "blank-first-line",
        "leading-space",
        "trailing-space",
        "upper",
        "no-space",
        "on",
    ],
)
def test_the_directive_only_counts_when_it_is_exactly_the_first_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_line: str
) -> None:
    path = tmp_path / "rga.sqlite3"
    conn = connect(path)
    migrate(conn)
    insert_rifle(conn)
    conn.commit()
    with_files(monkeypatch, fake_migration(first_line))

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        migrate(conn)

    assert schema_version(conn) == LATEST and not conn.in_transaction
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert backups(path) == []
    conn.close()


def test_a_directive_migration_with_crlf_line_endings_is_still_recognised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "rga.sqlite3")
    migrate(conn)
    number, name, sql = fake_migration(DIRECTIVE)
    with_files(monkeypatch, (number, name, sql.replace("\n", "\r\n")))
    assert migrate(conn) == NEXT
    conn.close()


def test_a_directive_migration_that_leaves_a_foreign_key_violation_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rga.sqlite3"
    conn = build_legacy_database(path, 4)
    migrate(conn)
    before = full_dump(conn)
    dangling = (
        "INSERT INTO shots (group_id, seq, x_in, y_in, origin) VALUES (999, 1, 0, 0, 'manual');\n"
    )
    with_files(monkeypatch, fake_migration(DIRECTIVE, dangling))

    with pytest.raises(MigrationError, match="foreign key violation"):
        migrate(conn)

    assert schema_version(conn) == LATEST and not conn.in_transaction
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert full_dump(conn) == before
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_a_directive_migration_cannot_start_inside_a_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rga.sqlite3"
    conn = connect(path)
    migrate(conn)
    insert_rifle(conn)
    with_files(monkeypatch, fake_migration(DIRECTIVE))
    assert conn.in_transaction
    with pytest.raises(MigrationError, match="transaction"):
        migrate(conn)
    assert schema_version(conn) == LATEST
    conn.rollback()
    conn.close()


@pytest.mark.parametrize(
    ("reported", "message"),
    [(1, "could not turn foreign keys off"), (0, "could not turn foreign keys back on")],
)
def test_the_runner_verifies_both_foreign_key_switches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reported: int, message: str
) -> None:
    conn = connect(tmp_path / "rga.sqlite3")
    migrate(conn)
    with_files(monkeypatch, fake_migration(DIRECTIVE))
    monkeypatch.setattr(db_connection, "_foreign_keys", lambda _conn: reported)
    with pytest.raises(MigrationError, match=message):
        migrate(conn)
    monkeypatch.undo()
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_005_starts_with_the_directive_and_only_005_does() -> None:
    first_lines = {
        number: sql.split("\n", 1)[0] for number, _, sql in db_connection._migration_files()
    }
    assert first_lines[5] == DIRECTIVE
    assert [n for n, line in first_lines.items() if line == DIRECTIVE] == [5]


@pytest.mark.parametrize(
    ("line", "expected"),
    [(DIRECTIVE, True), (DIRECTIVE + "\r", True), (" " + DIRECTIVE, False), ("", False)],
)
def test_the_directive_matcher(line: str, expected: bool) -> None:
    assert db_connection._wants_foreign_keys_off(f"{line}\nSELECT 1;") is expected


def test_an_unsuppressed_session_that_still_names_a_suppressor_migrates_as_plain_unsuppressed(
    tmp_path: Path,
) -> None:
    conn = build_legacy_database(tmp_path / "rga.sqlite3", 4)
    conn.execute("UPDATE sessions SET suppressor_id = 1 WHERE id = 1 AND suppressor = 0")
    conn.commit()
    assert conn.execute("SELECT suppressor, suppressor_id FROM sessions WHERE id = 1").fetchone()[
        0:2
    ] == (0, 1)

    migrate(conn)

    group = conn.execute("SELECT suppressor, suppressor_id FROM groups WHERE id = 1").fetchone()
    assert tuple(group) == (0, None)
    conn.close()


def test_a_directive_migration_refuses_to_start_inside_a_transaction_itself(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "rga.sqlite3")
    migrate(conn)
    insert_rifle(conn)
    assert conn.in_transaction
    number, name, sql = fake_migration(DIRECTIVE)
    with pytest.raises(MigrationError, match="foreign keys off, which cannot change"):
        db_connection._apply_with_foreign_keys_off(conn, number, name, sql)
    conn.rollback()
    conn.close()


# ----------------------------------------------------------------------------------- migration 006
# The first stored settings: a `settings` table of JSON documents. No existing data changes.


def _all_rows(conn: sqlite3.Connection, skip: frozenset[str] = frozenset()) -> dict[str, list]:
    tables = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name"
        )
        if r["name"] not in skip
    ]
    return {
        t: [tuple(r) for r in conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid')]  # noqa: S608
        for t in tables
    }


def test_migration_006_is_packaged_and_sets_version_6() -> None:
    names = [name for _, name, _ in db_connection._migration_files()]
    assert "006_settings.sql" in names and LATEST >= 6


def test_migration_006_upgrades_a_v5_database_without_changing_any_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = db_connection._migration_files
    conn = build_legacy_database(tmp_path / "v4.sqlite3", 4, extras=True)
    monkeypatch.setattr(db_connection, "_migration_files", lambda: [f for f in real() if f[0] <= 5])
    migrate(conn)
    assert schema_version(conn) == 5
    before = _all_rows(conn)
    assert before["groups"] and before["shots"]

    monkeypatch.setattr(db_connection, "_migration_files", real)
    migrate(conn)

    assert schema_version(conn) == LATEST and LATEST >= 6
    assert _all_rows(conn, skip=frozenset({"settings"})) == before
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_the_settings_table_holds_one_json_document_per_key(
    migrated_conn: sqlite3.Connection,  # noqa: F811
) -> None:
    columns = {r["name"]: r for r in migrated_conn.execute("PRAGMA table_info(settings)")}
    assert list(columns) == ["key", "value", "updated_at"]
    assert columns["key"]["pk"] == 1 and columns["value"]["notnull"] == 1
    migrated_conn.execute("INSERT INTO settings VALUES ('a', '{\"x\": true}', ?)", (NOW,))
    for sql, params in (
        ("INSERT INTO settings VALUES ('a', '{}', ?)", (NOW,)),
        ("INSERT INTO settings VALUES ('', '{}', ?)", (NOW,)),
        ("INSERT INTO settings VALUES ('b', 'not json', ?)", (NOW,)),
        ("INSERT INTO settings VALUES ('c', NULL, ?)", (NOW,)),
        ("INSERT INTO settings (key, value) VALUES ('d', '{}')", ()),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            migrated_conn.execute(sql, params)


def test_the_settings_row_dataclass_mirrors_its_table(
    migrated_conn: sqlite3.Connection,  # noqa: F811
) -> None:
    columns = [r["name"] for r in migrated_conn.execute("PRAGMA table_info(settings)")]
    assert [f.name for f in fields(repo_settings.SettingRow)] == columns


def test_a_failing_006_would_leave_the_v5_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = db_connection._migration_files
    conn = build_legacy_database(tmp_path / "v4.sqlite3", 4)
    monkeypatch.setattr(db_connection, "_migration_files", lambda: [f for f in real() if f[0] <= 5])
    migrate(conn)
    before = _all_rows(conn)
    broken = [
        *[f for f in real() if f[0] <= 5],
        (6, "006_broken.sql", "CREATE TABLE settings (key TEXT); SELECT * FROM nope;"),
    ]
    monkeypatch.setattr(db_connection, "_migration_files", lambda: broken)

    with pytest.raises(sqlite3.OperationalError):
        migrate(conn)

    assert schema_version(conn) == 5 and _all_rows(conn) == before
    conn.close()
