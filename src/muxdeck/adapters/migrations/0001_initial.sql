CREATE TABLE agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    backend TEXT NOT NULL,
    tmux_session_name TEXT NOT NULL,
    tmux_window_id TEXT NOT NULL,
    tmux_window_name TEXT,
    tmux_pane_id TEXT NOT NULL,
    pane_tty TEXT,
    cwd TEXT NOT NULL,
    repo_root TEXT,
    worktree_path TEXT,
    branch TEXT,
    task_title TEXT,
    task_summary TEXT,
    copilot_session_id TEXT,
    pid INTEGER,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_activity_at TEXT,
    last_seen_at TEXT NOT NULL,
    idle_seconds INTEGER NOT NULL,
    needs_attention INTEGER NOT NULL,
    attention_reason TEXT,
    token_input INTEGER,
    token_output INTEGER,
    token_total INTEGER,
    estimated_cost_usd TEXT,
    CHECK (needs_attention IN (0, 1))
);

CREATE UNIQUE INDEX idx_agents_tmux_pane_id ON agents(tmux_pane_id);
CREATE INDEX idx_agents_copilot_session_id ON agents(copilot_session_id);

CREATE TABLE worktrees (
    id TEXT PRIMARY KEY,
    repo_root TEXT NOT NULL,
    path TEXT NOT NULL,
    branch TEXT NOT NULL,
    base_branch TEXT,
    is_main_worktree INTEGER NOT NULL,
    is_dirty INTEGER NOT NULL,
    ahead_count INTEGER,
    behind_count INTEGER,
    locked INTEGER NOT NULL,
    assigned_agent_id TEXT REFERENCES agents(id) ON UPDATE CASCADE ON DELETE SET NULL,
    created_at TEXT,
    last_seen_at TEXT NOT NULL,
    CHECK (is_main_worktree IN (0, 1)),
    CHECK (is_dirty IN (0, 1)),
    CHECK (locked IN (0, 1))
);

CREATE UNIQUE INDEX idx_worktrees_path ON worktrees(path);
CREATE INDEX idx_worktrees_repo_root ON worktrees(repo_root);
CREATE INDEX idx_worktrees_assigned_agent_id ON worktrees(assigned_agent_id);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    copilot_session_id TEXT,
    task_title TEXT,
    created_at TEXT NOT NULL,
    ended_at TEXT,
    exit_reason TEXT
);

CREATE INDEX idx_sessions_agent_id ON sessions(agent_id);
CREATE INDEX idx_sessions_copilot_session_id ON sessions(copilot_session_id);

CREATE TABLE events (
    storage_order INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    agent_id TEXT REFERENCES agents(id) ON UPDATE CASCADE ON DELETE SET NULL,
    session_id TEXT REFERENCES sessions(id) ON UPDATE CASCADE ON DELETE SET NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX idx_events_agent_id ON events(agent_id);
CREATE INDEX idx_events_session_id ON events(session_id);
CREATE INDEX idx_events_occurred_at ON events(occurred_at);
CREATE INDEX idx_events_session_order ON events(session_id, occurred_at, storage_order);

CREATE TABLE log_chunks (
    storage_order INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    session_id TEXT REFERENCES sessions(id) ON UPDATE CASCADE ON DELETE SET NULL,
    source TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    content TEXT NOT NULL
);

CREATE INDEX idx_log_chunks_agent_id ON log_chunks(agent_id);
CREATE INDEX idx_log_chunks_session_id ON log_chunks(session_id);
CREATE INDEX idx_log_chunks_session_sequence ON log_chunks(session_id, sequence_no, storage_order);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE cache_entries (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    PRIMARY KEY (namespace, key)
);

CREATE INDEX idx_cache_entries_expires_at ON cache_entries(expires_at);

CREATE TABLE session_context_cache (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON UPDATE CASCADE ON DELETE CASCADE,
    agent_id TEXT REFERENCES agents(id) ON UPDATE CASCADE ON DELETE SET NULL,
    worktree_id TEXT REFERENCES worktrees(id) ON UPDATE CASCADE ON DELETE SET NULL,
    tmux_pane_id TEXT,
    pane_tty TEXT,
    worktree_path TEXT,
    copilot_session_id TEXT,
    repo_root TEXT,
    branch TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_session_context_worktree_id ON session_context_cache(worktree_id, updated_at DESC);
CREATE INDEX idx_session_context_tmux_pane_id ON session_context_cache(tmux_pane_id, updated_at DESC);
CREATE INDEX idx_session_context_copilot_session_id ON session_context_cache(copilot_session_id, updated_at DESC);
