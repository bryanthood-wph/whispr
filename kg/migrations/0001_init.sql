-- Knowledge graph + pipeline state (docs/plan/C-knowledge-graph.md C.4 and C.5,
-- docs/plan/D-architecture-and-ops.md D.1, D.5, D.6).
--
-- Shared SQL only: every statement here runs unchanged on SQLite and Postgres.
-- TEXT/INTEGER/REAL columns, TEXT ids made by code (kg/db.py), no AUTOINCREMENT,
-- no PRAGMA, no SQLite-only syntax. Times are ISO-8601 UTC text, so they sort.
-- Nothing in the graph is deleted: episodes are tombstoned, facts and edges are
-- superseded, merges keep an undo record.

-- One episode per transcript: the provenance root the graph is rebuilt from.
CREATE TABLE episode (
    id                TEXT PRIMARY KEY,
    transcript_path   TEXT NOT NULL,
    sha256            TEXT NOT NULL,
    meeting_start     TEXT,
    call_type         TEXT,
    extractor_version TEXT,
    mic_coverage      REAL,
    output_device     TEXT,
    deleted_at        TEXT,             -- tombstone
    recorded_at       TEXT NOT NULL
);

-- canonical_key: a person's email (lower case); any other type's normalized name.
CREATE TABLE entity (
    id             TEXT PRIMARY KEY,
    type           TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    canonical_key  TEXT NOT NULL,
    merged_into    TEXT REFERENCES entity (id),     -- set by a C.5 merge; NULL = live
    created_at     TEXT NOT NULL,
    UNIQUE (type, canonical_key)
);

-- entity_id NULL = an unresolved alias: a first name alone never becomes an
-- entity (lesson L12); C.5 entity resolution may attach it later.
CREATE TABLE alias (
    id         TEXT PRIMARY KEY,
    entity_id  TEXT REFERENCES entity (id),
    alias      TEXT NOT NULL,
    alias_key  TEXT NOT NULL,                       -- normalized form, for matching
    source     TEXT NOT NULL,                       -- outlook | extract | maintain | ...
    episode_id TEXT REFERENCES episode (id),
    created_at TEXT NOT NULL
);
CREATE INDEX alias_key_idx ON alias (alias_key);
CREATE INDEX alias_entity_idx ON alias (entity_id);

-- provenance EXTRACTED only when the quote is a verbatim substring of the episode's
-- transcript; otherwise AMBIGUOUS.
CREATE TABLE fact (
    id                TEXT PRIMARY KEY,
    type              TEXT NOT NULL,
    text              TEXT NOT NULL,
    quote             TEXT NOT NULL,
    quote_start       TEXT,                         -- hh:mm:ss of the supporting turn
    episode_id        TEXT NOT NULL REFERENCES episode (id),
    subject_entity_id TEXT REFERENCES entity (id),
    provenance        TEXT NOT NULL CHECK (provenance IN ('EXTRACTED', 'AMBIGUOUS')),
    confidence        REAL,
    valid_from        TEXT,
    valid_to          TEXT,
    superseded_by     TEXT REFERENCES fact (id),
    supersede_reason  TEXT,
    recorded_at       TEXT NOT NULL
);
CREATE INDEX fact_episode_idx ON fact (episode_id);
CREATE INDEX fact_subject_idx ON fact (subject_entity_id);

CREATE TABLE edge (
    id               TEXT PRIMARY KEY,
    src_entity_id    TEXT NOT NULL REFERENCES entity (id),
    dst_entity_id    TEXT NOT NULL REFERENCES entity (id),
    relation         TEXT NOT NULL,
    quote            TEXT NOT NULL,
    quote_start      TEXT,
    episode_id       TEXT NOT NULL REFERENCES episode (id),
    provenance       TEXT NOT NULL CHECK (provenance IN ('EXTRACTED', 'AMBIGUOUS')),
    confidence       REAL,
    valid_from       TEXT,
    valid_to         TEXT,
    superseded_by    TEXT REFERENCES edge (id),
    supersede_reason TEXT,
    recorded_at      TEXT NOT NULL
);
CREATE INDEX edge_src_idx ON edge (src_entity_id);
CREATE INDEX edge_dst_idx ON edge (dst_entity_id);
CREATE INDEX edge_episode_idx ON edge (episode_id);

-- Task lifecycle (D.5). The statuses must equal config/schema/task.json's status enum
-- (kg/store.py checks on open). `reaches` names the funnel stage a status implies
-- the task got to (in_progress implies ready); funnel_rank orders the funnel
-- captured -> confirmed -> ready -> done. Lifecycle state lives only here (lesson L19).
CREATE TABLE task_status (
    status      TEXT PRIMARY KEY,
    reaches     TEXT REFERENCES task_status (status),
    funnel_rank INTEGER
);
INSERT INTO task_status (status, reaches, funnel_rank) VALUES ('captured', 'captured', 1);
INSERT INTO task_status (status, reaches, funnel_rank) VALUES ('confirmed', 'confirmed', 2);
INSERT INTO task_status (status, reaches, funnel_rank) VALUES ('ready', 'ready', 3);
INSERT INTO task_status (status, reaches, funnel_rank) VALUES ('done', 'done', 4);
INSERT INTO task_status (status, reaches, funnel_rank) VALUES ('in_progress', 'ready', NULL);
INSERT INTO task_status (status, reaches, funnel_rank) VALUES ('dropped', NULL, NULL);

