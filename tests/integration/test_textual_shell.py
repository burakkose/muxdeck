from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from textual.widgets import Input

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
    WorktreeActionView,
    WorktreeConflictView,
    WorktreeDetailView,
    WorktreeStartAgentIntent,
    WorktreeSummaryView,
)
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.models import Session
from copilot_commander.services import SetupCheck, SetupDoctorReport, TmuxSocketOption


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
                current_activity="Planning dashboard layout",
                sparkline="▁▂▄▆█",
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
                current_activity="Reviewing logs",
                sparkline="▁▁▂▃▄▅",
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
                copilot_session_id=None,
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
                    DashboardLogLineView(
                        captured_at=timestamp,
                        source="stderr",
                        sequence_no=1,
                        content="warning line",
                    ),
                ),
                recent_events=("⚡ Running tests", "⚠ Needs input"),
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
                "kind": "interrupt",
                "label": "Interrupt agent",
                "agent": type("AgentTarget", (), {"name": agent_id})(),
                "metadata": (("pane_target", "%1"),),
            },
        )()

    def open_pane_intent(self, agent_id: str) -> object:
        return type(
            "Intent",
            (),
            {
                "kind": "open_pane",
                "label": "Open pane",
                "agent": type("AgentTarget", (), {"name": agent_id})(),
                "metadata": (("pane_target", "%1"),),
            },
        )()

    def open_worktree_intent(self, agent_id: str) -> object:
        return type(
            "Intent",
            (),
            {
                "kind": "open_worktree",
                "label": "Open worktree",
                "agent": type("AgentTarget", (), {"name": agent_id})(),
                "metadata": (("path", "/repo/worktrees/ui"),),
            },
        )()


class FakeActionService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def execute_intent(self, intent: object) -> object:
        record = cast(Any, intent)
        metadata = dict(record.metadata)
        pane_id = metadata.get("pane_target", "")
        kind = record.kind
        self.calls.append((kind, pane_id))
        message = {
            "interrupt": f"sent interrupt to pane {pane_id}",
            "open_pane": f"focused pane {pane_id}",
        }.get(kind, f"executed {kind}")
        return type(
            "ActionResult",
            (),
            {
                "success": True,
                "message": message,
                "pane_id": pane_id,
            },
        )()


