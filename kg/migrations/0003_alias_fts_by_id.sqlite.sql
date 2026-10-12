-- Alias rows in search_fts keyed by alias id, SQLite only (kg/README.md Known gaps).
-- Until now an alias's index row carried its entity id and was deleted by (entity,
-- body), so two alias rows with the same text shared one index row and deleting or
-- moving one lost it. Now ref_id is the alias's own id, every alias is indexed
-- (unresolved ones too), and kg/store.py search() maps an alias hit to the alias's
-- entity at query time: attaching a mention or a merge repointing an alias needs no
-- index change. A Postgres port adds 0003_alias_fts_by_id.postgres.sql instead.

DROP TRIGGER alias_fts_insert;
DROP TRIGGER alias_fts_update;
DROP TRIGGER alias_fts_delete;
DELETE FROM search_fts WHERE kind = 'alias';

CREATE TRIGGER alias_fts_insert AFTER INSERT ON alias BEGIN
    INSERT INTO search_fts (kind, ref_id, body) VALUES ('alias', NEW.id, NEW.alias);
END;

CREATE TRIGGER alias_fts_update AFTER UPDATE OF alias ON alias BEGIN
    DELETE FROM search_fts WHERE kind = 'alias' AND ref_id = OLD.id;
    INSERT INTO search_fts (kind, ref_id, body) VALUES ('alias', NEW.id, NEW.alias);
END;

CREATE TRIGGER alias_fts_delete AFTER DELETE ON alias BEGIN
    DELETE FROM search_fts WHERE kind = 'alias' AND ref_id = OLD.id;
END;

INSERT INTO search_fts (kind, ref_id, body) SELECT 'alias', id, alias FROM alias;
