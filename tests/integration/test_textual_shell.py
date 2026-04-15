from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast

import pytest

from copilot_commander.app import CommanderApp, CommanderRuntime
from copilot_commander.controllers import (
    DashboardAgentListItemView,
    DashboardAlertView,
    DashboardFilterState,
    DashboardHealthSummary,
    DashboardLogLineView,
    DashboardMetricView,
    DashboardSelectedAgentView,
    DashboardSort,
    DashboardState,
    ReplayExportIntent,
    ReplayJumpMarkerView,
    ReplayStateView,
    ReplayTranscriptEntryView,
    WorktreeConflictView,
    WorktreeDetailView,
    WorktreeStartAgentIntent,
    WorktreeSummaryView,
)
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.models import Session


class FakeConfig:
    class General:
        discovery_interval_sec = 60
        log_preview_lines = 8

    general = General()


class FakeStore:
    def __init__(self) -> None:
        timestamp = datetime(2025, 1, 1, 12, tzinfo=UTC)
        self.sessions: dict[str, Session] = {
            "session-1": Session(id="session-1", agent_id="agent-1", created_at=timestamp),
            "session-2": Session(id="session-2", agent_id="agent-2", created_at=timestamp),
        }

    def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def list_sessions(self, agent_id: str | None = None) -> tuple[Session, ...]:
        sessions = tuple(self.sessions.values())
        if agent_id is None:
            return sessions
        return tuple(session for session in sessions if session.agent_id == agent_id)


class FakeDashboardController:
    def build_state(
        self,
        *,
        filters: DashboardFilterState | None = None,
        sort: DashboardSort | None = None,
        selected_agent_id: str | None = None,
        preview_line_limit: int = 8,
        alert_limit: int = 5,
    ) -> DashboardState:
        del preview_line_limit, alert_limit, sort
        timestamp = datetime(2025, 1, 1, 12, tzinfo=UTC)
        agents: tuple[DashboardAgentListItemView, ...] = (
            DashboardAgentListItemView(
                agent_id="agent-1",
                name="Planner",
                status=AgentStatus.RUNNING,
                repo_name="repo",
                branch="task/planner",
                worktree_name="planner",
                pane_id="%1",
                task_title="Plan UI shell",
                worktree_path="/repo/planner",
                latest_session_id="session-1",
                last_event_kind="agent.updated",
                last_log_at=timestamp,
                last_seen_at=timestamp,
                started_at=timestamp,
                idle_seconds=5,
                needs_attention=False,
                attention_reason=None,
                token_total=120,
                estimated_cost_usd="0.120000",
            ),
            DashboardAgentListItemView(
                agent_id="agent-2",
                name="Reviewer",
                status=AgentStatus.WAITING_INPUT,
                repo_name="repo",
                branch="task/reviewer",
                worktree_name="reviewer",
                pane_id="%2",
                task_title="Review logs",
                worktree_path="/repo/reviewer",
                latest_session_id="session-2",
                last_event_kind="agent.blocked",
                last_log_at=timestamp,
                last_seen_at=timestamp,
                started_at=timestamp,
                idle_seconds=80,
                needs_attention=True,
                attention_reason="waiting for operator",
                token_total=33,
                estimated_cost_usd="0.033000",
            ),
        )
        if filters and filters.normalized_query() == "planner":
            agents = (agents[0],)
        selected = next(
            (agent for agent in agents if agent.agent_id == selected_agent_id), agents[0]
        )
        return DashboardState(
            generated_at=timestamp,
            metrics=(
                DashboardMetricView(key="agents", label="Agents", value=2),
                DashboardMetricView(key="active", label="Active", value=2),
            ),
            filters=filters or DashboardFilterState(),
            sort=DashboardSort(),
            health=DashboardHealthSummary(
                tone="warning",
                message="some agents need review",
                total_agents=2,
                active_agents=2,
                attention_agents=1,
                waiting_input_agents=1,
                blocked_agents=0,
                error_agents=0,
            ),
            alerts=(
                DashboardAlertView(
                    agent_id="agent-2",
                    agent_name="Reviewer",
                    severity="warning",
                    title="waiting_input",
                    message="waiting for operator",
                    occurred_at=timestamp,
                ),
            ),
            agents=agents,
            selected_agent_id=selected.agent_id,
            selected_agent=DashboardSelectedAgentView(
                item=selected,
                repo_root="/repo",
                worktree_id="worktree-1",
                session_count=2,
                open_session_id=selected.latest_session_id,
                latest_event_kind=selected.last_event_kind,
                latest_event_severity="warning" if selected.needs_attention else "info",
                latest_event_at=timestamp,
                log_preview=(
                    DashboardLogLineView(
                        captured_at=timestamp,
                        source="stdout",
                        sequence_no=0,
                        content="tail line",
                    ),
                ),
            ),
        )


class FakeAgentController:
    def mark_complete(self, agent_id: str) -> object:
        return type(
            "Result",
            (),
            {
                "agent": type("AgentTarget", (), {"name": agent_id})(),
                "session_id": "session-1",
                "session_ended": True,
            },
        )()

    def interrupt_intent(self, agent_id: str) -> object:
        return type(
            "Intent",
            (),
            {
                "label": "Interrupt agent",
                "agent": type("AgentTarget", (), {"name": agent_id})(),
                "metadata": (("pane_target", "%1"),),
            },
        )()

    open_pane_intent = interrupt_intent
    open_worktree_intent = interrupt_intent


