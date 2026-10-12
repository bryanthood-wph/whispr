"""kg/view.py: the neighbourhood viewer's JSON + SVG answers and its CLI contract.
No model calls; every database lives in a temp data dir (as in test_kg_graph.py)."""

from __future__ import annotations

import copy
import io
import json
import math
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import yaml

from kg import db
from kg.resolve import Resolver
from kg.state import State
from kg.store import Store
from kg.view import AMBIGUOUS, CUT_MAX_NODES, CUT_SVG, ERROR, NO_DATABASE, NOT_FOUND, OK, SVG_NS, USAGE, View, \
    ViewError, host_length, main
from pipeline.config import config_file, load_config, read_yaml
from pipeline_helpers import overlay
from test_kg_graph import GraphCase

REPO = Path(__file__).resolve().parent.parent
SVG = "{" + SVG_NS + "}"
# What a self-contained, script-free SVG may hold, and what it must never.
ALLOWED_TAGS = {SVG + t for t in ("svg", "title", "style", "g", "line", "circle", "text")}
FORBIDDEN = (re.compile(r"<script", re.I), re.compile(r"\son[a-z]+\s*=", re.I), re.compile(r"href", re.I),
             re.compile(r"url\(", re.I), re.compile(r"@import", re.I), re.compile(r"<foreignObject", re.I))
NODE_KEYS = {"id", "name", "type", "depth", "x", "y", "route_only"}
EDGE_KEYS = {"id", "src", "dst", "relation", "quote", "episode", "episode_date", "valid_from", "provenance", "state"}
ANSWER_KEYS = {"center", "hops", "hops_requested", "reached", "offset", "next_offset", "truncated", "truncated_by",
               "nodes", "edges", "svg", "alt", "text"}


def with_view(cfg: dict, **view) -> dict:
    """A copy of cfg with kg.view keys replaced."""
    cfg = copy.deepcopy(cfg)
    cfg["kg"]["view"].update(view)
    return cfg


class ViewCase(GraphCase):
    def setUp(self):
        super().setUp()
        self.view = View(self.store, self.cfg)

    def assert_safe_svg(self, svg: str, cap: int) -> ET.Element:
        self.assertLessEqual(len(svg), cap)
        for pattern in FORBIDDEN:
            self.assertIsNone(pattern.search(svg), pattern.pattern)
        root = ET.fromstring(svg)                       # well-formed XML, or this raises
        self.assertEqual(root.tag, SVG + "svg")
        for el in root.iter():
            self.assertIn(el.tag, ALLOWED_TAGS)
            for name in el.attrib:
                self.assertFalse(name.lower().startswith("on"), name)
        return root

    def write_overlay(self, ov: dict) -> Path:
        path = self.root / "overlay.yaml"
        path.write_text(yaml.safe_dump(ov), encoding="utf-8")
        return path

    def cli(self, *argv: str, ov: dict = None) -> tuple[int, dict]:
        out = io.BytesIO()
        code = main([*argv, "--config", str(self.write_overlay(ov or self.ov))], stdout=out)
        lines = out.getvalue().decode("utf-8").splitlines()
        self.assertEqual(len(lines), 1, lines)          # one JSON object, always
        return code, json.loads(lines[0])


class TestSearch(ViewCase):
    def test_entity_cards_only(self):
        answer = self.view.search("Atlas")
        self.assertEqual(answer["results"], [{"id": self.ids["Project Atlas"], "name": "Project Atlas",
                                              "type": "project"}])
        for card in self.view.search("Snowflake Databricks Atlas")["results"]:
            self.assertEqual(set(card), {"id", "name", "type"})
        self.assertLessEqual(len(self.view.search("Atlas Sam Dana Lee Priya")["results"]),
                             self.cfg["kg"]["search_cards"])

    def test_cli(self):
        code, answer = self.cli("search", "Project", "Atlas")
        self.assertEqual(code, OK)
        self.assertEqual(answer["query"], "Project Atlas")
        self.assertEqual(answer["results"][0]["id"], self.ids["Project Atlas"])


