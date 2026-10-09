"""T2: the real-photo evaluation tool, driven by synthetic scenarios exported in the real-photo format."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import cv2
import numpy as np
import pytest

import tools.eval_detection as eval_detection
import tools.eval_real_photos as tool
from rga.domain.types import (
    Blob,
    BlobKind,
    CircleCandidate,
    ReferenceKind,
    ReferenceResult,
    ShotOrigin,
    WorkingFrame,
)
from rga.vision.pipeline import ReferenceProposal, build_working_image, manual_reference
from rga.vision.ragged import ResolveResult
from rga.vision.transform import apply_h
from tests.synth.generate import load_catalog, plane_to_image, render_named
from tools.eval_real_photos import (
    GroupReport,
    PhotoReport,
    TruthError,
    parse_truth,
    plane_in_to_oriented_px,
    truth_document,
)

CATALOG = load_catalog()
SCENARIOS = ["clean_grid", "skew15_grid", "multi_dot_target"]
CIRCLE_SCALE_PCT = "1.0"
EXIT_PASS, EXIT_FAIL, EXIT_USAGE, EXIT_OUTSTANDING = 0, 1, 2, 3
KIND_BY_DIAMETER = {
    1.0: ReferenceKind.CIRCLE_1IN,
    1.5: ReferenceKind.CIRCLE_1_5IN,
    3.0: ReferenceKind.CIRCLE_3IN,
}
REAL_PHOTOS = Path(__file__).resolve().parents[2] / "data" / "real_photos"


def _display_order(corners_px: np.ndarray) -> list[int]:
    by_y = np.argsort(corners_px[:, 1])
    top = sorted(by_y[:2], key=lambda i: corners_px[i, 0])
    bottom = sorted(by_y[2:], key=lambda i: -corners_px[i, 0])
    return [top[0], top[1], bottom[0], bottom[1]]


def confirmed_reference(truth: dict) -> tuple[ReferenceKind, ReferenceResult]:
    """The reference a user would confirm: built from the printed circle's or grid's true geometry."""
    ref = truth["reference"]
    if ref["kind"] == "grid":
        corners = np.array(ref["corners_px"], dtype=np.float64)
        ordered = corners[_display_order(corners)]
        result = manual_reference(
            ReferenceKind.GRID_1IN,
            grid_corners_px=[tuple(c) for c in ordered],
            squares=(ref["squares_across"], ref["squares_down"]),
        )
        return ReferenceKind.GRID_1IN, result
    kind = KIND_BY_DIAMETER[ref["diameter_in"]]
    cx, cy = ref["center_in"]
    r = ref["diameter_in"] / 2
    angles = np.linspace(0, 2 * math.pi, 12, endpoint=False)
    plane = np.column_stack([cx + r * np.cos(angles), cy + r * np.sin(angles)])
    edge = plane_to_image(truth, plane)
    return kind, manual_reference(kind, edge_points_px=[tuple(p) for p in edge])


def export_scenario(
    name: str,
    seed: int,
    folder: Path,
    *,
    with_reference: bool = True,
    hand_es: bool = False,
    stem: str | None = None,
) -> dict:
    """Write `<stem>.jpg` and `<stem>.truth.json` for a rendered scenario; returns the truth document."""
    img, truth = render_named(name, seed)
    stem = stem or f"{name}_s{seed}"
    folder.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(folder / f"{stem}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    kind, reference = confirmed_reference(truth)
    holes_px = plane_to_image(truth, np.array(truth["holes_in"]))
    groups = []
    for i, g in enumerate(truth["groups"]):
        idx = g["hole_indices"]
        pts = np.array([truth["holes_in"][k] for k in idx])
        es = max(math.dist(a, b) for a in pts for b in pts) if len(pts) > 1 else None
        groups.append(
            {
                "name": f"g{i + 1}",
                "roi_px": g["roi_px"],
                "aim_px": g["aim_px"],
                "shots_expected": len(idx),
                "hole_indices": idx,
                "hand_es_in": es if hand_es else None,
            }
        )
    doc = truth_document(
        reference_kind=kind,
        distance_yd=100,
        bullet_diameter_in=truth["bullet_diameter_in"],
        holes_px=holes_px,
        groups=groups,
        reference=reference if with_reference else None,
        aim_px=truth["aim_px"],
    )
    (folder / f"{stem}{tool.TRUTH_SUFFIX}").write_text(tool.truth_to_json(doc), encoding="utf-8")
    return doc


def run(folder: Path, *extra: str) -> int:
    return tool.main(["--dir", str(folder), *extra])


def run_circle(folder: Path, *extra: str) -> int:
    """Synthetic discs render about 1.2 px wider than the annotated circle, so allow 1 % scale error."""
    return run(folder, "--scale-pct", CIRCLE_SCALE_PCT, *extra)


@pytest.fixture(scope="module")
def exported(tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("real_photos")
    for name in SCENARIOS:
        export_scenario(name, 0, folder, hand_es=name == "clean_grid")
    return folder


# ----------------------------------------------------------------------------------- end to end


def test_three_synthetic_photos_pass_the_real_photo_gate_and_fill_the_report(
    exported: Path, tmp_path: Path, capsys
) -> None:
    out = tmp_path / "report.json"
    assert run_circle(exported, "--json", str(out)) == EXIT_PASS
    report = json.loads(out.read_text())
    assert report["schema"] == tool.REPORT_SCHEMA
    assert report["passed"] is True and report["failures"] == []
    assert report["provisional"] is True
    assert report["thresholds"] == {
        "scale_error_pct": float(CIRCLE_SCALE_PCT),
        "center_p95_in": 0.05,
        "recall": 0.90,
        "false_positives_per_photo": 5,
        "es_error_in": 0.05,
        "runtime_s": 3.0,
    }
    s = report["summary"]
    assert s["photos_total"] == 3 and s["photos_scored"] == 3 and s["groups"] == 5
    fixture_holes = {n: len(render_named(n, 0)[1]["holes_in"]) for n in SCENARIOS}
    assert fixture_holes == {"clean_grid": 10, "skew15_grid": 10, "multi_dot_target": 15}
    assert sum(fixture_holes.values()) == 10 + 10 + 15 == 35
    assert s["holes"] == 35
    for photo in report["photos"]:
        scenario = photo["name"].removesuffix("_s0")
        assert sum(g["holes"] for g in photo["groups"]) == fixture_holes[scenario], photo["name"]
    for key in (
        "recall",
        "center_p50_in",
        "center_p95_in",
        "center_max_in",
        "false_positives_max_per_photo",
        "scale_error_max_pct",
        "es_error_max_in",
        "es_vs_hand_max_in",
        "runtime_max_s",
    ):
        assert key in s and not (isinstance(s[key], float) and math.isnan(s[key])), key
    assert s["recall"] == 1.0 and s["center_p95_in"] <= 0.05
    assert s["scale_error_photos"] == 3 and s["scale_error_max_pct"] <= 1.0
    assert s["es_vs_hand_groups"] == 1
    names = [p["name"] for p in report["photos"]]
    assert names == sorted(names) and len(names) == 3
    for photo in report["photos"]:
        assert photo["status"] == "ok" and photo["reference_source"] == "auto"
        assert photo["proposals"] >= 1 and photo["proposal_rank"] >= 1
        assert photo["runtime_s"] > 0
        for g in photo["groups"]:
            assert g["matched"] == g["holes"] and g["false_positives"] <= 5
            assert g["shots_resolved"] == g["shots_expected"]
            assert g["es_app_in"] is not None and g["es_error_in"] <= 0.05
    multi = next(p for p in report["photos"] if p["name"].startswith("multi_dot"))
    assert len(multi["groups"]) == 3
    text = capsys.readouterr().out
    assert "PASS" in text and "provisional" in text and "3/3 photos scored" in text


def export_ragged(folder: Path, shots_expected: int | None = None, keep_all: bool = True) -> dict:
    """ragged_10x7 (3 holes in one ragged blob) beside clean_grid, which supplies the ES row."""
    export_scenario("clean_grid", 0, folder)
    doc = export_scenario("ragged_10x7", 0, folder)
    if shots_expected is not None:
        doc["groups"][0]["shots_expected"] = shots_expected
    if not keep_all:
        _, truth = render_named("ragged_10x7", 0)
        dropped = set(truth["ragged_groups"][0][1:])
        doc["groups"][0]["hole_indices"] = [
            i for i in doc["groups"][0]["hole_indices"] if i not in dropped
        ]
    (folder / f"ragged_10x7_s0{tool.TRUTH_SUFFIX}").write_text(
        tool.truth_to_json(doc), encoding="utf-8"
    )
    return doc


def ragged_group(folder: Path, *extra: str) -> tuple[int, dict, dict]:
    out = folder / "r.json"
    rc = run_circle(folder, "--json", str(out), *extra)
    report = json.loads(out.read_text())
    photo = next(p for p in report["photos"] if p["name"].startswith("ragged"))
    return rc, report, photo["groups"][0]


def test_ragged_holes_are_credited_only_for_the_resolved_shots_placed_inside_the_blob(
    tmp_path: Path,
) -> None:
    export_ragged(tmp_path)
    rc, report, g = ragged_group(tmp_path)
    assert rc == EXIT_PASS and report["verdict"] == "PASS"
    assert g["ragged_holes"] == 3 and g["ragged_credited"] == 3 and g["shots_estimated"] == 3
    assert g["holes"] == 10 and g["matched"] == 10 and g["recall"] == 1.0
    assert g["false_positives"] == 0


def test_a_ragged_blob_filled_with_too_few_shots_is_a_must_fail_case(tmp_path: Path) -> None:
    """Three annotated holes in one blob but no resolved shot inside it: three misses."""
    export_ragged(tmp_path, shots_expected=7)
    rc, report, g = ragged_group(tmp_path)
    assert rc == EXIT_FAIL and report["verdict"] == "FAIL" and report["passed"] is False
    assert g["ragged_holes"] == 3 and g["ragged_credited"] == 0 and g["matched"] == 7
    assert any("recall" in f for f in report["failures"])


def test_surplus_shots_in_a_ragged_blob_are_false_positives(tmp_path: Path) -> None:
    export_ragged(tmp_path, keep_all=False)
    _, _, g = ragged_group(tmp_path)
    assert g["holes"] == 8 and g["ragged_holes"] == 1 and g["ragged_credited"] == 1
    assert g["false_positives"] == 2


def test_a_single_unmatched_hole_is_a_miss_not_a_ragged_cluster(tmp_path: Path) -> None:
    doc = export_scenario("clean_grid", 0, tmp_path)
    path = tmp_path / f"clean_grid_s0{tool.TRUTH_SUFFIX}"
    x, y = doc["holes_px"][0]
    doc["holes_px"][0] = [x + 200.0, y]
    path.write_text(tool.truth_to_json(doc), encoding="utf-8")
    out = tmp_path / "r.json"
    run(tmp_path, "--json", str(out))
    g = json.loads(out.read_text())["photos"][0]["groups"][0]
    assert g["ragged_holes"] == 0 and g["matched"] == 9 and g["recall"] == pytest.approx(0.9)


def test_the_app_es_is_compared_with_the_hand_measured_es(exported: Path, tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    run(exported, "--json", str(out))
    photo = next(
        p for p in json.loads(out.read_text())["photos"] if p["name"].startswith("clean_grid")
    )
    g = photo["groups"][0]
    _, truth = render_named("clean_grid", 0)
    idx = truth["groups"][0]["hole_indices"]
    pts = [truth["holes_in"][i] for i in idx]
    fixture_es = max(math.dist(a, b) for a in pts for b in pts)
    assert g["es_hand_in"] == pytest.approx(fixture_es)
    assert g["es_app_in"] == pytest.approx(fixture_es, abs=0.05)
    assert g["es_vs_hand_in"] == pytest.approx(abs(g["es_app_in"] - fixture_es))
    assert g["es_vs_hand_in"] <= 0.05


def test_scoring_reuses_the_detection_harness_scorer() -> None:
    assert tool.score_blobs is eval_detection.score_blobs
    assert tool.aggregate is eval_detection.aggregate


def test_overlay_images_are_written_on_request(exported: Path, tmp_path: Path) -> None:
    overlay = tmp_path / "overlays"
    run(exported, "--overlay-dir", str(overlay))
    assert sorted(p.name for p in overlay.glob("*.png")) == [
        "clean_grid_s0.png",
        "multi_dot_target_s0.png",
        "skew15_grid_s0.png",
    ]


# ----------------------------------------------------------------------------------- gate rows


def group_report(**overrides) -> GroupReport:
    base = {
        "name": "g",
        "holes": 10,
        "ragged_holes": 0,
        "ragged_credited": 0,
        "shots_expected": 10,
        "detections": 10,
        "matched": 10,
        "recall": 1.0,
        "false_positives": 0,
        "center_errors_in": [0.01] * 10,
        "shots_resolved": 10,
        "shots_estimated": 0,
        "resolve_status": "ok",
        "es_app_in": 1.0,
        "es_truth_in": 1.0,
        "es_error_in": 0.0,
        "es_hand_in": None,
        "es_vs_hand_in": None,
        "poi_error_in": 0.0,
    }
    return GroupReport(**{**base, **overrides})


def photo_report(group: dict | None = None, **overrides) -> PhotoReport:
    base = {
        "name": "p",
        "status": "ok",
        "runtime_s": 1.0,
        "scale_error_pct": 0.0,
        "scale_status": "evaluated",
    }
    return PhotoReport(**{**base, **overrides}, groups=[group_report(**(group or {}))])


def failures_of(*reports: PhotoReport) -> list[str]:
    return tool.summarize(list(reports))["failures"]


def test_a_clean_report_passes_every_row() -> None:
    assert failures_of(photo_report()) == []


@pytest.mark.parametrize(
    ("group", "photo", "fragment"),
    [
        ({"matched": 8, "center_errors_in": [0.01] * 8}, {}, "recall"),
        ({"center_errors_in": [0.01] * 9 + [0.2]}, {}, "center error p95"),
        ({"false_positives": 6}, {}, "false positives"),
        ({}, {"scale_error_pct": 0.6}, "scale error"),
        ({}, {"scale_error_pct": -0.6}, "scale error"),
        ({"es_error_in": 0.06}, {}, "group ES error"),
        ({"es_vs_hand_in": 0.06}, {}, "hand_es_in"),
        ({}, {"runtime_s": 3.1}, "runtime"),
    ],
)
def test_each_row_fails_just_past_its_limit(group: dict, photo: dict, fragment: str) -> None:
    failures = failures_of(photo_report(group, **photo))
    assert len(failures) == 1 and fragment in failures[0], failures


@pytest.mark.parametrize(
    ("group", "photo"),
    [
        ({"matched": 9, "center_errors_in": [0.01] * 9}, {}),
        ({"center_errors_in": [0.05] * 10}, {}),
        ({"false_positives": 5}, {}),
        ({}, {"scale_error_pct": 0.5}),
        ({}, {"scale_error_pct": -0.5}),
        ({"es_error_in": 0.05}, {}),
        ({"es_vs_hand_in": 0.05}, {}),
        ({}, {"runtime_s": 3.0}),
    ],
)
def test_each_row_passes_exactly_at_its_limit(group: dict, photo: dict) -> None:
    assert failures_of(photo_report(group, **photo)) == []


def photo_of(*groups: GroupReport, **overrides) -> PhotoReport:
    base = {
        "name": "p",
        "status": "ok",
        "runtime_s": 1.0,
        "scale_error_pct": 0.0,
        "scale_status": "evaluated",
    }
    return PhotoReport(**{**base, **overrides}, groups=list(groups))


def incomplete_of(*reports: PhotoReport) -> list[str]:
    return tool.summarize(list(reports))["incomplete"]


def test_groups_with_estimated_shots_report_es_but_do_not_gate_on_it() -> None:
    estimated = group_report(es_error_in=0.4, es_vs_hand_in=0.4, shots_estimated=3)
    assert failures_of(photo_of(group_report(), estimated)) == []


def test_recall_is_pooled_over_groups_and_false_positives_per_photo() -> None:
    low = group_report(matched=8, recall=0.8, center_errors_in=[0.01] * 8)
    assert failures_of(photo_of(low, group_report(), group_report())) == []
    two_fp = group_report(false_positives=3)
    assert any("false positives 6" in f for f in failures_of(photo_of(two_fp, two_fp)))


def test_unevaluable_photos_fail_the_gate_and_are_named() -> None:
    bad = PhotoReport("broken", "unreadable", "not an image")
    failures = failures_of(photo_report(), bad)
    assert len(failures) == 1 and "broken" in failures[0]


def test_a_complete_clean_report_has_no_incomplete_rows_and_the_verdict_is_pass() -> None:
    report = tool.build_report([photo_report()])
    assert report["incomplete"] == [] and report["verdict"] == "PASS" and report["passed"] is True


@pytest.mark.parametrize(
    ("reports", "fragment"),
    [
        ([photo_report(scale_error_pct=None, scale_status="no_annotated_reference")], "scale row"),
        (
            [photo_report(scale_error_pct=None, scale_status="manual_reference_needed")],
            "scale row",
        ),
        ([photo_report({"center_errors_in": [], "matched": 0})], "center error row"),
        ([photo_report({"shots_estimated": 2})], "group ES row"),
        ([photo_report({"es_error_in": None, "es_app_in": None})], "group ES row"),
    ],
)
def test_a_row_that_cannot_be_evaluated_is_incomplete_and_never_a_pass(
    reports: list[PhotoReport], fragment: str
) -> None:
    result = tool.summarize(reports)
    assert any(fragment in reason for reason in result["incomplete"]), result
    report = tool.build_report(reports)
    assert report["verdict"] in ("INCOMPLETE", "FAIL") and report["passed"] is False


def test_one_photo_without_an_annotated_reference_makes_a_complete_set_incomplete() -> None:
    good = photo_report(name="a")
    unchecked = photo_report(name="b", scale_error_pct=None, scale_status="no_annotated_reference")
    assert incomplete_of(good) == []
    reasons = incomplete_of(good, unchecked)
    assert len(reasons) == 1 and "b" in reasons[0]
    assert tool.build_report([good, unchecked])["verdict"] == "INCOMPLETE"


def test_a_failure_takes_precedence_over_incomplete_in_the_verdict() -> None:
    report = tool.build_report(
        [
            photo_report(
                {"false_positives": 9}, scale_error_pct=None, scale_status="no_annotated_reference"
            )
        ]
    )
    assert report["verdict"] == "FAIL" and report["incomplete"]


def test_nothing_scored_is_incomplete() -> None:
    assert incomplete_of(PhotoReport("broken", "unreadable")) == ["no photo was scored"]


def test_review_ten_case_two_ragged_blobs_over_four_distinct_holes_now_fails() -> None:
    """Four distinct holes read as two ragged blobs with one resolved shot each: two misses."""
    holes = [[100, 100], [130, 100], [400, 100], [430, 100]]
    truth = parse_truth(
        {
            "reference_kind": "circle_1in",
            "distance_yd": 100,
            "bullet_diameter_in": 0.224,
            "holes_px": holes,
        }
    )
    frame = WorkingFrame(ppi=200.0, width_px=800, height_px=300)

    def ragged(x0: float) -> Blob:
        outline = tuple(
            (int(x), int(y))
            for x, y in ((x0 - 25, 75), (x0 + 55, 75), (x0 + 55, 125), (x0 - 25, 125))
        )
        return Blob(BlobKind.RAGGED, outline, 4000.0, ((x0 + 15.0, 100.0),), 0.5)

    def resolved(shots) -> ResolveResult:
        return ResolveResult(tuple((x, 100.0, ShotOrigin.ESTIMATED, 0.5) for x in shots), "ok", ())

    blobs = [ragged(100), ragged(400)]
    group = truth.groups[0]
    short = tool._score_group(group, truth, blobs, resolved([115, 415]), frame, np.eye(3))
    assert (short.ragged_holes, short.ragged_credited, short.matched) == (4, 2, 2)
    assert short.recall == 0.5
    assert any("recall" in f for f in tool.summarize([photo_of(short)])["failures"])
    full = tool._score_group(group, truth, blobs, resolved([108, 122, 408, 422]), frame, np.eye(3))
    assert (full.ragged_credited, full.matched, full.false_positives) == (4, 4, 0)


def test_a_hole_inside_a_ragged_outline_is_never_matched_a_second_time_by_a_nearby_shot() -> None:
    truth = parse_truth(
        {
            "reference_kind": "circle_1in",
            "distance_yd": 100,
            "bullet_diameter_in": 0.224,
            "holes_px": [[104, 100]],
        }
    )
    frame = WorkingFrame(ppi=200.0, width_px=400, height_px=300)
    outline = ((80, 80), (110, 80), (110, 120), (80, 120))
    blob = Blob(BlobKind.RAGGED, outline, 1200.0, ((95.0, 100.0),), 0.5)
    shots = ResolveResult(
        (
            (95.0, 100.0, ShotOrigin.ESTIMATED, 0.5),
            (125.0, 100.0, ShotOrigin.DETECTED, 0.9),
        ),
        "ok",
        (),
    )
    g = tool._score_group(truth.groups[0], truth, [blob], shots, frame, np.eye(3))
    assert (g.holes, g.ragged_credited, g.matched, g.false_positives) == (1, 1, 1, 1)
    assert g.recall == 1.0


def test_shots_far_from_every_annotated_hole_are_false_positives_and_misses() -> None:
    truth = parse_truth(
        {
            "reference_kind": "circle_1in",
            "distance_yd": 100,
            "bullet_diameter_in": 0.224,
            "holes_px": [[100, 100], [300, 100]],
        }
    )
    frame = WorkingFrame(ppi=200.0, width_px=500, height_px=300)
    shots = ResolveResult(
        ((100.0, 100.0, ShotOrigin.DETECTED, 1.0), (300.0, 190.0, ShotOrigin.DETECTED, 1.0)),
        "ok",
        (),
    )
    g = tool._score_group(truth.groups[0], truth, [], shots, frame, np.eye(3))
    assert (g.matched, g.false_positives, g.recall) == (1, 1, 0.5)


# ----------------------------------------------------------------------------------- the gate bites


def test_wrong_truth_fails_the_gate_with_named_rows(tmp_path: Path, capsys) -> None:
    doc = export_scenario("clean_grid", 0, tmp_path)
    path = tmp_path / f"clean_grid_s0{tool.TRUTH_SUFFIX}"
    shifted = [[x + 80.0, y] for x, y in doc["holes_px"]]
    doc["holes_px"] = shifted
    doc["groups"][0]["hand_es_in"] = 9.0
    path.write_text(tool.truth_to_json(doc), encoding="utf-8")
    out = tmp_path / "r.json"
    assert run(tmp_path, "--json", str(out)) == EXIT_FAIL
    report = json.loads(out.read_text())
    assert report["passed"] is False
    joined = " | ".join(report["failures"])
    assert "recall" in joined
    assert "FAIL" in capsys.readouterr().out


def test_hand_es_far_from_the_app_es_fails_only_the_hand_row(tmp_path: Path) -> None:
    doc = export_scenario("clean_grid", 0, tmp_path, hand_es=True)
    path = tmp_path / f"clean_grid_s0{tool.TRUTH_SUFFIX}"
    doc["groups"][0]["hand_es_in"] += 0.5
    path.write_text(tool.truth_to_json(doc), encoding="utf-8")
    out = tmp_path / "r.json"
    assert run(tmp_path, "--json", str(out)) == EXIT_FAIL
    failures = json.loads(out.read_text())["failures"]
    assert len(failures) == 1 and "hand_es_in" in failures[0]


def test_a_reference_scale_off_by_one_percent_fails_the_scale_row(tmp_path: Path) -> None:
    doc = export_scenario("clean_grid", 0, tmp_path)
    path = tmp_path / f"clean_grid_s0{tool.TRUTH_SUFFIX}"
    h = np.array(doc["reference"]["h_px_to_plane"])
    doc["reference"]["h_px_to_plane"] = (np.diag([1.02, 1.02, 1.0]) @ h).tolist()
    path.write_text(tool.truth_to_json(doc), encoding="utf-8")
    out = tmp_path / "r.json"
    assert run(tmp_path, "--json", str(out)) == EXIT_FAIL
    report = json.loads(out.read_text())
    assert any("scale error" in f for f in report["failures"])
    assert abs(report["photos"][0]["scale_error_pct"]) > 0.5


def test_a_truth_file_without_a_confirmed_reference_can_never_pass(tmp_path: Path, capsys) -> None:
    export_scenario("clean_grid", 0, tmp_path, with_reference=False)
    out = tmp_path / "r.json"
    assert run(tmp_path, "--json", str(out)) == EXIT_FAIL
    report = json.loads(out.read_text())
    assert report["verdict"] == "INCOMPLETE" and report["passed"] is False
    assert report["failures"] == [] and any("scale row" in r for r in report["incomplete"])
    assert report["photos"][0]["scale_status"] == "no_annotated_reference"
    assert report["summary"]["scale_error_photos"] == 0
    assert "INCOMPLETE" in capsys.readouterr().out


def test_a_photo_whose_grid_is_not_found_reports_that_a_manual_reference_is_needed(
    tmp_path: Path,
) -> None:
    export_scenario("clean_grid", 0, tmp_path)
    export_scenario("clean_grid", 1, tmp_path, stem="blank")
    cv2.imwrite(str(tmp_path / "blank.jpg"), np.full((1925, 1547, 3), 235, np.uint8))
    out = tmp_path / "r.json"
    run(tmp_path, "--json", str(out))
    blank = next(p for p in json.loads(out.read_text())["photos"] if p["name"] == "blank")
    assert blank["reference_source"] == "annotated"
    assert blank["scale_status"] == "manual_reference_needed" and blank["scale_error_pct"] is None


# ----------------------------------------------------------------------------------- outstanding gate


def test_an_empty_folder_reports_the_real_photo_gate_outstanding(tmp_path: Path, capsys) -> None:
    assert run(tmp_path) == EXIT_OUTSTANDING
    err = capsys.readouterr().err
    assert "real-photo gate outstanding" in err and "has no photos" in err


def test_a_missing_folder_reports_the_real_photo_gate_outstanding(tmp_path: Path, capsys) -> None:
    assert run(tmp_path / "absent") == EXIT_OUTSTANDING
    assert "real-photo gate outstanding" in capsys.readouterr().err


def test_photos_without_truth_files_report_the_gate_outstanding(tmp_path: Path, capsys) -> None:
    export_scenario("clean_grid", 0, tmp_path)
    (tmp_path / f"clean_grid_s0{tool.TRUTH_SUFFIX}").unlink()
    assert run(tmp_path) == EXIT_OUTSTANDING
    assert "no <photo>.truth.json file" in capsys.readouterr().err


def test_a_truth_file_alone_is_not_a_photo(tmp_path: Path, capsys) -> None:
    (tmp_path / f"lonely{tool.TRUTH_SUFFIX}").write_text("{}", encoding="utf-8")
    assert run(tmp_path) == EXIT_OUTSTANDING
    assert "has no photos" in capsys.readouterr().err


def test_unannotated_photos_are_listed_but_do_not_block_scoring(tmp_path: Path, capsys) -> None:
    export_scenario("clean_grid", 0, tmp_path)
    extra = tmp_path / "extra.jpg"
    extra.write_bytes((tmp_path / "clean_grid_s0.jpg").read_bytes())
    assert run(tmp_path) == EXIT_PASS
    assert "not annotated (not scored): extra.jpg" in capsys.readouterr().out


# ----------------------------------------------------------------------------------- unusable photos


def test_a_photo_that_is_not_an_image_fails_the_gate_as_unreadable(tmp_path: Path) -> None:
    export_scenario("clean_grid", 0, tmp_path)
    (tmp_path / "clean_grid_s0.jpg").write_bytes(b"not an image")
    out = tmp_path / "r.json"
    assert run(tmp_path, "--json", str(out)) == EXIT_FAIL
    report = json.loads(out.read_text())
    assert report["photos"][0]["status"] == "unreadable"
    assert "could not be evaluated" in report["failures"][0]


def test_holes_outside_the_image_are_reported_as_bad_truth(tmp_path: Path) -> None:
    doc = export_scenario("clean_grid", 0, tmp_path)
    doc["holes_px"][0] = [99999.0, 5.0]
    (tmp_path / f"clean_grid_s0{tool.TRUTH_SUFFIX}").write_text(
        tool.truth_to_json(doc), encoding="utf-8"
    )
    out = tmp_path / "r.json"
    assert run(tmp_path, "--json", str(out)) == EXIT_FAIL
    photo = json.loads(out.read_text())["photos"][0]
    assert photo["status"] == "bad_truth" and "outside" in photo["detail"]


def test_no_reference_found_and_none_annotated_cannot_be_evaluated(tmp_path: Path) -> None:
    img, truth = render_named("no_reference", 0)
    cv2.imwrite(str(tmp_path / "bare.jpg"), img)
    doc = truth_document(
        reference_kind=ReferenceKind.CIRCLE_1IN,
        distance_yd=100,
        bullet_diameter_in=truth["bullet_diameter_in"],
        holes_px=plane_to_image(truth, np.array(truth["holes_in"])),
        groups=[],
    )
    del doc["groups"]
    (tmp_path / f"bare{tool.TRUTH_SUFFIX}").write_text(tool.truth_to_json(doc), encoding="utf-8")
    out = tmp_path / "r.json"
    assert run(tmp_path, "--json", str(out)) == EXIT_FAIL
    assert json.loads(out.read_text())["photos"][0]["status"] == "no_reference"


def test_an_annotated_reference_is_used_when_nothing_is_proposed(tmp_path: Path) -> None:
    img, truth = render_named("no_reference", 0)
    cv2.imwrite(str(tmp_path / "bare.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    h_plane = np.array(truth["h_plane_to_image"], dtype=np.float64)
    reference = ReferenceResult(
        ReferenceKind.CIRCLE_1IN,
        "manual",
        tuple(tuple(float(v) for v in row) for row in np.linalg.inv(h_plane)),
        None,
        (),
    )
    doc = truth_document(
        reference_kind=ReferenceKind.CIRCLE_1IN,
        distance_yd=100,
        bullet_diameter_in=truth["bullet_diameter_in"],
        holes_px=plane_to_image(truth, np.array(truth["holes_in"])),
        groups=[],
        reference=reference,
    )
    del doc["groups"]
    (tmp_path / f"bare{tool.TRUTH_SUFFIX}").write_text(tool.truth_to_json(doc), encoding="utf-8")
    out = tmp_path / "r.json"
    run(tmp_path, "--json", str(out))
    photo = json.loads(out.read_text())["photos"][0]
    assert photo["status"] == "ok" and photo["reference_source"] == "annotated"
    assert photo["scale_error_pct"] is None
    assert photo["groups"][0]["recall"] >= 0.9


# ----------------------------------------------------------------------------------- truth format


def minimal_doc() -> dict:
    return {
        "reference_kind": "circle_1in",
        "distance_yd": 100,
        "bullet_diameter_in": 0.224,
        "holes_px": [[10, 20], [30, 40]],
        "aim_px": [20, 30],
        "hand_es_in": None,
    }


def test_the_documented_minimal_format_parses_into_one_group_of_all_holes() -> None:
    truth = parse_truth(minimal_doc())
    assert truth.reference_kind is ReferenceKind.CIRCLE_1IN
    assert truth.holes_px.shape == (2, 2) and truth.aim_px == (20.0, 30.0)
    assert truth.hand_es_in is None and truth.reference_h_px_to_plane is None
    (group,) = truth.groups
    assert group.hole_indices == (0, 1) and group.shots_expected == 2 and group.roi_px is None


def test_aim_and_hand_es_are_optional_and_default_to_null() -> None:
    doc = minimal_doc()
    del doc["aim_px"], doc["hand_es_in"]
    truth = parse_truth(doc)
    assert truth.aim_px is None and truth.hand_es_in is None and truth.groups[0].aim_px is None


def circle(center: tuple[float, float], score: float) -> ReferenceProposal:
    candidate = CircleCandidate(
        center_px=center, axes_px=(100.0, 100.0), angle_deg=0.0, score=score
    )
    result = ReferenceResult(
        ReferenceKind.CIRCLE_1IN, "auto", tuple(tuple(r) for r in np.eye(3)), 1.0, ()
    )
    return ReferenceProposal(result=result, score=score, circle=candidate)


def test_the_confirm_step_picks_the_proposal_nearest_the_annotated_circle() -> None:
    proposals = [
        circle((900.0, 100.0), 0.9),
        circle((300.0, 500.0), 0.8),
        circle((311.0, 489.5), 0.7),
    ]
    truth = parse_truth(
        {
            **minimal_doc(),
            "reference": {"h_px_to_plane": [[0.01, 0, -3.1], [0, 0.01, -4.9], [0, 0, 1]]},
        }
    )
    picked, rank = tool._pick_proposal(proposals, truth)
    assert rank == 3 and picked is proposals[2]


def test_without_an_annotated_reference_the_top_proposal_is_used() -> None:
    proposals = [circle((900.0, 100.0), 0.9), circle((300.0, 500.0), 0.8)]
    picked, rank = tool._pick_proposal(proposals, parse_truth(minimal_doc()))
    assert rank == 1 and picked is proposals[0]


def test_the_full_format_round_trips_through_the_exporter() -> None:
    doc = export_scenario_doc()
    truth = parse_truth(json.loads(json.dumps(doc)))
    assert len(truth.groups) == 3
    assert truth.reference_h_px_to_plane is not None and truth.reference_method == "manual"
    assert truth.groups[0].roi_px is not None and truth.groups[0].aim_px is not None
    assert truth.holes_px.shape == (len(doc["holes_px"]), 2)


def export_scenario_doc() -> dict:
    img, truth = render_named("multi_dot_target", 0)
    kind, reference = confirmed_reference(truth)
    return truth_document(
        reference_kind=kind,
        distance_yd=100,
        bullet_diameter_in=truth["bullet_diameter_in"],
        holes_px=plane_to_image(truth, np.array(truth["holes_in"])),
        groups=[
            {
                "name": f"g{i}",
                "roi_px": g["roi_px"],
                "aim_px": g["aim_px"],
                "shots_expected": len(g["hole_indices"]),
                "hole_indices": g["hole_indices"],
                "hand_es_in": None,
            }
            for i, g in enumerate(truth["groups"])
        ],
        reference=reference,
        aim_px=truth["aim_px"],
    )


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda d: d.update(reference_kind="circle_4in"), "reference_kind"),
        (lambda d: d.pop("reference_kind"), "reference_kind"),
        (lambda d: d.update(distance_yd=0), "distance_yd"),
        (lambda d: d.update(distance_yd="100"), "distance_yd"),
        (lambda d: d.update(bullet_diameter_in=2.0), "bullet_diameter_in"),
        (lambda d: d.update(holes_px=[]), "holes_px"),
        (lambda d: d.update(holes_px=[[1, 2, 3]]), "holes_px[0]"),
        (lambda d: d.update(holes_px=[[1, float("nan")]]), "holes_px[0]"),
        (lambda d: d.update(aim_px=[1]), "aim_px"),
        (lambda d: d.update(hand_es_in=-1), "hand_es_in"),
        (lambda d: d.update(format_version=2), "format_version"),
        (
            lambda d: d.update(reference={"h_px_to_plane": [[1, 0], [0, 1]]}),
            "reference.h_px_to_plane",
        ),
        (lambda d: d.update(reference={"h_px_to_plane": [[0] * 3] * 3}), "reference.h_px_to_plane"),
        (
            lambda d: d.update(reference={"method": "x", "h_px_to_plane": np.eye(3).tolist()}),
            "reference.method",
        ),
        (lambda d: d.update(groups=[]), "groups"),
        (lambda d: d.update(groups=[{"hole_indices": [0, 5]}]), "groups[0].hole_indices"),
        (lambda d: d.update(groups=[{"hole_indices": [0, 0]}]), "groups[0].hole_indices"),
        (
            lambda d: d.update(groups=[{"hole_indices": [0], "roi_px": [5, 5, 4, 9]}]),
            "groups[0].roi_px",
        ),
        (
            lambda d: d.update(groups=[{"hole_indices": [0], "shots_expected": 0}]),
            "groups[0].shots_expected",
        ),
        (lambda d: d.update(groups=[{"hole_indices": [0]}, {"hole_indices": [0, 1]}]), "groups"),
    ],
)
def test_invalid_truth_documents_name_the_offending_field(mutate, field: str) -> None:
    doc = minimal_doc()
    mutate(doc)
    with pytest.raises(TruthError, match=field.replace("[", r"\[").replace("]", r"\]")):
        parse_truth(doc, "photo.truth.json")


def test_a_non_object_or_unreadable_truth_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(TruthError, match="JSON object"):
        parse_truth([1, 2], "x")
    bad = tmp_path / f"a{tool.TRUTH_SUFFIX}"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(TruthError, match="cannot read JSON"):
        tool.load_truth(bad)


def test_an_invalid_truth_file_stops_the_run_with_exit_two_and_names_the_file(
    tmp_path: Path, capsys
) -> None:
    export_scenario("clean_grid", 0, tmp_path)
    (tmp_path / f"clean_grid_s0{tool.TRUTH_SUFFIX}").write_text(
        '{"reference_kind": "no"}', encoding="utf-8"
    )
    assert run(tmp_path) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "clean_grid_s0.truth.json" in err and "reference_kind" in err


def test_the_readme_examples_parse_with_the_documented_meaning() -> None:
    readme = (Path(__file__).resolve().parents[1] / "real" / "README.md").read_text()
    blocks = re.findall(r"```json\n(.*?)```", readme, re.S)
    assert len(blocks) == 2
    minimal, full = (parse_truth(json.loads(b), "README") for b in blocks)
    assert minimal.reference_kind is ReferenceKind.GRID_1IN and minimal.hand_es_in == 1.12
    assert len(minimal.groups) == 1 and minimal.groups[0].shots_expected == 3
    assert full.reference_kind is ReferenceKind.CIRCLE_1IN and len(full.groups) == 2
    assert full.groups[1].hand_es_in == 0.55 and full.groups[0].roi_px == (520, 600, 760, 820)
    assert full.reference_h_px_to_plane is not None and full.reference_method == "manual"


def test_the_readme_lists_every_exit_code_and_threshold_the_tool_uses() -> None:
    readme = (Path(__file__).resolve().parents[1] / "real" / "README.md").read_text()
    for code in (EXIT_PASS, EXIT_FAIL, EXIT_USAGE, EXIT_OUTSTANDING):
        assert f"`{code}`" in readme
    for word in ("PASS", "FAIL", "INCOMPLETE", "manual_reference_needed", "ragged_credited"):
        assert word in readme, word
    t = tool.REAL_P11
    for needle in (
        f"{t.scale_error_pct} %",
        f"{t.center_p95_in} in",
        "90 %",
        str(t.false_positives_per_photo),
        f"{t.es_error_in} in",
        f"{t.runtime_s:.0f} s",
    ):
        assert needle in readme, needle


def test_coarse_mode_compares_unannotated_photos_with_the_visual_counts(
    tmp_path: Path, capsys
) -> None:
    export_scenario("clean_grid", 0, tmp_path)
    img, _ = render_named("clean_circle1", 0)
    cv2.imwrite(str(tmp_path / "real_01.jpg"), img)
    assert run(tmp_path, "--coarse") == EXIT_PASS
    out = capsys.readouterr().out
    assert "coarse mode" in out and "real_01" in out


# ----------------------------------------------------------------------------------- exporter recipe


def test_working_plane_shots_convert_back_to_oriented_px_for_export() -> None:
    img, truth = render_named("skew10_grid", 0)
    kind, reference = confirmed_reference(truth)
    working, frame, h = build_working_image(img, reference)
    holes_px = plane_to_image(truth, np.array(truth["holes_in"]))
    shots_in = apply_h(h, holes_px) / frame.ppi
    back = plane_in_to_oriented_px(shots_in, frame.ppi, h)
    assert np.abs(back - holes_px).max() < 1e-6


# ----------------------------------------------------------------------------------- real photos


def test_real_photos_when_annotated(tmp_path: Path) -> None:
    if not REAL_PHOTOS.is_dir() or not any(REAL_PHOTOS.glob(f"*{tool.TRUTH_SUFFIX}")):
        pytest.skip(
            "real-photo gate outstanding: no annotated photos in data/real_photos (gitignored)"
        )
    out = tmp_path / "real.json"
    rc = run(REAL_PHOTOS, "--json", str(out))
    report = json.loads(out.read_text())
    assert rc == (EXIT_PASS if report["verdict"] == "PASS" else EXIT_FAIL)
    if not report["provisional"]:
        assert report["verdict"] == "PASS", report["failures"] + report["incomplete"]