-- The allowed status changes. done is final; dropped can be undone back to captured.
CREATE TABLE task_transition (
    from_status TEXT NOT NULL REFERENCES task_status (status),
    to_status   TEXT NOT NULL REFERENCES task_status (status),
    PRIMARY KEY (from_status, to_status)
);
INSERT INTO task_transition (from_status, to_status) VALUES ('captured', 'confirmed');
INSERT INTO task_transition (from_status, to_status) VALUES ('captured', 'dropped');
INSERT INTO task_transition (from_status, to_status) VALUES ('confirmed', 'ready');
INSERT INTO task_transition (from_status, to_status) VALUES ('confirmed', 'captured');
INSERT INTO task_transition (from_status, to_status) VALUES ('confirmed', 'dropped');
INSERT INTO task_transition (from_status, to_status) VALUES ('ready', 'in_progress');
INSERT INTO task_transition (from_status, to_status) VALUES ('ready', 'done');
INSERT INTO task_transition (from_status, to_status) VALUES ('ready', 'confirmed');
INSERT INTO task_transition (from_status, to_status) VALUES ('ready', 'dropped');
INSERT INTO task_transition (from_status, to_status) VALUES ('in_progress', 'done');
INSERT INTO task_transition (from_status, to_status) VALUES ('in_progress', 'ready');
INSERT INTO task_transition (from_status, to_status) VALUES ('in_progress', 'dropped');
INSERT INTO task_transition (from_status, to_status) VALUES ('dropped', 'captured');

-- The D.5 contract, one column per field (source -> episode_id + source_start;
-- tools_allowed is a JSON array).
CREATE TABLE task (
    id            TEXT PRIMARY KEY,                 -- sha1(episode + quote)
    owner         TEXT,
    owner_basis   TEXT NOT NULL,
    action        TEXT NOT NULL,
    due           TEXT,
    due_basis     TEXT NOT NULL,
    context       TEXT NOT NULL,
    quote         TEXT NOT NULL,
    episode_id    TEXT NOT NULL REFERENCES episode (id),
    source_start  TEXT,
    confidence    REAL NOT NULL,
    status        TEXT NOT NULL REFERENCES task_status (status),
    tools_allowed TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX task_status_idx ON task (status);

CREATE TABLE task_entity (
    task_id   TEXT NOT NULL REFERENCES task (id),
    entity_id TEXT NOT NULL REFERENCES entity (id),
    PRIMARY KEY (task_id, entity_id)
);

-- Every status a task has held, first one included: the funnel counts the furthest
-- stage each task ever reached, not just where it sits now.
CREATE TABLE task_event (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES task (id),
    from_status TEXT REFERENCES task_status (status),
    to_status   TEXT NOT NULL REFERENCES task_status (status),
    note        TEXT,
    at          TEXT NOT NULL
);
CREATE INDEX task_event_task_idx ON task_event (task_id);

-- Pipeline runs (D.1). A run succeeds only if it processed >= 1 item or proved there
-- were none eligible (lesson L1). seq orders runs of a job without trusting clock ties.
CREATE TABLE run (
    id          TEXT PRIMARY KEY,
    seq         INTEGER NOT NULL UNIQUE,
    job         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    processed   INTEGER,
    eligible    INTEGER,                            -- NULL = never counted, so not proven zero
    backlog     INTEGER,
    error       TEXT
);
CREATE INDEX run_job_idx ON run (job, seq);

-- One row per transcript/episode per stage (D.1: one bad item never blocks the queue).
-- attempts counts starts, so an item that kills the process still reaches quarantine.
CREATE TABLE item (
    id               TEXT PRIMARY KEY,
    stage            TEXT NOT NULL,
    ref              TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('queued', 'done', 'quarantined')),
    attempts         INTEGER NOT NULL,
    last_error       TEXT,
    quarantined_at   TEXT,
    digested_at      TEXT,                          -- moved into the quarantine digest
    consecutive_runs INTEGER NOT NULL,
    last_run_id      TEXT REFERENCES run (id),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (stage, ref)
);
CREATE INDEX item_stage_status_idx ON item (stage, status);

-- The one alert surface (D.6). Open = neither acknowledged nor expired (expired: folded
-- into the quarantine digest). Raising a known dedupe_key counts it and reopens it.
CREATE TABLE alert (
    id              TEXT PRIMARY KEY,
    dedupe_key      TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL,
    message         TEXT NOT NULL,
    fix             TEXT NOT NULL,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    count           INTEGER NOT NULL,
    acknowledged_at TEXT,
    expired_at      TEXT
);

-- C.5 nightly maintenance: one row per check per run, plus the run's health metrics.
CREATE TABLE maintenance_log (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES run (id),
    check_name  TEXT NOT NULL,                      -- "check" is reserved in SQL
    found       INTEGER NOT NULL,
    repaired    INTEGER NOT NULL,
    escalated   INTEGER NOT NULL,
    details     TEXT,
    recorded_at TEXT NOT NULL
);

CREATE TABLE health_metric (
    run_id TEXT NOT NULL REFERENCES run (id),
    name   TEXT NOT NULL,
    value  REAL NOT NULL,
    PRIMARY KEY (run_id, name)
);

-- C.5 merge undo record: what a merge repointed, so it can be reversed exactly.
CREATE TABLE entity_merge (
    id         TEXT PRIMARY KEY,
    kept_id    TEXT NOT NULL REFERENCES entity (id),
    merged_id  TEXT NOT NULL REFERENCES entity (id),
    decision   TEXT NOT NULL,                       -- the typed decision, as JSON
    undo       TEXT NOT NULL,                       -- repointed rows, as JSON
    run_id     TEXT REFERENCES run (id),
    merged_at  TEXT NOT NULL,
    undone_at  TEXT
);
