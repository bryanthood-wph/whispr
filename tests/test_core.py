"""whispr unit tests (stdlib unittest — no extra deps).

Run: .venv\\Scripts\\python.exe -m unittest discover -s tests -v
Covers everything that does NOT require a live Teams call, real audio hardware, or
the Anthropic API. Those are live gates tracked in PROGRESS.md.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from whispr.config import load_config
from whispr.models import (
    PENDING_SUMMARY_TOPIC,
    CallMetadata,
    CallSession,
    TranscriptResult,
    Turn,
)

CFG = load_config()

# E1: redirect transcript writes to an isolated temp dir so tests never touch the
# real transcripts directory, even if a test fails or raises mid-run.
_REAL_TRANSCRIPTS: Optional[Path] = None
_TEMP_DIR: Optional[tempfile.TemporaryDirectory] = None


def setUpModule():
    global _REAL_TRANSCRIPTS, _TEMP_DIR
    _REAL_TRANSCRIPTS = CFG["paths"]["transcripts"]
    _TEMP_DIR = tempfile.TemporaryDirectory()
    CFG["paths"]["transcripts"] = Path(_TEMP_DIR.name)


def tearDownModule():
    global _TEMP_DIR
    if _REAL_TRANSCRIPTS is not None:
        CFG["paths"]["transcripts"] = _REAL_TRANSCRIPTS
    if _TEMP_DIR is not None:
        _TEMP_DIR.cleanup()
        _TEMP_DIR = None


def _session(**kw):
    start = datetime(2026, 7, 14, 10, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
    base = dict(
        call_type="meeting", window_title="Weekly Sync | Microsoft Teams",
        start=start, end=start + timedelta(minutes=30), mic_wav="m.wav", loopback_wav="l.wav",
    )
    base.update(kw)
    return CallSession(**base)


class TestClassify(unittest.TestCase):
    def test_session_vs_end_vs_none(self):
        from whispr.watcher import classify_title
        # A call/meeting window (subject | Microsoft Teams, no nav prefix) = session.
        self.assertEqual(classify_title("Virkar, Jayesh | Microsoft Teams", CFG), "session")
        self.assertEqual(classify_title("Sprint Review | Microsoft Teams", CFG), "session")
        # Home/nav = end.
        self.assertEqual(classify_title("Activity | Microsoft Teams", CFG), "end")
        self.assertEqual(classify_title("Microsoft Teams", CFG), "end")
        # A chat window is neither a session nor (necessarily) an end trigger we act on.
        self.assertIsNone(classify_title("Notepad", CFG))
        self.assertIsNone(classify_title("", CFG))

    def test_chat_window_is_not_a_session(self):
        # Regression: `Chat | <name> | Microsoft Teams` must NOT trigger recording.
        from whispr.watcher import session_subject, classify_title
        self.assertIsNone(session_subject("Chat | Virkar, Jayesh | Microsoft Teams", CFG))
        # It matches the end/chat pattern, so classify returns 'end', never 'session'.
        self.assertEqual(classify_title("Chat | Virkar, Jayesh | Microsoft Teams", CFG), "end")

    def test_session_subject_extraction(self):
        from whispr.watcher import session_subject
        self.assertEqual(session_subject("Virkar, Jayesh | Microsoft Teams", CFG), "Virkar, Jayesh")
        self.assertEqual(session_subject("Q3 Planning | Microsoft Teams", CFG), "Q3 Planning")
        self.assertIsNone(session_subject("Calls | Microsoft Teams", CFG))  # nav prefix
        self.assertIsNone(session_subject("Microsoft Teams", CFG))          # no suffix


class TestCounterpart(unittest.TestCase):
    def test_parse(self):
        from whispr.metadata import _counterpart_from_title
        self.assertEqual(_counterpart_from_title("Calls | Bob Jones"), "Bob Jones")
        self.assertEqual(_counterpart_from_title("Bob Jones | Microsoft Teams"), "Bob Jones")
        self.assertIsNone(_counterpart_from_title(""))


class TestSlugAndTime(unittest.TestCase):
    def test_slugify(self):
        from whispr.output import _slugify
        self.assertEqual(_slugify("Weekly: AI/Sync?"), "weekly-aisync")
        self.assertEqual(_slugify("  multiple   spaces  "), "multiple-spaces")
        self.assertEqual(_slugify(""), "untitled")
        self.assertLessEqual(len(_slugify("x" * 200)), 60)

    def test_hms(self):
        from whispr.output import _format_hms
        self.assertEqual(_format_hms(4), "00:00:04")
        self.assertEqual(_format_hms(3725.5), "01:02:06")


class TestDuration(unittest.TestCase):
    def test_duration_min(self):
        s = _session()
        self.assertEqual(s.duration_min, 30)
        s2 = _session(end=None)
        self.assertEqual(s2.duration_min, 0)


class TestDbfs(unittest.TestCase):
    def test_dbfs_rms(self):
        import numpy as np
        from whispr.capture import _dbfs, _rms
        self.assertEqual(_dbfs(0.0), -180.0)
        self.assertAlmostEqual(_dbfs(1.0), 0.0, places=5)
        self.assertEqual(_rms(np.zeros(10, dtype="float32")), 0.0)
        self.assertAlmostEqual(_rms(np.ones(10, dtype="float32")), 1.0, places=5)


class TestOutputContract(unittest.TestCase):
    """The transcript writer must produce frontmatter that summarize.py can patch.
    This locks the output<->summarize interface (regression: they disagreed on
    topic flow vs block style)."""

    def _write(self, transcript):
        from whispr.output import write_transcript
        meta = CallMetadata(
            source="outlook", call_title="Weekly AI Sync", organizer="Jane Doe",
            attendees=["Jane Doe", "Colby Hood"], invite_notes="Agenda " * 200,
        )
        return write_transcript(_session(), meta, transcript, CFG)

    def test_frontmatter_and_body(self):
        tr = TranscriptResult(turns=[
            Turn(4.0, "Others", "Hi"), Turn(19.0, "Me", "Hello"),
        ], is_stub=False)
        path = self._write(tr)
        try:
            text = path.read_text(encoding="utf-8")
            # Vault schema keys present.
            for key in ("date:", "source: meeting", "status: raw", "confidence: working",
                        "call_title:", "call_type:", "attendees:", "metadata_source:",
                        "model: small-int8", "whispr_version:"):
                self.assertIn(key, text, key)
            # Topic placeholder in FLOW style (summarize.py requires this).
            self.assertIn(f"topic: [{PENDING_SUMMARY_TOPIC}]", text)
            # E5: invite_notes truncated to configured max — parse to be precise.
            parts = text.split("---")
            fm = yaml.safe_load(parts[1])
            self.assertLessEqual(len(fm["invite_notes"]), CFG["metadata"]["invite_notes_max_chars"])
            # Interleaved timestamped turns.
            self.assertIn("**[00:00:04] Others:** Hi", text)
            self.assertIn("**[00:00:19] Me:** Hello", text)
            self.assertIn("> _Summary pending._", text)
        finally:
            path.unlink(missing_ok=True)

    def test_stub_body(self):
        path = self._write(TranscriptResult(turns=[], is_stub=True))
        try:
            text = path.read_text(encoding="utf-8")
            self.assertIn("_No speech detected._", text)
        finally:
            path.unlink(missing_ok=True)

    def test_placeholder_contract(self):
        """Output must emit the exact placeholders the summary-agent handoff
        (SUMMARY_AGENT.md) tells the agent to look for and replace."""
        import whispr.summarize as S
        path = self._write(TranscriptResult(turns=[Turn(1.0, "Me", "Hi")], is_stub=False))
        try:
            text = path.read_text(encoding="utf-8")
            self.assertIn(f"topic: [{PENDING_SUMMARY_TOPIC}]", text)
            self.assertIn("> _Summary pending._", text)
            # The pending-lister must flag this transcript as needing a summary.
            self.assertRegex(text, S._UNSUMMARIZED_RE)
        finally:
            path.unlink(missing_ok=True)

    def test_minimal_metadata(self):
        """source=window-title, None organizer/notes, empty attendees exercises all None branches."""
        from whispr.output import write_transcript, VAULT_SOURCE
        meta = CallMetadata(
            source="window-title",
            call_title=None,
            organizer=None,
            attendees=[],
            invite_notes=None,
        )
        s = _session(window_title="Bob | Microsoft Teams")
        path = write_transcript(s, meta, TranscriptResult(turns=[], is_stub=True), CFG)
        try:
            text = path.read_text(encoding="utf-8")
            self.assertIn("metadata_source: window-title", text)
            self.assertIn(f"source: {VAULT_SOURCE}", text)
        finally:
            path.unlink(missing_ok=True)


class TestMergeTurns(unittest.TestCase):
    """Tests for the _merge_turns algorithm — the live-bug-prone merge logic."""

    def setUp(self):
        from whispr.transcribe import _merge_turns
        self._merge = _merge_turns
        self._gap = CFG["transcription"]["turn_merge_gap_seconds"]  # 1.5

    def test_same_speaker_within_gap_merges(self):
        items = [(0.0, "Me", "Hello"), (1.0, "Me", "world")]
        turns = self._merge(items, self._gap)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].text, "Hello world")
        self.assertEqual(turns[0].speaker, "Me")

    def test_same_speaker_at_gap_boundary_splits(self):
        # At exactly gap distance: not less than gap → new turn.
        items = [(0.0, "Me", "Hello"), (self._gap, "Me", "world")]
        turns = self._merge(items, self._gap)
        self.assertEqual(len(turns), 2)

    def test_alternating_speakers_never_merge(self):
        items = [(0.0, "Me", "Hi"), (0.5, "Others", "Hello"), (1.0, "Me", "How are you")]
        turns = self._merge(items, self._gap)
        self.assertEqual(len(turns), 3)

    def test_out_of_order_input_is_sorted(self):
        # Simulate cross-stream items arriving out of chronological order.
        items = [(5.0, "Others", "B"), (1.0, "Me", "A")]
        turns = self._merge(items, self._gap)
        self.assertEqual(turns[0].text, "A")
        self.assertEqual(turns[1].text, "B")

    def test_empty_input(self):
        self.assertEqual(self._merge([], self._gap), [])

    def test_stub_on_empty_wavs(self):
        # transcribe_session with missing files -> stub, no crash.
        from whispr.transcribe import transcribe_session
        s = _session(mic_wav=None, loopback_wav=None)
        result = transcribe_session(s, CFG)
        self.assertTrue(result.is_stub)
        self.assertEqual(result.turns, [])


class TestBackstopStep(unittest.TestCase):
    """Tests for _backstop_step — the pure state-machine step function."""

    def _step(self, **kw):
        from whispr.watcher import _backstop_step
        defaults = dict(
            recording=True,
            window_exists=True,
            audio_active=False,
            gone_for=0.0,
            audio_seen=False,
            no_window_polls=0,
            gone_limit=45.0,
            confirm_polls=2,
            poll_seconds=2.0,
        )
        defaults.update(kw)
        return _backstop_step(**defaults)

    def test_not_recording_resets_state(self):
        gf, als, nwp, stop = self._step(recording=False, gone_for=30.0, audio_seen=True, no_window_polls=1)
        self.assertFalse(stop)
        self.assertEqual(gf, 0.0)
        self.assertFalse(als)
        self.assertEqual(nwp, 0)

    def test_window_flickers_one_poll_no_stop(self):
        _, _, _, stop = self._step(window_exists=False, no_window_polls=0)
        self.assertFalse(stop)

    def test_window_gone_two_polls_stops(self):
        _, _, _, stop = self._step(window_exists=False, no_window_polls=1)
        self.assertTrue(stop)

    def test_audio_active_then_silent_triggers_stop(self):
        # gone_for=43.0 + poll_seconds=2.0 >= gone_limit=45.0 → stop
        _, _, _, stop = self._step(audio_active=False, audio_seen=True, gone_for=43.0, poll_seconds=2.0)
        self.assertTrue(stop)

    def test_audio_silent_pre_join_never_stops(self):
        # audio_seen=False (never had audio): backstop never fires regardless of gone_for.
        _, _, _, stop = self._step(audio_active=False, audio_seen=False, gone_for=100.0)
        self.assertFalse(stop)

    def test_audio_active_resets_gone_for(self):
        gf, als, _, stop = self._step(audio_active=True, audio_seen=False, gone_for=30.0)
        self.assertFalse(stop)
        self.assertTrue(als)
        self.assertEqual(gf, 0.0)


class TestResolveUniquePath(unittest.TestCase):
    def test_no_collision(self):
        from whispr.output import _resolve_unique_path
        with tempfile.TemporaryDirectory() as d:
            result = _resolve_unique_path(Path(d), "stem", ".md")
            self.assertEqual(result.name, "stem.md")

    def test_collision_suffix(self):
        from whispr.output import _resolve_unique_path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "stem.md").touch()
            (p / "stem-2.md").touch()
            result = _resolve_unique_path(p, "stem", ".md")
            self.assertEqual(result.name, "stem-3.md")


class TestSubjectMatches(unittest.TestCase):
    def test_exact_match(self):
        from whispr.metadata import _subject_matches
        self.assertTrue(_subject_matches("Weekly Sync", "Weekly Sync"))

    def test_prefix_match(self):
        # Teams may truncate long subjects in the title bar.
        from whispr.metadata import _subject_matches
        self.assertTrue(_subject_matches("Weekly AI", "Weekly AI Advantage Sync"))

    def test_containment_both_directions(self):
        from whispr.metadata import _subject_matches
        self.assertTrue(_subject_matches("AI Advantage Sync", "Weekly AI Advantage Sync"))
        self.assertTrue(_subject_matches("Weekly AI Advantage Sync", "AI Advantage"))

    def test_too_short_returns_false(self):
        from whispr.metadata import _subject_matches
        self.assertFalse(_subject_matches("AB", "AB"))  # len < 3

    def test_no_match(self):
        from whispr.metadata import _subject_matches
        self.assertFalse(_subject_matches("Sprint Review", "Weekly Sync"))


class TestListPending(unittest.TestCase):
    def _run_list_pending(self, md_path: Path) -> str:
        from whispr.summarize import list_pending
        captured = io.StringIO()
        old = sys.stdout
        sys.stdout = captured
        try:
            list_pending([str(md_path)])
        finally:
            sys.stdout = old
        return captured.getvalue()

    def test_pending_file_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            md_path = Path(d) / "test.md"
            md_path.write_text(
                f"---\ntopic: [{PENDING_SUMMARY_TOPIC}]\n---\n\nbody\n", encoding="utf-8"
            )
            output = self._run_list_pending(md_path)
        self.assertIn("pending summary", output)

    def test_trailing_space_still_matches(self):
        # Edge: "topic: [unsorted]   " with trailing whitespace.
        with tempfile.TemporaryDirectory() as d:
            md_path = Path(d) / "test.md"
            md_path.write_text(
                f"---\ntopic: [{PENDING_SUMMARY_TOPIC}]   \n---\n\nbody\n", encoding="utf-8"
            )
            output = self._run_list_pending(md_path)
        self.assertIn("pending summary", output)

    def test_done_file_not_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            md_path = Path(d) / "done.md"
            md_path.write_text("---\ntopic: [ai, teams]\n---\n\nbody\n", encoding="utf-8")
            output = self._run_list_pending(md_path)
        self.assertIn("done:", output)
        self.assertNotIn("pending summary", output)


class TestWatcherSuppression(unittest.TestCase):
    """Kill-switch identity: a tray stop of a still-live session must not
    auto-re-record THAT session, while a genuinely different/new call still records
    and a watcher-detected end never suppresses. Regression for the notify_stopped
    re-trigger bug."""

    def _watcher(self):
        from whispr.watcher import TeamsWatcher
        return TeamsWatcher(
            CFG, on_start=lambda *a: None, on_stop=lambda: None, on_discard=lambda: None
        )

    def test_tray_stop_latches_active_subject(self):
        # notify_stopped WITHOUT a preceding _do_stop = tray kill switch: suppress.
        w = self._watcher()
        w._recording = True
        w._active_subject = "Weekly Sync"
        w.notify_stopped()
        self.assertFalse(w._recording)
        self.assertEqual(w._suppressed_subject, "Weekly Sync")
        self.assertIsNone(w._active_subject)

    def test_watcher_end_does_not_suppress(self):
        # _do_stop (window-gone / end-title) clears _active_subject first, so the
        # notify_stopped that follows in the real flow has nothing to suppress.
        w = self._watcher()
        w._recording = True
        w._active_subject = "Weekly Sync"
        w._do_stop(keep=True)          # on_stop is a no-op lambda here
        self.assertIsNone(w._active_subject)
        w.notify_stopped()             # mirrors the real _finish -> notify_stopped call
        self.assertIsNone(w._suppressed_subject)

    def test_suppressed_subject_blocks_only_itself(self):
        w = self._watcher()
        w._suppressed_subject = "Weekly Sync"
        self.assertTrue(w._start_suppressed("Weekly Sync"))
        self.assertFalse(w._start_suppressed("Different Call"))

    def test_no_suppression_when_nothing_active(self):
        # A stop with no active subject (already idle) must not latch anything.
        w = self._watcher()
        w.notify_stopped()
        self.assertIsNone(w._suppressed_subject)
        self.assertFalse(w._start_suppressed("Anything"))

    def test_do_stop_sets_stopping(self):
        # _do_stop must latch _stopping so the start-gate stays closed through the
        # (slow) teardown that follows, even though _recording is already False.
        w = self._watcher()
        w._recording = True
        w._active_subject = "Weekly Sync"
        w._do_stop(keep=True)  # on_stop is a no-op lambda; notify_stopped not called
        self.assertTrue(w._stopping)
        self.assertFalse(w._recording)
        self.assertIsNone(w._active_subject)

    def test_stopping_blocks_new_start(self):
        # A new session must not start while a prior stop is still tearing down,
        # and _do_start must report it did NOT start (False).
        w = self._watcher()
        w._recording = False
        w._stopping = True
        started = []
        w._on_start = lambda call_type, title: started.append(title)
        result = w._do_start("call", "New Call | Microsoft Teams", "New Call")
        self.assertFalse(result)
        self.assertEqual(started, [])
        self.assertFalse(w._recording)

    def test_do_start_returns_true_on_real_start(self):
        # A clean start returns True so _maybe_start knows to log the detection.
        w = self._watcher()
        started = []
        w._on_start = lambda call_type, title: started.append(title)
        result = w._do_start("meeting", "Weekly Sync | Microsoft Teams", "Weekly Sync")
        self.assertTrue(result)
        self.assertTrue(w._recording)
        self.assertEqual(w._active_subject, "Weekly Sync")

    def test_notify_stopped_clears_stopping(self):
        # notify_stopped is the terminal teardown step: it must re-open the gate.
        w = self._watcher()
        w._recording = False
        w._stopping = True
        w.notify_stopped()
        self.assertFalse(w._stopping)

    def test_maybe_start_respects_suppression_regardless_of_source(self):
        # _maybe_start is the shared start path for both the WinEvent hook and the
        # poll fallback (see 2026-07-16 hook-silence incident); a suppressed
        # subject must be blocked the same way from either caller.
        w = self._watcher()
        w._suppressed_subject = "Weekly Sync"
        started = []
        w._on_start = lambda call_type, title: started.append((call_type, title))
        w._maybe_start("Weekly Sync | Microsoft Teams", "Weekly Sync", via_poll=False)
        w._maybe_start("Weekly Sync | Microsoft Teams", "Weekly Sync", via_poll=True)
        self.assertEqual(started, [])
        self.assertFalse(w._recording)


class TestIsTooShort(unittest.TestCase):
    """Pure predicate gating auto-discard of short false-positive sessions
    (e.g. a Teams pre-join preview window flashing a session-shaped title for a
    few seconds — see 2026-07-17 incident)."""

    def test_below_floor_is_too_short(self):
        from whispr.__main__ import _is_too_short
        # 4s of audio at 16kHz, 12s floor.
        self.assertTrue(_is_too_short(64000, 64000, 16000, 12.0))

    def test_above_floor_is_not_too_short(self):
        from whispr.__main__ import _is_too_short
        # 30s of audio at 16kHz, 12s floor.
        self.assertFalse(_is_too_short(480000, 480000, 16000, 12.0))

    def test_uses_longer_stream(self):
        from whispr.__main__ import _is_too_short
        # mic stream is long enough on its own even though loopback is short.
        self.assertFalse(_is_too_short(480000, 1000, 16000, 12.0))


class TestShouldAutoDiscard(unittest.TestCase):
    """Auto-discard must fire ONLY for a kept, non-partial, too-short session — an
    explicit user discard or a deliberate mid-call quit must never be mislabeled
    as an automatic min-duration discard."""

    SHORT = (64000, 64000, 16000, 12.0)   # 4s of audio, 12s floor

    def test_kept_short_session_is_auto_discarded(self):
        from whispr.__main__ import _should_auto_discard
        self.assertTrue(_should_auto_discard(True, False, *self.SHORT))

    def test_explicit_discard_is_not_auto_discard(self):
        from whispr.__main__ import _should_auto_discard
        # keep=False: the user chose to discard; not our heuristic's business.
        self.assertFalse(_should_auto_discard(False, False, *self.SHORT))

    def test_partial_quit_is_not_auto_discarded(self):
        from whispr.__main__ import _should_auto_discard
        # partial=True: deliberate mid-call quit is always honored even if short.
        self.assertFalse(_should_auto_discard(True, True, *self.SHORT))

    def test_kept_long_session_is_not_auto_discarded(self):
        from whispr.__main__ import _should_auto_discard
        self.assertFalse(_should_auto_discard(True, False, 480000, 480000, 16000, 12.0))


class TestIncidents(unittest.TestCase):
    def _cfg(self, log_dir: Path) -> dict:
        return {"paths": {"logs": log_dir}}

    def test_round_trip(self):
        from whispr.incidents import read_incidents, record_incident
        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(Path(d))
            record_incident(cfg, "crash", thread="MainThread", error="boom")
            incidents = read_incidents(cfg)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["kind"], "crash")
        self.assertEqual(incidents[0]["error"], "boom")

    def test_malformed_line_skipped_not_fatal(self):
        from whispr.incidents import read_incidents
        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(Path(d))
            (Path(d) / "incidents.jsonl").write_text("not json\n", encoding="utf-8")
            incidents = read_incidents(cfg)  # must not raise
        self.assertEqual(incidents, [])

    def test_missing_file_returns_empty(self):
        from whispr.incidents import read_incidents
        with tempfile.TemporaryDirectory() as d:
            incidents = read_incidents(self._cfg(Path(d)))
        self.assertEqual(incidents, [])


class TestLoadConfig(unittest.TestCase):
    def test_absolute_path_creates_dirs(self):
        from whispr.config import load_config
        with tempfile.TemporaryDirectory() as d:
            transcripts = Path(d) / "tr"
            recordings = Path(d) / "rec"
            logs_dir = Path(d) / "logs"
            cfg_path = Path(d) / "config.yaml"
            cfg_path.write_text(
                f"paths:\n"
                f"  transcripts: {transcripts}\n"
                f"  recordings: {recordings}\n"
                f"  logs: {logs_dir}\n"
                "version: '0.0.1'\n",
                encoding="utf-8",
            )
            cfg = load_config(str(cfg_path))
            self.assertEqual(cfg["paths"]["transcripts"], transcripts)
            self.assertTrue(transcripts.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
