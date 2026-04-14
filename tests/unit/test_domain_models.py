# ruff: noqa: E501,I001,PT009,PT027

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, get_type_hints
import unittest

from copilot_commander import Agent as ExportedAgent
from copilot_commander import Event as ExportedEvent
from copilot_commander import LogChunk as ExportedLogChunk
from copilot_commander import Session as ExportedSession
from copilot_commander import Worktree as ExportedWorktree
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.events import Event, LogChunk
from copilot_commander.domain.models import Agent, Session, Worktree
from copilot_commander.exceptions import DomainValidationError


class DomainModelTests(unittest.TestCase):
    def test_agent_matches_psd_contract_and_normalizes_values(self) -> None:
        expected_fields = (
            "id",
            "name",
            "backend",
            "tmux_session_name",
            "tmux_window_id",
            "tmux_window_name",
            "tmux_pane_id",
            "pane_tty",
            "cwd",
            "repo_root",
            "worktree_path",
            "branch",
            "task_title",
            "task_summary",
            "copilot_session_id",
            "pid",
            "status",
            "started_at",
            "last_activity_at",
            "last_seen_at",
            "idle_seconds",
            "needs_attention",
            "attention_reason",
            "token_input",
            "token_output",
            "token_total",
            "estimated_cost_usd",
        )
        self.assertEqual(tuple(field.name for field in fields(Agent)), expected_fields)
        hints = get_type_hints(Agent)
        self.assertEqual(hints["backend"], Literal["copilot_cli"])
        self.assertEqual(hints["estimated_cost_usd"], Decimal | None)

        started_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        agent = Agent(
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
            branch="task/domain-exactness-alignment",
            task_title="Domain exactness",
            task_summary="Align public contracts",
            copilot_session_id="copilot-123",
            pid=4321,
            status=AgentStatus.RUNNING,
            started_at=started_at,
            last_activity_at=started_at + timedelta(seconds=30),
            last_seen_at=started_at + timedelta(seconds=60),
            idle_seconds=12,
            needs_attention=True,
            attention_reason="parser stalled",
            token_input=10,
            token_output=20,
            token_total=30,
            estimated_cost_usd=Decimal("1.25"),
        )

        self.assertEqual(agent.backend, "copilot_cli")
        self.assertIsInstance(agent.id, str)
        self.assertEqual(agent.estimated_cost_usd, Decimal("1.25"))
        self.assertEqual(agent.started_at.tzinfo, UTC)
        self.assertEqual(agent.last_seen_at.tzinfo, UTC)

    def test_agent_rejects_inconsistent_token_totals_and_attention_reason_without_flag(self) -> None:
        now = datetime(2025, 1, 1, tzinfo=UTC)
        with self.assertRaises(DomainValidationError):
            Agent(
                id="agent-123",
                name="planner",
                tmux_session_name="muxdeck",
                tmux_window_id="@1",
                tmux_pane_id="%1",
                cwd="/repo",
                status=AgentStatus.RUNNING,
                started_at=now,
                last_seen_at=now,
                token_input=10,
                token_output=20,
                token_total=31,
            )
        with self.assertRaises(DomainValidationError):
            Agent(
                id="agent-123",
                name="planner",
                tmux_session_name="muxdeck",
                tmux_window_id="@1",
                tmux_pane_id="%1",
                cwd="/repo",
                status=AgentStatus.RUNNING,
                started_at=now,
                last_seen_at=now,
                attention_reason="unexpected",
            )

    def test_worktree_and_session_match_psd_contracts(self) -> None:
        self.assertEqual(
            tuple(field.name for field in fields(Worktree)),
            (
                "id",
                "repo_root",
                "path",
                "branch",
                "base_branch",
                "is_main_worktree",
                "is_dirty",
                "ahead_count",
                "behind_count",
                "locked",
                "assigned_agent_id",
                "created_at",
                "last_seen_at",
            ),
        )
        self.assertEqual(
            tuple(field.name for field in fields(Session)),
            ("id", "agent_id", "copilot_session_id", "task_title", "created_at", "ended_at", "exit_reason"),
        )

        created_at = datetime(2025, 1, 1, tzinfo=UTC)
        worktree = Worktree(
            id="worktree-123",
            repo_root="/repo",
            path="/repo/worktrees/task",
            branch="task/domain-exactness-alignment",
            base_branch="main",
            is_main_worktree=False,
            is_dirty=True,
            ahead_count=2,
            behind_count=1,
            locked=True,
            assigned_agent_id="agent-123",
            created_at=created_at,
            last_seen_at=created_at + timedelta(seconds=5),
        )
        session = Session(
            id="session-123",
            agent_id="agent-123",
            copilot_session_id="copilot-123",
            task_title="Domain exactness",
            created_at=created_at,
            ended_at=created_at + timedelta(minutes=1),
            exit_reason="completed",
        )

        self.assertEqual(worktree.assigned_agent_id, "agent-123")
        self.assertEqual(session.copilot_session_id, "copilot-123")
        self.assertEqual(session.created_at.tzinfo, UTC)
        assert session.ended_at is not None
        self.assertEqual(session.ended_at.tzinfo, UTC)

    def test_top_level_exports_remain_coherent(self) -> None:
        self.assertIs(ExportedAgent, Agent)
        self.assertIs(ExportedWorktree, Worktree)
        self.assertIs(ExportedSession, Session)
        self.assertIs(ExportedEvent, Event)
        self.assertIs(ExportedLogChunk, LogChunk)


if __name__ == "__main__":
    unittest.main()
