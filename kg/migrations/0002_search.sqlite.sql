-- Full-text search, SQLite only (C.4: FTS5 now, tsvector on Postgres). Read only by
-- kg/store.py search(); nothing else may depend on this table. A Postgres port adds
-- 0002_search.postgres.sql instead.
--
-- One row per searchable thing: kind 'entity' (its canonical name), 'alias' (a
-- resolved alias; ref_id is its entity), 'fact' (text + quote). Triggers keep it in
-- step, so no writer can forget to. Filtering (superseded, tombstoned, merged) happens
-- at query time, so supersede and tombstone need no index change.

CREATE VIRTUAL TABLE search_fts USING fts5 (kind UNINDEXED, ref_id UNINDEXED, body);

CREATE TRIGGER entity_fts_insert AFTER INSERT ON entity BEGIN
    INSERT INTO search_fts (kind, ref_id, body) VALUES ('entity', NEW.id, NEW.canonical_name);
END;

CREATE TRIGGER entity_fts_rename AFTER UPDATE OF canonical_name ON entity BEGIN
    DELETE FROM search_fts WHERE kind = 'entity' AND ref_id = OLD.id;
    INSERT INTO search_fts (kind, ref_id, body) VALUES ('entity', NEW.id, NEW.canonical_name);
END;

CREATE TRIGGER alias_fts_insert AFTER INSERT ON alias WHEN NEW.entity_id IS NOT NULL BEGIN
    INSERT INTO search_fts (kind, ref_id, body) VALUES ('alias', NEW.entity_id, NEW.alias);
END;

CREATE TRIGGER alias_fts_update AFTER UPDATE OF entity_id, alias ON alias BEGIN
    DELETE FROM search_fts WHERE kind = 'alias' AND ref_id = OLD.entity_id AND body = OLD.alias;
    INSERT INTO search_fts (kind, ref_id, body)
        SELECT 'alias', NEW.entity_id, NEW.alias WHERE NEW.entity_id IS NOT NULL;
END;

CREATE TRIGGER alias_fts_delete AFTER DELETE ON alias BEGIN
    DELETE FROM search_fts WHERE kind = 'alias' AND ref_id = OLD.entity_id AND body = OLD.alias;
END;

CREATE TRIGGER fact_fts_insert AFTER INSERT ON fact BEGIN
    INSERT INTO search_fts (kind, ref_id, body) VALUES ('fact', NEW.id, NEW.text || ' ' || NEW.quote);
END;

CREATE TRIGGER fact_fts_update AFTER UPDATE OF text, quote ON fact BEGIN
    DELETE FROM search_fts WHERE kind = 'fact' AND ref_id = OLD.id;
    INSERT INTO search_fts (kind, ref_id, body) VALUES ('fact', NEW.id, NEW.text || ' ' || NEW.quote);
END;

-- Rows written before this migration (none on a fresh database).
INSERT INTO search_fts (kind, ref_id, body) SELECT 'entity', id, canonical_name FROM entity;
INSERT INTO search_fts (kind, ref_id, body) SELECT 'alias', entity_id, alias FROM alias WHERE entity_id IS NOT NULL;
INSERT INTO search_fts (kind, ref_id, body) SELECT 'fact', id, text || ' ' || quote FROM fact;
