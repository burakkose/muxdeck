# ruff: noqa: I001,PT009,PT027

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import shutil
import sqlite3
import unittest

from copilot_commander.adapters import DEFAULT_DATABASE_FILE_NAME, SQLiteStore
from copilot_commander.config import AppConfig, PathsConfig
from copilot_commander.domain.enums import AgentStatus, TaskPriority, TaskStatus
from copilot_commander.domain.events import Event, LogChunk
from copilot_commander.domain.models import Agent, Session, Worktree
from copilot_commander.domain.task_models import Task
from copilot_commander.exceptions import PersistenceError


class SQLiteStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_dir = (
            Path(__file__).resolve().parent / "_runtime_sqlite_store" / self._testMethodName
        )
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._cleanup_runtime_dir)
        self.config = AppConfig(
            paths=PathsConfig(
                state_dir=self.runtime_dir / "state",
                workspace_root=self.runtime_dir / "worktrees",
                database_path=self.runtime_dir / "state" / DEFAULT_DATABASE_FILE_NAME,
                fallback_database_path=self.runtime_dir
                / "legacy-state"
                / DEFAULT_DATABASE_FILE_NAME,
            ),
            config_file=self.runtime_dir / "config.toml",
        )
        self.store = SQLiteStore.from_config(self.config)
        self.addCleanup(self.store.close)

    def _cleanup_runtime_dir(self) -> None:
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)

    def _make_agent(self, *, pane_id: str = "%1") -> Agent:
        started_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        return Agent(
            id="agent-123",
            name="planner",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_window_name="main",
            tmux_pane_id=pane_id,
            pane_tty="/dev/pts/1",
            cwd="/repo",
            repo_root="/repo",
            worktree_path="/repo/worktrees/task",
            branch="task/sqlite-store",
            task_title="SQLite store",
            task_summary="Persist commander state",
            copilot_session_id="copilot-123",
            pid=4321,
            status=AgentStatus.RUNNING,
            started_at=started_at,
            last_activity_at=started_at + timedelta(seconds=10),
            last_seen_at=started_at + timedelta(seconds=20),
            idle_seconds=2,
            needs_attention=False,
            token_input=5,
            token_output=7,
            token_total=12,
            estimated_cost_usd=Decimal("1.250000"),
        )

    def _make_worktree(self) -> Worktree:
        return Worktree(
            id="worktree-123",
            repo_root="/repo",
            path="/repo/worktrees/task",
            branch="task/sqlite-store",
            base_branch="main",
            is_main_worktree=False,
            is_dirty=True,
            ahead_count=2,
            behind_count=1,
            locked=True,
            assigned_agent_id="agent-123",
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
        )

    def _make_session(self) -> Session:
        return Session(
            id="session-123",
            agent_id="agent-123",
            copilot_session_id="copilot-123",
            task_title="SQLite store",
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            ended_at=datetime(2025, 1, 1, 12, 30, tzinfo=UTC),
            exit_reason="completed",
        )

    def _make_task(self) -> Task:
        return Task(
            id="task-123",
            title="SQLite store task",
            summary="Persist commander state",
            description="Create a first-class task record",
            repo_root="/repo",
            priority=TaskPriority.HIGH,
            status=TaskStatus.ASSIGNED,
            assigned_agent_id="agent-123",
            assigned_worktree_id="worktree-123",
            created_at=datetime(2025, 1, 1, 11, 55, tzinfo=UTC),
            notes="ready for launch",
        )

    def test_bootstraps_schema_migrations_and_pragmas(self) -> None:
        self.assertEqual(
            self.store.database_path,
            self.config.paths.database_path.resolve(),
        )
        self.assertEqual(
            self.store.applied_migrations(),
            ("0001_initial.sql", "0002_add_tasks.sql"),
        )
        self.assertEqual(self.store.journal_mode, "wal")
        self.assertTrue(self.store.foreign_keys_enabled)

        with sqlite3.connect(self.store.database_path) as connection:
            table_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            applied_migrations = connection.execute(
                "SELECT version FROM migrations ORDER BY version ASC"
            ).fetchall()
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()

        self.assertTrue(
            {
                "agents",
                "worktrees",
                "sessions",
                "events",
                "log_chunks",
                "tasks",
                "settings",
                "cache_entries",
            }.issubset(table_names)
        )
        self.assertEqual(
            applied_migrations,
            [("0001_initial.sql",), ("0002_add_tasks.sql",)],
        )
        self.assertEqual(journal_mode, ("wal",))

    def test_upsert_and_query_helpers_round_trip_psd_models(self) -> None:
        agent = self._make_agent()
        self.store.upsert_agent(agent)
        updated_agent = Agent(
            id=agent.id,
            name="planner-updated",
            tmux_session_name=agent.tmux_session_name,
            tmux_window_id=agent.tmux_window_id,
            tmux_window_name=agent.tmux_window_name,
            tmux_pane_id=agent.tmux_pane_id,
            pane_tty=agent.pane_tty,
            cwd=agent.cwd,
            repo_root=agent.repo_root,
            worktree_path=agent.worktree_path,
            branch=agent.branch,
            task_title=agent.task_title,
            task_summary=agent.task_summary,
            copilot_session_id=agent.copilot_session_id,
            pid=agent.pid,
            status=AgentStatus.IDLE,
            started_at=agent.started_at,
            last_activity_at=agent.last_activity_at,
            last_seen_at=agent.last_seen_at + timedelta(minutes=1),
            idle_seconds=45,
            needs_attention=True,
            attention_reason="awaiting review",
            token_input=agent.token_input,
            token_output=agent.token_output,
            token_total=agent.token_total,
            estimated_cost_usd=agent.estimated_cost_usd,
        )
        self.store.upsert_agent(updated_agent)

        self.store.upsert_worktree(self._make_worktree())
        self.store.upsert_session(self._make_session())
        task = self._make_task()
        updated_task = Task(
            id=task.id,
            title=task.title,
            summary=task.summary,
            description=task.description,
            repo_root=task.repo_root,
            priority=task.priority,
            status=TaskStatus.RUNNING,
            assigned_agent_id=task.assigned_agent_id,
            assigned_worktree_id=task.assigned_worktree_id,
            created_at=task.created_at,
            started_at=task.created_at + timedelta(minutes=10),
            notes="running in worktree",
        )
        self.store.upsert_task(task)
        self.store.upsert_task(updated_task)
        self.store.set_setting("ui.theme", {"mode": "dark"})
        self.store.set_cache_entry(
            "git-status",
            "repo",
            {"dirty": True},
            expires_at=datetime(2099, 1, 2, tzinfo=UTC),
        )

        self.assertEqual(self.store.list_agents(), (updated_agent,))
        self.assertEqual(self.store.get_agent(updated_agent.id), updated_agent)
        self.assertEqual(self.store.get_agent_by_pane_id(updated_agent.tmux_pane_id), updated_agent)
        self.assertEqual(
            self.store.get_agent_by_copilot_session_id("copilot-123"),
            updated_agent,
        )
        self.assertEqual(self.store.get_worktree("worktree-123"), self._make_worktree())
        self.assertEqual(
            self.store.get_worktree_by_path("/repo/worktrees/task"),
            self._make_worktree(),
        )
        self.assertEqual(self.store.list_worktrees_by_repo("/repo"), (self._make_worktree(),))
        self.assertEqual(self.store.list_tasks(), (updated_task,))
        self.assertEqual(self.store.list_tasks(status=TaskStatus.RUNNING), (updated_task,))
        self.assertEqual(self.store.list_tasks(assigned_agent_id="agent-123"), (updated_task,))
        self.assertEqual(
            self.store.list_tasks(assigned_worktree_id="worktree-123"),
            (updated_task,),
        )
        self.assertEqual(self.store.list_tasks(repo_root="/repo"), (updated_task,))
        self.assertEqual(self.store.get_task("task-123"), updated_task)
        self.assertEqual(self.store.get_session("session-123"), self._make_session())
        self.assertEqual(
            self.store.get_session_by_copilot_session_id("copilot-123"),
            self._make_session(),
        )
        self.assertEqual(self.store.list_sessions("agent-123"), (self._make_session(),))
        self.assertEqual(self.store.get_setting("ui.theme"), {"mode": "dark"})
        self.assertEqual(self.store.get_cache_entry("git-status", "repo"), {"dirty": True})
        self.assertTrue(self.store.delete_setting("ui.theme"))
        self.assertTrue(self.store.delete_cache_entry("git-status", "repo"))
        self.assertTrue(self.store.delete_task("task-123"))
        self.assertIsNone(self.store.get_setting("ui.theme"))
        self.assertIsNone(self.store.get_cache_entry("git-status", "repo"))
        self.assertIsNone(self.store.get_task("task-123"))

    def test_upserts_reconcile_agent_pane_and_worktree_path_uniqueness(self) -> None:
        first_agent = self._make_agent(pane_id="%9")
        second_agent = Agent(
            id="agent-456",
            name="planner-two",
            tmux_session_name=first_agent.tmux_session_name,
            tmux_window_id="@9",
            tmux_window_name=first_agent.tmux_window_name,
            tmux_pane_id=first_agent.tmux_pane_id,
            pane_tty=first_agent.pane_tty,
            cwd=first_agent.cwd,
            repo_root=first_agent.repo_root,
            worktree_path=first_agent.worktree_path,
            branch=first_agent.branch,
            task_title=first_agent.task_title,
            task_summary=first_agent.task_summary,
            copilot_session_id="copilot-456",
            pid=9876,
            status=AgentStatus.IDLE,
            started_at=first_agent.started_at,
            last_activity_at=first_agent.last_activity_at,
            last_seen_at=first_agent.last_seen_at + timedelta(minutes=2),
            idle_seconds=20,
            needs_attention=True,
            attention_reason="rediscovered",
            token_input=1,
            token_output=2,
            token_total=3,
            estimated_cost_usd=Decimal("0.250000"),
        )
        self.store.upsert_agent(first_agent)
        self.store.upsert_agent(second_agent)

        first_worktree = Worktree(
            id="worktree-123",
            repo_root="/repo",
            path="/repo/worktrees/task",
            branch="task/sqlite-store",
            base_branch="main",
            is_main_worktree=False,
            is_dirty=True,
            ahead_count=2,
            behind_count=1,
            locked=True,
            assigned_agent_id=second_agent.id,
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
        )
        second_worktree = Worktree(
            id="worktree-456",
            repo_root=first_worktree.repo_root,
            path=first_worktree.path,
            branch="task/sqlite-store-v2",
            base_branch=first_worktree.base_branch,
            is_main_worktree=first_worktree.is_main_worktree,
            is_dirty=False,
            ahead_count=3,
            behind_count=0,
            locked=False,
            assigned_agent_id=second_agent.id,
            created_at=first_worktree.created_at,
            last_seen_at=first_worktree.last_seen_at + timedelta(minutes=2),
        )
        self.store.upsert_worktree(first_worktree)
        self.store.upsert_worktree(second_worktree)

        self.assertEqual(self.store.list_agents(), (second_agent,))
        self.assertEqual(self.store.get_agent_by_pane_id(second_agent.tmux_pane_id), second_agent)
        self.assertEqual(self.store.list_worktrees_by_repo("/repo"), (second_worktree,))
        self.assertEqual(self.store.get_worktree_by_path(first_worktree.path), second_worktree)

    def test_append_events_is_atomic_and_preserves_occurrence_ties(self) -> None:
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_session(self._make_session())
        occurred_at = datetime(2025, 1, 1, 12, 5, tzinfo=UTC)
        first = Event(
            id="event-1",
            occurred_at=occurred_at,
            agent_id="agent-123",
            session_id="session-123",
            kind="session.updated",
            severity="info",
            payload_json='{"step":1}',
        )
        second = Event(
            id="event-2",
            occurred_at=occurred_at,
            agent_id="agent-123",
            session_id="session-123",
            kind="session.updated",
            severity="warning",
            payload_json='{"step":2}',
        )
        self.store.append_events((first, second))

        invalid = Event(
            id="event-3",
            occurred_at=occurred_at + timedelta(seconds=1),
            agent_id="agent-123",
            session_id="missing-session",
            kind="session.updated",
            severity="error",
            payload_json='{"step":3}',
        )
        with self.assertRaises(PersistenceError):
            self.store.append_events(
                (
                    Event(
                        id="event-4",
                        occurred_at=occurred_at + timedelta(seconds=2),
                        agent_id="agent-123",
                        session_id="session-123",
                        kind="session.updated",
                        severity="info",
                        payload_json='{"step":4}',
                    ),
                    invalid,
                )
            )

        self.assertEqual(self.store.list_events_for_session("session-123"), (first, second))
        self.assertEqual(self.store.list_events(agent_id="agent-123"), (first, second))

    def test_append_log_chunks_orders_results_and_supports_cache_expiry(self) -> None:
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_session(self._make_session())
        captured_at = datetime(2025, 1, 1, 12, 10, tzinfo=UTC)
        later_inserted = LogChunk(
            id="logchunk-2",
            agent_id="agent-123",
            session_id="session-123",
            source="stdout",
            sequence_no=4,
            captured_at=captured_at,
            content="second",
        )
        earlier_inserted = LogChunk(
            id="logchunk-1",
            agent_id="agent-123",
            session_id="session-123",
            source="stdout",
            sequence_no=3,
            captured_at=captured_at,
            content="first",
        )
        self.store.append_log_chunks((later_inserted, earlier_inserted))

        self.assertEqual(
            self.store.list_log_chunks("session-123"),
            (earlier_inserted, later_inserted),
        )
        self.assertEqual(self.store.get_log_chunk("logchunk-1"), earlier_inserted)
        self.assertEqual(
            self.store.list_log_chunks_for_agent("agent-123"),
            (earlier_inserted, later_inserted),
        )

        self.store.set_cache_entry(
            "parser",
            "last",
            {"ok": False},
            expires_at=datetime(2024, 12, 31, tzinfo=UTC),
        )
        self.assertIsNone(self.store.get_cache_entry("parser", "last"))
        self.assertEqual(
            self.store.purge_expired_cache(now=datetime(2025, 1, 1, tzinfo=UTC)),
            1,
        )

    def test_get_latest_session_for_agent(self) -> None:
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_session(self._make_session())
        result = self.store.get_latest_session_for_agent("agent-123")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.id, "session-123")
        self.assertIsNone(self.store.get_latest_session_for_agent("nonexistent"))

    def test_get_latest_event_for_session(self) -> None:
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_session(self._make_session())
        first = Event(
            id="evt-1",
            agent_id="agent-123",
            session_id="session-123",
            kind="tool_start",
            occurred_at=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
            severity="info",
        )
        second = Event(
            id="evt-2",
            agent_id="agent-123",
            session_id="session-123",
            kind="tool_end",
            occurred_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
            severity="info",
        )
        self.store.append_events((first, second))
        result = self.store.get_latest_event_for_session("session-123")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.id, "evt-2")
        self.assertIsNone(self.store.get_latest_event_for_session("nonexistent"))

    def test_get_latest_log_chunk(self) -> None:
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_session(self._make_session())
        chunk1 = LogChunk(
            id="log-1",
            agent_id="agent-123",
            session_id="session-123",
            source="stdout",
            sequence_no=1,
            captured_at=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
            content="first",
        )
        chunk2 = LogChunk(
            id="log-2",
            agent_id="agent-123",
            session_id="session-123",
            source="stdout",
            sequence_no=2,
            captured_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
            content="second",
        )
        self.store.append_log_chunks((chunk1, chunk2))
        result = self.store.get_latest_log_chunk("session-123")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.id, "log-2")
        self.assertIsNone(self.store.get_latest_log_chunk("nonexistent"))

    def test_foreign_keys_reject_orphaned_rows(self) -> None:
        with self.assertRaises(PersistenceError):
            self.store.upsert_session(self._make_session())
        with self.assertRaises(PersistenceError):
            self.store.upsert_task(
                Task(
                    id="task-404",
                    title="Missing assignment target",
                    status=TaskStatus.ASSIGNED,
                    assigned_agent_id="agent-missing",
                )
            )

    def test_count_sessions_for_agent(self) -> None:
        self.store.upsert_agent(self._make_agent())
        self.assertEqual(self.store.count_sessions_for_agent("agent-123"), 0)
        self.store.upsert_session(self._make_session())
        self.assertEqual(self.store.count_sessions_for_agent("agent-123"), 1)
        s2 = Session(
            id="session-456",
            agent_id="agent-123",
            created_at=datetime(2025, 1, 1, 13, 0, tzinfo=UTC),
        )
        self.store.upsert_session(s2)
        self.assertEqual(self.store.count_sessions_for_agent("agent-123"), 2)
        self.assertEqual(self.store.count_sessions_for_agent("nonexistent"), 0)

    def test_get_open_session_for_agent(self) -> None:
        self.store.upsert_agent(self._make_agent())
        self.assertIsNone(self.store.get_open_session_for_agent("agent-123"))
        # Insert an open session (no ended_at)
        open_session = Session(
            id="session-open",
            agent_id="agent-123",
            created_at=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
        )
        self.store.upsert_session(open_session)
        result = self.store.get_open_session_for_agent("agent-123")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.id, "session-open")
        # Close it
        closed = Session(
            id="session-open",
            agent_id="agent-123",
            created_at=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
            ended_at=datetime(2025, 1, 1, 12, 30, tzinfo=UTC),
        )
        self.store.upsert_session(closed)
        self.assertIsNone(self.store.get_open_session_for_agent("agent-123"))

    def test_list_recent_log_chunks(self) -> None:
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_session(self._make_session())
        chunks = []
        for i in range(25):
            chunks.append(
                LogChunk(
                    id=f"log-{i:03d}",
                    agent_id="agent-123",
                    session_id="session-123",
                    source="stdout",
                    sequence_no=i,
                    captured_at=datetime(2025, 1, 1, 12, i, tzinfo=UTC),
                    content=f"line-{i}",
                )
            )
        self.store.append_log_chunks(tuple(chunks))
        # Default limit=20 returns last 20 in chronological order
        recent = self.store.list_recent_log_chunks("session-123")
        self.assertEqual(len(recent), 20)
        self.assertEqual(recent[0].id, "log-005")
        self.assertEqual(recent[-1].id, "log-024")
        # Custom limit
        recent5 = self.store.list_recent_log_chunks("session-123", limit=5)
        self.assertEqual(len(recent5), 5)
        self.assertEqual(recent5[0].id, "log-020")
        self.assertEqual(recent5[-1].id, "log-024")
        # Empty session
        self.assertEqual(self.store.list_recent_log_chunks("nonexistent"), ())


if __name__ == "__main__":
    unittest.main()
