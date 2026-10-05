# G. Git strategy and work tracking

> **One screen.** `master` stays production in `C:\github\whispr`, because
> all four live scheduled tasks run from that folder. The build happens on a
> `rebuild` branch in a separate worktree, with `feat/<issue>-<slug>`
> branches cut from it. Work is tracked in GitHub Issues: one milestone per
> phase, one issue per deliverable, and a `gate` issue for each decision
> that's yours. Each issue follows one loop (G.5), and every stop leaves a
> `RESUME.md`. Cutover is one PR, after the gates pass.

## G.1 Why the main folder never changes branch (verified 2026-10-04)

These four tasks all execute scripts from `C:\github\whispr`:
- `whispr-recorder`
- `whispr-nightly-ingest`
- `whispr-nightly-freshness-check`
- `whispr-weekly-lint-compile`

A `git checkout` there would change what records and syncs tonight. Using a
separate worktree is how "keep everything running" is enforced
mechanically.

## G.2 Branches

| Branch | Role | Location |
|---|---|---|
| `master` | Production. Changes only through **gate-approved hotfixes** and the cutover. | `C:\github\whispr` |
| `rebuild` | Integration branch for this plan. Always green on unit tests and the harness dry-run, plus `eval regress` once an accepted scorecard exists (B.7). | `git worktree add C:\github\whispr-rebuild rebuild` |
| `feat/<issue#>-<slug>` | One per issue, cut from `rebuild`, squash-merged back (G.5) | in the rebuild worktree |
| `hotfix/<slug>` | An urgent production fix. Cut from `master`, merged to `master`, then **`master` is merged into `rebuild` the same day** | main folder, briefly |

## G.3 Rules

- **Scored eval runs come from a tagged commit** (`eval-pilot`,
  `eval-run1`, …). The tag and SHA go into every scorecard (Appendix B.7).
- **Eval runs are one-shot scheduled tasks generated from `eval.schedule`**
  (B.9). They point at the rebuild worktree path, never at production, and
  are never started from inside a Claude session.
- **Data stays out of git:** transcripts, audio, results, cache and the
  database. `eval/` commits only code, the preregistration, schemas and
  `REPORT.md`.
- **`master` is merged into `rebuild`** weekly, and before every scored
  run.
- **Cutover (Phase 8).**
  1. Tag `master` `pre-cutover`.
  2. Merge one PR `rebuild` → `master`, tagged `v0.2.0`.
  3. Switch tasks one at a time from config, with `pipeline schedule
     --apply`.

  Rollback: check out `pre-cutover` in the main folder, then re-apply the
  schedule.
- **There is no branch protection.** It needs a paid plan on a private
  repo, so the harness enforces the checks locally: clean tree, tagged
  commit, regression gate.
- **Pushing, `gh issue` and `gh pr`** all use the CLAUDE.md account-switch
  procedure. The repo is private to `bryanthood-wph`.
- **Housekeeping:** the merged `fix/nightly-ingest-idempotency` branch,
  local and remote, is deleted by you (issue #2). The agent's attempt was
  blocked by a permission rule.

## G.4 Work tracking: GitHub Issues + Milestones

- **Milestones:** `P0 Prep` … `P8 Implement`. Each phase's exit criterion
  (README §7) is the milestone description.
- **Issues:** one per deliverable, with acceptance checks copied from the
  plan section it implements.
- **Labels:**
  - `phase:N`
  - area: `area:eval`, `area:pipeline`, `area:kg`, `area:plugin`,
    `area:asr`, `area:ops`
  - `gate`: assigned to you. Work behind it waits until you close it with
    the decision recorded.
  - `blocked`
  - `lesson:Lnn`: an issue that implements a guardrail from Appendix E
- **The change-record template** (README §5) is
  `.github/ISSUE_TEMPLATE/change-record.md`. A change that alters behavior
  must link the issue and its scorecard diff.
- **Issues are closed by hand** with the squash commit's SHA. GitHub's
  `Closes #N` only acts on the default branch (`master`), and features merge
  into `rebuild`. The milestones still show progress, with no separate
  status document.

## G.5 The per-issue loop (testing, one commit per feature, iterating on failure)

The task list is the open issues, worked in milestone order. Within a
milestone, work the lowest number that isn't `blocked` and isn't a `gate`.

1. **Start.** Cut `feat/<issue#>-<slug>` from `rebuild` in
   `C:\github\whispr-rebuild`. Re-read the issue's acceptance checks and
   the plan section it cites, then check Appendix E for its lessons.
2. **Test first.** Write or extend tests for each acceptance check before
   or alongside the code. Tests never make model calls; they use a fake
   CLI or a fake response.
3. **Run.** Run the unit tests and, once it exists, the harness dry-run
   (`python -m eval run --dry-run`).
4. **When a test fails, iterate.**
   - Fix and re-run.
   - After **two failed attempts**, stop changing code. Write the
     hypothesis in the issue ("X fails because Y"), re-read the code and
     the error, then make one targeted change.
   - After a third failure, check the system: env, auth, branch, config
     drift. If it's still red, label the issue `blocked` with the evidence,
     and move to the next issue.
5. **Commit by feature.** One logical change per commit, and the message
   says what and why. Squash-merge into `rebuild` as
   `"<title> (#N)"`. `rebuild` must stay green.
6. **Close.** Close the issue with the SHA and the evidence: what ran and
   what it showed.
7. **Push** `rebuild` using the CLAUDE.md account-switch procedure.

**Model spend.** Any step that spends money runs as a one-shot scheduled
task (G.3), never from a Claude session. Each run's cost goes into the
run ledger, and `python -m eval status` reports the total against
`eval.budget_usd`.

**Stopping and resuming (`RESUME.md`).** The work stops at:
- every `gate` issue, which waits for you
- a cumulative spend within $5 of `eval.budget_usd` ($95 of $100)
- a `blocked` issue that has nothing else workable behind it

At a stop, the agent overwrites `RESUME.md` at the root of the rebuild
worktree with:
- the stop reason
- the branch and HEAD SHA
- spend to date from the ledger
- the issue in progress and its next step
- what you need to decide
- the exact first command to run when you say "continue"

"Continue" means: read `RESUME.md`, verify the branch and SHA match, and
pick up at that step.
