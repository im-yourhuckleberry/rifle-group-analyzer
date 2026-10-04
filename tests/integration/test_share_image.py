"""T2: `GET /api/groups/{id}/share.png`, the group share card.

Layers are checked on decoded pixels: the plot frame is found by its border color, a layer is
present when adding it changes the plot where the page's own `Plot` puts it, and the specs block
is read through its legend swatches (exact colors at 1x). Masked groups are checked three ways:
omitted layers render byte-identically to no layers, two masked groups with the same shots and
different aim points render byte-identically, and a forged `Plot` that carries aim, mean POI,
correction and thing hitter still renders the honest card. Every spy and probe has a control that
shows it can fire.
"""

from __future__ import annotations

import ast
import io
import math
import re
import socket
import sqlite3
import struct
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import get_args

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from rga.config import Settings
from rga.db import repo_groups
from rga.db.connection import connect
from rga.domain.types import Offset, PlanePoint, ShotOrigin
from rga.services import analysis
from rga.services import share as share_service
from rga.web import schemas
from rga.web.app import create_app
from tests.support.seed import (
    DEFAULT_AIM,
    seed_context,
    seed_group,
    seed_load,
    seed_photo,
    seed_rifle,
)

ORIGIN = "http://127.0.0.1:8765"
SRC = Path(__file__).resolve().parents[2] / "src" / "rga"
PPI = 200.0
BULLET_IN = 0.224
HOLE_PX = BULLET_IN * PPI / 2
ALL_LAYERS = "photo,spread,thing_hitter,poi,correction,numbers"
DEFAULT = "photo,spread,numbers"
RED = (0xFF, 0x1A, 0x1A)
ALLOWED_CHUNKS = {b"IHDR", b"IDAT", b"IEND"}
BAD_CHUNKS = {b"tEXt", b"iTXt", b"zTXt", b"eXIf", b"iCCP", b"tIME"}
LINE = (0xD9, 0xD5, 0xC8)
SWATCH = {
    "correction": share_service.INK,
    "thing_hitter": share_service.HITTER,
    "spread": share_service.GROUP_COLOR,
}


def us_date(iso: str) -> str:
    """`iso` (YYYY-MM-DD) as the share image writes it (MM/DD/YYYY, B-82)."""
    year, month, day = iso.split("-")
    return f"{month}/{day}/{year}"


def ring(cx: float, cy: float, n: int = 10, radius: float = 0.4) -> list[Offset]:
    """`n` aim-relative shots on a circle whose mean is (cx, cy) inches (y up)."""
    return [
        Offset(
            cx + radius * math.cos(2 * math.pi * i / n), cy + radius * math.sin(2 * math.pi * i / n)
        )
        for i in range(n)
    ]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url=ORIGIN) as c:
        yield c


@pytest.fixture
def conn(settings: Settings, client: TestClient) -> Iterator[sqlite3.Connection]:
    connection = connect(settings.db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def world(conn):
    rifle = seed_rifle(conn, bullet_diameter_in=BULLET_IN, name="Tikka T3x", scope="Razor 4-16")
    return seed_load(conn, rifle), seed_context(rifle, distance_yd=100)


@dataclass(frozen=True)
class Seeded:
    group: object
    photo: object | None


def make_group(
    conn,
    settings: Settings,
    world,
    offsets: list[Offset],
    *,
    purpose: str = "general",
    aim: PlanePoint | None = DEFAULT_AIM,
    origins=None,
    with_photo: bool = True,
    write_image: bool = True,
) -> Seeded:
    """A group of `offsets` (aim-relative, y up) on a rectified synthetic photo with a dot per hole."""
    load, context = world
    plane = aim or DEFAULT_AIM
    holes = [((plane.x_in + o.dx_in) * PPI, (plane.y_in - o.dy_in) * PPI) for o in offsets]
    photo = None
    if with_photo:
        width = int(max(x for x, _ in holes)) + 300
        height = int(max(y for _, y in holes)) + 300
        photo = seed_photo(conn, confirmed=False)
        with conn:
            photo = repo_groups.update_photo_reference(
                conn,
                photo.id,
                reference_kind="circle_1in",
                reference_json="{}",
                h_px_to_plane_json="[[1,0,0],[0,1,0],[0,0,1]]",
                working_ppi=PPI,
                working_w=width,
                working_h=height,
                axis_ratio=1.0,
                reference_confirmed=True,
            )
        if write_image:
            image = np.full((height, width, 3), 235, np.uint8)
            for x, y in holes:
                cv2.circle(image, (round(x), round(y)), round(HOLE_PX), (20, 20, 20), -1)
            assert cv2.imwrite(str(settings.working_dir / f"{photo.id}.png"), image)
    group = seed_group(
        conn,
        context=context,
        load=load,
        offsets=offsets,
        origins=origins,
        purpose=purpose,
        aim=aim,
        photo=photo,
        finalize=aim is not None,
    )
    return Seeded(group, photo)


def share(client: TestClient, group_id: int, layers: str | None = None):
    suffix = "" if layers is None else f"?layers={layers}"
    return client.get(f"/api/groups/{group_id}/share.png{suffix}")


def decode(png: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))


def chunk_types(png: bytes) -> list[bytes]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    types, at = [], 8
    while at < len(png):
        length, kind = struct.unpack(">I4s", png[at : at + 8])
        types.append(kind)
        at += 12 + length
    return types


