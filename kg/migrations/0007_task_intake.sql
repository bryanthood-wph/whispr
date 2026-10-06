-- The task intake (docs/plan/task-intake-and-worker.md §3, §4): a task's brief and a
-- project's default scope. Rows, never files (lesson L19). Shared SQL.

-- A brief answer is a task_note row of kind 'brief': `field` names the brief field
-- (tasks.intake.fields), `text` holds the answer (the scope field's as a JSON list of
-- {type, value}), `source` says who gave it (tasks.intake.answer_sources: stated or
-- confirmed). The newest row per field is the answer; older ones are its history. The
-- kind CHECK of 0006 can't be altered in place, so the table is rebuilt with every
-- existing row copied unchanged, in its old order (field and source stay NULL for them).
CREATE TABLE task_note_new (
    id      TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES task (id),
    kind    TEXT NOT NULL CHECK (kind IN ('clarification', 'input', 'brief')),
    field   TEXT,
    source  TEXT,
    text    TEXT NOT NULL,
    actor   TEXT NOT NULL,
    at      TEXT NOT NULL,
    CHECK ((kind = 'brief') = (field IS NOT NULL AND source IS NOT NULL))
);
INSERT INTO task_note_new (id, task_id, kind, text, actor, at)
    SELECT id, task_id, kind, text, actor, at FROM task_note ORDER BY rowid;
DROP TABLE task_note;
ALTER TABLE task_note_new RENAME TO task_note;
CREATE INDEX task_note_task_idx ON task_note (task_id);
CREATE INDEX task_note_brief_idx ON task_note (task_id, field, at);

-- A project's default scope (tasks.intake.scope_entity_type), proposed at intake and
-- edited per task: one row per (source type, value). `value` may be tasks.intake.scope_none,
-- "this project has nothing of that type". project_scope_set replaces an entity's rows.
CREATE TABLE entity_scope (
    entity_id   TEXT NOT NULL REFERENCES entity (id),
    source_type TEXT NOT NULL,
    value       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (entity_id, source_type, value)
);
