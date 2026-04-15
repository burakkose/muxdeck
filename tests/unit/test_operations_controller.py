# ruff: noqa: E402,I001

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import sys
from pathlib import Path
from typing import cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from copilot_commander.controllers import (
    AgentActionResult,
    AgentIntentView,
    AgentTargetView,
    DashboardAgentListItemView,
    DashboardAlertView,
    DashboardFilterState,
    DashboardHealthSummary,
    OperationsActionPort,
    DashboardSort,
    DashboardState,
    OperationsAction,
    OperationsController,
)
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.exceptions import PersistenceError
from copilot_commander.services import OperationAuditService

_TS = datetime(2025, 1, 1, 12, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _FakeOperationResult:
    success: bool
    message: str


class _FakeDashboardController:
    def __init__(self) -> None:
        self.agents = (
            DashboardAgentListItemView(
                agent_id="agent-1",
                name="Planner",
                status=AgentStatus.RUNNING,
                repo_name="repo",
                branch="feat/planner",
                worktree_name="planner",
                pane_id="%1",
                task_title="Plan",
                worktree_path="/repo/planner",
                latest_session_id="session-1",
                last_event_kind="agent.updated",
                last_log_at=_TS,
                last_seen_at=_TS,
                started_at=_TS,
                idle_seconds=5,
                needs_attention=False,
                attention_reason=None,
                token_total=10,
                estimated_cost_usd="0.10",
            ),
            DashboardAgentListItemView(
                agent_id="agent-2",
                name="Reviewer",
                status=AgentStatus.WAITING_INPUT,
                repo_name="repo",
                branch="feat/reviewer",
                worktree_name="reviewer",
                pane_id="%2",
                task_title="Review",
                worktree_path="/repo/reviewer",
                latest_session_id="session-2",
                last_event_kind="agent.blocked",
                last_log_at=_TS,
                last_seen_at=_TS,
                started_at=_TS,
                idle_seconds=45,
                needs_attention=True,
                attention_reason="needs operator input",
                token_total=20,
                estimated_cost_usd="0.20",
            ),
        )

    def build_state(
        self,
        *,
        filters: DashboardFilterState | None = None,
        sort: DashboardSort | None = None,
        selected_agent_id: str | None = None,
        preview_line_limit: int = 8,
        alert_limit: int = 5,
    ) -> DashboardState:
        del filters, sort, selected_agent_id, preview_line_limit, alert_limit
        return DashboardState(
            generated_at=_TS,
            metrics=(),
            filters=DashboardFilterState(),
            sort=DashboardSort(),
            health=DashboardHealthSummary(
                tone="warning",
                message="some agents need review",
                total_agents=2,
                active_agents=2,
                attention_agents=1,
                waiting_input_agents=1,
                blocked_agents=0,
                error_agents=0,
            ),
            alerts=(
                DashboardAlertView(
                    agent_id="agent-2",
                    agent_name="Reviewer",
                    severity="warning",
                    title="waiting_input",
                    message="needs operator input",
                    occurred_at=_TS,
                ),
            ),
            agents=self.agents,
            selected_agent_id=None,
            selected_agent=None,
        )


class _FakeAgentController:
    def interrupt_intent(self, agent_id: str) -> AgentIntentView:
        return AgentIntentView(
            kind="interrupt",
            agent=AgentTargetView(
                agent_id=agent_id,
                name=agent_id,
                status=AgentStatus.RUNNING,
                pane_target=f"%{agent_id[-1]}",
                worktree_path="/repo",
                repo_root="/repo",
                branch="main",
                latest_session_id="session-1",
            ),
            label="Interrupt",
        )

    open_pane_intent = interrupt_intent
    open_worktree_intent = interrupt_intent

    def mark_complete(
        self,
        agent_id: str,
        *,
        exit_reason: str = "marked_complete",
    ) -> AgentActionResult:
        del exit_reason
        return AgentActionResult(
            action="mark_complete",
            agent=AgentTargetView(
                agent_id=agent_id,
                name=f"name-{agent_id}",
                status=AgentStatus.RUNNING,
                pane_target=f"%{agent_id[-1]}",
                worktree_path="/repo",
                repo_root="/repo",
                branch="main",
                latest_session_id="session-1",
            ),
            session_id="session-1",
            session_ended=True,
        )


class _FakeActionService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def execute_intents(
        self,
        intents: Sequence[AgentIntentView],
    ) -> tuple[_FakeOperationResult, ...]:
        self.calls.append(tuple(intent.agent.pane_target for intent in intents))
        return tuple(
            _FakeOperationResult(success=True, message=f"executed {intent.agent.pane_target}")
            for intent in intents
        )


def test_preview_action_requires_confirmation_for_interrupt() -> None:
    controller = OperationsController(
        _FakeDashboardController(),
        _FakeAgentController(),
        OperationAuditService(),
        actions=cast(OperationsActionPort, _FakeActionService()),
        clock=lambda: _TS,
    )

    preview = controller.preview_action(OperationsAction.INTERRUPT, ("agent-1", "agent-2"))

    assert preview.requires_confirmation is True
    assert preview.summary == "Interrupt 2 agents"
    assert [target.agent_id for target in preview.targets] == ["agent-1", "agent-2"]


def test_execute_preview_records_audit_entries() -> None:
    audit = OperationAuditService()
    action_service = _FakeActionService()
    controller = OperationsController(
        _FakeDashboardController(),
        _FakeAgentController(),
        audit,
        actions=cast(OperationsActionPort, action_service),
        clock=lambda: _TS,
    )
    preview = controller.preview_action(OperationsAction.OPEN_PANE, ("agent-1",))

    result = controller.execute_preview(preview)

    assert result.success_count == 1
    assert result.failure_count == 0
    assert action_service.calls == [("%1",)]
    history = audit.list_entries(limit=5)
    assert len(history) == 1
    assert history[0].action == "open_pane"
    assert history[0].message == "executed %1"


def test_execute_mark_complete_uses_agent_controller() -> None:
    audit = OperationAuditService()
    controller = OperationsController(
        _FakeDashboardController(),
        _FakeAgentController(),
        audit,
        actions=None,
        clock=lambda: _TS,
    )
    preview = controller.preview_action(OperationsAction.MARK_COMPLETE, ("agent-2",))

    result = controller.execute_preview(preview)

    assert result.status_message == "Mark complete completed for 1 agent(s)"
    [entry] = audit.list_entries(limit=5)
    assert entry.action == "mark_complete"
    assert entry.message == "mark_complete name-agent-2 session session-1 ended=True"


def test_preview_action_requires_selection() -> None:
    controller = OperationsController(
        _FakeDashboardController(),
        _FakeAgentController(),
        OperationAuditService(),
        actions=None,
        clock=lambda: _TS,
    )

    with pytest.raises(PersistenceError) as exc_info:
        controller.preview_action(OperationsAction.OPEN_PANE, ())

    assert "select at least one agent" in str(exc_info.value)
