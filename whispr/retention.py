"""Audio retention: the ASR-study keep window and the hard max-age purge.

Two config keys under `retention` (see config.yaml):
- `keep_audio_until` — until this date, a successful transcription keeps its WAVs
  instead of deleting them (docs/plan/F-asr-live.md F.1). Null means no window.
- `audio_max_age_days` — `python -m whispr purge-audio` deletes any WAV pair older
  than this whose call has a transcript.

A WAV pair with no transcript is never deleted (lesson L14): it is a call whose
transcription never finished, and deleting it would lose the call. The purge
reports it and exits 1, so the caller (the freshness check) fails loudly until a
human resolves it.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from whispr.incidents import record_incident

WAV_STAMP_FORMAT = "%Y-%m-%d-%H%M%S"
_STREAM_SUFFIXES = ("-mic.wav", "-loopback.wav")
_FRONTMATTER_START_RE = re.compile(r"^start:\s*['\"]?([^'\"\s]+)")


def wav_stamp(session_start: datetime) -> str:
    """The per-session prefix shared by a call's two WAV files."""
    return session_start.strftime(WAV_STAMP_FORMAT)


def keeping_audio(retention: dict[str, Any], today: date) -> bool:
    """True while the ASR-study window is open (through `keep_audio_until`, inclusive)."""
    until = retention.get("keep_audio_until")
    if not until:
        return False
    return today <= date.fromisoformat(str(until))


def transcript_stamps(transcripts_dir: Path) -> set[str]:
    """WAV stamps of every call that has a transcript, read from frontmatter `start:`."""
    stamps: set[str] = set()
    for path in Path(transcripts_dir).glob("*.md"):
        with open(path, encoding="utf-8") as fh:
            if fh.readline().strip() != "---":
                continue
            for line in fh:
                if line.strip() == "---":
                    break
                match = _FRONTMATTER_START_RE.match(line)
                if match:
                    try:
                        stamps.add(wav_stamp(datetime.fromisoformat(match.group(1))))
                    except ValueError:
                        pass
                    break
    return stamps


def wav_groups(recordings_dir: Path) -> dict[str, list[Path]]:
    """WAV files grouped by session stamp. Files that don't match the naming are ignored."""
    groups: dict[str, list[Path]] = {}
    for path in Path(recordings_dir).glob("*.wav"):
        for suffix in _STREAM_SUFFIXES:
            if path.name.endswith(suffix):
                groups.setdefault(path.name[: -len(suffix)], []).append(path)
    return groups


def _stamp_time(stamp: str) -> Optional[datetime]:
    try:
        return datetime.strptime(stamp, WAV_STAMP_FORMAT)
    except ValueError:
        return None


def purge_audio(cfg: dict[str, Any], now: datetime, check_only: bool = False) -> tuple[int, list[str]]:
    """Delete overdue WAV pairs that have a transcript; report everything still overdue.

    Returns (exit_code, report_lines). exit_code is 0 when no overdue audio remains,
    1 otherwise. With check_only, nothing is deleted, so an overdue pair with a
    transcript also counts as remaining (the purge has not run).
    """
    max_age = timedelta(days=cfg["retention"]["audio_max_age_days"])
    have_transcript = transcript_stamps(cfg["paths"]["transcripts"])
    remaining: list[str] = []
    report: list[str] = []
    for stamp, paths in sorted(wav_groups(cfg["paths"]["recordings"]).items()):
        started = _stamp_time(stamp)
        if started is None or now - started <= max_age:
            continue
        if stamp not in have_transcript:
            remaining.append(f"{stamp}: no transcript (untranscribed call) — kept; transcribe or delete it by hand")
            continue
        if check_only:
            remaining.append(f"{stamp}: has a transcript but was not purged")
            continue
        failed = []
        for path in paths:
            try:
                path.unlink()
            except OSError as exc:
                failed.append(f"{path.name} ({exc})")
        if failed:
            remaining.append(f"{stamp}: delete failed: {', '.join(failed)}")
        else:
            report.append(f"{stamp}: deleted ({len(paths)} file(s))")
            record_incident(cfg, "audio-purged", stamp=stamp, files=len(paths))
    report.extend(f"OVERDUE {line}" for line in remaining)
    if not report:
        report.append("no audio older than the retention limit")
    return (1 if remaining else 0), report