class FakeWorktreeController:
    def __init__(self) -> None:
        self.create_calls: list[tuple[str, str]] = []
        self.attach_calls: list[str] = []
        self._worktrees: list[WorktreeSummaryView] = [
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
        ]

    def list_worktrees(self) -> tuple[WorktreeSummaryView, ...]:
        return tuple(self._worktrees)

    def get_worktree_detail(self, worktree_id: str) -> WorktreeDetailView:
        summary = next(
            (worktree for worktree in self._worktrees if worktree.worktree_id == worktree_id),
            self._worktrees[0],
        )
        conflicts = (
            (
                WorktreeConflictView(
                    code="orphan",
                    message="stale branch assignment",
                    path=summary.path,
                    worktree_id=summary.worktree_id,
                    agent_id="agent-1",
                    branch=summary.branch,
                ),
            )
            if summary.worktree_id == "worktree-1"
            else ()
        )
        pane_targets = ("%1",) if summary.worktree_id == "worktree-1" else ()
        return WorktreeDetailView(
            summary=summary,
            conflicts=conflicts,
            active_session_ids=("session-1",),
            pane_targets=pane_targets,
        )

    def start_agent_intent(
        self,
        worktree_id: str,
        *,
        model: str | None = None,
    ) -> WorktreeStartAgentIntent:
        summary = self.get_worktree_detail(worktree_id).summary
        return WorktreeStartAgentIntent(
            worktree_id=summary.worktree_id,
            repo_root=summary.repo_root,
            worktree_path=summary.path,
            branch=summary.branch,
            suggested_session_name="muxdeck",
            suggested_window_name=summary.branch.rsplit("/", 1)[-1],
            prompt=f"Continue work for {summary.branch}",
            model=model,
        )

    def create_worktree(
        self,
        cwd: str,
        *,
        task_title: str | None = None,
        **_: object,
    ) -> WorktreeActionView:
        title = task_title or "new worktree"
        slug = self._slugify(title)
        summary = WorktreeSummaryView(
            worktree_id=f"worktree-{len(self._worktrees) + 1}",
            repo_root=cwd,
            path=f"{cwd}/worktrees/{slug}",
            branch=f"task/{slug}",
            base_branch="main",
            is_main_worktree=False,
            is_dirty=False,
            ahead_count=0,
            behind_count=0,
            locked=False,
            assigned_agent_id=None,
            assigned_agent_name=None,
            active_session_count=0,
            context_count=0,
            has_conflicts=False,
        )
        self.create_calls.append((cwd, title))
        self._worktrees.append(summary)
        detail = self.get_worktree_detail(summary.worktree_id)
        return WorktreeActionView(
            action="create",
            message=f"created {summary.path}",
            worktree=detail,
            conflicts=(),
        )

    def attach_worktree(self, cwd_or_path: str, **_: object) -> WorktreeActionView:
        path = Path(cwd_or_path)
        repo_root = str(path.parents[1]) if len(path.parents) > 1 else str(path.parent)
        slug = path.name or "attached"
        summary = WorktreeSummaryView(
            worktree_id=f"worktree-{len(self._worktrees) + 1}",
            repo_root=repo_root,
            path=str(path),
            branch=f"task/{slug}",
            base_branch="main",
            is_main_worktree=False,
            is_dirty=False,
            ahead_count=0,
            behind_count=0,
            locked=False,
            assigned_agent_id=None,
            assigned_agent_name=None,
            active_session_count=0,
            context_count=0,
            has_conflicts=False,
        )
        self.attach_calls.append(str(path))
        self._worktrees.append(summary)
        detail = self.get_worktree_detail(summary.worktree_id)
        return WorktreeActionView(
            action="attach",
            message=f"attached {summary.path}",
            worktree=detail,
            conflicts=(),
        )

    @staticmethod
    def _slugify(value: str) -> str:
        parts = [part for part in value.casefold().split() if part]
        return "-".join(parts) or "worktree"


