from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar, Protocol

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical

from muxdeck.bindings import BindingSpec, KeyHint
from muxdeck.controllers import (
    OperationsAction,
    OperationsActionPreview,
    OperationsExecutionSummary,
    OperationsState,
)
from muxdeck.exceptions import PersistenceError
from muxdeck.screens.base import ShellScreen
from muxdeck.screens.confirm_dialog import ConfirmScreen
from muxdeck.widgets.dashboard import AlertPanel, FleetHealthPanel
from muxdeck.widgets.operations import (
    BulkActionPreviewPanel,
    OperationsAgentListPanel,
    OperationsHistoryPanel,
    OperationsSelectionPanel,
)

if TYPE_CHECKING:
    from muxdeck.app import MuxdeckRuntime


class OperationsScreenController(Protocol):
    def build_state(
        self,
        *,
        selected_agent_ids: tuple[str, ...] = (),
        preview: OperationsActionPreview | None = None,
        preview_line_limit: int = 6,
        alert_limit: int = 6,
        history_limit: int = 12,
    ) -> OperationsState: ...

    def toggle_selection(
        self,
        selected_agent_ids: tuple[str, ...],
        agent_id: str,
    ) -> tuple[str, ...]: ...

    def select_all(self, agents: Sequence[object]) -> tuple[str, ...]: ...

    def clear_selection(self) -> tuple[str, ...]: ...

    def preview_action(
        self,
        action: OperationsAction,
        selected_agent_ids: tuple[str, ...],
    ) -> OperationsActionPreview: ...

    def execute_preview(self, preview: OperationsActionPreview) -> OperationsExecutionSummary: ...


OPERATIONS_BINDINGS: tuple[Binding, ...] = (
    Binding("j", "cursor_down", "Next agent", show=False),
    Binding("k", "cursor_up", "Prev agent", show=False),
    Binding("space", "toggle_selection", "Toggle selection", show=False),
    Binding("A", "select_all", "Select all", show=False),
    Binding("u", "clear_selection", "Clear selection", show=False),
    Binding("i", "preview_interrupt", "Preview interrupt", show=False),
    Binding("p", "preview_open_pane", "Preview pane", show=False),
    Binding("w", "preview_open_worktree", "Preview worktree", show=False),
    Binding("c", "preview_mark_complete", "Preview complete", show=False),
    Binding("x", "execute_preview", "Execute preview", show=False),
)

OPERATIONS_HINTS = (
    KeyHint("j/k", "move"),
    KeyHint("space", "select"),
    KeyHint("A", "all"),
    KeyHint("u", "clear"),
    KeyHint("i", "interrupt"),
    KeyHint("p", "pane"),
    KeyHint("w", "worktree"),
    KeyHint("c", "complete"),
    KeyHint("x", "execute"),
)


