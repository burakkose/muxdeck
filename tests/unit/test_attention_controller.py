# ruff: noqa: ANN201

from __future__ import annotations

from datetime import UTC, datetime

from muxdeck.controllers.attention_controller import (
    AttentionController,
    AttentionFilterState,
)
from muxdeck.controllers.dashboard_controller import (
    DashboardAgentListItemView,
    DashboardFilterState,
    DashboardHealthSummary,
    DashboardSelectedAgentView,
    DashboardSort,
    DashboardState,
)
from muxdeck.domain.enums import AgentStatus
from muxdeck.services.attention_service import AttentionInboxService
from muxdeck.services.operator_status_service import describe_operator_status

_TS = datetime(2025, 1, 1, 12, tzinfo=UTC)


def _agent(
    *,
    agent_id: str,
    name: str,
    status: AgentStatus,
    needs_attention: bool,
    attention_reason: str | None,
    is_potentially_stuck: bool = False,
) -> DashboardAgentListItemView:
    return DashboardAgentListItemView(
        agent_id=agent_id,
        name=name,
        status=status,
        repo_name="repo",
        branch="task/demo",
        worktree_name="demo",
        pane_id=f"%{agent_id[-1]}",
        task_title="Handle alerts",
        worktree_path="/repo/demo",
        latest_session_id=f"session-{agent_id}",
        last_event_kind="agent.updated",
        last_log_at=_TS,
        last_seen_at=_TS,
        started_at=_TS,
        idle_seconds=90,
        needs_attention=needs_attention,
        attention_reason=attention_reason,
        token_total=10,
        estimated_cost_usd="0.010000",
        current_activity="checking alerts",
        sparkline="▁▂▄▆█",
        is_potentially_stuck=is_potentially_stuck,
        operator_status=describe_operator_status(
            agent_status=status,
            needs_attention=needs_attention,
            attention_reason=attention_reason,
            idle_seconds=90,
            is_potentially_stuck=is_potentially_stuck,
            task_title="Handle alerts",
            current_activity="checking alerts",
        ),
    )


def _selected(item: DashboardAgentListItemView) -> DashboardSelectedAgentView:
    return DashboardSelectedAgentView(
        item=item,
        repo_root="/repo",
        worktree_id="worktree-1",
        session_count=1,
        open_session_id=item.latest_session_id,
        copilot_session_id=None,
        latest_event_kind=item.last_event_kind,
        latest_event_severity="error" if item.status is AgentStatus.ERROR else "warning",
        latest_event_at=_TS,
        log_preview=(),
        recent_events=("⚠ Alert",),
    )


class FakeDashboard:
    def __init__(self) -> None:
        self.items = (
            _agent(
                agent_id="agent-1",
                name="Reviewer",
                status=AgentStatus.WAITING_INPUT,
                needs_attention=True,
                attention_reason="waiting for confirmation input",
            ),
            _agent(
                agent_id="agent-2",
                name="Fixer",
                status=AgentStatus.ERROR,
                needs_attention=True,
                attention_reason="tool failed with exit code 1",
            ),
        )
        self.selected = {item.agent_id: _selected(item) for item in self.items}

    def build_state(
        self,
        *,
        filters: DashboardFilterState | None = None,
        selected_agent_id: str | None = None,
        preview_line_limit: int = 8,
        alert_limit: int = 20,
    ) -> DashboardState:
        del filters, preview_line_limit, alert_limit
        selected_id = selected_agent_id or self.items[0].agent_id
        selected = self.selected[selected_id]
        return DashboardState(
            generated_at=_TS,
            metrics=(),
            filters=DashboardFilterState(attention_only=True, include_completed=True),
            sort=DashboardSort(),
            health=DashboardHealthSummary(
                tone="critical",
                message="intervention required",
                total_agents=2,
                active_agents=2,
                attention_agents=2,
                waiting_input_agents=1,
                blocked_agents=0,
                error_agents=1,
            ),
            alerts=(),
            agents=self.items,
            selected_agent_id=selected_id,
            selected_agent=selected,
        )

    def build_selected_agent_view(
        self,
        item: DashboardAgentListItemView,
        *,
        preview_line_limit: int = 8,
    ) -> DashboardSelectedAgentView:
        del preview_line_limit
        return self.selected[item.agent_id]


