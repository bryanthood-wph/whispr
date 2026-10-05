"""Shared data contracts passed between whispr modules.

These dataclasses are the stable interface every module builds against. Keep field
names stable — output.py serializes several of them into frontmatter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

CallType = Literal["meeting", "call"]
MetadataSource = Literal["outlook", "window-title", "none"]
Speaker = Literal["Me", "Others"]

# Marks a transcript whose topic/summary placeholder has not yet been filled by
# the local summary agent. output.py writes it; summarize.py scans for it.
PENDING_SUMMARY_TOPIC = "unsorted"


@dataclass
class CallSession:
    """One recording session, from trigger to stop.

    Created by the watcher when a call/meeting starts; carried through capture,
    metadata, transcription, and output.
    """

    call_type: CallType
    window_title: str                 # raw Teams window title at trigger time
    start: datetime                   # tz-aware local start
    end: Optional[datetime] = None    # tz-aware local end, set on stop
    mic_wav: Optional[str] = None     # path to the "Me" stream WAV
    loopback_wav: Optional[str] = None  # path to the "Others" stream WAV
    mic_device: Optional[str] = None  # resolved input device name
    output_device: Optional[str] = None  # locked loopback endpoint name
    partial: bool = False             # True if recording was cut off (crash/kill/sleep)

    @property
    def duration_min(self) -> int:
        if self.end is None:
            return 0
        return max(0, round((self.end - self.start).total_seconds() / 60))


@dataclass
class CallMetadata:
    """Calendar/context metadata for a session. Best-effort; fields may be None."""

    source: MetadataSource = "none"
    call_title: Optional[str] = None
    organizer: Optional[str] = None
    attendees: list[str] = field(default_factory=list)
    invite_notes: Optional[str] = None


@dataclass
class Turn:
    """One speaker turn in the merged transcript."""

    start_seconds: float              # offset from session start
    speaker: Speaker
    text: str


@dataclass
class TranscriptResult:
    """Output of the transcription stage: merged, ordered turns plus stub flag."""

    turns: list[Turn] = field(default_factory=list)
    is_stub: bool = False             # True when no speech was detected in either stream


@dataclass
class CaptureResult:
    """Returned by DualStreamRecorder.stop(). Field names mirror CallSession."""

    mic_device: Optional[str]
    output_device: Optional[str]      # locked loopback endpoint name
    mic_frames: int
    loopback_frames: int
    mic_dropouts: int = 0             # times the mic stream was lost (failed open or died)
    mic_lost_seconds: float = 0.0     # wall-clock seconds of "Me" audio not captured
