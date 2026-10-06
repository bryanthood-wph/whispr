"""`python -m pipeline`, from the whispr folder named on the command line, whatever the
current directory is:

    <whispr python> -s <plugin>/bin/pipeline.py <whispr folder> <pipeline arguments...>

/whispr-setup runs every step through this, as one plain command. A checkout's venv does
not put the repo on sys.path (the installed distribution's python\\ does, through its
._pth file), and `cd <folder> && python -m pipeline` would be two commands to approve.

The folder gets the same checks as the SessionStart hook's (lib/whispr_paths.py): no
`..`, resolved, it must hold pipeline/__main__.py, and the pipeline package imported must
come from under it. Its exit code is the pipeline's.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import whispr_paths as paths  # noqa: E402

USAGE = 2


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: pipeline.py <whispr folder> <python -m pipeline arguments...>", file=sys.stderr)
        return USAGE
    try:
        root = paths.whispr_root(argv[0], "the folder given")
        paths.put_first(root)
        import pipeline
        paths.loaded_from(root, pipeline)
    except paths.Refused as exc:
        print(f"whispr: {exc}", file=sys.stderr)
        return USAGE
    sys.argv = [str(root / paths.MARKER), *argv[1:]]
    try:
        runpy.run_module("pipeline", run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
