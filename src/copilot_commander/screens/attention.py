from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical

from copilot_commander.bindings import ATTENTION_BINDINGS, ATTENTION_HINTS
from copilot_commander.controllers.attention_controller import (
    AttentionFilterState,
    AttentionState,
)
from copilot_commander.screens.base import ShellScreen
from copilot_commander.services.attention_service import AttentionNotification
from copilot_commander.widgets.attention import (
    AttentionActivityPanel,
    AttentionDetailPanel,
    AttentionListPanel,
    AttentionSummaryBar,
)
from copilot_commander.widgets.dashboard import LogPreviewPanel

if TYPE_CHECKING:
    from copilot_commander.app import CommanderApp, CommanderRuntime

_NOTIFY_SEVERITY: dict[str, Literal["information", "warning", "error"]] = {
    "info": "information",
    "warning": "warning",
    "error": "error",
}


class AttentionScreen(ShellScreen):
    SCREEN_TITLE = "ATTENTION"
    BINDINGS = ATTENTION_BINDINGS
    FOOTER_HINTS = ATTENTION_HINTS

    def __init__(self, runtime: CommanderRuntime) -> None:
        super().__init__(runtime)
        self._filters = AttentionFilterState()
        self._selected_agent_id: str | None = None
        self._state: AttentionState | None = None

    @property
    def commander_app(self) -> CommanderApp:
        return cast("CommanderApp", self.app)

    def compose_body(self) -> ComposeResult:
        with Vertical(id="attention-root"):
            yield AttentionSummaryBar(id="attention-summary")
            with Horizontal(id="attention-main", classes="frame"):
                yield AttentionListPanel(widget_id="attention-list", classes="divider-right")
                with Vertical(id="attention-sidebar"):
                    yield AttentionDetailPanel(id="attention-detail", classes="section")
                    yield LogPreviewPanel(id="attention-log", classes="section-top")
                    yield AttentionActivityPanel(id="attention-activity", classes="section-top")

    def on_mount(self) -> None:
        self.refresh_data()
        self.call_after_refresh(self.query_one(AttentionListPanel).focus_list)

    def on_show(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        controller = getattr(self.runtime, "attention", None)
        if controller is None:
            self.set_status("attention inbox unavailable")
            return
        self._state = controller.build_state(
            filters=self._filters,
            selected_agent_id=self._selected_agent_id,
            preview_line_limit=self._preview_line_limit(),
        )
        self._selected_agent_id = self._state.selected_agent_id
        self.query_one(AttentionSummaryBar).set_state(self._state.summary, self._filters)
        self.query_one(AttentionListPanel).set_items(
            self._state.items,
            selected_agent_id=self._state.selected_agent_id,
        )
        self.query_one(AttentionDetailPanel).set_item(self._state.selected_item)
        self.query_one(LogPreviewPanel).set_logs(
            self._state.selected_item.agent if self._state.selected_item is not None else None
        )
        self.query_one(AttentionActivityPanel).set_items(self._state.items)
        self._emit_notifications(self._state.notifications)
        if not self._state.items:
            self.set_status("attention inbox clear")
            return
        self.set_status(
            f"{self._state.summary.total_items} attention · "
            f"{self._state.summary.unread_items} unread · "
            f"{self._state.summary.critical_items} critical"
        )

    def on_attention_list_panel_attention_selected(
        self,
        message: AttentionListPanel.AttentionSelected,
    ) -> None:
        if message.agent_id == self._selected_agent_id:
            return
        self._selected_agent_id = message.agent_id
        self.refresh_data()

    def action_cursor_down(self) -> None:
        self.query_one(AttentionListPanel).move_cursor(1)

    def action_cursor_up(self) -> None:
        self.query_one(AttentionListPanel).move_cursor(-1)

    def action_toggle_unread(self) -> None:
        self._filters = AttentionFilterState(unread_only=not self._filters.unread_only)
        label = "showing unread only" if self._filters.unread_only else "showing all items"
        self.set_status(label)
        self.refresh_data()

    def action_mark_selected_read(self) -> None:
        controller = getattr(self.runtime, "attention", None)
        if controller is None:
            self.set_status("attention inbox unavailable")
            return
        selected = self._state.selected_item if self._state is not None else None
        if selected is None:
            self.set_status("no attention item selected")
            return
        controller.mark_read(selected.item.alert_id)
        self.set_status(f"marked {selected.item.agent_name} read")
        self.refresh_data()

    def action_mark_all_read(self) -> None:
        controller = getattr(self.runtime, "attention", None)
        if controller is None:
            self.set_status("attention inbox unavailable")
            return
        controller.mark_all_read()
        self.set_status("marked all attention items read")
        self.refresh_data()

    def _emit_notifications(self, notifications: Sequence[AttentionNotification]) -> None:
        if not notifications:
            return
        self.app.bell()
        head = notifications[0]
        message = head.message
        if len(notifications) > 1:
            message = f"{message} (+{len(notifications) - 1} more critical)"
        self.app.notify(
            message,
            title=head.title,
            severity=_NOTIFY_SEVERITY[head.severity],
        )

    def _preview_line_limit(self) -> int:
        return min(self.runtime.config.general.log_preview_lines, 24)


__all__ = ["AttentionScreen"]
