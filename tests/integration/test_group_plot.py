"""T2: group results page overlays (UF1): photo, spread circle, mean POI and correction toggles."""

from __future__ import annotations

import html as htmllib
import math
import random
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from rga.config import Settings
from rga.db import repo_groups
from rga.db.connection import connect
from rga.domain.enclosing import smallest_enclosing_circle
from rga.domain.types import Offset, PlanePoint, ShotOrigin
from rga.domain.zeroing import correction_for, step1_advice, step2_initial_advice
from rga.services import analysis as analysis_service
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
WEB = Path(__file__).resolve().parents[2] / "src" / "rga" / "web"
PPI = 200.0
BULLET_IN = 0.224
HOLE_PX = BULLET_IN * PPI / 2
PLOT_UNITS_PER_IN = analysis_service.PLOT_PX_PER_IN
POI_WORDS = re.compile(r"\b(mils?|high|low|left|right|POI)\b|mean_d[xy]|poi_text", re.IGNORECASE)
DIRECTION_WORDS = re.compile(r"\b(up|down|left|right|high|low)\b", re.IGNORECASE)


def ring(cx: float, cy: float, n: int = 10, radius: float = 0.4) -> list[Offset]:
    """`n` aim-relative shots on a circle whose mean is exactly (cx, cy) inches (y up)."""
    return [
        Offset(
            cx + radius * math.cos(2 * math.pi * i / n), cy + radius * math.sin(2 * math.pi * i / n)
        )
        for i in range(n)
    ]


@dataclass(frozen=True)
class Seeded:
    group: object
    photo: object | None
    holes_px: list[tuple[float, float]]
    width: int
    height: int


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


def make_world(conn, distance_yd: int = 100):
    rifle = seed_rifle(conn, bullet_diameter_in=BULLET_IN)
    return seed_load(conn, rifle), seed_context(rifle, distance_yd=distance_yd)


def photo_group(
    conn,
    settings: Settings,
    world,
    offs: list[Offset],
    *,
    purpose: str = "general",
    aim: PlanePoint | None = DEFAULT_AIM,
    roi: tuple[int, int, int, int] | None = None,
    origins=None,
    write_image: bool = True,
    with_photo: bool = True,
    lot_code: str = "LOT-1",
) -> Seeded:
    """A group of `offs` (aim-relative, y up) on a rectified synthetic photo with a dot per hole."""
    load, session = world
    plane = aim or DEFAULT_AIM
    holes = [((plane.x_in + o.dx_in) * PPI, (plane.y_in - o.dy_in) * PPI) for o in offs]
    width = int(max(x for x, _ in holes)) + 300
    height = int(max(y for _, y in holes)) + 300
    photo = None
    if with_photo:
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
                cv2.circle(
                    image, (round(x), round(y)), round(HOLE_PX), (20, 20, 20), -1, cv2.LINE_AA
                )
            assert cv2.imwrite(str(settings.working_dir / f"{photo.id}.png"), image)
    group = seed_group(
        conn,
        context=session,
        load=load,
        offsets=offs,
        origins=origins,
        purpose=purpose,
        aim=aim,
        photo=photo,
        roi_json=None
        if roi is None
        else f'{{"x0": {roi[0]}, "y0": {roi[1]}, "x1": {roi[2]}, "y1": {roi[3]}}}',
        finalize=aim is not None,
        lot_code=lot_code,
    )
    return Seeded(group, photo, holes, width, height)


def without_es_mil(text: str) -> str:
    """`text` without the label sentence and the extreme spread's mil equivalent (size only)."""
    text = re.sub(r'<span class="muted es-mil"[^>]*>.*?</span>', "", text)
    text = re.sub(r"\u00b7\s*[\d.]+ mil", "", text)
    return text.replace("smallest target every shot hit", "")


def html_of(client, group_id: int) -> str:
    response = client.get(f"/groups/{group_id}")
    assert response.status_code == 200, response.text
    return response.text


def main_of(html: str) -> str:
    return html.split("<main", 1)[1]


def plot_shots(html: str) -> list[tuple[float, float, float]]:
    return [
        (float(x), float(y), float(r))
        for x, y, r in re.findall(
            r'<circle class="shot [a-z]+" cx="([-\d.]+)" cy="([-\d.]+)" r="([\d.]+)"', html
        )
    ]


def image_attrs(html: str) -> tuple[str, float, float, float, float] | None:
    match = re.search(
        r'<image href="([^"]+)" x="([-\d.]+)" y="([-\d.]+)" width="([\d.]+)" height="([\d.]+)"',
        html,
    )
    return None if match is None else (match[1], *(float(v) for v in match.groups()[1:]))


def view_box(html: str) -> tuple[float, float, float, float]:
    match = re.search(r'<svg class="group-plot group-color-0" viewBox="([-\d. ]+)"', html)
    assert match, html[:400]
    x, y, w, h = (float(v) for v in match[1].split())
    return x, y, w, h


def checkbox(html: str, element_id: str) -> str | None:
    match = re.search(rf'<input type="checkbox" id="{element_id}"[^>]*>', html)
    return None if match is None else match[0]


def layer_classes(html: str) -> dict[str, str]:
    return {
        name: classes
        for classes, name in re.findall(r'<g class="(layer layer-([a-z]+)[^"]*)"', html)
        for classes, name in [(classes, name)]
    }


def correction_text(html: str) -> str | None:
    match = re.search(r'<p class="correction-text[^"]*" id="correction-text">([^<]*)</p>', html)
    return None if match is None else match[1]


def parse_adjustment(text: str) -> tuple[float, float]:
    """(up_mil, right_mil) signed from "Adjust 0.2 mil down, 0.3 mil left." (missing axis is 0)."""
    up = right = 0.0
    for amount, word in re.findall(r"([\d.]+) mil (up|down|right|left)", text):
        value = float(amount)
        if word == "up":
            up = value
        elif word == "down":
            up = -value
        elif word == "right":
            right = value
        else:
            right = -value
    return up, right


# ------------------------------------------------------------------------------------ defaults


class TestToggleMarkupAndDefaults:
    def test_an_unmasked_group_with_a_photo_offers_all_four_toggles_with_the_decided_defaults(
        self, client, conn, settings
    ):
        seeded = photo_group(conn, settings, make_world(conn), ring(0.2, 0.1))

        html = html_of(client, seeded.group.id)

        assert "checked" in checkbox(html, "toggle-photo")
        assert "checked" in checkbox(html, "toggle-circle")
        assert "checked" not in checkbox(html, "toggle-poi")
        assert checkbox(html, "toggle-correction") is not None
        layers = layer_classes(html)
        assert "is-hidden" not in layers["photo"] and "is-hidden" not in layers["circle"]
        assert "is-hidden" in layers["poi"] and "is-hidden" in layers["correction"]
        for label in ("Show photo", "Cone of fire", "Mean POI", "Correction"):
            assert f"> {label}</label>" in html

    def test_a_group_without_a_photo_has_no_photo_toggle_or_image(self, client, conn, settings):
        seeded = photo_group(conn, settings, make_world(conn), ring(0.2, 0.1), with_photo=False)

        html = html_of(client, seeded.group.id)

        assert checkbox(html, "toggle-photo") is None and "<image" not in html
        assert checkbox(html, "toggle-circle") is not None

    def test_a_photo_whose_working_image_is_missing_is_not_offered(self, client, conn, settings):
        seeded = photo_group(conn, settings, make_world(conn), ring(0.2, 0.1), write_image=False)

        html = html_of(client, seeded.group.id)

        assert checkbox(html, "toggle-photo") is None and "<image" not in html

    def test_the_stylesheet_and_scripts_are_linked_and_the_script_count_is_three(
        self, client, conn, settings
    ):
        seeded = photo_group(conn, settings, make_world(conn), ring(0.2, 0.1))

        html = html_of(client, seeded.group.id)

        assert '<link rel="stylesheet" href="/static/css/group.css">' in html
        assert '<script type="module" src="/static/js/analysis.js">' in html
        assert '<script type="module" src="/static/js/group.js">' in html
        assert '<script type="module" src="/static/js/group_context.js">' in html
        assert len(re.findall(r"<script\b", html)) == 3
        assert client.get("/static/css/group.css").headers["content-type"].startswith("text/css")
        assert client.get("/static/js/group.js").status_code == 200

    def test_a_single_shot_group_has_no_spread_circle(self, client, conn, settings):
        seeded = photo_group(conn, settings, make_world(conn), [Offset(0.1, 0.1)])

        html = html_of(client, seeded.group.id)

        assert checkbox(html, "toggle-circle") is None and "spread-circle" not in html


# --------------------------------------------------------------------------------------- photo


