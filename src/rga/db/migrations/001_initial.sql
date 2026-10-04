-- 001_initial: full v1 schema. Applied by rga.db.connection.migrate().
PRAGMA foreign_keys = ON;

CREATE TABLE rifles (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  cartridge TEXT NOT NULL,
  bullet_diameter_in REAL NOT NULL CHECK (bullet_diameter_in > 0.1 AND bullet_diameter_in < 0.6),
  scope TEXT,
  notes TEXT NOT NULL DEFAULT '',
  zero_load_id INTEGER REFERENCES loads(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE loads (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  rifle_id INTEGER NOT NULL REFERENCES rifles(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('factory','handload')),
  label TEXT NOT NULL,
  maker TEXT, product TEXT, bullet_weight_gr REAL, bullet_type TEXT,          -- factory
  bullet TEXT, powder TEXT, charge_gr REAL, primer TEXT,                       -- handload
  trued_mv_fps REAL CHECK (trued_mv_fps IS NULL OR trued_mv_fps > 0),
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX ix_loads_rifle ON loads(rifle_id);

CREATE TABLE load_lots (
  id INTEGER PRIMARY KEY,
  load_id INTEGER NOT NULL REFERENCES loads(id) ON DELETE CASCADE,
  lot_code TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (load_id, lot_code)
);

CREATE TABLE sessions (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  rifle_id INTEGER NOT NULL REFERENCES rifles(id) ON DELETE CASCADE,
  date TEXT NOT NULL,
  distance_yd INTEGER NOT NULL CHECK (distance_yd > 0),
  position TEXT NOT NULL CHECK (position IN ('bench','prone_off_pack','kneeling_sitting','standing_field')),
  suppressor INTEGER NOT NULL CHECK (suppressor IN (0,1)),
  temperature_f REAL,
  density_altitude_ft REAL,
  wind_mph REAL,
  wind_clock INTEGER CHECK (wind_clock IS NULL OR wind_clock BETWEEN 1 AND 12),
  weather_skipped INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX ix_sessions_rifle_date ON sessions(rifle_id, date);

CREATE TABLE photos (
  id INTEGER PRIMARY KEY,
  sha256 TEXT NOT NULL UNIQUE,
  ext TEXT NOT NULL,
  width_px INTEGER NOT NULL,
  height_px INTEGER NOT NULL,
  reference_kind TEXT CHECK (reference_kind IS NULL OR reference_kind IN ('grid_1in','circle_1in','circle_1_5in','circle_3in')),
  reference_json TEXT,
  h_px_to_plane_json TEXT,
  working_ppi REAL,
  working_w INTEGER,
  working_h INTEGER,
  axis_ratio REAL,
  reference_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (reference_confirmed IN (0,1)),
  created_at TEXT NOT NULL,
  CHECK (reference_confirmed = 0 OR (h_px_to_plane_json IS NOT NULL AND working_ppi IS NOT NULL))
);

CREATE TABLE zeros (
  id INTEGER PRIMARY KEY,
  rifle_id INTEGER NOT NULL REFERENCES rifles(id) ON DELETE CASCADE,
  load_id INTEGER NOT NULL REFERENCES loads(id) ON DELETE CASCADE,
  lot_id INTEGER REFERENCES load_lots(id),
  mode TEXT NOT NULL CHECK (mode IN ('new_setup','existing')),
  state TEXT NOT NULL CHECK (state IN ('step1','step2_initial','step2_confirm','step3_dot','complete','superseded')),
  dialed_up_mil REAL,
  dialed_right_mil REAL,
  zero_es_in REAL,
  zero_shot_count INTEGER,
  dot_size_in REAL CHECK (dot_size_in IS NULL OR dot_size_in > 0),
  completed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX ix_zeros_rifle_load ON zeros(rifle_id, load_id, state);

CREATE TABLE groups (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
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
  CHECK (status = 'draft' OR (aim_x_in IS NOT NULL AND aim_y_in IS NOT NULL))
);
CREATE INDEX ix_groups_session ON groups(session_id);
CREATE INDEX ix_groups_load_status ON groups(load_id, status);
CREATE INDEX ix_groups_lot ON groups(lot_id, finalized_at);
CREATE INDEX ix_groups_zero ON groups(zero_id);

CREATE TABLE shots (
  id INTEGER PRIMARY KEY,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  x_in REAL NOT NULL,
  y_in REAL NOT NULL,
  dx_in REAL,
  dy_in REAL,
  dx_mil REAL,
  dy_mil REAL,
  origin TEXT NOT NULL CHECK (origin IN ('detected','split','estimated','manual')),
  moved_by_user INTEGER NOT NULL DEFAULT 0 CHECK (moved_by_user IN (0,1)),
  confidence REAL,
  UNIQUE (group_id, seq)
);

CREATE TABLE detection_rejections (
  id INTEGER PRIMARY KEY,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  x_in REAL NOT NULL,
  y_in REAL NOT NULL,
  reason TEXT NOT NULL CHECK (reason = 'not_a_bullet_hole'),
  created_at TEXT NOT NULL
);

CREATE TABLE zero_checks (
  id INTEGER PRIMARY KEY,
  zero_id INTEGER NOT NULL REFERENCES zeros(id) ON DELETE CASCADE,
  group_id INTEGER NOT NULL UNIQUE REFERENCES groups(id) ON DELETE CASCADE,
  result TEXT NOT NULL CHECK (result IN ('pass','possible_lost_zero')),
  created_at TEXT NOT NULL
);

PRAGMA user_version = 1;