class FakeWorktreeController:
    def list_worktrees(self) -> tuple[WorktreeSummaryView, ...]:
        return (
            WorktreeSummaryView(
                worktree_id="worktree-1",
                repo_root="/repo",
                path="/repo/worktrees/ui",
                branch="task/ui",
                base_branch="main",
                is_main_worktree=False,
                is_dirty=False,
                ahead_count=1,
                behind_count=0,
                locked=False,
                assigned_agent_id="agent-1",
                assigned_agent_name="Planner",
                active_session_count=1,
                context_count=1,
                has_conflicts=True,
            ),
        )

    def get_worktree_detail(self, worktree_id: str) -> WorktreeDetailView:
        del worktree_id
        summary = self.list_worktrees()[0]
        return WorktreeDetailView(
            summary=summary,
            conflicts=(
                WorktreeConflictView(
                    code="orphan",
                    message="stale branch assignment",
                    path=summary.path,
                    worktree_id=summary.worktree_id,
                    agent_id="agent-1",
                    branch=summary.branch,
                ),
            ),
            active_session_ids=("session-1",),
            pane_targets=("%1",),
        )

    def start_agent_intent(
        self,
        worktree_id: str,
        *,
        model: str | None = None,
    ) -> WorktreeStartAgentIntent:
        del worktree_id
        return WorktreeStartAgentIntent(
            worktree_id="worktree-1",
            repo_root="/repo",
            worktree_path="/repo/worktrees/ui",
            branch="task/ui",
            suggested_session_name="muxdeck",
            suggested_window_name="ui",
            prompt="Continue work for task/ui",
            model=model,
        )


class FakeReplayController:
    def load_state(
        self,
        *,
        session_id: str | None = None,
        selected_index: int | None = None,
        **_: object,
    ) -> ReplayStateView:
        timestamp = "2025-01-01T12:00:00+00:00"
        return ReplayStateView(
            session_id=session_id or "session-1",
            agent_id="agent-1",
            task_title="Replay UI",
            selected_index=selected_index,
            transcript=(
                ReplayTranscriptEntryView(
                    ordinal=0,
                    kind="event",
                    timestamp=timestamp,
                    label="session.created",
                    severity="info",
                    lines=("session start",),
                    is_selected=(selected_index == 0),
                ),
                ReplayTranscriptEntryView(
                    ordinal=1,
                    kind="log",
                    timestamp=timestamp,
                    label="stdout#1",
                    severity=None,
                    lines=("tail line",),
                    is_selected=(selected_index == 1),
                ),
            ),
            jump_markers=(
                ReplayJumpMarkerView(
                    index=0, timestamp=timestamp, label="session.created", kind="event"
                ),
                ReplayJumpMarkerView(index=1, timestamp=timestamp, label="stdout", kind="log"),
            ),
        )

    def jump_to_marker(
        self,
        state: ReplayStateView,
        marker_ordinal: int,
    ) -> ReplayStateView:
        target_index = state.jump_markers[marker_ordinal].index
        return ReplayStateView(
            session_id=state.session_id,
            agent_id=state.agent_id,
            task_title=state.task_title,
            selected_index=target_index,
            transcript=tuple(
                ReplayTranscriptEntryView(
                    ordinal=entry.ordinal,
                    kind=entry.kind,
                    timestamp=entry.timestamp,
                    label=entry.label,
                    severity=entry.severity,
                    lines=entry.lines,
                    is_selected=entry.ordinal == target_index,
                )
                for entry in state.transcript
            ),
            jump_markers=state.jump_markers,
        )

    def build_export_intent(
        self,
        state: ReplayStateView,
        *,
        export_format: Literal["text", "json"] = "text",
    ) -> ReplayExportIntent:
        return ReplayExportIntent(
            session_id=state.session_id,
            format=export_format,
            filename_hint=f"replay-{state.session_id}.{export_format}",
            content="payload",
        )


class FakeRuntime:
    def __init__(self) -> None:
        self.config = FakeConfig()
        self.store = FakeStore()
        self.dashboard = FakeDashboardController()
        self.worktrees = FakeWorktreeController()
        self.replay = FakeReplayController()
        self.agents = FakeAgentController()
        self.synchronizer = None
        self.sync_store = None
        self.actions = None


def rendered_text(widget: object) -> str:
    renderable = cast(Any, widget).render()
    return renderable.plain if hasattr(renderable, "plain") else str(renderable)


@pytest.mark.asyncio
async def test_textual_shell_navigation_and_updates() -> None:
    app = CommanderApp(cast(CommanderRuntime, FakeRuntime()))

    async with app.run_test() as pilot:
        await pilot.pause()
        assert "Planner" in rendered_text(app.screen.query_one("#dashboard-detail"))

        await pilot.press("slash")
        await pilot.press("p", "l", "a", "n", "n", "e", "r")
        await pilot.pause()
        assert "1 agents" in rendered_text(app.screen.query_one("#shell-footer"))

        app.action_show_worktrees()
        await pilot.pause()
        assert "task/ui" in rendered_text(app.screen.query_one("#worktrees-detail"))

        await pilot.press("s")
        await pilot.pause()
        assert "Continue work for task/ui" in rendered_text(
            app.screen.query_one("#worktrees-intent")
        )

        app.action_show_replay()
        await pilot.pause()
        assert "session-1" in rendered_text(app.screen.query_one("#replay-summary"))

        await pilot.press("e")
        await pilot.pause()
        assert "export json" in rendered_text(app.screen.query_one("#shell-footer")).lower()

        app.action_show_help()
        await pilot.pause()
        assert "Copilot Commander" in rendered_text(app.screen.query_one("#help-content"))
