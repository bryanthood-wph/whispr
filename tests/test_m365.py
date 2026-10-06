"""whispr-m365 (m365/): the email scope grammar, task and role binding, the scope checks
on every query, attachments, drafts and the server's health, over a fake Outlook COM
layer. No real Outlook, no model calls; every database and folder is a temp one."""

from __future__ import annotations

import ast
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime
from pathlib import Path

import yaml

from kg import db
from kg.store import Store, StoreError, task_id
from m365 import outlook as ol
from m365.outlook import ComWorker, Outlook, format_day, insert_after_body, safe_name
from m365.sanitize import sanitize_html, text_to_html
from m365.scope import ScopeError, allowed_recipients, grammar_help, parse_email_scope
from m365.server import (DRAFT, MANIFEST, READ, ROLE_ENV, ROLES, TASK_ENV, M365Server, load_binding, tool_names)
from pipeline.config import ConfigError, load_config, merged
from pipeline_helpers import budget_answer, overlay

REPO = Path(__file__).resolve().parent.parent
TODAY = date(2026, 10, 6)
EP = "ep-2026-09-30-1000"
WHO = "tester"
DISCONNECT = -2147417848          # RPC_E_DISCONNECTED
SCOPE = ["sender:@example.com", "folder:Inbox", "since:2026-09-01"]


# ---- the fake Outlook ----------------------------------------------------------------


class FakeComError(Exception):
    def __init__(self, hresult: int):
        super().__init__(hresult, "fake COM error")
        self.hresult = hresult


class Accessor:
    def __init__(self, props: dict):
        self.props = props

    def GetProperty(self, name):
        return self.props.get(name, "")


class Entry:
    def __init__(self, smtp: str):
        self.Type, self.Address, self.smtp = "EX", "/o=ExchangeLabs/cn=" + smtp.split("@")[0], smtp

    def GetExchangeUser(self):
        return type("User", (), {"PrimarySmtpAddress": self.smtp})()


class Recipient:
    def __init__(self, address: str, exchange: bool = False):
        self.Type = ol.OL_TO
        self.Address = Entry(address).Address if exchange else address
        self.PropertyAccessor = Accessor({ol.PR_SMTP_ADDRESS: address} if exchange else {})
        self.AddressEntry = Entry(address)


class Recipients(list):
    def Add(self, address):
        r = Recipient(address)
        self.append(r)
        return r

    def ResolveAll(self):
        return True


class Attachment:
    def __init__(self, name: str, data: bytes):
        self.FileName, self.data, self.Size, self.Type = name, data, len(data), 1

    def SaveAsFile(self, path):
        Path(path).write_bytes(self.data)


class Attachments(list):
    @property
    def Count(self):
        return len(self)

    def Item(self, i):
        return self[i - 1]

    def Add(self, path):
        self.append(Attachment(Path(path).name, Path(path).read_bytes()))


class UserProperties(dict):
    def Add(self, name, kind, add_to_folder):
        prop = type("Prop", (), {"Value": None})()
        self[name] = prop
        return prop


class Draft:
    """An unsaved mail item. It has no Send or Display: calling either fails the test."""

    def __init__(self, outlook: "FakeOutlook", recipients=(), html="", subject="", attachments=()):
        self._outlook, self.Recipients = outlook, Recipients(recipients)
        self.HTMLBody, self.Subject, self.EntryID = html, subject, None
        self.Attachments, self.UserProperties = Attachments(attachments), UserProperties()

    def Save(self):
        self.EntryID = f"draft-{len(self._outlook.drafts.items) + 1}"
        self._outlook.drafts.items.append(self)


class Mail:
    MessageClass = "IPM.Note"

    def __init__(self, entry_id, sender, received, subject, body="", cc=(), attachments=()):
        self.EntryID, self.smtp, self.ReceivedTime, self.Subject, self.Body = entry_id, sender, received, subject, body
        self.SenderName, self.SenderEmailAddress = sender.split("@")[0].title(), Entry(sender).Address
        self.Sender, self.To, self.CC, self.cc = Entry(sender), "Pat Example", "; ".join(cc), list(cc)
        self.PropertyAccessor = Accessor({ol.PR_SENDER_SMTP: sender})
        self.Attachments, self.Parent, self.outlook = Attachments(attachments), None, None

    def Reply(self):
        return Draft(self.outlook, [Recipient(self.smtp, exchange=True)], "<html><body><div>quoted</div></body></html>",
                     "RE: " + self.Subject)

    def ReplyAll(self):
        return Draft(self.outlook, [Recipient(a, exchange=True) for a in [self.smtp, *self.cc]],
                     "<html><body><div>quoted</div></body></html>", "RE: " + self.Subject)

    def Forward(self):
        return Draft(self.outlook, [], "<html><body><div>forwarded</div></body></html>", "FW: " + self.Subject,
                     list(self.Attachments))


