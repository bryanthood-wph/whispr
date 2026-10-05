# G. Git strategy and work tracking

> **One screen.** `master` stays production in `C:\github\whispr`, because
> all four live scheduled tasks run from that folder. The build happens on a
> `rebuild` branch in a separate worktree, with `feat/<issue>-<slug>`
> branches cut from it. Work is tracked in GitHub Issues: one milestone per
> phase, one issue per deliverable, and a `gate` issue for each decision
> that's yours. Cutover is one PR, after the gates pass.

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
| `feat/<issue#>-<slug>` | One per issue, cut from `rebuild`, squash-merged back by PR | in the rebuild worktree |
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
  local and remote, is deleted with your OK in Phase 0.

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
- **The change-record template** (README §5) is an issue template. A PR
  that changes behavior must link the issue and its scorecard diff.
- **PRs say `Closes #N`**, so the milestones show progress with no separate
  status document.
