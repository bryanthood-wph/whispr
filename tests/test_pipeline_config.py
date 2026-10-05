"""config/ loading and validation (issue #5, D.2). No model calls."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pipeline.config import ConfigError, load_config
from pipeline.jsonschema_lite import SchemaError, validate
from pipeline_helpers import overlay


class TestLoadConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_complete_overlay_loads(self):
        cfg = load_config(overlay=overlay(self.root))
        self.assertEqual(cfg["owner"]["name"], "Pat Example")
        self.assertEqual(cfg["eval"]["budget_usd"], 100)

    def test_defaults_alone_fail_on_missing_overlay_values(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(overlay={})
        self.assertIn("owner.name", str(ctx.exception))

    def test_unknown_overlay_key_fails(self):
        bad = overlay(self.root)
        bad["owner"]["nickname"] = "P"
        with self.assertRaises(ConfigError) as ctx:
            load_config(overlay=bad)
        self.assertIn("owner.nickname", str(ctx.exception))

    def test_wrong_type_fails(self):
        bad = overlay(self.root)
        bad["eval"] = {"budget_usd": "lots"}
        with self.assertRaises(ConfigError):
            load_config(overlay=bad)

    def test_bad_effort_fails(self):
        bad = overlay(self.root)
        bad["models"] = {"extractor": {"model": "haiku", "effort": "turbo"}}
        with self.assertRaises(ConfigError):
            load_config(overlay=bad)


class TestJsonSchemaLite(unittest.TestCase):
    SCHEMA = {
        "type": "object", "additionalProperties": False, "required": ["a"],
        "properties": {"a": {"type": "array", "items": {"type": "integer", "minimum": 0}},
                       "b": {"type": ["string", "null"]}},
    }

    def test_valid(self):
        self.assertEqual(validate({"a": [0, 2], "b": None}, self.SCHEMA), [])

    def test_errors_name_the_path(self):
        errors = validate({"a": [1, -1, "x"], "c": 1}, self.SCHEMA)
        self.assertTrue(any("$.a[1]" in e for e in errors))
        self.assertTrue(any("$.a[2]" in e for e in errors))
        self.assertTrue(any("unknown key 'c'" in e for e in errors))

    def test_bool_is_not_integer(self):
        self.assertTrue(validate({"a": [True]}, self.SCHEMA))

    def test_unsupported_keyword_raises(self):
        with self.assertRaises(SchemaError):
            validate("x", {"type": "string", "pattern": "^x$"})

    def test_unsupported_keyword_raises_even_where_no_data_reaches(self):
        schema = {"type": "object", "properties": {
            "a": {"type": "array", "items": {"type": "string", "pattern": "^x$"}}}}
        with self.assertRaises(SchemaError):
            validate({"a": []}, schema)


if __name__ == "__main__":
    unittest.main()
