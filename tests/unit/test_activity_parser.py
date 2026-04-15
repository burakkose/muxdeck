"""Tests for activity pattern parsing in copilot output parser."""

from __future__ import annotations

import unittest
from decimal import Decimal

from copilot_commander.parsers.copilot_output_parser import (
    CopilotOutputParseResult,
    parse_copilot_output,
)
from copilot_commander.services.monitoring_service import _extract_latest_activity


class ActivityParserFileReadTests(unittest.TestCase):
    def test_read_file_colon(self) -> None:
        result = parse_copilot_output("Read file: src/main.py")
        assert len(result.activity_markers) == 1
        marker = result.activity_markers[0]
        assert marker.activity == "reading src/main.py"
        assert marker.category == "file_read"

    def test_reading_file(self) -> None:
        result = parse_copilot_output("Reading src/auth.py")
        assert len(result.activity_markers) == 1
        assert result.activity_markers[0].activity == "reading src/auth.py"
        assert result.activity_markers[0].category == "file_read"

    def test_read_function_call_style(self) -> None:
        result = parse_copilot_output('Read(file_path="src/utils.py")')
        assert len(result.activity_markers) == 1
        assert result.activity_markers[0].activity == "reading src/utils.py"
        assert result.activity_markers[0].category == "file_read"

    def test_read_with_backtick_quotes(self) -> None:
        result = parse_copilot_output("Read file: `src/config.toml`")
        assert len(result.activity_markers) == 1
        assert "src/config.toml" in result.activity_markers[0].activity


class ActivityParserFileWriteTests(unittest.TestCase):
    def test_write_file(self) -> None:
        result = parse_copilot_output("Write file: src/main.py")
        assert len(result.activity_markers) == 1
        assert result.activity_markers[0].activity == "writing src/main.py"
        assert result.activity_markers[0].category == "file_write"

    def test_editing_file(self) -> None:
        result = parse_copilot_output("Editing src/parser.py")
        assert len(result.activity_markers) == 1
        assert result.activity_markers[0].activity == "writing src/parser.py"
        assert result.activity_markers[0].category == "file_write"

    def test_creating_file(self) -> None:
        result = parse_copilot_output("Creating tests/test_new.py")
        assert len(result.activity_markers) == 1
        assert result.activity_markers[0].activity == "writing tests/test_new.py"
        assert result.activity_markers[0].category == "file_write"

    def test_wrote_to_file(self) -> None:
        result = parse_copilot_output("Written to src/output.json")
        assert len(result.activity_markers) == 1
        assert result.activity_markers[0].category == "file_write"


class ActivityParserCommandTests(unittest.TestCase):
    def test_run_command(self) -> None:
        result = parse_copilot_output("Run command: npm test")
        assert len(result.activity_markers) == 1
        assert result.activity_markers[0].activity == "running npm test"
        assert result.activity_markers[0].category == "command"

    def test_running_command(self) -> None:
        result = parse_copilot_output("Running: pytest -v")
        assert len(result.activity_markers) == 1
        assert "pytest -v" in result.activity_markers[0].activity
        assert result.activity_markers[0].category == "command"

    def test_bash_function_call(self) -> None:
        result = parse_copilot_output('Bash(command="npm install")')
        assert len(result.activity_markers) == 1
        assert result.activity_markers[0].activity == "running npm install"
        assert result.activity_markers[0].category == "command"

    def test_executing_command(self) -> None:
        result = parse_copilot_output("Executing command: make build")
        assert len(result.activity_markers) == 1
        assert result.activity_markers[0].category == "command"


class ActivityParserThinkingTests(unittest.TestCase):
    def test_thinking_ellipsis(self) -> None:
        result = parse_copilot_output("Thinking...")
        assert len(result.activity_markers) == 1
        assert result.activity_markers[0].activity == "thinking"
        assert result.activity_markers[0].category == "thinking"

    def test_planning(self) -> None:
        result = parse_copilot_output("Planning...")
        assert len(result.activity_markers) == 1
        assert result.activity_markers[0].activity == "thinking"
        assert result.activity_markers[0].category == "thinking"

    def test_analyzing(self) -> None:
        result = parse_copilot_output("Analyzing")
        assert len(result.activity_markers) == 1
        assert result.activity_markers[0].activity == "thinking"
        assert result.activity_markers[0].category == "thinking"


