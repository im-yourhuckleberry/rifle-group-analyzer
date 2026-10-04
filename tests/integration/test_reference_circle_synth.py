"""T2: circle reference candidates and aim snap on the synthetic catalog, scored against its truth."""

from __future__ import annotations

import math
from functools import cache

import numpy as np
import pytest

from rga.domain.types import CircleCandidate, ReferenceKind
from rga.vision.reference_circle import (
    find_circle_candidates,
    reference_from_circle,
    refine_circle_near,
)
from tests.synth.generate import load_catalog, render

CATALOG = load_catalog()
SEEDS = (0, 1)
KIND_BY_DIAMETER = {
    1.0: ReferenceKind.CIRCLE_1IN,
    1.5: ReferenceKind.CIRCLE_1_5IN,
    3.0: ReferenceKind.CIRCLE_3IN,
}
CIRCLE_SCENARIOS = sorted(
    name
    for name, sc in CATALOG.items()
    if sc.get("reference") and sc["reference"]["kind"] == "circle"
)
RENDER_OFFSET_PX = (
    1.5  # the renderer's anti-aliased fill is about 1.2 px wider than its truth polygon
)
SNAP_TOLERANCE_IN = 0.02


@cache
def rendered(name: str, seed: int):
    return render(CATALOG[name], seed)


@cache
def candidates(name: str, seed: int, kind: ReferenceKind) -> tuple[CircleCandidate, ...]:
    img, _ = rendered(name, seed)
    return tuple(find_circle_candidates(img, kind))


def reference_kind(name: str) -> ReferenceKind:
    ref = CATALOG[name].get("reference")
    if ref and ref["kind"] == "circle":
        return KIND_BY_DIAMETER[ref["diameter_in"]]
    return ReferenceKind.CIRCLE_1IN


def mean_diameter(c: CircleCandidate) -> float:
    return (c.axes_px[0] + c.axes_px[1]) / 2


def true_candidate(name: str, seed: int) -> tuple[int, CircleCandidate, dict]:
    _, truth = rendered(name, seed)
    ref = truth["reference"]
    found = candidates(name, seed, reference_kind(name))
    radius = max(ref["axes_px"]) / 2
    for rank, c in enumerate(found):
        if math.dist(c.center_px, ref["ellipse_center_px"]) < 0.25 * radius:
            return rank, c, ref
    pytest.fail(f"{name} seed {seed}: true reference not proposed; got {found}")


def test_catalog_has_circle_scenarios_including_the_cardboard_sticker() -> None:
    assert {"clean_circle1", "clean_circle3", "circle_tilt_warn"} <= set(CIRCLE_SCENARIOS)
    assert any(CATALOG[n].get("background") == "cardboard" for n in CIRCLE_SCENARIOS)


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("name", CIRCLE_SCENARIOS)
def test_the_true_reference_is_among_the_top_three_and_accurate(name: str, seed: int) -> None:
    rank, c, ref = true_candidate(name, seed)
    assert rank < 3
    truth_mean = sum(ref["axes_px"]) / 2
    assert math.dist(c.center_px, ref["ellipse_center_px"]) < 0.5
    assert abs(mean_diameter(c) - truth_mean) <= 0.003 * truth_mean + RENDER_OFFSET_PX
    assert min(c.axes_px) / max(c.axes_px) == pytest.approx(ref["axis_ratio"], abs=0.01)
    assert 0 < c.score <= 1


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("name", ["clean_circle1", "clean_circle3"])
def test_clean_circles_are_scaled_within_the_renderer_offset(name: str, seed: int) -> None:
    rank, c, ref = true_candidate(name, seed)
    truth_mean = sum(ref["axes_px"]) / 2
    assert rank == 0
    assert abs(mean_diameter(c) - truth_mean) <= 0.003 * truth_mean + RENDER_OFFSET_PX
    assert abs(mean_diameter(c) - truth_mean) <= 0.01 * truth_mean


@pytest.mark.parametrize("seed", SEEDS)
def test_the_tilted_circle_keeps_its_axes_and_triggers_the_tilt_warning(seed: int) -> None:
    _, c, ref = true_candidate("circle_tilt_warn", seed)
    # the truth angle is OpenCV's first-axis angle, which may be the minor axis
    offsets = [abs((c.angle_deg - ref["angle_deg"] - turn + 90) % 180 - 90) for turn in (0, 90)]
    assert min(offsets) < 5.0
    result = reference_from_circle(c, ReferenceKind.CIRCLE_1IN, "auto")
    assert result.axis_ratio is not None and result.axis_ratio < 0.98
    assert result.warnings
    assert result.method == "auto"


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("name", ["clean_circle1", "clean_circle3", "merged_pairs"])
def test_level_circles_get_no_tilt_warning(name: str, seed: int) -> None:
    _, c, _ = true_candidate(name, seed)
    assert reference_from_circle(c, reference_kind(name), "auto").warnings == ()


