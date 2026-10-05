"""Dual-stream audio capture.

Records two mono 16 kHz streams to disk incrementally:
  * "Me"     — the microphone, via `sounddevice` (soundcard cannot open the
               Plantronics mic on this machine; sounddevice can).
  * "Others" — Teams' actual render endpoint, via `soundcard` WASAPI loopback.

Teams does not render call audio to the default Windows output device on this
machine; it uses a machine-specific endpoint (e.g. SoundWire). We therefore
*probe* all active loopback endpoints at call start and *lock* the one carrying
audio, and an RMS watchdog re-probes if that endpoint goes quiet while a call is
active (covers device switches / dock changes mid-call).

Writing streams to disk incrementally means a crash or forced stop leaves a
playable partial recording rather than nothing.
"""

from __future__ import annotations

import math
import threading
import time
import wave
from typing import Optional

import numpy as np
import sounddevice as sd
import soundcard as sc

from whispr.log import get_logger
from whispr.models import CaptureResult
from whispr.winutil import com_initialized

log = get_logger("capture")

# Capture chunk in frames. 0.1 s at 16 kHz keeps disk writes frequent (small loss
# window on crash) without excessive syscall overhead.
_CHUNK_DIVISOR = 10

_SLEEP_STEP = 0.1         # interruptible sleep granularity (seconds)
_THREAD_JOIN_TIMEOUT = 5.0


def _dbfs(rms: float) -> float:
    """Convert a linear RMS (float samples in [-1, 1]) to dBFS. Floors at -180."""
    if rms <= 1e-9:
        return -180.0
    return 20.0 * math.log10(rms)


def _rms(arr: np.ndarray) -> float:
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(arr, dtype=np.float64))))


def resolve_mic(name_match: str, quiet: bool = False) -> tuple[Optional[int], Optional[str]]:
    """Return (device_index, device_name) of the first input device whose name
    contains `name_match` (case-insensitive); else the system default input.
    `quiet` demotes the choice to DEBUG for the mic-recovery retry loop."""
    say = log.debug if quiet else log.info
    warn = log.debug if quiet else log.warning
    try:
        devices = sd.query_devices()
    except Exception as exc:  # pragma: no cover - depends on host audio stack
        warn("could not query input devices: %s", exc)
        return None, None

    needle = name_match.lower()
    for idx, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) > 0 and needle in dev.get("name", "").lower():
            say("mic matched by name: [%d] %s", idx, dev["name"])
            return idx, dev["name"]

    # Fall back to default input device.
    try:
        default_in = sd.default.device[0]
        if default_in is not None and default_in >= 0:
            name = sd.query_devices(default_in)["name"]
            say("mic name %r not found; using default input [%d] %s", name_match, default_in, name)
            return default_in, name
    except Exception as exc:  # pragma: no cover
        warn("no default input device: %s", exc)
    return None, None


def _refresh_devices() -> None:
    """Re-enumerate PortAudio devices.

    PortAudio snapshots the device list when it initializes. After a device is
    added or removed (dock, headset, Bluetooth), every cached index can be stale:
    reopening by index fails with MME 'device ID out of range' or 'no driver
    installed' until the list is rebuilt. Only the mic uses PortAudio (loopback is
    soundcard/WASAPI), and the dead stream is closed before this runs.
    """
    try:
        sd._terminate()
        sd._initialize()
    except Exception as exc:  # pragma: no cover - host dependent
        log.debug("PortAudio re-initialize failed: %s", exc)


def _loopback_endpoints() -> list:
    """All loopback (render) capture endpoints known to soundcard."""
    try:
        return [m for m in sc.all_microphones(include_loopback=True) if m.isloopback]
    except Exception as exc:  # pragma: no cover
        log.warning("could not enumerate loopback endpoints: %s", exc)
        return []


def _measure_endpoint(endpoint, samplerate: int, seconds: float) -> float:
    """Record `seconds` from one loopback endpoint and return its dBFS. -180 on error."""
    try:
        frames = int(samplerate * seconds)
        with endpoint.recorder(samplerate=samplerate, channels=1) as rec:
            data = rec.record(numframes=frames)
        mono = data[:, 0] if data.ndim > 1 else data
        return _dbfs(_rms(mono))
    except Exception as exc:  # pragma: no cover - hardware dependent
        log.debug("probe failed on %s: %s", getattr(endpoint, "name", "?"), exc)
        return -180.0


