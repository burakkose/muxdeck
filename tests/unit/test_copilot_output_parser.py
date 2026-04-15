from __future__ import annotations

import unittest
from decimal import Decimal

from copilot_commander.parsers.copilot_output_parser import parse_copilot_output


class CopilotOutputParserTests(unittest.TestCase):
    def test_parse_copilot_output_extracts_session_boundaries_and_usage(self) -> None:
        output = "\n".join(
            (
                "Copilot session id: session-01HZX9ABCDEF",
                "Prompt: summarize the repo status",
                "Response: working on it",
                "input_tokens: 1,200",
                "output_tokens: 345",
                "estimated cost: $0.012345",
            )
        )

        result = parse_copilot_output(output)

        assert [candidate.value for candidate in result.session_ids] == ["session-01HZX9ABCDEF"]
        assert [boundary.kind for boundary in result.boundaries] == [
            "prompt_start",
            "response_start",
        ]
        assert len(result.usage_snapshots) == 1
        usage = result.usage_snapshots[0]
        assert usage.input_tokens == 1200
        assert usage.output_tokens == 345
        assert usage.cost == Decimal("0.012345")
        assert usage.currency == "USD"
        assert (usage.span.start_line, usage.span.end_line) == (4, 6)

    def test_parse_copilot_output_detects_blockers_and_errors_with_evidence(self) -> None:
        output = "\n".join(
            (
                "waiting for confirmation before applying patch",
                "fatal: merge conflict in src/app.py",
                "authentication failed; sign in again",
                "tool failed with exit code 1",
                "HTTP 429 rate limit exceeded",
                "Traceback (most recent call last):",
            )
        )

        result = parse_copilot_output(output)

        assert [issue.kind for issue in result.blocking_issues] == [
            "waiting_for_confirmation",
            "merge_conflict",
            "authentication_issue",
            "tool_failure",
            "rate_limit",
        ]
        assert len(result.errors) == 2
        assert result.errors[0].span.start_line == 2
        assert result.errors[1].span.start_line == 6
        assert all(span.confidence >= Decimal("0.9000") for span in result.evidence_spans)

    def test_parse_copilot_output_stays_quiet_on_noise(self) -> None:
        output = "\n".join(
            (
                "session ready but no identifier",
                "assistant is thinking",
                "token budget may change later",
            )
        )

        result = parse_copilot_output(output)

        assert result.session_ids == ()
        assert result.boundaries == ()
        assert result.usage_snapshots == ()
        assert result.blocking_issues == ()
        assert result.errors == ()
        assert result.evidence_spans == ()

    def test_parse_copilot_output_avoids_success_false_positives(self) -> None:
        output = "\n".join(
            (
                "tool completed successfully with exit code 0",
                "authentication succeeded",
                "merge conflict resolved",
                "current rate limit: 1000 requests/hour",
            )
        )

        result = parse_copilot_output(output)

        assert result.blocking_issues == ()


if __name__ == "__main__":
    unittest.main()
