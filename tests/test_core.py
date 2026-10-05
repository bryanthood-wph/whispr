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


class TestSubjectCore(unittest.TestCase):
    """Titles observed in whispr.log, 2026-09-18..10-02."""

    def test_named_meeting_behind_join_label_and_account(self):
        from whispr.metadata import subject_core
        t = "Meeting join | Kroger FIH Onboarding | Deloitte (O365D) | cohood@deloitte.com | Microsoft Teams"
        self.assertEqual(subject_core(t, CFG), "Kroger FIH Onboarding")

    def test_counterpart_of_ad_hoc_call(self):
        from whispr.metadata import subject_core
        self.assertEqual(subject_core("Cosby, Cecile | Deloitte (O365D) | cohood@deloitte.com", CFG), "Cosby, Cecile")
        self.assertEqual(subject_core("Bob Jones | Microsoft Teams", CFG), "Bob Jones")

    def test_generic_titles_have_no_core(self):
        from whispr.metadata import subject_core
        self.assertEqual(subject_core("Deloitte (O365D) | cohood@deloitte.com | Microsoft Teams", CFG), "")
        self.assertEqual(subject_core(
            "Meeting join | Microsoft Teams meeting | Deloitte (O365D) | cohood@deloitte.com", CFG), "")
        self.assertEqual(subject_core("", CFG), "")


def _appt(**kw):
    from types import SimpleNamespace
    base = dict(AllDayEvent=False, MeetingStatus=3, ResponseStatus=3,
                Body="Join: https://teams.microsoft.com/l/meetup-join/x")
    base.update(kw)
    return SimpleNamespace(**base)


class TestOverlapCandidate(unittest.TestCase):
    MARKER = "teams.microsoft.com"

    def test_accepted_teams_meeting_is_candidate(self):
        from whispr.metadata import _is_overlap_candidate
        self.assertTrue(_is_overlap_candidate(_appt(), self.MARKER))

    def test_rejects_items_that_overlap_everything(self):
        # Each of these overlapped 100% of a real recording in the 2026-10-02 probe.
        from whispr.metadata import _is_overlap_candidate
        self.assertFalse(_is_overlap_candidate(_appt(AllDayEvent=True), self.MARKER))   # 'Neil - OOO'
        self.assertFalse(_is_overlap_candidate(_appt(MeetingStatus=0), self.MARKER))    # personal 'Block'
        self.assertFalse(_is_overlap_candidate(_appt(MeetingStatus=5), self.MARKER))    # cancelled
        self.assertFalse(_is_overlap_candidate(_appt(ResponseStatus=4), self.MARKER))   # declined
        self.assertFalse(_is_overlap_candidate(_appt(Body="On Air"), self.MARKER))      # no Teams link


class TestPickByOverlap(unittest.TestCase):
    def test_single_dominant_event_wins(self):
        from whispr.metadata import _pick_by_overlap
        # 2026-09-22 12:06, 55.5 min: Databricks covered 54%, the WAYMO demo 42%.
        self.assertEqual(_pick_by_overlap([("waymo", 1404), ("databricks", 1800)], 3330, 0.5),
                         ("databricks", 1800))

    def test_tie_is_ambiguous(self):
        from whispr.metadata import _pick_by_overlap
        # 2026-09-23 10:00: two meetings in the same slot, both covering 100%.
        self.assertIsNone(_pick_by_overlap([("loop", 762), ("kroger", 762)], 762, 0.5))

    def test_nothing_covers_enough(self):
        from whispr.metadata import _pick_by_overlap
        self.assertIsNone(_pick_by_overlap([("a", 100)], 1000, 0.5))
        self.assertIsNone(_pick_by_overlap([], 1000, 0.5))


