"""SQLite connection factory and forward-only migration runner.

Migrations are `migrations/NNN_<name>.sql` package-data files. A file is applied when
NNN is greater than the database's `PRAGMA user_version`; each file runs in one transaction
and must itself set `PRAGMA user_version = NNN` (verified before the commit).

A file whose first line is exactly `-- rga: foreign_keys=off` is a table rebuild. Before the first
pending migration is applied, the runner writes one backup (`VACUUM INTO '<db>.pre-NNN'`, mode 0600,
NNN the first such file; only for a file database that already existed, not one created by this
run). It turns foreign keys off outside any transaction and verifies it, runs the file in one
transaction, requires `PRAGMA foreign_key_check` to return nothing before it commits, and turns
foreign keys back on (verified) whatever happened.
"""

from __future__ import annotations

import os
import re
import sqlite3
from importlib import resources
from pathlib import Path

_MIGRATION_NAME = re.compile(r"^(\d{3})_[A-Za-z0-9_]+\.sql$")
FOREIGN_KEYS_OFF_DIRECTIVE = "-- rga: foreign_keys=off"
BACKUP_MODE = 0o600


class MigrationError(RuntimeError):
    """Raised when migration files are malformed or the database is newer than the code."""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open `path` with foreign keys ON, WAL journaling, `sqlite3.Row` rows and no type detection.

    The connection is intended for one request or task at a time (not shared concurrently),
    but may be created and used on different threads.
    """
    conn = sqlite3.connect(path, detect_types=0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the database's `PRAGMA user_version`."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _migration_files() -> list[tuple[int, str, str]]:
    """Return `(version, filename, sql)` for every packaged migration, ordered by version."""
    found: dict[int, tuple[str, str]] = {}
    for entry in resources.files("rga.db").joinpath("migrations").iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match is None:
            continue
        version = int(match.group(1))
        if version in found:
            raise MigrationError(f"duplicate migration number {version:03d}")
        found[version] = (entry.name, entry.read_text(encoding="utf-8"))
    return [(v, name, sql) for v, (name, sql) in sorted(found.items())]


def latest_version() -> int:
    """Return the highest packaged migration number (0 if none)."""
    files = _migration_files()
    return files[-1][0] if files else 0


def _wants_foreign_keys_off(sql: str) -> bool:
    return sql.split("\n", 1)[0].rstrip("\r") == FOREIGN_KEYS_OFF_DIRECTIVE


def _database_file(conn: sqlite3.Connection) -> Path | None:
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row["file"]) if row is not None and row["file"] else None


def backup_database(conn: sqlite3.Connection, version: int) -> Path | None:
    """Write `<db>.pre-NNN` (mode 0600) with `VACUUM INTO` and return its path.

    A backup that already exists is never replaced: the next free `<db>.pre-NNN.<n>` is used.
    Returns None for a database without a file (in memory). Raises `MigrationError` when the
    connection is inside a transaction, which `VACUUM` cannot run in.
    """
    source = _database_file(conn)
    if source is None:
        return None
    if conn.in_transaction:
        raise MigrationError("cannot back up the database inside a transaction")
    base = source.with_name(f"{source.name}.pre-{version:03d}")
    target, suffix = base, 0
    while True:
        try:
            os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, BACKUP_MODE))
            break
        except FileExistsError:
            suffix += 1
            target = base.with_name(f"{base.name}.{suffix}")
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
        os.chmod(target, BACKUP_MODE)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def _foreign_keys(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA foreign_keys").fetchone()[0])


def _apply(conn: sqlite3.Connection, version: int, name: str, sql: str) -> None:
    """Run one migration in one transaction; commit only after user_version is verified."""
    try:
        conn.executescript(f"BEGIN IMMEDIATE;\n{sql}")
        if not conn.in_transaction:
            raise MigrationError(f"{name} ended its own transaction (COMMIT/ROLLBACK/END)")
        applied = schema_version(conn)
        if applied != version:
            raise MigrationError(f"{name} left user_version at {applied}, expected {version}")
        if _wants_foreign_keys_off(sql):
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                tables = sorted({str(row[0]) for row in violations})
                raise MigrationError(
                    f"{name} left {len(violations)} foreign key violation(s) in {', '.join(tables)}"
                )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def _apply_with_foreign_keys_off(
    conn: sqlite3.Connection, version: int, name: str, sql: str
) -> None:
    if conn.in_transaction:
        raise MigrationError(f"{name} needs foreign keys off, which cannot change in a transaction")
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        if _foreign_keys(conn) != 0:
            raise MigrationError(f"{name} could not turn foreign keys off")
        _apply(conn, version, name, sql)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        if _foreign_keys(conn) != 1:
            raise MigrationError(f"{name} could not turn foreign keys back on")


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations in order and return the resulting schema version.

    Idempotent: with nothing pending it changes nothing. Each file runs in one transaction that
    is committed only after `user_version` is verified. A file that is not a complete script
    (unterminated comment, string or statement), does not set the expected `user_version`, or
    ends its own transaction raises `MigrationError`; a failure leaves the database unchanged.
    A file starting with the `-- rga: foreign_keys=off` directive is backed up and checked as the
    module docstring describes. Also raises `MigrationError` if the database is newer than the
    packaged migrations.
    """
    files = _migration_files()
    newest = files[-1][0] if files else 0
    current = schema_version(conn)
    if current > newest:
        raise MigrationError(f"database schema version {current} is newer than code ({newest})")
    pending = [(v, n, q) for v, n, q in files if v > current]
    for _, name, sql in pending:
        if not sqlite3.complete_statement(sql):
            raise MigrationError(
                f"{name} is not a complete SQL script (unterminated comment, string or statement)"
            )
    rebuilds = [v for v, _, q in pending if _wants_foreign_keys_off(q)]
    if rebuilds and current > 0:
        backup_database(conn, rebuilds[0])
    for version, name, sql in pending:
        if _wants_foreign_keys_off(sql):
            _apply_with_foreign_keys_off(conn, version, name, sql)
        else:
            _apply(conn, version, name, sql)
    return schema_version(conn)
