"""Teams window-title watcher + call-state machine.

Sets a global WinEvent hook for EVENT_OBJECT_NAMECHANGE and filters events to the
Teams process by name. Title transitions drive a small state machine:

    idle  --meeting title-->  recording (auto)
    idle  --call title-->     recording + Yes/No prompt (No => discard)
    recording --end title (confirmed by pycaw)--> stop & keep

A pycaw-based backstop force-stops a recording if Teams' audio session has been
gone for a configured number of seconds, covering a missed end-title event.

Classification (`classify_title`) is a pure function so it can be unit-tested
without a live Teams client. The hook + message pump require a real desktop
session and a live call to fully verify (LIVE GATE).
"""

from __future__ import annotations

import ctypes
import re
import threading
from ctypes import wintypes
from typing import Callable, Literal, Optional

from whispr.log import get_logger
from whispr.winutil import (
    EVENT_OBJECT_NAMECHANGE,
    WINEVENT_OUTOFCONTEXT,
    WINEVENT_SKIPOWNPROCESS,
    com_initialized,
    process_name_for_hwnd,
    window_title,
)

log = get_logger("watcher")

Classification = Literal["session", "end"]

_THREAD_JOIN_TIMEOUT = 5.0

# MessageBox constants (for the ad-hoc call prompt).
_MB_YESNO = 0x00000004
_MB_ICONQUESTION = 0x00000020
_MB_TOPMOST = 0x00040000
_MB_SYSTEMMODAL = 0x00001000
_IDYES = 6
_IDNO = 7
_MB_TIMEDOUT = 32000
_OBJID_WINDOW = 0
_CHILDID_SELF = 0


def session_subject(title: str, cfg: dict) -> Optional[str]:
    """If `title` is an active call/meeting window, return its subject X, else None.

    A session window is `<X> | Microsoft Teams` where X does not start with a nav
    keyword (Chat/Activity/etc.). Meeting-vs-call is NOT decided here — both look
    identical; that's resolved by an Outlook cross-reference in the watcher.
    """
    trg = cfg["trigger"]
    suffix = trg["session_title_suffix"]
    if not title or not title.endswith(suffix):
        return None
    x = title[: -len(suffix)].strip()
    if not x:
        return None
    # Reject nav/chat panes. Appending " |" lets one check match both a bare nav
    # window (x="Calls" -> "Calls |") and a chat window (x="Chat | Bob" -> "Chat | Bob |").
    probe = (x + " |").lower()
    for nav in trg.get("nav_prefixes", []):
        if probe.startswith(nav.lower()):
            return None
    return x


def classify_title(title: str, cfg: dict) -> Optional[Classification]:
    """Classify a Teams window title. Returns 'session' | 'end' | None.

    'session' = an active call/meeting window (subject via `session_subject`).
    'end'     = a home/nav title (call returned to home).
    End patterns win, so a return-to-home title isn't misread as a new session.
    """
    if not title:
        return None
    for pat in cfg["trigger"].get("end_title_patterns", []):
        if re.search(pat, title, re.IGNORECASE):
            return "end"
    if session_subject(title, cfg) is not None:
        return "session"
    return None


def teams_audio_active(cfg: dict) -> bool:
    """True if any configured Teams-related process has an ACTIVE audio session.

    Best-effort; returns False on any error. Requires COM to be initialized on the
    calling thread (the watcher thread does this).
    """
    try:
        from pycaw.pycaw import AudioUtilities

        targets = {n.lower() for n in cfg["trigger"]["audio_session_process_names"]}
        for session in AudioUtilities.GetAllSessions():
            proc = session.Process
            if proc is None:
                continue
            try:
                name = proc.name().lower()
            except Exception:
                continue
            if name in targets and session.State == 1:  # 1 = AudioSessionStateActive
                return True
    except Exception as exc:  # pragma: no cover - host dependent
        log.debug("teams_audio_active check failed: %s", exc)
    return False


