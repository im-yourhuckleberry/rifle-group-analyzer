"""Group share image: a PNG card of the group page's plot, header and specs, drawn with Pillow.

Geometry, masking and every number come from `services.analysis.group_page` (the group page's own
`Plot`, statistics and gate functions); nothing is recomputed here. A requested layer that the page
would not show (masked group, no aim, no photo, fewer than two shots) is omitted and returned in
`ShareImage.omitted`; the card itself never says so. The aim mark and the shots are always drawn
where the page draws them, both aim and mean point of impact in bright red (a crosshair with a ring
and an open x). The `numbers` layer prints the shot numbers (available whenever there are shots).
The `correction` layer also draws the mean point of impact mark and its
legend line, as the page's correction view does.

Fonts are Pillow's built-in default (`ImageFont.load_default`); no network, no external files
other than the group's working image, which is read and never copied. The PNG carries no metadata
chunks. Output is at most `MAX_WIDTH` wide; a render holds one of `MAX_CONCURRENT_RENDERS` slots and
stops at `RENDER_BUDGET_S`. Errors are `ShareError` with a stable `code`: `invalid_layers`,
`share_busy`, `share_timeout`; a missing group raises `GroupNotFound`.
"""

from __future__ import annotations

import io
import math
import sqlite3
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from rga.db import repo_groups
from rga.services import analysis
from rga.services.analysis import GroupPage
from rga.services.groups import GroupError

LAYERS = ("photo", "spread", "thing_hitter", "poi", "correction", "numbers")
DEFAULT_LAYERS = ("photo", "spread", "numbers")

MAX_WIDTH = 2000
CARD_WIDTH = 1000
MAX_CARD_HEIGHT = 2600
MAX_PHOTO_PIXELS = 36_000_000
MAX_CONCURRENT_RENDERS = 2
RENDER_BUDGET_S = 10.0

PAD = 40
PLOT_MAX_WIDTH = CARD_WIDTH - 2 * PAD
PLOT_MAX_HEIGHT = 1000
SUPERSAMPLE = 2
STROKE_PX = 1.5
HALO_EXTRA_PX = 1.0
LABEL_PX = 15
FADED_ALPHA = 0.25
SHOT_FILL_ALPHA = 0.12
DASH_SCALE = STROKE_PX / 1.25

INK = (0x1D, 0x23, 0x20)
INK_SOFT = (0x5A, 0x64, 0x5E)
LINE = (0xD9, 0xD5, 0xC8)
PAGE_BG = (255, 255, 255)
PLOT_BG = (0xFB, 0xFA, 0xF5)
AIM = (0xFF, 0x1A, 0x1A)
POI = AIM
HITTER = (0x5E, 0x3C, 0x99)
GROUP_COLOR = (0x00, 0x72, 0xB2)

SPREAD_DASH = (5, 4)
HITTER_DASH = (9, 3, 2, 3)
ESTIMATED_DASH = (4, 3)
CORRECTION_DASH = (1, 5)
AIM_ARM = 24.0
AIM_RING = 14.0
POI_ARM = 11.0

_slots = threading.BoundedSemaphore(MAX_CONCURRENT_RENDERS)

Point = tuple[float, float]


class ShareError(GroupError):
    """Base of the share image errors: a stable `code` plus a user-facing message."""


@dataclass(frozen=True)
class ShareImage:
    """A rendered card: PNG bytes, the requested layers drawn (`shown`) and left out (`omitted`)."""

    png: bytes
    width: int
    height: int
    shown: tuple[str, ...]
    omitted: tuple[str, ...]


@dataclass(frozen=True)
class _Stroke:
    lines: list[list[Point]]
    color: tuple[int, ...]
    width: int
    round_caps: bool = False


class _Deadline:
    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def check(self) -> None:
        if time.monotonic() > self._end:
            raise ShareError("share_timeout", "The share image took too long to draw; try again.")


def normalize_layers(layers: Sequence[str]) -> tuple[str, ...]:
    """The layers in request order without duplicates; `ShareError` `invalid_layers` for a name outside `LAYERS`."""
    unknown = [name for name in layers if name not in LAYERS]
    if unknown:
        raise ShareError(
            "invalid_layers", f"unknown layer {unknown[0]!r}; choose from {', '.join(LAYERS)}"
        )
    return tuple(dict.fromkeys(layers))


