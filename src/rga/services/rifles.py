"""Rifle, load and lot service: validation on top of `repo_rifles`, plus rifle summaries.

Public functions that write commit; errors are `RifleError` subclasses carrying a stable
snake_case `code` (`*_not_found` maps to 404, `invalid_*` to 422, anything else to 409).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from rga.db import repo_groups, repo_rifles
from rga.db.rows import LoadRow, LotRow, RifleRow, SuppressorRow
from rga.domain.calibers import CALIBERS, diameter_for
from rga.domain.calibers import DIAMETER_CHOICES as DIAMETER_CHOICES  # the rifle forms' list
from rga.services.group_records import DayGroups, rifle_group_days

FACTORY_FIELDS = ("maker", "product", "bullet_weight_gr", "bullet_type")
HANDLOAD_FIELDS = ("bullet_weight_gr", "bullet", "primer", "brass", "coal_in")


class RifleError(Exception):
    """Base error with a stable snake_case `code` and a user-facing message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RifleNotFound(RifleError):
    """The rifle id does not exist."""

    def __init__(self, rifle_id: int) -> None:
        super().__init__("rifle_not_found", f"rifle {rifle_id} does not exist")


class LoadNotFound(RifleError):
    """The load id does not exist."""

    def __init__(self, load_id: int) -> None:
        super().__init__("load_not_found", f"load {load_id} does not exist")


class ZeroLoadNotInRifle(RifleError):
    """The chosen zero load does not exist or belongs to another rifle."""

    def __init__(self, rifle_id: int, load_id: int | None) -> None:
        super().__init__("load_not_in_rifle", f"load {load_id} is not a load of rifle {rifle_id}")


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run the block in one transaction holding the write lock; commit on success, roll back on error.

    Inside a transaction the caller already opened, the block runs in a savepoint instead.
    """
    nested = conn.in_transaction
    conn.execute("SAVEPOINT rifle_service" if nested else "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if nested:
            conn.execute("ROLLBACK TO rifle_service")
            conn.execute("RELEASE rifle_service")
        else:
            conn.rollback()
        raise
    if nested:
        conn.execute("RELEASE rifle_service")
    else:
        conn.commit()


def list_calibers() -> list[tuple[str, float]]:
    """The caliber table as (cartridge, bullet diameter in inches) pairs, in table order."""
    return list(CALIBERS.items())


def _require_rifle(conn: sqlite3.Connection, rifle_id: int) -> RifleRow:
    rifle = repo_rifles.get_rifle(conn, rifle_id)
    if rifle is None:
        raise RifleNotFound(rifle_id)
    return rifle


def _require_load(conn: sqlite3.Connection, load_id: int) -> LoadRow:
    load = repo_rifles.get_load(conn, load_id)
    if load is None:
        raise LoadNotFound(load_id)
    return load


def list_rifles(conn: sqlite3.Connection) -> list[RifleRow]:
    """All rifles ordered by name."""
    return repo_rifles.list_rifles(conn)


def get_rifle(conn: sqlite3.Connection, rifle_id: int) -> RifleRow:
    """The rifle, or raises `RifleNotFound`."""
    return _require_rifle(conn, rifle_id)


def rifle_with_loads(conn: sqlite3.Connection, rifle_id: int) -> tuple[RifleRow, list[LoadRow]]:
    """The rifle and its loads in creation order; raises `RifleNotFound`."""
    return _require_rifle(conn, rifle_id), repo_rifles.list_loads(conn, rifle_id)


def create_rifle(
    conn: sqlite3.Connection,
    *,
    name: str,
    cartridge: str,
    bullet_diameter_in: float | None = None,
    scope: str | None = None,
    notes: str = "",
) -> RifleRow:
    """Create a rifle; a missing bullet diameter is taken from the caliber table.

    Raises `RifleError` `invalid_cartridge` when no diameter is given and the cartridge is not in
    the table.
    """
    if bullet_diameter_in is None:
        bullet_diameter_in = diameter_for(cartridge)
        if bullet_diameter_in is None:
            raise RifleError(
                "invalid_cartridge",
                f"unknown cartridge {cartridge!r}: enter the bullet diameter in inches",
            )
    with _write_transaction(conn):
        return repo_rifles.create_rifle(
            conn,
            name=name,
            cartridge=cartridge,
            bullet_diameter_in=bullet_diameter_in,
            scope=scope,
            notes=notes,
        )


def update_rifle(conn: sqlite3.Connection, rifle_id: int, changes: Mapping[str, Any]) -> RifleRow:
    """Apply the given rifle fields (only keys present are changed); raises `RifleNotFound`."""
    with _write_transaction(conn):
        _require_rifle(conn, rifle_id)
        updated = repo_rifles.update_rifle(conn, rifle_id, **changes)
    if updated is None:
        raise RifleNotFound(rifle_id)
    return updated


def set_zero_load(conn: sqlite3.Connection, rifle_id: int, load_id: int | None) -> RifleRow:
    """Set or clear (None) the rifle's zero load.

    Raises `RifleNotFound`, or `ZeroLoadNotInRifle` when the load is missing or belongs to another
    rifle.
    """
    with _write_transaction(conn):
        _require_rifle(conn, rifle_id)
        try:
            rifle = repo_rifles.set_zero_load(conn, rifle_id, load_id)
        except repo_rifles.LoadNotInRifle:
            raise ZeroLoadNotInRifle(rifle_id, load_id) from None
    if rifle is None:
        raise RifleNotFound(rifle_id)
    return rifle


def list_loads(conn: sqlite3.Connection, rifle_id: int) -> list[LoadRow]:
    """The rifle's loads in creation order; raises `RifleNotFound`."""
    _require_rifle(conn, rifle_id)
    return repo_rifles.list_loads(conn, rifle_id)


