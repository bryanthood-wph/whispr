# whispr rebuild plan

**Status:** PLAN. Not approved and nothing executed (2026-10-04).

**How to read this:**
- This page is the whole plan in one sitting.
- Each appendix opens with a one-screen summary, and the detail is below it.
- Read an appendix only when you need to act on it.

| Appendix | Read it when |
|---|---|
| [A. Inventory](A-inventory.md) | you need today's facts: components, models, effort, configs, recordings, handoffs, costs |
| [B. Evaluation](B-eval.md) | you're running or judging the eval |
| [C. Knowledge graph](C-knowledge-graph.md) | you're surveying, building or evaluating the graph |
| [D. Architecture & ops](D-architecture-and-ops.md) | you're building the pipeline, config, task flow, alerts or installer |
| [E. Lessons learned](E-lessons.md) | you're about to build anything (26 mistakes not to repeat) |
| [F. ASR & live assist](F-asr-live.md) | you're working on the audio or live tracks |
| [G. Git & tracking](G-git-and-tracking.md) | you're opening a branch, issue or PR |

---

## 1. North star

> **Every Teams call I'm on becomes, by the next morning, an accurate summary
> that explicitly lists every task I own.** It is written into a knowledge
> graph I can query and see through Claude. It runs unattended, fails loudly,
> costs little, and installs on a Deloitte colleague's machine in a few
> steps. And the captured tasks are the starting point for getting the work
> done.

**How we know we're there.** These are measured by the eval and `doctor`,
not asserted:

| Measure | Target |
|---|---|
| My-task capture | recall ≥95% with bootstrap lower bound ≥90%, precision ≥90%, every miss root-caused (B.6) |
| My Actions in every transcript | `doctor` count of transcripts with no My Actions section = 0 (D.1) |
| Accuracy | faithfulness and attribution beaten with paired CIs (B.6) |
| Unattended | zero silent failures: every failure shows in the alert surface within one cycle (D.6) |
| Simple | the simplicity scorecard (§8) improves on every row we choose to move |
| Hand-off | the setup acceptance test passes on a clean profile (D.7) |
| Work-ready | the task funnel captured → confirmed → ready is live (D.5) |

## 2. Decisions this plan is built on

All of these come from you on 2026-10-04 (seven `/clarify` rounds, a
premortem round, and later messages). Changing one changes the plan.

| Decision | Value |
|---|---|
| Judging | JEV-style, using Claude only: typed decisions, confidence from agreement, escalation Haiku → Sonnet → Opus |
| Reference | two-family consensus (Opus + Sonnet), no human labelling |
| Validation | error injection + planted positives; coverage of actionable info; task capture |
| Sample | stratified across all 281 transcripts |
| Models tested | up to **Opus 5.5 at effort high**; Fable excluded |
| "Work I need to do" | assigned to me **or** volunteered by me, in a dedicated section of **every** summary, "None" when empty |
| Task bar | as in §1 |
| Eval budget | **up to $100, staged**; production cost: accuracy first |
| Win rule | paired comparison; the 95% CI of the difference excludes 0, **Holm-adjusted** across the 5 pre-registered comparisons (B.6; confirmed 2026-10-04) |
| Vault | **no Obsidian**; a knowledge graph with a visual view, read through Claude, self-healing, with its own evals; **SQLite now, Postgres-ready**; **rebuilt from transcripts** |
| Graph build | **adopt a maintained tool if it passes the must-haves**; otherwise a minimal SQLite core |
| Distribution | Deloitte colleagues with Claude, each with their own graph; **Claude Code plugin with mods** is the lead option |
| Repo | all pipeline code in `whispr`; data outside git |
| ASR | in scope; keep audio 2 weeks, then delete |
| Live assist | feasibility only |
| Current nightly | **keeps running** until its replacement passes |
| Removals | anything the data justifies, each one approved by you |
| Agents | **encouraged, least privilege** (§4), a standard way of working |
| Git | integration branch + feature branches + issue tracking (G) |

## 3. Principles: how every part of this gets built

1. **Simplest thing that meets the north star.** Before anything is added,
   ask whether it is needed to hit a §1 target. Anything not yet proven goes
   in the **deferred table** below, not in the build.
2. **Evidence before build.** Eval results are the standard input to every
   change (§5).
3. **Config is the only home for tunables.** Models, effort, budgets,
   thresholds, schedules, retention, names and paths all live in
   `config/defaults.yaml` plus a per-user overlay. None of them appear in
   code or prompts (D.2).
4. **Progressive disclosure, everywhere.**
   - **Docs:** each folder README opens with a one-screen summary.
   - **Summaries:** My Actions first, then decisions, then detail, then the
     source link.
   - **Retrieval:** MCP returns short cards, then facts with quotes, then
     the transcript span (C.4).
   - **Skills:** a short SKILL.md with references behind it.