class TestPhotoAlignment:
    def test_the_photo_sits_in_the_shots_plane_inch_frame(self, client, conn, settings):
        offs = ring(0.3, -0.2, n=7, radius=0.7)
        seeded = photo_group(conn, settings, make_world(conn), offs, aim=PlanePoint(4.0, 5.0))

        html = html_of(client, seeded.group.id)

        href, x, y, width, height = image_attrs(html)
        assert href == f"/api/photos/{seeded.photo.id}/image?variant=working"
        shots = plot_shots(html)
        assert len(shots) == 7
        mapped = [
            ((cx - x) / width * seeded.width, (cy - y) / height * seeded.height)
            for cx, cy, _ in shots
        ]
        for (mx, my), (hx, hy) in zip(mapped, seeded.holes_px, strict=True):
            assert math.dist((mx, my), (hx, hy)) < 0.25
        assert width == pytest.approx(seeded.width / PPI * PLOT_UNITS_PER_IN, abs=0.01)
        assert height == pytest.approx(seeded.height / PPI * PLOT_UNITS_PER_IN, abs=0.01)
        assert x == pytest.approx(-4.0 * PLOT_UNITS_PER_IN)
        assert y == pytest.approx(-5.0 * PLOT_UNITS_PER_IN)

    def test_the_pixel_under_every_shot_centre_is_the_hole(self, client, conn, settings):
        seeded = photo_group(conn, settings, make_world(conn), ring(0.0, 0.0, n=8, radius=1.0))
        html = html_of(client, seeded.group.id)
        _, x, y, width, height = image_attrs(html)
        image = cv2.imread(
            str(settings.working_dir / f"{seeded.photo.id}.png"), cv2.IMREAD_GRAYSCALE
        )

        for cx, cy, _ in plot_shots(html):
            px = round((cx - x) / width * seeded.width)
            py = round((cy - y) / height * seeded.height)
            assert image[py, px] < 60
            assert image[py, round(px + 3 * HOLE_PX)] > 200

    def test_the_view_covers_the_roi_plus_a_margin_and_not_the_whole_photo(
        self, client, conn, settings
    ):
        offs = ring(0.0, 0.0, n=8, radius=0.5)
        roi = (500, 700, 1300, 1300)
        seeded = photo_group(
            conn, settings, make_world(conn), offs, aim=PlanePoint(5.0, 6.0), roi=roi
        )

        html = html_of(client, seeded.group.id)

        bx, by, bw, bh = view_box(html)
        _, ix, iy, iw, ih = image_attrs(html)
        margin = 0.5 * PLOT_UNITS_PER_IN
        wanted = (
            roi[0] / PPI * 100 - margin - 500,
            roi[1] / PPI * 100 - margin - 600,
            roi[2] / PPI * 100 + margin - 500,
            roi[3] / PPI * 100 + margin - 600,
        )
        assert bx <= max(wanted[0], ix) + 0.2 and by <= max(wanted[1], iy) + 0.2
        assert bx + bw >= min(wanted[2], ix + iw) - 0.2 and by + bh >= min(wanted[3], iy + ih) - 0.2
        assert bw < iw and bh < ih

    def test_the_shots_stay_inside_the_view(self, client, conn, settings):
        seeded = photo_group(
            conn,
            settings,
            make_world(conn),
            ring(0.4, 0.2, n=9, radius=0.9),
            roi=(900, 1000, 1100, 1200),
        )

        html = html_of(client, seeded.group.id)

        bx, by, bw, bh = view_box(html)
        for cx, cy, r in plot_shots(html):
            assert bx <= cx - r and cx + r <= bx + bw and by <= cy - r and cy + r <= by + bh


# -------------------------------------------------------------------------------- spread circle


class TestSpreadCircle:
    def test_the_circle_is_the_minimum_enclosing_circle_of_the_shot_centres(
        self, client, conn, settings
    ):
        offs = [Offset(0.5, 0.2), Offset(-0.4, 0.1), Offset(0.0, -0.6), Offset(0.1, 0.0)]
        seeded = photo_group(conn, settings, make_world(conn), offs, aim=PlanePoint(4.0, 5.0))

        html = html_of(client, seeded.group.id)

        match = re.search(
            r'<circle class="spread-circle" cx="([-\d.]+)" cy="([-\d.]+)" r="([\d.]+)"', html
        )
        cx, cy, r = (float(v) for v in match.groups())
        points = [
            (4.0 + o.dx_in, 5.0 - o.dy_in)  # plane inches of each shot centre
            for o in offs
        ]
        expected = smallest_enclosing_circle(points)
        assert (cx, cy, r) == (
            pytest.approx((expected.cx - 4.0) * 100, abs=0.06),
            pytest.approx((expected.cy - 5.0) * 100, abs=0.06),
            pytest.approx(expected.r * 100, abs=0.06),
        )
        shots = plot_shots(html)
        assert all(math.dist((sx, sy), (cx, cy)) <= r + 0.1 for sx, sy, _ in shots)

    def test_the_legend_names_the_circle_and_gives_its_diameter_next_to_es(
        self, client, conn, settings
    ):
        offs = [Offset(0.5, 0.2), Offset(-0.4, 0.1), Offset(0.0, -0.6)]
        seeded = photo_group(conn, settings, make_world(conn), offs, aim=PlanePoint(4.0, 5.0))
        points = [(4.0 + o.dx_in, 5.0 - o.dy_in) for o in offs]
        diameter = 2 * smallest_enclosing_circle(points).r

        html = html_of(client, seeded.group.id)

        assert f"{diameter:.2f} in across" in html
        legend = html.split('<ul class="plot-legend">', 1)[1].split("</ul>", 1)[0]
        assert "Cone of fire" in legend and "extreme spread" in legend
        es = max(math.dist(a, b) for a in points for b in points)
        assert f"{es:.2f} in" in html
        assert diameter >= es - 1e-9

    def test_two_shots_circle_has_the_extreme_spread_as_its_diameter(self, client, conn, settings):
        offs = [Offset(0.5, 0.0), Offset(-0.5, 0.0)]
        seeded = photo_group(conn, settings, make_world(conn), offs)

        html = html_of(client, seeded.group.id)

        assert "1.00 in across" in html


# -------------------------------------------------------------------------------------- mean POI


class TestMeanPoi:
    def test_the_marker_sits_at_the_mean_offset_in_a_distinct_colour_from_the_aim(
        self, client, conn, settings
    ):
        seeded = photo_group(conn, settings, make_world(conn), ring(0.3, -0.2, n=9, radius=0.5))

        html = html_of(client, seeded.group.id)

        match = re.search(
            r'<g class="layer layer-poi is-hidden" transform="translate\(([-\d.]+) ([-\d.]+)\)"',
            html,
        )
        assert (float(match[1]), float(match[2])) == pytest.approx((30.0, 20.0), abs=0.06)
        app_css = (WEB / "static/css/app.css").read_text()
        poi_color = re.search(r"--poi:\s*(#[0-9a-f]{6})", app_css)[1]
        aim_color = re.search(r"--shot-aim:\s*(#[0-9a-f]{6})", app_css)[1]
        assert poi_color == aim_color == "#ff1a1a"
        assert "Mean point of impact" in html and "Aim point" in html
        layer = re.search(r'<g class="layer layer-poi[^"]*"[^>]*>(.*?)</g>', html, re.DOTALL)[1]
        aim = re.search(r'<g class="aim-mark">(.*?)</g>', html, re.DOTALL)[1]
        assert re.findall(r"<(\w+)", layer) == ["line", "line"]
        assert re.findall(r"<(\w+)", aim) == ["line", "line", "circle"], "the shapes stay distinct"

    def test_a_group_without_an_aim_has_no_poi_toggle_or_marker(self, client, conn, settings):
        seeded = photo_group(conn, settings, make_world(conn), ring(0.3, 0.2), aim=None)

        html = html_of(client, seeded.group.id)

        assert checkbox(html, "toggle-poi") is None and "layer-poi" not in html
        assert 'class="aim-mark"' not in html


# ----------------------------------------------------------------------------- masked groups

MASKED = [
    ("zero_check", 2),
    ("zero_check", 12),
    ("zero_step2", 6),
    ("zero_confirm", 9),
    ("zero_step1", 5),
]


class TestMaskedGroups:
    @pytest.mark.parametrize(("purpose", "n"), MASKED)
    def test_no_poi_toggle_no_aim_mark_and_no_numbers_beyond_n_and_es(
        self, client, conn, settings, purpose, n
    ):
        seeded = photo_group(
            conn, settings, make_world(conn), ring(0.4, 0.3, n=n, radius=0.5), purpose=purpose
        )

        html = html_of(client, seeded.group.id)

        body = main_of(html)
        assert checkbox(html, "toggle-poi") is None and "layer-poi" not in html
        assert 'class="aim-mark"' not in html and "Aim point" not in html
        assert "Mean point of impact" not in html and "poi-mark" not in html
        assert checkbox(html, "toggle-photo") is not None
        assert checkbox(html, "toggle-circle") is not None
        assert "in across" not in html
        legend = html.split('<ul class="plot-legend">', 1)[1].split("</ul>", 1)[0]
        legend_text = re.sub(r"<[^>]+>", "", legend)
        assert "Cone of fire" in legend_text and not re.search(r"\d", legend_text)
        assert not POI_WORDS.search(without_es_mil(body)), POI_WORDS.search(body)

    @pytest.mark.parametrize(("purpose", "n"), MASKED)
    def test_the_photo_is_not_shifted_by_a_hidden_aim(self, client, conn, settings, purpose, n):
        seeded = photo_group(
            conn,
            settings,
            make_world(conn),
            ring(0.4, 0.3, n=n, radius=0.5),
            purpose=purpose,
            aim=PlanePoint(4.0, 5.0),
        )

        html = html_of(client, seeded.group.id)

        _, x, y, _, _ = image_attrs(html)
        shots = plot_shots(html)
        assert abs(sum(s[0] for s in shots) / n) < 0.1 and abs(sum(s[1] for s in shots) / n) < 0.1
        centroid_in = (
            sum(h[0] for h in seeded.holes_px) / n / PPI,
            sum(h[1] for h in seeded.holes_px) / n / PPI,
        )
        assert (x, y) == pytest.approx((-centroid_in[0] * 100, -centroid_in[1] * 100), abs=0.06)


# ---------------------------------------------------------------------------- correction gating

OFFERED = [
    ("general", 10),
    ("general", 11),
    ("zero_step2", 10),
    ("zero_confirm", 10),
    ("zero_step1", 1),
    ("zero_step1", 3),
]
WITHHELD = [
    ("general", 1),
    ("general", 9),
    ("zero_step2", 6),
    ("zero_confirm", 9),
    ("zero_step1", 4),
    ("zero_step1", 10),
    ("zero_check", 2),
    ("zero_check", 10),
]
LEAKS = [
    re.compile(r"\badjust\b", re.IGNORECASE),
    re.compile(r"\d(?:\.\d)? mil (?:up|down)", re.IGNORECASE),
    re.compile(r"turret|No adjustment|layer-correction|correction-card|correction-text"),
    re.compile(r"id=\"correction-info\""),
]


