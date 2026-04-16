from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input
from textual.worker import Worker, WorkerState

from copilot_commander.bindings import BindingSpec, KeyHint
from copilot_commander.controllers.fleet_controller import (
    FleetController,
    FleetFilterState,
    FleetState,
)
from copilot_commander.screens.base import ShellScreen
from copilot_commander.widgets.fleet import (
    FleetGroupsPanel,
    FleetHistoryPanel,
    FleetResourcesPanel,
    FleetSearchPanel,
    FleetSummaryBar,
)

if TYPE_CHECKING:
    from copilot_commander.app import CommanderRuntime

_FLEET_BINDINGS: list[BindingSpec] = [
    Binding("slash", "focus_filter", "Filter", show=False),
    Binding("a", "toggle_attention", "Attention", show=False),
    Binding("x", "toggle_completed", "Completed", show=False),
]

_FLEET_HINTS = (
    KeyHint("/", "filter"),
    KeyHint("a", "attention"),
    KeyHint("x", "completed"),
)


_WORKER_NAME = "fleet_load"


class FleetScreen(ShellScreen):
    SCREEN_TITLE = "FLEET"
    BINDINGS = _FLEET_BINDINGS
    FOOTER_HINTS = _FLEET_HINTS

    def __init__(
        self,
        runtime: CommanderRuntime,
        *,
        controller: FleetController | None = None,
    ) -> None:
        super().__init__(runtime)
        self._controller = controller
        self._filters = FleetFilterState(include_completed=False)
        self._state: FleetState | None = None
        self._loading: bool = False

    def compose_body(self) -> ComposeResult:
        with Vertical(id="fleet-root"):
            yield FleetSummaryBar(id="fleet-summary")
            yield Input(placeholder="/ search fleet", id="fleet-filter-input")
            with Horizontal(id="fleet-main"):
                yield FleetGroupsPanel(id="fleet-groups", classes="panel")
                with Vertical(id="fleet-side"):
                    yield FleetResourcesPanel(id="fleet-resources", classes="panel")
                    yield FleetHistoryPanel(id="fleet-history", classes="panel")
                    yield FleetSearchPanel(id="fleet-search", classes="panel")

    def on_mount(self) -> None:
        self.refresh_data()

    def on_show(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        controller = self._resolve_controller()
        if controller is None:
            self.set_status("fleet controller unavailable")
            return
        first_load = self._state is None
        if first_load and not self._loading:
            self.set_status("loading fleet…")
            self.begin_loading(
                self.query_one(FleetGroupsPanel),
                self.query_one(FleetResourcesPanel),
                self.query_one(FleetHistoryPanel),
            )
        filters = self._filters

        def _load() -> FleetState:
            return controller.build_state(filters=filters)

        self._loading = True
        self.run_worker(_load, thread=True, exclusive=True, name=_WORKER_NAME)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != _WORKER_NAME:
            return
        if event.state == WorkerState.ERROR:
            self._loading = False
            self.end_loading(
                self.query_one(FleetGroupsPanel),
                self.query_one(FleetResourcesPanel),
                self.query_one(FleetHistoryPanel),
            )
            self.set_status("fleet load failed")
            return
        if event.state != WorkerState.SUCCESS:
            return
        self._loading = False
        self.end_loading(
            self.query_one(FleetGroupsPanel),
            self.query_one(FleetResourcesPanel),
            self.query_one(FleetHistoryPanel),
        )
        state = event.worker.result
        if state is None:
            return
        self._apply_state(state)

    def _apply_state(self, state: FleetState) -> None:
        self._state = state
        self.query_one(FleetSummaryBar).set_state(state)
        self.query_one(FleetGroupsPanel).set_groups(state.groups)
        self.query_one(FleetResourcesPanel).set_resources(state.resources)
        self.query_one(FleetHistoryPanel).set_history(state.history_metrics, state.recent_activity)
        self.query_one(FleetSearchPanel).set_search(
            query=self._filters.normalized_query(),
            helpers=state.search_helpers,
            hits=state.search_hits,
        )
        status_parts = [
            f"{state.total_groups} groups",
            f"{state.total_visible_agents} agents",
        ]
        if self._filters.attention_only:
            status_parts.append("attention")
        if not self._filters.include_completed:
            status_parts.append("hide-done")
        self.set_status(" · ".join(status_parts))

    def action_focus_filter(self) -> None:
        self.query_one("#fleet-filter-input", Input).focus()

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


__all__ = ["FleetScreen"]
