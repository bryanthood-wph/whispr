-- The /create-tasks review (D.3, D.5): who changed a task's status and why, and the
-- clarifications and inputs attached to it. Rows, never files (lesson L19). Shared SQL.
-- Numbered 0006: 0005 belongs to another branch (versions may have gaps, kg/db.py).

-- Who made a status change (NULL: the pipeline inserting the task, or a change made
-- before this migration), and what else it set, as JSON ({"tools_allowed": [...]} when
-- a task is marked ready). `note` (0001) is the why, `at` the when.
ALTER TABLE task_event ADD COLUMN actor TEXT;
ALTER TABLE task_event ADD COLUMN details TEXT;

-- A clarification (the user's answer to an open question about the task) or an input
-- the work needs (a file path, a link, a value), attached during review.
CREATE TABLE task_note (
    id      TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES task (id),
    kind    TEXT NOT NULL CHECK (kind IN ('clarification', 'input')),
    text    TEXT NOT NULL,
    actor   TEXT NOT NULL,
    at      TEXT NOT NULL
);
CREATE INDEX task_note_task_idx ON task_note (task_id);
