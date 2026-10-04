"""Seed helpers that build rifles, loads, photos and groups through the real repos.

All helpers take the connection first and commit, so seeded data is visible to other connections
(for example the app under test). `seed_group` builds a group from aim-relative offsets
(inches, y up), stores the shots through `services.groups.save_review` and, by default,
finalizes it, so every derived value comes from the same code the app uses.

A group carries its own context. `seed_context` describes one (rifle, date, distance, position,
suppressor, weather, notes) without touching the database; `seed_group(context=ctx)` spreads it
onto the group.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from rga.db import repo_groups, repo_rifles
from rga.db.rows import GroupRow, LoadRow, PhotoRow, RifleRow
from rga.domain.types import Offset, PlanePoint, Shot, ShotOrigin, ShotTimeSource
from rga.services import groups as group_service

DEFAULT_AIM = PlanePoint(4.25, 5.5)


def seed_rifle(
    conn: sqlite3.Connection,
    *,
    name: str = "Test Rifle",
    cartridge: str = "6.5 Creedmoor",
    bullet_diameter_in: float = 0.264,
    scope: str | None = None,
    notes: str = "",
) -> RifleRow:
    """Create a rifle (default 6.5 Creedmoor, 0.264 in bullet) and commit."""
    with conn:
        return repo_rifles.create_rifle(
            conn,
            name=name,
            cartridge=cartridge,
            bullet_diameter_in=bullet_diameter_in,
            scope=scope,
            notes=notes,
        )


def seed_load(
    conn: sqlite3.Connection,
    rifle: RifleRow | int,
    *,
    label: str = "Factory 140 ELD-M",
    kind: str = "factory",
    notes: str = "",
    **fields: object,
) -> LoadRow:
    """Create a load under `rifle` (a row or id) and commit; extra fields go to `create_load`."""
    rifle_id = rifle if isinstance(rifle, int) else rifle.id
    with conn:
        return repo_rifles.create_load(
            conn, rifle_id, kind=kind, label=label, notes=notes, **fields
        )


@dataclass(frozen=True)
class SeedContext:
    """The context a seeded group carries (not a database row)."""

    rifle_id: int
    distance_yd: int = 100
    date: str = "2026-09-30"
    time: str | None = None
    utc_offset: str | None = None
    position: str = "bench"
    suppressor: bool = False
    suppressor_id: int | None = None
    notes: str = ""
    temperature_f: float | None = None
    density_altitude_ft: float | None = None
    wind_mph: float | None = None
    wind_clock: int | None = None
    weather_skipped: bool = False

    def group_fields(self) -> dict[str, object]:
        """The `create_group` keyword arguments this context stands for."""
        return {
            "shot_date": self.date,
            "shot_time": self.time,
            "utc_offset": self.utc_offset,
            "distance_yd": self.distance_yd,
            "position": self.position,
            "suppressor": self.suppressor,
            "suppressor_id": self.suppressor_id,
            "temperature_f": self.temperature_f,
            "density_altitude_ft": self.density_altitude_ft,
            "wind_mph": self.wind_mph,
            "wind_clock": self.wind_clock,
            "weather_skipped": self.weather_skipped,
            "legacy_session_notes": self.notes,
        }


def seed_context(
    rifle: RifleRow | int,
    *,
    distance_yd: int = 100,
    date: str = "2026-09-30",
    position: str = "bench",
    suppressor: bool = False,
    notes: str = "",
    **fields: object,
) -> SeedContext:
    """Describe a group context for `rifle` (a row or id); `fields` are the other
    `SeedContext` fields (time, utc_offset, suppressor_id, weather)."""
    rifle_id = rifle if isinstance(rifle, int) else rifle.id
    return SeedContext(
        rifle_id,
        distance_yd=distance_yd,
        date=date,
        position=position,
        suppressor=suppressor,
        notes=notes,
        **fields,  # type: ignore[arg-type]
    )


def seed_photo(
    conn: sqlite3.Connection, *, confirmed: bool = True, sha256: str | None = None
) -> PhotoRow:
    """Create a photo, with a confirmed circle reference by default, and commit."""
    count = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    digest = sha256 or f"{count + 1:064x}"
    with conn:
        photo = repo_groups.create_photo(
            conn, sha256=digest, ext="jpg", width_px=4000, height_px=3000
        )
        if confirmed:
            photo = repo_groups.update_photo_reference(
                conn,
                photo.id,
                reference_kind="circle_1in",
                reference_json="{}",
                h_px_to_plane_json="[[1,0,0],[0,1,0],[0,0,1]]",
                working_ppi=200.0,
                working_w=1600,
                working_h=1200,
                axis_ratio=1.0,
                reference_confirmed=True,
            )
    return photo


def seed_group(
    conn: sqlite3.Connection,
    *,
    load: LoadRow | int,
    offsets: Sequence[Offset],
    context: SeedContext | None = None,
    origins: Sequence[ShotOrigin] | None = None,
    lot_code: str = "LOT-1",
    aim: PlanePoint | None = DEFAULT_AIM,
    shots_expected: int | None = None,
    purpose: str = "general",
    finalize: bool = True,
    elevation_dialed_mil: float | None = None,
    notes: str = "",
    photo: PhotoRow | int | None = None,
    roi_json: str | None = None,
) -> GroupRow:
    """Create a group whose shots sit at `offsets` from `aim` and return the stored row.

    The group's context comes from `context`; without one it is the default
    `SeedContext` of the load's rifle (100 yd, bench, 2026-09-30).

    `offsets` are aim-relative inches with y up (as `domain.units.offset_from_aim` returns), and
    are turned into plane positions by the inverse of that single flip. `origins` defaults to all
    detected. `shots_expected` defaults to `len(offsets)`. With `finalize=True` (the default) the
    group is finalized through the service, which needs an `aim`; a finalize guard failure raises.
    Pass `finalize=False` for a draft (use `shots_expected` different from `len(offsets)` for a
    partial one).
    """
    load_id = load if isinstance(load, int) else load.id
    ctx = context
    if ctx is None:
        owner = conn.execute("SELECT rifle_id FROM loads WHERE id = ?", (load_id,)).fetchone()
        ctx = SeedContext(int(owner["rifle_id"]))
    photo_id = photo if photo is None or isinstance(photo, int) else photo.id
    kinds = list(origins) if origins is not None else [ShotOrigin.DETECTED] * len(offsets)
    if len(kinds) != len(offsets):
        raise ValueError("origins must match offsets one to one")
    with conn:
        lot = repo_rifles.get_or_create_lot(conn, load_id, lot_code)
        group = repo_groups.create_group(
            conn,
            **ctx.group_fields(),
            shot_at_source=ShotTimeSource.MANUAL,
            load_id=load_id,
            lot_id=lot.id,
            shots_expected=shots_expected or max(len(offsets), 1),
            photo_id=photo_id,
            roi_json=roi_json,
            elevation_dialed_mil=elevation_dialed_mil,
            purpose=purpose,
            notes=notes,
        )
    plane = aim or DEFAULT_AIM
    shots = [
        Shot(PlanePoint(plane.x_in + o.dx_in, plane.y_in - o.dy_in), origin)
        for o, origin in zip(offsets, kinds, strict=True)
    ]
    group_service.save_review(
        conn, group.id, aim, shots, [], shots_expected or max(len(offsets), 1)
    )
    if finalize:
        group_service.finalize(conn, group.id)
    seeded = repo_groups.get_group(conn, group.id)
    assert seeded is not None
    return seeded