class TestCorrectionGating:
    @pytest.mark.parametrize(("purpose", "n"), OFFERED)
    def test_the_correction_is_offered_for_ten_or_more_shots_or_step_1_up_to_three(
        self, client, conn, settings, purpose, n
    ):
        seeded = photo_group(
            conn, settings, make_world(conn), ring(0.4, 0.3, n=n, radius=0.3), purpose=purpose
        )

        html = html_of(client, seeded.group.id)

        assert "disabled" not in checkbox(html, "toggle-correction")
        assert correction_text(html) and 'id="correction-card"' in html
        assert 'id="correction-why"' not in html

    @pytest.mark.parametrize(("purpose", "n"), WITHHELD)
    def test_otherwise_the_toggle_is_disabled_with_a_fixed_reason_and_no_correction_anywhere(
        self, client, conn, settings, purpose, n
    ):
        seeded = photo_group(
            conn, settings, make_world(conn), ring(0.4, 0.3, n=n, radius=0.3), purpose=purpose
        )

        html = html_of(client, seeded.group.id)

        assert "disabled" in checkbox(html, "toggle-correction")
        why = htmllib.unescape(re.search(r'id="correction-why">([^<]*)<', html)[1])
        assert why in {
            analysis_service.CORRECTION_BLOCKED_TOO_FEW,
            analysis_service.CORRECTION_BLOCKED_STEP1,
            analysis_service.CORRECTION_BLOCKED_ZERO_CHECK,
        }
        assert not re.search(r"\d\.\d|mil|adjust", why, re.IGNORECASE)
        for leak in LEAKS:
            assert not leak.search(html), (leak.pattern, purpose, n)

    def test_the_boundary_is_nine_versus_ten_shots(self, client, conn, settings):
        world = make_world(conn)
        nine = photo_group(conn, settings, world, ring(0.4, 0.3, n=9, radius=0.3), lot_code="A")
        ten = photo_group(conn, settings, world, ring(0.4, 0.3, n=10, radius=0.3), lot_code="B")

        assert "disabled" in checkbox(html_of(client, nine.group.id), "toggle-correction")
        assert "disabled" not in checkbox(html_of(client, ten.group.id), "toggle-correction")

    def test_the_step_1_boundary_is_three_versus_four_shots(self, client, conn, settings):
        world = make_world(conn)
        three = photo_group(
            conn,
            settings,
            world,
            ring(0.4, 0.3, n=3, radius=0.3),
            purpose="zero_step1",
            lot_code="A",
        )
        four = photo_group(
            conn,
            settings,
            world,
            ring(0.4, 0.3, n=4, radius=0.3),
            purpose="zero_step1",
            lot_code="B",
        )

        assert "disabled" not in checkbox(html_of(client, three.group.id), "toggle-correction")
        four_html = html_of(client, four.group.id)
        assert "disabled" in checkbox(four_html, "toggle-correction")
        assert 'class="aim-mark"' not in four_html

    def test_a_ten_shot_group_without_an_aim_point_is_withheld_with_the_aim_reason(
        self, client, conn, settings
    ):
        seeded = photo_group(
            conn, settings, make_world(conn), ring(0.4, 0.3, n=10, radius=0.3), aim=None
        )

        html = html_of(client, seeded.group.id)

        assert "disabled" in checkbox(html, "toggle-correction")
        assert analysis_service.CORRECTION_BLOCKED_NO_AIM in html
        for leak in LEAKS:
            assert not leak.search(html)

    def test_the_gate_function_names_each_reason_once(self):
        gate = analysis_service.correction_gate
        assert gate("general", 10, True) is None and gate("zero_step1", 3, True) is None
        assert gate("general", 9, True) == analysis_service.CORRECTION_BLOCKED_TOO_FEW
        assert gate("zero_step2", 9, True) == analysis_service.CORRECTION_BLOCKED_TOO_FEW
        assert gate("zero_step1", 4, True) == analysis_service.CORRECTION_BLOCKED_STEP1
        assert gate("zero_check", 2, True) == analysis_service.CORRECTION_BLOCKED_ZERO_CHECK
        assert gate("zero_check", 30, True) == analysis_service.CORRECTION_BLOCKED_ZERO_CHECK
        assert gate("general", 10, False) == analysis_service.CORRECTION_BLOCKED_NO_AIM
        assert gate("general", 5, False) == analysis_service.CORRECTION_BLOCKED_TOO_FEW


# ---------------------------------------------------------------------------- correction values


class TestCorrectionValues:
    @pytest.mark.parametrize(
        ("cx", "cy", "expected"),
        [
            (1.08, 0.72, "Adjust 0.2 mil down, 0.3 mil left."),
            (-1.08, -0.72, "Adjust 0.2 mil up, 0.3 mil right."),
            (1.08, -0.72, "Adjust 0.2 mil up, 0.3 mil left."),
            (-1.08, 0.72, "Adjust 0.2 mil down, 0.3 mil right."),
            (0.0, 0.72, "Adjust 0.2 mil down."),
            (1.08, 0.0, "Adjust 0.3 mil left."),
            (0.0, 0.0, "No adjustment needed."),
        ],
    )
    def test_at_100_yd_the_text_is_the_opposite_of_the_mean_poi(
        self, client, conn, settings, cx, cy, expected
    ):
        seeded = photo_group(conn, settings, make_world(conn), ring(cx, cy, n=10, radius=0.3))

        assert correction_text(html_of(client, seeded.group.id)) == expected

    def test_at_300_yd_the_text_names_the_distance_and_uses_that_distances_mils(
        self, client, conn, settings
    ):
        world = make_world(conn, distance_yd=300)
        seeded = photo_group(conn, settings, world, ring(3.24, 2.16, n=10, radius=0.4))

        html = html_of(client, seeded.group.id)

        assert correction_text(html) == "To center at 300 yd: adjust 0.2 mil down, 0.3 mil left."
        card = html.split('id="correction-card"', 1)[1].split("</div>", 1)[0]
        assert "not a change to your zero" in card

    def test_the_100_yd_card_has_no_distance_paragraph(self, client, conn, settings):
        seeded = photo_group(conn, settings, make_world(conn), ring(1.0, 0.5, n=10))

        card = (
            html_of(client, seeded.group.id)
            .split('id="correction-card"', 1)[1]
            .split("</div>", 1)[0]
        )

        assert "not a change to your zero" not in card
        assert "elevation turret" in card and "windage turret" in card and "0.1 mil" in card

    def test_the_dotted_lines_run_vertically_and_horizontally_from_the_poi_to_the_aim(
        self, client, conn, settings
    ):
        seeded = photo_group(conn, settings, make_world(conn), ring(1.08, 0.72, n=10, radius=0.3))

        html = html_of(client, seeded.group.id)

        block = html.split('class="layer layer-correction is-hidden"', 1)[1].split("</g>", 1)[0]
        lines = [
            tuple(float(v) for v in match)
            for match in re.findall(
                r'<line x1="([-\d.]+)" y1="([-\d.]+)" x2="([-\d.]+)" y2="([-\d.]+)"', block
            )
        ]
        assert len(lines) == 2
        vertical, horizontal = lines
        poi = (108.0, -72.0)
        assert vertical == pytest.approx((poi[0], poi[1], poi[0], 0.0), abs=0.06)
        assert horizontal == pytest.approx((poi[0], 0.0, 0.0, 0.0), abs=0.06)

    @pytest.mark.parametrize("seed", range(12))
    def test_the_page_and_the_zeroing_flow_always_agree(self, client, conn, settings, seed):
        rng = random.Random(seed)
        world = make_world(conn)
        step1 = seed % 3 == 0
        n = rng.randint(1, 3) if step1 else rng.randint(10, 25)
        offs = [Offset(rng.gauss(0.3, 0.5), rng.gauss(-0.2, 0.5)) for _ in range(n)]
        origins = [ShotOrigin.DETECTED] * n
        seeded = photo_group(
            conn,
            settings,
            world,
            offs,
            purpose="zero_step1" if step1 else "general",
            origins=origins,
            lot_code=f"LOT-{seed}",
        )

        advice = step1_advice(offs, origins) if step1 else step2_initial_advice(offs, origins)
        page_up, page_right = parse_adjustment(correction_text(html_of(client, seeded.group.id)))

        assert advice.correction is not None
        assert (page_up, page_right) == (advice.correction.up_mil, advice.correction.right_mil)

    def test_the_page_takes_its_numbers_from_the_zeroing_modules_correction_function(
        self, client, conn, settings, monkeypatch
    ):
        from rga.domain.types import Correction

        seeded = photo_group(conn, settings, make_world(conn), ring(0.5, 0.5, n=10))
        assert analysis_service.correction_for is correction_for
        monkeypatch.setattr(
            analysis_service, "correction_for", lambda dx, dy: Correction(1.0, -2.0, "x")
        )

        text = correction_text(html_of(client, seeded.group.id))

        assert text == "Adjust 1.0 mil up, 2.0 mil left."


# ------------------------------------------------------------------------------ static assets


class TestStaticAssets:
    def test_opacity_and_numbers_are_css_class_rules(self):
        css = (WEB / "static/css/group.css").read_text()

        assert re.search(r"\.group-plot\.correction-on \.shot\s*\{[^}]*opacity:\s*0\.25", css)
        assert re.search(r"\.group-plot\.correction-on \.shot-label\s*\{[^}]*display:\s*none", css)
        assert re.search(r"\.layer-correction line\s*\{[^}]*stroke-dasharray", css)
        assert re.search(r"\.layer\.is-hidden\s*\{\s*display:\s*none", css)

    def test_the_script_only_toggles_classes_and_holds_no_numbers_or_directions(self):
        source = (WEB / "static/js/group.js").read_text()
        code = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)

        assert "classList.toggle" in code and "correction-on" in code
        assert not re.search(
            r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|fetch\(", code
        )
        assert not re.search(r"setAttribute\(\s*[\"']style|\.style\b|cssText", code)
        assert not DIRECTION_WORDS.search(code) and not re.search(r"\d\.\d|\bmil\b", code)
        assert not re.search(r"https?://", code)

    def test_the_stylesheet_has_no_inline_style_hooks_or_external_assets(self):
        css = (WEB / "static/css/group.css").read_text()

        assert "url(" not in css and "@import" not in css

    def test_the_template_has_no_inline_script_style_or_handlers(self):
        template = (WEB / "templates/group.html").read_text()

        assert not re.search(r"<script(?![^>]*\bsrc=)", template)
        assert not re.search(r"\sstyle\s*=|\son[a-z]+\s*=|javascript:", template)


# --------------------------------------------------------------------------------- real browser


def _playwright_ready() -> bool:
    import os

    browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""))
    return browsers.is_dir() and any(browsers.glob("chromium*"))


