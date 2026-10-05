"""The eval's model access: one `ask` callable that every stage receives.

    ask(role, prompt, schema, *, system_prompt=None, replicate=0) -> dict

returns the schema-valid structured output for `role` (a config `models` key). Stage
logic (reference consensus, judging) takes `ask` as a parameter, so it is tested with
a plain function and run for real with `make_ask`, which binds pipeline.calls.cached_call
to the run's ledger, cache and budget guard. Nothing else in eval/ calls a model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Protocol

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
