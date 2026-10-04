"""Pydantic request/response models for every HTTP API route (design §4.7).

Conventions
- JSON only, under `/api`; list endpoints return a bare JSON array of the item model.
- Errors are `ErrorResponse` (`{"detail": str, "code": str}`), the shape `static/js/api.js` reads.
- Coordinates: `*_in` values are target-plane inches (origin top-left, y down); `*_px` values are
  working-image pixels; offsets (`dx_*`, `dy_*`) are from the aim point with x right, y up.
- Request models forbid unknown fields and non-finite floats. PATCH models need at least one
  field, and only fields listed in `nullable` may be set to null.
- `ROUTE_SCHEMAS` maps every (method, path) of design §4.7 to its request/query/response models.
"""

import datetime as dt
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

from rga.domain.types import (
    AdviceKind,
    GroupPurpose,
    GroupStatus,
    LoadKind,
    Position,
    ReferenceKind,
    RejectionReason,
    ShotOrigin,
    ShotTimeSource,
    ZeroState,
)

# ---------------------------------------------------------------------------------------------
# Base classes and shared field types
# ---------------------------------------------------------------------------------------------

Name = Annotated[str, StringConstraints(min_length=1, max_length=200)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=200)]
LotCode = Annotated[str, StringConstraints(min_length=1, max_length=64)]
Notes = Annotated[str, StringConstraints(max_length=10_000)]
Inches = Annotated[float, Field(ge=-1000, le=1000)]
SQLITE_MAX_INTEGER = 2**63 - 1
PositiveId = Annotated[int, Field(ge=1, le=SQLITE_MAX_INTEGER)]
BulletDiameterIn = Annotated[float, Field(gt=0.1, lt=0.6)]
ShotsExpected = Annotated[int, Field(ge=1, le=200)]
DistanceYd = Annotated[int, Field(gt=0, le=10_000)]
WindClock = Annotated[int, Field(ge=1, le=12)]
Mil = Annotated[float, Field(ge=-100, le=100)]

ZeroMode = Literal["new_setup", "existing"]
ZeroCheckOutcome = Literal["pass", "possible_lost_zero"]
CompareStatus = Literal["insufficient_data", "ok", "high_confidence"]
PairVerdict = Literal["insufficient_data", "no_real_difference", "a_smaller", "b_smaller"]
DetectStatus = Literal["ok", "too_many", "unassigned_ragged"]
CompareTier = Literal["tier10", "tier30"]
SearchKind = Literal["rifle", "load", "group"]

ES_LABEL = "smallest target every shot hit (center-to-center)"


