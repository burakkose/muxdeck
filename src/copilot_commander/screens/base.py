from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Header, Static

from copilot_commander.bindings import GLOBAL_HINTS, KeyHint
from copilot_commander.widgets.common import KeyHintFooter

if TYPE_CHECKING:
    from copilot_commander.app import CommanderRuntime


class ShellScreen(Screen[None]):
    SCREEN_TITLE = "SCREEN"
    FOOTER_HINTS: tuple[KeyHint, ...] = ()

    def __init__(self, runtime: CommanderRuntime) -> None:
        super().__init__()
        self.runtime = runtime
        self._status = "ready"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, id="shell-header")
        with Vertical(id="shell-frame"):
            yield Static(self.SCREEN_TITLE, id="screen-title")
            with Vertical(id="screen-body"):
                yield from self.compose_body()
        yield KeyHintFooter(
            title=self.SCREEN_TITLE,
            hints=self.footer_hints(),
            status=self._status,
            widget_id="shell-footer",
        )

    def compose_body(self) -> ComposeResult:
        yield Static()

    def footer_hints(self) -> tuple[KeyHint, ...]:
        return (*GLOBAL_HINTS, *self.FOOTER_HINTS)

    def set_status(self, message: str) -> None:
        self._status = message
        if self.is_mounted:
            footer = self.query_one(KeyHintFooter)
            footer.status = message

    def set_hints(self, hints: Iterable[KeyHint]) -> None:
        if self.is_mounted:
            self.query_one(KeyHintFooter).hints = tuple(hints)

    def refresh_data(self) -> None:
        return
