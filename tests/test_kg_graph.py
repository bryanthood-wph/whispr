"""kg/traverse.py, kg/resolve.py and the MCP server (kg/mcp_server.py): multi-hop reads,
entity resolution with merge/undo, and the read-only stdio server.
No model calls (a fake `ask` stands in); every database lives in a temp data dir."""

from __future__ import annotations

import json
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

from kg import db
from kg.mcp_server import INTERNAL_ERROR, INVALID_PARAMS, METHOD_NOT_FOUND, PARSE_ERROR, Server
from kg.resolve import SAME, Candidate, Resolver, edit_distance
from kg.state import State
from kg.store import RETRACTED, SUPERSEDED, Store, StoreError, task_id
from kg.traverse import Traverser
from pipeline.config import load_config
from pipeline_helpers import overlay

REPO = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).with_name("fixtures") / "kg_multihop.json"
# Arguments of the fixture's steps that name an entity (resolved to its id).
_ENTITY_ARGS = ("entity_id", "src_id", "dst_id")


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def build_fixture(store: Store, data: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Write the fixture's graph; returns (entity name -> id, edge key -> id)."""
    texts = {}
    for ep in data["episodes"]:
        texts[ep["id"]] = "\n".join(ep["transcript"])
        store.upsert_episode(ep["id"], transcript_path=f"transcripts/{ep['id']}.md", sha256=db.stable_id(ep["id"]),
                             meeting_start=ep["meeting_start"], call_type="meeting")
    ids = {p["name"]: store.upsert_person(p["email"], p["name"], source="outlook") for p in data["people"]}
    ids.update({e["name"]: store.upsert_entity(e["type"], e["name"], source="extract") for e in data["entities"]})
    edges = {e["key"]: store.add_edge(src_entity_id=ids[e["src"]], dst_entity_id=ids[e["dst"]],
                                      relation=e["relation"], quote=e["quote"], quote_start=e["start"],
                                      episode_id=e["episode"], transcript_text=texts[e["episode"]])
             for e in data["edges"]}
    for s in data["supersede"]:
        store.supersede_edge(edges[s["old"]], edges[s["new"]], reason=s["reason"])
    for f in data["facts"]:
        store.add_fact(type_=f["type"], text=f["text"], quote=f["quote"], quote_start=f["start"],
                       episode_id=f["episode"], transcript_text=texts[f["episode"]], subject_entity_id=ids[f["subject"]])
    for t in data["tasks"]:
        store.add_task({"id": task_id(t["episode"], t["quote"]), "owner": t["owner"], "owner_basis": t["owner_basis"],
                        "action": t["action"], "due": t["due"], "due_basis": t["due_basis"], "context": t["context"],
                        "quote": t["quote"], "source": {"episode": t["episode"], "start": t["start"]},
                        "confidence": 0.9, "status": "captured", "tools_allowed": []},
                       entity_ids=[ids[n] for n in t["entities"]])
    return ids, edges


def snapshot(conn: sqlite3.Connection) -> dict[str, list]:
    """Every graph row, row by row, for exact before/after comparison (merge records,
    maintenance log and runs excluded: an unmerge adds to those by design). The search
    index is compared by content, since a re-inserted row gets a new rowid."""
    tables = ("episode", "entity", "alias", "fact", "edge", "task", "task_entity", "task_event")
    snap = {t: sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {t}")) for t in tables}
    snap["search_fts"] = sorted(tuple(r) for r in conn.execute("SELECT kind, ref_id, body FROM search_fts"))
    return snap


def quotes(chain: list[dict]) -> list[str]:
    return [edge["quote"] for edge in chain]


class GraphCase(unittest.TestCase):
    """A fresh config and migrated database holding the multi-hop fixture."""
    overlay_extra: dict = {}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ov = overlay(self.root)
        for key, value in self.overlay_extra.items():
            self.ov[key] = value
        self.cfg = load_config(overlay=self.ov)
        self.conn = db.connect(self.cfg)
        self.addCleanup(self.conn.close)
        self.store = Store(self.conn, self.cfg)
        self.t = Traverser(self.store)
        self.data = load_fixture()
        self.ids, self.edges = build_fixture(self.store, self.data)

    def names(self, answer: dict) -> list[str]:
        return [r["entity"]["name"] for r in answer["results"]]

    def result(self, answer: dict, name: str) -> dict:
        return next(r for r in answer["results"] if r["entity"]["name"] == name)


class TestNeighbors(GraphCase):
    def test_one_two_and_three_hops(self):
        dana = self.ids["Dana Reyes"]
        one = self.t.neighbors(dana, hops=1)
        self.assertEqual(sorted(self.names(one)), ["Data Platform", "Project Atlas"])
        two = self.t.neighbors(dana, hops=2)
        self.assertEqual(sorted(self.names(two)), ["Data Platform", "Databricks", "Project Atlas", "Sam Ortiz"])
        three = self.t.neighbors(dana, hops=3)
        lee = self.result(three, "Lee Wong")
        self.assertEqual(lee["depth"], 3)
        self.assertEqual(quotes(lee["chain"]), ["Dana is leading the Atlas migration",
                                                "we decided to move Atlas to Databricks",
                                                "Lee runs the Databricks workspace"])
        self.assertEqual([r["depth"] for r in three["results"]], sorted(r["depth"] for r in three["results"]))
        hop = lee["chain"][0]
        for key in ("relation", "quote", "provenance", "episode_id", "valid_from", "valid_to", "src_id", "dst_id"):
            self.assertIn(key, hop)
        self.assertEqual((hop["relation"], hop["provenance"], hop["episode_id"]),
                         ("works_on", "EXTRACTED", "ep-2026-09-01-1500"))

    def test_relation_filter(self):
        answer = self.t.neighbors(self.ids["Dana Reyes"], hops=3, relations=["works_on"])
        self.assertEqual(sorted(self.names(answer)), ["Project Atlas", "Sam Ortiz"])
        with self.assertRaises(StoreError):
            self.t.neighbors(self.ids["Dana Reyes"], hops=1, relations=["likes"])

    def test_as_of_shows_the_graph_then(self):
        atlas = self.ids["Project Atlas"]
        now = self.t.neighbors(atlas, hops=1, relations=["uses"])
        self.assertEqual(self.names(now), ["Databricks"])
        before = self.t.neighbors(atlas, hops=1, relations=["uses"], as_of="2026-09-10T00:00:00+00:00")
        self.assertEqual(self.names(before), ["Snowflake"])
        self.assertEqual(self.result(before, "Snowflake")["chain"][0]["state"], SUPERSEDED)
        after = self.t.neighbors(atlas, hops=1, relations=["uses"], as_of="2026-09-20T00:00:00+00:00")
        self.assertEqual(self.names(after), ["Databricks"])
        # The handover moment: the old edge's validity ends where the new one's starts.
        at = self.t.neighbors(atlas, hops=1, relations=["uses"], as_of="2026-09-15T15:00:00+00:00")
        self.assertEqual(self.names(at), ["Databricks"])

    def test_window_keeps_edges_valid_in_it(self):
        atlas = self.ids["Project Atlas"]
        early = self.t.neighbors(atlas, hops=1, relations=["uses"], until="2026-09-10T00:00:00+00:00")
        self.assertEqual(self.names(early), ["Snowflake"])
        late = self.t.neighbors(atlas, hops=1, relations=["uses"], since="2026-09-16T00:00:00+00:00")
        self.assertEqual(self.names(late), ["Databricks"])
        span = self.t.neighbors(atlas, hops=1, relations=["uses"], since="2026-09-01T00:00:00+00:00",
                                until="2026-09-30T00:00:00+00:00")
        self.assertEqual(sorted(self.names(span)), ["Databricks", "Snowflake"])

    def test_cycles_end_and_each_entity_shows_once(self):
        # Dana - Atlas - Sam - Data Platform - Dana is a cycle; add a self-loop too.
        atlas = self.ids["Project Atlas"]
        self.store.add_edge(src_entity_id=atlas, dst_entity_id=atlas, relation="related_to", quote="Atlas",
                            episode_id="ep-2026-09-01-1500", transcript_text="Atlas")
        answer = self.t.neighbors(self.ids["Dana Reyes"], hops=3)
        names = self.names(answer)
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("Dana Reyes", names)
        self.assertEqual(self.result(answer, "Sam Ortiz")["depth"], 2)

    def test_hidden_rows(self):
        self.store.tombstone_episode("ep-2026-09-08-1500")
        names = self.names(self.t.neighbors(self.ids["Dana Reyes"], hops=3))
        self.assertNotIn("Data Platform", names)
        self.assertNotIn("Sam Ortiz", names)       # both of Sam's edges were said in that episode
        self.assertIn("Lee Wong", names)

    def test_parallel_edges_fold_into_one_hop(self):
        text = "Dana is leading the Atlas migration again"
        self.store.upsert_episode("ep-extra", transcript_path="t.md", sha256="cd" * 32,
                                  meeting_start="2026-09-20T15:00:00+00:00")
        self.store.add_edge(src_entity_id=self.ids["Dana Reyes"], dst_entity_id=self.ids["Project Atlas"],
                            relation="works_on", quote=text, episode_id="ep-extra", transcript_text=text)
        hop = self.result(self.t.neighbors(self.ids["Dana Reyes"], hops=1), "Project Atlas")["chain"][0]
        self.assertEqual((hop["quote"], hop["parallel"]), ("Dana is leading the Atlas migration", 1))

    def test_bad_hops_refused(self):
        with self.assertRaises(StoreError):
            self.t.neighbors(self.ids["Dana Reyes"], hops=0)
        with self.assertRaises(StoreError):
            self.t.neighbors("no-such-entity", hops=1)


class TestHubsAndPages(GraphCase):
    """The owner's node joins everything, so walks don't step through it by default;
    within a depth the most recently linked come first; types and offset narrow and page."""
    EXTRA = "ep-2026-09-20-1500"

    def setUp(self):
        super().setUp()
        s = self.store
        s.upsert_episode(self.EXTRA, transcript_path="t.md", sha256="ab" * 32, meeting_start="2026-09-20T15:00:00+00:00")
        self.owner = s.upsert_person(self.cfg["owner"]["email"], self.cfg["owner"]["name"], source="outlook")
        for other in ("Dana Reyes", "Priya Shah"):
            s.add_edge(src_entity_id=self.owner, dst_entity_id=self.ids[other], relation="related_to",
                       quote=f"me and {other}", episode_id=self.EXTRA, transcript_text=f"me and {other}")

    def test_walks_do_not_pass_through_the_owner(self):
        dana, priya = self.ids["Dana Reyes"], self.ids["Priya Shah"]
        names = self.names(self.t.neighbors(dana, hops=2))
        self.assertIn(self.cfg["owner"]["name"], names)               # an end is fine
        self.assertNotIn("Priya Shah", names)                         # only reachable through the owner
        self.assertIn("Priya Shah", self.names(self.t.neighbors(dana, hops=2, through_owner=True)))
        self.assertEqual(sorted(self.names(self.t.neighbors(self.owner, hops=1))), ["Dana Reyes", "Priya Shah"])
        self.assertEqual(self.t.paths(dana, priya, max_hops=3)["paths"], [])
        via = self.t.paths(dana, priya, max_hops=3, through_owner=True)["paths"]
        self.assertEqual([quotes(p["edges"]) for p in via], [["me and Dana Reyes", "me and Priya Shah"]])
        self.assertEqual(len(self.t.paths(dana, self.owner, max_hops=1)["paths"]), 1)

    def test_most_recently_linked_first_then_types_and_offset(self):
        dana = self.ids["Dana Reyes"]
        # Depth 1 of Dana: the owner (09-20), Data Platform (09-08), Project Atlas (09-01).
        self.assertEqual(self.names(self.t.neighbors(dana, hops=1)),
                         [self.cfg["owner"]["name"], "Data Platform", "Project Atlas"])
        page = self.t.neighbors(dana, hops=1, offset=1)
        self.assertEqual((self.names(page), page["offset"], page["truncated"]), (["Data Platform", "Project Atlas"],
                                                                                 1, False))
        people = self.t.neighbors(dana, hops=2, types=["person"])
        self.assertEqual(sorted(self.names(people)), sorted([self.cfg["owner"]["name"], "Sam Ortiz"]))
        self.assertEqual(people["reached"], 2)
        with self.assertRaises(StoreError):
            self.t.neighbors(dana, hops=1, types=["planet"])
        with self.assertRaises(StoreError):
            self.t.neighbors(dana, hops=1, offset=-1)

    def test_mcp_exposes_types_offset_and_through_owner(self):
        server = Server(self.conn, self.cfg)
        props = server.tools["neighbors"]["inputSchema"]["properties"]
        self.assertEqual(set(props["types"]["items"]["enum"]), self.store.entity_types)
        self.assertIn("offset", props)
        self.assertIn("through_owner", props)
        self.assertIn("through_owner", server.tools["paths"]["inputSchema"]["properties"])
        self.assertIn("item_id", server.tools["source"]["description"])
        reply = server.call_tool("neighbors", {"entity_id": self.ids["Dana Reyes"], "offset": 2,
                                               "types": ["project"], "through_owner": True})
        self.assertFalse(reply["isError"], reply)
        self.assertEqual(json.loads(reply["content"][0]["text"])["results"], [])


class TestCaps(GraphCase):
    overlay_extra = {"kg": {"traverse": {"max_hops": 2, "default_hops": 1, "max_results": 2, "max_paths": 1,
                                         "quote_chars": 10}}}

    def test_hops_results_paths_and_quotes_are_capped(self):
        answer = self.t.neighbors(self.ids["Dana Reyes"], hops=5)
        self.assertEqual((answer["hops"], answer["hops_requested"]), (2, 5))
        self.assertEqual(len(answer["results"]), 2)
        self.assertTrue(answer["truncated"])
        self.assertEqual(answer["reached"], 4)
        self.assertNotIn("Lee Wong", [r["entity"]["name"] for r in answer["results"]])
        quote = answer["results"][0]["chain"][0]["quote"]
        self.assertLessEqual(len(quote), 11)
        self.assertTrue(quote.endswith("…"))
        paths = self.t.paths(self.ids["Sam Ortiz"], self.ids["Dana Reyes"], max_hops=9)
        self.assertEqual(paths["max_hops"], 2)
        self.assertEqual(len(paths["paths"]), 1)
        self.assertTrue(paths["truncated"])

    def test_timeline_keeps_the_latest(self):
        answer = self.t.timeline(self.ids["Project Atlas"])
        self.assertTrue(answer["truncated"])
        self.assertEqual(len(answer["items"]), 2)
        self.assertTrue(all(i["at"].startswith("2026-09-15") for i in answer["items"]))


class TestScale(unittest.TestCase):
    """A synthetic graph of personal-graph scale and worse: 20k entities, 150k edges,
    one hub of degree 15k. Walks must read only the edges near them (the edge indexes),
    never the whole edge table; a walk past kg.traverse.time_limit_ms is stopped."""
    ENTITIES, EDGES, HUB_DEGREE, EPISODES = 20000, 150000, 15000, 100
    BOUND_S = 2.0                 # generous: the index-driven walks take well under this

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.cfg = load_config(overlay=overlay(Path(tmp.name)))
        cls.conn = db.connect(cls.cfg)
        cls.addClassCleanup(cls.conn.close)
        now = db.utc_now()
        node = lambda i: f"{i:040x}"
        rng = random.Random(1)
        links = [(node(0), node(j)) for j in range(1, cls.HUB_DEGREE + 1)]         # node 0 is the hub
        while len(links) < cls.EDGES:
            a, b = rng.randrange(1, cls.ENTITIES), rng.randrange(1, cls.ENTITIES)
            if a != b:
                links.append((node(a), node(b)))
        cls.conn.execute("PRAGMA foreign_keys = OFF")           # bulk build: the rows are consistent by construction
        with db.transaction(cls.conn):
            cls.conn.executemany("INSERT INTO episode (id, transcript_path, sha256, meeting_start, recorded_at)"
                                 " VALUES (?, ?, 'x', ?, ?)",
                                 [(f"ep{i}", f"t{i}.md", now, now) for i in range(cls.EPISODES)])
            cls.conn.executemany("INSERT INTO entity (id, type, canonical_name, canonical_key, created_at)"
                                 " VALUES (?, 'topic', ?, ?, ?)",
                                 [(node(i), f"Topic {i}", f"topic {i}", now) for i in range(cls.ENTITIES)])
            cls.conn.executemany("INSERT INTO edge (id, src_entity_id, dst_entity_id, relation, quote, episode_id,"
                                 " provenance, valid_from, recorded_at) VALUES (?, ?, ?, 'related_to', 'q', ?,"
                                 " 'EXTRACTED', ?, ?)",
                                 [(f"e{k}", a, b, f"ep{k % cls.EPISODES}", now, now) for k, (a, b) in enumerate(links)])
        cls.conn.execute("PRAGMA foreign_keys = ON")
        cls.store = Store(cls.conn, cls.cfg)
        cls.leaf, cls.other, cls.hub = node(12345), node(17777), node(0)

    def timed(self, walk):
        started = time.perf_counter()
        answer = walk()
        return answer, time.perf_counter() - started

    def test_walks_stay_fast_beside_a_hub(self):
        t = Traverser(self.store)
        two, took = self.timed(lambda: t.neighbors(self.leaf, hops=2))
        self.assertLess(took, self.BOUND_S)
        self.assertGreater(two["reached"], self.HUB_DEGREE)            # the leaf's hub neighbour opens 15k more
        paths, took = self.timed(lambda: t.paths(self.leaf, self.other, max_hops=3))
        self.assertLess(took, self.BOUND_S)
        self.assertTrue(paths["paths"])                                # leaf - hub - other at least

    def test_time_limit_stops_a_long_walk(self):
        cfg = {**self.cfg, "kg": {**self.cfg["kg"], "traverse": {**self.cfg["kg"]["traverse"], "time_limit_ms": 1}}}
        t = Traverser(Store(self.conn, cfg))
        with self.assertRaisesRegex(StoreError, "time_limit_ms"):
            t.neighbors(self.hub, hops=3)
        reply = Server(self.conn, cfg).call_tool("neighbors", {"entity_id": self.hub, "hops": 3})
        self.assertTrue(reply["isError"])
        self.assertIn("time_limit_ms", reply["content"][0]["text"])
        self.assertEqual(Traverser(self.store).neighbors(self.leaf, hops=1)["start"]["id"], self.leaf)  # handler gone


class TestPaths(GraphCase):
    def test_shortest_paths_are_ordered_edge_lists(self):
        sam, dana = self.ids["Sam Ortiz"], self.ids["Dana Reyes"]
        answer = self.t.paths(sam, dana, max_hops=2)
        self.assertFalse(answer["truncated"])
        found = sorted(quotes(p["edges"]) for p in answer["paths"])
        self.assertEqual(found, [["Sam is helping Dana on Atlas", "Dana is leading the Atlas migration"],
                                 ["Sam is on the Data Platform team", "Dana sits in Data Platform too"]])
        for path in answer["paths"]:
            self.assertEqual(path["length"], len(path["edges"]))
            at = sam                                   # each edge continues from the last entity
            for edge in path["edges"]:
                self.assertIn(at, (edge["src_id"], edge["dst_id"]))
                at = edge["dst_id"] if edge["src_id"] == at else edge["src_id"]
            self.assertEqual(at, dana)

    def test_longer_routes_come_after_shorter_and_none_repeat_an_entity(self):
        answer = self.t.paths(self.ids["Sam Ortiz"], self.ids["Dana Reyes"], max_hops=3)
        lengths = [p["length"] for p in answer["paths"]]
        self.assertEqual(lengths, sorted(lengths))
        for path in answer["paths"]:
            ends = [e["src_id"] for e in path["edges"]] + [e["dst_id"] for e in path["edges"]]
            self.assertEqual(len(set(ends)), path["length"] + 1)

    def test_as_of_and_no_route(self):
        dana, priya = self.ids["Dana Reyes"], self.ids["Priya Shah"]
        self.assertEqual(self.t.paths(dana, priya, max_hops=3)["paths"], [])
        then = self.t.paths(dana, priya, max_hops=3, as_of="2026-09-10T00:00:00+00:00")["paths"]
        self.assertEqual([quotes(p["edges"]) for p in then],
                         [["Dana is leading the Atlas migration", "Atlas runs on Snowflake for now",
                           "Priya owns our Snowflake account"]])
        self.assertEqual(self.t.paths(dana, self.ids["Lee Wong"], max_hops=2)["paths"], [])
        with self.assertRaises(StoreError):
            self.t.paths(dana, dana, max_hops=2)


class TestTimeline(GraphCase):
    def test_time_order_with_superseded_rows_marked(self):
        items = self.t.timeline(self.ids["Project Atlas"])["items"]
        self.assertEqual([i["at"] for i in items], sorted(i["at"] for i in items))
        old = next(i for i in items if i.get("quote") == "Atlas runs on Snowflake for now")
        new = next(i for i in items if i["kind"] == "edge" and i["quote"] == "we decided to move Atlas to Databricks")
        self.assertEqual((old["state"], old["superseded_by"], old["supersede_reason"]),
                         (SUPERSEDED, new["id"], "Atlas moved from Snowflake to Databricks"))
        self.assertEqual(new["state"], "active")
        self.assertLess(items.index(old), items.index(new))
        kinds = {i["kind"] for i in items}
        self.assertEqual(kinds, {"edge", "fact", "task"})
        task = next(i for i in items if i["kind"] == "task")
        self.assertEqual((task["owner"], task["episode_id"]), ("Sam Ortiz", "ep-2026-09-01-1500"))

    def test_retracted_edge_is_marked_and_never_valid(self):
        quote = "Atlas runs on Snowflake for now"
        gone, episode = self.conn.execute("SELECT id, episode_id FROM edge WHERE quote = ?", (quote,)).fetchone()
        rest = [r[0] for r in self.conn.execute("SELECT id FROM edge WHERE episode_id = ? AND id != ?",
                                                (episode, gone))]
        self.store.retract_unwritten(episode, "edge", rest, reason="not in the new extraction")
        item = next(i for i in self.t.timeline(self.ids["Project Atlas"])["items"] if i.get("quote") == quote)
        self.assertEqual((item["state"], item["retract_reason"]), (RETRACTED, "not in the new extraction"))
        then = self.t.neighbors(self.ids["Project Atlas"], hops=1, relations=["uses"],
                                as_of="2026-09-10T00:00:00+00:00")
        self.assertEqual(self.names(then), [])

    def test_window(self):
        items = self.t.timeline(self.ids["Project Atlas"], since="2026-09-08T00:00:00+00:00",
                                until="2026-09-09T00:00:00+00:00")["items"]
        self.assertEqual([i["quote"] for i in items], ["Sam is helping Dana on Atlas"])


class TestMixedOffsets(unittest.TestCase):
    """Times written with a local offset (the recorder writes -04:00) and asked in UTC,
    or the other way round, compare by the moment they name, not as text."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = load_config(overlay=overlay(Path(tmp.name)))
        self.conn = db.connect(self.cfg)
        self.addCleanup(self.conn.close)
        s = self.store = Store(self.conn, self.cfg)
        self.t = Traverser(s)
        self.dana = s.upsert_person("dana@example.test", "Dana Reyes", source="outlook")
        atlas = s.upsert_entity("project", "Project Atlas", source="extract")
        beta = s.upsert_entity("project", "Project Beta", source="extract")
        # A local evening (00:00Z on the 2nd) and a UTC time two hours earlier in real time,
        # though "2026-09-01T20..." sorts before "2026-09-01T22..." as text.
        s.upsert_episode("A", transcript_path="a.md", sha256="a", meeting_start="2026-09-01T20:00:00-04:00")
        s.upsert_episode("B", transcript_path="b.md", sha256="b", meeting_start="2026-09-01T22:00:00+00:00")
        self.beta_edge = s.add_edge(src_entity_id=self.dana, dst_entity_id=beta, relation="works_on", quote="Beta",
                                    episode_id="B", transcript_text="Beta")
        self.atlas_edge = s.add_edge(src_entity_id=self.dana, dst_entity_id=atlas, relation="works_on",
                                     quote="Atlas", episode_id="A", transcript_text="Atlas")

    def reached(self, **when) -> list[str]:
        return [r["entity"]["name"] for r in self.t.neighbors(self.dana, hops=1, **when)["results"]]

    def test_stored_in_utc(self):
        self.assertEqual(self.store.episode("A")["meeting_start"], "2026-09-02T00:00:00.000000+00:00")
        self.assertEqual(self.store.get(self.atlas_edge)["valid_from"], "2026-09-02T00:00:00.000000+00:00")
        self.assertEqual(db.utc_time("2026-09-02T00:00:00Z"), db.utc_now(datetime(2026, 9, 2, tzinfo=timezone.utc)))

    def test_as_of_and_supersede(self):
        self.assertEqual(self.reached(as_of="2026-09-01T23:00:00+00:00"), ["Project Beta"])
        self.store.supersede_edge(self.beta_edge, self.atlas_edge, reason="moved")
        self.assertEqual(self.store.get(self.beta_edge)["valid_to"], "2026-09-02T00:00:00.000000+00:00")
        self.assertEqual(self.reached(as_of="2026-09-01T23:00:00+00:00"), ["Project Beta"])
        self.assertEqual(self.reached(as_of="2026-09-01T21:00:00-04:00"), ["Project Atlas"])    # 01:00Z on the 2nd

    def test_timeline_order_and_since(self):
        self.assertEqual([i["dst_name"] for i in self.t.timeline(self.dana)["items"]], ["Project Beta", "Project Atlas"])
        later = self.t.timeline(self.dana, since="2026-09-01T23:00:00+00:00")["items"]
        self.assertEqual([i["dst_name"] for i in later], ["Project Atlas"])
        self.assertEqual(sorted(self.reached(since="2026-09-01T19:30:00-04:00")),        # 23:30Z: both open
                         ["Project Atlas", "Project Beta"])

    def test_bare_date_is_its_midnight_utc(self):
        self.assertIn("Project Atlas", self.reached(as_of="2026-09-02"))
        self.assertNotIn("Project Atlas", self.reached(until="2026-09-01"))

    def test_garbage_time_is_refused(self):
        with self.assertRaises(ValueError):
            self.reached(as_of="garbage")
        with self.assertRaises(ValueError):
            self.store.upsert_episode("C", transcript_path="c.md", sha256="c", meeting_start="next Tuesday")
        reply = Server(self.conn, self.cfg).call_tool("neighbors", {"entity_id": self.dana, "as_of": "garbage"})
        self.assertTrue(reply["isError"])
        self.assertIn("ISO-8601", reply["content"][0]["text"])


class TestMultiHopFixture(GraphCase):
    """Each fixture question answered through the MCP tool layer, step by step; the
    tools must return the evidence chain the question needs (seeds C.6 retrieval QA)."""

    def setUp(self):
        super().setUp()
        self.server = Server(self.conn, self.cfg)

    def call(self, name: str, args: dict) -> dict:
        args = {k: self.ids.get(v, v) if k in _ENTITY_ARGS else v for k, v in args.items()}
        result = self.server.call_tool(name, args)
        self.assertFalse(result["isError"], result)
        return json.loads(result["content"][0]["text"])

    def test_every_question_has_its_evidence_chain(self):
        questions = self.data["questions"]
        self.assertGreaterEqual(len(questions), 4)
        for q in questions:
            with self.subTest(q["question"]):
                for step in q["steps"]:
                    self.check(step["tool"], self.call(step["tool"], step["args"]), step["expect"])

    def check(self, tool: str, out, expect: dict) -> None:
        if "finds" in expect:
            self.assertIn(expect["finds"], [c.get("name") for c in out])
        if "reaches" in expect:
            reached = {r["entity"]["name"]: r for r in out["results"]}
            self.assertIn(expect["reaches"], reached)
            self.assertEqual(quotes(reached[expect["reaches"]]["chain"]), expect["chain"])
            for edge in reached[expect["reaches"]]["chain"]:
                self.assertEqual(edge["provenance"], "EXTRACTED")
        for name in expect.get("not_reaches", []):
            self.assertNotIn(name, [r["entity"]["name"] for r in out["results"]])
        if "paths" in expect:
            self.assertEqual(sorted(quotes(p["edges"]) for p in out["paths"]), sorted(expect["paths"]))
        if "task" in expect:
            tasks = [i for i in out["items"] if i["kind"] == "task"]
            self.assertIn((expect["task"]["owner"], expect["task"]["quote"]), [(t["owner"], t["quote"]) for t in tasks])
        if "sequence" in expect:
            seen = [[i["kind"], i["quote"], i.get("state")] for i in out["items"]]
            at = [seen.index(item) for item in expect["sequence"]]
            self.assertEqual(at, sorted(at))
        if "span" in expect:
            self.assertIn(expect["span"], [s["quote"] for s in out["spans"]])


# ---- entity resolution ---------------------------------------------------------------------

class FakeAsk:
    """Stands in for a model: records each call and answers with a fixed decision."""

    def __init__(self, decision: str = SAME):
        self.decision = decision
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, role: str, prompt: str, schema: dict) -> dict:
        self.calls.append((role, prompt, schema))
        return {"decision": self.decision, "reason": f"fake says {self.decision}"}


def no_ask(role: str, prompt: str, schema: dict) -> dict:
    raise AssertionError("the model must not be asked")


class ResolveCase(unittest.TestCase):
    EP = "ep-er"
    TEXT = "Chris Lee owns the deck. Priya Shaw runs the budget. Jamie Doe is on Atlas."

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = load_config(overlay=overlay(Path(tmp.name)))
        self.conn = db.connect(self.cfg)
        self.addCleanup(self.conn.close)
        self.store = Store(self.conn, self.cfg)
        self.state = State(self.conn, self.cfg)
        self.r = Resolver(self.store, self.state)
        self.run_id = self.state.begin_run("maintain")
        self.store.upsert_episode(self.EP, transcript_path="t.md", sha256="ef" * 32,
                                  meeting_start="2026-09-01T15:00:00+00:00")

    def pairs(self) -> set[frozenset]:
        return {frozenset((c.a, c.b)) for c in self.r.candidates()}

    def candidate(self, a: str, b: str) -> Candidate:
        return next(c for c in self.r.candidates() if {c.a, c.b} == {a, b})


class TestEditDistance(unittest.TestCase):
    def test_distances(self):
        self.assertEqual(edit_distance("priya shah", "priya shaw"), 1)
        self.assertEqual(edit_distance("kitten", "sitting"), 3)
        self.assertEqual(edit_distance("", "abc"), 3)
        self.assertEqual(edit_distance("same", "same"), 0)
        self.assertEqual(edit_distance("chris lee", "chris park", limit=2), 3)    # cut off above the limit
        self.assertEqual(edit_distance("a", "abcdef", limit=2), 3)


class TestCandidates(ResolveCase):
    def test_two_different_chris_never_merge(self):
        lee = self.store.upsert_person("chris.lee@example.test", "Chris Lee", source="outlook")
        park = self.store.upsert_person("chris.park@example.test", "Chris Park", source="outlook")
        for person in (lee, park):
            self.store.add_alias(person, "Chris", source="outlook")
        self.assertEqual(self.pairs(), set())                  # a shared first name is no signal
        self.assertIsNone(self.store.mention_person("Chris", source="extract", episode_id=self.EP))
        summary = self.r.run(self.run_id, no_ask)
        self.assertEqual((summary["merged"], summary["attached"]), ([], 0))
        self.assertEqual(len(self.store.unresolved_aliases()), 1)     # two fit, so it stays unresolved
        self.assertIsNone(self.store.entity(lee)["merged_into"])
        self.assertIsNone(self.store.entity(park)["merged_into"])

    def test_near_namesakes_with_emails_go_to_the_model_not_a_rule(self):
        lee = self.store.upsert_person("chris.lee@example.test", "Chris Lee", source="outlook")
        lea = self.store.upsert_person("c.lea@other.test", "Chris Lea", source="outlook")
        c = self.candidate(lee, lea)
        self.assertFalse(c.certain)
        ask = FakeAsk("different")
        summary = self.r.run(self.run_id, ask)
        self.assertEqual((len(ask.calls), summary["merged"], summary["different"]), (1, [], 1))
        self.assertIsNone(self.store.entity(lea)["merged_into"])

    def test_names_that_differ_in_number_are_not_near(self):
        r = self.r
        self.assertEqual(r._near(["sprint 12"], ["sprint 13"]), [])
        self.assertEqual(r._near(["q3 planning"], ["q4 planning"]), [])
        self.assertTrue(r._near(["q3 planning"], ["q3 plannng"]))

    def test_edit_distance_variant_is_a_candidate_and_merges_on_same(self):
        shah = self.store.upsert_person("priya.shah@example.test", "Priya Shah", source="outlook")
        shaw = self.store.mention_person("Priya Shaw", source="extract", episode_id=self.EP)
        self.store.add_fact(type_="fact", text="Runs the budget", quote="Priya Shaw runs the budget",
                            episode_id=self.EP, transcript_text=self.TEXT, subject_entity_id=shaw)
        c = self.candidate(shah, shaw)
        self.assertFalse(c.certain)
        self.assertTrue(any(s.startswith("near names") for s in c.signals), c.signals)
        ask = FakeAsk(SAME)
        summary = self.r.run(self.run_id, ask)
        role, prompt, schema = ask.calls[0]
        self.assertEqual(role, self.cfg["kg"]["models"]["resolve"])
        self.assertEqual(schema["properties"]["decision"]["enum"], ["same", "different", "unsure"])
        for text in ("Priya Shah", "Priya Shaw", "priya.shah@example.test", "Priya Shaw runs the budget"):
            self.assertIn(text, prompt)
        self.assertNotIn("{{", prompt)
        self.assertEqual(len(summary["merged"]), 1)
        self.assertEqual(self.store.entity(shaw)["merged_into"], shah)     # the email-keyed person survives
        record = json.loads(self.conn.execute("SELECT decision FROM entity_merge").fetchone()[0])
        self.assertEqual((record["decision"], record["basis"]), (SAME, "model"))

    def test_unsure_is_escalated_and_bad_output_refused(self):
        shah = self.store.upsert_person("priya.shah@example.test", "Priya Shah", source="outlook")
        self.store.mention_person("Priya Shaw", source="extract", episode_id=self.EP)
        with self.assertRaises(StoreError):
            self.r.run(self.run_id, lambda role, prompt, schema: {"decision": "maybe"})
        summary = self.r.run(self.run_id, FakeAsk("unsure"))
        self.assertEqual((summary["merged"], len(summary["unsure"])), ([], 1))
        log = {r["check_name"]: r for r in self.state.maintenance(self.run_id)}
        self.assertEqual(log["entity_resolution"]["escalated"], 1)
        self.assertEqual(self.r.run(self.run_id, no_ask)["skipped"], 1)          # escalated once, not every pass
        self.assertIsNone(self.store.entity(shah)["merged_into"])

    def test_same_type_only(self):
        self.store.upsert_entity("project", "Atlas", source="extract")
        self.store.upsert_entity("system", "Atlas", source="extract")
        self.assertEqual(self.pairs(), set())

    def test_full_name_person_merges_into_the_email_person_by_rule(self):
        named = self.store.mention_person("Jamie Doe", source="extract", episode_id=self.EP)
        keyed = self.store.upsert_person("jamie.doe@example.test", "Jamie Doe", source="outlook", episode_id=self.EP)
        self.assertNotEqual(named, keyed)
        self.assertTrue(self.candidate(named, keyed).certain)        # Jamie Doe attended the call that named them
        self.r.run(self.run_id, no_ask)
        self.assertEqual(self.store.entity(named)["merged_into"], keyed)

    def test_shared_full_name_without_a_shared_call_goes_to_the_model(self):
        named = self.store.mention_person("Jamie Doe", source="extract", episode_id=self.EP)
        self.store.upsert_episode("ep-other", transcript_path="o.md", sha256="0" * 64,
                                  meeting_start="2026-09-02T15:00:00+00:00")
        keyed = self.store.upsert_person("jamie.doe@example.test", "Jamie Doe", source="outlook",
                                         episode_id="ep-other")
        self.assertFalse(self.candidate(named, keyed).certain)
        ask = FakeAsk("different")
        self.r.run(self.run_id, ask)
        self.assertEqual(len(ask.calls), 1)
        self.assertIsNone(self.store.entity(named)["merged_into"])

    def test_a_decided_pair_is_not_asked_again_until_a_side_changes(self):
        shah = self.store.upsert_person("priya.shah@example.test", "Priya Shah", source="outlook")
        shaw = self.store.upsert_person("priya.shaw@other.test", "Priya Shaw", source="outlook")
        ask = FakeAsk("different")
        first = self.r.run(self.run_id, ask)
        second = self.r.run(self.run_id, ask)
        self.assertEqual((first["different"], second["skipped"], len(ask.calls)), (1, 1, 1))
        self.store.add_alias(shaw, "Priya K Shaw", source="outlook")          # renamed: ask again
        third = self.r.run(self.run_id, ask)
        self.assertEqual((third["different"], len(ask.calls)), (1, 2))
        self.assertEqual(self.r.run(self.run_id, ask)["skipped"], 1)
        self.assertEqual(len(ask.calls), 2)
        self.assertIsNone(self.store.entity(shah)["merged_into"])

    def test_candidates_are_blocked_not_all_pairs(self):
        rng = random.Random(4000)
        letters = "abcdefghijklmnopqrstuvwxyz"
        word = lambda n: "".join(rng.choice(letters) for _ in range(n))
        now = db.utc_now()
        people = [(f"{i:040x}", f"{word(6)} {word(8)}", f"p{i}@example.test") for i in range(4000)]
        with db.transaction(self.conn):
            self.conn.executemany("INSERT INTO entity (id, type, canonical_name, canonical_key, created_at)"
                                  " VALUES (?, 'person', ?, ?, ?)", [(i, n, e, now) for i, n, e in people])
            self.conn.executemany("INSERT INTO alias (id, entity_id, alias, alias_key, source, created_at)"
                                  " VALUES (?, ?, ?, ?, 'outlook', ?)", [("a" + i, i, n, n, now) for i, n, _ in people])
        variant = self.store.upsert_person("x@example.test", people[0][1][:-1] + "q", source="outlook")
        started = time.perf_counter()
        found = self.r.candidates()
        self.assertLess(time.perf_counter() - started, 10)               # all pairs: about 130 s
        self.assertIn(frozenset((people[0][0], variant)), {frozenset((c.a, c.b)) for c in found})

    def test_a_first_name_attached_once_does_not_stick_after_a_namesake(self):
        self.assertIsNone(self.store.mention_person("Chris", source="extract", episode_id=self.EP))
        lee = self.store.upsert_person("chris.lee@example.test", "Chris Lee", source="outlook")
        self.assertEqual([e for _, e in self.r.attach_unresolved(self.run_id)], [lee])
        self.assertEqual(self.store.mention_person("Chris", source="extract", episode_id=self.EP), lee)   # unique
        self.store.upsert_person("chris.park@example.test", "Chris Park", source="outlook")
        self.store.upsert_episode("ep-later", transcript_path="l.md", sha256="1" * 64,
                                  meeting_start="2026-09-10T15:00:00+00:00")
        self.assertIsNone(self.store.mention_person("Chris", source="extract", episode_id="ep-later"))
        self.assertEqual(self.r.attach_unresolved(self.run_id), [])
        self.assertEqual(len(self.store.unresolved_aliases()), 1)

    def test_first_name_only_stays_unresolved_until_one_person_fits(self):
        self.assertIsNone(self.store.mention_person("Dana", source="extract", episode_id=self.EP))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0], 0)
        self.assertEqual(self.r.attach_unresolved(self.run_id), [])
        dana = self.store.upsert_person("dana.reyes@example.test", "Dana Reyes", source="outlook")
        attached = self.r.attach_unresolved(self.run_id)
        self.assertEqual([e for _, e in attached], [dana])
        self.assertEqual(self.store.unresolved_aliases(), [])
        self.assertIn("Dana", self.store.aliases(dana))

    def test_first_name_with_two_fits_stays_unresolved(self):
        self.store.upsert_person("dana.reyes@example.test", "Dana Reyes", source="outlook")
        self.store.upsert_person("dana.kim@example.test", "Dana Kim", source="outlook")
        self.store.mention_person("Dana", source="extract", episode_id=self.EP)
        self.assertEqual(self.r.attach_unresolved(self.run_id), [])
        self.assertEqual(len(self.store.unresolved_aliases()), 1)

    def test_role_must_be_in_the_roster(self):
        self.cfg["kg"]["models"]["resolve"] = "no_such_role"
        with self.assertRaises(StoreError):
            Resolver(self.store, self.state)


class TestMerge(ResolveCase):
    def setUp(self):
        super().setUp()
        s = self.store
        self.keep = s.upsert_person("jamie.doe@example.test", "Jamie Doe", source="outlook")
        self.drop = s.upsert_person("jdoe@old.test", "J. Doe", source="outlook")
        s.add_alias(self.drop, "Jamie Doe", source="extract")
        s.add_alias(self.drop, "Jamie", source="extract")
        self.atlas = s.upsert_entity("project", "Atlas", source="extract")
        self.deck = s.upsert_entity("project", "Deck", source="extract")
        self.e1 = s.add_edge(src_entity_id=self.drop, dst_entity_id=self.atlas, relation="works_on",
                             quote="Jamie Doe is on Atlas", episode_id=self.EP, transcript_text=self.TEXT)
        self.e2 = s.add_edge(src_entity_id=self.keep, dst_entity_id=self.deck, relation="works_on", quote="deck",
                             episode_id=self.EP, transcript_text=self.TEXT)
        self.e3 = s.add_edge(src_entity_id=self.keep, dst_entity_id=self.drop, relation="related_to", quote="x",
                             episode_id=self.EP, transcript_text=self.TEXT)
        self.f1 = s.add_fact(type_="fact", text="On Atlas", quote="Jamie Doe is on Atlas", episode_id=self.EP,
                             transcript_text=self.TEXT, subject_entity_id=self.drop)
        both = {"id": task_id(self.EP, "q1"), "owner": "Jamie Doe", "owner_basis": "others", "action": "a",
                "due": None, "due_basis": "not_stated", "context": "", "quote": "q1",
                "source": {"episode": self.EP, "start": None}, "confidence": 0.5, "status": "captured",
                "tools_allowed": []}
        s.add_task(both, entity_ids=[self.keep, self.drop])
        s.add_task({**both, "id": task_id(self.EP, "q2"), "quote": "q2"}, entity_ids=[self.drop])

    def test_merge_repoints_and_unmerge_restores_exactly(self):
        before = snapshot(self.conn)
        merge_id = self.r.merge(self.keep, self.drop, reason="same person, old email", run_id=self.run_id)
        self.assertEqual(self.store.entity(self.drop)["merged_into"], self.keep)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM edge WHERE ? IN (src_entity_id, dst_entity_id)",
                                           (self.drop,)).fetchone()[0], 0)
        self.assertEqual(self.store.get(self.f1)["subject_entity_id"], self.keep)
        self.assertEqual(self.store.task_entities(task_id(self.EP, "q1")), [self.keep])
        self.assertEqual(self.store.task_entities(task_id(self.EP, "q2")), [self.keep])
        self.assertIn("J. Doe", self.store.aliases(self.keep))            # the dropped name is kept
        self.assertEqual(self.store.search("J. Doe")[0]["id"], self.keep)
        self.assertEqual(self.store.mention_person("Jamie", source="extract"), self.keep)
        log = [r["check_name"] for r in self.state.maintenance(self.run_id)]
        self.assertIn("entity_merge", log)

        self.assertTrue(self.r.unmerge(merge_id, run_id=self.run_id))
        self.assertEqual(snapshot(self.conn), before)
        self.assertFalse(self.r.unmerge(merge_id, run_id=self.run_id))      # already undone: no change
        self.assertEqual(snapshot(self.conn), before)
        self.assertIn("entity_unmerge", [r["check_name"] for r in self.state.maintenance(self.run_id)])

    def test_rewriting_an_episode_after_a_merge_adds_no_second_edge(self):
        before = snapshot(self.conn)
        merge_id = self.r.merge(self.keep, self.drop, reason="same", run_id=self.run_id)
        count = lambda: self.conn.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
        edges = count()
        for src in (self.keep, self.drop):                 # the survivor, or the merged-away id itself
            again = self.store.add_edge(src_entity_id=src, dst_entity_id=self.atlas, relation="works_on",
                                        quote="Jamie Doe is on Atlas", episode_id=self.EP, transcript_text=self.TEXT)
            self.assertEqual(again, self.e1)
        self.assertEqual(self.store.add_edge(src_entity_id=self.keep, dst_entity_id=self.keep, relation="related_to",
                                             quote="x", episode_id=self.EP, transcript_text=self.TEXT), self.e3)
        self.assertEqual(count(), edges)
        hop = Traverser(self.store).neighbors(self.keep, hops=1)
        self.assertEqual([r["chain"][0]["parallel"] for r in hop["results"] if r["entity"]["id"] == self.atlas], [0])
        self.assertTrue(self.r.unmerge(merge_id, run_id=self.run_id))
        self.assertEqual(snapshot(self.conn), before)             # no residue

    def test_merge_is_idempotent(self):
        first = self.r.merge(self.keep, self.drop, reason="same", run_id=self.run_id)
        after = snapshot(self.conn)
        self.assertEqual(self.r.merge(self.keep, self.drop, reason="same", run_id=self.run_id), first)
        self.assertEqual(snapshot(self.conn), after)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entity_merge").fetchone()[0], 1)

    def test_merge_is_one_transaction(self):
        before = snapshot(self.conn)
        finished = self.state.begin_run("other")
        self.state.finish_run(finished, processed=0, eligible=0, backlog=0)
        with self.assertRaises(Exception):                 # logging needs a running run: all rolls back
            self.r.merge(self.keep, self.drop, reason="same", run_id=finished)
        self.assertEqual(snapshot(self.conn), before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entity_merge").fetchone()[0], 0)

    def test_refusals(self):
        for keep, drop in ((self.keep, self.keep), (self.keep, self.atlas), (self.keep, "nope")):
            with self.assertRaises(StoreError):
                self.r.merge(keep, drop, reason="x", run_id=self.run_id)
        with self.assertRaises(StoreError):
            self.r.merge(self.keep, self.drop, reason=" ", run_id=self.run_id)
        with self.assertRaises(StoreError):
            self.r.unmerge("no-such-merge", run_id=self.run_id)

    def test_writes_after_a_merge_follow_the_survivor(self):
        self.r.merge(self.keep, self.drop, reason="same", run_id=self.run_id)
        self.assertEqual(self.store.upsert_person("jdoe@old.test", "J. Doe", source="outlook"), self.keep)
        self.assertEqual(self.store.mention_person("Jamie Doe", source="extract"), self.keep)
        self.assertEqual(self.store.live_id(self.drop), self.keep)
        # Traversal from the merged-away id starts at the survivor, and says so.
        start = Traverser(self.store).neighbors(self.drop, hops=1)["start"]
        self.assertEqual((start["id"], start["resolved_from"]), (self.keep, self.drop))

    def test_unmerge_is_last_in_first_out(self):
        third = self.store.upsert_person("jamie@third.test", "Jamie D", source="outlook")
        first = self.r.merge(self.keep, self.drop, reason="same", run_id=self.run_id)
        second = self.r.merge(third, self.keep, reason="same", run_id=self.run_id)
        self.assertEqual(self.store.live_id(self.drop), third)
        with self.assertRaises(StoreError):
            self.r.unmerge(first, run_id=self.run_id)
        self.assertTrue(self.r.unmerge(second, run_id=self.run_id))
        self.assertTrue(self.r.unmerge(first, run_id=self.run_id))
        self.assertIsNone(self.store.entity(self.drop)["merged_into"])
        self.assertIsNone(self.store.entity(self.keep)["merged_into"])


class TestAliasIndexById(ResolveCase):
    def test_two_alias_rows_with_one_text_keep_separate_index_rows(self):
        # Dana's own "Dana" alias, plus a mention recorded before she was known, then attached:
        # two alias rows with one text on one entity (the 0002 triggers shared one index row).
        dana = self.store.upsert_person("dana.reyes@example.test", "Dana Reyes", source="outlook")
        self.store.add_alias(dana, "Dana", source="outlook")
        self.store.add_alias(None, "Dana", source="extract", episode_id=self.EP)
        self.r.attach_unresolved(self.run_id)
        rows = [r[0] for r in self.conn.execute("SELECT id FROM alias WHERE alias = 'Dana' AND entity_id = ?",
                                                (dana,))]
        self.assertEqual(len(rows), 2)
        self.conn.execute("DELETE FROM alias WHERE id = ?", (rows[0],))
        self.assertEqual([c["id"] for c in self.store.search("Dana")], [dana])
        indexed = self.conn.execute("SELECT COUNT(*) FROM search_fts WHERE kind = 'alias' AND ref_id = ?",
                                    (rows[1],)).fetchone()[0]
        self.assertEqual(indexed, 1)


# ---- MCP server ---------------------------------------------------------------------------

class TestReadOnly(GraphCase):
    def test_readonly_connection_refuses_writes(self):
        ro = db.connect_readonly(self.cfg)
        self.addCleanup(ro.close)
        self.assertEqual(ro.execute("PRAGMA query_only").fetchone()[0], 1)
        for sql in ("INSERT INTO episode (id, transcript_path, sha256, recorded_at) VALUES ('x', 'p', 's', 'now')",
                    "UPDATE entity SET canonical_name = 'x'", "DELETE FROM edge"):
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute(sql)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("PRAGMA query_only = OFF")
            ro.execute("DELETE FROM edge")                         # mode=ro holds even then
        self.assertEqual(Store(ro, self.cfg).search("Atlas")[0]["name"], "Project Atlas")

    def test_readonly_refuses_a_missing_or_other_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(overlay=overlay(Path(tmp)))
            with self.assertRaises(db.MigrationError):
                db.connect_readonly(cfg)
        self.conn.execute("INSERT INTO schema_version (version, name, applied_at) VALUES (9999, 'future', 'x')")
        with self.assertRaises(db.MigrationError):
            db.connect_readonly(self.cfg)


class TestServerInProcess(GraphCase):
    def setUp(self):
        super().setUp()
        self.server = Server(self.conn, self.cfg)

    def rpc(self, method: str, params=None, msg_id=1):
        message = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            message["params"] = params
        return self.server.handle_line(json.dumps(message).encode("utf-8"))

    def test_tools_list_tells_claude_how_to_chain(self):
        tools = {t["name"]: t for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertEqual(sorted(tools), ["get", "neighbors", "paths", "search", "source", "timeline"])
        for name, tool in tools.items():
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertGreater(len(tool["description"]), 80, name)
        self.assertIn("search", tools["neighbors"]["description"] + tools["search"]["description"])
        self.assertIn("source", tools["neighbors"]["description"])
        self.assertIn("neighbors", tools["search"]["description"])
        self.assertIn(str(self.cfg["kg"]["traverse"]["max_hops"]), tools["neighbors"]["description"])
        relations = tools["neighbors"]["inputSchema"]["properties"]["relations"]["items"]["enum"]
        self.assertEqual(set(relations), set(self.store.relations))

    def test_errors(self):
        self.assertEqual(self.server.handle_line(b"{not json")["error"]["code"], PARSE_ERROR)
        self.assertEqual(self.rpc("no/such")["error"]["code"], METHOD_NOT_FOUND)
        self.assertEqual(self.rpc("tools/call", {"name": "drop_table"})["error"]["code"], INVALID_PARAMS)
        bad = self.rpc("tools/call", {"name": "neighbors", "arguments": {"entity_id": "x", "hops": "two"}})
        self.assertTrue(bad["result"]["isError"])
        missing = self.rpc("tools/call", {"name": "get", "arguments": {"id": "nope"}})
        self.assertTrue(missing["result"]["isError"])
        self.assertIsNone(self.server.handle_line(b'{"jsonrpc": "2.0", "method": "notifications/initialized"}'))
        self.assertIsNone(self.server.handle_line(b"   \n"))
        self.assertEqual(self.server.handle_line(b"[]")["error"]["code"], -32600)

    def test_internal_error_keeps_serving(self):
        self.server.calls["search"] = lambda a: 1 / 0
        with self.assertLogs("kg.mcp", level="ERROR"):
            reply = self.rpc("tools/call", {"name": "search", "arguments": {"query": "x"}})
        self.assertEqual(reply["error"]["code"], INTERNAL_ERROR)
        self.assertEqual(self.rpc("ping", msg_id=2)["result"], {})


class TestServerSubprocess(GraphCase):
    """The real `python -m kg.mcp` over stdio, on a temp database."""

    def test_stdio_session(self):
        overlay_path = self.root / "overlay.yaml"
        overlay_path.write_text(yaml.safe_dump(self.ov), encoding="utf-8")
        atlas, dana, sam = self.ids["Project Atlas"], self.ids["Dana Reyes"], self.ids["Sam Ortiz"]
        calls = [("search", {"query": "Atlas"}), ("get", {"id": atlas}),
                 ("source", {"episode_id": "ep-2026-09-01-1500"}), ("neighbors", {"entity_id": dana, "hops": 2}),
                 ("paths", {"src_id": sam, "dst_id": dana}), ("timeline", {"entity_id": atlas})]
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "test", "version": "0"}}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        lines += [{"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": n, "arguments": a}}
                  for i, (n, a) in enumerate(calls)]
        payload = "\n".join(json.dumps(m) for m in lines[:3]) + "\n{this is not json\n"
        payload += "\n".join(json.dumps(m) for m in lines[3:]) + "\n"
        payload += json.dumps({"jsonrpc": "2.0", "id": 99, "method": "ping"}) + "\n"
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = list(self.conn.iterdump())
        proc = subprocess.run([sys.executable, "-m", "kg.mcp", "--config", str(overlay_path)], cwd=REPO,
                              input=payload.encode("utf-8"), capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        replies = [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines()]
        by_id = {r["id"]: r for r in replies if r["id"] is not None}
        self.assertEqual(len(replies), len(lines) - 1 + 2)     # no reply to the notification; one to the bad line
        init = by_id[1]["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["capabilities"])
        self.assertEqual(len(by_id[2]["result"]["tools"]), 6)
        parse_errors = [r for r in replies if r["id"] is None]
        self.assertEqual([r["error"]["code"] for r in parse_errors], [PARSE_ERROR])
        for i, (name, _) in enumerate(calls):
            result = by_id[10 + i]["result"]
            self.assertFalse(result["isError"], (name, result))
            json.loads(result["content"][0]["text"])
        self.assertEqual(by_id[99]["result"], {})              # still serving after the bad line
        self.assertEqual(list(self.conn.iterdump()), before)   # answering changed nothing
        self.assertEqual(proc.stdout.decode("utf-8").count("\n"), len(replies))   # stdout: protocol only

    def test_missing_database_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            ov = overlay(Path(tmp))
            path = Path(tmp) / "overlay.yaml"
            path.write_text(yaml.safe_dump(ov), encoding="utf-8")
            proc = subprocess.run([sys.executable, "-m", "kg.mcp", "--config", str(path)], cwd=REPO,
                                  input=b"", capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, b"")
        self.assertIn(b"no database", proc.stderr)


if __name__ == "__main__":
    unittest.main()