def render_share(
    conn: sqlite3.Connection,
    group_id: int,
    layers: Sequence[str] = DEFAULT_LAYERS,
    working_dir: Path | None = None,
) -> ShareImage:
    """Draw the share card of `group_id` with the requested `layers` (default `DEFAULT_LAYERS`).

    Raises `ShareError` (`invalid_layers`, `share_busy` when every render slot is taken,
    `share_timeout`) and `GroupNotFound`.
    """
    wanted = normalize_layers(layers)
    if not _slots.acquire(blocking=False):
        raise ShareError("share_busy", "Another share image is being drawn; try again in a moment.")
    try:
        deadline = _Deadline(RENDER_BUDGET_S)
        page = analysis.group_page(conn, group_id, working_dir)
        conditions = _conditions(conn, page)
        available = _available_layers(page)
        shown = tuple(name for name in wanted if name in available)
        omitted = tuple(name for name in wanted if name not in available)
        photo = _load_photo(page, working_dir) if "photo" in shown else None
        if "photo" in shown and photo is None:
            shown = tuple(name for name in shown if name != "photo")
            omitted = (*omitted, "photo")
        deadline.check()
        plot_image = _draw_plot(page, shown, photo, deadline)
        card = _compose(page, conditions, shown, plot_image, deadline)
        return ShareImage(_encode(card), card.width, card.height, shown, omitted)
    finally:
        _slots.release()


@dataclass(frozen=True)
class _Conditions:
    """The shooting conditions the header states: when, distance, position and suppressor label."""

    when: str
    distance_yd: int
    position: str
    suppressor: str


def _conditions(conn: sqlite3.Connection, page: GroupPage) -> _Conditions:
    """The header's conditions; the only place the card reads them from the group's own fields."""
    group = page.view.group
    if not group.suppressor:
        suppressor = "Unsuppressed"
    else:
        named = (
            repo_groups.get_suppressor(conn, group.suppressor_id) if group.suppressor_id else None
        )
        suppressor = f"Suppressed ({named.name})" if named else "Suppressed"
    year, month, day = group.shot_date.split("-")
    when = f"{month}/{day}/{year}"
    if group.shot_time:
        when = f"{when} {group.shot_time[:5]}"
    return _Conditions(
        when=when,
        distance_yd=group.distance_yd,
        position=group.position.replace("_", " "),
        suppressor=suppressor,
    )


def _available_layers(page: GroupPage) -> set[str]:
    plot = page.plot
    unmasked = not page.masked
    return {
        name
        for name, present in (
            ("photo", plot.photo is not None),
            ("spread", plot.circle is not None),
            ("thing_hitter", unmasked and plot.hitter is not None),
            ("poi", unmasked and plot.poi is not None),
            ("correction", unmasked and plot.correction is not None),
            ("numbers", bool(plot.shots)),
        )
        if present
    }


# ---------------------------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------------------------


def _load_photo(page: GroupPage, working_dir: Path | None) -> Image.Image | None:
    """The group's working image as RGB, or None when it is missing, oversized or unreadable."""
    photo = page.view.photo
    if photo is None or working_dir is None:
        return None
    path = Path(working_dir) / f"{photo.id}.png"
    try:
        with Image.open(path) as image:
            if image.width * image.height > MAX_PHOTO_PIXELS:
                return None
            image.load()
            return image.convert("RGB")
    except OSError, ValueError, Image.DecompressionBombError:
        return None


