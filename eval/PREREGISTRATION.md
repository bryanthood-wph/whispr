# Preregistration: whispr summary evaluation (B.2)

**Status: DRAFT.** To be committed and tagged `eval-prereg` before the first
scored run. After the tag, any change is an amendment (see Deviations).

Source of design: `docs/plan/B-eval.md`. Every number below is a config key
under `eval.*` in `config/defaults.yaml`; the key name is cited so the
config stays the single source of truth. If this document and the config
disagree, the tagged config wins and the disagreement is a deviation.

"The recording owner" is the person whose microphone is the `Me` channel.
No transcript content, person names, email addresses or client names appear
in this document.

## Hypotheses

One hypothesis per comparison in the family, all on the primary endpoint
(my-task recall). Each is two-sided in testing; the expected direction is
stated so a result against it is reported as such. Configs A, B-raw, B, C, D, E
are defined in B.8.

| ID | Comparison | Isolates | Expected direction |
|---|---|---|---|
| H-P | B-raw − A | revised prompt effect (Haiku, raw input) | positive: the revised prompt raises my-task recall |
| H-C | B − B-raw | cleaning (`pipeline/prepare.py`) effect | positive, or no difference. A zero effect supports removing the cleaning step (Q4) |
| H-M | C − B | model effect (Sonnet 5.5 high vs Haiku) | positive |
| H-D | D − C | stronger model (Opus 5.5 medium vs Sonnet 5.5 high) | positive, or no difference |
| H-E | E − D | effort effect (Opus 5.5 high vs medium, 20 transcripts) | positive, or no difference |
| H-S2 | minimal `--system-prompt` (`prompts.system_minimal`) vs the CLI default system prompt, same prompt otherwise | CLI overhead | no loss in quality at lower cost. **Exploratory**: measured in the pilot only, outside the confirmatory family and not Holm-adjusted |

Null for each confirmatory hypothesis: the paired difference in my-task
recall is 0.

## Primary endpoint