class Appointment:
    MessageClass = "IPM.Appointment"
    AllDayEvent, Location, IsRecurring, RequiredAttendees, OptionalAttendees = False, "Room 1", False, "Jane", ""

    def __init__(self, entry_id, start, end, subject, organizer="jane@example.com", body="agenda"):
        self.EntryID, self.Start, self.End, self.Subject, self.Body = entry_id, start, end, subject, body
        self.Organizer, self._org, self.Recipients, self.Parent = organizer.split("@")[0], organizer, Recipients(), None

    def GetOrganizer(self):
        return Entry(self._org)


class Row:
    def __init__(self, values):
        self.values = values

    def GetValues(self):
        if isinstance(self.values, Exception):
            raise self.values
        return self.values


class Columns(list):
    def RemoveAll(self):
        self.clear()

    def Add(self, name):
        self.append(name)


class Table:
    def __init__(self, rows):
        self.rows, self.i, self.Columns = rows, 0, Columns()

    def Sort(self, column, descending):
        pass

    @property
    def EndOfTable(self):
        return self.i >= len(self.rows)

    def GetNextRow(self):
        self.i += 1
        return self.rows[self.i - 1]


class Restricted:
    def __init__(self, items):
        self.items, self.i = items, 0

    def GetFirst(self):
        self.i = 0
        return self.GetNext()

    def GetNext(self):
        if self.i >= len(self.items):
            return None
        self.i += 1
        return self.items[self.i - 1]


class Items(list):
    IncludeRecurrences = False

    def Sort(self, column):
        self.sorted_by = column

    def Restrict(self, jet):
        self.jet = jet
        return Restricted(sorted(self, key=lambda a: a.Start))


class Folder:
    def __init__(self, name, path, items=(), children=()):
        self.Name, self.FolderPath, self.items, self.Folders = name, path, list(items), list(children)
        self.filters, self.fail, self.block = [], [], None
        for item in self.items:
            item.Parent = self

    @property
    def Items(self):
        return Items(self.items)

    def GetTable(self, dasl, contents):
        self.filters.append(dasl)
        if self.block is not None:
            self.block.wait(5)
        if self.fail:
            raise self.fail.pop(0)
        marker = re.search(r'/WhisprTaskId" = \'([^\']*)\'', dasl)
        if marker:
            return Table([Row((d.EntryID, d.Subject)) for d in self.items
                          if d.UserProperties.get("WhisprTaskId") and d.UserProperties["WhisprTaskId"].Value
                          == marker.group(1)])
        return Table([m if isinstance(m, Row) else
                      Row((m.EntryID, m.Subject, m.ReceivedTime, m.SenderName, m.smtp, bool(m.Attachments)))
                      for m in sorted(self.items, key=lambda m: getattr(m, "ReceivedTime", datetime.min),
                                      reverse=True)])


class FakeOutlook:
    """An Outlook Application with a small mailbox."""

    def __init__(self):
        d = lambda day: datetime(2026, 9, day, 10, 0)                      # noqa: E731
        att = Attachment("q4 plan.xlsx", b"budget")
        self.m1 = Mail("m1", "jane@example.com", d(15), "Apollo budget",
                       "Hello. " + "x" * 30 + " the budget is due Friday. " + "y" * 30, attachments=[att])
        self.m2 = Mail("m2", "bob@other.org", d(20), "Lunch")
        self.m3 = Mail("m3", "jane@example.com", datetime(2026, 8, 1, 9), "Old news")
        self.m4 = Mail("m4", "amy@example.com", d(25), "Apollo plan")
        self.m5 = Mail("m5", "jane@example.com", d(16), "Elsewhere")
        self.m6 = Mail("m6", "jane@example.com", d(18), "Apollo all hands", cc=["bob@other.org"])
        root = "\\\\pat@example.com"
        self.projects = Folder("Projects", root + "\\Inbox\\Projects", [self.m4])
        self.inbox = Folder("Inbox", root + "\\Inbox", [self.m1, self.m2, self.m3, self.m6], [self.projects])
        self.sent = Folder("Sent Items", root + "\\Sent Items")
        self.other = Folder("Other", root + "\\Other", [self.m5])
        self.drafts = Folder("Drafts", root + "\\Drafts")
        self.calendar = Folder("Calendar", root + "\\Calendar", [
            Appointment("a1", datetime(2026, 10, 7, 9), datetime(2026, 10, 7, 10), "Apollo sync"),
            Appointment("a2", datetime(2026, 10, 8, 9), datetime(2026, 10, 8, 10), "Dentist", "me@else.net"),
            Appointment("a3", datetime(2026, 12, 1, 9), datetime(2026, 12, 1, 10), "Far away")])
        self.root = Folder("pat@example.com", root, [], [self.inbox, self.sent, self.other, self.drafts,
                                                         self.calendar])
        for folder in (self.projects, self.inbox, self.other):
            for m in folder.items:
                m.outlook = self
        self.connects = 0
        app = self

        class NS:
            DefaultStore = type("Store", (), {"StoreID": "store-1", "GetRootFolder": lambda s: app.root})()

            def GetDefaultFolder(self, n):
                return {ol.OL_FOLDER_CALENDAR: app.calendar, ol.OL_FOLDER_DRAFTS: app.drafts}[n]

            def GetItemFromID(self, entry_id, store_id):
                for item in app.all_items():
                    if item.EntryID == entry_id:
                        return item
                raise FakeComError(-2147221233)          # MAPI_E_NOT_FOUND

        self.ns = NS()

    def all_items(self):
        return [*self.inbox.items, *self.projects.items, *self.other.items, *self.drafts.items, *self.calendar.items]

    def connect(self):
        self.connects += 1
        return self

    def GetNamespace(self, name):
        return self.ns

    def CreateItem(self, kind):
        return Draft(self)