class RequestModel(BaseModel):
    """Base for request bodies and query models: strict, trimmed, finite floats only."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class PatchModel(RequestModel):
    """Base for PATCH bodies: at least one field, and null only where `nullable` allows it."""

    nullable: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def _check_patch(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        for name in self.model_fields_set:
            if getattr(self, name) is None and name not in self.nullable:
                raise ValueError(f"{name} cannot be null")
        return self


class ResponseModel(BaseModel):
    """Base for response bodies; built from row dataclasses or dicts."""

    model_config = ConfigDict(from_attributes=True, allow_inf_nan=False)


class ErrorResponse(ResponseModel):
    """Error body for every 4xx/5xx: `detail` is human readable, `code` is a stable snake_case key."""

    detail: str
    code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class HealthOut(ResponseModel):
    """`GET /healthz`."""

    status: Literal["ok"]
    schema_version: int


# ---------------------------------------------------------------------------------------------
# Shared geometry
# ---------------------------------------------------------------------------------------------


class PointPx(RequestModel):
    """A point in image pixels."""

    x_px: Annotated[float, Field(ge=0, le=100_000)]
    y_px: Annotated[float, Field(ge=0, le=100_000)]


class PlanePointIn(RequestModel):
    """A point on the target plane in inches."""

    x_in: Inches
    y_in: Inches


class PlanePointOut(ResponseModel):
    """A point on the target plane in inches."""

    x_in: float
    y_in: float


class RoiPx(RequestModel):
    """Rectangle in working-image pixels: `x0,y0` inclusive top-left, `x1,y1` exclusive bottom-right."""

    x0: Annotated[int, Field(ge=0)]
    y0: Annotated[int, Field(ge=0)]
    x1: Annotated[int, Field(ge=1)]
    y1: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def _non_empty(self) -> Self:
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("roi must have x1 > x0 and y1 > y0")
        return self


class RoiPxOut(ResponseModel):
    """Stored region of interest in working-image pixels."""

    x0: int
    y0: int
    x1: int
    y1: int


class WorkingFrameOut(ResponseModel):
    """Working-image geometry: plane inches times `ppi` equals working pixels."""

    ppi: float
    width_px: int
    height_px: int


# ---------------------------------------------------------------------------------------------
# Calibers
# ---------------------------------------------------------------------------------------------


class CaliberOut(ResponseModel):
    """One row of the caliber table (`GET /api/calibers` returns a list)."""

    cartridge: str
    bullet_diameter_in: float


# ---------------------------------------------------------------------------------------------
# Rifles
# ---------------------------------------------------------------------------------------------


class RifleCreate(RequestModel):
    """`POST /api/rifles`. Omitting `bullet_diameter_in` asks the server to use the caliber table."""

    name: Name
    cartridge: ShortText
    bullet_diameter_in: BulletDiameterIn | None = None
    scope: Annotated[str, StringConstraints(max_length=200)] | None = None
    notes: Notes = ""


class RifleUpdate(PatchModel):
    """`PATCH /api/rifles/{id}`."""

    nullable: ClassVar[frozenset[str]] = frozenset({"scope"})

    name: Name | None = None
    cartridge: ShortText | None = None
    bullet_diameter_in: BulletDiameterIn | None = None
    scope: Annotated[str, StringConstraints(max_length=200)] | None = None
    notes: Notes | None = None


class ZeroLoadSet(RequestModel):
    """`PUT /api/rifles/{id}/zero-load`; `load_id` null clears the zero load."""

    load_id: PositiveId | None


class RifleOut(ResponseModel):
    """A rifle."""

    id: int
    public_id: str
    name: str
    cartridge: str
    bullet_diameter_in: float
    scope: str | None
    notes: str
    zero_load_id: int | None
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------------------------
# Loads and lots
# ---------------------------------------------------------------------------------------------

_FACTORY_FIELDS = ("maker", "product", "bullet_weight_gr", "bullet_type")
_HANDLOAD_FIELDS = ("bullet_weight_gr", "bullet", "primer", "brass", "coal_in")

OptText = Annotated[str, StringConstraints(max_length=200)] | None
OptWeight = Annotated[float, Field(gt=0, le=2000)] | None
OptMv = Annotated[float, Field(gt=0, le=6000)] | None


def _three_decimals(value: float) -> float:
    if abs(value * 1000 - round(value * 1000)) > 1e-6:
        raise ValueError("must have at most 3 decimal places")
    return value


OptCoal = Annotated[float, Field(ge=1, le=5), AfterValidator(_three_decimals)] | None


class LoadCreate(RequestModel):
    """`POST /api/rifles/{id}/loads`. Factory-only and handload-only fields cannot be mixed."""

    kind: LoadKind
    label: Name
    maker: OptText = None
    product: OptText = None
    bullet_weight_gr: OptWeight = None
    bullet_type: OptText = None
    bullet: OptText = None
    primer: OptText = None
    brass: OptText = None
    coal_in: OptCoal = None
    trued_mv_fps: OptMv = None
    notes: Notes = ""

    @model_validator(mode="after")
    def _kind_fields(self) -> Self:
        own = _FACTORY_FIELDS if self.kind is LoadKind.FACTORY else _HANDLOAD_FIELDS
        foreign = (set(_FACTORY_FIELDS) | set(_HANDLOAD_FIELDS)) - set(own)
        used = sorted(f for f in foreign if getattr(self, f) is not None)
        if used:
            raise ValueError(f"fields not allowed for a {self.kind.value} load: {', '.join(used)}")
        return self


class LoadUpdate(PatchModel):
    """`PATCH /api/loads/{id}`. `kind` is immutable; the service rejects cross-kind fields."""

    nullable: ClassVar[frozenset[str]] = frozenset(
        {*_FACTORY_FIELDS, *_HANDLOAD_FIELDS, "trued_mv_fps"}
    )

    label: Name | None = None
    maker: OptText = None
    product: OptText = None
    bullet_weight_gr: OptWeight = None
    bullet_type: OptText = None
    bullet: OptText = None
    primer: OptText = None
    brass: OptText = None
    coal_in: OptCoal = None
    trued_mv_fps: OptMv = None
    notes: Notes | None = None


class LoadOut(ResponseModel):
    """A load."""

    id: int
    public_id: str
    rifle_id: int
    kind: LoadKind
    label: str
    maker: str | None
    product: str | None
    bullet_weight_gr: float | None
    bullet_type: str | None
    bullet: str | None
    primer: str | None
    brass: str | None
    coal_in: float | None
    trued_mv_fps: float | None
    notes: str
    created_at: str
    updated_at: str


class LotCreate(RequestModel):
    """`POST /api/loads/{id}/lots`: get-or-create by code."""

    lot_code: LotCode


class LotOut(ResponseModel):
    """A lot or batch of a load."""

    id: int
    load_id: int
    lot_code: str
    created_at: str


class RifleDetailOut(RifleOut):
    """`GET /api/rifles/{id}`: the rifle with its loads."""

    loads: list[LoadOut]


# ---------------------------------------------------------------------------------------------
# Groups (each group carries its own date, place and weather context; B-66)
# ---------------------------------------------------------------------------------------------

_CONTEXT_NULLABLE = frozenset(
    {
        "shot_time",
        "utc_offset",
        "suppressor_id",
        "temperature_f",
        "density_altitude_ft",
        "wind_mph",
        "wind_clock",
    }
)
SuppressorName = Annotated[str, StringConstraints(min_length=1, max_length=80)]
TemperatureF = Annotated[float, Field(ge=-100, le=150)] | None
DensityAltitudeFt = Annotated[float, Field(ge=-2000, le=30_000)] | None
WindMph = Annotated[float, Field(ge=0, le=200)] | None
WindClockOpt = WindClock | None
UtcOffset = Annotated[str, StringConstraints(pattern=r"^[+-][0-1][0-9]:[0-5][0-9]$")]
_TIME_TEXT = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$")


def _clock_time(value: str) -> str:
    if not _TIME_TEXT.fullmatch(value):
        raise ValueError("time must be HH:MM or HH:MM:SS (24 hour, local)")
    return value if len(value) == 8 else f"{value}:00"


ShotTime = Annotated[str, AfterValidator(_clock_time)]
"""Local time of day, `HH:MM` or `HH:MM:SS`, normalized to `HH:MM:SS`."""


def _check_suppressor(suppressor: bool | None, suppressor_id: int | None) -> None:
    if suppressor_id is not None and suppressor is False:
        raise ValueError("suppressor_id is only allowed when suppressor is true")


class SuppressorCreate(RequestModel):
    """`POST /api/suppressors`: remember a suppressor by name (any letter case matches a saved one)."""

    name: SuppressorName


class SuppressorOut(ResponseModel):
    """A saved suppressor."""

    id: int
    name: str
    created_at: str


class GroupCreate(RequestModel):
    """`POST /api/groups`. `lot_code` is get-or-create under `load_id`.

    The rifle comes from the load. `shot_date` may be omitted when the photo carries its own date;
    `shot_time` and `utc_offset` are optional and local. A new group is a `general` group not yet
    linked to a zero (the service refuses any other `purpose` and any `zero_id`). `suppressor_id` names the suppressor and needs `suppressor` true. Weather
    fields may be null; `weather_skipped` records an explicit skip (the service rejects entered
    values together with a skip).
    """

    load_id: PositiveId
    lot_code: LotCode
    shots_expected: ShotsExpected
    shot_date: dt.date | None = None
    shot_time: ShotTime | None = None
    utc_offset: UtcOffset | None = None
    distance_yd: DistanceYd
    position: Position
    suppressor: bool
    suppressor_id: PositiveId | None = None
    temperature_f: TemperatureF = None
    density_altitude_ft: DensityAltitudeFt = None
    wind_mph: WindMph = None
    wind_clock: WindClockOpt = None
    weather_skipped: bool = False
    elevation_dialed_mil: Mil | None = None
    purpose: GroupPurpose = GroupPurpose.GENERAL
    zero_id: PositiveId | None = None
    photo_id: PositiveId | None = None
    notes: Notes = ""

    @model_validator(mode="after")
    def _context_rules(self) -> Self:
        _check_suppressor(self.suppressor, self.suppressor_id)
        if self.shot_time is not None and self.shot_date is None:
            raise ValueError("shot_time needs shot_date")
        return self


class GroupUpdate(PatchModel):
    """`PATCH /api/groups/{id}`; `photo_id` links an uploaded photo to the group.

    Carries the context fields too. Turning the suppressor off clears its name. Editing the date
    or time makes the group's date source `manual`; changing the distance recomputes its offsets.
    """

    nullable: ClassVar[frozenset[str]] = _CONTEXT_NULLABLE | {
        "elevation_dialed_mil",
        "zero_id",
        "photo_id",
    }

    shots_expected: ShotsExpected | None = None
    shot_date: dt.date | None = None
    shot_time: ShotTime | None = None
    utc_offset: UtcOffset | None = None
    distance_yd: DistanceYd | None = None
    position: Position | None = None
    suppressor: bool | None = None
    suppressor_id: PositiveId | None = None
    temperature_f: TemperatureF = None
    density_altitude_ft: DensityAltitudeFt = None
    wind_mph: WindMph = None
    wind_clock: WindClockOpt = None
    weather_skipped: bool | None = None
    elevation_dialed_mil: Mil | None = None
    purpose: GroupPurpose | None = None
    zero_id: PositiveId | None = None
    photo_id: PositiveId | None = None
    notes: Notes | None = None

    @model_validator(mode="after")
    def _context_rules(self) -> Self:
        _check_suppressor(self.suppressor, self.suppressor_id)
        return self


class GroupListQuery(RequestModel):
    """Query string of `GET /api/groups`; every filter is optional and filters combine with AND."""

    load_id: PositiveId | None = None
    rifle_id: PositiveId | None = None
    status: GroupStatus | None = None


class GroupDefaultsOut(ResponseModel):
    """`GET /api/rifles/{id}/group-defaults`: values to pre-fill the next group of this rifle.

    Taken from the rifle's latest group; distance, position and suppressor fall back to the latest
    group of any rifle. Every field is null when there is nothing to copy.
    """

    source_group_id: int | None = None
    load_id: int | None = None
    lot_code: str | None = None
    shots_expected: int | None = None
    shot_date: dt.date | None = None
    distance_yd: int | None = None
    position: Position | None = None
    suppressor: bool | None = None
    suppressor_id: int | None = None
    temperature_f: float | None = None
    density_altitude_ft: float | None = None
    wind_mph: float | None = None
    wind_clock: int | None = None
    weather_skipped: bool | None = None


class ShotOut(ResponseModel):
    """A stored or proposed shot; offsets are null until an aim point exists."""

    seq: int
    x_in: float
    y_in: float
    dx_in: float | None = None
    dy_in: float | None = None
    dx_mil: float | None = None
    dy_mil: float | None = None
    origin: ShotOrigin
    moved_by_user: bool = False
    confidence: float | None = None


class RejectionOut(ResponseModel):
    """A detection the user rejected."""

    id: int
    x_in: float
    y_in: float
    reason: RejectionReason


class GroupOut(ResponseModel):
    """A group with its own context; `new_lot` is the "new lot, check POI against zero load" flag
    (AC-11) and `weather_prompt` is true at 200 yd and beyond (AC-12)."""

    id: int
    public_id: str
    rifle_id: int
    load_id: int
    lot_id: int
    lot_code: str
    photo_id: int | None
    roi_px: RoiPxOut | None
    shot_date: dt.date
    shot_time: str | None = None
    utc_offset: str | None = None
    shot_at_source: ShotTimeSource
    distance_yd: int
    position: Position
    suppressor: bool
    suppressor_id: int | None = None
    suppressor_name: str | None = None
    temperature_f: float | None = None
    density_altitude_ft: float | None = None
    wind_mph: float | None = None
    wind_clock: int | None = None
    weather_skipped: bool = False
    weather_prompt: bool = False
    shots_expected: int
    elevation_dialed_mil: float | None
    aim: PlanePointOut | None
    purpose: GroupPurpose
    zero_id: int | None
    status: GroupStatus
    notes: str
    new_lot: bool = False
    masked: bool = False
    finalized_at: str | None
    created_at: str
    updated_at: str


class GroupStatsOut(ResponseModel):
    """Group statistics; `poi_text` carries direction words, e.g. "0.3 mil high, 0.2 mil right"."""

    n: int
    es_in: float
    es_moa: float
    mean_dx_in: float
    mean_dy_in: float
    mean_dx_mil: float
    mean_dy_mil: float
    includes_estimated: bool
    es_label: str = ES_LABEL
    poi_text: str = ""


class GroupDetailOut(GroupOut):
    """`GET /api/groups/{id}`: the group with shots, rejections, the geometry the editor needs, and any
    notes carried over from before groups had their own context."""

    shots: list[ShotOut]
    rejections: list[RejectionOut]
    frame: WorkingFrameOut | None
    bullet_diameter_in: float
    load_label: str
    legacy_session_notes: str = ""


# ---------------------------------------------------------------------------------------------
# Photos and reference
# ---------------------------------------------------------------------------------------------


class ImageVariant(StrEnum):
    """`variant` query value of `GET /api/photos/{id}/image`."""

    ORIENTED = "oriented"
    WORKING = "working"


class ImageQuery(RequestModel):
    """Query string of `GET /api/photos/{id}/image`."""

    variant: ImageVariant = ImageVariant.ORIENTED


class PhotoOut(ResponseModel):
    """A stored photo; `duplicate` is true when an upload matched an existing sha256. `taken_at` is the
    local capture time from the image metadata (`YYYY-MM-DDTHH:MM:SS`), null when it has none."""

    id: int
    sha256: str
    ext: str
    width_px: int
    height_px: int
    reference_kind: ReferenceKind | None
    reference_confirmed: bool
    working_ppi: float | None
    working_w: int | None
    working_h: int | None
    axis_ratio: float | None
    taken_at: str | None = None
    taken_at_offset: str | None = None
    created_at: str
    duplicate: bool = False


class ReferenceProposeRequest(RequestModel):
    """`POST /api/photos/{id}/reference/propose`."""

    kind: ReferenceKind


class CircleOut(ResponseModel):
    """Candidate circle or ellipse in oriented-image pixels."""

    center_x_px: float
    center_y_px: float
    axis_major_px: float
    axis_minor_px: float
    angle_deg: float


class ReferenceCandidateOut(ResponseModel):
    """One ranked reference candidate; `index` is what confirm refers to."""

    index: int
    method: Literal["auto", "manual"]
    axis_ratio: float | None
    warnings: list[str]
    circle: CircleOut | None = None
    grid_points_px: list[tuple[float, float]] | None = None


class ReferenceProposeOut(ResponseModel):
    """Ranked candidates; never auto-confirmed. An empty list means manual marking is needed."""

    kind: ReferenceKind
    candidates: list[ReferenceCandidateOut]


class ManualGrid(RequestModel):
    """Four intersection clicks (TL, TR, BR, BL) plus squares spanned across and down."""

    corners_px: Annotated[list[PointPx], Field(min_length=4, max_length=4)]
    squares_x: Annotated[int, Field(ge=1, le=100)]
    squares_y: Annotated[int, Field(ge=1, le=100)]


class ManualReference(RequestModel):
    """Manual marking: circle edge points (at least 3) or a `ManualGrid`, exactly one."""

    edge_points_px: Annotated[list[PointPx], Field(min_length=3, max_length=50)] | None = None
    grid: ManualGrid | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.edge_points_px is None) == (self.grid is None):
            raise ValueError("provide exactly one of edge_points_px or grid")
        return self


class ReferenceConfirmRequest(RequestModel):
    """`POST /api/photos/{id}/reference/confirm`: pick a proposed candidate or mark manually."""

    kind: ReferenceKind
    candidate_index: Annotated[int, Field(ge=0)] | None = None
    manual: ManualReference | None = None

    @model_validator(mode="after")
    def _choice_matches_kind(self) -> Self:
        if (self.candidate_index is None) == (self.manual is None):
            raise ValueError("provide exactly one of candidate_index or manual")
        if self.manual is not None:
            wants_grid = self.kind is ReferenceKind.GRID_1IN
            if wants_grid != (self.manual.grid is not None):
                raise ValueError(
                    "manual grid marking is required for grid_1in; edge points for circle kinds"
                )
        return self


class ReferenceConfirmOut(ResponseModel):
    """Confirmed reference: the photo now has a working image; `warnings` includes the ellipse warning."""

    photo: PhotoOut
    warnings: list[str]


class ReferenceResetOut(ResponseModel):
    """Reference cleared: `photo` is unconfirmed again; `cleared_groups` counts the draft groups
    that lost saved shots or an aim."""

    photo: PhotoOut
    cleared_groups: Annotated[int, Field(ge=0)]


class SnapAimRequest(RequestModel):
    """`POST /api/photos/{id}/snap-aim`: a click in working-image pixels."""

    x_px: Annotated[float, Field(ge=0, le=100_000)]
    y_px: Annotated[float, Field(ge=0, le=100_000)]


class SnapAimOut(ResponseModel):
    """Snapped aim point; `snapped` is false when no aim mark was found and the click is echoed."""

    snapped: bool
    x_px: float
    y_px: float
    x_in: float
    y_in: float


# ---------------------------------------------------------------------------------------------
# Detection, review, finalize
# ---------------------------------------------------------------------------------------------


class DetectRequest(RequestModel):
    """`POST /api/groups/{id}/detect`; `roi_px` restricts detection to this group's region."""

    roi_px: RoiPx | None = None


