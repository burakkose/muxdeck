from __future__ import annotations

import shutil
import threading
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
    SessionDetailView,
    SessionListItemView,
    SessionsState,
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
    class Paths:
        def __init__(self, state_dir: Path) -> None:
            self.state_dir = state_dir

    class General:
        discovery_interval_sec = 60
        log_preview_lines = 8

    def __init__(self, state_dir: Path) -> None:
        self.general = self.General()
        self.paths = self.Paths(state_dir)


class FakeStore:
    def __init__(self) -> None:
        timestamp = datetime(2025, 1, 1, 12, tzinfo=UTC)
        self.sessions: dict[str, Session] = {
            "session-1": Session(id="session-1", agent_id="agent-1", created_at=timestamp),
            "session-2": Session(id="session-2", agent_id="agent-2", created_at=timestamp),
        }
        self.list_agents_calls = 0

    def list_agents(self) -> tuple[object, ...]:
        self.list_agents_calls += 1
        return (
            type(
                "AgentRecord",
                (),
                {
                    "copilot_session_id": "session-1",
                    "tmux_pane_id": "%1",
                    "tmux_window_id": "@1",
                    "tmux_session_name": "muxdeck",
                },
            )(),
        )

    def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def list_sessions(self, agent_id: str | None = None) -> tuple[Session, ...]:
        sessions = tuple(self.sessions.values())
        if agent_id is None:
            return sessions
        return tuple(session for session in sessions if session.agent_id == agent_id)


class ThreadBoundFakeStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self._owner_thread = threading.get_ident()

    def list_agents(self) -> tuple[object, ...]:
        if threading.get_ident() != self._owner_thread:
            msg = "SQLite objects created in a thread can only be used in that same thread."
            raise RuntimeError(msg)
        return super().list_agents()


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

    def rename_window_intent(self, agent_id: str, *, new_name: str) -> object:
        return type(
            "Intent",
            (),
            {
                "kind": "rename_window",
                "label": "Rename window",
                "agent": type("AgentTarget", (), {"name": agent_id})(),
                "metadata": (("window_target", "@1"), ("window_name", new_name)),
            },
        )()

    def move_to_window_intent(
        self,
        agent_id: str,
        *,
        target_window: str | None = None,
        new_window_name: str | None = None,
    ) -> object:
        metadata: list[tuple[str, str]] = [("pane_target", "%1")]
        if target_window is not None:
            metadata.append(("window_target", target_window))
        if new_window_name is not None:
            metadata.append(("new_window_name", new_window_name))
        return type(
            "Intent",
            (),
            {
                "kind": "move_to_window",
                "label": "Move to window",
                "agent": type("AgentTarget", (), {"name": agent_id})(),
                "metadata": tuple(metadata),
            },
        )()

    def kill_pane_intent(self, agent_id: str) -> object:
        return type(
            "Intent",
            (),
            {
                "kind": "kill_pane",
                "label": "Kill pane",
                "agent": type("AgentTarget", (), {"name": agent_id})(),
                "metadata": (("pane_target", "%1"),),
            },
        )()


