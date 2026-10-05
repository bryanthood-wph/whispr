# B. Evaluation design

> **One screen.** We measure how faithful, complete and actionable today's
> summaries are, and whether they capture **every task I own** (the
> "my-task" metrics).
> - **Method, Claude models only:**
>   - a two-family consensus reference
>   - narrow typed judge decisions
>   - judges and matcher calibrated with planted errors
>   - a design-weighted cluster bootstrap
>   - paired CIs
> - **Every run writes the same scorecard** (§B.7), including a ledger of
>   every miss with its root cause. Every later change cites a scorecard
>   diff (README §5).
> - **Budget:** up to $100, staged by phase and resumable. The harness stops
>   and asks before it would break the cap or leave the task bar unprovable.
> - **Every number here is a config key** under `eval.*` in
>   `config/defaults.yaml`.

## B.1 Questions

- **Q1.** How faithful, complete and actionable are today's summaries?
- **Q2.** Does today's pipeline capture *my* tasks to the bar (§B.6)? Where
  does each miss come from?
- **Q3.** Which change (cleaning, prompt, model, effort) improves Q1/Q2 by a
  statistically clear margin?
- **Q4.** What can be removed or made deterministic without losing quality?
  (The cleaning step and H-S2, the minimal system prompt.)
- **Q5.** Does better ASR improve summaries, at what CPU and latency cost?
  Is live chunking viable? (Appendix F)

## B.2 Pre-registration

`eval/PREREGISTRATION.md` is committed and tagged before the first scored
run. It contains:
- the hypotheses
- the metrics and the primary endpoint (**my-task recall**)
- the comparison family and decision rules (§B.6)
- the sample and its seed
- the hashes of every prompt and schema
- the graph-suite thresholds (`eval.thresholds.kg.*`)

Anything changed after results are seen is reported as exploratory.

## B.3 Sample

**Frame:** all 281 transcripts, counted 2026-10-04 from frontmatter.
- "Meeting" means Outlook-matched; "call" means window-title metadata.
- Duration comes from start/end.
- 254 already have a baseline summary (config A). The other 27 get one
  generated with the exact current prompt.

| Cell | Frame N | Core n (all metrics) | + Task-only n |
|---|---|---|---|
| Call ≤15 min | 56 | 9 | 15 |
| Call 15–30 | 38 | 7 | 10 |
| Call >30 | 23 | 4 | 6 |
| Meeting ≤15 | 22 | 4 | 6 |
| Meeting 15–30 | 64 | 11 | 17 |
| Meeting 30–60 | 51 | 9 | 13 |
| Meeting >60 | 14 | 3 | 3 |
| Stub, zero turns (negative control) | 13 | 3 | 0 |
| **Total** | **281** | **50** | **70** |

**Second stratum: output device.** The frame has 228 transcripts on laptop
speakers and 53 on a headset or other device (frontmatter
`output_device`). Within each cell, draws are balanced on device, because
echo (lesson L4) occurs only on speakers.

**Analysis flags**, recorded for each sampled transcript:
- **Echo ratio:** the share of Me lines that repeat an Others line within
  `prepare.echo_window_s` at `prepare.echo_overlap` or more. These are the
  same keys the production cleaning step uses (D.4).
- **Low mic coverage** (lesson L5): mic ≪ loopback in the recorder's
  capture log. 13 such sessions are known.
  - These transcripts can't show my words, so they are **excluded from the
    primary my-task analysis** and reported separately.
  - A sampled transcript with this flag is replaced by the next draw in the
    same cell.
- **Pilot:** the 3 pilot transcripts are excluded from the confirmatory
  draw.

**Size.** The task-only extension targets about 150 of my tasks. Tasks
cluster within transcripts, so the effective n is lower:
- The pilot estimates the within-transcript correlation.
- n is then set so that the **effective** number of my tasks is at least
  `eval.min_effective_tasks` (default 140).
- If $100 can't reach that, the harness stops and asks (§B.9).

## B.4 Reference ("truth") with no humans

- **Extraction.** Opus 5.5 and Sonnet 5.5, both at effort high, each extract
  typed items independently via `--json-schema`:
  - types: decision | task | number | risk | open_question | fact
  - fields: owner, owner_basis, due, verbatim quote, importance 1–3
- **Consensus (the JEV pattern).**
  1. A Haiku matcher pairs items across the two families ("same item?").
  2. Items both families found are accepted.
  3. An item only one family found is escalated: each family judges
     `present / absent` against the quote. The item is accepted only if
     both say `present`; otherwise it is marked contested.
- **Quote check.** A deterministic substring check rejects any item whose
  quote is not in the transcript.
- **Self-agreement check.** Every score is recomputed on the items both
  families found. A candidate whose rank changes is reported as
  reference-sensitive.