# ---- fixtures ------------------------------------------------------------------------


def m365_overlay(root: Path, **m365) -> dict:
    ov = overlay(root)
    ov["tasks"] = {"m365": {"date_picture": "M/d/yyyy", **m365}}
    return ov


class M365Case(unittest.TestCase):
    """A temp config and database holding one ready task with an email scope."""

    m365: dict = {}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cfg = load_config(overlay=m365_overlay(self.root, **self.m365))
        conn = db.connect(self.cfg)
        self.addCleanup(conn.close)
        self.store = Store(conn, self.cfg)
        self.store.upsert_episode(EP, transcript_path="t.md", sha256="ab" * 32,
                                  meeting_start="2026-09-30T10:00:00+00:00", call_type="meeting")
        self.tid = self.add_task(SCOPE)
        self.fake = FakeOutlook()

    def add_task(self, email_values, *, quote="I'll send Jane the budget", status="ready"):
        tid = self.store.add_task({"id": task_id(EP, quote), "owner": "Pat Example", "owner_basis": "volunteered",
                                   "action": "Send Jane the budget", "due": "Friday", "due_basis": "stated",
                                   "context": "", "quote": quote, "source": {"episode": EP, "start": "00:00:01"},
                                   "confidence": 0.9, "status": "captured", "tools_allowed": []})
        intake = self.store.intake
        brief = {f: f"the {f}" for f in self.store.required_fields
                 if f not in (self.store.scope_field, self.store.budget_field)}
        brief["audience"] = "Jane Doe <Jane@Example.com>"
        brief[self.store.budget_field] = budget_answer(self.store)
        scope = [{"type": "email", "value": v} for v in email_values] + [
            {"type": t, "value": intake["scope_none"]} for t in intake["scope_types"] if t != "email"]
        self.store.update_task(tid, actor=WHO, brief=brief, scope=scope, brief_source="stated")
        for step in {"captured": (), "ready": ("confirmed", "ready")}[status]:
            self.store.update_task(tid, actor=WHO, status=step, reason="step")
        return tid

    def server(self, role=READ, tid=None, env=None, **kw) -> M365Server:
        env = env if env is not None else {TASK_ENV: tid or self.tid, ROLE_ENV: role}
        binding = load_binding(self.cfg, env, today=TODAY)
        worker = ComWorker(context=contextlib.nullcontext)
        self.addCleanup(worker.close)
        return M365Server(self.cfg, binding, Outlook(self.cfg, connect=self.fake.connect), worker)

    @staticmethod
    def call(server, name, args=None) -> tuple[bool, dict]:
        out = server.call_tool(name, args or {})
        return out["isError"], json.loads(out["content"][0]["text"])

    def ok(self, server, name, args=None) -> dict:
        is_error, body = self.call(server, name, args)
        self.assertFalse(is_error, body)
        return body

    def refused(self, server, name, args=None, pattern="") -> dict:
        is_error, body = self.call(server, name, args)
        self.assertTrue(is_error, body)
        self.assertRegex(body["error"], pattern)
        return body


# ---- the grammar ---------------------------------------------------------------------