class TestNeighborhood(ViewCase):
    def test_shape(self):
        answer = self.view.neighborhood(self.ids["Dana Reyes"], hops=3)
        self.assertEqual(set(answer), ANSWER_KEYS)
        self.assertEqual(answer["center"], {"id": self.ids["Dana Reyes"], "type": "person", "name": "Dana Reyes"})
        self.assertEqual(answer["hops"], 3)
        self.assertFalse(answer["truncated"])
        self.assertIsNone(answer["next_offset"])
        nodes = {n["id"]: n for n in answer["nodes"]}
        self.assertEqual(sorted(n["name"] for n in answer["nodes"]),
                         ["Dana Reyes", "Data Platform", "Databricks", "Lee Wong", "Project Atlas", "Sam Ortiz"])
        for node in answer["nodes"]:
            self.assertEqual(set(node), NODE_KEYS)
            self.assertIsInstance(node["x"], float)
        self.assertEqual(answer["nodes"][0]["depth"], 0)
        self.assertEqual(len(answer["edges"]), len(answer["nodes"]) - 1)        # a tree
        for edge in answer["edges"]:
            self.assertEqual(set(edge), EDGE_KEYS)
            self.assertIn(edge["src"], nodes)
            self.assertIn(edge["dst"], nodes)
        lee_edge = next(e for e in answer["edges"] if self.ids["Lee Wong"] in (e["src"], e["dst"]))
        self.assertEqual((lee_edge["relation"], lee_edge["quote"], lee_edge["episode_date"], lee_edge["provenance"]),
                         ("responsible_for", "Lee runs the Databricks workspace", "2026-09-15", "EXTRACTED"))
        self.assertEqual(nodes[self.ids["Lee Wong"]]["depth"], 3)
        root = self.assert_safe_svg(answer["svg"], self.cfg["kg"]["view"]["svg_max_chars"])
        self.assertEqual(root.find(SVG + "title").text, answer["alt"])
        self.assertEqual(len(root.findall(f".//{SVG}circle")), len(answer["nodes"]))
        self.assertEqual(len(root.findall(f".//{SVG}line")), 2 * len(answer["edges"]))   # each with a hover target
        titles = [t.text for t in root.iter(SVG + "title")]
        self.assertTrue(any("Lee runs the Databricks workspace" in t and "2026-09-15" in t for t in titles))
        self.assertTrue(answer["alt"].startswith("Dana Reyes (person) and 5 neighbors within 3 hops"))
        self.assertEqual(answer["text"].splitlines()[0], "Dana Reyes (person)")
        self.assertIn('      <-responsible_for- Lee Wong (person)  "Lee runs the Databricks workspace"  2026-09-15',
                      answer["text"].splitlines())

    def test_palette_colours_nodes_by_type(self):
        answer = self.view.neighborhood(self.ids["Dana Reyes"], hops=3)
        palette = self.cfg["kg"]["view"]["palette"]
        fills = [c.get("fill") for c in ET.fromstring(answer["svg"]).iter(SVG + "circle")]
        self.assertEqual(sorted(fills), sorted(palette[n["type"]] for n in answer["nodes"]))

    def test_deterministic(self):
        first = self.view.neighborhood(self.ids["Dana Reyes"], hops=3)
        again = View(self.store, self.cfg).neighborhood(self.ids["Dana Reyes"], hops=3)
        self.assertEqual(first["svg"], again["svg"])
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(again, sort_keys=True))

    def test_by_exact_name_or_email(self):
        by_id = self.view.neighborhood(self.ids["Project Atlas"])
        self.assertEqual(self.view.neighborhood("project atlas")["svg"], by_id["svg"])
        self.assertEqual(self.view.neighborhood("dana.reyes@example.test")["center"]["id"], self.ids["Dana Reyes"])
        self.assertEqual(by_id["hops"], self.cfg["kg"]["view"]["default_hops"])

    def test_unknown_and_ambiguous(self):
        with self.assertRaises(ViewError) as cm:
            self.view.neighborhood("Nobody Here")
        self.assertEqual(cm.exception.code, NOT_FOUND)
        other = self.store.upsert_person("dana.r@other.test", "Dana Reyes", source="outlook")
        with self.assertRaises(ViewError) as cm:
            self.view.neighborhood("Dana Reyes")
        self.assertEqual(cm.exception.code, AMBIGUOUS)
        self.assertEqual(sorted(c["id"] for c in cm.exception.answer["candidates"]),
                         sorted([self.ids["Dana Reyes"], other]))

    def test_type_filter_draws_the_route(self):
        answer = self.view.neighborhood(self.ids["Dana Reyes"], hops=2, types=["system"])
        nodes = {n["name"]: n for n in answer["nodes"]}
        self.assertFalse(nodes["Databricks"]["route_only"])
        self.assertTrue(nodes["Project Atlas"]["route_only"])
        self.assertIn("[route]", answer["text"])
        self.assertIn("plus 1 entity on the routes", answer["alt"])
        self.assert_safe_svg(answer["svg"], self.cfg["kg"]["view"]["svg_max_chars"])

    def test_as_of_and_relations_pass_through(self):
        atlas = self.ids["Project Atlas"]
        then = self.view.neighborhood(atlas, hops=1, relations=["uses"], as_of="2026-09-10T00:00:00+00:00")
        self.assertEqual([n["name"] for n in then["nodes"][1:]], ["Snowflake"])
        self.assertEqual(then["edges"][0]["state"], "superseded")

    def test_cut_quote(self):
        cfg = with_view(self.cfg, quote_chars=8)
        answer = View(self.store, cfg).neighborhood(self.ids["Lee Wong"], hops=1)
        self.assertEqual(answer["edges"][0]["quote"], "Lee runs…")