AimKindOut = Literal[
    "paster_center", "dot_center", "diamond_center", "cross_center", "grid_intersection"
]


class AimCandidateOut(ResponseModel):
    """A proposed aim point in plane inches; the user accepts it or places the aim themselves."""

    kind: AimKindOut
    x_in: float
    y_in: float
    score: Annotated[float, Field(ge=0, le=1)]


class DetectOut(ResponseModel):
    """Proposed shots after detect or fill-shortfall; the user reviews before anything is final.

    `aim_candidates` is filled by detect only, and only while the group has no aim yet.
    """

    status: DetectStatus
    messages: list[str]
    shots: list[ShotOut]
    frame: WorkingFrameOut
    bullet_diameter_in: float
    aim_candidates: list[AimCandidateOut] = []


class ShotIn(RequestModel):
    """A shot in a review payload; its `seq` is its position in the list."""

    x_in: Inches
    y_in: Inches
    origin: ShotOrigin
    moved_by_user: bool = False
    confidence: Annotated[float, Field(ge=0, le=1)] | None = None


class RejectionIn(RequestModel):
    """A rejected detection. The only accepted reason is `not_a_bullet_hole`; it must be explicit."""

    x_in: Inches
    y_in: Inches
    reason: RejectionReason


class ReviewRequest(RequestModel):
    """`PUT /api/groups/{id}/review`: the full reviewed state. `aim` may be null while still a draft."""

    aim: PlanePointIn | None = None
    shots: Annotated[list[ShotIn], Field(max_length=200)]
    rejections: Annotated[list[RejectionIn], Field(max_length=500)] = []
    shots_expected: ShotsExpected