def _backstop_step(
    recording: bool,
    window_exists: bool,
    audio_active: bool,
    gone_for: float,
    audio_seen: bool,
    no_window_polls: int,
    gone_limit: float,
    confirm_polls: int,
    poll_seconds: float,
) -> tuple[float, bool, int, bool]:
    """Pure step for the audio-backstop state machine.

    Returns (gone_for, audio_seen, no_window_polls, should_stop).
    Stateless: caller holds the mutable state and passes it in each iteration.
    """
    if not recording:
        return 0.0, False, 0, False
    # Primary end signal: the call/meeting window is gone (debounced).
    if window_exists:
        no_window_polls = 0
    else:
        no_window_polls += 1
        if no_window_polls >= confirm_polls:
            return 0.0, False, 0, True
    # Secondary net: audio was present then gone for too long.
    if audio_active:
        audio_seen = True
        gone_for = 0.0
    else:
        gone_for += poll_seconds
    if audio_seen and gone_for >= gone_limit:
        return 0.0, False, 0, True
    return gone_for, audio_seen, no_window_polls, False


class TeamsWatcher:
    """Drives recording lifecycle from Teams window titles.

    Callbacks (all called from the watcher thread; keep them quick / non-blocking):
      on_start(call_type, title)  -> begin recording
      on_stop()                   -> stop & keep (transcribe)
      on_discard()                -> stop & delete (no transcript)
    """

    def __init__(
        self,
        cfg: dict,
        on_start: Callable[[str, str], None],
        on_stop: Callable[[], None],
        on_discard: Callable[[], None],
        is_meeting: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self._cfg = cfg
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_discard = on_discard
        # Given a session-window subject, return True if it's a scheduled meeting
        # (auto-record) vs an ad-hoc call (prompt). Injected so the Outlook
        # dependency stays out of this module and classification stays testable.
        # Default: treat everything as a call (prompt) — the safe fallback.
        self._is_meeting = is_meeting or (lambda subject: False)

        self._process_name = cfg["trigger"]["process_name"].lower()
        self._state_lock = threading.Lock()
        self._recording = False
        # Subject of the session currently recording (set on start, cleared on stop).
        self._active_subject: Optional[str] = None
        # Subject the user stopped while its call/meeting window was still live.
        # Suppresses auto-re-recording that same still-open session; the backstop
        # clears it once the window is gone. See notify_stopped / _handle_title.
        self._suppressed_subject: Optional[str] = None
        # True from the moment a stop begins until notify_stopped completes.
        # recorder.stop() can take seconds, during which _recording is already
        # False; without this the hook thread and the backstop thread could each
        # start a NEW session in that window, and the stop's trailing (out-of-lock)
        # notify_stopped would then clobber the new session's flags. Keeping the
        # start-gate closed for the whole teardown closes that race. Set under
        # _state_lock; never held across the blocking stop itself.
        self._stopping = False

        self._thread: Optional[threading.Thread] = None
        self._hook = None
        self._proc_callback = None  # keep a ref so the CFUNCTYPE isn't GC'd
        self._thread_id = None
        self._pid_name_cache: dict[int, str] = {}

        self._user32 = ctypes.windll.user32
        self._stop_event = threading.Event()

    # -- public lifecycle -----------------------------------------------

    def start(self) -> None:
        """Start the watcher thread (message pump + hook)."""
        self._thread = threading.Thread(target=self._run, name="whispr-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the watcher thread by posting a quit to its message loop."""
        self._stop_event.set()
        if self._thread_id is not None:
            # WM_QUIT = 0x0012
            self._user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=_THREAD_JOIN_TIMEOUT)

    def notify_stopped(self) -> None:
        """Reset _recording from outside the watcher thread.

        Called by the orchestrator on every stop path so that a tray-menu stop
        (which calls Orchestrator._finish directly, bypassing _do_stop) doesn't
        leave _recording=True and block the next session from starting.
        Idempotent: safe to call when already stopped.

        If _active_subject is still set here, the stop did NOT come through the
        watcher's own end detection (_do_stop clears _active_subject first) — i.e.
        the user hit the tray kill switch while the call/meeting may still be live.
        Latch that subject as suppressed so the still-open window doesn't
        immediately auto-re-record on its next title change; the backstop drops the
        latch once the window is gone (a genuinely new call is unaffected).
        """
        with self._state_lock:
            self._recording = False
            self._stopping = False  # teardown complete; re-open the start-gate
            if self._active_subject is not None:
                self._suppressed_subject = self._active_subject
            self._active_subject = None

    def _start_suppressed(self, subject: str) -> bool:
        """True if `subject` is the session the user just stopped while it stayed live."""
        with self._state_lock:
            return self._suppressed_subject is not None and subject == self._suppressed_subject

    # -- watcher thread --------------------------------------------------

    def _run(self) -> None:
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        with com_initialized():
            WinEventProcType = ctypes.WINFUNCTYPE(
                None,
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.HWND,
                wintypes.LONG,
                wintypes.LONG,
                wintypes.DWORD,
                wintypes.DWORD,
            )
            self._proc_callback = WinEventProcType(self._on_win_event)
            self._hook = self._user32.SetWinEventHook(
                EVENT_OBJECT_NAMECHANGE,
                EVENT_OBJECT_NAMECHANGE,
                0,
                self._proc_callback,
                0,
                0,
                WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
            )
            if not self._hook:
                log.error("SetWinEventHook failed; watcher inactive")
                return
            log.info("watcher active (hook set on %s title changes)", self._process_name)

            # Start the audio-gone backstop timer.
            backstop = threading.Thread(target=self._audio_backstop_loop, daemon=True)
            backstop.start()

            # Standard Win32 message pump. GetMessageW blocks until a message arrives;
            # WM_QUIT (posted by stop()) returns 0 and ends the loop.
            msg = wintypes.MSG()
            while not self._stop_event.is_set():
                ret = self._user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
                if ret == 0 or ret == -1:
                    break
                self._user32.TranslateMessage(ctypes.byref(msg))
                self._user32.DispatchMessageW(ctypes.byref(msg))

            self._user32.UnhookWinEvent(self._hook)
        log.info("watcher stopped")

    def _session_window_exists(self, subject: Optional[str] = None) -> bool:
        """True if a Teams top-level window is currently a live call/meeting window.

        With `subject`, only a window whose session subject equals it counts (used
        to detect when one specific user-stopped session's window is gone); without
        it, any session window counts.

        Enumerates windows and checks for a session title. This is the reliable
        end signal: closing the call window fires no NAMECHANGE event, and Teams'
        pycaw audio session lingers ~minutes after leaving, so neither the title
        hook nor the audio backstop detects the end promptly. Window disappearance
        does — within one poll (~2 s).
        """
        found = {"v": False}

        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            try:
                if process_name_for_hwnd(hwnd, self._pid_name_cache) != self._process_name:
                    return True
                subj = session_subject(window_title(hwnd), self._cfg)
                if subj is not None and (subject is None or subj == subject):
                    found["v"] = True
                    return False  # stop enumerating
            except Exception:
                pass
            return True

        try:
            self._user32.EnumWindows(EnumProc(_cb), 0)
        except Exception as exc:  # pragma: no cover
            log.debug("EnumWindows failed: %s", exc)
            return True  # fail-safe: assume still in a call, don't stop on error
        return found["v"]

    def _find_live_session(self, skip_subject: Optional[str] = None) -> Optional[tuple[str, str]]:
        """Enumerate Teams windows for a live session; return (title, subject) or None.

        Poll-based fallback for starting a recording, run from the backstop loop
        alongside the existing end-detection poll. The WinEvent hook is normally
        the (faster) start signal, but it has been observed to go silent for hours
        with no error while the process stays alive; this bounds that outage to
        one backstop poll interval instead of indefinitely.

        `skip_subject` (the currently-suppressed session, if any) is passed over so
        a still-open user-stopped window can't mask a genuinely new call that
        enumerates behind it — without it, that new call would never be found while
        the suppressed window stays open.
        """
        result: dict[str, Optional[str]] = {"title": None, "subject": None}

        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            try:
                if process_name_for_hwnd(hwnd, self._pid_name_cache) != self._process_name:
                    return True
                title = window_title(hwnd)
                subj = session_subject(title, self._cfg)
                if subj is not None and subj != skip_subject:
                    result["title"] = title
                    result["subject"] = subj
                    return False  # stop enumerating
            except Exception:
                pass
            return True

        try:
            self._user32.EnumWindows(EnumProc(_cb), 0)
        except Exception as exc:  # pragma: no cover
            log.debug("EnumWindows failed during start-poll: %s", exc)
            return None
        if result["subject"] is not None:
            return result["title"], result["subject"]
        return None

    def _on_win_event(self, hWinEventHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
        # Only window-level name changes.
        if idObject != _OBJID_WINDOW or idChild != _CHILDID_SELF or not hwnd:
            return
        try:
            if process_name_for_hwnd(hwnd, self._pid_name_cache) != self._process_name:
                return
            title = window_title(hwnd)
            if title:
                self._handle_title(title)
        except Exception as exc:  # never let an exception escape into the OS callback
            log.debug("event handler error: %s", exc)

    # -- state machine ---------------------------------------------------

    def _handle_title(self, title: str) -> None:
        cls = classify_title(title, self._cfg)
        if cls is None:
            return
        with self._state_lock:
            recording = self._recording
            stopping = self._stopping

        if cls == "end" and recording:
            # Confirm with pycaw that audio really stopped (avoid transient title flips).
            if teams_audio_active(self._cfg):
                log.debug("end-title seen but Teams audio still active; ignoring")
                return
            log.info("end detected (title=%r)", title)
            self._do_stop(keep=True)
        elif cls == "session" and not recording and not stopping:
            # `not stopping`: don't start a new session while a prior stop is still
            # tearing down (see _stopping). _do_start re-checks under the lock too,
            # but gating here also avoids the wasted is_meeting/audio probe.
            subject = session_subject(title, self._cfg) or title
            self._maybe_start(title, subject)

    def _maybe_start(self, title: str, subject: str, *, via_poll: bool = False) -> None:
        """Shared start path for both the WinEvent hook and the poll fallback.

        `via_poll=True` means the WinEvent hook did not fire for this title change
        and the backstop poll caught it instead — logged at WARNING since that
        indicates the hook may have gone silent (see 2026-07-16 incident: the hook
        stopped delivering events for ~21h with no error, process still alive).
        """
        # Don't auto-re-record a session the user just stopped while it stayed
        # live: its window is still open and firing title changes, but the kill
        # switch means "leave this one alone" until the window actually closes.
        if self._start_suppressed(subject):
            log.debug("session %r suppressed (stopped by user; window still live)", subject)
            return
        # Confirm a call is actually live before starting (a session-shaped title
        # alone isn't proof of audio); avoids false starts on stray windows.
        if not teams_audio_active(self._cfg):
            log.debug("session title %r but no active Teams audio; ignoring", title)
            return
        try:
            meeting = bool(self._is_meeting(subject))
        except Exception as exc:
            log.warning("is_meeting classifier failed (%s); treating as call", exc)
            meeting = False
        call_type = "meeting" if meeting else "call"
        # Log the detection only AFTER a real start. If the hook and the backstop
        # poll both catch the same title change, only one wins _do_start's lock; the
        # loser must stay silent, otherwise the via_poll branch would emit the "hook
        # did not fire" WARNING even when the hook actually fired first.
        if not self._do_start(call_type, title, subject):
            return
        if via_poll:
            log.warning(
                "%s detected via poll fallback (title=%r); WinEvent hook did not fire "
                "for this title change",
                call_type, title,
            )
        else:
            log.info("%s detected (title=%r)", call_type, title)

    def _do_start(self, call_type: str, title: str, subject: str) -> bool:
        """Begin recording. Returns True if this call started it, False if it was
        already recording/stopping (another thread won) or on_start failed."""
        with self._state_lock:
            if self._recording or self._stopping:
                return False
            self._recording = True
            self._active_subject = subject
        try:
            self._on_start(call_type, title)
        except Exception as exc:
            log.error("on_start callback failed: %s", exc)
            with self._state_lock:
                self._recording = False
                self._active_subject = None
            return False
        if call_type == "call":
            # Recording is already running; ask whether to keep. Non-blocking.
            threading.Thread(
                target=self._prompt_call, args=(title,), name="whispr-prompt", daemon=True
            ).start()
        return True

    def _do_stop(self, keep: bool) -> None:
        with self._state_lock:
            if not self._recording:
                return
            self._recording = False
            # Hold the start-gate closed through the (slow) teardown that follows,
            # so a concurrent hook/backstop start can't slip in while _recording is
            # already False. Cleared by notify_stopped at the end of teardown.
            self._stopping = True
            # Watcher-detected end (window gone / confirmed end-title): the session
            # genuinely ended, so clear the active subject BEFORE the on_stop
            # callback runs notify_stopped — that's what tells notify_stopped this
            # was not a tray kill-switch stop and must not suppress a re-record.
            self._active_subject = None
        try:
            if keep:
                self._on_stop()
            else:
                self._on_discard()
        except Exception as exc:
            log.error("stop callback failed: %s", exc)

    def _prompt_call(self, title: str) -> None:
        keep = self._show_prompt(title)
        if not keep:
            log.info("user declined call recording; discarding")
            self._do_stop(keep=False)

    def _show_prompt(self, title: str) -> bool:
        """Topmost Yes/No box with timeout. Returns True to keep recording.

        Uses the undocumented-but-stable user32.MessageBoxTimeoutW. On timeout or a
        locked screen (call fails), applies the configured no_answer_action.
        """
        pcfg = self._cfg["prompt"]
        timeout_ms = int(pcfg["timeout_seconds"] * 1000)
        keep_on_no_answer = pcfg["no_answer_action"] == "keep"
        text = f"Record this Teams call?\n\n{title}\n\n(No answer = {'keep' if keep_on_no_answer else 'discard'})"
        caption = "whispr"
        flags = _MB_YESNO | _MB_ICONQUESTION | _MB_TOPMOST | _MB_SYSTEMMODAL
        try:
            result = self._user32.MessageBoxTimeoutW(0, text, caption, flags, 0, timeout_ms)
        except Exception as exc:
            log.warning("prompt failed (%s); applying no_answer_action=%s", exc, pcfg["no_answer_action"])
            return keep_on_no_answer
        if result == _IDYES:
            return True
        if result == _IDNO:
            return False
        # Timeout / dismissed.
        return keep_on_no_answer

    # -- audio backstop --------------------------------------------------

    def _audio_backstop_loop(self) -> None:
        with com_initialized():
            gone_limit = self._cfg["trigger"]["audio_gone_stop_seconds"]
            poll_seconds = self._cfg["trigger"].get("window_gone_poll_seconds", 2.0)
            confirm_polls = self._cfg["trigger"].get("window_gone_confirm_polls", 2)
            gone_for = 0.0
            audio_seen = False
            no_window_polls = 0
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=poll_seconds)
                with self._state_lock:
                    recording = self._recording
                    stopping = self._stopping
                    suppressed = self._suppressed_subject
                # Re-arm: once the window of a session the user stopped-while-live
                # has closed, drop the suppression so a later same-subject call can
                # record again. _session_window_exists fails safe to True, so an
                # enumeration error keeps the suppression rather than dropping it.
                if suppressed is not None and not self._session_window_exists(suppressed):
                    with self._state_lock:
                        if self._suppressed_subject == suppressed:
                            self._suppressed_subject = None
                    log.info("re-armed: stopped session %r window gone", suppressed)
                # Start-detection fallback: the WinEvent hook is the normal (faster)
                # start signal, but has been observed to go silent indefinitely with
                # no error. Poll for a live session window whenever not recording so
                # a hook outage is bounded to one poll interval instead of forever.
                if not recording and not stopping:
                    live = self._find_live_session(skip_subject=suppressed)
                    if live is not None:
                        self._maybe_start(live[0], live[1], via_poll=True)
                        with self._state_lock:
                            recording = self._recording
                try:
                    window_exists = self._session_window_exists()
                    audio_active = teams_audio_active(self._cfg)
                except Exception:
                    log.exception("backstop poll error; continuing")
                    continue
                prev_gone_for = gone_for
                gone_for, audio_seen, no_window_polls, should_stop = _backstop_step(
                    recording, window_exists, audio_active,
                    gone_for, audio_seen, no_window_polls,
                    gone_limit, confirm_polls, poll_seconds,
                )
                if should_stop:
                    if not window_exists:
                        log.info("call/meeting window gone; stopping")
                    else:
                        log.info(
                            "Teams audio gone %.0fs after being active; force-stopping",
                            prev_gone_for + poll_seconds,
                        )
                    self._do_stop(keep=True)