def _draw_plot(
    page: GroupPage, shown: tuple[str, ...], photo: Image.Image | None, deadline: _Deadline
) -> Image.Image:
    plot = page.plot
    bx, by, bw, bh = plot.box
    scale = min(PLOT_MAX_WIDTH / bw, PLOT_MAX_HEIGHT / bh)
    width, height = max(1, round(bw * scale)), max(1, round(bh * scale))
    base = Image.new("RGB", (width, height), PLOT_BG)
    if photo is not None and plot.photo is not None:
        _paste_photo(base, photo, plot.photo, plot.box, scale)
    deadline.check()

    k = scale * SUPERSAMPLE

    def at(x: float, y: float) -> Point:
        return ((x - bx) * k, (y - by) * k)

    stroke_w = max(1, round(STROKE_PX * SUPERSAMPLE))
    dash_unit = DASH_SCALE * SUPERSAMPLE
    faded = "correction" in shown
    alpha = FADED_ALPHA if faded else 1.0
    group_rgba = (*GROUP_COLOR, round(255 * alpha))
    overlay = Image.new("RGBA", (width * SUPERSAMPLE, height * SUPERSAMPLE), (0, 0, 0, 0))
    strokes: list[_Stroke] = []
    labels: list[tuple[Point, str]] = []

    if "spread" in shown and plot.circle is not None:
        c = plot.circle
        ring = _circle_points(*at(c.cx, c.cy), c.r * k)
        strokes.append(
            _Stroke(
                _dashed(ring, [v * dash_unit for v in SPREAD_DASH]), (*GROUP_COLOR, 255), stroke_w
            )
        )
    if "thing_hitter" in shown and plot.hitter is not None:
        ring = _circle_points(*at(0.0, 0.0), plot.hitter.r * k)
        strokes.append(
            _Stroke(_dashed(ring, [v * dash_unit for v in HITTER_DASH]), (*HITTER, 255), stroke_w)
        )
    for shot in plot.shots:
        deadline.check()
        cx, cy = at(shot.cx, shot.cy)
        radius = shot.r * k
        _fill_circle(overlay, cx, cy, radius, (*GROUP_COLOR, round(255 * SHOT_FILL_ALPHA * alpha)))
        ring = _circle_points(cx, cy, radius)
        lines = (
            _dashed(ring, [v * dash_unit for v in ESTIMATED_DASH])
            if "estimated" in shot.css_class.split()
            else [ring]
        )
        strokes.append(_Stroke(lines, group_rgba, stroke_w))
        if "numbers" in shown:
            labels.append(((cx + radius, cy - radius), shot.label))
    aim = None if page.masked else plot.aim
    poi_marked = ("poi" in shown or "correction" in shown) and plot.poi is not None
    if "correction" in shown and plot.correction is not None and plot.poi is not None:
        path = [at(*plot.poi), at(*plot.correction.corner), at(0.0, 0.0)]
        dots = _dashed(path, [v * dash_unit for v in CORRECTION_DASH])
        strokes.append(_Stroke(dots, (*INK, 255), stroke_w, round_caps=True))
    if aim is not None:
        ax, ay = at(*aim)
        arm = AIM_ARM * k
        cross = [[(ax - arm, ay), (ax + arm, ay)], [(ax, ay - arm), (ax, ay + arm)]]
        ring = _circle_points(ax, ay, AIM_RING * k)
        strokes.append(_Stroke([*cross, ring], (*AIM, 255), stroke_w))
    if poi_marked and plot.poi is not None:
        px, py = at(*plot.poi)
        arm = POI_ARM * k
        cross = [
            [(px - arm, py - arm), (px + arm, py + arm)],
            [(px - arm, py + arm), (px + arm, py - arm)],
        ]
        strokes.append(_Stroke(cross, (*POI, 255), stroke_w))

    draw = ImageDraw.Draw(overlay)
    halo = (255, 255, 255, 235)
    halo_w = stroke_w + round(2 * HALO_EXTRA_PX * SUPERSAMPLE)
    for stroke in strokes:
        _stroke(draw, stroke.lines, halo, halo_w, stroke.round_caps)
    for stroke in strokes:
        _stroke(draw, stroke.lines, stroke.color, stroke.width, stroke.round_caps)
    if labels:
        font = _font(LABEL_PX * SUPERSAMPLE)
        for position, text in labels:
            draw.text(
                position, text, font=font, fill=(255, 255, 255, 255), anchor="ls",
                stroke_width=SUPERSAMPLE + 1, stroke_fill=(0, 0, 0, 255),
            )  # fmt: skip
    deadline.check()

    flattened = overlay.resize((width, height), Image.Resampling.LANCZOS)
    result = Image.alpha_composite(base.convert("RGBA"), flattened).convert("RGB")
    ImageDraw.Draw(result).rectangle((0, 0, width - 1, height - 1), outline=LINE)
    return result


def _paste_photo(
    base: Image.Image,
    photo: Image.Image,
    placed: analysis.PlotPhoto,
    box: tuple[float, float, float, float],
    scale: float,
) -> None:
    bx, by, bw, bh = box
    x0, y0 = max(bx, placed.x), max(by, placed.y)
    x1, y1 = min(bx + bw, placed.x + placed.width), min(by + bh, placed.y + placed.height)
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        return
    sx, sy = photo.width / placed.width, photo.height / placed.height
    source = (
        (x0 - placed.x) * sx,
        (y0 - placed.y) * sy,
        (x1 - placed.x) * sx,
        (y1 - placed.y) * sy,
    )
    target = (round((x0 - bx) * scale), round((y0 - by) * scale))
    size = (max(1, round((x1 - x0) * scale)), max(1, round((y1 - y0) * scale)))
    piece = photo.resize(size, Image.Resampling.LANCZOS, box=source, reducing_gap=2.0)
    base.paste(piece, target)


