---
name: Change record
about: Any behavior change from the pilot onward (docs/plan/README.md §5)
labels: ''
---

**Why** — hypothesis, the miss/phantom ledger rows it targets (scorecard `results/<run-tag>/scorecard.json`), or the lesson ID (`Lnn`):

**What** — config diff and/or code diff:

**Evidence** — scorecard diff: run tag before → after, metric(s), paired CI:

**Decision rule** — the pre-registered rule it meets, or "guardrail" (a lesson fix proven by a test):

**Simplicity** — README §8 rows it moves:

**Regression** — `python -m eval regress` result (required once an accepted scorecard exists):
