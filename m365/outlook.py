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
  subjects and, for a body search, `textdescription LIKE`, over each scope folder and its
  subfolders (at most mail.max_folders); items are opened only for a body snippet,
  get_message, attachments and drafts. Every row is checked against the scope again in
  Python, so a filter Outlook reads loosely can never widen it. Calendar is
  IncludeRecurrences, then Sort, then Restrict, with dates in the user's short-date
  format (§5 improve 5).
- **Partial results say so** (`_page`): search_complete, truncated_reason (scan_cap,
  folder_cap, folder_unavailable, empty_window, or limit: more beyond this page),
  scanned_count, and the count of rows, items or folders that failed to read, with the
  first error_samples messages (§5 improve 6, 7). A folder that does not resolve, or a
  row with no received time, is counted, never a refusal.
- **Text only, capped, untrusted** (§5 improve 3): bodies are the plain-text Body cut to
  body_chars with `truncated`; no internet headers; tasks.m365.untrusted_label beside it.
- **Drafts are saved, never sent or shown** (§5 improve 9-14): no Send or Display
  anywhere; attachments only from the task folder, through no junction or link; every
  recipient of the built draft, inherited ones included, must be an address the brief or
  scope names, or nothing is saved; the sanitized text goes after <body>; the task id and,
  for a reply or forward, the source message's EntryID are UserProperties, and an
  existing draft for the same source (or a new-mail draft with the same subject) is
  reported instead of a second (F13). A draft tool returns ids, counts and reason codes
  only, never text or addresses taken from mail, so nothing in the mailbox reaches the
  draft role (F5).
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import re
import stat
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Optional

from m365.sanitize import sanitize_html, text_to_html
from m365.scope import ADDRESS, EmailScope, ScopeError, folder_path, parse_day, parse_sender
from whispr.winutil import com_initialized

log = logging.getLogger("m365.outlook")

PROG_ID = "Outlook.Application"
# Outlook object-model constants.
OL_MAIL_ITEM = 0
OL_FOLDER_CALENDAR, OL_FOLDER_DRAFTS = 9, 16
# OlDefaultFolders, by name, for tasks.m365.default_folders.
OL_DEFAULT_FOLDERS = {"olFolderDeletedItems": 3, "olFolderOutbox": 4, "olFolderSentMail": 5, "olFolderInbox": 6,
                      "olFolderCalendar": 9, "olFolderDrafts": 16, "olFolderJunk": 23}
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
UNSAFE_NAME_CHARS = re.compile(r'[<>:"/\\|?*]')
# Unicode categories removed from a saved name: format (bidi overrides, zero-width) and control.
UNSAFE_NAME_CATEGORIES = frozenset({"Cf", "Cc"})
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


def _reraise_disconnect(exc: BaseException) -> None:
    """A dropped connection is never counted as one failed row: it goes up to Outlook.run."""
    if hresult(exc) in DISCONNECT_HRESULTS:
        raise exc


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
    characters Windows forbids (':' included), no Unicode format or control characters
    (bidi overrides, zero-width), no trailing dots or spaces, a reserved device name (CON,
    NUL, COM1...) prefixed with '_', the extension cut to ext_chars and the whole to name_chars."""
    acfg = cfg["tasks"]["m365"]["attachments"]
    base = re.split(r"[\\/]", name or "")[-1]
    base = "".join(c for c in base if unicodedata.category(c) not in UNSAFE_NAME_CATEGORIES)
    base = UNSAFE_NAME_CHARS.sub("", base).strip().rstrip(". ")
    stem, ext = os.path.splitext(base)
    if not stem:
        stem = acfg["fallback_name"]
    if stem.split(".")[0].strip().casefold() in RESERVED_NAMES:
        stem = "_" + stem
    limit = acfg["name_chars"]
    ext = ext[:min(acfg["ext_chars"], limit // 2)]
    return stem[:limit - len(ext)].rstrip(". ") + ext


def is_link(path: Path) -> bool:
    """Whether `path` itself is a junction, symlink or any other reparse point."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    return bool(getattr(path, "is_junction", lambda: False)())


def refuse_links(path: Path, base: Path) -> None:
    """A ScopeError when `base` or any component of `path` under it (as written, not
    resolved) is a junction, symlink or reparse point, so no link redirects a read or save."""
    current = base
    for part in ("",) + path.relative_to(base).parts:
        current = current / part if part else current
        if is_link(current):
            raise ScopeError(f"{current.name or current} is a junction or link; refused")


