# ruff: noqa: E402,I001

from __future__ import annotations

from datetime import UTC, datetime
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from textual.app import App
from textual.widgets import Input

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

if TYPE_CHECKING:
    from muxdeck.app import MuxdeckRuntime

from muxdeck.controllers.fleet_controller import (
    FleetAgentSummaryView,
    FleetController,
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
from muxdeck.domain.enums import AgentStatus
from muxdeck.screens.fleet import FleetScreen
from muxdeck.widgets.fleet import FleetStoryLanesPanel

_TS = datetime(2025, 2, 1, 12, tzinfo=UTC)


class _FakeRuntime:
    pass


def _group(
    repo_key: str,
    repo_label: str,
    *,
    attention: int = 0,
    with_agent: bool = True,
) -> FleetRepoGroupView:
    return FleetRepoGroupView(
        repo_key=repo_key,
        repo_label=repo_label,
        repo_root=f"/{repo_label}",
        agent_count=1 if with_agent else 0,
        active_count=1 if with_agent else 0,
        attention_count=attention,
        worktree_count=1,
        dirty_worktree_count=1 if repo_label == "repo-b" else 0,
        locked_worktree_count=0,
        session_count=1,
        orphan_local_session_count=1 if repo_label == "repo-b" else 0,
        token_total=120 if with_agent else 0,
        estimated_cost_usd=None,
        agents=(
            (
                FleetAgentSummaryView(
                    agent_id="agent-1",
                    name="Planner",
                    status=AgentStatus.RUNNING,
                    repo_key=repo_key,
                    repo_label=repo_label,
                    repo_root=f"/{repo_label}",
                    worktree_name="planner",
                    branch="task/planner",
                    task_title="Plan shell",
                    session_label="copilot-1",
                    session_count=1,
                    needs_attention=False,
                    attention_summary=None,
                    last_update_at=_TS,
                    idle_seconds=5,
                    worktree_dirty=False,
                    worktree_locked=False,
                    token_total=120,
                    estimated_cost_usd=None,
                ),
            )
            if with_agent
            else ()
        ),
        open_session_count=1,
        local_session_count=1 if repo_label == "repo-b" else 0,
        unclosed_local_session_count=1 if repo_label == "repo-b" else 0,
    )


def _state() -> FleetState:
    return FleetState(
        generated_at=_TS,
        filters=FleetFilterState(),
        total_visible_agents=1,
        total_groups=2,
        health=FleetHealthSummary(
            tone="warning",
            message="review pending issues",
            total_agents=1,
            active_agents=1,
            attention_agents=0,
            waiting_agents=0,
            blocked_agents=0,
            error_agents=0,
            total_worktrees=2,
            dirty_worktrees=1,
            orphan_local_sessions=1,
        ),
        groups=(
            _group("root:/repo-a", "repo-a"),
            _group("root:/repo-b", "repo-b", with_agent=False),
        ),
        history_metrics=(
            FleetHistoryMetricView(label="repos", value="2", detail="1 dirty · 2 worktrees"),
        ),
        recent_activity=(
            FleetRecentActivityView(
                occurred_at=_TS,
                title="local session updated",
                detail="Investigate orphan session",
                severity="warning",
                repo_key="root:/repo-b",
                repo_label="repo-b",
                story_key="story:repo-b-focus",
                story_label="repo-b focus",
            ),
        ),
        search_hits=(
            FleetSearchHitView(
                kind="local",
                title="Investigate orphan session",
                detail="repo-b · task/orphan · orphan",
            ),
        ),
        search_helpers=(
            FleetSearchHelperView(
                label="orphan sessions",
                query="unclosed",
                detail="inspect local sessions not linked to tracked agents",
                match_count=1,
            ),
        ),
        resources=(
            FleetResourceView(
                label="local sessions",
                value="1",
                detail="linked 0 · orphan 1",
                tone="warning",
            ),
        ),
        local_sessions=(
            FleetLocalSessionView(
                session_id="local-b",
                repo_key="root:/repo-b",
                repo_label="repo-b",
                repo_root="/repo-b",
                summary="Investigate orphan session",
                branch="task/orphan",
                worktree_name="orphan",
                origin="local",
                updated_at=_TS,
                last_event_at=_TS,
                last_event_type="tool.execution_complete",
                checkpoint_count=2,
                is_cleanly_closed=False,
                is_orphan=True,
                linked_agent_id=None,
                linked_agent_name=None,
                token_total=88,
            ),
        ),
        story_lanes=(
            FleetStoryLaneView(
                story_key="story:plan-shell",
                story_label="Plan shell",
                repo_keys=("root:/repo-a",),
                repo_labels=("repo-a",),
                agent_ids=("agent-1",),
                session_ids=("session-a",),
                local_session_ids=(),
                live_agent_count=1,
                waiting_agent_count=0,
                attention_count=0,
                blocked_count=0,
                open_session_count=1,
                local_session_count=0,
                orphan_local_session_count=0,
                inbox_count=0,
                latest_update_at=_TS,
                next_action="monitor",
            ),
            FleetStoryLaneView(
                story_key="story:repo-b-focus",
                story_label="repo-b focus",
                repo_keys=("root:/repo-b",),
                repo_labels=("repo-b",),
                agent_ids=(),
                session_ids=(),
                local_session_ids=("local-b",),
                live_agent_count=0,
                waiting_agent_count=0,
                attention_count=1,
                blocked_count=0,
                open_session_count=0,
                local_session_count=1,
                orphan_local_session_count=1,
                inbox_count=1,
                latest_update_at=_TS,
                next_action="recover",
            ),
        ),
        response_inbox=(
            FleetInboxItemView(
                story_key="story:repo-b-focus",
                story_label="repo-b focus",
                repo_label="repo-b",
                source_kind="local",
                source_label="Investigate orphan session",
                reason="orphan local session can be resumed or archived",
                occurred_at=_TS,
                severity="warning",
                suggested_action="recover",
                local_session_id="local-b",
            ),
        ),
    )


class _RecordingFleetController:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.build_state_calls = 0

    def build_state(
        self,
        *,
        filters: FleetFilterState | None = None,
        activity_limit: int = 8,
        search_limit: int = 10,
    ) -> FleetState:
        del filters, activity_limit, search_limit
        self.build_state_calls += 1
        if self.fail:
            raise AssertionError("main-thread fleet controller should not be used")
        return _state()


class _FleetApp(App[None]):
    def __init__(
        self,
        controller: _RecordingFleetController,
        worker_controller: _RecordingFleetController,
    ) -> None:
        super().__init__()
        self._screen = FleetScreen(
            cast("MuxdeckRuntime", _FakeRuntime()),
            controller=cast("FleetController", controller),
            worker_controller=cast("FleetController", worker_controller),
        )

    def on_mount(self) -> None:
        self.push_screen(self._screen)


def _render(widget: object) -> str:
    renderable = widget.render()
    return renderable.plain if hasattr(renderable, "plain") else str(renderable)


@pytest.mark.asyncio
async def test_fleet_screen_uses_worker_controller_and_moves_selection() -> None:
    controller = _RecordingFleetController(fail=True)
    worker_controller = _RecordingFleetController()
    app = _FleetApp(controller, worker_controller)

    async with app.run_test() as pilot:
        for _ in range(5):
            await pilot.pause()
            if not cast(FleetScreen, app.screen)._loading:
                break

        assert controller.build_state_calls == 0
        assert worker_controller.build_state_calls >= 1
        assert "plan shell" in _render(app.screen.query_one("#fleet-command")).lower()

        await pilot.press("j")
        await pilot.pause()

        command = _render(app.screen.query_one("#fleet-command")).lower()
        sessions = _render(app.screen.query_one("#fleet-sessions")).lower()
        inbox = _render(app.screen.query_one("#fleet-inbox")).lower()
        assert "repo-b focus" in command
        assert "recover" in command
        assert "orphan" in sessions
        assert "investigate orphan session" in inbox


@pytest.mark.asyncio
async def test_fleet_screen_escape_returns_focus_to_story_list() -> None:
    controller = _RecordingFleetController(fail=True)
    worker_controller = _RecordingFleetController()
    app = _FleetApp(controller, worker_controller)

    async with app.run_test() as pilot:
        for _ in range(5):
            await pilot.pause()
            if not cast(FleetScreen, app.screen)._loading:
                break

        await pilot.press("/")
        await pilot.pause()

        filter_input = app.screen.query_one("#fleet-filter-input", Input)
        lanes = app.screen.query_one(FleetStoryLanesPanel)
        assert filter_input.has_focus

        await pilot.press("escape")
        await pilot.pause()

        assert not filter_input.has_focus
        assert lanes.has_focus
