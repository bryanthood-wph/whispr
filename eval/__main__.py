"""python -m eval <command>

  status                       spend vs budget, runs; exits 1 while a run is unresolved
                               or spend has reached the stop limit
  sample [--write]             freeze the frame, derive low-mic, draw the sample
  run --stage S [--dry-run]    run a stage; --dry-run plans it with zero model calls
                               (pilot: probe, every step, then report.json/.md in results/<run>/;
                               dev: one prompt revision on the tuning set, my-task measures)
  resolve RUN_ID --note TEXT   acknowledge a failed run
  graph-first [--dry-run | --rescore RUN_ID]
                               P2b (eval/graph_first.py): the agreed questions in fresh live
                               sessions, before and after the graph-first note, scored on
                               whispr-kg first + a cited graph quote; --dry-run prints the plan
                               and cost estimate with zero model calls; --rescore re-scores a
                               run's raw transcripts. Exit 0 PASS, 1 FAIL, 2 refused/stopped

Scored runs start only from a one-shot scheduled task (B.9), never a Claude session.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from eval import frame as F
from eval import ledger as L
from eval import dev, graph_first, pilot, preflight, stages
from pipeline import calls
from pipeline.config import load_config
from whispr.fileio import atomic_write_text


def _sample(cfg: dict) -> tuple[list[F.FrameItem], F.Sample, str]:
    items, state = F.load_frame(cfg)
    return items, F.draw(items, cfg), state


def _print_sample(items, sample, state) -> None:
    print(f"frame: {len(items)} transcripts ({state})")
    if state == F.PROVISIONAL:
        print("  the cutoff day has not ended: nothing frozen, and scored runs refuse this frame")
    low = sum(1 for i in items if i.low_mic)
    unmatched = [i.id for i in items if i.low_mic is None]
    print(f"low-mic: {low}; no recorder-log match: {len(unmatched)} {unmatched}")
    print(f"pilot: {sample.pilot}")
    for cell, core in sample.core.items():
        print(f"  {cell:15} core {len(core):3}  task-only {len(sample.task_only[cell]):3}  "
              f"low-mic replaced {len(sample.low_mic[cell])}  unscreened replaced {len(sample.unscreened[cell])}  "
              f"tuning {len(sample.dev.get(cell, []))}")
    if sample.shortfall:
        print(f"SHORTFALL (cell -> missing units): {sample.shortfall}")


def cmd_status(cfg: dict, _args) -> int:
    killed = L.reconcile(cfg)
    total, limit = L.spent(cfg), L.budget_limit(cfg)
    print(f"spend ${total:.2f} of ${cfg['eval']['budget_usd']:.2f} (stop limit ${limit:.2f};"
          f" unsettled reservations count at their cap)")
    bad_lines = L.malformed(cfg)
    if bad_lines:
        print(f"WARNING: {bad_lines} unreadable ledger/run-record line(s) skipped (a killed append?)")
    for rid in killed:
        print(f"marked failed (killed): {rid}")
    bad = L.unresolved(cfg)
    for rid, events in bad.items():
        print(f"UNRESOLVED {rid}: {events[-1].get('error')}  ->  python -m eval resolve {rid} --note ...")
    if total >= limit:
        print("STOP: spend reached the stop limit; write RESUME.md and ask (G.5)")
    return 1 if bad or total >= limit else 0


def cmd_sample(cfg: dict, args) -> int:
    items, sample, state = _sample(cfg)
    _print_sample(items, sample, state)
    if args.write:
        out = L.eval_dir(cfg) / f"sample-{sample.seed}.json"
        atomic_write_text(out, json.dumps({**asdict(sample), "frame": state,
                                           "low_mic_frame": sorted(i.id for i in items if i.low_mic)}, indent=1))
        print(f"wrote {out}")
    return 1 if sample.shortfall else 0


def cmd_run(cfg: dict, args) -> int:
    items, sample, state = _sample(cfg)
    jobs = stages.plan(args.stage, cfg, sample, items)
    cache = L.eval_dir(cfg) / "cache"
    cap = L.call_cap(cfg, args.stage)
    cached = {j.key for j in jobs if calls.cached_entry(cache, j.key, j.schema)}
    calls_needed = [j for j in jobs if j.key not in cached and not j.prepared.is_stub]
    refusals = preflight.refusals(cfg, args.stage)
    if state == F.PROVISIONAL:
        refusals.append("the frame is provisional: its cutoff day has not ended")
    total, by_stage = L.tally(cfg)
    print(f"stage {args.stage}: {len(jobs)} jobs, {len(calls_needed)} to call "
          f"({len(cached)} cached, stubs never call)")
    if args.stage in ("pilot", dev.STAGE):
        print(f"  {pilot.DRY_RUN_NOTE}")
    worst, stage_cap = cap * len(calls_needed), cfg["eval"]["stages"][args.stage]["cap_usd"]
    print(f"worst-case spend ${worst:.2f} (stage cap ${stage_cap:.2f}, ${by_stage.get(args.stage, 0.0):.2f} used;"
          f" spent ${total:.2f} of limit ${L.budget_limit(cfg):.2f})")
    if worst > stage_cap:
        print("  worst case exceeds the stage cap: each call reserves its full per-call cap, so the run"
              " stops with BudgetStop (resolve it) if real costs approach the cap")
    for j in jobs:
        status = "stub" if j.prepared.is_stub else "cached" if j.key in cached else "call"
        print(f"  {j.unit_id} {j.label:12} key {j.key[:12]} {status}")
    if args.dry_run:
        for r in refusals:
            print(f"would refuse a scored run: {r}")
        print("dry run: zero model calls made")
        return 0
    if refusals:
        for r in refusals:
            print(f"REFUSED: {r}")
        return 2

    with L.Run(cfg, args.stage, preflight.provenance(cfg)) as run:
        run_dir = L.eval_dir(cfg) / "results" / run.run_id
        run_dir.mkdir(parents=True)
        if args.stage == dev.STAGE:
            dev.execute(cfg, run, run_dir, jobs, cache, cap,
                        design=F.design(sample, items, include_task_only=False, dev=True))
        else:
            pilot.execute(cfg, run, run_dir, jobs, cache, cap)   # stages.plan refuses every other stage
    return 0


def cmd_resolve(cfg: dict, args) -> int:
    L.resolve(cfg, args.run_id, args.note)
    print(f"resolved {args.run_id}")
    return 0


def cmd_graph_first(cfg: dict, args) -> int:
    if args.rescore:
        return graph_first.rescore(cfg, args.rescore)
    return graph_first.run(cfg, dry_run=args.dry_run)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval")
    ap.add_argument("--overlay", type=Path, help="per-user config overlay (default %%APPDATA%%\\whispr\\config.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    s = sub.add_parser("sample")
    s.add_argument("--write", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--stage", required=True)
    r.add_argument("--dry-run", action="store_true")
    v = sub.add_parser("resolve")
    v.add_argument("run_id")
    v.add_argument("--note", required=True)
    gf = sub.add_parser("graph-first")
    mode = gf.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--rescore", metavar="RUN_ID")
    args = ap.parse_args(argv)
    cfg = load_config(overlay_path=args.overlay)
    return {"status": cmd_status, "sample": cmd_sample, "run": cmd_run, "resolve": cmd_resolve,
            "graph-first": cmd_graph_first}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
