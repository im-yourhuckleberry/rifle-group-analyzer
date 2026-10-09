"""Photo service: import, derived images and the user-confirmed reference.

Uploads are validated by `rga.vision.io.load_image`, stored as `<sha256>.<ext>` (no client
filename ever reaches the filesystem) and deduplicated by content hash. The pipeline only
proposes references; a working image and scale exist only after `confirm_reference` records the
reference the user picked or marked. Errors are `PhotoError`s carrying a stable `code`.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from rga.config import Settings
from rga.db import repo_groups
from rga.db.rows import PhotoRow
from rga.domain.types import ReferenceKind, ReferenceResult
from rga.vision.io import (
    MAX_UPLOAD_BYTES,
    ImageRejected,
    load_image,
    load_oriented,
    read_taken_at,
    sha256_hex,
)
from rga.vision.pipeline import (
    ReferenceProposal,
    build_working_image,
    manual_reference,
    propose_candidates,
)
from rga.vision.transform import build_working

MIN_REFERENCE_PX = 64
ORIENTED_JPEG_QUALITY = 92
ORIENTED_VARIANT = "oriented"
WORKING_VARIANT = "working"
THUMB_VARIANT = "thumb"
THUMB_MAX_SIDE_PX = 240


class PhotoError(Exception):
    """Base error with a stable snake_case `code`, a user-facing message and an HTTP status."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class PhotoNotFound(PhotoError):
    """The photo id does not exist."""

    def __init__(self, photo_id: int) -> None:
        super().__init__("photo_not_found", f"photo {photo_id} does not exist", 404)


class UploadRejected(PhotoError):
    """The upload is not an acceptable image; 413 for size limits, 415 for everything else."""

    def __init__(self, message: str) -> None:
        too_big = message.startswith(("upload exceeds", "image exceeds"))
        super().__init__(
            "upload_too_large" if too_big else "unsupported_image", message, 413 if too_big else 415
        )


class ImageTooSmall(PhotoError):
    """The image is smaller than the reference detectors can work on."""

    def __init__(self, width: int, height: int) -> None:
        super().__init__(
            "image_too_small",
            f"the image is {width}x{height} px; at least {MIN_REFERENCE_PX} px on each side is needed",
            422,
        )


class ReferenceUnconfirmed(PhotoError):
    """The photo has no user-confirmed reference, so it has no working image or scale."""

    def __init__(self, photo_id: int) -> None:
        super().__init__(
            "reference_unconfirmed",
            f"photo {photo_id} has no confirmed reference; confirm one first",
            409,
        )


class InvalidReference(PhotoError):
    """The chosen candidate or manual marks cannot define a reference."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, 422)


class ReferenceLocked(PhotoError):
    """A group on this photo holds shots or an aim measured against the reference."""

    def __init__(self, photo_id: int) -> None:
        super().__init__(
            "reference_locked",
            f"photo {photo_id} has a group with shots or an aim; clear them to change its reference",
            409,
        )


class ReferenceInUse(PhotoError):
    """A finalized group on this photo was measured against the reference, so it cannot change."""

    def __init__(self, photo_id: int) -> None:
        super().__init__(
            "reference_in_use",
            f"photo {photo_id} has a finalized group that uses its scale; the reference cannot change",
            409,
        )


@dataclass(frozen=True)
class StoredPhoto:
    """A photo row and whether the upload matched an existing photo."""

    photo: PhotoRow
    duplicate: bool


@dataclass(frozen=True)
class ReferenceWork:
    """Saved work on a photo's groups: draft groups with shots or an aim, and any finalized group."""

    draft_groups: int
    any_final: bool


@dataclass(frozen=True)
class ResetResult:
    """The photo after its reference was cleared and how many draft groups lost saved work."""

    photo: PhotoRow
    cleared_groups: int