class TestRetryWhenBusy(unittest.TestCase):
    BUSY = Exception(-2147418111, "Call was rejected by callee.", None, None)

    def _cfg(self, retries=2):
        return {"metadata": {"outlook_busy_retries": retries, "outlook_busy_retry_seconds": 0}}

    def test_busy_then_success(self):
        from whispr.metadata import _retry_when_busy
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise self.BUSY
            return "ok"
        self.assertEqual(_retry_when_busy(fn, self._cfg()), "ok")
        self.assertEqual(len(calls), 3)

    def test_busy_past_retries_raises(self):
        from whispr.metadata import _retry_when_busy

        def fn():
            raise self.BUSY
        with self.assertRaises(Exception):
            _retry_when_busy(fn, self._cfg(retries=1))

    def test_other_errors_are_not_retried(self):
        from whispr.metadata import _retry_when_busy
        calls = []

        def fn():
            calls.append(1)
            raise ValueError("boom")
        with self.assertRaises(ValueError):
            _retry_when_busy(fn, self._cfg())
        self.assertEqual(len(calls), 1)


class TestFetchMetadataRouting(unittest.TestCase):
    def _route(self, **session_kw):
        from unittest import mock
        from whispr import metadata
        with mock.patch.object(metadata, "_fetch_from_outlook", return_value=None) as fetch:
            meta = metadata.fetch_metadata(_session(**session_kw), CFG)
        _session_arg, _cfg_arg, core, allow_overlap = fetch.call_args.args
        return meta, core, allow_overlap

    def test_named_call_matches_by_subject_only(self):
        # Overlap for a named call grabbed a stray 'Enter MySource' placeholder.
        meta, core, allow_overlap = self._route(
            call_type="call", window_title="Cosby, Cecile | Deloitte (O365D) | cohood@deloitte.com | Microsoft Teams")
        self.assertEqual(core, "Cosby, Cecile")
        self.assertFalse(allow_overlap)
        self.assertEqual(meta.call_title, "Cosby, Cecile")
        self.assertEqual(meta.attendees, ["Cosby, Cecile"])

    def test_generic_call_may_use_overlap(self):
        title = "Deloitte (O365D) | cohood@deloitte.com | Microsoft Teams"
        meta, core, allow_overlap = self._route(call_type="call", window_title=title)
        self.assertEqual(core, "")
        self.assertTrue(allow_overlap)
        self.assertEqual(meta.call_title, title)
        self.assertEqual(meta.attendees, [])

    def test_meeting_fallback_does_not_list_subject_as_attendee(self):
        meta, _core, allow_overlap = self._route(call_type="meeting")
        self.assertTrue(allow_overlap)
        self.assertEqual(meta.call_title, "Weekly Sync")
        self.assertEqual(meta.attendees, [])


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

    def test_decline_discards_and_suppresses_until_window_closes(self):
        # 2026-10-02: answering No to one Teams recording playback was followed by
        # 7 more prompts for the same still-open window.
        w = self._watcher()
        discarded = []
        w._on_discard = lambda: discarded.append(True)
        w._recording = True
        w._active_subject = "Kroger FIH Onboarding"
        w._decline("Kroger FIH Onboarding")
        self.assertEqual(discarded, [True])
        self.assertFalse(w._recording)
        self.assertTrue(w._start_suppressed("Kroger FIH Onboarding"))
        w.notify_stopped()  # the real _finish path must not clear the latch
        self.assertEqual(w._suppressed_subject, "Kroger FIH Onboarding")

    def test_late_decline_never_stops_a_different_session(self):
        w = self._watcher()
        discarded = []
        w._on_discard = lambda: discarded.append(True)
        w._recording = True
        w._active_subject = "Other Call"
        w._decline("Kroger FIH Onboarding")
        self.assertEqual(discarded, [])
        self.assertTrue(w._recording)
        self.assertEqual(w._active_subject, "Other Call")


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


class _FakeStream:
    """Stand-in for sd.InputStream: one 0.1 s chunk per read, paced like real audio."""

    def __init__(self, chunk_value: float = 0.5):
        self._value = chunk_value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, frames):
        import time
        import numpy as np
        time.sleep(frames / 16000)
        return np.full((frames, 1), self._value, dtype="float32"), False


