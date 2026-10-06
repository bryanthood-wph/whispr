"""python -m pipeline <command>

  schedule --check      compare the live scheduled tasks with config (schedules.*);
                        exits 1 on any drift, 2 when the tasks cannot be read
  schedule --apply      Set-ScheduledTask on tasks that exist and drift, then re-check;
                        never registers, so a missing task stays drift (exit 1)
  schedule --register   /whispr-setup only: register missing tasks, then read every task
                        back; exits 1 unless all match config and have a NextRunTime
  schedule ... --yes    --apply and --register print each planned write first and write
                        only with --yes; without it they exit 1 having written nothing

More commands (run, doctor, ...) join the subcommand table in main().
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Optional

from pipeline import schedule as S
from pipeline.config import ConfigError, load_config


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
    ap = argparse.ArgumentParser(prog="python -m pipeline")
    ap.add_argument("--overlay", type=Path, help="per-user config overlay (default %%APPDATA%%\\whispr\\config.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("schedule", help="scheduled tasks generated from config")
    mode = s.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--register", action="store_true")
    s.add_argument("--yes", action="store_true", help="let --apply / --register write the changes they print")
    return ap, frozenset(sub.choices)


def main(argv: Optional[list[str]] = None, runner: Optional[Callable[[dict], S.Runner]] = None) -> int:
    """`runner(cfg)` builds the PowerShell Runner; tests pass a fake."""
    args = build_parser()[0].parse_args(argv)
    commands: dict[str, Callable[[dict, argparse.Namespace], int]] = {
        "schedule": lambda cfg, a: cmd_schedule(cfg, a, (runner or S.powershell_runner)(cfg)),
    }
    try:
        cfg = load_config(overlay_path=args.overlay)
        return commands[args.cmd](cfg, args)
    except (ConfigError, S.ScheduleError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
