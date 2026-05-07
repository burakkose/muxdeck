# ruff: noqa: I001,PT009,PT027

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
import shutil
import unittest

from muxdeck.adapters import DEFAULT_DATABASE_FILE_NAME, SQLiteStore
from muxdeck.config import AppConfig, PathsConfig
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.events import Event
from muxdeck.domain.models import Agent, Worktree
from muxdeck.exceptions import DomainValidationError
from muxdeck.services.session_service import SessionContextPatch, SessionService


class SessionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_dir = (
            Path(__file__).resolve().parent / "_runtime_session_service" / self._testMethodName
        )
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._cleanup_runtime_dir)
        self.repo_root = self.runtime_dir / "repo"
        self.repo_root.mkdir()
        self.worktree_path = self.runtime_dir / "worktrees" / "repo--task"
        self.worktree_path.mkdir(parents=True)
        self.config = AppConfig(
            paths=PathsConfig(
                state_dir=self.runtime_dir / "state",
                workspace_root=self.runtime_dir / "worktrees",
                database_path=self.runtime_dir / "state" / DEFAULT_DATABASE_FILE_NAME,
                fallback_database_path=(
                    self.runtime_dir / "legacy-state" / DEFAULT_DATABASE_FILE_NAME
                ),
            ),
            config_file=self.runtime_dir / "config.toml",
        )
        self.store = SQLiteStore.from_config(self.config)
        self.addCleanup(self.store.close)
        self.agent = Agent(
            id="agent-123",
            name="planner",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_window_name="main",
            tmux_pane_id="%1",
            pane_tty="/dev/pts/1",
            cwd=str(self.worktree_path),
            repo_root=str(self.repo_root),
            worktree_path=str(self.worktree_path),
            branch="task/services",
            task_title="Services",
            task_summary="Implement services",
            copilot_session_id="copilot-123",
            pid=1234,
            status=AgentStatus.RUNNING,
            started_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_activity_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 2, tzinfo=UTC),
            idle_seconds=0,
            token_input=10,
            token_output=5,
            token_total=15,
            estimated_cost_usd=Decimal("0.100000"),
        )
        self.worktree = Worktree(
            id="worktree-123",
            repo_root=str(self.repo_root),
            path=str(self.worktree_path),
            branch="task/services",
            base_branch="main",
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 2, tzinfo=UTC),
        )
        self.store.upsert_agent(self.agent)
        self.store.upsert_worktree(self.worktree)
        self.service = SessionService(store=self.store)

    def _cleanup_runtime_dir(self) -> None:
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)

    def test_create_update_and_end_session_persists_context_and_events(self) -> None:
        bundle = self.service.create_session(
            "agent-123",
            task_title="Initial task",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        updated = self.service.update_session(
            bundle.session.id,
            copilot_session_id="copilot-999",
            context_patch=SessionContextPatch(tmux_pane_id="%9"),
            events=(
                Event(
                    occurred_at=datetime(2025, 1, 1, 12, 5, tzinfo=UTC),
                    kind="custom.note",
                    payload_json='{"ok":true}',
                ),
            ),
        )
        finished = self.service.end_session(bundle.session.id, exit_reason="completed")

        self.assertEqual(bundle.context.worktree_id, "worktree-123")
        self.assertEqual(updated.context.tmux_pane_id, "%9")
        self.assertEqual(updated.context.copilot_session_id, "copilot-999")
        self.assertEqual(finished.session.exit_reason, "completed")
        event_kinds = [
            event.kind for event in self.store.list_events_for_session(bundle.session.id)
        ]
        self.assertEqual(
            event_kinds,
            ["session.created", "custom.note", "session.context.updated", "session.ended"],
        )

    def test_append_log_capture_uses_monotonic_sequence_numbers_and_replay_lookup(self) -> None:
        bundle = self.service.create_session("agent-123")

        first = self.service.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("first line",),
        )
        second = self.service.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("second line",),
        )
        by_pane = self.service.lookup_for_replay(tmux_pane_id="%1")
        by_copilot = self.service.lookup_for_replay(copilot_session_id="copilot-123")
        assert by_pane is not None
        assert by_copilot is not None

        self.assertEqual(first[0].sequence_no, 0)
        self.assertEqual(second[0].sequence_no, 1)
        self.assertEqual(by_pane.session.id, bundle.session.id)
        self.assertEqual(by_copilot.session.id, bundle.session.id)

    def test_append_log_capture_skips_empty_blocks(self) -> None:
        """append_log_capture ignores empty/whitespace-only blocks."""
        bundle = self.service.create_session("agent-123")

        result = self.service.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("", "\n", "\n\n", "actual line", ""),
        )

        # Only one non-empty block should be captured
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, "actual line")

    def test_append_log_capture_strips_content(self) -> None:
        """append_log_capture strips leading/trailing newlines."""
        bundle = self.service.create_session("agent-123")

        result = self.service.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("\n\nindented code\n\n",),
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, "indented code")

    def test_lookup_for_replay_with_multiple_locators_raises_error(self) -> None:
        """lookup_for_replay requires exactly one locator."""
        with self.assertRaises(DomainValidationError):
            self.service.lookup_for_replay(
                session_id="sid",
                copilot_session_id="csid",
            )

    def test_lookup_for_replay_with_no_locators_raises_error(self) -> None:
        """lookup_for_replay requires exactly one locator."""
        with self.assertRaises(DomainValidationError):
            self.service.lookup_for_replay()

    def test_lookup_for_replay_returns_none_when_session_not_found(self) -> None:
        """lookup_for_replay returns None when session doesn't exist."""
        result = self.service.lookup_for_replay(session_id="nonexistent")
        self.assertIsNone(result)

    def test_lookup_for_replay_by_copilot_session_id(self) -> None:
        """lookup_for_replay finds session by copilot_session_id."""
        bundle = self.service.create_session("agent-123")
        updated = self.service.update_session(
            bundle.session.id,
            copilot_session_id="copilot-999",
        )

        result = self.service.lookup_for_replay(copilot_session_id="copilot-999")

        assert result is not None
        self.assertEqual(result.session.id, updated.session.id)

    def test_assemble_session_context_creates_missing_context(self) -> None:
        """assemble_session_context creates and persists context if missing."""
        bundle = self.service.create_session("agent-123")

        # Verify context was created and persisted
        persisted_context = self.store.get_session_context(bundle.session.id)
        assert persisted_context is not None
        self.assertEqual(persisted_context.session_id, bundle.session.id)

    def test_end_session_marks_session_as_ended(self) -> None:
        """end_session sets ended_at and exit_reason."""
        created_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        bundle = self.service.create_session("agent-123", occurred_at=created_at)

        ended_at = datetime(2025, 1, 1, 13, tzinfo=UTC)
        result = self.service.end_session(
            bundle.session.id,
            exit_reason="completed",
            ended_at=ended_at,
        )

        self.assertEqual(result.session.exit_reason, "completed")
        self.assertEqual(result.session.ended_at, ended_at)

    def test_append_events_normalizes_events(self) -> None:
        """append_events normalizes event attributes correctly."""
        bundle = self.service.create_session("agent-123")

        event = Event(
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            kind="custom.event",
        )

        result = self.service.append_events(bundle.session.id, (event,))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].session_id, bundle.session.id)
        self.assertEqual(result[0].agent_id, "agent-123")


