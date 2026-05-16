# ruff: noqa: I001,PT009,PT027

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import shutil
import sqlite3
import unittest

from muxdeck.adapters import DEFAULT_DATABASE_FILE_NAME, SQLiteStore
from muxdeck.adapters.sqlite_store import SessionContextRecord
from muxdeck.config import AppConfig, PathsConfig
from muxdeck.domain.enums import AgentStatus, TaskPriority, TaskStatus
from muxdeck.domain.events import Event, LogChunk
from muxdeck.domain.models import Agent, Session, Worktree
from muxdeck.domain.task_models import Task
from muxdeck.exceptions import PersistenceError
from muxdeck.perf import summarize


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
            task_summary="Persist muxdeck state",
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
            summary="Persist muxdeck state",
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
            (
                "0001_initial.sql",
                "0002_add_tasks.sql",
                "0003_add_replay_annotations.sql",
                "0004_perf_indexes.sql",
            ),
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
            [
                ("0001_initial.sql",),
                ("0002_add_tasks.sql",),
                ("0003_add_replay_annotations.sql",),
                ("0004_perf_indexes.sql",),
            ],
        )
        self.assertEqual(journal_mode, ("wal",))

    def test_bootstrap_recreates_perf_indexes_if_missing(self) -> None:
        """Regression: dropping a perf index (manual sqlite shell,
        partial backup restore, etc.) must not silently degrade
        dashboard query plans. Reopening the store should restore
        the indexes via the post-migration guard rather than wait
        for the next operator-initiated migration.
        """
        original_path = self.store.database_path
        with sqlite3.connect(original_path) as connection:
            connection.execute("DROP INDEX IF EXISTS idx_sessions_agent_open;")
            connection.execute("DROP INDEX IF EXISTS idx_sessions_agent_created;")
            connection.execute("DROP INDEX IF EXISTS idx_events_session_latest;")
            connection.commit()

        # Close + reopen the store via a fresh SQLiteStore instance
        # so the bootstrap path (which calls _ensure_perf_indexes)
        # runs against the now-pruned database.
        self.store.close()
        self.store = SQLiteStore.from_config(self.config)
        self.addCleanup(self.store.close)

        with sqlite3.connect(self.store.database_path) as connection:
            index_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }

        for required in (
            "idx_sessions_agent_open",
            "idx_sessions_agent_created",
            "idx_events_session_latest",
        ):
            assert required in index_names, f"{required} was not recreated on reopen"

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

    def test_upsert_log_capture_if_changed_inserts_first_chunk(self) -> None:
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_session(self._make_session())
        captured_at = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)

        chunk = self.store.upsert_log_capture_if_changed(
            agent_id="agent-123",
            session_id="session-123",
            source="tmux_capture",
            content="initial",
            captured_at=captured_at,
            chunk_id_factory=lambda: "log-initial",
        )

        assert chunk is not None
        self.assertEqual(chunk.id, "log-initial")
        self.assertEqual(chunk.sequence_no, 0)
        self.assertEqual(chunk.content, "initial")
        latest = self.store.get_latest_log_chunk("session-123")
        assert latest is not None
        self.assertEqual(latest.id, "log-initial")

    def test_upsert_log_capture_if_changed_skips_duplicate_tail(self) -> None:
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_session(self._make_session())
        captured_at = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        self.store.upsert_log_capture_if_changed(
            agent_id="agent-123",
            session_id="session-123",
            source="tmux_capture",
            content="initial",
            captured_at=captured_at,
            chunk_id_factory=lambda: "log-initial",
        )

        result = self.store.upsert_log_capture_if_changed(
            agent_id="agent-123",
            session_id="session-123",
            source="tmux_capture",
            content="initial",
            captured_at=captured_at + timedelta(seconds=2),
            chunk_id_factory=lambda: "log-should-not-exist",
        )

        self.assertIsNone(result)
        listed = self.store.list_log_chunks("session-123")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].id, "log-initial")

    def test_upsert_log_capture_if_changed_appends_when_tail_differs(self) -> None:
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_session(self._make_session())
        captured_at = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        self.store.upsert_log_capture_if_changed(
            agent_id="agent-123",
            session_id="session-123",
            source="tmux_capture",
            content="initial",
            captured_at=captured_at,
            chunk_id_factory=lambda: "log-initial",
        )

        chunk = self.store.upsert_log_capture_if_changed(
            agent_id="agent-123",
            session_id="session-123",
            source="tmux_capture",
            content="initial\nstep two",
            captured_at=captured_at + timedelta(seconds=3),
            chunk_id_factory=lambda: "log-second",
        )

        assert chunk is not None
        self.assertEqual(chunk.sequence_no, 1)
        listed = self.store.list_log_chunks("session-123")
        self.assertEqual([c.id for c in listed], ["log-initial", "log-second"])

    def test_get_dashboard_agent_snapshots_returns_latest_per_agent(self) -> None:
        agent_a = self._make_agent(pane_id="%1")
        agent_b = Agent(
            id="agent-b",
            name="builder",
            tmux_session_name="muxdeck",
            tmux_window_id="@2",
            tmux_window_name="build",
            tmux_pane_id="%2",
            pane_tty="/dev/pts/2",
            cwd="/repo",
            status=AgentStatus.RUNNING,
            started_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        self.store.upsert_agent(agent_a)
        self.store.upsert_agent(agent_b)

        # Agent A: two sessions; newest should win.
        session_a_old = Session(
            id="session-a-old",
            agent_id="agent-123",
            created_at=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
            ended_at=datetime(2025, 1, 1, 12, 5, tzinfo=UTC),
        )
        session_a_new = Session(
            id="session-a-new",
            agent_id="agent-123",
            created_at=datetime(2025, 1, 1, 12, 10, tzinfo=UTC),
        )
        # Agent B: one session, no events / logs.
        session_b = Session(
            id="session-b",
            agent_id="agent-b",
            created_at=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
        )
        self.store.upsert_session(session_a_old)
        self.store.upsert_session(session_a_new)
        self.store.upsert_session(session_b)

        # Events: only on the NEW session for A. Two ordered events so
        # we know the snapshot picks the most recent.
        evt_old = Event(
            id="evt-a-old",
            agent_id="agent-123",
            session_id="session-a-new",
            kind="tool_start",
            occurred_at=datetime(2025, 1, 1, 12, 10, 5, tzinfo=UTC),
            severity="info",
        )
        evt_new = Event(
            id="evt-a-new",
            agent_id="agent-123",
            session_id="session-a-new",
            kind="tool_end",
            occurred_at=datetime(2025, 1, 1, 12, 10, 10, tzinfo=UTC),
            severity="info",
        )
        # A stray event on the OLD session must NOT be picked.
        evt_stray = Event(
            id="evt-a-stray",
            agent_id="agent-123",
            session_id="session-a-old",
            kind="tool_start",
            occurred_at=datetime(2025, 1, 1, 12, 4, tzinfo=UTC),
            severity="info",
        )
        self.store.append_events((evt_old, evt_new, evt_stray))

        # Log chunks: only on the NEW session for A; two of them.
        log_first = LogChunk(
            id="log-a-1",
            agent_id="agent-123",
            session_id="session-a-new",
            source="stdout",
            sequence_no=0,
            captured_at=datetime(2025, 1, 1, 12, 10, 6, tzinfo=UTC),
            content="hello",
        )
        log_second = LogChunk(
            id="log-a-2",
            agent_id="agent-123",
            session_id="session-a-new",
            source="stdout",
            sequence_no=1,
            captured_at=datetime(2025, 1, 1, 12, 10, 11, tzinfo=UTC),
            content="world",
        )
        self.store.append_log_chunks((log_first, log_second))

        # Agent with no sessions: must not appear in the dict.
        snapshots = self.store.get_dashboard_agent_snapshots(
            ["agent-123", "agent-b", "agent-empty"]
        )

        self.assertEqual(set(snapshots.keys()), {"agent-123", "agent-b"})

        snap_a = snapshots["agent-123"]
        assert snap_a.session is not None
        self.assertEqual(snap_a.session.id, "session-a-new")
        assert snap_a.latest_event is not None
        self.assertEqual(snap_a.latest_event.id, "evt-a-new")
        assert snap_a.latest_log is not None
        self.assertEqual(snap_a.latest_log.id, "log-a-2")

        snap_b = snapshots["agent-b"]
        assert snap_b.session is not None
        self.assertEqual(snap_b.session.id, "session-b")
        self.assertIsNone(snap_b.latest_event)
        self.assertIsNone(snap_b.latest_log)

    def test_get_dashboard_agent_snapshots_empty_input(self) -> None:
        # No agents → no SQL fired, no rows.
        self.assertEqual(self.store.get_dashboard_agent_snapshots([]), {})
        # Unknown agents only → empty dict, no exception.
        self.store.upsert_agent(self._make_agent())
        self.assertEqual(self.store.get_dashboard_agent_snapshots(["unknown-1", "unknown-2"]), {})

    def test_get_dashboard_agent_snapshots_dedupes_input(self) -> None:
        # Repeated agent_ids must not duplicate the result and must
        # not blow up the SQL.
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_session(self._make_session())
        snapshots = self.store.get_dashboard_agent_snapshots(
            ["agent-123", "agent-123", "agent-123"]
        )
        self.assertEqual(set(snapshots.keys()), {"agent-123"})
        snap = snapshots["agent-123"]
        assert snap.session is not None
        self.assertEqual(snap.session.id, "session-123")

    def test_get_agent_action_target_returns_full_bundle(self) -> None:
        """Bundle must include the latest session, the currently-open
        session, the session_context for the latest session, and the
        worktree referenced by that context — all in one call.
        """
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_worktree(self._make_worktree())
        latest = Session(
            id="session-latest",
            agent_id="agent-123",
            created_at=datetime(2025, 1, 1, 13, tzinfo=UTC),
        )
        # An older session, already ended, so the open lookup must
        # find LATEST (open) not this one.
        ended = Session(
            id="session-ended",
            agent_id="agent-123",
            created_at=datetime(2025, 1, 1, 11, tzinfo=UTC),
            ended_at=datetime(2025, 1, 1, 11, 30, tzinfo=UTC),
        )
        self.store.upsert_session(ended)
        self.store.upsert_session(latest)
        self.store.upsert_session_context(
            SessionContextRecord(
                session_id="session-latest",
                agent_id="agent-123",
                worktree_id="worktree-123",
                tmux_pane_id="%1",
                worktree_path="/repo/worktrees/task",
                repo_root="/repo",
                branch="task/sqlite-store",
                updated_at=datetime(2025, 1, 1, 13, tzinfo=UTC),
            )
        )

        target = self.store.get_agent_action_target("agent-123")

        assert target.latest_session is not None
        self.assertEqual(target.latest_session.id, "session-latest")
        assert target.open_session is not None
        self.assertEqual(target.open_session.id, "session-latest")
        assert target.context is not None
        self.assertEqual(target.context.worktree_id, "worktree-123")
        assert target.worktree is not None
        self.assertEqual(target.worktree.path, "/repo/worktrees/task")

    def test_get_agent_action_target_returns_empty_for_unknown_agent(self) -> None:
        target = self.store.get_agent_action_target("does-not-exist")
        self.assertIsNone(target.latest_session)
        self.assertIsNone(target.open_session)
        self.assertIsNone(target.context)
        self.assertIsNone(target.worktree)

    def test_get_agent_action_target_no_context_or_worktree(self) -> None:
        # Agent has a session but no session_context cached → no
        # worktree resolution attempted.
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_session(self._make_session())
        target = self.store.get_agent_action_target("agent-123")
        assert target.latest_session is not None
        self.assertEqual(target.latest_session.id, "session-123")
        # session-123 is ended by self._make_session()
        self.assertIsNone(target.open_session)
        self.assertIsNone(target.context)
        self.assertIsNone(target.worktree)

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

    def test_list_recent_log_chunks_with_cursor(self) -> None:
        """Composite cursor: ``before_sequence_no`` + ``before_storage_order``
        return the previous page (strictly older than the cursor) so the UI
        can implement reverse-paged "load older" without duplicates.
        """
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_session(self._make_session())
        chunks = tuple(
            LogChunk(
                id=f"log-{i:03d}",
                agent_id="agent-123",
                session_id="session-123",
                source="stdout",
                sequence_no=i,
                captured_at=datetime(2025, 1, 1, 12, i, tzinfo=UTC),
                content=f"line-{i}",
            )
            for i in range(10)
        )
        self.store.append_log_chunks(chunks)

        # Get latest 3 → log-007..log-009 (chronological)
        tail = self.store.list_recent_log_chunks("session-123", limit=3)
        self.assertEqual([c.id for c in tail], ["log-007", "log-008", "log-009"])

        # Page older than the oldest of the tail (log-007) using the cursor.
        # Storage order matches insertion order (1-indexed), so log-007 has
        # storage_order=8 in this DB.
        oldest_seq = tail[0].sequence_no  # 7
        # Find the storage_order of that row via the unbounded listing to
        # avoid hard-coding implementation details.
        all_rows = self.store.list_log_chunks("session-123")
        oldest_full = next(c for c in all_rows if c.sequence_no == oldest_seq)
        # storage_order is not exposed on LogChunk; use sequence_no+1 as the
        # cursor companion since storage_order is monotonic with sequence_no
        # in this test scenario. We just need ANY value paired with
        # before_sequence_no to satisfy the composite-cursor contract.
        prev_page = self.store.list_recent_log_chunks(
            "session-123",
            limit=3,
            before_sequence_no=oldest_seq,
            before_storage_order=oldest_full.sequence_no + 1,
        )
        self.assertEqual([c.id for c in prev_page], ["log-004", "log-005", "log-006"])

    def test_list_recent_log_chunks_validates_args(self) -> None:
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_session(self._make_session())
        # limit=0 is a no-op
        self.assertEqual(
            self.store.list_recent_log_chunks("session-123", limit=0),
            (),
        )
        # negative limit is rejected
        with self.assertRaises(ValueError):
            self.store.list_recent_log_chunks("session-123", limit=-1)
        # partial cursor is rejected
        with self.assertRaises(ValueError):
            self.store.list_recent_log_chunks("session-123", limit=10, before_sequence_no=5)

    def test_list_recent_events_for_session(self) -> None:
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_session(self._make_session())
        base = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        events = tuple(
            Event(
                id=f"event-{i:03d}",
                occurred_at=base + timedelta(seconds=i),
                agent_id="agent-123",
                session_id="session-123",
                kind="session.updated",
                severity="info",
                payload_json=f'{{"step":{i}}}',
            )
            for i in range(25)
        )
        self.store.append_events(events)

        # Latest 5 in chronological order
        recent = self.store.list_recent_events_for_session("session-123", limit=5)
        self.assertEqual([e.id for e in recent], [f"event-{i:03d}" for i in range(20, 25)])

        # All events when limit exceeds total
        recent_all = self.store.list_recent_events_for_session("session-123", limit=100)
        self.assertEqual(len(recent_all), 25)
        self.assertEqual(recent_all[0].id, "event-000")
        self.assertEqual(recent_all[-1].id, "event-024")

        # Empty session
        self.assertEqual(
            self.store.list_recent_events_for_session("nonexistent", limit=5),
            (),
        )

    def test_list_recent_events_for_session_with_cursor(self) -> None:
        """Composite ``(before_occurred_at, before_storage_order)`` cursor
        returns the previous page strictly older than the marker."""
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_session(self._make_session())
        base = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        events = tuple(
            Event(
                id=f"event-{i:03d}",
                occurred_at=base + timedelta(seconds=i),
                agent_id="agent-123",
                session_id="session-123",
                kind="session.updated",
                severity="info",
                payload_json=f'{{"step":{i}}}',
            )
            for i in range(10)
        )
        self.store.append_events(events)

        # Get latest 3 events
        tail = self.store.list_recent_events_for_session("session-123", limit=3)
        self.assertEqual([e.id for e in tail], ["event-007", "event-008", "event-009"])

        # Page back from the oldest of the tail using cursor.
        # storage_order is internal; passing a low value (0) ensures the
        # composite condition (occurred_at == cursor.ts AND storage_order < 0)
        # matches nothing, so the cursor effectively means "events strictly
        # older than cursor.occurred_at" — excluding event-007 itself.
        cursor_event = tail[0]
        prev_page = self.store.list_recent_events_for_session(
            "session-123",
            limit=3,
            before_occurred_at=cursor_event.occurred_at,
            before_storage_order=0,
        )
        # Should return event-004..event-006 (strictly older by occurred_at)
        self.assertEqual([e.id for e in prev_page], ["event-004", "event-005", "event-006"])

    def test_list_recent_events_for_session_validates_args(self) -> None:
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_session(self._make_session())
        # limit=0 is a no-op
        self.assertEqual(
            self.store.list_recent_events_for_session("session-123", limit=0),
            (),
        )
        # negative limit is rejected
        with self.assertRaises(ValueError):
            self.store.list_recent_events_for_session("session-123", limit=-1)
        # partial cursor is rejected
        with self.assertRaises(ValueError):
            self.store.list_recent_events_for_session(
                "session-123",
                limit=5,
                before_occurred_at=datetime(2025, 1, 1, tzinfo=UTC),
            )


if __name__ == "__main__":
    unittest.main()


# ── extra tests targeting validators, replay annotations, edge branches ──


def _row(**columns: object) -> sqlite3.Row:
    """Build a one-row ``sqlite3.Row`` whose columns are the given kwargs."""
    cols = list(columns.keys())
    cn = sqlite3.connect(":memory:")
    try:
        cn.row_factory = sqlite3.Row
        select = ", ".join(f"? AS {c}" for c in cols)
        result: sqlite3.Row = cn.execute(f"SELECT {select}", tuple(columns.values())).fetchone()
        return result
    finally:
        cn.close()


class RowValidatorsTests(unittest.TestCase):
    def test_row_value_missing_column_raises(self) -> None:
        from muxdeck.adapters.sqlite_store import _row_value

        row = _row(id="a")
        with self.assertRaises(PersistenceError):
            _row_value(row, "missing")

    def test_require_text_rejects_non_text(self) -> None:
        from muxdeck.adapters.sqlite_store import _require_text

        row = _row(name=123)
        with self.assertRaises(PersistenceError):
            _require_text(row, "name")

    def test_require_text_rejects_blank(self) -> None:
        from muxdeck.adapters.sqlite_store import _require_text

        row = _row(name="   ")
        with self.assertRaises(PersistenceError):
            _require_text(row, "name")

    def test_require_text_returns_value(self) -> None:
        from muxdeck.adapters.sqlite_store import _require_text

        row = _row(name="hi")
        self.assertEqual(_require_text(row, "name"), "hi")

    def test_require_text_allow_empty_returns_blank(self) -> None:
        from muxdeck.adapters.sqlite_store import _require_text_allow_empty

        row = _row(body="")
        self.assertEqual(_require_text_allow_empty(row, "body"), "")

    def test_require_text_allow_empty_rejects_non_text(self) -> None:
        from muxdeck.adapters.sqlite_store import _require_text_allow_empty

        row = _row(body=42)
        with self.assertRaises(PersistenceError):
            _require_text_allow_empty(row, "body")

    def test_optional_text_returns_none(self) -> None:
        from muxdeck.adapters.sqlite_store import _optional_text

        row = _row(name=None)
        self.assertIsNone(_optional_text(row, "name"))

    def test_optional_text_rejects_non_text(self) -> None:
        from muxdeck.adapters.sqlite_store import _optional_text

        row = _row(name=42)
        with self.assertRaises(PersistenceError):
            _optional_text(row, "name")

    def test_optional_text_rejects_blank(self) -> None:
        from muxdeck.adapters.sqlite_store import _optional_text

        row = _row(name="   ")
        with self.assertRaises(PersistenceError):
            _optional_text(row, "name")

    def test_require_int_rejects_text(self) -> None:
        from muxdeck.adapters.sqlite_store import _require_int

        row = _row(n="x")
        with self.assertRaises(PersistenceError):
            _require_int(row, "n")

    def test_require_int_returns_int(self) -> None:
        from muxdeck.adapters.sqlite_store import _require_int

        row = _row(n=7)
        self.assertEqual(_require_int(row, "n"), 7)

    def test_optional_int_returns_none(self) -> None:
        from muxdeck.adapters.sqlite_store import _optional_int

        row = _row(n=None)
        self.assertIsNone(_optional_int(row, "n"))

    def test_optional_int_rejects_text(self) -> None:
        from muxdeck.adapters.sqlite_store import _optional_int

        row = _row(n="boom")
        with self.assertRaises(PersistenceError):
            _optional_int(row, "n")

    def test_require_bool_accepts_zero_and_one(self) -> None:
        from muxdeck.adapters.sqlite_store import _require_bool

        self.assertFalse(_require_bool(_row(b=0), "b"))
        self.assertTrue(_require_bool(_row(b=1), "b"))

    def test_require_bool_rejects_other_values(self) -> None:
        from muxdeck.adapters.sqlite_store import _require_bool

        with self.assertRaises(PersistenceError):
            _require_bool(_row(b=2), "b")
        with self.assertRaises(PersistenceError):
            _require_bool(_row(b="true"), "b")

    def test_require_datetime_rejects_non_text(self) -> None:
        from muxdeck.adapters.sqlite_store import _require_datetime

        with self.assertRaises(PersistenceError):
            _require_datetime(_row(t=42), "t")

    def test_require_datetime_rejects_invalid_iso(self) -> None:
        from muxdeck.adapters.sqlite_store import _require_datetime

        with self.assertRaises(PersistenceError):
            _require_datetime(_row(t="not-a-date"), "t")

    def test_require_datetime_rejects_naive_iso(self) -> None:
        from muxdeck.adapters.sqlite_store import _require_datetime

        with self.assertRaises(PersistenceError):
            _require_datetime(_row(t="2025-01-01T00:00:00"), "t")

    def test_require_datetime_returns_utc(self) -> None:
        from muxdeck.adapters.sqlite_store import _require_datetime

        parsed = _require_datetime(_row(t="2025-01-01T00:00:00+00:00"), "t")
        self.assertEqual(parsed.tzinfo, UTC)

    def test_optional_datetime_returns_none(self) -> None:
        from muxdeck.adapters.sqlite_store import _optional_datetime

        self.assertIsNone(_optional_datetime(_row(t=None), "t"))

    def test_optional_datetime_rejects_non_text(self) -> None:
        from muxdeck.adapters.sqlite_store import _optional_datetime

        with self.assertRaises(PersistenceError):
            _optional_datetime(_row(t=42), "t")

    def test_optional_datetime_rejects_invalid_iso(self) -> None:
        from muxdeck.adapters.sqlite_store import _optional_datetime

        with self.assertRaises(PersistenceError):
            _optional_datetime(_row(t="not-a-date"), "t")

    def test_optional_datetime_rejects_naive_iso(self) -> None:
        from muxdeck.adapters.sqlite_store import _optional_datetime

        with self.assertRaises(PersistenceError):
            _optional_datetime(_row(t="2025-01-01T00:00:00"), "t")

    def test_optional_datetime_returns_utc(self) -> None:
        from muxdeck.adapters.sqlite_store import _optional_datetime

        parsed = _optional_datetime(_row(t="2025-01-01T00:00:00+00:00"), "t")
        assert parsed is not None
        self.assertEqual(parsed.tzinfo, UTC)

    def test_optional_decimal_returns_none(self) -> None:
        from muxdeck.adapters.sqlite_store import _optional_decimal

        self.assertIsNone(_optional_decimal(_row(d=None), "d"))

    def test_optional_decimal_rejects_unsupported_type(self) -> None:
        from muxdeck.adapters.sqlite_store import _optional_decimal

        with self.assertRaises(PersistenceError):
            _optional_decimal(_row(d=b"bytes"), "d")

    def test_optional_decimal_rejects_invalid_decimal(self) -> None:
        from muxdeck.adapters.sqlite_store import _optional_decimal

        with self.assertRaises(PersistenceError):
            _optional_decimal(_row(d="not-a-number"), "d")

    def test_optional_decimal_handles_string_int_float(self) -> None:
        from muxdeck.adapters.sqlite_store import _optional_decimal

        self.assertEqual(_optional_decimal(_row(d="1.5"), "d"), Decimal("1.5"))
        self.assertEqual(_optional_decimal(_row(d=2), "d"), Decimal("2"))


class RowToModelErrorTests(unittest.TestCase):
    """Direct tests for `_row_to_*` PersistenceError branches."""

    def test_row_to_agent_invalid_status_raises(self) -> None:
        from muxdeck.adapters.sqlite_store import _row_to_agent

        row = _row(
            id="a-1",
            name="x",
            backend="copilot-cli",
            tmux_session_name="s",
            tmux_window_id="@1",
            tmux_window_name=None,
            tmux_pane_id="%1",
            pane_tty=None,
            cwd="/repo",
            repo_root=None,
            worktree_path=None,
            branch=None,
            task_title=None,
            task_summary=None,
            copilot_session_id=None,
            pid=None,
            status="not-a-real-status",
            started_at="2025-01-01T00:00:00+00:00",
            last_activity_at=None,
            last_seen_at="2025-01-01T00:00:00+00:00",
            idle_seconds=0,
            needs_attention=0,
            attention_reason=None,
            token_input=None,
            token_output=None,
            token_total=None,
            estimated_cost_usd=None,
        )
        with self.assertRaises(PersistenceError):
            _row_to_agent(row)

    def test_row_to_agent_domain_validation_raises(self) -> None:
        from muxdeck.adapters.sqlite_store import _row_to_agent

        # idle_seconds is required to be non-negative — pass -1 to trigger
        # DomainValidationError handling.
        row = _row(
            id="a-2",
            name="x",
            backend="copilot-cli",
            tmux_session_name="s",
            tmux_window_id="@1",
            tmux_window_name=None,
            tmux_pane_id="%1",
            pane_tty=None,
            cwd="/repo",
            repo_root=None,
            worktree_path=None,
            branch=None,
            task_title=None,
            task_summary=None,
            copilot_session_id=None,
            pid=None,
            status="running",
            started_at="2025-01-01T00:00:00+00:00",
            last_activity_at=None,
            last_seen_at="2025-01-01T00:00:00+00:00",
            idle_seconds=-1,
            needs_attention=0,
            attention_reason=None,
            token_input=None,
            token_output=None,
            token_total=None,
            estimated_cost_usd=None,
        )
        with self.assertRaises(PersistenceError):
            _row_to_agent(row)

    def test_row_to_worktree_domain_validation_raises(self) -> None:
        from muxdeck.adapters.sqlite_store import _row_to_worktree

        # ahead_count is required to be >= 0 → triggers Domain error.
        row = _row(
            id="w-1",
            repo_root="/repo",
            path="/repo/x",
            branch="b",
            base_branch=None,
            is_main_worktree=0,
            is_dirty=0,
            ahead_count=-1,
            behind_count=None,
            locked=0,
            assigned_agent_id=None,
            created_at=None,
            last_seen_at="2025-01-01T00:00:00+00:00",
        )
        with self.assertRaises(PersistenceError):
            _row_to_worktree(row)

    def test_row_to_replay_annotation_invalid_kind_raises(self) -> None:
        from muxdeck.adapters.sqlite_store import _row_to_replay_annotation

        row = _row(
            id="r-1",
            session_id="s-1",
            ordinal=1,
            created_at="2025-01-01T00:00:00+00:00",
            kind="bogus",
            body="x",
        )
        with self.assertRaises(PersistenceError):
            _row_to_replay_annotation(row)

    def test_row_to_replay_annotation_value_error_raises(self) -> None:
        from muxdeck.adapters.sqlite_store import _row_to_replay_annotation

        # ordinal is required to be int; -1 triggers ValueError via domain.
        row = _row(
            id="r-2",
            session_id="s-1",
            ordinal=-1,
            created_at="2025-01-01T00:00:00+00:00",
            kind="bookmark",
            body="",
        )
        with self.assertRaises(PersistenceError):
            _row_to_replay_annotation(row)

    def test_row_to_task_invalid_priority_raises(self) -> None:
        from muxdeck.adapters.sqlite_store import _row_to_task

        row = _row(
            id="t-1",
            title="hi",
            summary=None,
            description=None,
            repo_root=None,
            priority="not-a-real-priority",
            status="assigned",
            assigned_agent_id=None,
            assigned_worktree_id=None,
            created_at="2025-01-01T00:00:00+00:00",
            started_at=None,
            completed_at=None,
            notes=None,
        )
        with self.assertRaises(PersistenceError):
            _row_to_task(row)

    def test_row_to_session_domain_validation_raises(self) -> None:
        from muxdeck.adapters.sqlite_store import _row_to_session

        # Missing/invalid agent_id triggers domain validation error.
        row = _row(
            id="s-1",
            agent_id="   ",
            copilot_session_id=None,
            task_title=None,
            created_at="2025-01-01T00:00:00+00:00",
            ended_at=None,
            exit_reason=None,
        )
        with self.assertRaises(PersistenceError):
            _row_to_session(row)

    def test_row_to_event_domain_validation_raises(self) -> None:
        from muxdeck.adapters.sqlite_store import _row_to_event

        # Empty kind → domain validation failure.
        row = _row(
            id="e-1",
            occurred_at="2025-01-01T00:00:00+00:00",
            agent_id=None,
            session_id=None,
            kind="   ",
            severity="info",
            payload_json="{}",
        )
        with self.assertRaises(PersistenceError):
            _row_to_event(row)

    def test_row_to_log_chunk_domain_validation_raises(self) -> None:
        from muxdeck.adapters.sqlite_store import _row_to_log_chunk

        # sequence_no is required >= 0 → DomainValidationError → PersistenceError.
        row = _row(
            id="l-1",
            agent_id="a-1",
            session_id="s-1",
            source="stdout",
            sequence_no=-1,
            captured_at="2025-01-01T00:00:00+00:00",
            content="hi",
        )
        with self.assertRaises(PersistenceError):
            _row_to_log_chunk(row)

    def test_row_to_session_context_domain_validation_raises(self) -> None:
        from muxdeck.adapters.sqlite_store import _row_to_session_context

        # session_id blank → DomainValidationError → PersistenceError.
        # Use _require_text rejection path: blank session_id raises before
        # SessionContextRecord is even constructed.
        row = _row(
            session_id="   ",
            agent_id=None,
            worktree_id=None,
            tmux_pane_id=None,
            pane_tty=None,
            worktree_path=None,
            copilot_session_id=None,
            repo_root=None,
            branch=None,
            updated_at="2025-01-01T00:00:00+00:00",
        )
        with self.assertRaises(PersistenceError):
            _row_to_session_context(row)


class JsonAndSerializeHelperTests(unittest.TestCase):
    def test_serialize_json_raises_on_non_serializable(self) -> None:
        from muxdeck.adapters.sqlite_store import _serialize_json

        with self.assertRaises(PersistenceError):
            _serialize_json({"x": object()}, key_name="k")  # type: ignore[dict-item]

    def test_deserialize_json_raises_on_invalid_json(self) -> None:
        from muxdeck.adapters.sqlite_store import _deserialize_json

        with self.assertRaises(PersistenceError):
            _deserialize_json("{not-json", context="test")


class StoreSettingsAndCacheBranchTests(SQLiteStoreTests):
    def test_set_and_get_setting_round_trip(self) -> None:
        self.store.set_setting("greeting", "hi")
        self.assertEqual(self.store.get_setting("greeting"), "hi")

    def test_get_setting_returns_none_for_missing(self) -> None:
        self.assertIsNone(self.store.get_setting("missing"))

    def test_set_setting_rejects_non_serializable(self) -> None:
        with self.assertRaises(PersistenceError):
            # object() isn't JSON serializable.
            self.store.set_setting("k", object())  # type: ignore[arg-type]

    def test_delete_setting_returns_false_when_missing(self) -> None:
        self.assertFalse(self.store.delete_setting("nothing"))

    def test_delete_setting_returns_true_after_set(self) -> None:
        self.store.set_setting("k", 1)
        self.assertTrue(self.store.delete_setting("k"))

    def test_cache_entry_round_trip_and_expiry(self) -> None:
        future = datetime.now(UTC) + timedelta(hours=1)
        self.store.set_cache_entry("ns", "k", {"v": 1}, expires_at=future)
        self.assertEqual(self.store.get_cache_entry("ns", "k"), {"v": 1})

    def test_get_cache_entry_returns_none_when_expired(self) -> None:
        past = datetime.now(UTC) - timedelta(hours=1)
        self.store.set_cache_entry("ns", "expired", "v", expires_at=past)
        self.assertIsNone(self.store.get_cache_entry("ns", "expired"))

    def test_get_cache_entry_returns_none_when_missing(self) -> None:
        self.assertIsNone(self.store.get_cache_entry("ns", "nope"))

    def test_delete_cache_entry_returns_false_when_missing(self) -> None:
        self.assertFalse(self.store.delete_cache_entry("ns", "nope"))

    def test_purge_expired_cache_removes_expired_only(self) -> None:
        future = datetime.now(UTC) + timedelta(hours=1)
        past = datetime.now(UTC) - timedelta(hours=1)
        self.store.set_cache_entry("ns", "fresh", 1, expires_at=future)
        self.store.set_cache_entry("ns", "stale", 1, expires_at=past)
        removed = self.store.purge_expired_cache()
        self.assertGreaterEqual(removed, 1)
        self.assertEqual(self.store.get_cache_entry("ns", "fresh"), 1)
        self.assertIsNone(self.store.get_cache_entry("ns", "stale"))


class StoreEventsValidationTests(SQLiteStoreTests):
    def test_list_events_rejects_both_filters(self) -> None:
        with self.assertRaises(PersistenceError):
            self.store.list_events(agent_id="a", session_id="s")

    def test_list_events_session_filter_calls_per_session_method(self) -> None:
        # No data exists → expect empty tuple, not raise.
        self.assertEqual(self.store.list_events(session_id="missing"), ())

    def test_list_events_agent_filter_calls_per_agent_method(self) -> None:
        self.assertEqual(self.store.list_events(agent_id="missing"), ())

    def test_append_events_with_empty_sequence_is_noop(self) -> None:
        # Should not raise; nothing changes.
        self.store.append_events([])

    def test_append_log_chunks_with_empty_sequence_is_noop(self) -> None:
        self.store.append_log_chunks([])


class ReplayAnnotationStoreTests(SQLiteStoreTests):
    def _make_session_for_annotations(self) -> Session:
        # ReplayAnnotation rows reference sessions(id) → seed an agent + session.
        agent = self._make_agent(pane_id="%9")
        self.store.upsert_agent(agent)
        session = self._make_session()
        self.store.upsert_session(session)
        return session

    def test_insert_list_and_find_bookmark(self) -> None:
        from muxdeck.domain.replay_annotations import ReplayAnnotation

        session = self._make_session_for_annotations()
        bookmark = ReplayAnnotation(
            session_id=session.id,
            ordinal=3,
            kind="bookmark",
            body="",
        )
        note = ReplayAnnotation(
            session_id=session.id,
            ordinal=4,
            kind="note",
            body="follow up",
        )
        self.store.insert_replay_annotation(bookmark)
        self.store.insert_replay_annotation(note)
        listed = self.store.list_replay_annotations(session.id)
        self.assertEqual(len(listed), 2)
        # Ordered ascending by ordinal.
        self.assertEqual(listed[0].ordinal, 3)
        # find_replay_bookmark returns the bookmark only.
        found = self.store.find_replay_bookmark(session.id, 3)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.kind, "bookmark")
        # Missing ordinal → None.
        self.assertIsNone(self.store.find_replay_bookmark(session.id, 999))

    def test_update_replay_annotation_body_returns_true_when_present(self) -> None:
        from muxdeck.domain.replay_annotations import ReplayAnnotation

        session = self._make_session_for_annotations()
        note = ReplayAnnotation(session_id=session.id, ordinal=2, kind="note", body="initial")
        self.store.insert_replay_annotation(note)
        self.assertTrue(self.store.update_replay_annotation_body(note.id, "updated"))
        listed = self.store.list_replay_annotations(session.id)
        self.assertEqual(listed[0].body, "updated")

    def test_update_replay_annotation_body_returns_false_when_missing(self) -> None:
        self.assertFalse(self.store.update_replay_annotation_body("does-not-exist", "x"))

    def test_delete_replay_annotation_returns_true_on_success(self) -> None:
        from muxdeck.domain.replay_annotations import ReplayAnnotation

        session = self._make_session_for_annotations()
        note = ReplayAnnotation(session_id=session.id, ordinal=1, kind="note", body="y")
        self.store.insert_replay_annotation(note)
        self.assertTrue(self.store.delete_replay_annotation(note.id))
        self.assertEqual(self.store.list_replay_annotations(session.id), ())

    def test_delete_replay_annotation_returns_false_on_missing(self) -> None:
        self.assertFalse(self.store.delete_replay_annotation("missing-id"))


