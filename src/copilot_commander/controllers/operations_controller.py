from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from copilot_commander.controllers.agent_controller import (
    AgentActionResult,
    AgentIntentView,
)
from copilot_commander.controllers.dashboard_controller import (
    DashboardAgentListItemView,
    DashboardAlertView,
    DashboardFilterState,
    DashboardHealthSummary,
    DashboardSort,
    DashboardState,
)
from copilot_commander.domain.value_objects import utc_now
from copilot_commander.exceptions import PersistenceError
from copilot_commander.services.operations_service import OperationAuditEntry
from copilot_commander.types import Clock


class OperationsAction(StrEnum):
    INTERRUPT = "interrupt"
    MARK_COMPLETE = "mark_complete"
    OPEN_PANE = "open_pane"
    OPEN_WORKTREE = "open_worktree"


@dataclass(frozen=True, slots=True)
class OperationsActionPreview:
    action: OperationsAction
    label: str
    summary: str
    confirmation_message: str
    selected_agent_ids: tuple[str, ...]
    targets: tuple[DashboardAgentListItemView, ...]
    requires_confirmation: bool


@dataclass(frozen=True, slots=True)
class OperationsExecutionSummary:
    preview: OperationsActionPreview
    entries: tuple[OperationAuditEntry, ...]
    success_count: int
    failure_count: int
    status_message: str


@dataclass(frozen=True, slots=True)
class OperationsState:
    generated_at: datetime
    health: DashboardHealthSummary
    alerts: tuple[DashboardAlertView, ...]
    agents: tuple[DashboardAgentListItemView, ...]
    selected_agent_ids: tuple[str, ...]
    preview: OperationsActionPreview | None
    history: tuple[OperationAuditEntry, ...]


class OperationAuditPort(Protocol):
    def record_batch(self, entries: Sequence[OperationAuditEntry]) -> None: ...

    def list_entries(self, *, limit: int = 20) -> Sequence[OperationAuditEntry]: ...


class OperationsDashboardPort(Protocol):
    def build_state(
        self,
        *,
        filters: DashboardFilterState | None = None,
        sort: DashboardSort | None = None,
        selected_agent_id: str | None = None,
        preview_line_limit: int = 8,
        alert_limit: int = 5,
    ) -> DashboardState: ...


class OperationsAgentPort(Protocol):
    def interrupt_intent(self, agent_id: str) -> AgentIntentView: ...

    def mark_complete(
        self,
        agent_id: str,
        *,
        exit_reason: str = "marked_complete",
    ) -> AgentActionResult: ...

    def open_pane_intent(self, agent_id: str) -> AgentIntentView: ...

    def open_worktree_intent(self, agent_id: str) -> AgentIntentView: ...


class OperationsActionPort(Protocol):
    def execute_intents(
        self,
        intents: Sequence[AgentIntentView],
    ) -> Sequence[OperationResultPort]: ...


class OperationResultPort(Protocol):
    @property
    def success(self) -> bool: ...

    @property
    def message(self) -> str: ...