- **Stability.** The reference is re-run on `eval.stability_n` (default 10)
  core transcripts, and their Jaccard overlap is measured. If my-task
  stability is below 0.8, the plan stops and reports that the reference is
  too noisy.
- **Matcher calibration.** Planted paraphrase pairs and near-miss pairs give
  the matcher its own sensitivity and specificity (§B.5).

## B.5 Judging

| Decision | Form | Feeds |
|---|---|---|
| Reference item present in summary? | yes / partial / no | recall by type |
| Summary claim supported? | supported / unsupported / contradicted | faithfulness |
| Speech attributed to a named far-end person without proof? | yes / no | attribution violations |
| Task owner correct? Due date captured when stated? | yes / no | task correctness |
| Task actionable without reading the transcript? | yes / no | actionability |
| My Actions section present and complete? | yes / no + missing ids | structure |

- **Claims are split deterministically**, one per sentence or bullet.
- **Confidence:**
  - Each decision is sampled `eval.judge.samples` (default 3) times on
    Haiku. A unanimous answer is accepted.
  - A split answer goes to Sonnet. A Sonnet answer with stated confidence
    below `eval.judge.escalate_below` goes to Opus.
  - Escalation rate is reported per decision type.
- **Calibration without humans.** Errors are planted in code into copies of
  real summaries:
  - owner swap
  - deleted my-task
  - changed number
  - invented claim
  - `Others` statement reattributed to a named person
  - dropped due date

  Verbatim transcript claims are planted as positives, and paraphrase and
  near-miss item pairs are planted for the matcher.
  - **Sensitivity** per error type, target ≥90%. A type below target makes
    its metric "unreliable".
  - **Specificity** from the planted positives.
  - **Rogan–Gladen correction:**
    `true = (obs + spec − 1) / (sens + spec − 1)`. Raw and corrected values
    are both reported.
- **Normalization first.** Mojibake is repaired before any matching
  (lesson L9).

## B.6 Metrics, statistics, decision rules

**Metrics**
- my-task recall (primary) and precision
- all-task recall and precision
- owner and due accuracy
- actionability
- coverage by type, plus an importance-weighted version
- faithfulness: supported and contradicted shares
- attribution violations per summary
- proper-noun accuracy against Outlook spellings
- cost and latency per transcript

**Statistics**
- All CIs use a **design-weighted cluster bootstrap** that resamples
  transcripts, 10,000 replicates. That includes the task bar's lower bound.
  Wilson intervals are used only for unweighted small cells.
- **Paired comparisons** on the same transcripts and the same reference. A
  win needs the 95% CI of the difference to exclude 0.
- **Comparison family** (pre-registered), on the primary endpoint:
  - B-raw − A
  - B − B-raw
  - C − B
  - D − C
  - E − D

  Its CIs are **Holm-adjusted** (confirmed by the user on 2026-10-04). That
  is stricter than an unadjusted CI and stops a lucky comparison being
  declared a win.

**My-task bar (all four must hold)**
1. Recall ≥95%.
2. Recall 95% bootstrap lower bound ≥90%.
3. Precision ≥90%.
4. Every miss root-caused, using this tree, first match wins:

| Check | Cause |
|---|---|
| Mic coverage low for that span | **capture** |
| Task phrase garbled or missing in the transcript | **ASR** |
| Clear in the transcript, missed by the baseline prompt, caught by the same model on the new prompt | **prompt** |
| Missed on a weaker model, caught by a stronger one | **model** |
| Escalation finds the reference wrong | **reference error**: excluded from recall, counted |

Phantom my-tasks (precision errors) go through the same tree, with one extra
first check: if the supporting quote is an **echo** line, the cause is
**capture**.

## B.7 The scorecard: a standard input to every later change

Every scored run writes `results/<run-tag>/scorecard.json`, outside git,
with the **same keys every time**:
- every §B.6 metric with its CI, raw and Rogan–Gladen-corrected
- the same metrics **per stratum**: cell, device, mic-coverage flag
- the **miss and phantom ledger**: item id, root cause from the B.6 tree,
  quote. A change record cites the ledger rows it targets.
- the escalation rate per decision type
- calibration sensitivity and specificity per error type, including the
  matcher
- the simplicity scorecard numbers (README §8)
- cost, the commit SHA and tag, and the config, prompt and schema hashes

`eval/REPORT.md` and every change record (README §5) render from
scorecards.

**Regression gate.** `python -m eval regress` runs a fixed subset of
`eval.regress.n` transcripts, capped at `eval.regress.budget_usd` per run,
and compares against the last **accepted** scorecard within the
`eval.regress.tolerance.*` keys.
- It is required from the first accepted scorecard (Phase 7) onward.
- Before that, `rebuild` must be green on unit tests and the harness
  dry-run.

## B.8 Configurations compared

