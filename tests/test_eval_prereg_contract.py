"""CONTRACT for eval/PREREGISTRATION.md (issue #12, B.2). Written by the build manager.

Builders must not edit this file (checksummed, G.6). The preregistration must
carry the required sections, and every hash it records must match the file on
disk, so a prompt or schema edited after registration fails this test until the
change is re-registered (or reported as exploratory).
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from pipeline import prompts
from pipeline.config import CONFIG_DIR, load_config
from pipeline.calls import canonical as _canonical
from pipeline_helpers import overlay

PREREG = Path(__file__).resolve().parent.parent / "eval" / "PREREGISTRATION.md"
REQUIRED_HEADINGS = [
    "Hypotheses", "Primary endpoint", "Metrics", "Comparison family", "Decision rules",
    "My-task bar", "Sample", "Hashes", "Graph-suite thresholds", "Deviations",
]
# Rows of the Hashes table: | `config/<relative path>` | `<sha256>` |
_ROW = re.compile(r"^\|\s*`config/([^`]+)`\s*\|\s*`([0-9a-f]{64})`\s*\|", re.MULTILINE)


def file_hash(relative: str) -> str:
    """JSON files hash in canonical form; text files hash with LF line endings."""
    path = CONFIG_DIR / relative
    if path.suffix == ".json":
        import json
        return prompts.sha256_text(_canonical(json.loads(path.read_text(encoding="utf-8"))))
    return prompts.sha256_text(path.read_text(encoding="utf-8").replace("\r\n", "\n"))


class TestPreregistration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = PREREG.read_text(encoding="utf-8")
        cls._tmp = tempfile.TemporaryDirectory()
        cls.cfg = load_config(overlay=overlay(Path(cls._tmp.name)))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_required_sections(self):
        headings = re.findall(r"^#+\s+(.+?)\s*$", self.text, re.MULTILINE)
        for h in REQUIRED_HEADINGS:
            self.assertTrue(any(x.lower().startswith(h.lower()) for x in headings), h)

    def test_every_configured_prompt_schema_and_ontology_is_hashed(self):
        rows = dict(_ROW.findall(self.text))
        wanted = [*self.cfg["prompts"].values(), *self.cfg["schemas"].values(), self.cfg["ontology"]]
        for rel in wanted:
            self.assertIn(rel, rows, f"{rel} not in the Hashes table")

    def test_hashes_match_files(self):
        for rel, digest in _ROW.findall(self.text):
            self.assertEqual(digest, file_hash(rel), f"config/{rel} changed since registration")

    def test_seed_cutoff_and_primary_endpoint(self):
        self.assertIn(str(self.cfg["eval"]["seed"]), self.text)
        self.assertIn(self.cfg["eval"]["sample"]["frame_cutoff"], self.text)
        self.assertIn("my-task recall", self.text.lower())
        self.assertIn("holm", self.text.lower())


if __name__ == "__main__":
    unittest.main()