**My-task recall**: the share of the reference's tasks owned by the recording
owner that the candidate summary captures (judge decision "reference item
present in summary"). The primary computation counts yes as 1 and partial
as 0.5, consistent with `Outcome.value` in `eval/records.py` (1 hit, 0 miss,
0.5 partial) (added at registration; not in B-eval.md). Recall with partial
counted as 0 is reported as a sensitivity analysis.
It is computed on the confirmatory sample after the low-mic exclusion (see
Sample), against the two-family consensus reference (B.4), with reference
errors excluded from the denominator and counted.

Secondary metrics are listed under Metrics.

## Metrics

Primary: my-task recall (above). Secondary, from B.6:

- my-task precision
- all-task recall and precision
- owner accuracy and due-date accuracy
- actionability
- coverage by type (decision, task, number, risk, open_question, fact), and
  an importance-weighted version
- faithfulness: supported share and contradicted share of claims
- attribution violations per summary
- proper-noun accuracy against Outlook spellings
- cost and latency per transcript

Every metric is reported raw and Rogan-Gladen corrected
(`true = (obs + spec - 1) / (sens + spec - 1)`), per stratum (cell, device,
mic-coverage flag), with its CI. A judge decision type whose calibration
sensitivity is below `eval.calibration_min_sensitivity` (0.90) marks the
affected metric "unreliable". Reference self-agreement (scores on items both
families found) and reference stability (`eval.stability_n` = 10 transcripts,
Jaccard at least `eval.stability_min_jaccard` = 0.8) are reported alongside.

Judging: each decision is sampled `eval.judge.samples` (3) times on Haiku;
unanimous is accepted; a split goes to Sonnet; Sonnet confidence below
`eval.judge.escalate_below` (0.7) goes to Opus.

## Comparison family

The pre-registered family, on the primary endpoint only, is exactly five
paired comparisons:

1. B-raw − A
2. B − B-raw
3. C − B
4. D − C
5. E − D

**Holm adjustment** across the five (confirmed by the user on 2026-10-04):
order the five p-values ascending and compare the k-th smallest to
`alpha / (5 - k + 1)`, stopping at the first non-rejection. Confidence
intervals are widened to the corresponding Holm-adjusted level. This is
stricter than unadjusted CIs and stops a lucky comparison being declared a
win.

**Interval method.** Paired cluster bootstrap that resamples transcripts (the
cluster), design-weighted by cell, with `eval.bootstrap_replicates` (10000)
replicates and `eval.alpha` (0.05) as the family-wise level. The same
transcripts and the same reference are used for both sides of each pair.
Wilson intervals are used only for unweighted small cells.

## Decision rules

- **Win:** a comparison is a win only if p_holm < `eval.alpha` AND the
  Holm-adjusted CI of the difference (newer config minus older config, in
  the order written in the comparison family) lies entirely above 0. A CI
  entirely below 0 is reported as a significant loss, never a win.
- If a comparison is not a win, the simpler or cheaper configuration is
  preferred (added at registration; not in B-eval.md).
- Secondary metrics never create a win. They are reported with unadjusted CIs
  labelled secondary and are used only to flag regressions (for example a
  higher faithfulness-contradicted share) that count against adopting a
  config (added at registration; not in B-eval.md).
- **Stops:** reference stability below `eval.stability_min_jaccard` stops the
  plan and reports the reference as too noisy. Measured cost above the stage
  estimate times `eval.cost_overrun_factor` (1.25) stops and asks. The ledger
  hard-stops at `eval.budget_usd` (100) and stops and asks rather than shrink
  n (`eval.stop_margin_usd` = 5 triggers the resume note). Effective my-task
  count below `eval.min_effective_tasks` stops and asks.
- **Reference sensitivity:** a candidate whose rank changes when scores are
  recomputed on items both families found is reported as reference-sensitive.
- **Regression gate** (after the first accepted scorecard): `eval.regress.n`
  (10) transcripts, at most `eval.regress.budget_usd` (3) per run, tolerance
  `eval.regress.tolerance.my_task_recall` (0.02) and
  `eval.regress.tolerance.my_task_precision` (0.02) against the last accepted
  scorecard.

## My-task bar

All four must hold (values from `eval.bar`):

1. My-task recall at least `eval.bar.my_task_recall` = 0.95.
2. The 95% bootstrap lower bound of my-task recall at least
   `eval.bar.my_task_recall_lower_bound` = 0.90. The lower bound uses the
   same design-weighted cluster bootstrap (`eval.bootstrap_replicates`).
3. My-task precision at least `eval.bar.my_task_precision` = 0.90.
4. Every miss is root-caused with the tree below, first match wins:

| Check | Cause |
|---|---|
| Mic coverage low for that span | capture |
| Task phrase garbled or missing in the transcript | ASR |
| Clear in the transcript, missed by the baseline prompt, caught by the same model on the new prompt | prompt |
| Missed on a weaker model, caught by a stronger one | model |
| Escalation finds the reference wrong | reference error: excluded from recall, counted |

Phantom my-tasks (precision errors) go through the same tree with one extra
first check: if the supporting quote is an echo line, the cause is capture.

The ledger of every miss and phantom (item id, root cause, quote) is written
into every scorecard (B.7).

## Sample

- **Frame:** all transcripts dated on or before `eval.sample.frame_cutoff`,
  which is **2026-10-04** (281 transcripts counted on that date from
  frontmatter). "Meeting" means Outlook-matched; "call" means window-title
  metadata. Duration comes from start and end. Configuration A reuses the
  existing baseline summary where one exists; the rest get one generated with
  the exact baseline prompt (see Hashes).
- **Seed:** `eval.seed` = **20261004**. Draws are deterministic given the
  seed, the frame and the cell definitions.
- **Cells** (`eval.sample.cells`; duration bounds in minutes,
  min exclusive, max inclusive):

| Cell (config name) | Definition | Core n (all metrics) | Task-only n |
|---|---|---|---|
| Call <=15 min (`call_le15`) | call, duration <= 15 | 9 | 15 |
| Call 15-30 (`call_15_30`) | call, 15 < duration <= 30 | 7 | 10 |
| Call >30 (`call_gt30`) | call, duration > 30 | 4 | 6 |
| Meeting <=15 (`meeting_le15`) | meeting, duration <= 15 | 4 | 6 |
| Meeting 15-30 (`meeting_15_30`) | meeting, 15 < duration <= 30 | 11 | 17 |
| Meeting 30-60 (`meeting_30_60`) | meeting, 30 < duration <= 60 | 9 | 13 |
| Meeting >60 (`meeting_gt60`) | meeting, duration > 60 | 3 | 3 |
| Stub, zero turns (`stub`) | negative control | 3 | 0 |
| **Total** | | **50** | **70** |

- **Device balancing:** the second stratum is output device. Within each cell,
  draws are balanced on device, where "speakers" are output devices matching
  `eval.sample.speaker_device_pattern` (`(?i)speaker`), because echo occurs
  only on speakers.
- **Analysis flags** recorded per sampled transcript: echo ratio (using
  `prepare.echo_window_s` and `prepare.echo_overlap`, the same keys as
  production cleaning) and low mic coverage.
- **Low-mic exclusion:** a transcript flagged low mic coverage (mic much
  lower than loopback in the capture log) cannot show the owner's words. It
  is excluded from the primary my-task analysis and reported separately. A
  sampled transcript with this flag is replaced by the next draw in the same
  cell.
- **Pilot exclusion:** `eval.sample.pilot_n` = 3 pilot transcripts are
  excluded from the confirmatory draw.
- **Effective-n rule:** tasks cluster within transcripts. The pilot estimates
  the within-transcript correlation, and n is then set so that the effective
  number of the recording owner's tasks is at least
  `eval.min_effective_tasks` = 140. If the budget cannot reach that, the
  harness stops and asks; it does not silently shrink n.
- **Drawn unit list:** produced by the harness (`python -m eval`, issue #11)
  from the frame, the seed and the cells above, and **appended to this
  document by amendment before the `eval-prereg` tag**. This draft does not
  contain it.

## Hashes

Prompts (`prompts.*`), schemas (`schemas.*`) and the ontology as configured
in `config/defaults.yaml` at registration, plus the judge-calibration planting
seed data `config/planting.yaml` (invented-claim templates and the
positive-claim and reattribution filters, B.5). That file is not a config key:
it is part of the registered design, and no user overlay can change it. JSON
files are hashed in canonical form (`json.dumps(obj, sort_keys=True, separators=(",", ":"),
ensure_ascii=False)`, via `pipeline.calls.canonical`); text files are
hashed as UTF-8 with CRLF converted to LF. `tests/test_eval_prereg_contract.py`
re-checks every row against the file on disk, so an edit after registration
fails until it is re-registered or reported as exploratory.

| File | sha256 |
|---|---|
| `config/prompts/extract.md` | `bcee9d37e2ed3224f28a39eef0a4a5be04d4c2e885d18cfb91e9a2ed81d906f9` |
| `config/prompts/system-minimal.md` | `4633e13eded939eb8af93c2154a6adcbd899f9c95c08cc3e30e2243023cfea1f` |
| `config/schema/extract.json` | `b196eff61ccf9a701e563a8759c8457ad2feb5e349e7ef92ba8ae5c620d68ad8` |
| `config/schema/task.json` | `67584bfc005327eb68052234065553289cfaba5415a1928d5e200a602f67e635` |
| `config/schema/scorecard.json` | `2708d61e1dbd3655023d2d1be75b520bbcd85039bf5df25a3e46a57e159757a0` |
| `config/prompts/reference.md` | `e3ba143f462c75c582c14c8864052e27c42436ec84e27edda5b072aff24b1f14` |
| `config/prompts/matcher.md` | `b2b49cbebc20629710a363a84f9d29a0fed77b3fe640524dec6cdd24726994a1` |
| `config/prompts/presence.md` | `eca8a57b8c003ac0c5b741b4f5ae515c7fc79b0882b3160ac8a1ecbd2638f874` |
| `config/schema/reference.json` | `6e1da40e93483c69a21c54b425ed232306dd83c14c885117b12cdefa35f109e3` |
| `config/schema/matcher.json` | `019785f404444098900ad6f2d7b9bdce6b83a959872874d86ecbd1734fc4672a` |
| `config/schema/presence.json` | `3454e2eb5981a112d1558dafb757368305ff5c2f7fc0fba0911a0d0c7b71e69f` |
| `config/prompts/judge.md` | `e146a9d0332dca83916a57db42c931c61b1cec540107160a8a2c81c6e0b9fcc9` |
| `config/schema/judge.json` | `20307df8b5a0f2fc0a55bf4485de7eb889b93cc1be1063b0b0ad20484ac2a3a7` |
| `config/planting.yaml` | `27621149cc631812fe0fe7c434185afdc160f841b0a86ef5ee43b98ae8c251e8` |
| `config/ontology.yaml` | `d76e3c0161ca49466bf5c167043e3062bfa11b41ccd4dad456897d9e36efa748` |

Baseline (configuration A) summarizer prompt, recorded outside the table
above because it lives outside `config/`:

- Path: `scripts/summarize-prompt.txt` in the whispr repository (the shared
  template loaded by `scripts/summarize-transcript.ps1` and
  `scripts/nightly-ingest.ps1`; the nightly job runs it on Haiku).
- sha256: `baf655c26ac33da2eb161c6887a0fa9b8a978b7d1ca2b8411b5ca446294dd0c4`
- How hashed: sha256 of the file's UTF-8 text with CRLF converted to LF (the
  file has no CRLF, so this equals the raw-bytes hash), computed by a short
  Python one-liner. The hash covers the template with its `{{...}}`
  placeholders unsubstituted.
- Caveat: the file's last change in git history is 2026-08-27 (the commit
  that brought the scripts into the repository). Baseline summaries
  generated before the file's current state may have used an earlier
  template; this is reported as a limitation of configuration A, not as a
  deviation.

