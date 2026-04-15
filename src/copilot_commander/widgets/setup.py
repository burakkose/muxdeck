from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import ListItem, ListView, Static

from copilot_commander.services import SetupDoctorReport, TmuxSocketOption
from copilot_commander.theme import BLUE, FG, FG1, FG4, GREEN, ORANGE, RED

_CHECK_STYLES: dict[str, tuple[str, str]] = {
    "ok": ("✓", GREEN),
    "warning": ("!", ORANGE),
    "error": ("✗", RED),
    "info": ("·", BLUE),
}


class SocketListPanel(Vertical):
    def __init__(self, *, widget_id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=widget_id, classes=classes)
        self._options: tuple[TmuxSocketOption, ...] = ()

    def compose(self) -> ComposeResult:
        yield ListView(id="setup-socket-list")

    def set_options(self, options: Sequence[TmuxSocketOption]) -> None:
        list_view = self.query_one(ListView)
        list_view.clear()
        self._options = tuple(options)
        selected_index = 0
        for index, option in enumerate(self._options):
            list_view.append(ListItem(Static(self._render_option(option))))
            if option.is_selected:
                selected_index = index
        if self._options:
            list_view.index = selected_index

    def move_cursor(self, delta: int) -> None:
        if not self._options:
            return
        list_view = self.query_one(ListView)
        current = list_view.index if list_view.index is not None else 0
        list_view.index = max(0, min(len(self._options) - 1, current + delta))
        list_view.focus()

    def focus_list(self) -> None:
        self.query_one(ListView).focus()

    def selected_option(self) -> TmuxSocketOption | None:
        list_view = self.query_one(ListView)
        index = list_view.index
        if index is None or index >= len(self._options):
            return None
        return self._options[index]

    def _render_option(self, option: TmuxSocketOption) -> Text:
        line = Text()
        if option.is_selected:
            line.append("● ", style=f"bold {BLUE}")
        else:
            line.append("○ ", style=FG4)
        line.append(option.label, style=f"bold {FG}" if option.exists else f"bold {ORANGE}")
        if option.note:
            line.append("  ")
            line.append(option.note, style=FG4)
        return line


class SetupSummaryPanel(Static):
    def set_report(self, report: SetupDoctorReport | None) -> None:
        if report is None:
            self.update(Text("Setup diagnostics unavailable", style=FG4))
            return
        status_glyph, status_color = _CHECK_STYLES[report.overall_status]
        lines: list[Text] = []
        for label, value, style in (
            ("health", f"{status_glyph} {report.overall_status}", f"bold {status_color}"),
            (
                "target",
                report.effective_socket_path or "tmux default lookup",
                FG1,
            ),
            ("attached", report.attached_socket_path or "-", FG1),
            ("configured", report.configured_socket_path or "-", FG1),
            (
                "panes",
                "-" if report.pane_count is None else str(report.pane_count),
                FG1,
            ),
        ):
            line = Text()
            line.append(f"{label:<10}", style=FG4)
            line.append(value, style=style)
            lines.append(line)
        result = Text()
        for index, line in enumerate(lines):
            if index:
                result.append("\n")
            result.append_text(line)
        self.update(result)


class DoctorDetailPanel(Static):
    def set_report(self, report: SetupDoctorReport | None) -> None:
        if report is None:
            self.update(Text("No doctor report available", style=FG4))
            return
        content = Text()
        for index, check in enumerate(report.checks):
            glyph, color = _CHECK_STYLES[check.status]
            if index:
                content.append("\n")
            content.append(f"{glyph} ", style=f"bold {color}")
            content.append(check.title, style=f"bold {FG}")
            content.append("\n")
            content.append(f"  {check.detail}", style=FG4)
        self.update(content)


__all__ = ["DoctorDetailPanel", "SetupSummaryPanel", "SocketListPanel"]