| Config | Summarizer | Input, prompt | Isolates |
|---|---|---|---|
| A | Haiku (existing outputs) | raw, current prompt | baseline, $0 to generate; scored on my-tasks, faithfulness and attribution |
| B-raw | Haiku | raw, revised prompt | prompt effect (B-raw − A) |
| B | Haiku | **prepared** (D.4), revised prompt | cleaning effect (B − B-raw) |
| C | Sonnet 5.5, effort high | prepared, revised | model effect (C − B) |
| D | Opus 5.5, effort medium | prepared, revised | stronger model at default effort (D − C) |
| E | Opus 5.5, effort **high**, 20 transcripts | prepared, revised | effort effect (E − D). Fable excluded (user) |

- **Configs B through E emit the full unified output in one pass**,
  validated against `config/schema/extract.json`. It contains:
  - the summary
  - `my_actions`, which is required, and "None" when empty
  - all tasks
  - entities, facts and edges in the `config/ontology.yaml` v1 shape (fixed
    in Phase 0)

  The graph phase reuses these outputs.
- **One code path.** Candidates are generated by `pipeline/prepare.py` and
  `pipeline/extract.py` through `pipeline/models.py`, so the winning config
  is the code that ships.
- **The revised prompt** is one pre-registered change set:
  - My Actions section
  - owner and basis per task, with `owner_basis: unclear` allowed
  - due date plus `due_basis`
  - JSON output, rendered to markdown in code
  - topic tags in the same call
  - owner name from config
- **The cleaning step's own checks:**
  - echo lines dropped vs kept
  - zero join links or passcodes left
  - aliases rewritten correctly
  - split meetings merged
- **H-S2** is tested in the pilot: the same prompt with a minimal
  `--system-prompt` vs the default CLI prompt.

## B.9 Budget, stage gates and run safety

Stages are keyed to the README §7 phases. Estimates use list prices (Haiku
$1/$5, Sonnet 5.5 $2/$10, Opus 5.5 $4/$20 per M tokens) and assume H-S2
removes the CLI overhead; if it doesn't, costs rise about 2–3×. The pilot
replaces every estimate.

| Phase | Stage | Estimate | Gate to the next stage |
|---|---|---|---|
| 1 | Pilot: 3 transcripts, every step, H-S2 | $3 | measured costs logged; n re-sized |
| 2 | Reference (50 core) + stability rerun (10) | $16 | stability ≥0.8 |
| 2 | Calibration: judges and matcher | $4 | sensitivity ≥90% per error type, or the metric is marked unreliable |
| 3 | Score A | $3 | — |
| 4 | Candidates B-raw–D on 50, E on 20, judged | $28 | measured cost ≤ estimate × `eval.cost_overrun_factor` (1.25), else stop and ask |
| 4 | Task-only extension (70) | $18 | effective-n target reached, else stop and ask |
| 5 | ASR downstream (10 calls × 3 models) | $5 | — |
| 6 | Graph: smoke test, comparator, C.6 suites | $12 | — |
| | Reserve | $11 | |
| | **Cap** (`eval.budget_usd`) | **$100** | |

**Run safety** (lessons L1, L3, L6, L8):
- **Launch path.** Scored runs are **one-shot scheduled tasks generated
  from `eval.schedule`**, pointed at the rebuild worktree, with the
  AC-power condition. They never start from inside a Claude session.
- **The harness refuses to start:**
  - if `CLAUDECODE` is set
  - if the auth source isn't the approved one
  - on a dirty tree or an untagged commit
- **Spend limits:**
  - every call carries `--max-budget-usd` = `eval.max_budget_per_call_usd`
  - the ledger hard-stops at `eval.budget_usd`
  - it **stops and asks** rather than shrink n
- **Caching.** Each call is cached by the sha256 of the **exact request**:
  model, effort, system prompt, schema hash, and the input text actually
  sent. Raw and prepared inputs therefore never share a cache entry, and a
  restarted run skips finished calls.
- **Sleep.** The harness holds a keep-awake. Closing the lid still sleeps
  the machine, hence the AC-power start.
- **Kill handling.** A killed run is marked **failed** in the run ledger.
  `python -m eval status` exits non-zero until the run is resolved. After
  Phase 8 it also raises an alert on the alert surface (D.6).

## B.10 Model roster for the eval (least privilege, README §4)

| Role | Model / effort | Tools | Why this model |
|---|---|---|---|
| Reference extractor | Opus 5.5 high + Sonnet 5.5 high | none (`--json-schema` only) | two families, so the reference isn't biased toward one candidate family |
| Item matcher | Haiku (calibrated, §B.5) | none | a narrow yes/no at volume; calibration shows whether it's accurate enough |
| Judge (first pass) | Haiku × `eval.judge.samples` | none | agreement gives the confidence |
| Judge escalation | Sonnet, then Opus | none | only split or low-confidence items |
| Candidates | as in §B.8 | none | the configs under test |

No eval role reads files, writes files or runs commands. The harness passes
the text in and validates the JSON that comes out.
