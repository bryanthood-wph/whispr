"""Prompt templates from config/prompts/: load, fill, and hash.

Placeholders are {{NAME}}. Rendering fails if any placeholder is left unfilled or a
supplied value matches no placeholder, so a renamed field can't silently send a
prompt with a literal "{{...}}" in it. Every run records the template's hash.
"""

from __future__ import annotations

import hashlib
import re

from pipeline.config import config_file

_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


class PromptError(ValueError):
    pass


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load(relative: str) -> str:
    """Template text with line endings normalized to LF, so git's CRLF conversion on
    checkout never changes a prompt's hash or a request's cache key."""
    return config_file(relative).read_text(encoding="utf-8").replace("\r\n", "\n")


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