class TestBounds(ViewCase):
    def test_max_nodes_pages_and_cuts(self):
        cfg = with_view(self.cfg, max_nodes=2)
        answer = View(self.store, cfg).neighborhood(self.ids["Dana Reyes"], hops=3)
        self.assertTrue(answer["truncated"])
        self.assertEqual(answer["truncated_by"], [CUT_MAX_NODES])
        self.assertEqual(answer["next_offset"], 2)
        self.assertEqual(len(answer["nodes"]), 3)
        self.assertIn("showing 1-2 of 5", answer["alt"])
        rest = View(self.store, cfg).neighborhood(self.ids["Dana Reyes"], hops=3, offset=2)
        self.assertEqual(rest["offset"], 2)
        firsts = {n["id"] for n in answer["nodes"][1:]}
        shown = {n["id"] for n in rest["nodes"][1:] if not n["route_only"]}
        self.assertFalse(firsts & shown)

    def test_svg_cap_drops_farthest_and_stays_valid(self):
        full = self.view.neighborhood(self.ids["Dana Reyes"], hops=3)
        cap = len(full["svg"]) - 1
        answer = View(self.store, with_view(self.cfg, svg_max_chars=cap)).neighborhood(self.ids["Dana Reyes"], hops=3)
        self.assertTrue(answer["truncated"])
        self.assertIn(CUT_SVG, answer["truncated_by"])
        self.assert_safe_svg(answer["svg"], cap)
        self.assertLess(len(answer["nodes"]), len(full["nodes"]))
        # The walk's order puts the farthest last, so the deepest entity goes first.
        self.assertNotIn("Lee Wong", [n["name"] for n in answer["nodes"]])
        self.assertEqual(answer["next_offset"], len(answer["nodes"]) - 1)

    def test_a_neighbour_too_big_to_draw_is_skipped_not_repeated(self):
        """Following next_offset always ends: one neighbour that cannot fit even alone is
        stepped over, and the small one is still reached."""
        ep = "ep-huge"
        self.store.upsert_episode(ep, transcript_path="huge.md", sha256="cd" * 32,
                                  meeting_start="2026-09-20T15:00:00+00:00")
        text = "Pat owns the huge topic\nPat owns the small topic"
        pat = self.store.upsert_person("pat@example.test", "Pat Example", source="outlook")
        def link(name: str, quote: str) -> None:
            project = self.store.upsert_entity("project", name, source="extract")
            self.store.add_edge(src_entity_id=pat, dst_entity_id=project, relation="responsible_for",
                                quote=quote, episode_id=ep, transcript_text=text)
        link("Small Topic", "Pat owns the small topic")
        cap = len(self.view.neighborhood(pat, hops=1)["svg"]) + 200     # room for the small one, not the huge
        link("Z" * 5000, "Pat owns the huge topic")
        view = View(self.store, with_view(self.cfg, svg_max_chars=cap))
        seen, offsets, offset = set(), [], 0
        while offset is not None and len(offsets) < 5:
            answer = view.neighborhood(pat, hops=1, offset=offset)
            offsets.append(offset)
            seen |= {n["name"] for n in answer["nodes"][1:]}
            offset = answer["next_offset"]
        self.assertIsNone(offset, offsets)
        self.assertEqual(len(offsets), len(set(offsets)), offsets)      # never the same page twice
        self.assertIn("Small Topic", seen)

    def test_cap_counts_utf16_units(self):
        """An emoji is two units to the host, so a drawing at the code-point cap is cut."""
        centre = self.store.upsert_entity("topic", "\U0001F600" * 30, source="extract")
        other = self.store.upsert_entity("topic", "\U0001F680" * 30, source="extract")
        self.store.upsert_episode("ep-emoji", transcript_path="emoji.md", sha256="ef" * 32,
                                  meeting_start="2026-09-20T15:00:00+00:00")
        self.store.add_edge(src_entity_id=centre, dst_entity_id=other, relation="related_to", quote="x relates",
                            episode_id="ep-emoji", transcript_text="x relates")
        full = self.view.neighborhood(centre, hops=1)
        cap = len(full["svg"])                                          # fits in code points, not in UTF-16
        self.assertGreater(host_length(full["svg"]), cap)
        try:
            answer = View(self.store, with_view(self.cfg, svg_max_chars=cap)).neighborhood(centre, hops=1)
        except ViewError as exc:                                        # the centre alone may not fit either
            self.assertEqual(exc.code, ERROR)
            return
        self.assertLessEqual(host_length(answer["svg"]), cap)
        self.assertIn(CUT_SVG, answer["truncated_by"])

    def test_offset_past_the_end_is_not_no_neighbours(self):
        answer = self.view.neighborhood(self.ids["Dana Reyes"], hops=2, offset=50)
        self.assertNotIn("no neighbors", answer["alt"])
        self.assertIn("none on this page", answer["alt"])

    def test_cap_too_small_for_the_centre_is_an_error(self):
        with self.assertRaises(ViewError) as cm:
            View(self.store, with_view(self.cfg, svg_max_chars=10)).neighborhood(self.ids["Dana Reyes"])
        self.assertEqual(cm.exception.code, ERROR)

    def hub_graph(self) -> str:
        """A centre on 8 projects of 4 people each: 40 neighbours at 2 hops. Returns the centre's id."""
        ep = "ep-big"
        lines = [f"P{i} is on project {p}" for p in range(8) for i in range(4)] + [f"Hub runs {p}" for p in range(8)]
        self.store.upsert_episode(ep, transcript_path="big.md", sha256="ab" * 32,
                                  meeting_start="2026-09-20T15:00:00+00:00")
        text = "\n".join(lines)
        hub = self.store.upsert_person("hub@example.test", "Hub Person", source="outlook")
        for p in range(8):
            project = self.store.upsert_entity("project", f"Project Number {p} With A Long Name", source="extract")
            self.store.add_edge(src_entity_id=hub, dst_entity_id=project, relation="works_on",
                                quote=f"Hub runs {p}", episode_id=ep, transcript_text=text)
            for i in range(4):
                person = self.store.upsert_person(f"p{p}-{i}@example.test", f"Person {p}-{i} Example", source="outlook")
                self.store.add_edge(src_entity_id=person, dst_entity_id=project, relation="works_on",
                                    quote=f"P{i} is on project {p}", episode_id=ep, transcript_text=text)
        return hub

    def test_forty_neighbours_fit_the_host_cap(self):
        """40 neighbours, fetched in pages of kg.traverse.max_results, drawn under the default cap."""
        answer = self.view.neighborhood(self.hub_graph(), hops=2)
        self.assertGreater(40, self.cfg["kg"]["traverse"]["max_results"])            # paging was needed
        self.assertEqual(len(answer["nodes"]), 41)
        self.assertFalse(answer["truncated"])
        self.assert_safe_svg(answer["svg"], self.cfg["kg"]["view"]["svg_max_chars"])

    def test_equal_subtrees_share_the_circle_equally(self):
        """Each child's wedge is its share of the parent's whole wedge: 8 projects of 4 people
        sit 45 degrees apart all the way round, not bunched on one side."""
        answer = self.view.neighborhood(self.hub_graph(), hops=2)
        centre = answer["nodes"][0]
        angles = sorted(math.degrees(math.atan2(n["y"] - centre["y"], n["x"] - centre["x"])) % 360
                        for n in answer["nodes"] if n["depth"] == 1)
        gaps = [b - a for a, b in zip(angles, angles[1:])] + [angles[0] + 360 - angles[-1]]
        for gap in gaps:
            self.assertAlmostEqual(gap, 45, delta=0.5)


