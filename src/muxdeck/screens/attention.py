from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.worker import Worker, WorkerState

from muxdeck.bindings import ATTENTION_BINDINGS, ATTENTION_HINTS
from muxdeck.controllers.attention_controller import (
    AttentionFilterState,
    AttentionState,
)
from muxdeck.screens.base import ShellScreen
from muxdeck.services.attention_service import AttentionNotification
from muxdeck.widgets.attention import (
    AttentionActivityPanel,
    AttentionDetailPanel,
    AttentionListPanel,
    AttentionSummaryBar,
)
from muxdeck.widgets.dashboard import LogPreviewPanel

if TYPE_CHECKING:
    from muxdeck.app import MuxdeckApp, MuxdeckRuntime

_NOTIFY_SEVERITY: dict[str, Literal["information", "warning", "error"]] = {
    "info": "information",
    "warning": "warning",
    "error": "error",
}

_ATTENTION_WORKER = "attention_load"


class AttentionScreen(ShellScreen):
    SCREEN_TITLE = "ATTENTION"
    BINDINGS = ATTENTION_BINDINGS
    FOOTER_HINTS = ATTENTION_HINTS

    def __init__(self, runtime: MuxdeckRuntime) -> None:
        super().__init__(runtime)
        self._filters = AttentionFilterState()
        self._selected_agent_id: str | None = None
        self._state: AttentionState | None = None
        self._loading: bool = False
        self._refresh_pending: bool = False
        # Textual fires ``on_mount`` followed immediately by ``on_show``
        # on first activation. Without this guard the screen does
        # ``build_state`` twice back-to-back on every cold open, doubling
        # the dashboard query load on the worker pool.
        self._skip_next_show_refresh: bool = True

    @property
    def muxdeck_app(self) -> MuxdeckApp:
        return cast("MuxdeckApp", self.app)

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
        if self._skip_next_show_refresh:
            self._skip_next_show_refresh = False
            return
        self.refresh_data()

    def refresh_data(self) -> None:
        """Kick off a worker-thread build of the attention state.

        ``AttentionController.build_state`` is dominated by the
        embedded ``DashboardController.build_state`` call, which scans
        the agent store, computes operator status, and assembles
        previews — work that takes hundreds of milliseconds even on
        warm caches and seconds on cold ones. Doing it inline froze
        the UI on every tab activation, so the worker pattern matches
        ``DashboardScreen.refresh_data``.
        """
        controller = self.runtime.attention
        if controller is None:
            self.set_status("attention inbox unavailable")
            return
        if self._loading:
            # Coalesce concurrent refresh requests instead of stacking
            # workers; the latest snapshot is the only one that matters.
            self._refresh_pending = True
            return
        first_load = self._state is None
        loading_widgets = (
            self.query_one(AttentionListPanel),
            self.query_one(AttentionDetailPanel),
            self.query_one(LogPreviewPanel),
            self.query_one(AttentionActivityPanel),
        )
        if first_load:
            self.set_status("loading attention inbox…")
            self.begin_loading(*loading_widgets)

        filters = self._filters
        selected_id = self._selected_agent_id
        preview_lines = self._preview_line_limit()

        def _build() -> AttentionState | None:
            try:
                return controller.build_state(
                    filters=filters,
                    selected_agent_id=selected_id,
                    preview_line_limit=preview_lines,
                )
            except Exception:
                return None

        self._loading = True
        self.run_worker(_build, thread=True, exclusive=True, name=_ATTENTION_WORKER)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        super().on_worker_state_changed(event)
        if event.worker.name != _ATTENTION_WORKER:
            return
        loading_widgets = (
            self.query_one(AttentionListPanel),
            self.query_one(AttentionDetailPanel),
            self.query_one(LogPreviewPanel),
            self.query_one(AttentionActivityPanel),
        )
        if event.state == WorkerState.ERROR:
            self._loading = False
            self.end_loading(*loading_widgets)
            if self._state is None:
                self.set_status("attention inbox refresh failed")
            self._schedule_pending_refresh()
            return
        if event.state == WorkerState.CANCELLED:
            self._loading = False
            self.end_loading(*loading_widgets)
            self._schedule_pending_refresh()
            return
        if event.state != WorkerState.SUCCESS:
            return
        self._loading = False
        self.end_loading(*loading_widgets)
        state = cast("AttentionState | None", event.worker.result)
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

    def _apply_state(self, state: AttentionState) -> None:
        self._state = state
        self._selected_agent_id = state.selected_agent_id
        self.query_one(AttentionSummaryBar).set_state(state.summary, self._filters)
        self.query_one(AttentionListPanel).set_items(
            state.items,
            selected_agent_id=state.selected_agent_id,
        )
        self.query_one(AttentionDetailPanel).set_item(state.selected_item)
        self.query_one(LogPreviewPanel).set_logs(
            state.selected_item.agent if state.selected_item is not None else None
        )
        self.query_one(AttentionActivityPanel).set_items(state.items)
        self._emit_notifications(state.notifications)
        if not state.items:
            self.set_status("attention inbox clear")
            return
        self.set_status(
            f"{state.summary.total_items} attention · "
            f"{state.summary.unread_items} unread · "
            f"{state.summary.critical_items} critical"
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
        # Send enough preview lines to fill a tall panel — the
        # ``LogPreviewPanel`` widget tails this list to the rows that
        # physically fit on screen.
        return min(self.runtime.config.general.log_preview_lines, 200)


__all__ = ["AttentionScreen"]