class TestMicRecovery(unittest.TestCase):
    """The mic stream used to end for the whole call on its first error (18 calls
    lost the user's side, 2026-08-10..09-29). It must now reopen and keep "Me"
    aligned with "Others" by padding the outage with silence."""

    def _run_mic_loop(self, open_results, run_seconds=0.6):
        import copy
        import threading
        import time
        from unittest import mock
        from whispr import capture

        cfg = copy.deepcopy(CFG)
        cfg["audio"]["mic_retry_seconds"] = 0.1
        attempts = []

        def fake_input_stream(**_kw):
            outcome = open_results[min(len(attempts), len(open_results) - 1)]
            attempts.append(outcome)
            if isinstance(outcome, Exception):
                raise outcome
            return _FakeStream()

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(capture.sd, "InputStream", side_effect=fake_input_stream), \
                mock.patch.object(capture, "_refresh_devices") as refresh, \
                mock.patch.object(capture, "resolve_mic", return_value=(7, "Headset Mic")):
            rec = capture.DualStreamRecorder(cfg, str(Path(d) / "m.wav"), str(Path(d) / "l.wav"))
            rec._mic_writer = capture._WavWriter(rec._mic_wav, rec._sr)
            rec._running.set()
            t = threading.Thread(target=rec._mic_loop, args=(1,))
            t.start()
            time.sleep(run_seconds)
            rec._running.clear()
            t.join(timeout=5)
            rec._mic_writer.close()
            return rec, attempts, refresh

    def test_reopens_after_failed_open_and_pads_the_gap(self):
        boom = RuntimeError("A device ID has been used that is out of range [MME error 2]")
        rec, attempts, refresh = self._run_mic_loop([boom, None])
        self.assertGreaterEqual(len(attempts), 2)
        self.assertTrue(refresh.called)
        self.assertEqual(rec._mic_dropouts, 1)
        self.assertEqual(rec.mic_device_name, "Headset Mic")
        # Wall-clock coverage: padded gap + captured audio ~= the whole run.
        covered = rec._mic_writer.frames_written / rec._sr
        self.assertGreater(covered, 0.4)
        self.assertGreater(rec._mic_lost_seconds, 0.05)

    def test_never_recovering_counts_one_dropout_and_the_lost_time(self):
        boom = RuntimeError("There is no driver installed on your system.")
        rec, attempts, _ = self._run_mic_loop([boom])
        self.assertGreater(len(attempts), 1)          # it kept trying
        self.assertEqual(rec._mic_dropouts, 1)        # one outage, not one per attempt
        self.assertEqual(rec._mic_writer.frames_written, 0)
        self.assertGreater(rec._mic_lost_seconds, 0.4)


class TestNoSpeechKeepsAudio(unittest.TestCase):
    """2026-09-28: a 26-min meeting captured only silence; whispr wrote 'No speech
    detected', deleted both WAVs and told no one."""

    def _transcribed(self, result, keep_audio_until=None):
        import copy
        from types import SimpleNamespace
        from unittest import mock
        from whispr import metadata
        from whispr.__main__ import Orchestrator
        from whispr.incidents import read_incidents

        with tempfile.TemporaryDirectory() as d:
            cfg = copy.deepcopy(CFG)
            cfg["paths"]["logs"] = Path(d)
            cfg["retention"]["keep_audio_until"] = keep_audio_until
            deleted, notes = [], []
            fake = SimpleNamespace(_cfg=cfg, _delete_wavs=lambda s: deleted.append(s),
                                   _tray=SimpleNamespace(notify=notes.append))
            with mock.patch.object(metadata, "fetch_metadata", return_value=CallMetadata(source="none")):
                Orchestrator._on_transcribed(fake, _session(), result)
            return deleted, notes, read_incidents(cfg)

    def test_stub_keeps_wavs_records_incident_and_notifies(self):
        deleted, notes, incidents = self._transcribed(TranscriptResult(turns=[], is_stub=True))
        self.assertEqual(deleted, [])
        self.assertEqual(len(notes), 1)
        self.assertEqual([i["kind"] for i in incidents], ["no-speech"])
        self.assertEqual(incidents[0]["loopback_wav"], "l.wav")

    def test_real_transcript_still_deletes_audio(self):
        result = TranscriptResult(turns=[Turn(start_seconds=0.0, speaker="Me", text="hi")])
        deleted, notes, incidents = self._transcribed(result)
        self.assertEqual(len(deleted), 1)
        self.assertEqual(notes, [])
        self.assertEqual(incidents, [])

    def test_study_window_keeps_audio(self):
        result = TranscriptResult(turns=[Turn(start_seconds=0.0, speaker="Me", text="hi")])
        deleted, _, _ = self._transcribed(result, keep_audio_until="2999-01-01")
        self.assertEqual(deleted, [])

    def test_closed_study_window_deletes_audio(self):
        result = TranscriptResult(turns=[Turn(start_seconds=0.0, speaker="Me", text="hi")])
        deleted, _, _ = self._transcribed(result, keep_audio_until="2000-01-01")
        self.assertEqual(len(deleted), 1)