def _fill_circle(
    overlay: Image.Image, cx: float, cy: float, r: float, rgba: tuple[int, ...]
) -> None:
    x0, y0 = max(0, math.floor(cx - r) - 1), max(0, math.floor(cy - r) - 1)
    x1, y1 = min(overlay.width, math.ceil(cx + r) + 2), min(overlay.height, math.ceil(cy + r) + 2)
    if x1 <= x0 or y1 <= y0:
        return
    patch = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    ImageDraw.Draw(patch).ellipse((cx - r - x0, cy - r - y0, cx + r - x0, cy + r - y0), fill=rgba)
    region = overlay.crop((x0, y0, x1, y1))
    overlay.paste(Image.alpha_composite(region, patch), (x0, y0))


def _circle_points(cx: float, cy: float, r: float) -> list[Point]:
    steps = max(48, min(4096, int(2 * math.pi * r / 4)))
    return [
        (cx + r * math.cos(2 * math.pi * i / steps), cy + r * math.sin(2 * math.pi * i / steps))
        for i in range(steps + 1)
    ]


def _dashed(points: Sequence[Point], pattern: Sequence[float]) -> list[list[Point]]:
    """`points` cut into dashes: `pattern` alternates dash and gap lengths, starting with a dash."""
    dashes: list[list[Point]] = []
    current: list[Point] = [points[0]]
    index, on = 0, True
    remaining = pattern[0]
    for (ax, ay), (bx, by) in zip(points, points[1:], strict=False):
        length = math.hypot(bx - ax, by - ay)
        if length == 0:
            continue
        used = 0.0
        while length - used > remaining:
            used += remaining
            t = used / length
            point = (ax + (bx - ax) * t, ay + (by - ay) * t)
            if on:
                current.append(point)
                dashes.append(current)
                current = []
            else:
                current = [point]
            on = not on
            index = (index + 1) % len(pattern)
            remaining = pattern[index]
        remaining -= length - used
        if on:
            current.append((bx, by))
    if on and len(current) > 1:
        dashes.append(current)
    return dashes


def _stroke(
    draw: ImageDraw.ImageDraw,
    lines: list[list[Point]],
    color: tuple[int, ...],
    width: int,
    round_caps: bool,
) -> None:
    for line in lines:
        if len(line) < 2:
            continue
        draw.line(line, fill=color, width=width, joint="curve")
        if round_caps:
            for x, y in (line[0], line[-1]):
                draw.ellipse(
                    (x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=color
                )


# ---------------------------------------------------------------------------------------------
# Card
# ---------------------------------------------------------------------------------------------


@lru_cache(maxsize=16)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _clean(text: str) -> str:
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())


