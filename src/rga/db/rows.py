"""Frozen row dataclasses mirroring the SQLite columns 1:1 (see migrations/001_initial.sql).

Each class has `from_row(sqlite3.Row)`. SQLite integers used as booleans stay `int` (0/1).
`DopeRow` is the result shape of `repo_dope.dope_rows` rather than a table. `SessionRow` mirrors
the dead legacy table left by migration 005 (no code reads it).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from typing import Self


class _Row:
    """Mixin giving a dataclass a `from_row` constructor keyed by column name."""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """Build the dataclass from a row containing every field as a column."""
        return cls(**{f.name: row[f.name] for f in fields(cls)})


@dataclass(frozen=True)
class RifleRow(_Row):
    """Row of `rifles`."""

    id: int
    public_id: str
    name: str
    cartridge: str
    bullet_diameter_in: float
    scope: str | None
    notes: str
    zero_load_id: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class LoadRow(_Row):
    """Row of `loads`; the retired `powder` and `charge_gr` columns are not read."""

    id: int
    public_id: str
    rifle_id: int
    kind: str
    label: str
    maker: str | None
    product: str | None
    bullet_weight_gr: float | None
    bullet_type: str | None
    bullet: str | None
    primer: str | None
    trued_mv_fps: float | None
    notes: str
    created_at: str
    updated_at: str
    brass: str | None
    coal_in: float | None


@dataclass(frozen=True)
class LotRow(_Row):
    """Row of `load_lots`."""

    id: int
    load_id: int
    lot_code: str
    created_at: str


@dataclass(frozen=True)
class SessionRow(_Row):
    """Row of the dead legacy table, kept only so the table mirror test has its row type."""

    id: int
    public_id: str
    rifle_id: int
    date: str
    distance_yd: int
    position: str
    suppressor: int
    temperature_f: float | None
    density_altitude_ft: float | None
    wind_mph: float | None
    wind_clock: int | None
    weather_skipped: int
    notes: str
    created_at: str
    updated_at: str
    suppressor_id: int | None = None


@dataclass(frozen=True)
class SuppressorRow(_Row):
    """Row of `suppressors`."""

    id: int
    name: str
    created_at: str


@dataclass(frozen=True)
class PhotoRow(_Row):
    """Row of `photos`."""

    id: int
    sha256: str
    ext: str
    width_px: int
    height_px: int
    reference_kind: str | None
    reference_json: str | None
    h_px_to_plane_json: str | None
    working_ppi: float | None
    working_w: int | None
    working_h: int | None
    axis_ratio: float | None
    reference_confirmed: int
    created_at: str
    taken_at: str | None
    taken_at_offset: str | None


@dataclass(frozen=True)
class GroupRow(_Row):
    """Row of `groups`; the context (date, distance, position, suppressor, weather) is its own."""

    id: int
    public_id: str
    load_id: int
    lot_id: int
    photo_id: int | None
    roi_json: str | None
    shots_expected: int
    elevation_dialed_mil: float | None
    aim_x_in: float | None
    aim_y_in: float | None
    purpose: str
    zero_id: int | None
    status: str
    notes: str
    finalized_at: str | None
    created_at: str
    updated_at: str
    shot_date: str
    shot_time: str | None
    utc_offset: str | None
    shot_at_source: str
    distance_yd: int
    position: str
    suppressor: int
    suppressor_id: int | None
    temperature_f: float | None
    density_altitude_ft: float | None
    wind_mph: float | None
    wind_clock: int | None
    weather_skipped: int
    legacy_session_id: int | None
    legacy_session_notes: str


@dataclass(frozen=True)
class ShotRow(_Row):
    """Row of `shots`."""

    id: int
    group_id: int
    seq: int
    x_in: float
    y_in: float
    dx_in: float | None
    dy_in: float | None
    dx_mil: float | None
    dy_mil: float | None
    origin: str
    moved_by_user: int
    confidence: float | None


@dataclass(frozen=True)
class RejectionRow(_Row):
    """Row of `detection_rejections`."""

    id: int
    group_id: int
    x_in: float
    y_in: float
    reason: str
    created_at: str


@dataclass(frozen=True)
class ZeroRow(_Row):
    """Row of `zeros`."""

    id: int
    rifle_id: int
    load_id: int
    lot_id: int | None
    mode: str
    state: str
    dialed_up_mil: float | None
    dialed_right_mil: float | None
    zero_es_in: float | None
    zero_shot_count: int | None
    dot_size_in: float | None
    completed_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ZeroCheckRow(_Row):
    """Row of `zero_checks`."""

    id: int
    zero_id: int
    group_id: int
    result: str
    created_at: str


@dataclass(frozen=True)
class DopeRow(_Row):
    """One finalized group with elevation set (query result, not a table)."""

    group_id: int
    shot_date: str
    shot_time: str | None
    distance_yd: int
    elevation_dialed_mil: float
    shot_count: int
