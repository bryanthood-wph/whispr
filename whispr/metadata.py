"""Call metadata via Outlook (classic, COM) with a window-title fallback.

Best-effort and non-blocking to recording: called AFTER a recording stops. Matches
the recording's time window against calendar items (recurrences expanded) and picks
the best time-overlap. Uses recipient *display names* only — never touches
Recipient.Address / AddressEntry, so the Outlook object-model guard is never tripped.

If no calendar match (typical for ad-hoc calls), falls back to parsing the
counterpart name out of the Teams window title.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Callable, Optional, TypeVar

from whispr.log import get_logger
from whispr.models import CallMetadata, CallSession
from whispr.winutil import com_initialized

log = get_logger("metadata")

_T = TypeVar("_T")

# Outlook object-model constants.
_OL_FOLDER_CALENDAR = 9
_OL_NON_MEETING = 0                 # olNonMeeting: a personal appointment (blocks, reminders)
_OL_CANCELED = (5, 7)               # olMeetingCanceled, olMeetingReceivedAndCanceled
_OL_RESPONSE_DECLINED = 4           # olResponseDeclined

# HRESULTs Outlook returns while busy (modal dialog, send/receive). Transient.
_OUTLOOK_BUSY_HRESULTS = (
    -2147418111,  # 0x80010001 RPC_E_CALL_REJECTED: "Call was rejected by callee."
    -2147417846,  # 0x8001010A RPC_E_SERVERCALL_RETRYLATER
)


def _is_outlook_busy(exc: Exception) -> bool:
    return bool(exc.args) and exc.args[0] in _OUTLOOK_BUSY_HRESULTS


def _retry_when_busy(fn: Callable[[], _T], cfg: dict) -> _T:
    """Run `fn`, retrying while Outlook reports busy. Other errors propagate."""
    retries = cfg["metadata"]["outlook_busy_retries"]
    wait = cfg["metadata"]["outlook_busy_retry_seconds"]
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            if attempt >= retries or not _is_outlook_busy(exc):
                raise
            attempt += 1
            log.info("Outlook busy (%s); retry %d/%d in %.1fs", exc, attempt, retries, wait)
            time.sleep(wait)


def subject_core(title: str, cfg: dict) -> str:
    """The part of a Teams session-window title that names the meeting or person.

    Drops the ' | Microsoft Teams' suffix, every segment listed in
    trigger.generic_title_segments, and any segment containing '@' (the account).
    'Meeting join | Kroger FIH Onboarding | Deloitte (O365D) | me@x.com' -> 'Kroger FIH
    Onboarding'. Returns '' for a generic title that names nothing, e.g. 'Deloitte
    (O365D) | me@x.com' — Teams shows that for some meeting windows.
    """
    trg = cfg["trigger"]
    text = (title or "").strip()
    suffix = trg["session_title_suffix"].strip()
    if suffix and text.endswith(suffix):
        text = text[: -len(suffix)]
    generic = {s.strip().lower() for s in trg.get("generic_title_segments", [])}
    kept = [
        seg.strip() for seg in text.split("|")
        if seg.strip() and seg.strip().lower() not in generic and "@" not in seg
    ]
    return " | ".join(kept)


def _is_overlap_candidate(appt, online_marker: str) -> bool:
    """True if a calendar item is plausibly a Teams meeting the user attended.

    Overlap alone is weak evidence: an all-day event or a personal 'Block' overlaps
    100% of any recording (observed: transcripts titled 'Neil - OOO' and 'The fourth
    snapshot deadline for PMY27...').
    """
    if getattr(appt, "AllDayEvent", False):
        return False
    status = getattr(appt, "MeetingStatus", None)
    if status == _OL_NON_MEETING or status in _OL_CANCELED:
        return False
    if getattr(appt, "ResponseStatus", None) == _OL_RESPONSE_DECLINED:
        return False
    body = str(getattr(appt, "Body", "") or "")
    return online_marker.lower() in body.lower()


def _pick_by_overlap(candidates: list[tuple[object, float]], recording_seconds: float,
                     min_fraction: float) -> Optional[tuple[object, float]]:
    """Return the single (event, overlap) covering >= min_fraction of the recording.

    None when no candidate qualifies, or when more than one does (two meetings at the
    same time, or a recording spanning back-to-back meetings) — guessing there
    mislabels the transcript, which is worse than the window-title fallback.
    """
    floor = recording_seconds * min_fraction
    qualifying = [c for c in candidates if c[1] >= floor]
    return qualifying[0] if len(qualifying) == 1 else None


def _to_naive(dt: datetime) -> datetime:
    """Drop tzinfo for comparison against Outlook's local-time values."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _com_datetime_to_naive(value) -> Optional[datetime]:
    """Convert a pywintypes time (or datetime) to a naive python datetime."""
    try:
        return datetime(value.year, value.month, value.day, value.hour, value.minute, value.second)
    except Exception:
        return None


def _restrict_string(start: datetime, end: datetime) -> str:
    """Outlook Restrict DASL-ish filter over [Start]/[End] using US-format local times."""
    fmt = "%m/%d/%Y %I:%M %p"
    return f"[Start] <= '{end.strftime(fmt)}' AND [End] >= '{start.strftime(fmt)}'"


def _overlap_seconds(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    latest_start = max(a_start, b_start)
    earliest_end = min(a_end, b_end)
    return max(0.0, (earliest_end - latest_start).total_seconds())


def _connect_outlook(retries: int):
    """Connect to Outlook.Application with up to `retries` attempts. Returns app or None."""
    import win32com.client

    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            return win32com.client.Dispatch("Outlook.Application")
        except Exception as exc:  # pragma: no cover - COM/host dependent
            last_exc = exc
            log.debug("Outlook Dispatch attempt %d failed: %s", attempt + 1, exc)
    log.warning("could not connect to Outlook: %s", last_exc)
    return None


def _restricted_calendar_items(ns, win_start: datetime, win_end: datetime, max_items: int):
    """Yield calendar appointments overlapping [win_start, win_end], up to max_items."""
    cal = ns.GetDefaultFolder(_OL_FOLDER_CALENDAR)
    items = cal.Items
    # Must sort ascending and expand recurrences BEFORE restricting, or expanded
    # occurrences are not returned.
    items.IncludeRecurrences = True
    items.Sort("[Start]")
    restricted = items.Restrict(_restrict_string(win_start, win_end))
    count = 0
    for appt in restricted:
        count += 1
        if count > max_items:
            break
        yield appt


def _fetch_from_outlook(session: CallSession, cfg: dict, core: str, allow_overlap: bool) -> Optional[CallMetadata]:
    """Return CallMetadata for the calendar item this session was, or None.

    A subject match on `core` wins. Otherwise, if `allow_overlap`, the single
    candidate event (see _is_overlap_candidate) covering most of the recording.
    """
    mcfg = cfg["metadata"]
    tol = timedelta(seconds=mcfg["calendar_match_tolerance_seconds"])
    notes_max = mcfg["invite_notes_max_chars"]
    connect_retries = mcfg.get("outlook_connect_retries", 3)
    max_items = mcfg.get("max_calendar_items", 200)

    session_start = _to_naive(session.start)
    session_end = _to_naive(session.end) if session.end else session_start + timedelta(minutes=1)
    recording_seconds = max(1.0, (session_end - session_start).total_seconds())
    win_start = session_start - tol
    win_end = session_end + tol

    with com_initialized():
        app = _connect_outlook(connect_retries)
        if app is None:
            return None

        def _match():
            ns = app.GetNamespace("MAPI")
            subject_match = None
            candidates: list[tuple[object, float]] = []
            for appt in _restricted_calendar_items(ns, win_start, win_end, max_items):
                a_start = _com_datetime_to_naive(getattr(appt, "Start", None))
                a_end = _com_datetime_to_naive(getattr(appt, "End", None))
                if a_start is None or a_end is None:
                    continue
                ov = _overlap_seconds(a_start, a_end, session_start, session_end)
                if ov <= 0.0:
                    continue
                if core and _subject_matches(core, str(getattr(appt, "Subject", "") or "")):
                    return appt, ov, "subject"
                if allow_overlap and _is_overlap_candidate(appt, mcfg["online_meeting_marker"]):
                    candidates.append((appt, ov))
            picked = _pick_by_overlap(candidates, recording_seconds, mcfg["overlap_min_fraction"])
            return None if picked is None else (*picked, "overlap")

        try:
            found = _retry_when_busy(_match, cfg)
            if found is None:
                log.info("no calendar item identifies this recording (core=%r, overlap allowed=%s)",
                         core, allow_overlap)
                return None
            best, best_overlap, how = found

            title = str(getattr(best, "Subject", "") or "") or None
            organizer = str(getattr(best, "Organizer", "") or "") or None

            attendees: list[str] = []
            try:
                for r in best.Recipients:
                    name = str(getattr(r, "Name", "") or "").strip()
                    if name and name not in attendees:
                        attendees.append(name)
            except Exception as exc:  # pragma: no cover
                log.warning("could not read recipients: %s", exc)

            invite_notes = None
            try:
                body = str(getattr(best, "Body", "") or "").strip()
                if body:
                    invite_notes = body[:notes_max]
            except Exception as exc:  # pragma: no cover
                log.warning("could not read body: %s", exc)

            log.info("matched calendar item %r by %s (overlap %.0fs, %d attendees)",
                     title, how, best_overlap, len(attendees))
            return CallMetadata(
                source="outlook",
                call_title=title,
                organizer=organizer,
                attendees=attendees,
                invite_notes=invite_notes,
            )
        except Exception as exc:  # pragma: no cover
            log.warning("Outlook metadata lookup failed: %s", exc)
            return None


def _subject_matches(window_x: str, event_subject: str) -> bool:
    """Fuzzy match a session-window subject against a calendar event subject.

    Teams may truncate long subjects in the title bar, so accept exact match or
    either-contains (with a length guard to avoid trivial matches).
    """
    a = window_x.strip().lower()
    b = event_subject.strip().lower()
    if len(a) < 3 or len(b) < 3:
        return False
    return a == b or a in b or b in a


def is_live_meeting(window_x: str, when: datetime, cfg: dict) -> bool:
    """True if an Outlook calendar event is live at `when` whose subject matches the
    session-window title `window_x` — i.e. this Teams window is a SCHEDULED MEETING
    rather than an ad-hoc call. Best-effort; returns False on any error or no match
    (so an unmatched window is treated as an ad-hoc call and prompts).

    Uses 1 Outlook connect attempt (fail-fast on the recording-start hot path), but
    retries a busy Outlook briefly — see metadata.outlook_busy_retries.
    """
    tol = timedelta(seconds=cfg["metadata"]["calendar_match_tolerance_seconds"])
    max_items = cfg["metadata"].get("max_calendar_items", 200)
    when_naive = _to_naive(when)
    win_start = when_naive - tol
    win_end = when_naive + tol

    core = subject_core(window_x, cfg)
    if not core:
        log.info("window %r names no meeting -> classify as ad-hoc call", window_x)
        return False

    with com_initialized():
        app = _connect_outlook(1)  # fail-fast: 1 attempt on the hot path
        if app is None:
            return False

        def _lookup() -> Optional[str]:
            ns = app.GetNamespace("MAPI")
            for appt in _restricted_calendar_items(ns, win_start, win_end, max_items):
                subject = str(getattr(appt, "Subject", "") or "")
                if _subject_matches(core, subject):
                    return subject
            return None

        try:
            subject = _retry_when_busy(_lookup, cfg)
        except Exception as exc:  # pragma: no cover
            log.warning("is_live_meeting lookup failed: %s", exc)
            return False
        if subject is None:
            log.info("window %r matches no live calendar event -> classify as ad-hoc call", window_x)
            return False
        log.info("window %r matches live meeting %r -> classify as meeting", window_x, subject)
        return True


def _fallback_from_title(session: CallSession, core: str) -> CallMetadata:
    log.info("using window-title fallback; core=%r", core)
    return CallMetadata(
        source="window-title",
        call_title=core or session.window_title.strip() or None,
        organizer=None,
        # An ad-hoc call's title is the other party; a meeting's is its subject.
        attendees=[core] if core and session.call_type == "call" else [],
        invite_notes=None,
    )


def fetch_metadata(session: CallSession, cfg: dict) -> CallMetadata:
    """Best-effort metadata for a finished session. Never raises.

    Every session tries a calendar SUBJECT match on the title's core. Falling back to
    time OVERLAP is allowed only for meetings and for generic titles that name
    nothing. A call titled with a person's name must not take overlap: it grabs
    whatever unrelated event fills that slot (observed: a stray 'Enter MySource'
    placeholder). A generic title has no other evidence, so overlap is its only
    chance, held to the filters in _is_overlap_candidate / _pick_by_overlap.
    """
    core = subject_core(session.window_title, cfg)
    allow_overlap = session.call_type == "meeting" or not core
    try:
        meta = _fetch_from_outlook(session, cfg, core, allow_overlap)
        if meta is not None:
            return meta
    except Exception as exc:  # pragma: no cover
        log.warning("metadata: outlook path errored: %s", exc)
    return _fallback_from_title(session, core)