class FakeActionService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def execute_intent(self, intent: object) -> object:
        record = cast(Any, intent)
        metadata = dict(record.metadata)
        pane_id = metadata.get("pane_target", "")
        kind = record.kind
        self.calls.append((kind, pane_id))
        message = {
            "interrupt": f"sent interrupt to pane {pane_id}",
            "open_pane": f"focused pane {pane_id}",
            "kill_pane": f"killed pane {pane_id}",
            "rename_window": "renamed window @1 to ui-agent",
            "move_to_window": f"moved pane {pane_id} to review",
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

    def launch_model_hint(self) -> object:
        return type(
            "ActionModelHint",
            (),
            {
                "configured_model": "gpt-5.4",
                "message": (
                    "Configured model: gpt-5.4. "
                    "Model availability depends on your Copilot account/provider. "
                    "Enter a model manually or leave it blank to use Copilot's default."
                ),
            },
        )()

    def start_agent(
        self,
        *,
        cwd: Path,
        model: str | None = None,
        window_name: str | None = None,
        target_session: str | None = None,
        prompt: str | None = None,
    ) -> object:
        self.calls.append(("start_agent", str(cwd), model, window_name, target_session, prompt))
        return type(
            "ActionResult",
            (),
            {
                "success": True,
                "message": f"started agent in %10 ({window_name or 'copilot'})",
                "pane_id": "%10",
            },
        )()

    def window_choices(self) -> tuple[object, ...]:
        return ()

    def resume_session(
        self,
        session_id: str,
        *,
        cwd: Path | None = None,
        window_name: str | None = None,
        origin: str = "local",
        windows_cwd: str | None = None,
    ) -> object:
        del cwd, window_name, origin, windows_cwd
        self.calls.append(("resume_session", session_id))
        return type(
            "ActionResult",
            (),
            {
                "success": True,
                "message": f"resumed session {session_id}",
                "pane_id": "%9",
            },
        )()

    def focus_pane(
        self,
        pane_id: str,
        *,
        window_id: str | None = None,
        session_name: str | None = None,
    ) -> object:
        del window_id, session_name
        self.calls.append(("focus_pane", pane_id))
        return type(
            "ActionResult",
            (),
            {
                "success": True,
                "message": f"focused pane {pane_id}",
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
        prompt: str | None = None,
        model: str | None = None,
        target_session_name: str | None = None,
        window_name: str | None = None,
    ) -> WorktreeStartAgentIntent:
        summary = self.get_worktree_detail(worktree_id).summary
        suggested_window_name = summary.branch.rsplit("/", 1)[-1]
        return WorktreeStartAgentIntent(
            worktree_id=summary.worktree_id,
            repo_root=summary.repo_root,
            worktree_path=summary.path,
            branch=summary.branch,
            suggested_session_name=target_session_name or "muxdeck",
            suggested_window_name=window_name or suggested_window_name,
            prompt=prompt or f"Continue work for {summary.branch}",
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


class FakeSessionsController:
    def __init__(self) -> None:
        self.build_state_calls = 0
        self._details = {
            "session-1": SessionDetailView(
                session_id="session-1",
                summary="Plan UI shell",
                repository="repo",
                branch="task/planner",
                cwd="/repo/planner",
                git_root="/repo",
                status="active",
                status_glyph="🟢",
                created_at="5m ago",
                updated_at="10s ago",
                last_event_type="agent.updated",
                last_event_at="10s ago",
                checkpoint_count=2,
                is_resumable=True,
                resume_command="copilot --resume=session-1",
            ),
            "session-2": SessionDetailView(
                session_id="session-2",
                summary="Review logs",
                repository="repo",
                branch="task/reviewer",
                cwd="/repo/reviewer",
                git_root="/repo",
                status="completed",
                status_glyph="⚪",
                created_at="20m ago",
                updated_at="2m ago",
                last_event_type="session.shutdown",
                last_event_at="2m ago",
                checkpoint_count=1,
                is_resumable=False,
                resume_command="copilot --resume=session-2",
            ),
        }

    def build_state(
        self,
        *,
        live_session_ids: frozenset[str] = frozenset(),
        selected_session_id: str | None = None,
        filter_text: str = "",
        show_completed: bool = True,
    ) -> SessionsState:
        self.build_state_calls += 1
        items = (
            SessionListItemView(
                session_id="session-1",
                summary="Plan UI shell",
                repository="repo",
                branch="task/planner",
                status="active",
                status_glyph="🟢",
                updated="10s ago",
                created="5m ago",
                checkpoint_count=2,
                last_event_type="agent.updated",
                cwd="/repo/planner",
                is_resumable=True,
            ),
            SessionListItemView(
                session_id="session-2",
                summary="Review logs",
                repository="repo",
                branch="task/reviewer",
                status="completed",
                status_glyph="⚪",
                updated="2m ago",
                created="20m ago",
                checkpoint_count=1,
                last_event_type="session.shutdown",
                cwd="/repo/reviewer",
                is_resumable=False,
            ),
        )
        visible = tuple(
            item
            for item in items
            if (show_completed or item.status != "completed")
            and (
                not filter_text
                or filter_text.casefold()
                in " ".join(
                    (item.summary, item.repository, item.branch, item.session_id)
                ).casefold()
            )
        )
        visible_ids = {item.session_id for item in visible}
        resolved = selected_session_id if selected_session_id in visible_ids else None
        if resolved is None and visible:
            resolved = visible[0].session_id
        selected = self._details.get(resolved) if resolved is not None else None
        return SessionsState(
            sessions=visible,
            selected=selected,
            total_count=2,
            active_count=len(live_session_ids),
            unclosed_count=0,
            completed_count=1,
            selected_session_id=resolved,
        )

    def get_session_detail(
        self,
        session_id: str | None,
        *,
        live_session_ids: frozenset[str] = frozenset(),
    ) -> SessionDetailView | None:
        del live_session_ids
        if session_id is None:
            return None
        return self._details.get(session_id)


class FakeReplayController:
    def __init__(self) -> None:
        self.load_state_calls = 0

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
        self.load_state_calls += 1
        timestamp = "2025-01-01T12:00:00+00:00"
        target_index = 1 if follow_latest else selected_index
        transcript: tuple[ReplayTranscriptEntryView, ...] = (
            ReplayTranscriptEntryView(
                ordinal=0,
                kind="event",
                timestamp=timestamp,
                label="session.created",
                severity="info",
                marker_kind=None,
                lines=("session start",),
                is_selected=(target_index == 0),
                raw_lines=("session start",),
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
                raw_lines=("waiting for operator", "tail line"),
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
                raw_lines=entry.raw_lines,
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
            session_ids=((session_id or "session-1"),),
            agent_ids=("agent-1",),
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
                    raw_lines=entry.raw_lines,
                )
                for entry in state.transcript
            ),
            jump_markers=state.jump_markers,
            presentation=state.presentation,
            filter_text=state.filter_text,
            follow_latest=state.follow_latest,
            total_entries=state.total_entries,
            total_markers=state.total_markers,
            session_ids=state.session_ids,
            agent_ids=state.agent_ids,
            playback=state.playback,
            files_touched=state.files_touched,
            tool_calls=state.tool_calls,
            worktree_path=state.worktree_path,
            annotations=state.annotations,
            insights=state.insights,
        )

    def select_entry(self, state: ReplayStateView, ordinal: int) -> ReplayStateView:
        return ReplayStateView(
            session_id=state.session_id,
            agent_id=state.agent_id,
            task_title=state.task_title,
            selected_index=ordinal,
            transcript=tuple(
                ReplayTranscriptEntryView(
                    ordinal=entry.ordinal,
                    kind=entry.kind,
                    timestamp=entry.timestamp,
                    label=entry.label,
                    severity=entry.severity,
                    marker_kind=entry.marker_kind,
                    lines=entry.lines,
                    is_selected=entry.ordinal == ordinal,
                    raw_lines=entry.raw_lines,
                )
                for entry in state.transcript
            ),
            jump_markers=state.jump_markers,
            presentation=state.presentation,
            filter_text=state.filter_text,
            follow_latest=state.follow_latest,
            total_entries=state.total_entries,
            total_markers=state.total_markers,
            session_ids=state.session_ids,
            agent_ids=state.agent_ids,
            playback=state.playback,
            files_touched=state.files_touched,
            tool_calls=state.tool_calls,
            worktree_path=state.worktree_path,
            annotations=state.annotations,
            insights=state.insights,
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
        export_format: Literal["text", "json", "markdown"] = "text",
    ) -> ReplayExportIntent:
        return ReplayExportIntent(
            session_id=state.session_id,
            format=export_format,
            filename_hint=(
                f"replay-{state.session_id}.md"
                if export_format == "markdown"
                else f"replay-{state.session_id}.{export_format}"
            ),
            content="payload",
        )

    def initial_playback(self, state: ReplayStateView) -> None:
        # The integration shell only verifies navigation; no playback
        # is exercised. Returning None keeps the screen in its paused,
        # bar-hidden state and matches the documented contract for
        # empty / non-applicable transcripts.
        del state
        return


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
        self.runtime_dir = Path(__file__).resolve().parent / "_runtime_textual_shell"
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)
        (self.runtime_dir / "state").mkdir(parents=True, exist_ok=True)
        self.config = FakeConfig(self.runtime_dir / "state")
        self.store: FakeStore = FakeStore()
        self.dashboard = FakeDashboardController()
        self.worktrees = FakeWorktreeController()
        self.replay = FakeReplayController()
        self.replay_worker = None
        self.sessions_ctrl = FakeSessionsController()
        self.setup = FakeSetupService()
        self.agents = FakeAgentController()
        self.synchronizer = None
        self.sync_store: FakeStore | None = None
        self.actions: FakeActionService = FakeActionService()
        self.pane_stream = None

    def export_path(self, filename: str) -> Path:
        return self.config.paths.state_dir / "replay-exports" / filename

    def cleanup(self) -> None:
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)


