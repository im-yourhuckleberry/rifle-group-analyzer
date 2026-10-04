# B-66 redesign: no sessions, every group carries its own context (binding; B-67)

**Source:** a read-only architect plan, adopted by the lead with the decisions in B-67. Workers read this together with tasks.md "Group 4: B-66 redesign".

**Facts that shape the design:**
- **The user's database:** `data/rga.sqlite3` is at **schema v2**, with 1 rifle, 1 load, 3 sessions (one with no groups), 4 groups, 40 shots, 2 photos and 0 zeros. Startup applies 003, 004 and 005 in order.
- **The groups table must be rebuilt.** `groups.session_id` is `NOT NULL REFERENCES sessions(id) ON DELETE CASCADE`, so it can't simply be dropped. With foreign keys on, `DROP TABLE groups` would cascade into shots, detection_rejections, zero_checks and aggregate_groups.
- **No `rifle_id` column on groups:** the rifle always comes from the load (`groups.load_id` → `loads.rifle_id`, both NOT NULL).

## 1. Data model and migration 005

**New `groups` columns** (copy the CHECKs from `sessions`):

| Column | Definition |
|---|---|
| `shot_date` | TEXT NOT NULL CHECK `date(shot_date)=shot_date`: local calendar date `YYYY-MM-DD` |
| `shot_time` | TEXT, NULL or CHECK `time(shot_time)=shot_time`: local `HH:MM:SS`; NULL for migrated groups |
| `utc_offset` | TEXT, NULL or GLOB `[+-][0-1][0-9]:[0-5][0-9]`: information only |
| `shot_at_source` | TEXT NOT NULL IN (`photo`,`manual`,`session`), backed by a new domain enum `ShotTimeSource` |
| `distance_yd` | INTEGER NOT NULL > 0 |
| `position` | TEXT NOT NULL IN (the 4 positions) |
| `suppressor` | INTEGER NOT NULL IN (0,1) |
| `suppressor_id` | INTEGER REFERENCES suppressors(id) ON DELETE SET NULL, plus the table CHECK `(suppressor = 1 OR suppressor_id IS NULL)` |
| Weather | `temperature_f`, `density_altitude_ft`, `wind_mph`, `wind_clock` (CHECK 1..12), `weather_skipped` INTEGER NOT NULL DEFAULT 0 IN (0,1) |
| `legacy_session_id` | INTEGER with no FK: traceability |
| `legacy_session_notes` | TEXT NOT NULL DEFAULT '': the session's notes copied verbatim, shown read-only and searchable (B-16: group notes are never merged) |

- **No CHECK for "weather entered and skipped":** the service enforces it.
- **Photos:** `photos` gets `taken_at` TEXT (local `YYYY-MM-DDTHH:MM:SS`) and `taken_at_offset` TEXT.
- **Time:**
  - The calendar day is `shot_date` exactly as recorded. No timezone conversion happens anywhere.
  - Sort groups by `(shot_date, coalesce(shot_time,''), id)`.
  - `created_at`, `finalized_at` and `updated_at` stay UTC `…Z`.

**Runner directive (`db/connection.py`).** A migration whose first line is exactly `-- rga: foreign_keys=off` runs these steps:
1. **Before anything else, back up the database:** `VACUUM INTO '<db>.pre-<NNN>'`, written with mode 0600 (B-67).
2. Run `PRAGMA foreign_keys=OFF` outside any transaction and read it back. If it isn't 0, raise `MigrationError`.
3. Run `BEGIN IMMEDIATE` + the SQL, with the existing transaction and user_version checks.
4. Run `PRAGMA foreign_key_check` inside the transaction. If it returns any row, roll back and raise `MigrationError`.
5. Commit.
6. In a `finally`, run `PRAGMA foreign_keys=ON` and verify it is 1.

Every other migration runs as it does today.

