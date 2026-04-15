# ruff: noqa: I001,PT009,PT027

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
import shutil
import unittest

from copilot_commander.adapters import DEFAULT_DATABASE_FILE_NAME, SQLiteStore
from copilot_commander.config import AppConfig, PathsConfig
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.events import Event
from copilot_commander.domain.models import Agent, Worktree
from copilot_commander.services.session_service import SessionContextPatch, SessionService


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


if __name__ == "__main__":
    unittest.main()