def test_attention_controller_builds_inbox_and_unread_filter() -> None:
    controller = AttentionController(FakeDashboard(), AttentionInboxService())

    state = controller.build_state()

    assert [item.agent_id for item in state.items] == ["agent-1", "agent-2"]
    assert state.summary.total_items == 2
    assert state.summary.unread_items == 2
    assert state.summary.critical_items == 1
    assert [note.alert_id for note in state.notifications] == ["agent-2:failed"]
    assert state.selected_item is not None
    assert state.selected_item.item.operator_status.headline == "waiting for input"

    controller.mark_read("agent-1:waiting_input")
    unread_only = controller.build_state(filters=AttentionFilterState(unread_only=True))

    assert [item.agent_id for item in unread_only.items] == ["agent-2"]


def test_attention_controller_observe_alerts() -> None:
    from muxdeck.controllers.dashboard_controller import DashboardAlertView

    controller = AttentionController(FakeDashboard(), AttentionInboxService())

    alerts = (
        DashboardAlertView(
            agent_id="agent-1",
            agent_name="Reviewer",
            alert_id="alert-1",
            severity="error",
            title="failed",
            message="tool failed",
            occurred_at=_TS,
        ),
        DashboardAlertView(
            agent_id="agent-2",
            agent_name="Fixer",
            alert_id="alert-2",
            severity="warning",
            title="warning",
            message="something",
            occurred_at=_TS,
        ),
    )

    notifications = controller.observe_alerts(alerts)

    assert len(notifications) == 1
    assert notifications[0].alert_id == "alert-1"


def test_attention_controller_builds_state_with_selected_agent() -> None:
    controller = AttentionController(FakeDashboard(), AttentionInboxService())

    state = controller.build_state(selected_agent_id="agent-2")

    assert state.selected_agent_id == "agent-2"
    assert state.selected_item is not None
    assert state.selected_item.item.agent_id == "agent-2"


def test_attention_controller_mark_read_and_count() -> None:
    inbox = AttentionInboxService()
    controller2 = AttentionController(FakeDashboard(), inbox)

    state1 = controller2.build_state()
    assert state1.summary.unread_items == 2

    controller2.mark_read("agent-1:waiting_input")
    state2 = controller2.build_state()
    assert state2.summary.unread_items == 1


def test_attention_controller_mark_all_read() -> None:
    inbox = AttentionInboxService()
    controller = AttentionController(FakeDashboard(), inbox)

    state1 = controller.build_state()
    assert state1.summary.unread_items == 2

    controller.mark_all_read()
    state2 = controller.build_state()
    assert state2.summary.unread_items == 0


def test_attention_controller_filters_unread_only() -> None:
    inbox = AttentionInboxService()
    controller = AttentionController(FakeDashboard(), inbox)

    controller.build_state()
    controller.mark_read("agent-1:waiting_input")
    state2 = controller.build_state(filters=AttentionFilterState(unread_only=True))

    assert len(state2.items) == 1
    assert state2.items[0].agent_id == "agent-2"


def test_attention_controller_selects_first_item_when_no_agent_specified() -> None:
    controller = AttentionController(FakeDashboard(), AttentionInboxService())

    state = controller.build_state()

    assert state.selected_agent_id == "agent-1"


def test_attention_controller_observe_dashboard_state() -> None:
    controller = AttentionController(FakeDashboard(), AttentionInboxService())
    dashboard = FakeDashboard()
    dashboard_state = dashboard.build_state()

    notifications = controller.observe_dashboard_state(dashboard_state)

    assert len(notifications) == 0
