# ruff: noqa: I001,PTH201,PT009,PT027

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import unittest

from muxdeck.domain.enums import AgentStatus, EvidenceKind
from muxdeck.domain.value_objects import (
    AgentId,
    CommandResult,
    CostBreakdown,
    ParserEvidence,
    TokenPricing,
    TokenUsage,
)
from muxdeck.exceptions import DomainValidationError


class ValueObjectTests(unittest.TestCase):
    def test_agent_status_matches_psd(self) -> None:
        self.assertEqual(
            [status.value for status in AgentStatus],
            [
                "discovered",
                "starting",
                "running",
                "idle",
                "waiting_input",
                "blocked",
                "error",
                "completed",
                "dead",
                "unknown",
            ],
        )

    def test_token_usage_addition_and_totals(self) -> None:
        first = TokenUsage(input_tokens=10, output_tokens=20)
        second = TokenUsage(input_tokens=5, output_tokens=7)

        combined = first + second

        self.assertEqual(combined.input_tokens, 15)
        self.assertEqual(combined.output_tokens, 27)
        self.assertEqual(combined.total_tokens, 42)

    def test_token_pricing_estimates_cost_by_bucket(self) -> None:
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=500_000)
        pricing = TokenPricing(input_token_cost_per_1m="1.25", output_token_cost_per_1m="10")

        cost = pricing.estimate_cost(usage)

        self.assertEqual(cost.input_cost, Decimal("1.250000"))
        self.assertEqual(cost.output_cost, Decimal("5.000000"))
        self.assertEqual(cost.total_cost, Decimal("6.250000"))
        self.assertTrue(cost.estimated)

    def test_cost_breakdown_rejects_currency_mismatch_on_addition(self) -> None:
        with self.assertRaises(DomainValidationError):
            _ = CostBreakdown(currency="USD") + CostBreakdown(currency="EUR")

    def test_parser_evidence_validates_kind_specific_payloads(self) -> None:
        observed_at = datetime(2025, 1, 1, tzinfo=UTC)
        evidence = ParserEvidence(
            source="tmux-capture",
            kind=EvidenceKind.RAW,
            raw_value={"line": "status: running"},
            confidence="0.8",
            observed_at=observed_at,
        )

        self.assertEqual(evidence.confidence, Decimal("0.8000"))
        self.assertEqual(evidence.observed_at, observed_at)
        with self.assertRaises(DomainValidationError):
            ParserEvidence(source="parser", kind=EvidenceKind.DERIVED)
        with self.assertRaises(DomainValidationError):
            ParserEvidence(
                source="parser",
                kind=EvidenceKind.RAW,
                raw_value={"ok": True},
                derived_value={"bad": True},
            )

    def test_command_result_reports_success_and_normalizes_cwd(self) -> None:
        started_at = datetime(2025, 1, 1, tzinfo=UTC)
        result = CommandResult(
            command=("git", "status"),
            exit_code=0,
            started_at=started_at,
            finished_at=started_at + timedelta(seconds=2),
            cwd=Path("."),
        )

        self.assertTrue(result.succeeded)
        self.assertEqual(result.cwd, Path(".").resolve())
        self.assertEqual(str(AgentId.generate()).split("-", maxsplit=1)[0], "agent")


