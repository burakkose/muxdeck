from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from copilot_commander.controllers.fleet_controller import (
    FleetAgentSummaryView,
    FleetFilterState,
    FleetHealthSummary,
    FleetHistoryMetricView,
    FleetInboxItemView,
    FleetLocalSessionView,
    FleetRecentActivityView,
    FleetRepoGroupView,
    FleetResourceView,
    FleetSearchHelperView,
    FleetSearchHitView,
    FleetState,
    FleetStoryLaneView,
)
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.widgets.fleet import (
    FleetCommandDeckPanel,
    FleetHistoryPanel,
    FleetInboxPanel,
    FleetLocalSessionsPanel,
    FleetSearchPanel,
    FleetStoryLanesPanel,
    FleetSummaryBar,
)

_TS = datetime(2025, 2, 1, 12, tzinfo=UTC)


def _agent() -> FleetAgentSummaryView:
    return FleetAgentSummaryView(
        agent_id="agent-1",
        name="Planner",
        status=AgentStatus.WAITING_INPUT,
        repo_key="root:/repo",
        repo_label="repo",
        repo_root="/repo",
        worktree_name="planner",
        branch="task/planner",
        task_title="Plan fleet insights",
        session_label="copilot-1",
        session_count=2,
        needs_attention=True,
        attention_summary="waiting for operator",
        last_update_at=_TS,
        idle_seconds=120,
        worktree_dirty=True,
        worktree_locked=False,
        token_total=240,
        estimated_cost_usd=Decimal("0.75"),
    )


def _local_session(*, orphan: bool = True) -> FleetLocalSessionView:
    return FleetLocalSessionView(
        session_id="local-1",
        repo_key="root:/repo",
        repo_label="repo",
        repo_root="/repo",
        summary="Inspect orphan session",
        branch="task/orphan",
        worktree_name="orphan",
        origin="local",
        updated_at=_TS,
        last_event_at=_TS,
        last_event_type="tool.execution_complete",
        checkpoint_count=3,
        is_cleanly_closed=False,
        is_orphan=orphan,
        linked_agent_id=None if orphan else "agent-1",
        linked_agent_name=None if orphan else "Planner",
        token_total=88,
    )


def _state() -> FleetState:
    group = FleetRepoGroupView(
        repo_key="root:/repo",
        repo_label="repo",
        repo_root="/repo",
        agent_count=1,
        active_count=1,
        attention_count=1,
        worktree_count=1,
        dirty_worktree_count=1,
        locked_worktree_count=0,
        session_count=2,
        orphan_local_session_count=1,
        token_total=240,
        estimated_cost_usd=Decimal("0.75"),
        agents=(_agent(),),
        open_session_count=1,
        local_session_count=1,
        unclosed_local_session_count=1,
    )
    local_session = _local_session()
    return FleetState(
        generated_at=_TS,
        filters=FleetFilterState(text_query="planner"),
        total_visible_agents=1,
        total_groups=1,
        health=FleetHealthSummary(
            tone="warning",
            message="review pending issues",
            total_agents=1,
            active_agents=1,
            attention_agents=1,
            waiting_agents=1,
            blocked_agents=0,
            error_agents=0,
            total_worktrees=1,
            dirty_worktrees=1,
            orphan_local_sessions=1,
        ),
        groups=(group,),
        history_metrics=(
            FleetHistoryMetricView(label="repos", value="1", detail="1 dirty · 1 worktrees"),
        ),
        recent_activity=(
            FleetRecentActivityView(
                occurred_at=_TS,
                title="agent waiting input",
                detail="Planner",
                severity="warning",
                repo_key="root:/repo",
                repo_label="repo",
                story_key="story:plan-fleet-insights",
                story_label="Plan fleet insights",
            ),
        ),
        search_hits=(
            FleetSearchHitView(
                kind="agent",
                title="Planner · Plan fleet insights",
                detail="repo · planner · task/planner",
            ),
        ),
        search_helpers=(
            FleetSearchHelperView(
                label="waiting input",
                query="waiting",
                detail="find agents paused for input",
                match_count=1,
            ),
        ),
        resources=(
            FleetResourceView(
                label="repos",
                value="1",
                detail="visible repo buckets",
                tone="healthy",
            ),
        ),
        local_sessions=(local_session,),
        story_lanes=(
            FleetStoryLaneView(
                story_key="story:plan-fleet-insights",
                story_label="Plan fleet insights",
                repo_keys=("root:/repo",),
                repo_labels=("repo",),
                agent_ids=("agent-1",),
                session_ids=("session-1",),
                local_session_ids=("local-1",),
                live_agent_count=1,
                waiting_agent_count=1,
                attention_count=1,
                blocked_count=0,
                open_session_count=1,
                local_session_count=1,
                orphan_local_session_count=1,
                inbox_count=2,
                latest_update_at=_TS,
                next_action="reply",
            ),
        ),
        response_inbox=(
            FleetInboxItemView(
                story_key="story:plan-fleet-insights",
                story_label="Plan fleet insights",
                repo_label="repo",
                source_kind="agent",
                source_label="Planner",
                reason="waiting for operator",
                occurred_at=_TS,
                severity="warning",
                suggested_action="reply",
                agent_id="agent-1",
            ),
            FleetInboxItemView(
                story_key="story:plan-fleet-insights",
                story_label="Plan fleet insights",
                repo_label="repo",
                source_kind="local",
                source_label="Inspect orphan session",
                reason="orphan local session can be resumed or archived",
                occurred_at=_TS,
                severity="warning",
                suggested_action="recover",
                local_session_id="local-1",
            ),
        ),
    )


