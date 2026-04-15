from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from copilot_commander.controllers.dashboard_controller import (
    AlertSeverity,
    DashboardAgentListItemView,
    DashboardAlertView,
    DashboardFilterState,
    DashboardHealthSummary,
    DashboardSort,
    DashboardState,
)
from copilot_commander.domain.enums import AgentStatus

_ALERT_LIMIT = 256


class AttentionDashboardPort(Protocol):
    def build_state(
        self,
        *,
        filters: DashboardFilterState | None = None,
        sort: DashboardSort | None = None,
        selected_agent_id: str | None = None,
        preview_line_limit: int = 8,
        alert_limit: int = 5,
    ) -> DashboardState: ...


@dataclass(frozen=True, slots=True)
class AttentionInboxRowView:
    alert_key: str
    agent_id: str
    agent_name: str
    severity: AlertSeverity
    title: str
    message: str
    occurred_at: datetime
    agent_status: AgentStatus
    branch: str | None
    idle_seconds: int
    current_activity: str | None
    attention_reason: str | None
    last_event_kind: str | None
    is_acknowledged: bool
    is_unread: bool


@dataclass(frozen=True, slots=True)
class AttentionInboxSummaryView:
    total_rows: int
    unread_rows: int
    acknowledged_rows: int
    critical_rows: int
    warning_rows: int


@dataclass(frozen=True, slots=True)
class AttentionInboxState:
    generated_at: datetime
    health: DashboardHealthSummary
    summary: AttentionInboxSummaryView
    rows: tuple[AttentionInboxRowView, ...]
    selected_alert_key: str | None
    selected_row: AttentionInboxRowView | None


class AttentionInboxController:
    def __init__(self, dashboard: AttentionDashboardPort) -> None:
        self._dashboard = dashboard
        self._acknowledged: set[str] = set()
        self._seen: set[str] = set()

    def build_state(
        self,
        *,
        selected_alert_key: str | None = None,
        include_acknowledged: bool = True,
    ) -> AttentionInboxState:
        dashboard_state = self._dashboard.build_state(
            filters=DashboardFilterState(attention_only=True, include_completed=False),
            sort=DashboardSort(field="last_seen", descending=True),
            preview_line_limit=0,
            alert_limit=_ALERT_LIMIT,
        )
        agents_by_id = {agent.agent_id: agent for agent in dashboard_state.agents}
        all_rows = tuple(
            self._build_row(alert, agents_by_id.get(alert.agent_id))
            for alert in dashboard_state.alerts
            if agents_by_id.get(alert.agent_id) is not None
        )
        rows = (
            all_rows
            if include_acknowledged
            else tuple(row for row in all_rows if not row.is_acknowledged)
        )
        selected_row = self._select_row(rows, selected_alert_key)
        return AttentionInboxState(
            generated_at=dashboard_state.generated_at,
            health=dashboard_state.health,
            summary=self._build_summary(rows),
            rows=rows,
            selected_alert_key=selected_row.alert_key if selected_row is not None else None,
            selected_row=selected_row,
        )

    def acknowledge(self, alert_key: str) -> bool:
        if alert_key in self._acknowledged:
            return False
        self._acknowledged.add(alert_key)
        self._seen.add(alert_key)
        return True

    def mark_read(self, alert_key: str) -> bool:
        if alert_key in self._seen:
            return False
        self._seen.add(alert_key)
        return True

    def _build_row(
        self,
        alert: DashboardAlertView,
        agent: DashboardAgentListItemView | None,
    ) -> AttentionInboxRowView:
        if agent is None:
            msg = "attention rows require a matching dashboard agent"
            raise ValueError(msg)
        alert_key = self._alert_key(alert)
        return AttentionInboxRowView(
            alert_key=alert_key,
            agent_id=alert.agent_id,
            agent_name=alert.agent_name,
            severity=alert.severity,
            title=alert.title,
            message=alert.message,
            occurred_at=alert.occurred_at,
            agent_status=agent.status,
            branch=agent.branch,
            idle_seconds=agent.idle_seconds,
            current_activity=agent.current_activity,
            attention_reason=agent.attention_reason,
            last_event_kind=agent.last_event_kind,
            is_acknowledged=alert_key in self._acknowledged,
            is_unread=alert_key not in self._seen,
        )

    def _build_summary(
        self,
        rows: tuple[AttentionInboxRowView, ...],
    ) -> AttentionInboxSummaryView:
        return AttentionInboxSummaryView(
            total_rows=len(rows),
            unread_rows=sum(1 for row in rows if row.is_unread),
            acknowledged_rows=sum(1 for row in rows if row.is_acknowledged),
            critical_rows=sum(1 for row in rows if row.severity == "error"),
            warning_rows=sum(1 for row in rows if row.severity == "warning"),
        )

    def _select_row(
        self,
        rows: tuple[AttentionInboxRowView, ...],
        selected_alert_key: str | None,
    ) -> AttentionInboxRowView | None:
        if selected_alert_key is not None:
            for row in rows:
                if row.alert_key == selected_alert_key:
                    return row
        for row in rows:
            if not row.is_acknowledged:
                return row
        return rows[0] if rows else None

    def _alert_key(self, alert: DashboardAlertView) -> str:
        return "|".join(
            (
                alert.agent_id,
                alert.severity,
                alert.occurred_at.isoformat(),
                alert.title,
                alert.message,
            )
        )


__all__ = [
    "AttentionDashboardPort",
    "AttentionInboxController",
    "AttentionInboxRowView",
    "AttentionInboxState",
    "AttentionInboxSummaryView",
]
