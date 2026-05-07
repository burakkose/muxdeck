# ruff: noqa: PT009, PT027, B017

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Protocol, cast

from muxdeck.parsers.copilot_output_parser import (
    CopilotOutputParseResult,
    parse_copilot_output,
)


class _CachedParser(Protocol):
    def __call__(self, output: str) -> CopilotOutputParseResult: ...

    def cache_clear(self) -> None: ...


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
                "CONFLICT (content): merge conflict in src/app.py",
                "authentication failed; sign in again",
                "HTTP 429 rate limit exceeded",
                "Traceback (most recent call last):",
            )
        )

        result = parse_copilot_output(output)

        # tool_failure patterns were intentionally removed because
        # "command failed" / "exit code N" / "stderr:" match routine
        # tool output and produced constant false positives in the
        # status layer. The remaining blocking kinds cover the signals
        # that actually require operator attention.
        assert [issue.kind for issue in result.blocking_issues] == [
            "waiting_for_confirmation",
            "merge_conflict",
            "authentication_issue",
            "rate_limit",
        ]
        assert len(result.errors) == 1
        assert result.errors[0].span.start_line == 5
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

    def test_parse_copilot_output_reuses_cached_result_for_identical_text(self) -> None:
        parser = cast(_CachedParser, parse_copilot_output)
        parser.cache_clear()
        output = " / commands · ? help · ctrl+q enqueue"

        first = parser(output)
        second = parser(output)

        assert first is second


class CopilotEvidenceSpanValidationTests(unittest.TestCase):
    """Cover the validation branches of ``CopilotEvidenceSpan``."""

    def test_invalid_line_numbers_raise(self) -> None:
        from muxdeck.parsers.copilot_output_parser import CopilotEvidenceSpan

        with self.assertRaises(ValueError):
            CopilotEvidenceSpan(
                category="x",
                start_line=0,  # must be ≥ 1
                end_line=1,
                text="t",
                confidence=Decimal("0.9"),
            )
        with self.assertRaises(ValueError):
            CopilotEvidenceSpan(
                category="x",
                start_line=2,
                end_line=1,  # end before start
                text="t",
                confidence=Decimal("0.9"),
            )

    def test_empty_category_raises(self) -> None:
        from muxdeck.parsers.copilot_output_parser import CopilotEvidenceSpan

        with self.assertRaises(ValueError):
            CopilotEvidenceSpan(
                category="   ",
                start_line=1,
                end_line=1,
                text="t",
                confidence=Decimal("0.9"),
            )

    def test_empty_text_raises(self) -> None:
        from muxdeck.parsers.copilot_output_parser import CopilotEvidenceSpan

        with self.assertRaises(ValueError):
            CopilotEvidenceSpan(
                category="x",
                start_line=1,
                end_line=1,
                text="   ",
                confidence=Decimal("0.9"),
            )


class TaskEvidenceParserTests(unittest.TestCase):
    """Cover ``_parse_task_evidence`` and ``_detect_task_count_wrapped`` branches."""

    def test_detect_task_count_direct_pattern(self) -> None:
        from muxdeck.parsers.copilot_output_parser import _detect_task_count_wrapped

        assert _detect_task_count_wrapped("3 background /tasks") == 3
        assert _detect_task_count_wrapped("1 BACKGROUND /TASK") == 1

    def test_detect_task_count_line_wrapped_fallback(self) -> None:
        from muxdeck.parsers.copilot_output_parser import _detect_task_count_wrapped

        wrapped = "\n".join(
            (
                "5 backg",
                "round agents",
                "running",
                "/tasks",
            )
        )
        assert _detect_task_count_wrapped(wrapped) == 5

    def test_detect_task_count_no_match_returns_zero(self) -> None:
        from muxdeck.parsers.copilot_output_parser import _detect_task_count_wrapped

        assert _detect_task_count_wrapped("nothing relevant here") == 0
        # "5 backg" without a nearby "/tasks" stays at 0.
        assert _detect_task_count_wrapped("5 backgallery items") == 0

    def test_parse_task_evidence_collects_running_completed_idle(self) -> None:
        from muxdeck.parsers.copilot_output_parser import _parse_task_evidence

        output = "\n".join(
            (
                "● typeA(model-1) doing things",
                '● Background agent "typeB" (model-2) completed.',
                '● Background agent "typeC" (model-3) is idle',
            )
        )
        count, tasks = _parse_task_evidence(output)
        # Each pattern hit appends exactly one entry.
        statuses = sorted(t.status for t in tasks)
        assert "running" in statuses
        assert "completed" in statuses
        assert "idle" in statuses
        assert count == 0  # no "/tasks" marker → 0
        assert len(tasks) >= 3

    def test_parse_task_evidence_dedupes_by_label_model_keeping_latest(self) -> None:
        from muxdeck.parsers.copilot_output_parser import _parse_task_evidence

        # Same (label, model) seen twice — second entry should overwrite first.
        output = "\n".join(
            (
                "● typeA(model-1) running first",
                '● Background agent "typeA" (model-1) completed.',
            )
        )
        _, tasks = _parse_task_evidence(output)
        # Same key — only one survives.
        assert len([t for t in tasks if t.agent_type_label == "typeA"]) == 1
        # …and it's the later one (completed status).
        survivor = next(t for t in tasks if t.agent_type_label == "typeA")
        assert survivor.status == "completed"


if __name__ == "__main__":
    unittest.main()
