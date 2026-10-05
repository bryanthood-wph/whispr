# E. Lessons learned: what the rebuild must never repeat

> **One screen.** Four read-only agents (2026-10-04) mined the logs, git
> history, ops scripts and the transcript and vault data. Their findings were
> de-duplicated into **26 lessons**. Each one names its guardrail and where
> the plan enforces it.
>
> **The bar for a guardrail:** does the failure silently stop the pipeline,
> lose my tasks, waste money, or leak data? If so, a guardrail goes in. If
> not, the lesson is noted only. The recurring pattern is that every past
> fix arrived after an incident; these guardrails are built in before one.
>
> Labels: **[C]** confirmed from data, logs or commits; **[I]** inferred.

## E.1 Lessons that change the plan (ranked by impact)

| ID | Lesson | Evidence (short) | Guardrail | Enforced in |
|---|---|---|---|---|
| L1 | **"SUCCESS" while making no progress** [C] | 8 nights of SUCCESS with a frozen watermark (09-09..16), 159 "advance suppressed" lines; 4 SUCCESS runs with 0 files while the recorder was dead (08-24..27); freshness check accepted "no log yet" | success = progress or proven-empty; backlog logged every run; repeat-item alert | D.1, D.6 |
| L2 | **No alert ever reached a human** [C] | 51 of 51 toasts failed; 40 unread FAILED files; one parked file alerted 21× and is still unresolved | one alert surface (Claude Code session start + `doctor`), dedupe/ack, digest after N days, test alert at setup | D.6 |
| L3 | **Spend without idempotency or caps** [C] | ~$75 re-ingest and $69 frozen-watermark loop; $16.30 `/lint` with 0 output; no production `--max-budget-usd`; re-derivation would cost $30–110 each time | cache keys; per-call and nightly caps; alert on spend with zero output; re-derive approval above a config threshold | B.9, C.5, D.3, D.6 |
| L4 | **Echo labels others' speech "Me"** [C] | 82 transcripts with >20% of Me lines echoed (13 with >50%), **all on laptop speakers**, 0 of 43 on headsets | echo removal in prepare; device stratum + echo ratio in the eval; echo-quoted tasks → "confirm?" | B.3, B.6, D.4 |
| L5 | **Mic dropouts hide my own words** [C] | 18 capture failures; 13 sessions with mic ≪ loopback (5.9 h missing); 10 Others-only files; fixed after 55 days (8e2c97b) | `mic_coverage` per episode; low-coverage cell excluded from primary recall; "capture" root cause | B.3, B.6, C.4 |
| L6 | **Time limits and sleep kill runs** [C] | 1 h limit killed every run 09-17..22 ($54 billed); `/lint` frozen 19 h; 10-02 killed 12 s after wake | time budget → batch cap, exit PARTIAL; AC-power condition; resumable runs | B.9, D.6 |
| L7 | **One bad file blocked everything** [C] | 5 whole-night aborts; one stray file froze ingest 14 days | per-item isolation, retry with backoff, quarantine with one alert + expiry | D.1 |
| L8 | **Auth source never asserted; nested-session trap** [C/I] | user-scope `ANTHROPIC_API_KEY` "takes precedence over your claude.ai login" (stderr, 08-03 and 08-16); scripts never clear it; nested `claude` dies with 0 tokens | the model-call interface refuses `CLAUDECODE` and asserts the approved auth source; fails closed | B.9, D.3, **gate 0** |
| L9 | **Encoding corruption** [C] | mojibake in 230 of 258 vault sources (`ΓÇö` ×1,635); em-dashes broke a `.ps1` under PowerShell 5.1 | UTF-8 end to end; reject writes containing `Γ`/U+FFFD; pipeline moves to Python | B.5, D.4 |
| L10 | **Join links and passcodes left the machine** [C] | `invite_notes` in 155 transcripts (64 with passcodes); **134 vault sources** contain a passcode or join link | redaction in prepare before any model call + regression test | D.4, **gate 0** |
| L11 | **ASR misspells proper nouns, and the errors spread** [C] | "Teradyne" 0× vs 117 variants; "data bricks" 121×; one surname spelled 5 ways | Outlook-seeded hotwords (new calls); alias rewrite in prepare (old calls) | F.2, D.4 |
| L12 | **Duplicate entities** [C] | 21 first-name pages shadow full-name pages; one person has 4 pages | email-keyed people; first-name-only mentions stay unresolved aliases; real cases in the entity-resolution eval | C.4, C.6 |
| L13 | **Meetings split across files** [C] | 13 same-title pairs <10 min apart; lobby → matched pairs <1 min apart; manual merges | episode merge rule (gap from config) | D.4 |
| L14 | **Recorder dies silently; queued calls lost** [C] | 80.8 h gap (08-23..26); 14 watchdog relaunches; 0 crash entries; 308 queued vs 304 written; 4 orphan WAV pairs | heartbeat + exit reason; queue on disk; orphan sweep; deletion skips WAVs with no transcript; daily Outlook reconciliation | D.6, F.1 |
| L15 | **Metadata junk and wrong calendar matches** [C] | 123 window-title fallbacks with title text in `attendees`; reminder items matched; email in 34 filenames | attendee validation; skip reminder, all-day and single-attendee items; store the match score; re-match in the rebuild | C.8, D.4 |
| L16 | **Task Scheduler traps and drift** [C] | RestartCount never fires; a repetition grafted onto AtLogOn never arms; versioned MSIX path; all 3 schedules differ from `register-task.ps1` | schedules generated from config; `schedule --check` in `doctor`; trap list in the setup test | D.2, D.7 |
| L17 | **Install bugs only show up live; private-repo access** [C/I] | `._pth` and user-site leaks; launch from the wrong working directory exits 0; unauthenticated download from a now-private repo; recipients get no watchdog | setup acceptance matrix; separate distribution repo; recipients get liveness too | D.7 |
| L18 | **Headless prompts that need a second turn, or a model doing side effects** [C] | 4 weeks of lint records lost (`-p` can't answer its own question) | model output only as schema-validated JSON; code writes | D.1 |
| L19 | **State duplicated in files drifts** [C] | `status: raw` on 282 of 282 transcripts; 60 vault notes stuck at `raw`; 29 wiki pages with bad or no YAML; 3 vault notes whose transcript is gone | lifecycle state only in the database; schema check at ingest; episode = sha256 with a tombstone on delete | C.4, D.5 |
| L20 | **Tasks never became work** [C] | 0 of 1,262 vault items reached `tasks/`; only 23% have a due cue; owner parsing broken by mojibake | structured task contract, `due_basis` required, weekly funnel metric | D.5 |
| L21 | **Docs disagree with the machine** [C] | CLAUDE.md described a shortcut that didn't exist; the at-logon claim was retracted; 6 stale docs | `doctor` is the source of live status; stale docs archived | D.6, D.8 |
| L22 | **Data with no history or tested backup** [C] | cohoodOBS content untracked; ops scripts once lived on one disk | nightly backup + restore drill | D.6, D.7 |
| L23 | **Recorder bugs only appear in real calls** [C] | busy-spin deadlock, clobbered mutex, endpoint thrash, silent WinEvent hook (69% of starts come from the poll) | live-call checklist before cutover; poll documented as the primary trigger; the `capture.py` constraint stays | D.6, D.7 |
| L24 | **Plans absorb every finding** [I] | the plan grew to ~1,100 lines; the triage rule says "imperfections are okay if the pipeline works" | this bar; §E.2 declined list; the "deferred until evidence" table | C.7, README §3 |
| L25 | **Empty calls still summarized** [C] | 13 stubs → 10 vault notes, 3 of them "integrated" into the wiki | stub gate in prepare | D.4 |
| L26 | **Duplicate scripts bring back failed designs** [C] | `install_task.ps1` re-creates the 08-23 outage design; drifted copies of the dispatch code | one code path; delete duplicates | B.8, D.8 |

## E.2 Noted, no guardrail (below the bar)

| Finding | Why not |
|---|---|
| Short-session discards are rising (24, all ≤11.7 s) | harmless pre-join previews; `doctor` trends them |
| `lint-report-latest.txt` is stale | retired with `/lint` |
| Toast fails under pwsh 7 | the toast is removed (L2) rather than fixed |
| Audio replay harness for recorder tests | over-engineering for now; the live-call checklist covers it (L23) |

## E.3 Live issues found today (not plan items, so decided now)

1. **Auth source (L8).** The scheduled jobs probably bill the user-scope
   API key, not the enterprise login the egress exception relies on.
2. **Passcodes off-machine (L10).** 134 vault notes already hold join links
   or passcodes, and every nightly run sends more.
3. **Pipeline stalled.** The 10-02 nightly was killed with no log (resumes
   Monday 01:00). The freshness check keeps failing on the parked
   `2026-07-16-0959-quick-chat.md`, which needs you to move or delete it.
