# ruff: noqa: E402,I001,PT009

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
import shutil
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from copilot_commander.adapters import DEFAULT_DATABASE_FILE_NAME, SQLiteStore
from copilot_commander.config import AppConfig, PathsConfig
from copilot_commander.controllers.replay_controller import ReplayController
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.events import Event
from copilot_commander.domain.models import Agent, Worktree
from copilot_commander.services.replay_service import ReplayService
from copilot_commander.services.session_service import SessionService


class ReplayControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_dir = (
            Path(__file__).resolve().parent
            / "_runtime_replay_controller"
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
        self.store.upsert_agent(
            Agent(
                id="agent-123",
                name="planner",
                tmux_session_name="muxdeck",
                tmux_window_id="@1",
                tmux_pane_id="%1",
                cwd=str(self.worktree_path),
                repo_root=str(self.repo_root),
                worktree_path=str(self.worktree_path),
                branch="task/replay",
                task_title="Replay",
                copilot_session_id="copilot-123",
                status=AgentStatus.RUNNING,
                started_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
                last_activity_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
                last_seen_at=datetime(2025, 1, 1, 12, 2, tzinfo=UTC),
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
        self.controller = ReplayController(self.replays)

    def _cleanup_runtime_dir(self) -> None:
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)

    def test_load_state_jump_and_export(self) -> None:
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
            content_blocks=("Prompt: summarize\nwaiting for confirmation before applying patch",),
            captured_at=timestamp,
        )

        state = self.controller.load_state(session_id=bundle.session.id, selected_index=1)
        jumped = self.controller.jump_to_marker(state, 2)
        export_intent = self.controller.build_export_intent(jumped, export_format="json")

        self.assertEqual(state.transcript[1].label, "custom.note")
        self.assertEqual(
            [marker.label for marker in state.jump_markers[:3]],
            ["session.created", "custom.note", "prompt_start"],
        )
        self.assertTrue(jumped.transcript[2].is_selected)
        self.assertEqual(export_intent.filename_hint, f"replay-{bundle.session.id}.json")
        self.assertIn('"session_id"', export_intent.content)


if __name__ == "__main__":
    unittest.main()
