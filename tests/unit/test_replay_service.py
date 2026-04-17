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
from copilot_commander.services.replay_service import ReplayService
from copilot_commander.services.session_service import SessionService


class ReplayServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_dir = (
            Path(__file__).resolve().parent / "_runtime_replay_service" / self._testMethodName
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
        self.store.upsert_agent(
            Agent(
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
                branch="task/replay",
                task_title="Replay",
                task_summary="Build replay",
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
        )
        self.store.upsert_worktree(
            Worktree(
                id="worktree-123",
                repo_root=str(self.repo_root),
                path=str(self.worktree_path),
                branch="task/replay",
                base_branch="main",
                created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
                last_seen_at=datetime(2025, 1, 1, 12, 2, tzinfo=UTC),
            )
        )
        self.sessions = SessionService(store=self.store)
        self.replays = ReplayService(store=self.store, sessions=self.sessions)

    def _cleanup_runtime_dir(self) -> None:
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)

    def test_replay_orders_equal_timestamps_and_builds_jump_markers(self) -> None:
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        timestamp = datetime(2025, 1, 1, 12, 5, tzinfo=UTC)
        self.sessions.append_events(
            bundle.session.id,
            (
                Event(
                    occurred_at=timestamp,
                    kind="custom.note",
                    payload_json='{"message":"before log"}',
                ),
            ),
        )
        self.sessions.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=(
                "Prompt: summarize\n"
                "Running command: pytest\n"
                "waiting for confirmation before applying patch\n"
                "CONFLICT (content): merge conflict in src/app.py\n"
                "fatal: build aborted",
            ),
            captured_at=timestamp,
        )

        replay = self.replays.load_session_replay(bundle.session.id)
        transcript = self.replays.export_transcript_text(replay)

        self.assertEqual(replay.entries[1].kind, "event")
        self.assertEqual(replay.entries[2].kind, "log")
        self.assertEqual(
            [marker.label for marker in replay.jump_markers],
            [
                "session.created",
                "custom.note",
                "running pytest",
                "prompt_start",
                "waiting_for_confirmation",
                "merge_conflict",
                "fatal: build aborted",
            ],
        )
        self.assertIn("EVENT custom.note", transcript)
        self.assertIn("LOG stdout#0", transcript)


if __name__ == "__main__":
    unittest.main()
