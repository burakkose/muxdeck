from __future__ import annotations

import logging
import os
import sqlite3
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

from textual.app import App, ScreenStackError, SystemCommand
from textual.driver import Driver
from textual.screen import Screen
from textual.worker import Worker, WorkerState

from muxdeck.adapters import (
    CopilotAdapter,
    GitAdapter,
    ProcessAdapter,
    SQLiteStore,
    TmuxAdapter,
)
from muxdeck.adapters.copilot_activity_reader import CopilotActivityReader
from muxdeck.adapters.copilot_session_resolver import InuseLockResolver
from muxdeck.adapters.copilot_session_store import (
    CopilotSessionStore,
    SessionStoreRoot,
)
from muxdeck.adapters.os_notifier import OsNotifier, detect_os_notifier
from muxdeck.adapters.pane_stream import PaneStreamAdapter
from muxdeck.adapters.sqlite_replay_annotations import (
    SqliteReplayAnnotationsRepository,
)
from muxdeck.adapters.subagent_reader import SubAgentReader
from muxdeck.adapters.windows_host import WindowsHostInfo, detect_windows_host
from muxdeck.bindings import GLOBAL_BINDINGS
from muxdeck.config import AppConfig, load_config
from muxdeck.controllers import (
    AgentController,
    AttentionController,
    DashboardController,
    DashboardState,
    ReplayController,
    WorktreeController,
)
from muxdeck.controllers.sessions_controller import SessionsController
from muxdeck.exceptions import PersistenceError
from muxdeck.perf import log_summary as perf_log_summary
from muxdeck.perf import timed
from muxdeck.screens import (
    AttentionScreen,
    DashboardScreen,
    HelpScreen,
    ReplayScreen,
    SetupScreen,
    WorktreesScreen,
)
from muxdeck.screens.sessions import SessionsScreen
from muxdeck.services import (
    AgentService,
    AnnotationsService,
    AttentionInboxService,
    DiscoveryService,
    MonitoringService,
    MonitoringThresholds,
    ReplayService,
    RuntimeSynchronizer,
    RuntimeSyncReport,
    SessionService,
    SetupDoctorService,
    WorktreeService,
)
from muxdeck.services.action_service import TmuxActionService
from muxdeck.services.attention_service import AttentionNotification
from muxdeck.services.monitoring_service import MonitoringLocalSessionStore
from muxdeck.services.runtime_service import RuntimeSubAgentReaderPort
from muxdeck.services.subtask_registry import SubTaskRegistry
from muxdeck.ui_preferences import (
    UiContrast,
    UiDecorations,
    UiDensity,
    UiGlyphs,
    UiPreferences,
)
from muxdeck.widgets.common import TabBar

_log = logging.getLogger(__name__)

_SYNC_GROUP = "sync"
_PERF_LOG_INTERVAL = 10  # log perf summary every N sync cycles
_sync_cycle_count = 0
_FALSEY_ENV_VALUES = frozenset({"", "0", "false", "no", "off"})
_UI_CLASS_PREFIXES = (
    "ux-density-",
    "ux-glyphs-",
    "ux-contrast-",
    "ux-decor-",
    "ux-wrap-",
    "ux-nowrap-",
)


_URGENCY_BY_SEVERITY: dict[str, str] = {
    "error": "critical",
    "warning": "normal",
    "info": "low",
}


def _urgency_for(notification: AttentionNotification) -> Literal["low", "normal", "critical"]:
    urgency = _URGENCY_BY_SEVERITY.get(notification.severity, "normal")
    if urgency == "critical":
        return "critical"
    if urgency == "low":
        return "low"
    return "normal"


def _command_logging_enabled() -> bool:
    value = os.environ.get("MUXDECK_LOG")
    if value is None:
        return False
    return value.strip().casefold() not in _FALSEY_ENV_VALUES


