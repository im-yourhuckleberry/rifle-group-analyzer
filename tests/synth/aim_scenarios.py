"""Aim-mark scenes for `rga.vision.aim`: catalog scenarios plus derived pen crosses and bare grids.

`aim_scene(name, seed, group)` renders one scenario with the synthetic generator, rectifies it with
the truth homography and returns the working image, a group ROI built from that group's hole
positions (the user's region, not the truth ROI, which always contains the aim), the working ppi and
the true aim in working px. The derived scenarios reuse generator features only (overlay lines for
pen crosses, no aim mark on a printed grid), and `catalog.json` is left untouched so the detection
gate keeps its scenario set.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from functools import cache

import numpy as np

from rga.vision.transform import apply_h
from tests.synth.generate import load_catalog, plane_to_image, render
from tools.eval_detection import working_from_truth

ROI_MARGIN_IN = 0.2
PEN_ARM_IN = 0.5
PEN_THICKNESS_IN = 0.03


@dataclass(frozen=True)
class AimScene:
    """A rectified synthetic target: group `roi`, true `aim_px` and the group's `holes_px` (working px)."""

    working: np.ndarray
    roi: tuple[int, int, int, int]
    ppi: float
    aim_px: tuple[float, float]
    holes_px: tuple[tuple[float, float], ...] = ()


def _pen_cross(center: list[float], kind: str) -> list[dict]:
    cx, cy = center
    if kind == "plus":
        ends = [
            ([cx - PEN_ARM_IN, cy], [cx + PEN_ARM_IN, cy]),
            ([cx, cy - PEN_ARM_IN], [cx, cy + PEN_ARM_IN]),
        ]
    else:
        a = PEN_ARM_IN / math.sqrt(2)
        ends = [([cx - a, cy - a], [cx + a, cy + a]), ([cx - a, cy + a], [cx + a, cy - a])]
    return [
        {
            "kind": "line",
            "p0_in": p0,
            "p1_in": p1,
            "thickness_in": PEN_THICKNESS_IN,
            "color": "black",
        }
        for p0, p1 in ends
    ]


def derived_scenarios() -> dict[str, dict]:
    """Scenarios built from catalog ones: pen plus and X crosses, and grids with no aim mark."""
    catalog = load_catalog()
    out: dict[str, dict] = {}
    for base, name in (
        ("clean_grid", "grid_crossing_clean"),
        ("skew10_grid", "grid_crossing_skew10"),
        ("curved_green_grid", "grid_crossing_curved"),
    ):
        scenario = copy.deepcopy(catalog[base])
        scenario["aim_dots"], scenario["overlay_marks"] = [], []
        out[name] = scenario
    for base, name, kind in (
        ("clean_circle1", "pen_plus_circle_sheet", "plus"),
        ("clean_circle1", "pen_x_circle_sheet", "x"),
        ("clean_grid", "pen_plus_grid_sheet", "plus"),
        ("noisy_lighting", "pen_x_noisy", "x"),
    ):
        scenario = copy.deepcopy(catalog[base])
        scenario["aim_dots"] = []
        scenario["overlay_marks"] = _pen_cross(scenario["groups"][0]["aim_in"], kind)
        out[name] = scenario
    return out


@cache
def _scenarios() -> dict[str, dict]:
    return {**load_catalog(), **derived_scenarios()}


def scenario_names() -> list[str]:
    """Every scenario `aim_scene` can render: the catalog's and the derived ones."""
    return list(_scenarios())


@cache
def aim_scene(name: str, seed: int = 0, group: int = 0) -> AimScene:
    """Render scenario `name` and return group `group`'s region and true aim in working px."""
    image, truth = render(_scenarios()[name], seed)
    working, frame, h = working_from_truth(image, truth)
    holes = np.array(truth["holes_in"], dtype=np.float64).reshape(-1, 2)
    spec = truth["groups"][group]
    pixels = apply_h(h, plane_to_image(truth, holes[spec["hole_indices"]]))
    margin = ROI_MARGIN_IN * frame.ppi
    low, high = pixels.min(axis=0) - margin, pixels.max(axis=0) + margin
    roi = (
        int(max(0, low[0])),
        int(max(0, low[1])),
        int(min(frame.width_px, high[0])),
        int(min(frame.height_px, high[1])),
    )
    aim = apply_h(h, plane_to_image(truth, np.array([spec["aim_in"]], dtype=np.float64)))[0]
    every = apply_h(h, plane_to_image(truth, holes)) if len(holes) else np.empty((0, 2))
    return AimScene(
        working,
        roi,
        frame.ppi,
        (float(aim[0]), float(aim[1])),
        tuple((float(x), float(y)) for x, y in every[spec["hole_indices"]]),
    )