class TestEscaping(ViewCase):
    def test_names_and_quotes_are_escaped(self):
        ep = "ep-esc"
        quote = 'he said "<b>&</b>" to Ops'
        nasty = 'Ops <script>alert("x")</script> & "Co"'
        self.store.upsert_episode(ep, transcript_path="esc.md", sha256="cd" * 32,
                                  meeting_start="2026-09-21T15:00:00+00:00")
        org = self.store.upsert_entity("organization", nasty, source="extract")
        self.store.add_edge(src_entity_id=self.ids["Sam Ortiz"], dst_entity_id=org, relation="member_of",
                            quote=quote, episode_id=ep, transcript_text=quote)
        cfg = with_view(self.cfg, label_chars=200)
        answer = View(self.store, cfg).neighborhood(org, hops=1)
        root = self.assert_safe_svg(answer["svg"], cfg["kg"]["view"]["svg_max_chars"])
        self.assertNotIn("<b>", answer["svg"])
        labels = [t.text for t in root.iter(SVG + "text")]
        self.assertIn(nasty, labels)
        titles = " ".join(t.text for t in root.iter(SVG + "title"))
        self.assertIn(nasty, titles)
        self.assertIn(quote, titles)
        self.assertIn(nasty, answer["alt"])

    def test_config_text_cannot_reach_the_svg(self):
        for change in ({"font_family": "x}</style><script>"},
                       {"colors": {**self.cfg["kg"]["view"]["colors"], "edge": "url(http://x)"}},
                       {"palette": {**self.cfg["kg"]["view"]["palette"], "person": "red;}"}}):
            with self.assertRaises(ViewError, msg=change) as cm:
                View(self.store, with_view(self.cfg, **change))
            self.assertEqual(cm.exception.code, ERROR)