@pytest.fixture
def browser_world():
    """A live server, a Chromium page and a seeder bound to the server's data directory."""
    sync_api = pytest.importorskip("playwright.sync_api")
    from tests.support.live_server import run_live_server

    with run_live_server() as server, sync_api.sync_playwright() as playwright:
        settings = Settings(data_dir=server.data_dir, port=server.port)
        connection = connect(settings.db_path)
        browser = playwright.chromium.launch()
        try:
            world = make_world(connection)
            page = browser.new_page(viewport={"width": 1300, "height": 1000})
            problems: list[str] = []
            page.on("pageerror", lambda error: problems.append(str(error)))
            page.on(
                "console",
                lambda message: problems.append(message.text) if message.type == "error" else None,
            )

            def seed(offs, **kwargs):
                return photo_group(connection, settings, world, offs, **kwargs)

            yield page, server, seed, problems
        finally:
            browser.close()
            connection.close()


def _opacity(page, selector: str) -> list[float]:
    return page.evaluate(
        "(s) => [...document.querySelectorAll(s)].map((e) => Number(getComputedStyle(e).opacity))",
        selector,
    )


def _visible(page, selector: str) -> bool:
    return page.evaluate(
        "(s) => { const e = document.querySelector(s); return !!e && getComputedStyle(e).display !== 'none'; }",
        selector,
    )


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestGroupPlotInARealBrowser:
    def test_the_defaults_and_each_layer_toggle(self, browser_world):
        page, server, seed, problems = browser_world
        seeded = seed(ring(0.3, 0.2, n=10, radius=0.5))
        page.goto(f"{server}/groups/{seeded.group.id}")
        page.wait_for_selector(".group-plot")

        assert _visible(page, ".layer-photo") and _visible(page, ".layer-circle")
        assert not _visible(page, ".layer-poi") and not _visible(page, ".layer-correction")
        assert not _visible(page, "#correction-text")
        page.uncheck("#toggle-photo")
        assert not _visible(page, ".layer-photo") and _visible(page, ".layer-circle")
        page.check("#toggle-photo")
        page.uncheck("#toggle-circle")
        assert not _visible(page, ".layer-circle")
        page.check("#toggle-circle")
        page.check("#toggle-poi")
        assert _visible(page, ".layer-poi")
        page.uncheck("#toggle-poi")
        assert not _visible(page, ".layer-poi")
        assert problems == []

    def test_the_correction_toggle_fades_impacts_hides_numbers_and_brings_poa_and_poi_forward(
        self, browser_world
    ):
        page, server, seed, problems = browser_world
        seeded = seed(ring(1.08, 0.72, n=10, radius=0.4))
        page.goto(f"{server}/groups/{seeded.group.id}")
        page.wait_for_selector(".group-plot")
        assert set(_opacity(page, ".group-plot .shot")) == {1.0}
        assert _visible(page, ".group-plot .shot-label")

        page.check("#toggle-correction")

        assert set(_opacity(page, ".group-plot .shot")) == {0.25}
        assert not _visible(page, ".group-plot .shot-label")
        assert _visible(page, ".layer-poi") and _visible(page, ".layer-correction")
        assert page.inner_text("#correction-text") == "Adjust 0.2 mil down, 0.3 mil left."
        order = page.evaluate(
            """() => {
              const kids = [...document.querySelector('.group-plot').children];
              const at = (sel) => kids.indexOf(document.querySelector(sel));
              const lastShot = Math.max(...kids.map((k, i) => (k.matches('circle.shot') ? i : -1)));
              return {lastShot, lines: at('.layer-correction'), aim: at('g.aim-mark'), poi: at('.layer-poi')};
            }"""
        )
        assert order["lastShot"] < order["lines"] < order["aim"] < order["poi"]
        line_styles = page.evaluate(
            """() => [...document.querySelectorAll('.layer-correction line')].map((l) => {
              const s = getComputedStyle(l);
              return [s.strokeDasharray, Number(l.getAttribute('x1')) === Number(l.getAttribute('x2')),
                      Number(l.getAttribute('y1')) === Number(l.getAttribute('y2'))];
            })"""
        )
        assert [(v, h) for _, v, h in line_styles] == [(True, False), (False, True)]
        assert all(d != "none" for d, _, _ in line_styles)

        page.uncheck("#toggle-correction")
        assert set(_opacity(page, ".group-plot .shot")) == {1.0}
        assert _visible(page, ".group-plot .shot-label") and not _visible(page, "#correction-text")
        assert not _visible(page, ".layer-poi")
        assert problems == []

    def test_the_info_card_opens_from_the_i_button_and_closes_on_escape_or_an_outside_click(
        self, browser_world
    ):
        page, server, seed, problems = browser_world
        seeded = seed(ring(0.5, 0.5, n=10, radius=0.4))
        page.goto(f"{server}/groups/{seeded.group.id}")
        page.wait_for_selector("#correction-info")
        assert not _visible(page, "#correction-card")
        assert page.get_attribute("#correction-info", "aria-expanded") == "false"

        page.click("#correction-info")
        assert _visible(page, "#correction-card")
        assert page.get_attribute("#correction-info", "aria-expanded") == "true"
        assert "elevation turret" in page.inner_text("#correction-card")
        page.keyboard.press("Escape")
        assert not _visible(page, "#correction-card")
        page.click("#correction-info")
        page.click("h1")
        assert not _visible(page, "#correction-card")
        page.click("#correction-info")
        page.click("#correction-info")
        assert not _visible(page, "#correction-card")
        assert problems == []

    @pytest.mark.parametrize(
        ("purpose", "n"), [("general", 9), ("zero_check", 2), ("zero_step1", 4)]
    )
    def test_a_withheld_group_has_a_disabled_toggle_that_does_nothing(
        self, browser_world, purpose, n
    ):
        page, server, seed, problems = browser_world
        seeded = seed(ring(0.5, 0.5, n=n, radius=0.4), purpose=purpose)
        page.goto(f"{server}/groups/{seeded.group.id}")
        page.wait_for_selector(".group-plot")

        assert page.is_disabled("#toggle-correction")
        page.locator("#toggle-correction").click(force=True)
        assert not page.is_checked("#toggle-correction")
        assert set(_opacity(page, ".group-plot .shot")) == {1.0}
        assert page.is_visible("#correction-why")
        assert page.locator("#correction-info").count() == 0
        text = page.inner_text("main")
        assert "Adjust" not in text and "turret" not in text
        assert problems == []

    def test_a_masked_group_has_no_poi_toggle_and_no_aim_mark_in_the_page(self, browser_world):
        page, server, seed, problems = browser_world
        seeded = seed(ring(0.5, 0.5, n=2, radius=0.4), purpose="zero_check")
        page.goto(f"{server}/groups/{seeded.group.id}")
        page.wait_for_selector(".group-plot")

        assert page.locator("#toggle-poi").count() == 0 and page.locator(".aim-mark").count() == 0
        assert page.locator(".layer-poi").count() == 0
        assert page.is_checked("#toggle-photo") and page.is_checked("#toggle-circle")
        page.uncheck("#toggle-circle")
        assert not _visible(page, ".layer-circle")
        assert not POI_WORDS.search(without_es_mil(page.inner_text("main")))
        assert problems == []

    def test_rendered_shot_circles_sit_on_the_holes_of_the_photo(self, browser_world):
        page, server, seed, problems = browser_world
        seeded = seed(ring(0.2, -0.1, n=8, radius=0.9), aim=PlanePoint(4.0, 5.0))
        page.goto(f"{server}/groups/{seeded.group.id}")
        page.wait_for_selector(".group-plot")
        svg = page.locator(".group-plot")
        svg.scroll_into_view_if_needed()
        box = svg.bounding_box()
        centres = page.evaluate(
            """() => [...document.querySelectorAll('.group-plot circle.shot')].map((c) => {
              const r = c.getBoundingClientRect();
              return [r.left + r.width / 2, r.top + r.height / 2, r.width / 2];
            })"""
        )
        page.evaluate(
            """() => {
              for (const e of document.querySelectorAll('.group-plot > :not(.layer-photo)')) {
                e.style.display = 'none';
              }
            }"""
        )
        picture = cv2.imdecode(np.frombuffer(svg.screenshot(), np.uint8), cv2.IMREAD_GRAYSCALE)
        assert len(centres) == 8
        for cx, cy, radius in centres:
            x, y = cx - box["x"], cy - box["y"]
            window = int(radius * 1.6)
            x0, y0 = int(x) - window, int(y) - window
            patch = picture[max(y0, 0) : y0 + 2 * window + 1, max(x0, 0) : x0 + 2 * window + 1]
            weights = (255.0 - patch.astype(np.float64)) ** 2
            ys, xs = np.indices(patch.shape)
            centroid = (
                (xs * weights).sum() / weights.sum() + max(x0, 0),
                (ys * weights).sum() / weights.sum() + max(y0, 0),
            )
            assert weights.sum() > 0
            assert math.dist(centroid, (x, y)) < max(1.5, 0.1 * radius), (centroid, (x, y))
        assert problems == []


# ------------------------------------------------------------------------------- plot visuals

COLOR_CLASS = re.compile(r"\bgroup-color-(\d+)\b")
MAX_STROKE_PX = 1.25


def _css_rule(css: str, selector: str) -> str:
    match = re.search(rf"(?:^|\n){re.escape(selector)}\s*\{{([^}}]*)\}}", css)
    assert match, f"no rule for {selector}"
    return match[1]


