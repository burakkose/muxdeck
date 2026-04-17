CREATE TABLE IF NOT EXISTS replay_annotations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('bookmark','note')),
    body TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_replay_annotations_session_ordinal
    ON replay_annotations(session_id, ordinal);

CREATE UNIQUE INDEX IF NOT EXISTS idx_replay_annotations_unique_bookmark
    ON replay_annotations(session_id, ordinal)
    WHERE kind = 'bookmark';
