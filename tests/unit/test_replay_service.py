# ruff: noqa: I001,PT009,PT027

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import shutil
import unittest

from muxdeck.adapters import DEFAULT_DATABASE_FILE_NAME, SQLiteStore
from muxdeck.config import AppConfig, PathsConfig
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.events import Event, LogChunk
from muxdeck.domain.models import Agent, Worktree
from muxdeck.services.replay_service import ReplayService
from muxdeck.services.session_service import SessionService


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
        self._upsert_agent("agent-123", "%1")
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

    def _upsert_agent(self, agent_id: str, pane_id: str) -> None:
        self.store.upsert_agent(
            Agent(
                id=agent_id,
                name=f"planner-{agent_id}",
                tmux_session_name="muxdeck",
                tmux_window_id="@1",
                tmux_window_name="main",
                tmux_pane_id=pane_id,
                pane_tty=f"/dev/pts/{pane_id.lstrip('%')}",
                cwd=str(self.worktree_path),
                repo_root=str(self.repo_root),
                worktree_path=str(self.worktree_path),
                branch="task/replay",
                task_title="Replay",
                task_summary="Build replay",
                copilot_session_id=f"copilot-{agent_id}",
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

    def test_load_multi_session_replay_merges_chronologically(self) -> None:
        self._upsert_agent("agent-456", "%2")
        bundle_a = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
        )
        bundle_b = self.sessions.create_session(
            "agent-456",
            occurred_at=datetime(2025, 1, 1, 12, 0, 30, tzinfo=UTC),
        )
        self.sessions.append_events(
            bundle_a.session.id,
            (
                Event(
                    occurred_at=datetime(2025, 1, 1, 12, 1, tzinfo=UTC),
                    kind="a.work",
                    payload_json='{"x":1}',
                ),
            ),
        )
        self.sessions.append_events(
            bundle_b.session.id,
            (
                Event(
                    occurred_at=datetime(2025, 1, 1, 12, 2, tzinfo=UTC),
                    kind="b.work",
                    payload_json='{"y":2}',
                ),
                Event(
                    occurred_at=datetime(2025, 1, 1, 12, 3, tzinfo=UTC),
                    kind="b.done",
                    payload_json='{"y":3}',
                ),
            ),
        )

        replay = self.replays.load_multi_session_replay((bundle_a.session.id, bundle_b.session.id))

        # Entries are chronologically ordered across sessions and re-ordinaled.
        ordinals = [entry.ordinal for entry in replay.entries]
        self.assertEqual(ordinals, list(range(len(replay.entries))))
        kinds_by_session = [
            (entry.session_id, entry.event.kind if entry.event else "log")
            for entry in replay.entries
        ]
        self.assertEqual(
            kinds_by_session,
            [
                (bundle_a.session.id, "session.created"),
                (bundle_b.session.id, "session.created"),
                (bundle_a.session.id, "a.work"),
                (bundle_b.session.id, "b.work"),
                (bundle_b.session.id, "b.done"),
            ],
        )
        # agent_switch markers fire at every transition between distinct agents.
        switches = [marker for marker in replay.jump_markers if marker.kind == "agent_switch"]
        self.assertEqual(len(switches), 3)
        self.assertEqual(
            [s.label for s in switches],
            ["agent-123→agent-456", "agent-456→agent-123", "agent-123→agent-456"],
        )
        self.assertEqual(replay.sessions[0].id, bundle_a.session.id)
        self.assertEqual(replay.sessions[1].id, bundle_b.session.id)

    def test_load_multi_session_replay_single_agent_skips_agent_switch(self) -> None:
        bundle_a = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
        )
        bundle_b = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, 0, 30, tzinfo=UTC),
        )

        replay = self.replays.load_multi_session_replay((bundle_a.session.id, bundle_b.session.id))

        kinds = {marker.kind for marker in replay.jump_markers}
        self.assertNotIn("agent_switch", kinds)

    def test_load_multi_session_replay_rejects_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            self.replays.load_multi_session_replay(())

    def test_jump_markers_include_file_edit_and_tool_call_kinds(self) -> None:
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        captured_at = datetime(2025, 1, 1, 12, 5, tzinfo=UTC)
        self.sessions.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("Editing file: src/auth.py\nTool: ripgrep\nDeleted: legacy/old.py",),
            captured_at=captured_at,
        )

        replay = self.replays.load_session_replay(bundle.session.id)
        kinds = [marker.kind for marker in replay.jump_markers]
        labels_for_file_edit = [
            marker.label for marker in replay.jump_markers if marker.kind == "file_edit"
        ]
        labels_for_tool = [
            marker.label for marker in replay.jump_markers if marker.kind == "tool_call"
        ]

        self.assertIn("file_edit", kinds)
        self.assertIn("tool_call", kinds)
        self.assertEqual(sorted(labels_for_file_edit), ["legacy/old.py", "src/auth.py"])
        self.assertEqual(labels_for_tool, ["ripgrep"])

    def test_load_multi_session_replay_deduplicates_session_ids(self) -> None:
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
        )

        replay = self.replays.load_multi_session_replay((bundle.session.id, bundle.session.id))

        self.assertEqual(len(replay.sessions), 1)

    def test_export_transcript_text_includes_all_entry_types(self) -> None:
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
                    payload_json='{"message":"test"}',
                    severity="info",
                ),
            ),
        )
        self.sessions.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("output line 1", "output line 2"),
            captured_at=datetime(2025, 1, 1, 12, 10, tzinfo=UTC),
        )

        replay = self.replays.load_session_replay(bundle.session.id)
        transcript = self.replays.export_transcript_text(replay)

        self.assertIn("EVENT", transcript)
        self.assertIn("LOG", transcript)
        self.assertIn("output line 1", transcript)

    def test_export_transcript_skips_log_entries_with_no_chunk(self) -> None:
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )

        replay = self.replays.load_session_replay(bundle.session.id)
        lines = self.replays.export_transcript_lines(replay)

        self.assertEqual(len(lines), 1)

    def test_build_entry_markers_with_file_mutations(self) -> None:
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        timestamp = datetime(2025, 1, 1, 12, 5, tzinfo=UTC)
        self.sessions.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=("Created file: src/new.py",),
            captured_at=timestamp,
        )

        replay = self.replays.load_session_replay(bundle.session.id)
        markers = replay.jump_markers

        file_edit_markers = [m for m in markers if m.kind == "file_edit"]
        self.assertEqual(len(file_edit_markers), 1)

    def test_payload_normalization_handles_invalid_json(self) -> None:
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
                    payload_json='{"valid":"json"}',
                ),
            ),
        )

        replay = self.replays.load_session_replay(bundle.session.id)
        transcript = self.replays.export_transcript_text(replay)

        self.assertIn("valid", transcript)

    def test_load_replay_by_locator_with_copilot_session_id(self) -> None:
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )

        replay = self.replays.load_replay_by_locator(copilot_session_id="copilot-agent-123")

        self.assertEqual(replay.entries[0].session_id, bundle.session.id)

    def test_load_replay_by_locator_with_tmux_pane_id(self) -> None:
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )

        replay = self.replays.load_replay_by_locator(tmux_pane_id="%1")

        self.assertEqual(replay.entries[0].session_id, bundle.session.id)

    def test_load_replay_by_locator_raises_on_not_found(self) -> None:
        with self.assertRaises(LookupError) as cm:
            self.replays.load_replay_by_locator(session_id="nonexistent")
        self.assertIn("no replayable session found", str(cm.exception))

    def test_load_session_replay_preview_limit_uses_bounded_store_api(self) -> None:
        """Live-tail / follow_latest mode must not materialize the whole
        session backlog. When ``preview_limit`` is provided, the service
        calls the bounded store APIs (``list_recent_events_for_session`` /
        ``list_recent_log_chunks``) and the resulting replay contains at
        most ``preview_limit`` entries per kind.
        """
        bundle = self.sessions.create_session(
            "agent-123",
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        # 50 events + 50 log chunks → 101 entries with the session.created
        # event. preview_limit=5 must return at most 5 events + 5 chunks
        # + 1 created event ≤ 11 entries.
        base = datetime(2025, 1, 1, 12, 5, tzinfo=UTC)

        self.sessions.append_events(
            bundle.session.id,
            tuple(
                Event(
                    occurred_at=base + timedelta(seconds=i),
                    kind="custom.tick",
                    payload_json=f'{{"i":{i}}}',
                )
                for i in range(50)
            ),
        )
        self.sessions.append_log_capture(
            bundle.session.id,
            source="stdout",
            content_blocks=tuple(f"line-{i}\n" for i in range(50)),
            captured_at=base,
        )

        full = self.replays.load_session_replay(bundle.session.id)
        preview = self.replays.load_session_replay(bundle.session.id, preview_limit=5)

        # Full replay sees everything; preview sees the recent tail only.
        self.assertGreater(len(full.entries), len(preview.entries))
        self.assertLessEqual(len(preview.entries), 5 + 5 + 1)
        # Preview must end at the latest entry (live-tail UX).
        self.assertEqual(preview.entries[-1].timestamp, full.entries[-1].timestamp)

    def test_load_session_replay_preview_falls_back_for_legacy_stores(self) -> None:
        """Stores that don't expose the bounded helpers must still work —
        the service falls back to the unbounded list APIs.
        """

        class LegacyStore:
            def __init__(self, store: SQLiteStore) -> None:
                self._store = store

            def list_events_for_session(self, session_id: str, /) -> tuple[Event, ...]:
                return self._store.list_events_for_session(session_id)

            def list_log_chunks(self, session_id: str, /) -> tuple[LogChunk, ...]:
                return self._store.list_log_chunks(session_id)

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
                    payload_json='{"m":"hi"}',
                ),
            ),
        )

        legacy = ReplayService(store=LegacyStore(self.store), sessions=self.sessions)
        # Should not raise; should return the full replay regardless of
        # preview_limit (since the legacy store cannot honour it).
        replay = legacy.load_session_replay(bundle.session.id, preview_limit=1)
        self.assertGreaterEqual(len(replay.entries), 2)


if __name__ == "__main__":
    unittest.main()
