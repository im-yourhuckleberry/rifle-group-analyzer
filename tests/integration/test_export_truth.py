"""T3.10: the truth-file exporter, driven by real stored photos and scored by the real-photo tool."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pytest

import tools.eval_real_photos as tool
from rga import export_truth
from rga.config import Settings
from rga.db import repo_groups
from rga.db.connection import connect, latest_version
from rga.domain.types import PlanePoint, ReferenceKind, Shot, ShotOrigin
from rga.services import group_records
from rga.services import groups as group_service
from rga.services import photos as photo_service
from rga.vision.pipeline import roi_to_working
from rga.vision.transform import apply_h, build_working
from rga.web.deps import init_database
from tests.support.seed import seed_load, seed_rifle
from tests.synth.generate import load_catalog, plane_to_image, render_named

CATALOG = load_catalog()
PROJECT_ROOT = Path(__file__).resolve().parents[2]
KIND_BY_DIAMETER = {
    1.0: ReferenceKind.CIRCLE_1IN,
    1.5: ReferenceKind.CIRCLE_1_5IN,
    3.0: ReferenceKind.CIRCLE_3IN,
}
EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    config = Settings(data_dir=tmp_path / "data")
    init_database(config)
    return config


@pytest.fixture
def conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    connection = connect(settings.db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def env(settings: Settings, monkeypatch) -> Settings:
    monkeypatch.setenv("RGA_DATA_DIR", str(settings.data_dir))
    return settings


def _display_order(corners_px: np.ndarray) -> list[int]:
    by_y = np.argsort(corners_px[:, 1])
    top = sorted(by_y[:2], key=lambda i: corners_px[i, 0])
    bottom = sorted(by_y[2:], key=lambda i: -corners_px[i, 0])
    return [top[0], top[1], bottom[0], bottom[1]]


def manual_marks(truth: dict) -> tuple[ReferenceKind, dict]:
    """The marks a user would click on the printed reference of a synthetic photo."""
    ref = truth["reference"]
    if ref["kind"] == "grid":
        corners = np.array(ref["corners_px"], dtype=np.float64)
        ordered = corners[_display_order(corners)]
        marks = {
            "grid_corners_px": [tuple(float(v) for v in c) for c in ordered],
            "squares": (ref["squares_across"], ref["squares_down"]),
        }
        return ReferenceKind.GRID_1IN, marks
    cx, cy = ref["center_in"]
    r = ref["diameter_in"] / 2
    angles = np.linspace(0, 2 * math.pi, 12, endpoint=False)
    plane = np.column_stack([cx + r * np.cos(angles), cy + r * np.sin(angles)])
    edge = plane_to_image(truth, plane)
    return KIND_BY_DIAMETER[ref["diameter_in"]], {"edge_points_px": [tuple(p) for p in edge]}


@dataclass
class Stored:
    photo_id: int
    truth: dict
    group_ids: list[int]
    rifle_id: int
    load_id: int
    h_working: np.ndarray
    ppi: float
    image: np.ndarray
    data: bytes


def store_photo(conn, settings, name: str, seed: int = 0, *, finalize: bool = True) -> Stored:
    """Upload a rendered scenario, confirm its reference and finalize one group per truth group."""
    image, truth = render_named(name, seed)
    data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()
    stored = photo_service.store_upload(conn, settings, data)
    kind, marks = manual_marks(truth)
    confirmed = photo_service.confirm_reference(
        conn, settings, stored.photo.id, kind, manual=marks
    ).photo
    oriented = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    h_plane = np.array(json.loads(confirmed.h_px_to_plane_json))
    _, frame, h_working = build_working(oriented, h_plane)
    rifle = seed_rifle(conn, bullet_diameter_in=truth["bullet_diameter_in"])
    load = seed_load(conn, rifle)
    holes_px = plane_to_image(truth, np.array(truth["holes_in"]))
    holes_working = apply_h(h_working, holes_px) / frame.ppi
    group_ids = []
    for index, g in enumerate(truth["groups"]):
        view = group_records.create_group(
            conn,
            load_id=load.id,
            lot_code="LOT-1",
            shots_expected=len(g["hole_indices"]),
            distance_yd=100,
            position="bench",
            suppressor=False,
            shot_date="2026-09-30",
            photo_id=confirmed.id,
        )
        x0, y0, x1, y1 = g["roi_px"]
        roi = roi_to_working(h_working, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], frame)
        with conn:
            conn.execute(
                "UPDATE groups SET roi_json = ? WHERE id = ?",
                (json.dumps(dict(zip(("x0", "y0", "x1", "y1"), roi, strict=True))), view.group.id),
            )
        aim = apply_h(h_working, np.array([g["aim_px"]])) / frame.ppi
        shots = [
            Shot(
                PlanePoint(float(holes_working[i][0]), float(holes_working[i][1])),
                ShotOrigin.MANUAL,
            )
            for i in g["hole_indices"]
        ]
        group_service.save_review(
            conn,
            view.group.id,
            PlanePoint(float(aim[0][0]), float(aim[0][1])),
            shots,
            [],
            len(shots),
        )
        if finalize or index > 0:
            group_service.finalize(conn, view.group.id)
        group_ids.append(view.group.id)
    return Stored(
        confirmed.id,
        truth,
        group_ids,
        rifle.id,
        load.id,
        h_working,
        frame.ppi,
        oriented,
        data,
    )


def run_cli(*args: str) -> int:
    return export_truth.main(list(args))


def exported_files(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ------------------------------------------------------------------------------ round trips


class TestRoundTrip:
    @pytest.mark.parametrize("scenario", ["clean_grid", "skew15_grid"])
    def test_exported_truth_matches_the_rendered_holes_and_scores_pass(
        self, env, conn, tmp_path, capsys, scenario
    ):
        stored = store_photo(conn, env, scenario)
        out = tmp_path / "real_photos"

        assert run_cli("--photo-id", str(stored.photo_id), "--out", str(out)) == EXIT_OK

        stem = export_truth.export_stem(repo_groups.get_photo(conn, stored.photo_id))
        assert exported_files(out) == [f"{stem}.jpg", f"{stem}.truth.json"]
        doc = json.loads((out / f"{stem}.truth.json").read_text())
        expected = plane_to_image(stored.truth, np.array(stored.truth["holes_in"]))
        assert np.array(doc["holes_px"]) == pytest.approx(expected, abs=1e-3)
        assert doc["format_version"] == 1 and doc["hand_es_in"] is None
        assert doc["reference_kind"] == "grid_1in"
        assert doc["distance_yd"] == 100.0
        assert doc["bullet_diameter_in"] == stored.truth["bullet_diameter_in"]
        assert doc["reference"]["method"] == "manual"
        assert len(doc["groups"]) == 1
        group = doc["groups"][0]
        assert group["hole_indices"] == list(range(10)) and group["shots_expected"] == 10
        assert group["hand_es_in"] is None and group["name"] == "group1"
        truth_group = stored.truth["groups"][0]
        assert group["aim_px"] == pytest.approx(truth_group["aim_px"], abs=1e-3)
        assert doc["aim_px"] == pytest.approx(truth_group["aim_px"], abs=1e-3)
        tx0, ty0, tx1, ty1 = truth_group["roi_px"]
        x0, y0, x1, y1 = group["roi_px"]
        assert x0 <= tx0 + 2 and y0 <= ty0 + 2 and x1 >= tx1 - 2 and y1 >= ty1 - 2
        grow = 0.1 * max(tx1 - tx0, ty1 - ty0)  # the boxed ROI of a rotated sheet is a bit larger
        assert (tx0 - x0) < grow and (ty0 - y0) < grow and (x1 - tx1) < grow and (y1 - ty1) < grow
        parsed = tool.parse_truth(doc, "exported")
        assert len(parsed.groups) == 1 and len(parsed.holes_px) == 10
        capsys.readouterr()

        report = tmp_path / "report.json"
        assert tool.main(["--dir", str(out), "--json", str(report)]) == 0
        result = json.loads(report.read_text())
        assert result["verdict"] == "PASS" and result["failures"] == []
        assert result["summary"]["recall"] == 1.0 and result["summary"]["holes"] == 10

    def test_a_three_group_sheet_exports_every_group_and_scores_pass(self, env, conn, tmp_path):
        stored = store_photo(conn, env, "multi_dot_target")
        out = tmp_path / "out"

        assert run_cli("--photo-id", str(stored.photo_id), "--out", str(out)) == EXIT_OK

        doc = json.loads(next(out.glob("*.truth.json")).read_text())
        assert [g["name"] for g in doc["groups"]] == ["group1", "group2", "group3"]
        indices = [i for g in doc["groups"] for i in g["hole_indices"]]
        assert indices == list(range(15)) and len(doc["holes_px"]) == 15
        assert doc["aim_px"] is None
        assert all(g["roi_px"] and g["aim_px"] for g in doc["groups"])
        assert doc["reference_kind"] == "circle_1in"
        assert tool.main(["--dir", str(out), "--scale-pct", "1.0"]) == 0

    def test_the_photo_is_copied_byte_for_byte(self, env, conn, tmp_path):
        stored = store_photo(conn, env, "clean_grid")
        out = tmp_path / "out"

        run_cli("--photo-id", str(stored.photo_id), "--out", str(out))

        copy = next(out.glob("*.jpg"))
        assert copy.read_bytes() == stored.data

    @pytest.mark.parametrize("scenario", ["clean_grid", "skew15_grid", "multi_dot_target"])
    def test_the_working_transform_matches_the_real_working_image_builder(
        self, env, conn, scenario
    ):
        stored = store_photo(conn, env, scenario)
        photo = repo_groups.get_photo(conn, stored.photo_id)

        h_working, size = export_truth.h_px_to_working(
            np.array(json.loads(photo.h_px_to_plane_json)),
            photo.width_px,
            photo.height_px,
            photo.working_ppi,
        )

        assert h_working == pytest.approx(stored.h_working, rel=1e-9, abs=1e-9)
        assert size == (photo.working_w, photo.working_h)

    def test_the_working_size_is_capped_at_4000_px(self):
        h = np.array([[0.01, 0, 0], [0, 0.01, 0], [0, 0, 1.0]])

        _, size = export_truth.h_px_to_working(h, 100_000, 10, 200.0)

        assert size == (4000, 20)


# ---------------------------------------------------------------------------- overwrite rules


class TestOverwrite:
    def test_a_second_export_is_refused_and_changes_nothing(self, env, conn, tmp_path, capsys):
        stored = store_photo(conn, env, "clean_grid")
        out = tmp_path / "out"
        run_cli("--photo-id", str(stored.photo_id), "--out", str(out))
        before = {p.name: p.read_bytes() for p in out.iterdir()}
        capsys.readouterr()

        code = run_cli("--photo-id", str(stored.photo_id), "--out", str(out))

        assert code == EXIT_REFUSED
        assert "--force" in capsys.readouterr().err
        assert {p.name: p.read_bytes() for p in out.iterdir()} == before

    @pytest.mark.parametrize("suffix", [".jpg", ".truth.json"])
    def test_either_existing_file_blocks_the_export(self, env, conn, tmp_path, suffix):
        stored = store_photo(conn, env, "clean_grid")
        out = tmp_path / "out"
        out.mkdir()
        stem = export_truth.export_stem(repo_groups.get_photo(conn, stored.photo_id))
        (out / f"{stem}{suffix}").write_text("precious")

        code = run_cli("--photo-id", str(stored.photo_id), "--out", str(out))

        assert code == EXIT_REFUSED
        assert exported_files(out) == [f"{stem}{suffix}"]
        assert (out / f"{stem}{suffix}").read_text() == "precious"

    def test_force_replaces_both_files(self, env, conn, tmp_path):
        stored = store_photo(conn, env, "clean_grid")
        out = tmp_path / "out"
        run_cli("--photo-id", str(stored.photo_id), "--out", str(out))
        truth_file = next(out.glob("*.truth.json"))
        photo_file = next(out.glob("*.jpg"))
        good = truth_file.read_text()
        truth_file.write_text("{}")
        photo_file.write_bytes(b"corrupt")

        code = run_cli("--photo-id", str(stored.photo_id), "--out", str(out), "--force")

        assert code == EXIT_OK
        assert truth_file.read_text() == good
        assert photo_file.read_bytes() == stored.data
        assert mode(truth_file) == 0o600 and mode(photo_file) == 0o600

    def test_no_staging_files_are_left_behind(self, env, conn, tmp_path):
        stored = store_photo(conn, env, "clean_grid")
        out = tmp_path / "out"

        run_cli("--photo-id", str(stored.photo_id), "--out", str(out))
        run_cli("--photo-id", str(stored.photo_id), "--out", str(out))
        run_cli("--photo-id", str(stored.photo_id), "--out", str(out), "--force")

        assert len(exported_files(out)) == 2
        assert not [name for name in exported_files(out) if ".part-" in name]


# ----------------------------------------------------------------------------- refusal paths


class TestContent:
    def test_the_stem_is_photo_id_and_the_sha_prefix(self, env, conn):
        stored = store_photo(conn, env, "clean_grid")
        photo = repo_groups.get_photo(conn, stored.photo_id)

        assert export_truth.export_stem(photo) == f"photo_{photo.id}_{photo.sha256[:8]}"
        assert re.fullmatch(r"photo_1_[0-9a-f]{8}", export_truth.export_stem(photo))

    def test_the_group_distance_is_exported(self, env, conn, tmp_path):
        stored = store_photo(conn, env, "clean_grid")
        with conn:
            conn.execute("UPDATE groups SET distance_yd = 300")

        run_cli("--photo-id", str(stored.photo_id), "--out", str(tmp_path / "out"))

        doc = json.loads(next((tmp_path / "out").glob("*.truth.json")).read_text())
        assert doc["distance_yd"] == 300.0

    def test_only_the_groups_of_the_requested_photo_are_exported(self, env, conn, tmp_path):
        first = store_photo(conn, env, "clean_grid")
        second = store_photo(conn, env, "multi_dot_target")

        run_cli("--photo-id", str(first.photo_id), "--out", str(tmp_path / "one"))
        run_cli("--photo-id", str(second.photo_id), "--out", str(tmp_path / "two"))

        one = json.loads(next((tmp_path / "one").glob("*.truth.json")).read_text())
        two = json.loads(next((tmp_path / "two").glob("*.truth.json")).read_text())
        assert (len(one["groups"]), len(one["holes_px"])) == (1, 10)
        assert (len(two["groups"]), len(two["holes_px"])) == (3, 15)

    def test_publish_refuses_to_replace_a_file_that_appeared_after_the_pre_check(self, tmp_path):
        final = tmp_path / "x.truth.json"
        final.write_text("someone else's")
        staged = export_truth._stage(tmp_path, final.name, b"mine")

        with pytest.raises(export_truth.ExportRefused):
            export_truth._publish(staged, final, force=False)

        assert final.read_text() == "someone else's"
        assert not staged.exists()

    def test_publish_with_force_replaces_the_file(self, tmp_path):
        final = tmp_path / "x.truth.json"
        final.write_text("old")
        staged = export_truth._stage(tmp_path, final.name, b"mine")

        export_truth._publish(staged, final, force=True)

        assert final.read_text() == "mine" and not staged.exists() and mode(final) == 0o600


class TestRefusals:
    def test_a_draft_group_blocks_the_export_with_a_clear_message(
        self, env, conn, tmp_path, capsys
    ):
        stored = store_photo(conn, env, "multi_dot_target", finalize=False)
        out = tmp_path / "never_created"
        drafts = [g.id for g in repo_groups.list_groups(conn) if g.status != "final"]
        assert drafts == [stored.group_ids[0]]

        code = run_cli("--photo-id", str(stored.photo_id), "--out", str(out))

        message = capsys.readouterr().err
        assert code == EXIT_REFUSED
        assert "not finalized" in message and f"#{drafts[0]} (draft)" in message
        assert not out.exists()

    def test_every_unfinalized_group_is_named_in_the_message(self, env, conn, tmp_path, capsys):
        stored = store_photo(conn, env, "multi_dot_target", finalize=False)
        with conn:
            conn.execute(
                "UPDATE groups SET status = 'draft', finalized_at = NULL WHERE id = ?",
                (stored.group_ids[2],),
            )

        code = run_cli("--photo-id", str(stored.photo_id), "--out", str(tmp_path / "out"))

        message = capsys.readouterr().err
        assert code == EXIT_REFUSED
        assert f"#{stored.group_ids[0]} (draft)" in message
        assert f"#{stored.group_ids[2]} (draft)" in message
        assert f"#{stored.group_ids[1]}" not in message

    def test_a_photo_whose_reference_is_not_confirmed_is_refused_even_with_a_transform(
        self, env, conn, tmp_path, capsys
    ):
        stored = store_photo(conn, env, "clean_grid")
        with conn:
            conn.execute(
                "UPDATE photos SET reference_confirmed = 0 WHERE id = ?", (stored.photo_id,)
            )

        code = run_cli("--photo-id", str(stored.photo_id), "--out", str(tmp_path / "out"))

        assert code == EXIT_REFUSED
        assert "no confirmed reference" in capsys.readouterr().err

    def test_finalizing_the_last_group_makes_the_export_succeed(self, env, conn, tmp_path):
        stored = store_photo(conn, env, "multi_dot_target", finalize=False)
        out = tmp_path / "out"
        assert run_cli("--photo-id", str(stored.photo_id), "--out", str(out)) == EXIT_REFUSED

        group_service.finalize(conn, stored.group_ids[0])

        assert run_cli("--photo-id", str(stored.photo_id), "--out", str(out)) == EXIT_OK

    def test_an_unknown_photo_id_is_exit_2(self, env, conn, tmp_path, capsys):
        code = run_cli("--photo-id", "9999", "--out", str(tmp_path / "out"))

        assert code == EXIT_USAGE
        assert "photo 9999 does not exist" in capsys.readouterr().err
        assert not (tmp_path / "out").exists()

    def test_a_photo_without_a_confirmed_reference_is_refused(self, env, conn, tmp_path, capsys):
        photo = repo_groups.create_photo(
            conn, sha256="a" * 64, ext="jpg", width_px=100, height_px=100
        )
        conn.commit()

        code = run_cli("--photo-id", str(photo.id), "--out", str(tmp_path / "out"))

        assert code == EXIT_REFUSED
        assert "no confirmed reference" in capsys.readouterr().err

    def test_a_photo_no_group_uses_is_refused(self, env, conn, tmp_path, capsys):
        stored = store_photo(conn, env, "clean_grid")
        with conn:
            conn.execute("DELETE FROM groups WHERE photo_id = ?", (stored.photo_id,))

        code = run_cli("--photo-id", str(stored.photo_id), "--out", str(tmp_path / "out"))

        assert code == EXIT_REFUSED
        assert "nothing to export" in capsys.readouterr().err

    def test_groups_with_different_distances_cannot_share_one_truth_file(
        self, env, conn, tmp_path, capsys
    ):
        stored = store_photo(conn, env, "clean_grid")
        with conn:
            conn.execute("UPDATE groups SET distance_yd = 300")
        other = group_records.create_group(
            conn,
            load_id=stored.load_id,
            lot_code="LOT-1",
            shots_expected=3,
            distance_yd=100,
            position="bench",
            suppressor=False,
            shot_date="2026-09-30",
            photo_id=stored.photo_id,
        )
        group = other.group
        with conn:
            conn.execute(
                "UPDATE groups SET status = 'final', aim_x_in = 1, aim_y_in = 1, "
                "finalized_at = '2026-10-02T00:00:00.000000Z' WHERE id = ?",
                (group.id,),
            )

        code = run_cli("--photo-id", str(stored.photo_id), "--out", str(tmp_path / "out"))

        assert code == EXIT_USAGE
        assert "different distances" in capsys.readouterr().err

    def test_a_missing_original_is_exit_2(self, env, conn, tmp_path, capsys):
        stored = store_photo(conn, env, "clean_grid")
        photo = repo_groups.get_photo(conn, stored.photo_id)
        (env.photos_dir / f"{photo.sha256}.{photo.ext}").unlink()

        code = run_cli("--photo-id", str(stored.photo_id), "--out", str(tmp_path / "out"))

        assert code == EXIT_USAGE and "original" in capsys.readouterr().err
        assert not (tmp_path / "out").exists()

    def test_a_stored_working_size_that_disagrees_with_the_transform_is_exit_2(
        self, env, conn, tmp_path, capsys
    ):
        stored = store_photo(conn, env, "clean_grid")
        with conn:
            conn.execute(
                "UPDATE photos SET working_w = working_w + 7 WHERE id = ?", (stored.photo_id,)
            )

        code = run_cli("--photo-id", str(stored.photo_id), "--out", str(tmp_path / "out"))

        assert code == EXIT_USAGE and "re-confirm the reference" in capsys.readouterr().err

    def test_a_missing_database_is_exit_2_and_creates_none(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("RGA_DATA_DIR", str(tmp_path / "empty"))

        code = run_cli("--photo-id", "1")

        assert code == EXIT_USAGE and "no database" in capsys.readouterr().err
        assert not (tmp_path / "empty").exists()

    def test_a_bad_configuration_is_exit_2(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("RGA_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("RGA_HOST", "0.0.0.0")

        assert run_cli("--photo-id", "1") == EXIT_USAGE
        assert "RGA_HOST" in capsys.readouterr().err

    def test_arguments_are_validated(self, capsys):
        with pytest.raises(SystemExit) as missing:
            run_cli()
        with pytest.raises(SystemExit) as text:
            run_cli("--photo-id", "abc")

        assert missing.value.code == 2 and text.value.code == 2


# ------------------------------------------------------------------------------------ privacy


class TestPrivacyAndSafety:
    def test_files_are_0600_and_created_directories_are_0700(self, env, conn, tmp_path):
        stored = store_photo(conn, env, "clean_grid")
        out = tmp_path / "private" / "nested" / "real_photos"
        old_umask = os.umask(0o000)
        try:
            run_cli("--photo-id", str(stored.photo_id), "--out", str(out))
        finally:
            os.umask(old_umask)

        for directory in (out, out.parent, out.parent.parent):
            assert mode(directory) == 0o700, directory
        assert [mode(p) for p in out.iterdir()] == [0o600, 0o600]

    def test_an_existing_output_directory_keeps_its_mode(self, env, conn, tmp_path):
        stored = store_photo(conn, env, "clean_grid")
        out = tmp_path / "shared"
        out.mkdir()
        out.chmod(0o755)

        run_cli("--photo-id", str(stored.photo_id), "--out", str(out))

        assert mode(out) == 0o755

    def test_the_default_output_folder_is_real_photos_inside_the_data_dir(self, env, conn):
        stored = store_photo(conn, env, "clean_grid")

        assert run_cli("--photo-id", str(stored.photo_id)) == EXIT_OK

        default = env.data_dir / "real_photos"
        assert mode(default) == 0o700
        assert len(exported_files(default)) == 2

    def test_the_database_is_not_modified(self, env, conn, tmp_path):
        stored = store_photo(conn, env, "multi_dot_target")
        tables = ("photos", "groups", "shots", "sessions", "rifles", "loads", "load_lots")

        def snapshot():
            return {
                t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in tables
            }

        before = snapshot()
        run_cli("--photo-id", str(stored.photo_id), "--out", str(tmp_path / "out"))

        assert snapshot() == before

    def test_a_database_below_the_latest_schema_is_refused_with_exit_2(
        self, env, conn, tmp_path, capsys
    ):
        stored = store_photo(conn, env, "clean_grid")
        latest = latest_version()
        conn.execute(f"PRAGMA user_version = {latest - 1}")
        conn.commit()

        code = run_cli("--photo-id", str(stored.photo_id), "--out", str(tmp_path / "out"))

        err = capsys.readouterr().err
        assert code == EXIT_USAGE
        assert "start the app once to upgrade the database" in err
        assert f"schema version {latest - 1}" in err
        assert not (tmp_path / "out").exists()
        conn.execute(f"PRAGMA user_version = {latest}")
        conn.commit()
        assert (
            run_cli("--photo-id", str(stored.photo_id), "--out", str(tmp_path / "out")) == EXIT_OK
        )

    def test_the_schema_check_runs_before_any_photo_is_looked_up(self, env, conn):
        conn.execute("PRAGMA user_version = 1")
        conn.commit()

        with pytest.raises(export_truth.ExportError, match="start the app once"):
            export_truth.export_photo(conn, env, 999, Path("unused"))

    def test_the_module_never_touches_the_network(self):
        source = (PROJECT_ROOT / "src" / "rga" / "export_truth.py").read_text()

        assert not re.search(r"\b(socket|urllib|http\.client|requests|httpx|aiohttp)\b", source)
        assert not re.search(r"https?://", source)


class TestReadOnlyAndIdBounds:
    def test_open_readonly_reads_but_cannot_write(self, env, conn):
        stored = store_photo(conn, env, "clean_grid")
        ro = export_truth.open_readonly(env.db_path)
        try:
            assert ro.execute("SELECT id FROM photos").fetchone()["id"] == stored.photo_id
            for statement in (
                "UPDATE photos SET ext = 'png'",
                "DELETE FROM groups",
                "INSERT INTO rifles (public_id, name, cartridge, bullet_diameter_in, created_at, "
                "updated_at) VALUES ('x', 'x', 'x', 0.2, 't', 't')",
                "PRAGMA user_version = 99",
            ):
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    ro.execute(statement)
        finally:
            ro.close()
        assert conn.execute("SELECT ext FROM photos").fetchone()["ext"] == "jpg"

    def test_open_readonly_does_not_create_a_missing_database(self, tmp_path):
        with pytest.raises(sqlite3.OperationalError):
            export_truth.open_readonly(tmp_path / "nope.sqlite3")

        assert not (tmp_path / "nope.sqlite3").exists()

    def test_the_cli_runs_on_a_read_only_connection(self, env, conn, tmp_path, monkeypatch):
        stored = store_photo(conn, env, "clean_grid")
        seen: list[str] = []
        real = export_truth.export_photo

        def probing(connection, settings, photo_id, out_dir, *, force=False):
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute("UPDATE photos SET ext = 'png'")
            seen.append("probed")
            return real(connection, settings, photo_id, out_dir, force=force)

        monkeypatch.setattr(export_truth, "export_photo", probing)

        code = run_cli("--photo-id", str(stored.photo_id), "--out", str(tmp_path / "out"))

        assert code == EXIT_OK and seen == ["probed"]

    def test_the_cli_opens_the_database_with_mode_ro(self, env, conn, tmp_path, monkeypatch):
        stored = store_photo(conn, env, "clean_grid")
        calls: list[tuple] = []
        real_connect = sqlite3.connect

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(export_truth.sqlite3, "connect", spy)

        run_cli("--photo-id", str(stored.photo_id), "--out", str(tmp_path / "out"))

        assert len(calls) == 1
        target, options = calls[0][0][0], calls[0][1]
        assert target.startswith("file:") and target.endswith("?mode=ro")
        assert options == {"uri": True}

    def test_it_works_when_no_other_connection_has_the_database_open(self, env, conn, tmp_path):
        stored = store_photo(conn, env, "clean_grid")
        conn.close()

        code = run_cli("--photo-id", str(stored.photo_id), "--out", str(tmp_path / "out"))

        assert code == EXIT_OK

    def test_a_data_directory_with_uri_special_characters_still_opens(self, tmp_path, monkeypatch):
        odd = Settings(data_dir=tmp_path / "my data #1 ?x%20")
        init_database(odd)
        monkeypatch.setenv("RGA_DATA_DIR", str(odd.data_dir))
        connection = connect(odd.db_path)
        try:
            stored = store_photo(connection, odd, "clean_grid")
        finally:
            connection.close()

        code = run_cli("--photo-id", str(stored.photo_id), "--out", str(tmp_path / "out"))

        assert code == EXIT_OK

    @pytest.mark.parametrize(
        "value",
        ["0", "-1", "-5", str(2**63), str(2**64), str(10**30), "abc", "1.5", "", "1e3"],
    )
    def test_out_of_range_or_malformed_photo_ids_exit_2_with_a_message(
        self, env, tmp_path, capsys, value
    ):
        with pytest.raises(SystemExit) as caught:
            run_cli("--photo-id", value, "--out", str(tmp_path / "out"))

        err = capsys.readouterr().err
        assert caught.value.code == 2
        assert "Traceback" not in err and "--photo-id" in err
        assert not (tmp_path / "out").exists()

    @pytest.mark.parametrize("value", ["0", str(2**63), str(2**64)])
    def test_the_range_message_names_the_limits(self, capsys, value):
        with pytest.raises(SystemExit):
            run_cli("--photo-id", value)

        assert "between 1 and 9223372036854775807" in capsys.readouterr().err

    def test_the_largest_valid_id_is_accepted_and_reported_as_unknown(
        self, env, conn, tmp_path, capsys
    ):
        code = run_cli("--photo-id", str(2**63 - 1), "--out", str(tmp_path / "out"))

        assert code == EXIT_USAGE
        assert f"photo {2**63 - 1} does not exist" in capsys.readouterr().err

    def test_ids_beyond_sqlite_never_reach_the_database_in_a_subprocess(self, env, conn, tmp_path):
        environment = {**os.environ, "RGA_DATA_DIR": str(env.data_dir)}

        done = subprocess.run(
            [sys.executable, "-m", "rga.export_truth", "--photo-id", str(2**64)],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        assert done.returncode == 2
        assert "Traceback" not in done.stderr and "OverflowError" not in done.stderr


def test_python_dash_m_runs_the_exporter_end_to_end(env, conn, tmp_path):
    stored = store_photo(conn, env, "clean_grid")
    out = tmp_path / "cli_out"
    environment = {**os.environ, "RGA_DATA_DIR": str(env.data_dir), "PYTHONDONTWRITEBYTECODE": "1"}

    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "rga.export_truth",
            "--photo-id",
            str(stored.photo_id),
            "--out",
            str(out),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    again = subprocess.run(
        [
            sys.executable,
            "-m",
            "rga.export_truth",
            "--photo-id",
            str(stored.photo_id),
            "--out",
            str(out),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert "exported 1 group(s), 10 hole(s)" in done.stdout
    assert again.returncode == 1 and "--force" in again.stderr
    assert len(exported_files(out)) == 2
