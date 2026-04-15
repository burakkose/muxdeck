from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical

from copilot_commander.bindings import KeyHint
from copilot_commander.controllers import AttentionInboxController, AttentionInboxState
from copilot_commander.screens.base import ShellScreen
from copilot_commander.widgets.attention import (
    AttentionInboxDetailPanel,
    AttentionInboxListPanel,
    AttentionInboxSummaryPanel,
)

if TYPE_CHECKING:
    from copilot_commander.app import CommanderRuntime

_ATTENTION_HINTS = (
    KeyHint("j/k", "move"),
    KeyHint("a", "ack"),
)


class AttentionInboxScreen(ShellScreen):
    SCREEN_TITLE = "ATTENTION"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("j", "cursor_down", "Next alert", show=False),
        Binding("k", "cursor_up", "Prev alert", show=False),
        Binding("a", "acknowledge_selected", "Acknowledge", show=False),
    ]
    FOOTER_HINTS = _ATTENTION_HINTS

    def __init__(self, runtime: CommanderRuntime) -> None:
        super().__init__(runtime)
        self._controller = AttentionInboxController(runtime.dashboard)
        self._selected_alert_key: str | None = None
        self._state: AttentionInboxState | None = None

    def compose_body(self) -> ComposeResult:
        with Vertical(id="attention-root"):
            yield AttentionInboxSummaryPanel(id="attention-summary", classes="section")
            with Horizontal(id="attention-main", classes="frame"):
                yield AttentionInboxListPanel(
                    widget_id="attention-list",
                    classes="divider-right",
                )
                yield AttentionInboxDetailPanel(id="attention-detail", classes="section")

    def on_mount(self) -> None:
        self.refresh_data()
        self.call_after_refresh(self.query_one(AttentionInboxListPanel).focus_list)

    def refresh_data(self) -> None:
        state = self._controller.build_state(selected_alert_key=self._selected_alert_key)
        if state.selected_row is not None and self._controller.mark_read(
            state.selected_row.alert_key
        ):
            state = self._controller.build_state(selected_alert_key=state.selected_alert_key)
        self._state = state
        self._selected_alert_key = state.selected_alert_key
        self.query_one(AttentionInboxSummaryPanel).set_state(state.summary, state.health)
        self.query_one(AttentionInboxListPanel).set_rows(
            state.rows,
            selected_alert_key=state.selected_alert_key,
        )
        self.query_one(AttentionInboxDetailPanel).set_row(state.selected_row)
        if not state.rows:
            self.set_status("attention inbox clear")
            return
        self.set_status(
            f"{state.summary.total_rows} items · "
            f"{state.summary.unread_rows} unread · "
            f"{state.summary.critical_rows} critical"
        )

    def on_attention_inbox_list_panel_row_selected(
        self,
        message: AttentionInboxListPanel.RowSelected,
    ) -> None:
        if message.alert_key == self._selected_alert_key:
            return
        self._selected_alert_key = message.alert_key
        self.refresh_data()

    def action_cursor_down(self) -> None:
        self.query_one(AttentionInboxListPanel).move_cursor(1)

    def action_cursor_up(self) -> None:
        self.query_one(AttentionInboxListPanel).move_cursor(-1)

    def action_acknowledge_selected(self) -> None:
        if self._selected_alert_key is None:
            self.set_status("no alert selected")
            return
        if not self._controller.acknowledge(self._selected_alert_key):
            self.set_status("alert already acknowledged")
            return
        self.refresh_data()
        if self._state is None or self._state.selected_row is None:
            self.set_status("alert acknowledged")
            return
        self.set_status(f"acknowledged {self._state.selected_row.agent_name}")


__all__ = ["AttentionInboxScreen"]