# ---------------------------------------------------------------------------------------------
# Analysis: aggregate and search
# ---------------------------------------------------------------------------------------------


class AggregateRequest(RequestModel):
    """`POST /api/aggregate`: finalized groups of one rifle and distance, unique ids."""

    group_ids: Annotated[list[PositiveId], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.group_ids)) != len(self.group_ids):
            raise ValueError("group_ids must be unique")
        return self


class AggregateShotOut(ResponseModel):
    """One shot of the overlay, aligned on its own group's aim point."""

    group_id: int
    dx_in: float
    dy_in: float
    origin: ShotOrigin


class AggregateOut(ResponseModel):
    """Combined statistics of the selected groups plus the overlay points."""

    group_count: int
    n: int
    es_in: float
    es_moa: float
    center_dx_in: float
    center_dy_in: float
    center_dx_mil: float
    center_dy_mil: float
    includes_estimated: bool
    mixed_loads: bool
    warnings: list[str]
    shots: list[AggregateShotOut]
    es_label: str = ES_LABEL


class SaveAggregateRequest(AggregateRequest):
    """`POST /api/aggregates`: a name and at least two finalized groups of one load, rifle and distance."""

    name: Name
    group_ids: Annotated[list[PositiveId], Field(min_length=2, max_length=100)]


