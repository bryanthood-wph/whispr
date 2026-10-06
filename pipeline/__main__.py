"""python -m pipeline [--overlay PATH] <command>

  run [--dry-run] [--once TRANSCRIPT]
      process every new or changed transcript dated on or after pipeline.run.process_since:
      prepare -> extract -> write (pipeline/run.py).
      --dry-run   discover and plan only: no model call, nothing written (it prints)
      --once T    run one transcript now, whatever its queue state or date (a path, file
                  name or stem of a transcript in paths.transcripts), for troubleshooting
      exit 0 ok (or another run holds the lock), 1 failed, 2 usage, 3 partial (stopped
      before a time or spend limit; the rest stay queued), 4 refused (nested Claude
      session, or pipeline.run.process_since unset)
  backfill [--since DATE] [--until DATE] [--yes]
      count the older transcripts dated since..until (default: up to the day before
      process_since) not yet processed, and print their estimated cost; with --yes,
      queue them for the scheduled run (newest first, within its spend caps)
  requeue (REF | --all-quarantined)
      after fixing the cause: put a quarantined transcript (REF, its file stem, as the
      alert names it), or every quarantined one, back in the queue with fresh attempts
  schedule --check      compare the live scheduled tasks with config (schedules.*);
                        exits 1 on any drift, 2 when the tasks cannot be read
  schedule --apply      Set-ScheduledTask on tasks that exist and drift, then re-check;
                        never registers, so a missing task stays drift (exit 1)
  schedule --register   /whispr-setup only: register missing tasks, then read every task
                        back; exits 1 unless all match config and have a NextRunTime
  schedule ... --yes    --apply and --register print each planned write first and write
                        only with --yes; without it they exit 1 having written nothing

The `whispr-pipeline` scheduled task runs `run` at logon and every 15 minutes. Never run
it without --dry-run from inside a Claude Code session: it refuses (and alerts).

Any exception that escapes a command (a bad config under `run`, an unwritable data dir)
is appended, with a timestamp and traceback, to %LOCALAPPDATA%/whispr/pipeline-fatal.log,
a fixed place found without config, because under pythonw nothing else would show it;
the exit code is then 1. `schedule` is run by hand, so it prints a config or scheduler
error instead and exits 2.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from pipeline import run as pipeline_run
from pipeline import schedule as S
from pipeline.config import ConfigError, load_config


FATAL_LOG = Path("whispr") / "pipeline-fatal.log"     # under %LOCALAPPDATA%: needs no config to find


def fatal_log_path() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / FATAL_LOG


def _print_findings(findings: list[S.Finding]) -> None:
    for f in findings:
        print(f"DRIFT {f}")


def _confirm(yes: bool) -> S.Confirm:
    """Print each planned write; let it go ahead only with --yes."""
    def confirm(planned: list[S.Finding]) -> bool:
        for f in planned:
            print(f"would {'register' if f.kind == S.MISSING else 'change'} {f}")
        return yes
    return confirm


def _declined() -> int:
    print("nothing written: re-run with --yes to write the changes above", file=sys.stderr)
    return 1


def cmd_schedule(cfg: dict, args, run: S.Runner) -> int:
    if args.check:
        findings = S.check(cfg, run)
        _print_findings(findings)
        print(f"schedule: {len(cfg['schedules']['tasks'])} task(s), {len(findings)} finding(s)")
        return 1 if findings else 0
    if args.apply:
        outcome = S.apply(cfg, run, confirm=_confirm(args.yes))
        if outcome.declined:
            return _declined()
        for name in outcome.changed:
            print(f"updated {name}")
        _print_findings(outcome.findings)
        if any(f.kind == S.MISSING for f in outcome.findings):
            print("a missing task is registered by setup (schedule --register), never by --apply")
        return 1 if outcome.findings else 0
    try:
        outcome = S.register(cfg, run, confirm=_confirm(args.yes))
        if outcome.declined:
            return _declined()
    except S.RegistrationFailed as exc:
        for name in exc.outcome.changed:
            print(f"registered {name}")
        _print_findings(exc.outcome.findings)
        print("FAILED: the read-back does not match config; the tasks above are not armed as configured",
              file=sys.stderr)
        return 1
    for name in outcome.changed:
        print(f"registered {name}")
    print(f"schedule: all {len(cfg['schedules']['tasks'])} task(s) match config and have a NextRunTime")
    return 0


def build_parser() -> tuple[argparse.ArgumentParser, frozenset[str]]:
    """main()'s parser and the subcommands it defines. schedule.py reads the second to
    refuse a task whose args run any other `-m pipeline` subcommand."""
    ap = argparse.ArgumentParser(prog="python -m pipeline", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--overlay", type=Path, help="per-user config overlay (default %%APPDATA%%\\whispr\\config.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="process every new or changed transcript")
    p.add_argument("--dry-run", action="store_true", help="discover and plan only: zero model calls")
    p.add_argument("--once", metavar="TRANSCRIPT", help="run one transcript now (troubleshooting)")
    b = sub.add_parser("backfill", help="quote, then (with --yes) queue, older transcripts")
    b.add_argument("--since", type=date.fromisoformat, metavar="DATE", help="earliest call date (YYYY-MM-DD)")
    b.add_argument("--until", type=date.fromisoformat, metavar="DATE", help="latest call date (YYYY-MM-DD)")
    b.add_argument("--yes", action="store_true", help="queue them: the scheduled run then spends on them")
    q = sub.add_parser("requeue", help="put a quarantined transcript back in the queue")
    q.add_argument("ref", nargs="?", help="the transcript's file stem, as its quarantine alert names it")
    q.add_argument("--all-quarantined", action="store_true", help="every quarantined transcript")
    s = sub.add_parser("schedule", help="scheduled tasks generated from config")
    mode = s.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--register", action="store_true")
    s.add_argument("--yes", action="store_true", help="let --apply / --register write the changes they print")
    return ap, frozenset(sub.choices)


def main(argv: Optional[list[str]] = None, runner: Optional[Callable[[dict], S.Runner]] = None) -> int:
    """The command's exit code; an escaped exception is logged to fatal_log_path(), exit 1.
    `runner(cfg)` builds the schedule command's PowerShell Runner; tests pass a fake."""
    try:
        return _main(argv, runner)
    except Exception:
        args = " ".join(sys.argv[1:] if argv is None else argv)
        text = f"{datetime.now(timezone.utc).isoformat()} python -m pipeline {args}\n{traceback.format_exc()}\n"
        try:
            path = fatal_log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass                                # nowhere left to write; the exit code still says it failed
        if sys.stderr is not None:              # None under pythonw
            sys.stderr.write(text)
        return pipeline_run.EXIT_FAILED


def _main(argv: Optional[list[str]], runner: Optional[Callable[[dict], S.Runner]]) -> int:
    parser = build_parser()[0]
    args = parser.parse_args(argv)
    if args.cmd == "schedule":
        try:
            cfg = load_config(overlay_path=args.overlay)
            return cmd_schedule(cfg, args, (runner or S.powershell_runner)(cfg))
        except (ConfigError, S.ScheduleError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    if args.cmd == "requeue" and (args.ref is None) == (not args.all_quarantined):
        parser.error("requeue: give exactly one of REF or --all-quarantined")
    cfg = load_config(overlay_path=args.overlay)
    if args.cmd == "requeue":
        return pipeline_run.requeue(cfg, args.ref, all_quarantined=args.all_quarantined)
    if args.cmd == "backfill":
        return pipeline_run.backfill(cfg, since=args.since, until=args.until, yes=args.yes)
    return pipeline_run.run(cfg, dry_run=args.dry_run, once=args.once)


if __name__ == "__main__":
    sys.exit(main())
