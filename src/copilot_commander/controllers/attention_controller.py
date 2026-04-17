from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, runtime_checkable

from copilot_commander.controllers.dashboard_controller import (
    AlertSeverity,
    DashboardAgentListItemView,
    DashboardFilterState,
    DashboardSelectedAgentView,
    DashboardState,
)
from copilot_commander.services.attention_service import (
    AttentionInboxService,
    AttentionNotification,
    AttentionSignal,
)
from copilot_commander.services.operator_status_service import (
    OperatorStatus,
    describe_operator_status,
)


@runtime_checkable
class AttentionDashboardPort(Protocol):
    def build_state(
        self,
        *,
        filters: DashboardFilterState | None = None,
        selected_agent_id: str | None = None,
        preview_line_limit: int = 8,
        alert_limit: int = 20,
    ) -> DashboardState:
        """Build a dashboard state snapshot."""

    def build_selected_agent_view(
        self,
        item: DashboardAgentListItemView,
        *,
        preview_line_limit: int = 8,
    ) -> DashboardSelectedAgentView:
        """Build a selected-agent detail view."""


@dataclass(frozen=True, slots=True)
class AttentionFilterState:
    unread_only: bool = False


@dataclass(frozen=True, slots=True)
class AttentionItemView:
    alert_id: str
    agent_id: str
    agent_name: str
    severity: AlertSeverity
    operator_status: OperatorStatus
    message: str
    occurred_at: datetime
    branch: str | None
    worktree_name: str | None
    task_title: str | None
    pane_id: str
    unread: bool = False


@dataclass(frozen=True, slots=True)
class AttentionSummaryView:
    total_items: int
    unread_items: int
    critical_items: int


@dataclass(frozen=True, slots=True)
class AttentionSelectedItemView:
    item: AttentionItemView
    agent: DashboardSelectedAgentView


@dataclass(frozen=True, slots=True)
class AttentionState:
    generated_at: datetime
    filters: AttentionFilterState
    summary: AttentionSummaryView
    items: tuple[AttentionItemView, ...]
    selected_agent_id: str | None
    selected_item: AttentionSelectedItemView | None
    notifications: tuple[AttentionNotification, ...] = ()


class AttentionController:
    def __init__(
        self,
        dashboard: AttentionDashboardPort,
        inbox: AttentionInboxService,
    ) -> None:
        self._dashboard = dashboard
        self._inbox = inbox

    def build_state(
        self,
        *,
        filters: AttentionFilterState | None = None,
        selected_agent_id: str | None = None,
        preview_line_limit: int = 8,
    ) -> AttentionState:
        applied_filters = AttentionFilterState() if filters is None else filters
        source_state = self._dashboard.build_state(
            filters=DashboardFilterState(attention_only=True, include_completed=True),
            selected_agent_id=selected_agent_id,
            preview_line_limit=preview_line_limit,
            alert_limit=20,
        )
        source_items = tuple(self._build_item(agent) for agent in source_state.agents)
        sync_result = self._inbox.synchronize(
            tuple(self._signal_for_item(item) for item in source_items)
        )
        items = tuple(
            replace(item, unread=item.alert_id in sync_result.unread_ids) for item in source_items
        )
        visible_items = self._filter_items(items, applied_filters)
        selected_item = self._select_item(visible_items, selected_agent_id)
        selected_agent = None
        if selected_item is not None:
            source_item = next(
                item for item in source_state.agents if item.agent_id == selected_item.agent_id
            )
            if (
                source_state.selected_agent is not None
                and source_state.selected_agent.item.agent_id == selected_item.agent_id
            ):
                selected_agent = source_state.selected_agent
            else:
                selected_agent = self._dashboard.build_selected_agent_view(
                    source_item,
                    preview_line_limit=preview_line_limit,
                )
        return AttentionState(
            generated_at=source_state.generated_at,
            filters=applied_filters,
            summary=self._build_summary(items),
            items=visible_items,
            selected_agent_id=selected_item.agent_id if selected_item is not None else None,
            selected_item=(
                AttentionSelectedItemView(item=selected_item, agent=selected_agent)
                if selected_item is not None and selected_agent is not None
                else None
            ),
            notifications=sync_result.notifications,
        )

    def observe_dashboard_state(self, state: DashboardState) -> tuple[AttentionNotification, ...]:
        signals = tuple(
            AttentionSignal(
                alert_id=alert.alert_id,
                severity=alert.severity,
                title=alert.title,
                message=alert.message,
                occurred_at=alert.occurred_at,
            )
            for alert in state.alerts
            if alert.alert_id
        )
        return self._inbox.observe(signals)

    @property
    def unread_count(self) -> int:
        return self._inbox.unread_count

    def mark_read(self, alert_id: str) -> None:
        self._inbox.mark_read((alert_id,))

    def mark_all_read(self) -> None:
        self._inbox.mark_all_read()

    def _build_item(self, agent: DashboardAgentListItemView) -> AttentionItemView:
        operator_status = agent.operator_status
        if operator_status is None:
            operator_status = describe_operator_status(
                agent_status=agent.status,
                needs_attention=agent.needs_attention,
                attention_reason=agent.attention_reason,
                idle_seconds=agent.idle_seconds,
                is_potentially_stuck=agent.is_potentially_stuck,
                task_title=agent.task_title,
                current_activity=agent.current_activity,
            )
        severity = _severity_for_status(operator_status)
        return AttentionItemView(
            alert_id=f"{agent.agent_id}:{operator_status.kind.value}",
            agent_id=agent.agent_id,
            agent_name=agent.name,
            severity=severity,
            operator_status=operator_status,
            message=operator_status.reason,
            occurred_at=agent.last_seen_at,
            branch=agent.branch,
            worktree_name=agent.worktree_name,
            task_title=agent.task_title,
            pane_id=agent.pane_id,
        )

    def _signal_for_item(self, item: AttentionItemView) -> AttentionSignal:
        return AttentionSignal(
            alert_id=item.alert_id,
            severity=item.severity,
            title=item.operator_status.headline,
            message=item.message,
            occurred_at=item.occurred_at,
        )

    def _filter_items(
        self,
        items: Sequence[AttentionItemView],
        filters: AttentionFilterState,
    ) -> tuple[AttentionItemView, ...]:
        if not filters.unread_only:
            return tuple(items)
        return tuple(item for item in items if item.unread)

    def _select_item(
        self,
        items: Sequence[AttentionItemView],
        selected_agent_id: str | None,
    ) -> AttentionItemView | None:
        if selected_agent_id is not None:
            for item in items:
                if item.agent_id == selected_agent_id:
                    return item
        return items[0] if items else None

    def _build_summary(self, items: Sequence[AttentionItemView]) -> AttentionSummaryView:
        critical_items = sum(1 for item in items if item.severity == "error")
        unread_items = sum(1 for item in items if item.unread)
        return AttentionSummaryView(
            total_items=len(items),
            unread_items=unread_items,
            critical_items=critical_items,
        )


def _severity_for_status(operator_status: OperatorStatus) -> AlertSeverity:
    if operator_status.tone == "error":
        return "error"
    if operator_status.tone == "warning":
        return "warning"
    return "info"


__all__ = [
    "AttentionController",
    "AttentionDashboardPort",
    "AttentionFilterState",
    "AttentionItemView",
    "AttentionSelectedItemView",
    "AttentionState",
    "AttentionSummaryView",
]
