"""Coverage for muxdeck.app lifecycle, sync worker, and helper paths.

Targets the missing lines in ``MuxdeckApp`` action wiring, the worker-thread
``_run_sync`` happy path, ``on_worker_state_changed`` dispatch, the
preference helpers' guards, and the ``build_runtime`` factory.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import MagicMock, patch

import pytest
from textual.worker import Worker, WorkerState

from muxdeck.app import (
    MuxdeckApp,
    MuxdeckRuntime,
    _get_tmux_safe_driver,
    _urgency_for,
    build_runtime,
)
from muxdeck.config import (
    AppConfig,
    GeneralConfig,
    PathsConfig,
)
from muxdeck.controllers import (
    DashboardAgentListItemView,
    DashboardFilterState,
    DashboardHealthSummary,
    DashboardSelectedAgentView,
    DashboardSort,
    DashboardState,
)
from muxdeck.controllers.attention_controller import AttentionController
from muxdeck.controllers.dashboard_controller import DashboardAlertView
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.models import Session
from muxdeck.screens.dashboard import DashboardScreen
from muxdeck.services.attention_service import (
    AttentionInboxService,
    AttentionNotification,
)
from muxdeck.services.runtime_service import RuntimeSyncReport
from muxdeck.ui_preferences import UiDensity, UiPreferences
from muxdeck.widgets.common import KeyHintFooter, TabBar

_TS = datetime(2025, 1, 1, 12, tzinfo=UTC)


# ── shared fakes ────────────────────────────────────────────────────


class _FakeConfigGeneral:
    discovery_interval_sec: int = 600
    log_preview_lines: int = 8
    idle_threshold_sec: int = 300


class _FakeConfig:
    general = _FakeConfigGeneral()


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
        return tuple(session for session in sessions if session.agent_id == agent_id)


def _agent_item() -> DashboardAgentListItemView:
    return DashboardAgentListItemView(
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


def _dashboard_state(item: DashboardAgentListItemView) -> DashboardState:
    return DashboardState(
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
        agents=(item,),
        selected_agent_id=item.agent_id,
        selected_agent=DashboardSelectedAgentView(
            item=item,
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


class _FakeDashboardController:
    def __init__(self) -> None:
        self.build_state_calls: int = 0
        self.build_items_calls: int = 0
        self.build_alerts_calls: int = 0
        self._item = _agent_item()
        self._alert = DashboardAlertView(
            agent_id="agent-1",
            agent_name="agent-1",
            severity="error",
            title="agent failed",
            message="boom",
            occurred_at=_TS,
            alert_id="agent-1:failed",
        )

    def build_state(self, **kwargs: object) -> DashboardState:
        del kwargs
        self.build_state_calls += 1
        return _dashboard_state(self._item)

    def build_selected_agent_view(
        self,
        item: DashboardAgentListItemView,
        *,
        preview_line_limit: int = 8,
    ) -> DashboardSelectedAgentView:
        del preview_line_limit
        return DashboardSelectedAgentView(
            item=item,
            repo_root="/repo",
            worktree_id="wt-1",
            session_count=0,
            open_session_id=None,
            copilot_session_id=None,
            latest_event_kind=None,
            latest_event_severity=None,
            latest_event_at=None,
            log_preview=(),
        )

    def build_agent_items(self) -> tuple[DashboardAgentListItemView, ...]:
        self.build_items_calls += 1
        return (self._item,)

    def build_alerts_from_items(
        self,
        items: Sequence[DashboardAgentListItemView],
        *,
        limit: int = 20,
    ) -> tuple[DashboardAlertView, ...]:
        del items, limit
        self.build_alerts_calls += 1
        return (self._alert,)


class _FakeSynchronizer:
    def __init__(self) -> None:
        self.call_count: int = 0

    def refresh(self) -> RuntimeSyncReport:
        self.call_count += 1
        return RuntimeSyncReport()


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Literal["low", "normal", "critical"]]] = []

    def notify(
        self,
        title: str,
        body: str,
        urgency: Literal["low", "normal", "critical"],
    ) -> None:
        self.calls.append((title, body, urgency))


def _make_runtime(
    *,
    synchronizer: object | None = None,
    sync_dashboard: object | None = None,
    attention: object | None = None,
    notifier: object | None = None,
) -> MuxdeckRuntime:
    return cast(
        MuxdeckRuntime,
        type(
            "FakeRuntime",
            (),
            {
                "config": _FakeConfig(),
                "store": _FakeStore(),
                "dashboard": _FakeDashboardController(),
                "worktrees": object(),
                "replay": object(),
                "agents": object(),
                "synchronizer": synchronizer,
                "sync_store": None,
                "sync_dashboard": sync_dashboard,
                "attention": attention,
                "notifier": notifier,
                "actions": None,
            },
        )(),
    )


def _make_event(
    *,
    state: WorkerState,
    group: str = "sync",
    result: object | None = None,
    error: BaseException | None = None,
) -> Worker.StateChanged:
    worker = SimpleNamespace(state=state, group=group, result=result, error=error)
    return cast(Worker.StateChanged, SimpleNamespace(state=state, worker=worker))


# ── _urgency_for branches ───────────────────────────────────────────


class UrgencyForTests(unittest.TestCase):
    """``_urgency_for`` maps notification severities to OS notifier urgencies."""

    def _notification(self, severity: str) -> AttentionNotification:
        return AttentionNotification(
            alert_id="x",
            severity=cast(Literal["info", "warning", "error"], severity),
            title="t",
            message="m",
        )

    def test_error_severity_maps_to_critical(self) -> None:
        assert _urgency_for(self._notification("error")) == "critical"

    def test_info_severity_maps_to_low(self) -> None:
        assert _urgency_for(self._notification("info")) == "low"

    def test_warning_severity_maps_to_normal(self) -> None:
        assert _urgency_for(self._notification("warning")) == "normal"


# ── _get_tmux_safe_driver win32 + driver behaviour ──────────────────


class TmuxSafeDriverPlatformTests(unittest.TestCase):
    def test_returns_none_on_win32_even_inside_tmux(
        self,
    ) -> None:
        # Both TMUX set and platform "win32" must return None to avoid
        # importing the linux-only driver on Windows.
        with (
            patch.dict("os.environ", {"TMUX": "/tmp/tmux/0,1,2"}),
            patch.object(sys, "platform", "win32"),
        ):
            assert _get_tmux_safe_driver() is None

    def test_driver_start_application_mode_writes_pop_kitty_sequence(self) -> None:
        # Patch the parent ``LinuxDriver.start_application_mode`` so we can
        # exercise the override without actually opening a terminal.
        from textual.drivers.linux_driver import LinuxDriver

        with patch.dict("os.environ", {"TMUX": "/tmp/tmux/0,1,2"}):
            driver_cls = _get_tmux_safe_driver()
            assert driver_cls is not None

        instance = driver_cls.__new__(driver_cls)
        written: list[str] = []
        flushed: list[bool] = []
        instance.write = lambda data: written.append(data)  # type: ignore[method-assign]
        instance.flush = lambda: flushed.append(True)  # type: ignore[method-assign]

        with patch.object(LinuxDriver, "start_application_mode", lambda self: None):
            driver_cls.start_application_mode(instance)

        assert written == ["\x1b[<u"]
        assert flushed == [True]


# ── on_mount + action_show_* + reset preferences ────────────────────


class AppLifecycleAndActionTests(unittest.TestCase):
    """Mounting wires every mode and each action_show_* switches mode."""

    def test_each_action_show_helper_invokes_activate_mode_with_its_label(self) -> None:
        """Each ``action_show_*`` delegates to ``_activate_mode`` with a name."""
        # Use a minimal app instance and stub ``_activate_mode`` to capture the
        # mode name. This avoids the cost of fully mounting every concrete
        # screen — the action methods themselves are the unit under test.
        attention = AttentionController(_FakeDashboardController(), AttentionInboxService())
        runtime = _make_runtime(attention=attention)
        app = MuxdeckApp(runtime)
        recorded: list[str] = []
        app._activate_mode = recorded.append  # type: ignore[assignment]

        app.action_show_dashboard()
        app.action_show_worktrees()
        app.action_show_replay()
        app.action_show_sessions()
        app.action_show_setup()
        app.action_show_attention()
        app.action_show_help()

        assert recorded == [
            "dashboard",
            "worktrees",
            "replay",
            "sessions",
            "setup",
            "attention",
            "help",
        ]

    def test_attention_mode_registered_when_runtime_provides_attention(self) -> None:
        """``on_mount`` registers the attention mode only when wired in."""

        async def scenario() -> tuple[bool, bool]:
            attention = AttentionController(_FakeDashboardController(), AttentionInboxService())
            runtime = _make_runtime(attention=attention)
            app_with = MuxdeckApp(runtime)
            async with app_with.run_test() as pilot:
                await pilot.pause()
                with_attention_registered = "attention" in app_with._modes

            runtime_no = _make_runtime(attention=None)
            app_without = MuxdeckApp(runtime_no)
            async with app_without.run_test() as pilot:
                await pilot.pause()
                without_attention_registered = "attention" in app_without._modes
            return with_attention_registered, without_attention_registered

        with_, without_ = asyncio.run(scenario())
        assert with_ is True
        assert without_ is False

    def test_action_show_attention_is_noop_when_runtime_has_no_attention(self) -> None:
        async def scenario() -> str:
            runtime = _make_runtime(attention=None)
            app = MuxdeckApp(runtime)
            async with app.run_test() as pilot:
                await pilot.pause()
                # No "attention" mode is registered when runtime.attention is
                # None — the action_show_attention call short-circuits.
                app.action_show_attention()
                await pilot.pause()
                return app.screen.__class__.__name__

        assert asyncio.run(scenario()) == "DashboardScreen"

    def test_reset_ui_preferences_when_already_default_only_sets_status(self) -> None:
        async def scenario() -> str:
            runtime = _make_runtime()
            app = MuxdeckApp(runtime)
            async with app.run_test() as pilot:
                await pilot.pause()
                assert app.ui_preferences.is_default
                app.action_reset_ui_preferences()
                await pilot.pause()
                return app.screen.query_one(KeyHintFooter).status

        status = asyncio.run(scenario())
        assert status == "ui modes already at defaults"

    def test_reset_ui_preferences_after_toggle_restores_defaults(self) -> None:
        # Drive the unit method directly so the dashboard refresh chain does
        # not race with the status setter via async events.
        runtime = _make_runtime()
        app = MuxdeckApp(runtime)
        # Toggle density off-default without going through the screen.
        app.ui_preferences = UiPreferences(density=UiDensity.COMFORTABLE)
        assert not app.ui_preferences.is_default
        # Stub the screen-touching helpers so the call is synchronous.
        app._apply_ui_preferences = lambda *, refresh_screen: None  # type: ignore[method-assign]
        statuses: list[str] = []
        app._set_screen_status = statuses.append  # type: ignore[assignment]

        app.action_reset_ui_preferences()

        assert app.ui_preferences.is_default
        assert statuses == ["ui modes reset"]

    def test_open_attention_system_command_only_present_with_attention(self) -> None:
        async def scenario() -> tuple[bool, bool]:
            attention = AttentionController(_FakeDashboardController(), AttentionInboxService())
            runtime_with = _make_runtime(attention=attention)
            app_with = MuxdeckApp(runtime_with)
            async with app_with.run_test() as pilot:
                await pilot.pause()
                titles_with = {cmd.title for cmd in app_with.get_system_commands(app_with.screen)}

            runtime_without = _make_runtime(attention=None)
            app_without = MuxdeckApp(runtime_without)
            async with app_without.run_test() as pilot:
                await pilot.pause()
                titles_without = {
                    cmd.title for cmd in app_without.get_system_commands(app_without.screen)
                }
            return ("Open attention" in titles_with, "Open attention" in titles_without)

        with_attention, without_attention = asyncio.run(scenario())
        assert with_attention is True
        assert without_attention is False


# ── set_tab_badge tab-bar refresh path ──────────────────────────────


class SetTabBadgeRefreshTests(unittest.TestCase):
    def test_set_tab_badge_propagates_to_mounted_tab_bar(self) -> None:
        async def scenario() -> Mapping[str, int]:
            runtime = _make_runtime()
            app = MuxdeckApp(runtime)
            async with app.run_test() as pilot:
                await pilot.pause()
                app.set_tab_badge("attention", 4)
                await pilot.pause()
                tab_bar = app.screen.query_one(TabBar)
                return dict(tab_bar.badges)

        assert asyncio.run(scenario()) == {"attention": 4}


# ── _run_sync happy and degenerate paths ────────────────────────────


class RunSyncBehaviourTests(unittest.TestCase):
    def test_run_sync_returns_none_when_no_synchronizer(self) -> None:
        runtime = _make_runtime(synchronizer=None)
        app = MuxdeckApp(runtime)
        assert app._run_sync() is None

    def test_run_sync_full_dashboard_attention_path(self) -> None:
        async def scenario() -> tuple[
            int,
            int,
            bool,
            int | None,
        ]:
            sync_dashboard = _FakeDashboardController()
            attention = AttentionController(sync_dashboard, AttentionInboxService())
            synchronizer = _FakeSynchronizer()
            runtime = _make_runtime(
                synchronizer=synchronizer,
                sync_dashboard=sync_dashboard,
                attention=attention,
            )
            app = MuxdeckApp(runtime)
            async with app.run_test() as pilot:
                await pilot.pause()
                # First sync from on_mount has already executed by now.
                # Reset counters to focus on the direct call below.
                sync_dashboard.build_items_calls = 0
                sync_dashboard.build_state_calls = 0
                sync_dashboard.build_alerts_calls = 0
                # Direct call exercises the worker-thread routine inline.
                result = app._run_sync()

            assert result is not None
            return (
                sync_dashboard.build_items_calls,
                sync_dashboard.build_alerts_calls,
                result.dashboard_state is not None,
                result.attention_unread_count,
            )

        items_calls, alerts_calls, has_dash, unread = asyncio.run(scenario())
        assert items_calls == 1
        assert alerts_calls == 1
        # Dashboard screen is mounted ⇒ ``build_state`` runs and produces a
        # non-None state on the result.
        assert has_dash is True
        # The inbox tracks the alert observed during the on_mount sync, so
        # the unread count is at least 1.
        assert unread is not None
        assert unread >= 1

    def test_run_sync_skips_dashboard_when_screen_query_raises(self) -> None:
        # When ``self.screen`` raises (no app mounted), the screen branch is
        # skipped but build_agent_items / attention paths still run.
        sync_dashboard = _FakeDashboardController()
        attention = AttentionController(sync_dashboard, AttentionInboxService())
        synchronizer = _FakeSynchronizer()
        runtime = _make_runtime(
            synchronizer=synchronizer,
            sync_dashboard=sync_dashboard,
            attention=attention,
        )
        app = MuxdeckApp(runtime)
        # No run_test ⇒ accessing self.screen raises ScreenStackError, which
        # the broad-except in ``_run_sync`` catches and turns into screen=None.
        result = app._run_sync()
        assert result is not None
        # Dashboard branch is skipped because screen is None ⇒ no state.
        assert result.dashboard_state is None
        # Attention path still runs and observes the alert.
        assert sync_dashboard.build_alerts_calls == 1

    def test_run_sync_returns_minimal_result_without_sync_dashboard(self) -> None:
        synchronizer = _FakeSynchronizer()
        runtime = _make_runtime(
            synchronizer=synchronizer,
            sync_dashboard=None,
            attention=None,
        )
        app = MuxdeckApp(runtime)
        result = app._run_sync()
        assert result is not None
        assert result.dashboard_state is None
        assert result.attention_notifications == ()
        assert result.attention_unread_count is None

    def test_run_sync_swallows_synchronizer_exception(self) -> None:
        class _ExplodingSynchronizer:
            def refresh(self) -> RuntimeSyncReport:
                msg = "tmux missing"
                raise RuntimeError(msg)

        runtime = _make_runtime(
            synchronizer=_ExplodingSynchronizer(),
            sync_dashboard=_FakeDashboardController(),
            attention=None,
        )
        app = MuxdeckApp(runtime)
        # Worker-side exception is logged and swallowed → returns None.
        assert app._run_sync() is None


# ── on_worker_state_changed dispatch ────────────────────────────────


class WorkerStateChangedDispatchTests(unittest.TestCase):
    def test_unknown_worker_group_short_circuits(self) -> None:
        runtime = _make_runtime(synchronizer=_FakeSynchronizer())
        app = MuxdeckApp(runtime)
        app._sync_in_progress = True
        # An event from a different worker group must not flip the in-progress
        # flag or otherwise mutate state.
        app.on_worker_state_changed(_make_event(state=WorkerState.SUCCESS, group="other"))
        assert app._sync_in_progress is True

    def test_success_event_with_result_records_report_and_dispatches(self) -> None:
        # Build a runtime with attention so dispatch runs the badge update.
        attention = AttentionController(_FakeDashboardController(), AttentionInboxService())
        notifier = _RecordingNotifier()
        runtime = _make_runtime(
            synchronizer=_FakeSynchronizer(),
            attention=attention,
            notifier=notifier,
        )
        app = MuxdeckApp(runtime)
        # Stub _refresh_screen_widgets so the dashboard-refresh consumer
        # cannot clear ``last_dashboard_state`` after we set it.
        refresh_calls: list[bool] = []
        app._refresh_screen_widgets = lambda *, force=False: refresh_calls.append(force)  # type: ignore[method-assign]

        report = RuntimeSyncReport()
        state = _dashboard_state(_agent_item())
        result = MuxdeckApp._SyncResult(
            report=report,
            dashboard_state=state,
            attention_notifications=(
                AttentionNotification(
                    alert_id="agent-9:failed",
                    severity="error",
                    title="agent-9 failed",
                    message="boom",
                ),
            ),
            attention_unread_count=2,
        )
        app._sync_in_progress = True
        app._manual_refresh = True
        app.on_worker_state_changed(_make_event(state=WorkerState.SUCCESS, result=result))

        assert app.last_sync_report is report
        assert app.last_dashboard_state is state
        assert app._sync_in_progress is False
        # Dispatch wired through to the notifier with the critical mapping.
        assert notifier.calls == [("agent-9 failed", "boom", "critical")]
        # Manual flag was consumed and forwarded to the refresh helper.
        assert refresh_calls == [True]
        assert app._manual_refresh is False

    def test_error_event_logs_warning_and_clears_in_progress(self) -> None:
        runtime = _make_runtime(synchronizer=_FakeSynchronizer())
        app = MuxdeckApp(runtime)
        app._refresh_screen_widgets = lambda *, force=False: None  # type: ignore[method-assign]
        app._sync_in_progress = True
        app.on_worker_state_changed(
            _make_event(state=WorkerState.ERROR, error=RuntimeError("kaboom"))
        )
        assert app._sync_in_progress is False

    def test_periodic_perf_summary_fires_every_10_cycles(self) -> None:
        runtime = _make_runtime(synchronizer=_FakeSynchronizer())
        app = MuxdeckApp(runtime)
        app._refresh_screen_widgets = lambda *, force=False: None  # type: ignore[method-assign]
        calls: list[bool] = []

        def fake_perf_summary(*, reset: bool = True) -> None:
            calls.append(reset)

        with patch("muxdeck.app.perf_log_summary", fake_perf_summary):
            # Force the cycle counter into a known state and trigger
            # exactly one event that crosses the modulo boundary.
            import muxdeck.app as app_module

            app_module._sync_cycle_count = 9
            app._sync_in_progress = True
            app.on_worker_state_changed(_make_event(state=WorkerState.SUCCESS, result=None))
        assert calls == [True]

    def test_pending_refresh_is_drained_after_event(self) -> None:
        synchronizer = _FakeSynchronizer()
        runtime = _make_runtime(synchronizer=synchronizer)
        app = MuxdeckApp(runtime)
        # Replace the worker spawn so we count without going through Textual.
        worker_spawned: list[bool] = []
        app._refresh_screen_widgets = lambda *, force=False: None  # type: ignore[method-assign]
        original_refresh = MuxdeckApp._refresh_current_screen.__get__(app)

        def fake_refresh(*, manual: bool = False) -> None:
            worker_spawned.append(manual)

        app._refresh_current_screen = fake_refresh  # type: ignore[method-assign]
        app._sync_in_progress = True
        app._refresh_pending = True
        app.on_worker_state_changed(_make_event(state=WorkerState.SUCCESS, result=None))
        # Ensure the captured original is referenced (linter happiness).
        assert callable(original_refresh)
        assert worker_spawned == [False]
        assert app._refresh_pending is False


# ── _dispatch_attention_notifications notifier=None branch ─────────


class DispatchAttentionWithoutNotifierTests(unittest.TestCase):
    def test_attention_present_but_no_notifier_only_updates_badge(self) -> None:
        attention = AttentionController(_FakeDashboardController(), AttentionInboxService())
        runtime = _make_runtime(attention=attention, notifier=None)
        app = MuxdeckApp(runtime)
        notifications = (
            AttentionNotification(
                alert_id="agent-1:failed",
                severity="error",
                title="agent-1 failed",
                message="boom",
            ),
        )
        app._dispatch_attention_notifications(notifications, unread_count=5)
        # Tab badge reflects the unread count provided.
        assert app.tab_badges.get("attention") == 5


# ── _refresh_screen_widgets / _set_screen_status guards ────────────


class RefreshScreenWidgetsGuardsTests(unittest.TestCase):
    def test_refresh_screen_widgets_skips_when_screen_lacks_callable_refresher(
        self,
    ) -> None:
        async def scenario() -> bool:
            runtime = _make_runtime()
            app = MuxdeckApp(runtime)
            async with app.run_test() as pilot:
                await pilot.pause()
                # Replace the dashboard's ``refresh_data`` with a non-callable
                # so the guard at the bottom of ``_refresh_screen_widgets``
                # exits before invoking the refresher.
                cast(Any, app.screen).refresh_data = None
                # Force=True bypasses the screen-type filter so we land on the
                # callable check.
                app._refresh_screen_widgets(force=True)
            return True

        assert asyncio.run(scenario()) is True

    def test_refresh_screen_widgets_skips_non_dashboard_screens_without_force(
        self,
    ) -> None:
        """When the active screen isn't dashboard/attention/sessions and
        ``force=False``, ``_refresh_screen_widgets`` must short-circuit
        WITHOUT touching the screen's ``refresh_data`` (otherwise the
        periodic sync would spam git subprocesses on the worktree screen).

        The earlier version of this test just returned ``1`` from the
        scenario closure and asserted ``== 1`` — it never observed
        whether the refresh actually fired. Spy on the help screen's
        ``refresh_data`` so the missing skip would be observable.
        """

        async def scenario() -> tuple[int, int]:
            runtime = _make_runtime()
            app = MuxdeckApp(runtime)
            async with app.run_test() as pilot:
                await pilot.pause()
                app.action_show_help()
                await pilot.pause()
                refresh_calls: list[bool] = []
                cast(Any, app.screen).refresh_data = lambda: refresh_calls.append(True)
                # Without force=True this should be a no-op.
                app._refresh_screen_widgets(force=False)
                no_force = len(refresh_calls)
                # With force=True it must fall through and actually
                # call refresh_data — proves the spy works.
                app._refresh_screen_widgets(force=True)
                with_force = len(refresh_calls)
            return no_force, with_force

        no_force, with_force = asyncio.run(scenario())
        assert no_force == 0, (
            f"non-dashboard screen refreshed without force: got {no_force} call(s)"
        )
        assert with_force == 1, f"force=True must still call refresh_data, got {with_force} call(s)"


# ── _set_ui_preferences and helpers ────────────────────────────────


class SetUiPreferencesGuardsTests(unittest.TestCase):
    def test_setting_same_preferences_only_updates_status(self) -> None:
        async def scenario() -> str:
            runtime = _make_runtime()
            app = MuxdeckApp(runtime)
            async with app.run_test() as pilot:
                await pilot.pause()
                # Same preferences as current ⇒ takes the no-change branch.
                app._set_ui_preferences(app.ui_preferences, message="unchanged")
                await pilot.pause()
                return app.screen.query_one(KeyHintFooter).status

        assert asyncio.run(scenario()) == "unchanged"

    def test_apply_ui_preferences_returns_when_no_screen_mounted(self) -> None:
        runtime = _make_runtime()
        app = MuxdeckApp(runtime)
        # Pre-mount: no screen is on the stack, so ``_apply_ui_preferences``
        # exits after toggling app classes/sub_title without poking screens.
        app._apply_ui_preferences(refresh_screen=True)
        assert app.sub_title == "Operator Console"

    def test_apply_ui_preferences_falls_back_to_refresh_when_applier_returns_false(
        self,
    ) -> None:
        async def scenario() -> int:
            runtime = _make_runtime()
            app = MuxdeckApp(runtime)
            async with app.run_test() as pilot:
                await pilot.pause()
                refresh_calls: list[bool] = []
                # Override ``apply_ui_preferences`` to return False so the
                # refresh branch fires. ``refresh_data`` is overridden to a
                # recording lambda.
                cast(Any, app.screen).apply_ui_preferences = lambda: False
                cast(Any, app.screen).refresh_data = lambda: refresh_calls.append(True)
                app._apply_ui_preferences(refresh_screen=True)
                await pilot.pause()
            return len(refresh_calls)

        # Applier handled the refresh — fallback skipped.
        assert asyncio.run(scenario()) == 1

    def test_apply_ui_preferences_skips_refresh_when_applier_returns_true(self) -> None:
        async def scenario() -> int:
            runtime = _make_runtime()
            app = MuxdeckApp(runtime)
            async with app.run_test() as pilot:
                await pilot.pause()
                refresh_calls: list[bool] = []
                cast(Any, app.screen).apply_ui_preferences = lambda: True
                cast(Any, app.screen).refresh_data = lambda: refresh_calls.append(True)
                app._apply_ui_preferences(refresh_screen=True)
                await pilot.pause()
            return len(refresh_calls)

        # Applier handled the refresh — fallback skipped.
        assert asyncio.run(scenario()) == 0

    def test_apply_ui_preferences_handles_screen_without_applier(self) -> None:
        async def scenario() -> bool:
            runtime = _make_runtime()
            app = MuxdeckApp(runtime)
            async with app.run_test() as pilot:
                await pilot.pause()
                # Drop both helpers so the ``callable(...)`` checks take the
                # False branch.
                cast(Any, app.screen).apply_ui_preferences = None
                cast(Any, app.screen).refresh_data = None
                app._apply_ui_preferences(refresh_screen=True)
                await pilot.pause()
            return True

        assert asyncio.run(scenario()) is True


class SetScreenStatusGuardsTests(unittest.TestCase):
    def test_set_screen_status_returns_silently_without_screen(self) -> None:
        runtime = _make_runtime()
        app = MuxdeckApp(runtime)
        # No screen mounted ⇒ early return; must not raise.
        app._set_screen_status("noop")

    def test_set_screen_status_skips_when_screen_lacks_set_status(self) -> None:
        async def scenario() -> bool:
            runtime = _make_runtime()
            app = MuxdeckApp(runtime)
            async with app.run_test() as pilot:
                await pilot.pause()
                cast(Any, app.screen).set_status = None
                app._set_screen_status("noop")
                await pilot.pause()
            return True

        assert asyncio.run(scenario()) is True


# ── _current_screen ScreenStackError ───────────────────────────────


class CurrentScreenStackErrorTests(unittest.TestCase):
    def test_current_screen_returns_none_when_screen_stack_empty(self) -> None:
        runtime = _make_runtime()
        app = MuxdeckApp(runtime)
        # Pre-mount: the screen stack is empty, so accessing ``self.screen``
        # raises ``ScreenStackError`` and ``_current_screen`` returns None.
        assert app._current_screen() is None


# ── build_runtime end-to-end smoke ─────────────────────────────────


@contextmanager
def _isolate_environment(tmp_path: Path) -> Iterator[None]:
    """Force the Windows-host detection to skip and protect the local env."""
    keys_to_clear = ("USERPROFILE", "WINDOWSSESSIONS_DIR", "TMUX_PANE")
    saved: dict[str, str | None] = {key: __import__("os").environ.get(key) for key in keys_to_clear}
    import os as _os

    for key in keys_to_clear:
        _os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                _os.environ.pop(key, None)
            else:
                _os.environ[key] = value


def _temp_app_config(tmp_path: Path) -> AppConfig:
    state_dir = tmp_path / "state"
    db_path = state_dir / "muxdeck.db"
    fallback_db = tmp_path / "fallback.db"
    workspace = tmp_path / "workspace"
    return AppConfig(
        paths=PathsConfig(
            state_dir=state_dir,
            workspace_root=workspace,
            database_path=db_path,
            fallback_database_path=fallback_db,
        ),
        general=GeneralConfig(),
        config_file=tmp_path / "config.toml",
    )


@pytest.fixture
def build_runtime_isolated(tmp_path: Path) -> Iterator[Callable[[], MuxdeckRuntime]]:
    """Return a factory that builds a runtime against a tmp state directory."""
    config = _temp_app_config(tmp_path)
    with _isolate_environment(tmp_path):
        yield lambda: build_runtime(config)


def test_build_runtime_constructs_full_runtime_in_tmp_state_dir(
    build_runtime_isolated: Callable[[], MuxdeckRuntime],
) -> None:
    runtime = build_runtime_isolated()
    try:
        # The factory must wire every required collaborator the app uses.
        assert runtime.config is not None
        assert runtime.store is not None
        assert runtime.dashboard is not None
        assert runtime.worktrees is not None
        assert runtime.replay is not None
        assert runtime.agents is not None
        assert runtime.synchronizer is not None
        assert runtime.sync_store is not None
        assert runtime.sync_dashboard is not None
        assert runtime.sync_worktrees is not None
        assert runtime.sessions_ctrl is not None
        assert runtime.setup is not None
        assert runtime.attention is not None
        assert runtime.tmux is not None
        assert runtime.pane_stream is not None
        assert runtime.session_resolver is not None
        assert runtime.actions is not None
        # State directory was created on disk by the factory.
        assert runtime.config.paths.state_dir.exists()
    finally:
        runtime.store.close()
        if runtime.sync_store is not None:
            runtime.sync_store.close()


def test_build_runtime_with_default_config_uses_supplied_paths(
    tmp_path: Path,
) -> None:
    # Calling build_runtime with config=None falls back to AppConfig.default()
    # which reads XDG_STATE_HOME — point that at the tmp dir so the test
    # never writes to the real user's state dir.
    state_root = tmp_path / "xdg-state"
    state_root.mkdir()
    monkeypatch_env = {
        "XDG_STATE_HOME": str(state_root),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "HOME": str(tmp_path / "home"),
    }
    keys_to_unset = ("USERPROFILE", "WINDOWSSESSIONS_DIR", "TMUX_PANE")
    import os as _os

    saved_env: dict[str, str | None] = {key: _os.environ.get(key) for key in monkeypatch_env}
    saved_unset: dict[str, str | None] = {key: _os.environ.get(key) for key in keys_to_unset}
    for key, value in monkeypatch_env.items():
        _os.environ[key] = value
    for key in keys_to_unset:
        _os.environ.pop(key, None)
    try:
        runtime = build_runtime(None)
        try:
            assert runtime.config.paths.state_dir.is_relative_to(state_root)
        finally:
            runtime.store.close()
            if runtime.sync_store is not None:
                runtime.sync_store.close()
    finally:
        for key, env_value in saved_env.items():
            if env_value is None:
                _os.environ.pop(key, None)
            else:
                _os.environ[key] = env_value
        for key, env_value in saved_unset.items():
            if env_value is None:
                _os.environ.pop(key, None)
            else:
                _os.environ[key] = env_value


# ── safety-net suppressed: unused import to silence ruff ───────────


_DENSITY = UiDensity  # keep imported enum referenced for clarity in tests
_PREFS = UiPreferences  # keep imported preference dataclass referenced
_DASH_SCREEN = DashboardScreen  # ensure dashboard class import exercised
_MAGIC = MagicMock  # keep MagicMock import referenced for future tests