class TestPlotVisuals:
    def test_a_groups_shots_share_one_color_whatever_their_origin(self, client, conn, settings):
        origins = [ShotOrigin.DETECTED, ShotOrigin.SPLIT, ShotOrigin.ESTIMATED, ShotOrigin.MANUAL]
        seeded = photo_group(conn, settings, make_world(conn), ring(0.2, 0.1, n=4), origins=origins)

        html = html_of(client, seeded.group.id)

        svg = re.search(r'<svg class="group-plot([^"]*)"', html)[1]
        assert len(COLOR_CLASS.findall(svg)) == 1
        shots = re.findall(r'<circle class="(shot [^"]*)"', html)
        assert [s.split()[1] for s in shots] == [o.value for o in origins]
        assert not any(COLOR_CLASS.search(s) for s in shots)
        app_css = (WEB / "static/css/app.css").read_text()
        rule = _css_rule(app_css, ".shot")
        assert "stroke: var(--group-color" in rule and "fill: var(--group-color" in rule
        assert not re.search(r"\.shot\.(detected|split|manual)\s*\{", app_css)
        assert "stroke-dasharray" in _css_rule(app_css, ".shot.estimated")
        assert "stroke:" not in _css_rule(app_css, ".shot.estimated")

    def test_the_spread_circle_takes_the_groups_color(self):
        css = (WEB / "static/css/group.css").read_text()

        assert "stroke: var(--group-color" in _css_rule(css, ".spread-circle")

    def test_the_poi_marker_is_an_open_x_with_no_filled_shape(self, client, conn, settings):
        seeded = photo_group(conn, settings, make_world(conn), ring(0.3, -0.2, n=9, radius=0.5))

        html = html_of(client, seeded.group.id)

        layer = re.search(r'<g class="layer layer-poi[^"]*"[^>]*>(.*?)</g>', html, re.DOTALL)[1]
        assert re.findall(r"<(\w+)", layer) == ["line", "line"]
        assert layer.count('class="poi-mark"') == 2
        app_css = (WEB / "static/css/app.css").read_text()
        poi = _css_rule(app_css, ".poi-mark")
        assert re.search(r"fill:\s*none", poi) and "fill-opacity" not in poi
        legend = html.split('<ul class="plot-legend">', 1)[1].split("</ul>", 1)[0]
        key = re.search(r'<span class="key key-poi">(.*?)</span>', legend, re.DOTALL)[1]
        assert re.findall(r"<(?!/)(\w+)", key) == ["svg", "line", "line"]

    @pytest.mark.parametrize(
        ("css_file", "selector"),
        [
            ("app.css", ".shot"),
            ("app.css", ".aim-mark"),
            ("app.css", ".poi-mark"),
            ("group.css", ".spread-circle"),
            ("group.css", ".layer-correction line"),
        ],
    )
    def test_marker_strokes_are_thin_and_do_not_scale_with_the_viewbox(self, css_file, selector):
        rule = _css_rule((WEB / "static/css" / css_file).read_text(), selector)

        width = float(re.search(r"stroke-width:\s*([\d.]+)", rule)[1])
        assert width <= MAX_STROKE_PX
        assert "vector-effect: non-scaling-stroke" in rule

    def test_grouped_aim_marker_children_also_use_non_scaling_strokes(self):
        css = (WEB / "static/css/app.css").read_text()

        rule = _css_rule(css, ".aim-mark line, .aim-mark circle")
        assert "vector-effect: non-scaling-stroke" in rule

    def test_the_halo_is_a_class_rule_and_the_stylesheets_stay_csp_clean(self):
        for name in ("app.css", "group.css", "review.css"):
            css = (WEB / "static/css" / name).read_text()
            assert "url(" not in css and "@import" not in css
        app_css = (WEB / "static/css/app.css").read_text()
        assert "--plot-halo: drop-shadow(" in app_css
        assert "filter: var(--plot-halo)" in _css_rule(app_css, ".shot")

    def test_the_palette_has_distinct_colors_and_none_matches_the_aim_or_the_poi(self):
        app_css = (WEB / "static/css/app.css").read_text()

        colors = re.findall(r"--group-color-\d+:\s*(#[0-9a-f]{6})", app_css)
        assert len(colors) >= 6 and len(set(colors)) == len(colors)
        aim = re.search(r"--shot-aim:\s*(#[0-9a-f]{6})", app_css)[1]
        poi = re.search(r"--poi:\s*(#[0-9a-f]{6})", app_css)[1]
        assert aim == poi == "#ff1a1a" and aim not in colors
        for index in range(len(colors)):
            assert (
                f".group-color-{index} {{ --group-color: var(--group-color-{index}); }}" in app_css
            )

    def test_the_correction_view_and_masking_rules_are_unchanged(self, client, conn, settings):
        css = (WEB / "static/css/group.css").read_text()
        assert re.search(r"\.group-plot\.correction-on \.shot\s*\{[^}]*opacity:\s*0\.25", css)
        assert re.search(r"\.group-plot\.correction-on \.shot-label\s*\{[^}]*display:\s*none", css)
        masked = photo_group(
            conn, settings, make_world(conn), ring(0.4, 0.3, n=6), purpose="zero_step2"
        )

        html = html_of(client, masked.group.id)

        assert "layer-poi" not in html and "poi-mark" not in html and 'class="aim-mark"' not in html


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestPlotVisualsInARealBrowser:
    def test_every_shot_of_a_group_draws_in_one_color_and_estimated_ones_are_dashed(
        self, browser_world
    ):
        page, server, seed, problems = browser_world
        origins = [ShotOrigin.DETECTED, ShotOrigin.SPLIT, ShotOrigin.ESTIMATED, ShotOrigin.MANUAL]
        seeded = seed(ring(0.2, 0.1, n=4), origins=origins)
        page.goto(f"{server}/groups/{seeded.group.id}")
        page.wait_for_selector(".group-plot")

        paint = page.evaluate(
            """() => [...document.querySelectorAll('.group-plot .shot')].map((e) => {
              const c = getComputedStyle(e);
              return {stroke: c.stroke, fill: c.fill, width: c.strokeWidth, dash: c.strokeDasharray,
                      cls: e.getAttribute('class')};
            })"""
        )
        assert len(paint) == 4 and len({p["stroke"] for p in paint}) == 1
        assert len({p["fill"] for p in paint}) == 1 and paint[0]["stroke"] != "none"
        assert {p["width"] for p in paint} == {"1.25px"}
        assert [p["dash"] != "none" for p in paint] == [False, False, True, False]
        circle = page.evaluate(
            "() => getComputedStyle(document.querySelector('.spread-circle')).stroke"
        )
        assert circle == paint[0]["stroke"]
        page.check("#toggle-poi")
        marks = page.evaluate(
            """() => [...document.querySelectorAll('.layer-poi .poi-mark')].map((e) => {
              const c = getComputedStyle(e); return [c.fill, c.strokeWidth];
            })"""
        )
        assert marks == [["none", "1.25px"], ["none", "1.25px"]]
        assert problems == []


# --------------------------------------------------------------------- extreme spread in mil


def _es_mil_text(html: str) -> str:
    match = re.search(r'<span class="([^"]*)" id="es-mil">&middot; ([-\d.]+) mil</span>', html)
    assert match, "no extreme spread mil element"
    assert "muted" in match[1].split()
    return match[2]


class TestExtremeSpreadInMil:
    @pytest.mark.parametrize(("distance_yd", "expected"), [(100, "0.42"), (300, "0.14")])
    def test_mil_at_the_session_distance_rounded_to_a_hundredth(
        self, client, conn, settings, distance_yd, expected
    ):
        offs = [Offset(0.75, 0.0), Offset(-0.75, 0.0)]
        world = make_world(conn, distance_yd=distance_yd)
        seeded = photo_group(conn, settings, world, offs)

        html = html_of(client, seeded.group.id)

        assert "1.50 in" in html
        assert _es_mil_text(html) == expected

    def test_a_masked_zeroing_group_shows_it_too_without_any_poi_term(self, client, conn, settings):
        offs = [Offset(0.75, 0.0), Offset(-0.75, 0.0)]
        seeded = photo_group(conn, settings, make_world(conn), offs, purpose="zero_check")

        html = html_of(client, seeded.group.id)

        assert _es_mil_text(html) == "0.42"
        assert not POI_WORDS.search(without_es_mil(main_of(html)))

    def test_the_element_is_small_and_muted_in_the_stylesheet(self):
        css = (WEB / "static/css/group.css").read_text()

        assert re.search(r"\.es-mil\s*\{[^}]*font-size:\s*0\.\d+em", css)


# ----------------------------------------------------------------- thing hitter circle

HITTER_CIRCLE = re.compile(
    r'<circle class="hitter-circle" cx="([-\d.]+)" cy="([-\d.]+)" r="([\d.]+)"'
)
HITTER_OFFSETS = [Offset(0.3, 0.4), Offset(-0.6, 0.8), Offset(0.1, 0.0)]


