"""Tests for event log extraction in the dashboard controller."""

from __future__ import annotations

from datetime import UTC, datetime

from muxdeck.controllers.dashboard_controller import (
    _MAX_RECENT_EVENTS,
    _extract_recent_events,
)
from muxdeck.domain.events import LogChunk


def _log(content: str, *, sequence_no: int = 0) -> LogChunk:
    return LogChunk(
        agent_id="agent-1",
        session_id="session-1",
        source="tmux_capture",
        sequence_no=sequence_no,
        captured_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        content=content,
    )


# ── parsing from sample pane output ────────────────────────────────


class TestEventParsingFromPaneOutput:
    def test_file_read_event(self) -> None:
        logs = [_log("Read file: src/app.py")]
        events = _extract_recent_events(logs)
        assert len(events) == 1
        assert "📖" in events[0]
        assert "src/app.py" in events[0]

    def test_file_write_event(self) -> None:
        logs = [_log("Editing src/utils.py")]
        events = _extract_recent_events(logs)
        assert len(events) == 1
        assert "✏️" in events[0]
        assert "src/utils.py" in events[0]

    def test_command_event(self) -> None:
        logs = [_log("Running: python -m pytest")]
        events = _extract_recent_events(logs)
        assert len(events) == 1
        assert "⚡" in events[0]
        assert "python -m pytest" in events[0]

    def test_thinking_event(self) -> None:
        logs = [_log("Thinking...")]
        events = _extract_recent_events(logs)
        assert len(events) == 1
        assert "💭" in events[0]
        assert "Thinking" in events[0]

    def test_search_event(self) -> None:
        logs = [_log("Searching for 'imports'")]
        events = _extract_recent_events(logs)
        assert len(events) == 1
        assert "🔍" in events[0]

    def test_tool_use_event(self) -> None:
        logs = [_log("Using tool: bash")]
        events = _extract_recent_events(logs)
        assert len(events) == 1
        assert "🔧" in events[0]
        assert "bash" in events[0]

    def test_error_event(self) -> None:
        logs = [_log("error: something went wrong")]
        events = _extract_recent_events(logs)
        assert len(events) == 1
        assert "⚠️" in events[0]

    def test_mixed_activities(self) -> None:
        content = "\n".join(
            [
                "Read file: src/main.py",
                "some intermediate output",
                "Editing src/utils.py",
                "Running: python -m pytest",
                "Thinking...",
            ]
        )
        logs = [_log(content)]
        events = _extract_recent_events(logs)
        assert len(events) >= 4
        # Verify order is preserved
        assert "📖" in events[0]
        assert "✏️" in events[1]
        assert "⚡" in events[2]
        assert "💭" in events[3]

    def test_no_activities_in_plain_output(self) -> None:
        logs = [_log("just a regular line\nnothing to see here\n")]
        events = _extract_recent_events(logs)
        assert events == ()

    def test_empty_logs(self) -> None:
        events = _extract_recent_events([])
        assert events == ()

    def test_multiple_log_chunks(self) -> None:
        logs = [
            _log("Read file: src/a.py", sequence_no=0),
            _log("Writing src/b.py", sequence_no=1),
        ]
        events = _extract_recent_events(logs)
        assert len(events) >= 2


# ── event formatting ────────────────────────────────────────────────


class TestEventFormatting:
    def test_events_are_emoji_prefixed_strings(self) -> None:
        logs = [_log("Read file: src/app.py")]
        events = _extract_recent_events(logs)
        assert len(events) == 1
        assert isinstance(events[0], str)
        # Should start with an emoji, not plain text
        assert not events[0][0].isalpha()

    def test_activity_text_is_capitalized(self) -> None:
        logs = [_log("Thinking...")]
        events = _extract_recent_events(logs)
        # After emoji and space, first letter should be capitalized
        text_part = events[0].split(" ", 1)[1] if " " in events[0] else events[0]
        assert text_part[0].isupper()

    def test_file_read_format(self) -> None:
        logs = [_log("Read file: src/app.py")]
        events = _extract_recent_events(logs)
        assert events[0] == "📖 Reading src/app.py"

    def test_command_format(self) -> None:
        logs = [_log("Running: npm test")]
        events = _extract_recent_events(logs)
        assert events[0] == "⚡ Running npm test"

    def test_thinking_format(self) -> None:
        logs = [_log("Thinking...")]
        events = _extract_recent_events(logs)
        assert events[0] == "💭 Thinking"

    def test_consecutive_duplicates_are_deduped(self) -> None:
        content = "\n".join(
            [
                "Thinking...",
                "some output",
                "Thinking...",
            ]
        )
        logs = [_log(content)]
        events = _extract_recent_events(logs)
        thinking_events = [e for e in events if "💭" in e]
        assert len(thinking_events) == 1


# ── max events cap ──────────────────────────────────────────────────


class TestMaxEventsCap:
    def test_caps_at_max(self) -> None:
        # Generate more than _MAX_RECENT_EVENTS activities
        lines = [f"Read file: src/file_{i}.py" for i in range(30)]
        logs = [_log("\n".join(lines))]
        events = _extract_recent_events(logs)
        assert len(events) <= _MAX_RECENT_EVENTS

    def test_keeps_most_recent(self) -> None:
        lines = [f"Read file: src/file_{i}.py" for i in range(30)]
        logs = [_log("\n".join(lines))]
        events = _extract_recent_events(logs)
        # The last events should come from the highest-numbered files
        assert "file_29" in events[-1]

    def test_custom_limit(self) -> None:
        lines = [f"Read file: src/file_{i}.py" for i in range(10)]
        logs = [_log("\n".join(lines))]
        events = _extract_recent_events(logs, limit=5)
        assert len(events) <= 5

    def test_max_constant_is_20(self) -> None:
        assert _MAX_RECENT_EVENTS == 20
