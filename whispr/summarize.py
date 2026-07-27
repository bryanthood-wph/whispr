"""Pending-summary tracker (local, read-only).

whispr does NOT summarize transcripts and does not fill the summary/topic fields.
That is handed off to a local, on-device agent (see SUMMARY_AGENT.md) so that no
transcript text ever leaves the machine.

This module's only job is to *report* which transcripts still need a summary — a
transcript whose frontmatter still reads `topic: [unsorted]`. It reads files and
prints; it never edits them and never contacts any service.

    python -m whispr list-pending          # list all transcripts awaiting a summary
    python -m whispr list-pending <file>   # check one file
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from whispr.config import load_config
from whispr.log import get_logger
from whispr.models import PENDING_SUMMARY_TOPIC

log = get_logger("summarize")

# A transcript still needs a summary while its topic line is the placeholder.
_UNSUMMARIZED_RE = re.compile(
    r"^topic:\s*\[" + re.escape(PENDING_SUMMARY_TOPIC) + r"\]\s*$",
    re.MULTILINE,
)


def list_pending(argv: list[str] | None = None) -> None:
    """List transcripts still awaiting a local summary. Read-only; no network."""
    args = sys.argv[1:] if argv is None else argv
    cfg = load_config()
    transcripts_dir = Path(cfg["paths"]["transcripts"])

    candidates = [Path(args[0])] if args else sorted(transcripts_dir.glob("*.md"))
    pending = 0
    for md_path in candidates:
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("could not read %s: %s", md_path, exc)
            print(f"error: {md_path} ({exc})")
            continue
        if _UNSUMMARIZED_RE.search(text):
            pending += 1
            print(f"pending summary: {md_path}")
        else:
            print(f"done: {md_path}")
    print(f"\n{pending} transcript(s) awaiting a local summary (see SUMMARY_AGENT.md). "
          "whispr does not summarize on-device or send text off the machine.")


# Backwards-compatible CLI name; kept so scripts referencing the old verb still work.
retry_cli = list_pending


if __name__ == "__main__":
    list_pending()