class EnsureValidatorTests(unittest.TestCase):
    def test_ensure_aware_datetime_rejects_naive(self) -> None:
        from muxdeck.domain.value_objects import ensure_aware_datetime

        with self.assertRaises(DomainValidationError):
            ensure_aware_datetime(datetime(2025, 1, 1), field_name="when")

    def test_ensure_aware_datetime_normalizes_to_utc(self) -> None:
        from datetime import timezone as dt_timezone

        from muxdeck.domain.value_objects import ensure_aware_datetime

        ist = dt_timezone(timedelta(hours=3))
        normalized = ensure_aware_datetime(datetime(2025, 1, 1, 12, tzinfo=ist), field_name="when")
        self.assertEqual(normalized.tzinfo, UTC)
        self.assertEqual(normalized.hour, 9)

    def test_ensure_non_empty_text_rejects_blank_strings(self) -> None:
        from muxdeck.domain.value_objects import ensure_non_empty_text

        with self.assertRaises(DomainValidationError):
            ensure_non_empty_text("   ", field_name="name")
        self.assertEqual(ensure_non_empty_text("  hi  ", field_name="name"), "hi")

    def test_ensure_non_negative_int_rejects_bool_and_negatives(self) -> None:
        from muxdeck.domain.value_objects import ensure_non_negative_int

        with self.assertRaises(DomainValidationError):
            ensure_non_negative_int(True, field_name="n")  # type: ignore[arg-type]
        with self.assertRaises(DomainValidationError):
            ensure_non_negative_int(-1, field_name="n")
        self.assertEqual(ensure_non_negative_int(0, field_name="n"), 0)

    def test_ensure_non_negative_decimal_rejects_invalid_and_negative(self) -> None:
        from muxdeck.domain.value_objects import ensure_non_negative_decimal

        with self.assertRaises(DomainValidationError):
            ensure_non_negative_decimal("nope", field_name="d")
        with self.assertRaises(DomainValidationError):
            ensure_non_negative_decimal("-0.01", field_name="d")
        # quantize=None preserves precision.
        result = ensure_non_negative_decimal("1.234567890", field_name="d", quantize=None)
        self.assertEqual(result, Decimal("1.234567890"))

    def test_ensure_confidence_rejects_above_one(self) -> None:
        from muxdeck.domain.value_objects import ensure_confidence

        with self.assertRaises(DomainValidationError):
            ensure_confidence("1.5")
        self.assertEqual(ensure_confidence("0.5"), Decimal("0.5000"))


class IdConverterTests(unittest.TestCase):
    def test_ensure_agent_id_passthrough_and_string(self) -> None:
        from muxdeck.domain.value_objects import ensure_agent_id

        existing = AgentId(value="agent-1")
        self.assertIs(ensure_agent_id(existing), existing)
        new_id = ensure_agent_id("agent-from-str")
        self.assertEqual(new_id.value, "agent-from-str")

    def test_ensure_worktree_id_passthrough_and_string(self) -> None:
        from muxdeck.domain.value_objects import WorktreeId, ensure_worktree_id

        existing = WorktreeId(value="wt-1")
        self.assertIs(ensure_worktree_id(existing), existing)
        new_id = ensure_worktree_id("wt-from-str")
        self.assertEqual(new_id.value, "wt-from-str")

    def test_ensure_session_id_passthrough_and_string(self) -> None:
        from muxdeck.domain.value_objects import SessionId, ensure_session_id

        existing = SessionId(value="sess-1")
        self.assertIs(ensure_session_id(existing), existing)
        new_id = ensure_session_id("sess-from-str")
        self.assertEqual(new_id.value, "sess-from-str")

    def test_ensure_event_id_passthrough_and_string(self) -> None:
        from muxdeck.domain.value_objects import EventId, ensure_event_id

        existing = EventId(value="evt-1")
        self.assertIs(ensure_event_id(existing), existing)
        new_id = ensure_event_id("evt-from-str")
        self.assertEqual(new_id.value, "evt-from-str")

    def test_ensure_log_chunk_id_passthrough_and_string(self) -> None:
        from muxdeck.domain.value_objects import LogChunkId, ensure_log_chunk_id

        existing = LogChunkId(value="log-1")
        self.assertIs(ensure_log_chunk_id(existing), existing)
        new_id = ensure_log_chunk_id("log-from-str")
        self.assertEqual(new_id.value, "log-from-str")

    def test_id_str_returns_value(self) -> None:
        from muxdeck.domain.value_objects import (
            EventId,
            LogChunkId,
            SessionId,
            WorktreeId,
        )

        self.assertEqual(str(AgentId(value="agent-x")), "agent-x")
        self.assertEqual(str(WorktreeId(value="wt-x")), "wt-x")
        self.assertEqual(str(SessionId(value="s-x")), "s-x")
        self.assertEqual(str(EventId(value="e-x")), "e-x")
        self.assertEqual(str(LogChunkId(value="l-x")), "l-x")

    def test_id_generate_yields_unique_prefixed_values(self) -> None:
        from muxdeck.domain.value_objects import (
            EventId,
            LogChunkId,
            SessionId,
            WorktreeId,
        )

        for cls, prefix in (
            (AgentId, "agent"),
            (WorktreeId, "worktree"),
            (SessionId, "session"),
            (EventId, "event"),
            (LogChunkId, "logchunk"),
        ):
            generated = cls.generate()
            self.assertTrue(generated.value.startswith(f"{prefix}-"))