def probe_loopback(cfg: dict, exclude_name: Optional[str] = None):
    """Probe all loopback endpoints in parallel and choose the best one.

    Selection order:
      1. Endpoint with the highest dBFS above `probe_dbfs_floor`.
      2. If none is above the floor, the endpoint whose name contains
         `loopback_name_hint` (the configured Teams-render hint).
      3. Otherwise the default speaker's loopback.

    Returns (endpoint, name, dbfs) or (None, None, -180) if nothing is available.
    `exclude_name` lets the watchdog avoid re-locking the endpoint that just went
    silent when an equally-silent alternative exists.
    """
    audio_cfg = cfg["audio"]
    samplerate = audio_cfg["samplerate"]
    seconds = audio_cfg["probe_seconds"]
    floor = audio_cfg["probe_dbfs_floor"]
    hint = audio_cfg["loopback_name_hint"].lower()

    endpoints = _loopback_endpoints()
    if not endpoints:
        return None, None, -180.0

    results: dict[int, float] = {}
    threads: list[threading.Thread] = []

    def worker(i: int, ep) -> None:
        # soundcard uses Media Foundation / COM; each thread touching it must init COM.
        with com_initialized():
            results[i] = _measure_endpoint(ep, samplerate, seconds)

    for i, ep in enumerate(endpoints):
        t = threading.Thread(target=worker, args=(i, ep), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=seconds + 5.0)

    measured = [(endpoints[i], endpoints[i].name, results.get(i, -180.0)) for i in range(len(endpoints))]
    for _, name, db in sorted(measured, key=lambda x: x[2], reverse=True):
        log.info("probe: %s = %.1f dBFS", name, db)

    # 1. Loudest above floor (respecting exclude on ties of silence).
    above = [m for m in measured if m[2] > floor and m[1] != exclude_name]
    if above:
        best = max(above, key=lambda x: x[2])
        log.info("locked loopback (energy): %s @ %.1f dBFS", best[1], best[2])
        return best

    # 2. Name-hint match.
    for ep, name, db in measured:
        if hint and hint in name.lower():
            log.info("locked loopback (name hint %r): %s", audio_cfg["loopback_name_hint"], name)
            return ep, name, db

    # 3. Default speaker loopback.
    try:
        spk = sc.default_speaker()
        for ep, name, db in measured:
            if spk.name in name:
                log.info("locked loopback (default speaker): %s", name)
                return ep, name, db
    except Exception:  # pragma: no cover
        pass

    # 4. Last resort: loudest of whatever we have.
    best = max(measured, key=lambda x: x[2])
    log.info("locked loopback (fallback loudest): %s @ %.1f dBFS", best[1], best[2])
    return best


