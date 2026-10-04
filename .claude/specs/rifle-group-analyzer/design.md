# Design: Rifle Group Analyzer

> Slug: `rifle-group-analyzer` · Companion to `spec.md`. All paths are absolute under project root `/Users/calebschwarz/Documents/Hunting` (written `<root>` below).

## 1. Architecture

```
Browser (localhost only)
  templates (Jinja2, server-rendered pages)  +  static/js ES modules (no build step)
      │  fetch JSON /api/*  (same-origin, Content-Type: application/json | multipart)
      ▼
FastAPI app  (rga.web.app:create_app)  — Uvicorn bound to 127.0.0.1:<RGA_PORT>
  middleware: TrustedHost(127.0.0.1, localhost) → OriginGuard → upload size limit
  routers: auto-discovered from rga.web.routes.* (each module exports `router`)
      │
      ▼
services (rga.services.*)  — orchestration, transactions, invariants (offset recompute, finalize guards)
      │                      │
      ▼                      ▼
domain (pure)            vision (OpenCV, numpy)          db (stdlib sqlite3)
 units, stats,            io → reference_{circle,grid}    connection, migrations,
 zeroing, comparison,     → transform → detect → ragged   rows, repo_* modules
 calibers, summaries      → pipeline
                                                           SQLite file + photo store
                                                           under RGA_DATA_DIR
```

Dependency rule: `domain` imports nothing from the other layers. `vision` imports only `domain.types`. `db` imports only `domain.types`. `services` import `domain`, `vision`, and `db`. `web` imports `services`, `web.schemas`, `domain.types`, and `config` only, never `db` or `vision` directly. The one exception is `web/deps.py → db.connection`. A unit test enforces this by checking import graphs (task 1.2 writes the test; it stays green as modules land).

## 2. Repository layout (file ownership is assigned in `tasks.md`)

```
<root>/
  .python-version              # 3.14
  .gitignore                   # .venv/ data/ __pycache__/ .pytest_cache/ test-results/ *.egg-info
  pyproject.toml               # project metadata, pytest + ruff + bandit config
  requirements.in              # runtime direct deps (ranges)
  requirements-dev.in          # -c requirements.lock + dev deps
  requirements.lock            # pip-compile --generate-hashes
  requirements-dev.lock        # pip-compile --generate-hashes
  Makefile                     # bootstrap, test, accuracy, smoke, e2e, scan, run, lock
  README.md                    # Phase 4 (documentation skill)
  scripts/
    bootstrap.sh               # venv + locked install + playwright chromium
    lock.sh                    # regenerate both lock files
    run.sh                     # start server (loopback only)
    smoke.sh                   # T3 local smoke: start → checks → stop
    scan.sh                    # bandit + pip-audit → spec folder
  src/rga/
    __init__.py  __main__.py   # `python -m rga` → uvicorn with config
    config.py
    domain/   __init__.py types.py units.py calibers.py stats.py zeroing.py comparison.py summaries.py
    vision/   __init__.py io.py transform.py reference_circle.py reference_grid.py detect.py ragged.py pipeline.py
    db/       __init__.py connection.py rows.py migrations/001_initial.sql
              repo_rifles.py repo_groups.py repo_zeroing.py repo_dope.py repo_aggregates.py
    services/ __init__.py groups.py group_records.py rifles.py photos.py review.py analysis.py compare.py zeroing.py dope.py share.py
    web/      __init__.py app.py security.py deps.py schemas.py
              routes/__init__.py dashboard.py rifles.py groups.py groups_page.py settings.py share.py photos.py review.py analysis.py compare.py zeroing.py dope.py
              templates/base.html index.html rifles.html rifle_detail.html session.html reference.html review.html
                        group.html analysis.html compare.html zeroing.html dope.html
              static/css/app.css
              static/js/api.js rifles.js session.js reference.js review.js svg_editor.js analysis.js compare.js zeroing.js dope.js
  tools/
    eval_detection.py          # spike / accuracy harness (synthetic + real)
    eval_real_photos.py        # real-photo gate runner (user's 10–20 photos)
  tests/
    __init__.py  conftest.py   # conftest owned by scaffold; NOT edited later
    support/  __init__.py db.py live_server.py seed.py
    synth/    __init__.py generate.py catalog.json
    unit/  integration/  smoke/  e2e/
    real/README.md             # annotation format for the user's photos (photos themselves in data/real_photos/, gitignored)
  data/                        # runtime data dir (gitignored): rga.sqlite3, photos/<sha256>.<ext>, working/<photo_id>.png
```

**Shared-file rule:** `tests/conftest.py`, `pyproject.toml`, and `Makefile` belong to the scaffold task (1.1). Later tasks import fixtures explicitly (`from tests.support.db import migrated_conn`) and never edit these three files. If a later task needs a new dependency, it messages the lead, who routes a lock update to `devops-1`.

## 3. Coordinate conventions (the #1 seam — read before writing any vision/domain code)

| Space | Origin | Axes | Units | Where it lives |
|---|---|---|---|---|
| Oriented image px | top-left of EXIF-corrected image | x right, **y down** | px | reference detection, manual reference clicks |
| Working image px | top-left of rectified working image | x right, **y down** | px | detection, review editor SVG |
| Plane (`PlanePoint`) | top-left of working image | x right, **y down** | inches = working px / `ppi` | stored on shots and aim points |
| Offset (`Offset`) | the group's aim point | x right, **y UP** | inches (and mil) | stats, zeroing, display |

- The single y-flip happens in `rga.domain.units.offset_from_aim(shot, aim)`, which returns `Offset(dx_in = shot.x - aim.x, dy_in = aim.y - shot.y)`. No other code flips y.
- A shot that is high and right of the aim point gives `dx > 0`, `dy > 0`. The correction is `DOWN dy_mil`, `LEFT dx_mil`. A contract test renders a high-right synthetic group and asserts the correction words end to end (T2).
- Angular conversions (small angle, as used on mil/MOA reticles):
  - `mil = in × 1000 / (yd × 36)`
  - `MOA = in / (1.0471975512 × yd / 100)`
- Rounding to 0.1 mil uses `decimal.Decimal.quantize(Decimal("0.1"), ROUND_HALF_UP)` on the absolute value with the sign restored (half away from zero). Python's built-in `round()` (banker's rounding) is forbidden for corrections, and a test proves 0.05 → 0.1 and −0.05 → −0.1.

## 4. Interface contracts (authored in Group 1; consumers in Groups 2–4 code against these exactly)

