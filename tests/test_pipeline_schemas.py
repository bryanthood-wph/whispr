"""ontology.yaml v1, schema/extract.json, schema/task.json (issue #6). No model calls."""

from __future__ import annotations

import copy
import json
import unittest

import yaml

from pipeline.config import config_file
from pipeline.jsonschema_lite import validate


def _load_json(rel):
    with open(config_file(rel), encoding="utf-8") as fh:
        return json.load(fh)


EXTRACT = _load_json("schema/extract.json")
TASK = _load_json("schema/task.json")
with open(config_file("ontology.yaml"), encoding="utf-8") as _fh:
    ONTOLOGY = yaml.safe_load(_fh)

SAMPLE = {
    "headline": "The team agreed to ship the deck Friday.",
    "sections": [{"heading": "Deck", "points": ["Review moved to Thursday."]}],
    "my_actions": [{
        "action": "Send the deck to Jamie", "owner_basis": "volunteered",
        "due": {"text": "Friday", "basis": "stated"}, "context": "Final review copy",
        "quote": "I'll send the deck to Jamie by Friday", "start": "00:01:02",
    }],
    "other_tasks": [{
        "action": "Book the room", "owner": None, "due": {"text": None, "basis": "not_stated"},
        "context": "", "quote": "someone book the room", "start": None,
    }],
    "topics": ["deck"],
    "entities": [{"name": "Jamie Doe", "type": "person", "aliases": ["Jamie"]}],
    "facts": [{"type": "decision", "text": "Ship Friday", "subject": None, "quote": "ship it Friday", "start": "00:02:00"}],
    "edges": [{"src": "Jamie Doe", "relation": "works_on", "dst": "Deck", "quote": "Jamie owns the deck", "start": None}],
}


def _enum(schema_items, field):
    return set(schema_items["items"]["properties"][field]["enum"])


class TestExtractSchema(unittest.TestCase):
    def test_sample_is_valid(self):
        self.assertEqual(validate(SAMPLE, EXTRACT), [])

    def test_my_actions_is_required(self):
        bad = copy.deepcopy(SAMPLE)
        del bad["my_actions"]
        self.assertTrue(any("my_actions" in e for e in validate(bad, EXTRACT)))

    def test_empty_my_actions_is_valid(self):
        ok = copy.deepcopy(SAMPLE)
        ok["my_actions"] = []
        self.assertEqual(validate(ok, EXTRACT), [])

    def test_unknown_owner_basis_fails(self):
        bad = copy.deepcopy(SAMPLE)
        bad["my_actions"][0]["owner_basis"] = "probably"
        self.assertTrue(validate(bad, EXTRACT))

    def test_uses_only_structured_output_keywords(self):
        banned = {"minimum", "maximum", "minLength", "maxLength", "pattern", "format"}
        text = json.dumps(EXTRACT)
        self.assertFalse([k for k in banned if f'"{k}"' in text])


class TestOntologyMatchesSchema(unittest.TestCase):
    props = EXTRACT["properties"]

    def test_entity_types(self):
        self.assertEqual(_enum(self.props["entities"], "type"), set(ONTOLOGY["entity_types"]))

    def test_fact_types(self):
        self.assertEqual(_enum(self.props["facts"], "type"), set(ONTOLOGY["fact_types"]))

    def test_relations(self):
        self.assertEqual(_enum(self.props["edges"], "relation"), set(ONTOLOGY["relations"]))

    def test_owner_basis(self):
        mine = _enum(self.props["my_actions"], "owner_basis")
        stored = set(TASK["properties"]["owner_basis"]["enum"])
        self.assertTrue(mine <= set(ONTOLOGY["owner_basis"]))
        self.assertEqual(stored, set(ONTOLOGY["owner_basis"]))


class TestTaskSchema(unittest.TestCase):
    def test_contract_sample(self):
        task = {
            "id": "abc", "owner": "Pat Example", "owner_basis": "assigned", "action": "Send deck",
            "due": None, "due_basis": "not_stated", "context": "", "quote": "send the deck",
            "source": {"episode": "e1", "start": "00:00:01"}, "confidence": 0.8,
            "status": "captured", "tools_allowed": [],
        }
        self.assertEqual(validate(task, TASK), [])
        task["confidence"] = 1.5
        self.assertTrue(validate(task, TASK))


if __name__ == "__main__":
    unittest.main()