## Graph-suite thresholds

The graph suites (extraction, entity resolution, temporal, retrieval QA,
self-healing; `docs/plan/C-knowledge-graph.md` C.6) pass or fail on
`eval.thresholds.kg.*` keys. These keys do not exist in config yet.

**TBD before the graph phase, registered by amendment.** No threshold value
is set in this draft. They will be added to `config/defaults.yaml` and to
this document by an amendment dated and tagged before any graph run, and no
graph result will be seen before that amendment.

## Deviations

Anything changed after results are seen is reported as exploratory. Changes
made before any scored result exists are amendments and are logged here with
"seen-results" = no. A change made after any scored result (including pilot
results that inform it) is logged with "seen-results" = yes, and every
analysis it touches is labelled exploratory in `eval/REPORT.md`. The
`eval-prereg` tag is never moved; amendments are new commits, and the hashes
table is updated in the same commit as the file it covers.

**Carve-out (pre-specified pilot uses, not deviations):** re-sizing n by the
effective-n rule (B.3, B.9: n is set so the effective number of the recording
owner's tasks is at least `eval.min_effective_tasks`) and adopting the H-S2
outcome (minimal vs default system prompt) are both specified in advance as
uses of the pilot. Doing either is not a deviation and needs no
"seen-results" entry; any other use of pilot results is.

Amendments log:

| Date | Change | Reason | Seen-results (yes/no) |
|---|---|---|---|
| 2026-10-05 | The pilot's per-call cap is `eval.stages.pilot.max_budget_per_call_usd` (0.75), not `eval.max_budget_per_call_usd` (1.50); other stages keep the global key | The budget guard reserves each call's full cap against the pilot's $3 stage cap, so 1.50 would stop the pilot after about $1.50 of real spend (#13) | no |
| 2026-10-05 | The pilot stage cap is $3.75, not $3 | The first pilot run was booked $0.75 (its per-call cap, as an upper bound) for an extract call the CLI rejected at argument parsing, before any session or API request; real spend stays within the planned $3. Such calls are now booked at $0 (#13) | no |
