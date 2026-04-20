from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input
from textual.worker import Worker, WorkerState

from muxdeck.bindings import BindingSpec, KeyHint
from muxdeck.controllers.fleet_controller import (
    FleetAgentSummaryView,
    FleetController,
    FleetFilterState,
    FleetInboxItemView,
    FleetLocalSessionView,
    FleetRepoGroupView,
    FleetState,
    FleetStoryLaneView,
)
from muxdeck.screens.base import ShellScreen
from muxdeck.widgets.fleet import (
    FleetCommandDeckPanel,
    FleetHistoryPanel,
    FleetInboxPanel,
    FleetLocalSessionsPanel,
    FleetSearchPanel,
    FleetStoryLanesPanel,
    FleetSummaryBar,
)

if TYPE_CHECKING:
    from muxdeck.app import MuxdeckRuntime

_FLEET_BINDINGS: list[BindingSpec] = [
    Binding("j", "cursor_down", "Next story", show=False),
    Binding("k", "cursor_up", "Prev story", show=False),
    Binding("slash", "focus_filter", "Filter", show=False),
    Binding("escape", "escape_filter", "Back to lanes", show=False, priority=True),
    Binding("a", "toggle_attention", "Attention", show=False),
    Binding("x", "toggle_completed", "Completed", show=False),
    Binding("y", "copy_selection", "Copy", show=False),
]

_FLEET_HINTS = (
    KeyHint("j/k", "move"),
    KeyHint("/", "filter"),
    KeyHint("a", "attention"),
    KeyHint("x", "completed"),
    KeyHint("y", "copy"),
)

_WORKER_NAME = "fleet_load"