class SessionServiceErrorBranchTests(unittest.TestCase):
    """Negative-path branch coverage for SessionService."""

    def setUp(self) -> None:
        self.runtime_dir = (
            Path(__file__).resolve().parent
            / "_runtime_session_service_branch"
            / self._testMethodName
        )
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._cleanup_runtime_dir)
        self.repo_root = self.runtime_dir / "repo"
        self.repo_root.mkdir()
        self.worktree_path = self.runtime_dir / "worktrees" / "repo--task"
        self.worktree_path.mkdir(parents=True)
        self.config = AppConfig(
            paths=PathsConfig(
                state_dir=self.runtime_dir / "state",
                workspace_root=self.runtime_dir / "worktrees",
                database_path=self.runtime_dir / "state" / DEFAULT_DATABASE_FILE_NAME,
                fallback_database_path=(
                    self.runtime_dir / "legacy-state" / DEFAULT_DATABASE_FILE_NAME
                ),
            ),
            config_file=self.runtime_dir / "config.toml",
        )
        self.store = SQLiteStore.from_config(self.config)
        self.addCleanup(self.store.close)
        self.agent = Agent(
            id="agent-err",
            name="errplanner",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_window_name="main",
            tmux_pane_id="%1",
            pane_tty="/dev/pts/1",
            cwd=str(self.worktree_path),
            repo_root=str(self.repo_root),
            worktree_path=str(self.worktree_path),
            branch="task/svc",
            task_title="Svc",
            task_summary="Implement",
            copilot_session_id="copilot-err",
            pid=4321,
            status=AgentStatus.RUNNING,
            started_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_activity_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 12, 2, tzinfo=UTC),
            idle_seconds=0,
        )
        self.store.upsert_agent(self.agent)
        self.service = SessionService(store=self.store)

    def _cleanup_runtime_dir(self) -> None:
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)

    def test_append_log_capture_rejects_mismatched_agent_id(self) -> None:
        bundle = self.service.create_session("agent-err")
        with self.assertRaises(DomainValidationError):
            self.service.append_log_capture(
                bundle.session.id,
                source="stdout",
                content_blocks=("hello",),
                agent_id="agent-other",
            )

    def test_append_events_rejects_session_id_mismatch(self) -> None:
        from muxdeck.domain.events import Event as DomainEvent

        bundle = self.service.create_session("agent-err")
        event = DomainEvent(
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            kind="custom.event",
            session_id="some-other-session",
        )
        with self.assertRaises(DomainValidationError):
            self.service.append_events(bundle.session.id, (event,))

    def test_append_events_rejects_agent_id_mismatch(self) -> None:
        from muxdeck.domain.events import Event as DomainEvent

        bundle = self.service.create_session("agent-err")
        event = DomainEvent(
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            kind="custom.event",
            agent_id="another-agent",
        )
        with self.assertRaises(DomainValidationError):
            self.service.append_events(bundle.session.id, (event,))

    def test_assemble_session_context_raises_when_session_missing(self) -> None:
        from muxdeck.exceptions import PersistenceError

        with self.assertRaises(PersistenceError):
            self.service.assemble_session_context("ghost-session")

    def test_create_session_raises_when_agent_missing(self) -> None:
        from muxdeck.exceptions import PersistenceError

        with self.assertRaises(PersistenceError):
            self.service.create_session("ghost-agent")

    def test_append_events_returns_empty_tuple_when_no_events_supplied(self) -> None:
        bundle = self.service.create_session("agent-err")
        result = self.service.append_events(bundle.session.id, ())
        self.assertEqual(result, ())


if __name__ == "__main__":
    unittest.main()