def test_the_cardboard_sticker_is_found_as_a_disc_on_textured_cardboard() -> None:
    name = next(n for n in CIRCLE_SCENARIOS if CATALOG[n].get("background") == "cardboard")
    for seed in SEEDS:
        rank, c, ref = true_candidate(name, seed)
        assert rank == 0
        assert mean_diameter(c) > 0.3 * min(rendered(name, seed)[0].shape[:2])


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("kind", list(KIND_BY_DIAMETER.values()), ids=lambda k: k.value)
def test_no_reference_scenario_proposes_nothing(seed: int, kind: ReferenceKind) -> None:
    assert candidates("no_reference", seed, kind) == ()


def printed_circles(name: str, truth: dict) -> list[tuple[np.ndarray, float]]:
    """(image center, image diameter) of every circular print in the scenario (not diamonds)."""
    sc = CATALOG[name]
    h = np.array(truth["h_plane_to_image"])
    ppi = sc.get("ppi", 180)

    def project(p) -> np.ndarray:
        v = h @ np.array([p[0], p[1], 1.0])
        return v[:2] / v[2]

    circles = []
    if sc.get("reference") and sc["reference"]["kind"] == "circle":
        circles.append((sc["reference"]["center_in"], sc["reference"]["diameter_in"]))
    circles += [
        (a["center_in"], a["diameter_in"])
        for a in sc.get("aim_dots", [])
        if a.get("shape", "circle") == "circle"
    ]
    if sc.get("bull"):
        circles.append((sc["bull"]["center_in"], sc["bull"]["diameter_in"]))
    circles += [
        (m["center_in"], m["diameter_in"])
        for m in sc.get("overlay_marks", [])
        if m["kind"] == "ring"
    ]
    return [(project(c), d * ppi) for c, d in circles]


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("name", sorted(CATALOG))
def test_every_proposed_candidate_is_a_printed_circle_never_a_hole_diamond_or_cross(
    name: str, seed: int
) -> None:
    _, truth = rendered(name, seed)
    legit = printed_circles(name, truth)
    for kind in (reference_kind(name), ReferenceKind.CIRCLE_3IN):
        found = candidates(name, seed, kind)
        for i, a in enumerate(found):
            for b in found[i + 1 :]:
                same_place = math.dist(a.center_px, b.center_px) < 0.3 * min(a.axes_px)
                assert not (same_place and 0.8 < mean_diameter(a) / mean_diameter(b) < 1.25)
        for c in found:
            d = mean_diameter(c)
            assert any(
                math.dist(c.center_px, center) < 0.12 * d and 0.7 < d / diameter < 1.25
                for center, diameter in legit
            ), f"{name} seed {seed} {kind.value}: unexpected candidate {c}"


def aim_marks(name: str) -> list[tuple[tuple[float, float], str]]:
    sc = CATALOG[name]
    marks = [(tuple(a["center_in"]), a.get("shape", "circle")) for a in sc.get("aim_dots", [])]
    marks += [
        (tuple(m["center_in"]), m["kind"])
        for m in sc.get("overlay_marks", [])
        if m["kind"] in ("diamond", "tape_cross")
    ]
    if sc.get("bull"):
        marks.append((tuple(sc["bull"]["center_in"]), "bull"))
    return marks


def snap_cases() -> list[tuple[str, int, int, str]]:
    cases = []
    for name, sc in CATALOG.items():
        marks = aim_marks(name)
        for gi, group in enumerate(sc.get("groups", [])):
            kinds = [k for center, k in marks if np.allclose(center, group["aim_in"], atol=1e-6)]
            cases += [(name, seed, gi, kinds[0]) for seed in SEEDS if kinds]
    return cases


SNAP_CASES = snap_cases()


def test_snap_cases_cover_dots_diamonds_a_bull_and_the_tape_cross() -> None:
    assert {"circle", "diamond", "bull", "tape_cross"} <= {k for *_, k in SNAP_CASES}


@pytest.mark.parametrize(("name", "seed", "group", "kind"), SNAP_CASES, ids=lambda v: str(v))
def test_snap_lands_on_the_aim_mark_center_within_two_hundredths_of_an_inch(
    name: str, seed: int, group: int, kind: str
) -> None:
    img, truth = rendered(name, seed)
    ppi = CATALOG[name].get("ppi", 180)
    aim = np.array(truth["groups"][group]["aim_px"])
    click = aim + np.array([0.15 * ppi, -0.10 * ppi])
    c = refine_circle_near(img, (float(click[0]), float(click[1])), 0.5 * ppi)
    assert c is not None, f"no {kind} found near the click"
    assert math.dist(c.center_px, aim) <= SNAP_TOLERANCE_IN * ppi


@pytest.mark.parametrize("seed", SEEDS)
def test_snap_with_nothing_near_returns_none(seed: int) -> None:
    img, _ = rendered("no_reference", seed)
    assert refine_circle_near(img, (200.0, 200.0), 40.0) is None
