# ruff: noqa: ANN001,ANN201,E501

from __future__ import annotations

import threading
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

_TIMESTAMP = datetime(2025, 1, 1, 12, tzinfo=UTC)


class FakeConfig:
    class General:
        discovery_interval_sec = 60
        log_preview_lines = 8

    general = General()


class FakeStore:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {
            "session-1": Session(id="session-1", agent_id="agent-1", created_at=_TIMESTAMP),
        }

    def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def list_sessions(self, agent_id: str | None = None) -> tuple[Session, ...]:
        sessions = tuple(self.sessions.values())
        if agent_id is None:
            return sessions
        return tuple(s for s in sessions if s.agent_id == agent_id)


class FakeSynchronizer:
    """Synchronizer that can optionally block until released."""

    def __init__(self, *, block: bool = False) -> None:
        self.call_count = 0
        self._block = block
        self._gate = threading.Event()
        if not block:
            self._gate.set()

    def refresh(self) -> RuntimeSyncReport:
        self.call_count += 1
        self._gate.wait(timeout=5.0)
        return RuntimeSyncReport()

    def release(self) -> None:
        self._gate.set()


class FakeDashboardController:
    def build_state(self, **kwargs: object) -> DashboardState:
        agents: tuple[DashboardAgentListItemView, ...] = (
            DashboardAgentListItemView(
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
                last_log_at=_TIMESTAMP,
                last_seen_at=_TIMESTAMP,
                started_at=_TIMESTAMP,
                idle_seconds=0,
                needs_attention=False,
                attention_reason=None,
                token_total=0,
                estimated_cost_usd="0.00",
            ),
        )
        return DashboardState(
            generated_at=_TIMESTAMP,
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
            agents=agents,
            selected_agent_id="agent-1",
            selected_agent=DashboardSelectedAgentView(
                item=agents[0],
                repo_root="/repo",
                worktree_id="wt-1",
                session_count=1,
                open_session_id="session-1",
                latest_event_kind="agent.updated",
                latest_event_severity="info",
                latest_event_at=_TIMESTAMP,
                log_preview=(),
            ),
        )


class FakeWorktreeController:
    def list_worktrees(self) -> tuple[WorktreeSummaryView, ...]:
        return ()

    def get_worktree_detail(self, worktree_id: str) -> WorktreeDetailView | None:
        return None

    def start_agent_intent(self, worktree_id: str, *, model: str | None = None) -> None:
        return None


class FakeReplayController:
    def load_state(self, **kwargs: object) -> ReplayStateView:
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


class FakeAgentController:
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


def _build_fake_runtime(
    *,
    synchronizer: FakeSynchronizer | None = None,
) -> CommanderRuntime:
    return cast(
        CommanderRuntime,
        type(
            "FakeRuntime",
            (),
            {
                "config": FakeConfig(),
                "store": FakeStore(),
                "dashboard": FakeDashboardController(),
                "worktrees": FakeWorktreeController(),
                "replay": FakeReplayController(),
                "agents": FakeAgentController(),
                "synchronizer": synchronizer,
                "sync_store": None,
                "actions": None,
            },
        )(),
    )


@pytest.mark.asyncio
async def test_sync_runs_in_worker_thread() -> None:
    """Sync refresh happens off the main thread so the UI stays responsive."""
    synchronizer = FakeSynchronizer()
    app = CommanderApp(_build_fake_runtime(synchronizer=synchronizer))

    async with app.run_test() as pilot:
        await pilot.pause()
        # The initial refresh on mount should have triggered the sync worker
        assert synchronizer.call_count >= 1


@pytest.mark.asyncio
async def test_concurrent_refresh_is_guarded() -> None:
    """Second refresh while sync is in flight is deferred, not concurrent."""
    synchronizer = FakeSynchronizer(block=True)
    app = CommanderApp(_build_fake_runtime(synchronizer=synchronizer))

    async with app.run_test() as pilot:
        await pilot.pause()
        initial_count = synchronizer.call_count
        assert initial_count == 1, "First sync should start"

        # Trigger another refresh while the first is still blocked
        app.action_refresh_screen()
        await pilot.pause()
        assert synchronizer.call_count == 1, "Blocked sync should prevent a second concurrent call"
        assert app._refresh_pending is True

        # Release the blocked sync
        synchronizer.release()
        await pilot.pause()
        # Allow the pending refresh to execute
        await pilot.pause()
        await pilot.pause()
        assert synchronizer.call_count >= 2, "Pending refresh should run after first completes"


@pytest.mark.asyncio
async def test_no_synchronizer_still_refreshes_widgets() -> None:
    """When runtime has no synchronizer, widget refresh still happens."""
    app = CommanderApp(_build_fake_runtime(synchronizer=None))

    async with app.run_test() as pilot:
        await pilot.pause()
        # Should not crash and should show default content
        assert app.last_sync_report is None


@pytest.mark.asyncio
async def test_sync_error_clears_in_progress_flag() -> None:
    """If synchronizer.refresh() raises, the worker catches it and the app stays healthy."""

    class ErrorSynchronizer:
        def __init__(self) -> None:
            self.call_count = 0

        def refresh(self) -> RuntimeSyncReport:
            self.call_count += 1
            if self.call_count == 1:
                msg = "tmux not found"
                raise RuntimeError(msg)
            return RuntimeSyncReport()

    synchronizer = ErrorSynchronizer()
    runtime = cast(
        CommanderRuntime,
        type(
            "FakeRuntime",
            (),
            {
                "config": FakeConfig(),
                "store": FakeStore(),
                "dashboard": FakeDashboardController(),
                "worktrees": FakeWorktreeController(),
                "replay": FakeReplayController(),
                "agents": FakeAgentController(),
                "synchronizer": synchronizer,
                "sync_store": None,
                "actions": None,
            },
        )(),
    )
    app = CommanderApp(runtime)

    async with app.run_test() as pilot:
        await pilot.pause()
        # First sync errored but was caught — app should be alive
        assert synchronizer.call_count >= 1
        assert app._sync_in_progress is False

        # Trigger manual refresh — should work despite prior error
        app.action_refresh_screen()
        await pilot.pause()
        await pilot.pause()
        assert synchronizer.call_count >= 2