def frame_of(card: np.ndarray) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the plot frame, found by its border color."""
    border = (card == LINE).all(axis=2)
    rows = np.where(border.sum(axis=1) > 300)[0]
    cols = np.where(border.sum(axis=0) > 300)[0]
    assert len(rows) >= 2 and len(cols) >= 2, "no plot frame found"
    return int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max())


def to_pixel(card: np.ndarray, box, point) -> tuple[float, float]:
    left, top, right, _ = frame_of(card)
    scale = (right - left + 1) / box[2]
    return left + (point[0] - box[0]) * scale, top + (point[1] - box[1]) * scale


def plot_diff_bbox(a: np.ndarray, b: np.ndarray) -> tuple[int, int, int, int] | None:
    """Bounding box (x0, y0, x1, y1) of the pixels inside the plot frame that differ."""
    left, top, right, bottom = frame_of(a)
    assert frame_of(b) == (left, top, right, bottom)
    inner = (slice(top + 1, bottom), slice(left + 1, right))
    changed = np.abs(a[inner].astype(int) - b[inner].astype(int)).sum(axis=2) > 30
    ys, xs = np.where(changed)
    if len(xs) == 0:
        return None
    return (
        left + 1 + int(xs.min()),
        top + 1 + int(ys.min()),
        left + 1 + int(xs.max()),
        top + 1 + int(ys.max()),
    )


def below_frame_count(card: np.ndarray, color: tuple[int, int, int]) -> int:
    bottom = frame_of(card)[3]
    return int((card[bottom + 1 :] == color).all(axis=2).sum())


def has_solid_block(card: np.ndarray, color: tuple[int, int, int], size: int = 19) -> bool:
    """True when the specs block holds a `size` x `size` square of exactly `color` (a legend swatch)."""
    mask = (card[frame_of(card)[3] + 1 :] == color).all(axis=2).astype(np.int64)
    integral = np.pad(mask.cumsum(axis=0).cumsum(axis=1), ((1, 0), (1, 0)))
    window = (
        integral[size:, size:]
        - integral[:-size, size:]
        - integral[size:, :-size]
        + integral[:-size, :-size]
    )
    return bool((window == size * size).any())


def solid_block_count(card: np.ndarray, color: tuple[int, int, int], size: int = 19) -> int:
    """How many separate `size` x `size` legend swatches of exactly `color` the specs block holds."""
    mask = (card[frame_of(card)[3] + 1 :] == color).all(axis=2).astype(np.int64)
    integral = np.pad(mask.cumsum(axis=0).cumsum(axis=1), ((1, 0), (1, 0)))
    window = (
        integral[size:, size:]
        - integral[:-size, size:]
        - integral[size:, :-size]
        + integral[:-size, :-size]
    )
    rows = np.where((window == size * size).any(axis=1))[0]
    return int((np.diff(rows) > size).sum() + 1) if len(rows) else 0


def specs_swatches(card: np.ndarray) -> set[str]:
    """Legend entries found by swatch color; aim and mean POI share red, so they count in order."""
    found = {name for name, color in SWATCH.items() if has_solid_block(card, color)}
    red = solid_block_count(card, RED)
    if red >= 1:
        found.add("aim")
    if red >= 2:
        found.add("poi")
    return found


def page_of(conn, settings: Settings, group_id: int) -> analysis.GroupPage:
    return analysis.group_page(conn, group_id, settings.working_dir)


# ------------------------------------------------------------------------------ response contract


def test_response_is_a_png_attachment_with_the_layer_manifest(
    client, conn, settings, world
) -> None:
    seeded = make_group(conn, settings, world, ring(0.3, -0.2))
    response = share(client, seeded.group.id, ALL_LAYERS)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="group-{seeded.group.id}.png"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-share-layers"] == ALL_LAYERS
    assert response.headers["x-share-omitted"] == ""
    image = Image.open(io.BytesIO(response.content))
    assert image.format == "PNG" and image.mode == "RGB"
    assert image.width == share_service.CARD_WIDTH <= share_service.MAX_WIDTH
    assert 0 < image.height <= share_service.MAX_CARD_HEIGHT


def test_default_layers_are_photo_spread_and_numbers_and_the_render_is_deterministic(
    client, conn, settings, world
) -> None:
    gid = make_group(conn, settings, world, ring(0.1, 0.1)).group.id
    default = share(client, gid)
    assert default.headers["x-share-layers"] == DEFAULT
    assert default.content == share(client, gid, DEFAULT).content
    assert default.content != share(client, gid, "photo,spread").content, "numbers is drawn"
    assert default.content == share(client, gid).content


def test_empty_layers_draws_the_shots_only(client, conn, settings, world) -> None:
    gid = make_group(conn, settings, world, ring(0.1, 0.1)).group.id
    response = share(client, gid, "")
    assert response.status_code == 200
    assert response.headers["x-share-layers"] == "" and response.headers["x-share-omitted"] == ""
    assert specs_swatches(decode(response.content)) == {"aim"}


def test_the_png_has_no_metadata_chunks(client, conn, settings, world) -> None:
    gid = make_group(conn, settings, world, ring(0.1, 0.1)).group.id
    png = share(client, gid, ALL_LAYERS).content
    kinds = chunk_types(png)
    assert set(kinds) <= ALLOWED_CHUNKS, kinds
    assert not (set(kinds) & BAD_CHUNKS)
    assert Image.open(io.BytesIO(png)).info == {}


def test_the_chunk_check_flags_a_png_that_carries_text(tmp_path) -> None:
    from PIL import PngImagePlugin

    meta = PngImagePlugin.PngInfo()
    meta.add_text("Comment", "aim 1,2")
    out = io.BytesIO()
    Image.new("RGB", (4, 4)).save(out, "PNG", pnginfo=meta)
    assert set(chunk_types(out.getvalue())) & BAD_CHUNKS


# ------------------------------------------------------------------------------ errors


def test_unknown_group_is_404_group_not_found(client) -> None:
    response = share(client, 4242)
    assert response.status_code == 404
    assert response.json() == {"detail": response.json()["detail"], "code": "group_not_found"}


@pytest.mark.parametrize("group_id", [0, -1, 2**63, 2**64])
def test_ids_outside_the_sqlite_range_are_a_422(client, group_id: int) -> None:
    response = client.get(f"/api/groups/{group_id}/share.png")
    assert response.status_code == 422
    assert set(response.json()) == {"detail", "code"}


def test_the_top_of_the_id_range_is_an_ordinary_404(client) -> None:
    response = client.get(f"/api/groups/{2**63 - 1}/share.png")
    assert response.status_code == 404 and response.json()["code"] == "group_not_found"


@pytest.mark.parametrize(
    "query",
    [
        "layers=bogus",
        "layers=photo,bogus",
        "layers=Photo",
        "layers=" + "photo," * 80,
        "layers=photo&extra=1",
        "format=jpg",
    ],
)
def test_bad_queries_are_422_validation_errors(client, conn, settings, world, query: str) -> None:
    gid = make_group(conn, settings, world, ring(0, 0)).group.id
    response = client.get(f"/api/groups/{gid}/share.png?{query}")
    assert response.status_code == 422, response.text[:200]
    assert response.json()["code"] == "validation_error"
    assert set(response.json()) == {"detail", "code"}


def test_the_service_rejects_an_unknown_layer_with_a_stable_code(conn, settings, world) -> None:
    gid = make_group(conn, settings, world, ring(0, 0)).group.id
    with pytest.raises(share_service.ShareError) as raised:
        share_service.render_share(conn, gid, ["poi", "nope"], settings.working_dir)
    assert raised.value.code == "invalid_layers"


def test_repeated_and_blank_layer_names_are_tolerated(client, conn, settings, world) -> None:
    gid = make_group(conn, settings, world, ring(0, 0)).group.id
    response = share(client, gid, "spread,,spread, photo ")
    assert response.status_code == 200
    assert response.headers["x-share-layers"] == "spread,photo"


def test_a_busy_renderer_answers_503_with_retry_after_then_recovers(
    client, conn, settings, world
) -> None:
    gid = make_group(conn, settings, world, ring(0, 0)).group.id
    held = [
        share_service._slots.acquire(blocking=False)
        for _ in range(share_service.MAX_CONCURRENT_RENDERS)
    ]
    try:
        assert all(held), "the control needs every slot free before the test"
        busy = share(client, gid)
        assert busy.status_code == 503 and busy.json()["code"] == "share_busy"
        assert busy.headers["retry-after"] == "1"
    finally:
        for _ in held:
            share_service._slots.release()
    assert share(client, gid).status_code == 200


def test_a_render_past_its_time_budget_is_a_503_and_frees_its_slot(
    client, conn, settings, world, monkeypatch
) -> None:
    gid = make_group(conn, settings, world, ring(0, 0)).group.id
    monkeypatch.setattr(share_service, "RENDER_BUDGET_S", -1.0)
    late = share(client, gid)
    assert late.status_code == 503 and late.json()["code"] == "share_timeout"
    assert late.headers["retry-after"] == "1"
    monkeypatch.setattr(share_service, "RENDER_BUDGET_S", 10.0)
    assert share(client, gid).status_code == 200
    slots = [
        share_service._slots.acquire(blocking=False)
        for _ in range(share_service.MAX_CONCURRENT_RENDERS)
    ]
    try:
        assert all(slots), "a failed render leaked its slot"
    finally:
        for _ in slots:
            share_service._slots.release()


def test_a_locked_database_is_a_503_busy_never_a_500(
    client, conn, settings, world, monkeypatch
) -> None:
    gid = make_group(conn, settings, world, ring(0, 0)).group.id

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(analysis, "group_page", locked)
    response = share(client, gid)
    assert response.status_code == 503 and response.json()["code"] == "busy"
    assert response.headers["retry-after"] == "1"


def test_an_unreadable_or_oversized_working_image_drops_the_photo_not_the_card(
    client, conn, settings, world, monkeypatch
) -> None:
    seeded = make_group(conn, settings, world, ring(0.2, 0.2))
    working = settings.working_dir / f"{seeded.photo.id}.png"
    control = share(client, seeded.group.id, "photo")
    assert control.headers["x-share-layers"] == "photo"

    monkeypatch.setattr(share_service, "MAX_PHOTO_PIXELS", 10)
    big = share(client, seeded.group.id, "photo,spread")
    assert big.status_code == 200
    assert big.headers["x-share-layers"] == "spread" and big.headers["x-share-omitted"] == "photo"
    monkeypatch.undo()

    working.write_bytes(b"not a png at all")
    broken = share(client, seeded.group.id, "photo")
    assert broken.status_code == 200
    assert broken.headers["x-share-omitted"] == "photo"
    working.unlink()
    gone = share(client, seeded.group.id, "photo")
    assert gone.status_code == 200 and gone.headers["x-share-omitted"] == "photo"


# ------------------------------------------------------------------------------ layers


def test_the_photo_layer_draws_the_working_image_under_the_shots(
    client, conn, settings, world
) -> None:
    seeded = make_group(conn, settings, world, ring(0.3, -0.2))
    page = page_of(conn, settings, seeded.group.id)
    with_photo = decode(share(client, seeded.group.id, "photo").content)
    without = decode(share(client, seeded.group.id, "").content)
    shot = page.plot.shots[0]
    x, y = (round(v) for v in to_pixel(with_photo, page.plot.box, (shot.cx, shot.cy)))
    assert with_photo[y, x].max() < 80, "the dark hole should sit under the first shot"
    assert without[y, x].min() > 180
    bbox = plot_diff_bbox(with_photo, without)
    assert bbox is not None
    left, top, right, bottom = frame_of(with_photo)
    assert (bbox[2] - bbox[0]) > 0.5 * (right - left) and (bbox[3] - bbox[1]) > 0.5 * (bottom - top)


def test_the_spread_layer_is_the_pages_circle_where_the_page_puts_it(
    client, conn, settings, world
) -> None:
    seeded = make_group(conn, settings, world, ring(0.3, -0.2, radius=0.45))
    page = page_of(conn, settings, seeded.group.id)
    on = decode(share(client, seeded.group.id, "spread").content)
    off = decode(share(client, seeded.group.id, "").content)
    box = plot_diff_bbox(on, off)
    assert box is not None
    circle = page.plot.circle
    cx, cy = to_pixel(on, page.plot.box, (circle.cx, circle.cy))
    left, top, right, _ = frame_of(on)
    radius = circle.r * (right - left + 1) / page.plot.box[2]
    assert (box[0] + box[2]) / 2 == pytest.approx(cx, abs=3)
    assert (box[1] + box[3]) / 2 == pytest.approx(cy, abs=3)
    assert (box[2] - box[0]) / 2 == pytest.approx(radius, abs=5)
    assert specs_swatches(on) == {"aim", "spread"}


def test_the_thing_hitter_layer_is_centered_on_the_aim_with_the_pages_radius(
    client, conn, settings, world
) -> None:
    seeded = make_group(conn, settings, world, ring(0.3, -0.2, radius=0.45))
    page = page_of(conn, settings, seeded.group.id)
    on = decode(share(client, seeded.group.id, "thing_hitter").content)
    off = decode(share(client, seeded.group.id, "").content)
    box = plot_diff_bbox(on, off)
    assert box is not None
    aim_x, aim_y = to_pixel(on, page.plot.box, (0.0, 0.0))
    left, top, right, _ = frame_of(on)
    radius = page.plot.hitter.r * (right - left + 1) / page.plot.box[2]
    assert (box[0] + box[2]) / 2 == pytest.approx(aim_x, abs=3)
    assert (box[1] + box[3]) / 2 == pytest.approx(aim_y, abs=3)
    assert (box[2] - box[0]) / 2 == pytest.approx(radius, abs=5)
    assert specs_swatches(on) == {"aim", "thing_hitter"}


def test_the_poi_layer_marks_the_pages_mean_point_of_impact(client, conn, settings, world) -> None:
    seeded = make_group(conn, settings, world, ring(0.3, -0.2))
    page = page_of(conn, settings, seeded.group.id)
    on = decode(share(client, seeded.group.id, "poi").content)
    off = decode(share(client, seeded.group.id, "").content)
    box = plot_diff_bbox(on, off)
    assert box is not None
    px, py = to_pixel(on, page.plot.box, page.plot.poi)
    assert (box[0] + box[2]) / 2 == pytest.approx(px, abs=3)
    assert (box[1] + box[3]) / 2 == pytest.approx(py, abs=3)
    assert specs_swatches(on) == {"aim", "poi"}


def test_the_correction_layer_draws_the_path_fades_the_shots_and_lists_the_correction(
    client, conn, settings, world
) -> None:
    seeded = make_group(conn, settings, world, ring(0.3, -0.2))
    page = page_of(conn, settings, seeded.group.id)
    assert page.plot.correction is not None
    on = decode(share(client, seeded.group.id, "correction").content)
    off = decode(share(client, seeded.group.id, "").content)
    box = plot_diff_bbox(on, off)
    assert box is not None
    poi_x, poi_y = to_pixel(on, page.plot.box, page.plot.poi)
    assert box[0] <= poi_x <= box[2] and box[1] <= poi_y <= box[3]
    for shot in page.plot.shots:
        x, y = to_pixel(on, page.plot.box, (shot.cx + shot.r, shot.cy))
        assert box[0] - 2 <= x <= box[2] + 2 and box[1] - 2 <= y <= box[3] + 2, (
            "the shot rings fade"
        )
    assert specs_swatches(on) == {"aim", "poi", "correction"}


def test_layers_not_requested_are_not_drawn(client, conn, settings, world) -> None:
    gid = make_group(conn, settings, world, ring(0.3, -0.2)).group.id
    for requested, expected in [
        ("photo", {"aim"}),
        ("spread", {"aim", "spread"}),
        ("poi", {"aim", "poi"}),
        ("thing_hitter,spread", {"aim", "thing_hitter", "spread"}),
        (ALL_LAYERS, {"aim", "poi", "correction", "thing_hitter", "spread"}),
    ]:
        card = decode(share(client, gid, requested).content)
        assert specs_swatches(card) == expected, requested


def test_a_single_shot_group_omits_the_spread_circle_and_lists_it(
    client, conn, settings, world
) -> None:
    gid = make_group(conn, settings, world, [Offset(0.2, 0.1)]).group.id
    response = share(client, gid, "spread,thing_hitter")
    assert response.status_code == 200
    assert response.headers["x-share-layers"] == "thing_hitter"
    assert response.headers["x-share-omitted"] == "spread"


def test_a_group_without_a_photo_omits_the_photo_layer(client, conn, settings, world) -> None:
    gid = make_group(conn, settings, world, ring(0, 0), with_photo=False).group.id
    response = share(client, gid, "photo,spread")
    assert response.headers["x-share-layers"] == "spread"
    assert response.headers["x-share-omitted"] == "photo"


def test_no_estimated_badge_is_drawn_even_when_a_shot_is_estimated(
    client, conn, settings, world
) -> None:
    offsets = ring(0.1, 0.1, n=5)
    estimated = make_group(
        conn, settings, world, offsets, origins=[ShotOrigin.ESTIMATED] + [ShotOrigin.DETECTED] * 4
    ).group.id
    plain = make_group(conn, settings, world, offsets).group.id
    badge = (0xFB, 0xEE, 0xDB)  # the fill of the badge B-82 removed
    assert below_frame_count(decode(share(client, estimated).content), badge) == 0
    assert below_frame_count(decode(share(client, plain).content), badge) == 0


def test_the_header_names_rifle_scope_suppressor_load_date_and_distance(
    conn, settings, world
) -> None:
    load, context = world
    gid = make_group(conn, settings, world, ring(0, 0)).group.id
    page = page_of(conn, settings, gid)
    title, lines = share_service._header_lines(page, share_service._conditions(conn, page))
    assert title == "Tikka T3x 6.5 Creedmoor"
    assert lines == [
        "Scope: Razor 4-16",
        f"{load.label} | lot LOT-1",
        f"{us_date(context.date)} | 100 yd | bench | Unsuppressed",
    ]


@pytest.mark.parametrize(
    ("name", "cartridge", "title"),
    [
        ("T3x", ".223", "T3x .223"),
        ("T3x .223", ".223", "T3x .223"),
        ("Tikka T3X 6.5 creedmoor", "6.5 Creedmoor", "Tikka T3X 6.5 creedmoor"),
        ("T3x", "  ", "T3x"),
    ],
)
def test_the_title_is_the_rifle_model_and_chambering_once(name, cartridge, title) -> None:
    assert share_service._rifle_title(name, cartridge) == title


def test_the_header_date_is_month_day_year(conn, settings, world) -> None:
    load, _ = world
    context = seed_context(load.rifle_id, date="2026-10-02")
    gid = seed_group(conn, context=context, load=load, offsets=ring(0, 0)).id
    page = page_of(conn, settings, gid)
    line = share_service._header_lines(page, share_service._conditions(conn, page))[1][-1]
    assert line.startswith("10/02/2026 | ")


def test_the_header_shows_the_shot_time_to_the_minute_when_the_group_has_one(
    conn, settings, world
) -> None:
    load, _ = world
    rifle_id = load.rifle_id
    timed = seed_context(rifle_id, date="2026-09-30", time="07:05:42", utc_offset="-06:00")
    untimed = seed_context(rifle_id, date="2026-09-30")
    lines = {}
    for key, context in {"timed": timed, "untimed": untimed}.items():
        gid = seed_group(conn, context=context, load=load, offsets=ring(0, 0)).id
        page = page_of(conn, settings, gid)
        lines[key] = share_service._header_lines(page, share_service._conditions(conn, page))[1][-1]
    assert lines == {
        "timed": "09/30/2026 07:05 | 100 yd | bench | Unsuppressed",
        "untimed": "09/30/2026 | 100 yd | bench | Unsuppressed",
    }


def test_the_header_reads_distance_and_position_from_the_group(conn, settings, world) -> None:
    load, _ = world
    context = seed_context(load.rifle_id, distance_yd=300, position="prone_off_pack")
    gid = seed_group(conn, context=context, load=load, offsets=ring(0, 0)).id
    page = page_of(conn, settings, gid)
    _, lines = share_service._header_lines(page, share_service._conditions(conn, page))
    assert lines[-1] == "09/30/2026 | 300 yd | prone off pack | Unsuppressed"


def test_the_suppressor_shows_its_name_when_the_group_names_one(conn, settings, world) -> None:
    rifle = seed_rifle(conn, name="Other", scope=None)
    load = seed_load(conn, rifle)
    named = repo_groups.create_suppressor(conn, "Omega 300")
    contexts = {
        "plain": seed_context(rifle, suppressor=True),
        "named": seed_context(rifle, suppressor=True, suppressor_id=named.id),
        "off": seed_context(rifle, suppressor=False),
    }
    pages = {}
    for key, context in contexts.items():
        gid = seed_group(conn, context=context, load=load, offsets=ring(0, 0)).id
        pages[key] = page_of(conn, settings, gid)
    labels = {key: share_service._conditions(conn, page).suppressor for key, page in pages.items()}
    assert labels == {
        "plain": "Suppressed",
        "named": "Suppressed (Omega 300)",
        "off": "Unsuppressed",
    }
    _, lines = share_service._header_lines(
        pages["named"], share_service._conditions(conn, pages["named"])
    )
    assert lines == [
        f"{load.label} | lot LOT-1",
        f"{us_date(contexts['named'].date)} | 100 yd | bench | Suppressed (Omega 300)",
    ]


CONTEXT_FIELDS = {
    "shot_date",
    "shot_time",
    "distance_yd",
    "position",
    "suppressor",
    "suppressor_id",
}


def context_reads(source: str) -> list[str]:
    """Names of the functions that read a context attribute (date, time, ...) off a group row."""
    readers = []
    for func in ast.walk(ast.parse(source)):
        if isinstance(func, ast.FunctionDef) and any(
            isinstance(node, ast.Attribute)
            and node.attr in CONTEXT_FIELDS
            and (
                (isinstance(node.value, ast.Name) and node.value.id == "group")
                or (isinstance(node.value, ast.Attribute) and node.value.attr == "group")
            )
            for node in ast.walk(func)
        ):
            readers.append(func.name)
    return readers


def test_only_the_conditions_accessor_reads_the_group_context() -> None:
    source = (SRC / "services" / "share.py").read_text()
    assert context_reads(source) == ["_conditions"]
    assert context_reads("def other(page):\n    return page.view.group.shot_date") == ["other"]


def test_header_text_from_the_database_is_cleaned_and_fitted() -> None:
    assert share_service._clean("a\nb\t\x00c  d") == "a b c d"
    from PIL import ImageDraw

    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font = share_service._font(26)
    fitted = share_service._fit(draw, "W" * 200, font, 300)
    assert fitted.endswith("...") and draw.textlength(fitted, font=font) <= 300
    assert share_service._fit(draw, "short", font, 300) == "short"


def test_fonts_are_pillows_built_in_default_only() -> None:
    assert isinstance(share_service._font(24), ImageFont.FreeTypeFont)
    source = (SRC / "services" / "share.py").read_text()
    assert "load_default(size=" in source
    assert "truetype(" not in source and ".ttf" not in source and ".otf" not in source


def drawn_texts(monkeypatch) -> list[str]:
    """Every string the renderer draws, recorded by wrapping `ImageDraw.text`."""
    texts: list[str] = []
    real = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        texts.append(text)
        return real(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    return texts


def test_the_card_text_carries_the_pages_numbers_and_gated_lines(
    conn, settings, world, monkeypatch
) -> None:
    load, context = world
    seeded = make_group(conn, settings, world, ring(0.3, -0.2))
    page = page_of(conn, settings, seeded.group.id)
    texts = drawn_texts(monkeypatch)
    share_service.render_share(conn, seeded.group.id, share_service.LAYERS, settings.working_dir)
    stats, plot = page.stats, page.plot
    assert "Tikka T3x 6.5 Creedmoor" in texts, "control: the spy must see the header"
    assert str(stats.n) in texts
    assert f"{stats.es_in:.2f} in" in texts
    assert f"{stats.es_moa:.2f} MOA | {page.es_mil:.2f} mil" in texts
    assert f"Mean point of impact: {page.poi_text}" in texts
    assert f"Correction: {plot.correction.text}" in texts
    hitter = plot.hitter
    assert (
        f"“Thing-hitter” circle: {hitter.diameter_in:.2f} in ({hitter.diameter_moa:.2f} MOA, "
        f"{hitter.diameter_mil:.2f} mil at 100 yd)"
    ) in texts
    circle = plot.circle
    assert (
        f"Cone of fire: {circle.diameter_in:.2f} in "
        f"({circle.diameter_moa:.2f} MOA, {circle.diameter_mil:.2f} mil)"
    ) in texts
    assert "Aim point" in texts
    labels = [shot.label for shot in plot.shots]
    assert [t for t in texts if t.isdigit()] == [*labels, str(stats.n)]
    assert f"{us_date(context.date)} | 100 yd | bench | Unsuppressed" in texts
    assert f"{load.label} | lot LOT-1" in texts and "Scope: Razor 4-16" in texts


def test_a_layer_that_is_not_requested_adds_no_text(conn, settings, world, monkeypatch) -> None:
    seeded = make_group(conn, settings, world, ring(0.3, -0.2))
    texts = drawn_texts(monkeypatch)
    share_service.render_share(conn, seeded.group.id, ["photo"], settings.working_dir)
    assert [t for t in texts if t.isdigit()] == [
        str(len(page_of(conn, settings, seeded.group.id).plot.shots))
    ]
    joined = " | ".join(texts)
    for word in ("Mean point", "Correction", "Thing-hitter", "Cone of fire"):
        assert word not in joined


def test_no_estimated_text_is_drawn_for_estimated_shots(conn, settings, world, monkeypatch) -> None:
    offsets = ring(0.1, 0.1, n=5)
    estimated = make_group(
        conn, settings, world, offsets, origins=[ShotOrigin.ESTIMATED] + [ShotOrigin.DETECTED] * 4
    ).group.id
    texts = drawn_texts(monkeypatch)
    share_service.render_share(conn, estimated, [], settings.working_dir)
    assert "Tikka T3x 6.5 Creedmoor" in texts, "control: the spy must see the header"
    assert not any("estimated" in t.lower() for t in texts)


def test_an_estimated_shot_is_drawn_dashed_where_a_detected_one_is_solid(
    client, conn, settings, world
) -> None:
    offsets = ring(0.1, 0.1, n=5)
    estimated = make_group(
        conn,
        settings,
        world,
        offsets,
        origins=[ShotOrigin.ESTIMATED] + [ShotOrigin.DETECTED] * 4,
        with_photo=False,
    )
    detected = make_group(conn, settings, world, offsets, with_photo=False)
    a = decode(share(client, estimated.group.id, "").content)
    b = decode(share(client, detected.group.id, "").content)
    page = page_of(conn, settings, estimated.group.id)
    shot = page.plot.shots[0]
    left, top, right, bottom = frame_of(a)
    scale = (right - left + 1) / page.plot.box[2]
    cx, cy = to_pixel(a, page.plot.box, (shot.cx, shot.cy))
    inner = (slice(top + 1, bottom), slice(left + 1, right))
    changed = np.abs(a[inner].astype(int) - b[inner].astype(int)).sum(axis=2) > 30
    ys, xs = np.where(changed)
    assert len(xs) > 20, "the dashed ring must differ from the solid one"
    reach = shot.r * scale + 4
    assert abs(left + 1 + xs - cx).max() <= reach and abs(top + 1 + ys - cy).max() <= reach


# ------------------------------------------------------------------------------ masking


def masked_pair(conn, settings, world, purpose="zero_check", n=5):
    """Two masked groups with identical plane shot positions and different aim points."""
    shots = ring(0.3, -0.2, n=n)
    aim_a, aim_b = PlanePoint(4.25, 5.5), PlanePoint(5.0, 4.0)
    shift = (aim_a.x_in - aim_b.x_in, aim_a.y_in - aim_b.y_in)
    shifted = [Offset(o.dx_in + shift[0], o.dy_in - shift[1]) for o in shots]
    a = make_group(conn, settings, world, shots, purpose=purpose, aim=aim_a)
    b = make_group(conn, settings, world, shifted, purpose=purpose, aim=aim_b)
    return a, b


@pytest.mark.parametrize(
    ("purpose", "n"), [("zero_check", 5), ("zero_check", 12), ("zero_step2", 5)]
)
def test_a_masked_group_never_shows_aim_poi_correction_or_thing_hitter(
    client, conn, settings, world, purpose: str, n: int
) -> None:
    seeded = make_group(conn, settings, world, ring(0.3, -0.2, n=n), purpose=purpose)
    assert page_of(conn, settings, seeded.group.id).masked
    requested = share(client, seeded.group.id, ALL_LAYERS)
    assert requested.status_code == 200
    assert requested.headers["x-share-layers"] == DEFAULT
    assert requested.headers["x-share-omitted"] == "thing_hitter,poi,correction"
    plain = share(client, seeded.group.id, DEFAULT)
    assert requested.content == plain.content, "an omitted layer changed the picture"
    card = decode(requested.content)
    assert specs_swatches(card) == {"spread"}
    plot = card[frame_of(card)[1] : frame_of(card)[3]]
    for color in (share_service.AIM, share_service.HITTER):
        near = (np.abs(plot.astype(int) - np.array(color)).sum(axis=2) < 40).sum()
        assert near == 0, color


def test_masked_cards_do_not_depend_on_the_aim_point_but_unmasked_cards_do(
    client, conn, settings, world
) -> None:
    a, b = masked_pair(conn, settings, world)
    assert (
        share(client, a.group.id, ALL_LAYERS).content
        == share(client, b.group.id, ALL_LAYERS).content
    )
    ua, ub = masked_pair(conn, settings, world, purpose="general", n=10)
    assert share(client, ua.group.id, "").content != share(client, ub.group.id, "").content, (
        "control: the aim point must change an unmasked card"
    )


def test_the_masked_card_text_and_bytes_hold_no_aim_poi_or_correction_wording(
    client, conn, settings, world
) -> None:
    seeded = make_group(conn, settings, world, ring(0.3, -0.2), purpose="zero_check")
    png = share(client, seeded.group.id, ALL_LAYERS).content
    page = page_of(conn, settings, seeded.group.id)
    assert page.poi_text is None and page.plot.poi is None and page.plot.correction is None
    assert not re.search(rb"(?i)aim|poi|correction|hitter|mil (high|low|up|down)", png[:200])
    assert set(chunk_types(png)) <= ALLOWED_CHUNKS


def test_a_forged_plot_with_aim_poi_correction_and_hitter_still_renders_the_masked_card(
    client, conn, settings, world, monkeypatch
) -> None:
    seeded = make_group(conn, settings, world, ring(0.3, -0.2), purpose="zero_check")
    honest = share(client, seeded.group.id, ALL_LAYERS).content
    real = analysis.group_page

    def forged(connection, group_id, working_dir=None):
        page = real(connection, group_id, working_dir)
        plot = replace(
            page.plot,
            aim=(0.0, 0.0),
            poi=(12.0, 8.0),
            hitter=analysis.PlotHitter(
                r=90.0, diameter_in=1.8, diameter_moa=1.7, diameter_mil=0.5, distance_yd=100
            ),
            correction=analysis.PlotCorrection(0.1, 0.1, "Adjust 0.1 mil up", 100, (12.0, 0.0)),
        )
        return replace(page, plot=plot, poi_text="0.3 mil high, 0.2 mil right")

    monkeypatch.setattr(analysis, "group_page", forged)
    assert share(client, seeded.group.id, ALL_LAYERS).content == honest
    unmasked = make_group(conn, settings, world, ring(0.3, -0.2))
    assert share(client, unmasked.group.id, ALL_LAYERS).headers["x-share-omitted"] == "", (
        "control: the forged-page wrapper must not hide layers of an unmasked group"
    )


def test_a_group_with_no_aim_omits_aim_dependent_layers(client, conn, settings, world) -> None:
    seeded = make_group(conn, settings, world, ring(0.2, 0.1), aim=None)
    response = share(client, seeded.group.id, ALL_LAYERS)
    assert response.status_code == 200
    assert response.headers["x-share-omitted"] == "thing_hitter,poi,correction"


def test_correction_is_withheld_where_the_page_withholds_it(client, conn, settings, world) -> None:
    short = make_group(conn, settings, world, ring(0.3, -0.2, n=5))
    assert page_of(conn, settings, short.group.id).plot.correction is None
    response = share(client, short.group.id, "poi,correction")
    assert response.headers["x-share-layers"] == "poi"
    assert response.headers["x-share-omitted"] == "correction"
    step1 = make_group(conn, settings, world, ring(0.3, -0.2, n=3), purpose="zero_step1")
    assert page_of(conn, settings, step1.group.id).plot.correction is not None
    assert share(client, step1.group.id, "correction").headers["x-share-layers"] == "correction"


# ------------------------------------------------------------------------------ bounds


def test_a_huge_spread_and_a_crowd_of_shots_stay_inside_the_size_and_time_bounds(
    client, conn, settings, world
) -> None:
    far = [Offset(-300.0, 250.0), Offset(280.0, -260.0), Offset(1.0, 1.0)]
    crowd = ring(0.0, 0.0, n=150, radius=2.0)
    for offsets in (far, crowd):
        gid = make_group(conn, settings, world, offsets, with_photo=False).group.id
        started = time.monotonic()
        response = share(client, gid, ALL_LAYERS)
        elapsed = time.monotonic() - started
        assert response.status_code == 200, response.text[:200]
        image = Image.open(io.BytesIO(response.content))
        assert image.width <= share_service.MAX_WIDTH
        assert image.height <= share_service.MAX_CARD_HEIGHT
        assert elapsed < share_service.RENDER_BUDGET_S


def test_a_group_with_no_shots_still_renders_the_header_and_a_note(
    client, conn, settings, world
) -> None:
    load, context = world
    group = seed_group(conn, context=context, load=load, offsets=[], aim=None, finalize=False)
    response = share(client, group.id, ALL_LAYERS)
    assert response.status_code == 200
    assert Image.open(io.BytesIO(response.content)).width == share_service.CARD_WIDTH


# ------------------------------------------------------------------------------ isolation


def test_a_render_uses_no_network_and_the_spy_can_fire(conn, settings, world, monkeypatch) -> None:
    seeded = make_group(conn, settings, world, ring(0.3, -0.2))
    calls: list[str] = []

    def spy(name: str):
        def wrapper(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"network call: {name}")

        return wrapper

    for name in ("connect", "connect_ex", "sendto", "bind"):
        monkeypatch.setattr(socket.socket, name, spy(f"socket.{name}"))
    monkeypatch.setattr(socket, "getaddrinfo", spy("getaddrinfo"))
    monkeypatch.setattr(socket, "create_connection", spy("create_connection"))

    result = share_service.render_share(
        conn, seeded.group.id, share_service.LAYERS, settings.working_dir
    )
    assert result.shown == share_service.LAYERS and calls == []
    with pytest.raises(AssertionError):
        socket.create_connection(("example.invalid", 80))
    assert calls == ["create_connection"], "control: the spy must see a network call"


NETWORK_MODULES = {
    "socket",
    "ssl",
    "http",
    "urllib",
    "requests",
    "httpx",
    "aiohttp",
    "ftplib",
    "smtplib",
    "websockets",
}


def imported_roots(source: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_the_share_modules_import_no_network_library_and_the_scan_can_fire() -> None:
    for path in (SRC / "services" / "share.py", SRC / "web" / "routes" / "share.py"):
        assert imported_roots(path.read_text()) & NETWORK_MODULES == set(), path
    assert imported_roots("import urllib.request\nfrom http import client") & NETWORK_MODULES == {
        "urllib",
        "http",
    }


def test_rendering_writes_no_files_and_copies_no_photo(client, conn, settings, world) -> None:
    seeded = make_group(conn, settings, world, ring(0.3, -0.2))

    def listing() -> dict[str, tuple[int, int]]:
        return {
            str(p.relative_to(settings.data_dir)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(settings.data_dir.rglob("*"))
            if p.is_file() and not p.name.startswith(settings.db_path.name)
        }

    before = listing()
    assert any(name.startswith("working") for name in before), "control: the working image exists"
    for layers in (None, ALL_LAYERS, ""):
        assert share(client, seeded.group.id, layers).status_code == 200
    assert listing() == before
    stray = settings.data_dir / "stray.png"
    stray.write_bytes(b"x")
    assert listing() != before, "control: the listing must notice a new file"


# ------------------------------------------------------------------------------ contracts


def test_the_query_model_and_the_service_agree_on_the_layer_names() -> None:
    assert get_args(schemas.ShareLayer) == share_service.LAYERS
    assert schemas.SHARE_DEFAULT_LAYERS == share_service.DEFAULT_LAYERS
    assert schemas.ShareQuery().layers == list(share_service.DEFAULT_LAYERS)


def test_the_route_is_registered_and_in_the_openapi_document(client) -> None:
    schema = schemas.ROUTE_SCHEMAS[("GET", "/api/groups/{id}/share.png")]
    assert schema.query is schemas.ShareQuery and schema.request is None and schema.response is None
    operation = client.app.openapi()["paths"]["/api/groups/{group_id}/share.png"]["get"]
    assert "image/png" in operation["responses"]["200"]["content"]
    assert schemas.ROUTE_SCHEMAS[
        ("GET", "/api/groups/{id}/share.png")
    ].query.model_fields.keys() == {"layers"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("photo,spread", ["photo", "spread"]),
        ("spread,photo,spread", ["spread", "photo"]),
        ("", []),
        (" poi , correction ", ["poi", "correction"]),
    ],
)
def test_the_layers_query_parses_a_comma_list(text: str, expected: list[str]) -> None:
    assert schemas.ShareQuery.model_validate({"layers": text}).layers == expected


# ------------------------------------------------------------------------------ B-76 (T4.0q)


def red_near(card: np.ndarray, point: tuple[float, float], radius: int = 2) -> bool:
    """True when a pixel within `radius` of `point` is bright red (#ff1a1a, allowing for blending)."""
    x, y = round(point[0]), round(point[1])
    window = card[y - radius : y + radius + 1, x - radius : x + radius + 1].astype(int)
    return bool((np.abs(window - np.array(RED)).sum(axis=2) < 90).any())


def units_to_pixels(card: np.ndarray, plot: analysis.Plot) -> float:
    left, _, right, _ = frame_of(card)
    return (right - left + 1) / plot.box[2]


def test_the_aim_and_the_mean_poi_are_bright_red_and_differ_in_shape(
    client, conn, settings, world
) -> None:
    gid = make_group(conn, settings, world, ring(0.3, -0.2), with_photo=False).group.id
    plot = page_of(conn, settings, gid).plot
    assert share_service.AIM == share_service.POI == RED
    card = decode(share(client, gid, "poi").content)
    bare = decode(share(client, gid, "").content)
    unit = units_to_pixels(card, plot)
    ax, ay = to_pixel(card, plot.box, (0.0, 0.0))
    px, py = to_pixel(card, plot.box, plot.poi)
    arm = 18 * unit
    diagonal = 5 * unit
    assert red_near(card, (ax + arm, ay)) and red_near(card, (ax, ay - arm)), "aim crosshair arms"
    assert not red_near(card, (ax + 6 * unit, ay + 6 * unit)), "no aim mark off the arms and ring"
    assert red_near(card, (ax + 14 * unit, ay)), "aim ring"
    assert red_near(card, (px + diagonal, py + diagonal)) and red_near(
        card, (px + diagonal, py - diagonal)
    ), "POI open x"
    assert not red_near(card, (px + 8 * unit, py)) and not red_near(card, (px, py - 8 * unit)), (
        "the POI mark is an x, not a crosshair"
    )
    assert red_near(bare, (ax + arm, ay)), "the aim is drawn without any layer"
    assert not red_near(bare, (px + diagonal, py + diagonal)), (
        "control: no POI mark without its layer"
    )


def test_the_numbers_layer_prints_the_shot_numbers_and_the_toggle_changes_the_picture(
    client, conn, settings, world, monkeypatch
) -> None:
    gid = make_group(conn, settings, world, ring(0.3, -0.2)).group.id
    page = page_of(conn, settings, gid)
    labels = [shot.label for shot in page.plot.shots]
    texts = drawn_texts(monkeypatch)
    on = share(client, gid, "numbers")
    assert on.headers["x-share-layers"] == "numbers"
    assert [t for t in texts if t.isdigit()] == [*labels, str(page.stats.n)]
    texts.clear()
    off = share(client, gid, "")
    assert [t for t in texts if t.isdigit()] == [str(page.stats.n)]
    box = plot_diff_bbox(decode(on.content), decode(off.content))
    assert box is not None, "the numbers show on the plot"
    card = decode(on.content)
    for shot in page.plot.shots:
        x, y = to_pixel(card, page.plot.box, (shot.cx + shot.r, shot.cy - shot.r))
        assert box[0] - 4 <= x <= box[2] + 4 and box[1] - 4 <= y <= box[3] + 4


def test_the_numbers_layer_is_listed_when_drawn_and_survives_the_other_layers(
    client, conn, settings, world, monkeypatch
) -> None:
    gid = make_group(conn, settings, world, ring(0.3, -0.2)).group.id
    labels = [shot.label for shot in page_of(conn, settings, gid).plot.shots]
    texts = drawn_texts(monkeypatch)
    response = share(client, gid, "correction,numbers")
    assert response.headers["x-share-layers"] == "correction,numbers"
    assert response.headers["x-share-omitted"] == ""
    assert [t for t in texts if t.isdigit()][: len(labels)] == labels


def test_numbers_is_omitted_for_a_group_with_no_shots_and_listed_as_such(
    client, conn, settings, world
) -> None:
    load, context = world
    group = seed_group(conn, context=context, load=load, offsets=[], aim=None, finalize=False)
    response = share(client, group.id, "numbers")
    assert response.status_code == 200
    assert response.headers["x-share-layers"] == ""
    assert response.headers["x-share-omitted"] == "numbers"


def test_a_masked_group_may_show_numbers_and_the_masking_proofs_still_hold(
    client, conn, settings, world
) -> None:
    a, b = masked_pair(conn, settings, world)
    on = share(client, a.group.id, "photo,spread,numbers,poi,correction,thing_hitter")
    assert on.headers["x-share-layers"] == "photo,spread,numbers"
    assert on.headers["x-share-omitted"] == "poi,correction,thing_hitter"
    assert on.content == share(client, a.group.id, DEFAULT).content
    assert on.content == share(client, b.group.id, DEFAULT).content, "aim point must not show"
    assert on.content != share(client, a.group.id, "photo,spread").content, "numbers are drawn"


def test_the_spread_label_carries_the_pages_inches_moa_and_mil_at_the_groups_distance(
    conn, settings, world, monkeypatch
) -> None:
    load, _ = world
    far = seed_context(load.rifle_id, distance_yd=300)
    for context, distance in ((world[1], 100), (far, 300)):
        gid = seed_group(conn, context=context, load=load, offsets=ring(0.3, -0.2)).id
        circle = page_of(conn, settings, gid).plot.circle
        texts = drawn_texts(monkeypatch)
        share_service.render_share(conn, gid, ["spread"], settings.working_dir)
        label = (
            f"Cone of fire: {circle.diameter_in:.2f} in "
            f"({circle.diameter_moa:.2f} MOA, {circle.diameter_mil:.2f} mil)"
        )
        assert label in texts, (distance, [t for t in texts if "Cone" in t])
        assert circle.diameter_moa == pytest.approx(
            circle.diameter_in / (1.047 * distance / 100), rel=0.01
        )
        monkeypatch.undo()


def test_the_spread_label_has_sizes_only_where_the_page_allows_them(
    conn, settings, world, monkeypatch
) -> None:
    load, context = world
    masked = seed_group(
        conn, context=context, load=load, offsets=ring(0.3, -0.2), purpose="zero_check"
    ).id
    circle = page_of(conn, settings, masked).plot.circle
    assert circle.diameter_in is None and circle.diameter_moa is None
    texts = drawn_texts(monkeypatch)
    share_service.render_share(conn, masked, ["spread"], settings.working_dir)
    assert "Cone of fire" in texts
    assert not any(t.startswith("Cone of fire:") or "MOA" in t and "Cone" in t for t in texts)