class ActivityParserSearchTests(unittest.TestCase):
    def test_searching_for(self) -> None:
        result = parse_copilot_output("Searching for 'auth handler'")
        assert len(result.activity_markers) == 1
        assert "auth handler" in result.activity_markers[0].activity
        assert result.activity_markers[0].category == "search"

    def test_grep_pattern(self) -> None:
        result = parse_copilot_output("Grep(pattern='TODO')")
        assert len(result.activity_markers) == 1
        assert result.activity_markers[0].category == "search"


class ActivityParserToolUseTests(unittest.TestCase):
    def test_tool_colon(self) -> None:
        result = parse_copilot_output("Tool: read_file")
        assert len(result.activity_markers) == 1
        assert result.activity_markers[0].activity == "using tool read_file"
        assert result.activity_markers[0].category == "tool_use"

    def test_using_tool(self) -> None:
        result = parse_copilot_output("Using tool: bash_command")
        assert len(result.activity_markers) == 1
        assert result.activity_markers[0].category == "tool_use"


class ActivityParserEdgeCaseTests(unittest.TestCase):
    def test_no_activity_in_noise(self) -> None:
        result = parse_copilot_output("just a regular line\nnothing to see here\n")
        assert result.activity_markers == ()

    def test_multiple_activities_last_wins(self) -> None:
        output = "\n".join(
            [
                "Reading src/a.py",
                "some output here",
                "Writing src/b.py",
                "more output",
                "Thinking...",
            ]
        )
        result = parse_copilot_output(output)
        assert len(result.activity_markers) == 3
        last = result.activity_markers[-1]
        assert last.activity == "thinking"
        assert last.category == "thinking"

    def test_activity_markers_have_correct_line_numbers(self) -> None:
        output = "line one\nReading src/test.py\nline three"
        result = parse_copilot_output(output)
        assert len(result.activity_markers) == 1
        assert result.activity_markers[0].span.start_line == 2

    def test_activity_markers_in_evidence_spans(self) -> None:
        result = parse_copilot_output("Reading src/foo.py")
        assert len(result.activity_markers) == 1
        activity_spans = [
            span for span in result.evidence_spans if span.category.startswith("activity:")
        ]
        assert len(activity_spans) == 1

    def test_confidence_is_valid(self) -> None:
        result = parse_copilot_output("Thinking...")
        marker = result.activity_markers[0]
        assert marker.span.confidence == Decimal("0.8500")

    def test_backward_compat_default_empty(self) -> None:
        """CopilotOutputParseResult can be created without activity_markers."""
        result = CopilotOutputParseResult(
            session_ids=(),
            boundaries=(),
            usage_snapshots=(),
            blocking_issues=(),
            errors=(),
            ui_markers=(),
            evidence_spans=(),
        )
        assert result.activity_markers == ()

    def test_mixed_with_session_and_usage(self) -> None:
        output = "\n".join(
            [
                "Copilot session id: sess-abc123",
                "Reading src/main.py",
                "input_tokens: 500",
                "output_tokens: 200",
            ]
        )
        result = parse_copilot_output(output)
        assert len(result.session_ids) == 1
        assert len(result.activity_markers) == 1
        assert len(result.usage_snapshots) == 1


class ExtractLatestActivityTests(unittest.TestCase):
    def test_returns_none_for_no_markers(self) -> None:
        result = parse_copilot_output("no activity here")
        assert _extract_latest_activity(result) is None

    def test_returns_last_activity(self) -> None:
        output = "Reading src/a.py\nWriting src/b.py"
        result = parse_copilot_output(output)
        activity = _extract_latest_activity(result)
        assert activity == "writing src/b.py"

    def test_returns_single_activity(self) -> None:
        result = parse_copilot_output("Thinking...")
        activity = _extract_latest_activity(result)
        assert activity == "thinking"


if __name__ == "__main__":
    unittest.main()
