# ruff: noqa: E402,I001,PT009,PT027

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
            Path(__file__).resolve().parent / "_runtime_replay_controller" / self._testMethodName
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

    def test_load_state_supports_parsed_filtering_and_export(self) -> None:
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
                "fatal: merge conflict",
            ),
            captured_at=timestamp,
        )

        state = self.controller.load_state(
            session_id=bundle.session.id,
            selected_index=1,
            presentation="parsed",
        )
        filtered = self.controller.load_state(
            session_id=bundle.session.id,
            filter_text="merge",
            presentation="parsed",
            follow_latest=True,
        )
        export_intent = self.controller.build_export_intent(filtered, export_format="json")

        self.assertEqual(state.transcript[2].label, "fatal: merge conflict")
        self.assertEqual(state.transcript[2].marker_kind, "error")
        self.assertIn("activity", [marker.kind for marker in state.jump_markers])
        self.assertEqual(len(filtered.transcript), 1)
        self.assertEqual(filtered.transcript[0].label, "fatal: merge conflict")
        self.assertEqual(filtered.selected_index, filtered.transcript[0].ordinal)
        self.assertIn('"presentation": "parsed"', export_intent.content)
        self.assertIn('"filter_text": "merge"', export_intent.content)

    def test_jump_actions_follow_activity_problem_and_previous_marker(self) -> None:
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
                "Running command: pytest\n"
                "waiting for confirmation before applying patch\n"
                "fatal: merge conflict",
            ),
            captured_at=timestamp,
        )

        state = self.controller.load_state(
            session_id=bundle.session.id,
            selected_index=1,
            presentation="parsed",
        )
        next_activity = self.controller.jump_to_next_activity(state)
        next_problem = self.controller.jump_to_next_problem(state)
        previous_marker = self.controller.jump_to_previous_marker(state)

        self.assertIsNotNone(next_activity)
        self.assertEqual(next_activity.selected_index, 2)
        self.assertIsNotNone(next_problem)
        self.assertEqual(next_problem.selected_index, 2)
        self.assertIsNotNone(previous_marker)
        self.assertEqual(previous_marker.selected_index, 0)

    def test_parsed_transcript_drops_redundant_first_signal_line(self) -> None:
        """The first parsed signal line is pure duplication.

        ``_build_parsed_log_view`` uses ``signals[0]`` as ``(label_kind,
        label)`` for the kind and label columns, then formats every
        signal as ``f"{kind}: {value}"`` into ``lines``. That makes the
        first line always equal to ``f"{label_kind}: {label}"`` — which
        the transcript widget already renders as separate columns,
        producing a visible double-render like
        ``activity reading (General-purpose activity: reading (General-purpose``.
        The controller must drop that redundant first line but keep any
        additional distinct signals.
        """

        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        captured_at = datetime(2025, 1, 1, 12, 5, tzinfo=UTC)
        # Single-activity chunk — ``lines`` must be empty after the fix.
        self.sessions.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("Running command: pytest",),
            captured_at=captured_at,
        )
        # Multi-signal chunk — the redundant activity line is dropped
        # but the error signal from a later line is still retained.
        self.sessions.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("Running command: ruff\nfatal: merge conflict",),
            captured_at=captured_at,
        )

        state = self.controller.load_state(
            session_id=bundle.session.id,
            presentation="parsed",
        )

        single_activity = next(
            entry for entry in state.transcript if entry.label.startswith("running")
        )
        self.assertEqual(single_activity.marker_kind, "activity")
        self.assertEqual(
            single_activity.lines,
            (),
            "single-signal activity entries must not repeat the label as a preview line",
        )

        multi_signal = next(entry for entry in state.transcript if entry.marker_kind == "error")
        redundant = f"{multi_signal.marker_kind}: {multi_signal.label}"
        self.assertNotIn(
            redundant,
            multi_signal.lines,
            "the first signal line must be dropped even when additional signals remain",
        )
        self.assertTrue(
            multi_signal.lines,
            "additional distinct signals must still be surfaced",
        )

    def test_load_state_single_session_omits_agent_label_and_lists_session_id(self) -> None:
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )

        state = self.controller.load_state(session_id=bundle.session.id)

        self.assertEqual(state.session_ids, (bundle.session.id,))
        self.assertEqual(state.agent_ids, (bundle.session.agent_id,))
        for entry in state.transcript:
            self.assertIsNone(entry.agent_label)

    def test_load_multi_state_populates_agent_label_and_agent_ids(self) -> None:
        from copilot_commander.domain.enums import AgentStatus
        from copilot_commander.domain.models import Agent

        self.store.upsert_agent(
            Agent(
                id="agent-456",
                name="reviewer",
                tmux_session_name="muxdeck",
                tmux_window_id="@2",
                tmux_pane_id="%2",
                cwd=str(self.worktree_path),
                repo_root=str(self.repo_root),
                worktree_path=str(self.worktree_path),
                branch="task/replay",
                task_title="Review",
                copilot_session_id="copilot-456",
                status=AgentStatus.RUNNING,
                started_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
                last_activity_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
                last_seen_at=datetime(2025, 1, 1, 12, 2, tzinfo=UTC),
            )
        )
        bundle_a = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
        )
        bundle_b = self.sessions.create_session(
            "agent-456",
            occurred_at=datetime(2025, 1, 1, 12, 0, 30, tzinfo=UTC),
        )

        state = self.controller.load_multi_state(
            (bundle_a.session.id, bundle_b.session.id),
        )

        self.assertEqual(state.session_ids, (bundle_a.session.id, bundle_b.session.id))
        self.assertEqual(set(state.agent_ids), {"agent-123", "agent-456"})
        agent_labels = {entry.agent_id: entry.agent_label for entry in state.transcript}
        self.assertEqual(agent_labels["agent-123"], "A")
        self.assertEqual(agent_labels["agent-456"], "B")
        self.assertEqual(state.session_id, bundle_a.session.id)

    def test_load_multi_state_rejects_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            self.controller.load_multi_state(())


if __name__ == "__main__":
    unittest.main()