def rendered_text(widget: object) -> str:
    renderable = cast(Any, widget).render()
    return renderable.plain if hasattr(renderable, "plain") else str(renderable)


@pytest.mark.asyncio
async def test_textual_shell_navigation_and_updates() -> None:
    runtime = FakeRuntime()
    app = CommanderApp(cast(CommanderRuntime, runtime))
    try:
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
            assert "task/ui" in rendered_text(app.screen.query_one("#launch-agent-summary"))
            assert (
                "Continue work for task/ui"
                in app.screen.query_one("#launch-agent-prompt", Input).value
            )
            assert app.screen.query_one("#launch-agent-model", Input).value == "gpt-5.4"
            assert (
                "depends on your copilot account/provider"
                in rendered_text(app.screen.query_one("#launch-agent-model-help")).lower()
            )

            await pilot.press("enter")
            await pilot.pause()
            assert cast(tuple[object, ...], runtime.actions.calls[-1]) == (
                "start_agent",
                "/repo/worktrees/ui",
                "gpt-5.4",
                "ui",
                "muxdeck",
                "Continue work for task/ui",
            )
            assert (
                "started agent in %10"
                in rendered_text(app.screen.query_one("#shell-footer")).lower()
            )

            app.action_show_replay()
            await pilot.pause()
            assert "session-1" in rendered_text(app.screen.query_one("#replay-summary"))
            assert "parsed" in rendered_text(app.screen.query_one("#replay-summary"))

            load_calls = runtime.replay.load_state_calls
            app.screen.query_one("#replay-transcript-list").focus()
            await pilot.press("k")
            await pilot.pause()
            assert runtime.replay.load_state_calls == load_calls
            assert "session.created" in rendered_text(app.screen.query_one("#replay-detail"))

            await pilot.press("j")
            await pilot.pause()
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

            app.screen.query_one("#replay-transcript-list").focus()
            await pilot.press("E")
            await pilot.pause()
            assert runtime.export_path("replay-session-1.json").exists()

            app.action_show_help()
            await pilot.pause()
            assert "Copilot Commander" in rendered_text(app.screen.query_one("#help-content"))

            app.action_show_setup()
            await pilot.pause()
            assert "/tmp/tmux-1000/default" in rendered_text(app.screen.query_one("#setup-summary"))
    finally:
        runtime.cleanup()


