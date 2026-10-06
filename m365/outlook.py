"""Classic Outlook over COM for whispr-m365: one attached session, one COM thread, and
the tools' Outlook work, every query inside the task's email scope (m365/scope.py).

- **Attach once** (§5 improve 4). `connect_outlook` takes the running Outlook
  (GetActiveObject); only when none runs does it start one (Dispatch: Outlook is a
  single-instance server, so this never makes a second). `Outlook` caches the app, the
  MAPI namespace and the default store, and drops the cache when Outlook drops the
  connection (an RPC disconnect HRESULT), re-attaching on the next call. A read is then
  retried tasks.m365.read_retries times; a draft never is, since whether it was saved is
  unknown.
- **One COM thread** (`ComWorker`): every call runs on one thread inside
  whispr.winutil.com_initialized, and the caller waits at most tasks.m365.call_timeout_s.
  A call that runs longer is abandoned (the server then refuses every later call).
- **Searches never scan in Python** (§5 improve 1, 2). Mail is `Folder.GetTable("@SQL=")`
  with only the columns a card needs, filtered by date, class, the scope's senders and
  subjects and, for a body search, `textdescription LIKE`; items are opened only for a
  body snippet, get_message, attachments and drafts. Every row is checked against the
  scope again in Python, so a filter Outlook reads loosely can never widen it. Calendar is
  IncludeRecurrences, then Sort, then Restrict, with dates in the user's short-date
  format (§5 improve 5).
- **Partial results say so**: search_complete, truncated_reason (limit: more beyond this
  page; scan_cap: stopped after max_scan rows), scanned_count, and the count of rows or
  items that failed to read, with the first error_samples messages (§5 improve 6, 7).
- **Text only, capped, untrusted** (§5 improve 3): bodies are the plain-text Body cut to
  body_chars with `truncated`; no internet headers; tasks.m365.untrusted_label beside it.
- **Drafts are saved, never sent or shown** (§5 improve 9-14): no Send or Display
  anywhere; attachments only from the task folder; every recipient of the built draft,
  inherited ones included, must be one the brief or scope names, or nothing is saved;
  the sanitized text goes after <body>; the task id is a UserProperty, and an existing
  draft with it is reported instead of a new one (F13).
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from m365.sanitize import sanitize_html, text_to_html
from m365.scope import ADDRESS, DOMAIN, EmailScope, ScopeError, folder_path, parse_day
from whispr.winutil import com_initialized

log = logging.getLogger("m365.outlook")

PROG_ID = "Outlook.Application"
# Outlook object-model constants.
OL_MAIL_ITEM = 0
OL_FOLDER_CALENDAR, OL_FOLDER_DRAFTS = 9, 16
OL_TO, OL_CC = 1, 2
OL_TEXT = 1                            # OlUserPropertyType olText
OL_USER_ITEMS = 0                      # OlTableContents olUserItems
MAIL_CLASS, APPOINTMENT_CLASS = "IPM.Note", "IPM.Appointment"
FOLDER_PATH_SEP = "\\"                 # Folder.FolderPath's separator
# MAPI properties, by DASL name.
PR_MESSAGE_CLASS = "http://schemas.microsoft.com/mapi/proptag/0x001A001F"
PR_SENDER_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x5D01001F"
PR_HASATTACH = "http://schemas.microsoft.com/mapi/proptag/0x0E1B000B"
PR_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x39FE001F"
DATE_RECEIVED = "urn:schemas:httpmail:datereceived"
SUBJECT = "urn:schemas:httpmail:subject"
TEXT_DESCRIPTION = "urn:schemas:httpmail:textdescription"
PS_PUBLIC_STRINGS = "http://schemas.microsoft.com/mapi/string/{00020329-0000-0000-C000-000000000046}/"
# A mail search's table columns, in this order.
MAIL_COLUMNS = ("EntryID", "Subject", "ReceivedTime", "SenderName", PR_SENDER_SMTP, PR_HASATTACH)
# HRESULTs of a dropped connection to Outlook (it closed, crashed or restarted).
DISCONNECT_HRESULTS = frozenset({
    -2147417848,   # 0x80010108 RPC_E_DISCONNECTED
    -2147023174,   # 0x800706BA RPC_S_SERVER_UNAVAILABLE
    -2147220995,   # 0x800401FD CO_E_OBJNOTCONNECTED
    -2147418094,   # 0x80010012 RPC_E_SERVER_DIED
    -2147418093,   # 0x80010013 RPC_E_SERVER_DIED_DNE
})
# File names Windows reserves, with or without an extension.
RESERVED_NAMES = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                            *(f"lpt{i}" for i in range(1, 10))})
UNSAFE_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
BODY_TAG = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
PICTURE_TOKEN = re.compile(r"'[^']*'|([A-Za-z])\1*|.", re.DOTALL)
PICTURE_LENGTHS = {"d": (1, 2), "M": (1, 2), "y": (2, 4)}


class OutlookError(ValueError):
    """Outlook could not answer: not attached, a folder or item missing, a refused draft."""


class CallTimeout(Exception):
    """A call ran past tasks.m365.call_timeout_s."""


# ---- the COM thread ------------------------------------------------------------------


class ComWorker:
    """One daemon thread that runs every COM call inside `context` (com_initialized), so
    every COM object stays on the thread that made it. call() waits at most timeout_s."""

    def __init__(self, context: Callable = com_initialized):
        self._context = context
        self._jobs: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    def _loop(self) -> None:
        with self._context():
            while True:
                job = self._jobs.get()
                if job is None:
                    return
                fn, box, done = job
                try:
                    box["value"] = fn()
                except BaseException as exc:          # handed back to the caller
                    box["error"] = exc
                done.set()

    def call(self, fn: Callable[[], Any], timeout_s: float) -> Any:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="m365-com", daemon=True)
            self._thread.start()
        box: dict = {}
        done = threading.Event()
        self._jobs.put((fn, box, done))
        if not done.wait(timeout_s):
            raise CallTimeout(f"no answer from Outlook within {timeout_s} s")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def close(self) -> None:
        if self._thread is not None:
            self._jobs.put(None)


# ---- the attached session ------------------------------------------------------------


def connect_outlook() -> Any:
    """The running Outlook, or a newly started one when none runs."""
    import win32com.client
    try:
        return win32com.client.GetActiveObject(PROG_ID)
    except Exception:
        log.info("Outlook is not running; starting it")
        return win32com.client.Dispatch(PROG_ID)


def hresult(exc: BaseException) -> Optional[int]:
    code = getattr(exc, "hresult", None)
    if code is None and exc.args and isinstance(exc.args[0], int):
        code = exc.args[0]
    return code


@dataclass(frozen=True)
class Session:
    app: Any
    ns: Any
    store: Any


class Outlook:
    """The cached attachment to Outlook; run() re-attaches after a disconnect."""

    def __init__(self, cfg: dict, connect: Callable[[], Any] = connect_outlook):
        self._connect = connect
        self.retries = cfg["tasks"]["m365"]["read_retries"]
        self._session: Optional[Session] = None
        self.attaches = 0

    def session(self) -> Session:
        if self._session is None:
            try:
                app = self._connect()
                ns = app.GetNamespace("MAPI")
                self._session = Session(app, ns, ns.DefaultStore)
            except Exception as exc:
                raise OutlookError(f"cannot attach to Outlook: {type(exc).__name__}: {exc}") from exc
            self.attaches += 1
        return self._session

    def run(self, fn: Callable[[Session], Any], *, read: bool) -> Any:
        attempts = 1 + (self.retries if read else 0)
        for attempt in range(attempts):
            try:
                return fn(self.session())
            except Exception as exc:
                if hresult(exc) not in DISCONNECT_HRESULTS:
                    raise
                self._session = None
                log.warning("Outlook dropped the connection (%s); attempt %d of %d", exc, attempt + 1, attempts)
                if attempt + 1 == attempts:
                    if read:
                        raise OutlookError(f"Outlook dropped the connection {attempts} time(s); try again later") \
                            from exc
                    raise OutlookError("Outlook dropped the connection during a draft call: whether the draft was "
                                       "saved is unknown. Check Drafts; do not retry") from exc


# ---- values --------------------------------------------------------------------------


def naive(value: Any) -> Optional[datetime]:
    """A COM time as a naive local datetime (pywin32 tags local times with a UTC zone)."""
    if value is None:
        return None
    return datetime(value.year, value.month, value.day, value.hour, value.minute, value.second)


def windows_short_date() -> str:
    """The user's short-date picture (e.g. M/d/yyyy)."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\International") as key:
        return winreg.QueryValueEx(key, "sShortDate")[0]


