# Spec: Rifle Group Analyzer

> Slug: `rifle-group-analyzer` · Author: fullstack-agent (planning) · Date: 2026-09-30
> Inputs: `requirements.md` (FR/NFR), `decisions.md` (D-01..D-18, settled). This spec does not override any D-xx. Planning-level choices it adds are numbered **P-xx** below. They need user confirmation and then get promoted into `decisions.md` by the lead.

## 1. Summary

This is a local, single-user web app on the user's Mac. It turns photos of paper targets into a per-rifle statistical record. Python (FastAPI + OpenCV + SQLite) serves a browser UI on `127.0.0.1` only. A photo goes through these steps:

1. Pick the reference object (dropdown, D-08). The tool auto-locates it and the user confirms, or marks it by hand.
2. The image is rectified into a target plane in inches.
3. Classic CV finds hole centers (D-06).
4. Merged holes are split, and any shortfall against the expected shot count is placed as estimated shots inside ragged holes (FR-3.7, D-18).
5. Mandatory user review.
6. The finalized group is stored, along with extreme spread, mean POI, and Form-doctrine zeroing and comparison logic (D-04, D-14, D-15).

No network calls, no AI, no accounts (NFR, D-06, D-16).

## 2. Goals and non-goals

**Goals:** the observable success criteria SC-1..SC-8 in `requirements.md` §Intent, implemented as FR-1..FR-7.

**Non-goals (v1):** everything in `requirements.md` §Out of Scope and D-16. That includes Open Brain sync (a hook only, P-09), phone/remote access, chronograph import, ballistic solver/MV back-calc (D-03), any AI call, dot drills, zero-history log, stringing analysis, mean radius / group reduction (D-14), full reloading log, and barrel round count.

## 3. Acceptance criteria (each maps to a T4 journey in `test-plan.md`)