def _check_kind_fields(kind: str, fields: Mapping[str, Any]) -> None:
    own = FACTORY_FIELDS if kind == "factory" else HANDLOAD_FIELDS
    foreign = {*FACTORY_FIELDS, *HANDLOAD_FIELDS} - set(own)
    used = sorted(name for name in foreign if fields.get(name) is not None)
    if used:
        raise RifleError(
            "invalid_load_fields", f"fields not allowed for a {kind} load: {', '.join(used)}"
        )


def create_load(conn: sqlite3.Connection, rifle_id: int, fields: Mapping[str, Any]) -> LoadRow:
    """Create a load under the rifle from `kind`, `label` and the kind's fields.

    Raises `RifleNotFound`, or `RifleError` `invalid_load_fields` for fields of the other kind.
    """
    _check_kind_fields(str(fields["kind"]), fields)
    with _write_transaction(conn):
        _require_rifle(conn, rifle_id)
        return repo_rifles.create_load(conn, rifle_id, **fields)


def update_load(conn: sqlite3.Connection, load_id: int, changes: Mapping[str, Any]) -> LoadRow:
    """Apply the given load fields; `kind` cannot change.

    Raises `LoadNotFound`, or `RifleError` `invalid_load_fields` for fields of the other kind.
    """
    with _write_transaction(conn):
        load = _require_load(conn, load_id)
        _check_kind_fields(load.kind, changes)
        updated = repo_rifles.update_load(conn, load_id, **changes)
    if updated is None:
        raise LoadNotFound(load_id)
    return updated


def list_lots(conn: sqlite3.Connection, load_id: int) -> list[LotRow]:
    """The load's lots in creation order; raises `LoadNotFound`."""
    _require_load(conn, load_id)
    return repo_rifles.list_lots(conn, load_id)


def get_or_create_lot(conn: sqlite3.Connection, load_id: int, lot_code: str) -> tuple[LotRow, bool]:
    """The lot with this code under the load, creating it when absent; returns (lot, created)."""
    with _write_transaction(conn):
        _require_load(conn, load_id)
        existing = {lot.lot_code for lot in repo_rifles.list_lots(conn, load_id)}
        lot = repo_rifles.get_or_create_lot(conn, load_id, lot_code)
    return lot, lot_code not in existing


@dataclass(frozen=True)
class RifleSummary:
    """A rifle with its load count and zero-load label for lists and the "Add new group" dialog."""

    rifle: RifleRow
    load_count: int
    zero_load_label: str | None


@dataclass(frozen=True)
class LoadWithLots:
    """A load with its lots for the rifle page."""

    load: LoadRow
    lots: list[LotRow]


@dataclass(frozen=True)
class RiflePage:
    """What the rifle page shows: the rifle, its loads with lots, its groups by day (newest day
    first) and the saved suppressors for the new-group form.
    """

    rifle: RifleRow
    loads: list[LoadWithLots]
    days: list[DayGroups]
    suppressors: list[SuppressorRow]


def rifle_page_data(conn: sqlite3.Connection, rifle_id: int) -> RiflePage:
    """Rifle, loads with lots, groups by day and suppressors for the detail page; raises
    `RifleNotFound`.
    """
    rifle, loads = rifle_with_loads(conn, rifle_id)
    return RiflePage(
        rifle,
        [LoadWithLots(load, repo_rifles.list_lots(conn, load.id)) for load in loads],
        rifle_group_days(conn, rifle_id),
        repo_groups.list_suppressors(conn),
    )


def rifle_summaries(conn: sqlite3.Connection) -> list[RifleSummary]:
    """Every rifle with its load count and zero-load label."""
    summaries = []
    for rifle in repo_rifles.list_rifles(conn):
        loads = repo_rifles.list_loads(conn, rifle.id)
        zero = next((load.label for load in loads if load.id == rifle.zero_load_id), None)
        summaries.append(RifleSummary(rifle, len(loads), zero))
    return summaries