def _fit(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    text = _clean(text)
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "...", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "..."


def _wrap_parts(
    draw: ImageDraw.ImageDraw, parts: list[str], font: ImageFont.FreeTypeFont, width: int
) -> list[str]:
    """Join `parts` with " | " onto as few lines as fit `width`, wrapping at part boundaries and,
    for a part wider than a line, at its words; nothing is cut off unless one word is too wide.
    """
    lines: list[str] = []
    current = ""
    for part in parts:
        joined = f"{current} | {part}" if current else part
        if draw.textlength(joined, font=font) <= width:
            current = joined
            continue
        if current:
            lines.append(current)
        chunk = ""
        for word in part.split():
            attempt = f"{chunk} {word}".strip()
            if not chunk or draw.textlength(attempt, font=font) <= width:
                chunk = attempt
            else:
                lines.append(chunk)
                chunk = word
        current = chunk
    if current:
        lines.append(current)
    return lines


def _rifle_title(name: str, cartridge: str) -> str:
    """The rifle model and chambering ("T3x .223"); the chambering is left off when the name has it."""
    cartridge = cartridge.strip()
    if not cartridge or cartridge.casefold() in name.casefold():
        return name
    return f"{name} {cartridge}"


def _header_lines(page: GroupPage, conditions: _Conditions) -> tuple[str, list[str]]:
    view = page.view
    title = _rifle_title(view.rifle.name, view.rifle.cartridge)
    lines = []
    if view.rifle.scope:
        lines.append(f"Scope: {view.rifle.scope}")
    lines.append(f"{view.load.label} | lot {view.lot_code}")
    lines.append(
        f"{conditions.when} | {conditions.distance_yd} yd | {conditions.position} | "
        f"{conditions.suppressor}"
    )
    return title, lines


def _compose(
    page: GroupPage,
    conditions: _Conditions,
    shown: tuple[str, ...],
    plot_image: Image.Image,
    deadline: _Deadline,
) -> Image.Image:
    card = Image.new("RGB", (CARD_WIDTH, MAX_CARD_HEIGHT), PAGE_BG)
    draw = ImageDraw.Draw(card)
    inner = CARD_WIDTH - 2 * PAD
    title, lines = _header_lines(page, conditions)

    y = PAD
    title_font, line_font = _font(44), _font(26)
    draw.text((PAD, y), _fit(draw, title, title_font, inner), font=title_font, fill=INK)
    y += 62
    parts = [
        conditions.when,
        f"{conditions.distance_yd} yd",
        conditions.position,
        conditions.suppressor,
    ]
    for line in [*lines[:-1], *_wrap_parts(draw, parts, line_font, inner)]:
        draw.text((PAD, y), _fit(draw, line, line_font, inner), font=line_font, fill=INK_SOFT)
        y += 36
    y += 14

    card.paste(plot_image, (PAD + (inner - plot_image.width) // 2, y))
    y += plot_image.height + 28
    deadline.check()

    y = _specs_block(draw, page, shown, y)
    return card.crop((0, 0, CARD_WIDTH, min(MAX_CARD_HEIGHT, y + PAD - 12)))


def _specs_block(draw: ImageDraw.ImageDraw, page: GroupPage, shown: tuple[str, ...], y: int) -> int:
    stats, plot = page.stats, page.plot
    label_font, value_font, sub_font = _font(20), _font(40), _font(24)
    if stats is None:
        draw.text((PAD, y), "No shots recorded yet.", font=sub_font, fill=INK_SOFT)
        return y + 40

    draw.text((PAD, y), "SHOTS", font=label_font, fill=INK_SOFT)
    draw.text((PAD, y + 26), str(stats.n), font=value_font, fill=INK)
    column = PAD + 190
    draw.text((column, y), "EXTREME SPREAD", font=label_font, fill=INK_SOFT)
    draw.text((column, y + 26), f"{stats.es_in:.2f} in", font=value_font, fill=INK)
    sub = f"{stats.es_moa:.2f} MOA | {page.es_mil:.2f} mil"
    draw.text((column, y + 78), sub, font=sub_font, fill=INK_SOFT)
    y += 124

    legend: list[tuple[tuple[int, int, int], str]] = []
    if plot.aim is not None and not page.masked:
        legend.append((AIM, "Aim point"))
    poi_listed = ("poi" in shown or "correction" in shown) and plot.poi is not None
    if poi_listed and page.poi_text:
        legend.append((POI, f"Mean point of impact: {page.poi_text}"))
    if "correction" in shown and plot.correction is not None:
        legend.append((INK, f"Correction: {plot.correction.text}"))
    if "thing_hitter" in shown and plot.hitter is not None:
        hitter = plot.hitter
        text = (
            f"“Thing-hitter” circle: {hitter.diameter_in:.2f} in ({hitter.diameter_moa:.2f} MOA, "
            f"{hitter.diameter_mil:.2f} mil at {hitter.distance_yd} yd)"
        )
        legend.append((HITTER, text))
    if "spread" in shown and plot.circle is not None:
        circle = plot.circle
        size = ""
        if circle.diameter_in is not None:
            size = f": {circle.diameter_in:.2f} in"
            if circle.diameter_moa is not None and circle.diameter_mil is not None:
                size += f" ({circle.diameter_moa:.2f} MOA, {circle.diameter_mil:.2f} mil)"
        legend.append((GROUP_COLOR, f"Cone of fire{size}"))
    text_font = _font(24)
    inner = CARD_WIDTH - 2 * PAD - 34
    for color, text in legend:
        draw.rectangle((PAD, y + 6, PAD + 18, y + 24), fill=color)
        draw.text((PAD + 34, y), _fit(draw, text, text_font, inner), font=text_font, fill=INK)
        y += 36
    return y


def _encode(card: Image.Image) -> bytes:
    """PNG bytes of `card` with no ancillary chunks (no text, profile or timestamp)."""
    out = io.BytesIO()
    card.convert("RGB").save(out, format="PNG", compress_level=6)
    return out.getvalue()
