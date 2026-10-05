"""The eval's model access: one `ask` callable that every stage receives.

    ask(role, prompt, schema, *, system_prompt=None, replicate=0) -> dict

returns the schema-valid structured output for `role` (a config `models` key). Stage
logic (reference consensus, judging) takes `ask` as a parameter, so it is tested with
a plain function and run for real with `make_ask`, which binds pipeline.calls.cached_call
to the run's ledger, cache and budget guard. Nothing else in eval/ calls a model.

`fan_out` runs independent jobs (each making its own `ask` calls) on eval.workers
threads. Every job makes exactly the requests it would make alone, so only the wall
time changes; results come back in job order.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from pipeline import calls


class Ask(Protocol):
    def __call__(self, role: str, prompt: str, schema: dict, *, system_prompt: Optional[str] = None,
                 replicate: int = 0) -> dict: ...


def make_ask(cfg: dict, *, ledger: Path, cache_dir: Optional[Path], max_budget_usd: float,
             before_call: Optional[Callable[[str, float], None]] = None) -> Ask:
    """`max_budget_usd` is the per-call cap, required so a stage-guarded ask always
    carries its stage's cap (eval.ledger.call_cap) rather than a silent global default."""

    def ask(role: str, prompt: str, schema: dict, *, system_prompt: Optional[str] = None,
            replicate: int = 0) -> dict:
        return calls.cached_call(cfg, role, prompt, schema=schema, max_budget_usd=max_budget_usd, ledger=ledger,
                                 cache_dir=cache_dir, system_prompt=system_prompt, before_call=before_call,
                                 replicate=replicate).output
    return ask


@dataclass
class Done:
    """A finished job: its value, or the exception it raised."""
    value: Any = None
    error: Optional[BaseException] = None


def fan_out(cfg: dict, jobs: Sequence[Callable[[], Any]]) -> list[Optional[Done]]:
    """Each job's outcome, in job order. Once a job raises, jobs not yet started are
    skipped (None), as a one-at-a-time loop would never reach them; jobs already
    running finish, since their calls may be paid. With eval.workers 1 it is that loop."""
    failed = threading.Event()

    def run(job: Callable[[], Any]) -> Optional[Done]:
        if failed.is_set():
            return None
        try:
            return Done(job())
        except BaseException as exc:
            failed.set()
            return Done(error=exc)
    with ThreadPoolExecutor(max_workers=cfg["eval"]["workers"]) as pool:
        return list(pool.map(run, jobs))


def values(outcomes: Sequence[Optional[Done]]) -> list:
    """The jobs' values; if any job raised, the first error in job order is raised."""
    for done in outcomes:
        if done is not None and done.error is not None:
            raise done.error
    return [done.value for done in outcomes]
