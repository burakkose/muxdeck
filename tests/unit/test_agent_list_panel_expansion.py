"""Widget-level tests for AgentListPanel expand/collapse behavior."""

from __future__ import annotations

from datetime import UTC, datetime

from copilot_commander.controllers.dashboard_controller import (
    DashboardAgentListItemView,
    DashboardSubAgentTreeView,
    DashboardSubAgentView,
)
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.widgets.dashboard import AgentListPanel


def _agent(agent_id: str, name: str = "Agent") -> DashboardAgentListItemView:
    return DashboardAgentListItemView(
        agent_id=agent_id,
        name=name,
        status=AgentStatus.RUNNING,
        repo_name="repo",
        branch="main",
        worktree_name=None,
        pane_id="%1",
        task_title="task",
        worktree_path=None,
        latest_session_id=None,
        last_event_kind=None,
        last_log_at=None,
        last_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        idle_seconds=0,
        needs_attention=False,
        attention_reason=None,
        token_total=None,
        estimated_cost_usd=None,
    )


def _subagent_view(
    tool_call_id: str, display: str = "General Purpose Agent", *, running: bool = True
) -> DashboardSubAgentView:
    return DashboardSubAgentView(
        tool_call_id=tool_call_id,
        agent_name="general-purpose",
        display_name=display,
        description=None,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at=None if running else datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC),
        is_running=running,
    )


class TestAgentListPanelExpansion:
    def test_toggle_expand_marks_agent_expanded_and_requests_load(self) -> None:
        panel = AgentListPanel()
        panel.set_agents([_agent("a1"), _agent("a2")], selected_agent_id="a1")
        assert "a1" not in panel._expanded
        changed = panel.toggle_expand()
        assert changed == "a1"
        assert "a1" in panel._expanded
        # No cached tree yet → marked loading.
        assert "a1" in panel._loading

    def test_toggle_collapses_when_already_expanded(self) -> None:
        panel = AgentListPanel()
        panel.set_agents([_agent("a1")], selected_agent_id="a1")
        panel.toggle_expand()
        panel.set_subagents(
            "a1",
            DashboardSubAgentTreeView(
                agent_id="a1",
                session_id="sess",
                running=(_subagent_view("call_r"),),
                recent=(),
            ),
        )
        assert "a1" in panel._expanded
        panel.toggle_expand()
        assert "a1" not in panel._expanded

    def test_set_subagents_clears_loading_state(self) -> None:
        panel = AgentListPanel()
        panel.set_agents([_agent("a1")], selected_agent_id="a1")
        panel.toggle_expand()
        assert "a1" in panel._loading
        panel.set_subagents(
            "a1",
            DashboardSubAgentTreeView(
                agent_id="a1",
                session_id="sess",
                running=(),
                recent=(),
            ),
        )
        assert "a1" not in panel._loading
        assert "a1" in panel._subagents

    def test_set_agents_drops_expansion_for_gone_agents(self) -> None:
        """If a previously-expanded agent disappears (reaped), its

        expansion / cache entries should be garbage-collected so the
        sets don't grow unbounded.
        """
        panel = AgentListPanel()
        panel.set_agents([_agent("a1"), _agent("a2")], selected_agent_id="a1")
        panel.toggle_expand()
        panel.set_subagents(
            "a1",
            DashboardSubAgentTreeView(agent_id="a1", session_id="s", running=(), recent=()),
        )
        assert "a1" in panel._expanded
        assert "a1" in panel._subagents
        # Refresh without a1 — simulate reap.
        panel.set_agents([_agent("a2")], selected_agent_id="a2")
        assert "a1" not in panel._expanded
        assert "a1" not in panel._subagents

    def test_expanded_tree_preserved_across_refreshes(self) -> None:
        panel = AgentListPanel()
        panel.set_agents([_agent("a1")], selected_agent_id="a1")
        panel.toggle_expand()
        panel.set_subagents(
            "a1",
            DashboardSubAgentTreeView(
                agent_id="a1",
                session_id="sess",
                running=(_subagent_view("call_r"),),
                recent=(),
            ),
        )
        # Refresh with same agent list — expansion / cache must survive.
        panel.set_agents([_agent("a1", name="Renamed")], selected_agent_id="a1")
        assert "a1" in panel._expanded
        assert "a1" in panel._subagents
        assert panel._subagents["a1"].running[0].tool_call_id == "call_r"

    def test_toggle_expand_on_empty_list_returns_none(self) -> None:
        panel = AgentListPanel()
        panel.set_agents([], selected_agent_id=None)
        assert panel.toggle_expand() is None