class _WavWriter:
    """Incremental mono WAV writer. Converts float32 [-1,1] to int16 PCM on write.

    Thread-safe; `frames_written` lets callers tell whether a stream captured
    anything. Writing chunk-by-chunk keeps a partial file valid if we crash.
    """

    def __init__(self, path: str, samplerate: int) -> None:
        self._path = path
        self._wf = wave.open(path, "wb")
        self._wf.setnchannels(1)
        self._wf.setsampwidth(2)  # int16
        self._wf.setframerate(samplerate)
        self._lock = threading.Lock()
        self.frames_written = 0

    def write_float(self, arr: np.ndarray) -> None:
        if arr.size == 0:
            return
        pcm = (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        with self._lock:
            self._wf.writeframes(pcm)
            self.frames_written += arr.size

    def close(self) -> None:
        with self._lock:
            try:
                self._wf.close()
            except Exception as exc:  # pragma: no cover
                log.debug("WAV close failed (%s): %s", self._path, exc)


class DualStreamRecorder:
    """Records mic + loopback to two WAV files until stop() is called.

    Usage:
        rec = DualStreamRecorder(cfg, mic_wav_path, loopback_wav_path)
        rec.start()   # returns once both streams are running
        ...
        info = rec.stop()   # returns dict of resolved device names + frame counts
    """

    def __init__(self, cfg: dict, mic_wav: str, loopback_wav: str) -> None:
        self._cfg = cfg
        self._sr = cfg["audio"]["samplerate"]
        self._chunk = max(1, self._sr // _CHUNK_DIVISOR)
        self._mic_wav = mic_wav
        self._loopback_wav = loopback_wav

        self._running = threading.Event()
        self._mic_thread: Optional[threading.Thread] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None

        self._mic_writer: Optional[_WavWriter] = None
        self._loop_writer: Optional[_WavWriter] = None

        self.mic_device_name: Optional[str] = None
        self.loopback_device_name: Optional[str] = None

        # Shared loopback state (guarded by _lock).
        self._lock = threading.Lock()
        self._current_endpoint = None
        self._reprobe_requested = False
        self._recent_loop_dbfs = -180.0
        # Once the locked endpoint has carried real audio, we trust it for the rest
        # of the call and never switch — otherwise natural far-end pauses (or two
        # endpoints that both carry the call) cause endless endpoint ping-ponging
        # that fragments the stream. Re-probe only rescues a wrong/dead initial lock.
        self._loop_had_audio = False

        # Mic-stream health, written only by the mic thread, read after it joins.
        self._mic_dropouts = 0
        self._mic_lost_seconds = 0.0

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._running.set()

        mic_idx, self.mic_device_name = resolve_mic(self._cfg["audio"]["mic_name_match"])
        endpoint, self.loopback_device_name, db = probe_loopback(self._cfg)
        self._current_endpoint = endpoint

        self._mic_writer = _WavWriter(self._mic_wav, self._sr)
        self._loop_writer = _WavWriter(self._loopback_wav, self._sr)

        self._mic_thread = threading.Thread(target=self._mic_loop, args=(mic_idx,), daemon=True)
        self._loop_thread = threading.Thread(target=self._loopback_loop, daemon=True)
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._mic_thread.start()
        self._loop_thread.start()
        self._watchdog_thread.start()
        log.info("recording started (mic=%s, loopback=%s)", self.mic_device_name, self.loopback_device_name)

    def stop(self) -> CaptureResult:
        self._running.clear()
        for t in (self._mic_thread, self._loop_thread, self._watchdog_thread):
            if t is not None:
                t.join(timeout=_THREAD_JOIN_TIMEOUT)
        mic_frames = self._mic_writer.frames_written if self._mic_writer else 0
        loop_frames = self._loop_writer.frames_written if self._loop_writer else 0
        if self._mic_writer:
            self._mic_writer.close()
        if self._loop_writer:
            self._loop_writer.close()
        log.info("recording stopped (mic frames=%d, loopback frames=%d)", mic_frames, loop_frames)
        return CaptureResult(
            mic_device=self.mic_device_name,
            output_device=self.loopback_device_name,
            mic_frames=mic_frames,
            loopback_frames=loop_frames,
            mic_dropouts=self._mic_dropouts,
            mic_lost_seconds=round(self._mic_lost_seconds, 1),
        )

    def _sleep(self, seconds: float) -> None:
        """Sleep up to `seconds`, waking early if recording has stopped.

        NOTE: do NOT use self._running.wait() to pace loops — `_running` is *set*
        while recording, so .wait() returns immediately and the loop busy-spins
        (this once storming the audio stack into a deadlock).
        """
        remaining = seconds
        while remaining > 0 and self._running.is_set():
            time.sleep(min(_SLEEP_STEP, remaining))
            remaining -= _SLEEP_STEP

    # -- stream loops ----------------------------------------------------

    def _mic_loop(self, mic_idx: Optional[int]) -> None:
        """Capture the mic until stop, reopening it whenever the stream is lost.

        Before 2026-10 a single error ended "Me" capture for the rest of the call:
        18 calls lost the user's own voice, mostly within seconds of the start.
        """
        retry_seconds = self._cfg["audio"]["mic_retry_seconds"]
        lost_at: Optional[float] = None  # monotonic time the stream was lost
        while self._running.is_set():
            if mic_idx is not None:
                try:
                    with sd.InputStream(
                        device=mic_idx, samplerate=self._sr, channels=1, dtype="float32"
                    ) as stream:
                        if lost_at is not None:
                            gap = self._pad_mic_gap(lost_at)
                            log.info("mic recovered on %s after %.1fs; gap padded with silence",
                                     self.mic_device_name, gap)
                            lost_at = None
                        while self._running.is_set():
                            data, _overflowed = stream.read(self._chunk)
                            self._mic_writer.write_float(data[:, 0])
                        return
                except Exception as exc:
                    if lost_at is None:
                        lost_at = time.monotonic()
                        self._mic_dropouts += 1
                        log.error("mic capture failed: %s; retrying every %gs", exc, retry_seconds)
                    else:
                        log.debug("mic reopen failed: %s", exc)
            elif lost_at is None:
                lost_at = time.monotonic()
                self._mic_dropouts += 1
                log.warning("no mic device resolved; retrying every %gs", retry_seconds)
            self._sleep(retry_seconds)
            if not self._running.is_set():
                break
            _refresh_devices()
            mic_idx, name = resolve_mic(self._cfg["audio"]["mic_name_match"], quiet=True)
            if name:
                self.mic_device_name = name
        if lost_at is not None:
            self._mic_lost_seconds += time.monotonic() - lost_at

    def _pad_mic_gap(self, lost_at: float) -> float:
        """Write silence for the time the mic was down, so later "Me" turns keep
        their true offsets relative to "Others". Returns the gap in seconds."""
        gap = time.monotonic() - lost_at
        self._mic_lost_seconds += gap
        remaining = int(gap * self._sr)
        block = np.zeros(self._sr, dtype="float32")  # 1 s per write bounds memory
        while remaining > 0:
            n = min(remaining, block.size)
            self._mic_writer.write_float(block[:n])
            remaining -= n
        return gap

    def _loopback_loop(self) -> None:
        with com_initialized():
            self._loopback_loop_body()

    def _loopback_loop_body(self) -> None:
        # Consecutive open/capture failures on the current endpoint. After
        # _MAX_OPEN_FAILURES we re-probe excluding it rather than retrying a dead
        # endpoint (a transient WASAPI open error shouldn't cost 30s of far-end
        # audio waiting for the silence watchdog).
        open_failures = 0
        # Endpoints already tried and found dead during initial-lock rescue. Once
        # every candidate has been tried, we stop switching and just keep retrying
        # the current endpoint — prevents infinite ping-pong between two endpoints
        # that both error while idle (no call audio yet).
        tried_dead: set[str] = set()
        while self._running.is_set():
            with self._lock:
                endpoint = self._current_endpoint
                self._reprobe_requested = False
            if endpoint is None:
                log.warning("no loopback endpoint; attempting probe")
                ep, name, _ = probe_loopback(self._cfg)
                with self._lock:
                    self._current_endpoint = ep
                    self.loopback_device_name = name
                if ep is None:
                    self._sleep(1.0)
                    continue
                endpoint = ep
            try:
                with endpoint.recorder(samplerate=self._sr, channels=1) as rec:
                    open_failures = 0  # opened successfully
                    while self._running.is_set():
                        with self._lock:
                            if self._reprobe_requested:
                                break
                        data = rec.record(numframes=self._chunk)
                        mono = data[:, 0] if data.ndim > 1 else data
                        self._loop_writer.write_float(mono)
                        db = _dbfs(_rms(mono))
                        with self._lock:
                            self._recent_loop_dbfs = db
                            if db > self._cfg["audio"]["watchdog_dbfs_floor"]:
                                self._loop_had_audio = True
            except Exception as exc:
                open_failures += 1
                log.error("loopback capture failed on %s (attempt %d): %s",
                          self.loopback_device_name, open_failures, exc)
                self._sleep(1.0)
                # Only switch endpoints for a bad INITIAL lock (never carried audio).
                # After the endpoint has produced audio, a transient hiccup retries
                # the same endpoint rather than switching (which fragments the stream).
                with self._lock:
                    had_audio = self._loop_had_audio
                if not had_audio and open_failures >= self._cfg["audio"].get("max_open_failures", 2) and self._running.is_set():
                    failed_name = self.loopback_device_name
                    if failed_name:
                        tried_dead.add(failed_name)
                    ep, name, _ = probe_loopback(self._cfg, exclude_name=failed_name)
                    # Only switch to a candidate we haven't already found dead; once
                    # all are exhausted, stick with the current one and keep retrying.
                    if ep is not None and name != failed_name and name not in tried_dead:
                        log.info("loopback endpoint %s never carried audio; switching to %s", failed_name, name)
                        with self._lock:
                            self._current_endpoint = ep
                            self.loopback_device_name = name
                        open_failures = 0

    def _watchdog_loop(self) -> None:
        with com_initialized():
            self._watchdog_loop_body()

    def _watchdog_loop_body(self) -> None:
        floor = self._cfg["audio"]["watchdog_dbfs_floor"]
        silence_limit = self._cfg["audio"]["watchdog_silence_seconds"]
        silent_for = 0.0
        while self._running.is_set():
            self._sleep(1.0)
            with self._lock:
                db = self._recent_loop_dbfs
                current_name = self.loopback_device_name
                had_audio = self._loop_had_audio
            # Once the locked endpoint has carried real audio, trust it for the rest
            # of the call — natural far-end pauses are not a reason to switch. The
            # watchdog only rescues an endpoint that never produced any audio.
            if had_audio:
                continue
            if db < floor:
                silent_for += 1.0
            else:
                silent_for = 0.0
            if silent_for >= silence_limit:
                log.info("loopback silent %.0fs (<%.0f dBFS); re-probing", silent_for, floor)
                silent_for = 0.0
                ep, name, ndb = probe_loopback(self._cfg, exclude_name=current_name)
                if ep is not None and name != current_name:
                    log.info("watchdog switching loopback %s -> %s", current_name, name)
                    with self._lock:
                        self._current_endpoint = ep
                        self.loopback_device_name = name
                        self._reprobe_requested = True
