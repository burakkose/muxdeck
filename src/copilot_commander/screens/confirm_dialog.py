"""Confirmation modal for destructive actions."""

from __future__ import annotations

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label

from copilot_commander import theme
from copilot_commander.bindings import BindingSpec


class ConfirmScreen(ModalScreen[bool]):
    """Modal confirmation dialog. Dismisses with True (confirm) or False (cancel)."""

    DEFAULT_CSS = f"""
    ConfirmScreen {{
        align: center middle;
    }}
    #confirm-dialog {{
        width: 50;
        height: auto;
        max-height: 10;
        background: {theme.BG1};
        border: thick {theme.RED};
        border-title-color: {theme.RED};
        padding: 1 2;
    }}
    #confirm-message {{
        height: auto;
        margin-bottom: 1;
        color: {theme.FG};
    }}
    #confirm-buttons {{
        height: auto;
        align: right middle;
    }}
    #confirm-buttons Button {{
        margin-left: 1;
        min-width: 10;
    }}
    #btn-yes {{
        background: {theme.TONE_CRITICAL_BG};
        color: {theme.RED};
        border: none;
    }}
    #btn-yes:hover {{
        background: {theme.RED};
        color: {theme.BG1};
    }}
    #btn-no {{
        background: {theme.BG3};
        color: {theme.FG3};
        border: none;
    }}
    #btn-no:hover {{
        background: {theme.BG4};
    }}
    """

    BINDINGS: ClassVar[list[BindingSpec]] = [
        ("escape", "cancel", "Cancel"),
        ("y", "confirm", "Yes"),
        ("n", "cancel", "No"),
        ("left,h", "focus_no", "←No"),
        ("right,l", "focus_yes", "→Yes"),
        ("enter", "press_focused", "Select"),
    ]

    def __init__(self, message: str, title: str = "Confirm") -> None:
        super().__init__()
        self._message = message
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog") as dialog:
            dialog.border_title = self._title
            yield Label(self._message, id="confirm-message")
            with Horizontal(id="confirm-buttons"):
                yield Button("No", id="btn-no", variant="default")
                yield Button("Yes", id="btn-yes", variant="error")

    def on_mount(self) -> None:
        self.query_one("#btn-no", Button).focus()

    @on(Button.Pressed, "#btn-yes")
    def _on_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#btn-no")
    def _on_no(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_focus_no(self) -> None:
        self.query_one("#btn-no", Button).focus()

    def action_focus_yes(self) -> None:
        self.query_one("#btn-yes", Button).focus()

    def action_press_focused(self) -> None:
        focused = self.focused
        if isinstance(focused, Button):
            focused.press()