class StoreContextHelpersTests(SQLiteStoreTests):
    def _make_context(
        self,
        *,
        session_id: str = "session-123",
        worktree_id: str | None = "worktree-123",
        tmux_pane_id: str | None = "%1",
    ) -> object:
        from muxdeck.adapters.sqlite_store import SessionContextRecord

        return SessionContextRecord(
            session_id=session_id,
            agent_id="agent-123",
            worktree_id=worktree_id,
            tmux_pane_id=tmux_pane_id,
            pane_tty="/dev/pts/1",
            worktree_path="/repo/worktrees/x",
            copilot_session_id="cp-1",
            repo_root="/repo",
            branch="b",
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

    def test_get_session_context_returns_none_for_missing(self) -> None:
        self.assertIsNone(self.store.get_session_context("missing"))

    def test_get_session_context_round_trip(self) -> None:
        agent = self._make_agent()
        self.store.upsert_agent(agent)
        self.store.upsert_worktree(self._make_worktree())
        self.store.upsert_session(self._make_session())
        ctx = self._make_context()
        self.store.upsert_session_context(ctx)  # type: ignore[arg-type]
        fetched = self.store.get_session_context("session-123")
        self.assertIsNotNone(fetched)

    def test_get_session_context_by_tmux_pane_id_returns_none_for_missing(
        self,
    ) -> None:
        self.assertIsNone(self.store.get_session_context_by_tmux_pane_id("missing"))

    def test_list_session_contexts_for_worktree_empty_when_missing(self) -> None:
        self.assertEqual(self.store.list_session_contexts_for_worktree("missing"), ())


class StoreAgentReconcileTests(SQLiteStoreTests):
    def test_upsert_agent_reconciles_pane_conflict(self) -> None:
        # Insert agent A on pane %1 then insert agent B with same pane,
        # which triggers tmux_pane_id integrity violation. The store must
        # update the pane to point at agent B.
        agent_a = self._make_agent(pane_id="%1")
        self.store.upsert_agent(agent_a)
        # New agent with different id but same pane.
        started_at = datetime(2025, 1, 2, tzinfo=UTC)
        agent_b = Agent(
            id="agent-456",
            name="planner-b",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_window_name="main",
            tmux_pane_id="%1",
            pane_tty="/dev/pts/1",
            cwd="/repo",
            repo_root="/repo",
            worktree_path=None,
            branch=None,
            status=AgentStatus.RUNNING,
            started_at=started_at,
            last_seen_at=started_at,
            idle_seconds=0,
            needs_attention=False,
        )
        self.store.upsert_agent(agent_b)
        # Reconcile must update the row keyed on pane to point at agent
        # B (the most recently upserted row). The previous assertion
        # accepted either id, so a regression that left agent-123 in
        # place — exactly the bug this test claims to guard against —
        # would have silently passed.
        found = self.store.get_agent_by_pane_id("%1")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.id, "agent-456")
        self.assertEqual(found.name, "planner-b")
        # And there must be exactly one agent row for this pane.
        listed_for_pane = [
            agent for agent in self.store.list_agents() if agent.tmux_pane_id == "%1"
        ]
        self.assertEqual(len(listed_for_pane), 1)


