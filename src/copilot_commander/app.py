from __future__ import annotations

import logging
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
from copilot_commander.bindings import GLOBAL_BINDINGS
from copilot_commander.config import AppConfig, load_config
from copilot_commander.controllers import (
    AgentController,
    DashboardController,
    ReplayController,
    WorktreeController,
)
from copilot_commander.screens import (
    DashboardScreen,
    HelpScreen,
    ReplayScreen,
    WorktreesScreen,
)
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

_log = logging.getLogger(__name__)

_SYNC_GROUP = "sync"


@dataclass(slots=True)
class CommanderRuntime:
    config: AppConfig
    store: SQLiteStore
    dashboard: DashboardController
    worktrees: WorktreeController
    replay: ReplayController
    agents: AgentController
    synchronizer: RuntimeSynchronizer | None = None
    sync_store: SQLiteStore | None = None


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
        self._sync_in_progress: bool = False
        self._refresh_pending: bool = False

    def on_mount(self) -> None:
        self.add_mode("dashboard", lambda: DashboardScreen(self.runtime))
        self.add_mode("worktrees", lambda: WorktreesScreen(self.runtime))
        self.add_mode("replay", lambda: ReplayScreen(self.runtime))
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

    def action_show_help(self) -> None:
        self.switch_mode("help")

    def action_refresh_screen(self) -> None:
        self._refresh_current_screen()

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

    def _refresh_current_screen(self) -> None:
        synchronizer = self.runtime.synchronizer
        if synchronizer is None:
            self._refresh_screen_widgets()
            return
        if self._sync_in_progress:
            self._refresh_pending = True
            return
        self._sync_in_progress = True
        self.run_worker(
            self._run_sync,
            thread=True,
            exclusive=True,
            group=_SYNC_GROUP,
        )

    def _run_sync(self) -> RuntimeSyncReport | None:
        synchronizer = self.runtime.synchronizer
        if synchronizer is None:
            return None
        try:
            return synchronizer.refresh()
        except Exception:
            _log.exception("sync worker error")
            return None

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != _SYNC_GROUP:
            return
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            self._sync_in_progress = False
            if event.state == WorkerState.SUCCESS and event.worker.result is not None:
                self.last_sync_report = event.worker.result
            elif event.state == WorkerState.ERROR:
                _log.warning("sync worker failed: %s", event.worker.error)
            self._refresh_screen_widgets()
            if self._refresh_pending:
                self._refresh_pending = False
                self._refresh_current_screen()

    def _refresh_screen_widgets(self) -> None:
        screen = self.screen
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
    discovery = DiscoveryService(
        tmux_adapter,
        copilot_adapter,
        sync_store,
        process_inspector=process_adapter,
        capture_start_line=-max(resolved_config.general.log_preview_lines, 200),
    )
    worktree_service = WorktreeService(
        config=resolved_config,
        git=git_adapter,
        worktrees=store,
        agents=store,
        session_contexts=store,
    )
    return CommanderRuntime(
        config=resolved_config,
        store=store,
        dashboard=DashboardController(store),
        worktrees=WorktreeController(worktree_service, store),
        replay=ReplayController(replay_service),
        agents=AgentController(store, sessions),
        synchronizer=RuntimeSynchronizer(discovery, monitoring, git_adapter),
        sync_store=sync_store,
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