class OperationsController:
    """Application-layer orchestration for bulk operator workflows."""

    def __init__(
        self,
        dashboard: OperationsDashboardPort,
        agents: OperationsAgentPort,
        audit: OperationAuditPort,
        *,
        actions: OperationsActionPort | None = None,
        clock: Clock = utc_now,
        filters: DashboardFilterState | None = None,
        sort: DashboardSort | None = None,
    ) -> None:
        self._dashboard = dashboard
        self._agents = agents
        self._audit = audit
        self._actions = actions
        self._clock = clock
        self._filters = (
            DashboardFilterState(include_completed=False) if filters is None else filters
        )
        self._sort = DashboardSort(field="last_seen", descending=True) if sort is None else sort

    def build_state(
        self,
        *,
        selected_agent_ids: Sequence[str] = (),
        preview: OperationsActionPreview | None = None,
        preview_line_limit: int = 6,
        alert_limit: int = 6,
        history_limit: int = 12,
    ) -> OperationsState:
        dashboard_state = self._dashboard.build_state(
            filters=self._filters,
            sort=self._sort,
            selected_agent_id=next(iter(selected_agent_ids), None),
            preview_line_limit=preview_line_limit,
            alert_limit=alert_limit,
        )
        resolved_selection = tuple(
            agent.agent_id
            for agent in dashboard_state.agents
            if agent.agent_id in set(selected_agent_ids)
        )
        resolved_preview = preview
        if preview is not None and any(
            agent_id not in set(resolved_selection) for agent_id in preview.selected_agent_ids
        ):
            resolved_preview = None
        return OperationsState(
            generated_at=dashboard_state.generated_at,
            health=dashboard_state.health,
            alerts=dashboard_state.alerts,
            agents=dashboard_state.agents,
            selected_agent_ids=resolved_selection,
            preview=resolved_preview,
            history=tuple(self._audit.list_entries(limit=history_limit)),
        )

    def toggle_selection(
        self,
        selected_agent_ids: Sequence[str],
        agent_id: str,
    ) -> tuple[str, ...]:
        current = tuple(selected_agent_ids)
        if agent_id in current:
            return tuple(candidate for candidate in current if candidate != agent_id)
        return (*current, agent_id)

    def clear_selection(self) -> tuple[str, ...]:
        return ()

    def select_all(self, agents: Sequence[DashboardAgentListItemView]) -> tuple[str, ...]:
        return tuple(agent.agent_id for agent in agents)

    def preview_action(
        self,
        action: OperationsAction,
        selected_agent_ids: Sequence[str],
    ) -> OperationsActionPreview:
        targets = self._resolve_targets(selected_agent_ids)
        count = len(targets)
        label = _ACTION_LABELS[action]
        summary = f"{label} {count} agent{'s' if count != 1 else ''}"
        agent_names = ", ".join(target.name for target in targets)
        return OperationsActionPreview(
            action=action,
            label=label,
            summary=summary,
            confirmation_message=f"{label} {count} agent{'s' if count != 1 else ''}? {agent_names}",
            selected_agent_ids=tuple(target.agent_id for target in targets),
            targets=targets,
            requires_confirmation=count > 1 or action in _DESTRUCTIVE_ACTIONS,
        )

    def execute_preview(self, preview: OperationsActionPreview) -> OperationsExecutionSummary:
        entries: tuple[OperationAuditEntry, ...]
        match preview.action:
            case OperationsAction.MARK_COMPLETE:
                entries = tuple(self._execute_mark_complete(preview.targets))
            case _:
                entries = tuple(self._execute_tmux_action(preview))
        self._audit.record_batch(entries)
        success_count = sum(1 for entry in entries if entry.success)
        failure_count = len(entries) - success_count
        if failure_count:
            status_message = f"{preview.label} finished with {success_count}/{len(entries)} success"
        else:
            status_message = f"{preview.label} completed for {success_count} agent(s)"
        return OperationsExecutionSummary(
            preview=preview,
            entries=entries,
            success_count=success_count,
            failure_count=failure_count,
            status_message=status_message,
        )

    def _resolve_targets(
        self,
        selected_agent_ids: Sequence[str],
    ) -> tuple[DashboardAgentListItemView, ...]:
        state = self.build_state(selected_agent_ids=selected_agent_ids)
        targets = tuple(
            agent for agent in state.agents if agent.agent_id in set(state.selected_agent_ids)
        )
        if not targets:
            msg = "select at least one agent"
            raise PersistenceError(msg)
        return targets

    def _execute_tmux_action(
        self,
        preview: OperationsActionPreview,
    ) -> Sequence[OperationAuditEntry]:
        if self._actions is None:
            msg = "action service unavailable"
            raise RuntimeError(msg)
        intents = tuple(
            self._intent_for_action(preview.action, target.agent_id) for target in preview.targets
        )
        results = self._actions.execute_intents(intents)
        return tuple(
            OperationAuditEntry(
                occurred_at=self._clock(),
                action=preview.action.value,
                agent_id=target.agent_id,
                agent_name=target.name,
                success=result.success,
                message=result.message,
            )
            for target, result in zip(preview.targets, results, strict=True)
        )

    def _execute_mark_complete(
        self,
        targets: Sequence[DashboardAgentListItemView],
    ) -> Sequence[OperationAuditEntry]:
        results: list[OperationAuditEntry] = []
        for target in targets:
            action_result = self._agents.mark_complete(target.agent_id)
            results.append(
                OperationAuditEntry(
                    occurred_at=self._clock(),
                    action=OperationsAction.MARK_COMPLETE.value,
                    agent_id=target.agent_id,
                    agent_name=target.name,
                    success=True,
                    message=_format_mark_complete_message(action_result),
                )
            )
        return results

    def _intent_for_action(self, action: OperationsAction, agent_id: str) -> AgentIntentView:
        match action:
            case OperationsAction.INTERRUPT:
                return self._agents.interrupt_intent(agent_id)
            case OperationsAction.OPEN_PANE:
                return self._agents.open_pane_intent(agent_id)
            case OperationsAction.OPEN_WORKTREE:
                return self._agents.open_worktree_intent(agent_id)
            case OperationsAction.MARK_COMPLETE:
                msg = "mark_complete does not use tmux intents"
                raise RuntimeError(msg)


def _format_mark_complete_message(result: AgentActionResult) -> str:
    session_id = result.session_id or "-"
    return f"mark_complete {result.agent.name} session {session_id} ended={result.session_ended}"


_ACTION_LABELS: dict[OperationsAction, str] = {
    OperationsAction.INTERRUPT: "Interrupt",
    OperationsAction.MARK_COMPLETE: "Mark complete",
    OperationsAction.OPEN_PANE: "Focus pane",
    OperationsAction.OPEN_WORKTREE: "Reveal worktree",
}

_DESTRUCTIVE_ACTIONS = frozenset({OperationsAction.INTERRUPT, OperationsAction.MARK_COMPLETE})

__all__ = [
    "OperationsAction",
    "OperationsActionPort",
    "OperationsActionPreview",
    "OperationsAgentPort",
    "OperationsController",
    "OperationsDashboardPort",
    "OperationsExecutionSummary",
    "OperationsState",
]
