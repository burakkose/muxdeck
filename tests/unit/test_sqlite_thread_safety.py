# ruff: noqa: ANN001,ANN201,E501

"""Regression test: verify SQLiteStore works across threads with check_same_thread=False."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from muxdeck.adapters.sqlite_store import SQLiteStore
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.models import Agent


def test_cross_thread_write_and_read(tmp_path) -> None:
    """sync_store on a worker thread can write data that main store reads."""
    db_path = tmp_path / "test.db"

    # Main-thread store (default check_same_thread=True)
    main_store = SQLiteStore(database_path=db_path)

    # Worker-thread store with cross-thread access enabled
    worker_store = SQLiteStore(database_path=db_path, check_same_thread=False)

    agent = Agent(
        id="agent-cross-thread",
        name="test-agent",
        tmux_pane_id="%99",
        tmux_session_name="main",
        tmux_window_id="@0",
        cwd="/tmp/wt",
        status=AgentStatus.RUNNING,
        worktree_path="/tmp/wt",
        started_at=datetime(2025, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2025, 1, 1, tzinfo=UTC),
        idle_seconds=0,
        needs_attention=False,
    )

    errors: list[Exception] = []

    def worker_write() -> None:
        try:
            worker_store.upsert_agent(agent)
        except Exception as exc:
            errors.append(exc)

    # Write from a different thread
    thread = threading.Thread(target=worker_write)
    thread.start()
    thread.join(timeout=5.0)

    assert not errors, f"Worker thread raised: {errors}"

    # Read from the main thread
    agents = main_store.list_agents()
    assert len(agents) == 1
    assert agents[0].id == "agent-cross-thread"
    assert agents[0].name == "test-agent"

    main_store.close()
    worker_store.close()


def test_default_store_rejects_cross_thread_access(tmp_path) -> None:
    """Default SQLiteStore raises when accessed from a non-creating thread."""
    db_path = tmp_path / "test.db"
    store = SQLiteStore(database_path=db_path)

    agent = Agent(
        id="agent-reject",
        name="test",
        tmux_pane_id="%0",
        tmux_session_name="main",
        tmux_window_id="@0",
        cwd="/tmp",
        status=AgentStatus.RUNNING,
        worktree_path="/tmp",
        started_at=datetime(2025, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2025, 1, 1, tzinfo=UTC),
        idle_seconds=0,
        needs_attention=False,
    )

    errors: list[Exception] = []

    def worker_write() -> None:
        try:
            store.upsert_agent(agent)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker_write)
    thread.start()
    thread.join(timeout=5.0)

    # Should have raised PersistenceError (wrapping the underlying sqlite3 thread error)
    assert len(errors) == 1
    # The store wraps sqlite3.ProgrammingError in PersistenceError
    assert "failed" in str(errors[0]).lower() or "thread" in str(errors[0]).lower()

    store.close()
