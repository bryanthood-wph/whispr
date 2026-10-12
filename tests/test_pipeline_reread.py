"""What a run no longer reads twice: transcript hashes kept by size and mtime
(pipeline/run.py Hashes) and the ledger read only where it grew
(pipeline/models.py read_jsonl_appended). Temp dirs only; no model call."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from pipeline import models, prepare
from pipeline import run as runner
from test_pipeline_run import T0, FakeAsk, RunCase


SECOND_NS = 10**9
TICK_NS = 10**8          # coarser than any filesystem's mtime resolution (NTFS: 100 ns)


def bump_mtime(path: Path, ns: int) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, ns))


class TestHashes(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.file, self.cache = self.root / "2026-10-05-1000-t.md", self.root / "hashes.json"
        self.file.write_text("first", encoding="utf-8")
        bump_mtime(self.file, self.file.stat().st_mtime_ns - SECOND_NS)   # older than any save below

    def hashed(self) -> tuple[str, int]:
        """(the sha256 a fresh Hashes gives, how many files it read), saved after."""
        with mock.patch.object(prepare, "file_sha256", wraps=prepare.file_sha256) as read:
            h = runner.Hashes(self.cache)
            sha = h.sha256(self.file)
            h.save()
        return sha, read.call_count

    def test_an_unchanged_file_is_read_once_then_served_from_the_file(self):
        self.assertEqual(self.hashed(), (prepare.file_sha256(self.file), 1))
        self.assertEqual(self.hashed(), (prepare.file_sha256(self.file), 0))

    def test_a_same_size_edit_with_a_new_mtime_is_read_again(self):
        self.hashed()
        old = self.file.stat().st_mtime_ns
        self.file.write_text("other", encoding="utf-8")                  # same size
        bump_mtime(self.file, old + TICK_NS)                            # still older than the save
        self.assertEqual(self.hashed(), (prepare.file_sha256(self.file), 1))

    def test_a_racy_entry_is_read_again(self):
        self.hashed()
        bump_mtime(self.file, self.cache.stat().st_mtime_ns)            # not older than the save
        self.assertEqual(self.hashed()[1], 1)

    def test_an_unreadable_cache_file_means_every_file_is_read(self):
        self.cache.write_text("not json", encoding="utf-8")
        self.assertEqual(self.hashed()[1], 1)
        self.assertIn(self.file.name, json.loads(self.cache.read_text(encoding="utf-8")))

    def test_nothing_hashed_writes_nothing(self):
        runner.Hashes(self.cache).save()
        self.assertFalse(self.cache.exists())


class TestRunHashes(RunCase):
    def test_a_same_size_edit_is_still_requeued(self):
        path = self.transcript("2026-10-05T10:00:00-04:00")
        bump_mtime(path, path.stat().st_mtime_ns - SECOND_NS)
        ask = FakeAsk()
        self.go(ask)                                    # writes the episode
        with mock.patch.object(prepare, "file_sha256", wraps=prepare.file_sha256) as read:
            self.go(ask, now=T0 + timedelta(minutes=15))        # hashes it once, then serves it from the file
            self.go(ask, now=T0 + timedelta(minutes=30))
        self.assertEqual(read.call_count, 1)
        text = path.read_text(encoding="utf-8")
        edited = text.replace("Jamie owns the deck", "Jamie owns the plan")     # same length
        self.assertEqual(len(edited), len(text))
        old = path.stat().st_mtime_ns
        path.write_text(edited, encoding="utf-8")
        bump_mtime(path, old + TICK_NS)
        self.go(ask, now=T0 + timedelta(minutes=45))
        self.assertEqual(self.runs()[-1]["changed"], 1)
        self.assertEqual(ask.refs, [path.stem, path.stem])

    def test_a_dry_run_writes_no_hash_file(self):
        self.transcript("2026-10-05T10:00:00-04:00")
        self.go(dry_run=True)
        self.assertFalse(runner.files(self.cfg, "hashes", create=False).exists())


class TestReadJsonlAppended(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "ledger.jsonl"

    def same(self) -> None:
        self.assertEqual(models.read_jsonl_appended(self.path), models.read_jsonl(self.path))

    def test_appends_torn_lines_and_a_replaced_file_read_as_read_jsonl_reads_them(self):
        self.same()                                                     # missing: empty
        models.append_jsonl(self.path, {"n": 1})
        self.same()
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write('{"n": 2}\n["not an object"]\n\n{"n": 3')         # a torn last line
        self.same()
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(', "done": true}\n')                              # its newline lands
        self.same()
        self.assertEqual(models.read_jsonl_appended(self.path)[-1], {"n": 3, "done": True})
        self.path.write_text('{"n": 9}\n', encoding="utf-8")             # replaced, shorter
        self.same()
        self.path.write_text('{"n": 8}\n{"n": 7}\n{"n": 6}\n', encoding="utf-8")   # replaced, longer
        self.same()
        self.path.unlink()
        self.same()


if __name__ == "__main__":
    unittest.main()
