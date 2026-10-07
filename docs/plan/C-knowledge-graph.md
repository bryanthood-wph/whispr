# C. Knowledge graph (replaces the Obsidian vault)

> **One screen.** The graph is a structured store of what calls produced:
> people, projects, decisions, facts, tasks. Every fact carries a verbatim
> quote from a transcript, and the graph can always be **rebuilt from the
> transcripts**. Claude reads it through MCP, and you see the relationships
> in a Claude Code view.
>
> **How it gets built.**
> - **Phase 0:** a desk survey of existing tools ($0). The graph shape the
>   extractor will emit (`ontology.yaml` v1) is fixed at the same time, so
>   the eval doesn't pay for output no tool can accept.
> - **Phase 6:** **adopt the tool if it passes the must-haves** (user
>   decision). Otherwise build a small SQLite core.
>
> Either way, the result must pass the same graph evals. Anything the evals
> haven't shown we need is deferred (README §3, deferred table).

## C.1 Requirements (user, 2026-10-04)

The graph must:
- not use Obsidian
- be structured and able to evolve
- show relationships visually
- be read through Claude only
- sustain and heal itself
- have its own evals
- use SQLite now and be Postgres-ready
- be rebuilt from the transcripts

## C.2 Step 1: tool survey (desk part in Phase 0 at $0; smoke test in Phase 6 inside its $12)

**Candidates (at minimum)**
- Graphify
- Cognee
- LightRAG
- Microsoft GraphRAG (local)
- Graphiti
- mem0 graph memory
- any SQLite- or NetworkX-backed library found along the way

Every claim is verified from the tool's own repo, docs and license file.

**Must-haves**
- runs natively on Windows with an embedded store (no server, WSL or Docker)
- facts supersede rather than overwrite
- entity resolution
- works with Claude, with no external embeddings
- **accepts our pre-extracted entities, facts and edges with no second LLM
  extraction pass**, so the one-call design and its cost hold
- a license that allows redistribution to colleagues
- actively maintained
- can run headless

**Scored criteria**
- setup steps
- code we would own
- extraction quality on a 5-transcript smoke test
- cost per transcript
- visualization
- MCP access

**Output:**
- **Phase 0:** a must-haves table (desk research), which you confirm at
  gate 0.
- **Phase 6:** for the survivors, a scored table including the smoke test,
  which you confirm at gate 3.