@dataclass(frozen=True)
class Confirmed:
    """The photo after confirmation and the warnings the user must see."""

    photo: PhotoRow
    warnings: tuple[str, ...]


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run the block in one write-locked transaction (savepoint when one is already open)."""
    nested = conn.in_transaction
    conn.execute("SAVEPOINT photo_service" if nested else "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if nested:
            conn.execute("ROLLBACK TO photo_service")
            conn.execute("RELEASE photo_service")
        else:
            conn.rollback()
        raise
    if nested:
        conn.execute("RELEASE photo_service")
    conn.commit()


def original_path(settings: Settings, photo: PhotoRow) -> Path:
    """Path of the stored original, derived only from the content hash and sniffed extension."""
    return settings.photos_dir / f"{photo.sha256}.{photo.ext}"


def working_path(settings: Settings, photo_id: int) -> Path:
    """Path of the photo's rectified working image."""
    return settings.working_dir / f"{photo_id}.png"


def oriented_path(settings: Settings, photo_id: int) -> Path:
    """Path of the photo's EXIF-corrected, browser-displayable rendition."""
    return settings.working_dir / f"{photo_id}_oriented.jpg"


def thumb_path(settings: Settings, photo_id: int) -> Path:
    """Path of the photo's small oriented preview (longest side `THUMB_MAX_SIDE_PX`)."""
    return settings.working_dir / f"{photo_id}_thumb.jpg"


def preferred_reference_kind(conn: sqlite3.Connection) -> str | None:
    """The scale reference kind past groups used most (ties go to the earlier kind in the list),
    offered as the reference page's default; None before any group has a confirmed reference."""
    counts = repo_groups.reference_kind_counts(conn)
    if not counts:
        return None
    order = [kind.value for kind in ReferenceKind]
    return max(
        counts, key=lambda kind: (counts[kind], -order.index(kind) if kind in order else -99)
    )


def remove_photo_files(settings: Settings, photos: Sequence[PhotoRow]) -> None:
    """Remove the stored original and every derived image of deleted photos (missing files are
    fine). Only paths derived from the photo rows are touched."""
    for photo in photos:
        for path in (
            original_path(settings, photo),
            working_path(settings, photo.id),
            oriented_path(settings, photo.id),
            thumb_path(settings, photo.id),
        ):
            path.unlink(missing_ok=True)


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _encode(ext: str, img: np.ndarray, params: Sequence[int] = ()) -> bytes:
    ok, buffer = cv2.imencode(ext, img, list(params))
    if not ok:
        raise PhotoError("encode_failed", "the image could not be encoded", 422)
    return buffer.tobytes()


def get_photo(conn: sqlite3.Connection, photo_id: int) -> PhotoRow:
    """Return the photo or raise `PhotoNotFound`."""
    photo = repo_groups.get_photo(conn, photo_id)
    if photo is None:
        raise PhotoNotFound(photo_id)
    return photo


def page_photo(conn: sqlite3.Connection, photo_id: int, group_id: int | None) -> PhotoRow:
    """The photo for the reference page; also checks that `group_id`, when given, exists.

    Raises `PhotoNotFound`, or `PhotoError` `group_not_found` (404).
    """
    photo = get_photo(conn, photo_id)
    if group_id is not None and repo_groups.get_group(conn, group_id) is None:
        raise PhotoError("group_not_found", f"group {group_id} does not exist", 404)
    return photo


def store_upload(conn: sqlite3.Connection, settings: Settings, data: bytes) -> StoredPhoto:
    """Validate and store an uploaded image, deduplicating by sha256.

    The capture time found in the image's EXIF (`taken_at`, `taken_at_offset`) is stored with the
    photo. A byte-identical upload returns the existing photo with `duplicate=True`, and fills the
    capture time when the stored photo has none. Raises `UploadRejected` (413/415) for anything
    `load_image` refuses.
    """
    digest = sha256_hex(data)
    existing = repo_groups.get_photo_by_sha(conn, digest)
    if existing is not None and original_path(settings, existing).is_file():
        return StoredPhoto(_backfill_taken_at(conn, existing, data), True)
    try:
        loaded = load_image(data)
    except ImageRejected as exc:
        raise UploadRejected(str(exc)) from exc
    img, ext = loaded.bgr, loaded.ext
    height, width = img.shape[:2]
    _atomic_write(settings.photos_dir / f"{digest}.{ext}", data)
    try:
        with _write_transaction(conn):
            photo = repo_groups.get_photo_by_sha(conn, digest)
            duplicate = photo is not None
            if photo is None:
                photo = repo_groups.create_photo(
                    conn,
                    sha256=digest,
                    ext=ext,
                    width_px=width,
                    height_px=height,
                    taken_at=loaded.taken_at,
                    taken_at_offset=loaded.taken_at_offset,
                )
    except sqlite3.IntegrityError:
        photo = repo_groups.get_photo_by_sha(conn, digest)
        if photo is None:
            raise
        duplicate = True
    if duplicate:
        photo = _backfill_taken_at(conn, photo, data)
    if not duplicate or not oriented_path(settings, photo.id).is_file():
        _atomic_write(
            oriented_path(settings, photo.id),
            _encode(".jpg", img, (cv2.IMWRITE_JPEG_QUALITY, ORIENTED_JPEG_QUALITY)),
        )
    return StoredPhoto(photo, duplicate)