@dataclass(slots=True)
class MuxdeckRuntime:
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
    # Thread-safe worktree controller for worker-thread usage (worktrees screen).
    sync_worktrees: WorktreeController | None = None
    setup: SetupDoctorService | None = None
    attention: AttentionController | None = None
    tmux: TmuxAdapter | None = None
    pane_stream: PaneStreamAdapter | None = None
    session_resolver: InuseLockResolver | None = None
    notifier: OsNotifier | None = None


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
            super().start_application_mode()  # type: ignore[no-untyped-call]
            # Pop the Kitty keyboard protocol so tmux can see its prefix key.
            self.write("\x1b[<u")
            self.flush()

    return _TmuxSafeDriver


class MuxdeckApp(App[None]):
    CSS_PATH = "styles.tcss"
    BINDINGS = GLOBAL_BINDINGS
    TITLE = "Muxdeck"
    SUB_TITLE = "Operator Console"
    COMMAND_PALETTE_DISPLAY = "ctrl+p"

    def __init__(self, runtime: MuxdeckRuntime) -> None:
        super().__init__(driver_class=_get_tmux_safe_driver())
        self.runtime = runtime
        self.selected_agent_id: str | None = None
        self.selected_worktree_id: str | None = None
        self.selected_session_id: str | None = None
        self.last_sync_report: RuntimeSyncReport | None = None
        self.last_dashboard_state: DashboardState | None = None
        # Flips to True after the first sync attempt finishes (success
        # *or* failure). Screens use it to decide whether to wait on a
        # pending sync or fall back to a local build of stale data.
        self.sync_attempted: bool = False
        self._sync_in_progress: bool = False
        self._refresh_pending: bool = False
        self._manual_refresh: bool = False
        self.tab_badges: dict[str, int] = {}
        self.ui_preferences = UiPreferences()

    def set_tab_badge(self, name: str, count: int) -> None:
        """Update the badge count for a tab and refresh any mounted TabBar."""
        safe_count = max(0, int(count))
        if self.tab_badges.get(name, 0) == safe_count:
            return
        if safe_count == 0:
            self.tab_badges.pop(name, None)
        else:
            self.tab_badges[name] = safe_count
        try:
            screen = self.screen
        except Exception:
            return
        for tab_bar in screen.query(TabBar):
            tab_bar.set_badges(self.tab_badges)

    def on_mount(self) -> None:
        attention = getattr(self.runtime, "attention", None)
        self.add_mode("dashboard", lambda: DashboardScreen(self.runtime))
        self.add_mode("worktrees", lambda: WorktreesScreen(self.runtime))
        self.add_mode("replay", lambda: ReplayScreen(self.runtime))
        self.add_mode("sessions", lambda: SessionsScreen(self.runtime))
        self.add_mode("setup", lambda: SetupScreen(self.runtime))
        if attention is not None:
            self.add_mode("attention", lambda: AttentionScreen(self.runtime))
        self.add_mode("help", lambda: HelpScreen(self.runtime))
        self._activate_mode("dashboard")
        self._apply_ui_preferences(refresh_screen=False)
        interval_sec = max(2, self.runtime.config.general.discovery_interval_sec)
        self.call_after_refresh(self._refresh_current_screen)
        self.set_interval(interval_sec, self._refresh_current_screen)

    def action_show_dashboard(self) -> None:
        self._activate_mode("dashboard")

    def action_show_worktrees(self) -> None:
        self._activate_mode("worktrees")

    def action_show_replay(self) -> None:
        self._activate_mode("replay")

    def action_show_sessions(self) -> None:
        self._activate_mode("sessions")

    def action_show_setup(self) -> None:
        self._activate_mode("setup")

    def action_show_attention(self) -> None:
        if getattr(self.runtime, "attention", None) is None:
            return
        self._activate_mode("attention")

    def action_show_help(self) -> None:
        self._activate_mode("help")

    def action_refresh_screen(self) -> None:
        self._refresh_current_screen(manual=True)

    def action_toggle_density(self) -> None:
        next_density = (
            UiDensity.COMFORTABLE
            if self.ui_preferences.density is UiDensity.COMPACT
            else UiDensity.COMPACT
        )
        self._set_ui_preferences(
            replace(self.ui_preferences, density=next_density),
            message=(
                "comfortable density on"
                if next_density is UiDensity.COMFORTABLE
                else "comfortable density off"
            ),
        )

    def action_toggle_glyphs(self) -> None:
        next_glyphs = (
            UiGlyphs.ASCII if self.ui_preferences.glyphs is UiGlyphs.RICH else UiGlyphs.RICH
        )
        self._set_ui_preferences(
            replace(self.ui_preferences, glyphs=next_glyphs),
            message=("simple glyphs on" if next_glyphs is UiGlyphs.ASCII else "simple glyphs off"),
        )

    def action_toggle_contrast(self) -> None:
        next_contrast = (
            UiContrast.HIGH
            if self.ui_preferences.contrast is UiContrast.STANDARD
            else UiContrast.STANDARD
        )
        self._set_ui_preferences(
            replace(self.ui_preferences, contrast=next_contrast),
            message=(
                "high contrast on" if next_contrast is UiContrast.HIGH else "high contrast off"
            ),
        )

    def action_toggle_decorations(self) -> None:
        next_decorations = (
            UiDecorations.REDUCED
            if self.ui_preferences.decorations is UiDecorations.FULL
            else UiDecorations.FULL
        )
        self._set_ui_preferences(
            replace(self.ui_preferences, decorations=next_decorations),
            message=(
                "reduced decoration on"
                if next_decorations is UiDecorations.REDUCED
                else "reduced decoration off"
            ),
        )

    def action_toggle_log_wrap(self) -> None:
        wrap_logs = not self.ui_preferences.wrap_logs
        self._set_ui_preferences(
            replace(self.ui_preferences, wrap_logs=wrap_logs),
            message=("log wrap on" if wrap_logs else "log wrap off"),
        )

    def action_reset_ui_preferences(self) -> None:
        if self.ui_preferences.is_default:
            self._set_screen_status("ui modes already at defaults")
            return
        self._set_ui_preferences(UiPreferences(), message="ui modes reset")

    def get_system_commands(self, screen: Screen[object]) -> list[SystemCommand]:
        commands = list(super().get_system_commands(screen))
        commands.extend(
            (
                SystemCommand(
                    "Open dashboard", "Switch to the dashboard", self.action_show_dashboard
                ),
                SystemCommand(
                    "Open worktrees", "Switch to the worktree browser", self.action_show_worktrees
                ),
                SystemCommand("Open replay", "Switch to replay mode", self.action_show_replay),
                SystemCommand(
                    "Open sessions", "Switch to the sessions browser", self.action_show_sessions
                ),
                SystemCommand("Open setup", "Switch to setup diagnostics", self.action_show_setup),
                SystemCommand(
                    "Open help", "Show the searchable help reference", self.action_show_help
                ),
                SystemCommand(
                    "Toggle comfortable density",
                    (
                        "Show more context in lists"
                        if self.ui_preferences.density is UiDensity.COMPACT
                        else "Return to the compact list density"
                    ),
                    self.action_toggle_density,
                ),
                SystemCommand(
                    "Toggle simple glyphs",
                    (
                        "Use ASCII-safe symbols"
                        if self.ui_preferences.glyphs is UiGlyphs.RICH
                        else "Restore the richer symbol set"
                    ),
                    self.action_toggle_glyphs,
                ),
                SystemCommand(
                    "Toggle high contrast",
                    (
                        "Strengthen focus and contrast"
                        if self.ui_preferences.contrast is UiContrast.STANDARD
                        else "Return to the standard contrast mode"
                    ),
                    self.action_toggle_contrast,
                ),
                SystemCommand(
                    "Toggle reduced decoration",
                    (
                        "Simplify borders and separators"
                        if self.ui_preferences.decorations is UiDecorations.FULL
                        else "Restore the fuller visual decoration"
                    ),
                    self.action_toggle_decorations,
                ),
                SystemCommand(
                    "Toggle log wrap",
                    (
                        "Wrap long live log lines"
                        if not self.ui_preferences.wrap_logs
                        else "Keep live logs unwrapped"
                    ),
                    self.action_toggle_log_wrap,
                ),
            )
        )
        if getattr(self.runtime, "attention", None) is not None:
            commands.append(
                SystemCommand(
                    "Open attention", "Switch to the attention inbox", self.action_show_attention
                )
            )
        if not self.ui_preferences.is_default:
            commands.append(
                SystemCommand(
                    "Reset UI modes",
                    "Restore the default density, glyph, contrast, and wrapping",
                    self.action_reset_ui_preferences,
                )
            )
        commands.extend(self._screen_commands(screen))
        return commands

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
        attention_notifications: tuple[AttentionNotification, ...] = ()
        attention_unread_count: int | None = None

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
            attention_notifications: tuple[AttentionNotification, ...] = ()
            attention_unread_count: int | None = None
            sync_dashboard = self.runtime.sync_dashboard
            attention = self.runtime.attention
            if sync_dashboard is not None:
                # Compute per-agent items once and reuse them for both
                # the dashboard render and the attention alert path.
                # Each item involves several SQLite queries and a
                # JSONL tail read; building them twice (the previous
                # behavior) doubled the cost of every sync cycle.
                with timed("sync.build_items"):
                    agent_items = sync_dashboard.build_agent_items()
                try:
                    screen = self.screen
                except Exception:
                    screen = None
                if isinstance(screen, DashboardScreen):
                    with timed("sync.build_dashboard"):
                        dashboard_state = sync_dashboard.build_state(
                            filters=screen.current_filters,
                            sort=screen.current_sort,
                            selected_agent_id=screen.current_selected_agent_id,
                            preview_line_limit=min(
                                self.runtime.config.general.log_preview_lines, 200
                            ),
                            precomputed_items=agent_items,
                        )
                # Attention only needs the alert list — derive it
                # directly from the precomputed items instead of
                # paying for a second full ``build_state`` (which
                # would re-run filter/sort/select and another
                # ``_build_selected_agent`` round-trip).
                if attention is not None:
                    with timed("sync.build_attention"):
                        alerts = sync_dashboard.build_alerts_from_items(agent_items, limit=20)
                    attention_notifications = attention.observe_alerts(alerts)
                    attention_unread_count = attention.unread_count
            return MuxdeckApp._SyncResult(
                report=report,
                dashboard_state=dashboard_state,
                attention_notifications=attention_notifications,
                attention_unread_count=attention_unread_count,
            )
        except Exception as exc:
            # Thread workers cannot be hard-cancelled, so a sync started
            # just before app shutdown can race the SQLite ``close()``
            # in :func:`run_app`. Surfacing that race as an
            # ``_log.exception`` traceback alarms operators who think
            # the sync crashed mid-run, when in fact the app is just
            # tearing down. Detect the closed-database signature and
            # swallow it quietly; everything else still gets logged.
            if isinstance(exc, PersistenceError) and "closed database" in str(exc).lower():
                return None
            cause = exc.__cause__
            if isinstance(cause, sqlite3.ProgrammingError) and "closed" in str(cause).lower():
                return None
            _log.exception("sync worker error")
            return None

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != _SYNC_GROUP:
            return
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            self._sync_in_progress = False
            # Mark the first sync attempt as complete so the dashboard
            # stops waiting on it. Even an errored/cancelled sync
            # signals that the synchronizer is no longer about to
            # deliver fresh data, and the dashboard should fall back
            # to whatever the local SQLite store has rather than
            # showing "syncing fleet…" forever.
            self.sync_attempted = True
            manual = self._manual_refresh
            self._manual_refresh = False
            if event.state == WorkerState.SUCCESS and event.worker.result is not None:
                result = event.worker.result
                self.last_sync_report = result.report
                if result.dashboard_state is not None:
                    self.last_dashboard_state = result.dashboard_state
                self._dispatch_attention_notifications(
                    result.attention_notifications,
                    unread_count=result.attention_unread_count,
                )
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

    def _dispatch_attention_notifications(
        self,
        notifications: tuple[AttentionNotification, ...],
        *,
        unread_count: int | None,
    ) -> None:
        attention = self.runtime.attention
        if attention is None:
            return
        notifier = self.runtime.notifier
        if notifier is not None:
            for notification in notifications:
                notifier.notify(
                    notification.title,
                    notification.message,
                    _urgency_for(notification),
                )
        self.set_tab_badge(
            "attention",
            unread_count if unread_count is not None else attention.unread_count,
        )

    def _refresh_screen_widgets(self, *, force: bool = False) -> None:
        screen = self.screen
        # Periodic sync auto-refreshes the screens where staleness is
        # user-visible. Other screens (worktrees, replay, setup) only
        # refresh on tab-switch (on_show) or a manual `r` key so we don't
        # spam git subprocesses or filesystem scans.
        if not force and not isinstance(
            screen,
            DashboardScreen | AttentionScreen | SessionsScreen,
        ):
            return
        refresher = getattr(screen, "refresh_data", None)
        if callable(refresher):
            refresher()

    def _activate_mode(self, mode_name: str) -> None:
        self.switch_mode(mode_name)
        self._apply_ui_preferences(refresh_screen=False)

    def _current_screen(self) -> Screen[object] | None:
        try:
            return self.screen
        except ScreenStackError:
            return None

    def _screen_commands(self, screen: Screen[object]) -> list[SystemCommand]:
        command_specs = (
            ("Focus filter", "Jump to the current screen filter input", "focus_filter"),
            ("Toggle attention filter", "Show only items that need review", "toggle_attention"),
            ("Toggle completed items", "Show or hide completed items", "toggle_completed"),
            ("Focus replay markers", "Move focus to replay markers", "focus_markers"),
            ("Focus replay transcript", "Move focus to the replay transcript", "focus_transcript"),
            (
                "Toggle replay presentation",
                "Switch between parsed and raw replay views",
                "toggle_presentation",
            ),
            (
                "Toggle replay follow latest",
                "Follow or release the newest replay entry",
                "toggle_follow_latest",
            ),
            ("Toggle live log follow", "Pause or resume live log following", "toggle_follow"),
            ("Close current screen", "Dismiss the current modal screen", "close"),
        )
        commands: list[SystemCommand] = []
        for title, help_text, action_name in command_specs:
            callback = getattr(screen, f"action_{action_name}", None)
            if callable(callback):
                commands.append(SystemCommand(title, help_text, callback))
        return commands

    def _set_ui_preferences(self, preferences: UiPreferences, *, message: str) -> None:
        if preferences == self.ui_preferences:
            self._set_screen_status(message)
            return
        self.ui_preferences = preferences
        self._apply_ui_preferences(refresh_screen=True)
        self._set_screen_status(message)

    def _apply_ui_preferences(self, *, refresh_screen: bool) -> None:
        for class_name in tuple(self.classes):
            if class_name.startswith(_UI_CLASS_PREFIXES):
                self.remove_class(class_name)
        for class_name in self.ui_preferences.css_classes():
            self.add_class(class_name)
        mode_badges = self.ui_preferences.mode_badges()
        if mode_badges:
            self.sub_title = f"Operator Console · {' · '.join(mode_badges)}"
        else:
            self.sub_title = "Operator Console"
        screen = self._current_screen()
        if screen is None:
            return
        handled = False
        applier = getattr(screen, "apply_ui_preferences", None)
        if callable(applier):
            handled = bool(applier())
        if refresh_screen and not handled:
            refresher = getattr(screen, "refresh_data", None)
            if callable(refresher):
                refresher()
        screen.refresh(repaint=True, layout=True)

    def _set_screen_status(self, message: str) -> None:
        screen = self._current_screen()
        if screen is None:
            return
        setter = getattr(screen, "set_status", None)
        if callable(setter):
            setter(message)


