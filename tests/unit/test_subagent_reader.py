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
        # Sanity: replace file contents but keep mtime → cached result wins.
        original_mtime = events_path.stat().st_mtime_ns
        events_path.write_text("", encoding="utf-8")
        import os

        os.utime(events_path, ns=(original_mtime, original_mtime))
        second = reader.read(session_id)
        # Same cached tree object is returned.
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
            (
                '{"type":"tool.execution_complete","timestamp":"2026-01-01T00:00:02Z",'
                '"data":{"toolName":"task","toolCallId":"tc-2","success":true,'
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
