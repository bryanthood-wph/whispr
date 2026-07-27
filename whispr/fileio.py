"""Small filesystem helpers shared across modules."""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(path: Path, text: str, newline: str = "\n") -> None:
    """Write text to `path` atomically: write a sibling temp file, then os.replace.

    A crash mid-write never leaves a half-written file at `path`. Uses LF newlines
    by default so generated markdown is consistent regardless of platform.
    """
    tmp_path = path.with_name(f".{path.name}.tmp")
    with open(tmp_path, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)
    os.replace(tmp_path, path)
