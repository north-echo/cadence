-- Collector-run audit table (WP-14).
--
-- Every successful or failed invocation of `cadence collect <source>` records
-- a row here. The health command and the optional metrics endpoint use it to
-- answer "when did each source last run, and was it healthy?" — distinct from
-- "when was the last new record persisted", which is what the per-table
-- collected_at columns capture.

CREATE TABLE collection_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,            -- 'rhsa' | 'csaf' | 'repodata' | 'catalog' | 'quay'
    started_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP NOT NULL,
    records INTEGER NOT NULL,
    errors INTEGER NOT NULL,
    error_messages TEXT              -- JSON array of strings; NULL when errors = 0
);

CREATE INDEX idx_collection_run_source_completed
    ON collection_run(source, completed_at);