class TestGrammar(M365Case):
    def test_clauses_parse(self):
        s = parse_email_scope(["sender:Jane@Example.com", "SENDER:@x.org", "folder:Inbox / Projects",
                               "subject:Apollo", "since:2026-09-01", "until:2026-09-30"], self.cfg, today=TODAY)
        self.assertEqual((s.addresses, s.domains, s.folders, s.subjects), (
            {"jane@example.com"}, {"x.org"}, (("Inbox", "Projects"),), ("Apollo",)))
        self.assertEqual((s.since, s.until, s.since_given, s.none), (date(2026, 9, 1), date(2026, 9, 30), True, False))
        self.assertTrue(s.sender_ok("bob@X.org") and s.sender_ok("jane@example.com"))
        self.assertFalse(s.sender_ok("amy@example.com") or s.sender_ok(""))

    def test_default_window_and_none(self):
        s = parse_email_scope(["sender:a@b.com"], self.cfg, today=TODAY)
        self.assertEqual((s.since, s.closes(TODAY), s.since_given), (date(2026, 7, 8), TODAY, False))
        self.assertTrue(parse_email_scope(["none"], self.cfg, today=TODAY).none)
        self.assertTrue(parse_email_scope([], self.cfg, today=TODAY).none)

    def test_every_bad_clause_is_named(self):
        with self.assertRaises(ScopeError) as ctx:
            parse_email_scope(["from:a@b.com", "sender:bob", "since:01/09/2026", "folder:Inbox//x",
                               "subject:", "since:2026-02-01", "since:2026-03-01"], self.cfg, today=TODAY)
        msg = str(ctx.exception)
        for part in ("'from:a@b.com'", "'sender:bob'", "01/09/2026", "empty level", "nothing after",
                     "at most one since"):
            self.assertIn(part, msg)
        with self.assertRaises(ScopeError) as ctx:
            parse_email_scope(["since:2026-09-02", "until:2026-09-01"], self.cfg, today=TODAY)
        self.assertIn("after until", str(ctx.exception))

    def test_prefixes_come_from_config(self):
        cfg = load_config(overlay={**m365_overlay(self.root), "tasks": {"m365": {
            "grammar": {"keys": {"sender": "from"}, "separator": "="}}}})
        self.assertEqual(parse_email_scope(["FROM=a@b.com"], cfg, today=TODAY).addresses, {"a@b.com"})
        with self.assertRaises(ScopeError):
            parse_email_scope(["sender:a@b.com"], cfg, today=TODAY)
        self.assertIn("from=<address or @domain>", grammar_help(cfg))

    def test_the_intake_refuses_a_bad_email_value(self):
        with self.assertRaises(StoreError) as ctx:
            self.add_task(["sendr:jane@example.com"], quote="another")
        self.assertIn("sendr", str(ctx.exception))

    def test_scope_type_must_be_a_scope_type(self):
        with self.assertRaises(ConfigError):
            load_config(overlay={**overlay(self.root), "tasks": {"m365": {"scope_type": "mail"}}})

    def test_allowed_recipients_are_addresses_named(self):
        s = parse_email_scope(["sender:bob@x.org", "sender:@y.org"], self.cfg, today=TODAY)
        brief = {"audience": {"value": "Jane <JANE@example.com> and Al"}, "budget": {"value": {"reason": "a@b.cc"}},
                 "scope": {"value": [{"type": "folder", "value": "C:/x"}]}}
        self.assertEqual(allowed_recipients(brief, s), {"jane@example.com", "a@b.cc", "bob@x.org"})


# ---- binding and roles ---------------------------------------------------------------


