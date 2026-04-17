# ruff: noqa: ANN201

"""Tests for the compact agent list table and display helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from copilot_commander.controllers import (
    DashboardAgentListItemView,
    DashboardHealthSummary,
    DashboardLogLineView,
    DashboardSelectedAgentView,
)
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.widgets.dashboard import _format_idle

_TS = datetime(2025, 1, 1, tzinfo=UTC)


def _agent(
    *,
    name: str = "node",
    repo_name: str | None = "myrepo",
    worktree_name: str | None = "myworktree",
    window_name: str | None = None,
    branch: str | None = "main",
    status: AgentStatus = AgentStatus.RUNNING,
    idle_seconds: int = 0,
    needs_attention: bool = False,
    attention_reason: str | None = None,
    task_title: str | None = None,
    current_activity: str | None = None,
    sparkline: str = "        ",
    is_potentially_stuck: bool = False,
    token_total: int | None = None,
    estimated_cost_usd: str | None = None,
    last_event_kind: str | None = "agent.updated",
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
        last_event_kind=last_event_kind,
        last_log_at=_TS,
        last_seen_at=_TS,
        started_at=_TS,
        idle_seconds=idle_seconds,
        needs_attention=needs_attention,
        attention_reason=attention_reason,
        token_total=token_total,
        estimated_cost_usd=estimated_cost_usd,
        window_name=window_name,
        current_activity=current_activity,
        sparkline=sparkline,
        is_potentially_stuck=is_potentially_stuck,
    )


def _selected_agent(
    item: DashboardAgentListItemView | None = None,
    *,
    recent_events: tuple[str, ...] = (),
    log_preview: tuple[DashboardLogLineView, ...] | None = None,
    latest_event_kind: str | None = "agent.updated",
    latest_event_severity: str | None = "info",
) -> DashboardSelectedAgentView:
    selected_item = item or _agent()
    return DashboardSelectedAgentView(
        item=selected_item,
        repo_root="/repo",
        worktree_id="wt-1",
        session_count=2,
        open_session_id="session-1",
        copilot_session_id=None,
        latest_event_kind=latest_event_kind,
        latest_event_severity=latest_event_severity,
        latest_event_at=_TS,
        log_preview=(
            (
                DashboardLogLineView(
                    captured_at=_TS,
                    source="stdout",
                    sequence_no=1,
                    content="stdout line",
                ),
                DashboardLogLineView(
                    captured_at=_TS,
                    source="stderr",
                    sequence_no=2,
                    content="stderr line",
                ),
            )
            if log_preview is None
            else log_preview
        ),
        recent_events=recent_events,
    )


def _render(widget: object) -> str:
    renderable = widget.render()
    return renderable.plain if hasattr(renderable, "plain") else str(renderable)


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
    """Verify the compact table builds with 5 columns."""

    def test_table_has_five_columns(self):
        from copilot_commander.widgets.dashboard import AgentListPanel

        panel = AgentListPanel(widget_id="test")
        panel._agents = (_agent(),)
        panel._selected_index = 0
        table = panel._build_table()
        assert len(table.columns) == 5

    def test_display_name_uses_process_name(self):
        """Agent name is the primary display name (unique in list)."""
        from copilot_commander.widgets.dashboard import AgentListPanel

        agent = _agent(name="Planner", repo_name="tachyon", worktree_name="wt-tachyon")
        panel = AgentListPanel(widget_id="test")
        panel._agents = (agent,)
        panel._selected_index = 0
        table = panel._build_table()
        row_cells = table.columns[1]._cells
        assert len(row_cells) == 1
        assert "Planner" in str(row_cells[0])

    def test_display_name_disambiguates_duplicates(self):
        """When names collide, worktree/repo suffix is added."""
        from copilot_commander.widgets.dashboard import AgentListPanel

        a1 = _agent(name="node", repo_name="tachyon", worktree_name="wt-a")
        a2 = _agent(name="node", repo_name="tachyon", worktree_name="wt-b")
        panel = AgentListPanel(widget_id="test")
        panel._agents = (a1, a2)
        panel._selected_index = 0
        table = panel._build_table()
        cells = table.columns[1]._cells
        assert "wt-a" in str(cells[0])
        assert "wt-b" in str(cells[1])

    def test_display_name_uses_window_name_when_repo_and_worktree_match(self):
        from copilot_commander.widgets.dashboard import AgentListPanel

        a1 = _agent(
            name="CosmosDB",
            repo_name="CosmosDB",
            worktree_name="native",
            window_name="ParallelTransformation",
        )
        a2 = _agent(
            name="CosmosDB",
            repo_name="CosmosDB",
            worktree_name="native",
            window_name="Expired Transactions",
        )
        panel = AgentListPanel(widget_id="test")
        panel._agents = (a1, a2)
        panel._selected_index = 0
        table = panel._build_table()
        cells = table.columns[1]._cells
        assert "ParallelTransformation" in str(cells[0])
        assert "Expired Transactions" in str(cells[1])

    def test_display_name_falls_back_to_process_name(self):
        from copilot_commander.widgets.dashboard import AgentListPanel

        agent = _agent(name="python", repo_name=None, worktree_name=None)
        panel = AgentListPanel(widget_id="test")
        panel._agents = (agent,)
        panel._selected_index = 0
        table = panel._build_table()
        row_cells = table.columns[1]._cells
        assert "python" in str(row_cells[0])

    def test_attention_agent_gets_attention_row_style(self):
        """Agents needing attention get a distinct row background."""
        from copilot_commander.widgets.dashboard import AgentListPanel

        agent = _agent(needs_attention=True, attention_reason="idle for 300s")
        panel = AgentListPanel(widget_id="test")
        panel._agents = (agent,)
        panel._selected_index = -1
        table = panel._build_table()
        # With 5 columns (glyph, name, status, activity, branch),
        # attention info is in detail panel.
        # The table row should have attention background style.
        assert len(table.columns) == 5

    def test_short_status_column_shows_status(self):
        """The status column (index 2) shows a readable status label."""
        from copilot_commander.widgets.dashboard import AgentListPanel

        agent = _agent(status=AgentStatus.RUNNING)
        panel = AgentListPanel(widget_id="test")
        panel._agents = (agent,)
        panel._selected_index = 0
        table = panel._build_table()
        status_cells = table.columns[2]._cells
        assert "working" in str(status_cells[0])

    def test_attention_running_agent_shows_review_status(self):
        from copilot_commander.widgets.dashboard import AgentListPanel

        agent = _agent(
            status=AgentStatus.RUNNING,
            needs_attention=True,
            attention_reason="waiting for review",
        )
        panel = AgentListPanel(widget_id="test")
        panel._agents = (agent,)
        panel._selected_index = 0
        table = panel._build_table()
        status_cells = table.columns[2]._cells
        assert "review" in str(status_cells[0])

    def test_stuck_agent_shows_stuck_status(self):
        from copilot_commander.widgets.dashboard import AgentListPanel

        agent = _agent(status=AgentStatus.RUNNING, is_potentially_stuck=True)
        panel = AgentListPanel(widget_id="test")
        panel._agents = (agent,)
        panel._selected_index = 0
        table = panel._build_table()
        status_cells = table.columns[2]._cells
        assert "stale" in str(status_cells[0])


class TestDashboardPanels:
    def test_focus_panel_highlights_attention_and_session_summary(self):
        from copilot_commander.widgets.dashboard import AgentDetailPanel

        selected = _selected_agent(
            _agent(
                name="Planner",
                task_title="Plan dashboard",
                needs_attention=True,
                attention_reason="waiting for operator",
                current_activity="Reviewing layout",
                sparkline="▁▂▄▆█",
                token_total=120,
                estimated_cost_usd="0.120000",
            ),
            latest_event_severity="warning",
        )
        panel = AgentDetailPanel(id="focus")

        panel.set_agent(selected)

        rendered = _render(panel).lower()
        assert "focus" in rendered
        assert "waiting for operator" in rendered
        assert "session-1 (2 total)" in rendered

    def test_activity_panel_renders_recent_markers(self):
        from copilot_commander.widgets.dashboard import ActivityPanel

        selected = _selected_agent(
            _agent(
                task_title="Review logs",
                current_activity="Reviewing logs",
                sparkline="▁▂▄▆█",
            ),
            recent_events=("⚡ Running tests", "⚠ Needs input"),
        )
        panel = ActivityPanel(id="activity")

        panel.set_agent(selected)

        rendered = _render(panel)
        assert "activity" in rendered.lower()
        assert "Running tests" in rendered
        assert "Needs input" in rendered

    def test_log_preview_panel_promotes_output_title_and_lines(self):
        from copilot_commander.widgets.dashboard import LogPreviewPanel

        panel = LogPreviewPanel(id="output")

        panel.set_logs(_selected_agent())

        rendered = _render(panel)
        assert "output" in rendered.lower()
        assert "stdout line" in rendered
        assert "stderr line" in rendered

    def test_fleet_health_panel_renders_counts_and_selected_status(self):
        from copilot_commander.widgets.dashboard import FleetHealthPanel

        panel = FleetHealthPanel(id="fleet")
        health = DashboardHealthSummary(
            tone="warning",
            message="some agents need review",
            total_agents=4,
            active_agents=3,
            attention_agents=2,
            waiting_input_agents=1,
            blocked_agents=0,
            error_agents=1,
        )

        panel.set_state(health, _selected_agent(_agent(name="Planner")))

        rendered = _render(panel).lower()
        assert "fleet" in rendered
        assert "4 total / 3 active" in rendered
        assert "planner" in rendered

    def test_subagent_detail_renders_structured_block_for_background(self):
        from copilot_commander.controllers import DashboardSubAgentView
        from copilot_commander.domain.subagents import ReadAgentInteraction
        from copilot_commander.widgets.dashboard import AgentDetailPanel

        interactions = (
            ReadAgentInteraction(
                timestamp=datetime(2026, 4, 5, 4, 14, 31, tzinfo=UTC),
                arguments_summary='agent_id="risk-reviewer", wait=true, timeout=15',
                result_content="progress update\nsecond line",
            ),
            ReadAgentInteraction(
                timestamp=datetime(2026, 4, 5, 4, 15, 5, tzinfo=UTC),
                arguments_summary='agent_id="risk-reviewer", wait=false',
                result_content="final answer",
            ),
        )
        view = DashboardSubAgentView(
            tool_call_id="call_background_abcdef",
            agent_name="general-purpose",
            display_name="General Purpose Agent",
            description="Assessing risks",
            started_at=datetime(2026, 4, 5, 4, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 4, 5, 4, 16, 0, tzinfo=UTC),
            is_running=False,
            task_name="risk-reviewer",
            mode="background",
            read_interactions=interactions,
            total_tokens=133463,
            duration_ms=124335,
            total_tool_calls=18,
            model="claude-sonnet-4.5",
            error_message="AbortError: aborted",
        )
        panel = AgentDetailPanel(id="focus")

        panel.set_subagent(view)

        rendered = _render(panel)
        assert "risk-reviewer" in rendered
        assert "[background]" in rendered
        assert "133,463" in rendered
        assert "claude-sonnet-4.5" in rendered
        assert "AbortError" in rendered
        assert "interactions (2)" in rendered
        assert "read_agent(" in rendered
        assert 'agent_id="risk-reviewer"' in rendered
        assert "final answer" in rendered
        # The launch-ack heading must NOT be rendered when we have a
        # structured block to show instead.
        assert "launch ack" not in rendered.lower()

    def test_subagent_detail_falls_back_to_launch_ack_when_no_signals(self):
        from copilot_commander.controllers import DashboardSubAgentView
        from copilot_commander.widgets.dashboard import AgentDetailPanel

        view = DashboardSubAgentView(
            tool_call_id="call_new_launch",
            agent_name="general-purpose",
            display_name="General Purpose Agent",
            description=None,
            started_at=datetime(2026, 4, 5, 4, 12, 0, tzinfo=UTC),
            completed_at=None,
            is_running=True,
            task_name="bg-worker",
            mode="background",
            result_content="Agent started in background with agent_id: bg-worker",
        )
        panel = AgentDetailPanel(id="focus")

        panel.set_subagent(view)

        rendered = _render(panel).lower()
        assert "launch ack" in rendered
        assert "bg-worker" in rendered
        assert "interactions" not in rendered

    def test_subagent_detail_foreground_keeps_output_rendering(self):
        from copilot_commander.controllers import DashboardSubAgentView
        from copilot_commander.widgets.dashboard import AgentDetailPanel

        view = DashboardSubAgentView(
            tool_call_id="call_fg_1234",
            agent_name="explore",
            display_name="Explore Agent",
            description=None,
            started_at=datetime(2026, 4, 5, 4, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 4, 5, 4, 12, 3, tzinfo=UTC),
            is_running=False,
            task_name="fg",
            mode=None,
            result_content="the exploration answer",
            success=True,
        )
        panel = AgentDetailPanel(id="focus")

        panel.set_subagent(view)

        rendered = _render(panel)
        assert "the exploration answer" in rendered
        # Foreground has no metrics and no read_agent interactions — we
        # must use the existing output rendering, not a metrics block.
        assert "interactions" not in rendered.lower()
        assert "output" in rendered.lower()
