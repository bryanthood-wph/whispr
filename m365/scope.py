"""The email scope grammar (tasks.m365.grammar) and the checks every whispr-m365 query
passes (docs/plan/task-intake-and-worker.md §5, F6). Stdlib only.

A task's scope holds items {type, value}; the items of type tasks.m365.scope_type are
the email scope, one clause per value, `<prefix><separator><value>`:

    sender:jane@example.com   sender:@example.com   folder:Inbox/Projects
    subject:Apollo budget     since:2026-09-01      until:2026-10-31

- **sender**: an address, or every address at a domain (`@` then the domain). Senders
  are alternatives; with none, any sender.
- **folder**: a path under the mailbox root, levels split by folder_separator; its
  subfolders are searched too. With none, tasks.m365.default_folders (Outlook's own
  default folders, by olFolder name or number) and their subfolders.
- **subject**: words the subject contains, letter case ignored; alternatives.
- **since / until**: the window, inclusive days in grammar.date_format, at most one of
  each. With no since, the window opens tasks.m365.default_window_days before today; with
  no until, it closes today.

Prefixes, the separator and the date format come from config; letter case of a prefix is
ignored. kg/store.py runs parse_email_scope on the values the intake records, so a bad
clause is refused there, by name, before a task can be ready.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

ADDRESS = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
DOMAIN = re.compile(r"@[^@\s]+\.[^@\s]+")
# An address inside free text (a brief answer), for the draft recipient check.
ADDRESS_IN_TEXT = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
KINDS = ("sender", "folder", "subject", "since", "until")


class ScopeError(ValueError):
    """A scope clause that does not parse, or a query outside the scope."""


def _grammar(cfg: dict) -> dict:
    return cfg["tasks"]["m365"]["grammar"]


def date_hint(cfg: dict) -> str:
    """grammar.date_format as a reader writes it (%Y-%m-%d -> YYYY-MM-DD)."""
    hint = _grammar(cfg)["date_format"]
    for code, shown in (("%Y", "YYYY"), ("%m", "MM"), ("%d", "DD")):
        hint = hint.replace(code, shown)
    return hint


def grammar_help(cfg: dict) -> str:
    """The grammar in one line, from config, for errors and tool descriptions."""
    g = _grammar(cfg)
    k, sep, day = g["keys"], g["separator"], date_hint(cfg)
    return (f"{k['sender']}{sep}<address or @domain>, {k['folder']}{sep}<path{g['folder_separator']}under"
            f"{g['folder_separator']}the mailbox>, {k['subject']}{sep}<words in the subject>, "
            f"{k['since']}{sep}<{day}>, {k['until']}{sep}<{day}>")


def parse_day(text: str, cfg: dict, what: str = "date") -> date:
    """A day in grammar.date_format, or a ScopeError naming `what`."""
    try:
        return datetime.strptime(text.strip(), _grammar(cfg)["date_format"]).date()
    except (ValueError, AttributeError):
        raise ScopeError(f"{what} {text!r} is not a date like {date_hint(cfg)}") from None


def folder_path(text: str, cfg: dict) -> tuple[str, ...]:
    """A folder path's levels; an empty level is a ScopeError."""
    parts = tuple(p.strip() for p in text.strip().split(_grammar(cfg)["folder_separator"]))
    if not text.strip() or any(not p for p in parts):
        raise ScopeError(f"folder {text!r} has an empty level; write levels like "
                         f"Inbox{_grammar(cfg)['folder_separator']}Projects")
    return parts


def parse_sender(text: str, what: str = "sender") -> tuple[str, str]:
    """A sender: ('address', jane@x.com) or ('domain', x.com), casefolded; else a ScopeError."""
    text = text.strip()
    if ADDRESS.fullmatch(text):
        return "address", text.casefold()
    if DOMAIN.fullmatch(text):
        return "domain", text[1:].casefold()
    raise ScopeError(f"{what}: a sender is an address (jane@example.com) or a domain (@example.com), "
                     f"not {text!r}")


def parse_clause(value: str, cfg: dict) -> tuple[str, Any]:
    """One scope value -> (kind, parsed): sender -> ('address' | 'domain', text casefolded),
    folder -> levels, subject -> words, since/until -> a date."""
    g = _grammar(cfg)
    prefix, sep, rest = value.partition(g["separator"])
    by_prefix = {p.casefold(): kind for kind, p in g["keys"].items()}
    kind = by_prefix.get(prefix.strip().casefold())
    rest = rest.strip()
    if not sep or kind is None:
        raise ScopeError(f"{value!r} is not a clause of the email scope grammar: {grammar_help(cfg)}")
    if not rest:
        raise ScopeError(f"{value!r} has nothing after {prefix.strip()}{g['separator']}")
    if kind == "sender":
        return kind, parse_sender(rest, what=repr(value))
    if kind == "folder":
        return kind, folder_path(rest, cfg)
    if kind == "subject":
        return kind, rest
    return kind, parse_day(rest, cfg, what=f"{value!r}:")


