-- Compound indexes for hot read paths surfaced by query profiling.
--
-- ``get_open_session_for_agent`` filters by ``(agent_id, ended_at IS NULL)``
-- and orders by ``created_at DESC``. The existing ``idx_sessions_agent_id``
-- doesn't help with the secondary filter or the sort, so SQLite has to
-- read every session for the agent and sort. With dozens of sessions per
-- agent that adds noticeable latency to the dashboard refresh.
CREATE INDEX IF NOT EXISTS idx_sessions_agent_open
    ON sessions(agent_id, ended_at, created_at DESC);

-- ``get_latest_session_for_agent`` needs (agent_id, created_at DESC).
-- Also speeds up ``list_sessions(agent_id)`` ordered by ``created_at``.
CREATE INDEX IF NOT EXISTS idx_sessions_agent_created
    ON sessions(agent_id, created_at DESC);

-- ``get_latest_event_for_session`` reads the newest event per session.
-- The existing ``idx_events_session_order`` orders ASC; a DESC index
-- lets SQLite jump to the tail without scanning.
CREATE INDEX IF NOT EXISTS idx_events_session_latest
    ON events(session_id, occurred_at DESC, storage_order DESC);

-- Refresh planner statistics so the new indexes are actually used.
-- ANALYZE is cheap on a database this size and idempotent.
ANALYZE;
