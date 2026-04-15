# ruff: noqa: E402,I001

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from textual.app import App

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

if TYPE_CHECKING:
    from copilot_commander.app import CommanderRuntime

from copilot_commander.controllers import (
    DashboardAgentListItemView,
    DashboardAlertView,
    DashboardHealthSummary,
    OperationsAction,
    OperationsActionPreview,
    OperationsExecutionSummary,
    OperationsState,
)
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.screens.confirm_dialog import ConfirmScreen
from copilot_commander.screens.operations import OperationsScreen
from copilot_commander.widgets.common import KeyHintFooter

_TS = datetime(2025, 1, 1, 12, tzinfo=UTC)


def _agent(agent_id: str, *, name: str) -> DashboardAgentListItemView:
    return DashboardAgentListItemView(
        agent_id=agent_id,
        name=name,
        status=AgentStatus.RUNNING,
        repo_name="repo",
        branch="main",
        worktree_name="wt",
        pane_id=f"%{agent_id[-1]}",
        task_title="task",
        worktree_path="/repo/wt",
        latest_session_id="session-1",
        last_event_kind="agent.updated",
        last_log_at=_TS,
        last_seen_at=_TS,
        started_at=_TS,
        idle_seconds=3,
        needs_attention=False,
        attention_reason=None,
        token_total=10,
        estimated_cost_usd="0.10",
    )


class _FakeRuntime:
    class Config:
        class General:
            log_preview_lines = 8

        general = General()

    config = Config()


class _FakeOperationsController:
    def __init__(self) -> None:
        self.preview_calls: list[tuple[OperationsAction, tuple[str, ...]]] = []
        self.execute_calls: list[OperationsActionPreview] = []

    def build_state(
        self,
        *,
        selected_agent_ids: tuple[str, ...] = (),
        preview: OperationsActionPreview | None = None,
        preview_line_limit: int = 6,
        alert_limit: int = 6,
        history_limit: int = 12,
    ) -> OperationsState:
        del preview_line_limit, alert_limit, history_limit
        agents = (_agent("agent-1", name="Planner"), _agent("agent-2", name="Reviewer"))
        valid_selected = tuple(
            agent_id for agent_id in selected_agent_ids if agent_id in {"agent-1", "agent-2"}
        )
        return OperationsState(
            generated_at=_TS,
            health=DashboardHealthSummary(
                tone="healthy",
                message="ok",
                total_agents=2,
                active_agents=2,
                attention_agents=0,
                waiting_input_agents=0,
                blocked_agents=0,
                error_agents=0,
            ),
            alerts=(
                DashboardAlertView(
                    agent_id="agent-2",
                    agent_name="Reviewer",
                    severity="warning",
                    title="review",
                    message="needs review",
                    occurred_at=_TS,
                ),
            ),
            agents=agents,
            selected_agent_ids=valid_selected,
            preview=preview,
            history=(),
        )

    def toggle_selection(
        self,
        selected_agent_ids: tuple[str, ...],
        agent_id: str,
    ) -> tuple[str, ...]:
        return (*selected_agent_ids, agent_id) if agent_id not in selected_agent_ids else ()

    def select_all(self, agents: Sequence[object]) -> tuple[str, ...]:
        return tuple(
            agent.agent_id for agent in agents if isinstance(agent, DashboardAgentListItemView)
        )

    def clear_selection(self) -> tuple[str, ...]:
        return ()

    def preview_action(
        self,
        action: OperationsAction,
        selected_agent_ids: tuple[str, ...],
    ) -> OperationsActionPreview:
        self.preview_calls.append((action, selected_agent_ids))
        agents = tuple(
            _agent(agent_id, name=f"Agent {agent_id[-1]}") for agent_id in selected_agent_ids
        )
        return OperationsActionPreview(
            action=action,
            label="Focus pane" if action is OperationsAction.OPEN_PANE else "Interrupt",
            summary=f"{action.value} {len(selected_agent_ids)}",
            confirmation_message="confirm",
            selected_agent_ids=selected_agent_ids,
            targets=agents,
            requires_confirmation=action is OperationsAction.INTERRUPT,
        )

    def execute_preview(self, preview: OperationsActionPreview) -> OperationsExecutionSummary:
        self.execute_calls.append(preview)
        return OperationsExecutionSummary(
            preview=preview,
            entries=(),
            success_count=len(preview.targets),
            failure_count=0,
            status_message="executed",
        )


class _OperationsApp(App[None]):
    def __init__(self, controller: _FakeOperationsController) -> None:
        super().__init__()
        self._screen = OperationsScreen(cast("CommanderRuntime", _FakeRuntime()), controller)

    def on_mount(self) -> None:
        self.push_screen(self._screen)


@pytest.mark.asyncio
async def test_operations_screen_can_preview_and_execute_non_confirmed_action() -> None:
    controller = _FakeOperationsController()
    app = _OperationsApp(controller)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()

        assert controller.preview_calls == [(OperationsAction.OPEN_PANE, ("agent-1",))]
        assert len(controller.execute_calls) == 1
        footer = cast(KeyHintFooter, app.screen.query_one("#shell-footer"))
        assert footer.status == "executed"


@pytest.mark.asyncio
async def test_operations_screen_prompts_for_confirmed_action() -> None:
    controller = _FakeOperationsController()
    app = _OperationsApp(controller)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("space", "j", "space")
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()

        assert controller.preview_calls[-1] == (OperationsAction.INTERRUPT, ("agent-1", "agent-2"))
        assert isinstance(app.screen_stack[-1], ConfirmScreen)
