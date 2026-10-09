-- rga: foreign_keys=off
-- 007_reference_circles: 2 inch and 2.5 inch printed circles join the reference kinds. SQLite cannot
-- alter a CHECK, so the photos table is rebuilt with the wider reference_kind list, keeping ids and
-- every column. The runner backs the database up and runs this with foreign keys off, then checks
-- foreign_key_check before it commits.
CREATE TABLE photos_new (
  id INTEGER PRIMARY KEY,
  sha256 TEXT NOT NULL UNIQUE,
  ext TEXT NOT NULL,
  width_px INTEGER NOT NULL,
  height_px INTEGER NOT NULL,
  reference_kind TEXT CHECK (reference_kind IS NULL OR reference_kind IN ('grid_1in','circle_1in','circle_1_5in','circle_2in','circle_2_5in','circle_3in')),
  reference_json TEXT,
  h_px_to_plane_json TEXT,
  working_ppi REAL,
  working_w INTEGER,
  working_h INTEGER,
  axis_ratio REAL,
  reference_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (reference_confirmed IN (0,1)),
  created_at TEXT NOT NULL,
  taken_at TEXT CHECK (taken_at IS NULL OR strftime('%Y-%m-%dT%H:%M:%S', taken_at) IS taken_at),
  taken_at_offset TEXT CHECK (taken_at_offset IS NULL OR taken_at_offset GLOB '[+-][0-1][0-9]:[0-5][0-9]'),
  CHECK (reference_confirmed = 0 OR (h_px_to_plane_json IS NOT NULL AND working_ppi IS NOT NULL))
);

INSERT INTO photos_new (
  id, sha256, ext, width_px, height_px, reference_kind, reference_json, h_px_to_plane_json,
  working_ppi, working_w, working_h, axis_ratio, reference_confirmed, created_at, taken_at,
  taken_at_offset
)
SELECT
  id, sha256, ext, width_px, height_px, reference_kind, reference_json, h_px_to_plane_json,
  working_ppi, working_w, working_h, axis_ratio, reference_confirmed, created_at, taken_at,
  taken_at_offset
FROM photos
ORDER BY id;

DROP TABLE photos;
ALTER TABLE photos_new RENAME TO photos;

PRAGMA user_version = 7;