### 4.1 `rga.domain.types` (task 1.2)
```python
class ReferenceKind(StrEnum): GRID_1IN="grid_1in"; CIRCLE_1IN="circle_1in"; CIRCLE_1_5IN="circle_1_5in"; CIRCLE_3IN="circle_3in"
REFERENCE_SIZE_IN: dict[ReferenceKind, float]          # grid square / circle diameter
class ShotOrigin(StrEnum): DETECTED="detected"; SPLIT="split"; ESTIMATED="estimated"; MANUAL="manual"
class BlobKind(StrEnum): SINGLE="single"; SPLIT="split"; RAGGED="ragged"
class LoadKind(StrEnum): FACTORY="factory"; HANDLOAD="handload"
class Position(StrEnum): BENCH="bench"; PRONE_OFF_PACK="prone_off_pack"; KNEELING_SITTING="kneeling_sitting"; STANDING_FIELD="standing_field"
class GroupPurpose(StrEnum): GENERAL="general"; ZERO_STEP1="zero_step1"; ZERO_STEP2="zero_step2"; ZERO_CONFIRM="zero_confirm"; ZERO_CHECK="zero_check"
class GroupStatus(StrEnum): DRAFT="draft"; FINAL="final"
class ZeroState(StrEnum): STEP1="step1"; STEP2_INITIAL="step2_initial"; STEP2_CONFIRM="step2_confirm"; STEP3_DOT="step3_dot"; COMPLETE="complete"; SUPERSEDED="superseded"
class RejectionReason(StrEnum): NOT_A_BULLET_HOLE="not_a_bullet_hole"   # the ONLY member, by design (FR-3.5, D-14)
class AdviceKind(StrEnum): CORRECTION="correction"; NO_ADJUSTMENT="no_adjustment"; SHOOT_MORE="shoot_more"; TOUCH_UP_OK="touch_up_ok"; CONFIRM_AGAIN="confirm_again"; READY_FOR_DOT="ready_for_dot"; INVALID="invalid"

@dataclass(frozen=True) class PlanePoint: x_in: float; y_in: float
@dataclass(frozen=True) class Offset: dx_in: float; dy_in: float
@dataclass(frozen=True) class Shot: point: PlanePoint; origin: ShotOrigin; confidence: float | None = None; moved_by_user: bool = False
@dataclass(frozen=True) class WorkingFrame: ppi: float; width_px: int; height_px: int
@dataclass(frozen=True) class Blob: kind: BlobKind; contour_px: tuple[tuple[int, int], ...]; area_px: float; centers_px: tuple[tuple[float, float], ...]; confidence: float
@dataclass(frozen=True) class CircleCandidate: center_px: tuple[float, float]; axes_px: tuple[float, float]; angle_deg: float; score: float
@dataclass(frozen=True) class ReferenceResult: kind: ReferenceKind; method: Literal["auto","manual"]; h_px_to_plane: tuple[tuple[float, ...], ...]; axis_ratio: float | None; warnings: tuple[str, ...]
@dataclass(frozen=True) class GroupStats: n: int; es_in: float; es_moa: float; mean_dx_in: float; mean_dy_in: float; mean_dx_mil: float; mean_dy_mil: float; includes_estimated: bool
@dataclass(frozen=True) class Correction: up_mil: float; right_mil: float; text: str        # signed; text e.g. "Dial DOWN 0.2, LEFT 0.3" or "No adjustment"
@dataclass(frozen=True) class Advice: kind: AdviceKind; message: str; correction: Correction | None = None; stats: GroupStats | None = None
@dataclass(frozen=True) class ZeroCheckResult: passed: bool; per_shot_inside: tuple[bool, ...]; message: str
```

### 4.2 `rga.domain.units` (task 1.2)
`in_to_mil(inches, yd)`, `mil_to_in(mil, yd)`, `in_to_moa(inches, yd)`, `moa_to_in(moa, yd)`, `round_tenth(x) -> float` (half away from zero), `offset_from_aim(shot: PlanePoint, aim: PlanePoint) -> Offset`, `ZERO_DISTANCE_YD = 100`, `CLICK_MIL = 0.1`.

### 4.3 `rga.domain.calibers` (task 1.2)
`CALIBERS: dict[str, float]` maps cartridge name to bullet diameter in inches. It covers at least .223 Rem, 5.56, 22 Creedmoor, 22-250, 6mm ARC, 6mm Creedmoor, .243 Win, 6UM, 6.5 Creedmoor, 6.5 PRC, .270 Win, 7mm PRC, 7mm Rem Mag, .308 Win, .300 Win Mag, .300 PRC, and .338. `diameter_for(cartridge: str) -> float | None`.

### 4.4 Domain functions (Group 2)
```python
# stats.py
def extreme_spread_in(offsets: Sequence[Offset]) -> float                 # 0.0 for n<2
def group_stats(offsets: Sequence[Offset], origins: Sequence[ShotOrigin], distance_yd: float) -> GroupStats
@dataclass(frozen=True) class AggregateStats: n: int; es_in: float; es_moa: float; center_dx_in: float; center_dy_in: float; center_dx_mil: float; center_dy_mil: float; includes_estimated: bool; mixed_loads: bool
def aggregate(groups: Sequence[AggregateInput]) -> AggregateStats         # AggregateInput(offsets, origins, distance_yd, load_id); ValueError on mixed distance
# zeroing.py
STANDARD_DOTS_IN = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
def correction_for(mean_dx_mil: float, mean_dy_mil: float) -> Correction
def step1_advice(offsets: Sequence[Offset]) -> Advice                      # 1–3 → CORRECTION/NO_ADJUSTMENT; ≥4 → SHOOT_MORE; 0 → INVALID
def step2_initial_advice(offsets: Sequence[Offset], origins) -> Advice     # ≥10 → CORRECTION/NO_ADJUSTMENT; else SHOOT_MORE (never a number)
def step2_confirm_advice(initial: Sequence[Offset], dialed: Correction, confirms: Sequence[Sequence[Offset]], origins_all) -> Advice   # P-02 REVISED: center-on-center overlay (each group translated to its own centroid) for ES; acceptance on the confirm group's centroid; `dialed` is stored/displayed only, never applied to the overlay; TOUCH_UP_OK | CONFIRM_AGAIN | READY_FOR_DOT; stats = overlay stats
def suggest_dot(zero_es_in: float, bullet_diameter_in: float) -> float     # P-01
def zero_check(offsets: Sequence[Offset], dot_size_in: float, bullet_diameter_in: float) -> ZeroCheckResult   # ValueError unless 1–2 shots
# comparison.py
NORMAL_GROUP_VARIATION = 0.30; MIN_ROUNDS = 10; HIGH_CONFIDENCE_ROUNDS = 20
@dataclass(frozen=True) class CompareGroup: es_moa: float; n: int; position: Position; suppressor: bool; includes_estimated: bool
# P-13 REVISED (like with like): tier10 = groups with 10–19 shots; tier30 = groups/aggregates with ≥30 shots; 20–29 shown, in no tier
@dataclass(frozen=True) class TierSummary: rounds: int; groups: int; mean_es_moa: float | None; worst_es_moa: float | None
@dataclass(frozen=True) class LoadSummary: load_id: int; tier10: TierSummary; tier30: TierSummary; status: Literal["insufficient_data","ok","high_confidence"]; precision_verdict: str | None   # status/verdict from tier10; high_confidence = tier10.rounds ≥ 20
def summarize_load(load_id: int, groups: Sequence[CompareGroup]) -> LoadSummary
def compare_pair(a: LoadSummary, b: LoadSummary, tier: Literal["tier10","tier30"] = "tier10") -> Literal["insufficient_data","no_real_difference","a_smaller","b_smaller"]
def precision_verdict(es_moa: float) -> str
def poi_shift_mil(pairs: Sequence[tuple[tuple[float, float], tuple[float, float]]]) -> tuple[float, float] | None   # [(load_mean(dx,dy), zero_mean(dx,dy)) per shared session]
def conditions_warning(a: Sequence[CompareGroup], b: Sequence[CompareGroup]) -> str | None
ZERO_TO_HUNTING_LOAD_ADVICE: str
# summaries.py
@dataclass(frozen=True) class SummaryGroup: public_id: str; load_label: str; lot_code: str; shots: int; stats: GroupStats; notes: str
@dataclass(frozen=True) class SummarySession: public_id: str; date: str; rifle_label: str; distance_yd: int; position: Position; suppressor: bool; notes: str
def group_summary(session: SummarySession, group: SummaryGroup) -> str
def session_summary(session: SummarySession, groups: Sequence[SummaryGroup]) -> str
```

