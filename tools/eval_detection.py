"""Accuracy harness for hole detection against the P-11 synthetic thresholds (spec.md section 6).

Usage:
    python -m tools.eval_detection --catalog tests/synth/catalog.json [--seeds 0-4]
        [--report .claude/specs/rifle-group-analyzer/spike-report.md] [--real-dir data/real_photos]

Each scenario is rendered per seed, rectified with the TRUTH transform (isolating detection from
reference error), run through `detect_holes` on the whole working image, and scored against the
truth holes with Hungarian matching and a 0.5 * bullet-diameter gate. Exits non-zero when any
scenario misses a P-11 threshold.
"""

from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import math
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from rga.domain.types import Blob, BlobKind, WorkingFrame
from rga.vision.detect import DetectParams, detect_holes
from rga.vision.transform import apply_h, build_working

GATE_FRACTION = 0.5
SPLIT_FLOOR_D = 0.6


@dataclass(frozen=True)
class Thresholds:
    """P-11 synthetic thresholds."""

    center_p95_in: float = 0.03
    center_max_in: float = 0.05
    recall_clean: float = 0.98
    recall_noisy: float = 0.95
    fp_clean: int = 1
    fp_noisy: int = 3
    single_read: float = 0.95
    pair_resolved: float = 0.90
    pair_center_in: float = 0.05
    es_error_in: float = 0.05
    runtime_s: float = 3.0


P11 = Thresholds()