class FleetScreen(ShellScreen):
    SCREEN_TITLE = "FLEET"
    BINDINGS = _FLEET_BINDINGS
    FOOTER_HINTS = _FLEET_HINTS

    def __init__(
        self,
        runtime: MuxdeckRuntime,
        *,
        controller: FleetController | None = None,
        worker_controller: FleetController | None = None,
    ) -> None:
        super().__init__(runtime)
        self._controller = controller
        self._worker_controller = worker_controller
        self._filters = FleetFilterState(include_completed=False)
        self._state: FleetState | None = None
        self._selected_story_key: str | None = None
        self._loading: bool = False
        self._refresh_pending: bool = False

    def compose_body(self) -> ComposeResult:
        with Vertical(id="fleet-root"):
            yield FleetSummaryBar(id="fleet-summary")
            yield Input(
                placeholder="/ filter stories, repos, branches, sessions, or drift",
                id="fleet-filter-input",
            )
            with Horizontal(id="fleet-main", classes="frame"):
                yield FleetStoryLanesPanel(
                    widget_id="fleet-stories",
                    classes="divider-right focusable",
                )
                with Vertical(id="fleet-side"):
                    yield FleetCommandDeckPanel(id="fleet-command", classes="section")
                    with Horizontal(id="fleet-middle"):
                        yield FleetInboxPanel(
                            id="fleet-inbox",
                            classes="section divider-right",
                        )
                        yield FleetLocalSessionsPanel(
                            id="fleet-sessions",
                            classes="section",
                        )
                    with Horizontal(id="fleet-bottom"):
                        yield FleetHistoryPanel(
                            id="fleet-history",
                            classes="section divider-right",
                        )
                        yield FleetSearchPanel(id="fleet-search", classes="section")

    def on_mount(self) -> None:
        self.refresh_data()
        self.call_after_refresh(self.query_one(FleetStoryLanesPanel).focus_list)

    def on_show(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        if self._loading:
            self._refresh_pending = True
            return
        controller = self._resolve_controller()
        worker_controller = self._resolve_worker_controller()
        if controller is None and worker_controller is None:
            self.set_status("fleet controller unavailable")
            return
        first_load = self._state is None
        if first_load:
            self.set_status("loading fleet…")
            self.begin_loading(*self._loading_widgets())
        if worker_controller is None:
            if controller is None:
                self.set_status("fleet controller unavailable")
                self.end_loading(*self._loading_widgets())
                return
            self._apply_state(controller.build_state(filters=self._filters))
            self.end_loading(*self._loading_widgets())
            return
        filters = self._filters

        def _load() -> FleetState:
            return worker_controller.build_state(filters=filters)

        self._loading = True
        self.run_worker(_load, thread=True, exclusive=True, name=_WORKER_NAME)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != _WORKER_NAME:
            return
        if event.state == WorkerState.ERROR:
            self._loading = False
            self.end_loading(*self._loading_widgets())
            self.set_status("fleet load failed")
            self._schedule_pending_refresh()
            return
        if event.state == WorkerState.CANCELLED:
            self._loading = False
            self.end_loading(*self._loading_widgets())
            self._schedule_pending_refresh()
            return
        if event.state != WorkerState.SUCCESS:
            return
        self._loading = False
        self.end_loading(*self._loading_widgets())
        state = event.worker.result
        if state is None:
            self._schedule_pending_refresh()
            return
        self._apply_state(state)
        self._schedule_pending_refresh()

    def _schedule_pending_refresh(self) -> None:
        if not self._refresh_pending:
            return
        self._refresh_pending = False
        if self.is_mounted:
            self.call_after_refresh(self.refresh_data)

    def _loading_widgets(self) -> tuple[object, ...]:
        return (
            self.query_one(FleetStoryLanesPanel),
            self.query_one(FleetCommandDeckPanel),
            self.query_one(FleetInboxPanel),
            self.query_one(FleetLocalSessionsPanel),
            self.query_one(FleetHistoryPanel),
            self.query_one(FleetSearchPanel),
        )

    def _apply_state(self, state: FleetState) -> None:
        self._state = state
        if self._selected_story_key not in {story.story_key for story in state.story_lanes}:
            self._selected_story_key = state.story_lanes[0].story_key if state.story_lanes else None
        self.query_one(FleetSummaryBar).set_state(state)
        self.query_one(FleetStoryLanesPanel).set_lanes(
            state.story_lanes,
            selected_story_key=self._selected_story_key,
        )
        self.query_one(FleetSearchPanel).set_search(
            query=self._filters.normalized_query(),
            helpers=state.search_helpers,
            hits=state.search_hits,
        )
        self._render_selection()
        self._update_status()

    def _render_selection(self) -> None:
        selected_story = self._selected_story()
        selected_agents = self._selected_story_agents()
        selected_groups = self._selected_story_groups()
        selected_inbox = self._selected_story_inbox()
        selected_local_sessions = self._selected_story_local_sessions()
        self.query_one(FleetCommandDeckPanel).set_story(
            selected_story,
            repo_groups=selected_groups,
            agents=selected_agents,
            inbox_items=selected_inbox,
        )
        self.query_one(FleetInboxPanel).set_inbox(
            items=self._state.response_inbox if self._state is not None else (),
            selected_story_key=self._selected_story_key,
            selected_story_label=selected_story.story_label if selected_story is not None else None,
        )
        self.query_one(FleetLocalSessionsPanel).set_sessions(
            scope_label=selected_story.story_label if selected_story is not None else None,
            sessions=selected_local_sessions,
        )
        if self._state is None:
            self.query_one(FleetHistoryPanel).set_history((), ())
            return
        recent_activity = (
            tuple(
                item
                for item in self._state.recent_activity
                if selected_story is not None and item.story_key == selected_story.story_key
            )
            if selected_story is not None
            else self._state.recent_activity
        )
        self.query_one(FleetHistoryPanel).set_history(
            self._state.history_metrics,
            recent_activity,
            scope_label=selected_story.story_label if selected_story is not None else None,
        )

    def _selected_story(self) -> FleetStoryLaneView | None:
        if self._state is None:
            return None
        return next(
            (
                story
                for story in self._state.story_lanes
                if story.story_key == self._selected_story_key
            ),
            None,
        )

    def _selected_story_groups(self) -> tuple[FleetRepoGroupView, ...]:
        selected_story = self._selected_story()
        if self._state is None or selected_story is None:
            return ()
        return tuple(
            group for group in self._state.groups if group.repo_key in selected_story.repo_keys
        )

    def _selected_story_agents(self) -> tuple[FleetAgentSummaryView, ...]:
        selected_story = self._selected_story()
        if self._state is None or selected_story is None:
            return ()
        agent_ids = frozenset(selected_story.agent_ids)
        return tuple(
            agent
            for group in self._state.groups
            for agent in group.agents
            if agent.agent_id in agent_ids
        )

    def _selected_story_inbox(self) -> tuple[FleetInboxItemView, ...]:
        selected_story = self._selected_story()
        if self._state is None or selected_story is None:
            return ()
        return tuple(
            item
            for item in self._state.response_inbox
            if item.story_key == selected_story.story_key
        )

    def _selected_story_local_sessions(self) -> tuple[FleetLocalSessionView, ...]:
        selected_story = self._selected_story()
        if self._state is None or selected_story is None:
            return ()
        return tuple(
            session
            for session in self._state.local_sessions
            if session.session_id in selected_story.local_session_ids
        )

    def action_cursor_down(self) -> None:
        panel = self.query_one(FleetStoryLanesPanel)
        panel.move_cursor(1)
        self._selected_story_key = panel.current_story_key
        self._render_selection()
        self._update_status()

    def action_cursor_up(self) -> None:
        panel = self.query_one(FleetStoryLanesPanel)
        panel.move_cursor(-1)
        self._selected_story_key = panel.current_story_key
        self._render_selection()
        self._update_status()

    def action_focus_filter(self) -> None:
        self.query_one("#fleet-filter-input", Input).focus()
        self.set_status("filter fleet")

    def action_escape_filter(self) -> None:
        self.query_one(FleetStoryLanesPanel).focus_list()
        self._update_status()

    def action_toggle_attention(self) -> None:
        self._filters = FleetFilterState(
            text_query=self._filters.text_query,
            attention_only=not self._filters.attention_only,
            include_completed=self._filters.include_completed,
        )
        self.refresh_data()

    def action_toggle_completed(self) -> None:
        self._filters = FleetFilterState(
            text_query=self._filters.text_query,
            attention_only=self._filters.attention_only,
            include_completed=not self._filters.include_completed,
        )
        self.refresh_data()

    def action_copy_selection(self) -> None:
        self.copy_rendered_text(
            "fleet selection",
            self.query_one(FleetSummaryBar),
            self.query_one(FleetCommandDeckPanel),
            self.query_one(FleetInboxPanel),
            self.query_one(FleetLocalSessionsPanel),
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "fleet-filter-input":
            return
        self._filters = FleetFilterState(
            text_query=event.value,
            attention_only=self._filters.attention_only,
            include_completed=self._filters.include_completed,
        )
        self.refresh_data()

    def _resolve_controller(self) -> FleetController | None:
        if self._controller is not None:
            return self._controller
        candidate = getattr(self.runtime, "fleet", None)
        if isinstance(candidate, FleetController):
            return candidate
        return None

    def _resolve_worker_controller(self) -> FleetController | None:
        if self._worker_controller is not None:
            return self._worker_controller
        candidate = getattr(self.runtime, "sync_fleet", None)
        if isinstance(candidate, FleetController):
            return candidate
        return None

    def _update_status(self) -> None:
        if self._state is None:
            return
        story_count = len(self._state.story_lanes)
        status_parts = [
            f"{story_count} stor{'ies' if story_count != 1 else 'y'}",
            (
                f"{self._state.total_visible_agents} live"
                if self._state.total_visible_agents != 1
                else "1 live"
            ),
            f"{len(self._state.response_inbox)} waiting",
        ]
        selected_story = self._selected_story()
        if selected_story is not None:
            status_parts.insert(0, selected_story.story_label)
        if self._filters.attention_only:
            status_parts.append("attention")
        if not self._filters.include_completed:
            status_parts.append("hide-done")
        query = self._filters.normalized_query()
        if query:
            status_parts.append(f"filter:{query}")
        self.set_status(" · ".join(status_parts))


__all__ = ["FleetScreen"]