class TokenUsageAdditionalTests(unittest.TestCase):
    def test_token_usage_addition_with_non_token_usage_returns_not_implemented(self) -> None:
        usage = TokenUsage(input_tokens=1, output_tokens=2)
        # __add__ returning NotImplemented means Python falls back to TypeError.
        with self.assertRaises(TypeError):
            _ = usage + "not-a-usage"  # type: ignore[operator]

    def test_token_usage_rejects_negative(self) -> None:
        with self.assertRaises(DomainValidationError):
            TokenUsage(input_tokens=-1)


class CostBreakdownAdditionalTests(unittest.TestCase):
    def test_cost_breakdown_addition_with_non_breakdown_returns_not_implemented(self) -> None:
        cb = CostBreakdown(currency="USD")
        with self.assertRaises(TypeError):
            _ = cb + "not-a-breakdown"  # type: ignore[operator]

    def test_cost_breakdown_addition_combines_values_and_estimated_flag(self) -> None:
        a = CostBreakdown(input_cost="1.0", output_cost="2.0", currency="USD", estimated=False)
        b = CostBreakdown(input_cost="0.5", output_cost="3.0", currency="USD", estimated=True)
        c = a + b
        self.assertEqual(c.input_cost, Decimal("1.500000"))
        self.assertEqual(c.output_cost, Decimal("5.000000"))
        # estimated requires both ends to be estimated.
        self.assertFalse(c.estimated)


class ParserEvidenceAdditionalTests(unittest.TestCase):
    def test_parser_evidence_strips_blank_summary(self) -> None:
        with self.assertRaises(DomainValidationError):
            ParserEvidence(
                source="parser",
                kind=EvidenceKind.RAW,
                raw_value={"x": 1},
                summary="   ",  # blank → ensure_non_empty_text raises
            )

    def test_parser_evidence_derived_requires_value_or_summary(self) -> None:
        with self.assertRaises(DomainValidationError):
            ParserEvidence(source="parser", kind=EvidenceKind.DERIVED)

    def test_parser_evidence_derived_rejects_raw_value(self) -> None:
        with self.assertRaises(DomainValidationError):
            ParserEvidence(
                source="parser",
                kind=EvidenceKind.DERIVED,
                derived_value={"x": 1},
                raw_value={"y": 2},
            )

    def test_parser_evidence_derived_with_summary_only_is_ok(self) -> None:
        evidence = ParserEvidence(
            source="parser",
            kind=EvidenceKind.DERIVED,
            summary="ok",
        )
        self.assertEqual(evidence.summary, "ok")


class CommandResultAdditionalTests(unittest.TestCase):
    def test_command_result_rejects_empty_command(self) -> None:
        started_at = datetime(2025, 1, 1, tzinfo=UTC)
        with self.assertRaises(DomainValidationError):
            CommandResult(
                command=(),
                exit_code=0,
                started_at=started_at,
                finished_at=started_at,
            )

    def test_command_result_rejects_finished_before_started(self) -> None:
        started_at = datetime(2025, 1, 1, tzinfo=UTC)
        with self.assertRaises(DomainValidationError):
            CommandResult(
                command=("ls",),
                exit_code=0,
                started_at=started_at,
                finished_at=started_at - timedelta(seconds=1),
            )

    def test_command_result_succeeded_false_for_nonzero_exit(self) -> None:
        started_at = datetime(2025, 1, 1, tzinfo=UTC)
        result = CommandResult(
            command=("ls",),
            exit_code=1,
            started_at=started_at,
            finished_at=started_at + timedelta(seconds=1),
        )
        self.assertFalse(result.succeeded)


if __name__ == "__main__":
    unittest.main()
