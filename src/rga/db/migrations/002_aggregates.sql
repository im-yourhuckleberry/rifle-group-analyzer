-- 002_aggregates: saved combinations of finalized groups of one load (feed the tier30 comparison).
PRAGMA foreign_keys = ON;

CREATE TABLE aggregates (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  rifle_id INTEGER NOT NULL REFERENCES rifles(id) ON DELETE CASCADE,
  load_id INTEGER NOT NULL REFERENCES loads(id) ON DELETE CASCADE,
  distance_yd INTEGER NOT NULL CHECK (distance_yd > 0),
  group_count INTEGER NOT NULL CHECK (group_count >= 2),
  created_at TEXT NOT NULL
);
CREATE INDEX ix_aggregates_load ON aggregates(load_id);

CREATE TABLE aggregate_groups (
  aggregate_id INTEGER NOT NULL REFERENCES aggregates(id) ON DELETE CASCADE,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  PRIMARY KEY (aggregate_id, group_id)
);
CREATE INDEX ix_aggregate_groups_group ON aggregate_groups(group_id);

PRAGMA user_version = 2;