**Must-haves table (desk survey, 2026-10-04; for you to confirm at gate 0, issue #1)**

Every cell was checked against the tool's own repo, docs or LICENSE file.
Columns: 1 Windows + embedded store · 2 supersede · 3 entity resolution ·
4 Claude, no mandatory embedding API · 5 pre-extracted insert, no second
extraction · 6 redistributable license · 7 maintained · 8 headless.

| Tool | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | Survives |
|---|---|---|---|---|---|---|---|---|---|
| Graphify | ✓ | ✗ | ✓ | ✓ | ✓ | ✓ Apache-2.0 | ✓ | ✓ | no (2) |
| Cognee | ✓ | ✓ | ? | ✓ | ✓ | ✓ Apache-2.0 | ✓ | ✓ | **yes** |
| LightRAG | ✓ | ✗ | ✓ | ✓ | ✓ | ✓ MIT | ✓ | ✓ | no (2) |
| MS GraphRAG | ✓ | ✗ | ? | ? | ✓ | ✓ MIT | ✓ (maintenance mode) | ✓ | no (2) |
| Graphiti | ✗ | ✓ | ✓ | ✓ | ✓ | ✓ Apache-2.0 | ✓ | ✓ | no (1) |
| mem0 | ✗ | ? | ✗ | ? | ✗ | ✓ Apache-2.0 | ✓ | ✓ | no (1, 3, 5) |
| MemPalace `KnowledgeGraph` | ✓ | ✓ | ? | ✓ | ✓ | ✓ MIT | ✓ | ✓ | **yes** |
| DomLynch/Temporal | ? | ✓ | ✓ | ✓ | ✓ | ✓ MIT | ✓ (single author, 6 months old) | ✓ | yes, weak |
| Mnemosyne `TripleStore` | ? | ✓ | ? | ? | ✓ | ✓ MIT | ✓ (6 months old) | ✓ | yes, weak |

✓ pass · ✗ fail · ? unclear from the tool's own sources.

**Why the failures fail**
- **Graphify:** no time or validity model, and `update` re-extracts, which
  overwrites. (Its license is now Apache-2.0, which corrects the earlier
  "no license" note.) It stays the C.3 comparator.
- **LightRAG, GraphRAG:** no supersede. GraphRAG's README also calls it
  "largely in maintenance mode".
- **Graphiti:** its core needs a Neo4j, FalkorDB or Neptune server. Kuzu is
  deprecated, and FalkorDBLite isn't documented on Windows.
- **mem0:** graph memory left the open-source SDK in v3 (it is hosted-only),
  and `add()` always runs its own extraction.

**Survivors go to the Phase 6 smoke test:** Cognee and MemPalace
`KnowledgeGraph`. Temporal and Mnemosyne are optional extras.

**Risks the smoke test must settle**
- **Cognee:**
  - Supersede is persisted only by its default store, Ladybug, a Kuzu fork
    whose health is unverified.
  - Search doesn't filter out superseded facts.
  - An embedding provider must be configured. Local `fastembed` works, but
    it downloads model weights once.
- **MemPalace:**
  - Its graph module is stdlib SQLite, but the wider package pulls in
    ChromaDB, which has a known install regression.
  - Entity resolution is name normalization only.
- **Both:** fuzzy merge of pre-inserted entities is unverified. If either
  one fails on merge or Windows, the fallback is the minimal SQLite core
  (C.3).

## C.3 Step 2: adopt, or build the minimal core

- **A tool passes:** adopt it, and write only the gap code needed to meet
  §C.4.
- **Nothing passes:** build the core below. It is written to be small:
  - the graph writer is deterministic code, never the model
  - the model only fills the unified output schema (Appendix B.8)

**Graphify comparator.** If its license permits, Graphify runs once over the
same 50 core summaries, supervised, and is scored by §C.6. If it can't run,
the report says **"not run"**, never "lost".

## C.4 What the result must do, whichever path wins

| Must do | Minimal form |
|---|---|
| Provenance root | one **episode** per transcript (path, sha256, meeting time, extractor version). The graph is rebuildable from episodes. |
| Facts change over time | each fact and edge has `valid_from` / `valid_to` plus `superseded_by`. Supersede, never delete. An item a re-written transcript no longer supports is retracted (`retracted_at` + `retract_reason`, no replacement), kept and never valid. |
| Trustworthy edges | `EXTRACTED` only if the verbatim quote passes a substring check; otherwise `AMBIGUOUS` |
| Typed, versioned ontology | types live in `config/ontology.yaml` and change by PR, with a version bump |
| People resolved | people keyed by email from Outlook attendees; aliases table; merges by typed decision with an undo record |
| Read through Claude | MCP tools using progressive disclosure: `search` returns short cards → `get` returns facts with quotes → `source` returns the transcript span |
| See relationships | a **local Claude Code plugin mod**, filterable by type, person and time window. An artifact or a browser `graph.html` is used only after an explicit gate: an artifact stores meeting content off the machine, and a browser view is outside Claude. |
| Search | full-text search behind the data-access module (FTS5 now, `tsvector` on Postgres) + graph traversal |
| Tasks | the same store (Appendix D.5 contract) |

**Schema** (numbered SQL migrations, using only SQL that SQLite and Postgres
share, behind one data-access module):

```
episode   (id, transcript_path, sha256, meeting_start, call_type, extractor_version, mic_coverage,
           output_device, deleted_at)                                 -- deleted_at = tombstone
entity    (id, type, canonical_name, canonical_key, created_at)      -- people keyed by email
alias     (entity_id, alias, source)
fact      (id, type, text, quote, episode_id, subject_entity_id, provenance, confidence,
           valid_from, valid_to, superseded_by, retracted_at, recorded_at)
edge      (id, src_entity_id, dst_entity_id, relation, episode_id, provenance, confidence,
           valid_from, valid_to, superseded_by, retracted_at, recorded_at)
task      (Appendix D.5 contract + entity links + retracted_at)  -- retracted_at: no longer in
                                                                 -- the episode's latest write
maintenance_log (run_id, check, found, repaired, escalated, details)
```

To move to Postgres later: change the connection string in config and run
one migration. That is worth doing only for a shared, team-wide graph.

## C.5 Self-sustaining and self-healing (one nightly job, after ingest)

Every repair is logged and reversible. Records are superseded with a reason
and never deleted.

1. **Entity resolution.** Candidates come from the same email, alias
   overlap or name edit distance ≤ `kg.er.max_edit_distance`. A typed "same
   entity?" decision is made, with escalation. A first-name-only mention
   stays an unresolved alias and never becomes a new entity (lesson L12). A merge repoints edges, keeps aliases and writes an undo
   record.
2. **Contradictions.** A new fact that conflicts with an active one on the
   same subject and attribute (a moved due date, a new owner, a reversed
   decision) gets a typed decision. If confirmed, the old fact is superseded.
3. **Integrity.** Dangling references are repaired. Orphans are flagged,
   not deleted. A fact whose quote check fails is downgraded to
   `AMBIGUOUS`.
4. **Rebuild is the ultimate repair.** It is always possible.
   - A re-derivation after an extractor or ontology change shows a cost
     estimate first.
   - Above `kg.rederive_approval_usd` (config), it waits for your approval
     (lesson L3).
5. **Health metrics** are recorded each run:
   - duplicate rate
   - `AMBIGUOUS` share
   - orphan rate
   - open contradictions
   - unresolved escalations

   An alert fires only on regression, through the one alert surface
   (Appendix D.6).

## C.6 Graph evals (same harness, same scorecard, pre-registered)

Pass thresholds per suite are the `eval.thresholds.kg.*` keys. They are
set in the preregistration before any graph run.

| Suite | Method | Metric |
|---|---|---|
| Extraction | facts and edges vs the consensus reference (B.4) | precision / recall by type |
| Entity resolution | planted aliases + false-merge traps (two different "Chris") | B-cubed precision / recall |
| Temporal | synthetic later episodes that move a date, change an owner, reverse a decision | old fact superseded, new one active, as-of answers correct |
| Retrieval QA | questions generated from reference items, answered via MCP | answer correctness + citation validity |
| Self-healing | injected duplicate, dangling edge, stale fact, wrong merge | detect + repair rate; second run is a no-op |

## C.7 Deferred items

All deferrals, graph ones included (embeddings, community detection,
rendered pages, model-proposed types, system-time columns), are in **one
table in README §3**, so there is a single place to look.

## C.8 Rebuild from transcripts (Phase 8, production spend)

- **What runs:** all transcripts go through the winning extractor.
- **Estimated cost:**
  - ~$30–55 at Sonnet, or ~$70–110 at Opus, approved at the gate
  - plus a calendar **re-match** for the 48 transcripts with generic titles
    (lesson L15). That is local and $0.
- **Before the rebuild:**
  - snapshot cohoodOBS, then make it a read-only archive
  - list the `human-override` blocks and filed `analysis` pages for manual
    carry-over. They can't be derived from transcripts.
