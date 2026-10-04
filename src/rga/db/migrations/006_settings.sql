-- 006_settings: the first stored settings. One row per setting key; the value is a JSON document.
-- With no row the application's built-in default for that key applies.
CREATE TABLE settings (
  key TEXT PRIMARY KEY CHECK (length(key) > 0),
  value TEXT NOT NULL CHECK (json_valid(value)),
  updated_at TEXT NOT NULL
);
PRAGMA user_version = 6;
