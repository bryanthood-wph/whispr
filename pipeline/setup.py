"""python -m pipeline setup: the steps /whispr-setup runs (docs/plan/D-architecture-and-ops.md
D.7), one subcommand each, so the skill can show you each one's output before the next.

  setup plan [--json]
      what setup would put in the overlay, key by key, and where each value comes from:
        overlay    the existing overlay already sets it: kept
        derived    read from this whispr folder: the recorder's transcripts folder and log
                   (its config.yaml), liveness.command / working_dir (the recorder's
                   pythonw beside this interpreter), pipeline.run.process_since = today
        suggested  a default to confirm (setup.data_dir, setup.backup)
        ask        never derived (setup.ask): the skill asks you
        problem    could not be derived (the message says why)
      Plus the recorder's mic match, which is the recorder's own setting (its config.yaml,
      chosen by the installer's device picker), not the overlay's. Writes nothing (reading
      the recorder's config makes its folders, as the recorder itself does on start).
  setup write [--set KEY=VALUE ...] [--yes]
      the overlay: the existing one, plus each derived or suggested value it does not set
      yet, plus each --set (setup.keys only; an empty VALUE is null). Every `ask` key must
      be set by now. It is validated as a complete config, then printed; written, atomically,
      only with --yes.
  setup init
      create paths.data_dir and the database (every migration applied).
  setup test-alert
      raise setup.test_alert and print the line the SessionStart hook shows for it.

The overlay is --overlay, else WHISPR_OVERLAY, else %APPDATA%\\whispr\\config.yaml.
Exit 0 ok, 1 nothing written (write without --yes, or a value still to ask for), 2 bad
config or usage.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import yaml

from kg import db
from kg.state import State
from pipeline import alerts
from pipeline import backup as pipeline_backup
from pipeline import schedule as S
from pipeline.config import ConfigError, data_dir, default_overlay_path, load_config, merged, read_yaml
from pipeline.run import EXIT_FAILED, EXIT_OK
from whispr import config as recorder_config
from whispr.fileio import atomic_write_text
from whispr.log import LOG_FILENAME

EXIT_USAGE = 2
OVERLAY, DERIVED, SUGGESTED, ASK, PROBLEM = "overlay", "derived", "suggested", "ask", "problem"
# Keys whose value is not a string, so `--set` can't give them: always derived.
LIVENESS_COMMAND = "liveness.command"


class SetupError(ValueError):
    """A usage problem (exit 2); the message says what to change."""


class StillToAsk(SetupError):
    """A value setup cannot derive has not been given yet (exit 1)."""


def lookup(tree: Mapping, key: str) -> tuple[bool, Any]:
    """(found, value) for a dotted key."""
    node: Any = tree
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return False, None
        node = node[part]
    return True, node


def assign(tree: dict, key: str, value: Any) -> None:
    *parents, last = key.split(".")
    for part in parents:
        tree = tree.setdefault(part, {})
    tree[last] = value


def recorder(config_path: Optional[Path] = None) -> dict:
    """The recorder settings setup reads, from its config.yaml (whispr/config.py)."""
    rc = recorder_config.load_config(config_path)
    return {"config": str(config_path or recorder_config.CONFIG_PATH),
            "transcripts": str(rc["paths"]["transcripts"]),
            "recorder_log": str(Path(rc["paths"]["logs"]) / LOG_FILENAME),
            "mic_name_match": rc["audio"]["mic_name_match"]}


def suggest(hint: dict, environ: Mapping[str, str]) -> Optional[str]:
    """`hint.name` under the folder in the first of `hint.env` that is set, or None."""
    for var in hint["env"]:
        base = (environ.get(var) or "").strip()
        if base:
            return str(Path(base) / hint["name"])
    return None


def plan(overlay_path: Path, *, python: str = sys.executable, environ: Mapping[str, str] = os.environ,
         today: Optional[date] = None, recorder_path: Optional[Path] = None,
         root: Path = recorder_config.REPO_ROOT) -> dict:
    """See the module docstring: {"overlay", "existing", "values": {key: {value, source}},
    "recorder"}."""
    existing = read_yaml(overlay_path) if overlay_path.exists() else {}
    cfg = merged(existing)
    st = cfg["setup"]
    rec = recorder(recorder_path)
    found: dict[str, tuple[Any, str]] = {
        "paths.transcripts": (rec["transcripts"], DERIVED),
        "paths.recorder_log": (rec["recorder_log"], DERIVED),
        "liveness.working_dir": (str(root), DERIVED),
        "pipeline.run.process_since": ((today or date.today()).isoformat(), DERIVED),
        "paths.data_dir": (suggest(st["data_dir"], environ), SUGGESTED),
        "backup.destination": (suggest(st["backup"], environ), SUGGESTED),
        **{key: (None, ASK) for key in st["ask"]},
    }
    try:
        interpreter = S.resolve_interpreter(cfg, python)
        found[LIVENESS_COMMAND] = ([str(interpreter), *cfg["schedules"]["interpreter_args"], *st["recorder_args"]],
                                   DERIVED)
    except S.ScheduleError as exc:
        found[LIVENESS_COMMAND] = (str(exc), PROBLEM)
    values = {}
    for key in [*st["keys"], LIVENESS_COMMAND]:
        have, current = lookup(existing, key)
        value, source = (current, OVERLAY) if have else found.get(key, (None, ASK))
        if source == SUGGESTED and value is None:
            source = ASK                            # nothing to suggest it from
        values[key] = {"value": value, "source": source}
    return {"overlay": str(overlay_path), "existing": existing, "values": values, "recorder": rec}


def parse_sets(pairs: list[str], allowed: list[str]) -> dict[str, Optional[str]]:
    """KEY=VALUE strings as {key: value}, an empty value as None; SetupError for a key
    outside `allowed`."""
    out: dict[str, Optional[str]] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or key not in allowed:
            raise SetupError(f"--set {pair!r}: give KEY=VALUE with KEY one of {', '.join(allowed)}")
        out[key] = value.strip() or None
    return out


def compose(planned: dict, sets: Mapping[str, Optional[str]]) -> dict:
    """The overlay `write` would write: the existing one, plus each derived or suggested
    value it lacks, plus `sets`. SetupError when a value is still to ask for."""
    overlay = copy.deepcopy(planned["existing"])
    missing = []
    for key, entry in planned["values"].items():
        if key in sets:
            assign(overlay, key, sets[key])
        elif entry["source"] in (DERIVED, SUGGESTED):
            assign(overlay, key, entry["value"])
        elif entry["source"] in (ASK, PROBLEM):
            missing.append(f"{key} ({entry['value']})" if entry["source"] == PROBLEM else key)
    if missing:
        raise StillToAsk("still to set (--set KEY=VALUE, an empty VALUE for none): " + "; ".join(missing))
    return overlay


def changes(before: dict, after: dict, keys: list[str]) -> list[str]:
    lines = []
    for key in keys:
        had, old = lookup(before, key)
        _, new = lookup(after, key)
        if not had or old != new:
            lines.append(f"  {key}: {json.dumps(old) if had else '(unset)'} -> {json.dumps(new)}")
    return lines


def write(overlay_path: Path, pairs: list[str], *, yes: bool = False,
          out: Callable[[str], None] = print, **plan_kw) -> int:
    """`setup write`: see the module docstring. `pairs` are the --set KEY=VALUE strings."""
    planned = plan(overlay_path, **plan_kw)
    keys = merged(planned["existing"])["setup"]["keys"]
    overlay = compose(planned, parse_sets(pairs, keys))
    cfg = load_config(overlay=overlay)              # ConfigError: the caller prints it
    try:
        pipeline_backup.destination(cfg)
    except pipeline_backup.NotConfigured:
        out("note: backup.destination is unset: the daily job will alert until it is set")
    except pipeline_backup.BackupError as exc:
        raise ConfigError(str(exc)) from exc
    text = yaml.safe_dump(overlay, sort_keys=False, allow_unicode=True)
    diff = changes(planned["existing"], overlay, list(planned["values"]))
    out(f"overlay {overlay_path}:" + ("" if diff else " no change"))
    for line in diff:
        out(line)
    if not diff:
        return EXIT_OK
    if not yes:
        out("nothing written: re-run with --yes to write the overlay above")
        return EXIT_FAILED
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(overlay_path, text)
    out(f"wrote {overlay_path}")
    return EXIT_OK


def init(cfg: dict, out: Callable[[str], None] = print) -> int:
    folder = data_dir(cfg)
    with contextlib.closing(db.connect(cfg)) as conn:
        versions = sorted(db.applied_versions(conn))
    out(f"data folder {folder}; database {db.database_path(cfg)} at schema version(s) {versions}")
    return EXIT_OK


def test_alert(cfg: dict, out: Callable[[str], None] = print) -> int:
    t = cfg["setup"]["test_alert"]
    with contextlib.closing(db.connect(cfg)) as conn:
        State(conn, cfg).raise_alert(t["kind"], t["key"], t["message"], t["fix"])
    out("raised the test alert. The SessionStart hook now shows:")
    out(alerts.session_start_text(cfg=cfg) or "(nothing: the alert did not reach the surface)")
    out(f"key {t['key']} (acknowledge it: python -m pipeline alerts --ack {t['key']})")
    return EXIT_OK


def add_parser(sub) -> None:
    """The `setup` subcommand, on the CLI's subparsers."""
    p = sub.add_parser("setup", help="the /whispr-setup steps: plan, write the overlay, init, test alert")
    steps = p.add_subparsers(dest="step", required=True)
    pl = steps.add_parser("plan", help="what setup would write, and where each value comes from")
    pl.add_argument("--json", action="store_true")
    w = steps.add_parser("write", help="write the overlay (only with --yes)")
    w.add_argument("--set", dest="sets", action="append", default=[], metavar="KEY=VALUE")
    w.add_argument("--yes", action="store_true", help="write it; without --yes only print it")
    steps.add_parser("init", help="create the data folder and the database")
    steps.add_parser("test-alert", help="raise the test alert and show it as the hook will")


def main(args, out: Callable[[str], None] = print) -> int:
    overlay_path = args.overlay or default_overlay_path()
    try:
        if args.step == "plan":
            planned = plan(overlay_path)
            if args.json:
                out(json.dumps(planned, indent=1, default=str))
            else:
                out(f"overlay {planned['overlay']}")
                for key, entry in planned["values"].items():
                    out(f"  {key}: {json.dumps(entry['value'])} [{entry['source']}]")
                rec = planned["recorder"]
                out(f"recorder mic match {rec['mic_name_match']!r} (its own setting, in {rec['config']})")
            return EXIT_OK
        if args.step == "write":
            return write(overlay_path, args.sets, yes=args.yes, out=out)
        cfg = load_config(overlay_path=overlay_path)
        return init(cfg, out) if args.step == "init" else test_alert(cfg, out)
    except SetupError as exc:
        out(f"setup: {exc}")
        return EXIT_FAILED if isinstance(exc, StillToAsk) else EXIT_USAGE
    except ConfigError as exc:
        out(f"setup: the config does not load: {exc}")
        return EXIT_USAGE
