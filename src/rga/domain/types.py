"""Shared domain types: enums and frozen dataclasses used by every layer."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class ReferenceKind(StrEnum):
    """Kind of printed scale reference in a target photo."""

    GRID_1IN = "grid_1in"
    CIRCLE_1IN = "circle_1in"
    CIRCLE_1_5IN = "circle_1_5in"
    CIRCLE_3IN = "circle_3in"


REFERENCE_SIZE_IN: dict[ReferenceKind, float] = {
    ReferenceKind.GRID_1IN: 1.0,
    ReferenceKind.CIRCLE_1IN: 1.0,
    ReferenceKind.CIRCLE_1_5IN: 1.5,
    ReferenceKind.CIRCLE_3IN: 3.0,
}


class ShotOrigin(StrEnum):
    """How a shot position was obtained."""

    DETECTED = "detected"
    SPLIT = "split"
    ESTIMATED = "estimated"
    MANUAL = "manual"


class BlobKind(StrEnum):
    """Classification of a detected hole blob."""

    SINGLE = "single"
    SPLIT = "split"
    RAGGED = "ragged"


class LoadKind(StrEnum):
    """Whether a load is factory ammunition or a handload."""

    FACTORY = "factory"
    HANDLOAD = "handload"


class Position(StrEnum):
    """Shooting position of a group."""

    BENCH = "bench"
    PRONE_OFF_PACK = "prone_off_pack"
    KNEELING_SITTING = "kneeling_sitting"
    STANDING_FIELD = "standing_field"


class ShotTimeSource(StrEnum):
    """Where a group's shot date and time came from: the photo's EXIF, typed in, or carried over from before groups had their own context."""

    PHOTO = "photo"
    MANUAL = "manual"
    SESSION = "session"


class GroupPurpose(StrEnum):
    """Role a group plays in the workflow or zeroing procedure."""

    GENERAL = "general"
    ZERO_STEP1 = "zero_step1"
    ZERO_STEP2 = "zero_step2"
    ZERO_CONFIRM = "zero_confirm"
    ZERO_CHECK = "zero_check"


class GroupStatus(StrEnum):
    """Review state of a group."""

    DRAFT = "draft"
    FINAL = "final"


class ZeroState(StrEnum):
    """Step of the zeroing procedure a zero record is in."""

    STEP1 = "step1"
    STEP2_INITIAL = "step2_initial"
    STEP2_CONFIRM = "step2_confirm"
    STEP3_DOT = "step3_dot"
    COMPLETE = "complete"
    SUPERSEDED = "superseded"


class RejectionReason(StrEnum):
    """The only rejection reason by design: a rejected detection is not a bullet hole."""

    NOT_A_BULLET_HOLE = "not_a_bullet_hole"


class AdviceKind(StrEnum):
    """Category of advice returned by the zeroing rules."""

    CORRECTION = "correction"
    NO_ADJUSTMENT = "no_adjustment"
    SHOOT_MORE = "shoot_more"
    TOUCH_UP_OK = "touch_up_ok"
    CONFIRM_AGAIN = "confirm_again"
    READY_FOR_DOT = "ready_for_dot"
    INVALID = "invalid"


@dataclass(frozen=True)
class PlanePoint:
    """Point on the target plane in inches; origin top-left, y down."""

    x_in: float
    y_in: float


@dataclass(frozen=True)
class Offset:
    """Displacement from the aim point in inches; x right, y up."""

    dx_in: float
    dy_in: float


@dataclass(frozen=True)
class Shot:
    """A shot position on the target plane with its provenance."""

    point: PlanePoint
    origin: ShotOrigin
    confidence: float | None = None
    moved_by_user: bool = False


@dataclass(frozen=True)
class WorkingFrame:
    """Pixel dimensions and pixels-per-inch of a rectified working image."""

    ppi: float
    width_px: int
    height_px: int


@dataclass(frozen=True)
class Blob:
    """A detected dark region with its contour and estimated hole centers."""

    kind: BlobKind
    contour_px: tuple[tuple[int, int], ...]
    area_px: float
    centers_px: tuple[tuple[float, float], ...]
    confidence: float


@dataclass(frozen=True)
class CircleCandidate:
    """A fitted reference-circle ellipse in oriented image pixels."""

    center_px: tuple[float, float]
    axes_px: tuple[float, float]
    angle_deg: float
    score: float


@dataclass(frozen=True)
class ReferenceResult:
    """A confirmed reference with its pixel-to-plane homography."""

    kind: ReferenceKind
    method: Literal["auto", "manual"]
    h_px_to_plane: tuple[tuple[float, ...], ...]
    axis_ratio: float | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class GroupStats:
    """Summary statistics of one group of shots."""

    n: int
    es_in: float
    es_moa: float
    mean_dx_in: float
    mean_dy_in: float
    mean_dx_mil: float
    mean_dy_mil: float
    includes_estimated: bool


@dataclass(frozen=True)
class Correction:
    """Signed scope correction in mil; text is e.g. 'Dial DOWN 0.2, LEFT 0.3' or 'No adjustment'."""

    up_mil: float
    right_mil: float
    text: str


@dataclass(frozen=True)
class Advice:
    """Advice message with optional correction and statistics."""

    kind: AdviceKind
    message: str
    correction: Correction | None = None
    stats: GroupStats | None = None


@dataclass(frozen=True)
class ZeroCheckResult:
    """Outcome of a zero check against a dot."""

    passed: bool
    per_shot_inside: tuple[bool, ...]
    message: str