def format_day(day: date, picture: str) -> str:
    """`day` written in a Windows date picture: numeric d, dd, M, MM, yy, yyyy, and
    literal text (quoted, or any non-letter). Any other letter run is an OutlookError,
    so a picture with month names is never guessed at (set tasks.m365.date_picture)."""
    out = []
    for m in PICTURE_TOKEN.finditer(picture):
        tok = m.group(0)
        if tok.startswith("'"):
            out.append(tok[1:-1] if len(tok) > 1 and tok.endswith("'") else tok[1:])
            continue
        letter = tok[0]
        if not letter.isalpha():
            out.append(tok)
            continue
        if letter not in PICTURE_LENGTHS or len(tok) not in PICTURE_LENGTHS[letter]:
            raise OutlookError(f"date picture {picture!r}: {tok!r} is not supported (numeric d, dd, M, MM, yy, "
                               "yyyy only); set tasks.m365.date_picture")
        number = {"d": day.day, "M": day.month, "y": day.year % 100 if len(tok) == 2 else day.year}[letter]
        out.append(f"{number:0{len(tok)}d}")
    return "".join(out)


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _like(prop: str, text: str) -> str:
    return f'"{prop}" LIKE {_quote("%" + text + "%")}'


def _entry_smtp(entry: Any) -> str:
    """An AddressEntry's SMTP address ('' when it has none)."""
    if entry is None:
        return ""
    if entry.Type == "EX":
        user = entry.GetExchangeUser()
        if user is not None and user.PrimarySmtpAddress:
            return user.PrimarySmtpAddress.casefold()
    address = entry.Address or ""
    return address.casefold() if ADDRESS.fullmatch(address) else ""


