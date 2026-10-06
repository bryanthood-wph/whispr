"""kg/: migrations, the graph store and pipeline state (C.4, C.5, D.1, D.5, D.6).
No model calls; every database lives in a temp data dir."""

from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kg import db
from kg.state import KIND_DIGEST, KIND_QUARANTINE, KIND_REPEAT, KIND_RUN_FAILED, QUARANTINED, QUEUED, State, StateError
from kg.store import AMBIGUOUS, EXTRACTED, Store, StoreError, provenance, task_id
from pipeline.config import ConfigError, config_file, load_config, read_yaml
from pipeline_helpers import overlay

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
EPISODE = "ep-2026-10-01-0900"
TRANSCRIPT = ("[00:00:01] Me: I'll send the deck to Jamie by Friday\n"
              "[00:00:05] Others: we decided to ship it Friday\n"
              "[00:00:09] Others: Jamie Doe owns the pricing model\n")


class KgCase(unittest.TestCase):
    """A fresh config and migrated database in a temp data dir."""
    overlay_extra: dict = {}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        ov = overlay(self.root)
        ov.update(self.overlay_extra)
        self.cfg = load_config(overlay=ov)
        self.conn = db.connect(self.cfg)
        self.addCleanup(self.conn.close)


class TestConfig(unittest.TestCase):
    def test_kg_and_alerts_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(overlay=overlay(Path(tmp)))
            self.assertEqual(cfg["kg"], {
                "database": "whispr.db", "search_cards": 20, "busy_timeout_ms": 5000,
                "traverse": {"max_hops": 3, "default_hops": 1, "max_results": 25, "max_paths": 3, "quote_chars": 240,
                             "time_limit_ms": 5000},
                "er": {"max_edit_distance": 2, "block_chars": 2, "context_items": 3, "prompt": "prompts/resolve.md",
                       "schema": "kg/resolve.json"},
                "models": {"resolve": "kg_resolve"}, "rederive_approval_usd": 5})
            self.assertIn(cfg["kg"]["models"]["resolve"], cfg["models"])
            self.assertEqual(cfg["alerts"], {"quarantine_digest_days": 7, "repeat_item_runs": 3})
            bad = overlay(Path(tmp))
            bad["kg"] = {"er": {"max_edit_distance": -1}}
            with self.assertRaises(ConfigError):
                load_config(overlay=bad)