def _render(widget: object) -> str:
    renderable = widget.render()
    return renderable.plain if hasattr(renderable, "plain") else str(renderable)


def test_summary_bar_renders_story_and_waiting_counts() -> None:
    panel = FleetSummaryBar()
    panel.set_state(_state())

    rendered = _render(panel).lower()
    assert "fleet command center" in rendered
    assert "1 story" in rendered
    assert "2 waiting items" in rendered
    assert "1 live agent" in rendered


def test_story_lanes_panel_renders_columns_and_selection() -> None:
    state = _state()
    lanes = FleetStoryLanesPanel(widget_id="fleet-stories")
    lanes.set_lanes(state.story_lanes, selected_story_key="story:plan-fleet-insights")

    table = lanes._build_table()

    assert len(table.columns) == 6
    assert "plan fleet insights" in str(table.columns[1]._cells[0]).lower()
    assert "2 act" in str(table.columns[4]._cells[0]).lower()
    assert lanes.current_story_key == "story:plan-fleet-insights"


def test_command_deck_inbox_local_sessions_history_and_search_render_content() -> None:
    state = _state()
    command = FleetCommandDeckPanel()
    inbox = FleetInboxPanel()
    sessions = FleetLocalSessionsPanel()
    history = FleetHistoryPanel()
    search = FleetSearchPanel()

    command.set_story(
        state.story_lanes[0],
        repo_groups=state.groups,
        agents=state.groups[0].agents,
        inbox_items=state.response_inbox,
    )
    inbox.set_inbox(
        items=state.response_inbox,
        selected_story_key=state.story_lanes[0].story_key,
        selected_story_label=state.story_lanes[0].story_label,
    )
    sessions.set_sessions(
        scope_label=state.story_lanes[0].story_label,
        sessions=state.local_sessions,
    )
    history.set_history(
        state.history_metrics,
        state.recent_activity,
        scope_label=state.story_lanes[0].story_label,
    )
    search.set_search(
        query=state.filters.normalized_query(),
        helpers=state.search_helpers,
        hits=state.search_hits,
    )

    command_rendered = _render(command).lower()
    assert "command deck" in command_rendered
    assert "plan fleet insights" in command_rendered
    assert "next moves" in command_rendered
    assert "reply" in command_rendered
    inbox_rendered = _render(inbox).lower()
    assert "response inbox" in inbox_rendered
    assert "planner" in inbox_rendered
    assert "recover" in inbox_rendered
    sessions_rendered = _render(sessions).lower()
    assert "orphan" in sessions_rendered
    assert "tool.execution_complete" in sessions_rendered
    history_rendered = _render(history).lower()
    assert "recent story activity · plan fleet insights" in history_rendered
    assert "agent waiting input" in history_rendered
    search_rendered = _render(search).lower()
    assert "filters / search" in search_rendered
    assert "waiting input" in search_rendered
    assert "planner · plan fleet insights" in search_rendered
