# ruff: noqa: I001,PT009,PT027

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
import json
import shutil
import unittest

from copilot_commander.adapters import DEFAULT_DATABASE_FILE_NAME, SQLiteStore
from copilot_commander.adapters.copilot_adapter import CopilotAdapter
from copilot_commander.config import AppConfig, CostingConfig, PathsConfig
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.models import Agent, Session
from copilot_commander.services.costing_service import CostingService


class CostingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_dir = Path(__file__).resolve().parent / "_runtime_costing_service" / self._testMethodName
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._cleanup_runtime_dir)
        self.config = AppConfig(
            paths=PathsConfig(
                state_dir=self.runtime_dir / "state",
                workspace_root=self.runtime_dir / "worktrees",
                database_path=self.runtime_dir / "state" / DEFAULT_DATABASE_FILE_NAME,
                fallback_database_path=self.runtime_dir / "legacy-state" / DEFAULT_DATABASE_FILE_NAME,
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

    def test_record_usage_evidence_preserves_raw_payload_and_aggregates_actual_and_estimated(self) -> None:
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


class _NoopRunner:
    def run(self, command: object, /, **kwargs: object) -> object:
        raise AssertionError(f"unexpected runner call: {command!r} {kwargs!r}")


if __name__ == "__main__":
    unittest.main()
