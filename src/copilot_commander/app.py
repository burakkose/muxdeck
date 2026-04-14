from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from textual.app import App

from copilot_commander.adapters import GitAdapter, ProcessAdapter, SQLiteStore
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
from copilot_commander.services import ReplayService, SessionService, WorktreeService


@dataclass(slots=True)
class CommanderRuntime:
    config: AppConfig
    store: SQLiteStore
    dashboard: DashboardController
    worktrees: WorktreeController
    replay: ReplayController
    agents: AgentController


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

    def on_mount(self) -> None:
        self.add_mode("dashboard", lambda: DashboardScreen(self.runtime))
        self.add_mode("worktrees", lambda: WorktreesScreen(self.runtime))
        self.add_mode("replay", lambda: ReplayScreen(self.runtime))
        self.add_mode("help", lambda: HelpScreen(self.runtime))
        self.switch_mode("dashboard")
        interval_sec = max(2, self.runtime.config.general.discovery_interval_sec)
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

    def _refresh_current_screen(self) -> None:
        screen = self.screen
        refresher = getattr(screen, "refresh_data", None)
        if callable(refresher):
            refresher()


def build_runtime(config: AppConfig | None = None) -> CommanderRuntime:
    resolved_config = AppConfig.default() if config is None else config
    resolved_config.paths.state_dir.mkdir(parents=True, exist_ok=True)
    resolved_config.paths.database_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore.from_config(resolved_config)
    sessions = SessionService(store=store)
    replay_service = ReplayService(store=store, sessions=sessions)
    worktree_service = WorktreeService(
        config=resolved_config,
        git=GitAdapter(ProcessAdapter()),
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
    )


def run_app(config_path: str | Path | None = None) -> int:
    config = load_config(config_path)
    runtime = build_runtime(config)
    try:
        CommanderApp(runtime).run()
    finally:
        runtime.store.close()
    return 0


__all__ = ["CommanderApp", "CommanderRuntime", "build_runtime", "run_app"]