def build_runtime(config: AppConfig | None = None) -> MuxdeckRuntime:
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
    copilot_adapter = CopilotAdapter(process_adapter)
    action_service = TmuxActionService(tmux=tmux_adapter, copilot=copilot_adapter)
    pane_stream_adapter = PaneStreamAdapter(tmux=tmux_adapter)
    sessions = SessionService(store=store)
    replay_service = ReplayService(store=store, sessions=sessions)
    annotations_service = AnnotationsService(SqliteReplayAnnotationsRepository(store))
    # Thread-safe replay service for worker-thread usage (replay screen).
    sync_sessions = SessionService(store=sync_store)
    sync_replay_service = ReplayService(store=sync_store, sessions=sync_sessions)
    sync_annotations_service = AnnotationsService(SqliteReplayAnnotationsRepository(sync_store))
    sync_agent_service = AgentService(
        sync_store,
        sync_store,
        sync_store,
        sync_store,
        sync_store,
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
    subtask_registry = SubTaskRegistry()
    subagent_reader = SubAgentReader(copilot_session_store)
    activity_reader = CopilotActivityReader(store=copilot_session_store)
    session_resolver = InuseLockResolver(copilot_session_store)
    monitoring = MonitoringService(
        sync_agent_service,
        session_resolver=session_resolver,
        local_session_store=cast(MonitoringLocalSessionStore, copilot_session_store),
        log_history=sync_store,
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
    dashboard = DashboardController(
        store,
        subtask_registry=subtask_registry,
        subagent_reader=subagent_reader,
        session_resolver=session_resolver,
        activity_reader=activity_reader,
    )
    agent_controller = AgentController(store, sessions)
    sync_dashboard = DashboardController(
        sync_store,
        subtask_registry=subtask_registry,
        subagent_reader=subagent_reader,
        session_resolver=session_resolver,
        activity_reader=activity_reader,
    )
    # AttentionController.build_state internally calls dashboard.build_state,
    # which is run from a worker thread (see AttentionScreen). Use the
    # thread-safe sync_dashboard so the worker never touches the
    # main-thread-bound store. ``mark_read`` and ``unread_count`` only touch
    # the local inbox state and remain UI-thread safe.
    attention = AttentionController(sync_dashboard, AttentionInboxService())
    return MuxdeckRuntime(
        config=resolved_config,
        store=store,
        dashboard=dashboard,
        worktrees=WorktreeController(worktree_service, store),
        sync_worktrees=WorktreeController(sync_worktree_service, sync_store),
        replay=ReplayController(replay_service, annotations_service),
        replay_worker=ReplayController(sync_replay_service, sync_annotations_service),
        agents=agent_controller,
        actions=action_service,
        synchronizer=RuntimeSynchronizer(
            discovery,
            monitoring,
            git_adapter,
            agent_store=sync_store,
            worktree_sync=sync_worktree_service,
            subtask_registry=subtask_registry,
            subagent_reader=cast(RuntimeSubAgentReaderPort, subagent_reader),
            session_resolver=session_resolver,
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
        tmux=tmux_adapter,
        pane_stream=pane_stream_adapter,
        session_resolver=session_resolver,
        notifier=detect_os_notifier(),
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
        MuxdeckApp(runtime).run()
    finally:
        perf_log_summary(reset=True)
        runtime.store.close()
        if runtime.sync_store is not None:
            runtime.sync_store.close()
    return 0


__all__ = ["MuxdeckApp", "MuxdeckRuntime", "build_runtime", "run_app"]