| ID | Criterion | Source |
|---|---|---|
| AC-1 | User imports a photo, selects the reference from the dropdown, confirms the reference, reviews detected holes, marks the aim point, and sees ES (in, MOA) and mean POI offset (mil, in). On a synthetic fixture, ES is within **0.05 in** of ground truth and mean POI within **0.02 mil** | SC-1, FR-3, FR-4.1/4.2 |
| AC-2 | Two loads with ≥10 rounds each are shown side by side by ES (MOA). A load with <10 rounds shows "insufficient data" and is not ranked. Loads within normal variation show "no real difference". 20+ rounds is labeled higher confidence | SC-2, FR-5.1/5.2 |
| AC-3 | Each load's POI shift vs the rifle's zero load is shown in mils (from same-session data, P-06), with "zero the rifle to your hunting load" advice and no app-offset advice | SC-3, FR-5.4, FR-6.7 |
| AC-4 | User selects N groups (same rifle, same distance). The aggregate overlay shows combined ES, combined center, and total shot count (e.g. 3×10 → 30) | SC-4, FR-4.4 |
| AC-5 | The zeroing flow runs Step 1 → Step 2 (10, adjust, 10, overlay to 20, optional 30) → Step 3 (dot). Corrections are given in 0.1 mil with direction words. **No numeric correction is ever produced from a 2–9 shot group outside Step 1** | SC-5, FR-6.1–6.5 |
| AC-6 | Zero check with 1–2 shots against the stored dot returns PASS (inside or touching) or "possible lost zero", with routing to Step 2 and a mount check (#1) then scope check (#2) prompt | SC-6, FR-6.6 |
| AC-7 | The per-load dope table lists distance, elevation dialed (mil), group center vertical offset (mil), and centered elevation (mil) | SC-7, FR-7.1 |
| AC-8 | Session and group notes are captured, shown with their groups, and searchable | SC-8, FR-2.4 |
| AC-9 | Ragged hole: a 10-shot synthetic photo with 7 distinct holes and one ragged hole yields 10 shots, 3 marked estimated, all inside the ragged outline. The stats are labeled "includes estimated shots" | FR-3.7, D-18 |
| AC-10 | Must-never guards: no shot can be removed except with reason `not_a_bullet_hole`. No scale is applied without a confirmed reference. A group cannot be finalized unless shot count equals expected count. The server refuses to bind non-loopback and makes no outbound connections | Edge cases, NFR |
| AC-11 | First group of a new lot of an existing load is flagged "new lot — check POI against zero load" | FR-1.5 |
| AC-12 | Session at ≥200 yd prompts for weather (temp, optional DA and wind). Below 200 yd it is optional | FR-2.2 |

## 4. Functional specification

### 4.1 Rifles, loads, lots (FR-1, D-10, D-11)
- Rifle: name, cartridge, `bullet_diameter_in` (pre-filled from the caliber table in `domain/calibers.py`, user-editable; FR-1.4), scope description, notes, and `zero_load_id` (nullable). The scope is always mil/mil with a 0.1 mil click (constant, not a field).
- Load (always scoped to one rifle):
  - `factory`: maker, product, bullet weight (gr), bullet type.
  - `handload`: bullet, powder, charge (gr), primer.
  - Optional `trued_mv_fps`. Notes.
  - No velocity SD/ES, no COAL/CBTO fields.
- Lot/batch: table `load_lots(load_id, lot_code)`. The factory "lot number" and handload "batch ID" both live here. Every group references exactly one lot (FR-1.5). New-lot flag (AC-11): a group is flagged when it is the earliest finalized group for its `lot_id` **and** the load already has finalized groups under a different lot.

### 4.2 Sessions and groups (FR-2, D-12)
- Session: date, rifle, distance (yd, integer > 0), position (enum, P-10), suppressor (bool), notes. Weather fields are `temperature_f`, `density_altitude_ft`, `wind_mph`, and `wind_clock` (1–12). The UI expands and prompts these at ≥200 yd and requires either values or an explicit "skip weather" click. The API accepts nulls (AC-12).
- Group: session, load, lot, photo (nullable), ROI on the photo (nullable, P-08), `shots_expected` (≥1), `elevation_dialed_mil` (nullable), aim point, purpose (enum `general|zero_step1|zero_step2|zero_confirm|zero_check`), `zero_id` (nullable), status `draft|final`, notes.
- Shot: plane coordinates (in), offsets from aim (in, y-up), offsets (mil), origin `detected|split|estimated|manual`, `moved_by_user`, confidence. Offsets are derived data, recomputed in one place (`services/groups.py::recompute_offsets`) whenever the aim point, shots, or session distance change (P-04).

### 4.3 Photo pipeline (FR-3, D-06, D-07, D-08)
1. **Import**: JPEG/PNG/HEIC. EXIF orientation is applied. Size limit 40 MB, decoded-pixel limit 60 MP. The image is stored content-addressed (sha256) under the data dir.
2. **Reference** (dropdown: `grid_1in | circle_1in | circle_1_5in | circle_3in`). The server proposes candidates and **the user confirms one or marks manually**.
   - Manual circle: three clicks on the edge.
   - Manual grid: four intersection clicks plus the number of squares across/down.
   - If nothing is confirmed, there is no scale and the pipeline stops (AC-10).
   - Grid → full homography (perspective-corrected).
   - Circle → similarity (scale only), plus an **ellipse warning** when the minor/major axis ratio is < 0.98 (P-03). There is no ellipse correction in v1.
3. **Rectify** to a working image in the target plane at `working_ppi = min(native_ppi, 200)`, long side ≤ 4000 px.
4. **Detect** hole centers in the group ROI using the known bullet diameter in px. Each blob is classified as `single`, `split` (a merged blob resolved into k circles), or `ragged` (unresolvable).
5. **Shortfall fill** (FR-3.7, D-18, P-05):
   - `distinct = single + split` shots.
   - If `distinct < shots_expected`, the shortfall `s` is distributed across ragged blobs in proportion to excess area, and all of it is marked `estimated`. Placement: k-means on the blob's pixels, constrained inside the outline, spaced ≥ bullet diameter where the outline allows. This runs deterministically.
   - With no ragged blob, estimated shots go at the centers of the largest distinct holes (through-the-same-hole).
   - `distinct > shots_expected` blocks finalization with "delete detection errors or raise the shot count".
6. **Review** (mandatory, FR-3.5). The user can drag any shot (estimated included), add a shot (`manual`), delete with the single reason `not_a_bullet_hole` (persisted in `detection_rejections`), set the aim point (with optional snap to the nearest aim-mark centroid), and edit the expected count. Shortfall fill re-runs on demand. Holes are drawn as fixed circles at bullet diameter × ppi (D-07).
7. **Finalize**: requires a confirmed reference, an aim point, and `len(shots) == shots_expected`.

### 4.4 Statistics (FR-4, D-14)
- ES = max pairwise center-to-center distance (in). MOA = in / (1.0471975512 × yd/100). Labeled "smallest target every shot hit (center-to-center)".
- Mean POI = centroid of offsets. Reported in mil (`in × 1000 / (yd × 36)`) and in.
- There is no mean radius or best-N computation anywhere in the codebase (a test greps for it).
- Aggregate (FR-4.4, D-13): user-selected groups. All must share the rifle and distance, or the request is rejected. Each group is aligned on its own aim point with no turret compensation. Output: combined ES, center, and shot count. Mixed loads give a warning, not a block.
- A group with any `estimated` shot carries `includes_estimated: true`. The UI labels it.

### 4.5 Load comparison (FR-5, D-15)
- Eligible groups are finalized, belong to the same rifle, and have `shots ≥ 10`. Per load: rounds = sum of eligible shots, and metric = mean ES (MOA) across eligible groups. Worst eligible group ES is shown too.
- < 10 rounds → "insufficient data", not ranked. ≥ 20 → "higher confidence" badge.
- "No real difference" when `|a−b| / mean(a,b) ≤ 0.30` (constant `NORMAL_GROUP_VARIATION = 0.30`, CBHB-FORM-013, P-07).
- Precision verdict per load (LD-07): ≤1.5 MOA → "sufficient for deer/elk to 800 yd". ≤2.0 → "to 600 yd". Otherwise "above Form's 2 MOA / 10-shot bar".
- POI shift vs zero load (P-06): for each session that has finalized groups of both the load and the rifle's zero load at the same distance, shift = mean POI(load) − mean POI(zero load), in mil. Report the mean across such sessions. If no such session exists: "shoot both loads in one session to measure POI shift". Advice text is always "zero the rifle to your hunting load; do not enter a ballistic-app offset".
- Filters: position and suppressor. A warning is shown when the compared loads' eligible groups differ in position or suppressor mix.

### 4.6 Zeroing (FR-6, D-04)
A zero is a per-(rifle, load, lot) record with state `step1 → step2_initial → step2_confirm → step3_dot → complete`, or `superseded`. All zeroing sessions must be at 100 yd.

**Corrections.** `correction_mil = round_half_away_from_zero(−mean_poi_mil, 0.1)` per axis. Text: elevation `UP`/`DOWN`, windage `LEFT`/`RIGHT`. Example: group high and right → "Dial DOWN 0.2, LEFT 0.3". A 0.0 correction means "no adjustment".

**Step 1 (get on paper)** — only for "new scope/rifle/scope swap" zeros.
- 1–3 shots → numeric correction.
- 4+ shots → "continue to 10 for Step 2" (no number).

**Step 2 initial:**
- ≥10 shots on one aim point → correction off the centroid of all shots.
- 2–9 shots → "shoot more" (AC-5).
- The user confirms the dialed correction (stored).

**Step 2 confirm:**
- ≥10 more shots. The tool overlays the initial and confirm groups **center on center**: each group is translated so its own centroid sits at the origin, then the 20 shots are measured as one group (P-02, user decision B-04; Form's "look at the center of both groups and overlay them visually", 22CM-FORM-020). The overlay gives the 20-shot ES (cone size) only. It never sets the zero.
- The zero is judged on the **confirm group's own centroid** relative to the aim point. If it rounds to ±0.1 mil on an axis → "a 0.1 mil touch-up is acceptable".
- If it rounds to ≥0.2 → correct and shoot another 10-shot confirm.
- Optional further 10 → 30-shot center-on-center overlay, "very high confidence".

**Step 3 (dot):**
- `zero_es_in` = ES of the 20+ (or 30) shot overlay.
- Suggested dot = the standard size (from `[0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]`) nearest to `zero_es_in − bullet_diameter_in` (P-01; reproduces Form's 1.2" → 1" example). User-overridable. Stored as `dot_size_in`.
- Completing a new zero supersedes the previous zero for that rifle+load.

**Zero check:**
- 1–2 shots at 100 yd. 0 or ≥3 shots is rejected.
- Per shot, PASS if `dist(shot, aim) ≤ dot_size_in/2 + bullet_diameter_in/2` (inside or touching).
- Any shot outside → "possible lost zero": route to Step 2 and show "check mount first (#1 cause), then the scope (#2)" (OPT-22).

**Never:** a numeric correction from a 2–9 shot group outside Step 1. Ballistic-app offsets are never suggested.

### 4.7 Dope (FR-7, D-03)
Per load: rows from finalized groups with `elevation_dialed_mil` set. Columns: distance, date, elevation dialed, group center dy (mil), **centered elevation = dialed − center_dy_mil**, shot count, includes-estimated. No MV computation.

## 5. Non-functional
- **Local only:**
  - Bind `127.0.0.1`. Config rejects any non-loopback host and exits non-zero.
  - Host-header allowlist (`127.0.0.1`, `localhost`) against DNS rebinding.
  - State-changing requests must carry `Content-Type: application/json` or multipart, and any `Origin` header must be same-origin (blocks browser CSRF from other sites).
  - No outbound network (enforced in tests by `pytest-socket` and in T3 by `lsof`).
  - No CDN assets.
- **Performance:** import → detection → stats ≤ 3 s for a 12 MP photo on Apple Silicon (measured in the spike).
- **Data:** SQLite (WAL, `foreign_keys=ON`), schema version via `PRAGMA user_version`, numbered SQL migrations. The data dir defaults to `/Users/calebschwarz/Documents/Hunting/data/` (gitignored) and is overridable with `RGA_DATA_DIR`.
- **Units:** in and MOA for size, mil for offsets/corrections. Mil/mil is always assumed.
- **Hygiene:** Python 3.14 (Homebrew, the only interpreter present), `.venv`, `pip-tools` hash-pinned lock files, non-interactive scripts with `set -euo pipefail`.
- **Extensibility:** stable `public_id` UUIDs and `updated_at` on sessions and groups, plus a pure `domain/summaries.py` renderer (P-09).

## 6. Measurement tolerances (answers open question 1, P-11)
Proposed, pending user confirmation:

| Metric | Synthetic fixtures (gate now) | Real photos (gate when user supplies 10–20) |
|---|---|---|
| Reference scale error | ≤ 0.3 % | ≤ 0.5 % vs hand measurement |
| Isolated-hole center error | p95 ≤ 0.03 in, max ≤ 0.05 in | p95 ≤ 0.05 in |
| Isolated-hole recall / false positives | ≥ 98 % / ≤ 1 per target (clean); ≥ 95 % / ≤ 3 (noisy) | ≥ 90 % / ≤ 5 (user deletes the rest) |
| Merged pair (sep ≥ 0.6 d) split | ≥ 90 % resolved, center err ≤ 0.05 in | reported, not gated |
| Ragged shortfall | exact count, 100 % inside outline | exact count (by construction) |
| Group ES (no estimated shots) | ≤ 0.05 in | ≤ 0.05 in after user review |
| Grid perspective (≤ 15° tilt) | center err p95 ≤ 0.03 in after rectification | reported |
| Runtime, 12 MP | ≤ 3 s | ≤ 3 s |

0.05 in at 100 yd ≈ 0.014 mil, roughly 7× finer than one 0.1 mil click (0.36 in).

## 7. Planning decisions (P-xx — proposed; lead promotes to `decisions.md` after user confirmation)

- **P-01 Dot suggestion rule.** Use the nearest standard dot size to `ES − bullet_diameter`, user-overridable. Form's example (22CM-FORM-020: 1.2" → 1" dot) matches. The "touching counts" pass radius `(dot + bullet)/2` then ≈ ES/2. Alternative: nearest to ES (also matches the example, but is looser for big bullets). *Open question Q-1.*
- **P-02 (REVISED by user, B-04) Step 2 overlay is center on center.** Each 10-shot group is translated to its own centroid before the union ES is measured. No dialed-correction compensation is applied, so scope tracking error can't leak into the cone size. Zero acceptance uses the confirm group's centroid. The general FR-4.4 aggregate is unchanged (aim-point aligned, D-13). *Q-2 answered.*
- **P-03 Circle-only perspective: warn only.** No ellipse correction. Warn when the axis ratio is < 0.98 (≈11° tilt, ≈2 % foreshortening on one axis) and recommend a straighter re-shoot. Answers requirements open question 2, consistent with the requirements edge-case text.
- **P-04 Offsets stored and derived in one function.** FR-2.3 says shots store in and mil offsets. Plane coordinates are the source of truth. Offsets are recomputed in a single service function on any change to aim, shots, or distance. A test asserts the invariant.
- **P-05 Shot origins `detected|split|estimated|manual`.** Split (resolved from a merged blob by the known diameter) counts as distinct. Ragged-blob shots are always `estimated`. This is what makes AC-9 produce exactly 3 estimated, not 2.
- **P-06 POI shift from same-session pairs only.** Comparing POIs across sessions assumes an unchanged scope and zero, which the app cannot know. Same-session pairing is the only defensible measure.
- **P-07 "No real difference" = relative difference ≤ 30 % of the pair mean**, as a named constant.
- **P-08 Multiple groups per photo** via a rectangular ROI per group, sharing the photo's reference transform. Form's own targets carry several aim dots (WYBTS-FORM-134: "Left dot", "Center dot"). This is additive, not a requirements conflict. *Q-6.*
- **P-09 Open Brain hook** = `public_id` UUID + `updated_at` + a pure summary renderer. There is no sync table in v1.
- **P-10 Positions** use the requirements default: `bench, prone_off_pack, kneeling_sitting, standing_field` (user removed `sandbags`, B-04). *Q-5 answered.*
- **P-11 Tolerances** per §6.
- **P-12 Stack:**
  - FastAPI + Uvicorn, Jinja2 templates, and vanilla ES-module JS with an SVG overlay editor. No Node build step and nothing for the user to maintain beyond Python.
  - Stdlib `sqlite3` with SQL migrations (no ORM).
  - OpenCV headless, Pillow + pillow-heif (iPhone AirDrop delivers HEIC).
  - Tests: pytest, pytest-socket, httpx, Playwright for T4.
  - D-17 is satisfied.
- **P-13 (REVISED by user, B-04) Compare like with like.** Two tiers. **tier10** = groups with 10–19 shots, summarized by mean ES (MOA). **tier30** = groups or saved aggregates with ≥30 shots, summarized by mean ES. 20–29 shot groups are shown but belong to no tier. `compare_pair` compares tier10 when both loads have tier10 data, and separately tier30 when both have it. A tier missing on either side → `insufficient_data` for that tier. The precision verdict (sub-2 MOA/10, LD-07) uses the tier10 mean only. `high_confidence` = ≥20 rounds in tier10. A larger group never penalizes a load. *Q-4 answered.*
- **P-14 No sa-agent.** No IAM, KMS, security group, network exposure, or state backend is in scope. The local-security surface (loopback bind, Host/Origin checks, upload parsing) is covered by review-agent plus T3 smoke checks. The lead logs this in `decisions.md`.

## 8. Testing-standard mapping (local app)
See `test-plan.md`. Summary:
- **T1:** pure units (domain math, vision helpers).
- **T2:** photo → stats against synthetic ground truth, DB schema vs every repository query, API contract vs JS client, config surface.
- **T3:** the **running local server process** with its real config, data dir, and loopback socket. That covers health, bind address, Host/Origin denial, an upload → finalize → stats round trip, on-disk DB integrity, and no non-loopback sockets.
- **T4:** Playwright journeys through the real UI, one per AC, plus failure journeys.

**AWS rules that do not apply** (declared, not skipped silently):
- All of `AWS-security-guidelines.md`: KMS, S3, Lambda, DynamoDB, RDS, EBS, API Gateway authorizers/WAF, DSQL, Amplify, resource tagging, `kms-key-usage.md`.
- The T3 cloud mechanics: stack outputs, `FunctionError`, `iam simulate-principal-policy`, dev accounts, cloud-side teardown verification.
- IaC output contracts.

Local analogues that *do* apply:
- The config-surface contract (`RGA_*` env vars read vs set by `scripts/run.sh`).
- Start → smoke → stop with a verified teardown (port freed, temp dir removed).
- Negative-path denial checks (non-loopback bind, foreign Host, cross-origin POST, bad upload).

## 9. Open questions
See the Handoff Package (Q-1..Q-8). None block Groups 1–2.
