"""Widget-level tests for AgentListPanel expand/collapse behavior."""

from __future__ import annotations

from datetime import UTC, datetime

from muxdeck.controllers.dashboard_controller import (
    DashboardAgentListItemView,
    DashboardSubAgentTreeView,
    DashboardSubAgentView,
)
from muxdeck.domain.enums import AgentStatus
from muxdeck.widgets.dashboard import AgentListPanel


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


class TestAgentListPanelSubAgentNavigation:
    def test_cursor_steps_over_subagent_rows(self) -> None:
        panel = AgentListPanel()
        panel.set_agents([_agent("a1"), _agent("a2")], selected_agent_id="a1")
        panel.toggle_expand()
        panel.set_subagents(
            "a1",
            DashboardSubAgentTreeView(
                agent_id="a1",
                session_id="s",
                running=(_subagent_view("call_r1"), _subagent_view("call_r2")),
                recent=(),
            ),
        )
        # Rows are: a1, header, sub1, sub2, a2 → 5 total. The header is
        # decorative and should never hold the cursor.
        assert len(panel._rows) == 5
        assert panel.selected_agent_id == "a1"
        assert panel.selected_subagent is None
        # Step down: skip the header and land on the first sub-agent.
        panel.move_cursor(1)
        assert panel.selected_agent_id == "a1"
        sub = panel.selected_subagent
        assert sub is not None
        assert sub.tool_call_id == "call_r1"
        # Step down again: second sub-agent.
        panel.move_cursor(1)
        sub = panel.selected_subagent
        assert sub is not None
        assert sub.tool_call_id == "call_r2"
        # Step onto a2 — agent id flips to a2, subagent becomes None.
        panel.move_cursor(1)
        assert panel.selected_agent_id == "a2"
        assert panel.selected_subagent is None

    def test_collapse_from_subagent_row_snaps_back_to_parent(self) -> None:
        panel = AgentListPanel()
        panel.set_agents([_agent("a1"), _agent("a2")], selected_agent_id="a1")
        panel.toggle_expand()
        panel.set_subagents(
            "a1",
            DashboardSubAgentTreeView(
                agent_id="a1",
                session_id="s",
                running=(_subagent_view("call_r1"),),
                recent=(),
            ),
        )
        # Move cursor to sub-agent row.
        panel.move_cursor(2)
        assert panel.selected_subagent is not None
        # Toggle collapses the parent; cursor should land on a1 again.
        panel.toggle_expand()
        assert panel.selected_agent_id == "a1"
        assert panel.selected_subagent is None
        assert "a1" not in panel._expanded

    def test_completed_subagents_are_hidden(self) -> None:
        panel = AgentListPanel()
        panel.set_agents([_agent("a1")], selected_agent_id="a1")
        panel.toggle_expand()
        panel.set_subagents(
            "a1",
            DashboardSubAgentTreeView(
                agent_id="a1",
                session_id="s",
                running=(_subagent_view("call_r"),),
                recent=(_subagent_view("call_c", running=False),),
            ),
        )
        # Rows: a1, header, sub_running → recent is NOT included.
        assert len(panel._rows) == 3

    def test_header_shows_running_count_when_not_loading(self) -> None:
        panel = AgentListPanel()
        panel.set_agents([_agent("a1")], selected_agent_id="a1")
        panel.toggle_expand()
        panel.set_subagents(
            "a1",
            DashboardSubAgentTreeView(
                agent_id="a1",
                session_id="s",
                running=(_subagent_view("r1"), _subagent_view("r2"), _subagent_view("r3")),
                recent=(),
            ),
        )
        # count on the parent row.
        assert panel._running_subagent_count("a1") == 3

    def test_cursor_stays_on_subagent_across_set_agents_refresh(self) -> None:
        """Periodic refresh calls set_agents(..., selected_agent_id=parent).

        Before the fix, that would snap the cursor back up to the
        parent row, making it impossible to hover a sub-agent while
        the dashboard was polling.
        """
        panel = AgentListPanel()
        panel.set_agents([_agent("a1"), _agent("a2")], selected_agent_id="a1")
        panel.toggle_expand()
        panel.set_subagents(
            "a1",
            DashboardSubAgentTreeView(
                agent_id="a1",
                session_id="s",
                running=(_subagent_view("call_r1"), _subagent_view("call_r2")),
                recent=(),
            ),
        )
        panel.move_cursor(2)  # onto call_r1
        assert panel.selected_subagent is not None
        assert panel.selected_subagent.tool_call_id == "call_r1"
        # Simulate a periodic refresh: same agents, same selected parent.
        panel.set_agents(
            [_agent("a1", name="Renamed"), _agent("a2")],
            selected_agent_id="a1",
        )
        assert panel.selected_subagent is not None
        assert panel.selected_subagent.tool_call_id == "call_r1"

    def test_cursor_stays_on_subagent_across_set_subagents_refresh(self) -> None:
        panel = AgentListPanel()
        panel.set_agents([_agent("a1")], selected_agent_id="a1")
        panel.toggle_expand()
        panel.set_subagents(
            "a1",
            DashboardSubAgentTreeView(
                agent_id="a1",
                session_id="s",
                running=(_subagent_view("call_r1"), _subagent_view("call_r2")),
                recent=(),
            ),
        )
        panel.move_cursor(3)  # onto call_r2
        assert panel.selected_subagent is not None
        assert panel.selected_subagent.tool_call_id == "call_r2"
        # Same tree delivered again (polling) — cursor must stay put.
        panel.set_subagents(
            "a1",
            DashboardSubAgentTreeView(
                agent_id="a1",
                session_id="s",
                running=(_subagent_view("call_r1"), _subagent_view("call_r2")),
                recent=(),
            ),
        )
        assert panel.selected_subagent is not None
        assert panel.selected_subagent.tool_call_id == "call_r2"

    def test_cursor_falls_back_to_parent_when_subagent_vanishes(self) -> None:
        panel = AgentListPanel()
        panel.set_agents([_agent("a1")], selected_agent_id="a1")
        panel.toggle_expand()
        panel.set_subagents(
            "a1",
            DashboardSubAgentTreeView(
                agent_id="a1",
                session_id="s",
                running=(_subagent_view("call_gone"),),
                recent=(),
            ),
        )
        panel.move_cursor(2)
        assert panel.selected_subagent is not None
        # Tree updates — the sub-agent finished and dropped out.
        panel.set_subagents(
            "a1",
            DashboardSubAgentTreeView(agent_id="a1", session_id="s", running=(), recent=()),
        )
        # No sub-agent to hover — cursor should land on the parent,
        # not off the end of the list.
        assert panel.selected_subagent is None
        assert panel.selected_agent_id == "a1"

    def test_sub_agent_highlighted_message_fires_on_subagent_row(self) -> None:
        """Cursor on a sub-agent row should emit SubAgentHighlighted with the view."""
        panel = AgentListPanel()
        panel.set_agents([_agent("a1")], selected_agent_id="a1")
        panel.toggle_expand()
        panel.set_subagents(
            "a1",
            DashboardSubAgentTreeView(
                agent_id="a1",
                session_id="s",
                running=(_subagent_view("call_r1"),),
                recent=(),
            ),
        )
        posted: list[object] = []
        panel.post_message = posted.append  # type: ignore[assignment,method-assign]
        # Move to header then onto sub-agent row.
        panel.move_cursor(2)
        # The most recent SubAgentHighlighted should carry the sub view.
        highlights = [m for m in posted if isinstance(m, AgentListPanel.SubAgentHighlighted)]
        assert highlights, f"expected SubAgentHighlighted, got {posted!r}"
        last = highlights[-1]
        assert last.subagent is not None
        assert last.subagent.tool_call_id == "call_r1"

    def test_cursor_skips_header_row_when_moving_down(self) -> None:
        """Moving down from the parent lands on the first sub-agent, not the header."""
        panel = AgentListPanel()
        panel.set_agents([_agent("a1")], selected_agent_id="a1")
        panel.toggle_expand()
        panel.set_subagents(
            "a1",
            DashboardSubAgentTreeView(
                agent_id="a1",
                session_id="s",
                running=(_subagent_view("call_r1"),),
                recent=(),
            ),
        )
        # Rows: [a1, header, sub1]. Cursor starts on a1.
        panel.move_cursor(1)
        # Should have skipped the header and landed on sub1.
        sub = panel.selected_subagent
        assert sub is not None
        assert sub.tool_call_id == "call_r1"

    def test_cursor_skips_header_row_when_moving_up(self) -> None:
        """Moving up from a sub-agent lands on the parent, skipping the header."""
        panel = AgentListPanel()
        panel.set_agents([_agent("a1")], selected_agent_id="a1")
        panel.toggle_expand()
        panel.set_subagents(
            "a1",
            DashboardSubAgentTreeView(
                agent_id="a1",
                session_id="s",
                running=(_subagent_view("call_r1"),),
                recent=(),
            ),
        )
        panel.move_cursor(1)  # to sub1
        assert panel.selected_subagent is not None
        panel.move_cursor(-1)  # back up
        # Should have landed on the parent agent row, not the header.
        assert panel.selected_subagent is None
        assert panel.selected_agent_id == "a1"
