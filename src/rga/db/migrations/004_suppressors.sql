-- 004_suppressors: suppressors the user names once and picks again; a session may name the one used.
-- sessions.suppressor_id is only meaningful when sessions.suppressor = 1 (the services keep it so);
-- deleting a suppressor leaves its sessions as plain "suppressed".
CREATE TABLE suppressors (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK (length(trim(name)) > 0),
  created_at TEXT NOT NULL
);

ALTER TABLE sessions ADD COLUMN suppressor_id INTEGER REFERENCES suppressors(id) ON DELETE SET NULL;
CREATE INDEX ix_sessions_suppressor ON sessions(suppressor_id);

PRAGMA user_version = 4;