def _backfill_taken_at(conn: sqlite3.Connection, photo: PhotoRow, data: bytes) -> PhotoRow:
    """Fill the capture time of a stored photo that has none from a re-upload of its bytes."""
    if photo.taken_at is not None:
        return photo
    taken_at, offset = read_taken_at(data)
    if taken_at is None:
        return photo
    with _write_transaction(conn):
        updated = repo_groups.set_photo_taken_at(conn, photo.id, taken_at, offset)
    return updated or photo


def _load_original(settings: Settings, photo: PhotoRow) -> np.ndarray:
    try:
        img, _ = load_oriented(original_path(settings, photo).read_bytes())
    except (ImageRejected, OSError) as exc:
        raise PhotoError(
            "photo_file_unreadable", f"the stored file of photo {photo.id} cannot be read", 409
        ) from exc
    return img


def _require_big_enough(img: np.ndarray) -> None:
    height, width = img.shape[:2]
    if min(height, width) < MIN_REFERENCE_PX:
        raise ImageTooSmall(width, height)


def propose(
    conn: sqlite3.Connection, settings: Settings, photo_id: int, kind: ReferenceKind
) -> list[ReferenceProposal]:
    """Ranked, unconfirmed reference candidates of `kind`; stores nothing."""
    photo = get_photo(conn, photo_id)
    img = _load_original(settings, photo)
    _require_big_enough(img)
    try:
        return propose_candidates(img, kind)
    except ValueError as exc:
        raise InvalidReference(
            "reference_failed", f"no reference could be proposed: {exc}"
        ) from exc


