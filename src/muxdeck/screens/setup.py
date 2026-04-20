from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical

from muxdeck.bindings import SETUP_BINDINGS, SETUP_HINTS
from muxdeck.screens.base import ShellScreen
from muxdeck.widgets.setup import DoctorDetailPanel, SetupSummaryPanel, SocketListPanel

if TYPE_CHECKING:
    from muxdeck.app import MuxdeckApp, MuxdeckRuntime


class SetupScreen(ShellScreen):
    SCREEN_TITLE = "SETUP"
    BINDINGS = SETUP_BINDINGS
    FOOTER_HINTS = SETUP_HINTS

    def __init__(self, runtime: MuxdeckRuntime) -> None:
        super().__init__(runtime)

    def compose_body(self) -> ComposeResult:
        with Vertical(id="setup-root"), Horizontal(id="setup-main", classes="frame"):
            yield SocketListPanel(widget_id="setup-sockets", classes="divider-right")
            with Vertical(id="setup-sidebar"):
                yield SetupSummaryPanel(id="setup-summary", classes="section")
                yield DoctorDetailPanel(id="setup-checks", classes="section-top")

    def on_mount(self) -> None:
        self.refresh_data()
        self.call_after_refresh(self.query_one(SocketListPanel).focus_list)

    def on_show(self) -> None:
        self.refresh_data()

    @property
    def muxdeck_app(self) -> MuxdeckApp:
        return cast("MuxdeckApp", self.app)

    def refresh_data(self) -> None:
        service = self.runtime.setup
        if service is None:
            self.query_one(SocketListPanel).set_options(())
            self.query_one(SetupSummaryPanel).set_report(None)
            self.query_one(DoctorDetailPanel).set_report(None)
            self.set_status("setup unavailable")
            return
        report = service.build_report()
        self.query_one(SocketListPanel).set_options(report.socket_options)
        self.query_one(SetupSummaryPanel).set_report(report)
        self.query_one(DoctorDetailPanel).set_report(report)
        issue_count = report.error_count + report.warning_count
        if issue_count:
            self.set_status(f"{issue_count} setup issue(s) detected")
            return
        self.set_status("tmux setup looks healthy")

    def action_cursor_down(self) -> None:
        self.query_one(SocketListPanel).move_cursor(1)

    def action_cursor_up(self) -> None:
        self.query_one(SocketListPanel).move_cursor(-1)

    def action_apply_socket(self) -> None:
        service = self.runtime.setup
        if service is None:
            self.set_status("setup unavailable")
            return
        option = self.query_one(SocketListPanel).selected_option()
        if option is None:
            self.set_status("no socket selected")
            return
        if option.is_selected:
            self.set_status("socket already active")
            return
        report = service.select_socket(option.socket_path)
        self.query_one(SocketListPanel).set_options(report.socket_options)
        self.query_one(SetupSummaryPanel).set_report(report)
        self.query_one(DoctorDetailPanel).set_report(report)
        if option.socket_path is None:
            self.set_status("using auto tmux socket selection")
            return
        self.set_status(f"using tmux socket {option.socket_path}")

    def action_clear_socket(self) -> None:
        service = self.runtime.setup
        if service is None:
            self.set_status("setup unavailable")
            return
        report = service.select_socket(None)
        self.query_one(SocketListPanel).set_options(report.socket_options)
        self.query_one(SetupSummaryPanel).set_report(report)
        self.query_one(DoctorDetailPanel).set_report(report)
        self.set_status("using auto tmux socket selection")


__all__ = ["SetupScreen"]