class TestBinding(M365Case):
    def test_no_task_id_refuses_every_tool(self):
        server = self.server(env={ROLE_ENV: READ})
        for spec in MANIFEST:
            if READ in spec.roles:
                self.refused(server, spec.name, {}, "no task")
        self.assertEqual(self.fake.connects, 0)

    def test_unknown_task_and_a_bad_id_refuse(self):
        self.refused(self.server(tid="f" * 40), "scope_get", {}, "unknown task")
        self.refused(self.server(tid="..\\x"), "scope_get", {}, "not a task id")

    def test_a_task_not_ready_refuses(self):
        tid = self.add_task(SCOPE, quote="captured one", status="captured")
        self.refused(self.server(tid=tid), "scope_get", {}, "bind_statuses")

    def test_bad_role_serves_nothing(self):
        server = self.server(env={TASK_ENV: self.tid, ROLE_ENV: "admin"})
        self.assertEqual(server.dispatch("tools/list", {})["tools"], [])
        self.refused(server, "mail_search", {}, ROLE_ENV)
        self.refused(server, "draft_new", {"subject": "s", "to": ["jane@example.com"], "body": "b"}, ROLE_ENV)

    def test_each_role_lists_and_answers_only_its_tools(self):
        for role in ROLES:
            server = self.server(role)
            listed = {t["name"] for t in server.dispatch("tools/list", {})["tools"]}
            self.assertEqual(listed, set(tool_names(role)))
            for spec in MANIFEST:
                if role not in spec.roles:
                    self.refused(server, spec.name, {}, "not served")
        self.assertEqual(set(tool_names(READ)) & set(tool_names(DRAFT)), {"scope_get"})
        self.assertTrue(all(s.read for s in MANIFEST if READ in s.roles))
        self.assertFalse(any(s.read for s in MANIFEST if s.roles == (DRAFT,)))

    def test_no_tool_sends_moves_deletes_or_flags(self):
        for spec in MANIFEST:
            self.assertNotRegex(spec.name, r"(?i)send|move|delete|flag|remove|display")
        for path in (REPO / "m365").glob("*.py"):
            names = {n.attr for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                     if isinstance(n, ast.Attribute)}
            self.assertFalse(names & {"Send", "Display", "Move", "Delete", "MarkAsTask", "FlagRequest",
                                      "FlagStatus", "PermanentDelete"}, path.name)

    def test_scope_get_shows_the_binding(self):
        out = self.ok(self.server(DRAFT), "scope_get")
        self.assertEqual((out["task_id"], out["role"], out["status"]), (self.tid, DRAFT, "ready"))
        self.assertEqual(out["email_scope"]["senders"], ["@example.com"])
        self.assertEqual(out["allowed_recipients"], ["jane@example.com"])
        self.assertNotIn("allowed_recipients", self.ok(self.server(READ), "scope_get"))

    def test_the_module_launches_and_refuses_without_a_task(self):
        ov = self.root / "overlay.yaml"
        ov.write_text(yaml.safe_dump(m365_overlay(self.root)), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in (TASK_ENV, ROLE_ENV, "WHISPR_OVERLAY")}
        env[ROLE_ENV] = READ
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "mail_search",
                                                                                "arguments": {}}}]
        proc = subprocess.run([sys.executable, "-m", "m365", "--config", str(ov)], cwd=REPO, env=env,
                              input="".join(json.dumps(x) + "\n" for x in lines), capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = [json.loads(x) for x in proc.stdout.splitlines()]
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "whispr-m365")
        self.assertEqual({t["name"] for t in replies[1]["result"]["tools"]}, set(tool_names(READ)))
        self.assertTrue(replies[2]["result"]["isError"])
        self.assertIn("no task", replies[2]["result"]["content"][0]["text"])


# ---- reads ---------------------------------------------------------------------------