**`005_group_context.sql`, a rebuild:**
1. Create `groups_new` with every existing column except `session_id`, plus the new columns. Keep every existing CHECK, including "final needs aim".
2. Copy the rows, keeping ids: `INSERT … SELECT g.*, s.date, NULL, NULL, 'session', s.distance_yd, s.position, s.suppressor, CASE WHEN s.suppressor=1 THEN s.suppressor_id END, s.<weather…>, s.id, s.notes FROM groups g LEFT JOIN sessions s ON s.id=g.session_id`. An orphan group then fails NOT NULL and aborts the migration, rather than being dropped.
3. `DROP TABLE groups`.
4. `ALTER TABLE groups_new RENAME TO groups`.
5. Recreate the indexes: `ix_groups_load_status`, `ix_groups_lot` and `ix_groups_zero`. Add `ix_groups_load_day (load_id, shot_date, distance_yd)` and `ix_groups_suppressor`.
6. Add `photos.taken_at` and `photos.taken_at_offset`.
7. `PRAGMA user_version=5`.

**`sessions` stays as a dead legacy table.** Nothing in `src/` reads it after this wave, and a grep gate enforces that. `SessionRow` stays, only to satisfy the table↔row mirror test.

**`export_truth`** opens the database read-only. It must refuse a database below the latest version with exit 2 and "start the app once to upgrade the database".

## 2. API (registry 48 → 45; design §4.7 updated by the owner of schemas.py)

**Removed:**
- `GET,POST /api/sessions`, `GET,PATCH /api/sessions/{id}`, `POST /api/sessions/{id}/groups`.
- The `/sessions/{id}` page.

**Added:**
- **`POST /api/groups`** takes `GroupCreate` and returns 201 GroupOut.
  - **Fields:**
    - load and group: load_id, lot_code, shots_expected;
    - when: shot_date?, shot_time?, utc_offset?;
    - where: distance_yd, position, suppressor, suppressor_id?;
    - weather: the 4 weather fields, weather_skipped;
    - also: elevation_dialed_mil?, purpose (general only), zero_id (must be null), photo_id?, notes.
  - **The server sets `shot_at_source`:**
    - if `shot_date` is omitted and the photo has `taken_at`, the server uses the photo's date and time and sets source `photo`;
    - if the date and time sent equal the photo's `taken_at`, source is `photo`;
    - otherwise source is `manual`;
    - no date at all and no photo date gives 422 `invalid_shot_date`.
- **`GET /api/rifles/{id}/group-defaults`** returns `GroupDefaultsOut`: source_group_id, load_id, lot_code, shots_expected, shot_date, distance_yd, position, suppressor, suppressor_id, weather and weather_skipped. Every field is null when there's nothing to copy.
  - **Source:** the rifle's highest-id group. If the rifle has none, fall back to the global latest group, for distance, position and suppressor only (B-67).

**Changed:**
- **`GroupOut` / `GroupDetailOut`:**
  - **Dropped:** `session_id`.
  - **Added:** shot_date, shot_time, utc_offset, shot_at_source, rifle_id (from the load), distance_yd, position, suppressor, suppressor_id, suppressor_name, weather, weather_skipped, and `weather_prompt` (distance ≥ 200).
  - **GroupDetailOut:** inherits `distance_yd` under the same name and adds `legacy_session_notes`.
- **`PATCH /api/groups/{id}`:** `GroupUpdate` gains the context fields. shot_time, utc_offset, suppressor_id and the weather fields are nullable.
- **`GET /api/groups`:** the query drops `session_id`.
- **`POST /api/zeros/{id}/check-groups`:** the body becomes {lot_code, shots_expected, notes, shot_date?, shot_time?, position, suppressor, suppressor_id?, photo_id?}. Distance is forced to 100.
- **`PhotoOut`:** gains `taken_at` and `taken_at_offset`.
- **`CompareGroupOut` / `DopeRowOut`:** drop session_id. `date` becomes `shot_date`, plus `shot_time`.
- **`SearchKind`:** drops "session". `legacy_session_notes` hits come back as kind `group`.
- **Weather:** entered + skipped together gives 422 `invalid_weather`. The ≥ 200 yd gate is page-enforced (B-36). Weather pre-filled from the same `shot_date` satisfies the gate.

