"""Prompt templates from config/prompts/: load, fill, and hash.

Placeholders are {{NAME}}. Rendering fails if any placeholder is left unfilled or a
supplied value matches no placeholder, so a renamed field can't silently send a
prompt with a literal "{{...}}" in it. Every run records the template's hash.

A template may open with one `<!-- ... -->` author note for whoever edits the file;
the loader strips it, so it never reaches a model.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pipeline.config import config_file

_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")
_AUTHOR_NOTE = re.compile(r"\A\s*<!--.*?-->[ \t]*\n?", re.DOTALL)


class PromptError(ValueError):
    pass


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(obj: Any) -> str:
    """The one canonical JSON form hashed for keys and registered schema hashes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load(relative: str) -> str:
    """Template text with line endings normalized to LF, so git's CRLF conversion on
    checkout never changes a prompt's hash or a request's cache key, and a leading
    author note removed."""
    text = config_file(relative).read_text(encoding="utf-8").replace("\r\n", "\n")
    return _AUTHOR_NOTE.sub("", text, count=1)


def placeholders(template: str) -> set[str]:
    return set(_PLACEHOLDER.findall(template))


def render(template: str, values: dict[str, str]) -> str:
    wanted = placeholders(template)
    missing = wanted - set(values)
    unused = set(values) - wanted
    if missing or unused:
        raise PromptError(f"placeholders missing={sorted(missing)} unused={sorted(unused)}")
    # Single pass, so a value that itself contains "{{X}}" is never re-expanded.
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)
