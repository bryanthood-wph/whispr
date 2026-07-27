# whispr — Review-Findings Fix Plan

Source: consolidated maintainability review (4 zones, 2026-07-13). 17 accepted findings.
Decisions locked via Q&A: (Q1) `source: meeting` stays for all transcripts — named
constant + comment; (Q2) `instrument_titles.py` kept as rerunnable diagnostic — extract
shared helpers; (Q3) CLI renamed `list-pending`, `retry-summary` kept as deprecated
alias; (Q4) config.yaml gets only the true tunables, borderline literals become named
module constants.

**Plan only — no code changed yet.** Batches are ordered so correctness-adjacent fixes
land first; each batch ends with a verification step. Findings reference the review
report numbering (F1–F17).

**Hard constraint carried from the review:** do NOT restructure `capture.py`'s endpoint
selection logic (probe / rescue / `_loop_had_audio` latch / `_sleep`) — it encodes three
live-verified fixes. Batch D touches it only to rename constants and add a phase comment.

---

## Batch A — State-desync & callback guards (F1, F2) — the two HIGH findings

**A1. Reset `watcher._recording` on every stop path.**
- `watcher.py`: add `notify_stopped()` — acquires `_state_lock`, sets `_recording = False`,
  idempotent (safe when the stop originated in `_do_stop`, which already reset it).
- `__main__.py::Orchestrator._finish`: after `recorder.stop()`, call
  `self._watcher.notify_stopped()` (guard `self._watcher is not None` — `record-test`
  path has no watcher).
- Why not a full `RecordingState` object: rejected in review adjudication (over-built for
  this tool; risks regressing the live-verified state machine).

**A2. Guard tray menu callbacks like watcher callbacks.**
- `tray.py::_handle_stop_keep/_handle_stop_discard/_handle_quit`: wrap callback invocation
  in `try/except Exception: log.exception(...)`; put the state-restoring calls
  (`set_idle()` / `stop()`) in `finally` so a raising orchestrator can't strand the tray.
  Mirror the comment style of `watcher.py`'s "never let an exception escape into the OS
  callback".

**Verify (A):** unit — new test drives `notify_stopped()` idempotency and a raising
callback through a `TrayController` with stub callbacks (no icon run). Live — one quick
call: tray "Stop & keep" mid-call, then immediately start a second call; second call must
trigger recording (this is the exact bug F1 predicts).

## Batch B — Sentinel constant, CLI rename, logger naming (F3, F5, F12)

**B1. Single `"unsorted"` sentinel.**
- `models.py`: `PENDING_SUMMARY_TOPIC = "unsorted"` (with comment: marks a transcript
  awaiting the local summary agent; scanner regex derives from it).
- `output.py`: build the topic placeholder from it. `summarize.py`: build
  `_UNSUMMARIZED_RE` from `re.escape(PENDING_SUMMARY_TOPIC)`.
- `tests/test_core.py::test_placeholder_contract` keeps locking the cross-module contract
  (now via the shared constant — update assertions to reference it).

**B2. CLI verb rename with alias (per Q3).**
- `__main__.py`: dispatch `list-pending` as the primary verb; `retry-summary` still
  dispatches but prints one deprecation line first. Fix the module docstring line
  ("backfill summaries" → "list transcripts awaiting a local summary").
- Module file stays `summarize.py` for now (P6 smallest-change; its docstring is already
  accurate). File rename to `pending.py` noted as optional follow-up, not in this plan.
- Grep `SUMMARY_AGENT.md` / PROGRESS.md for `retry-summary` and update mentions.

**B3. Logger consistency.**
- Rename `logger` → `log` in `output.py`, `transcribe.py`, `tray.py` (~15 call sites).
- `summarize.py`: add `log = get_logger("summarize")`; keep `print` for CLI output but
  also `log.warning` on the `except OSError` path.

**Verify (B):** full unit suite; `python -m whispr list-pending` and `retry-summary`
both run against the real transcripts dir (read-only command).

## Batch C — metadata dedup, COM consistency, watcher hardening (F4, F7a, F10, F16)

**C1. Extract the shared Outlook pipeline (`metadata.py`).**
- New helpers: `_connect_outlook(retries: int)` (Dispatch with retry, returns app or
  None) and `_restricted_calendar_items(ns, win_start, win_end, max_items)` (folder,
  `IncludeRecurrences`, `Sort`, `Restrict`, capped iteration — yields appointments).