class TestMailReads(M365Case):
    def test_search_returns_only_what_the_scope_allows(self):
        out = self.ok(self.server(), "mail_search")
        self.assertEqual([r["entry_id"] for r in out["results"]], ["m6", "m1"])    # not m2 (sender), m3 (date)
        self.assertEqual((out["search_complete"], out["truncated_reason"], out["scanned_count"], out["error_count"]),
                         (True, None, 4, 0))
        self.assertIn("UNTRUSTED", out["untrusted"])
        dasl = self.fake.inbox.filters[-1]
        self.assertIn(f'"{ol.PR_SENDER_SMTP}" LIKE \'%@example.com\'', dasl)
        self.assertIn("'8/31/2026'", dasl)                                       # the locale picture, widened a day
        self.assertEqual(self.fake.other.filters, [])                            # never searched
        sub = self.ok(self.server(), "mail_search", {"folder": "inbox/projects"})
        self.assertEqual([r["entry_id"] for r in sub["results"]], ["m4"])

    def test_queries_outside_the_scope_are_refused(self):
        server = self.server()
        self.refused(server, "mail_search", {"folder": "Other"}, "outside")
        self.refused(server, "mail_search", {"sender": "bob@other.org"}, "outside")
        self.refused(server, "mail_search", {"since": "2026-08-01"}, "outside")
        self.refused(server, "mail_search", {"until": "2026-10-30"}, "outside")
        self.refused(server, "mail_search", {"since": "1 Sept"}, "not a date")
        self.assertEqual(self.ok(server, "mail_search", {"sender": "jane@example.com"})["results"][0]["sender"],
                         "jane@example.com")
        for entry_id, why in (("m2", "sender"), ("m5", "folder"), ("m3", "date")):
            self.refused(server, "mail_get", {"entry_id": entry_id}, why)
        self.refused(server, "mail_get", {"entry_id": "nope"}, "no item")

    def test_an_email_scope_of_none_refuses_reads(self):
        tid = self.add_task(["none"], quote="no mail")
        self.refused(self.server(tid=tid), "mail_search", {}, "none")
        self.refused(self.server(tid=tid), "calendar_search", {}, "none")

    def test_get_message_is_capped_text_marked_untrusted(self):
        self.cfg["tasks"]["m365"]["mail"]["body_chars"] = 20
        out = self.ok(self.server(), "mail_get", {"entry_id": "m1"})
        self.assertEqual((len(out["body"]), out["truncated"], out["body_chars"]), (20, True, len(self.fake.m1.Body)))
        self.assertIn("UNTRUSTED", out["untrusted"])
        self.assertFalse({"headers", "internet_headers", "html"} & set(out))

    def test_body_search_filters_on_text_and_cuts_snippets_from_hits(self):
        out = self.ok(self.server(), "mail_body_search", {"query": "budget"})
        self.assertIn(f'"{ol.TEXT_DESCRIPTION}" LIKE \'%budget%\'', self.fake.inbox.filters[-1])
        hit = {r["entry_id"]: r for r in out["results"]}["m1"]
        self.assertTrue(hit["match_in_body"])
        self.assertIn("budget", hit["snippet"])

    def test_scan_cap_and_limit_mark_partial_results(self):
        self.cfg["tasks"]["m365"]["mail"]["max_scan"] = 1
        out = self.ok(self.server(), "mail_search")
        self.assertEqual((out["search_complete"], out["truncated_reason"], out["scanned_count"]),
                         (False, "scan_cap", 1))
        self.cfg["tasks"]["m365"]["mail"]["max_scan"] = 100
        page = self.ok(self.server(), "mail_search", {"limit": 1})
        self.assertEqual((len(page["results"]), page["has_more"], page["truncated_reason"]), (1, True, "limit"))
        self.assertEqual(self.ok(self.server(), "mail_search", {"limit": 1, "offset": 1})["results"][0]["entry_id"],
                         "m1")

    def test_errors_are_counted_not_swallowed(self):
        self.fake.inbox.items.append(Row(FakeComError(-2147467259)))
        out = self.ok(self.server(), "mail_search")
        self.assertEqual((out["error_count"], len(out["errors"])), (1, 1))
        self.assertEqual([r["entry_id"] for r in out["results"]], ["m6", "m1"])

    def test_attach_once_and_reattach_after_a_disconnect(self):
        server = self.server()
        self.ok(server, "mail_search")
        self.ok(server, "mail_search")
        self.assertEqual(self.fake.connects, 1)
        self.fake.inbox.fail = [FakeComError(DISCONNECT)]
        self.assertEqual(len(self.ok(server, "mail_search")["results"]), 2)       # one retry, re-attached
        self.assertEqual(self.fake.connects, 2)
        self.fake.inbox.fail = [FakeComError(DISCONNECT), FakeComError(DISCONNECT)]
        self.refused(server, "mail_search", {}, "dropped the connection")


class TestCalendar(M365Case):
    def test_search_expands_recurrences_and_keeps_to_scope(self):
        out = self.ok(self.server(), "calendar_search")
        self.assertEqual([r["entry_id"] for r in out["results"]], ["a1"])       # a2's people are outside scope
        self.assertEqual(out["window"], {"since": "2026-10-06", "until": "2026-10-12"})
        self.assertTrue(out["search_complete"])

    def test_windows_are_capped_and_bounded(self):
        server = self.server()
        self.refused(server, "calendar_search", {"since": "2026-10-01", "until": "2026-12-01"}, "at most")
        self.refused(server, "calendar_search", {"since": "2026-08-01", "until": "2026-08-02"}, "outside")
        self.refused(server, "calendar_search", {"since": "2027-03-01", "until": "2027-03-02"}, "outside")

    def test_get_checks_the_appointment(self):
        out = self.ok(self.server(), "calendar_get", {"entry_id": "a1"})
        self.assertEqual((out["subject"], out["body"]), ("Apollo sync", "agenda"))
        self.refused(self.server(), "calendar_get", {"entry_id": "a2"}, "people")
        self.refused(self.server(), "calendar_get", {"entry_id": "m1"}, "not a calendar")

    def test_locale_date_pictures(self):
        d = date(2026, 3, 7)
        self.assertEqual([format_day(d, p) for p in ("M/d/yyyy", "dd.MM.yyyy", "yyyy-MM-dd", "d'/'M'/'yy")],
                         ["3/7/2026", "07.03.2026", "2026-03-07", "7/3/26"])
        with self.assertRaises(ol.OutlookError):
            format_day(d, "dd-MMM-yyyy")


# ---- attachments ---------------------------------------------------------------------