**EXIF date (vision/io.py; security-sensitive, SE-4):**
- `_exif_taken_at(tiff)` walks at most `MAX_IFD_ENTRIES` entries of IFD0 to find 0x8769 (LONG, count 1).
- It makes **one** bounds-checked hop to the Exif sub-IFD, also capped at `MAX_IFD_ENTRIES`.
- It reads 0x9003 (falling back to 0x9004): ASCII, count 20, 19 bytes at a validated offset. It also reads 0x9011 OffsetTimeOriginal, 7 bytes.
- The value must match `^\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}$`, be a real datetime, and have a year between 1990 and next year.
- Any failure returns None. A bad date never rejects an image.
- No other IFD pointers are followed. An XMP `exif:DateTimeOriginal` fallback is optional.
- **Public API:**
  - `load_image(data) -> LoadedImage(bgr, ext, taken_at, taken_at_offset)`;
  - `load_oriented` stays as a thin wrapper;
  - `read_taken_at(data)` is for duplicate uploads.
- **`store_upload`** saves `taken_at`, and backfills it on a duplicate whose stored value is NULL.
- **Pillow** still never parses upload EXIF (B-44/B-50). The IFD spy must stay silent.

## 3. Logic

1. **Offsets (P-04/B-23):** offsets are computed at `group.distance_yd`. A PATCH that changes the distance recomputes that group's offsets in the same transaction. `recompute_session_offsets` is deleted.
2. **Zero-linked groups can't change distance.** If `zero_id` is set, a `distance_yd` change gives 409 `group_linked_to_zero` with "detach it from the zero before changing the distance". Zero-check groups are covered too. Position, suppressor, date and weather stay editable.
3. **Zeroing:** the 100 yd rule reads `group.distance_yd` in `_require_100_yd`, `_inputs` and `create_check_group`. The message no longer mentions sessions.
4. **Zero link SQL:** `repo_zeroing._LINK_GROUP` checks `groups.load_id = zeros.load_id AND (zeros.lot_id IS NULL OR zeros.lot_id = groups.lot_id)` only.
5. **Link checks:** the lot-of-load and zero load/lot checks stay. The session rifle checks and the session move guard are deleted.
6. **POI shift (replaces P-06; the user's decision in B-67):**
   - **Buckets:** eligible groups (≥ MIN_ROUNDS, unmasked) are bucketed by **(shot_date, distance_yd, position, suppressor)**, for the load and for the zero load.
   - **Shift:** pool each bucket to a mil center. The shift is the mean of (load − zero) over the shared buckets. `PoiShift.days` replaces `.sessions`, shown as "(mean of N shared day(s))".
   - **Empty note:** "shoot both loads on the same day, distance, position and suppressor setting to measure POI shift".
   - **Re-zero mid-day:** this is accepted and documented, because legacy groups have no times (B-67).
7. **New-lot flag:**
   - **Order:** `(shot_date, coalesce(shot_time,''), finalized_at, id)`.
   - **Rule:** a final group is flagged when it is the earliest finalized group of its lot **and** the load has an earlier finalized group of another lot.
   - **Drafts:** the draft rule is unchanged.
8. **Aggregates (B-50):** the mixed-distance check, the live distance, positions and suppressors all come from the groups. `aggregates.distance_yd` stays as a fallback.
9. **Dope:** order by `distance_yd, shot_date, shot_time, id`. `DopeRow` holds (group_id, shot_date, shot_time, distance_yd, elevation_dialed_mil, shot_count).
10. **Search:** hits come from `groups.notes` and `legacy_session_notes`, de-duplicated by group id.
11. **Summaries:** `group_summary(rifle_label, group)` and `day_summary(rifle_label, date, groups)` replace the session forms.
12. **Share header:** `"{shot_date}[ {HH:MM}] | {distance} yd | {position} | {suppressor label}"`, built from the group.
13. **Truth export:** distance from the group, rifle from `load.rifle_id`. The one-distance-per-photo rule stays.
14. **`weather_prompt(distance)`** moves to services/groups.py and is applied per group and to the form's live distance.
15. **Defaults (B-67):** load and lot come from the rifle's latest group. Distance, position and suppressor fall back globally. Weather is offered only when the previous group's `shot_date` equals the new group's date, and that is applied client-side after the photo's date arrives.
16. **Review page:** it gains `other_groups_on_photo`. When that is non-zero and the group has no region, show "Draw a region first" instead of auto-detecting the whole photo (the T4.0m follow-up).

## 4. UI

- **Session page:** removed, both `session.html` and `session.js`.
- **Rifle page ("Groups"):**
  - **Groups list:** grouped by day, newest first. Each day heading shows the date and its distances. Rows are ordered by time then id and show:
    - time, distance, position, suppressed badge;
    - load and lot, with the new-lot badge for unmasked groups only;
    - "n of m", size in in/MOA (none when masked), status, notes;
    - the actions from session.html (View, Review shots, Confirm reference, Import photo, Use existing photo). Existing photos are offered from this rifle's groups on the same day.
  - **Links:** `return_to` points to `/rifles/{id}`.
  - **"Add a group" card, in this order:**
    - photo (optional). It uploads as soon as it's chosen and pre-fills date and time from EXIF, showing "from photo";
    - date + optional time, distance, position;
    - the suppressor picker macro (from T4.0n);
    - weather, which expands live once distance ≥ 200, with Skip;
    - load, lot (datalist), shots, elevation, notes.
  - **Pre-fill:** from `group-defaults`.
  - **Submit:** `POST /api/groups`, then go to reference/review if there's a photo, otherwise reload.
  - **"Start a session":** removed.
- **Zeroing page:**
  - **Fields:** the session select is replaced by date/time, position, the suppressor picker and a fixed "Distance: 100 yd", plus an optional photo with EXIF pre-fill.
  - **Steps:** `POST /api/groups` with distance 100, then link. The check uses check-groups with the new body.
  - **After a step:** go to reference/review with `return_to=/zeroing/{id}`.
- **Dashboard:** "Recent groups" shows the latest 10 by (shot_date, shot_time, id) desc:
  - date/time, rifle, load/lot, distance, position, suppressed badge;
  - "n of m", status and a link.
  - No stats.
- **Group page:**
  - **Back link:** "← {rifle}", to `/rifles/{rifle_id}`.
  - **Header:** rifle · load · lot · date [time] ("from photo"/"entered") · distance · position · suppressor (name) · weather (or "weather skipped").
  - **New "Context" edit card:** uses PATCH. Changing the distance warns "recalculates mil values", and the field is disabled with its reason while the group is zero-linked.
  - **Notes:** "Group notes" (editable) and "Notes from the former session" (read-only, only when non-empty).
- **Analysis:** drop `GET /api/sessions` and read date, rifle and distance from GroupOut.
- **Wording:** in analysis.html, dope.html and dope.js.

## 5. Tests and gates

**Seed shim:**
- `seed_context(rifle, distance_yd=100, date=…, position=…, suppressor=…, notes=…, **weather) -> SeedContext`, a frozen dataclass that is not a database row.
- `seed_group(conn, load=…, session=ctx | context=ctx, …)` spreads it onto the group.
- The `session=` alias is removed in the final integration task.

**Gates to update:**
- registry count 45 and the design §4.7 parse;
- the id-bounds `BODIES`: remove the session routes, add `POST /api/groups` and the `group-defaults` path id, change the check-groups body;
- the client contract: re-target the planted mutants and recompute the thresholds;
- the domain↔db enum pairs: add `groups.position` and `groups.shot_at_source`;
- smoke step 6.4 becomes "read the group defaults (none yet)", and 6.5 becomes `POST /api/groups`. The numbering stays stable;
- the e2e nav sweep.

**New gates:**
- `tests/unit/test_no_sessions.py`: a grep that fails on `\bsessions?\b|session_id` in `src/`, outside migrations and `legacy_session*`.
- The EXIF-date io tests:
  - tracemalloc on 10,000-entry sub-IFDs;
  - out-of-bounds, self and IFD0 pointers;
  - bad count or type;
  - `0000:00:00`;
  - offset tag present or absent;
  - HEIC and PNG carriers;
  - the IFD spy stays silent.
- **005 migration tests:**
  - v2→v5 and v4→v5 with ids and child rows byte-equal;
  - FKs ON afterwards and `foreign_key_check` empty;
  - a failing 005 leaves v4 untouched with FKs ON;
  - the directive only counts on the first line;
  - the group-less session is kept;
  - the pre-005 backup file exists with mode 0600.
