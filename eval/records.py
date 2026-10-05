"""Record types shared by every eval stage (B.3, B.6, B.7).

The sample, reference, judging and scoring code all exchange these, so a stage
can be built and tested on its own. Ids are transcript file stems, which stay
stable across runs; content changes are caught by the frozen frame's sha256.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

DEVICE_SPEAKER = "speaker"   # output device matches eval.sample.speaker_device_pattern (echo-prone)
DEVICE_OTHER = "other"


@dataclass(frozen=True)
class Unit:
    """One sampled transcript: the cluster that the bootstrap resamples."""
    id: str
    cell: str        # an eval.sample.cells[].name
    device: str      # DEVICE_SPEAKER | DEVICE_OTHER
    low_mic: bool    # excluded from the primary my-task analysis (B.3)


@dataclass(frozen=True)
class Design:
    """The scored sample plus the frame size of each cell, for design weights."""
    frame_n: dict[str, int]
    units: tuple[Unit, ...]

    def weight(self, unit: Unit) -> float:
        """Frame N / sample n for the unit's cell: how many frame transcripts it stands for."""
        sampled = Counter(u.cell for u in self.units)[unit.cell]
        return self.frame_n[unit.cell] / sampled


@dataclass(frozen=True)
class Outcome:
    """One scored decision about one item, under one config."""
    unit_id: str
    config: str      # "A", "B-raw", "B", "C", "D", "E"
    metric: str      # a B.6 metric, e.g. "my_task_recall"
    item_id: str
    value: float     # 1 hit, 0 miss, 0.5 partial (B.5 "partial")
