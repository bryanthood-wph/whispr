-- The task intake (docs/plan/task-intake-and-worker.md §3, §4): a task's brief and a
-- project's default scope. Rows, never files (lesson L19). Postgres twin of
-- 0007_task_intake.sqlite.sql: the same columns, constraints, index and table, with the
-- kind CHECK altered in place rather than the table rebuilt. Keep the two alike.

-- A brief answer is a task_note row of kind 'brief' with its field and source (see the
-- SQLite file). Existing rows keep field and source NULL.
ALTER TABLE task_note ADD COLUMN field TEXT;
ALTER TABLE task_note ADD COLUMN source TEXT;
-- 0006 declared the kind CHECK inline, which Postgres names <table>_<column>_check.
ALTER TABLE task_note DROP CONSTRAINT task_note_kind_check;
ALTER TABLE task_note ADD CONSTRAINT task_note_kind_check CHECK (kind IN ('clarification', 'input', 'brief'));
ALTER TABLE task_note ADD CONSTRAINT task_note_brief_check
    CHECK ((kind = 'brief') = (field IS NOT NULL AND source IS NOT NULL));
CREATE INDEX task_note_brief_idx ON task_note (task_id, field, at);

-- A project's default scope (see the SQLite file).
CREATE TABLE entity_scope (
    entity_id   TEXT NOT NULL REFERENCES entity (id),
    source_type TEXT NOT NULL,
    value       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (entity_id, source_type, value)
);
