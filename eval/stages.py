"""What each stage runs (B.9). A stage is a list of jobs; `plan` builds them without
any model call, so `run --dry-run` can show every request, its cache state and the
worst-case spend.

pilot (#11 skeleton): each pilot transcript, prepared, through the extractor with the
CLI's default system prompt and with the minimal one (H-S2). Reference, matcher and
judge steps join the pilot when #15/#16 land.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from eval.frame import FrameItem, Sample
from pipeline import extract, prepare, prompts

DEFAULT_SYSTEM = "cli-default"
MINIMAL_SYSTEM = "minimal"


@dataclass
class Job:
    unit_id: str
    label: str
    role: str
    prepared: prepare.Prepared
    system_prompt: Optional[str]
    key: str


def _system_variants(cfg: dict) -> dict[str, Optional[str]]:
    return {DEFAULT_SYSTEM: None, MINIMAL_SYSTEM: prompts.load(cfg["prompts"]["system_minimal"])}


def plan(stage: str, cfg: dict, sample: Sample, frame: list[FrameItem]) -> list[Job]:
    if stage != "pilot":
        raise ValueError(f"stage {stage!r} is not built yet")
    by_id = {i.id: i for i in frame}
    jobs = []
    for unit_id in sample.pilot:
        prep = prepare.prepare([prepare.parse(Path(by_id[unit_id].path))], cfg)
        for label, system in _system_variants(cfg).items():
            req = extract.build_request(prep, cfg, role="extractor", system_prompt=system)
            jobs.append(Job(unit_id, label, "extractor", prep, system, req.key))
    return jobs