class FakeReplayController:
    def load_state(
        self,
        *,
        session_id: str | None = None,
        selected_index: int | None = None,
        filter_text: str = "",
        presentation: str = "parsed",
        follow_latest: bool = False,
        **_: object,
    ) -> ReplayStateView:
        timestamp = "2025-01-01T12:00:00+00:00"
        target_index = 1 if follow_latest else selected_index
        transcript = (
            ReplayTranscriptEntryView(
                ordinal=0,
                kind="event",
                timestamp=timestamp,
                label="session.created",
                severity="info",
                marker_kind=None,
                lines=("session start",),
                is_selected=(target_index == 0),
            ),
            ReplayTranscriptEntryView(
                ordinal=1,
                kind="log",
                timestamp=timestamp,
                label="waiting for operator" if presentation == "parsed" else "stdout#1",
                severity="warning" if presentation == "parsed" else None,
                marker_kind="blocking" if presentation == "parsed" else None,
                lines=(
                    ("blocking: waiting_for_confirmation",)
                    if presentation == "parsed"
                    else ("tail line",)
                ),
                is_selected=(target_index == 1),
            ),
        )
        if filter_text:
            transcript = tuple(
                entry
                for entry in transcript
                if filter_text.casefold() in (entry.label + " " + " ".join(entry.lines)).casefold()
            )
        resolved_index = target_index
        if resolved_index not in {entry.ordinal for entry in transcript}:
            resolved_index = transcript[-1].ordinal if transcript else None
        transcript = tuple(
            ReplayTranscriptEntryView(
                ordinal=entry.ordinal,
                kind=entry.kind,
                timestamp=entry.timestamp,
                label=entry.label,
                severity=entry.severity,
                marker_kind=entry.marker_kind,
                lines=entry.lines,
                is_selected=entry.ordinal == resolved_index,
            )
            for entry in transcript
        )
        return ReplayStateView(
            session_id=session_id or "session-1",
            agent_id="agent-1",
            task_title="Replay UI",
            selected_index=resolved_index,
            transcript=transcript,
            jump_markers=(
                ReplayJumpMarkerView(
                    index=0, timestamp=timestamp, label="session.created", kind="event"
                ),
                ReplayJumpMarkerView(
                    index=1,
                    timestamp=timestamp,
                    label="waiting for operator",
                    kind="blocking",
                ),
            ),
            presentation=cast(Literal["parsed", "raw"], presentation),
            filter_text=filter_text,
            follow_latest=follow_latest,
            total_entries=2,
            total_markers=2,
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
                    marker_kind=entry.marker_kind,
                    lines=entry.lines,
                    is_selected=entry.ordinal == target_index,
                )
                for entry in state.transcript
            ),
            jump_markers=state.jump_markers,
            presentation=state.presentation,
            filter_text=state.filter_text,
            follow_latest=state.follow_latest,
            total_entries=state.total_entries,
            total_markers=state.total_markers,
        )

    def jump_to_next_marker(self, state: ReplayStateView) -> ReplayStateView | None:
        return self.jump_to_marker(state, 1)

    def jump_to_previous_marker(self, state: ReplayStateView) -> ReplayStateView | None:
        return self.jump_to_marker(state, 0)

    def jump_to_next_activity(self, state: ReplayStateView) -> ReplayStateView | None:
        del state
        return None

    def jump_to_next_problem(self, state: ReplayStateView) -> ReplayStateView | None:
        return self.jump_to_marker(state, 1)

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

    def initial_playback(self, state: ReplayStateView) -> None:
        # The integration shell only verifies navigation; no playback
        # is exercised. Returning None keeps the screen in its paused,
        # bar-hidden state and matches the documented contract for
        # empty / non-applicable transcripts.
        del state
        return None


class FakeSetupService:
    def build_report(self) -> SetupDoctorReport:
        timestamp = datetime(2025, 1, 1, 12, tzinfo=UTC)
        return SetupDoctorReport(
            generated_at=timestamp,
            selected_socket_path=None,
            effective_socket_path="/tmp/tmux-1000/default",
            attached_socket_path="/tmp/tmux-1000/default",
            configured_socket_path=None,
            pane_count=4,
            socket_options=(
                TmuxSocketOption(
                    label="Auto / attached server",
                    socket_path=None,
                    note="follow the current TMUX attachment",
                    is_selected=True,
                    exists=True,
                ),
                TmuxSocketOption(
                    label="/tmp/tmux-1000/default",
                    socket_path="/tmp/tmux-1000/default",
                    note="attached, detected",
                    is_selected=False,
                    exists=True,
                ),
            ),
            checks=(
                SetupCheck(
                    key="attached-server",
                    status="ok",
                    title="attached server",
                    detail="the UI is attached to /tmp/tmux-1000/default",
                ),
                SetupCheck(
                    key="tmux-connection",
                    status="ok",
                    title="tmux connection",
                    detail="connected to a tmux server with 4 visible panes",
                ),
            ),
        )

    def select_socket(self, socket_path: str | None) -> SetupDoctorReport:
        del socket_path
        return self.build_report()


class FakeRuntime:
    def __init__(self) -> None:
        self.config = FakeConfig()
        self.store = FakeStore()
        self.dashboard = FakeDashboardController()
        self.worktrees = FakeWorktreeController()
        self.replay = FakeReplayController()
        self.replay_worker = None
        self.setup = FakeSetupService()
        self.agents = FakeAgentController()
        self.synchronizer = None
        self.sync_store = None
        self.actions = FakeActionService()


