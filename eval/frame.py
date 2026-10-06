"""Sample frame and seeded stratified draw (B.3).

Frame: every transcript dated on or before eval.sample.frame_cutoff. It is frozen to
<data_dir>/eval/frame-<cutoff>.json the first time it is built after the cutoff day
has ended (until then it is provisional: shown, never written, and a scored run
refuses it). Later loads verify every file's sha256, so a transcript edited after
freezing stops the harness. Each item's low-mic flag (eval.lowmic) is frozen with it.

Cells come from eval.sample.cells. "stub" means zero turns; otherwise the kind is
prepare.call_type (meeting or call), and duration (start to end, in minutes) picks
the cell by min_exclusive < duration <= max_inclusive.

Draw (seeded by eval.seed, stable across machines): within each cell, transcripts are
split by device, each group shuffled, and the groups interleaved starting with the
rarer device, so draws stay device-balanced (echo only occurs on speakers). The pilot
takes the first eligible transcript from eval.sample.pilot_n randomly chosen non-stub
cells and is excluded from the confirmatory draw. A low-mic transcript that would be
drawn is replaced by the next one in its cell and kept as a separately reported unit;
one whose mic coverage can't be checked (no recorder-log match) is replaced too and
only listed, since it can't be screened either way.

Tuning set (`Sample.dev`): after the draw, each cell's next eval.sample.cells[].dev
screened transcripts (not low-mic, not unscreened) in the same order. Prompt revisions
are tuned on it, so it never overlaps the pilot or the confirmatory draw that judges
the tuned prompt.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Optional

from eval import lowmic
from eval.ledger import eval_dir
from eval.records import DEVICE_OTHER, DEVICE_SPEAKER, Design, Unit
from pipeline import prepare
from whispr.fileio import atomic_write_text

STUB_KIND = "stub"
FROZEN_NOW, VERIFIED, PROVISIONAL = "frozen now", "verified against frozen hashes", "provisional"


class FrameError(RuntimeError):
    pass


@dataclass(frozen=True)
class FrameItem:
    id: str
    path: str
    sha256: str
    date: str
    end: str
    cell: str
    duration_min: Optional[float]
    device: str
    turns: int
    low_mic: Optional[bool] = None    # None: no recorder-log stop record to join (eval.lowmic)


@dataclass
class Sample:
    seed: int
    pilot: list[str]
    core: dict[str, list[str]]
    task_only: dict[str, list[str]]
    low_mic: dict[str, list[str]]          # cell -> low-mic transcripts that were drawn, then replaced
    unscreened: dict[str, list[str]] = field(default_factory=dict)   # no log match: drawn, then replaced
    shortfall: dict[str, int] = field(default_factory=dict)
    dev: dict[str, list[str]] = field(default_factory=dict)          # cell -> tuning-set transcripts


def _duration(meta: dict) -> Optional[float]:
    start, end = prepare.parse_iso(meta.get("start")), prepare.parse_iso(meta.get("end"))
    return None if start is None or end is None else (end - start).total_seconds() / 60


def classify(t: prepare.Transcript, cfg: dict) -> FrameItem:
    s = cfg["eval"]["sample"]
    device_rx = re.compile(s["speaker_device_pattern"])
    duration = _duration(t.meta)
    kind = prepare.call_type(t.meta) if t.turns else STUB_KIND
    matches = [c["name"] for c in s["cells"] if c["kind"] == kind and (kind == STUB_KIND or (
        duration is not None
        and (c["min_exclusive"] is None or duration > c["min_exclusive"])
        and (c["max_inclusive"] is None or duration <= c["max_inclusive"])))]
    if len(matches) != 1:
        raise FrameError(f"{t.path.name}: kind={kind} duration={duration} matches cells {matches}")
    device = DEVICE_SPEAKER if device_rx.search(str(t.meta.get("output_device") or "")) else DEVICE_OTHER
    return FrameItem(t.path.stem, str(t.path), t.sha256, str(t.meta.get("date")), str(t.meta.get("end")), matches[0],
                     duration, device, len(t.turns))


def frame_path(cfg: dict) -> Path:
    return eval_dir(cfg) / f"frame-{cfg['eval']['sample']['frame_cutoff']}.json"


def build_frame(cfg: dict) -> list[FrameItem]:
    cutoff = cfg["eval"]["sample"]["frame_cutoff"]
    items = []
    for path in sorted(Path(cfg["paths"]["transcripts"]).glob("*.md")):
        t = prepare.parse(path)
        if str(t.meta.get("date")) <= cutoff:
            items.append(classify(t, cfg))
    flags = lowmic.flags(items, cfg)
    return [replace(i, low_mic=flags[i.id]) for i in items]


def load_frame(cfg: dict, today: Optional[date] = None) -> tuple[list[FrameItem], str]:
    """(frame, FROZEN_NOW | VERIFIED | PROVISIONAL). Freezes on the first load after
    the cutoff day; afterwards verifies every hash."""
    path = frame_path(cfg)
    if not path.exists():
        items = build_frame(cfg)
        if (today or date.today()).isoformat() <= cfg["eval"]["sample"]["frame_cutoff"]:
            return items, PROVISIONAL
        atomic_write_text(path, json.dumps([asdict(i) for i in items], indent=1))
        return items, FROZEN_NOW
    items = [FrameItem(**d) for d in json.loads(path.read_text(encoding="utf-8"))]
    for item in items:
        p = Path(item.path)
        if not p.exists() or prepare.file_sha256(p) != item.sha256:
            raise FrameError(f"{item.id} changed or vanished since the frame was frozen ({path})")
    return items, VERIFIED


def _device_order(items: list[FrameItem], rng: random.Random) -> list[FrameItem]:
    groups = {d: sorted((i for i in items if i.device == d), key=lambda i: i.id)
              for d in (DEVICE_SPEAKER, DEVICE_OTHER)}
    for g in groups.values():
        rng.shuffle(g)
    first, second = sorted(groups.values(), key=len)   # rarer device first
    order = []
    for k in range(max(len(first), len(second))):
        order += [g[k] for g in (first, second) if k < len(g)]
    return order


def draw(frame: list[FrameItem], cfg: dict) -> Sample:
    s = cfg["eval"]["sample"]
    low_mic = {i.id for i in frame if i.low_mic}
    unscreened = {i.id for i in frame if i.low_mic is None}
    rng = random.Random(cfg["eval"]["seed"])
    orders = {c["name"]: _device_order([i for i in frame if i.cell == c["name"]], rng) for c in s["cells"]}

    pilot_cells = rng.sample(sorted(c["name"] for c in s["cells"] if c["kind"] != STUB_KIND), s["pilot_n"])
    pilot = []
    for cell in pilot_cells:
        pick = next((i for i in orders[cell] if i.low_mic is False), None)
        if pick is None:
            raise FrameError(f"no eligible pilot transcript in cell {cell}")
        pilot.append(pick.id)
        orders[cell].remove(pick)

    sample = Sample(cfg["eval"]["seed"], pilot, {}, {}, {})
    for c in s["cells"]:
        want = c["core"] + c["task_only"]
        chosen, replaced, skipped = [], [], []
        order = orders[c["name"]]
        for at, item in enumerate(order + [None]):      # `at`: the first transcript the draw left
            if len(chosen) == want or item is None:
                break
            (replaced if item.id in low_mic else skipped if item.id in unscreened else chosen).append(item.id)
        sample.dev[c["name"]] = [i.id for i in order[at:] if i.id not in low_mic and i.id not in unscreened][:c["dev"]]
        sample.core[c["name"]] = chosen[:c["core"]]
        sample.task_only[c["name"]] = chosen[c["core"]:]
        sample.low_mic[c["name"]] = replaced
        sample.unscreened[c["name"]] = skipped
        if len(chosen) < want:
            sample.shortfall[c["name"]] = want - len(chosen)
    return sample


def design(sample: Sample, frame: list[FrameItem], *, include_task_only: bool, low_mic: bool = False,
           dev: bool = False) -> Design:
    """A scoring design. The primary one: core units, plus task-only units for task
    metrics. With low_mic=True, the drawn-then-replaced low-mic units alone, reported
    separately (B.3). Low-mic transcripts are excluded from the primary analysis (B.3),
    so each cell's frame size counts the subpopulation its units stand for: the cell's
    low-mic transcripts for the low-mic design, all the others (unscreened included)
    for the primary one, and the two partition the frame. They are never mixed:
    Design.weight divides a cell's frame size by every unit of that cell in the design."""
    by_id = {i.id: i for i in frame}
    if dev:                                  # the tuning set: screened units, like the primary design
        ids = [x for cell in sample.dev.values() for x in cell]
    elif low_mic:
        ids = [x for cell in sample.low_mic.values() for x in cell]
    else:
        ids = [x for cell in sample.core.values() for x in cell]
        if include_task_only:
            ids += [x for cell in sample.task_only.values() for x in cell]
    units = tuple(Unit(x, by_id[x].cell, by_id[x].device, low_mic) for x in ids)
    frame_n: dict[str, int] = {}
    for i in frame:
        if bool(i.low_mic) == low_mic:
            frame_n[i.cell] = frame_n.get(i.cell, 0) + 1
    return Design(frame_n, units)