@dataclass(frozen=True)
class EmailScope:
    """A task's email scope, parsed. `none` means nothing is in scope (every mail and
    calendar read is refused)."""
    since: date
    until: Optional[date] = None
    since_given: bool = False
    addresses: frozenset = frozenset()
    domains: frozenset = frozenset()
    folders: tuple = ()
    subjects: tuple = ()
    none: bool = False
    values: tuple = field(default=())

    @property
    def has_senders(self) -> bool:
        return bool(self.addresses or self.domains)

    def sender_ok(self, address: str) -> bool:
        """Whether mail from `address` is in scope (any sender when the scope names none).
        An empty address (one Outlook could not resolve) is in scope only then."""
        if not self.has_senders:
            return True
        a = (address or "").strip().casefold()
        return bool(a) and (a in self.addresses or ("@" in a and a.rpartition("@")[2] in self.domains))

    def sender_arg_ok(self, sender: tuple[str, str]) -> bool:
        """Whether a parsed sender argument lies inside the scope's senders: an address the
        scope allows, or a domain the scope names whole (or any, with no senders)."""
        kind, value = sender
        return self.sender_ok(value) if kind == "address" else (not self.has_senders or value in self.domains)

    def subject_ok(self, subject: str) -> bool:
        s = (subject or "").casefold()
        return not self.subjects or any(w.casefold() in s for w in self.subjects)

    def closes(self, today: date) -> date:
        return self.until if self.until is not None else today

    def describe(self, cfg: dict, today: date) -> dict:
        sep = _grammar(cfg)["folder_separator"]
        return {"none": self.none, "values": list(self.values),
                "senders": sorted(self.addresses) + sorted(f"@{d}" for d in self.domains),
                "folders": [sep.join(f) for f in self.folders] or None,
                "default_folders": None if self.folders else list(cfg["tasks"]["m365"]["default_folders"]),
                "subjects": list(self.subjects), "since": self.since.isoformat(),
                "since_default": not self.since_given, "until": self.closes(today).isoformat()}


def parse_email_scope(values: Iterable[str], cfg: dict, *, today: date) -> EmailScope:
    """The email scope from its values (tasks.intake.scope_none alone, or no value, is a
    scope with nothing in it). Every bad clause is named in one ScopeError."""
    values = [v for v in values]
    none = cfg["tasks"]["intake"]["scope_none"]
    default_since = today - timedelta(days=cfg["tasks"]["m365"]["default_window_days"])
    if not values or all(v.strip().casefold() == none.casefold() for v in values):
        return EmailScope(since=default_since, none=True, values=tuple(values))
    errors, found = [], {k: [] for k in KINDS}
    for value in values:
        try:
            kind, parsed = parse_clause(value, cfg)
        except ScopeError as exc:
            errors.append(str(exc))
            continue
        found[kind].append(parsed)
    for kind in ("since", "until"):
        if len(found[kind]) > 1:
            errors.append(f"at most one {_grammar(cfg)['keys'][kind]} clause, not {len(found[kind])}")
    since = found["since"][0] if found["since"] else None
    until = found["until"][0] if found["until"] else None
    if since and until and since > until:
        errors.append(f"since {since} is after until {until}")
    if errors:
        raise ScopeError("email scope: " + "; ".join(errors))
    return EmailScope(since=since or default_since, until=until, since_given=since is not None,
                      addresses=frozenset(v for t, v in found["sender"] if t == "address"),
                      domains=frozenset(v for t, v in found["sender"] if t == "domain"),
                      folders=tuple(dict.fromkeys(found["folder"])), subjects=tuple(found["subject"]),
                      values=tuple(values))


def addresses_in(value: Any) -> set[str]:
    """Every email address in a brief answer (text, list or object, walked), casefolded."""
    if isinstance(value, str):
        return {a.casefold() for a in ADDRESS_IN_TEXT.findall(value)}
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple)):
        return set().union(*(addresses_in(v) for v in value)) if value else set()
    return set()


def allowed_recipients(brief: dict, scope: EmailScope) -> frozenset:
    """The addresses a draft may go to: those the brief names (any field, the scope list
    included) and the scope's sender addresses. A sender domain names no one."""
    return frozenset(addresses_in([answer.get("value") for answer in brief.values()]) | set(scope.addresses))
