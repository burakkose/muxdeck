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
from copilot_commander.adapters.sqlite_replay_annotations import (
    SqliteReplayAnnotationsRepository,
)
from copilot_commander.config import AppConfig, PathsConfig
from copilot_commander.controllers.replay_controller import ReplayController
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.events import Event
from copilot_commander.domain.models import Agent, Worktree
from copilot_commander.services.annotations_service import AnnotationsService
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
        self.annotations = AnnotationsService(SqliteReplayAnnotationsRepository(self.store))
        self.controller = ReplayController(self.replays, self.annotations)

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
        assert next_activity is not None
        self.assertEqual(next_activity.selected_index, 2)
        self.assertIsNotNone(next_problem)
        assert next_problem is not None
        self.assertEqual(next_problem.selected_index, 2)
        self.assertIsNotNone(previous_marker)
        assert previous_marker is not None
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
        self.assertEqual(
            multi_signal.raw_lines,
            ("Running command: ruff", "fatal: merge conflict"),
        )

    def test_select_entry_updates_view_without_reloading(self) -> None:
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        timestamp = datetime(2025, 1, 1, 12, 5, tzinfo=UTC)
        self.sessions.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("Running command: pytest", "fatal: merge conflict"),
            captured_at=timestamp,
        )

        state = self.controller.load_state(
            session_id=bundle.session.id,
            follow_latest=False,
            selected_index=0,
        )
        updated = self.controller.select_entry(state, 2)

        self.assertEqual(updated.selected_index, 2)
        self.assertTrue(updated.transcript[2].is_selected)
        self.assertFalse(updated.transcript[0].is_selected)

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

    def test_load_state_defaults_playback_to_none(self) -> None:
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        self.sessions.append_events(
            bundle.session.id,
            (
                Event(
                    occurred_at=datetime(2025, 1, 1, 12, 5, tzinfo=UTC),
                    kind="custom.note",
                    payload_json='{"message":"hello"}',
                ),
            ),
        )
        state = self.controller.load_state(session_id=bundle.session.id)
        self.assertIsNone(state.playback)

    def test_initial_playback_returns_none_for_empty_transcript(self) -> None:
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        empty = self.controller.load_state(
            session_id=bundle.session.id,
            filter_text="this-token-will-match-nothing",
        )
        self.assertEqual(empty.transcript, ())
        self.assertIsNone(self.controller.initial_playback(empty))

    def test_apply_playback_updates_selected_index_and_view(self) -> None:
        from datetime import timedelta

        from copilot_commander.services.playback_controller import (
            SPEED_DOUBLE,
            PlaybackState,
        )

        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        ts1 = datetime(2025, 1, 1, 12, 5, tzinfo=UTC)
        ts2 = datetime(2025, 1, 1, 12, 10, tzinfo=UTC)
        self.sessions.append_events(
            bundle.session.id,
            (
                Event(
                    occurred_at=ts1,
                    kind="custom.first",
                    payload_json='{"i":1}',
                ),
                Event(
                    occurred_at=ts2,
                    kind="custom.second",
                    payload_json='{"i":2}',
                ),
            ),
        )
        state = self.controller.load_state(
            session_id=bundle.session.id,
            follow_latest=False,
            selected_index=0,
        )
        playback_state = self.controller.initial_playback(state)
        assert playback_state is not None

        ts1 = datetime.fromisoformat(state.transcript[0].timestamp)
        ts2 = datetime.fromisoformat(state.transcript[-1].timestamp)
        ts_mid = datetime.fromisoformat(state.transcript[1].timestamp) - timedelta(microseconds=1)
        self.assertEqual(playback_state.start, ts1)
        self.assertEqual(playback_state.end, ts2)

        midway = PlaybackState(
            mode="paused",
            speed=SPEED_DOUBLE,
            clock=ts_mid,
            start=ts1,
            end=ts2,
        )
        synced = self.controller.apply_playback(state, midway)
        self.assertEqual(synced.selected_index, state.transcript[0].ordinal)
        self.assertIsNotNone(synced.playback)
        assert synced.playback is not None
        self.assertEqual(synced.playback.speed_label, "2x")
        self.assertEqual(synced.playback.mode, "paused")

        endpoint = PlaybackState(
            mode="paused",
            speed=SPEED_DOUBLE,
            clock=ts2,
            start=ts1,
            end=ts2,
        )
        ended = self.controller.apply_playback(state, endpoint)
        self.assertEqual(ended.selected_index, state.transcript[-1].ordinal)
        assert ended.playback is not None
        self.assertEqual(ended.playback.progress, 1.0)

    def test_file_edit_precedence_and_jump_and_count(self) -> None:
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        captured_at = datetime(2025, 1, 1, 12, 5, tzinfo=UTC)
        # Chunk 1: only an activity (lower-precedence signal).
        self.sessions.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("Running command: pytest",),
            captured_at=captured_at,
        )
        # Chunk 2: contains a file edit AND an activity-style line — the
        # file_edit must win as ``marker_kind`` per the precedence rule.
        self.sessions.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("Editing file: src/auth.py\nRunning command: ruff",),
            captured_at=captured_at,
        )
        # Chunk 3: another file edit so the count and jump have somewhere
        # to advance to.
        self.sessions.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("Created file: tests/test_auth.py",),
            captured_at=captured_at,
        )

        state = self.controller.load_state(
            session_id=bundle.session.id,
            presentation="parsed",
            selected_index=0,
        )

        # Precedence: file_edit beats activity in the same chunk.
        file_edit_entry = next(
            entry for entry in state.transcript if entry.label == "modify: src/auth.py"
        )
        self.assertEqual(file_edit_entry.marker_kind, "file_edit")
        self.assertEqual(file_edit_entry.file_path, "src/auth.py")

        # Derived count covers every detected mutation, not entries.
        self.assertEqual(state.files_touched, 2)

        # Jump action lands on the next file_edit marker.
        jumped = self.controller.jump_to_next_file_edit(state)
        assert jumped is not None
        next_entry = next(
            entry for entry in jumped.transcript if entry.ordinal == jumped.selected_index
        )
        self.assertEqual(next_entry.marker_kind, "file_edit")

    def test_jump_to_next_file_edit_returns_none_without_file_markers(self) -> None:
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        captured_at = datetime(2025, 1, 1, 12, 5, tzinfo=UTC)
        self.sessions.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("Running command: pytest",),
            captured_at=captured_at,
        )

        state = self.controller.load_state(
            session_id=bundle.session.id,
            presentation="parsed",
        )

        self.assertIsNone(self.controller.jump_to_next_file_edit(state))

    def test_annotations_appear_in_state_and_jump_markers(self) -> None:
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        timestamp = datetime(2025, 1, 1, 12, 5, tzinfo=UTC)
        self.sessions.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("Running command: pytest", "fatal: merge conflict"),
            captured_at=timestamp,
        )

        added = self.controller.toggle_bookmark(bundle.session.id, ordinal=0)
        note = self.controller.add_note(bundle.session.id, ordinal=1, body="off the rails")
        state = self.controller.load_state(
            session_id=bundle.session.id,
            presentation="parsed",
        )

        self.assertTrue(added)
        self.assertEqual(len(state.annotations), 2)
        glyphs = {entry.ordinal: entry.annotation_glyph for entry in state.transcript}
        self.assertEqual(glyphs[0], "✱")
        self.assertEqual(glyphs[1], "✎")
        annotation_kinds = [marker.kind for marker in state.jump_markers]
        self.assertIn("annotation", annotation_kinds)
        annotation_marker = next(
            marker
            for marker in state.jump_markers
            if marker.kind == "annotation" and marker.index == 1
        )
        self.assertIn("off the rails", annotation_marker.label)

        removed = self.controller.toggle_bookmark(bundle.session.id, ordinal=0)
        self.assertFalse(removed)
        cleared = self.controller.list_annotations(bundle.session.id)
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0].id, note.id)

    def test_export_intent_honors_range_and_includes_annotations(self) -> None:
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        timestamp = datetime(2025, 1, 1, 12, 5, tzinfo=UTC)
        self.sessions.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("Running command: pytest", "fatal: merge conflict"),
            captured_at=timestamp,
        )
        self.controller.add_note(bundle.session.id, ordinal=1, body="watch this")

        state = self.controller.load_state(session_id=bundle.session.id)

        text_full = self.controller.build_export_intent(
            state,
            export_format="text",
            include_annotations=True,
        )
        text_sliced = self.controller.build_export_intent(
            state,
            export_format="text",
            range=(2, 2),
            include_annotations=True,
        )
        json_sliced = self.controller.build_export_intent(
            state,
            export_format="json",
            range=(1, 1),
            include_annotations=True,
        )
        markdown_full = self.controller.build_export_intent(
            state,
            export_format="markdown",
            include_annotations=True,
        )

        self.assertIn("note: watch this", text_full.content)
        self.assertNotIn("watch this", text_sliced.content)
        self.assertIn("merge conflict", text_sliced.content)
        self.assertIn('"annotations"', json_sliced.content)
        self.assertIn("watch this", json_sliced.content)
        self.assertNotIn("merge conflict", json_sliced.content)
        self.assertEqual(markdown_full.format, "markdown")
        self.assertTrue(markdown_full.filename_hint.endswith(".md"))
        self.assertIn("## Replay slice", markdown_full.content)
        self.assertIn("### #1", markdown_full.content)
        self.assertIn("```", markdown_full.content)
        self.assertIn("> Note: watch this", markdown_full.content)

    def test_substring_filter_remains_backward_compatible(self) -> None:
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        captured_at = datetime(2025, 1, 1, 12, 5, tzinfo=UTC)
        self.sessions.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("Running command: pytest\nfatal: merge conflict",),
            captured_at=captured_at,
        )

        bare = self.controller.load_state(
            session_id=bundle.session.id,
            filter_text="merge",
            presentation="parsed",
            follow_latest=True,
        )

        self.assertEqual(len(bare.transcript), 1)
        self.assertEqual(bare.transcript[0].label, "fatal: merge conflict")

    def test_chip_helpers_emit_canonical_filter_text(self) -> None:
        self.assertEqual(self.controller.apply_errors_only_chip(), "severity:error")
        self.assertEqual(self.controller.apply_activity_chip(), "marker:activity")
        self.assertEqual(self.controller.apply_tool_calls_chip(), "marker:tool_call")
        self.assertEqual(self.controller.clear_chips(), "")

    def test_load_state_attaches_insights(self) -> None:
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        captured_at = datetime(2025, 1, 1, 12, 5, tzinfo=UTC)
        self.sessions.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("fatal: merge conflict in /a/b/file.py",),
            captured_at=captured_at,
        )

        state = self.controller.load_state(
            session_id=bundle.session.id,
            presentation="parsed",
        )

        self.assertIsNotNone(state.insights)
        assert state.insights is not None
        self.assertGreaterEqual(state.insights.error_count, 1)


if __name__ == "__main__":
    unittest.main()
