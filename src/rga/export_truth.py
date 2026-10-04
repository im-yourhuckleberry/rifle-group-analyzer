"""`python -m rga.export_truth --photo-id N [--out DIR] [--force]`: export a photo for the real-photo gate.

From a photo whose groups are all finalized in the app, copies the stored original to
`<out>/<stem>.<ext>` and writes `<stem>.truth.json` in the format documented in
`tests/real/README.md` (oriented-image px; the confirmed reference; per group the region, aim,
expected count and hole indices; `hand_es_in` null). `<stem>` is `photo_<id>_<sha256[:8]>`. The
default `--out` is `<RGA_DATA_DIR>/real_photos`. Nothing is overwritten without `--force`, output
files are mode 0600 and directories this tool creates are 0700. Opens the database read-only
(`mode=ro`); no network.

Exit codes: 0 exported; 1 refused (photo not exportable, or the output exists); 2 bad arguments,
configuration, unknown photo, missing data, or a database that is not at the latest schema version
(start the app once to upgrade it).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from rga.config import ConfigError, Settings, load_settings
from rga.db import repo_groups, repo_rifles
from rga.db.connection import latest_version, schema_version
from rga.db.rows import GroupRow, PhotoRow
from rga.vision.transform import apply_h

EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2
FORMAT_VERSION = 1
TRUTH_SUFFIX = ".truth.json"
MAX_WORKING_SIDE = 4000
MAX_PHOTO_ID = 2**63 - 1
FILE_MODE = 0o600
DIR_MODE = 0o700


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the database read-only (`mode=ro`); any write on the connection raises
    `sqlite3.OperationalError`. Rows are `sqlite3.Row`."""
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def require_current_schema(conn: sqlite3.Connection) -> None:
    """Raise `ExportError` unless the database is at the latest schema version."""
    found, latest = schema_version(conn), latest_version()
    if found < latest:
        raise ExportError(
            f"the database is at schema version {found}, below the latest {latest}; "
            "start the app once to upgrade the database"
        )


