"""pipeline/backup.py: the daily job's backup step and `python -m pipeline restore`
(D.6, D.7, L22). Temp dirs only; the database is the real SQLite schema."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pipeline.__main__ as cli
from kg import db
from kg.store import Store
from pipeline import backup as B
from pipeline import run as runner
from test_pipeline_run import T0, PipelineCase


class BackupCase(PipelineCase):
    def setUp(self):
        super().setUp()
        self.dest = self.root / "backups"
        self.cfg["backup"]["destination"] = str(self.dest)
        self.a = self.transcript("2026-10-05T10:00:00-04:00")
        self.b = self.transcript("2026-10-05T11:00:00-04:00")
        with self.db() as (store, _):
            store.upsert_episode(self.a.stem, transcript_path=str(self.a), sha256="x")

    def make(self, at=T0) -> dict:
        with contextlib.closing(db.connect(self.cfg)) as conn:
            return B.backup(self.cfg, conn, now=at)

    def episodes(self) -> list[str]:
        with self.db() as (store, _):
            return [r[0] for r in store.conn.execute("SELECT id FROM episode ORDER BY id")]

    def restore(self, source, yes: bool) -> tuple[int, str]:
        lines: list[str] = []
        code = B.restore(self.cfg, Path(source), yes=yes, now=T0 + timedelta(hours=1), out=lines.append)
        return code, "\n".join(lines)


class TestBackup(BackupCase):
    def test_backup_is_a_verified_consistent_copy(self):
        # A writer stays open across the backup, as the 15-minute run may: a committed
        # episode that lives only in the WAL (never checkpointed), and an uncommitted write.
        writer = db.connect(self.cfg)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        Store(writer, self.cfg).upsert_episode(self.b.stem, transcript_path=str(self.b), sha256="x")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE episode SET sha256 = 'uncommitted'")
        wal = db.database_path(self.cfg).with_name(db.database_path(self.cfg).name + "-wal")
        self.assertGreater(wal.stat().st_size, 0)
        made = self.make()
        writer.rollback()
        folder = Path(made["path"])
        self.assertEqual(folder.name, self.cfg["backup"]["prefix"] + "20261005T150000Z")
        manifest = json.loads((folder / B.MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(manifest["database"]["integrity"], "ok")
        self.assertEqual(manifest["transcripts"]["count"], 2)
        self.assertEqual(sorted(p.name for p in (folder / B.TRANSCRIPTS).iterdir()), sorted([self.a.name, self.b.name]))
        copy = folder / self.cfg["kg"]["database"]
        with contextlib.closing(sqlite3.connect(copy)) as conn:
            self.assertEqual(sorted(conn.execute("SELECT id, sha256 FROM episode")),
                             sorted([(self.a.stem, "x"), (self.b.stem, "x")]))   # committed in, uncommitted out
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")   # one self-contained file
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertFalse(list(self.dest.glob("*" + B.PARTIAL)))

    def test_retention_keeps_the_newest_and_touches_nothing_else(self):
        self.cfg["backup"]["keep"] = 2
        stray = self.dest / "my-notes"
        stray.mkdir(parents=True)
        stale = self.dest / (self.cfg["backup"]["prefix"] + "20200101T000000Z" + B.PARTIAL)
        stale.mkdir()
        made = [self.make(T0 + timedelta(days=n)) for n in range(3)]
        self.assertEqual([p.name for p in B.backups(self.cfg)], [Path(m["path"]).name for m in made[1:]])
        self.assertTrue(stray.is_dir())
        self.assertFalse(stale.exists())
        self.assertIn(Path(made[0]["path"]).name, made[2]["pruned"] + made[1]["pruned"] + made[0]["pruned"])

    def test_a_hand_made_copy_with_the_prefix_is_neither_counted_nor_pruned(self):
        self.cfg["backup"]["keep"] = 1
        mine = self.dest / (self.cfg["backup"]["prefix"] + "before-upgrade")
        mine.mkdir(parents=True)
        (mine / B.MANIFEST).write_text("{}", encoding="utf-8")       # it even holds a manifest
        stray = self.dest / (self.cfg["backup"]["prefix"] + "before-upgrade" + B.PARTIAL)
        stray.mkdir()
        made = self.make()
        self.assertTrue(Path(made["path"]).is_dir())                 # keep: 1 keeps the backup just made
        self.assertEqual(B.backups(self.cfg), [Path(made["path"])])
        self.assertTrue(mine.is_dir() and stray.is_dir())
        self.assertEqual(made["pruned"], [])

    def test_retention_orders_by_stamp(self):
        self.cfg["backup"]["keep"] = 2
        made = [Path(self.make(T0 + timedelta(days=n))["path"]).name for n in (2, 0, 1)]
        self.assertEqual([p.name for p in B.backups(self.cfg)], [made[2], made[0]])

    def test_include_copies_override_data(self):
        overrides = Path(self.cfg["paths"]["data_dir"]) / "overrides" / "a.json"
        overrides.parent.mkdir(parents=True)
        overrides.write_text("{}", encoding="utf-8")
        self.cfg["backup"]["include"] = ["overrides"]
        folder = Path(self.make()["path"])
        self.assertTrue((folder / B.DATA / "overrides" / "a.json").is_file())

    def test_include_outside_the_data_dir_is_refused(self):
        for bad in ("../elsewhere", str(self.root)):
            self.cfg["backup"]["include"] = [bad]
            with self.subTest(bad), self.assertRaises(B.BackupError):
                self.make()

    def test_unset_destination_refuses(self):
        self.cfg["backup"]["destination"] = None
        with self.assertRaises(B.NotConfigured):
            self.make()

    def test_destination_inside_what_it_copies_is_refused(self):
        for key in ("data_dir", "transcripts"):
            self.cfg["backup"]["destination"] = str(Path(self.cfg["paths"][key]) / "backups")
            with self.subTest(key), self.assertRaises(B.BackupError):
                self.make()

    def test_a_copy_that_fails_verification_leaves_nothing(self):
        with mock.patch.object(B, "verify_database", side_effect=B.BackupError("integrity_check: bad page")):
            with self.assertRaises(B.BackupError):
                self.make()
        self.assertEqual(list(self.dest.iterdir()), [])


class TestRestore(BackupCase):
    def test_round_trip(self):
        folder = Path(self.make()["path"])
        with self.db() as (store, _):                      # the live state moves on, then is lost
            store.upsert_episode(self.b.stem, transcript_path=str(self.b), sha256="y")
        self.a.unlink()
        self.b.write_text("edited after the backup", encoding="utf-8")

        code, out = self.restore(folder, yes=False)       # dry run: verify and plan, change nothing
        self.assertEqual(code, runner.EXIT_OK)
        self.assertIn("nothing restored", out)
        self.assertFalse(self.a.exists())
        self.assertEqual(self.episodes(), sorted([self.a.stem, self.b.stem]))

        code, out = self.restore(folder, yes=True)
        self.assertEqual(code, runner.EXIT_OK, out)
        self.assertEqual(self.episodes(), [self.a.stem])
        self.assertTrue(self.a.is_file())                                  # missing: copied back
        self.assertEqual(self.b.read_text(encoding="utf-8"), "edited after the backup")   # differs: kept
        aside, = Path(self.cfg["paths"]["data_dir"]).glob(self.cfg["kg"]["database"] + B.PRE_RESTORE + "*")
        with contextlib.closing(sqlite3.connect(aside)) as conn:            # the replaced state is kept
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0], 2)
        with contextlib.closing(db.connect(self.cfg)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_restore_over_a_corrupt_live_database(self):
        folder = Path(self.make()["path"])
        live = Path(self.cfg["paths"]["data_dir"]) / self.cfg["kg"]["database"]
        for side in ("-wal", "-shm"):
            live.with_name(live.name + side).unlink(missing_ok=True)
        live.write_bytes(b"garbage" * 1000)                   # restore's main use: the live file is damaged
        live.with_name(live.name + "-wal").write_bytes(b"stale wal" * 100)
        code, out = self.restore(folder, yes=False)
        self.assertEqual(code, runner.EXIT_OK)
        self.assertIn("is damaged", out)
        self.assertEqual(live.read_bytes(), b"garbage" * 1000)           # the dry run changed nothing
        code, out = self.restore(folder, yes=True)
        self.assertEqual(code, runner.EXIT_OK, out)
        self.assertEqual(self.episodes(), [self.a.stem])
        aside, = Path(self.cfg["paths"]["data_dir"]).glob(self.cfg["kg"]["database"] + B.PRE_RESTORE + "*[0-9Z]")
        self.assertEqual(aside.read_bytes(), b"garbage" * 1000)          # kept, not opened
        self.assertEqual(aside.with_name(aside.name + "-wal").read_bytes(), b"stale wal" * 100)
        with contextlib.closing(db.connect(self.cfg)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_a_damaged_backup_is_refused(self):
        folder = Path(self.make()["path"])
        (folder / self.cfg["kg"]["database"]).write_bytes(b"not a database at all" * 100)
        code, out = self.restore(folder, yes=True)
        self.assertEqual(code, runner.EXIT_FAILED)
        self.assertIn("does not open", out)
        self.assertEqual(self.episodes(), [self.a.stem])

    def test_a_partial_backup_is_refused(self):
        code, out = self.restore(self.root / "nowhere", yes=True)
        self.assertEqual(code, runner.EXIT_FAILED)
        self.assertIn("not a finished backup", out)

    def test_refused_while_a_run_holds_its_lock(self):
        folder = Path(self.make()["path"])
        for key in ("lock", "daily_lock"):
            with self.subTest(key), runner.single_instance(runner.files(self.cfg, key)) as held:
                self.assertTrue(held)
                code, out = self.restore(folder, yes=True)
                self.assertEqual(code, runner.EXIT_FAILED)
                self.assertIn("holds its lock", out)

    def test_cli_defaults_to_the_dry_run(self):
        folder = Path(self.make()["path"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(cli, "load_config", return_value=self.cfg):
            self.assertEqual(cli.main(["restore", "--from", str(folder)]), runner.EXIT_OK)
        self.assertIn("nothing restored", buf.getvalue())
