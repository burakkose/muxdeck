from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen

from copilot_commander.bindings import GLOBAL_HINTS, KeyHint
from copilot_commander.widgets.common import KeyHintFooter, TabBar

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
        yield TabBar(
            active=self.SCREEN_TITLE.lower(),
            widget_id="shell-tab-bar",
        )
        with Vertical(id="shell-frame"):
            yield from self.compose_body()
        yield KeyHintFooter(
            hints=self.footer_hints(),
            status=self._status,
            widget_id="shell-footer",
        )

    def compose_body(self) -> ComposeResult:
        from textual.widgets import Static

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