def recipient_smtp(recipient: Any) -> str:
    """A Recipient's SMTP address ('' when it cannot be resolved)."""
    address = recipient.Address or ""
    if ADDRESS.fullmatch(address):
        return address.casefold()
    smtp = recipient.PropertyAccessor.GetProperty(PR_SMTP_ADDRESS)
    if smtp:
        return smtp.casefold()
    return _entry_smtp(recipient.AddressEntry)


def sender_smtp(item: Any) -> str:
    smtp = item.PropertyAccessor.GetProperty(PR_SENDER_SMTP)
    if smtp:
        return smtp.casefold()
    address = item.SenderEmailAddress or ""
    return address.casefold() if ADDRESS.fullmatch(address) else _entry_smtp(item.Sender)


def safe_name(name: str, cfg: dict) -> str:
    """An attachment's file name made safe to save: the last path part only, no
    characters Windows forbids (':' included), no trailing dots or spaces, a reserved
    device name (CON, NUL, COM1...) prefixed with '_', cut to name_chars."""
    acfg = cfg["tasks"]["m365"]["attachments"]
    base = re.split(r"[\\/]", name or "")[-1]
    base = UNSAFE_NAME_CHARS.sub("", base).strip().rstrip(". ")
    stem, ext = os.path.splitext(base)
    if not stem:
        stem, ext = acfg["fallback_name"], ext
    if stem.split(".")[0].strip().casefold() in RESERVED_NAMES:
        stem = "_" + stem
    limit = acfg["name_chars"]
    ext = ext[:max(0, limit // 2)]
    return stem[:limit - len(ext)].rstrip(". ") + ext


def insert_after_body(page: str, fragment: str) -> str:
    """`fragment` placed right after the page's <body> tag (above a reply's quoted text)."""
    m = BODY_TAG.search(page or "")
    if m is None:
        return f"<html><body>{fragment}{page or ''}</body></html>"
    return page[:m.end()] + fragment + page[m.end():]


class _Errors:
    """Per-search count of rows or items that failed to read, and the first few."""

    def __init__(self, keep: int):
        self.keep, self.count, self.samples = keep, 0, []

    def add(self, exc: BaseException) -> None:
        self.count += 1
        if len(self.samples) < self.keep:
            self.samples.append(f"{type(exc).__name__}: {exc}")

    def report(self) -> dict:
        return {"error_count": self.count, "errors": self.samples}


# ---- the tools' Outlook work ---------------------------------------------------------


class Mailbox:
    """The tools, over one Outlook, inside one task's scope (`scope`, `recipients`,
    `task_dir`). `today` is fixed when the server starts, like the scope's window."""

    def __init__(self, cfg: dict, outlook: Outlook, *, task_id: str, task_dir: Path, scope: EmailScope,
                 recipients: frozenset, today: date):
        self.cfg, self.m = cfg, cfg["tasks"]["m365"]
        self.outlook, self.task_id, self.task_dir = outlook, task_id, task_dir
        self.scope, self.recipients, self.today = scope, recipients, today
        self._picture: Optional[str] = None

    # ---- shared checks ---------------------------------------------------------------

    def _day(self, d: date) -> str:
        if self._picture is None:
            self._picture = self.m["date_picture"] or windows_short_date()
        return format_day(d, self._picture)

    def _need_scope(self) -> EmailScope:
        if self.scope.none:
            raise ScopeError("the task's email scope is none: no mail or calendar may be read")
        return self.scope

    def _arg_day(self, a: dict, key: str) -> Optional[date]:
        return parse_day(a[key], self.cfg, what=key) if key in a else None

    def _mail_window(self, a: dict) -> tuple[date, date]:
        scope = self._need_scope()
        since = self._arg_day(a, "since") or scope.since
        until = self._arg_day(a, "until") or scope.closes(self.today)
        if since < scope.since or until > scope.closes(self.today):
            raise ScopeError(f"the window {since}..{until} is outside the task's email scope "
                             f"{scope.since}..{scope.closes(self.today)}")
        if since > until:
            raise ScopeError(f"since {since} is after until {until}")
        return since, until

    def _sender_arg(self, a: dict) -> Optional[tuple[str, str]]:
        """The sender argument, which must lie inside the scope's senders."""
        if "sender" not in a:
            return None
        text = a["sender"].strip()
        if ADDRESS.fullmatch(text):
            parsed = ("address", text.casefold())
            ok = self.scope.sender_ok(parsed[1])
        elif DOMAIN.fullmatch(text):
            parsed = ("domain", text[1:].casefold())
            ok = not self.scope.has_senders or parsed[1] in self.scope.domains
        else:
            raise ScopeError(f"sender {text!r} is not an address or @domain")
        if not ok:
            raise ScopeError(f"sender {text!r} is outside the task's email scope")
        return parsed

    @staticmethod
    def _sender_matches(arg: Optional[tuple[str, str]], smtp: str) -> bool:
        if arg is None:
            return True
        kind, value = arg
        return smtp == value if kind == "address" else smtp.rpartition("@")[2] == value

    def _allowed_folders(self) -> list[tuple[str, ...]]:
        return list(self.scope.folders) or [folder_path(f, self.cfg) for f in self.m["default_folders"]]

    def _search_folders(self, a: dict) -> list[tuple[str, ...]]:
        allowed = self._allowed_folders()
        if "folder" not in a:
            return allowed
        levels = folder_path(a["folder"], self.cfg)
        folded = tuple(p.casefold() for p in levels)
        if not any(folded[:len(f)] == tuple(p.casefold() for p in f) for f in allowed):
            raise ScopeError(f"folder {a['folder']!r} is outside the task's email scope")
        return [levels]

    def _folder(self, s: Session, levels: tuple[str, ...]) -> Any:
        folder = s.store.GetRootFolder()
        for name in levels:
            match = None
            for child in folder.Folders:
                if child.Name.casefold() == name.casefold():
                    match = child
                    break
            if match is None:
                raise OutlookError(f"no folder {self.m['grammar']['folder_separator'].join(levels)!r} in the mailbox")
            folder = match
        return folder

    def _open(self, s: Session, entry_id: str) -> Any:
        try:
            return s.ns.GetItemFromID(entry_id, s.store.StoreID)
        except Exception as exc:
            if hresult(exc) in DISCONNECT_HRESULTS:
                raise
            raise OutlookError(f"no item with entry_id {entry_id!r} in the default store") from exc

    def _in_window(self, day: date, until: date) -> bool:
        return self.scope.since <= day <= until

    def _mail_item(self, s: Session, entry_id: str) -> Any:
        """A mail item, refused unless it lies inside the scope: folder, sender, date, subject."""
        scope = self._need_scope()
        item = self._open(s, entry_id)
        if not (item.MessageClass or "").startswith(MAIL_CLASS):
            raise ScopeError("that item is not a mail message")
        where = (item.Parent.FolderPath or "").casefold()
        roots = [self._folder(s, f).FolderPath.casefold() for f in self._allowed_folders()]
        if not any(where == r or where.startswith(r + FOLDER_PATH_SEP) for r in roots):
            raise ScopeError("that message is outside the task's email scope (its folder)")
        if not scope.sender_ok(sender_smtp(item)):
            raise ScopeError("that message is outside the task's email scope (its sender)")
        if not self._in_window(naive(item.ReceivedTime).date(), scope.closes(self.today)):
            raise ScopeError("that message is outside the task's email scope (its date)")
        if not scope.subject_ok(item.Subject):
            raise ScopeError("that message is outside the task's email scope (its subject)")
        return item

    def _partial(self, *, complete: bool, reason: Optional[str], scanned: int, errors: _Errors) -> dict:
        return {"search_complete": complete, "truncated_reason": reason, "scanned_count": scanned,
                **errors.report()}

    def _text(self, text: str, cap: int) -> dict:
        text = text or ""
        return {"body": text[:cap], "body_chars": len(text), "truncated": len(text) > cap}

    # ---- mail ------------------------------------------------------------------------

    def _mail_filter(self, since: date, until: date, sender: Optional[tuple[str, str]], subject: Optional[str],
                     text: Optional[str]) -> str:
        # Dates widened a day each way: DASL compares in UTC, and the Python check below is exact.
        terms = [f'"{PR_MESSAGE_CLASS}" LIKE {_quote(MAIL_CLASS + "%")}',
                 f'"{DATE_RECEIVED}" >= {_quote(self._day(since - timedelta(days=1)))}',
                 f'"{DATE_RECEIVED}" < {_quote(self._day(until + timedelta(days=2)))}']
        senders = [sender] if sender else ([("address", a) for a in sorted(self.scope.addresses)]
                                           + [("domain", d) for d in sorted(self.scope.domains)])
        if senders:
            terms.append("(" + " OR ".join(f'"{PR_SENDER_SMTP}" = {_quote(v)}' if kind == "address"
                                           else f'"{PR_SENDER_SMTP}" LIKE {_quote("%@" + v)}'
                                           for kind, v in senders) + ")")
        if self.scope.subjects:
            terms.append("(" + " OR ".join(_like(SUBJECT, w) for w in self.scope.subjects) + ")")
        if subject:
            terms.append(_like(SUBJECT, subject))
        if text:
            terms.append(_like(TEXT_DESCRIPTION, text))
        return " AND ".join(terms)

    def search(self, a: dict, *, body: bool) -> dict:
        """Mail cards newest first, in scope; with `body`, only messages whose text holds
        a['query'], each with a snippet around it."""
        since, until = self._mail_window(a)
        sender = self._sender_arg(a)
        folders = self._search_folders(a)
        mail = self.m["mail"]
        cap = mail["body_max_results"] if body else mail["max_results"]
        limit, offset = min(a.get("limit", cap), cap), a.get("offset", 0)
        text = a["query"].strip() if body else None
        subject = a.get("subject", "").strip() or None
        dasl = self._mail_filter(since, until, sender, subject, text)
        sep = self.m["grammar"]["folder_separator"]

        def work(s: Session) -> dict:
            errors, hits, scanned, capped, more = _Errors(self.m["error_samples"]), [], 0, False, False
            for levels in folders:
                table = self._folder(s, levels).GetTable("@SQL=" + dasl, OL_USER_ITEMS)
                table.Columns.RemoveAll()
                for column in MAIL_COLUMNS:
                    table.Columns.Add(column)
                table.Sort("[ReceivedTime]", True)
                found = 0
                while not table.EndOfTable:
                    if found > offset + limit:
                        more = True
                        break
                    if scanned >= mail["max_scan"]:
                        capped = True
                        break
                    scanned += 1
                    try:
                        entry_id, subj, received, name, smtp, has_att = table.GetNextRow().GetValues()
                        smtp, received = (smtp or "").casefold(), naive(received)
                    except Exception as exc:
                        errors.add(exc)
                        continue
                    if not (self.scope.sender_ok(smtp) and self._sender_matches(sender, smtp)
                            and since <= received.date() <= until and self.scope.subject_ok(subj)
                            and (subject is None or subject.casefold() in (subj or "").casefold())):
                        continue
                    found += 1
                    hits.append({"entry_id": entry_id, "folder": sep.join(levels), "subject": subj,
                                 "sender_name": name, "sender": smtp, "received": received.isoformat(),
                                 "has_attachments": bool(has_att)})
                if capped:
                    break
            hits.sort(key=lambda h: h["received"], reverse=True)
            more = more or len(hits) > offset + limit
            page = hits[offset:offset + limit]
            if body:
                for card in page:
                    try:
                        found_text = self._open(s, card["entry_id"]).Body or ""
                    except Exception as exc:
                        if hresult(exc) in DISCONNECT_HRESULTS:
                            raise
                        errors.add(exc)
                        card["snippet"] = None
                        continue
                    card.update(self._snippet(found_text, text))
            reason = "scan_cap" if capped else ("limit" if more else None)
            return {"results": page, "offset": offset, "has_more": more,
                    "window": {"since": since.isoformat(), "until": until.isoformat()},
                    **self._partial(complete=not (capped or more), reason=reason, scanned=scanned, errors=errors),
                    "untrusted": self.m["untrusted_label"]}

        return self.outlook.run(work, read=True)

    def _snippet(self, body: str, text: str) -> dict:
        flat = " ".join(body.split())
        at = flat.casefold().find(text.casefold())
        width = self.m["mail"]["snippet_chars"]
        start = max(0, at - width // 2) if at >= 0 else 0
        return {"snippet": flat[start:start + width], "match_in_body": at >= 0}

    def get_message(self, a: dict) -> dict:
        def work(s: Session) -> dict:
            item = self._mail_item(s, a["entry_id"])
            return {"entry_id": a["entry_id"], "subject": item.Subject, "sender_name": item.SenderName,
                    "sender": sender_smtp(item), "to": item.To, "cc": item.CC,
                    "received": naive(item.ReceivedTime).isoformat(), "attachment_count": item.Attachments.Count,
                    **self._text(item.Body, self.m["mail"]["body_chars"]), "untrusted": self.m["untrusted_label"]}
        return self.outlook.run(work, read=True)

    def list_attachments(self, a: dict) -> dict:
        def work(s: Session) -> dict:
            atts = self._mail_item(s, a["entry_id"]).Attachments
            return {"entry_id": a["entry_id"], "untrusted": self.m["untrusted_label"],
                    "attachments": [{"index": i, "name": att.FileName, "size": att.Size, "type": att.Type}
                                    for i, att in ((i, atts.Item(i)) for i in range(1, atts.Count + 1))]}
        return self.outlook.run(work, read=True)

    def attachments_dir(self) -> Path:
        return self.task_dir / self.m["attachments"]["folder"]

    def save_attachment(self, a: dict) -> dict:
        """One attachment saved into <task folder>/<attachments.folder>/ under its cleaned
        name; never over a file: the same content already there is reported, other content
        gets the name with its sha256 prefix added."""
        max_bytes = self.m["attachments"]["max_bytes"]
        dest = self.attachments_dir()

        def work(s: Session) -> dict:
            atts = self._mail_item(s, a["entry_id"]).Attachments
            if not 1 <= a["index"] <= atts.Count:
                raise OutlookError(f"attachment index {a['index']} is not 1..{atts.Count}")
            att = atts.Item(a["index"])
            if att.Size > max_bytes:
                raise OutlookError(f"attachment is {att.Size} bytes, over tasks.m365.attachments.max_bytes {max_bytes}")
            name = safe_name(att.FileName, self.cfg)
            dest.mkdir(parents=True, exist_ok=True)
            temp = dest / f"~{uuid.uuid4().hex}.part"
            att.SaveAsFile(str(temp))
            try:
                data = temp.read_bytes()
                if len(data) > max_bytes:
                    raise OutlookError(f"saved file is {len(data)} bytes, over {max_bytes}")
                digest = hashlib.sha256(data).hexdigest()
                stem, ext = os.path.splitext(name)
                for candidate in (name, f"{stem}-{digest[:12]}{ext}"):
                    target = dest / candidate
                    if target.resolve().parent != dest.resolve():
                        raise OutlookError(f"{candidate!r} would land outside the attachments folder")
                    if target.exists():
                        if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                            return {"path": str(target), "name": candidate, "sha256": digest, "bytes": len(data),
                                    "already_saved": True}
                        continue
                    os.rename(temp, target)                  # fails rather than replace a file
                    return {"path": str(target), "name": candidate, "sha256": digest, "bytes": len(data),
                            "already_saved": False}
                raise OutlookError(f"{name!r} and its hashed name both hold other content; nothing saved")
            finally:
                if temp.exists():
                    temp.unlink()
        return self.outlook.run(work, read=True)

    # ---- calendar --------------------------------------------------------------------

    def _calendar_window(self, a: dict) -> tuple[date, date]:
        scope, cal = self._need_scope(), self.m["calendar"]
        since = self._arg_day(a, "since") or max(self.today, scope.since)
        until = self._arg_day(a, "until") or since + timedelta(days=cal["default_days"] - 1)
        latest = scope.until if scope.until is not None else self.today + timedelta(days=cal["future_days"])
        if since > until:
            raise ScopeError(f"since {since} is after until {until}")
        if since < scope.since or until > latest:
            raise ScopeError(f"the window {since}..{until} is outside the task's scope {scope.since}..{latest}")
        if (until - since).days + 1 > cal["max_window_days"]:
            raise ScopeError(f"a calendar window is at most {cal['max_window_days']} days "
                             "(tasks.m365.calendar.max_window_days)")
        return since, until

    def _people_ok(self, item: Any) -> bool:
        """With scope senders, an appointment is in scope when its organizer or an attendee is one."""
        if not self.scope.has_senders:
            return True
        people = [_entry_smtp(item.GetOrganizer())] + [recipient_smtp(r) for r in item.Recipients]
        return any(self.scope.sender_ok(p) for p in people if p)

    def calendar_search(self, a: dict) -> dict:
        since, until = self._calendar_window(a)
        cal = self.m["calendar"]
        limit, offset = min(a.get("limit", cal["max_results"]), cal["max_results"]), a.get("offset", 0)
        query = a.get("query", "").strip().casefold()
        jet = f"[Start] < {_quote(self._day(until + timedelta(days=1)))} AND [End] > {_quote(self._day(since))}"
        lo, hi = datetime.combine(since, datetime.min.time()), datetime.combine(until + timedelta(days=1),
                                                                                datetime.min.time())

        def work(s: Session) -> dict:
            items = s.ns.GetDefaultFolder(OL_FOLDER_CALENDAR).Items
            items.IncludeRecurrences = True
            items.Sort("[Start]")
            found = items.Restrict(jet)
            errors, hits, scanned, capped, more = _Errors(self.m["error_samples"]), [], 0, False, False
            item = found.GetFirst()
            while item is not None:
                if len(hits) > offset + limit:
                    more = True
                    break
                if scanned >= cal["max_scan"]:
                    capped = True
                    break
                scanned += 1
                try:
                    start, end, subj = naive(item.Start), naive(item.End), item.Subject or ""
                    if (start < hi and end > lo and self.scope.subject_ok(subj)
                            and (not query or query in subj.casefold()) and self._people_ok(item)):
                        hits.append({"entry_id": item.EntryID, "subject": subj, "start": start.isoformat(),
                                     "end": end.isoformat(), "all_day": bool(item.AllDayEvent),
                                     "location": item.Location, "organizer": item.Organizer,
                                     "recurring": bool(item.IsRecurring)})
                except Exception as exc:
                    if hresult(exc) in DISCONNECT_HRESULTS:
                        raise
                    errors.add(exc)
                item = found.GetNext()
            more = more or len(hits) > offset + limit
            reason = "scan_cap" if capped else ("limit" if more else None)
            return {"results": hits[offset:offset + limit], "offset": offset, "has_more": more,
                    "window": {"since": since.isoformat(), "until": until.isoformat()},
                    **self._partial(complete=not (capped or more), reason=reason, scanned=scanned, errors=errors),
                    "untrusted": self.m["untrusted_label"]}

        return self.outlook.run(work, read=True)

    def calendar_get(self, a: dict) -> dict:
        scope = self._need_scope()
        latest = scope.until if scope.until is not None else \
            self.today + timedelta(days=self.m["calendar"]["future_days"])

        def work(s: Session) -> dict:
            item = self._open(s, a["entry_id"])
            if not (item.MessageClass or "").startswith(APPOINTMENT_CLASS):
                raise ScopeError("that item is not a calendar appointment")
            calendar = s.ns.GetDefaultFolder(OL_FOLDER_CALENDAR).FolderPath.casefold()
            if (item.Parent.FolderPath or "").casefold() != calendar:
                raise ScopeError("that appointment is not in the default calendar")
            start, end = naive(item.Start), naive(item.End)
            if item.IsRecurring:
                end = naive(item.GetRecurrencePattern().PatternEndDate)
            if not (start.date() <= latest and end.date() >= scope.since):
                raise ScopeError("that appointment is outside the task's scope (its dates)")
            if not scope.subject_ok(item.Subject) or not self._people_ok(item):
                raise ScopeError("that appointment is outside the task's scope (its subject or people)")
            return {"entry_id": a["entry_id"], "subject": item.Subject, "start": naive(item.Start).isoformat(),
                    "end": naive(item.End).isoformat(), "recurring": bool(item.IsRecurring),
                    "location": item.Location, "organizer": item.Organizer,
                    "required": item.RequiredAttendees, "optional": item.OptionalAttendees,
                    **self._text(item.Body, self.m["calendar"]["body_chars"]), "untrusted": self.m["untrusted_label"]}
        return self.outlook.run(work, read=True)

    # ---- drafts ----------------------------------------------------------------------

    def _draft_body(self, a: dict) -> str:
        body = a["body"]
        if len(body) > self.m["drafts"]["body_chars"]:
            raise OutlookError(f"body is {len(body)} characters, over tasks.m365.drafts.body_chars "
                               f"{self.m['drafts']['body_chars']}")
        if a.get("body_format", "text") == "html":
            return sanitize_html(body, self.m["sanitize"])
        return text_to_html(body)

    def _draft_files(self, names: list[str]) -> list[Path]:
        """Each file to attach: inside the task folder, existing, at most max_bytes."""
        if len(names) > self.m["drafts"]["max_attachments"]:
            raise OutlookError(f"at most {self.m['drafts']['max_attachments']} attachments")
        root, files = self.task_dir.resolve(), []
        for name in names:
            path = (self.task_dir / name).resolve()
            if path != root and root not in path.parents:
                raise ScopeError(f"{name!r} is outside the task folder; attach only files under {self.task_dir}")
            if not path.is_file():
                raise OutlookError(f"{name!r} is not a file in the task folder")
            if path.stat().st_size > self.m["attachments"]["max_bytes"]:
                raise OutlookError(f"{name!r} is over tasks.m365.attachments.max_bytes")
            files.append(path)
        return files

    def _check_recipients(self, addresses: list[str]) -> None:
        outside = [x for x in addresses if not x or x.casefold() not in self.recipients]
        if outside:
            raise ScopeError(f"recipients the brief and email scope do not name: "
                             f"{[x or '<unresolved>' for x in outside]}; nothing was saved")

    def existing_drafts(self, s: Session) -> list[dict]:
        """Drafts already marked with this task id (F13)."""
        marker = f'"{PS_PUBLIC_STRINGS}{self.m["drafts"]["marker_property"]}" = {_quote(self.task_id)}'
        table = s.ns.GetDefaultFolder(OL_FOLDER_DRAFTS).GetTable("@SQL=" + marker, OL_USER_ITEMS)
        table.Columns.RemoveAll()
        for column in ("EntryID", "Subject"):
            table.Columns.Add(column)
        found = []
        while not table.EndOfTable:
            entry_id, subject = table.GetNextRow().GetValues()
            found.append({"entry_id": entry_id, "subject": subject})
        return found

    def draft(self, a: dict, *, kind: str) -> dict:
        """An unsaved draft built (new, reply or forward), checked, then saved, never sent:
        refused before Save() when any recipient, inherited ones included, is not named
        by the brief or scope; reported instead when one for this task exists."""
        fragment = self._draft_body(a)
        files = self._draft_files(a.get("attachments", []))
        explicit = [(OL_TO, x) for x in a.get("to", [])] + [(OL_CC, x) for x in a.get("cc", [])]
        self._check_recipients([x.strip() for _, x in explicit])

        def work(s: Session) -> dict:
            existing = self.existing_drafts(s)
            if existing:
                return {"created": False, "existing": existing,
                        "message": f"a draft for task {self.task_id} is already in Drafts; review it there"}
            original = None
            if kind == "new":
                item = s.app.CreateItem(OL_MAIL_ITEM)
                item.Subject = a["subject"]
            else:
                original = self._mail_item(s, a["entry_id"])
                item = (original.Forward() if kind == "forward"
                        else original.ReplyAll() if a.get("reply_all") else original.Reply())
            for kind_code, address in explicit:
                item.Recipients.Add(address.strip()).Type = kind_code
            item.Recipients.ResolveAll()
            got = []
            for r in item.Recipients:
                try:
                    got.append(recipient_smtp(r))
                except Exception as exc:
                    if hresult(exc) in DISCONNECT_HRESULTS:
                        raise
                    got.append("")
            if not got:
                raise OutlookError("the draft has no recipients; nothing was saved")
            self._check_recipients(got)
            carried = None
            if kind == "forward":
                carried = {"original": original.Attachments.Count, "carried": item.Attachments.Count}
            item.HTMLBody = insert_after_body(item.HTMLBody, fragment)
            for path in files:
                item.Attachments.Add(str(path))
            item.UserProperties.Add(self.m["drafts"]["marker_property"], OL_TEXT, False).Value = self.task_id
            item.Save()
            out = {"created": True, "entry_id": item.EntryID, "subject": item.Subject, "recipients": got,
                   "attachments": [p.name for p in files]}
            if carried is not None:
                out["forwarded_attachments"] = {**carried, "all_carried": carried["carried"] >= carried["original"]}
            return out

        return self.outlook.run(work, read=False)