def photo_id_arg(text: str) -> int:
    """argparse type for `--photo-id`: an integer from 1 to 2**63 - 1 (SQLite's integer range)."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if not 1 <= value <= MAX_PHOTO_ID:
        raise argparse.ArgumentTypeError(f"photo id must be between 1 and {MAX_PHOTO_ID}")
    return value


class ExportRefused(Exception):
    """The photo cannot be exported as it stands (non-final groups, no reference, output exists)."""


class ExportError(Exception):
    """Bad input or inconsistent stored data (unknown photo, missing original, frame mismatch)."""


@dataclass(frozen=True)
class Exported:
    """What was written: the two paths and how many groups and holes the truth file holds."""

    photo_path: Path
    truth_path: Path
    groups: int
    holes: int


def export_stem(photo: PhotoRow) -> str:
    """File stem of an export: `photo_<id>_<first 8 hex of the sha256>`."""
    return f"photo_{photo.id}_{photo.sha256[:8]}"


def h_px_to_working(
    h_px_to_plane: np.ndarray, width_px: int, height_px: int, ppi: float
) -> tuple[np.ndarray, tuple[int, int]]:
    """The working-image homography for a stored reference, and the working size it implies.

    Mirrors how the working image is built: the plane bounding box of the oriented image is
    moved to the origin and scaled by `ppi`; the size is its ceiling, capped at 4000 px.
    """
    corners = np.array(
        [[0, 0], [width_px, 0], [width_px, height_px], [0, height_px]], dtype=np.float64
    )
    plane = apply_h(h_px_to_plane, corners)
    low, span = plane.min(axis=0), plane.max(axis=0) - plane.min(axis=0)
    to_working = np.array([[ppi, 0.0, -ppi * low[0]], [0.0, ppi, -ppi * low[1]], [0.0, 0.0, 1.0]])
    size = tuple(min(MAX_WORKING_SIDE, max(1, math.ceil(float(s) * ppi - 1e-9))) for s in span)
    return to_working @ h_px_to_plane, (size[0], size[1])


def _roi_working_px(roi_json: str | None) -> tuple[float, float, float, float] | None:
    if not roi_json:
        return None
    try:
        data = json.loads(roi_json)
        roi = (float(data["x0"]), float(data["y0"]), float(data["x1"]), float(data["y1"]))
    except ValueError, KeyError, TypeError:
        return None
    return roi if roi[2] > roi[0] and roi[3] > roi[1] else None


def _to_oriented(points_in, ppi: float, h_working: np.ndarray) -> np.ndarray:
    pts = np.asarray(points_in, dtype=np.float64).reshape(-1, 2) * ppi
    return apply_h(np.linalg.inv(h_working), pts)


def _group_entry(
    index: int,
    group: GroupRow,
    first_hole: int,
    shots_in: list[tuple[float, float]],
    ppi: float,
    h_working: np.ndarray,
) -> dict:
    roi = _roi_working_px(group.roi_json)
    roi_px = None
    if roi is not None:
        corners = [[roi[0], roi[1]], [roi[2], roi[1]], [roi[2], roi[3]], [roi[0], roi[3]]]
        oriented = apply_h(np.linalg.inv(h_working), np.array(corners, dtype=np.float64))
        low, high = oriented.min(axis=0), oriented.max(axis=0)
        roi_px = [float(low[0]), float(low[1]), float(high[0]), float(high[1])]
    aim_px = None
    if group.aim_x_in is not None and group.aim_y_in is not None:
        aim = _to_oriented([[group.aim_x_in, group.aim_y_in]], ppi, h_working)[0]
        aim_px = [float(aim[0]), float(aim[1])]
    return {
        "name": f"group{index}",
        "roi_px": roi_px,
        "aim_px": aim_px,
        "shots_expected": group.shots_expected,
        "hole_indices": list(range(first_hole, first_hole + len(shots_in))),
        "hand_es_in": None,
    }


def _refusals(photo: PhotoRow, groups: list[GroupRow]) -> None:
    if (
        not photo.reference_confirmed
        or photo.h_px_to_plane_json is None
        or photo.working_ppi is None
    ):
        raise ExportRefused(f"photo {photo.id} has no confirmed reference")
    if not groups:
        raise ExportRefused(f"no group uses photo {photo.id}; there is nothing to export")
    pending = [g for g in groups if g.status != "final"]
    if pending:
        listing = ", ".join(f"#{g.id} ({g.status})" for g in pending)
        raise ExportRefused(
            f"photo {photo.id} has groups that are not finalized: {listing}. "
            "Finalize them in the review editor first."
        )


def build_truth(conn: sqlite3.Connection, photo: PhotoRow, groups: list[GroupRow]) -> dict:
    """The truth document for a photo with confirmed reference and only finalized groups.

    Raises `ExportRefused` for an unexportable photo and `ExportError` when its groups disagree
    on distance or bullet diameter, or the stored working frame does not match the transform.
    """
    _refusals(photo, groups)
    h_plane = np.array(json.loads(photo.h_px_to_plane_json), dtype=np.float64)
    ppi = float(photo.working_ppi)
    h_working, size = h_px_to_working(h_plane, photo.width_px, photo.height_px, ppi)
    if (photo.working_w, photo.working_h) != size:
        raise ExportError(
            f"photo {photo.id}: stored working size {photo.working_w}x{photo.working_h} does not "
            f"match its transform ({size[0]}x{size[1]}); re-confirm the reference"
        )
    distances, diameters = set(), set()
    for group in groups:
        load = repo_rifles.get_load(conn, group.load_id)
        rifle = repo_rifles.get_rifle(conn, load.rifle_id) if load else None
        if rifle is None:
            raise ExportError(f"group {group.id} has no rifle")
        distances.add(group.distance_yd)
        diameters.add(rifle.bullet_diameter_in)
    if len(distances) != 1 or len(diameters) != 1:
        raise ExportError(
            f"photo {photo.id}: its groups have different distances {sorted(distances)} yd or "
            f"bullet diameters {sorted(diameters)} in; export them from separate photos"
        )
    holes: list[tuple[float, float]] = []
    entries: list[dict] = []
    for index, group in enumerate(groups, start=1):
        shots = [(s.x_in, s.y_in) for s in repo_groups.list_shots(conn, group.id)]
        entries.append(_group_entry(index, group, len(holes), shots, ppi, h_working))
        holes.extend(shots)
    holes_px = _to_oriented(holes, ppi, h_working)
    meta = json.loads(photo.reference_json or "{}")
    return {
        "format_version": FORMAT_VERSION,
        "reference_kind": photo.reference_kind,
        "distance_yd": float(distances.pop()),
        "bullet_diameter_in": float(diameters.pop()),
        "holes_px": [[float(x), float(y)] for x, y in holes_px],
        "aim_px": entries[0]["aim_px"] if len(entries) == 1 else None,
        "hand_es_in": None,
        "reference": {
            "method": meta.get("method", "manual") if isinstance(meta, dict) else "manual",
            "h_px_to_plane": h_plane.tolist(),
        },
        "groups": entries,
    }


def _make_dirs(path: Path) -> None:
    missing = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    for directory in reversed(missing):
        directory.mkdir(mode=DIR_MODE)
        directory.chmod(DIR_MODE)


def _stage(directory: Path, name: str, data: bytes) -> Path:
    temp = directory / f".{name}.part-{os.getpid()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(temp, flags, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return temp


def _publish(temp: Path, final: Path, force: bool) -> None:
    if force:
        os.replace(temp, final)
        return
    try:
        os.link(temp, final)
    except FileExistsError as exc:
        raise ExportRefused(f"{final} already exists; use --force to overwrite") from exc
    finally:
        temp.unlink(missing_ok=True)


def export_photo(
    conn: sqlite3.Connection,
    settings: Settings,
    photo_id: int,
    out_dir: Path,
    *,
    force: bool = False,
) -> Exported:
    """Export one photo and its finalized groups into `out_dir`; see the module docstring.

    Raises `ExportError` (exit 2) and `ExportRefused` (exit 1). Nothing is written on any error;
    existing files are only replaced with `force`.
    """
    require_current_schema(conn)
    photo = repo_groups.get_photo(conn, photo_id)
    if photo is None:
        raise ExportError(f"photo {photo_id} does not exist")
    groups = [g for g in repo_groups.list_groups(conn) if g.photo_id == photo_id]
    truth = build_truth(conn, photo, groups)
    original = settings.photos_dir / f"{photo.sha256}.{photo.ext}"
    if not original.is_file():
        raise ExportError(f"the stored original {original} is missing")
    stem = export_stem(photo)
    photo_target = out_dir / f"{stem}.{photo.ext}"
    truth_target = out_dir / f"{stem}{TRUTH_SUFFIX}"
    if not force:
        for target in (photo_target, truth_target):
            if target.exists():
                raise ExportRefused(f"{target} already exists; use --force to overwrite")
    _make_dirs(out_dir)
    body = (json.dumps(truth, indent=2) + "\n").encode("utf-8")
    staged = [
        _stage(out_dir, photo_target.name, original.read_bytes()),
        _stage(out_dir, truth_target.name, body),
    ]
    try:
        _publish(staged[0], photo_target, force)
        _publish(staged[1], truth_target, force)
    except BaseException:
        for temp in staged:
            temp.unlink(missing_ok=True)
        raise
    return Exported(photo_target, truth_target, len(groups), len(truth["holes_px"]))


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m rga.export_truth",
        description="Export a finalized photo and its truth file for the real-photo gate.",
    )
    parser.add_argument(
        "--photo-id", type=photo_id_arg, required=True, help="id of the photo to export"
    )
    parser.add_argument("--out", help="output folder (default: <RGA_DATA_DIR>/real_photos)")
    parser.add_argument("--force", action="store_true", help="overwrite existing exported files")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"export_truth: configuration error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not settings.db_path.is_file():
        print(f"export_truth: no database at {settings.db_path}", file=sys.stderr)
        return EXIT_USAGE
    out_dir = Path(args.out) if args.out else settings.data_dir / "real_photos"
    conn = open_readonly(settings.db_path)
    try:
        result = export_photo(conn, settings, args.photo_id, out_dir, force=args.force)
    except ExportRefused as exc:
        print(f"export_truth: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except ExportError as exc:
        print(f"export_truth: {exc}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        conn.close()
    print(f"exported {result.groups} group(s), {result.holes} hole(s)")
    print(result.photo_path)
    print(result.truth_path)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
