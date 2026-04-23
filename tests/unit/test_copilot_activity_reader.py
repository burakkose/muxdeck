"""Tests for the events.jsonl-based activity reader."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from muxdeck.adapters.copilot_activity_reader import CopilotActivityReader
from muxdeck.adapters.copilot_session_store import SessionStoreRoot


@dataclass
class _FakeStore:
    session_state_dir: Path
    extra_roots: tuple[SessionStoreRoot, ...] = ()


def _write(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def _append(path: Path, events: list[dict[str, object]]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def _start(*, call_id: str, tool: str, ts: str, **args: object) -> dict[str, object]:
    return {
        "type": "tool.execution_start",
        "timestamp": ts,
        "data": {
            "toolCallId": call_id,
            "toolName": tool,
            "arguments": args,
        },
    }


def _complete(*, call_id: str, ts: str) -> dict[str, object]:
    return {
        "type": "tool.execution_complete",
        "timestamp": ts,
        "data": {"toolCallId": call_id, "toolName": "x", "success": True},
    }


def test_returns_none_when_session_has_no_events_file(tmp_path: Path) -> None:
    reader = CopilotActivityReader(store=_FakeStore(tmp_path))  # type: ignore[arg-type]
    assert reader.read("missing") is None


def test_pending_tool_summary_is_newest_unmatched(tmp_path: Path) -> None:
    events_path = tmp_path / "s1" / "events.jsonl"
    _write(
        events_path,
        [
            _start(call_id="a", tool="bash", ts="2026-01-01T00:00:00Z", description="run tests"),
            _start(call_id="b", tool="view", ts="2026-01-01T00:00:01Z", path="/repo/src/foo.py"),
            # b completes; a is still running and is older.
            _complete(call_id="b", ts="2026-01-01T00:00:02Z"),
        ],
    )
    reader = CopilotActivityReader(store=_FakeStore(tmp_path))  # type: ignore[arg-type]
    act = reader.read("s1")
    assert act is not None
    assert act.tool_name == "bash"
    assert act.summary == "running run tests"
    assert not act.waiting_for_user


def test_ask_user_pending_flags_waiting_for_user(tmp_path: Path) -> None:
    events_path = tmp_path / "s2" / "events.jsonl"
    _write(
        events_path,
        [
            _start(
                call_id="q1",
                tool="ask_user",
                ts="2026-01-01T00:00:00Z",
                question="Which database should I use?",
            ),
        ],
    )
    reader = CopilotActivityReader(store=_FakeStore(tmp_path))  # type: ignore[arg-type]
    act = reader.read("s2")
    assert act is not None
    assert act.waiting_for_user is True
    assert act.tool_name == "ask_user"
    assert act.summary is not None
    assert "Which database" in act.summary


def test_ask_user_after_complete_clears_waiting(tmp_path: Path) -> None:
    events_path = tmp_path / "s3" / "events.jsonl"
    _write(
        events_path,
        [
            _start(
                call_id="q1",
                tool="ask_user",
                ts="2026-01-01T00:00:00Z",
                question="Which DB?",
            ),
            _complete(call_id="q1", ts="2026-01-01T00:00:05Z"),
        ],
    )
    reader = CopilotActivityReader(store=_FakeStore(tmp_path))  # type: ignore[arg-type]
    act = reader.read("s3")
    assert act is not None
    assert act.waiting_for_user is False
    # All tools done → no pending tool summary; intent may still be None.
    assert act.tool_name is None


def test_report_intent_is_fallback_summary(tmp_path: Path) -> None:
    events_path = tmp_path / "s4" / "events.jsonl"
    _write(
        events_path,
        [
            _start(
                call_id="i1",
                tool="report_intent",
                ts="2026-01-01T00:00:00Z",
                intent="Investigating bug hunt",
            ),
        ],
    )
    reader = CopilotActivityReader(store=_FakeStore(tmp_path))  # type: ignore[arg-type]
    act = reader.read("s4")
    assert act is not None
    assert act.intent == "Investigating bug hunt"
    assert act.summary == "Investigating bug hunt"
    assert act.tool_name is None


def test_incremental_read_only_processes_new_events(tmp_path: Path) -> None:
    events_path = tmp_path / "s5" / "events.jsonl"
    _write(
        events_path,
        [
            _start(
                call_id="a",
                tool="bash",
                ts="2026-01-01T00:00:00Z",
                description="first",
            ),
        ],
    )
    reader = CopilotActivityReader(store=_FakeStore(tmp_path))  # type: ignore[arg-type]
    first = reader.read("s5")
    assert first is not None
    assert first.summary == "running first"

    _append(
        events_path,
        [
            _start(
                call_id="b",
                tool="edit",
                ts="2026-01-01T00:00:01Z",
                path="/repo/src/bar.py",
            ),
        ],
    )
    second = reader.read("s5")
    assert second is not None
    assert second.tool_name == "edit"
    # Path target is basenamed.
    assert second.summary == "editing bar.py"


def test_rotate_detects_inode_change(tmp_path: Path) -> None:
    events_path = tmp_path / "s6" / "events.jsonl"
    _write(
        events_path,
        [
            _start(
                call_id="a",
                tool="bash",
                ts="2026-01-01T00:00:00Z",
                description="old",
            ),
        ],
    )
    reader = CopilotActivityReader(store=_FakeStore(tmp_path))  # type: ignore[arg-type]
    reader.read("s6")

    # Rotate: truncate + rewrite with completely different content.
    events_path.unlink()
    _write(
        events_path,
        [
            _start(
                call_id="x",
                tool="view",
                ts="2026-01-02T00:00:00Z",
                path="/repo/new.py",
            ),
        ],
    )
    # Force mtime distinctly forward so the reader re-examines the head
    # fingerprint even when the filesystem's mtime resolution coalesces
    # the two writes into the same nanosecond.
    import os as _os

    st = events_path.stat()
    _os.utime(events_path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    act = reader.read("s6")
    assert act is not None
    assert act.tool_name == "view"
    assert act.summary == "reading new.py"


def test_truncation_resets_pending_state(tmp_path: Path) -> None:
    events_path = tmp_path / "s7" / "events.jsonl"
    _write(
        events_path,
        [
            _start(
                call_id="a",
                tool="bash",
                ts="2026-01-01T00:00:00Z",
                description="first",
            ),
        ],
    )
    reader = CopilotActivityReader(store=_FakeStore(tmp_path))  # type: ignore[arg-type]
    reader.read("s7")
    # Truncate in place (same inode, smaller size).
    with events_path.open("w", encoding="utf-8") as fh:
        fh.truncate()
    act = reader.read("s7")
    assert act is not None
    # Pending state should have been cleared; no activity to report.
    assert act.summary is None
    assert act.tool_name is None


def test_handles_partial_trailing_line(tmp_path: Path) -> None:
    events_path = tmp_path / "s8" / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    # Write a complete event followed by a partial line (no trailing \n).
    first = json.dumps(
        _start(call_id="a", tool="view", ts="2026-01-01T00:00:00Z", path="/repo/a.py")
    )
    partial = '{"type":"tool.execution_start","timestamp":"2026-01-01T00:00:01Z",'
    with events_path.open("w", encoding="utf-8") as fh:
        fh.write(first + "\n" + partial)

    reader = CopilotActivityReader(store=_FakeStore(tmp_path))  # type: ignore[arg-type]
    act = reader.read("s8")
    assert act is not None
    assert act.tool_name == "view"
    # Now finish the partial line + a complete one.
    completion = '"data":{"toolCallId":"b","toolName":"edit","arguments":{"path":"/repo/b.py"}}}\n'
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(completion)
    act = reader.read("s8")
    assert act is not None
    assert act.tool_name == "edit"
    assert act.summary == "editing b.py"


# ── transcript capture ────────────────────────────────────────────────────


def _assistant_message(*, ts: str, content: str) -> dict[str, object]:
    return {
        "type": "assistant.message",
        "timestamp": ts,
        "data": {"content": content},
    }


def _user_message(*, ts: str, content: str) -> dict[str, object]:
    return {
        "type": "user.message",
        "timestamp": ts,
        "data": {"content": content},
    }


def test_transcript_captures_assistant_and_user_messages(tmp_path: Path) -> None:
    events_path = tmp_path / "s-tx" / "events.jsonl"
    _write(
        events_path,
        [
            _user_message(ts="2026-01-01T00:00:00Z", content="hello, please do X"),
            _assistant_message(
                ts="2026-01-01T00:00:01Z",
                content="Sure — first I'll look at the code.\nThen I'll run tests.",
            ),
        ],
    )
    reader = CopilotActivityReader(store=_FakeStore(tmp_path))  # type: ignore[arg-type]
    lines = reader.read_transcript("s-tx", limit=10)
    assert len(lines) == 3
    assert lines[0].role == "user"
    assert lines[0].content == "hello, please do X"
    assert lines[1].role == "assistant"
    assert lines[1].content == "Sure — first I'll look at the code."
    assert lines[2].role == "assistant"
    assert lines[2].content == "Then I'll run tests."


def test_transcript_ignores_empty_and_tool_only_messages(tmp_path: Path) -> None:
    """assistant.message with empty content (pure tool-request turn) is skipped."""
    events_path = tmp_path / "s-empty" / "events.jsonl"
    _write(
        events_path,
        [
            _assistant_message(ts="2026-01-01T00:00:00Z", content=""),
            _assistant_message(ts="2026-01-01T00:00:01Z", content="   \n   "),
            _assistant_message(ts="2026-01-01T00:00:02Z", content="real output"),
        ],
    )
    reader = CopilotActivityReader(store=_FakeStore(tmp_path))  # type: ignore[arg-type]
    lines = reader.read_transcript("s-empty", limit=10)
    assert len(lines) == 1
    assert lines[0].content == "real output"


def test_transcript_is_incremental_across_appends(tmp_path: Path) -> None:
    """A message appended later must show up without losing earlier turns."""
    events_path = tmp_path / "s-incr" / "events.jsonl"
    _write(
        events_path,
        [_assistant_message(ts="2026-01-01T00:00:00Z", content="first")],
    )
    reader = CopilotActivityReader(store=_FakeStore(tmp_path))  # type: ignore[arg-type]
    first = reader.read_transcript("s-incr", limit=10)
    assert [ln.content for ln in first] == ["first"]

    _append(events_path, [_assistant_message(ts="2026-01-01T00:00:05Z", content="second")])
    second = reader.read_transcript("s-incr", limit=10)
    assert [ln.content for ln in second] == ["first", "second"]
    # Sequence numbers are monotonically increasing — so clients can
    # de-dupe by seq across polls.
    assert second[0].sequence_no < second[1].sequence_no


def test_transcript_returns_empty_tuple_when_unknown(tmp_path: Path) -> None:
    reader = CopilotActivityReader(store=_FakeStore(tmp_path))  # type: ignore[arg-type]
    assert reader.read_transcript("does-not-exist", limit=10) == ()


def test_transcript_clears_on_file_truncation(tmp_path: Path) -> None:
    """When events.jsonl is truncated (size shrinks) the reader must reset
    the transcript so stale prose doesn't persist into the new run."""
    session_dir = tmp_path / "s-rot"
    events_path = session_dir / "events.jsonl"
    _write(
        events_path,
        [
            _assistant_message(ts="2026-01-01T00:00:00Z", content="old one"),
            _assistant_message(ts="2026-01-01T00:00:01Z", content="old two"),
            _assistant_message(ts="2026-01-01T00:00:02Z", content="old three"),
        ],
    )
    reader = CopilotActivityReader(store=_FakeStore(tmp_path))  # type: ignore[arg-type]
    assert len(reader.read_transcript("s-rot", limit=10)) == 3

    # Truncate in place with a single small event — size drops below the
    # reader's recorded last_size, so the size-shrink branch of rotation
    # detection fires regardless of inode / mtime quirks.
    _write(events_path, [_assistant_message(ts="2026-01-02T00:00:00Z", content="fresh")])
    lines = reader.read_transcript("s-rot", limit=10)
    assert [ln.content for ln in lines] == ["fresh"]
