"""Unit tests for the sub-agent reader and its event parsing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from copilot_commander.adapters.copilot_session_store import SessionStoreRoot
from copilot_commander.adapters.subagent_reader import SubAgentReader
from copilot_commander.domain.subagents import SubAgentSnapshot, SubAgentTree


@dataclass
class _FakeStore:
    """Minimal stand-in for ``CopilotSessionStore`` — just enough for

    the reader's ``_SessionDirProvider`` protocol. We never touch the
    real store in these tests.
    """

    session_state_dir: Path
    extra_roots: tuple[SessionStoreRoot, ...] = ()


def _write_events(session_dir: Path, lines: list[str]) -> Path:
    session_dir.mkdir(parents=True, exist_ok=True)
    events_path = session_dir / "events.jsonl"
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return events_path


def _started_event(
    *,
    tool_call_id: str,
    agent_name: str = "general-purpose",
    display: str = "General Purpose Agent",
    description: str | None = "a general purpose agent",
    timestamp: str,
) -> str:
    import json as _json

    data = {
        "toolCallId": tool_call_id,
        "agentName": agent_name,
        "agentDisplayName": display,
        "agentDescription": description,
    }
    event = {
        "type": "subagent.started",
        "data": data,
        "id": f"evt-{tool_call_id}-start",
        "timestamp": timestamp,
        "parentId": None,
    }
    return _json.dumps(event)


def _completed_event(
    *,
    tool_call_id: str,
    agent_name: str = "general-purpose",
    display: str = "General Purpose Agent",
    timestamp: str,
) -> str:
    import json as _json

    event = {
        "type": "subagent.completed",
        "data": {
            "toolCallId": tool_call_id,
            "agentName": agent_name,
            "agentDisplayName": display,
        },
        "id": f"evt-{tool_call_id}-end",
        "timestamp": timestamp,
        "parentId": None,
    }
    return _json.dumps(event)


class TestSubAgentReader:
    def test_returns_none_when_session_not_resolvable(self, tmp_path: Path) -> None:
        store = _FakeStore(session_state_dir=tmp_path)
        reader = SubAgentReader(store)
        assert reader.read("missing-session") is None

    def test_returns_empty_tree_when_no_subagent_events(self, tmp_path: Path) -> None:
        session_id = "session-a"
        _write_events(
            tmp_path / session_id,
            [
                '{"type":"session.start","data":{"sessionId":"session-a"},'
                '"id":"e1","timestamp":"2026-04-03T21:23:04.699Z"}',
                '{"type":"user.message","data":{"content":"hi"},'
                '"id":"e2","timestamp":"2026-04-03T21:23:05.000Z"}',
            ],
        )
        reader = SubAgentReader(_FakeStore(session_state_dir=tmp_path))
        tree = reader.read(session_id)
        assert tree is not None
        assert tree.session_id == session_id
        assert tree.running == ()
        assert tree.recent == ()
        assert tree.is_empty()

    def test_pairs_started_and_completed_by_tool_call_id(self, tmp_path: Path) -> None:
        session_id = "session-b"
        _write_events(
            tmp_path / session_id,
            [
                _started_event(
                    tool_call_id="call_a",
                    timestamp="2026-04-03T21:00:00.000Z",
                ),
                _started_event(
                    tool_call_id="call_b",
                    agent_name="explore",
                    display="Explore Agent",
                    timestamp="2026-04-03T21:00:05.000Z",
                ),
                _completed_event(
                    tool_call_id="call_a",
                    timestamp="2026-04-03T21:00:12.000Z",
                ),
            ],
        )
        reader = SubAgentReader(_FakeStore(session_state_dir=tmp_path))
        tree = reader.read(session_id)
        assert tree is not None
        assert {s.tool_call_id for s in tree.running} == {"call_b"}
        assert [s.tool_call_id for s in tree.recent] == ["call_a"]
        completed = tree.recent[0]
        assert completed.agent_name == "general-purpose"
        assert completed.display_name == "General Purpose Agent"
        assert completed.completed_at is not None
        assert completed.duration_seconds == pytest.approx(12.0)
        running = tree.running[0]
        assert running.agent_name == "explore"
        assert running.display_name == "Explore Agent"
        assert running.is_running is True

    def test_completed_without_matching_start_still_recorded(self, tmp_path: Path) -> None:
        """Truncated logs can leave a completion event whose ``started``

        partner has scrolled off. We record a synthetic snapshot so the
        operator can still see *something* happened, rather than the
        event being silently dropped.
        """
        session_id = "session-c"
        _write_events(
            tmp_path / session_id,
            [
                _completed_event(
                    tool_call_id="call_orphan",
                    timestamp="2026-04-03T21:10:00.000Z",
                ),
            ],
        )
        reader = SubAgentReader(_FakeStore(session_state_dir=tmp_path))
        tree = reader.read(session_id)
        assert tree is not None
        assert len(tree.recent) == 1
        orphan = tree.recent[0]
        assert orphan.tool_call_id == "call_orphan"
        assert orphan.completed_at is not None
        assert orphan.started_at == orphan.completed_at

    def test_mtime_cache_skips_reparse_when_unchanged(self, tmp_path: Path) -> None:
        session_id = "session-d"
        events_path = _write_events(
            tmp_path / session_id,
            [
                _started_event(
                    tool_call_id="call_x",
                    timestamp="2026-04-03T21:00:00.000Z",
                ),
            ],
        )
        reader = SubAgentReader(_FakeStore(session_state_dir=tmp_path))
        first = reader.read(session_id)
        assert first is not None
        # File untouched → the incremental reader hands back the same
        # tree object without re-opening the file.
        assert events_path.exists()
        second = reader.read(session_id)
        assert second is first

    def test_mtime_cache_invalidates_on_update(self, tmp_path: Path) -> None:
        session_id = "session-e"
        session_dir = tmp_path / session_id
        _write_events(
            session_dir,
            [
                _started_event(
                    tool_call_id="call_one",
                    timestamp="2026-04-03T21:00:00.000Z",
                ),
            ],
        )
        reader = SubAgentReader(_FakeStore(session_state_dir=tmp_path))
        first = reader.read(session_id)
        assert first is not None
        assert len(first.running) == 1

        # Append a completion — mtime advances, cache must refresh.
        with (session_dir / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(
                _completed_event(
                    tool_call_id="call_one",
                    timestamp="2026-04-03T21:00:07.000Z",
                )
                + "\n"
            )
        import os

        new_ns = (session_dir / "events.jsonl").stat().st_mtime_ns + 10_000_000
        os.utime(session_dir / "events.jsonl", ns=(new_ns, new_ns))
        assert (session_dir / "events.jsonl").stat().st_mtime_ns == new_ns

        second = reader.read(session_id)
        assert second is not None
        assert second.running == ()
        assert len(second.recent) == 1

    def test_extra_roots_are_searched(self, tmp_path: Path) -> None:
        primary = tmp_path / "primary"
        extra = tmp_path / "windows"
        primary.mkdir()
        extra.mkdir()

        session_id = "session-on-extra"
        _write_events(
            extra / session_id,
            [
                _started_event(
                    tool_call_id="call_w",
                    timestamp="2026-04-03T21:00:00.000Z",
                ),
            ],
        )
        store = _FakeStore(
            session_state_dir=primary,
            extra_roots=(SessionStoreRoot(extra, "windows"),),
        )
        reader = SubAgentReader(store)
        tree = reader.read(session_id)
        assert tree is not None
        assert len(tree.running) == 1

    def test_incremental_append_only_reads_new_bytes(self, tmp_path: Path) -> None:
        """Appending a new event must not cause a full-file re-parse.

        We prove it by monkey-patching ``Path.open`` to record how
        many bytes each read call consumes. The second read, after
        appending one line, should consume strictly less than the
        full file.
        """
        session_id = "session-incr"
        session_dir = tmp_path / session_id
        first_line = _started_event(
            tool_call_id="call_a",
            timestamp="2026-04-03T21:00:00.000Z",
        )
        _write_events(session_dir, [first_line])
        events_path = session_dir / "events.jsonl"

        reader = SubAgentReader(_FakeStore(session_state_dir=tmp_path))
        first = reader.read(session_id)
        assert first is not None
        assert len(first.running) == 1

        baseline_size = events_path.stat().st_size

        # Append a second event.
        with events_path.open("a", encoding="utf-8") as fh:
            fh.write(
                _completed_event(
                    tool_call_id="call_a",
                    timestamp="2026-04-03T21:00:05.000Z",
                )
                + "\n"
            )

        # Intercept the open+read the reader performs and record how
        # many characters come back.
        read_sizes: list[int] = []
        original_open = Path.open

        def tracking_open(self: Path, *args: object, **kwargs: object) -> object:
            fh = original_open(self, *args, **kwargs)  # type: ignore[arg-type]
            if self == events_path:
                original_read = fh.read

                def read(size: int = -1, _orig: object = original_read) -> str:
                    data = _orig(size)  # type: ignore[operator]
                    if isinstance(data, str):
                        read_sizes.append(len(data))
                    return data  # type: ignore[no-any-return]

                fh.read = read  # type: ignore[method-assign]
            return fh

        import unittest.mock

        with unittest.mock.patch.object(Path, "open", tracking_open):
            second = reader.read(session_id)

        assert second is not None
        # Only the appended bytes were read — not the whole file.
        total_read = sum(read_sizes)
        assert total_read > 0
        assert total_read < baseline_size, (
            f"reader consumed {total_read} bytes but baseline file was "
            f"{baseline_size}; expected incremental tail read"
        )
        # And the tree reflects the new event.
        assert second.running == ()
        assert len(second.recent) == 1

    def test_rotation_resets_offset(self, tmp_path: Path) -> None:
        """If the file shrinks below our offset (truncate or swap), we
        reset and reparse from byte zero instead of silently skipping."""
        session_id = "session-rot"
        session_dir = tmp_path / session_id
        _write_events(
            session_dir,
            [
                _started_event(
                    tool_call_id="call_old",
                    timestamp="2026-04-03T21:00:00.000Z",
                ),
                _completed_event(
                    tool_call_id="call_old",
                    timestamp="2026-04-03T21:00:01.000Z",
                ),
            ],
        )
        reader = SubAgentReader(_FakeStore(session_state_dir=tmp_path))
        first = reader.read(session_id)
        assert first is not None
        assert len(first.recent) == 1

        # Rewrite the file with a single new event (smaller than original).
        _write_events(
            session_dir,
            [
                _started_event(
                    tool_call_id="call_new",
                    timestamp="2026-04-03T22:00:00.000Z",
                ),
            ],
        )
        second = reader.read(session_id)
        assert second is not None
        # We saw the fresh event and forgot the old one (file was rotated).
        assert len(second.running) == 1
        assert second.running[0].tool_call_id == "call_new"
        assert second.recent == ()


class TestSubAgentSnapshot:
    def test_duration_seconds_none_while_running(self) -> None:
        snap = SubAgentSnapshot(
            tool_call_id="x",
            agent_name="a",
            display_name="A",
            description=None,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=None,
        )
        assert snap.is_running is True
        assert snap.duration_seconds is None

    def test_total_count_combines_running_and_recent(self) -> None:
        snap_running = SubAgentSnapshot(
            tool_call_id="r",
            agent_name="a",
            display_name="A",
            description=None,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=None,
        )
        snap_done = SubAgentSnapshot(
            tool_call_id="d",
            agent_name="a",
            display_name="A",
            description=None,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC),
        )
        tree = SubAgentTree(
            session_id="s",
            running=(snap_running,),
            recent=(snap_done,),
            scanned_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        )
        assert tree.running_count == 1
        assert tree.total_count == 2
        assert tree.is_empty() is False


class TestTaskToolEnrichment:
    def test_reader_merges_task_prompt_and_result(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "sess-xyz"
        events = [
            (
                '{"type":"tool.execution_start","timestamp":"2026-01-01T00:00:00Z",'
                '"data":{"toolName":"task","toolCallId":"tc-1","arguments":'
                '{"name":"math-helper","agent_type":"general-purpose",'
                '"description":"help with math","prompt":"solve 2+2"}}}'
            ),
            _started_event(tool_call_id="tc-1", timestamp="2026-01-01T00:00:01Z"),
        ]
        _write_events(session_dir, events)
        store = _FakeStore(session_state_dir=tmp_path)
        reader = SubAgentReader(store=store)  # type: ignore[arg-type]

        tree = reader.read("sess-xyz")

        assert tree is not None
        assert len(tree.running) == 1
        snap = tree.running[0]
        assert snap.task_name == "math-helper"
        assert snap.agent_type == "general-purpose"
        assert snap.prompt == "solve 2+2"
        # Still running → no result yet.
        assert snap.result_content is None
        assert snap.success is None

    def test_reader_captures_success_and_result(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "sess-done"
        events = [
            (
                '{"type":"tool.execution_start","timestamp":"2026-01-01T00:00:00Z",'
                '"data":{"toolName":"task","toolCallId":"tc-2","arguments":'
                '{"name":"n","agent_type":"explore","prompt":"p"}}}'
            ),
            _started_event(tool_call_id="tc-2", timestamp="2026-01-01T00:00:01Z"),
            _completed_event(tool_call_id="tc-2", timestamp="2026-01-01T00:00:02Z"),
            # Real CLI does NOT repeat ``toolName`` on tool.execution_complete.
            # Matching must work off toolCallId alone.
            (
                '{"type":"tool.execution_complete","timestamp":"2026-01-01T00:00:02Z",'
                '"data":{"toolCallId":"tc-2","success":true,'
                '"result":{"content":"final answer"}}}'
            ),
        ]
        _write_events(session_dir, events)
        store = _FakeStore(session_state_dir=tmp_path)
        reader = SubAgentReader(store=store)  # type: ignore[arg-type]

        tree = reader.read("sess-done")

        assert tree is not None
        assert len(tree.recent) == 1
        snap = tree.recent[0]
        assert snap.success is True
        assert snap.result_content == "final answer"
        assert snap.prompt == "p"

    def test_reader_prefers_detailed_content_when_available(self, tmp_path: Path) -> None:
        """``detailedContent`` carries the full agent output; ``content`` is a
        summary. The UI wants the detailed version when present."""
        session_dir = tmp_path / "sess-detailed"
        events = [
            (
                '{"type":"tool.execution_start","timestamp":"2026-01-01T00:00:00Z",'
                '"data":{"toolName":"task","toolCallId":"tc-3","arguments":'
                '{"name":"n","agent_type":"general-purpose","prompt":"p"}}}'
            ),
            _started_event(tool_call_id="tc-3", timestamp="2026-01-01T00:00:01Z"),
            _completed_event(tool_call_id="tc-3", timestamp="2026-01-01T00:00:02Z"),
            (
                '{"type":"tool.execution_complete","timestamp":"2026-01-01T00:00:02Z",'
                '"data":{"toolCallId":"tc-3","success":true,'
                '"result":{"content":"short","detailedContent":"the full answer body"}}}'
            ),
        ]
        _write_events(session_dir, events)
        reader = SubAgentReader(store=_FakeStore(session_state_dir=tmp_path))  # type: ignore[arg-type]
        tree = reader.read("sess-detailed")
        assert tree is not None
        snap = tree.recent[0]
        assert snap.result_content == "the full answer body"

    def test_reader_captures_background_mode(self, tmp_path: Path) -> None:
        """Background-mode tasks are tagged so the UI can label the
        parent's 'result' as a launch ack rather than real output."""
        session_dir = tmp_path / "sess-bg"
        events = [
            (
                '{"type":"tool.execution_start","timestamp":"2026-01-01T00:00:00Z",'
                '"data":{"toolName":"task","toolCallId":"tc-bg","arguments":'
                '{"name":"bg-agent","agent_type":"general-purpose",'
                '"mode":"background","prompt":"do stuff"}}}'
            ),
            _started_event(tool_call_id="tc-bg", timestamp="2026-01-01T00:00:01Z"),
            (
                '{"type":"tool.execution_complete","timestamp":"2026-01-01T00:00:02Z",'
                '"data":{"toolCallId":"tc-bg","success":true,'
                '"result":{"content":"Agent started in background with agent_id: bg-agent"}}}'
            ),
        ]
        _write_events(session_dir, events)
        reader = SubAgentReader(store=_FakeStore(session_state_dir=tmp_path))  # type: ignore[arg-type]
        tree = reader.read("sess-bg")
        assert tree is not None
        # Background sub-agent is still "running" in the subagent-event sense
        # until its own subagent.completed lands.
        snap = tree.running[0]
        assert snap.mode == "background"
        assert snap.result_content is not None
        assert "background" in snap.result_content.lower()


