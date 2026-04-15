# ruff: noqa: ANN001,ANN201,E402,I001

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
from textual.app import App

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from copilot_commander.controllers import (
    AttentionInboxController,
    DashboardAgentListItemView,
    DashboardAlertView,
    DashboardFilterState,
    DashboardHealthSummary,
    DashboardSort,
    DashboardState,
)
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.screens.attention import AttentionInboxScreen
from copilot_commander.widgets.attention import (
    AttentionInboxDetailPanel,
    AttentionInboxListPanel,
    AttentionInboxSummaryPanel,
)

_TS = datetime(2025, 1, 1, 12, tzinfo=UTC)


def _agent(
    *,
    agent_id: str,
    name: str,
    status: AgentStatus,
    idle_seconds: int,
    needs_attention: bool = True,
    attention_reason: str | None = "waiting for operator",
) -> DashboardAgentListItemView:
    return DashboardAgentListItemView(
        agent_id=agent_id,
        name=name,
        status=status,
        repo_name="repo",
        branch=f"task/{name.lower()}",
        worktree_name=name.lower(),
        pane_id=f"%{agent_id[-1]}",
        task_title=f"Handle {name}",
        worktree_path=f"/repo/{name.lower()}",
        latest_session_id=f"session-{agent_id[-1]}",
        last_event_kind="agent.updated",
        last_log_at=_TS,
        last_seen_at=_TS,
        started_at=_TS,
        idle_seconds=idle_seconds,
        needs_attention=needs_attention,
        attention_reason=attention_reason,
        token_total=10,
        estimated_cost_usd="0.100000",
        current_activity="Reviewing output",
        sparkline="▁▂▄▆█",
    )


def _state() -> DashboardState:
    reviewer = _agent(
        agent_id="agent-1",
        name="Reviewer",
        status=AgentStatus.WAITING_INPUT,
        idle_seconds=80,
    )
    builder = _agent(
        agent_id="agent-2",
        name="Builder",
        status=AgentStatus.ERROR,
        idle_seconds=140,
        attention_reason="tests failed",
    )
    return DashboardState(
        generated_at=_TS,
        metrics=(),
        filters=DashboardFilterState(),
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
        alerts=(
            DashboardAlertView(
                agent_id="agent-2",
                agent_name="Builder",
                severity="error",
                title="error",
                message="tests failed",
                occurred_at=_TS,
            ),
            DashboardAlertView(
                agent_id="agent-1",
                agent_name="Reviewer",
                severity="warning",
                title="waiting_input",
                message="waiting for operator",
                occurred_at=_TS,
            ),
        ),
        agents=(builder, reviewer),
        selected_agent_id="agent-2",
        selected_agent=None,
    )


class _FakeDashboard:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.state = _state()

    def build_state(
        self,
        *,
        filters: DashboardFilterState | None = None,
        sort: DashboardSort | None = None,
        selected_agent_id: str | None = None,
        preview_line_limit: int = 8,
        alert_limit: int = 5,
    ) -> DashboardState:
        self.calls.append(
            {
                "filters": filters,
                "sort": sort,
                "selected_agent_id": selected_agent_id,
                "preview_line_limit": preview_line_limit,
                "alert_limit": alert_limit,
            }
        )
        return self.state


def _render(widget: object) -> str:
    renderable = widget.render()
    return renderable.plain if hasattr(renderable, "plain") else str(renderable)


def test_attention_inbox_controller_reuses_dashboard_state_and_tracks_acknowledgements():
    dashboard = _FakeDashboard()
    controller = AttentionInboxController(dashboard)

    initial = controller.build_state()

    assert len(initial.rows) == 2
    assert initial.summary.unread_rows == 2
    assert initial.selected_row is not None
    assert initial.selected_row.agent_name == "Builder"
    assert dashboard.calls[0]["filters"] == DashboardFilterState(
        attention_only=True,
        include_completed=False,
    )
    assert dashboard.calls[0]["sort"] == DashboardSort(field="last_seen", descending=True)
    assert dashboard.calls[0]["preview_line_limit"] == 0

    controller.mark_read(initial.selected_row.alert_key)
    controller.acknowledge(initial.selected_row.alert_key)
    updated = controller.build_state(selected_alert_key=initial.selected_row.alert_key)

    assert updated.summary.unread_rows == 1
    assert updated.summary.acknowledged_rows == 1
    assert updated.selected_row is not None
    assert updated.selected_row.is_acknowledged is True
    assert updated.selected_row.is_unread is False


def test_attention_inbox_list_and_detail_render_triage_states():
    controller = AttentionInboxController(_FakeDashboard())
    state = controller.build_state()
    selected = state.selected_row
    assert selected is not None

    summary_panel = AttentionInboxSummaryPanel(id="summary")
    summary_panel.set_state(state.summary, state.health)
    assert "unread" in _render(summary_panel).lower()

    list_panel = AttentionInboxListPanel(widget_id="list")
    list_panel.set_rows(state.rows, selected_alert_key=state.selected_alert_key)
    table = list_panel._build_table()
    assert "Builder: tests failed" in str(table.columns[6]._cells[0])
    assert "new" in str(table.columns[1]._cells[0])

    detail_panel = AttentionInboxDetailPanel(id="detail")
    detail_panel.set_row(selected)
    rendered = _render(detail_panel).lower()
    assert "triage" in rendered
    assert "tests failed" in rendered
    assert "reviewing output" in rendered


class _AttentionTestApp(App[None]):
    def __init__(self, runtime: object) -> None:
        super().__init__()
        self._runtime = runtime

    def on_mount(self) -> None:
        self.push_screen(AttentionInboxScreen(self._runtime))


@pytest.mark.asyncio
async def test_attention_inbox_screen_marks_selected_row_read_and_acknowledges() -> None:
    dashboard = _FakeDashboard()
    runtime = SimpleNamespace(dashboard=dashboard)
    app = _AttentionTestApp(runtime)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, AttentionInboxScreen)
        assert "unread" in _render(screen.query_one("#attention-summary")).lower()
        assert "builder" in _render(screen.query_one("#attention-detail")).lower()

        await pilot.press("a")
        await pilot.pause()

        list_panel = screen.query_one(AttentionInboxListPanel)
        table = list_panel._build_table()
        assert "acked" in str(table.columns[1]._cells[0])
        assert screen._state is not None
        assert screen._state.selected_row is not None
        assert screen._state.selected_row.is_acknowledged is True
        assert screen._status == "acknowledged Builder"
