from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from textual.app import App
from textual.driver import Driver
from textual.worker import Worker, WorkerState

from copilot_commander.adapters import (
    CopilotAdapter,
    GitAdapter,
    ProcessAdapter,
    SQLiteStore,
    TmuxAdapter,
)
from copilot_commander.adapters.copilot_session_store import (
    CopilotSessionStore,
    SessionStoreRoot,
)
from copilot_commander.adapters.subagent_reader import SubAgentReader
from copilot_commander.adapters.windows_host import WindowsHostInfo, detect_windows_host
from copilot_commander.bindings import GLOBAL_BINDINGS
from copilot_commander.config import AppConfig, load_config
from copilot_commander.controllers import (
    AgentController,
    AttentionController,
    DashboardController,
    DashboardState,
    FleetController,
    OperationsController,
    ReplayController,
    WorktreeController,
)
from copilot_commander.controllers.sessions_controller import SessionsController
from copilot_commander.perf import log_summary as perf_log_summary
from copilot_commander.perf import timed
from copilot_commander.screens import (
    AttentionScreen,
    DashboardScreen,
    FleetScreen,
    HelpScreen,
    OperationsScreen,
    ReplayScreen,
    SetupScreen,
    WorktreesScreen,
)
from copilot_commander.screens.sessions import SessionsScreen
from copilot_commander.services import (
    AgentService,
    AttentionInboxService,
    DiscoveryService,
    MonitoringService,
    MonitoringThresholds,
    OperationAuditService,
    ReplayService,
    RuntimeSynchronizer,
    RuntimeSyncReport,
    SessionService,
    SetupDoctorService,
    WorktreeService,
)
from copilot_commander.services.action_service import TmuxActionService

_log = logging.getLogger(__name__)

_SYNC_GROUP = "sync"
_PERF_LOG_INTERVAL = 10  # log perf summary every N sync cycles
_sync_cycle_count = 0
_FALSEY_ENV_VALUES = frozenset({"", "0", "false", "no", "off"})


def _command_logging_enabled() -> bool:
    value = os.environ.get("COMMANDER_LOG")
    if value is None:
        return False
    return value.strip().casefold() not in _FALSEY_ENV_VALUES


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
    # Thread-safe replay controller for worker-thread usage (replay screen).
    replay_worker: ReplayController | None = None
    sync_store: SQLiteStore | None = None
    sessions_ctrl: SessionsController | None = None
    sync_dashboard: DashboardController | None = None
    setup: SetupDoctorService | None = None
    attention: AttentionController | None = None
    operations: OperationsController | None = None
    fleet: FleetController | None = None


def _get_tmux_safe_driver() -> type[Driver] | None:
    """Return a driver that disables the Kitty keyboard protocol inside tmux.

    Textual enables the Kitty keyboard protocol (``\\x1b[>1u``) which changes
    how modifier keys are encoded.  tmux (with ``extended-keys off``, the
    default) does not recognise the enhanced encoding, so the prefix key
    (e.g. Ctrl-A) stops working.  When we detect a tmux session we pop the
    protocol immediately after Textual pushes it.
    """
    if not os.environ.get("TMUX"):
        return None
    if sys.platform == "win32":
        return None

    from textual.drivers.linux_driver import LinuxDriver

    class _TmuxSafeDriver(LinuxDriver):
        def start_application_mode(self) -> None:
            super().start_application_mode()
            # Pop the Kitty keyboard protocol so tmux can see its prefix key.
            self.write("\x1b[<u")
            self.flush()

    return _TmuxSafeDriver