class TestThingHitterCircle:
    @pytest.mark.parametrize(
        ("distance_yd", "moa", "mil"), [(100, "1.91", "0.56"), (300, "0.64", "0.19")]
    )
    def test_radius_diameter_moa_and_mil_are_computed_about_the_aim(
        self, client, conn, settings, distance_yd, moa, mil
    ):
        world = make_world(conn, distance_yd=distance_yd)
        seeded = photo_group(conn, settings, world, HITTER_OFFSETS)

        html = html_of(client, seeded.group.id)

        cx, cy, r = HITTER_CIRCLE.search(html).groups()
        assert (float(cx), float(cy)) == (0.0, 0.0) and float(r) == pytest.approx(100.0, abs=0.06)
        legend = html.split('<ul class="plot-legend">', 1)[1].split("</ul>", 1)[0]
        text = re.sub(r"<[^>]+>", "", legend)
        assert "“Thing-hitter” circle" in text
        assert f"2.00 in across ({moa} MOA, {mil} mil at {distance_yd} yd)" in text

    def test_the_layer_and_the_toggle_are_off_by_default(self, client, conn, settings):
        seeded = photo_group(conn, settings, make_world(conn), HITTER_OFFSETS)

        html = html_of(client, seeded.group.id)

        box = checkbox(html, "toggle-hitter")
        assert box is not None and "checked" not in box and "disabled" not in box
        assert '<g class="layer layer-hitter is-hidden">' in html

    def test_it_is_a_different_circle_from_the_spread_circle(self, client, conn, settings):
        seeded = photo_group(conn, settings, make_world(conn), ring(0.5, 0.4, n=8, radius=0.3))

        html = html_of(client, seeded.group.id)

        spread = re.search(
            r'<circle class="spread-circle" cx="([-\d.]+)" cy="([-\d.]+)" r="([\d.]+)"', html
        )
        hitter = HITTER_CIRCLE.search(html)
        assert (float(spread[1]), float(spread[2])) != (0.0, 0.0)
        assert float(hitter[3]) > float(spread[3])
        css = (WEB / "static/css/group.css").read_text()
        app_css = (WEB / "static/css/app.css").read_text()
        assert re.search(r"--hitter:\s*(#[0-9a-f]{6})", app_css)[1] not in re.findall(
            r"--(?:group-color-\d+|poi|shot-aim):\s*(#[0-9a-f]{6})", app_css
        )
        hit_rule, spread_rule = _css_rule(css, ".hitter-circle"), _css_rule(css, ".spread-circle")
        assert "var(--hitter)" in hit_rule and "var(--hitter)" not in spread_rule
        assert (
            re.search(r"stroke-dasharray:\s*([^;]+);", hit_rule)[1]
            != re.search(r"stroke-dasharray:\s*([^;]+);", spread_rule)[1]
        )
        assert float(re.search(r"stroke-width:\s*([\d.]+)", hit_rule)[1]) <= MAX_STROKE_PX
        assert "vector-effect: non-scaling-stroke" in hit_rule

    @pytest.mark.parametrize(
        ("kwargs", "n"),
        [
            ({"purpose": "zero_check"}, 2),
            ({"purpose": "zero_step2"}, 6),
            ({"purpose": "zero_confirm"}, 9),
            ({"purpose": "zero_step1"}, 5),
            ({"aim": None}, 10),
        ],
    )
    def test_masked_and_no_aim_groups_emit_no_circle_geometry_or_number(
        self, client, conn, settings, kwargs, n
    ):
        offs = [Offset(0.5 + 0.02 * i, 0.5 - 0.03 * i) for i in range(n)]
        farthest = max(math.hypot(o.dx_in, o.dy_in) for o in offs)
        seeded = photo_group(conn, settings, make_world(conn), offs, **kwargs)

        html = html_of(client, seeded.group.id)

        for leak in ("hitter-circle", "layer-hitter", "key-hitter", "farthest shot center"):
            assert leak not in html
        box = checkbox(html, "toggle-hitter")
        assert box is not None and "disabled" in box and "data-layer" not in box
        assert 'id="hitter-why"' in html and "needs an aim point" in html
        for number in (f"{2 * farthest:.2f}", f"{farthest * PLOT_UNITS_PER_IN:.1f}"):
            assert number not in html, number

    def test_the_reason_text_is_fixed_whichever_gate_applies(self, client, conn, settings):
        world = make_world(conn)
        masked = photo_group(conn, settings, world, ring(0.4, 0.3, n=6), purpose="zero_step2")
        no_aim = photo_group(conn, settings, world, ring(0.2, 0.1), aim=None)

        reasons = {
            re.search(r'id="hitter-why">([^<]*)<', html_of(client, s.group.id))[1]
            for s in (masked, no_aim)
        }

        assert reasons == {analysis_service.HITTER_BLOCKED}


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestThingHitterInARealBrowser:
    def test_the_toggle_shows_the_circle_in_its_own_color_and_keeps_the_correction_view(
        self, browser_world
    ):
        page, server, seed, problems = browser_world
        seeded = seed(ring(0.5, 0.4, n=10, radius=0.3))
        page.goto(f"{server}/groups/{seeded.group.id}")
        page.wait_for_selector(".group-plot")

        assert not page.is_checked("#toggle-hitter") and not _visible(page, ".layer-hitter")
        page.check("#toggle-hitter")
        assert _visible(page, ".layer-hitter")
        strokes = page.evaluate(
            """() => ['.hitter-circle', '.spread-circle'].map((s) => getComputedStyle(document.querySelector(s)).stroke)"""
        )
        assert strokes[0] != strokes[1] and "none" not in strokes
        page.check("#toggle-correction")
        assert _visible(page, ".layer-hitter") and _visible(page, ".layer-correction")
        assert _opacity(page, ".group-plot .shot") == [0.25] * 10
        page.uncheck("#toggle-hitter")
        assert not _visible(page, ".layer-hitter")
        assert problems == []

    def test_a_masked_group_has_a_disabled_toggle_and_no_circle_in_the_dom(self, browser_world):
        page, server, seed, problems = browser_world
        seeded = seed(ring(0.5, 0.5, n=2, radius=0.4), purpose="zero_check")
        page.goto(f"{server}/groups/{seeded.group.id}")
        page.wait_for_selector(".group-plot")

        assert page.is_disabled("#toggle-hitter")
        assert page.locator(".hitter-circle, .layer-hitter, .key-hitter").count() == 0
        assert problems == []


# ------------------------------------------------------------------------------ plot zoom


class TestPlotZoomStatic:
    def test_the_group_page_uses_the_shared_zoom_module_and_holds_no_zoom_logic(self):
        source = (WEB / "static/js/group.js").read_text()
        shared = (WEB / "static/js/svg_zoom.js").read_text()

        assert 'from "/static/js/svg_zoom.js"' in source and "createZoom(plot" in source
        assert not re.search(r'addEventListener\(\s*"wheel"|getScreenCTM|MAX_ZOOM', source)
        assert "MAX_ZOOM = 16" in shared and re.search(r'addEventListener\(\s*"wheel"', shared)
        assert not re.search(r"setAttribute\(\s*[\"']style|\.style\b|cssText|innerHTML", shared)

    def test_the_controls_sit_in_the_plot_frame_and_the_page_loads_the_module_script(
        self, client, conn, settings
    ):
        seeded = photo_group(conn, settings, make_world(conn), ring(0.2, 0.1))

        html = html_of(client, seeded.group.id)

        frame = html.split('<div class="plot-frame">', 1)[1].split('<ul class="plot-legend">', 1)[0]
        for element in ('id="plot-zoom-out"', 'id="plot-zoom-in"', 'id="plot-zoom-fit"'):
            assert element in frame
        assert frame.index("</svg>") < frame.index('class="plot-zoom"')
        assert '<script type="module" src="/static/js/group.js"></script>' in html
        assert client.get("/static/js/svg_zoom.js").status_code == 200


def _plot_view(page) -> dict:
    return page.evaluate(
        """() => { const s = document.querySelector('.group-plot');
          const [x, y, w, h] = s.getAttribute('viewBox').split(' ').map(Number);
          return {x, y, w, h}; }"""
    )


def _unit_under(page, client_x: float, client_y: float) -> tuple[float, float]:
    return tuple(
        page.evaluate(
            """([cx, cy]) => { const s = document.querySelector('.group-plot');
              const p = s.createSVGPoint(); p.x = cx; p.y = cy;
              const m = p.matrixTransform(s.getScreenCTM().inverse()); return [m.x, m.y]; }""",
            [client_x, client_y],
        )
    )


def _level(page) -> str:
    return page.inner_text("#plot-zoom-level")


def _height_on_screen(page, selector: str) -> float:
    return page.evaluate(
        "(s) => document.querySelector(s).getBoundingClientRect().height", selector
    )


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestPlotZoomInARealBrowser:
    def _open(self, browser_world, **kwargs):
        page, server, seed, problems = browser_world
        seeded = seed(ring(0.5, 0.4, n=10, radius=0.3), **kwargs)
        page.goto(f"{server}/groups/{seeded.group.id}")
        page.wait_for_selector(".group-plot")
        return page, problems

    def test_buttons_zoom_around_the_center_and_fit_restores_the_view(self, browser_world):
        page, problems = self._open(browser_world)
        start = _plot_view(page)

        assert _level(page).startswith("1.0")
        page.click("#plot-zoom-in")
        once = _plot_view(page)
        assert once["w"] == pytest.approx(start["w"] / 1.5)
        assert _level(page).startswith("1.5")
        centre = (start["x"] + start["w"] / 2, start["y"] + start["h"] / 2)
        assert once["x"] + once["w"] / 2 == pytest.approx(centre[0], abs=0.5)
        page.click("#plot-zoom-out")
        assert _plot_view(page) == pytest.approx(start)
        page.click("#plot-zoom-in")
        page.click("#plot-zoom-fit")
        assert _plot_view(page) == pytest.approx(start) and _level(page).startswith("1.0")
        assert problems == []

    def test_the_wheel_zooms_around_the_cursor_and_is_clamped_to_1x_to_16x(self, browser_world):
        page, problems = self._open(browser_world)
        start = _plot_view(page)
        box = page.locator(".group-plot").bounding_box()
        cursor = (box["x"] + box["width"] * 0.7, box["y"] + box["height"] * 0.3)
        before = _unit_under(page, *cursor)

        page.mouse.move(*cursor)
        page.mouse.wheel(0, -400)
        zoomed = _plot_view(page)

        assert zoomed["w"] < start["w"] * 0.8
        assert _unit_under(page, *cursor) == pytest.approx(before, abs=0.5)
        for _ in range(12):
            page.mouse.wheel(0, -2000)
        assert _level(page).startswith("16.0")
        assert _plot_view(page)["w"] == pytest.approx(start["w"] / 16)
        for _ in range(12):
            page.mouse.wheel(0, 3000)
        assert _level(page).startswith("1.0") and _plot_view(page) == pytest.approx(start)
        assert problems == []

    def test_dragging_pans_and_the_view_stays_inside_the_plot(self, browser_world):
        page, problems = self._open(browser_world)
        bounds = _plot_view(page)
        for _ in range(4):
            page.click("#plot-zoom-in")
        box = page.locator(".group-plot").bounding_box()
        centre = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        before = _plot_view(page)

        page.mouse.move(*centre)
        page.mouse.down()
        page.mouse.move(centre[0] - 60, centre[1] - 40, steps=6)
        page.mouse.up()
        moved = _plot_view(page)

        assert moved["w"] == before["w"] and (moved["x"], moved["y"]) != (before["x"], before["y"])
        assert moved["x"] > before["x"] and moved["y"] > before["y"]
        page.mouse.move(*centre)
        page.mouse.down()
        page.mouse.move(centre[0] - 5000, centre[1] - 5000, steps=4)
        page.mouse.up()
        edge = _plot_view(page)
        assert edge["x"] + edge["w"] == pytest.approx(bounds["x"] + bounds["w"])
        assert edge["y"] + edge["h"] == pytest.approx(bounds["y"] + bounds["h"])
        page.mouse.move(*centre)
        page.mouse.down()
        page.mouse.move(centre[0] + 9000, centre[1] + 9000, steps=4)
        page.mouse.up()
        origin = _plot_view(page)
        assert (origin["x"], origin["y"]) == pytest.approx((bounds["x"], bounds["y"]))
        assert problems == []

    def test_markers_labels_and_strokes_keep_their_on_screen_size_while_shots_stay_to_scale(
        self, browser_world
    ):
        page, problems = self._open(browser_world)
        page.check("#toggle-poi")
        names = [".layer-poi", "g.aim-mark", ".shot-label"]
        base = [_height_on_screen(page, n) for n in names]
        shot_base = _height_on_screen(page, ".group-plot .shot")

        for _ in range(5):
            page.click("#plot-zoom-in")

        zoomed = [_height_on_screen(page, n) for n in names]
        assert zoomed == pytest.approx(base, rel=0.05)
        assert _height_on_screen(page, ".group-plot .shot") > shot_base * 4
        widths = page.evaluate(
            """() => ['.shot', '.poi-mark', '.spread-circle', '.aim-mark line'].map(
              (s) => getComputedStyle(document.querySelector(s)).strokeWidth)"""
        )
        assert set(widths) == {"1.25px"}
        assert problems == []

    def test_the_correction_view_and_toggles_still_work_while_zoomed(self, browser_world):
        page, problems = self._open(browser_world)
        for _ in range(3):
            page.click("#plot-zoom-in")
        view = _plot_view(page)

        page.check("#toggle-correction")
        assert _opacity(page, ".group-plot .shot") == [0.25] * 10
        assert _visible(page, ".layer-correction") and _visible(page, "#correction-text")
        page.check("#toggle-hitter")
        assert _visible(page, ".layer-hitter") and _visible(page, ".layer-poi")
        assert _plot_view(page) == view
        page.uncheck("#toggle-correction")
        assert _opacity(page, ".group-plot .shot") == [1.0] * 10
        assert problems == []

    def test_the_wheel_only_scrolls_the_page_away_from_the_plot(self, browser_world):
        page, problems = self._open(browser_world)

        prevented = page.evaluate(
            """() => ['.group-plot', 'h1'].map((s) => {
              const e = new WheelEvent('wheel', {deltaY: 120, bubbles: true, cancelable: true});
              document.querySelector(s).dispatchEvent(e);
              return e.defaultPrevented;
            })"""
        )

        assert prevented == [True, False]
        assert problems == []

    def test_a_masked_group_zooms_too_and_shows_no_poi_or_aim(self, browser_world):
        page, problems = self._open(browser_world, purpose="zero_check")
        page.click("#plot-zoom-in")

        assert _level(page).startswith("1.5")
        assert page.locator(".layer-poi, g.aim-mark, .layer-hitter").count() == 0
        assert problems == []


