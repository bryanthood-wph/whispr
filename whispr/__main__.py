"""whispr entrypoint: single-instance guard, tray, watcher, transcription queue.

Usage:
    python -m whispr                      # run the recorder (default)
    python -m whispr list-pending [file]  # list transcripts awaiting a local summary
    python -m whispr record-test SECONDS  # record both streams for N seconds, then stop
    python -m whispr doctor [DAYS]        # summarize crashes + auto-discards (default 7 days)
"""

from __future__ import annotations

import ctypes
import sys
import threading
from datetime import datetime
from typing import Optional

from whispr import __version__
from whispr.capture import DualStreamRecorder
from whispr.config import load_config
from whispr.log import configure_logging, get_logger
from whispr.models import CallSession, TranscriptResult
from whispr.tray import TrayController

log = get_logger("main")

_MUTEX_NAME = "Global\\whispr_single_instance"  # deliberately fixed — must be stable across launches
_ERROR_ALREADY_EXISTS = 183
_TRAY_LABEL_MAX = 60
_QUEUE_DRAIN_TIMEOUT = 600.0


def _acquire_single_instance() -> Optional[int]:
    """Create a named mutex. Returns the handle, or None if another instance holds it.

    Uses use_last_error=True + ctypes.get_last_error(): reading
    kernel32.GetLastError() as a *separate* ctypes call is unreliable because ctypes
    overwrites the thread's last-error between calls, so the duplicate instance never
    saw ERROR_ALREADY_EXISTS and a second full app ran (double capture on every call).
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    err = ctypes.get_last_error()
    if err == _ERROR_ALREADY_EXISTS:
        if handle:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None
    return handle


def _now_local() -> datetime:
    """Timezone-aware local now."""
    return datetime.now().astimezone()


def _wav_paths(cfg: dict, session_start: datetime) -> tuple[str, str]:
    rec_dir = cfg["paths"]["recordings"]
    stamp = session_start.strftime("%Y-%m-%d-%H%M%S")
    return str(rec_dir / f"{stamp}-mic.wav"), str(rec_dir / f"{stamp}-loopback.wav")


def _captured_seconds(mic_frames: int, loopback_frames: int, samplerate: int) -> float:
    """Seconds of audio in the longer of the two captured streams."""
    return max(mic_frames, loopback_frames) / samplerate


def _is_too_short(mic_frames: int, loopback_frames: int, samplerate: int, min_seconds: float) -> bool:
    """True if the longer of the two captured streams is under the min-duration floor."""
    return _captured_seconds(mic_frames, loopback_frames, samplerate) < min_seconds


def _should_auto_discard(
    keep: bool, partial: bool, mic_frames: int, loopback_frames: int,
    samplerate: int, min_seconds: float,
) -> bool:
    """True if a session should be silently auto-discarded as too short.

    Only a session we'd otherwise KEEP (keep=True) and that wasn't a deliberate
    mid-call quit (partial=False) is a candidate — an explicit user discard is not
    an auto-discard and must never be logged or recorded as one.
    """
    return keep and not partial and _is_too_short(mic_frames, loopback_frames, samplerate, min_seconds)


class Orchestrator:
    """Owns the current recording session and wires watcher/tray/queue together."""

    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._session: Optional[CallSession] = None
        self._recorder: Optional[DualStreamRecorder] = None

        # Lazy imports keep module load light and let record-test skip heavy deps.
        from whispr.transcribe import TranscriptionQueue

        self._queue = TranscriptionQueue(cfg, self._on_transcribed)
        self._tray = TrayController(
            on_stop_keep=self._tray_stop_keep,
            on_stop_discard=self._tray_stop_discard,
            on_quit=self._quit,
        )
        self._watcher = None  # set in run()

    # -- watcher callbacks (watcher thread) ------------------------------

    def _on_start(self, call_type: str, title: str) -> None:
        with self._lock:
            if self._session is not None:
                return
            start = _now_local()
            mic_wav, loop_wav = _wav_paths(self._cfg, start)
            session = CallSession(
                call_type=call_type, window_title=title, start=start,
                mic_wav=mic_wav, loopback_wav=loop_wav,
            )
            recorder = DualStreamRecorder(self._cfg, mic_wav, loop_wav)
            recorder.start()
            session.mic_device = recorder.mic_device_name
            session.output_device = recorder.loopback_device_name
            self._session = session
            self._recorder = recorder
        self._tray.set_recording(title[:_TRAY_LABEL_MAX])

    def _on_stop(self) -> None:
        self._finish(keep=True)

    def _on_discard(self) -> None:
        self._finish(keep=False)

    # -- tray callbacks (tray thread) ------------------------------------

    def _tray_stop_keep(self) -> None:
        self._finish(keep=True)

    def _tray_stop_discard(self) -> None:
        self._finish(keep=False)

    # -- shared stop path ------------------------------------------------

    def _finish(self, keep: bool, partial: bool = False) -> None:
        with self._lock:
            session = self._session
            recorder = self._recorder
            self._session = None
            self._recorder = None
        if session is None or recorder is None:
            return
        try:
            result = recorder.stop()
        finally:
            # Always re-open the watcher's start-gate, even if stop() raises —
            # otherwise a failed teardown would strand _stopping=True and block
            # every future recording. notify_stopped is idempotent.
            if self._watcher is not None:
                self._watcher.notify_stopped()
        session.end = _now_local()
        session.mic_device = result.mic_device
        session.output_device = result.output_device
        session.partial = partial
        self._tray.set_idle()

        # Auto-discard a KEPT-but-too-short session (most commonly a Teams pre-join
        # preview window flashing a session-shaped title). Gated on keep/partial so
        # an explicit user discard or a deliberate mid-call quit is never mislabeled
        # as an auto-discard. See _should_auto_discard.
        too_short = _should_auto_discard(
            keep, partial, result.mic_frames, result.loopback_frames,
            self._cfg["audio"]["samplerate"], self._cfg["trigger"]["min_recording_seconds"],
        )
        from whispr.incidents import record_incident

        if too_short:
            seconds = round(_captured_seconds(
                result.mic_frames, result.loopback_frames, self._cfg["audio"]["samplerate"]), 1)
            record_incident(self._cfg, "short-session-discarded", title=session.window_title, seconds=seconds)

        if keep and not too_short:
            if result.mic_dropouts:
                record_incident(self._cfg, "mic-dropout", title=session.window_title,
                                dropouts=result.mic_dropouts, lost_seconds=result.mic_lost_seconds)
            log.info("queuing session for transcription (%s)", session.window_title)
            self._queue.submit(session)
        else:
            self._delete_wavs(session)
            reason = "auto-discarded (too short)" if too_short else "discarded"
            log.info("%s recording (%s)", reason, session.window_title)

    def _delete_wavs(self, session: CallSession) -> None:
        import os

        for path in (session.mic_wav, session.loopback_wav):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError as exc:
                    log.warning("could not delete %s: %s", path, exc)

    # -- transcription completion (worker thread) ------------------------

    def _on_transcribed(self, session: CallSession, result: TranscriptResult) -> None:
        from whispr.metadata import fetch_metadata
        from whispr.output import write_transcript

        try:
            meta = fetch_metadata(session, self._cfg)
        except Exception as exc:
            log.warning("metadata fetch failed: %s", exc)
            from whispr.models import CallMetadata

            meta = CallMetadata(source="none")

        try:
            path = write_transcript(session, meta, result, self._cfg)
        except Exception as exc:
            log.error("transcript write failed; keeping audio for retry: %s", exc)
            return

        # Summaries are NOT whispr's job — a local agent fills the placeholders
        # off-line. See SUMMARY_AGENT.md. whispr's pipeline ends at a written
        # transcript; nothing here contacts any service.

        if result.is_stub:
            # A kept session with no speech in either stream is a capture failure
            # (2026-09-28: 26 min of meeting, loopback locked to a silent endpoint,
            # mic failed to open) or a silent lobby. Either way, don't destroy the
            # only evidence, and say so now — while there's still time to get a
            # Teams recording or notes.
            from whispr.incidents import record_incident

            record_incident(self._cfg, "no-speech", title=session.window_title,
                            duration_min=session.duration_min,
                            mic_wav=session.mic_wav, loopback_wav=session.loopback_wav)
            log.warning("no speech in either stream; keeping audio (%s, %s)",
                        session.mic_wav, session.loopback_wav)
            self._tray.notify(f"No speech captured in a {session.duration_min}-min recording: "
                              f"{session.window_title[:_TRAY_LABEL_MAX]}. Audio kept in recordings.")
        elif self._cfg["retention"]["delete_audio_on_success"]:
            self._delete_wavs(session)
        log.info("transcript ready: %s", path)

    # -- lifecycle -------------------------------------------------------

    def _is_meeting(self, subject: str) -> bool:
        """Ask Outlook whether this session-window subject is a live scheduled
        meeting (auto-record) vs an ad-hoc call (prompt)."""
        from whispr.metadata import is_live_meeting

        return is_live_meeting(subject, _now_local(), self._cfg)

    def run(self) -> None:
        from whispr.watcher import TeamsWatcher

        self._watcher = TeamsWatcher(
            self._cfg, self._on_start, self._on_stop, self._on_discard,
            is_meeting=self._is_meeting,
        )
        self._watcher.start()
        log.info("whispr %s running; tray active", __version__)
        # Tray owns the main-thread event loop.
        self._tray.run()
        # run() returns only after quit.

    def _quit(self) -> None:
        log.info("quit requested")
        # If a recording is in flight, keep it (mark partial).
        with self._lock:
            recording = self._session is not None
        if recording:
            self._finish(keep=True, partial=True)
        if self._watcher is not None:
            self._watcher.stop()
        # Drain the queue before exit so an in-flight/just-queued transcription
        # isn't lost when the daemon worker is killed at process exit.
        self._queue.stop(join_timeout=_QUEUE_DRAIN_TIMEOUT)
        self._tray.stop()


def _install_crash_logging(cfg: dict) -> None:
    """Log + record an incident for any exception that would otherwise kill the
    process with zero trace — whispr runs under pythonw.exe (no console), so an
    unhandled exception anywhere previously left nothing: not in the log, not on
    screen (observed 2026-07-16/17: the process vanished mid-idle with no error).

    Covers both the main thread (sys.excepthook) and spawned threads
    (threading.excepthook) — watcher/backstop/tray callbacks already guard their
    own bodies, but this is the last-resort net for anything that slips past.
    """
    from whispr.incidents import record_incident

    def _log_crash(exc_type, exc_value, tb, thread_name: str = "MainThread") -> None:
        # Record the durable incident FIRST: record_incident never raises, whereas
        # log.critical() can (a raising log handler, or a RecursionError that
        # StreamHandler.emit deliberately re-raises). Doing it first guarantees the
        # crash leaves a trace in incidents.jsonl even if the log write itself fails
        # — this hook IS sys/threading.excepthook, so anything it raises is lost.
        record_incident(cfg, "crash", thread=thread_name, error=str(exc_value))
        log.critical("unhandled exception in %s", thread_name, exc_info=(exc_type, exc_value, tb))

    sys.excepthook = _log_crash
    threading.excepthook = lambda args: _log_crash(
        args.exc_type, args.exc_value, args.exc_traceback, args.thread.name if args.thread else "unknown"
    )
    log.info("crash logging installed (sys.excepthook + threading.excepthook)")


def _run_app() -> int:
    cfg = load_config()
    configure_logging(cfg["paths"]["logs"])
    _install_crash_logging(cfg)
    handle = _acquire_single_instance()
    if handle is None:
        log.error("another whispr instance is already running; exiting")
        return 1
    log.info("whispr %s starting", __version__)
    orch = Orchestrator(cfg)
    orch.run()
    return 0


def _record_test(seconds: float) -> int:
    """Record both streams for N seconds to the recordings dir (manual capture check)."""
    import time

    cfg = load_config()
    configure_logging(cfg["paths"]["logs"])
    start = _now_local()
    mic_wav, loop_wav = _wav_paths(cfg, start)
    rec = DualStreamRecorder(cfg, mic_wav, loop_wav)
    rec.start()
    print(f"recording {seconds}s -> {mic_wav} / {loop_wav}")
    time.sleep(seconds)
    info = rec.stop()
    print("done:", info)
    return 0


# (incident kind, heading, one-line detail) — one section per kind, in print order.
_DOCTOR_SECTIONS = (
    ("crash", "Crashes", lambda i: f"[{i.get('thread')}]  {i.get('error')}"),
    ("no-speech", "Kept sessions with no speech (audio kept)",
     lambda i: f"{i.get('duration_min')}m  {i.get('title')}  -> {i.get('loopback_wav')}"),
    ("mic-dropout", "Mic dropouts (your side of the call missing for a while)",
     lambda i: f"{i.get('dropouts')}x, {i.get('lost_seconds')}s lost  {i.get('title')}"),
    ("short-session-discarded", "Auto-discarded short sessions",
     lambda i: f"{i.get('seconds')}s  {i.get('title')}"),
)


def _doctor(argv: list[str]) -> int:
    """Print incidents from the last N days (default 7), grouped by kind."""
    from whispr.incidents import read_incidents

    since_days = float(argv[0]) if argv else 7.0
    cfg = load_config()
    incidents = read_incidents(cfg, since_days)

    print(f"whispr doctor - last {since_days:g} day(s)")
    for kind, heading, detail in _DOCTOR_SECTIONS:
        rows = [i for i in incidents if i.get("kind") == kind]
        print(f"\n{heading}: {len(rows)}")
        for row in rows:
            print(f"  {row.get('ts')}  {detail(row)}")
    if not incidents:
        print("\nNo incidents recorded - see logs/incidents.jsonl once any occur.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        return _run_app()
    cmd = argv[0]
    if cmd == "list-pending":
        from whispr.summarize import list_pending

        list_pending(argv[1:])
        return 0
    if cmd == "retry-summary":
        print("Note: 'retry-summary' is deprecated; use 'list-pending' instead.")
        from whispr.summarize import list_pending

        list_pending(argv[1:])
        return 0
    if cmd == "record-test":
        seconds = float(argv[1]) if len(argv) > 1 else 10.0
        return _record_test(seconds)
    if cmd == "doctor":
        return _doctor(argv[1:])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
