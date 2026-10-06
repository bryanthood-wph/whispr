# kg/ — knowledge graph and pipeline state

What it does: stores the graph (episodes, entities, aliases, facts, edges, tasks) and the pipeline's state (runs, items, alerts) in one SQLite database, schema by numbered migrations.
How to run it: it is a library; `kg.db.connect(cfg)` opens and migrates the database, `kg.store.Store(conn, cfg)` and `kg.state.State(conn, cfg)` read and write it.
Tests: `.venv\Scripts\python.exe -m unittest tests.test_kg -v`.
Config keys read: `paths.data_dir`, `kg.database`, `kg.busy_timeout_ms`, `kg.search_cards`, `ontology`, `schemas.task`, `pipeline.max_attempts`, `alerts.quarantine_digest_days`, `alerts.repeat_item_runs`.
Reserved for C.5 maintenance (in config, read by no code yet): `kg.er.max_edit_distance`, `kg.rederive_approval_usd`.

## Design

Plan: `docs/plan/C-knowledge-graph.md` §C.4–C.5, `docs/plan/D-architecture-and-ops.md` §D.1, §D.5, §D.6.

**Database.** `<paths.data_dir>/<kg.database>`, outside git. `connect` turns on foreign keys and WAL mode (readers never block a writer's COMMIT), waits up to `kg.busy_timeout_ms` for another process's lock, runs the connection in autocommit mode, and applies pending migrations in order. Writers group statements with `kg.db.transaction`, which nests through savepoints; its outermost level takes the write lock up front (`BEGIN IMMEDIATE`) and rolls back on any error, a failed COMMIT included, so the connection never stays inside a dead transaction. `fetch_one`/`fetch_all` return rows as dicts for both modules. Close the connection when done.

**Migrations** (`kg/migrations/`). `NNNN_name.sql` holds only SQL that SQLite and Postgres share: TEXT/INTEGER/REAL, TEXT ids made by code, no AUTOINCREMENT, no PRAGMA. `NNNN_name.<dialect>.sql` runs only on that dialect; full-text search is `0002_search.sqlite.sql` (FTS5), and a Postgres port adds `0002_search.postgres.sql` (tsvector) under the same version number. Each migration commits together with its `schema_version` row, so a failure leaves the previous version intact. The version row is written first, under the write lock, so two processes migrating a fresh database don't collide: the second finds the version taken and skips it. A database recording a version this code doesn't know is refused.

**Store** (`store.py`), the one data-access module for the graph:
- *Provenance root.* One episode per transcript (path, sha256, meeting time, call type, extractor version, mic coverage, output device). Episodes are tombstoned, never deleted; writing one again clears the tombstone.
- *Provenance check.* A fact or edge is `EXTRACTED` only if its quote is a verbatim substring of the transcript text passed with it; otherwise `AMBIGUOUS`.
- *Time.* Facts and edges carry `valid_from` (defaulting to the meeting start), `valid_to` and `superseded_by` + a reason. Supersede, never delete. Reads show active rows of live episodes.
- *People.* Keyed by email (`upsert_person`). Every name is an alias row. `mention_person` resolves a name to the single person with that alias; a first-name-only mention with no single match, or a name matching two people, stays an unresolved alias (lesson L12). A full name with no match becomes a person keyed `name:<name>`, for C.5 to merge.
- *Types.* Entity, fact and relation types come from `config/ontology.yaml`; an edge's endpoints must have the types its relation allows.
- *Tasks.* Inserted from the D.5 contract, validated against `config/schema/task.json`, id = sha1(episode + quote). Allowed transitions and the funnel order (captured → confirmed → ready → done; in_progress counts as ready) are rows seeded by migration 0001, checked on open against the schema's status enum. `funnel()` counts the furthest stage each task ever reached, from its event history.
- *Reads for MCP* (progressive disclosure): `search(text)` returns at most `kg.search_cards` short cards, `get(id)` the entity's facts with quotes and edges (or one fact, edge or task), `source(episode_id)` the transcript path, sha256 and quoted spans.

**State** (`state.py`):
- *Runs.* `finish_run` succeeds only if the run processed ≥ 1 item or proved zero eligible (lesson L1), records the backlog, and raises a `run-failed` alert otherwise. Negative counts, or more processed than eligible, raise `ValueError`. `begin_run` fails any run of the same job still marked running (it never finished: the process died or was killed) with the same alert, so a killed run is a failed run and the new one is its retry (D.6); a job runs one at a time.
- *Items.* One per (stage, ref). `begin_item` counts the attempt before the work, so a process-killing item still reaches quarantine after `pipeline.max_attempts`, with one alert naming the fix. `requeue` gives it fresh attempts. An item started in `alerts.repeat_item_runs` consecutive runs of its job raises a `repeat-item` alert.
- *Alerts.* Rows with a dedupe key, first/last seen, count, acknowledged. Raising a known key counts and reopens it. `quarantine_digest` moves items quarantined `alerts.quarantine_digest_days` ago into one digest alert, re-raised at most once per period unless new items join.
- *Maintenance.* `log_maintenance` and `record_metric` hold C.5's per-check log and health metrics; `entity_merge` (undo records) and `entity.merged_into` are in the schema for C.5's merges.

## Known gaps

- A person mention after a merge returns the merged-away entity id; `mention_person` should follow `entity.merged_into` (for C.5).
- Alias FTS triggers delete index rows by (entity, body), so two alias rows with the same text share one index row and deleting one loses it; store the alias id in an UNINDEXED column (for C.5).
- `requeue` leaves the item's quarantine alert open, an empty digest leaves a stale digest alert open, and retry backoff isn't stored (stream A stage runner).
- A task inserted as `dropped` is missing from the funnel (stream E).