class TestMigrations(KgCase):
    def test_fresh_database_has_every_version(self):
        versions = [m.version for m in db.migrations()]
        self.assertEqual(sorted(db.applied_versions(self.conn)), versions)
        self.assertTrue(db.database_path(self.cfg).is_file())
        self.assertEqual(db.database_path(self.cfg).name, self.cfg["kg"]["database"])

    def test_reapplying_is_a_no_op(self):
        self.assertEqual(db.migrate(self.conn), [])
        second = db.connect(self.cfg)
        try:
            self.assertEqual(db.migrate(second), [])
            count = second.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        finally:
            second.close()
        self.assertEqual(count, len(db.migrations()))

    def test_foreign_keys_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO fact (id, type, text, quote, episode_id, provenance, recorded_at)"
                              " VALUES ('f', 'fact', 't', 'q', 'no-such-episode', 'EXTRACTED', 'now')")

    def test_newer_database_is_refused(self):
        self.conn.execute("INSERT INTO schema_version (version, name, applied_at) VALUES (9999, 'future', 'x')")
        with self.assertRaises(db.MigrationError) as ctx:
            db.connect(self.cfg)
        self.assertIn("9999", str(ctx.exception))

    def _migration_dir(self, files: dict[str, str]) -> Path:
        directory = self.root / "migrations"
        directory.mkdir()
        for name, sql in files.items():
            (directory / name).write_text(sql, encoding="utf-8")
        return directory

    def test_failed_migration_rolls_back_to_previous_version(self):
        directory = self._migration_dir({
            "0001_a.sql": "CREATE TABLE a (id TEXT PRIMARY KEY);",
            "0002_b.sql": "CREATE TABLE b (id TEXT PRIMARY KEY);\nTHIS IS NOT SQL;",
        })
        conn = sqlite3.connect(self.root / "other.db", isolation_level=None)
        try:
            with self.assertRaises(db.MigrationError):
                db.migrate(conn, directory)
            self.assertEqual(db.applied_versions(conn), {1})
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()
        self.assertIn("a", tables)
        self.assertNotIn("b", tables)

    def test_version_with_only_another_dialect_is_recorded(self):
        directory = self._migration_dir({"0001_a.sql": "CREATE TABLE a (id TEXT PRIMARY KEY);",
                                         "0002_b.postgres.sql": "CREATE EXTENSION nothing_here;"})
        conn = sqlite3.connect(self.root / "other.db", isolation_level=None)
        try:
            self.assertEqual(db.migrate(conn, directory), [1, 2])
        finally:
            conn.close()

    def test_bad_file_names_fail(self):
        with self.assertRaises(db.MigrationError):
            db.migrations(self._migration_dir({"1_init.sql": ""}))

    def test_racing_migrators_skip_what_the_other_applied(self):
        # Two processes open a fresh database: the second read its versions before the
        # first committed, so it believes nothing is applied.
        path = self.root / "race.db"
        first = sqlite3.connect(path, isolation_level=None)
        second = sqlite3.connect(path, isolation_level=None)
        real, reads = db.applied_versions, []

        def read_before_first_committed(conn):
            reads.append(conn)
            return set() if len(reads) == 1 else real(conn)

        try:
            self.assertEqual(db.migrate(first), [m.version for m in db.migrations()])
            with mock.patch.object(db, "applied_versions", read_before_first_committed):
                self.assertEqual(db.migrate(second), [])
            count = second.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        finally:
            first.close()
            second.close()
        self.assertEqual(count, len(db.migrations()))

    def test_shared_migrations_use_only_shared_sql(self):
        sqlite_only = ("AUTOINCREMENT", "PRAGMA", "VIRTUAL", "WITHOUT ROWID", "TRIGGER", "FTS5")
        for m in db.migrations():
            if m.path is None or m.path.name.count(".") > 1:
                continue
            text = re.sub(r"--[^\n]*", "", m.path.read_text(encoding="utf-8")).upper()
            for word in sqlite_only:
                self.assertNotIn(word, text, f"{m.path.name} uses {word}")


class TestConnection(KgCase):
    overlay_extra = {"kg": {"busy_timeout_ms": 1234}}

    def test_wal_and_busy_timeout_from_config(self):
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.conn.execute("PRAGMA busy_timeout").fetchone()[0], 1234)

    def test_writer_takes_the_write_lock_at_begin(self):
        other = sqlite3.connect(db.database_path(self.cfg), isolation_level=None, timeout=0)
        try:
            with db.transaction(self.conn):
                with self.assertRaises(sqlite3.OperationalError):
                    other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()