class TestReadAgentInteractions:
    """Background sub-agents stream their output back to the parent via
    ``read_agent`` tool calls keyed by the task's ``name`` argument.
    The reader must correlate those calls back to the launching
    sub-agent so the detail view has something to render."""

    def test_read_agent_calls_are_attached_to_matching_background_subagent(
        self, tmp_path: Path
    ) -> None:
        session_dir = tmp_path / "sess-ra"
        events = [
            (
                '{"type":"tool.execution_start","timestamp":"2026-01-01T00:00:00Z",'
                '"data":{"toolName":"task","toolCallId":"tc-task",'
                '"arguments":{"name":"risk-reviewer","agent_type":"general-purpose",'
                '"mode":"background","prompt":"assess risks"}}}'
            ),
            _started_event(tool_call_id="tc-task", timestamp="2026-01-01T00:00:01Z"),
            (
                '{"type":"tool.execution_start","timestamp":"2026-01-01T00:01:00Z",'
                '"data":{"toolName":"read_agent","toolCallId":"tc-read-1",'
                '"arguments":{"agent_id":"risk-reviewer","wait":true,"timeout":15}}}'
            ),
            (
                '{"type":"tool.execution_complete","timestamp":"2026-01-01T00:01:05Z",'
                '"data":{"toolCallId":"tc-read-1","success":true,'
                '"result":{"content":"progress so far","detailedContent":"long details"}}}'
            ),
            (
                '{"type":"tool.execution_start","timestamp":"2026-01-01T00:02:00Z",'
                '"data":{"toolName":"read_agent","toolCallId":"tc-read-2",'
                '"arguments":{"agent_id":"risk-reviewer","wait":false}}}'
            ),
            (
                '{"type":"tool.execution_complete","timestamp":"2026-01-01T00:02:01Z",'
                '"data":{"toolCallId":"tc-read-2","success":true,'
                '"result":{"content":"final answer"}}}'
            ),
            # An unrelated read_agent for a different agent must not leak in.
            (
                '{"type":"tool.execution_start","timestamp":"2026-01-01T00:03:00Z",'
                '"data":{"toolName":"read_agent","toolCallId":"tc-read-other",'
                '"arguments":{"agent_id":"some-other-agent"}}}'
            ),
            (
                '{"type":"tool.execution_complete","timestamp":"2026-01-01T00:03:01Z",'
                '"data":{"toolCallId":"tc-read-other","success":true,'
                '"result":{"content":"elsewhere"}}}'
            ),
        ]
        _write_events(session_dir, events)
        reader = SubAgentReader(store=_FakeStore(session_state_dir=tmp_path))  # type: ignore[arg-type]

        tree = reader.read("sess-ra")

        assert tree is not None
        snap = tree.running[0]
        assert snap.task_name == "risk-reviewer"
        # Interactions appear in observation order.
        assert len(snap.read_interactions) == 2
        first, second = snap.read_interactions
        assert 'agent_id="risk-reviewer"' in first.arguments_summary
        assert "wait=true" in first.arguments_summary
        assert "timeout=15" in first.arguments_summary
        # Detailed content wins over short content.
        assert first.result_content == "long details"
        assert "wait=false" in second.arguments_summary
        assert second.result_content == "final answer"

    def test_background_subagent_with_no_read_agent_calls_still_captures_metrics(
        self, tmp_path: Path
    ) -> None:
        session_dir = tmp_path / "sess-metrics"
        events = [
            (
                '{"type":"tool.execution_start","timestamp":"2026-01-01T00:00:00Z",'
                '"data":{"toolName":"task","toolCallId":"tc-m",'
                '"arguments":{"name":"worker","agent_type":"general-purpose",'
                '"mode":"background","prompt":"work"}}}'
            ),
            _started_event(tool_call_id="tc-m", timestamp="2026-01-01T00:00:01Z"),
            (
                '{"type":"subagent.completed","timestamp":"2026-01-01T00:02:05Z",'
                '"data":{"toolCallId":"tc-m","agentName":"general-purpose",'
                '"agentDisplayName":"General Purpose Agent",'
                '"model":"claude-sonnet-4.5","totalTokens":133463,'
                '"totalToolCalls":18,"durationMs":124335}}'
            ),
        ]
        _write_events(session_dir, events)
        reader = SubAgentReader(store=_FakeStore(session_state_dir=tmp_path))  # type: ignore[arg-type]

        tree = reader.read("sess-metrics")

        assert tree is not None
        snap = tree.recent[0]
        assert snap.read_interactions == ()
        assert snap.total_tokens == 133463
        assert snap.duration_ms == 124335
        assert snap.total_tool_calls == 18
        assert snap.model == "claude-sonnet-4.5"
        assert snap.error_message is None

    def test_subagent_failed_populates_error_message(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "sess-fail"
        events = [
            _started_event(tool_call_id="tc-f", timestamp="2026-01-01T00:00:01Z"),
            (
                '{"type":"subagent.failed","timestamp":"2026-01-01T00:00:30Z",'
                '"data":{"toolCallId":"tc-f","agentName":"general-purpose",'
                '"agentDisplayName":"General Purpose Agent",'
                '"error":"AbortError: This operation was aborted"}}'
            ),
        ]
        _write_events(session_dir, events)
        reader = SubAgentReader(store=_FakeStore(session_state_dir=tmp_path))  # type: ignore[arg-type]

        tree = reader.read("sess-fail")

        assert tree is not None
        snap = tree.recent[0]
        assert snap.error_message is not None
        assert "Abort" in snap.error_message
        assert snap.success is False
        # ``subagent.failed`` is also terminal — it must move the sub-agent
        # out of the running bucket.
        assert tree.running == ()

    def test_foreground_subagent_has_no_read_interactions_and_keeps_result_content(
        self, tmp_path: Path
    ) -> None:
        """Foreground tasks return output synchronously; they don't
        produce read_agent calls and the existing result_content must
        still round-trip intact."""
        session_dir = tmp_path / "sess-fg"
        events = [
            (
                '{"type":"tool.execution_start","timestamp":"2026-01-01T00:00:00Z",'
                '"data":{"toolName":"task","toolCallId":"tc-fg",'
                '"arguments":{"name":"fg","agent_type":"explore","prompt":"look"}}}'
            ),
            _started_event(tool_call_id="tc-fg", timestamp="2026-01-01T00:00:01Z"),
            _completed_event(tool_call_id="tc-fg", timestamp="2026-01-01T00:00:05Z"),
            (
                '{"type":"tool.execution_complete","timestamp":"2026-01-01T00:00:05Z",'
                '"data":{"toolCallId":"tc-fg","success":true,'
                '"result":{"content":"the answer"}}}'
            ),
        ]
        _write_events(session_dir, events)
        reader = SubAgentReader(store=_FakeStore(session_state_dir=tmp_path))  # type: ignore[arg-type]

        tree = reader.read("sess-fg")

        assert tree is not None
        snap = tree.recent[0]
        assert snap.read_interactions == ()
        assert snap.result_content == "the answer"
        assert snap.total_tokens is None
        assert snap.error_message is None
