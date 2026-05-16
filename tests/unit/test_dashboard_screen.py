"""Screen-level tests for ``DashboardScreen``.

These tests focus on the cheap, easily-driven branches: the early
"no agent selected" / "action service unavailable" guards in every
action method and a couple of focused happy paths through the
``mark_complete`` and message flows. Heavy SQLite + worker paths
(``refresh_data`` / ``_apply_state``) are intentionally avoided —
they are exercised through the existing controller tests.
"""

# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import dataclasses
import unittest
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from textual.app import App, ComposeResult
from textual.widgets import Input

from muxdeck.adapters.copilot_session_resolver import ResolvedCopilotTarget
from muxdeck.app import MuxdeckRuntime
from muxdeck.controllers import (
    DashboardAgentListItemView,
    DashboardSelectedAgentView,
    DashboardState,
    DashboardSubAgentTreeView,
    DashboardSubAgentView,
)
from muxdeck.controllers.agent_controller import (
    AgentActionResult,
    AgentIntentView,
    AgentTargetView,
)
from muxdeck.controllers.dashboard_controller import (
    DashboardFilterState,
    DashboardHealthSummary,
    DashboardSort,
)
from muxdeck.domain.enums import AgentStatus
from muxdeck.exceptions import PersistenceError
from muxdeck.screens.dashboard import DashboardScreen
from muxdeck.screens.message_input import MessageResult
from muxdeck.screens.window_input import MoveWindowResult, RenameWindowResult
from muxdeck.services.action_service import ActionResult
from muxdeck.services.attention_service import AttentionNotification
from muxdeck.widgets.dashboard import (
    AgentListPanel,
    FilterBar,
)

ScreenBody = Callable[["_Harness", DashboardScreen, object], Awaitable[None]]


def _agent_view(
    agent_id: str = "agent-1",
    *,
    pane_id: str = "%1",
    name: str = "agent-1",
    status: AgentStatus = AgentStatus.RUNNING,
) -> DashboardAgentListItemView:
    now = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    return DashboardAgentListItemView(
        agent_id=agent_id,
        name=name,
        status=status,
        repo_name="repo",
        branch="feature/x",
        worktree_name="wt",
        pane_id=pane_id,
        task_title="task",
        worktree_path="/repo/wt",
        latest_session_id="session-1",
        last_event_kind=None,
        last_log_at=now,
        last_seen_at=now,
        started_at=now,
        idle_seconds=0,
        needs_attention=False,
        attention_reason=None,
        token_total=None,
        estimated_cost_usd=None,
        window_name="win",
        window_id="@1",
    )


def _selected_view(
    item: DashboardAgentListItemView,
    *,
    open_session_id: str | None = "session-1",
) -> DashboardSelectedAgentView:
    return DashboardSelectedAgentView(
        item=item,
        repo_root="/repo",
        worktree_id="wt-1",
        session_count=1,
        open_session_id=open_session_id,
        copilot_session_id="session-1",
        latest_event_kind=None,
        latest_event_severity=None,
        latest_event_at=None,
        log_preview=(),
    )


def _state(
    *,
    agents: tuple[DashboardAgentListItemView, ...] = (),
    selected: DashboardSelectedAgentView | None = None,
) -> DashboardState:
    selected_id = selected.item.agent_id if selected is not None else None
    return DashboardState(
        generated_at=datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        metrics=(),
        filters=DashboardFilterState(include_completed=False),
        sort=DashboardSort(),
        health=DashboardHealthSummary(
            tone="healthy",
            message="all green",
            total_agents=len(agents),
            active_agents=sum(1 for a in agents if a.status == AgentStatus.RUNNING),
            attention_agents=0,
            waiting_input_agents=0,
            blocked_agents=0,
            error_agents=0,
        ),
        alerts=(),
        agents=agents,
        selected_agent_id=selected_id,
        selected_agent=selected,
    )


@dataclass
class _RecordingDashboardCtrl:
    state_to_return: DashboardState
    build_calls: int = 0
    subagent_calls: list[str] = field(default_factory=list)
    last_precomputed_items: tuple[DashboardAgentListItemView, ...] | None = None
    last_precomputed_selected: DashboardSelectedAgentView | None = None

    def build_state(
        self,
        *,
        filters: DashboardFilterState | None = None,
        sort: DashboardSort | None = None,
        selected_agent_id: str | None = None,
        preview_line_limit: int = 8,
        alert_limit: int = 5,
        precomputed_items: tuple[DashboardAgentListItemView, ...] | None = None,
        precomputed_selected: DashboardSelectedAgentView | None = None,
    ) -> DashboardState:
        del filters, sort, selected_agent_id, preview_line_limit, alert_limit
        self.build_calls += 1
        self.last_precomputed_items = precomputed_items
        self.last_precomputed_selected = precomputed_selected
        return self.state_to_return

    def load_subagents(self, agent_id: str) -> DashboardSubAgentTreeView:
        self.subagent_calls.append(agent_id)
        return DashboardSubAgentTreeView(
            agent_id=agent_id,
            session_id=None,
            running=(),
            recent=(),
        )

    def build_agent_items(self) -> tuple[DashboardAgentListItemView, ...]:
        return self.state_to_return.agents


@dataclass
class _RecordingAgents:
    intent_to_return: AgentIntentView | None = None
    mark_result: AgentActionResult | None = None
    mark_raises: BaseException | None = None
    interrupt_calls: list[str] = field(default_factory=list)
    kill_calls: list[str] = field(default_factory=list)
    open_pane_calls: list[str] = field(default_factory=list)
    open_worktree_calls: list[str] = field(default_factory=list)
    mark_calls: list[str] = field(default_factory=list)
    rename_calls: list[tuple[str, str]] = field(default_factory=list)
    move_calls: list[tuple[str, str | None, str | None]] = field(default_factory=list)

    def _target(self, agent_id: str) -> AgentTargetView:
        return AgentTargetView(
            agent_id=agent_id,
            name=agent_id,
            status=AgentStatus.RUNNING,
            pane_target="%1",
            worktree_path="/repo/wt",
            repo_root="/repo",
            branch="feature/x",
            latest_session_id="session-1",
        )

    def mark_complete(self, agent_id: str) -> AgentActionResult:
        self.mark_calls.append(agent_id)
        if self.mark_raises is not None:
            raise self.mark_raises
        if self.mark_result is not None:
            return self.mark_result
        target = self._target(agent_id)
        return AgentActionResult(
            action="mark_complete",
            agent=target,
            session_id="session-1",
            session_ended=True,
        )

    def interrupt_intent(self, agent_id: str) -> AgentIntentView:
        self.interrupt_calls.append(agent_id)
        return AgentIntentView(
            kind="interrupt",
            agent=self._target(agent_id),
            label="Interrupt agent",
        )

    def kill_pane_intent(self, agent_id: str) -> AgentIntentView:
        self.kill_calls.append(agent_id)
        return AgentIntentView(
            kind="kill_pane",
            agent=self._target(agent_id),
            label="Kill pane",
        )

    def open_pane_intent(self, agent_id: str) -> AgentIntentView:
        self.open_pane_calls.append(agent_id)
        return AgentIntentView(
            kind="open_pane",
            agent=self._target(agent_id),
            label="Focus console",
        )

    def open_worktree_intent(self, agent_id: str) -> AgentIntentView:
        self.open_worktree_calls.append(agent_id)
        return AgentIntentView(
            kind="open_worktree",
            agent=self._target(agent_id),
            label="Open worktree",
            metadata=(("path", "/repo/wt"),),
        )

    def rename_window_intent(
        self,
        agent_id: str,
        *,
        new_name: str,
    ) -> AgentIntentView:
        self.rename_calls.append((agent_id, new_name))
        return AgentIntentView(
            kind="rename_window",
            agent=self._target(agent_id),
            label="Rename window",
            metadata=(("new_window_name", new_name),),
        )

    def move_to_window_intent(
        self,
        agent_id: str,
        *,
        target_window: str | None = None,
        new_window_name: str | None = None,
    ) -> AgentIntentView:
        self.move_calls.append((agent_id, target_window, new_window_name))
        return AgentIntentView(
            kind="move_to_window",
            agent=self._target(agent_id),
            label="Move to window",
            metadata=(("pane_target", "%1"),),
        )


@dataclass
class _RecordingActions:
    execute_result: ActionResult = field(
        default_factory=lambda: ActionResult(success=True, message="ok")
    )
    send_message_result: ActionResult = field(
        default_factory=lambda: ActionResult(success=True, message="sent")
    )
    stop_results: list[ActionResult] = field(default_factory=list)
    execute_calls: list[str] = field(default_factory=list)
    send_calls: list[tuple[str, str]] = field(default_factory=list)
    stop_calls: list[tuple[str, ...]] = field(default_factory=list)

    def execute_intent(self, intent: AgentIntentView) -> ActionResult:
        self.execute_calls.append(intent.kind)
        return self.execute_result

    def send_message(self, pane_id: str, text: str) -> ActionResult:
        self.send_calls.append((pane_id, text))
        return self.send_message_result

    def stop_all_agents(self, pane_ids: list[str]) -> list[ActionResult]:
        self.stop_calls.append(tuple(pane_ids))
        if self.stop_results:
            return self.stop_results
        return [ActionResult(success=True, message="stopped") for _ in pane_ids]

    def window_choices(
        self,
        *,
        exclude_window_id: str | None = None,
    ) -> tuple[Any, ...]:
        del exclude_window_id
        return ()


class _MinimalGeneral:
    log_preview_lines = 8
    discovery_interval_sec = 2


class _MinimalConfig:
    general = _MinimalGeneral()