class TestPalette(ViewCase):
    def test_palette_covers_the_ontology(self):
        types = set(read_yaml(config_file(self.cfg["ontology"]))["entity_types"])
        self.assertEqual(set(self.cfg["kg"]["view"]["palette"]), types)
        palette = dict(self.cfg["kg"]["view"]["palette"])
        del palette["topic"]
        with self.assertRaises(ViewError):
            View(self.store, with_view(self.cfg, palette=palette))


class TestMergedAway(ViewCase):
    def test_merged_away_id_follows_to_the_survivor(self):
        state = State(self.conn, self.cfg)
        run_id = state.begin_run("maintain")
        dupe = self.store.upsert_person("sam@old.test", "S. Ortiz", source="outlook")
        Resolver(self.store, state).merge(self.ids["Sam Ortiz"], dupe, reason="same person", run_id=run_id)
        answer = self.view.neighborhood(dupe, hops=1)
        self.assertEqual(answer["center"]["id"], self.ids["Sam Ortiz"])
        self.assertEqual(answer["center"]["resolved_from"], dupe)
        self.assertEqual(self.view.neighborhood("S. Ortiz", hops=1)["center"]["id"], self.ids["Sam Ortiz"])


class TestCli(ViewCase):
    def test_neighborhood_flags(self):
        code, answer = self.cli("neighborhood", "Dana Reyes", "--hops", "3", "--types", "person", "system",
                                "--relations", "works_on", "uses", "responsible_for")
        self.assertEqual(code, OK, answer)
        self.assertEqual(sorted(n["name"] for n in answer["nodes"] if not n["route_only"]),
                         ["Dana Reyes", "Databricks", "Lee Wong", "Sam Ortiz"])

    def test_exit_codes(self):
        code, answer = self.cli("neighborhood", "Nobody Here")
        self.assertEqual((code, set(answer)), (NOT_FOUND, {"error"}))
        code, answer = self.cli("neighborhood", "Dana Reyes", "--hops", "0")
        self.assertEqual((code, set(answer)), (USAGE, {"error"}))
        code, answer = self.cli("frobnicate")
        self.assertEqual(code, USAGE)
        code, answer = self.cli("neighborhood", "Dana Reyes", "--relations", "likes")
        self.assertEqual((code, set(answer)), (ERROR, {"error"}))
        code, answer = self.cli("neighborhood", "Dana Reyes", "--as-of", "last tuesday")
        self.assertEqual((code, set(answer)), (ERROR, {"error"}))

    def test_help_is_one_json_object(self):
        code, answer = self.cli("-h")
        self.assertEqual(code, USAGE)
        self.assertIn("usage:", answer["error"])

    def test_lone_surrogate_still_prints_json(self):
        code, answer = self.cli("search", "x\ud800")
        self.assertIn(code, (OK, ERROR))

    def test_episode_date_is_the_local_meeting_date(self):
        start = "2026-09-02T00:30:00+00:00"                             # an evening call west of UTC
        self.store.upsert_episode("ep-evening", transcript_path="evening.md", sha256="0e" * 32, meeting_start=start)
        ent = self.store.upsert_entity("project", "Evening Project", source="extract")
        self.store.add_edge(src_entity_id=self.ids["Dana Reyes"], dst_entity_id=ent, relation="responsible_for",
                            quote="Dana owns evening", episode_id="ep-evening", transcript_text="Dana owns evening")
        answer = self.view.neighborhood(self.ids["Dana Reyes"], hops=1)
        (edge,) = [e for e in answer["edges"] if e["episode"] == "ep-evening"]
        self.assertEqual(edge["episode_date"], datetime.fromisoformat(start).astimezone().date().isoformat())

    def test_missing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            ov = overlay(Path(tmp))
            code, answer = self.cli("search", "Atlas", ov=ov)
        self.assertEqual(code, NO_DATABASE)
        self.assertIn("no database", answer["error"])

    def test_subprocess_is_read_only_and_utf8(self):
        self.store.upsert_entity("topic", "Café ✓ Ops", source="extract")
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = list(self.conn.iterdump())
        path = self.write_overlay(self.ov)
        proc = subprocess.run([sys.executable, "-m", "kg.view", "neighborhood", self.ids["Dana Reyes"], "--hops", "2",
                               "--config", str(path)], cwd=REPO, capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, OK, proc.stderr.decode("utf-8", "replace"))
        answer = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(set(answer), ANSWER_KEYS)
        proc = subprocess.run([sys.executable, "-m", "kg.view", "search", "Café", "--config", str(path)], cwd=REPO,
                              capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, OK, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(json.loads(proc.stdout.decode("utf-8"))["results"][0]["name"], "Café ✓ Ops")
        self.assertEqual(list(self.conn.iterdump()), before)
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "overlay.yaml"
            empty.write_text(yaml.safe_dump(overlay(Path(tmp))), encoding="utf-8")
            proc = subprocess.run([sys.executable, "-m", "kg.view", "search", "x", "--config", str(empty)], cwd=REPO,
                                  capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, NO_DATABASE)
        self.assertIn("error", json.loads(proc.stdout.decode("utf-8")))


if __name__ == "__main__":
    unittest.main()
