"""Dope table service: a load's recorded elevations with each group's center folded in.

`repo_dope.dope_rows` decides which groups qualify (finalized, elevation recorded, zeroing-workflow
groups excluded under B-15/B-17); this module never re-implements that selection. For each row it
adds the group's center height in mil and the centered elevation `dialed - center_dy`.
Groups whose point of impact is withheld (`SizeOnlyStats`) never yield a center: a row that
somehow reaches this module for one is dropped.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from rga.db import repo_dope, repo_rifles
from rga.db.rows import LoadRow, RifleRow
from rga.domain.types import GroupStats
from rga.services import groups as group_service
from rga.services.rifles import LoadNotFound, RifleError

__all__ = [
    "DopeLine",
    "DopeTable",
    "LoadChoice",
    "LoadNotFound",
    "LoadRow",
    "RifleError",
    "RifleRow",
    "dope_table",
    "load_choices",
]


@dataclass(frozen=True)
class DopeLine:
    """One group's contribution to the dope table."""

    group_id: int
    shot_date: str
    shot_time: str | None
    distance_yd: int
    elevation_dialed_mil: float
    center_dy_mil: float
    centered_elevation_mil: float
    shots: int
    includes_estimated: bool


@dataclass(frozen=True)
class DopeTable:
    """A load, its rifle and its dope lines ordered by distance, then shot date and time, then group id."""

    load: LoadRow
    rifle: RifleRow
    lines: list[DopeLine]


@dataclass(frozen=True)
class LoadChoice:
    """A load with its rifle's name, for the page's load picker."""

    rifle_name: str
    load: LoadRow


def dope_table(conn: sqlite3.Connection, load_id: int) -> DopeTable:
    """The dope table of `load_id`; raises `LoadNotFound` for an unknown load."""
    load = repo_rifles.get_load(conn, load_id)
    if load is None:
        raise LoadNotFound(load_id)
    rifle = repo_rifles.get_rifle(conn, load.rifle_id)
    if rifle is None:
        raise LoadNotFound(load_id)
    lines: list[DopeLine] = []
    for row in repo_dope.dope_rows(conn, load_id):
        bundle = group_service.load_group(conn, row.group_id)
        stats = bundle.stats
        if not isinstance(stats, GroupStats):
            continue
        lines.append(
            DopeLine(
                group_id=row.group_id,
                shot_date=row.shot_date,
                shot_time=row.shot_time,
                distance_yd=row.distance_yd,
                elevation_dialed_mil=row.elevation_dialed_mil,
                center_dy_mil=stats.mean_dy_mil,
                centered_elevation_mil=row.elevation_dialed_mil - stats.mean_dy_mil,
                shots=stats.n,
                includes_estimated=stats.includes_estimated,
            )
        )
    return DopeTable(load, rifle, lines)


def load_choices(conn: sqlite3.Connection) -> list[LoadChoice]:
    """Every load of every rifle (rifles by name), for the picker shown without a `load_id`."""
    choices: list[LoadChoice] = []
    for rifle in repo_rifles.list_rifles(conn):
        choices.extend(
            LoadChoice(rifle.name, load) for load in repo_rifles.list_loads(conn, rifle.id)
        )
    return choices