@pytest.mark.asyncio
async def test_worktrees_launch_uses_default_model_hint_when_actions_lack_helper() -> None:
    runtime = FakeRuntime()
    cast(Any, runtime).actions = object()
    app = CommanderApp(cast(CommanderRuntime, runtime))

    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_show_worktrees()
            await pilot.pause()

            await pilot.press("s")
            await pilot.pause()

            assert app.screen.query_one("#launch-agent-model", Input).value == ""
            assert (
                "enter a model manually"
                in rendered_text(app.screen.query_one("#launch-agent-model-help")).lower()
            )
    finally:
        runtime.cleanup()


@pytest.mark.asyncio
async def test_sessions_screen_open_replay_uses_selected_session() -> None:
    runtime = FakeRuntime()
    app = CommanderApp(cast(CommanderRuntime, runtime))

    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_show_sessions()
            await pilot.pause()

            assert "session-1" in rendered_text(app.screen.query_one("#sessions-detail"))
            assert "replay" in rendered_text(app.screen.query_one("#sessions-actions")).lower()

            await pilot.press("j")
            await pilot.pause()
            assert "session-2" in rendered_text(app.screen.query_one("#sessions-detail"))

            await pilot.press("enter")
            await pilot.pause()
            assert "session-2" in rendered_text(app.screen.query_one("#replay-summary"))
    finally:
        runtime.cleanup()


@pytest.mark.asyncio
async def test_sessions_screen_uses_sync_store_for_worker_load() -> None:
    runtime = FakeRuntime()
    runtime.store = ThreadBoundFakeStore()
    runtime.sync_store = FakeStore()
    app = CommanderApp(cast(CommanderRuntime, runtime))

    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_show_sessions()
            for _ in range(5):
                await pilot.pause()
                if not cast(Any, app.screen)._loading:
                    break

            assert "session-1" in rendered_text(app.screen.query_one("#sessions-detail"))
            assert runtime.sync_store is not None
            assert runtime.sync_store.list_agents_calls >= 1
            assert runtime.store.list_agents_calls == 0
    finally:
        runtime.cleanup()


@pytest.mark.asyncio
async def test_replay_and_sessions_skip_duplicate_initial_show_refresh() -> None:
    runtime = FakeRuntime()
    app = CommanderApp(cast(CommanderRuntime, runtime))

    try:
        async with app.run_test() as pilot:
            await pilot.pause()

            app.action_show_sessions()
            for _ in range(3):
                await pilot.pause()
                if not cast(Any, app.screen)._loading:
                    break
            assert "session-1" in rendered_text(app.screen.query_one("#sessions-detail"))
            assert runtime.sessions_ctrl.build_state_calls == 1

            app.action_show_dashboard()
            await pilot.pause()
            app.action_show_sessions()
            for _ in range(3):
                await pilot.pause()
                if not cast(Any, app.screen)._loading:
                    break
            assert runtime.sessions_ctrl.build_state_calls == 2

            app.action_show_replay()
            for _ in range(3):
                await pilot.pause()
                if not cast(Any, app.screen)._loading:
                    break
            assert "session-1" in rendered_text(app.screen.query_one("#replay-summary"))
            assert runtime.replay.load_state_calls == 1

            app.action_show_dashboard()
            await pilot.pause()
            app.action_show_replay()
            for _ in range(3):
                await pilot.pause()
                if not cast(Any, app.screen)._loading:
                    break
            assert runtime.replay.load_state_calls == 2
    finally:
        runtime.cleanup()


@pytest.mark.asyncio
async def test_worktrees_screen_can_create_and_select_existing_worktrees() -> None:
    runtime = FakeRuntime()
    app = CommanderApp(cast(CommanderRuntime, runtime))
    try:
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
    finally:
        runtime.cleanup()