class TestRetention(unittest.TestCase):
    """F.1 keep window + L14: the purge never deletes a WAV whose call has no transcript."""

    NOW = datetime(2026, 11, 1, 12, 0, 0)

    def test_keeping_audio_window_is_inclusive(self):
        from datetime import date
        from whispr.retention import keeping_audio

        r = {"keep_audio_until": "2026-10-18"}
        self.assertTrue(keeping_audio(r, date(2026, 10, 18)))
        self.assertFalse(keeping_audio(r, date(2026, 10, 19)))
        self.assertFalse(keeping_audio({"keep_audio_until": None}, date(2026, 10, 1)))
        self.assertFalse(keeping_audio({}, date(2026, 10, 1)))

    def _cfg(self, root: Path) -> dict:
        import copy

        cfg = copy.deepcopy(CFG)
        for key in ("transcripts", "recordings", "logs"):
            (root / key).mkdir()
            cfg["paths"][key] = root / key
        cfg["retention"]["audio_max_age_days"] = 21
        return cfg

    def _wavs(self, cfg, stamp):
        for stream in ("mic", "loopback"):
            (cfg["paths"]["recordings"] / f"{stamp}-{stream}.wav").write_bytes(b"RIFF")

    def _transcript(self, cfg, start_iso):
        (cfg["paths"]["transcripts"] / f"{start_iso[:10]}-call.md").write_text(
            f"---\ncall_title: x\nstart: '{start_iso}'\n---\n\nbody\n", encoding="utf-8")

    def test_purge_deletes_old_transcribed_keeps_orphan_and_young(self):
        from whispr.retention import purge_audio

        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(Path(d))
            self._wavs(cfg, "2026-09-01-100000")            # old, transcribed -> deleted
            self._transcript(cfg, "2026-09-01T10:00:00-04:00")
            self._wavs(cfg, "2026-09-02-100000")            # old, no transcript -> kept
            self._wavs(cfg, "2026-10-30-100000")            # young -> untouched
            code, report = purge_audio(cfg, self.NOW)
            left = sorted(p.name for p in cfg["paths"]["recordings"].iterdir())
        self.assertEqual(code, 1)
        self.assertEqual(left, ["2026-09-02-100000-loopback.wav", "2026-09-02-100000-mic.wav",
                                "2026-10-30-100000-loopback.wav", "2026-10-30-100000-mic.wav"])
        self.assertTrue(any(line.startswith("OVERDUE 2026-09-02-100000") for line in report))

    def test_check_only_deletes_nothing_and_fails_on_overdue(self):
        from whispr.retention import purge_audio

        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(Path(d))
            self._wavs(cfg, "2026-09-01-100000")
            self._transcript(cfg, "2026-09-01T10:00:00-04:00")
            code, _ = purge_audio(cfg, self.NOW, check_only=True)
            left = len(list(cfg["paths"]["recordings"].iterdir()))
        self.assertEqual((code, left), (1, 2))

    def test_nothing_overdue_exits_zero(self):
        from whispr.retention import purge_audio

        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(Path(d))
            self._wavs(cfg, "2026-10-30-100000")
            code, _ = purge_audio(cfg, self.NOW)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