# ------------------------------------------------------------------------------- share card

SHARE_LAYER = re.compile(r'<input type="checkbox" data-share-layer="(\w+)"([^>]*)>')
SHARE_DEFAULTS_URL = "/api/settings/share-defaults"
BUILT_IN_SHARE = {
    "photo": True,
    "spread": True,
    "thing_hitter": False,
    "poi": False,
    "correction": False,
    "numbers": True,
}


def _embedded_defaults(html: str) -> dict:
    import html as html_module
    import json

    attribute = re.search(r'id="share-card"[^>]*data-defaults="([^"]*)"', html)
    assert attribute, "the share card carries no data-defaults"
    return json.loads(html_module.unescape(attribute.group(1)))


def _share_boxes(html: str) -> dict[str, str]:
    return {m[1]: m[2] for m in SHARE_LAYER.finditer(html)}


class TestShareCard:
    def test_every_layer_is_offered_and_enabled_for_a_full_unmasked_group(
        self, client, conn, settings
    ):
        seeded = photo_group(conn, settings, make_world(conn), ring(0.3, 0.2, n=10))

        html = html_of(client, seeded.group.id)

        boxes = _share_boxes(html)
        assert list(boxes) == ["photo", "spread", "thing_hitter", "poi", "correction", "numbers"]
        assert not any("disabled" in rest for rest in boxes.values())
        assert 'id="share-open"' in html and 'aria-controls="share-card"' in html
        assert '<div class="share-card is-hidden" id="share-card"' in html
        assert f'download="group-{seeded.group.id}.png"' in html
        assert f'href="/api/groups/{seeded.group.id}/share.png"' in html
        assert 'id="share-preview"' in html

    def test_a_layer_the_group_cannot_offer_is_disabled_with_the_pages_own_reason(
        self, client, conn, settings
    ):
        seeded = photo_group(
            conn, settings, make_world(conn), ring(0.3, 0.2, n=10), with_photo=False, aim=None
        )

        html = html_of(client, seeded.group.id)

        boxes = _share_boxes(html)
        assert list(boxes) == ["photo", "spread", "thing_hitter", "poi", "correction", "numbers"]
        assert all("disabled" in boxes[k] for k in ("photo", "thing_hitter", "poi", "correction"))
        assert "disabled" not in boxes["spread"]
        assert "This group has no photo." in html
        assert analysis_service.HITTER_BLOCKED in html
        assert analysis_service.CORRECTION_BLOCKED_NO_AIM in html

    @pytest.mark.parametrize("purpose", ["zero_check", "zero_step2"])
    def test_a_masked_group_offers_no_poi_correction_or_thing_hitter(
        self, client, conn, settings, purpose
    ):
        n = 2 if purpose == "zero_check" else 6
        seeded = photo_group(conn, settings, make_world(conn), ring(0.4, 0.3, n=n), purpose=purpose)

        html = html_of(client, seeded.group.id)

        assert list(_share_boxes(html)) == ["photo", "spread", "numbers"]
        card = html.split('id="share-card"', 1)[1].split("</div>", 1)[0]
        assert not POI_WORDS.search(without_es_mil(re.sub(r"<[^>]+>", " ", card)))

    def test_the_card_embeds_the_built_in_defaults_until_the_user_saves_some(
        self, client, conn, settings
    ):
        seeded = photo_group(conn, settings, make_world(conn), ring(0.3, 0.2, n=10))

        assert _embedded_defaults(html_of(client, seeded.group.id)) == BUILT_IN_SHARE

    def test_a_masked_group_is_given_only_the_layers_it_may_show(self, client, conn, settings):
        masked = photo_group(
            conn, settings, make_world(conn), ring(0.4, 0.3, n=2), purpose="zero_check"
        )
        assert (
            client.put(SHARE_DEFAULTS_URL, json=dict.fromkeys(BUILT_IN_SHARE, True)).status_code
            == 200
        )

        assert _embedded_defaults(html_of(client, masked.group.id)) == {
            "photo": True,
            "spread": True,
            "numbers": True,
        }

    def test_the_card_embeds_the_saved_defaults(self, client, conn, settings):
        seeded = photo_group(conn, settings, make_world(conn), ring(0.3, 0.2, n=10))
        saved = {**BUILT_IN_SHARE, "photo": False, "poi": True, "correction": True}
        assert client.put(SHARE_DEFAULTS_URL, json=saved).status_code == 200

        assert _embedded_defaults(html_of(client, seeded.group.id)) == saved

    def test_checkbox_state_comes_from_the_defaults_not_from_the_markup(
        self, client, conn, settings
    ):
        seeded = photo_group(conn, settings, make_world(conn), ring(0.3, 0.2, n=10))

        boxes = _share_boxes(html_of(client, seeded.group.id))

        assert not any("checked" in rest or "data-default" in rest for rest in boxes.values())
        assert not any("data-source" in rest for rest in boxes.values())

    def test_the_script_is_a_small_module_that_draws_the_preview_from_the_share_route(self):
        group_js = (WEB / "static/js/group.js").read_text()
        card_js = (WEB / "static/js/share_card.js").read_text()

        assert 'from "/static/js/share_card.js"' in group_js and "wireShare()" in group_js
        assert "/share.png?layers=" in card_js
        assert "dataset.defaults" in card_js and "toggle-" not in card_js
        assert not re.search(
            r"fetch\(|innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(", card_js
        )
        assert not re.search(r"setAttribute\(\s*[\"']style|\.style\b|cssText|https?://", card_js)

    def test_the_card_styles_hide_it_by_class_and_load_no_external_assets(self):
        css = (WEB / "static/css/group.css").read_text()

        assert re.search(r"\.share-card\.is-hidden\s*[,{]", css)
        assert "url(" not in css