class AggregateListQuery(RequestModel):
    """Query string of `GET /api/aggregates`."""

    load_id: PositiveId | None = None


class SavedAggregateOut(AggregateOut):
    """A saved aggregate with its live statistics.

    `valid` is false when a member group was deleted or is no longer a finalized, unmasked group;
    the statistics then cover the members that remain and `invalid_reason` says why. `tier` is
    `tier30` for a valid aggregate of 30 or more shots. Only valid aggregates feed the comparison.
    """

    id: int
    public_id: str
    name: str
    rifle_id: int
    load_id: int
    distance_yd: int
    group_ids: list[int]
    created_at: str
    valid: bool
    invalid_reason: str | None = None
    tier: CompareTier | None = None


class SearchQuery(RequestModel):
    """Query string of `GET /api/search`."""

    q: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class SearchHitOut(ResponseModel):
    """One note search hit (`GET /api/search` returns a list)."""

    kind: SearchKind
    id: int
    snippet: str


# ---------------------------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------------------------


class ComparisonQuery(RequestModel):
    """Query string of `GET /api/rifles/{id}/comparison`; `load_ids` also parses "1,2,3"."""

    load_ids: Annotated[list[PositiveId], Field(min_length=1, max_length=20)]
    position: Position | None = None
    suppressor: bool | None = None

    @field_validator("load_ids", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [part for part in (p.strip() for p in value.split(",")) if part]
        return value

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.load_ids)) != len(self.load_ids):
            raise ValueError("load_ids must be unique")
        return self


ShareLayer = Literal["photo", "spread", "thing_hitter", "poi", "correction", "numbers"]
SHARE_DEFAULT_LAYERS: tuple[ShareLayer, ...] = ("photo", "spread", "numbers")