5. **Fail loud, never silent.**
   - Success means progress.
   - Every failure reaches the one alert surface.
   - Every check exits non-zero when it fails (D.1, D.6).
6. **Deterministic where possible.** The model fills one schema. Code does
   the cleaning, the writing, the rendering and the integrity checks.
7. **One code path.** The eval runs the production code, prepare and
   extract, so the winner is what ships (B.8).
8. **Lessons are built in, not learned again.** Every build issue checks
   Appendix E.
9. **The bar for findings.** A finding gets a fix if it silently stops the
   pipeline, loses my tasks, wastes money, or leaks data. Otherwise it is
   noted and declined with a reason (E.2).

**Deferred until evidence.** This is the single list. Nothing here is built
until its trigger fires.

| Deferred | Add it when |
|---|---|
| Ownership re-sampling (k samples on borderline tasks) | the eval winner alone misses 90% my-task precision |
| API path in `pipeline/models.py` (Options 2/3) | the plugin/CLI path fails vetting (D.7) |
| Tray alert badge | an alert sits unseen for more than a day |
| Local embeddings (sqlite-vec + model download) | retrieval QA misses its threshold with full-text search + traversal |
| Community detection | the graph view is unreadable without clustering |
| Rendered entity pages | MCP answers prove too slow or too costly without them |
| Model-proposed ontology types | the extraction suite shows the fixed ontology is limiting |
| Separate system-time columns | an "as recorded on" query is ever needed |
| Audio replay harness for recorder tests | the live-call checklist misses a regression |
| Effort sweeps beyond D/E, more prompt variants | a later change record needs them |
| **Future state (your note, 2026-10-06): talk tasks through with Claude interactively, and Claude pulls the key information it needs to execute them** (graph, transcript spans, files, people). It builds on `/create-tasks` and `/execute-tasks` (D.5) | the pipeline is built, cut over and running; then **discuss first**, no build before that |

## 4. Agents: encouraged, least privilege

Subagents are encouraged, at build time and in the product. Each one gets
the model and effort its job needs to be accurate, and **only the tools that
job needs**.
- **The default is no tools, or read-only.** Every extra grant has a stated
  reason.
- **Rosters:**
  - eval roles in B.10
  - production roles in D.3
  - task workers carry their own `tools_allowed` (D.5)
- **Build-time research and review agents are read-only,** with explicit
  instructions: no edits, moves or deletes, no nested `claude`, no
  scheduled-task changes.

This is codified for all projects in your global CLAUDE.md.

## 5. Change records: eval findings drive every change

From the pilot onward, **no behavior change merges without a change
record**. The record is a GitHub issue template (G.4) with these fields:

| Field | Content |
|---|---|
| Why | the hypothesis, the miss/phantom ledger rows it targets (B.7), or the lesson ID (`Lnn`) |
| What | the config diff and/or code diff |
| Evidence | scorecard diff: run tag before → after, the metric(s), the paired CI |
| Decision rule | the pre-registered rule it meets, or "guardrail" (a lesson fix proven by a test, not by the eval) |
| Simplicity | the §8 scorecard rows it moves |
| Regression | `python -m eval regress` passes, once an accepted scorecard exists (B.7) |

Every scorecard has the same keys, so changes are compared the same way
every time. The report and every change cite scorecards, never memory.

## 6. Target folder structure

```
whispr/            the recorder. Local-only, enforced. The capture.py logic stays as is
pipeline/          prepare → extract → write → maintain; models.py (the only model-call path); doctor
                   (absorbs `whispr doctor`); schedule (tasks generated from config)
kg/                SQLite schema + migrations, data-access module, maintenance, retrieval, MCP server (data only)
eval/              harness, PREREGISTRATION.md, suites, REPORT.md (results live outside git)
plugin/            Claude Code plugin: skills (create-tasks, execute-tasks, ask), mods (graph view, alerts),
                   session-start alert hook, /whispr-setup
config/            defaults.yaml, config.schema.json, prompts/, schema/, ontology.yaml   ← every tunable
installer/         recorder installer + build-dist (publishes plugin + installer to a separate dist repo)
ops/               author-only jobs; never shipped; extracted before the repo is ever made public
tests/             unit tests + prepare/redaction/encoding regression tests
docs/              this plan, per-area READMEs, history/ (archived docs)
```

- **Every top-level folder has a README:** what it does in five lines, how
  to run it, which config keys it reads, then the detail.
- **Per-user data lives outside git,** in the data folder named in the
  overlay: transcripts, audio, the database, results, backups.
- **Today's `scripts/`** splits into `ops/` (the watchdog and author jobs)
  and `installer/`.
- **At cutover, CLAUDE.md's "extract `scripts/` first" guardrail** is
  re-pointed at `ops/` (Phase 8).

## 7. The march

