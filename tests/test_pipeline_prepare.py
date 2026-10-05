"""pipeline/prepare.py (issue #9, D.4). Synthetic transcripts only; no model calls."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from pipeline import prepare as P
from pipeline.config import load_config
from pipeline_helpers import overlay, transcript

FILLER = "we walked through the quarterly plan and the staffing model in detail today"


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = load_config(overlay=overlay(self.root))
        self.n = 0

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, *args, **kw) -> P.Transcript:
        self.n += 1
        path = self.root / f"t{self.n}.md"
        path.write_text(transcript(*args, **kw), encoding="utf-8")
        return P.parse(path)

    def prep(self, *args, **kw) -> P.Prepared:
        return P.prepare([self.write(*args, **kw)], self.cfg)


class TestParseAndStub(_Base):
    def test_parse_turns_and_meta(self):
        t = self.write("2026-10-01T10:00:00-04:00", [("00:00:05", "Me", "hello there"), ("01:02:03", "Others", "hi")])
        self.assertEqual([(x.seconds, x.speaker) for x in t.turns], [(5, "Me"), (3723, "Others")])
        self.assertEqual(t.meta["call_title"], "Weekly Sync")

    def test_zero_turns_is_stub(self):
        self.assertTrue(self.prep("2026-10-01T10:00:00-04:00", []).is_stub)

    def test_few_words_is_stub(self):
        self.assertTrue(self.prep("2026-10-01T10:00:00-04:00", [("00:00:01", "Me", "hello?")]).is_stub)

    def test_enough_words_is_not_stub(self):
        turns = [("00:00:01", "Others", FILLER)] * 5
        out = self.prep("2026-10-01T10:00:00-04:00", turns)
        self.assertFalse(out.is_stub)
        self.assertEqual(out.render().splitlines()[0], f"[00:00:01] Others: {FILLER}")


class TestEcho(_Base):
    def test_repeated_line_dropped_short_reply_and_distant_line_kept(self):
        turns = [
            ("00:00:10", "Others", "please send the revised deck to finance by friday"),
            ("00:00:11", "Me", "send the revised deck to finance by friday"),   # echo -> dropped
            ("00:00:12", "Me", "yes"),                                         # too short -> kept
            ("00:05:00", "Me", "send the revised deck to finance by friday"),   # outside window -> kept
        ]
        out = self.prep("2026-10-01T10:00:00-04:00", turns)
        self.assertEqual([t.stamp for t in out.echo_dropped], ["00:00:11"])
        self.assertEqual([t.text for t in out.turns if t.speaker == "Me"],
                         ["yes", "send the revised deck to finance by friday"])


class TestRedaction(_Base):
    def test_invite_artifacts_removed_spoken_word_kept(self):
        turns = [
            ("00:00:01", "Others", "Join: https://teams.microsoft.com/meet/361646951846640?p=pBlBbv3Gy5g8XJ2FyY now"),
            ("00:00:02", "Others", "Meeting ID: 361 646 951 846 640 Passcode: qJ7xK2"),
            ("00:00:03", "Others", "Dial in by phone +1 470-555-0100,,123456789# United States"),
            ("00:00:04", "Me", "I had to change passcode on my phone"),
        ]
        out = self.prep("2026-10-01T10:00:00-04:00", turns)
        text = out.render()
        for leak in ("teams.microsoft.com", "pBlBbv3", "qJ7xK2", "361 646", "123456789"):
            self.assertNotIn(leak, text)
        self.assertIn("change passcode on my phone", text)
        self.assertGreaterEqual(out.redactions, 4)

    def test_new_domain_upper_case_and_attendee_dial_in_redacted(self):
        out = self.prep("2026-10-01T10:00:00-04:00",
                        [("00:00:01", "Others", "HTTPS://Teams.Cloud.Microsoft/meet/3616?p=pBlBbv3 ok")],
                        attendees=["Doe, Jane", "+1 470-555-0100,,123456789#"])
        self.assertNotIn("pBlBbv3", out.render())
        self.assertNotIn("123456789", str(out.meta["attendees"]))
        self.assertIn("Jane Doe", out.meta["attendees"])

    def test_invite_notes_never_reach_meta(self):
        out = self.prep("2026-10-01T10:00:00-04:00", [], extra_frontmatter="invite_notes: 'Passcode: x1'\n")
        self.assertNotIn("invite_notes", out.meta)
        self.assertNotIn("x1", str(out.meta))


class TestMetadata(_Base):
    def test_attendees_are_people(self):
        out = self.prep("2026-10-01T10:00:00-04:00", [], attendees=[
            "Doe, Jane", "cohood@example.com", "Example (TEN)", "Microsoft Teams", "Doe, Jane", "Sam Lee"])
        self.assertEqual(out.meta["attendees"], ["Jane Doe", "Sam Lee"])

    def _title(self, raw):
        return self.prep("2026-10-01T10:00:00-04:00", [], metadata_source="window-title",
                         call_title=f"'{raw}'").meta

    def test_all_generic_window_title_becomes_null(self):
        meta = self._title("Meeting join | Microsoft Teams meeting | Example (TEN) | pat@example.com | Microsoft Teams")
        self.assertIsNone(meta["call_title"])
        self.assertEqual(meta["call_type"], "call")

    def test_generic_segments_dropped_real_ones_kept(self):
        self.assertEqual(self._title("Doe, Jane | Microsoft Teams")["call_title"], "Doe, Jane")
        self.assertEqual(self._title("Meeting join | Project Kickoff | Microsoft Teams")["call_title"],
                         "Project Kickoff")

    def test_real_title_kept(self):
        self.assertEqual(self.prep("2026-10-01T10:00:00-04:00", []).meta["call_title"], "Weekly Sync")


class TestAliases(_Base):
    def test_rewrite_and_table_validation(self):
        t = self.write("2026-10-01T10:00:00-04:00", [("00:00:01", "Others", "the terra dine pipeline and Terradyne data")])
        out = P.prepare([t], self.cfg, {"Teradyne": ["terra dine", "Terradyne"]})
        self.assertIn("the Teradyne pipeline and Teradyne data", out.render())
        self.assertEqual(out.aliases_rewritten, 2)
        bad = self.root / "aliases.yaml"
        bad.write_text("Teradyne: terra dine\n", encoding="utf-8")
        with self.assertRaises(P.PrepareError):
            P.load_aliases(bad)
        self.assertEqual(P.load_aliases(self.root / "missing.yaml"), {})

    def test_canonical_text_not_rewritten_again_and_backslash_safe(self):
        rewrite = lambda text, table: P.rewrite_aliases(text, P.compile_aliases(table))
        self.assertEqual(rewrite("Jamie Doe said, then jamie left", {"Jamie Doe": ["Jamie"]}),
                         ("Jamie Doe said, then Jamie Doe left", 1))
        self.assertEqual(rewrite("the r and d team", {r"R\D": ["r and d"]}), (r"the R\D team", 1))
        self.assertEqual(rewrite("Jamieson spoke", {"Jamie Doe": ["Jamie"]}), ("Jamieson spoke", 0))  # whole words only


class TestEpisodes(_Base):
    def _pair(self, second_start, title2="Weekly Sync"):
        a = self.write("2026-10-01T10:00:00-04:00", [("00:00:10", "Others", "part one")])
        a.meta["end"] = "2026-10-01T10:20:00-04:00"
        b = self.write(second_start, [("00:00:05", "Others", "part two")], call_title=title2)
        return a, b

    def test_split_meeting_merged_with_offset(self):
        a, b = self._pair("2026-10-01T10:23:00-04:00")
        groups = P.group_episodes([b, a], self.cfg)
        self.assertEqual([len(g) for g in groups], [2])
        merged = P.prepare(groups[0], self.cfg)
        self.assertEqual([t.stamp for t in merged.turns], ["00:00:10", "00:23:05"])

    def test_long_gap_or_other_title_not_merged(self):
        a, b = self._pair("2026-10-01T11:00:00-04:00")
        self.assertEqual(len(P.group_episodes([a, b], self.cfg)), 2)
        a, b = self._pair("2026-10-01T10:23:00-04:00", title2="Other Meeting")
        self.assertEqual(len(P.group_episodes([a, b], self.cfg)), 2)


class TestEncodingAndKey(_Base):
    def test_mojibake_rejected(self):
        with self.assertRaises(P.PrepareError):
            self.prep("2026-10-01T10:00:00-04:00", [("00:00:01", "Me", "itΓÇÖs broken")])

    def test_key_stable_and_config_sensitive(self):
        t = self.write("2026-10-01T10:00:00-04:00", [("00:00:01", "Me", "hello")])
        k1 = P.prepare([t], self.cfg).key
        self.assertEqual(k1, P.prepare([t], self.cfg).key)
        cfg2 = copy.deepcopy(self.cfg)
        cfg2["prepare"]["echo_overlap"] = 0.5
        self.assertNotEqual(k1, P.prepare([t], cfg2).key)
        self.assertNotEqual(k1, P.prepare([t], self.cfg, {"X": ["y"]}).key)


if __name__ == "__main__":
    unittest.main()