class OperationsScreen(ShellScreen):
    SCREEN_TITLE = "OPERATIONS"
    BINDINGS: ClassVar[list[BindingSpec]] = list(OPERATIONS_BINDINGS)
    FOOTER_HINTS = OPERATIONS_HINTS

    def __init__(self, runtime: MuxdeckRuntime, controller: OperationsScreenController) -> None:
        super().__init__(runtime)
        self._controller = controller
        self._selected_agent_ids: tuple[str, ...] = ()
        self._cursor_agent_id: str | None = None
        self._preview: OperationsActionPreview | None = None
        # Avoid the duplicate ``on_show`` refresh that would otherwise
        # immediately follow ``on_mount`` on first activation.
        self._skip_next_show_refresh: bool = True

    def compose_body(self) -> ComposeResult:
        with Vertical(id="operations-root"), Horizontal(id="operations-main", classes="frame"):
            yield OperationsAgentListPanel(
                widget_id="operations-agents",
                classes="divider-right focusable",
            )
            with Vertical(id="operations-sidebar"):
                with Horizontal(id="operations-top"):
                    yield OperationsSelectionPanel(
                        id="operations-selection",
                        classes="section divider-right",
                    )
                    yield FleetHealthPanel(id="operations-health", classes="section")
                with Horizontal(id="operations-bottom"):
                    yield BulkActionPreviewPanel(
                        id="operations-preview",
                        classes="section divider-right",
                    )
                    with Vertical(id="operations-sidepanels"):
                        yield AlertPanel(id="operations-alerts", classes="section")
                        yield OperationsHistoryPanel(
                            id="operations-history",
                            classes="section-top",
                        )

    def on_mount(self) -> None:
        self.refresh_data()
        self.call_after_refresh(self.query_one(OperationsAgentListPanel).focus_list)

    def on_show(self) -> None:
        if self._skip_next_show_refresh:
            self._skip_next_show_refresh = False
            return
        self.refresh_data()

    def refresh_data(self) -> None:
        state = self._controller.build_state(
            selected_agent_ids=self._selected_agent_ids,
            preview=self._preview,
        )
        self._selected_agent_ids = state.selected_agent_ids
        self._preview = state.preview
        if self._cursor_agent_id not in {agent.agent_id for agent in state.agents}:
            self._cursor_agent_id = state.agents[0].agent_id if state.agents else None
        selected_agents = tuple(
            agent for agent in state.agents if agent.agent_id in set(self._selected_agent_ids)
        )
        self.query_one(OperationsAgentListPanel).set_agents(
            state.agents,
            selected_agent_ids=self._selected_agent_ids,
            cursor_agent_id=self._cursor_agent_id,
        )
        self.query_one(OperationsSelectionPanel).set_selection(
            selected_agents,
            total_agents=len(state.agents),
        )
        self.query_one(FleetHealthPanel).set_state(state.health, None)
        self.query_one(AlertPanel).set_alerts(state.alerts)
        self.query_one(BulkActionPreviewPanel).set_preview(state.preview)
        self.query_one(OperationsHistoryPanel).set_entries(state.history)
        if self._preview is None:
            self.set_status(
                "no agents selected"
                if not self._selected_agent_ids
                else f"{len(self._selected_agent_ids)} agent(s) selected"
            )
            return
        self.set_status(self._preview.summary)

    def action_cursor_down(self) -> None:
        panel = self.query_one(OperationsAgentListPanel)
        panel.move_cursor(1)
        self._cursor_agent_id = panel.current_agent_id

    def action_cursor_up(self) -> None:
        panel = self.query_one(OperationsAgentListPanel)
        panel.move_cursor(-1)
        self._cursor_agent_id = panel.current_agent_id

    def action_toggle_selection(self) -> None:
        agent_id = self.query_one(OperationsAgentListPanel).current_agent_id
        if agent_id is None:
            self.set_status("no agent selected")
            return
        self._selected_agent_ids = self._controller.toggle_selection(
            self._selected_agent_ids,
            agent_id,
        )
        if self._preview is not None and agent_id not in self._selected_agent_ids:
            self._preview = None
        self.refresh_data()

    def action_select_all(self) -> None:
        panel = self.query_one(OperationsAgentListPanel)
        self._selected_agent_ids = self._controller.select_all(panel.agents)
        self.refresh_data()

    def action_clear_selection(self) -> None:
        self._selected_agent_ids = self._controller.clear_selection()
        self._preview = None
        self.refresh_data()

    def action_preview_interrupt(self) -> None:
        self._build_preview(OperationsAction.INTERRUPT)

    def action_preview_open_pane(self) -> None:
        self._build_preview(OperationsAction.OPEN_PANE)

    def action_preview_open_worktree(self) -> None:
        self._build_preview(OperationsAction.OPEN_WORKTREE)

    def action_preview_mark_complete(self) -> None:
        self._build_preview(OperationsAction.MARK_COMPLETE)

    def action_execute_preview(self) -> None:
        if self._preview is None:
            self.set_status("no preview to execute")
            return
        if self._preview.requires_confirmation:
            self.app.push_screen(
                ConfirmScreen(
                    message=self._preview.confirmation_message,
                    title=self._preview.label,
                ),
                callback=self._on_execution_confirmed,
            )
            return
        self._apply_execution(self._preview)

    def _build_preview(self, action: OperationsAction) -> None:
        try:
            self._preview = self._controller.preview_action(action, self._selected_agent_ids)
        except (PersistenceError, RuntimeError, ValueError) as exc:
            self.set_status(str(exc))
            return
        self.refresh_data()

    def _on_execution_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            self.set_status("bulk action cancelled")
            return
        if self._preview is None:
            self.set_status("preview expired")
            return
        self._apply_execution(self._preview)

    def _apply_execution(self, preview: OperationsActionPreview) -> None:
        try:
            result = self._controller.execute_preview(preview)
        except (PersistenceError, RuntimeError, ValueError) as exc:
            self.set_status(str(exc))
            return
        self._preview = None
        self.refresh_data()
        self.set_status(result.status_message)


__all__ = ["OPERATIONS_BINDINGS", "OPERATIONS_HINTS", "OperationsScreen"]
