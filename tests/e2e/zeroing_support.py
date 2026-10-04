"""Shared set-up for the zeroing journeys (AC-5, AC-6).

Groups are created through the zeroing page like a shooter does; their shots are stored through
the group service, standing in for the photo import and review journeys that AC-1 to AC-4 cover.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from rga.domain.types import Offset, PlanePoint, Shot, ShotOrigin
from rga.services import groups as group_service
from tests.e2e.pages import App
from tests.support.seed import DEFAULT_AIM

LOT = "LOT-1"
DIGIT_BEARING_CORRECTION = re.compile(
    r"(\b(?:dial|up|down|left|right|high|low)\b\s*\d)|(\d[\d.]*\s*(?:mil|clicks?)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ZeroWorld:
    rifle_id: int
    load_id: int


def ring(cx_in: float, cy_in: float, n: int = 10, radius_in: float = 0.4) -> list[Offset]:
    """`n` shots on a circle centred (cx, cy) from the aim, y up."""
    return [
        Offset(
            cx_in + radius_in * math.cos(2 * math.pi * i / n),
            cy_in + radius_in * math.sin(2 * math.pi * i / n),
        )
        for i in range(n)
    ]


def zero_world(app: App) -> ZeroWorld:
    """A rifle and a load, both made through the UI."""
    rifle_id = app.create_rifle()
    return ZeroWorld(rifle_id, app.create_load(rifle_id))


def shoot(
    app: App,
    group_id: int,
    offsets: list[Offset],
    *,
    finalize: bool = True,
    aim: PlanePoint = DEFAULT_AIM,
) -> None:
    """Store `offsets` (inches from the aim, y up) as the group's shots, then finalize it."""
    shots = [
        Shot(PlanePoint(aim.x_in + o.dx_in, aim.y_in - o.dy_in), ShotOrigin.DETECTED)
        for o in offsets
    ]
    with app.seeding() as conn:
        row = conn.execute("SELECT shots_expected FROM groups WHERE id = ?", (group_id,)).fetchone()
        group_service.save_review(conn, group_id, aim, shots, [], row["shots_expected"])
        if finalize:
            group_service.finalize(conn, group_id)


def page_text(app: App) -> str:
    """What the shooter reads in `<main>`, minus fixed text that is not a point of impact: the
    dialed card's instruction sentence and the size tile's MOA and mil of the extreme spread."""
    text = re.sub(r"Enter what you actually dialed[^\n]*", "", app.main.inner_text())
    return re.sub(r"[\d.]+ MOA(?: \u00b7 [\d.]+ mil)?", "", text)


def complete_zero(app: App, world: ZeroWorld) -> tuple[int, float]:
    """Take an existing-setup zero through every step on the zeroing page; returns (id, dot_in)."""
    zero_id = app.create_zero(world.rifle_id, world.load_id, LOT, "existing")
    shoot(app, app.zeroing_add_group(shots=10), ring(1.08, 0.72))
    app.goto(f"/zeroing/{zero_id}")
    app.zeroing_record_dialed(-0.2, -0.3)
    shoot(app, app.zeroing_add_group(shots=10), ring(0.0, 0.0))
    app.goto(f"/zeroing/{zero_id}")
    app.zeroing_set_dot()
    with app.db() as conn:
        dot = conn.execute("SELECT dot_size_in FROM zeros WHERE id = ?", (zero_id,)).fetchone()
    return zero_id, float(dot["dot_size_in"])
