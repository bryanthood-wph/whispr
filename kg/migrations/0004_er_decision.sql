-- C.5 "same entity?" decisions the model made (kg/resolve.py), kept so a pair is not
-- asked again on every pass. Shared SQL. One row per unordered pair (a_id < b_id);
-- a_hash / b_hash fingerprint each side's names and key (email) when it was decided,
-- so the pair is asked again once either side is renamed or gains an email.
CREATE TABLE er_decision (
    a_id       TEXT NOT NULL REFERENCES entity (id),
    b_id       TEXT NOT NULL REFERENCES entity (id),
    decision   TEXT NOT NULL,                       -- same | different | unsure
    reason     TEXT,
    a_hash     TEXT NOT NULL,
    b_hash     TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (a_id, b_id)
);
