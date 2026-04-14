# ruff: noqa: I001,PTH201,PT009,PT027

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import unittest

from copilot_commander.domain.enums import AgentStatus, EvidenceKind
from copilot_commander.domain.value_objects import (
    AgentId,
    CommandResult,
    CostBreakdown,
    ParserEvidence,
    TokenPricing,
    TokenUsage,
)
from copilot_commander.exceptions import DomainValidationError


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


if __name__ == "__main__":
    unittest.main()