### 4.5 Vision functions (Groups 1–2)
```python
# transform.py (task 1.4)
def apply_h(h: np.ndarray, pts: np.ndarray) -> np.ndarray
def similarity_from_circle(c: CircleCandidate, diameter_in: float) -> np.ndarray          # oriented px → plane in (isotropic scale)
def homography_from_points(src_px: np.ndarray, dst_in: np.ndarray) -> np.ndarray          # ≥4 pts; RANSAC if >4
def build_working(img: np.ndarray, h_px_to_plane: np.ndarray, ppi_cap: float = 200, max_side: int = 4000) -> tuple[np.ndarray, WorkingFrame, np.ndarray]   # returns (working_img, frame, h_px_to_working)
def working_px_to_plane(pts, frame) -> list[PlanePoint]; def plane_to_working_px(pts: Sequence[PlanePoint], frame) -> np.ndarray
# io.py (task 2.1)
MAX_UPLOAD_BYTES = 40 * 1024 * 1024; MAX_PIXELS = 60_000_000
class ImageRejected(ValueError): ...
def load_oriented(data: bytes) -> tuple[np.ndarray, str]                  # (BGR uint8, normalized ext); handles JPEG/PNG/HEIC + EXIF orientation; raises ImageRejected
def sha256_hex(data: bytes) -> str
# reference_circle.py (task 2.2)
def find_circle_candidates(img: np.ndarray, kind: ReferenceKind, max_candidates: int = 5) -> list[CircleCandidate]
def circle_from_edge_points(points_px: Sequence[tuple[float, float]]) -> CircleCandidate   # 3 → circle, ≥5 → ellipse fit
def reference_from_circle(c: CircleCandidate, kind: ReferenceKind, method: str) -> ReferenceResult   # warning if axis ratio < 0.98
def refine_circle_near(img: np.ndarray, point_px: tuple[float, float], max_radius_px: float) -> CircleCandidate | None   # aim snap
# reference_grid.py (task 2.3)
@dataclass(frozen=True) class GridCandidate: intersections_px: np.ndarray; lattice_idx: np.ndarray; h_px_to_plane: np.ndarray; rms_residual_in: float; score: float
def find_grid(img: np.ndarray) -> GridCandidate | None
def grid_from_manual(corners_px: Sequence[tuple[float, float]], squares_x: int, squares_y: int) -> GridCandidate   # TL,TR,BR,BL
def reference_from_grid(g: GridCandidate, method: str) -> ReferenceResult
# detect.py (task 2.4)
@dataclass(frozen=True) class DetectParams: ...                          # spike tunes; defaults are the shipped values
def detect_holes(working: np.ndarray, frame: WorkingFrame, bullet_diameter_in: float, roi_px: tuple[int, int, int, int] | None = None, params: DetectParams | None = None) -> list[Blob]
# ragged.py (task 2.5)
@dataclass(frozen=True) class ResolveResult: shots_px: tuple[tuple[float, float, ShotOrigin, float], ...]; status: Literal["ok","too_many","unassigned_ragged"]; messages: tuple[str, ...]
def resolve_shots(blobs: Sequence[Blob], shots_expected: int, bullet_diameter_px: float) -> ResolveResult
# pipeline.py (task 2.16)
def propose_reference(img, kind) -> list[ReferenceResult | GridCandidate-derived ReferenceResult]   # ranked candidates, never auto-confirmed
def analyze_group(working, frame, bullet_diameter_in, shots_expected, roi_px) -> tuple[list[Blob], ResolveResult]
```

### 4.6 DB rows and repos (Groups 1–2)
`rga.db.rows` (task 1.3) defines frozen dataclasses `RifleRow`, `LoadRow`, `LotRow`, `SessionRow`, `PhotoRow`, `GroupRow`, `ShotRow`, `RejectionRow`, `ZeroRow`, `ZeroCheckRow`, `DopeRow`, which mirror the DDL columns 1:1. Every repo function takes `conn: sqlite3.Connection` first and does **not** commit. Services own transactions (`with conn:`).

| Module | Functions (minimum) |
|---|---|
| `repo_rifles` | `create_rifle`, `get_rifle`, `list_rifles`, `update_rifle`, `set_zero_load`, `create_load`, `get_load`, `list_loads(rifle_id)`, `update_load`, `get_or_create_lot(load_id, code)`, `list_lots(load_id)` |
| `repo_groups` (was repo_sessions; sessions removed, B-66/B-67) | `create_photo`, `get_photo`, `get_photo_by_sha`, `update_photo_reference`, `set_photo_taken_at`, `create_group` (with per-group context), `get_group`, `list_groups(load_id=None, rifle_id=None, status=None, …filters, newest_first)`, `latest_group`, `update_group`, `replace_shots`, `list_shots`, `add_rejection`, `list_rejections`, `search_notes`, `first_group_of_lot`, `load_has_other_lot_groups`, suppressors |
| `repo_zeroing` | `create_zero`, `get_zero`, `active_zero(rifle_id, load_id)`, `update_zero`, `supersede_zeros(rifle_id, load_id, except_id)`, `link_group(zero_id, group_id)`, `create_zero_check`, `list_zero_checks(zero_id)` |
| `repo_dope` | `dope_rows(load_id) -> list[DopeRow]` (query over finalized groups ⨝ sessions with elevation set) |

### 4.7 HTTP API (Group 3; pydantic models in `rga.web.schemas`, task 1.7)
All JSON lives under `/api`. Errors use `{"detail": str, "code": str}` with 4xx. Pages are HTML under `/`.