def _runtime_with(
    *,
    dashboard_ctrl: _RecordingDashboardCtrl | None = None,
    agents_ctrl: _RecordingAgents | None = None,
    actions: _RecordingActions | None = None,
    has_attention: bool = True,
    pane_stream: object | None = None,
    session_resolver: object | None = None,
    tmux: object | None = None,
    store: object | None = None,
    sync_store: object | None = None,
    attention_obj: object | None = None,
) -> MuxdeckRuntime:
    attrs: dict[str, Any] = {
        "config": _MinimalConfig(),
        "dashboard": dashboard_ctrl,
        "sync_dashboard": dashboard_ctrl,
        "agents": agents_ctrl,
        "actions": actions,
        "pane_stream": pane_stream,
        "session_resolver": session_resolver,
        "tmux": tmux,
        "store": store if store is not None else object(),
        "sync_store": sync_store,
    }
    if attention_obj is not None:
        attrs["attention"] = attention_obj
    elif has_attention:
        attrs["attention"] = type(
            "_FakeAttention",
            (),
            {"observe_dashboard_state": lambda self, state: ()},
        )()
    else:
        attrs["attention"] = None
    return cast(MuxdeckRuntime, type("_FakeRuntime", (), attrs)())


class _Harness(App[None]):
    def __init__(self, runtime: MuxdeckRuntime) -> None:
        super().__init__()
        self._runtime = runtime
        self.tab_badges: dict[str, str] = {}
        self.remembered_agents: list[str] = []
        self.remembered_sessions: list[str] = []
        self.switched_to: list[str] = []
        self.last_sync_report = None
        self.last_dashboard_state: DashboardState | None = None
        self.selected_agent_id: str | None = None
        # Mirrors ``MuxdeckApp.sync_attempted``. Defaulting to True
        # means the test harness skips the new "wait for first sync"
        # gate in ``DashboardScreen.refresh_data`` and falls through
        # to the local build path the existing tests exercise. The
        # cold-start gating logic is covered by a dedicated test that
        # explicitly sets this flag to ``False``.
        self.sync_attempted: bool = True

    def compose(self) -> ComposeResult:
        return iter(())

    def remember_agent_selection(self, agent_id: str) -> None:
        self.remembered_agents.append(agent_id)

    def remember_session_selection(self, session_id: str) -> None:
        self.remembered_sessions.append(session_id)

    def switch_mode(self, mode: str) -> None:  # type: ignore[override]
        self.switched_to.append(mode)


