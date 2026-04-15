from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from textual.app import App
from textual.worker import Worker, WorkerState

from copilot_commander.adapters import (
    CopilotAdapter,
    GitAdapter,
    ProcessAdapter,
    SQLiteStore,
    TmuxAdapter,
)
from copilot_commander.adapters.copilot_session_store import CopilotSessionStore
from copilot_commander.bindings import GLOBAL_BINDINGS
from copilot_commander.config import AppConfig, load_config
from copilot_commander.controllers import (
    AgentController,
    DashboardController,
    DashboardState,
    ReplayController,
    WorktreeController,
)
from copilot_commander.controllers.sessions_controller import SessionsController
from copilot_commander.perf import log_summary as perf_log_summary
from copilot_commander.perf import timed
from copilot_commander.screens import (
    DashboardScreen,
    HelpScreen,
    ReplayScreen,
    WorktreesScreen,
)
from copilot_commander.screens.sessions import SessionsScreen
from copilot_commander.services import (
    AgentService,
    DiscoveryService,
    MonitoringService,
    MonitoringThresholds,
    ReplayService,
    RuntimeSynchronizer,
    RuntimeSyncReport,
    SessionService,
    WorktreeService,
)
from copilot_commander.services.action_service import TmuxActionService

_log = logging.getLogger(__name__)

_SYNC_GROUP = "sync"
_PERF_LOG_INTERVAL = 10  # log perf summary every N sync cycles
_sync_cycle_count = 0


@dataclass(slots=True)
class CommanderRuntime:
    config: AppConfig
    store: SQLiteStore
    dashboard: DashboardController
    worktrees: WorktreeController
    replay: ReplayController
    agents: AgentController
    actions: TmuxActionService | None = None
    synchronizer: RuntimeSynchronizer | None = None
    sync_store: SQLiteStore | None = None
    sessions_ctrl: SessionsController | None = None
    sync_dashboard: DashboardController | None = None


class CommanderApp(App[None]):
    CSS_PATH = "styles.tcss"
    BINDINGS = GLOBAL_BINDINGS
    TITLE = "Copilot Commander"
    SUB_TITLE = "Operator Console"

    def __init__(self, runtime: CommanderRuntime) -> None:
        super().__init__()
        self.runtime = runtime
        self.selected_agent_id: str | None = None
        self.selected_worktree_id: str | None = None
        self.selected_session_id: str | None = None
        self.last_sync_report: RuntimeSyncReport | None = None
        self.last_dashboard_state: DashboardState | None = None
        self._sync_in_progress: bool = False
        self._refresh_pending: bool = False
        self._manual_refresh: bool = False

    def on_mount(self) -> None:
        self.add_mode("dashboard", lambda: DashboardScreen(self.runtime))
        self.add_mode("worktrees", lambda: WorktreesScreen(self.runtime))
        self.add_mode("replay", lambda: ReplayScreen(self.runtime))
        self.add_mode("sessions", lambda: SessionsScreen(self.runtime))
        self.add_mode("help", lambda: HelpScreen(self.runtime))
        self.switch_mode("dashboard")
        interval_sec = max(2, self.runtime.config.general.discovery_interval_sec)
        self.call_after_refresh(self._refresh_current_screen)
        self.set_interval(interval_sec, self._refresh_current_screen)

    def action_show_dashboard(self) -> None:
        self.switch_mode("dashboard")

    def action_show_worktrees(self) -> None:
        self.switch_mode("worktrees")

    def action_show_replay(self) -> None:
        self.switch_mode("replay")

    def action_show_sessions(self) -> None:
        self.switch_mode("sessions")

    def action_show_help(self) -> None:
        self.switch_mode("help")

    def action_refresh_screen(self) -> None:
        self._refresh_current_screen(manual=True)

    def remember_agent_selection(self, agent_id: str) -> None:
        self.selected_agent_id = agent_id

    def remember_worktree_selection(self, worktree_id: str) -> None:
        self.selected_worktree_id = worktree_id

    def remember_session_selection(self, session_id: str) -> None:
        self.selected_session_id = session_id

    def resolve_replay_session_id(self, current_session_id: str | None = None) -> str | None:
        if (
            current_session_id is not None
            and self.runtime.store.get_session(current_session_id) is not None
        ):
            return current_session_id
        if self.selected_agent_id is not None:
            agent_sessions = self.runtime.store.list_sessions(self.selected_agent_id)
            if agent_sessions:
                return agent_sessions[0].id
        if (
            self.selected_session_id is not None
            and self.runtime.store.get_session(self.selected_session_id) is not None
        ):
            return self.selected_session_id
        sessions = self.runtime.store.list_sessions()
        return sessions[0].id if sessions else None

    # ── sync lifecycle ───────────────────────────────────────────────

    def _refresh_current_screen(self, *, manual: bool = False) -> None:
        synchronizer = self.runtime.synchronizer
        if synchronizer is None:
            self._refresh_screen_widgets(force=True)
            return
        if self._sync_in_progress:
            self._refresh_pending = True
            if manual:
                self._manual_refresh = True
            return
        self._sync_in_progress = True
        if manual:
            self._manual_refresh = True
        self.run_worker(
            self._run_sync,
            thread=True,
            exclusive=True,
            group=_SYNC_GROUP,
        )

    @dataclass(frozen=True, slots=True)
    class _SyncResult:
        report: RuntimeSyncReport
        dashboard_state: DashboardState | None = None

    def _run_sync(self) -> _SyncResult | None:
        synchronizer = self.runtime.synchronizer
        if synchronizer is None:
            return None
        try:
            with timed("sync.total"):
                report = synchronizer.refresh()
            # Build dashboard state here (worker thread) to avoid
            # blocking the main UI thread with SQLite queries.
            dashboard_state = None
            sync_dashboard = self.runtime.sync_dashboard
            if sync_dashboard is not None:
                screen = self.screen
                if isinstance(screen, DashboardScreen):
                    with timed("sync.build_dashboard"):
                        dashboard_state = sync_dashboard.build_state(
                            filters=screen.current_filters,
                            sort=screen.current_sort,
                            selected_agent_id=screen.current_selected_agent_id,
                            preview_line_limit=min(
                                self.runtime.config.general.log_preview_lines, 12
                            ),
                        )
            return CommanderApp._SyncResult(
                report=report,
                dashboard_state=dashboard_state,
            )
        except Exception:
            _log.exception("sync worker error")
            return None

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != _SYNC_GROUP:
            return
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            self._sync_in_progress = False
            manual = self._manual_refresh
            self._manual_refresh = False
            if event.state == WorkerState.SUCCESS and event.worker.result is not None:
                result = event.worker.result
                self.last_sync_report = result.report
                if result.dashboard_state is not None:
                    self.last_dashboard_state = result.dashboard_state
            elif event.state == WorkerState.ERROR:
                _log.warning("sync worker failed: %s", event.worker.error)
            with timed("ui.refresh_widgets"):
                self._refresh_screen_widgets(force=manual)
            # Periodic perf summary
            global _sync_cycle_count
            _sync_cycle_count += 1
            if _sync_cycle_count % _PERF_LOG_INTERVAL == 0:
                perf_log_summary(reset=True)
            if self._refresh_pending:
                self._refresh_pending = False
                self._refresh_current_screen()

    def _refresh_screen_widgets(self, *, force: bool = False) -> None:
        screen = self.screen
        # Periodic sync only auto-refreshes the dashboard.
        # Other screens refresh on tab switch (on_show) or manual r key.
        if not force and not isinstance(screen, DashboardScreen):
            return
        refresher = getattr(screen, "refresh_data", None)
        if callable(refresher):
            refresher()