@pytest.mark.e2e
@pytest.mark.skipif(not _playwright_ready(), reason="Playwright browsers are not installed")
class TestShareCardInARealBrowser:
    def _open(self, browser_world, **kwargs):
        page, server, seed, problems = browser_world
        seeded = seed(ring(0.5, 0.4, n=10, radius=0.3), **kwargs)
        page.goto(f"{server}/groups/{seeded.group.id}")
        page.wait_for_selector(".group-plot")
        return page, server, seeded, problems

    @staticmethod
    def _preview_ready(page) -> None:
        from playwright.sync_api import expect

        expect(page.locator("#share-status")).to_have_text("")
        assert page.evaluate("() => document.getElementById('share-preview').naturalWidth") > 0

    def test_open_preset_toggle_preview_and_download(self, browser_world):
        from playwright.sync_api import expect

        page, server, seeded, problems = self._open(browser_world)
        page.check("#toggle-poi")
        assert not _visible(page, "#share-card")

        page.click("#share-open")

        assert _visible(page, "#share-card")
        assert page.get_attribute("#share-open", "aria-expanded") == "true"
        checked = page.evaluate(
            """() => Object.fromEntries([...document.querySelectorAll('[data-share-layer]')]
              .map((b) => [b.dataset.shareLayer, b.checked]))"""
        )
        assert checked == BUILT_IN_SHARE, "the page's own toggles no longer preset the card"
        self._preview_ready(page)
        source = page.get_attribute("#share-preview", "src")
        assert source == f"/api/groups/{seeded.group.id}/share.png?layers=photo,spread,numbers"
        assert page.get_attribute("#share-download", "download") == f"group-{seeded.group.id}.png"
        assert page.get_attribute("#share-download", "href") == source

        page.uncheck('[data-share-layer="photo"]')
        page.check('[data-share-layer="thing_hitter"]')
        page.check('[data-share-layer="poi"]')
        expect(page.locator("#share-preview")).to_have_attribute(
            "src", f"/api/groups/{seeded.group.id}/share.png?layers=spread,thing_hitter,poi,numbers"
        )
        self._preview_ready(page)
        href = page.get_attribute("#share-download", "href")
        assert href == page.get_attribute("#share-preview", "src")

        response = page.request.get(f"{server}{href}")
        assert response.status == 200
        assert response.headers["content-type"] == "image/png"
        assert response.body()[:8] == b"\x89PNG\r\n\x1a\n"
        assert set(response.headers["x-share-layers"].split(",")) >= {"spread", "poi"}

        page.click("#share-open")
        assert not _visible(page, "#share-card")
        assert problems == []

    @staticmethod
    def _save_defaults(page, server, values: dict) -> None:
        import json

        response = page.request.put(
            f"{server}{SHARE_DEFAULTS_URL}",
            data=json.dumps(values),
            headers={"Content-Type": "application/json", "Origin": str(server).rstrip("/")},
        )
        assert response.status == 200, response.text()

    @staticmethod
    def _checked(page) -> dict:
        return page.evaluate(
            """() => Object.fromEntries([...document.querySelectorAll('[data-share-layer]')]
              .map((b) => [b.dataset.shareLayer, b.checked]))"""
        )

    def test_the_card_opens_with_the_saved_defaults(self, browser_world):
        page, server, seed, problems = browser_world
        seeded = seed(ring(0.5, 0.4, n=10, radius=0.3))
        saved = {
            "photo": False,
            "spread": False,
            "thing_hitter": True,
            "poi": True,
            "correction": True,
            "numbers": False,
        }
        self._save_defaults(page, server, saved)
        page.goto(f"{server}/groups/{seeded.group.id}")
        page.wait_for_selector(".group-plot")

        page.click("#share-open")

        assert self._checked(page) == saved
        self._preview_ready(page)
        assert page.get_attribute("#share-preview", "src") == (
            f"/api/groups/{seeded.group.id}/share.png?layers=thing_hitter,poi,correction"
        )
        assert problems == []

    def test_each_opening_returns_to_the_saved_defaults_whatever_was_toggled(self, browser_world):
        page, server, seeded, problems = self._open(browser_world)
        page.click("#share-open")
        page.uncheck('[data-share-layer="photo"]')
        page.check('[data-share-layer="correction"]')
        page.click("#share-open")
        assert not _visible(page, "#share-card")

        page.click("#share-open")

        assert self._checked(page) == BUILT_IN_SHARE
        assert problems == []

    def test_a_layer_the_group_cannot_offer_stays_unchecked_and_disabled_whatever_is_saved(
        self, browser_world
    ):
        page, server, seed, problems = browser_world
        seeded = seed(ring(0.5, 0.4, n=10, radius=0.3), with_photo=False, aim=None)
        self._save_defaults(page, server, dict.fromkeys(BUILT_IN_SHARE, True))
        page.goto(f"{server}/groups/{seeded.group.id}")
        page.wait_for_selector(".group-plot")

        page.click("#share-open")

        checked = self._checked(page)
        disabled = page.evaluate(
            """() => Object.fromEntries([...document.querySelectorAll('[data-share-layer]')]
              .map((b) => [b.dataset.shareLayer, b.disabled]))"""
        )
        assert disabled == {
            "photo": True,
            "spread": False,
            "thing_hitter": True,
            "poi": True,
            "correction": True,
            "numbers": False,
        }
        assert checked == {
            "photo": False,
            "spread": True,
            "thing_hitter": False,
            "poi": False,
            "correction": False,
            "numbers": True,
        }
        self._preview_ready(page)
        assert page.get_attribute("#share-preview", "src").endswith(
            "share.png?layers=spread,numbers"
        )
        assert problems == []

    def test_a_masked_group_ignores_saved_poi_and_correction_defaults(self, browser_world):
        page, server, seed, problems = browser_world
        seeded = seed(ring(0.5, 0.4, n=2, radius=0.3), purpose="zero_check")
        self._save_defaults(page, server, dict.fromkeys(BUILT_IN_SHARE, True))
        page.goto(f"{server}/groups/{seeded.group.id}")
        page.wait_for_selector(".group-plot")

        page.click("#share-open")

        assert list(self._checked(page)) == ["photo", "spread", "numbers"]
        self._preview_ready(page)
        source = page.get_attribute("#share-preview", "src")
        assert not any(word in source for word in ("poi", "correction", "thing_hitter"))
        assert problems == []

    def test_rapid_toggling_draws_one_preview_not_one_per_click(self, browser_world):
        page, server, seeded, problems = self._open(browser_world)
        requests = []
        page.on("request", lambda r: requests.append(r.url) if "share.png" in r.url else None)
        page.click("#share-open")
        page.check('[data-share-layer="poi"]')
        page.uncheck('[data-share-layer="photo"]')
        page.uncheck('[data-share-layer="spread"]')
        self._preview_ready(page)

        assert page.get_attribute("#share-download", "href") == page.get_attribute(
            "#share-preview", "src"
        )
        assert len(requests) <= 2
        assert requests[-1].endswith("share.png?layers=poi,numbers")
        assert problems == []

    def test_no_layers_chosen_asks_for_the_shots_only(self, browser_world):
        page, server, seeded, problems = self._open(browser_world)
        page.click("#share-open")
        page.uncheck('[data-share-layer="photo"]')
        page.uncheck('[data-share-layer="spread"]')
        page.uncheck('[data-share-layer="numbers"]')
        self._preview_ready(page)

        assert page.get_attribute("#share-download", "href") == (
            f"/api/groups/{seeded.group.id}/share.png?layers="
        )
        assert (
            page.request.get(f"{server}{page.get_attribute('#share-download', 'href')}").status
            == 200
        )
        assert problems == []

    def test_a_masked_group_offers_only_photo_and_spread(self, browser_world):
        page, server, seeded, problems = self._open(browser_world, purpose="zero_check")

        page.click("#share-open")

        layers = page.evaluate(
            "() => [...document.querySelectorAll('[data-share-layer]')].map((b) => b.dataset.shareLayer)"
        )
        assert layers == ["photo", "spread", "numbers"]
        self._preview_ready(page)
        assert "poi" not in page.get_attribute("#share-preview", "src")
        assert problems == []


# ------------------------------------------------------- red markers, spread legend, numbers


class TestRedMarkersAndSpreadLegend:
    def test_the_aim_and_poi_markers_are_bright_red_on_every_plot_and_the_palette_has_no_red(self):
        app_css = (WEB / "static/css/app.css").read_text()
        review_css = (WEB / "static/css/review.css").read_text()

        assert re.search(r"--shot-aim:\s*#ff1a1a", app_css) and re.search(
            r"--poi:\s*#ff1a1a", app_css
        )
        assert "stroke: var(--shot-aim)" in _css_rule(app_css, ".aim-mark")
        assert "stroke: var(--poi)" in _css_rule(app_css, ".poi-mark")
        assert "stroke: var(--shot-aim)" in _css_rule(review_css, ".review-editor .aim-candidate")
        analysis = (WEB / "static/js/analysis.js").read_text()
        assert '"aim-mark"' in analysis and '"poi-mark"' in analysis, (
            "the overlay reuses the classes"
        )
        assert not re.search(r"#ff1a1a", analysis + (WEB / "static/js/svg_editor.js").read_text())
        for colour in re.findall(r"--group-color-\d+:\s*#([0-9a-f]{6})", app_css):
            red, green, blue = (int(colour[i : i + 2], 16) for i in (0, 2, 4))
            assert not (red > 200 and green < 100 and blue < 100), colour

    @pytest.mark.parametrize(
        ("distance_yd", "moa", "mil"), [(100, "1.91", "0.56"), (300, "0.64", "0.19")]
    )
    def test_the_spread_legend_gives_inches_moa_and_mil_at_the_distance(
        self, client, conn, settings, distance_yd, moa, mil
    ):
        offs = [Offset(1.0, 0.0), Offset(-1.0, 0.0)]
        seeded = photo_group(conn, settings, make_world(conn, distance_yd=distance_yd), offs)

        html = html_of(client, seeded.group.id)

        legend = html.split('<ul class="plot-legend">', 1)[1].split("</ul>", 1)[0]
        text = re.sub(r"<[^>]+>", "", legend)
        assert f"2.00 in across ({moa} MOA, {mil} mil at {distance_yd} yd)" in text
        assert re.search(r"Cone of fire: .*?2\.00 in across \(", text)

    @pytest.mark.parametrize("purpose", ["zero_check", "zero_step2"])
    def test_a_masked_group_keeps_a_spread_legend_without_a_size(
        self, client, conn, settings, purpose
    ):
        n = 2 if purpose == "zero_check" else 6
        seeded = photo_group(conn, settings, make_world(conn), ring(0.4, 0.3, n=n), purpose=purpose)

        html = html_of(client, seeded.group.id)

        legend = html.split('<ul class="plot-legend">', 1)[1].split("</ul>", 1)[0]
        text = re.sub(r"<[^>]+>", "", legend)
        assert "Cone of fire" in text and not re.search(r"\d|across|MOA", text)

    def test_the_share_card_offers_shot_numbers_on_by_default_even_for_a_masked_group(
        self, client, conn, settings
    ):
        full = photo_group(conn, settings, make_world(conn), ring(0.3, 0.2, n=10))
        masked = photo_group(
            conn, settings, make_world(conn), ring(0.4, 0.3, n=2), purpose="zero_check"
        )

        for seeded in (full, masked):
            html = html_of(client, seeded.group.id)
            box = re.search(r'<input type="checkbox" data-share-layer="numbers"([^>]*)>', html)
            assert box and "disabled" not in box[1]
            assert _embedded_defaults(html)["numbers"] is True
            assert "Shot numbers" in html