class DashboardScreenActionTests(unittest.TestCase):
    def _run_with_screen(
        self,
        runtime: MuxdeckRuntime,
        body: ScreenBody,
        *,
        select_agent_id: str | None = None,
        seed_state: DashboardState | None = None,
    ) -> tuple[_Harness, DashboardScreen, Any]:
        captured: dict[str, Any] = {}

        async def scenario() -> None:
            app = _Harness(runtime)
            captured["app"] = app
            async with app.run_test(size=(160, 60)) as pilot:
                screen = DashboardScreen(runtime)
                if seed_state is not None:
                    screen._state = seed_state
                screen._skip_next_show_refresh = True
                await app.push_screen(screen)
                await pilot.pause()  # type: ignore[attr-defined]
                # Set selection AFTER any worker-driven _apply_state
                # has run so the selection is not reset to the state's
                # (typically None) selected_agent_id.
                if select_agent_id is not None:
                    screen._selected_agent_id = select_agent_id
                captured["screen"] = screen
                await body(app, screen, pilot)

        asyncio.run(scenario())
        return captured["app"], captured["screen"], None

    # ── Early return guards ───────────────────────────────────────────

    def test_action_mark_complete_no_selection(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_mark_complete()

        _, screen, _ = self._run_with_screen(runtime, body)
        assert screen._status == "no agent selected"

    def test_action_interrupt_no_selection(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_interrupt_agent()

        _, screen, _ = self._run_with_screen(runtime, body)
        assert screen._status == "no agent selected"

    def test_action_kill_no_selection(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_kill_agent()

        _, screen, _ = self._run_with_screen(runtime, body)
        assert screen._status == "no agent selected"

    def test_action_view_logs_no_selection(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_view_logs()

        _, screen, _ = self._run_with_screen(runtime, body)
        assert screen._status == "no agent selected"

    def test_action_view_logs_no_state(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = None
            screen.action_view_logs()

        _, screen, _ = self._run_with_screen(runtime, body, select_agent_id="agent-1")
        assert screen._status == "no agent detail loaded"

    def test_action_view_logs_no_open_session(self) -> None:
        item = _agent_view()
        sel = _selected_view(item, open_session_id=None)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            del app
            screen._state = st
            screen.action_view_logs()

        _, screen, _ = self._run_with_screen(
            runtime, body, select_agent_id="agent-1", seed_state=st
        )
        assert screen._status == "no active session for this agent"

    def test_action_view_logs_switches_to_replay(self) -> None:
        item = _agent_view()
        sel = _selected_view(item, open_session_id="session-1")
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            screen.action_view_logs()

        app, _, _ = self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)
        assert "session-1" in app.remembered_sessions
        assert "replay" in app.switched_to

    def test_action_open_attention_inbox_unavailable(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
            has_attention=False,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_open_attention_inbox()

        _, screen, _ = self._run_with_screen(runtime, body)
        assert screen._status == "attention inbox unavailable"

    def test_action_open_attention_inbox_switches(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
            has_attention=True,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_open_attention_inbox()

        app, _, _ = self._run_with_screen(runtime, body)
        assert "attention" in app.switched_to

    def test_action_send_message_no_selection(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_send_message()

        _, screen, _ = self._run_with_screen(runtime, body)
        assert screen._status == "no agent selected"

    def test_action_send_message_no_action_service(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
            actions=None,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_send_message()

        _, screen, _ = self._run_with_screen(runtime, body, select_agent_id="agent-1")
        assert "action service unavailable" in screen._status

    def test_action_view_pane_no_selection(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_view_pane()

        _, screen, _ = self._run_with_screen(runtime, body)
        assert screen._status == "no agent selected"

    def test_action_rename_window_no_selection(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_rename_window()

        _, screen, _ = self._run_with_screen(runtime, body)
        assert screen._status == "no agent selected"

    def test_action_rename_window_no_state(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = None
            screen.action_rename_window()

        _, screen, _ = self._run_with_screen(runtime, body, select_agent_id="agent-1")
        assert screen._status == "no agent detail loaded"

    def test_action_move_to_window_no_selection(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_move_to_window()

        _, screen, _ = self._run_with_screen(runtime, body)
        assert screen._status == "no agent selected"

    def test_action_move_to_window_no_action_service(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
            actions=None,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_move_to_window()

        _, screen, _ = self._run_with_screen(runtime, body, select_agent_id="agent-1")
        assert "action service unavailable" in screen._status

    def test_action_stop_all_no_action_service(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
            actions=None,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_stop_all()

        _, screen, _ = self._run_with_screen(runtime, body)
        assert "action service unavailable" in screen._status

    def test_action_stop_all_no_state(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
            actions=_RecordingActions(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = None
            screen.action_stop_all()

        _, screen, _ = self._run_with_screen(runtime, body)
        assert screen._status == "no agents to stop"

    def test_action_stop_all_no_active_panes(self) -> None:
        # Agent with empty pane_id => no panes to stop.
        item = _agent_view(pane_id="")
        st = _state(agents=(item,))
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            actions=_RecordingActions(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            screen.action_stop_all()

        _, screen, _ = self._run_with_screen(runtime, body, seed_state=st)
        assert screen._status == "no active panes to stop"

    # ── Cycle / toggle actions ────────────────────────────────────────

    def test_action_cycle_sort_advances(self) -> None:
        st = _state()
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        captured: dict[str, Any] = {}

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            before = screen._sort.field
            screen.action_cycle_sort()
            captured["before"] = before
            captured["after"] = screen._sort.field

        self._run_with_screen(runtime, body, seed_state=st)
        assert captured["before"] != captured["after"]

    def test_action_toggle_attention_flips_filter(self) -> None:
        st = _state()
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        captured: dict[str, Any] = {}

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            before = screen._filters.attention_only
            screen.action_toggle_attention()
            captured["before"] = before
            captured["after"] = screen._filters.attention_only

        self._run_with_screen(runtime, body, seed_state=st)
        assert captured["before"] is False
        assert captured["after"] is True

    def test_action_toggle_completed_flips_filter(self) -> None:
        st = _state()
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        captured: dict[str, Any] = {}

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            before = screen._filters.include_completed
            screen.action_toggle_completed()
            captured["before"] = before
            captured["after"] = screen._filters.include_completed

        self._run_with_screen(runtime, body, seed_state=st)
        assert captured["before"] != captured["after"]

    # ── Happy paths ──────────────────────────────────────────────────

    def test_action_mark_complete_calls_agent_ctrl(self) -> None:
        agents = _RecordingAgents()
        st = _state()
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=agents,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_mark_complete()

        _, screen, _ = self._run_with_screen(runtime, body, select_agent_id="agent-1")
        assert agents.mark_calls == ["agent-1"]
        assert "mark_complete" in screen._status

    def test_action_mark_complete_handles_missing_agent(self) -> None:
        # Reproduces the close-agent crash: the operator selects an
        # agent on the dashboard and then closes it inside the tmux
        # pane. The reaper transitions the agent to DEAD/COMPLETED and
        # the controller raises ``PersistenceError`` on actions that
        # target a record the store no longer recognises. The handler
        # must absorb that failure into a status update and trigger a
        # refresh — bubbling out of an action handler crashes Textual.
        agents = _RecordingAgents(
            mark_raises=PersistenceError("unknown agent: agent-1"),
        )
        st = _state()
        ctrl = _RecordingDashboardCtrl(state_to_return=st)
        runtime = _runtime_with(
            dashboard_ctrl=ctrl,
            agents_ctrl=agents,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            # Should NOT raise.
            screen.action_mark_complete()

        _, screen, _ = self._run_with_screen(runtime, body, select_agent_id="agent-1")
        assert agents.mark_calls == ["agent-1"]
        assert "mark_complete unavailable" in screen._status
        assert "unknown agent" in screen._status

    def test_action_mark_complete_handles_unexpected_error(self) -> None:
        # Defensive net for any other failure surfaced by the agents
        # controller (e.g. a downstream session_service raising). The
        # action handler must still degrade to a status message rather
        # than crashing the screen.
        agents = _RecordingAgents(mark_raises=RuntimeError("boom"))
        st = _state()
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=agents,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_mark_complete()

        _, screen, _ = self._run_with_screen(runtime, body, select_agent_id="agent-1")
        assert agents.mark_calls == ["agent-1"]
        assert "mark_complete failed" in screen._status
        assert "boom" in screen._status

    def test_action_open_pane_executes_intent(self) -> None:
        agents = _RecordingAgents()
        actions = _RecordingActions()
        st = _state()
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=agents,
            actions=actions,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_open_pane()

        self._run_with_screen(runtime, body, select_agent_id="agent-1")
        assert agents.open_pane_calls == ["agent-1"]
        assert "open_pane" in actions.execute_calls

    def test_action_open_worktree_sets_status(self) -> None:
        agents = _RecordingAgents()
        st = _state()
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=agents,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_open_worktree()

        _, screen, _ = self._run_with_screen(runtime, body, select_agent_id="agent-1")
        assert agents.open_worktree_calls == ["agent-1"]
        assert "open worktree" in screen._status.lower()

    # ── Callbacks ─────────────────────────────────────────────────────

    def test_on_interrupt_confirmed_false(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._on_interrupt_confirmed(False)

        _, screen, _ = self._run_with_screen(runtime, body)
        assert screen._status == "interrupt cancelled"

    def test_on_interrupt_confirmed_none(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._on_interrupt_confirmed(None)

        _, screen, _ = self._run_with_screen(runtime, body)
        assert screen._status == "interrupt cancelled"

    def test_on_kill_confirmed_false(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._on_kill_confirmed(False)

        _, screen, _ = self._run_with_screen(runtime, body)
        assert screen._status == "kill cancelled"

    def test_on_message_result_none(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._on_message_result(None)

        _, screen, _ = self._run_with_screen(runtime, body)
        assert screen._status == "message cancelled"

    def test_on_stop_all_confirmed_false(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._on_stop_all_confirmed(False)

        _, screen, _ = self._run_with_screen(runtime, body)
        assert screen._status == "stop cancelled"

    def test_on_stop_all_confirmed_executes(self) -> None:
        item = _agent_view(pane_id="%2", agent_id="agent-2")
        st = _state(agents=(item,))
        actions = _RecordingActions()
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            actions=actions,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            screen._on_stop_all_confirmed(True)

        _, screen, _ = self._run_with_screen(runtime, body, seed_state=st)
        assert actions.stop_calls == [("%2",)]
        assert "stopped" in screen._status

    def test_on_rename_window_result_none(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._on_rename_window_result(None)

        _, screen, _ = self._run_with_screen(runtime, body)
        assert screen._status == "rename cancelled"

    def test_on_move_window_result_none(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._on_move_window_result(None)

        _, screen, _ = self._run_with_screen(runtime, body)
        assert screen._status == "move cancelled"


# ── Extended coverage ───────────────────────────────────────────────


def _subagent_view(
    tool_call_id: str = "call_xyz",
    *,
    is_running: bool = True,
) -> DashboardSubAgentView:
    return DashboardSubAgentView(
        tool_call_id=tool_call_id,
        agent_name="sub",
        display_name="sub agent",
        description="some sub task",
        started_at=datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        completed_at=None,
        is_running=is_running,
    )


@dataclass
class _StubAttention:
    """Captures dashboard observations and emits configured notifications."""

    notifications: tuple[AttentionNotification, ...] = ()
    observe_calls: list[DashboardState] = field(default_factory=list)

    def observe_dashboard_state(self, state: DashboardState) -> tuple[AttentionNotification, ...]:
        self.observe_calls.append(state)
        return self.notifications


@dataclass
class _StubResolver:
    target: ResolvedCopilotTarget | None = None
    calls: list[int | None] = field(default_factory=list)

    def resolve_target_for_pid(self, pane_pid: int | None) -> ResolvedCopilotTarget | None:
        self.calls.append(pane_pid)
        return self.target


@dataclass
class _StubStore:
    agents: dict[str, object] = field(default_factory=dict)
    get_calls: list[str] = field(default_factory=list)

    def get_agent(self, agent_id: str) -> object | None:
        self.get_calls.append(agent_id)
        return self.agents.get(agent_id)


class _RecordAgent:
    pid: int = 1234


class _RecordTmux:
    def __init__(self) -> None:
        self.with_socket_calls: list[Path] = []

    def with_socket_path(self, path: Path) -> _RecordTmux:
        self.with_socket_calls.append(path)
        return self


def _make_push_recorder(pushed: list[Any]) -> Callable[..., None]:
    """Return a typed stand-in for ``screen.app.push_screen`` that records calls."""

    def _push(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        pushed.append((args, kwargs))

    return _push


class DashboardScreenAdditionalTests(unittest.TestCase):
    """Extra branches in screens/dashboard.py beyond the early-return guards."""

    def _run_with_screen(
        self,
        runtime: MuxdeckRuntime,
        body: ScreenBody,
        *,
        select_agent_id: str | None = None,
        seed_state: DashboardState | None = None,
    ) -> tuple[_Harness, DashboardScreen]:
        captured: dict[str, Any] = {}

        async def scenario() -> None:
            app = _Harness(runtime)
            captured["app"] = app
            async with app.run_test(size=(160, 60)) as pilot:
                screen = DashboardScreen(runtime)
                if seed_state is not None:
                    screen._state = seed_state
                screen._skip_next_show_refresh = True
                await app.push_screen(screen)
                await pilot.pause()  # type: ignore[attr-defined]
                if select_agent_id is not None:
                    screen._selected_agent_id = select_agent_id
                captured["screen"] = screen
                await body(app, screen, pilot)

        asyncio.run(scenario())
        return captured["app"], captured["screen"]

    # ── property accessors ────────────────────────────────────────

    def test_property_accessors_expose_internal_state(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            assert screen.current_filters is screen._filters
            assert screen.current_sort is screen._sort
            assert screen.current_selected_agent_id == screen._selected_agent_id

        self._run_with_screen(runtime, body, select_agent_id="agent-x")

    # ── refresh_data with pre-built state ────────────────────────

    def test_refresh_data_uses_pre_built_state_from_app(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            app.last_dashboard_state = st
            screen.refresh_data()
            assert app.last_dashboard_state is None
            assert screen._state is st

        self._run_with_screen(runtime, body)

    # ── on_show without skip ─────────────────────────────────────

    def test_on_show_triggers_refresh_when_skip_flag_clear(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._skip_next_show_refresh = False
            app.last_dashboard_state = st
            screen.on_show()
            assert app.last_dashboard_state is None

        self._run_with_screen(runtime, body)

    def test_on_show_first_call_skips_refresh(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._skip_next_show_refresh = True
            app.last_dashboard_state = _state()
            screen.on_show()
            # Skip flag flipped, pre-built state was *not* consumed.
            assert screen._skip_next_show_refresh is False
            assert app.last_dashboard_state is not None

        self._run_with_screen(runtime, body)

    # ── _apply_state status messages ─────────────────────────────

    def test_apply_state_renders_filter_summary_status(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._filters = DashboardFilterState(
                attention_only=True,
                text_query="auth",
                include_completed=False,
            )
            from muxdeck.services.runtime_service import RuntimeSyncReport

            screen._apply_state(st, RuntimeSyncReport())
            assert "1 visible" in screen._status
            assert "attn-only" in screen._status
            assert "hide-done" in screen._status
            assert "filter:auth" in screen._status
            assert "sort:" in screen._status

        self._run_with_screen(runtime, body, select_agent_id="agent-1")

    def test_apply_state_surfaces_sync_report_error(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            from muxdeck.services.runtime_service import RuntimeSyncReport

            screen._apply_state(st, RuntimeSyncReport(error="tmux down"))
            assert screen._status == "tmux down"

        self._run_with_screen(runtime, body)

    def test_apply_state_with_no_sync_report_uses_health_message(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._apply_state(st, None)
            assert "1 agents" in screen._status
            assert "all green" in screen._status

        self._run_with_screen(runtime, body)

    def test_apply_state_remembers_selected_agent(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._apply_state(st, None)
            assert "agent-1" in app.remembered_agents

        self._run_with_screen(runtime, body)

    def test_apply_state_emits_attention_notifications(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        attention = _StubAttention(
            notifications=(
                AttentionNotification(
                    alert_id="a1",
                    severity="error",
                    title="boom",
                    message="critical alert",
                ),
                AttentionNotification(
                    alert_id="a2",
                    severity="warning",
                    title="careful",
                    message="watch out",
                ),
            )
        )
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            attention_obj=attention,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._apply_state(st, None)
            assert len(attention.observe_calls) >= 1

        self._run_with_screen(runtime, body)

    # ── on_input_changed branches ────────────────────────────────

    def test_on_input_changed_for_filter_input_updates_filters(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            input_widget = screen.query_one(FilterBar).query_one(Input)
            input_widget.value = ""
            event = Input.Changed(input_widget, "auth")
            screen.on_input_changed(event)
            assert screen._filters.text_query == "auth"
            assert screen._filter_timer is not None

        self._run_with_screen(runtime, body)

    def test_on_input_changed_ignores_unrelated_input(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            other = Input(id="some-other-input")
            event = Input.Changed(other, "ignored")
            before = screen._filters.text_query
            screen.on_input_changed(event)
            assert screen._filters.text_query == before

        self._run_with_screen(runtime, body)

    def test_on_input_changed_uses_cached_items_without_worker(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        ctrl = _RecordingDashboardCtrl(state_to_return=st)
        runtime = _runtime_with(
            dashboard_ctrl=ctrl,
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._cached_agent_items = (item,)
            screen._cached_selected_view = sel
            calls_before = ctrl.build_calls
            input_widget = screen.query_one(FilterBar).query_one(Input)
            input_widget.value = ""
            event = Input.Changed(input_widget, "auth")
            screen.on_input_changed(event)
            assert screen._filters.text_query == "auth"
            assert screen._filter_timer is None
            assert ctrl.build_calls == calls_before + 1
            assert ctrl.last_precomputed_items == (item,)
            assert ctrl.last_precomputed_selected is sel

        self._run_with_screen(runtime, body)

    def test_apply_state_caches_unfiltered_items_and_selected(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        st = dataclasses.replace(st, all_agent_items=(item,))
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._apply_state(st, None)
            assert screen._cached_agent_items == (item,)
            assert screen._cached_selected_view is sel

        self._run_with_screen(runtime, body)

    # ── cursor / toggle / focus actions ──────────────────────────

    def test_cursor_actions_delegate_to_panel(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_cursor_down()
            screen.action_cursor_up()
            screen.action_toggle_expand()

        self._run_with_screen(runtime, body, seed_state=st)

    def test_action_focus_filter_focuses_input_and_sets_status(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_focus_filter()
            assert screen._status == "filter agents"

        self._run_with_screen(runtime, body)

    def test_action_escape_filter_returns_focus_to_list(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_escape_filter()

        self._run_with_screen(runtime, body)

    # ── on_agent_list_panel_* messages ──────────────────────────

    def test_on_agent_selected_no_op_when_same_id(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._selected_agent_id = "agent-1"
            initial_token = screen._detail_request_token
            msg = AgentListPanel.AgentSelected("agent-1")
            screen.on_agent_list_panel_agent_selected(msg)
            assert screen._detail_request_token == initial_token

        self._run_with_screen(runtime, body, select_agent_id="agent-1")

    def test_on_agent_selected_changes_selection_and_starts_timer(self) -> None:
        a1 = _agent_view("agent-1")
        a2 = _agent_view("agent-2", pane_id="%2")
        sel = _selected_view(a1)
        st = _state(agents=(a1, a2), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            screen._selected_agent_id = "agent-1"
            initial_token = screen._detail_request_token
            msg = AgentListPanel.AgentSelected("agent-2")
            screen.on_agent_list_panel_agent_selected(msg)
            assert screen._selected_agent_id == "agent-2"
            assert "agent-2" in app.remembered_agents
            assert screen._detail_request_token == initial_token + 1
            assert screen._detail_timer is not None
            # Re-emit to exercise the timer-stop branch.
            msg2 = AgentListPanel.AgentSelected("agent-1")
            screen.on_agent_list_panel_agent_selected(msg2)
            assert screen._selected_agent_id == "agent-1"

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_on_subagent_highlighted_renders_subagent_in_panel(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            sub = _subagent_view()
            msg = AgentListPanel.SubAgentHighlighted(sub)
            screen.on_agent_list_panel_sub_agent_highlighted(msg)

        self._run_with_screen(runtime, body, seed_state=st)

    def test_on_subagent_highlighted_resets_to_parent_when_none(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            msg = AgentListPanel.SubAgentHighlighted(None)
            screen.on_agent_list_panel_sub_agent_highlighted(msg)

        self._run_with_screen(runtime, body, seed_state=st)

    def test_on_expand_requested_kicks_off_worker(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, pilot: object) -> None:
            msg = AgentListPanel.ExpandRequested("agent-1")
            screen.on_agent_list_panel_expand_requested(msg)
            # Allow the worker thread to complete.
            await pilot.pause()  # type: ignore[attr-defined]
            await pilot.pause()  # type: ignore[attr-defined]

        self._run_with_screen(runtime, body, seed_state=st)

    # ── selected_action_subject from sub-agent ───────────────────

    def test_selected_action_subject_uses_subagent_when_highlighted(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            panel = screen.query_one(AgentListPanel)
            panel.set_agents((item,), selected_agent_id="agent-1")
            sub = _subagent_view()
            tree = DashboardSubAgentTreeView(
                agent_id="agent-1",
                session_id="session-1",
                running=(sub,),
                recent=(),
            )
            panel._expanded.add("agent-1")
            panel.set_subagents("agent-1", tree)
            # cursor on the sub-agent row (parent row is index 0,
            # header at 1, sub at 2).
            panel._selected_index = 2
            assert screen._selected_action_subject() == sub.display_name

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_selected_action_subject_falls_back_to_default_when_no_agent(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            assert screen._selected_action_subject() == "agent"

        self._run_with_screen(runtime, body)

    # ── kill / interrupt confirmed True paths ────────────────────

    def test_on_interrupt_confirmed_true_executes_intent(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))
        agents = _RecordingAgents()
        actions = _RecordingActions()
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=agents,
            actions=actions,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._on_interrupt_confirmed(True)

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)
        assert agents.interrupt_calls == ["agent-1"]
        assert "interrupt" in actions.execute_calls

    def test_on_kill_confirmed_true_executes_intent(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))
        agents = _RecordingAgents()
        actions = _RecordingActions()
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=agents,
            actions=actions,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._on_kill_confirmed(True)

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)
        assert agents.kill_calls == ["agent-1"]
        assert "kill_pane" in actions.execute_calls

    # ── action_kill_agent push paths ─────────────────────────────

    def test_action_kill_agent_no_pane_sets_status(self) -> None:
        item = _agent_view(pane_id="")
        st = _state(agents=(item,))
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            actions=_RecordingActions(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            screen.action_kill_agent()
            assert "agent has no pane" in screen._status

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_action_kill_agent_with_pane_pushes_confirm_screen(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            actions=_RecordingActions(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            pushed: list[Any] = []
            screen.app.push_screen = _make_push_recorder(pushed)  # type: ignore[assignment]
            screen.action_kill_agent()
            assert len(pushed) == 1

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_action_interrupt_with_selection_pushes_confirm_screen(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            actions=_RecordingActions(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            pushed: list[Any] = []
            screen.app.push_screen = _make_push_recorder(pushed)  # type: ignore[assignment]
            screen.action_interrupt_agent()
            assert len(pushed) == 1

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    # ── rename / move modals ─────────────────────────────────────

    def test_action_rename_window_with_agent_pushes_modal(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            pushed: list[Any] = []
            screen.app.push_screen = _make_push_recorder(pushed)  # type: ignore[assignment]
            screen.action_rename_window()
            assert len(pushed) == 1

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_on_rename_window_result_executes_intent(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))
        agents = _RecordingAgents()
        actions = _RecordingActions()
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=agents,
            actions=actions,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._on_rename_window_result(RenameWindowResult(name="newwin"))

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)
        assert agents.rename_calls == [("agent-1", "newwin")]
        assert "rename_window" in actions.execute_calls

    def test_action_move_to_window_no_pane_sets_status(self) -> None:
        item = _agent_view(pane_id="")
        st = _state(agents=(item,))
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            actions=_RecordingActions(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            screen.action_move_to_window()
            assert "agent has no pane" in screen._status

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_action_move_to_window_with_pane_pushes_modal(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            actions=_RecordingActions(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            pushed: list[Any] = []
            screen.app.push_screen = _make_push_recorder(pushed)  # type: ignore[assignment]
            screen.action_move_to_window()
            assert len(pushed) == 1

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_on_move_window_result_executes_intent(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))
        agents = _RecordingAgents()
        actions = _RecordingActions()
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=agents,
            actions=actions,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._on_move_window_result(
                MoveWindowResult(target_window="window-2", new_window_name=None)
            )

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)
        assert agents.move_calls == [("agent-1", "window-2", None)]
        assert "move_to_window" in actions.execute_calls

    # ── send_message paths ───────────────────────────────────────

    def test_action_send_message_no_pane_sets_status(self) -> None:
        item = _agent_view(pane_id="")
        st = _state(agents=(item,))
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            actions=_RecordingActions(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            screen.action_send_message()
            assert "agent has no pane" in screen._status

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_action_send_message_pushes_modal(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            actions=_RecordingActions(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            pushed: list[Any] = []
            screen.app.push_screen = _make_push_recorder(pushed)  # type: ignore[assignment]
            screen.action_send_message()
            assert len(pushed) == 1

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_on_message_result_sends_message_via_actions(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))
        actions = _RecordingActions()
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            actions=actions,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._on_message_result(MessageResult(text="hello", pane_id="%1"))

        _, screen = self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)
        assert actions.send_calls == [("%1", "hello")]
        assert "✓" in screen._status

    def test_on_message_result_handles_failed_send(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))
        actions = _RecordingActions(
            send_message_result=ActionResult(success=False, message="pane gone"),
        )
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            actions=actions,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._on_message_result(MessageResult(text="hi", pane_id="%1"))

        _, screen = self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)
        assert "✗" in screen._status

    def test_on_message_result_no_actions_returns_early(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
            actions=None,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._on_message_result(MessageResult(text="hi", pane_id="%1"))

        _, screen = self._run_with_screen(runtime, body)
        # No status change beyond default since actions is None.
        assert "message cancelled" not in screen._status

    # ── view_pane paths ──────────────────────────────────────────

    def test_action_view_pane_no_pane_sets_status(self) -> None:
        item = _agent_view(pane_id="")
        st = _state(agents=(item,))
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            screen.action_view_pane()
            assert "agent has no pane" in screen._status

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_action_view_pane_streaming_unavailable_sets_status(self) -> None:
        # No pane_stream and no resolver — adapter returns None.
        item = _agent_view()
        st = _state(agents=(item,))
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            pane_stream=None,
            session_resolver=None,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            screen.action_view_pane()
            assert "pane streaming unavailable" in screen._status

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_action_view_pane_with_stream_pushes_screen(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))

        class _DummyStream:
            pass

        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            pane_stream=_DummyStream(),
            session_resolver=None,
            store=_StubStore(),
        )

        # The mirror-screen constructor reaches into runtime.config.paths.state_dir,
        # so install a richer config for this test only.
        class _Paths:
            state_dir = Path("/tmp/state")

        class _Cfg:
            general = _MinimalGeneral()
            paths = _Paths()

        type(runtime).config = _Cfg()  # type: ignore[assignment, misc]

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            pushed: list[Any] = []
            screen.app.push_screen = _make_push_recorder(pushed)  # type: ignore[assignment]
            screen.action_view_pane()
            assert len(pushed) == 1

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    # ── _resolve_live_mirror_target branches ────────────────────

    def test_resolve_live_mirror_target_no_pane_returns_blank(self) -> None:
        item = _agent_view(pane_id="")
        st = _state(agents=(item,))

        class _Stream:
            pass

        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            pane_stream=_Stream(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            pane_id, stream = screen._resolve_live_mirror_target(item)
            assert pane_id == ""
            assert stream is not None

        self._run_with_screen(runtime, body, seed_state=st)

    def test_resolve_live_mirror_target_no_resolver(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))

        class _Stream:
            pass

        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            pane_stream=_Stream(),
            session_resolver=None,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            pane_id, stream = screen._resolve_live_mirror_target(item)
            assert pane_id == item.pane_id
            assert stream is not None

        self._run_with_screen(runtime, body, seed_state=st)

    def test_resolve_live_mirror_target_no_agent_record(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))

        class _Stream:
            pass

        resolver = _StubResolver(target=None)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            pane_stream=_Stream(),
            session_resolver=resolver,
            store=_StubStore(),  # no agent record present
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            pane_id, stream = screen._resolve_live_mirror_target(item)
            assert pane_id == item.pane_id
            assert stream is not None

        self._run_with_screen(runtime, body, seed_state=st)

    def test_resolve_live_mirror_target_resolver_returns_none_target(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))

        class _Stream:
            pass

        resolver = _StubResolver(target=None)
        store = _StubStore(agents={"agent-1": _RecordAgent()})
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            pane_stream=_Stream(),
            session_resolver=resolver,
            store=store,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            pane_id, stream = screen._resolve_live_mirror_target(item)
            assert pane_id == item.pane_id
            assert stream is not None

        self._run_with_screen(runtime, body, seed_state=st)

    def test_resolve_live_mirror_target_target_without_socket(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))

        class _Stream:
            pass

        target = ResolvedCopilotTarget(session_id="sess-1", pane_id="%nested", socket_path=None)
        resolver = _StubResolver(target=target)
        store = _StubStore(agents={"agent-1": _RecordAgent()})
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            pane_stream=_Stream(),
            session_resolver=resolver,
            store=store,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            pane_id, stream = screen._resolve_live_mirror_target(item)
            assert pane_id == "%nested"
            assert stream is not None

        self._run_with_screen(runtime, body, seed_state=st)

    def test_resolve_live_mirror_target_target_with_socket_uses_nested_stream(
        self,
    ) -> None:
        item = _agent_view()
        st = _state(agents=(item,))

        class _Stream:
            pass

        socket = Path("/tmp/socket.sock")
        target = ResolvedCopilotTarget(session_id="sess-1", pane_id="%nested", socket_path=socket)
        resolver = _StubResolver(target=target)
        store = _StubStore(agents={"agent-1": _RecordAgent()})
        tmux_obj = _RecordTmux()
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            pane_stream=_Stream(),
            session_resolver=resolver,
            store=store,
            tmux=tmux_obj,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            tmux_obj.with_socket_calls.clear()
            pane_id, stream = screen._resolve_live_mirror_target(item)
            assert pane_id == "%nested"
            assert stream is not None
            assert tmux_obj.with_socket_calls == [socket]

        self._run_with_screen(runtime, body, seed_state=st)

    def test_resolve_live_mirror_target_prefers_sync_store(self) -> None:
        """Thread-safety regression: worker-thread path must use sync_store.

        ``_resolve_live_mirror_target`` runs from the live-tail worker
        thread (``_resolve_and_capture``). Touching the UI-thread-bound
        ``runtime.store`` from there raises ``sqlite3.ProgrammingError:
        SQLite objects created in a thread can only be used in that
        same thread`` -- exactly the crash operators reported when
        navigating the dashboard. The resolver must look up agents
        through ``sync_store`` (built with ``check_same_thread=False``)
        whenever it is wired.
        """
        item = _agent_view()
        st = _state(agents=(item,))

        class _Stream:
            pass

        ui_store = _StubStore(agents={"agent-1": _RecordAgent()})
        sync_store = _StubStore(agents={"agent-1": _RecordAgent()})
        target = ResolvedCopilotTarget(session_id="sess-1", pane_id="%nested", socket_path=None)
        resolver = _StubResolver(target=target)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            pane_stream=_Stream(),
            session_resolver=resolver,
            store=ui_store,
            sync_store=sync_store,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            ui_store.get_calls.clear()
            sync_store.get_calls.clear()
            pane_id, stream = screen._resolve_live_mirror_target(item)
            assert pane_id == "%nested"
            assert stream is not None
            assert sync_store.get_calls == ["agent-1"], (
                "live-mirror resolution must read agents through the "
                "thread-safe sync_store, not the UI-bound store"
            )
            assert ui_store.get_calls == [], (
                "UI-bound store must never be touched when sync_store "
                "is wired -- doing so crashes the live-tail worker"
            )

        self._run_with_screen(runtime, body, seed_state=st)

    def test_resolve_live_mirror_target_falls_back_to_store_when_no_sync(self) -> None:
        """Without sync_store, the resolver still works against the UI store.

        Lighter test harnesses (and any production wiring without the
        secondary connection) leave ``runtime.sync_store`` as ``None``;
        the resolver must fall back to ``runtime.store`` rather than
        crashing or returning empty results.
        """
        item = _agent_view()
        st = _state(agents=(item,))

        class _Stream:
            pass

        ui_store = _StubStore(agents={"agent-1": _RecordAgent()})
        target = ResolvedCopilotTarget(session_id="sess-1", pane_id="%nested", socket_path=None)
        resolver = _StubResolver(target=target)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            pane_stream=_Stream(),
            session_resolver=resolver,
            store=ui_store,
            sync_store=None,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            ui_store.get_calls.clear()
            pane_id, _stream = screen._resolve_live_mirror_target(item)
            assert pane_id == "%nested"
            assert ui_store.get_calls == ["agent-1"]

        self._run_with_screen(runtime, body, seed_state=st)

    def test_stream_adapter_for_socket_returns_none_when_no_tmux(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
            tmux=None,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            assert screen._stream_adapter_for_socket(Path("/x")) is None

        self._run_with_screen(runtime, body)

    # ── copy_details ─────────────────────────────────────────────

    def test_action_copy_details_no_selection_sets_status(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen.action_copy_details()
            assert screen._status == "no agent selected"

        self._run_with_screen(runtime, body)

    def test_action_copy_details_with_selection_copies_text(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            panel = screen.query_one(AgentListPanel)
            panel.set_agents((item,), selected_agent_id="agent-1")
            screen.action_copy_details()
            assert "copied" in screen._status

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    # ── stop_all happy path / failure summary ────────────────────

    def test_action_stop_all_with_active_panes_pushes_confirm(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            actions=_RecordingActions(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            pushed: list[Any] = []
            screen.app.push_screen = _make_push_recorder(pushed)  # type: ignore[assignment]
            screen.action_stop_all()
            assert len(pushed) == 1

        self._run_with_screen(runtime, body, seed_state=st)

    def test_on_stop_all_confirmed_records_failures(self) -> None:
        item1 = _agent_view("agent-1", pane_id="%1")
        item2 = _agent_view("agent-2", pane_id="%2")
        st = _state(agents=(item1, item2))
        actions = _RecordingActions(
            stop_results=[
                ActionResult(success=True, message="ok"),
                ActionResult(success=False, message="failed"),
            ]
        )
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            actions=actions,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            screen._on_stop_all_confirmed(True)

        _, screen = self._run_with_screen(runtime, body, seed_state=st)
        assert "stopped 1/2" in screen._status

    def test_on_stop_all_confirmed_no_actions_returns_early(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
            actions=None,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._on_stop_all_confirmed(True)

        self._run_with_screen(runtime, body)

    # ── _execute_agent_intent / _set_agent_intent_status ─────────

    def test_execute_agent_intent_no_actions_sets_status(self) -> None:
        item = _agent_view()
        st = _state(agents=(item,))
        agents = _RecordingAgents()
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=agents,
            actions=None,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._execute_agent_intent("focus", agents.open_pane_intent)
            assert "action service unavailable" in screen._status

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_execute_agent_intent_no_selection_sets_status(self) -> None:
        agents = _RecordingAgents()
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=agents,
            actions=_RecordingActions(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._execute_agent_intent("focus", agents.open_pane_intent)
            assert "no agent selected" in screen._status

        self._run_with_screen(runtime, body)

    def test_set_agent_intent_status_no_selection_sets_status(self) -> None:
        agents = _RecordingAgents()
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=agents,
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._set_agent_intent_status("worktree", agents.open_worktree_intent)
            assert "no agent selected" in screen._status

        self._run_with_screen(runtime, body)

    # ── _emit_notifications ──────────────────────────────────────

    def test_emit_notifications_no_op_for_empty(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._emit_notifications(())

        self._run_with_screen(runtime, body)

    def test_emit_notifications_handles_multiple(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )
        notifications = (
            AttentionNotification(
                alert_id="a1",
                severity="error",
                title="boom",
                message="critical",
            ),
            AttentionNotification(
                alert_id="a2",
                severity="error",
                title="other",
                message="another",
            ),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._emit_notifications(notifications)

        self._run_with_screen(runtime, body)

    # ── _update_selected_detail ──────────────────────────────────

    def test_update_selected_detail_clears_panels_when_no_selection(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            # No selection and no state → method clears panels and returns.
            screen._update_selected_detail()

        self._run_with_screen(runtime, body)

    def test_update_selected_detail_handles_loader_exception(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)

        class _BoomCtrl(_RecordingDashboardCtrl):
            def build_selected_agent_view(
                self, item: DashboardAgentListItemView, *, preview_line_limit: int = 8
            ) -> DashboardSelectedAgentView:
                raise RuntimeError("boom")

        ctrl = _BoomCtrl(state_to_return=st)
        runtime = _runtime_with(
            dashboard_ctrl=ctrl,
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            screen._update_selected_detail()
            # Method swallowed exception; state untouched.
            assert screen._state is st

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_update_selected_detail_refreshes_panels_with_view(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)

        class _Ctrl(_RecordingDashboardCtrl):
            def build_selected_agent_view(
                self, item: DashboardAgentListItemView, *, preview_line_limit: int = 8
            ) -> DashboardSelectedAgentView:
                return _selected_view(item)

        ctrl = _Ctrl(state_to_return=st)
        runtime = _runtime_with(
            dashboard_ctrl=ctrl,
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            screen._update_selected_detail()

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    # ── _schedule_selected_detail_worker ─────────────────────────

    def test_schedule_detail_worker_no_selection_falls_back(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._schedule_selected_detail_worker()

        self._run_with_screen(runtime, body)

    def test_schedule_detail_worker_without_sync_dashboard_falls_back(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)

        class _Ctrl(_RecordingDashboardCtrl):
            def build_selected_agent_view(
                self, item: DashboardAgentListItemView, *, preview_line_limit: int = 8
            ) -> DashboardSelectedAgentView:
                return _selected_view(item)

        ctrl = _Ctrl(state_to_return=st)
        runtime = _runtime_with(
            dashboard_ctrl=ctrl,
            agents_ctrl=_RecordingAgents(),
        )
        # Drop the sync_dashboard attribute entirely so the fallback path runs.
        delattr(type(runtime), "sync_dashboard")
        type(runtime).sync_dashboard = None  # type: ignore[misc]

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            screen._schedule_selected_detail_worker()

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_schedule_detail_worker_runs_worker_to_success(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)

        class _Ctrl(_RecordingDashboardCtrl):
            def build_selected_agent_view(
                self, item: DashboardAgentListItemView, *, preview_line_limit: int = 8
            ) -> DashboardSelectedAgentView:
                return _selected_view(item)

        ctrl = _Ctrl(state_to_return=st)
        runtime = _runtime_with(
            dashboard_ctrl=ctrl,
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, pilot: object) -> None:
            screen._state = st
            screen._schedule_selected_detail_worker()
            await pilot.pause()  # type: ignore[attr-defined]
            await pilot.pause()  # type: ignore[attr-defined]

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    # ── _apply_async_detail branches ─────────────────────────────

    def test_apply_async_detail_drops_stale_token(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            screen._detail_request_token = 5
            view = _selected_view(item)
            screen._apply_async_detail(token=1, agent_id="agent-1", view=view)
            # Stale tokens should leave selected_agent untouched.
            assert screen._state is st

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_apply_async_detail_drops_when_agent_id_changed(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            view = _selected_view(item)
            screen._apply_async_detail(
                token=screen._detail_request_token,
                agent_id="agent-other",
                view=view,
            )

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_apply_async_detail_skips_when_view_is_none(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            screen._apply_async_detail(
                token=screen._detail_request_token,
                agent_id="agent-1",
                view=None,
            )

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_apply_async_detail_updates_state_and_panels(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._state = st
            view = _selected_view(item)
            screen._apply_async_detail(
                token=screen._detail_request_token,
                agent_id="agent-1",
                view=view,
            )
            assert screen._state.selected_agent is view

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    # ── on_worker_state_changed branches ─────────────────────────

    def test_on_worker_state_changed_dashboard_error_with_state_sets_status(
        self,
    ) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            from textual.worker import Worker, WorkerState

            screen._state = st
            worker: Any = type("W", (), {"name": "dashboard_load", "state": WorkerState.ERROR})()
            event = Worker.StateChanged(worker, WorkerState.ERROR)
            screen.on_worker_state_changed(event)
            assert "refresh failed" in screen._status

        self._run_with_screen(runtime, body, seed_state=st)

    def test_on_worker_state_changed_ignores_unrelated_workers(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            from textual.worker import Worker, WorkerState

            worker: Any = type(
                "W",
                (),
                {
                    "name": "subagents:foo",
                    "state": WorkerState.SUCCESS,
                    "result": None,
                },
            )()
            event = Worker.StateChanged(worker, WorkerState.SUCCESS)
            # Should not raise; falls through both branches.
            screen.on_worker_state_changed(event)

        self._run_with_screen(runtime, body)

    def test_on_worker_state_changed_dashboard_success_applies_state(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            from textual.worker import Worker, WorkerState

            kickoff = screen._state_apply_seq
            worker: Any = type(
                "W",
                (),
                {
                    "name": "dashboard_load",
                    "state": WorkerState.SUCCESS,
                    "result": (kickoff, st),
                },
            )()
            event = Worker.StateChanged(worker, WorkerState.SUCCESS)
            screen.on_worker_state_changed(event)
            assert screen._state is st

        self._run_with_screen(runtime, body)

    def test_on_worker_state_changed_dashboard_success_drops_stale_seq(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            from textual.worker import Worker, WorkerState

            screen._state_apply_seq = 99
            worker: Any = type(
                "W",
                (),
                {
                    "name": "dashboard_load",
                    "state": WorkerState.SUCCESS,
                    "result": (1, st),
                },
            )()
            event = Worker.StateChanged(worker, WorkerState.SUCCESS)
            screen.on_worker_state_changed(event)

        self._run_with_screen(runtime, body)

    def test_on_worker_state_changed_dashboard_success_with_invalid_result(
        self,
    ) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            from textual.worker import Worker, WorkerState

            worker: Any = type(
                "W",
                (),
                {
                    "name": "dashboard_load",
                    "state": WorkerState.SUCCESS,
                    "result": "not a tuple",
                },
            )()
            event = Worker.StateChanged(worker, WorkerState.SUCCESS)
            screen.on_worker_state_changed(event)

        self._run_with_screen(runtime, body)

    def test_on_worker_state_changed_dashboard_success_with_non_state_result(
        self,
    ) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            from textual.worker import Worker, WorkerState

            worker: Any = type(
                "W",
                (),
                {
                    "name": "dashboard_load",
                    "state": WorkerState.SUCCESS,
                    "result": (0, "not a state"),
                },
            )()
            event = Worker.StateChanged(worker, WorkerState.SUCCESS)
            screen.on_worker_state_changed(event)

        self._run_with_screen(runtime, body)

    def test_on_worker_state_changed_dashboard_running_returns_early(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            from textual.worker import Worker, WorkerState

            worker: Any = type(
                "W",
                (),
                {"name": "dashboard_load", "state": WorkerState.RUNNING, "result": None},
            )()
            event = Worker.StateChanged(worker, WorkerState.RUNNING)
            screen.on_worker_state_changed(event)

        self._run_with_screen(runtime, body)

    def test_on_worker_state_changed_detail_success_applies_view(self) -> None:
        item = _agent_view()
        sel = _selected_view(item)
        st = _state(agents=(item,), selected=sel)
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            from textual.worker import Worker, WorkerState

            screen._state = st
            view = _selected_view(item)
            worker: Any = type(
                "W",
                (),
                {
                    "name": "dashboard_detail",
                    "state": WorkerState.SUCCESS,
                    "result": (screen._detail_request_token, "agent-1", view),
                },
            )()
            event = Worker.StateChanged(worker, WorkerState.SUCCESS)
            screen.on_worker_state_changed(event)

        self._run_with_screen(runtime, body, select_agent_id="agent-1", seed_state=st)

    def test_on_worker_state_changed_detail_non_success_returns(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            from textual.worker import Worker, WorkerState

            worker: Any = type(
                "W",
                (),
                {"name": "dashboard_detail", "state": WorkerState.RUNNING, "result": None},
            )()
            event = Worker.StateChanged(worker, WorkerState.RUNNING)
            screen.on_worker_state_changed(event)

        self._run_with_screen(runtime, body)

    def test_on_worker_state_changed_detail_invalid_result_returns(self) -> None:
        runtime = _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=_state()),
            agents_ctrl=_RecordingAgents(),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            from textual.worker import Worker, WorkerState

            worker: Any = type(
                "W",
                (),
                {"name": "dashboard_detail", "state": WorkerState.SUCCESS, "result": "bad"},
            )()
            event = Worker.StateChanged(worker, WorkerState.SUCCESS)
            screen.on_worker_state_changed(event)

        self._run_with_screen(runtime, body)


# ── Live-tail loop ──────────────────────────────────────────────────


@dataclass
class _FakeStream:
    """Recording fake of ``PaneStreamAdapter`` for the live-tail tests."""

    capture_text: str = ""
    raise_on_capture: bool = False
    capture_tail_calls: list[tuple[str, int]] = field(default_factory=list)

    def capture_tail(self, pane_id: str, *, lines: int = 100) -> str:
        self.capture_tail_calls.append((pane_id, lines))
        if self.raise_on_capture:
            raise RuntimeError("tmux unreachable")
        return self.capture_text


class DashboardLiveTailTests(unittest.TestCase):
    """Behavioural coverage for the dashboard ``Selected agent · output`` live tail."""

    # ``_start_live_tail`` dispatches the resolver round-trip + first
    # capture into a worker thread, then ``call_from_thread`` posts
    # ``_install_live_tail`` back to the UI loop where the periodic
    # timer is wired up. On a fast local machine two ``pilot.pause()``
    # turns are usually enough to drain that pipeline; on a slow CI
    # runner the thread pool can take noticeably longer to schedule
    # the worker, leaving the assertion racing the install. Poll up
    # to ~3 s instead of relying on a fixed pause count so the test
    # is deterministic regardless of host load.
    _LIVE_TAIL_INSTALL_MAX_PAUSES = 60

    @classmethod
    async def _wait_for_live_tail_install(
        cls,
        screen: DashboardScreen,
        pilot: object,
    ) -> None:
        """Block until ``_install_live_tail`` has wired up the timer."""
        for _ in range(cls._LIVE_TAIL_INSTALL_MAX_PAUSES):
            if screen._live_tail_timer is not None:
                return
            await pilot.pause()  # type: ignore[attr-defined]
        msg = "live-tail install never wired up the timer"
        raise AssertionError(msg)

    def _run(
        self,
        runtime: MuxdeckRuntime,
        body: ScreenBody,
        *,
        seed_state: DashboardState | None = None,
    ) -> tuple[_Harness, DashboardScreen]:
        captured: dict[str, Any] = {}

        async def scenario() -> None:
            app = _Harness(runtime)
            captured["app"] = app
            async with app.run_test(size=(160, 60)) as pilot:
                screen = DashboardScreen(runtime)
                if seed_state is not None:
                    screen._state = seed_state
                screen._skip_next_show_refresh = True
                await app.push_screen(screen)
                await pilot.pause()  # type: ignore[attr-defined]
                captured["screen"] = screen
                await body(app, screen, pilot)

        asyncio.run(scenario())
        return captured["app"], captured["screen"]

    def _build_runtime(
        self,
        *,
        stream: _FakeStream,
        item: DashboardAgentListItemView,
    ) -> tuple[MuxdeckRuntime, DashboardState]:
        st = _state(agents=(item,))
        return _runtime_with(
            dashboard_ctrl=_RecordingDashboardCtrl(state_to_return=st),
            agents_ctrl=_RecordingAgents(),
            pane_stream=stream,
            store=_StubStore(agents={item.agent_id: _RecordAgent()}),
        ), st

    def test_start_live_tail_kicks_immediate_capture(self) -> None:
        item = _agent_view()
        stream = _FakeStream(capture_text="hello live\n")
        runtime, st = self._build_runtime(stream=stream, item=item)

        async def body(_app: _Harness, screen: DashboardScreen, pilot: object) -> None:
            screen._selected_agent_id = None  # ensure cold start
            screen._start_live_tail(item.agent_id)
            await pilot.pause()  # type: ignore[attr-defined]
            await pilot.pause()  # type: ignore[attr-defined]
            assert stream.capture_tail_calls
            pane_id, lines = stream.capture_tail_calls[0]
            assert pane_id == item.pane_id
            assert lines == 200
            assert screen._live_tail_timer is not None

        self._run(runtime, body, seed_state=st)

    def test_apply_live_tail_caches_lines_and_repaints_panel(self) -> None:
        from muxdeck.widgets.dashboard import LogPreviewPanel

        item = _agent_view()
        stream = _FakeStream()
        runtime, st = self._build_runtime(stream=stream, item=item)
        st = DashboardState(
            generated_at=st.generated_at,
            metrics=st.metrics,
            filters=st.filters,
            sort=st.sort,
            health=st.health,
            alerts=st.alerts,
            agents=st.agents,
            selected_agent_id=item.agent_id,
            selected_agent=_selected_view(item),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._selected_agent_id = item.agent_id
            screen._state = st
            screen._live_tail_token = 5
            screen._apply_live_tail(item.agent_id, 5, "first line\nsecond\n")
            cached = screen._live_tail_lines[item.agent_id]
            assert tuple(line.content for line in cached) == ("first line", "second")
            assert all(line.source == "tmux_capture" for line in cached)
            # Subsequent paints substitute the cached preview.
            preview_panel = screen.query_one(LogPreviewPanel)
            preview_panel.update("")  # clear
            screen.query_one(LogPreviewPanel).set_logs(
                screen._with_live_preview(st.selected_agent),
            )

        self._run(runtime, body, seed_state=st)

    def test_apply_live_tail_drops_stale_token(self) -> None:
        item = _agent_view()
        stream = _FakeStream()
        runtime, st = self._build_runtime(stream=stream, item=item)

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._selected_agent_id = item.agent_id
            screen._live_tail_token = 7
            screen._apply_live_tail(item.agent_id, 6, "ignored\n")
            assert item.agent_id not in screen._live_tail_lines

        self._run(runtime, body, seed_state=st)

    def test_apply_live_tail_drops_when_selection_changed(self) -> None:
        item = _agent_view()
        other = _agent_view(agent_id="agent-2", pane_id="%2", name="agent-2")
        stream = _FakeStream()
        runtime, st = self._build_runtime(stream=stream, item=item)

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._selected_agent_id = other.agent_id
            screen._live_tail_token = 1
            screen._apply_live_tail(item.agent_id, 1, "stale\n")
            assert item.agent_id not in screen._live_tail_lines

        self._run(runtime, body, seed_state=st)

    def test_apply_live_tail_empty_capture_clears_cache(self) -> None:
        item = _agent_view()
        stream = _FakeStream()
        runtime, st = self._build_runtime(stream=stream, item=item)
        st = DashboardState(
            generated_at=st.generated_at,
            metrics=st.metrics,
            filters=st.filters,
            sort=st.sort,
            health=st.health,
            alerts=st.alerts,
            agents=st.agents,
            selected_agent_id=item.agent_id,
            selected_agent=_selected_view(item),
        )

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._selected_agent_id = item.agent_id
            screen._state = st
            screen._live_tail_token = 1
            screen._live_tail_lines[item.agent_id] = ()
            screen._apply_live_tail(item.agent_id, 1, "    \n\n")
            assert item.agent_id not in screen._live_tail_lines

        self._run(runtime, body, seed_state=st)

    def test_with_live_preview_returns_view_when_no_cache(self) -> None:
        item = _agent_view()
        view = _selected_view(item)
        stream = _FakeStream()
        runtime, st = self._build_runtime(stream=stream, item=item)

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            assert screen._with_live_preview(None) is None
            painted = screen._with_live_preview(view)
            assert painted is view  # untouched when nothing cached

        self._run(runtime, body, seed_state=st)

    def test_with_live_preview_substitutes_cached_lines(self) -> None:
        item = _agent_view()
        view = _selected_view(item)
        stream = _FakeStream()
        runtime, st = self._build_runtime(stream=stream, item=item)

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            cached = DashboardScreen._build_live_preview_lines(
                "alpha\nbeta\n",
                line_limit=12,
                captured_at=datetime(2024, 1, 1, tzinfo=UTC),
                sequence_no=42,
            )
            screen._live_tail_lines[item.agent_id] = cached
            painted = screen._with_live_preview(view)
            assert painted is not None
            assert painted.log_preview == cached

        self._run(runtime, body, seed_state=st)

    def test_capture_live_tail_swallows_exceptions(self) -> None:
        item = _agent_view()
        stream = _FakeStream(raise_on_capture=True)
        runtime, st = self._build_runtime(stream=stream, item=item)

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._selected_agent_id = item.agent_id
            screen._live_tail_token = 1
            # Should not raise even though the underlying stream throws.
            screen._capture_live_tail(cast("Any", stream), "%1", item.agent_id, 1)
            # Cache stays empty — transient errors should not blank the panel.
            assert item.agent_id not in screen._live_tail_lines

        self._run(runtime, body, seed_state=st)

    def test_stop_live_tail_clears_state_and_cancels_timer(self) -> None:
        item = _agent_view()
        stream = _FakeStream()
        runtime, st = self._build_runtime(stream=stream, item=item)

        async def body(_app: _Harness, screen: DashboardScreen, pilot: object) -> None:
            screen._selected_agent_id = item.agent_id
            screen._start_live_tail(item.agent_id)
            await self._wait_for_live_tail_install(screen, pilot)
            assert screen._live_tail_timer is not None
            token_before = screen._live_tail_token
            screen._stop_live_tail()
            assert screen._live_tail_timer is None
            assert screen._live_tail_agent_id is None
            assert screen._live_tail_pane_id is None
            assert screen._live_tail_stream is None
            assert screen._live_tail_token > token_before

        self._run(runtime, body, seed_state=st)

    def test_start_live_tail_skips_when_pane_id_missing(self) -> None:
        item = _agent_view(pane_id="")
        stream = _FakeStream()
        runtime, st = self._build_runtime(stream=stream, item=item)

        async def body(_app: _Harness, screen: DashboardScreen, _pilot: object) -> None:
            screen._selected_agent_id = item.agent_id
            screen._start_live_tail(item.agent_id)
            assert screen._live_tail_timer is None
            assert screen._live_tail_agent_id is None

        self._run(runtime, body, seed_state=st)

    def test_start_live_tail_defers_resolver_to_worker_thread(self) -> None:
        """The resolver round-trip must not block the UI thread on j/k.

        ``_resolve_live_mirror_target`` walks ``/proc`` and may stat
        the SQLite store + spin up nested socket adapters. The
        original implementation called it synchronously from
        ``_start_live_tail`` on every cursor move, which on slow
        filesystems (WSL ``/mnt/c``) made the dashboard feel laggy.
        Pin the off-thread behaviour so a regression is caught.
        """
        from threading import get_ident

        item = _agent_view()
        stream = _FakeStream(capture_text="hello\n")
        runtime, st = self._build_runtime(stream=stream, item=item)

        async def body(_app: _Harness, screen: DashboardScreen, pilot: object) -> None:
            ui_thread_id = get_ident()
            resolver_thread_ids: list[int] = []
            original = screen._resolve_live_mirror_target

            def _record(
                agent: DashboardAgentListItemView,
            ) -> tuple[str, Any]:
                resolver_thread_ids.append(get_ident())
                return original(agent)

            screen._resolve_live_mirror_target = _record  # type: ignore[method-assign]
            screen._selected_agent_id = item.agent_id
            screen._start_live_tail(item.agent_id)
            await self._wait_for_live_tail_install(screen, pilot)

            assert resolver_thread_ids, "resolver was never invoked"
            assert all(tid != ui_thread_id for tid in resolver_thread_ids), (
                "resolver must run off the UI thread "
                f"(ui={ui_thread_id} resolver_threads={resolver_thread_ids})"
            )
            assert screen._live_tail_timer is not None

        self._run(runtime, body, seed_state=st)

    def test_build_live_preview_lines_preserves_interior_blanks_and_tails(self) -> None:
        # Interior blank rows survive — the operator's tmux pane has
        # those gaps for a reason (paragraph spacing, command/output
        # separation, embedded tables) and the panel should look like
        # the actual pane.
        text = "a\n\nbb\n   \nccc\n"
        ts = datetime(2024, 1, 1, tzinfo=UTC)
        out = DashboardScreen._build_live_preview_lines(
            text, line_limit=4, captured_at=ts, sequence_no=1
        )
        # ``"   "`` is rstripped to ``""`` per row but stays as an
        # empty visual line — same as the row above.
        assert tuple(line.content for line in out) == ("", "bb", "", "ccc")
        assert all(line.captured_at == ts for line in out)
        assert all(line.source == "tmux_capture" for line in out)

    def test_build_live_preview_lines_trims_trailing_pane_padding(self) -> None:
        # tmux pads the captured region with blank rows when the
        # prompt sits high in the pane. The panel should anchor at
        # the freshest *content*, not at empty pane bottom.
        text = "first\nsecond\n\n\n\n"
        ts = datetime(2024, 1, 1, tzinfo=UTC)
        out = DashboardScreen._build_live_preview_lines(
            text, line_limit=10, captured_at=ts, sequence_no=1
        )
        assert tuple(line.content for line in out) == ("first", "second")

    def test_build_live_preview_lines_empty_inputs(self) -> None:
        ts = datetime(2024, 1, 1, tzinfo=UTC)
        assert (
            DashboardScreen._build_live_preview_lines(
                "", line_limit=10, captured_at=ts, sequence_no=1
            )
            == ()
        )
        assert (
            DashboardScreen._build_live_preview_lines(
                "x\n", line_limit=0, captured_at=ts, sequence_no=1
            )
            == ()
        )
        assert (
            DashboardScreen._build_live_preview_lines(
                "  \n  \n", line_limit=10, captured_at=ts, sequence_no=1
            )
            == ()
        )

    def test_on_screen_suspend_stops_tail_and_resume_restarts(self) -> None:
        item = _agent_view()
        stream = _FakeStream()
        runtime, st = self._build_runtime(stream=stream, item=item)

        async def body(_app: _Harness, screen: DashboardScreen, pilot: object) -> None:
            screen._selected_agent_id = item.agent_id
            screen._start_live_tail(item.agent_id)
            await self._wait_for_live_tail_install(screen, pilot)
            assert screen._live_tail_timer is not None
            screen.on_screen_suspend()
            assert screen._live_tail_timer is None
            screen.on_screen_resume()
            await self._wait_for_live_tail_install(screen, pilot)
            assert screen._live_tail_timer is not None

        self._run(runtime, body, seed_state=st)


class DashboardScreenColdStartGateTests(unittest.TestCase):
    """Cold-start refresh paints local state immediately while sync runs.

    The previous behaviour kept the loading overlay up until the
    synchronizer worker delivered its first ``DashboardState``. With
    a slow synchronizer (multi-pane WSL fleets routinely take 5-30s
    to enumerate) the dashboard sat blank for the entire window, and
    operators reported the app feeling frozen -- even mode switches
    appeared blocked. The new behaviour kicks off a local build off
    the SQLite store right away so cached agents render in a few
    hundred milliseconds; the sync worker's result overwrites via
    ``last_dashboard_state`` once it lands. ``_state_apply_seq``
    already protects against the local build clobbering a fresher
    sync-driven paint.
    """

    def _build_runtime(
        self,
        *,
        synchronizer: object | None,
    ) -> MuxdeckRuntime:
        attrs: dict[str, Any] = {
            "config": _MinimalConfig(),
            "dashboard": _RecordingDashboardCtrl(state_to_return=_state()),
            "sync_dashboard": _RecordingDashboardCtrl(state_to_return=_state()),
            "agents": _RecordingAgents(),
            "actions": None,
            "pane_stream": None,
            "session_resolver": None,
            "tmux": None,
            "store": object(),
            "sync_store": None,
            "synchronizer": synchronizer,
            "attention": type(
                "_FakeAttention",
                (),
                {"observe_dashboard_state": lambda self, state: ()},
            )(),
        }
        return cast(MuxdeckRuntime, type("_FakeRuntime", (), attrs)())

    def _run(
        self,
        runtime: MuxdeckRuntime,
        body: ScreenBody,
        *,
        sync_attempted: bool,
    ) -> tuple[_Harness, DashboardScreen]:
        captured: dict[str, Any] = {}

        async def scenario() -> None:
            app = _Harness(runtime)
            app.sync_attempted = sync_attempted
            captured["app"] = app
            async with app.run_test(size=(160, 60)) as pilot:
                screen = DashboardScreen(runtime)
                screen._skip_next_show_refresh = True
                await app.push_screen(screen)
                await pilot.pause()  # type: ignore[attr-defined]
                captured["screen"] = screen
                await body(app, screen, pilot)

        asyncio.run(scenario())
        return captured["app"], captured["screen"]

    def test_first_load_with_pending_synchronizer_paints_local_state(self) -> None:
        """Cold open with sync pending must dispatch the local build immediately."""
        runtime = self._build_runtime(synchronizer=object())

        async def body(_app: _Harness, screen: DashboardScreen, pilot: object) -> None:
            screen._state = None  # simulate cold open
            # Initial status copy (set before the worker dispatches) must
            # signal that the sync is in flight so the operator knows
            # fresh state is on the way.
            screen.refresh_data()
            assert "syncing fleet" in (screen._status or "").lower(), (
                "cold open must surface a 'syncing fleet…' hint while the "
                "synchronizer worker is still in flight"
            )
            # Drain the worker dispatched by refresh_data so the local
            # build actually runs against the recording controller; the
            # _apply_state callback will then overwrite the status with
            # the local snapshot's summary line.
            for _ in range(20):
                await pilot.pause()  # type: ignore[attr-defined]
                calls = cast(_RecordingDashboardCtrl, runtime.sync_dashboard).build_calls
                if calls >= 1:
                    break
            calls = cast(_RecordingDashboardCtrl, runtime.sync_dashboard).build_calls
            assert calls >= 1, (
                "cold open must paint local state immediately so the dashboard "
                "isn't frozen for the duration of the first sync"
            )

        self._run(runtime, body, sync_attempted=False)

    def test_first_load_after_sync_runs_local_build(self) -> None:
        """Once the sync worker has reported, the local fallback still runs."""
        runtime = self._build_runtime(synchronizer=object())

        async def body(_app: _Harness, screen: DashboardScreen, pilot: object) -> None:
            screen._state = None
            screen.refresh_data()
            for _ in range(20):
                await pilot.pause()  # type: ignore[attr-defined]
                calls = cast(_RecordingDashboardCtrl, runtime.sync_dashboard).build_calls
                if calls >= 1:
                    break
            calls = cast(_RecordingDashboardCtrl, runtime.sync_dashboard).build_calls
            assert calls >= 1, "local build should run once sync_attempted is set"

        self._run(runtime, body, sync_attempted=True)

    def test_first_load_without_synchronizer_runs_local_build(self) -> None:
        """No synchronizer -> no waiting; local build runs immediately."""
        runtime = self._build_runtime(synchronizer=None)

        async def body(_app: _Harness, screen: DashboardScreen, pilot: object) -> None:
            screen._state = None
            screen.refresh_data()
            for _ in range(20):
                await pilot.pause()  # type: ignore[attr-defined]
                calls = cast(_RecordingDashboardCtrl, runtime.sync_dashboard).build_calls
                if calls >= 1:
                    break
            calls = cast(_RecordingDashboardCtrl, runtime.sync_dashboard).build_calls
            assert calls >= 1, "local build should run when no synchronizer is wired"

        self._run(runtime, body, sync_attempted=False)


if __name__ == "__main__":
    unittest.main()
