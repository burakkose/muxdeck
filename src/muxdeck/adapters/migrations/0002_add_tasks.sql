CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT,
    description TEXT,
    repo_root TEXT,
    priority TEXT NOT NULL,
    status TEXT NOT NULL,
    assigned_agent_id TEXT REFERENCES agents(id) ON UPDATE CASCADE ON DELETE SET NULL,
    assigned_worktree_id TEXT REFERENCES worktrees(id) ON UPDATE CASCADE ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    notes TEXT
);

CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_repo_root ON tasks(repo_root);
CREATE INDEX idx_tasks_assigned_agent_id ON tasks(assigned_agent_id);
CREATE INDEX idx_tasks_assigned_worktree_id ON tasks(assigned_worktree_id);
CREATE INDEX idx_tasks_created_at ON tasks(created_at);