class TestAttachments(M365Case):
    def test_names_are_cleaned(self):
        self.assertEqual(safe_name("..\\..\\a/b\\c:d?.pdf", self.cfg), "cd.pdf")
        self.assertEqual(safe_name("CON.txt", self.cfg), "_CON.txt")
        self.assertEqual(safe_name("nul", self.cfg), "_nul")
        self.assertEqual(safe_name("lpt1.tar.gz", self.cfg), "_lpt1.tar.gz")
        self.assertEqual(safe_name("...", self.cfg), "attachment")
        self.assertEqual(safe_name("report. ", self.cfg), "report")
        long = safe_name("x" * 500 + ".docx", self.cfg)
        self.assertEqual((len(long), long[-5:]), (self.cfg["tasks"]["m365"]["attachments"]["name_chars"], ".docx"))

    def test_saves_into_the_task_folder_only_and_never_overwrites(self):
        server = self.server()
        self.assertEqual(self.ok(server, "attachment_list", {"entry_id": "m1"})["attachments"],
                         [{"index": 1, "name": "q4 plan.xlsx", "size": 6, "type": 1}])
        att = self.fake.m1.Attachments[0]
        att.FileName = "..\\..\\evil:CON.xlsx"
        first = self.ok(server, "attachment_save", {"entry_id": "m1", "index": 1})
        folder = Path(self.cfg["paths"]["data_dir"]) / "tasks" / self.tid / "attachments"
        self.assertEqual(Path(first["path"]).parent, folder)
        self.assertEqual((first["name"], first["already_saved"]), ("evilCON.xlsx", False))
        self.assertTrue(self.ok(server, "attachment_save", {"entry_id": "m1", "index": 1})["already_saved"])
        att.data = b"other content"
        att.Size = len(att.data)
        third = self.ok(server, "attachment_save", {"entry_id": "m1", "index": 1})
        self.assertNotEqual(third["name"], first["name"])
        self.assertEqual(Path(first["path"]).read_bytes(), b"budget")             # not overwritten
        self.assertEqual(sorted(p.name for p in folder.iterdir()), sorted([first["name"], third["name"]]))
        self.refused(server, "attachment_save", {"entry_id": "m1", "index": 2}, "index")
        self.refused(server, "attachment_save", {"entry_id": "m2", "index": 1}, "outside")

    def test_size_cap(self):
        self.cfg["tasks"]["m365"]["attachments"]["max_bytes"] = 3
        self.refused(self.server(), "attachment_save", {"entry_id": "m1", "index": 1}, "max_bytes")


# ---- drafts --------------------------------------------------------------------------


class TestDrafts(M365Case):
    def new(self, server, **kw):
        return self.call(server, "draft_new", {"subject": "Budget", "to": ["jane@example.com"],
                                               "body": "Hi Jane,\n\nThe budget.", **kw})

    def test_a_new_draft_is_saved_marked_and_never_sent(self):
        is_error, out = self.new(self.server(DRAFT))
        self.assertFalse(is_error, out)
        self.assertEqual((out["created"], out["recipients"]), (True, ["jane@example.com"]))
        [draft] = self.fake.drafts.items
        self.assertEqual(draft.UserProperties["WhisprTaskId"].Value, self.tid)
        self.assertIn("<p>Hi Jane,</p><p>The budget.</p>", draft.HTMLBody)
        self.assertFalse(hasattr(draft, "Send") or hasattr(draft, "Display"))

    def test_a_rerun_reports_the_existing_draft(self):
        server = self.server(DRAFT)
        self.new(server)
        is_error, again = self.new(server)
        self.assertFalse(is_error)
        self.assertEqual((again["created"], [d["entry_id"] for d in again["existing"]]), (False, ["draft-1"]))
        self.assertEqual(len(self.fake.drafts.items), 1)

    def test_a_recipient_the_brief_does_not_name_is_refused(self):
        server = self.server(DRAFT)
        self.refused(server, "draft_new", {"subject": "s", "to": ["amy@example.com"], "body": "b"}, "do not name")
        self.refused(server, "draft_new", {"subject": "s", "to": ["jane@example.com"], "cc": ["x@y.com"],
                                           "body": "b"}, "x@y.com")
        self.assertEqual(self.fake.drafts.items, [])

    def test_reply_checks_inherited_recipients_and_inserts_after_body(self):
        server = self.server(DRAFT)
        self.refused(server, "draft_reply", {"entry_id": "m6", "reply_all": True, "body": "b"}, "bob@other.org")
        self.assertEqual(self.fake.drafts.items, [])
        out = self.ok(server, "draft_reply", {"entry_id": "m6", "body": "Thanks"})
        self.assertEqual(out["recipients"], ["jane@example.com"])
        html = self.fake.drafts.items[0].HTMLBody
        self.assertLess(html.index("<body>"), html.index("Thanks"))
        self.assertLess(html.index("Thanks"), html.index("quoted"))

    def test_reply_to_a_message_outside_scope_is_refused(self):
        self.refused(self.server(DRAFT), "draft_reply", {"entry_id": "m2", "body": "b"}, "outside")

    def test_forward_reports_carried_attachments(self):
        out = self.ok(self.server(DRAFT), "draft_forward", {"entry_id": "m1", "to": ["jane@example.com"],
                                                            "body": "FYI"})
        self.assertEqual(out["forwarded_attachments"], {"original": 1, "carried": 1, "all_carried": True})

    def test_attachments_only_from_the_task_folder(self):
        task_dir = Path(self.cfg["paths"]["data_dir"]) / "tasks" / self.tid
        (task_dir / "out").mkdir(parents=True)
        (task_dir / "out" / "memo.docx").write_bytes(b"memo")
        outside = self.root / "secret.txt"
        outside.write_text("s", encoding="utf-8")
        server = self.server(DRAFT)
        for bad in ("../secret.txt", str(outside), "..\\..\\..\\secret.txt"):
            self.refused(server, "draft_new", {"subject": "s", "to": ["jane@example.com"], "body": "b",
                                               "attachments": [bad]}, "outside the task folder")
        self.refused(server, "draft_new", {"subject": "s", "to": ["jane@example.com"], "body": "b",
                                           "attachments": ["out/missing.docx"]}, "not a file")
        is_error, out = self.new(server, attachments=["out/memo.docx"])
        self.assertFalse(is_error, out)
        self.assertEqual(out["attachments"], ["memo.docx"])

    def test_html_bodies_are_sanitized(self):
        self.ok(self.server(DRAFT), "draft_new", {"subject": "s", "to": ["jane@example.com"], "body_format": "html",
                                                  "body": "<p onclick='x()'>Hi</p><script>alert(1)</script>"})
        self.assertIn("<p>Hi</p>", self.fake.drafts.items[0].HTMLBody)
        self.assertNotIn("script", self.fake.drafts.items[0].HTMLBody)


