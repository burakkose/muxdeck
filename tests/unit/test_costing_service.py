# ruff: noqa: I001,PT009,PT027

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
import json
import shutil
import unittest

from muxdeck.adapters import DEFAULT_DATABASE_FILE_NAME, SQLiteStore
from muxdeck.adapters.copilot_adapter import CopilotAdapter
from muxdeck.config import AppConfig, CostingConfig, PathsConfig
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.models import Agent, Session
from muxdeck.domain.value_objects import CommandResult
from muxdeck.services.costing_service import CostingService
from muxdeck.types import JsonValue


class CostingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_dir = (
            Path(__file__).resolve().parent / "_runtime_costing_service" / self._testMethodName
        )
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._cleanup_runtime_dir)
        self.config = AppConfig(
            paths=PathsConfig(
                state_dir=self.runtime_dir / "state",
                workspace_root=self.runtime_dir / "worktrees",
                database_path=self.runtime_dir / "state" / DEFAULT_DATABASE_FILE_NAME,
                fallback_database_path=(
                    self.runtime_dir / "legacy-state" / DEFAULT_DATABASE_FILE_NAME
                ),
            ),
            costing=CostingConfig(
                default_input_token_cost_per_1m=2,
                default_output_token_cost_per_1m=4,
                estimation_enabled=True,
            ),
            config_file=self.runtime_dir / "config.toml",
        )
        self.store = SQLiteStore.from_config(self.config)
        self.addCleanup(self.store.close)
        self.store.upsert_agent(
            Agent(
                id="agent-123",
                name="planner",
                tmux_session_name="muxdeck",
                tmux_window_id="@1",
                tmux_window_name="main",
                tmux_pane_id="%1",
                pane_tty="/dev/pts/1",
                cwd="/repo",
                repo_root="/repo",
                worktree_path="/repo/worktrees/task",
                branch="task/costing",
                task_title="Costing",
                task_summary="Track usage",
                copilot_session_id="copilot-123",
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
        self.store.upsert_session(
            Session(
                id="session-123",
                agent_id="agent-123",
                copilot_session_id="copilot-123",
                task_title="Costing",
                created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            )
        )
        self.service = CostingService(config=self.config, store=self.store)
        self.adapter = CopilotAdapter(_NoopRunner())

    def _cleanup_runtime_dir(self) -> None:
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)

    def test_record_usage_evidence_preserves_raw_payload_and_aggregates_actual_and_estimated(
        self,
    ) -> None:
        evidence = self.adapter.interpret_output(
            "\n".join(
                (
                    "Copilot session id: session-01TEST",
                    "input_tokens: 1200",
                    "output_tokens: 300",
                    "estimated cost: $0.012345",
                )
            )
        )

        record = self.service.record_usage_evidence(
            "session-123",
            evidence,
            observed_at=datetime(2025, 1, 1, 12, 30, tzinfo=UTC),
        )
        session_summary = self.service.summarize_session("session-123")
        agent_summary = self.service.summarize_agent("agent-123")
        day_summary = self.service.summarize_day("2025-01-01")
        payload = json.loads(record.event.payload_json)

        self.assertEqual(payload["raw_evidence"]["copilot_session_id"], "session-01TEST")
        self.assertEqual(payload["derived_fact"]["total_tokens"], 1500)
        self.assertEqual(session_summary.evidence_count, 1)
        self.assertEqual(agent_summary.total_tokens, 1500)
        self.assertEqual(day_summary.actual_evidence_count, 1)
        self.assertEqual(day_summary.estimated_evidence_count, 1)
        self.assertEqual(
            [(bucket.currency, bucket.estimated) for bucket in session_summary.cost_buckets],
            [("USD", False), ("USD", True)],
        )

    def test_cost_bucket_total_cost_sums_input_and_output(self) -> None:
        from muxdeck.services.costing_service import CostBucket

        bucket = CostBucket(
            currency="USD",
            estimated=False,
            input_cost=Decimal("0.001"),
            output_cost=Decimal("0.002"),
        )
        self.assertEqual(bucket.total_cost, Decimal("0.003"))

    def test_record_usage_with_default_agent_id(self) -> None:
        """Test recording evidence falls back to session's agent_id when not provided."""
        evidence = self.adapter.interpret_output(
            "\n".join(
                (
                    "Copilot session id: custom-session",
                    "input_tokens: 100",
                    "output_tokens: 50",
                )
            )
        )

        record = self.service.record_usage_evidence(
            "session-123",
            evidence,
            observed_at=datetime(2025, 1, 1, 12, 30, tzinfo=UTC),
        )

        # Should use session's agent_id when not explicitly provided
        self.assertEqual(record.fact.agent_id, "agent-123")
        self.assertEqual(record.fact.input_tokens, 100)

    def test_summarize_session_with_empty_events(self) -> None:
        """Summarize session with no events should return zero aggregate."""
        summary = self.service.summarize_session("session-123")

        self.assertEqual(summary.evidence_count, 0)
        self.assertEqual(summary.input_tokens, 0)
        self.assertEqual(summary.output_tokens, 0)
        self.assertEqual(summary.total_tokens, 0)
        self.assertEqual(summary.cost_buckets, ())

    def test_summarize_agent(self) -> None:
        """Test summarizing costs by agent ID."""
        evidence = self.adapter.interpret_output("input_tokens: 500\noutput_tokens: 250")

        self.service.record_usage_evidence(
            "session-123",
            evidence,
            observed_at=datetime(2025, 1, 1, 12, 30, tzinfo=UTC),
        )

        summary = self.service.summarize_agent("agent-123")
        self.assertEqual(summary.input_tokens, 500)
        self.assertEqual(summary.output_tokens, 250)
        self.assertEqual(summary.total_tokens, 750)

    def test_summarize_day(self) -> None:
        """Test summarizing costs by day."""
        evidence = self.adapter.interpret_output("input_tokens: 200\noutput_tokens: 100")

        self.service.record_usage_evidence(
            "session-123",
            evidence,
            observed_at=datetime(2025, 1, 1, 12, 30, tzinfo=UTC),
        )

        summary = self.service.summarize_day("2025-01-01")
        self.assertEqual(summary.evidence_count, 1)
        self.assertEqual(summary.input_tokens, 200)
        self.assertEqual(summary.output_tokens, 100)

    def test_deserialize_fact_with_wrong_event_kind_returns_none(self) -> None:
        from muxdeck.domain.events import Event

        event = Event(
            occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            agent_id="agent-123",
            session_id="session-123",
            kind="other.event.kind",  # Not costing.usage_recorded
            payload_json="{}",
        )

        result = self.service._deserialize_fact(event)
        self.assertIsNone(result)

    def test_payload_to_bucket_with_invalid_data_returns_none(self) -> None:
        self.assertIsNone(self.service._payload_to_bucket(None))
        self.assertIsNone(self.service._payload_to_bucket("not_a_dict"))
        self.assertIsNone(self.service._payload_to_bucket({"currency": 123, "estimated": True}))
        self.assertIsNone(
            self.service._payload_to_bucket({"currency": "USD", "estimated": "not_bool"})
        )

    def test_payload_to_bucket_with_missing_costs_uses_zero(self) -> None:
        payload: JsonValue = {
            "currency": "USD",
            "estimated": True,
        }
        bucket = self.service._payload_to_bucket(payload)

        self.assertIsNotNone(bucket)
        assert bucket is not None
        self.assertEqual(bucket.input_cost, Decimal("0"))
        self.assertEqual(bucket.output_cost, Decimal("0"))

    def test_bucket_payload_returns_none_for_none_bucket(self) -> None:
        result = self.service._bucket_payload(None)
        self.assertIsNone(result)

    def test_normalize_day_from_date(self) -> None:
        from datetime import date

        d = date(2025, 1, 15)
        result = self.service._normalize_day(d)

        self.assertEqual(result, d)

    def test_normalize_day_from_datetime(self) -> None:
        dt = datetime(2025, 1, 15, 14, 30, tzinfo=UTC)
        result = self.service._normalize_day(dt)

        self.assertEqual(result.year, 2025)
        self.assertEqual(result.month, 1)
        self.assertEqual(result.day, 15)

    def test_normalize_day_from_iso_string(self) -> None:
        result = self.service._normalize_day("2025-01-15")

        self.assertEqual(result.year, 2025)
        self.assertEqual(result.month, 1)
        self.assertEqual(result.day, 15)

    def test_optional_int_returns_int_or_none(self) -> None:
        self.assertEqual(self.service._optional_int(42), 42)
        self.assertIsNone(self.service._optional_int("not_int"))
        self.assertIsNone(self.service._optional_int(None))
        self.assertIsNone(self.service._optional_int(3.14))


class _NoopRunner:
    def run(
        self,
        command: Sequence[str],
        /,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> CommandResult:
        del cwd, env, timeout_sec
        raise AssertionError(f"unexpected runner call: {tuple(command)!r}")


if __name__ == "__main__":
    unittest.main()
