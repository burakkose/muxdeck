from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from copilot_commander.controllers.fleet_controller import (
    FleetAgentSummaryView,
    FleetFilterState,
    FleetHealthSummary,
    FleetHistoryMetricView,
    FleetRecentActivityView,
    FleetRepoGroupView,
    FleetResourceView,
    FleetSearchHelperView,
    FleetSearchHitView,
    FleetState,
)
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.widgets.fleet import (
    FleetGroupsPanel,
    FleetHistoryPanel,
    FleetResourcesPanel,
    FleetSearchPanel,
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
    )
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
                label="attention sweep",
                query="attention",
                detail="focus agents that need operator review",
                match_count=1,
            ),
        ),
        resources=(
            FleetResourceView(
                label="repos",
                value="1",
                detail="active repo groupings in this view",
                tone="healthy",
            ),
        ),
    )


def _render(widget: object) -> str:
    renderable = widget.render()
    return renderable.plain if hasattr(renderable, "plain") else str(renderable)


def test_summary_bar_renders_health_counts() -> None:
    panel = FleetSummaryBar()
    panel.set_state(_state())

    rendered = _render(panel).lower()
    assert "review pending issues" in rendered
    assert "visible 1" in rendered
    assert "orphans 1" in rendered


def test_groups_history_search_and_resource_panels_render_content() -> None:
    state = _state()
    groups = FleetGroupsPanel()
    history = FleetHistoryPanel()
    search = FleetSearchPanel()
    resources = FleetResourcesPanel()

    groups.set_groups(state.groups)
    history.set_history(state.history_metrics, state.recent_activity)
    search.set_search(
        query=state.filters.normalized_query(),
        helpers=state.search_helpers,
        hits=state.search_hits,
    )
    resources.set_resources(state.resources)

    lead_cells = groups.renderable.columns[5]._cells
    assert any("planner" in str(cell).lower() for cell in lead_cells)
    history_rendered = _render(history).lower()
    assert "analytics" in history_rendered
    assert "agent waiting input" in history_rendered
    search_rendered = _render(search).lower()
    assert "attention sweep" in search_rendered
    assert "planner · plan fleet insights" in search_rendered
    assert "active repo groupings" in _render(resources).lower()