class TestSanitizer(unittest.TestCase):
    RULES = merged({})["tasks"]["m365"]["sanitize"]

    def test_allowlist(self):
        clean = lambda h: sanitize_html(h, self.RULES)                          # noqa: E731
        self.assertEqual(clean("<p onclick='x'>a<b>b</p>"), "<p>a<b>b</b></p>")
        self.assertEqual(clean("<script>evil()</script><style>p{}</style>ok"), "ok")
        self.assertEqual(clean('<a href="javascript:alert(1)">x</a>'), "<a>x</a>")
        self.assertEqual(clean('<a href="jav&#x61;script:alert(1)">x</a>'), "<a>x</a>")
        self.assertEqual(clean('<a href="https://e.com/?a=1&b=2" target="_blank">x</a>'),
                         '<a href="https://e.com/?a=1&amp;b=2">x</a>')
        self.assertEqual(clean("<blink>t</blink><img src=x onerror=y>"), "t")
        self.assertEqual(clean("<!-- c --><p>1 < 2 &amp; 3</p>"), "<p>1 &lt; 2 &amp; 3</p>")
        self.assertEqual(clean("<div><p>open"), "<div><p>open</p></div>")
        self.assertEqual(clean("</p></body><p>x</p>"), "<p>x</p>")
        self.assertEqual(text_to_html("a<b>\nc\n\nd"), "<p>a&lt;b&gt;<br>c</p><p>d</p>")
        self.assertEqual(insert_after_body("<html><BODY class='x'>q</BODY></html>", "N"),
                         "<html><BODY class='x'>Nq</BODY></html>")


# ---- health --------------------------------------------------------------------------


class TestHealth(M365Case):
    m365 = {"call_timeout_s": 0.2}

    def test_a_timeout_marks_the_server_unhealthy(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.fake.inbox.block = release
        server = self.server()
        self.refused(server, "mail_search", {}, "no answer from Outlook")
        release.set()
        self.refused(server, "scope_get", {}, "unhealthy")
        self.refused(server, "mail_search", {}, "unhealthy")

    def test_a_draft_timeout_says_the_result_is_unknown(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.fake.drafts.block = release
        server = self.server(DRAFT)
        self.refused(server, "draft_new", {"subject": "s", "to": ["jane@example.com"], "body": "b"},
                     "unknown.*do not retry")

    def test_a_draft_is_not_retried_after_a_disconnect(self):
        self.fake.drafts.fail = [FakeComError(DISCONNECT)]
        self.refused(self.server(DRAFT), "draft_new", {"subject": "s", "to": ["jane@example.com"], "body": "b"},
                     "unknown")
        self.assertEqual(self.fake.drafts.items, [])


if __name__ == "__main__":
    unittest.main()