- `_fetch_from_outlook` and `is_live_meeting` both consume them. Retry policy stays a
  parameter: fetch uses `metadata.outlook_connect_retries` (config, Batch D),
  `is_live_meeting` passes 1 (fail-fast on the recording-start hot path — preserve this).
- Both functions switch to `with com_initialized():` (F10, metadata half).
- Bump `could not read recipients` / `could not read body` from `log.debug` to
  `log.warning` (F16 — user-visible frontmatter degradation).

**C2. Watcher COM + backstop hardening.**
- `_run` and `_audio_backstop_loop` switch to `with com_initialized():` (F10, watcher
  half). `_run`'s early-return-on-hook-failure works unchanged under the context manager.
- Extract pure `_backstop_step(state, window_exists, audio_active, cfg) ->
  (state, should_stop)` from `_audio_backstop_loop_body`; loop becomes: poll inputs →
  step → act. Preserves exact current semantics (audio_seen latch, ≥2-poll debounce).
- Add per-iteration `try/except Exception: log.exception(...)` around the poll body
  (mirrors `TranscriptionQueue._run`) so the backstop thread can't die silently (F7a).

**C3. `_WavWriter.close()`:** log swallowed close failures at `debug` (store path on the
instance for the message) — removes the codebase's only silent `except: pass`.

**Verify (C):** unit — new `_backstop_step` tests (Batch E3) prove semantics preserved;
metadata refactor proven by existing behavior test plus one live meeting later. Smoke —
`record-test 3`. Live gate note: next real meeting confirms Outlook metadata still
matches (log line "matched calendar item").

## Batch D — Config homes & named constants (F11, F13, + Q1/Q4 decisions)

**D1. config.yaml — the four true tunables (per Q4):**
```yaml
trigger:
  window_gone_poll_seconds: 2.0      # backstop poll cadence
  window_gone_confirm_polls: 2       # debounce: consecutive no-window polls before stop
metadata:
  max_calendar_items: 200            # Restrict-iteration safety cap
  outlook_connect_retries: 3
audio:
  max_open_failures: 2               # loopback open failures before re-probe
```
Code reads them where the literals live today (`watcher.py:395,410`, `metadata.py:68,102,199`,
`capture.py:41`).

**D2. Borderline literals → named module constants with one-line comments (per Q4):**
- `capture.py`: `_CHUNK_DIVISOR` (exists — add comment linking to chunk seconds),
  `_SLEEP_STEP = 0.1`, `_THREAD_JOIN_TIMEOUT = 5.0`.
- `watcher.py`: reuse `_THREAD_JOIN_TIMEOUT` pattern (own constant — no cross-module
  import for a timeout).
- `__main__.py`: `_QUEUE_DRAIN_TIMEOUT = 600.0`, `_TRAY_LABEL_MAX = 60`; mutex name is
  already a named constant — add a comment noting it's deliberately fixed.
- `output.py`: `_SLUG_MAX = 60`; `VAULT_SOURCE = "meeting"` with comment: vault taxonomy
  note-type per cohoodOBS schema — intentionally constant for calls AND meetings; distinct
  from `call_type` (Q1 decision).

**D3. `CaptureResult` dataclass (F13).**
- `models.py`: `CaptureResult(mic_device, output_device, mic_frames, loopback_frames)` —
  field names mirror `CallSession` (kills the three-name drift for the same concept).
- `capture.DualStreamRecorder.stop()` returns it; `__main__._finish` and `_record_test`
  consume attributes instead of `.get()` on a dict.

**Verify (D):** unit suite; `record-test 3` (exercises CaptureResult + capture constants);
config sanity — grep confirms every new config key has exactly one reader.

## Batch E — Test additions (F7b, F8, F9, F14, F17)

**E1. Isolate test output (F8):** `setUpModule` swaps `CFG["paths"]["transcripts"]` to a
`tempfile.TemporaryDirectory()`; tearDown restores + cleans. No test touches the real
transcripts dir again regardless of outcome.

**E2. `TestMergeTurns` (F9):** drive `transcribe.py`'s merge with synthetic
`(start, speaker, text)` items: same-speaker within gap merges; at/over gap splits;
alternating speakers never merge; out-of-order cross-stream input sorts first.
(Rename/replace the overpromising `TestTranscribeMerge` stub-path test — keep the stub
case as its own method.)