class TestTransaction(unittest.TestCase):
    """kg.db.transaction on bare connections in rollback-journal mode, where an open
    read blocks another connection's COMMIT."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "t.db"
        self.conn = self.open()
        self.conn.execute("CREATE TABLE t (x INTEGER)")

    def open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=0)
        self.addCleanup(conn.close)
        return conn

    def test_failed_commit_rolls_back_and_later_writes_persist(self):
        self.conn.execute("INSERT INTO t VALUES (1), (2)")
        reader = self.open()
        unfinished = reader.execute("SELECT x FROM t")
        unfinished.fetchone()                           # an unfinished read holds a shared lock
        with self.assertRaises(sqlite3.OperationalError):
            with db.transaction(self.conn):
                self.conn.execute("INSERT INTO t VALUES (3)")
        self.assertFalse(self.conn.in_transaction)
        unfinished.close()
        with db.transaction(self.conn):
            self.conn.execute("INSERT INTO t VALUES (4)")
        self.conn.close()
        reopened = self.open()
        self.assertEqual([r[0] for r in reopened.execute("SELECT x FROM t ORDER BY x")], [1, 2, 4])

    def test_failed_rollback_does_not_hide_the_error(self):
        # SQLite rolls the whole transaction back itself on some errors (SQLITE_FULL);
        # the rollback that follows then fails, and must not replace the real error.
        with self.assertRaises(ValueError):
            with db.transaction(self.conn):
                self.conn.execute("ROLLBACK")
                raise ValueError("the real error")
        with self.assertRaises(ValueError):
            with db.transaction(self.conn):
                with db.transaction(self.conn):         # a savepoint
                    self.conn.execute("ROLLBACK")
                    raise ValueError("the real error")
        self.assertFalse(self.conn.in_transaction)


class StoreCase(KgCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.conn, self.cfg)
        self.store.upsert_episode(EPISODE, transcript_path="transcripts/2026-10-01-0900-t.md", sha256="ab" * 32,
                                  meeting_start="2026-10-01T09:00:00+00:00", call_type="meeting",
                                  extractor_version="x1", mic_coverage=0.9, output_device="Headset", now=T0)

    def fact(self, quote: str = "we decided to ship it Friday", text: str = "Ship Friday", **kw) -> str:
        args = dict(type_="decision", text=text, quote=quote, episode_id=EPISODE, transcript_text=TRANSCRIPT)
        args.update(kw)
        return self.store.add_fact(**args)

    def task(self, quote: str = "I'll send the deck to Jamie by Friday", **kw) -> dict:
        t = {"id": task_id(EPISODE, quote), "owner": "Pat Example", "owner_basis": "volunteered",
             "action": "Send the deck to Jamie", "due": "Friday", "due_basis": "stated", "context": "review copy",
             "quote": quote, "source": {"episode": EPISODE, "start": "00:00:01"}, "confidence": 0.9,
             "status": "captured", "tools_allowed": []}
        t.update(kw)
        return t


class TestProvenance(StoreCase):
    def test_verbatim_quote_is_extracted(self):
        self.assertEqual(self.store.get(self.fact())["provenance"], EXTRACTED)

    def test_paraphrase_is_ambiguous(self):
        fact_id = self.fact(quote="we agreed to ship on Friday")
        self.assertEqual(self.store.get(fact_id)["provenance"], AMBIGUOUS)

    def test_empty_quote_is_ambiguous(self):
        self.assertEqual(provenance("  ", TRANSCRIPT), AMBIGUOUS)

    def test_edge_provenance_checked_too(self):
        jamie = self.store.mention_person("Jamie Doe", source="extract", episode_id=EPISODE)
        model = self.store.upsert_entity("project", "Pricing model", source="extract")
        good = self.store.add_edge(src_entity_id=jamie, dst_entity_id=model, relation="works_on",
                                   quote="Jamie Doe owns the pricing model", episode_id=EPISODE,
                                   transcript_text=TRANSCRIPT)
        bad = self.store.add_edge(src_entity_id=jamie, dst_entity_id=model, relation="responsible_for",
                                  quote="Jamie is responsible", episode_id=EPISODE, transcript_text=TRANSCRIPT)
        self.assertEqual(self.store.get(good)["provenance"], EXTRACTED)
        self.assertEqual(self.store.get(bad)["provenance"], AMBIGUOUS)

    def test_writing_the_same_fact_twice_adds_nothing(self):
        self.assertEqual(self.fact(), self.fact())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM fact").fetchone()[0], 1)

    def test_valid_from_defaults_to_meeting_start(self):
        self.assertEqual(self.store.get(self.fact())["valid_from"], "2026-10-01T09:00:00.000000+00:00")

    def test_types_come_from_the_ontology(self):
        with self.assertRaises(StoreError):
            self.fact(type_="rumour")
        with self.assertRaises(StoreError):
            self.store.upsert_entity("planet", "Mars", source="extract")

    def test_edge_endpoint_types_are_checked(self):
        jamie = self.store.mention_person("Jamie Doe", source="extract")
        model = self.store.upsert_entity("project", "Pricing model", source="extract")
        with self.assertRaises(StoreError):
            self.store.add_edge(src_entity_id=model, dst_entity_id=jamie, relation="works_on", quote="x",
                                episode_id=EPISODE, transcript_text=TRANSCRIPT)
        with self.assertRaises(StoreError):
            self.store.add_edge(src_entity_id=jamie, dst_entity_id=model, relation="likes", quote="x",
                                episode_id=EPISODE, transcript_text=TRANSCRIPT)
        # related_to is any -> any
        self.store.add_edge(src_entity_id=model, dst_entity_id=jamie, relation="related_to", quote="x",
                            episode_id=EPISODE, transcript_text=TRANSCRIPT)

    def test_unknown_or_tombstoned_episode_refused(self):
        with self.assertRaises(StoreError):
            self.fact(episode_id="nope")
        self.store.tombstone_episode(EPISODE, now=T0)
        with self.assertRaises(StoreError):
            self.fact()
        with self.assertRaises(StoreError):
            self.store.tombstone_episode("nope")


class TestSupersede(StoreCase):
    def test_supersede_keeps_the_old_fact(self):
        old = self.fact()
        new = self.fact(quote="ship it Monday", text="Ship Monday", valid_from="2026-10-03T09:00:00+00:00")
        self.store.supersede_fact(old, new, reason="date moved")
        row = self.store.get(old)
        self.assertEqual((row["superseded_by"], row["supersede_reason"]), (new, "date moved"))
        self.assertEqual(row["valid_to"], "2026-10-03T09:00:00.000000+00:00")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM fact").fetchone()[0], 2)
        self.assertEqual([c["id"] for c in self.store.search("ship") if c["kind"] == "fact"], [new])

    def test_supersede_only_once_and_never_by_itself(self):
        old, new = self.fact(), self.fact(quote="ship it Monday", text="Ship Monday")
        with self.assertRaises(StoreError):
            self.store.supersede_fact(old, old, reason="x")
        with self.assertRaises(StoreError):
            self.store.supersede_fact(old, new, reason=" ")
        self.store.supersede_fact(old, new, reason="date moved")
        with self.assertRaises(StoreError):
            self.store.supersede_fact(old, new, reason="again")
        with self.assertRaises(StoreError):
            self.store.supersede_fact(new, old, reason="old is superseded")
        with self.assertRaises(StoreError):
            self.store.supersede_fact(new, "missing", reason="x")

    def test_supersede_edge(self):
        jamie = self.store.mention_person("Jamie Doe", source="extract")
        a = self.store.upsert_entity("project", "Pricing model", source="extract")
        b = self.store.upsert_entity("project", "Deck", source="extract")
        old = self.store.add_edge(src_entity_id=jamie, dst_entity_id=a, relation="works_on", quote="x",
                                  episode_id=EPISODE, transcript_text=TRANSCRIPT)
        new = self.store.add_edge(src_entity_id=jamie, dst_entity_id=b, relation="works_on", quote="y",
                                  episode_id=EPISODE, transcript_text=TRANSCRIPT)
        self.store.supersede_edge(old, new, reason="moved projects")
        self.assertEqual([e["id"] for e in self.store.get(jamie)["edges"]], [new])


class TestPeople(StoreCase):
    def test_people_keyed_by_email_case_insensitively(self):
        a = self.store.upsert_person("Jane.Doe@Example.com", "Doe, Jane", source="outlook")
        b = self.store.upsert_person("jane.doe@example.com", "Jane Doe", source="outlook")
        self.assertEqual(a, b)
        self.assertEqual(self.store.aliases(a), ["Doe, Jane", "Jane Doe"])
        self.assertEqual(self.store.mention_person("jane  DOE", source="extract"), a)
        with self.assertRaises(StoreError):
            self.store.upsert_person("", "Nobody", source="outlook")

    def test_first_name_only_stays_an_unresolved_alias(self):
        self.assertIsNone(self.store.mention_person("Jamie", source="extract", episode_id=EPISODE))
        self.assertIsNone(self.store.mention_person("Jamie", source="extract", episode_id=EPISODE))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0], 0)
        unresolved = self.store.unresolved_aliases()
        self.assertEqual([(a["alias"], a["episode_id"]) for a in unresolved], [("Jamie", EPISODE)])

    def test_two_people_with_one_alias_stay_unresolved(self):
        a = self.store.upsert_person("chris.a@example.com", "Chris Adams", source="outlook")
        b = self.store.upsert_person("chris.b@example.com", "Chris Brown", source="outlook")
        self.store.add_alias(a, "Chris", source="outlook")
        # A one-word alias settles nothing (L12): two full names start with Chris.
        self.assertIsNone(self.store.mention_person("Chris", source="extract"))
        self.store.add_alias(b, "Chris", source="outlook")
        self.assertIsNone(self.store.mention_person("Chris", source="extract", episode_id=EPISODE))
        self.assertEqual(self.store.mention_person("Chris Adams", source="extract"), a)

    def test_full_name_without_email_becomes_a_person(self):
        jamie = self.store.mention_person("Jamie Doe", source="extract", episode_id=EPISODE)
        self.assertEqual(self.store.entity(jamie)["canonical_key"], "name:jamie doe")
        self.assertEqual(self.store.mention_person("Jamie Doe", source="extract"), jamie)

    def test_ontology_must_define_person(self):
        ontology = read_yaml(config_file(self.cfg["ontology"]))
        del ontology["entity_types"]["person"]
        with mock.patch("kg.store.read_yaml", return_value=ontology):
            with self.assertRaises(StoreError) as ctx:
                Store(self.conn, self.cfg)
        self.assertIn("person", str(ctx.exception))

    def test_people_never_enter_by_name_alone(self):
        with self.assertRaises(StoreError):
            self.store.upsert_entity("person", "Jamie", source="extract")
        with self.assertRaises(StoreError):
            self.store.add_alias("no-such-entity", "X", source="extract")


class TestTasks(StoreCase):
    def test_contract_round_trips(self):
        jamie = self.store.mention_person("Jamie Doe", source="extract")
        t = self.task()
        self.store.add_task(t, entity_ids=[jamie])
        self.assertEqual(self.store.get_task(t["id"]), t)
        self.assertEqual(self.store.get(t["id"])["entity_ids"], [jamie])
        self.assertEqual(self.store.tasks("captured"), [t])

    def test_invalid_contract_refused(self):
        bad = self.task(owner_basis="maybe")
        with self.assertRaises(StoreError) as ctx:
            self.store.add_task(bad)
        self.assertIn("owner_basis", str(ctx.exception))
        missing = self.task()
        del missing["tools_allowed"]
        with self.assertRaises(StoreError):
            self.store.add_task(missing)
        with self.assertRaises(StoreError):
            self.store.add_task(self.task(id="not-the-sha1"))
        with self.assertRaises(StoreError):
            self.store.add_task(self.task(), entity_ids=["no-such-entity"])
        self.assertEqual(self.store.tasks(), [])

    def test_transitions(self):
        t = self.store.add_task(self.task())
        with self.assertRaises(StoreError):
            self.store.set_task_status(t, "done")       # captured -> done skips review
        for status in ("confirmed", "ready", "in_progress", "done"):
            self.store.set_task_status(t, status)
        with self.assertRaises(StoreError):
            self.store.set_task_status(t, "captured")   # done is final
        with self.assertRaises(StoreError):
            self.store.set_task_status("missing", "confirmed")
        events = self.conn.execute("SELECT from_status, to_status FROM task_event WHERE task_id = ?"
                                   " ORDER BY at, rowid", (t,)).fetchall()
        self.assertEqual([tuple(e) for e in events], [(None, "captured"), ("captured", "confirmed"),
                                                      ("confirmed", "ready"), ("ready", "in_progress"),
                                                      ("in_progress", "done")])

    def test_rewriting_a_task_keeps_its_lifecycle(self):
        t = self.store.add_task(self.task())
        self.store.set_task_status(t, "confirmed")
        self.store.add_task(self.task())
        self.assertEqual(self.store.get_task(t)["status"], "confirmed")

    def test_funnel_counts_furthest_stage_reached(self):
        quotes = ["I'll send the deck to Jamie by Friday", "we decided to ship it Friday",
                  "Jamie Doe owns the pricing model", "Me: I'll"]
        ids = [self.store.add_task(self.task(quote=q), now=T0) for q in quotes]
        self.store.set_task_status(ids[1], "confirmed")
        for status in ("confirmed", "ready", "in_progress"):
            self.store.set_task_status(ids[2], status)
        for status in ("confirmed", "ready", "done"):
            self.store.set_task_status(ids[3], status)
        self.store.set_task_status(ids[0], "dropped")
        self.assertEqual(self.store.funnel(), {"captured": 4, "confirmed": 3, "ready": 2, "done": 1})
        self.assertEqual(self.store.funnel(since=db.utc_now(T0 + timedelta(days=1))),
                         {"captured": 0, "confirmed": 0, "ready": 0, "done": 0})

    def test_statuses_must_match_the_schema(self):
        self.conn.execute("INSERT INTO task_status (status, reaches, funnel_rank) VALUES ('archived', NULL, NULL)")
        with self.assertRaises(StoreError):
            Store(self.conn, self.cfg)


class TestRetrieval(StoreCase):
    def setUp(self):
        super().setUp()
        self.jamie = self.store.upsert_person("jamie@example.com", "Jamie Doe", source="outlook")
        self.store.add_alias(self.jamie, "JD", source="outlook")
        self.fact_id = self.fact(subject_entity_id=self.jamie, quote_start="00:00:05")

    def test_search_returns_short_cards(self):
        cards = self.store.search("Friday")
        self.assertEqual([(c["kind"], c["id"]) for c in cards], [("fact", self.fact_id)])
        self.assertEqual(cards[0]["text"], "Ship Friday")
        entity = self.store.search("JD")[0]
        self.assertEqual((entity["kind"], entity["id"], entity["facts"]), ("entity", self.jamie, 1))

    def test_search_dedupes_and_tolerates_query_syntax(self):
        self.assertEqual([c["id"] for c in self.store.search("Jamie Doe")], [self.jamie])
        self.assertEqual(self.store.search('"AND( * -- '), [])
        self.assertEqual(self.store.search("?!"), [])

    def test_get_returns_facts_with_quotes(self):
        got = self.store.get(self.jamie)
        self.assertEqual(got["aliases"], ["Jamie Doe", "JD"])
        self.assertEqual([(f["id"], f["quote"]) for f in got["facts"]], [(self.fact_id, "we decided to ship it Friday")])
        self.assertIsNone(self.store.get("unknown"))

    def test_source_returns_path_and_spans(self):
        task = self.store.add_task(self.task())
        src = self.store.source(EPISODE)
        self.assertEqual(src["transcript_path"], "transcripts/2026-10-01-0900-t.md")
        self.assertEqual([(s["kind"], s["start"]) for s in src["spans"]], [("task", "00:00:01"), ("fact", "00:00:05")])
        self.assertEqual([s["id"] for s in self.store.source(EPISODE, self.fact_id)["spans"]], [self.fact_id])
        self.assertEqual(self.store.source(EPISODE, task)["spans"][0]["quote"], self.task()["quote"])
        with self.assertRaises(StoreError):
            self.store.source(EPISODE, "not-in-this-episode")
        self.assertIsNone(self.store.source("unknown"))

    def test_tombstone_hides_but_keeps(self):
        self.store.tombstone_episode(EPISODE, now=T0)
        self.assertEqual(self.store.search("Friday"), [])
        self.assertEqual(self.store.get(self.jamie)["facts"], [])
        self.assertIsNotNone(self.store.get(self.fact_id))
        self.assertIsNotNone(self.store.source(EPISODE)["deleted_at"])
        self.store.upsert_episode(EPISODE, transcript_path="t.md", sha256="cd" * 32)
        self.assertEqual(len(self.store.search("Friday")), 1)


class TestSearchLimit(StoreCase):
    overlay_extra = {"kg": {"search_cards": 1}}

    def test_at_most_search_cards(self):
        self.fact()
        self.fact(quote="ship it Friday or Monday", text="Ship Friday maybe")
        self.assertEqual(len(self.store.search("Friday")), 1)


class StateCase(KgCase):
    overlay_extra = {"pipeline": {"max_attempts": 3}, "alerts": {"quarantine_digest_days": 7, "repeat_item_runs": 3}}

    def setUp(self):
        super().setUp()
        self.state = State(self.conn, self.cfg)


class TestRuns(StateCase):
    def finish(self, **kw):
        run = self.state.begin_run("ingest", now=T0)
        return run, self.state.finish_run(run, **{"backlog": 0, **kw})

    def test_success_needs_progress_or_proven_empty(self):
        self.assertTrue(self.finish(processed=1, eligible=4)[1])
        self.assertTrue(self.finish(processed=0, eligible=0)[1])
        self.assertEqual(self.state.open_alerts(), [])

    def test_no_progress_fails_and_alerts(self):
        run, ok = self.finish(processed=0, eligible=None)
        self.assertFalse(ok)
        self.assertEqual(self.state.run(run)["status"], "failed")
        self.assertFalse(self.finish(processed=0, eligible=5, backlog=5)[1])
        self.assertFalse(self.finish(processed=3, eligible=5, error="disk full")[1])
        alerts = self.state.open_alerts()
        self.assertEqual([(a["kind"], a["count"]) for a in alerts], [(KIND_RUN_FAILED, 3)])
        self.assertIn("disk full", alerts[0]["message"])

    def test_finish_once(self):
        run, _ = self.finish(processed=1, eligible=1)
        with self.assertRaises(StateError):
            self.state.finish_run(run, processed=1, eligible=1, backlog=0)
        with self.assertRaises(StateError):
            self.state.finish_run("missing", processed=1, eligible=1, backlog=0)

    def test_impossible_counts_are_refused(self):
        run = self.state.begin_run("ingest", now=T0)
        for counts in ({"processed": -1, "eligible": 1, "backlog": 0}, {"processed": 0, "eligible": -1, "backlog": 0},
                       {"processed": 0, "eligible": None, "backlog": -1}, {"processed": 2, "eligible": 1, "backlog": 0}):
            with self.subTest(**counts), self.assertRaises(ValueError):
                self.state.finish_run(run, **counts)
        self.assertEqual(self.state.run(run)["status"], "running")
        self.assertTrue(self.state.finish_run(run, processed=1, eligible=None, backlog=0))

    def test_run_that_never_finished_fails_at_the_next_start(self):
        dead = self.state.begin_run("ingest", now=T0)
        other = self.state.begin_run("digest", now=T0)     # another job's run is left alone
        live = self.state.begin_run("ingest", now=T0 + timedelta(hours=1))
        row = self.state.run(dead)
        self.assertEqual((row["status"], row["finished_at"]), ("failed", db.utc_now(T0 + timedelta(hours=1))))
        self.assertIn("never finished", row["error"])
        self.assertEqual([self.state.run(r)["status"] for r in (other, live)], ["running", "running"])
        alert = self.state.alert(f"{KIND_RUN_FAILED}:ingest")
        self.assertIn(dead, alert["message"])
        self.assertIn("never finished", alert["message"])
        with self.assertRaises(StateError):
            self.state.finish_run(dead, processed=1, eligible=1, backlog=0)
        self.assertTrue(self.state.finish_run(live, processed=1, eligible=1, backlog=0))

    def test_backlog_and_last_success(self):
        run, _ = self.finish(processed=2, eligible=7, backlog=5)
        self.finish(processed=0, eligible=None)
        self.assertEqual(self.state.last_success("ingest")["id"], run)
        self.assertEqual(self.state.last_success("ingest")["backlog"], 5)


class TestItems(StateCase):
    def test_quarantine_after_max_attempts_and_queue_moves_on(self):
        bad = self.state.enqueue("extract", "bad.md", now=T0)
        good = self.state.enqueue("extract", "good.md", now=T0 + timedelta(seconds=1))
        for attempt in range(3):
            run = self.state.begin_run("ingest")
            self.assertTrue(self.state.begin_item(run, bad))
            status = self.state.item_failed(bad, f"schema error {attempt}")
            self.assertEqual(status, QUARANTINED if attempt == 2 else QUEUED)
            if attempt == 0:                            # the bad item doesn't hold the good one up
                self.assertTrue(self.state.begin_item(run, good))
                self.state.item_done(good)
            self.state.finish_run(run, processed=int(attempt == 0), eligible=len(self.state.eligible("extract")) + 1,
                                  backlog=self.state.backlog("extract"))
        self.assertEqual(self.state.eligible("extract"), [])
        self.assertEqual(self.state.item(good)["status"], "done")
        alert = self.state.alert(f"{KIND_QUARANTINE}:extract:bad.md")
        self.assertEqual(alert["count"], 1)
        self.assertIn("schema error 2", alert["message"])
        self.assertIn("requeue", alert["fix"])

    def test_item_that_never_reports_back_is_quarantined(self):
        item = self.state.enqueue("extract", "crashy.md")
        run = self.state.begin_run("ingest")
        for _ in range(3):
            self.assertTrue(self.state.begin_item(run, item))
        self.assertFalse(self.state.begin_item(run, item))
        self.assertEqual(self.state.item(item)["status"], QUARANTINED)
        self.assertIn("never reported back", self.state.alert(f"{KIND_QUARANTINE}:extract:crashy.md")["message"])
        with self.assertRaises(StateError):
            self.state.begin_item(run, item)

    def test_requeue_gives_fresh_attempts(self):
        item = self.state.enqueue("extract", "bad.md")
        run = self.state.begin_run("ingest")
        for _ in range(3):
            self.state.begin_item(run, item)
            self.state.item_failed(item, "boom")
        self.state.requeue(item)
        row = self.state.item(item)
        self.assertEqual((row["status"], row["attempts"], row["last_error"]), (QUEUED, 0, None))
        self.assertEqual(self.state.backlog("extract"), 1)

    def test_enqueue_is_idempotent_and_unknown_items_fail(self):
        item = self.state.enqueue("extract", "a.md")
        self.state.item_done(item)
        self.assertEqual(self.state.enqueue("extract", "a.md"), item)
        self.assertEqual(self.state.item(item)["status"], "done")
        with self.assertRaises(StateError):
            self.state.item_failed(item, "done items don't fail")
        with self.assertRaises(StateError):
            self.state.item_done("missing")

    def test_repeat_item_alert_after_consecutive_runs(self):
        state = State(self.conn, {**self.cfg, "pipeline": {"max_attempts": 100}})
        item = state.enqueue("extract", "stuck.md")
        key = f"{KIND_REPEAT}:extract:stuck.md"

        def one_run(touch: bool, job: str = "ingest"):
            run = state.begin_run(job)
            if touch:
                state.begin_item(run, item)
                state.begin_item(run, item)            # a retry in the same run counts once
                state.item_failed(item, "still failing")
            state.finish_run(run, processed=0, eligible=1, backlog=1)

        one_run(True)
        one_run(True)
        one_run(False)                                  # a run without it breaks the streak
        one_run(True, job="other")                      # another job's run doesn't count
        one_run(True)
        one_run(True)
        self.assertIsNone(state.alert(key))
        one_run(True)
        self.assertEqual(state.alert(key)["count"], 1)
        self.assertEqual(state.item(item)["consecutive_runs"], 3)


class TestAlerts(StateCase):
    def test_dedupe_count_and_reopen(self):
        a = self.state.raise_alert("k", "k:1", "first", "fix it", now=T0)
        b = self.state.raise_alert("k", "k:1", "second", "fix it", now=T0 + timedelta(hours=1))
        self.assertEqual(a, b)
        row = self.state.alert("k:1")
        self.assertEqual((row["count"], row["message"]), (2, "second"))
        self.assertLess(row["first_seen"], row["last_seen"])
        self.assertTrue(self.state.acknowledge("k:1"))
        self.assertFalse(self.state.acknowledge(a))      # already acknowledged
        self.assertFalse(self.state.acknowledge("no-such-alert"))
        self.assertEqual(self.state.open_alerts(), [])
        self.state.raise_alert("k", "k:1", "third", "fix it")
        self.assertEqual([(r["id"], r["count"]) for r in self.state.open_alerts()], [(a, 3)])

    def test_quarantine_digest(self):
        item = self.state.enqueue("extract", "bad.md")
        run = self.state.begin_run("ingest")
        for _ in range(3):
            self.state.begin_item(run, item, now=T0)
            self.state.item_failed(item, "boom", now=T0)
        own_key = f"{KIND_QUARANTINE}:extract:bad.md"
        day = timedelta(days=1)

        self.assertEqual(self.state.quarantine_digest(now=T0 + day), [])
        self.assertEqual([a["dedupe_key"] for a in self.state.open_alerts()], [own_key])

        digest = self.state.quarantine_digest(now=T0 + 8 * day)
        self.assertEqual([i["id"] for i in digest], [item])
        self.assertEqual([a["kind"] for a in self.state.open_alerts()], [KIND_DIGEST])
        self.assertIsNotNone(self.state.alert(own_key)["expired_at"])

        self.state.acknowledge(KIND_DIGEST)
        self.state.quarantine_digest(now=T0 + 9 * day)   # within the period: stays quiet
        self.assertEqual(self.state.open_alerts(), [])
        self.state.quarantine_digest(now=T0 + 16 * day)  # a period later: raised again
        self.assertEqual([(a["kind"], a["count"]) for a in self.state.open_alerts()], [(KIND_DIGEST, 2)])


class TestMaintenanceLog(StateCase):
    def test_log_and_metrics_belong_to_a_running_run(self):
        run = self.state.begin_run("maintain")
        self.state.log_maintenance(run, "integrity", found=2, repaired=1, escalated=1, details="dangling edge")
        self.state.record_metric(run, "ambiguous_share", 0.1)
        self.state.record_metric(run, "ambiguous_share", 0.2)
        self.assertEqual([(m["check_name"], m["found"]) for m in self.state.maintenance(run)], [("integrity", 2)])
        self.assertEqual(self.state.metrics(run), {"ambiguous_share": 0.2})
        self.state.finish_run(run, processed=1, eligible=1, backlog=0)
        with self.assertRaises(StateError):
            self.state.record_metric(run, "late", 1.0)


if __name__ == "__main__":
    unittest.main()