def hungarian(cost: np.ndarray) -> list[tuple[int, int]]:
    """Minimum-cost assignment for a rectangular cost matrix; returns (row, col) pairs."""
    cost = np.asarray(cost, dtype=np.float64)
    if cost.size == 0:
        return []
    transposed = cost.shape[0] > cost.shape[1]
    c = cost.T if transposed else cost
    n, m = c.shape
    inf = float("inf")
    u, v = np.zeros(n + 1), np.zeros(m + 1)
    way, match = np.zeros(m + 1, dtype=int), np.zeros(m + 1, dtype=int)
    for i in range(1, n + 1):
        match[0] = i
        j0 = 0
        minv, used = np.full(m + 1, inf), np.zeros(m + 1, dtype=bool)
        while True:
            used[j0] = True
            i0, delta, j1 = match[j0], inf, 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = c[i0 - 1, j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j], way[j] = cur, j0
                if minv[j] < delta:
                    delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[match[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if match[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            match[j0] = match[j1]
            j0 = j1
    pairs = [(match[j] - 1, j - 1) for j in range(1, m + 1) if match[j] != 0]
    return sorted((j, i) if transposed else (i, j) for i, j in pairs)


@dataclass
class RenderScore:
    """Raw scoring facts for one rendered target."""

    scenario: str
    seed: int
    n_isolated: int
    matched_isolated: int
    isolated_single: int
    isolated_errors_in: list[float]
    false_positives: int
    pairs: list[dict]
    clusters: list[dict]
    es_errors_in: list[float]
    runtime_s: float
    n_blobs: int
    kinds: dict[str, int] = field(default_factory=dict)


def is_noisy(scenario: dict) -> bool:
    """A scenario is 'noisy' (relaxed P-11 recall/FP row) when it carries heavy degradation or decoys."""
    light = scenario.get("lighting") or {}
    return bool(
        scenario.get("noise_sigma", 2.0) >= 6
        or scenario.get("texture_sigma", 2.0) >= 5
        or scenario.get("jpeg_quality", 92) < 80
        or scenario.get("n_specks", 0) > 0
        or scenario.get("n_decoys", 0) > 0
        or light.get("shadow", 0) > 0
        or light.get("vignette", 0) > 0.1
        or scenario.get("print_decoys")
        or scenario.get("staples")
        or scenario.get("wrinkles")
        or scenario.get("background", "paper") != "paper"
    )


def working_from_truth(img: np.ndarray, truth: dict):
    """Rectify with the truth plane->image homography; returns (working, frame, h_px_to_working)."""
    h = np.array(truth["h_plane_to_image"], dtype=np.float64)
    return build_working(img, np.linalg.inv(h))


def truth_holes_working_px(truth: dict, h_px_to_working: np.ndarray) -> np.ndarray:
    """Truth hole centers in working px; honors the synthetic sheet curvature when present."""
    from tests.synth.generate import plane_to_image

    pts = np.array(truth["holes_in"], dtype=np.float64).reshape(-1, 2)
    return apply_h(h_px_to_working, plane_to_image(truth, pts)) if len(pts) else pts


def detection_centers(blobs: Sequence[Blob]) -> tuple[np.ndarray, list[Blob]]:
    """Every blob centre as an (N, 2) array plus, per row, the blob that owns it."""
    centers, owners = [], []
    for b in blobs:
        for c in b.centers_px:
            centers.append(c)
            owners.append(b)
    return np.array(centers, dtype=np.float64).reshape(-1, 2), owners


def score_render(
    scenario_name: str,
    seed: int,
    img: np.ndarray,
    truth: dict,
    params: DetectParams | None = None,
    detect: Callable[..., list[Blob]] = detect_holes,
) -> RenderScore:
    """Rectify a render with its truth transform, run `detect`, and score the blobs against truth."""
    working, frame, h_work = working_from_truth(img, truth)
    t0 = time.perf_counter()
    blobs = detect(working, frame, truth["bullet_diameter_in"], None, params)
    runtime = time.perf_counter() - t0
    return score_blobs(scenario_name, seed, blobs, truth, frame, h_work, runtime)


def score_blobs(
    scenario_name: str,
    seed: int,
    blobs: Sequence[Blob],
    truth: dict,
    frame: WorkingFrame,
    h_px_to_working: np.ndarray,
    runtime: float = 0.0,
) -> RenderScore:
    """Match blob centres to truth holes (Hungarian, 0.5 d gate) and collect the per-render facts."""
    d_px = truth["bullet_diameter_in"] * frame.ppi
    gate = GATE_FRACTION * d_px
    holes = truth_holes_working_px(truth, h_px_to_working)
    n = len(holes)

    in_pair: dict[int, dict] = {}
    pair_units = []
    for m in truth["merged_pairs"]:
        unit = {"indices": list(m["indices"]), "sep_d": m["separation_d"], "kind": "pair"}
        pair_units.append(unit)
        for i in unit["indices"]:
            in_pair[i] = unit
    ragged_units = [
        {"indices": list(g), "sep_d": 0.0, "kind": "ragged"} for g in truth["ragged_groups"]
    ]

    exempt_units = [u for u in pair_units if u["sep_d"] < SPLIT_FLOOR_D] + ragged_units
    exempt_idx = {i for u in exempt_units for i in u["indices"]}
    matchable = [i for i in range(n) if i not in exempt_idx]
    isolated = [i for i in matchable if i not in in_pair]

    det, owners = detection_centers(blobs)
    cost = np.full((len(matchable), len(det)), 1e9)
    for a, hi in enumerate(matchable):
        for b in range(len(det)):
            dist = float(np.hypot(*(holes[hi] - det[b])))
            if dist <= gate:
                cost[a, b] = dist
    assignment = {}
    for a, b in hungarian(cost) if cost.size else []:
        if cost[a, b] < 1e8:
            assignment[matchable[a]] = b
    used_det = set(assignment.values())

    errors_in = {
        hi: float(np.hypot(*(holes[hi] - det[b]))) / frame.ppi for hi, b in assignment.items()
    }

    clusters = []
    absorbed: set[int] = set()
    for unit in exempt_units:
        pts = holes[unit["indices"]]
        centroid = pts.mean(axis=0)
        radius = float(np.hypot(*(pts - centroid).T).max()) + gate
        near = [
            b
            for b in range(len(det))
            if np.hypot(*(det[b] - centroid)) <= radius and b not in used_det
        ]
        absorbed.update(near)
        kinds = sorted({owners[b].kind.value for b in near})
        clusters.append(
            {
                "kind": unit["kind"],
                "sep_d": unit["sep_d"],
                "covered": bool(near),
                "blob_kinds": kinds,
                "exactly_one_ragged": unit["kind"] == "ragged"
                and len({id(owners[b]) for b in near}) == 1
                and kinds == [BlobKind.RAGGED.value],
            }
        )

    fp = sum(1 for b in range(len(det)) if b not in used_det and b not in absorbed)

    pairs = []
    for unit in pair_units:
        if unit["sep_d"] < SPLIT_FLOOR_D:
            continue
        i, j = unit["indices"]
        both = i in assignment and j in assignment
        worst = max((errors_in[k] for k in (i, j) if k in errors_in), default=float("inf"))
        pairs.append(
            {
                "sep_d": unit["sep_d"],
                "resolved": both,
                "max_err_in": worst if both else float("inf"),
                "split_blob": both and owners[assignment[i]].kind == BlobKind.SPLIT,
            }
        )

    es_errors = []
    for g in truth["groups"]:
        idx = g["hole_indices"]
        if (
            len(idx) < 2
            or any(k not in assignment for k in idx)
            or any(k in exempt_idx for k in idx)
        ):
            continue
        t_pts = holes[idx]
        d_pts = np.array([det[assignment[k]] for k in idx])
        es_errors.append(abs(_extreme_spread(t_pts) - _extreme_spread(d_pts)) / frame.ppi)

    iso_matched = [i for i in isolated if i in assignment]
    kinds: dict[str, int] = {}
    for b in blobs:
        kinds[b.kind.value] = kinds.get(b.kind.value, 0) + 1
    return RenderScore(
        scenario=scenario_name,
        seed=seed,
        n_isolated=len(isolated),
        matched_isolated=len(iso_matched),
        isolated_single=sum(
            1 for i in iso_matched if owners[assignment[i]].kind == BlobKind.SINGLE
        ),
        isolated_errors_in=[errors_in[i] for i in iso_matched],
        false_positives=fp,
        pairs=pairs,
        clusters=clusters,
        es_errors_in=es_errors,
        runtime_s=runtime,
        n_blobs=len(blobs),
        kinds=kinds,
    )


def _extreme_spread(pts: np.ndarray) -> float:
    return max(float(np.hypot(*(a - b))) for a, b in itertools.combinations(pts, 2))


def _percentile(values: Sequence[float], q: float) -> float:
    return float(np.percentile(values, q)) if len(values) else float("nan")


@dataclass
class ScenarioResult:
    """Aggregated metrics and verdicts for one scenario over all seeds."""

    name: str
    noisy: bool
    seeds: int
    n_isolated: int
    recall: float
    single_rate: float
    fp_max: int
    fp_mean: float
    p50_in: float
    p95_in: float
    max_in: float
    pairs_total: int
    pairs_resolved: int
    split_rate: float
    pair_err_max_in: float
    ragged_total: int
    ragged_ok: int
    subfloor_total: int
    subfloor_covered: int
    es_err_max_in: float
    runtime_mean_s: float
    runtime_max_s: float
    failures: list[str]

    @property
    def passed(self) -> bool:
        """True when the scenario scored at least one render and missed no P-11 row."""
        return not self.failures


def aggregate(
    name: str, noisy: bool, scores: Sequence[RenderScore], t: Thresholds = P11
) -> ScenarioResult:
    """Pool the per-render scores of one scenario into metrics and P-11 verdicts."""
    iso = sum(s.n_isolated for s in scores)
    matched = sum(s.matched_isolated for s in scores)
    errs = [e for s in scores for e in s.isolated_errors_in]
    pairs = [p for s in scores for p in s.pairs]
    clusters = [c for s in scores for c in s.clusters]
    ragged = [c for c in clusters if c["kind"] == "ragged"]
    subfloor = [c for c in clusters if c["kind"] == "pair"]
    resolved = [p for p in pairs if p["resolved"] and p["max_err_in"] <= t.pair_center_in]
    pair_errs = [p["max_err_in"] for p in pairs if p["resolved"]]
    es = [e for s in scores for e in s.es_errors_in]
    runtimes = [s.runtime_s for s in scores]
    res = ScenarioResult(
        name=name,
        noisy=noisy,
        seeds=len(scores),
        n_isolated=iso,
        recall=matched / iso if iso else 1.0,
        single_rate=sum(s.isolated_single for s in scores) / matched if matched else 1.0,
        fp_max=max((s.false_positives for s in scores), default=0),
        fp_mean=statistics.fmean(s.false_positives for s in scores) if scores else 0.0,
        p50_in=_percentile(errs, 50),
        p95_in=_percentile(errs, 95),
        max_in=max(errs, default=float("nan")),
        pairs_total=len(pairs),
        pairs_resolved=len(resolved),
        split_rate=len(resolved) / len(pairs) if pairs else float("nan"),
        pair_err_max_in=max(pair_errs, default=float("nan")),
        ragged_total=len(ragged),
        ragged_ok=sum(1 for c in ragged if c["exactly_one_ragged"]),
        subfloor_total=len(subfloor),
        subfloor_covered=sum(1 for c in subfloor if c["covered"]),
        es_err_max_in=max(es, default=float("nan")),
        runtime_mean_s=statistics.fmean(runtimes) if runtimes else 0.0,
        runtime_max_s=max(runtimes, default=0.0),
        failures=[],
    )
    res.failures = verdict(res, t)
    return res


def verdict(r: ScenarioResult, t: Thresholds = P11) -> list[str]:
    """Names of the P-11 rows a scenario misses (empty list means pass)."""
    fails = []
    if r.seeds == 0:
        return ["no renders scored"]
    recall_min = t.recall_noisy if r.noisy else t.recall_clean
    fp_max = t.fp_noisy if r.noisy else t.fp_clean
    if r.recall < recall_min:
        fails.append(f"recall {r.recall:.3f} < {recall_min}")
    if r.single_rate < t.single_read:
        fails.append(f"isolated holes read as single {r.single_rate:.3f} < {t.single_read}")
    if r.fp_max > fp_max:
        fails.append(f"false positives {r.fp_max} > {fp_max}")
    if r.n_isolated and r.p95_in > t.center_p95_in:
        fails.append(f"center p95 {r.p95_in:.4f} in > {t.center_p95_in}")
    if r.n_isolated and r.max_in > t.center_max_in:
        fails.append(f"center max {r.max_in:.4f} in > {t.center_max_in}")
    if r.pairs_total and r.split_rate < t.pair_resolved:
        fails.append(f"pair split rate {r.split_rate:.2f} < {t.pair_resolved}")
    if r.ragged_total and r.ragged_ok < r.ragged_total:
        fails.append(f"ragged blob identified {r.ragged_ok}/{r.ragged_total}")
    if not math.isnan(r.es_err_max_in) and r.es_err_max_in > t.es_error_in:
        fails.append(f"ES error {r.es_err_max_in:.4f} in > {t.es_error_in}")
    if r.runtime_max_s > t.runtime_s:
        fails.append(f"runtime {r.runtime_max_s:.2f} s > {t.runtime_s}")
    return fails


def evaluate_catalog(
    catalog: dict[str, dict],
    seeds: Sequence[int],
    params: DetectParams | None = None,
    render: Callable[[dict, int], tuple[np.ndarray, dict]] | None = None,
    only: Sequence[str] | None = None,
) -> list[ScenarioResult]:
    """Render, detect and score every (selected) catalog scenario for every seed.

    Raises ValueError for an empty seed list, an unknown `only` name, or a selection that matches no
    scenario, so a run can never pass by evaluating nothing.
    """
    if not list(seeds):
        raise ValueError("no seeds to evaluate (empty --seeds range)")
    unknown = sorted(set(only or ()) - set(catalog))
    if unknown:
        raise ValueError(
            f"unknown scenario(s) {', '.join(unknown)}; known: {', '.join(sorted(catalog))}"
        )
    if render is None:
        from tests.synth.generate import render as render_fn

        render = render_fn
    results = []
    for name, scenario in catalog.items():
        if only and name not in only:
            continue
        scores = []
        for seed in seeds:
            img, truth = render(scenario, seed)
            scores.append(score_render(name, seed, img, truth, params))
        results.append(aggregate(name, is_noisy(scenario), scores))
    if not results:
        raise ValueError("no scenarios evaluated (empty catalog)")
    return results


def parse_seeds(text: str) -> list[int]:
    """Parse '0-4,7' into [0, 1, 2, 3, 4, 7]; raises ValueError for malformed or empty ranges."""
    seeds: list[int] = []
    for part in text.split(","):
        lo, _, hi = part.strip().partition("-")
        try:
            first, last = int(lo), int(hi or lo)
        except ValueError:
            raise ValueError(f"malformed seed range {part!r} in {text!r}") from None
        if last < first or first < 0:
            raise ValueError(f"empty or negative seed range {part!r}")
        seeds.extend(range(first, last + 1))
    return seeds


def format_table(results: Sequence[ScenarioResult]) -> str:
    """Markdown metrics table with one row and one verdict per scenario."""
    head = (
        "| scenario | class | recall | single | FP max | p50 in | p95 in | max in | pairs resolved | "
        "ragged ok | sub-0.6d covered | ES err max in | runtime mean/max s | verdict |"
    )
    rows = [head, "|" + "---|" * 14]
    for r in results:
        pairs = f"{r.pairs_resolved}/{r.pairs_total}" if r.pairs_total else "-"
        ragged = f"{r.ragged_ok}/{r.ragged_total}" if r.ragged_total else "-"
        sub = f"{r.subfloor_covered}/{r.subfloor_total}" if r.subfloor_total else "-"
        es = "-" if math.isnan(r.es_err_max_in) else f"{r.es_err_max_in:.4f}"
        rows.append(
            f"| {r.name} | {'noisy' if r.noisy else 'clean'} | {r.recall:.3f} | {r.single_rate:.3f} | "
            f"{r.fp_max} | {r.p50_in:.4f} | {r.p95_in:.4f} | {r.max_in:.4f} | {pairs} | {ragged} | "
            f"{sub} | {es} | {r.runtime_mean_s:.2f}/{r.runtime_max_s:.2f} | "
            f"{'PASS' if r.passed else 'FAIL: ' + '; '.join(r.failures)} |"
        )
    return "\n".join(rows)


REAL_COUNTS_PATH = Path(__file__).resolve().parents[1] / "tests" / "real" / "counts.json"
REAL_PHOTO_DIR = Path(__file__).resolve().parents[1] / "data" / "real_photos"
REAL_GATE_FRACTION = 0.6
WORKING_PPI = 200.0


def load_real_counts(path: Path = REAL_COUNTS_PATH) -> dict:
    """Load the hand-read visual hole counts for the real photos (`tests/real/counts.json`)."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@dataclass
class RealPhotoScore:
    """Outcome of one detector run against one real photo's visual hole list (not exact truth)."""

    name: str
    visual_count: int
    detections: int
    found: int
    missed: list[int]
    false_positives: int
    known_decoy_hits: int
    other_false: int
    outside_sheet: int
    absorbed_in_clusters: int
    clusters_touched: int
    clusters_total: int
    kinds: dict[str, int]
    runtime_s: float

    @property
    def recall(self) -> float:
        """Found holes as a fraction of the visually counted holes."""
        return self.found / self.visual_count if self.visual_count else 1.0


def detect_real_photo(
    image_path: Path, entry: dict, params: DetectParams | None = None
) -> tuple[np.ndarray, list[Blob], np.ndarray, float]:
    """Rescale a photo to 200 ppi from its hand-measured grid pitch and run `detect_holes`.

    Returns (centres in original file px, blobs, rescaled working image, runtime seconds).
    """
    img = cv2.imread(str(image_path))
    if img is None:
        raise FileNotFoundError(image_path)
    scale = WORKING_PPI / float(entry["px_per_in"])
    working = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    frame = WorkingFrame(WORKING_PPI, working.shape[1], working.shape[0])
    t0 = time.perf_counter()
    blobs = detect_holes(working, frame, float(entry["bullet_d_in"]), None, params)
    runtime = time.perf_counter() - t0
    centers, _ = detection_centers(blobs)
    return centers / scale, blobs, working, runtime


def score_real_photo(
    name: str, entry: dict, centers: np.ndarray, blobs: Sequence[Blob], runtime: float = 0.0
) -> RealPhotoScore:
    """Match detection centres to the listed holes (gate 0.6 d, Hungarian) and classify the rest."""
    d_file = float(entry["bullet_d_in"]) * float(entry["px_per_in"])
    gate = REAL_GATE_FRACTION * d_file
    holes = np.array([[h["x"], h["y"]] for h in entry["holes"]], dtype=np.float64).reshape(-1, 2)
    decoys = np.array(
        [[h["x"], h["y"]] for h in entry.get("decoys", [])], dtype=np.float64
    ).reshape(-1, 2)
    polygon = entry.get("sheet_polygon")
    poly = np.array(polygon, dtype=np.float32).reshape(-1, 1, 2) if polygon else None

    cost = np.full((len(holes), len(centers)), 1e9)
    for i, hole in enumerate(holes):
        for j, c in enumerate(centers):
            dist = float(np.hypot(*(hole - c)))
            if dist <= gate:
                cost[i, j] = dist
    matched = {}
    for i, j in hungarian(cost) if cost.size else []:
        if cost[i, j] < 1e8:
            matched[i] = j
    used = set(matched.values())

    zones = []
    for cl in entry.get("clusters", []):
        pts = holes[cl["members"]]
        centroid = pts.mean(axis=0)
        zones.append((centroid, float(np.hypot(*(pts - centroid).T).max()) + gate))

    outside = absorbed = known = other = 0
    touched = set()
    for j, c in enumerate(centers):
        for k, (centroid, radius) in enumerate(zones):
            if float(np.hypot(*(c - centroid))) <= radius:
                touched.add(k)
        if j in used:
            continue
        if poly is not None and cv2.pointPolygonTest(poly, (float(c[0]), float(c[1])), False) < 0:
            outside += 1
        elif any(float(np.hypot(*(c - centroid))) <= radius for centroid, radius in zones):
            absorbed += 1
        elif len(decoys) and float(np.hypot(*(decoys - c).T).min()) <= 0.8 * d_file:
            known += 1
        else:
            other += 1
    kinds = {k.value: sum(1 for b in blobs if b.kind == k) for k in BlobKind}
    return RealPhotoScore(
        name=name,
        visual_count=len(holes),
        detections=len(centers),
        found=len(matched),
        missed=[i for i in range(len(holes)) if i not in matched],
        false_positives=known + other,
        known_decoy_hits=known,
        other_false=other,
        outside_sheet=outside,
        absorbed_in_clusters=absorbed,
        clusters_touched=len(touched),
        clusters_total=len(zones),
        kinds=kinds,
        runtime_s=runtime,
    )


def real_photo_scores(
    real_dir: Path = REAL_PHOTO_DIR,
    params: DetectParams | None = None,
    counts: dict | None = None,
    overlay_dir: Path | None = None,
) -> list[RealPhotoScore]:
    """Score every photo of `counts` found in `real_dir` (silently skips absent files)."""
    counts = counts or load_real_counts()
    scores = []
    for name, entry in counts["photos"].items():
        path = next(iter(sorted(real_dir.glob(f"{name}.*"))), None)
        if path is None:
            continue
        centers, blobs, working, runtime = detect_real_photo(path, entry, params)
        scores.append(score_real_photo(name, entry, centers, blobs, runtime))
        if overlay_dir is not None:
            overlay_dir.mkdir(parents=True, exist_ok=True)
            _draw_overlay(working, blobs, overlay_dir / f"{name}_detect.png")
    return scores


def _draw_overlay(working: np.ndarray, blobs: Sequence[Blob], out: Path) -> None:
    colors = {"single": (0, 200, 0), "split": (255, 100, 0), "ragged": (0, 0, 255)}
    vis = working.copy()
    for b in blobs:
        for cx, cy in b.centers_px:
            cv2.circle(vis, (int(cx), int(cy)), 14, colors[b.kind.value], 2)
    cv2.imwrite(str(out), vis)


def format_before_after(scores: Sequence[RealPhotoScore], counts: dict) -> str:
    """Found / false-positive counts of the T2.4 detector (stored) next to the live run."""
    before = counts["meta"]["baseline"]["before"]
    out = [
        "| file | holes by eye | found before | found now | false positives before | false positives now |",
        "|---|---|---|---|---|---|",
    ]
    totals = [0, 0, 0, 0, 0]
    for r in scores:
        b = before[r.name]
        out.append(
            f"| {r.name} | {r.visual_count} | {b['found']} | {r.found} | {b['false_positives']} | "
            f"{r.false_positives} |"
        )
        for i, v in enumerate(
            (r.visual_count, b["found"], r.found, b["false_positives"], r.false_positives)
        ):
            totals[i] += v
    out.append(
        f"| **total** | {totals[0]} | {totals[1]} | {totals[2]} | {totals[3]} | {totals[4]} |"
    )
    return "\n".join(out)


def format_real_table(scores: Sequence[RealPhotoScore], counts: dict) -> str:
    """Markdown detail table of the live real-photo scores."""
    out = [
        "| file | holes by eye | detected centres (single/split/ragged blobs) | found | missed | false positives "
        "(known decoys / other) | outside sheet | in touching clusters | runtime s | notes |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in scores:
        k = r.kinds
        note = counts["photos"][r.name].get("count_note", "")
        out.append(
            f"| {r.name} | {r.visual_count} | {r.detections} ({k['single']}/{k['split']}/{k['ragged']}) | "
            f"{r.found} ({r.recall:.0%}) | {r.visual_count - r.found} | {r.false_positives} "
            f"({r.known_decoy_hits} / {r.other_false}) | {r.outside_sheet} | {r.absorbed_in_clusters} | "
            f"{r.runtime_s:.2f} | {note} |"
        )
    return "\n".join(out)


SCORING_RULES = """\
- **Working image:** each render is rectified with the truth plane-to-image homography (`build_working`), so the score isolates detection from reference error. Truth hole centres go through `tests.synth.generate.plane_to_image` (honours the curved-sheet warp) and the same working transform. `detect_holes` runs on the whole working image (no ROI), so the grid, reference circle, aim marks, QR codes, text and staples are all false-positive bait.
- **Matching:** Hungarian assignment of truth holes to detected centres (every centre of every blob), distance gate 0.5 d (d = bullet diameter in working px). Errors are reported in inches (px / working ppi).
- **Isolated holes:** holes that are not part of a merged pair below 0.6 d and not in a ragged cluster. Recall, centre error (p50/p95/max over all seeds) and false positives (per render, max over seeds) are gated on these. The share read as `single` is a harness-added row (>= 95 %): an isolated hole read as split or ragged would be mis-handled by the ragged logic.
- **Merged pairs, separation >= 0.6 d:** resolved when both members are matched within the gate; the resolved share must be >= 90 % and each centre error <= 0.05 in (a pair whose cores do not touch may legitimately come back as two singles).
- **Merged pairs, separation < 0.6 d (scoring rule agreed for B-11):** not required to split. The pair counts as covered if at least one detection lies within (cluster radius + 0.5 d) of the pair centroid; detections inside that zone are neither hits nor false positives. Reported in the `sub-0.6d covered` column, informational.
- **Ragged clusters:** the zone holds exactly one detection and it is a `ragged` blob (gated: the shortfall logic in `rga.vision.ragged` depends on it).
- **ES error:** per truth group with all members matched and no exempt cluster, |ES(detected) - ES(truth)| <= 0.05 in.
- **Noisy vs clean:** a scenario is noisy (recall >= 0.95, FP <= 3 per render) when it has noise sigma >= 6, texture >= 5, JPEG < 80, specks, decoys, a shadow band, vignette > 0.1, print decoys, staples, wrinkles or a non-paper background; otherwise it is clean (recall >= 0.98, FP <= 1).
- **Runtime:** `detect_holes` wall time per render <= 3 s (the 12 MP end-to-end timing is asserted in `tests/integration/test_detection_accuracy.py`).
"""


def _report_header(seeds: Sequence[int], n_scenarios: int) -> str:
    """Report preamble: generator, library versions, catalog size and seed range."""
    return (
        f"- Generated by `python -m tools.eval_detection` (do not edit by hand).\n"
        f"- OpenCV {cv2.__version__}, NumPy {np.__version__}, Python {sys.version.split()[0]}.\n"
        f"- Catalog scenarios: {n_scenarios}; seeds {seeds[0]}-{seeds[-1]} ({len(seeds)} renders per scenario).\n"
    )


def gate_conclusions(results: Sequence[ScenarioResult]) -> str:
    """Gate and B-08 escalation sentences, computed from the scenario verdicts."""
    failing = [r for r in results if not r.passed]
    passed = len(results) - len(failing)
    if not failing:
        return (
            f"- Synthetic P-11 gate (B-11): **PASS**, {passed}/{len(results)} scenarios meet every "
            "threshold.\n- B-08 escalation: **not triggered** by the synthetic gate."
        )
    names = ", ".join(f"`{r.name}`" for r in failing)
    return (
        f"- Synthetic P-11 gate (B-11): **FAIL**, {passed}/{len(results)} scenarios pass; failing: "
        f"{names}.\n- B-08 escalation: **REQUIRED** for the failing scenario(s) above; the lead must "
        "escalate to the user before the Group 3 photo slices."
    )


def real_photo_conclusions(scores: Sequence[RealPhotoScore], counts: dict) -> str:
    """Real-photo totals against the stored T2.4 baseline, computed from the live scores."""
    if not scores:
        return "- Real photos: not evaluated (no photos found in the real-photo directory)."
    before = counts["meta"]["baseline"]["before"]
    visual = sum(s.visual_count for s in scores)
    found = sum(s.found for s in scores)
    fps = sum(s.false_positives for s in scores)
    found0 = sum(before[s.name]["found"] for s in scores)
    fps0 = sum(before[s.name]["false_positives"] for s in scores)
    return (
        f"- Real photos (not gated): {found} of {visual} visually counted holes found "
        f"({found / visual:.0%}) with {fps} false positives, against {found0} found and {fps0} false "
        "positives for the T2.4 detector. The lists are hand-read and approximate, so this shows "
        "direction, not accuracy."
    )


def build_report(
    results: Sequence[ScenarioResult],
    seeds: Sequence[int],
    params: DetectParams,
    real_scores: Sequence[RealPhotoScore],
    real_counts: dict | None = None,
) -> str:
    """Render the whole report; every verdict and conclusion sentence is computed from the inputs."""
    counts = real_counts or load_real_counts()
    failing = [r for r in results if not r.passed]
    verdict = (
        "**P-11 synthetic gate: PASS** (all scenarios meet every threshold)."
        if not failing
        else "**P-11 synthetic gate: FAIL** for " + ", ".join(f"`{r.name}`" for r in failing) + "."
    )
    passing = ", ".join(f"`{r.name}`" for r in results if r.passed) or "none"
    failing_text = (
        "none"
        if not failing
        else "; ".join(f"`{r.name}` ({'; '.join(r.failures)})" for r in failing)
    )
    thresholds = dataclasses.asdict(P11)
    params_dump = "\n".join(f"    {k} = {v!r}" for k, v in dataclasses.asdict(params).items())
    real_section = (
        "_no photos found in the real-photo directory_"
        if not real_scores
        else "**Before (T2.4 detector) and now:**\n\n"
        + format_before_after(real_scores, counts)
        + "\n\n**Detail (now):**\n\n"
        + format_real_table(real_scores, counts)
    )
    parts = [
        "# Spike report: hole-center detection (T2.4, real-photo tuning T2.19)\n",
        _report_header(seeds, len(results)),
        f"## Verdict\n\n{verdict}\n\n- Passing: {passing}\n- Failing: {failing_text}\n",
        "## Conclusions (computed from this run)\n\n"
        + gate_conclusions(results)
        + "\n"
        + real_photo_conclusions(real_scores, counts)
        + "\n",
        "## P-11 thresholds applied (spec.md section 6)\n\n```\n"
        + "\n".join(f"{k} = {v}" for k, v in thresholds.items())
        + "\n```\n",
        "## Scoring rules\n\n" + SCORING_RULES,
        "## Metrics (synthetic, all catalog scenarios, B-11)\n\n" + format_table(results) + "\n",
        "## Final `DetectParams`\n\n```\n" + params_dump + "\n```\n",
        DESIGN_POINTS,
        "## Real photos (NOT GATED, visual counts only)\n\n"
        "Each photo is rescaled to 200 ppi from its hand-measured grid pitch and run through the same "
        "`detect_holes`. The hole lists in `tests/real/counts.json` were read by eye from zoomed crops "
        "(about +/-5 px, 12 px on the 100 px holes) and are NOT exact ground truth; a hole counts as "
        "found when a detection centre lies within 0.6 bullet diameters. Detections outside the sheet "
        "are ignored, and extra centres inside a listed touching cluster are not counted as false "
        "positives.\n\n" + real_section + "\n",
        REAL_PHOTO_POLICY,
    ]
    return "\n".join(parts)


DESIGN_POINTS = """\
## Detector design (what the code does and which test pins each cue)

Every cue and threshold below is pinned by a test in `tests/integration/test_detection_accuracy.py`
that runs without the gitignored real photos. The pinning was checked by mutation on a scratch copy
of the repository that has no `data/real_photos` directory (see `test-evidence.md`, FY2.5). The
real-photo test (`test_detection_real_photos.py`) is an extra regression floor that skips when the
photos are absent; it pins nothing on its own.

- **Brightness channel.** Darkness is measured on the maximum colour channel (HSV value), so saturated print is never dark while neutral black, grey and brown hole material is. Pinned by `test_saturated_print_is_never_dark`, `test_hole_on_coloured_print_is_still_found` and the synthetic `red_grid_band_light_diamond` scenario.
- **Dark threshold `dark_rel` 0.5.** A hole core on a coloured disc is only 0.4-0.5 of the disc brightness. Pinned by `test_hole_core_at_half_its_disc_brightness_is_found`.
- **Grey route (`gray_dark_rel` 0.55 with `max_saturation` 0.3).** An unsaturated grey core at about 0.52 of the paper is dark by this route only, and coloured pixels at that brightness are not. Pinned by `test_grey_rimmed_hole_just_above_the_core_threshold_is_found` and `test_coloured_core_at_the_same_brightness_is_not_dark`.
- **Dark cores.** Pixels below the thresholds, small enclosed gaps filled (light-cored rings), thin print removed by an opening, then size, roundness, halo, core-fraction and crowding rules (`test_acceptance_rules`, `test_drawn_hole_is_found_at_its_centre`).
- **Area floor.** Round marks under 0.55 hole areas are never holes. Pinned by `test_round_marks_below_the_area_floor_are_never_holes`.
- **Light cores on black regions.** A top-hat bright blob enclosed by dark and not inside a ragged dark region. Pinned by the synthetic `black_bull_light_holes` and `black_dot_mixed` scenarios.
- **Tan cores.** A small, round, tan (backing-colour) blob that is bright enough (`tan_min_value`), not already covered by a detection; a tan region over `tan_max_a1` hole areas is background. Pinned by `test_tan_core_without_a_dark_rim_is_a_hole`, `test_dark_brown_dirt_mark_is_not_a_tan_core`, `test_tan_blob_larger_than_a_hole_core_is_background` and `test_streak_shaped_tan_blob_is_not_a_hole_core`.
- **Clumps of touching holes.** Distance-transform peaks give one centre per hole (the peak position itself). Pinned by `test_chain_of_touching_holes_is_split_into_one_centre_each` and `test_clump_centres_follow_each_hole_when_sizes_differ`.
- **Merged pairs.** A pair read needs two distance peaks of at least `pair_min_radius_r`, so a hole with a printed stub is not split. Pinned by `test_pair_path_needs_two_distance_peaks` and `test_print_heavy_group_yields_one_centre_per_hole`.
- **Classification.** Single (one round blob), split (a pair, or a clump with one centre per peak) or ragged (round and at least 1.4 hole areas; pinned by the synthetic ragged scenarios).

Rules removed because only one real photo supported them: subtraction of large printed dark regions, the pair axis-ratio cap, the clump core-radius restriction (FX2.6) and the clump cell-centroid centre (FY2.5, replaced by the plain peak position). The tan area cap is a structural 3 hole areas rather than a value fitted to one photo.
"""

REAL_PHOTO_POLICY = """\
## Real-photo policy (unchanged by the numbers above)

1. Keep manual review the primary path (D-06): the review editor must stay one click per missed hole and one click per false positive.
2. The hole lists are hand-read from five photos. Run the real-photo gate with exact ground truth from the user's corrected reviews (10-20 full-size photos; P-11 real row: p95 <= 0.05 in, recall >= 90 %, FP <= 5) and tune against that, not against these lists.
3. The round hanger holes, printed text and cardboard behind the sheet produce detections; the product ROI must exclude cardboard.
"""


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns 0 when every scenario passes, 1 on a P-11 miss, 2 on a bad run."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--catalog", default="tests/synth/catalog.json")
    ap.add_argument("--seeds", default="0-4")
    ap.add_argument("--only", default="")
    ap.add_argument("--report", default="")
    ap.add_argument("--real-dir", default="data/real_photos")
    ap.add_argument("--overlay-dir", default="")
    args = ap.parse_args(argv)

    from tests.synth.generate import load_catalog

    try:
        catalog = load_catalog(args.catalog)
        seeds = parse_seeds(args.seeds)
        only = [s.strip() for s in args.only.split(",") if s.strip()]
        params = DetectParams()
        results = evaluate_catalog(catalog, seeds, params, only=only or None)
    except (ValueError, OSError) as exc:
        print(f"error: nothing was evaluated: {exc}", file=sys.stderr)
        return 2
    print(format_table(results))
    failed = [r.name for r in results if not r.passed]
    print(
        f"\n{len(results) - len(failed)}/{len(results)} scenarios pass; failing: {failed or 'none'}"
    )
    if args.report:
        real_dir = Path(args.real_dir)
        overlay = Path(args.overlay_dir) if args.overlay_dir else None
        counts = load_real_counts()
        scores = real_photo_scores(real_dir, params, counts, overlay) if real_dir.is_dir() else []
        Path(args.report).write_text(
            build_report(results, seeds, params, scores, counts), encoding="utf-8"
        )
        print(f"report written to {args.report}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
