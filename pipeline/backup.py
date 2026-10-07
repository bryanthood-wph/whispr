"""Backup and restore (docs/plan/D-architecture-and-ops.md D.6, D.7; lesson L22).

`backup(cfg, conn)` is the first step of `python -m pipeline daily`. One backup is a
folder `<backup.prefix><UTC stamp>` under backup.destination holding:
- `<kg.database>`: a consistent copy of the graph + state database made with the
  SQLite backup API from the job's own connection, never a file copy, so a pipeline run
  writing meanwhile cannot tear it. The copy is switched out of WAL mode, so it is one
  self-contained file.
- `data/<path>` for each backup.include path under paths.data_dir (override data).
- `transcripts/*.md`, when backup.transcripts: the rebuild source (D.6).
- `manifest.json`: what was copied, the copy's sha256 and schema versions.

It is built as `<name>.partial` and renamed only after the copy opens read-only and
passes `PRAGMA integrity_check` and every listed file is there, so a half-made backup
(a killed run) is never counted, pruned to or restored from. Then the oldest verified
backups beyond backup.keep are deleted, and any `.partial` folder a killed run left.
Only folders named exactly `<prefix><stamp>` that hold a manifest (or that plus
`.partial`) are ever counted or deleted, oldest stamp first; nothing else in the
destination is touched (a hand-made `<prefix>before-upgrade` copy is left alone).

backup.destination null refuses (NotConfigured), which the daily job turns into one
setup alert, as the runner does for an unset process_since.

`restore(cfg, source, yes=...)` is the setup restore drill (D.7) and the real thing.
Without `yes` it verifies the backup and prints what it would do, changing nothing.
With it, under both the pipeline and the daily lock (it refuses while a run holds
either), it first checks the live database as it checks a backup (verify_database):
- healthy: it is copied aside (`<kg.database>.pre-restore-<stamp>`, by the backup API)
  and the backup's database is written into it through the backup API (so a reader
  such as the MCP server, holding the file open, never stops it);
- damaged (restore's main use) or not a whispr database: the file and its -wal/-shm
  are moved aside under that same name (never opened as a database: a stale -wal must
  not be replayed onto the restored copy), and the backup is written to a fresh file.
Then it re-opens the result with kg.db.connect (WAL mode, and any migration newer than
the backup) and copies back each transcript and include file the live folders lack. An existing transcript or include
file is never overwritten: it is either the same or newer than the backup.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from kg import db
from pipeline import run as pipeline_run

MANIFEST = "manifest.json"
PARTIAL = ".partial"            # suffix of a backup still being built
DATA = "data"                   # the backup's folder for backup.include paths
TRANSCRIPTS = "transcripts"     # the backup's folder for the transcripts
STAMP = "%Y%m%dT%H%M%SZ"         # UTC, so backup names sort in time order
PRE_RESTORE = ".pre-restore-"   # the live database copied aside before a restore

SETUP_FIX = ("Set backup.destination in the config overlay to an absolute folder outside paths.data_dir and "
             "paths.transcripts (D.6: your OneDrive data folder); /whispr-setup sets it. The next daily run backs up.")


class BackupError(RuntimeError):
    """A backup could not be made or did not verify, or a restore source is unusable."""


class NotConfigured(BackupError):
    """backup.destination is unset."""


def destination(cfg: dict) -> Path:
    """backup.destination, checked: set, absolute, and not inside the folders it copies."""
    value = cfg["backup"]["destination"]
    if value is None:
        raise NotConfigured("backup.destination is not set, so there is nowhere to back up to")
    dest = Path(value)
    if not dest.is_absolute():
        raise BackupError(f"backup.destination {dest} is not an absolute path")
    for key in ("data_dir", "transcripts"):
        inside = Path(cfg["paths"][key]).resolve()
        if dest.resolve() == inside or inside in dest.resolve().parents:
            raise BackupError(f"backup.destination {dest} is inside paths.{key}: a backup would copy itself")
    return dest


def _include(cfg: dict) -> list[str]:
    """backup.include, each a relative path that stays under paths.data_dir."""
    root = Path(cfg["paths"]["data_dir"]).resolve()
    out = []
    for rel in cfg["backup"]["include"]:
        path = (root / rel).resolve()
        if Path(rel).is_absolute() or ".." in Path(rel).parts or root not in path.parents:
            raise BackupError(f"backup.include {rel!r} is not a path under paths.data_dir")
        out.append(Path(rel).as_posix())
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def copy_database(src: sqlite3.Connection, target: Path) -> None:
    """A consistent copy of `src`'s database at `target` (the SQLite backup API), as one
    self-contained file (no WAL beside it)."""
    with contextlib.closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode = DELETE")


def verify_database(path: Path) -> dict:
    """{"integrity": "ok", "schema_versions": [...]} for a database file that opens
    read-only and passes PRAGMA integrity_check; else BackupError."""
    if not path.is_file():
        raise BackupError(f"no database at {path}")
    try:
        with contextlib.closing(_open_readonly(path)) as conn:
            problems = [row[0] for row in conn.execute("PRAGMA integrity_check")]
            versions = sorted(row[0] for row in conn.execute("SELECT version FROM schema_version"))
    except sqlite3.Error as exc:
        raise BackupError(f"{path} does not open as a whispr database: {exc}") from exc
    if problems != ["ok"]:
        raise BackupError(f"{path} fails PRAGMA integrity_check: {'; '.join(problems[:5])}")
    return {"integrity": "ok", "schema_versions": versions}


def _while_locked(cfg: dict, fn: Callable, *args) -> None:
    """fn(*args), retried on PermissionError (Windows: a scanner or indexer briefly holds
    a file just written) up to backup.locked_retries times, the waits doubling from
    backup.locked_retry_s; the last error propagates."""
    retries, wait = cfg["backup"]["locked_retries"], cfg["backup"]["locked_retry_s"]
    for attempt in range(retries + 1):
        try:
            fn(*args)
            return
        except PermissionError:
            if attempt == retries:
                raise
            time.sleep(wait * 2 ** attempt)


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def stamp_of(cfg: dict, name: str) -> Optional[datetime]:
    """When the backup folder `name` (`<prefix><STAMP>`, or that plus PARTIAL) was made,
    or None for any other name: a folder whose name is not exactly this job's is never
    counted, kept in a backup.keep slot, or deleted."""
    prefix = cfg["backup"]["prefix"]
    if not name.startswith(prefix):
        return None
    rest = name[len(prefix):]
    rest = rest[:-len(PARTIAL)] if rest.endswith(PARTIAL) else rest
    try:
        return datetime.strptime(rest, STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def backups(cfg: dict) -> list[Path]:
    """The verified backups in backup.destination, oldest first (by their stamp)."""
    dest = destination(cfg)
    if not dest.is_dir():
        return []
    found = [p for p in dest.iterdir() if p.is_dir() and not p.name.endswith(PARTIAL)
             and stamp_of(cfg, p.name) is not None and (p / MANIFEST).is_file()]
    return sorted(found, key=lambda p: stamp_of(cfg, p.name))


def prune(cfg: dict) -> list[str]:
    """Delete the oldest verified backups beyond backup.keep, and every `.partial`
    folder (a killed run's); the names deleted."""
    dest = destination(cfg)
    done = backups(cfg)
    doomed = done[:max(0, len(done) - cfg["backup"]["keep"])]
    doomed += [p for p in dest.iterdir() if p.is_dir() and p.name.endswith(PARTIAL)
               and stamp_of(cfg, p.name) is not None]
    for path in doomed:
        _while_locked(cfg, shutil.rmtree, path)
    return [p.name for p in doomed]


def backup(cfg: dict, conn: sqlite3.Connection, *, now: Optional[datetime] = None) -> dict:
    """Make, verify and keep one backup (see the module docstring); its manifest, plus
    the backup's path and the folders pruned. NotConfigured / BackupError on failure,
    with nothing kept."""
    dest = destination(cfg)
    now = now or datetime.now(timezone.utc)
    name = cfg["backup"]["prefix"] + now.astimezone(timezone.utc).strftime(STAMP)
    final, work = dest / name, dest / (name + PARTIAL)
    if final.exists():
        raise BackupError(f"{final} already exists")
    include = _include(cfg)
    sources = pipeline_run.transcripts(cfg) if cfg["backup"]["transcripts"] else []
    if work.exists():
        _while_locked(cfg, shutil.rmtree, work)
    work.mkdir(parents=True)
    try:
        db_file = work / cfg["kg"]["database"]
        copy_database(conn, db_file)
        for rel in include:
            src = Path(cfg["paths"]["data_dir"]) / rel
            if not src.exists():
                raise BackupError(f"backup.include {rel!r}: {src} does not exist")
            _copy(src, work / DATA / rel)
        for path in sources:
            _copy(path, work / TRANSCRIPTS / path.name)
        manifest = {
            "created_at": db.utc_now(now),
            "database": {"file": db_file.name, "sha256": _sha256(db_file), **verify_database(db_file)},
            "include": include,
            "transcripts": {"count": len(sources), "bytes": sum(p.stat().st_size for p in sources)},
            "source": {"data_dir": str(cfg["paths"]["data_dir"]), "transcripts": str(cfg["paths"]["transcripts"])},
        }
        copied = len(list((work / TRANSCRIPTS).glob("*.md"))) if sources else 0
        if copied != len(sources):
            raise BackupError(f"{copied} of {len(sources)} transcripts were copied")
        missing = [rel for rel in include if not (work / DATA / rel).exists()]
        if missing:
            raise BackupError(f"include path(s) {missing} are missing from the copy")
        (work / MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")   # the rename below is the commit
        _while_locked(cfg, work.rename, final)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return {**manifest, "path": str(final), "pruned": prune(cfg)}


# ---- restore ------------------------------------------------------------------------

def _manifest(source: Path) -> dict:
    try:
        manifest = json.loads((source / MANIFEST).read_text(encoding="utf-8"))
        manifest["database"]["file"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BackupError(f"{source} is not a finished backup (no readable {MANIFEST}: {exc})") from exc
    return manifest


SIDECARS = ("-wal", "-shm")       # SQLite's WAL side files, moved with a damaged database


def _live_damage(live_db: Path) -> Optional[str]:
    """Why the live database is unusable, or None when it is healthy or absent."""
    if not live_db.exists():
        return None
    try:
        verify_database(live_db)
    except BackupError as exc:
        return str(exc)
    return None


def _set_aside(cfg: dict, live_db: Path, aside: Path) -> None:
    """Move a damaged database and its side files to `aside` (+ the same suffixes). The
    main file goes first, so a failure (the file is in use) leaves everything in place."""
    _while_locked(cfg, live_db.rename, aside)
    for suffix in SIDECARS:
        side = live_db.with_name(live_db.name + suffix)
        if side.exists():
            _while_locked(cfg, side.rename, aside.with_name(aside.name + suffix))


def _plan_files(pairs: list[tuple[Path, Path]]) -> tuple[list[tuple[Path, Path]], int, list[str]]:
    """(the (backup file, live path) pairs to copy back: the live one is missing; how
    many are already the same; the live paths that differ and are kept)."""
    copy, same, kept = [], 0, []
    for src, dst in pairs:
        if not dst.exists():
            copy.append((src, dst))
        elif dst.is_file() and src.is_file() and _sha256(src) == _sha256(dst):
            same += 1
        else:
            kept.append(str(dst))
    return copy, same, kept


def restore(cfg: dict, source: Path, *, yes: bool = False, now: Optional[datetime] = None,
            out: Callable[[str], None] = print) -> int:
    """Restore the backup folder `source` (see the module docstring). Exit 0 when
    restored or (without `yes`) when the plan was printed; 1 when the backup is
    unusable or a run holds a lock."""
    source, now = Path(source), now or datetime.now(timezone.utc)
    try:
        manifest = _manifest(source)
        checked = verify_database(source / manifest["database"]["file"])
    except BackupError as exc:
        out(f"restore: {exc}")
        return pipeline_run.EXIT_FAILED
    newer = sorted(set(checked["schema_versions"]) - {m.version for m in db.migrations()})
    if newer:
        out(f"restore: {source} has schema version(s) {newer} this code does not know: it was made by a newer whispr")
        return pipeline_run.EXIT_FAILED
    live_db = Path(cfg["paths"]["data_dir"]) / cfg["kg"]["database"]
    folder = Path(cfg["paths"]["transcripts"])
    pairs = [(p, folder / p.name) for p in sorted((source / TRANSCRIPTS).glob("*.md"))]
    pairs += [(source / DATA / rel, Path(cfg["paths"]["data_dir"]) / rel) for rel in manifest.get("include", [])]
    files, same, kept = _plan_files(pairs)
    aside = live_db.with_name(live_db.name + PRE_RESTORE + now.astimezone(timezone.utc).strftime(STAMP))
    damage = _live_damage(live_db)
    out(f"restore from {source} (made {manifest.get('created_at')}, integrity ok, schema {checked['schema_versions']}):")
    if damage:
        out(f"  database: {live_db} is damaged ({damage}); it and its side files are moved to {aside.name} first")
    else:
        out(f"  database: {live_db} " + (f"is replaced; the current one is copied to {aside.name} first"
                                         if live_db.exists() else "is created (none exists)"))
    out(f"  files: {len(files)} copied back (missing here), {same} already the same, {len(kept)} kept (they differ "
        "here and are never overwritten)")
    for path in kept:
        out(f"    kept {path}")
    if not yes:
        out("nothing restored: re-run with --yes to restore")
        return pipeline_run.EXIT_OK
    with pipeline_run.single_instance(pipeline_run.files(cfg, "lock")) as a, \
            pipeline_run.single_instance(pipeline_run.files(cfg, "daily_lock")) as b:
        if not (a and b):
            out("restore: a pipeline or daily run holds its lock; nothing restored. Try again once it has finished.")
            return pipeline_run.EXIT_FAILED
        existed, damage = live_db.exists(), _live_damage(live_db)
        backup_file = source / manifest["database"]["file"]
        if existed and damage is None:
            with contextlib.closing(db.connect(cfg)) as live:
                copy_database(live, aside)
                with contextlib.closing(_open_readonly(backup_file)) as src:
                    src.backup(live)
        else:
            if existed:
                try:
                    _set_aside(cfg, live_db, aside)
                except OSError as exc:
                    out(f"restore: the damaged {live_db} could not be moved aside ({exc}); is a reader holding it "
                        "open (the MCP server)? Nothing restored.")
                    return pipeline_run.EXIT_FAILED
            live_db.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(_open_readonly(backup_file)) as src:
                copy_database(src, live_db)
        with contextlib.closing(db.connect(cfg)):      # WAL mode again, and any migration newer than the backup
            pass
        for src, dst in files:
            _copy(src, dst)
    pipeline_run.job_log(cfg, "restore")({"event": "restore", "source": str(source), "files": len(files),
                                          "kept": len(kept), "aside": str(aside) if existed else None,
                                          "damaged": damage})
    out(f"restored {live_db}" + (f" (the previous one is {aside})" if existed else "")
        + f" and {len(files)} file(s)")
    return pipeline_run.EXIT_OK
