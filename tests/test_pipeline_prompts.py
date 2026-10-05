"""config/prompts/ templates and rendering (issue #7). No model calls."""

from __future__ import annotations

import unittest

from pipeline import prompts

EXTRACT_FIELDS = {"CALL_TITLE", "DATE", "CALL_TYPE", "ORGANIZER", "ATTENDEES", "OWNER_NAME", "TRANSCRIPT"}


class TestExtractPrompt(unittest.TestCase):
    def setUp(self):
        self.template = prompts.load("prompts/extract.md")

    def test_placeholders(self):
        self.assertEqual(prompts.placeholders(self.template), EXTRACT_FIELDS)

    def test_no_person_specific_text(self):
        # Owner name comes from config (D.2); the shipped prompt names no real person.
        for name in ("Colby", "Hood", "Deloitte"):
            self.assertNotIn(name, self.template)

    def test_render_fills_everything(self):
        values = {k: f"<{k}>" for k in EXTRACT_FIELDS}
        out = prompts.render(self.template, values)
        self.assertNotIn("{{", out)
        self.assertIn("<OWNER_NAME>", out)

    def test_render_rejects_missing_and_unused(self):
        values = {k: "x" for k in EXTRACT_FIELDS}
        del values["TRANSCRIPT"]
        with self.assertRaises(prompts.PromptError):
            prompts.render(self.template, values)
        values["TRANSCRIPT"], values["EXTRA"] = "x", "y"
        with self.assertRaises(prompts.PromptError):
            prompts.render(self.template, values)

    def test_value_is_not_re_expanded(self):
        self.assertEqual(prompts.render("a {{X}} b", {"X": "{{X}}"}), "a {{X}} b")


if __name__ == "__main__":
    unittest.main()
