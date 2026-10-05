"""What each stage runs (B.9). A stage is a list of jobs; `plan` builds them without
any model call, so `run --dry-run` can show every request, its cache state and the
worst-case spend.

pilot: each pilot transcript, prepared, through the extractor with the CLI's default
system prompt and with the minimal one (H-S2). These are the only calls known before
the run: the reference, judge and calibration calls depend on the extract outputs, so
eval/pilot.py makes them at run time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from eval.frame import FrameItem, Sample
from pipeline import extract, prepare, prompts

DEFAULT_SYSTEM = "cli-default"
MINIMAL_SYSTEM = "minimal"
EXTRACTOR = "extractor"          # the config `models` role every extract call runs on


@dataclass
class Job:
    unit_id: str
    label: str
    prepared: prepare.Prepared
    schema: dict        # the request's output schema: a cached entry is valid only against it
    key: str


def system_variants(cfg: dict) -> dict[str, Optional[str]]:
    """H-S2's two extract system prompts by label: the CLI default (None) and the minimal one."""
    return {DEFAULT_SYSTEM: None, MINIMAL_SYSTEM: prompts.load(cfg["prompts"]["system_minimal"])}


def plan(stage: str, cfg: dict, sample: Sample, frame: list[FrameItem]) -> list[Job]:
    if stage != "pilot":
        raise ValueError(f"stage {stage!r} is not built yet")
    by_id = {i.id: i for i in frame}
    jobs = []
    for unit_id in sample.pilot:
        prep = prepare.prepare([prepare.parse(Path(by_id[unit_id].path))], cfg)
        for label, system in system_variants(cfg).items():
            req = extract.build_request(prep, cfg, role=EXTRACTOR, system_prompt=system)
            jobs.append(Job(unit_id, label, prep, req.schema, req.key))
    return jobs