def insert_after_body(page: str, fragment: str) -> str:
    """`fragment` placed right after the page's <body> tag (above a reply's quoted text)."""
    m = BODY_TAG.search(page or "")
    if m is None:
        return f"<html><body>{fragment}{page or ''}</body></html>"
    return page[:m.end()] + fragment + page[m.end():]


def normal_subject(subject: Optional[str]) -> str:
    return " ".join((subject or "").split()).casefold()


class _Errors:
    """Per-search count of rows, items or folders that failed to read, and the first few."""

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
    `task_dir`; the server refreshes scope and recipients from the database before each
    call). `today` is fixed when the server starts, like the scope's default window."""

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
        """The sender argument, parsed as a scope sender clause is, inside the scope's senders."""
        if "sender" not in a:
            return None
        parsed = parse_sender(a["sender"])
        if not self.scope.sender_arg_ok(parsed):
            raise ScopeError(f"sender {a['sender'].strip()!r} is outside the task's email scope")
        return parsed

    @staticmethod
    def _sender_matches(arg: Optional[tuple[str, str]], smtp: str) -> bool:
        if arg is None:
            return True
        kind, value = arg
        return smtp == value if kind == "address" else smtp.rpartition("@")[2] == value

    def _path_folder(self, s: Session, levels: tuple[str, ...]) -> Any:
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

    def _default_folder(self, s: Session, ref: Any) -> Any:
        code = ref if isinstance(ref, int) else OL_DEFAULT_FOLDERS.get(ref)
        if code is None:
            raise OutlookError(f"tasks.m365.default_folders: {ref!r} is not an olFolder name "
                               f"{sorted(OL_DEFAULT_FOLDERS)} or number")
        return s.ns.GetDefaultFolder(code)

    def _roots(self, s: Session, errors: _Errors) -> tuple[list, bool]:
        """The scope's folders, or the default folders, resolved; one that does not
        resolve is counted in `errors` (second value True), never raised."""
        specs = ([lambda lv=lv: self._path_folder(s, lv) for lv in self.scope.folders] if self.scope.folders
                 else [lambda r=r: self._default_folder(s, r) for r in self.m["default_folders"]])
        roots, missing = [], False
        for resolve in specs:
            try:
                roots.append(resolve())
            except Exception as exc:
                _reraise_disconnect(exc)
                errors.add(exc)
                missing = True
        return roots, missing

    @staticmethod
    def _under(folder: Any, roots: list) -> bool:
        where = (folder.FolderPath or "").casefold()
        return any(where == r.FolderPath.casefold() or where.startswith(r.FolderPath.casefold() + FOLDER_PATH_SEP)
                   for r in roots)

    def _expand(self, starts: list, errors: _Errors) -> tuple[list, bool]:
        """`starts` and their subfolders, breadth first, each once, at most mail.max_folders
        (second value True when the cap cut the walk)."""
        cap, seen, out, todo = self.m["mail"]["max_folders"], set(), [], list(starts)
        while todo:
            folder = todo.pop(0)
            key = (folder.FolderPath or "").casefold()
            if key in seen:
                continue
            if len(out) >= cap:
                return out, True
            seen.add(key)
            out.append(folder)
            try:
                todo.extend(folder.Folders)
            except Exception as exc:
                _reraise_disconnect(exc)
                errors.add(exc)
        return out, False

    def _display(self, s: Session, folder: Any) -> str:
        root = s.store.GetRootFolder().FolderPath
        path = folder.FolderPath or ""
        rest = path[len(root):] if path.casefold().startswith(root.casefold()) else path
        return rest.strip(FOLDER_PATH_SEP).replace(FOLDER_PATH_SEP, self.m["grammar"]["folder_separator"])

    def _open(self, s: Session, entry_id: str) -> Any:
        try:
            return s.ns.GetItemFromID(entry_id, s.store.StoreID)
        except Exception as exc:
            _reraise_disconnect(exc)
            raise OutlookError(f"no item with entry_id {entry_id!r} in the default store") from exc

    def _mail_item(self, s: Session, entry_id: str) -> Any:
        """A mail item, refused unless it lies inside the scope: folder (or a subfolder),
        sender, date, subject."""
        scope = self._need_scope()
        item = self._open(s, entry_id)
        if not (item.MessageClass or "").startswith(MAIL_CLASS):
            raise ScopeError("that item is not a mail message")
        roots, _ = self._roots(s, _Errors(0))
        if not self._under(item.Parent, roots):
            raise ScopeError("that message is outside the task's email scope (its folder)")
        if not scope.sender_ok(sender_smtp(item)):
            raise ScopeError("that message is outside the task's email scope (its sender)")
        received = naive(item.ReceivedTime)
        if received is None or not scope.since <= received.date() <= scope.closes(self.today):
            raise ScopeError("that message is outside the task's email scope (its date)")
        if not scope.subject_ok(item.Subject):
            raise ScopeError("that message is outside the task's email scope (its subject)")
        return item

    def _page(self, hits: list, a: dict, limit: int, *, more: bool, scanned: int, errors: _Errors,
              window: tuple[date, date], partial: Optional[str]) -> dict:
        """The tail every search ends with: one page of `hits`, and the partial flags.
        `partial` is why the search stopped short (scan_cap, folder_cap, ...), if it did."""
        offset = a.get("offset", 0)
        more = more or len(hits) > offset + limit
        reason = partial or ("limit" if more else None)
        return {"results": hits[offset:offset + limit], "offset": offset, "has_more": more,
                "window": {"since": window[0].isoformat(), "until": window[1].isoformat()},
                "search_complete": reason is None, "truncated_reason": reason, "scanned_count": scanned,
                **errors.report(), "untrusted": self.m["untrusted_label"]}

    def _text(self, text: str, cap: int) -> dict:
        text = text or ""
        return {"body": text[:cap], "body_chars": len(text), "truncated": len(text) > cap}

    def _limit(self, a: dict, cap: int) -> int:
        return min(a.get("limit", cap), cap)

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
        """Mail cards newest first, in scope, over the scope folders and their subfolders;
        with `body`, only messages whose text holds a['query'], each with a snippet."""
        since, until = self._mail_window(a)
        sender = self._sender_arg(a)
        target = folder_path(a["folder"], self.cfg) if "folder" in a else None
        mail = self.m["mail"]
        limit = self._limit(a, mail["body_max_results"] if body else mail["max_results"])
        want = a.get("offset", 0) + limit
        text = a["query"].strip() if body else None
        subject = a.get("subject", "").strip() or None
        dasl = self._mail_filter(since, until, sender, subject, text)

        def work(s: Session) -> dict:
            errors = _Errors(self.m["error_samples"])
            roots, missing = self._roots(s, errors)
            starts = roots
            if target is not None:
                folder = self._path_folder(s, target)
                if not self._under(folder, roots):
                    raise ScopeError(f"folder {a['folder']!r} is outside the task's email scope")
                starts = [folder]
            folders, folder_cap = self._expand(starts, errors)
            hits, scanned, capped, more = [], 0, False, False
            for folder in folders:
                table = folder.GetTable("@SQL=" + dasl, OL_USER_ITEMS)
                table.Columns.RemoveAll()
                for column in MAIL_COLUMNS:
                    table.Columns.Add(column)
                table.Sort("[ReceivedTime]", True)
                found, where = 0, self._display(s, folder)
                while not table.EndOfTable:
                    if found > want:
                        more = True
                        break
                    if scanned >= mail["max_scan"]:
                        capped = True
                        break
                    scanned += 1
                    try:
                        entry_id, subj, received, name, smtp, has_att = table.GetNextRow().GetValues()
                        smtp, received = (smtp or "").casefold(), naive(received)
                        if received is None:
                            raise OutlookError("a row has no ReceivedTime")
                    except Exception as exc:
                        _reraise_disconnect(exc)
                        errors.add(exc)
                        continue
                    if not (self.scope.sender_ok(smtp) and self._sender_matches(sender, smtp)
                            and since <= received.date() <= until and self.scope.subject_ok(subj)
                            and (subject is None or subject.casefold() in (subj or "").casefold())):
                        continue
                    found += 1
                    hits.append({"entry_id": entry_id, "folder": where, "subject": subj, "sender_name": name,
                                 "sender": smtp, "received": received.isoformat(), "has_attachments": bool(has_att)})
                if capped:
                    break
            hits.sort(key=lambda h: h["received"], reverse=True)
            partial = ("scan_cap" if capped else "folder_cap" if folder_cap
                       else "folder_unavailable" if missing else None)
            out = self._page(hits, a, limit, more=more, scanned=scanned, errors=errors, window=(since, until),
                             partial=partial)
            if body:
                for card in out["results"]:
                    try:
                        card.update(self._snippet(self._open(s, card["entry_id"]).Body or "", text))
                    except Exception as exc:
                        _reraise_disconnect(exc)
                        errors.add(exc)
                        card["snippet"] = None
                out.update(errors.report())
            return out

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
        name, through no junction or link; never over a file: the same content already
        there is reported, other content gets the name with its sha256 prefix added."""
        acfg = self.m["attachments"]
        max_bytes, dest, base = acfg["max_bytes"], self.attachments_dir(), self.task_dir.parent

        def work(s: Session) -> dict:
            atts = self._mail_item(s, a["entry_id"]).Attachments
            if not 1 <= a["index"] <= atts.Count:
                raise OutlookError(f"attachment index {a['index']} is not 1..{atts.Count}")
            att = atts.Item(a["index"])
            if att.Size > max_bytes:
                raise OutlookError(f"attachment is {att.Size} bytes, over tasks.m365.attachments.max_bytes {max_bytes}")
            name = safe_name(att.FileName, self.cfg)
            refuse_links(dest, base)
            dest.mkdir(parents=True, exist_ok=True)
            refuse_links(dest, base)
            temp = dest / f"~{uuid.uuid4().hex}{acfg['temp_suffix']}"
            att.SaveAsFile(str(temp))
            try:
                refuse_links(temp, base)
                data = temp.read_bytes()
                if len(data) > max_bytes:
                    raise OutlookError(f"saved file is {len(data)} bytes, over {max_bytes}")
                digest = hashlib.sha256(data).hexdigest()
                stem, ext = os.path.splitext(name)
                for candidate in (name, f"{stem}-{digest[:acfg['digest_chars']]}{ext}"):
                    target = dest / candidate
                    refuse_links(target, base)
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

    def _calendar_latest(self) -> date:
        """The last day a calendar read may reach: the scope's until, or future_days past today."""
        scope = self._need_scope()
        return scope.until if scope.until is not None else self.today + timedelta(days=self.m["calendar"]["future_days"])

    def _calendar_window(self, a: dict) -> Optional[tuple[date, date]]:
        """The window asked for, refused outside the scope; defaults are clamped into it.
        None when the scope leaves no day to read."""
        scope, cal = self._need_scope(), self.m["calendar"]
        latest, span = self._calendar_latest(), timedelta(days=cal["default_days"] - 1)
        if scope.since > latest:
            return None
        since, until = self._arg_day(a, "since"), self._arg_day(a, "until")
        if since is None:
            since = max(scope.since, until - span) if until is not None else min(max(self.today, scope.since), latest)
        if until is None:
            until = min(since + span, latest)
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
        cal = self.m["calendar"]
        limit = self._limit(a, cal["max_results"])
        window = self._calendar_window(a)
        if window is None:
            return self._page([], a, limit, more=False, scanned=0, errors=_Errors(0),
                              window=(self.scope.since, self._calendar_latest()), partial="empty_window")
        since, until = window
        want = a.get("offset", 0) + limit
        query = a.get("query", "").strip().casefold()
        jet = f"[Start] < {_quote(self._day(until + timedelta(days=1)))} AND [End] > {_quote(self._day(since))}"
        lo = datetime.combine(since, datetime.min.time())
        hi = datetime.combine(until + timedelta(days=1), datetime.min.time())

        def work(s: Session) -> dict:
            items = s.ns.GetDefaultFolder(OL_FOLDER_CALENDAR).Items
            items.IncludeRecurrences = True
            items.Sort("[Start]")
            found = items.Restrict(jet)
            errors, hits, scanned, capped, more = _Errors(self.m["error_samples"]), [], 0, False, False
            item = found.GetFirst()
            while item is not None:
                if len(hits) > want:
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
                    _reraise_disconnect(exc)
                    errors.add(exc)
                item = found.GetNext()
            return self._page(hits, a, limit, more=more, scanned=scanned, errors=errors, window=(since, until),
                              partial="scan_cap" if capped else None)

        return self.outlook.run(work, read=True)

    def calendar_get(self, a: dict) -> dict:
        scope, latest = self._need_scope(), self._calendar_latest()

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
        """Each file to attach: a relative path inside the task folder, through no junction
        or link (the task folder included), existing, at most max_bytes."""
        if len(names) > self.m["drafts"]["max_attachments"]:
            raise OutlookError(f"at most {self.m['drafts']['max_attachments']} attachments")
        root, files = self.task_dir.resolve(), []
        for i, name in enumerate(names):
            lexical = self.task_dir / name
            if PureWindowsPath(name).anchor or (lexical.resolve() != root and root not in lexical.resolve().parents):
                raise ScopeError(f"attachments[{i}] is outside the task folder; attach only files under it")
            refuse_links(lexical, self.task_dir.parent)
            path = lexical.resolve()
            if not path.is_file():
                raise OutlookError(f"attachments[{i}] is not a file in the task folder")
            if path.stat().st_size > self.m["attachments"]["max_bytes"]:
                raise OutlookError(f"attachments[{i}] is over tasks.m365.attachments.max_bytes")
            files.append(path)
        return files

    def _check_recipients(self, addresses: list[str], *, inherited: int = 0) -> None:
        """Refused when any address is not one the brief or scope names. The error gives
        counts and a reason code only, never the addresses (they may come from mail)."""
        outside = [i for i, x in enumerate(addresses) if not x or x.casefold() not in self.recipients]
        if outside:
            from_mail = sum(1 for i in outside if i < inherited)
            raise ScopeError(f"recipients_outside_scope: {len(outside)} recipient(s) the brief and email scope do "
                             f"not name ({from_mail} inherited from the original); nothing was saved")

    def _task_drafts(self, s: Session) -> list[tuple[str, str, str]]:
        """(entry_id, normalized subject, source EntryID or '') of every draft marked with this task."""
        d = self.m["drafts"]
        marker = f'"{PS_PUBLIC_STRINGS}{d["marker_property"]}" = {_quote(self.task_id)}'
        table = s.ns.GetDefaultFolder(OL_FOLDER_DRAFTS).GetTable("@SQL=" + marker, OL_USER_ITEMS)
        table.Columns.RemoveAll()
        for column in ("EntryID", "Subject", PS_PUBLIC_STRINGS + d["source_property"]):
            table.Columns.Add(column)
        found = []
        while not table.EndOfTable:
            entry_id, subject, source = table.GetNextRow().GetValues()
            found.append((entry_id, normal_subject(subject), source or ""))
        return found

    def draft(self, a: dict, *, kind: str) -> dict:
        """An unsaved draft built (new, reply or forward), checked, then saved, never sent:
        refused before Save() when any recipient, inherited ones included, is not named
        by the brief or scope; reported instead when this task already has one for the same
        source message (a reply or forward) or the same subject (a new mail). Returns ids,
        counts and reason codes only."""
        d = self.m["drafts"]
        fragment = self._draft_body(a)
        files = self._draft_files(a.get("attachments", []))
        explicit = [(OL_TO, x.strip()) for x in a.get("to", [])] + [(OL_CC, x.strip()) for x in a.get("cc", [])]
        self._check_recipients([x for _, x in explicit])

        def work(s: Session) -> dict:
            original = None if kind == "new" else self._mail_item(s, a["entry_id"])
            source = "" if original is None else original.EntryID
            key = normal_subject(a["subject"]) if original is None else None
            existing = [e for e, subj, src in self._task_drafts(s)
                        if (src == source if original is not None else (not src and subj == key))]
            if existing:
                return {"created": False, "reason": "draft_exists", "existing": existing}
            if original is None:
                item = s.app.CreateItem(OL_MAIL_ITEM)
                item.Subject = a["subject"]
            else:
                item = (original.Forward() if kind == "forward"
                        else original.ReplyAll() if a.get("reply_all") else original.Reply())
            inherited = item.Recipients.Count
            for kind_code, address in explicit:
                item.Recipients.Add(address).Type = kind_code
            item.Recipients.ResolveAll()
            got = []
            for r in item.Recipients:
                try:
                    got.append(recipient_smtp(r))
                except Exception as exc:
                    _reraise_disconnect(exc)
                    got.append("")
            if not got:
                raise OutlookError("no_recipients: the draft has no recipients; nothing was saved")
            self._check_recipients(got, inherited=inherited)
            carried = None
            if kind == "forward":
                carried = {"original": original.Attachments.Count, "carried": item.Attachments.Count}
            item.HTMLBody = insert_after_body(item.HTMLBody, fragment)
            for path in files:
                item.Attachments.Add(str(path))
            item.UserProperties.Add(d["marker_property"], OL_TEXT, False).Value = self.task_id
            if source:
                item.UserProperties.Add(d["source_property"], OL_TEXT, False).Value = source
            item.Save()
            out = {"created": True, "entry_id": item.EntryID, "recipient_count": len(got),
                   "inherited_recipient_count": inherited, "attachment_count": len(files)}
            if source:
                out["source_entry_id"] = source
            if carried is not None:
                out["forwarded_attachments"] = {**carried, "all_carried": carried["carried"] >= carried["original"]}
            return out

        return self.outlook.run(work, read=False)