class StoreWorktreeReconcileTests(SQLiteStoreTests):
    def test_upsert_worktree_reconciles_path_conflict(self) -> None:
        # Two worktrees with different ids but same path → IntegrityError
        # routes through the path-conflict reconciliation.
        wt_a = Worktree(
            id="worktree-A",
            repo_root="/repo",
            path="/repo/worktrees/shared",
            branch="branch-a",
            is_main_worktree=False,
            is_dirty=False,
            locked=False,
            last_seen_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        wt_b = Worktree(
            id="worktree-B",
            repo_root="/repo",
            path="/repo/worktrees/shared",
            branch="branch-b",
            is_main_worktree=False,
            is_dirty=False,
            locked=False,
            last_seen_at=datetime(2025, 1, 2, tzinfo=UTC),
        )
        self.store.upsert_worktree(wt_a)
        self.store.upsert_worktree(wt_b)
        listed = self.store.list_worktrees()
        # One row remains AND it carries wt_b's data — proving the
        # path-conflict reconciliation actually replaced the row, not
        # silently dropped wt_b. The earlier "len == 1" assertion would
        # have passed even if wt_a survived unchanged.
        self.assertEqual(len(listed), 1)
        survivor = listed[0]
        self.assertEqual(survivor.id, "worktree-B")
        self.assertEqual(survivor.branch, "branch-b")


class StorePragmaErrorTests(SQLiteStoreTests):
    def test_journal_mode_returns_lowercase_string(self) -> None:
        # The default journal mode set by SQLiteStore migration is "wal";
        # the type guard already enforces str, so the previous
        # ``isinstance(... str)`` assertion was redundant. Pin to the
        # actual configured mode (lower-case) so a regression that
        # changed the connection's journal_mode would surface.
        mode = self.store.journal_mode
        self.assertEqual(mode, mode.lower())
        self.assertEqual(mode, "wal")

    def test_foreign_keys_enabled_returns_bool(self) -> None:
        # SQLiteStore enables foreign keys at startup. The earlier
        # ``isinstance(... bool)`` assertion was structurally guaranteed
        # by the method's return annotation.
        self.assertIs(self.store.foreign_keys_enabled, True)


class StoreCountAndOpenSessionTests(SQLiteStoreTests):
    def test_count_sessions_for_agent_zero_when_missing(self) -> None:
        self.assertEqual(self.store.count_sessions_for_agent("missing"), 0)

    def test_count_sessions_for_agent_returns_count(self) -> None:
        self.store.upsert_agent(self._make_agent())
        self.store.upsert_session(self._make_session())
        self.assertEqual(self.store.count_sessions_for_agent("agent-123"), 1)

    def test_get_open_session_for_agent_returns_open(self) -> None:
        self.store.upsert_agent(self._make_agent())
        open_session = Session(
            id="session-open",
            agent_id="agent-123",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        self.store.upsert_session(open_session)
        found = self.store.get_open_session_for_agent("agent-123")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertIsNone(found.ended_at)

    def test_get_open_session_for_agent_returns_none_when_missing(self) -> None:
        self.assertIsNone(self.store.get_open_session_for_agent("missing"))

    def test_hot_read_paths_record_perf_spans(self) -> None:
        summarize(reset=True)
        try:
            agent = self._make_agent()
            session = self._make_session()
            self.store.upsert_agent(agent)
            self.store.upsert_session(session)
            self.store.get_latest_session_for_agent(agent.id)
            self.store.list_sessions(agent.id)
        finally:
            spans = {s.name for s in summarize(reset=True)}
        self.assertIn("sqlite.upsert agent", spans)
        self.assertIn("sqlite.upsert session", spans)
        self.assertIn("sqlite.get latest session for agent", spans)
        self.assertIn("sqlite.list sessions", spans)


class StoreDatabasePathOverrideTests(unittest.TestCase):
    def test_explicit_database_path_overrides_config(self) -> None:
        runtime_dir = (
            Path(__file__).resolve().parent
            / "_runtime_sqlite_store"
            / "test_explicit_database_path_overrides_config"
        )
        runtime_dir.mkdir(parents=True, exist_ok=True)
        try:
            db_path = runtime_dir / "explicit.sqlite"
            store = SQLiteStore(database_path=db_path)
            try:
                self.assertEqual(store.database_path, db_path.resolve())
            finally:
                store.close()
        finally:
            if runtime_dir.exists():
                shutil.rmtree(runtime_dir)