class ShareQuery(RequestModel):
    """Query string of `GET /api/groups/{id}/share.png`.

    `layers` is a comma list of `ShareLayer` names (repeats dropped, order kept); absent means
    `SHARE_DEFAULT_LAYERS`, empty means shots only.
    """

    layers: Annotated[list[ShareLayer], Field(max_length=6)] = Field(
        default_factory=lambda: list(SHARE_DEFAULT_LAYERS)
    )

    @field_validator("layers", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            if len(value) > 200:
                raise ValueError("layers is too long")
            return list(dict.fromkeys(p for p in (s.strip() for s in value.split(",")) if p))
        return value


class TierSummaryOut(ResponseModel):
    """Like-with-like summary: tier10 is 10-19 shot groups, tier30 is groups or aggregates of 30+."""

    rounds: int
    groups: int
    mean_es_moa: float | None
    worst_es_moa: float | None


class CompareGroupOut(ResponseModel):
    """A group shown under its load; `tier` is null for sizes in no tier (for example 20-29 shots)."""

    group_id: int
    shot_date: dt.date
    shot_time: str | None = None
    distance_yd: int
    position: Position
    suppressor: bool
    shots: int
    es_moa: float
    includes_estimated: bool
    tier: CompareTier | None


class CompareAggregateOut(ResponseModel):
    """A saved aggregate of the load; `counted` is true when it is a tier30 entry, else `note` says why not."""

    aggregate_id: int
    name: str
    group_ids: list[int]
    distance_yd: int
    shots: int
    es_moa: float
    includes_estimated: bool
    tier: CompareTier | None
    counted: bool
    note: str | None


class PoiShiftOut(ResponseModel):
    """POI shift versus the zero load in mil (right and up positive), averaged over shared days."""

    right_mil: float
    up_mil: float
    text: str


class LoadComparisonOut(ResponseModel):
    """One load's tiers, status, verdict, POI shift, and groups."""

    load_id: int
    label: str
    is_zero_load: bool
    tier10: TierSummaryOut
    tier30: TierSummaryOut
    status: CompareStatus
    precision_verdict: str | None
    poi_shift: PoiShiftOut | None
    poi_shift_note: str | None
    groups: list[CompareGroupOut]
    aggregates: list[CompareAggregateOut]


class PairComparisonOut(ResponseModel):
    """Verdict for loads A and B, per tier; a tier missing on either side is `insufficient_data`."""

    a_load_id: int
    b_load_id: int
    tier10: PairVerdict
    tier30: PairVerdict


class ComparisonFiltersOut(ResponseModel):
    """Filters that were applied."""

    position: Position | None
    suppressor: bool | None


class ComparisonOut(ResponseModel):
    """`GET /api/rifles/{id}/comparison`."""

    rifle_id: int
    filters: ComparisonFiltersOut
    loads: list[LoadComparisonOut]
    pairs: list[PairComparisonOut]
    conditions_warning: str | None
    zero_to_hunting_load_advice: str


# ---------------------------------------------------------------------------------------------
# Zeroing
# ---------------------------------------------------------------------------------------------


class ZeroCreate(RequestModel):
    """`POST /api/zeros`: starts a zero for a rifle, load, and optional lot."""

    rifle_id: PositiveId
    load_id: PositiveId
    lot_code: LotCode | None = None
    mode: ZeroMode


class ZeroLinkGroup(RequestModel):
    """`POST /api/zeros/{id}/groups`."""

    group_id: PositiveId


class ZeroDialed(RequestModel):
    """`POST /api/zeros/{id}/dialed`: the correction the user dialed, in whole 0.1 mil clicks."""

    up_mil: Mil
    right_mil: Mil

    @model_validator(mode="after")
    def _whole_clicks(self) -> Self:
        for name in ("up_mil", "right_mil"):
            tenths = getattr(self, name) * 10
            if not math.isclose(tenths, round(tenths), abs_tol=1e-6):
                raise ValueError(f"{name} must be a multiple of 0.1 mil")
        return self


class ZeroDot(RequestModel):
    """`POST /api/zeros/{id}/dot`."""

    dot_size_in: Annotated[float, Field(gt=0, le=10)]


class ZeroCheckCreate(RequestModel):
    """`POST /api/zeros/{id}/checks`: the zero-check group (1 or 2 shots)."""

    group_id: PositiveId


class ZeroCheckGroupCreate(RequestModel):
    """`POST /api/zeros/{id}/check-groups`: a zero-check group (1 or 2 shots); its distance is 100 yd.

    `shot_date` may be omitted when the photo carries its own date (as for `GroupCreate`).
    """

    lot_code: LotCode
    shots_expected: Annotated[int, Field(ge=1, le=2)]
    notes: Notes = ""
    shot_date: dt.date | None = None
    shot_time: ShotTime | None = None
    position: Position
    suppressor: bool
    suppressor_id: PositiveId | None = None
    photo_id: PositiveId | None = None

    @model_validator(mode="after")
    def _context_rules(self) -> Self:
        _check_suppressor(self.suppressor, self.suppressor_id)
        if self.shot_time is not None and self.shot_date is None:
            raise ValueError("shot_time needs shot_date")
        return self


class CorrectionOut(ResponseModel):
    """Signed correction in mil (up and right positive) with its direction-word text."""

    up_mil: float
    right_mil: float
    text: str


class SizeOnlyStatsOut(ResponseModel):
    """Group size without any point-of-impact field.

    The only stats a `shoot_more` or `invalid` advice may carry, and what `GET /api/groups/{id}/stats`
    returns for a group whose mean POI must not be shown (short zeroing groups, B-09).
    """

    model_config = ConfigDict(from_attributes=True, allow_inf_nan=False, extra="forbid")

    n: int
    es_in: float
    es_moa: float
    includes_estimated: bool
    es_label: str = ES_LABEL


_DIRECTION = r"(?:up|down|left|right|high|low)"
_BENIGN_COUNT_TEXT = re.compile(
    r"\b(?:shots?|rounds?)\s+left\b|\d+\s+left\s+to\s+(?:fire|shoot)\b", re.I
)
_CORRECTION_TEXT_PATTERNS: dict[str, re.Pattern[str]] = {
    "decimal number": re.compile(r"\d*\.\d+"),
    "unit or dial wording": re.compile(r"\b(?:dial\w*|click\w*|mils?|mrads?|moa)\b", re.I),
    "come up/down": re.compile(r"\bcome\s+(?:up|down)\b", re.I),
    "direction before number": re.compile(rf"\b{_DIRECTION}\b\W{{0,3}}\d", re.I),
    "number before direction": re.compile(rf"\d\W{{0,3}}\b{_DIRECTION}\b", re.I),
}
_NO_NUMBER_KINDS = (AdviceKind.SHOOT_MORE, AdviceKind.INVALID)


def _correction_text_reason(message: str) -> str | None:
    """Name of the first correction-text pattern found in `message`, or None if it is plain."""
    text = _BENIGN_COUNT_TEXT.sub(" ", message)
    for reason, pattern in _CORRECTION_TEXT_PATTERNS.items():
        if pattern.search(text):
            return reason
    return None


class AdviceOut(ResponseModel):
    """Advice from the domain.

    `shoot_more` and `invalid` never carry a mil POI, mil offset, correction, or direction text in any
    field: `correction` must be null, `stats` may only be `SizeOnlyStatsOut`, and `message` must not
    contain a decimal number, a unit or dial/click wording, "come up/down", or a number next to a
    direction word (integer shot counts such as "6 left to fire" are fine).
    """

    kind: AdviceKind
    message: str
    correction: CorrectionOut | None = None
    stats: GroupStatsOut | SizeOnlyStatsOut | None = None

    @model_validator(mode="after")
    def _no_number_on_short_groups(self) -> Self:
        if self.kind in _NO_NUMBER_KINDS:
            name = self.kind.value
            if self.correction is not None:
                raise ValueError(f"{name} advice must not carry a correction")
            if isinstance(self.stats, GroupStatsOut):
                raise ValueError(f"{name} advice must not carry mean POI stats")
            reason = _correction_text_reason(self.message)
            if reason:
                raise ValueError(f"{name} advice message must not carry correction text ({reason})")
        if self.kind is AdviceKind.CORRECTION and self.correction is None:
            raise ValueError("correction advice requires a correction")
        return self


class ZeroGroupOut(ResponseModel):
    """A group linked to a zero."""

    group_id: int
    purpose: GroupPurpose
    status: GroupStatus
    shots: int


class ZeroCheckOut(ResponseModel):
    """A zero check; `route` lists the follow-ups for `possible_lost_zero` (mount first, then scope)."""

    id: int
    zero_id: int
    group_id: int
    result: ZeroCheckOutcome
    message: str = ""
    per_shot_inside: list[bool] = []
    route: list[str] = []
    created_at: str


class ZeroOut(ResponseModel):
    """A zero and its stepper state; `advice` is the latest domain advice, if any."""

    id: int
    rifle_id: int
    load_id: int
    lot_id: int | None
    mode: ZeroMode
    state: ZeroState
    dialed_up_mil: float | None
    dialed_right_mil: float | None
    zero_es_in: float | None
    zero_shot_count: int | None
    dot_size_in: float | None
    suggested_dot_in: float | None = None
    completed_at: str | None
    created_at: str
    updated_at: str
    groups: list[ZeroGroupOut] = []
    checks: list[ZeroCheckOut] = []
    advice: AdviceOut | None = None


# ---------------------------------------------------------------------------------------------
# Dope
# ---------------------------------------------------------------------------------------------


class DopeRowOut(ResponseModel):
    """One dope row; `centered_elevation_mil` is `elevation_dialed_mil - center_dy_mil`."""

    group_id: int
    shot_date: dt.date
    shot_time: str | None = None
    distance_yd: int
    elevation_dialed_mil: float
    center_dy_mil: float
    centered_elevation_mil: float
    shots: int
    includes_estimated: bool


class DopeOut(ResponseModel):
    """`GET /api/loads/{id}/dope`."""

    load_id: int
    rows: list[DopeRowOut]


# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------


class ShareDefaults(RequestModel):
    """`PUT /api/settings/share-defaults`: which share-card layers start checked.

    All six layers are required and each must be a JSON boolean (no 0/1 or text).
    """

    photo: StrictBool
    spread: StrictBool
    thing_hitter: StrictBool
    poi: StrictBool
    correction: StrictBool
    numbers: StrictBool


class ShareDefaultsOut(ResponseModel):
    """The saved share-card defaults (the built-in ones when nothing was saved)."""

    photo: bool
    spread: bool
    thing_hitter: bool
    poi: bool
    correction: bool
    numbers: bool


# ---------------------------------------------------------------------------------------------
# Route registry
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteSchema:
    """Models of one route: `request` body, `query` string, and `response` annotation (None = no JSON)."""

    request: type[BaseModel] | None = None
    response: Any = None
    query: type[BaseModel] | None = None


ROUTE_SCHEMAS: dict[tuple[str, str], RouteSchema] = {
    ("GET", "/healthz"): RouteSchema(response=HealthOut),
    ("GET", "/api/calibers"): RouteSchema(response=list[CaliberOut]),
    ("GET", "/api/rifles"): RouteSchema(response=list[RifleOut]),
    ("POST", "/api/rifles"): RouteSchema(request=RifleCreate, response=RifleOut),
    ("GET", "/api/rifles/{id}"): RouteSchema(response=RifleDetailOut),
    ("PATCH", "/api/rifles/{id}"): RouteSchema(request=RifleUpdate, response=RifleOut),
    ("PUT", "/api/rifles/{id}/zero-load"): RouteSchema(request=ZeroLoadSet, response=RifleOut),
    ("GET", "/api/rifles/{id}/loads"): RouteSchema(response=list[LoadOut]),
    ("POST", "/api/rifles/{id}/loads"): RouteSchema(request=LoadCreate, response=LoadOut),
    ("PATCH", "/api/loads/{id}"): RouteSchema(request=LoadUpdate, response=LoadOut),
    ("GET", "/api/loads/{id}/lots"): RouteSchema(response=list[LotOut]),
    ("POST", "/api/loads/{id}/lots"): RouteSchema(request=LotCreate, response=LotOut),
    ("GET", "/api/suppressors"): RouteSchema(response=list[SuppressorOut]),
    ("POST", "/api/suppressors"): RouteSchema(request=SuppressorCreate, response=SuppressorOut),
    ("GET", "/api/rifles/{id}/group-defaults"): RouteSchema(response=GroupDefaultsOut),
    ("GET", "/api/groups"): RouteSchema(query=GroupListQuery, response=list[GroupOut]),
    ("POST", "/api/groups"): RouteSchema(request=GroupCreate, response=GroupOut),
    ("POST", "/api/photos"): RouteSchema(response=PhotoOut),
    ("GET", "/api/photos/{id}/image"): RouteSchema(query=ImageQuery),
    ("POST", "/api/photos/{id}/reference/propose"): RouteSchema(
        request=ReferenceProposeRequest, response=ReferenceProposeOut
    ),
    ("POST", "/api/photos/{id}/reference/confirm"): RouteSchema(
        request=ReferenceConfirmRequest, response=ReferenceConfirmOut
    ),
    ("POST", "/api/photos/{id}/reference/reset"): RouteSchema(response=ReferenceResetOut),
    ("POST", "/api/photos/{id}/snap-aim"): RouteSchema(request=SnapAimRequest, response=SnapAimOut),
    ("POST", "/api/groups/{id}/detect"): RouteSchema(request=DetectRequest, response=DetectOut),
    ("POST", "/api/groups/{id}/fill-shortfall"): RouteSchema(response=DetectOut),
    ("PUT", "/api/groups/{id}/review"): RouteSchema(request=ReviewRequest, response=GroupDetailOut),
    ("POST", "/api/groups/{id}/finalize"): RouteSchema(response=GroupDetailOut),
    ("GET", "/api/groups/{id}"): RouteSchema(response=GroupDetailOut),
    ("GET", "/api/groups/{id}/stats"): RouteSchema(response=GroupStatsOut | SizeOnlyStatsOut),
    ("PATCH", "/api/groups/{id}"): RouteSchema(request=GroupUpdate, response=GroupOut),
    ("POST", "/api/aggregate"): RouteSchema(request=AggregateRequest, response=AggregateOut),
    ("POST", "/api/aggregates"): RouteSchema(
        request=SaveAggregateRequest, response=SavedAggregateOut
    ),
    ("GET", "/api/aggregates"): RouteSchema(
        query=AggregateListQuery, response=list[SavedAggregateOut]
    ),
    ("DELETE", "/api/aggregates/{id}"): RouteSchema(),
    ("GET", "/api/search"): RouteSchema(query=SearchQuery, response=list[SearchHitOut]),
    ("GET", "/api/rifles/{id}/comparison"): RouteSchema(
        query=ComparisonQuery, response=ComparisonOut
    ),
    ("POST", "/api/zeros"): RouteSchema(request=ZeroCreate, response=ZeroOut),
    ("GET", "/api/zeros/{id}"): RouteSchema(response=ZeroOut),
    ("POST", "/api/zeros/{id}/groups"): RouteSchema(request=ZeroLinkGroup, response=ZeroOut),
    ("POST", "/api/zeros/{id}/dialed"): RouteSchema(request=ZeroDialed, response=ZeroOut),
    ("POST", "/api/zeros/{id}/dot"): RouteSchema(request=ZeroDot, response=ZeroOut),
    ("POST", "/api/zeros/{id}/checks"): RouteSchema(request=ZeroCheckCreate, response=ZeroCheckOut),
    ("POST", "/api/zeros/{id}/check-groups"): RouteSchema(
        request=ZeroCheckGroupCreate, response=GroupOut
    ),
    ("DELETE", "/api/zeros/{id}/groups/{gid}"): RouteSchema(response=ZeroOut),
    ("GET", "/api/loads/{id}/dope"): RouteSchema(response=DopeOut),
    ("GET", "/api/groups/{id}/share.png"): RouteSchema(query=ShareQuery),
    ("GET", "/api/settings/share-defaults"): RouteSchema(response=ShareDefaultsOut),
    ("PUT", "/api/settings/share-defaults"): RouteSchema(
        request=ShareDefaults, response=ShareDefaultsOut
    ),
}
