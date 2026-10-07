-- Retraction (pipeline/write.py, Store.retract_unwritten): a fact, edge or task that an
-- episode's earlier extraction produced and its latest write no longer does (the
-- transcript changed, or became a stub). Unlike a supersede there is no replacement
-- row; the corrected transcript just doesn't support it. Nothing is deleted: the row
-- stays, reads treat it as never valid, and writing the item again clears both columns.
-- `retract_reason` names the transcript sha256 the retracting write read. Shared SQL.
ALTER TABLE fact ADD COLUMN retracted_at TEXT;
ALTER TABLE fact ADD COLUMN retract_reason TEXT;
ALTER TABLE edge ADD COLUMN retracted_at TEXT;
ALTER TABLE edge ADD COLUMN retract_reason TEXT;
ALTER TABLE task ADD COLUMN retracted_at TEXT;
ALTER TABLE task ADD COLUMN retract_reason TEXT;
-- fact and edge have their episode indexes (0001); a retraction reads a task by episode too.
CREATE INDEX task_episode_idx ON task (episode_id);
