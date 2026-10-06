"""Open the graph + state database and bring its schema up to date (C.4).

- The database is one SQLite file, `kg.database`, in the per-user data dir
  (`paths.data_dir`), never in git.
- The schema is numbered SQL files in kg/migrations/, applied in order and recorded
  in `schema_version`, each in one transaction with its version row, so a failed
  migration leaves the database at the previous version. Applying is idempotent:
  `connect` on an up-to-date database runs nothing.
- File names: `NNNN_name.sql` is shared SQL (SQLite and Postgres); `NNNN_name.<dialect>.sql`
  runs only on that dialect. A version with files only for other dialects is recorded
  as applied with nothing run, so version numbers stay shared across dialects.
- A database recording a version this code has no file for is newer than the code:
  `connect` refuses it rather than run against a schema it doesn't know.
- The connection is in autocommit mode; writers group statements with `transaction`,
  which nests through savepoints (the same SQL on Postgres). The outermost level takes
  the write lock up front (`BEGIN IMMEDIATE`; a Postgres port uses plain `BEGIN`), so
  two writing processes queue on `kg.busy_timeout_ms` instead of failing mid-way.
- The SQLite file runs in WAL mode, so readers never block a writer's COMMIT.
- Two processes opening a fresh database can both migrate it: each version is applied
  under the write lock, version row first, and a version the other process applied
  meanwhile is skipped.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from pipeline.config import data_dir

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
DIALECT = "sqlite"
# SQLite: take the write lock at BEGIN, not at the first write, so a writer waits for
# another one up front rather than failing with "database is locked" half-way through.
BEGIN_WRITE = "BEGIN IMMEDIATE"
_MIGRATION_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)(?:\.([a-z]+))?\.sql$")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Optional[Path]          # None: this version has no file for this dialect


def migrations(directory: Path = MIGRATIONS_DIR, dialect: str = DIALECT) -> list[Migration]:
    """Every schema version, in order, with the file to run on `dialect`."""
    by_version: dict[int, dict[Optional[str], Path]] = {}
    for path in directory.iterdir():
        if path.suffix != ".sql":
            continue
        m = _MIGRATION_NAME.match(path.name)
        if not m:
            raise MigrationError(f"migration file name not NNNN_name[.dialect].sql: {path.name}")
        files = by_version.setdefault(int(m.group(1)), {})
        if m.group(3) in files:
            raise MigrationError(f"two migrations for version {m.group(1)} and dialect {m.group(3)}")
        files[m.group(3)] = path
    result = []
    for version in sorted(by_version):
        files = by_version[version]
        if None in files and len(files) > 1:
            raise MigrationError(f"version {version} has both a shared and a dialect-specific file")
        path = files.get(None) or files.get(dialect)
        name = path.name if path else f"{version:04d} (no {dialect} file)"
        result.append(Migration(version, name, path))
    return result


def utc_now(now: Optional[datetime] = None) -> str:
    """ISO-8601 UTC text, microseconds included, so stored times sort as strings."""
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="microseconds")


def stable_id(*parts: str) -> str:
    """A deterministic id from its parts, so re-writing the same thing is a no-op."""
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def new_id() -> str:
    """A fresh id for an event-like row (a run, a task event)."""
    return uuid.uuid4().hex


def fetch_one(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> Optional[dict]:
    """The first row as a dict, or None."""
    row = conn.execute(sql, tuple(params)).fetchone()
    return dict(row) if row is not None else None


def fetch_all(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> list[dict]:
    """Every row, as dicts."""
    return [dict(r) for r in conn.execute(sql, tuple(params))]


def _undo(conn: sqlite3.Connection, *statements: str) -> None:
    """Roll back after an error without hiding it. SQLite may already have rolled the
    whole transaction back on its own (SQLITE_FULL, an interrupt), so the savepoint or
    transaction can be gone; a rollback that fails then must not replace the error
    that caused it, which the caller re-raises."""
    if not conn.in_transaction:
        return
    try:
        for sql in statements:
            conn.execute(sql)
    except sqlite3.Error:
        pass


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, or a savepoint when already inside one; rolled back
    on any error, a failed COMMIT included, so the connection is never left inside a
    dead transaction that would silently swallow every later write."""
    if conn.in_transaction:
        name = f"sp_{new_id()}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
            conn.execute(f"RELEASE SAVEPOINT {name}")
        except BaseException:
            _undo(conn, f"ROLLBACK TO SAVEPOINT {name}", f"RELEASE SAVEPOINT {name}")
            raise
        return
    conn.execute(BEGIN_WRITE)
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        _undo(conn, "ROLLBACK")
        raise


def database_path(cfg: dict) -> Path:
    return data_dir(cfg) / cfg["kg"]["database"]


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    return {row[0] for row in conn.execute("SELECT version FROM schema_version")}


def migrate(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply every pending migration in order; return the versions applied."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version ("
                 "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    known = migrations(directory)
    done = applied_versions(conn)
    unknown = done - {m.version for m in known}
    if unknown:
        raise MigrationError(f"database has schema version(s) {sorted(unknown)} this code does not know; "
                             "it was written by a newer whispr")
    applied = []
    for m in known:
        if m.version in done:
            continue
        sql = m.path.read_text(encoding="utf-8") if m.path else ""
        # One script, one transaction: the schema change and its version row commit
        # together or not at all. Names come from _MIGRATION_NAME, so they hold no quote.
        # The version row goes first, under the write lock: if another process applied
        # this version since `done` was read, its primary key stops the script before
        # any schema change. (The lock can't be taken and the versions re-read before
        # the script: executescript commits any open transaction before it runs.)
        script = (f"{BEGIN_WRITE};\nINSERT INTO schema_version (version, name, applied_at) "
                  f"VALUES ({m.version}, '{m.name}', '{utc_now()}');\n{sql}\n;\nCOMMIT;")
        try:
            conn.executescript(script)
        except sqlite3.Error as exc:
            _undo(conn, "ROLLBACK")
            if m.version in applied_versions(conn):
                continue                                # the other process applied it
            raise MigrationError(f"migration {m.name} failed: {exc}") from exc
        applied.append(m.version)
    return applied


def connect(cfg: dict, *, directory: Path = MIGRATIONS_DIR) -> sqlite3.Connection:
    """The database, migrated, with foreign keys enforced and rows readable by name,
    in WAL mode, waiting up to kg.busy_timeout_ms for another process's lock.
    The caller closes it."""
    conn = sqlite3.connect(database_path(cfg), isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        # The timeout first: switching to WAL itself needs a lock another process may hold.
        conn.execute(f"PRAGMA busy_timeout = {int(cfg['kg']['busy_timeout_ms'])}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        migrate(conn, directory)
    except BaseException:
        conn.close()
        raise
    return conn
