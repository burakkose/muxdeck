# ruff: noqa: ANN001,ANN201,E501

"""Tests for periodic vs manual refresh screen targeting."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from copilot_commander.app import CommanderApp, CommanderRuntime
from copilot_commander.controllers import (
    DashboardAgentListItemView,
    DashboardFilterState,
    DashboardHealthSummary,
    DashboardSelectedAgentView,
    DashboardSort,
    DashboardState,
    ReplayStateView,
    WorktreeDetailView,
    WorktreeSummaryView,
)
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.models import Session
from copilot_commander.services.runtime_service import RuntimeSyncReport

_TS = datetime(2025, 1, 1, 12, tzinfo=UTC)


class _FakeConfig:
    class General:
        discovery_interval_sec = 600  # long interval to avoid auto-triggering
        log_preview_lines = 8
        idle_threshold_sec = 300

    general = General()


class _FakeStore:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {
            "session-1": Session(id="session-1", agent_id="agent-1", created_at=_TS),
        }

    def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def list_sessions(self, agent_id: str | None = None) -> tuple[Session, ...]:
        sessions = tuple(self.sessions.values())
        if agent_id is None:
            return sessions
        return tuple(s for s in sessions if s.agent_id == agent_id)


_AGENT = DashboardAgentListItemView(
    agent_id="agent-1",
    name="Agent",
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
    idle_seconds=0,
    needs_attention=False,
    attention_reason=None,
    token_total=0,
    estimated_cost_usd="0.00",
)

_DASHBOARD_STATE = DashboardState(
    generated_at=_TS,
    metrics=(),
    filters=DashboardFilterState(),
    sort=DashboardSort(),
    health=DashboardHealthSummary(
        tone="healthy",
        message="ok",
        total_agents=1,
        active_agents=1,
        attention_agents=0,
        waiting_input_agents=0,
        blocked_agents=0,
        error_agents=0,
    ),
    alerts=(),
    agents=(_AGENT,),
    selected_agent_id="agent-1",
    selected_agent=DashboardSelectedAgentView(
        item=_AGENT,
        repo_root="/repo",
        worktree_id="wt-1",
        session_count=1,
        open_session_id="session-1",
        copilot_session_id=None,
        latest_event_kind="agent.updated",
        latest_event_severity="info",
        latest_event_at=_TS,
        log_preview=(),
    ),
)


class _TrackingDashboardController:
    def __init__(self) -> None:
        self.call_count = 0

    def build_state(self, **kwargs: object) -> DashboardState:
        self.call_count += 1
        return _DASHBOARD_STATE


class _TrackingWorktreeController:
    def __init__(self) -> None:
        self.call_count = 0

    def list_worktrees(self) -> tuple[WorktreeSummaryView, ...]:
        self.call_count += 1
        return ()

    def get_worktree_detail(self, worktree_id: str) -> WorktreeDetailView | None:
        return None

    def start_agent_intent(self, worktree_id: str, *, model: str | None = None) -> None:
        return None


class _TrackingReplayController:
    def __init__(self) -> None:
        self.call_count = 0

    def load_state(self, **kwargs: object) -> ReplayStateView:
        self.call_count += 1
        return ReplayStateView(
            session_id="session-1",
            agent_id="agent-1",
            task_title="task",
            selected_index=0,
            transcript=(),
            jump_markers=(),
            presentation="parsed",
            filter_text="",
            follow_latest=False,
            total_entries=0,
            total_markers=0,
        )


class _FakeAgentController:
    def mark_complete(self, agent_id: str) -> object:
        return type(
            "R",
            (),
            {
                "agent": type("A", (), {"name": agent_id})(),
                "session_id": "s1",
                "session_ended": True,
            },
        )()

    def interrupt_intent(self, agent_id: str) -> object:
        return type(
            "I",
            (),
            {"label": "Interrupt", "agent": type("A", (), {"name": agent_id})(), "metadata": ()},
        )()

    open_pane_intent = interrupt_intent
    open_worktree_intent = interrupt_intent


class _FastSynchronizer:
    def __init__(self) -> None:
        self.call_count = 0

    def refresh(self) -> RuntimeSyncReport:
        self.call_count += 1
        return RuntimeSyncReport()


def _build_runtime(
    *,
    dashboard: _TrackingDashboardController | None = None,
    worktrees: _TrackingWorktreeController | None = None,
    replay: _TrackingReplayController | None = None,
    synchronizer: _FastSynchronizer | None = None,
) -> tuple[
    CommanderRuntime,
    _TrackingDashboardController,
    _TrackingWorktreeController,
    _TrackingReplayController,
]:
    d = dashboard or _TrackingDashboardController()
    w = worktrees or _TrackingWorktreeController()
    r = replay or _TrackingReplayController()
    s = synchronizer or _FastSynchronizer()
    runtime = cast(
        CommanderRuntime,
        type(
            "FakeRuntime",
            (),
            {
                "config": _FakeConfig(),
                "store": _FakeStore(),
                "dashboard": d,
                "worktrees": w,
                "replay": r,
                "agents": _FakeAgentController(),
                "synchronizer": s,
                "sync_store": None,
                "actions": None,
            },
        )(),
    )
    return runtime, d, w, r


@pytest.mark.asyncio
async def test_periodic_refresh_only_updates_dashboard() -> None:
    """Periodic timer should NOT call refresh_data on non-dashboard screens."""
    runtime, dashboard, worktrees, replay = _build_runtime()
    app = CommanderApp(runtime)

    async with app.run_test() as pilot:
        await pilot.pause()

        # Switch to worktrees tab
        app.switch_mode("worktrees")
        await pilot.pause()
        mount_worktree_calls = worktrees.call_count

        # Simulate periodic refresh (non-manual)
        app._refresh_current_screen()
        await pilot.pause()
        await pilot.pause()

        # Worktrees should NOT have been called again by periodic refresh
        assert worktrees.call_count == mount_worktree_calls


@pytest.mark.asyncio
async def test_manual_refresh_updates_current_screen() -> None:
    """Manual r key should refresh whatever screen is active."""
    runtime, dashboard, worktrees, replay = _build_runtime()
    app = CommanderApp(runtime)

    async with app.run_test() as pilot:
        await pilot.pause()

        # Switch to worktrees tab
        app.switch_mode("worktrees")
        await pilot.pause()
        calls_before = worktrees.call_count

        # Manual refresh (r key)
        app._refresh_current_screen(manual=True)
        await pilot.pause()
        await pilot.pause()

        # Worktrees SHOULD have been refreshed by manual action
        assert worktrees.call_count > calls_before
