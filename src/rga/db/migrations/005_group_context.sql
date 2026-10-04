-- rga: foreign_keys=off
-- 005_group_context: sessions are gone; every group carries its own context (date, distance,
-- position, suppressor, weather). The groups table is rebuilt without session_id, keeping ids and
-- every child row. The runner backs the database up and runs this with foreign keys off, then
-- checks foreign_key_check before it commits. The `sessions` table stays as a dead legacy table.
CREATE TABLE groups_new (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  load_id INTEGER NOT NULL REFERENCES loads(id),
  lot_id INTEGER NOT NULL REFERENCES load_lots(id),
  photo_id INTEGER REFERENCES photos(id),
  roi_json TEXT,
  shots_expected INTEGER NOT NULL CHECK (shots_expected >= 1),
  elevation_dialed_mil REAL,
  aim_x_in REAL,
  aim_y_in REAL,
  purpose TEXT NOT NULL DEFAULT 'general' CHECK (purpose IN ('general','zero_step1','zero_step2','zero_confirm','zero_check')),
  zero_id INTEGER REFERENCES zeros(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','final')),
  notes TEXT NOT NULL DEFAULT '',
  finalized_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  shot_date TEXT NOT NULL CHECK (date(shot_date) IS shot_date),
  shot_time TEXT CHECK (shot_time IS NULL OR time(shot_time) IS shot_time),
  utc_offset TEXT CHECK (utc_offset IS NULL OR utc_offset GLOB '[+-][0-1][0-9]:[0-5][0-9]'),
  shot_at_source TEXT NOT NULL CHECK (shot_at_source IN ('photo','manual','session')),
  distance_yd INTEGER NOT NULL CHECK (distance_yd > 0),
  position TEXT NOT NULL CHECK (position IN ('bench','prone_off_pack','kneeling_sitting','standing_field')),
  suppressor INTEGER NOT NULL CHECK (suppressor IN (0,1)),
  suppressor_id INTEGER REFERENCES suppressors(id) ON DELETE SET NULL,
  temperature_f REAL,
  density_altitude_ft REAL,
  wind_mph REAL,
  wind_clock INTEGER CHECK (wind_clock IS NULL OR wind_clock BETWEEN 1 AND 12),
  weather_skipped INTEGER NOT NULL DEFAULT 0 CHECK (weather_skipped IN (0,1)),
  legacy_session_id INTEGER,
  legacy_session_notes TEXT NOT NULL DEFAULT '',
  CHECK (status = 'draft' OR (aim_x_in IS NOT NULL AND aim_y_in IS NOT NULL)),
  CHECK (suppressor = 1 OR suppressor_id IS NULL)
);

INSERT INTO groups_new (
  id, public_id, load_id, lot_id, photo_id, roi_json, shots_expected, elevation_dialed_mil,
  aim_x_in, aim_y_in, purpose, zero_id, status, notes, finalized_at, created_at, updated_at,
  shot_date, shot_time, utc_offset, shot_at_source, distance_yd, position, suppressor,
  suppressor_id, temperature_f, density_altitude_ft, wind_mph, wind_clock, weather_skipped,
  legacy_session_id, legacy_session_notes
)
SELECT
  g.id, g.public_id, g.load_id, g.lot_id, g.photo_id, g.roi_json, g.shots_expected,
  g.elevation_dialed_mil, g.aim_x_in, g.aim_y_in, g.purpose, g.zero_id, g.status, g.notes,
  g.finalized_at, g.created_at, g.updated_at,
  s.date, NULL, NULL, 'session', s.distance_yd, s.position, s.suppressor,
  CASE WHEN s.suppressor = 1 THEN s.suppressor_id END,
  s.temperature_f, s.density_altitude_ft, s.wind_mph, s.wind_clock, s.weather_skipped,
  s.id, s.notes
FROM groups g LEFT JOIN sessions s ON s.id = g.session_id
ORDER BY g.id;

DROP TABLE groups;
ALTER TABLE groups_new RENAME TO groups;

CREATE INDEX ix_groups_load_status ON groups(load_id, status);
CREATE INDEX ix_groups_lot ON groups(lot_id, finalized_at);
CREATE INDEX ix_groups_zero ON groups(zero_id);
CREATE INDEX ix_groups_load_day ON groups(load_id, shot_date, distance_yd);
CREATE INDEX ix_groups_suppressor ON groups(suppressor_id);

ALTER TABLE photos ADD COLUMN taken_at TEXT CHECK (taken_at IS NULL OR strftime('%Y-%m-%dT%H:%M:%S', taken_at) IS taken_at);
ALTER TABLE photos ADD COLUMN taken_at_offset TEXT CHECK (taken_at_offset IS NULL OR taken_at_offset GLOB '[+-][0-1][0-9]:[0-5][0-9]');

PRAGMA user_version = 5;