def build_runtime(config: AppConfig | None = None) -> CommanderRuntime:
    resolved_config = AppConfig.default() if config is None else config
    resolved_config.paths.state_dir.mkdir(parents=True, exist_ok=True)
    resolved_config.paths.database_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore.from_config(resolved_config)
    # Dedicated store for the sync worker thread to avoid cross-thread
    # sqlite3 access.  Both connections target the same WAL-mode database.
    sync_store = SQLiteStore.from_config(resolved_config, check_same_thread=False)
    process_adapter = ProcessAdapter()
    git_adapter = GitAdapter(process_adapter)
    tmux_adapter = TmuxAdapter(process_adapter)
    action_service = TmuxActionService(tmux=tmux_adapter)
    copilot_adapter = CopilotAdapter(process_adapter)
    sessions = SessionService(store=store)
    replay_service = ReplayService(store=store, sessions=sessions)
    sync_agent_service = AgentService(
        sync_store,
        sync_store,
        sync_store,
        sync_store,
        sync_store,
    )
    monitoring = MonitoringService(
        sync_agent_service,
        thresholds=MonitoringThresholds(
            waiting_input_after_seconds=max(15, resolved_config.general.discovery_interval_sec * 2),
            idle_after_seconds=resolved_config.general.idle_threshold_sec,
            attention_idle_after_seconds=max(
                resolved_config.general.idle_threshold_sec * 3,
                resolved_config.general.idle_threshold_sec + 60,
            ),
        ),
    )
    # Filter out the TUI's own tmux pane from discovery to avoid
    # self-detection as a copilot agent.
    self_pane = os.environ.get("TMUX_PANE")
    ignore_panes: frozenset[str] = frozenset({self_pane}) if self_pane else frozenset()
    discovery = DiscoveryService(
        tmux_adapter,
        copilot_adapter,
        sync_store,
        process_inspector=process_adapter,
        capture_start_line=-max(resolved_config.general.log_preview_lines, 200),
        ignore_pane_ids=ignore_panes,
    )
    worktree_service = WorktreeService(
        config=resolved_config,
        git=git_adapter,
        worktrees=store,
        agents=store,
        session_contexts=store,
    )
    copilot_session_store = CopilotSessionStore()
    sessions_ctrl = SessionsController(copilot_session_store)
    sync_dashboard = DashboardController(sync_store)
    return CommanderRuntime(
        config=resolved_config,
        store=store,
        dashboard=DashboardController(store),
        worktrees=WorktreeController(worktree_service, store),
        replay=ReplayController(replay_service),
        agents=AgentController(store, sessions),
        actions=action_service,
        synchronizer=RuntimeSynchronizer(
            discovery,
            monitoring,
            git_adapter,
            agent_store=sync_store,
            dead_grace_period_sec=resolved_config.general.dead_grace_period_sec,
        ),
        sync_store=sync_store,
        sessions_ctrl=sessions_ctrl,
        sync_dashboard=sync_dashboard,
    )


def run_app(config_path: str | Path | None = None) -> int:
    config = load_config(config_path)
    runtime = build_runtime(config)
    try:
        CommanderApp(runtime).run()
    finally:
        runtime.store.close()
        if runtime.sync_store is not None:
            runtime.sync_store.close()
    return 0


__all__ = ["CommanderApp", "CommanderRuntime", "build_runtime", "run_app"]
