"""T2 contract: every domain enum / API literal is tied to its DB CHECK list, read from both sides."""

from __future__ import annotations

import re
import sqlite3
from enum import StrEnum
from typing import get_args

import pytest

from rga.domain import types as domain
from rga.web import schemas
from tests.integration.test_schema import Seed
from tests.support.db import migrated_conn  # noqa: F401

PAIRS: dict[tuple[str, str], tuple[str, ...]] = {
    ("loads", "kind"): tuple(m.value for m in domain.LoadKind),
    ("sessions", "position"): tuple(m.value for m in domain.Position),
    ("groups", "position"): tuple(m.value for m in domain.Position),
    ("groups", "shot_at_source"): tuple(m.value for m in domain.ShotTimeSource),
    ("photos", "reference_kind"): tuple(m.value for m in domain.ReferenceKind),
    ("zeros", "state"): tuple(m.value for m in domain.ZeroState),
    ("zeros", "mode"): get_args(schemas.ZeroMode),
    ("groups", "purpose"): tuple(m.value for m in domain.GroupPurpose),
    ("groups", "status"): tuple(m.value for m in domain.GroupStatus),
    ("shots", "origin"): tuple(m.value for m in domain.ShotOrigin),
    ("detection_rejections", "reason"): tuple(m.value for m in domain.RejectionReason),
    ("zero_checks", "result"): get_args(schemas.ZeroCheckOutcome),
}
PAIR_IDS = [f"{t}.{c}" for t, c in PAIRS]

_QUOTED_IN = re.compile(r"\b(\w+)\s+IN\s*\(\s*'")
_QUOTED_EQ = re.compile(r"\b(\w+)\s*=\s*'")


def table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return row.fetchone()["sql"]


def check_values(conn: sqlite3.Connection, table: str, column: str) -> set[str]:
    """Allowed text values for `table.column` as written in the DDL (IN-list or equality)."""
    sql = table_sql(conn, table)
    in_list = re.search(rf"\b{column}\s+IN\s*\(([^)]*)\)", sql)
    if in_list:
        return set(re.findall(r"'([^']*)'", in_list.group(1)))
    equality = re.search(rf"\b{column}\s*=\s*'([^']*)'", sql)
    assert equality, f"{table}.{column} has no text CHECK in the DDL"
    return {equality.group(1)}


def test_the_pair_table_covers_every_text_check_in_the_ddl(
    migrated_conn: sqlite3.Connection,  # noqa: F811
) -> None:
    found: set[tuple[str, str]] = set()
    names = [
        r["name"]
        for r in migrated_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for table in names:
        sql = table_sql(migrated_conn, table)
        found.update((table, column) for column in _QUOTED_IN.findall(sql))
        found.update((table, column) for column in _QUOTED_EQ.findall(sql))
    assert found == set(PAIRS)


@pytest.mark.parametrize(("table", "column"), list(PAIRS), ids=PAIR_IDS)
def test_ddl_check_list_equals_the_domain_values(
    migrated_conn: sqlite3.Connection,  # noqa: F811
    table: str,
    column: str,
) -> None:
    assert check_values(migrated_conn, table, column) == set(PAIRS[(table, column)])


@pytest.mark.parametrize(("table", "column"), list(PAIRS), ids=PAIR_IDS)
def test_every_domain_value_is_accepted_and_a_non_member_is_rejected(
    migrated_conn: sqlite3.Connection,  # noqa: F811
    table: str,
    column: str,
) -> None:
    seed = Seed(migrated_conn)
    aim = {"aim_x_in": 0.0, "aim_y_in": 0.0} if (table, column) == ("groups", "status") else {}
    for value in PAIRS[(table, column)]:
        seed.insert(table, **{column: value, **aim})
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        seed.insert(table, **{column: "not_a_member", **aim})


def test_pairs_use_real_enums_not_empty_sets() -> None:
    for enum_cls in (
        domain.LoadKind,
        domain.Position,
        domain.ShotTimeSource,
        domain.ReferenceKind,
        domain.ZeroState,
        domain.GroupPurpose,
        domain.GroupStatus,
        domain.ShotOrigin,
        domain.RejectionReason,
    ):
        assert issubclass(enum_cls, StrEnum)
        assert len(enum_cls) >= 1
    assert get_args(schemas.ZeroMode) and get_args(schemas.ZeroCheckOutcome)