> **Reordered 2026-10-05 (your decision).** Phase 8's build starts now, on what is
> already known (lessons, prepare, the task contract, config, distribution), with the
> knowledge graph as the minimal SQLite core (C.3). The extract prompt is tuned in a
> capped loop on a 25-transcript tuning set (`python -m eval run --stage dev`, $65 cap).
> Phases 2–4 shrink to a **task-first** comparison (my-task measures; faithfulness on a
> claim sample) that runs last, as the acceptance test of the built pipeline, followed by
> your Phase 7 approvals and the Phase 8 cutover gates. The table below is the original
> order. See `eval/PREREGISTRATION.md` (2026-10-05 reorder row).

**Legend**
- **Configs:**
  - A: today's Haiku summaries
  - B-raw / B: the new prompt on raw / prepared transcripts (Haiku)
  - C: Sonnet 5.5
  - D / E: Opus 5.5 at medium / high effort (E on 20 transcripts)
- **Other terms:**
  - H-S2: a minimal system prompt vs the CLI default
  - Q1–Q5: the eval questions (B.1)

| Phase | What | Exit criterion | Spend |
|---|---|---|---|
| **0. Prep** | branch, worktree and issues (G); `config/` skeleton incl. `ontology.yaml` v1 and `schema/extract.json`; `pipeline/models.py` with the auth and `CLAUDECODE` guards; `prepare.py` + tests; `extract.py`; harness + `eval status`; preregistration skeleton; desk survey of graph tools (C.2); gate 0 | harness dry-run passes with no model calls; prepare tests green; gate 0 closed | $0 |
| **1. Pilot** | 3 transcripts through every step; measure costs, H-S2, schemas and within-transcript correlation; re-size n | measured costs replace the estimates | ≤$3 |
| **2–4. Scored run** (one approval) | 2: reference, stability rerun, calibration. 3: score A. 4: B-raw, B, C, D, E; task-only extension | B.9 stage gates; paired CIs; my-task bar verdict | ~$20 + $3 + $46 |
| **5. ASR + live** (when the 14-day collection ends; runs alongside 6) | ASR models + hotwords, chunking, CPU, downstream effect; then delete the audio (F.1) | Q5 answered; audio deleted | ~$5 |
| **6. Graph** | survey smoke test → adopt or minimal core, on Phase 4 outputs; Claude Code view; C.6 suites; Graphify comparator | gate 3 closed; suites pass `eval.thresholds.kg.*` | ~$12 |
| **7. Report** | `eval/REPORT.md` from scorecards: Q1–Q5, the miss ledger, production config, graph results, removals, distribution, rebuild cost, §8 scorecard; accept the scorecard → the regression gate goes live | **you approve** what ships | $0 |
| **8. Implement** | consolidate; plugin + installer + dist repo; alerts; `doctor`; schedule-from-config; rebuild the graph from transcripts; retire cohoodOBS machinery; re-point the CLAUDE.md guardrail; cut over (G.3) | setup acceptance test + live-call checklist pass (D.7) | production, approved separately (rebuild ~$30–110) |

The eval total is ≈$89, plus an $11 reserve, for a cap of $100 (B.9).

## 8. Simplicity scorecard (today → proposed, in the report)

| Row | Today (measured, A) |
|---|---|
| Model calls per transcript | 2 nightly + a weekly share |
| Scheduled tasks | 4 |
| Places holding pipeline code | 3 (this repo, cohoodOBS, `~/.claude`) |
| Agents / skills / hooks | 5 / 4 / 3 |
| Config files | 11 in 3 places (A.6) |
| Hardcoded machine/person values | 7 kinds in about 15 places (A.5) |
| Setup steps for a new user | measured at Phase 8 |
| Cost per transcript | ≈$0.49 + a weekly share |
| Manual interventions per month | from `incidents.jsonl` + logs |

## 9. Your gates (each one is a `gate` issue)

0. **Now** (E.3):
   - which auth source the jobs may use
   - whether the current nightly strips join links and passcodes before
     cutover
   - a `hotfix/keep-audio` on `master`: the config flip + the deletion task
     (F.1)
   - deleting `install_task.ps1` as a hotfix
   - moving or deleting the parked `2026-07-16-0959-quick-chat.md`
   - the branch and worktree setup, and deleting the merged fix branch
   - the graph-tool must-haves table (C.2)
1. **After the pilot:** approve phases 2–4 as one scored run inside $100.
2. **Before Phase 5:** the one-time ASR model downloads.
3. **During Phase 6, after the smoke test:** the graph tool verdict.
4. **Phase 7:**
   - production config
   - each removal
   - distribution option
   - rebuild spend
5. **Phase 8 cutover:** the setup acceptance test and live-call checklist
   pass.
6. **Any later exception:** showing the graph as an artifact or a browser
   `graph.html` (C.4).
7. **Before any live-assist build:** Deloitte policy.

## 10. Out of scope

- Code before approval.
- The JEV service itself.
- Package upgrades during the baseline.
- Adopting a graph tool that fails the must-haves.
- Building live assist.
- Everything in the deferred table (§3).
