"""T2: dope repository query against the migrated schema (real SQLite, no mocks)."""

from __future__ import annotations

import ast
import inspect
import re
import sqlite3
from pathlib import Path

import pytest

from rga.db import repo_dope
from rga.db.repo_dope import MIN_ZEROING_SHOTS, dope_rows
from rga.db.repo_rifles import create_load, create_rifle, get_or_create_lot
from rga.db.rows import DopeRow
from tests.support.db import migrated_conn  # noqa: F401

MODULE_PATH = Path(inspect.getsourcefile(repo_dope))
SQL_START = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b")
NOW = "2026-10-01T12:00:00.000000Z"


class World:
    """A rifle with two loads and raw-SQL helpers for groups (each with its own context) and shots."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.rifle = create_rifle(conn, name="R1", cartridge="6.5 CM", bullet_diameter_in=0.264)
        self.load = create_load(conn, self.rifle.id, kind="factory", label="A")
        self.other_load = create_load(conn, self.rifle.id, kind="factory", label="B")
        self.lots = {
            self.load.id: get_or_create_lot(conn, self.load.id, "L1").id,
            self.other_load.id: get_or_create_lot(conn, self.other_load.id, "L1").id,
        }

    def context(
        self, distance_yd: int = 100, date: str = "2026-09-01", time: str | None = None
    ) -> dict[str, object]:
        """The context a group is shot in: day, optional time of day, distance."""
        return {"distance_yd": distance_yd, "shot_date": date, "shot_time": time}

    def group(
        self,
        ctx: dict[str, object],
        *,
        load_id: int | None = None,
        elevation: float | None = 1.5,
        status: str = "final",
        purpose: str = "general",
        shots: int = 10,
        expected: int = 10,
    ) -> int:
        load_id = load_id or self.load.id
        n = self.conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
        aim = (5.0, 5.0) if status == "final" else (None, None)
        gid = self.conn.execute(
            "INSERT INTO groups (public_id, load_id, lot_id, shots_expected,"
            " elevation_dialed_mil, aim_x_in, aim_y_in, purpose, status, created_at, updated_at,"
            " shot_date, shot_time, shot_at_source, distance_yd, position, suppressor)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?, 'bench', 0)",
            (
                f"g-{n}",
                load_id,
                self.lots[load_id],
                expected,
                elevation,
                *aim,
                purpose,
                status,
                NOW,
                NOW,
                ctx["shot_date"],
                ctx["shot_time"],
                ctx["distance_yd"],
            ),
        ).lastrowid
        for seq in range(shots):
            self.conn.execute(
                "INSERT INTO shots (group_id, seq, x_in, y_in, origin) VALUES (?, ?, 1.0, 1.0,"
                " 'detected')",
                (gid, seq),
            )
        return gid


@pytest.fixture
def world(migrated_conn):  # noqa: F811
    return World(migrated_conn)


def ids(rows: list[DopeRow]) -> list[int]:
    return [r.group_id for r in rows]


class TestSelection:
    def test_returns_the_documented_row_shape(self, world):
        ctx = world.context(300, "2026-09-15", "07:30:00")
        gid = world.group(ctx, elevation=4.2, shots=7)
        (row,) = dope_rows(world.conn, world.load.id)
        assert isinstance(row, DopeRow)
        assert row == DopeRow(
            group_id=gid,
            shot_date="2026-09-15",
            shot_time="07:30:00",
            distance_yd=300,
            elevation_dialed_mil=4.2,
            shot_count=7,
        )

    def test_no_groups_or_unknown_load_gives_an_empty_list(self, world):
        assert dope_rows(world.conn, world.load.id) == []
        assert dope_rows(world.conn, 999) == []

    def test_only_finalized_groups_qualify(self, world):
        ctx = world.context()
        final = world.group(ctx, status="final")
        world.group(ctx, status="draft")
        assert ids(dope_rows(world.conn, world.load.id)) == [final]

    def test_only_groups_with_elevation_set_qualify(self, world):
        ctx = world.context()
        world.group(ctx, elevation=None)
        with_elev = world.group(ctx, elevation=2.0)
        assert ids(dope_rows(world.conn, world.load.id)) == [with_elev]

    @pytest.mark.parametrize("elevation", [0.0, -0.5, 0.1])
    def test_zero_and_negative_elevation_are_real_values(self, world, elevation):
        gid = world.group(world.context(), elevation=elevation)
        (row,) = dope_rows(world.conn, world.load.id)
        assert (row.group_id, row.elevation_dialed_mil) == (gid, elevation)

    def test_only_the_requested_load_is_returned(self, world):
        ctx = world.context()
        mine = world.group(ctx, load_id=world.load.id)
        theirs = world.group(ctx, load_id=world.other_load.id)
        assert ids(dope_rows(world.conn, world.load.id)) == [mine]
        assert ids(dope_rows(world.conn, world.other_load.id)) == [theirs]

    def test_shot_count_is_the_number_of_stored_shots_not_the_expected_count(self, world):
        ctx = world.context()
        world.group(ctx, shots=6, expected=10)
        world.group(ctx, shots=0, expected=5)
        assert sorted(r.shot_count for r in dope_rows(world.conn, world.load.id)) == [0, 6]

    def test_several_groups_in_one_context_each_get_a_row(self, world):
        ctx = world.context()
        a, b = world.group(ctx), world.group(ctx)
        assert ids(dope_rows(world.conn, world.load.id)) == [a, b]


class TestOrdering:
    def test_by_distance_numerically_then_date_then_group_id(self, world):
        s1000 = world.context(1000, "2026-01-01")
        s200_late = world.context(200, "2026-09-30")
        s200_early = world.context(200, "2026-03-01")
        s100 = world.context(100, "2026-12-31")
        g1000 = world.group(s1000)
        g200_late = world.group(s200_late)
        g200_early_a = world.group(s200_early)
        g200_early_b = world.group(s200_early)
        g100 = world.group(s100)
        assert ids(dope_rows(world.conn, world.load.id)) == [
            g100,
            g200_early_a,
            g200_early_b,
            g200_late,
            g1000,
        ]

    def test_date_orders_within_a_distance_even_when_ids_disagree(self, world):
        late = world.group(world.context(300, "2026-10-01"))
        early = world.group(world.context(300, "2026-02-01"))
        assert ids(dope_rows(world.conn, world.load.id)) == [early, late]

    def test_time_of_day_orders_within_a_day_and_an_untimed_group_comes_first(self, world):
        evening = world.group(world.context(300, "2026-10-01", "18:00:00"))
        morning = world.group(world.context(300, "2026-10-01", "07:15:00"))
        untimed = world.group(world.context(300, "2026-10-01"))
        next_day_untimed = world.group(world.context(300, "2026-10-02"))
        assert ids(dope_rows(world.conn, world.load.id)) == [
            untimed,
            morning,
            evening,
            next_day_untimed,
        ]

    def test_the_distance_is_the_groups_own_so_one_day_can_hold_several(self, world):
        far = world.group(world.context(600, "2026-10-01"))
        near = world.group(world.context(100, "2026-10-01"))
        assert ids(dope_rows(world.conn, world.load.id)) == [near, far]


class TestShortZeroingGroupsAreExcluded:
    """B-15/B-17: zeroing-workflow groups never yield dope rows unless they are step2/confirm of 10+ shots."""

    @pytest.mark.parametrize("purpose", ["zero_step2", "zero_confirm"])
    @pytest.mark.parametrize("shots", [0, 1, 2, 5, 9])
    def test_short_step2_and_confirm_groups_are_excluded(self, world, purpose, shots):
        world.group(world.context(), purpose=purpose, shots=shots)
        assert dope_rows(world.conn, world.load.id) == []

    @pytest.mark.parametrize("purpose", ["zero_step2", "zero_confirm"])
    @pytest.mark.parametrize("shots", [10, 11, 20])
    def test_step2_and_confirm_groups_of_ten_or_more_are_kept(self, world, purpose, shots):
        gid = world.group(world.context(), purpose=purpose, shots=shots)
        assert ids(dope_rows(world.conn, world.load.id)) == [gid]

    @pytest.mark.parametrize("shots", [0, 1, 3, 9, 10, 25])
    def test_general_groups_are_not_filtered_by_size(self, world, shots):
        gid = world.group(world.context(), purpose="general", shots=shots)
        assert ids(dope_rows(world.conn, world.load.id)) == [gid]

    @pytest.mark.parametrize("purpose", ["zero_step1", "zero_check"])
    @pytest.mark.parametrize("shots", [0, 1, 2, 3, 10, 30])
    def test_step1_and_zero_check_groups_are_always_excluded_whatever_their_size(
        self, world, purpose, shots
    ):
        world.group(world.context(), purpose=purpose, shots=shots)
        assert dope_rows(world.conn, world.load.id) == []

    def test_the_exclusion_is_per_group_and_leaves_neighbours_alone(self, world):
        ctx = world.context()
        short = world.group(ctx, purpose="zero_step2", shots=4)
        full = world.group(ctx, purpose="zero_step2", shots=10)
        general = world.group(ctx, purpose="general", shots=3)
        step1 = world.group(ctx, purpose="zero_step1", shots=2)
        check = world.group(ctx, purpose="zero_check", shots=1)
        assert {short, step1, check}.isdisjoint(ids(dope_rows(world.conn, world.load.id)))
        assert ids(dope_rows(world.conn, world.load.id)) == [full, general]

    def test_the_bar_cannot_be_lowered_by_the_caller(self, world):
        world.group(world.context(), purpose="zero_confirm", shots=5)
        assert list(inspect.signature(dope_rows).parameters) == ["conn", "load_id"]
        for override in (0, 1, 5):
            with pytest.raises(TypeError):
                dope_rows(world.conn, world.load.id, min_zeroing_shots=override)
            with pytest.raises(TypeError):
                dope_rows(world.conn, world.load.id, override)
        assert dope_rows(world.conn, world.load.id) == []

    def test_the_threshold_is_the_domain_step2_minimum(self):
        from rga.domain.zeroing import STEP2_MIN_SHOTS

        assert MIN_ZEROING_SHOTS == STEP2_MIN_SHOTS == 10

    def test_the_purposes_named_in_the_sql_are_the_domain_zeroing_purposes(self):
        from rga.domain.types import GroupPurpose

        quoted = set(re.findall(r"'(zero_\w+)'", repo_dope._DOPE_ROWS))
        zeroing = {p.value for p in GroupPurpose if p is not GroupPurpose.GENERAL}
        assert quoted == zeroing
        assert quoted == {"zero_step1", "zero_step2", "zero_confirm", "zero_check"}


class TestReadOnly:
    def test_the_query_writes_nothing_and_opens_no_transaction(self, world):
        world.group(world.context())
        world.conn.commit()
        changes = world.conn.total_changes
        dope_rows(world.conn, world.load.id)
        assert world.conn.total_changes == changes
        assert not world.conn.in_transaction

    def test_module_source_never_writes_or_commits(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        called = {
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert not called & {"commit", "rollback", "executescript", "executemany"}
        assert all(
            not s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in _sql()
        )


def _sql() -> list[str]:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and SQL_START.match(n.value)
    ]


def bind(sql: str) -> tuple[int, ...]:
    return (1,) * sql.count("?")


class TestDataSchemaContract:
    def test_the_module_has_one_constant_sql_statement(self):
        assert _sql() == [repo_dope._DOPE_ROWS]
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        interpolated = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.JoinedStr)
            and SQL_START.match("".join(v.value for v in n.values if isinstance(v, ast.Constant)))
        ]
        assert interpolated == []

    def test_the_statement_passes_explain_against_the_migrated_schema(self, migrated_conn):  # noqa: F811
        sql = repo_dope._DOPE_ROWS
        migrated_conn.execute("EXPLAIN " + sql, bind(sql)).fetchall()

    def test_the_plan_uses_ix_groups_load_status_on_both_columns(self, migrated_conn):  # noqa: F811
        sql = repo_dope._DOPE_ROWS
        plan = " | ".join(
            r["detail"] for r in migrated_conn.execute("EXPLAIN QUERY PLAN " + sql, bind(sql))
        )
        assert "USING INDEX ix_groups_load_status (load_id=? AND status=?)" in plan
        assert "SCAN groups" not in plan

    def test_the_shot_count_uses_the_shots_index_and_nothing_reads_sessions(
        self,
        migrated_conn,  # noqa: F811
    ):
        sql = repo_dope._DOPE_ROWS
        plan = " | ".join(
            r["detail"] for r in migrated_conn.execute("EXPLAIN QUERY PLAN " + sql, bind(sql))
        )
        assert re.search(r"SEARCH shots USING (COVERING )?INDEX sqlite_autoindex_shots_1", plan)
        assert "SCAN shots" not in plan
        assert "sessions" not in sql and "session_id" not in sql

    def test_index_columns_the_plan_relies_on_exist_in_the_schema(self, migrated_conn):  # noqa: F811
        cols = [
            c["name"] for c in migrated_conn.execute("PRAGMA index_info(ix_groups_load_status)")
        ]
        assert cols == ["load_id", "status"]
        shots_unique = [
            [c["name"] for c in migrated_conn.execute(f"PRAGMA index_info({i['name']})")]
            for i in migrated_conn.execute("PRAGMA index_list(shots)")
            if i["unique"]
        ]
        assert ["group_id", "seq"] in shots_unique

    def test_selected_columns_are_exactly_the_dope_row_fields_in_order(self, migrated_conn):  # noqa: F811
        cursor = migrated_conn.execute(repo_dope._DOPE_ROWS, bind(repo_dope._DOPE_ROWS))
        assert [d[0] for d in cursor.description] == list(DopeRow.__dataclass_fields__)

    def test_the_status_literal_is_a_valid_group_status(self):
        from rga.domain.types import GroupStatus

        assert (
            re.search(r"status = '(\w+)'", repo_dope._DOPE_ROWS).group(1) == GroupStatus.FINAL.value
        )

    def test_rows_feed_the_api_dope_row_model(self, world):
        from rga.web.schemas import DopeOut, DopeRowOut

        world.group(world.context(400, "2026-09-20", "09:05:00"), elevation=5.0, shots=10)
        (row,) = dope_rows(world.conn, world.load.id)
        out = DopeOut(
            load_id=world.load.id,
            rows=[
                DopeRowOut(
                    group_id=row.group_id,
                    shot_date=row.shot_date,
                    shot_time=row.shot_time,
                    distance_yd=row.distance_yd,
                    elevation_dialed_mil=row.elevation_dialed_mil,
                    center_dy_mil=0.2,
                    centered_elevation_mil=row.elevation_dialed_mil - 0.2,
                    shots=row.shot_count,
                    includes_estimated=False,
                )
            ],
        )
        assert (out.rows[0].distance_yd, out.rows[0].shots) == (400, 10)
        assert (out.rows[0].shot_date.isoformat(), out.rows[0].shot_time) == (
            "2026-09-20",
            "09:05:00",
        )