def _photo_has_measurements(conn: sqlite3.Connection, photo_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM groups g WHERE g.photo_id = ? AND (g.aim_x_in IS NOT NULL "
        "OR EXISTS (SELECT 1 FROM shots s WHERE s.group_id = g.id)) LIMIT 1",
        (photo_id,),
    ).fetchone()
    return row is not None


def reference_locked(
    conn: sqlite3.Connection, photo_id: int, except_group: int | None = None
) -> bool:
    """True while a group on the photo has shots or an aim, which fixes its reference.

    With `except_group` only the other groups count, plus any finalized one: a group going back to
    its own reference step may change the scale (its own aim and shots are cleared first), but not
    one another group measured against."""
    if except_group is None:
        return _photo_has_measurements(conn, photo_id)
    row = conn.execute(
        "SELECT 1 FROM groups g WHERE g.photo_id = ? AND g.id <> ? AND (g.status = 'final' "
        "OR g.aim_x_in IS NOT NULL OR EXISTS (SELECT 1 FROM shots s WHERE s.group_id = g.id)) "
        "LIMIT 1",
        (photo_id, except_group),
    ).fetchone()
    return row is not None


def group_has_work(conn: sqlite3.Connection, group_id: int) -> bool:
    """True when the group has saved shots or an aim (taken against its photo's current scale)."""
    row = conn.execute(
        "SELECT 1 FROM groups g WHERE g.id = ? AND (g.aim_x_in IS NOT NULL "
        "OR EXISTS (SELECT 1 FROM shots s WHERE s.group_id = g.id))",
        (group_id,),
    ).fetchone()
    return row is not None


_DRAFTS_WITH_WORK = (
    "SELECT g.id FROM groups g WHERE g.photo_id = ? AND g.status = 'draft' AND "
    "(g.aim_x_in IS NOT NULL OR EXISTS (SELECT 1 FROM shots s WHERE s.group_id = g.id)) "
    "ORDER BY g.id"
)


def _any_final(conn: sqlite3.Connection, photo_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM groups WHERE photo_id = ? AND status = 'final' LIMIT 1", (photo_id,)
    ).fetchone()
    return row is not None


def reference_work(conn: sqlite3.Connection, photo_id: int) -> ReferenceWork:
    """Count the photo's draft groups that hold shots or an aim, and whether any group is final."""
    drafts = conn.execute(_DRAFTS_WITH_WORK, (photo_id,)).fetchall()
    return ReferenceWork(len(drafts), _any_final(conn, photo_id))


def reset_reference(conn: sqlite3.Connection, photo_id: int) -> ResetResult:
    """Clear the photo's confirmed reference and the measurements taken against it, atomically.

    Every draft group on the photo loses its shots, rejections, aim and region; the photo returns
    to the unconfirmed state so a new reference can be chosen. Groups on other photos are not
    touched. Raises `PhotoNotFound`, and `ReferenceInUse` when any group on the photo is final;
    nothing changes on any error.
    """
    get_photo(conn, photo_id)
    with _write_transaction(conn):
        if _any_final(conn, photo_id):
            raise ReferenceInUse(photo_id)
        with_work = {row["id"] for row in conn.execute(_DRAFTS_WITH_WORK, (photo_id,))}
        drafts = conn.execute(
            "SELECT id FROM groups WHERE photo_id = ? AND status = 'draft' ORDER BY id",
            (photo_id,),
        ).fetchall()
        for row in drafts:
            repo_groups.replace_shots(conn, row["id"], [])
            conn.execute("DELETE FROM detection_rejections WHERE group_id = ?", (row["id"],))
            repo_groups.update_group(conn, row["id"], aim_x_in=None, aim_y_in=None, roi_json=None)
        photo = repo_groups.update_photo_reference(
            conn,
            photo_id,
            reference_kind=None,
            reference_json=None,
            h_px_to_plane_json=None,
            working_ppi=None,
            working_w=None,
            working_h=None,
            axis_ratio=None,
            reference_confirmed=False,
        )
    if photo is None:
        raise PhotoNotFound(photo_id)
    return ResetResult(photo, len(with_work))


def _chosen_reference(
    kind: ReferenceKind,
    candidate_index: int | None,
    manual: dict | None,
    proposals: Sequence[ReferenceProposal],
) -> ReferenceResult:
    if candidate_index is not None:
        if candidate_index >= len(proposals):
            raise InvalidReference(
                "invalid_candidate_index",
                f"candidate {candidate_index} does not exist; {len(proposals)} were proposed",
            )
        return proposals[candidate_index].result
    try:
        return manual_reference(kind, **(manual or {}))
    except ValueError as exc:
        raise InvalidReference("invalid_reference", f"the marks are not usable: {exc}") from exc


def confirm_reference(
    conn: sqlite3.Connection,
    settings: Settings,
    photo_id: int,
    kind: ReferenceKind,
    *,
    candidate_index: int | None = None,
    manual: dict | None = None,
) -> Confirmed:
    """Record the user's chosen reference, build and store the working image, mark it confirmed.

    Exactly one of `candidate_index` (into a fresh proposal of `kind`) or `manual` (keyword
    arguments of `manual_reference`) is given. The reference is locked while a group on the photo
    has shots or an aim; otherwise confirming clears the saved ROIs of the photo's groups, which
    are in the old working pixels. Raises `InvalidReference`, `ImageTooSmall`,
    `ReferenceLocked` or `PhotoNotFound`; nothing is stored on failure.
    """
    if (candidate_index is None) == (manual is None):
        raise InvalidReference("invalid_reference", "give exactly one of candidate_index or manual")
    photo = get_photo(conn, photo_id)
    if _photo_has_measurements(conn, photo_id):
        raise ReferenceLocked(photo_id)
    img = _load_original(settings, photo)
    _require_big_enough(img)
    proposals: list[ReferenceProposal] = []
    if candidate_index is not None:
        try:
            proposals = propose_candidates(img, kind)
        except ValueError as exc:
            raise InvalidReference(
                "reference_failed", f"no reference could be proposed: {exc}"
            ) from exc
    reference = _chosen_reference(kind, candidate_index, manual, proposals)
    try:
        working, frame, _ = build_working_image(img, reference)
    except ValueError as exc:
        raise InvalidReference(
            "reference_failed", f"the working image could not be built: {exc}"
        ) from exc
    with _write_transaction(conn):
        if _photo_has_measurements(conn, photo_id):
            raise ReferenceLocked(photo_id)
        conn.execute("UPDATE groups SET roi_json = NULL WHERE photo_id = ?", (photo_id,))
        updated = repo_groups.update_photo_reference(
            conn,
            photo_id,
            reference_kind=kind.value,
            reference_json=json.dumps(
                {
                    "kind": reference.kind.value,
                    "method": reference.method,
                    "axis_ratio": reference.axis_ratio,
                    "warnings": list(reference.warnings),
                }
            ),
            h_px_to_plane_json=json.dumps(reference.h_px_to_plane),
            working_ppi=frame.ppi,
            working_w=frame.width_px,
            working_h=frame.height_px,
            axis_ratio=reference.axis_ratio,
            reference_confirmed=True,
        )
    if updated is None:
        raise PhotoNotFound(photo_id)
    _atomic_write(working_path(settings, photo_id), _encode(".png", working))
    return Confirmed(updated, tuple(reference.warnings))


def image_file(conn: sqlite3.Connection, settings: Settings, photo_id: int, variant: str) -> Path:
    """Path of the photo's `oriented`, `working` or `thumb` image, rebuilt from the original if missing.

    The working image exists only for a confirmed reference (`ReferenceUnconfirmed` otherwise).
    The path is derived from the photo row; no client value ever reaches the filesystem.
    """
    photo = get_photo(conn, photo_id)
    if variant == WORKING_VARIANT:
        if not photo.reference_confirmed or photo.h_px_to_plane_json is None:
            raise ReferenceUnconfirmed(photo_id)
        path = working_path(settings, photo_id)
        if not path.is_file():
            img = _load_original(settings, photo)
            working, _, _ = build_working(img, np.array(json.loads(photo.h_px_to_plane_json)))
            _atomic_write(path, _encode(".png", working))
        return path
    if variant == THUMB_VARIANT:
        path = thumb_path(settings, photo_id)
        if not path.is_file():
            img = _load_original(settings, photo)
            scale = THUMB_MAX_SIDE_PX / max(img.shape[:2])
            if scale < 1:
                size = (max(1, round(img.shape[1] * scale)), max(1, round(img.shape[0] * scale)))
                img = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
            _atomic_write(
                path, _encode(".jpg", img, (cv2.IMWRITE_JPEG_QUALITY, ORIENTED_JPEG_QUALITY))
            )
        return path
    path = oriented_path(settings, photo_id)
    if not path.is_file():
        img = _load_original(settings, photo)
        _atomic_write(path, _encode(".jpg", img, (cv2.IMWRITE_JPEG_QUALITY, ORIENTED_JPEG_QUALITY)))
    return path


__all__ = [
    "MAX_UPLOAD_BYTES",
    "Confirmed",
    "ImageTooSmall",
    "InvalidReference",
    "PhotoError",
    "PhotoNotFound",
    "PhotoRow",
    "ReferenceInUse",
    "ReferenceLocked",
    "ReferenceProposal",
    "ReferenceUnconfirmed",
    "ReferenceWork",
    "ResetResult",
    "StoredPhoto",
    "UploadRejected",
    "confirm_reference",
    "get_photo",
    "image_file",
    "oriented_path",
    "group_has_work",
    "preferred_reference_kind",
    "remove_photo_files",
    "thumb_path",
    "original_path",
    "page_photo",
    "propose",
    "reference_locked",
    "reference_work",
    "reset_reference",
    "store_upload",
    "working_path",
]
