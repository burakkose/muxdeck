from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.worker import Worker, WorkerState

from muxdeck.bindings import SETUP_BINDINGS, SETUP_HINTS
from muxdeck.screens.base import ShellScreen
from muxdeck.services.setup_service import SetupDoctorReport
from muxdeck.widgets.setup import DoctorDetailPanel, SetupSummaryPanel, SocketListPanel

if TYPE_CHECKING:
    from muxdeck.app import MuxdeckApp, MuxdeckRuntime


_SETUP_WORKER = "setup_build_report"


class SetupScreen(ShellScreen):
    SCREEN_TITLE = "SETUP"
    BINDINGS = SETUP_BINDINGS
    FOOTER_HINTS = SETUP_HINTS

    # Tab-hop activations within this window reuse the cached report
    # instead of re-probing tmux + filesystem + git. The periodic
    # discovery timer in MuxdeckApp ensures the cached value never
    # ages past discovery_interval_sec in practice.
    _ACTIVATION_REFRESH_TTL_SEC: float = 3.0

    def __init__(self, runtime: MuxdeckRuntime) -> None:
        super().__init__(runtime)
        self._loading: bool = False
        self._cached_report: SetupDoctorReport | None = None
        self._last_refresh_completed_at: float = 0.0
        # Coalesce refresh requests that arrive while a worker is
        # still running so we never queue parallel build_report
        # invocations on the worker pool.
        self._refresh_pending: bool = False

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
        # Skip when a worker is already in flight: the in-flight
        # report will paint when it lands, so kicking another one
        # would just queue duplicate work via _refresh_pending.
        if self._loading:
            return
        # Throttle the show-driven refresh so fast Tab navigation
        # doesn't re-run build_report on every activation. The first
        # show after mount falls through because _last_refresh_completed_at
        # starts at 0.0.
        elapsed = time.monotonic() - self._last_refresh_completed_at
        if elapsed < self._ACTIVATION_REFRESH_TTL_SEC and self._cached_report is not None:
            return
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
        if self._loading:
            # build_report can block for hundreds of ms on WSL while
            # it probes tmux / scans the socket directory; coalesce
            # repeat requests rather than queuing parallel work.
            self._refresh_pending = True
            return
        if self._cached_report is None:
            self.set_status("loading setup report…")
        self._loading = True

        def _build() -> SetupDoctorReport | None:
            try:
                return service.build_report()
            except Exception:
                return None

        self.run_worker(_build, thread=True, exclusive=True, name=_SETUP_WORKER)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        super().on_worker_state_changed(event)
        if event.worker.name != _SETUP_WORKER:
            return
        if event.state in (WorkerState.ERROR, WorkerState.CANCELLED):
            self._loading = False
            if self._refresh_pending:
                self._refresh_pending = False
                self.refresh_data()
            return
        if event.state != WorkerState.SUCCESS:
            return
        report = cast("SetupDoctorReport | None", event.worker.result)
        self._loading = False
        self._last_refresh_completed_at = time.monotonic()
        if report is None:
            self.set_status("setup report failed")
            if self._refresh_pending:
                self._refresh_pending = False
                self.refresh_data()
            return
        self._apply_report(report)
        if self._refresh_pending:
            self._refresh_pending = False
            self.refresh_data()

    def _apply_report(self, report: SetupDoctorReport) -> None:
        self._cached_report = report
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
        self._apply_report(report)
        self._last_refresh_completed_at = time.monotonic()
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
        self._apply_report(report)
        self._last_refresh_completed_at = time.monotonic()
        self.set_status("using auto tmux socket selection")


__all__ = ["SetupScreen"]
