# ruff: noqa: ANN001,ANN201,E501

"""Tests for the compact agent list table and display helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from copilot_commander.controllers import DashboardAgentListItemView
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.widgets.dashboard import _format_idle

_TS = datetime(2025, 1, 1, tzinfo=UTC)


def _agent(
    *,
    name: str = "node",
    repo_name: str | None = "myrepo",
    worktree_name: str | None = "myworktree",
    branch: str | None = "main",
    status: AgentStatus = AgentStatus.RUNNING,
    idle_seconds: int = 0,
    needs_attention: bool = False,
    attention_reason: str | None = None,
    task_title: str | None = None,
) -> DashboardAgentListItemView:
    return DashboardAgentListItemView(
        agent_id="agent-1",
        name=name,
        status=status,
        repo_name=repo_name,
        branch=branch,
        worktree_name=worktree_name,
        pane_id="%1",
        task_title=task_title,
        worktree_path="/repo/wt",
        latest_session_id="session-1",
        last_event_kind="agent.updated",
        last_log_at=_TS,
        last_seen_at=_TS,
        started_at=_TS,
        idle_seconds=idle_seconds,
        needs_attention=needs_attention,
        attention_reason=attention_reason,
        token_total=None,
        estimated_cost_usd=None,
    )


# ── _format_idle ────────────────────────────────────────────────────


class TestFormatIdle:
    def test_seconds(self):
        assert _format_idle(0) == "0s"
        assert _format_idle(59) == "59s"

    def test_minutes(self):
        assert _format_idle(60) == "1m"
        assert _format_idle(3599) == "59m"

    def test_hours(self):
        assert _format_idle(3600) == "1h0m"
        assert _format_idle(7260) == "2h1m"


# ── AgentListPanel table rendering ──────────────────────────────────


class TestAgentListTable:
    """Verify the compact table builds with 7 columns."""

    def test_table_has_seven_columns(self):
        from copilot_commander.widgets.dashboard import AgentListPanel

        panel = AgentListPanel(widget_id="test")
        panel._agents = (_agent(),)
        panel._selected_index = 0
        table = panel._build_table()
        assert len(table.columns) == 7

    def test_display_name_prefers_repo_name(self):
        """repo_name is used as the display name when available."""
        from copilot_commander.widgets.dashboard import AgentListPanel

        agent = _agent(name="node", repo_name="tachyon", worktree_name="wt-tachyon")
        panel = AgentListPanel(widget_id="test")
        panel._agents = (agent,)
        panel._selected_index = 0
        table = panel._build_table()
        row_cells = table.columns[1]._cells
        assert len(row_cells) == 1
        assert "tachyon" in str(row_cells[0])

    def test_display_name_falls_back_to_worktree(self):
        """worktree_name is used when repo_name is absent."""
        from copilot_commander.widgets.dashboard import AgentListPanel

        agent = _agent(name="node", repo_name=None, worktree_name="tachyon")
        panel = AgentListPanel(widget_id="test")
        panel._agents = (agent,)
        panel._selected_index = 0
        table = panel._build_table()
        row_cells = table.columns[1]._cells
        assert "tachyon" in str(row_cells[0])

    def test_display_name_falls_back_to_process_name(self):
        from copilot_commander.widgets.dashboard import AgentListPanel

        agent = _agent(name="python", repo_name=None, worktree_name=None)
        panel = AgentListPanel(widget_id="test")
        panel._agents = (agent,)
        panel._selected_index = 0
        table = panel._build_table()
        row_cells = table.columns[1]._cells
        assert "python" in str(row_cells[0])

    def test_info_column_shows_attention_reason(self):
        from copilot_commander.widgets.dashboard import AgentListPanel

        agent = _agent(needs_attention=True, attention_reason="idle for 300s")
        panel = AgentListPanel(widget_id="test")
        panel._agents = (agent,)
        panel._selected_index = 0
        table = panel._build_table()
        # info is the last column (index 6)
        row_cells = table.columns[6]._cells
        assert "idle for 300s" in str(row_cells[0])

    def test_info_column_shows_task_when_no_attention(self):
        from copilot_commander.widgets.dashboard import AgentListPanel

        agent = _agent(task_title="Fix bug #42", needs_attention=False)
        panel = AgentListPanel(widget_id="test")
        panel._agents = (agent,)
        panel._selected_index = 0
        table = panel._build_table()
        row_cells = table.columns[6]._cells
        assert "Fix bug #42" in str(row_cells[0])
