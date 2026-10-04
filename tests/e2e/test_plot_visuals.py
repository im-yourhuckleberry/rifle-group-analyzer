"""Plot visuals (B-60): one color per group, a distinct color per overlaid group with a legend, a
light open POI marker, and thin non-scaling strokes on the analysis overlay and the review editor.
"""

from __future__ import annotations

import math
import re

import pytest

from rga.domain.types import Offset, ShotOrigin
from tests.e2e.pages import App
from tests.support import seed

pytestmark = pytest.mark.e2e

COLOR_CLASS = re.compile(r"group-color-(\d+)")
STROKE_WIDTH = "1.25px"


def ring(cx: float, cy: float, n: int = 10, radius: float = 0.35) -> list[Offset]:
    return [
        Offset(
            cx + radius * math.cos(2 * math.pi * i / n), cy + radius * math.sin(2 * math.pi * i / n)
        )
        for i in range(n)
    ]


def overlay_paint(app: App) -> list[dict]:
    return app.page.evaluate(
        """() => [...document.querySelectorAll('#aggregate-plot circle.shot')].map((e) => {
          const c = getComputedStyle(e);
          return {cls: e.getAttribute('class'), stroke: c.stroke, fill: c.fill,
                  width: c.strokeWidth, dash: c.strokeDasharray};
        })"""
    )


def test_overlaid_groups_get_distinct_colors_a_legend_and_an_open_center_marker(app: App) -> None:
    rifle = app.create_rifle(name="Overlay rifle")
    load = app.create_load(rifle, label="Overlay load")
    ctx = app.group_context(rifle, distance_yd=100)
    with app.seeding() as conn:
        ids = [
            seed.seed_group(
                conn,
                context=ctx,
                load=load,
                offsets=ring(dx, dy),
                lot_code=f"LOT-{n}",
                origins=[ShotOrigin.ESTIMATED] + [ShotOrigin.DETECTED] * 9 if n == 1 else None,
            ).id
            for n, (dx, dy) in enumerate([(-0.4, 0.2), (0.3, -0.3), (0.0, 0.5)])
        ]

    app.analysis_open()
    app.analysis_pick(ids)
    assert app.analysis_aggregate() is not None
    paint = overlay_paint(app)
    assert len(paint) == 30

    classes = [COLOR_CLASS.search(p["cls"])[1] for p in paint]
    per_group = [set(classes[i : i + 10]) for i in range(0, 30, 10)]
    assert all(len(group) == 1 for group in per_group), "one color class per group"
    slots = [next(iter(group)) for group in per_group]
    assert len(set(slots)) == 3, "each group has its own color class"
    strokes = [paint[i * 10]["stroke"] for i in range(3)]
    assert len(set(strokes)) == 3 and "none" not in strokes
    assert all(p["stroke"] == strokes[i // 10] for i, p in enumerate(paint))
    assert {p["width"] for p in paint} == {STROKE_WIDTH}
    estimated = [p for p in paint if "estimated" in p["cls"].split()]
    assert len(estimated) == 1 and estimated[0]["dash"] != "none"
    assert all(p["dash"] == "none" for p in paint if p not in estimated)
    assert (
        len({p["stroke"] for p in paint if p["cls"].split()[-1] == estimated[0]["cls"].split()[-1]})
        == 1
    )

    items = app.page.locator("#aggregate-legend li")
    texts = items.all_inner_texts()
    assert len(texts) == 5
    for index, group_id in enumerate(ids):
        assert f"Group #{group_id}" in texts[index] and f"LOT-{index}" in texts[index]
        key_class = items.nth(index).locator(".key").get_attribute("class")
        assert f"group-color-{slots[index]}" in key_class
        key_stroke = items.nth(index).locator("circle").evaluate("e => getComputedStyle(e).stroke")
        assert key_stroke == strokes[index]
    assert texts[3] == "Aim point" and texts[4] == "Combined center"

    marks = app.page.evaluate(
        """() => [...document.querySelectorAll('#aggregate-plot .poi-mark')].map((e) => {
          const c = getComputedStyle(e); return [e.tagName, c.fill, c.strokeWidth];
        })"""
    )
    assert marks == [["line", "none", STROKE_WIDTH]] * 2
    assert app.page.locator("#aggregate-plot circle:not(.shot)").count() == 0
    assert app.page.locator("#aggregate-plot .aim-mark").count() == 1


def test_a_single_group_overlay_still_draws_one_color_and_one_legend_entry(app: App) -> None:
    rifle = app.create_rifle()
    load = app.create_load(rifle)
    ctx = app.group_context(rifle, distance_yd=100)
    with app.seeding() as conn:
        group = seed.seed_group(conn, context=ctx, load=load, offsets=ring(0.1, 0.1)).id
        other = seed.seed_group(conn, context=ctx, load=load, offsets=ring(-0.1, 0.0)).id
    app.analysis_open()
    app.analysis_pick([group, other])
    assert app.analysis_aggregate() is not None
    assert app.page.locator("#aggregate-legend li").count() == 4
    app.analysis_pick([group])
    assert app.analysis_aggregate() is not None
    assert app.page.locator("#aggregate-legend li").count() == 3
    assert len({p["stroke"] for p in overlay_paint(app)}) == 1


def test_review_editor_shots_share_one_color_with_estimated_ones_dashed(app: App) -> None:
    rifle = app.create_rifle(cartridge="223 Remington", bullet_diameter_in=0.224)
    app.create_load(rifle)
    ctx = app.group_context(rifle)
    app.photo_group_to_review(ctx, "ragged_10x7")
    paint = app.page.evaluate(
        """() => [...document.querySelectorAll('#editor-svg circle.shot')].map((e) => {
          const c = getComputedStyle(e);
          return {origin: e.getAttribute('class').split(' ')[1], stroke: c.stroke,
                  width: c.strokeWidth, dash: c.strokeDasharray};
        })"""
    )
    assert len(paint) == 10
    assert len({p["stroke"] for p in paint}) == 1 and paint[0]["stroke"] != "none"
    assert {p["width"] for p in paint} == {STROKE_WIDTH}
    assert all((p["dash"] != "none") == (p["origin"] == "estimated") for p in paint)
    assert sum(p["origin"] == "estimated" for p in paint) == 3
    zoom_before = app.review_zoom_to(3.0)
    assert zoom_before >= 3.0
    widths = app.page.evaluate(
        "() => [...document.querySelectorAll('#editor-svg circle.shot')].map((e) => getComputedStyle(e).strokeWidth)"
    )
    assert set(widths) == {STROKE_WIDTH}, "strokes keep their on-screen width at any zoom"


def test_aim_marker_lines_keep_a_thin_non_scaling_stroke_on_the_overlay(app: App) -> None:
    rifle = app.create_rifle()
    load = app.create_load(rifle)
    ctx = app.group_context(rifle, distance_yd=100)
    with app.seeding() as conn:
        ids = [
            seed.seed_group(conn, context=ctx, load=load, offsets=ring(dx, 0.1)).id
            for dx in (-0.2, 0.2)
        ]
    app.analysis_open()
    app.analysis_pick(ids)
    assert app.analysis_aggregate() is not None
    effects = app.page.evaluate(
        """() => [...document.querySelectorAll('#aggregate-plot .aim-mark line')].map((e) => {
          const c = getComputedStyle(e); return [c.vectorEffect, c.strokeWidth];
        })"""
    )
    assert effects == [["non-scaling-stroke", STROKE_WIDTH]] * 2