def rendered_text(widget: object) -> str:
    renderable = cast(Any, widget).render()
    return renderable.plain if hasattr(renderable, "plain") else str(renderable)


@pytest.mark.asyncio
async def test_textual_shell_navigation_and_updates() -> None:
    runtime = FakeRuntime()
    app = CommanderApp(cast(CommanderRuntime, runtime))

    async with app.run_test() as pilot:
        await pilot.pause()
        detail_text = rendered_text(app.screen.query_one("#dashboard-detail"))
        assert "Planner" in detail_text
        # Dashboard overhaul (c30552f) inlined the former ActivityPanel
        # into the agent detail render and removed the standalone
        # FleetHealthPanel from the dashboard (it now lives on the
        # operations screen). The activity line is sourced from
        # FakeDashboardController's current_activity field.
        assert "planning" in detail_text.lower()
        assert "output" in rendered_text(app.screen.query_one("#dashboard-log")).lower()

        await pilot.press("p")
        await pilot.pause()
        assert runtime.actions.calls[-1] == ("open_pane", "%1")
        assert "focused pane %1" in rendered_text(app.screen.query_one("#shell-footer")).lower()

        await pilot.press("i")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert runtime.actions.calls[-1] == ("interrupt", "%1")
        assert (
            "sent interrupt to pane %1"
            in rendered_text(app.screen.query_one("#shell-footer")).lower()
        )

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
        assert "parsed" in rendered_text(app.screen.query_one("#replay-summary"))

        app.screen.query_one("#replay-transcript-list").focus()
        await pilot.press("v")
        await pilot.pause()
        assert "stdout#1" in rendered_text(app.screen.query_one("#replay-detail"))

        await pilot.press("v")
        await pilot.pause()
        await pilot.press("slash")
        await pilot.press("o", "p", "e", "r", "a", "t", "o", "r")
        await pilot.pause()
        assert "waiting for operator" in rendered_text(app.screen.query_one("#replay-detail"))

        cast(Any, app.screen).action_focus_transcript()
        cast(Any, app.screen).action_jump_next_problem()
        await pilot.pause()
        assert "jumped to problem" in rendered_text(app.screen.query_one("#shell-footer")).lower()

        app.screen.query_one("#replay-transcript-list").focus()
        await pilot.press("E")
        await pilot.pause()
        assert "export json" in rendered_text(app.screen.query_one("#shell-footer")).lower()

        app.action_show_help()
        await pilot.pause()
        assert "Copilot Commander" in rendered_text(app.screen.query_one("#help-content"))

        app.action_show_setup()
        await pilot.pause()
        assert "/tmp/tmux-1000/default" in rendered_text(app.screen.query_one("#setup-summary"))


@pytest.mark.asyncio
async def test_worktrees_screen_can_create_and_select_existing_worktrees() -> None:
    runtime = FakeRuntime()
    app = CommanderApp(cast(CommanderRuntime, runtime))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_worktrees()
        await pilot.pause()

        await pilot.press("c")
        await pilot.pause()
        app.screen.query_one("#create-worktree-title", Input).value = "New task"
        await pilot.press("enter")
        await pilot.pause()

        assert runtime.worktrees.create_calls == [("/repo", "New task")]
        assert "task/new-task" in rendered_text(app.screen.query_one("#worktrees-detail"))
        assert (
            "created /repo/worktrees/new-task"
            in rendered_text(app.screen.query_one("#shell-footer")).lower()
        )

        await pilot.press("a")
        await pilot.pause()
        app.screen.query_one("#attach-worktree-path", Input).value = "/repo/worktrees/ops"
        await pilot.press("enter")
        await pilot.pause()

        assert runtime.worktrees.attach_calls == ["/repo/worktrees/ops"]
        assert "/repo/worktrees/ops" in rendered_text(app.screen.query_one("#worktrees-detail"))
        assert (
            "attached /repo/worktrees/ops"
            in rendered_text(app.screen.query_one("#shell-footer")).lower()
        )
