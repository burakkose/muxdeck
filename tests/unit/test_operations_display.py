# ruff: noqa: E402,I001

from __future__ import annotations

from datetime import UTC, datetime
import sys
from pathlib import Path

from textual.widgets import Static

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from muxdeck.controllers import OperationsAction, OperationsActionPreview
from muxdeck.controllers.dashboard_controller import DashboardAgentListItemView
from muxdeck.domain.enums import AgentStatus
from muxdeck.services.operations_service import OperationAuditEntry
from muxdeck.widgets.operations import (
    BulkActionPreviewPanel,
    OperationsAgentListPanel,
    OperationsHistoryPanel,
    OperationsSelectionPanel,
)

_TS = datetime(2025, 1, 1, tzinfo=UTC)


def _agent(agent_id: str = "agent-1", *, name: str = "Planner") -> DashboardAgentListItemView:
    return DashboardAgentListItemView(
        agent_id=agent_id,
        name=name,
        status=AgentStatus.RUNNING,
        repo_name="repo",
        branch="main",
        worktree_name="wt",
        pane_id="%1",
        task_title="task",
        worktree_path="/repo/wt",
        latest_session_id="session-1",
        last_event_kind="agent.updated",
        last_log_at=_TS,
        last_seen_at=_TS,
        started_at=_TS,
        idle_seconds=12,
        needs_attention=False,
        attention_reason=None,
        token_total=10,
        estimated_cost_usd="0.10",
    )


def _render(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if hasattr(renderable, "plain") else str(renderable)


def test_operations_agent_list_panel_includes_selection_column() -> None:
    panel = OperationsAgentListPanel(widget_id="ops")
    panel.set_agents((_agent(),), selected_agent_ids=("agent-1",), cursor_agent_id="agent-1")

    table = panel._build_table()

    assert len(table.columns) == 6
    assert "x" in str(table.columns[0]._cells[0]).lower()


def test_selection_panel_renders_selected_agents() -> None:
    panel = OperationsSelectionPanel(id="selection")
    panel.set_selection(
        (_agent(name="Planner"), _agent("agent-2", name="Reviewer")),
        total_agents=3,
    )

    rendered = _render(panel)

    assert "2 selected / 3 visible" in rendered
    assert "Planner" in rendered
    assert "Reviewer" in rendered


def test_preview_panel_renders_confirmation_state() -> None:
    panel = BulkActionPreviewPanel(id="preview")
    preview = OperationsActionPreview(
        action=OperationsAction.INTERRUPT,
        label="Interrupt",
        summary="Interrupt 2 agents",
        confirmation_message="Interrupt 2 agents? Planner, Reviewer",
        selected_agent_ids=("agent-1", "agent-2"),
        targets=(_agent(name="Planner"), _agent("agent-2", name="Reviewer")),
        requires_confirmation=True,
    )

    panel.set_preview(preview)

    rendered = _render(panel)
    assert "Interrupt 2 agents" in rendered
    assert "confirm required" in rendered


def test_history_panel_renders_audit_rows() -> None:
    panel = OperationsHistoryPanel(id="history")
    panel.set_entries(
        (
            OperationAuditEntry(
                occurred_at=_TS,
                action="interrupt",
                agent_id="agent-1",
                agent_name="Planner",
                success=True,
                message="sent interrupt",
            ),
        )
    )

    rendered = _render(panel)
    assert "interrupt Planner" in rendered
    assert "sent interrupt" in rendered
