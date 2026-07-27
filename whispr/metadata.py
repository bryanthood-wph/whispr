"""Call metadata via Outlook (classic, COM) with a window-title fallback.

Best-effort and non-blocking to recording: called AFTER a recording stops. Matches
the recording's time window against calendar items (recurrences expanded) and picks
the best time-overlap. Uses recipient *display names* only — never touches
Recipient.Address / AddressEntry, so the Outlook object-model guard is never tripped.

If no calendar match (typical for ad-hoc calls), falls back to parsing the
counterpart name out of the Teams window title.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

from whispr.log import get_logger
from whispr.models import CallMetadata, CallSession
from whispr.winutil import com_initialized

log = get_logger("metadata")

_OL_FOLDER_CALENDAR = 9


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


def _fetch_from_outlook(session: CallSession, cfg: dict) -> Optional[CallMetadata]:
    """Return CallMetadata from the best-overlapping calendar item, or None."""
    tol = timedelta(seconds=cfg["metadata"]["calendar_match_tolerance_seconds"])
    notes_max = cfg["metadata"]["invite_notes_max_chars"]
    connect_retries = cfg["metadata"].get("outlook_connect_retries", 3)
    max_items = cfg["metadata"].get("max_calendar_items", 200)

    session_start = _to_naive(session.start)
    session_end = _to_naive(session.end) if session.end else session_start + timedelta(minutes=1)
    win_start = session_start - tol
    win_end = session_end + tol

    with com_initialized():
        app = _connect_outlook(connect_retries)
        if app is None:
            return None

        try:
            ns = app.GetNamespace("MAPI")

            suffix = cfg["trigger"].get("session_title_suffix", "")
            wanted = session.window_title
            if suffix and wanted.endswith(suffix):
                wanted = wanted[: -len(suffix)].strip()

            best = None
            best_overlap = 0.0
            subject_match = None
            for appt in _restricted_calendar_items(ns, win_start, win_end, max_items):
                a_start = _com_datetime_to_naive(getattr(appt, "Start", None))
                a_end = _com_datetime_to_naive(getattr(appt, "End", None))
                if a_start is None or a_end is None:
                    continue
                ov = _overlap_seconds(a_start, a_end, session_start, session_end)
                if ov <= 0.0:
                    continue
                if subject_match is None and wanted and _subject_matches(wanted, str(getattr(appt, "Subject", "") or "")):
                    subject_match = appt
                if ov > best_overlap:
                    best_overlap = ov
                    best = appt

            best = subject_match or best
            if best is None:
                log.info("no overlapping calendar item found for recording window")
                return None

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

            log.info("matched calendar item %r (overlap %.0fs, %d attendees)", title, best_overlap, len(attendees))
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

    Uses 1 Outlook connect attempt (fail-fast on the recording-start hot path).
    """
    tol = timedelta(seconds=cfg["metadata"]["calendar_match_tolerance_seconds"])
    max_items = cfg["metadata"].get("max_calendar_items", 200)
    when_naive = _to_naive(when)
    win_start = when_naive - tol
    win_end = when_naive + tol

    with com_initialized():
        app = _connect_outlook(1)  # fail-fast: 1 attempt on the hot path
        if app is None:
            return False
        try:
            ns = app.GetNamespace("MAPI")
            for appt in _restricted_calendar_items(ns, win_start, win_end, max_items):
                subject = str(getattr(appt, "Subject", "") or "")
                if _subject_matches(window_x, subject):
                    log.info("window %r matches live meeting %r -> classify as meeting", window_x, subject)
                    return True
            log.info("window %r matches no live calendar event -> classify as ad-hoc call", window_x)
            return False
        except Exception as exc:  # pragma: no cover
            log.warning("is_live_meeting lookup failed: %s", exc)
            return False


def _counterpart_from_title(title: str) -> Optional[str]:
    """Extract the other party's name from a Teams call window title.

    Handles 'Calls | Alice Smith', 'Alice Smith | Microsoft Teams', etc.
    """
    if not title:
        return None
    cleaned = title.strip()
    # Strip a leading 'Calls |' / 'Meeting |' style prefix.
    cleaned = re.sub(r"^(calls|meeting|meet)\s*\|\s*", "", cleaned, flags=re.IGNORECASE)
    # Strip a trailing '| Microsoft Teams' style suffix.
    cleaned = re.sub(r"\s*\|\s*microsoft teams\s*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" |")
    return cleaned or None


def _fallback_from_title(session: CallSession) -> CallMetadata:
    counterpart = _counterpart_from_title(session.window_title)
    log.info("using window-title fallback; counterpart=%r", counterpart)
    return CallMetadata(
        source="window-title",
        call_title=session.window_title.strip() or None,
        organizer=None,
        attendees=[counterpart] if counterpart else [],
        invite_notes=None,
    )


def fetch_metadata(session: CallSession, cfg: dict) -> CallMetadata:
    """Best-effort metadata for a finished session. Never raises.

    Only MEETINGS consult the Outlook calendar (they have a scheduled item to match).
    Ad-hoc CALLS use the window-title counterpart directly — matching a call against
    the calendar by time overlap grabs whatever unrelated event happens to be on the
    calendar at that moment (observed: a stray 'Enter MySource' placeholder).
    """
    if session.call_type == "meeting":
        try:
            meta = _fetch_from_outlook(session, cfg)
            if meta is not None:
                return meta
        except Exception as exc:  # pragma: no cover
            log.warning("metadata: outlook path errored: %s", exc)
    return _fallback_from_title(session)
