"""Where the whispr install is, for the plugin's own scripts (hooks/session_start.py,
bin/pipeline.py), and the path checks they share (the user's hook-security rules 3-5).

Standard library only: it runs before anything of whispr's can be imported.
"""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path, PurePath
from typing import Optional

# What makes a folder a whispr install: the pipeline CLI every plugin script runs.
MARKER = Path("pipeline") / "__main__.py"
# The folder above the plugin: the whispr folder when the plugin is loaded in place
# (plugin/lib/<this> -> plugin -> the whispr folder).
IN_PLACE_ROOT = Path(__file__).resolve().parents[2]
# Hook-security rule 5, by file name (case-insensitive); `.git` by folder.
SENSITIVE = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "credentials.*", "settings.local.json")
SENSITIVE_DIRS = (".git",)


class Refused(ValueError):
    """A path this plugin will not use; the message says why."""


def checked_path(raw: str, what: str) -> Path:
    """`raw` resolved to an absolute path; Refused if it has a `..` segment."""
    if any(part == ".." for part in PurePath(raw).parts):
        raise Refused(f"the {what} {raw!r} contains '..'")
    return Path(raw).expanduser().resolve()


def sensitive(path: Path) -> bool:
    name = path.name.lower()
    return (any(fnmatch.fnmatchcase(name, pattern) for pattern in SENSITIVE)
            or any(part.lower() in SENSITIVE_DIRS for part in path.parts))


def whispr_root(raw: Optional[str], where: str) -> Path:
    """The whispr folder `raw` names (`where` says where it came from), or the folder
    above this plugin when `raw` is empty. Refused unless it is a whispr install."""
    raw = (raw or "").strip()
    root = checked_path(raw, "whispr folder") if raw else IN_PLACE_ROOT
    if not (root / MARKER).is_file():
        source = where if raw else f"{where} is unset, and this is the folder above the plugin"
        raise Refused(f"no whispr install at {root} ({source})")
    return root


def put_first(root: Path) -> None:
    """Make `root` the first place imports look, and drop the plugin's own script
    folders, so `pipeline` and `kg` can only come from the whispr folder."""
    plugin = Path(__file__).resolve().parents[1]
    sys.path[:] = [p for p in sys.path if not _inside(p, plugin)]
    sys.path.insert(0, str(root))


def loaded_from(root: Path, module) -> None:
    """Refused unless `module` was imported from under `root` (a prefix check)."""
    loaded = Path(module.__file__).resolve()
    if root not in loaded.parents:
        raise Refused(f"{module.__name__} loaded from {loaded}, outside the whispr folder {root}")


def _inside(entry: str, folder: Path) -> bool:
    try:
        path = Path(entry or ".").resolve()
    except OSError:
        return False
    return path == folder or folder in path.parents
