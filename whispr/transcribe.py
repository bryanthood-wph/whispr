"""Transcription stage: turns a CallSession's WAV streams into a merged TranscriptResult.

Loads a faster-whisper model lazily (once, cached) and runs it against the mic
("Me") and loopback ("Others") streams independently, then interleaves and merges
the resulting segments into speaker turns. Also provides a background queue worker
so back-to-back calls transcribe sequentially without blocking the caller.
"""

from __future__ import annotations

import os
import queue
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from faster_whisper import WhisperModel

from whispr.log import get_logger
from whispr.models import CallSession, Speaker, TranscriptResult, Turn

log = get_logger("transcribe")

_model: Optional[WhisperModel] = None
_model_lock = threading.Lock()


def get_model(cfg: dict[str, Any]) -> WhisperModel:
    """Return the process-wide WhisperModel, constructing it once from cfg["transcription"]."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                tcfg = cfg["transcription"]
                log.info("Loading WhisperModel(%s, device=%s, compute_type=%s)",
                            tcfg["model"], tcfg["device"], tcfg["compute_type"])
                _model = WhisperModel(
                    tcfg["model"], device=tcfg["device"], compute_type=tcfg["compute_type"],
                    local_files_only=tcfg.get("local_files_only", True),
                    download_root=tcfg.get("model_cache_dir"),
                )
    return _model


def _wav_usable(path: Optional[str]) -> bool:
    """True if path is set, exists, and is non-empty."""
    if not path:
        return False
    p = Path(path)
    return p.is_file() and p.stat().st_size > 0


def _transcribe_stream(
    wav_path: Optional[str], speaker: Speaker, cfg: dict[str, Any]
) -> list[tuple[float, Speaker, str]]:
    """Run the model over one WAV stream, returning (start_seconds, speaker, text) items."""
    if not _wav_usable(wav_path):
        log.warning("Skipping %s stream: missing or empty WAV (%r)", speaker, wav_path)
        return []

    tcfg = cfg["transcription"]
    model = get_model(cfg)
    segments, _info = model.transcribe(
        wav_path,
        beam_size=tcfg["beam_size"],
        language=tcfg["language"],
        vad_filter=tcfg["vad_filter"],
        vad_parameters=dict(min_silence_duration_ms=tcfg["vad_min_silence_ms"]),
    )

    items: list[tuple[float, Speaker, str]] = []
    for seg in segments:  # generator is lazy — iterate to force transcription
        text = seg.text.strip()
        if text:
            items.append((seg.start, speaker, text))
    return items


def _merge_turns(
    items: list[tuple[float, Speaker, str]], turn_merge_gap_seconds: float
) -> list[Turn]:
    """Sort chronologically and merge consecutive same-speaker items within the gap threshold."""
    items = sorted(items, key=lambda item: item[0])
    turns: list[Turn] = []
    for start, speaker, text in items:
        if turns and turns[-1].speaker == speaker and (start - turns[-1].start_seconds) < turn_merge_gap_seconds:
            turns[-1].text = f"{turns[-1].text} {text}"
        else:
            turns.append(Turn(start_seconds=start, speaker=speaker, text=text))
    return turns


def transcribe_session(session: CallSession, cfg: dict[str, Any]) -> TranscriptResult:
    """Transcribe both streams of a CallSession and return merged, ordered Turns.

    Returns TranscriptResult(turns=[], is_stub=True) if no speech was detected in
    either stream.
    """
    tcfg = cfg["transcription"]
    mic_items = _transcribe_stream(session.mic_wav, "Me", cfg)
    loopback_items = _transcribe_stream(session.loopback_wav, "Others", cfg)

    all_items = mic_items + loopback_items
    if not all_items:
        return TranscriptResult(turns=[], is_stub=True)

    turns = _merge_turns(all_items, tcfg["turn_merge_gap_seconds"])
    return TranscriptResult(turns=turns, is_stub=False)


class TranscriptionQueue:
    """Background worker that transcribes CallSessions one at a time, in submission order."""

    def __init__(self, cfg: dict[str, Any], on_complete: Callable[[CallSession, TranscriptResult], None]) -> None:
        self._cfg = cfg
        self._on_complete = on_complete
        self._queue: queue.Queue[Optional[CallSession]] = queue.Queue()
        self._sentinel: Optional[CallSession] = None
        self._thread = threading.Thread(target=self._run, name="transcription-queue", daemon=True)
        self._thread.start()

    def _lower_priority(self) -> None:
        """Best-effort: lower this process's priority on Windows so transcription doesn't
        compete with the foreground call recording/UI."""
        try:
            import psutil

            proc = psutil.Process(os.getpid())
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        except Exception:
            log.debug("Could not lower process priority", exc_info=True)

    def _run(self) -> None:
        self._lower_priority()
        while True:
            session = self._queue.get()
            if session is self._sentinel:
                break
            try:
                result = transcribe_session(session, self._cfg)
            except Exception:
                log.exception("Transcription failed for session starting %s", session.start)
                continue
            try:
                self._on_complete(session, result)
            except Exception:
                log.exception("on_complete callback failed for session starting %s", session.start)

    def submit(self, session: CallSession) -> None:
        """Enqueue a session for transcription."""
        self._queue.put(session)

    def stop(self, join_timeout: Optional[float] = None) -> None:
        """Signal the worker to stop after finishing all queued jobs.

        Pass join_timeout to block until the worker drains and exits (used on app
        quit so an in-flight transcription isn't lost when the daemon thread is
        killed at process exit). A queued 60-min call can take a few minutes to
        finish, so callers that must not lose data should pass a generous timeout.
        """
        self._queue.put(self._sentinel)
        if join_timeout is not None:
            self._thread.join(timeout=join_timeout)
            if self._thread.is_alive():
                log.warning("transcription worker still running after %.0fs join timeout", join_timeout)
