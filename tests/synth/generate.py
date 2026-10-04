"""Synthetic target-photo generator with exact ground truth (design.md section 7).

Library use:
    img_bgr, truth = render(scenario_dict, seed)          # deterministic per (scenario, seed)
    img_bgr, truth = render_named("clean_circle1", seed)  # scenario looked up in catalog.json
CLI:
    python -m tests.synth.generate --scenario <name> --seed <n> --out <dir>
    python -m tests.synth.generate --list

Geometry is defined on a paper plane in inches (origin top-left of the sheet, x right, y DOWN,
matching design section 3) and drawn through the truth homography `h_plane_to_image`, so every
printed element and hole lands at an exactly known image position. Depends only on numpy, OpenCV
and the standard library.

Truth keys (JSON-serializable):
  Core: scenario, seed, ppi, bullet_diameter_in, image_size [w, h], paper_in, h_plane_to_image (3x3),
    holes_in (plane inches, y down), ragged_groups (hole indices per ragged blob),
    ragged_outlines_in (plane polygon per ragged blob), aim_in, aim_px, groups (per group: aim_in,
    aim_px, hole_indices, roi_px [x0, y0, x1, y1]; roi_px contains every hole of the group,
    including its radius).
  merged_pairs: {indices, separation_d, core_gap_in, cores_overlap}; core_gap_in is the smallest
    distance between the two dark core polygons and cores_overlap is core_gap_in <= 0, so a pair
    with a positive gap can render as two components.
  decoys_in: {center_in, radius_in} hole-sized dark marks that are not shots.
  holes_light_core (per hole): True when the core is rendered light, either on a black region or
    as a light-core hole (tan backing inside a dark rim). holes_wipe (per hole): bullet-wipe ring.
  reference: null, a circle (kind, diameter_in, center_in, center_px, ellipse_center_px, axes_px,
    angle_deg, axis_ratio, px_per_in_at_ref) or a grid (kind, spacing_in, squares_across,
    squares_down, corners_in, corners_px, line_color, line_bgr, band). Grid corners are labelled
    in plane coordinates (TL, TR, BR, BL of the sheet), so on a 180 degree sheet corners_px[0] is at
    the image bottom-right. line_color is blue, black, red or green; band is null or
    {orientation, center_in, width_in} (a thick bar printed in the line color).
  Real-photo traits (real-photo-notes.md): hole_style ("dark", "light_core" or "mixed_disc": light
    core only inside the sticker disc), background ("paper" or "cardboard"), paper_bgr,
    non_planar (bool) with non_planar_warp ({amplitude_in} or null), print_decoys_in ({kind "qr" or
    "text", rect_in [x0, y0, x1, y1], text}), staples_in ({center_in, length_in, angle_deg}),
    wrinkles_in ({p0_in, p1_in, width_in, strength} dark ridges with a lighter flank),
    creases_in (polylines on cardboard), corrugation_period_in (null on paper), aim_marks_in
    ({kind, center_in}: printed dots, overlay diamonds, user tape crosses).
  Curved sheets: with non_planar true, holes_in and every plane-inch truth are positions on the
    flat sheet; the render shows them through the bow. Map any plane point to the image with
    `plane_to_image(truth, pts_in)`, which applies the warp then h_plane_to_image. The
    four corners_px therefore do not explain the interior grid lines.

Scenario fields beyond the basics: reference.line_color/band/allow_holes/rim_clear_in,
paper_bgr, background, hole_style, non_planar {amplitude_in}, print_decoys, staples, wrinkles,
overlay_marks (diamond, ring, line, tape_cross; a diamond keeps holes out of its area),
n_creases, corrugation_period_in, groups[].ragged_at_center. New features draw from a separate RNG
stream, so scenarios that do not use them render byte-identically across versions.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import zlib
from pathlib import Path

import cv2
import numpy as np

CATALOG_PATH = Path(__file__).with_name("catalog.json")

_SHIFT = 4
_FIXED = 1 << _SHIFT

PAPER_BGR = (232, 238, 242)
TABLE_BGR = (150, 155, 160)
GRID_LINE_BGR = (200, 150, 90)
REFERENCE_BGR = (40, 70, 235)
BLACK_BGR = (18, 18, 18)
SPLATTER_BGR = (40, 255, 200)
GRID_LINE_COLORS_BGR = {
    "blue": GRID_LINE_BGR,
    "black": (35, 35, 35),
    "red": (60, 60, 225),
    "green": (60, 170, 40),
}
CARDBOARD_BGR = (115, 155, 190)
CARDBOARD_STRIPE_BGR = (100, 138, 172)
CREASE_GRAY = 70
TAN_CORE_BGR = (135, 175, 200)
TAPE_BLUE_BGR = (220, 140, 60)
QR_MODULES = 21
HOLE_STYLES = ("dark", "light_core", "mixed_disc")
OVERLAY_KINDS = ("diamond", "ring", "line", "tape_cross")
PRINT_KINDS = ("qr", "text")
INK_COLORS_BGR = {"black": BLACK_BGR, "red": (40, 40, 215), "green": (60, 170, 40)}
DOT_COLORS_BGR = {
    "orange": (30, 140, 250),
    "green": (70, 170, 60),
    "blue": (230, 140, 60),
    "black": BLACK_BGR,
}

_DEFAULTS: dict = {
    "name": "unnamed",
    "description": "",
    "paper_in": [8.5, 11.0],
    "ppi": 180.0,
    "rotation_deg": 0.0,
    "tilt_deg": 0.0,
    "tilt_axis_deg": 0.0,
    "bullet_diameter_in": 0.224,
    "reference": None,
    "bull": None,
    "aim_dots": [],
    "groups": [],
    "splatter": False,
    "wipe_prob": 0.0,
    "n_specks": 0,
    "n_decoys": 0,
    "blur_sigma_px": 0.8,
    "noise_sigma": 2.0,
    "texture_sigma": 2.0,
    "jpeg_quality": 92,
    "lighting": {"gradient": 0.06, "angle_deg": 30.0, "vignette": 0.0, "shadow": 0.0},
    "paper_bgr": list(PAPER_BGR),
    "background": "paper",
    "hole_style": "dark",
    "non_planar": None,
    "print_decoys": [],
    "staples": [],
    "wrinkles": [],
    "overlay_marks": [],
    "n_creases": 0,
    "corrugation_period_in": 0.22,
}
_GROUP_DEFAULTS: dict = {
    "aim_in": None,
    "offset_from_aim_in": [0.0, 0.0],
    "spread_in": 0.7,
    "n_single": 5,
    "merged_pairs": [],
    "ragged": [],
    "min_sep_d": 2.0,
    "roi_half_in": 1.1,
    "ragged_at_center": False,
}
_PLACEMENT_TRIES = 400
_GROUP_RESTARTS = 200


class _PlacementJam(Exception):
    """Raised when a unit cannot be placed under the current partial layout."""


def load_catalog(path: str | Path | None = None) -> dict[str, dict]:
    """Return the scenario catalog as {name: scenario_dict}; each scenario carries its `name`."""
    with open(path or CATALOG_PATH, encoding="utf-8") as fh:
        raw = json.load(fh)
    return {name: {**sc, "name": name} for name, sc in raw["scenarios"].items()}


def render_named(name: str, seed: int, catalog_path: str | Path | None = None):
    """Render catalog scenario `name`; raises KeyError listing valid names when unknown."""
    catalog = load_catalog(catalog_path)
    if name not in catalog:
        raise KeyError(f"unknown scenario {name!r}; known: {', '.join(sorted(catalog))}")
    return render(catalog[name], seed)


def render(scenario: dict, seed: int) -> tuple[np.ndarray, dict]:
    """Render `scenario` to (BGR uint8 image, truth dict); identical output for identical input."""
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    sc = _resolve(scenario)
    rng = np.random.default_rng([seed, zlib.crc32(sc["name"].encode())])
    cam = _Camera(sc)
    layout = _layout(sc, rng)
    shapes = [_hole_shapes(sc, layout, unit, rng) for unit in layout["units"]]
    specks = _specks(sc, layout, rng)
    layout["decoys"] = _decoys(sc, layout, rng)
    extras = np.random.default_rng([seed, zlib.crc32(sc["name"].encode()), 1])
    layout["creases"] = _creases(sc, layout, extras)
    layout["print_items"] = _print_items(sc, extras)
    img = _draw_scene(sc, cam, layout, shapes, specks)
    img = _camera_effects(sc, cam, img, rng)
    return img, _truth(sc, seed, cam, layout, shapes)


# ---------------------------------------------------------------- scenario validation


def _resolve(scenario: dict) -> dict:
    unknown = set(scenario) - set(_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown scenario keys: {sorted(unknown)}")
    sc = {**_DEFAULTS, **scenario}
    sc["lighting"] = {**_DEFAULTS["lighting"], **scenario.get("lighting", {})}
    groups = []
    for grp in sc["groups"]:
        bad = set(grp) - set(_GROUP_DEFAULTS)
        if bad:
            raise ValueError(f"unknown group keys: {sorted(bad)}")
        merged = {**_GROUP_DEFAULTS, **grp}
        if merged["aim_in"] is None:
            raise ValueError("every group needs aim_in")
        groups.append(merged)
    sc["groups"] = groups
    if sc["reference"] is not None and sc["reference"].get("kind") not in ("circle", "grid"):
        raise ValueError("reference.kind must be 'circle' or 'grid'")
    for dot in sc["aim_dots"]:
        if dot.get("color", "green") not in DOT_COLORS_BGR:
            raise ValueError(f"unknown dot color {dot.get('color')!r}")
    if not 0 < sc["ppi"] <= 400:
        raise ValueError("ppi must be in (0, 400]")
    _validate_traits(sc)
    return sc


def _validate_traits(sc: dict) -> None:
    if sc["hole_style"] not in HOLE_STYLES:
        raise ValueError(f"hole_style must be one of {HOLE_STYLES}")
    if sc["background"] not in ("paper", "cardboard"):
        raise ValueError("background must be 'paper' or 'cardboard'")
    ref = sc["reference"]
    if (ref is not None and ref["kind"] == "grid"
            and ref.get("line_color", "blue") not in GRID_LINE_COLORS_BGR):
        raise ValueError(f"grid line_color must be one of {sorted(GRID_LINE_COLORS_BGR)}")
    if sc["hole_style"] == "mixed_disc" and not (ref and ref["kind"] == "circle"):
        raise ValueError("hole_style 'mixed_disc' needs a circle reference (the sticker disc)")
    if sc["non_planar"] is not None and set(sc["non_planar"]) != {"amplitude_in"}:
        raise ValueError("non_planar must be {'amplitude_in': <inches>}")
    for item in sc["print_decoys"]:
        if item.get("kind") not in PRINT_KINDS:
            raise ValueError(f"print_decoys kind must be one of {PRINT_KINDS}")
    for mark in sc["overlay_marks"]:
        if mark.get("kind") not in OVERLAY_KINDS:
            raise ValueError(f"overlay_marks kind must be one of {OVERLAY_KINDS}")
    for wrinkle in sc["wrinkles"]:
        if not 0 < wrinkle.get("strength", 0.3) < 0.7:
            raise ValueError("wrinkle strength must be in (0, 0.7)")


# ---------------------------------------------------------------- camera


class _Camera:
    """Plane-inch to image-pixel homography: rotation, tilt (perspective), uniform scale."""

    def __init__(self, sc: dict):
        pw, ph = sc["paper_in"]
        ppi = float(sc["ppi"])
        rot = math.radians(sc["rotation_deg"])
        tilt = math.radians(sc["tilt_deg"])
        axis = math.radians(sc["tilt_axis_deg"])
        dist = 1.5 * max(pw, ph)
        focal = ppi * dist
        c, s = math.cos(rot), math.sin(rot)
        rot_z = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float64)
        ax = np.array([math.cos(axis), math.sin(axis), 0.0])
        cross = np.array([[0, -ax[2], ax[1]], [ax[2], 0, -ax[0]], [-ax[1], ax[0], 0]])
        tilt_m = (
            np.cos(tilt) * np.eye(3) + np.sin(tilt) * cross + (1 - np.cos(tilt)) * np.outer(ax, ax)
        )
        m = tilt_m @ rot_z
        extrinsic = np.column_stack([m[:, 0], m[:, 1], [0.0, 0.0, dist]])
        center = np.array([[1, 0, -pw / 2], [0, 1, -ph / 2], [0, 0, 1]], dtype=np.float64)
        intrinsic = np.diag([focal, focal, 1.0])
        h0 = intrinsic @ extrinsic @ center
        corners = project(h0, [[0, 0], [pw, 0], [pw, ph], [0, ph]])
        margin = 0.3 * ppi
        lo = corners.min(axis=0) - margin
        hi = corners.max(axis=0) + margin
        shift = np.array([[1, 0, -lo[0]], [0, 1, -lo[1]], [0, 0, 1]], dtype=np.float64)
        self.h = shift @ h0
        self.width = int(math.ceil(hi[0] - lo[0]))
        self.height = int(math.ceil(hi[1] - lo[1]))
        self.paper_in = (pw, ph)
        self.ppi = ppi
        self.h_inv = np.linalg.inv(self.h)
        bow = sc["non_planar"]
        self.bow = float(bow["amplitude_in"]) if bow else 0.0

    def map(self, pts) -> np.ndarray:
        """Plane inches to image px, including the sheet curvature when the scenario has any."""
        arr = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
        if self.bow:
            arr = bow_displace(arr, self.bow, self.paper_in[0])
        return project(self.h, arr)


def bow_displace(pts, amplitude_in: float, paper_width_in: float) -> np.ndarray:
    """Sheet curvature: plane y shifts by amplitude * (1 - ((x - w/2) / (w/2))^2), zero at the
    left and right paper edges and maximal along the vertical centre line."""
    arr = np.asarray(pts, dtype=np.float64).reshape(-1, 2).copy()
    half = paper_width_in / 2
    arr[:, 1] += amplitude_in * (1 - ((arr[:, 0] - half) / half) ** 2)
    return arr


def plane_to_image(truth: dict, pts_in) -> np.ndarray:
    """Map plane-inch points to image px exactly as the renderer did (applies truth['non_planar_warp']
    curvature when present). On flat scenarios this equals applying truth['h_plane_to_image']."""
    arr = np.asarray(pts_in, dtype=np.float64).reshape(-1, 2)
    warp = truth.get("non_planar_warp")
    if warp:
        arr = bow_displace(arr, warp["amplitude_in"], truth["paper_in"][0])
    return project(np.array(truth["h_plane_to_image"]), arr)


def project(h: np.ndarray, pts) -> np.ndarray:
    """Apply homography `h` to an (N, 2) array of points; returns (N, 2) float64."""
    arr = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(arr, np.asarray(h, dtype=np.float64)).reshape(-1, 2)


# ---------------------------------------------------------------- layout


def _layout(sc: dict, rng: np.random.Generator) -> dict:
    d = sc["bullet_diameter_in"]
    r = d / 2
    holes: list[dict] = []
    units: list[dict] = []
    groups: list[dict] = []
    for gi, grp in enumerate(sc["groups"]):
        aim = np.array(grp["aim_in"], dtype=np.float64)
        right, up = grp["offset_from_aim_in"]
        center = aim + np.array([right, -up])
        gap = (grp["min_sep_d"] - 1.0) * d
        wanted = (
            [("cluster", k, 2 * r) for k in grp["ragged"]]
            + [("pair", s, s * d / 2 + r) for s in grp["merged_pairs"]]
            + [("single", None, r)] * grp["n_single"]
        )
        by_kind = _place_group(sc, rng, wanted, center, grp, gap, gi)
        group_holes: list[int] = []
        for kind in ("single", "pair", "cluster"):
            for unit in by_kind[kind]:
                unit["holes"] = _unit_holes(sc, rng, unit, holes, d)
                group_holes.extend(unit["holes"])
                units.append(unit)
        groups.append({"spec": grp, "aim": aim, "hole_indices": group_holes})
    return {"holes": holes, "units": units, "groups": groups, "d": d}


def _place_group(sc, rng, wanted, center, grp, gap, group_index) -> dict[str, list[dict]]:
    """Place every unit of a group; restarts the whole group when random placement jams."""
    for _ in range(_GROUP_RESTARTS):
        placed: list[tuple[np.ndarray, float]] = []
        by_kind: dict[str, list[dict]] = {"single": [], "pair": [], "cluster": []}
        try:
            for kind, param, extent in wanted:
                if kind == "cluster" and grp["ragged_at_center"]:
                    pos = center + rng.uniform(-0.15, 0.15, size=2)
                else:
                    pos = _place(sc, rng, center, grp["spread_in"], extent, gap, placed)
                placed.append((pos, extent))
                by_kind[kind].append(
                    {"kind": kind, "param": param, "pos": pos, "group": group_index}
                )
        except _PlacementJam:
            continue
        return by_kind
    raise RuntimeError(f"could not place group {group_index} of scenario {sc['name']!r}; widen spread_in")


def _place(sc, rng, center, spread, extent, gap, placed) -> np.ndarray:
    ref = sc["reference"]
    for _ in range(_PLACEMENT_TRIES):
        ang = rng.uniform(0, 2 * math.pi)
        rad = spread * math.sqrt(rng.uniform())
        pos = center + rad * np.array([math.cos(ang), math.sin(ang)])
        if any(np.hypot(*(pos - q)) < extent + e + gap for q, e in placed):
            continue
        if any(_straddles_black(region, pos, extent) for region in _black_regions(sc)):
            continue
        if any(np.hypot(*(pos - np.array(m["center_in"]))) < m["diameter_in"] / 2 + extent + 0.05
               for m in sc["overlay_marks"] if m["kind"] == "diamond"):
            continue
        if ref is not None and ref["kind"] == "circle":
            rc, rr = np.array(ref["center_in"]), ref["diameter_in"] / 2
            dist = np.hypot(*(pos - rc))
            if ref.get("allow_holes"):
                if abs(dist - rr) < extent + ref.get("rim_clear_in", 0.3):
                    continue
            elif dist < rr + extent + 1.0:
                continue
        if not _inside_paper(sc, pos, extent + 0.3):
            continue
        return pos
    raise _PlacementJam


def _straddles_black(region, pos, extent) -> bool:
    """A unit must lie wholly inside a bull, and wholly in or wholly out of a black aim dot."""
    depth = _depth(region, pos)
    if region["bull"]:
        return depth < extent + 0.05
    return abs(depth) < extent + 0.03


def _inside_paper(sc, pos, margin) -> bool:
    pw, ph = sc["paper_in"]
    return margin <= pos[0] <= pw - margin and margin <= pos[1] <= ph - margin


def _unit_holes(sc, rng, unit, holes, d) -> list[int]:
    r = d / 2
    pos = unit["pos"]
    if unit["kind"] == "single":
        centers = [pos]
    elif unit["kind"] == "pair":
        ang = rng.uniform(0, math.pi)
        half = unit["param"] * d / 2
        off = half * np.array([math.cos(ang), math.sin(ang)])
        centers = [pos - off, pos + off]
        unit["separation_d"] = float(unit["param"])
    else:
        centers = _cluster_centers(rng, pos, int(unit["param"]), d)
    indices = []
    for c in centers:
        holes.append(
            {
                "center": np.asarray(c, dtype=np.float64),
                "core_r": r * rng.uniform(0.85, 1.0),
                "light": _in_black(sc, c),
                "style": "dark" if unit["kind"] == "cluster" else _hole_style(sc, c),
            }
        )
        indices.append(len(holes) - 1)
    return indices


def _hole_style(sc, point) -> str:
    style = sc["hole_style"]
    if style != "mixed_disc":
        return style
    ref = sc["reference"]
    inside = np.hypot(*(np.asarray(point) - np.array(ref["center_in"]))) < ref["diameter_in"] / 2
    return "light_core" if inside else "dark"


CLUSTER_MAX_SEPARATION_D = 0.5
CLUSTER_MIN_SEPARATION_D = 0.4
CLUSTER_MIN_AREA_RATIO = 1.5


def _cluster_centers(rng, pos, k, d) -> list[np.ndarray]:
    """Members of a ragged blob: max pairwise separation in [0.4, 0.5] d (below the 0.6 d split
    floor) and a union area of at least 1.5x one bullet hole so the blob is not read as single."""
    r = d / 2
    for _ in range(2000):
        base = rng.uniform(0, 2 * math.pi)
        centers = []
        for i in range(k):
            ang = base + 2 * math.pi * i / k + rng.uniform(-0.3, 0.3)
            rad = r * rng.uniform(0.25, 0.6)
            centers.append(pos + rad * np.array([math.cos(ang), math.sin(ang)]))
        seps = [np.hypot(*(a - b)) for i, a in enumerate(centers) for b in centers[i + 1 :]]
        if not CLUSTER_MIN_SEPARATION_D * d <= max(seps) <= 0.99 * CLUSTER_MAX_SEPARATION_D * d:
            continue
        if _union_area(centers, [0.85 * r] * k) >= CLUSTER_MIN_AREA_RATIO * math.pi * r**2:
            return centers
    raise RuntimeError("could not sample a ragged cluster within the separation and area limits")


def _union_area(centers, radii) -> float:
    """Area of a union of discs by grid sampling (step radius/40)."""
    lo = np.min([c - rad for c, rad in zip(centers, radii, strict=True)], axis=0)
    hi = np.max([c + rad for c, rad in zip(centers, radii, strict=True)], axis=0)
    step = max(radii) / 40
    xs, ys = np.meshgrid(np.arange(lo[0], hi[0], step), np.arange(lo[1], hi[1], step))
    covered = np.zeros(xs.shape, dtype=bool)
    for c, rad in zip(centers, radii, strict=True):
        covered |= (xs - c[0]) ** 2 + (ys - c[1]) ** 2 <= rad**2
    return float(covered.sum() * step * step)


def _black_regions(sc) -> list[dict]:
    regions = []
    if sc["bull"] is not None:
        regions.append({"shape": "circle", "center": sc["bull"]["center_in"],
                        "size": sc["bull"]["diameter_in"], "bull": True})
    for dot in sc["aim_dots"]:
        if dot.get("color", "green") == "black":
            regions.append({"shape": dot.get("shape", "circle"), "center": dot["center_in"],
                            "size": dot["diameter_in"], "bull": False})
    return regions


def _depth(region, point) -> float:
    """Signed distance in inches from the edge of a black region (positive inside)."""
    dx, dy = np.asarray(point, dtype=np.float64) - np.array(region["center"])
    half = region["size"] / 2
    if region["shape"] == "diamond":
        return (half - (abs(dx) + abs(dy))) / math.sqrt(2)
    return half - math.hypot(dx, dy)


def _in_black(sc, point) -> bool:
    return any(_depth(region, point) > 0 for region in _black_regions(sc))


# ---------------------------------------------------------------- hole shapes


def _ring_noise(rng, n, amp, keep_dipole) -> np.ndarray:
    """Zero-mean polar noise of RMS ~amp; without the 1st harmonic the shape centroid stays put."""
    theta = np.linspace(0, 2 * math.pi, n, endpoint=False)
    eps = np.zeros(n)
    for m in range(2, 13):
        a, b = rng.normal(size=2) / m
        eps += a * np.cos(m * theta) + b * np.sin(m * theta)
    eps += 0.35 * rng.normal(size=n)
    eps -= eps.mean()
    if not keep_dipole:
        eps -= (2 / n) * (eps @ np.cos(theta)) * np.cos(theta)
        eps -= (2 / n) * (eps @ np.sin(theta)) * np.sin(theta)
    rms = float(np.sqrt(np.mean(eps**2)))
    if rms > 0:
        eps = eps / rms * amp
    return np.clip(eps, -2.5 * amp, 2.5 * amp)


def _polar_poly(center, radius, eps) -> np.ndarray:
    theta = np.linspace(0, 2 * math.pi, len(eps), endpoint=False)
    rad = radius * (1 + eps) if np.isscalar(radius) else np.asarray(radius) * (1 + eps)
    return np.column_stack([center[0] + rad * np.cos(theta), center[1] + rad * np.sin(theta)])


def _gray(value) -> tuple[int, int, int]:
    v = int(round(float(value)))
    return (v, v, v)


def _hole_shapes(sc, layout, unit, rng) -> dict:
    d = layout["d"]
    if unit["kind"] == "cluster":
        return {"kind": "cluster", **_cluster_shape(sc, layout, unit, rng)}
    items = []
    for idx in unit["holes"]:
        hole = layout["holes"][idx]
        c, rc, light = hole["center"], hole["core_r"], hole["light"]
        core = _polar_poly(c, rc, _ring_noise(rng, 64, 0.05, keep_dipole=False))
        halo = _polar_poly(c, rc * rng.uniform(1.2, 1.5), _ring_noise(rng, 48, 0.15, True))
        wipe = None
        if rng.uniform() < sc["wipe_prob"]:
            wipe = _polar_poly(
                c, rc * rng.uniform(1.08, 1.16), _ring_noise(rng, 64, 0.03, keep_dipole=False)
            )
        splat = None
        if sc["splatter"]:
            outer = 0.5 * d * rng.uniform(1.5, 3.0)
            splat = _polar_poly(c, outer, _ring_noise(rng, 64, 0.12, True))
        item = {
            "core": core,
            "halo": halo,
            "wipe": wipe,
            "splat": splat,
            "core_gray": rng.uniform(205, 225) if light else rng.uniform(22, 42),
            "halo_gray": rng.uniform(60, 80) if light else rng.uniform(150, 175),
            "wipe_gray": rng.uniform(48, 62),
        }
        if hole["style"] == "light_core":
            item["inner"] = _polar_poly(
                c, rc * rng.uniform(0.5, 0.62), _ring_noise(rng, 48, 0.05, keep_dipole=False)
            )
            item["inner_bgr"] = tuple(
                int(v + rng.uniform(-8, 8)) for v in TAN_CORE_BGR
            )
        items.append(item)
    return {"kind": unit["kind"], "items": items}


def _cluster_shape(sc, layout, unit, rng) -> dict:
    holes = [layout["holes"][i] for i in unit["holes"]]
    c = np.mean([h["center"] for h in holes], axis=0)
    n = 72
    theta = np.linspace(0, 2 * math.pi, n, endpoint=False)
    dirs = np.column_stack([np.cos(theta), np.sin(theta)])
    radius = np.full(n, 0.5 * layout["d"] / 2)
    for h in holes:
        w = c - h["center"]
        proj = dirs @ w
        disc = proj**2 - (w @ w - h["core_r"] ** 2)
        hit = np.where(disc >= 0, -proj + np.sqrt(np.maximum(disc, 0)), 0.0)
        radius = np.maximum(radius, hit)
    outline = _polar_poly(c, radius, _ring_noise(rng, n, 0.07, keep_dipole=False))
    halo = _polar_poly(c, radius * rng.uniform(1.2, 1.4), _ring_noise(rng, n, 0.15, True))
    splat = None
    if sc["splatter"]:
        splat = _polar_poly(c, radius * 1.8, _ring_noise(rng, n, 0.12, True))
    return {
        "outline": outline,
        "halo": halo,
        "splat": splat,
        "core_gray": rng.uniform(22, 42),
        "halo_gray": rng.uniform(150, 175),
    }


def _specks(sc, layout, rng) -> list[tuple[np.ndarray, float]]:
    pw, ph = sc["paper_in"]
    r = sc["bullet_diameter_in"] / 2
    out = []
    centers = [h["center"] for h in layout["holes"]]
    for _ in range(int(sc["n_specks"])):
        for _ in range(_PLACEMENT_TRIES):
            pos = np.array([rng.uniform(0.5, pw - 0.5), rng.uniform(0.5, ph - 0.5)])
            if all(np.hypot(*(pos - c)) > 6 * r for c in centers):
                out.append((pos, r * rng.uniform(0.2, 0.4)))
                break
    return out


def _decoys(sc, layout, rng) -> list[dict]:
    """Hole-sized dark marks (area 0.36-0.81 of one hole, above the 0.35 area floor) away from
    holes and printed elements: false-positive bait that is not a shot."""
    pw, ph = sc["paper_in"]
    r = sc["bullet_diameter_in"] / 2
    keep_out = [(np.array(dot["center_in"]), dot["diameter_in"] / 2 + 0.3) for dot in sc["aim_dots"]]
    ref = sc["reference"]
    if ref is not None and ref["kind"] == "circle":
        keep_out.append((np.array(ref["center_in"]), ref["diameter_in"] / 2 + 0.3))
    centers = [h["center"] for h in layout["holes"]]
    out: list[dict] = []
    for _ in range(int(sc["n_decoys"])):
        for _ in range(_PLACEMENT_TRIES):
            pos = np.array([rng.uniform(0.7, pw - 0.7), rng.uniform(0.7, ph - 0.7)])
            if any(np.hypot(*(pos - c)) <= 6 * r for c in centers):
                continue
            if any(np.hypot(*(pos - d["center"])) <= 6 * r for d in out):
                continue
            if any(np.hypot(*(pos - c)) <= rad for c, rad in keep_out):
                continue
            radius = r * rng.uniform(0.6, 0.9)
            poly = _polar_poly(pos, radius, _ring_noise(rng, 48, 0.06, keep_dipole=False))
            out.append({"center": pos, "radius": radius, "poly": poly, "gray": rng.uniform(50, 75)})
            break
        else:
            raise RuntimeError("could not place a decoy mark")
    return out


# ---------------------------------------------------------------- drawing


def _fill(img, cam, poly_in, color) -> None:
    pts = np.round(cam.map(poly_in) * _FIXED).astype(np.int32)
    cv2.fillPoly(img, [pts], color, cv2.LINE_AA, _SHIFT)


def _line(img, cam, p0, p1, color, thickness) -> None:
    if cam.bow:
        length = float(np.hypot(*(np.asarray(p1) - np.asarray(p0))))
        n = max(2, int(math.ceil(length / 0.25)) + 1)
        t = np.linspace(0.0, 1.0, n)[:, None]
        pts = np.asarray(p0, dtype=np.float64) * (1 - t) + np.asarray(p1, dtype=np.float64) * t
        _polyline(img, cam, pts, color, thickness, closed=False)
        return
    a, b = np.round(cam.map([p0, p1]) * _FIXED).astype(np.int32)
    cv2.line(img, tuple(int(v) for v in a), tuple(int(v) for v in b), color, thickness,
             cv2.LINE_AA, _SHIFT)


def _polyline(img, cam, pts_in, color, thickness, closed) -> None:
    pts = np.round(cam.map(pts_in) * _FIXED).astype(np.int32)
    cv2.polylines(img, [pts], closed, color, thickness, cv2.LINE_AA, _SHIFT)


def _circle_poly(center, diameter, n=128) -> np.ndarray:
    return _polar_poly(center, diameter / 2, np.zeros(n))


def _diamond_poly(center, diagonal) -> np.ndarray:
    h = diagonal / 2
    cx, cy = center
    return np.array([[cx, cy - h], [cx + h, cy], [cx, cy + h], [cx - h, cy]])


def _draw_scene(sc, cam, layout, shapes, specks) -> np.ndarray:
    img = np.empty((cam.height, cam.width, 3), dtype=np.uint8)
    img[:] = TABLE_BGR
    pw, ph = cam.paper_in
    paper = tuple(int(v) for v in sc["paper_bgr"])
    if sc["background"] == "cardboard":
        _draw_cardboard(img, cam, sc, layout)
    else:
        _fill(img, cam, [[0, 0], [pw, 0], [pw, ph], [0, ph]], paper)

    ref = sc["reference"]
    if ref is not None and ref["kind"] == "grid":
        _draw_grid(img, cam, ref, sc["ppi"])
    for item in layout["print_items"]:
        _draw_print_item(img, cam, item, paper)
    if sc["bull"] is not None:
        _fill(img, cam, _circle_poly(sc["bull"]["center_in"], sc["bull"]["diameter_in"]), BLACK_BGR)
    for dot in sc["aim_dots"]:
        color = DOT_COLORS_BGR[dot.get("color", "green")]
        if dot.get("shape", "circle") == "diamond":
            poly = _diamond_poly(dot["center_in"], dot["diameter_in"])
        else:
            poly = _circle_poly(dot["center_in"], dot["diameter_in"])
        _fill(img, cam, poly, color)
    if ref is not None and ref["kind"] == "circle":
        _fill(img, cam, _circle_poly(ref["center_in"], ref["diameter_in"]), REFERENCE_BGR)
    for mark in sc["overlay_marks"]:
        _draw_overlay(img, cam, mark, sc["ppi"])
    for pos, radius in specks:
        _fill(img, cam, _circle_poly(pos, 2 * radius, 16), _gray(65))
    for decoy in layout["decoys"]:
        _fill(img, cam, decoy["poly"], _gray(decoy["gray"]))
    for staple in sc["staples"]:
        _draw_staple(img, cam, staple)

    for shape in shapes:
        for item in _items(shape):
            if item.get("splat") is not None:
                _fill(img, cam, item["splat"], SPLATTER_BGR)
    for shape in shapes:
        for item in _items(shape):
            _fill(img, cam, item["halo"], _gray(item["halo_gray"]))
    for shape in shapes:
        for item in _items(shape):
            if item.get("wipe") is not None:
                _fill(img, cam, item["wipe"], _gray(item["wipe_gray"]))
    for shape in shapes:
        for item in _items(shape):
            poly = item["outline"] if shape["kind"] == "cluster" else item["core"]
            _fill(img, cam, poly, _gray(item["core_gray"]))
    for shape in shapes:
        for item in _items(shape):
            if item.get("inner") is not None:
                _fill(img, cam, item["inner"], item["inner_bgr"])
    return img


def _items(shape) -> list[dict]:
    return [shape] if shape["kind"] == "cluster" else shape["items"]


def _draw_grid(img, cam, ref, ppi) -> None:
    ox, oy = ref.get("origin_in", [0.25, 0.5])
    across, down = ref.get("squares_across", 8), ref.get("squares_down", 10)
    color = GRID_LINE_COLORS_BGR[ref.get("line_color", "blue")]
    thickness = max(1, round(0.015 * ppi))
    band = ref.get("band")
    if band:
        half = band["width_in"] / 2
        x = band["center_in"]
        _fill(img, cam, [[x - half, oy], [x + half, oy], [x + half, oy + down],
                         [x - half, oy + down]], color)
    for i in range(across + 1):
        _line(img, cam, (ox + i, oy), (ox + i, oy + down), color, thickness)
    for j in range(down + 1):
        _line(img, cam, (ox, oy + j), (ox + across, oy + j), color, thickness)


def _draw_cardboard(img, cam, sc, layout) -> None:
    img[:] = CARDBOARD_BGR
    corners = project(cam.h_inv, [[0, 0], [cam.width, 0], [cam.width, cam.height], [0, cam.height]])
    lo, hi = corners.min(axis=0) - 1.0, corners.max(axis=0) + 1.0
    period = sc["corrugation_period_in"]
    x = math.floor(lo[0] / period) * period
    while x < hi[0]:
        _fill(img, cam, [[x, lo[1]], [x + period / 2, lo[1]], [x + period / 2, hi[1]],
                         [x, hi[1]]], CARDBOARD_STRIPE_BGR)
        x += period
    width = max(1, round(0.012 * sc["ppi"]))
    for crease in layout["creases"]:
        _polyline(img, cam, crease, _gray(CREASE_GRAY), width, closed=False)


def _creases(sc, layout, extras) -> list[np.ndarray]:
    """Random-walk crack lines on cardboard, kept 3 bullet radii away from every hole."""
    if sc["background"] != "cardboard" or not sc["n_creases"]:
        return []
    pw, ph = sc["paper_in"]
    r = sc["bullet_diameter_in"] / 2
    centers = [h["center"] for h in layout["holes"]]
    out: list[np.ndarray] = []
    for _ in range(int(sc["n_creases"])):
        for _ in range(_PLACEMENT_TRIES):
            pos = np.array([extras.uniform(0.5, pw - 0.5), extras.uniform(0.5, ph - 0.5)])
            ang = extras.uniform(0, 2 * math.pi)
            pts = [pos]
            for _ in range(int(extras.integers(8, 20))):
                ang += extras.uniform(-0.5, 0.5)
                pos = pos + 0.15 * np.array([math.cos(ang), math.sin(ang)])
                pts.append(pos)
            arr = np.array(pts)
            if (arr.min(axis=0) < 0.2).any() or (arr.max(axis=0) > [pw - 0.2, ph - 0.2]).any():
                continue
            if all(np.min(np.hypot(*(arr - c).T)) > 3 * r for c in centers):
                out.append(arr)
                break
        else:
            raise RuntimeError("could not place a crease")
    return out


# ---------------------------------------------------------------- printed decoys, marks, staples


def _print_items(sc, extras) -> list[dict]:
    items = []
    for spec in sc["print_decoys"]:
        if spec["kind"] == "qr":
            items.append(_qr_item(spec, extras))
        else:
            items.append(_text_item(spec))
    return items


def _qr_item(spec, extras) -> dict:
    cx, cy = spec["center_in"]
    size = spec["size_in"]
    module = size / QR_MODULES
    grid = extras.integers(0, 2, size=(QR_MODULES, QR_MODULES)).astype(bool)
    for r0, c0 in ((0, 0), (0, QR_MODULES - 7), (QR_MODULES - 7, 0)):
        grid[r0 : r0 + 7, c0 : c0 + 7] = True
        grid[r0 + 1 : r0 + 6, c0 + 1 : c0 + 6] = False
        grid[r0 + 2 : r0 + 5, c0 + 2 : c0 + 5] = True
    x0, y0 = cx - size / 2, cy - size / 2
    quads = [
        [[x0 + c * module, y0 + r * module], [x0 + (c + 1) * module, y0 + r * module],
         [x0 + (c + 1) * module, y0 + (r + 1) * module], [x0 + c * module, y0 + (r + 1) * module]]
        for r in range(QR_MODULES) for c in range(QR_MODULES) if grid[r, c]
    ]
    return {
        "kind": "qr",
        "rect_in": [x0, y0, x0 + size, y0 + size],
        "quiet_in": [x0 - module, y0 - module, x0 + size + module, y0 + size + module],
        "quads": quads,
    }


def _text_item(spec) -> dict:
    scale_px_per_in = 200
    font = cv2.FONT_HERSHEY_DUPLEX
    text, height = spec["text"], spec["height_in"]
    (_, base_h), _ = cv2.getTextSize(text, font, 1.0, 2)
    font_scale = height * scale_px_per_in / base_h
    thick = max(2, round(font_scale * 2))
    (tw, th), baseline = cv2.getTextSize(text, font, font_scale, thick)
    pad = 6
    canvas = np.zeros((th + baseline + 2 * pad, tw + 2 * pad), dtype=np.uint8)
    cv2.putText(canvas, text, (pad, pad + th), font, font_scale, 255, thick, cv2.LINE_8)
    contours, hierarchy = cv2.findContours(canvas, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    ang = math.radians(spec.get("angle_deg", 0.0))
    rot = np.array([[math.cos(ang), -math.sin(ang)], [math.sin(ang), math.cos(ang)]])
    centre = np.array(spec["center_in"], dtype=np.float64)
    polys = []
    for contour, hier in zip(contours, hierarchy[0], strict=True):
        local = (contour.reshape(-1, 2) - [canvas.shape[1] / 2, canvas.shape[0] / 2])
        plane = (local / scale_px_per_in) @ rot.T + centre
        polys.append((plane, bool(hier[3] != -1)))
    allpts = np.vstack([poly for poly, _ in polys])
    lo, hi = allpts.min(axis=0), allpts.max(axis=0)
    return {
        "kind": "text",
        "text": text,
        "ink_bgr": INK_COLORS_BGR[spec.get("color", "black")],
        "rect_in": [float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])],
        "polys": polys,
    }


def _draw_print_item(img, cam, item, paper) -> None:
    if item["kind"] == "qr":
        x0, y0, x1, y1 = item["quiet_in"]
        _fill(img, cam, [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], (245, 245, 245))
        for quad in item["quads"]:
            _fill(img, cam, quad, BLACK_BGR)
        return
    for poly, is_hole in item["polys"]:
        if not is_hole:
            _fill(img, cam, poly, item["ink_bgr"])
    for poly, is_hole in item["polys"]:
        if is_hole:
            _fill(img, cam, poly, paper)


def _draw_overlay(img, cam, mark, ppi) -> None:
    kind = mark["kind"]
    color = DOT_COLORS_BGR[mark.get("color", "black")]
    thickness = max(1, round(mark.get("thickness_in", 0.02) * ppi))
    if kind == "diamond":
        _fill(img, cam, _diamond_poly(mark["center_in"], mark["diameter_in"]), color)
    elif kind == "ring":
        _polyline(img, cam, _circle_poly(mark["center_in"], mark["diameter_in"]), color,
                  thickness, closed=True)
    elif kind == "line":
        _line(img, cam, mark["p0_in"], mark["p1_in"], color, thickness)
    else:
        cx, cy = mark["center_in"]
        arm, half = mark["arm_in"], mark["width_in"] / 2
        _fill(img, cam, [[cx - arm, cy - half], [cx + arm, cy - half],
                         [cx + arm, cy + half], [cx - arm, cy + half]], TAPE_BLUE_BGR)
        _fill(img, cam, [[cx - half, cy - arm], [cx + half, cy - arm],
                         [cx + half, cy + arm], [cx - half, cy + arm]], TAPE_BLUE_BGR)
        _fill(img, cam, _circle_poly(mark["center_in"], 0.14, 32), BLACK_BGR)


def _staple_polys(staple) -> list[np.ndarray]:
    cx, cy = staple["center_in"]
    length, width, leg = staple["length_in"], 0.04, 0.1
    ang = math.radians(staple.get("angle_deg", 0.0))
    ux, uy = math.cos(ang), math.sin(ang)
    vx, vy = -uy, ux

    def rect(u0, u1, v0, v1):
        return np.array([[cx + ux * u + vx * v, cy + uy * u + vy * v]
                         for u, v in ((u0, v0), (u1, v0), (u1, v1), (u0, v1))])

    h = length / 2
    return [rect(-h, h, -width / 2, width / 2),
            rect(-h, -h + width, width / 2, width / 2 + leg),
            rect(h - width, h, width / 2, width / 2 + leg)]


def _draw_staple(img, cam, staple) -> None:
    outline = max(1, round(0.012 * cam.ppi))
    for poly in _staple_polys(staple):
        _fill(img, cam, poly, _gray(205))
        _polyline(img, cam, poly, _gray(45), outline, closed=True)


# ---------------------------------------------------------------- camera effects


def _lighting_field(sc, width, height) -> np.ndarray:
    light = sc["lighting"]
    gh, gw = max(2, height // 8), max(2, width // 8)
    yy, xx = np.mgrid[0:gh, 0:gw].astype(np.float32)
    xx, yy = xx / (gw - 1) - 0.5, yy / (gh - 1) - 0.5
    phi = math.radians(light["angle_deg"])
    t = xx * math.cos(phi) + yy * math.sin(phi)
    field = 1 + 2 * light["gradient"] * t
    field *= 1 - light["vignette"] * 2 * (xx**2 + yy**2)
    if light["shadow"]:
        field *= 1 - light["shadow"] * np.exp(-(((t - 0.12) / 0.09) ** 2))
    field /= max(float(field.max()), 1.0)
    return cv2.resize(field.astype(np.float32), (width, height), interpolation=cv2.INTER_LINEAR)


def _wrinkle_field(sc, cam, width, height) -> np.ndarray:
    """Multiplicative shading from sheet wrinkles: a dark ridge line with a lighter flank."""
    gh, gw = max(2, height // 6), max(2, width // 6)
    ys, xs = np.mgrid[0:gh, 0:gw].astype(np.float64)
    pix = np.column_stack([xs.ravel() * width / gw, ys.ravel() * height / gh])
    plane = project(cam.h_inv, pix)
    field = np.ones(len(plane), dtype=np.float64)
    for wr in sc["wrinkles"]:
        p0, p1 = np.array(wr["p0_in"], dtype=np.float64), np.array(wr["p1_in"], dtype=np.float64)
        width_in, strength = wr["width_in"], wr.get("strength", 0.3)
        length = float(np.hypot(*(p1 - p0)))
        u = (p1 - p0) / length
        rel = plane - p0
        along, side = rel @ u, rel @ np.array([-u[1], u[0]])
        overshoot = np.maximum(0.0, np.maximum(-along, along - length))
        extent = np.exp(-((overshoot / width_in) ** 2))
        dark = np.exp(-((side / width_in) ** 2))
        flank = np.exp(-(((side - 1.8 * width_in) / (0.8 * width_in)) ** 2))
        field *= 1 - strength * extent * dark + 0.5 * strength * extent * flank
    small = field.reshape(gh, gw).astype(np.float32)
    return cv2.resize(small, (width, height), interpolation=cv2.INTER_LINEAR)


def _camera_effects(sc, cam, img, rng) -> np.ndarray:
    height, width = img.shape[:2]
    out = img
    if sc["blur_sigma_px"] > 0:
        out = cv2.GaussianBlur(out, (0, 0), sc["blur_sigma_px"])
    field = _lighting_field(sc, width, height)
    if sc["wrinkles"]:
        field = field * _wrinkle_field(sc, cam, width, height)
    out = out.astype(np.float32) * field[:, :, None]
    if sc["texture_sigma"] > 0:
        coarse = rng.normal(size=(height // 6 + 2, width // 6 + 2)).astype(np.float32)
        tex = cv2.resize(coarse, (width, height), interpolation=cv2.INTER_CUBIC)
        out += sc["texture_sigma"] * tex[:, :, None]
    if sc["noise_sigma"] > 0:
        out += (sc["noise_sigma"] * rng.standard_normal(out.shape, dtype=np.float32))
    out = np.clip(out, 0, 255).astype(np.uint8)
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, int(sc["jpeg_quality"])])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


# ---------------------------------------------------------------- truth


def _truth(sc, seed, cam, layout, shapes) -> dict:
    h = cam.h
    holes_in = [h_["center"].tolist() for h_ in layout["holes"]]
    r_in = sc["bullet_diameter_in"] / 2
    ragged_groups, ragged_outlines, merged = [], [], []
    for unit, shape in zip(layout["units"], shapes, strict=True):
        if unit["kind"] == "cluster":
            ragged_groups.append(list(unit["holes"]))
            ragged_outlines.append(shape["outline"].tolist())
        elif unit["kind"] == "pair":
            gap = _polygon_gap(shape["items"][0]["core"], shape["items"][1]["core"])
            merged.append(
                {
                    "indices": list(unit["holes"]),
                    "separation_d": unit["separation_d"],
                    "core_gap_in": gap,
                    "cores_overlap": gap <= 0,
                }
            )
    groups = []
    for grp in layout["groups"]:
        aim = grp["aim"]
        half = grp["spec"]["roi_half_in"]
        rect = [[aim[0] - half, aim[1] - half], [aim[0] + half, aim[1] - half],
                [aim[0] + half, aim[1] + half], [aim[0] - half, aim[1] + half]]
        for idx in grp["hole_indices"]:
            cx, cy = layout["holes"][idx]["center"]
            reach = 1.5 * r_in
            rect += [[cx - reach, cy - reach], [cx + reach, cy - reach],
                     [cx + reach, cy + reach], [cx - reach, cy + reach]]
        px = cam.map(rect)
        roi = [
            max(0, int(math.floor(px[:, 0].min()))),
            max(0, int(math.floor(px[:, 1].min()))),
            min(cam.width, int(math.ceil(px[:, 0].max()))),
            min(cam.height, int(math.ceil(px[:, 1].max()))),
        ]
        groups.append(
            {
                "aim_in": aim.tolist(),
                "aim_px": cam.map([aim])[0].tolist(),
                "hole_indices": list(grp["hole_indices"]),
                "roi_px": roi,
            }
        )
    return {
        "scenario": sc["name"],
        "seed": seed,
        "ppi": cam.ppi,
        "bullet_diameter_in": sc["bullet_diameter_in"],
        "image_size": [cam.width, cam.height],
        "paper_in": list(sc["paper_in"]),
        "h_plane_to_image": h.tolist(),
        "holes_in": holes_in,
        "ragged_groups": ragged_groups,
        "ragged_outlines_in": ragged_outlines,
        "merged_pairs": merged,
        "decoys_in": [
            {"center_in": dc["center"].tolist(), "radius_in": float(dc["radius"])}
            for dc in layout["decoys"]
        ],
        "holes_light_core": [
            bool(h_["light"] or h_["style"] == "light_core") for h_ in layout["holes"]
        ],
        "holes_wipe": _wipe_flags(layout, shapes),
        "hole_style": sc["hole_style"],
        "background": sc["background"],
        "paper_bgr": [int(v) for v in sc["paper_bgr"]],
        "non_planar": bool(sc["non_planar"]),
        "non_planar_warp": dict(sc["non_planar"]) if sc["non_planar"] else None,
        "print_decoys_in": [
            {"kind": it["kind"], "rect_in": it["rect_in"], **({"text": it["text"]} if "text" in it else {})}
            for it in layout["print_items"]
        ],
        "staples_in": [
            {"center_in": list(st["center_in"]), "length_in": st["length_in"],
             "angle_deg": st.get("angle_deg", 0.0)}
            for st in sc["staples"]
        ],
        "wrinkles_in": [
            {"p0_in": list(w["p0_in"]), "p1_in": list(w["p1_in"]), "width_in": w["width_in"],
             "strength": w.get("strength", 0.3)}
            for w in sc["wrinkles"]
        ],
        "creases_in": [crease.tolist() for crease in layout["creases"]],
        "corrugation_period_in": sc["corrugation_period_in"] if sc["background"] == "cardboard" else None,
        "aim_marks_in": _aim_marks(sc),
        "reference": _reference_truth(sc, cam),
        "aim_in": groups[0]["aim_in"] if groups else None,
        "aim_px": groups[0]["aim_px"] if groups else None,
        "groups": groups,
    }


def _polygon_gap(a: np.ndarray, b: np.ndarray) -> float:
    """Smallest distance in inches between two polygons (<= 0 when they overlap)."""
    inside = [
        cv2.pointPolygonTest(b.astype(np.float32), (float(x), float(y)), True) for x, y in a
    ] + [cv2.pointPolygonTest(a.astype(np.float32), (float(x), float(y)), True) for x, y in b]
    return float(-max(inside))


def _aim_marks(sc) -> list[dict]:
    marks = [{"kind": f"dot_{d.get('shape', 'circle')}", "center_in": list(d["center_in"])}
             for d in sc["aim_dots"]]
    marks += [{"kind": m["kind"], "center_in": list(m["center_in"])}
              for m in sc["overlay_marks"] if m["kind"] in ("diamond", "tape_cross")]
    return marks


def _wipe_flags(layout, shapes) -> list[bool]:
    flags = [False] * len(layout["holes"])
    for unit, shape in zip(layout["units"], shapes, strict=True):
        if unit["kind"] != "cluster":
            for idx, item in zip(unit["holes"], shape["items"], strict=True):
                flags[idx] = item["wipe"] is not None
    return flags


def _reference_truth(sc, cam) -> dict | None:
    ref = sc["reference"]
    if ref is None:
        return None
    if ref["kind"] == "circle":
        poly = cam.map(_circle_poly(ref["center_in"], ref["diameter_in"], 256))
        (ex, ey), (wd, ht), angle = cv2.fitEllipse(poly.astype(np.float32))
        major, minor = max(wd, ht), min(wd, ht)
        return {
            "kind": "circle",
            "diameter_in": ref["diameter_in"],
            "center_in": list(ref["center_in"]),
            "center_px": cam.map([ref["center_in"]])[0].tolist(),
            "ellipse_center_px": [float(ex), float(ey)],
            "axes_px": [float(major), float(minor)],
            "angle_deg": float(angle),
            "axis_ratio": float(minor / major),
            "px_per_in_at_ref": float((major + minor) / 2 / ref["diameter_in"]),
        }
    ox, oy = ref.get("origin_in", [0.25, 0.5])
    across, down = ref.get("squares_across", 8), ref.get("squares_down", 10)
    corners_in = [[ox, oy], [ox + across, oy], [ox + across, oy + down], [ox, oy + down]]
    return {
        "kind": "grid",
        "spacing_in": 1.0,
        "squares_across": across,
        "squares_down": down,
        "corners_in": corners_in,
        "corners_px": cam.map(corners_in).tolist(),
        "line_color": ref.get("line_color", "blue"),
        "line_bgr": list(GRID_LINE_COLORS_BGR[ref.get("line_color", "blue")]),
        "band": dict(ref["band"]) if ref.get("band") else None,
    }


# ---------------------------------------------------------------- CLI


def write_scenario(name: str, seed: int, out_dir: str | Path) -> tuple[Path, Path]:
    """Render a catalog scenario into `<out_dir>/<name>.png` and `<name>.truth.json`."""
    img, truth = render_named(name, seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    png, meta = out / f"{name}.png", out / f"{name}.truth.json"
    if not cv2.imwrite(str(png), img):
        raise OSError(f"could not write {png}")
    meta.write_text(json.dumps(truth, indent=2), encoding="utf-8")
    return png, meta


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns a process exit code."""
    parser = argparse.ArgumentParser(description="Render a synthetic target photo and its truth.")
    parser.add_argument("--scenario", help="catalog scenario name")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", help="output directory")
    parser.add_argument("--list", action="store_true", help="list scenario names and exit")
    args = parser.parse_args(argv)
    if args.list:
        print("\n".join(load_catalog()))
        return 0
    if not args.scenario or not args.out:
        parser.error("--scenario and --out are required")
    try:
        png, meta = write_scenario(args.scenario, args.seed, args.out)
    except (KeyError, ValueError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(png)
    print(meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