**E3. `TestBackstopStep` (F7b):** injected sequences against the new pure function —
window flickers 1 poll → no stop; gone 2 polls → stop; audio active→silent ≥ limit →
stop; pre-join silence (never active) → never stops; not-recording resets state.

**E4. Small pure-logic tests (F14):** `_resolve_unique_path` collision suffixes (temp
dir, pre-created `stem.md`/`stem-2.md` → `stem-3.md`); `_subject_matches` (exact,
truncation-prefix, containment both directions, <3-char → False); `list_pending` on a
temp dir with mixed frontmatter (incl. trailing-space edge); minimal-metadata
`TestOutputContract` case (`source="window-title"`, None organizer/notes, empty
attendees — exercises the None branches of `_build_frontmatter`); `load_config` with a
temp YAML (relative vs absolute path resolution + mkdir).

**E5. Replace the `+ 40` tolerance (F17):** parse the written file with `yaml.safe_load`
and assert `len(fm["invite_notes"]) <= invite_notes_max_chars` directly.

**Verify (E):** full suite green; deliberately break the merge-gap comparison locally
(mental check only — no committed mutation) to confirm the new tests would catch it.

## Batch F — Shared Win32 helpers & doc drift (F6, F15)

**F1. Extract shared window helpers (per Q2).**
- `winutil.py` gains: the three WinEvent constants, `process_name_for_hwnd(hwnd, cache)`,
  `window_title(hwnd)` (the `GetWindowTextLengthW/GetWindowTextW` dance).
- `watcher.py` and `scripts/instrument_titles.py` import them (~35 duplicated lines
  removed; the diagnostic can no longer drift from the app).

**F2. PLAN.md drift (three edits from Zone 1):** line ~121 module description → pending
tracker; Phase 5 block → marked superseded by local-only posture with pointer to
SUMMARY_AGENT.md; deps line → drop `anthropic`.

**Verify (F):** unit suite; import-only smoke of `instrument_titles.py` (construct, don't
hook); grep PLAN.md for "anthropic|Claude API" returns only the Local-only-posture
section that documents the removal.

---

## Execution order & rules

A → B → C → D → E → F. Each batch: implement → run full unit suite + `record-test 3`
smoke → tick the checklist below → next batch. Any two consecutive failures in a batch →
stop, re-read, hypothesis before further edits (discipline P17).

Out of scope (explicitly deferred, per review adjudication): capture endpoint-logic
restructure (`EndpointSelector`), typed config dataclass, `summarize.py` file rename,
`ui.py` prompt-ownership move, everything in the review's Speculative list.

## Guardrails left behind (P24)

- New regression tests: merge algorithm, backstop state machine, sentinel contract,
  collision suffixes, subject matching — the five behaviors with live-bug history or
  silent-failure modes.
- Test isolation fixture prevents future transcript-dir pollution by construction.
- Shared winutil helpers prevent watcher/diagnostic drift by construction.
- This file doubles as the checklist; tick items as batches land.

## Checklist

- [x] A1 notify_stopped wired into _finish
- [x] A2 tray callbacks guarded
- [ ] A live verification: tray-stop then second call records (requires live call)
- [x] B1 PENDING_SUMMARY_TOPIC shared constant
- [x] B2 list-pending verb + deprecated alias + doc mentions updated
- [x] B3 logger naming unified; summarize logs errors
- [x] C1 Outlook pipeline deduped; com_initialized; log levels bumped
- [x] C2 watcher com_initialized; _backstop_step extracted; per-iteration guard
- [x] C3 _WavWriter.close logged
- [x] D1 five tunables in config.yaml with single readers (window_gone_poll/confirm, max_calendar_items, outlook_connect_retries, max_open_failures)
- [x] D2 named constants for borderline literals (incl. VAULT_SOURCE)
- [x] D3 CaptureResult dataclass end-to-end
- [x] E1 temp-dir test isolation
- [x] E2 TestMergeTurns
- [x] E3 TestBackstopStep
- [x] E4 pure-logic tests (unique-path, subject-match, list_pending, minimal metadata, load_config)
- [x] E5 invite_notes assertion via yaml.safe_load
- [x] F1 winutil shared helpers; script deduped
- [x] F2 PLAN.md drift corrected (summarize.py description, Phase 5 superseded, anthropic dep removed)
- [ ] Final: full suite + record-test + one live meeting/call confirms A & C behavior