| Method & path | Slice | Purpose |
|---|---|---|
| `GET /healthz` | app (1.6) | `{status:"ok", schema_version:int}` |
| `GET /api/calibers` | rifles | caliber table |
| `GET,POST /api/rifles` · `GET,PATCH /api/rifles/{id}` · `PUT /api/rifles/{id}/zero-load` | rifles | FR-1.1 |
| `GET,POST /api/rifles/{id}/loads` · `PATCH /api/loads/{id}` · `GET,POST /api/loads/{id}/lots` | rifles | FR-1.2/1.3/1.5 |
| `POST /api/groups` · `GET /api/rifles/{id}/group-defaults` | groups | FR-2, B-66: every group carries its own context. Create takes load, lot code, shots_expected, optional shot_date/shot_time/utc_offset (the server takes the date from the photo's `taken_at` when `shot_date` is omitted and sets `shot_at_source` to photo or manual), distance_yd, position, suppressor, optional suppressor_id, the four weather fields with weather_skipped, elevation, purpose (general only), zero_id (null only), optional photo_id and notes. Defaults return the rifle's latest group's load, lot, shots, date, distance, position, suppressor and weather (distance, position and suppressor fall back to the latest group of any rifle), all null when there is none |
| `POST /api/photos` (multipart) · `GET /api/photos/{id}/image?variant=oriented\|working` | photos | FR-3.1; dedupe by sha256 |
| `POST /api/photos/{id}/reference/propose` `{kind}` · `POST /api/photos/{id}/reference/confirm` `{kind, candidate_index? , manual?}` | photos | FR-3.2; confirm builds the working image |
| `POST /api/photos/{id}/snap-aim` `{x_px,y_px}` | review | aim snap |
| `POST /api/photos/{id}/reference/reset` (no body) | photos | B-79: clears the photo's confirmed reference and, in one transaction, the shots, rejections, aim and region of its draft groups; 409 `reference_in_use` when any group on the photo is final. Returns the unconfirmed photo and `cleared_groups` (draft groups that had shots or an aim). The review page's "Change scale reference" button (above the shot counter) reads `reference_change` from the page state: `direct` (nothing saved, go to the reference page), `confirm` (an inline Continue/Cancel panel, then this route, then the reference page) or `blocked` (a finalized group uses the scale) |
| `POST /api/groups/{id}/detect` `{roi_px?}` · `POST /api/groups/{id}/fill-shortfall` · `PUT /api/groups/{id}/review` `{aim, shots[], rejections[], shots_expected}` · `POST /api/groups/{id}/finalize` | review | FR-3.3–3.7 |
| `GET /api/groups?load_id=&rifle_id=&status=` · `GET /api/groups/{id}` · `GET /api/groups/{id}/stats` · `PATCH /api/groups/{id}` (the context fields included; a distance change recomputes the offsets and is refused while the group is linked to a zero) · `POST /api/aggregate` `{group_ids}` · `GET /api/search?q=` | analysis | FR-4, FR-2.4; the list route (B-06) feeds the /analysis group picker and returns a bare array |
| `GET /api/rifles/{id}/comparison?load_ids=&position=&suppressor=` | compare | FR-5 |
| `POST /api/zeros` · `GET /api/zeros/{id}` · `POST /api/zeros/{id}/groups` `{group_id}` · `POST /api/zeros/{id}/dialed` `{up_mil,right_mil}` · `POST /api/zeros/{id}/dot` `{dot_size_in}` · `POST /api/zeros/{id}/checks` `{group_id}` · `DELETE /api/zeros/{id}/groups/{gid}` (detach, B-26/B-37) · `POST /api/zeros/{id}/check-groups` `{lot_code, shots_expected, notes, shot_date?, shot_time?, position, suppressor, suppressor_id?, photo_id?}` (creates a masked zero_check group at 100 yd, B-50) | zeroing | FR-6 |
| `POST /api/aggregates` `{name, group_ids}` · `GET /api/aggregates?load_id=` · `DELETE /api/aggregates/{id}` (saved aggregates feed tier30, B-44) | analysis | FR-5, P-13 |
| `GET /api/suppressors` · `POST /api/suppressors` `{name}` (named suppressors, B-65) | groups | FR-2 |
| `GET /api/groups/{id}/share.png?layers=photo,spread,thing_hitter,poi,correction` (share card image, PNG, B-65) | share | FR-5 |
| `GET /api/loads/{id}/dope` | dope | FR-7 |
| `GET /api/settings/share-defaults` · `PUT /api/settings/share-defaults` `{photo, spread, thing_hitter, poi, correction, numbers}` (the share card's starting checkboxes, the first stored setting, B-77; all six are required JSON booleans; no saved row means photo, spread and numbers on and the rest off) | settings | FR-5 |

Pages: `/`, `/rifles`, `/rifles/{id}`, `/photos/{id}/reference?group_id=`, `/groups/{id}/review?return_to=`, `/groups/{id}`, `/analysis`, `/compare?rifle_id=`, `/zeroing/{zero_id}`, `/dope?load_id=`.
- `return_to` must be a same-site relative path starting with `/` and not `//`. It is validated server-side (open-redirect guard).
- Navigation contract: reference confirm → review page → finalize → `return_to` or `/groups/{id}`.

`static/js/api.js` exports `apiGet(path)`, `apiSend(method, path, body)`, and `apiUpload(path, file)`. Every page script uses these and never calls `fetch` directly. That lets the T2 contract test (task 4.11) statically extract every `/api/...` path the UI calls and assert each is a registered route.

## 5. Database (task 1.3 — `src/rga/db/migrations/001_initial.sql`)

```sql
PRAGMA foreign_keys = ON;
CREATE TABLE rifles (id INTEGER PRIMARY KEY, public_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL, cartridge TEXT NOT NULL,
  bullet_diameter_in REAL NOT NULL CHECK (bullet_diameter_in > 0.1 AND bullet_diameter_in < 0.6), scope TEXT, notes TEXT NOT NULL DEFAULT '',
  zero_load_id INTEGER REFERENCES loads(id) ON DELETE SET NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE loads (id INTEGER PRIMARY KEY, public_id TEXT NOT NULL UNIQUE, rifle_id INTEGER NOT NULL REFERENCES rifles(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('factory','handload')), label TEXT NOT NULL,
  maker TEXT, product TEXT, bullet_weight_gr REAL, bullet_type TEXT,          -- factory
  bullet TEXT, powder TEXT, charge_gr REAL, primer TEXT,                       -- handload
  trued_mv_fps REAL CHECK (trued_mv_fps IS NULL OR trued_mv_fps > 0), notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX ix_loads_rifle ON loads(rifle_id);
CREATE TABLE load_lots (id INTEGER PRIMARY KEY, load_id INTEGER NOT NULL REFERENCES loads(id) ON DELETE CASCADE, lot_code TEXT NOT NULL,
  created_at TEXT NOT NULL, UNIQUE (load_id, lot_code));
CREATE TABLE sessions (id INTEGER PRIMARY KEY, public_id TEXT NOT NULL UNIQUE, rifle_id INTEGER NOT NULL REFERENCES rifles(id) ON DELETE CASCADE,
  date TEXT NOT NULL, distance_yd INTEGER NOT NULL CHECK (distance_yd > 0),
  position TEXT NOT NULL CHECK (position IN ('bench','prone_off_pack','kneeling_sitting','standing_field')),
  suppressor INTEGER NOT NULL CHECK (suppressor IN (0,1)), temperature_f REAL, density_altitude_ft REAL, wind_mph REAL,
  wind_clock INTEGER CHECK (wind_clock IS NULL OR wind_clock BETWEEN 1 AND 12), weather_skipped INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX ix_sessions_rifle_date ON sessions(rifle_id, date);
CREATE TABLE photos (id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, ext TEXT NOT NULL, width_px INTEGER NOT NULL, height_px INTEGER NOT NULL,
  reference_kind TEXT CHECK (reference_kind IS NULL OR reference_kind IN ('grid_1in','circle_1in','circle_1_5in','circle_3in')),
  reference_json TEXT, h_px_to_plane_json TEXT, working_ppi REAL, working_w INTEGER, working_h INTEGER, axis_ratio REAL,
  reference_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (reference_confirmed IN (0,1)), created_at TEXT NOT NULL,
  CHECK (reference_confirmed = 0 OR (h_px_to_plane_json IS NOT NULL AND working_ppi IS NOT NULL)));
CREATE TABLE zeros (id INTEGER PRIMARY KEY, rifle_id INTEGER NOT NULL REFERENCES rifles(id) ON DELETE CASCADE,
  load_id INTEGER NOT NULL REFERENCES loads(id) ON DELETE CASCADE, lot_id INTEGER REFERENCES load_lots(id),
  mode TEXT NOT NULL CHECK (mode IN ('new_setup','existing')),
  state TEXT NOT NULL CHECK (state IN ('step1','step2_initial','step2_confirm','step3_dot','complete','superseded')),
  dialed_up_mil REAL, dialed_right_mil REAL, zero_es_in REAL, zero_shot_count INTEGER, dot_size_in REAL CHECK (dot_size_in IS NULL OR dot_size_in > 0),
  completed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX ix_zeros_rifle_load ON zeros(rifle_id, load_id, state);
CREATE TABLE groups (id INTEGER PRIMARY KEY, public_id TEXT NOT NULL UNIQUE, session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  load_id INTEGER NOT NULL REFERENCES loads(id), lot_id INTEGER NOT NULL REFERENCES load_lots(id),
  photo_id INTEGER REFERENCES photos(id), roi_json TEXT, shots_expected INTEGER NOT NULL CHECK (shots_expected >= 1),
  elevation_dialed_mil REAL, aim_x_in REAL, aim_y_in REAL,
  purpose TEXT NOT NULL DEFAULT 'general' CHECK (purpose IN ('general','zero_step1','zero_step2','zero_confirm','zero_check')),
  zero_id INTEGER REFERENCES zeros(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','final')), notes TEXT NOT NULL DEFAULT '',
  finalized_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CHECK (status = 'draft' OR (aim_x_in IS NOT NULL AND aim_y_in IS NOT NULL)));
CREATE INDEX ix_groups_session ON groups(session_id);
CREATE INDEX ix_groups_load_status ON groups(load_id, status);
CREATE INDEX ix_groups_lot ON groups(lot_id, finalized_at);
CREATE INDEX ix_groups_zero ON groups(zero_id);
CREATE TABLE shots (id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE, seq INTEGER NOT NULL,
  x_in REAL NOT NULL, y_in REAL NOT NULL, dx_in REAL, dy_in REAL, dx_mil REAL, dy_mil REAL,
  origin TEXT NOT NULL CHECK (origin IN ('detected','split','estimated','manual')),
  moved_by_user INTEGER NOT NULL DEFAULT 0 CHECK (moved_by_user IN (0,1)), confidence REAL, UNIQUE (group_id, seq));
CREATE TABLE detection_rejections (id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  x_in REAL NOT NULL, y_in REAL NOT NULL, reason TEXT NOT NULL CHECK (reason = 'not_a_bullet_hole'), created_at TEXT NOT NULL);
CREATE TABLE zero_checks (id INTEGER PRIMARY KEY, zero_id INTEGER NOT NULL REFERENCES zeros(id) ON DELETE CASCADE,
  group_id INTEGER NOT NULL UNIQUE REFERENCES groups(id) ON DELETE CASCADE,
  result TEXT NOT NULL CHECK (result IN ('pass','possible_lost_zero')), created_at TEXT NOT NULL);
PRAGMA user_version = 1;
```
- `connection.py`:
  - `connect(path) -> Connection` sets `foreign_keys=ON`, `journal_mode=WAL`, `row_factory=sqlite3.Row`, and `detect_types=0`.
  - `migrate(conn)` applies `migrations/NNN_*.sql` with number > `user_version`, in order, each in a transaction.
  - `get_db()` is the FastAPI dependency (one connection per request, from `config.db_path()`, migrated at app startup). It lives in `rga/web/deps.py`, the single sanctioned `web → db.connection` import, so that `db` never depends on `config`.
- Timestamps are ISO-8601 UTC strings. IDs are integer PKs internally; `public_id` (uuid4) is the Open Brain hook (P-09).
- **Rejection is schema-enforced:** the `CHECK (reason = 'not_a_bullet_hole')` means no code path can store a "flier".
- **Finalize is schema-backstopped:** a final group must have an aim point. The service additionally enforces `count(shots) == shots_expected` and `photo.reference_confirmed = 1` (when `photo_id` is set).

## 6. Photo pipeline algorithms (guidance for Group 2; the spike may revise and must log changes)

**Reference — circle.**
- Grayscale → Otsu/adaptive threshold → contours → `fitEllipse` on contours with area/convexity consistent with a filled circle or ring. Score by edge contrast and fill uniformity.
- Return the top-k candidates. **The user always confirms**: printed aim dots are also circles, so auto-picking is ambiguous by design.
- Scale = `diameter_in / mean(axes_px)`. `axis_ratio = minor/major`.

**Reference — grid.**
- Suppress non-line content (morphological opening with long horizontal/vertical kernels), then find line segments (`HoughLinesP`), cluster them into two families, and intersect.
- Fit the lattice index by RANSAC homography to integer inch coordinates. Reject if `rms_residual_in > 0.02` or fewer than 9 inliers.
- Manual fallback: 4 corners + squares across/down.

**Working image.** Warp with `h_px_to_working = S · h_px_to_plane`, where `S = diag(ppi, ppi, 1)` plus a translation so the content bbox starts at (0,0). `ppi = min(native, 200)`, and the long side is ≤ 4000 px.

**Detection (spike task 2.4).** Expected single-hole radius is `r = bullet_diameter_in × ppi / 2`.
- Multi-cue candidates:
  1. Dark-on-light blobs (adaptive threshold, after erasing printed lines with an opening longer than 3r).
  2. Light-on-dark blobs inside dark printed regions.
  3. A high-saturation ring cue for splatter targets.
- Keep blobs with `0.35·A1 ≤ area ≤ 8·A1` (A1 = πr²) and solidity ≥ 0.6.
- Classify by area ratio, circularity, and distance-transform peaks (min peak separation 0.5·2r):
  - `single`: one peak, ratio < 1.45.
  - `split`: k peaks whose union of k radius-r circles has IoU ≥ 0.8 with the blob.
  - `ragged`: otherwise.
- Center = intensity-weighted centroid of the core, refined by sub-pixel circle fit at radius r.
- `confidence` ∈ [0,1] from contrast and shape fit.

**Shortfall fill (task 2.5).** Implements spec §4.3 step 5.
- Each ragged blob gets `n_i = s × excess_i / Σexcess` (largest-remainder rounding), where `excess = area − A1`. `n_i ≥ 1` for every ragged blob while `s ≥` the number of ragged blobs. Otherwise status `unassigned_ragged` with a message.
- Placement: `cv2.kmeans` with fixed seed and `KMEANS_PP_CENTERS` on blob pixel coords, k = n_i.
- Then up to 50 iterations of pairwise repulsion toward separation `2r`, projected back onto the blob's eroded interior (`distanceTransform > 0`). Every final center must satisfy `pointPolygonTest(contour) ≥ 0`.
- Deterministic: the same input always gives the same output.

**Evaluation harness (`tools/eval_detection.py`).** It takes a catalog of synthetic scenarios (and optionally a real-photo dir) and writes a metrics table: recall, false positives, center error p50/p95/max, split rate, ES error, runtime. It runs Hungarian matching with a 0.5·d gate. Exit code is non-zero if any P-11 synthetic threshold fails. It writes `<root>/.claude/specs/rifle-group-analyzer/spike-report.md`.

## 7. Synthetic fixtures (task 1.5 — `tests/synth/generate.py`)
`render(scenario: dict, seed: int) -> (image_bgr: np.ndarray, truth: dict)`. It is deterministic and needs only numpy + OpenCV drawing.
- **Paper and print:** white paper with low-frequency lighting gradient + texture noise. Printed elements: 1" grid (thin lines), solid circle reference of exact diameter, aim dots/diamonds (black and colored), optional black bull.
- **Holes:**
  - Each hole is a dark core at radius `r·U(0.85,1.0)` with polar jagged-edge noise, a grey torn halo, and an optional bullet-wipe ring.
  - Holes in black regions render a light core.
  - Splatter variant: fluorescent ring 1.5–3d.
- **Composites:** merged pairs at separations 0.4–1.0 d; ragged clusters (k holes within 0.5 d).
- **Camera:** ppi 120–350, rotation, perspective homography (tilt ≤ 15°), blur, gaussian noise, JPEG q 70–95.
- **Truth:**
  - `holes_in` (plane, y-down, inches).
  - `ragged_groups` (indices of holes that form each ragged blob).
  - `reference` (kind, center/corners in image px).
  - `h_plane_to_image`, `ppi`, `bullet_diameter_in`.
- **`catalog.json` named scenarios:** `clean_circle1`, `clean_circle3`, `clean_grid`, `noisy_lighting`, `skew10_grid`, `skew15_grid`, `merged_pairs`, `ragged_10x7` (AC-9: 7 distinct + 1 ragged blob of 3), `black_bull_light_holes`, `splatter`, `circle_tilt_warn` (axis ratio ≈ 0.95), `no_reference`, `multi_dot_target` (3 aim dots, 3 groups, P-08), `high_right_group` (coordinate-convention seam).

## 8. UI
- Server-rendered pages with small ES modules. Review editor (`svg_editor.js`): an SVG layer over the working image, with a viewBox in working px.
  - Shots are circles of radius `bullet_d × ppi / 2`.
  - `estimated` shots are dashed orange, labeled "est".
  - `split` shots are solid blue. `detected` and `manual` are solid green.
  - The aim point is a crosshair.
  - Interactions: drag (pointer events), click empty → add manual shot, select + Delete key or button → "Not a bullet hole" (the only delete wording), "Mark aim" mode with snap, expected-count input, "Fill shortfall" button.
  - A live shot counter reads "9 of 10 shots". Finalize stays disabled until counts match.
- Stats panels always show ES as in + MOA with the phrase "smallest target every shot hit". Mean POI is shown in mil with direction words, and there is an "includes estimated shots" badge.
- The zeroing page is a stepper (Step 1/2/3, Zero check) that shows only the advice the domain returns. When the advice is `SHOOT_MORE`, no correction number is rendered anywhere on the page.
- No external fonts, scripts, or CDNs. CSS is hand-written and minimal.

## 9. Config surface (T2 contract: read vs set, both directions)

| Var | Default | Read by | Set by |
|---|---|---|---|
| `RGA_DATA_DIR` | `<root>/data` | `config.py` | `scripts/run.sh`, `tests/support/live_server.py`, `scripts/smoke.sh` |
| `RGA_HOST` | `127.0.0.1` (must be loopback: `127.0.0.1`, `::1`, `localhost`) | `config.py` | `scripts/run.sh` |
| `RGA_PORT` | `8765` | `config.py` | `scripts/run.sh`, live_server, smoke |
| `RGA_LOG_LEVEL` | `info` | `config.py` | `scripts/run.sh` |

A contract test (task 1.8) parses `scripts/run.sh` and `config.py` and asserts the two sets are equal.

## 10. Dependencies (task 1.1 pins exact versions via pip-compile; ranges below are intent)
Runtime:
- `fastapi`, `uvicorn` (no `[standard]` extras; avoids uvloop/httptools/watchfiles)
- `jinja2`, `python-multipart`, `pydantic`
- `numpy`, `opencv-python-headless`, `pillow`, `pillow-heif`

Dev:
- `pytest`, `pytest-socket`, `httpx`, `playwright`, `pytest-playwright`
- `pip-tools`, `ruff`, `bandit`, `pip-audit`

Python 3.14.5 (Homebrew `/opt/homebrew/bin/python3.14`). PyPI (checked 2026-09-30) has cp314/abi3 macOS arm64 wheels for all of these. `opencv-python-headless` latest is **5.0.0.93** (a new major). The scaffold pins it, and the spike confirms API compatibility. If anything used is broken in 5.x, fall back to the latest 4.x and log the decision.

## 11. Security Considerations

Threat model: a single user on their own Mac. The assets are the user's shooting records and photos. Realistic threats:
- (a) Exposure on the LAN (café Wi-Fi).
- (b) A malicious website in the user's browser driving the local API (CSRF / DNS rebinding).
- (c) Malformed or hostile image files.
- (d) Accidental data exfiltration via dependencies or CDNs.
- (e) Data loss.

| Control | Implementation | Verified by |
|---|---|---|
| Loopback-only bind | `config.py` rejects non-loopback `RGA_HOST` with exit code 2; `run.sh` never passes `--host 0.0.0.0` | T1 config test; T3 `lsof` shows `127.0.0.1:<port>` only; T3 negative start with `RGA_HOST=0.0.0.0` exits non-zero |
| DNS-rebinding defense | `TrustedHostMiddleware(allowed_hosts=["127.0.0.1","localhost","[::1]"])` | T2 + T3: `Host: evil.example` → 400 |
| Browser CSRF defense | `OriginGuard`: for POST/PUT/PATCH/DELETE, if `Origin` is present it must equal the server origin; body must be `application/json` or `multipart/form-data` (rejects simple-form posts) | T2 + T3: cross-origin POST → 403; `text/plain` POST → 415 |
| No authentication | Accepted for v1: single user, loopback only (D-09). Compensating controls are the three rows above. Any local process can reach the API, same as any local file. *The API Gateway "authorizer on every route" rule does not apply (no API Gateway).* | Documented here; lead records in `decisions.md` / `security-exceptions.md` |
| Upload hardening | Size cap 40 MB (413), decoded-pixel cap 60 MP, Pillow `MAX_IMAGE_PIXELS` set, magic-byte sniffing (JPEG/PNG/HEIC only → else 415), stored under `sha256.ext` (no user filename ever touches the filesystem), working image re-encoded as PNG | T1 io tests; T3 oversize + non-image uploads |
| Anti-framing / content policy (threat b; B-09) | The security middleware adds headers to every response: `X-Frame-Options: DENY`, `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`. No inline scripts or styles in templates. | T2 header test on a page, an API response and an error response; T3 smoke header check |
| Path traversal | Only server-generated names; `GET image` resolves from the DB row, never from a client path | T2 |
| Open redirect | `return_to` validated as a relative path beginning with a single `/` | T2 |
| XSS | Jinja2 autoescape on; JS renders text via `textContent`, never `innerHTML`, for user strings (notes, labels) | T2 template test with `<script>` in notes; review checks |
| SQL injection | Parameterized queries only; `search_notes` escapes `%`/`_` with `ESCAPE '\'` | T2 repo tests with `%`, `_`, `'` inputs |
| No outbound network | No HTTP client in runtime deps; `pytest-socket` blocks non-loopback in all T1/T2; no CDN assets (template test greps for `http://`/`https://` in templates/static) | T1/T2 socket guard; T3 `lsof -i` shows no non-loopback connections for the server PID |
| Secrets | None exist in v1 (no API keys, no accounts) | bandit scan |
| Encryption at rest | Not implemented in-app. Relies on macOS FileVault for the data dir (the README tells the user to confirm it is on). *KMS/SSE rules do not apply.* | Documented |
| Data loss | SQLite WAL; README documents backup = copy `data/` while the server is stopped | Documented; T3 `PRAGMA integrity_check` |
| Supply chain | Hash-pinned lock files (`--require-hashes`); `pip-audit` + `bandit` via `scripts/scan.sh`; results saved to the spec folder | Task 4.12 |

Data classification: personal/internal. There is no tagging mechanism (local files). The AWS tagging and logging requirements do not apply.

## 12. Risks
1. **CV reliability on real mixed targets** (requirements Assumption 1). Mitigations:
   - The spike on synthetic data runs in Group 2.
   - A real-photo harness is ready for the user's 10–20 photos.
   - Manual review means the product still works with weak detection: the user clicks holes, as in Hornady but with stats, aggregation, and zeroing.
2. **OpenCV 5.0 is a new major version.** The spike validates it; fallback is the latest 4.x.
3. **HEIC from AirDrop** is handled by pillow-heif and covered by a T1 test using a HEIC encoded by pillow-heif.
4. **Grid detection on busy targets.** The manual 4-corner fallback is always available.
5. **Playwright needs a one-time Chromium download** (~150 MB, network) at bootstrap. This is dev tooling only, not app runtime.

## Build-time interface notes (lead, from Group 1 reports)

These are binding for later groups and come from what actually landed.
- **Scaffold (T1.1):**
  - `rga` is importable through a venv `.pth` file.
  - Playwright browsers live in `<root>/.playwright-browsers` (`PLAYWRIGHT_BROWSERS_PATH`).
  - pytest runs with `--strict-markers` (`accuracy`, `e2e` only) and a loopback-only socket guard. A blocked connect raises `SocketConnectBlockedError`.
  - conftest strips `RGA_*` and sets a temp `RGA_DATA_DIR`. `live_server` must set all four `RGA_*` vars for its child.
  - `scan.sh` must call `bandit -c pyproject.toml`.
  - The Makefile has the targets `bootstrap lock test test-fast accuracy smoke e2e scan run lint`.
- **Domain (T1.2):**
  - `round_tenth` rounds half away from zero, never returns -0.0, and raises on NaN/inf.
  - Unit conversions raise `ValueError` for yd ≤ 0.
  - `MOA_IN_PER_100YD = 1.0471975512`.
  - `diameter_for` is case/whitespace-insensitive and returns `None` if the caliber is unknown.
- **DB (T1.3):**
  - `connect()` uses `check_same_thread=False`, with one connection per request.
  - The API is `migrate()`, `schema_version()`, `latest_version()`, `MigrationError`.
  - Migrations load via `importlib.resources`.
  - `DopeRow` = `group_id, session_id, session_date, distance_yd, elevation_dialed_mil, shot_count`. The center dy is computed in the service from shots.
  - Fixture: `from tests.support.db import migrated_conn  # noqa: F401`, and `# noqa: F811` on tests taking it.
- **UI shell (T1.9):**
  - `base.html` blocks: `title, head, content, scripts`. The `active` var is one of `home|rifles|analysis|compare|dope`.
  - Static files are served at `/static`.
  - The nav links only `/`, `/rifles`, `/analysis`, `/compare`, `/dope`, so `/compare` and `/dope` must work with no query string (show a picker).
  - `#flash` is the error area.
  - `api.js`: `apiGet`, `apiSend`, `apiUpload` and `ApiError{detail,code,status}`. **The upload multipart field name is `file`**, so `POST /api/photos` must read `file`.
- **Transforms (T1.4):**
  - `homography_from_points` is exact for 4 points and uses RANSAC (0.05 in) for more. It returns h with `h[2,2]=1` and raises on fewer than 4 points or degenerate input.
  - `apply_h` takes an ndarray or tuple-of-tuples.
  - `similarity_from_circle` takes FULL axis lengths (as `cv2.fitEllipse` returns them) and maps the circle center to plane (0,0). `build_working` then re-origins the plane to the working image's top-left.
  - `build_working` returns `(working, frame, h_px_to_working)`, where h maps the ORIGINAL oriented px. It uses `BORDER_REPLICATE`.
  - Integer pixel coordinates are pixel centers.
- **Config/app/security (T1.6):**
  - `Settings` is frozen. Use `settings.db_path` (there is no `config.db_path()` function), `photos_dir` and `working_dir`.
  - Routes export `router = APIRouter()` with full `/api/...` paths and get no prefix.
  - Handlers use `conn: Annotated[sqlite3.Connection, Depends(get_db)]`. `get_db` doesn't commit, so services/routes commit explicitly.
  - Only `web/deps.py` imports `db.connection`.
  - Templates are at `app.state.templates`.
  - The OriginGuard requires an exact `http://<Host>` Origin and JSON/multipart content types. Bodyless POSTs pass.
  - Use `safe_return_to` for any `return_to`.
  - `/docs` is disabled.
  - The 413 upload cap is owned by T3.3.
  - The global `{detail, code}` handler is owned by T1.10.
- **Synthetic fixtures (T1.5):**
  - Use `tests.synth.generate.load_catalog()` and don't parse catalog.json. Render with a fixed seed (0).
  - Truth plane coordinates are inches from the PAPER's top-left (y down). They are not the pipeline's working origin, so compare offsets/ES or map through `h_plane_to_image`.
  - `ragged_outlines_in` gives the AC-9 containment polygon. `groups[].roi_px` gives the multi-group ROIs.
  - The 1 in aim dot can look like the 1 in reference circle, so pick the circle candidate nearest `truth.reference.center_px`.
  - Byte-determinism holds only for a fixed cv2 version.
- **API schemas (T1.7):**
  - Use `rga.web.schemas.ROUTE_SCHEMAS[(METHOD, path)]` for request, response and query models.
  - List endpoints return bare arrays. Request models forbid extra fields and NaN.
  - `*_in` = plane inches (origin top-left, y down), `*_px` = working px, offsets are y-up.
  - `RoiPx{x0,y0,x1,y1}` is in working px.
  - `ReviewRequest` takes plane inches.
  - `ReferenceConfirmRequest` takes exactly one of `candidate_index`/`manual`.
  - `ComparisonQuery.model_validate(dict(request.query_params))` takes `load_ids` as "1,2,3".
  - `ZeroDialed` must be whole 0.1 mil clicks.
- **Errors (T1.10):**
  - Routes call `from rga.web.app import ApiError; raise ApiError(status, code, detail)`. The code is snake_case.
  - HTTPException → `http_<status>`, 422 → `validation_error`, 500 → `internal_error`.
  - Tests of unexpected exceptions use `TestClient(app, raise_server_exceptions=False)`.
- **Docstring gate (F1.4):** `tests/unit/test_types.py` fails if any public top-level class or function in `src/rga/domain/*.py` lacks a docstring, so Group 2 domain modules must document their public names.
- **Security headers and CSP (F1.1):**
  - `SecurityHeaders` middleware (outermost) adds the §11 headers to every response.
  - A test scans all `templates/*.html` and `static/**/*.js` for CSP violations: inline script or style, `style=`, `on*=`, `javascript:`, `setAttribute('style')`, eval. Group 3 pages must use external module scripts and CSS classes only.
  - Also from F1.1: the domain↔DB enum contract test fails if a new DB CHECK enum has no pair.
- **Synthetic truth additions (F1.5):**
  - New keys: `merged_pairs[].core_gap_in` and `cores_overlap` (the 1.0 d pair is marginal per seed), `decoys_in` (dark non-shot marks of 0.36–0.81 A1 that must not be detected), `holes_light_core`, `holes_wipe`.
  - Ragged members are ≤ 0.5 d apart. The outline area is 1.6–1.8 A1.
  - The catalog now has 17 scenarios. `hires_wipe_decoys` is 17.8 MP at ppi 380, so it exercises the 200 ppi and 4000 px caps.
  - The P-11 gate covers all 17 (B-11).
- **Rules from B-13:**
  - No native HTML form POSTs; all writes go through `api.js` (Referrer-Policy no-referrer would make Chromium send `Origin: null` → 403).
  - No table-rebuild migration without an FK-off-outside-transaction helper plus `foreign_key_check`.
- **Image IO (T2.1):**
  - `rga.vision.io.load_oriented(data) -> (bgr, ext)`, `sha256_hex`, `ImageRejected(ValueError)`.
  - T3.3 maps "unsupported image type" → 415 and "upload exceeds"/"image exceeds" → 413, and applies its own streaming 413 guard before reading the whole body.
- **Ragged resolution (T2.5):**
  - The Blob contract (agreed between coding-1 and coding-6): absolute working px; `contour_px` = int outer contour; `area_px` = `cv2.contourArea`; ragged blobs carry their centroid in `centers_px`.
  - `resolve_shots` statuses are `ok`, `too_many` and `unassigned_ragged`.
  - **No blobs + shortfall → status `ok` with empty shots and an "add them by hand" message.** T2.16 and T3.4 must treat that as manual entry.
  - A shortfall with no ragged blob is stacked at the largest hole.
  - `cv2.setRNGSeed` is called before kmeans (a global side effect).
  - Estimated confidence is 0.25.
- **Live server (T2.17):**
  - `from tests.support.live_server import live_server, run_live_server`. The fixture value is a URL str with `.request(method, path, headers, body)`, `.log_text()`, `.data_dir` and `.stop()`.
  - Test modules importing it need `# ruff: noqa: F811`.
  - smoke.sh sets only the four RGA_* vars.
  - A SIGKILLed pytest leaves the server child running (it's in its own session).
- **Stats (T2.6):**
  - `rga.domain.stats`: `extreme_spread_in`, `group_stats(offsets, origins, distance_yd)`, `AggregateInput(offsets, origins, distance_yd, load_id=None)`, `aggregate(groups) -> AggregateStats(..., mixed_loads)`.
  - The Step 2 center-on-center overlay lives in T2.7: translate each group's offsets by its own centroid, then `group_stats` on the union.
  - The identifiers `mean_radius`, `best_of`, `best_n` and `group_reduction` are banned anywhere in `src/`.
  - Keep the existing synth scenario names stable.
- **Zeroing (T2.7):**
  - `Advice.stats` is a domain `GroupStats` (None for SHOOT_MORE/INVALID). Services map it to `GroupStatsOut` for correction kinds and build `SizeOnlyStatsOut` themselves for short groups.
  - Confirm-advice overlay stats have mean ≈ 0 by construction, so don't present them as the zero's POI; the judged centroid is in `advice.correction`.
  - Step 1 with ≥10 shots → a "use Step 2" message.
  - `suggest_dot` ties go to the larger dot.
- **Comparison (T2.8):**
  - A load with only tier30 data has status `insufficient_data` and no verdict; the UI shows tier30 separately.
  - The `tier_for(shots)` helper exists.
  - `poi_shift_mil` returns `(right_mil, up_mil)` or None, and the service writes the "shoot both loads in one session" text.
  - `ZERO_TO_HUNTING_LOAD_ADVICE` deliberately avoids the word "offset" (lead-accepted).
- **Rifles repo (T2.10):**
  - `LoadNotInRifle(ValueError)` maps to **409** in routes (lead choice).
  - `update_*` take `**changes` restricted to `RIFLE_UPDATABLE`/`LOAD_UPDATABLE`. The service rejects cross-kind load fields.
  - `now_iso()` returns `%Y-%m-%dT%H:%M:%S.%fZ`. `public_id` is a uuid4.
  - IntegrityError propagates, and callers commit.
- **Sessions repo (T2.11):**
  - `replace_shots` renumbers seq and uses a savepoint, but doesn't commit.
  - `add_rejection` has no reason parameter (always `not_a_bullet_hole`) and is append-only, so the review service de-duplicates against `list_rejections`.
  - Updatable group fields are listed in the repo whitelist.
  - New-lot flag: use `first_group_of_lot` + `load_has_other_lot_groups(..., finalized_before=<first group's finalized_at>)`.
  - `search_notes` returns `(kind, id, snippet)`.
  - `detection_rejections` has no group_id index (accepted; the table is tiny).
- **Mutation testing:** run with bytecode writing disabled (`python -B` / `PYTHONDONTWRITEBYTECODE=1`) to avoid stale `.pyc` false kills.
- **Zeroing repo (T2.12):**
  - `ZeroTargetMismatch` and `GroupNotLinkable` map to **409** in routes.
  - Services own state legality, call `supersede_zeros` on completion, and set group purpose via repo_groups.
  - `list_linked_groups` maps to `ZeroGroupOut`.
  - `zero_checks` has no zero_id index (accepted).
- **Group service (T2.14):**
  - `rga.services.groups`: `load_group` → `GroupBundle(group, session, load, rifle, shots, stats)` where stats is `GroupStats | SizeOnlyStats | None`. Routes map these to `GroupStatsOut`/`SizeOnlyStatsOut`.
  - `save_review(conn, group_id, aim, shots, rejections, shots_expected)` commits. Its no-silent-drops rule is count-based.
  - `recompute_offsets` / `recompute_session_offsets`.
  - `finalize` raises `FinalizeError` with codes in order `reference_unconfirmed`, `no_aim`, `count_mismatch`.
  - `new_lot_flag`.
  - Errors are `GroupError(.code, .message)`. Routes map `group_not_found` → 404, `invalid_*` → 422, everything else → 409.
  - `tests/support/seed.py`: `seed_rifle`, `seed_load`, `seed_session`, `seed_photo`, `seed_group(offsets aim-relative y-up, finalize=True)`.
  - POI masking (`masks_poi(purpose, n)`; `is_short_zero_group` is an alias) covers short zero_step2/confirm groups and all zero_check groups (B-18).
- **Synthetic catalog v3 (T2.18):**
  - 23 scenarios.
  - New truth keys: `hole_style`, `background`, `paper_bgr`, `non_planar`, `non_planar_warp`, `print_decoys_in`, `staples_in`, `wrinkles_in`, `creases_in`, `corrugation_period_in`, `aim_marks_in`. The grid reference gains `line_color`, `line_bgr`, `band`.
  - **For curved sheets use `plane_to_image(truth, pts_in)`**, not `h_plane_to_image` alone.
  - Grid `corners_px` are in PLANE order, so on the 180° scenario TL is at the image bottom-right.
  - `groups[].roi_px` now contains all of its group's holes.
- **Detection (T2.4):**
  - `detect_holes(working, frame, bullet_diameter_in, roi_px=None, params=None) -> list[Blob]` returns absolute working px, sorted by (y, x), deterministically.
  - SINGLE blobs have 1 center, SPLIT 2, RAGGED 1 (the centroid).
  - Only holes are returned; QR, text, grid, aim marks, staples and dirt are rejected inside.
  - `DetectParams` is frozen. **Don't change its defaults without re-running the gate.**
  - The ROI is a working-px box; synth `groups[].roi_px` is in oriented image px, so convert it.
  - `tools.eval_detection` exports `score_render`/`aggregate`/`hungarian` for T2.15.
- **Circle reference (T2.2):**
  - Size gates per kind (1 in: 4.5–40% of the short side; 1.5 in: 6–50%; 3 in: 10–95%).
  - The true circle is at rank 0 or 1, so **the UI must let the user pick** and never auto-confirm.
  - `refine_circle_near(img, xy, max_radius_px≈0.5 in·ppi)` returns None if nothing is in reach (keep the raw click). Its `axes_px` aren't a real size.
- **Grid reference (T2.3):**
  - `GridCandidate` fields are per §4.5. The plane origin is the lattice corner with the smallest indices.
  - Warning text: `"non_planar: the grid lines are curved; flatten the target and re-shoot"`, keyed by the `non_planar` prefix, for rms > 0.05 in or NaN.
  - `grid_from_manual` takes TL, TR, BR, BL as displayed.
- **Pipeline (T2.16):**
  - `rga.vision.pipeline`: `propose_reference`/`propose_candidates` (`ReferenceProposal` with `.circle` / `.grid_points_px` for the T3.3 overlay; the user picks and nothing is auto-confirmed), `manual_reference(kind, edge_points_px | grid_corners_px + squares)`, `build_working_image` (only after confirm), `analyze_group(working, frame, d, shots_expected, roi_px)` → `(blobs, ResolveResult)`, `shots_from_resolve`, `roi_to_working`.
  - A tilted circle reference has about 9% scale error in the tilt direction, which is why it warns.
- **Hardening (FX2.4):** `rga.vision.io.MAX_ASPECT_RATIO = 20` (`load_oriented` rejects anything beyond it from the header). The reference detectors import it and raise ValueError. `dope_rows(conn, load_id)` takes no override.