class CommanderApp(App[None]):
    CSS_PATH = "styles.tcss"
    BINDINGS = GLOBAL_BINDINGS
    TITLE = "Copilot Commander"
    SUB_TITLE = "Operator Console"

    def __init__(self, runtime: CommanderRuntime) -> None:
        super().__init__(driver_class=_get_tmux_safe_driver())
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
        attention = getattr(self.runtime, "attention", None)
        operations = getattr(self.runtime, "operations", None)
        fleet = getattr(self.runtime, "fleet", None)
        self.add_mode("dashboard", lambda: DashboardScreen(self.runtime))
        self.add_mode("worktrees", lambda: WorktreesScreen(self.runtime))
        self.add_mode("replay", lambda: ReplayScreen(self.runtime))
        self.add_mode("sessions", lambda: SessionsScreen(self.runtime))
        self.add_mode("setup", lambda: SetupScreen(self.runtime))
        if attention is not None:
            self.add_mode("attention", lambda: AttentionScreen(self.runtime))
        if operations is not None:
            self.add_mode(
                "operations",
                lambda: OperationsScreen(self.runtime, operations),
            )
        if fleet is not None:
            self.add_mode("fleet", lambda: FleetScreen(self.runtime, controller=fleet))
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

    def action_show_setup(self) -> None:
        self.switch_mode("setup")

    def action_show_attention(self) -> None:
        if getattr(self.runtime, "attention", None) is None:
            return
        self.switch_mode("attention")

    def action_show_operations(self) -> None:
        if getattr(self.runtime, "operations", None) is None:
            return
        self.switch_mode("operations")

    def action_show_fleet(self) -> None:
        if getattr(self.runtime, "fleet", None) is None:
            return
        self.switch_mode("fleet")

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
        # Try interpreting current_session_id as a copilot session ID
        if current_session_id is not None:
            by_copilot = self.runtime.store.get_session_by_copilot_session_id(current_session_id)
            if by_copilot is not None:
                return by_copilot.id
        if self.selected_agent_id is not None:
            agent_sessions = self.runtime.store.list_sessions(self.selected_agent_id)
            if agent_sessions:
                return agent_sessions[0].id
        if self.selected_session_id is not None:
            # selected_session_id may be an internal ID or a copilot session ID
            sess = self.runtime.store.get_session(self.selected_session_id)
            if sess is not None:
                return self.selected_session_id
            by_copilot = self.runtime.store.get_session_by_copilot_session_id(
                self.selected_session_id
            )
            if by_copilot is not None:
                return by_copilot.id
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
                                self.runtime.config.general.log_preview_lines, 24
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
        # Periodic sync auto-refreshes the screens where staleness is
        # user-visible. Other screens (worktrees, replay, setup) only
        # refresh on tab-switch (on_show) or a manual `r` key so we don't
        # spam git subprocesses or filesystem scans.
        if not force and not isinstance(
            screen,
            DashboardScreen | AttentionScreen | OperationsScreen | FleetScreen | SessionsScreen,
        ):
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
    tmux_adapter = TmuxAdapter(process_adapter, socket_path=resolved_config.tmux.socket_path)
    action_service = TmuxActionService(tmux=tmux_adapter)
    copilot_adapter = CopilotAdapter(process_adapter)
    sessions = SessionService(store=store)
    replay_service = ReplayService(store=store, sessions=sessions)
    # Thread-safe replay service for worker-thread usage (replay screen).
    sync_sessions = SessionService(store=sync_store)
    sync_replay_service = ReplayService(store=sync_store, sessions=sync_sessions)
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
    # Separate worktree service for sync worker thread (uses sync_store)
    sync_worktree_service = WorktreeService(
        config=resolved_config,
        git=git_adapter,
        worktrees=sync_store,
        agents=sync_store,
        session_contexts=sync_store,
    )
    copilot_session_store = CopilotSessionStore()
    # WSL users launch some agents through pwsh.exe, which stores its
    # session state under the Windows %USERPROFILE%. Bridge that here so
    # both roots feed the same Sessions screen and Setup diagnostics.
    windows_host: WindowsHostInfo = detect_windows_host(env=os.environ)
    if windows_host.is_available and windows_host.session_state_dir is not None:
        copilot_session_store.set_extra_roots(
            [SessionStoreRoot(windows_host.session_state_dir, "windows")]
        )
    sessions_ctrl = SessionsController(copilot_session_store)
    subagent_reader = SubAgentReader(copilot_session_store)
    dashboard = DashboardController(store, subagent_reader=subagent_reader)
    agent_controller = AgentController(store, sessions)
    attention = AttentionController(dashboard, AttentionInboxService())
    operations = OperationsController(
        dashboard,
        agent_controller,
        OperationAuditService(),
        actions=action_service,
    )
    fleet = FleetController(store, local_sessions=copilot_session_store)
    sync_dashboard = DashboardController(sync_store, subagent_reader=subagent_reader)
    return CommanderRuntime(
        config=resolved_config,
        store=store,
        dashboard=dashboard,
        worktrees=WorktreeController(worktree_service, store),
        replay=ReplayController(replay_service),
        replay_worker=ReplayController(sync_replay_service),
        agents=agent_controller,
        actions=action_service,
        synchronizer=RuntimeSynchronizer(
            discovery,
            monitoring,
            git_adapter,
            agent_store=sync_store,
            worktree_sync=sync_worktree_service,
            dead_grace_period_sec=resolved_config.general.dead_grace_period_sec,
        ),
        sync_store=sync_store,
        sessions_ctrl=sessions_ctrl,
        sync_dashboard=sync_dashboard,
        setup=SetupDoctorService(
            tmux_adapter,
            configured_socket_path=resolved_config.tmux.socket_path,
            windows_host_provider=lambda: windows_host,
            windows_session_count_provider=lambda: copilot_session_store.count_by_origin("windows"),
        ),
        attention=attention,
        operations=operations,
        fleet=fleet,
    )


def run_app(config_path: str | Path | None = None) -> int:
    if _command_logging_enabled():
        logging.basicConfig(
            level=logging.WARNING,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        )
    config = load_config(config_path)
    runtime = build_runtime(config)
    try:
        CommanderApp(runtime).run()
    finally:
        perf_log_summary(reset=True)
        runtime.store.close()
        if runtime.sync_store is not None:
            runtime.sync_store.close()
    return 0


__all__ = ["CommanderApp", "CommanderRuntime", "build_runtime", "run_app"]
